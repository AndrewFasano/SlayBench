# SpireLink protocol (v1)

Line-delimited JSON over TCP (`127.0.0.1:5555`). One JSON object per line, both directions.

```
request:  {"id": <int>, "cmd": "<name>", "args": { ... }}\n
response: {"id": <int>, "ok": true,  "data": { ... }}\n
       or {"id": <int>, "ok": false, "error": "<msg>"}\n
```

The same protocol is served by two implementations:
- **the in-game mod** (`spirelink.dll`) — drives the real game; and
- **the mock** (`bridge/mock_game.py`) — simulates a minimal run for offline testing.

The MCP server (`bridge/mcp_server.py`) is a *client* of this protocol and re-exposes it as MCP tools.

## Interaction model

The game is driven by the connected client. The mod runs the real game loop and, at every
player decision, **pauses and exposes a single pending decision**; the client answers it with
`decide`, and the game proceeds. This guarantees the client makes *every* relevant decision.

There is **no decision timeout**: the client may think for minutes on a pending decision
(the mod suppresses AutoSlayer's QA watchdog / room-screen timeouts while a decision is
pending). Use `abandon_run` to escape a run you no longer want to continue.

The client loop is:

```
observe()  ->  if phase == "awaiting_decision": reason about decision, decide(...)
           ->  if phase == "busy": poll observe() again
           ->  if phase == "menu": start_run()
           ->  if phase == "run_over": read last_result (and last_error); done,
               or call start_run() again to begin a new run
```

## Commands

### `ping`
`data`: `{pong, version, in_run, in_combat}`

### `start_run`  (from menu)
`args`: `{character?: str, ascension?: int, seed?: str}`
`data`: `{started: true}`

Note: `ascension` is accepted but **not yet honored by the real mod** (mock only).

### `abandon_run`
Abort the active run (escape hatch for stuck states or restarts). The run ends with
`last_result: "abandoned"`; a new `start_run` is then valid.
`data`: `{abandoned: true}`

### `observe`
`args` (optional): `{wait_s?: number}` — long-poll: block server-side up to `wait_s`
seconds while phase is `busy`, returning as soon as a decision is pending / the run
ends / the menu is reached. Eliminates client poll-spins during animations.

`data`:
```jsonc
{
  "phase": "menu" | "awaiting_decision" | "busy" | "run_over",
  "last_result": "completed" | "failed" | "abandoned",   // present once a run has ended
  "last_error": "<msg>",                                  // present if the run failed with an error
  "run_summary": {                                        // present once a run has ended
    "result": "completed"|"failed"|"abandoned",
    "outcome": "victory"|"defeat"|"abandoned"|"error",     // defeat=died in play; error=harness/game fault
    "victory": bool, "defeat": bool, "score": int,
    "seed": str,                                           // the ACTUAL run seed (verify it!)
    "act": int, "floor": int, "hp": int, "max_hp": int, "gold": int,
    "character": str, "deck": [str...], "relics": [str...]
  },
  "decision": null | {
     "id": <int>,                 // echo back in decide
     "type": "map"|"combat"|"card_reward"|"combat_reward"|"event"|
             "shop"|"rest"|"treasure"|"card_select"|"relic_select"|"game_over",
     "prompt": "<human-readable>",
     "options": [ {"index": <int>, "label": "<str>", "detail": "<str>", ...} ],
     // type-specific context is also included inline (e.g. event body, shop prices)
  },
  "state": { ...full readable snapshot (see below)... }
}
```

### `decide`
`args`: `{decision_id: <int>, choice: <object>}` — `choice` shape per decision type.

**Validation:** an invalid choice (bad index, unplayable card, unreachable coord,
out-of-range target for a card *or potion*, an unaffordable shop purchase, skip where
skipping isn't allowed…) returns `ok:false` with a message listing the valid values,
and **the decision remains pending** — retry with a corrected choice. Invalid input is
never silently coerced.

| decision type   | choice                                                            |
|-----------------|-------------------------------------------------------------------|
| `combat`        | `{action:"play_card", card_index, target_index?}` · `{action:"use_potion", potion_index, target_index?}` · `{action:"end_turn"}` |
| `map`           | `{coord:{col,row}}`  (one of `reachable_next`)                     |
| `card_reward`   | `{option_index}` or `{skip:true}`                                  |
| `combat_reward` | `{option_index}` (take one) or `{proceed:true}`                   |
| `event`         | `{option_index}`                                                  |
| `shop`          | `{buy_index}` (one purchase) or `{leave:true}`                     |
| `rest`          | `{option_index}`                                                  |
| `treasure`      | `{take:true}` or `{skip:true}`                                     |
| `card_select`   | `{indices:[...]}` (choose `min_select`..`max_select`) or `{skip:true}` (only valid when `min_select` is 0) |
| `relic_select`  | `{option_index}`                                                  |
| `game_over`     | `{ack:true}`                                                      |

`data`: `{accepted: true}`. After a successful `decide`, call `observe` again.

### Read-only queries (always available)
- `get_state` → the `state` object below
- `get_deck`  → `{in_run, deck:[card...]}`
- `get_map`   → `{act, act_floor, nodes:[...], current, reachable_next:[...]}`

## State snapshot

Everything a human player can see is included: every card / relic / potion / power / orb
carries its **resolved rules text** (`name`, `text`), and enemy intents carry **computed
damage numbers** — STS2 content is new, so clients must not rely on STS1 knowledge.

```jsonc
{
  "in_run": bool,
  "seed": str,                                           // the actual run seed
  "act": int, "act_floor": int, "total_floor": int,
  "game_mode": str, "ascension": int, "game_over": bool,
  "current_room": str|null,
  "event": null | {"id":str, "title":str, "body":str},   // current event page, in event rooms
  "players": [ {
     "character": str, "hp": int, "max_hp": int, "block": int,
     "gold": int, "max_potion_slots": int, "deck_count": int,
     "relics":  [ {"id":str,"name":str,"text":str} ],
     "potions": [ {"id":str,"name":str,"text":str,"target":str} ]
  } ],
  "combat": null | {
     "round": int, "play_phase": bool, "energy": int, "max_energy": int,
     "player_block": int,
     "hand": [ {"id","name","cost","cost_now","upgrade","type","target","rarity","playable","text"} ],
     "draw_pile":    [ {"id","name","cost","upgrade","type"} ],   // contents, compact
     "discard_pile": [ ... ], "exhaust_pile": [ ... ],
     "draw_count": int, "discard_count": int, "exhaust_count": int,
     "player_powers": [ {"id":str,"amount":int,"text":str} ],
     "orbs": [ {"id","name","text","passive","evoke"} ],  // + "orb_capacity", when in use
     "enemies": [ {"name":str,"hp":int,"max_hp":int,"block":int,"alive":bool,
                   "powers":[{"id","amount","text"}],
                   "intent":[ {"type":str, "title":str, "text":str,
                               "damage":int?,   // TOTAL damage across all hits
                               "hits":int?} ]} ]
  }
}
```

Decision options are similarly enriched: combat `play_card` options carry `name`/`text`;
`card_reward` / `card_select` options are full card objects (`card_select` adds
`upgraded_text` — the resolved post-upgrade rules text); `shop` options carry the item's
rules text plus `cost`/`affordable` (including the `CARD_REMOVAL` service, whose purchase
opens a `card_select`); `event` decisions carry the event `title`/`body` and per-option
`detail`; `treasure` carries `relic_info`; `relic_select` options carry relic text.

Card ids look like `CARD.STRIKE_IRONCLAD`; relics `RELIC.BURNING_BLOOD`; etc.
