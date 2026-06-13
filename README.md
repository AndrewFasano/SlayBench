# SlayBench — an AI-agent benchmark on Slay the Spire 2

SlayBench turns **Slay the Spire 2** into a text-controllable evaluation: an AI agent
observes structured game state (with full rules text and computed combat numbers) and
makes **every** player decision — combat, map routing, card rewards, events, shops,
rest sites — through a small tool interface. Runs are **hermetic and
seed-deterministic**, scored from the game's own outcome data, and packaged as an
[Inspect AI](https://inspect.aisi.org.uk/) task. The in-game control layer (mod +
wire protocol) is called **SpireLink** and is usable standalone — point any MCP-capable
agent at it and play interactively.

Why it's a good eval: STS2 is a deep, stochastic-looking but fully determinizable
roguelike whose content post-dates model training data, so agents must *read and
reason*, not recall. A run is hundreds of interdependent decisions with delayed
consequences — deck-building strategy, risk management, and arithmetic all matter.

```
 agent ──(Inspect tools / MCP)──▶ bridge (Python) ──(TCP line-JSON)──▶ spirelink.dll (in game) ──▶ STS2
        observe / decide                                 per-run hermetic instance
```

## What you need

- **macOS** (Apple Silicon or Intel) — the launcher and save isolation are
  macOS-specific today; the mod/protocol themselves are platform-neutral
  (Windows/Linux support is a welcome contribution)
- **Slay the Spire 2 via Steam** (your own copy; Steam running). Verified against
  game **v0.103.3** — other builds may need a mod rebuild and will produce
  non-comparable results (the game build is recorded in every score)
- **Docker** (builds the C# mod in a pinned .NET 9 container)
- **Python ≥3.10 + [uv](https://docs.astral.sh/uv/)**

## Quickstart

```bash
# 0. configure (defaults assume a standard Steam install; override in config.local.sh)
$EDITOR config.local.sh        # optional — see config.sh for the knobs

# 1. one-time: create the isolated game copy (never touches your real install/saves)
#    -> follow docs/TROUBLESHOOTING.md "First-time setup" (5 steps, ~5 minutes)

# 2. build + install the mod into the isolated copy
./spirelink/build.sh && cp spirelink/dist/spirelink/* sts2-test.app/Contents/MacOS/mods/spirelink/

# 3. capture the canonical hermetic baseline (zero-progression profile)
./make_fresh_template.sh

# 4. python env
uv sync   # or: uv venv && uv pip install inspect-ai==0.3.239 anthropic mcp

# 5. smoke-test the plumbing (no API key, no cost — scripted baseline agent)
.venv/bin/inspect eval spirelink/bridge/inspect_task.py@slaybench_pilot \
    --model mockllm/model --solver greedy_solver --max-samples 1

# 6. run a real model (put ANTHROPIC_API_KEY etc. in .env)
.venv/bin/inspect eval spirelink/bridge/inspect_task.py@slaybench_pilot \
    --model anthropic/claude-haiku-4-5 --max-samples 1 --cache-prompt true
```

Browse results with `.venv/bin/inspect view`, or summarize a log directory with
`.venv/bin/python spirelink/bridge/inspect_report.py <log_dir>`.

## The evaluation

Two task variants (both `version=1`, scored identically):

| task | budget | what it measures |
|---|---|---|
| `slaybench_pilot` | ~100 decisions/run | cheap signal: floors reached + decision quality |
| `slaybench` | full runs (win or death) | the real thing: win rate, score, floor |

**Metrics**: `victory` (win rate), `floor`, in-game `score`, `decisions`. Every score
records the requested seed (verified against the actual run seed — mismatches are
flagged), the HOME-template hash, the mod version, and the game build, so results are
auditable and comparable only when they should be. Interpretation notes:
floors-per-decision is the fair cross-agent metric under token budgets (a free
baseline executes more decisions than a paying model); in-game `score` is 0 for
budget-capped incomplete runs.

**Always pass `--max-samples 1`** (one game instance; samples are serialized) and
**`--cache-prompt true`** for real models (observations are large; caching cuts cost
~10×). Bound spend with `--token-limit`.

**Reference results** (token-limited pilot, 3M tokens/sample, 2 seeds): Haiku 4.5
8 floors/86 decisions (~$0.41/run), Sonnet 4.0 7/85 (~$1.26/run), scripted greedy
baseline 11 floors over 150 free decisions — both models beat the baseline
per-decision (0.093/0.082 vs 0.073 floors/decision), with zero invalid tool calls.

## Repo layout

| path | what |
|---|---|
| `spirelink/mod/` | C# mod: Harmony-patches the game's own QA self-play harness so every decision blocks on a connected client; serves the TCP line-JSON protocol |
| `spirelink/bridge/` | Python: Inspect task + tools, MCP server, eval/replay/report utilities, protocol mock + offline contract tests |
| `spirelink/PROTOCOL.md` | the wire protocol and state schema |
| `config.sh`, `launch_instance.sh`, `make_fresh_template.sh` | machine config and hermetic-instance plumbing |
| `docs/TROUBLESHOOTING.md` | first-time setup + platform quirks |

Deep-dive docs: [`spirelink/README.md`](spirelink/README.md) (architecture, MCP usage,
standalone runner), [`spirelink/PROTOCOL.md`](spirelink/PROTOCOL.md).

## Reproducibility model

- **Hermetic instances**: every sample launches a fresh game process with a pristine
  HOME stamped from `home_template/` (run content depends on save meta-progression,
  not just the seed — validated: two independent instances produced byte-identical
  decision streams across a full 271-decision run).
- **Canonical template**: `make_fresh_template.sh` builds the template from a
  factory-fresh profile, so independently-generated templates of the same game build
  are equivalent; the template content hash rides along in every score.
- **Offline contract tests**: `spirelink/test.sh` exercises the full protocol and the
  MCP/Inspect plumbing against a built-in mock — no game, no API key (runs in CI).

## Security & legal notes

- The mod opens an **unauthenticated localhost TCP port** (any local process can
  drive the game) and patches `IsReleaseGame()` to unlock the game's own QA/dev
  facilities. Run it on machines you trust, in the isolated copy.
- The harness only ever drives the **isolated copy** with private HOMEs; `CloudGuard`
  additionally blocks Steam Cloud writes from modded instances.
- This repo contains **no game code or assets** (and they must not be committed —
  see `.gitignore`). You need your own copy of STS2; decompiled reference source for
  mod development is regenerated locally (`docs/TROUBLESHOOTING.md`). Modding uses
  the game's official mod loader; review Mega Crit's terms yourself.

## Known limitations

- macOS-only launcher (see above); single game instance per port (multi-port works,
  a fleet supervisor does not exist yet).
- `ascension` is accepted by the protocol but not yet honored; potion targeting
  beyond single-enemy and potion-discard are not exposed; `relic_select` has never
  been runtime-verified.
- One full run is 300–1200 decisions ≈ thousands of model calls; budget accordingly
  (the pilot task exists for exactly this reason).

## License

MIT for the harness code — see [LICENSE](LICENSE). Slay the Spire 2 belongs to
Mega Crit and is not included.
