#!/usr/bin/env python3
"""Summarize SpireLink Inspect eval logs into a comparison table.

Usage: .venv/bin/python spirelink/bridge/inspect_report.py [log_dir]

Per sample: model, seed, outcome, floor, decisions, invalid decides (rejected
tool calls), tokens (input/cached/output), and estimated cost. Pricing is
looked up from PRICES (per MTok); cache reads are billed at 0.1x input,
cache writes at 1.25x input.
"""
import sys
from collections import defaultdict

from inspect_ai.log import read_eval_log

# $/MTok: (input, output). Cache read = 0.1x input; cache write = 1.25x input.
PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-0": (3.00, 15.00),
    "claude-sonnet-4-20250514": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "mockllm/model": (0.0, 0.0),
}


def price_for(model):
    for k, v in PRICES.items():
        if k in model:
            return v
    return (None, None)


def main(log_dir):
    import glob
    rows = []
    for path in sorted(glob.glob(f"{log_dir}/*.eval")):
        log = read_eval_log(path)
        if not log.samples:
            continue
        # use any sample that actually scored, even if a sibling sample errored
        # (log.status is "error" if ANY sample failed)
        model = str(log.eval.model)
        scored = [s for s in log.samples
                  if s.scores and s.error is None]
        for s in scored:
            sc = list(s.scores.values())[0]
            usage = defaultdict(int)
            for mu in (log.stats.model_usage or {}).values():
                usage["input"] += mu.input_tokens or 0
                usage["output"] += mu.output_tokens or 0
                usage["cache_read"] += mu.input_tokens_cache_read or 0
                usage["cache_write"] += mu.input_tokens_cache_write or 0
            # tool errors = rejected decides/observes the model had to recover from
            tool_errors = sum(
                1 for m in s.messages
                if getattr(m, "role", "") == "tool" and getattr(m, "error", None)
            )
            pin, pout = price_for(model)
            cost = None
            if pin is not None and len(scored) > 0:
                # usage is per-log; attribute evenly across samples in the log
                n = len(scored)
                cost = (usage["input"] / n * pin
                        + usage["cache_read"] / n * pin * 0.1
                        + usage["cache_write"] / n * pin * 1.25
                        + usage["output"] / n * pout) / 1_000_000
            v = sc.value if isinstance(sc.value, dict) else {}
            rows.append({
                "model": model.split("/")[-1],
                "seed": str(s.id),
                "outcome": sc.answer,
                "floor": v.get("floor"),
                "decisions": v.get("decisions"),
                "victory": v.get("victory"),
                "tool_errors": tool_errors,
                "msgs": len(s.messages),
                "in_tok": usage["input"] // max(1, len(scored)),
                "cached": usage["cache_read"] // max(1, len(scored)),
                "out_tok": usage["output"] // max(1, len(scored)),
                "cost": cost,
                "log": path.split("/")[-1][:40],
            })
    if not rows:
        print("no successful eval logs found in", log_dir)
        return
    hdr = ["model", "seed", "outcome", "floor", "decisions", "tool_errors",
           "msgs", "in_tok", "cached", "out_tok", "cost"]
    print(" | ".join(f"{h:>10}" for h in hdr))
    print("-" * (13 * len(hdr)))
    for r in rows:
        cells = []
        for h in hdr:
            val = r[h]
            if h == "cost" and val is not None:
                val = f"${val:.2f}"
            cells.append(f"{str(val):>10}")
        print(" | ".join(cells))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "eval_results/inspect_logs")
