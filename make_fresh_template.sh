#!/usr/bin/env bash
# Build a CANONICAL hermetic HOME template from a factory-fresh game profile.
#
# Unlike make_home_template.sh (which snapshots whatever meta-progression your dev
# HOME has accumulated), this produces a zero-progression baseline that anyone can
# regenerate: results from canonical templates of the same game version are
# comparable across machines and labs.
#
# What it does:
#   1. boots the game once with an EMPTY home to let it create settings.save
#   2. enables mod loading in settings.save (the in-game "mods warning" dialog
#      can't be clicked headlessly)
#   3. boots again (now modded) so the modded profile dirs exist, then stops it
#      at the menu
#   4. snapshots the result into home_template/ via make_home_template.sh
#
# Requires: the isolated test app set up (see docs/TROUBLESHOOTING.md) and Steam
# running. Takes ~3-4 minutes.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config.sh"

FRESH="$(mktemp -d "${TMPDIR:-/tmp}/spirelink_fresh_XXXXXX")"
PORT="${SPIRE_PORT:-5557}"
trap 'pkill -9 -f "^\./Slay the Spire 2" 2>/dev/null || true' EXIT

if pgrep -f '^\./Slay the Spire 2' >/dev/null 2>&1; then
  echo "ERROR: a test game instance is running — stop it first"; exit 1
fi

echo ">> boot 1/2: factory-fresh HOME (creating settings.save)..."
PID=$("$SPIRELINK_ROOT/launch_instance.sh" "$PORT" "$FRESH")
SETTINGS=""
for _ in $(seq 1 90); do
  SETTINGS=$(ls "$FRESH/Library/Application Support/SlayTheSpire2/steam/"*/settings.save 2>/dev/null | head -1 || true)
  [ -n "$SETTINGS" ] && break
  sleep 2
done
[ -n "$SETTINGS" ] || { echo "ERROR: settings.save never appeared (log: /tmp/sts2_$PORT.log)"; exit 1; }
sleep 5  # let the first write finish
kill -9 "$PID" 2>/dev/null || true
sleep 2

echo ">> enabling mod loading in settings.save"
python3 - "$SETTINGS" <<'EOF'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
ms = d.get("mod_settings") or {}   # key exists but is null in a fresh save
ms["mods_enabled"] = True
d["mod_settings"] = ms
json.dump(d, open(p, "w"), indent=2)
print("   mods_enabled = true")
EOF

echo ">> boot 2/2: modded boot to the menu (creating modded profile)..."
PID=$("$SPIRELINK_ROOT/launch_instance.sh" "$PORT" "$FRESH")
ok=""
for _ in $(seq 1 90); do
  if nc -z 127.0.0.1 "$PORT" 2>/dev/null; then ok=1; break; fi
  sleep 2
done
[ -n "$ok" ] || { echo "ERROR: mod never came up (log: /tmp/sts2_$PORT.log)"; exit 1; }
sleep 5
kill -9 "$PID" 2>/dev/null || true
sleep 2

echo ">> snapshotting canonical template"
"$SPIRELINK_ROOT/make_home_template.sh" "$FRESH"
rm -rf "$FRESH"
echo ">> done — canonical zero-progression template captured"
