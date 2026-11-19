import asyncio
import json
import os
from pathlib import Path
import platform
import pwd
import re
import time


PCI_DEVICES = Path("/sys/bus/pci/devices")
BACKLIGHT = Path("/sys/class/backlight")
AMD_VENDOR = "1002"
# RX 6600 (Navi 23) and RX 9070 / 9070 XT (Navi 48).
EGPU_DEVICES = ("73ff", "7550")
SERVICE = "egpu-gamescope-fix.service"
TARGET = "gamescope-session.target"
SETTINGS_FILE = "settings.json"
BACKLIGHT_POLL_SEC = 2
AUTO_SWITCH_POLL_SEC = 2
# Matches the auto-fix script's wait for amdgpu/DRM to finish after the driver binds.
AUTO_SWITCH_SETTLE_SEC = 3
# The auto-fix touches its marker just before restarting; an arrival this soon after is its boot restart.
AUTOFIX_GRACE_SEC = 30
RADEON_BRACKET = re.compile(r"\[([^\]]*Radeon[^\]]*)\]", re.IGNORECASE)


def gpu_label(device):
    for path in (
        device / "drm",
        Path("/sys/class/drm"),
    ):
        if not path.exists():
            continue
        for node in path.iterdir():
            if not node.name.startswith("card") or "-" in node.name:
                continue
            for name in ("device/label", "label"):
                try:
                    label = (node / name).read_text().strip()
                except FileNotFoundError:
                    continue
                if label:
                    return short_gpu_name(label)
    for name in ("label", "model", "product"):
        try:
            label = (device / name).read_text().strip()
        except FileNotFoundError:
            continue
        if label:
            return short_gpu_name(label)
    return None


LSPCI_GPU = re.compile(
    r"^(?:([0-9a-f:.]+)\s+)?(?:VGA compatible controller|3D controller|Display controller)"
    rf"\s+\[[0-9a-f]+\]:\s+(.+?)\s+\[{AMD_VENDOR}:(?:{'|'.join(EGPU_DEVICES)})\]",
    re.IGNORECASE | re.MULTILINE,
)


def parse_lspci_names(output):
    names = {}
    for match in LSPCI_GPU.finditer(output or ""):
        address, name = match.group(1), match.group(2).strip()
        if not name:
            continue
        short = short_gpu_name(name)
        if address:
            names[address] = short
            if ":" in address:
                names[address.split(":", 1)[1]] = short
        names["egpu"] = short
    return names


def short_gpu_name(name):
    if not name:
        return None
    text = " ".join(name.split())
    bracket = RADEON_BRACKET.search(text)
    haystack = bracket.group(1) if bracket else text
    haystack = re.sub(r"^(?:AMD(?:/ATI)?\s+)?Radeon\s+", "", haystack, flags=re.IGNORECASE)
    parts = [part.strip() for part in haystack.split("/") if part.strip()]
    if not parts:
        return text
    prefix, candidates = "RX", []
    for part in parts:
        match = re.match(r"(RX)\s+(.*)$", part, re.IGNORECASE)
        if match:
            prefix, part = match.group(1).upper(), match.group(2)
        candidates.append(part)
    chosen = next((part for part in candidates if re.search(r"\bXT\b", part, re.IGNORECASE)), candidates[-1])
    if not re.match(r"RX\b", chosen, re.IGNORECASE):
        chosen = f"{prefix} {chosen}"
    return " ".join(chosen.split())


def apply_lspci_names(devices, output):
    names = parse_lspci_names(output)
    for device in devices:
        if device.get("name"):
            continue
        address = device.get("address") or ""
        device["name"] = (
            names.get(address)
            or names.get(address.split(":", 1)[-1])
            or names.get("egpu")
        )
    return devices


def gpu_status():
    devices = []
    # An unavailable sysfs is an error, not a disconnected GPU.
    for device in PCI_DEVICES.iterdir():
        try:
            vendor = (device / "vendor").read_text().strip().lower()
            product = (device / "device").read_text().strip().lower()
            if not is_egpu_id(vendor, product):
                continue
            driver = device / "driver"
            devices.append({
                "address": device.name,
                # Removing and adding the device back, as the sleep hook does,
                # gives it a new sysfs node.
                "node": device.stat().st_ino,
                "driver": driver.resolve().name if driver.exists() else None,
                "name": gpu_label(device),
            })
        except FileNotFoundError:
            # Hot-unplug can remove a device between reads.
            continue
    return devices


