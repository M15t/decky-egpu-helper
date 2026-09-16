import asyncio
import os
from pathlib import Path
import re
import time


PCI_DEVICES = Path("/sys/bus/pci/devices")
GPU_ID = ("0x1002", "0x73ff")
SERVICE = "egpu-gamescope-fix.service"
TARGET = "gamescope-session.target"


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
            })
        except FileNotFoundError:
            # Hot-unplug can remove a device between reads.
            continue
    return devices


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


def parse_bolt_devices(output):
    devices = []
    current = {}
    # boltctl uses tree prefixes and may emit terminal color sequences.
    output = re.sub(r"\x1b\[[0-9;]*m", "", output)
    for line in output.splitlines():
        field = re.match(r"^[\s│├└─]*?(type|name|vendor|uuid|status):\s*(.*?)\s*$", line)
        if not field:
            continue
        key, value = field.groups()
        if key == "type" and current:
            devices.append(current)
            current = {}
        current[key] = value
    if current:
        devices.append(current)
    if output.strip() and not devices:
        raise ValueError("Unrecognized boltctl output.")
    result = []
    for device in devices:
        if device.get("type") == "host":
            continue
        if not device.get("uuid") or not device.get("status"):
            raise ValueError("Incomplete boltctl device status.")
        result.append({
            "id": device["uuid"],
            "name": " ".join(filter(None, (device.get("vendor"), device.get("name"))))
                    or "Thunderbolt device",
            "status": device["status"],
        })
    return result


async def bolt_status():
    env = os.environ.copy()
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("LD_PRELOAD", None)
    env.update({"LC_ALL": "C", "TERM": "dumb", "NO_COLOR": "1"})
    try:
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/boltctl", "list",
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
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


class Plugin:
    def __init__(self):
        self._restart_lock = asyncio.Lock()
        self._cooldown_until = 0.0

    async def get_bolt_status(self):
        return await bolt_status()

    async def get_status(self):
        devices = await asyncio.to_thread(gpu_status)
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
