# Roblox Executor — Engineering Plan

Status: draft v1 · Target: Windows x64 desktop client · Author: DeepSeek

---

## 0. Facts about this machine (measured, not assumed)

| Fact | Value | Why it matters |
|---|---|---|
| Client package | `ROBLOXCorporation.RobloxGDK` (Microsoft Store / GDK build) | Different ACLs, different launcher, no `%LOCALAPPDATA%\Roblox\Versions` |
| Exe | `C:\Program Files\WindowsApps\...\RobloxPlayerBeta.exe` (137 MB) | Process name unchanged → discovery is easy |
| Sibling | `RobloxPlayerBeta.dll` (16.9 MB) | Engine may be split; **scan every loaded module**, not one file |
| Version now | `2.748.1526.0` (logs show 2.747 → 2.748 within ~4 days) | Versions churn weekly → offsets must be scanned, never hardcoded |
| Reading the exe from disk | **Access denied** (`Copy-Item` fails) | Offline pattern work needs a memory dump, or a non-Store install |
| Process | running, PID 19980, session 1, user-level | Attach path is testable right now |
| CPU / RAM / OS | i9-9900K / 16 GB / Windows 11 26300 | Fine for building |
| Toolchains present | .NET SDK, Python 3.13, Node 24. **No clang/gcc/rust/cmake** | C++ needs a compiler install (or Zig) |

**Consequence:** the Store build is a workable but awkward target. Recommendation: install the
normal web client from roblox.com **as well** and develop against it
(`%LOCALAPPDATA%\Roblox\Versions\version-*\RobloxPlayerBeta.exe`, readable on disk, directly
launchable, the build every public executor targets). Keep GDK as the second supported target.
This single decision deletes several unrelated risks (ACL, launcher/activation, auth).

---

## 1. Scope

**v1 is a real executor:** launch-or-attach Roblox → inject → run arbitrary Luau passed from a
desktop GUI → real `game` environment, `loadstring`, hooks, files, HTTP, console output.

In v1: single client instance per pipe is fine (multi supported by design), x64 only, Player
client only (not Studio), no drawing API (`draw.*`), no decompiler (stub), no Luau-VM-per-actor.

Out of v1: `decompile`/`getscriptbytecode`, `draw`/ImGui overlay, marketplace, mobile, Mac.

---

## 2. Stack — decision

**C++20 injected DLL + C# WPF front end, talking over a named pipe.**

| Piece | Choice | Why |
|---|---|---|
| Injected core | C++20, x64, `/MT` (static CRT), no external deps | Needs SEH (`__try/__except`) around every risky pointer read — a bad scan must not kill the client. NativeAOT C# cannot catch AVs; this is the deciding factor |
| GUI | C# WPF + AvalonEdit | Already installed, fast startup, good text editor with Luau syntax highlighting via `.xshd` |
| IPC | Named pipe `\\.\pipe\<name>-<pid>`, newline-delimited JSON | Async, no ports, survives GUI restart, trivially debuggable with a text tool |
| Compiler | MSVC Build Tools (VS 2022, C++ workload) | Standard, best Win32 headers. Fallback if no admin: **Zig** (`zig c++ -target x86_64-windows-gnu -shared`), zero-admin single zip |

Rejected: all-C# NativeAOT (no SEH — see above) · Electron (slow, heavy) · Python+ctypes
(pointer/AOB work is miserable) · own Luau VM with full API bindings (month+ of binding work
for v1; kept as a possible v2 for true sandboxing) · in-game ImGui overlay (adds a D3D11 Present
hook — extra Hyperion surface; revisit later).

---

## 3. Architecture

```
     ┌───────────────┐   named pipe, JSON lines    ┌──────────────────────────────┐
     │  WPF GUI      │◄───────────────────────────►│  Injected DLL (C++)          │
     │  editor/tabs  │  exec · output · status      │  scanner · hooks · Win32     │
     └───────────────┘                              └───────────────┬──────────────┘
                                                                    │ 1 call in, then Luau drives
                                                                    ▼
                                                     ┌──────────────────────────────┐
                                                     │ Roblox client · Luau VM      │
                                                     │  runner chunk (trusted)      │
                                                     │   ├─ command queue drain     │
                                                     │   ├─ per-script env          │
                                                     │   ├─ print/warn capture      │
                                                     │   └─ exec APIs in Luau       │
                                                     └──────────────────────────────┘
```

