#!/usr/bin/env python3
"""#status board: one live message per agent, edited in place, green / amber / red.

Reads the presence beats written by tools/pulse.py and rewrites two messages owned by the bot.
Only the bot edits them, so the channel history stays two lines and the colour is always current.

    python3 tools/status.py update
    python3 tools/status.py init      # first run: create the channel messages and remember them

Colour comes from the beat age, not from a claim: an agent that says "working" and then dies goes
amber and then red on its own.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state", "agent", "status.json")
PRESENCE = os.path.join(ROOT, "state", "agent", "presence.json")
API = "https://discord.com/api/v10"

WORKING = 600          # under ten minutes: green
QUIET = 1800           # under thirty: amber, above: red

SIDES = {
    "windows": ("Windows agent", "live client, MSVC, the box"),
    "container": ("Container agent", "source, docs, store, releases"),
}


def token():
    for path in (os.path.join(ROOT, "..", ".secrets", "discord.token"),
                 os.path.join(ROOT, ".secrets", "discord.token")):
        if os.path.exists(path):
            return open(path).read().strip()
    sys.exit("discord.token not found")


def call(path, payload=None, method=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(API + path, data=data,
                                     method=method or ("POST" if data else "GET"))
    request.add_header("Authorization", "Bot " + token())
    request.add_header("User-Agent", "LyphenStatus/1.0")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        sys.exit("discord %s: %s %s" % (path, error.code, error.read()[:200]))


def presence():
    try:
        return json.load(open(PRESENCE))
    except (OSError, ValueError):
        return {}


def shape(entry):
    if not entry:
        return "\U0001F534", "offline", "never beat"
    age = time.time() - entry.get("at", 0)
    when = entry.get("iso", "?")
    note = entry.get("note") or "(no note)"
    if entry.get("state") == "stuck":
        return "\U0001F534", "stuck", "%s\n%s" % (note, when)
    if age < WORKING:
        return "\U0001F7E2", "working", "%s\nbeat %s" % (note, when)
    if age < QUIET:
        return "\U0001F7E1", "quiet for %d min" % (age // 60), "%s\nbeat %s" % (note, when)
    return "\U0001F534", "offline for %d min" % (age // 60), "%s\nbeat %s" % (note, when)


def render(side, entry):
    title, role = SIDES[side]
    dot, word, detail = shape(entry)
    return "### %s **%s**\n`%s` \u00b7 %s\n>>> %s" % (dot, title, word, role, detail)


def init(_args):
    state = {"messages": {}}
    for side in SIDES:
        message = call("/channels/%s/messages" % channel(), {"content": render(side, presence().get(side))})
        state["messages"][side] = message["id"]
    json.dump(state, open(STATE, "w"), indent=1)
    print("initialised %s" % channel())


def channel():
    try:
        return json.load(open(os.path.join(ROOT, "state", "discord.json")))["channels"]["status_board"]
    except (OSError, ValueError, KeyError):
        sys.exit("state/discord.json has no channels.status_board")


ROTATE = 3000          # discord refuses edits to messages older than an hour, so start a new one


def update(_args):
    try:
        state = json.load(open(STATE))
    except (OSError, ValueError):
        state = {"messages": {}, "born": {}}
    state.setdefault("born", {})
    here = presence()
    for side in SIDES:
        body = render(side, here.get(side))
        message = state["messages"].get(side)
        if message and time.time() - state["born"].get(side, 0) > ROTATE:
            message = None                                  # age it out before discord refuses
        if not message:
            created = call("/channels/%s/messages" % channel(), {"content": body})
            state["messages"][side] = created["id"]
            state["born"][side] = time.time()
        else:
            try:
                call("/channels/%s/messages/%s" % (channel(), message), {"content": body},
                     method="PATCH")
            except SystemExit:
                created = call("/channels/%s/messages" % channel(), {"content": body})
                state["messages"][side] = created["id"]
                state["born"][side] = time.time()
    nudge(state, here)
    json.dump(state, open(STATE, "w"), indent=1)


NUDGE_AFTER = 3600          # a peer red this long gets called out once an hour
NUDGE_REPEAT = 3600


def nudge(state, here):
    """A silent agent must be audible, not just painted red. One line in the relay per hour per
       dead side, so the survivor and the owner both see it without watching a board."""
    sent = state.setdefault("nudged", {})
    relay = None
    for side in SIDES:
        entry = here.get(side) or {}
        age = time.time() - entry.get("at", 0)
        if age < NUDGE_AFTER and entry.get("state") != "stuck":
            sent.pop(side, None)
            continue
        if time.time() - sent.get(side, 0) < NUDGE_REPEAT:
            continue
        if relay is None:
            try:
                relay = json.load(open(os.path.join(ROOT, "state", "discord.json")))["channels"]["relay"]
            except (OSError, ValueError, KeyError):
                return
        note = (entry.get("note") or "no note")[:120]
        call("/channels/%s/messages" % relay, {
            "content": "@agents **%s** has been quiet %d min - last note: %s"
                       % (side, age // 60, note)})
        sent[side] = time.time()


def main():
    args = sys.argv[1:] or ["update"]
    if os.path.exists(STATE):
        update(args)
    else:
        init(args)
    print("updated %s" % datetime.now(timezone.utc).strftime("%H:%M:%SZ"))


if __name__ == "__main__":
    main()
