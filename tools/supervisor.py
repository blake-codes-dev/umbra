#!/usr/bin/env python3
"""
supervisor.py — the part that does not need a brain.

Runs every 5 minutes (scheduled task + startup shortcut). Detects mode
(online/offline) and writes the handoff that switches control between the agent
(online) and qwen (offline). Heals the machinery deterministically.

Checks
  disk space, log/artifact growth, worker heartbeats, stale locks, ollama,
  the DeepAI Code app, the Roblox client, scheduled tasks, agent marker

Actions (idempotent, logged, capped)
  prune, rotate, clear_locks, start_ollama, start_app, start_roblox,
  ensure_tasks, restart_night_runner, flag_human

Escalation
  a check failing 3 cycles in a row writes state/supervisor/NEEDS_HUMAN.md with
  the runbook anchor; it never deletes data and never edits project files.

Usage
  python tools/supervisor.py --once      one cycle, print health
  python tools/supervisor.py --auto      forever (scheduled)
  python tools/supervisor.py --status    print last health
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = Path(__file__).resolve().parent
STATE = ROOT / "state"
SUP = STATE / "supervisor"
HEALTH = SUP / "health.json"
EVENTS = SUP / "events.jsonl"
NEEDS_HUMAN = SUP / "NEEDS_HUMAN.md"
MODE = STATE / "mode.json"
HANDOFF = STATE / "handoff"
HEARTBEATS = {
    "night_runner": STATE / "night" / "heartbeat.json",
    "intel": STATE / "intel" / "heartbeat.json",
    "qwen": STATE / "qwen" / "heartbeat.json",
    "agent": STATE / "agent" / "last_seen.json",
}
LOCKS = [STATE / "night" / "lock", STATE / "intel" / "lock", STATE / "qwen" / "lock"]
STOP = STATE / "night" / "stop"
APP_EXE = Path(r"C:\Program Files\DeepAI Code\DeepAI Code.exe")
PYTHONW = Path(sys.executable).with_name("pythonw.exe")
NIGHT_RUNNER = TOOLS / "night_runner.py"
AGENT_TURN_MAX_AGE = 40 * 3600  # generous: an agent turn may be missed without the world ending
OWNER_LOCK = SUP / "lock"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def now() -> datetime:
    return datetime.now()


def stamp() -> str:
    return now().strftime("%Y-%m-%dT%H:%M:%S")


def ensure() -> None:
    SUP.mkdir(parents=True, exist_ok=True)
    HANDOFF.mkdir(parents=True, exist_ok=True)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    kernel32 = ctypes.WinDLL("kernel32")
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    code = ctypes.c_ulong(0)
    ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel32.CloseHandle(handle)
    return bool(ok) and code.value == 259


def processes() -> list[dict]:
    """pid + name, via the toolhelp snapshot already used by the auditor."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "footprint_audit", TOOLS / "footprint_audit.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.process_list()


def online(timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=timeout):
            return True
    except OSError:
        return False


def event(kind: str, data: dict, console: bool = False) -> None:
    ensure()
    with EVENTS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": stamp(), "kind": kind, "data": data}) + chr(10))
    if console:
        print(f"[{stamp()}] {kind}: {json.dumps(data)[:220]}")
    try:
        if EVENTS.stat().st_size > 2 * 1024 * 1024:
            lines = EVENTS.read_text(encoding="utf-8").splitlines()[-500:]
            EVENTS.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    except OSError:
        pass


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def heartbeat_age(path: Path) -> float | None:
    data = load_json(path)
    at = data.get("at") or data.get("seen_at")
    if not at:
        return None
    try:
        parsed = datetime.fromisoformat(at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # naive timestamps in this project are local time (intel_daemon writes UTC)
        return (now() - parsed).total_seconds()
    return (datetime.now(timezone.utc) - parsed).total_seconds()


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------


def prune(max_age_days: int = 3, keep_gb: float = 5.0) -> dict:
    removed = 0
    cutoff = time.time() - max_age_days * 86400
    for folder in (STATE / "night" / "artifacts", STATE / "observer", STATE / "intel"):
        if not folder.exists():
            continue
        for item in folder.rglob("*"):
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink(missing_ok=True)
                removed += 1
    for log in list(STATE.rglob("*.jsonl")):
        try:
            if log.stat().st_size > 6 * 1024 * 1024:
                lines = log.read_text(encoding="utf-8").splitlines()[-800:]
                log.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
        except OSError:
            pass
    return {"removed": removed}


def clear_locks() -> dict:
    cleared = []
    for lock in LOCKS:
        if not lock.exists():
            continue
        info = load_json(lock)
        if not pid_alive(int(info.get("pid", 0))):
            lock.unlink(missing_ok=True)
            cleared.append(str(lock.relative_to(ROOT)))
    return {"cleared": cleared}


def ollama_up() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2):
            return True
    except Exception:  # noqa: BLE001
        return False


