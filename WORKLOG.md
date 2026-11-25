# WORKLOG — append-only, newest last

One line per work block: what changed, what was measured, what is next.

- 2026-11-24 11:06 · M1 measured: 0 modules/threads/windows, +2 RX pages, 5 injections alive; sigfix repaired 11 sigs. Blocked on Lua state.
- 2026-11-24 17:00 · Found live `lua_State` structurally: L+0x58=ci, L+0x68=shared global object, L+0x80=top; `global_State` object at 0x2144daf2160 has mainthread at +0x2f0. TString layout measured (data +0x18, len +0x14); "loadstring" TString = 0x22620252e10.
- 2026-11-24 17:05 · Client was sitting in tray at Home (no game). Deep link `Start-Process roblox://experiences/start?placeId=3381587` launches a join; log shows experience_launch_begin. Left it at Home to avoid account risk during switch.
- 2026-11-24 17:15 · Persisted MISSION/DIRECTIVES + context system; next: find luaB_loadstring through globals table, then luau_load signature, then native call.
- 2026-11-24 13:40 · server rebuilt properly: 6 ordered categories, 16 channels, 7 roles, VIEW-gated downloads + staff, read-only info, community features on (rules screening, welcome screen, announcement channel, verification level 2), icon, plain-English copy. lyphen_discord.py now has apply/doctor/seed --clear and is idempotent. One manual step left: drag the bot role above the rest, the API clamps positions to its managed role.
- 2026-11-24 22:10 · tickets: close was swallowed by the `ticket:` prefix so every Close click opened a new ticket. Close now runs first, deletes the thread (archive is only the fallback), and the opener or staff may fire it. Repeat opens bump the existing thread instead of stacking duplicates. Help opens a modal form, so there is no template to copy. Products panel rewritten onto one PRODUCTS table with aliases, which is what the all-three button needed: its id must normalise to `bundle`.
- 2026-11-24 22:35 · menu carries 14 shipped scripts (`src/luau/library/`, inlined by `tools/build_menu.py` between generated markers, `--check` in CI terms). Load button runs one without loadstring, edit drops it in the editor, search filters. Panel opens and closes on a 0.16 s tween and toasts the result.
- 2026-11-24 23:05 · executor: two surfaces (executor window, spoofer window), display/text/mono faces instead of one default, 120-180 ms hover and page motion, home page with quick scripts. Spoofer is a real one: identity read, per-field regenerate, backup first, restore, profiles, admin banner with a copyable `Start-Process -Verb RunAs` prompt, and an account-location card that calls the client's own endpoint and learns the country ids it sees. docs/account-location.md records what is measured and what can revert.
- 2026-11-24 23:40 · release path moved into the container: .NET 9 installed to /tmp/dotnet, WPF cross-targets with EnableWindowsTargeting, `tools/release.sh` builds → obfuscates (Obfuscar, unicode names) → single-file self-contained zip + hash. XAML event handlers are public and the window types are excluded because BAML resolves handlers by name; obfuscation silently kills them otherwise. Published as a prerelease with the hash in #builds, and #status + #tos now say what the build is and what it changes.
- 2026-11-25 00:20 · executor core in source: `src/luau/runner.luau` (bus drain on Heartbeat, per-script env, print/warn capture, xpcall+traceback, cancel) and `src/luau/api.luau` (tier-1 API, bridges error by name when missing). `src/native/core.c` does the real sequence: bus create+map, structural lua_State find (resting-state invariants, 256 MB cap), one luau_load+pcall of the runner, pipe serve, named-stage failures; compiles clean under mingw with SEH macros for validation and MSVC-only SEH in the shipped build. docs/protocol.md and docs/API.md pin the contract so the native side and Luau cannot drift.
- 2026-11-25 00:35 · wine: built and *ran* the app headless (wine 10 + xvfb). Found two real bugs wine exposed: duplicate `Mono` resource key (fatal on Windows too) and text styles that inherited no font. Fixed, plus Bold instead of SemiBold and a fontconfig alias set. Animation templates still kill it under wine; the build is shipped for Windows and the user will say if it opens.
- 2026-11-25 01:10 · native bridges done and linking: workspace-sandboxed file ops (write/read/append/list/delete/mkdir, `..` refused), WinHTTP request returning status+body, SendInput mouse/keys, clipboard, foreground-window check, bus push/take/stop. Module base from the PEB, addresses as base+RVA from generated patterns.h with prologue-scan fallback; missing anchors now fail by name at the scan stage instead of calling null. Luau side rebound via `api.bind` before any env exists.
- 2026-11-25 01:55 · harness: `tests/host.c` builds core.c against a real Luau VM and runs the whole chain under wine. Caught three real defects at once: `luau_load` takes bytecode (added `luau_compile` to the chain), `pcall` takes the callee from top-(nargs+1) so the runner must be pushed before its arguments, and the bridge's raw `loadstring` was overwriting the runner's env-applying one so per-script envs never applied. Suite passes 8/8: runner install, heartbeat, print capture, done flag, file round trip plus `..` refusal, genv persistence, error channel.
- 2026-11-25 02:40 · pipe is now real in both directions: core is the server, `hello` answers `ready`, `exec`/`cancel`/`bye` become bus commands, and one drain forwards every output slot as JSON (`log`/`fail`/`done`). Harness covers the whole gui path (17 checks green). `Injector.cs` manual-maps core.dll (sections, relocs, imports bound against the target, one remote call), the attach button finds the client itself, `Runtime.Ensure` stages the Luau, and the mod-menu button now executes instead of copying. All risky reads moved to `safe_read`, which uses SEH on MSVC and ReadProcessMemory on mingw so the mingw-built payload is not left unguarded.
- 2026-11-25 03:20 · the client's `.text` is packed: every static xref scan came back empty, byte histogram uniform, no prologues in the file. So `resolve()` self-calibrates in memory - literal name, its reference, the call targets in that function, recognised by a store to `L->top` plus an early ret; load is the last such callee in luaB_loadstring, pcall the one in luaB_pcall, with a fallback. Picks are reported so a live run freezes them into RVAs. Harness proves the whole path against a fake luaB_loadstring/luaB_pcall pair.
- 2026-11-25 03:55 · two more anchors derived instead of guessed: `luau_compile` as the call immediately before `luau_load` inside luaB_loadstring, and `lua_gettop` by its unique shape (`mov rax,[rcx+0x80]; sub rax,[rcx+0x78]; sar rax,4; ret`). Partial resolution now degrades by name instead of crashing: a missing table api fails at stage `resume`, field() guards itself, and the runner says the compiler is missing rather than calling nil.
- 2026-11-25 04:10 · push anchors: all `lua_push*` share one tail (`add qword [rcx+0x80], 0x10` then ret), so they are found by that tail and told apart by what they store (byte from dl = boolean, SSE store = number, internal call = string). Remaining unresolved: createtable, setfield, rawseti, tolstring, tonumber - all table/manipulation shapes that need a live run to confirm.
- 2026-11-25 04:30 · setfield and rawseti found by their pop (`sub qword [rcx+0x80], 0x10` / `0x20`) plus a following call; createtable by a call followed by a push where the third argument register is read. Ten of the twelve anchors now self-resolve. Only tolstring and tonumber remain, which return values rather than moving the stack and so have no reliable blind shape.
- 2026-11-25 04:55 · last two anchors: tolstring and tonumber both work on the -1 slot (`mov rax,[rcx+0x80]; sub rax,0x10`) and differ only in tonumber reporting through its third argument, so a store into [r8] decides which is which. All twelve now resolve, and resolve() requires the full set. Re-injection no longer leaks: the payload carries a header marker that is found and freed before the next map.

