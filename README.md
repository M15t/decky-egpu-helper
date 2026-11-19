# eGPU Helper

A standalone Decky plugin for AMD eGPUs listed in `EGPU_DEVICES` in `main.py`:
`1002:73ff` (RX 6600) and `1002:7550` (RX 9070 / 9070 XT).

- **Status** shows the GPU state (short SKU when sysfs or lspci has a name), the
  dock and its USB4 link, and whether an eGPU connector is driving a display.
  When the dock only enumerates its USB controller (no USB4 link), it says to
  press the TB5 button on the dock.
- Manually restart `gamescope-session.target` after confirmation.
- **Setup** toggles the boot-time auto-fix, auto-switch, and the sleep hook (see below).
- Toggle Zapret2 TCP DPI bypass after installing the dedicated helper below.
- Optionally force the internal panel backlight off. Preference is remembered.
  Gamescope may rewrite brightness, so the plugin keeps writing `0` while the
  toggle is on. It does not auto-restore when the eGPU unplugs.

The dock line loads once when the panel opens. **Refresh** (under Tools) fetches it
again; there is no automatic boltctl polling. If any listed device is
`authorized`, Refresh also runs `/usr/bin/lspci -Dnn` to probe PCI config space.
That is the same poke that makes this eGPU appear after Thunderbolt
authorization; it does not restart Gamescope and does not authorize devices.
Overlapping requests are prevented. It requires `/usr/bin/boltctl` and a working
boltd service. Missing tools, timeouts, and errors appear separately without
blocking PCI status or restart controls. An empty list means no devices were
reported, not proof that a specific eGPU is disconnected. Authorization is not
proof of GPU rendering.

The plugin runs as the Decky installation user, **without root**. Manual restart calls `systemctl --user --no-block restart
gamescope-session.target` directly, bypassing the script's marker guard without
removing its marker.

Restart can close games and restart Steam. Save first. The button is disabled
only while requesting a restart or during the reported 15-second cooldown.
GPU detection, driver readiness, and status polling errors do not disable it.
After confirmation, the backend requires an active Gamescope target and checks
that the automatic service is not starting/stopping. Failures appear in the panel.
“Restart queued” means systemd accepted the request, not that the fix succeeded.

Neither the dock line nor "Display: eGPU" proves that a game renders on the eGPU.
The script creates its marker before restarting, so a present marker is not
proof of success either. A completed oneshot service normally becomes inactive.
The PCI ID identifies a model, not whether its physical connection is external.

Internal backlight uses `/sys/class/backlight`, skipping any node on the eGPU
(`1002:73ff`). The plugin stays non-root; if brightness is not writable the
toggle disables with the error. Unload does not turn the panel back on.

## Boot-time auto-fix

The auto-fix is a user systemd unit, `egpu-gamescope-fix.service`, that runs
`~/.local/bin/egpu-gamescope-fix.sh` at login. The script waits up to 20
seconds for a supported eGPU on PCI, then restarts `gamescope-session.target`
once per session, guarded by `/run/user/$UID/egpu-gamescope-restarted`.
It runs without Decky, so a broken Decky after an OS update does not break it.

The plugin ships both files in `autofix/`. The script's ID pattern is generated
from `EGPU_DEVICES`, so adding a GPU means editing that one tuple.

- Turning **Auto-fix at boot** on writes both files, runs `daemon-reload`, and enables the
  unit. It never starts the unit, so nothing restarts until the next login.
- An existing file without the `# Managed by eGPU Helper.` line is a
  hand-installed copy. Install keeps it as `<name>.bak` (the first backup only),
  then replaces it.
- When the plugin loads and its bundled files differ from an installed managed
  copy, it rewrites them and runs `daemon-reload`. Hand-installed copies are
  never touched until you turn the toggle on.
- Turning it off disables the unit and deletes the managed files. `.bak`
  files stay. Uninstalling the plugin leaves the unit enabled, so turn it off
  first if you want it gone.

## Auto-switch to eGPU

The auto-fix only runs at login. **Setup → Auto-switch to eGPU** covers an eGPU
that comes up later, for example after pressing TB5 on the dock after a wake.
It is off by default.

The backend checks every 2 seconds whether an eGPU's `amdgpu` driver has bound.
When one arrives, it waits 3 seconds for DRM to settle, then restarts
`gamescope-session.target` once, the same way the Restart button does.

- A GPU that is already up when the plugin loads is not an arrival. The
  auto-fix handles boot, so auto-switch also stands down while the auto-fix
  unit runs and for 30 seconds after it touches its marker.
- While a game runs, it waits for the game to exit, then restarts. It detects
  games by a process whose command line contains `SteamLaunch`, which is how
  Steam starts every game through `reaper`.
