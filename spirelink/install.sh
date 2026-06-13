#!/usr/bin/env bash
# Install the built mod into the game's local mods directory.
# The loader reads <exe dir>/mods recursively; each subfolder needs manifest.json + <id>.dll.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/../config.sh"
APP="$STS2_APP"
MODS_DIR="$APP/Contents/MacOS/mods"

if [[ ! -f "$HERE/dist/spirelink/spirelink.dll" ]]; then
  echo "ERROR: build first ( ./build.sh )"; exit 1
fi

mkdir -p "$MODS_DIR/spirelink"
cp "$HERE/dist/spirelink/spirelink.dll" "$MODS_DIR/spirelink/spirelink.dll"
cp "$HERE/dist/spirelink/manifest.json" "$MODS_DIR/spirelink/manifest.json"

echo ">> installed to: $MODS_DIR/spirelink"
ls -la "$MODS_DIR/spirelink"
echo
echo "Now launch 'Slay the Spire 2' from Steam. Look for '--- RUNNING MODDED! ---' in the log:"
echo "  ~/Library/Application Support/SlayTheSpire2/logs/godot.log"
echo "and a SpireLink line in:"
echo "  ~/Library/Application Support/SlayTheSpire2/spirelink.log"
