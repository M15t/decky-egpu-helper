# eGPU Helper

A standalone Decky plugin for the eGPU with PCI ID `1002:73ff`.

- Show a short GPU SKU when sysfs or lspci has a name, plus PCI ready vs not on PCI.
- Show Connection first. Status is the main line; host, generation, and link
  sit under it. GPU ready lives only in the eGPU section.
- Manually restart `gamescope-session.target` after confirmation.
- Toggle Zapret2 TCP DPI bypass after installing the dedicated helper below.
- Optionally force the internal panel backlight off. Preference is remembered.
  Gamescope may rewrite brightness, so the plugin keeps writing `0` while the
  toggle is on. It does not auto-restore when the eGPU unplugs.

Connection status loads once when the panel opens. **Refresh** fetches it
again; there is no automatic boltctl polling. If any listed device is
`authorized`, Refresh also runs `/usr/bin/lspci -Dnn` to probe PCI config space.
That is the same poke that makes this eGPU appear after Thunderbolt
authorization; it does not restart Gamescope and does not authorize devices.
Overlapping requests are prevented. It requires `/usr/bin/boltctl` and a working
boltd service. Missing tools, timeouts, and errors appear separately without
blocking PCI status or restart controls. An empty list means no devices were
reported, not proof that a specific eGPU is disconnected. Authorization is not
proof of GPU rendering.

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

Internal backlight uses `/sys/class/backlight`, skipping any node on the eGPU
(`1002:73ff`). The plugin stays non-root; if brightness is not writable the
toggle disables with the error. Unload does not turn the panel back on.

## Build and install

From this directory:

```sh
pnpm install
pnpm test
curl -fL https://github.com/bol-van/zapret2/releases/download/v1.0.5.2/zapret2-v1.0.5.2.tar.gz -o /tmp/zapret2-v1.0.5.2.tar.gz
python3 zapret2/prepare.py /tmp/zapret2-v1.0.5.2.tar.gz
pnpm package
```

Enable Decky developer mode and install `decky-egpu-helper.zip`. This package is
independent of the ROG Ally controller plugin.

## Zapret2 helper (one-time Linux setup)

Requires x86_64 Linux, systemd, polkit, Python 3 at `/usr/bin/python3`, nftables,
and kernel NFQUEUE support. The ZIP includes the prepared runtime. Extract it
and open a terminal in `egpu-gamescope/zapret2`. Install nftables using your
distribution's supported package mechanism if missing. Then run, replacing
`YOUR_DECKY_USER` with the non-root account running Decky:

```sh
sudo /usr/bin/python3 install.py --user YOUR_DECKY_USER
```

Setup installs root-owned files into `/opt/decky-zapret2`, a dedicated system
unit `/etc/systemd/system/decky-zapret2.service`, and
`/etc/polkit-1/rules.d/50-decky-zapret2.rules`. It refuses to overwrite existing
files. The polkit rule permits only that user's start/stop of this exact unit;
the plugin remains non-root, with empty flags. Existing eGPU controls continue
to use the user's systemd bus. This new helper uses the system bus.

Open **Tools → DPI bypass · Zapret2**. The toggle saves your ON/OFF selection;
the description reports the actual service state. ON starts the helper now
and automatically on future reboots/power-ons, without needing to open Decky.
OFF stops it now and keeps it off after reboot. A new installation defaults
to OFF. Closing/unloading Decky leaves its current state intact; turn it OFF
before uninstalling the plugin. A saved ON with a failed service is reported
as an error, not as a running or successful bypass.

The installer enables the unit's boot hook once. Its `ConditionPathExists`
checks `/var/lib/decky-zapret2/enabled`, a presence-only preference in a directory
owned by the Decky user. The plugin creates/removes that marker before
starting/stopping the service. Shutdown cleanup leaves the marker intact.
No privileged code or configuration lives in this user-writable directory;
the existing narrow start/stop polkit permissions are unchanged. If start/stop
fails, the saved selection still applies at the next boot and the UI reports
the runtime failure.

**Upgrading from 0.2.0:** updating only the plugin ZIP is insufficient. Remove
the old helper using the commands below, then run the new `install.py` once
and select ON again. The installer continues to refuse overwriting existing
installations. The plugin reports when the old helper needs updating.

The initial preset splits HTTP requests and TLS ClientHello on outbound TCP
ports 80/443, for IPv4 and IPv6, excluding loopback and common private/local
destinations. It does not process QUIC (UDP/443), change DNS, install certificates,
or provide VPN privacy. Compatibility depends on the ISP; existing connections
may need reopening. All matching TCP packets pass through NFQUEUE while ON,
so measure throughput and CPU on the device before everyday use.

Only the nftables table `inet decky_zapret2` is managed; OFF and service failure
run cleanup. Queue 28745 uses `bypass` so packets pass normally if no listener
exists. Startup refuses an occupied queue or a foreign table with that name.
Do not share queue 28745 or mark bit `0x40000000` with another networking tool.

Runtime comes from [official Zapret2 v1.0.5.2](https://github.com/bol-van/zapret2/releases/tag/v1.0.5.2),
source commit `6b6c63e3385fa73f8af3be4a69171e947f5a319d`. Preparation checks the
archive SHA-256 and extracts only four regular files. Packaging and installation
verify each runtime file again; pinned values are in `zapret2/prepare.py`.
No downloads, upstream installer, or auto-update runs on toggle. The engine
drops to `nobody` while retaining network capabilities. Lua/runtime files remain
root-owned. Hashes pin bytes; they do not constitute a security audit or prove
the upstream binary reproduces from source. Its MIT license ships alongside it.

Diagnostics (on the Linux device):

```sh
systemctl status decky-zapret2.service
journalctl -u decky-zapret2.service -b
sudo nft list table inet decky_zapret2
```

Validate ON → reboot/power-on → running and OFF → reboot → stopped, failed starts, normal LAN access, TCP download
speed and the target blocked sites on the device. Local development tests mock
systemd/nftables; Linux service behavior and ISP effectiveness are not verified
on macOS. SteamOS/Bazzite installation and OS-update compatibility still need
device testing.

To remove or replace this helper, stop it first and ensure its nftables table
is gone, then remove only these installation paths:

```sh
sudo systemctl stop decky-zapret2.service
sudo systemctl disable decky-zapret2.service
sudo rm /etc/polkit-1/rules.d/50-decky-zapret2.rules
sudo rm /etc/systemd/system/decky-zapret2.service
sudo rm -r /opt/decky-zapret2
sudo rm -rf /var/lib/decky-zapret2
sudo systemctl daemon-reload
```

If stopping fails, inspect the journal and resolve cleanup before removal.
Updating the Decky ZIP alone does not replace the installed privileged helper.

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
