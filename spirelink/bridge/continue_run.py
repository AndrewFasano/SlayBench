#!/usr/bin/env python3
"""Continue the CURRENT in-progress run with the greedy reference policy until it ends.

Useful when another agent (e.g. a human or LLM playing interactively) has played a
prefix of the run and wants the reference policy to finish it out.

Usage: python3 continue_run.py [max_decisions]
"""
import json
import sys
import time

from eval import choose, rpc


def main():
    maxd = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
    decisions = 0
    while decisions < maxd:
        obs = rpc("observe", {"wait_s": 25})
        phase = obs["phase"]
        if phase in ("run_over", "menu"):
            print(f"\n== ended: {obs.get('last_result')} ==")
            print(json.dumps(obs.get("run_summary"), indent=1)[:800])
            return
        if phase != "awaiting_decision":
            time.sleep(0.5)
            continue
        dec = obs["decision"]
        ch = choose(dec, obs["state"])
        rpc("decide", {"decision_id": dec["id"], "choice": ch})
        decisions += 1
        hp = (obs["state"].get("players") or [{}])[0].get("hp")
        print(f"[{decisions:4}] {dec['type']:13} floor={obs['state'].get('total_floor')} "
              f"hp={hp} -> {json.dumps(ch)[:70]}", flush=True)
    print("hit max decisions")


if __name__ == "__main__":
    main()
