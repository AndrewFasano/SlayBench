#!/usr/bin/env python3
"""SlayBench: an Inspect AI (UK AISI) evaluation where an agent plays Slay the Spire 2.

Built on the SpireLink control layer (the in-game mod + line-JSON protocol).

Each Sample is one seeded run. Sample setup launches a hermetic game instance (fresh
HOME stamped from home_template/ — identical meta-progression every run) and starts the
run; the solver is a react agent whose only actions are the SpireLink observe/decide
tools (plus read-only map/deck views); the scorer reads the game's structured
run_summary and verifies the run used the REQUESTED seed (tripwire for the
silently-lost-seed bug class this system has exhibited before).

The agent makes EVERY decision: there is no fallback policy — an invalid decide returns
a tool error (listing valid values) that the model must read and correct.

Run (after `uv pip install inspect-ai` and setting a provider API key):

    # budget-capped pilot first (~100 decisions/run — about half of Act 1):
    inspect eval spirelink/bridge/inspect_task.py@slaybench_pilot \
        --model anthropic/claude-sonnet-4-6

    # full runs (deep runs are LONG; see token/cost notes below):
    inspect eval spirelink/bridge/inspect_task.py@slaybench \
        --model anthropic/claude-sonnet-4-6

    # plumbing check / baseline, no model decisions (greedy reference policy):
    inspect eval spirelink/bridge/inspect_task.py@slaybench_pilot \
        --model mockllm/model --solver greedy_solver

    # task options:  -T runs=10 -T character=SILENT -T hermetic=false

Design notes (read before changing):
- ONE game instance, structurally serialized: Task config sets max_connections=1, so
  Inspect's default max_samples=1 follows; a module-level lock (ownership-tracked) is
  a second line of defense, and a cross-process flock on the port (instance.py)
  guards against a second eval process.
- Budgets: a decision costs ~4 messages (observe: assistant+tool, decide:
  assistant+tool) plus retries/nudges — budget ~4.5 msgs/decision. Conversation
  growth is handled by react(truncation="auto"); each observe() is self-contained,
  so losing old history is safe by design.
- Limit-hit samples (message/token/time) are still scored (verified for inspect-ai
  0.3.239): the scorer reads live game state and scores progress-so-far.
  score_on_error=True additionally scores samples that ERROR (e.g. game crash),
  using progress persisted in the sample store by the observe tool.
- Token spend is bounded per-sample via token_limit — message limits alone do NOT
  bound spend (history is re-sent each call). Tune before expensive runs.
- `inspect view` shows the full decision transcript per sample. Expect large logs
  (multi-KB tool results x thousands of messages) on full runs.
"""
import asyncio
import atexit
import json
import os
import sys

import anyio

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from inspect_ai import Task, task
from inspect_ai.agent import react, AgentPrompt
from inspect_ai.dataset import Sample
from inspect_ai.model import GenerateConfig
from inspect_ai.scorer import Score, Target, accuracy, mean, scorer, stderr
from inspect_ai.solver import Generate, TaskState, solver
from inspect_ai.tool import ToolError, tool
from inspect_ai.util import store

from eval import choose as greedy_choose
from instance import GameInstance, TEMPLATE
from spire_cli import call as spire_call

PORT = int(os.environ.get("SPIRE_PORT", "5555"))

# One game instance. The lock is a defensive second line (max_connections=1 is the
# structural serialization); ownership is tracked per-sample via the store so a
# cancelled WAITER's cleanup can never release a lock the PLAYING sample holds.
_GAME_LOCK = anyio.Lock()
_LOCK_STORE_KEY = "spirelink:lock_held"
_INSTANCE = GameInstance(port=PORT)
atexit.register(_INSTANCE.stop)  # don't leak the game process / temp HOME at eval end


class GameUnreachable(Exception):
    pass


def _rpc(cmd: str, args: dict | None = None, timeout: float = 90) -> dict:
    """Harness-side RPC: raises plain exceptions (NOT ToolError)."""
    try:
        r = spire_call(cmd, args, port=PORT, timeout=timeout)
    except (OSError, ValueError) as e:
        raise GameUnreachable(f"{cmd}: game unreachable on port {PORT}: {e}") from e
    if not r.get("ok"):
        raise RuntimeError(f"{cmd}: {r.get('error')}")
    return r["data"]


