using System;
using HarmonyLib;
using MegaCrit.Sts2.Core.Modding;
using MegaCrit.Sts2.Core.Nodes;

namespace SpireLink
{
    // Entry point. ModManager calls this static method (named in the attribute) once at load.
    // Because we declare a ModInitializer, we must call Harmony.PatchAll ourselves.
    [ModInitializer("Init")]
    public static class SpireLinkMod
    {
        public const int DefaultPort = 5555;

        // SPIRELINK_PORT lets a supervisor run several game instances side by side.
        public static int Port
        {
            get
            {
                var env = Environment.GetEnvironmentVariable("SPIRELINK_PORT");
                return int.TryParse(env, out int p) && p > 0 ? p : DefaultPort;
            }
        }

        public static void Init()
        {
            Log.Info("Initializing (build " + BuildInfo.Stamp + ") ...");
            try
            {
                var harmony = new Harmony("spirelink");
                harmony.PatchAll(typeof(SpireLinkMod).Assembly);
                Log.Info("Harmony patches applied.");
            }
            catch (Exception e)
            {
                Log.Error("Harmony PatchAll failed: " + e);
            }

            try
            {
                SpireServer.Start(Port);
                Log.Info("TCP control server listening on 127.0.0.1:" + Port);
            }
            catch (Exception e)
            {
                Log.Error("Server start failed: " + e);
            }
        }
    }

    // Unlock non-release-only features (autoslay path, dev console, etc.).
    [HarmonyPatch(typeof(NGame), "IsReleaseGame")]
    public static class Patch_IsReleaseGame
    {
        public static bool Prefix(ref bool __result)
        {
            __result = false;
            return false; // skip original
        }
    }

    internal static class BuildInfo
    {
        public const string Stamp = "0.1.0";
    }
}
