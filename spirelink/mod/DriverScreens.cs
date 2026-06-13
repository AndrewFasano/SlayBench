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
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Nodes.Cards.Holders;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Events;
using MegaCrit.Sts2.Core.Nodes.Events.Custom;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.RestSite;
using MegaCrit.Sts2.Core.Nodes.Rooms;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Nodes.Screens.Shops;
using MegaCrit.Sts2.Core.Nodes.Screens.TreasureRoomRelic;
using MegaCrit.Sts2.Core.Random;
using MegaCrit.Sts2.Core.Runs;

namespace SpireLink
{
    // Client-driven implementations of the non-combat screens, replacing AutoSlayer's random
    // choices. Each mirrors the structure of the corresponding game handler but routes the
    // decision through the connected client.
    internal static class Screens
    {
        private static string Safe(Func<string> f, string fallback)
        {
            try
            {
                var s = Snapshot.CleanText(f());
                return string.IsNullOrEmpty(s) ? fallback : s;
            }
            catch { return fallback; }
        }

        // ---- card reward: pick one offered card, or skip ----
        public static async Task CardRewardAsync(Rng random, CancellationToken ct)
        {
            var screen = AutoSlayer.GetCurrentScreen<NCardRewardSelectionScreen>();
            await Task.Delay(400, ct);
            var holders = UiHelper.FindAll<NCardHolder>(screen);
            if (holders.Count == 0) { AutoSlayLog.Warn("[SpireLink] no card holders"); return; }

            var options = new JsonArray();
            for (int i = 0; i < holders.Count; i++)
            {
                int ii = i;
                JsonObject opt;
                try { opt = (JsonObject)Snapshot.CardJson(holders[ii].CardModel); } catch { opt = new JsonObject(); }
                opt["index"] = i;
                opt["label"] = Safe(() => holders[ii].CardModel?.Id.ToString(), "card " + ii);
                options.Add(opt);
            }
            var skip = UiHelper.FindFirst<NChoiceSelectionSkipButton>(screen);
            var extra = new JsonObject { ["can_skip"] = skip != null };
            var choice = await Decisions.Ask("card_reward", "Choose a card to add" + (skip != null ? " (or skip)" : "") + ".", options, extra);

            if (choice["skip"] != null && (bool)choice["skip"] && skip != null)
            {
                await UiHelper.Click(skip);
            }
            else
            {
                int oi = choice["option_index"] != null ? (int)choice["option_index"] : 0;
                if (oi < 0 || oi >= holders.Count) oi = 0;
                holders[oi].EmitSignal(NCardHolder.SignalName.Pressed, holders[oi]);
            }
            await WaitHelper.Until(() => !GodotObject.IsInstanceValid(screen) || !screen.IsVisibleInTree(), ct,
                TimeSpan.FromSeconds(10L), "Card reward screen did not close");
        }

