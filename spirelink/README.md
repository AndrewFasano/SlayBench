# SpireLink — the text/MCP control layer under SlayBench

> This is the deep-dive doc for the control layer. For the benchmark itself
> (Inspect tasks, scoring, quickstart), start at the [root README](../README.md).

SpireLink turns Slay the Spire 2 into a headless, text-controllable environment so an AI
agent (or any program) can play full runs and be evaluated — **no vision required**. The
agent observes structured game state and makes every player decision (combat, map, card
rewards, events, shops, rest sites, treasure) through tools or a simple JSON protocol.

It is built as a small **C# mod** loaded by the game's own mod loader, which drives the
game through MegaCrit's built-in `AutoSlayer` automation while deferring each decision to
a connected client. An **MCP server** re-exposes this to MCP-capable agents.

```
 agent ──(MCP tools)──▶ mcp_server.py ──(TCP line-JSON)──▶ spirelink.dll (in game) ──▶ STS2
         observe/decide                  127.0.0.1:5555      Harmony-patched handlers
```

## Layout

| Path | What |
|------|------|
| `mod/` | The C# mod (`spirelink.dll`). Patches the game, serializes state, executes actions, serves TCP. |
| `bridge/mcp_server.py` | MCP server exposing `observe` / `decide` / `start_run` / `abandon_run` / `get_map` / `get_deck` / `get_state`. |
| `bridge/spire_cli.py` | One-shot CLI client (for manual poking). |
| `bridge/real_play.py` | A greedy auto-player that drives a full run (reference client / smoke test). |
| `bridge/mock_game.py` | A protocol-faithful mock game (a winnable mini-run) for offline dev/testing. |
| `bridge/mcp_play_test.py` | Spawns the MCP server and plays a run through MCP tools (end-to-end test). |
| `bridge/eval.py` | **Eval runner**: N seeded runs, per-decision JSONL transcripts, outcome summaries, crash relaunch. |
| `PROTOCOL.md` | The line-JSON protocol + state schema. |
| `build.sh` / `install.sh` | Build the mod (in Docker) / install into the game's `mods/`. |

## How it works

- The mod declares a `[ModInitializer]`; on load it applies Harmony patches and opens a TCP
  server on `127.0.0.1:5555`.
- `NGame.IsReleaseGame()` is patched to `false`, which unlocks the built-in `AutoSlayer`
  (a QA self-play harness) and the dev console.
- `start_run` launches `AutoSlayer`, which handles menu navigation, run setup (fast mode,
  non-interactive), and the room/screen flow.
- Each room/screen **handler** (`CombatRoomHandler`, `MapScreenHandler`, `EventRoomHandler`,
  `ShopRoomHandler`, `RestSiteRoomHandler`, `TreasureRoomHandler`, `CardRewardScreenHandler`)
  is Harmony-patched so that instead of choosing randomly it **serializes the options and
  blocks until the client answers** (`Decisions.Ask` ⇄ `decide`).
- Combat is driven via the game's command API (`CardCmd.AutoPlay`, `PlayerCmd.EndTurn`,
  potions); map navigation via `RunManager.EnterMapCoord`. All state reads/writes are
  marshaled onto the Godot main thread.
- **No decision timeout**: AutoSlayer's QA limits (30s no-progress watchdog, 30s–2min
  per-screen caps, 25min run cap) are suppressed while a decision is pending
  (`Robustness.cs`), so a slowly deliberating agent never gets its run killed.
  `abandon_run` is the explicit escape hatch. When a run ends (`completed` / `failed` /
  `abandoned`) the mod returns the game to the main menu, and `observe` reports
  `last_result` (plus `last_error` with the failure reason).
- **Human-parity observations**: every card / relic / potion / power / orb carries its
  resolved rules text; enemy intents carry computed damage × hits; event decisions carry
  the event body; combat exposes draw/discard/exhaust pile contents and orbs; smith-style
  `card_select` includes upgrade previews. (STS2 content is new — agents can't rely on
  STS1 knowledge.)
- **Validated decisions**: an invalid `decide` is rejected with a message listing the
  valid values and the decision stays pending. Invalid input is never silently coerced.
- **Structured outcomes**: `observe().run_summary` gives `{victory, score, act, floor,
  hp, gold, deck, relics}` once a run ends — win and loss are distinguishable.
- **Long-poll**: `observe(wait_s=…)` blocks through `busy` phases server-side.
- Run speed defaults to the game's `Instant` fast mode (`SPIRELINK_FASTMODE=fast` to
  revert); `SPIRELINK_PORT` overrides the TCP port (for multi-instance setups).

See `PROTOCOL.md` for the exact request/response shapes.

## Running it

All machine-specific settings (game install path, isolated-copy path, CPU arch) live in
`config.sh`; override them in a gitignored `config.local.sh`. If the game crashes at
launch on your machine, see `docs/TROUBLESHOOTING.md`.

### Isolated test instance (recommended)
To avoid touching your real game/saves/cloud, testing uses a **separate copy** of the app
with a **private `HOME`**, and the mod additionally **blocks all Steam Cloud writes**
(`CloudGuard`). See `../run_test.sh` and `../redeploy.sh`.

