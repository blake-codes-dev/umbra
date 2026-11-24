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
cp dist/protected/Mapping.txt obfuscar.xml dist/release/
(cd dist/release && sha256sum Umbra.exe > SHA256SUMS.txt)
(cd dist/release && zip -q -r "../Umbra-$version-win-x64.zip" Umbra.exe library SHA256SUMS.txt Mapping.txt obfuscar.xml)

echo
echo "artifact: dist/Umbra-$version-win-x64.zip"
sha256sum "dist/Umbra-$version-win-x64.zip"
echo "post that hash in #builds with the version, then the changelog line"
