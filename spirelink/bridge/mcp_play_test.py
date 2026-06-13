#!/usr/bin/env python3
"""End-to-end test of the SpireLink MCP server.

Spawns mcp_server.py over stdio (as a real MCP client would), then plays a full
run by calling the MCP tools observe/start_run/decide with a greedy policy.
Proves: MCP client -> MCP server -> TCP -> game (mock or real mod).

By default it spawns its own mock_game.py on a private port, so it is safe to
run while the real game is up. Set SPIRE_PORT explicitly to target an
already-running server (e.g. the real mod on 5555) instead.

Run:  uv run --with mcp python mcp_play_test.py                  # vs the mock
      SPIRE_PORT=5555 uv run --with mcp python mcp_play_test.py  # vs the real game
"""
import asyncio
import json
import os
import socket
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.environ.get("SPIRE_PORT")  # None -> spawn the mock on an ephemeral port


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def parse(result):
    """Extract the dict a FastMCP tool returned."""
    if getattr(result, "structuredContent", None):
        sc = result.structuredContent
        # FastMCP wraps non-model returns under "result"
        return sc.get("result", sc)
    for block in result.content:
        if getattr(block, "text", None):
            return json.loads(block.text)
    return None


def choose_combat(dec, st):
    # real mod format: options carry action/card_index/playable/needs_target
    for o in dec.get("options", []):
        if o.get("action") == "play_card" and o.get("playable"):
            ch = {"action": "play_card", "card_index": o["card_index"]}
            if o.get("needs_target"):
                ch["target_index"] = 0
            return ch
    return {"action": "end_turn"}


def choose(dec, st):
    t = dec["type"]
    opts = dec.get("options", [])
    if t == "map":
        return {"coord": opts[0]["coord"]}
    if t == "combat":
        return choose_combat(dec, st)
    if t == "card_reward":
        return {"option_index": 0}
    if t == "combat_reward":
        return {"proceed": True}
    if t == "card_select":
        return {"indices": [0]}
    if t == "relic_select":
        return {"option_index": 0}
    if t == "event":
        return {"option_index": 0}
    if t == "shop":
        return {"leave": True}
    if t == "rest":
        return {"option_index": 0}
    if t == "treasure":
        return {"take": True}
    if t == "game_over":
        return {"ack": True}
    return {"option_index": 0}


async def main():
    port = PORT
    mock = None
    if port is None:
        port = str(free_port())
        mock = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_game.py"), port],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await asyncio.sleep(0.5)
    try:
        await drive(port)
    finally:
        if mock:
            mock.terminate()
            mock.wait(timeout=5)


async def drive(port):
    params = StdioServerParameters(
        command="uv", args=["run", "--with", "mcp", "python", os.path.join(HERE, "mcp_server.py")],
        env={**os.environ, "SPIRE_PORT": port},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("MCP tools:", [t.name for t in tools.tools])

            async def tool(name, **args):
                return parse(await session.call_tool(name, args))

            print("ping:", await tool("ping"))
            import os as _os
            MAXD = int(_os.environ.get("MCP_MAXD", "30"))
            steps = 0
            acted = 0
            while True:
                steps += 1
                if steps > 5000:
                    raise SystemExit("too many steps")
                obs = await tool("observe")
                phase = obs["phase"]
                if phase == "menu":
                    print(">> start_run (via MCP)")
                    await tool("start_run", character="IRONCLAD", seed="MCP")
                    continue
                if phase == "busy":
                    await asyncio.sleep(0.3); continue
                if phase == "run_over":
                    p = obs["state"]["players"][0]
                    print(f"== RUN OVER via MCP: hp={p['hp']}/{p['max_hp']} deck={p['deck_count']} acted={acted} ==")
                    return
                dec = obs["decision"]
                ch = choose(dec, obs["state"])
                st = obs["state"]
                hp = (st.get("players") or [{}])[0].get("hp")
                print(f"[{dec['type']:11}] floor={st.get('act_floor')} hp={hp} -> {ch}")
                await tool("decide", decision_id=dec["id"], choice=ch)
                acted += 1
                if acted >= MAXD:
                    print(f"== {acted} decisions made via MCP tools (stopping test) ==")
                    return


if __name__ == "__main__":
    asyncio.run(main())
