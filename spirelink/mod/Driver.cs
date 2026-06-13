using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Text.Json.Nodes;
using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.AutoSlay;
using MegaCrit.Sts2.Core.AutoSlay.Handlers.Rooms;
using MegaCrit.Sts2.Core.AutoSlay.Handlers.Screens;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.GameActions.Multiplayer;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.Screens.CharacterSelect;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Random;
using MegaCrit.Sts2.Core.Runs;

namespace SpireLink
{
    // Drives the real game by running the game's own AutoSlayer (which handles menu -> run ->
    // navigation + non-interactive setup) while Harmony-patched handlers defer each player
    // decision to the connected client.
    internal static class Driver
    {
        public static bool RunActive;
        public static string LastResult;        // null while running; "completed"/"failed"/"abandoned" after
        public static string LastError;         // failure reason from AutoSlayLog.RunFailed (see Robustness.cs)
        public static JsonNode RunSummary;      // structured outcome of the last run (victory, score, floor, deck...)
        public static string PendingCharacterId; // honored by the character-pick patch at run start
        public static string PendingSeed;        // re-asserted at confirm time (see PlayMainMenuAsync)

        private static AutoSlayer _slayer;
        private static bool _abandoning;

        public static void StartRun(string seed, string character)
        {
            if (RunActive)
                throw new Exception("a run is already active");
            LastResult = null;
            LastError = null;
            RunSummary = null;
            _abandoning = false;
            RunActive = true;
            PendingCharacterId = string.IsNullOrEmpty(character) ? null : character;
            string s = string.IsNullOrEmpty(seed) ? "SPIRELINK" : seed;
            PendingSeed = s;
            Log.Info($"StartRun seed={s} character={character ?? "(default)"}");
            _slayer = new AutoSlayer();
            _slayer.Start(s);
        }

        // Client-driven escape hatch: cancel the pending decision (if any) and the
        // AutoSlayer run task. The run unwinds through RunAsync's finally -> QuitGame
        // -> OnRunEnded, and the next StartRun's menu handler clears the abandoned save.
        public static void AbandonRun()
        {
            if (!RunActive)
                throw new Exception("no active run to abandon");
            Log.Info("AbandonRun requested by client");
            _abandoning = true;
            Decisions.Cancel();
            try { _slayer?.Stop(); } catch (Exception e) { Log.Error("Stop failed: " + e.Message); }
        }

        public static void OnRunEnded(int exitCode)
        {
            RunActive = false;
            LastResult = _abandoning ? "abandoned" : (exitCode == 0 ? "completed" : "failed");
            _abandoning = false;
            _slayer = null;
            Decisions.Cancel();
            // Capture the structured outcome NOW, while the run state still exists
            // (ReturnToMainMenu below calls RunManager.CleanUp).
            try { RunSummary = Snapshot.RunSummary(LastResult); } catch (Exception e) { Log.Error("RunSummary failed: " + e.Message); }
            Log.Info($"run ended (exit {exitCode}, result {LastResult})");

            // Don't leak the seed override into a later manually-started run.
            try { if (NGame.Instance != null) NGame.Instance.DebugSeedOverride = null; } catch { }

            // Vanilla AutoSlayer exits the process here, so nothing ever navigates back to
            // the menu. Since we suppress the exit, do it ourselves or the next start_run
            // finds the dead run scene still loaded ("MainMenu node not found").
            try
            {
                var game = NGame.Instance;
                if (game != null && game.MainMenu == null)
                {
                    Log.Info("returning to main menu");
                    _ = game.ReturnToMainMenu();
                }
            }
            catch (Exception e) { Log.Error("ReturnToMainMenu failed: " + e.Message); }
        }

