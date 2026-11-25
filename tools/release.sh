#!/usr/bin/env bash
# Release build. Works in this container: no Windows interop needed, dotnet is
# installed into /tmp/dotnet by the step below.
#
#   bash tools/release.sh 0.1.0-dev
#
# Output: dist/Umbra-<version>-win-x64.zip + the hash to post in #builds.
set -euo pipefail

version="${1:-0.0.0-dev}"
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

if [ ! -x /tmp/dotnet/dotnet ]; then
  curl -sSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh
  bash /tmp/dotnet-install.sh --channel 9.0 --install-dir /tmp/dotnet --no-path >/dev/null
fi
export DOTNET_ROOT=/tmp/dotnet
export PATH="/tmp/dotnet:/tmp/tools:$PATH"
if [ ! -x /tmp/tools/obfuscar.console ]; then
  dotnet tool install --tool-path /tmp/tools Obfuscar.GlobalTool >/dev/null
fi

proj=src/app/Umbra

# native payload: normally built by src/native/build.bat (MSVC, real SEH). Fall back to mingw
# here so the artifact is never shipped without a core, and say so when it is degraded.
mkdir -p src/app/Umbra/payload
if [ -f src/native/core.c ]; then
  if command -v x86_64-w64-mingw32-gcc >/dev/null; then
    x86_64-w64-mingw32-gcc -shared -O2 -mno-stack-arg-probe src/native/core.c \
      -o src/app/Umbra/payload/core.dll -lwinhttp -luser32 || {
        echo "core.dll build failed"; exit 1; }
    echo "core.dll built with mingw (SEH guards are MSVC-only; build.bat on Windows is the real one)"
  else
    echo "no mingw and no core.dll: the app will refuse to inject"
  fi
fi

(cd "$proj" && dotnet build -c Release -r win-x64)

# obfuscar resolves WPF types from copies next to the module when it is not on Windows
refpack=/config/.nuget/packages/microsoft.windowsdesktop.app.ref
newest="$(ls -d "$refpack"/*/ref/net9.0 2>/dev/null | sort -V | tail -1 || true)"
if [ -n "$newest" ]; then
  cp "$newest"/Presentation*.dll "$newest"/WindowsBase.dll "$newest"/System.Xaml.dll \
     "$proj/bin/Release/net9.0-windows/win-x64/" 2>/dev/null || true
fi

rm -rf dist/protected
obfuscar.console -v:q obfuscar.xml
cp dist/protected/Umbra.dll "$proj/bin/Release/net9.0-windows/win-x64/Umbra.dll"

rm -rf dist/publish
(cd "$proj" && dotnet publish -c Release -r win-x64 --self-contained true --no-build \
  -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true \
  -p:EnableCompressionInSingleFile=true -p:Version="$version" -o "$root/dist/publish")

mkdir -p dist/release
cp dist/publish/Umbra.exe dist/release/
cp -r dist/publish/library dist/release/
# runtime payload and the chunks the core loads: without these the app can never inject
cp -r dist/publish/luau dist/release/
# payload/ sits inside the project and publish skips it, so take it from the build output
cp -r "$proj/bin/Release/net9.0-windows/win-x64/payload" dist/release/
cp dist/protected/Mapping.txt obfuscar.xml dist/release/
(cd dist/release && sha256sum Umbra.exe > SHA256SUMS.txt)
(cd dist/release && zip -q -r "../Umbra-$version-win-x64.zip" Umbra.exe library luau payload SHA256SUMS.txt Mapping.txt obfuscar.xml)

echo
echo "artifact: dist/Umbra-$version-win-x64.zip"
sha256sum "dist/Umbra-$version-win-x64.zip"
echo "post that hash in #builds with the version, then the changelog line"
