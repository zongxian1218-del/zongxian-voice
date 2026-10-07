/*
BridgeContract.cs —— C#↔页面 契约的**唯一登记处**（S1，2026-10-06）。

【为什么要有这个文件】审计病根 3：两侧是裸字符串，双向都有对不上 ——
  · 页面发、C# 无 handler：`devices` / `ice-state` / `remote-stats` / `chat-state` / `engine-methods`
  · C# 有 handler、页面从不发：`link-replaced` / `link-busy` / `share-audio-attach-skipped` / `stats-debug`
  · 本可兜底的 `listMethods()` 自己没人调用
后果是"事件发了没人接"和"调了不存在的方法"都**静默**：界面看着正常，功能不通。

【本文件的职责】
  1. 声明 C# **必须能处理**的页面事件集合（EventsIn）
  2. 声明 C# **会调用**的页面方法集合（MethodsOut）
  3. 提供启动握手：拉 listMethods() 与本地集合对账，打印**明确差异**（不是只打日志）
  4. 提供「这个事件有没有真 handler」的查询（用于把「有人接」与「接了但空实现」分开）
登记与实现是否一致，还有 build/run-checks.py 的静态守卫在把关（防「改了页面忘了登记」）。
*/
using System;
using System.Collections.Generic;
using System.Linq;

namespace ZongxianVoice;

internal static class BridgeContract
{
    /// <summary>页面 → C#：页面会发出的**全部**事件 type（页面上 post/sendSignal 的 type 字面量）。</summary>
    public static readonly IReadOnlyList<string> EventsIn = new[]
    {
        "answer", "call-error", "call-link", "call-state", "chat",
        "chat-closed", "chat-error", "chat-message", "chat-open", "chat-state",
        "chat-debug",   // ④ 诊断：DataChannel 的 readyState / error（通道为何不 open）
        "chat-queued",  // 产品化：通道还没 open 时消息进队列（DataChannel 比链路晚约 1 秒）
        "chat-sent",    // 产品化：消息真的发出去了（界面据此确认，而不是"看起来发了"）
        "connection-state", "denoise-applied", "denoise-needs-reconnect", "device-applied", "devices",
        "engine-error", "engine-loaded", "engine-methods", "file-done", "file-end",
        "file-error", "file-meta", "file-progress", "file-received", "file-start",
        "ice", "ice-candidate-error", "ice-config", "ice-config-state", "ice-state",
        "incoming-call", "joined", "js-error", "js-rejection", "link-added",
        "link-switched", "links", "mic-level", "mic-opened", "muted",
        "link-closed",   // S3 第 2 步：一条链路被显式释放（含原因），便于诊断多人场景
        "link-debug",    // S3：welcome 处理时上报"看到几个对端"（双实例自检的取证口径）
        "sdp-summary",   // A1：SDP 结构摘要（m-line/方向/ssrc），用于判断共享音轨是否协商成发送
        "link-error",    // S3：建链失败的原因（原来只 log 到隐藏 DOM ⇒ 应用日志查不到）
        "modules-ready", // S4：页面模块宿主接线完成，上报模块清单（自检口径）
        "module-error",  // S4：某个模块 init/dispose 失败（不许静默）
        "modules-disposed", // S4：模块统一释放结果（关窗时页面 log 可能写不进去，必须走事件）
        "offer", "pcm", "ready", "recording", "remote-screen-started",
        "remote-screen-stopped", "remote-screen-switched", "remote-stats", "remote-track", "roster",
        "screen-audio-received", "screen-error", "screen-frames", "screen-probe", "screen-source",
        "screen-started", "screen-stop-notify", "screen-stopped", "screen-track-ended", "scriptError", "selfcheck",
        "share-audio-attach-error", "share-audio-attached", "share-audio-debug", "share-audio-error", "share-audio-started",
        "share-audio-push-error",   // A1：把采集 PCM 推给页面失败（原来只 return false，静默）
        "share-audio-renegotiated", // A1：共享音轨挂到已存在链路并完成重协商
        "hangup",          // 页面 → 信令服务器：挂断通知对端（不经过 C#）
        "share-audio-stopped", "signal-closed",
        "signal-reconnecting",      // 信令被动断开后正在自动重连（第 N 次，退避 N 秒）
        "signal-reconnect-gaveup",  // 连续 10 次重连失败，已放弃（要用户介入）
        "signal-debug", "signal-open", "signal-test",
        "speaker-test-started",     // 设置面板「测试扬声器」已播放提示音（诊断）
        "roster-update", // A3：每次发 roster 的时序诊断（人数/名单/触发原因）
        "stats", "stats-debug", "test-backdoor",
    };

