import asyncio
import json
import os
from pathlib import Path
import re
import time


PCI_DEVICES = Path("/sys/bus/pci/devices")
BACKLIGHT = Path("/sys/class/backlight")
GPU_ID = ("0x1002", "0x73ff")
SERVICE = "egpu-gamescope-fix.service"
TARGET = "gamescope-session.target"
SETTINGS_FILE = "settings.json"
BACKLIGHT_POLL_SEC = 2
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
    r"\s+\[[0-9a-f]+\]:\s+(.+?)\s+\[1002:73ff\]",
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
        names["1002:73ff"] = short
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
            or names.get("1002:73ff")
        )
    return devices


def gpu_status():
    devices = []
    # An unavailable sysfs is an error, not a disconnected GPU.
    for device in PCI_DEVICES.iterdir():
        try:
            vendor = (device / "vendor").read_text().strip().lower()
            product = (device / "device").read_text().strip().lower()
            if (vendor, product) != GPU_ID:
                continue
            driver = device / "driver"
            devices.append({
                "address": device.name,
                "driver": driver.resolve().name if driver.exists() else None,
                "name": gpu_label(device),
            })
        except FileNotFoundError:
            # Hot-unplug can remove a device between reads.
            continue
    return devices


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
    return pci_ids_for(node) == GPU_ID


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
    }


def save_settings(settings):
    settings_dir().mkdir(parents=True, exist_ok=True)
    settings_path().write_text(json.dumps({
        "backlight_off": bool(settings.get("backlight_off")),
        "saved_brightness": settings.get("saved_brightness"),
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
        "show", unit, "--property=LoadState,ActiveState,SubState,Result",
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


class Plugin:
    def __init__(self):
        self._restart_lock = asyncio.Lock()
        self._cooldown_until = 0.0
        self._lspci_names = {}
        self._lspci_at = 0.0
        self._backlight_lock = asyncio.Lock()
        self._backlight_task = None
        self._settings = load_settings()

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
                    or names.get("1002:73ff")
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
        return {
            "devices": devices,
            "service": service,
            "target": target,
            "marker": Path(
                f"/run/user/{os.geteuid()}/egpu-gamescope-restarted"
            ).exists(),
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

    async def _main(self):
        if self._settings.get("backlight_off"):
            try:
                self._settings = await asyncio.to_thread(apply_backlight, True, self._settings)
            except (FileNotFoundError, OSError, PermissionError):
                pass
        self._backlight_task = asyncio.create_task(self._keep_backlight_off())

    async def _unload(self):
        if self._backlight_task:
            self._backlight_task.cancel()
            try:
                await self._backlight_task
            except asyncio.CancelledError:
                pass
            self._backlight_task = None

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