**Runner architecture (the key design decision).** Native code calls *into* Luau exactly once:
`task.spawn(runnerChunk)`. After that, Luau owns the loop — a `RunService.Heartbeat` connection
polls a native command queue, and each script is executed with `task.spawn` + `xpcall` from
inside the game's own scheduler. Payoff:

- No yields ever cross a native frame (the classic source of executor crashes/hangs).
- Executor APIs that are naturally Luau (`getgenv`, `loadstring`, `hookfunction`, `getconnections`,
  `task`, `checkcaller`) are implemented **in Luau**, where they are 10× cheaper to write.
- The native surface shrinks to ~10 C API functions + queue memory + Win32 shims.

**Native surface (all AOB-scanned, no link-time dependency):**

`luau_load` · `luau_compile` · `lua_pcall`/`lua_resume` · `lua_pushcclosure` · `lua_getfield` ·
`lua_setfield` · `lua_createtable` · `lua_tolstring` · `lua_State*` acquisition · identity field.

**State acquisition (two independent paths, both scanned):**
1. `TaskScheduler*` → job vector → job named `ScriptContext` → `lua_State*` field (no code patching).
2. Fallback: hook `ScriptContext::Resume`, capture the state on the next core-script resume.
`game` itself is then read *through Luau* (`lua_getglobal(state, "game")`), so we never need
C++ reflection or DataModel offsets — one less brittle layer.

---

## 4. Injection pipeline

**Modes** (GUI has one button per mode, all attach to the same pipe):

1. **Launch + early inject (primary).** `CreateProcess(RobloxPlayerBeta.exe, -app, CREATE_SUSPENDED)`
   → write DLL path → `CreateRemoteThread(LoadLibraryW)` or `QueueUserAPC` on the main thread
   before the runtime is locked down → `ResumeThread`. Earliest possible window; the usual way
   executors survive anti-tamper init.
2. **Attach (fallback).** `OpenProcess` → manual-map the DLL (resolve imports/relocs in-process,
   wipe PE header) → run entry via a thread hijack (`SuspendThread` + `SetThreadContext`) or
   `NtCreateThreadEx`. Used when the client is already in a game.
3. **Late/repair inject.** Same as 2, retried on failure; GUI shows the concrete error, not a spinner.

**GDK caveat (measured):** Store builds activate via `gamelaunchhelper.exe` / app model, so
`suspended launch` is not guaranteed to give a valid session. For GDK the attach path is primary
and we grab the process within ~200 ms of `RunService` existing. This is another reason the web
client is the better primary target.

**Readiness detection (per injection):** poll until `game.GameId ~= 0`, `Players.LocalPlayer`
exists, and a probe chunk (`return game:GetService("Players").LocalPlayer.Name`) round-trips.
Timeout 120 s with a visible stage in the GUI; never a silent hang.

**Anti-tamper (Hyperion/Byfron) — assume present.** It is the #1 maintenance risk: it validates
pages and blocks some classical injection. Mitigations, in order: early inject; manual mapping
with no readable PE header; never patch remote code unless required; keep every hook optional at
runtime so a failed hook degrades a feature instead of crashing; log the exact stage on failure
so a Roblox update is diagnosable in one run. Also: test on a throwaway account (tamper
detection has historically been enforced with bans, not just crashes).

---

## 5. Execution semantics

- **One state, per-script envs.** Run everything in the client's main Luau state (duplicate
  `Instance` userdata would otherwise need re-binding). Each script gets
  `env = setmetatable({}, {__index = gameEnv})`, so globals don't leak between scripts.
  `getgenv()` returns one shared table across scripts (executor convention).
- **Identity 8** on the executing thread (`setthreadidentity` / field write) so `game:HttpGet`,
  `Instance.new`, `InsertService` etc. behave like a privileged local script.
- **`loadstring`**: `luau_compile` → `luau_load(chunkname="@Executor: N")` → set env on the closure.
  Compile errors come back with line info and are rendered as a console entry + editor gutter mark.
  If the player build ever ships without the compiler (it should not — the client compiles its own
  LocalScripts), fall back to linking Luau's compiler into the DLL; nothing from the client is
  needed for that, so it is a compile-time switch, not a redesign.
- **Errors**: `xpcall(f, debug.traceback)`; output rows carry `kind` (print/warn/error/info), the
  script id, and a timestamp; GUI colors them and links the line.
- **Cancel**: per-script thread handle (`task.cancel`) so "Stop" actually stops a runaway script.
- **Yields** work for free because scripts are `task.spawn`ed by the runner (including `wait`,
  `task.wait`, event waits, `HttpGet` internals).
