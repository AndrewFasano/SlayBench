#!/usr/bin/env bash
# Rebuild the mod, copy it into the isolated test app, and relaunch the test
# instance (shared dev HOME, headless). The inner dev loop.
set -e
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config.sh"

echo ">> build"
"$SPIRELINK_ROOT/spirelink/build.sh" >/tmp/build.log 2>&1 || { tail -30 /tmp/build.log; exit 1; }
grep -qE "Build succeeded" /tmp/build.log && echo "   build ok" || { tail -20 /tmp/build.log; exit 1; }

echo ">> deploy to test app"
cp "$SPIRELINK_ROOT/spirelink/dist/spirelink/spirelink.dll" \
   "$SPIRELINK_TEST_APP/Contents/MacOS/mods/spirelink/spirelink.dll"

echo ">> relaunch isolated instance"
# Test instances run with a RELATIVE cmdline (./Slay the Spire 2), which is what
# distinguishes them from a Steam-launched real game — never widen this pattern.
pkill -9 -f "^\./Slay the Spire 2" 2>/dev/null || true
sleep 1
bash "$SPIRELINK_ROOT/run_test.sh" > /tmp/sts2test.log 2>&1 &
echo "   launched (log: /tmp/sts2test.log)"