- An arrival while the toggle is off is dropped, so turning it on later does
  not restart Gamescope for an old arrival.

## Sleep hook

With the eGPU attached, the Ally can hang in s2idle and never wake. The sleep
hook is a system unit, `egpu-sleep.service`, tied to `sleep.target`. Before
sleep it removes the eGPU's PCI functions; after wake it rescans PCI.

The USB4 link to the AG03 still drops during sleep (`TB_PORT_UP ... lost during
suspend`), so press the dock's TB5 button after waking, then restart Gamescope.
The dock stays stuck after the kernel's USB4 router sleep; even a reboot does
not revive it, only TB5 does. **Keep dock linked during sleep** (below) avoids that.

The same link drop happens while awake when the USB4 controller runtime-suspends,
which it does whenever no PCIe device is up behind the dock. Separately, a
runtime-suspended PCIe tunnel port can miss the eGPU's hotplug after TB5, leaving
the GPU off PCI until a rescan. A udev rule, `90-egpu-helper.rules`, runs
`egpu-sleep.sh pm on` when the `thunderbolt` driver binds. That sets
`power/control` to `on` for the controller and the PCIe ports it tunnels. This
costs some idle power.

**Setup → Sleep hook** runs `sleephook/install.py` through `pkexec`. It writes
`/etc/egpu-sleep/egpu-sleep.sh`, `/etc/systemd/system/egpu-sleep.service` and
`/etc/udev/rules.d/90-egpu-helper.rules`, enables the unit, and applies the
runtime PM setting right away. Hand-installed copies are kept as `.bak`. The
script's ID pattern comes from `EGPU_DEVICES`. Turning the toggle off disables
the unit, restores runtime PM to `auto`, and deletes the managed files. Like the Zapret2 helper, it needs a polkit agent; in
Gaming Mode without one, use Desktop Mode or run as root:

```sh
sudo /usr/bin/python3 sleephook/install.py                   # install or update
sudo /usr/bin/python3 sleephook/install.py --keep-dock on    # or off
sudo /usr/bin/python3 sleephook/install.py --remove          # remove
```

### Keep dock linked during sleep

Experimental and off by default. It appears under the sleep hook once that is
installed, and toggles `/etc/egpu-sleep/keep-dock` through the same installer.

While the marker exists and a USB4 device is attached, the hook also unbinds the
`thunderbolt` driver from the USB4 controller before sleep, and binds it again
after wake, before the PCI rescan. Shutdown takes the same path (the driver's
`.shutdown` is its remove), so the kernel never puts the dock's router to sleep,
and the dock comes back without TB5. Turn on Auto-switch to move Gamescope to
the eGPU afterwards. Auto-switch tracks the eGPU's sysfs node, so a removal and
return shorter than its 2-second poll still counts as an arrival.

- The Ally stays out of hardware sleep while docked (`amd_pmc: Last suspend
  didn't reach deepest state`). The dock powers the Ally, so this costs wall
  power, not battery. Undocked sleep is unchanged.
- The dock stays powered, so the eGPU, detached from its driver, runs its fans
  at the VBIOS default for the whole sleep. Keeping the eGPU attached instead
  (with `thunderbolt.host_reset=0` so the rebind adopts the live tunnel) hung
  the Ally at suspend entry, so the hook must still detach it.
- One test sleep crashed and rebooted the Ally at suspend entry, with no log
  left. Its cause is unknown. A game already ends on docked sleep when it runs
  on the eGPU, but anything else unsaved is lost.
- Unplugging the dock while the Ally sleeps is untested. The controller stays
  released until wake, so expect light sleep on battery until then.
- If rebinding fails after wake, the journal shows `egpu-sleep: rebinding ...
  failed` and the PCI rescan still runs. Reboot to recover the dock.

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
and kernel NFQUEUE support. The ZIP includes the prepared runtime.

In **Tools**, select **Install helper** and approve the Linux administrator
prompt. The plugin detects the Decky account automatically, uses the bundled
installer, and refreshes status when setup finishes. Setup leaves DPI bypass
OFF; turn it ON afterwards if wanted. No password is collected or stored by
the plugin, and its backend remains non-root. Installation requires your
explicit button press; it never runs automatically on plugin load.

This button needs `/usr/bin/pkexec` and a working polkit authentication agent
for the calling session. Gaming Mode or service-hosted Decky may not provide
one. If no prompt is available, the plugin reports the error; try Desktop
Mode, or use the terminal fallback below. Existing installations are never
overwritten by this button. Missing nftables must still be installed with
your distribution's supported package mechanism.

**Terminal fallback:** extract the ZIP and open a terminal in
`egpu-gamescope/zapret2`. Run, replacing
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