def start_ollama() -> dict:
    if ollama_up():
        return {"started": False, "reason": "already up"}
    subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008 | 0x00000200)  # DETACHED | NEW_PROCESS_GROUP
    for _ in range(10):
        time.sleep(0.5)
        if ollama_up():
            return {"started": True}
    return {"started": False, "reason": "no response after 5s"}


def start_app() -> dict:
    if any(p["name"].lower().startswith("deepai code") for p in processes()):
        return {"started": False, "reason": "already running"}
    if not APP_EXE.exists():
        return {"started": False, "reason": "exe missing"}
    subprocess.Popen([str(APP_EXE), "--force-renderer-accessibility"],
                     creationflags=0x00000008)
    time.sleep(3)
    running = any(p["name"].lower().startswith("deepai code") for p in processes())
    return {"started": running}


def start_roblox() -> dict:
    names = {p["name"].lower() for p in processes()}
    if "robloxplayerbeta.exe" in names or "windowsplayer.exe" in names:
        return {"started": False, "reason": "already running"}
    attempts = []
    try:
        os.startfile("roblox://")
        attempts.append("protocol")
    except OSError as error:
        attempts.append(f"protocol failed: {error}")
    for _ in range(12):
        time.sleep(1)
        if any(p["name"].lower() in ("robloxplayerbeta.exe", "windowsplayer.exe") for p in processes()):
            return {"started": True, "via": attempts[-1]}
    return {"started": False, "attempts": attempts}


def restart_night_runner() -> dict:
    """Kill a wedged runner (stale heartbeat) and let the 5-min task relaunch it."""
    killed = []
    for proc in processes():
        if proc["name"].lower() not in ("pythonw.exe", "python.exe"):
            continue
    # the lock knows the pid; only kill it if its heartbeat is old
    age = heartbeat_age(HEARTBEATS["night_runner"])
    lock = load_json(STATE / "night" / "lock")
    pid = int(lock.get("pid", 0))
    if age is not None and age > 45 * 60 and pid_alive(pid):
        kernel32 = ctypes.WinDLL("kernel32")
        handle = kernel32.OpenProcess(0x0001, False, pid)  # TERMINATE
        if handle:
            kernel32.TerminateProcess(handle, 0)
            kernel32.CloseHandle(handle)
            killed.append(pid)
        (STATE / "night" / "lock").unlink(missing_ok=True)
    return {"killed": killed, "heartbeat_age": round(age, 1) if age else None}


def ensure_tasks(console: bool = False) -> dict:
    wanted = {
        "RobloxNightRunner": f'"{PYTHONW}" "{NIGHT_RUNNER}" --auto --narrator qwen3:8b',
        "RobloxSupervisor": f'"{PYTHONW}" "{TOOLS / "supervisor.py"}" --auto',
        "RobloxQwenShift": f'"{PYTHONW}" "{TOOLS / "qwen_agent.py"}" --auto',
    }
    missing = []
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    for name, command in wanted.items():
        probe = subprocess.run(["schtasks", "/Query", "/TN", name], capture_output=True,
                               text=True, env=env)
        if probe.returncode != 0:
            missing.append(name)
            subprocess.run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO",
                            "15" if name == "RobloxQwenShift" else "5",
                            "/TN", name, "/TR", command],
                           capture_output=True, text=True, env=env)
    return {"missing": missing}


