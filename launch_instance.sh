#!/usr/bin/env bash
# Launch one isolated game instance: a given SpireLink port + a given HOME directory.
# If the HOME directory does not exist it is stamped fresh from home_template/
# (see make_home_template.sh) — this is the hermetic-run primitive.
#
# Usage: launch_instance.sh [PORT] [HOME_DIR]
#   PORT     default 5555
#   HOME_DIR default $SPIRELINK_DEV_HOME (the legacy shared HOME; NOT hermetic)
#
# Prints the game PID on stdout (last line) so callers can manage the process.
# STS_HEADLESS=0 launches with a window.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config.sh"

PORT="${1:-5555}"
HOME_DIR="${2:-$SPIRELINK_DEV_HOME}"
TEMPLATE="$SPIRELINK_ROOT/home_template"

if [ ! -d "$HOME_DIR" ]; then
  [ -d "$TEMPLATE" ] || { echo "ERROR: no home_template/ — run make_home_template.sh first" >&2; exit 1; }
  mkdir -p "$HOME_DIR"
  cp -R "$TEMPLATE/." "$HOME_DIR/"
fi

# Saves live under steam/<steam-id>/. The template carries the ID of the machine
# that captured it; if this machine's Steam account differs, the game would start
# a fresh profile and hermeticity silently breaks. Rename the template's ID dir
# to the local account's (detected from the real game's save dir, or override
# with SPIRELINK_STEAM_ID in config.local.sh).
STEAM_DIR="$HOME_DIR/Library/Application Support/SlayTheSpire2/steam"
if [ -d "$STEAM_DIR" ]; then
  tmpl_id="$(ls "$STEAM_DIR" 2>/dev/null | head -1)"
  local_id="${SPIRELINK_STEAM_ID:-}"
  if [ -z "$local_id" ] && [ -d "$HOME/Library/Application Support/SlayTheSpire2/steam" ]; then
    local_id="$(ls "$HOME/Library/Application Support/SlayTheSpire2/steam" 2>/dev/null | head -1)"
  fi
  if [ -n "$tmpl_id" ] && [ -n "$local_id" ] && [ "$tmpl_id" != "$local_id" ]; then
    mv "$STEAM_DIR/$tmpl_id" "$STEAM_DIR/$local_id"
  fi
fi

FLAG="--headless"
[ "${STS_HEADLESS:-1}" = "0" ] && FLAG=""

cd "$SPIRELINK_TEST_APP/Contents/MacOS"
# shellcheck disable=SC2046
env HOME="$HOME_DIR" SPIRELINK_PORT="$PORT" $(sts2_exec_prefix) "./Slay the Spire 2" $FLAG \
  > "${SPIRELINK_LOG:-/tmp/sts2_$PORT.log}" 2>&1 &
echo $!