async def _rpc_tool(cmd: str, args: dict | None = None, timeout: float = 90) -> dict:
    """Tool-side RPC: every failure becomes a ToolError the model can react to."""
    try:
        return await asyncio.to_thread(_rpc, cmd, args, timeout)
    except GameUnreachable as e:
        raise ToolError(f"{e} — the game may be mid-crash; wait a moment and retry observe()")
    except RuntimeError as e:
        raise ToolError(str(e))


# ---------------------------------------------------------------------------
# Tools — the agent's entire interface to the game
# ---------------------------------------------------------------------------

@tool
def observe():
    async def execute(wait_s: float = 25.0) -> str:
        """Observe the current game situation. Call this before every decision.

        Long-polls: if the game is busy animating, blocks up to wait_s seconds until a
        decision is pending or the run ends, so you rarely need to call it twice.

        The result is JSON:
          phase: "awaiting_decision" (act now) | "busy" (call observe again) |
                 "run_over" (the run ended; check run_summary, then submit)
          decision: {id, type, prompt, options[...], ...} — the pending decision;
                    every option carries resolved rules text
          state: full snapshot (your hp/gold/relics/potions, combat board with enemy
                 intents incl. total incoming damage, draw/discard/exhaust pile
                 contents, current event text, ...). Each observation is complete —
                 you never need to re-read old observations.
          run_summary: only when the run is over — victory/score/floor

        Args:
            wait_s: Max seconds to wait server-side while the game is busy
                (default 25, max 60).

        Returns:
            JSON string of the observation.
        """
        wait_s = max(0.0, min(float(wait_s), 60.0))
        data = await _rpc_tool("observe", {"wait_s": wait_s}, wait_s + 30.0)
        # Persist progress so the scorer can score even if the game dies later.
        try:
            st = data.get("state") or {}
            s = store()
            if st.get("in_run"):
                s.set("spirelink:floor", st.get("total_floor", 0))
                s.set("spirelink:actual_seed", st.get("seed"))
            s.set("spirelink:last_phase", data.get("phase"))
            if data.get("run_summary"):
                s.set("spirelink:run_summary", data["run_summary"])
        except Exception:
            pass
        return json.dumps(data)
    return execute


@tool
def decide():
    async def execute(decision_id: int, choice: dict[str, object]) -> str:
        """Answer the pending decision (get decision_id from observe()).

        Choice shapes by decision.type:
          combat:        {"action":"play_card","card_index":I,"target_index":J?} |
                         {"action":"use_potion","potion_index":I,"target_index":J?} |
                         {"action":"end_turn"}
          map:           {"coord":{"col":C,"row":R}}  (from one of the options)
          card_reward:   {"option_index":I} | {"skip":true} (when can_skip)
          combat_reward: {"option_index":I} (take one reward) | {"proceed":true}
          event:         {"option_index":I}
          shop:          {"buy_index":I} (one purchase; shop re-presents) | {"leave":true}
          rest:          {"option_index":I}
          treasure:      {"take":true} | {"skip":true}
          card_select:   {"indices":[I,...]} (min_select..max_select of them) |
                         {"skip":true} (only when min_select is 0)
          relic_select:  {"option_index":I}
          game_over:     {"ack":true}

        An INVALID choice raises a tool error listing the valid values, and the
        decision stays pending — read the error and decide() again. After a successful
        decide, call observe() to see the result.

        Args:
            decision_id: The id of the pending decision (from observe()).
            choice: The choice object, shaped per the decision type as above.

        Returns:
            JSON string {"accepted": true}.
        """
        data = await _rpc_tool(
            "decide", {"decision_id": int(decision_id), "choice": choice}, 90)
        try:
            s = store()
            s.set("spirelink:decisions", (s.get("spirelink:decisions") or 0) + 1)
        except Exception:
            pass
        return json.dumps(data)
    return execute


@tool
def view_map():
    async def execute() -> str:
        """Read the full act map: all nodes with type/coords/children, your position,
        and the reachable next nodes. Useful for route planning (rest sites before
        elites/boss, shops when rich, '?' = Unknown events).

        Returns:
            JSON string of the map.
        """
        return json.dumps(await _rpc_tool("get_map"))
    return execute


@tool
def view_deck():
    async def execute() -> str:
        """Read your full current deck with each card's resolved rules text.

        Returns:
            JSON string of the deck.
        """
        return json.dumps(await _rpc_tool("get_deck"))
    return execute


