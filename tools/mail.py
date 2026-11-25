#!/usr/bin/env python3
"""Mailbox between the two Lyphen agents (container/Linux and Windows).

The container mounts C:\\ at /config over 9p, so a file written here is readable on the Windows
side at the matching path *immediately* - no git, no network, no polling delay. This is the
primary channel; git carries the durable record and Discord carries alerts.

    python3 tools/mail.py send --to windows "core.dll rebuilt, please run the harness"
    python3 tools/mail.py read --for windows          # unread only, advances the cursor
    python3 tools/mail.py read --for windows --all    # everything, no cursor change
    python3 tools/mail.py watch --for windows         # block, print messages as they arrive

Messages are append-only markdown; nothing is ever rewritten, so both sides can append safely.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIL = os.path.join(ROOT, "state", "agent", "mail")


def inbox(side):
    return os.path.join(MAIL, "to-%s.md" % side)


def cursor_path(side):
    return os.path.join(MAIL, ".cursor-%s.json" % side)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def count_messages(text):
    return sum(1 for line in text.splitlines() if line.startswith("## "))


def send(args):
    side = args.to
    body = args.text if args.text else sys.stdin.read()
    if not body.strip():
        sys.exit("nothing to send")
    sender = args.sender or "container"
    os.makedirs(MAIL, exist_ok=True)
    header = "## %s \u00b7 %s\n\n" % (now(), sender)
    with open(inbox(side), "a", encoding="utf-8") as handle:
        handle.write(header)
        handle.write(body.rstrip() + "\n\n")
    print("sent to %s (%s)" % (side, inbox(side)))


def split_messages(text):
    chunks, current = [], []
    for line in text.splitlines():
        if line.startswith("## "):
            if current:
                chunks.append("\n".join(current).strip())
            current = [line]
        elif current:
            current.append(line)
    if current:
        chunks.append("\n".join(current).strip())
    return [c for c in chunks if c.strip()]


def read(args):
    side = args.side
    path = inbox(side)
    if not os.path.exists(path):
        print("(empty inbox: %s)" % path)
        return
    text = open(path, encoding="utf-8").read()
    messages = split_messages(text)
    start = 0
    if not args.all:
        try:
            start = json.load(open(cursor_path(side)))["seen"]
        except (OSError, KeyError, ValueError):
            start = 0
    for message in messages[start:]:
        print(message)
        print()
    if args.all:
        print("(%d messages total)" % len(messages))
        return
    if start < len(messages):
        json.dump({"seen": len(messages), "at": now()}, open(cursor_path(side), "w"))
        print("(%d new, cursor at %d)" % (len(messages) - start, len(messages)))
    else:
        print("(nothing new)")


def watch(args):
    seen = -1
    while True:
        path = inbox(args.side)
        if os.path.exists(path):
            messages = split_messages(open(path, encoding="utf-8").read())
            if seen < 0:
                seen = len(messages)
            while seen < len(messages):
                print(messages[seen])
                print()
                seen += 1
        time.sleep(args.interval)


def status(_args):
    for side in ("windows", "container"):
        path = inbox(side)
        messages = split_messages(open(path, encoding="utf-8").read()) if os.path.exists(path) else []
        try:
            seen = json.load(open(cursor_path(side)))["seen"]
        except (OSError, KeyError, ValueError):
            seen = 0
        print("%-10s %2d messages, %2d unread  %s" % (side, len(messages), len(messages) - seen, path))


def main():
    parser = argparse.ArgumentParser(description="agent mailbox")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("send")
    p.add_argument("--to", required=True, choices=["windows", "container"])
    p.add_argument("--from", dest="sender", choices=["windows", "container"])
    p.add_argument("text", nargs="?")
    p.set_defaults(func=send)

    p = sub.add_parser("read")
    p.add_argument("--for", dest="side", required=True, choices=["windows", "container"])
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=read)

    p = sub.add_parser("watch")
    p.add_argument("--for", dest="side", required=True, choices=["windows", "container"])
    p.add_argument("--interval", type=float, default=1.0)
    p.set_defaults(func=watch)

    p = sub.add_parser("status")
    p.set_defaults(func=status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
