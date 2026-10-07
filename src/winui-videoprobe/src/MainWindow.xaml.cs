using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Windows.Foundation;
using Windows.Graphics;

namespace VideoProbe;

public sealed partial class MainWindow : Window
{
    // 推流端分辨率。只用来算显示区的宽高比（letterbox），**不再要求视频区尺寸等于它**。
    // 覆盖窗口可以由父应用指定任意尺寸，画面由 DXGI_SCALING_STRETCH 拉伸填满客户区。
    private int StreamW = 1280;
    private int StreamH = 720;

    // 助手 exe 的查找顺序（**不要写死绝对路径**：写死之后换机器/换目录就找不到助手，
    // 之前那串 D:\文档\ai001\... 只是本机的开发路径）：
    //   1) 应用 exe 同级            —— 打包后的正常形态
    //   2) 应用 exe 同级 media\     —— 助手跟媒体资源一起分发时的形态
    //   3) 开发树默认位置           —— 用源码直接跑时的兜底
    private static string ResolveHelperPath()
    {
        var candidates = new[]
        {
            System.IO.Path.Combine(AppContext.BaseDirectory, "receiver-probe.exe"),
            System.IO.Path.Combine(AppContext.BaseDirectory, "media", "receiver-probe.exe"),
            @"D:\文档\ai001\src\media\probe\build\receiver-probe.exe",
        };
        foreach (var c in candidates)
        {
            if (System.IO.File.Exists(c)) return c;
        }
        return candidates[0];
    }

    private readonly string HelperPath = ResolveHelperPath();

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT { public int X; public int Y; }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool ClientToScreen(IntPtr hWnd, ref POINT p);

    private Process? _proc;
    private IntPtr _hwnd;
    private Microsoft.UI.Windowing.AppWindow? _appWin;

    public MainWindow()
    {
        InitializeComponent();
        Title = "棕仙语音 — 视频嵌入验证";

        _hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        var id = Microsoft.UI.Win32Interop.GetWindowIdFromWindow(_hwnd);
        _appWin = Microsoft.UI.Windowing.AppWindow.GetFromWindowId(id);
        _appWin.Resize(new SizeInt32(1360, 880));
        // 窗口一动（移动/改尺寸/最小化还原），覆盖在它上面的视频窗口也要跟着动
        _appWin.Changed += (_, e) =>
        {
            if (e.DidPositionChange || e.DidSizeChange || e.DidPresenterChange) SendRect();
        };

        // 【不再固定视频区尺寸】原来写死 1280x720（因为渲染器 CopyResource 要求后台缓冲同尺寸），
        // 现在助手侧靠 DXGI_SCALING_STRETCH 把画面拉伸到窗口客户区，视频区可以是任意尺寸。
        RootGrid.Loaded += (_, _) => Start_Click(this, new RoutedEventArgs());
    }

