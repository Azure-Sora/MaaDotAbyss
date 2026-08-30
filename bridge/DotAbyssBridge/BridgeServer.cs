using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using BepInEx;
using UnityEngine;
using IOPath = System.IO.Path;

namespace DotAbyssBridge;

/// <summary>
/// 极简 HTTP 服务（TcpListener 手写，绕开 HttpListener 的 URLACL 权限问题）：
/// POST /命令 + JSON 体 → JSON 响应；GET /ping；/screenshot 返回 image/png。
/// </summary>
public sealed class BridgeServer
{
    private readonly BridgeBehaviour _owner;
    private readonly int _port;
    private TcpListener _listener;
    private Thread _thread;
    private volatile bool _running;

    public BridgeServer(BridgeBehaviour owner, int port)
    {
        _owner = owner;
        _port = port;
    }

    public int Port => _port;

    public void Start()
    {
        _listener = new TcpListener(IPAddress.Loopback, _port);
        _listener.Start();
        _running = true;
        _thread = new Thread(Loop) { IsBackground = true, Name = "DotAbyssBridge" };
        _thread.Start();
        WriteDiscovery();
        BridgePlugin.Log.LogInfo($"bridge listening on 127.0.0.1:{_port}");
    }

    public void Stop()
    {
        _running = false;
        try { _listener?.Stop(); } catch { /* 收尾阶段无所谓 */ }
    }

    private void WriteDiscovery()
    {
        try
        {
            var json = "{\"port\":" + _port
                     + ",\"pid\":" + Process.GetCurrentProcess().Id
                     + ",\"unity\":\"" + Application.unityVersion
                     + "\",\"plugin\":\"" + BridgePlugin.VERSION + "\"}";
            File.WriteAllText(IOPath.Combine(Paths.BepInExRootPath, "bridge.json"), json);
        }
        catch (Exception e)
        {
            BridgePlugin.Log.LogWarning($"discovery 写入失败: {e.Message}");
        }
    }

    private void Loop()
    {
        while (_running)
        {
            TcpClient client;
            try { client = _listener.AcceptTcpClient(); }
            catch { break; }
            Task.Run(() => Serve(client));
        }
    }

    private void Serve(TcpClient client)
    {
        try
        {
            using (client)
            {
                var stream = client.GetStream();
                stream.ReadTimeout = 5000;
                var req = ReadRequest(stream);
                if (req is not { } parsed) return;
                var (code, body, contentType) = Handle(parsed.path, parsed.body);
                var head = "HTTP/1.1 " + code + "\r\nContent-Type: " + contentType
                         + "; charset=utf-8\r\nContent-Length: " + body.Length
                         + "\r\nConnection: close\r\n\r\n";
                var headBytes = Encoding.ASCII.GetBytes(head);
                stream.Write(headBytes, 0, headBytes.Length);
                stream.Write(body, 0, body.Length);
                stream.Flush();
            }
        }
        catch (Exception e)
        {
            BridgePlugin.Log.LogWarning($"serve: {e.Message}");
        }
    }

    private static (string path, byte[] body)? ReadRequest(NetworkStream stream)
    {
        var buf = new MemoryStream();
        var chunk = new byte[4096];
        int headerEnd = -1;
        while (headerEnd < 0)
        {
            int n = stream.Read(chunk, 0, chunk.Length);
            if (n <= 0) return null;
            buf.Write(chunk, 0, n);
            headerEnd = FindHeaderEnd(buf);
            if (buf.Length > 1 << 20) return null;
        }
        var bytes = buf.ToArray();
        var headerText = Encoding.ASCII.GetString(bytes, 0, headerEnd);
        var firstLine = headerText.Split('\r')[0];
        var parts = firstLine.Split(' ');
        if (parts.Length < 2) return null;
        var contentLength = 0;
        foreach (var line in headerText.Split('\n'))
        {
            var idx = line.IndexOf("Content-Length:", StringComparison.OrdinalIgnoreCase);
            if (idx >= 0)
                int.TryParse(line.Substring(idx + 15).Trim(), out contentLength);
        }
        var bodyStart = headerEnd + 4;
        var have = bytes.Length - bodyStart;
        while (have < contentLength)
        {
            int n = stream.Read(chunk, 0, Math.Min(chunk.Length, contentLength - have));
            if (n <= 0) break;
            buf.Write(chunk, 0, n);
            have += n;
        }
        bytes = buf.ToArray();
        var body = new byte[Math.Max(0, have)];
        Array.Copy(bytes, bodyStart, body, 0, body.Length);
        return (parts[1], body);
    }

    private static int FindHeaderEnd(MemoryStream buf)
    {
        var bytes = buf.GetBuffer();
        int len = (int)buf.Length;
        for (int i = 0; i + 3 < len; i++)
            if (bytes[i] == 13 && bytes[i + 1] == 10 && bytes[i + 2] == 13 && bytes[i + 3] == 10)
                return i;
        return -1;
    }

