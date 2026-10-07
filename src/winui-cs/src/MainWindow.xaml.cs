// MainWindow.xaml.cs —— 主窗口
//
// 职责分两层，刻意分清：
//   · 界面（三栏、主题、窗口行为、通话控件）—— 本文件
//   · 媒体（麦克风、编解码、回声消除、信令）—— 交给 MediaEngine
//
// 两者之间只有一条 JSON 消息通道，界面不直接碰任何音视频 API。
//
// 命令行参数：
//   --port 45890        本地服务端口（提供媒体页；房主模式下同时中继信令）
//   --name 张三          本机显示名
//   --room test         房间名（双方必须相同）
//   --signal ws://host:port/signal
//                       连到别人的信令服务器。省略则自己当房主。
//
// 典型用法（异地组网下的双人通话）：
//   房主：ZongxianVoice.exe --port 45890 --name 我 --room home
//   对方：ZongxianVoice.exe --port 45890 --name 朋友 --room home \
//                            --signal ws://<房主的组网IP>:45890/signal
//   房主把组网 IP 告诉对方即可（用现有的 netgroup 功能查地址）。

using System;
using System.Globalization;
using System.Text;
using System.Text.Json;
using Microsoft.UI;
using Microsoft.UI.Composition.SystemBackdrops;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Windows.Graphics;

namespace ZongxianVoice;

public sealed partial class MainWindow : Window
{
    // 窗口初始尺寸：按 UI 规范 §2 的三栏宽度反推 ——
    // 240(左) + 480(中，最小) + 224(右) = 944，再加余量。
    private const int InitialWidth = 1180;
    private const int InitialHeight = 780;
    private const int MinWidth = 900;
    private const int MinHeight = 600;

    private MediaEngine? _media;
    /// <summary>S4：关窗时"已让页面释放过模块资源"的标志（防止 Closing 重入）。</summary>
    private bool _closingDisposed;
    private NativeVideoHost? _nativeVideo;   // P1-6：原生视频通道（跟着画面区可见性起停）

    // 【S6】正在共享的对端集合 + 最新一位（多人共享时只显示最新一位，见设计稿 D1）
    private readonly HashSet<string> _sharingPeers = new(StringComparer.Ordinal);
    private string? _lastSharer;

    /// <summary>把对端 id 缩成 6 位，日志/界面里好读。</summary>
    private static string Short(string? id) =>
        string.IsNullOrEmpty(id) ? "对方" : id[..Math.Min(6, id.Length)];

    /// <summary>
    /// 原生视频通道的开关：界面开关（可交付形态）或环境变量（脚本化验收用）。
    /// </summary>
    private bool NativeVideoEnabled =>
        NativeVideoToggle.IsChecked == true ||
        Environment.GetEnvironmentVariable(AppConfig.NativeVideoEnv) == "1";

    /// <summary>
    /// 助手的视频端口，默认值取 AppConfig.DefaultNativeVideoPort（唯一来源）。
    /// 【为什么要能改】两个应用实例同时跑时，两个助手都绑同一个端口会冲突（第二个绑定失败直接退出），
    /// 双机联调/并行验收就跑不了。这个覆盖只给脚本化测试用，界面上不暴露。
    /// </summary>
    private static int NativeVideoPort =>
        int.TryParse(Environment.GetEnvironmentVariable(AppConfig.NativeVideoPortEnv), out var p) && p > 0
            ? p : AppConfig.DefaultNativeVideoPort;

    /// <summary>
    /// 起原生视频通道（幂等）。**只在画面区要显示时调用** ——
    /// 启动那一刻画面区还没进视觉树，算不出屏幕矩形，所以它是懒启动的。
    /// </summary>
    private void EnsureNativeVideoStarted()
    {
        if (!NativeVideoEnabled || _nativeVideo is not null) return;
        try
        {
            if (!ScreenView.IsLoaded)
            {
                // 还没布局完就先挂一次 Loaded，避免 TransformToVisual 抛异常（实测踩过：助手没起来）
                ScreenView.Loaded += (_, _) => EnsureNativeVideoStarted();
                return;
            }
            _nativeVideo = new NativeVideoHost(MediaHost, this, AppendEngineLog, port: NativeVideoPort);
            _nativeVideo.StreamSizeChanged += (w, h) => AppendEngineLog($"[原生视频] 收到码流 {w}x{h}");
            _nativeVideo.HelperEvent += line => AppendEngineLog("[原生视频助手] " + line);
            if (_nativeVideo.Start())
                AppendEngineLog("[原生视频] 已启动（诊断: nativevideo-stdout.txt / nativevideo-pos.txt）");
            else
                _nativeVideo = null;
        }
        catch (Exception ex)
        {
            AppendEngineLog("[原生视频] 启动失败: " + ex.Message);
            _nativeVideo = null;
        }
    }

    private void StopNativeVideo()
    {
        if (_nativeVideo is null) return;
        try { _nativeVideo.Stop(); } catch { /* 已经退了就算了 */ }
        _nativeVideo = null;
        AppendEngineLog("[原生视频] 已停止");
    }

    /// <summary>界面开关被点：开 → 若画面区已可见就立刻起；关 → 停掉。</summary>
    private void OnNativeVideoToggled(bool on)
    {
        AppendEngineLog($"[原生视频] 界面开关 → {(on ? "开" : "关")}");
        if (on)
        {
            if (ScreenView.Visibility == Visibility.Visible) EnsureNativeVideoStarted();
            else AppendEngineLog("[原生视频] 画面区当前不可见，等它显示时再启动");
        }
        else
        {
            StopNativeVideo();
        }
        RenderEngineLog();
    }
    private readonly StringBuilder _engineLog = new();

    // 命令行为准，缺省值让单实例也能跑起来（默认值唯一来源：AppConfig）
    private int _signalPort = AppConfig.DefaultSignalPort;
    // 设备下拉是否已由**引擎枚举**填过（引擎的名字/参数更准；页面在未授权时只有占位项）
    private bool _devicesFromEngine;
    // 【S2 拆桥】基础设施模块：UI 只通过它们做事，不再自己起进程/解析 JSON/决定落盘
    private readonly EngineProbe _engineProbe;
    private readonly FileStore _fileStore;
    private readonly SharedAudioService _sharedAudio;
    private readonly ScreenShareService _screenShare = new();
    // 启动时 C#↔页面 契约对账是否通过（S1；不通过时界面会明确提示，详见 BridgeContract）
    private bool _bridgeContractOk = true;
    // S1 契约补齐用：去重键（只在状态/统计真的变化时写日志，避免刷屏）
    private string _lastIceState = "";
    private string _lastRemoteStatsKey = "";
    // 【2026-10-06 审计 §五-4 清债】共享声音状态轮询的取消令牌。
    // 之前 `case "ready"` 里 `_ = Task.Run(async () => { while (true) ... })` 永不退出、无取消，
    // 而且**没在共享时也**每 2 秒调一次 shareAudioDebug ⇒ 用户什么都不做也每 2 秒一条日志
    // （实测：tmp\smoke-ui.log 里 6 条间隔正好 2 秒的 share-audio-debug，把有用日志挤出 120 行窗口）。
    // 现在：① 有关闭时取消 ② 只有开关真的在"开"时才轮询。
    private System.Threading.CancellationTokenSource? _shareAudioPollCts;

    /// <summary>用户配置（TURN 中继等）。启动时从应用目录的配置文件读，设置里改完写回。</summary>
    private readonly AppConfig _config = AppConfig.Load();
    private string _selfName = "我";

    /// <summary>命令行 --name 指定的名字（优先于配置文件；空 = 没指定，用配置里的）。</summary>
    private string _selfNameFromCli = "";

    private string _room = "default";
    private string? _externalSignalUrl;
    /// <summary>--record-test：启动后自动录一段并退出。用于脚本化验证录音链路。</summary>
    private bool _recordTestMode;
    /// <summary>--signal-test：启动后跑合成正弦波的信号链验证并退出。</summary>
    private bool _signalTestMode;
    /// <summary>--chat-test &lt;文本&gt;：连上后自动发一条消息，用于验证 DataChannel 聊天。</summary>
    private string? _chatTestText;

    /// <summary>--chat-test 是否已发出（避免通道重开后重复发送）。</summary>
    private bool _chatTestSent;

    /// <summary>--file-test 是否已发送（避免通道重开后重复发）。</summary>
    private bool _fileTestSent;

    /// <summary>--call-test 是否已发起（避免通道重开后重复拨号）。</summary>
    private bool _callTestStarted;
    /// <summary>
    /// --log-file &lt;路径&gt;：把引擎日志周期性写到文件。
    ///
    /// 存在的理由：日志原本只在界面上，验证时要截图 —— 一张全屏截图几万
    /// token，非常浪费。落到文件后就能用 grep 读，成本几乎为零。
    /// </summary>
    private string? _logFilePath;
    /// <summary>--file-test &lt;路径&gt;：连上后自动把该文件发给对端，用于验证文件传输。</summary>
    private string? _fileTestPath;
    /// <summary>
    /// --screen-test：用**合成画面**跑一遍屏幕共享的全链路。
    ///
    /// 为什么是合成画面而不是真屏幕：getDisplayMedia 必须由用户在系统弹窗里
    /// 选窗口，无法脚本化。合成画面除了"采集源"不同，重协商、视频编码、
    /// 对端收帧、画面渲染全都是同一条代码，所以能自动覆盖绝大部分风险。
    /// </summary>
    private bool _screenTestMode;
    /// <summary>
    /// --share-audio-test：链路就绪后自动打开「共享电脑声音」（A1 取证用）。
    /// 走界面同一路径（与拨开关等价），这样"自动复现"与"用户手点"是同一条代码路径。
    private bool _shareAudioTestMode;
    /// <summary>--parse-invite-test：离线跑邀请串解析用例（不需要 GUI，也不会连网）。</summary>
    private bool _parseInviteTestMode;

    /// <summary>--room-test：离线跑房间列表用例（不需 GUI/网络）。</summary>
    private bool _roomTestMode;

    /// <summary>--rename-room-test：对当前房间改名（走界面 RenameRoomAsync 同一段代码）。</summary>
    private string? _renameRoomTestName;
    /// <summary>--delete-room-test：删除当前房间（走界面 DeleteRoomAsync 同一段代码）。</summary>
    private bool _deleteRoomTestMode;

    /// <summary>--set-name-test &lt;名字&gt;：走设置面板「关闭」同一段代码改名字。</summary>
    private string? _setNameTestValue;

    /// <summary>--user-flow-host &lt;房间名&gt;：按用户真实流程走（改名→删房间→建房→保活）。</summary>
    /// <summary>最后一次"发送文件"卡片的状态行 —— file-done 时把"发送中…"改成"已发送"。</summary>
    private TextBlock? _lastSendFileStatus;

    private string? _userFlowHost;
    /// <summary>--user-flow-join：按用户真实流程走（改名→等扫描→一键加入）。</summary>
    private bool _userFlowJoin;
    /// <summary>--user-flow-join [房间名]：按指定房间名加入（测试确定性，避免被别人的房间干扰）。</summary>
    private string? _userFlowJoinRoom;

    /// <summary>--create-room-test &lt;名字&gt;：走界面同一条 CreateRoomAsync，验证"能创建房间"。</summary>
    private string? _createRoomTestName;

    /// <summary>--create-room-keep：建房验收后**不退**，保持当房主（双实例端到端测试用）。</summary>
    private bool _createRoomKeep;

    /// <summary>房间列表（产品化：多房间 + 切换 + 记住）。持久化在 app-config.json。</summary>
    private RoomStore _rooms = new();

    /// <summary>每个房间的连接状态（只用于界面显示，不参与连接逻辑）。</summary>
    private readonly Dictionary<string, string> _roomState = new();

    /// <summary>待启动的房主房间（引擎/信令地址还没就绪时记下，就绪后自动开房）。</summary>
    private string? _pendingHostRoomId;

    /// <summary>通话中周期性音频统计的计时器（用户要看"语音到底在不在传"）。</summary>
    private Microsoft.UI.Xaml.DispatcherTimer? _callStatsTimer;

    /// --call-test：链路就绪后自动发起语音通话。
    ///
    /// 存在的理由：验证"点发起通话"这条路时，之前用模拟鼠标点击按钮，
    /// 结果点到了旁边的「录音自检」（日志里冒出削波警告才发现）。
    /// 靠像素坐标点按钮太脆弱 —— 改成开关，可靠且可脚本化。
    /// </summary>
    private bool _callTestMode;
    /// <summary>--net-report &lt;路径&gt;：只做网络与防火墙检查，输出诊断并退出。</summary>
    private string? _netReportPath;
    /// <summary>--check-host &lt;地址&gt;：检查能否连到房主（对端用来自检）。</summary>
    private string? _checkHostTarget;
    /// <summary>--screen-probe：只测宿主是否放行屏幕捕获，不做传输。</summary>
    private bool _screenProbeMode;
    /// <summary>
    /// --screen-cycle：合成画面共享 → 等待 → 停止共享。
    /// 用来验证"停止后双方都真的回到未共享状态"这个完整周期。
    /// </summary>
    private bool _screenCycleMode;
    /// <summary>
    /// --share-real [秒数]：用**真实 getDisplayMedia** 发起共享（会弹系统窗口）。
    ///
    /// 合成画面只能验证传输链路，验证不了真实采集 —— 而用户报告的正是
    /// 真实采集失败。这个开关就是为那条路径准备的：流程与点按钮完全一致
    /// （先放大媒体宿主，再请求采集），只是不需要人去点按钮。
    /// </summary>
    private int _shareRealSeconds;
    private int _nativeVideoTestSeconds;   // P1-6 验收：显示画面区 N 秒后隐藏
    private bool _probeAppAudioTest;       // 验收：探测「单个应用音频」可用性
    /// <summary>
    /// --regress：复现用户报告的症状序列 ——
    /// 共享 → 停止 → **再次共享** → 通话。
    ///
    /// 用户反馈"第一次共享结束后，就不能再共享、也不能通话了"。
    /// 之前的测试只跑了单次"共享→停止"，所以一直是绿的。
    /// 这个开关专门测**第二次**共享与后续通话，正是回归发生的位置。
    /// </summary>
    private bool _regressMode;
    /// <summary>
    /// --selftest-host / --selftest-join：两台机器各跑一套完整的自动化测试。
    ///
    /// 【为什么需要它】
    /// 用户有两台真机，但反复手动测试成本高、且很难复现同一序列。
    /// 这两个开关让两台机器**无需人工点击**（只有屏幕选择弹窗需要点一次）
    /// 自动跑完：聊天 → 通话 → 共享 → 停止 → 再共享 → 传文件，
    /// 每一步都记录"做了什么、观察到什么、判定结果"，各自落盘。
    /// 用户只需要把两边的 zx-log.txt 发回来。
    ///
    /// 分工：host 负责发起通话与共享；join 负责回应并回发一条消息，
    /// 这样两个方向都被覆盖到。
    /// </summary>
    private string? _selfTestRole;
    /// <summary>
    /// 自测是否使用**真实屏幕采集**（而不是合成画面）。
    ///
    /// 合成画面只能验证传输链路；用户遇到的失败都在真实 getDisplayMedia 上。
    /// 但"选择共享源"弹窗在 WebView2 合成器内部渲染，**没有独立顶层窗口**，
    /// 外部无法自动化（已实测枚举确认）。所以这是唯一需要人点一次的步骤：
    /// 用 --selftest-real 时，两台各点一次弹窗，其余步骤全自动。
    /// </summary>
    private bool _selfTestReal;

    private bool _inCall;
    private bool _muted;
    private int _peerCount;
    /// <summary>界面初始化是否完成。用来避免 XAML 应用默认值时
    /// 误触发 SelectionChanged 之类的回调（那会在引擎还没起来时
    /// 就去调媒体接口，属于自找的空引用）。</summary>
    private bool _uiReady;

    public MainWindow()
    {
        ParseCommandLine();

        // 【S2 拆桥】基础设施模块先建好：日志回调统一走 AppendEngineLog（UI 线程安全，
        // 它自己会用 DispatcherQueue 编组），这样模块内部不需要知道窗口/控件的存在。
        _engineProbe = new EngineProbe(AppendEngineLog);
        _fileStore = new FileStore(AppendEngineLog);
        _sharedAudio = new SharedAudioService(AppendEngineLog);
        _staticLogSink = AppendEngineLog;    // 静态方法（窗口归位等）也要能记日志

        InitializeComponent();

        Title = _externalSignalUrl is null
            ? $"同频 — {_selfName}（房主 · 端口 {_signalPort}）"
            : $"同频 — {_selfName}（连接 {_externalSignalUrl}）";

        ApplyTheme();
        ConfigureWindow();

        // P1-6：原生视频通道**不在启动时开**，而是跟着"画面区可见"这条真实流程起停
        // （见 EnsureNativeVideoStarted / SetScreenViewVisible）。开关仍是环境变量，
        // 等接了真实共享流程再把它变成界面上的设置项。

        // 【2026-10-06 删除假控件】原来这里用 RootGrid.FindName 把"远端控制"红色横幅设成隐藏
        // —— 而 XAML 里它本来就是隐藏的，且那个「停止控制」按钮全仓库没有任何 Click 与代码引用
        //（"看着有、点了没用"）。远端控制是**计划功能**，等它真的实现时再一起补
        // "有事件来源的界面入口"，不在这里留空壳。

        // 初始状态：通话控件不可用，等引擎就绪再放开
        CallButton.IsEnabled = false;
        CallButton.Click += async (_, _) =>
        {
            // 合并键：通话中 ⇒ 挂断；否则 ⇒ 加入语音
            if (_inCall) await OnHangupClickedAsync();
            else await OnCallClickedAsync();
        };
        MuteButton.Click += async (_, _) => await OnMuteClickedAsync();
        HangupButton.Click += async (_, _) => await OnHangupClickedAsync();
        RunSelfCheckButton.Click += async (_, _) => await RunSelfCheckAsync();
        RecordButton.Click += async (_, _) => await OnRecordClickedAsync();
        NativeVideoToggle.Checked += (_, _) => OnNativeVideoToggled(true);
        NativeVideoToggle.Unchecked += (_, _) => OnNativeVideoToggled(false);
        DenoiseCombo.SelectionChanged += async (_, _) => await OnDenoiseChangedAsync();
        SendMessageButton.Click += async (_, _) => await SendChatAsync();
        ShareScreenButton.Click += async (_, _) => await OnShareScreenClickedAsync();
        StopShareButton.Click += async (_, _) => await OnStopShareClickedAsync();
        // 共享画面浮层上的停止按钮：自己共享时停采集，看对方时只退出查看
        ScreenViewStopButton.Click += async (_, _) => await OnScreenViewStopClickedAsync();
        AttachFileButton.Click += async (_, _) => await OnAttachFileClickedAsync();
        // 藏掉"继承来的控制台窗口"：
        //   用户反馈"后台启动时候的 cmd 还在" —— 应用本身是 WinExe 不会建控制台，
        //   但从脚本/命令行里拉起时会**继承**那个控制台，看起来就像应用带了个黑框。
        //   A 线的 entry.py 也是这么处理的，行为保持一致。
        HideInheritedConsole();

        // 联机与诊断入口：以前这些只能靠命令行，界面上没入口（用户表示"没法自己测"）
        JoinButton.Click += async (_, _) => await OnJoinClickedAsync();
        JoinRoomButton.Click += async (_, _) => await OnJoinSelectedRoomAsync();
        // 【③ 修复】一键加入扫描到的房间：用户不必先在下拉框里"选中"
        JoinScannedButton.Click += async (_, _) => await OnJoinScannedAsync();
                // 【A4】复制的这一串带房间名：对方粘贴即进同一房间（旧写法 ip:端口 也仍然认）
        CopyAddressButton.Click += (_, _) => CopyToClipboard(MyInvite(), "邀请串（房间名@地址）");
        AudioCapButton.Click += async (_, _) => await OnAudioCapabilityClickedAsync();
        LogFolderButton.Click += (_, _) => OpenFolder(AppContext.BaseDirectory, "日志");
        DiagCopyButton.Click += (_, _) => CopyDiagnostics();
        // 设置面板：设备选择、共享声音、实验开关、诊断入口都收在里面（主界面只留日常操作）
        SettingsButton.Click += async (_, _) => await OnOpenSettingsAsync();
        SettingsCloseButton.Click += (_, _) => OnCloseSettings();
        TestSpeakerButton.Click += async (_, _) =>
        {
            // 【诊断】能听到提示音 ⇒ 扬声器/音量没问题（"听不到对方"要往别处查）；
            // 听不到 ⇒ 问题就在扬声器选择或音量这一环。
            if (_media is null || !_media.IsReady) return;
            AppendEngineLog("[扬声器测试] 播放 1 秒 440Hz 提示音到当前选定的扬声器…");
            RenderEngineLog();
            await _media.CallAsync("testSpeaker");
        };
        // 第一屏（起名字 + 选房间/填地址）：新用户进来先看到它
        WelcomeEnterButton.Click += (_, _) => EnterMainUi();
        // 【产品化】创建房间（本机当房主）—— 原来没有这个入口，"加入"必须填别人的地址
        CreateRoomButton.Click += async (_, _) =>
        {
            // 【取证】先记"处理器被调用"，否则"点了没反应"时分不清
            // 是"点击没到"还是"逻辑早退"（本轮就卡在这个区分上）。
            AppendEngineLog($"[建房] 点击处理器被调用（输入框=「{(CreateRoomNameBox.Text ?? "<空>")}」）");
            await CreateRoomAsync();
        };
        CreateRoomNameBox.KeyDown += async (_, e) =>
        {
            if (e.Key == Windows.System.VirtualKey.Enter) await CreateRoomAsync();
        };
        // 【产品化·去重】"加房间"只保留**一个**入口（左栏按钮）。
        // 原来有两个「联机 / 换房间」按钮 + 中栏图标按钮，全都调同一个 ShowWelcome()：
        //   · SidebarConnectButton2 —— 已从 XAML 删除（重复入口）
        //   · OpenRoomPanelButton   —— 保留（中栏顶栏），与左栏按钮**同一条路径**，不再算重复功能
        // 【产品化】房间名：回车或失焦**立即生效**（现状是"改完要重启"，用户明确抱怨过）
        RoomNameBox.KeyDown += async (_, e) =>
        {
            if (e.Key == Windows.System.VirtualKey.Enter) await ApplyRoomNameAsync();
        };
        RoomNameBox.LostFocus += async (_, _) => await ApplyRoomNameAsync();
        SidebarConnectButton.Click += (_, _) => ShowWelcome();
        OpenRoomPanelButton.Click += (_, _) =>
        {
            // 【取证】"点中栏联机按钮没反应"必须能区分"点击没到"与"ShowWelcome 失败"
            AppendEngineLog("[联机入口] 中栏按钮 Click 触发");
            RenderEngineLog();
            ShowWelcome();
        };
        // 【共享电脑声音】独立开关 + 来源列表（列表来自引擎的音频会话枚举）
        ShareAudioToggle.Toggled += async (_, _) => await OnShareAudioToggledAsync();
        RefreshShareAppsButton.Click += async (_, _) => await RefreshShareAudioAppsAsync();
        MicDeviceCombo.SelectionChanged += async (_, _) => await OnDevicePickedAsync(audioInput: true);
        SpkDeviceCombo.SelectionChanged += async (_, _) => await OnDevicePickedAsync(audioInput: false);
        // 关窗时收掉发现用的 UDP 端口（不然第二个实例可能绑不上）
        Closed += (_, _) => { try { _lanTick?.Dispose(); } catch { } try { _lan?.Dispose(); } catch { } _lan = null; };
        // 关窗时停掉共享声音状态轮询（审计 §五-4：原来那条 while(true) 没有取消令牌，关窗后仍在跑）
        Closed += (_, _) => { try { _shareAudioPollCts?.Cancel(); _shareAudioPollCts?.Dispose(); } catch { } _shareAudioPollCts = null; };
        // Enter 发送 / Shift+Enter 换行 —— 聊天软件的标准约定
        MessageInput.KeyDown += async (_, e) =>
        {
            if (e.Key == Windows.System.VirtualKey.Enter
                && !e.KeyStatus.IsMenuKeyDown)
            {
                e.Handled = true;
                await SendChatAsync();
            }
        };

        _uiReady = true;
        // 【产品化离线单测】房间列表：增/改/删/切换 + **持久化往返**（"重启后还在"的直接证据）
        if (_roomTestMode)
        {
            // 【诊断通道】窗口标题是同步生效的，用它确认"块到底进来了没有"
            try
            {
            // 【路径选择】不要用系统 TEMP：应用容器会拒绝写它（实测 UnauthorizedAccessException）。
            // 应用自己的目录是可写的（app-config.json 就写在这里）。
            var tmpCfg = System.IO.Path.Combine(AppContext.BaseDirectory,
                            "room-test-" + Guid.NewGuid().ToString("N")[..6] + ".json");
            var store = new RoomStore();
            var pass = 0;
            var total = 0;
            void Check(string name, bool ok, string detail)
            {
                total++;
                if (ok) pass++;
                AppendEngineLog($"[房间用例] {(ok ? "PASS" : "FAIL")} {name} —— {detail}");
            }

            var a = store.Add("我的房间", "", "default");
            Check("加第一个房间即成为当前", store.Current?.Id == a.Id, $"current={store.Current?.Display}");

            var b = store.Add("朋友家", "192.168.1.5:45891", "weekend");
            Check("加第二个房间并切过去", store.Current?.Id == b.Id, $"current={store.Current?.Display}");

            var dup = store.Add("重复项", "192.168.1.5:45891", "weekend");
            Check("同地址+同房间名不重复添加（只切换）",
                  store.Rooms.Count == 2 && dup.Id == b.Id, $"count={store.Rooms.Count}");

            Check("改名生效", store.Rename(b.Id, "老王家") && store.Current?.Display == "老王家",
                  $"display={store.Current?.Display}");

            var hostLine = store.Rooms.First(r => r.IsHost);
            Check("房主项副标题标为『本机（房主）』", hostLine.Subtitle.Contains("本机"), hostLine.Subtitle);

            Check("删除当前项后自动切到剩下的", store.Remove(b.Id) && store.Rooms.Count == 1
                  && store.Current?.Id == a.Id, $"current={store.Current?.Display}");

            // 持久化往返：这是"重启后房间还在"的直接证据
            var cfg = new AppConfig { RoomList = store };
            var saved = cfg.Save(tmpCfg);
            var fileExists = System.IO.File.Exists(tmpCfg);
            var fileBytes = fileExists ? new System.IO.FileInfo(tmpCfg).Length : 0;
            var back = AppConfig.Load(tmpCfg);
            Check("保存成功", saved, saved ? tmpCfg : $"失败原因={cfg.LastSaveError}");
            Check("文件已落盘", fileExists && fileBytes > 2, $"exists={fileExists} bytes={fileBytes}");
            // 编码：必须是带 BOM 的 UTF-8，否则外部工具看到的中文是乱码
            var head = fileExists ? System.IO.File.ReadAllBytes(tmpCfg).Take(3).ToArray() : Array.Empty<byte>();
            var hasBom = head.Length == 3 && head[0] == 0xEF && head[1] == 0xBB && head[2] == 0xBF;
            Check("配置是带 BOM 的 UTF-8（外部工具不乱码）", hasBom,
                  hasBom ? "EF BB BF" : $"前 3 字节={string.Join(" ", head.Select(x => x.ToString("X2")))}");
            // 【名字持久化】用户报"名字修改完全失效" —— 名字必须能写盘并读回
            cfg.SelfName = "持久化名字甲";
            var saved2 = cfg.Save(tmpCfg);
            var back2 = AppConfig.Load(tmpCfg);
            Check("名字存盘并读回一致（重启后名字还在）",
                  saved2 && back2.SelfName == "持久化名字甲",
                  saved2 ? $"读回={back2.SelfName}" : $"写盘失败={cfg.LastSaveError}");
            Check("读回的列表条数 = 写出的条数",
                  back.RoomList.Rooms.Count == store.Rooms.Count,
                  $"写={store.Rooms.Count} 读={back.RoomList.Rooms.Count}");
            Check("往返后当前项一致", back.RoomList.CurrentId == store.CurrentId,
                  $"写={store.CurrentId} 读={back.RoomList.CurrentId}");
            // 不直接索引 [0]：列表为空时索引会抛异常，反而掩盖"读回是空"这个真因
            var first = back.RoomList.Rooms.FirstOrDefault();
            Check("往返后名字与地址一致",
                  first is not null && first.Display == store.Rooms[0].Display
                  && first.SignalAddress == store.Rooms[0].SignalAddress,
                  first is null ? "读回为空" : $"{first.Display} / {first.SignalAddress}");

            try { System.IO.File.Delete(tmpCfg); } catch { }
            AppendEngineLog($"[房间用例] 结果 {pass}/{total}");
            RenderEngineLog();
            DispatcherQueue.TryEnqueue(() => { try { Application.Current.Exit(); } catch { } });
            return;
            }
            catch (Exception ex)
            {
                // 单测自己抛异常也要说清（含前几帧堆栈）—— 静默的话就只能看到"没输出"
                var frames = (ex.StackTrace ?? "").Split('\n')
                    .Select(s => s.Trim()).Where(s => s.StartsWith("at ")).Take(3);
                AppendEngineLog($"[房间用例] 抛出异常：{ex.GetType().Name}: {ex.Message}" +
                                $" ← {string.Join(" | ", frames)}");
                return;
            }
        }

        // 【A4 离线单测】邀请串解析：不起媒体、不连网，只把用例结果写进日志后退出。
        if (_parseInviteTestMode)
        {
            var cases = new (string raw, string wantAddr, string? wantRoom)[]
            {
                ("我的房间@192.168.1.5:45891", "192.168.1.5:45891", "我的房间"),
                ("zongxian://room/我的房间@192.168.1.5:45891", "192.168.1.5:45891", "我的房间"),
                ("192.168.1.5:45891", "192.168.1.5:45891", null),
                ("ws://192.168.1.5:45891/signal", "ws://192.168.1.5:45891/signal", null),
                (" 房间 A @10.0.0.2:5000 ", "10.0.0.2:5000", "房间 A"),
                ("name@host:1234@1.2.3.4:5678", "1.2.3.4:5678", "name@host:1234"),
            };
            var pass = 0;
            foreach (var (raw, wantAddr, wantRoom) in cases)
            {
                var (addr, room) = ParseInvite(raw);
                var ok = addr == wantAddr && room == wantRoom;
                if (ok) pass++;
                AppendEngineLog($"[邀请解析] {(ok ? "PASS" : "FAIL")} 输入={raw} → 地址={addr} 房间={room ?? "(保持)"}");
            }
            AppendEngineLog($"[邀请解析] 结果 {pass}/{cases.Length}");
            RenderEngineLog();
            DispatcherQueue.TryEnqueue(() => { try { Application.Current.Exit(); } catch { } });
            return;
        }

        // 【产品化】房间列表：从配置读入；首次启动建一个"我的房间"（本机当房主）
        _rooms = _config.RoomList;
        // 【2026-10-07】名字也要从配置恢复：以前名字不落盘，重启就丢（用户报"名字修改完全失效"）。
        // 命令行 --name 优先（测试/脚本用），其次配置文件里的 SelfName。
        if (_selfNameFromCli.Length == 0 && _config.SelfName.Trim().Length > 0)
        {
            _selfName = _config.SelfName.Trim();
        }
        SelfNameBox.Text = _selfName;
        var curRoom = _rooms.EnsureDefault(_room);
        _room = curRoom.Room;
        RenderRooms();

        RootGrid.Loaded += async (_, _) =>
        {
            await InitializeMediaAsync();

            // ================= 用户真实流程 =================
            if (_userFlowHost is { Length: > 0 } hostRoom)
            {
                AppendEngineLog("[流程] ① 给自己改名");
                SelfNameBox.Text = "房主改名";
                OnCloseSettings();                       // 界面同一段代码
                await System.Threading.Tasks.Task.Delay(2500);

                AppendEngineLog("[流程] ② 删掉当前房间（我的房间）");
                if (_rooms.Current is { } cur0)
                {
                    await DeleteRoomAsync(cur0.Id);      // 界面同一段代码
                }
                await System.Threading.Tasks.Task.Delay(1500);

                AppendEngineLog($"[流程] ③ 新建房间「{hostRoom}」");
                CreateRoomNameBox.Text = hostRoom;
                await CreateRoomAsync();                 // 界面同一段代码
                await System.Threading.Tasks.Task.Delay(2500);

                var hostOk = _rooms.Rooms.Any(r => r.Display == hostRoom) && _room == hostRoom;
                AppendEngineLog(hostOk
                    ? $"[流程] 结果 PASS（房主：名字={_selfName} 房间={_room}）"
                    : $"[流程] 结果 FAIL（名字={_selfName} 房间={_room} 列表={_rooms.Rooms.Count}）");
                RenderEngineLog();
                await RunChatTestAsync();
                return;                                  // 保活，等对方加入
            }
            if (_userFlowJoin)
            {
                AppendEngineLog("[流程] ① 给自己改名");
                SelfNameBox.Text = "加入方改名";
                OnCloseSettings();
                await System.Threading.Tasks.Task.Delay(2500);

                AppendEngineLog("[流程] ② 等局域网扫描到房间");
                for (var i = 0; i < 60 && _allRooms.Count == 0; i++)
                {
                    await System.Threading.Tasks.Task.Delay(500);
                }
                AppendEngineLog($"[流程] 扫描到 {_allRooms.Count} 个房间");
                // 【确定性】真机上可能还有别人的房间（实测用户自己的实例也在广播），
                // "一键加入"取第一个是正确语义，但测试要按**指定房间名**筛，
                // 否则会误判成"加入失败"。走的是同一个 OnJoinSelectedRoomAsync。
                var pick = _userFlowJoinRoom is { Length: > 0 } want
                    ? _allRooms.FirstOrDefault(r => r.Room == want)
                    : _allRooms.FirstOrDefault();
                if (pick is not null)
                {
                    AppendEngineLog($"[流程] ③ 加入：{pick.Display}");
                    await OnJoinSelectedRoomAsync(pick);  // 界面同一段代码
                    await System.Threading.Tasks.Task.Delay(3000);
                }
                var joinOk = _rooms.Rooms.Count > 0 && _rooms.Current is not null
                             && _peerCount > 0;
                AppendEngineLog(joinOk
                    ? $"[流程] 结果 PASS（加入方：名字={_selfName} 房间={_room} 成员={_peerCount + 1}）"
                    : $"[流程] 结果 FAIL（名字={_selfName} 房间={_room} 成员={_peerCount + 1} 扫描={_allRooms.Count}）");
                RenderEngineLog();
                await RunChatTestAsync();
                await System.Threading.Tasks.Task.Delay(2000);
                if (!_createRoomKeep) DispatcherQueue.TryEnqueue(() => { try { Application.Current.Exit(); } catch { } });
                return;
            }


            // 【名字验收】走设置面板「关闭」同一段代码（OnCloseSettings）
            if (_setNameTestValue is { Length: > 0 } sn)
            {
                AppendEngineLog($"[名字验收] 把「我的名字」改为「{sn}」并关闭设置");
                SelfNameBox.Text = sn;          // 等价于用户在设置里输入
                OnCloseSettings();              // 等价于用户点「关闭」
                await System.Threading.Tasks.Task.Delay(2500);   // 等重连把新名字广播出去
                var inCfg = AppConfig.Load().SelfName;
                var ok = _selfName == sn && inCfg == sn;
                AppendEngineLog(ok
                    ? $"[名字验收] 结果 PASS（内存={_selfName} 配置={inCfg}，已重连广播）"
                    : $"[名字验收] 结果 FAIL（内存={_selfName} 配置={inCfg}，期望={sn}）");
                RenderEngineLog();
                await System.Threading.Tasks.Task.Delay(1000);
                if (!_createRoomKeep) DispatcherQueue.TryEnqueue(() => { try { Application.Current.Exit(); } catch { } });
                return;
            }


            // 【改名/删除验收】走界面按钮同一段代码（RenameRoomAsync / DeleteRoomAsync）
            if (_renameRoomTestName is { Length: > 0 } rn)
            {
                AppendEngineLog($"[改名验收] 把当前房间改名为「{rn}」");
                var cur = _rooms.Current;
                if (cur is null)
                {
                    AppendEngineLog("[改名验收] 结果 FAIL：没有当前房间");
                }
                else
                {
                    await RenameRoomAsync(cur.Id, rn);
                    var changed = cur.Room == rn && _room == rn;
                    AppendEngineLog(changed
                        ? $"[改名验收] 结果 PASS（房间名已变={cur.Room}，重连已触发）"
                        : $"[改名验收] 结果 FAIL（room={cur.Room}，_room={_room}）");
                }
                RenderEngineLog();
                await System.Threading.Tasks.Task.Delay(1200);
                DispatcherQueue.TryEnqueue(() => { try { Application.Current.Exit(); } catch { } });
                return;
            }
            if (_deleteRoomTestMode)
            {
                var cur = _rooms.Current;
                AppendEngineLog($"[删除验收] 删除当前房间「{cur?.Display ?? "(无)"}」");
                if (cur is null)
                {
                    AppendEngineLog("[删除验收] 结果 FAIL：没有当前房间");
                }
                else
                {
                    await DeleteRoomAsync(cur.Id);
                    var gone = _rooms.Rooms.All(x => x.Id != cur.Id);
                    var membersCleared = _peerCount == 0;
                    AppendEngineLog(gone && membersCleared
                        ? "[删除验收] 结果 PASS（房间已删，成员已清空）"
                        : $"[删除验收] 结果 FAIL（房间已删={gone}，成员数={_peerCount}）");
                }
                RenderEngineLog();
                await System.Threading.Tasks.Task.Delay(1200);
                DispatcherQueue.TryEnqueue(() => { try { Application.Current.Exit(); } catch { } });
                return;
            }

            // 【建房验收】初始化完成后跑；走的是界面同一条 CreateRoomAsync
            if (_createRoomTestName is { Length: > 0 } crName) await RunCreateRoomTestAsync(crName);
        };
        Closed += (_, _) => _media?.Shutdown();

        // 【S4 方案 A】关窗前先让页面释放模块资源（WebView 还活着，脚本才执行得到）。
        // 为什么不用页面的 pagehide：WebView2 关窗时它不可靠（实测没有释放记录）。
        // 这里 await 到释放完成再放行关闭 —— 保证"关引擎 = 定时器/采集/链路都真的收干净"。
        AppWindow.Closing += async (_, args) =>
        {
            if (_closingDisposed || _media is null) return;
            _closingDisposed = true;
            args.Cancel = true;                 // 先拦住，等释放完成再关
            try { await _media.DisposePageAsync(); } catch { }
            try { Close(); } catch { }
        };
    }

