#!/usr/bin/env python3
"""Build and maintain the Lyphen server: layout, roles, gates, copy.

Everything is idempotent and ordered: channels are renamed into the plan, moved
to a fixed position, re-parented, re-gated, and anything left over from an older
layout is reported (and removed only with --prune).

  python tools/lyphen_discord.py apply            # make the server match the plan
  python tools/lyphen_discord.py doctor           # report drift, change nothing
  python tools/lyphen_discord.py apply --prune    # also delete channels not in the plan
  python tools/lyphen_discord.py seed             # post the copy (skips non-empty channels)
  python tools/lyphen_discord.py invite
  python tools/lyphen_discord.py post --channel changelog --text "..."

Uses only the bot token in secrets/discord.token.
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
OWNER_ID = "1326301118824841216"

VIEW = 0x400
READ_HISTORY = 0x10000
SEND_MESSAGES = 0x800
ADD_REACTIONS = 0x40
ATTACH_FILES = 0x800
CONNECT = 0x100000
SPEAK = 0x200000
USE_EXTERNAL_EMOJI = 0x40000
MANAGE_MESSAGES = 0x2000
MENTION_EVERYONE = 0x20000

# lowest -> highest, so position patching is ascending
ROLES = [
    ("Muted", 0x6B7280, False),
    ("Verified", 0x22D3EE, False),
    ("Beta", 0xA78BFA, False),
    ("Buyer", 0xFBBF24, False),
    ("Maintainer", 0xC084FC, True),
    ("Owner", 0x7C5CFF, True),
]
RENAMED_ROLES: dict[str, str] = {}
RENAMED_CHANNELS = {"support": "help", "media": "showcase", "verify": "rules",
                    "downloads": "builds", "vouches": "vouch"}
# the bot holds position 1, so role ordering clamps to it until the human drags it
RENAMED_CATEGORIES = {"INFORMATION": "START", "START HERE": "START",
                      "LIBRARY": "DOWNLOADS"}

# name, topic, special
CATEGORIES = [
    ("START", None, "open"),
    ("STORE", None, "open"),
    ("DOWNLOADS", None, "library"),
    ("COMMUNITY", None, "open"),
    ("STAFF", None, "staff"),
    ("VOICE", None, "open"),
]
CHANNELS = [
    ("welcome", "what this is and how to start", "readonly"),
    ("rules", "six lines. react to accept", "readonly"),
    ("announcements", "releases and downtime", "announce"),
    ("changelog", "every build and what changed", "readonly"),
    ("pricing", "three tiers. no key, no ads", "readonly"),
    ("faq", "read this first", "readonly"),
    ("vouch", "bought it? post proof", "send"),
    ("builds", "zips with hashes, role locked", "library-read"),
    ("scripts", "scripts that actually work", "library-send"),
    ("showcase", "clips", "library-send"),
    ("general", "anything", "send"),
    ("help", "post the template, one thread each", "send"),
    ("staff", "internal", "staff-send"),
    ("mod-log", "roles and bans", "staff-read"),
    ("Voice", "talk", "voice"),
    ("Support", "screen share a broken build", "voice"),
]
READONLY_ROLES = ("Maintainer", "Owner")
GATED_ROLES = ("Verified", "Beta", "Buyer", "Maintainer", "Owner")

NL = chr(10)

WELCOME = '**Lyphen**\nExecutor and mod menu for Roblox. Small, quiet, no key system, no ads, no watermark.\n\n**Start**\n1. Read #rules and react. You get Verified.\n2. Check #pricing. Free beta seats exist, paid tiers start at $4.\n3. Pay the way #pricing says and #builds unlocks.\n\nMenus ship with 14 working scripts already inside them, so it does something in any game before you write a line. Bring your own too, the editor runs real Luau.\n\nStill in development. Beta is free because it breaks. Want stable, wait or pay, both fine.\n\nTest on an alt. Keep your main clean until you trust a build.'

RULES = "**Rules**\n1. One buyer, one person. Don't resell builds.\n2. Don't leak builds or keys outside this server.\n3. Support goes in #help, with the template.\n4. No spam, no DM ads.\n5. Chargeback means the role comes off until it's settled.\n6. Your account, your risk. Use an alt to test.\n\nReact below to get Verified and unlock the rest."

PRICING = '**What is for sale**\n**executor · $10 · 1000 R$**\nmanual map injection, real luau environment, editor, console, files, http, hooks.\n**mod menu · $4 · 400 R$**\npanel with tabs, sliders, keybinds, esp, and 14 scripts already in it. load and go, any game.\n**spoofer · $6 · 600 R$**\nclears device and account ids. run before login, a ban stops following you.\n**all three · $14 · 1400 R$**\nsix less than buying them apart. one licence, one pc, every build after this.\n\nNo subscription, no key system, no server in the middle. Robux or card, both fine.\n\nBuy with the buttons in #products, or say which one in #help. Refund inside 48h if it never\ninjected on your machine.'

FAQ = ("**FAQ**\n"
       "**Is it detected?** Nothing stays undetected forever. #changelog says when a build is pulled.\n"
       "**Windows?** 10 21H2+ and 11, x64.\n"
       "**Key system?** No. No loader, no ads, no watermark.\n"
       "**How many games does it work in?** The panels are plain luau against the client, so any game the\n"
       "executor can reach. 14 scripts ship inside the menu, and the executor runs whatever you write.\n"
       "**Main account?** Fine, but test new builds on an alt.\n"
       "**Why not a free executor?** Injection adds no module, no thread, no window, no overlay, and the\n"
       "menu is not a web page in a box.\n"
       "**Can it change my account location?** The spoofer can call the same endpoint the client uses, so\n"
       "it genuinely writes the account. Ping and matchmaking do not move with it though.\n"
       "**Mobile or Mac?** Not in v1.\n"
       "**Script doesn't run?** Check #changelog, then open a ticket in #help.\n"
       "**Hashes?** Every build has a SHA-256. If yours differs, it isn't mine.")

BUILDS = "Builds go here, one message each, version in the message, SHA-256 under it. Check the hash\nbefore you run anything. SmartScreen warns because nothing is signed, the hash is the source\nof truth.\n\nBeta breaks. A build that survives a day of reports becomes stable and the older one gets\ndeleted. Don't mix files from two builds."

CHANGELOG = ("**v0.2.0-dev**\n```\n"
             "added   14 shipped scripts inside the menu, one click each, any game\n"
             "added   spoofer window: machine guid, product id, mac, machine name, profiles, backup, restore\n"
             "added   account location read and write, with a country id table that learns from every apply\n"
             "added   executor library pane, two surfaces, animated layout and real type\n"
             "fixed   closing a ticket opened a new one instead of closing it\n"
             "fixed   closed tickets were archived forever, they are deleted now\n"
             "fixed   the all-three button on products errored\n"
             "known   executor API still not wired to the runner, so custom scripts do not run yet\n"
             "known   Roblox re-detects account location over time, a change can revert\n```")

HELP = '**Post this, filled in**\n```\nversion:\nwindows:\ngame or place id:\nwhat I did:\nwhat happened:\nerror text:\nempty script also fails: yes/no\n```\nOne thread per problem. Crash? Attach the newest log from %LOCALAPPDATA%\\Lyphen\\logs.\nFirst reply usually inside 12h.'

ANNOUNCE = '**Server is up.**\nLyphen is an executor and mod menu in development. No key system, no ads, nothing public.\n\nFive beta seats are free right now. React in #rules and post in #help with your Windows\nversion. Prices are in #pricing, founders closes at ten.'

SCRIPTS = ("**Scripts**\n"
           "Every build carries the following inside the menu already, so there is nothing to paste:\n"
           "```\nwalk speed   super jump     infinite jump  noclip\nfull heal    respawn        anti afk       teleport to cursor\ncopy position  fullbright   free cam       spin\nunlock fps   rejoin\n```\n"
           "All of them are plain luau and work in any game. Change one by pressing edit in the menu, it\n"
           "drops straight into the editor. Post a fixed script here and the next build ships it.")

TEXTS = {
    "welcome": WELCOME,
    "rules": RULES,
    "pricing": PRICING,
    "faq": FAQ,
    "builds": BUILDS,
    "changelog": CHANGELOG,
    "help": HELP,
    "announce": ANNOUNCE,
    "scripts": SCRIPTS,
}

def tok() -> str:
    return (SECRETS / "discord.token").read_text(encoding="utf-8").strip()


def api(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    for attempt in range(6):
        req = urllib.request.Request(API + path, data=data, method=method)
        req.add_header("Authorization", "Bot " + tok())
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "LyphenBuilder/2.0")
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


def live_roles(state) -> dict:
    return {r["name"]: r for r in api("GET", f"/guilds/{state['guild_id']}/roles") or []}


def live_channels(state) -> list[dict]:
    return api("GET", f"/guilds/{state['guild_id']}/channels") or []


def ensure_role(state, roles: dict, name: str, colour: int, hoist: bool) -> str:
    existing = roles.get(name)
    if existing:
        rid = existing["id"]
        if existing["color"] != colour or existing["hoist"] != hoist:
            api("PATCH", f"/guilds/{state['guild_id']}/roles/{rid}",
                {"color": colour, "hoist": hoist, "mentionable": True})
    else:
        created = api("POST", f"/guilds/{state['guild_id']}/roles",
                      {"name": name, "color": colour, "hoist": hoist, "mentionable": True})
        rid = created["id"]
        roles[name] = created
    state["roles"][name] = rid
    return rid


def overwrites_for(state, gate: str) -> list[dict] | None:
    everyone = state["guild_id"]
    deny_all = {"id": everyone, "type": 0, "allow": "0", "deny": str(VIEW | READ_HISTORY)}
    allow = lambda role: {"id": state["roles"][role], "type": 0,
                          "allow": str(VIEW | READ_HISTORY | SEND_MESSAGES | ADD_REACTIONS
                                       | ATTACH_FILES | USE_EXTERNAL_EMOJI), "deny": "0"}
    read_only = lambda role: {"id": state["roles"][role], "type": 0,
                              "allow": str(VIEW | READ_HISTORY | SEND_MESSAGES), "deny": "0"}
    muted = state["roles"].get("Muted")
    if gate == "open":
        return []
    if gate == "library":
        return [deny_all] + [allow(r) for r in GATED_ROLES]
    if gate == "staff":
        return [deny_all] + [allow(r) for r in READONLY_ROLES]
    if gate == "library-read":
        return [deny_all] + [read_only(r) for r in GATED_ROLES]
    if gate == "library-send":
        out = [deny_all] + [allow(r) for r in GATED_ROLES]
        return out
    if gate == "staff-read":
        return [deny_all] + [{"id": state["roles"][r], "type": 0,
                              "allow": str(VIEW | READ_HISTORY), "deny": "0"}
                             for r in READONLY_ROLES]
    if gate == "staff-send":
        return [deny_all] + [allow(r) for r in READONLY_ROLES[:1]]
    if gate == "readonly":
        out = [{"id": everyone, "type": 0, "allow": str(VIEW | READ_HISTORY | ADD_REACTIONS),
                "deny": str(SEND_MESSAGES)}]
        out += [read_only(r) for r in READONLY_ROLES]
        return out
    if gate == "send":
        out = [{"id": everyone, "type": 0,
                "allow": str(VIEW | READ_HISTORY | SEND_MESSAGES | ADD_REACTIONS
                             | ATTACH_FILES | USE_EXTERNAL_EMOJI), "deny": "0"}]
        if muted:
            out.append({"id": muted, "type": 0, "allow": str(VIEW | READ_HISTORY),
                        "deny": str(SEND_MESSAGES)})
        return out
    if gate == "voice":
        out = [{"id": everyone, "type": 0, "allow": str(VIEW | CONNECT | SPEAK),
                "deny": "0"}]
        return out
    if gate == "announce":
        out = [{"id": everyone, "type": 0, "allow": str(VIEW | READ_HISTORY | ADD_REACTIONS),
                "deny": str(SEND_MESSAGES)}]
        out += [read_only(r) for r in READONLY_ROLES]
        return out
    return None


STORED = ("pricing", "buy", "faq", "vouch")


def category_for(name: str, gate: str) -> str:
    if gate.startswith("library"):
        return "DOWNLOADS"
    if gate.startswith("staff"):
        return "STAFF"
    if gate == "voice":
        return "VOICE"
    if name in STORED:
        return "STORE"
    if gate in ("readonly", "announce"):
        return "START HERE"
    return "COMMUNITY"


def plan_channel(state, existing: list[dict], name: str, parent: str, position: int,
                 topic: str, gate: str, dry: bool) -> str:
    kind = 2 if gate == "voice" else 0
    text_types = (0, 5)
    match = next((c for c in existing if c["name"].lower() == name.lower()
                  and ((kind == 0 and c["type"] in text_types) or (kind == 2 and c["type"] == 2))),
                 None)
    if match:
        cid = match["id"]
        patch = {}
        if match["name"] != name:
            patch["name"] = name
        if match.get("parent_id") != parent:
            patch["parent_id"] = parent
        if match.get("position") != position:
            patch["position"] = position
        if gate != "voice" and (match.get("topic") or "") != (topic or ""):
            patch["topic"] = topic
        if match["type"] != kind:
            patch["type"] = kind
        over = overwrites_for(state, gate)
        if over is not None:
            patch["permission_overwrites"] = over
        if patch:
            if not dry:
                api("PATCH", f"/channels/{cid}", patch)
            print(f"  ~ {name} " + ",".join(sorted(patch)))
    else:
        payload = {"name": name, "parent_id": parent, "position": position, "type": kind}
        if gate != "voice":
            payload["topic"] = topic
        over = overwrites_for(state, gate)
        if over is not None:
            payload["permission_overwrites"] = over
        if dry:
            print(f"  + {name} (would create)")
            cid = f"dry-{name}"
        else:
            created = api("POST", f"/guilds/{state['guild_id']}/channels", payload)
            cid = created["id"]
            print(f"  + {name}")
            time.sleep(0.15)
        existing.append({"id": cid, "name": name, "type": payload["type"],
                         "parent_id": parent, "position": position, "topic": topic})
    state["channels"][name] = cid
    return cid


def apply(args):
    state = load()
    state["guild_id"] = GUILD_ID
    for key in ("channels", "roles", "categories"):
        state.setdefault(key, {})
    g = api("GET", f"/guilds/{GUILD_ID}")
    print("guild:", g.get("name"))
    if g.get("name") != "Lyphen":
        api("PATCH", f"/guilds/{GUILD_ID}", {"name": "Lyphen"})
        print("  = renamed to Lyphen")
    api("PATCH", f"/guilds/{GUILD_ID}",
        {"description": "Private executor and mod menu for Roblox. No key system, no ads.",
         "default_message_notifications": 1})
    roles = live_roles(state)
    for old, new in RENAMED_ROLES.items():
        if old in roles and new not in roles and not args.dry:
            api("PATCH", f"/guilds/{GUILD_ID}/roles/{roles[old]['id']}", {"name": new})
            print(f"  = role {old} -> {new}")
            roles = live_roles(state)
    order = []
    for name, colour, hoist in ROLES:
        rid = ensure_role(state, roles, name, colour, hoist)
        order.append((rid, name))
    for index, (rid, name) in enumerate(order, start=1):
        if not args.dry:
            api("PATCH", f"/guilds/{GUILD_ID}/roles/{rid}", {"position": index})
    for old, new in RENAMED_CATEGORIES.items():
        match = next((c for c in live_channels(state)
                      if c["type"] == 4 and c["name"].lower() == old.lower()), None)
        if match and not any(c["type"] == 4 and c["name"].lower() == new.lower()
                             for c in live_channels(state)):
            if not args.dry:
                api("PATCH", f"/channels/{match['id']}", {"name": new})
            print(f"  = category {old} -> {new}")
    for index, (name, _topic, _gate) in enumerate(CATEGORIES):
        cat = next((c for c in live_channels(state)
                    if c["type"] == 4 and c["name"].lower() == name.lower()), None)
        over = overwrites_for(state, _gate) if _gate else None
        if cat:
            patch = {}
            if cat["position"] != index:
                patch["position"] = index
            if over is not None and cat.get("permission_overwrites") != over:
                patch["permission_overwrites"] = over
            if patch and not args.dry:
                api("PATCH", f"/channels/{cat['id']}", patch)
            cid = cat["id"]
            print(f"  = {name}" + (f" ({','.join(sorted(patch))})" if patch else ""))
        else:
            payload = {"name": name, "type": 4, "position": index}
            if over is not None:
                payload["permission_overwrites"] = over
            if args.dry:
                print(f"  + {name} (category, would create)")
                cid = f"dry-cat-{name}"
            else:
                cid = api("POST", f"/guilds/{GUILD_ID}/channels", payload)["id"]
                print(f"  + {name} (category)")
            time.sleep(0.15)
        state["categories"][name] = cid
        time.sleep(0.15)
    existing = live_channels(state)
    for old, new in RENAMED_CHANNELS.items():
        match = next((c for c in existing if c["name"].lower() == old), None)
        if match and not any(c["name"].lower() == new for c in existing):
            if not args.dry:
                api("PATCH", f"/channels/{match['id']}", {"name": new})
            match["name"] = new
            print(f"  = channel {old} -> {new}")
    for index, (name, topic, gate) in enumerate(CHANNELS):
        parent = state["categories"][category_for(name, gate)]
        plan_channel(state, existing, name, parent, index, topic, gate, args.dry)
    planned = {n.lower() for n, _t, _g in CHANNELS} | {n.lower() for n, _t, _g in CATEGORIES}
    leftovers = [c for c in existing if c["name"].lower() not in planned
                 and c["type"] in (0, 2, 4)]
    for c in leftovers:
        if args.prune and not args.dry:
            api("DELETE", f"/channels/{c['id']}")
            print(f"  - {c['name']}")
        else:
            print(f"  ? leftover {c['name']} ({'pruned' if args.prune else 'todo'})")
    # the bot still looks these up by their old keys
    for alias, target in (("support", "help"), ("verify", "rules"), ("downloads", "builds"),
                          ("vouches", "vouch")):
        if state["channels"].get(target):
            state["channels"][alias] = state["channels"][target]
    if not args.dry:
        keep = {n for n, _t, _g in CHANNELS} | {"support", "verify", "downloads", "vouches"}
        state["channels"] = {k: v for k, v in state["channels"].items() if k in keep}
        state["categories"] = {k: v for k, v in state["categories"].items()
                               if k in {c for c, _t, _g in CATEGORIES}}
        api("PUT", f"/guilds/{GUILD_ID}/members/{OWNER_ID}/roles/{state['roles']['Owner']}", {})
        save(state)
    print("done:", len(state["channels"]), "channels,", len(state["roles"]), "roles")


def doctor(_args):
    state = load()
    problems = 0
    roles = live_roles(state)
    for name, colour, hoist in ROLES:
        current = roles.get(name)
        if not current:
            print(f"  missing role {name}")
            problems += 1
        elif current["color"] != colour or current["hoist"] != hoist:
            print(f"  role {name} drifted (colour/hoist)")
            problems += 1
    chans = {c["name"].lower(): c for c in live_channels(state)}
    for name, topic, gate in CHANNELS:
        current = chans.get(name.lower())
        if not current:
            print(f"  missing channel {name}")
            problems += 1
            continue
        if gate != "voice" and (current.get("topic") or "") != (topic or ""):
            print(f"  topic drifted: {name}")
            problems += 1
        want_deny = gate.startswith(("library", "staff"))
        blocked = any(o["id"] == state["guild_id"] and int(o["deny"]) & VIEW
                      for o in current.get("permission_overwrites") or [])
        if want_deny and not blocked:
            print(f"  gate open at @everyone: {name}")
            problems += 1
    print(f"{problems} problem(s)")


def clear_channel(cid: str) -> int:
    removed = 0
    while True:
        msgs = api("GET", f"/channels/{cid}/messages?limit=100") or []
        if not msgs:
            return removed
        ids = [m["id"] for m in msgs]
        if len(ids) == 1:
            api("DELETE", f"/channels/{cid}/messages/{ids[0]}")
        else:
            api("POST", f"/channels/{cid}/messages/bulk-delete", {"messages": ids})
        removed += len(ids)
        time.sleep(0.4)


def seed(args):
    state = load()
    for name, text in TEXTS.items():
        cid = state["channels"].get(name)
        if not cid:
            continue
        existing = api("GET", f"/channels/{cid}/messages?limit=1") or []
        if existing and args.clear:
            print("  cleared", clear_channel(cid), "message(s) in", name)
        elif existing and not args.force:
            print("  = has content, skipping", name)
            continue
        api("POST", f"/channels/{cid}/messages",
            {"content": text.replace("<#CHANGELOG>", state["channels"].get("changelog", ""))[:1950]})
        print("  posted", name)
        time.sleep(0.5)
    rules = state["channels"].get("rules")
    if rules:
        api("PUT", f"/channels/{rules}/messages/{api('GET', f'/channels/{rules}/messages?limit=1')[0]['id']}"
                    "/reactions/%E2%9C%85/@me", {})
        print("  reacted to the rules message")


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
    p = sub.add_parser("apply")
    p.add_argument("--dry", action="store_true")
    p.add_argument("--prune", action="store_true")
    sub.add_parser("doctor")
    p = sub.add_parser("seed")
    p.add_argument("--force", action="store_true")
    p.add_argument("--clear", action="store_true")
    sub.add_parser("invite")
    p = sub.add_parser("post")
    p.add_argument("--channel", required=True)
    p.add_argument("--text", required=True)
    args = ap.parse_args()
    {"apply": apply, "doctor": doctor, "seed": seed, "invite": invite,
     "post": post}[args.cmd](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
