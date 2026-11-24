"""Capture the live lua_State from thread registers parked in Luau's interpreter.

Sample every thread repeatedly; any thread whose RIP is inside one of the known
interpreter copies holds `lua_State*` in RBX (copy A) or RDI (copy B).
Validation reads L->global at +0x68 and checks it is a pointer, then reads
L+0x80 (top) and L+0x78 (stack) and requires stack <= top.

    python tools/luacapture.py [--pid N] [--seconds 3] [--dump]
"""
from __future__ import annotations

import argparse
import collections
import ctypes
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manualmap
import sigcheck

RIP_OFFSET = 0xF8
FLAGS_OFFSET = 0x30
CONTEXT_SIZE = 1232
REG_OFF = {"rax": 0x78, "rcx": 0x80, "rdx": 0x88, "rbx": 0x90, "rsp": 0x98,
           "rbp": 0xA0, "rsi": 0xA8, "rdi": 0xB0, "r8": 0xB8, "r9": 0xC0,
           "r10": 0xC8, "r11": 0xD0, "r12": 0xD8, "r13": 0xE0, "r14": 0xE8, "r15": 0xF0}


def read(handle, address, n):
    buf = ctypes.create_string_buffer(max(n, 1))
    got = ctypes.c_size_t(0)
    if not manualmap.k32.ReadProcessMemory(handle, ctypes.c_void_p(address), buf, n,
                                           ctypes.byref(got)):
        return None
    return buf.raw[:got.value]


def q(handle, address):
    d = read(handle, address, 8)
    return struct.unpack("<Q", d)[0] if d and len(d) == 8 else None


def snapshots(pid):
    """One pass: list of (tid, rip, regs) for every thread."""
    out = []
    for tid in manualmap.threads_of(pid):
        thread = manualmap.k32.OpenThread(0x1F03FF, False, tid)
        if not thread:
            continue
        if manualmap.k32.SuspendThread(thread) == 0xFFFFFFFF:
            manualmap.k32.CloseHandle(thread)
            continue
        ctx = ctypes.create_string_buffer(CONTEXT_SIZE)
        struct.pack_into("<I", ctx, FLAGS_OFFSET, 0x00100007)
        if manualmap.k32.GetThreadContext(thread, ctx):
            rip = struct.unpack_from("<Q", ctx, RIP_OFFSET)[0]
            regs = {n: struct.unpack_from("<Q", ctx, o)[0] for n, o in REG_OFF.items()}
            out.append((tid, rip, regs))
        manualmap.k32.ResumeThread(thread)
        manualmap.k32.CloseHandle(thread)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--dump", action="store_true")
    args = ap.parse_args()
    pid = args.pid or sigcheck.find_pid()
    if not pid:
        print("client not running")
        return 1
    handle, rights = manualmap.open_process(pid)
    if not handle:
        print("OpenProcess failed", rights)
        return 1
    base = sigcheck.remote_module_base(pid, "RobloxPlayerBeta.exe")
    # three interpreter copies: entry RVAs located by scanning for the shared prologue
    funcs = [(0x796980, 0x375bd70), (0x375bd70, 0x410b0cd + 0x1000), (0x5794150, 0x58bc29d + 0x1000)]
    ranges = [(base + a, base + b) for a, b in funcs]
    hits = []
    seen_rips = collections.Counter()
    deadline = time.time() + args.seconds
    passes = 0
    while time.time() < deadline:
        passes += 1
        for tid, rip, regs in snapshots(pid):
            rva = rip - base
            if rva < 0 or rva >= 200 * 1024 * 1024:
                continue
            idx = next((i for i, (lo, hi) in enumerate(ranges) if lo <= rip < hi), None)
            if idx is None:
                seen_rips[rip] += 1
                continue
            reg = "rbx" if idx != 1 else "rdi"
            L = regs[reg]
            g = q(handle, L + 0x68)
            if not g:
                continue
            hits.append((tid, rip, idx, reg, L, g, regs.get("rcx", 0)))
    print(f"{passes} passes over the thread list")
    print(f"{len(hits)} interpreter hit(s)")
    uniq = {}
    for tid, rip, idx, reg, L, g, rcx in hits:
        uniq.setdefault(L, (tid, rip, idx, reg, g, rcx))
    print(f"{len(uniq)} distinct lua_State value(s)")
    for L, (tid, rip, idx, reg, g, rcx) in list(uniq.items())[:12]:
        top = q(handle, L + 0x80)
        stack = q(handle, L + 0x78)
        ok = top is not None and stack is not None and stack <= top
        print(f"  L={L:#x} via {reg} tid {tid} copy {idx} rip {rip:#x} "
              f"g={g:#x} stack={stack if stack is None else hex(stack)} "
              f"top={top if top is None else hex(top)} {'OK' if ok else 'BAD'}")
    if args.dump and uniq:
        L = next(iter(uniq))
        print(f"--- header of L {L:#x} (and its global {uniq[L][4]:#x})")
        for label, addr in (("L", L), ("G", uniq[L][4])):
            d = read(handle, addr, 0x90)
            for off in range(0, 0x90, 16):
                c = d[off:off + 16]
                print(f"  {label}+{off:#04x} " + " ".join(
                    f"{int.from_bytes(c[i:i+8], 'little'):#018x}" for i in range(0, 16, 8)))
    return 0 if uniq else 3


if __name__ == "__main__":
    raise SystemExit(main())
