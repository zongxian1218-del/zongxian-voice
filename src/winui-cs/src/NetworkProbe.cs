/*
NetworkProbe.cs —— 网络自检（S2 拆桥，2026-10-06）。

【为什么必须存在】第一次真机联调失败的根因是网络：房主只绑了回环地址，
对端从网络根本连不上。这类问题在应用里判断不出"断在哪一环"（绑定？防火墙？组网？），
所以要有能明确回答"断在哪"的检查。

【它负责什么】跑 netstat/netsh、探测 TCP/HTTP/WebSocket，产出**文本报告**。
【它不负责什么】不弹窗口、不碰控件 —— 报告交给调用方显示或复制。
拆桥前这些在 MainWindow 里（`Process.Start` 直接混在事件处理里），S2 的判据之一就是它们要搬走。

【不写死的点】端口、超时、路径全来自参数或 AppConfig；命令名（netstat/netsh）是系统固定名，
属于白名单里的"外部程序名"，不是可配置项。
*/
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Net.Http;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace ZongxianVoice;

internal sealed class NetworkProbe
{
    private readonly int _signalPort;
    private readonly Action<string> _log;

    public NetworkProbe(int signalPort, Action<string>? log = null)
    {
        _signalPort = signalPort;
        _log = log ?? (_ => { });
    }

    /// <summary>只读网络报告：监听状态、防火墙规则、本机地址（端口取真实生效的那个）。</summary>
    public string BuildReport()
    {
        var sb = new StringBuilder();
        sb.AppendLine("=== 监听状态 ===");
        var netstat = RunTool("netstat", "-ano", AppConfig.NetstatTimeoutMs);
        if (netstat is null)
        {
            sb.AppendLine("  检查失败：netstat 跑不起来");
        }
        else
        {
            bool any = false;
            foreach (var line in netstat.Split('\n'))
            {
                // 真实生效的端口 + 它的下一个端口（同一套自检要能看见两个实例）
                if (line.Contains($":{_signalPort}") || line.Contains($":{_signalPort + 1}"))
                {
                    sb.AppendLine("  " + line.Trim());
                    any = true;
                }
            }
            if (!any) sb.AppendLine($"  （没有监听 {_signalPort}/{_signalPort + 1} —— 应用可能没启动）");
        }

        sb.AppendLine();
        sb.AppendLine("=== 防火墙规则 ===");
        var rules = RunTool("netsh", "advfirewall firewall show rule name=all", AppConfig.NetshTimeoutMs);
        if (rules is null)
        {
            sb.AppendLine("  检查失败：netsh 跑不起来");
        }
        else
        {
            bool found = false;
            foreach (var b in rules.Split(new[] { "\r\n\r\n" }, StringSplitOptions.RemoveEmptyEntries))
            {
                if (b.Contains("ZongxianVoice", StringComparison.OrdinalIgnoreCase))
                {
                    sb.AppendLine("  " + b.Trim().Replace("\r\n", "\n     "));
                    found = true;
                }
            }
            if (!found)
            {
                sb.AppendLine("  ✗ 没有找到 ZongxianVoice 的防火墙规则");
                sb.AppendLine("    → 这就是对端连不上的原因。");
                sb.AppendLine("    → 解决办法：以管理员身份运行 4-allow-firewall.cmd");
            }
        }

        sb.AppendLine();
        sb.AppendLine("=== 本机地址（对端应该连这些）===");
        foreach (var (addr, name) in LocalIPv4())
            sb.AppendLine($"  {addr,-18} ws://{addr}:{_signalPort}{AppConfig.SignalPath}   [{name}]");

        return sb.ToString();
    }

    /// <summary>本机可用的 IPv4（排除回环）与其网卡名。纯静态：枚举失败就返回已拿到的部分。</summary>
    public static List<(string addr, string nic)> LocalIPv4()
    {
        var list = new List<(string, string)>();
        try
        {
            foreach (var ni in NetworkInterface.GetAllNetworkInterfaces())
            {
                if (ni.OperationalStatus != OperationalStatus.Up) continue;
                foreach (var a in ni.GetIPProperties().UnicastAddresses)
                {
                    if (a.Address.AddressFamily != AddressFamily.InterNetwork) continue;
                    var s = a.Address.ToString();
                    if (s.StartsWith("127.")) continue;
                    list.Add((s, ni.Name));
                }
            }
        }
        catch
        {
            // 拿不到就返回已枚举到的部分：调用方只把它当提示信息
        }
        return list;
    }

