"""Own exactly one nftables table. Invoked only by the dedicated system unit."""
import json
from pathlib import Path
import subprocess
import sys

TABLE = "decky_zapret2"
OWNER = "decky-egpu-helper-zapret2-v1"
QUEUE = 28745
NFT = next((str(p) for p in (Path("/usr/sbin/nft"), Path("/usr/bin/nft")) if p.is_file()), "/usr/sbin/nft")
RULES = '''create table inet decky_zapret2 { comment "decky-egpu-helper-zapret2-v1"; }
add chain inet decky_zapret2 raw_output { type filter hook output priority -401; policy accept; }
add rule inet decky_zapret2 raw_output meta mark & 0x40000000 != 0 notrack
add chain inet decky_zapret2 output { type filter hook output priority -150; policy accept; }
add rule inet decky_zapret2 output oifname "lo" return
add rule inet decky_zapret2 output ip daddr { 0.0.0.0/8, 10.0.0.0/8, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 240.0.0.0/4 } return
add rule inet decky_zapret2 output ip6 daddr { ::/128, ::1/128, fc00::/7, fe80::/10, ff00::/8 } return
add rule inet decky_zapret2 output meta mark & 0x40000000 == 0 tcp dport { 80, 443 } queue num 28745 bypass
'''


def nft(*args, input=None):
    return subprocess.run([NFT, *args], input=input, text=True, capture_output=True, check=True, timeout=8).stdout


def remove_owned_table():
    # Listing all tables distinguishes a missing table from permissions/kernel errors.
    tables = json.loads(nft("-j", "list", "tables"))["nftables"]
    exists = any(item.get("table", {}).get("name") == TABLE and item["table"].get("family") == "inet" for item in tables)
    if not exists:
        return
    details = json.loads(nft("-j", "list", "table", "inet", TABLE))["nftables"]
    if not any(item.get("table", {}).get("comment") == OWNER for item in details):
        raise RuntimeError("Refusing to change an existing table not owned by this helper")
    nft("delete", "table", "inet", TABLE)


def start():
    queues = Path("/proc/net/netfilter/nfnetlink_queue")
    if queues.exists() and any(line.split() and int(line.split()[0]) == QUEUE for line in queues.read_text().splitlines()):
        raise RuntimeError("NFQUEUE 28745 is already in use")
    remove_owned_table()
    nft("-f", "-", input=RULES)


if __name__ == "__main__":
    if sys.argv[1:] == ["start"]:
        start()
    elif sys.argv[1:] == ["stop"]:
        remove_owned_table()
    else:
        raise SystemExit("Expected start or stop")
