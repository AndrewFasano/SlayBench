# SlayBench workspace

SlayBench = the benchmark (Inspect tasks, scoring, docs). SpireLink = the in-game
control layer (mod + protocol) it's built on; code/env-var names keep the SpireLink
prefix.
SpireLink exposes Slay the Spire 2 to AI agents as a text/MCP environment. Layout:
`spirelink/mod/` (C# mod, Harmony patches over the game's AutoSlayer), `spirelink/bridge/`
(MCP server, eval runner, mock, tests), `decomp/` (decompiled game source, reference only,
gitignored), `sts2-test.app/` + `testhome/` (isolated test instance + its private HOME,
gitignored).

Machine-specific settings (paths, CPU arch) live in `config.sh` defaults +
gitignored `config.local.sh` overrides — keep machine facts there or in
`docs/TROUBLESHOOTING.md`, never hardcoded in scripts or this file.

## Commands

- Inspect eval (primary): `.venv/bin/inspect eval spirelink/bridge/inspect_task.py@slaybench_pilot --model <provider/model>`; no-cost plumbing check: `--model mockllm/model --solver greedy_solver`
- Hermetic template: `./make_fresh_template.sh` (canonical zero-progression baseline; game stopped). `make_home_template.sh <home>` snapshots an arbitrary HOME. Re-baselining invalidates cross-template comparisons — the MANIFEST hash is recorded in every score.

- Build + deploy + relaunch the test game: `./redeploy.sh` (Docker required; ~1 min)
- Offline tests (no game): `spirelink/test.sh`
- Eval runs: `python3 spirelink/bridge/eval.py --runs N` (transcripts in `eval_results/`)
- Poke the live game: `python3 spirelink/bridge/spire_cli.py observe`
- Mod log: `testhome/Library/Application Support/SlayTheSpire2/spirelink.log`

## Hard-won facts (do not rediscover)

- The run seed flows: AutoSlayer sets `NGame.DebugSeedOverride` → **NCharacterSelectScreen
  NULLS it in singleplayer when the screen opens** → `StartRunLobby` reads it at confirm.
  The mod re-asserts the seed just before clicking Confirm (Driver.PlayMainMenuAsync);
  the ACTUAL seed is exposed in `state.seed`/`run_summary.seed` and the Inspect scorer
  trips on mismatch. Before this fix, every "seeded" run was randomly seeded.
- Full-run determinism holds (validated: 271 byte-identical decisions across two fresh
  instances) ONLY with hermetic HOMEs — run content depends on save meta-progression.
- Inspect react agents need `truncation="auto"` here: observations are self-contained
  and ~1k tokens each; without trimming, full runs overflow any context window.

- The test instance's process cmdline is the RELATIVE `./Slay the Spire 2` (run_test.sh
  cd's into the app). `pkill -f '^\./Slay the Spire 2'` kills only it, never the user's
  Steam-launched real game.
- Never patch shared generic methods (e.g. `Rng.NextItem<T>`) — .NET shares ref-type
  generic code and the patch lands everywhere; this once broke run-start (`Room type not
  assigned` = run-start slowed past its timeout).
- AutoSlayer's QA timeouts (30s watchdog, per-screen caps, 25min run cap) would kill any
  run whose decision sits pending ~30s; `mod/Robustness.cs` suppresses them while a
  decision is pending. Keep that invariant when adding handlers.
- Vanilla AutoSlayer EXITS THE PROCESS at run end; our `QuitGame` patch suppresses that,
  so `Driver.OnRunEnded` must capture `run_summary` and call `NGame.ReturnToMainMenu()`
  (else the next `start_run` fails with "MainMenu node not found").
- Some machines need the game under Rosetta (`SPIRELINK_ARCH=x86_64` in
  config.local.sh) — see docs/TROUBLESHOOTING.md for the symptom.
- `decide` must validate-or-reject (centrally in `Decisions.Submit`); never clamp invalid
  input to a default option — it corrupts eval validity.
- The mock (`bridge/mock_game.py`) must emit EXACTLY the mod's decision/option shapes —
  no extra fields — or clients silently grow mock-only dependencies (this happened).
  `play_test.py`/`mcp_play_test.py` spawn their own mock on an ephemeral port; a fixed
  port was once squatted by a stale mock, silently testing old code.
- Game APIs for observations (from `decomp/`): `CardModel.GetDescriptionForPile`,
  `AttackIntent.GetTotalDamage`/`Repeats`, `EventRoom.LocalMutableEvent`,
  `PlayerCombatState.OrbQueue`, `ScoreUtility.CalculateScore(runState, won)`,
  `run.CurrentRoom.IsVictoryRoom` (victory detection). Rules text needs BBCode stripping
  (`Snapshot.CleanText`).

## Conventions

- Test against the mock first (`spirelink/test.sh`), then redeploy and validate on the
  real game before committing mod changes.
- PROTOCOL.md is the contract: mod, mock, and clients must all match it. Update all
  three together.
- Commit in logical units; mod changes that alter the protocol bump PROTOCOL.md in the
  same commit.
