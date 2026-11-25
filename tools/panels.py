#!/usr/bin/env python3
"""Post the three interactive panels (verify, tickets, products).

Same payloads the bot's `panels()` sends, but runnable from a machine that cannot
start the gateway. Buttons only do something while the bot process is up.

  python tools/panels.py            # post all three
  python tools/panels.py --clear    # empty the channels first
"""
from __future__ import annotations

import argparse
import json
import pathlib
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = json.loads((ROOT / "state" / "discord.json").read_text())
TOKEN = (ROOT / "secrets" / "discord.token").read_text().strip()
API = "https://discord.com/api/v10"
ACCENT = 0x7C5CFF


def api(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bot " + TOKEN)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "LyphenPanels")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        print("  !", method, path, exc.code, exc.read().decode()[:200])
        return None


def embed(title: str, description: str, fields=None, footer: str | None = None):
    data = {"title": title, "description": description[:4000], "color": ACCENT}
    if fields:
        data["fields"] = [{"name": n, "value": v[:1000], "inline": i} for n, v, i in fields]
    if footer:
        data["footer"] = {"text": footer}
    return {"embeds": [data]}


def button(label: str, cid: str, style: int = 2, emoji: str | None = None):
    item = {"type": 2, "label": label, "style": style, "custom_id": cid}
    if emoji:
        item["emoji"] = {"name": emoji}
    return item


def clear(channel_id: str):
    for _ in range(12):
        batch = api("GET", f"/channels/{channel_id}/messages?limit=100") or []
        if not batch:
            return
        for message in batch:
            api("DELETE", f"/channels/{channel_id}/messages/{message['id']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clear", action="store_true")
    args = ap.parse_args()
    channels = STATE["channels"]

    posts = [
        ("verify", {
            **embed("Verification",
                    "Click below to accept the rules and unlock the server."
                    + chr(10) + chr(10) +
                    "1. Be decent." + chr(10) + "2. No leaks or resale." + chr(10) +
                    "3. Support goes through the ticket buttons." + chr(10) +
                    "4. Test risky builds on an alt.",
                    footer="reacting works too, the button is just faster"),
            "components": [{"type": 1, "components": [button("Accept rules", "verify:accept", 3, "\u2705")]}],
        }),
        ("support", {
            **embed("Open a ticket",
                    "**Purchase** — buy the executor, the menu, the spoofer, or all three."
                    + chr(10) +
                    "**Support** — a bug, a crash, a script that will not run. Opens a short form,"
                    " nothing to copy." + chr(10) +
                    "**Bug report** — a reproducible issue with steps."),
            "components": [{"type": 1, "components": [
                button("Purchase", "ticket:buy", 3, "\U0001f6d2"),
                button("Support", "ticket:help", 2, "\U0001f3ab"),
                button("Bug report", "ticket:bug", 2, "\U0001f41b"),
            ]}],
        }),
        ("pricing", {
            **embed("what's for sale", "", fields=[
                ("\U0001f9e9 executor \u00b7 **$10**",
                 "1000 R$" + chr(10) + "manual map injection" + chr(10) + "luau runner + editor"
                 + chr(10) + "console, files, http, hooks", True),
                ("\U0001f4cd mod menu \u00b7 **$4**",
                 "400 R$" + chr(10) + "panel with 14 scripts inside" + chr(10) +
                 "tabs, sliders, keybinds, esp" + chr(10) + "load and go, any game", True),
                ("\U0001f9f9 spoofer \u00b7 **$6**",
                 "600 R$" + chr(10) + "clears device and account ids" + chr(10) +
                 "run before login" + chr(10) + "bans stop following you", True),
                ("\U0001f48e all three \u00b7 **$14**",
                 "1400 R$ — six less than buying them apart. one licence, one pc, every build "
                 "after this, update latency same for everyone.", False),
            ], footer="beta is free and unstable \u00b7 refund by ticket inside 48h if it never injected"),
            "components": [{"type": 1, "components": [
                button("Executor", "ticket:buy:executor", 2),
                button("All three \u00b7 $14", "ticket:buy:bundle", 3),
                button("Menu", "ticket:buy:modmenu", 2),
                button("Spoofer", "ticket:buy:spoofer", 2),
            ]}],
        }),
    ]

    for key, payload in posts:
        channel = channels.get(key)
        if not channel:
            print("  ? no channel for", key)
            continue
        if args.clear:
            clear(channel)
        api("POST", f"/channels/{channel}/messages", payload)
        print("  posted", key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
