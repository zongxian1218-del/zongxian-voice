// MediaEngine.cs —— WebView2 承载的媒体/通话引擎
//
// ============================================================================
// 为什么媒体放在 WebView2 里
// ============================================================================
// 这是本项目最重要的一条架构决策：
//
// Windows 上能拿到实时音视频只有三条路：
//   1) C++ + libwebrtc            能力最全，但要 VS 专有组件（本机装不上）
//   2) C# 纯托管（SIPSorcery 等）  没有 AEC3、没有 NetEQ，屏幕采集要自己写
//   3) WebView2（Chromium）        AEC3 / NS / AGC / Opus / NetEQ /
//                                 getDisplayMedia / RTCDataChannel 全部现成
//
// 选 3，并且已在应用内实测验证（见 media-engine-architecture 文档）：
//   ✓ 回声消除生效 — true
//   ✓ 降噪生效    — true
//   ✓ 自动增益生效 — true
//
// 关键设计：这个 WebView2 **不可见**，它不是界面，是「媒体服务」。
// 界面全部是原生 WinUI 控件；音频样本一个都不进 C#。
//
// ============================================================================
// 为什么媒体页由本地 HTTP 服务器提供，而不是虚拟主机映射
// ============================================================================
// getUserMedia 要求**安全上下文**。两种可行做法：
//   a) SetVirtualHostNameToFolderMapping → https:// 虚拟主机
//   b) http://127.0.0.1/... → Chromium 把 localhost 视为安全上下文
//
// 选 b，原因是信令要用 WebSocket：
//   https 页面连 ws:// 会被当**混合内容**拦掉，而用 wss:// 就得给 localhost
//   签证书 —— 不值得。用 http 源则页面与 ws:// 同源，完全没有这个问题。
//
// 顺带的收益：页面与信令同源，不用处理跨源；调试时可直接用浏览器打开
// http://127.0.0.1:端口/call.html。
//
// ============================================================================
// 与界面之间怎么通信
// ============================================================================
// 只用一条通道：WebMessage（JSON 字符串）。
//   C# → JS：ExecuteScriptAsync("zxEngine.handle(<json>)")
//   JS → C#：window.chrome.webview.postMessage(json) → WebMessageReceived
// 刻意不做按样本的数据传递 —— 解码播放全在 Chromium 内部完成。
// 这样既没有跨语言开销，也不会在音频线程上踩托管 GC。

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Threading.Tasks;
using Microsoft.UI.Xaml.Controls;
using Microsoft.Web.WebView2.Core;

namespace ZongxianVoice;

/// <summary>媒体引擎上报的事件。</summary>
public sealed class MediaEvent
{
    public string Type { get; init; } = "";
    public string Raw { get; init; } = "";
    public JsonElement? Payload { get; init; }
}

public sealed class MediaEngine : IDisposable
{
    private readonly WebView2 _webView;
    private readonly string _mediaRoot;
    private readonly SignalingServer _server;
    private readonly bool _shareSignaling;
    private readonly string? _externalSignalUrl;
    private bool _ready;
    private bool _initFailed;
    private string _initError = "";
    private bool _engineLoaded;

    public event Action<MediaEvent>? EventRaised;
    /// <summary>信令服务器的日志，转发给界面便于观察。</summary>
    public event Action<string>? Log;

    public bool IsReady => _ready;
    public string InitError => _initError;
    public int Port => _server.Port;

    /// <summary>
    /// 页面里应该连接的信令地址。
    /// 指定了 --signal 时用对端的，否则用本机（自己当房主）。
    /// </summary>
    public string SignalUrl => _externalSignalUrl ?? _server.LocalSignalUrl;

    /// <param name="webView">承载媒体页的 WebView2 控件（不可见）。</param>
    /// <param name="port">本地服务端口（提供媒体页；房主模式下同时提供信令）。</param>
    /// <param name="externalSignalUrl">
    /// 对端的信令地址，形如 ws://192.168.1.5:45890/signal。
    /// 为 null 表示本机自己当房主、自行中继信令。
    /// </param>
    public MediaEngine(WebView2 webView, int port, string? externalSignalUrl = null)
    {
        _webView = webView ?? throw new ArgumentNullException(nameof(webView));
        _mediaRoot = LocateMediaRoot();
        _shareSignaling = string.IsNullOrWhiteSpace(externalSignalUrl);
        _externalSignalUrl = _shareSignaling ? null : externalSignalUrl!.Trim();
        _server = new SignalingServer(port, _mediaRoot, _shareSignaling);
        _server.Log += line => Log?.Invoke(line);
    }

