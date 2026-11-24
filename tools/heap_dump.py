"""Snapshot the private RW heap to disk so structure hunting is repeatable.

Reads region by region in 1 MB chunks (never whole-process) and writes a flat
`dump/heap.bin` plus `dump/heap.json` with the region map. Protection in the
target is untouched except where a page is not readable at all, which is lifted
just long enough to read it.

  python tools/heap_dump.py --budget 1400
"""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manualmap  # noqa: E402
import pe_dump  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
CHUNK = 1 << 20
USABLE = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_ulonglong), ("AllocationBase", ctypes.c_ulonglong),
                ("AllocationProtect", ctypes.c_ulong), ("__a", ctypes.c_ulong),
                ("RegionSize", ctypes.c_ulonglong), ("State", ctypes.c_ulong),
                ("Protect", ctypes.c_ulong), ("Type", ctypes.c_ulong), ("__b", ctypes.c_ulong)]


def regions(handle: int) -> list[tuple[int, int, int]]:
    out = []
    addr = 0x10000
    mbi = MBI()
    while addr < 0x7FFFFFFFFFFF and len(out) < 60000:
        if not manualmap.k32.VirtualQueryEx(handle, ctypes.c_void_p(addr),
                                            ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        if mbi.State == 0x1000 and mbi.Type == 0x20000 and mbi.Protect & 0xFF in USABLE \
                and not mbi.Protect & 0x100:
            out.append((mbi.BaseAddress, mbi.RegionSize, mbi.Protect))
        addr = mbi.BaseAddress + max(mbi.RegionSize, 0x1000)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--budget", type=int, default=1400, help="megabytes to capture at most")
    ap.add_argument("--min", type=int, default=0x1000)
    args = ap.parse_args()
    pid = args.pid or manualmap.find_pid()
    if not pid:
        print("client not running")
        return 1
    handle, rights = manualmap.open_process(pid)
    if not handle:
        print(f"OpenProcess {rights}")
        return 1
    todo = [r for r in regions(handle) if r[1] >= args.min]
    todo.sort(key=lambda r: -r[1])
    limit = args.budget * 1024 * 1024
    out_path = ROOT / "dump/heap.bin"
    manifest = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pid": pid, "regions": []}
    written = 0
    started = time.time()
    with out_path.open("wb") as f:
        for base, size, protect in todo:
            take = min(size, limit - written)
            if take < 0x1000:
                break
            got = 0
            while got < take:
                piece = min(CHUNK, take - got)
                data = pe_dump._read_region(handle, base + got, piece, protect)
                if len(data) < piece:
                    data = data + bytes(piece - len(data))
                f.write(data)
                got += piece
            manifest["regions"].append({"file_offset": written, "base": base, "size": take,
                                        "protect": protect})
            written += take
            print(f"  {base:#x} {take / 2**20:7.1f} MB  total {written / 2**20:7.1f} MB", flush=True)
            if written >= limit:
                break
    (ROOT / "dump/heap.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"wrote {written / 2**20:.0f} MB in {time.time() - started:.0f}s "
          f"({len(manifest['regions'])} regions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