    private void ParseCommandLine()
    {
        var args = Environment.GetCommandLineArgs();
        for (int i = 1; i < args.Length - 1; i++)
        {
            switch (args[i])
            {
                case "--port":
                    if (int.TryParse(args[i + 1], NumberStyles.Integer,
                                     CultureInfo.InvariantCulture, out var p) && p > 1024)
                        _signalPort = p;
                    break;
                case "--name":
                    _selfName = args[i + 1];
                    _selfNameFromCli = _selfName;   // 命令行优先于配置文件里的 SelfName
                    break;
                case "--room":
                    _room = args[i + 1];
                    break;
                case "--signal":
                    _externalSignalUrl = args[i + 1];
                    break;
                case "--record-test":
                    _recordTestMode = true;
                    break;
                case "--signal-test":
                    _signalTestMode = true;
                    break;
                case "--chat-test":
                    _chatTestText = args[i + 1];
                    break;
                case "--log-file":
                    _logFilePath = args[i + 1];
                    break;
                case "--file-test":
                    _fileTestPath = args[i + 1];
                    break;
                case "--screen-test":
                    _screenTestMode = true;
                    break;
                case "--share-audio-test":
                    _shareAudioTestMode = true;
                    break;
                case "--parse-invite-test":
                    _parseInviteTestMode = true;
                    break;
                case "--room-test":
                    _roomTestMode = true;
                    break;
                case "--create-room-test":
                    _createRoomTestName = args[i + 1];
                    break;
                case "--rename-room-test":
                    _renameRoomTestName = args[i + 1];
                    break;
                case "--delete-room-test":
                    _deleteRoomTestMode = true;
                    break;
                case "--set-name-test":
                    _setNameTestValue = args[i + 1];
                    break;
                case "--user-flow-host":
                    _userFlowHost = args[i + 1];
                    break;
                case "--user-flow-join":
                    _userFlowJoin = true;
                    if (i + 1 < args.Length && !args[i + 1].StartsWith("--"))
                    {
                        _userFlowJoinRoom = args[i + 1];
                    }
                    break;
                case "--create-room-keep":
                    _createRoomKeep = true;
                    break;
                case "--call-test":
                    _callTestMode = true;
                    break;
                case "--net-report":
                    _netReportPath = args[i + 1];
                    break;
                case "--check-host":
                    _checkHostTarget = args[i + 1];
                    break;
                case "--screen-probe":
                    _screenProbeMode = true;
                    break;
                case "--screen-cycle":
                    _screenCycleMode = true;
                    break;
                case "--share-real":
                    _shareRealSeconds = 25;
                    if (i + 1 < args.Length && int.TryParse(args[i + 1], out var secs) && secs > 0)
                        _shareRealSeconds = secs;
                    break;
                case "--native-video-test":
                    // P1-6 验收用：显示画面区 N 秒（默认 20），走真实的 SetScreenViewVisible 收口点
                    _nativeVideoTestSeconds = 20;
                    if (i + 1 < args.Length && int.TryParse(args[i + 1], out var nvSecs) && nvSecs > 0)
                        _nativeVideoTestSeconds = nvSecs;
                    break;
                case "--probe-app-audio":
                    // 探测「单个应用音频」能不能用（在子进程里探，见 AppAudioProbe 的注释）
                    _probeAppAudioTest = true;
                    break;
                case "--regress":
                    _regressMode = true;
                    break;
                case "--selftest-host":
                    _selfTestRole = "host";
                    break;
                case "--selftest-join":
                    _selfTestRole = "join";
                    break;
                case "--selftest-real":
                    _selfTestReal = true;
                    break;
            }
        }
    }

    // =======================================================================
    // 引擎初始化
    // =======================================================================

    private async System.Threading.Tasks.Task InitializeMediaAsync()
    {
        // 纯诊断模式：只查网络，不启动媒体引擎。
        if (_netReportPath is { Length: > 0 })
        {
            try
            {
                System.IO.File.WriteAllText(_netReportPath, BuildNetReport());
                AppendEngineLog($"网络报告已写入 {_netReportPath}");
            }
            catch (Exception ex)
            {
                AppendEngineLog($"网络报告写入失败: {ex.Message}");
            }
            RenderEngineLog();
            await System.Threading.Tasks.Task.Delay(300);
            try { Application.Current.Exit(); } catch { }
            return;
        }

        if (_checkHostTarget is { Length: > 0 })
        {
            var report = await CheckHostAsync(_checkHostTarget);
            AppendEngineLog("=== 房主连接自检 ===");
            foreach (var line in report.Split('\n')) AppendEngineLog(line.TrimEnd());
            RenderEngineLog();
            try
            {
                System.IO.File.WriteAllText(
                    System.IO.Path.Combine(AppContext.BaseDirectory, "check-host.txt"), report);
            }
            catch { }
            await System.Threading.Tasks.Task.Delay(1500);
            try { Application.Current.Exit(); } catch { }
            return;
        }

        // 【加入方预检】连房主之前先试一次裸 TCP。
        //
        // 为什么要这一步：原来失败时只有一句 "websocket error"，无法区分
        // "网络不通/被防火墙拦" 与 "应用协议出错"。而这两种原因的排查方向
        // 完全不同。先做 TCP 预检，就能给出明确的结论和该跑哪个脚本。
        if (_externalSignalUrl is { Length: > 0 })
        {
            var target = TryParseHostPort(_externalSignalUrl);
            if (target is { } tp)
            {
                AppendEngineLog($"预检：正在连接房主 {tp.host}:{tp.port} …");
                RenderEngineLog();
                var (reachable, detail) = await ProbeTcpAsync(tp.host, tp.port,
                    AppConfig.JoinPreflightTimeoutMs);
                if (reachable)
                {
                    AppendEngineLog($"✓ 房主可达（{detail}）");
                }
                else
                {
                    AppendEngineLog($"✗ 连不上房主：{detail}");
                    AppendEngineLog("");
                    AppendEngineLog("可能的原因与对策：");
                    AppendEngineLog("  1) 房主的防火墙拦了 —— 让房主在【那台机器】上");
                    AppendEngineLog("     右键以管理员运行 4-allow-firewall.cmd");
                    AppendEngineLog($"  2) 房主没启动，或端口不是 {_signalPort}");
                    AppendEngineLog("  3) 组网工具没连通 —— 让房主 ping 你的组网 IP");
                    AppendEngineLog("     或你在本机跑 6-check-host.cmd 看断在哪一环");
                    AppendEngineLog("");
                    AppendEngineLog("诊断信息已写入 logs\\join-preflight.txt");
                    try
                    {
                        // 【不再写 exe 同目录】实测问题：写进安装目录有两个坏处 ——
                        //   ① 打包/守卫把这种运行期产物当"残留"（本轮真的拦下了 v60）；
                        //   ② 真正装到 Program Files 时那个目录**只读**，写入会失败（诊断信息反而拿不到）。
                        // 统一放 logs\ 子目录（与日志同处），并在需要时自己创建。
                        var diagDir = System.IO.Path.Combine(AppContext.BaseDirectory, "logs");
                        System.IO.Directory.CreateDirectory(diagDir);
                        System.IO.File.WriteAllText(
                            System.IO.Path.Combine(diagDir, "join-preflight.txt"),
                            BuildNetReport() + $"\n\n目标 {tp.host}:{tp.port}\n结果: {detail}\n");
                    }
                    catch { }
                }
                RenderEngineLog();
            }
        }

        _media = new MediaEngine(MediaHost, _signalPort, _externalSignalUrl);
        _media.EventRaised += OnMediaEvent;
        _media.Log += OnEngineLog;

        EngineStatus.Text = _externalSignalUrl is null
            ? $"正在启动（房主模式，端口 {_signalPort}）…"
            : $"正在启动（连接 {_externalSignalUrl}）…";

        var ok = await _media.InitializeAsync();
        if (!ok)
        {
            EngineStatus.Text = "媒体引擎初始化失败";
            RenderEngineLog();
            AppendEngineLog($"[初始化失败] {_media.InitError}");
            AppendEngineLog("可能的原因：");
            AppendEngineLog("  · WebView2 Runtime 未安装（本机应已有）");
            AppendEngineLog("  · 信令端口被占用，换一个 --port");
            AppendEngineLog("  · media 目录或 call.html 未随产物部署");
            RenderEngineLog();
            return;
        }

        EngineStatus.Text = "等待通话页加载…";
        if (!await _media.WaitForEngineAsync())
        {
            EngineStatus.Text = "通话页加载超时";
            AppendEngineLog("[超时] 通话页未上报 engine-loaded");
            RenderEngineLog();
            return;
        }

        // 下发启动参数，让页面连上信令
        await _media.CallAsync("start", new
        {
            selfName = _selfName,
            room = _room,
            signalUrl = _media.SignalUrl,
            noiseSuppression = true,
            echoCancellation = true,
            autoGainControl = true,
        });

        // 【跨网段 ICE】把用户在设置里填的 TURN 配置下发给页面（唯一通道 zxEngine.setIceConfig）。
        // 不阻塞启动：下发与"等页面回执"都在后台，超时/失败只写日志（见 PushIceConfigAsync）。
        _ = PushIceConfigAsync("启动");

        // 【S1 启动对账】主动问页面"你有哪些方法"，与 BridgeContract 登记对账。
        // 以前 listMethods() 存在但**没人调**（审计病根 3）⇒ 方法名拼错/页面漏实现会静默失败。
        // 结果由 case "engine-methods" 处理；出错时在界面与日志里明确说清。
        try
        {
            await _media.CallAsync("listMethods");
        }
        catch (Exception ex)
        {
            AppendEngineLog($"[契约] 启动对账失败：调 listMethods 就出错了（{ex.Message}）");
            RenderEngineLog();
        }

        EngineStatus.Text = $"信令已启动 · {_signalPort}";
        AppendEngineLog($"信令地址 {_media.SignalUrl}");
        if (_externalSignalUrl is null)
        {
            AppendEngineLog($"模式：房主（本机中继信令）");
            AppendEngineLog($"把本地信令地址给对方：{_media.SignalUrl}");
            AppendEngineLog($"本机的组网/局域网地址可用现有 netgroup 功能查询");
        }
        else
        {
            AppendEngineLog($"模式：客户端（信令走 {_externalSignalUrl}）");
        }
        AppendEngineLog($"房间名 {_room} —— 双方必须一致");
        RenderEngineLog();

        // 脚本化录音自检：录完直接退出，方便 CI/命令行验证整条录音链路。
        // 用命令行而不是模拟点击按钮 —— 后者依赖像素坐标，很脆弱。
        if (_recordTestMode)
        {
            AppendEngineLog("【录音自检模式】3 秒后开始录制…");
            RenderEngineLog();
            await System.Threading.Tasks.Task.Delay(1000);
            await OnRecordClickedAsync();
        }

        // 双机自动化自测
        if (_selfTestRole is { Length: > 0 })
        {
            await RunSelfTestAsync(_selfTestRole);
        }

        // 回归测试：共享 → 停止 → 再次共享 → 通话
        if (_regressMode)
        {
            // 由谁主叫：优先看显式角色，否则用「是否房主模式」推断
            var isHostForCall = _selfTestRole is { Length: > 0 }
                ? _selfTestRole == "host"
                : _externalSignalUrl is null;
            AppendEngineLog("【回归】等待链路…");
            RenderEngineLog();
            for (int i = 0; i < 60 && !_chatReady; i++)
                await System.Threading.Tasks.Task.Delay(250);

            if (!_chatReady)
            {
                AppendEngineLog("【回归】链路未就绪，放弃");
                RenderEngineLog();
            }
            else
            {
                // ① 第一次共享
                AppendEngineLog("【回归】① 第一次共享（合成画面，6 秒）");
                RenderEngineLog();
                await _media!.CallAsync("startScreenShare",
                    new { synthetic = true, audio = false, maxFps = 15 });
                await System.Threading.Tasks.Task.Delay(6000);

                // ② 停止
                AppendEngineLog("【回归】② 停止共享");
                RenderEngineLog();
                await _media.CallAsync("stopScreenShare");
                await System.Threading.Tasks.Task.Delay(3000);

                // ③ 【关键】第二次共享 —— 用户报告这里就坏了
                AppendEngineLog("【回归】③ 第二次共享（用户报告此处失败）");
                RenderEngineLog();
                await _media.CallAsync("startScreenShare",
                    new { synthetic = true, audio = false, maxFps = 15 });
                await System.Threading.Tasks.Task.Delay(6000);

                await _media.CallAsync("stopScreenShare");
                await System.Threading.Tasks.Task.Delay(2000);

                // ④ 【关键】共享之后还能不能通话
                // 只在 host 端发起：两端同时主叫会互相打断（glare）。
                if (isHostForCall)
                {
                    AppendEngineLog("【回归】④ 共享之后再发起通话（用户报告此处也坏了）");
                    RenderEngineLog();
                    await OnCallClickedAsync();
                    await System.Threading.Tasks.Task.Delay(8000);
                }
                else
                {
                    AppendEngineLog("【回归】④ 等待对端来电");
                    RenderEngineLog();
                    for (int i = 0; i < 40 && !_inCall; i++)
                        await System.Threading.Tasks.Task.Delay(250);
                }

                AppendEngineLog($"【回归】结束：通话中={_inCall} " +
                                $"静音可用={MuteButton.IsEnabled}");
                RenderEngineLog();
            }
        }

        // P1-6 原生视频验收：直接驱动"画面区可见"这个收口点。
        // 【为什么需要这个旗标】真实触发者是「开始共享」和「观看对方」，而这两条都要求
        // 已经和第二个对端建立聊天（_chatReady）——单实例跑不起来。这里只替换"谁来让它可见"，
        // 走的是同一个 SetScreenViewVisible，所以原生视频的起停逻辑是真实被验证的。
        if (_nativeVideoTestSeconds > 0)
        {
            await System.Threading.Tasks.Task.Delay(2000);
            AppendEngineLog($"【原生视频验收】显示画面区 {_nativeVideoTestSeconds} 秒");
            RenderEngineLog();
            SetScreenViewVisible(true, "原生视频验收（测试旗标）");
            await System.Threading.Tasks.Task.Delay(_nativeVideoTestSeconds * 1000);
            AppendEngineLog("【原生视频验收】隐藏画面区（助手应随之停止）");
            SetScreenViewVisible(false);
            RenderEngineLog();
            await System.Threading.Tasks.Task.Delay(2000);
            AppendEngineLog("【原生视频验收】结束");
            RenderEngineLog();
        }

        // 「单个应用音频」可用性探测。
        //   ① 验收旗标 --probe-app-audio：显式探一次并把结论写日志；
        //   ② 正式路径：共享开始时**在后台**探一次（结论缓存），把"本机能不能用"如实写进日志。
        // 【为什么放后台】它会拉起一个子进程（1~2 秒），不能阻塞共享流程；
        // 【为什么在子进程里探】进程回环激活会让调用进程堆损坏 0xC0000374，
        //   SEH 拦不住 —— 子进程崩了就当"不支持"，主程序照常跑（见 AppAudioProbe 注释）。
        if (_probeAppAudioTest)
        {
            AppendEngineLog("【音频能力验收】开始探测「单个应用音频」（在子进程里探）…");
            RenderEngineLog();
            var probeResult = await System.Threading.Tasks.Task.Run(() => AppAudioProbe.Probe(force: true));
            AppendEngineLog($"【音频能力验收】{probeResult.ShortText}");
            if (probeResult.Reason.Length > 0)
                AppendEngineLog($"【音频能力验收】原因：{probeResult.Reason}");
            AppendEngineLog($"【音频能力验收】判定={probeResult.Verdict}（主进程存活 = 子进程隔离生效）");
            RenderEngineLog();
        }

        // 真实屏幕采集测试：走和点按钮完全相同的路径
        if (_shareRealSeconds > 0)
        {
            AppendEngineLog("【真实共享测试】等待链路…");
            RenderEngineLog();
            for (int i = 0; i < 60 && !_chatReady; i++)
                await System.Threading.Tasks.Task.Delay(250);

            if (_chatReady)
            {
                AppendEngineLog("【真实共享测试】调用 OnShareScreenClickedAsync（与点按钮同一路径）");
                RenderEngineLog();
                await OnShareScreenClickedAsync();
                AppendEngineLog($"【真实共享测试】等待 {_shareRealSeconds} 秒后自动停止");
                RenderEngineLog();
                await System.Threading.Tasks.Task.Delay(_shareRealSeconds * 1000);
                AppendEngineLog("【真实共享测试】停止共享");
                RenderEngineLog();
                await _media!.CallAsync("stopScreenShare");
                await System.Threading.Tasks.Task.Delay(3000);
                AppendEngineLog("【真实共享测试】结束");
                RenderEngineLog();
            }
            else
            {
                AppendEngineLog($"【真实共享测试】链路未就绪（聊天未连接）");
                RenderEngineLog();
            }
        }

        // 屏幕共享完整周期：开始 → 等 10 秒 → 停止
        if (_screenCycleMode)
        {
            AppendEngineLog("【共享周期测试】等待链路…");
            RenderEngineLog();
            for (int i = 0; i < 60 && !_chatReady; i++)
                await System.Threading.Tasks.Task.Delay(250);

            if (_chatReady)
            {
                AppendEngineLog("【共享周期测试】① 开始共享（合成画面）");
                RenderEngineLog();
                await _media!.CallAsync("startScreenShare",
                    new { synthetic = true, audio = false, maxFps = 15 });

                AppendEngineLog("【共享周期测试】共享 10 秒，观察对端收帧…");
                RenderEngineLog();
                await System.Threading.Tasks.Task.Delay(10000);

                AppendEngineLog("【共享周期测试】② 停止共享");
                RenderEngineLog();
                await _media.CallAsync("stopScreenShare");

                AppendEngineLog("【共享周期测试】停止后再观察 10 秒（对端帧计数应归零）");
                RenderEngineLog();
                await System.Threading.Tasks.Task.Delay(10000);
                AppendEngineLog("【共享周期测试】结束");

                // 打印最后一次收到的帧计数，便于判断是否真的停了
                AppendEngineLog($"【共享周期测试】最终远端帧计数 = {_lastRemoteFrameCount:F0}");
                RenderEngineLog();
            }
            else
            {
                AppendEngineLog($"【共享周期测试】链路未就绪（聊天未连接）");
                RenderEngineLog();
            }
        }

        // 屏幕捕获能力探针：只回答"宿主放不放行"，不做传输
        if (_screenProbeMode)
        {
            AppendEngineLog("【屏幕捕获探针】调用 getDisplayMedia（会弹出选择窗口）…");
            RenderEngineLog();
            await _media.CallAsync("probeScreenCapture", new { timeoutMs = 12000 });
            await System.Threading.Tasks.Task.Delay(1000);
            try { Application.Current.Exit(); } catch { }
        }

        // 【脚本化语音通话验证已改为事件驱动】
        // 原来这里的等待循环跑在 InitializeMediaAsync 里、而加入方之后才连上
        // ⇒ 15 秒必然超时（"链路未就绪，放弃"）⇒ **语音通话从来没被真正验证过**。
        // 现在由 chat-open 事件触发 RunCallTestAsync（与 chat/file 测试一致）。
        _ = _callTestMode;

        // 脚本化「共享电脑声音」验证（A1）：链路就绪后自动拨开开关（走界面同一路径）
        if (_shareAudioTestMode)
        {
            AppendEngineLog("【共享声音测试】等待链路…");
            RenderEngineLog();
            for (int i = 0; i < 60 && !_chatReady; i++)
            {
                await System.Threading.Tasks.Task.Delay(250);
            }
            AppendEngineLog(_chatReady ? "【共享声音测试】链路就绪，打开「共享电脑声音」"
                                       : "【共享声音测试】链路未就绪，仍然打开开关（看日志里的错误）");
            RenderEngineLog();
            ShareAudioToggle.IsOn = true;          // 触发 Toggled → OnShareAudioToggledAsync（与手点一致）
            await System.Threading.Tasks.Task.Delay(1500);
        }

        // 脚本化屏幕共享验证：链路就绪后用合成画面开始共享
        if (_screenTestMode)
        {
            AppendEngineLog("【屏幕共享测试】等待链路…");
            RenderEngineLog();
            for (int i = 0; i < 60 && !_chatReady; i++)
            {
                await System.Threading.Tasks.Task.Delay(250);
            }
            if (_chatReady)
            {
                AppendEngineLog("【屏幕共享测试】用合成画面开始共享（跳过系统选窗口）");
                RenderEngineLog();
                await _media!.CallAsync("startScreenShare",
                    new { synthetic = true, audio = false, maxFps = 15 });
            }
            else
            {
                AppendEngineLog($"【屏幕共享测试】链路未就绪（聊天未连接）");
                RenderEngineLog();
            }
        }

        // 【脚本化文件传输验证已改为事件驱动】
        // 原来这里的等待循环跑在 InitializeMediaAsync 里、而加入方在之后才连上
        // ⇒ 15 秒必然超时（"链路未就绪"）⇒ **文件传输从来没被真正验证过**。
        // 现在改到 `chat-open`（通道就绪）时触发，见 RunFileTestAsync 的调用点。
        _ = _fileTestPath;

        // 脚本化信号链验证：合成 1 kHz 正弦跑一遍编解码，只报客观指标。
        // 这一项可进 CI —— 它不需要人耳、不需要真实麦克风环境。
        if (_signalTestMode)
        {
            AppendEngineLog("【信号链验证】合成 1 kHz 正弦走编解码往返…");
            RenderEngineLog();
            await System.Threading.Tasks.Task.Delay(500);
            await _media.CallAsync("signalChainTest", new { freq = 1000, seconds = 1 });

            // 兜底超时：验证本身挂了也要退出，别让命令行一直吊着
            _ = System.Threading.Tasks.Task.Delay(20000).ContinueWith(_ =>
                DispatcherQueue.TryEnqueue(() => { try { Application.Current.Exit(); } catch { } }));
        }
    }

    private async System.Threading.Tasks.Task RunSelfCheckAsync()
    {
        if (_media is null || !_media.IsReady) return;
        RunSelfCheckButton.IsEnabled = false;
        EngineStatus.Text = "检测中…";
        await _media.CallAsync("selfCheck");
    }

    // =======================================================================
    // 通话控制
    // =======================================================================

    private async System.Threading.Tasks.Task OnCallClickedAsync()
    {
        if (_media is null) return;
        CallButton.IsEnabled = false;
        AppendEngineLog("发起通话…");
        RenderEngineLog();
        await _media.CallAsync("call");
    }

    private async System.Threading.Tasks.Task OnMuteClickedAsync()
    {
        if (_media is null) return;
        _muted = !_muted;
        MuteButton.Content = _muted ? "取消静音" : "静音";
        await _media.CallAsync("setMuted", new { muted = _muted });
    }

    private async System.Threading.Tasks.Task OnHangupClickedAsync()
    {
        if (_media is null) return;
        await _media.CallAsync("hangup");
    }

    /// <summary>
    /// 录音自检：录 3 秒麦克风（含当前降噪处理）并落盘。
    ///
    /// 为什么要有这个功能：同一台机器上跑两个实例测出来的"音质差"里，
    /// 混着声学回环伪影（扬声器的声音被麦克风收回去），无法用来判断
    /// 真实音质。而录下本地麦克风经过处理链之后的音频，就能在单机上
    /// 客观对比不同降噪档位的差别 —— 听文件、看波形、比噪声底都行。
    /// </summary>
    private async System.Threading.Tasks.Task OnRecordClickedAsync()
    {
        if (_media is null || !_media.IsReady) return;
        RecordButton.IsEnabled = false;
        AppendEngineLog("开始录音自检（3 秒），请正常说话…");
        RenderEngineLog();
        await _media.CallAsync("recordSample", new { seconds = 3 });
    }

