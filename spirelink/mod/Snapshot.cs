using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json.Nodes;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.MonsterMoves.Intents;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;

namespace SpireLink
{
    // Read-only serialization of live game state into JSON. Runs on the main thread.
    //
    // Design rule: expose everything a human player can see. STS2 content is new, so an
    // agent cannot rely on memorized STS1 knowledge — every card/relic/potion/power/orb
    // carries its resolved rules text, and enemy intents carry computed damage numbers.
    internal static class Snapshot
    {
        // Strip Godot BBCode markup from rules text. [img]path[/img] blocks (inline icons,
        // e.g. the energy symbol) are removed wholly — their payload is a resource path,
        // not text. Then remaining [tag]/[/tag] wrappers are stripped, keeping inner text.
        private static readonly System.Text.RegularExpressions.Regex _imgBlock =
            new System.Text.RegularExpressions.Regex(@"\[img[^\]]*\].*?\[/img\]",
                System.Text.RegularExpressions.RegexOptions.Singleline);
        private static readonly System.Text.RegularExpressions.Regex _bbcode =
            new System.Text.RegularExpressions.Regex(@"\[/?[a-zA-Z][^\[\]]*\]");

        public static string CleanText(string s)
        {
            if (string.IsNullOrEmpty(s)) return null;
            s = _imgBlock.Replace(s, "");
            s = _bbcode.Replace(s, "").Trim();
            return s.Length == 0 ? null : s;
        }

        private static string Txt(Func<string> f)
        {
            try { return CleanText(f()); } catch { return null; }
        }

        // ---- top-level state ----
        public static JsonNode GetState()
        {
            var root = new JsonObject();
            var run = RunManager.Instance?.DebugOnlyGetState();
            if (run == null)
            {
                root["in_run"] = false;
                return root;
            }
            root["in_run"] = true;
            try { root["seed"] = run.Rng.StringSeed; } catch { }
            root["act"] = run.CurrentActIndex;
            root["act_floor"] = run.ActFloor;
            root["total_floor"] = run.TotalFloor;
            root["game_mode"] = run.GameMode.ToString();
            root["ascension"] = run.AscensionLevel;
            root["game_over"] = run.IsGameOver;
            try { root["current_room"] = run.CurrentRoom?.RoomType.ToString(); } catch { }

            var players = new JsonArray();
            foreach (var p in run.Players)
                players.Add(PlayerJson(p));
            root["players"] = players;

            try { root["event"] = EventJson(run); } catch { }
            root["combat"] = CombatJson(run);
            return root;
        }

        // ---- event context (title + current page body), when in an event room ----
        public static JsonNode EventJson(RunState run)
        {
            if (!(run.CurrentRoom is EventRoom er)) return null;
            EventModel ev = null;
            try { ev = er.LocalMutableEvent; } catch { }
            if (ev == null) { try { ev = er.CanonicalEvent; } catch { } }
            if (ev == null) return null;
            var o = new JsonObject();
            try { o["id"] = ev.Id.ToString(); } catch { }
            o["title"] = Txt(() => ev.Title.GetFormattedText());
            o["body"] = Txt(() => (ev.Description ?? ev.InitialDescription)?.GetFormattedText());
            return o;
        }

        private static JsonNode PlayerJson(object playerObj)
        {
            var p = (MegaCrit.Sts2.Core.Entities.Players.Player)playerObj;
            var o = new JsonObject();
            try { o["character"] = p.Character.Id.ToString(); } catch { }
            try { o["hp"] = p.Creature.CurrentHp; } catch { }
            try { o["max_hp"] = p.Creature.MaxHp; } catch { }
            try { o["block"] = p.Creature.Block; } catch { }
            try { o["gold"] = p.Gold; } catch { }
            try { o["max_potion_slots"] = p.MaxPotionCount; } catch { }
            try { o["deck_count"] = p.Deck.Cards.Count; } catch { }

            var relics = new JsonArray();
            try { foreach (var r in p.Relics) relics.Add(RelicJson(r)); } catch { }
            o["relics"] = relics;

            var potions = new JsonArray();
            try { foreach (var pot in p.Potions) potions.Add(PotionJson(pot)); } catch { }
            o["potions"] = potions;
            return o;
        }

