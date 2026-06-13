#!/usr/bin/env python3
"""Mock SpireLink game server.

Implements the SpireLink line-JSON protocol (see PROTOCOL.md) with a small but
coherent, *winnable* Slay-the-Spire-like run, so the MCP server / CLI / agent
play-loop can be developed and validated without the real (flaky-to-launch) game.

Run:   python3 mock_game.py [port]      (default 5555)
"""
import json
import socket
import socketserver
import sys

# ---- static content -------------------------------------------------------

CARDS = {
    "CARD.STRIKE_IRONCLAD": {"cost": 1, "type": "ATTACK", "target": "AnyEnemy", "dmg": 6,
                             "name": "Strike", "text": "Deal 6 damage."},
    "CARD.DEFEND_IRONCLAD": {"cost": 1, "type": "SKILL", "target": "Self", "block": 5,
                             "name": "Defend", "text": "Gain 5 Block."},
    "CARD.BASH":           {"cost": 2, "type": "ATTACK", "target": "AnyEnemy", "dmg": 8, "vuln": 2,
                            "name": "Bash", "text": "Deal 8 damage. Apply 2 Vulnerable."},
}
CARD_REWARD_POOL = ["CARD.ANGER", "CARD.CLEAVE", "CARD.IRON_WAVE"]
REWARD_CARDS = {
    "CARD.ANGER":     {"cost": 0, "type": "ATTACK", "target": "AnyEnemy", "dmg": 6,
                       "name": "Anger", "text": "Deal 6 damage. Add a copy to your discard pile."},
    "CARD.CLEAVE":    {"cost": 1, "type": "ATTACK", "target": "AllEnemies", "dmg": 8,
                       "name": "Cleave", "text": "Deal 8 damage to ALL enemies."},
    "CARD.IRON_WAVE": {"cost": 1, "type": "ATTACK", "target": "AnyEnemy", "dmg": 5, "block": 5,
                       "name": "Iron Wave", "text": "Gain 5 Block. Deal 5 damage."},
}
ALL_CARDS = {**CARDS, **REWARD_CARDS}


def card_view(cid, energy, in_combat=True):
    c = ALL_CARDS[cid]
    return {
        "id": cid, "name": c["name"], "cost": c["cost"], "cost_now": c["cost"],
        "upgrade": 0, "type": c["type"], "target": c["target"],
        "playable": (not in_combat) or c["cost"] <= energy,
        "text": c["text"],
    }


# ---- a node in the act map ------------------------------------------------

class Node:
    def __init__(self, col, row, kind):
        self.col, self.row, self.kind = col, row, kind  # kind: Monster/Event/Shop/RestSite/Treasure/Boss
        self.children = []


class Enemy:
    def __init__(self, name, hp):
        self.name, self.hp, self.max_hp, self.block = name, hp, hp, 0
        self.vuln = 0
        self.turn = 0
    def intent(self):
        # Same shape as the mod: objects with type + computed damage for attacks.
        if self.turn % 2 == 0:
            return [{"type": "Attack", "title": "Attack", "damage": 11, "hits": 1,
                     "text": "This enemy intends to attack for 11 damage."}]
        return [{"type": "Defend", "title": "Defend",
                 "text": "This enemy intends to gain Block."}]
    def view(self):
        return {"name": self.name, "hp": self.hp, "max_hp": self.max_hp, "block": self.block,
                "alive": self.hp > 0,
                "powers": ([{"id": "POWER.VULNERABLE", "amount": self.vuln,
                             "text": "Takes 50% more damage from attacks."}] if self.vuln else []),
                "intent": self.intent()}


# ---- the game state machine ----------------------------------------------

