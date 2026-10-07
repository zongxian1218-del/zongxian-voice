/*
ProbeLocator.cs —— 找 zxprobe.exe 的**唯一实现**（S2 拆桥，2026-10-06）。

【为什么要有它】拆桥前，"找探针"这件事在三个地方各写了一遍：
  · MainWindow.FindProbeExe()          —— 应用目录 / tools
  · AppAudioProbe.ResolveToolPath()    —— 应用目录 / tools（外加曾经硬编码的开发机路径）
  · NativeVideoHost 找助手 exe          —— 同样的两处
三份各写各的，正是审计病根 2（同一件事两三份实现）的小号版本；一旦打包布局变了，
只改一处就会让另外两处静默失效（旧探针副本就是这么把"共享电脑声音"弄成静默失效的）。
现在只留这一份：
  1) 显式测试后门（环境变量，只用于开发/自检，命名都带 ZX_ 前缀）
  2) 应用目录（便携版布局）
  3) 应用目录\tools（打包版布局）
**不允许**出现开发机绝对路径（审计真 bug #9）。
*/
using System;
using System.IO;

namespace ZongxianVoice;

internal static class ProbeLocator
{
    /// <summary>引擎探针（zxprobe.exe）的全路径；找不到返回 null。</summary>
    public static string? FindProbe() => Find("zxprobe.exe", AppConfig.ProbePathEnv);

    /// <summary>原生视频助手（receiver-probe.exe）的全路径；找不到返回 null。</summary>
    public static string? FindVideoHelper() => Find("receiver-probe.exe", AppConfig.VideoHelperPathEnv);

    /// <summary>
    /// 按"后门 → 应用目录 → tools 子目录"的顺序找。
    /// 环境变量只当**测试后门**：填了就先用它，但调用方应在日志里写明"走了后门"。
    /// </summary>
    public static string? Find(string exeName, string envOverride)
    {
        var fromEnv = Environment.GetEnvironmentVariable(envOverride);
        if (!string.IsNullOrWhiteSpace(fromEnv) && File.Exists(fromEnv)) return fromEnv;

        var dir = AppContext.BaseDirectory;
        foreach (var rel in new[] { exeName, Path.Combine("tools", exeName) })
        {
            var p = Path.Combine(dir, rel);
            if (File.Exists(p)) return p;
        }
        return null;
    }

    /// <summary>是否正在用测试后门（日志里要如实说，别让它变成隐形入口）。</summary>
    public static bool UsingBackdoor(string exeName, string envOverride)
    {
        var fromEnv = Environment.GetEnvironmentVariable(envOverride);
        if (string.IsNullOrWhiteSpace(fromEnv) || !File.Exists(fromEnv)) return false;
        return string.Equals(Path.GetFileName(fromEnv), exeName, StringComparison.OrdinalIgnoreCase);
    }
}