        public static JsonNode RelicJson(RelicModel r)
        {
            var o = new JsonObject();
            try { o["id"] = r.Id.ToString(); } catch { }
            o["name"] = Txt(() => r.Title.GetFormattedText());
            o["text"] = Txt(() => r.DynamicEventDescription.GetFormattedText());
            return o;
        }

        public static JsonNode PotionJson(PotionModel pot)
        {
            var o = new JsonObject();
            try { o["id"] = pot.Id.ToString(); } catch { }
            o["name"] = Txt(() => pot.Title.GetFormattedText());
            o["text"] = Txt(() => pot.DynamicDescription.GetFormattedText());
            try { o["target"] = pot.TargetType.ToString(); } catch { }
            return o;
        }

        // ---- combat ----
        private static JsonNode CombatJson(RunState run)
        {
            var cm = CombatManager.Instance;
            if (cm == null || !cm.IsInProgress) return null;
            var cs = cm.DebugOnlyGetState();
            if (cs == null) return null;

            var o = new JsonObject();
            try { o["round"] = cs.RoundNumber; } catch { }
            try { o["play_phase"] = cm.IsPlayPhase; } catch { }

            // "me" = first player with a combat state
            var me = run.Players.FirstOrDefault(pl => pl.PlayerCombatState != null) ?? run.Players.FirstOrDefault();
            if (me != null)
            {
                var pcs = me.PlayerCombatState;
                if (pcs != null)
                {
                    try { o["energy"] = pcs.Energy; } catch { }
                    try { o["max_energy"] = pcs.MaxEnergy; } catch { }
                    o["hand"] = CardListJson(pcs.Hand?.Cards, full: true);
                    // Pile contents, like a human inspecting the piles (compact: no rules
                    // text per card; full text is available on the same ids in hand/deck).
                    try { o["draw_pile"] = CardListJson(pcs.DrawPile?.Cards, full: false); } catch { }
                    try { o["discard_pile"] = CardListJson(pcs.DiscardPile?.Cards, full: false); } catch { }
                    try { o["exhaust_pile"] = CardListJson(pcs.ExhaustPile?.Cards, full: false); } catch { }
                    try { o["draw_count"] = pcs.DrawPile.Cards.Count; } catch { }
                    try { o["discard_count"] = pcs.DiscardPile.Cards.Count; } catch { }
                    try { o["exhaust_count"] = pcs.ExhaustPile.Cards.Count; } catch { }
                    try
                    {
                        // Orb-style mechanics (queue + capacity), present only when in use.
                        var oq = pcs.OrbQueue;
                        if (oq != null && (oq.Capacity > 0 || oq.Orbs.Count > 0))
                        {
                            var orbs = new JsonArray();
                            foreach (var orb in oq.Orbs)
                            {
                                var oo = new JsonObject();
                                try { oo["id"] = orb.Id.ToString(); } catch { }
                                oo["name"] = Txt(() => orb.Title.GetFormattedText());
                                oo["text"] = Txt(() => orb.Description.GetFormattedText());
                                try { oo["passive"] = (int)orb.PassiveVal; } catch { }
                                try { oo["evoke"] = (int)orb.EvokeVal; } catch { }
                                orbs.Add(oo);
                            }
                            o["orbs"] = orbs;
                            o["orb_capacity"] = oq.Capacity;
                        }
                    }
                    catch { }
                }
                try { o["player_powers"] = PowersJson(me.Creature.Powers); } catch { }
                try { o["player_block"] = me.Creature.Block; } catch { }
            }

            var enemies = new JsonArray();
            try { foreach (var e in cs.Enemies) enemies.Add(CreatureJson(e)); } catch { }
            o["enemies"] = enemies;
            return o;
        }

