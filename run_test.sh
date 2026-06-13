#!/usr/bin/env bash
# Launch the ISOLATED test copy of STS2 with the shared dev HOME, so saves/cloud
# never touch your real game. Headless (no window) by default.
# STS_HEADLESS=0 launches with a window (needed if headless stalls on render waits).
#
# For hermetic per-run instances, use launch_instance.sh / bridge/instance.py instead.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config.sh"

FLAG="--headless"
[ "${STS_HEADLESS:-1}" = "0" ] && FLAG=""

cd "$SPIRELINK_TEST_APP/Contents/MacOS"
# shellcheck disable=SC2046
exec env HOME="$SPIRELINK_DEV_HOME" $(sts2_exec_prefix) "./Slay the Spire 2" $FLAG
