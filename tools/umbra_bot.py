#!/usr/bin/env python3
"""Umbra Discord bot: verification, tickets, purchase flow, license keys.

No discord.py dependency - raw gateway v10 over websockets + aiohttp REST.

  python tools/umbra_bot.py            # run (foreground)
  python tools/umbra_bot.py --register # register slash commands and exit

State lives in state/discord.json (channels/roles) and state/license/keys.json.
The Ed25519 private key is secrets/license_ed25519.pem; the public key is mirrored
to state/license/public_key.pem for the app build.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import signal
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "secrets"
STATE = ROOT / "state" / "discord.json"
LICENSE_DIR = ROOT / "state" / "license"
KEYS = LICENSE_DIR / "keys.json"
API = "https://discord.com/api/v10"
ACCENT = 0x7C5CFF
SUCCESS = 0x22C55E
WARN = 0xF59E0B

INTENTS = 1 | 512  # GUILDS | GUILD_MESSAGES (no privileged intents)


def token() -> str:
    return (SECRETS / "discord.token").read_text(encoding="utf-8").strip()


def app_id() -> str:
    p = SECRETS / "discord.client_id"
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    # bot user id == application id
    return "1574839950862712912"


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def http(method: str, path: str, payload=None, timeout: int = 30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bot " + token())
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "UmbraBot/1.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        if e.code == 429:
            try:
                time.sleep(float(json.loads(body).get("retry_after", 1.0)) + 0.2)
            except Exception:
                time.sleep(1.2)
            return http(method, path, payload, timeout)
        print(f"API {method} {path} -> {e.code} {body[:300]}", flush=True)
        return None


# ---------------------------------------------------------------- licenses
LICENSE_PREFIX = "UMBRA1"


def private_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return serialization.load_pem_private_key(
        (SECRETS / "license_ed25519.pem").read_bytes(), password=None)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_license(payload: dict) -> str:
    body = b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signed = f"{LICENSE_PREFIX}.{body}"
    sig = private_key().sign(signed.encode())
    return f"{signed}.{b64(sig)}"


def verify_license(key: str) -> dict | None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        prefix, body, sig = key.strip().split(".")
        if prefix != LICENSE_PREFIX:
            return None
        pub = serialization.load_pem_public_key((LICENSE_DIR / "public_key.pem").read_bytes())
        pub.verify(unb64(sig), f"{prefix}.{body}".encode())
        return json.loads(unb64(body))
    except Exception:
        return None


def keys_db() -> dict:
    return load(KEYS, {"users": {}})


def issue(uid: str, plan: str, hwid: str = "", days: int | None = None) -> dict:
    db = keys_db()
    now = datetime.now(timezone.utc)
    exp = None if days is None else (now + timedelta(days=days)).isoformat()
    payload = {
        "uid": str(uid),
        "plan": plan,
        "hwid": hwid,
        "iat": now.isoformat(),
        "exp": exp,
        "jti": base64.urlsafe_b64encode(os.urandom(9)).decode().rstrip("="),
    }
    key = sign_license(payload)
    db["users"][str(uid)] = {
        "plan": plan, "hwid": hwid, "issued": payload["iat"], "expires": exp,
        "key": key, "revoked": False,
    }
    save(KEYS, db)
    return {"key": key, **payload}


def user_license(uid: str) -> dict | None:
    return keys_db()["users"].get(str(uid))


# ---------------------------------------------------------------- embeds
def embed(title, description="", colour=ACCENT, fields=None, footer=None):
    data = {"title": title, "description": description[:4000], "color": colour}
    if fields:
        data["fields"] = [{"name": n, "value": v[:1000], "inline": i} for n, v, i in fields]
    if footer:
        data["footer"] = {"text": footer}
    return {"embeds": [data]}


def button(label, cid, style=2, emoji=None):
    b = {"type": 2, "label": label, "style": style, "custom_id": cid}
    if emoji:
        b["emoji"] = {"name": emoji}
    return b


def row(*buttons):
    return {"type": 1, "components": list(buttons)}


# ---------------------------------------------------------------- handlers
def state_ids():
    s = load(STATE, {})
    return s.get("guild_id"), s.get("channels", {}), s.get("roles", {})


def is_staff(member: dict) -> bool:
    _g, _c, roles = state_ids()
    staff = {roles.get("Owner"), roles.get("Maintainer")}
    return bool(staff & set(member.get("roles", []))) or member.get("permissions", 0) & 0x20


async def respond(interaction, data, ephemeral=False):
    if ephemeral:
        data = dict(data)
        data["flags"] = 64
    body = {"type": 4, "data": data}
    await asyncio.to_thread(http, "POST",
                            f"/interactions/{interaction['id']}/{interaction['token']}/callback",
                            body)


async def followup(interaction, data, ephemeral=False):
    if ephemeral:
        data = dict(data)
        data["flags"] = 64
    app = interaction.get("application_id") or app_id()
    await asyncio.to_thread(http, "POST",
                            f"/webhooks/{app}/{interaction['token']}", data)


async def on_component(interaction):
    cid = interaction.get("data", {}).get("custom_id", "")
    guild_id, channels, roles = state_ids()
    user = interaction.get("user") or interaction.get("member", {}).get("user", {})
    uid = user.get("id")

    if cid == "verify:accept":
        role = roles.get("Verified")
        member = interaction.get("member", {})
        if role and member:
            await asyncio.to_thread(http, "PUT",
                                    f"/guilds/{guild_id}/members/{uid}/roles/{role}", {})
            await respond(interaction, embed("Verified", "You're in. Say hi in <#%s>."
                                             % channels.get("general", ""), SUCCESS), ephemeral=True)
        else:
            await respond(interaction, embed("Verification unavailable",
                                             "Missing role. Ping a maintainer.", WARN), ephemeral=True)
        return

    if cid.startswith("ticket:"):
        parts = cid.split(":")
        kind = parts[1]
        product = parts[2] if len(parts) > 2 else ""
        support = channels.get("support")
        name = {"buy": "purchase", "support": "help", "bug": "bug"}.get(kind, "ticket")
        display = (user.get("global_name") or user.get("username") or "user")[:20]
        thread = await asyncio.to_thread(
            http, "POST", f"/channels/{support}/threads",
            {"name": f"{name}-{display}", "type": 11, "invitable": False,
             "auto_archive_duration": 1440})
        if not thread:
            await respond(interaction, embed("Ticket failed", "Try again or ping staff.", WARN),
                          ephemeral=True)
            return
        tid = thread["id"]
        await asyncio.to_thread(http, "PUT", f"/channels/{tid}/thread-members/{uid}", {})
        intro = {
            "buy": ((f"**Purchase — {product or 'bundle'}**\n" if product else "**Purchase**\n")
                    + "Thanks for supporting Umbra.\n\n"
                    "**Price list**\n• Executor — **$6** lifetime (Founders)\n"
                    "• Mod Menu — **$4** lifetime\n• Bundle — **$10** lifetime\n\n"
                    "**Pay with Robux** (no bank needed):\n"
                    "1. Open the Roblox experience linked in this thread.\n"
                    "2. Buy the gamepass for the product you want.\n"
                    "3. Paste your Roblox username + the gamepass receipt here.\n\n"
                    "A maintainer verifies and grants **Buyer**, then DMs your license key.\n"
                    "Refunds within 48 h if the executor does not run on your machine."),
            "support": ("**Support**\nPost the template below and a maintainer will help.\n```\n"
                        "Umbra version:\nWindows:\nGame / place id:\nWhat I did:\nWhat happened:\n"
                        "Error text:\nFresh empty script also fails? yes/no\n```"),
            "bug": ("**Bug report**\nWhat broke, how to reproduce, and the console text. "
                    "Screenshots/clips welcome. We fix the top report every build."),
        }.get(kind, "**Ticket**\nA maintainer will be with you shortly.")
        msg = await asyncio.to_thread(http, "POST", f"/channels/{tid}/messages", {
            "content": f"<@{uid}>",
            **embed(f"{kind.upper()} ticket", intro, ACCENT),
            "components": [row(button("Close", "ticket:close", 4, "🔒"))],
        })
        await respond(interaction, embed("Ticket opened", f"<#{tid}>", SUCCESS), ephemeral=True)
        return

    if cid == "ticket:close":
        member = interaction.get("member", {})
        if not is_staff(member):
            await respond(interaction, embed("Staff only", "Only staff can close tickets.", WARN),
                          ephemeral=True)
            return
        channel_id = interaction.get("channel_id")
        await asyncio.to_thread(http, "PATCH", f"/channels/{channel_id}",
                                {"archived": True, "locked": True})
        await respond(interaction, embed("Closed", "Ticket archived. Open a new one any time.",
                                         SUCCESS), ephemeral=True)
        return

    await respond(interaction, embed("Unknown action", cid, WARN), ephemeral=True)


async def on_command(interaction):
    data = interaction.get("data", {})
    name = data.get("name")
    opts = {o["name"]: o.get("value") for o in data.get("options", [])}
    guild_id, channels, roles = state_ids()
    user = interaction.get("user") or interaction.get("member", {}).get("user", {})
    uid = str(user.get("id"))
    member = interaction.get("member", {})

    if name == "key":
        hwid = str(opts.get("hwid", "") or "")
        lic = user_license(uid)
        if not lic:
            await respond(interaction, embed("No license yet",
                                             "Buy a plan, then run `/key hwid:<your-hwid>`.\n"
                                             "The app shows your HWID in Settings → License.",
                                             WARN), ephemeral=True)
            return
        if lic.get("revoked"):
            await respond(interaction, embed("License revoked", "Contact a maintainer.", WARN),
                          ephemeral=True)
            return
        if hwid and not lic.get("hwid"):
            new = issue(uid, lic["plan"], hwid, None)
            lic = user_license(uid)
            dm = await asyncio.to_thread(http, "POST", "/users/@me/channels", {"recipient_id": uid})
            if dm:
                await asyncio.to_thread(http, "POST", f"/channels/{dm['id']}/messages",
                                        {"content": "**Your Umbra license**\n```\n"
                                                    + new["key"] + "\n```"})
            await respond(interaction, embed("License bound",
                                             "HWID locked. The key was sent to your DMs.",
                                             SUCCESS), ephemeral=True)
            return
        if hwid and lic.get("hwid") != hwid:
            await respond(interaction, embed("HWID mismatch",
                                             "This license is already bound to another machine. "
                                             "Staff can reset it.", WARN), ephemeral=True)
            return
        exp = lic.get("expires") or "lifetime"
        await respond(interaction, embed("Your license", "",
                                         SUCCESS, fields=[
                                             ("Plan", lic["plan"], True),
                                             ("HWID", lic.get("hwid") or "unbound", True),
                                             ("Expires", exp, True),
                                         ]), ephemeral=True)
        return

    if name == "trial":
        lic = user_license(uid)
        if lic and lic.get("plan") != "trial":
            await respond(interaction, embed("You already have a license", "Skip the trial.", WARN),
                          ephemeral=True)
            return
        if lic and lic.get("plan") == "trial":
            await respond(interaction, embed("Trial already used",
                                             "One trial per account.", WARN), ephemeral=True)
            return
        issue(uid, "trial", "", 1)
        await respond(interaction, embed("Trial issued",
                                         "Run `/key hwid:<your-hwid>` within 24 h to bind it.",
                                         SUCCESS), ephemeral=True)
        return

    if name == "buy":
        product = opts.get("product", "bundle")
        prices = {"executor": ("Executor", "$6 / 600 R$", "executor"),
                  "modmenu": ("Mod Menu", "$4 / 400 R$", "modmenu"),
                  "bundle": ("Bundle", "$10 / 1000 R$", "bundle")}
        label, price, code = prices.get(product, prices["bundle"])
        support = channels.get("support")
        display = (user.get("global_name") or user.get("username") or "user")[:20]
        thread = await asyncio.to_thread(
            http, "POST", f"/channels/{support}/threads",
            {"name": f"buy-{code}-{display}", "type": 11, "invitable": False,
             "auto_archive_duration": 1440})
        if not thread:
            await respond(interaction, embed("Purchase failed", "Try again.", WARN), ephemeral=True)
            return
        tid = thread["id"]
        await asyncio.to_thread(http, "PUT", f"/channels/{tid}/thread-members/{uid}", {})
        await asyncio.to_thread(http, "POST", f"/channels/{tid}/messages", {
            "content": f"<@{uid}>",
            **embed(f"{label} — {price}",
                    "**Pay with Robux** (no bank needed):\n"
                    "1. Open the gamepass link in this thread.\n"
                    "2. Buy the pass for the amount above.\n"
                    "3. Post your Roblox username + receipt.\n\n"
                    "**Pay with card / PayPal** (optional): ask for the link.\n"
                    "A maintainer grants **Buyer** and DMs the license key.",
                    ACCENT),
        })
        await respond(interaction, embed("Purchase", f"<#{tid}>", SUCCESS), ephemeral=True)
        return

    if name == "status":
        keys = keys_db()["users"]
        active = sum(1 for v in keys.values() if not v.get("revoked"))
        await respond(interaction, embed("Umbra status", "",
                                         SUCCESS, fields=[
                                             ("Build", "0.1.0-dev", True),
                                             ("Licenses", str(active), True),
                                             ("Client", "2.748.1526.0 GDK", True),
                                         ], footer="Executor + mod menu · quietly undetected"),
                      ephemeral=True)
        return

    if name == "grant":
        if not is_staff(member):
            await respond(interaction, embed("Staff only", "", WARN), ephemeral=True)
            return
        target = opts.get("user")
        plan = opts.get("plan", "buyer")
        if isinstance(target, str):
            target_id = target
        else:
            target_id = str(target)
        if plan in ("buyer", "beta"):
            role = roles.get(plan.capitalize())
            if role:
                await asyncio.to_thread(http, "PUT",
                                        f"/guilds/{guild_id}/members/{target_id}/roles/{role}", {})
        lic = issue(target_id, plan, "", 30 if plan == "beta" else None)
        dm = await asyncio.to_thread(http, "POST", "/users/@me/channels", {"recipient_id": target_id})
        if dm:
            await asyncio.to_thread(http, "POST", f"/channels/{dm['id']}/messages",
                                    {"content": f"**Umbra {plan}**\nRun `/key hwid:<your-hwid>` "
                                                f"in the server to bind this license.\n\n```\n{lic['key']}\n```"})
        await respond(interaction, embed("Granted", f"<@{target_id}> · {plan}", SUCCESS),
                      ephemeral=True)
        return

    if name == "revoke":
        if not is_staff(member):
            await respond(interaction, embed("Staff only", "", WARN), ephemeral=True)
            return
        target = opts.get("user")
        tid = target if isinstance(target, str) else str(target)
        db = keys_db()
        if tid in db["users"]:
            db["users"][tid]["revoked"] = True
            save(KEYS, db)
            await respond(interaction, embed("Revoked", f"<@{tid}>", SUCCESS), ephemeral=True)
        else:
            await respond(interaction, embed("No license", f"<@{tid}> has none.", WARN),
                          ephemeral=True)
        return

    await respond(interaction, embed("Unknown command", str(name), WARN), ephemeral=True)


async def on_member_update(payload):
    guild_id, channels, roles = state_ids()
    member = payload.get("d") or {}
    uid = str(member.get("user", {}).get("id"))
    have = set(member.get("roles", []))
    if not ({roles.get("Buyer"), roles.get("Beta")} & have):
        return
    lic = user_license(uid)
    if not lic:
        lic = issue(uid, "buyer" if roles.get("Buyer") in have else "beta", "", None)
    dm = await asyncio.to_thread(http, "POST", "/users/@me/channels", {"recipient_id": uid})
    if dm:
        await asyncio.to_thread(http, "POST", f"/channels/{dm['id']}/messages", {
            "content": "**Welcome to Umbra.**\nYour license:\n```\n" + lic["key"] + "\n```\n"
                       "Run `/key hwid:<your-hwid>` in the server to bind it to this PC.\n"
                       "The HWID is shown in the app under Settings → License."})


# ---------------------------------------------------------------- gateway
async def run_gateway():
    url = "wss://gateway.discord.gg/?v=10&encoding=json"
    while True:
        try:
            async with websockets.connect(url, max_size=2**24) as ws:
                hello = json.loads(await ws.recv())
                interval = hello["d"]["heartbeat_interval"] / 1000.0
                seq = [None]

                async def heartbeat():
                    while True:
                        await asyncio.sleep(interval)
                        await ws.send(json.dumps({"op": 1, "d": seq[0]}))

                hb = asyncio.create_task(heartbeat())
                await ws.send(json.dumps({
                    "op": 2,
                    "d": {"token": token(), "intents": INTENTS,
                          "properties": {"os": "windows", "browser": "umbra",
                                         "device": "umbra"}},
                }))
                print("gateway: connected", flush=True)
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("s") is not None:
                        seq[0] = msg["s"]
                    op, t, d = msg.get("op"), msg.get("t"), msg.get("d")
                    if op == 7 or op == 9:
                        break
                    if t == "INTERACTION_CREATE":
                        try:
                            if d.get("type") == 2:
                                await on_command(d)
                            elif d.get("type") == 3:
                                await on_component(d)
                        except Exception:
                            traceback.print_exc()
                            try:
                                await respond(d, embed("Error", "Try again.", WARN), ephemeral=True)
                            except Exception:
                                pass
                    elif t == "GUILD_MEMBER_UPDATE":
                        try:
                            await on_member_update(msg)
                        except Exception:
                            traceback.print_exc()
                hb.cancel()
        except Exception as exc:
            print("gateway:", repr(exc)[:200], flush=True)
        await asyncio.sleep(3)


# ---------------------------------------------------------------- setup
COMMANDS = [
    {"name": "key", "description": "Show or bind your Umbra license",
     "options": [{"name": "hwid", "description": "Your HWID from the app", "type": 3,
                  "required": False}]},
    {"name": "trial", "description": "Get a 24 hour trial license"},
    {"name": "buy", "description": "Open a purchase ticket",
     "options": [{"name": "product", "description": "What to buy", "type": 3, "required": False,
                  "choices": [{"name": "Executor", "value": "executor"},
                              {"name": "Mod Menu", "value": "modmenu"},
                              {"name": "Bundle", "value": "bundle"}]}]},
    {"name": "status", "description": "Build and license status"},
    {"name": "grant", "description": "Staff: grant a plan",
     "options": [{"name": "user", "description": "Member", "type": 6, "required": True},
                 {"name": "plan", "description": "buyer or beta", "type": 3, "required": True,
                  "choices": [{"name": "Buyer", "value": "buyer"},
                              {"name": "Beta", "value": "beta"}]}]},
    {"name": "revoke", "description": "Staff: revoke a license",
     "options": [{"name": "user", "description": "Member", "type": 6, "required": True}]},
]


def register():
    gid, _c, _r = state_ids()
    res = http("PUT", f"/applications/{app_id()}/guilds/{gid}/commands", COMMANDS)
    print("registered", len(res or []), "commands")


def panels():
    _g, channels, roles = state_ids()
    # verification panel
    if channels.get("verify"):
        http("POST", f"/channels/{channels['verify']}/messages", {
            **embed("Verification",
                    "Click below to accept the rules and unlock the server.\n\n"
                    "1. Be decent.\n2. No leaks or resale.\n3. Support requests use the template.\n"
                    "4. Use an alt for risky scripts.",
                    ACCENT),
            "components": [row(button("Accept rules", "verify:accept", 3, "✅"))]})
    # support ticket panel
    if channels.get("support"):
        http("POST", f"/channels/{channels['support']}/messages", {
            **embed("Open a ticket",
                    "**Purchase** — buy the executor, mod menu or bundle with Robux.\n"
                    "**Support** — a bug, a crash, a script that will not run.\n"
                    "**Bug report** — reproducible issue with steps.",
                    ACCENT),
            "components": [row(button("Purchase", "ticket:buy", 3, "🛒"),
                               button("Support", "ticket:support", 2, "🎫"),
                               button("Bug report", "ticket:bug", 2, "🐛"))]})
    # pricing panel
    if channels.get("pricing"):
        http("POST", f"/channels/{channels['pricing']}/messages", {
            **embed("Pricing",
                    "**Executor — $6 lifetime** (Founders; 10 slots)\n"
                    "Loadstring, real environment, console, files, HTTP, hooks.\n\n"
                    "**Mod Menu — $4 lifetime**\nTabs, toggles, sliders, keybinds, configs, "
                    "notifications, search.\n\n"
                    "**Bundle — $10 lifetime**\nBoth products. Every future update.\n\n"
                    "**Robux accepted** — 600 / 400 / 1000 R$. No bank needed.\n"
                    "Refunds within 48 h if the executor does not run on your machine.",
                    ACCENT),
            "components": [row(button("Buy Executor", "ticket:buy:executor", 3, "⚡"),
                               button("Buy Bundle", "ticket:buy:bundle", 1, "💜"),
                               button("Buy Mod Menu", "ticket:buy:modmenu", 2, "🧩"))]})
    print("panels posted")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--register", action="store_true")
    ap.add_argument("--panels", action="store_true")
    args = ap.parse_args()
    if args.register:
        register()
        return
    if args.panels:
        panels()
        return
    print("starting bot", flush=True)
    asyncio.run(run_gateway())


if __name__ == "__main__":
    main()