        // ---- event: choose an option (handles event-triggered combat via the client too) ----
        public static async Task EventAsync(Rng random, CancellationToken ct)
        {
            Node root = ((SceneTree)Engine.GetMainLoop()).Root;
            Node eventRoom = await WaitHelper.ForNode<Node>(root, "/root/Game/RootSceneContainer/Run/RoomContainer/EventRoom", ct, null);

            // Special event layouts (mirrors AutoSlayer's WaitForEventOptions): Ancient
            // events hide their options behind a dialogue that must be clicked through;
            // FakeMerchant has no options at all, just a proceed button.
            var ancient = UiHelper.FindFirst<NAncientEventLayout>(eventRoom);
            if (ancient != null)
                await ClickThroughAncientDialogue(ancient, ct);
            var fakeMerchant = UiHelper.FindFirst<NFakeMerchant>(eventRoom);
            if (fakeMerchant != null)
            {
                AutoSlayLog.Action("[SpireLink] FakeMerchant event: clicking proceed");
                NProceedButton fmProceed = null;
                await WaitHelper.Until(() =>
                {
                    fmProceed = UiHelper.FindFirst<NProceedButton>(fakeMerchant);
                    return fmProceed != null && fmProceed.IsEnabled && fmProceed.Visible;
                }, ct, TimeSpan.FromSeconds(10L), "FakeMerchant proceed button not available");
                await UiHelper.Click(fmProceed);
                return;
            }

            int guard = 0;
            while (guard++ < 50)
            {
                ct.ThrowIfCancellationRequested();
                if (!GodotObject.IsInstanceValid(eventRoom) || !eventRoom.IsInsideTree()) break;

                await WaitHelper.Until(() =>
                    UiHelper.FindAll<NEventOptionButton>(eventRoom).Any(o => !o.Option.IsLocked)
                    || CombatManager.Instance.IsInProgress
                    || (NOverlayStack.Instance?.ScreenCount > 0)
                    || !GodotObject.IsInstanceValid(eventRoom) || !eventRoom.IsInsideTree(),
                    ct, TimeSpan.FromSeconds(30L), "Event options not loaded");

                if (CombatManager.Instance.IsInProgress) { await Driver.CombatAsync(random, ct); continue; }
                if (NOverlayStack.Instance?.ScreenCount > 0) break; // drain loop handles overlay
                if (!GodotObject.IsInstanceValid(eventRoom) || !eventRoom.IsInsideTree()) break;

                var btns = UiHelper.FindAll<NEventOptionButton>(eventRoom).Where(o => !o.Option.IsLocked).ToList();
                if (btns.Count == 0) break;

                var options = new JsonArray();
                for (int i = 0; i < btns.Count; i++)
                {
                    int ii = i;
                    var opt = new JsonObject { ["index"] = i, ["label"] = Safe(() => btns[ii].Option.Title.GetFormattedText(), "option " + ii) };
                    var detail = Safe(() => btns[ii].Option.Description.GetFormattedText(), null);
                    if (detail != null) opt["detail"] = detail;
                    options.Add(opt);
                }
                // Include the event's title + current body text so the agent isn't
                // choosing between option labels blind.
                JsonObject extra = null;
                try
                {
                    if (Snapshot.EventJson(RunManager.Instance.DebugOnlyGetState()) is JsonObject ev)
                        extra = new JsonObject { ["event"] = ev };
                }
                catch { }
                var choice = await Decisions.Ask("event", "Choose an event option.", options, extra);
                int oi = choice["option_index"] != null ? (int)choice["option_index"] : 0;
                if (oi < 0 || oi >= btns.Count) oi = 0;
                var chosen = btns[oi];
                bool isProceed = chosen.Option.IsProceed;
                await UiHelper.Click(chosen);
                if (isProceed)
                {
                    await WaitHelper.Until(() => !GodotObject.IsInstanceValid(eventRoom) || !eventRoom.IsInsideTree() || (NMapScreen.Instance?.IsOpen ?? false),
                        ct, TimeSpan.FromSeconds(5L), "Event did not close after proceed");
                    break;
                }
                await Task.Delay(300, ct);
            }
        }

        // Ancient events show dialogue lines that must each be clicked before the real
        // options appear (mirrors AutoSlayer.HandleAncientEventDialogue).
        private static async Task ClickThroughAncientDialogue(NAncientEventLayout ancient, CancellationToken ct)
        {
            AutoSlayLog.Action("[SpireLink] Ancient event: clicking through dialogue");
            int clicks = 0;
            while (clicks < 50)
            {
                ct.ThrowIfCancellationRequested();
                if (!GodotObject.IsInstanceValid(ancient)) return;
                if (UiHelper.FindAll<NEventOptionButton>(ancient).Any(b => b.IsEnabled && !b.Option.IsLocked))
                    break;
                NButton hitbox = ancient.GetNodeOrNull<NButton>("%DialogueHitbox");
                if (hitbox == null || !hitbox.Visible || !hitbox.IsEnabled)
                {
                    await Task.Delay(100, ct);
                    continue;
                }
                hitbox.EmitSignal(NClickableControl.SignalName.Released, hitbox);
                clicks++;
                await Task.Delay(500, ct);
            }
            await WaitHelper.Until(() => !GodotObject.IsInstanceValid(ancient)
                    || UiHelper.FindAll<NEventOptionButton>(ancient).Any(b => b.IsEnabled && !b.Option.IsLocked),
                ct, TimeSpan.FromSeconds(10L), "Ancient event options did not appear after dialogue");
        }

