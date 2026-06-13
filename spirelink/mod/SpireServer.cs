using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading;
using Godot;

namespace SpireLink
{
    // Line-delimited JSON request/response server.
    //
    //   request:  {"id": <int>, "cmd": "<name>", "args": { ... }}\n
    //   response: {"id": <int>, "ok": true,  "data": { ... }}\n
    //          or {"id": <int>, "ok": false, "error": "<msg>"}\n
    //
    // All game-state access is marshaled onto the Godot main thread via Callable.CallDeferred,
    // which is the supported way to hop threads in Godot. Socket I/O runs on background threads.
    internal static class SpireServer
    {
        private static volatile bool _running;
        private static TcpListener _listener;

        public static void Start(int port)
        {
            if (_running) return;
            _running = true;
            var t = new Thread(() => ListenLoop(port)) { IsBackground = true, Name = "SpireLink-Accept" };
            t.Start();
        }

        private static void ListenLoop(int port)
        {
            try
            {
                _listener = new TcpListener(IPAddress.Loopback, port);
                _listener.Server.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
                _listener.Start();
            }
            catch (Exception e)
            {
                Log.Error("Could not bind 127.0.0.1:" + port + " : " + e.Message);
                _running = false;
                return;
            }

            while (_running)
            {
                TcpClient client;
                try { client = _listener.AcceptTcpClient(); }
                catch (Exception) { break; }
                var ct = new Thread(() => HandleClient(client)) { IsBackground = true, Name = "SpireLink-Client" };
                ct.Start();
            }
        }

        private static void HandleClient(TcpClient client)
        {
            Log.Info("Client connected.");
            try
            {
                using (client)
                using (var stream = client.GetStream())
                using (var reader = new StreamReader(stream, Encoding.UTF8))
                using (var writer = new StreamWriter(stream, new UTF8Encoding(false)) { AutoFlush = true, NewLine = "\n" })
                {
                    string line;
                    while ((line = reader.ReadLine()) != null)
                    {
                        if (line.Length == 0) continue;
                        string response = Dispatch(line);
                        writer.WriteLine(response);
                    }
                }
            }
            catch (Exception e)
            {
                Log.Error("Client loop ended: " + e.Message);
            }
            Log.Info("Client disconnected.");
        }

        private static string Dispatch(string line)
        {
            JsonNode req = null;
            int id = 0;
            try
            {
                req = JsonNode.Parse(line);
                if (req?["id"] != null) id = req["id"].GetValue<int>();
                string cmd = (string)(req?["cmd"]) ?? "";
                JsonObject args = req?["args"] as JsonObject;

                JsonNode data = cmd == "observe"
                    ? ObserveMaybeWait(args)
                    : OnMain(() => Commands.Handle(cmd, args));
                var ok = new JsonObject { ["id"] = id, ["ok"] = true, ["data"] = data };
                return ok.ToJsonString();
            }
            catch (Exception e)
            {
                var err = new JsonObject { ["id"] = id, ["ok"] = false, ["error"] = e.Message };
                Log.Error("cmd error: " + e);
                return err.ToJsonString();
            }
        }

        // Long-poll observe: `observe {wait_s: N}` blocks (on this socket thread) until the
        // phase is something other than "busy" — i.e. a decision is pending, the run ended,
        // or we're at the menu — or the wait expires. Saves clients a poll-spin (and an MCP
        // agent a tool round-trip) during animations/enemy turns.
        private static JsonNode ObserveMaybeWait(JsonObject args)
        {
            double waitS = 0;
            try { if (args?["wait_s"] != null) waitS = (double)args["wait_s"]; } catch { }
            waitS = Math.Min(waitS, 120);
            var deadline = DateTime.UtcNow.AddSeconds(waitS);
            while (true)
            {
                JsonNode data = OnMain(() => Commands.Handle("observe", null));
                if ((string)data?["phase"] != "busy" || DateTime.UtcNow >= deadline)
                    return data;
                Thread.Sleep(150);
            }
        }

        // Run fn on the Godot main thread and block the calling (socket) thread for the result.
        private sealed class Box { public JsonNode Value; public Exception Error; }

        public static int MainThreadId;

        public static JsonNode OnMain(Func<JsonNode> fn, int timeoutMs = 30000)
        {
            var box = new Box();
            using (var done = new ManualResetEventSlim(false))
            {
                Callable.From(() =>
                {
                    MainThreadId = System.Threading.Thread.CurrentThread.ManagedThreadId;
                    try { box.Value = fn(); }
                    catch (Exception e) { box.Error = e; }
                    finally { done.Set(); }
                }).CallDeferred();

                if (!done.Wait(timeoutMs))
                    throw new TimeoutException("main-thread job timed out (game not running / paused?)");
            }
            if (box.Error != null) throw box.Error;
            return box.Value;
        }
    }
}