        // ---- main menu: same navigation as AutoSlayer, but pick the client's character ----
        public static async Task PlayMainMenuAsync(CancellationToken ct)
        {
            AutoSlayLog.Action("[SpireLink] main menu");
            // AutoSlayer sets FastMode=Fast before this runs; Instant is faster still and
            // wall-clock dominates eval cost. SPIRELINK_FASTMODE=fast opts back down.
            try
            {
                var pref = System.Environment.GetEnvironmentVariable("SPIRELINK_FASTMODE");
                MegaCrit.Sts2.Core.Saves.SaveManager.Instance.PrefsSave.FastMode =
                    pref == "fast" ? MegaCrit.Sts2.Core.Settings.FastModeType.Fast
                                   : MegaCrit.Sts2.Core.Settings.FastModeType.Instant;
            }
            catch (Exception e) { Log.Error("FastMode set failed: " + e.Message); }
            Node root = ((SceneTree)Engine.GetMainLoop()).Root;
            Control mainMenu = await WaitHelper.ForNode<Control>(root, "/root/Game/RootSceneContainer/MainMenu", ct, TimeSpan.FromSeconds(30L));

            NButton abandon = mainMenu.GetNode<NButton>("MainMenuTextButtons/AbandonRunButton");
            if (abandon.Visible)
            {
                await UiHelper.Click(abandon);
                await WaitHelper.Until(() => NModalContainer.Instance?.OpenModal != null, ct, AutoSlayConfig.nodeWaitTimeout, "Abandon popup did not appear");
                NButton yes = ((Node)NModalContainer.Instance.OpenModal).GetNode<NButton>("VerticalPopup/YesButton");
                await UiHelper.Click(yes);
                await WaitHelper.Until(() => NModalContainer.Instance.OpenModal == null, ct, AutoSlayConfig.nodeWaitTimeout, "Abandon popup did not close");
            }

            NButton sp = mainMenu.GetNode<NButton>("MainMenuTextButtons/SingleplayerButton");
            await UiHelper.Click(sp);

            Control charSelectScreen = null;
            NButton standardButton = null;
            await WaitHelper.Until(() =>
            {
                charSelectScreen = mainMenu.GetNodeOrNull<Control>("Submenus/CharacterSelectScreen");
                standardButton = mainMenu.GetNodeOrNull<NButton>("Submenus/SingleplayerSubmenu/StandardButton");
                return (charSelectScreen?.Visible ?? false) || (standardButton?.Visible ?? false);
            }, ct, AutoSlayConfig.nodeWaitTimeout, "Character select / submenu did not appear");

            if ((standardButton?.Visible ?? false) && !(charSelectScreen?.Visible ?? false))
            {
                await UiHelper.Click(standardButton);
                await WaitHelper.Until(() => mainMenu.GetNodeOrNull<Control>("Submenus/CharacterSelectScreen")?.Visible ?? false, ct, AutoSlayConfig.nodeWaitTimeout, "Character select did not appear");
                charSelectScreen = mainMenu.GetNode<Control>("Submenus/CharacterSelectScreen");
            }

            Node container = charSelectScreen.GetNode("CharSelectButtons/ButtonContainer");
            var buttons = UiHelper.FindAll<NCharacterSelectButton>(container);
            foreach (var b in buttons) b.UnlockIfPossible();
            var unlocked = buttons.Where(b => !b.IsLocked).ToList();

            NCharacterSelectButton chosen = null;
            if (PendingCharacterId != null)
                chosen = unlocked.FirstOrDefault(b => CharId(b).IndexOf(PendingCharacterId, StringComparison.OrdinalIgnoreCase) >= 0);
            if (chosen == null) chosen = unlocked.FirstOrDefault();
            PendingCharacterId = null;
            if (chosen == null) { AutoSlayLog.Warn("[SpireLink] no unlocked character"); return; }

            AutoSlayLog.Action("[SpireLink] selecting character " + CharId(chosen));
            chosen.Select();
            await Task.Delay(100, ct);
            NButton confirm = await WaitHelper.ForNode<NButton>(mainMenu, "Submenus/CharacterSelectScreen/ConfirmButton", ct, null);
            // The character-select screen NULLS NGame.DebugSeedOverride when it opens in
            // singleplayer (NCharacterSelectScreen), and the run lobby reads the override
            // at confirm time (StartRunLobby) — so AutoSlayer's seed was silently lost and
            // every run was randomly seeded. Re-assert ours at the last moment.
            if (PendingSeed != null)
            {
                NGame.Instance.DebugSeedOverride = PendingSeed;
                Log.Info("seed re-asserted before confirm: " + PendingSeed);
                PendingSeed = null;
            }
            await UiHelper.Click(confirm);
        }

