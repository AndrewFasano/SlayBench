#!/usr/bin/env bash
# Build the SpireLink mod assembly in a .NET 9 SDK container, referencing the game's
# managed assemblies directly. Produces dist/spirelink/{spirelink.dll,manifest.json}.
#
# Why Docker: building in a pinned .NET 9 SDK container avoids any host dotnet
# quirks, and the resulting managed DLL is arch-independent.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/../config.sh"
GAME_DATA="$STS2_GAME_DATA"
IMAGE="mcr.microsoft.com/dotnet/sdk:9.0"

if [[ ! -f "$GAME_DATA/sts2.dll" ]]; then
  echo "ERROR: game assemblies not found at:"
  echo "  $GAME_DATA"
  echo "Install Slay the Spire 2 via Steam, or set STS2_APP / STS2_GAME_DATA in config.local.sh"
  exit 1
fi

echo ">> building spirelink.dll ..."
docker run --rm \
  -v "$HERE/mod":/src \
  -v "$GAME_DATA":/game:ro \
  -w /src \
  -e DOTNET_CLI_TELEMETRY_OPTOUT=1 \
  -e DOTNET_NOLOGO=1 \
  "$IMAGE" \
  dotnet build spirelink.csproj -c Release -p:GameLibs=/game -o /src/_out

mkdir -p "$HERE/dist/spirelink"
cp "$HERE/mod/_out/spirelink.dll" "$HERE/dist/spirelink/spirelink.dll"
cp "$HERE/mod/manifest.json"      "$HERE/dist/spirelink/manifest.json"

echo ">> built:"
ls -la "$HERE/dist/spirelink"
echo
echo "Install with:  $HERE/install.sh"