    private static string LocateMediaRoot()
    {
        var candidates = new List<string>
        {
            Path.Combine(AppContext.BaseDirectory, "media"),
            // 开发期：从 bin\x64\Release\net8.0-...\win-x64 往上找源码树
            Path.GetFullPath(Path.Combine(AppContext.BaseDirectory,
                "..", "..", "..", "..", "..", "media")),
        };
        foreach (var c in candidates)
        {
            if (Directory.Exists(c) && File.Exists(Path.Combine(c, "call.html")))
                return c;
        }
        return candidates[0];
    }

    /// <summary>启动信令服务与 WebView2，加载通话页。</summary>
    /// <summary>
    /// 组装 WebView2 的浏览器参数。
    /// 默认关日志（--enable-logging 会让 WebView2 开出一个控制台窗口，用户看到"黑框"）；
    /// 测试时可用 ZX_AUTOSHARE=1 让屏幕共享自动选中整个屏幕（不弹选择窗口）。
    /// </summary>
    private static string BuildBrowserArgs()
    {
        var log = Environment.GetEnvironmentVariable("ZX_WEBVIEW_LOG") == "1"
            ? "--enable-logging"
            : "--disable-logging --log-level=3";
        if (Environment.GetEnvironmentVariable("ZX_AUTOSHARE") == "1")
        {
            // 让 getDisplayMedia 自动选"整个屏幕"（Chromium 的测试开关）
            log += " --auto-select-desktop-capture-source=Entire screen";
        }
        return log;
    }

    public async Task<bool> InitializeAsync()
    {
        if (_ready) return true;
        if (_initFailed) return false;

        try
        {
            _server.Start();

            // WebView2 的 user data folder。
            // 放在 exe 同目录而不是 %LOCALAPPDATA%：曾遇到对后者子目录的
            // UnauthorizedAccessException（手工创建同路径却成功，说明与
            // WebView2 内部创建时序/权限继承有关）。放 exe 旁还更便携。
            var udf = Path.Combine(AppContext.BaseDirectory, ".webview2");
            Directory.CreateDirectory(udf);

            var env = await CoreWebView2Environment.CreateWithOptionsAsync(
                null, udf, new CoreWebView2EnvironmentOptions
                {
                    // 默认**关掉** Chromium 日志：`--enable-logging` 会让 WebView2 把日志
                    // 吐进一个控制台窗口 —— 用户看到的"应用旁边挂着黑框、满屏 ERROR:...
                    // 且标题是 msedgewebview2.exe（未响应）"就是它，
                    // 既不是我们的日志面板、也不是继承来的 cmd（已实测确认）。
                    // 排查 WebView2 问题时用环境变量 ZX_WEBVIEW_LOG=1 临时打开。
                    //
                    // 【测试专用】ZX_AUTOSHARE=1 时加上 --auto-select-desktop-capture-source，
                    // 让"共享屏幕"自动选中整个屏幕、**不弹选择窗口**。
                    // 为什么需要：Chromium 的选择窗口是模态 UI，自动化脚本去查 UIA 会抢焦点
                    // 把它关掉（实测：一查就没了，然后 20 秒超时）。有了这个开关，
                    // "共享整个屏幕 + 系统音频"这条链路才能自动验证（P4）。
                    AdditionalBrowserArguments = BuildBrowserArgs(),
                });

            await _webView.EnsureCoreWebView2Async(env);
            var core = _webView.CoreWebView2;

            core.WebMessageReceived += OnWebMessageReceived;

            // 屏幕共享：把采集请求的来源记进日志（补 P3 缺的"失败原因可读"那一半）
            HookScreenCaptureLogging(core);

            // 媒体页是我们自己的代码，直接放行麦克风；其余一律拒绝。
            // 界面上的"是否允许通话"由我们自己的 UI 负责，不弹 WebView2 的框。
            core.PermissionRequested += (_, e) =>
            {
                switch (e.PermissionKind)
                {
                    case CoreWebView2PermissionKind.Microphone:
                    case CoreWebView2PermissionKind.Camera:
                        e.State = CoreWebView2PermissionState.Allow;
                        break;
                    default:
                        e.State = CoreWebView2PermissionState.Deny;
                        break;
                }
                e.Handled = true;
            };

            // 【屏幕共享必须单独处理，否则会被默认拒绝】
            //
            // getDisplayMedia() 走的**不是** PermissionRequested，而是这个专门的事件
            // CoreWebView2.ScreenCaptureStarting。它的默认行为是取消 ——
            // 表现为调用 getDisplayMedia 后既不报错也不返回，界面上"点共享没反应"。
            // 本会话就踩了这个：所有底层的传输链路都测通了（合成画面 3777 帧），
            // 但真实屏幕采集一直卡住，日志停在"请求屏幕共享权限…"之后再无输出。
            //
            // 合法组合只有两种：
            //   Handled=true,  Cancel=false → 允许，由 WebView2 弹出源选择窗口
            //   Handled=true,  Cancel=true  → 拒绝，getDisplayMedia 收到 NotAllowedError
            // 保持 Handled=false 会继续走默认链，最终仍是拒绝。
            core.ScreenCaptureStarting += (_, e) =>
            {
                e.Handled = true;
                e.Cancel = false;
                Debug.WriteLine("[MediaEngine] 屏幕捕获请求已放行，等待用户选择窗口");
            };

            core.Settings.AreDevToolsEnabled = true;   // 开发期方便排查
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.AreBrowserAcceleratorKeysEnabled = false;

            core.Navigate($"http://127.0.0.1:{_server.Port}/call.html");
            _ready = true;
            return true;
        }
        catch (Exception ex)
        {
            _initFailed = true;
            // 完整异常都带上：WebView2 的失败原因常藏在 InnerException 里
            _initError = $"{ex.GetType().Name}: {ex.Message}" +
                         (ex.InnerException is { } inner
                             ? $" | Inner: {inner.GetType().Name}: {inner.Message}"
                             : "") +
                         $" | HRESULT=0x{ex.HResult:X8}" +
                         $" | 媒体目录={_mediaRoot}";
            Debug.WriteLine($"[MediaEngine] 初始化失败: {_initError}");
            return false;
        }
    }