        private static string CharId(NCharacterSelectButton b) { try { return b.Character?.Id?.ToString() ?? ""; } catch { return ""; } }

        // ---- combat: client picks each action (play card / use potion / end turn) ----
        public static async Task CombatAsync(Rng random, CancellationToken ct)
        {
            AutoSlayLog.Action("[SpireLink] combat: awaiting client");
            await WaitHelper.Until(() => CombatManager.Instance.IsInProgress, ct,
                AutoSlayConfig.nodeWaitTimeout, "Combat not started");
            var run = RunManager.Instance.DebugOnlyGetState();
            Player me = LocalContext.GetMe(run);

            // Single loop that ALWAYS yields (await) each iteration, so the engine can process
            // enemy turns and combat-end. Never busy-spin.
            int guard = 0;
            while (CombatManager.Instance.IsInProgress && guard++ < 5000)
            {
                ct.ThrowIfCancellationRequested();
                if (!CombatManager.Instance.IsPlayPhase) { await Task.Delay(100, ct); continue; }
                var cs = CombatManager.Instance.DebugOnlyGetState();
                if (cs == null || CombatManager.Instance.IsEnding || cs.HittableEnemies.Count == 0)
                {
                    // Enemies down but combat still flagged in-progress: force the win check
                    // (the engine resolves the victory at this point, like ending a turn would).
                    try { await CombatManager.Instance.CheckWinCondition(); } catch { }
                    await Task.Delay(100, ct);
                    continue;
                }
                var (options, extra) = BuildCombatDecision(me);
                JsonObject choice = await Decisions.Ask("combat",
                    $"Your turn. Energy {me.PlayerCombatState?.Energy}/{me.PlayerCombatState?.MaxEnergy}. Pick an action.",
                    options, extra);

                string action = (string)(choice["action"]) ?? "end_turn";
                if (action == "end_turn") { PlayerCmd.EndTurn(me, canBackOut: false); await Task.Delay(150, ct); }
                else if (action == "play_card") { await PlayCard(me, choice, ct); await Task.Delay(80, ct); }
                else if (action == "use_potion") { UsePotion(me, choice); await Task.Delay(200, ct); }
                else throw new Exception("unknown combat action: " + action);
            }
            await WaitHelper.Until(() => !CombatManager.Instance.IsInProgress, ct,
                TimeSpan.FromSeconds(30L), "Combat did not end");
            AutoSlayLog.Action("[SpireLink] combat finished");
        }

        private static async Task PlayCard(Player me, JsonObject choice, CancellationToken ct)
        {
            int idx = choice["card_index"] != null ? (int)choice["card_index"] : -1;
            var hand = PileType.Hand.GetPile(me).Cards;
            if (idx < 0 || idx >= hand.Count)
                throw new Exception("bad card_index");
            CardModel card = hand[idx];
            Creature target = null;
            if (card.TargetType == TargetType.AnyEnemy)
            {
                var cs = CombatManager.Instance.DebugOnlyGetState();
                var enemies = cs.HittableEnemies.ToList();
                int ti = choice["target_index"] != null ? (int)choice["target_index"] : 0;
                if (ti < 0 || ti >= enemies.Count)
                    throw new Exception($"invalid target_index {ti} (0..{enemies.Count - 1})");
                target = enemies[ti];
            }
            AutoSlayLog.Info("[SpireLink] play " + card.Id.Entry);
            await CardCmd.AutoPlay(new BlockingPlayerChoiceContext(), card, target);
        }

