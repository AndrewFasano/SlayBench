#!/usr/bin/env bash
# Snapshot the current isolated test HOME into home_template/, the pristine state
# every hermetic run is stamped from.
#
# Run content depends on the save's meta-progression (unlocks, stats), NOT just the
# run seed — so evals are only seed-comparable if every run starts from an IDENTICAL
# HOME. The template freezes whatever meta-state testhome has right now; re-run this
# script to re-baseline (and note it in the eval changelog: results across different
# templates are not comparable).
#
# Capture with the game STOPPED and no run in progress.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config.sh"
ROOT="$SPIRELINK_ROOT"
SRC="${1:-$SPIRELINK_DEV_HOME}"
DST="$ROOT/home_template"

if pgrep -f '^\./Slay the Spire 2' >/dev/null 2>&1; then
  echo "ERROR: a test game instance is running — stop it first (its HOME may be mid-write)"
  exit 1
fi
[ -d "$SRC" ] || { echo "ERROR: $SRC not found"; exit 1; }

rm -rf "$DST"
mkdir -p "$DST"
# Keep settings/profile/save state; drop logs, run history, replays, crash/telemetry.
rsync -a \
  --exclude 'Library/Application Support/SlayTheSpire2/logs/' \
  --exclude 'Library/Application Support/SlayTheSpire2/spirelink.log' \
  --exclude 'Library/Application Support/SlayTheSpire2/sentry.dat' \
  --exclude '**/saves/history/' \
  --exclude '**/replays/' \
  --exclude '*.save.backup' \
  "$SRC/" "$DST/"

# Manifest: content hash + capture time, so evals can record exactly which template
# they ran against (results across different templates are not comparable).
HASH=$(cd "$DST" && find . -type f ! -name MANIFEST.json -print0 | sort -z \
       | xargs -0 shasum -a 256 | shasum -a 256 | cut -d' ' -f1)
STEAM_ID=$(ls "$DST/Library/Application Support/SlayTheSpire2/steam" 2>/dev/null | head -1)
cat > "$DST/MANIFEST.json" <<EOF
{"template_sha256": "$HASH", "captured_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)", "source": "$(basename "$SRC")", "steam_id": "${STEAM_ID:-unknown}"}
EOF

echo ">> template captured: $(du -sh "$DST" | cut -f1) at $DST"
echo ">> manifest: $(cat "$DST/MANIFEST.json")"
