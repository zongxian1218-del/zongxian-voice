using System;
using System.Diagnostics;
using System.IO;
using System.Text;

namespace ZongxianVoice;

/// <summary>「单个应用音频」（WASAPI 进程回环）可用性探测的结果。</summary>
public enum AppAudioVerdict
{
    Supported,      // 子进程正常退出，能抓
    Unsupported,    // 引擎明确说本机不支持
    Crashed,        // 子进程崩了（0xC0000374 堆损坏）—— 这正是必须先做子进程隔离的原因
    ToolMissing,    // 找不到捕获工具（zxprobe.exe）
    Timeout,        // 子进程超时
}

public sealed record AppAudioProbeResult(AppAudioVerdict Verdict, string Reason, string Detail)
{
    /// <summary>给界面用的短句。</summary>
    public string ShortText => Verdict switch
    {
        AppAudioVerdict.Supported => "单个应用音频：可用",
        AppAudioVerdict.Unsupported => "单个应用音频：本机不支持（用「共享全部应用的声音」）",
        AppAudioVerdict.Crashed => "单个应用音频：本机不支持（激活会让进程堆损坏）",
        AppAudioVerdict.ToolMissing => "单个应用音频：缺少捕获工具 zxprobe.exe",
        _ => "单个应用音频：探测超时",
    };
}

/// <summary>
/// 探测「单个应用音频」能不能用 —— **必须在子进程里探**。
///
/// 依据（src/media/STATUS.md §7 / §7.1，都在本机实测过）：
///   · 进程回环激活会让**调用进程**堆损坏（0xC0000374），SEH 拦不住、抓不到异常；
///   · 所以主进程绝不能自己碰这个 API —— 让 zxprobe.exe 去碰，崩了当"不支持"；
///   · 音频客户端句柄不能跨进程转移，所以将来真正抓 PCM 也得留在那个子进程里，
///     由它把 PCM 交给主进程（这一步是后续工作，本类只负责"能不能用"）。
///
/// 同时它也是"隔离在应用里生效"的证据：子进程崩，主进程照常活着并给出降级结论。
/// </summary>
public static class AppAudioProbe
{
    private const int ExitUnsupported = 2;
    private const int ExitHeapCorruption = unchecked((int)0xC0000374);

    private static AppAudioProbeResult? _cached;

    /// <summary>探测一次并缓存（同一进程内只探一次：子进程可能崩，不重复付代价）。</summary>
    public static AppAudioProbeResult Probe(bool force = false)
    {
        if (!force && _cached is not null) return _cached;
        _cached = ProbeOnce();
        return _cached;
    }