    /// <summary>像对端那样去连房主的信令：TCP → 取页面 → WebSocket 握手，逐层报告。</summary>
    public async Task<string> CheckHostAsync(string ip)
    {
        var sb = new StringBuilder();
        sb.AppendLine($"目标: {ip}:{_signalPort}");
        sb.AppendLine();

        sb.AppendLine("=== TCP 连通性 ===");
        var tcp = await ProbeTcpAsync(ip, _signalPort, AppConfig.CheckHostTcpTimeoutMs);
        if (!tcp.ok)
        {
            sb.AppendLine($"  ✗ TCP {_signalPort} 连不上（{tcp.detail}）");
            sb.AppendLine("    可能原因：");
            sb.AppendLine("      · 房主没点防火墙的「允许访问」→ 让房主跑 4-allow-firewall.cmd");
            sb.AppendLine($"      · 房主没启动 / 端口不是 {_signalPort}");
            sb.AppendLine("      · 组网工具没连通 → 让房主 ping 一下你的组网 IP");
            return sb.ToString();
        }
        sb.AppendLine($"  ✓ {tcp.detail}");

        sb.AppendLine();
        sb.AppendLine("=== 媒体页 ===");
        try
        {
            using var http = new HttpClient
            { Timeout = TimeSpan.FromMilliseconds(AppConfig.CheckHostHttpTimeoutMs) };
            var html = await http.GetStringAsync(
                $"http://{ip}:{_signalPort}/{AppConfig.CallPageName}");
            sb.AppendLine($"  ✓ 取到 {AppConfig.CallPageName}，{html.Length} 字符");
            sb.AppendLine(html.Contains("zxEngine")
                ? "  ✓ 页面内容正常（含 zxEngine）"
                : "  ✗ 页面内容异常，可能取到了别的东西");
        }
        catch (Exception ex)
        {
            sb.AppendLine($"  ✗ 取页面失败: {ex.Message}");
        }

        sb.AppendLine();
        sb.AppendLine("=== 信令 WebSocket ===");
        try
        {
            using var ws = new ClientWebSocket();
            using var cts = new CancellationTokenSource(
                TimeSpan.FromMilliseconds(AppConfig.CheckHostHttpTimeoutMs));
            await ws.ConnectAsync(
                new Uri($"ws://{ip}:{_signalPort}{AppConfig.SignalPath}?room=selftest&name=probe"),
                cts.Token);
            sb.AppendLine($"  ✓ WebSocket 握手成功，状态 = {ws.State}");
            var buf = new byte[1024];
            var recv = await ws.ReceiveAsync(new ArraySegment<byte>(buf), cts.Token);
            var text = Encoding.UTF8.GetString(buf, 0, recv.Count);
            sb.AppendLine($"  ✓ 收到服务端消息: {text}");
            sb.AppendLine();
            sb.AppendLine("结论：网络与房主服务都正常。如果应用里仍连不上，是应用侧问题。");
        }
        catch (Exception ex)
        {
            sb.AppendLine($"  ✗ WebSocket 失败: {ex.Message}");
        }

        return sb.ToString();
    }

    /// <summary>从 ws://host:port/signal 里取出主机与端口。</summary>
    public static (string host, int port)? TryParseHostPort(string url)
    {
        try
        {
            var u = new Uri(url);
            return (u.Host, u.Port);
        }
        catch { return null; }
    }

    /// <summary>
    /// 裸 TCP 预检。用把"网络层不通"与"应用层出错"分开 ——
    /// 这两种现象都是 "websocket error"，但排查方向完全不同。
    /// </summary>
    public static async Task<(bool ok, string detail)> ProbeTcpAsync(string host, int port, int timeoutMs)
    {
        var sw = Stopwatch.StartNew();
        try
        {
            using var tcp = new TcpClient();
            var connect = tcp.ConnectAsync(host, port);
            await Task.WhenAny(connect, Task.Delay(timeoutMs));
            sw.Stop();
            if (connect.IsCompletedSuccessfully && tcp.Connected)
                return (true, $"TCP 已连通，耗时 {sw.ElapsedMilliseconds} ms");
            if (!connect.IsCompleted)
                return (false, $"TCP 连接超时（{timeoutMs} ms）—— 多半被防火墙丢弃了");
            return (false, $"TCP 连接被拒绝或被重置 —— {connect.Exception?.Message}");
        }
        catch (Exception ex)
        {
            sw.Stop();
            return (false, $"{ex.GetType().Name}: {ex.Message}（{sw.ElapsedMilliseconds} ms）");
        }
    }

    /// <summary>跑一个系统命令行工具并取 stdout（带超时；失败返回 null 并记日志）。</summary>
    private string? RunTool(string exe, string args, int timeoutMs)
    {
        try
        {
            var psi = new ProcessStartInfo(exe, args)
            {
                RedirectStandardOutput = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            using var p = Process.Start(psi);
            if (p is null) return null;
            var text = p.StandardOutput.ReadToEnd();
            if (!p.WaitForExit(timeoutMs))
            {
                try { p.Kill(entireProcessTree: true); } catch { }
                _log($"[网络自检] {exe} 超时 {timeoutMs} ms，已终止");
                return null;
            }
            return text;
        }
        catch (Exception ex)
        {
            _log($"[网络自检] {exe} 失败：{ex.Message}");
            return null;
        }
    }
}