        // ---- rest site: choose an action ----
        public static async Task RestAsync(Rng random, CancellationToken ct)
        {
            Node root = ((SceneTree)Engine.GetMainLoop()).Root;
            NRestSiteRoom room = await WaitHelper.ForNode<NRestSiteRoom>(root, "/root/Game/RootSceneContainer/Run/RoomContainer/RestSiteRoom", ct, null);
            var btns = UiHelper.FindAll<NRestSiteButton>(room).Where(b => b.Option.IsEnabled).ToList();
            if (btns.Count == 0) { AutoSlayLog.Warn("[SpireLink] no rest options"); return; }

            var options = new JsonArray();
            for (int i = 0; i < btns.Count; i++)
            {
                int ii = i;
                options.Add(new JsonObject { ["index"] = i, ["label"] = Safe(() => btns[ii].Option.Title.GetFormattedText(), btns[ii].Option.GetType().Name) });
            }
            var choice = await Decisions.Ask("rest", "Choose a rest-site action.", options);
            int oi = choice["option_index"] != null ? (int)choice["option_index"] : 0;
            if (oi < 0 || oi >= btns.Count) oi = 0;
            await UiHelper.Click(btns[oi]);

            var proceed = room.ProceedButton;
            await WaitHelper.Until(() => proceed.IsEnabled || (NOverlayStack.Instance?.ScreenCount > 0), ct,
                TimeSpan.FromSeconds(10L), "Rest option did not respond");
            if (NOverlayStack.Instance?.ScreenCount > 0) return; // e.g. smith opens a card-select overlay
            await UiHelper.Click(proceed);
        }

        // ---- treasure: open chest, take the relic or skip ----
        public static async Task TreasureAsync(Rng random, CancellationToken ct)
        {
            Node root = ((SceneTree)Engine.GetMainLoop()).Root;
            NTreasureRoom room = await WaitHelper.ForNode<NTreasureRoom>(root, "/root/Game/RootSceneContainer/Run/RoomContainer/TreasureRoom", ct, null);
            var chest = room.GetNode<NClickableControl>("Chest");
            await UiHelper.Click(chest);
            await Task.Delay(1000, ct);

            var holders = UiHelper.FindAll<NTreasureRoomRelicHolder>(room);
            string relic = holders.Count > 0 ? Safe(() => holders[0].Relic.Model.Id.ToString(), "relic") : "relic";
            var extra = new JsonObject { ["relic"] = relic };
            try { if (holders.Count > 0) extra["relic_info"] = Snapshot.RelicJson(holders[0].Relic.Model); } catch { }
            var options = new JsonArray
            {
                new JsonObject { ["index"] = 0, ["label"] = "Take " + relic },
                new JsonObject { ["index"] = 1, ["label"] = "Skip" },
            };
            var choice = await Decisions.Ask("treasure", "A treasure chest. Take the relic or skip.", options, extra);
            bool take = choice["take"] != null ? (bool)choice["take"]
                       : (choice["option_index"] != null ? (int)choice["option_index"] : 0) == 0;
            if (take)
            {
                foreach (var h in holders)
                {
                    if (h.IsEnabled && h.Visible) { await UiHelper.Click(h); await Task.Delay(500, ct); }
                }
            }
            var proceed = room.ProceedButton;
            await WaitHelper.Until(() => proceed.IsEnabled, ct, TimeSpan.FromSeconds(5L), "Treasure proceed not enabled");
            await UiHelper.Click(proceed);
        }