def drives_display(address):
    # An enabled connector shows scanout on this GPU, not where games render.
    for card in (PCI_DEVICES / address / "drm").glob("card*"):
        for connector in card.glob(f"{card.name}-*"):
            try:
                if (connector / "enabled").read_text().strip() == "enabled":
                    return True
            except OSError:
                continue
    return False


def autofix_marker():
    return Path(f"/run/user/{os.geteuid()}/egpu-gamescope-restarted")


def autofix_restarted_recently():
    try:
        return time.time() - autofix_marker().stat().st_mtime < AUTOFIX_GRACE_SEC
    except FileNotFoundError:
        return False


def game_running(proc=Path("/proc")):
    # Steam starts every game, non-Steam shortcuts included, through "reaper SteamLaunch".
    for cmdline in proc.glob("[0-9]*/cmdline"):
        try:
            if b"SteamLaunch" in cmdline.read_bytes():
                return True
        except OSError:
            continue
    return False


USB_DEVICES = Path("/sys/bus/usb/devices")


def usb_dock_present():
    # Without a USB4 link, a TB5 dock still enumerates its USB2 hub controller.
    for product in USB_DEVICES.glob("*/product"):
        try:
            if "TBT" in product.read_text():
                return True
        except OSError:
            continue
    return False


def is_egpu_id(vendor, product):
    return vendor == f"0x{AMD_VENDOR}" and product.removeprefix("0x") in EGPU_DEVICES


def sys_int(path):
    try:
        return int(Path(path).read_text().strip())
    except (FileNotFoundError, OSError, ValueError):
        return None


def pci_ids_for(path):
    current = Path(path).resolve()
    for parent in (current, *current.parents):
        vendor = parent / "vendor"
        device = parent / "device"
        if vendor.exists() and device.exists():
            try:
                return (
                    vendor.read_text().strip().lower(),
                    device.read_text().strip().lower(),
                )
            except (FileNotFoundError, OSError):
                return None
    return None


def is_egpu_backlight(node):
    ids = pci_ids_for(node)
    return ids is not None and is_egpu_id(*ids)


def internal_backlight_node():
    if not BACKLIGHT.exists():
        return None
    nodes = sorted(path for path in BACKLIGHT.iterdir() if path.is_dir())
    internal = [node for node in nodes if not is_egpu_backlight(node)]
    preferred = [node for node in internal if node.name.startswith("amdgpu_bl")]
    return (preferred or internal or [None])[0]


def read_backlight(node):
    brightness = sys_int(node / "brightness")
    maximum = sys_int(node / "max_brightness")
    actual = sys_int(node / "actual_brightness")
    if brightness is None and actual is None:
        raise FileNotFoundError(str(node / "brightness"))
    return {
        "node": node.name,
        "path": str(node),
        "brightness": actual if actual is not None else brightness,
        "requested": brightness,
        "max": maximum,
    }


def write_backlight(node, value):
    (node / "brightness").write_text(str(int(value)))


def default_restore(maximum):
    if not maximum:
        return 1
    return max(1, int(maximum * 0.4))


def settings_dir():
    return Path(os.environ.get("DECKY_PLUGIN_SETTINGS_DIR") or ".")


def settings_path():
    return settings_dir() / SETTINGS_FILE