- **`require`** works inside user scripts because it is the game's own VM.

---

## 6. API surface

**Tier 1 (v1 blocker — covers the vast majority of hub scripts):**
`loadstring`/`load`, `print`/`warn` capture, `getgenv`/`shared`, `HttpGet`/`HttpPost`,
`identifyexecutor`, `getthreadidentity`/`setthreadidentity`, `task.*`, `hookfunction`,
`hookmetamethod`, `getrawmetatable`, `setreadonly`, `checkcaller`, `newcclosure`, `islclosure`,
`clonefunction`, `getinfo`, `getupvalue`/`setupvalue`, `getconnections`, `firesignal`,
`firetouchinterest`, `setclipboard`, `writefile`/`readfile`/`appendfile`/`isfile`/`folder` ops,
`listfiles`, `queue_on_teleport`, `setfpscap`, `mousemoverel`, `keypress`, `isrbxactive`.

**Tier 2:** `getscriptclosure`, `getconstants`/`setconstants`, `getgc`, `getinstances`,
`getnilinstances`, `getcustomasset`, `getscriptbytecode`, `decompile` (returns
`-- decompiler not implemented` until built), `setfflag`, `gethui`.

**Implementation notes that matter**
- `hookfunction(f, g)`: replace the closure's prototype pointer in place, copy upvalues, keep the
  original for chaining. Needs Luau `Closure` layout → use **pinned Luau headers** (see §7).
- `getcustomasset(path)`: the client's `content` dir is inside `WindowsApps` (read-only), so
  instead of copying files there, serve the workspace over `http://127.0.0.1:<port>/<hash>`.
  Rojo already proves the client's content pipeline accepts localhost URLs for images/sounds.
  Fallback if a property rejects it: `rbxasset://` against a writable content dir.
- HTTP: native WinHTTP (headers, redirects, gzip, timeout) rather than `HttpService`, because
  `HttpService` respects game-level restrictions and rate limits.
- Filesystem is sandboxed to `%LOCALAPPDATA%\<Brand>\workspace`; `..` cannot escape.

---

## 7. Internals & offsets

- **Everything is scanned at runtime** in the loaded module(s); nothing depends on a shipped
  offset table. Ship *secondary* patterns per target and pick the first that hits exactly once.
- **Pinned Luau headers** (no linking) matched to the client's Luau line, used only for
  `offsetof`/struct layout in `hookfunction`, `getrawmetatable`, `getconnections`.
- **Validation probes** run at bootstrap: for each discovered function/offset, a cheap probe
  asserts it behaves (`push/pop` balance, `type()` round-trip, identity check via
  `game:HttpGet`). Failures are logged with the pattern name — this is what makes a Roblox
  update a 10-minute fix instead of a rewrite.
- **Data-driven**: `patterns.json` in the DLL resources; last-good results cached to
  `%LOCALAPPDATA%\<Brand>\scan-cache.json` keyed by client version, validated before reuse.
- **Module set**: scan `RobloxPlayerBeta.exe` *and* `RobloxPlayerBeta.dll` (measured: engine is
  split in the GDK build); pick hits by module + uniqueness, not by assumption.

---

## 8. GUI (WPF)

Editor (AvalonEdit, Luau highlighting, Ctrl+Enter = run, Tab indent, bracket matching) in tabs
with renamable scripts · console pane (filter, timestamps, colored kinds) · script hub tab (a JSON
index from a URL, cache + favorites) · status bar (client version, injected?, script count) ·
settings (auto-exec script, font size, always-on-top, theme) · hotkey to show/hide (default `Insert`) ·
tray icon · multi-instance dropdown when >1 client is attached.

Autocomplete: the bootstrap pushes a list built from Luau globals + `game`'s services + class names,
so the editor completes real API, not a hardcoded list.

---

## 9. Lifecycle & edge cases

- **Teleport/rejoin:** `game.Loaded` + `LocalPlayer.CharacterAdded` → re-run the runner, re-apply
  auto-exec, keep scripts that asked to persist.
- **Client close:** pipe EOF → GUI shows "not injected", keeps editor state on disk.
- **Crash safety:** SEH around every pointer read; scan failures degrade to "core unavailable"
  rather than an exception in the client thread.
- **Multi-instance:** one pipe per PID; GUI can target either.
- **Self-check:** a "Diagnostics" panel runs the probe suite on demand and prints pattern hits,
  so a user bug report contains everything needed to fix an update break.

---

## 10. Milestones (each has a proof, not a vibe)

