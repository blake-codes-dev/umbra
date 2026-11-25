#!/usr/bin/env python3
"""Presence beats, so one agent going quiet is visible instead of just quiet.

Neither agent is a process the other controls. If the Windows side dies mid-build, the container
side keeps working and waits forever on a reply that never comes; the cheapest defence is a beat
file plus a watchdog that shouts when a beat goes stale.

    python3 tools/pulse.py beat --as windows --note "pinning gettop for 2.748"
    python3 tools/pulse.py status
    python3 tools/pulse.py check          # watchdog: exit 1 and flag if anyone is stale

The note doubles as a resume point - if an agent dies, the next one reads its last note and
continues from there instead of restarting the investigation.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state", "agent", "presence.json")
ALERT = os.path.join(ROOT, "state", "agent", "ALERT.md")
STALE_SECONDS = float(os.environ.get("PULSE_STALE", 900))
SIDES = ("windows", "container")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def save(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(state, open(STATE, "w"), indent=1)


def beat(args):
    state = load()
    previous = state.get(args.side, {})
    state[args.side] = {"at": time.time(), "iso": now(), "note": args.note or previous.get("note", ""),
                        "alerted": False, "beats": previous.get("beats", 0) + 1}
    save(state)
    if os.path.exists(ALERT):
        try:
            os.remove(ALERT)
        except OSError:
            pass
    print("%s beat (%d) %s" % (args.side, state[args.side]["beats"], args.note or ""))


def staleness(entry):
    return time.time() - entry.get("at", 0)


def status(_args):
    state = load()
    for side in SIDES:
        entry = state.get(side)
        if not entry:
            print("%-10s never" % side)
            continue
        age = staleness(entry)
        print("%-10s %6.1f min  beats=%-3d %s" % (side, age / 60, entry.get("beats", 0),
                                                 entry.get("note", "")))


def check(_args):
    """Returns the list of sides that went stale since their last beat, and flags them once."""
    state = load()
    fresh = []
    for side in SIDES:
        entry = state.get(side)
        if not entry:
            continue
        if staleness(entry) > STALE_SECONDS:
            if not entry.get("alerted"):
                entry["alerted"] = True
                save(state)
                fresh.append((side, staleness(entry) / 60, entry.get("note", "")))
    if fresh:
        lines = ["# stale agent", "", "written %s by the watchdog" % now(), ""]
        for side, minutes, note in fresh:
            lines.append("- **%s** silent for %.0f min, last note: %s" % (side, minutes, note or "(none)"))
        lines.append("")
        lines.append("If this is the Windows side: it may be building, or it may have died mid-step.")
        lines.append("Resume from the last note above, or re-issue the step. Its work is on the shared")
        lines.append("disk, so nothing is lost - but nobody is going to reply until it beats again.")
        os.makedirs(os.path.dirname(ALERT), exist_ok=True)
        open(ALERT, "w").write("\n".join(lines) + "\n")
    for side, minutes, note in fresh:
        print("stale %s %.0f min %s" % (side, minutes, note))
    return 0


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("beat")
    p.add_argument("--as", dest="side", required=True, choices=list(SIDES))
    p.add_argument("--note")
    p.set_defaults(func=beat)
    p = sub.add_parser("status")
    p.set_defaults(func=status)
    p = sub.add_parser("check")
    p.set_defaults(func=check)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
