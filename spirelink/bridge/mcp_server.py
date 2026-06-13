#!/usr/bin/env python3
"""SpireLink MCP server.

Exposes the SpireLink game-control protocol (PROTOCOL.md) as MCP tools, so an
MCP-capable agent (Claude, etc.) can observe and play Slay the Spire 2. It is a
thin client of the line-JSON TCP server provided by either the in-game mod
(spirelink.dll) or the mock (mock_game.py).

Run (standalone):  uv run --with mcp python mcp_server.py
Configure as an MCP server with command:
    uv run --with mcp python /abs/path/bridge/mcp_server.py
Environment: SPIRE_HOST (default 127.0.0.1), SPIRE_PORT (default 5555).
"""
import json
import os
import socket
from typing import Any

from mcp.server.fastmcp import FastMCP

HOST = os.environ.get("SPIRE_HOST", "127.0.0.1")
PORT = int(os.environ.get("SPIRE_PORT", "5555"))

mcp = FastMCP("spirelink")
_counter = [0]


def _call(cmd: str, args: dict | None = None, timeout: float = 30.0) -> dict:
    """One line-JSON request/response against the SpireLink TCP server."""
    _counter[0] += 1
    req = {"id": _counter[0], "cmd": cmd}
    if args:
        req["args"] = args
    try:
        with socket.create_connection((HOST, PORT), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except OSError as e:
        return {"ok": False, "error": f"cannot reach SpireLink at {HOST}:{PORT}: {e}. "
                                      f"Is the modded game (or mock) running?"}
    line = buf.split(b"\n", 1)[0]
    return json.loads(line.decode("utf-8"))


def _data(resp: dict) -> Any:
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error", "unknown error"))
    return resp["data"]


@mcp.tool()
def ping() -> dict:
    """Check the SpireLink connection. Returns version and whether a run/combat is active."""
    return _data(_call("ping"))


@mcp.tool()
def start_run(character: str = "IRONCLAD", ascension: int = 0, seed: str = "") -> dict:
    """Start a new run from the main menu.

    character: e.g. IRONCLAD / SILENT / DEFECT / REGENT / NECROBINDER.
    Returns {started: true}. Then call observe() to see the first decision.
    """
    args = {"character": character, "ascension": ascension}
    if seed:
        args["seed"] = seed
    return _data(_call("start_run", args))


@mcp.tool()
def observe(wait_s: float = 20.0) -> dict:
    """Observe the current situation — THE primary tool. Returns:

      phase: "menu" | "awaiting_decision" | "busy" | "run_over"
      decision: null, or {id, type, prompt, options[...], ...type-specific context}
      state: full readable snapshot (run, player(s), combat board if any)
      run_summary: structured outcome (victory, score, floor, deck) once a run ends

    Long-polls: if the game is busy (animations / enemy turn), the call blocks
    server-side up to wait_s seconds until a decision is pending, the run ends,
    or the wait expires — so you rarely need to poll.

    When phase == "awaiting_decision", read decision.options and call decide()
    with decision.id and a choice. When phase == "menu", call start_run().
    """
    wait_s = max(0.0, min(wait_s, 60.0))
    return _data(_call("observe", {"wait_s": wait_s}, timeout=wait_s + 30.0))


@mcp.tool()
def decide(decision_id: int, choice: dict) -> dict:
    """Answer the current pending decision. `choice` shape depends on decision.type:

      combat:        {"action":"play_card","card_index":I,"target_index":J?} |
                     {"action":"use_potion","potion_index":I,"target_index":J?} |
                     {"action":"end_turn"}
      map:           {"coord":{"col":C,"row":R}}   (one of state.../decision.reachable_next)
      card_reward:   {"option_index":I} | {"skip":true}
      combat_reward: {"option_index":I} | {"proceed":true}
      event:         {"option_index":I}
      shop:          {"buy_index":I} | {"leave":true}
      rest:          {"option_index":I}
      treasure:      {"take":true} | {"skip":true}
      card_select:   {"indices":[...]} | {"skip":true}
      relic_select:  {"option_index":I}
      game_over:     {"ack":true}

    Returns {accepted:true}. Then call observe() again to see the result.
    An INVALID choice is rejected with an error and the decision stays pending —
    read the error message (it lists the valid indices) and decide() again.
    """
    return _data(_call("decide", {"decision_id": decision_id, "choice": choice}))


@mcp.tool()
def abandon_run() -> dict:
    """Abandon the current run (escape hatch if the game seems stuck or you want to restart).

    The run ends with last_result == "abandoned"; then call start_run() to begin a new one.
    """
    return _data(_call("abandon_run"))


@mcp.tool()
def get_map() -> dict:
    """Read the act map: all nodes (coord + type + children), current position, and reachable_next."""
    return _data(_call("get_map"))


@mcp.tool()
def get_deck() -> dict:
    """Read the full current deck (out of combat)."""
    return _data(_call("get_deck"))


@mcp.tool()
def get_state() -> dict:
    """Read the full state snapshot (same as observe().state)."""
    return _data(_call("get_state"))


if __name__ == "__main__":
    mcp.run()
