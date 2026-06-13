#!/usr/bin/env python3
"""Drive a full real STS2 run through the SpireLink mod with a simple greedy policy.

Reads the mod's actual decision/option format (see PROTOCOL.md) and plays until the
run ends. Prints a transcript. For validating the interface on the real game.

Run:  python3 real_play.py [max_decisions]
"""
import sys, os, time, json
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from spire_cli import call

PORT = 5555
MAXD = int(sys.argv[1]) if len(sys.argv) > 1 else 400


def rpc(cmd, args=None, retries=4):
    last = None
    for attempt in range(retries):
        try:
            r = call(cmd, args, port=PORT, timeout=40)
        except OSError as e:
            last = str(e); time.sleep(1.0); continue
        if r.get("ok"):
            return r["data"]
        last = r.get("error")
        # transient main-thread spikes: retry observe/decide
        if "timed out" in (last or "") or "main-thread" in (last or ""):
            time.sleep(1.0); continue
        raise SystemExit(f"{cmd} failed: {last}")
    raise SystemExit(f"{cmd} failed after {retries} tries: {last}")


def combat_choice(dec, st):
    """Play the first playable card (target enemy 0); else end turn."""
    for o in dec.get("options", []):
        if o.get("action") == "play_card" and o.get("playable"):
            ch = {"action": "play_card", "card_index": o["card_index"]}
            if o.get("needs_target"):
                ch["target_index"] = 0
            return ch, f"play {o.get('label')}"
    return {"action": "end_turn"}, "end turn"


def pick(dec, st):
    t = dec["type"]
    opts = dec.get("options", [])
    if t == "combat":
        return combat_choice(dec, st)
    if t == "map":
        return {"coord": opts[0]["coord"]}, f"go {opts[0]['label']} {opts[0]['coord']}"
    if t == "card_reward":
        return {"option_index": 0}, f"take {opts[0]['label']}"
    if t == "combat_reward":
        takeable = [o for o in opts if o.get("index") != 100]
        if takeable:
            return {"option_index": takeable[0]["index"]}, f"take {takeable[0]['label']}"
        return {"proceed": True}, "proceed"
    if t == "card_select":
        mn = max(int(dec.get("min_select", 1) or 0), 0)
        n = min(max(mn, 1), len(opts))
        return {"indices": list(range(n))}, f"select {n}: {[opts[i]['label'] for i in range(n)]}"
    if t == "relic_select":
        return {"option_index": 0}, f"relic: {opts[0]['label']}"
    if t == "event":
        return {"option_index": 0}, f"event: {opts[0]['label']}"
    if t == "rest":
        smith = next((o for o in opts if any(k in o["label"].lower() for k in ("smith", "upgrade"))), None)
        if smith:
            return {"option_index": smith["index"]}, f"rest: {smith['label']}"
        return {"option_index": 0}, f"rest: {opts[0]['label']}"
    if t == "treasure":
        return {"take": True}, "take relic"
    if t == "shop":
        return {"leave": True}, "leave shop"
    if t == "game_over":
        return {"ack": True}, "ack game over"
    return {"option_index": 0}, f"{t}: default"


def main():
    print("ping:", rpc("ping"))
    if rpc("observe")["phase"] == "menu":
        args = {"seed": "SPIRELINK1"}
        ch = os.environ.get("SPIRE_CHAR", "")
        if ch:
            args["character"] = ch
        print(f">> start_run {args}"); rpc("start_run", args)
    decisions = 0
    last_phase = None
    idle = 0
    while decisions < MAXD:
        obs = rpc("observe")
        phase = obs["phase"]
        if phase in ("run_over", "menu") and decisions > 0:
            p = obs["state"].get("players", [{}])
            print(f"\n== RUN OVER (phase={phase}, last_result={obs.get('last_result')}) after {decisions} decisions ==")
            return
        if phase == "busy":
            idle += 1
            if idle > 200:
                print("stuck busy, aborting"); return
            time.sleep(0.4); continue
        idle = 0
        if phase != "awaiting_decision":
            time.sleep(0.4); continue
        dec = obs["decision"]; st = obs["state"]
        choice, desc = pick(dec, st)
        # concise context
        ctx = ""
        if dec["type"] == "combat":
            c = st.get("combat") or {}
            en = c.get("enemies") or []
            ctx = f"r{c.get('round')} hp={st['players'][0]['hp']} e={c.get('energy')} enemies={[(e['name'],e['hp']) for e in en]}"
        elif st.get("players"):
            ctx = f"floor={st.get('act_floor')} hp={st['players'][0]['hp']} gold={st['players'][0]['gold']}"
        print(f"[{dec['type']:12}] {desc:34} | {ctx}")
        rpc("decide", {"decision_id": dec["id"], "choice": choice})
        decisions += 1
    print(f"\n== hit max {MAXD} decisions ==")


if __name__ == "__main__":
    main()
