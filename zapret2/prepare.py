"""Prepare only the pinned runtime; never execute downloaded files."""
import hashlib
from pathlib import Path
import sys
import tarfile

VERSION = "v1.0.5.2"
COMMIT = "6b6c63e3385fa73f8af3be4a69171e947f5a319d"
URL = f"https://github.com/bol-van/zapret2/releases/download/{VERSION}/zapret2-{VERSION}.tar.gz"
ARCHIVE_SHA256 = "fb3bcf69e7d86b9fa2d60bd53c956ac06d9dcc3adf9392afd84865f1d94b1158"
FILES = {
    "binaries/linux-x86_64/nfqws2": "de1414b1e0f9a5659d438cddcb0c7e9099b4533bf5c8a3cf841c7bc7e5aa1473",
    "lua/zapret-lib.lua": "b67a470f23b00a8d6e732c4e5135a39b224511e0b71809d5f4616adf62674980",
    "lua/zapret-antidpi.lua": "31c9dd75b0bd55e98e5306293f2be81e9d2ecadcbbf9157394ff37dcff7dc85a",
    "docs/LICENSE.txt": "d089978dd77d53cb6aa5dff51cfdbff617e52dd44af1ac44a6df02c6644f17d5",
}


def checked(data, expected):
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Zapret2 checksum mismatch")
    return data


def prepare(archive, destination):
    checked(Path(archive).read_bytes(), ARCHIVE_SHA256)
    contents = {}
    with tarfile.open(archive, "r:gz") as source:
        for name, digest in FILES.items():
            member = source.getmember(f"zapret2-{VERSION}/{name}")
            if not member.isfile() or member.size > 1024 * 1024:
                raise ValueError("Unexpected runtime archive member")
            contents[Path(name).name] = checked(source.extractfile(member).read(), digest)
    destination.mkdir(parents=True, exist_ok=True)
    for name, data in contents.items():
        (destination / name).write_bytes(data)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"Usage: python3 prepare.py ARCHIVE\nDownload: {URL}")
    prepare(sys.argv[1], Path(__file__).resolve().parent / "runtime")
