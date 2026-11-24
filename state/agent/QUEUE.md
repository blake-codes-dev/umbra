# QUEUE — the job. A turn is a shift.

Take the top unchecked item. Finish it or record the blocker. Tick it. Next.
Do not end a turn just because a sub-task finished; end interim messages with
<!-- deepai:work-remains --> on its own line.

## M0 — patterns (in progress)
- [x] dump exe (.text 100.2 MB) and dll (.byfron 14.7 MB) from process memory
- [x] string xrefs: 2 unique sigs (luaG_index_error, luaG_runerror)
- [x] anchors: 5 unique sigs (near-descriptor matching); 4 unresolved
- [x] re-dump x17 and merge: .text zeros 75% -> 55%, 20,756 pages recovered (dump/merged-all.bin)
- [x] 10 unique sigs, each verified to hit exactly once on an independent snapshot
- [x] luau_execute located (3 dispatch sites: 0x797395, 0x410b0cd, 0x58bc29d)
- [ ] luaG_arith sig misses on sparse snapshots: re-derive from a page always present

## M1 — injector
- [x] stub DLL built (3,584 B) + manual map validated on a throwaway process (marker file)
- [x] host: manualmap.py (relocations, imports, entry via hijack)
- [x] stub->host channel: file bus (pagefile sections went copy-on-write)
- [ ] gate: footprint_audit --diff clean (needs a proper baseline for the same state)
- [x] live injection survives: 30 s+, no new module, no new thread
- [ ] sigfix 8 broken sigs against the live module, then sigcheck again
- [x] attach mode (M1-RESULT.md: 0 modules, 0 threads, +2 RX pages, client survives)
- [ ] launch-suspended mode
- [ ] unmap stale images on re-injection (8 KB leak per run)

## M2 — lua_State
- [ ] anchor chain to lua_State, one native->Luau print, survive rejoin

## M3 — runner
- [ ] task.spawn queue on Heartbeat, loadstring, output capture, 30-assert self-test

## Product
- [ ] Discord (needs bot token), README, changelog, screenshots, beta invites

## Ops
- [~] local_brain: tools/brain.py client done (swap qwen<->inkling, idle stop pending); action sandbox pending
- [x] Inkling load test: llama.cpp b12600, 8.8 tok/s, 391 correct, 60-140 s load
- [ ] harden auto-run when elevated
