// SignalingServer.cs —— 信令中继 + 媒体页服务
//
// ============================================================================
// 为什么自己写一个，而不是复用 Python 的 webhost.py
// ============================================================================
// Python 那边的 `/signal` 中继（src/swiftdrop/signaling.py）已经实现了完整的
// WebSocket 帧协议，逻辑是对的。但如果由它来服务媒体页，就会引入两个问题：
//
//   1) 媒体页必须从 **localhost 的 http 源** 加载，才能既满足 getUserMedia 的
//      安全上下文要求、又能用 ws:// 连信令（同源、无混合内容拦截）。
//      页面从 https 虚拟主机加载时连 ws:// 会被 Chromium 当混合内容拦掉，
//      而用 wss:// 就得给 localhost 签证书 —— 不值得。
//   2) 两个进程之间的额外一跳只会增加故障面。
//
// 所以 C# 侧自带一个小的 HTTP + WebSocket 服务器：同一端口同时提供
// 媒体页（http）与信令（ws），完全同源。
//
// 不用的东西：不动 webhost.py，文件传输那条 TCP 链路保持原样。
//
// ============================================================================
// 依赖：只用 System.Net.Sockets + System.Text，不引第三方 WebSocket 库。
// 手写的关键部分只有：HTTP 请求行解析、WebSocket 握手、帧编解码。
// 这三样加起来不到 200 行，换来"零依赖 + 完全可控"。
// ============================================================================

using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace ZongxianVoice;

/// <summary>一个已连接的 WebSocket 信令客户端。</summary>
internal sealed class SignalPeer
{
    public required string Id { get; init; }
    public required string Room { get; init; }
    public required NetworkStream Stream { get; init; }
    public required object SendLock { get; init; }

    /// <summary>对端标识，用于日志与"谁在说话"这类展示。</summary>
    public string DisplayName { get; set; } = "";
}

/// <summary>
/// 信令服务器：HTTP 提供媒体页，WebSocket 转发 SDP / ICE / 状态。
///
/// 只有中继职责，**不看也不改 SDP**：媒体是端到端加密的（DTLS-SRTP），
/// 服务器能碰到的只有用来建连的元数据。这与仓库既有的信令设计一致。
/// </summary>
public sealed class SignalingServer : IDisposable
{
    private int _port;
    private readonly string _mediaRoot;
    private readonly bool _shareSignaling;
    private TcpListener? _listener;
    private CancellationTokenSource? _cts;
    private readonly ConcurrentDictionary<string, SignalPeer> _peers = new();

    /// <summary>日志回调，接到界面上便于观察信令往来。</summary>
    public event Action<string>? Log;

    public int Port => _port;

    /// <summary>本机信令地址（无论是否共享，都用于本地回环连接）。</summary>
    public string LocalSignalUrl => $"ws://127.0.0.1:{_port}/signal";

    /// <param name="port">监听端口。</param>
    /// <param name="mediaRoot">媒体页所在目录。</param>
    /// <param name="shareSignaling">
    /// true  = 本服务器**同时**承担信令中继职责（此人当房主，对端连过来）。
    /// false = 只提供媒体页，信令走别的服务器。
    ///
    /// 为什么要区分：每个客户端都需要一个本地 HTTP 服务来提供媒体页
    /// （getUserMedia 要求安全上下文，http://127.0.0.1 算安全上下文）。
    /// 但信令**必须所有人连同一个服务器**，否则互相发现不了。
    /// 把两件事拆开，就不会出现"各自跑一个信令服务器、各连各的"这种错。
    /// </param>
    public SignalingServer(int port, string mediaRoot, bool shareSignaling = true)
    {
        _port = port;
        _mediaRoot = mediaRoot;
        _shareSignaling = shareSignaling;
    }

    public void Start()
    {
        _cts = new CancellationTokenSource();
        // 【必须监听 0.0.0.0，不能只监听 127.0.0.1】
        // 只绑回环的话，这个进程**只有自己能连** —— 对端从网络永远连不上，
        // 表现为对端日志里的 "websocket error"。本会话就踩过这个坑：
        // 所有自测都在同一台机器上，回环刚好能通，所以一直没暴露。
        _listener = new TcpListener(IPAddress.Any, _port);
        _listener.Start();

        _port = ((IPEndPoint)_listener.LocalEndpoint).Port;

        _ = Task.Run(() => AcceptLoopAsync(_cts.Token));
        Emit($"本地服务已启动: http://127.0.0.1:{_port}/" +
             (_shareSignaling ? $"   信令: {LocalSignalUrl}" : "   （信令由外部服务器提供）"));
        Emit($"监听地址: 0.0.0.0:{_port}（对端可用本机组网 IP 连接）");
        if (_shareSignaling)
        {
            // 提示实际要给对方的地址，别让用户自己猜哪个 IP 是对的
            foreach (var ip in LocalIPv4Addresses())
                Emit($"  对端应连接: ws://{ip}:{_port}/signal");
        }
    }

