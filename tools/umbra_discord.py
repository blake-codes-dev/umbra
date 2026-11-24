#!/usr/bin/env python3
"""Build the Umbra product server: categories, channels, roles, gates, welcome copy.

Idempotent. Uses only the bot token in secrets/discord.token.
  python tools/umbra_discord.py build
  python tools/umbra_discord.py seed
  python tools/umbra_discord.py invite
  python tools/umbra_discord.py post --channel changelog --text "..."
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "secrets"
STATE = ROOT / "state" / "discord.json"
API = "https://discord.com/api/v10"
GUILD_ID = "1494098236716089434"

ROLES = [
    ("Owner", 0x7C5CFF, True),
    ("Maintainer", 0xC084FC, True),
    ("Beta", 0x22D3EE, False),
    ("Buyer", 0xF59E0B, False),
]
CATEGORIES = [("INFORMATION", 0x7C5CFF), ("COMMUNITY", 0x22D3EE), ("STAFF", 0x6B7280)]
CHANNELS = [
    ("welcome", "INFORMATION", "Start here · rules · access", None),
    ("announcements", "INFORMATION", "Releases, downtime, transitions", None),
    ("changelog", "INFORMATION", "Every build: version · hash · fixes · known issues", None),
    ("downloads", "INFORMATION", "Role-gated builds", "gated"),
    ("general", "COMMUNITY", "Chat", None),
    ("support", "COMMUNITY", "Tickets: version + error + steps", None),
    ("media", "COMMUNITY", "Clips and screenshots", None),
    ("staff", "STAFF", "Internal: roadmap, keys, incidents", "staff"),
]
VIEW = 0x400
READ_HISTORY = 0x10000


def tok() -> str:
    return (SECRETS / "discord.token").read_text(encoding="utf-8").strip()


def api(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    for attempt in range(6):
        req = urllib.request.Request(API + path, data=data, method=method)
        req.add_header("Authorization", "Bot " + tok())
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "UmbraBuilder/1.0")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 429:
                try:
                    wait = float(json.loads(body).get("retry_after", 1.0))
                except Exception:
                    wait = 1.0
                time.sleep(wait + 0.2)
                continue
            if 500 <= e.code < 600:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise SystemExit(f"{method} {path} -> {e.code} {body[:300]}")
    raise SystemExit(f"{method} {path}: retries exhausted")


def load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"guild_id": GUILD_ID, "channels": {}, "roles": {}, "categories": {}}


def save(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def ensure_role(state, name, colour, hoist):
    for r in api("GET", f"/guilds/{state['guild_id']}/roles") or []:
        if r["name"] == name:
            state["roles"][name] = r["id"]
            return r["id"]
    r = api("POST", f"/guilds/{state['guild_id']}/roles",
            {"name": name, "color": colour, "hoist": hoist, "mentionable": True})
    state["roles"][name] = r["id"]
    print("  + role", name)
    return r["id"]


def ensure_category(state, name):
    for c in api("GET", f"/guilds/{state['guild_id']}/channels") or []:
        if c.get("type") == 4 and c["name"] == name:
            state["categories"][name] = c["id"]
            return c["id"]
    c = api("POST", f"/guilds/{state['guild_id']}/channels",
            {"name": name, "type": 4, "position": len(state["categories"])})
    state["categories"][name] = c["id"]
    print("  + category", name)
    return c["id"]


def ensure_channel(state, name, category, topic):
    for c in api("GET", f"/guilds/{state['guild_id']}/channels") or []:
        if c.get("type") == 0 and c["name"] == name:
            state["channels"][name] = c["id"]
            return c["id"]
    c = api("POST", f"/guilds/{state['guild_id']}/channels",
            {"name": name, "type": 0, "topic": topic,
             "parent_id": state["categories"][category]})
    state["channels"][name] = c["id"]
    print("  + #" + name)
    return c["id"]


def set_overwrites(state, name, allow_roles, deny_everyone):
    cid = state["channels"][name]
    overwrites = [{"id": state["guild_id"], "type": 0, "allow": "0",
                   "deny": str(deny_everyone)}]
    for role in allow_roles:
        rid = state["roles"].get(role)
        if rid:
            overwrites.append({"id": rid, "type": 0, "allow": str(VIEW | READ_HISTORY),
                               "deny": "0"})
    api("PATCH", f"/channels/{cid}", {"permission_overwrites": overwrites})


def cleanup_defaults(state):
    keep = set(state["channels"].values()) | set(state["categories"].values())
    for c in api("GET", f"/guilds/{state['guild_id']}/channels") or []:
        if c["id"] in keep:
            continue
        if c.get("type") in (0, 2, 4):
            api("DELETE", f"/channels/{c['id']}")
            print("  - removed default", c.get("name"))


WELCOME = (
    "**UMBRA** — quietly undetected.\n\n"
    "Umbra is a small, private executor and mod menu for Roblox. No key system, no ads, "
    "no watermark. Built for people who want their scripts to just run.\n\n"
    "**Products**\n"
    "• **Umbra Executor** — inject, `loadstring`, a real script environment, file/HTTP APIs.\n"
    "• **Umbra Mod Menu** — a polished in-game panel: tabs, toggles, sliders, keybinds, configs.\n\n"
    "**Get access**\n"
    "1. Read the rules below.\n"
    "2. Grab a **Beta** slot in #announcements (first 5 are free).\n"
    "3. Buyers get the **Buyer** role and the downloads channel forever.\n\n"
    "**Rules**\n"
    "1. No leaking builds or keys outside this server.\n"
    "2. No harassment, slurs, or spam.\n"
    "3. Support requests go in #support and include: version, game, steps, error text.\n"
    "4. One account per buyer. Sharing a build is fine; reselling it is not.\n"
    "5. Exploiting is at your own risk — use an alt for experiments.\n\n"
    "**Links**\n"
    "GitHub: <https://github.com/blake-codes-dev/umbra>\n"
    "Changelog: <#CHANGELOG>"
)
SUPPORT = (
    "**Support template — copy, fill, send.**\n"
    "```\n"
    "Umbra version: (Settings -> About, e.g. 0.1.0+3f2a1c)\n"
    "Windows: (winver)\n"
    "Game / place id:\n"
    "What I did:\n"
    "What happened:\n"
    "Error text (console):\n"
    "Fresh empty script also fails? yes/no\n"
    "```\n"
    "First response usually < 12 h. If the client crashes, attach the newest file from "
    "`%LOCALAPPDATA%\\Umbra\\logs`."
)
DOWNLOADS = (
    "**Downloads are role-gated.**\n\n"
    "• **Beta** — free, limited slots, expect breakage, report everything.\n"
    "• **Buyer** — paid, stable channel, priority fixes.\n\n"
    "Builds are posted here as `.zip` attachments with a SHA-256 in the message. "
    "Verify before running. Windows SmartScreen may warn on unsigned builds; "
    "the hash is the source of truth."
)
ANNOUNCE = (
    "**Umbra is open.**\n\n"
    "The executor is in private development; the mod menu is already usable. "
    "First five **Beta** slots are free — react with 🧪 and open a ticket in #support "
    "with your Roblox username and Windows build.\n\n"
    "Paid access: **$6 lifetime Founders** (10 slots), then **$4/mo** or **$10 lifetime**. "
    "Executor + mod menu are included. Payment link and buyer role are handled in #support."
)
CHANGELOG = (
    "**v0.1.0-dev** · build `dev`\n"
    "```\n"
    "added   executor core: manual-map injection, 0 modules/threads added\n"
    "added   Luau state discovery (ci/global/top offsets from the live interpreter)\n"
    "added   mod menu: draggable panel, tabs, toggles, sliders, keybinds, config\n"
    "added   Discord server + role-gated builds\n"
    "known   executor API not wired to the runner yet; scripts run when the gate lands\n"
    "```\n"
    "Every build lists a SHA-256 in #downloads. If a script breaks twice, mark it and "
    "roll back to the previous build."
)


def build(_args):
    state = load()
    state["guild_id"] = GUILD_ID
    state.setdefault("channels", {})
    state.setdefault("roles", {})
    state.setdefault("categories", {})
    g = api("GET", f"/guilds/{GUILD_ID}")
    print("guild:", g.get("name"))
    if g.get("name") in ("scp funtimes", "Server"):
        api("PATCH", f"/guilds/{GUILD_ID}",
            {"name": "Umbra",
             "description": "Executor + mod menu for Roblox. Quietly undetected."})
        print("  = renamed guild to Umbra")
    for name, colour, hoist in ROLES:
        ensure_role(state, name, colour, hoist)
    for name, _colour in CATEGORIES:
        ensure_category(state, name)
    for name, cat, topic, gate in CHANNELS:
        ensure_channel(state, name, cat, topic)
        if gate == "gated":
            set_overwrites(state, name, ["Beta", "Buyer"], VIEW | READ_HISTORY)
        elif gate == "staff":
            set_overwrites(state, name, ["Maintainer"], VIEW | READ_HISTORY)
    save(state)
    cleanup_defaults(state)
    save(state)
    print("done. channels:", len(state["channels"]), "roles:", len(state["roles"]))


def seed(_args):
    state = load()
    posts = {
        "welcome": WELCOME.replace("<#CHANGELOG>", state["channels"].get("changelog", "")),
        "support": SUPPORT,
        "downloads": DOWNLOADS,
        "announcements": ANNOUNCE,
        "changelog": CHANGELOG,
    }
    for name, text in posts.items():
        cid = state["channels"].get(name)
        if not cid:
            continue
        api("POST", f"/channels/{cid}/messages", {"content": text[:1900]})
        print("  posted", name)
        time.sleep(0.4)


def invite(_args):
    state = load()
    data = api("POST", f"/channels/{state['channels']['welcome']}/invites",
               {"max_age": 0, "max_uses": 0, "unique": False})
    print("https://discord.gg/" + data["code"])


def post(args):
    state = load()
    cid = state["channels"].get(args.channel) or args.channel
    api("POST", f"/channels/{cid}/messages", {"content": args.text})
    print("sent")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    sub.add_parser("seed")
    sub.add_parser("invite")
    p = sub.add_parser("post")
    p.add_argument("--channel", required=True)
    p.add_argument("--text", required=True)
    args = ap.parse_args()
    {"build": build, "seed": seed, "invite": invite, "post": post}[args.cmd](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
