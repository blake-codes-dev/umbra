"""Page-wise best-of-N merge, streaming so RAM stays around 200 MB.

Earlier version loaded every dump at once (147 MB each); with 20 snapshots that
was ~3 GB and it helped take the box down. Now one page from one file is in
memory at a time and only the merged image is held.

  python tools/dump_merge.py -o dump/merged7.bin dump/a.bin dump/b.bin ...
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PAGE = 4096


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    out = Path("dump/merged.bin")
    for i, a in enumerate(argv):
        if a == "-o" and i + 1 < len(argv):
            out = Path(argv[i + 1])
    files: list[Path] = []
    skip = False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg == "-o":
            skip = True
            continue
        if arg.endswith(".bin"):
            files.append(Path(arg))
    if len(files) < 2:
        print("need at least two dumps")
        return 1

    size = min(f.stat().st_size for f in files)
    size -= size % PAGE
    handles = [open(f, "rb") for f in files]
    merged = bytearray(size)
    changed = 0
    try:
        for offset in range(0, size, PAGE):
            best, best_score = None, None
            for h in handles:
                h.seek(offset)
                page = h.read(PAGE)
                if len(page) < PAGE:
                    page = page + b"\x00" * (PAGE - len(page))
                score = page.count(0)
                if best_score is None or score < best_score:
                    best, best_score = page, score
            merged[offset:offset + PAGE] = best
            if best_score < PAGE:
                changed += 1
    finally:
        for h in handles:
            h.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes(merged))
    total_zero = merged.count(0) / size * 100
    all_zero = sum(1 for i in range(0, size, PAGE) if not any(merged[i:i + PAGE]))
    print(f"merged {len(files)} dumps -> {out}  ({size / 2**20:.0f} MB)")
    print(f"pages with data: {changed}  zero bytes: {total_zero:.1f}%  all-zero pages: {all_zero}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
