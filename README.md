# eGPU Helper

A standalone Decky plugin for the eGPU with PCI ID `1002:73ff`.

- Read connection and driver status from PCI sysfs every two seconds.
- Show each device reported by `boltctl list`, including authorized, connected
  (not yet authorized), and disconnected states. This includes non-eGPU devices.
- Manually restart `gamescope-session.target` after confirmation.

Thunderbolt/USB4 status is read-only and refreshes independently every two
seconds after each response. **Refresh** fetches it immediately; overlapping
requests are prevented. It requires `/usr/bin/boltctl` and a working boltd
service. Missing tools, timeouts, and errors appear separately without blocking
PCI status or restart controls. An empty list means no devices were reported,
not proof that a specific eGPU is disconnected. Authorization is not proof of
GPU rendering. The plugin never enrolls or authorizes devices.

The plugin runs as the Decky installation user, **without root**. It does not
install, modify, or execute your existing script. Your automatic service can
remain enabled. Manual restart calls `systemctl --user --no-block restart
gamescope-session.target` directly, bypassing the script's marker guard without
removing its marker.

Restart can close games and restart Steam. Save first. The button is disabled
only while requesting a restart or during the reported 15-second cooldown.
GPU detection, driver readiness, and status polling errors do not disable it.
After confirmation, the backend requires an active Gamescope target and checks
that the automatic service is not starting/stopping. Failures appear in the panel.
“Restart queued” means systemd accepted the request, not that the fix succeeded.

Connection does not prove that Gamescope or a game is rendering on the eGPU.
The script creates its marker before restarting, so a present marker is not
proof of success either. A completed oneshot service normally becomes inactive.
The PCI ID identifies a model, not whether its physical connection is external.

## Build and install

From this directory:

```sh
pnpm install
pnpm test
pnpm package
```

Enable Decky developer mode and install `decky-egpu-helper.zip`. This package is
independent of the ROG Ally controller plugin.

## Hardware checks

On the Ally, check disconnected, connected, and driver-not-ready states.
Cancel confirmation once to verify that nothing restarts. Then save/close games
and confirm a restart. Reopen Decky and verify Steam/Gamescope recovered.
Check service failures using:

```sh
systemctl --user status egpu-gamescope-fix.service
journalctl --user -u egpu-gamescope-fix.service -b
```

Local tests mock Linux hardware and systemd. Live Decky rendering and actual
eGPU/Gamescope behavior require verification on the Ally.
