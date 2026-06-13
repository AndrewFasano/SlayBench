using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using System.Text.Json.Nodes;

namespace SpireLink
{
    // The decision rendezvous between the in-game driver (which pauses at each player
    // decision) and the connected client (which answers via `decide`).
    //
    // Everything here runs on the Godot main thread: command dispatch is marshaled to it
    // (SpireServer.OnMain), and the driver's handlers run in the AutoSlayer task on the main
    // thread. So Ask/Submit/Current are effectively single-threaded — no locking needed —
    // and `await Ask(...)` simply yields the main thread until `Submit` provides the choice.
    internal static class Decisions
    {
        private static JsonObject _pending;                  // exposed to observe()
        private static TaskCompletionSource<JsonObject> _tcs; // completed by decide()
        private static int _idCounter;

        // Called by a driver handler. Registers the decision and returns a task that
        // completes when the client answers. options/extra describe the choice.
        public static Task<JsonObject> Ask(string type, string prompt, JsonArray options, JsonObject extra = null)
        {
            if (_pending != null)
            {
                // Should be impossible (handlers are sequential); fail loudly rather than
                // silently orphaning the previous asker in a never-completing await.
                Log.Error($"Ask({type}) while decision[{_pending["id"]}] still pending — cancelling the old one");
                Cancel();
            }
            _idCounter++;
            var d = new JsonObject
            {
                ["id"] = _idCounter,
                ["type"] = type,
                ["prompt"] = prompt,
                ["options"] = options ?? new JsonArray(),
            };
            if (extra != null)
                foreach (var kv in extra)
                    d[kv.Key] = kv.Value?.DeepClone();
            _pending = d;
            _tcs = new TaskCompletionSource<JsonObject>(TaskCreationOptions.RunContinuationsAsynchronously);
            Log.Info($"decision[{_idCounter}] {type}: {prompt}");
            return _tcs.Task;
        }

        // A fresh copy of the current pending decision (or null), for observe().
        public static JsonNode CurrentPending()
        {
            return _pending?.DeepClone();
        }

        public static bool HasPending => _pending != null;

        // Called by the `decide` command. Validates the choice against the pending
        // decision; an invalid choice throws (the client gets ok:false) and the decision
        // REMAINS PENDING so the client can retry — invalid input must never be silently
        // coerced into a different choice.
        public static void Submit(int decisionId, JsonObject choice)
        {
            if (_pending == null)
                throw new Exception("no pending decision");
            int curId = (int)_pending["id"];
            if (decisionId != curId)
                throw new Exception($"stale decision_id (pending is {curId})");
            choice = choice ?? new JsonObject();
            Validate(_pending, choice);
            var tcs = _tcs;
            _pending = null;
            _tcs = null;
            tcs.SetResult(choice);
        }

        // Abort any pending decision (e.g., run cancelled).
        public static void Cancel()
        {
            var tcs = _tcs;
            _pending = null;
            _tcs = null;
            tcs?.TrySetCanceled();
        }

        // ---- choice validation ----

        private static HashSet<int> OptionIndices(JsonObject d, Func<JsonObject, bool> filter = null)
        {
            var set = new HashSet<int>();
            if (d["options"] is JsonArray opts)
                foreach (var n in opts)
                    if (n is JsonObject o && o["index"] != null && (filter == null || filter(o)))
                        set.Add((int)o["index"]);
            return set;
        }

        private static int? Int(JsonObject o, string key)
        {
            try { return o[key] != null ? (int?)(int)o[key] : null; } catch { return null; }
        }

        private static bool Flag(JsonObject o, string key)
        {
            try { return o[key] != null && (bool)o[key]; } catch { return false; }
        }

        private static void Validate(JsonObject d, JsonObject choice)
        {
            string type = (string)d["type"];
            switch (type)
            {
                case "combat": ValidateCombat(d, choice); break;
                case "map": ValidateMap(d, choice); break;
                case "card_select": ValidateCardSelect(d, choice); break;
                case "card_reward":
                    if (Flag(choice, "skip"))
                    {
                        if (!Flag(d, "can_skip")) throw new Exception("this card reward cannot be skipped");
                        break;
                    }
                    RequireOptionIndex(d, choice);
                    break;
                case "shop":
                {
                    if (Flag(choice, "leave")) break;
                    int? bi = Int(choice, "buy_index") ?? Int(choice, "option_index");
                    RequireIndexIn(OptionIndices(d), bi, "buy_index");
                    // Reject unaffordable purchases (the handler would otherwise no-op
                    // silently and re-present the shop — a coerced non-action).
                    if (d["options"] is JsonArray sopts)
                        foreach (var n in sopts)
                            if (n is JsonObject o && Int(o, "index") == bi
                                && o["affordable"] != null && !Flag(o, "affordable"))
                                throw new Exception($"cannot afford item {bi} (cost {Int(o, "cost")})");
                    break;
                }
                case "treasure":
                    if (Flag(choice, "take") || Flag(choice, "skip")) break;
                    RequireOptionIndex(d, choice);
                    break;
                case "combat_reward":
                    if (Flag(choice, "proceed")) break;
                    RequireOptionIndex(d, choice);
                    break;
                case "event":
                case "rest":
                case "relic_select":
                    RequireOptionIndex(d, choice);
                    break;
                case "game_over":
                    break; // any ack accepted
                default:
                    RequireOptionIndex(d, choice); // unknown types: at least a valid index
                    break;
            }
        }

