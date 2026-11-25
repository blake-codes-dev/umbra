#!/usr/bin/env python3
"""Compose the prompt an agent wakes up with.

A restarted agent must not have to be told what to do, so the prompt is assembled from state that
is already on disk: the top of the queue, unread mail, the other side's last beat, and any alert.
That makes a restart a resume instead of a cold start.

    python3 tools/brief.py --for windows > prompt.txt
    python3 tools/brief.py --for container
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(ROOT, "state", "agent")

PREAMBLE = """You are the {side} agent on Lyphen (Roblox executor + in-game menu + spoofer). You were
started by the loop, possibly after a crash - so this prompt is your whole memory of the past.

Rules: never say a task is finished while work remains, pad nothing, run things instead of asking,
and after every meaningful step run `{beat}`. If you are blocked for real, beat with
`--state stuck` and say why. End your turn only when the queue item is done or genuinely blocked.

"""
TASKS = {}


def read(path):
    try:
        return open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def unread_mail(side):
    """Messages the other side queued for us, without consuming the cursor (the agent's own tool
    advances it when it reads properly)."""
    text = read(os.path.join(AGENT, "mail", "to-%s.md" % side))
    if not text:
        return "(nothing)"
    blocks = [b.strip() for b in text.split("## ") if b.strip()]
    return "\n\n".join("## " + b for b in blocks[-4:]) or "(nothing)"


def queue():
    text = read(os.path.join(AGENT, "TODO.md"))
    lines = [line for line in text.splitlines() if line.startswith("- [ ]")]
    return "\n".join(lines[:5]) or "(queue empty: pick the next thing from next.json)"


def beats():
    try:
        data = json.load(open(os.path.join(AGENT, "presence.json")))
    except (OSError, ValueError):
        data = {}
    out = []
    for side in ("windows", "container"):
        entry = data.get(side)
        if not entry:
            out.append("%s: never beaten" % side)
            continue
        age = (datetime.now(timezone.utc).timestamp() - entry.get("at", 0)) / 60
        out.append("%s: %.0f min ago, state=%s, note=%s" % (side, age, entry.get("state", "working"),
                                                            entry.get("note", "")))
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--for", dest="side", required=True, choices=["windows", "container"])
    args = parser.parse_args()

    beat = "tools\\pulse.bat" if args.side == "windows" else "python3 tools/pulse.py beat --as container"
    parts = [PREAMBLE.format(side=args.side, beat=beat)]
    parts.append("QUEUE (do the top one):\n" + queue())
    parts.append("AGENT CLOCKS:\n" + beats())
    alert = read(os.path.join(AGENT, "ALERT.md"))
    if alert:
        parts.append("ALERT (someone went stale - read it):\n" + alert.strip())
    parts.append("UNREAD MAIL for you:\n" + unread_mail(args.side))
    parts.append("Tag the other side with `[%s]` on the relay. Answer their questions before new work."
                 % args.side)
    sys.stdout.write("\n\n".join(parts) + "\n")


if __name__ == "__main__":
    main()