def load_settings():
    try:
        data = json.loads(settings_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    saved = data.get("saved_brightness")
    try:
        saved = int(saved) if saved is not None else None
    except (TypeError, ValueError):
        saved = None
    return {
        "backlight_off": bool(data.get("backlight_off", False)),
        "saved_brightness": saved if saved and saved > 0 else None,
        "auto_switch": bool(data.get("auto_switch", False)),
    }


def save_settings(settings):
    settings_dir().mkdir(parents=True, exist_ok=True)
    settings_path().write_text(json.dumps({
        "backlight_off": bool(settings.get("backlight_off")),
        "saved_brightness": settings.get("saved_brightness"),
        "auto_switch": bool(settings.get("auto_switch")),
    }))


def backlight_status(settings=None):
    settings = settings if settings is not None else load_settings()
    node = internal_backlight_node()
    if node is None:
        return {
            "available": False,
            "off": bool(settings.get("backlight_off")),
            "brightness": None,
            "max": None,
            "saved": settings.get("saved_brightness"),
            "node": None,
            "error": "No internal backlight node",
        }
    try:
        current = read_backlight(node)
    except (FileNotFoundError, OSError, ValueError) as failure:
        return {
            "available": False,
            "off": bool(settings.get("backlight_off")),
            "brightness": None,
            "max": None,
            "saved": settings.get("saved_brightness"),
            "node": node.name,
            "error": str(failure) or "Could not read backlight",
        }
    return {
        "available": True,
        "off": bool(settings.get("backlight_off")),
        "brightness": current["brightness"],
        "max": current["max"],
        "saved": settings.get("saved_brightness"),
        "node": current["node"],
        "error": None,
    }


def apply_backlight(off, settings=None):
    settings = dict(settings if settings is not None else load_settings())
    node = internal_backlight_node()
    if node is None:
        raise FileNotFoundError("No internal backlight node")
    current = read_backlight(node)
    if off:
        level = current["requested"]
        if level is None:
            level = current["brightness"]
        if level and level > 0:
            settings["saved_brightness"] = level
        write_backlight(node, 0)
        settings["backlight_off"] = True
    else:
        restore = settings.get("saved_brightness") or default_restore(current["max"])
        if current["max"] is not None:
            restore = min(restore, current["max"])
        write_backlight(node, max(1, restore))
        settings["backlight_off"] = False
    save_settings(settings)
    return settings


async def systemctl(*args):
    if os.geteuid() == 0:
        raise RuntimeError("This plugin must run without the root flag.")
    env = os.environ.copy()
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("LD_PRELOAD", None)
    runtime = f"/run/user/{os.geteuid()}"
    env["XDG_RUNTIME_DIR"] = runtime
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/systemctl", "--user", *args,
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=8)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode:
        raise RuntimeError(stderr.decode(errors="replace").strip() or "systemctl failed")
    return stdout.decode(errors="replace")


async def unit_status(unit):
    output = await systemctl(
        "show", unit, "--property=LoadState,ActiveState,SubState,Result,UnitFileState",
    )
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


FIELD = re.compile(
    r"(?<![A-Za-z-])(type|name|vendor|uuid|status|rx speed|tx speed|generation|power|powering):\s+(\S.*?)\s*$",
    re.IGNORECASE,
)


def compact_speed(value):
    if not value:
        return None
    match = re.search(r"(\d+(?:\.\d+)?\s*Gb/s)", value, re.IGNORECASE)
    return match.group(1) if match else None


def link_speed(device):
    rx, tx = compact_speed(device.get("rx speed")), compact_speed(device.get("tx speed"))
    if rx and tx and rx != tx:
        return f"{rx} RX · {tx} TX"
    return rx or tx


def output_preview(output):
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return " | ".join(lines)[:180]


def parse_bolt_devices(output):
    devices = []
    current = {}
    # boltctl uses tree prefixes and may emit terminal color sequences.
    # LC_ALL=C can replace box-drawing characters with "?".
    # Plugin pipes can add garbage before the key; match the key anywhere on the line.
    output = re.sub(r"\x1b\[[0-9;]*m", "", output)
    for raw in output.splitlines():
        line = raw.replace("\r", "").strip()
        field = FIELD.search(line)
        if not field:
            continue
        key, value = field.group(1).lower(), field.group(2).strip()
        if key == "type" and current:
            devices.append(current)
            current = {}
        current[key] = value
    if current:
        devices.append(current)
    if output.strip() and not devices:
        raise ValueError(f"Unrecognized boltctl output: {output_preview(output)}")
    result = []
    for device in devices:
        if device.get("type") == "host":
            continue
        if not device.get("uuid") or not device.get("status"):
            raise ValueError(f"Incomplete boltctl device status: {output_preview(output)}")
        result.append({
            "id": device["uuid"],
            "name": " ".join(filter(None, (device.get("vendor"), device.get("name"))))
                    or "Thunderbolt device",
            "status": device["status"].lower(),
            "generation": device.get("generation") or None,
            "link": link_speed(device),
            "power": device.get("power") or device.get("powering"),
        })
    return result


async def bolt_status():
    try:
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/boltctl", "list",
            env=clean_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        if process.returncode:
            raise RuntimeError(
                stderr.decode(errors="replace").strip() or "boltctl failed."
            )
        return {
            "available": True,
            "devices": parse_bolt_devices(stdout.decode(errors="replace")),
            "error": None,
        }
    except FileNotFoundError:
        error = "boltctl is not installed."
    except asyncio.TimeoutError:
        error = "boltctl timed out. Check the boltd service."
    except (OSError, RuntimeError, ValueError) as failure:
        error = str(failure)
    return {"available": False, "devices": [], "error": error}