    private (int, byte[], string) Handle(string path, byte[] body)
    {
        var cmd = path.Trim('/').ToLowerInvariant();
        try
        {
            switch (cmd)
            {
                case "ping":
                    return (200, Json("{\"pong\":true,\"pid\":" + Process.GetCurrentProcess().Id
                        + ",\"unity\":\"" + Application.unityVersion
                        + "\",\"product\":\"" + Application.productName
                        + "\",\"plugin\":\"" + BridgePlugin.VERSION
                        + "\",\"focused\":" + (Application.isFocused ? "true" : "false") + "}"), "application/json");

                case "screenshot":
                {
                    var task = _owner.CapturePngAsync();
                    if (!task.Wait(15000))
                        return (504, JsonError("截图超时（主线程阻塞或未渲染）"), "application/json");
                    return (200, task.Result, "image/png");
                }

                case "ui":
                    return (200, Json(RunOnMain(() => _owner.BuildUiJson())), "application/json");

                case "click":
                {
                    var p = ParseJson(body);
                    string path2 = p.TryGetValue("path", out var v) ? v : null;
                    if (string.IsNullOrEmpty(path2))
                        return (400, JsonError("缺少 path"), "application/json");
                    string clicked = RunOnMain(() => _owner.ClickByPath(path2));
                    return (200, Json("{\"clicked\":true,\"path\":\"" + BridgeBehaviour.Esc(clicked) + "\"}"), "application/json");
                }

                case "click_at":
                {
                    var p = ParseJson(body);
                    int x = int.Parse(p.TryGetValue("x", out var vx) ? vx : "-1");
                    int y = int.Parse(p.TryGetValue("y", out var vy) ? vy : "-1");
                    if (x < 0 || y < 0)
                        return (400, JsonError("缺少 x/y"), "application/json");
                    string clicked = RunOnMain(() => _owner.ClickAtPoint(x, y));
                    return (200, Json("{\"clicked\":true,\"path\":\"" + BridgeBehaviour.Esc(clicked) + "\"}"), "application/json");
                }

                default:
                    return (404, JsonError("未知命令: " + cmd), "application/json");
            }
        }
        catch (AggregateException ae)
        {
            var inner = ae.InnerException?.Message ?? ae.Message;
            if (inner.Contains("未找到") || inner.Contains("没有可交互"))
                return (404, JsonError(inner), "application/json");
            return (500, JsonError(inner), "application/json");
        }
        catch (Exception e)
        {
            return (500, JsonError(e.Message), "application/json");
        }
    }

    private string RunOnMain(Func<string> produce)
    {
        var tcs = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);
        _owner.MainThreadJobs.Enqueue(() =>
        {
            try { tcs.SetResult(produce()); }
            catch (Exception e) { tcs.SetException(e); }
        });
        if (!tcs.Task.Wait(15000))
            throw new TimeoutException("主线程 15s 未响应");
        return tcs.Task.Result;
    }

    private static System.Collections.Generic.Dictionary<string, string> ParseJson(byte[] body)
    {
        // 只解析扁平 {key: string|number} —— 桥的请求参数够用
        var dict = new System.Collections.Generic.Dictionary<string, string>();
        var s = Encoding.UTF8.GetString(body ?? Array.Empty<byte>());
        int i = 0, n = s.Length;
        while (i < n)
        {
            int keyStart = s.IndexOf('"', i);
            if (keyStart < 0) break;
            int keyEnd = s.IndexOf('"', keyStart + 1);
            if (keyEnd < 0) break;
            int colon = s.IndexOf(':', keyEnd + 1);
            if (colon < 0) break;
            int valStart = -1;
            for (int j = colon + 1; j < n; j++)
                if (s[j] != ' ' && s[j] != '\t') { valStart = j; break; }
            if (valStart < 0) break;
            string key = s.Substring(keyStart + 1, keyEnd - keyStart - 1);
            if (valStart < n && s[valStart] == '"')
            {
                int ve = s.IndexOf('"', valStart + 1);
                if (ve < 0) break;
                dict[key] = s.Substring(valStart + 1, ve - valStart - 1);
                i = ve + 1;
            }
            else
            {
                int ve = valStart;
                while (ve < n && s[ve] != ',' && s[ve] != '}') ve++;
                dict[key] = s.Substring(valStart, ve - valStart).Trim();
                i = ve;
            }
        }
        return dict;
    }

    private static byte[] Json(string s) => Encoding.UTF8.GetBytes(s);
    private static byte[] JsonError(string msg) => Encoding.UTF8.GetBytes("{\"error\":\"" + BridgeBehaviour.Esc(msg) + "\"}");
}
