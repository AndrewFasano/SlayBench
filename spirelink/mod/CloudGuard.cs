using System;
using System.Threading.Tasks;
using HarmonyLib;
using MegaCrit.Sts2.Core.Platform.Steam;

namespace SpireLink
{
    // Safety: this mod is a testing/eval harness running against an isolated copy of the game.
    // It must NEVER write to the real player's Steam Cloud. These patches no-op every cloud
    // mutation in SteamRemoteSaveStore (local saves under the isolated HOME are unaffected).
    internal static class CloudGuard
    {
        private static bool _warned;
        private static void Note(string what)
        {
            if (!_warned) { Log.Info("Steam Cloud writes are BLOCKED by SpireLink (isolation)."); _warned = true; }
        }

        [HarmonyPatch(typeof(SteamRemoteSaveStore), "WriteFile", new Type[] { typeof(string), typeof(byte[]) })]
        internal static class Patch_WriteFile
        {
            private static bool Prefix() { Note("WriteFile"); return false; }
        }

        [HarmonyPatch(typeof(SteamRemoteSaveStore), "WriteFileAsync", new Type[] { typeof(string), typeof(byte[]) })]
        internal static class Patch_WriteFileAsync
        {
            private static bool Prefix(ref Task __result) { Note("WriteFileAsync"); __result = Task.CompletedTask; return false; }
        }

        [HarmonyPatch(typeof(SteamRemoteSaveStore), "DeleteFile", new Type[] { typeof(string) })]
        internal static class Patch_DeleteFile
        {
            private static bool Prefix() { Note("DeleteFile"); return false; }
        }

        [HarmonyPatch(typeof(SteamRemoteSaveStore), "RenameFile", new Type[] { typeof(string), typeof(string) })]
        internal static class Patch_RenameFile
        {
            private static bool Prefix() { Note("RenameFile"); return false; }
        }
    }
}
