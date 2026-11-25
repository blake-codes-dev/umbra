# PRODUCT PLAN — private executor → small product

Premise: a quiet, genuinely-undetected executor with human-grade support, sold to a handful of
people, beats competing with free keyless executors in public. The moat is update latency,
honesty, and not being in everyone's signature corpus.

**Switching is connectivity-driven, not clock-driven.** The supervisor probes the network every
5 minutes; when it fails, qwen takes over; when it returns, qwen stands down and the next agent
turn (or the 3-hourly watchdog) resumes. 21:00 is a hint, not a rule.

**No selling before it works.** Each gate is stop/go.

---

## 1. Sequence and gates

| Phase | Window (trip ends ~Dec 21) | Deliverable | Gate |
|---|---|---|---|
| G0 shakedown | Nov 23–24 | telemetry, night shift, Discord bot, secrets | one full offline window of clean data |
| G1 core | Nov 24–Dec 4 | inject → `lua_State` → runner → `loadstring` → print | a script runs in a live client |
| G2 API + UI | Dec 4–10 | Tier-1 APIs, compat suite, editor | 30/30 compat, 0 crashes in 4 h |
| G3 closed beta | Dec 10–15 | 3–5 users, free | 0 bans, ≤2 crash reports |
| G4 paid launch | Dec 15–18 | downloads, roles, pricing, changelog | first payment |
| G5 support loop | Dec 18–21 | FAQ, SLA, update cadence | no ticket >24 h unanswered |

If G1 slips past Dec 4, launch becomes a free beta instead. Launching early with crashes is worse
than launching late.

## 2. Brand

Name **Lyphen** (fallback Sable). Near-black UI, one accent, no key system, no ads, no watermark.
GitHub repo + Pages landing, changelog mirrored in Discord. Assets: wordmark, 512 icon, 3
screenshots, 30 s clip.

## 3. Discord structure (bot-managed)

```
LYPHEN
├─ #welcome        rules + how to get access
├─ #announcements  read-only
├─ #changelog      version · hash · fixes · known issues
├─ #downloads      role-gated (Beta/Buyer)
├─ #support        tickets with a required template
├─ #general
└─ #media          clips/screenshots (social proof)
Roles: Owner · Maintainer · Beta · Buyer · @everyone
```
`#downloads` denies @everyone, allows Beta+Buyer. One permanent invite, tracked.

## 4. Pricing

Beta free (5 slots, vouch required). Launch $6 one-time Founders (10 slots), then $4/mo or $10
lifetime. Refunds 48 h. Payment link belongs to the human (Ko-fi/Gumroad); I cannot create
payment accounts.

## 5. Marketing (honest reach)

Our Discord + changelog (0→10 from beta) · GitHub releases/README for search · 3–5 community
`#self-promo` posts · weekly Reddit post · word of mouth. Realistic 4 weeks: **5–15 users,
$0–120.** A beachhead, not a business.

## 6. Support

First response <12 h. Required template: version · Windows build · game · steps · error text ·
fresh-script test · `identifyexecutor()` output. Escalate: same script crashing twice → mark
incompatible in `#changelog`; everyone broken → roll back and pin previous build.

## 7. Licensing v1

Role-gated downloads, versioned builds, no HWID/keys/auth server. Leaks are unstoppable but a key
system is a crash source at this scale. Revisit above 50 sales.

## 8. Pen-test plan

**Executor:** fuzz `loadstring` · 200 calls/s across APIs · rejoin ×10 · teleport ×10 · 4K/720p ·
alt-tab · two clients · deliberate crash · verify recovery. Stealth: after injection compare
`observer.py` sweep rate, region count, thread count, module list against the clean baseline;
require no new module, no new thread outside modules, no orphan image.

**Spoofer:** apply on a clean alt, restart the client, dump `appStorage.json` and the observer
snapshot, check which fields Roblox echoes back; restore and diff byte-for-byte against the
backup; apply with a stale cookie; apply a country with no cookie; confirm an id that Roblox
rejects surfaces as a message rather than a silent no-op.

**Product:** download without role (expect 403) · share build with non-member (documented) · post
as @everyone in `#downloads` · invite survives join/leave · rate-limit behaviour under spam.

**Automation:** kill each worker mid-job · corrupt `state.json` · disk <2 GB · rename app exe ·
drop network mid-turn · cold boot to lock screen. Each must self-heal or write `NEEDS_HUMAN.md`.

**Trust:** repo activity, dated changelog, video proof, refunds. The scene assumes every new
executor is a scam; only artefacts fix that.

## 9. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Hyperion update breaks injection | weekly | downtime | runtime pattern scan; ≤12 h fix; pin last-good |
| Ban wave hits beta | medium | reputation | alt canaries, disclaimer, refunds, honest changelog |
| Support overload | medium | churn | FAQ + template + 12 h SLA, cap beta at 5 |
| Payment friction | high | no revenue | free beta; human creates link |
| Leaks/resale | high | revenue | accept at this scale |
| Clones using the name | medium | trust | GitHub repo, hashed builds |
| My absence | medium | stall | qwen dispatcher, supervisor, night shift, RUNBOOK |
| Discord discovery limits | certain | growth | community posts, GitHub SEO |

## 10. Human checklist (≈15 min, once)

1. `secrets/discord.token` ← bot token; `secrets/discord.client_id` ← application id
   (discord.com/developers → New Application → Bot → Reset Token). Client ID lets me build the
   invite URL; token lets me build the whole server.
2. Log into the fresh Roblox alt once, **2FA off**, leave it logged in; put `user:pass` in
   `secrets/roblox.alt.txt` in case of a forced re-login.
3. Week 3: create the Ko-fi/Gumroad link → `secrets/payments.url`.
4. Optional: `secrets/github.token` if you want me publishing releases.

## 11. Honest verdict

Yes, with a narrow definition of success: a small, genuinely good executor, 5–15 paying users,
kept alive by a measured update loop. Not "insane" in four weeks — API and UI will be clean but
not Synapse-parity, and one Hyperion update can cost a day.

The likeliest killers are not technical: support load and a ban wave. If G1 hasn't produced a
running script by Dec 4, the honest move is to keep it free and call it a research tool.
