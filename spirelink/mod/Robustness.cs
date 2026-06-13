using System;
using HarmonyLib;
using MegaCrit.Sts2.Core.AutoSlay;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;

namespace SpireLink
{
    // AutoSlayer was built for fast random self-play: it wraps the whole run in a
    // 25-minute cap (AutoSlayConfig.runTimeout), each room/screen handler in a
    // 30s-2min cap (handler.Timeout via WaitHelper.WithTimeout), and a Watchdog that
    // aborts after 30s without "progress". A deliberating client legitimately sits on
    // a pending decision for minutes, which those QA limits would kill as a failed run
    // (observed: a pending combat_reward decision aborted the run after ~29s).
    //
    // Genuine wedges are still caught: every internal WaitHelper.Until keeps its own
    // short timeout, the watchdog still runs whenever no decision is pending, and
    // `abandon_run` gives the client an explicit escape hatch.

    // While a decision is pending, waiting is progress — keep resetting the watchdog.
    [HarmonyPatch(typeof(Watchdog), "Check")]
    internal static class Patch_Watchdog
    {
        private static bool Prefix(Watchdog __instance)
        {
            if (Decisions.HasPending)
            {
                __instance.Reset("awaiting client decision");
                return false;
            }
            return true;
        }
    }

    // WithTimeout is used only for the run/room/screen umbrella caps (inner waits use
    // Until); stretch them so client thinking time is unbounded.
    [HarmonyPatch(typeof(WaitHelper), "WithTimeout")]
    internal static class Patch_UmbrellaTimeouts
    {
        private static void Prefix(ref TimeSpan timeout)
        {
            timeout = TimeSpan.FromDays(2);
        }
    }

    // Record why a run failed so observe() can surface it as last_error.
    [HarmonyPatch(typeof(AutoSlayLog), "RunFailed")]
    internal static class Patch_RunFailed
    {
        private static void Prefix(string seed, Exception ex)
        {
            Driver.LastError = ex?.Message;
        }
    }
}
