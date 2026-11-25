#!/usr/bin/env bash
# Build and run the native harness under wine.
# Needs: mingw gcc/g++, a Windows Luau static build, wine. See tests/README.md.
#   LUAU_BUILD=/tmp/luaub LUAU_SRC=/tmp/luau tools/test_native.sh
set -euo pipefail
cd "$(dirname "$0")/.."
LUAU_BUILD="${LUAU_BUILD:-/tmp/luaub}"
LUAU_SRC="${LUAU_SRC:-/tmp/luau}"
OUT="dist/test"
mkdir -p "$OUT"

if [ ! -f "$LUAU_BUILD/libLuau.VM.a" ]; then
  echo "no Luau build at $LUAU_BUILD - see tests/README.md"
  exit 1
fi

# Luau's VM is C++ with mangled names, so the harness compiles as C++ too; core.c is
# included from it so the same source that ships is the one under test
x86_64-w64-mingw32-g++ -std=c++17 -O1 -g -fno-ipa-icf -DLYPHEN_HOST \
  -I"$LUAU_SRC/VM/include" -I"$LUAU_SRC/Compiler/include" \
  -I"$LUAU_SRC/Common/include" -I"$LUAU_SRC/Ast/include" \
  -x c++ tests/host.c -x none \
  "$LUAU_BUILD/libLuau.Compiler.a" "$LUAU_BUILD/libLuau.Ast.a" \
  "$LUAU_BUILD/libLuau.Bytecode.a" \
  "$LUAU_BUILD/libLuau.VM.a" "$LUAU_BUILD/libLuau.Common.a" \
  -lstdc++ -lwinhttp -luser32 -static -o "$OUT/host.exe"

# the calibration path looks for the client's module name, so test under that name too
cp "$OUT/host.exe" "$OUT/RobloxPlayerBeta.exe"
# every run is kept; a non-zero exit also keeps a numbered copy beside it
set +e
WINEPREFIX=/config/.wx DISPLAY=:1.0 /usr/lib/wine/wine64 "$OUT/RobloxPlayerBeta.exe" 2>&1 | tee "$OUT/last.log"
status="${PIPESTATUS[0]}"
set -e
if [ "$status" -ne 0 ]; then
  cp "$OUT/last.log" "$OUT/crash-$(date +%s).log"
  [ -f "$OUT/crash.log" ] && cp "$OUT/crash.log" "$OUT/crash-$(date +%s)-app.log"
  echo "kept $OUT/last.log"
fi
# calibration is fragile by nature (it guesses at packed code), so it runs on its own and is
# reported separately instead of being able to take the suite down with it
set +e
WINEPREFIX=/config/.wx DISPLAY=:1.0 WINEDEBUG=-all timeout 40 /usr/lib/wine/wine64 \
  "$OUT/RobloxPlayerBeta.exe" calibrate \
  > "$OUT/calibrate.log" 2>&1
cal_status=$?
set -e
if [ "$cal_status" -eq 0 ]; then
  grep -E "^  ok|^PASSED" "$OUT/calibrate.log" | sed 's/^/  /'
  echo "calibration: ok"
else
  echo "calibration: CRASHED or failed (separate, does not fail the suite) - $OUT/calibrate.log"
  tail -3 "$OUT/calibrate.log"
fi
exit "$status"