# ---------------------------------------------------------------------------
# Sample lifecycle
# ---------------------------------------------------------------------------

def _template_manifest() -> dict:
    try:
        with open(os.path.join(TEMPLATE, "MANIFEST.json")) as f:
            return json.load(f)
    except Exception:
        return {}


@solver
def game_setup(hermetic: bool = True):
    """Acquire the game, (hermetic) launch a fresh instance+HOME, start the sample's run."""
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        # Serialize samples. NOTE: Task-level GenerateConfig(max_connections=1) does
        # NOT reach Inspect's sample semaphore (verified on 0.3.239: explicit
        # --max-samples wins, else default-on adaptive concurrency applies) — so if
        # --max-samples 1 was forgotten, samples land here concurrently and QUEUE.
        # The generous timeout lets a forgotten flag still complete sequentially
        # (queued samples burn their time_limit while waiting — fine at pilot scale,
        # tighten your sample count or pass --max-samples 1 for full runs).
        try:
            with anyio.fail_after(45 * 60):
                await _GAME_LOCK.acquire()
        except TimeoutError:
            raise RuntimeError(
                "could not acquire the game after 45min (another sample is playing). "
                "This eval requires sequential samples: run with --max-samples 1.")
        state.store.set(_LOCK_STORE_KEY, True)

        meta = state.metadata or {}
        seed = meta.get("seed") or str(state.sample_id)
        character = meta.get("character", "IRONCLAD")
        if hermetic:
            manifest = _template_manifest()
            if not manifest.get("template_sha256"):
                raise RuntimeError(
                    "home_template/MANIFEST.json missing — re-run make_home_template.sh "
                    "(results are only comparable against a versioned template)")
            state.store.set("spirelink:template_sha256", manifest["template_sha256"])
            await asyncio.to_thread(_INSTANCE.fresh)
        else:
            # best effort: clear any active run, waiting out the async unwind
            obs = await asyncio.to_thread(_rpc, "observe")
            if obs["phase"] not in ("menu", "run_over"):
                await asyncio.to_thread(_rpc, "abandon_run")
                for _ in range(12):
                    await asyncio.sleep(2)
                    obs = await asyncio.to_thread(_rpc, "observe")
                    if obs["phase"] in ("menu", "run_over"):
                        break
        ping = await asyncio.to_thread(_rpc, "ping")
        state.store.set("spirelink:mod_version", ping.get("version"))
        state.store.set("spirelink:game_version", ping.get("game_version"))
        await asyncio.to_thread(_rpc, "start_run",
                                {"seed": seed, "character": character})
        state.store.set("spirelink:requested_seed", seed)
        return state
    return solve


async def game_cleanup(state: TaskState) -> None:
    """Runs after scoring (all paths incl. errors/limits): abandon any still-active
    run; release the game lock ONLY if this sample actually holds it."""
    if state.store.get(_LOCK_STORE_KEY):
        try:
            obs = await asyncio.to_thread(_rpc, "observe")
            if obs.get("state", {}).get("in_run") and obs.get("phase") != "run_over":
                await asyncio.to_thread(_rpc, "abandon_run")
        except Exception:
            pass  # instance may be gone; the next sample's fresh() handles it
        finally:
            state.store.set(_LOCK_STORE_KEY, False)
            _GAME_LOCK.release()


# ---------------------------------------------------------------------------
# Solvers
# ---------------------------------------------------------------------------

PLAY_INSTRUCTIONS = """\
You are playing Slay the Spire 2, a roguelike deck-building game, through tools.
You observe structured game state and make every player decision. This is STS2 —
content differs from the original game, so rely on the rules text provided in every
observation (cards, relics, potions, powers, enemy intents) rather than memory.

Core loop: observe() -> reason about the pending decision -> decide(...) -> repeat.
Each observation is complete; never re-read old ones.

Play to WIN the run (beat all acts). Guidance:
- Combat: enemy intents show TOTAL incoming damage across hits; block what you can't
  afford to take, race when the enemy isn't attacking. Check playable flags and costs.
- Deck: prefer a small, focused deck. Don't add weak cards (card rewards may be
  skippable); remove basic cards at shops when affordable.
- Map: plan routes with view_map() — rest before elites/bosses, fight monsters for
  rewards when healthy.
- Read event text carefully; weigh costs vs rewards.
- If decide() returns an error, the decision is still pending: read the error (it
  lists valid values) and submit a corrected choice.

The run ends when observe() returns phase "run_over" (victory or defeat). ONLY then
call submit() with a one-paragraph report: outcome, floor reached, and what shaped
the run. Never submit before the run is over.
"""

