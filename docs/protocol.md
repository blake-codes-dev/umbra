# Bus and pipe protocol

Two channels, both boring on purpose. Nothing here needs a thread inside the client.

```
  GUI (WPF)                injected core (C++)            runner (Luau, in-client)
      │  named pipe              │                              │
      │  newline JSON ──────────►│  scan once, then            │
      │                          │  write command slots ───────►│ Heartbeat drains
      │  ◄──────── output slots ◄│◄──────────── output slots ───┘
```

The core never calls into Luau more than once (`task.spawn(runner)`); after that it only
moves bytes. That is what keeps yields out of native frames.

## Pipe (GUI ↔ core)

`\\.\pipe\lyphen-<pid>`, one JSON object per line, both directions.

| Direction | Object | Meaning |
|---|---|---|
| GUI → core | `{"t":"hello"}` | attach probe, core answers with footprint |
| GUI → core | `{"t":"exec","id":7,"src":"..."}` | queue a script |
| GUI → core | `{"t":"cancel","id":7}` | cancel that script's thread |
| core → GUI | `{"t":"ready","pid":1234,"build":"2.748.1526.0"}` | runner is live |
| core → GUI | `{"t":"out","id":7,"kind":"print","text":"hi"}` | captured output |
| core → GUI | `{"t":"done","id":7,"ok":true}` | script finished |
| core → GUI | `{"t":"err","stage":"scan","text":"..."}` | a stage failed, with the stage name |

Unknown `t` is ignored by both sides, so a newer GUI can talk to an older core.

## Bus (core ↔ runner)

One 64 KiB section, file-backed so it survives a re-inject while the client runs:
`%LOCALAPPDATA%\Lyphen\bus-<pid>.bin`. The core creates it, the runner opens it through a
bridged `BUS` table (native `MapViewOfFile` result, exposed as `push`/`take`/`stop`).

Header, all u32 little endian:

| Offset | Field | Writer |
|---|---|---|
| 0x00 | magic `UMBR` | core, once |
| 0x04 | version (1) | core, once |
| 0x08 | command head | core |
| 0x0C | command tail | runner |
| 0x10 | output head | runner |
| 0x14 | output tail | core |
| 0x18 | flags (bit 0 = stopped) | both |

Command slots: 60 × 1 KiB at 0x100 → `{u32 op, u32 id, u32 len, char payload[len]}`.
Output slots: 100 × 512 B at 0x10000 → `{u32 kind, u32 id, u32 len, char text[len]}`.

`head` advances on write, `tail` on read, both modulo the slot count. One writer per field, so
no locks and no atomics beyond the natural 4-byte store. A full ring drops the newest command
and reports `err`, it never blocks the client's scheduler.

Ops: `1 exec`, `2 cancel`, `3 probe`, `4 shutdown`.
Output kinds: `1 print`, `2 warn`, `3 error`, `4 info`, `5 done`.

## Bridging into Luau

`api.luau` and `runner.luau` ship next to the app and are read from `%LOCALAPPDATA%\Lyphen`
at inject time, so a Luau change needs no rebuild and the C side stays small. The native
closures are registered in one table and rebound by the runner before any script env exists:
file (workspace-sandboxed), WinHTTP, SendInput/clipboard, foreground check, plus the three
bus closures the runner drains with. Every bridge returns its failure as a value.

Function addresses are resolved as `module base + rva` from `tools/gen_patterns.py` output
(`src/native/patterns.h`), with the recorded prologue as a scan fallback for anything not yet
measured, so an unmeasured function still resolves if the prologue matches.

## Finding the Luau entry points

The shipped `.text` is packed (Byfron), so nothing can be read off the file: the byte histogram
is almost uniform and no prologue signature exists until the client unpacks itself. Strings in
`.rdata` are not packed, so `resolve()` self-calibrates in memory instead:

1. find the literal name (`loadstring`, `pcall`), exact and NUL-terminated
2. find the code that references it, either `lea r64,[rip+d]` or `mov r64,[rip+d]` of a slot
3. take the call targets in that function; a real one is recognised by storing `L->top`
   (`mov [reg+0x80], reg` with a disp32) and returning soon after
4. `luau_load` is the last such callee in `luaB_loadstring` (the first is the compiler),
   `lua_pcall` is the one in `luaB_pcall`

Note the offsets in step 3 are the *client's* `lua_State`, whose `top` sits at `0x80`. The harness
links a different Luau whose layout is unrelated (`top` at `0x8`, `base` at `0x10`), so host builds
take those from the linked build while the client keeps the measured ones. Do not mix them.

Every pick is reported as a `calibrated load=... call=...` line, so a live run converts into
fixed RVAs in `patterns.json` and the guessing stops being load-bearing. RVAs are still tried
first when present.

## Rules that matter

- **Every risky read is wrapped.** The core uses `__try/__except` around every pointer deref;
  a bad scan degrades a feature, never the client.
- **Nothing is patched** unless a feature needs it. Manual map leaves no module entry, and the
  core creates no thread inside the client (entry runs on a hijacked existing one).
- **The runner is fetched, not built in.** It is a Luau string; changing it needs no rebuild.
- **Cancel is cooperative.** `task.cancel` on the script's thread; a script in a tight C call
  finishes that call first.
- **One state, many envs.** Scripts share the client's `lua_State` (an `Instance` userdata
  cannot be rebound to a second VM) but each gets its own environment table, so globals do not
  leak between scripts while `getgenv()` still does.

## Failure is a named stage

Every failure answers with a stage, so a bug report is diagnosable in one run:
`scan`, `state`, `compile`, `resume`, `pipe`, `bus`. A Roblox update is then a pattern fix
against a named stage, not a hunt.

## Patching

How the client is hooked, and the four rules that keep it alive: see [hooks.md](hooks.md).
