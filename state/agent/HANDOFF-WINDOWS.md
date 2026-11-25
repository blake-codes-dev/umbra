# Handoff: continue Umbra on the Windows box

You are picking up a Roblox executor + in-game mod menu + spoofer called **Umbra**. Another agent
built it inside a Linux container (WSL, no Roblox, no GPU) and verified everything it could under
Wine. You have the real thing: Windows, MSVC, and Roblox itself. Your job is to finish the last
mile: build natively, prove the executor injects and runs a script inside Roblox, and fix whatever
breaks now that the code meets the real client.

Read these first, in this order:

- `README.md` - what the product is
- `state/agent/BRIEF.md`, `state/agent/RULES.md` - the standing rules
- `docs/build.md` - build + release
- `docs/protocol.md` - the bus and named-pipe wire format (this is the load-bearing design)
- `docs/API.md` - honest per-API status table (tier 1 = works, tier 2 = not implemented)
- `WORKLOG.md` - chronological log of what was done and why

## Where things are

```
src/app/Umbra/            WPF GUI (.NET 9, net9.0-windows, win-x64)
  Services/Injector.cs    manual-map PE loader (Parse/Relocations/Imports/CreateRemoteThread)
  Services/Runtime.cs     copies runner.luau + api.luau into %LOCALAPPDATA%\Umbra
  Services/PipeBackend.cs named-pipe client speaking docs/protocol.md
src/native/core.c         the injected payload (no CRT, PIC, entry DllMain)
src/native/build.bat      MSVC build for stub.dll + core.dll
src/luau/runner.luau      in-VM runner: one native call in, pumps itself on Heartbeat
src/luau/api.luau         tier-1 executor API installed into every script env
src/luau/menu.luau        the mod menu (tabs, sliders, keybinds, live log)
src/luau/library/*.luau   14 ready-to-run scripts, one-click load in the menu
tools/build_menu.py       inlines library/*.luau into menu.luau (`--check` must pass)
tools/release.sh          builds payload + GUI, publishes dist/Umbra-<v>-win-x64.zip
tools/umbra_bot.py        Discord bot (tickets, filters, help modal)
tests/host.c              native harness: runs core.c against a real Luau VM, 22 checks
```

## Design decision you must not undo

The executor is **one native call in, then it pumps itself**. The injected core pushes `runner.luau`
into the target `lua_State` and calls it once. The runner drains a command ring on
`RunService.Heartbeat`, so no yield ever crosses a native frame and the core adds **no thread**.
Commands and output go through a shared-memory bus (64 KiB header + 60 x 1 KiB command slots +
100 x 512 B output slots) mapped at `%LOCALAPPDATA%\Umbra\bus-<pid>.bin`. A named pipe
`\\.\pipe\umbra-<pid>` carries the same events to the GUI. If you are tempted to spawn a thread or
hook a keyboard poll to run scripts, don't: it changes what the anti-cheat can see.

Second design decision: **`.text` is packed (Byfron)**, so RVAs read from the file are garbage.
`core.c` therefore self-calibrates in memory: find the `lua_State` structurally, find function names
in readable sections, follow rip-relative references to the call sites, and identify each Luau API by
the *shape* of its body (a store to `[reg+0x80]`, a `ret`, a shared tail). All 12 anchors resolve at
runtime on the current client build. If a client update breaks one, `resolve()` refuses and the core
falls back to the injected-thread install path instead of guessing.

## What is verified, and how

`tests/host.c` links `core.c` against a real Luau VM and runs it under Wine (mingw in-container) or
natively on Windows:

```
build:  vcvars64 && cl /nologo /O1 /DUMBRA_HOST tests\host.c src\native\core.c ...
run:    dist\test\RobloxPlayerBeta.exe
```

21 of 22 checks pass: pipe open/connect, runner install, heartbeat, print capture, done flag,
file round-trip, `..` refusal, genv persistence, error channel, hello->ready, exec over pipe,
log line back, calibration. The GUI builds and publishes; the mod menu and its 14 scripts build.

Every run writes `dist/test/last.log`; a crash also writes `RobloxPlayerBeta.crash.log`
(exception code, rip, rsp, rax/rcx/rdx, 24 stack words) plus a timestamped copy. Read the crash log
before anything else when something dies.

## The one open defect

**`a hooked call runs and unwinds, repeatedly`** - the VM-thread handoff hook.

`install_hook()` patches the first instruction of a Luau API (currently `lua_pcall`) with a 5-byte
`E9` jump to a stub in a `VirtualAlloc` page near the target. The stub saves rax/rcx/rdx/r8/r9,
calls `hook_thunk` (which installs the runner the first time it fires, so the install happens on the
VM thread), restores, then jumps to a *replay* buffer holding the displaced instructions followed by
a jump back to `target + displaced`. Displaced bytes always cover whole instructions.

Current state under Wine: the patch, the stub and the replay bytes all verify correct, the replay's
jump decodes back to exactly `target + displaced`, and yet the hooked `lua_pcall` returns
`stack overflow` with a 3-slot stack drift. With the thunk bypassed (bare `E9` -> replay) the drift
is unchanged, so the suspect is the replay of the prologue, not the stub, the register saves or the
alignment. Work through it on Windows where you have a real debugger:

1. Build the harness with MSVC, run it, confirm whether the failure reproduces natively.
   If it does not reproduce, the failure is a Wine artifact and the hook is fine.
2. If it reproduces, single-step from the `E9` at `lua_pcall`, through the stub, through the replay,
   and compare rsp/rbp and the 6 pushed registers against an unhooked call. The `+0x10`/`+8` slots
   of `lua_pcall`'s frame are where the mismatch will show.
3. Known-good fallbacks if the hook proves fragile: install the runner from a **hijacked existing
   client thread** (queue a user APC to any thread inside the VM, or detour a thread that already
   calls into Luau) instead of `CreateRemoteThread` + `lua_pcall` hook. That is also the next
   undetectability step, so it is not wasted work.

## Then, in this order

1. `src\native\build.bat`, then `tools\release.sh` equivalent on Windows (or use the container
   script if it still runs). Ship `Umbra.exe` + `library\` + `luau\` + `payload\core.dll`.
2. **Live test with an ALT account, never the main.** Launch Roblox, join any game, start Umbra,
   Attach, then load one library script from the menu. Watch the GUI log. Success = the mod menu
   window appears in-game and a script like `fullbright.luau` visibly works.
   Rules: no `.text` patching of the client, no re-attach loops, three launches maximum in a row
   before you go read the code again.
3. Freeze the calibration: the core prints the 12 resolved anchors. Put those RVAs/signatures into
   `patterns.json` / `src/native/patterns.h` so a live run stops searching.
4. Verify the spoofer: device identity (see `docs/spoofer.md`) and account location
   (`docs/account-location.md`) with the alt signed in - read, change, read back, relaunch and
   confirm the rewrite sticks. Note the result in the doc; it is documented as revertible, keep it
   honest.
5. Replace `CreateRemoteThread` with the hijacked-thread install.
6. Start the Fortnite mod menu only after a chunk runs in Roblox.

## Rules

- Never say the project is finished; keep working, and log in `WORKLOG.md` as things land.
- Prefer a run over a rewrite: this codebase is late-stage, most defects are small.
- Never leave the user without a working download: after any change, ship a version and post its hash.
- Do not get stuck in a loop. If the same check fails three times, change the oracle (new test, real
  client, new debugger view), not the same code again.
- Don't break the "one native call in" property, the sandboxed file API (`..` refused), the bus
  layout in `docs/protocol.md`, or the `--check` invariant in `tools/build_menu.py`.
