# Umbra

A quiet Roblox executor and mod menu for Windows x64.

- **Executor** — attach or launch, inject without adding a module or a thread, run Luau from a
  desktop editor, capture output and errors.
- **Mod Menu** — a polished in-game panel (tabs, toggles, sliders, keybinds, saved configs)
  written in Luau and shipped as a standalone script/hub.

No key system. No ads. No watermark.

## Status

Pre-alpha, developed against the Microsoft Store / GDK client `2.748.1526.0`.
The injector is verified (manual map, no module list entry, no new thread, client stable).
Luau state discovery and the script runner are in progress — see [PLAN.md](PLAN.md) and
[WORKLOG.md](WORKLOG.md).

## Layout

| Path | What |
|---|---|
| `src/` | native stub (C), Luau packages, future WPF app |
| `tools/` | pattern scanner, PE dumper, injector, observer, state finder, Discord builder |
| `patterns.json` | live-verified signatures for the current client build |
| `PLAN.md` | engineering plan and milestones |
| `PRODUCT.md` | pricing, Discord, marketing, support |
| `WORKLOG.md` | append-only progress log |
| `state/agent/` | machine-readable state + brief used to survive context loss |

## Build

Native stub (MSVC x64):

```bat
src\native\build.bat
```

It produces `src/native/stub.dll`, a position-independent manual-map payload.

## Verify the injector

```bash
python tools/footprint_audit.py --json     # baseline: modules, threads, RX pages
python tools/hijack.py --dll src/native/stub.dll
python tools/footprint_audit.py --diff
```

## Mod menu

`src/luau/modmenu.lua` is a self-contained ScreenGui panel. Run it from the executor console.
It needs `game:GetService("CoreGui")` (or falls back to `PlayerGui`).

## License

MIT for the source. Builds distributed through the Discord are licensed to the buyer.
