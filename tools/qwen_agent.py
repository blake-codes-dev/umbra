#!/usr/bin/env python3
"""
qwen_agent.py — offline cognition: a dispatcher over a fixed playbook.

When the internet is gone the remote agent cannot think. This process runs on a
15-minute task, reads the supervisor's health file, and asks the local model to
pick ONE action from a whitelist. The model never writes code, never edits files,
and never invents actions: unknown output falls back to deterministic rules.

Why a dispatcher and not an agent: qwen3:8b is ~40 tok/s and forgets context; a
whitelist plus a schema plus a fallback is more reliable than open-ended
reasoning, and it cannot loop.

Usage
  python tools/qwen_agent.py --auto            # scheduled every 15 min
  python tools/qwen_agent.py --once --force-offline
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = Path(__file__).resolve().parent
STATE = ROOT / "state"
QWEN = STATE / "qwen"
HEARTBEAT = QWEN / "heartbeat.json"
DECISIONS = QWEN / "decisions.jsonl"
BUDGET_FILE = QWEN / "budget.json"
HEALTH = STATE / "supervisor" / "health.json"
RUNS = STATE / "night" / "runs.jsonl"
MODE = STATE / "mode.json"
STOP = STATE / "night" / "stop"
OLLAMA = "http://127.0.0.1:11434"
MAX_ACTIONS_PER_HOUR = 6
MAX_ACTIONS_PER_NIGHT = 40


def now() -> datetime:
    return datetime.now()


def stamp() -> str:
    return now().strftime("%Y-%m-%dT%H:%M:%S")


def ensure() -> None:
    QWEN.mkdir(parents=True, exist_ok=True)


def load(path: Path, default: dict | None = None) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default or {}


def tail(path: Path, lines: int = 12) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()[-lines:]
    except OSError:
        return []


def online(timeout: float = 1.5) -> bool:
    import socket
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=timeout):
            return True
    except OSError:
        return False


def supervisor():
    """Reuse the supervisor's playbook functions instead of duplicating them."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("supervisor", TOOLS / "supervisor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PLAYBOOK = {
    "restart_night_runner": "night worker heartbeat is stale or wedged; kill and let the task relaunch it",
    "start_ollama": "the local model server is down; start it so offline cognition keeps working",
    "start_app": "the DeepAI Code app is not running; start it so the online agent can return",
    "start_roblox": "the game client is closed; relaunch so telemetry keeps comparing against a live client",
    "prune": "disk is filling; delete artifacts older than 3 days and compress oversized logs",
    "clear_locks": "a lock file references a dead pid; remove it so workers are not blocked",
    "ensure_tasks": "scheduled tasks are missing; recreate them",
    "app_repair": "the app window shows a signed-out state; run the UI doctor to sign in with Google",
    "report_only": "nothing to do; write the situation down and leave the machine alone",
}


def budget() -> dict:
    data = load(BUDGET_FILE, {"hour": "", "count_hour": 0, "night": now().strftime("%Y-%m-%d"),
                              "count_night": 0, "actions": 0})
    hour = now().strftime("%Y-%m-%dT%H")
    if data.get("hour") != hour:
        data["hour"], data["count_hour"] = hour, 0
    night = now().strftime("%Y-%m-%d")
    if data.get("night") != night:
        data["night"], data["count_night"] = night, 0
    return data


def spend(data: dict) -> bool:
    if data["count_hour"] >= MAX_ACTIONS_PER_HOUR or data["count_night"] >= MAX_ACTIONS_PER_NIGHT:
        return False
    data["count_hour"] += 1
    data["count_night"] += 1
    data["actions"] = data.get("actions", 0) + 1
    BUDGET_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return True


