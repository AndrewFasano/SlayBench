using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Text.Json.Nodes;
using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.AutoSlay;
using MegaCrit.Sts2.Core.AutoSlay.Handlers.Screens;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.Relics;
using MegaCrit.Sts2.Core.Nodes.Rewards;
using MegaCrit.Sts2.Core.Nodes.Screens;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Random;
using MegaCrit.Sts2.Core.Rewards;
using MegaCrit.Sts2.Core.Runs;

namespace SpireLink
{
    // Additional client decisions: combat-reward selection, card selection (smith/transform/
    // enchant/hand), and relic selection. (Character selection lives in Driver.)
    internal static class MoreScreens
    {
        // ---- card selection: route ALL CardSelectCmd selections through the client ----
        public static async Task<IEnumerable<CardModel>> SelectCardsAsync(IEnumerable<CardModel> options, int minSelect, int maxSelect)
        {
            var list = options.ToList();
            if (list.Count == 0) return Array.Empty<CardModel>();

            var opts = new JsonArray();
            for (int i = 0; i < list.Count; i++)
            {
                int ii = i;
                JsonObject opt;
                try { opt = (JsonObject)Snapshot.CardJson(list[ii]); } catch { opt = new JsonObject(); }
                opt["index"] = i;
                opt["label"] = SafeId(list[ii]);
                // For smith-style selections the key question is "what does the upgrade do".
                try
                {
                    var up = Snapshot.UpgradePreview(list[ii]);
                    if (up != null) opt["upgraded_text"] = up;
                }
                catch { }
                opts.Add(opt);
            }
            var extra = new JsonObject { ["min_select"] = minSelect, ["max_select"] = maxSelect };
            var choice = await Decisions.Ask("card_select", $"Select {minSelect}-{maxSelect} card(s).", opts, extra);

            var picked = new List<CardModel>();
            // skip is only valid when nothing must be selected (validated in Decisions)
            if (choice["skip"] != null && (bool)choice["skip"] && minSelect <= 0)
                return Array.Empty<CardModel>();
            if (choice["indices"] is JsonArray arr)
            {
                foreach (var n in arr)
                {
                    int idx = (int)n;
                    if (idx >= 0 && idx < list.Count && !picked.Contains(list[idx])) picked.Add(list[idx]);
                }
            }
            else if (choice["option_index"] != null)
            {
                int idx = (int)choice["option_index"];
                if (idx >= 0 && idx < list.Count) picked.Add(list[idx]);
            }
            // enforce bounds
            if (picked.Count > maxSelect) picked = picked.Take(maxSelect).ToList();
            foreach (var c in list)
            {
                if (picked.Count >= minSelect) break;
                if (!picked.Contains(c)) picked.Add(c);
            }
            return picked;
        }

        // ---- combat rewards: take a reward, or proceed ----
        public static async Task RewardsAsync(Rng random, CancellationToken ct)
        {
            var screen = AutoSlayer.GetCurrentScreen<NRewardsScreen>();
            int guard = 0;
            while (guard++ < 40)
            {
                ct.ThrowIfCancellationRequested();
                var me = LocalContext.GetMe(RunManager.Instance.DebugOnlyGetState());
                bool hasPotionSlots = me?.HasOpenPotionSlots ?? false;
                var buttons = UiHelper.FindAll<NRewardButton>(screen)
                    .Where(b => b.IsEnabled && (!(b.Reward is PotionReward) || hasPotionSlots)).ToList();

                var options = new JsonArray();
                for (int i = 0; i < buttons.Count; i++)
                {
                    int ii = i;
                    options.Add(new JsonObject { ["index"] = i, ["label"] = RewardLabel(buttons[ii].Reward) });
                }
                options.Add(new JsonObject { ["index"] = 100, ["label"] = "Proceed (done taking rewards)" });
                var choice = await Decisions.Ask("combat_reward", "Take a reward, or proceed.", options);

                int idx = choice["option_index"] != null ? (int)choice["option_index"]
                          : (choice["proceed"] != null ? 100 : 100);
                if (idx == 100)
                {
                    var proceed = UiHelper.FindFirst<NProceedButton>(screen);
                    if (proceed != null)
                    {
                        await UiHelper.Click(proceed);
                        await WaitHelper.Until(() => !GodotObject.IsInstanceValid(screen) || NOverlayStack.Instance?.Peek() != screen || (NMapScreen.Instance?.IsOpen ?? false),
                            ct, TimeSpan.FromSeconds(10L), "Rewards did not close after proceed");
                    }
                    break;
                }
                if (idx >= 0 && idx < buttons.Count)
                {
                    await UiHelper.Click(buttons[idx]);
                    await Task.Delay(400, ct);
                    var top = NOverlayStack.Instance?.Peek();
                    if (top != null && top != screen) return; // child (e.g. card reward) -> drain loop re-enters
                }
            }
        }

