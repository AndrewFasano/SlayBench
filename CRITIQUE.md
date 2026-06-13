# SpireLink design critique

A review of the SpireLink prototype (C# mod + line-JSON protocol + MCP bridge) that exposes
Slay the Spire 2 to AI agents. Covers the mod (`Driver.cs`, `DriverScreens.cs`,
`Decisions.cs`, `Snapshot.cs`, `SpireServer.cs`, `Commands.cs`), the bridge
(`mcp_server.py`, `real_play.py`, `mock_game.py`), `PROTOCOL.md`, and spot-checks of the
decompiled game source to verify what data is available but not yet exposed.

## Overall verdict

The core architecture is genuinely good. Hijacking the game's own `AutoSlayer` QA harness
and Harmony-patching its per-room handlers so each decision blocks on a
`TaskCompletionSource` (`Decisions.Ask` ⇄ `decide`) is the right interaction model: the
game stays authoritative, the client is guaranteed to see every decision, and the
id-checked `decide` prevents stale answers. Main-thread marshaling is handled correctly,
the layering (mod → line-JSON → MCP) is clean, and the isolated test app + `CloudGuard`
shows good operational hygiene. The gaps below are mostly about *what flows through* that
pipe, not the pipe itself.

## Status update — decision-coverage pass

A round of work after this review closed most of gap 4 and the decision-type half of gap 5.
Gaps 1, 2, 3, 6, 7 are untouched.

**Addressed**
- **Character selection** is now a `start_run(character=…)` choice (gaps 4, 5). Implemented by
  replacing `AutoSlayer.PlayMainMenuAsync` with an isolated copy. (An earlier attempt patched
  the shared generic `Rng.NextItem<NCharacterSelectButton>`; because .NET shares ref-type
  generic code, that patch landed on *every* `NextItem<refT>` call and slowed map-gen past the
  run-start timeout — reverted. See newly-identified #3.)
- **Smith / transform / enchant / in-combat hand picks** are now `card_select` decisions, via a
  single patch of `AutoSlayCardSelector.GetSelectedCards` — and notably model-level, not
  UI-clicking, partially answering the "inconsistent driving" note.
- **Combat rewards no longer auto-take**: the agent explicitly takes gold/potion/relic/card or
  proceeds (`combat_reward`), removing the "auto-take potions/relics" sub-point of gap 4.
- **Boss/event relic choice** is now a `relic_select` decision.
- The mod now emits **10 of the 11** documented decision types (all but `game_over`, surfaced as
  a `phase` rather than a decision) — the "only 7 of 11" drift in gap 5 is largely closed.

**Still open from the original gaps**
- Gap 1 entirely: intent is still type-only (`Snapshot.cs` — `IntentType.ToString()`), and there
  is still no card/relic/power rules text and no event body.
- Gaps 2, 3, 6, 7 — unchanged.
- Gap 4 leftovers: shop still filters out the card-removal service (`DriverScreens.cs` —
  `!(s is NMerchantCardRemoval)`); targeting still handles only `TargetType.AnyEnemy`.
- Gap 5 leftovers: `start_run`'s `ascension` is still accepted by MCP and ignored by the mod;
  `mock_game.py`'s combat decision shape still differs from the real mod (its `hand` key vs the
  mod's action-bearing `options`), so the contract-test recommendation stands.

## Status update — takeover / runnability pass

A second pass focused on making the system reliable to actually run with a real (slow,
deliberating) agent. Validated end-to-end on the real game.

**Addressed**
- **Critical, newly discovered: AutoSlayer's QA timeouts killed slow agents.** A pending
  decision aborted the run after ~29s (observed live: an unanswered `combat_reward` →
  `run ended (exit 1)`), via the 30s no-progress `Watchdog`, the 30s–2min per-screen
  `WithTimeout` caps, and a 25min run cap. An LLM agent thinking between tool calls would
  routinely trip these. Fixed in `mod/Robustness.cs` with two narrow patches:
  `Watchdog.Check` resets while a decision is pending, and `WaitHelper.WithTimeout`
  stretches the umbrella caps. Genuine wedges are still caught (inner `Until` timeouts
  remain). Verified: a decision held 95s, then answered and accepted.
- **Recovery (gap 3, partially):** `abandon_run` command + MCP tool. Cancels the pending
  decision and the AutoSlayer task; `last_result: "abandoned"`. `OnRunEnded` now also
  returns the game to the main menu (`NGame.ReturnToMainMenu`) — vanilla AutoSlayer
  exited the process there, so without this no second `start_run` could ever work.
  Verified: start → abandon mid-combat → restart → play.
- **Error surfacing (gap 2, the `observe` half):** run failures now report a diagnostic via
  `observe().last_error` (patched `AutoSlayLog.RunFailed`). Verified live — it pinpointed
  the return-to-menu bug above. (`decide` validate-vs-clamp is still open.)
- **Contract drift (gap 5, the mock/test half):** `play_test.py` consumed mock-only fields
  (`hand`, `reachable_next`) and defaulted to port 5555 — the *real game* — so the "mock
  test" silently drove the live instance (and crashed). Both test scripts now spawn their
  own mock on an ephemeral port (a fixed port was found squatted by a stale mock, silently
  testing old code) and use only protocol-documented shapes; the mock's combat/map/
  combat_reward/shop decisions were aligned to the mod's exact option shapes. Both tests
  pass against the mock; `mcp_play_test.py` also verified against the real game.
- PROTOCOL.md / README updated: `abandon_run`, `last_result`/`last_error`, no-decision-
  timeout semantics, and an explicit note that `ascension` is mock-only.

**Still open (unchanged ranking):** P0 rich observations (gap 1) and `decide`
validate-or-reject (gap 2); P1 structured end-of-run summary (gap 6) and long-poll
`observe` (gap 7); P2/P3 as listed below.

## Status update — production-eval pass

A third pass closed the P0/P1 backlog and shipped an eval harness. All validated on the
real game (invalid-input rejection, intent numbers, full-run completion).

**Addressed**
- **Gap 1 (rich observations) — closed.** Cards/relics/potions/powers/orbs carry resolved
  rules text (BBCode-stripped); enemy intents carry computed damage × hits via
  `AttackIntent.GetTotalDamage` (the game's own hover-tip path); event decisions include
  the event title/body and per-option detail; combat exposes draw/discard/exhaust pile
  CONTENTS, orbs (`OrbQueue`), and player block; `card_select` includes resolved upgrade
  previews; shop items carry rules text.
- **Gap 2 (validate-or-reject) — closed.** `Decisions.Validate` checks every choice
  against the pending decision's options centrally at `Submit`; invalid input returns an
  error listing the valid values and the decision stays pending. Verified live for bad
  coords, bad/unplayable card indices, and unknown actions. The mock has matching
  semantics.
- **Gap 6 (structured outcomes) — closed.** `observe().run_summary` = {victory, score
  (via `ScoreUtility.CalculateScore`), act, floor, hp, gold, deck, relics}, captured in
  `OnRunEnded` before `RunManager.CleanUp`. Win ≠ loss at last.
- **Gap 7 (long-poll) — closed.** `observe {wait_s}` blocks on the socket thread through
  `busy` phases (main thread stays free); the MCP `observe` tool defaults to wait_s=20.
- **Gap 4 leftover (shop card removal) — closed.** The removal service's purchase runs
  `CardSelectCmd.FromDeckForRemoval`, which the existing `AutoSlayCardSelector` patch
  already routes to the client — so un-filtering the slot was sufficient.
- **Eval harness:** `bridge/eval.py` — N seeded runs, per-decision JSONL transcript
  (observation + choice + timing), per-run `summary.json`, aggregate `results.jsonl`,
  stall abandonment, crash relaunch. Speed: FastMode now defaults to `Instant`
  (~2-3× faster runs). `SPIRELINK_PORT` env makes the port configurable (supervisor
  prerequisite). Fixed a dangerous `pkill` pattern that could match the user's real
  Steam game (test instance is `./Slay the Spire 2` — relative path — which is the
  discriminator).

**Still open:** run config (ascension honored, character variety policy); targeting
beyond `AnyEnemy`; potion discard; multi-instance supervisor; runtime-verify
`relic_select`; act-boss preview on the map; CI for the contract tests.

**End-to-end validation (final):** a complete game was played through the interface on
the real game — interactive LLM-driven prefix (combat with intent-damage block math,
targeted potions, shop card-removal via `card_select`, treasure, card rewards chosen from
rules text, an invalid choice rejected and retried) + reference policy to game over:
1,170+ decisions, Act 3 / floor 48, `outcome: "defeat"`, score 1310. The Ancient-event
dialogue fix fired 4× during the run. Two more findings fixed en route: `[img]` BBCode
blocks leaked resource paths into rules text, and intent `damage` is the TOTAL across
hits (policy + docs corrected). Run-content determinism depends on save meta-state, not
just the seed — hermetic per-run HOMEs are required for replay-exact evals (supervisor
backlog).

## Handoff notes (for the next team)

**Current state.** The pipeline works end-to-end on the real game: an agent makes every in-run
decision and can play multiple acts. What's weak is *what flows through the pipe* (observations)
and *eval-grade robustness* (error handling, recovery, structured outcomes) — see the prioritized
backlog at the end.

**Repo contents.** `spirelink/mod/` (C# mod), `spirelink/bridge/` (MCP server + CLI + mock +
greedy reference client), the `*.md` docs, and `run_test.sh`/`redeploy.sh`. The decompiled game
source and the ~2 GB game copy are intentionally **not** committed (`.gitignore`).

**Prerequisites**
- Docker — the mod is built in a .NET 9 SDK container (the host `dotnet` is unusable on this box).
- Slay the Spire 2 installed via Steam, with Steam running.
- Because the mod references the game's own assemblies, the game install must be present to build.

**Regenerate the decompiled source** (essential reference for extending the mod; gitignored):
```
DATA=".../SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
docker run --rm -v "$DATA":/game:ro -v "$PWD/decomp":/out mcr.microsoft.com/dotnet/sdk:8.0 \
  bash -lc 'export PATH=$PATH:/root/.dotnet/tools;
            dotnet tool install -g ilspycmd --version 9.0.0.7889;
            ilspycmd -p -o /out/sts2_src /game/sts2.dll'
```

**Build / iterate loop**
- `./redeploy.sh` — build the mod (Docker), copy it into the isolated test app, relaunch it.
- `python3 spirelink/bridge/real_play.py 200` — drive a full run with the greedy reference client.
- `python3 spirelink/bridge/spire_cli.py observe` — poke the live game by hand.

**Launch gotchas (this machine specifically)**
- The native **arm64** .NET runtime crashes on launch here (`EXC_GUARD` in CoreCLR's Mach
  exception thread) because of the `-arm64e_preview_abi` boot-arg. The harness runs the **x86_64
  slice under Rosetta** instead (`run_test.sh`). The clean fix is to drop that boot-arg, reboot,
  and run native — which would also be faster and remove the Rosetta-induced timing flakiness.
- Testing uses a **separate copy** of the app (`sts2-test.app`) with a private `HOME`
  (`testhome/`) so it never touches the real game/saves; `CloudGuard` also blocks every Steam
  Cloud write. First-time setup: copy the app, drop `steam_appid.txt` (`2868840`) in
  `Contents/MacOS/`, launch once to create `settings.save`, then set
  `mod_settings.mods_enabled = true` in it (the game gates mod-loading behind a first-run "mods
  warning" the headless instance can't click through).
- A `Room type not assigned` error on `start_run` means run-start was slowed past its timeout —
  historically from over-broad Harmony patches (see Status update / newly-identified #3). Keep new
  patches narrow; never patch shared generic methods like `Rng.NextItem`.

## Major gaps

### 1. Observations are too thin for an agent to actually play well

This is the biggest gap given the stated purpose. An agent sees `CARD.STRIKE_IRONCLAD`,
`RELIC.BURNING_BLOOD`, `POWER.*` — bare IDs with no rules text. STS2 is a new game with
mostly new content, so an LLM cannot fall back on memorized STS1 knowledge. The data is
available in the model layer: `CardModel.Description` / `GetDescriptionForPile()`
(decomp `CardModel.cs:109,1056`) resolves live numbers, and relics/powers/potions have
equivalents.

Worse, enemy intent is serialized as just the intent *type* (`Snapshot.cs:117-119` —
`it.IntentType.ToString()` → `"Attack"`), with no damage amount or repeat count, even
though `AttackIntent.DamageCalc` / `GetTotalDamage()` / `Repeats` exist in the game model.
Without incoming-damage numbers, the single most important calculation in every Spire
turn — "how much do I need to block?" — is impossible.

Events have the same problem: `EventAsync` sends option titles only, no event body text,
so the agent picks between "Pray" and "Leave" blind.

**Recommendation:** inline descriptions and resolved numbers in options/state, or add a
`get_info(id)` reference-lookup tool (or both — inline for hand/intents, lookup for the
long tail).

### 2. Errors after `decide` vanish; invalid choices are silently rewritten

`decide` returns `{accepted:true}` the moment `Submit` resolves the TCS — but the
*consequence* of the choice runs afterward in the AutoSlayer task. If `PlayCard` throws
(`bad card_index`, `Driver.cs:103`) or a `WaitHelper` times out, the exception is
unobserved by any client; the run likely wedges in `busy` forever with no diagnostic.

The non-combat screens fail in the opposite direction: an out-of-range `option_index` is
silently clamped to option 0 (`DriverScreens.cs:68,107-108,137-138`), so an agent's
malformed choice becomes a *different decision* without anyone noticing — poison for
evaluation validity.

**Recommendation:** validate the choice synchronously inside `decide` (reject, don't
clamp), and surface async handler failures through `observe` (e.g. `phase: "error"` plus
a `last_error` field).

### 3. No recovery path

There is no `abandon_run` / `reset` command and no timeout on `Decisions.Ask`. If a
handler wedges (UI race, timeout throw, the 5000-iteration combat guard), `RunActive`
stays true and the only fix is restarting the game process. `real_play.py`'s
"stuck busy, aborting" counter is a tacit admission. For an unattended eval harness
running many runs back-to-back, a watchdog-able reset command is table stakes.

### 4. Strategically important decisions are still taken away from the agent

The README acknowledges some (smith's *which card to upgrade*, transform/enchant,
character select), but two deserve stronger flagging:

- The shop explicitly filters out the card-removal service (`DriverScreens.cs:192` —
  `!(s is NMerchantCardRemoval)`), and removal is arguably the strongest gold sink in
  the game.
- Combat rewards auto-take potions/relics, which matters when potion slots are full or a
  relic is situationally bad.

Also, targeting only handles `TargetType.AnyEnemy` (`Driver.cs:106,125`) — any other
target type silently gets `null`, which will break or misplay cards with unusual
targeting.

An evaluation of "can the agent play StS2 well" is skewed when the harness makes several
of the highest-leverage choices itself.

### 5. Protocol / mock / implementation drift

`PROTOCOL.md` and `mock_game.py` define decision types the real mod never emits
(`combat_reward`, `card_select`, `relic_select`, `game_over` — only 7 of the 11
documented types appear as `Decisions.Ask` call sites in the mod). `start_run` accepts
`character` and `ascension` through MCP, and the mod silently ignores both
(`Driver.StartRun` passes only the seed to `slayer.Start`). An agent developed against
the mock or the docs will mis-handle the real game.

**Recommendation:** a contract test that runs the same client script against both
implementations, and either implement or remove the unsupported decision types and
`start_run` parameters.

### 6. Run outcomes are too coarse to evaluate with

A run ends as `last_result: "completed" | "failed"` from AutoSlayer's exit code — it is
not even clear a *lost* run is distinguishable from a *crashed* one. There is no score,
floor reached, cause of death, final deck/relics, or decision transcript. For the stated
goal (evaluating AI agents), the harness should emit a structured end-of-run summary and
ideally a replayable per-decision log; the game's `NGameOverScreen` / run-summary data
exists to draw from.

### 7. The polling model is expensive for LLM agents

During enemy turns and animations, the agent must spin `observe()` → `busy` →
`observe()`, and each poll is a full MCP tool round-trip (tokens + latency), each
returning the full state snapshot. A long-poll variant — `observe(wait_seconds=20)` that
blocks server-side until the next decision or timeout — would collapse most `busy` polls
into one call. This is cheap to add since the socket thread can simply wait on the
decision rendezvous.

## Smaller design notes

- **Inconsistent driving strategy.** Map navigation uses the model API
  (`RunManager.EnterMapCoord` — deterministic, headless-safe, and the comment at
  `Driver.cs:245` explains why), but rest/shop/treasure/card-reward drive the UI by
  clicking Godot nodes found via hardcoded scene paths, padded with fixed
  `Task.Delay(400–1000ms)` sleeps. That is slow, flaky, and will shatter on game patches.
  Migrating screens to model-level APIs like the map handler is the obvious hardening
  path.
- **Multiple TCP clients can connect concurrently** with no ownership of the pending
  decision — two agents (or a stray CLI poke) can race `decide`. Fine for a prototype;
  worth a single-client lock eventually.
- **`Decisions.Ask` overwrites `_pending` without cancelling a prior `_tcs`.** Today the
  flow is sequential so it cannot happen, but an assert/throw there would turn a silent
  deadlock into a loud bug if reentrancy ever changes (the event→combat reentry path in
  `EventAsync` is exactly where to worry).
- **The unauthenticated localhost port** plus the `IsReleaseGame` patch (which also
  unlocks the dev console) means any local process can drive the game. Acceptable for
  this setup; worth stating in the README as an explicit non-goal.
- **In-combat deck visibility.** `get_deck` is documented as out-of-combat, and combat
  state exposes only pile *counts*. Strong play relies on pile contents (what is left in
  the draw pile, what was discarded) — STS1 tooling like CommunicationMod exposed these.

## Newly identified (post decision-coverage)

Issues introduced or surfaced by the decision-coverage work:

1. **`card_select` / `relic_select` are as observation-thin as gap 1.** The agent picks which
   card to upgrade/transform/enchant from bare IDs (`CARD.STRIKE_DEFECT`) with no upgrade
   preview (current vs upgraded numbers), and relic options are labeled best-effort (`relic N`
   when no `NRelic` child is found) with no relic text. These are high-leverage choices made
   blind — same root cause as gap 1, now on more screens.

2. **`relic_select` is unverified at runtime.** It compiles and reuses the proven Ask+click
   mechanism, but no test seed actually triggered a relic-choice screen, so its node lookup /
   click behavior is unconfirmed (unlike the others, which were observed firing). Needs a
   forced-scenario test (e.g. dev-console relic event) before it can be trusted in an eval.

3. **Character selection re-introduces the UI fragility it was meant to avoid.** The fix copies
   `PlayMainMenuAsync` verbatim, hardcoding ~8 menu scene paths (`MainMenuTextButtons/…`,
   `Submenus/CharacterSelectScreen/…`) plus the abandon-run modal flow. This is duplicated game
   code on the *critical run-start path* that will break on any menu re-layout — the maintenance
   liability the "inconsistent driving" note warns about, now load-bearing.

4. **No-character runs are deterministic, not random.** When `start_run` omits `character`, the
   copied handler selects the *first unlocked* character every time (the original used
   `_random.NextItem`). Good for reproducibility, but it silently removes character variety from
   any eval that doesn't set the character — should be made explicit (seed-random, or a required
   parameter). Character matching is also a loose substring `IndexOf`, so an unexpected id could
   mis-match.

5. **No run-configuration surface beyond `character`.** Still no way to set ascension, daily, or
   custom-run modifiers — all of which materially change difficulty and are needed to evaluate an
   agent across the game's real settings. (Generalizes gap 5's `ascension` point: the parameter
   exists in the MCP tool but is dropped on the floor.)

6. **Potions are take-or-use only.** `use_potion` shares the card targeting limitation
   (`AnyEnemy` only), and there is no discard-potion action — which now matters because the agent
   controls the "take this potion?" reward decision and can therefore fill its own slots with no
   way to free them.

7. **The decision surface keeps growing on UI-click + fixed-sleep handlers.** `combat_reward` and
   `relic_select` add more `UiHelper.Click` + `Task.Delay(…)` screen-driving, widening the
   fragility/latency surface. `card_select` (model-level via `GetSelectedCards`) is the
   counter-example to migrate the others toward.

8. **`combat_reward` can mask gap 2's silent-clamp problem.** Its take-loop re-presents after each
   take and treats any out-of-range/unrecognized choice as "proceed" — so a malformed reward
   choice silently ends reward collection instead of erroring, exactly the validity hazard gap 2
   describes, on a newly-added screen.

## Prioritized backlog

Ranked for impact on *evaluation validity* first, then unattended-run robustness, then breadth,
then hardening. Refs point to the rationale above. This subsumes the original "top two."

**P0 — without these, run quality does not reflect agent skill**
1. **Rich observations** (gap 1; newly-id #1). Enemy intent *damage* + repeat count; card / relic
   / power rules text via `CardModel.GetDescriptionForPile()` and the model equivalents; event
   body text; upgrade previews in `card_select`. Suggest inline for hand/intents + a `get_info(id)`
   lookup tool for the long tail. *This is the single highest-value item.*
2. **`decide` validates-or-rejects** instead of silently clamping out-of-range choices, and async
   handler failures surface through `observe` (`phase:"error"` + `last_error`) (gap 2; newly-id #8).

**P1 — needed to run an unattended eval at all**
3. **Recovery**: an `abandon_run` / `reset` command plus a timeout on `Decisions.Ask`, so a wedged
   handler doesn't require killing the game process (gap 3).
4. **Structured end-of-run summary**: win / lose / abandon (currently indistinguishable), floor,
   score, cause of death, final deck/relics, and a per-decision transcript (gap 6).
5. **Long-poll `observe(wait_seconds)`** to collapse `busy` spins into one MCP round-trip (gap 7).

**P2 — completeness & correctness of the decision surface**
6. Restore the **taken-away choices**: shop card-removal service (gap 4; newly-id #5/#6); full card
   & potion targeting beyond `AnyEnemy`; a discard-potion action; run config
   (ascension / daily / custom modifiers — the MCP `ascension` param is currently dropped).
7. **In-combat pile contents** (draw / discard / exhaust), not just counts (smaller notes; gap 1).
8. **Runtime-verify `relic_select`** by forcing a relic-choice scenario (dev console) — it is
   unverified at runtime (newly-id #2).

**P3 — hardening & hygiene**
9. Migrate the **UI-click + fixed-sleep** screen handlers to model-level APIs, as the map handler
   and `card_select` already do; reduces flakiness and game-patch fragility, including the copied
   `PlayMainMenuAsync` on the run-start path (smaller notes; newly-id #3, #7).
10. Single-client **ownership** of the pending decision; assert on `Decisions.Ask` reentrancy; a
    **contract test** running one client script against both the mock and the real mod (gap 5);
    and document the unauthenticated localhost port (+ the `IsReleaseGame`/dev-console unlock) as
    an explicit non-goal.