def clean_env():
    env = os.environ.copy()
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("LD_PRELOAD", None)
    env.update({"LC_ALL": "C.UTF-8", "TERM": "dumb", "NO_COLOR": "1"})
    return env


async def pci_scan():
    # Authorized Thunderbolt is not PCI enumeration. lspci reads config space
    # and is the same poke that makes the eGPU appear on this hardware.
    try:
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/lspci", "-Dnn",
            env=clean_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        if process.returncode:
            raise RuntimeError(stderr.decode(errors="replace").strip() or "lspci failed.")
        return {"ran": True, "error": None, "output": stdout.decode(errors="replace")}
    except FileNotFoundError:
        error = "lspci is not installed."
    except asyncio.TimeoutError:
        error = "lspci timed out."
    except (OSError, RuntimeError) as failure:
        error = str(failure)
    return {"ran": False, "error": error, "output": ""}


AUTOFIX_DIR = Path(__file__).resolve().parent / "autofix"
MANAGED = "# Managed by eGPU Helper."


def autofix_files(home):
    script = (AUTOFIX_DIR / "egpu-gamescope-fix.sh").read_text().replace(
        "@EGPU_ID@", f"{AMD_VENDOR}:({'|'.join(EGPU_DEVICES)})",
    )
    return [
        (home / ".local/bin/egpu-gamescope-fix.sh", script, 0o755),
        (home / ".config/systemd/user" / SERVICE, (AUTOFIX_DIR / SERVICE).read_text(), 0o644),
    ]


def managed_state(files):
    texts = [path.read_text() if path.is_file() else None for path, _, _ in files]
    if any(text is not None and MANAGED not in text for text in texts):
        return "foreign"
    if None in texts:
        return "missing"
    if all(text == content for text, (_, content, _) in zip(texts, files)):
        return "current"
    return "outdated"


def write_managed(files):
    for path, content, mode in files:
        if path.is_file():
            current = path.read_text()
            if current == content:
                continue
            backup = path.with_name(path.name + ".bak")
            # Keep the first backup: it is the hand-installed original.
            if MANAGED not in current and not backup.exists():
                backup.write_text(current)
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(path.name + ".tmp")
        staged.write_text(content)
        staged.chmod(mode)
        os.replace(staged, path)


def remove_managed(files):
    for path, _, _ in files:
        if path.is_file() and MANAGED in path.read_text():
            path.unlink()


SLEEP_HOOK_DIR = Path(__file__).resolve().parent / "sleephook"
SLEEP_SERVICE = "egpu-sleep.service"
SLEEP_RULE = "90-egpu-helper.rules"
# Lives next to the installed sleep hook script while Keep dock linked is on.
KEEP_DOCK_NAME = "keep-dock"


def sleep_hook_files(root=Path("/")):
    script = (SLEEP_HOOK_DIR / "egpu-sleep.sh").read_text().replace(
        "@EGPU_ID@", f"^0x{AMD_VENDOR}:0x({'|'.join(EGPU_DEVICES)})$",
    )
    return [
        (root / "etc/egpu-sleep/egpu-sleep.sh", script, 0o755),
        (root / "etc/systemd/system" / SLEEP_SERVICE, (SLEEP_HOOK_DIR / SLEEP_SERVICE).read_text(), 0o644),
        (root / "etc/udev/rules.d" / SLEEP_RULE, (SLEEP_HOOK_DIR / SLEEP_RULE).read_text(), 0o644),
    ]


def sleep_hook_state():
    files = sleep_hook_files()
    return managed_state(files), files[0][0].with_name(KEEP_DOCK_NAME).exists()


async def system_unit_enabled(unit):
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/systemctl", "--system", "show", unit, "--property=UnitFileState",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    stdout, _ = await asyncio.wait_for(process.communicate(), timeout=8)
    return stdout.decode(errors="replace").strip() == "UnitFileState=enabled"


