#!/usr/bin/python3
# Managed by eGPU Helper. Edits are overwritten when the plugin updates.
"""Give each GPU its own Steam shader cache.

Runs as ExecStartPre of gamescope-session.service, before Steam starts. Points every
library's steamapps/shadercache at shadercache.<profile> and swaps the ShaderCacheManager
block of Steam's config.vdf, so switching GPUs does not reprocess every game's shaders.
Never deletes a cache. Any failure leaves Steam's files as they were and exits 0.
"""
import os
from pathlib import Path
import re
import subprocess
import sys

AMD_VENDOR = "0x1002"
EGPU_DEVICES = tuple(f"0x{device}" for device in "@EGPU_DEVICES@".split(","))
PCI_DEVICES = Path("/sys/bus/pci/devices")
PCI_IDS = Path("/usr/share/hwdata/pci.ids")
HOME = Path.home()
STEAM = HOME / ".local/share/Steam"
STATE = HOME / ".local/share/egpu-helper/shaders"
PROFILE = r"igpu|egpu-[0-9a-f]{4}"
LINK_TARGET = re.compile(rf"shadercache\.({PROFILE})")


def log(message):
    print(f"egpu-shader-swap: {message}", file=sys.stderr)


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def is_egpu(device):
    return read(device / "vendor") == AMD_VENDOR and read(device / "device") in EGPU_DEVICES


def ready_egpu():
    """PCI device of an eGPU with amdgpu bound, or None."""
    for device in sorted(PCI_DEVICES.glob("*")):
        if is_egpu(device) and (device / "driver").is_symlink() and os.readlink(device / "driver").endswith("/amdgpu"):
            return device
    return None


def profile_now():
    device = ready_egpu()
    return f"egpu-{read(device / 'device')[2:]}" if device else "igpu"


def gpu_slot(profile):
    """PCI address of the GPU a profile runs on: the ready eGPU, or the first other display device."""
    if profile != "igpu":
        device = ready_egpu()
        return device.name if device else None
    for device in sorted(PCI_DEVICES.glob("*")):
        if (read(device / "class") or "").startswith("0x03") and not is_egpu(device):
            return device.name
    return None


def write_env(profile):
    """Pin Steam's OpenGL and Vulkan probes to the session's GPU.

    Without DRI_PRIME, Steam's OpenGL bucket probe sometimes lands on the other GPU, sees a
    "changed" bucket and prunes compiled shaders. steam-launcher.service reads this file.
    """
    slot = gpu_slot(profile)
    path = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "egpu-helper-environment"
    if not slot:
        path.unlink(missing_ok=True)
        return
    atomic_write(path, f"DRI_PRIME=pci-{re.sub(r'[:.]', '_', slot)}\n")


def steam_running():
    return subprocess.run(["/usr/bin/pgrep", "-u", str(os.getuid()), "-x", "steam"],
                          capture_output=True).returncode == 0


def libraries():
    found = [STEAM]
    text = read(STEAM / "steamapps/libraryfolders.vdf") or ""
    for match in re.finditer(r'"path"\s+"([^"]+)"', text):
        path = Path(match.group(1))
        if path.resolve() != STEAM.resolve():
            found.append(path)
    return [lib / "steamapps" for lib in found if (lib / "steamapps").is_dir()]


def config_path():
    return STEAM / "config/config.vdf"


def vdf_block(text, key):
    """(start, end) of the "key" { ... } block, including its closing brace line."""
    match = re.search(rf'^([ \t]*)"{key}"[ \t]*\r?\n\1\{{[ \t]*\r?\n', text, re.M)
    if not match:
        return None
    close = re.compile(rf"^{re.escape(match.group(1))}\}}[ \t]*\r?\n", re.M).search(text, match.end())
    return (match.start(), close.end()) if close else None


