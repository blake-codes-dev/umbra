#!/usr/bin/env python3
"""injector.py - M1 host: put the stub inside the client and hear it over a pipe.

    python tools/injector.py                    # loadlib mode, default stub
    python tools/injector.py --dll path.dll     # another build

The stub writes one line on attach; the host prints it. `--mode manual` comes
next (no loader entry, no new thread left behind).
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PIPE_NAME = b"\\\\.\\pipe\\lyphen_stub"
PROCESS_ALL = 0x1F0FFF
MEM_COMMIT_RESERVE = 0x3000
PAGE_READWRITE = 0x04
ERROR_PIPE_CONNECTED = 535

k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.VirtualAllocEx.restype = ctypes.c_void_p
k32.VirtualAllocEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD]
k32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.CreateRemoteThread.restype = wt.HANDLE
k32.CreateRemoteThread.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]
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
k32.GetExitCodeThread.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]


def find_pid(name: str = "RobloxPlayerBeta.exe") -> int | None:
    for line in os.popen("tasklist /fo csv /nh").read().splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == name.lower():
            try:
                return int(parts[1])
            except ValueError:
                continue
    return None


def pipe_listener(timeout: float) -> dict:
    handle = k32.CreateNamedPipeA(PIPE_NAME, 0x00000001, 0, 1, 4096, 4096, 0, None)
    result: dict = {"line": None, "error": None}
    if not handle or handle == wt.HANDLE(-1).value:
        result["error"] = f"CreateNamedPipe {ctypes.get_last_error()}"
        return result

    def serve() -> None:
        connected = k32.ConnectNamedPipe(handle, None)
        if not connected and ctypes.get_last_error() != ERROR_PIPE_CONNECTED:
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


def inject_loadlib(pid: int, dll: Path) -> dict:
    handle = k32.OpenProcess(PROCESS_ALL, False, pid)
    if not handle:
        return {"error": f"OpenProcess {ctypes.get_last_error()}"}
    data = str(dll).encode("utf-16-le") + b"\0\0"
    size = len(data)
    address = k32.VirtualAllocEx(handle, None, size, MEM_COMMIT_RESERVE, PAGE_READWRITE)
    if not address:
        k32.CloseHandle(handle)
        return {"error": f"VirtualAllocEx {ctypes.get_last_error()}"}
    written = ctypes.c_size_t(0)
    if not k32.WriteProcessMemory(handle, address, data, size, ctypes.byref(written)):
        k32.CloseHandle(handle)
        return {"error": f"WriteProcessMemory {ctypes.get_last_error()}"}
    kernel32 = k32.GetModuleHandleW("kernel32.dll")
    load = k32.GetProcAddress(kernel32, b"LoadLibraryW")
    thread = k32.CreateRemoteThread(handle, None, 0, load, address, 0, None)
    if not thread:
        k32.CloseHandle(handle)
        return {"error": f"CreateRemoteThread {ctypes.get_last_error()}"}
    k32.WaitForSingleObject(thread, 10000)
    code = wt.DWORD(0)
    k32.GetExitCodeThread(thread, ctypes.byref(code))
    k32.CloseHandle(thread)
    k32.CloseHandle(handle)
    return {"module": hex(code.value), "ok": bool(code.value)}


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
    parser.add_argument("--mode", default="loadlib", choices=["loadlib"])
    parser.add_argument("--wait", type=float, default=12.0)
    args = parser.parse_args(argv)
    if not alt_confirmed() and '--force' not in sys.argv:
        print({'note': 'main account: pass --force, or log in the alt'})
        return 3

    dll = Path(args.dll)
    if not dll.exists():
        print(f"missing {dll}")
        return 1
    pid = find_pid()
    if not pid:
        print("client not running")
        return 1
    captured: dict = {}
    thread = threading.Thread(target=lambda: captured.update(pipe_listener(args.wait)), daemon=True)
    thread.start()
    time.sleep(0.3)
    result = inject_loadlib(pid, dll)
    thread.join(args.wait + 3)
    print({"pid": pid, "dll": dll.name, "size": dll.stat().st_size, **result, "pipe": captured})
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
