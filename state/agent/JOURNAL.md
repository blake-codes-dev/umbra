# JOURNAL (append-only, one line per turn)

2026-11-23 21:10 · built survival kit: brief.py, RULES.md, hung-app detection, atomic health, query-driven night plan · next M0
2026-11-23 22:08 · M0 dumped exe(.text 100.2MB)+dll(.byfron 14.7MB); guard.py+harden.py written; BLOCKED: blake not in Administrators (Users+docker-users only), autologon needs MS password not PIN
2026-11-23 22:26 · M0 seed: patterns.py resolves string xrefs; 2 unique sigs (luaG_index_error 0x2585900, luaG_runerror 0x77dfb0); rest unresolved -> dump pages likely encrypted; vault snapshot+backup ok (3973 files, drift 0)
2026-11-24 08:31 · POWER LOSS 23:11 (event 6008/Kernel-Power 41); machine off ~9h, boot 08:21; merge 6 dumps: 518 pages recovered, image still 62% zeros; toolchain check next
2026-11-24 09:05 · M1 manual map VALIDATED on throwaway process: reloc(0) imports(4) entry via hijack, DllMain wrote marker; pipe handshake from stub still unverified; patterns now 5 unique; ASSUMPTIONS.md written
2026-11-24 09:12 · M1 gate met via marker file (manual map + hijack entry in throwaway proc); shm name matches but stub write unverified; importing 8 kernel32 fns
2026-11-24 09:35 · both brains verified: qwen3 20s incl. load, inkling (llama.cpp b12600) 8.8 tok/s, 17*23=391; brain.py swaps them because 8.1+5.2 GB > 12 GB VRAM; capstone installed, disasm.py decodes .text and finds 40 callers of luaG_runerror
2026-11-24 09:56 · M0 gate met: 10 unique sigs verified on an independent snapshot; luau_execute found (3 dispatch copies, jmp [rbx+rdi*8]); learned rdata strings are tail-shared so exact-match misses, and dump_merge is now streaming (RAM 200MB not 3GB)
2026-11-24 10:56 · LIVE injection into the client works and survives: register-preserving stub + suspend-count drain + thread quality order. Found: sleeping threads never take a planted RIP; .text needs protect-lift to read; merged-dump sigs have zero holes. Wrote sigcheck.py + sigfix.py to verify/repair signatures live
2026-11-24 11:06 · M1 measured: 0 modules, 0 threads, 0 windows, +2 RX pages (8 KB), page protections verified, 5 surviving injections across 3 pids. sigfix repaired all 11 signatures to live-unique. Waiting on Roblox sign-in for Lua work