        private static JsonNode CreatureJson(object creatureObj)
        {
            var c = (MegaCrit.Sts2.Core.Entities.Creatures.Creature)creatureObj;
            var o = new JsonObject();
            try { o["name"] = c.Name; } catch { }
            try { o["hp"] = c.CurrentHp; } catch { }
            try { o["max_hp"] = c.MaxHp; } catch { }
            try { o["block"] = c.Block; } catch { }
            try { o["alive"] = c.IsAlive; } catch { }
            try { o["powers"] = PowersJson(c.Powers); } catch { }
            o["intent"] = IntentsJson(c);
            return o;
        }

        // Enemy intents with the numbers a human sees: computed total damage and hit
        // count for attacks, plus the localized tooltip text.
        public static JsonArray IntentsJson(MegaCrit.Sts2.Core.Entities.Creatures.Creature c)
        {
            var intents = new JsonArray();
            try
            {
                var mon = c.Monster;
                var move = mon?.NextMove;
                if (move == null) return intents;
                foreach (var it in move.Intents)
                {
                    var io = new JsonObject();
                    try { io["type"] = it.IntentType.ToString(); } catch { }
                    try
                    {
                        // Mirror the game's own hover-tip path (Creature.HoverTips).
                        var targets = c.CombatState?.Allies;
                        if (targets != null)
                        {
                            var tip = it.GetHoverTip(targets, c);
                            var title = CleanText(tip.Title);
                            var text = CleanText(tip.Description);
                            if (title != null) io["title"] = title;
                            if (text != null) io["text"] = text;
                            if (it is AttackIntent ai)
                            {
                                io["damage"] = ai.GetTotalDamage(targets, c);
                                int reps = ai.Repeats;
                                if (reps > 0) io["hits"] = reps + 1;
                            }
                        }
                    }
                    catch { }
                    intents.Add(io);
                }
            }
            catch { }
            return intents;
        }

        private static JsonArray PowersJson(IEnumerable<PowerModel> powers)
        {
            var arr = new JsonArray();
            if (powers == null) return arr;
            foreach (var p in powers)
            {
                var po = new JsonObject();
                try { po["id"] = p.Id.ToString(); } catch { }
                try { po["amount"] = p.Amount; } catch { }
                po["text"] = Txt(() => p.SmartDescription.GetFormattedText());
                arr.Add(po);
            }
            return arr;
        }

        // ---- cards ----
        public static JsonArray CardListJson(IEnumerable<CardModel> cards, bool full)
        {
            var arr = new JsonArray();
            if (cards == null) return arr;
            foreach (var c in cards) arr.Add(CardJson(c, full));
            return arr;
        }

        public static JsonNode CardJson(CardModel c, bool full = true)
        {
            var o = new JsonObject();
            try { o["id"] = c.Id.ToString(); } catch { }
            o["name"] = Txt(() => c.TitleLocString.GetFormattedText());
            try { o["cost"] = c.EnergyCost.Canonical; } catch { }
            try { o["upgrade"] = c.CurrentUpgradeLevel; } catch { }
            try { o["type"] = c.Type.ToString(); } catch { }
            if (!full) return o;
            try { o["cost_now"] = c.EnergyCost.GetResolved(); } catch { }
            try { o["target"] = c.TargetType.ToString(); } catch { }
            try { o["rarity"] = c.Rarity.ToString(); } catch { }
            try { o["playable"] = c.CanPlay(); } catch { }
            o["text"] = Txt(() => c.GetDescriptionForPile(PileType.None));
            return o;
        }

        // Resolved upgrade preview ("what would this card say if upgraded").
        public static string UpgradePreview(CardModel c)
        {
            return Txt(() => c.GetDescriptionForUpgradePreview());
        }

        // ---- deck (out of combat) ----
        public static JsonNode GetDeck()
        {
            var run = RunManager.Instance?.DebugOnlyGetState();
            var o = new JsonObject();
            if (run == null) { o["in_run"] = false; return o; }
            o["in_run"] = true;
            var me = run.Players.FirstOrDefault();
            if (me != null) o["deck"] = CardListJson(me.Deck?.Cards, full: true);
            return o;
        }

