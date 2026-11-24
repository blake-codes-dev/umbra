# ASSUMPTIONS — what the build rests on, and how each is proved

Status: **measured** = observed on this machine with a tool in `tools/`.
**observed once** = seen in one run, not reproduced. **assumed** = not yet tested.
Rule: nothing becomes a design decision while it is *assumed*.

## Client binary
| Claim | Status | Evidence | Verify with |
|---|---|---|---|
| Build is GDK `2.748.1526.0`, exe `RobloxPlayerBeta.exe` + engine `RobloxPlayerBeta.dll` | measured | version key, module list | `footprint_audit.py --json` |
| `.text` is 100.2 MB, `.byfron` 14.7 MB RX | measured | PE dump | `pe_dump.py`, `dump/modules.json` |
| Image has ~1,727 small no-access ranges inside `.text` | measured | read stats | `pe_dump.py` output |
| ~67–90 protection sweeps per 40 s, median gap ~0.55–1.1 s, only `0x1`↔`0x20` | measured | observer runs | `observer.py --seconds 40` |
| No dedicated scanner thread; all threads live inside modules | measured | footprint + observer | `footprint_audit.py` |
| Decoy pages are not read by the client | measured | `observer.py --decoy` | rerun after any update |
| Baseline: 162–182 modules, 19 private RX regions (~604 KB), 0 orphan images | measured | baseline JSON | `footprint_audit.py --diff` |

## Injection
| Claim | Status | Evidence | Verify with |
|---|---|---|---|
| `CreateRemoteThread` returns exit code `0xC000071C` for *any* payload (even `mov eax,42; ret`) | measured | 3 runs today | rerun with a second payload |
| `LoadLibraryW` from a remote thread does **not** put the DLL in the module list | measured | module list after inject | `hijack.py --force`, then check modules |
| Thread hijack executes a 40-byte stub and resumes the thread | observed once | client stayed responsive | `hijack.py --force` on the alt |
| Pipe `\.\pipe\umbra_stub` works in-process | measured | in-process load printed `stub: attached` | rerun |
| Pipe works across processes (stub in client) | **unknown** | no message yet | first alt test |
| Client survives injection attempts | measured | one crash, then restart | count crashes in `state/supervisor/events.jsonl` |

## Lua / engine
| Claim | Status | Evidence | Verify with |
|---|---|---|---|
| Engine uses Luau 5.1 semantics with `task.spawn` | assumed | Roblox docs, not measured here | run `task.spawn(print, "x")` in-client |
| `lua_State*` reachable from a `luaD_throw`/`luaG_runerror` xref | assumed | two anchors resolved (`0x2552500`, `0x77dfb0`) | M2 test |
| A native→Luau call needs only one entry per frame (runner poll) | assumed | design | M3 test |
| `loadstring` exists in the executor environment | assumed | standard executor API | M3 test |

## Environment
| Claim | Status | Evidence | Verify with |
|---|---|---|---|
| Account is a Microsoft account; auto-logon needs its password | measured | `Get-LocalUser`, `LogonUser` 1326 | `harden.py` |
| `blake` is not in Administrators | measured | `net user blake` | `whoami /groups` after elevation |
| Power loss leaves the machine off (no AC restore) | measured | shutdown 23:11, boot 08:21 | BIOS setting |
| `cl.exe` 14.43 + Windows SDK 10.0.26100 build a 3,584-byte DLL | measured | `src/native/build.bat` | rebuild |

Open questions to close before M2:
1. Why does every remote thread exit with `0xC000071C`? (Hyperion wrapper or a real error?)
2. Does the stub connect to the pipe from inside the client?
3. Does the client tolerate a manual-mapped image with no loader entry?

## Verified 2026-11-24 (client-free)
| Claim | Status | Evidence |
|---|---|---|
| Inkling 2 Mini runs on this box | measured | llama.cpp b12600 CUDA, 8.78 tok/s decode, 20 tok/s prefill, correct 17*23=391 |
| ollama cannot load Inkling | measured | `unknown model architecture: 'inkling2_loop'` at 0.40.16 |
| qwen3:8b and Inkling cannot co-reside in 12 GB | measured | 5.2 + 8.1 GB weights + 1.3 GB desktop |
| Manual map entry runs | measured | DllMain wrote `state/inject/attach.marker` in a spawned python.exe |
| luaG_runerror has 40 direct callers | measured | E8 scan over .text, tools/disasm.py |

## Corrected / measured 2026-11-24 (live client, app shell pid 20832)
| Claim | Status | Evidence |
|---|---|---|
| Hijacking a thread corrupts the client | measured | first live attempt: stub ran, client died <3 s |
| Saving volatile registers in the stub fixes it | measured | 30 s+ alive after the same run, 121 threads stable |
| A thread parked in Sleep does not take a planted RIP | measured | python `time.sleep` target: RIP stayed at stub[0] for 4 threads; a spinning target ran on the first |
| Hyperion denies PROCESS_ALL_ACCESS on the client | measured | OpenProcess 0xC0000022 (err 5) with 0x1F0FFF from an unprivileged handle; full access granted later to the same pid, so it varies by state |
| `.text` pages need a protection lift to read | measured | plain ReadProcessMemory returns partial reads; pe_dump._read_region succeeds |
| 3 of 11 static signatures resolve in the live module | measured | sigcheck: exact RVA hits for luaG_arith, luaG_index_error, luaG_index_error_msg |
| Dump-merged signatures can carry zero holes | measured | 8 signatures missed live although static-unique |
