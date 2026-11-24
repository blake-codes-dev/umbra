#!/usr/bin/env python3
"""
observer.py — measure what the client's anti-tamper actually DOES.

Hyperion emits no messages, but it cannot hide behaviour:

  * CPU time per thread   → it runs somewhere; periodic bursts give a period
  * page protections      → integrity checks and page crypto touch protections
  * handle / heap churn   → it allocates buffers while scanning
  * guard-page decoys     → plant a page in the client; if the scanner reads it,
                            the guard bit clears and we know it scanned AND when

Layers
  L1 external (this file, no injection): thread timing, protection churn,
     handle/working-set churn, and decoy pages allocated with VirtualAllocEx.
  L2 in-process (needs the DLL): VEH on guard decoys → the scanner's RIP, i.e.
     the exact Hyperion function that looked at us.
  L3 ETW (kernel providers): virtual alloc/free, thread start, image load.

Usage
  python tools/observer.py --seconds 20                       measure
  python tools/observer.py --seconds 15 --decoy               + decoy pages
  python tools/observer.py --seconds 20 --json state/obs.json
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import statistics
import sys
import time
from ctypes import wintypes as w
from pathlib import Path

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi")
ntdll = ctypes.WinDLL("ntdll")

THREAD_QUERY_INFORMATION = 0x0040
THREAD_QUERY_LIMITED_INFORMATION = 0x0800
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020

MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
MEM_PRIVATE = 0x20000
MEM_IMAGE = 0x1000000
PAGE_READWRITE = 0x04
PAGE_GUARD = 0x100
PAGE_EXECUTE_READ = 0x20


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


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", w.DWORD),
        ("PageFaultCount", w.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
k32.OpenProcess.restype = w.HANDLE
k32.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
k32.OpenThread.restype = w.HANDLE
k32.GetThreadTimes.argtypes = [w.HANDLE, w.LPFILETIME, w.LPFILETIME, w.LPFILETIME, w.LPFILETIME]
k32.GetThreadTimes.restype = w.BOOL
k32.GetProcessTimes.argtypes = [w.HANDLE, w.LPFILETIME, w.LPFILETIME, w.LPFILETIME, w.LPFILETIME]
k32.GetProcessTimes.restype = w.BOOL
k32.GetProcessHandleCount.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
k32.GetProcessHandleCount.restype = w.BOOL
psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), w.DWORD]
psapi.GetProcessMemoryInfo.restype = w.BOOL
k32.VirtualQueryEx.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.POINTER(MEMORY_BASIC_INFORMATION64), ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualAllocEx.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_size_t, w.DWORD, w.DWORD]
k32.VirtualAllocEx.restype = ctypes.c_void_p
k32.VirtualFreeEx.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_size_t, w.DWORD]
k32.VirtualFreeEx.restype = w.BOOL
k32.VirtualProtectEx.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_size_t, w.DWORD, ctypes.POINTER(w.DWORD)]
k32.VirtualProtectEx.restype = w.BOOL


def load_footprint_module():
    spec = importlib.util.spec_from_file_location(
        "footprint_audit", Path(__file__).with_name("footprint_audit.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fa = load_footprint_module()


def filetime_to_int(value: w.FILETIME) -> int:
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


def thread_times(handle) -> int:
    creation, exit_, kernel, user = w.FILETIME(), w.FILETIME(), w.FILETIME(), w.FILETIME()
    if not k32.GetThreadTimes(handle, ctypes.byref(creation), ctypes.byref(exit_),
                              ctypes.byref(kernel), ctypes.byref(user)):
        return 0
    return filetime_to_int(kernel) + filetime_to_int(user)


def process_times(handle) -> int:
    creation, exit_, kernel, user = w.FILETIME(), w.FILETIME(), w.FILETIME(), w.FILETIME()
    if not k32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_),
                               ctypes.byref(kernel), ctypes.byref(user)):
        return 0
    return filetime_to_int(kernel) + filetime_to_int(user)


def autocorr_period(series: list[float], interval: float, max_lag: int = 40) -> tuple[float, int, float]:
    """Best lag (in samples) where the signal repeats -> (period_seconds, lag, strength)."""
    n = len(series)
    if n < max_lag * 2:
        return (0.0, 0, 0.0)
    mean = statistics.fmean(series)
    centred = [value - mean for value in series]
    denom = sum(value * value for value in centred)
    if denom <= 1e-9:
        return (0.0, 0, 0.0)
    best_lag, best = 0, 0.0
    for lag in range(2, min(max_lag, n // 2)):
        num = sum(centred[i] * centred[i + lag] for i in range(n - lag))
        score = num / denom
        if score > best:
            best_lag, best = lag, score
    return (best_lag * interval, best_lag, round(best, 3))


def tracked_regions(handle, mods: list[dict]) -> list[dict]:
    """Regions inside loaded modules only — integrity checks live there."""
    regions = []
    seen = set()
    for mod in mods:
        addr = mod["base"]
        mbi = MEMORY_BASIC_INFORMATION64()
        while addr < mod["end"]:
            if not k32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            if mbi.RegionSize == 0:
                break
            key = (mbi.BaseAddress, mbi.RegionSize)
            if key not in seen:
                seen.add(key)
                regions.append({"base": mbi.BaseAddress, "size": mbi.RegionSize, "module": mod["name"]})
            addr = mbi.BaseAddress + mbi.RegionSize
    return regions


def probe_protect(handle, addr: int) -> int | None:
    mbi = MEMORY_BASIC_INFORMATION64()
    if not k32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
        return None
    return mbi.Protect


def sample(handle, proc, tid_names: dict[int, dict]) -> dict:
    threads = {}
    with fa.Snapshot(fa.TH32CS_SNAPTHREAD) as snap:
        entry = fa.THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Thread32First(snap.handle, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == proc["pid"]:
                handle_t = k32.OpenThread(THREAD_QUERY_INFORMATION | THREAD_QUERY_LIMITED_INFORMATION,
                                          False, entry.th32ThreadID)
                if handle_t:
                    try:
                        threads[entry.th32ThreadID] = thread_times(handle_t)
                    finally:
                        k32.CloseHandle(handle_t)
            ok = k32.Thread32Next(snap.handle, ctypes.byref(entry))

    handles = w.DWORD(0)
    k32.GetProcessHandleCount(handle, ctypes.byref(handles))
    mem = PROCESS_MEMORY_COUNTERS_EX()
    mem.cb = ctypes.sizeof(mem)
    psapi.GetProcessMemoryInfo(handle, ctypes.byref(mem), mem.cb)

    return {
        "threads": threads,
        "total": process_times(handle),
        "handles": handles.value,
        "working_set": mem.WorkingSetSize,
        "page_faults": mem.PageFaultCount,
    }


def measure(proc, seconds: float, interval: float) -> dict:
    handle = k32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_VM_OPERATION | PROCESS_VM_WRITE,
        False, proc["pid"],
    )
    if not handle:
        raise SystemExit(f"cannot open pid {proc['pid']}")

    mods = fa.modules(proc["pid"])
    regions = tracked_regions(handle, mods)
    region_protect = {r["base"]: probe_protect(handle, r["base"]) for r in regions}
    thread_starts = {t["tid"]: t for t in fa.threads(proc["pid"], mods)}

    series: dict[int, list[float]] = {}
    total_series: list[float] = []
    handle_series: list[int] = []
    ws_series: list[int] = []
    flips: dict[int, list[dict]] = {}
    flip_events: list[float] = []
    timestamps: list[float] = []
    previous = None
    started = time.monotonic()
    try:
        while time.monotonic() - started < seconds:
            current = sample(handle, proc, thread_starts)
            timestamps.append(round(time.monotonic() - started, 3))

            if previous:
                for tid, value in current["threads"].items():
                    if tid in previous["threads"]:
                        delta = max(0, value - previous["threads"][tid]) / 10_000.0  # ms
                        series.setdefault(tid, []).append(delta)
                total_series.append(max(0, current["total"] - previous["total"]) / 10_000.0)

            handle_series.append(current["handles"])
            ws_series.append(current["working_set"])

            for region in regions:
                protect = probe_protect(handle, region["base"])
                old = region_protect.get(region["base"])
                if protect != old:
                    at = round(time.monotonic() - started, 3)
                    flips.setdefault(region["base"], []).append(
                        {"at": at, "from": old, "to": protect}
                    )
                    flip_events.append(at)
                    region_protect[region["base"]] = protect

            previous = current
            time.sleep(interval)
    finally:
        k32.CloseHandle(handle)

    # thread analysis
    thread_report = []
    for tid, values in series.items():
        if len(values) < 5:
            continue
        nonzero = [value for value in values if value > 0.01]
        mean = statistics.fmean(values)
        peak = max(values)
        period, lag, strength = autocorr_period(values, interval)
        info = thread_starts.get(tid, {})
        thread_report.append(
            {
                "tid": tid,
                "start": info.get("start"),
                "where": info.get("where"),
                "cpu_ms_total": round(sum(values), 1),
                "cpu_ms_peak": round(peak, 2),
                "active_ratio": round(len(nonzero) / len(values), 3),
                "burstiness": round(peak / mean, 1) if mean > 0 else 0,
                "period_s": round(period, 2),
                "period_strength": strength,
            }
        )
    thread_report.sort(key=lambda t: -t["cpu_ms_total"])

    flip_report = []
    for base, events in flips.items():
        meta = next((r for r in regions if r["base"] == base), {})
        flip_report.append(
            {
                "base": hex(base),
                "module": meta.get("module"),
                "size": meta.get("size"),
                "flips": len(events),
                "transitions": events[:10],
            }
        )
    flip_report.sort(key=lambda f: -f["flips"])

    # cluster flip timestamps into "sweeps": a burst of pages changing together
    clusters: list[list[float]] = []
    for at in sorted(flip_events):
        if clusters and at - clusters[-1][-1] <= 0.25:
            clusters[-1].append(at)
        else:
            clusters.append([at])
    cluster_starts = [round(c[0], 2) for c in clusters]
    intervals = [round(b - a, 2) for a, b in zip(cluster_starts, cluster_starts[1:])]
    sweeps = {
        "count": len(clusters),
        "pages_per_sweep": [len(c) for c in clusters],
        "starts": cluster_starts[:20],
        "intervals": intervals[:20],
        "median_interval": round(statistics.median(intervals), 2) if intervals else None,
    }

    return {
        "pid": proc["pid"],
        "seconds": seconds,
        "interval": interval,
        "samples": len(timestamps),
        "threads": thread_report[:12],
        "thread_count": len(series),
        "process_cpu_ms_total": round(sum(total_series), 1),
        "regions_tracked": len(regions),
        "regions_flipped": len(flip_report),
        "region_flips": flip_report[:12],
        "sweeps": sweeps,
        "handle_count": {"min": min(handle_series), "max": max(handle_series),
                         "delta": handle_series[-1] - handle_series[0]},
        "working_set_mb": {"min": round(min(ws_series) / 2**20, 1), "max": round(max(ws_series) / 2**20, 1),
                           "delta_mb": round((ws_series[-1] - ws_series[0]) / 2**20, 1)},
    }


# --------------------------------------------------------------------------
# decoy pages: does anything read foreign memory, and when?
# --------------------------------------------------------------------------


def decoy_experiment(proc, seconds: float, interval: float) -> dict:
    """Allocate guard pages inside the client via VirtualAllocEx.

    A guard page raises an exception on first touch and the OS clears the guard
    bit, so polling VirtualQueryEx tells us *whether and when* something read a
    page we own. Keeping the page small and unaligned to anything meaningful
    keeps the footprint boring.
    """
    handle = k32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_VM_OPERATION | PROCESS_VM_WRITE,
        False, proc["pid"],
    )
    if not handle:
        raise SystemExit("cannot open process for decoys")

    sizes = [0x1000, 0x10000, 0x100000]  # 4K, 64K, 1M
    decoys = []
    try:
        for size in sizes:
            address = k32.VirtualAllocEx(handle, None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
            if not address:
                continue
            old = w.DWORD(0)
            if not k32.VirtualProtectEx(handle, ctypes.c_void_p(address), size,
                                        PAGE_READWRITE | PAGE_GUARD, ctypes.byref(old)):
                k32.VirtualFreeEx(handle, ctypes.c_void_p(address), 0, MEM_RELEASE)
                continue
            decoys.append({"address": address, "size": size, "hits": [], "armed": 1})

        started = time.monotonic()
        while time.monotonic() - started < seconds:
            for decoy in decoys:
                protect = probe_protect(handle, decoy["address"])
                if protect is not None and not (protect & PAGE_GUARD):
                    decoy["hits"].append(round(time.monotonic() - started, 3))
                    old = w.DWORD(0)
                    if k32.VirtualProtectEx(handle, ctypes.c_void_p(decoy["address"]), decoy["size"],
                                            PAGE_READWRITE | PAGE_GUARD, ctypes.byref(old)):
                        decoy["armed"] += 1
            time.sleep(interval)
    finally:
        for decoy in decoys:
            k32.VirtualFreeEx(handle, ctypes.c_void_p(decoy["address"]), 0, MEM_RELEASE)
        k32.CloseHandle(handle)

    return {
        "decoys": [
            {"size": d["size"], "address": hex(d["address"]), "hits": len(d["hits"]),
             "first_hits": d["hits"][:12], "rearms": d["armed"] - 1}
            for d in decoys
        ]
    }


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def report(result: dict, decoy: dict | None) -> str:
    lines = []
    lines.append(
        f"OBSERVER · pid {result['pid']} · {result['samples']} samples over {result['seconds']}s "
        f"@ {result['interval']}s"
    )
    lines.append(f"  process CPU: {result['process_cpu_ms_total']:.0f} ms  "
                 f"threads tracked: {result['thread_count']}  "
                 f"regions tracked: {result['regions_tracked']}")
    lines.append("")

    lines.append("THREADS BY CPU (top 8)")
    if not result["threads"]:
        lines.append("  (no CPU measured)")
    for row in result["threads"][:8]:
        lines.append(
            f"  {row['tid']:>7}  {row['cpu_ms_total']:>8.1f}ms  peak {row['cpu_ms_peak']:>5.1f}ms  "
            f"active {row['active_ratio'] * 100:>4.1f}%  burst x{row['burstiness']:>5.1f}  "
            f"period {row['period_s']:>5.2f}s (r={row['period_strength']})  "
            f"{(row['where'] or '?')[:44]}"
        )

    lines.append("")
    sw = result.get("sweeps", {})
    lines.append(f"PROTECTION SWEEPS: {sw.get('count', 0)} in {result['seconds']:.0f}s  "
                 f"pages/sweep {sw.get('pages_per_sweep', [])[:12]}  "
                 f"median gap {sw.get('median_interval')}s")
    lines.append(f"  sweep starts {sw.get('starts', [])[:12]}")
    lines.append(f"  intervals   {sw.get('intervals', [])[:14]}")
    lines.append("")
    lines.append(f"PROTECTION CHURN · {result['regions_flipped']} of {result['regions_tracked']} regions changed")
    for row in result["region_flips"][:8]:
        transitions = ", ".join(f"{t['at']}s {t['from']:#x}->{t['to']:#x}" for t in row["transitions"][:4])
        lines.append(f"  {row['module'][:22]:<22} {row['base']:>14}  {row['flips']:>3} flips   {transitions}")

    h = result["handle_count"]
    w_ = result["working_set_mb"]
    lines.append("")
    lines.append(f"CHURN  handles {h['min']}..{h['max']} (d{h['delta']:+d})   "
                 f"working set {w_['min']:.0f}..{w_['max']:.0f} MB (d{w_['delta_mb']:+.1f})")

    if decoy:
        lines.append("")
        lines.append("DECOY PAGES (guard bit cleared == something read our memory)")
        for row in decoy["decoys"]:
            hits = ", ".join(f"{t}s" for t in row["first_hits"][:6])
            lines.append(f"  {row['size']:>9} B @ {row['address']}  hits: {row['hits']:<3} {hits}")
        if decoy["decoys"] and not any(row["hits"] for row in decoy["decoys"]):
            lines.append("  nothing read the decoys during the window")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="observe what the client's anti-tamper does")
    ap.add_argument("--pid", type=int)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--interval", type=float, default=0.05)
    ap.add_argument("--decoy", action="store_true", help="plant guard pages and watch for reads")
    ap.add_argument("--json", metavar="PATH")
    args = ap.parse_args(argv)

    proc = fa.find_client(args.pid)
    result = measure(proc, args.seconds, args.interval)
    decoy = decoy_experiment(proc, max(3.0, args.seconds * 0.6), args.interval) if args.decoy else None
    print(report(result, decoy))

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps({"measure": result, "decoy": decoy}, indent=1),
                                   encoding="utf-8")
        print(f"\nwritten to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