def flag_human(issues: list[str]) -> None:
    if not issues:
        NEEDS_HUMAN.unlink(missing_ok=True)
        return
    text = ["# Needs a human", "", f"written {stamp()}", ""]
    text += [f"- {item}" for item in issues]
    text += ["", "Runbook: RUNBOOK.md §Recovery. The supervisor keeps retrying; nothing is lost.",
             "Stop all automation: create state/night/stop"]
    NEEDS_HUMAN.write_text(chr(10).join(text) + chr(10), encoding="utf-8")


# --------------------------------------------------------------------------
# cycle
# --------------------------------------------------------------------------


def hung_app_pids(procs: list[dict]) -> list[int]:
    """Visible windows of the app that Windows marks 'Not Responding'."""
    from ctypes import wintypes

    pids = {p["pid"] for p in procs if p["name"].lower().startswith("deepai code")}
    if not pids:
        return []
    user32 = ctypes.windll.user32
    hung: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd) and user32.IsHungAppWindow(hwnd):
            hung.append(pid.value)
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return hung


def _doctor(flag: str) -> dict:
    try:
        out = subprocess.run([sys.executable, str(TOOLS / "app_doctor.py"), flag],
                             capture_output=True, text=True, timeout=120,
                             encoding="utf-8", errors="replace").stdout
    except (OSError, subprocess.SubprocessError) as error:
        return {"state": "error", "note": repr(error)[:120]}
    for line in reversed(out.strip().splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {"state": "error", "note": "no json from app_doctor"}


def app_state() -> dict:
    return _doctor("--check")


def app_repair() -> dict:
    return _doctor("--auto")


def login_due(minutes: int = 30) -> bool:
    try:
        return time.time() - (STATE / "app" / "last_login_attempt").stat().st_mtime > minutes * 60
    except OSError:
        return True


def check_cycle(console: bool = False) -> dict:
    ensure()
    procs = processes()
    names = [p["name"].lower() for p in procs]
    checks: dict[str, dict] = {}
    actions: list[dict] = []
    issues: list[str] = []

    was_online = bool(load_json(MODE).get("online"))
    is_online = online()
    if was_online != is_online:
        MODE.write_text(json.dumps({"online": is_online, "since": stamp(),
                                    "previous": was_online}, indent=1), encoding="utf-8")
        handoff = {
            "at": stamp(),
            "to": "agent" if is_online else "qwen",
            "queued": ["night_runner plan", "qwen playbooks", "observer artifacts"],
            "note": "agent takes over; read MORNING.md + state/supervisor/health.json"
            if is_online else "qwen takes over; agent resumes on the next online turn",
        }
        (HANDOFF / f"{'online' if is_online else 'offline'}-{int(time.time())}.json").write_text(
            json.dumps(handoff, indent=1), encoding="utf-8")
        event("mode", {"online": is_online}, console)
    checks["mode"] = {"online": is_online}

    usage = shutil.disk_usage(ROOT)
    free_gb = usage.free / 2 ** 30
    checks["disk"] = {"free_gb": round(free_gb, 1)}
    if free_gb < 8:
        actions.append({"action": "prune", "result": prune()})

    ages = {}
    for name, path in HEARTBEATS.items():
        age = heartbeat_age(path)
        ages[name] = round(age / 60, 1) if age is not None else None
    checks["heartbeats"] = ages
    if ages.get("night_runner") and ages["night_runner"] > 45:
        actions.append({"action": "restart_night_runner", "result": restart_night_runner()})
    if ages.get("agent") and ages["agent"] * 60 > AGENT_TURN_MAX_AGE:
        issues.append(f"no agent turn for {ages['agent'] / 60:.0f} h (app closed or provider down)")

    # vault: daily snapshot, weekly backup into D:/LyphenVault, drift report for user files
    try:
        subprocess.run([sys.executable, str(TOOLS / "vault.py"), "--quiet"],
                       capture_output=True, timeout=600)
        drift = load_json(STATE / "vault" / "drift.json")
        checks["vault"] = {"added": drift.get("added"), "removed": drift.get("removed"),
                           "changed": drift.get("changed"), "at": drift.get("at")}
    except (OSError, subprocess.SubprocessError):
        checks["vault"] = {"error": True}

    checks["locks"] = clear_locks()

    checks["ollama"] = {"up": ollama_up()}
    if not checks["ollama"]["up"]:
        actions.append({"action": "start_ollama", "result": start_ollama()})

    app_running = any(name.startswith("deepai code") for name in names)
    checks["app"] = {"running": app_running}
    if not app_running:
        actions.append({"action": "start_app", "result": start_app()})
        issues.append("DeepAI Code was not running (restarted)")

    # A hung Electron window never exits and never answers. Three consecutive
    # "Not Responding" checks (15 min) is long enough that it is not a render blip.
    hung = hung_app_pids(procs)
    streak = load_json(HEALTH).get("app_hung_streak", 0) + 1 if hung else 0
    checks["app"]["hung"] = bool(hung)
    if streak >= 3:
        killed = []
        for pid in sorted(set(hung)):
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                               timeout=10)
                killed.append(pid)
            except (OSError, subprocess.SubprocessError):
                pass
        actions.append({"action": "restart_hung_app", "result": killed})
        issues.append("DeepAI Code was not responding for 15 min (restarted)")
        streak = 0
        time.sleep(3)
        start_app()

    # Sign-in is the one app failure a restart cannot fix: the session is gone and
    # someone has to click through Google. Only attempt it offline (no agent turn to
    # disturb) and at most every 30 minutes, so a blind attempt never becomes a loop.
    if app_running:
        doctor = app_state()
        checks["app"]["doctor"] = doctor.get("state", "?")
        if doctor.get("state") == "sign_in":
            if is_online:
                issues.append("app is signed out (online: agent must sign in with Google)")
            elif login_due():
                (STATE / "app").mkdir(parents=True, exist_ok=True)
                (STATE / "app" / "last_login_attempt").write_text(str(int(time.time())),
                                                                  encoding="utf-8")
                actions.append({"action": "app_repair", "result": app_repair()})
        elif doctor.get("state") == "unknown" and doctor.get("accessibility") == "off":
            checks["app"]["accessibility"] = "off"

    client = any(name in ("robloxplayerbeta.exe", "windowsplayer.exe") for name in names)
    checks["client"] = {"running": client}
    if not client and not is_online:
        # only worth relaunching while we are off the clock; a client launch
        # during the day may interrupt a session the human is using
        actions.append({"action": "start_roblox", "result": start_roblox()})

    checks["tasks"] = ensure_tasks(console)
    checks["python_workers"] = sum(1 for name in names if name in ("pythonw.exe", "python.exe"))

    data = {"at": stamp(), "checks": checks, "actions": actions,
            "app_hung_streak": streak}
    tmp = HEALTH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, HEALTH)

    if issues:
        event("issue", {"issues": issues}, console)
        flag_human(issues)
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="deterministic supervisor")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--interval", type=float, default=300.0)
    args = parser.parse_args(argv)

    if args.status:
        print(json.dumps(load_json(HEALTH), indent=1))
        return 0

    if args.once:
        print(json.dumps(check_cycle(console=True), indent=1))
        return 0

    ensure()
    print(f"supervisor up · interval {args.interval}s · pid {os.getpid()}")
    while True:
        try:
            check_cycle(console=True)
        except Exception as error:  # noqa: BLE001
            event("supervisor_error", {"error": repr(error)[:300]})
        if STOP.exists():
            print("stop file present; supervisor idle")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
