# eGPU Helper

A standalone Decky plugin for AMD eGPUs listed in `EGPU_DEVICES` in `main.py`:
`1002:73ff` (RX 6600) and `1002:7550` (RX 9070 / 9070 XT).

- **Status** shows the GPU state (short SKU when sysfs or lspci has a name), the
  dock and its USB4 link, and whether an eGPU connector is driving a display.
  When the dock only enumerates its USB controller (no USB4 link), it says to
  press the TB5 button on the dock.
- Manually restart `gamescope-session.target` after confirmation.
- **Setup** toggles the boot-time auto-fix, auto-switch, the sleep hook, and
  separate shader caches per GPU (see below).
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

If Gamescope is rendering on the eGPU, removing the card crashes it, so the hook
first stops that user's `gamescope-session.target`. SDDM logs back in on the
built-in GPU; after wake, auto-switch (or **Restart**) moves the session back.
A game running at sleep time is closed either way.
It also stops `lactd` (LACT) before removing the card and starts it after wake;
otherwise LACT segfaults and leaves a stale `/run/lactd.sock` that blocks its restart.
Once amdgpu lets go of the card, its firmware spins the fans hard for the whole
sleep. The hook first sets a fixed fan speed, which the card keeps after removal.
It asks the card for an RPM target, a share of the card's own `fan1_max`, chosen
from the hottest sensor at that moment: 25% below 55 °C, 35% below 70 °C, 45% below
85 °C, 60% above. An RPM target works on any card; a fixed PWM does not, since fans
start at different PWMs (an RX 6600 stays still at 25%). Cards without RPM control
get a PWM a little above the same share. It never stops the fan, because nothing can restart it
until amdgpu probes the card again; amdgpu puts the fans back on auto when it probes the card after wake.

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

**Setup → Sleep hook** asks for the `deck` password and runs
`sleephook/install.py` through `sudo`. The password goes to `sudo` on stdin and
is not stored. It writes
`/etc/egpu-sleep/egpu-sleep.sh`, `/etc/systemd/system/egpu-sleep.service` and
`/etc/udev/rules.d/90-egpu-helper.rules`, enables the unit, and applies the
runtime PM setting right away. Hand-installed copies are kept as `.bak`. The
script's ID pattern comes from `EGPU_DEVICES`. Turning the toggle off disables
the unit, restores runtime PM to `auto`, and deletes the managed files. To do
the same from a terminal:

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

## Separate shader cache per GPU

Steam keeps one shader cache and remembers which GPU built it. Switching between
the eGPU and the built-in GPU makes Steam process every game's shaders again.
**Shaders → Separate shader cache per GPU** keeps one copy per GPU. It is off by
default.

Turning it on writes `~/.local/bin/egpu-shader-swap.py` and a drop-in,
`~/.config/systemd/user/gamescope-session.service.d/egpu-helper-shaders.conf`,
that runs the script before Gamescope and Steam start. Nothing changes until the
next Gamescope start.

- The script picks `egpu-<device ID>` when a supported eGPU has `amdgpu` bound,
  otherwise `igpu`. At boot, before the eGPU is up, that is `igpu`; the auto-fix
  or auto-switch restart then moves to the eGPU profile.
- Each library's `steamapps/shadercache` becomes a symlink to
  `shadercache.<profile>`. The `ShaderCacheManager` block of
  `~/.local/share/Steam/config/config.vdf` is saved per profile in
  `~/.local/share/egpu-helper/shaders/` and put back on switch. The previous
  `config.vdf` is kept as `config.vdf.egpu-bak`.
- The first time, the existing cache is filed under the GPU named in Steam's
  `CurrentBucketGPU` (matched by chip, such as `NAVI23`, from `pci.ids`), or under
  `igpu` if none matches. The other GPU starts empty and processes its shaders once.
- The script also writes `$XDG_RUNTIME_DIR/egpu-helper-environment` with
  `DRI_PRIME` set to the profile's GPU, and a drop-in,
  `~/.config/systemd/user/steam-launcher.service.d/egpu-helper-steam.conf`, makes
  Steam read it. Without it, Steam's OpenGL probe sometimes picks the other GPU,
  decides its shader bucket changed, and prunes compiled shaders.
- Each profile keeps its own copy, so expect up to about twice the disk space
  of today's `shadercache` folder.
- It never deletes a cache, skips a library whose `shadercache` is a symlink it
  did not make, and does nothing while Steam runs. Errors are logged
  (`journalctl --user -u gamescope-session -b | grep egpu-shader-swap`) and never
  stop the session.

The **Shaders** tab shows the active GPU, the size of each GPU's copy, and each
game's size per GPU (names come from `appmanifest_*.acf`). **Delete unused**
removes one game's copies for every GPU except the active one, after
confirmation. It never touches the active copy, which Steam is using.

Turning it off removes the drop-in and script. Steam keeps using the last
profile's folder through the symlink. To merge it back into a plain folder,
exit Steam in Desktop Mode and run:

```sh
python3 ~/homebrew/plugins/egpu-gamescope/shaders/egpu-shader-swap.py --restore
```

Other profiles' folders stay; delete them by hand if you want the space back.

## Build and install

From this directory:

```sh
pnpm install
pnpm test
pnpm package
```

Enable Decky developer mode and install `decky-egpu-helper.zip`. This package is
independent of the ROG Ally controller plugin.

## Removing the old Zapret2 helper

Versions before 0.5.0 bundled a Zapret2 DPI bypass helper. The plugin no longer
ships or controls it, and updating the plugin does not remove it. If you
installed it, remove it from a terminal:

```sh
sudo systemctl stop decky-zapret2.service
sudo systemctl disable decky-zapret2.service
sudo rm /etc/polkit-1/rules.d/50-decky-zapret2.rules
sudo rm /etc/systemd/system/decky-zapret2.service
sudo rm -r /opt/decky-zapret2
sudo rm -rf /var/lib/decky-zapret2
sudo systemctl daemon-reload
```

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
