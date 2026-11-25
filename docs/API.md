# API status

Honest table. `luau` means implemented in `src/luau/api.luau` and working off bridges;
`bridge` means the Luau side is done and the native closure exists in `core.c`; `todo` means
nothing yet. A bridge marked `bridge` has been run in the harness (`tests/`), not in a live client.

Nothing here is ticked until it ran in a live client.

## Tier 1

| API | Where | Status |
|---|---|---|
| `getgenv`, `getrenv`, `shared` | runner | luau, per-run env (harness-proven to persist across scripts) |
| `print` / `warn` capture | runner | luau, goes to the bus and the gui console |
| `loadstring`, `load` | runner env | luau, compiles through the bridge and applies the script env (harness-proven) |
| `identifyexecutor` | api | luau |
| `getthreadidentity` / `setthreadidentity` | api | luau (reports 8; the field write is still todo) |
| `checkcaller` | api | luau |
| `islclosure`, `clonefunction` | api | luau |
| `getupvalue`, `setupvalue` | api | luau (`debug.*`) |
| `getrawmetatable`, `setreadonly` | api | luau |
| `hookfunction` | api | luau, guides upvalues onto the target |
| `hookmetamethod` | api | luau |
| `newcclosure` | api | luau wrapper, not a real C closure yet |
| `getconnections`, `firesignal` | api | todo, needs the signal walk |
| `firetouchinterest` | api | luau best effort |
| `writefile`, `readfile`, `appendfile`, `isfile`, `isfolder`, `makefolder`, `delfile`, `listfiles` | api | bridge, sandboxed to the workspace |
| `request`, `httpget`, `HttpPost` | api | bridge, WinHTTP, returns status+body, never throws on 4xx |
| `setclipboard`, `mousemoverel`, `keypress`, `keyrelease` | api | bridge, SendInput + clipboard |
| `setfpscap` | api | luau no-op until the flag is found |
| `isrbxactive` | api | bridge, foreground window pid |
| `queue_on_teleport` | api | luau stub, stored only |
| `getcustomasset` | api | needs the localhost asset server |

## Tier 2 (not in v1)

`getscriptclosure`, `getconstants`/`setconstants`, `getgc`, `getinstances`, `getnilinstances`,
`getscriptbytecode`, `decompile` (explicit stub), `setfflag`, `gethui`.

## Rules

- A missing bridge must `error` with the bridge name, never silently return `nil`.
- Bridges push `false`+reason on failure; they never throw across the C/Luau boundary.
- Anything that parses user input is the caller's risk: `request` returns body, status and
  headers, and never throws on an HTTP error code.
- Files are sandboxed to `%LOCALAPPDATA%\Umbra\workspace`; `..` cannot escape it.
- Hooks are reversible: `hookfunction` returns the original, and a hook that throws is
  reported with the script id so it can be undone.
