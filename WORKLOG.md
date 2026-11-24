# WORKLOG — append-only, newest last

One line per work block: what changed, what was measured, what is next.

- 2026-11-24 11:06 · M1 measured: 0 modules/threads/windows, +2 RX pages, 5 injections alive; sigfix repaired 11 sigs. Blocked on Lua state.
- 2026-11-24 17:00 · Found live `lua_State` structurally: L+0x58=ci, L+0x68=shared global object, L+0x80=top; `global_State` object at 0x2144daf2160 has mainthread at +0x2f0. TString layout measured (data +0x18, len +0x14); "loadstring" TString = 0x22620252e10.
- 2026-11-24 17:05 · Client was sitting in tray at Home (no game). Deep link `Start-Process roblox://experiences/start?placeId=3381587` launches a join; log shows experience_launch_begin. Left it at Home to avoid account risk during switch.
- 2026-11-24 17:15 · Persisted MISSION/DIRECTIVES + context system; next: find luaB_loadstring through globals table, then luau_load signature, then native call.