def chip_names():
    """{device ID: "NAVI23"} for the supported eGPUs, from the PCI ID database.
    Works while the eGPU is still off the bus, as it is early in boot."""
    names = {}
    vendor = False
    for line in (read(PCI_IDS) or "").splitlines():
        if not line or line.startswith("#"):
            continue
        if not line.startswith("\t"):
            vendor = line.startswith("1002 ")
            continue
        fields = line[1:].split(None, 1)
        if vendor and not line.startswith("\t\t") and len(fields) == 2 and f"0x{fields[0]}" in EGPU_DEVICES:
            chip = re.search(r"Navi (\d+)", fields[1])
            if chip:
                names[fields[0]] = f"NAVI{chip.group(1)}"
    return names


def bucket_owner():
    """Profile that Steam's current shader bucket was built for. Used once, on first adoption."""
    bucket = re.search(r'"CurrentBucketGPU"\s+"([^"]*)"', read(config_path()) or "")
    if bucket:
        for device, chip in chip_names().items():
            if chip in bucket.group(1).upper():
                return f"egpu-{device}"
    return "igpu"


def atomic_write(path, text):
    staged = path.with_name(path.name + ".egpu-tmp")
    staged.write_text(text)
    os.replace(staged, path)


def swap_config(current, profile):
    """Save current's ShaderCacheManager block, then put profile's saved block in its place."""
    config = config_path()
    if not config.is_file():
        return
    text = config.read_text()
    block = vdf_block(text, "ShaderCacheManager")
    if not block:
        return
    STATE.mkdir(parents=True, exist_ok=True)
    atomic_write(STATE / f"{current}.vdf", text[block[0]:block[1]])
    saved = STATE / f"{profile}.vdf"
    if not saved.is_file():
        # Steam sees a bucket for another GPU and processes shaders for this one once.
        return
    updated = text[:block[0]] + saved.read_text() + text[block[1]:]
    if updated != text:
        atomic_write(config.with_name(config.name + ".egpu-bak"), text)
        atomic_write(config, updated)


def point_library(steamapps, profile, owner):
    cache = steamapps / "shadercache"
    target = f"shadercache.{profile}"
    if cache.is_symlink():
        link = os.readlink(cache)
        if not LINK_TARGET.fullmatch(link):
            log(f"{cache} is a custom symlink; leaving it alone")
            return
        if link == target:
            return
    elif cache.is_dir():
        adopted = steamapps / f"shadercache.{owner}"
        if adopted.exists():
            log(f"{adopted} already exists; leaving {cache} alone")
            return
        cache.rename(adopted)
    elif cache.exists():
        return
    (steamapps / target).mkdir(exist_ok=True)
    staged = steamapps / "shadercache.egpu-tmp"
    if staged.is_symlink():
        staged.unlink()
    staged.symlink_to(target)
    os.replace(staged, cache)


def swap():
    if steam_running():
        log("Steam is running; not switching")
        return
    profile = profile_now()
    write_env(profile)
    current = read(STATE / "active")
    if not current or not re.fullmatch(PROFILE, current):
        current = bucket_owner()
    for steamapps in libraries():
        point_library(steamapps, profile, current)
    if current != profile:
        swap_config(current, profile)
    STATE.mkdir(parents=True, exist_ok=True)
    atomic_write(STATE / "active", profile + "\n")
    if current != profile:
        log(f"{current} -> {profile}")


def restore():
    """Turn the active profile's cache back into a plain shadercache folder. Other copies stay."""
    if steam_running():
        raise SystemExit("Exit Steam first.")
    for steamapps in libraries():
        cache = steamapps / "shadercache"
        if cache.is_symlink() and LINK_TARGET.fullmatch(os.readlink(cache)):
            real = steamapps / os.readlink(cache)
            cache.unlink()
            real.rename(cache)
    (STATE / "active").unlink(missing_ok=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--restore"]:
        restore()
    else:
        try:
            swap()
        except Exception as error:
            log(f"failed: {error}")
