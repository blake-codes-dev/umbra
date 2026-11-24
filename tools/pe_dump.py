#!/usr/bin/env python3
"""
pe_dump.py — read the Roblox client's PE images out of process memory.

The install directory is ACL-blocked, so the binary on disk cannot be read, but
the mapping in the running process can. This is M0's foundation: everything the
pattern scanner needs (section layout, .text bytes, strings) comes from here.

    python tools/pe_dump.py                 # dump every module larger than 8 MB
    python tools/pe_dump.py --min 1         # include smaller modules
    python tools/pe_dump.py --name RobloxPlayerBeta.dll

Output: dump/<module>-<size>.bin, dump/sections.json, dump/modules.json
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DUMP = ROOT / "dump"
PROCESS_NAME = "RobloxPlayerBeta.exe"

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
PROCESS_VM_READ = 0x0010
PROCESS_VM_OPERATION = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400


class MODULEENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("th32ModuleID", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("GlblcntUsage", wt.DWORD),
        ("ProccntUsage", wt.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wt.DWORD),
        ("hModule", wt.HMODULE),
        ("szModule", ctypes.c_char * 256),
        ("szExePath", ctypes.c_char * 260),
    ]


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


NAMES = ("robloxplayerbeta.exe", "windowsplayer.exe", "robloxplayerlauncher.exe")


def find_pid(names: tuple[str, ...] = NAMES) -> int | None:
    """Toolhelp process walk; the exe name is the only stable identifier here."""
    snapshot = k32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == -1:
        return None
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    try:
        ok = k32.Process32First(snapshot, ctypes.byref(entry))
        while ok:
            exe = entry.szExeFile.decode("mbcs", "replace").lower()
            if exe in names:
                return int(entry.th32ProcessID)
            ok = k32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    return None


def modules(pid: int) -> list[dict]:
    snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snapshot == -1:
        raise OSError("CreateToolhelp32Snapshot failed")
    entry = MODULEENTRY32()
    entry.dwSize = ctypes.sizeof(MODULEENTRY32)
    found = []
    try:
        ok = k32.Module32First(snapshot, ctypes.byref(entry))
        while ok:
            found.append({
                "name": entry.szModule.decode("mbcs", "replace"),
                "base": ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0,
                "size": int(entry.modBaseSize),
                "path": entry.szExePath.decode("mbcs", "replace"),
            })
            ok = k32.Module32Next(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    return found


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("PartitionId", wt.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100


def _read_region(handle, address: int, size: int, protect: int) -> bytes:
    buffer = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t(0)
    if k32.ReadProcessMemory(handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(read)):
        return buffer.raw[:read.value]
    relaxed = (protect & ~PAGE_GUARD) or 0x02
    if (relaxed & 0xFF) == PAGE_NOACCESS:
        relaxed = 0x02
    old = wt.DWORD(0)
    ignored = wt.DWORD(0)
    if not k32.VirtualProtectEx(handle, ctypes.c_void_p(address), size, relaxed,
                                ctypes.byref(old)):
        return bytes(size)
    try:
        if k32.ReadProcessMemory(handle, ctypes.c_void_p(address), buffer, size,
                                 ctypes.byref(read)):
            return buffer.raw[:read.value]
        return bytes(size)
    finally:
        k32.VirtualProtectEx(handle, ctypes.c_void_p(address), size, old.value,
                             ctypes.byref(ignored))


def read_image(handle, base: int, size: int):
    """Walk committed regions: page-level NOACCESS holes make chunked reads lie."""
    out = bytearray()
    address = base
    end = base + size
    lifted = 0
    holes = 0
    while address < end:
        mbi = MBI()
        got = k32.VirtualQueryEx(handle, ctypes.c_void_p(address), ctypes.byref(mbi),
                                 ctypes.sizeof(mbi))
        if not got:
            break
        region_base = mbi.BaseAddress or address
        region_size = mbi.RegionSize or 0x1000
        region_end = min(region_base + region_size, end)
        if region_base > address:
            out.extend(bytes(region_base - address))
        want = max(0, region_end - region_base)
        if want:
            data = _read_region(handle, region_base, want, mbi.Protect)
            if (mbi.Protect & 0xFF) == PAGE_NOACCESS:
                holes += 1
                lifted += 1
            elif mbi.Protect & PAGE_GUARD:
                lifted += 1
            out.extend(data)
        address = max(region_end, address + 0x1000)
    return bytes(out), {"lifted_regions": lifted, "noaccess_regions": holes, "bytes": len(out)}


def parse_pe(image: bytes) -> dict:
    if image[:2] != b"MZ":
        return {"error": "no MZ"}
    e_lfanew = int.from_bytes(image[0x3C:0x40], "little")
    if image[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        return {"error": "no PE header"}
    coff = e_lfanew + 4
    machine = int.from_bytes(image[coff:coff + 2], "little")
    count = int.from_bytes(image[coff + 2:coff + 4], "little")
    opt_size = int.from_bytes(image[coff + 16:coff + 18], "little")
    opt = coff + 20
    magic = int.from_bytes(image[opt:opt + 2], "little")
    image_base = int.from_bytes(image[opt + 24:opt + 32], "little") if magic == 0x20B else 0
    entry = int.from_bytes(image[opt + 16:opt + 20], "little")
    section_table = opt + opt_size
    sections = []
    for index in range(count):
        raw = section_table + index * 40
        name = image[raw:raw + 8].rstrip(b"\0").decode("ascii", "replace")
        vsize = int.from_bytes(image[raw + 8:raw + 12], "little")
        vaddr = int.from_bytes(image[raw + 12:raw + 16], "little")
        rawsize = int.from_bytes(image[raw + 16:raw + 20], "little")
        rawptr = int.from_bytes(image[raw + 20:raw + 24], "little")
        chars = int.from_bytes(image[raw + 36:raw + 40], "little")
        sections.append({"name": name, "va": vaddr, "vsize": vsize, "raw_ptr": rawptr,
                         "raw_size": rawsize, "characteristics": hex(chars),
                         "executable": bool(chars & 0x20000000)})
    return {"machine": hex(machine), "sections": sections, "image_base": hex(image_base),
            "entry_rva": hex(entry), "pe64": magic == 0x20B}


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    argv = argv if argv is not None else sys.argv[1:]
    minimum = 8 * 1024 * 1024
    only = None
    for index, arg in enumerate(argv):
        if arg == "--min" and index + 1 < len(argv):
            minimum = int(float(argv[index + 1]) * 1024 * 1024)
        if arg == "--name" and index + 1 < len(argv):
            only = argv[index + 1].lower()

    pid = find_pid()
    if not pid:
        print(f"{PROCESS_NAME} not running")
        return 1
    handle = k32.OpenProcess(
        PROCESS_VM_READ | PROCESS_VM_OPERATION | PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        print(f"OpenProcess failed: {ctypes.get_last_error()}")
        return 1

    DUMP.mkdir(parents=True, exist_ok=True)
    manifest = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pid": pid, "modules": []}
    try:
        for module in sorted(modules(pid), key=lambda m: -m["size"]):
            if module["size"] < minimum:
                continue
            if only and only not in module["name"].lower():
                continue
            started = time.monotonic()
            image, stats = read_image(handle, module["base"], module["size"])
            info = parse_pe(image)
            info["read"] = stats
            target = DUMP / f"{module['name']}-{module['size']}.bin"
            target.write_bytes(image)
            info.update({"name": module["name"], "size": module["size"],
                         "base": hex(module["base"]), "file": str(target),
                         "seconds": round(time.monotonic() - started, 2)})
            manifest["modules"].append(info)
            print(f"{module['name']:<28} {module['size'] / 2**20:7.1f} MB  "
                  f"{len(info.get('sections', []))} sections  {info['seconds']}s  -> {target.name}")
            for section in info.get("sections", []):
                print(f"    {section['name']:<10} va {section['va']:#010x} "
                      f"vsize {section['vsize']:>10,} raw {section['raw_size']:>10,}"
                      f"{'  RX' if section['executable'] else ''}")
    finally:
        k32.CloseHandle(handle)

    (DUMP / "modules.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"\nmanifest: {DUMP / 'modules.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
