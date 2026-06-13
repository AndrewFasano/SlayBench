using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json.Nodes;
using Godot;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Runs;

namespace SpireLink
{
    // Command dispatch. Every handler runs on the Godot main thread (see SpireServer.OnMain),
    // so it is safe to read live game singletons here.
    internal static class Commands
    {
        public static JsonNode Handle(string cmd, JsonObject args)
        {
            switch (cmd)
            {
                case "ping": return Ping();
                case "get_state": return Snapshot.GetState();
                case "get_deck": return Snapshot.GetDeck();
                case "get_map": return Snapshot.GetMap();
                case "start_run": return StartRun(args);
                case "abandon_run": return AbandonRun();
                case "observe": return Observe();
                case "decide": return Decide(args);
                default: throw new Exception("unknown cmd: '" + cmd + "'");
            }
        }

        private static JsonNode StartRun(JsonObject args)
        {
            string seed = args != null && args["seed"] != null ? (string)args["seed"] : null;
            string character = args != null && args["character"] != null ? (string)args["character"] : null;
            Driver.StartRun(seed, character);
            return new JsonObject { ["started"] = true };
        }

        private static JsonNode AbandonRun()
        {
            Driver.AbandonRun();
            return new JsonObject { ["abandoned"] = true };
        }

        private static JsonNode Observe()
        {
            var pending = Decisions.CurrentPending();
            string phase;
            bool inRun = false;
            try { inRun = RunManager.Instance?.DebugOnlyGetState() != null; } catch { }

            if (pending != null) phase = "awaiting_decision";
            else if (Driver.RunActive) phase = "busy";
            else if (Driver.LastResult != null) phase = "run_over";
            else phase = "menu";

            var o = new JsonObject
            {
                ["phase"] = phase,
                ["decision"] = pending,
                ["state"] = Snapshot.GetState(),
            };
            if (Driver.LastResult != null) o["last_result"] = Driver.LastResult;
            if (Driver.LastError != null) o["last_error"] = Driver.LastError;
            if (Driver.RunSummary != null) o["run_summary"] = Driver.RunSummary.DeepClone();
            return o;
        }

        private static JsonNode Decide(JsonObject args)
        {
            if (args == null || args["decision_id"] == null)
                throw new Exception("decide requires decision_id");
            int id = (int)args["decision_id"];
            var choice = args["choice"] as JsonObject ?? new JsonObject();
            Decisions.Submit(id, choice);
            return new JsonObject { ["accepted"] = true };
        }

        // Game build (from <app>/Contents/Resources/release_info.json), read once.
        // Results are only comparable across identical game builds — the harness
        // records this per sample.
        private static string _gameVersion;
        private static string GameVersion()
        {
            if (_gameVersion != null) return _gameVersion;
            try
            {
                string exe = Godot.OS.GetExecutablePath();
                string path = System.IO.Path.GetFullPath(System.IO.Path.Combine(
                    System.IO.Path.GetDirectoryName(exe), "..", "Resources", "release_info.json"));
                var info = JsonNode.Parse(System.IO.File.ReadAllText(path));
                _gameVersion = $"{(string)info["version"]} ({(string)info["commit"]})";
            }
            catch { _gameVersion = "unknown"; }
            return _gameVersion;
        }

        private static JsonNode Ping()
        {
            var o = new JsonObject
            {
                ["pong"] = true,
                ["mod"] = "spirelink",
                ["version"] = BuildInfo.Stamp,
                ["game_version"] = GameVersion(),
            };
            try { o["in_run"] = RunManager.Instance?.DebugOnlyGetState() != null; } catch { o["in_run"] = false; }
            try { o["in_combat"] = CombatManager.Instance != null && CombatManager.Instance.IsInProgress; } catch { o["in_combat"] = false; }
            return o;
        }
    }
}
