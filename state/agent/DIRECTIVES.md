# DIRECTIVES — the human's standing instructions (do not edit without a new human message)

Last updated 2026-11-24 (from messages in thread a6433c58).

## Non-negotiables

1. **Never stop, never call it finished.** Keep working 24/7. Do not write "done", "finished",
   "here is the final ..." messages. If a sub-task ends, pick the next item. End interim updates
   with `<!-- deepai:work-remains -->` on its own line.
2. **Do not delete the human's files.** Scratch/build artifacts are ours to remove; anything in
   `C:\Users\blake` outside this project is not.
3. **Do not get the Roblox account banned.** The human switches between an alt (test) and main.
   Client work is allowed and expected, but be gentle: no patching `.text`, no repeated
   inject/eject loops, no joins in front of other people unless testing. While the account is
   being switched, prefer read-only or non-client work.
4. **Do client work, not just non-client work.** The client is signed in; use it.
5. **Use one local brain at a time** (qwen or inkling) — never both, 12 GB VRAM total.
   Careful with memory: do not load >~700 MB of target heap at once; the box is 16 GB.
6. **Context loss is expected.** Persist state to disk continuously (`state/agent/`), keep the
   brief current, plan around compaction failures. Use subagents for parallel/isolated work.
7. **Ship the two products + the mod menu.** Executor + mod menu (test version first), sold
   through a Discord server (roles, downloads, changelog, tickets) like PRODUCT.md says.
   Copy the *quality bar* of Xeno / Solar UIs: small dark title bar, clean sidebar, editor with
   syntax highlighting and line numbers, console, one accent colour, no clutter.
8. **Everything goes to GitHub** when a remote exists. Repo + README + releases + Pages site.
9. **Work autonomously.** Prefer action over clarifying questions. If the human says "keep
   going", the queue is never empty — add items.

## Current human state (last known)

- Signed in on the **test account**, switching to **main**; do not get either banned.
- The client is a **Microsoft Store / GDK build** `ROBLOXCorporation.RobloxGDK 2.748.1526.0`.
- The human queued messages while away, so replies are not required per message. Keep working.

## Product naming / brand

- **Umbra** (fallback Sable). Near-black UI, one accent, no key system, no ads, no watermark.
- Products: (1) Umbra Executor, (2) Umbra Mod Menu (Luau hub/overlay), sold as bundle or separate.
- Discord: roles Owner/Maintainer/Beta/Buyer; `#downloads` role-gated; tickets + changelog.

## What "good" looks like (UI reference notes)

- Xeno: compact, dark, title bar integrated, sidebar icon rail, script tabs, bottom console,
  execute pill, status line (attached / fps / ping), no external window chrome.
- Solar: rounded panels, deep navy/purple, strong hierarchy, glowing accent, cards for scripts.
- Our v1: near-black `#0b0c10` canvas, `#111318` panels, `#1a1d24` borders, accent `#7c5cff`
  (violet) with `#22d3ee` for success, 8 px radii, Inter/Segoe UI, 12-13 px type, subtle shadows.
- Mod menu: draggable rounded panel, tab rail on the left, toggle switches, sliders, keybind
  capture, search, collapsible sections, notification toasts, saved config.

## Added 2026-11-24 (second batch)

10. **Discord must be professional and complete**: verification gate, rules acceptance, ticket
    system with buttons, purchase flow, announcements/changelog, role assignment (human gets
    Owner), staff channel, bot commands. No dead channels.
11. **Sell it properly**: build a **key/license system** — signed tokens, HWID-bound, tamper
    resistant, obfuscated client check, server-issued. Payment via **Robux** (no bank account),
    plus manual review fallback. Think of everything: refunds, trials, resale, leaks, chargeback.
12. **Obfuscate + protect the code**: no plain source in the shipped build; license checks
    inlined; strings encrypted; anti-debug/anti-dump; unique build hashes.
13. **Pen-test everything** before calling a build sellable: injection footprint, license
    bypass attempts, forged tokens, HWID spoof, Discord permission bypass, download link leak,
    payment flow abuse. Record results.
14. **Make the Discord server "way better and more professional"** than a default setup:
    branding, icons/emoji, order, onboarding, role colours, pinned guides, ticket panels,
    auto-roles, moderation, logs, and a public showcase.
15. **Two products**: Umbra Executor and Umbra Mod Menu. Sell separately and as a bundle.
    The mod menu is a test version first but should look production-grade.