        // ---- map ----
        public static JsonNode GetMap()
        {
            var run = RunManager.Instance?.DebugOnlyGetState();
            var o = new JsonObject();
            if (run == null) { o["in_run"] = false; return o; }
            o["in_run"] = true;
            o["act"] = run.CurrentActIndex;
            o["act_floor"] = run.ActFloor;

            var map = run.Map;
            var nodes = new JsonArray();
            try
            {
                foreach (var pt in map.GetAllMapPoints())
                    nodes.Add(MapPointJson(pt));
            }
            catch (Exception e) { o["map_error"] = e.Message; }
            o["nodes"] = nodes;

            try
            {
                var cur = run.CurrentMapPoint;
                if (cur != null) o["current"] = CoordJson(cur.coord);
            }
            catch { }

            // reachable next steps
            var reachable = new JsonArray();
            try
            {
                var cur = run.CurrentMapPoint;
                IEnumerable<object> next;
                if (cur != null) next = cur.Children.Cast<object>();
                else next = map.startMapPoints.Cast<object>();
                foreach (var n in next) reachable.Add(MapPointJson(n));
            }
            catch (Exception e) { o["reachable_error"] = e.Message; }
            o["reachable_next"] = reachable;
            return o;
        }

        private static JsonNode MapPointJson(object ptObj)
        {
            var pt = (MegaCrit.Sts2.Core.Map.MapPoint)ptObj;
            var o = new JsonObject();
            try { o["coord"] = CoordJson(pt.coord); } catch { }
            try { o["type"] = pt.PointType.ToString(); } catch { }
            var kids = new JsonArray();
            try { foreach (var ch in pt.Children) kids.Add(CoordJson(((MegaCrit.Sts2.Core.Map.MapPoint)ch).coord)); } catch { }
            o["children"] = kids;
            return o;
        }

        private static JsonNode CoordJson(MegaCrit.Sts2.Core.Map.MapCoord coord)
        {
            return new JsonObject { ["col"] = coord.col, ["row"] = coord.row };
        }

        // ---- end-of-run summary (computed while the run state is still alive) ----
        public static JsonNode RunSummary(string result)
        {
            var o = new JsonObject { ["result"] = result };
            try
            {
                var run = RunManager.Instance?.DebugOnlyGetState();
                if (run == null) return o;
                bool victory = false;
                try { victory = run.CurrentRoom?.IsVictoryRoom ?? false; } catch { }
                bool defeat = false;
                try { defeat = run.IsGameOver; } catch { }   // all players dead
                o["victory"] = victory;
                o["defeat"] = defeat;
                // Disambiguates the exit code: a "failed" run with defeat=false died to a
                // harness/game error, not in play.
                o["outcome"] = victory ? "victory" : defeat ? "defeat"
                             : result == "abandoned" ? "abandoned" : "error";
                try { o["seed"] = run.Rng.StringSeed; } catch { }   // ACTUAL run seed (verifiable)
                o["act"] = run.CurrentActIndex;
                o["floor"] = run.TotalFloor;
                try { o["score"] = ScoreUtility.CalculateScore(run, victory); } catch { }
                var me = run.Players.FirstOrDefault();
                if (me != null)
                {
                    try { o["hp"] = me.Creature.CurrentHp; } catch { }
                    try { o["max_hp"] = me.Creature.MaxHp; } catch { }
                    try { o["gold"] = me.Gold; } catch { }
                    try { o["character"] = me.Character.Id.ToString(); } catch { }
                    var deck = new JsonArray();
                    try { foreach (var c in me.Deck.Cards) deck.Add(c.Id.ToString() + (c.CurrentUpgradeLevel > 0 ? "+" + c.CurrentUpgradeLevel : "")); } catch { }
                    o["deck"] = deck;
                    var relics = new JsonArray();
                    try { foreach (var r in me.Relics) relics.Add(r.Id.ToString()); } catch { }
                    o["relics"] = relics;
                }
            }
            catch { }
            return o;
        }
    }
}