```bash
# build the mod, deploy into the test copy, relaunch it (Rosetta + private HOME, headless)
../redeploy.sh

# drive a full run with the greedy reference client
python3 bridge/real_play.py 3000
```

### Run an Inspect AI eval (recommended)
The eval is ported to [Inspect](https://inspect.aisi.org.uk/) (`bridge/inspect_task.py`):
per-sample hermetic instances, seed tripwire, structured scoring (win rate / floor /
score / decisions), full transcripts in `inspect view`.

```bash
uv pip install inspect-ai     # one-time (repo .venv already has it)
# pilot (~100 decisions/run) — run this first with a real model:
.venv/bin/inspect eval spirelink/bridge/inspect_task.py@slaybench_pilot \
    --model anthropic/claude-sonnet-4-6
# baseline / plumbing check (no API cost):
.venv/bin/inspect eval spirelink/bridge/inspect_task.py@slaybench_pilot \
    --model mockllm/model --solver greedy_solver
```

**Hermetic runs:** `make_home_template.sh` snapshots a pristine HOME (with a content-
hash MANIFEST recorded into every score); each sample stamps a fresh copy via
`launch_instance.sh` / `bridge/instance.py`. Validated: two independent instances with
the same seed produced byte-identical options across an entire 271-decision run.
(Note: the run seed was previously *silently ignored* by the game — fixed in the mod
and now verified per-sample by the scorer.)

**First real-model results** (token-limited pilot, 2 shared seeds, 3M tokens/sample,
prompt caching on — ~$3.60 total): Haiku 4.5 reached 8 floors over 86 decisions
($0.10/floor), Sonnet 4.0 7 floors over 85 ($0.36/floor), greedy baseline 11 floors
over 150 free decisions. Per decision both models out-advance the baseline
(0.093/0.082 vs 0.073 floors/decision); **zero invalid `decide` calls across all four
model samples** — the tool interface is learnable from docstrings alone. Summarize any
log dir with `bridge/inspect_report.py`; always pass `--max-samples 1` and
`--cache-prompt true`.

### Run a standalone eval
```bash
# 3 seeded runs with the reference greedy agent; transcripts + summaries + results.jsonl
python3 bridge/eval.py --runs 3 --character IRONCLAD --out ../eval_results
```
Each run directory contains `transcript.jsonl` (every observation + choice, replayable)
and `summary.json` (victory, score, floor, final deck/relics, decisions, wall time).

### Manual / MCP
```bash
# one-off observe
SPIRE_PORT=5555 python3 bridge/spire_cli.py observe

# run the MCP server (for an MCP-capable agent)
uv run --with mcp python bridge/mcp_server.py     # talks to 127.0.0.1:5555
```

MCP client config (e.g. Claude Code `mcpServers`; substitute your repo path):
```json
{
  "spirelink": {
    "command": "uv",
    "args": ["run", "--with", "mcp", "python",
             "<REPO_ROOT>/spirelink/bridge/mcp_server.py"],
    "env": { "SPIRE_PORT": "5555" }
  }
}
```

## Status

Working end-to-end on the real game (isolated copy): mod load, state observation, and the
client making **every** decision, across multiple acts:

- **character** selection (`start_run(character=…)`)
- **combat** — play card (with target), use potion, end turn
- **map** path choice
- **card_reward** — pick a card or skip
- **combat_reward** — take gold / potion / relic / card, or proceed
- **card_select** — rest-site smith upgrade, transform, enchant, in-combat hand picks
- **relic_select** — boss/event relic choices
- **event** options, **rest** options, **treasure**, **shop** buy/leave

**End-to-end validation (production-eval pass):** complete games have been played through
this interface on the real game — e.g. a 1,170-decision run reaching Act 3 / floor 48
(LLM-driven prefix + reference policy), ending in a proper in-game defeat with
`run_summary: {outcome: "defeat", score: 1310, ...}`, including events (Ancient dialogue
events included), shops with card removal, treasure, smith/steal `card_select`s, targeted
potion use, and rejected-then-retried invalid choices. Transcripts live in
`eval_results/`.

Run-lifecycle robustness (validated on the real game):
- a decision can stay pending indefinitely (tested 95s+; previously the QA watchdog killed
  the run at ~30s) — required for LLM agents that think between tool calls
- `abandon_run` mid-combat → `last_result: "abandoned"` → immediate `start_run` works
- run failures surface a diagnostic via `observe().last_error`

Offline tests (no game needed): `python3 bridge/play_test.py` and
`uv run --with mcp python bridge/mcp_play_test.py` each spawn a private mock on an
ephemeral port and play a full run using only protocol-documented shapes (contract test);
set `SPIRE_PORT=5555` to point either at the real game instead.

Notes / next steps:
- Rosetta is a workaround; native arm64 (remove `-arm64e_preview_abi` from boot-args, reboot)
  would be faster/cleaner.
- The bundled reference client (`real_play.py`) is a greedy placeholder to prove the
  interface; the eval plugs a real model in via the MCP tools.