    /// <summary>把媒体页传来的 WAV 写到磁盘。</summary>
    private void SaveRecording(JsonElement payload)
    {
        try
        {
            if (payload.TryGetProperty("ok", out var okEl)
                && okEl.ValueKind == JsonValueKind.False)
            {
                var err = payload.TryGetProperty("error", out var e) ? e.GetString() : "未知";
                AppendEngineLog($"[录音失败] {err}");
                return;
            }

            var b64 = payload.TryGetProperty("dataBase64", out var d) ? d.GetString() : null;
            if (string.IsNullOrEmpty(b64))
            {
                AppendEngineLog("[录音失败] 没收到音频数据");
                return;
            }

            var bytes = Convert.FromBase64String(b64);

            // 放在 exe 同目录的 recordings\ 下，并按档位命名 ——
            // 切换档位各录一次，文件名就能直接对比。
            var dir = System.IO.Path.Combine(AppContext.BaseDirectory, "recordings");
            System.IO.Directory.CreateDirectory(dir);
            var level = payload.TryGetProperty("stats", out var st) &&
                        st.TryGetProperty("denoiseLevel", out var lv)
                ? lv.GetString() ?? "unknown" : "unknown";
            var name = $"mic-{level}-{DateTime.Now:HHmmss}.wav";
            var path = System.IO.Path.Combine(dir, name);
            System.IO.File.WriteAllBytes(path, bytes);

            AppendEngineLog($"录音已保存: {path}");
            if (payload.TryGetProperty("stats", out var s))
            {
                AppendEngineLog($"  时长 {Str(s, "seconds")}s  峰值 {Str(s, "peakDbfs")} dBFS  " +
                                $"RMS {Str(s, "rmsDbfs")} dBFS");
                AppendEngineLog($"  浏览器 AEC={GetBool(s, "browserAec")} " +
                                $"NS={GetBool(s, "browserNs")} " +
                                $"AGC={GetBool(s, "browserAgc")} " +
                                $"软件链={GetBool(s, "softwareChain")}");
                if (payload.TryGetProperty("stats", out var s2)
                    && s2.TryGetProperty("clipped", out var cl)
                    && cl.ValueKind == JsonValueKind.True)
                {
                    AppendEngineLog("  ⚠ 检测到削波：把麦克风音量调低一档会更好");
                }
            }
            AppendEngineLog("对比方法：分别用「关 / 标准 / 强力」各录一次，播放对比。");

            // 录音自检模式下写完文件就退出，便于脚本判断成功与否。
            // 用 Application.Current.Exit() 而不是 Window.Close()：
            // 后者只关窗口，进程可能因为还有存活的 COM/WebView2 资源而不退，
            // 表现就是命令行一直挂着（踩过）。
            if (_recordTestMode)
            {
                RenderEngineLog();
                _recordTestMode = false;
                _ = System.Threading.Tasks.Task.Delay(600).ContinueWith(_ =>
                    DispatcherQueue.TryEnqueue(() =>
                    {
                        try { Application.Current.Exit(); } catch { }
                    }));
            }
        }
        catch (Exception ex)
        {
            AppendEngineLog($"[录音保存失败] {ex.GetType().Name}: {ex.Message}");
        }
    }

    /// <summary>更新麦克风电平条。dBFS 映射到 0~60px 宽度。</summary>
    private void UpdateMicLevel(JsonElement p)
    {
        var db = GetDouble(p, "dbfs");
        var muted = p.TryGetProperty("muted", out var m) && m.ValueKind == JsonValueKind.True;

        // -60 dBFS → 0px，-10 dBFS → 满宽
        var pct = Math.Clamp((db + 60) / 50.0, 0, 1);
        MicLevelBar.Width = 60 * pct;

        var clipped = p.TryGetProperty("clipped", out var c) && c.ValueKind == JsonValueKind.True;
        // 削波变红（要提醒用户调小），静音变灰，正常用说话绿
        MicLevelBar.Background = new SolidColorBrush(clipped
            ? Microsoft.UI.Colors.OrangeRed
            : muted
                ? Microsoft.UI.Colors.Gray
                : Windows.UI.Color.FromArgb(255, 0x4C, 0xC2, 0x6A));
    }

    // =======================================================================
    // 文字聊天
    // =======================================================================

    private async System.Threading.Tasks.Task SendChatAsync()
    {
        if (_media is null || !_media.IsReady) return;
        var text = MessageInput.Text?.Trim() ?? "";
        if (text.Length == 0) return;

        MessageInput.Text = "";
        await _media.CallAsync("sendChat", new { text });
    }

    /// <summary>
    /// 往消息列表里追加一条消息。
    ///
    /// 动态建控件而不是数据绑定：消息列表要支持"文件卡片""系统提示"
    /// 这类形态差异较大的条目，用模板选择器反而更啰嗦。
    /// </summary>
    private void AddMessage(string text, bool isSelf, DateTime at, string senderName = "")
    {
        // 记录进日志：这样"自己发的消息到底有没有显示"是**可脚本验证**的。
        // 本会话就因为只能靠肉眼看界面，把"自己的消息完全不显示"漏了很久。
        AppendEngineLog($"[界面] 插入消息气泡 {(isSelf ? "我方" : "对方")}：" +
                        (text.Length > 24 ? text[..24] + "…" : text));

        // 【C3】按**真实日期**插入分隔（以前 XAML 里写死一个"今天"，永远显示今天）。
        if (_lastMessageDay != at.Date)
        {
            _lastMessageDay = at.Date;
            var today = DateTime.Today;
            var label = at.Date == today ? "今天"
                : at.Date == today.AddDays(-1) ? "昨天"
                : at.Date.ToString("MM-dd");
            MessagesPanel.Children.Add(new TextBlock
            {
                Text = label,
                Style = (Style)Application.Current.Resources["CaptionTextBlockStyle"],
                FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
                Foreground = ThemeBrush("TextFillColorTertiaryBrush"),
                HorizontalAlignment = HorizontalAlignment.Center,
            });
            AppendEngineLog($"[界面] 聊天日期分隔 → {label}");   // 让"分隔是不是真实日期"可脚本验证
        }

        var row = new Grid();
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

        // 头像
        var avatar = new Grid { Width = 34, Height = 34, VerticalAlignment = VerticalAlignment.Top };
        var circle = new Microsoft.UI.Xaml.Shapes.Ellipse
        {
            Fill = isSelf
                ? (Microsoft.UI.Xaml.Media.Brush)Application.Current.Resources["AccentFillColorDefaultBrush"]
                : (Microsoft.UI.Xaml.Media.Brush)Application.Current.Resources["ControlStrongFillColorDefaultBrush"],
        };
        var initial = new TextBlock
        {
            // 头像首字：以前写死"我"/"对"（用户一眼看出是写死的）→ 用真实名字首字
            Text = FirstChar(isSelf ? SelfDisplayName() : (senderName.Length > 0 ? senderName : PeerDisplayName(""))),
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
        };
        if (isSelf) initial.Foreground = (Microsoft.UI.Xaml.Media.Brush)Application.Current.Resources["TextOnAccentFillColorPrimaryBrush"];
        avatar.Children.Add(circle);
        avatar.Children.Add(initial);
        Grid.SetColumn(avatar, 0);
        row.Children.Add(avatar);

        // 正文
        var body = new StackPanel { Margin = new Thickness(10, 0, 0, 0) };
        var head = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        head.Children.Add(new TextBlock
        {
            Text = isSelf ? SelfDisplayName() : (senderName.Length > 0 ? senderName : "对方"),
            FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
        });
        head.Children.Add(new TextBlock
        {
            Text = at.ToString("HH:mm"),
            Style = (Style)Application.Current.Resources["CaptionTextBlockStyle"],
            VerticalAlignment = VerticalAlignment.Center,
        });
        body.Children.Add(head);
        body.Children.Add(new TextBlock
        {
            Text = text,
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 3, 0, 0),
        });
        Grid.SetColumn(body, 1);
        row.Children.Add(body);

        MessagesPanel.Children.Add(row);

        // 只保留最近 200 条，避免长时间聊天后视觉树无限增长
        while (MessagesPanel.Children.Count > 201)
        {
            MessagesPanel.Children.RemoveAt(1);
        }

