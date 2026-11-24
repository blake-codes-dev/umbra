#!/usr/bin/env python3
"""
footprint_audit.py — measure what an anti-cheat can see in the Roblox client.

Enumerates, for a live RobloxPlayerBeta.exe process, the same surfaces a
client-side anti-tamper system reads:

  modules      PEB loader list (what a PE loader knows about)
  threads      every thread + its start address, resolved to a module
  regions      VirtualQueryEx: type / protection / private-executable pages
  handles      every process handle pointing at the client, and its rights
  windows      top-level windows owned by the client
  objects      named pipes (object-directory leftovers)

Usage
  python tools/footprint_audit.py                     human report
  python tools/footprint_audit.py --json out.json     machine record
  python tools/footprint_audit.py --save base.json    record a baseline
  python tools/footprint_audit.py --diff base.json    report the delta since baseline
  python tools/footprint_audit.py --watch 8           sample for 8s, keep transient artifacts

Exit code is 0 unless --strict and a high-severity finding exists, so it can
gate the injection harness: inject -> audit --diff baseline -> ship or fail.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import time
from ctypes import wintypes as w

if ctypes.sizeof(ctypes.c_void_p) != 8:
    sys.exit("needs 64-bit Python (target client is x64)")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
user32 = ctypes.WinDLL("user32", use_last_error=True)

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPTHREAD = 0x00000004
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

THREAD_QUERY_INFORMATION = 0x0040
THREAD_QUERY_LIMITED_INFORMATION = 0x0800
ThreadQuerySetWin32StartAddress = 9

STILL_ACTIVE = 259

MEM_COMMIT = 0x1000
MEM_IMAGE = 0x1000000
MEM_MAPPED = 0x40000
MEM_PRIVATE = 0x20000

PAGE_GUARD = 0x100
PAGE_EXECUTE = 0x10
PAGE_EXECUTE_READ = 0x20
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80
EXEC_MASK = PAGE_EXECUTE | PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY

SystemExtendedHandleInformation = 64

TYPE_NAMES = {
    0: "free",
    MEM_PRIVATE: "private",
    MEM_MAPPED: "mapped",
    MEM_IMAGE: "image",
}

PROCESS_DUP_HANDLE = 0x0040
DUPLICATE_SAME_ACCESS = 0x0002

PROCESS_RIGHTS = [
    (0x0001, "TERMINATE"),
    (0x0002, "CREATE_THREAD"),
    (0x0008, "VM_OPERATION"),
    (0x0010, "VM_READ"),
    (0x0020, "VM_WRITE"),
    (0x0040, "DUP_HANDLE"),
    (0x0100, "SET_QUOTA"),
    (0x0200, "SET_INFO"),
    (0x0400, "QUERY_INFO"),
    (0x0800, "SUSPEND_RESUME"),
    (0x1000, "QUERY_LIMITED"),
    (0x2000, "SET_LIMITED"),
    (0x00100000, "SYNCHRONIZE"),
]


# --------------------------------------------------------------------------
# structs
# --------------------------------------------------------------------------


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD),
        ("cntUsage", w.DWORD),
        ("th32ProcessID", w.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", w.DWORD),
        ("cntThreads", w.DWORD),
        ("th32ParentProcessID", w.DWORD),
        ("pcPriClassBase", w.LONG),
        ("dwFlags", w.DWORD),
        ("szExeFile", w.WCHAR * 260),
    ]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD),
        ("cntUsage", w.DWORD),
        ("th32ThreadID", w.DWORD),
        ("th32OwnerProcessID", w.DWORD),
        ("tpBasePri", w.LONG),
        ("tpDeltaPri", w.LONG),
        ("dwFlags", w.DWORD),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD),
        ("th32ModuleID", w.DWORD),
        ("th32ProcessID", w.DWORD),
        ("GlblcntUsage", w.DWORD),
        ("ProccntUsage", w.DWORD),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", w.DWORD),
        ("hModule", ctypes.c_void_p),
        ("szModule", w.WCHAR * 256),
        ("szExePath", w.WCHAR * 260),
    ]


class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", w.DWORD),
        ("__alignment1", w.DWORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", w.DWORD),
        ("Protect", w.DWORD),
        ("Type", w.DWORD),
        ("__alignment2", w.DWORD),
    ]


class SYSTEM_HANDLE_ENTRY_EX(ctypes.Structure):
    _fields_ = [
        ("Object", ctypes.c_void_p),
        ("UniqueProcessId", ctypes.c_size_t),
        ("HandleValue", ctypes.c_size_t),
        ("GrantedAccess", w.DWORD),
        ("CreatorBackTraceIndex", w.WORD),
        ("ObjectTypeIndex", w.WORD),
        ("HandleAttributes", w.DWORD),
        ("Reserved", w.DWORD),
    ]


# --------------------------------------------------------------------------
# prototypes
# --------------------------------------------------------------------------

k32.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
k32.CreateToolhelp32Snapshot.restype = w.HANDLE

k32.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
k32.Process32FirstW.restype = w.BOOL
k32.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
k32.Process32NextW.restype = w.BOOL

k32.Thread32First.argtypes = [w.HANDLE, ctypes.POINTER(THREADENTRY32)]
k32.Thread32First.restype = w.BOOL
k32.Thread32Next.argtypes = [w.HANDLE, ctypes.POINTER(THREADENTRY32)]
k32.Thread32Next.restype = w.BOOL

k32.Module32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
k32.Module32FirstW.restype = w.BOOL
k32.Module32NextW.argtypes = [w.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
k32.Module32NextW.restype = w.BOOL

k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
k32.OpenProcess.restype = w.HANDLE
k32.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
k32.OpenThread.restype = w.HANDLE
k32.CloseHandle.argtypes = [w.HANDLE]
k32.CloseHandle.restype = w.BOOL

k32.VirtualQueryEx.argtypes = [
    w.HANDLE,
    ctypes.c_void_p,
    ctypes.POINTER(MEMORY_BASIC_INFORMATION64),
    ctypes.c_size_t,
]
k32.VirtualQueryEx.restype = ctypes.c_size_t

k32.GetCurrentProcess.restype = w.HANDLE
k32.DuplicateHandle.argtypes = [
    w.HANDLE,
    w.HANDLE,
    w.HANDLE,
    ctypes.POINTER(w.HANDLE),
    w.DWORD,
    w.BOOL,
    w.DWORD,
]
k32.DuplicateHandle.restype = w.BOOL
k32.GetProcessId.argtypes = [w.HANDLE]
k32.GetProcessId.restype = w.DWORD

ntdll.NtQueryInformationThread.argtypes = [
    w.HANDLE,
    ctypes.c_ulong,
    ctypes.c_void_p,
    ctypes.c_ulong,
    ctypes.POINTER(ctypes.c_ulong),
]
ntdll.NtQueryInformationThread.restype = ctypes.c_long

ntdll.NtQuerySystemInformation.argtypes = [
    ctypes.c_ulong,
    ctypes.c_void_p,
    ctypes.c_ulong,
    ctypes.POINTER(ctypes.c_ulong),
]
ntdll.NtQuerySystemInformation.restype = ctypes.c_long


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def hexa(value: int) -> str:
    return f"0x{value:X}"


class Snapshot:
    def __init__(self, kind: int, pid: int = 0):
        self.handle = k32.CreateToolhelp32Snapshot(kind, pid)
        self.valid = self.handle not in (0, w.HANDLE(-1).value, ctypes.c_void_p(-1).value)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.valid:
            k32.CloseHandle(self.handle)


def process_list() -> list[dict]:
    out = []
    with Snapshot(TH32CS_SNAPPROCESS) as snap:
        if not snap.valid:
            return out
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snap.handle, ctypes.byref(entry))
        while ok:
            out.append({"pid": entry.th32ProcessID, "name": entry.szExeFile})
            ok = k32.Process32NextW(snap.handle, ctypes.byref(entry))
    return out


def client_version(path: str) -> str:
    match = re.search(r"_(\d+\.\d+\.\d+\.\d+)_", path or "")
    return match.group(1) if match else ""


def find_client(pid: int | None) -> dict:
    targets = {"robloxplayerbeta.exe", "windowsplayer.exe", "robloxstudio.exe"}
    procs = process_list()
    if pid:
        for proc in procs:
            if proc["pid"] == pid:
                return proc
        raise SystemExit(f"pid {pid} not found")

    candidates = [p for p in procs if p["name"].lower() in targets]
    if not candidates:
        raise SystemExit("Roblox client is not running (no RobloxPlayerBeta.exe)")
    # Multiple clients: prefer the one with more threads (the in-game one).
    return candidates[-1] if len(candidates) == 1 else candidates[0]


def modules(pid: int) -> list[dict]:
    out = []
    with Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid) as snap:
        if not snap.valid:
            return out
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Module32FirstW(snap.handle, ctypes.byref(entry))
        while ok:
            base = entry.modBaseAddr or 0
            out.append(
                {
                    "name": entry.szModule,
                    "base": base,
                    "size": entry.modBaseSize,
                    "end": base + entry.modBaseSize,
                    "path": entry.szExePath,
                }
            )
            ok = k32.Module32NextW(snap.handle, ctypes.byref(entry))
    out.sort(key=lambda m: m["base"])
    return out


def resolve(addr: int, mods: list[dict]) -> str | None:
    for mod in mods:
        if mod["base"] <= addr < mod["end"]:
            return f"{mod['name']}+{hexa(addr - mod['base'])}"
    return None


def threads(pid: int, mods: list[dict]) -> list[dict]:
    out = []
    with Snapshot(TH32CS_SNAPTHREAD) as snap:
        if not snap.valid:
            return out
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Thread32First(snap.handle, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                record = {
                    "tid": entry.th32ThreadID,
                    "start": None,
                    "where": "unknown",
                    "suspect": False,
                }
                handle = k32.OpenThread(
                    THREAD_QUERY_INFORMATION | THREAD_QUERY_LIMITED_INFORMATION,
                    False,
                    entry.th32ThreadID,
                )
                if handle:
                    try:
                        start = ctypes.c_void_p(0)
                        ret = ctypes.c_ulong(0)
                        status = ntdll.NtQueryInformationThread(
                            handle,
                            ThreadQuerySetWin32StartAddress,
                            ctypes.byref(start),
                            ctypes.sizeof(start),
                            ctypes.byref(ret),
                        )
                        if status == 0 and start.value:
                            addr = start.value
                            record["start"] = hexa(addr)
                            location = resolve(addr, mods)
                            record["where"] = location or "OUTSIDE MODULES"
                            record["suspect"] = location is None
                    finally:
                        k32.CloseHandle(handle)
                out.append(record)
            ok = k32.Thread32Next(snap.handle, ctypes.byref(entry))
    return out


def inside_module(addr: int, mods: list[dict]) -> bool:
    for mod in mods:
        if mod["base"] <= addr < mod["end"]:
            return True
    return False


def regions(handle, mods: list[dict], max_regions: int = 400000) -> dict:
    stats: dict[tuple[str, int], dict] = {}
    private_exec: list[dict] = []
    orphan_images: list[dict] = []
    mapped_exec: list[dict] = []

    addr = 0x10000
    limit = 0x00007FFFFFFFFFFF
    mbi = MEMORY_BASIC_INFORMATION64()
    seen = 0
    while addr < limit and seen < max_regions:
        got = k32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not got:
            break
        seen += 1
        base = mbi.BaseAddress
        size = mbi.RegionSize
        if size == 0:
            break
        if mbi.State == MEM_COMMIT:
            kind = TYPE_NAMES.get(mbi.Type, "other")
            key = (kind, mbi.Protect & ~(PAGE_GUARD))
            bucket = stats.setdefault(key, {"count": 0, "bytes": 0})
            bucket["count"] += 1
            bucket["bytes"] += size
            executable = bool(mbi.Protect & EXEC_MASK)
            entry = {
                "base": hexa(base),
                "size": size,
                "protect": hexa(mbi.Protect),
                "type": kind,
            }
            if executable and kind == "private":
                private_exec.append(entry)
            if executable and kind == "mapped":
                mapped_exec.append(entry)
            if kind == "image" and not inside_module(mbi.AllocationBase, mods):
                orphan_images.append(entry)
        addr = base + size

    return {
        "regions_scanned": seen,
        "stats": [
            {"type": k[0], "protect": hexa(k[1]), "count": v["count"], "bytes": v["bytes"]}
            for k, v in sorted(stats.items(), key=lambda kv: -kv[1]["bytes"])
        ],
        "private_exec": private_exec,
        "mapped_exec": mapped_exec,
        "orphan_images": orphan_images,
    }


def query_system_handles():
    size = 1 << 22
    for _ in range(10):
        buf = ctypes.create_string_buffer(size)
        ret = ctypes.c_ulong(0)
        status = ntdll.NtQuerySystemInformation(
            SystemExtendedHandleInformation, buf, size, ctypes.byref(ret)
        )
        if status == 0:
            count = ctypes.c_size_t.from_buffer(buf, 0).value
            return count, buf
        size = max(size * 2, int(ret.value) + (1 << 16))
    return 0, None


def decode_access(granted: int) -> str:
    names = [name for bit, name in PROCESS_RIGHTS if granted & bit]
    if granted & 0x1FFFFF == 0x1FFFFF:
        names.append("ALL_ACCESS")
    return "|".join(names) if names else hexa(granted)


def handles_to(pid: int, proc_handle, mods: list[dict], pnames: dict[int, str]) -> dict:
    """Every handle in the system that points at `pid`.

    Newer Windows builds return Object == 0 in SystemExtendedHandleInformation
    (kernel pointer leak hardening), so object identity is useless. Instead we
    take the object-type index from our own known handle, then duplicate each
    candidate into this process and ask GetProcessId who it really points at.
    """
    count, buf = query_system_handles()
    if not buf:
        return {"available": False, "count": 0, "holders": []}

    entry_size = ctypes.sizeof(SYSTEM_HANDLE_ENTRY_EX)
    header = 2 * ctypes.sizeof(ctypes.c_size_t)
    raw = ctypes.string_at(buf, header + count * entry_size)
    entries = [
        SYSTEM_HANDLE_ENTRY_EX.from_buffer_copy(raw[header + i * entry_size : header + (i + 1) * entry_size])
        for i in range(count)
    ]

    type_index = None
    for e in entries:
        if e.UniqueProcessId == os.getpid() and e.HandleValue == (proc_handle or 0):
            type_index = e.ObjectTypeIndex
            break
    if type_index is None:
        return {"available": True, "count": 0, "holders": [], "note": "own handle not found"}

    current = k32.GetCurrentProcess()
    holders: dict[tuple[int, int], dict] = {}
    unreadable: set[str] = set()
    sources: dict[int, int] = {}

    for e in entries:
        if e.ObjectTypeIndex != type_index:
            continue
        holder = int(e.UniqueProcessId)
        if holder == os.getpid():
            source = current
        else:
            source = sources.get(holder)
            if source is None:
                source = k32.OpenProcess(PROCESS_DUP_HANDLE, False, holder)
                sources[holder] = source
            if not source:
                unreadable.add(pnames.get(holder, str(holder)))
                continue
        duplicated = w.HANDLE()
        if not k32.DuplicateHandle(
            source, w.HANDLE(e.HandleValue), current, ctypes.byref(duplicated), 0, False,
            DUPLICATE_SAME_ACCESS,
        ):
            continue
        try:
            if k32.GetProcessId(duplicated) == pid:
                holders[(holder, e.HandleValue)] = {
                    "pid": holder,
                    "process": pnames.get(holder, "?"),
                    "handle": hexa(e.HandleValue),
                    "access": decode_access(e.GrantedAccess),
                }
        finally:
            k32.CloseHandle(duplicated)

    for source in sources.values():
        if source:
            k32.CloseHandle(source)

    ordered = sorted(holders.values(), key=lambda h: (h["process"].lower(), h["handle"]))
    return {
        "available": True,
        "count": len(ordered),
        "holders": ordered,
        "unreadable": sorted(unreadable),
        "scanned_object_type": type_index,
    }


WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)


def windows(pid: int) -> list[dict]:
    out = []

    def callback(hwnd, _param):
        owner = w.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            cls = ctypes.create_unicode_buffer(256)
            title = ctypes.create_unicode_buffer(512)
            user32.GetClassNameW(hwnd, cls, 256)
            user32.GetWindowTextW(hwnd, title, 512)
            out.append(
                {
                    "hwnd": hexa(hwnd or 0),
                    "class": cls.value,
                    "title": title.value,
                    "visible": bool(user32.IsWindowVisible(hwnd)),
                }
            )
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return out


def named_pipes() -> list[str]:
    try:
        return sorted(os.listdir(r"\\.\pipe\\"))
    except OSError:
        return []


# --------------------------------------------------------------------------
# collection
# --------------------------------------------------------------------------


def collect(pid: int | None, region_scan: bool) -> dict:
    proc = find_client(pid)
    handle = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, proc["pid"])
    if not handle:
        raise SystemExit(f"cannot open pid {proc['pid']} (err {ctypes.get_last_error()})")

    try:
        mods = modules(proc["pid"])
        records = {
            "process": {
                "pid": proc["pid"],
                "name": proc["name"],
                "version": client_version(
                    next((m["path"] for m in mods if m["name"].lower().endswith(".exe")), "")
                ),
            },
            "modules": mods,
            "threads": threads(proc["pid"], mods),
            "handles": handles_to(
                proc["pid"], handle, mods, {p["pid"]: p["name"] for p in process_list()}
            ),
            "windows": windows(proc["pid"]),
            "pipes": named_pipes(),
            "regions": regions(handle, mods) if region_scan else None,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    finally:
        k32.CloseHandle(handle)

    records["findings"] = analyse(records)
    return records


def analyse(rec: dict) -> list[dict]:
    findings = []

    def add(severity, text):
        findings.append({"severity": severity, "text": text})

    mods = {m["name"].lower() for m in rec["modules"]}
    suspects = [t for t in rec["threads"] if t["suspect"]]
    if suspects:
        add("high", f"{len(suspects)} thread(s) start outside every loaded module: "
                     + ", ".join(t["tid"] and f"tid {t['tid']} @ {t['start']}" for t in suspects[:6]))
    else:
        add("ok", "every thread starts inside a loaded module")

    if rec["regions"]:
        r = rec["regions"]
        priv = r["private_exec"]
        large = [x for x in priv if x["size"] >= 512 * 1024]
        if large:
            add("high", f"{len(large)} private executable region(s) over 512KB: "
                        + ", ".join(f"{x['base']} {human(x['size'])}" for x in large[:5]))
        elif priv:
            total = sum(x["size"] for x in priv)
            add("info", f"{len(priv)} small private executable region(s), {human(total)} total "
                        "(baseline noise; no image-sized manual map)")
        if r["orphan_images"]:
            add("high", f"{len(r['orphan_images'])} image-mapped region(s) with no loader entry "
                        + "(a manually mapped PE looks like this)")

    if rec["handles"].get("available"):
        hs = rec["handles"]["holders"]
        writers = [h for h in hs if "VM_WRITE" in h["access"] or "CREATE_THREAD" in h["access"]]
        if writers:
            who = sorted({h["process"] for h in writers})
            add("medium", f"{len(who)} process(es) hold write/thread rights on the client: "
                          + ", ".join(who[:8]))
        else:
            add("ok", "no other process holds VM_WRITE or CREATE_THREAD on the client")

    odd_windows = [
        x for x in rec["windows"] if x["class"] != "WINDOWSCLIENT" and x["visible"] and x["title"]
    ]
    if odd_windows:
        add("medium", f"{len(odd_windows)} non-standard window class(es) on the client")

    tokens = ("executor", "cheat", "inject", "exploit", "synapse", "xeno", "solara",
              "fluxus", "krnl", "hydrogen", "arceus")
    suspicious_pipes = [p for p in rec["pipes"] if any(t in p.lower() for t in tokens)]
    if suspicious_pipes:
        add("low", "executor-looking named pipes: " + ", ".join(suspicious_pipes[:8]))

    module_names = {m["name"].lower() for m in rec["modules"]}
    if "robloxplayerbeta.dll" in module_names:
        add("info", "engine is split: RobloxPlayerBeta.dll is loaded (scan every module)")

    if len(mods) > 120:
        add("info", f"{len(mods)} loaded modules")
    return findings


def merge(samples: list[dict]) -> dict:
    if len(samples) == 1:
        return samples[0]
    base = json.loads(json.dumps(samples[0]))
    base["samples"] = len(samples)

    by_name = {m["name"]: m for m in base["modules"]}
    by_tid = {t["tid"]: t for t in base["threads"]}
    by_hwnd = {x["hwnd"]: x for x in base["windows"]}
    pipes = set(base["pipes"])
    holders = {(h["pid"], h["handle"]): h for h in base["handles"].get("holders", [])}
    priv = {(r["base"], r["size"]): r for r in (base["regions"] or {}).get("private_exec", [])}
    transient_threads = set()

    for sample in samples[1:]:
        for m in sample["modules"]:
            by_name.setdefault(m["name"], m)
        for t in sample["threads"]:
            if t["tid"] not in by_tid:
                transient_threads.add(t["tid"])
                by_tid[t["tid"]] = t
        for x in sample["windows"]:
            by_hwnd.setdefault(x["hwnd"], x)
        pipes.update(sample["pipes"])
        for h in sample["handles"].get("holders", []):
            holders.setdefault((h["pid"], h["handle"]), h)
        for r in (sample["regions"] or {}).get("private_exec", []):
            priv.setdefault((r["base"], r["size"]), r)

    base["modules"] = list(by_name.values())
    base["threads"] = [
        dict(t, transient=t["tid"] in transient_threads) for t in by_tid.values()
    ]
    base["windows"] = list(by_hwnd.values())
    base["pipes"] = sorted(pipes)
    base["handles"] = dict(base["handles"], holders=list(holders.values()),
                           count=len(holders))
    if base["regions"]:
        base["regions"]["private_exec"] = list(priv.values())
    base["findings"] = analyse(base)
    return base


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def report(rec: dict) -> str:
    lines = []
    p = rec["process"]
    version = f" v{p['version']}" if p.get("version") else ""
    lines.append(f"FOOTPRINT AUDIT · {p['name']}{version} pid {p['pid']} · "
                 f"{rec.get('samples', 1)} sample(s) · {rec['captured_at']}")

    mods = rec["modules"]
    exe = next((m for m in mods if m["name"].lower().endswith(".exe")), None)
    if exe:
        lines.append(f"  image: {exe['path']} ({human(exe['size'])})")
    others = [m["name"] for m in mods if m is not exe]
    lines.append(f"  modules: {len(mods)} (exe + {len(others)})")

    threads = rec["threads"]
    if threads:
        lines.append("")
        lines.append(f"THREADS ({len(threads)})")
        ordered = sorted(threads, key=lambda t: -(int(t["start"], 16) if t["start"] else 0))
        for t in ordered[:12]:
            mark = "  << outside modules" if t["suspect"] else ""
            transient = " [transient]" if t.get("transient") else ""
            lines.append(f"  {t['tid']:>7}  {(t['start'] or '?'):>18}  {t['where']}{mark}{transient}")
        if len(ordered) > 12:
            lines.append(f"  ... {len(ordered) - 12} more")

    if rec["regions"]:
        r = rec["regions"]
        lines.append("")
        lines.append(f"MEMORY ({r['regions_scanned']} regions scanned)")
        for row in r["stats"][:8]:
            lines.append(f"  {row['type']:<8} {row['protect']:<8} "
                         f"{row['count']:>6} regions  {human(row['bytes']):>8}")
        if r["private_exec"]:
            lines.append("  private executable:")
            for row in r["private_exec"][:10]:
                lines.append(f"    {row['base']}  {human(row['size']):>8}  {row['protect']}")
            if len(r["private_exec"]) > 10:
                lines.append(f"    ... {len(r['private_exec']) - 10} more")

    h = rec["handles"]
    who = sorted({row["process"] for row in h.get("holders", [])})
    lines.append("")
    lines.append(f"HANDLES TO CLIENT: {h.get('count', 0)} from {len(who)} process(es)")
    writers = [row for row in h.get("holders", []) if "VM_WRITE" in row["access"]]
    for row in writers[:8]:
        lines.append(f"  VM_WRITE  {row['process']} (pid {row['pid']})  {row['handle']}  {row['access']}")
    if h.get("unreadable"):
        lines.append(f"  unreadable holders: {', '.join(h['unreadable'][:6])}")

    lines.append("")
    lines.append(f"WINDOWS ({len(rec['windows'])})")
    for x in rec["windows"]:
        lines.append(f"  {x['class']:<22} visible={str(x['visible']):<5} {x['title'][:48]!r}")

    lines.append("")
    lines.append(f"NAMED PIPES: {len(rec['pipes'])}")

    lines.append("")
    lines.append("FINDINGS")
    order = {"high": 0, "medium": 1, "low": 2, "info": 3, "ok": 4}
    for f in sorted(rec["findings"], key=lambda f: order.get(f["severity"], 9)):
        lines.append(f"  [{f['severity']:<6}] {f['text']}")
    return "\n".join(lines)


def diff(old: dict, new: dict) -> str:
    lines = ["DELTA SINCE BASELINE"]
    new_mods = {m["name"] for m in new["modules"]}
    old_mods = {m["name"] for m in old["modules"]}
    for name in sorted(new_mods - old_mods):
        lines.append(f"  + module  {name}")
    for name in sorted(old_mods - new_mods):
        lines.append(f"  - module  {name}")

    new_tids = {t["tid"]: t for t in new["threads"]}
    old_tids = {t["tid"]: t for t in old["threads"]}
    for tid in sorted(set(new_tids) - set(old_tids)):
        t = new_tids[tid]
        lines.append(f"  + thread {tid} @ {t['start']}  {t['where']}")
    for tid in sorted(set(old_tids) - set(new_tids)):
        lines.append(f"  - thread {tid}")

    def priv_set(rec):
        return {(r["base"], r["size"]) for r in (rec.get("regions") or {}).get("private_exec", [])}

    for base, size in sorted(priv_set(new) - priv_set(old)):
        lines.append(f"  + private-exec region {base}  {human(size)}")
    for base, size in sorted(priv_set(old) - priv_set(new)):
        lines.append(f"  - private-exec region {base}  {human(size)}")

    new_holders = {(h["pid"], h["handle"]) for h in new["handles"].get("holders", [])}
    old_holders = {(h["pid"], h["handle"]) for h in old["handles"].get("holders", [])}
    for pid, handle in sorted(new_holders - old_holders):
        match = next(h for h in new["handles"]["holders"] if (h["pid"], h["handle"]) == (pid, handle))
        lines.append(f"  + handle {handle} from {match['process']} ({match['access']})")

    new_pipes = set(new["pipes"]) - set(old["pipes"])
    for pipe in sorted(new_pipes):
        lines.append(f"  + named pipe {pipe}")

    if len(lines) == 1:
        lines.append("  (identical footprint)")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="audit a live Roblox client's detection footprint")
    ap.add_argument("--pid", type=int, default=None, help="target pid (default: find the client)")
    ap.add_argument("--json", metavar="PATH", help="write the full record as json")
    ap.add_argument("--save", metavar="PATH", help="write the record and treat it as a baseline")
    ap.add_argument("--diff", metavar="PATH", help="compare against a saved baseline")
    ap.add_argument("--watch", type=float, default=0.0, metavar="SECONDS",
                    help="sample repeatedly for this long to catch transient artifacts")
    ap.add_argument("--interval", type=float, default=0.1, metavar="SECONDS")
    ap.add_argument("--no-regions", action="store_true", help="skip the (slow) region scan")
    ap.add_argument("--strict", action="store_true", help="exit 1 on high findings")
    args = ap.parse_args(argv)

    samples = []
    deadline = time.time() + max(0.0, args.watch)
    while True:
        samples.append(collect(args.pid, not args.no_regions))
        if time.time() >= deadline:
            break
        time.sleep(max(0.01, args.interval))

    rec = merge(samples)
    print(report(rec))

    if args.diff:
        baseline = json.load(open(args.diff, encoding="utf-8"))
        print()
        print(diff(baseline, rec))

    if args.json or args.save:
        path = args.json or args.save
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=1)
        print(f"\nrecord written to {path}")

    if args.strict and any(f["severity"] == "high" for f in rec["findings"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
