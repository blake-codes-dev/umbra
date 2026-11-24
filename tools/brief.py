#!/usr/bin/env python3
"""
brief.py — distill the whole project into one small file a fresh turn can read.

Why: 28 days of turns at 5-10k tokens each will compact this thread several times.
Memory that lives only in the conversation is lost; memory that lives in
state/agent/BRIEF.md survives compaction, app restarts and model changes.

    python tools/brief.py            # write state/agent/BRIEF.md and print it
    python tools/brief.py --quiet    # write only

The brief is capped at ~100 lines on purpose: cheap to read every turn.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
BRIEF = STATE / "agent" / "BRIEF.md"
TASKS = ["RobloxNightRunner", "RobloxSupervisor", "RobloxQwenShift"]


def load(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def age_min(value) -> float | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is not None:
        return (datetime.now(when.tzinfo) - when).total_seconds() / 60
    return (datetime.now() - when).total_seconds() / 60


def tail_lines(path: Path, count: int) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-count:]
    except OSError:
        return []


def processes() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
                             timeout=15, encoding="utf-8", errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return found
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) < 2:
            continue
        name = parts[0].lstrip('"').lower()
        try:
            pid = int(parts[1].strip('"'))
        except ValueError:
            continue
        found.setdefault(name, []).append(pid)
    return found


def task_state(name: str) -> str:
    try:
        out = subprocess.run(["schtasks", "/query", "/tn", name, "/fo", "list"],
                             capture_output=True, text=True, timeout=15,
                             encoding="utf-8", errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return "?"
    for line in out.splitlines():
        if line.lower().startswith("status:"):
            return line.split(":", 1)[1].strip()
    return "missing"


def milestone_rows() -> list[str]:
    try:
        text = (ROOT / "PLAN.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows = [line.strip() for line in text.splitlines() if line.startswith("| M")][:8]
    return [row[:150] for row in rows]


def build() -> str:
    mode = load(STATE / "mode.json")
    health = load(STATE / "supervisor" / "health.json")
    qwen_budget = load(STATE / "qwen" / "budget.json")
    next_up = load(STATE / "agent" / "next.json")
    procs = processes()

    def count(*names: str) -> int:
        return sum(len(procs.get(name, [])) for name in names)

    app = count("deepai code.exe")
    roblox = count("robloxplayerbeta.exe", "windowsplayer.exe")
    ollama = count("ollama.exe", "ollama app.exe")

    beats = {
        "night": age_min(load(STATE / "night" / "heartbeat.json").get("at")),
        "qwen": age_min(load(STATE / "qwen" / "heartbeat.json").get("at")),
        "agent": age_min(load(STATE / "agent" / "last_seen.json").get("at")),
        "intel": age_min(load(STATE / "intel" / "heartbeat.json").get("at")),
    }
    beat_line = " · ".join(f"{k} {'—' if v is None else f'{v:.0f}m'}" for k, v in beats.items())

    runs = []
    for line in tail_lines(STATE / "night" / "runs.jsonl", 5):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        runs.append(f"{row.get('at', '?')[-8:]} {row.get('kind', '?'):<9} "
                    f"{row.get('status', '?'):<8} {row.get('seconds', 0):.0f}s")
    kept = runs or ["no runs recorded yet"]

    decisions = []
    for line in tail_lines(STATE / "qwen" / "decisions.jsonl", 2):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        decisions.append(f"{row.get('at', '?')[-8:]} {row.get('action', '?')} "
                         f"({str(row.get('reason', ''))[:70]})")
    kept_decisions = decisions or ["none yet"]

    issues = []
    for line in tail_lines(STATE / "supervisor" / "events.jsonl", 40):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("kind") in ("issue", "mode"):
            issues.append(f"{row.get('at', '?')[-8:]} {row.get('kind')} {str(row.get('data'))[:90]}")
    kept_issues = issues[-3:] or ["none"]

    free_gb = shutil.disk_usage(ROOT).free / 2 ** 30
    mode_line = (f"{'ONLINE' if mode.get('online') else 'OFFLINE'} "
                 f"since {str(mode.get('since', '?'))[-8:]}")

    lines = [
        "# BRIEF — read this first (generated, do not hand-edit)",
        f"generated {time.strftime('%Y-%m-%d %H:%M')} · root {ROOT}",
        f"mode: {mode_line} · disk {free_gb:.0f} GB free",
        "",
        "## liveness",
        f"app {app} proc · roblox {roblox} proc · ollama {ollama} proc · "
        f"hung streak {health.get('app_hung_streak', 0)}",
        f"heartbeats: {beat_line}",
        "tasks: " + " · ".join(f"{name}={task_state(name)}" for name in TASKS),
        "",
        "## last jobs",
        *kept,
        "",
        "## qwen dispatcher",
        f"actions hour {qwen_budget.get('hour_count', 0)}/{qwen_budget.get('hour_cap', 6)} · "
        f"night {qwen_budget.get('night_count', 0)}/{qwen_budget.get('night_cap', 40)}",
        *kept_decisions,
        "",
        "## next",
        json.dumps(next_up) if next_up else "state/agent/next.json missing",
        "",
        "## milestones (PLAN.md §10)",
        *milestone_rows(),
        "",
        "## vault",
        f"drift +{load(STATE / 'vault' / 'drift.json').get('added', '?')} "
        f"-{load(STATE / 'vault' / 'drift.json').get('removed', '?')} "
        f"~{load(STATE / 'vault' / 'drift.json').get('changed', '?')} · backup "
        f"{load(STATE / 'vault' / 'backup.json').get('target', 'none')}",
        "",
        "## recent issues",
        *kept_issues,
        "",
        "## DIRECTIVES (standing human instructions)",
        *tail_lines(STATE / "agent" / "DIRECTIVES.md", 12),
        "",
        "## WORKLOG (last 6)",
        *tail_lines(ROOT / "WORKLOG.md", 6),
        "",
        "## protocol",
        "Read state/agent/RULES.md before acting. Update next.json + JOURNAL.md + "
        "last_seen.json before ending a turn.",
        "3 strikes: the same failure three times → write state/supervisor/NEEDS_HUMAN.md and stop.",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    BRIEF.parent.mkdir(parents=True, exist_ok=True)
    text = build()
    BRIEF.write_text(text, encoding="utf-8")
    if "--quiet" not in argv:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