    /// <summary>列出本机可用的 IPv4，供房主告诉对端该连哪个地址。</summary>
    private static IEnumerable<string> LocalIPv4Addresses()
    {
        var list = new List<string>();
        try
        {
            foreach (var ni in System.Net.NetworkInformation.NetworkInterface.GetAllNetworkInterfaces())
            {
                if (ni.OperationalStatus != System.Net.NetworkInformation.OperationalStatus.Up)
                    continue;
                foreach (var addr in ni.GetIPProperties().UnicastAddresses)
                {
                    if (addr.Address.AddressFamily != AddressFamily.InterNetwork)
                        continue;
                    var s = addr.Address.ToString();
                    if (s.StartsWith("127.")) continue;
                    list.Add(s);
                }
            }
        }
        catch { /* 拿不到就算了，不影响服务 */ }
        return list;
    }

    private async Task AcceptLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            TcpClient client;
            try
            {
                client = await _listener!.AcceptTcpClientAsync(ct);
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex)
            {
                Emit($"接受连接失败: {ex.Message}");
                continue;
            }

            // 每个连接独立处理，不阻塞接受循环
            _ = Task.Run(() => HandleClientAsync(client, ct));
        }
    }

    private async Task HandleClientAsync(TcpClient client, CancellationToken ct)
    {
        var stream = client.GetStream();
        try
        {
            var request = await ReadHttpRequestAsync(stream);
            if (request is null) { client.Close(); return; }

            var path = request.Value.Path;
            // 【必须是"精确 /signal 或其子路径"，不能用 StartsWith("/signal")】
            // 2026-10-06 实测踩到：新增页面模块 media\signaling.js 后，请求 /signaling.js
            // 也被 StartsWith("/signal") 命中，被当成信令 WebSocket 端点处理 ⇒ 返回 404
            // ⇒ 页面 import 失败 ⇒ **整页一行都不执行**，而 C# 侧只看到
            // "通话页未上报 engine-loaded"，没有任何原因（连 js-error 都收不到，
            // 因为模块图解析失败不会触发 window.onerror）。
            // 判据：/signal 本身、/signal/…、/signal?…
            var isSignalRoute = path.Equals("/signal", StringComparison.OrdinalIgnoreCase)
                || path.StartsWith("/signal/", StringComparison.OrdinalIgnoreCase)
                || path.StartsWith("/signal?", StringComparison.OrdinalIgnoreCase);
            if (isSignalRoute)
            {
                await HandleWebSocketAsync(client, stream, request.Value, ct);
            }
            else
            {
                await ServeStaticAsync(stream, path);
                client.Close();
            }
        }
        catch (Exception ex)
        {
            Debug.WriteLine($"[Signaling] 连接处理异常: {ex.Message}");
            try { client.Close(); } catch { }
        }
    }

    // ---------------------------------------------------------------------
    // 最小 HTTP 解析：只取方法、路径、请求头。够用即可，不做完整实现。
    // ---------------------------------------------------------------------
    private readonly record struct HttpRequest(
        string Method, string Path, Dictionary<string, string> Headers);

    private static async Task<HttpRequest?> ReadHttpRequestAsync(NetworkStream stream)
    {
        var buf = new List<byte>(1024);
        var one = new byte[1];
        // 读到 \r\n\r\n 为止（请求头结束）
        while (buf.Count < 16384)
        {
            int n = await stream.ReadAsync(one);
            if (n == 0) return null;
            buf.Add(one[0]);
            int c = buf.Count;
            if (c >= 4 && buf[c - 4] == (byte)'\r' && buf[c - 3] == (byte)'\n'
                        && buf[c - 2] == (byte)'\r' && buf[c - 1] == (byte)'\n')
                break;
        }

        var text = Encoding.UTF8.GetString(buf.ToArray());
        var lines = text.Split("\r\n");
        if (lines.Length == 0) return null;

        var parts = lines[0].Split(' ');
        if (parts.Length < 2) return null;

        var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        for (int i = 1; i < lines.Length; i++)
        {
            var idx = lines[i].IndexOf(':');
            if (idx <= 0) continue;
            headers[lines[i][..idx].Trim()] = lines[i][(idx + 1)..].Trim();
        }
        return new HttpRequest(parts[0], parts[1], headers);
    }

    // ---------------------------------------------------------------------
    // 静态文件服务（媒体页）
    // ---------------------------------------------------------------------
    private async Task ServeStaticAsync(NetworkStream stream, string path)
    {
        var rel = path.TrimStart('/');
        if (string.IsNullOrEmpty(rel)) rel = "selfcheck.html";
        // 去掉查询串
        var q = rel.IndexOf('?');
        if (q >= 0) rel = rel[..q];

        // 路径穿越防护：解析后必须仍在媒体目录内
        var full = Path.GetFullPath(Path.Combine(_mediaRoot, rel));
        var rootFull = Path.GetFullPath(_mediaRoot);
        if (!full.StartsWith(rootFull, StringComparison.OrdinalIgnoreCase)
            || !File.Exists(full))
        {
            await WriteHttpAsync(stream, 404, "text/plain; charset=utf-8",
                Encoding.UTF8.GetBytes("404 Not Found"));
            return;
        }

        var body = await File.ReadAllBytesAsync(full);
        var mime = Path.GetExtension(full).ToLowerInvariant() switch
        {
            ".html" => "text/html; charset=utf-8",
            ".js" => "application/javascript; charset=utf-8",
            ".css" => "text/css; charset=utf-8",
            ".json" => "application/json; charset=utf-8",
            _ => "application/octet-stream",
        };
        await WriteHttpAsync(stream, 200, mime, body);
    }

    private static async Task WriteHttpAsync(NetworkStream stream, int code,
                                             string contentType, byte[] body)
    {
        var reason = code == 200 ? "OK" : "Not Found";
        var head = $"HTTP/1.1 {code} {reason}\r\n" +
                   $"Content-Type: {contentType}\r\n" +
                   $"Content-Length: {body.Length}\r\n" +
                   "Cache-Control: no-store\r\n" +
                   "Connection: close\r\n\r\n";
        var headBytes = Encoding.ASCII.GetBytes(head);
        await stream.WriteAsync(headBytes);
        await stream.WriteAsync(body);
        await stream.FlushAsync();
    }

    // ---------------------------------------------------------------------
    // WebSocket：握手 + 帧收发
    // ---------------------------------------------------------------------
    private async Task HandleWebSocketAsync(TcpClient client, NetworkStream stream,
                                            HttpRequest request, CancellationToken ct)
    {
        if (!_shareSignaling)
        {
            // 本服务器不承担信令职责。正常流程下不会有人连到这里
            // （页面里的 signalUrl 指向真正的信令服务器），
            // 但万一连了，给一个明确的拒绝而不是静默挂着。
            await WriteHttpAsync(stream, 404, "text/plain; charset=utf-8",
                Encoding.UTF8.GetBytes("this server does not relay signaling"));
            client.Close();
            return;
        }

        if (!request.Headers.TryGetValue("Sec-WebSocket-Key", out var key))
        {
            await WriteHttpAsync(stream, 404, "text/plain", Array.Empty<byte>());
            client.Close();
            return;
        }

        // 握手：Sec-WebSocket-Accept = base64(sha1(key + 固定 GUID))
        const string wsGuid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
        var accept = Convert.ToBase64String(
            SHA1.HashData(Encoding.ASCII.GetBytes(key + wsGuid)));

        var handshake = "HTTP/1.1 101 Switching Protocols\r\n" +
                        "Upgrade: websocket\r\n" +
                        "Connection: Upgrade\r\n" +
                        $"Sec-WebSocket-Accept: {accept}\r\n\r\n";
        await stream.WriteAsync(Encoding.ASCII.GetBytes(handshake));
        await stream.FlushAsync();

        // 从查询串取房间名与显示名
        var query = ParseQuery(request.Path);
        var room = query.GetValueOrDefault("room", "default");
        var name = query.GetValueOrDefault("name", "");
        var id = Guid.NewGuid().ToString("N")[..8];

        var peer = new SignalPeer
        {
            Id = id,
            Room = room,
            Stream = stream,
            SendLock = new object(),
            DisplayName = name,
        };
        _peers[id] = peer;
        Emit($"信令连接加入 room={room} id={id} name={name}");

        // 告诉它自己是谁，以及房间里还有谁
        await SendAsync(peer, new
        {
            type = "welcome",
            selfId = id,
            peers = _peers.Values
                .Where(p => p.Room == room && p.Id != id)
                .Select(p => new { id = p.Id, name = p.DisplayName })
                .ToArray(),
        });

        // 通知同房间的其他人
        await BroadcastAsync(room, id, new
        {
            type = "peer-joined",
            id,
            name,
        });

        try
        {
            while (!ct.IsCancellationRequested)
            {
                var msg = await ReadFrameAsync(stream, ct);
                if (msg is null) break;

                // 中继：把消息转给同房间除自己外的所有人。
                // 单聊场景只需要一个对端，但按广播写更通用。
                using var doc = JsonDocument.Parse(msg);
                var to = doc.RootElement.TryGetProperty("to", out var t)
                    ? t.GetString() : null;

                // 【必须补上发送者 id】页面发出的信令只有 {type,to,sdp,...}，**没有 from**
                // （实测：三实例联调时每条 offer/answer 的 from 都是 undefined）。
                // 不补的话接收方根本无法判断"这条消息属于哪位对端"，而且应答方
                // `sendSignal({type:'answer', to: msg.from})` 会因为 msg.from 是 undefined
                // 而只能走广播 —— 多余的 answer 落到不期待的端上就会报
                // "setRemoteDescription ... Called in wrong state: stable"（实测踩过）。
                var relayed = AddFrom(msg, id);

                if (!string.IsNullOrEmpty(to) && _peers.TryGetValue(to, out var target))
                {
                    await SendRawAsync(target, relayed);
                }
                else
                {
                    await BroadcastRawAsync(room, id, relayed);
                }
            }
        }
        catch (Exception ex)
        {
            Debug.WriteLine($"[Signaling] 读取循环结束 id={id}: {ex.Message}");
        }
        finally
        {
            _peers.TryRemove(id, out _);
            await BroadcastAsync(room, id, new { type = "peer-left", id });
            Emit($"信令连接断开 id={id}");
            try { client.Close(); } catch { }
        }
    }

    /// <summary>
    /// 把发送者 id 补进要转发的信令消息。
    ///
    /// 【为什么需要】页面发出的信令只有 {type,to,sdp,purpose...}，从来没有 from，
    /// 而中继原来是**原样转发**。后果实测有三条：
    ///   1) 接收端 `msg.from` 全是 undefined → 无法判断"这条 offer 是哪位对端的"，
    ///      于是"不同对端该忽略"的守卫永远不成立；
    ///   2) 应答方 `sendSignal({type:'answer', to: msg.from})` 的 to 也是 undefined
    ///      → 只能广播给全房间，多余的 answer 落到不期待的端上，
    ///      触发 "setRemoteDescription ... Called in wrong state: stable"；
    ///   3) 房主无法区分"同一位对端的重协商"和"另一位对端的新连接"。
    /// 补上之后，上面三条同时消失。
    /// </summary>
    private static string AddFrom(string json, string fromId)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            using var ms = new MemoryStream();
            using (var w = new Utf8JsonWriter(ms))
            {
                w.WriteStartObject();
                w.WriteString("from", fromId);
                foreach (var prop in doc.RootElement.EnumerateObject())
                {
                    if (prop.NameEquals("from")) continue;   // 不覆盖页面自己带的
                    prop.WriteTo(w);
                }
                w.WriteEndObject();
            }
            return Encoding.UTF8.GetString(ms.ToArray());
        }
        catch
        {
            // 解析不了就原样转发：不能因为加一个诊断/路由字段把信令本身弄坏
            return json;
        }
    }

    private static Dictionary<string, string> ParseQuery(string path)    {
        var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        var q = path.IndexOf('?');
        if (q < 0) return result;
        foreach (var kv in path[(q + 1)..].Split('&', StringSplitOptions.RemoveEmptyEntries))
        {
            var eq = kv.IndexOf('=');
            if (eq <= 0) continue;
            result[Uri.UnescapeDataString(kv[..eq])] = Uri.UnescapeDataString(kv[(eq + 1)..]);
        }
        return result;
    }

    // ---- 帧编解码 ----
    // 只实现必需的部分：文本帧（0x1）、关闭（0x8）、ping/pong（0x9/0xA）。
    // 浏览器发来的帧一定带掩码；我们发出去的不带（服务端不允许加掩码）。

    private static async Task<string?> ReadFrameAsync(NetworkStream stream,
                                                      CancellationToken ct)
    {
        var header = await ReadExactAsync(stream, 2, ct);
        if (header is null) return null;

        int opcode = header[0] & 0x0F;
        bool masked = (header[1] & 0x80) != 0;
        long len = header[1] & 0x7F;

        if (len == 126)
        {
            var ext = await ReadExactAsync(stream, 2, ct);
            if (ext is null) return null;
            len = (ext[0] << 8) | ext[1];
        }
        else if (len == 127)
        {
            var ext = await ReadExactAsync(stream, 8, ct);
            if (ext is null) return null;
            len = 0;
            for (int i = 0; i < 8; i++) len = (len << 8) | ext[i];
        }

        byte[]? mask = null;
        if (masked)
        {
            mask = await ReadExactAsync(stream, 4, ct);
            if (mask is null) return null;
        }

        var payload = len > 0 ? await ReadExactAsync(stream, (int)len, ct) : Array.Empty<byte>();
        if (payload is null) return null;

        if (mask is not null)
        {
            for (int i = 0; i < payload.Length; i++) payload[i] ^= mask[i % 4];
        }

        switch (opcode)
        {
            case 0x1:   // text
                return Encoding.UTF8.GetString(payload);
            case 0x8:   // close
                return null;
            case 0x9:   // ping → 回 pong
                await WriteFrameAsync(stream, 0xA, payload, CancellationToken.None);
                return await ReadFrameAsync(stream, ct);
            case 0xA:   // pong：忽略
                return await ReadFrameAsync(stream, ct);
            default:
                // 二进制与分片帧用不到，直接跳过
                return await ReadFrameAsync(stream, ct);
        }
    }

    private static async Task<byte[]?> ReadExactAsync(NetworkStream stream, int count,
                                                      CancellationToken ct)
    {
        var buf = new byte[count];
        int read = 0;
        while (read < count)
        {
            int n = await stream.ReadAsync(buf.AsMemory(read, count - read), ct);
            if (n == 0) return null;
            read += n;
        }
        return buf;
    }

    private static async Task WriteFrameAsync(NetworkStream stream, int opcode,
                                              byte[] payload, CancellationToken ct)
    {
        using var ms = new MemoryStream();
        ms.WriteByte((byte)(0x80 | opcode));   // FIN + opcode

        if (payload.Length < 126)
        {
            ms.WriteByte((byte)payload.Length);
        }
        else if (payload.Length <= ushort.MaxValue)
        {
            ms.WriteByte(126);
            ms.WriteByte((byte)(payload.Length >> 8));
            ms.WriteByte((byte)(payload.Length & 0xFF));
        }
        else
        {
            ms.WriteByte(127);
            for (int i = 7; i >= 0; i--)
                ms.WriteByte((byte)((long)payload.Length >> (8 * i) & 0xFF));
        }

        ms.Write(payload);
        var bytes = ms.ToArray();
        await stream.WriteAsync(bytes, ct);
        await stream.FlushAsync(ct);
    }

    // ---- 发送辅助 ----

    private async Task SendRawAsync(SignalPeer peer, string json)
    {
        try
        {
            // 加锁：同一连接的并发写会交错成坏帧
            lock (peer.SendLock)
            {
                WriteFrameAsync(peer.Stream, 0x1, Encoding.UTF8.GetBytes(json),
                                CancellationToken.None).GetAwaiter().GetResult();
            }
            await Task.CompletedTask;
        }
        catch (Exception ex)
        {
            Debug.WriteLine($"[Signaling] 发送失败 id={peer.Id}: {ex.Message}");
        }
    }

    private Task SendAsync(SignalPeer peer, object payload)
        => SendRawAsync(peer, JsonSerializer.Serialize(payload, JsonOpts));

    private async Task BroadcastRawAsync(string room, string exceptId, string json)
    {
        foreach (var p in _peers.Values.Where(p => p.Room == room && p.Id != exceptId))
            await SendRawAsync(p, json);
    }

    private Task BroadcastAsync(string room, string exceptId, object payload)
        => BroadcastRawAsync(room, exceptId, JsonSerializer.Serialize(payload, JsonOpts));

    private void Emit(string line)
    {
        Log?.Invoke(line);
        Debug.WriteLine($"[Signaling] {line}");
    }

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    public void Dispose()
    {
        try { _cts?.Cancel(); } catch { }
        try { _listener?.Stop(); } catch { }
        foreach (var p in _peers.Values)
        {
            try { p.Stream.Close(); } catch { }
        }
        _peers.Clear();
        _cts?.Dispose();
    }
}
