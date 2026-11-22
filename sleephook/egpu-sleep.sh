#!/bin/bash
# Managed by eGPU Helper. Edits are overwritten when the plugin updates.
# pre: detach the eGPU from PCI before sleep. post: rescan PCI after wake.
# With keep-dock, pre also releases a docked USB4 controller and post rebinds it.
# pm on|auto: set runtime PM for the USB4 controller and the PCIe ports it tunnels.
EGPU_ID='@EGPU_ID@'
# Present while the Keep dock linked setting is on.
KEEP_DOCK="${0%/*}/keep-dock"
RELEASED=/run/egpu-sleep-released
STOPPED=/run/egpu-sleep-stopped
# Root daemons that keep the eGPU open: LACT.
DAEMONS="lactd.service"
# The card keeps the fan setting made before it is detached for the whole sleep, and
# nothing can change it until amdgpu probes the card again. Pick the speed from the
# hottest sensor so a card hot from a game keeps cooling, and never stop the fan: a wake
# that leaves the dock unlinked keeps the card powered with no driver for an unknown time.
hottest_temp() {
    local hottest=0 t
    for t in "$1"/temp*_input; do
        [[ -r "$t" ]] && (( $(cat "$t") > hottest )) && hottest=$(cat "$t")
    done
    echo "$hottest"
}
# Percent of the card's top fan speed.
sleep_fan_percent() {
    if (($1 < 55000)); then echo 25
    elif (($1 < 70000)); then echo 35
    elif (($1 < 85000)); then echo 45
    else echo 60
    fi
}
# Ask for an RPM, not a PWM: fans start at different PWMs (an RX 6600 stays still at
# 25% PWM), while the card's firmware drives an RPM target itself.
hold_fan() {
    local hw=$1 temp percent max
    temp=$(hottest_temp "$hw")
    percent=$(sleep_fan_percent "$temp")
    max=$(cat "$hw/fan1_max" 2>/dev/null)
    if [[ -w "$hw/fan1_target" ]] && ((max > 0)) &&
        echo 1 > "$hw/fan1_enable" && echo $((max * percent / 100)) > "$hw/fan1_target"; then
        echo "egpu-sleep: hottest sensor $((temp / 1000)) C, holding the fan at $((max * percent / 100)) rpm"
    elif [[ -w "$hw/pwm1" ]] && echo 1 > "$hw/pwm1_enable" && echo $((255 * percent / 100 + 25)) > "$hw/pwm1"; then
        echo "egpu-sleep: hottest sensor $((temp / 1000)) C, holding the fan at PWM $((255 * percent / 100 + 25))"
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

case "$1" in
pre)
    # A Gamescope session rendering on the eGPU crashes when the card is removed.
    # Stop it cleanly first. SDDM logs the user back in, on the built-in GPU once
    # the eGPU is gone, and auto-switch can move the session back after wake.
    for user in $(egpu_users); do
        echo "egpu-sleep: stopping the Gamescope session of $user"
        timeout 20 systemctl --user -M "$user@" stop gamescope-session.target ||
            echo "egpu-sleep: could not stop the session of $user"
    done
    # GPU daemons running as root segfault when their card is removed and can leave
    # a stale socket that blocks their restart. Stop them now, start them after wake.
    for unit in $DAEMONS; do
        systemctl is-active --quiet "$unit" || continue
        echo "egpu-sleep: stopping $unit"
        timeout 15 systemctl stop "$unit" && echo "$unit" >> "$STOPPED"
    done
    # Stopping the target returns before gamescope itself exits. Wait for the card to be free.
    for _ in $(seq 50); do
        [[ -z "$(egpu_users)" ]] && break
        sleep 0.2
    done
    [[ -z "$(egpu_users)" ]] || echo "egpu-sleep: eGPU still in use after 10s; removing it anyway"
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
    if [[ -f "$STOPPED" ]]; then
        while read -r unit; do
            echo "egpu-sleep: starting $unit"
            systemctl --no-block start "$unit"
        done < "$STOPPED"
        rm -f "$STOPPED"
    fi
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