def ask_qwen(model: str, situation: dict) -> dict | None:
    prompt = (
        "You are the night operator for a telemetry rig watching a Roblox client. "
        "Pick exactly ONE action from this catalog:\n"
        + "\n".join(f"- {name}: {desc}" for name, desc in PLAYBOOK.items())
        + "\n\nSituation JSON:\n"
        + json.dumps(situation)[:3000]
        + '\n\nReply with JSON only: {"action": "<name>", "reason": "<max 20 words>"}'
    )
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False, "think": False,
                          "options": {"temperature": 0.0, "num_predict": 90},
                          "keep_alive": "30m"}).encode()
    request = urllib.request.Request(f"{OLLAMA}/api/generate", data=payload,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            text = (json.loads(response.read().decode("utf-8")).get("response") or "").strip()
    except Exception:  # noqa: BLE001
        return None
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        decision = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    action = str(decision.get("action", "")).strip()
    if action not in PLAYBOOK:
        return None
    return {"action": action, "reason": str(decision.get("reason", ""))[:160], "source": "qwen"}


def rules(situation: dict) -> dict:
    """Deterministic fallback, ordered by severity."""
    checks = situation.get("checks", {})
    ages = checks.get("heartbeats", {}) or {}
    if (checks.get("app", {}) or {}).get("doctor") == "sign_in":
        return {"action": "app_repair", "source": "rules", "reason": "rule: app signed out"}
    if ages.get("night_runner") and ages["night_runner"] > 45:
        return {"action": "restart_night_runner", "source": "rules", "reason": "rule: stale night heartbeat"}
    if not (checks.get("ollama", {}) or {}).get("up", True):
        return {"action": "start_ollama", "source": "rules", "reason": "rule: ollama down"}
    if not (checks.get("app", {}) or {}).get("running", True):
        return {"action": "start_app", "source": "rules", "reason": "rule: app down"}
    if (checks.get("disk", {}) or {}).get("free_gb", 99) < 8:
        return {"action": "prune", "source": "rules", "reason": "rule: low disk"}
    if not (checks.get("client", {}) or {}).get("running", True):
        return {"action": "start_roblox", "source": "rules", "reason": "rule: client closed"}
    return {"action": "report_only", "source": "rules", "reason": "rule: nothing to fix"}


def fresh_health() -> dict:
    """A stale or missing health file makes the model guess; refresh it first."""
    sup = supervisor()
    existing = load(HEALTH, {})
    try:
        age = (now() - datetime.fromisoformat(existing.get("at", ""))).total_seconds()
    except (TypeError, ValueError):
        age = 10 ** 6
    if age > 600:
        try:
            return sup.check_cycle(console=False).get("checks", {})
        except Exception:  # noqa: BLE001
            return existing.get("checks", {})
    return existing.get("checks", {})


def cycle(model: str, force_offline: bool) -> dict:
    ensure()
    is_online = False if force_offline else online()
    mode = load(MODE, {})
    situation = {
        "at": stamp(),
        "online": is_online,
        "mode_file": mode,
        "checks": fresh_health(),
        "last_night_runs": tail(RUNS, 6),
        "needs_human": (STATE / "supervisor" / "NEEDS_HUMAN.md").exists(),
    }

    if is_online and not force_offline:
        record = {"at": stamp(), "online": True, "action": "handoff_to_agent",
                  "reason": "internet is up; qwen stands down"}
        DECISIONS.parent.mkdir(parents=True, exist_ok=True)
        with DECISIONS.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + chr(10))
        return record

    data = budget()
    if not spend(data):
        return {"at": stamp(), "online": False, "action": "budget_exhausted",
                "reason": "hourly/nightly action cap reached"}

    decision = ask_qwen(model, situation) or rules(situation)
    sup = supervisor()
    function = getattr(sup, decision["action"], None)
    result = {}
    try:
        if function is not None:
            result = function() or {}
        elif decision["action"] == "report_only":
            result = {"ok": True}
    except Exception as error:  # noqa: BLE001
        result = {"error": repr(error)[:200]}

    record = {"at": stamp(), "online": False, "action": decision["action"],
              "reason": decision["reason"], "result": result,
              "source": decision.get("source", "rules")}
    ensure()
    with DECISIONS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + chr(10))
    HEARTBEAT.write_text(json.dumps({"at": stamp(), "action": decision["action"]}), encoding="utf-8")
    try:
        if DECISIONS.stat().st_size > 1_000_000:
            lines = DECISIONS.read_text(encoding="utf-8").splitlines()[-400:]
            DECISIONS.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    except OSError:
        pass
    return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="offline dispatcher (qwen)")
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--force-offline", action="store_true")
    parser.add_argument("--interval", type=float, default=900.0)
    args = parser.parse_args(argv)

    if args.once or args.force_offline:
        print(json.dumps(cycle(args.model, args.force_offline), indent=1))
        return 0

    print(f"qwen agent up · {args.model} · every {args.interval}s")
    while True:
        try:
            record = cycle(args.model, False)
            print(f"  {record.get('action')} · {record.get('reason', '')[:80]}")
        except Exception as error:  # noqa: BLE001
            print(f"cycle error: {error!r}")
        if STOP.exists():
            print("stop file present; qwen idle")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
