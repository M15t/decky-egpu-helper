from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from zapret2.prepare import FILES, checked


root = Path(__file__).resolve().parent
for name, digest in FILES.items():
    checked((root / "zapret2/runtime" / Path(name).name).read_bytes(), digest)
files = ("dist/index.js", "main.py", "plugin.json", "package.json", "README.md",
         "zapret2/prepare.py", "zapret2/install.py", "zapret2/firewall.py", "zapret2/decky-zapret2.service",
         "zapret2/runtime/nfqws2", "zapret2/runtime/zapret-lib.lua", "zapret2/runtime/zapret-antidpi.lua", "zapret2/runtime/LICENSE.txt")
for name in files:
    if not (root / name).is_file():
        raise SystemExit(f"Missing {name}; run pnpm build first.")
with ZipFile(root / "decky-egpu-helper.zip", "w", ZIP_DEFLATED) as archive:
    for name in files:
        archive.write(root / name, f"egpu-gamescope/{name}")
print(root / "decky-egpu-helper.zip")
