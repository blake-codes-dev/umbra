"""Find lua_State by structure, not by offsets or by catching a register.

A Luau `lua_State` starts with a `global_State *`, and that `global_State` starts
with `mainthread`, a `lua_State *` back to itself. So the triple

    L -> global G,  G -> mainthread M,  M -> global G

is a self-checking pattern no ordinary struct satisfies, and it needs no knowledge
of the rest of either layout. `M`'s type tag (byte 8) must also be LUA_TTHREAD.

Runs read-only against the client. Loads the largest private RW heap regions,
then the whole test is vectorised over that memory - no per-candidate syscalls.

  python tools/vmheap.py                 # report lua_State candidates
  python tools/vmheap.py --budget 400    # MB of heap to load (default 256)
"""
from __future__ import annotations

import argparse
import ctypes
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manualmap  # noqa: E402
import pe_dump  # noqa: E402
import sigcheck  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LUA_TTHREAD = 9
GLOBAL_OFF = 0x68   # L->global, read off the interpreter itself (0x797376: mov rcx,[rbx+0x68])
TOP_OFF = 0x80     # L->top
BASE_OFF = 0x58    # L->base


def heap_regions(pid: int, limit: int = 40000) -> list[tuple[int, int]]:
    handle, _ = manualmap.open_process(pid)
    if not handle:
        return []

    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_ulonglong), ("AllocationBase", ctypes.c_ulonglong),
                    ("AllocationProtect", ctypes.c_ulong), ("__a", ctypes.c_ulong),
                    ("RegionSize", ctypes.c_ulonglong), ("State", ctypes.c_ulong),
                    ("Protect", ctypes.c_ulong), ("Type", ctypes.c_ulong), ("__b", ctypes.c_ulong)]

    out = []
    addr = 0x10000
    mbi = MBI()
    while addr < 0x7FFFFFFFFFFF and len(out) < limit:
        got = manualmap.k32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                          ctypes.sizeof(mbi))
        if not got:
            break
        if mbi.State == 0x1000 and mbi.Type == 0x20000 and mbi.Protect in (0x04, 0x08):
            out.append((mbi.BaseAddress, mbi.RegionSize))
        addr = mbi.BaseAddress + max(mbi.RegionSize, 0x1000)
    manualmap.k32.CloseHandle(handle)
    return out


def load(pid: int, regions: list[tuple[int, int]]) -> tuple[bytearray, list[tuple[int, int, int]]]:
    handle, _ = manualmap.open_process(pid)
    blob = bytearray()
    spans = []
    for base, size in regions:
        pad = (-len(blob)) % 8
        blob += b"\x00" * pad
        data = pe_dump._read_region(handle, base, size, 0x04)
        if len(data) < size:
            data += b"\x00" * (size - len(data))
        spans.append((base, size, len(blob)))
        blob += data
    manualmap.k32.CloseHandle(handle)
    return blob, spans


def locator(spans: list[tuple[int, int, int]], blob_len: int):
    bases = np.array([s[0] for s in spans], dtype=np.int64)
    sizes = np.array([s[1] for s in spans], dtype=np.int64)
    offs = np.array([s[2] for s in spans], dtype=np.int64)
    order = np.argsort(bases)
    bases, sizes, offs = bases[order], sizes[order], offs[order]

    def locate(values: np.ndarray) -> np.ndarray:
        idx = np.searchsorted(bases, values, side="right") - 1
        ok = (idx >= 0) & (idx < len(bases))
        idx_c = np.clip(idx, 0, len(bases) - 1)
        delta = values - bases[idx_c]
        ok &= delta >= 0
        ok &= delta < sizes[idx_c]
        byte = offs[idx_c] + delta
        aligned = (byte % 8) == 0
        out = np.where(ok & aligned, byte // 8, -1)
        return out.astype(np.int64)

    return locate


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--budget", type=int, default=256, help="MB of heap to load")
    ap.add_argument("--max", type=int, default=20, help="candidates to print")
    ap.add_argument("--dump", action="store_true", help="hex-dump candidate headers")
    args = ap.parse_args()
    pid = args.pid or sigcheck.find_pid()
    if not pid:
        print("client not running")
        return 1
    regions = heap_regions(pid)
    regions.sort(key=lambda r: -r[1])
    chosen, total = [], 0
    for base, size in regions:
        if size > 64 * 1024 * 1024:
            continue
        if total + size > args.budget * 1024 * 1024:
            break
        chosen.append((base, size))
        total += size
    print(f"{len(regions)} heap regions, loading {len(chosen)} ({total / 2**20:.0f} MB)")
    blob, spans = load(pid, chosen)
    qwords = np.frombuffer(blob, dtype="<u8")
    locate = locator(spans, len(blob))
    addr = np.zeros(len(qwords), dtype=np.int64)
    for base, size, off in spans:
        start = off // 8
        addr[start:start + size // 8] = base + np.arange(0, size - size % 8, 8, dtype=np.int64)

    tags = np.frombuffer(blob, dtype=np.uint8)
    pos = locate(qwords)
    # 1. every value that lands in loaded heap is a candidate lua_State address
    cand = np.nonzero((pos >= 0) & (addr > 0))[0]
    print(f"{len(cand)} pointers into loaded heap")
    if not len(cand):
        return 3
    state = pos[cand]
    # 2. a lua_State begins with its type header: tag LUA_TTHREAD
    keep = tags[state * 8] == LUA_TTHREAD
    cand, state = cand[keep], state[keep]
    print(f"  tagged thread: {len(cand)}")
    if args.dump:
        for i in range(min(len(cand), 5)):
            start = state[i]
            row = " ".join(f"{qwords[start + j]:#018x}" for j in range(10))
            print(f"    {addr[cand[i]]:#x}: {row}")
        return 0
    if not len(cand):
        return 3
    # 3. field at +8 is `global`; a global_State starts with `mainthread`
    g_pos = locate(qwords[state + GLOBAL_OFF // 8])
    keep = g_pos >= 0
    cand, state, g_pos = cand[keep], state[keep], g_pos[keep]
    if not len(cand):
        return 3
    g = qwords[state + GLOBAL_OFF // 8]
    main_pos = locate(qwords[g_pos])
    keep = main_pos >= 0
    cand, state, g_pos, g, main_pos = cand[keep], state[keep], g_pos[keep], g[keep], main_pos[keep]
    # 4. the back-reference: mainthread's own `global` must equal this global
    back = qwords[main_pos + GLOBAL_OFF // 8] == g
    cand, state, g, main_pos = cand[back], state[back], g[back], main_pos[back]
    print(f"  back-reference holds: {len(cand)}")
    if not len(cand):
        return 3
    # 5. and the mainthread must itself be a thread
    thread = (tags[main_pos * 8] == LUA_TTHREAD) & (tags[state * 8] == LUA_TTHREAD)
    cand, state, g, main_pos = cand[thread], state[thread], g[thread], main_pos[thread]
    print(f"{len(cand)} candidate lua_State(s)")
    for i in range(min(len(cand), args.max)):
        l_addr = addr[cand[i]]
        top = qwords[state[i] + TOP_OFF // 8]
        base = qwords[state[i] + BASE_OFF // 8]
        ok = "top>=base" if top >= base else "BAD-stack"
        print(f"  L={l_addr:#x} global={g[i]:#x} mainthread={qwords[main_pos[i]]:#x} "
              f"top={top:#x} base={base:#x} {ok}")
    return 0 if len(cand) else 3


if __name__ == "__main__":
    raise SystemExit(main())
