"""Install or remove the eGPU sleep hook. Runs as root through pkexec."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from main import KEEP_DOCK_NAME, MANAGED, SLEEP_SERVICE, remove_managed, sleep_hook_files, write_managed  # noqa: E402


def systemctl(*args, check=True):
    subprocess.run(["/usr/bin/systemctl", *args], check=check, timeout=15)


def power(script, mode, check=True):
    subprocess.run([str(script), "pm", mode], check=check, timeout=15)


def install():
    files = sleep_hook_files()
    write_managed(files)
    systemctl("daemon-reload")
    systemctl("enable", SLEEP_SERVICE)
    # The udev rule runs at the next boot; apply it to this one too.
    power(files[0][0], "on")


def keep_dock(enabled):
    script = sleep_hook_files()[0][0]
    if not script.is_file():
        raise SystemExit("Install the sleep hook first.")
    marker = script.with_name(KEEP_DOCK_NAME)
    if enabled:
        marker.write_text(MANAGED + "\n")
        marker.chmod(0o644)
    else:
        marker.unlink(missing_ok=True)


def remove():
    systemctl("disable", SLEEP_SERVICE, check=False)
    files = sleep_hook_files()
    if files[0][0].is_file():
        power(files[0][0], "auto", check=False)
    remove_managed(files)
    files[0][0].with_name(KEEP_DOCK_NAME).unlink(missing_ok=True)
    try:
        files[0][0].parent.rmdir()
    except OSError:
        pass  # Keeps a hand-installed copy or its .bak.
    systemctl("daemon-reload")


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("Run as root.")
    parser = argparse.ArgumentParser()
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--keep-dock", choices=("on", "off"))
    args = parser.parse_args()
    if args.remove:
        remove()
    elif args.keep_dock:
        keep_dock(args.keep_dock == "on")
    else:
        install()
