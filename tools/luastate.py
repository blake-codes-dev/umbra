"""Find a live lua_State by looking at what the interpreter itself is holding.

Instead of guessing struct offsets from a header, this asks the running process:
threads parked inside the Luau interpreter have a `lua_State *` in a register,
and that pointer can be validated structurally without knowing the layout -
a `lua_State` starts with a `global_State *`, both live in private RW heap.

Read-only. Nothing is written to the target.

  python tools/luastate.py                # report candidates
  python tools/luastate.py --pid 1234 --verbose
"""
from __future__ import annotations

import argparse
import ctypes
import json
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manualmap  # noqa: E402
import sigcheck  # noqa: E402
import pe_dump  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
k32 = manualmap.k32
CONTEXT_CONTROL_INTEGER = 0x00100003
RIP_OFFSET = 0xF8
FLAGS_OFFSET = 0x30
CONTEXT_SIZE = 1232
# rcx rdx r8 r9 rbx rsi rdi r12..r15 rbp
GP_OFFSETS = {"rcx": 0x80, "rdx": 0x88, "rbx": 0x90, "rsi": 0xA8, "rdi": 0xB0,
              "r8": 0xB8, "r9": 0xC0, "r12": 0xD8, "r13": 0xE0, "r14": 0xE8,
              "r15": 0xF0, "rbp": 0xA0}


def dispatch_ranges(handle: int, base: int) -> list[tuple[int, int]]:
    """Locate the interpreter copies from patterns.json and widen them."""
    path = ROOT / "patterns.json"
    out = []
    for entry in json.loads(path.read_text(encoding="utf-8"))["patterns"]:
        if not entry.get("func_va") or "dispatch" not in entry["name"]:
            continue
        rva = int(entry["func_va"], 16)
        out.append((base + rva - 0x40000, base + rva + 0x40000))
    return out


def private_rw(pid: int) -> list[tuple[int, int]]:
    """Regions that could hold heap objects."""
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
    while addr < 0x7FFFFFFFFFFF and len(out) < 40000:
        if not k32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        if mbi.State == 0x1000 and mbi.Type == 0x20000 and mbi.Protect in (0x04, 0x08):
            out.append((mbi.BaseAddress, mbi.BaseAddress + mbi.RegionSize))
        addr = mbi.BaseAddress + max(mbi.RegionSize, 0x1000)
    k32.CloseHandle(handle)
    return out


def read_qword(handle: int, address: int):
    buffer = ctypes.create_string_buffer(8)
    got = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(handle, ctypes.c_void_p(address), buffer, 8, ctypes.byref(got)):
        return None
    return struct.unpack("<Q", buffer.raw)[0]


def inside(ranges, value: int) -> bool:
    return any(lo <= value < hi for lo, hi in ranges)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--samples", type=int, default=1)
    ap.add_argument("--interval", type=float, default=0.15)
    args = ap.parse_args()
    pid = args.pid or sigcheck.find_pid()
    if not pid:
        print("client not running")
        return 1
    handle, rights = manualmap.open_process(pid)
    if not handle:
        print(f"OpenProcess {rights}")
        return 1
    base = sigcheck.remote_module_base(pid, "RobloxPlayerBeta.exe")
    zones = dispatch_ranges(handle, base)
    heap = private_rw(pid)
    if not zones:
        print("no dispatch anchors in patterns.json")
        return 2
    hits = []
    for _ in range(max(1, args.samples)):
        hits += sample_once(handle, pid, zones, heap, args.verbose)
        if hits:
            break
        time.sleep(args.interval)
    seen = {}
    for hit in hits:
        seen.setdefault((hit["lua_state"], hit["reg"]), hit)
    for hit in seen.values():
        print(f"  candidate lua_State {hit['lua_state']} in {hit['reg']} on tid {hit['tid']} "
              f"global {hit['global_state']}")
    print(f"{len(seen)} candidate(s)")
    return 0 if seen else 3


def sample_once(handle, pid, zones, heap, verbose, any_rip=False):
    hits = []
    for tid in manualmap.threads_of(pid):
        thread = k32.OpenThread(0x1F03FF, False, tid)
        if not thread:
            continue
        if k32.SuspendThread(thread) == 0xFFFFFFFF:
            k32.CloseHandle(thread)
            continue
        context = ctypes.create_string_buffer(CONTEXT_SIZE)
        struct.pack_into("<I", context, FLAGS_OFFSET, CONTEXT_CONTROL_INTEGER)
        if not k32.GetThreadContext(thread, context):
            k32.ResumeThread(thread)
            k32.CloseHandle(thread)
            continue
        rip = struct.unpack_from("<Q", context, RIP_OFFSET)[0]
        in_vm = any_rip or any(lo <= rip < hi for lo, hi in zones)
        if verbose:
            print(f"  tid {tid} rip {rip:#x} in_vm={in_vm}")
        if in_vm:
            for name, offset in GP_OFFSETS.items():
                value = struct.unpack_from("<Q", context, offset)[0]
                if not inside(heap, value):
                    continue
                top = read_qword(handle, value + 0x80)
                base = read_qword(handle, value + 0x78)
                func = read_qword(handle, value + 0x70)
                ci = read_qword(handle, value + 0x58)
                if None in (top, base, func, ci):
                    continue
                if not (inside(heap, top) and inside(heap, base) and inside(heap, ci)):
                    continue
                if not base <= top or (top - base) % 16:
                    continue
                if read_qword(handle, ci + 8) != func:
                    continue
                if read_qword(handle, ci + 0x10) != base:
                    continue
                global_state = read_qword(handle, value + 0x68)
                hits.append({"tid": tid, "reg": name, "lua_state": hex(value),
                             "global_state": hex(global_state)})
        k32.ResumeThread(thread)
        k32.CloseHandle(thread)
    return hits


if __name__ == "__main__":
    raise SystemExit(main())
