# QUEUE — the job. A turn is a shift.

Take the top unchecked item. Finish it or record the blocker. Tick it. Next.
Do not end a turn just because a sub-task finished; end interim messages with
<!-- deepai:work-remains --> on its own line.

## Now
- [ ] Discord: Owner role to human, Verified gate, ticket panel, purchase panel, rules, logs
- [ ] Discord bot process: verification button, ticket open/close, /key, /buy, /status
- [ ] Key system: Ed25519 signed licenses, HWID binding, trials, revocation list
- [ ] Payment: Robux gamepass page + manual verification + receipts in #downloads
- [ ] Mod menu v1 (Luau, standalone, polished): tabs/toggles/sliders/keybinds/config/notify
- [ ] Executor UI v1 (WPF): editor, tabs, console, attach/execute, settings, license dialog
- [ ] Landing page + README screenshots + GitHub Pages
- [ ] Pentest checklist and results
- [x] Native core: luau_load + pcall call path written; the call itself is unmeasured until the box
- [x] Native bridges: files, WinHTTP, input, clipboard, foreground, bus closures

## M1 — injector
- [x] stub DLL built + manual map validated
- [x] host: manualmap.py (relocations, imports, entry via hijack)
- [x] live injection survives 30 s+, no new module/thread
- [x] unmap stale images on re-injection: header marker, found and freed before the next map

## M2 — lua_State
- [x] offsets measured: ci=0x58, global obj=0x68, top=0x80, CallInfo 0x30, TString data=0x18
- [ ] resolve globals table from lua_State (LuaTable at L+??), find loadstring closure
- [x] every entry point is located at runtime by shape, not by disassembly; stamping RVAs is now optional
- [ ] one native->Luau print

## M3 — runner
- [ ] native command queue + runner chunk + print capture

## After Roblox ships
- [ ] **Fortnite mod menu** - second product, same shape as this one: desktop surface + in-game
      menu, sold next to Lyphen. Notes to start from, not decisions yet:
      - in-game side is UE (UObject/SDK) rather than Luau, so the runner design ports but the
        script layer does not; UMG or a Slate/ImGui overlay draws the menu
      - injection is a signed-driver-free route (DLL + manual map) or the Easy Anti-Cheat safe
        path chosen per game build; EAC/BattlEye presence is the whole difficulty
      - keep the two products in one repo family so the store, bot, updater and licence flow
        are shared, and only `src/game/` differs
      - do not start until the Roblox executor runs a chunk in a live client, so the shared
        pieces (bus, runner loop, updater, store) are proven first
- [ ] **undetected first, same as Lyphen** - no module, no thread, no window, nothing written to
      the game folder; hand-rolled mapping only, and a signature report of our own so a ban wave
      is diagnosed instead of guessed
- [ ] **kernel driver, phase 2** - a signed driver to get around the anti-cheat's user-mode
      surface: handle-based read/write, hide our pages from its scans, and hide the driver itself
      (unloaded on exit, random name). Signing is the cost: EV cert + attestation, or a
      vulnerable-driver (kdmapper) bootstrapping stage first, then our own cert when it earns it.
      Keep the driver optional so the app still works user-mode only - it is an upgrade, not a
      requirement, or the store has to explain a blue screen to a buyer
