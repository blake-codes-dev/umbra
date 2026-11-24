"""Find the live lua_State by CallInfo structure (offsets read off the client's
own interpreter, not guessed):

    L+0x20  embedded base CallInfo, CallInfo is 0x30 bytes
    L+0x58  L->ci           (0x7969dc: mov rcx,[rbx+0x58])
    L+0x68  L->global       (0x7969a0: mov rcx,[rbx+0x68])
    L+0x80  L->top          (0x7969e0: mov rax,[rbx+0x80])
    ci+0x08 pointer to the frame's function TValue, tag at +0x0c == LUA_TFUNCTION(7)
    ci+0x18 frame top >= L->top

Vectorised over loaded heap regions. Read-only.

    python tools/luastate2.py --budget 600
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manualmap
import sigcheck
import vmheap

CI, GLOBAL, TOP = 0x58, 0x68, 0x80
BASE_CI = 0x20
CI_SIZE = 0x30
TAG_FUNCTION = 7


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--budget", type=int, default=600, help="MB of heap to load")
    ap.add_argument("--max", type=int, default=12)
    args = ap.parse_args()
    pid = args.pid or sigcheck.find_pid()
    if not pid:
        print("client not running")
        return 1
    regions = vmheap.heap_regions(pid)
    regions.sort(key=lambda r: -r[1])
    chosen, total = [], 0
    for base, size in regions:
        if total + size > args.budget * 1024 * 1024:
            continue
        chosen.append((base, size))
        total += size
    print(f"loading {total // 2**20} MB of {len(regions)} heap regions")
    blob, spans = vmheap.load(pid, chosen)
    blob += b"\x00" * 0x400   # guard for field reads at the end
    q = np.frombuffer(blob, dtype="<u8")
    u32 = np.frombuffer(blob, dtype="<u4")
    locate = vmheap.locator(spans, len(blob))
    addr = np.zeros(len(q), dtype=np.int64)
    for base, size, off in spans:
        s = off // 8
        addr[s:s + size // 8] = base + np.arange(0, size - size % 8, 8, dtype=np.int64)

    pos = locate(q)
    cand = np.nonzero((pos >= 0) & (addr > 0))[0]
    state = np.unique(pos[cand])
    print(f"{len(cand)} pointers into loaded heap -> {len(state)} distinct targets")
    if not len(state):
        return 3

    def field(state, off):
        return q[state + off // 8]

    def index(values):
        return locate(values)

    ci = field(state, CI)
    ci_i = index(ci)
    keep = ci_i >= 0
    state, ci, ci_i = state[keep], ci[keep], ci_i[keep]
    print(f"  ci in heap: {len(state)}")
    if not len(state):
        return 3

    # CallInfo runs from L+0x20 upward in 0x30-byte steps
    delta = ci - addr[state] - BASE_CI
    keep = (delta >= 0) & (delta < 0x40000) & (delta % CI_SIZE == 0)
    state, ci, ci_i = state[keep], ci[keep], ci_i[keep]
    print(f"  ci aligned to embedded base: {len(state)}")
    for k in range(min(6, len(state))):
        L = int(addr[state[k]])
        print(f"    dbg L={L:#x} ci={int(ci[k]):#x} bytes={blob[state[k]*8:state[k]*8+24].hex()}")
        print("        ci:" + " ".join(f"{int(q[ci_i[k]+j]):#x}" for j in range(6)))
    if not len(state):
        return 3

    # frame function TValue and its tag
    func = q[ci_i + 1]
    f_i = index(func)
    keep = f_i >= 0
    state, ci, ci_i, func, f_i = state[keep], ci[keep], ci_i[keep], func[keep], f_i[keep]
    tag = u32[f_i * 2 + 3]
    keep = tag == TAG_FUNCTION
    state, ci, ci_i, func, f_i = state[keep], ci[keep], ci_i[keep], func[keep], f_i[keep]
    print(f"  frame tag function: {len(state)}")
    if not len(state):
        return 3

    # L->top must be a readable heap pointer at or below the frame top
    top = field(state, TOP)
    t_i = index(top)
    keep = t_i >= 0
    state, ci, ci_i, top, t_i = state[keep], ci[keep], ci_i[keep], top[keep], t_i[keep]
    frame_top = q[ci_i + 3]
    keep = top <= frame_top
    state, ci, ci_i, top, t_i = state[keep], ci[keep], ci_i[keep], top[keep], t_i[keep]
    print(f"  top within frame: {len(state)}")
    if not len(state):
        return 3

    for k in range(min(3, len(state))):
        L = int(addr[state[k]])
        print(f"    pre-global L={L:#x} ci={int(ci[k]):#x} func={int(func[k]):#x} "
              f"top={int(top[k]):#x} frame_top={int(q[ci_i[k]+3]):#x}")
        print("      hdr " + " ".join(f"{int(q[state[k]+j]):#018x}" for j in range(16)))
    # L->global with sane GC thresholds
    g = field(state, GLOBAL)
    g_i = index(g)
    keep = g_i >= 0
    state, g, g_i = state[keep], g[keep], g_i[keep]
    print(f"  global in heap: {len(state)}")
    for k in range(min(6, len(state))):
        L = int(addr[state[k]])
        print(f"    survivor L={L:#x} ci={int(ci[k]):#x} func={int(func[k]):#x} "
              f"top={int(top[k]):#x} frame_top={int(q[ci_i[k]+3]):#x} g={int(g[k]):#x}")
    if not len(state):
        return 3

    uniq, first = np.unique(state, return_index=True)
    print(f"{len(uniq)} distinct lua_State candidate(s)")
    for i in first[:args.max]:
        L = int(addr[state[i]])
        idx = int(state[i])
        marker = int(q[idx]) & 0xFF
        marked = int(blob[idx * 8])
        print(f"  L={L:#x} ci={int(ci[i]):#x} func={int(func[i]):#x} top={int(top[i]):#x} "
              f"g={int(g[i]):#x} hdr0={marked:#04x}")
        row = " ".join(f"{int(q[idx + j]):#018x}" for j in range(16))
        print(f"      {row}")
    return 0 if len(uniq) else 3


if __name__ == "__main__":
    raise SystemExit(main())
