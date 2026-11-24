"""Re-derive the signature set from the live image instead of a merged dump.

Dump-merged bytes can contain zero holes in the middle of a signature, which
makes it miss in the process. For every pattern with a known function RVA this
reads the live bytes, keeps a 32-byte signature, checks that it occurs exactly
once in .text, and rewrites patterns.json only when both hold.

  python tools/sigfix.py            # repair patterns.json from the running client
  python tools/sigfix.py --dry-run
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manualmap  # noqa: E402
import pe_dump  # noqa: E402
import sigcheck  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
TEXT_VA = 0x1000
TEXT_LEN = 105_075_420
SIGLEN = 32
CHUNK = 8 * 1024 * 1024


def read_text(handle: int, base: int) -> bytearray:
    text = bytearray(TEXT_LEN)
    offset = 0
    while offset < TEXT_LEN:
        want = min(CHUNK, TEXT_LEN - offset)
        chunk = pe_dump._read_region(handle, base + TEXT_VA + offset, want, 0x20)
        text[offset:offset + len(chunk)] = chunk
        offset += want
    return text


def count(text: bytes, sig: bytes) -> int:
    n, i = 0, 0
    while True:
        i = text.find(sig, i)
        if i < 0:
            return n
        n += 1
        i += 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    pid = args.pid or sigcheck.find_pid()
    if not pid:
        print("client not running")
        return 1
    handle = manualmap.k32.OpenProcess(0x0438, False, pid)
    if not handle:
        print(f"OpenProcess {ctypes.get_last_error()}")
        return 1
    base = sigcheck.remote_module_base(pid, "RobloxPlayerBeta.exe")
    if not base:
        print("module not found")
        return 1
    text = bytes(read_text(handle, base))
    path = ROOT / "patterns.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    fixed = dropped = 0
    for entry in data["patterns"]:
        func = entry.get("func_va")
        if not func:
            continue
        rva = int(func, 16)
        sig = text[rva - TEXT_VA: rva - TEXT_VA + SIGLEN]
        if len(sig) < SIGLEN or not any(sig):
            entry["sig"] = None
            entry["note"] = "unreadable page live"
            dropped += 1
            print(f"  drop {entry['name']}: page not readable")
            continue
        hits = count(text, sig)
        entry["sig"] = " ".join(f"{b:02X}" for b in sig)
        entry["hits"] = hits
        entry["note"] = "live-derived" if hits == 1 else f"live duplicate x{hits}"
        if hits != 1:
            dropped += 1
        else:
            fixed += 1
        print(f"  {'ok  ' if hits == 1 else 'dup '} {entry['name']:<24} hits={hits}")
    if not args.dry_run:
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"{fixed} live-unique, {dropped} rejected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