class Game:
    def __init__(self):
        self.phase = "menu"          # menu | awaiting_decision | busy | run_over
        self.last_result = None      # None | completed | failed | abandoned
        self.run_summary = None
        self.decision = None
        self._dec_id = 0
        self.character = "CHARACTER.IRONCLAD"
        self.hp = 70
        self.max_hp = 70
        self.block = 0
        self.gold = 99
        self.deck = ["CARD.STRIKE_IRONCLAD"] * 5 + ["CARD.DEFEND_IRONCLAD"] * 4 + ["CARD.BASH"]
        self.relics = ["RELIC.BURNING_BLOOD"]
        self.potions = []
        self.max_potion_slots = 3
        self.act = 0
        self.act_floor = 0
        self.total_floor = 0
        self.current_room = None
        self.nodes = []
        self.current = None          # (col,row)
        self.combat = None
        self.won = False

    # --- map generation: 3 floors, branching, ending in a boss ---
    def _build_map(self):
        r0 = [Node(0, 0, "Monster"), Node(1, 0, "Event")]
        r1 = [Node(0, 1, "RestSite"), Node(1, 1, "Shop"), Node(2, 1, "Treasure")]
        boss = Node(1, 2, "Boss")
        r0[0].children = [r1[0], r1[1]]
        r0[1].children = [r1[1], r1[2]]
        for n in r1:
            n.children = [boss]
        self.nodes = r0 + r1 + [boss]
        self.current = None

    def _node_at(self, col, row):
        for n in self.nodes:
            if n.col == col and n.row == row:
                return n
        return None

    def _reachable(self):
        if self.current is None:
            return [n for n in self.nodes if n.row == 0]
        cur = self._node_at(*self.current)
        return cur.children if cur else []

    # --- decision construction ---
    def _new_decision(self, dtype, prompt, options, **extra):
        self._dec_id += 1
        self.decision = {"id": self._dec_id, "type": dtype, "prompt": prompt, "options": options}
        self.decision.update(extra)
        self.phase = "awaiting_decision"

    def _map_decision(self):
        nxt = self._reachable()
        if not nxt:
            return self._win()
        # Same option shape as the real mod: index/label/coord only (no mock-only
        # extras like reachable_next — clients must not depend on fields the mod lacks).
        opts = [{"index": i, "label": n.kind,
                 "coord": {"col": n.col, "row": n.row}} for i, n in enumerate(nxt)]
        self._new_decision("map", "Choose the next room to enter.", opts)

    # --- snapshots ---
    def state(self):
        st = {
            "in_run": self.phase != "menu",
            "act": self.act, "act_floor": self.act_floor, "total_floor": self.total_floor,
            "game_mode": "Standard", "ascension": 0, "game_over": self.phase == "run_over",
            "current_room": self.current_room,
            "players": [{
                "character": self.character, "hp": self.hp, "max_hp": self.max_hp,
                "block": self.block, "gold": self.gold, "max_potion_slots": self.max_potion_slots,
                "deck_count": len(self.deck), "relics": list(self.relics), "potions": list(self.potions),
            }],
            "combat": self._combat_view(),
        }
        return st

    def _combat_view(self):
        c = self.combat
        if not c:
            return None
        return {
            "round": c["round"], "play_phase": True,
            "energy": c["energy"], "max_energy": 3,
            "hand": [card_view(cid, c["energy"]) for cid in c["hand"]],
            "draw_count": len(c["draw"]), "discard_count": len(c["discard"]), "exhaust_count": 0,
            "player_powers": [],
            "enemies": [e.view() for e in c["enemies"]],
        }

    def observe(self):
        o = {"phase": self.phase, "decision": self.decision, "state": self.state()}
        if self.last_result:
            o["last_result"] = self.last_result
        if self.run_summary:
            o["run_summary"] = self.run_summary
        return o

    # --- lifecycle ---
    def start_run(self, character=None, ascension=0, seed=None):
        self.last_result = None
        self.run_summary = None
        self.won = False
        if character:
            self.character = character if character.startswith("CHARACTER.") else "CHARACTER." + character.upper()
        self._build_map()
        self.act_floor = 0
        self._map_decision()
        return {"started": True}

    def abandon_run(self):
        if self.phase == "menu":
            raise ValueError("no active run to abandon")
        self.decision = None
        self.combat = None
        self.phase = "run_over"
        self.last_result = "abandoned"
        self.run_summary = {"result": "abandoned", "victory": False, "defeat": False,
                            "outcome": "abandoned",
                            "act": self.act, "floor": self.total_floor,
                            "hp": self.hp, "max_hp": self.max_hp, "gold": self.gold,
                            "character": self.character, "deck": list(self.deck),
                            "relics": list(self.relics)}
        return {"abandoned": True}

    def _enter_room(self, node):
        self.current = (node.col, node.row)
        self.act_floor += 1
        self.total_floor += 1
        self.current_room = node.kind
        if node.kind in ("Monster", "Boss"):
            self._start_combat(boss=(node.kind == "Boss"))
        elif node.kind == "Event":
            self._new_decision("event",
                "A cloaked figure offers you a deal: lose 6 HP to gain 50 gold?",
                [{"index": 0, "label": "Accept (-6 HP, +50 gold)", "detail": ""},
                 {"index": 1, "label": "Refuse", "detail": ""}],
                body="The figure waits, coins glinting.")
        elif node.kind == "Shop":
            self.shop_stock = [
                {"index": 0, "label": "CARD.CLEAVE", "detail": "card", "cost": 50, "kind": "card", "id": "CARD.CLEAVE"},
                {"index": 1, "label": "RELIC.LANTERN", "detail": "relic", "cost": 70, "kind": "relic", "id": "RELIC.LANTERN"},
                {"index": 2, "label": "POTION.FIRE", "detail": "potion", "cost": 30, "kind": "potion", "id": "POTION.FIRE"},
            ]
            self._shop_decision()
        elif node.kind == "RestSite":
            self._new_decision("rest", "A rest site. Choose an action.",
                [{"index": 0, "label": "Rest", "detail": "heal 30% max HP"},
                 {"index": 1, "label": "Smith", "detail": "upgrade a card"}])
        elif node.kind == "Treasure":
            self._new_decision("treasure", "A treasure chest sits before you.",
                [{"index": 0, "label": "Take RELIC.ORICHALCUM", "detail": "relic"},
                 {"index": 1, "label": "Leave", "detail": ""}],
                relic="RELIC.ORICHALCUM")

    def _shop_decision(self):
        opts = ([{**s, "affordable": self.gold >= s["cost"]} for s in self.shop_stock]
                + [{"index": 100, "label": "Leave"}])
        self._new_decision("shop", f"Merchant. You have {self.gold} gold.", opts, gold=self.gold)

    # --- combat ---
    def _start_combat(self, boss=False):
        enemies = [Enemy("Ascended Guardian" if boss else "Jaw Worm", 60 if boss else 40)]
        draw = list(self.deck)
        self.combat = {"round": 1, "energy": 3, "hand": [], "draw": draw, "discard": [],
                       "enemies": enemies, "boss": boss}
        self.block = 0
        self._draw_hand()
        self._combat_decision()

    def _draw_hand(self):
        c = self.combat
        c["energy"] = 3
        self.block = 0
        for e in c["enemies"]:
            e.block = 0
        # reshuffle if needed
        if len(c["draw"]) < 5:
            c["draw"] += c["discard"]
            c["discard"] = []
        c["hand"] = c["draw"][:5]
        c["draw"] = c["draw"][5:]

    def _combat_decision(self):
        c = self.combat
        # Same option/extra shape as the real mod: play_card options carry
        # cost/type/target/playable/needs_target; the only extra is "targets".
        opts = []
        for i, cid in enumerate(c["hand"]):
            cd = ALL_CARDS[cid]
            playable = cd["cost"] <= c["energy"]
            opts.append({"index": i, "action": "play_card", "card_index": i,
                         "label": cid, "cost": cd["cost"], "type": cd["type"],
                         "target": cd["target"], "needs_target": cd["target"] == "AnyEnemy",
                         "playable": playable})
        opts.append({"index": 100, "action": "end_turn", "label": "End turn"})
        living = [e for e in c["enemies"] if e.hp > 0]
        targets = [{"target_index": i, "name": e.name, "hp": e.hp, "block": e.block,
                    "intent": e.intent()}
                   for i, e in enumerate(living)]
        self._new_decision("combat",
            f"Your turn. Energy {c['energy']}/3. Pick an action.",
            opts, targets=targets)

    def _combat_play(self, choice):
        c = self.combat
        action = choice.get("action")
        if action == "end_turn":
            return self._enemy_turn()
        if action == "play_card":
            idx = choice.get("card_index")
            if idx is None or idx < 0 or idx >= len(c["hand"]):
                raise ValueError("bad card_index")
            cid = c["hand"][idx]
            cd = ALL_CARDS[cid]
            if cd["cost"] > c["energy"]:
                raise ValueError("not enough energy")
            c["energy"] -= cd["cost"]
            # apply effects
            living = [e for e in c["enemies"] if e.hp > 0]
            if "block" in cd:
                self.block += cd["block"]
            if "dmg" in cd:
                targets = living if cd["target"] == "AllEnemies" else []
                if cd["target"] == "AnyEnemy":
                    ti = choice.get("target_index", 0)
                    targets = [living[ti]] if 0 <= ti < len(living) else living[:1]
                for e in targets:
                    dmg = cd["dmg"] + (round(cd["dmg"] * 0.5) if e.vuln else 0)
                    e.hp = max(0, e.hp - dmg)
                if "vuln" in cd and targets:
                    targets[0].vuln += cd["vuln"]
            # move card to discard
            c["discard"].append(c["hand"].pop(idx))
            # win?
            if all(e.hp <= 0 for e in c["enemies"]):
                return self._combat_won()
            return self._combat_decision()
        raise ValueError("unknown combat action")

    def _enemy_turn(self):
        c = self.combat
        for e in c["enemies"]:
            if e.hp <= 0:
                continue
            if e.intent() == ["Attack"]:
                dmg = 11
                absorbed = min(self.block, dmg)
                self.block -= absorbed
                self.hp = max(0, self.hp - (dmg - absorbed))
            else:
                e.block += 8
            e.turn += 1
            if e.vuln > 0:
                e.vuln -= 1
        if self.hp <= 0:
            return self._lose()
        c["round"] += 1
        c["discard"] += c["hand"]
        c["hand"] = []
        self._draw_hand()
        return self._combat_decision()

    def _combat_won(self):
        boss = self.combat.get("boss")
        self.combat = None
        self.current_room = None
        if boss:
            return self._win()
        # combat reward: gold then card reward
        # Mod shape: reward options by index, proceed at index 100.
        self._new_decision("combat_reward", "Take a reward, or proceed.",
            [{"index": 0, "label": "Gold (+25)"},
             {"index": 100, "label": "Proceed (done taking rewards)"}])

    def _card_reward_decision(self):
        opts = [{"index": i, "label": cid, "detail": f"{REWARD_CARDS[cid]['type']}"}
                for i, cid in enumerate(CARD_REWARD_POOL)]
        self._new_decision("card_reward", "Choose a card to add to your deck (or skip).", opts, can_skip=True)

    def _win(self):
        self.won = True
        self._new_decision("game_over", "You won the run! 🎉",
            [{"index": 0, "label": "Acknowledge", "detail": ""}], won=True)

    def _lose(self):
        self.combat = None
        self._new_decision("game_over", "You died. Run over.",
            [{"index": 0, "label": "Acknowledge", "detail": ""}], won=False)

    # --- choice validation: reject invalid input, keep the decision pending ---
    def _validate(self, dec, choice):
        dtype = dec["type"]
        idxs = {o["index"] for o in dec.get("options", [])}

        def need_index(field="option_index"):
            i = choice.get(field, choice.get("option_index"))
            if i is None or i not in idxs:
                raise ValueError(f"invalid {field} {i} (valid: {sorted(idxs)})")

        if dtype == "combat":
            action = choice.get("action")
            if action == "end_turn":
                return
            if action == "play_card":
                cis = {o["card_index"] for o in dec["options"] if o.get("action") == "play_card"}
                ci = choice.get("card_index")
                if ci not in cis:
                    raise ValueError(f"invalid card_index {ci} (hand indices: {sorted(cis)})")
                playable = {o["card_index"] for o in dec["options"]
                            if o.get("action") == "play_card" and o.get("playable")}
                if ci not in playable:
                    raise ValueError(f"card {ci} is not playable right now")
                return
            raise ValueError(f"unknown combat action '{action}'")
        if dtype == "map":
            co = choice.get("coord")
            if co is not None:
                coords = {(o["coord"]["col"], o["coord"]["row"]) for o in dec["options"]}
                if (co.get("col"), co.get("row")) not in coords:
                    raise ValueError(f"coord {co} is not a reachable next room")
                return
            need_index()
            return
        if dtype == "card_reward":
            if choice.get("skip"):
                return
            need_index()
            return
        if dtype == "shop":
            if choice.get("leave"):
                return
            need_index("buy_index")
            bi = choice.get("buy_index", choice.get("option_index"))
            for o in dec.get("options", []):
                if o["index"] == bi and o.get("affordable") is False:
                    raise ValueError(f"cannot afford item {bi} (cost {o.get('cost')})")
            return
        if dtype == "treasure":
            if choice.get("take") or choice.get("skip"):
                return
            need_index()
            return
        if dtype == "combat_reward":
            if choice.get("proceed"):
                return
            need_index()
            return
        if dtype == "game_over":
            return
        need_index()

    # --- apply a client decision ---
    def decide(self, decision_id, choice):
        if self.decision is None:
            raise ValueError("no pending decision")
        if decision_id != self.decision["id"]:
            raise ValueError(f"stale decision_id (expected {self.decision['id']})")
        dtype = self.decision["type"]
        self._validate(self.decision, choice)  # raises BEFORE clearing: decision stays pending
        self.decision = None

        if dtype == "map":
            coord = choice.get("coord")
            nxt = self._reachable()
            target = None
            if coord:
                target = next((n for n in nxt if n.col == coord.get("col") and n.row == coord.get("row")), None)
            elif "option_index" in choice:
                i = choice["option_index"]
                target = nxt[i] if 0 <= i < len(nxt) else None
            if target is None:
                raise ValueError("invalid map choice")
            self._enter_room(target)

        elif dtype == "combat":
            self._combat_play(choice)

        elif dtype == "combat_reward":
            if choice.get("proceed") or choice.get("option_index") == 1:
                self._card_reward_decision()
            else:
                self.gold += 25
                # let them proceed next
                self._new_decision("combat_reward", "Rewards (gold taken). Proceed?",
                    [{"index": 1, "label": "Proceed", "detail": "", "kind": "proceed"}])

        elif dtype == "card_reward":
            if not choice.get("skip"):
                i = choice.get("option_index", 0)
                if 0 <= i < len(CARD_REWARD_POOL):
                    self.deck.append(CARD_REWARD_POOL[i])
            self._map_decision()

        elif dtype == "event":
            i = choice.get("option_index", 1)
            if i == 0:
                self.hp = max(1, self.hp - 6)
                self.gold += 50
            self._map_decision()

        elif dtype == "shop":
            if choice.get("leave") or choice.get("buy_index") == 100:
                self._map_decision()
            else:
                bi = choice.get("buy_index")
                item = next((s for s in self.shop_stock if s["index"] == bi), None)
                if item and self.gold >= item["cost"]:
                    self.gold -= item["cost"]
                    if item["kind"] == "card":
                        self.deck.append(item["id"])
                    elif item["kind"] == "relic":
                        self.relics.append(item["id"])
                    elif item["kind"] == "potion":
                        self.potions.append(item["id"])
                    self.shop_stock = [s for s in self.shop_stock if s["index"] != bi]
                self._shop_decision()

        elif dtype == "rest":
            i = choice.get("option_index", 0)
            if i == 0:
                self.hp = min(self.max_hp, self.hp + round(self.max_hp * 0.3))
            # (smith upgrade is a no-op in the mock)
            self._map_decision()

        elif dtype == "treasure":
            if choice.get("take") or choice.get("option_index") == 0:
                self.relics.append(self.decision_relic if False else "RELIC.ORICHALCUM")
            self._map_decision()

        elif dtype == "game_over":
            self.phase = "run_over"
            self.last_result = "completed" if self.won else "failed"
            self.run_summary = {
                "result": self.last_result, "victory": self.won, "defeat": not self.won,
                "outcome": "victory" if self.won else "defeat",
                "act": self.act, "floor": self.total_floor,
                "score": self.total_floor * 10 + (100 if self.won else 0),
                "hp": self.hp, "max_hp": self.max_hp, "gold": self.gold,
                "character": self.character, "deck": list(self.deck),
                "relics": list(self.relics),
            }
            self.decision = None

        else:
            raise ValueError(f"unhandled decision type {dtype}")

        return {"accepted": True}


