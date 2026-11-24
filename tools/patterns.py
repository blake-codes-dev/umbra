#!/usr/bin/env python3
"""patterns.py - seed AOB signatures from the dumped engine image (M0).

No disassembler needed. Lua error strings live in .rdata; the code that raises
them references those addresses with RIP-relative LEA. Find the references, walk
back to the function prologue, and emit a byte signature that is checked for
uniqueness against the whole .text.

    python tools/patterns.py
    python tools/patterns.py --module RobloxPlayerBeta.exe --min-hits 1
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DUMP = ROOT / "dump"
OUT = ROOT / "patterns.json"

STRINGS = {
    # real literals from .rdata (Luau builds some messages from "%s" templates)
    "luaG_typeerror_index": b"attempt to index %s with %s",
    "luaG_typeerror_call": b"attempt to call missing method '%s' of %s",
    "luaG_arith": b"attempt to perform arithmetic (%s) on %s and %s",
    "luaG_concat": b"attempt to concatenate %s with %s",
    "luaG_compare": b"attempt to compare %s %s %s",
    "luaG_index_error": b"cannot index %s with a %s",
    "luaG_runerror": b"stack overflow",
    "luaG_runerror_handler": b"error in error handling",
    "luaG_nil_index": b"table index is nil",
    "luaL_argerror": b"bad argument #%d",
}
LEA = tuple(bytes([rex, 0x8d, modrm]) for rex in (0x48, 0x4C)
            for modrm in (0x05, 0x0d, 0x15, 0x1d, 0x25, 0x2d, 0x35, 0x3d))


def parse(image: bytes) -> dict:
    e = int.from_bytes(image[0x3C:0x40], "little")
    coff = e + 4
    count = int.from_bytes(image[coff + 2:coff + 4], "little")
    opt_size = int.from_bytes(image[coff + 16:coff + 18], "little")
    table = coff + 20 + opt_size
    sections = {}
    for i in range(count):
        raw = table + i * 40
        name = image[raw:raw + 8].rstrip(b"\0").decode("ascii", "replace")
        vsize = int.from_bytes(image[raw + 8:raw + 12], "little")
        vaddr = int.from_bytes(image[raw + 12:raw + 16], "little")
        chars = int.from_bytes(image[raw + 36:raw + 40], "little")
        sections[name] = {"va": vaddr, "size": vsize, "rx": bool(chars & 0x20000000)}
    return sections


def lea_targets(text: bytes, text_va: int, wanted: set[int]) -> dict[int, int]:
    exact: dict[int, int] = {}
    near: dict[int, int] = {}
    for prefix in LEA:
        offset = 0
        while True:
            idx = text.find(prefix, offset)
            if idx == -1 or idx + 7 > len(text):
                break
            disp = int.from_bytes(text[idx + 3:idx + 7], "little", signed=True)
            target = text_va + idx + 7 + disp
            if target in wanted:
                exact.setdefault(target, idx)
            else:
                for want in wanted:
                    if 0 < want - target <= 64:
                        near.setdefault(want, idx)
                        break
            offset = idx + 1
    near.update(exact)
    return near

def prologue(text: bytes, offset: int, back: int = 0x2000) -> int:
    lo = max(0, offset - back)
    window = text[lo:offset]
    marker = window.rfind(b"\xcc\xcc\xcc\xcc")
    if marker != -1:
        return lo + marker + 4
    marker = window.rfind(b"\xc3")
    if marker != -1:
        return lo + marker + 1
    return offset


def clean(text: bytes, offset: int) -> int:
    while offset < len(text) and text[offset] in (0xCC, 0x90):
        offset += 1
    return offset


def signature(text: bytes, offset: int, length: int = 32) -> str:
    raw = text[offset:offset + length]
    return " ".join(f"{b:02X}" for b in raw)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    argv = argv if argv is not None else sys.argv[1:]
    module = "RobloxPlayerBeta.exe"
    if "--module" in argv:
        module = argv[argv.index("--module") + 1]
    candidates = sorted(DUMP.glob(f"{module}-*.bin"))
    if not candidates:
        print("no dump; run tools/pe_dump.py first")
        return 1
    image = candidates[0].read_bytes()
    sections = parse(image)
    text = sections.get(".text")
    data = sections.get(".rdata") or sections.get(".rodata")
    if not text or not data:
        print("missing sections:", list(sections))
        return 1
    t_off, t_va, t_size = text["va"], text["va"], text["size"]
    body = image[t_off:t_off + t_size]
    r_off, r_size = data["va"], data["size"]
    rodata = image[r_off:r_off + r_size]

    found: dict[str, int] = {}
    occurrences: dict[str, list[int]] = {}
    for name, needle in STRINGS.items():
        positions = []
        start = 0
        while len(positions) < 4:
            hit = rodata.find(needle, start)
            if hit == -1:
                break
            positions.append(r_off + hit)
            start = hit + 1
        if positions:
            found[name] = positions[0]
            occurrences[name] = positions

    wanted = {va for group in occurrences.values() for va in group}
    started = time.monotonic()
    refs = lea_targets(body, t_va, wanted)
    elapsed = round(time.monotonic() - started, 2)

    patterns = []
    for name, string_va in found.items():
        index = None
        for candidate in occurrences.get(name, [string_va]):
            if candidate in refs:
                index = refs[candidate]
                string_va = candidate
                break
        if index is None:
            patterns.append({"name": name, "string_va": hex(string_va), "func_va": None,
                             "hits": 0, "note": "no rip-relative reference"})
            continue
        start = clean(body, prologue(body, index))
        sig = signature(body, start)
        hits = body.count(bytes.fromhex(sig.replace(" ", "")))
        patterns.append({"name": name, "string_va": hex(string_va),
                         "func_va": hex(t_va + start), "xor_va": hex(t_va + index),
                         "sig": sig, "hits": hits})

    payload = {"module": module, "image": str(candidates[0]), "text_va": hex(t_va),
               "text_size": t_size, "scan_seconds": elapsed,
               "generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "patterns": patterns}
    OUT.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    unique = sum(1 for p in patterns if p.get("hits") == 1)
    print(f"{module}: {len(patterns)} strings, {len(patterns) - unique} unresolved, "
          f"{unique} unique signatures, {elapsed}s scan")
    for row in patterns:
        hits = row.get("hits", 0)
        print(f"  {row['name']:<22} {str(row.get('func_va')):>12}  hits={hits:<3} "
              f"{row.get('note', '')}")
    print(f"-> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
