// NativeVideoHost.cs —— 把 C++ 助手的无边框顶层覆盖窗口贴到指定 XAML 元素上方，
// 作为 WinUI 3 里显示原生视频的第二条路径（P1-6「把原型搬进主应用」的第一片）。
//
// 逻辑全部来自**已验证的原型** src/winui-videoprobe/src/MainWindow.xaml.cs，这里抽成可复用类：
//   · 覆盖窗口（顶层窗口）而不是 WS_CHILD 子窗口
//     —— 试过 --parent-hwnd，WinUI 3 的 DesktopChildSiteBridge 会不断把内容岛提到最前，
//        把子窗口盖住（实测助手解码 300 帧、界面全黑），所以只能用顶层窗口；
//   · 助手按「视频尺寸」建 swapchain，窗口尺寸由 rect 命令控制，DXGI_SCALING_STRETCH 负责拉伸；
//   · 应用侧 letterbox：视频区是任意尺寸时按流宽高比居中，避免拉变形；
//   · 最小化要发 hide —— 顶层窗口不会跟着应用最小化，不藏就是屏幕上挂一块孤儿画面；
//   · 助手 stdout 只认 ASCII 事件行（中文经管道编码对不上，靠 Contains 匹配永远不中）。
//
// 【默认不启动】由调用方决定；正式接入时应由「开始共享」流程触发，且 --remote-control
// 必须等用户授权后才允许打开（设计文档 §9.5），所以这个类默认不带该参数。

using Microsoft.UI;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using Windows.Graphics;

namespace ZongxianVoice;

public sealed class NativeVideoHost : IDisposable
{
    private readonly FrameworkElement _area;      // 覆盖窗口要对齐的 XAML 元素
    private readonly Window _host;                // 主窗口（取 hwnd / AppWindow 用）
    private readonly Action<string>? _log;        // 诊断输出（应用自己的日志区）
    private readonly int _port;
    private readonly bool _enableRemoteControl;

    private IntPtr _hwnd;
    private AppWindow? _appWin;
    private Process? _proc;
    private int _streamW = 1280, _streamH = 720;
    private int _stdoutLines;
    private string _lastStdout = "";
    // 【必须切回 UI 线程】助手 stdout 的回调跑在**线程池线程**上。原型里每个回调都先
    // DispatcherQueue.TryEnqueue 再动状态/日志；我抽这个类时把这一步弄丢了，后果实测很硬：
    // 应用启动后立刻崩（KERNELBASE / 0xe0434352，托管异常），而不带原生视频路径就完全正常。
    private readonly Microsoft.UI.Dispatching.DispatcherQueue? _uiQueue;
    // Sync 去重要用的"上次发出去的矩形 / 是否正处于隐藏状态"
    private (int X, int Y, int W, int H) _lastRect = (-1, -1, -1, -1);
    private bool _hidden;
    private bool _degenerateLogged;   // "布局未就绪"只记一次，避免刷屏

    /// <summary>助手报出真实码流尺寸时触发（调用方据此更新状态栏/重算 letterbox）。</summary>
    public event Action<int, int>? StreamSizeChanged;

    /// <summary>助手 stdout 的 ASCII 事件行（view-ready / stream-size / stats）。</summary>
    public event Action<string>? HelperEvent;

    public bool IsRunning => _proc is { HasExited: false };

    public NativeVideoHost(FrameworkElement area, Window host, Action<string>? log = null,
                           int port = AppConfig.DefaultNativeVideoPort, bool enableRemoteControl = false)
    {
        _area = area;
        _host = host;
        _log = log;
        _port = port;
        _enableRemoteControl = enableRemoteControl;

        _hwnd = WinRT.Interop.WindowNative.GetWindowHandle(_host);
        var id = Microsoft.UI.Win32Interop.GetWindowIdFromWindow(_hwnd);
        _appWin = AppWindow.GetFromWindowId(id);
        _uiQueue = _area.DispatcherQueue;   // 助手 stdout 回调要切回这个队列

        // 布局或窗口状态一变，覆盖窗口立刻跟上。
        // 【不要挂 LayoutUpdated】它在每次布局都会触发。实测后果：同一个矩形连发 25 条 rect 命令，
        // 每次都伴随 SetWindowPos + 写盘诊断；已验证能出画面的原型只挂 SizeChanged + AppWindow.Changed。
        _area.SizeChanged += (_, _) => Sync();
        _appWin.Changed += (_, e) =>
        {
            if (e.DidPositionChange || e.DidSizeChange || e.DidPresenterChange) Sync();
        };
    }

