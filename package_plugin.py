from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED


root = Path(__file__).resolve().parent
files = ("dist/index.js", "main.py", "plugin.json", "package.json", "README.md",
         "autofix/egpu-gamescope-fix.sh", "autofix/egpu-gamescope-fix.service",
         "sleephook/egpu-sleep.sh", "sleephook/egpu-sleep.service", "sleephook/90-egpu-helper.rules",
         "sleephook/egpu-thunderbolt.conf",
         "sleephook/egpu-helper.sudoers",
         "sleephook/install.py",
         "shaders/egpu-shader-swap.py", "shaders/egpu-helper-shaders.conf",
         "shaders/egpu-helper-steam.conf")
for name in files:
    if not (root / name).is_file():
        raise SystemExit(f"Missing {name}; run pnpm build first.")
with ZipFile(root / "decky-egpu-helper.zip", "w", ZIP_DEFLATED) as archive:
    for name in files:
        archive.write(root / name, f"egpu-gamescope/{name}")
print(root / "decky-egpu-helper.zip")