# ---- TCP server -----------------------------------------------------------

GAME = Game()


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        peer = self.client_address
        print(f"[mock] client connected {peer}", flush=True)
        for raw in self.rfile:
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                rid = req.get("id", 0)
                data = self.dispatch(req.get("cmd"), req.get("args") or {})
                resp = {"id": rid, "ok": True, "data": data}
            except Exception as e:  # noqa
                resp = {"id": req.get("id", 0) if "req" in dir() else 0, "ok": False, "error": str(e)}
            self.wfile.write((json.dumps(resp) + "\n").encode("utf-8"))
            self.wfile.flush()
        print(f"[mock] client disconnected {peer}", flush=True)

    def dispatch(self, cmd, args):
        if cmd == "ping":
            return {"pong": True, "version": "mock-0.1", "in_run": GAME.phase != "menu",
                    "in_combat": GAME.combat is not None}
        if cmd == "start_run":
            return GAME.start_run(args.get("character"), args.get("ascension", 0), args.get("seed"))
        if cmd == "abandon_run":
            return GAME.abandon_run()
        if cmd == "observe":
            return GAME.observe()
        if cmd == "decide":
            return GAME.decide(args.get("decision_id"), args.get("choice") or {})
        if cmd == "get_state":
            return GAME.state()
        if cmd == "get_deck":
            return {"in_run": GAME.phase != "menu",
                    "deck": [card_view(c, 0, in_combat=False) for c in GAME.deck]}
        if cmd == "get_map":
            return {"act": GAME.act, "act_floor": GAME.act_floor,
                    "nodes": [{"coord": {"col": n.col, "row": n.row}, "type": n.kind,
                               "children": [{"col": c.col, "row": c.row} for c in n.children]}
                              for n in GAME.nodes],
                    "current": ({"col": GAME.current[0], "row": GAME.current[1]} if GAME.current else None),
                    "reachable_next": [{"col": n.col, "row": n.row} for n in GAME._reachable()]}
        raise ValueError(f"unknown cmd: {cmd}")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5555
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), Handler) as srv:
        print(f"[mock] SpireLink mock listening on 127.0.0.1:{port}", flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()
