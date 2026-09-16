from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED


root = Path(__file__).resolve().parent
files = ("dist/index.js", "main.py", "plugin.json", "package.json", "README.md")
for name in files:
    if not (root / name).is_file():
        raise SystemExit(f"Missing {name}; run pnpm build first.")
with ZipFile(root / "decky-egpu-helper.zip", "w", ZIP_DEFLATED) as archive:
    for name in files:
        archive.write(root / name, f"egpu-gamescope/{name}")
print(root / "decky-egpu-helper.zip")