    /// <summary>
    /// 这些 type **不该**有 C# case —— 它们是页面↔页面 / 页面↔Worklet / 页面↔信令 的内部消息，
    /// 只是在页面里也用 `post({type:…})` 的形式出现，所以会被静态扫描扫到。
    ///
    /// 【为什么单独列出来而不是从 EventsIn 删掉】删掉就等于"没人知道它存在"，下次有人再扫出
    /// 这 9 个又要重新判一遍。列在这里 + 写明理由，守卫就能区分"故意不管"与"忘了接"。
    /// </summary>
    public static readonly IReadOnlyDictionary<string, string> SignalingOnly = new Dictionary<string, string>
    {
        ["offer"] = "WebRTC 信令：页面直接发给信令服务器（sendSignal），不经过 C#",
        ["answer"] = "同上",
        ["ice"] = "同上（ICE 候选）",
        ["chat"] = "DataChannel 通道名（chatChannel 的 label），不是事件",
        ["pcm"] = "页面 → AudioWorklet 的 postMessage（共享电脑声音推流），不经 C#",
        ["file-end"] = "DataChannel 分片协议消息（对端收），不经 C#",
        ["file-meta"] = "同上",
        ["hangup"] = "WebRTC 信令：挂断通知对端（页面直接发），避免两端状态不一致",
        ["screen-stop-notify"] = "页面直接通知对端清理画面（sendSignal），不经 C#",
        ["incoming-call"] = "页面自己已把通话态置位并提示；C# 侧用 call-link/call-state 渲染",
        ["ice-config-state"] = "诊断快照：由页面的 iceConfigState() 主动查询时才发；" +
                               "目前 C# 不查询它（TURN 配置的确认走 ice-config 的 ack），" +
                               "所以它不是没人接，而是没人问。S4 接诊断面板时再启用。",
    };

    /// <summary>
    /// C# 里**保留**了处理、但当前页面实现还没发出的 type。
    ///
    /// 【为什么不删 C# 那几段】它们各自有明确的诊断价值（见 MainWindow 里的注释）：
    /// link-replaced = 对端重连、link-busy = 房间第二人拿不到媒体、share-audio-attach-skipped = 音轨没挂上。
    /// 这三件事一旦发生，用户看到的现象都是"功能莫名不通"，所以处理代码要留着；
    /// 列在这里是为了让守卫能区分"保留了但暂未触发"与"登记漏了/页面忘了发"。
    /// </summary>
    public static readonly IReadOnlyDictionary<string, string> ReservedEvents = new Dictionary<string, string>
    {
        ["link-replaced"] = "对端重连（页面目前只在 link-switched 里汇报现状）",
        ["link-busy"] = "房间第二人拿不到媒体（实测未触发；触发时有日志）",
        ["share-audio-attach-skipped"] = "共享声音音轨没挂上（触发时有日志）",
    };

    /// <summary>C# → 页面：C# 会调用的页面方法（CallAsync/CallRawStringAsync 的实参）。</summary>
    public static readonly IReadOnlyList<string> MethodsOut = new[]
    {
        "call", "hangup", "join", "listDevices", "probeScreenCapture",
        "pushSharedAudio", "recordSample", "selfCheck", "sendChat", "sendFile",
        "setDenoise", "setDevice", "setIceConfig", "setMuted", "setViewport",
        "shareAudioDebug", "signalChainTest", "start", "startScreenShare", "startSharedAudio",
        "stopAll", "stopScreenShare", "stopSharedAudio",
        "testSpeaker",     // 设置面板「测试扬声器」：播 1 秒 440Hz 到当前选定扬声器（诊断用）
        "listMethods",     // 启动对账：C# 调它拿页面方法集合（见 HandleMediaEvent 的 engine-methods）
    };

