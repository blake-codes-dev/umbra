"""Locate a live lua_State by structure, then (optionally) poke it.

Offsets are read out of the client's own interpreter:

  0x2576d30  mov rdi,[rcx+0x80]     lua_State.top
  0x2576d79  mov r15,[rcx+0x58]     lua_State.ci
  0x2576d7d  sub r15,[rcx+0x20]     lua_State.ci_base
  0x2576ece  mov rax,[rax+0x10]     CallInfo.base, spilled to lua_State+0x70
  0x2576d29  cmp byte [rdx+6],0     C closure flag on the frame's function
  0x2585a8a  cmp dword [rax+0xc],7  func TValue tag == LUA_TFUNCTION

Relations that hold for every real state seen live:
    ci->base == lua_State.base (0x70),  base <= top (0x80), (top-base) % 16 == 0
    frame function slot (ci+8) has tag 7 and sits just under ci->base

Read-only unless --call is given.

  python tools/luastate_scan.py --budget 420
  python tools/luastate_scan.py --call 0x58c2a40      # luaB_print(L)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manualmap  # noqa: E402
import pe_dump  # noqa: E402
import vmheap  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CI = 0x58 // 8
CI_BASE = 0x20 // 8
BASE = 0x70 // 8
TOP = 0x80 // 8
LUA_TFUNCTION = 7


def scan(pid: int, budget_mb: int):
    regions = sorted(vmheap.heap_regions(pid), key=lambda r: -r[1])
    chosen, total = [], 0
    for base, size in regions:
        if total + size > budget_mb * 1024 * 1024:
            break
        chosen.append((base, size))
        total += size
    print(f"loading {total / 2**20:.0f} MB of {len(regions)} heap regions")
    blob, spans = vmheap.load(pid, chosen)
    q = np.frombuffer(blob, dtype="<u8")
    u32 = np.frombuffer(blob, dtype=np.uint32)
    loc = vmheap.locator(spans, len(blob))

    def at(values) -> np.ndarray:
        return loc(np.asarray(values, dtype=np.uint64).astype(np.int64))

    pos = at(q)
    slot = np.nonzero((pos >= 0) & (q > 0x10000))[0]
    L, value = pos[slot], q[slot].copy()
    print(f"candidates: {len(L)}")

    def narrow(mask, *arrays):
        return (L[mask], value[mask], *(a[mask] for a in arrays))

    ci = q[L + CI]
    keep = at(ci) >= 0
    L, value, ci = narrow(keep, ci)
    ci_base = q[L + CI_BASE]
    keep = (at(ci_base) >= 0) & (ci >= ci_base) & ((ci - ci_base) < 0x400)
    L, value, ci, ci_base = narrow(keep, ci, ci_base)
    base, top = q[L + BASE], q[L + TOP]
    keep = (at(base) >= 0) & (at(top) >= 0) & (base <= top) \
        & ((top - base) % 16 == 0) & ((top - base) < (1 << 20))
    L, value, ci, base, top = narrow(keep, ci, base, top)
    print(f"  stack slice: {len(L)}")

    cip = at(ci)
    func = q[L + 0x70 // 8]
    if True:
        frame_base = q[np.clip(cip + 2, 0, len(q) - 1)]
        cf = q[np.clip(cip + 1, 0, len(q) - 1)]
        keep = (cip >= 0) & (cf == func) & (frame_base == base) & (func + 0x10 == base)
        L, value, ci, base, top, func = narrow(keep, ci, base, top, func)
        print(f"  ci frame mirrors state: {len(L)}")


    fp = at(func)
    keep = (fp >= 0) & (u32[np.clip(fp * 2 + 3, 0, len(u32) - 1)] == LUA_TFUNCTION)
    L, value, ci, base, top, func = narrow(keep, ci, base, top, func)
    print(f"  func slot just under base: {len(L)}")

    return [{"lua_state": int(value[i]), "ci": int(ci[i]), "base": int(base[i]),
             "top": int(top[i]), "func": int(func[i])} for i in range(len(L))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--budget", type=int, default=400)
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--call", default="")
    args = ap.parse_args()
    pid = args.pid or manualmap.find_pid()
    if not pid:
        print("client not running")
        return 1
    found = scan(pid, args.budget)
    seen = {}
    for item in found:
        seen.setdefault(item["lua_state"], item)
    for key, item in seen.items():
        print(f"  L={key:#x} ci={item['ci']:#x} base={item['base']:#x} "
              f"top={item['top']:#x} func={item['func']:#x}")
    if args.dump and seen:
        handle, _ = manualmap.open_process(pid)
        for key in list(seen)[:2]:
            data = pe_dump._read_region(handle, key, 0xA0, 0x04)
            for row in range(0, len(data), 16):
                print(f"    {key + row:#x}  {data[row:row + 16].hex()}")
    print(f"{len(seen)} distinct candidate(s)")
    return 0 if seen else 3


if __name__ == "__main__":
    raise SystemExit(main())
