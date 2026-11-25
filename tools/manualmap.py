#!/usr/bin/env python3
"""manualmap.py - map a DLL into a process without LoadLibrary.

Why: the client's loader is watched, and a LoadLibrary entry shows up in the
module list. Manual mapping writes the image into private memory, applies
relocations, resolves imports against already-loaded modules, and calls the
entry point from a borrowed thread. No loader entry, no new thread left behind.

    python tools/manualmap.py --spawn            # map into a throwaway python.exe
    python tools/manualmap.py --pid 1234         # map into a running process
    python tools/manualmap.py --pid 1234 --dll src/native/stub.dll

Import resolution: system DLLs (kernel32, ntdll, user32) are shared images at the
same base in every process, so a locally resolved address is valid remotely.
Imports from other DLLs are reported and left zero on purpose - a wrong address
is worse than a missing one.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

DEBUG = "-v" in sys.argv

PIPE_NAME = (chr(92) * 2 + "." + chr(92) + "pipe" + chr(92) + "lyphen_stub").encode("ascii")
CONTEXT_CONTROL_INTEGER = 0x00100003
RIP_OFFSET = 0xF8
FLAGS_OFFSET = 0x30
CONTEXT_SIZE = 1232
MEM_COMMIT_RESERVE = 0x3000
PAGE_READWRITE = 0x04
PAGE_RX = 0x20

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
k32.LoadLibraryW.restype = wt.HANDLE
k32.LoadLibraryW.argtypes = [ctypes.c_wchar_p]
k32.GetProcAddress.restype = ctypes.c_void_p
k32.GetProcAddress.argtypes = [wt.HANDLE, ctypes.c_char_p]
k32.CreateFileMappingA.restype = wt.HANDLE
k32.CreateFileMappingA.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, wt.DWORD,
                                   wt.DWORD, ctypes.c_char_p]
k32.MapViewOfFile.restype = ctypes.c_void_p
k32.MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
k32.CreateNamedPipeA.restype = wt.HANDLE
k32.CreateNamedPipeA.argtypes = [ctypes.c_char_p, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
                                 wt.DWORD, wt.DWORD, ctypes.c_void_p]
k32.ConnectNamedPipe.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
                         ctypes.c_void_p]


def parse_pe(path: Path) -> dict:
    image = bytearray(path.read_bytes())
    e = int.from_bytes(image[0x3C:0x40], "little")
    coff = e + 4
    count = int.from_bytes(image[coff + 2:coff + 4], "little")
    opt_size = int.from_bytes(image[coff + 16:coff + 18], "little")
    opt = coff + 20
    magic = int.from_bytes(image[opt:opt + 2], "little")
    image_base = int.from_bytes(image[opt + 24:opt + 32], "little") if magic == 0x20B else 0
    entry = int.from_bytes(image[opt + 16:opt + 20], "little")
    size_image = int.from_bytes(image[opt + 56:opt + 60], "little")
    table = opt + opt_size
    sections = []
    for index in range(count):
        raw = table + index * 40
        sections.append({
            "name": image[raw:raw + 8].rstrip(b"\0").decode("ascii", "replace"),
            "vsize": int.from_bytes(image[raw + 8:raw + 12], "little"),
            "va": int.from_bytes(image[raw + 12:raw + 16], "little"),
            "raw_size": int.from_bytes(image[raw + 16:raw + 20], "little"),
            "raw_ptr": int.from_bytes(image[raw + 20:raw + 24], "little"),
            "chars": int.from_bytes(image[raw + 36:raw + 40], "little"),
        })
    directories = {}
    for name, offset in (("reloc", 5), ("import", 1)):
        rva = int.from_bytes(image[opt + 112 + offset * 8:opt + 116 + offset * 8], "little")
        size = int.from_bytes(image[opt + 116 + offset * 8:opt + 120 + offset * 8], "little")
        directories[name] = (rva, size)
    return {"path": path, "image": image, "pe_offset": e, "opt": opt, "entry": entry,
            "image_base": image_base, "size_image": size_image or (sections[-1]["va"] + sections[-1]["vsize"]),
            "sections": sections, "directories": directories}


def build_local_image(pe: dict) -> bytearray:
    """Headers + sections at their virtual addresses, in one buffer."""
    out = bytearray(pe["size_image"])
    headers = min(pe["size_image"], pe["sections"][0]["raw_ptr"])
    out[:headers] = pe["image"][:headers]
    for section in pe["sections"]:
        start = section["va"]
        end = start + section["raw_size"]
        out[start:end] = pe["image"][section["raw_ptr"]:section["raw_ptr"] + section["raw_size"]]
    return out


def apply_relocations(image: bytearray, pe: dict, remote_base: int) -> int:
    if remote_base == pe["image_base"]:
        return 0
    rva, size = pe["directories"]["reloc"]
    if not rva:
        return 0
    delta = remote_base - pe["image_base"]
    applied = 0
    offset = rva
    end = rva + size
    while offset < end:
        block_va = int.from_bytes(image[offset:offset + 4], "little")
        block_size = int.from_bytes(image[offset + 4:offset + 8], "little")
        if block_size == 0:
            break
        entries = (block_size - 8) // 2
        for index in range(entries):
            entry = int.from_bytes(image[offset + 8 + index * 2:offset + 10 + index * 2],
                                   "little")
            kind = entry >> 12
            target = block_va + (entry & 0x0FFF)
            if kind == 10:  # IMAGE_REL_BASED_DIR64
                value = int.from_bytes(image[target:target + 8], "little")
                image[target:target + 8] = ((value + delta) & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")
                applied += 1
        offset += block_size
    return applied


def resolve_imports(image: bytearray, pe: dict) -> dict:
    rva, _size = pe["directories"]["import"]
    resolved, unresolved, modules = 0, [], {}
    if not rva:
        return {"resolved": 0, "unresolved": unresolved}
    offset = rva
    while True:
        original = int.from_bytes(image[offset:offset + 4], "little")
        name_rva = int.from_bytes(image[offset + 12:offset + 16], "little")
        first = int.from_bytes(image[offset + 16:offset + 20], "little")
        if name_rva == 0 and first == 0:
            break
        name_end = image.find(b"\0", name_rva)
        dll = image[name_rva:name_end].decode("ascii", "replace")
        module = k32.LoadLibraryW(dll)
        modules[dll] = bool(module)
        thunk = original or first
        target = first
        while True:
            value = int.from_bytes(image[thunk:thunk + 8], "little")
            if value == 0:
                break
            if value & (1 << 63):
                ordinal = value & 0xFFFF
                address = k32.GetProcAddress(module, ctypes.cast(ordinal, ctypes.c_char_p)) if module else 0
                label = f"#{ordinal}"
            else:
                entry_rva = value + 2
                entry_end = image.find(b"\0", entry_rva)
                symbol = image[entry_rva:entry_end].decode("ascii", "replace")
                address = k32.GetProcAddress(module, symbol.encode()) if module else 0
                label = symbol
            if address:
                image[target:target + 8] = int(address).to_bytes(8, "little")
                resolved += 1
            else:
                unresolved.append(f"{dll}!{label}")
            thunk += 8
            target += 8
        offset += 20
    return {"resolved": resolved, "unresolved": unresolved, "modules": modules}


SYSTEM_MODULES = ("ntdll", "kernel32", "kernelbase", "win32u", "user32", "gdi32", "msvcrt",
                  "combase", "rpcrt4", "kernel.appcore", "ntdll")


ACCESS_TRIES = (0x1F0FFF, 0x0438, 0x0430, 0x0410)   # 0x438 = vm_op|vm_read|vm_write|query


def open_process(pid: int) -> tuple:
    """Hyperion denies PROCESS_ALL_ACCESS on the client; fall back to the least rights."""
    last = 0
    for rights in ACCESS_TRIES:
        handle = k32.OpenProcess(rights, False, pid)
        if handle:
            return handle, rights
        last = ctypes.get_last_error()
    return None, last


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
    found = 0
    ok = k32.Process32First(snap, ctypes.byref(entry))
    while ok:
        if entry.szExeFile.decode("ascii", "replace").lower() == name.lower():
            found = entry.th32ProcessID
        ok = k32.Process32Next(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return found


def module_ranges(pid: int) -> list[tuple[int, int, str]]:
    class ME(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("GlblcntUsage", wt.DWORD), ("ProccntUsage", wt.DWORD),
                    ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wt.DWORD),
                    ("hModule", wt.HANDLE), ("szModule", ctypes.c_char * 256),
                    ("szExePath", ctypes.c_char * 260)]

    snap = k32.CreateToolhelp32Snapshot(0x00000008, pid)
    entry = ME()
    entry.dwSize = ctypes.sizeof(ME)
    out = []
    ok = k32.Module32First(snap, ctypes.byref(entry))
    while ok:
        base = entry.modBaseAddr or 0
        out.append((base, base + entry.modBaseSize, entry.szModule.decode("ascii", "replace")))
        ok = k32.Module32Next(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return out


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


BUS = ROOT / "state" / "inject" / "bus.bin"


def bus_prepare() -> Path:
    """A plain file, not a named section.

    Sections from CreateFileMapping(INVALID_HANDLE_VALUE) kept giving the stub a
    copy-on-write view: the stub could read the host's writes but the host never
    saw the stub's. A file-backed mapping shares properly and survives a crash,
    so it is also easier to inspect after the fact.
    """
    BUS.parent.mkdir(parents=True, exist_ok=True)
    BUS.write_bytes(b"\0" * 4096)
    return BUS


def bus_read() -> str | None:
    try:
        raw = BUS.read_bytes()[:128]
    except OSError as exc:
        print(f"  bus_read OSError {exc}", file=sys.stderr)
        return None
    if raw[:4] != b"UMBR":
        return None
    return raw[8:].split(b"\0")[0].decode("utf-8", "replace")


def _hijack(handle: int, tid: int, pid: int, entry: int, base: int, slot: int = 0) -> dict:
    """Point one thread at a stub that calls entry(base, 1, 0) and returns."""
    thread = k32.OpenThread(0x1F03FF, False, tid)
    if not thread:
        return {"error": "OpenThread"}
    try:
        if k32.SuspendThread(thread) == 0xFFFFFFFF:
            return {"error": "SuspendThread"}
        context = ctypes.create_string_buffer(CONTEXT_SIZE)
        struct.pack_into("<I", context, FLAGS_OFFSET, CONTEXT_CONTROL_INTEGER)
        if not k32.GetThreadContext(thread, context):
            return {"error": "GetThreadContext"}
        resume = struct.unpack_from("<Q", context, RIP_OFFSET)[0]
        # The hijacked thread is in the middle of real work, so every register the
        # stub touches is saved on its own stack and restored before it returns.
        # r12 holds the frame pointer because it survives calls by ABI.
        parts = [
            bytes([0x9C]),                                          # pushfq
            bytes([0x50]),                                          # push rax
            bytes([0x51]),                                          # push rcx
            bytes([0x52]),                                          # push rdx
            bytes([0x41, 0x50]),                                    # push r8
            bytes([0x41, 0x51]),                                    # push r9
            bytes([0x41, 0x52]),                                    # push r10
            bytes([0x41, 0x53]),                                    # push r11
            bytes([0x41, 0x54]),                                    # push r12
            bytes([0x49, 0x89, 0xE4]),                              # mov r12, rsp
            bytes([0x48, 0x83, 0xE4, 0xF0]),                        # and rsp, -16
            bytes([0x48, 0x83, 0xEC, 0x20]),                        # sub rsp, 0x20 (shadow)
            bytes([0x48, 0xB9]), struct.pack("<Q", base),           # mov rcx, base
            bytes([0xBA]), struct.pack("<I", 1),                    # mov edx, DLL_PROCESS_ATTACH
            bytes([0x45, 0x31, 0xC0]),                              # xor r8d, r8d
            bytes([0x48, 0xB8]), struct.pack("<Q", entry),          # mov rax, entry
            bytes([0xFF, 0xD0]),                                    # call rax
            bytes([0x4C, 0x89, 0xE4]),                              # mov rsp, r12
            bytes([0x41, 0x5C]),                                    # pop r12
            bytes([0x41, 0x5B]),                                    # pop r11
            bytes([0x41, 0x5A]),                                    # pop r10
            bytes([0x41, 0x59]),                                    # pop r9
            bytes([0x41, 0x58]),                                    # pop r8
            bytes([0x5A]),                                          # pop rdx
            bytes([0x59]),                                          # pop rcx
            bytes([0x58]),                                          # pop rax
            bytes([0x9D]),                                          # popfq
            bytes([0x48, 0xB8]), struct.pack("<Q", resume),         # mov rax, resume
            bytes([0xFF, 0xE0]),                                    # jmp rax
        ]
        stub = b"".join(parts)
        if slot:
            stub_addr = slot
        else:
            stub_addr = k32.VirtualAllocEx(handle, None, 0x1000, MEM_COMMIT_RESERVE,
                                           PAGE_READWRITE)
            if not stub_addr:
                return {"error": "VirtualAllocEx"}
        written = ctypes.c_size_t(0)
        k32.WriteProcessMemory(handle, stub_addr, stub, len(stub), ctypes.byref(written))
        page = stub_addr & ~0xFFF
        old = wt.DWORD(0)
        if not k32.VirtualProtectEx(handle, ctypes.c_void_p(page), 0x1000, PAGE_RX,
                                    ctypes.byref(old)):
            return {"error": f"VirtualProtectEx {ctypes.get_last_error()}"}
        after = wt.DWORD(0)
        k32.VirtualProtectEx(handle, ctypes.c_void_p(page), 0x1000, PAGE_RX, ctypes.byref(after))
        if after.value & 0xFF != PAGE_RX:
            return {"error": f"stub page not executable ({after.value:#x})"}
        struct.pack_into("<Q", context, RIP_OFFSET, stub_addr)
        if not k32.SetThreadContext(thread, context):
            return {"error": "SetThreadContext"}
        return {"tid": tid, "stub": stub_addr, "resume": resume, "thread": thread}
    finally:
        if "thread" in locals() and thread and not locals().get("_keep"):
            pass


def call_entry(pid: int, entry: int, base: int, timeout: float, data_va: int = 0x3000,
               slot: int = 0) -> dict:
    handle, rights = open_process(pid)
    if not handle:
        return {"error": f"OpenProcess {rights}"}
    # A thread parked in Sleep/Wait does not take on a patched RIP until it wakes,
    # so threads executing user code are tried first, and a candidate that makes no
    # progress within PROBE seconds is abandoned in favour of the next one.
    ranges = module_ranges(pid)

    def quality(tid: int) -> int:
        th = k32.OpenThread(0x1F03FF, False, tid)
        if not th:
            return 9
        ctx = ctypes.create_string_buffer(CONTEXT_SIZE)
        struct.pack_into("<I", ctx, FLAGS_OFFSET, CONTEXT_CONTROL_INTEGER)
        score = 5
        if k32.SuspendThread(th) != 0xFFFFFFFF:
            if k32.GetThreadContext(th, ctx):
                rip = struct.unpack_from("<Q", ctx, RIP_OFFSET)[0]
                hit = next((name.lower() for lo, hi, name in ranges if lo <= rip < hi), "")
                score = 0 if hit and not hit.startswith(SYSTEM_MODULES) else (1 if hit else 4)
            k32.ResumeThread(th)
        k32.CloseHandle(th)
        return score

    candidates = sorted(threads_of(pid), key=quality)
    deadlines = time.monotonic() + timeout
    bus_prepare()
    tried = []
    for tid in candidates[:4]:
        attempt = _hijack(handle, tid, pid, entry, base, slot)
        if "error" in attempt:
            tried.append(f"{tid}:{attempt['error']}")
            continue
        thread = attempt["thread"]
        # a thread can be suspended more than once; drain the count or it never runs
        for _ in range(8):
            if k32.ResumeThread(thread) <= 0:
                break
        k32.CloseHandle(thread)
        probe_until = min(deadlines, time.monotonic() + 0.6)
        while time.monotonic() < probe_until:
            got = bus_read()
            if got:
                return {"tid": tid, "bus": got, "stub": hex(attempt["stub"]), "stub_protect": "0x20",
                        "threads_tried": len(tried) + 1}
            time.sleep(0.03)
        tried.append(f"{tid}:no-run")
    got = bus_read()
    if got:
        return {"tid": candidates[0], "bus": got, "threads_tried": len(tried)}
    return {"bus": None, "threads_tried": len(tried), "tried": tried[:4],
            "note": "stub planted but no thread ran it"}


def start_pipe() -> dict:
    handle = k32.CreateNamedPipeA(PIPE_NAME, 3, 0, 1, 4096, 4096, 0, None)  # 3 = duplex
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
    return {"handle": handle, "result": result, "thread": thread}


def finish_pipe(pipe: dict, timeout: float) -> dict:
    pipe["thread"].join(timeout)
    k32.CloseHandle(pipe["handle"])
    return pipe["result"]


def spawn_target(seconds: int = 60) -> int:
    # spinning, not sleeping: a waiting thread keeps its patched RIP parked until it wakes
    code = "import time; end = time.time() + %d\nwhile time.time() < end: pass" % seconds
    proc = subprocess.Popen([sys.executable, "-c", code])
    time.sleep(0.7)
    return proc.pid


def map_into(pid: int, dll: Path, timeout: float = 6.0) -> dict:
    pe = parse_pe(dll)
    image = build_local_image(pe)
    handle, rights = open_process(pid)
    if not handle:
        return {"error": f"OpenProcess {rights}"}
    # one extra page after the image holds the entry thunk, so a run leaves a single
    # private executable region instead of two
    span = ((pe["size_image"] + 0xFFF) & ~0xFFF) + 0x1000
    base = k32.VirtualAllocEx(handle, None, span, MEM_COMMIT_RESERVE, PAGE_READWRITE)
    if not base:
        return {"error": f"VirtualAllocEx {ctypes.get_last_error()}"}
    relocs = apply_relocations(image, pe, base)
    imports = resolve_imports(image, pe)
    written = ctypes.c_size_t(0)
    ok = k32.WriteProcessMemory(handle, base, bytes(image), pe["size_image"], ctypes.byref(written))
    if not ok:
        return {"error": f"WriteProcessMemory {ctypes.get_last_error()}"}
    # protect sections: executable ones RX, the rest RW
    for section in pe["sections"]:
        protect = PAGE_RX if section["chars"] & 0x20000000 else 0x04
        old = wt.DWORD(0)
        k32.VirtualProtectEx(handle, ctypes.c_void_p(base + section["va"]),
                             max(section["vsize"], section["raw_size"]), protect, ctypes.byref(old))
    entry = base + pe["entry"]
    data_va = next((sec["va"] for sec in pe["sections"] if sec["name"] == ".data"), 0x3000)
    slot = base + ((pe["size_image"] + 0xFFF) & ~0xFFF)
    old = wt.DWORD(0)
    k32.VirtualProtectEx(handle, ctypes.c_void_p(slot), 0x1000, PAGE_RX, ctypes.byref(old))
    print(f"  access {rights:#x}", file=sys.stderr) if DEBUG else None
    call = call_entry(pid, entry, base, timeout, data_va, slot)
    return {"pid": pid, "base": hex(base), "size": pe["size_image"], "relocations": relocs,
            "imports": imports["resolved"], "unresolved": imports["unresolved"][:6],
            **call}


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--dll", default=str(ROOT / "src" / "native" / "stub.dll"))
    parser.add_argument("--pid", type=int, default=0)
    parser.add_argument("--spawn", action="store_true")
    parser.add_argument("--wait", type=float, default=6.0)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    global DEBUG
    DEBUG = DEBUG or args.verbose
    dll = Path(args.dll)
    if not dll.exists():
        print({"error": f"missing {dll}"})
        return 1
    pid = args.pid
    if not pid and not args.spawn:
        pid = find_pid()
        print(f"target pid {pid}", file=sys.stderr)
    if args.spawn or not pid:
        pid = spawn_target()
    print(map_into(pid, dll, args.wait))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
