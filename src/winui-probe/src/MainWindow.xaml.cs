// MainWindow.xaml.cs —— 屏幕采集 + 硬件编码 验证（WinUI 版）
//
// ============================================================================
// 【为什么从控制台改成 WinUI】
// 控制台版在 CreateForMonitor 上返回 E_ACCESSDENIED (0x80070005)。
// 需要验证的假设是：WGC 的采集互操作要求调用线程具备 DispatcherQueue
// （WinUI 线程天然有，纯控制台没有）。
//
// 而最终产品本来就要在 WinUI 里显示画面，所以这一步不算绕路。
//
// 本程序自动执行以下验证并写入 zx-capture-log.txt：
//   1. DispatcherQueue 是否存在
//   2. 能否创建 GraphicsCaptureItem（控制台版失败的那一步）
//   3. 持续采集 N 秒，统计实际帧率
//   4. 能否创建 H.264 硬件编码器，并打印它的名字
//
// 用法：Probe.exe [秒数]
// ============================================================================

using Microsoft.UI.Xaml;
using System;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using Windows.Graphics.Capture;
using Windows.Graphics.DirectX;
using Windows.Graphics.DirectX.Direct3D11;

namespace Probe;

public sealed partial class MainWindow : Window
{
    private static readonly StringBuilder LogBuf = new();
    // 写到固定位置（临时目录），避免 BaseDirectory 不可写导致"日志没生成"
    private static readonly string LogPath =
        Path.Combine(Path.GetTempPath(), "zx-capture-log.txt");

    private static void Log(string line)
    {
        LogBuf.AppendLine(line);
        try { File.WriteAllText(LogPath, LogBuf.ToString(), new UTF8Encoding(false)); }
        catch { /* 日志写不进去也不能影响验证 */ }
    }

    public MainWindow()
    {
        InitializeComponent();
        _ = RunProbeAsync();
    }

