#!/usr/bin/env python3
"""
discord_ops.py — bot-driven control of the product server. No dependencies.

A bot token is stable and scriptable; UI automation fights captchas and 2FA.
A bot can create the guild (up to 10), channels, categories, roles, permission
overwrites, invites, messages, embeds, file uploads, threads and moderation.

Setup (human, once)
  discord.com/developers/applications -> New Application -> Bot -> Reset Token
  secrets/discord.token       <- token
  secrets/discord.client_id   <- application id (for the invite URL)

Usage
  python tools/discord_ops.py whoami
  python tools/discord_ops.py setup                # idempotent guild/channels/roles
  python tools/discord_ops.py status
  python tools/discord_ops.py post --channel changelog --text "v0.1.0 ..."
  python tools/discord_ops.py post --channel announcements --title "Launch" --text "..."
  python tools/discord_ops.py upload --channel downloads --file dist/lyphen.zip --text "v0.1.0"
  python tools/discord_ops.py invite
  python tools/discord_ops.py role --user 123 --role Buyer
  python tools/discord_ops.py dm --user 123 --text "thanks for testing"
  python tools/discord_ops.py invite-url

Loop safety
  setup is idempotent; identical posts to the same channel are skipped for 6 h;
  429s are slept out; 5xx retried with backoff; ids cached in state/discord.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "secrets"
STATE = ROOT / "state" / "discord.json"
API = "https://discord.com/api/v10"
DEDUPE_HOURS = 6
ACCENT = 0x7C5CFF

CHANNELS = [
    ("welcome", "Rules, how to get access, FAQ"),
    ("announcements", "Releases, downtime, transitions"),
    ("changelog", "Every build: version, hash, fixes, known issues"),
    ("downloads", "Role-gated builds"),
    ("support", "Tickets: version + error + steps"),
    ("general", "Chat"),
    ("media", "Clips and screenshots"),
]
ROLES = [
    ("Maintainer", 0x7C5CFF, True),
    ("Beta", 0x00D1FF, False),
    ("Buyer", 0x2ECC71, False),
]
VIEW_CHANNEL = 0x400


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


def read_secret(name: str) -> str | None:
    try:
        value = (SECRETS / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def token() -> str:
    bearer = read_secret("discord.token")
    if not bearer:
        raise SystemExit(
            "no bot token. Create one at discord.com/developers -> New Application -> Bot -> "
            "Reset Token, then paste it into secrets/discord.token"
        )
    return bearer


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def request(method: str, path: str, payload: dict | None = None, raw: bytes | None = None,
            content_type: str = "application/json") -> dict | list | None:
    url = path if path.startswith("http") else API + path
    body = raw if raw is not None else (json.dumps(payload).encode() if payload else None)
    headers = {"Authorization": f"Bot {token()}", "User-Agent": "LyphenOps/1.0"}
    if body:
        headers["Content-Type"] = content_type

    for attempt in range(4):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                data = response.read()
            return json.loads(data) if data else None
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            if error.code == 429:
                retry = 1.0
                try:
                    retry = float(json.loads(detail).get("retry_after", 1.0))
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass
                time.sleep(min(10.0, retry + 0.25))
                continue
            if 500 <= error.code < 600 and attempt < 3:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise SystemExit(f"{method} {path} -> {error.code}: {detail}")
    return None


def multipart(channel_id: str, path: Path, text: str) -> dict:
    boundary = "----lyphen" + uuid.uuid4().hex
    parts: list[bytes] = []
    if text:
        parts += [f"--{boundary}".encode(), b'Content-Disposition: form-data; name="payload_json"',
                  b"Content-Type: application/json", b"", json.dumps({"content": text}).encode()]
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts += [f"--{boundary}".encode(),
              f'Content-Disposition: form-data; name="files[0]"; filename="{path.name}"'.encode(),
              f"Content-Type: {mime}".encode(), b"", path.read_bytes(),
              f"--{boundary}--".encode(), b""]
    return request("POST", f"/channels/{channel_id}/messages", raw=b"\r\n".join(parts),
                   content_type=f"multipart/form-data; boundary={boundary}")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def whoami(_args) -> None:
    me = request("GET", "/users/@me") or {}
    print(f"bot: {me.get('username')}  id: {me.get('id')}")


def invite_url(_args) -> None:
    client_id = read_secret("discord.client_id")
    if not client_id:
        raise SystemExit("put the application id in secrets/discord.client_id")
    print("https://discord.com/oauth2/authorize?client_id=%s&scope=bot+applications.commands"
          "&permissions=8" % client_id)


def ensure_guild(state: dict, name: str) -> str:
    guild_id = state.get("guild_id")
    if guild_id:
        guild = request("GET", f"/guilds/{guild_id}") or {}
        if guild.get("id"):
            return guild_id
    guild = request("POST", "/guilds", {"name": name}) or {}
    state.update({"guild_id": guild.get("id"), "channels": {}, "roles": {}})
    save_state(state)
    print(f"created guild {name} ({guild.get('id')})")
    return guild.get("id")


def ensure_channels(guild_id: str, state: dict) -> None:
    existing = request("GET", f"/guilds/{guild_id}/channels") or []
    by_name = {c["name"]: c["id"] for c in existing if c.get("type") == 0}
    state.setdefault("channels", {})
    for name, topic in CHANNELS:
        if name in by_name:
            state["channels"][name] = by_name[name]
            continue
        channel = request("POST", f"/guilds/{guild_id}/channels",
                          {"name": name, "type": 0, "topic": topic}) or {}
        state["channels"][name] = channel.get("id")
        print(f"  + #{name}")
    save_state(state)


def ensure_roles(guild_id: str, state: dict) -> None:
    existing = request("GET", f"/guilds/{guild_id}/roles") or []
    by_name = {r["name"]: r["id"] for r in existing}
    state.setdefault("roles", {})
    for name, colour, hoist in ROLES:
        if name in by_name:
            state["roles"][name] = by_name[name]
            continue
        role = request("POST", f"/guilds/{guild_id}/roles",
                       {"name": name, "color": colour, "hoist": hoist, "mentionable": True}) or {}
        state["roles"][name] = role.get("id")
        print(f"  + role {name}")
    save_state(state)


def lock_downloads(guild_id: str, state: dict) -> None:
    channel_id = state["channels"].get("downloads")
    if not channel_id:
        return
    allowed = [state["roles"].get("Beta"), state["roles"].get("Buyer")]
    overwrites = [{"id": guild_id, "type": 0, "allow": "0", "deny": str(VIEW_CHANNEL)}]
    overwrites += [{"id": role, "type": 0, "allow": str(VIEW_CHANNEL), "deny": "0"}
                   for role in allowed if role]
    request("PATCH", f"/channels/{channel_id}", {"permission_overwrites": overwrites})
    print("  = #downloads locked to Beta/Buyer")


def setup(args) -> None:
    state = load_state()
    guild_id = ensure_guild(state, args.name)
    ensure_channels(guild_id, state)
    ensure_roles(guild_id, state)
    lock_downloads(guild_id, state)
    state["name"] = args.name
    save_state(state)
    print(f"\nserver: https://discord.com/channels/{guild_id}")
    print(f"invite: https://discord.gg/{state.get('invite')}" if state.get("invite") else "")


def resolve_channel(value: str, state: dict) -> str:
    if value.isdigit():
        return value
    return state.get("channels", {}).get(value.lstrip("#"), "")


def dedupe(state: dict, channel: str, text: str) -> bool:
    rows = state.setdefault("recent", [])
    cutoff = time.time() - DEDUPE_HOURS * 3600
    rows[:] = [row for row in rows if row.get("ts", 0) > cutoff]
    digest = hashlib.sha1(f"{channel}:{text}".encode()).hexdigest()
    if any(row.get("hash") == digest for row in rows):
        return True
    rows.append({"hash": digest, "ts": time.time()})
    save_state(state)
    return False


def post(args) -> None:
    state = load_state()
    channel = resolve_channel(args.channel, state)
    if not channel:
        raise SystemExit(f"unknown channel {args.channel!r}; run setup first")
    if dedupe(state, channel, args.title + args.text):
        print("skipped: identical post within 6h")
        return
    payload = {"content": args.text[:1900]}
    if args.title:
        payload["embeds"] = [{"title": args.title[:250], "description": args.text[:3900],
                              "color": ACCENT}]
        payload["content"] = ""
    message = request("POST", f"/channels/{channel}/messages", payload) or {}
    print(f"posted to #{args.channel}: {message.get('id')}")


def upload(args) -> None:
    state = load_state()
    channel = resolve_channel(args.channel, state)
    path = Path(args.file)
    if not channel or not path.exists():
        raise SystemExit("need a known channel and an existing file")
    message = multipart(channel, path, args.text) or {}
    print(f"uploaded {path.name} ({path.stat().st_size} B) as {message.get('id')}")


def invite(args) -> None:
    state = load_state()
    channel = resolve_channel(args.channel, state)
    result = request("POST", f"/channels/{channel}/invites",
                     {"max_age": 0, "max_uses": 0, "unique": False}) or {}
    state["invite"] = result.get("code")
    save_state(state)
    print(f"invite: https://discord.gg/{result.get('code')} (never expires)")


def assign_role(args) -> None:
    state = load_state()
    role_id = state.get("roles", {}).get(args.role)
    if not role_id:
        raise SystemExit(f"unknown role {args.role!r}")
    request("PUT", f"/guilds/{state['guild_id']}/members/{args.user}/roles/{role_id}")
    print(f"gave {args.role} to {args.user}")


def dm(args) -> None:
    channel = request("POST", "/users/@me/channels", {"recipient_id": args.user}) or {}
    request("POST", f"/channels/{channel.get('id')}/messages", {"content": args.text[:1900]})
    print(f"dm sent to {args.user}")


def status(_args) -> None:
    state = load_state()
    if not state.get("guild_id"):
        print("no guild yet; run setup")
        return
    guild = request("GET", f"/guilds/{state['guild_id']}?with_counts=true") or {}
    print(f"guild: {guild.get('name')} ({state['guild_id']})")
    print(f"members: {guild.get('approximate_member_count')} · "
          f"online: {guild.get('approximate_presence_count')}")
    print("channels: " + ", ".join(f"#{name}" for name in state.get("channels", {})))
    print("roles: " + ", ".join(state.get("roles", {})))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Discord control for the product server")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("whoami", "status", "invite-url"):
        sub.add_parser(name)
    p_setup = sub.add_parser("setup")
    p_setup.add_argument("--name", default="Lyphen")
    p_post = sub.add_parser("post")
    p_post.add_argument("--channel", required=True)
    p_post.add_argument("--text", required=True)
    p_post.add_argument("--title", default="")
    p_upload = sub.add_parser("upload")
    p_upload.add_argument("--channel", required=True)
    p_upload.add_argument("--file", required=True)
    p_upload.add_argument("--text", default="")
    p_invite = sub.add_parser("invite")
    p_invite.add_argument("--channel", default="welcome")
    p_role = sub.add_parser("role")
    p_role.add_argument("--user", required=True)
    p_role.add_argument("--role", required=True)
    p_dm = sub.add_parser("dm")
    p_dm.add_argument("--user", required=True)
    p_dm.add_argument("--text", required=True)

    args = parser.parse_args(argv)
    {"whoami": whoami, "status": status, "invite-url": invite_url, "setup": setup,
     "post": post, "upload": upload, "invite": invite, "role": assign_role,
     "dm": dm}[args.command](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