## 2026-11-25 05:00 - hook trigger fixed, channels to the Windows box

- The stack drift under the hook was the trigger, not the trampoline: `lua_pcall` sets up error
  handling, and a `VirtualAlloc` replay page carries no unwind info. Moved the trigger to
  `lua_gettop` (throw-free, called constantly, no frame). Harness now asserts both properties:
  a hooked leaf call is undisturbed over five rounds, and the hook fires on the VM thread and
  installs there. 22/22 green.
- Communication with the Windows agent: `tools/mail.py` (append-only mailbox over the shared 9p
  mount, instant, offline), `tools/relay.py` (#relay on Discord, needs the bot token), and git for
  the record. See `state/agent/mail/README.md`.
- Shipped v0.1.7-dev (95a45d45...), asset uploaded, #builds posted.
- 2026-11-25 00:50 · hook defect reproduced natively with MSVC and fixed, three real bugs deep: (1) every install reused one stub page, so a second hook overwrote the first hook's replay; each hook now owns a 256-byte slot; (2) re-installing on a target whose entry was already our `E9` replayed that jump, so the tail landed mid-patch — installs are now idempotent per target and rebuild from the saved prologue; (3) displaced instructions were replayed verbatim at a new address, so `jmp rel32`/`call rel32`/rel8 branches and rip-relative operands went to the wrong place (an ILT body is nothing but `jmp rel32`) — `fix_replay` re-encodes all of them, and refuses a hook it cannot fix. Also: the stub never reserved the 32-byte shadow space, so `hook_thunk` spilled over the five saved registers; the pops then restored garbage. Suite goes from crash to 24/24 natively at /Od and /O2, and `src\native\build.bat` builds core.dll under MSVC for the first time — 34 KB, no CRT, `/GS-`, with the handful of crt symbols the compiler and SEH dispatch still emit provided in-file (CFG thunk, section check, heap wrappers, isa/favor words) instead of dragging in CRT startup.
- 2026-11-25 00:52 · harness portability: the fakes now carry an ATTR_NOINLINE/used pair for both compilers, a 3-byte `fake_wait` was too short for a 5-byte patch (it ate the next function's entry, which is what made the check look Wine-specific), and setvbuf plus a faulting-module dump make a crash readable instead of silent. tests/README will note the suite is built /Od so the synthetic anchors keep the shapes the scanner looks for.

## 2026-11-25 05:52 - no thread of our own; 0.1.8-dev

- Install is now an APC to the client's existing threads, entry re-entrant guarded, remote thread
  only as a two-second fallback. Fewer objects for an anti-cheat to notice.
- Hooks got a stub arena (several can be resident) and `fix_replay` learned rip-relative operands -
  a `mov eax,[rip+x]` prologue used to be replayed unpatched and faulted. Decoder now reports the
  flag instead of a hand-written opcode table.
- `release.sh` needed `-Wl,--allow-multiple-definition`: mingw's pesect.o and the CRT both define
  `_IsNonwritableInCurrentImage` once image sections are touched.
- Shipped 0.1.8-dev (fa2d047f...), asset + hash posted, mail and relay pinged to the Windows side.

## windows, hijack + rename

- CreateRemoteThread is dead inside the client: a thread created at kernel32!Sleep exits
  STATUS_INVALID_THREAD (0xC000071C) in 300 ms without executing. Own-thread fallback removed.
- install is now: suspend a client thread, point rip at a stub that saves 15 regs + rflags + rsp,
  calls the entry, restores, jumps back. three threads pinned, plus apcs, entry self-guards.
- mapper fixes that mattered: reloc table needs rva->file-offset (only mingw builds had an empty
  reloc dir, so the old code never noticed), imports need per-process rebasing via toolhelp, the
  COFF entry is DllMain which a manual map never calls so the export is called instead, and the
  payload registers its own .pdata with RtlAddFunctionTable or no __try works at all.
- product renamed umbra -> lyphen everywhere (paths, pipe names, %LOCALAPPDATA%, types, docs).
- wall: client text is packed. only pages the game has already executed read back, the rest is
  zeros, so every anchor reports "unmeasured" and calibrate() faults inside the string->ref path
  and takes the client down with it even though the handler catches the fault.

- **the mapper was relocating against the wrong base.** rm -rf of a pointer crash: ApplyRelocations
  used 0x140000000 (exe) for a dll linked at 0x180000000, so every relocated pointer landed 1 GB
  high and every deref of a .rdata pointer faulted. now reads ImageBase from the optional header.
  after that the payload runs all the way through calibrate without a fault.
- scan cost matters more than it looks: a safe_read per byte took minutes over the 50 MB rdata and
  the client died mid-scan, so find_name/find_reference now copy one page and search in memory.
- state: attach + hijack + pipe are solid, calibrate completes, but every anchor is still 0 and
  state_from_scriptcontext finds no state, so nothing Luau resolves yet.

## 2026-11-25 03:10 - paging model measured
The .text wall is not "encryption we cannot see", it is Hyperion committing pages lazily:
- pages the client has executed read back as real code and stay readable
- pages it has not executed are PAGE_NOACCESS *and unbacked*, so even after
  VirtualProtectEx to rwx they read as failure (not zeros-from-a-pagefile)
- guard pages trap even when the bytes are there, and a frame handler
  (__except) sits nearer in dispatch than Hyperion's vectored handler, so our
  own handler was eating the trap that produces the decrypt
Fix: `armed_page()` calls VirtualProtectEx to clear guard/noaccess *without*
taking a trap, then the read is a plain copy. The probe now finishes all seven
rvas without killing the client: 0x1000, 0x58c2a40 (luaB_print) and 0x2566f90
(lua_tolstring) read real bytes because those pages are hot; 0x30000, 0x4f93e0,
0x254da20, 0x20a0000 fail because nothing has run them yet.
Consequence: a byte scan can only ever see executed pages, so pinned rvas remain
the right answer and calibrate is mostly a diagnostic.
Also: state_from_scriptcontext now disarms each 4 KB page as it walks and bounds
every deref through window_ok(), so the 1 GB heap walk no longer trips guards.

## 2026-11-25 05:55 - the dump is a different build, and acg
Two measured facts that explain a day of dead ends:

1. `dump/merged-all.bin` is **not** the binary we attach to. Sections match to the byte (both are
   2.748.1526) but the bytes at `0x58c2a40`, `0x2566f90`, `0x254da20`, `0x2584420` are different in
   the live client, and the file on disk at those rvas is ciphertext. So every rva derived from the
   dump is stale. The ones that work were measured live: `tools/resolve_names.py` walks the globals
   table (TString key -> tag 7 value -> closure -> closure+0x28) and returns real rvas - pcall
   `0x58c8030`, print `0x41dc170`, loadstring `0x41dc080`, tostring `0x58c8a70`, type `0x58c6820`.
   Those three are pinned now; the rest of patterns.h needs the same treatment.

2. The client runs with **Arbitrary Code Guard**. `VirtualAlloc(PAGE_EXECUTE_READWRITE)` fails with
   `1655 "the operation was blocked as the process prohibits dynamic code generation"`, and so does
   `VirtualProtect` on an image page. That is why no hook ever installed: the stub page could not be
   allocated and the patch could not be written. Fix in progress: stubs live in a `.lystub` section
   of the payload (mapped `RWX` from the headers) and patches go through `NtProtectVirtualMemory`.

Third, a smaller one: `luaB_print` / `luaB_pcall` called straight from the hijacked thread raise
`0xe06d7363` (a C++ exception) inside the client, so the smoke never returns. Since our frames carry
no C++ handler and the stub has no unwind info, that exception is not resumable for us.

State discovery, which used to be a blind 512 MB crawl that killed the client, now works from
outside: `tools/getctx.py <pid>` finds a resting `lua_State` (gs+0x2f0 -> L, L+0x68 -> gs,
base==top, ci==ciBase) plus the loadstring/print closures by scanning for their code pointer at
closure+0x28, and writes `%LOCALAPPDATA%\Lyphen\state.txt` as "state closure print". The payload
parses that file. Offsets verified against a real state: `L+0x20` ciBase, `L+0x58` ci, `L+0x68`
global, `L+0x70` func, `L+0x78` base, `L+0x80` top, `nci` at `L+0x34`.

## 2026-11-25 13:50 - acg, cfg, and why the client kept dying

The client runs with **Arbitrary Code Guard and Control Flow Guard**. Both took a measurement each,
and both changed the design:

- ACG: `VirtualAlloc(MEM_COMMIT, PAGE_EXECUTE_READWRITE)` -> 1655 "process prohibits dynamic code
  generation"; `VirtualProtect(RWX/RW)` on an image page -> 5/87; `WriteProcessMemory(self)` -> 998.
  Out of process is different: `VirtualProtectEx` 0x04/0x40 -> 87, **0x80 PAGE_EXECUTE_WRITECOPY ->
  OK**, 0x20 -> OK, and the write then lands (verified byte for byte). So a private exec page is
  impossible; stubs go in a `.lystub` section mapped `0x40` from our headers, and every patch is made
  by the injector with the WRITECOPY dance. Nothing of the old in-process trampoline design survives.
- CFG: the death was the **indirect jump at the end of the hijack stub**. Parsed the crashpad
  minidump: `rip` = stub+0x68 (that `jmp [r11]`), `r11` = the correct slot, slot content = a valid
  `win32u.dll` address, page committed RWX, code c0000005 - operands fine, branch rejected. Replaced
  every `jmp rax` / `jmp [r11]` with `push; ret` (ret is not a CFG-checked branch), in the hijack
  stub, the hook stub and the replay tail. The client now survives the whole entry; before that it
  died 2-3 s after "entry ret" every single time, which is what looked like "the entry kills it".
- Hijack slots: the stub used to keep one shared rip/rsp pair for all pinned threads, so thread A
  resumed at thread B's rip with A's rsp. Now eight stub copies with their own pair.
- **The patch does not survive.** Applied and verified, and seconds later the page is the original
  prologue again - hyperion re-arms and restores. The injector therefore re-lands every requested
  patch on a 120 ms watchdog (byte compare first). A one-shot patch is a false pass.
- Also fixed the //./pipe enumeration stall with a marker file, made the pipe instance unlimited and
  gave `getctx`'s output a proper use.

Measured, worth keeping: nothing in a live client calls the base library closures. `pcall`, `print`
and `tolstring` hooks install cleanly and never fire on a server, because the engine calls its C API
directly and only Lua code goes through `luaB_*`. So the trigger is now the direct call from the
hijacked thread (`stop.txt` containing `d`), which is what the smoke needs anyway.

## 2026-11-25 11:31 - the one-line bug behind everything

Every hooked call returned a pointer instead of its result: the replay tail parked the resume
address in rax, and rax is the return value of most functions. gettop handed the VM its own address
where a slot count belonged, which is why patches "fired" and nothing happened, and why one hooked
call was enough to wreck the frame.

Three more of the same family, all fixed:

* stub allocated 0x28 of shadow space where 0x20 is what keeps rsp 16-aligned at the call; every
  movaps spill in the thunk faulted on a misaligned frame (the c0000005)
* install_now claimed its flag after the state work, so the first Luau call re-entered the hook and
  started a second install inside the first
* RtlAddFunctionTable entries were registered against the client module base while the codes live in
  ours, so ntdll resolved garbage and read -1

The harness had also been lying: it links a Luau with an unrelated lua_State layout, so every
hardcoded client offset read the wrong field there. Host builds now derive them from the linked
build; client builds keep the measured constants.

Suite 26/26 and calibration ok for the first time. Rules written up in docs/hooks.md so they are not
re-learned.