        private static void RequireOptionIndex(JsonObject d, JsonObject choice)
        {
            RequireIndexIn(OptionIndices(d), Int(choice, "option_index"), "option_index");
        }

        private static void RequireIndexIn(HashSet<int> valid, int? idx, string field)
        {
            if (idx == null)
                throw new Exception($"choice requires {field} (one of: {string.Join(",", valid.OrderBy(i => i))})");
            if (!valid.Contains(idx.Value))
                throw new Exception($"invalid {field} {idx} (valid: {string.Join(",", valid.OrderBy(i => i))})");
        }

        private static void ValidateCombat(JsonObject d, JsonObject choice)
        {
            string action = (string)choice["action"] ?? "";
            int targetCount = (d["targets"] as JsonArray)?.Count ?? 0;
            switch (action)
            {
                case "end_turn":
                    return;
                case "play_card":
                {
                    int? ci = Int(choice, "card_index");
                    var playable = OptionIndices(d, o => (string)o["action"] == "play_card" && Flag(o, "playable"));
                    var all = OptionIndices(d, o => (string)o["action"] == "play_card");
                    if (ci == null || !all.Contains(ci.Value))
                        throw new Exception($"invalid card_index {ci} (hand indices: {string.Join(",", all.OrderBy(i => i))})");
                    if (!playable.Contains(ci.Value))
                        throw new Exception($"card {ci} is not playable right now (playable: {string.Join(",", playable.OrderBy(i => i))})");
                    bool needsTarget = false;
                    if (d["options"] is JsonArray opts)
                        foreach (var n in opts)
                            if (n is JsonObject o && Int(o, "card_index") == ci && (string)o["action"] == "play_card")
                                needsTarget = Flag(o, "needs_target");
                    if (needsTarget)
                    {
                        int ti = Int(choice, "target_index") ?? 0;
                        if (ti < 0 || ti >= targetCount)
                            throw new Exception($"invalid target_index {ti} (0..{targetCount - 1})");
                    }
                    return;
                }
                case "use_potion":
                {
                    int? pi = Int(choice, "potion_index");
                    var pots = new HashSet<int>();
                    bool potNeedsTarget = false;
                    if (d["options"] is JsonArray opts)
                        foreach (var n in opts)
                            if (n is JsonObject o && (string)o["action"] == "use_potion" && o["potion_index"] != null)
                            {
                                pots.Add((int)o["potion_index"]);
                                if (Int(o, "potion_index") == pi) potNeedsTarget = Flag(o, "needs_target");
                            }
                    if (pi == null || !pots.Contains(pi.Value))
                        throw new Exception($"invalid potion_index {pi} (valid: {string.Join(",", pots.OrderBy(i => i))})");
                    if (potNeedsTarget)
                    {
                        int ti = Int(choice, "target_index") ?? 0;
                        if (ti < 0 || ti >= targetCount)
                            throw new Exception($"invalid target_index {ti} (0..{targetCount - 1})");
                    }
                    return;
                }
                default:
                    throw new Exception($"unknown combat action '{action}' (play_card | use_potion | end_turn)");
            }
        }

        private static void ValidateMap(JsonObject d, JsonObject choice)
        {
            if (choice["coord"] is JsonObject co)
            {
                int? col = Int(co, "col"), row = Int(co, "row");
                if (d["options"] is JsonArray opts)
                    foreach (var n in opts)
                        if (n is JsonObject o && o["coord"] is JsonObject oc
                            && Int(oc, "col") == col && Int(oc, "row") == row)
                            return;
                throw new Exception($"coord ({col},{row}) is not a reachable next room");
            }
            RequireOptionIndex(d, choice);
        }

        private static void ValidateCardSelect(JsonObject d, JsonObject choice)
        {
            int min = Int(d, "min_select") ?? 1;
            int max = Int(d, "max_select") ?? 1;
            var valid = OptionIndices(d);
            min = Math.Min(min, valid.Count);
            if (Flag(choice, "skip"))
            {
                if (min > 0)
                    throw new Exception($"cannot skip: must select at least {min} card(s)");
                return;
            }
            if (!(choice["indices"] is JsonArray arr))
            {
                // single option_index is accepted as shorthand when one pick suffices
                int? oi = Int(choice, "option_index");
                if (oi != null && valid.Contains(oi.Value) && min <= 1) return;
                throw new Exception($"card_select requires indices:[...] ({min}-{max} of: {string.Join(",", valid.OrderBy(i => i))})");
            }
            var picked = new HashSet<int>();
            foreach (var n in arr)
            {
                int idx;
                try { idx = (int)n; } catch { throw new Exception("indices must be integers"); }
                if (!valid.Contains(idx))
                    throw new Exception($"invalid card index {idx} (valid: {string.Join(",", valid.OrderBy(i => i))})");
                picked.Add(idx);
            }
            if (picked.Count < min || picked.Count > max)
                throw new Exception($"must select between {min} and {max} cards (got {picked.Count})");
        }
    }
}
