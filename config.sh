#!/usr/bin/env bash
# Central configuration for the SpireLink eval harness.
#
# Every launcher/build script sources this file. Defaults below work for a
# standard macOS + Steam install; override anything machine-specific in
# config.local.sh (gitignored) next to this file — do not edit defaults in place.
#
# shellcheck disable=SC2034

# Repo root (directory containing this file)
SPIRELINK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Your Steam installation of Slay the Spire 2 (used to build the mod against the
# game's assemblies, and as the source for the isolated test copy).
STS2_APP="${STS2_APP:-$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app}"

# The game's managed assemblies (referenced at mod build time).
STS2_GAME_DATA="${STS2_GAME_DATA:-$STS2_APP/Contents/Resources/data_sts2_macos_arm64}"

# The ISOLATED copy of the game the harness drives (never your real install —
# see docs/TROUBLESHOOTING.md "First-time setup" for how to create it).
SPIRELINK_TEST_APP="${SPIRELINK_TEST_APP:-$SPIRELINK_ROOT/sts2-test.app}"

# Legacy shared HOME for ad-hoc dev (hermetic eval runs use per-run temp HOMEs).
SPIRELINK_DEV_HOME="${SPIRELINK_DEV_HOME:-$SPIRELINK_ROOT/testhome}"

# CPU architecture to run the game under. "native" (default) runs the binary
# directly; "x86_64" forces Rosetta — only needed on machines where the arm64
# .NET runtime crashes (see docs/TROUBLESHOOTING.md "Rosetta workaround").
SPIRELINK_ARCH="${SPIRELINK_ARCH:-native}"

# Machine-local overrides (gitignored).
if [ -f "$SPIRELINK_ROOT/config.local.sh" ]; then
  # shellcheck source=/dev/null
  . "$SPIRELINK_ROOT/config.local.sh"
fi

# Helper: launch command prefix for the chosen architecture.
sts2_exec_prefix() {
  if [ "$SPIRELINK_ARCH" = "x86_64" ]; then
    echo "arch -x86_64"
  else
    echo ""
  fi
}