    // 视频**显示矩形**在屏幕上的物理像素位置与尺寸。
    // 覆盖窗口是顶层窗口，位置用屏幕坐标；XAML 用 DIP，所以要乘缩放比再换算。
    // 为什么要算矩形而不是直接用 VideoArea：视频区现在是任意尺寸，直接铺满会把画面拉变形，
    // 所以按流宽高比在其中居中（letterbox），覆盖窗口只盖这块居中的区域。
    private (int X, int Y, int W, int H) DisplayRectScreen()
    {
        var t = VideoArea.TransformToVisual(null);
        Point p = t.TransformPoint(new Point(0, 0));
        double scale = VideoArea.XamlRoot?.RasterizationScale ?? 1.0;

        double availW = Math.Max(1, VideoArea.ActualWidth);
        double availH = Math.Max(1, VideoArea.ActualHeight);
        double aspect = (StreamH > 0) ? (double)StreamW / StreamH : 16.0 / 9.0;

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

    private void SendRect()
    {
        // 【最小化要把覆盖窗口藏起来】覆盖窗口是顶层窗口，不会跟着应用一起最小化；
        // 不藏的话屏幕上会一直挂着一块"孤儿"画面。还原时再发一次 rect 就会显示回来
        // （助手的 rect 命令带 SWP_SHOWWINDOW）。
        var presenter = _appWin?.Presenter as Microsoft.UI.Windowing.OverlappedPresenter;
        if (presenter is not null &&
            presenter.State == Microsoft.UI.Windowing.OverlappedPresenterState.Minimized)
        {
            SendLine("hide");
            return;
        }

        var (x, y, w, h) = DisplayRectScreen();
        // 每次调整都落盘：窗口尺寸/位置一变，这里就是和助手日志对照的最新依据
        WriteDiag(x, y, w, h);
        SendLine($"rect {x} {y} {w} {h}");
    }

    private void SendLine(string text)
    {
        if (_proc is null || _proc.HasExited) return;
        try
        {
            _proc.StandardInput.WriteLine(text);
            _proc.StandardInput.Flush();
        }
        catch { /* 进程可能已退出 */ }
    }

    // 记录助手 stdout 的每一行（覆盖式：每次启动重开）。用于诊断协议层问题。
    private int _stdoutLines;
    private string _lastStdout = "";

    private void LogStdout(string line)
    {
        _stdoutLines++;
        _lastStdout = line.Length > 160 ? line.Substring(0, 160) : line;
        try
        {
            System.IO.File.AppendAllText(
                System.IO.Path.Combine(AppContext.BaseDirectory, "videoprobe-stdout.txt"),
                line + "\r\n");
        }
        catch { /* 诊断失败不能影响主流程 */ }
    }

    // 诊断用：把当前显示矩形落盘，方便和助手日志里的实际矩形对照。
    // 【为什么写到 exe 旁边而不是 %TEMP%】本机 harness 下，被启动的子进程写 %TEMP% 会失败：
    // sender-probe 用 _wfopen_s 写 %TEMP% 直接报"无法创建输出文件"，换成项目内路径同一份 exe 就成功。
    // 上一轮"位置文件没生成 → 判定 RootGrid.Loaded 没触发"的结论就是踩了这个坑（而且这里原本
    // 用空的 catch 静默吞掉了失败）。改成 exe 同级 + 失败时显示出来，让诊断本身可靠。
    private void WriteDiag(int x, int y, int w, int h)
    {
        try
        {
            var diagPath = System.IO.Path.Combine(AppContext.BaseDirectory, "videoprobe-pos.txt");
            System.IO.File.WriteAllText(diagPath,
                $"hwnd=0x{_hwnd.ToInt64():X}\r\nareaScreen={x},{y}\r\ndisplaySize={w}x{h}\r\n" +
                $"rasterScale={VideoArea.XamlRoot?.RasterizationScale}\r\n" +
                $"videoAreaActual={VideoArea.ActualWidth}x{VideoArea.ActualHeight}\r\n" +
                $"stream={StreamW}x{StreamH}\r\n" +
                $"stdoutLines={_stdoutLines}\r\nlastStdout={_lastStdout}\r\n" +
                $"utc={DateTime.UtcNow:O}\r\n");
        }
        catch (Exception ex)
        {
            StatusText.Text = "诊断文件写入失败: " + ex.Message;
        }
    }

    private void Start_Click(object sender, RoutedEventArgs e)
    {
        if (_proc is not null) return;

        if (!System.IO.File.Exists(HelperPath))
        {
            StatusText.Text = "找不到助手进程: " + HelperPath;
            return;
        }

        var (x, y, w, h) = DisplayRectScreen();
        StatusText.Text = $"启动助手… 显示区 ({x},{y}) {w}x{h}";
        _stdoutLines = 0;
        _lastStdout = "";
        try
        {
            System.IO.File.WriteAllText(
                System.IO.Path.Combine(AppContext.BaseDirectory, "videoprobe-stdout.txt"),
                $"; 助手 stdout 原始记录（每次启动重开） utc={DateTime.UtcNow:O}\r\n");
        }
        catch { /* 诊断失败不能影响主流程 */ }
        WriteDiag(x, y, w, h);

        var psi = new ProcessStartInfo
        {
            FileName = HelperPath,
            // 【为什么用无边框覆盖窗口而不是子窗口】
            // 试过 --parent-hwnd 建立真正的子窗口，但 WinUI 3 把 XAML 内容渲染在
            // 一个 DesktopChildSiteBridge 里，会不断把内容岛提到 z 序最前，
            // 把我们的子窗口盖住（实测助手已解码 300 帧，界面上却全黑）。
            // 顶层无边框窗口没有这个 z 序竞争。
            // 前 5 个位置参数是「视频」尺寸，不是窗口尺寸：助手按它建 swapchain，
            // 窗口大小随后由 rect 命令按显示区实际尺寸调整（DXGI 负责拉伸）。
            // --remote-control：这一版默认打开（把覆盖窗口上的鼠标转发给被控端），
            //   正式应用必须按设计文档 §9.5 走用户授权后才打开，并配合"停止控制"横幅。
            Arguments = $"86400 41001 {StreamW} {StreamH} 30 --window --frameless --topmost " +
                        $"--remote-control --pos {x} {y}",
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
                // 把助手 stdout 原样落盘：这是应用↔助手之间唯一的协议通道，
                // 出问题时（比如状态栏不刷新）必须能看清"到底收到了什么"，
                // 而不是靠猜。见 WriteDiag 的 eventLines 计数。
                LogStdout(a.Data);

                // 只认 ASCII 事件行：助手的中文日志经管道传过来编码对不上
                // （C# 侧按 UTF-8 解码、CRT 写管道用 ANSI 代码页），
                // 原来靠 Contains("已解码") 匹配，结果状态栏永远停在"等待推流…"。
                var line = a.Data.Trim();
                DispatcherQueue.TryEnqueue(() =>
                {
                    if (line.StartsWith("event view-ready"))
                    {
                        StatusText.Text = "窗口已就绪，等待推流…";
                    }
                    else if (line.StartsWith("event stream-size "))
                    {
                        var parts = line.Split(' ');
                        if (parts.Length >= 4 &&
                            int.TryParse(parts[2], out var sw) && int.TryParse(parts[3], out var sh) &&
                            sw > 0 && sh > 0)
                        {
                            StreamW = sw;
                            StreamH = sh;
                            SendRect();     // 按真实宽高比重算显示区（非 16:9 的流才不会被拉变形）
                            StatusText.Text = $"已收到画面 {sw}x{sh}";
                        }
                    }
                    else if (line.StartsWith("event stats "))
                    {
                        StatusText.Text = "接收中  " + line.Substring("event stats ".Length);
                    }
                });
            };
            _proc.Exited += (_, _) => DispatcherQueue.TryEnqueue(() =>
            {
                StatusText.Text = "助手已退出";
                StartBtn.IsEnabled = true;
                StopBtn.IsEnabled = false;
                _proc = null;
            });
            _proc.Start();
            _proc.BeginOutputReadLine();

            // 助手刚建好的窗口尺寸 = 视频尺寸，这里立刻按显示区实际尺寸纠正一次
            SendRect();

            StartBtn.IsEnabled = false;
            StopBtn.IsEnabled = true;
            StatusText.Text = "助手已启动，等待推流端…";
        }
        catch (Exception ex)
        {
            StatusText.Text = "启动失败: " + ex.Message;
            _proc = null;
        }
    }

    private void Stop_Click(object sender, RoutedEventArgs e) => StopHelper();

    private void StopHelper()
    {
        if (_proc is null) return;
        try
        {
            _proc.StandardInput.WriteLine("quit");
            _proc.StandardInput.Flush();
            if (!_proc.WaitForExit(3000)) _proc.Kill(entireProcessTree: true);
        }
        catch { /* 进程可能已经退出 */ }
        _proc = null;
        StartBtn.IsEnabled = true;
        StopBtn.IsEnabled = false;
        StatusText.Text = "已停止";
    }

    private void VideoArea_SizeChanged(object sender, SizeChangedEventArgs e) => SendRect();
}
