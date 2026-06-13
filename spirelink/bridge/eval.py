#!/usr/bin/env python3
"""SpireLink eval runner: play N seeded runs and record transcripts + outcomes.

Each run produces:
  <out>/<run_tag>/transcript.jsonl   one line per decision: the observation the agent
                                     saw, the choice it made, and timing — replayable.
  <out>/<run_tag>/summary.json       the structured end-of-run outcome (run_summary)
and the runner prints/writes an aggregate results.jsonl across runs.

The built-in policy is the greedy reference agent (see choose() — options-based,
protocol-shapes only). Swap in another agent by importing eval and passing policy=.

Robustness: observe long-polls (no busy spin); transient RPC errors retry; if the game
stays unreachable (crash) the runner relaunches it via run_test.sh and abandons the
run; a run with no progress for --stall-timeout seconds is abandoned and recorded.

Usage:
  python3 eval.py --runs 1 --character IRONCLAD --out ../../eval_results
  python3 eval.py --runs 3 --seed-prefix NIGHTLY --max-decisions 3000
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from spire_cli import call  # noqa

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("SPIRE_PORT", "5555"))
RUN_TEST = os.path.join(HERE, "..", "..", "run_test.sh")


class GameGone(Exception):
    pass


def rpc(cmd, args=None, timeout=90, retries=6):
    last = None
    for _ in range(retries):
        try:
            r = call(cmd, args, port=PORT, timeout=timeout)
        except OSError as e:
            last = str(e)
            time.sleep(2.0)
            continue
        if r.get("ok"):
            return r["data"]
        last = r.get("error", "")
        if "timed out" in last or "main-thread" in last:
            time.sleep(2.0)
            continue
        raise RuntimeError(f"{cmd}: {last}")
    raise GameGone(f"{cmd} unreachable after {retries} tries: {last}")


def relaunch_game():
    print("[eval] game unreachable — relaunching via run_test.sh", flush=True)
    # The test instance is launched with a relative path (cd <app>/Contents/MacOS &&
    # ./Slay the Spire 2), which distinguishes it from a Steam-launched real game.
    subprocess.run(["pkill", "-9", "-f", r"^\./Slay the Spire 2"], check=False)
    time.sleep(2)
    subprocess.Popen(["bash", RUN_TEST], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            if call("ping", port=PORT, timeout=5).get("ok"):
                print("[eval] game is back", flush=True)
                return True
        except OSError:
            pass
        time.sleep(3)
    return False


# ---- the reference greedy policy (options-based, protocol shapes only) ----

def choose_combat(dec, state):
    plays = [o for o in dec.get("options", [])
             if o.get("action") == "play_card" and o.get("playable")]
    targets = dec.get("targets", [])
    # incoming damage this turn ("damage" is already the TOTAL across hits)
    incoming = 0
    for t in targets:
        for it in t.get("intent", []):
            incoming += it.get("damage", 0)
    block = (state.get("combat") or {}).get("player_block", 0)
    attacks = [o for o in plays if o.get("type", "").upper() == "ATTACK"]
    skills = [o for o in plays if o not in attacks]
    # crude: if we're going to take damage and hold a skill (usually block), play it first
    order = (skills + attacks) if incoming > block else (attacks + skills)
    for o in order:
        ch = {"action": "play_card", "card_index": o["card_index"]}
        if o.get("needs_target"):
            # hit the lowest-hp living target
            alive = sorted(targets, key=lambda t: t.get("hp", 1 << 30))
            ch["target_index"] = alive[0]["target_index"] if alive else 0
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
        return {"option_index": opts[0]["index"]}
    if t == "combat_reward":
        take = [o for o in opts if o["index"] != 100]
        return {"option_index": take[0]["index"]} if take else {"proceed": True}
    if t == "shop":
        return {"leave": True}
    if t == "treasure":
        return {"take": True}
    if t == "card_select":
        n = max(1, min(dec.get("min_select", 1), len(opts)))
        return {"indices": [o["index"] for o in opts[:n]]}
    if t in ("event", "rest", "relic_select"):
        return {"option_index": opts[0]["index"]}
    if t == "game_over":
        return {"ack": True}
    return {"option_index": opts[0]["index"] if opts else 0}


# ---- single run ----

def play_run(seed, character, outdir, policy, max_decisions, stall_timeout):
    os.makedirs(outdir, exist_ok=True)
    tpath = os.path.join(outdir, "transcript.jsonl")
    decisions = 0
    t_start = time.time()
    last_progress = time.time()

    obs = rpc("observe")
    if obs["phase"] not in ("menu", "run_over"):
        print("[eval] a run is already active — abandoning it first", flush=True)
        rpc("abandon_run")
        time.sleep(5)
    rpc("start_run", {"seed": seed, "character": character})

    with open(tpath, "w") as tf:
        while decisions < max_decisions:
            try:
                obs = rpc("observe", {"wait_s": 25})
            except GameGone:
                if not relaunch_game():
                    return {"result": "crashed", "decisions": decisions,
                            "seed": seed, "character": character}
                return {"result": "crashed", "decisions": decisions,
                        "seed": seed, "character": character}
            phase = obs["phase"]
            if phase == "run_over":
                summary = obs.get("run_summary") or {"result": obs.get("last_result")}
                summary["decisions"] = decisions
                summary["wall_s"] = round(time.time() - t_start, 1)
                summary["seed"] = seed
                with open(os.path.join(outdir, "summary.json"), "w") as sf:
                    json.dump(summary, sf, indent=2)
                return summary
            if phase != "awaiting_decision":
                if time.time() - last_progress > stall_timeout:
                    print("[eval] stalled — abandoning run", flush=True)
                    try:
                        rpc("abandon_run")
                    except Exception:
                        pass
                    last_progress = time.time()
                continue
            dec = obs["decision"]
            ch = policy(dec, obs["state"])
            rec = {"t": round(time.time() - t_start, 1), "n": decisions,
                   "decision": dec, "state": obs["state"], "choice": ch}
            tf.write(json.dumps(rec) + "\n")
            tf.flush()
            try:
                rpc("decide", {"decision_id": dec["id"], "choice": ch})
            except RuntimeError as e:
                # invalid choice rejected — record and fall back to something legal
                rec["rejected"] = str(e)
                tf.write(json.dumps({"n": decisions, "rejected": str(e)}) + "\n")
                fallback = {"action": "end_turn"} if dec["type"] == "combat" else \
                           {"option_index": dec["options"][0]["index"]} if dec.get("options") else {}
                rpc("decide", {"decision_id": dec["id"], "choice": fallback})
            decisions += 1
            last_progress = time.time()
            hp = (obs["state"].get("players") or [{}])[0].get("hp")
            print(f"[{decisions:4}] {dec['type']:13} floor={obs['state'].get('total_floor')} "
                  f"hp={hp} -> {json.dumps(ch)[:70]}", flush=True)
    print("[eval] hit max decisions — abandoning", flush=True)
    rpc("abandon_run")
    return {"result": "max_decisions", "decisions": decisions, "seed": seed}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--character", default="IRONCLAD")
    ap.add_argument("--seed-prefix", default="EVAL")
    ap.add_argument("--out", default=os.path.join(HERE, "..", "..", "eval_results"))
    ap.add_argument("--max-decisions", type=int, default=3000)
    ap.add_argument("--stall-timeout", type=float, default=300)
    ap.add_argument("--hermetic", action="store_true",
                    help="fresh game instance + pristine HOME per run (requires home_template/; "
                         "needed for seed-comparable results)")
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    batch = os.path.join(args.out, stamp)
    os.makedirs(batch, exist_ok=True)
    inst = None
    if args.hermetic:
        from instance import GameInstance
        inst = GameInstance(port=PORT)
    results = []
    try:
        for i in range(args.runs):
            seed = f"{args.seed_prefix}{i + 1}"
            tag = f"run{i + 1}_{seed}"
            print(f"\n=== run {i + 1}/{args.runs} (seed {seed}) ===", flush=True)
            if inst:
                print("[eval] hermetic: launching fresh instance/HOME", flush=True)
                inst.fresh()
            summary = play_run(seed, args.character, os.path.join(batch, tag),
                               choose, args.max_decisions, args.stall_timeout)
            print("summary:", json.dumps(summary)[:300], flush=True)
            results.append(summary)
            with open(os.path.join(batch, "results.jsonl"), "a") as rf:
                rf.write(json.dumps(summary) + "\n")
    finally:
        if inst:
            inst.stop()

    wins = sum(1 for r in results if r.get("victory"))
    print(f"\n=== batch done: {len(results)} runs, {wins} wins -> {batch} ===")


if __name__ == "__main__":
    main()
