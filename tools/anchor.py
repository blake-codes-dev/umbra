"""String -> function -> machine signature, with validation.

ripfind gives candidate instruction ends. An end is real only if disassembling
from end-7 or end-6 produres an instruction that ends exactly there and whose
displacement lands on the target. From the validated site we walk back to the
prologue and cut a 32-byte signature, then test how many times that signature
occurs in .text. Only count == 1 is usable at runtime.

  python tools/anchor.py 0x72c9268
  python tools/anchor.py --all anchors.txt
"""
from __future__ import annotations

import sys
from pathlib import Path

from capstone import CS_ARCH_X86, CS_MODE_64, CS_OP_MEM, Cs

import ripfind

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DUMP = ROOT / "dump" / "merged-all.bin"
TEXT_VA = 0x1000
SIGLEN = 32

md = Cs(CS_ARCH_X86, CS_MODE_64)
md.detail = True


def text() -> bytes:
    return ripfind.load_text()


def validate(text: bytes, target: int, site: int):
    """Disassemble backwards from `site`; return the matching instruction or None."""
    for back in (7, 6, 8, 9, 10):
        start = site - back
        if start < TEXT_VA:
            continue
        chunk = text[start - TEXT_VA: site - TEXT_VA + 8]
        for ins in md.disasm(chunk, start):
            if ins.address + ins.size == site:          # instruction ENDS at the site
                for op in ins.operands[1:]:
                    if op.type == CS_OP_MEM and op.mem.base in (0, 41):
                        got = (ins.address + ins.size + op.mem.disp) & 0xFFFFFFFFFFFFFFFF
                        if got == target:
                            return ins
                break
            if ins.address + ins.size > site:
                break
    return None


def func_start(text: bytes, va: int, back: int = 0x700) -> int:
    lo = max(TEXT_VA, va - back)
    chunk = text[lo - TEXT_VA: va - TEXT_VA]
    idx = len(chunk)
    while idx > 0 and chunk[idx - 1] in (0xCC, 0x90, 0x00):
        idx -= 1
    return lo + idx if 0 < idx < len(chunk) else va


def uniqueness(text: bytes, sig: bytes) -> int:
    n, i = 0, 0
    while True:
        i = text.find(sig, i)
        if i < 0:
            return n
        n += 1
        i += 1


def anchor_for(target: int) -> dict:
    text_ = text()
    out = {"target": hex(target), "sites": 0, "validated": [], "sig": None, "count": 0}
    ends = ripfind.scan(target)
    out["sites"] = len(ends)
    for end in ends:
        ins = validate(text_, target, end)
        if ins is None:
            continue
        start = func_start(text_, ins.address)
        sig = text_[start - TEXT_VA: start - TEXT_VA + SIGLEN]
        if len(sig) < SIGLEN:
            continue
        count = uniqueness(text_, sig)
        out["validated"].append({
            "site": hex(ins.address), "func": hex(start), "sig": sig.hex(),
            "count": count, "insn": f"{ins.mnemonic} {ins.op_str}",
        })
    unique = [v for v in out["validated"] if v["count"] == 1]
    if unique:
        out["sig"] = unique[0]["sig"]
        out["count"] = 1
        out["func"] = unique[0]["func"]
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--all"]
    if not args:
        print(__doc__)
        return 2
    targets = []
    for a in args:
        p = Path(a)
        if p.exists() and p.suffix in (".txt", ".md"):
            targets += [int(x.split()[0], 16) for x in p.read_text().splitlines()
                        if x.strip() and not x.startswith("#")]
        else:
            targets.append(int(a, 16))
    good = 0
    for t in targets:
        r = anchor_for(t)
        flag = "UNIQUE" if r["sig"] else "----"
        print(f"{flag} {t:#x}: {r['sites']} raw, {len(r['validated'])} validated")
        for v in r["validated"][:4]:
            print(f"    site {v['site']} func {v['func']} count {v['count']}  {v['insn']}")
        if r["sig"]:
            good += 1
            print(f"    SIG {r['sig']} func {r['func']}")
    print(f"\n{good}/{len(targets)} unique")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
