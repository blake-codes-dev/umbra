# M1 gate result — measured 2026-11-24 11:0x on the live client (pid 12716)

| check | result |
|---|---|
| module list entry | none (150 modules before and after) |
| new thread | none (86-121 threads, unchanged) |
| new window | none (only the app window) |
| private executable regions added | 2 pages, 8 KB (.text page + entry-thunk page) |
| orphan image regions | 0 |
| protection correctness | image .text PAGE_EXECUTE_READ, thunk page PAGE_EXECUTE_READ, rest RW |
| client survival | 30 s+ per run, 5 successful runs across 3 client pids |
| reply channel | file bus `state/inject/bus.bin` -> "attached" |
| relocations applied | 1 (the `digits` pointer in the stub) |
| imports resolved | 10/10 kernel32 |

## what it took
1. Register-preserving thunk (pushfq + rax rcx rdx r8-r12, restore, popfq): the first
   live attempt without it ran the stub and the client died within 3 s.
2. Suspend-count drain: one ResumeThread leaves a multiply-suspended thread parked.
3. Thread selection by RIP: threads executing user code run the thunk immediately,
   threads parked in Sleep do not take a planted RIP at all.
4. Page-aligned VirtualProtectEx + read-back: an unaligned 61-byte protect left the
   page PAGE_READONLY (0x2) though the code still ran.
5. File bus instead of a pagefile section: those views were copy-on-write.

## still open before this is a product
- stale images are not unmapped on re-injection (each run leaks 8 KB in the process)
- no lua_State yet (M2); nothing runs inside a game
- `.text` reads need a protection lift, which is itself a visible operation
