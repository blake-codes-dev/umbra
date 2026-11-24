#!/usr/bin/env python3
"""hijack.py - M1b: load the stub by borrowing an existing thread's RIP.

CreateRemoteThread returns a fixed status in this client (Hyperion intercepts it),
so instead: suspend a thread, point RIP at a 40-byte stub that calls LoadLibraryW
with our path and jumps back, then resume. No new thread is created.

    python tools/hijack.py --dll src/native/stub.dll
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import struct
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PIPE_NAME = (chr(92) * 2 + "." + chr(92) + "pipe" + chr(92) + "umbra_stub").encode("ascii")
THREAD_ALL = 0x1F03FF
CONTEXT_CONTROL_INTEGER = 0x00100003
RIP_OFFSET = 0xF8
FLAGS_OFFSET = 0x30
CONTEXT_SIZE = 1232

k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.VirtualAllocEx.restype = ctypes.c_void_p
k32.VirtualAllocEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD]
k32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.VirtualProtectEx.restype = wt.BOOL
k32.VirtualProtectEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD,
                                 ctypes.POINTER(wt.DWORD)]
k32.OpenThread.restype = wt.HANDLE
k32.OpenThread.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.GetThreadContext.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.SetThreadContext.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.GetModuleHandleW.restype = wt.HANDLE
k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
k32.GetProcAddress.restype = ctypes.c_void_p
k32.GetProcAddress.argtypes = [wt.HANDLE, ctypes.c_char_p]
k32.CreateNamedPipeA.restype = wt.HANDLE
k32.CreateNamedPipeA.argtypes = [ctypes.c_char_p, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
                                 wt.DWORD, wt.DWORD, ctypes.c_void_p]
k32.ConnectNamedPipe.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
                         ctypes.c_void_p]


def find_pid(name: str = "RobloxPlayerBeta.exe") -> int | None:
    for line in os.popen("tasklist /fo csv /nh").read().splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == name.lower():
            try:
                return int(parts[1])
            except ValueError:
                continue
    return None


def threads_of(pid: int) -> list[int]:
    class TE(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ThreadID", wt.DWORD),
                    ("th32OwnerProcessID", wt.DWORD), ("tpBasePri", ctypes.c_long),
                    ("tpDeltaPri", ctypes.c_long), ("dwFlags", wt.DWORD)]

    snap = k32.CreateToolhelp32Snapshot(0x00000004, 0)
    entry = TE()
    entry.dwSize = ctypes.sizeof(TE)
    found = []
    ok = k32.Thread32First(snap, ctypes.byref(entry))
    while ok:
        if entry.th32OwnerProcessID == pid:
            found.append(int(entry.th32ThreadID))
        ok = k32.Thread32Next(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return found


def build_stub(entry: int, param: int, resume: int) -> bytes:
    parts = [
        bytes([0x48, 0xB9]), struct.pack("<Q", param),
        bytes([0x48, 0xB8]), struct.pack("<Q", entry),
        bytes([0x49, 0x89, 0xE3]),
        bytes([0x48, 0x83, 0xE4, 0xF0]),
        bytes([0x48, 0x83, 0xEC, 0x20]),
        bytes([0xFF, 0xD0]),
        bytes([0x4C, 0x89, 0xDC]),
        bytes([0x48, 0xB8]), struct.pack("<Q", resume),
        bytes([0xFF, 0xE0]),
    ]
    return b"".join(parts)

def pipe_listener(timeout: float) -> dict:
    handle = k32.CreateNamedPipeA(PIPE_NAME, 1, 0, 1, 4096, 4096, 0, None)
    result: dict = {"line": None, "error": None}
    if not handle or handle == wt.HANDLE(-1).value:
        result["error"] = f"CreateNamedPipe {ctypes.get_last_error()}"
        return result

    def serve() -> None:
        if not k32.ConnectNamedPipe(handle, None) and ctypes.get_last_error() != 535:
            result["error"] = f"ConnectNamedPipe {ctypes.get_last_error()}"
            return
        buffer = ctypes.create_string_buffer(512)
        read = wt.DWORD(0)
        if k32.ReadFile(handle, buffer, 511, ctypes.byref(read), None):
            result["line"] = buffer.raw[: read.value].decode("utf-8", "replace").strip()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    thread.join(timeout)
    k32.CloseHandle(handle)
    return result


def inject_hijack(pid: int, dll: Path, timeout: float) -> dict:
    handle = k32.OpenProcess(0x1F0FFF, False, pid)
    if not handle:
        return {"error": f"OpenProcess {ctypes.get_last_error()}"}
    load = k32.GetProcAddress(k32.GetModuleHandleW("kernel32.dll"), b"LoadLibraryW")
    path = str(dll).encode("utf-16-le") + b"\0\0"
    path_addr = k32.VirtualAllocEx(handle, None, len(path), 0x3000, 0x04)
    written = ctypes.c_size_t(0)
    k32.WriteProcessMemory(handle, path_addr, path, len(path), ctypes.byref(written))

    candidates = threads_of(pid)
    if not candidates:
        return {"error": "no threads"}
    order = candidates[:1] + candidates[1:][::-1]
    last_error = None
    for tid in order[:6]:
        thread = k32.OpenThread(THREAD_ALL, False, tid)
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
        resume = struct.unpack_from("<Q", context, RIP_OFFSET)[0]
        stub = build_stub(load, path_addr, resume)
        stub_addr = k32.VirtualAllocEx(handle, None, len(stub), 0x3000, 0x04)
        k32.WriteProcessMemory(handle, stub_addr, stub, len(stub), ctypes.byref(written))
        old = wt.DWORD(0)
        k32.VirtualProtectEx(handle, stub_addr, len(stub), 0x20, ctypes.byref(old))
        struct.pack_into("<Q", context, RIP_OFFSET, stub_addr)
        ok = k32.SetThreadContext(thread, context)
        k32.ResumeThread(thread)
        k32.CloseHandle(thread)
        if not ok:
            last_error = f"SetThreadContext {ctypes.get_last_error()}"
            continue
        captured = pipe_listener(timeout)
        if captured.get("line"):
            return {"tid": tid, "resume": hex(resume), "stub": hex(stub_addr), **captured}
        last_error = f"no pipe message from tid {tid}"
    k32.CloseHandle(handle)
    return {"error": last_error or "failed"}


def alt_confirmed() -> bool:
    try:
        text = (ROOT / 'state' / 'inject' / 'ACCOUNT.md').read_text(encoding='utf-8')
        return 'status: alt' in text.lower()
    except OSError:
        return False


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--dll", default=str(ROOT / "src" / "native" / "stub.dll"))
    parser.add_argument("--wait", type=float, default=6.0)
    args = parser.parse_args(argv)
    if not alt_confirmed() and '--force' not in sys.argv:
        print({'note': 'main account: pass --force, or log in the alt'})
        return 3
    dll = Path(args.dll)
    pid = find_pid()
    if not pid or not dll.exists():
        print({"error": "no client or no dll", "pid": pid})
        return 1
    print(inject_hijack(pid, dll, args.wait))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
