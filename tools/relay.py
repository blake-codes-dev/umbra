#!/usr/bin/env python3
"""Discord relay between the two Umbra agents, for things that need to be seen now.

The mailbox in state/agent/mail is the durable channel and needs no network. This one exists
because it is instant, it survives the two machines not sharing a disk, and the owner can watch it
from a phone. Both sides speak the same script; only --from differs.

    python3 tools/relay.py send "harness is green, rebuilding core.dll"
    python3 tools/relay.py tail                # new messages since the cursor
    python3 tools/relay.py watch               # block, print as they arrive
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANNEL = "1575012687878950992"          # #relay, guild 1494098236716089434
API = "https://discord.com/api/v10"
TAG = {"container": "container", "windows": "windows"}


def token():
    for candidate in (os.path.join(ROOT, "..", ".secrets", "discord.token"),
                      os.path.join(ROOT, ".secrets", "discord.token")):
        if os.path.exists(candidate):
            return open(candidate).read().strip()
    sys.exit("discord.token not found")


def call(path, payload=None, method=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(API + path, data=data,
                                     method=method or ("POST" if data else "GET"))
    request.add_header("Authorization", "Bot " + token())
    request.add_header("User-Agent", "UmbraRelay/1.0 (agent bridge)")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        sys.exit("discord %s: %s %s" % (path, error.code, error.read()[:200]))


def cursor_path(side):
    return os.path.join(ROOT, "state", "agent", "relay-%s.json" % side)


def send(args):
    text = args.text if args.text else sys.stdin.read()
    text = text.strip()
    body = text if text.startswith("[%s]" % TAG[args.sender]) else "[%s] %s" % (TAG[args.sender], text)
    result = call("/channels/%s/messages" % CHANNEL, {"content": body[:1900]})
    print("sent %s" % result.get("id", "?"))
    try:
        cursor = json.load(open(cursor_path(args.sender)))
    except (OSError, ValueError):
        cursor = {}
    cursor["last"] = result.get("id")
    cursor["at"] = time.time()
    json.dump(cursor, open(cursor_path(args.sender), "w"))


def fetch(last):
    path = "/channels/%s/messages?limit=50" % CHANNEL
    if last:
        path += "&after=" + last
    messages = call(path)
    return list(reversed(messages))


def tail(args):
    try:
        last = json.load(open(cursor_path(args.side))).get("last")
    except (OSError, ValueError):
        last = None
    messages = fetch(last)
    if not messages:
        print("(nothing new)")
        return
    for message in messages:
        content = message.get("content", "")
        if content.startswith("[%s]" % TAG[args.side]):
            continue
        print("%s  %s" % (message["timestamp"][11:19], content))
        last = message["id"]
    json.dump({"last": last, "at": time.time()}, open(cursor_path(args.side), "w"))


def watch(args):
    while True:
        tail(args)
        sys.stdout.flush()
        time.sleep(args.interval)


def main():
    parser = argparse.ArgumentParser(description="discord relay")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("send")
    p.add_argument("text", nargs="?")
    p.add_argument("--from", dest="sender", default="container", choices=list(TAG))
    p.set_defaults(func=send)

    p = sub.add_parser("tail")
    p.add_argument("--for", dest="side", default="container", choices=list(TAG))
    p.set_defaults(func=tail)

    p = sub.add_parser("watch")
    p.add_argument("--for", dest="side", default="container", choices=list(TAG))
    p.add_argument("--interval", type=float, default=3.0)
    p.set_defaults(func=watch)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