        private static void UsePotion(Player me, JsonObject choice)
        {
            int pi = choice["potion_index"] != null ? (int)choice["potion_index"] : -1;
            var potions = me.Potions.ToList();
            if (pi < 0 || pi >= potions.Count)
                throw new Exception("bad potion_index");
            PotionModel potion = potions[pi];
            Creature target = null;
            if (potion.TargetType == TargetType.AnyEnemy)
            {
                var cs = CombatManager.Instance.DebugOnlyGetState();
                var enemies = cs.HittableEnemies.ToList();
                int ti = choice["target_index"] != null ? (int)choice["target_index"] : 0;
                if (ti < 0 || ti >= enemies.Count)
                    throw new Exception($"invalid target_index {ti} (0..{enemies.Count - 1})");
                target = enemies[ti];
            }
            potion.EnqueueManualUse(target);
        }

        private static (JsonArray, JsonObject) BuildCombatDecision(Player me)
        {
            var options = new JsonArray();
            var pcs = me.PlayerCombatState;
            var hand = pcs?.Hand?.Cards;
            int i = 0;
            if (hand != null)
            {
                var csForPlayable = CombatManager.Instance.DebugOnlyGetState();
                bool anyEnemies = csForPlayable != null && csForPlayable.HittableEnemies.Count > 0;
                foreach (var c in hand)
                {
                    bool playable = false;
                    try { playable = c.CanPlay(); } catch { }
                    // A card that must target an enemy is not playable with no enemies left.
                    if (playable && c.TargetType == TargetType.AnyEnemy && !anyEnemies) playable = false;
                    var opt = new JsonObject
                    {
                        ["index"] = i,
                        ["action"] = "play_card",
                        ["card_index"] = i,
                        ["label"] = c.Id.ToString(),
                        ["cost"] = SafeCost(c),
                        ["type"] = c.Type.ToString(),
                        ["target"] = c.TargetType.ToString(),
                        ["needs_target"] = c.TargetType == TargetType.AnyEnemy,
                        ["playable"] = playable,
                    };
                    try { opt["name"] = Snapshot.CleanText(c.TitleLocString.GetFormattedText()); } catch { }
                    try { opt["text"] = Snapshot.CleanText(c.GetDescriptionForPile(PileType.Hand)); } catch { }
                    options.Add(opt);
                    i++;
                }
            }
            int p = 0;
            foreach (var pot in me.Potions)
            {
                var popt = new JsonObject
                {
                    ["index"] = 200 + p,
                    ["action"] = "use_potion",
                    ["potion_index"] = p,
                    ["label"] = pot.Id.ToString(),
                    ["needs_target"] = pot.TargetType == TargetType.AnyEnemy,
                };
                try { popt["name"] = Snapshot.CleanText(pot.Title.GetFormattedText()); } catch { }
                try { popt["text"] = Snapshot.CleanText(pot.DynamicDescription.GetFormattedText()); } catch { }
                options.Add(popt);
                p++;
            }
            options.Add(new JsonObject { ["index"] = 100, ["action"] = "end_turn", ["label"] = "End turn" });

            var enemies = new JsonArray();
            var cs = CombatManager.Instance.DebugOnlyGetState();
            if (cs != null)
            {
                int ei = 0;
                foreach (var e in cs.HittableEnemies)
                {
                    var eo = new JsonObject
                    {
                        ["target_index"] = ei,
                        ["name"] = SafeName(e),
                        ["hp"] = SafeHp(e),
                        ["block"] = SafeBlock(e),
                    };
                    try { eo["intent"] = Snapshot.IntentsJson(e); } catch { }
                    enemies.Add(eo);
                    ei++;
                }
            }
            var extra = new JsonObject { ["targets"] = enemies };
            return (options, extra);
        }

        private static int SafeCost(CardModel c) { try { return c.EnergyCost.GetResolved(); } catch { return 0; } }
        private static string SafeName(Creature e) { try { return e.Name; } catch { return "?"; } }
        private static int SafeHp(Creature e) { try { return e.CurrentHp; } catch { return 0; } }
        private static int SafeBlock(Creature e) { try { return e.Block; } catch { return 0; } }