    private void OnWebMessageReceived(CoreWebView2 sender,
                                      CoreWebView2WebMessageReceivedEventArgs args)
    {
        var raw = args.TryGetWebMessageAsString();
        if (string.IsNullOrEmpty(raw)) return;

        MediaEvent ev;
        try
        {
            using var doc = JsonDocument.Parse(raw);
            var root = doc.RootElement;
            var cloned = root.Clone();   // doc 会被释放，必须克隆
            ev = new MediaEvent
            {
                Type = root.TryGetProperty("type", out var t) ? t.GetString() ?? "" : "",
                Raw = raw,
                Payload = cloned,
            };
        }
        catch (JsonException)
        {
            ev = new MediaEvent { Type = "invalid", Raw = raw };
        }

        if (ev.Type == "engine-loaded") _engineLoaded = true;
        EventRaised?.Invoke(ev);
    }

    /// <summary>等通话页加载完成（它加载后会发 engine-loaded）。</summary>
    public async Task<bool> WaitForEngineAsync(int timeoutMs = 8000)
    {
        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < timeoutMs)
        {
            if (_engineLoaded) return true;
            await Task.Delay(100);
        }
        return false;
    }

    /// <summary>
    /// 调用通话页里的 <c>zxEngine</c> 上的方法。
    ///
    /// 参数序列化成 JSON 再作为字符串字面量传进 JS，由 JS 端 JSON.parse 还原 ——
    /// 这样特殊字符（引号、反斜杠、中文）全部由运行时处理，不用手工转义。
    /// </summary>
    public async Task CallAsync(string method, object? arg = null)
    {
        if (!_ready || _webView.CoreWebView2 is null) return;

        var argJson = arg is null
            ? "null"
            : JsonSerializer.Serialize(arg, JsonOpts);

        var argLiteral = JsonSerializer.Serialize(argJson);

        // 【必须写 window.zxEngine，不能写裸 zxEngine】2026-10-06 实测踩到：
        // 页面从"经典脚本"改成 ES module 之后，`window.zxEngine = {...}` **不再产生裸名绑定** ——
        // 模块作用域里裸 `zxEngine` 直接抛 `ReferenceError: zxEngine is not defined`，
        // 而它被 try/catch 转成 scriptError 上报，表现就是"某个功能悄悄不生效"。
        // 三处生成 JS 的地方（本方法、CallRawStringAsync、Shutdown）统一写全名。
        var script = $"(function(){{try{{" +
                     $"window.zxEngine.{method}(JSON.parse({argLiteral}));" +
                     $"}}catch(e){{" +
                     $"window.chrome.webview.postMessage(JSON.stringify(" +
                     $"{{type:'scriptError',method:'{method}',message:String(e)}}));" +
                     $"}}}})()";

        await _webView.ExecuteScriptAsync(script);
    }

    /// <summary>
    /// 调用 zxEngine 的某个方法，参数是**裸字符串字面量**（不做 JSON.parse）。
    /// 共享电脑声音每 50ms 推一次 PCM（base64 约 26KB），走 CallAsync 那条路
    /// 要先序列化成 JSON 再反转义，白白多一遍拷贝 —— 这里直接生成 JS 字符串字面量。
    /// </summary>
    public async Task CallRawStringAsync(string method, string value)
    {
        if (!_ready || _webView.CoreWebView2 is null) return;
        var literal = JsonSerializer.Serialize(value);
        // 同样必须写 window.zxEngine（模块作用域里裸名解析不到，见 CallAsync 的注释）
        await _webView.ExecuteScriptAsync($"window.zxEngine.{method}({literal})");
    }

    /// <summary>
    /// 屏幕共享：把"谁在请求、有没有被拦下"记清楚。
    ///
    /// 【为什么只记日志、不代选采集源（2026-10-06 核实）】
    /// `CoreWebView2ScreenCaptureStartingEventArgs` 的全部成员只有
    /// `Cancel` / `Handled` / `OriginalSourceFrameInfo` / `GetDeferral`
    /// —— **没有指定采集源的入口**。也就是说 Chromium 的选择窗口是**设计如此**
    /// （由用户决定共享哪个屏幕/窗口，宿主无权代选），与"所有屏幕共享软件都要用户选一次"一致。
    /// 所以这里保持默认处理（不设 Handled），只把请求来源写进日志 ——
    /// 这正好补上 P3 缺的那一半：失败时能说清"是哪个页面发起的、用户有没有选"。
    /// </summary>
    private void HookScreenCaptureLogging(Microsoft.Web.WebView2.Core.CoreWebView2 core)
    {
        try
        {
            core.ScreenCaptureStarting += (_, args) =>
            {
                var src = "未知来源";
                try
                {
                    var frame = args.OriginalSourceFrameInfo;
                    if (frame is not null)
                    {
                        src = string.IsNullOrEmpty(frame.Source) ? frame.Name : frame.Source;
                    }
                }
                catch { /* 取不到就算了，别因为日志把采集拦下来 */ }
                Log?.Invoke($"[屏幕共享] 收到采集请求（来源={src}）→ 交给系统选择窗口（由用户决定共享哪个屏幕/窗口）");
                // 刻意不设置 Handled / Cancel：保持默认行为，让选择窗口正常出现。
            };
        }
        catch (Exception ex)
        {
            Log?.Invoke("[屏幕共享] 挂 ScreenCaptureStarting 失败: " + ex.Message);
        }
    }

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    /// <summary>
    /// 停止媒体引擎。
    ///
    /// WebView2 是 XAML 控件，**不实现 IDisposable** —— 生命周期由视觉树管。
    /// 硬套 Dispose 模式会编译不过（CS1061）。这里只退订事件、停掉媒体。
    /// </summary>
    /// <summary>
    /// 【S4 方案 A】在 WebView 还活着的时候，让页面把模块资源释放掉。
    ///
    /// 【为什么必须单独有这个方法】页面自己的 `pagehide`/`beforeunload` 在 WebView2 关窗时
    /// **不可靠**（实测：走了正常关闭路径也没有释放记录），而 `Shutdown()` 挂在 `Window.Closed` 上——
    /// 那时 WebView2 已经开始销毁，`ExecuteScriptAsync` 根本执行不到。
    /// 所以要在 `AppWindow.Closing`（还能执行脚本）里先调它，再让窗口关。
    /// </summary>
    public async Task DisposePageAsync()
    {
        if (!_ready || _webView.CoreWebView2 is null) return;
        try
        {
            // 直接调 stopAll：它会先让模块宿主 disposeAll（见 call.js 的 stopAll 注释），
            // 再走业务停机。这样"资源回收"发生在 WebView 还活着的时候。
            await _webView.CoreWebView2.ExecuteScriptAsync(
                "try{window.zxEngine&&window.zxEngine.stopAll&&window.zxEngine.stopAll()}catch(e){}");
        }
        catch
        {
            // 关窗路径不许抛：这里失败也不影响关闭，只是资源晚一点随进程回收
        }
    }

    public void Shutdown()
    {
        try
        {
            if (_ready && _webView.CoreWebView2 is { } core)
            {
                core.WebMessageReceived -= OnWebMessageReceived;
                _ = core.ExecuteScriptAsync(
                    "try{window.zxEngine&&window.zxEngine.stopAll&&window.zxEngine.stopAll()}catch(e){}");
            }
            _ready = false;
        }
        catch
        {
            // 关闭路径上不抛：这时窗口可能已经在销毁了
        }
        _server.Dispose();
    }

    public void Dispose() => Shutdown();
}