        // ---- relic selection (boss/event relic choice) ----
        public static async Task RelicSelectAsync(Rng random, CancellationToken ct)
        {
            var screen = AutoSlayer.GetCurrentScreen<NChooseARelicSelection>();
            var list = UiHelper.FindAll<NClickableControl>(screen);
            if (list.Count == 0) { AutoSlayLog.Warn("[SpireLink] no relic options"); return; }

            var options = new JsonArray();
            for (int i = 0; i < list.Count; i++)
            {
                int ii = i;
                var opt = new JsonObject { ["index"] = i, ["label"] = RelicLabel(list[ii], ii) };
                try
                {
                    var relic = UiHelper.FindFirst<NRelic>(list[ii]);
                    if (relic != null && Snapshot.RelicJson(relic.Model) is JsonObject rj)
                        foreach (var kv in rj) opt[kv.Key] = kv.Value?.DeepClone();
                }
                catch { }
                options.Add(opt);
            }
            var choice = await Decisions.Ask("relic_select", "Choose a relic.", options);
            int idx = choice["option_index"] != null ? (int)choice["option_index"] : 0;
            if (idx < 0 || idx >= list.Count) idx = 0;
            await UiHelper.Click(list[idx]);
        }

        // ---- label helpers ----
        private static string SafeId(CardModel c) { try { return c.Id.ToString(); } catch { return "card"; } }

        private static string RewardLabel(Reward r)
        {
            if (r == null) return "reward";
            try { var s = Snapshot.CleanText(r.Description?.GetFormattedText()); if (!string.IsNullOrEmpty(s)) return s; } catch { }
            return r.GetType().Name;
        }

        private static string RelicLabel(Node node, int i)
        {
            try
            {
                var relic = UiHelper.FindFirst<NRelic>(node);
                if (relic != null) return relic.Model.Id.ToString();
            }
            catch { }
            return "relic " + i;
        }
    }

    // ---- patches ----

    [HarmonyPatch(typeof(AutoSlayCardSelector), "GetSelectedCards")]
    internal static class Patch_CardSelect
    {
        private static bool Prefix(IEnumerable<CardModel> options, int minSelect, int maxSelect, ref Task<IEnumerable<CardModel>> __result)
        { __result = MoreScreens.SelectCardsAsync(options, minSelect, maxSelect); return false; }
    }

    [HarmonyPatch(typeof(RewardsScreenHandler), "HandleAsync")]
    internal static class Patch_Rewards
    {
        private static bool Prefix(Rng random, CancellationToken ct, ref Task __result)
        { __result = MoreScreens.RewardsAsync(random, ct); return false; }
    }

    [HarmonyPatch(typeof(ChooseARelicScreenHandler), "HandleAsync")]
    internal static class Patch_RelicSelect
    {
        private static bool Prefix(Rng random, CancellationToken ct, ref Task __result)
        { __result = MoreScreens.RelicSelectAsync(random, ct); return false; }
    }
}
// Character selection lives in Driver.PlayMainMenuAsync + Patch_MainMenu (an earlier
// approach that patched the shared generic Rng.NextItem broke run-start, so it was replaced
// by an isolated patch of the menu navigation).