        // ---- map: client picks the next room from reachable nodes ----
        public static async Task MapAsync(Rng random, CancellationToken ct)
        {
            AutoSlayLog.Action("[SpireLink] map: awaiting client");
            Node root = ((SceneTree)Engine.GetMainLoop()).Root;
            NRun runNode = root.GetNode<NRun>("/root/Game/RootSceneContainer/Run");
            await WaitHelper.Until(() => runNode.GlobalUi.MapScreen.IsVisibleInTree(), ct,
                AutoSlayConfig.mapScreenTimeout, "Map screen not visible");
            List<NMapPoint> source = UiHelper.FindAll<NMapPoint>(runNode.GlobalUi.MapScreen);
            RunState run = RunManager.Instance.DebugOnlyGetState();

            List<NMapPoint> reachable;
            if (run.VisitedMapCoords.Count == 0)
            {
                reachable = source.Where(mp => mp.Point.coord.row == 0).ToList();
            }
            else
            {
                MapCoord last = run.VisitedMapCoords[run.VisitedMapCoords.Count - 1];
                NMapPoint lastNode = source.First(mp => mp.Point.coord.Equals(last));
                var childCoords = lastNode.Point.Children.Select(c => c.coord).ToList();
                reachable = source.Where(mp => childCoords.Any(cc => cc.Equals(mp.Point.coord))).ToList();
            }

            var options = new JsonArray();
            for (int i = 0; i < reachable.Count; i++)
            {
                var pt = reachable[i].Point;
                options.Add(new JsonObject
                {
                    ["index"] = i,
                    ["label"] = pt.PointType.ToString(),
                    ["coord"] = new JsonObject { ["col"] = pt.coord.col, ["row"] = pt.coord.row },
                });
            }
            JsonObject choice = await Decisions.Ask("map", "Choose the next room to enter.", options);

            NMapPoint chosen = ResolveMapChoice(reachable, choice);
            // Enter the chosen node via the model API rather than simulating a UI click
            // (deterministic, works headless and regardless of continuation thread).
            await RunManager.Instance.EnterMapCoord(chosen.Point.coord);
            AutoSlayLog.Action("[SpireLink] map: entered room " + chosen.Point.coord.col + "," + chosen.Point.coord.row);
        }

        private static NMapPoint ResolveMapChoice(List<NMapPoint> reachable, JsonObject choice)
        {
            if (choice["coord"] is JsonObject co)
            {
                int col = (int)co["col"]; int row = (int)co["row"];
                var m = reachable.FirstOrDefault(mp => mp.Point.coord.col == col && mp.Point.coord.row == row);
                if (m != null) return m;
            }
            if (choice["option_index"] != null)
            {
                int oi = (int)choice["option_index"];
                if (oi >= 0 && oi < reachable.Count) return reachable[oi];
            }
            throw new Exception("invalid map choice");
        }
    }

    // ---- Harmony patches: redirect handler decisions to the client ----

    [HarmonyPatch(typeof(CombatRoomHandler), "HandleAsync")]
    internal static class Patch_Combat
    {
        private static bool Prefix(Rng random, CancellationToken ct, ref Task __result)
        {
            // Diagnostic: SPIRELINK_PASSTHROUGH=1 lets the vanilla AutoSlayer combat run.
            if (System.Environment.GetEnvironmentVariable("SPIRELINK_PASSTHROUGH") == "1") return true;
            __result = Driver.CombatAsync(random, ct);
            return false;
        }
    }

    [HarmonyPatch(typeof(MapScreenHandler), "HandleAsync")]
    internal static class Patch_Map
    {
        private static bool Prefix(Rng random, CancellationToken ct, ref Task __result)
        {
            __result = Driver.MapAsync(random, ct);
            return false;
        }
    }

    // Character selection: replace only the menu navigation (isolated; no global RNG impact).
    [HarmonyPatch(typeof(AutoSlayer), "PlayMainMenuAsync")]
    internal static class Patch_MainMenu
    {
        private static bool Prefix(CancellationToken ct, ref Task __result)
        {
            __result = Driver.PlayMainMenuAsync(ct);
            return false;
        }
    }

    // Don't let AutoSlayer exit the process when a run ends; return to menu instead.
    [HarmonyPatch(typeof(AutoSlayer), "QuitGame")]
    internal static class Patch_QuitGame
    {
        private static bool Prefix(int exitCode)
        {
            Driver.OnRunEnded(exitCode);
            return false;
        }
    }
}