CONTINUE_NUDGE = (
    "The run is not over yet. Call observe() to see the current state, then decide(). "
    "Only call submit() after observe() shows phase \"run_over\"."
)


def spirelink_agent():
    return react(
        name="spirelink_player",
        description="Plays a full Slay the Spire 2 run via SpireLink tools",
        prompt=AgentPrompt(
            instructions=PLAY_INSTRUCTIONS,
            assistant_prompt=None,   # instructions are self-contained (incl. submit)
        ),
        tools=[observe(), decide(), view_map(), view_deck()],
        attempts=1,
        # Conversations grow ~1-1.5k tokens/decision; full runs are 300-1200
        # decisions. Old observations are dead weight (each is self-contained),
        # so trimming is safe and REQUIRED for deep runs.
        truncation="auto",
        on_continue=CONTINUE_NUDGE,
    )


@solver
def greedy_solver():
    """No-model baseline: the reference greedy policy plays via the same RPCs.

    Use with --model mockllm/model. Validates the full Inspect plumbing (setup ->
    play -> scoring -> logs) without API costs, and provides the baseline row.
    Decision count is capped to what a model agent could achieve under the task's
    message_limit (~limit/4), so baseline and model rows are budget-comparable.
    """
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        cap = (state.message_limit // 4) if state.message_limit else 5000
        decisions = 0
        while decisions < cap:
            obs = await asyncio.to_thread(_rpc, "observe", {"wait_s": 25}, 60)
            st = obs.get("state") or {}
            if st.get("in_run"):
                state.store.set("spirelink:floor", st.get("total_floor", 0))
                state.store.set("spirelink:actual_seed", st.get("seed"))
            if obs["phase"] == "run_over":
                break
            if obs["phase"] != "awaiting_decision":
                await asyncio.sleep(0.3)
                continue
            d = obs["decision"]
            try:
                ch = greedy_choose(d, obs["state"])
                await asyncio.to_thread(
                    _rpc, "decide", {"decision_id": d["id"], "choice": ch}, 90)
            except RuntimeError:
                # invalid choice: legal fallbacks, then give up on this decision
                for fb in ({"action": "end_turn"} if d["type"] == "combat"
                           else {"option_index": d["options"][0]["index"]},
                           {"proceed": True}, {"leave": True}):
                    try:
                        await asyncio.to_thread(
                            _rpc, "decide", {"decision_id": d["id"], "choice": fb}, 90)
                        break
                    except RuntimeError:
                        continue
            decisions += 1
            state.store.set("spirelink:decisions", decisions)
        return state
    return solve


# ---------------------------------------------------------------------------
# Scorer — reads the game's structured outcome, not the model's words
# ---------------------------------------------------------------------------

@scorer(metrics={
    "victory": [accuracy(), stderr()],
    "floor": [mean()],
    "decisions": [mean()],
    # NOTE: incomplete samples contribute score=0; interpret mean(score) only
    # alongside the victory/floor metrics (or filter to answer=="defeat"/"victory").
    "score": [mean()],
})
def run_outcome():
    async def score(state: TaskState, target: Target) -> Score:
        requested_seed = state.store.get("spirelink:requested_seed")
        decisions = state.store.get("spirelink:decisions") or 0

        # The agent's last decide may still be animating when the solver exits
        # (e.g. message limit right after the boss kill): long-poll + retry so a
        # just-finished run is not misscored as incomplete.
        obs = None
        for _ in range(3):
            try:
                obs = await asyncio.to_thread(_rpc, "observe", {"wait_s": 45}, 80)
                if obs.get("phase") != "busy":
                    break
            except Exception:
                await asyncio.sleep(5)
        if obs is None:
            # Game gone: score progress persisted by the tools during play.
            floor = state.store.get("spirelink:floor") or 0
            return Score(
                value={"victory": 0, "floor": floor, "score": 0,
                       "decisions": decisions},
                answer="unreachable",
                explanation=(f"game unreachable at scoring; last known floor {floor} "
                             f"(seed {requested_seed})"),
                metadata={"requested_seed": requested_seed},
            )

        summary = obs.get("run_summary") or state.store.get("spirelink:run_summary")
        meta = {"requested_seed": requested_seed,
                "template_sha256": state.store.get("spirelink:template_sha256"),
                "mod_version": state.store.get("spirelink:mod_version"),
                "game_version": state.store.get("spirelink:game_version")}

        if summary:
            actual = summary.get("seed")
            if requested_seed and actual and actual != requested_seed:
                # Tripwire for the silently-lost-seed bug class: the run that was
                # played is not the run that was requested — flag, don't average.
                return Score(
                    value={"victory": 0, "floor": 0, "score": 0,
                           "decisions": decisions},
                    answer="seed_mismatch",
                    explanation=(f"SEED MISMATCH: requested {requested_seed}, run used "
                                 f"{actual} — sample invalid for seed-comparable analysis"),
                    metadata={**meta, "run_summary": summary},
                )
            outcome = summary.get("outcome", summary.get("result", "unknown"))
            return Score(
                value={"victory": 1 if summary.get("victory") else 0,
                       "floor": summary.get("floor", 0),
                       "score": summary.get("score", 0),
                       "decisions": decisions},
                answer=outcome,
                explanation=(f"run over: {outcome}, act {summary.get('act')}, "
                             f"floor {summary.get('floor')}, score {summary.get('score')}, "
                             f"seed {actual}"),
                metadata={**meta, "run_summary": summary},
            )

        # Run still in progress: message/token/time limit hit, or early submit.
        st = obs.get("state", {})
        floor = st.get("total_floor", state.store.get("spirelink:floor") or 0)
        return Score(
            value={"victory": 0, "floor": floor, "score": 0, "decisions": decisions},
            answer="incomplete",
            explanation=(f"run not finished (phase {obs.get('phase')}); reached floor "
                         f"{floor} in {decisions} decisions (seed {st.get('seed')})"),
            metadata=meta,
        )
    return score


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def _dataset(runs: int, character: str, seed_prefix: str, seeds=None):
    seed_list = seeds if seeds else [f"{seed_prefix}{i + 1}" for i in range(runs)]
    return [
        Sample(
            id=seed,
            input=("Play this run of Slay the Spire 2 to the best of your ability. "
                   "The run has already been started for you — begin with observe()."),
            metadata={"seed": seed, "character": character},
        )
        for seed in seed_list
    ]


# max_connections=1 makes max_samples default to 1: samples are structurally
# sequential (queueing happens OUTSIDE per-sample time limits). Parallel tool
# calls are disabled: the game protocol is one-decision-at-a-time.
_CONFIG = GenerateConfig(max_connections=1, parallel_tool_calls=False)


@task
def slaybench(runs: int = 5, character: str = "IRONCLAD",
              seed_prefix: str = "INSPECT", hermetic: bool = True,
              seeds: list[str] | None = None) -> Task:
    """Full-run eval: play complete runs (all acts or death). ~300-1200 decisions/run.

    Budgets (per sample): message_limit 5000 ≈ 1100 decisions at ~4.5 msgs/decision;
    token_limit bounds SPEND (history is re-sent every call — message limits alone do
    not). Limit-hit samples are scored on progress (answer="incomplete").
    """
    return Task(
        dataset=_dataset(runs, character, seed_prefix, seeds),
        setup=game_setup(hermetic=hermetic),
        solver=spirelink_agent(),
        cleanup=game_cleanup,
        scorer=run_outcome(),
        config=_CONFIG,
        message_limit=5000,
        token_limit=40_000_000,
        time_limit=6 * 60 * 60,
        score_on_error=True,
        fail_on_error=0.5,
        version=1,
    )


@task
def slaybench_pilot(runs: int = 3, character: str = "IRONCLAD",
                    seed_prefix: str = "PILOT", hermetic: bool = True,
                    seeds: list[str] | None = None) -> Task:
    """Budget-capped pilot: ~100 decisions/run (≈ half of Act 1) at ~4.5 msgs/decision.

    Hitting the budget is expected: the score is floor/decisions progress, giving a
    cheap signal on play quality and per-decision cost before committing to full runs.
    """
    return Task(
        dataset=_dataset(runs, character, seed_prefix, seeds),
        setup=game_setup(hermetic=hermetic),
        solver=spirelink_agent(),
        cleanup=game_cleanup,
        scorer=run_outcome(),
        config=_CONFIG,
        message_limit=450,
        token_limit=5_000_000,
        time_limit=2 * 60 * 60,
        score_on_error=True,
        fail_on_error=0.5,
        version=1,
    )
