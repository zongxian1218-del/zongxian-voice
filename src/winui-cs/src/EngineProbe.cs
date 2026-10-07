/*
EngineProbe.cs —— zxprobe.exe 的封装（S2 拆桥，2026-10-06）。

【它负责什么】调用引擎探针、把它的输出解析成**强类型结果**：
  · devices —— 设备枚举（麦克风/扬声器，含采样率/声道/是否默认）
  · apps    —— Windows 音频会话枚举（"共享电脑声音"的来源列表）
【它不负责什么】不碰控件、不写界面、不决定文案 —— 那是调用方（UI 层）的事。
拆桥前这些都写在 MainWindow.xaml.cs 里（`Process.Start` + `JsonDocument.Parse` 直接混在事件处理中），
S2 的判据就是"MainWindow 里不再有 Process.Start / JsonDocument.Parse"。

【两个必须记住的坑（都是实测踩出来的）】
  1. zxprobe 的输出**不是纯 JSON**：它是 `采集设备：[...]` 再 `渲染（播放）设备：[...]`，
     两组数组中间夹中文标题 —— 直接 JsonDocument.Parse 会报
     `'0xE6' is invalid after a single JSON value`（0xE6 就是中文首字节）。
     所以要先按**配对括号**切出每一段数组（JsonArrays）。
  2. 探针必须**超时包住**并等退出，否则会留下跑飞的进程（实测有过 nstest 卡死把链接锁住）。
*/
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;

namespace ZongxianVoice;

internal sealed class EngineProbe
{
    public sealed record Device(string Id, string Name, string Kind, int SampleRate, int Channels, bool IsDefault);
    public sealed record AudioApp(int Pid, string Name, string Title, bool Active);

    private readonly Action<string> _log;
    public EngineProbe(Action<string>? log = null) => _log = log ?? (_ => { });

    public string? ToolPath => ProbeLocator.FindProbe();

    /// <summary>跑一次探针并拿 stdout；失败返回 null（调用方负责写日志）。</summary>
    private async Task<string?> RunAsync(string args, int timeoutMs)
    {
        var exe = ToolPath;
        if (exe is null) return null;
        try
        {
            var psi = new ProcessStartInfo(exe, args)
            {
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
                StandardOutputEncoding = Encoding.UTF8,
            };
            using var proc = Process.Start(psi);
            if (proc is null) return null;
            var read = proc.StandardOutput.ReadToEndAsync();
            var done = await Task.WhenAny(read, Task.Delay(timeoutMs));
            if (done != read)
            {
                try { proc.Kill(entireProcessTree: true); } catch { }
                _log($"[探针] {args} 超时 {timeoutMs} ms，已终止（避免留下跑飞的子进程）");
                return null;
            }
            return await read;
        }
        catch (Exception ex)
        {
            _log($"[探针] 运行 {args} 失败：{ex.Message}");
            return null;
        }
    }

    public async Task<List<Device>> DevicesAsync(int timeoutMs = AppConfig.SubprocessProbeTimeoutMs)
    {
        var text = await RunAsync("devices", timeoutMs);
        if (text is null) return new List<Device>();
        var list = new List<Device>();
        foreach (var json in JsonArrays(text))
        {
            try
            {
                using var doc = JsonDocument.Parse(json);
                foreach (var el in doc.RootElement.EnumerateArray())
                {
                    var kind = GetStr(el, "kind");
                    var name = GetStr(el, "name");
                    if (name.Length == 0) continue;
                    list.Add(new Device(
                        GetStr(el, "id"), name, kind,
                        GetInt(el, "sampleRate"), GetInt(el, "channels"),
                        el.TryGetProperty("isDefault", out var d) && d.ValueKind == JsonValueKind.True));
                }
            }
            catch (Exception ex)
            {
                _log("[探针] devices 的某段数组解析失败：" + ex.Message);
            }
        }
        return list;
    }

    public async Task<List<AudioApp>> AppsAsync(int timeoutMs = AppConfig.SubprocessProbeTimeoutMs)
    {
        var text = await RunAsync("apps", timeoutMs);
        if (text is null) return new List<AudioApp>();
        var list = new List<AudioApp>();
        foreach (var json in JsonArrays(text))
        {
            try
            {
                using var doc = JsonDocument.Parse(json);
                foreach (var el in doc.RootElement.EnumerateArray())
                {
                    var pid = GetInt(el, "pid");
                    var name = GetStr(el, "name");
                    if (pid <= 0 || name.Length == 0) continue;
                    list.Add(new AudioApp(pid, name, GetStr(el, "title"),
                        el.TryGetProperty("active", out var a) && a.ValueKind == JsonValueKind.True));
                }
            }
            catch (Exception ex)
            {
                _log("[探针] apps 的某段数组解析失败：" + ex.Message);
            }
        }
        return list;
    }

    private static string GetStr(JsonElement el, string key) =>
        el.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";

    private static int GetInt(JsonElement el, string key) =>
        el.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetInt32() : 0;

    /// <summary>
    /// 从探针输出里切出**所有完整的 JSON 数组**（按配对括号，考虑字符串里的括号与转义）。
    /// 理由见文件头坑 1 —— 这不是"过度防御"，是实测必须。
    /// </summary>
    internal static List<string> JsonArrays(string text)
    {
        var result = new List<string>();
        int i = 0;
        while (i < text.Length)
        {
            int start = text.IndexOf('[', i);
            if (start < 0) break;
            int depth = 0, end = -1;
            bool inStr = false, esc = false;
            for (int j = start; j < text.Length; j++)
            {
                char c = text[j];
                if (inStr)
                {
                    if (esc) esc = false;
                    else if (c == '\\') esc = true;
                    else if (c == '"') inStr = false;
                    continue;
                }
                if (c == '"') inStr = true;
                else if (c == '[') depth++;
                else if (c == ']')
                {
                    depth--;
                    if (depth == 0) { end = j; break; }
                }
            }
            if (end < 0) break;
            result.Add(text[start..(end + 1)]);
            i = end + 1;
        }
        return result;
    }
}
