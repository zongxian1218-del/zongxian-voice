using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace ZongxianVoice;

/// <summary>局域网里发现的一个"房间"。</summary>
public sealed record LanRoom(string Id, string Name, string Ip, int Port, string Room, DateTime LastSeen)
{
    public string Address => $"{Ip}:{Port}";

    /// <summary>
    /// 列表里显示的标签。
    /// 【A4 实测依据】三台机器同房间时，原来的 `机器名｜房间 X｜ip:port` 几乎一模一样
    /// （同一台开发机上甚至机器名都相同），只能靠端口分辨 ⇒ 选不出该连哪个。
    /// 现在把**房间名放最前**、端口单独标出来、并注明"本机"（本机不该被自己连）。
    /// </summary>
    public string Display =>
        string.IsNullOrEmpty(Room)
            ? $"{Name}（{Ip}:{Port}）"
            : $"房间 {Room} · {Name} · 端口 {Port}";

    /// <summary>是否是本机广播（本机不该出现在"加入"列表里）。</summary>
    public bool IsSelf { get; init; }

    /// <summary>供筛选用的合并文本（房间名/机器名/IP/端口）。</summary>
    public string SearchText => $"{Room} {Name} {Ip} {Port}".ToLowerInvariant();
}

/// <summary>
/// 局域网房间自动检测：UDP 广播"名片"，不需要任何人输地址。
///
/// 为什么要它：在这之前加入别人的房间只能靠命令行 `--signal`，界面上没地方填地址 ——
/// 用户原话是"测试不了这个没地方输命令"。有了自动发现，双端/多端测试就只剩点鼠标。
///
/// 沿用 A 线（`src/swiftdrop/discovery.py`）那套做法的**形状**，但**换端口**：
///   · A 线（文件传输）用 45871，退避段 45871–45879；
///   · 语音线用 45880，避开那一段，两个产品互不打扰。
///   · 同样的：每秒左右广播一次、名片带 magic 字段、超过 TTL 没消息就当离线、
///     除了广播地址还补发 127.0.0.1（同一台机器上开两个实例时，广播不一定回环给自己）。
///
/// 多实例同机：UDP 端口设置了 ReuseAddress（ExclusiveAddressUse=false），
/// 否则第二个实例绑不上同一个端口，同机双实例就永远发现不了对方。
/// </summary>
public sealed class LanDiscovery : IDisposable
{
    public const int DiscoveryPort = 45880;
    private const string Magic = "ZONGXIANVOICE1";
    private static readonly TimeSpan Ttl = TimeSpan.FromSeconds(12);
    private static readonly TimeSpan BeaconInterval = TimeSpan.FromSeconds(1.5);

    private readonly string _id = Guid.NewGuid().ToString("N")[..8];
    private readonly string _room;
    private readonly int _signalPort;
    private readonly Dictionary<string, LanRoom> _rooms = new();

    private UdpClient? _udp;
    private Timer? _timer;
    private volatile bool _running;

    /// <summary>发现的房间变了（新增/消失）。可能从后台线程触发，订阅方要自己切回 UI 线程。</summary>
    public event Action<IReadOnlyList<LanRoom>>? RoomsChanged;

    public LanDiscovery(int signalPort, string room)
    {
        _signalPort = signalPort;
        _room = string.IsNullOrWhiteSpace(room) ? "default" : room;
    }

    public string SelfId => _id;

    public IReadOnlyList<LanRoom> Rooms
    {
        get { lock (_rooms) return _rooms.Values.OrderByDescending(r => r.LastSeen).ToList(); }
    }

    public void Start()
    {
        if (_running) return;
        try
        {
            var client = new UdpClient();
            client.ExclusiveAddressUse = false;          // 关键：允许多个实例绑同一端口
            client.Client.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
            client.Client.Bind(new IPEndPoint(IPAddress.Any, DiscoveryPort));
            client.EnableBroadcast = true;
            _udp = client;
            _running = true;
            _ = Task.Run(ReceiveLoopAsync);
            _timer = new Timer(_ => SendBeacon(), null, TimeSpan.Zero, BeaconInterval);
        }
        catch
        {
            // 端口被别的程序占了、或被防火墙策略挡住：发现功能失效，但**不能影响主功能**
            _running = false;
        }
    }

    private byte[] BuildBeacon()
    {
        var card = new
        {
            magic = Magic,
            kind = "beacon",
            id = _id,
            name = Environment.MachineName,
            room = _room,
            port = _signalPort,
            os = "windows",
            t = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
        };
        return Encoding.UTF8.GetBytes(JsonSerializer.Serialize(card));
    }

    private void SendBeacon()
    {
        if (!_running || _udp is null) return;
        var bytes = BuildBeacon();
        foreach (var target in new[] { "255.255.255.255", "127.0.0.1" })
        {
            try { _udp.Send(bytes, bytes.Length, new IPEndPoint(IPAddress.Parse(target), DiscoveryPort)); }
            catch { /* 某个目标发不出去不算错（比如没有局域网网卡） */ }
        }
    }

    private async Task ReceiveLoopAsync()
    {
        while (_running && _udp is not null)
        {
            UdpReceiveResult r;
            try { r = await _udp.ReceiveAsync().ConfigureAwait(false); }
            catch { break; }                                  // socket 关了
            try { HandlePacket(r.Buffer, r.RemoteEndPoint); } catch { }
        }
    }

    private void HandlePacket(byte[] buffer, IPEndPoint from)
    {
        string? magic = null, id = null, name = null, room = null;
        int port = 0;
        try
        {
            using var doc = JsonDocument.Parse(buffer);
            var root = doc.RootElement;
            if (root.TryGetProperty("magic", out var m)) magic = m.GetString();
            if (root.TryGetProperty("id", out var i)) id = i.GetString();
            if (root.TryGetProperty("name", out var n)) name = n.GetString();
            if (root.TryGetProperty("room", out var rm)) room = rm.GetString();
            if (root.TryGetProperty("port", out var p) && p.TryGetInt32(out var pv)) port = pv;
        }
        catch { return; }
        if (magic != Magic || string.IsNullOrEmpty(id) || port <= 0) return;
        if (id == _id) return;                                 // 自己发的，忽略

        bool changed;
        lock (_rooms)
        {
            var ip = from.Address.ToString();
            var existed = _rooms.ContainsKey(id);
            _rooms[id] = new LanRoom(id, name ?? ip, ip, port, room ?? "default", DateTime.UtcNow);
            changed = !existed;
        }
        if (changed) Raise();
    }

    /// <summary>定期清理超时房间（对方关掉应用后，几秒内应从列表里消失）。</summary>
    private int Prune()
    {
        int removed;
        lock (_rooms)
        {
            var dead = _rooms.Where(kv => DateTime.UtcNow - kv.Value.LastSeen > Ttl).Select(kv => kv.Key).ToList();
            foreach (var k in dead) _rooms.Remove(k);
            removed = dead.Count;
        }
        return removed;
    }

    private void Raise() => RoomsChanged?.Invoke(Rooms);

    /// <summary>由外部定时调用（界面侧每秒一次即可）：清理超时项并在有变化时通知。</summary>
    public void Tick()
    {
        if (Prune() > 0) Raise();
    }

    public void Stop()
    {
        _running = false;
        try { _timer?.Dispose(); } catch { }
        _timer = null;
        try { _udp?.Close(); } catch { }
        _udp = null;
    }

    public void Dispose() => Stop();
}