        // 滚到底
        try { ChatScroll.ChangeView(null, ChatScroll.ScrollableHeight, null); }
        catch { }
    }

    // =======================================================================
    // 按钮可用状态的**唯一**决定处
    //
    // 【为什么必须集中】
    // 之前这些 IsEnabled 散落在 roster / call-state / screen-* 等六七个分支里。
    // 结果是「共享屏幕」按钮初始为禁用，但**没有任何一处把它打开** ——
    // 房间里有人的时候它仍然点不动，屏幕共享等于进不去。
    // 这类"状态被多处修改"的 bug 靠逐个补赋值只会越补越漏，所以统一到这里。
    // =======================================================================

    /// <summary>
    /// 本机是否正在共享屏幕（S2：状态本体在 ScreenShareService，这里只做只读转发，
    /// 写入口统一走 ScreenShareService 的迁移方法 —— 以前这个 bool 被 4 处随手赋值）。
    /// </summary>
    private bool _sharingScreen => _screenShare.Sharing;
    /// <summary>对端是否正在共享屏幕（本机在观看）。</summary>
    private bool _viewingRemoteScreen;

    private void UpdateActionButtons()
    {
        // 成员行的"共享中/我"徽标跟着状态走（内容没变时本函数直接返回，不会有开销）
        RefreshMemberList();
        var ready = _media?.IsReady == true;
        var hasPeer = _peerCount > 0;

        // 【2026-10-07 按用户建议合并】一个键在「加入语音」/「挂断」之间切换，
        // 不再单独放一个挂断键（用户原话："取消挂断键让加入语音被点击后自动变成挂断键"）。
        // 未通话：有对端就能加入；通话中：同一个键变成"挂断"，且始终可点。
        CallButton.Content = _inCall ? "挂断" : "加入语音";
        CallButton.IsEnabled = ready && hasPeer;

        // 共享屏幕：有对端、自己没有在共享（对端在共享时也不允许，
        // 避免两边同时推画面把带宽吃光，而且界面上只有一个显示区）
        ShareScreenButton.IsEnabled = ready && hasPeer
                                      && !_sharingScreen && !_viewingRemoteScreen;

        // 发声相关：在通话中才可用
        // 【2026-10-07 修复·"无法挂断和静音"】
        // 以前这两个按钮**完全依赖 `_inCall`**，而 `_inCall` 又只由页面的 `call-link`
        // 事件驱动（页面 `setInCall()` 本身不上报，任何一条路径漏了 reportCallState，
        // 界面就永远停在旧状态）⇒ 一旦判定出偏差，用户**既挂不断也静不了**（实测反馈）。
        // 改成：**只要有对端就能挂断/静音** —— 这是永远不会错的下限
        //（没有对端时确实是灰的，符合直觉）。
        MuteButton.IsEnabled = ready && hasPeer;
        HangupButton.IsEnabled = ready && hasPeer;

        // 只在状态真正变化时记一行。
        // 加这个的理由：按钮可用性曾是"没人在正确时机打开共享屏幕按钮"
        // 这种 bug，光看代码六个赋值点很难发现；有这一行就能在日志里
        // 直接确认"有对端时共享按钮确实被打开了"。
        if (_lastButtonSummary != (_call: CallButton.IsEnabled,
                                   _share: ShareScreenButton.IsEnabled,
                                   _mute: MuteButton.IsEnabled))
        {
            _lastButtonSummary = (CallButton.IsEnabled, ShareScreenButton.IsEnabled,
                                  MuteButton.IsEnabled);
            AppendEngineLog($"[按钮] 对端={_peerCount} 通话中={_inCall} " +
                            $"共享中={_sharingScreen} 看对方={_viewingRemoteScreen} → " +
                            $"发起通话={Yn(CallButton.IsEnabled)} " +
                            $"共享屏幕={Yn(ShareScreenButton.IsEnabled)} " +
                            $"静音={Yn(MuteButton.IsEnabled)}");
        }
    }

    private (bool _call, bool _share, bool _mute) _lastButtonSummary = (false, false, false);

    /// <summary>最近一次收到的远端画面帧计数（用于判断停止共享后是否真的停了）。</summary>
    private double _lastRemoteFrameCount;
    private double _lastLoggedFrameCount;

    /// <summary>
    /// 聊天通道是否真的打开。
    ///
    /// 【为什么需要单独一个字段】
    /// ChatStatus 的文本原来被三处互相覆盖：chat-open/closed 与屏幕共享的
    /// started/stopped。结果共享一开始，界面就显示"正在共享屏幕"；共享停止后
    /// 又写回"聊天未连接" —— 而聊天通道明明还开着，输入框于是被禁用。
    /// 现在状态与文案分开：这个字段是事实，ChatStatus 只是它的呈现。
    /// </summary>
    private bool _chatReady;
    /// <summary>
    /// 当前是否在共享/观看屏幕（只影响文案，不影响聊天可用性）。
    /// S2：本体在 ScreenShareService.Busy，写入口统一走它的迁移方法。
    /// </summary>
    private bool _screenBusy => _screenShare.Busy;

    /// <summary>
    /// 聊天栏的**临时提示**（如"捕获被占用，已清理，可重试"）与其过期时刻。
    ///
    /// 【2026-10-06 修复·UI 问题】原来 screen-error 分支刚把提示写进 ChatStatus，
    /// 紧跟着的 UpdateChatStatus() 就用"聊天已连接"把它覆盖 —— 提示**永远看不到**。
    /// 现在：未过期的提示优先显示，过期由计时器恢复真实状态；存活秒数来自 AppConfig（不写死）。
    /// </summary>
    private string? _chatTip;
    private DateTime _chatTipUntil;
    private Microsoft.UI.Dispatching.DispatcherQueueTimer? _chatTipTimer;

    /// <summary>显示一条临时提示：优先于事实文案，超过 AppConfig.ChatTipSeconds 自动恢复。</summary>
    private void ShowChatTip(string text)
    {
        _chatTip = text;
        _chatTipUntil = DateTime.UtcNow.AddSeconds(AppConfig.ChatTipSeconds);
        ChatStatus.Text = text;
        AppendEngineLog($"[UI] 聊天栏临时提示（{AppConfig.ChatTipSeconds} 秒后恢复真实状态）：{text}");

        if (_chatTipTimer is null)
        {
            _chatTipTimer = DispatcherQueue.CreateTimer();
            _chatTipTimer.IsRepeating = false;
            _chatTipTimer.Tick += (_, _) =>
            {
                _chatTip = null;          // 过期 → 恢复
                UpdateChatStatus();
            };
        }
        // 重新计时：连续提示不会提前消失（间隔同样来自配置）
        _chatTipTimer.Interval = TimeSpan.FromSeconds(AppConfig.ChatTipSeconds);
        _chatTipTimer.Start();
    }

    private void UpdateChatStatus()
    {
        // 未过期的临时提示优先 —— 否则它会被下面的事实文案覆盖（这正是原来的 bug）
        if (_chatTip is { } tip && DateTime.UtcNow < _chatTipUntil)
        {
            ChatStatus.Text = tip;
        }
        else
        {
            _chatTip = null;              // 过期/没有提示 → 显示事实
            if (_screenBusy)
            {
                ChatStatus.Text = _chatReady
                    ? "屏幕共享中 · 聊天仍可用"
                    : "屏幕共享中";
            }
            else
            {
                ChatStatus.Text = _chatReady ? "聊天已连接" : "聊天未连接";
            }
        }

        // 聊天可用性只看通道，与是否在共享屏幕无关
        MessageInput.IsEnabled = _chatReady;
        SendMessageButton.IsEnabled = _chatReady;
    }

    private static string Yn(bool b) => b ? "可点" : "禁用";

    // =======================================================================
    // 屏幕共享
    // =======================================================================

    private async System.Threading.Tasks.Task OnShareScreenClickedAsync()
    {
        if (_media is null || !_media.IsReady) return;
        ShareScreenButton.IsEnabled = false;
        AppendEngineLog("请求屏幕共享权限（系统会弹出选择窗口）…");

        // 【P3 2026-10-06】请求之前先把本窗口**带到前台并置顶**。
        // 原因：「选择要共享的内容」是 WebView2 自己渲染的子窗口，
        // 主窗口不在前台时它可能开在后面（或另一个显示器上）—— 用户看不到就点不到，
        // 于是等满超时被判失败，表现正是用户说的"屏幕共享时好时坏"。
        // 顺带把提示文字写清楚，别让用户对着"正在请求…"发呆。
        try { Activate(); AppWindow.MoveInZOrderAtTop(); } catch { }
        ScreenDetailText.Text = "请在系统弹出的「选择要共享的内容」里选一个屏幕/窗口" +
                                "（看不到就按 Alt+Tab 找它；要共享声音选“整个屏幕”并打开“使用系统音频共享”）";

        // 【顺序很重要，而且这是修"选择界面只有一条缝"的关键】
        // 先把媒体宿主放大到**覆盖整个窗口**，再去请求采集 ——
        // 因为「选择共享源」界面是 WebView2 自己渲染的，尺寸受宿主约束。
        // 宿主小，选择界面就小到没法用。
        ApplyScreenHostSize(true);
        // 【不要再把 ScreenView.Height 设为 0】
        // 中间列第 0 行是 Auto 高度，而 ScreenView 的高度原本由内容决定，
        // 内容里的 Border 没有固有高度 —— 结果整行塌陷成 0，
        // MediaHost 的 ActualHeight 永远是 0，画面显示不全。
        // 实测日志："20 次重试仍未拿到布局尺寸（当前 676×0）"。
        // 高度交给 XAML 里固定的 380，这里只负责可见性。
        ScreenView.Visibility = Visibility.Visible;
        ScreenStatusText.Text = "正在请求屏幕共享…";
        ScreenDetailText.Text = "请在系统弹窗里选择要共享的窗口（20 秒内）";
        // 这里绕过了 SetScreenViewVisible（因为要先把宿主放大再请求采集），
        // 所以原生视频通道的启动要在这里补一次。
        EnsureNativeVideoStarted();

        // 共享开始时，在后台探一次「单个应用音频」可用性，把结论如实写进日志。
        // 子进程隔离：这一步哪怕让捕获工具崩掉，也不会影响主程序（结论会被缓存，只探一次）。
        _ = System.Threading.Tasks.Task.Run(() =>
        {
            var r = AppAudioProbe.Probe();
            DispatcherQueue.TryEnqueue(() => AppendEngineLog(
                $"[音频能力] {r.ShortText}" + (r.Reason.Length > 0 ? $"（{r.Reason}）" : "")));
        });

        _screenShare.RequestStarted();    // S2：状态迁移走状态机（等价于原来的 _screenBusy = true）
        UpdateChatStatus();
        RenderEngineLog();

        await _media.CallAsync("startScreenShare",
            new { audio = true, maxFps = 15, timeoutMs = 20000 });
    }

    private async System.Threading.Tasks.Task OnStopShareClickedAsync()
    {
        if (_media is null || !_media.IsReady) return;
        await _media.CallAsync("stopScreenShare");
    }

    /// <summary>
    /// 本机在共享时，浮层上的停止按钮也要能用。
    ///
    /// 场景差异：自己共享时，浮层显示"屏幕共享中"（因为本机画面不回转给自己），
    /// 此时浮层按钮应该真的停掉采集；观看对方时，浮层按钮只是退出查看。
    /// </summary>
    private async System.Threading.Tasks.Task OnScreenViewStopClickedAsync()
    {
        if (_sharingScreen)
        {
            await OnStopShareClickedAsync();
        }
        else
        {
            _viewingRemoteScreen = false;
            SetScreenViewVisible(false);
        }
    }

    /// <summary>
    /// 切换"聊天视图 / 共享画面视图"。
    ///
    /// 共享画面由 WebView2 就地渲染，所以切换的本质是：
    /// 把那个平时 0×0 的媒体宿主放大成显示区并盖住聊天区。
    /// </summary>
    private void SetScreenViewVisible(bool visible, string? detail = null)
    {
        ScreenView.Visibility = visible ? Visibility.Visible : Visibility.Collapsed;

        // P1-6：原生视频通道跟着画面区起停。放在这里是因为它是"画面区可见性"的唯一收口点，
        // 共享开始、观看对方、点浮层停止都会经过它。
        if (visible) EnsureNativeVideoStarted(); else StopNativeVideo();

        // 【不要再隐藏聊天区】
        // 第一版共享时把 ChatScroll 和 InputBar 隐藏掉，理由是"避免重叠"。
        // 那是设计错误：看别人屏幕时仍然要能发消息、也要能看到历史。
        // 现在改成上下分栏 —— 共享画面占第 0 行，聊天与输入框保持可见。
        ApplyScreenHostSize(visible);

        if (visible)
        {
            ScreenStatusText.Text = _sharingScreen ? "我正在共享" : "正在观看";
            ScreenDetailText.Text = detail ?? "";
        }
        UpdateActionButtons();

        // 告诉页面显示区尺寸，让 <video> 按比例铺满
        if (_media is not null && _media.IsReady)
        {
            var w = Math.Max(1, (int)(ScreenView.ActualWidth - 2));
            var h = Math.Max(1, (int)(ScreenView.ActualHeight - 2));
            _ = _media.CallAsync("setViewport", new { w, h, visible });
        }

        // 诊断：1 秒后把画面区与宿主的真实几何信息打出来。
        // "看得见布局"比看代码靠谱 —— 之前误判多次都是因为没量实际坐标。
        DumpLayoutDiagnostics();
    }

    /// <summary>打印画面区/媒体宿主的实际尺寸与相对根容器的位置。</summary>
    private void DumpLayoutDiagnostics()
    {
        var t = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(900) };
        t.Tick += (_, _) =>
        {
            t.Stop();
            try
            {
                var svPos = ScreenView.TransformToVisual(RootGrid)
                    .TransformPoint(new Windows.Foundation.Point(0, 0));
                AppendEngineLog($"[诊断] 画面区: 可见={ScreenView.Visibility} " +
                                $"{ScreenView.ActualWidth:F0}×{ScreenView.ActualHeight:F0} " +
                                $"@({svPos.X:F0},{svPos.Y:F0})");
                AppendEngineLog($"[诊断] 媒体宿主: 尺寸={MediaHost.Width:F0}×{MediaHost.Height:F0} " +
                                $"实际={MediaHost.ActualWidth:F0}×{MediaHost.ActualHeight:F0} " +
                                $"透明度={MediaHost.Opacity:F1}");
                var wvPos = MediaHost.TransformToVisual(RootGrid)
                    .TransformPoint(new Windows.Foundation.Point(0, 0));
                AppendEngineLog($"[诊断] 宿主屏幕位置: @({wvPos.X:F0},{wvPos.Y:F0})");
                AppendEngineLog($"[诊断] 根容器: {RootGrid.ActualWidth:F0}×{RootGrid.ActualHeight:F0}");
            }
            catch (Exception ex)
            {
                AppendEngineLog($"[诊断] 失败: {ex.Message}");
            }
            RenderEngineLog();
        };
        t.Start();
    }

    /// <summary>
    /// 把媒体宿主（WebView2）放大成一块渲染表面，或缩回 0×0 隐藏状态。
    ///
    /// 用 宽高/边距 而不是 Visibility：WebView2 一旦被折叠就没有渲染表面，
    /// 屏幕采集的源选择窗口可能因此不弹出。
    /// </summary>
    /// <summary>
    /// 媒体宿主的显隐。
    ///
    /// 【为什么这里不再有任何尺寸/坐标计算】
    /// 之前 MediaHost 挂在根容器上，靠手算 Margin 定位、按窗口宽度算尺寸，
    /// 结果实测画在窗口 (0,0)，而画面区在 (252,60) —— 画面根本不在画面区里，
    /// 用户看到的就是"共享时看不到画面"。
    /// 现在 MediaHost 声明在 ScreenView **内部**，位置与尺寸全部由布局系统负责，
    /// 这里只需要控制透明度与命中测试。
    /// </summary>
    private void ApplyScreenHostSize(bool visible)
    {
        MediaHost.Opacity = visible ? 1 : 0;
        MediaHost.IsHitTestVisible = visible;
    }

    // =======================================================================
    // 文件传输
    // =======================================================================

    /// <summary>
    /// 选文件并发送。
    ///
    /// WebView2 页面没有文件系统访问权限（这是浏览器的安全边界），
    /// 所以由 C# 读盘 → base64 → 交给页面 → DataChannel 发送。
    ///
    /// WinUI 3 桌面应用的 FileOpenPicker 必须显式设置窗口句柄，
    /// 否则会抛「需要窗口句柄」的异常 —— 这是很容易踩的一步。
    /// </summary>
    private async System.Threading.Tasks.Task OnAttachFileClickedAsync()
    {
        if (_media is null || !_media.IsReady) return;

        if (!_chatReady)
        {
            AppendEngineLog("[文件] 聊天通道未就绪，无法发送文件");
            RenderEngineLog();
            return;
        }

        try
        {
            var picker = new Windows.Storage.Pickers.FileOpenPicker();
            var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
            WinRT.Interop.InitializeWithWindow.Initialize(picker, hwnd);
            picker.SuggestedStartLocation =
                Windows.Storage.Pickers.PickerLocationId.DocumentsLibrary;
            picker.FileTypeFilter.Add("*");

            var file = await picker.PickSingleFileAsync();
            if (file is null) return;

            // 大文件提醒：base64 会膨胀 33%，且全部在内存里。
            var props = await file.GetBasicPropertiesAsync();
            var size = (long)props.Size;
            AppendEngineLog($"准备发送 {file.Name}（{size / 1024.0:F1} KB）");
            if (size > 64L * 1024 * 1024)
            {
                AppendEngineLog("⚠ 文件超过 64 MB：当前实现整份读入内存并 base64，" +
                                "大文件建议后续改走多流 TCP 通道");
            }
            RenderEngineLog();

            var buffer = await Windows.Storage.FileIO.ReadBufferAsync(file);
            var bytes = new byte[buffer.Length];
            using (var reader = Windows.Storage.Streams.DataReader.FromBuffer(buffer))
            {
                reader.ReadBytes(bytes);
            }

            await _media.CallAsync("sendFile", new
            {
                name = file.Name,
                mime = file.ContentType ?? "application/octet-stream",
                dataBase64 = Convert.ToBase64String(bytes),
            });

            _lastSendFileStatus = AddFileMessage(file.Name, size, isSelf: true, "发送中…");
        }
        catch (Exception ex)
        {
            AppendEngineLog($"[文件发送失败] {ex.GetType().Name}: {ex.Message}");
            RenderEngineLog();
        }
    }

    /// <summary>
    /// 收到的文件写到磁盘。
    ///
    /// 【为什么不写死"下载"目录】
    /// 第一版写死了 %USERPROFILE%\Downloads，实测直接抛
    /// UnauthorizedAccessException（非提权进程常常不能写那里，
    /// 哪怕路径看起来是自己的用户目录）。
    /// 所以这里**按可写性探测**，找到第一个能写的位置：
    ///   1) 用户的「下载」目录（最符合预期，能写就用它）
    ///   2) exe 同目录的 received\（便携、几乎总能写）
    ///   3) 临时目录（最后的兜底，至少不丢文件）
    /// 并把实际保存位置写进日志 —— 用户找不到文件是最糟的体验。
    /// </summary>
    /// <summary>接收入口目录 —— 实现在 FileStore（S2：落盘策略集中一处）。</summary>
    private static string ResolveReceiveDirectory() => FileStore.ResolveReceiveDirectory();

    /// <summary>
    /// 收到文件：落盘交给 FileStore（目录候选、防路径穿越、重名不覆盖都在那边），
    /// 这里只负责"记日志 + 渲染卡片"（S2：UI 与落盘策略分开）。
    /// </summary>
    private void SaveReceivedFile(JsonElement p)
    {
        try
        {
            var b64 = Str(p, "dataBase64");
            if (string.IsNullOrEmpty(b64))
            {
                AppendEngineLog("[文件] 没收到数据");
                return;
            }
            var (path, bytes, savedName) = _fileStore.SaveBase64(Str(p, "name"), b64);
            AppendEngineLog($"文件已保存: {path}（{bytes} 字节）");
            AddFileMessage(savedName, bytes, isSelf: false, "已接收", savedPath: path);
        }
        catch (Exception ex)
        {
            AppendEngineLog($"[文件保存失败] {ex.GetType().Name}: {ex.Message}");
        }
    }

    /// <summary>用系统默认程序打开一个文件（失败时记日志，不静默）。</summary>
    private void OpenPath(string path, string what)
    {
        try
        {
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
            {
                FileName = path,
                UseShellExecute = true,       // 交给系统按扩展名选默认程序
            });
            AppendEngineLog($"[打开] 已请求系统打开{what}：{path}");
        }
        catch (Exception ex)
        {
            AppendEngineLog($"[打开失败] {what}：{ex.Message}");
            ShowChatTip($"打不开：{ex.Message}");
        }
        RenderEngineLog();
    }

    /// <summary>在资源管理器里**选中**该文件（比只打开目录更好用）。</summary>
    private void OpenFolderWithSelection(string path)
    {
        try
        {
            if (System.IO.File.Exists(path))
            {
                System.Diagnostics.Process.Start("explorer.exe", $"/select,\"{path}\"");
            }
            else
            {
                var dir = System.IO.Path.GetDirectoryName(path);
                if (!string.IsNullOrEmpty(dir)) OpenFolder(dir, "文件所在文件夹");
            }
        }
        catch (Exception ex)
        {
            AppendEngineLog($"[打开文件夹失败] {ex.Message}");
            RenderEngineLog();
        }
    }

    /// <summary>往消息流里插一张文件卡片。</summary>
    private TextBlock AddFileMessage(string name, long size, bool isSelf, string status,
                                     string senderName = "", string savedPath = "")
    {
        var row = new Grid();
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

        var avatar = new Grid { Width = 34, Height = 34, VerticalAlignment = VerticalAlignment.Top };
        avatar.Children.Add(new Microsoft.UI.Xaml.Shapes.Ellipse
        {
            Fill = isSelf
                ? (Microsoft.UI.Xaml.Media.Brush)Application.Current.Resources["AccentFillColorDefaultBrush"]
                : (Microsoft.UI.Xaml.Media.Brush)Application.Current.Resources["ControlStrongFillColorDefaultBrush"],
        });
        avatar.Children.Add(new TextBlock
        {
            // 头像首字：以前写死"我"/"对"（用户一眼看出是写死的）→ 用真实名字首字
            Text = FirstChar(isSelf ? SelfDisplayName() : (senderName.Length > 0 ? senderName : PeerDisplayName(""))),
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
            Foreground = isSelf
                ? (Microsoft.UI.Xaml.Media.Brush)Application.Current.Resources["TextOnAccentFillColorPrimaryBrush"]
                : null,
        });
        Grid.SetColumn(avatar, 0);
        row.Children.Add(avatar);

        var card = new Border
        {
            Background = (Microsoft.UI.Xaml.Media.Brush)Application.Current.Resources["CardBackgroundFillColorDefaultBrush"],
            BorderBrush = (Microsoft.UI.Xaml.Media.Brush)Application.Current.Resources["CardStrokeColorDefaultBrush"],
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(6),
            Padding = new Thickness(12, 10, 14, 10),
            MaxWidth = 380,
            HorizontalAlignment = HorizontalAlignment.Left,
            Margin = new Thickness(10, 0, 0, 0),
        };
        var inner = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
        inner.Children.Add(new FontIcon
        {
            Glyph = "\uE7C3",
            FontSize = 20,
            VerticalAlignment = VerticalAlignment.Center,
            Foreground = (Microsoft.UI.Xaml.Media.Brush)Application.Current.Resources["AccentFillColorDefaultBrush"],
        });
        var txt = new StackPanel();
        txt.Children.Add(new TextBlock { Text = name, FontSize = 13, FontWeight = Microsoft.UI.Text.FontWeights.SemiBold });
        // 【必须能更新】以前这里画完就没人再改，于是"发送中…"永远不变（用户实测：
        // 对端已经"已接收"，发送方还显示"发送中…"）。这里把状态 TextBlock 回传给调用方，
        // 由 file-done 事件改成"已发送"。
        var statusText = new TextBlock
        {
            Text = $"{size / 1024.0:F1} KB · {status}",
            Style = (Style)Application.Current.Resources["CaptionTextBlockStyle"],
        };
        txt.Children.Add(statusText);
        inner.Children.Add(txt);

        // 【用户要求：文件要有"打开方式"】以前收到文件后没有任何入口，用户拿到文件也打不开。
        // 有完整路径时给出两个按钮（功能必须有界面入口）。
        if (savedPath.Length > 0 && System.IO.File.Exists(savedPath))
        {
            var openFile = new Button
            {
                Content = "打开文件", Padding = new Thickness(8, 2, 8, 2),
                VerticalAlignment = VerticalAlignment.Center,
            };
            openFile.Click += (_, _) => OpenPath(savedPath, "文件");
            ToolTipService.SetToolTip(openFile, savedPath);
            var openDir = new Button
            {
                Content = "打开所在文件夹", Padding = new Thickness(8, 2, 8, 2),
                VerticalAlignment = VerticalAlignment.Center,
            };
            // 选中该文件（explorer /select,）比只开目录更有用
            openDir.Click += (_, _) => OpenFolderWithSelection(savedPath);
            ToolTipService.SetToolTip(openDir, "在资源管理器中选中这个文件");
            inner.Children.Add(openFile);
            inner.Children.Add(openDir);
        }
        card.Child = inner;
        Grid.SetColumn(card, 1);
        row.Children.Add(card);

        MessagesPanel.Children.Add(row);
        try { ChatScroll.ChangeView(null, ChatScroll.ScrollableHeight, null); } catch { }
        return statusText;      // 回传给调用方，便于后续更新状态
    }

    // =======================================================================
    // 网络自检
    //
    // 【为什么必须做】
    // 第一次真机联调失败的原因就是网络：房主的服务只绑了回环地址，
    // 对端从网络根本连不上。这类问题在应用内不好判断是哪一环断的
    // （绑定？防火墙？组网？），所以做一个能明确回答"断在哪"的检查。
    //
    // 【S2】实现已搬到 NetworkProbe（netstat/netsh/TCP/HTTP/WebSocket 全在那边），
    // 这里只保留一个转发 + UI 入口（S2 判据：MainWindow 里不再有 Process.Start 干这类活）。
    // =======================================================================

    /// <summary>只读网络报告：监听状态、防火墙规则、本机地址（端口取真实生效的 _signalPort）。</summary>
    private string BuildNetReport() => new NetworkProbe(_signalPort, AppendEngineLog).BuildReport();

    /// <summary>连接自检：像对端那样去连房主的信令。</summary>
    private System.Threading.Tasks.Task<string> CheckHostAsync(string ip) =>
        new NetworkProbe(_signalPort, AppendEngineLog).CheckHostAsync(ip);

    /// <summary>从 ws://host:port/signal 里取出主机与端口（转发到 NetworkProbe）。</summary>
    private static (string host, int port)? TryParseHostPort(string url) =>
        NetworkProbe.TryParseHostPort(url);

    /// <summary>裸 TCP 预检（转发到 NetworkProbe）。</summary>
    private static System.Threading.Tasks.Task<(bool ok, string detail)> ProbeTcpAsync(
        string host, int port, int timeoutMs) => NetworkProbe.ProbeTcpAsync(host, port, timeoutMs);

    /// <summary>
    /// 连接自检：像对端那样去连房主的信令。
    ///
    /// 对端连不上时，先用这个确认是"网络不通"还是"应用问题"。
    /// </summary>
    // 【S2 已搬走】原来这里是 CheckHostAsync（TCP → 取页面 → WebSocket 握手）的完整实现，
    // 现在它在 NetworkProbe.CheckHostAsync。MainWindow 只保留上面那个转发。

    /// <summary>从 ws://host:port/signal 里取出主机与端口。</summary>
    // 【S2 已搬走】原来这里还有 TryParseHostPort 与 ProbeTcpAsync 的完整实现（各一份），
    // 现在它们在 NetworkProbe 里；MainWindow 只保留上面的转发，避免"同一件事两份实现"。

    /// <summary>
    /// 双机自动化自测。host 与 join 各跑一套，覆盖双向收发。
    ///
    /// 每一步都输出 `[自测] 步骤N ... → 结果`，方便直接从日志判断成败。
    /// </summary>
    private async System.Threading.Tasks.Task RunSelfTestAsync(string role)
    {
        var isHost = role == "host";
        int step = 0;
        void Step(string what) => AppendEngineLog($"[自测] 步骤{++step} {what}");
        void Result(string what) => AppendEngineLog($"[自测]        → {what}");

        AppendEngineLog($"[自测] 角色={role} 开始（端口 {_signalPort} 房间 {_room}）");
        AppendEngineLog("[自测] 本端会等待最多 30 分钟。请到另一台机器上启动加入方脚本；" +
                        "对方一旦连上，测试会自动开始，不需要回来操作本端。");
        RenderEngineLog();

        // 等链路。
        //
        // 【为什么设 30 分钟】
        // 之前设 60 秒、后来 5 分钟，都不够：真人要走到另一台机器、解压、
        // 双击、等启动，超过 5 分钟很常见。房主端一旦超时退出，整个自测
        // 就白跑了，而且看起来像"通话功能坏了"。宁可等久一点，
        // 也不要因为等待不够而给出错误结论。
        Step("等待与对端建立连接（最多 30 分钟，请到另一台机器启动加入方）");
        for (int i = 0; i < 7200 && !_chatReady; i++)
            await System.Threading.Tasks.Task.Delay(250);
        Result(_chatReady ? "链路已建立，聊天通道已打开" : "30 分钟内未连上对端");
        if (!_chatReady)
        {
            AppendEngineLog("[自测] 中断：链路未建立");
            RenderEngineLog();
            return;
        }

        // ① 聊天
        Step($"发送聊天消息（{role} → 对端）");
        MessageInput.Text = $"自测消息 from {role}";
        await SendChatAsync();
        await System.Threading.Tasks.Task.Delay(2500);
        Result($"我方气泡已插入（界面日志有 [界面] 插入消息气泡 我方）");

        // ② 通话：host 主叫，join 等被叫
        if (isHost)
        {
            Step("发起语音通话");
            await OnCallClickedAsync();
            for (int i = 0; i < 60 && !_inCall; i++)
                await System.Threading.Tasks.Task.Delay(250);
            Result(_inCall ? $"通话已建立（静音可用={MuteButton.IsEnabled}）" : "通话未建立");
            await System.Threading.Tasks.Task.Delay(4000);
        }
        else
        {
            Step("等待 host 来电");
            for (int i = 0; i < 120 && !_inCall; i++)
                await System.Threading.Tasks.Task.Delay(250);
            Result(_inCall ? "已接通来电" : "没收到来电");
        }

        // ③ 共享
        var realShare = _selfTestReal;
        if (realShare)
        {
            Step("第一次共享屏幕（真实采集 —— 请在系统弹窗里选一个窗口）");
            AppendEngineLog("[自测] 注意：接下来会弹出「选择共享源」，请在其中选一个窗口");
            RenderEngineLog();
        }
        else
        {
            Step("第一次共享屏幕（合成画面，8 秒）");
        }
        await _media!.CallAsync("startScreenShare", realShare
            ? new { audio = false, maxFps = 15, timeoutMs = 45000 }
            : new { synthetic = true, audio = false, maxFps = 15 });
        await System.Threading.Tasks.Task.Delay(realShare ? 15000 : 8000);
        Result(_sharingScreen ? "共享已启动" : "共享未启动（见上面的失败原因）");

        Step("停止共享");
        await _media.CallAsync("stopScreenShare");
        await System.Threading.Tasks.Task.Delay(3000);
        Result("共享已停止");

        // ④ 再共享 —— 这是用户报告的失败点
        if (realShare)
        {
            Step("第二次共享（用户报告的失败点 —— 请再选一次窗口）");
            AppendEngineLog("[自测] 注意：会再次弹出「选择共享源」");
            RenderEngineLog();
            await _media.CallAsync("startScreenShare",
                new { audio = false, maxFps = 15, timeoutMs = 45000 });
            await System.Threading.Tasks.Task.Delay(12000);
        }
        else
        {
            Step("第二次共享（用户报告的失败点）");
            await _media.CallAsync("startScreenShare",
                new { synthetic = true, audio = false, maxFps = 15 });
            await System.Threading.Tasks.Task.Delay(6000);
        }
        Result(_sharingScreen ? "第二次共享已启动" : "第二次共享失败 ★ 这就是用户报的问题");
        await _media.CallAsync("stopScreenShare");
        await System.Threading.Tasks.Task.Delay(2000);

        // ⑤ 共享之后通话是否还在
        Step("检查共享结束后通话是否仍然可用");
        Result($"通话中={_inCall} 静音可用={MuteButton.IsEnabled} " +
               $"聊天可用={MessageInput.IsEnabled} 成员数={_peerCount + 1}");

        // ⑥ 通话中再共享一次（最容易坏的组合）
        Step("通话中再共享一次（最容易坏掉的组合）");
        await _media.CallAsync("startScreenShare",
            new { synthetic = true, audio = false, maxFps = 15 });
        await System.Threading.Tasks.Task.Delay(6000);
        await _media.CallAsync("stopScreenShare");
        await System.Threading.Tasks.Task.Delay(2000);
        Result($"通话中共享后：通话中={_inCall} 静音可用={MuteButton.IsEnabled}");

        AppendEngineLog($"[自测] 全部步骤结束（共 {step} 步）");
        RenderEngineLog();
    }

    private async System.Threading.Tasks.Task OnDenoiseChangedAsync()
    {
        // XAML 里 DenoiseCombo 的 SelectedIndex="1" 会在加载时触发一次
        // SelectionChanged。那时引擎还没起来，必须挡住。
        if (!_uiReady) return;
        if (_media is null || !_media.IsReady) return;
        if (DenoiseCombo.SelectedItem is not ComboBoxItem item) return;
        var level = item.Tag?.ToString() ?? "normal";

        // 通话中切档位需要重建采集链，会短暂中断音频。
        // 这里如实告诉用户，而不是偷偷切了让人以为"卡了一下"。
        if (_inCall)
        {
            AppendEngineLog($"通话中切换降噪 → {level}（将在下次通话生效）");
            RenderEngineLog();
        }
        await _media.CallAsync("setDenoise", new { level });
    }

    /// <summary>
    /// 设置通话状态文字，并**只在真的变化时**记一行日志。
    ///
    /// 【为什么要记】P2 那个 bug（"已接通却写建立连接"）事后无法证明修没修好 ——
    /// 日志里只有"通话中=True"这种判定，没有"界面文字到底变成了什么"。
    /// 这行日志就是"状态怎么变的"唯一证据（符合规矩二：UI 层的变化也要可审计）。
    /// </summary>
    private void SetCallText(string text)
    {
        if (CallStateText.Text == text) return;
        CallStateText.Text = text;
        AppendEngineLog($"[UI] 通话状态文字 → {text}");
        RenderEngineLog();
    }

    /// <summary>
    /// 设置通话**状态位**：圆点颜色、_inCall、按钮可用性。**绝不改文案**。
    ///
    /// 【2026-10-06 修复·UI 问题】原来它在内部调 SetCallText，于是 call-link 分支里
    /// `SetCallText(我在说话/仅收听/已连接…)` 紧接着 `SetCallState(active ? "通话中" : "未通话")`
    /// 会被后一句无条件覆盖 —— "我在说话/仅收听/已连接（未通话）"三种文案**永远显示不出来**。
    /// 现在职责拆开：状态位只归这里，文案只归 SetCallText；调用点顺序是"先 SetCallState、再 SetCallText"。
    /// </summary>
    /// <summary>
    /// 通话中每 5 秒把音频字节数/能量写进日志。
    /// 【为什么】实测：正常通话路径**从不打印**音频字节数（只有共享声音诊断才打），
    /// 于是"语音有没有真的在传"没有任何证据 —— 用户报"语音不可用"时无法判断。
    /// </summary>
    private void StartCallStats()
    {
        if (_callStatsTimer is not null) return;
        _callStatsTimer = new Microsoft.UI.Xaml.DispatcherTimer { Interval = TimeSpan.FromSeconds(5) };
        _callStatsTimer.Tick += async (_, _) =>
        {
            if (_media is null || !_media.IsReady) return;
            try
            {
                // 音频字节数只在页面的 shareAudioDebug 里计算（selfcheck.js），
                // 它会以 share-audio-debug 事件回执；这里只负责"拉一次"。
                await _media.CallRawStringAsync("shareAudioDebug", "{}");
            }
            catch { /* 统计失败不影响通话 */ }
        };
        _callStatsTimer.Start();
    }

    private void StopCallStats()
    {
        if (_callStatsTimer is null) return;
        _callStatsTimer.Stop();
        _callStatsTimer = null;
    }

    private void SetCallState(bool active, bool error = false)
    {
        // 通话开始就开统计、结束就停（用户/测试都能看到音频是否真的在传）
        if (active && !error) StartCallStats(); else StopCallStats();

        CallStateDot.Fill = new SolidColorBrush(error
            ? Microsoft.UI.Colors.OrangeRed
            : active
                ? Windows.UI.Color.FromArgb(255, 0x4C, 0xC2, 0x6A)
                : Microsoft.UI.Colors.Gray);

        // 通话中不允许再点「发起通话」—— 之前漏了这一步，
        // 表现出来就是通话进行中那个按钮还是亮的，点了也没反应，很困惑。
        if (_inCall != active)
        {
            // 记录调用栈：_inCall 曾出现"没点按钮就变 true"的问题，
            // 而 SetCallState 有多处调用，只靠读代码定位不出来。
            //（文案不在这里，所以这行只报状态位本身）
            AppendEngineLog($"[通话态] {_inCall} → {active}  文案={CallStateText.Text}  " +
                            $"栈={Environment.StackTrace.Split('\n')
                                .Skip(2).Take(2).Select(s => s.Trim()).Aggregate((a, b) => a + " ← " + b)}");
        }
        _inCall = active;
        UpdateActionButtons();
    }

    // =======================================================================
    // 引擎事件
    // =======================================================================

    private void OnEngineLog(string line)
    {
        DispatcherQueue.TryEnqueue(() =>
        {
            AppendEngineLog(line);
            RenderEngineLog();
        });
    }

    private void OnMediaEvent(MediaEvent ev)
    {
        // WebMessageReceived 在 UI 线程回调，但统一走 DispatcherQueue 更稳
        DispatcherQueue.TryEnqueue(() => HandleMediaEvent(ev));
    }

    private void HandleMediaEvent(MediaEvent ev)
    {
        var p = ev.Payload;
        switch (ev.Type)
        {
            // ===================================================================
            // 【S1 契约补齐】以下三条以前是"页面在发、C# 没有反应"（审计病根 3 点名）：
            //   chat-state / ice-state / remote-stats —— 事件发了没人接，静默。
            // 现在每条都给**可见的去处**（界面或日志），并保持"事件→渲染"的分层：
            // 这里只做映射，不判断业务（业务状态仍由页面提供）。
            // ===================================================================
            case "link-closed":
            {
                // 【S3 第 2 步】一条链路被显式释放（Link.close 上报，带原因）。
                // 多人场景里"谁走了、为什么走"原本只能靠猜；这条让它可见。
                var closedId = p is { } lc0 ? Str(lc0, "link") : "";
                var closedWhy = p is { } lc1 ? Str(lc1, "reason") : "";
                AppendEngineLog($"[链路] {closedId} 已释放" + (closedWhy.Length > 0 ? $"（{closedWhy}）" : ""));
                break;
            }

            case "chat-state":
            {
                // 页面里有几个对端、聊天通道是否可用。审计原话："3 人通话时第二个对端的
                // 音频状态永不上报" —— 根因就是只查 state.pc（单例）。这条事件带 peers 数组，
                // 正好让界面能反映"多人"的真实情况，而不是只显示一个对端。
                var csReady = p is { } cs0 && GetBool(cs0, "ready");
                var csHasLink = p is { } cs1 && GetBool(cs1, "hasLink");
                var csPeers = new List<string>();
                if (p is { } cs2 && cs2.TryGetProperty("peers", out var pv)
                    && pv.ValueKind == JsonValueKind.Array)
                {
                    foreach (var e in pv.EnumerateArray())
                        if (e.ValueKind == JsonValueKind.String) csPeers.Add(e.GetString() ?? "");
                }
                if (!csReady)
                {
                    // 不通就说清"为什么"，别让用户对着灰着的输入框猜
                    ShowChatTip(csHasLink ? "聊天通道未就绪（链路在，但数据通道没开）"
                                          : "聊天通道未就绪（还没有链路）");
                }
                AppendEngineLog($"[聊天状态] 通道={(csReady ? "可用" : "未就绪")} 链路={(csHasLink ? "有" : "无")} " +
                                $"对端 {csPeers.Count} 个{(csPeers.Count > 0 ? "：" + string.Join("、", csPeers) : "")}");
                break;
            }

            case "ice-state":
            {
                // ICE 连接状态（用户要求"把失败原因说清楚"，页面已给出可读原因，见 call.html 的
                // describeIceFailure）。这里只在**状态真的变化**时记一行，避免刷屏。
                var st = p is { } ie && ie.TryGetProperty("state", out var isv) ? isv.GetString() ?? "" : "";
                if (st.Length == 0) break;
                if (st == _lastIceState) break;
                _lastIceState = st;
                var readable = st switch
                {
                    "checking" => "正在探测连通性…",
                    "connected" or "completed" => "直连/中继已打通",
                    "disconnected" => "连通性中断（可能自行恢复）",
                    "failed" => "打不通（若已配 TURN 仍失败，检查 TURN 地址/端口/账号；未配 TURN 则跨网段可能无法直连）",
                    "closed" => "已关闭",
                    _ => st,
                };
                AppendEngineLog($"[ICE] {st} —— {readable}");
                break;
            }

            case "remote-stats":
            {
                // 对端统计（RTT/丢包）。以前页面在发、C# 丢弃 ⇒ 用户看到的统计永远只有本机值。
                // 只记**变化**（按 link 记），避免每 2 秒一条刷屏。
                var rsLink = p is { } rs0 ? Str(rs0, "from") : "";
                var rsRtt = p is { } rs1 && rs1.TryGetProperty("stats", out var stv)
                            && stv.TryGetProperty("rttMs", out var rv) ? rv.GetDouble() : -1;
                var rsLoss = p is { } rs2 && rs2.TryGetProperty("stats", out var stv2)
                             && stv2.TryGetProperty("lossPct", out var lv2) ? lv2.GetDouble() : -1;
                var key = $"{rsLink}:{rsRtt:0}:{rsLoss:0.0}";
                if (key == _lastRemoteStatsKey) break;
                _lastRemoteStatsKey = key;
                AppendEngineLog($"[对端统计] 链路 {rsLink} 延迟 {(rsRtt < 0 ? "—" : rsRtt.ToString("0") + " ms")} " +
                                $"丢包 {(rsLoss < 0 ? "—" : rsLoss.ToString("0.0") + "%")}");
                break;
            }
            case "stats-debug":
                // 诊断输出：一次性把统计循环的关键状态摊开。
                // 界面已不再提供触发入口，但保留处理逻辑 —— 排查同类问题
                // （"统计为什么不动"）时把按钮加回来即可，不用重写。
                if (p is { } sd)
                {
                    AppendEngineLog("— 统计诊断 —");
                    AppendEngineLog($"  有连接 {GetBool(sd, "hasPc")} / " +
                                    $"在通话 {GetBool(sd, "inCall")} / " +
                                    $"采样 {GetDouble(sd, "tick"):F0} / " +
                                    $"定时器活 {GetBool(sd, "timerAlive")}");
                    AppendEngineLog($"  连接 {Str(sd, "connState")} / " +
                                    $"ICE {Str(sd, "iceState")}");
                    if (sd.TryGetProperty("error", out var derr))
                        AppendEngineLog($"  错误 {derr.GetString()}");
                }
                break;

            case "screen-probe":
                if (p is { } spb)
                {
                    var probeOk = spb.TryGetProperty("ok", out var pov)
                                  && pov.ValueKind == JsonValueKind.True;
                    AppendEngineLog("— 屏幕捕获探针 —");
                    AppendEngineLog($"  API 存在     {GetBool(spb, "hasApi")}");
                    AppendEngineLog($"  结果         {(probeOk ? "成功" : "失败")}");
                    if (probeOk)
                    {
                        AppendEngineLog($"  分辨率       {GetDouble(spb, "width"):F0}×" +
                                        $"{GetDouble(spb, "height"):F0}");
                        AppendEngineLog($"  帧率         {GetDouble(spb, "frameRate"):F0} fps");
                        AppendEngineLog($"  捕获类型     {Str(spb, "displaySurface")}");
                    }
                    else
                    {
                        AppendEngineLog($"  原因         {Str(spb, "error")}");
                    }
                    RenderEngineLog();
                }
                break;

            case "screen-frames":
                // 接收端帧计数：画面真的在传的直接证据。
                // 只有 track 到达是不够的 —— 编码或协商出问题时 track 也会到，
                // 但一帧都解不出来。
                if (p is { } sf)
                {
                    var frameCount = GetDouble(sf, "frames");
                    _lastRemoteFrameCount = frameCount;
                    // 只在帧数跨过阈值时记录，避免刷屏；
                    // 停止共享后这里应当不再出现新的更大数字。
                    if (frameCount >= _lastLoggedFrameCount + 60)
                    {
                        _lastLoggedFrameCount = frameCount;
                        AppendEngineLog($"收到画面帧 {frameCount:F0} 帧 " +
                                        $"({GetDouble(sf, "videoWidth"):F0}×" +
                                        $"{GetDouble(sf, "videoHeight"):F0})");
                        RenderEngineLog();
                    }
                }
                break;

            case "screen-started":
                AppendEngineLog("屏幕共享已开始");
                _screenShare.Started();       // S2：状态机（原 _sharingScreen=true; _screenBusy=true）
                UpdateChatStatus();
                SetScreenViewVisible(true,
                    (p is { } ss
                        ? $"{GetDouble(ss, "width"):F0}×{GetDouble(ss, "height"):F0} · " +
                          $"{GetDouble(ss, "frameRate"):F0}fps" +
                          (GetBool(ss, "hasAudio") ? " · 含系统音频" : " · 无音频")
                        : null));
                break;

            case "screen-stopped":
                AppendEngineLog("屏幕共享已停止");
                _screenShare.Stopped();       // S2：状态机（原两个 bool 一起复位）
                UpdateChatStatus();
                SetScreenViewVisible(false);
                break;

            case "screen-track-ended":
                // 【A2】采集轨自己结束（用户点了"停止共享"或采集源消失）。
                // 页面已按"共享已停止"走完清理；这里把原因记清楚，用于区分
                // "用户主动停" 与 "共享莫名断了"（后者才是用户说的"时好时坏"）。
                AppendEngineLog($"[屏幕共享] 采集轨结束（{(p is { } ste2 ? Str(ste2, "kind") : "?")}）—— 已按停止处理");
                break;

            case "screen-error":
                if (p is { } se)
                {
                    var cancelled = se.TryGetProperty("cancelled", out var cc)
                                    && cc.ValueKind == JsonValueKind.True;
                    var notReadable = se.TryGetProperty("notReadable", out var nr)
                                      && nr.ValueKind == JsonValueKind.True;
                    // S2：无论取消/被占用/其他失败，都走同一条"停"迁移 ——
                    // 以前这里是两处手写复位，漏一处界面就永远停在"请求中"。
                    _screenShare.Stopped();
                    if (cancelled)
                    {
                        AppendEngineLog("屏幕共享被取消（用户没选窗口）");
                    }
                    else if (notReadable)
                    {
                        // 明确告诉用户这是资源占用，并且可以重试 ——
                        // 而不是让他以为功能彻底坏了。
                        // 【2026-10-07】把最可能的原因也写出来：实测最常见的是
                        // **同一台电脑上另一个窗口正在采集屏幕**（Windows/Chromium 对同一采集源互斥）。
                        // 只说"被占用"，用户不知道该去关谁。
                        AppendEngineLog("[屏幕共享失败] 捕获资源被占用（NotReadableError）。" +
                                        "最常见原因：同一台电脑上另一个窗口正在共享屏幕。" +
                                        "已执行清理，可以再点一次「共享屏幕」重试。");
                        // 用临时提示（不会被下面紧跟的 UpdateChatStatus 盖成"聊天已连接"）
                        ShowChatTip("捕获被占用：另一个窗口可能正在共享屏幕，已清理，可重试");
                    }
                    else
                    {
                        AppendEngineLog($"[屏幕共享失败] {Str(se, "message")}");
                    }
                    SetScreenViewVisible(false);
                    // 无论失败原因，界面必须恢复到可操作状态，
                    // 否则用户会以为"功能坏了、点不动了"。
                    UpdateChatStatus();
                }
                break;

            case "link-added":
                // S2：第二条及以后的链路（多人共享的前提）。聊天/音频仍只走主链路。
                if (p is { } la)
                    AppendEngineLog($"[链路] 与 {Str(la, "peer")} 建立第 " +
                                    $"{GetDouble(la, "count"):F0} 条媒体链路");
                break;

            case "links":
                // S1 多链路注册表的可观测证据（见 docs/multi-peer-mesh-design-2026-10-05.md）。
                if (p is { } lk)
                {
                    var cnt = (int)GetDouble(lk, "count");
                    var ids = lk.TryGetProperty("ids", out var idArr) && idArr.ValueKind == JsonValueKind.Array
                        ? string.Join(",", idArr.EnumerateArray().Select(x => x.GetString()))
                        : "";
                    AppendEngineLog($"[链路] 注册表 {cnt} 条{(ids.Length > 0 ? "：" + ids : "")}");
                    // 【S3 第 2 步】注册表一变（有人加入/离开、主链路切换）就是**最该取证的时刻**：
                    // 查一次"泵状态 + share-audio-debug（含当时的链路快照与收发字节数）"。
                    // 挂在 link-added 上是不够的 —— 那条只有"我方主动呼叫"才发，
                    // 而加入方是**应答方**（这次双实例自检就是这么抓到的）。
                    _ = QueryShareAudioDebugAsync();
                }
                break;

            case "signal-debug":
                // 页面的信令字段诊断（页面里的 log() 只写页面 DOM，看不到，所以走 post 通道）。
                // 用途：把"不同对端的 offer/answer 该不该忽略"这个判据建立在事实上，而不是猜。
                if (p is { } sigDbg) AppendEngineLog(Str(sigDbg, "text"));
                break;

            case "link-replaced":
                // 对端重连（新 id）：旧的链路已失效，页面换乘了新对端。如实写一行。
                if (p is { } lr)
                    AppendEngineLog($"[链路] 对端重连：{Str(lr, "from")} → {Str(lr, "to")}，" +
                                    "已换乘新链路（若本端在共享，屏幕轨会重新挂到新链路上）");
                break;

            case "link-busy":
                // 媒体是"一对一"（单 PeerConnection）：房间里第二位及以后的成员拿不到媒体/聊天通道。
                // 页面原来静默忽略，界面看起来"能通话"，实际没通 —— 这里如实说明。
                if (p is { } lb)
                {
                    var busyPeer = Str(lb, "peer");
                    var busyCurrent = Str(lb, "current");
                    AppendEngineLog($"[链路] 当前版本同一时间只与一位对端建立音视频：已连着 {busyCurrent}，" +
                                    $"不与会话 {busyPeer} 另建链路（房间其他成员只能看到在线状态）");
                    ScreenDetailText.Text = $"已连接 {busyCurrent}（同一时间只支持一位对端）";
                    RenderEngineLog();
                }
                break;

            case "link-switched":
                // mesh：主链路的对端离开了，别名切到了还活着的那位（其它人的链路不受影响）。
                if (p is { } lsw)
                    AppendEngineLog($"[链路] 主链路切换为 {Str(lsw, "current")}" +
                                    $"（共 {GetDouble(lsw, "count"):F0} 条）");
                break;

            case "remote-screen-started":
            {
                // 【S6】多人共享：维护"正在共享的对端集合"，按最新一位显示画面（设计稿 D1）。
                var startPeer = p is { } startObj ? Str(startObj, "peer") : "";
                if (startPeer.Length > 0) _sharingPeers.Add(startPeer);
                if (startPeer.Length > 0) _lastSharer = startPeer;
                _viewingRemoteScreen = true;
                AppendEngineLog($"对方开始共享屏幕（{Short(startPeer)}），当前共享者 {_sharingPeers.Count} 位");
                SetScreenViewVisible(true, $"{Short(_lastSharer)} 正在共享");
                break;
            }

            case "remote-screen-switched":
                // 多人同时共享时目前只显示最新一位的画面。这里明确告知，
                // 而不是让画面悄悄换人 —— 静默替换比"只能看一个"更让人困惑。
                if (p is { } swp && Str(swp, "peer") is { Length: > 0 } swPeer) _lastSharer = swPeer;
                AppendEngineLog($"[共享] 有另一位开始共享，画面切换为最新的一位（{Short(_lastSharer)}）" +
                                $"（当前版本只能同时看一路画面）");
                ScreenDetailText.Text = $"已切换为 {Short(_lastSharer)}";
                break;

            case "remote-screen-stopped":
                // 【S6：按共享者集合决定显不显示】
                // 旧版是个布尔：任何一位停止就收起画面 —— 3 人房间里两人在共享时会误收。
                var stopPeer = p is { } stp ? Str(stp, "peer") : "";
                if (stopPeer.Length > 0) _sharingPeers.Remove(stopPeer);
                else _sharingPeers.Clear();

                if (_sharingPeers.Count > 0)
                {
                    // 还有人在共享：保持显示，标题换成还活着的那位
                    var next = (_lastSharer is { Length: > 0 } && _sharingPeers.Contains(_lastSharer))
                        ? _lastSharer : _sharingPeers.First();
                    _lastSharer = next;
                    AppendEngineLog($"[界面] {Short(stopPeer)} 停止共享，还剩 {_sharingPeers.Count} 位，继续显示 {Short(next)}");
                    SetScreenViewVisible(true, $"{Short(next)} 正在共享");
                }
                else if (_viewingRemoteScreen)
                {
                    AppendEngineLog("[界面] 没有人在共享了，隐藏共享显示区" +
                                    $"（ScreenView → Collapsed）");
                    _viewingRemoteScreen = false;
                    _lastSharer = null;
                    SetScreenViewVisible(false);
                }
                else
                {
                    AppendEngineLog("[界面] 收到\"对端停止共享\"，但本端本来就没在看画面，忽略");
                }
                break;

            case "file-start":
                if (p is { } fs)
                {
                    AppendEngineLog($"开始接收 {Str(fs, "name")} " +
                                    $"({GetDouble(fs, "size") / 1024:F1} KB)");
                }
                break;

            case "file-progress":
                if (p is { } fp)
                {
                    var dirn = Str(fp, "direction");
                    var pct = GetDouble(fp, "percent");
                    FileProgressBar.Value = pct;
                    FileProgressPanel.Visibility = Visibility.Visible;
                    FileProgressText.Text =
                        $"{(dirn == "send" ? "发送" : "接收")} {Str(fp, "name")} · {pct:F0}%";
                    if (pct >= 100)
                    {
                        FileProgressPanel.Visibility = Visibility.Collapsed;
                    }
                }
                break;

            case "file-done":
                if (p is { } fd)
                {
                    AppendEngineLog($"文件发送完成: {Str(fd, "name")}");
                    FileProgressPanel.Visibility = Visibility.Collapsed;
                    // 【2026-10-07 修复】把聊天区那张卡片从"发送中…"改成"已发送"。
                    // 以前只隐藏底部进度条、不碰卡片 ⇒ 用户看到"一直发送中"（而对端早已"已接收"）。
                    if (_lastSendFileStatus is not null)
                    {
                        var kb = GetDouble(fd, "size") > 0 ? GetDouble(fd, "size") / 1024.0 : 0;
                        _lastSendFileStatus.Text = kb > 0
                            ? $"{kb:F1} KB · 已发送"
                            : "已发送";
                        _lastSendFileStatus = null;
                    }
                }
                break;

            case "file-received":
                if (p is { } fr) SaveReceivedFile(fr);
                FileProgressPanel.Visibility = Visibility.Collapsed;
                break;

            case "file-error":
                if (p is { } fe)
                {
                    AppendEngineLog($"[文件传输] {Str(fe, "message")}");
                    FileProgressPanel.Visibility = Visibility.Collapsed;
                }
                break;

            case "chat-message":
                // 【自己发的也要显示】
                // 这里原来只处理 from == "peer"，结果自己发的消息在本地完全不出现，
                // 用户看到的是一段"只有对方说话"的残缺记录。
                // 自己发的消息由页面回显一次（from = self），对端那条经 DataChannel
                // 送过去后由对方渲染，所以本地渲染一次正好，不会重复。
                if (p is { } cm)
                {
                    var from = Str(cm, "from");
                    var txt = Str(cm, "text");
                    var isSelf = from != "peer";
                    var peerName = Str(cm, "peerName");
                    if (isSelf)
                    {
                        AppendEngineLog($"[发送消息] {txt}");
                    }
                    else
                    {
                        // P1-b：带上真实说话人。以前这里只写"收到消息"，
                        // 界面气泡上也只显示写死的"对方"，三个人说话分不清谁是谁。
                        AppendEngineLog($"[收到消息] {(peerName.Length > 0 ? peerName : "对方")}: {txt}");
                    }
                    AddMessage(txt, isSelf, DateTime.Now, peerName);
                }
                break;

            case "screen-source":
                // 【P4 诊断】采集到的轨道数：audio=0 说明是**平台没给音频**
                //（Chromium 只在共享"整个屏幕/标签页"时提供系统音频，共享单个窗口没有），
                // audio=1 说明拿到了，那就该检查发送与播放链路。
                if (p is { } sa)
                {
                    var a = sa.TryGetProperty("audio", out var av) && av.TryGetInt32(out var ai) ? ai : 0;
                    var v = sa.TryGetProperty("video", out var vv) && vv.TryGetInt32(out var vi) ? vi : 0;
                    AppendEngineLog($"[共享] 采集轨道：音频={a} 视频={v}" +
                                    (a == 0 ? "  ← 平台未提供系统音频（共享单个窗口时 Chromium 不给）" : ""));
                    ScreenDetailText.Text = a > 0 ? "含系统声音" : "无系统声音（选“整个屏幕”才带声音）";
                }
                break;

            case "screen-audio-received":
                AppendEngineLog("[共享] 对方的屏幕声音已到达并在播放");
                break;

            case "remote-track":
                // 【P4 诊断】页面早就在发这个事件，但这里一直没处理 ——
                // 于是"对端到底有没有把音频轨传过来"在应用日志里查不到，
                // 排查"共享音频完全没声音"时只能靠猜。现在把它记下来。
                AppendEngineLog($"[媒体] 收到远端轨道 kind={Str(p is { } rt ? rt : default, "kind")}");
                RenderEngineLog();
                break;

            case "share-audio-debug": {
                // 把真实字节数显示到界面上（数据来自 WebRTC 的 getStats，不是自己数的）
                AppendEngineLog($"[共享声音] share-audio-debug: {p}");
                RenderEngineLog();
                if (p is { } dbg && dbg.TryGetProperty("info", out var info))
                {
                    var pushed = info.TryGetProperty("pushed", out var pv2) ? pv2.GetInt32() : 0;
                    var links = info.TryGetProperty("links", out var lvCnt) ? lvCnt.GetInt32() : 0;
                    long outB = 0, inB = 0;
                    double inE = 0, outE = 0;
                    if (info.TryGetProperty("perLink", out var pl) && pl.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var l in pl.EnumerateArray())
                        {
                            if (l.TryGetProperty("audioOutBytes", out var ob)) outB += ob.GetInt64();
                            if (l.TryGetProperty("audioInBytes", out var ib)) inB += ib.GetInt64();
                            if (l.TryGetProperty("audioInEnergy", out var ie)) inE += ie.GetDouble();
                            if (l.TryGetProperty("audioOutEnergy", out var oe)) outE += oe.GetDouble();
                        }
                    }
                    // 状态里同时给**字节数**和**音频能量**：字节数会被静音轨骗过，能量不会。
                    ShareAudioStatusText.Text =
                        $"链路 {links} 条 · 推送 {pushed} 块 · 已发 {outB:N0} 字节（能量 {outE:F4}） · " +
                        $"已收 {inB:N0} 字节（能量 {inE:F4}）";
                }
                break;
            }

            case "share-audio-attached":
            case "share-audio-attach-skipped":
            case "share-audio-renegotiated":
                // A1：共享音轨挂到**已存在**链路并完成重协商（用户"先建链、后点开关"的顺序）。
                // 这条是"对端到底能不能收到共享声音"的直接证据。
                if (p is { } sar) AppendEngineLog($"[共享声音] 链路 {Str(sar, "link")} 已重协商带上共享音轨");
                break;

            case "share-audio-push-error":
                // A1：采集到的 PCM 推给页面失败（例如 base64 太大/页面已卸载）。
                // 这条以前完全不存在 —— 失败只 return false，界面上就是"共享了但对方听不到"。
                if (p is { } pae)
                    AppendEngineLog($"[共享声音] 推送失败：{Str(pae, "message")}" +
                                    $"（已推 {(int)GetDouble(pae, "pushed")} 块）");
                break;

            case "share-audio-attach-error":
            case "share-audio-started":
            case "share-audio-stopped":
            case "share-audio-error":
                // 共享电脑声音的事件全部落到应用日志：这是判断"音轨到底加没加上"的唯一依据
                AppendEngineLog($"[共享声音] {Str(p is { } sae ? sae : default, "type")}: {p}");
                RenderEngineLog();
                break;

            case "chat-open":
                AppendEngineLog("聊天通道已打开（端到端加密，不经服务器）");
                _chatReady = true;
                UpdateChatStatus();
                // 【脚本化聊天验证·事件驱动】通道一 open 就发。
                // 不依赖 Loaded 的执行顺序（先后踩过两个坑：跑在建房之前 ⇒ 没对端必超时；
                // 只挂在 --create-room-keep 分支 ⇒ 加入方永远不发送）。
                if (_chatTestText is { Length: > 0 } ctText && !_chatTestSent)
                {
                    _chatTestSent = true;
                    AppendEngineLog($"【聊天测试】通道已就绪，发送：{ctText}");
                    MessageInput.Text = ctText;
                    _ = SendChatAsync().ContinueWith(_ =>
                        AppendEngineLog("【聊天测试】已发送"), TaskScheduler.FromCurrentSynchronizationContext());
                }
                // 【文件测试·事件驱动】通道就绪后发送（以前跑在加入方连接之前，必然超时）
                if (_fileTestPath is { Length: > 0 } && !_fileTestSent)
                {
                    _ = RunFileTestAsync();
                }
                // 【语音测试·事件驱动】通道就绪后拨号（以前跑在加入方连接之前，必然超时）
                if (_callTestMode && !_callTestStarted)
                {
                    _ = RunCallTestAsync();
                }
                break;

            case "chat-closed":
                _chatReady = false;
                UpdateChatStatus();
                break;

            case "chat-debug":
                // ④ 诊断：聊天通道的生命周期（bind/error）—— 用来判断"为什么一直不 open"
                AppendEngineLog($"[聊天诊断] {Str(p ?? default, "step")} " +
                                $"state={Str(p ?? default, "readyState")} " +
                                $"label={Str(p ?? default, "label")}" +
                                (Str(p ?? default, "message").Length > 0
                                 ? $" detail={Str(p ?? default, "message")}" : ""));
                break;

            case "chat-queued":
                // 【④ 修复】聊天通道比"链路 connected"晚约 1 秒才 open。
                // 原来的实现这时直接报错丢弃（用户看到"聊天通道未就绪"就以为坏了），
                // 现在页面侧排队、通道一开就发；这里如实告诉用户"在等"。
                AppendEngineLog($"[聊天] 通道尚未打开，消息已排队（待发 {GetDouble(p ?? default, "pending"):F0} 条）");
                ShowChatTip("通道打开后自动发出…");
                break;

            case "chat-sent":
                AppendEngineLog("[聊天] 已发出");
                break;

            case "chat-error":
                if (p is { } ce2)
                {
                    var m = Str(ce2, "message");
                    AppendEngineLog($"[聊天] {m}");
                    // 同样走"临时提示"：否则会被随后的 UpdateChatStatus 覆盖
                    ShowChatTip(m);
                }
                break;

            case "signal-test":
                // 信号链验证结果：只报客观数字，不作主观判断
                if (p is { } sit)
                {
                    var allOk = sit.TryGetProperty("ok", out var okv)
                                && okv.ValueKind == JsonValueKind.True;
                    AppendEngineLog(allOk ? "信号链验证：通过" : "信号链验证：有偏差");
                    if (sit.TryGetProperty("steps", out var stps)
                        && stps.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var st in stps.EnumerateArray())
                        {
                            var nm = Str(st, "name");
                            var sok = st.TryGetProperty("ok", out var so)
                                      && so.ValueKind == JsonValueKind.True;
                            var det = Str(st, "detail");
                            AppendEngineLog($"  {(sok ? "✓" : "✗")} {nm}" +
                                            (string.IsNullOrEmpty(det) ? "" : $"  — {det}"));
                        }
                    }
                    RenderEngineLog();
                }
                if (_signalTestMode)
                {
                    _signalTestMode = false;
                    _ = System.Threading.Tasks.Task.Delay(400).ContinueWith(_ =>
                        DispatcherQueue.TryEnqueue(() =>
                        {
                            try { Application.Current.Exit(); } catch { }
                        }));
                }
                break;

            case "recording":
                SaveRecording(p ?? default);
                RecordButton.IsEnabled = true;
                break;

            case "mic-level":
                if (p is { } ml) UpdateMicLevel(ml);
                break;

            case "denoise-applied":
                AppendEngineLog($"降噪档位已应用: " +
                    (p is { } da && da.TryGetProperty("level", out var lv)
                        ? lv.GetString() : ""));
                break;

            case "denoise-needs-reconnect":
                AppendEngineLog($"[提示] 降噪档位改动需要重连采集链");
                break;

            case "muted":
                AppendEngineLog(p is { } mu && mu.TryGetProperty("muted", out var m2)
                    && m2.ValueKind == JsonValueKind.True ? "已静音" : "已取消静音");
                break;

            case "engine-loaded":
                EngineStatus.Text = "通话页已加载";
                break;

            // 【S1 契约对账】页面把 window.zxEngine 的方法名报上来，与 BridgeContract 登记比。
            // 缺方法 = C# 调了不存在的名字 ⇒ 以后必然是静默失败，所以这里要**明确**报出来。
            case "engine-methods":
            {
                var methods = new List<string>();
                if (p is { } emPayload && emPayload.TryGetProperty("methods", out var mv)
                    && mv.ValueKind == JsonValueKind.Array)
                {
                    foreach (var m in mv.EnumerateArray())
                        if (m.ValueKind == JsonValueKind.String) methods.Add(m.GetString() ?? "");
                }
                var report = BridgeContract.Compare(methods);
                if (report.Ok)
                {
                    AppendEngineLog("[契约] " + report.ToString());
                }
                else
                {
                    _bridgeContractOk = false;
                    AppendEngineLog("[契约] ✗ 启动对账不通过：" + report.ToString());
                    AppendEngineLog("[契约] 影响：C# 会调用的页面方法若不存在，那个功能**不会报错也不会生效**" +
                                    "（审计病根 3 的静默失败）。请对照 src\\winui-cs\\src\\BridgeContract.cs 修登记或页面。");
                    EngineStatus.Text = "⚠ 桥接契约对账未通过（详见日志）";
                }
                foreach (var m in methods.OrderBy(x => x, StringComparer.Ordinal))
                    AppendEngineLog("    · 页面方法 " + m);
                RenderEngineLog();
                break;
            }

            case "ready":
                EngineStatus.Text = $"就绪 · 端口 {_signalPort} · 房间 {_room}";
                // 【自检】状态轮询**只由"采集泵真的起来了"驱动**（见 StartShareAudioPolling）。
                // 原来它挂在这里、且要求开关为"开"，于是**测试后门起泵时不轮询** ——
                // 结果"泵状态 / share-audio-debug（含链路快照）"这两条关键诊断在双实例自检里看不到。
                // 【共享电脑声音】独立功能：与屏幕共享、麦克风语音互不依赖。
                // 采集来源**只由界面控件决定**（ShareAudioAppCombo 的选中项 /「整个系统」，见 OnShareAudioToggledAsync）。
                // ZX_SHARE_AUDIO 已降级为**纯测试后门**：仅当界面还没有任何选择（下拉未选中）时才生效，
                // 且必须写清"走了测试后门" —— 不许两条入口并存而优先级不明。
                var uiPicked = ShareAudioAppCombo.SelectedItem as ShareSource;
                var backdoorSpec = Environment.GetEnvironmentVariable(AppConfig.ShareAudioBackdoorEnv);
                if (uiPicked is null && !string.IsNullOrEmpty(backdoorSpec))
                {
                    AppendEngineLog($"[共享声音] 界面没有选择来源 → 走测试后门" +
                                    $"（{AppConfig.ShareAudioBackdoorEnv}={backdoorSpec}）");
                    AppendEngineLog($"[共享声音] 采样率 {AppConfig.SharedAudioSampleRate} Hz（来源：采集泵常量）");
                    _ = StartShareAudioAsync(backdoorSpec);
                }
                // 【D4】中栏标题显示**真实房间名**（原来写死"和朋友的语音"）
                RoomTitleText.Text = $"房间 {_room}";
                AppendEngineLog($"[界面] 房间标题 → 房间 {_room}");
                // 界面上显示"我的地址"，对方照这个填就能加入（以前只在日志里，等于没法联机）
                MyAddressText.Text = $"我的地址：{MyAddress()}";
                EnsureLanDiscovery();
                // 先定状态位，再写文案（顺序固定，见 SetCallState 的注释）
                SetCallState(active: false);
                SetCallText("未通话");
                UpdateActionButtons();
                // 【D3】启动直接进自己的房间，不再拿"第一屏"当门槛。
                // 依据用户定的房间方案："开放端口能让别人监测到，自己直接进房间"。
                // 需要联机/换房间时，点左栏「联机 / 换房间」打开那个面板（ShowWelcome）。
                try { BringWindowOnScreen(AppWindow, "主界面"); } catch { }
                break;

            case "joined":
                // 页面侧换地址/房间后的回执（界面上「连接」按钮触发）
                EngineStatus.Text = $"已连接 · 房间 {_room}";
                AppendEngineLog("已连上对方的信令，等对方接听/共享");
                _welcomeWindow?.AppWindow.Hide();   // 联机成功 → 收起第一屏
                try { AppWindow.Show(); BringWindowOnScreen(AppWindow, "主界面"); Activate(); } catch { }
                break;

            case "signal-reconnecting":
                // 【产品级】信令被动断开后正在自动重连：必须让用户看到"正在恢复"，
                // 而不是一个永远不会变好的红点。
                AppendEngineLog($"[信令] 连接已断开，正在自动重连（第 {GetDouble(p ?? default, "attempt"):F0} 次，" +
                                $"{GetDouble(p ?? default, "delayMs"):F0} ms 后）");
                ShowChatTip("信令断开，正在自动重连…");
                SetCallText("正在重连…");
                RenderEngineLog();
                break;

            case "signal-reconnect-gaveup":
                AppendEngineLog("[信令] 连续 10 次自动重连都失败，已放弃 —— " +
                                "请检查对方是否在线/端口是否可达，然后用「联机 / 换房间」重新连接");
                ShowChatTip("信令恢复失败，请重新联机");
                SetCallState(active: false, error: true);
                SetCallText("信令断开");
                RenderEngineLog();
                break;

            case "speaker-test-started":
                AppendEngineLog($"[扬声器测试] 已播放（输出设备={Str(p ?? default, "sinkId")}，" +
                                $"内核支持选择扬声器={GetBool(p ?? default, "canSetSink")}）");
                RenderEngineLog();
                break;

            case "signal-open":
                AppendEngineLog("信令已连接");
                break;

            case "signal-closed":
                AppendEngineLog("信令断开");
                SetCallState(active: false, error: true);
                SetCallText("信令断开");
                // 【产品化】信令断开 = 已经不在房间里了，状态必须跟着变 ——
                // 否则用户看着绿点以为还在线（状态完全由实时事件驱动）。
                if (_rooms.Current is { } scRoom)
                {
                    _roomState[scRoom.Id] = "未连接";
                    RenderRooms();
                }
                break;

            case "mic-opened":
                if (p is { } mo && mo.TryGetProperty("applied", out var ap))
                {
                    AppendEngineLog($"麦克风已开: {Str(ap, "label")}");
                    AppendEngineLog($"  回声消除={GetBool(ap, "echoCancellation")} " +
                                    $"降噪={GetBool(ap, "noiseSuppression")} " +
                                    $"自动增益={GetBool(ap, "autoGainControl")}");
                }
                break;

            case "selfcheck": {
                // 自检结果：显示请求 vs 实际生效的对照。
                // 浏览器接受约束不等于生效，所以这里读的是实际值。
                if (p is { } sc)
                {
                    if (sc.TryGetProperty("error", out var err))
                    {
                        AppendEngineLog($"[自检失败] {err.GetString()}");
                    }
                    else if (sc.TryGetProperty("applied", out var ap2))
                    {
                        var nsOk = GetBool(ap2, "noiseSuppression");
                        var aecOk = GetBool(ap2, "echoCancellation");
                        var agcOk = GetBool(ap2, "autoGainControl");
                        var db = GetDouble(sc, "maxDbF");
                        var hasSignal = db > -80;

                        AppendEngineLog("— 麦克风自检 —");
                        AppendEngineLog($"  设备      {Str(ap2, "label")}");
                        AppendEngineLog($"  回声消除  {(aecOk ? "已生效" : "未生效")}");
                        AppendEngineLog($"  降噪      {(nsOk ? "已生效" : "未生效")}");
                        AppendEngineLog($"  自动增益  {(agcOk ? "已生效" : "未生效")}");
                        AppendEngineLog($"  采样率    {GetDouble(ap2, "sampleRate"):F0} Hz");
                        AppendEngineLog($"  电平      {db:F1} dBFS " +
                                        (hasSignal ? "(有信号)" : "(很安静：说话后重测)"));
                        EngineStatus.Text = nsOk && aecOk
                            ? "降噪与回声消除均已生效"
                            : "部分降噪能力未生效，详见日志";
                    }
                }
                RunSelfCheckButton.IsEnabled = true;
                break;
            }

            case "devices":
                // 【2026-10-06 审计发现的真 bug】页面一直在发 devices，这里却从来没有 case，
                // 唯一会解析它的 OnDevicesReported 全文零调用 ⇒ 设置里麦克风/扬声器下拉**永远是空的**，
                // 而且不报错。补上这一行是"换设备"这个功能唯一入口。
                // 注意必须判空：p 可能没有 payload，直接传 default 会在方法里炸掉 UI 线程。
                if (p is { } devPayload) OnDevicesReported(devPayload);
                else AppendEngineLog("[界面] 收到 devices 但没有 payload（忽略）");
                break;

            case "modules-ready":
                // 【S4 方案 A】页面模块宿主接线完成：把模块清单写进应用日志。
                // 作用与"契约对账"同类 —— 少一个模块（写错名字/工厂抛异常）在这里一眼可见。
                if (p is { } mr && mr.TryGetProperty("modules", out var modArr)
                    && modArr.ValueKind == JsonValueKind.Array)
                {
                    var list = string.Join(",", modArr.EnumerateArray().Select(x => x.GetString()));
                    AppendEngineLog($"[模块] 已就绪 {modArr.GetArrayLength()} 个：{list}");
                }
                break;

            case "modules-disposed":
                // 【S4 方案 A】模块统一释放的结果。关窗时页面 log() 可能已写不进去，
                // 所以释放结果走事件 —— 这是"到底有没有释放"的唯一可靠口径。
                if (p is { } md)
                    AppendEngineLog($"[模块] 已释放 {(int)GetDouble(md, "released")} 个" +
                                    $"{Str(md, "reason")}" +
                                    (md.TryGetProperty("failed", out var fa) && fa.ValueKind == JsonValueKind.Array
                                     && fa.GetArrayLength() > 0
                                     ? $"（失败: {string.Join(",", fa.EnumerateArray().Select(x => x.GetString()))}）"
                                     : ""));
                break;

            case "module-error":
                // 模块起不来 / 释放失败 ⇒ 明确的缺失或泄漏，必须进日志（审计"功能悄悄不生效"的对策）
                if (p is { } me2)
                    AppendEngineLog($"[模块] {Str(me2, "module")} {Str(me2, "stage")} 失败：" +
                                    Str(me2, "message") +
                                    (Str(me2, "stack").Length > 0 ? $"  ← {Str(me2, "stack")}" : ""));
                break;

            case "link-error":
                // 【静默失败的天敌】页面建链失败原来只写页面自己的日志区（隐藏 DOM），
                // 应用日志里什么都看不到 ⇒ 双实例自检只能看到"链路 0 条"却查不到原因。
                // 这条把原因（含前 3 行堆栈）带到应用日志与界面上。
                if (p is { } le)
                    AppendEngineLog($"[链路] 建链失败 peer={Str(le, "peer")}：{Str(le, "message")}" +
                                    (Str(le, "stack").Length > 0 ? $"  ← {Str(le, "stack")}" : ""));
                break;

            case "sdp-summary":
                // 【A1 诊断】SDP 结构摘要：判断共享音轨（第 2 条 audio m-line）有没有协商成发送方向
                if (p is { } sdpSum)
                {
                    var mlStr = sdpSum.TryGetProperty("mlines", out var mlArr) && mlArr.ValueKind == JsonValueKind.Array
                        ? string.Join(" | ", mlArr.EnumerateArray().Select(x => x.GetString()))
                        : "";
                    var dirStr = sdpSum.TryGetProperty("dirs", out var dirArr) && dirArr.ValueKind == JsonValueKind.Array
                        ? string.Join(",", dirArr.EnumerateArray().Select(x => x.GetString()))
                        : "";
                    AppendEngineLog($"[SDP {Str(sdpSum, "tag")}] {mlStr}｜方向 {dirStr}" +
                                    $"｜ssrc {(int)GetDouble(sdpSum, "ssrcCount")}" +
                                    $"｜audio senders {(int)GetDouble(sdpSum, "senders")}");
                }
                break;

            case "link-debug":
                // 【S3 双实例定位】页面在处理 welcome 时上报"我看到几个对端"。
                // 为什么必须存在：页面的 log() 只写隐藏 DOM，**应用日志完全看不到** ——
                // 双实例自检因此无法区分"welcome 没到"与"到了但对端列表是空的"。
                // 这条让自检能直接取证（2026-10-06 就是靠它定位到链路不建的根因）。
                if (p is { } ld)
                {
                    var step = Str(ld, "step");
                    var pcCnt = (int)GetDouble(ld, "peerCount");
                    var peerIds = ld.TryGetProperty("peers", out var pArr) && pArr.ValueKind == JsonValueKind.Array
                        ? string.Join(",", pArr.EnumerateArray().Select(x => x.GetString()))
                        : "";
                    AppendEngineLog($"[链路调试] {step}：对端 {pcCnt} 个{(peerIds.Length > 0 ? "（" + peerIds + "）" : "")}");
                }
                break;

            case "roster-update":
                // 【A3 诊断】页面每次发 roster 的时序：人数、名单、触发原因。
                // 成员数只来自 roster.peers 长度，时序错了界面就会少人 —— 先看清时序再改逻辑。
                if (p is { } ru)
                {
                    var idsRu = ru.TryGetProperty("ids", out var iaRu) && iaRu.ValueKind == JsonValueKind.Array
                        ? string.Join(",", iaRu.EnumerateArray().Select(x => x.GetString()))
                        : "";
                    AppendEngineLog($"[成员诊断] {Str(ru, "why")} count={(int)GetDouble(ru, "count")} " +
                                    $"ids=[{idsRu}]" +
                                    (Str(ru, "joined").Length > 0 ? $" joined={Str(ru, "joined")}" : ""));
                    // 【产品化】收到 welcome = 本端已进入房间 ⇒ 当前房间状态置「已连接」。
                    // 不这么做的话左栏状态点永远是灰的（装饰），用户看不出自己在不在房间里。
                    if (Str(ru, "why") == "welcome" && _rooms.Current is { } wc)
                    {
                        _roomState[wc.Id] = "已连接";
                        RenderRooms();
                    }
                }
                break;

            case "roster":
                if (p is { } ro && ro.TryGetProperty("peers", out var peers)
                    && peers.ValueKind == JsonValueKind.Array)
                {
                    _peerCount = peers.GetArrayLength();
                    // 成员列表改成数据驱动：roster.peers 里本来就带着 id/name，以前只取了人数。
                    _rosterPeers = peers.EnumerateArray()
                        .Select(x => (
                            Id: x.TryGetProperty("id", out var pid) ? (pid.GetString() ?? "") : "",
                            Name: x.TryGetProperty("name", out var pnm) ? (pnm.GetString() ?? "") : ""))
                        .Where(x => x.Id.Length > 0)
                        .Select(x => (x.Id, x.Name.Length > 0 ? x.Name : Short(x.Id)))
                        .ToList();
                    RefreshMemberList();
                    EngineStatus.Text = _peerCount > 0
                        ? $"房间内 {_peerCount} 人，可发起通话"
                        : $"等待他人加入（端口 {_signalPort} / 房间 {_room}）";
                    MemberCountText.Text = $"成员 — {_peerCount + 1}";
                    // 否则又会漏掉某个按钮（共享屏幕就是这么漏掉的）
                    UpdateActionButtons();
                }
                break;

            case "call-state": {
                // 这里**只改显示文案，绝不改 active 状态位**。
                // 状态位由 call-link 独占决定 —— 否则又从"文案事件"里
                // 漏出假状态（本会话出现过：只建链路聊天，界面闪一下"通话中"，
                // 因为 onOffer 里 purpose 对不上时会默认当成 'call'）。
                var st = p is { } cs && cs.TryGetProperty("state", out var s)
                    ? s.GetString() ?? "" : "";
                var reason = p is { } r && r.TryGetProperty("reason", out var rr)
                    ? rr.GetString() ?? "" : "";
                switch (st)
                {
                    case "calling":
                    case "connecting":
                        // 【P2 修复】判断原来是**反的**：写成 `if (_inCall)`，
                        // 于是通话已经接通、页面又发来一次 connecting 时，
                        // 就把 call-link 给出的"通话中"覆盖成"建立连接…" ——
                        // 用户看到"明明在通话，界面还写正在建立"（已截屏证实）。
                        // 正确语义：只有**还没有实际音频流**时才显示呼叫/建立中。
                        if (!_inCall) SetCallText(
                            st == "calling" ? "呼叫中…" : "建立连接…");
                        break;
                    case "idle":
                        // 同理：通话进行中收到 idle（可能来自别的链路）不要清掉通话状态，
                        // 通话状态的唯一权威是 call-link。
                        if (!_inCall)
                        {
                            SetCallText("未通话");
                            ResetStats();
                        }
                        break;
                }
                if (!string.IsNullOrEmpty(reason)) AppendEngineLog($"通话结束: {reason}");
                break;
            }

            case "call-link":
                // **通话状态的唯一权威来源。**
                // 不能拿 connectionState 当通话状态：那条连接同时承载聊天，
                // 两个人一进房间就会连上，界面会误报"通话中"，用户会以为
                // 麦克风被打开了。这里看的是有没有音频轨在实际传输。
                if (p is { } cl)
                {
                    var active = cl.TryGetProperty("active", out var av)
                                 && av.ValueKind == JsonValueKind.True;
                    var sending = GetBool(cl, "sending");
                    var receiving = GetBool(cl, "receiving");
                    var link = Str(cl, "link");

                    AppendEngineLog($"[通话判定] 链路={link} 发音频={sending} " +
                                    $"收音频={receiving} 页面inCall={GetBool(cl, "inCall")} " +
                                    $"共享轨在发={GetBool(cl, "sharedTrackSending")} → 通话中={active}");

                    // 【顺序固定】先 SetCallState（状态位/圆点/按钮），再 SetCallText（具体文案）。
                    // 反过来写就会被"通话中/未通话"这种粗颗粒文案盖掉发送/接收的细分状态。
                    SetCallState(active: active);
                    SetCallText(active
                        ? (sending && receiving ? "通话中"
                           : sending ? "我在说话" : "仅收听")
                        : (link == "connected" ? "已连接（未通话）" : "未通话"));
                    EngineStatus.Text = active
                        ? "通话中"
                        : (link == "connected"
                            ? $"已连接 · 可聊天/传文件（端口 {_signalPort}）"
                            : $"房间 {_room}");
                }
                break;

            case "connection-state": {
                // 只更新链路提示，**不再改通话状态** —— 那是 call-link 的职责。
                var st = p is { } c && c.TryGetProperty("state", out var cs2)
                    ? cs2.GetString() ?? "" : "";
                AppendEngineLog($"链路状态: {st}");
                break;
            }

            case "ice-config": {   // 页面回报 ICE 配置已应用（ack）
                // 页面只报个数与状态，不回显 URL/账号/密码 —— 这里同样只记个数
                var iceCount = 0;
                if (p is { } iceEl)
                {
                    foreach (var iceKey in new[] { "count", "servers", "turn" })
                    {
                        if (iceCount > 0) break;
                        if (iceEl.TryGetProperty(iceKey, out var iceVal)
                            && iceVal.ValueKind == JsonValueKind.Number)
                            iceCount = (int)iceVal.GetDouble();
                    }
                }
                var iceOk = p is { } iceObj
                            && iceObj.TryGetProperty("ok", out var iceOkv)
                            && iceOkv.ValueKind == JsonValueKind.True;
                AppendEngineLog($"[TURN] 页面已应用 ICE 配置：{iceCount} 个服务器（ack，ok={iceOk}）");
                _iceConfigAck?.TrySetResult(iceCount);
                break;
            }

            case "ice-candidate-error": {
                // TURN/STUN 候选出错：说明"中继这座桥"没搭起来，必须能在日志里一眼看到
                if (p is { } iceCr)
                {
                    var cUrl = Str(iceCr, "url");        // 页面已抹掉账密
                    var cCode = GetDouble(iceCr, "code");
                    var cText = Str(iceCr, "text");
                    var cAddr = Str(iceCr, "address");
                    AppendEngineLog($"[TURN] 候选出错：url={cUrl} code={cCode:F0} {cText} " +
                                    $"address={cAddr}（TURN 候选没建起来）");
                }
                else
                {
                    AppendEngineLog("[TURN] 候选出错（无 payload）");
                }
                break;
            }

            case "device-applied":
                // 页面确认设备选择已生效（设置里换麦克风/扬声器的回执）
                AppendEngineLog($"[设备] 页面已应用设备选择：{Str(p ?? default, "label")}" +
                                $"（kind={Str(p ?? default, "kind")}）");
                break;

            case "test-backdoor":
                // 页面侧显式测试后门被用到的痕迹：必须可见，避免"后门悄悄生效"
                AppendEngineLog($"[测试后门] 页面走了测试后门：{Str(p ?? default, "name")}");
                break;

            case "stats":
                if (p is { } sp && sp.TryGetProperty("stats", out var stats))
                    RenderStats(stats);
                break;

            case "call-error":
                AppendEngineLog($"[通话错误] " +
                    (p is { } ce && ce.TryGetProperty("reason", out var cr)
                        ? cr.GetString() : ""));
                break;

            case "engine-error":
                AppendEngineLog($"[引擎错误] " +
                    (p is { } ee && ee.TryGetProperty("message", out var em)
                        ? em.GetString() : ""));
                SetCallState(active: false, error: true);
                SetCallText("出错");
                break;

            case "js-error":
                // 【必带文件】只报行号时，同一个行号在不同模块里无法区分（本项目有 13 个页面模块，
                // 排查"remoteVideoEl is not defined（第 103 行）"这类错误时根本判断不出是哪个文件）。
                // 页面侧已上报 file/col/stack，这里全部记下来。
                AppendEngineLog($"[JS 异常] {Str(p ?? default, "message")} " +
                                $"@ {Str(p ?? default, "file")}:{GetDouble(p ?? default, "line"):F0}" +
                                (Str(p ?? default, "stack").Length > 0
                                 ? $"  ← {Str(p ?? default, "stack")}" : ""));
                break;

            case "js-rejection":
                AppendEngineLog($"[JS 未处理拒绝] {Str(p ?? default, "message")}");
                break;

            case "scriptError":
                AppendEngineLog($"[脚本错误] {ev.Raw}");
                break;

            default:
                AppendEngineLog($"[{ev.Type}] {ev.Raw}");
                break;
        }
        RenderEngineLog();
    }

    /// <summary>
    /// 把 getStats() 的真实质量指标显示出来。
    ///
    /// 这些数字是 W1 的量化验收依据 —— 不靠"感觉不卡"，而是看 RTT/丢包/抖动。
    /// 同一份数据同时更新顶栏与通话栏，避免两处显示不一致。
    /// </summary>
    private void RenderStats(JsonElement stats)
    {
        var rtt = GetDouble(stats, "rttMs");
        var loss = GetDouble(stats, "lossPct");
        var jitter = GetDouble(stats, "jitterMs");
        var kbps = GetDouble(stats, "inboundKbps");
        var path = stats.TryGetProperty("candidateType", out var ct)
            ? ct.GetString() ?? "" : "";

        StatsRtt.Text = $"延迟 {rtt:F0} ms";
        StatsLoss.Text = $"丢包 {loss:F1} %";
        StatsJitter.Text = $"抖动 {jitter:F1} ms";
        StatsBitrate.Text = $"码率 {kbps:F0} kbps";
        StatsPath.Text = $"链路 {PathLabel(path)}";
        // 采样序号：能一眼看出统计是不是真的在跑（数字会持续增长）。
        // 前面踩过坑：连接建立后日志不再更新，光看"延迟 1 ms"分不清
        // 是实时值还是残留值。
        var tick = GetDouble(stats, "tick");
        StatsSamples.Text = $"采样 {tick:F0}";

        // 顶栏用同样的值，保持一处真相
        QualityText.Text = $"延迟 {rtt:F0} ms · 丢包 {loss:F1}% · {PathLabel(path)}";

        // 链路质量着色：延迟与丢包任一超标就变黄/红
        var color = (loss > 5 || rtt > 300) ? Microsoft.UI.Colors.OrangeRed
                  : (loss > 1 || rtt > 150) ? Microsoft.UI.Colors.Goldenrod
                  : Windows.UI.Color.FromArgb(255, 0x4C, 0xC2, 0x6A);
        var brush = new SolidColorBrush(color);
        CallStateDot.Fill = brush;
        QualityDot.Fill = brush;
    }

    private static string PathLabel(string type) => type switch
    {
        "host" => "局域网直连",
        "srflx" => "NAT 打洞",
        "prflx" => "对端反射",
        "relay" => "中继",
        "" => "—",
        _ => type,
    };

    private static string FmtNum(JsonElement el, string name, int digits, string unit)
    {
        if (!el.TryGetProperty(name, out var v) || v.ValueKind != JsonValueKind.Number)
            return "—";
        return v.GetDouble().ToString("F" + digits, CultureInfo.InvariantCulture) + unit;
    }

    private static double GetDouble(JsonElement el, string name)
        => el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number
            ? v.GetDouble() : 0;

    private static bool GetBool(JsonElement el, string name)
        => el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.True;

    private static string Str(JsonElement el, string name)
        => el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? "" : "";

    private void ResetStats()
    {
        StatsRtt.Text = "延迟 —";
        StatsLoss.Text = "丢包 —";
        StatsJitter.Text = "抖动 —";
        StatsBitrate.Text = "码率 —";
        StatsPath.Text = "链路 —";
        StatsSamples.Text = "采样 —";
    }

    // =======================================================================
    // 日志
    // =======================================================================

    // =======================================================================
    // 联机与诊断入口（2026-10-05 新增）
    //   为什么要加：在这之前**加入别人的房间只能靠命令行 `--signal`**，
    //   界面上既看不到"我的地址"，也没有地方填对方地址；日志也只能靠 `--log-file`。
    //   结果就是用户**自己没法测**（"没地方输命令"）。这里把这几件事都做成按钮。
    // =======================================================================

    private LanDiscovery? _lan;

    /// <summary>
    /// 当前**局域网广播里实际使用的**房间名。
    /// 【为什么单独存】不能用 `_room` 的"变化"来判断：`CreateRoomAsync` 会先改 `_room`
    /// 再调 `SetCurrentRoom`，导致 changed 恒 false ⇒ 广播永远停在旧名（实测踩到）。
    /// </summary>
    private string _lanRoomName = "";
    // 用线程定时器而不是 DispatcherQueueTimer：清理超时房间是纯数据操作，
    // 不需要 UI 线程（列表更新由 RoomsChanged 自己切回 UI 线程）。
    private System.Threading.Timer? _lanTick;

    /// <summary>启动局域网自动发现（幂等）。失败只是"发现"没了，不影响手工填地址联机。</summary>
    private void EnsureLanDiscovery()
    {
        if (_lan is not null) return;
        try
        {
            _lan = new LanDiscovery(_signalPort, _room);
            _lan.RoomsChanged += OnRoomsChanged;
            _lan.Start();
            _lanTick = new System.Threading.Timer(_ => _lan?.Tick(), null,
                TimeSpan.FromSeconds(1), TimeSpan.FromSeconds(1));
            _lanRoomName = _room;      // 记录"广播里实际用的房间名"
            AppendEngineLog($"[局域网] 开始自动发现（UDP {LanDiscovery.DiscoveryPort}，房间 {_room}）");
        }
        catch (Exception ex)
        {
            AppendEngineLog($"[局域网] 自动发现启动失败：{ex.Message}（仍可手工填地址连接）");
        }
    }

    /// <summary>
    /// 用**当前**房间名重启局域网广播。
    /// 【为什么必须有】实测（按用户真实流程）：房主建房到「开黑房」后，信令房间已是"开黑房"，
    /// 但局域网广播里仍是旧名 "default" ⇒ 加入方扫到 default、进错房间 ⇒ **自动搜索加入失败**。
    /// 根因是 `LanDiscovery(_signalPort, _room)` 在构造时把房间名固定住了。
    /// 本方法在房间名变化时重建广播，让"扫到的房间" = "真实房间"。
    /// </summary>
    private void RestartLanDiscovery()
    {
        // 【不能因为 `_lan == null` 就 return】实测：房间名在"广播还没启动"时就变了
        //（用户的流程：改名 → 删房间 → 建房，都发生在 Loaded 早期），
        // 早退写法让广播一直停在启动时的旧房间名 ⇒ 加入方扫到旧名、进错房间。
        // 正确做法：无论之前有没有启动过，都按**当前**房间名（重新）启动广播。
        try
        {
            if (_lan is not null) _lan.RoomsChanged -= OnRoomsChanged;
            _lan?.Stop();
            _lan?.Dispose();
        }
        catch { }
        _lan = null;
        try { _lanTick?.Dispose(); } catch { }
        _lanTick = null;
        EnsureLanDiscovery();
        AppendEngineLog($"[局域网] 已按当前房间名启动/重启广播（房间 {_room}）");
        RenderEngineLog();
    }

    // =======================================================================
    // 设置面板（2026-10-05）：设备选择、共享声音、实验开关、诊断入口收在一处，
    // 主界面只留日常操作 —— 用户要的是"像正常软件那样"。
    // =======================================================================

    private bool _suppressDeviceEvents;   // 程序填列表时会触发 SelectionChanged，得挡住

    // =======================================================================
    // 第一屏（2026-10-06 新增）
    //   用户建议："先做个第一屏，让自己起名字、选择自动扫描房间还是输入 IP"。
    //   顺带解决了旧问题：这三个联机控件原来挤在 240px 宽的左栏底部，溢出重叠。
    // =======================================================================

    /// <summary>显示第一屏（起名字 + 扫描房间 / 填地址）。</summary>
    private void ShowWelcome()
    {
        if (WelcomeNameBox.Text.Length == 0) WelcomeNameBox.Text = _selfName;
        WelcomeOverlay.Visibility = Visibility.Visible;   // D3 之后它默认折叠，打开时要显式显示
        // 【2026-10-07】高度从 620 增到 780：加了「方式三：创建房间」之后内容更高，
        // 620 高时最底部的「先进入主界面」被挤出窗口（UIA 里 NOT_IN_TREE，用户点不到）。
        var win = HostOverlay(WelcomeOverlay, "欢迎使用同频", ref _welcomeWindow, 620, 780);

        if (!_welcomeHooked)
        {
            _welcomeHooked = true;
            win.AppWindow.Closing += (_, args) => { args.Cancel = true; win.AppWindow.Hide(); };
        }
        win.AppWindow.Show();
        BringWindowOnScreen(win.AppWindow, "第一屏");
        win.Activate();
    }

    /// <summary>进入主界面：把第一屏上填的名字应用到各处显示。</summary>
    private void EnterMainUi()
    {
        var name = (WelcomeNameBox.Text ?? "").Trim();
        if (name.Length > 0 && name != _selfName)
        {
            _selfName = name;
            SelfNameBox.Text = name;    // 与设置面板保持一致
            _memberSignature = "";      // 名字变了 → 强制重建成员列表
            RefreshMemberList();
        }
        _welcomeWindow?.AppWindow.Hide();       // 独立窗口：收起第一屏
        WelcomeOverlay.Visibility = Visibility.Collapsed;   // 万一它还在主窗口里（没搬走），也要收起
        try
        {
            AppWindow.Show();
            BringWindowOnScreen(AppWindow, "主界面");   // 必须归位：否则会出现在屏幕外
            Activate();
        }
        catch { }
        AppendEngineLog($"进入主界面（名字：{SelfDisplayName()}）");
        RenderEngineLog();
    }

    // =======================================================================
    // 独立窗口（2026-10-06）：设置面板与第一屏都不再是"盖在主界面上的浮层"，
    //   而是真正的 WinUI 窗口。做法是把同一块 UIElement 换个宿主（不重写 XAML），
    //   这样 x:Name 字段、事件、主题都照旧，改动面最小。
    //   关窗一律用 AppWindow.Hide() 而不是 Close()：Close 会把里面的控件销毁，
    //   下次再打开就没有内容了（踩过就麻烦）。用 Closing 事件把关闭改成隐藏。
    // =======================================================================
    private Window? _welcomeWindow;
    private bool _welcomeHooked;      // 第一屏窗口的 Closing 拦截只挂一次

    /// <summary>把浮层从主窗口搬到独立窗口（幂等）。</summary>
    private Window HostOverlay(UIElement overlay, string title, ref Window? slot,
                               int width, int height)
    {
        if (slot is null)
        {
            if (overlay is FrameworkElement fe && fe.Parent is Panel parent)
                parent.Children.Remove(overlay);
            var win = new Window { Title = title, Content = overlay };
            try { win.AppWindow.Resize(new Windows.Graphics.SizeInt32(width, height)); } catch { }
            slot = win;
        }
        return slot;
    }

    private async Task OnOpenSettingsAsync()
    {
        if (SelfNameBox.Text.Length == 0) SelfNameBox.Text = _selfName;
        if (RoomNameBox.Text.Length == 0) RoomNameBox.Text = _room;
        // TURN 配置：以磁盘上读到的 AppConfig 为准填进控件（关设置时写回并下发）
        TurnUrlBox.Text = _config.TurnUrls;
        TurnUserBox.Text = _config.TurnUsername;
        TurnPasswordBox.Password = _config.TurnCredential;
        // 暂时用主窗口内的整页浮层（试过搬进独立窗口，实测内容全黑渲染不出来）。
        SettingsOverlay.Visibility = Visibility.Visible;
        await RefreshDevicesAsync();
        await RefreshShareAudioAppsAsync();   // 打开设置就刷新"声音来源"列表（真数据）
    }

    /// <summary>
    /// 把窗口拉回主显示器工作区中央并置顶。
    ///
    /// **为什么必须有这个**（2026-10-06 自检实测）：
    /// 窗口被 `AppWindow.Hide()` 再 `Show()`，或第一屏/设置（都由浮层搬进独立窗口）创建时，
    /// 窗口会停在**屏幕外**的坐标 —— 自检读到 `WelcomeEnterButton` 在 `(6170, 879)`，
    /// 而屏幕只有 1920 宽。用户看到的现象就是"点了「进入」没反应"（其实是窗口在看不见的地方）。
    /// 所以：任何"把窗口显示出来"的地方，都要走这个函数。
    /// </summary>
    /// <summary>
    /// Win32 屏幕尺寸（真实像素）。用于给 DisplayArea 算出的位置做越界兜底 ——
    /// 实测 DisplayArea 的坐标空间可能与真实屏幕不一致（算出 x≈12550，屏幕只有 1920 宽）。
    /// </summary>
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern int GetSystemMetrics(int nIndex);
    private const int SM_CXSCREEN = 0;
    private const int SM_CYSCREEN = 1;

    /// <summary>静态上下文里也能记日志（走同一个日志缓冲）。</summary>
    private static void AppendLogStatic(string line)
    {
        _staticLogSink?.Invoke(line);
    }

    /// <summary>由实例构造时挂上：把静态日志转给当前窗口实例的 AppendEngineLog。</summary>
    private static Action<string>? _staticLogSink;

    /// <summary>读窗口真实矩形（Win32；只为诊断/夹回屏内用，失败时给 -1）。</summary>
    private static void Win32Rect(Microsoft.UI.Windowing.AppWindow aw, out int l, out int t, out int r, out int b)
    {
        l = t = r = b = -1;
        try
        {
            var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(aw);
            if (hwnd != IntPtr.Zero && GetWindowRect(hwnd, out var rc))
            {
                l = rc.left; t = rc.top; r = rc.right; b = rc.bottom;
            }
        }
        catch { /* 诊断失败不影响功能 */ }
    }

    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    private struct RECT32 { public int left, top, right, bottom; }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool GetWindowRect(IntPtr hWnd, out RECT32 lpRect);

    /// <summary>读窗口真实矩形（Win32；只为诊断/夹回屏内用，失败时给 -1）。</summary>
    private static void Win32Rect(Microsoft.UI.Windowing.AppWindow aw, string what)
    {
        Win32Rect(aw, out int l, out int t, out int r, out int b);
        AppendLogStatic($"[窗口] {what} rect=({l},{t},{r},{b})");
    }

    private static void BringWindowOnScreen(Microsoft.UI.Windowing.AppWindow aw, string what)
    {
        try
        {
            var area = Microsoft.UI.Windowing.DisplayArea.GetFromWindowId(
                aw.Id, Microsoft.UI.Windowing.DisplayAreaFallback.Primary);
            var wa = area.WorkArea;                       // 主显示器工作区（不含任务栏）
            int w = Math.Min(Math.Max(aw.Size.Width, 600), Math.Max(wa.Width - 40, 600));
            int h = Math.Min(Math.Max(aw.Size.Height, 400), Math.Max(wa.Height - 40, 400));
            int x = wa.X + Math.Max(0, (wa.Width - w) / 2);
            int y = wa.Y + Math.Max(0, (wa.Height - h) / 2);

            // 【2026-10-06 兜底】DisplayArea 给的是**它自己的坐标空间**，实测出现过算出来
            // x≈12550 而屏幕只有 1920 宽的情况（窗口被推到屏幕外，用户看到的就是"点了没反应"，
            // 与审计里那条"进不去主界面 = 窗口跑到屏幕外"是同一类现场）。
            // 这里再用 Win32 的真实屏幕尺寸夹一次：只要算出来的位置明显超出屏幕，就夹回可见区。
            // 兜底只在"明显越界"时生效，正常居中路径完全不变。
            int sw = GetSystemMetrics(SM_CXSCREEN), sh = GetSystemMetrics(SM_CYSCREEN);
            // 【诊断】把"算出的位置 / 窗口尺寸 / 移动前的真实矩形"记下来。
            // 2026-10-06 实测出现过"居中日志写 (370,150)，自检却量到 (1009,202)"——
            // 当时 BringWindowOnScreen 是**唯一没有日志的移动点**，只能靠推算，代价很高。
            Win32Rect(aw, out int bx, out int by, out int br, out int bb);
            if (sw > 0 && sh > 0 && (x < -8 || y < -8 || x + w > sw + 8 || y + h > sh + 8))
            {
                x = Math.Max(0, Math.Min(x, Math.Max(0, sw - w)));
                y = Math.Max(0, Math.Min(y, Math.Max(0, sh - h)));
                AppendLogStatic($"[窗口] DisplayArea 算出的位置越界，已夹回屏幕内：{what} → ({x},{y}) {w}x{h}（屏幕 {sw}x{sh}）");
                aw.MoveAndResize(new Windows.Graphics.RectInt32(x, y, w, h));
            }
            else
            {
                aw.MoveAndResize(new Windows.Graphics.RectInt32(x, y, w, h));
            }
            AppendLogStatic($"[窗口] 归位（{what}）：算出 ({x},{y}) {w}x{h}；移动前 rect=({bx},{by},{br},{bb})");
            aw.MoveInZOrderAtTop();
        }
        catch
        {
            // 拿不到显示器信息就算了：窗口归位失败不该影响功能
        }
    }

    // 【S2 已搬走】采集泵字段（原 `private ShareAudioCapture? _shareAudio;`）——
    // 现在泵的生命周期归 SharedAudioService（Capture 属性），窗口不再持有进程句柄。

    /// <summary>
    /// 开始「共享电脑声音」（独立功能，不依赖屏幕共享）。
    /// spec: "loopback"（整个系统）或 "process:&lt;pid&gt;"（指定应用）。
    /// 采集在应用进程（引擎 WASAPI 进程回环），PCM 按 50ms 一块推给页面变成独立音轨。
    /// </summary>
    /// <summary>主动查一次「共享声音/链路」诊断（不轮询；用于链路刚建立等关键时刻）。</summary>
    private async System.Threading.Tasks.Task QueryShareAudioDebugAsync()
    {
        try
        {
            var cap = _sharedAudio.Capture;
            if (cap is not null)
                AppendEngineLog("[共享声音] 泵状态：" +
                    $"块={cap.Blocks} 字节={cap.Bytes} " +
                    $"退出码={cap.ExitCode?.ToString() ?? "运行中"} 错误={cap.LastError ?? "无"}");
            if (_media is not null) await _media.CallAsync("shareAudioDebug");
        }
        catch { /* 诊断失败不影响功能 */ }
    }

    /// <summary>
    /// 启动「共享声音状态」轮询：只在**采集泵真的起来了**的时候跑（幂等）。
    ///
    /// 【为什么单独一个函数】原来这段挂在 `case "ready"` 里、并且要求开关为"开"，
    /// 于是同一个"共享声音"功能有**两条起泵路径**（界面开关 / 测试后门 ZX_SHARE_AUDIO）时，
    /// 只有界面那条会轮询 —— 后门起泵时"泵状态"和"share-audio-debug（含链路快照）"都看不到，
    /// 双实例自检因此拿不到链路证据。现在改由"泵起来了"这件事驱动，两条路径一致。
    ///
    /// 【两处旧债仍遵守】① 可取消（窗口关闭时停）；② 空闲不轮询（泵不在就不发）。
    /// </summary>
    private void StartShareAudioPolling()
    {
        _shareAudioPollCts?.Cancel();
        _shareAudioPollCts = new System.Threading.CancellationTokenSource();
        var pollToken = _shareAudioPollCts.Token;
        _ = Task.Run(async () =>
        {
            while (!pollToken.IsCancellationRequested)
            {
                try { await Task.Delay(AppConfig.ShareAudioStatusPollMs, pollToken); }
                catch (System.OperationCanceledException) { break; }
                if (pollToken.IsCancellationRequested) break;
                DispatcherQueue.TryEnqueue(async () =>
                {
                    try
                    {
                        var cap = _sharedAudio.Capture;
                        if (cap is null) return;      // 泵不在 ⇒ 不刷日志（审计 §五-4 的教训）
                        AppendEngineLog("[共享声音] 泵状态：" +
                            $"块={cap.Blocks} 字节={cap.Bytes} " +
                            $"退出码={cap.ExitCode?.ToString() ?? "运行中"} " +
                            $"错误={cap.LastError ?? "无"}");
                        await _media.CallAsync("shareAudioDebug");
                    }
                    catch { /* 自检失败不影响功能 */ }
                });
            }
        });
    }

    /// <summary>
    /// 开始「共享电脑声音」（独立功能，不依赖屏幕共享）。
    /// spec: "loopback"（整个系统）或 "process:&lt;pid&gt;"（指定应用）。
    /// 【S2】采集泵的生命周期搬到 SharedAudioService；这里只负责"通知页面 + 推 PCM"这两件
    /// 必须走 UI 线程的事（WebView2 套间绑定，见 PushSharedAudioToPageAsync 注释）。
    /// </summary>
    private async Task<bool> StartShareAudioAsync(string spec)
    {
        await _media.CallAsync("startSharedAudio");
        var ok = await _sharedAudio.StartAsync(spec, b64 => PushSharedAudioToPageAsync(b64));
        if (!ok)
        {
            // 采集没起来就别让页面空等：明确收回，避免"音轨在但永远静音"
            try { await _media.CallAsync("stopSharedAudio"); } catch { }
            return false;
        }
        StartShareAudioPolling();     // 泵起来了才开始轮询（两条起泵路径都覆盖）
        return true;
    }

    /// <summary>
    /// 把一块 PCM 推给页面。
    ///
    /// 【必须编组到 UI 线程】WebView2 的 ExecuteScriptAsync 是**套间绑定**的：
    /// 在后台线程调用会抛"应用程序调用一个已为另一线程整理的接口"(RPC_E_WRONG_THREAD)。
    /// 实测后果很隐蔽：音轨协商成功、对端也能收到轨，但**一直是静音的** ——
    /// 因为每一次推送都在后台线程上失败，而异常被采集泵的 catch 吞掉了。
    /// </summary>
    private Task PushSharedAudioToPageAsync(string b64)
    {
        var tcs = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        DispatcherQueue.TryEnqueue(async () =>
        {
            try
            {
                await _media.CallRawStringAsync("pushSharedAudio", b64);
                tcs.TrySetResult(true);
            }
            catch (Exception ex)
            {
                tcs.TrySetException(ex);
            }
        });
        return tcs.Task;
    }

    /// <summary>
    /// 一个"声音来源"条目。显示文字全部来自引擎给出的真实数据（进程名/窗口标题/pid），
    /// 不写死任何应用名 —— 用户机器上装什么就显示什么。
    /// </summary>
    private sealed class ShareSource
    {
        public string Label { get; init; } = "";
        public string Spec { get; init; } = "";      // "loopback" 或 "process:<pid>"
        public override string ToString() => Label;
    }

    /// <summary>
    /// 刷新「共享电脑声音」的来源列表：第一项总是"整个系统"，其余是**当前有音频会话**的进程。
    /// 数据来自 zxprobe apps（引擎走 IAudioSessionManager2 枚举），不是写死的清单。
    /// 【S2】枚举与解析搬到 EngineProbe（MainWindow 不再自己 Process.Start + JsonDocument.Parse）。
    /// </summary>
    private async Task RefreshShareAudioAppsAsync()
    {
        var list = new List<ShareSource>
        {
            new() { Label = "整个系统（所有正在发声的应用）", Spec = "loopback" },
        };
        if (_engineProbe.ToolPath is null)
        {
            ShareAudioStatusText.Text = "找不到 zxprobe.exe，无法枚举声音来源";
        }
        else
        {
            try
            {
                foreach (var app in await _engineProbe.AppsAsync())
                {
                    var label = app.Title.Length > 0
                        ? $"{app.Title}（{app.Name} #{app.Pid}）"
                        : $"{app.Name} #{app.Pid}";
                    if (app.Active) label += " · 正在发声";
                    list.Add(new ShareSource { Label = label, Spec = $"process:{app.Pid}" });
                }
                ShareAudioStatusText.Text = $"声音来源 {list.Count} 项（数据来自 Windows 音频会话枚举）";
            }
            catch (Exception ex)
            {
                ShareAudioStatusText.Text = "枚举声音来源失败：" + ex.Message;
            }
        }
        ShareAudioAppCombo.ItemsSource = list;
        if (ShareAudioAppCombo.SelectedIndex < 0) ShareAudioAppCombo.SelectedIndex = 0;
    }

    /// <summary>开关被拨动：开 → 按选中项开始共享；关 → 停止。</summary>
    private async Task OnShareAudioToggledAsync()
    {
        if (ShareAudioToggle.IsOn)
        {
            if (ShareAudioAppCombo.SelectedItem is not ShareSource src) src = new ShareSource { Spec = "loopback" };
            var ok = await StartShareAudioAsync(src.Spec);
            ShareAudioStatusText.Text = ok ? $"共享中：{src.Label}" : "启动共享失败（看诊断日志）";
        }
        else
        {
            await StopShareAudioAsync();
        }
    }

    /// <summary>停止共享电脑声音：停采集泵 + 让页面停止灌数据（音轨本身保留，见页面注释）。</summary>
    private async Task StopShareAudioAsync()
    {
        try { await _media.CallAsync("stopSharedAudio"); } catch { }
        _sharedAudio.Stop();          // S2：泵的释放归 SharedAudioService（幂等）
        ShareAudioStatusText.Text = "未共享";
        AppendEngineLog("[共享声音] 已停止");
    }

    /// <summary>找引擎探针 —— 实现在 ProbeLocator（S2：不再各处各写一份）。</summary>
    private static string? FindProbeExe() => ProbeLocator.FindProbe();

    private async void OnCloseSettings()
    {
        SettingsOverlay.Visibility = Visibility.Collapsed;   // 整页浮层：隐藏即关闭

        // 【2026-10-07 修复·名字修改完全失效】
        // 实测根因：`SelfDisplayName()` 只读 SelfNameBox 用于**本地显示**，
        // 从不写回 `_selfName`（信令 join 广播给别人的名字）、也不存盘。
        // 后果：自己看到新名字，别人看到的还是旧名；重启后丢失。
        // 现在：写回 _selfName → 存盘 → 重连让信令把新名字广播出去。
        var newName = (SelfNameBox.Text ?? "").Trim();
        var nameChanged = newName.Length > 0 && newName != _selfName;
        if (newName.Length > 0)
        {
            _selfName = newName;
            _config.SelfName = newName;
        }

        // 房间名：以前这里直接改 _room + 标题，既没存盘也没重连（注释还写"重启后生效"，是假的）。
        // 现在统一走 SetCurrentRoom + 存盘 + 重连。
        var newRoom = (RoomNameBox.Text ?? "").Trim();
        var roomChanged = newRoom.Length > 0 && newRoom != _room;
        if (roomChanged && _rooms.Current is { } cur)
        {
            cur.Room = newRoom;
            if (cur.Label.Trim().Length == 0) cur.Label = newRoom;
            SetCurrentRoom(newRoom, "设置里改的房间名");
        }

        // 【跨网段 ICE】收尾：把设置里的 TURN 配置写回磁盘（AppConfig 唯一读写点）并下发给页面。
        // 日志只记条数 —— 账号/密码绝不落日志。
        _config.TurnUrls = (TurnUrlBox.Text ?? "").Trim();
        _config.TurnUsername = (TurnUserBox.Text ?? "").Trim();
        _config.TurnCredential = TurnPasswordBox.Password ?? "";
        var saved = _config.Save();
        RenderRooms();
        AppendEngineLog($"[设置] 已保存：名字「{SelfDisplayName()}」· 房间「{_room}」· " +
                        $"{(saved ? "写盘成功" : "写盘失败：" + _config.LastSaveError)}");
        if (nameChanged) AppendEngineLog($"[设置] 名字已改为「{newName}」—— 正在按新名字重连，让对方看到");
        if (roomChanged) AppendEngineLog($"[设置] 房间名已改为「{newRoom}」—— 正在按新房名重连");
        RenderEngineLog();

        // 名字/房间名变了都要重连：名字与房间名都是 join/start 时广播给别人的参数
        if (nameChanged || roomChanged)
        {
            await ReconnectIdentityAsync();
        }
        _ = PushIceConfigAsync("设置关闭");
    }

    /// <summary>
    /// 按当前的名字与房间名重连（房主走 start、客户端走 join）—— 让**对方**看到新名字/新房名。
    /// 【为什么必须重连】名字与房间名都是信令 join/start 的参数，只改本地变量对方感知不到。
    /// </summary>
    private async Task ReconnectIdentityAsync()
    {
        if (_media is null || !_media.IsReady)
        {
            AppendEngineLog("[设置] 引擎未就绪，名字/房间名将在下次联机时生效");
            RenderEngineLog();
            return;
        }
        var cur = _rooms.Current;
        try
        {
            if (cur is { IsHost: false } && cur.SignalAddress.Trim().Length > 0)
            {
                var url = cur.SignalAddress.Trim();
                if (!url.StartsWith("ws://") && !url.StartsWith("wss://"))
                {
                    url = url.Replace("http://", "").Replace("https://", "").TrimEnd('/');
                    url = "ws://" + url + (url.Contains("/signal") ? "" : "/signal");
                }
                AppendEngineLog($"[设置] 按新名字重连（加入 {url} / 房间 {_room}）");
                await _media.CallAsync("join", new { signalUrl = url, room = _room, selfName = _selfName });
            }
            else
            {
                AppendEngineLog($"[设置] 按新名字重连（本机当房主 / 房间 {_room}）");
                // 房主重连走 join（先收旧再连新），避免同一实例出现两个身份
                await _media.CallAsync("join", new
                {
                    signalUrl = _media.SignalUrl,
                    room = _room,
                    selfName = _selfName,
                });
            }
        }
        catch (Exception ex)
        {
            AppendEngineLog($"[设置] 重连失败：{ex.Message}");
        }
        RenderEngineLog();
    }

    // =======================================================================
    // 跨网段 ICE（TURN 中继）配置下发
    // =======================================================================

    /// <summary>正在等页面回执的下发：页面回 ice-config 事件后完成（值 = 页面收到的 iceServers 条数）。</summary>
    private TaskCompletionSource<int>? _iceConfigAck;

    /// <summary>
    /// 把 AppConfig 里的 TURN 配置下发给页面：走**既有** C#→页面通道
    /// `zxEngine.setIceConfig`（页面侧转发给 window.zxIceConfig）。
    ///
    /// 就绪性用**页面回的 ice-config 事件当 ack**，最多等 AppConfig.IceConfigAckTimeoutMs；
    /// 超时在日志里写清"页面未就绪、TURN 配置未下发"，**不许静默**。
    /// 日志只记条数 —— 账号/密码绝不落日志。
    /// </summary>
    private async Task PushIceConfigAsync(string when)
    {
        var servers = _config.BuildIceServers();
        if (_media is null || !_media.IsReady)
        {
            AppendEngineLog($"[TURN] {when}：引擎未就绪，配置未下发（{_config.TurnSummary}）");
            return;
        }

        var ack = new TaskCompletionSource<int>(TaskCreationOptions.RunContinuationsAsynchronously);
        _iceConfigAck = ack;
        try
        {
            await _media.CallAsync(AppConfig.IceConfigPageMethod, new { iceServers = servers });
            AppendEngineLog($"[TURN] {when}：已下发 ICE 配置（{_config.TurnSummary}）");
        }
        catch (Exception ex)
        {
            AppendEngineLog($"[TURN] {when}：下发失败 {ex.Message}");
            _iceConfigAck = null;
            return;
        }

        var finished = await Task.WhenAny(ack.Task, Task.Delay(AppConfig.IceConfigAckTimeoutMs));
        if (finished != ack.Task)
        {
            AppendEngineLog($"[TURN] 页面未就绪、ICE 配置未下发（等 ack 超时：" +
                            $"{AppConfig.IceConfigAckTimeoutMs} ms，时机={when}）");
        }
        _iceConfigAck = null;
    }

    /// <summary>
    /// 打开设置时刷新麦克风/扬声器下拉。
    ///
    /// 【2026-10-06 修审计真 bug #1 —— 改用引擎枚举】原来只走页面的
    /// `navigator.mediaDevices.enumerateDevices()`。实测（`tmp\smoke-ui.log` 原文样本）：
    /// 启动后**先打开设置**时页面还没拿到麦克风权限，Chromium 只回两条
    /// `{"kind":"audioinput","id":"","label":"(未命名设备)"}` —— 于是下拉是空的占位项，
    /// 用户看到的就是"麦克风/扬声器下拉永远是空的"。而同一台机器上
    /// `zxprobe devices` 能列出 3 个麦 + 6 个播放设备（含 sampleRate/channels/isDefault）。
    ///
    /// 架构上也该这样：`architecture-modules §6` 写的是"引擎只提供设备/会话枚举"，
    /// 页面的 enumerateDevices 要等权限、不可靠。所以：**先问引擎**，拿到真名就填；
    /// 引擎不可用时再退回页面那条路（占位 + 说明），两条路都写日志，不许静默。
    /// </summary>
    private async Task RefreshDevicesAsync()
    {
        if (await RefreshDevicesFromEngineAsync()) return;
        if (_media is null) return;
        try { await _media.CallAsync("listDevices"); }
        catch (Exception ex) { AppendEngineLog($"列设备失败：{ex.Message}"); }
    }

    /// <summary>
    /// 用引擎（zxprobe devices）枚举设备并填下拉。成功返回 true。
    /// 【S2】枚举与 JSON 解析全在 EngineProbe；这里只做"映射成下拉项 + 渲染"。
    /// </summary>
    private async Task<bool> RefreshDevicesFromEngineAsync()
    {
        _devicesFromEngine = false;      // 每次刷新先复位：引擎这次没成，页面那条路还能兜底
        if (_engineProbe.ToolPath is null)
        {
            AppendEngineLog("[设置] 找不到 zxprobe.exe，设备列表退回页面枚举（可能只有占位项）");
            return false;
        }
        try
        {
            var devices = await _engineProbe.DevicesAsync();
            if (devices.Count == 0)
            {
                AppendEngineLog("[设置] 引擎设备枚举返回空列表，退回页面枚举");
                return false;
            }
            var mics = new List<DeviceItem>();
            var spks = new List<DeviceItem>();
            foreach (var d in devices)
            {
                // 标签带上真实参数（采样率/声道/默认），别让用户对着两个同名设备猜
                var label = $"{d.Name} · {d.SampleRate / 1000.0:0.#}kHz/{d.Channels}ch" +
                            (d.IsDefault ? " · 默认" : "");
                var item = new DeviceItem { Id = d.Id, Label = label };
                if (d.Kind == "mic") mics.Add(item);
                else if (d.Kind == "render") spks.Add(item);
            }
            if (mics.Count == 0 && spks.Count == 0)
            {
                AppendEngineLog("[设置] 引擎设备枚举没有可用的麦克风/扬声器，退回页面枚举");
                return false;
            }
            _suppressDeviceEvents = true;
            try
            {
                MicDeviceCombo.ItemsSource = mics;
                SpkDeviceCombo.ItemsSource = spks;
                MicDeviceCombo.DisplayMemberPath = "Label";
                SpkDeviceCombo.DisplayMemberPath = "Label";
            }
            finally { _suppressDeviceEvents = false; }
            AppendEngineLog($"[设置] 设备（引擎枚举）：麦克风 {mics.Count} 个、扬声器 {spks.Count} 个");
            foreach (var m in mics.Take(4)) AppendEngineLog("    · 麦 " + m.Label);
            foreach (var s in spks.Take(4)) AppendEngineLog("    · 扬声器 " + s.Label);
            _devicesFromEngine = true;
            RenderEngineLog();
            return true;
        }
        catch (Exception ex)
        {
            AppendEngineLog("[设置] 引擎设备枚举失败：" + ex.Message + "，退回页面枚举");
            return false;
        }
    }

    /// <summary>
    /// 从探针输出里截出**所有完整的 JSON 数组**。
    ///
    /// 【必须这么做的原因】`zxprobe devices` 的输出不是纯 JSON：
    /// 它是"采集设备：[...]" 再"渲染（播放）设备：[...]"，两组数组中间夹着中文标题
    /// （实测报错：`'0xE6' is invalid after a single JSON value` —— 0xE6 就是中文首字节）。
    /// 所以按配对括号切出每一段数组（考虑字符串里的括号与转义），逐个解析。
    /// </summary>
    // 【S2 已搬走】原来这里有个 200 行的 JsonArrays(text)：它把探针输出的多段 JSON 数组
    // 按配对括号切出来。现在实现在 EngineProbe.JsonArrays（与 Process.Start 一起搬走），
    // MainWindow 里不再有任何 JSON 解析 —— 这正是 S2 的判据之一。

    /// <summary>页面报上来的设备列表 → 填两个下拉框。</summary>
    private void OnDevicesReported(System.Text.Json.JsonElement payload)
    {
        // 引擎枚举已经填过真设备时，**不要**被页面的占位项覆盖（未授权时页面只给空 id 占位）
        if (_devicesFromEngine)
        {
            AppendEngineLog("[设置] 页面设备列表已忽略（当前列表来自引擎枚举，名字更准）");
            return;
        }
        if (!payload.TryGetProperty("devices", out var arr)
            || arr.ValueKind != System.Text.Json.JsonValueKind.Array)
            return;
        var mics = new List<DeviceItem>();
        var spks = new List<DeviceItem>();
        int raw = 0, noId = 0, unknownKind = 0;
        var rawNames = new List<string>();
        foreach (var d in arr.EnumerateArray())
        {
            raw++;
            if (rawNames.Count < 12)
                rawNames.Add(d.GetRawText());
            var kind = d.TryGetProperty("kind", out var k) ? k.GetString() : "";
            var id = d.TryGetProperty("id", out var i) ? i.GetString() : "";
            var label = d.TryGetProperty("label", out var l) ? l.GetString() : "";
            // 【2026-10-06 修审计真 bug #1 的残留】原来这里是 `if (string.IsNullOrEmpty(id)) continue;`
            // —— Chromium 在**未授予麦克风权限**时，enumerateDevices() 给的是**空 deviceId**
            // （label 也空），于是全部条目被丢光，界面显示"麦克风 0 个、扬声器 0 个"，
            // 而同一台机器上引擎枚举明明有 3 个麦 + 6 个播放设备（用户看到的就是"下拉永远是空的"）。
            // 现在：不再按 id 丢弃，改成**如实列出来并在标签上写明"需先授权"**；
            // 选中这种占位项时会明确提示（见 OnDevicePickedAsync），不再静默什么都不做。
            if (string.IsNullOrEmpty(id))
            {
                noId++;
                if (label!.Length == 0) label = "(未命名设备 · 页面尚未获得麦克风权限)";
                else label = label + "（需先授权）";
            }
            if (kind != "audioinput" && kind != "audiooutput") { unknownKind++; continue; }
            var item = new DeviceItem { Id = id ?? "", Label = label ?? (id ?? "设备") };
            if (kind == "audioinput") mics.Add(item);
            else spks.Add(item);
        }
        _suppressDeviceEvents = true;     // 填列表时不要当成"用户选了设备"
        try
        {
            MicDeviceCombo.ItemsSource = mics;
            SpkDeviceCombo.ItemsSource = spks;
            MicDeviceCombo.DisplayMemberPath = "Label";
            SpkDeviceCombo.DisplayMemberPath = "Label";
        }
        finally { _suppressDeviceEvents = false; }
        // 【可诊断】把"页面到底报了什么"记下来：空 id / 未知 kind / 条目数都要能看见，
        // 否则下次又会变成"界面是空的但日志里什么都没有"（这条 bug 藏了两轮就是这个原因）。
        AppendEngineLog($"[设置] 设备：麦克风 {mics.Count} 个、扬声器 {spks.Count} 个" +
                        $"（原始条目 {raw}；空 id {noId}；未知 kind {unknownKind}）");
        if (noId > 0)
        {
            AppendEngineLog("[设置] 有设备的 id 为空 —— 页面此刻尚未获得麦克风权限，" +
                            "列表按占位显示；拨一次「共享电脑声音」或发起通话后会自动刷新。");
        }
        if (raw == 0)
        {
            AppendEngineLog("[设置] 页面报上来的是**空数组** —— 需要查 call.html 的 " +
                            "enumerateDevices 路径（而不是 id 被过滤）。");
        }
        else if (_engineLog.Length < 4000)
        {
            // 只在前几次记原文，避免刷日志
            AppendEngineLog("[设置] devices 原文样本：" + string.Join(" | ", rawNames));
        }
        RenderEngineLog();
    }

    private async Task OnDevicePickedAsync(bool audioInput)
    {
        if (_suppressDeviceEvents || _media is null) return;
        var combo = audioInput ? MicDeviceCombo : SpkDeviceCombo;
        if (combo.SelectedItem is not DeviceItem item) return;
        try
        {
            await _media.CallAsync("setDevice", new
            {
                kind = audioInput ? "audioinput" : "audiooutput",
                id = item.Id,
                label = item.Label,
            });
        }
        catch (Exception ex) { AppendEngineLog($"切换设备失败：{ex.Message}"); }
        RenderEngineLog();
    }

    /// <summary>下拉框里的一项设备。</summary>
    private sealed class DeviceItem
    {
        public string Id { get; init; } = "";
        public string Label { get; init; } = "";
    }

    /// <summary>
    /// 藏掉"从父进程继承来的控制台窗口"。
    /// 应用本身是 WinExe、不会建控制台；但从 .cmd / 命令行拉起时会继承一个，
    /// 用户看到的就是"应用旁边挂着一个黑框"（用户反馈："后台启动时候的 cmd 还在"）。
    /// A 线的 entry.py 也做了同样的事，行为保持一致。
    /// </summary>
    private static void HideInheritedConsole()
    {
        try
        {
            var hwnd = GetConsoleWindow();
            if (hwnd != IntPtr.Zero) ShowWindow(hwnd, SW_HIDE);
        }
        catch { /* 没有控制台就什么都不做 */ }
    }

    [System.Runtime.InteropServices.DllImport("kernel32.dll")]
    private static extern IntPtr GetConsoleWindow();

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    private const int SW_HIDE = 0;

    // =======================================================================
    // 右栏成员列表（2026-10-05 改成数据驱动）
    //   以前这里是写死的三行"朋友A/朋友B/我"：只连上一个人时也照样显示，
    //   看着就像假软件。数据其实早在 roster.peers 里（id + name），只是没渲染。
    // =======================================================================

    private List<(string Id, string Name)> _rosterPeers = new();
    private string _memberSignature = "";     // 内容没变就不重建（这个函数会被高频调用）
    private DateTime _lastMessageDay = DateTime.MinValue;   // 聊天的日期分隔用（C3）

    /// <summary>取名字首字（头像用）。空名字给个问号，不让界面出现空白圆。</summary>
    private static string FirstChar(string name)
        => string.IsNullOrWhiteSpace(name) ? "?" : name.Trim().Substring(0, 1);

    /// <summary>
    /// 对端 id → 显示名。
    /// 【P1-b】以前聊天消息的名字是写死的"对方"：三个人说话全都显示"对方"，分不清谁说的。
    /// 名字来自 roster.peers（真实成员），拿不到才退回短 id。
    /// </summary>
    private string PeerDisplayName(string id)
    {
        if (!string.IsNullOrEmpty(id))
        {
            foreach (var p in _rosterPeers)
                if (p.Id == id && p.Name.Length > 0) return p.Name;
            return Short(id);
        }
        // 没有 id（例如收到文件）：两人房间里就是唯一那位；多人时可能不准，先如实退回"对方"
        return _rosterPeers.Count == 1 && _rosterPeers[0].Name.Length > 0
            ? _rosterPeers[0].Name
            : "对方";
    }

    /// <summary>我的显示名：设置里填的优先，其次命令行 --name，最后才用"我"。</summary>
    private string SelfDisplayName()
    {
        var n = (SelfNameBox.Text ?? "").Trim();
        if (n.Length > 0) return n;
        return string.IsNullOrWhiteSpace(_selfName) ? "我" : _selfName;
    }

    /// <summary>按当前状态重建成员列表。内容没变时直接返回（避免每秒重建）。</summary>
    private void RefreshMemberList()
    {
        var sig = string.Join("|", _rosterPeers.Select(x =>
                      $"{x.Id}:{x.Name}{(_sharingPeers.Contains(x.Id) ? "*" : "")}"))
                  + $"#self:{SelfDisplayName()}{(_sharingScreen ? "*" : "")}";
        if (sig == _memberSignature) return;
        _memberSignature = sig;

        MemberList.Children.Clear();
        foreach (var peer in _rosterPeers)
            MemberList.Children.Add(BuildMemberRow(peer.Name, isSelf: false,
                                                   sharing: _sharingPeers.Contains(peer.Id)));
        MemberList.Children.Add(BuildMemberRow(SelfDisplayName(), isSelf: true,
                                               sharing: _sharingScreen));

        // 【2026-10-07 修复】左下角的头像字与名字以前是 XAML 里写死的"我"，
        // 改名后这里不跟着变（用户实测"左下角的名字已经没改"）。
        if (SelfNameText is not null) SelfNameText.Text = SelfDisplayName();
        if (SelfAvatarText is not null) SelfAvatarText.Text = FirstChar(SelfDisplayName());

        var names = _rosterPeers.Select(x => x.Name).ToList();
        names.Add(SelfDisplayName() + "（我）");
        AppendEngineLog($"[成员] {names.Count} 人：{string.Join("、", names)}");
    }

    /// <summary>画一行成员：首字头像 + 名字 +（共享中）徽标。</summary>
    private Border BuildMemberRow(string name, bool isSelf, bool sharing)
    {
        var initial = string.IsNullOrWhiteSpace(name) ? "?" : name.Trim().Substring(0, 1);
        var avatar = new Grid { Width = 32, Height = 32 };
        avatar.Children.Add(new Microsoft.UI.Xaml.Shapes.Ellipse
        {
            Fill = ThemeBrush(isSelf ? "AccentFillColorDefaultBrush" : "ControlStrongFillColorDefaultBrush"),
        });
        avatar.Children.Add(new TextBlock
        {
            Text = initial,
            FontSize = 13,
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
            Foreground = ThemeBrush(isSelf ? "TextOnAccentFillColorPrimaryBrush" : "TextFillColorPrimaryBrush"),
        });

        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10 };
        row.Children.Add(avatar);
        row.Children.Add(new TextBlock
        {
            Text = name,
            VerticalAlignment = VerticalAlignment.Center,
            Foreground = ThemeBrush(isSelf ? "TextFillColorPrimaryBrush" : "TextFillColorSecondaryBrush"),
        });
        // 自己那行**不再额外加"我"徽标**：
        //   头像首字是名字首字、名字又是"我"、再加个"我"徽标 → 就成了"我 我 我"（用户截图发现）。
        //   自己的标识交给"强调色头像"就够了；名字不是"我"时也不用标注。
        if (sharing)
        {
            row.Children.Add(new Border
            {
                Background = ThemeBrush("AccentFillColorDefaultBrush"),
                CornerRadius = new CornerRadius(3),
                Padding = new Thickness(5, 1, 5, 1),
                VerticalAlignment = VerticalAlignment.Center,
                Child = new TextBlock
                {
                    Text = "共享中",
                    FontSize = 10,
                    Foreground = ThemeBrush("TextOnAccentFillColorPrimaryBrush"),
                },
            });
        }
        return new Border
        {
            Padding = new Thickness(8, 6, 8, 6),
            CornerRadius = new CornerRadius(4),
            Child = row,
        };
    }

    /// <summary>取主题画刷；取不到就退回一个中性色，绝不让界面因为画刷缺失而崩。</summary>
    private static Brush ThemeBrush(string key)
    {
        try
        {
            if (Application.Current.Resources.TryGetValue(key, out var v) && v is Brush b) return b;
        }
        catch { }
        return new SolidColorBrush(Colors.Gray);
    }

    /// <summary>
    /// 生成**邀请串**：`房间名@地址`（跨网段时用户只需把这一串发给对方）。
    /// 为什么要把房间名带上：原来的"复制地址"只给 ip:端口，对方连上后还得手动把房间名改一致，
    /// 否则两人不在同一房间 —— 用户反馈的"很别扭"里就包含这一步。
    /// </summary>
    private string MyInvite() => $"{_room}@{MyAddress()}";

    /// <summary>
    /// 解析对方给的串，兼容三种写法：
    ///   · 邀请串           `房间名@192.168.1.5:45891`
    ///   · 可点击邀请串     `zongxian://room/房间名@192.168.1.5:45891`
    ///   · 纯地址（旧写法） `192.168.1.5:45891` / `ws://…/signal`
    /// 房间名缺省时返回 null（保持当前房间，向后兼容）。
    /// </summary>
    internal static (string address, string? room) ParseInvite(string raw)
    {
        var s = (raw ?? string.Empty).Trim();
        if (s.Length == 0) return ("", null);
        // 可点击形态：zongxian://room/<房间名>@<地址>
        const string scheme = "zongxian://room/";
        if (s.StartsWith(scheme, StringComparison.OrdinalIgnoreCase)) s = s[scheme.Length..];
        // 房间名@地址：只在"@ 之前没有 ://"时才算邀请串（避免把 ws://… 里的 @ 误判）。
        // 【必须取**最后**一个 @】房间名里可能自带 @ —— 实测用例
        // `name@host:1234@1.2.3.4:5678` 用 IndexOf 会被切成 房间=name、
        // 地址=host:1234@1.2.3.4:5678（离线单测当场抓到）。
        var at = s.LastIndexOf('@');
        if (at > 0 && !s[..at].Contains("://"))
        {
            var room = s[..at].Trim();
            var addr = s[(at + 1)..].Trim();
            if (addr.Length > 0) return (addr, room.Length > 0 ? room : null);
        }
        return (s, null);
    }

    /// <summary>房间名输入变化：空名字时禁用「创建房间」，避免用户点了没反应。</summary>
    private void OnCreateRoomNameChanged(object sender, TextChangedEventArgs e)
    {
        if (CreateRoomButton is not null)
        {
            CreateRoomButton.IsEnabled = (CreateRoomNameBox.Text ?? "").Trim().Length > 0;
        }
    }

    /// <summary>
    /// 【产品化】创建房间：本机当房主。
    /// 流程：房间名 → 写进房间列表（房主项）→ 进主界面 → 启动本机房主链路 → 显示邀请串。
    /// 若房间名已存在就**直接切过去**（Add 已做去重），不会加出重复项。
    /// </summary>
    private async Task CreateRoomAsync()
    {
        var name = (CreateRoomNameBox.Text ?? "").Trim();
        if (name.Length == 0)
        {
            AppendEngineLog("[房间] 请先给房间起个名字（例如「周末开黑」）");
            RenderEngineLog();
            return;
        }
        var lobbyName = (WelcomeNameBox.Text ?? "").Trim();
        if (lobbyName.Length > 0)
        {
            _selfName = lobbyName;
            SelfNameBox.Text = lobbyName;
        }

        var room = AddRoomFromConnect(name, "", name);      // 地址为空 = 本机当房主
        _rooms.CurrentId = room.Id;
        // 【必须走统一设置点】以前这里直接写 `_room = name`，跳过了 SetCurrentRoom，
        // 于是"房间名变了要重启局域网广播"这段逻辑**永远不执行** ⇒
        // 广播一直停在启动时的旧房间名 ⇒ 别人扫到旧名、进错房间（用户流程必然踩到）。
        SetCurrentRoom(name, "创建房间");
        _config.Save();
        RenderRooms();

        EnterMainUi();                                      // 进主界面（关掉第一屏）
        AppendEngineLog($"[房间] 已创建「{name}」（本机当房主）");
        RenderEngineLog();

        if (_media is null)
        {
            // 引擎还没就绪：登记"待启动"，链路就绪时自动启动
            _pendingHostRoomId = room.Id;
            AppendEngineLog("[房间] 引擎还没就绪，链路就绪后自动开房");
            RenderEngineLog();
        }
        else
        {
            await StartHostRoomAsync(room);
        }

        // 邀请串：对方粘贴这一串就能进同一个房间（两形态都支持）
        var invite = MyInvite();
        CreateRoomInviteText.Text =
            $"把这一串发给对方（复制界面上的「复制」按钮）：\n{invite}\n" +
            "对方也可以在「方式一」里直接选到本机（同一局域网会自动发现）";
        CreateRoomInviteText.Visibility = Visibility.Visible;
    }

    /// <summary>启动本机房主链路（房间名参与信令 URL，所以每次换名都要重新 start）。</summary>
    private async Task StartHostRoomAsync(RoomEntry room)
    {
        if (_media is null || !_media.IsReady || _media.SignalUrl.Length == 0)
        {
            AppendEngineLog("[房间] 本机信令地址还没就绪，稍后自动重试");
            RenderEngineLog();
            _pendingHostRoomId = room.Id;
            return;
        }
        _roomState[room.Id] = "连接中";
        RenderRooms();
        try
        {
            // 【2026-10-07】房主重连改用 join（本机地址 + 新房名）：
            // join 会**先彻底收掉旧连接再连新的**，避免"同一实例两个身份"
            //（那会让成员数虚高、聊天通道打不开 ⇒ 输入框禁用 ⇒ 打不了字）。
            await _media.CallAsync("join", new
            {
                signalUrl = _media.SignalUrl,
                room = room.Room,
                selfName = _selfName,
            });
            _pendingHostRoomId = null;
        }
        catch (Exception ex)
        {
            _roomState[room.Id] = "未连接";
            AppendEngineLog($"[房间] 开房失败：{ex.Message}");
            RenderEngineLog();
        }
        RenderRooms();
    }


    /// <summary>
    /// 【产品化验收】创建房间：走界面同一条 CreateRoomAsync（名字由命令行给，
    /// 避开"模拟键盘输中文"这个测试工具限制），再核对房间列表与邀请串。
    /// </summary>
    private async System.Threading.Tasks.Task RunCreateRoomTestAsync(string crName)
    {
        CreateRoomNameBox.Text = crName;      // 等价于用户在输入框里打字
        AppendEngineLog($"[建房验收] 开始创建「{crName}」");
        await CreateRoomAsync();
        for (var i = 0; i < 25 && !_rooms.Rooms.Any(r => r.Display == crName); i++)
        {
            await System.Threading.Tasks.Task.Delay(200);
        }
        var inList = _rooms.Rooms.Any(r => r.Display == crName);
        var shown = RoomsList?.Children.Count ?? 0;
        var invite = MyInvite();
        var inviteOk = invite.Contains(crName);
        AppendEngineLog($"[建房验收] 在列表里={inList} 左栏项数={shown} " +
                        $"当前={_rooms.Current?.Display} 邀请串ok={inviteOk}（{invite}）");
        AppendEngineLog(inList && shown > 0 && inviteOk
            ? "[建房验收] 结果 PASS"
            : "[建房验收] 结果 FAIL");
        RenderEngineLog();
        if (_createRoomKeep)
        {
            // 【双实例测试】建房后保持当房主：真实产品行为就是这个（点完创建继续在线），
            // 之前的验收模式跑完就退，导致"房主 1 秒后掉线"，双实例联机根本测不了。
            AppendEngineLog("[建房验收] --create-room-keep：保持当房主，不退出");
            RenderEngineLog();
            // 【顺序修正】等房间真正建好之后再跑聊天测试（以前跑在建房之前必然超时）
            await RunChatTestAsync();
            return;
        }
        await System.Threading.Tasks.Task.Delay(800);
        DispatcherQueue.TryEnqueue(() => { try { Application.Current.Exit(); } catch { } });
    }

    /// <summary>设置里改的房间名**立即生效**：写回当前房间项、存盘；若本机是房主且已联机，则按新房名重连。
    /// 【为什么要重连】房间名参与信令 URL 的 room 参数，不重连的话对方仍停在旧房间 ——
    /// 老实现要求"改完重启"就是这个原因。
    /// </summary>
    /// <summary>
    /// 脚本化聊天验证：等链路就绪后自动发一条消息。
    /// 【2026-10-07 修正执行时机】以前它跑在 InitializeMediaAsync 里（建房**之前**），
    /// 那时房间里还没有对端 ⇒ 15 秒等待必然超时（"链路未就绪，放弃"）。
    /// 之前能"通过"只是因为残留实例误打误撞连了上来 —— 环境清干净后必然失败。
    /// 现在改为在 **建房/加入之后** 调用（见 Loaded 里的调用点）。
    /// </summary>
    /// <summary>
    /// 脚本化文件传输验证：通道就绪后把指定文件发出去。
    /// 【2026-10-07 修正执行时机】以前等待循环跑在 InitializeMediaAsync 里（加入方还没连上）
    /// ⇒ 15 秒必然超时 ⇒ **文件传输从来没被真正验证过**。现在由 chat-open 事件触发。
    /// </summary>
    /// <summary>
    /// 脚本化语音通话验证：通道就绪后自动发起通话（走真实的呼叫按钮逻辑 OnCallClickedAsync）。
    /// 【2026-10-07 修正执行时机】以前等待循环跑在 InitializeMediaAsync 里（加入方还没连上）
    /// ⇒ 必然超时 ⇒ **语音通话从来没被真正验证过**（这是"语音不能用"长期没被发现的原因）。
    /// </summary>
    private async System.Threading.Tasks.Task RunCallTestAsync()
    {
        if (!_callTestMode || _callTestStarted) return;
        _callTestStarted = true;
        AppendEngineLog("【通话测试】通道已就绪，发起通话");
        RenderEngineLog();
        await OnCallClickedAsync();
    }

    private async System.Threading.Tasks.Task RunFileTestAsync()
    {
        if (_fileTestPath is not { Length: > 0 } path || _fileTestSent) return;
        _fileTestSent = true;
        try
        {
            var bytes = System.IO.File.ReadAllBytes(path);
            var name = System.IO.Path.GetFileName(path);
            AppendEngineLog($"【文件测试】发送 {name}（{bytes.Length} 字节）");
            RenderEngineLog();
            if (_media is null) return;
            await _media.CallAsync("sendFile", new
            {
                name,
                mime = "application/octet-stream",
                dataBase64 = Convert.ToBase64String(bytes),
            });
        }
        catch (Exception ex)
        {
            AppendEngineLog($"【文件测试】读取/发送失败: {ex.Message}");
            RenderEngineLog();
        }
    }

    private async System.Threading.Tasks.Task RunChatTestAsync()
    {
        if (_chatTestText is null) return;
        // 事件驱动那条路（chat-open 时发送）可能已经发过了 —— 不要重复发
        if (_chatTestSent) return;
        AppendEngineLog($"【聊天测试】等待链路建立后发送: {_chatTestText}");
        RenderEngineLog();
        for (int i = 0; i < 60 && !_chatReady; i++)
        {
            await System.Threading.Tasks.Task.Delay(250);
        }
        if (_chatReady)
        {
            AppendEngineLog("【聊天测试】链路已就绪，发送中…");
            MessageInput.Text = _chatTestText;
            await SendChatAsync();
            AppendEngineLog("【聊天测试】已发送");
        }
        else
        {
            AppendEngineLog("【聊天测试】链路未就绪（聊天未连接），放弃");
        }
        RenderEngineLog();
    }

    private async Task ApplyRoomNameAsync()
    {
        var name = (RoomNameBox.Text ?? "").Trim();
        if (name.Length == 0 || name == _room) return;
        SetCurrentRoom(name, "设置里改的房间名");
        if (_rooms.Current is { } cur)
        {
            cur.Room = name;
            if (cur.Label.Trim().Length == 0) cur.Label = name;
            _config.Save();
            RenderRooms();
        }
        AppendEngineLog($"[房间] 房间名已改为「{name}」");
        RenderEngineLog();
        if (_media is not null && _media.IsReady && _rooms.Current is { IsHost: true })
        {
            try
            {
                // 房主重连走 join（先收旧再连新）
                await _media.CallAsync("join", new
                {
                    signalUrl = _media.SignalUrl,
                    room = name,
                    selfName = _selfName,
                });
                AppendEngineLog("[房间] 已按新房名重连（对方要用同一个房间名才能互相看到）");
            }
            catch (Exception ex)
            {
                AppendEngineLog($"[房间] 按新房名重连失败：{ex.Message}");
            }
            RenderEngineLog();
        }
    }

    /// <summary>
    /// 【唯一设置点】切换当前房间名：改 _room + 更新顶栏标题 + 同步设置里的输入框 + 存盘。
    /// 【为什么要有它】实测踩到：3 条路径（选局域网房间 / 点房间行 / 设置里改名）各改各的
    /// _room，只有"信令连上"那条会写 RoomTitleText ⇒ 用户看到的标题是旧房间名，
    /// 报"房间名不同步"。集中到这里，新增路径也不会再漏。
    /// </summary>
    private void SetCurrentRoom(string name, string reason)
    {
        var n = (name ?? "").Trim();
        if (n.Length == 0) return;
        var changed = n != _room;
        _room = n;
        if (RoomTitleText is not null) RoomTitleText.Text = $"房间 {n}";
        if (RoomNameBox is not null && RoomNameBox.Text != n) RoomNameBox.Text = n;
        // 【关键】广播里的房间名与目标不同就重启 —— 用 _lanRoomName 判断（不用 _room 的"变化"，
        // 因为 CreateRoomAsync 会先改 _room，导致 changed 恒 false）。
        if (_lanRoomName != n) RestartLanDiscovery();
        if (changed)
        {
            AppendEngineLog($"[房间] 当前房间改为「{n}」（{reason}）");
            RenderEngineLog();
        }
    }

    /// <summary>
    /// 渲染左栏房间列表：每项 = 名字 + 主机地址 + 状态点；列表为空时显示空态提示。
    /// 【为什么整块重建】房间数很少（个位数），重建比增量更新简单可靠，
    /// 而且状态点随连接状态变化，重建能保证界面与数据始终一致。
    /// </summary>
    private void RenderRooms()
    {
        if (RoomsList is null) return;
        RoomsList.Children.Clear();
        foreach (var r in _rooms.Rooms)
        {
            var state = _roomState.TryGetValue(r.Id, out var st) ? st : "未连接";
            var row = new Grid { Padding = new Thickness(6, 5, 4, 5) };
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

            var dot = new Microsoft.UI.Xaml.Shapes.Ellipse
            {
                Width = 8,
                Height = 8,
                VerticalAlignment = VerticalAlignment.Center,
                Fill = new Microsoft.UI.Xaml.Media.SolidColorBrush(
                    state == "已连接" ? Microsoft.UI.Colors.MediumSeaGreen
                    : state == "连接中" ? Microsoft.UI.Colors.Goldenrod
                    : Microsoft.UI.Colors.Gray),
            };
            Grid.SetColumn(dot, 0);
            row.Children.Add(dot);

            var text = new StackPanel { Margin = new Thickness(8, 0, 0, 0) };
            text.Children.Add(new TextBlock
            {
                Text = r.Display,
                FontSize = 13,
                TextTrimming = TextTrimming.CharacterEllipsis,
                FontWeight = r.Id == _rooms.CurrentId
                    ? Microsoft.UI.Text.FontWeights.SemiBold
                    : Microsoft.UI.Text.FontWeights.Normal,
            });
            text.Children.Add(new TextBlock
            {
                Text = $"{r.Subtitle} · {state}",
                FontSize = 11,
                TextTrimming = TextTrimming.CharacterEllipsis,
                Foreground = (Microsoft.UI.Xaml.Media.Brush)Application.Current.Resources["TextFillColorTertiaryBrush"],
            });
            Grid.SetColumn(text, 1);
            row.Children.Add(text);

            // 【可管理】改名 / 删除：放在行右侧的小图标按钮。
            // 不能只有"加"没有"改/删" —— 错项会永远留在列表里。
            var acts = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 2, VerticalAlignment = VerticalAlignment.Center };
            var renameBtn = new Button
            {
                Content = new FontIcon { Glyph = "\uE70F", FontSize = 11 },
                Padding = new Thickness(5, 2, 5, 2), Tag = r.Id, Background = new Microsoft.UI.Xaml.Media.SolidColorBrush(Microsoft.UI.Colors.Transparent), BorderThickness = new Thickness(0),
            };
            renameBtn.Click += async (_, _) => await RenameRoomAsync((string)renameBtn.Tag);
            ToolTipService.SetToolTip(renameBtn, "重命名这个房间");
            acts.Children.Add(renameBtn);
            var delBtn = new Button
            {
                Content = new FontIcon { Glyph = "\uE74D", FontSize = 11 },
                Padding = new Thickness(5, 2, 5, 2), Tag = r.Id, Background = new Microsoft.UI.Xaml.Media.SolidColorBrush(Microsoft.UI.Colors.Transparent), BorderThickness = new Thickness(0),
            };
            delBtn.Click += async (_, _) => await DeleteRoomAsync((string)delBtn.Tag);
            ToolTipService.SetToolTip(delBtn, "从列表里删除这个房间（不会退出已建立的连接）");
            acts.Children.Add(delBtn);
            Grid.SetColumn(acts, 2);
            row.Children.Add(acts);

            var btn = new Button
            {
                Content = row,
                HorizontalAlignment = HorizontalAlignment.Stretch,
                HorizontalContentAlignment = HorizontalAlignment.Stretch,
                Background = new Microsoft.UI.Xaml.Media.SolidColorBrush(Microsoft.UI.Colors.Transparent),
                BorderThickness = new Thickness(0),
                Padding = new Thickness(2),
                Tag = r.Id,
            };
            btn.Click += async (_, _) => await OnRoomRowClickedAsync((string)btn.Tag);
            ToolTipService.SetToolTip(btn, r.IsHost ? "本机当房主" : $"加入 {r.SignalAddress}");
            RoomsList.Children.Add(btn);
        }
        if (SidebarRoomHint is not null)
        {
            SidebarRoomHint.Visibility = _rooms.Rooms.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        }
        if (SidebarRoomsHeader is not null)
        {
            SidebarRoomsHeader.Text = $"房间（{_rooms.Rooms.Count}）";
        }
    }

    /// <summary>重命名房间：用输入对话框（ContentDialog），取消则不改。</summary>
    private async Task RenameRoomAsync(string roomId, string? presetName = null)
    {
        var r = _rooms.Rooms.FirstOrDefault(x => x.Id == roomId);
        if (r is null) return;
        // presetName 用于 --rename-room-test 走同一段代码（跳过弹窗）；界面按钮不传，走弹窗
        var name = (presetName ?? "").Trim();
        if (name.Length == 0)
        {
            var box = new TextBox { Text = r.Display, PlaceholderText = "新的房间名" };
            var dlg = new ContentDialog
            {
                Title = "重命名房间",
                Content = box,
                PrimaryButtonText = "保存",
                CloseButtonText = "取消",
                XamlRoot = RootGrid.XamlRoot,
            };
            var res = await dlg.ShowAsync();
            if (res != ContentDialogResult.Primary) return;
            name = (box.Text ?? "").Trim();
            if (name.Length == 0) return;
        }
        _rooms.Rename(roomId, name);
        if (r.Id == _rooms.CurrentId)
        {
            // 【2026-10-07 修复】之前只改了本地列表，**没重连** ——
            // 所以对方还停在旧房间名、成员列表也永远不会变（用户报"改名不刷新"）。
            // 现在：改名当前房间 = 房间名变了 = 信令房间变了 ⇒ 必须用 start 重连。
            r.Room = name;
            SetCurrentRoom(name, "房间行重命名");
            await ReconnectHostRoomAsync(name);
        }
        else
        {
            // 改的不是当前房间：只改记录（不会影响当前连接）
            _config.Save();
            RenderRooms();
            AppendEngineLog($"[房间] 已重命名「{r.Display}」（非当前房间，不影响当前连接）");
            RenderEngineLog();
        }
    }

    /// <summary>
    /// 以新房名重连当前房主房间（改名/切换都会走到这里）。
    /// 重连会关掉旧信令连接、重新 start —— 对方自然从旧房间消失、进新房名才看得见。
    /// </summary>
    private async Task ReconnectHostRoomAsync(string roomName)
    {
        _config.Save();
        RenderRooms();
        if (_media is not null && _media.IsReady && _rooms.Current is { IsHost: true })
        {
            try
            {
                // 同上：房主重连走 join（先收旧再连新）
                await _media.CallAsync("join", new
                {
                    signalUrl = _media.SignalUrl,
                    room = roomName,
                    selfName = _selfName,
                });
                AppendEngineLog($"[房间] 已按新房名「{roomName}」重连（对方要进同一房间名才能再看到你）");
            }
            catch (Exception ex)
            {
                AppendEngineLog($"[房间] 按新房名重连失败：{ex.Message}");
            }
            RenderEngineLog();
        }
    }

    /// <summary>
    /// 从列表里删除房间。
    /// 【2026-10-07 修复】之前注释写"只删记录、不断开连接" —— 用户实测"删了房间别人还在房间"。
    /// 现在：删的是**当前房间** = 用户明确要离开这个房间 ⇒ 先 stopAll（关信令、清链路、清成员），
    /// 再把当前房间切到列表里剩下的第一个。删非当前房间仍只删记录（不影响已建立的连接）。
    /// </summary>
    private async Task DeleteRoomAsync(string roomId)
    {
        var r = _rooms.Rooms.FirstOrDefault(x => x.Id == roomId);
        if (r is null) return;
        var wasCurrent = r.Id == _rooms.CurrentId;
        if (wasCurrent)
        {
            // 先停当前连接：stopAll 会关 ws、清链路、停采集，对方收到 peer-left 从房间消失
            if (_media is not null && _media.IsReady)
            {
                try { await _media.CallAsync("stopAll"); }
                catch (Exception ex) { AppendEngineLog($"[房间] 停止当前连接失败：{ex.Message}"); }
            }
        }
        _rooms.Remove(roomId);
        _roomState.Remove(roomId);
        if (wasCurrent)
        {
            // 清空成员列表（stopAll 后 roster 应已空；这里强制刷新一次，界面立即正确）
            _rosterPeers = new List<(string Id, string Name)>();
            _peerCount = 0;
            RefreshMemberList();
            SetCallState(active: false);
            SetCallText("未连接");
            // 若列表里还有房间，切到第一个并（如果是房主）重新开
            if (_rooms.Current is { } next)
            {
                SetCurrentRoom(next.Room, "删除了当前房间，切到列表第一个");
                _roomState[next.Id] = next.IsHost ? "连接中" : "未连接";
                if (next.IsHost) await ReconnectHostRoomAsync(next.Room);
            }
            else
            {
                SetCurrentRoom("default", "房间列表已空");
            }
        }
        _config.Save();
        RenderRooms();
        AppendEngineLog($"[房间] 已删除「{r.Display}」" +
                        (wasCurrent ? "（已断开连接，成员已清空）" : "（非当前房间，不影响连接）"));
        RenderEngineLog();
    }

    /// <summary>点某个房间 = 切过去。房主项走 start、加入项走 join（都是已有能力）。</summary>
    private async Task OnRoomRowClickedAsync(string roomId)
    {
        var r = _rooms.Rooms.FirstOrDefault(x => x.Id == roomId);
        if (r is null) return;
        _rooms.CurrentId = r.Id;
        SetCurrentRoom(r.Room, "点了这个房间");
        _config.Save();                       // 选中项也要记住（下次启动回到这里）

        if (_media is null || !_media.IsReady)
        {
            AppendEngineLog("[房间] 引擎还没就绪，稍后再试");
            RenderEngineLog();
            return;
        }
        _roomState[r.Id] = "连接中";
        RenderRooms();
        try
        {
            if (r.IsHost)
            {
                // 本机当房主：房间名参与信令 URL，必须用 start 重连
                AppendEngineLog($"[房间] 切到「{r.Display}」（本机当房主，房间名 {r.Room}）");
                // 同上：房主切房间走 join（先收旧再连新，避免双重身份）
                await _media.CallAsync("join", new
                {
                    signalUrl = _media.SignalUrl,
                    room = r.Room,
                    selfName = _selfName,
                });
            }
            else
            {
                var url = r.SignalAddress.Trim();
                if (!url.StartsWith("ws://") && !url.StartsWith("wss://"))
                {
                    url = url.Replace("http://", "").Replace("https://", "").TrimEnd('/');
                    url = "ws://" + url + (url.Contains("/signal") ? "" : "/signal");
                }
                AppendEngineLog($"[房间] 切到「{r.Display}」（加入 {url}，房间名 {r.Room}）");
                await _media.CallAsync("join", new { signalUrl = url, room = r.Room, selfName = _selfName });
            }
            _rooms.LastConnectedAddress = r.SignalAddress;
            _config.Save();
        }
        catch (Exception ex)
        {
            _roomState[r.Id] = "未连接";
            AppendEngineLog($"[房间] 切换失败：{ex.Message}");
            RenderEngineLog();
        }
        RenderRooms();
    }

    /// <summary>
    /// 把当前房间加进房间列表（"新建 / 加入房间"成功后调用）。
    /// label/address/room 都来自用户输入或局域网发现结果，不写死任何值。
    /// </summary>
    private RoomEntry AddRoomFromConnect(string label, string address, string room)
    {
        var e = _rooms.Add(label, address, room);
        _roomState[e.Id] = "连接中";
        _config.Save();
        RenderRooms();
        return e;
    }

    /// <summary>我的地址（局域网 IP + 信令端口）。对方照这个填。</summary>
    private string MyAddress()
    {
        string ip = "127.0.0.1";
        try
        {
            foreach (var a in System.Net.Dns.GetHostAddresses(System.Net.Dns.GetHostName()))
            {
                if (a.AddressFamily == System.Net.Sockets.AddressFamily.InterNetwork
                    && !System.Net.IPAddress.IsLoopback(a))
                {
                    ip = a.ToString();
                    break;
                }
            }
        }
        catch { /* 拿不到就退回回环地址，界面照常可用 */ }
        return $"{ip}:{_signalPort}";
    }

    /// <summary>「连接」：用界面上的地址加入对方房间（内部走页面的 zxEngine.join）。</summary>
    private async Task OnJoinClickedAsync()
    {
        var raw = (PeerAddressBox.Text ?? "").Trim();
        if (raw.Length == 0)
        {
            AppendEngineLog("请先填对方地址，或直接粘贴对方给的邀请串（形如 房间名@192.168.1.5:45891）");
            RenderEngineLog();
            return;
        }
        // 【A4】邀请串里带房间名：先把它写进房间输入框，再走统一的连接流程
        var parsed = ParseInvite(raw);
        if (parsed.room is not null && parsed.room != _room)
        {
            RoomNameBox.Text = parsed.room;
            AppendEngineLog($"[邀请] 对方房间是「{parsed.room}」，已自动填入房间名");
            RenderEngineLog();
            raw = parsed.address;
        }
        else if (parsed.room is null && raw != parsed.address)
        {
            raw = parsed.address;
        }
        // 三种写法都收：192.168.1.5:45891 / ws://…:45891/signal / http://…
        var url = raw;
        if (!url.StartsWith("ws://") && !url.StartsWith("wss://"))
        {
            url = url.Replace("http://", "").Replace("https://", "").TrimEnd('/');
            url = "ws://" + url + (url.Contains("/signal") ? "" : "/signal");
        }
        AppendEngineLog($"正在连接 {url} …");
        RenderEngineLog();
        if (_media is null)
        {
            AppendEngineLog("引擎还没就绪，稍等一下再点连接");
            RenderEngineLog();
            return;
        }
        try
        {
            // 【产品化】加入成功后把这个房间记进房间列表（多房间闭环）
            var label = parsed.room is { Length: > 0 } pr && pr != "default" ? pr : raw;
            AddRoomFromConnect(label, raw, _room);
            await _media.CallAsync("join", new { signalUrl = url, room = _room, selfName = _selfName });
        }
        catch (Exception ex)
        {
            AppendEngineLog($"连接请求失败：{ex.Message}");
            RenderEngineLog();
        }
    }

    /// <summary>「加入选中」：直接用自动发现到的房间联机，不用输地址。</summary>
    /// <summary>
    /// 【③ 修复】一键加入扫描到的房间：优先用下拉框里已选中的，没有就用扫描结果的第一个。
    /// 【为什么需要】实测：扫描明明发现了房间，但"加入选中的房间"要求先选中一项，
    /// 用户没选中时只看到"先选一个"，就判断"自动扫描加入不可用"。
    /// </summary>
    private async Task OnJoinScannedAsync()
    {
        if (RoomListCombo.SelectedItem is LanRoom sel)
        {
            await OnJoinSelectedRoomAsync();
            return;
        }
        if (_allRooms.Count == 0)
        {
            AppendEngineLog("[局域网] 没扫到房间 —— 确认对方也开着应用、且在同一网段" +
                            "（跨网段需要组网工具或 TURN，也可让对方把邀请串发给你，粘到「方式二」）");
            RenderEngineLog();
            return;
        }
        // 用扫描结果的第一个（列表是按最近发现排序的）
        // 【2026-10-07 修复·"一键加入"加错房间】
        // 以前这里先设 SelectedItem、日志也打 _allRooms[0]，但真正加入的
        // OnJoinSelectedRoomAsync() 读的是 SelectedItem —— 而扫描列表**每秒刷新**
        // （OnRoomsChanged 重建列表）会把选中项重置/丢弃
        // ⇒ **日志显示 A、实际加入 B**（实测：日志"开黑房"、结果房间=dggdf）。
        // 现在把目标房间显式传下去，不再依赖 UI 选中状态。
        var target = _allRooms[0];
        RoomListCombo.SelectedItem = target;
        AppendEngineLog($"[局域网] 一键加入：{target.Display}");
        RenderEngineLog();
        await OnJoinSelectedRoomAsync(target);
    }

    /// <param name="explicitRoom">
    /// 明确要加入的房间。**必须由"一键加入"等入口显式传入** ——
    /// 不能只读 `RoomListCombo.SelectedItem`：扫描列表每秒刷新会重置选中项（竞态）。
    /// 为 null 时才回退到读 UI 选中项（用户手点列表项的场景）。
    /// </param>
    private async Task OnJoinSelectedRoomAsync(LanRoom? explicitRoom = null)
    {
        if ((explicitRoom ?? RoomListCombo.SelectedItem) is not LanRoom room)
        {
            AppendEngineLog("先在左边的列表里选一个房间（对方开着应用就会自动出现）");
            RenderEngineLog();
            return;
        }
        // 【A+C】局域网发现的房间带着**房间名**：必须一起用上，
        // 否则本端停在旧房间名，连上去也看不到对方（A4 之前就是这个毛病）。
        if (!string.IsNullOrEmpty(room.Room) && room.Room != _room)
        {
            SetCurrentRoom(room.Room, "局域网发现该房间名");
        }
        PeerAddressBox.Text = room.Address;
        // 记进房间列表（多房间闭环）：以后一点就能回来，不用再扫
        AddRoomFromConnect(
            string.IsNullOrEmpty(room.Room) ? room.Name : $"{room.Room}·{room.Name}",
            room.Address, string.IsNullOrEmpty(room.Room) ? _room : room.Room);
        AppendEngineLog($"加入 {room.Display}");
        await OnJoinClickedAsync();
    }

    /// <summary>局域网发现的**完整**房间列表（筛选只改控件内容，不动它）。</summary>
    private IReadOnlyList<LanRoom> _allRooms = new List<LanRoom>();

    /// <summary>
    /// 按关键字筛选局域网房间并刷新下拉框。
    /// 【A4】实测：三台机器同房间时标签几乎一样（同一台开发机机器名相同，只有端口不同），
    /// 没有筛选就得逐个点开看端口。筛选口径不写死任何名字：房间/机器/IP/端口 合并文本包含即命中。
    /// </summary>
    private void ApplyRoomFilter(string? keyword)
    {
        var key = (keyword ?? string.Empty).Trim().ToLowerInvariant();
        var list = _allRooms;
        if (key.Length > 0)
            list = _allRooms.Where(r => r.SearchText.Contains(key)).ToList();

        var selected = (RoomListCombo.SelectedItem as LanRoom)?.Id;
        RoomListCombo.ItemsSource = list;
        if (selected is not null)
        {
            var again = list.FirstOrDefault(r => r.Id == selected);
            if (again is not null) RoomListCombo.SelectedItem = again;
        }
        // 筛选后只剩一个候选就自动选中 —— 让"输入即加入"只需一次回车
        if (list.Count == 1) RoomListCombo.SelectedItem = list[0];
    }

    private void OnRoomSearchLoaded(object sender, RoutedEventArgs e)
    {
        ApplyRoomFilter(RoomListCombo.Text);       // 初始按当前文本（通常为空）显示全部
    }

    /// <summary>回车提交：有筛选结果就直接加入（键盘用户不必用鼠标）。</summary>
    private async void OnRoomSearchSubmitted(ComboBox sender, ComboBoxTextSubmittedEventArgs args)
    {
        ApplyRoomFilter(sender.Text);
        if (sender.SelectedItem is LanRoom)
            await OnJoinSelectedRoomAsync();
        else if (_allRooms.Count == 0)
            AppendEngineLog("[局域网] 现在没扫描到房间（对方开着应用、且在同一网段才会出现）");
        else
            AppendEngineLog($"[局域网] 关键字「{sender.Text}」没匹配到房间（共 {_allRooms.Count} 个）");
        RenderEngineLog();
    }

    /// <summary>局域网房间列表变了：更新下拉框 + 记一行日志（日志是排查"没发现对方"的第一手材料）。</summary>
    private void OnRoomsChanged(IReadOnlyList<LanRoom> rooms)
    {
        // 事件可能来自接收线程，必须切回 UI 线程再碰控件
        if (!DispatcherQueue.TryEnqueue(() =>
        {
            _allRooms = rooms;
            var selected = (RoomListCombo.SelectedItem as LanRoom)?.Id;
            ApplyRoomFilter(RoomListCombo.Text);
            _ = selected;      // 选中项的恢复由 ApplyRoomFilter 处理（它按 Id 找）
            if (selected is not null)
            {
                var again = rooms.FirstOrDefault(r => r.Id == selected);
                if (again is not null) RoomListCombo.SelectedItem = again;
            }
            var text = rooms.Count == 0
                ? "（暂无）"
                : string.Join("；", rooms.Select(r => r.Display));
            // 左栏那行提示也一起更新（用户反映"扫描到房间"没有反馈）
            SidebarRoomHint.Text = rooms.Count == 0
                ? "还没有房间\n让对方也打开应用，或点下面的「联机」手工填地址"
                : $"扫描到 {rooms.Count} 个房间\n在「联机」里选一个加入";
            AppendEngineLog($"[局域网] 发现 {rooms.Count} 个房间：{text}");
            RenderEngineLog();
        })) { /* 窗口正在关：忽略 */ }
    }

    /// <summary>「音频能力」：在子进程里探「单个应用音频」，结论写进界面的日志区。</summary>
    private async Task OnAudioCapabilityClickedAsync()
    {
        AppendEngineLog("【音频能力】正在子进程里探测「单个应用音频」…（崩了也不影响主程序）");
        RenderEngineLog();
        var r = await System.Threading.Tasks.Task.Run(() => AppAudioProbe.Probe(force: true));
        AppendEngineLog($"【音频能力】{r.ShortText}");
        if (r.Reason.Length > 0) AppendEngineLog($"【音频能力】原因：{r.Reason}");
        RenderEngineLog();
    }

    /// <summary>
    /// 打开资源管理器到某个目录。
    /// 【S2 说明·为什么这里允许 Process.Start】它是**纯 UI 动作**（用户点了"打开日志文件夹"），
    /// 不是"业务里偷偷起子进程"：没有解析输出、没有等待、失败只记一行日志。
    /// S2 的判据针对的是后者（netstat/netsh/zxprobe 那三处已搬到 NetworkProbe/EngineProbe）。
    /// </summary>
    private void OpenFolder(string path, string what)
    {
        try
        {
            System.Diagnostics.Process.Start("explorer.exe", $"\"{path}\"");
            AppendEngineLog($"已打开{what}文件夹：{path}");
        }
        catch (Exception ex) { AppendEngineLog($"打开{what}文件夹失败：{ex.Message}"); }
        RenderEngineLog();
    }

    /// <summary>
    /// 打开 Windows 的"声音设置"页（用户要换设备时的引导）。
    /// 【S2 说明】同上：纯 UI 动作，起的是系统设置 URI，不是业务子进程。
    /// </summary>
    private void OpenSoundSettings(string what)
    {
        try
        {
            System.Diagnostics.Process.Start(
                new System.Diagnostics.ProcessStartInfo("ms-settings:sound") { UseShellExecute = true });
            AppendEngineLog($"已打开系统声音设置（在那里选{what}设备）");
        }
        catch (Exception ex) { AppendEngineLog($"打开声音设置失败：{ex.Message}"); }
        RenderEngineLog();
    }

    private void CopyToClipboard(string text, string what)
    {
        try
        {
            var dp = new Windows.ApplicationModel.DataTransfer.DataPackage();
            dp.SetText(text ?? "");
            Windows.ApplicationModel.DataTransfer.Clipboard.SetContent(dp);
            AppendEngineLog($"已复制{what}（{ (text ?? "").Length } 字），直接粘给我即可");
        }
        catch (Exception ex) { AppendEngineLog($"复制失败：{ex.Message}"); }
        RenderEngineLog();
    }

    /// <summary>
    /// 诊断入口：**先把不含探测结果的内容复制走**，再到后台探「单个应用音频」，
    /// 探测回来后补一份完整文本（并写日志）。
    ///
    /// 【2026-10-06 修复·UI 问题】原来这里直接 `AppAudioProbe.Probe()`：它在 UI 线程
    /// 起子进程并 `WaitForExit(15000)`，点一下「复制诊断信息」窗口就冻结最多 15 秒。
    /// 现在 UI 线程只拼字符串与写剪贴板，探测一律 Task.Run —— **绝不允许 UI 线程同步等待**。
    /// </summary>
    private void CopyDiagnostics()
    {
        CopyToClipboard(BuildDiagnosticsText(cap: null), "诊断信息（音频能力探测在后台进行）");

        var probe = System.Threading.Tasks.Task.Run(() => AppAudioProbe.Probe());
        probe.ContinueWith(t =>
        {
            // 回 UI 线程：剪贴板与日志都只能在 UI 线程碰
            DispatcherQueue.TryEnqueue(() =>
            {
                if (t.IsFaulted)
                {
                    AppendEngineLog($"[诊断] 音频能力探测异常：" +
                                    $"{t.Exception?.GetBaseException().Message}");
                    CopyToClipboard(BuildDiagnosticsText(cap: null), "诊断信息（音频能力探测失败）");
                    return;
                }
                var r = t.Result;
                AppendEngineLog($"[诊断] 单个应用音频: {r.ShortText}" +
                                (r.Reason.Length > 0 ? $"（{r.Reason}）" : ""));
                CopyToClipboard(BuildDiagnosticsText(r), "诊断信息（含音频能力）");
            });
        });
    }

    /// <summary>拼诊断文本。cap 为 null = 探测还没回来/失败，如实写"探测中"，不写假结论。</summary>
    private string BuildDiagnosticsText(AppAudioProbeResult? cap)
    {
        var sb = new System.Text.StringBuilder();
        sb.AppendLine($"同频 诊断  {DateTime.Now:yyyy-MM-dd HH:mm:ss}");
        sb.AppendLine($"我的地址: {MyAddress()}");
        sb.AppendLine($"信令端口: {_signalPort}    房间: {_room}");
        sb.AppendLine($"原生视频开关: {(NativeVideoToggle.IsChecked == true ? "开" : "关")}");
        sb.AppendLine(cap is null
            ? "单个应用音频: 探测中…（后台子进程，不阻塞界面）"
            : $"单个应用音频: {cap.Verdict}（{cap.Reason}）");
        sb.AppendLine($"跨网段中继: {_config.TurnSummary}");   // 只报条数，不回显账号/密码
        sb.AppendLine($"程序目录: {AppContext.BaseDirectory}");
        sb.AppendLine("---- 最近日志 ----");
        sb.Append(_engineLog.ToString());
        return sb.ToString();
    }

    private void AppendEngineLog(string line)
    {
        var ts = DateTime.Now.ToString("HH:mm:ss");
        // 只保留最近 120 行：日志区可视高度有限，保留太多只会让有用信息
        // 滚出视野（"日志看起来不更新"往往就是这个原因，不是真没写）。
        _engineLog.AppendLine($"{ts}  {line}");
        var lines = _engineLog.ToString().Split('\n');
        if (lines.Length > 120)
        {
            _engineLog.Clear();
            _engineLog.Append(string.Join('\n', lines[^121..]));
        }
    }

    private void RenderEngineLog()
    {
        EngineLogView.Text = _engineLog.ToString();
        // 滚到底，保证最新一行可见
        try { EngineLogScroll.ChangeView(null, EngineLogScroll.ScrollableHeight, null); }
        catch { /* 布局尚未完成时忽略 */ }

        // 落了 log-file 就同步写盘。每次追加都写：日志量小（最多 120 行），
        // 换来的好处是"任何时刻都能拿到最新日志"，不必等退出。
        if (_logFilePath is { Length: > 0 })
        {
            try { System.IO.File.WriteAllText(_logFilePath, _engineLog.ToString()); }
            catch { /* 写不了就算了，不能因为日志把应用搞崩 */ }
        }
    }

    // =======================================================================
    // 窗口
    // =======================================================================

    /// <summary>
    /// 应用主题。
    ///
    /// 跟随系统明暗主题，**不强制暗色**。
    /// 配色全部走语义画刷（见 App.xaml 与 ui-design-guideline §5），
    /// 所以跟随主题时不需要改任何颜色代码。
    /// </summary>
    private void ApplyTheme()
    {
        if (Content is FrameworkElement root)
        {
            root.RequestedTheme = ElementTheme.Default;
        }

        if (AppWindow?.TitleBar is { } titleBar)
        {
            titleBar.PreferredTheme = TitleBarTheme.UseDefaultAppMode;
        }

        // Mica：WinUI 3 原生窗口背景材质，随明暗主题变化。
        // Windows 10 上不可用，失败静默降级（语义画刷已保证底色正确）。
        try
        {
            SystemBackdrop = new MicaBackdrop();
        }
        catch { }
    }

    private void ConfigureWindow()
    {
        if (AppWindow is not { } appWindow) return;

        appWindow.Resize(new SizeInt32(InitialWidth, InitialHeight));

        if (appWindow.Presenter is OverlappedPresenter presenter)
        {
            presenter.PreferredMinimumWidth = MinWidth;
            presenter.PreferredMinimumHeight = MinHeight;
        }

        // 多实例测试时不要都叠在屏幕中央：按端口偏移，便于同时看两个窗口。
        // 【2026-10-06 修】原来直接用 `(端口 - 默认端口) * 60`：默认端口 45890 在中间，
        // 端口差 ±200 以上就把窗口推到屏幕外（实测端口 46101 → offsetX=12660 → 窗口在 x=13030，
        // 而屏幕只有 1920 宽；用户看到的就是"点了没反应"）。
        // 现在只取"第几个额外实例"（0,1,2…）再错位，并把总偏移夹在**工作区内**：
        // 无论端口是多少，窗口都一定可见，而且仍不会完全重叠。
        CenterOnWorkArea(appWindow,
            xOffset: ((_signalPort - AppConfig.DefaultSignalPort) % 6) * AppConfig.MultiInstanceWindowOffsetX);
    }

    /// <summary>
    /// 在主显示器的工作区居中。
    /// 用工作区（不含任务栏）而不是整屏，否则窗口会被任务栏压住一截。
    /// </summary>
    private static void CenterOnWorkArea(AppWindow appWindow, int xOffset = 0)
    {
        var area = DisplayArea.GetFromWindowId(appWindow.Id, DisplayAreaFallback.Primary);
        if (area is null) return;

        var work = area.WorkArea;
        // 偏移也要夹在**工作区内**：多实例错位的目的是"别完全重叠"，不是"推到屏幕外"。
        int maxOffset = Math.Max(0, (work.Width - InitialWidth) / 2 - 8);
        int clampedOffset = Math.Max(-maxOffset, Math.Min(xOffset, maxOffset));
        var x = work.X + (work.Width - InitialWidth) / 2 + clampedOffset;
        var y = work.Y + (work.Height - InitialHeight) / 2;
        // 【诊断】实测出现过窗口被放到 (12550,150)（屏幕只有 1920 宽）⇒ 先把"算出来的数"记下来，
        // 下次再出现就能一眼看出是 workArea 本身异常、还是偏移量异常。
        AppendLogStatic($"[窗口] 居中：workArea={work.X},{work.Y} {work.Width}x{work.Height} " +
                        $"offsetX={xOffset}（夹到 {clampedOffset}）→ ({x},{y})；" +
                        $"屏幕 {GetSystemMetrics(SM_CXSCREEN)}x{GetSystemMetrics(SM_CYSCREEN)}");
        appWindow.Move(new PointInt32(x, y));
    }
}
