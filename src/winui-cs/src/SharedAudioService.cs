/*
SharedAudioService.cs —— 「共享电脑声音」的采集泵生命周期（S2 拆桥，2026-10-06）。

【它负责什么】
  · 把界面选中的"声音来源"（spec）解析成引擎参数（loopback / process:<pid>）
  · 找探针、起采集泵、把引擎 stdout 逐行转给日志、停止与释放
  · 测试后门（ZX_SHARE_AUDIO）的**优先级**：只有界面没有任何选择时才用，并且必须能被记进日志
【它不负责什么】不碰控件、不推页面（推页面是 UI 线程的事，见 MainWindow.PushSharedAudioToPageAsync）、
不决定文案。拆桥前这些和窗口状态混在 MainWindow 里（`FindProbeExe` + `Process.Start` + 循环读 stdout）。

【两个必须记住的坑】
  1. 推送 PCM 到页面必须**编组到 UI 线程**（WebView2 套间绑定，后台线程会 RPC_E_WRONG_THREAD
     且异常被吞 ⇒ 音轨在、但对端一直静音）。这里不负责推送，但停止时要注意泵的读线程。
  2. 后门不许变成隐形入口：走没走后门要由调用方写日志（IsBackdoorUsed）。
*/
using System;
using System.Threading.Tasks;

namespace ZongxianVoice;

internal sealed class SharedAudioService
{
    private readonly Action<string> _log;
    private ShareAudioCapture? _capture;

    public SharedAudioService(Action<string>? log = null) => _log = log ?? (_ => { });

    public ShareAudioCapture? Capture => _capture;
    public bool IsRunning => _capture is not null;

    /// <summary>解析后的采集目标。</summary>
    public sealed record Target(string Kind, int Pid, string Spec)
    {
        public bool IsProcess => Pid > 0 && Kind == "process";
    }

    /// <summary>把 "loopback" / "process:1234" 解析成结构化目标（非法输入一律当整个系统）。</summary>
    public static Target ParseSpec(string? spec)
    {
        var s = (spec ?? "").Trim();
        if (s.Length == 0) return new Target("loopback", 0, "loopback");
        var colon = s.IndexOf(':');
        if (colon > 0)
        {
            var kind = s[..colon];
            if (int.TryParse(s[(colon + 1)..], out var pid) && pid > 0)
                return new Target(kind, pid, s);
        }
        return new Target(s, 0, s);
    }

    /// <summary>
    /// 测试后门：环境变量指定的采集目标。**只应在界面没有任何选择时使用**。
    /// 返回 null 表示"没用后门"（正常路径）。
    /// </summary>
    public static string? BackdoorSpec()
    {
        var v = Environment.GetEnvironmentVariable(AppConfig.ShareAudioBackdoorEnv);
        return string.IsNullOrWhiteSpace(v) ? null : v.Trim();
    }

    /// <summary>启动采集。成功返回 true；失败会写日志并说明原因（探针缺失/进程起不来）。</summary>
    public async Task<bool> StartAsync(string spec, Func<string, Task> onPcmBase64)
    {
        var target = ParseSpec(spec);
        var exe = ProbeLocator.FindProbe();
        if (exe is null)
        {
            _log("[共享声音] 找不到 zxprobe.exe，无法采集");
            return false;
        }

        Stop();   // 幂等：重复开就先关掉旧的
        _capture = ShareAudioCapture.Start(exe, target.Kind, target.Pid);
        if (_capture is null)
        {
            _log("[共享声音] 启动引擎采集失败");
            return false;
        }

        var cap = _capture;
        cap.StartPump(b64 => onPcmBase64(b64));
        _log($"[共享声音] 已开始采集（{target.Kind}{(target.Pid > 0 ? " pid=" + target.Pid : "")}），推给页面生成独立音轨");
        _log($"[共享声音] 引擎启动：{exe} {cap.LaunchArgs}");

        // 引擎 stdout 逐行转日志（引擎退出时 read 返回 null，循环自然结束）
        _ = Task.Run(async () =>
        {
            while (true)
            {
                var line = await cap.ReadEngineLineAsync();
                if (line is null) break;
                _log("[共享声音引擎] " + line);
            }
        });
        await Task.CompletedTask;
        return true;
    }

    /// <summary>停止采集并释放进程（幂等）。返回是否确实停掉了一个正在跑的泵。</summary>
    public bool Stop()
    {
        if (_capture is null) return false;
        try { _capture.Dispose(); }
        catch (Exception ex) { _log("[共享声音] 停止时出错：" + ex.Message); }
        _capture = null;
        return true;
    }
}
