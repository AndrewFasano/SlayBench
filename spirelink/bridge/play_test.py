#!/usr/bin/env python3
"""Drive a full SpireLink run with a simple greedy policy (contract test).

Validates the protocol end-to-end against any SpireLink server, using ONLY the
decision/option shapes documented in PROTOCOL.md (no mock-only fields), so the
same client logic works against both the mock and the real mod.

By default it spawns its own mock_game.py on a private port, so it is safe to
run while the real game is up. Set SPIRE_PORT explicitly to target an
already-running server (e.g. the real mod on 5555) instead.

Usage:
    python3 play_test.py                  # self-contained: spawns the mock
    SPIRE_PORT=5555 python3 play_test.py  # drive an already-running server
"""
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from spire_cli import call  # noqa

HERE = os.path.dirname(os.path.abspath(__file__))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise SystemExit(f"mock did not open port {port}")


def rpc(port, cmd, args=None):
    r = call(cmd, args, port=port)
    if not r.get("ok"):
        raise SystemExit(f"RPC {cmd} failed: {r.get('error')}")
    return r["data"]


def choose_combat(dec, state):
    """Greedy, options-based: first playable attack (target 0), else any playable card, else end turn."""
    plays = [o for o in dec.get("options", [])
             if o.get("action") == "play_card" and o.get("playable")]
    plays.sort(key=lambda o: 0 if o.get("type") == "ATTACK" else 1)
    for o in plays:
        ch = {"action": "play_card", "card_index": o["card_index"]}
        if o.get("needs_target"):
            ch["target_index"] = 0
        return ch
    return {"action": "end_turn"}


def choose(dec, state):
    t = dec["type"]
    opts = dec.get("options", [])
    if t == "combat":
        return choose_combat(dec, state)
    if t == "map":
        return {"coord": opts[0]["coord"]}
    if t == "card_reward":
        return {"option_index": 0}
    if t == "combat_reward":
        return {"proceed": True}
    if t == "event":
        return {"option_index": 0}
    if t == "shop":
        return {"leave": True}
    if t == "rest":
        return {"option_index": 0}
    if t == "treasure":
        return {"take": True}
    if t == "card_select":
        return {"indices": [0]}
    if t == "relic_select":
        return {"option_index": 0}
    if t == "game_over":
        return {"ack": True}
    return {"option_index": 0}


def play(port, max_decisions=500):
    print("ping:", rpc(port, "ping"))
    decisions = 0
    idle = 0
    while decisions < max_decisions:
        obs = rpc(port, "observe")
        phase = obs["phase"]
        if phase == "menu":
            print(">> start_run")
            rpc(port, "start_run", {"seed": "PLAYTEST", "character": "IRONCLAD"})
            continue
        if phase == "run_over":
            p = (obs["state"].get("players") or [{}])[0]
            print(f"== RUN OVER after {decisions} decisions "
                  f"(last_result={obs.get('last_result')}, hp={p.get('hp')}) ==")
            return obs
        if phase != "awaiting_decision":
            idle += 1
            if idle > 300:
                raise SystemExit("stuck busy, aborting")
            time.sleep(0.2)
            continue
        idle = 0
        dec = obs["decision"]
        st = obs["state"]
        ch = choose(dec, st)
        hp = (st.get("players") or [{}])[0].get("hp")
        print(f"[{dec['type']:13}] floor={st.get('act_floor')} hp={hp} -> {ch}")
        rpc(port, "decide", {"decision_id": dec["id"], "choice": ch})
        decisions += 1
    raise SystemExit(f"hit max {max_decisions} decisions without run_over")


def main():
    env_port = os.environ.get("SPIRE_PORT")
    if env_port:
        play(int(env_port))
        return
    # self-contained: spawn the mock on a fresh ephemeral port (a fixed port could
    # be squatted by a stale mock, silently testing against old code/state)
    port = free_port()
    mock = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_game.py"), str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_port(port)
        play(port)
        print("PASS")
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=5)
        except subprocess.TimeoutExpired:
            mock.kill()


if __name__ == "__main__":
    main()
