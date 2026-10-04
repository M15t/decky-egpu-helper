"""One-time offline Linux setup, elevated through pkexec or run manually."""
import argparse
import json
import os
from pathlib import Path
import platform
import pwd
import shutil
import subprocess
import tempfile

from prepare import FILES, checked

ROOT = Path("/opt/decky-zapret2")
UNIT = Path("/etc/systemd/system/decky-zapret2.service")
RULE = Path("/etc/polkit-1/rules.d/50-decky-zapret2.rules")
STATE = Path("/var/lib/decky-zapret2")


def polkit_rule(user, uid):
    if type(uid) is not int or uid <= 0:
        raise ValueError("Choose the non-root user running Decky")
    return '''// Installed by decky-egpu-helper; start/stop this unit only.
polkit.addRule(function(action, subject) {
    if (subject.user === %s &&
        action.id === "org.freedesktop.systemd1.manage-units" &&
        action.lookup("unit") === "decky-zapret2.service" &&
        (action.lookup("verb") === "start" || action.lookup("verb") === "stop")) {
        return polkit.Result.YES;
    }
});
''' % json.dumps(user)


def trusted_directory(path):
    for parent in (path, *path.parents):
        stat = parent.lstat()
        if parent.is_symlink() or not parent.is_dir() or stat.st_uid != 0 or stat.st_mode & 0o022:
            raise RuntimeError(f"Unsafe installation directory: {parent}")


def install(user):
    if os.geteuid() != 0 or platform.system() != "Linux" or platform.machine() != "x86_64":
        raise RuntimeError("Installer requires root on x86_64 Linux")
    account = pwd.getpwnam(user)
    uid = account.pw_uid
    policy = polkit_rule(user, uid)
    pwd.getpwnam("nobody")
    for executable in ("/usr/bin/python3", "/usr/bin/systemctl"):
        if not Path(executable).is_file():
            raise RuntimeError(f"Missing {executable}")
    if not any(Path(p).is_file() for p in ("/usr/bin/nft", "/usr/sbin/nft")):
        raise RuntimeError("Install nftables with your distribution package manager first")
    for target in (ROOT, UNIT, RULE, STATE):
        trusted_directory(target.parent)
        if target.exists() or target.is_symlink():
            raise RuntimeError(f"Refusing to overwrite existing installation: {target}")
    source = Path(__file__).resolve().parent
    # Read and verify all shipped upstream bytes before modifying the system.
    contents = {Path(name).name: checked((source / "runtime" / Path(name).name).read_bytes(), digest) for name, digest in FILES.items()}
    contents["firewall.py"] = (source / "firewall.py").read_bytes()
    unit = (source / UNIT.name).read_bytes()
    stage = Path(tempfile.mkdtemp(prefix=".decky-zapret2-", dir=ROOT.parent))
    created = []
    try:
        for name, data in contents.items():
            target = stage / name
            target.write_bytes(data)
            target.chmod(0o755 if name == "nfqws2" else 0o644)
        stage.chmod(0o755)
        stage.rename(ROOT)
        created.append(ROOT)
        STATE.mkdir(mode=0o700)
        created.append(STATE)
        os.chown(STATE, uid, account.pw_gid)
        for target, data in ((UNIT, unit), (RULE, policy.encode())):
            with target.open("xb") as stream:
                created.append(target)
                stream.write(data)
            target.chmod(0o644)
        subprocess.run(["/usr/bin/systemctl", "daemon-reload"], check=True, timeout=15)
        # No --now: new installations stay OFF until the user selects ON.
        subprocess.run(["/usr/bin/systemctl", "enable", UNIT.name], check=True, timeout=15)
    except BaseException:
        if UNIT in created:
            try:
                subprocess.run(["/usr/bin/systemctl", "disable", UNIT.name], check=False, timeout=15)
            except (OSError, subprocess.SubprocessError):
                pass  # Still remove the newly created files if systemd is unavailable.
        for target in reversed(created):
            shutil.rmtree(target) if target in (ROOT, STATE) else target.unlink()
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    print("Installed. Default OFF. Decky ON/OFF is saved and restored at boot.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True, help="Non-root Linux user running Decky")
    install(parser.parse_args().user)
