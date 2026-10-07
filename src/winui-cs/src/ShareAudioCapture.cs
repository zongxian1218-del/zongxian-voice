// ShareAudioCapture.cs —— 「共享电脑声音」的应用侧采集泵
//
// 职责：起引擎探针（zxprobe stream）→ 读它的 **二进制** PCM → 按块 base64 →
//       推给页面（zxEngine.pushSharedAudio）。页面把它变成一条**独立音轨**发出去。
//
// 为什么采集在应用进程而不是页面里：
//   Chromium 的 getDisplayMedia 只能共享"整个屏幕/标签页"的系统声音，**做不到"指定某个应用"**；
//   要按应用采集必须走 Windows 的进程回环（WASAPI），那只有引擎能做。
//
// 约定（与引擎、worklet 三方一致，改要一起改）：
//   · float32 交叉、48000 Hz、2 声道
//   · 一次一块 = 50ms = 4800 帧 = 38400 字节 → base64 约 51 KB
//
// 【踩过的坑】读 stdout 必须用 BaseStream（二进制）；用 StreamReader/ReadLine 会把 PCM 当文本，
// 而且 Windows 上还要注意引擎侧已经把 stdout 切成 _O_BINARY（见 zxprobe.cpp 的 CmdStream）。

using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Threading.Tasks;

namespace ZongxianVoice;

/// <summary>共享电脑声音的采集泵。生命周期与一次"共享"对应。</summary>
public sealed class ShareAudioCapture : IDisposable
{
    public const int SampleRate = 48000;
    public const int Channels = 2;
    private const int BlockBytes = SampleRate / 20 * Channels * 4;   // 50ms 的字节数

    private readonly Process _proc;
    private readonly CancellationTokenSource _cts = new();
    private Task? _pump;

    /// <summary>读到的 PCM 块数 / 总字节数，用于诊断（界面与日志都看这个）。</summary>
    public long Blocks { get; private set; }
    public long Bytes { get; private set; }
    public string Kind { get; }
    public string? LastError { get; private set; }
    /// <summary>启动时用的完整参数（诊断用：日志里要能看见到底怎么起的）。</summary>
    public string LaunchArgs { get; private set; } = "";

    /// <summary>引擎进程的退出码；还活着就是 null。</summary>
    public int? ExitCode
    {
        get
        {
            try { return _proc.HasExited ? _proc.ExitCode : (int?)null; }
            catch { return null; }
        }
    }

    private ShareAudioCapture(Process proc, string kind)
    {
        _proc = proc;
        Kind = kind;
    }

    /// <summary>
    /// 启动采集。kind: "loopback"（整个系统）或 "process"（指定应用，需要 pid）。
    /// </summary>
    public static ShareAudioCapture? Start(string exePath, string kind, int pid)
    {
        if (!File.Exists(exePath)) return null;
        var args = $"--kind {kind}";
        if (kind == "process") args += $" --pid {pid}";
        // 注意：这里**不**传 --ns —— 让引擎按采集源类型决定（内容源直通、麦克风才降噪）。
        var psi = new ProcessStartInfo(exePath, $"stream {args}")
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        try
        {
            var proc = Process.Start(psi);
            if (proc == null) return null;
            return new ShareAudioCapture(proc, kind) { LaunchArgs = psi.Arguments };
        }
        catch
        {
            return null;
        }
    }

    /// <summary>开始把 PCM 泵给页面。push 回调负责把 base64 交给页面。</summary>
    public void StartPump(Func<string, Task> push)
    {
        _pump = Task.Run(async () =>
        {
            var buf = new byte[BlockBytes];
            var stream = _proc.StandardOutput.BaseStream;   // 二进制读，绝不走 StreamReader
            try
            {
                while (!_cts.IsCancellationRequested)
                {
                    int filled = 0;
                    while (filled < BlockBytes)
                    {
                        int n = await stream.ReadAsync(buf.AsMemory(filled, BlockBytes - filled), _cts.Token);
                        if (n <= 0)
                        {
                            // 引擎输出结束：把"结束时的状态"记下来。
                            // 之前这里直接 return，于是"泵没数据"在日志里完全看不出来（实测踩到）。
                            LastError = $"引擎输出结束（已收 {Blocks} 块 {Bytes} 字节，退出码={ExitCode}）";
                            return;
                        }
                        filled += n;
                    }
                    Blocks++;
                    Bytes += BlockBytes;
                    await push(Convert.ToBase64String(buf, 0, BlockBytes));
                }
            }
            catch (OperationCanceledException) { }
            catch (Exception ex) { LastError = ex.Message; }
        });
    }

    /// <summary>引擎 stderr 的一行（含"生产端统计"，排查时很有用）。</summary>
    public async Task<string?> ReadEngineLineAsync()
    {
        try { return await _proc.StandardError.ReadLineAsync(); }
        catch { return null; }
    }

    public void Dispose()
    {
        try { _cts.Cancel(); } catch { }
        try { if (!_proc.HasExited) _proc.Kill(entireProcessTree: true); } catch { }
        try { _proc.Dispose(); } catch { }
    }
}
