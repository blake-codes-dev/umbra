"""Static call-graph walker over a memory dump.

The dumps are full image layouts: file offset == RVA. We only need .text, so
the walker reads that range once and decodes on demand.

  python tools/disasm.py callers 0x77dfb0 --depth 2
  python tools/disasm.py func 0x2585900          # disassemble one function
  python tools/disasm.py scan 0x77dfb0           # every E8 that targets it
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from capstone import CS_ARCH_X86, CS_MODE_64, Cs

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DUMP = ROOT / "dump" / "merged-exe.bin"
IMAGE_BASE = 0x140000000
TEXT_OFF = 0x1000
TEXT_LEN = 105_075_420
TEXT_VA = TEXT_OFF

md = Cs(CS_ARCH_X86, CS_MODE_64)
md.detail = False

_text: bytes | None = None


def text() -> bytes:
    global _text
    if _text is None:
        with open(DUMP, "rb") as f:
            f.seek(TEXT_OFF)
            _text = f.read(TEXT_LEN)
    return _text


def read(va: int, n: int) -> bytes:
    off = va - TEXT_VA
    if off < 0 or off >= len(text()):
        return b""
    return text()[off:off + n]


def is_code(va: int) -> bool:
    chunk = read(va, 16)
    if not chunk or chunk.count(0) == len(chunk):
        return False
    ins = next(md.disasm(chunk, va, 1), None)
    return ins is not None


def func_start(va: int, back: int = 0x800) -> int:
    """Walk back to the usual prologue (int3 padding or push rbp/mov rbp,rsp)."""
    lo = max(TEXT_VA, va - back)
    chunk = read(lo, va - lo)
    for i in range(len(chunk) - 4, -1, -4):
        if chunk[i:i + 1] == b"\xcc" and chunk[i + 1:i + 2] == b"\xcc":
            return lo + i + 1
    return lo


def disasm_func(va: int, limit: int = 400) -> list:
    start = func_start(va)
    out = []
    for ins in md.disasm(read(start, 4096), start):
        if not out and ins.mnemonic == "int3":
            continue
        out.append(ins)
        if ins.mnemonic == "ret" or len(out) >= limit:
            break
    return out


def callers(target: int, span: int = TEXT_LEN) -> list[int]:
    """Find direct E8 calls whose rel32 resolves to target."""
    data = text()
    hits = []
    i = 0
    while True:
        i = data.find(b"\xe8", i)
        if i < 0 or i + 5 > len(data):
            break
        rel = int.from_bytes(data[i + 1:i + 5], "little", signed=True)
        if TEXT_VA + i + 5 + rel == target:
            hits.append(TEXT_VA + i)
        i += 1
    return hits


def describe(ins) -> str:
    return f"{ins.address:#x}  {ins.mnemonic:<8}{ins.op_str}"


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    cmd = args[0]
    target = int(args[1], 16) if len(args) > 1 else 0
    if cmd == "scan":
        hits = callers(target)
        print(f"{len(hits)} direct call sites -> {target:#x}")
        for h in hits:
            print(f"  {h:#x}  (func {func_start(h):#x})")
        return 0
    if cmd == "func":
        for ins in disasm_func(target):
            print(describe(ins))
        return 0
    if cmd == "callers":
        depth = int(args[args.index("--depth") + 1]) if "--depth" in args else 1
        level = {target}
        for d in range(depth):
            nxt = set()
            for t in sorted(level):
                hits = callers(t)
                print(f"depth {d}: {t:#x} <- {len(hits)} sites")
                for h in hits:
                    start = func_start(h)
                    nxt.add(start)
                    print(f"   caller {start:#x} (call at {h:#x})")
            level = nxt
            if not level:
                break
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