    private async Task RunProbeAsync()
    {
        var args = Environment.GetCommandLineArgs();
        var seconds = 5;
        var useDxgi = false;

        foreach (var a in args)
        {
            if (a.Equals("--dxgi", StringComparison.OrdinalIgnoreCase)) useDxgi = true;
            else if (int.TryParse(a, out var s) && s > 0) seconds = s;
        }

        if (useDxgi)
        {
            RunDxgiProbe(seconds);
            return;
        }

        Log("=== 屏幕采集验证（WinUI 版 / WGC）===");
        Log($"时间: {DateTime.Now:HH:mm:ss}   采集时长: {seconds} 秒");

        // ---- 假设 1：DispatcherQueue 存在 ----
        var dq = DispatcherQueue;
        Log($"[{(dq is null ? "X" : "OK")}] DispatcherQueue: " +
            (dq is null ? "不存在" : "存在（这正是控制台版缺少的）"));
        if (dq is null)
        {
            Log("结论：拿不到 DispatcherQueue，本假设不成立，需要换方向排查");
            Finish();
            return;
        }

        // ---- WGC 支持性 ----
        var supported = GraphicsCaptureSession.IsSupported();
        Log($"[{(supported ? "OK" : "X")}] Windows.Graphics.Capture 支持: {supported}");

        // ---- 会话与权限诊断 ----
        // CreateForMonitor 返回 E_ACCESSDENIED 时，最常见的原因不是代码，
        // 而是"当前会话类型不允许采集"（远程桌面会话、无交互桌面等）。
        // 所以把这些环境事实先记下来，避免继续在代码里猜。
        try
        {
            Log($"[信息] 会话 ID      : {System.Diagnostics.Process.GetCurrentProcess().SessionId}");
            Log($"[信息] 远程会话     : {GetSystemMetrics(0x1000) != 0}  (SM_REMOTESESSION)");
            Log($"[信息] 窗口站       : {GetWindowStationName()}");
            Log($"[信息] 交互式桌面   : {Environment.UserInteractive}");
            Log($"[信息] 进程完整性   : {GetIntegrityLevel()}");
        }
        catch (Exception ex)
        {
            Log($"[信息] 会话诊断失败: {ex.Message}");
        }

        // ---- 假设 2：逐个显示器尝试创建采集源 ----
        // 这台机器装了两个虚拟显示器驱动（GameViewer、OrayIddDriver）。
        // 如果主显示器恰好是虚拟的，WGC 可能拒绝采集它，所以逐个试。
        var monitors = CaptureHelper.EnumMonitors();
        Log($"[信息] 共 {monitors.Count} 个显示器:");
        foreach (var m in monitors) Log($"        {m.Name}  句柄=0x{m.Handle:X}");

        GraphicsCaptureItem? item = null;
        foreach (var m in monitors)
        {
            try
            {
                var candidate = CaptureHelper.CreateItemForMonitor(m.Handle);
                if (candidate is not null)
                {
                    Log($"[OK] 采集源已创建: {m.Name}  " +
                        $"{candidate.Size.Width} x {candidate.Size.Height}");
                    item = candidate;
                    break;
                }
            }
            catch (Exception ex)
            {
                Log($"[X] {m.Name} 创建失败: {ex.GetType().Name} 0x{ex.HResult:X8}");
            }
        }

        if (item is null)
        {
            Log("[X] 所有显示器都无法创建采集源");
            Log("     → 既不是 DispatcherQueue，也不是虚拟显示器的问题");
            Finish();
            return;
        }

        Log("[OK] 采集源就绪");

        // ---- 假设 3：持续采集是否稳定 ----
        var sw = System.Diagnostics.Stopwatch.StartNew();
        long frames = 0;
        var lastReport = TimeSpan.Zero;

        try
        {
            using var framePool = Direct3D11CaptureFramePool.CreateFreeThreaded(
                CaptureHelper.WinRTDevice!, DirectXPixelFormat.B8G8R8A8UIntNormalized,
                2, item.Size);
            using var session = framePool.CreateCaptureSession(item);

            try { session.IsCursorCaptureEnabled = true; } catch { }

            var done = new TaskCompletionSource();
            framePool.FrameArrived += (pool, _) =>
            {
                var frame = pool.TryGetNextFrame();
                if (frame is null) return;
                frames++;
                frame.Dispose();

                var el = sw.Elapsed;
                if (el - lastReport >= TimeSpan.FromSeconds(1))
                {
                    lastReport = el;
                    Log($"      第 {el.TotalSeconds:F0} 秒: 累计 {frames} 帧 " +
                        $"({frames / el.TotalSeconds:F1} fps)");
                }
                if (el.TotalSeconds >= seconds) done.TrySetResult();
            };

            session.StartCapture();
            Log("[OK] 采集会话已启动");
            await done.Task;
        }
        catch (Exception ex)
        {
            Log($"[X] 采集过程异常: {ex.GetType().Name} 0x{ex.HResult:X8} {ex.Message}");
        }

        sw.Stop();
        var fps = sw.Elapsed.TotalSeconds > 0 ? frames / sw.Elapsed.TotalSeconds : 0;

        // ---- 假设 4：H.264 硬件编码器 ----
        var encName = await Task.Run(() => EncoderProbe.TryFindHardwareH264(out var n) ? n : null);
        Log(encName is null
            ? "[X] 未找到 H.264 硬件编码器"
            : $"[OK] H.264 硬件编码器: {encName}");

        // ---- 结论 ----
        Log("");
        Log("=== 结论 ===");
        Log($"  采集源创建 : 成功（控制台版同一步失败）");
        Log($"  采集时长   : {sw.Elapsed.TotalSeconds:F2} 秒");
        Log($"  采集帧数   : {frames}");
        Log($"  实测帧率   : {fps:F1} fps");
        Log($"  硬件编码器 : {(encName is null ? "无" : encName)}");
        Log("");
        if (frames > 0 && fps >= 25 && encName is not null)
            Log("  判定：采集 + 硬件编码 可用，可以进入下一步（UDP 传输）");
        else if (frames > 0)
            Log($"  判定：采集可用但需解决问题（帧率 {fps:F1}，编码器 {encName ?? "无"}）");
        else
            Log("  判定：采集没有拿到帧 —— 必须查清楚再往下做");

        Finish();
    }

