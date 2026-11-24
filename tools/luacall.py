"""Call one Luau C function with a live lua_State as its only argument.

This is the M2 probe: it finds a state from thread registers, checks that the
frame really holds a Lua function, then plants the register-preserving thunk
that calls `fn(L)` on a thread that is already executing client code.

Success is judged by the client log growing with a fresh Lua print line - and by
the client still being alive afterwards.

  python tools/luacall.py                      # calls luaB_print on the state
  python tools/luacall.py --fn 0x58c2a40 --dump

Nothing is written to the client image; one page of private RX is added per call.
"""
from __future__ import annotations

import argparse
import ctypes
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import luastate  # noqa: E402
import manualmap  # noqa: E402
import sigcheck  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LOG_DIR = Path.home() / "AppData/Local/Roblox/logs"


def newest_log() -> Path | None:
    logs = sorted(LOG_DIR.glob("*Player*.log"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def find_state(pid: int, seconds: float = 25.0) -> dict | None:
    handle, _ = manualmap.open_process(pid)
    if not handle:
        return None
    base = sigcheck.remote_module_base(pid, "RobloxPlayerBeta.exe")
    zones = luastate.dispatch_ranges(handle, base)
    heap = luastate.private_rw(pid)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        hits = luastate.sample_once(handle, pid, zones, heap, False, True)
        for hit in hits:
            state = int(hit["lua_state"], 16)
            check = verify(handle, state, heap)
            if check:
                return {"state": state, "tid": hit["tid"], "reg": hit["reg"], **check}
        time.sleep(0.02)
    return None


def verify(handle: int, state: int, heap) -> dict:
    def rd(addr, size=8) -> bytes:
        buf = ctypes.create_string_buffer(max(size, 8))
        got = ctypes.c_size_t()
        if not manualmap.k32.ReadProcessMemory(handle, ctypes.c_void_p(addr), buf, size,
                                               ctypes.byref(got)):
            return b""
        return buf.raw[:size]

    func = int.from_bytes(rd(state + 0x70), "little")
    tag = int.from_bytes(rd(func + 0xC, 4), "little")
    closure_flag = rd(func + 6, 1)
    top = int.from_bytes(rd(state + 0x80), "little")
    base = int.from_bytes(rd(state + 0x78), "little")
    ci = int.from_bytes(rd(state + 0x58), "little")
    ci_base = int.from_bytes(rd(state + 0x20), "little")
    nci = int.from_bytes(rd(state + 0x30, 2), "little")
    if ci != ci_base or nci > 4:
        return {}
    if func + 0x10 != base or base != top:
        return {}
    return {"func": func, "kind": "c" if closure_flag and closure_flag[0] else "lua",
            "argc": (top - base) // 16, "tag": tag}


def read_qword(pid: int, address: int):
    handle, _ = manualmap.open_process(pid)
    buf = ctypes.create_string_buffer(8)
    got = ctypes.c_size_t()
    if not manualmap.k32.ReadProcessMemory(handle, ctypes.c_void_p(address), buf, 8,
                                           ctypes.byref(got)):
        return None
    return struct.unpack("<Q", buf.raw)[0]


def pick_thread(pid: int, base: int, preferred: int, seconds: float) -> int | None:
    """Wait for a thread that is actually executing client code."""
    lo, hi = base + 0x1000, base + 0x1000 + 105_075_420
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for tid in manualmap.threads_of(pid):
            thread = manualmap.k32.OpenThread(0x1F03FF, False, tid)
            if not thread:
                continue
            if manualmap.k32.SuspendThread(thread) == 0xFFFFFFFF:
                manualmap.k32.CloseHandle(thread)
                continue
            context = ctypes.create_string_buffer(luastate.CONTEXT_SIZE)
            struct.pack_into("<I", context, luastate.FLAGS_OFFSET,
                             luastate.CONTEXT_CONTROL_INTEGER)
            hot = False
            if manualmap.k32.GetThreadContext(thread, context):
                rip = struct.unpack_from("<Q", context, luastate.RIP_OFFSET)[0]
                hot = lo <= rip < hi
            manualmap.k32.ResumeThread(thread)
            manualmap.k32.CloseHandle(thread)
            if hot:
                return tid
        time.sleep(0.02)
    return preferred or None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fn", default="0x58c2a40", help="RVA of the function to call")
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--seconds", type=float, default=25.0)
    ap.add_argument("--dump", action="store_true")
    args = ap.parse_args()
    pid = args.pid or manualmap.find_pid()
    if not pid:
        print("client not running")
        return 1
    base = sigcheck.remote_module_base(pid, "RobloxPlayerBeta.exe")
    if not base:
        print("no module base")
        return 1
    log = newest_log()
    before = log.stat().st_size if log else 0

    found = find_state(pid, args.seconds)
    if not found:
        print("no lua_State found (is the client actually running a script?)")
        return 3
    state = found["state"]
    print(f"state {state:#x} on tid {found['tid']} ({found['reg']}), "
          f"frame {found['kind']} func {found['func']:#x} argc {found['argc']} "
          f"tag {found['tag']}")
    if args.dump:
        handle, _ = manualmap.open_process(pid)
        buf = ctypes.create_string_buffer(0xA0)
        got = ctypes.c_size_t()
        manualmap.k32.ReadProcessMemory(handle, ctypes.c_void_p(state), buf, 0xA0,
                                        ctypes.byref(got))
        for row in range(0, 0xA0, 16):
            print(f"  {state + row:#x}  {buf.raw[row:row + 16].hex()}")

    entry = base + int(args.fn, 16)
    tid = pick_thread(pid, base, found["tid"], 15.0)
    if not tid:
        print("no thread entered client code")
        return 4
    before_top = read_qword(pid, state + 0x80)
    result = manualmap._hijack(manualmap.open_process(pid)[0], tid, pid, entry, state)
    if "error" in result:
        print("hijack:", result["error"])
        return 5
    thread = result["thread"]
    for _ in range(8):
        if manualmap.k32.ResumeThread(thread) <= 0:
            break
    manualmap.k32.CloseHandle(thread)
    time.sleep(2.0)
    after_top = read_qword(pid, state + 0x80)
    print(f"called on tid {tid} · top {before_top:#x} -> {after_top:#x}")

    time.sleep(1.2)
    alive = bool(manualmap.find_pid())
    log = newest_log()
    added = (log.stat().st_size - before) if log else 0
    tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-4:] if log else []
    print(f"client alive: {alive} · log +{added} B")
    for line in tail:
        print("   ", line[:150])
    return 0 if alive else 2


if __name__ == "__main__":
    raise SystemExit(main())
