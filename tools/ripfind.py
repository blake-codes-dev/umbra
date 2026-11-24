"""Find every RIP-relative displacement in .text that lands on a target VA.

Works without guessing opcode bytes: a 32-bit displacement at offset p belongs to
an instruction that ends at p+4, so the target is (p-3)+7+d for a 7-byte LEA or
(p-2)+6+d for a 6-byte LEA -- both reduce to p+4+d. We scan for d == T-(p+4).

Chunked with numpy so peak RSS stays around 300 MB.

  python tools/ripfind.py 0x72c9b58
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DUMP = ROOT / "dump" / "merged-all.bin"
TEXT_OFF = 0x1000
TEXT_VA = TEXT_OFF
CHUNK = 8_000_000


TEXT_LEN = 105_075_420


def load_text(limit: int = TEXT_LEN) -> bytes:
    """Only .text: index i in the returned buffer is VA i + TEXT_VA."""
    with open(DUMP, "rb") as f:
        f.seek(TEXT_OFF)
        return f.read(limit)


def scan(target: int) -> list[int]:
    text = load_text()
    want_idx = target - TEXT_VA          # index space, not VA space
    buf = np.frombuffer(text, dtype=np.uint8)
    hits: list[int] = []
    for start in range(0, len(buf) - 4, CHUNK):
        end = min(start + CHUNK, len(buf) - 4)
        win = np.lib.stride_tricks.sliding_window_view(buf[start:end + 3], 4)
        disp = win.view("<i4").reshape(-1).astype(np.int64)
        pos = np.arange(start, start + len(disp), dtype=np.int64)
        want = want_idx - (pos + 4)
        idx = np.nonzero(disp == want)[0]
        for i in idx:
            hits.append((TEXT_VA + start + int(i)) + 4)
    return hits


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    target = int(sys.argv[1], 16)
    ends = scan(target)
    print(f"{len(ends)} rip-relative refs land on {target:#x}")
    for e in ends[:20]:
        print(f"  instruction ends {e:#x}  (start {e - 7:#x} or {e - 6:#x})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