    /// <summary>页面自己实现、C# 目前**不**主动调的方法（列出来是为了「不是漏登记」）。</summary>
    public static readonly IReadOnlyList<string> PageOnlyMethods = new[]
    {
        "listMethods",     // 握手用：C# 调它拿页面方法集合
        "statsNow",        // 页面内部计时用
        "setTurnConfig",   // setIceConfig 的别名（兼容旧调用）
        "stopAll",         // 页面内部：整页停机
        "chatState",       // 页面内部：聊天状态通知
        "iceConfigState",  // 页面内部：ICE 配置状态通知
    };

    /// <summary>启动对账结果：缺什么、多什么、有没有空实现。</summary>
    public sealed class Report
    {
        public List<string> MissingOnPage { get; } = new();     // C# 会调，页面没有 ⇒ 调用必然静默失败
        public List<string> NotDeclared { get; } = new();       // 页面有、登记里没有 ⇒ 登记漏了
        public List<string> CaseOnly { get; } = new();          // 事件有 case 但没实现 ⇒ 等于没接
        public bool Ok => MissingOnPage.Count == 0 && NotDeclared.Count == 0 && CaseOnly.Count == 0;

        public override string ToString()
        {
            if (Ok) return $"契约对账通过（事件 {EventsIn.Count} 种 / 方法 {MethodsOut.Count} 个）";
            var parts = new List<string>();
            if (MissingOnPage.Count > 0)
                parts.Add($"页面缺少 C# 会调的方法 {MissingOnPage.Count} 个：{string.Join(", ", MissingOnPage)}");
            if (NotDeclared.Count > 0)
                parts.Add($"页面有但登记里没有 {NotDeclared.Count} 个：{string.Join(", ", NotDeclared)}");
            if (CaseOnly.Count > 0)
                parts.Add($"事件有 case 但无实现 {CaseOnly.Count} 个：{string.Join(", ", CaseOnly)}");
            return string.Join("；", parts);
        }
    }

    /// <summary>把页面报上来的方法集合与登记对账。</summary>
    public static Report Compare(IEnumerable<string> pageMethods, IEnumerable<string> caseOnlyEvents = null)
    {
        var page = new HashSet<string>(pageMethods ?? Enumerable.Empty<string>(), StringComparer.Ordinal);
        var r = new Report();
        foreach (var m in MethodsOut)
            if (!page.Contains(m) && !page.Contains(IceConfigPageMethod)) r.MissingOnPage.Add(m);
        var declared = new HashSet<string>(MethodsOut.Concat(PageOnlyMethods), StringComparer.Ordinal);
        foreach (var m in page)
            if (!declared.Contains(m)) r.NotDeclared.Add(m);
        if (caseOnlyEvents != null)
            foreach (var e in caseOnlyEvents) r.CaseOnly.Add(e);
        r.MissingOnPage.Sort(StringComparer.Ordinal);
        r.NotDeclared.Sort(StringComparer.Ordinal);
        r.CaseOnly.Sort(StringComparer.Ordinal);
        return r;
    }

    /// <summary>某个页面事件是否"故意不接"（信令/内部消息）或者"保留了但暂未触发"。</summary>
    public static bool IsIntentionallyHandledElsewhere(string eventType) =>
        SignalingOnly.ContainsKey(eventType) || ReservedEvents.ContainsKey(eventType);

    /// <summary>ICE 配置下发用的页面方法名（AppConfig 里也有同值，这里给出契约层入口）。</summary>
    public const string IceConfigPageMethod = "setIceConfig";
}
