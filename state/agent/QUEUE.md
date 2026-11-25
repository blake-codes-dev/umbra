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
- [ ] unmap stale images on re-injection (8 KB leak per run)

## M2 — lua_State
- [x] offsets measured: ci=0x58, global obj=0x68, top=0x80, CallInfo 0x30, TString data=0x18
- [ ] resolve globals table from lua_State (LuaTable at L+??), find loadstring closure
- [ ] find luau_load from luaB_loadstring disassembly, then stamp its prologue into patterns.json so gen_patterns picks it up
- [ ] one native->Luau print

## M3 — runner
- [ ] native command queue + runner chunk + print capture
