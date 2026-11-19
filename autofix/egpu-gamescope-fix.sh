#!/bin/bash
# Managed by eGPU Helper. Edits are overwritten when the plugin updates.
EGPU_ID="@EGPU_ID@"
MAX_WAIT=20
# Wait up to 20 seconds for the eGPU to enumerate.
for ((i=0; i<MAX_WAIT; i++)); do
    if lspci -Dn | grep -Eqi "$EGPU_ID"; then
        echo "eGPU detected: $EGPU_ID"
        # Give amdgpu/DRM a little more time to finish initialization.
        sleep 3
        # Avoid restarting Gamescope more than once for this boot/session.
        MARKER="/run/user/$(id -u)/egpu-gamescope-restarted"
        if [[ ! -e "$MARKER" ]]; then
            touch "$MARKER"
            systemctl --user restart gamescope-session.target
        fi
        exit 0
    fi
    sleep 1
done
# No eGPU -> handheld mode, do absolutely nothing.
exit 0
