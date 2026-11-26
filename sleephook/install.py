"""Install or remove the eGPU sleep hook. Runs as root through sudo."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from main import KEEP_DOCK_NAME, MANAGED, SLEEP_SERVICE, remove_managed, sleep_hook_files, write_managed  # noqa: E402


def systemctl(*args, check=True):
    subprocess.run(["/usr/bin/systemctl", *args], check=check, timeout=15)


def power(script, mode, check=True):
    subprocess.run([str(script), "pm", mode], check=check, timeout=15)


def check_sudoers(files):
    """visudo rejects a sudoers file that would break sudo; check it before it lands."""
    path, content, _ = next(file for file in files if file[0].parent.name == "sudoers.d")
    # Not in sudoers.d: sudo would read a staged file there that has no dot in its name.
    with tempfile.NamedTemporaryFile("w") as staged:
        staged.write(content)
        staged.flush()
        subprocess.run(["/usr/bin/visudo", "-cqf", staged.name], check=True, timeout=15)


def install():
    files = sleep_hook_files()
    check_sudoers(files)
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
