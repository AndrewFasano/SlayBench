#!/usr/bin/env python3
"""Replay a recorded transcript against the live game.

Starts a run with the transcript's seed/character and re-submits each recorded choice
in order, verifying the decision types line up (runs are seed-deterministic, so an
identical policy path reproduces the run). Useful for regression-testing fixes against
a failure recorded mid-run: replay up to the failure point, then watch the live game
continue past it.

On divergence (decision type mismatch) or transcript exhaustion it stops and leaves
the run live — finish it interactively or with continue_run.py.

Usage: python3 replay.py <transcript.jsonl> [--seed SEED] [--character CHAR]
"""
import argparse
import json
import sys
import time

from eval import rpc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript")
    ap.add_argument("--seed", default=None)
    ap.add_argument("--character", default="IRONCLAD")
    args = ap.parse_args()

    records = [json.loads(l) for l in open(args.transcript) if '"decision"' in l]
    if not records:
        raise SystemExit("no decisions in transcript")
    seed = args.seed
    if not seed:
        raise SystemExit("--seed required (the seed the transcript was recorded with)")

    obs = rpc("observe")
    if obs["phase"] not in ("menu", "run_over"):
        rpc("abandon_run")
        time.sleep(5)
    rpc("start_run", {"seed": seed, "character": args.character})

    for i, rec in enumerate(records):
        obs = rpc("observe", {"wait_s": 60})
        if obs["phase"] != "awaiting_decision":
            print(f"[replay] run ended early at step {i}: {obs.get('last_result')} / {obs.get('last_error')}")
            return
        dec = obs["decision"]
        want = rec["decision"]["type"]
        if dec["type"] != want:
            print(f"[replay] DIVERGED at step {i}: live={dec['type']} recorded={want} — leaving run live")
            return
        rpc("decide", {"decision_id": dec["id"], "choice": rec["choice"]})
        if i % 25 == 0 or i == len(records) - 1:
            print(f"[replay] {i + 1}/{len(records)} ({dec['type']}, floor {obs['state'].get('total_floor')})", flush=True)
    print(f"[replay] all {len(records)} recorded choices submitted — run is live past the recording")


if __name__ == "__main__":
    main()
