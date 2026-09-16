# eGPU Gamescope

A standalone Decky plugin for the eGPU with PCI ID `1002:73ff`.

- Read connection and driver status from PCI sysfs every two seconds.
- Show the existing `egpu-gamescope-fix.service` state and script marker.
- Manually restart `gamescope-session.target` after confirmation.

The plugin runs as the Decky installation user, **without root**. It does not
install, modify, or execute your existing script. Your automatic service can
remain enabled. Manual restart calls `systemctl --user --no-block restart
gamescope-session.target` directly, bypassing the script's marker guard without
removing its marker.

Restart can close games and restart Steam. Save first. The button is disabled
without an `amdgpu`-bound matching GPU, an active Gamescope target, or while the
automatic service is starting/stopping. Requests have a 15-second cooldown.
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

Enable Decky developer mode and install `egpu-gamescope.zip`. This package is
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