| # | Deliverable | Proof of done | Est. |
|---|---|---|---|
| M0 | Toolchain + binary recon: PE dumper (Python, from process memory — disk is ACL-blocked), pattern harness, `patterns.json` seed | every signature hits exactly once in the running client | 1–2 d |
| M1 | Injector + trivial DLL that logs via pipe | GUI shows "injected" against a live client, both modes; client stable 10 min | 3–5 d ⚠ |
| M2 | `lua_State*` + one native→Luau call (`print("hi")` → pipe) | line appears in GUI console | 3–4 d |
| M3 | Runner + env + `loadstring` + output capture + errors with lines | script creates a spinning part, `print`s, `error`s; all three visible | 4–6 d |
| M4 | Tier-1 API + files + HTTP + hooks + compat suite | 30-assertion self-test script passes in-client; a real hub script runs | 8–12 d |
| M5 | GUI polish: tabs, highlighting, autocomplete, hub, settings, hotkey | usable without reading docs | 6–8 d |
| M6 | Hardening: teleport, multi-instance, update check, diagnostics panel, branded installer | survives a Roblox version bump with only `patterns.json` edits | 4–6 d |

Realistic: **~5–7 weeks** solo at a sustainable pace; the schedule risk is entirely in M1/M2
(injection and state acquisition), which is why they come first and prove out before any UI work.

---

## 11. Test strategy

- **Footprint audit (built, runs now):** `tools/footprint_audit.py` enumerates modules, threads,
  memory regions, handles, windows and named pipes of the live client in ~0.6 s. `--save` records a
  version-keyed baseline (`baselines/clean-<version>.json`); after injection `--diff baseline` lists
  exactly what we added. This is the undetectability gate.
- **Offline (runs on this machine, no Roblox needed):** Python PE dump from process memory, pattern
  uniqueness check, PE/import sanity, Luau-version string extraction. Gate for every release.
- **In-client smoke:** `tests/smoke.luau` — Instance creation, signal connect, yield, HTTP, file IO,
  clipboard, hook a builtin, restore it, assert `checkcaller`.
- **In-client compat:** `tests/compat.luau` — one assertion per Tier-1 API, prints machine-readable
  PASS/FAIL that the GUI can display as a table.
- **Regression on version bump:** run diagnostics panel, diff pattern hits against last-good cache.
- **Crash loop guard:** if a script crashes the client, record "last executed script" so the GUI can
  disable it on next start.

---

## 12. Risks

| Risk | Hit | Impact | Mitigation |
|---|---|---|---|
| Hyperion blocks injection | High | Fatal (project-defining) | Early inject first; manual map; probe both in M1 before anything else |
| Roblox update breaks patterns | Certain (weekly) | Days of downtime | Runtime scanning, secondary patterns, diagnostics, cache diffing |
| `lua_State` acquisition changes shape | Medium | Large | Two independent paths (job scan, Resume hook) + probes |
| Store/GDK build behaves differently | High | Medium | Primary target = web install; GDK attach as secondary |
| Yield/thread model mistakes → hangs | Medium | Medium | Runner architecture; scripts always `task.spawn`ed |
| `hookfunction` corrupts closures | Medium | Client crash | Pinned headers, upvalue copy, probe after hook, revert on failure |
| Account bans from tamper detection | Medium | User-visible | Throwaway test account; conservative hooks; document it |
| Scope creep (decompiler, drawing, hub) | Certain | Weeks | §1 scope fence; `decompile` is an explicit stub in v1 |

---

## 13. What I'd cut from v1 (and why)

Decompiler (needs a bytecode→source pass; no script in the top-100 needs it) · `draw` API
(needs an overlay hook; ScriptGui covers it) · Studio support (different init path, low value) ·
auto-update backend (a GitHub Releases JSON is enough) · marketplace hub (a static JSON index).
Each is a week saved and can be added without redesign.

---

## 14. Open decisions

1. **Primary client target:** install the web build and develop against it? *(recommend: yes)*
2. **Toolchain:** MSVC Build Tools (~4 GB, admin) or Zig (zero admin, ~80 MB)? *(recommend: MSVC if you have admin, else Zig)*
3. **Brand/name:** cosmetic but needed for paths, pipe names, window title. *(suggest "Sable" or "Vector")*
4. **v1 API depth:** Tier 1 only, or Tier 1 + Tier 2? *(recommend Tier 1, then add)*
5. **Distribution:** local build only, or GitHub Releases + auto-update from day one? *(recommend: releases at M4)*
