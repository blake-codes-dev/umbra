"""Check the signature set against a live process, chunked and read-only.

Static uniqueness in a dump is only half the claim; the pattern has to hit once
in the real module too, at the expected RVA. Reports per-signature hit counts and
the VA of the single hit.

  python tools/sigcheck.py            # RobloxPlayerBeta.exe, patterns.json
  python tools/sigcheck.py --pid 1234
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pe_dump  # noqa: E402  (re-use the protection-lifting reader)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
TEXT_VA = 0x1000
TEXT_LEN = 105_075_420
CHUNK = 8 * 1024 * 1024
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]


def find_pid(name: str = "RobloxPlayerBeta.exe") -> int:
    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
    entry = PE()
    entry.dwSize = ctypes.sizeof(PE)
    hit = 0
    ok = k32.Process32First(snap, ctypes.byref(entry))
    while ok:
        if entry.szExeFile.decode("ascii", "replace").lower() == name.lower():
            hit = entry.th32ProcessID
        ok = k32.Process32Next(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return hit


def module_base(handle: int, name: str) -> int:
    class ME(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("GlblcntUsage", wt.DWORD), ("ProccntUsage", wt.DWORD),
                    ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wt.DWORD),
                    ("hModule", wt.HANDLE), ("szModule", ctypes.c_char * 256),
                    ("szExePath", ctypes.c_char * 260)]

    snap = k32.CreateToolhelp32Snapshot(0x00000008, ctypes.windll.kernel32.GetCurrentProcessId())
    entry = ME()
    entry.dwSize = ctypes.sizeof(ME)
    base = 0
    pid = ctypes.c_ulong()
    k32.GetWindowThreadProcessId  # noqa: B018  (keep import surface obvious)
    ok = k32.Module32First(snap, ctypes.byref(entry))
    while ok:
        if entry.szModule.decode("ascii", "replace").lower() == name.lower():
            base = entry.modBaseAddr or 0
        ok = k32.Module32Next(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return base or 0


def remote_module_base(pid: int, name: str) -> int:
    class ME(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("GlblcntUsage", wt.DWORD), ("ProccntUsage", wt.DWORD),
                    ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wt.DWORD),
                    ("hModule", wt.HANDLE), ("szModule", ctypes.c_char * 256),
                    ("szExePath", ctypes.c_char * 260)]

    snap = k32.CreateToolhelp32Snapshot(0x00000008, pid)
    entry = ME()
    entry.dwSize = ctypes.sizeof(ME)
    base = 0
    ok = k32.Module32First(snap, ctypes.byref(entry))
    while ok:
        if entry.szModule.decode("ascii", "replace").lower() == name.lower():
            base = entry.modBaseAddr or 0
        ok = k32.Module32Next(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return base or 0


def scan(handle: int, base: int, sig: bytes) -> list[int]:
    out: list[int] = []
    overlap = len(sig) - 1
    offset = 0
    buffer = ctypes.create_string_buffer(CHUNK + overlap)
    while offset < TEXT_LEN:
        want = min(CHUNK + overlap, TEXT_LEN - offset)
        data = pe_dump._read_region(handle, base + TEXT_VA + offset, want, 0x20)
        if not data or not any(data):
            offset += CHUNK
            continue
        start = 0
        while True:
            at = data.find(sig, start)
            if at < 0:
                break
            if offset + at < TEXT_LEN:
                out.append(base + TEXT_VA + offset + at)
            start = at + 1
        offset += CHUNK
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--patterns", default=str(ROOT / "patterns.json"))
    args = ap.parse_args()
    pid = args.pid or find_pid()
    if not pid:
        print("client not running")
        return 1
    patterns = json.loads(Path(args.patterns).read_text(encoding="utf-8"))["patterns"]
    handle = k32.OpenProcess(0x0438, False, pid)
    if not handle:
        print(f"OpenProcess {ctypes.get_last_error()}")
        return 1
    base = remote_module_base(pid, "RobloxPlayerBeta.exe")
    print(f"pid {pid} image base {base:#x}")
    bad = 0
    for entry in patterns:
        sig_text = entry.get("sig")
        if not sig_text:
            continue
        hits = scan(handle, base, bytes.fromhex(sig_text.replace(" ", "")))
        want = int(entry["func_va"], 16) if entry.get("func_va") else 0
        status = "ok" if len(hits) == 1 else ("MISSING" if not hits else f"DUPLICATE x{len(hits)}")
        at = hits[0] - base if hits else 0
        match = "at-expected" if hits and at == want else "off"
        if status != "ok" or match != "at-expected":
            bad += 1
        print(f"  {status:11} {entry['name']:<24} hits={len(hits)} rva={at:#x} want={want:#x} {match}")
    print(f"{len([p for p in patterns if p.get('sig')]) - bad} signature(s) verified live")
    return 0 if not bad else 2


if __name__ == "__main__":
    raise SystemExit(main())
