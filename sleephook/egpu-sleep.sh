#!/bin/bash
# Managed by eGPU Helper. Edits are overwritten when the plugin updates.
# pre: detach the eGPU from PCI before sleep. post: rescan PCI after wake.
# With keep-dock, pre also releases a docked USB4 controller and post rebinds it.
# pm on|auto: set runtime PM for the USB4 controller and the PCIe ports it tunnels.
EGPU_ID='@EGPU_ID@'
# Present while the Keep dock linked setting is on.
KEEP_DOCK="${0%/*}/keep-dock"
RELEASED=/run/egpu-sleep-released
TB=/sys/bus/pci/drivers/thunderbolt
case "$1" in
pre)
    for dev in /sys/bus/pci/devices/*; do
        # Removing a function earlier in this loop makes its later entry stale.
        [[ -e "$dev/vendor" ]] || continue
        [[ "$(cat "$dev/vendor"):$(cat "$dev/device")" =~ $EGPU_ID ]] || continue
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
