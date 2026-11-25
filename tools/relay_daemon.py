#!/usr/bin/env python3
"""#relay <-> mailbox bridge.

Two agents, two habits: the Windows one reads files, I read files, the owner reads Discord. This
daemon makes any of those a valid way in, so nobody has to care which channel the other used.

    #relay message from a human or from the windows agent  ->  appended to state/agent/mail/to-container.md
    a new message in state/agent/mail/to-windows.md        ->  echoed to #relay as [container]

    python3 tools/relay_daemon.py --once      # one pass, for a cron or a hand check
    python3 tools/relay_daemon.py             # loop
    nohup python3 tools/relay_daemon.py >> state/agent/relay.log 2>&1 &

Dedupe is by text hash, so a message that arrives down both paths is only seen once.
"""
import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIL = os.path.join(ROOT, "state", "agent", "mail")
STATE = os.path.join(ROOT, "state", "agent", "relay-state.json")
CHANNEL = "1575012687878950992"
API = "https://discord.com/api/v10"
BOT_MARK = "[container]"
INBOUND_MARK = "[windows]"


def token():
    for path in (os.path.join(ROOT, "..", ".secrets", "discord.token"),
                 os.path.join(ROOT, ".secrets", "discord.token")):
        if os.path.exists(path):
            return open(path).read().strip()
    sys.exit("discord.token not found")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def call(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(API + path, data=data, method="POST" if data else "GET")
    request.add_header("Authorization", "Bot " + token())
    request.add_header("User-Agent", "UmbraRelay/1.0 (agent bridge)")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        raise RuntimeError("discord %s: %s %s" % (path, error.code, error.read()[:200]))


def load_state():
    try:
        state = json.load(open(STATE))
    except (OSError, ValueError):
        state = {}
    state.setdefault("discord_last", None)
    state.setdefault("mirrored", 0)
    state.setdefault("posted", [])
    return state


def save_state(state):
    state["posted"] = state["posted"][-300:]
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(state, open(STATE, "w"), indent=1)


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
    return [chunk for chunk in chunks if chunk.strip()]


def append_mail(side, sender, body):
    path = os.path.join(MAIL, "to-%s.md" % side)
    os.makedirs(MAIL, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("## %s \u00b7 %s\n\n%s\n\n" % (now(), sender, body.strip()))


def digest(text):
    return hashlib.sha1(text.strip().encode()).hexdigest()[:16]


def pull(state, own_id):
    """#relay -> mailbox. Anything a human wrote, and anything the windows agent tagged."""
    path = "/channels/%s/messages?limit=50" % CHANNEL
    if state["discord_last"]:
        path += "&after=" + state["discord_last"]
    messages = list(reversed(call(path)))
    delivered = 0
    for message in messages:
        state["discord_last"] = message["id"]
        content = (message.get("content") or "").strip()
        author = message.get("author", {})
        who = author.get("global_name") or author.get("username") or "unknown"
        if not content:
            print("%s  . %s: empty" % (now(), who))
            continue
        if content.startswith(BOT_MARK):
            print("%s  . %s: our echo" % (now(), who))
            continue
        if author.get("id") == own_id and not content.startswith(INBOUND_MARK):
            print("%s  . %s: bot chatter" % (now(), who))
            continue
        mark = digest(content)
        if mark in state["posted"]:
            print("%s  . %s: already seen" % (now(), who))
            continue
        append_mail("container", "relay:" + who, content)
        state["posted"].append(mark)
        delivered += 1
        print("%s  <- %s: %s" % (now(), who, content.splitlines()[0][:90]))
    return delivered


def push(state):
    """mailbox -> #relay. Only to-windows messages, only ones not yet echoed."""
    path = os.path.join(MAIL, "to-windows.md")
    if not os.path.exists(path):
        return 0
    messages = split_messages(open(path, encoding="utf-8").read())
    sent = 0
    for message in messages[state["mirrored"]:]:
        body = message.split("\n", 1)[-1].strip() if message.startswith("## ") else message
        if not body:
            continue
        mark = digest(body)
        if mark not in state["posted"]:
            call("/channels/%s/messages" % CHANNEL,
                 {"content": (BOT_MARK + " " + body)[:1900]})
            state["posted"].append(mark)
            sent += 1
            print("%s  -> #relay: %s" % (now(), body.splitlines()[0][:90]))
        state["mirrored"] += 1
    return sent


def interaction_text(payload):
    """The /say command carries its own text, so reading it needs no message-content intent."""
    data = payload.get("data") or {}
    if (data.get("name") or "").lower() != "say":
        return None
    options = {option["name"]: option.get("value") for option in data.get("options", [])}
    text = (options.get("text") or "").strip()
    member = payload.get("member") or {}
    user = member.get("user") or payload.get("user") or {}
    who = user.get("global_name") or user.get("username") or "unknown"
    return who, text


def respond(payload, message):
    try:
        call("/interactions/%s/%s/callback" % (payload["id"], payload["token"]),
             {"type": 4, "data": {"content": message[:1900]}})
    except Exception as error:
        print("%s  ! reply failed: %s" % (now(), error))


async def gateway(state):
    """Interactions only: intents stay 0, so no portal toggle is needed for /say to work."""
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        print("%s  gateway: no websockets module, /say disabled (run me with /lsiopy/bin/python3)" % now())
        return
    url = call("/gateway/bot")["url"] + "/?v=10&encoding=json"
    while True:
        try:
            async with connect(url, max_size=2 ** 22, ping_interval=None) as socket:
                hello = json.loads(await socket.recv())
                interval = hello["d"]["heartbeat_interval"] / 1000.0
                sequence = [None]

                async def beat():
                    while True:
                        await asyncio.sleep(interval)
                        await socket.send(json.dumps({"op": 1, "d": sequence[0]}))

                beat_task = asyncio.create_task(beat())
                await socket.send(json.dumps({"op": 2, "d": {
                    "token": token(), "intents": 0,
                    "properties": {"os": "linux", "browser": "umbra", "device": "umbra"}}}))
                async for raw in socket:
                    payload = json.loads(raw)
                    if payload.get("s") is not None:
                        sequence[0] = payload["s"]
                    op, kind = payload.get("op"), payload.get("t")
                    if kind == "READY":
                        print("%s  gateway ready as %s" % (now(), payload["d"]["user"]["username"]))
                    elif kind == "INTERACTION_CREATE":
                        parsed = interaction_text(payload["d"])
                        if parsed is None:
                            respond(payload["d"], "only /say is wired up")
                            continue
                        who, text = parsed
                        if not text:
                            respond(payload["d"], "empty")
                            continue
                        append_mail("container", "relay:" + who, text)
                        state["posted"].append(digest(text))
                        save_state(state)
                        print("%s  <- /say %s: %s" % (now(), who, text[:90]))
                        respond(payload["d"], "logged")
                    elif op in (7, 9):
                        break
                beat_task.cancel()
        except Exception as error:
            print("%s  gateway error: %s" % (now(), error))
        await asyncio.sleep(3)


async def poll_loop(state, own_id, interval):
    while True:
        try:
            pull(state, own_id)
            push(state)
            save_state(state)
        except Exception as error:
            print("%s  poll error: %s" % (now(), error))
        await asyncio.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="relay bridge")
    parser.add_argument("--once", action="store_true", help="one REST pass, no gateway")
    parser.add_argument("--interval", type=float, default=3.0)
    args = parser.parse_args()

    own_id = call("/users/@me").get("id")
    state = load_state()
    print("%s  relay up as %s, last=%s mirrored=%d" % (now(), own_id, state["discord_last"], state["mirrored"]))
    if args.once:
        pull(state, own_id)
        push(state)
        save_state(state)
        return
    async def both():
        await asyncio.gather(poll_loop(state, own_id, args.interval), gateway(state))

    asyncio.run(both())


if __name__ == "__main__":
    main()
