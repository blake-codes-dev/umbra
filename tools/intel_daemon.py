#!/usr/bin/env python3
"""
intel_daemon.py — live, local intelligence about the Roblox client.

Instead of reading stale articles, this measures the machine. Collectors:

  version   Roblox's own deploy endpoints (current LIVE version + hash)
  deploy    DeployHistory.txt, tailed with HTTP ranges: real deploy cadence
  crashlog  new files in %LOCALAPPDATA%\\Roblox\\logs  → crash events + attachments
  footprint the live client: modules, threads, memory, handles, windows, and a
            protection-map digest of the client image (drift = someone is
            touching pages, which is what an integrity check looks like)
  canary    optional: public ban state of a test account (users.roblox.com)

Run modes
  python tools/intel_daemon.py --duration 15 --interval 3     short probe
  python tools/intel_daemon.py                                run forever
  python tools/intel_daemon.py --report                       digest of what was seen
  python tools/intel_daemon.py --install                      print a schtasks command

Never gets stuck: single-instance lock, per-collector timeouts, failure backoff,
append-only JSONL with rotation, heartbeat file, and a 5-minute scheduled relaunch
that exits immediately if another instance is alive.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import re
import signal
import socket
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state" / "intel"
EVENTS = STATE / "events.jsonl"
STATE_FILE = STATE / "state.json"
HEARTBEAT = STATE / "heartbeat.json"
LOCK = STATE / "lock"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) IntelDaemon/1.0"
ROBLOX_LOGS = Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "logs"

VERSION_URL = "https://clientsettingscdn.roblox.com/v2/client-version/WindowsPlayer/channel/LIVE"
DEPLOY_URL = "https://setup.rbxcdn.com/DeployHistory.txt"
USER_URL = "https://users.roblox.com/v1/users/{id}"

DEPLOY_RE = re.compile(
    r"New (Client|WindowsPlayer|Studio64|MacPlayer) version-(\w+) at ([0-9/]+ [0-9:]+ [AP]M)"
)
LOG_VERSION_RE = re.compile(r"^(\d+\.\d+\.\d+\.\d+)")

MAX_EVENTS_BYTES = 5 * 1024 * 1024
MAX_EVENT_FILES = 3


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_state() -> None:
    STATE.mkdir(parents=True, exist_ok=True)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


def emit(kind: str, data: dict, console: bool = False) -> None:
    ensure_state()
    record = {"at": now_iso(), "kind": kind, "data": data}
    with EVENTS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    rotate()
    if console:
        printable = {k: v for k, v in data.items() if k not in ("modules", "holders")}
        print(f"[{record['at']}] {kind}: {json.dumps(printable)[:300]}")


SAMPLES = STATE / "samples.jsonl"


def append_sample(summary: dict) -> None:
    try:
        with SAMPLES.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": now_iso(), **summary}) + chr(10))
        if SAMPLES.stat().st_size > 2 * 1024 * 1024:
            lines = SAMPLES.read_text(encoding="utf-8").splitlines()[-500:]
            SAMPLES.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    except OSError:
        pass


def rotate() -> None:
    try:
        if EVENTS.stat().st_size < MAX_EVENTS_BYTES:
            return
        rotated = EVENTS.with_name(f"events-{int(time.time())}.jsonl")
        EVENTS.replace(rotated)
        old = sorted(STATE.glob("events-*.jsonl"), key=lambda p: p.stat().st_mtime)
        for path in old[:-MAX_EVENT_FILES]:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def fetch(url: str, headers: dict | None = None, timeout: float = 8.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def pid_alive(pid: int) -> bool:
    """os.kill(pid, 0) TERMINATES on Windows — query the handle instead."""
    if pid <= 0:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    exit_code = ctypes.c_ulong(0)
    ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
    kernel32.CloseHandle(handle)
    return bool(ok) and exit_code.value == 259  # STILL_ACTIVE


def acquire_lock() -> bool:
    ensure_state()
    if LOCK.exists():
        try:
            info = json.loads(LOCK.read_text(encoding="utf-8"))
            if pid_alive(int(info.get("pid", 0))):
                return False
        except (OSError, ValueError):
            pass
    LOCK.write_text(json.dumps({"pid": os.getpid(), "at": now_iso()}), encoding="utf-8")
    return True


def release_lock() -> None:
    try:
        info = json.loads(LOCK.read_text(encoding="utf-8"))
        if int(info.get("pid", 0)) == os.getpid():
            LOCK.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


class Backoff:
    """Skip a collector for a while after repeated failures."""

    def __init__(self) -> None:
        self.failures: dict[str, int] = {}

    def run(self, name: str, fn, console: bool) -> None:
        failures = self.failures.get(name, 0)
        if failures >= 3 and failures % 3 != 0:
            self.failures[name] = failures + 1
            return
        try:
            fn(console)
            self.failures[name] = 0
        except Exception as error:  # a collector must never kill the daemon
            self.failures[name] = failures + 1
            emit(
                "collector_error",
                {"collector": name, "error": repr(error)[:300], "trace": traceback.format_exc()[-600:]},
                console,
            )


# --------------------------------------------------------------------------
# collectors
# --------------------------------------------------------------------------


def collect_version(console: bool) -> None:
    payload = json.loads(fetch(VERSION_URL).decode("utf-8"))
    version = payload.get("version", "")
    upload = payload.get("clientVersionUpload", "")
    state = load_state()
    previous = state.get("version", {})
    if previous.get("version") != version or previous.get("upload") != upload:
        emit("version", {"version": version, "upload": upload, "previous": previous}, console)
        state["version"] = {"version": version, "upload": upload, "at": now_iso()}
        save_state(state)


def collect_deploy(console: bool) -> None:
    raw = fetch(DEPLOY_URL, headers={"Range": "bytes=-131072"}).decode("utf-8", "replace")
    entries = []
    for line in raw.splitlines():
        match = DEPLOY_RE.search(line)
        if match:
            entries.append({"product": match.group(1), "hash": match.group(2), "at": match.group(3)})

    state = load_state()
    seen = set(state.get("deploy_seen", []))
    keyed = [(f"{e['product']}|{e['at']}", e) for e in entries]
    fresh = [e for key, e in keyed if key not in seen]
    if fresh:
        emit("deploy", {"new": fresh[-8:], "count": len(fresh)}, console)
        state["deploy_seen"] = [key for key, _ in keyed][-600:]
        save_state(state)


def collect_crashlogs(console: bool) -> None:
    if not ROBLOX_LOGS.is_dir():
        return
    state = load_state()
    seen = set(state.get("logs_seen", []))
    files = sorted(ROBLOX_LOGS.glob("*.log"), key=lambda p: p.stat().st_mtime)
    current = [p.name for p in files]
    fresh = [p for p in files if p.name not in seen]
    if seen and fresh:
        versions = {}
        for path in fresh:
            match = LOG_VERSION_RE.match(path.name)
            key = match.group(1) if match else "unknown"
            versions[key] = versions.get(key, 0) + 1
        emit(
            "crash_event",
            {
                "new_files": [p.name for p in fresh][-6:],
                "by_version": versions,
                "total_kept": len(current),
            },
            console,
        )
    state["logs_seen"] = current[-200:]
    save_state(state)


def footprint_module():
    spec = importlib.util.spec_from_file_location(
        "footprint_audit", Path(__file__).with_name("footprint_audit.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def protect_digest(rec: dict) -> str:
    """Stable fingerprint of which (type, protection) classes exist. Counts churn
    constantly in a live game, so they are deliberately excluded."""
    regions = (rec.get("regions") or {}).get("stats") or []
    payload = "|".join(sorted({f"{r['type']}:{r['protect']}" for r in regions}))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def collect_footprint(console: bool) -> None:
    fa = footprint_module()
    try:
        rec = fa.collect(None, True)
    except SystemExit:
        return  # client not running

    summary = {
        "pid": rec["process"]["pid"],
        "version": rec["process"].get("version", ""),
        "modules": len(rec["modules"]),
        "threads": len(rec["threads"]),
        "threads_outside_modules": sum(1 for t in rec["threads"] if t["suspect"]),
        "private_exec_regions": len(rec["regions"]["private_exec"]),
        "private_exec_bytes": sum(r["size"] for r in rec["regions"]["private_exec"]),
        "orphan_images": len(rec["regions"]["orphan_images"]),
        "handles": rec["handles"].get("count", 0),
        "write_holders": sorted(
            {h["process"] for h in rec["handles"].get("holders", []) if "VM_WRITE" in h["access"]}
        ),
        "visible_windows": sorted(
            {w["class"] for w in rec["windows"] if w["visible"]}
        ),
        "protect_digest": protect_digest(rec),
    }

    core_keys = (
        "version", "modules", "threads_outside_modules", "private_exec_regions",
        "private_exec_bytes", "orphan_images", "handles", "write_holders", "visible_windows",
    )
    core = {key: summary[key] for key in core_keys}

    state = load_state()
    previous = state.get("footprint_core")
    if previous != core:
        delta = {
            key: {"from": previous.get(key), "to": core.get(key)}
            for key in core
            if previous.get(key) != core.get(key)
        } if previous else {}
        module_names = {m["name"] for m in rec["modules"]}
        delta["modules_added"] = sorted(module_names - set(state.get("module_names", [])))
        emit("footprint", {"summary": summary, "delta": delta}, console)
        state["footprint_core"] = core
        state["module_names"] = sorted(module_names)
    state["footprint_latest"] = summary
    save_state(state)
    append_sample(summary)


def collect_canary(user_id: int | None, console: bool) -> None:
    if not user_id:
        return
    payload = json.loads(fetch(USER_URL.format(id=user_id)).decode("utf-8"))
    banned = bool(payload.get("isBanned"))
    state = load_state()
    key = f"canary_{user_id}"
    previous = state.get(key)
    if previous != banned:
        emit(
            "canary",
            {"user_id": user_id, "name": payload.get("name"), "is_banned": banned,
             "was": previous},
            console,
        )
        state[key] = banned
        save_state(state)


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------


def run(args) -> int:
    if not acquire_lock():
        print("another intel_daemon is already running; exiting")
        return 0

    socket.setdefaulttimeout(args.http_timeout)
    stop = {"flag": False}

    def on_signal(_signum, _frame):
        stop["flag"] = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    backoff = Backoff()
    started = time.monotonic()
    deadline = started + args.duration if args.duration else None
    next_version = 0.0
    next_deploy = 0.0
    next_footprint = 0.0
    next_crash = 0.0
    next_canary = 0.0
    cycles = 0

    print(f"intel daemon up · state={STATE} · interval={args.interval}s"
          + (f" · duration={args.duration}s" if args.duration else " · forever"))

    try:
        while not stop["flag"]:
            clock = time.monotonic()
            if deadline and clock >= deadline:
                break

            if clock >= next_version:
                backoff.run("version", collect_version, args.console)
                next_version = clock + args.interval * 4
            if clock >= next_deploy:
                backoff.run("deploy", collect_deploy, args.console)
                next_deploy = clock + args.interval * 4
            if clock >= next_crash:
                backoff.run("crashlog", collect_crashlogs, args.console)
                next_crash = clock + args.interval * 2
            if clock >= next_footprint:
                backoff.run("footprint", collect_footprint, args.console)
                next_footprint = clock + args.interval
            if clock >= next_canary:
                backoff.run("canary", lambda c: collect_canary(args.user_id, c), args.console)
                next_canary = clock + max(args.interval * 12, 300)

            cycles += 1
            HEARTBEAT.write_text(
                json.dumps({"pid": os.getpid(), "at": now_iso(), "cycle": cycles}),
                encoding="utf-8",
            )

            sleep_for = min(
                args.interval,
                max(0.0, deadline - time.monotonic()) if deadline else args.interval,
            )
            if sleep_for > 0:
                time.sleep(sleep_for)
    finally:
        release_lock()
        print(f"intel daemon stopped after {cycles} cycle(s)")
    return 0


def report() -> int:
    if not EVENTS.exists():
        print("no events yet")
        return 0
    counts: dict[str, int] = {}
    latest: dict[str, dict] = {}
    with EVENTS.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            counts[record["kind"]] = counts.get(record["kind"], 0) + 1
            latest[record["kind"]] = record

    print(f"intel digest · {EVENTS}")
    for kind, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {kind:<16} {count:>4}")
    for kind in ("version", "deploy", "footprint", "crash_event", "canary", "collector_error"):
        record = latest.get(kind)
        if not record:
            continue
        print(f"\n{kind} · {record['at']}")
        print(json.dumps(record["data"], indent=1)[:1200])
    return 0


def install_task() -> int:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    command = (
        f'"{pythonw}" "{Path(__file__).resolve()}"'
    ).replace('"', '\\"')
    print("schtasks /Create /F /SC MINUTE /MO 5 /TN \"SoloIntel\" ^")
    print(f'  /TR "{command}"')
    print("\n(5-minute relaunch = self-healing: a second instance exits via the lock file.)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="live local intelligence about the Roblox client")
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between footprint samples")
    ap.add_argument("--duration", type=float, default=0.0, help="stop after N seconds (0 = forever)")
    ap.add_argument("--http-timeout", type=float, default=8.0)
    ap.add_argument("--user-id", type=int, default=None, help="optional canary account id")
    ap.add_argument("--report", action="store_true", help="print a digest and exit")
    ap.add_argument("--install", action="store_true", help="print the scheduled-task command")
    ap.add_argument("--console", action="store_true", help="print events as they happen")
    args = ap.parse_args(argv)

    if args.report:
        return report()
    if args.install:
        return install_task()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
