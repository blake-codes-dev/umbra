#!/usr/bin/env python3
"""Cross-check the prologue decoder in core.c against capstone over a real binary.

The decoder only has to walk whole instructions - it decides how many bytes a hook displaces -
but every instruction it cannot measure is an unhookable target, and every instruction it
measures too short corrupts the replay. So walk every byte offset of a .text and compare.

usage: decoder_fuzz.py <exe> [--limit N] [--dump N]
"""
import subprocess
import sys
import tempfile
import os
from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import X86_OP_MEM, X86_REG_RIP

CORE = os.path.join(os.path.dirname(__file__), "..", "src", "native", "core.c")


def build_probe(tmp):
    """Generate a probe that exposes sub_insn_len for a whole buffer at once."""
    src = open(CORE).read()
    lines = src.splitlines(True)
    start = next(n for n, l in enumerate(lines) if l.startswith("static int sub_insn_len("))
    end = next(n for n in range(start, len(lines)) if lines[n].rstrip() == "}")
    body = "".join(lines[start:end + 1])
    # strip the LYPHEN_HOST-only printing, keep the function self-contained
    probe = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef unsigned char u8;
static int g_insn_rip;
@BODY@
int main(int argc, char **argv)
{
    FILE *f = fopen(argv[1], "rb");
    long size;
    u8 *buf;
    int offset = 1, limit = argc > 2 ? atoi(argv[2]) : 0;
    long at;
    if (!f) return 2;
    fseek(f, 0, SEEK_END); size = ftell(f); fseek(f, 0, SEEK_SET);
    buf = (u8 *)malloc(size); fread(buf, 1, size, f); fclose(f);
    for (at = 0; at + 16 < size && (limit <= 0 || at < limit); at++) {
        int len;
        g_insn_rip = 0;
        len = sub_insn_len(buf + at);
        printf("%ld %d %d\n", at, len, g_insn_rip);
    }
    return 0;
}
""".replace("@BODY@", body)
    path = os.path.join(tmp, "probe.c")
    open(path, "w").write(probe)
    exe = os.path.join(tmp, "probe.exe")
    subprocess.run(["x86_64-w64-mingw32-gcc", "-O1", "-o", exe, path], check=True)
    return exe


def text_range(data):
    """raw file offset + size of .text from the PE section table"""
    import struct
    lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    count = struct.unpack_from("<H", data, lfanew + 6)[0]
    opt = struct.unpack_from("<H", data, lfanew + 20)[0]
    at = lfanew + 24 + opt
    for _ in range(count):
        name = data[at:at + 8].rstrip(b"\0")
        size, raw = struct.unpack_from("<II", data, at + 16)
        if name == b".text":
            return raw, size
        at += 40
    raise SystemExit("no .text")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    exe = sys.argv[1]
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 20000

    data = open(exe, "rb").read()
    raw, size = text_range(data)
    size = min(size, limit)

    tmp = tempfile.mkdtemp()
    input_bin = os.path.join(tmp, "text.bin")
    open(input_bin, "wb").write(data[raw:raw + size])

    wine_bin = os.environ.get("WINE_BIN", "")
    if not wine_bin:
        for guess in ("/usr/lib/wine/wine64", "/usr/bin/wine64", "/usr/bin/wine"):
            if os.path.exists(guess):
                wine_bin = guess
                break
        else:
            raise SystemExit("no wine")
    wine = subprocess.run([wine_bin, build_probe(tmp), input_bin],
                          capture_output=True, text=True,
                          env={**os.environ, "WINEDEBUG": "-all"})
    if wine.returncode != 0:
        print("probe failed:", wine.returncode, wine.stderr[-400:])
        return 1
    decoded = {}
    for line in wine.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3:
            decoded[int(parts[0])] = (int(parts[1]), int(parts[2]))

    # linear sweep so we only compare at real instruction boundaries
    mdl = Cs(CS_ARCH_X86, CS_MODE_64)
    mdl.detail = True
    code = data[raw:raw + size]
    checked = 0
    short = 0
    over = 0
    rip_missed = 0
    for ins in mdl.disasm(code, 0):
        if ins.address + 16 > size:      # past the last offset the probe decodes
            break
        got, rip = decoded.get(ins.address, (None, 0))
        checked += 1
        if not got:
            short += 1
            if short <= 6:
                print(f"  MISS  {ins.address:#x} {ins.bytes.hex()} {ins.mnemonic} {ins.op_str}")
        elif got < len(ins.bytes):
            short += 1
            if short <= 6:
                print(f"  SHORT {ins.address:#x} dec={got} cap={len(ins.bytes)} {ins.mnemonic} {ins.op_str}")
        elif got > len(ins.bytes):
            over += 1
            if over <= 6:
                print(f"  LONG  {ins.address:#x} dec={got} cap={len(ins.bytes)} {ins.mnemonic} {ins.op_str}")
        wants = any(o.type == X86_OP_MEM and o.mem.base == X86_REG_RIP for o in ins.operands)
        if wants and not rip:
            rip_missed += 1
            if rip_missed <= 6:
                print(f"  RIP   {ins.address:#x} {ins.mnemonic} {ins.op_str} not flagged")

    print(f"insns={checked} miss={short} over={over} rip_missed={rip_missed}")
    return 1 if (short or over or rip_missed) else 0


if __name__ == "__main__":
    sys.exit(main())