        // ---- shop: buy items (repeatedly) or leave ----
        public static async Task ShopAsync(Rng random, CancellationToken ct)
        {
            Node root = ((SceneTree)Engine.GetMainLoop()).Root;
            NMerchantRoom room = await WaitHelper.ForNode<NMerchantRoom>(root, "/root/Game/RootSceneContainer/Run/RoomContainer/MerchantRoom", ct, null);
            room.OpenInventory();
            await Task.Delay(500, ct);

            int guard = 0;
            while (guard++ < 50)
            {
                ct.ThrowIfCancellationRequested();
                // Card removal is included: its purchase runs CardSelectCmd.FromDeckForRemoval,
                // which our AutoSlayCardSelector patch turns into a card_select decision.
                var slots = room.Inventory.GetAllSlots()
                    .Where(s => s.Entry.IsStocked).ToList();
                var options = new JsonArray();
                for (int i = 0; i < slots.Count; i++)
                {
                    var e = slots[i].Entry;
                    var opt = MerchantInfo(e);
                    opt["index"] = i;
                    opt["label"] = MerchantLabel(e);
                    opt["cost"] = e.Cost;
                    opt["affordable"] = e.EnoughGold;
                    options.Add(opt);
                }
                options.Add(new JsonObject { ["index"] = 100, ["label"] = "Leave" });
                int gold = 0;
                try { gold = LocalContext.GetMe(RunManager.Instance.DebugOnlyGetState())?.Gold ?? 0; } catch { }
                var choice = await Decisions.Ask("shop", "Merchant. Buy an item or leave.", options, new JsonObject { ["gold"] = gold });

                bool leave = (choice["leave"] != null && (bool)choice["leave"])
                    || ChoiceIndex(choice) == 100;
                if (leave) break;
                int bi = ChoiceIndex(choice);
                if (bi >= 0 && bi < slots.Count)
                {
                    var e = slots[bi].Entry;
                    if (e.EnoughGold) { await e.OnTryPurchaseWrapper(room.Inventory.Inventory); await Task.Delay(300, ct); }
                }
            }

            var back = UiHelper.FindFirst<NBackButton>(room);
            if (back != null) { await UiHelper.Click(back); await Task.Delay(300, ct); }
            await UiHelper.Click(room.ProceedButton);
        }

        private static int ChoiceIndex(JsonObject c)
        {
            if (c["buy_index"] != null) return (int)c["buy_index"];
            if (c["option_index"] != null) return (int)c["option_index"];
            return -1;
        }

        private static string MerchantLabel(MerchantEntry e)
        {
            try
            {
                if (e is MerchantCardEntry mc) return mc.CreationResult?.Card.Id.ToString() ?? "card";
                if (e is MerchantRelicEntry mr) return mr.Model?.Id.ToString() ?? "relic";
                if (e is MerchantPotionEntry mp) return mp.Model?.Id.ToString() ?? "potion";
                if (e is MerchantCardRemovalEntry) return "CARD_REMOVAL";
            }
            catch { }
            return e.GetType().Name;
        }

        // Name + rules text for a shop entry (what a human reads before buying).
        private static JsonObject MerchantInfo(MerchantEntry e)
        {
            try
            {
                if (e is MerchantCardEntry mc && mc.CreationResult?.Card != null)
                    return (JsonObject)Snapshot.CardJson(mc.CreationResult.Card);
                if (e is MerchantRelicEntry mr && mr.Model != null)
                    return (JsonObject)Snapshot.RelicJson(mr.Model);
                if (e is MerchantPotionEntry mp && mp.Model != null)
                    return (JsonObject)Snapshot.PotionJson(mp.Model);
                if (e is MerchantCardRemovalEntry)
                    return new JsonObject { ["text"] = "Remove a card of your choice from your deck (opens a card_select decision)." };
            }
            catch { }
            return new JsonObject();
        }
    }

    // ---- Harmony patches for the non-combat screens ----

    [HarmonyPatch(typeof(CardRewardScreenHandler), "HandleAsync")]
    internal static class Patch_CardReward
    {
        private static bool Prefix(Rng random, CancellationToken ct, ref Task __result)
        { __result = Screens.CardRewardAsync(random, ct); return false; }
    }

    [HarmonyPatch(typeof(EventRoomHandler), "HandleAsync")]
    internal static class Patch_Event
    {
        private static bool Prefix(Rng random, CancellationToken ct, ref Task __result)
        { __result = Screens.EventAsync(random, ct); return false; }
    }

    [HarmonyPatch(typeof(RestSiteRoomHandler), "HandleAsync")]
    internal static class Patch_Rest
    {
        private static bool Prefix(Rng random, CancellationToken ct, ref Task __result)
        { __result = Screens.RestAsync(random, ct); return false; }
    }

    [HarmonyPatch(typeof(TreasureRoomHandler), "HandleAsync")]
    internal static class Patch_Treasure
    {
        private static bool Prefix(Rng random, CancellationToken ct, ref Task __result)
        { __result = Screens.TreasureAsync(random, ct); return false; }
    }

    [HarmonyPatch(typeof(ShopRoomHandler), "HandleAsync")]
    internal static class Patch_Shop
    {
        private static bool Prefix(Rng random, CancellationToken ct, ref Task __result)
        { __result = Screens.ShopAsync(random, ct); return false; }
    }
}
