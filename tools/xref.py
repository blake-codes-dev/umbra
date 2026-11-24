"""RIP-relative string reference scanner that stays under ~250 MB RSS.

The first version decoded all 100 MB of .text with Capstone detail=True and kept
a dict of every reference target: ~20M instruction objects plus a multi-million
entry dict. On a 16 GB box with the app and a model loaded that was enough to
take Windows down.

This one holds .text (100 MB) and a regex scan, nothing else. Memory is flat in
the number of references.

  python tools/xref.py "reader function must return a string" "attempt to call"
  python tools/xref.py --disasm 0x41dcff4
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DUMP = ROOT / "dump" / "merged-exe.bin"
TEXT_OFF = 0x1000
TEXT_LEN = 105_075_420
TEXT_VA = TEXT_OFF

# lea/mov r64, [rip+disp32]: REX.W + 8d/8b + modrm(mod=00, rm=101)
PAT = re.compile(rb"[\x48\x4c][\x8d\x8b][\x05\x0d\x15\x1d\x25\x2d\x35\x3d]")


def load_text() -> bytes:
    with open(DUMP, "rb") as f:
        f.seek(TEXT_OFF)
        return f.read(TEXT_LEN)


def refs_to(text: bytes, targets: set[int]) -> dict[int, list[int]]:
    hits: dict[int, list[int]] = {t: [] for t in targets}
    limit = len(text) - 7
    for m in PAT.finditer(text):
        i = m.start()
        if i > limit:
            break
        rel = int.from_bytes(text[i + 3:i + 7], "little", signed=True)
        tgt = TEXT_VA + i + 7 + rel
        if tgt in hits:
            hits[tgt].append(TEXT_VA + i)
    return hits


def string_vas(needle: bytes) -> list[int]:
    out, i = [], 0
    with open(DUMP, "rb") as f:
        data = f.read()
    while True:
        i = data.find(needle, i)
        if i < 0:
            return out
        out.append(i)
        i += 1


def func_start(text: bytes, va: int, back: int = 0x600) -> int:
    lo = max(TEXT_VA, va - back)
    chunk = text[lo - TEXT_VA:va - TEXT_VA]
    idx = len(chunk)
    while idx > 0 and chunk[idx - 1] in (0xCC, 0x90):
        idx -= 1
    return lo + idx if idx < len(chunk) else va


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "--disasm":
        from capstone import CS_ARCH_X86, CS_MODE_64, Cs
        md = Cs(CS_ARCH_X86, CS_MODE_64)
        text = load_text()
        va = int(args[1], 16)
        for ins in md.disasm(text[va - TEXT_VA:va - TEXT_VA + 400], va):
            print(f"{ins.address:#x}  {ins.mnemonic:<7}{ins.op_str}")
            if ins.mnemonic == "ret":
                break
        return 0

    wanted = {a.encode(): a for a in args}
    targets: set[int] = set()
    found = {}
    for needle in wanted:
        vas = string_vas(needle)
        found[needle] = vas
        targets.update(vas[:4])
    text = load_text()
    hits = refs_to(text, targets)
    for needle, vas in found.items():
        print(f"== {needle.decode()}: {len(vas)} occurrences")
        for va in vas[:4]:
            sites = hits.get(va, [])
            funcs = sorted({func_start(text, s) for s in sites})
            print(f"   {va:#x}: {len(sites)} refs {[hex(s) for s in sites[:4]]} funcs {[hex(f) for f in funcs[:4]]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