    // 助手 exe 的查找顺序（不要写死绝对路径，换机器/换目录就找不到）：
    //   1) 应用 exe 同级  2) exe 同级 media\  3) 显式测试后门（环境变量）
    // 【2026-10-06 修复】原来第三项写死**开发机绝对路径**（盘符 + 项目目录）——
    // 产品代码不许出现开发机路径；开发期要指向树内产物必须显式设
    // AppConfig.VideoHelperPathEnv（测试后门）。
    private static string ResolveHelperPath()
    {
        var candidates = new System.Collections.Generic.List<string>
        {
            Path.Combine(AppContext.BaseDirectory, "receiver-probe.exe"),
            Path.Combine(AppContext.BaseDirectory, "media", "receiver-probe.exe"),
        };
        var backdoor = Environment.GetEnvironmentVariable(AppConfig.VideoHelperPathEnv);
        if (!string.IsNullOrWhiteSpace(backdoor)) candidates.Insert(0, backdoor);
        foreach (var c in candidates) if (File.Exists(c)) return c;
        return candidates[0];
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT { public int X; public int Y; }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool ClientToScreen(IntPtr hWnd, ref POINT p);

    /// <summary>视频显示矩形在屏幕上的物理像素位置与尺寸（按流宽高比 letterbox 居中）。</summary>
    public (int X, int Y, int W, int H) DisplayRectScreen()
    {
        // 元素还没进视觉树时 TransformToVisual 会抛异常。调用方（构造函数阶段）可能来得太早，
        // 这里直接返回一个退化矩形，等 Loaded 后再算 —— 不要让它把调用方整段打断。
        if (_area.XamlRoot is null) return (0, 0, 1, 1);

        var t = _area.TransformToVisual(null);
        var p = t.TransformPoint(new Windows.Foundation.Point(0, 0));
        double scale = _area.XamlRoot?.RasterizationScale ?? 1.0;

        double availW = Math.Max(1, _area.ActualWidth);
        double availH = Math.Max(1, _area.ActualHeight);
        double aspect = (_streamH > 0) ? (double)_streamW / _streamH : 16.0 / 9.0;

        double w = availW, h = availW / aspect;
        if (h > availH) { h = availH; w = availH * aspect; }
        double ox = (availW - w) / 2.0, oy = (availH - h) / 2.0;

        var pt = new POINT
        {
            X = (int)Math.Round((p.X + ox) * scale),
            Y = (int)Math.Round((p.Y + oy) * scale),
        };
        ClientToScreen(_hwnd, ref pt);
        return (pt.X, pt.Y,
                Math.Max(1, (int)Math.Round(w * scale)),
                Math.Max(1, (int)Math.Round(h * scale)));
    }

    /// <summary>让覆盖窗口跟上当前布局/窗口状态（最小化时藏起来）。</summary>
    public void Sync(bool force = false)
    {
        if (!IsRunning) return;
        var presenter = _appWin?.Presenter as OverlappedPresenter;
        if (presenter is not null && presenter.State == OverlappedPresenterState.Minimized)
        {
            if (!_hidden || force) { SendLine("hide"); _hidden = true; }
            return;
        }

        var rect = DisplayRectScreen();
        // 【布局未就绪时算出来的矩形是退化的，不要发】
        // 实测（两实例联调，观看方）：第一次 Sync 算出 1x1 —— 因为 ScreenView 刚被设为 Visible、
        // 布局还没量到尺寸，于是覆盖窗口瞬间被设成 1x1，日志里就是 `rect 命令：窗口 -> ... 1x1`。
        // 这种矩形直接丢掉，等布局完成后的 SizeChanged 再同步（那一刻的矩形是对的）。
        if (rect.W < 8 || rect.H < 8)
        {
            if (!_degenerateLogged)
            {
                _degenerateLogged = true;
                _log?.Invoke($"[原生视频] 布局未就绪（算出 {rect.W}x{rect.H}），等 SizeChanged 再同步");
            }
            return;
        }
        _degenerateLogged = false;

        // 【去重】矩形没变就不要再发。原型每次 SizeChanged 都发；我这版一度还挂了 LayoutUpdated，
        // 实测同一个矩形发了 25 次 —— 既刷日志，又反复 SetWindowPos（flip 模型下不划算）。
        if (!force && !_hidden && rect == _lastRect) return;

        WriteDiag(rect.X, rect.Y, rect.W, rect.H);
        SendLine($"rect {rect.X} {rect.Y} {rect.W} {rect.H}");
        _lastRect = rect;
        _hidden = false;
    }

    public bool Start()
    {
        if (IsRunning) return true;
        var helper = ResolveHelperPath();
        if (!File.Exists(helper)) { _log?.Invoke("找不到助手进程: " + helper); return false; }

        var (x, y, w, h) = DisplayRectScreen();
        _stdoutLines = 0;
        _lastStdout = "";
        try
        {
            File.WriteAllText(Path.Combine(AppContext.BaseDirectory, "nativevideo-stdout.txt"),
                $"; 助手 stdout 原始记录（每次启动重开） utc={DateTime.UtcNow:O}\r\n");
        }
        catch { /* 诊断失败不影响主流程 */ }
        WriteDiag(x, y, w, h);

        var args = $"86400 {_port} {_streamW} {_streamH} 30 --window --frameless --topmost " +
                   $"--pos {x} {y}";
        if (_enableRemoteControl) args += " --remote-control";

        var psi = new ProcessStartInfo
        {
            FileName = helper,
            Arguments = args,
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
            StandardOutputEncoding = System.Text.Encoding.UTF8,
        };

        try
        {
            _proc = new Process { StartInfo = psi, EnableRaisingEvents = true };
            _proc.OutputDataReceived += (_, a) =>
            {
                if (string.IsNullOrEmpty(a.Data)) return;
                LogStdout(a.Data);                 // 只写文件，与线程无关
                var line = a.Data.Trim();
                // 【切回 UI 线程再动状态/回调】否则会在线程池线程上碰 UI 与共享状态
                //（实测后果：进程直接崩，0xe0434352）
                _uiQueue?.TryEnqueue(() =>
                {
                    if (line.StartsWith("event stream-size "))
                    {
                        var parts = line.Split(' ');
                        if (parts.Length >= 4 && int.TryParse(parts[2], out var sw) &&
                            int.TryParse(parts[3], out var sh) && sw > 0 && sh > 0)
                        {
                            _streamW = sw; _streamH = sh;
                            Sync();                                   // 按真实宽高比重算显示区
                            StreamSizeChanged?.Invoke(sw, sh);
                        }
                    }
                    HelperEvent?.Invoke(line);
                });
            };
            _proc.Exited += (_, _) => _uiQueue?.TryEnqueue(() => _log?.Invoke("原生视频助手已退出"));
            _proc.Exited += (_, _) => _log?.Invoke("原生视频助手已退出");
            _proc.Start();
            _proc.BeginOutputReadLine();
            // 【锚点】把主窗口句柄告诉助手：它会把覆盖窗口插到主窗口正上方。
            // 实测这个覆盖窗口拿不到 WS_EX_TOPMOST（见 docs/winui-overlay-verified 的后续一节），
            // 而产品真正需要的是"视频盖在自己应用窗口之上"，不是"盖住屏幕上所有窗口"。
            SendLine($"anchor 0x{_hwnd.ToInt64():X}");
            Sync();   // 助手刚建好的窗口 = 视频尺寸，立刻按显示区纠正一次
            _log?.Invoke($"原生视频助手已启动（显示区 {x},{y} {w}x{h}，锚点 0x{_hwnd.ToInt64():X}）");
            return true;
        }
        catch (Exception ex)
        {
            _log?.Invoke("原生视频助手启动失败: " + ex.Message);
            _proc = null;
            return false;
        }
    }

    public void Stop()
    {
        if (_proc is null) return;
        try
        {
            _proc.StandardInput.WriteLine("quit");
            _proc.StandardInput.Flush();
            if (!_proc.WaitForExit(AppConfig.HelperExitWaitMs)) _proc.Kill(entireProcessTree: true);
        }
        catch { /* 可能已退出 */ }
        _proc = null;
    }

    private void SendLine(string text)
    {
        if (_proc is null || _proc.HasExited) return;
        try { _proc.StandardInput.WriteLine(text); _proc.StandardInput.Flush(); }
        catch { /* 进程可能已退出 */ }
    }

    private void LogStdout(string line)
    {
        _stdoutLines++;
        _lastStdout = line.Length > 160 ? line.Substring(0, 160) : line;
        try
        {
            File.AppendAllText(Path.Combine(AppContext.BaseDirectory, "nativevideo-stdout.txt"),
                               line + "\r\n");
        }
        catch { /* 诊断失败不影响主流程 */ }
    }

    // 诊断：把当前显示矩形落盘，方便和助手日志里的实际矩形对照。
    // 【写到 exe 旁边而不是 %TEMP%】本机环境下被启动的子进程写 %TEMP% 会失败，
    // 上一轮"位置文件没生成 → 误判 Loaded 没触发"就是踩了这个坑（而且原来用空 catch 吞掉了）。
    private void WriteDiag(int x, int y, int w, int h)
    {
        try
        {
            File.WriteAllText(Path.Combine(AppContext.BaseDirectory, "nativevideo-pos.txt"),
                $"hwnd=0x{_hwnd.ToInt64():X}\r\nareaScreen={x},{y}\r\ndisplaySize={w}x{h}\r\n" +
                $"rasterScale={_area.XamlRoot?.RasterizationScale}\r\n" +
                $"areaActual={_area.ActualWidth}x{_area.ActualHeight}\r\n" +
                $"stream={_streamW}x{_streamH}\r\n" +
                $"stdoutLines={_stdoutLines}\r\nlastStdout={_lastStdout}\r\n" +
                $"url={Environment.GetEnvironmentVariable(AppConfig.NativeVideoEnv)}\r\n" +
                $"utc={DateTime.UtcNow:O}\r\n");
        }
        catch { /* 诊断失败不影响主流程 */ }
    }

    public void Dispose() => Stop();
}
