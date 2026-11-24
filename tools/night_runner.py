#!/usr/bin/env python3
"""
night_runner.py — my decisions, executed by the machine while I'm offline.

Offline window is 21:00-08:00: the model cannot think then, so the thinking is
done BEFORE it: this runner executes a policy of jobs whose branching logic is
plain Python (if version changed -> rescan, if decoy read -> escalate, if crash
-> stop that experiment). No LLM is required to keep working.

qwen3:8b (Ollama) is optional garnish: it narrates MORNING.md and classifies
ambiguous job failures. It never decides what happens next.

Design rules against stalls:
  lock file           single instance, liveness-checked
  timeouts            every job killed at max_seconds
  budgets             nightly job count + total seconds + per-signature cap
  resumable           state.json on disk, restart continues
  polite exit         STOP file, cutoff, SIGINT/SIGTERM
  observable          heartbeat.json, runs.jsonl, MORNING.md

Modes
  python tools/night_runner.py --auto                 forever (supervisor)
  python tools/night_runner.py --force-night          allow heavy jobs now
  python tools/night_runner.py --once                 one job, then exit
  python tools/night_runner.py --report               print MORNING.md
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = Path(__file__).resolve().parent
STATE = ROOT / "state" / "night"
ARTIFACTS = STATE / "artifacts"
RUNS = STATE / "runs.jsonl"
STATE_FILE = STATE / "state.json"
HEARTBEAT = STATE / "heartbeat.json"
LOCK = STATE / "lock"
STOP = STATE / "stop"
MORNING = ROOT / "MORNING.md"

NIGHT_START_HOUR = 21
NIGHT_END_HOUR = 8
HEAVY = {"observer": 900, "decoy": 300, "intel": 900}   # 15 min sweep series / 5 min decoys
DEFAULT_BUDGET = {"max_jobs": 40, "max_seconds": int(11.5 * 3600), "max_per_signature": 2}
OLLAMA = "http://127.0.0.1:11434"


def ensure() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)


def now() -> datetime:
    return datetime.now()


def stamp() -> str:
    return now().strftime("%Y-%m-%dT%H:%M:%S")


def in_night(when: datetime | None = None) -> bool:
    hour = (when or now()).hour
    return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR


def night_id(when: datetime | None = None) -> str:
    when = when or now()
    if when.hour < NIGHT_END_HOUR:
        when -= timedelta(days=1)
    return when.strftime("%Y-%m-%d")


def online(timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=timeout):
            return True
    except OSError:
        return False


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"night": night_id(), "done": [], "spent": 0.0, "failures": 0}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


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


def acquire_lock() -> bool:
    if LOCK.exists():
        try:
            if pid_alive(int(json.loads(LOCK.read_text(encoding="utf-8")).get("pid", 0))):
                return False
        except (OSError, ValueError):
            pass
    LOCK.write_text(json.dumps({"pid": os.getpid(), "at": stamp()}), encoding="utf-8")
    return True


def release_lock() -> None:
    try:
        if json.loads(LOCK.read_text(encoding="utf-8")).get("pid") == os.getpid():
            LOCK.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def log_run(record: dict) -> None:
    ensure()
    with RUNS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + chr(10))
    try:
        if RUNS.stat().st_size > 2 * 1024 * 1024:
            lines = RUNS.read_text(encoding="utf-8").splitlines()[-400:]
            RUNS.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------
# the policy: my thinking, written as rules
# --------------------------------------------------------------------------


def job(kind: str, **kwargs) -> dict:
    spec = {"kind": kind, **kwargs}
    spec["signature"] = (kind + ":" + now().strftime("%Y%m%d%H") + ":"
                         + json.dumps({k: v for k, v in spec.items() if k != "signature"},
                                      sort_keys=True)[:100])
    return spec


def build_plan(off: bool, scale: float, heavy_override: bool = False) -> list[dict]:
    # connectivity decides how hard we collect, not the clock: the offline window
    # drifts (sometimes starts after 21:00), so "no internet" is the heavy mode and
    # "internet" is upkeep that stays out of the interactive work's way.
    heavy = off or heavy_override
    observer_seconds = int((HEAVY["observer"] if heavy else 60) * scale)
    decoy_seconds = int((HEAVY["decoy"] if heavy else 45) * scale)
    intel_seconds = int((HEAVY["intel"] if heavy else 300) * scale)

    plan = [
        # 1 Hz sweep-rate series: cheap, long, the main signal
        job("observer", seconds=observer_seconds, interval=1.0, max_seconds=observer_seconds + 60),
        # ground truth of our own footprint
        job("footprint", max_seconds=60),
        # does anything read foreign memory, at 20 Hz resolution
        job("decoy", seconds=decoy_seconds, max_seconds=decoy_seconds + 90),
        # 10 Hz protection churn: catches short-lived re-protections
        job("observer", seconds=300 if heavy else 30, interval=0.1,
            max_seconds=360),
    ]
    if not off:
        plan.append(job("intel", seconds=intel_seconds, interval=60,
                        max_seconds=intel_seconds + 120))
    return plan


def command_for(spec: dict, out_path: Path) -> list[str]:
    py = sys.executable
    kind = spec["kind"]
    if kind == "observer":
        return [py, str(TOOLS / "observer.py"), "--seconds", str(spec["seconds"]),
                "--interval", str(spec["interval"]), "--json", str(out_path)]
    if kind == "decoy":
        return [py, str(TOOLS / "observer.py"), "--seconds", str(spec["seconds"]),
                "--interval", "0.05", "--decoy", "--json", str(out_path)]
    if kind == "footprint":
        return [py, str(TOOLS / "footprint_audit.py"), "--json", str(out_path)]
    if kind == "intel":
        return [py, str(TOOLS / "intel_daemon.py"), "--duration", str(spec["seconds"]),
                "--interval", str(spec["interval"])]
    raise ValueError(f"unknown job kind {kind}")


def execute(spec: dict, state: dict) -> dict:
    folder = ARTIFACTS / night_id()
    folder.mkdir(parents=True, exist_ok=True)
    out_file = folder / f"{spec['kind']}-{int(time.time())}.json"
    cmd = command_for(spec, out_file)
    limit = float(spec.get("max_seconds", 900))

    started = time.monotonic()
    status, tail = "ok", ""
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=limit,
                                   cwd=str(ROOT), encoding="utf-8", errors="replace")
        tail = ((completed.stdout or "") + (completed.stderr or ""))[-3000:]
        if completed.returncode != 0:
            status = f"exit {completed.returncode}"
    except subprocess.TimeoutExpired:
        status = "timeout"
    except Exception as error:  # noqa: BLE001 - a job must never kill the shift
        status = f"error {error!r}"[:200]

    duration = round(time.monotonic() - started, 1)
    state["spent"] = round(state.get("spent", 0) + duration, 1)
    if status != "ok":
        state["failures"] = state.get("failures", 0) + 1
    state.setdefault("done", []).append(spec["signature"])
    save_state(state)

    record = {"at": stamp(), "kind": spec["kind"], "status": status, "seconds": duration,
              "artifact": str(out_file) if out_file.exists() else None, "tail": tail[-800:]}
    log_run(record)
    HEARTBEAT.write_text(json.dumps({"at": stamp(), "pid": os.getpid(),
                                     "job": spec["signature"]}), encoding="utf-8")
    return record


def run_shift(args) -> int:
    ensure()
    if not acquire_lock():
        return 0
    state = load_state()
    # a spent budget ends the shift, not the day: the 5-minute task relaunches us and
    # the cap is per shift, so daytime upkeep still happens after a long offline run.
    if state.get("night") != night_id() or state.get("spent", 0) >= args.max_seconds:
        state = {"night": night_id(), "done": [], "spent": 0.0, "failures": 0}
        save_state(state)

    scale = args.scale
    off = not online()
    print(f"night runner · {'offline' if off else 'online'} · "
          f"budget {args.max_jobs} jobs / {args.max_seconds:.0f}s")

    executed = 0
    try:
        while True:
            if STOP.exists():
                print("stop file present")
                break
            if executed >= args.max_jobs:
                print("job budget reached")
                break
            if state.get("spent", 0) >= args.max_seconds:
                print("time budget reached")
                break
            now_off = not online()
            if now_off != off:
                off = now_off
                print(f"  connectivity -> {'offline (heavy)' if off else 'online (upkeep)'}")

            plan = build_plan(off, scale, args.force_night)
            pick = None
            for candidate in plan:
                if state["done"].count(candidate["signature"]) >= DEFAULT_BUDGET["max_per_signature"]:
                    continue
                if state.get("spent", 0) + candidate["max_seconds"] <= args.max_seconds + 60:
                    pick = candidate
                    break
            if pick is None:
                print("queue exhausted for this budget")
                break

            record = execute(pick, state)
            executed += 1
            print(f"  [{record['status']:<9}] {record['kind']:<9} {record['seconds']:>6.1f}s  "
                  f"{record['tail'].strip()[-80:]}")
            write_morning(state, args)
            if args.once:
                break
            # offline: a 10-minute break spreads ~40 jobs across the window. online:
            # one light job every 45 minutes, so telemetry never competes with work.
            pause = args.gap if args.gap else (600 if (off or args.force_night) else 2700)
            if pause:
                for tick in range(int(pause / 5)):
                    if STOP.exists():
                        break
                    time.sleep(5)
                    # the cutoff drifts: notice a connectivity flip mid-pause at 30 s
                    # resolution instead of sitting out the whole 10-45 minute break
                    if tick % 6 == 5 and online() == off:
                        print("  connectivity changed mid-pause; re-planning")
                        break
                    # a 10-45 minute pause must not look like a dead worker
                    if tick % 12 == 11:
                        HEARTBEAT.write_text(json.dumps({"at": stamp(), "pid": os.getpid(),
                                                         "job": "pause"}), encoding="utf-8")
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        write_morning(state, args)
        release_lock()
        print(f"shift done · {executed} job(s) · {state.get('spent', 0):.0f}s spent")
    return 0


# --------------------------------------------------------------------------
# morning report (+ optional local narration)
# --------------------------------------------------------------------------


def latest_artifact(kind: str) -> dict | None:
    files = sorted(ARTIFACTS.rglob(f"{kind}-*.json"), key=lambda p: p.stat().st_mtime)
    for path in reversed(files):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return None


def summarise(data: dict | None) -> dict | None:
    if not data:
        return None
    measure = data.get("measure", data)
    decoy = data.get("decoy") or {}
    sweeps = measure.get("sweeps", {})
    return {
        "seconds": measure.get("seconds"),
        "sweeps": sweeps.get("count"),
        "median_gap": sweeps.get("median_interval"),
        "regions_flipped": measure.get("regions_flipped"),
        "decoy_hits": sum(row.get("hits", 0) for row in decoy.get("decoys", [])),
        "cpu_ms": measure.get("process_cpu_ms_total"),
    }


def ollama_text(model: str, prompt: str, timeout: float = 120.0) -> str | None:
    payload = json.dumps({
        "model": model, "prompt": prompt, "stream": False, "think": False,
        "options": {"temperature": 0.1, "num_predict": 200}, "keep_alive": "10m",
    }).encode()
    request = urllib.request.Request(f"{OLLAMA}/api/generate", data=payload,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        text = (data.get("response") or "").strip()
        return text or None
    except Exception:  # noqa: BLE001 - narration is optional
        return None


def write_morning(state: dict, args=None) -> None:
    jobs = []
    if RUNS.exists():
        for line in RUNS.read_text(encoding="utf-8").splitlines()[-80:]:
            try:
                jobs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    tonight = [j for j in jobs if j["at"][:10] >= night_id()]

    obs = summarise(latest_artifact("observer"))
    dec = summarise(latest_artifact("decoy"))
    footprint = latest_artifact("footprint")

    ok = sum(1 for j in tonight if j["status"] == "ok")
    lines = [f"# Night report · {night_id()} · generated {stamp()}", "",
             f"Jobs: {len(tonight)} ({ok} ok, {len(tonight) - ok} failed) · "
             f"spent {state.get('spent', 0):.0f}s · window {'night' if in_night() else 'day'}", ""]

    lines.append("## Signals")
    if obs:
        lines.append(f"- protection sweeps: **{obs['sweeps']}** in {obs['seconds']}s · "
                     f"median gap **{obs['median_gap']}s** · regions flipped {obs['regions_flipped']}")
    if dec:
        lines.append(f"- decoy reads (foreign memory touched): **{dec['decoy_hits']}** over {dec['seconds']}s")
    if footprint:
        lines.append(f"- footprint: {len(footprint.get('modules', []))} modules · "
                     f"{len(footprint.get('threads', []))} threads · "
                     f"{len((footprint.get('regions') or {}).get('private_exec', []))} private RX regions")
    if not obs and not footprint:
        lines.append("- no observations (client probably not running)")

    lines.append("")
    lines.append("## Jobs")
    for j in tonight[-12:]:
        lines.append(f"- `{j['kind']}` {j['seconds']:.0f}s → {j['status']}")
    if not tonight:
        lines.append("- none yet")

    lines.append("")
    lines.append("## Next actions")
    actions = []
    if dec and dec["decoy_hits"] > 0:
        actions.append("decoy pages were read → add the in-process VEH to capture the RIP that read them")
    if obs and not obs["sweeps"]:
        actions.append("no protection churn measured → repeat while in a game, not the menu")
    if any(j["status"] != "ok" for j in tonight):
        actions.append("a job failed → read state/night/runs.jsonl before queueing more")
    if not actions:
        actions.append("nothing anomalous → widen observation to in-game sessions and longer windows")
    for index, action in enumerate(actions, 1):
        lines.append(f"{index}. {action}")

    if getattr(args, "narrator", None):
        notes = ollama_text(
            args.narrator,
            "You are the night watch for a Roblox client telemetry rig. Using ONLY these numbers, "
            "write at most 4 short sentences of plain prose noting anything worth a human's attention. "
            "No bullets, no markdown, no speculation beyond the data.\n\n"
            + json.dumps({"observer": obs, "decoy": dec,
                          "jobs": [{"kind": j["kind"], "status": j["status"]} for j in tonight]}),
        )
        if notes:
            lines.extend(["", "## Night notes (qwen, local)", notes])

    lines.extend(["", "## Machine state", "```json",
                  json.dumps({"observer": obs, "decoy": dec,
                              "spent": state.get("spent"), "failures": state.get("failures")}, indent=1),
                  "```"])

    MORNING.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    (STATE / "last_night.json").write_text(
        json.dumps({"generated": stamp(), "observer": obs, "decoy": dec,
                    "jobs": len(tonight), "failures": state.get("failures", 0)}, indent=1),
        encoding="utf-8",
    )


def report() -> int:
    print(MORNING.read_text(encoding="utf-8") if MORNING.exists() else "no report yet")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="offline shift runner")
    parser.add_argument("--auto", action="store_true", help="supervisor mode (default)")
    parser.add_argument("--once", action="store_true", help="run one job and exit")
    parser.add_argument("--force-night", action="store_true", help="allow heavy jobs outside the window")
    parser.add_argument("--max-jobs", type=int, default=DEFAULT_BUDGET["max_jobs"])
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_BUDGET["max_seconds"])
    parser.add_argument("--gap", type=float, default=0.0)  # night pacing set below
    parser.add_argument("--scale", type=float, default=1.0, help="scale job durations")
    parser.add_argument("--narrator", metavar="OLLAMA_MODEL", default=None,
                        help="optional local model for the report prose, e.g. qwen3:8b")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args(argv)

    if args.report:
        return report()
    return run_shift(args)


if __name__ == "__main__":
    raise SystemExit(main())