async def pkexec_python(script, *args):
    process = await asyncio.create_subprocess_exec(
        str(PKEXEC), "--disable-internal-agent", "/usr/bin/python3", "-E", "-s", str(script), *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, cwd="/",
    )
    # Never kill an authorized root process mid-write.
    _, stderr = await process.communicate()
    if process.returncode == 126:
        raise RuntimeError("Cancelled. You can try again.")
    if process.returncode == 127:
        raise RuntimeError("Administrator authorization failed or no prompt is available. Try Desktop Mode or the README terminal setup.")
    if process.returncode:
        raise RuntimeError(stderr.decode(errors="replace").strip()[-2000:] or "Sleep hook setup failed.")


DPI_SERVICE = "decky-zapret2.service"
DPI_ENABLED_FILE = Path("/var/lib/decky-zapret2/enabled")
DPI_INSTALLER = Path(__file__).resolve().parent / "zapret2/install.py"
PKEXEC = Path("/usr/bin/pkexec")


def dpi_install_support():
    if os.geteuid() == 0:
        return "Decky must run as its non-root user."
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        return "Helper installation requires x86_64 Linux."
    if not PKEXEC.is_file():
        return "Linux administrator authentication is unavailable. Use the README terminal setup."
    if not DPI_INSTALLER.is_file():
        return "Installer is missing. Reinstall the plugin ZIP."
    return None


async def dpi_systemctl(operation):
    if operation not in ("show", "start", "stop"):
        raise ValueError("Unsupported DPI operation")
    if os.geteuid() == 0:
        raise RuntimeError("Decky plugin must run as the Decky user")
    args = ["/usr/bin/systemctl", "--system", "--no-ask-password", operation, DPI_SERVICE]
    if operation == "show":
        args.append("--property=LoadState,ActiveState,SubState,Result,MainPID")
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=25)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.communicate()
        raise
    output = stdout.decode(errors="replace")
    if process.returncode and not (operation == "show" and "LoadState=not-found" in output.splitlines()):
        raise RuntimeError(stderr.decode(errors="replace").strip() or "DPI service command failed")
    return output


async def dpi_status():
    try:
        fields = dict(line.split("=", 1) for line in (await dpi_systemctl("show")).splitlines() if "=" in line)
        available = fields.get("LoadState") == "loaded"
        state = fields.get("ActiveState", "unknown")
        enabled = available and state == "active" and fields.get("SubState") == "running" and int(fields.get("MainPID", "0")) > 0
        error = None if available else "Install the Zapret2 helper first (see README)."
        if state == "failed":
            error = "Zapret2 failed: " + fields.get("Result", "unknown")
        requested = DPI_ENABLED_FILE.exists()
        if not DPI_ENABLED_FILE.parent.is_dir():
            available = False
            error = "Install or update the Zapret2 helper to save its ON/OFF setting."
        busy = state in ("activating", "deactivating", "reloading")
        if available and not busy and requested != enabled and not error:
            error = "Saved ON, but service is not running." if requested else "Saved OFF, but service is still running."
        return {"available": available, "installed": fields.get("LoadState") == "loaded", "enabled": enabled, "requested": requested, "busy": busy, "state": state, "error": error}
    except Exception as error:
        return {"available": False, "enabled": False, "requested": None, "busy": False, "state": "unknown", "error": str(error) or type(error).__name__}


