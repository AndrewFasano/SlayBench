#!/usr/bin/env bash
# Offline test suite (no game needed): protocol contract test + MCP end-to-end test,
# each against a freshly spawned mock on an ephemeral port.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== play_test (protocol contract) =="
python3 "$HERE/bridge/play_test.py" | tail -3

echo "== mcp_play_test (MCP end-to-end) =="
uv run --with mcp python "$HERE/bridge/mcp_play_test.py" 2>/dev/null | tail -3

echo "== ALL TESTS PASSED =="
