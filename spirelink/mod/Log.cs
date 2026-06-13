using System;
using System.IO;
using Godot;

namespace SpireLink
{
    // Lightweight logger: writes to the Godot console (captured in godot.log) and to a
    // dedicated spirelink.log in the user data dir, so issues are easy to surface.
    internal static class Log
    {
        private static readonly object _gate = new object();
        private static string _path;

        private static string Path
        {
            get
            {
                if (_path == null)
                {
                    try { _path = OS.GetUserDataDir() + "/spirelink.log"; }
                    catch { _path = "spirelink.log"; }
                }
                return _path;
            }
        }

        public static void Info(string msg)
        {
            string line = "[SpireLink] " + msg;
            try { GD.Print(line); } catch { }
            try
            {
                lock (_gate) { File.AppendAllText(Path, DateTime.Now.ToString("HH:mm:ss.fff") + " " + msg + "\n"); }
            }
            catch { }
        }

        public static void Error(string msg)
        {
            string line = "[SpireLink][ERR] " + msg;
            try { GD.PrintErr(line); } catch { }
            try
            {
                lock (_gate) { File.AppendAllText(Path, DateTime.Now.ToString("HH:mm:ss.fff") + " ERR " + msg + "\n"); }
            }
            catch { }
        }
    }
}
