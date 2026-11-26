#!/bin/bash
# Managed by eGPU Helper. Edits are overwritten when the plugin updates.
# pre: detach the eGPU from PCI before sleep. post: rescan PCI after wake.
# With keep-dock, pre also releases a docked USB4 controller and post rebinds it.
# pm on|auto: set runtime PM for the USB4 controller and the PCIe ports it tunnels.
# rebar: give a hot-plugged eGPU its full BAR. The Decky user may run this through sudo
# without a password, so never trust the caller's environment.
PATH=/usr/bin:/bin
EGPU_ID='@EGPU_ID@'
# Present while the Keep dock linked setting is on.
KEEP_DOCK="${0%/*}/keep-dock"
RELEASED=/run/egpu-sleep-released
STOPPED=/run/egpu-sleep-stopped
REBAR_TRIED=/run/egpu-rebar-tried
# Root daemons that keep the eGPU open: LACT.
DAEMONS="lactd.service"
# The card keeps the fan setting made before it is detached for the whole sleep, and
# nothing can change it until amdgpu probes the card again. Keep the speed the card's
# own curve runs now, so a card hot from a game keeps cooling. Never stop the fan: a
# wake that leaves the dock unlinked keeps the card powered with no driver for an
# unknown time. A card resting at zero RPM gets 25% of its top speed: an RX 6600 fan
# (top 3630 rpm) was only ever seen spinning from about 765 rpm.
hold_fan() {
    local hw=$1 now max target pwm
    now=$(cat "$hw/fan1_input" 2>/dev/null || echo 0)
    max=$(cat "$hw/fan1_max" 2>/dev/null || echo 0)
    target=$((max * 25 / 100))
    ((now > target)) && target=$now
    if [[ -w "$hw/fan1_target" ]] && ((max > 0)) &&
        echo 1 > "$hw/fan1_enable" && echo "$target" > "$hw/fan1_target"; then
        echo "egpu-sleep: fan at $now rpm, holding it at $target rpm"
    # Without RPM control, keep the current PWM, at least 30%: an RX 6600 stays still at 25%.
    elif [[ -w "$hw/pwm1" ]]; then
        pwm=$(cat "$hw/pwm1" 2>/dev/null || echo 0)
        ((pwm < 77)) && pwm=77
        echo 1 > "$hw/pwm1_enable" && echo "$pwm" > "$hw/pwm1" &&
            echo "egpu-sleep: holding the fan at PWM $pwm"
    fi
}
TB=/sys/bus/pci/drivers/thunderbolt
egpus() {
    for dev in /sys/bus/pci/devices/*; do
        [[ -e "$dev/vendor" && "$(cat "$dev/vendor"):$(cat "$dev/device")" =~ $EGPU_ID ]] && echo "$dev"
    done
}

# Login users with a process that has an eGPU's DRM node open.
egpu_users() {
    local match=() node
    for dev in $(egpus); do
        for node in "$dev"/drm/card* "$dev"/drm/renderD*; do
            [[ -e "$node" ]] && match+=(-o -lname "/dev/dri/${node##*/}")
        done
    done
    ((${#match[@]})) || return 0
    find /proc/[0-9]*/fd -maxdepth 1 \( "${match[@]:1}" \) -printf '%h\n' 2>/dev/null |
        cut -d/ -f3 | sort -u | xargs -r -I{} stat -c %u /proc/{} 2>/dev/null | sort -u |
        while read -r uid; do ((uid >= 1000)) && id -nu "$uid"; done
}

# 0 when BAR 0 is smaller than the largest size the card can resize it to.
bar_too_small() {
    local sizes bit=0 top=0 start end
    sizes=$(cat "$1/resource0_resize" 2>/dev/null) || return 1
    sizes=$((16#$sizes))
    while ((sizes >> bit)); do
        ((sizes >> bit & 1)) && top=$bit
        ((bit++))
    done
    ((top)) || return 1
    read -r start end _ < "$1/resource"
    (( end - start + 1 < 1 << (top + 20) ))
}

stop_daemons() {
    for unit in $DAEMONS; do
        systemctl is-active --quiet "$unit" || continue
        echo "egpu-sleep: stopping $unit"
        timeout 15 systemctl stop "$unit" && echo "$unit" >> "$STOPPED"
    done
}

start_daemons() {
    [[ -f "$STOPPED" ]] || return 0
    while read -r unit; do
        echo "egpu-sleep: starting $unit"
        systemctl --no-block start "$unit"
    done < "$STOPPED"
    rm -f "$STOPPED"
}

# Stop what holds the eGPU before it is removed. 0 once the card is free.
free_egpu() {
    # A Gamescope session using the eGPU crashes when the card is removed. Stop it
    # cleanly first. SDDM logs the user back in, on the built-in GPU while the eGPU
    # is gone, and auto-switch moves the session back once the card returns.
    for user in $(egpu_users); do
        echo "egpu-sleep: stopping the Gamescope session of $user"
        timeout 20 systemctl --user -M "$user@" stop gamescope-session.target ||
            echo "egpu-sleep: could not stop the session of $user"
    done
    # GPU daemons running as root segfault when their card is removed and can leave
    # a stale socket that blocks their restart. Stop them now, start them afterwards.
    stop_daemons
    # Stopping the target returns before gamescope itself exits. Wait for the card to be free.
    for _ in $(seq 50); do
        [[ -z "$(egpu_users)" ]] && return 0
        sleep 0.2
    done
    return 1
}

case "$1" in
pre)
    free_egpu || echo "egpu-sleep: eGPU still in use after 10s; removing it anyway"
    for dev in /sys/bus/pci/devices/*; do
        # Removing a function earlier in this loop makes its later entry stale.
        [[ -e "$dev/vendor" ]] || continue
        [[ "$(cat "$dev/vendor"):$(cat "$dev/device")" =~ $EGPU_ID ]] || continue
        # Without amdgpu the card's firmware runs its fans hard. A manual speed set
        # now is kept after removal; amdgpu returns the fans to auto when it probes again.
        for hw in "$dev"/hwmon/hwmon*; do
            [[ -d "$hw" ]] && hold_fan "$hw"
        done
        # Remove every function of the card (GPU and HDMI audio), audio first.
        for fn in $(ls -d "${dev%.*}".* | sort -r); do
            echo "egpu-sleep: removing $(basename "$fn")"
            echo 1 > "$fn/remove"
        done
    done
    # Some docks never leave the USB4 router sleep the kernel puts them in.
    # Release the controller the way shutdown does, which skips that sleep.
    # The Ally then stays out of hardware sleep until wake.
    [[ -e "$KEEP_DOCK" ]] || exit 0
    for nhi in "$TB"/0000:*; do
        # Only when a device is attached; an idle controller sleeps fine.
        compgen -G "$nhi/domain*/*-0/*-*" > /dev/null || continue
        echo "egpu-sleep: releasing USB4 controller ${nhi##*/}"
        echo "${nhi##*/}" >> "$RELEASED"
        echo "${nhi##*/}" > "$TB/unbind"
    done
    ;;
post)
    if [[ -f "$RELEASED" ]]; then
        while read -r nhi; do
            echo "egpu-sleep: rebinding USB4 controller $nhi"
            echo "$nhi" > "$TB/bind" || echo "egpu-sleep: rebinding $nhi failed; reboot to recover the dock"
        done < "$RELEASED"
        rm -f "$RELEASED"
    fi
    echo "egpu-sleep: rescanning PCI"
    echo 1 > /sys/bus/pci/rescan
    start_daemons
    ;;
rebar)
    # A dock plugged in after boot arrives as a hot-plug. The kernel gives its empty
    # downstream ports most of the address space, so amdgpu keeps a 256 MB BAR. Remove
    # those empty ports and the card, rescan only the card's bridge, and amdgpu can
    # resize on the fresh probe. Exit 0 only when the card was re-added.
    # The plugin discards this output; keep it in the journal (journalctl -t egpu-sleep).
    exec > >(systemd-cat -t egpu-sleep) 2>&1
    dev=$(egpus | head -1)
    [[ -n "$dev" ]] && bar_too_small "$dev" || exit 3
    dev=$(readlink -f "$dev")
    bridge=${dev%/*}
    # One attempt per dock arrival: a replugged dock gets a new bridge device.
    tried="$(stat -c %i "$bridge") ${bridge##*/}"
    [[ "$(cat "$REBAR_TRIED" 2>/dev/null)" == "$tried" ]] && { echo "egpu-sleep: rebar already tried"; exit 3; }
    echo "$tried" > "$REBAR_TRIED"
    # A Game Mode session on the built-in GPU can still hold the eGPU's nodes. Stopping
    # only the session makes SDDM log back in at once, on the built-in GPU, so stop SDDM
    # until the card is back: the session then starts once, on the eGPU. Leave any other
    # session (Desktop Mode) alone.
    sddm_stopped=
    for user in $(egpu_users); do
        systemctl --user -M "$user@" is-active --quiet gamescope-session.target ||
            { echo "egpu-sleep: $user holds the eGPU outside Game Mode; not resizing"; exit 3; }
    done
    if [[ -n "$(egpu_users)" ]]; then
        echo "egpu-sleep: stopping SDDM until the eGPU is back"
        timeout 30 systemctl stop sddm.service && sddm_stopped=1
    fi
    if ! free_egpu; then
        echo "egpu-sleep: eGPU still in use; not resizing"
        start_daemons
        [[ -n "$sddm_stopped" ]] && systemctl --no-block start sddm.service
        exit 1
    fi
    # Empty sibling bridges of every bridge between the card and the root port.
    up=$bridge
    while [[ -e "${up%/*}/vendor" ]]; do
        for port in "${up%/*}"/0000:*; do
            [[ "$port" != "$up" && -e "$port/vendor" && "$(cat "$port/class")" == 0x0604* ]] || continue
            compgen -G "$port/0000:*/vendor" > /dev/null && continue
            echo "egpu-sleep: removing empty port ${port##*/}"
            echo 1 > "$port/remove"
        done
        up=${up%/*}
    done
    for fn in $(ls -d "${dev%.*}".* | sort -r); do
        echo "egpu-sleep: removing $(basename "$fn")"
        echo 1 > "$fn/remove"
    done
    echo "egpu-sleep: rescanning ${bridge##*/}"
    echo 1 > "$bridge/rescan"
    for _ in $(seq 100); do
        compgen -G "$dev/drm/card*" > /dev/null && break
        sleep 0.2
    done
    start_daemons
    [[ -n "$sddm_stopped" ]] && systemctl --no-block start sddm.service
    [[ -e "$dev/resource" ]] || { echo "egpu-sleep: the eGPU did not come back"; exit 1; }
    bar_too_small "$dev" && echo "egpu-sleep: BAR still small" || echo "egpu-sleep: BAR resized"
    exit 0
    ;;
pm)
    # When the USB4 controller runtime-suspends, the dock drops its USB4 link.
    # When the tunnel's PCIe port runtime-suspends, it misses eGPU hotplug.
    [[ "$2" == on || "$2" == auto ]] || exit 1
    shopt -s nullglob
    for nhi in "$TB"/0000:*; do
        echo "$2" > "$nhi/power/control"
        for port in "$nhi"/consumer:pci:*; do
            echo "$2" > "$port/consumer/power/control"
        done
    done
    ;;
esac
exit 0