    private static AppAudioProbeResult ProbeOnce()
    {
        var tool = ResolveToolPath();
        if (tool is null)
            return new AppAudioProbeResult(AppAudioVerdict.ToolMissing, "找不到 zxprobe.exe", "");

        // 临时目录一定要放在**工作区内**：本环境下被拉起的 exe 写工作区外会被拒
        //（这也是 NSIS 编译报 "error creating mmap" 的原因）。
        var workTmp = Path.Combine(AppContext.BaseDirectory, "audio-probe-tmp");
        try { Directory.CreateDirectory(workTmp); } catch { }

        var psi = new ProcessStartInfo
        {
            FileName = tool,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            // 显式指定 UTF-8：否则 .NET 按控制台代码页解码，
            // 工具输出的中文会变成不可匹配的乱码，"原因"那行就提取不出来（实测踩过）。
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
            CreateNoWindow = true,
            WorkingDirectory = AppContext.BaseDirectory,
        };
        psi.ArgumentList.Add("record");
        psi.ArgumentList.Add("--kind");
        psi.ArgumentList.Add("process");
        psi.ArgumentList.Add("--pid");
        psi.ArgumentList.Add(Environment.ProcessId.ToString());
        psi.ArgumentList.Add("--seconds");
        psi.ArgumentList.Add(AppConfig.AppAudioProbeSeconds.ToString());
        psi.ArgumentList.Add("--out");
        psi.ArgumentList.Add(Path.Combine(workTmp, "app-audio.wav"));
        psi.ArgumentList.Add("--raw");
        psi.ArgumentList.Add(Path.Combine(workTmp, "app-audio-raw.wav"));
        psi.Environment["TEMP"] = workTmp;
        psi.Environment["TMP"] = workTmp;

        try
        {
            using var p = Process.Start(psi);
            if (p is null)
                return new AppAudioProbeResult(AppAudioVerdict.ToolMissing, "无法启动捕获工具", "");

            var stdout = p.StandardOutput.ReadToEnd();
            var stderr = p.StandardError.ReadToEnd();
            // 子进程崩了（堆损坏）这里会正常返回 false → 超时分支，而不是把主进程带走
            if (!p.WaitForExit(AppConfig.SubprocessProbeTimeoutMs))
            {
                try { p.Kill(entireProcessTree: true); } catch { }
                return new AppAudioProbeResult(AppAudioVerdict.Timeout,
                    $"捕获工具 {AppConfig.SubprocessProbeTimeoutMs / 1000} 秒未退出", "");
            }

            var output = (stdout + "\n" + stderr).Trim();
            var reason = ExtractReason(output);
            int rc = p.ExitCode;

            if (rc == 0)
                return new AppAudioProbeResult(AppAudioVerdict.Supported, "子进程正常退出", output);
            if (rc == ExitHeapCorruption)
                return new AppAudioProbeResult(AppAudioVerdict.Crashed,
                    "子进程崩了：0xC0000374（堆损坏）—— 主进程无伤，降级为「共享全部应用的声音」", output);
            if (rc == ExitUnsupported)
                return new AppAudioProbeResult(AppAudioVerdict.Unsupported, reason, output);
            return new AppAudioProbeResult(AppAudioVerdict.Unsupported,
                $"捕获工具退出码 {rc}{(reason.Length > 0 ? "：" + reason : "")}", output);
        }
        catch (Exception ex)
        {
            return new AppAudioProbeResult(AppAudioVerdict.ToolMissing, "探测异常: " + ex.Message, "");
        }
    }

    /// <summary>从工具输出里挑一行人话（带「!」的那行是引擎给的说明）。</summary>
    private static string ExtractReason(string output)
    {
        foreach (var raw in output.Split('\n'))
        {
            var line = raw.Trim().TrimStart('!').Trim();
            if (line.Length == 0) continue;
            if (line.Contains("暂未开放") || line.Contains("无法开始录音") ||
                line.Contains("失败") || line.Contains("不支持"))
                return line;
        }
        return "";
    }

    /// <summary>
    /// 找捕获工具：① 应用 exe 同级 ② exe 同级 tools\ ③ 显式测试后门（环境变量）。
    /// 与 NativeVideoHost 找助手 exe 的顺序保持一致。
    ///
    /// 【2026-10-06 修复】原来这里写死了**开发机绝对路径**（盘符 + 项目目录）——
    /// 产品代码不该出现开发机路径。现在只用"应用目录 / tools"，开发期要指向树内产物
    /// 必须显式设 <see cref="AppConfig.ProbePathEnv"/>（测试后门，名字自带语义）。
    /// </summary>
    private static string? ResolveToolPath()
    {
        var candidates = new System.Collections.Generic.List<string>
        {
            Path.Combine(AppContext.BaseDirectory, "zxprobe.exe"),
            Path.Combine(AppContext.BaseDirectory, "tools", "zxprobe.exe"),
        };

        // 显式命名的测试后门：只在真的设了才用（正常交付环境不设 → 不存在开发机路径依赖）
        var backdoor = Environment.GetEnvironmentVariable(AppConfig.ProbePathEnv);
        if (!string.IsNullOrWhiteSpace(backdoor)) candidates.Insert(0, backdoor);

        foreach (var c in candidates)
            if (File.Exists(c)) return c;
        return null;
    }
}