    /// <summary>
    /// DXGI Desktop Duplication 采集验证。
    ///
    /// 换掉 WGC 的原因：WGC 的采集源创建在这台机器上无条件返回
    /// E_ACCESSDENIED，而 DXGI 不需要任何 WinRT 互操作，未打包应用可用。
    /// </summary>
    private void RunDxgiProbe(int seconds)
    {
        Log("=== 屏幕采集验证（DXGI Desktop Duplication）===");
        Log($"时间: {DateTime.Now:HH:mm:ss}   采集时长: {seconds} 秒");

        using var cap = new DxgiCapture();
        cap.OnStep = s => Log($"      [步骤] {s}");   // 实时写盘，卡住也能看到
        var ok = cap.Initialize();
        if (!ok)
        {
            Log($"[X] 初始化失败: {cap.LastError}");
            Log("结论：DXGI 路线也不通，需要重新选方案");
            Finish();
            return;
        }

        Log($"[OK] 适配器: {cap.AdapterName}");
        Log($"[OK] 输出  : {cap.OutputName}  {cap.Width} x {cap.Height}");
        Log("[OK] 桌面复制接口已创建（WGC 那一步失败的对应位置）");

        // ---- 持续抓帧 ----
        var sw = System.Diagnostics.Stopwatch.StartNew();
        long frames = 0, timeouts = 0, errors = 0;
        var lastReport = TimeSpan.Zero;

        while (sw.Elapsed.TotalSeconds < seconds)
        {
            if (cap.TryGetFrame(out var texture, 500))
            {
                frames++;
                cap.ReleaseFrame();
                if (texture != IntPtr.Zero) System.Runtime.InteropServices.Marshal.Release(texture);
            }
            else
            {
                timeouts++;
            }

            var el = sw.Elapsed;
            if (el - lastReport >= TimeSpan.FromSeconds(1))
            {
                lastReport = el;
                Log($"      第 {el.TotalSeconds:F0} 秒: 帧 {frames}  " +
                    $"({frames / el.TotalSeconds:F1} fps)  超时 {timeouts}");
            }
        }

        sw.Stop();
        var fps = sw.Elapsed.TotalSeconds > 0 ? frames / sw.Elapsed.TotalSeconds : 0;

        var encName = EncoderProbe.TryFindHardwareH264(out var n) ? n : null;
        Log(encName is null
            ? "[X] 未找到 H.264 硬件编码器"
            : $"[OK] H.264 硬件编码器: {encName}");

        Log("");
        Log("=== 结论 ===");
        Log($"  分辨率     : {cap.Width} x {cap.Height}");
        Log($"  采集时长   : {sw.Elapsed.TotalSeconds:F2} 秒");
        Log($"  采集帧数   : {frames}");
        Log($"  实测帧率   : {fps:F1} fps");
        Log($"  等待超时   : {timeouts}（桌面无变化时正常）");
        Log($"  错误       : {errors}");
        Log($"  硬件编码器 : {(encName is null ? "无" : encName)}");
        Log("");
        if (frames > 0 && fps >= 20 && encName is not null)
            Log("  判定：DXGI 采集 + 硬件编码 可用 —— 可以进入下一步");
        else if (frames > 0)
            Log("  判定：采集可用，但帧率或编码器需要处理");
        else
            Log("  判定：一帧都没抓到 —— 需要查 Dispose/回读逻辑");

        Finish();
    }

    private static void Finish()
    {
        Log($"（结束 {DateTime.Now:HH:mm:ss}）");
        Environment.Exit(0);   // 自动退出，便于脚本化验证
    }

    // ---- 环境诊断辅助 ----

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern int GetSystemMetrics(int index);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern IntPtr GetProcessWindowStation();

    private static string GetWindowStationName()
    {
        var h = GetProcessWindowStation();
        if (h == IntPtr.Zero) return "?";
        var buf = new System.Text.StringBuilder(256);
        uint needed = 0;
        return GetUserObjectInformation(h, 2 /*UOI_NAME*/, buf, 256, ref needed)
            ? buf.ToString() : "(取不到)";
    }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool GetUserObjectInformation(
        IntPtr hObj, int index, System.Text.StringBuilder info, int len, ref uint needed);

    private static string GetIntegrityLevel()
    {
        try
        {
            using var id = System.Security.Principal.WindowsIdentity.GetCurrent();
            var p = new System.Security.Principal.WindowsPrincipal(id);
            if (p.IsInRole(System.Security.Principal.WindowsBuiltInRole.Administrator))
                return "管理员";
            return "普通用户";
        }
        catch { return "?"; }
    }
}