class Plugin:
    def __init__(self):
        self._dpi_lock = asyncio.Lock()
        self._dpi_install_task = None
        self._dpi_install_error = None
        self._restart_lock = asyncio.Lock()
        self._autofix_lock = asyncio.Lock()
        self._sleep_task = None
        self._sleep_error = None
        self._home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
        self._cooldown_until = 0.0
        self._lspci_names = {}
        self._lspci_at = 0.0
        self._backlight_lock = asyncio.Lock()
        self._backlight_task = None
        self._settings = load_settings()
        # None until the first poll, so a GPU already up when the plugin loads is no arrival.
        self._egpu_ready = None
        self._switch_due = None
        self._switch_task = None

    async def get_dpi_status(self):
        async with self._dpi_lock:
            return await self._dpi_status_with_setup()

    async def _dpi_status_with_setup(self):
        status = await dpi_status()
        installing = self._dpi_install_task is not None and not self._dpi_install_task.done()
        if status["available"] and not installing:
            self._dpi_install_error = None
        support_error = dpi_install_support()
        status.update({
            "installing": installing,
            "can_install": not status.get("installed", True) and not support_error and not installing,
            "install_error": self._dpi_install_error,
            "install_hint": support_error,
        })
        return status

    async def install_dpi_helper(self):
        # No paths, commands, usernames or passwords accepted from the frontend.
        async with self._dpi_lock:
            status = await self._dpi_status_with_setup()
            if not status["can_install"]:
                return status
            self._dpi_install_error = None
            user = pwd.getpwuid(os.geteuid()).pw_name
            self._dpi_install_task = asyncio.create_task(self._install_dpi_helper(user))
            status.update({"installing": True, "can_install": False, "install_error": None})
            return status

    async def _install_dpi_helper(self, user):
        try:
            process = await asyncio.create_subprocess_exec(
                str(PKEXEC), "--disable-internal-agent", "/usr/bin/python3", "-E", "-s",
                str(DPI_INSTALLER), "--user", user,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, cwd="/",
            )
            # Do not block Decky's RPC or status polling during authentication.
            # Once authorized, the root installer may outlive this plugin; never
            # kill it mid-install or claim that a timeout cancelled installation.
            communication = asyncio.create_task(process.communicate())
            try:
                _, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout=120)
            except asyncio.TimeoutError:
                self._dpi_install_error = "Still waiting for administrator approval or setup. Check the Linux authentication dialog; do not start another installation."
                _, stderr = await communication
            if process.returncode == 126:
                raise RuntimeError("Installation cancelled. You can try again.")
            if process.returncode == 127:
                raise RuntimeError("Administrator authorization failed or no authentication dialog is available. Try from Desktop Mode; if no prompt appears, use the README terminal setup.")
            if process.returncode:
                raise RuntimeError(stderr.decode(errors="replace").strip()[-2000:] or "Helper installation failed. See README setup requirements.")
            status = await dpi_status()
            if not status["available"]:
                raise RuntimeError(status["error"] or "Setup finished but the helper is unavailable.")
            self._dpi_install_error = None
        except Exception as error:
            self._dpi_install_error = str(error) or type(error).__name__

    async def set_dpi_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        async with self._dpi_lock:
            if self._dpi_install_task is not None and not self._dpi_install_task.done():
                return await self._dpi_status_with_setup()
            try:
                # Save intent before changing runtime state. Shutdown stops the
                # service without changing this marker, so boot can restore ON.
                if not DPI_ENABLED_FILE.parent.is_dir():
                    raise RuntimeError("Install or update the Zapret2 helper first.")
                if enabled:
                    # Never follow a symlink or truncate an existing file.
                    try:
                        with DPI_ENABLED_FILE.open("x"):
                            pass
                    except FileExistsError:
                        if not DPI_ENABLED_FILE.is_file() or DPI_ENABLED_FILE.is_symlink():
                            raise RuntimeError("Invalid Zapret2 preference file")
                else:
                    DPI_ENABLED_FILE.unlink(missing_ok=True)
                await dpi_systemctl("start" if enabled else "stop")
            except Exception as error:
                status = await self._dpi_status_with_setup()
                status["error"] = str(error) or type(error).__name__
                return status
            status = await self._dpi_status_with_setup()
            if status["enabled"] != enabled and not status["error"]:
                status["error"] = "Service did not reach the requested state; check its journal."
            return status

    async def lspci_names(self):
        if self._lspci_names and time.monotonic() - self._lspci_at < 5:
            return self._lspci_names
        scan = await pci_scan()
        names = parse_lspci_names(scan.get("output") or "")
        if names or scan.get("ran"):
            self._lspci_names = names
            self._lspci_at = time.monotonic()
        return names

    async def named_gpus(self):
        devices = await asyncio.to_thread(gpu_status)
        if any(not device.get("name") for device in devices):
            names = await self.lspci_names()
            for device in devices:
                if device.get("name"):
                    continue
                address = device.get("address") or ""
                device["name"] = (
                    names.get(address)
                    or names.get(address.split(":", 1)[-1])
                    or names.get("egpu")
                )
        return devices

    async def get_bolt_status(self):
        result = await bolt_status()
        if result["available"] and any(device["status"] == "authorized" for device in result["devices"]):
            scan = await pci_scan()
            result["pci_scan"] = scan
            names = parse_lspci_names(scan.get("output") or "")
            if names or scan.get("ran"):
                self._lspci_names = names
                self._lspci_at = time.monotonic()
        else:
            result["pci_scan"] = None
        authorized = any(device["status"] == "authorized" for device in result["devices"])
        result["usb_fallback"] = not authorized and await asyncio.to_thread(usb_dock_present)
        try:
            gpus = await self.named_gpus()
            result["gpu_on_pci"] = bool(gpus)
            result["gpu_ready"] = any(device.get("driver") == "amdgpu" for device in gpus)
        except FileNotFoundError:
            # Missing sysfs is not "GPU unplugged". Leave PCI unknown.
            result["gpu_on_pci"] = None
            result["gpu_ready"] = None
        return result

    async def get_status(self):
        devices = await self.named_gpus()
        service, target = await asyncio.gather(unit_status(SERVICE), unit_status(TARGET))
        busy = self._restart_lock.locked() or time.monotonic() < self._cooldown_until
        ready = (
            target.get("ActiveState") == "active"
            and service.get("ActiveState") not in ("activating", "deactivating")
            and not busy
        )
        display = await asyncio.to_thread(
            lambda: any(drives_display(device["address"]) for device in devices),
        )
        return {
            "devices": devices,
            "egpu_display": display,
            "service": service,
            "target": target,
            "marker": autofix_marker().exists(),
            "busy": busy,
            "can_restart": ready,
        }

    async def restart_gamescope(self):
        if self._restart_lock.locked() or time.monotonic() < self._cooldown_until:
            raise RuntimeError("A restart was already requested. Please wait.")
        async with self._restart_lock:
            # Restart is also useful when PCI detection or the GPU driver is not ready.
            # Check the session after confirmation instead of trusting UI polling.
            service, target = await asyncio.gather(unit_status(SERVICE), unit_status(TARGET))
            if service.get("ActiveState") in ("activating", "deactivating"):
                raise RuntimeError("The automatic fix service is busy. Please wait.")
            if target.get("ActiveState") != "active":
                raise RuntimeError("No active Gamescope session to restart.")
            # Queue the job: restarting Steam can disconnect the frontend immediately.
            await systemctl("--no-block", "restart", TARGET)
            self._cooldown_until = time.monotonic() + 15
            return {"message": "Restart queued. Steam may disconnect while Gamescope restarts."}

    async def _autofix_status(self):
        state = await asyncio.to_thread(lambda: managed_state(autofix_files(self._home)))
        unit = await unit_status(SERVICE)
        return {"state": state, "enabled": unit.get("UnitFileState") == "enabled"}

    async def get_autofix_status(self):
        async with self._autofix_lock:
            return await self._autofix_status()

    async def install_autofix(self):
        async with self._autofix_lock:
            await asyncio.to_thread(lambda: write_managed(autofix_files(self._home)))
            await systemctl("daemon-reload")
            # Enable only. Starting the unit would restart Gamescope now.
            await systemctl("enable", SERVICE)
            return await self._autofix_status()

    async def remove_autofix(self):
        async with self._autofix_lock:
            files = await asyncio.to_thread(autofix_files, self._home)
            if await asyncio.to_thread(managed_state, files) == "foreign":
                raise RuntimeError("The installed auto-fix was not made by this plugin. Install it first.")
            if (await unit_status(SERVICE)).get("LoadState") == "loaded":
                await systemctl("disable", SERVICE)
            await asyncio.to_thread(remove_managed, files)
            await systemctl("daemon-reload")
            return await self._autofix_status()

    async def get_sleep_hook_status(self):
        error = self._sleep_error
        try:
            state, keep_dock = await asyncio.to_thread(sleep_hook_state)
            enabled = await system_unit_enabled(SLEEP_SERVICE)
        except (OSError, RuntimeError, asyncio.TimeoutError) as failure:
            state, keep_dock, enabled, error = None, False, False, str(failure) or type(failure).__name__
        busy = self._sleep_task is not None and not self._sleep_task.done()
        return {
            "state": state,
            "enabled": enabled,
            "keep_dock": keep_dock,
            "busy": busy,
            "error": error,
            "can_change": PKEXEC.is_file() and not busy,
        }

    async def set_sleep_hook(self, installed):
        if type(installed) is not bool:
            raise ValueError("installed must be a boolean")
        status = await self.get_sleep_hook_status()
        if not status["can_change"]:
            return status
        return self._start_sleep_installer(status, () if installed else ("--remove",))

    async def set_keep_dock(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        status = await self.get_sleep_hook_status()
        if not status["can_change"] or status["state"] != "current":
            return status
        return self._start_sleep_installer(status, ("--keep-dock", "on" if enabled else "off"))

    def _start_sleep_installer(self, status, args):
        self._sleep_error = None
        self._sleep_task = asyncio.create_task(self._run_sleep_installer(args))
        return {**status, "busy": True, "can_change": False, "error": None}

    async def _run_sleep_installer(self, args):
        try:
            await pkexec_python(SLEEP_HOOK_DIR / "install.py", *args)
        except Exception as error:
            self._sleep_error = str(error) or type(error).__name__

    async def _main(self):
        if self._settings.get("backlight_off"):
            try:
                self._settings = await asyncio.to_thread(apply_backlight, True, self._settings)
            except (FileNotFoundError, OSError, PermissionError):
                pass
        self._backlight_task = asyncio.create_task(self._keep_backlight_off())
        self._switch_task = asyncio.create_task(self._auto_switch_loop())
        # Keep an installed auto-fix in step with the plugin's GPU list.
        try:
            async with self._autofix_lock:
                files = await asyncio.to_thread(autofix_files, self._home)
                if await asyncio.to_thread(managed_state, files) == "outdated":
                    await asyncio.to_thread(write_managed, files)
                    await systemctl("daemon-reload")
        except Exception:
            pass

    async def _unload(self):
        for task in (self._backlight_task, self._switch_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._backlight_task = self._switch_task = None

    async def get_auto_switch(self):
        enabled = bool(self._settings.get("auto_switch"))
        return {"enabled": enabled, "waiting": enabled and self._switch_due is not None}

    async def set_auto_switch(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        # The backlight lock also serializes every settings write.
        async with self._backlight_lock:
            settings = {**self._settings, "auto_switch": enabled}
            await asyncio.to_thread(save_settings, settings)
            self._settings = settings
        return await self.get_auto_switch()

    async def _auto_switch_loop(self):
        while True:
            await asyncio.sleep(AUTO_SWITCH_POLL_SEC)
            try:
                await self._auto_switch_tick()
            except (OSError, RuntimeError, asyncio.TimeoutError):
                continue

    async def _auto_switch_tick(self):
        # Track sysfs nodes, not presence: across sleep the eGPU can be gone for
        # less than one poll.
        ready = {device["node"] for device in await asyncio.to_thread(gpu_status) if device["driver"] == "amdgpu"}
        if not ready:
            self._switch_due = None
        elif self._egpu_ready is not None and not ready <= self._egpu_ready:
            self._switch_due = time.monotonic() + AUTO_SWITCH_SETTLE_SEC
        self._egpu_ready = ready
        if self._switch_due is None or time.monotonic() < self._switch_due:
            return
        if not self._settings.get("auto_switch"):
            self._switch_due = None
            return
        if await asyncio.to_thread(game_running):
            return
        self._switch_due = None
        autofix = await unit_status(SERVICE)
        if autofix.get("ActiveState") in ("activating", "deactivating") or autofix_restarted_recently():
            return  # The boot-time auto-fix is handling this arrival.
        try:
            await self.restart_gamescope()
        except RuntimeError:
            pass  # Already restarting, or no Gamescope session to move.

    async def _keep_backlight_off(self):
        while True:
            await asyncio.sleep(BACKLIGHT_POLL_SEC)
            if not self._settings.get("backlight_off"):
                continue
            async with self._backlight_lock:
                if not self._settings.get("backlight_off"):
                    continue
                try:
                    node = internal_backlight_node()
                    if node is None:
                        continue
                    current = read_backlight(node)
                    if (current["requested"] or 0) > 0:
                        write_backlight(node, 0)
                except (FileNotFoundError, OSError, PermissionError, ValueError):
                    continue

    async def get_backlight_status(self):
        return await asyncio.to_thread(backlight_status, self._settings)

    async def set_backlight_off(self, enabled: bool):
        async with self._backlight_lock:
            try:
                self._settings = await asyncio.to_thread(
                    apply_backlight, bool(enabled), self._settings,
                )
            except PermissionError as failure:
                status = backlight_status(self._settings)
                status["available"] = False
                status["error"] = str(failure) or "Permission denied writing backlight"
                return status
            except (FileNotFoundError, OSError, ValueError) as failure:
                status = backlight_status(self._settings)
                status["available"] = False
                status["error"] = str(failure) or "Could not set backlight"
                return status
            return backlight_status(self._settings)
