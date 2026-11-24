import json, pathlib, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
tok = (pathlib.Path("/config/.deepai-code/scratch/2026-11-24-try-to-go-into-github-5017c798/.secrets/discord.token")
       .read_text().strip())
GUILD = "1494098236716089434"
API = "https://discord.com/api/v10"


def api(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bot " + tok)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "UmbraProbe")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            b = r.read()
            return json.loads(b) if b else None
    except urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode()[:300]}


chans = api("GET", f"/guilds/{GUILD}/channels")
for c in sorted(chans, key=lambda c: (c["position"], c["type"])) if isinstance(chans, list) else []:
    print(f'{c["type"]:>2} {c["position"]:>3} {c["id"]} {c.get("parent_id")} {c["name"]}'
          + (" ARCHIVED" if c.get("thread_metadata", {}).get("archived") else ""))
if not isinstance(chans, list):
    print(chans)

print("\n--- products channel messages ---")
prods = [c for c in chans if c["name"] == "products"] if isinstance(chans, list) else []
for c in prods:
    msgs = api("GET", f"/channels/{c['id']}/messages?limit=5")
    for m in msgs if isinstance(msgs, list) else []:
        print(json.dumps({"id": m["id"], "embeds": m.get("embeds"), "components": m.get("components")}, indent=1)[:2500])

# ---------------------------------------------------------------- state sync
if __name__ == "__main__" and "--sync" in __import__("sys").argv:
    import re as _re
    chans = api("GET", f"/guilds/{GUILD}/channels")
    state_path = pathlib.Path(__file__).resolve().parent.parent / "state" / "discord.json"
    state = json.loads(state_path.read_text())
    mapping = {}
    for c in chans:
        if c["type"] not in (0, 2):
            continue
        key = _re.sub(r"^[^\w]+", "", c["name"]).lower()
        mapping.setdefault(key, c["id"])
    state["channels"] = mapping
    state["guild_id"] = GUILD
    state_path.write_text(json.dumps(state, indent=1))
    print("synced", len(mapping), "channel keys")
