// AppConfig.cs —— 应用配置与默认值的**唯一来源**
//
// 【为什么要有这个文件】
// 之前端口（45890/45891/41001）、探测超时、提示存活时间、TURN 配置分散在
// MainWindow.xaml.cs 各处，既有写死的字面量（自检路径查的是 45890，而真实端口是
// --port 传进来的 _signalPort），也没有任何"用户可改配置"的落盘位置（跨网段要用的
// TURN 中继只能靠改代码）。这个类把两类东西收到一处：
//   ① 默认值常量：代码里**只允许出现一次**的那些数字/字符串；
//   ② 用户配置（当前是 TURN）：读写同一个 JSON 文件。
//
// 【不许写死】的来源说明（对照 docs/coding-rules-2026-10-06.md 规矩一）：
//   · 界面文字/列表仍来自事件与配置；这里的常量只提供**默认值**，调用点必须有数据来源；
//   · 环境变量只作为**显式命名的测试后门**（下面的 *Env 常量），且在生效时写进日志。

using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace ZongxianVoice;

/// <summary>一条 ICE 服务器（目前只用于用户填写的 TURN 中继）。</summary>
public sealed class IceServerEntry
{
    /// <summary>形如 turn:host:3478?transport=udp，可多条。</summary>
    public string[] Urls { get; init; } = Array.Empty<string>();
    public string? Username { get; init; }
    public string? Credential { get; init; }
}

public sealed class AppConfig
{
    // =====================================================================
    // ① 默认值（唯一来源）
    // =====================================================================

    /// <summary>本地服务/信令端口默认值（--port 可覆盖）。</summary>
    public const int DefaultSignalPort = 45890;

    /// <summary>原生视频助手端口默认值（ZX_NATIVE_VIDEO_PORT 可覆盖，避免双实例冲突）。</summary>
    public const int DefaultNativeVideoPort = 41001;

    /// <summary>「单个应用音频」子进程探测超时（ms）。</summary>
    public const int SubprocessProbeTimeoutMs = 15000;

    /// <summary>「单个应用音频」探测录制时长（秒）。</summary>
    public const int AppAudioProbeSeconds = 1;

    /// <summary>加入别人房间前的裸 TCP 预检超时（ms）。</summary>
    public const int JoinPreflightTimeoutMs = 6000;

    /// <summary>--check-host 自检：TCP 连接超时（ms）。</summary>
    public const int CheckHostTcpTimeoutMs = 5000;

    /// <summary>--check-host 自检：HTTP 取页超时（ms）。</summary>
    public const int CheckHostHttpTimeoutMs = 6000;

    /// <summary>netstat 采样超时（ms）。</summary>
    public const int NetstatTimeoutMs = 5000;

    /// <summary>netsh 防火墙查询超时（ms）。</summary>
    public const int NetshTimeoutMs = 8000;

    /// <summary>原生视频助手退出等待（ms），超时强杀。</summary>
    public const int HelperExitWaitMs = 3000;

    /// <summary>聊天栏"临时提示"的存活秒数：过期后自动回到真实状态文案。</summary>
    public const int ChatTipSeconds = 8;

    /// <summary>共享声音泵状态自检的轮询间隔（ms）。</summary>
    public const int ShareAudioStatusPollMs = 2000;

    /// <summary>
    /// 多实例窗口错开量（像素）：按端口差推导水平偏移，避免两个实例完全重叠。
    /// </summary>
    public const int MultiInstanceWindowOffsetX = 60;

    /// <summary>ICE 配置下发后等页面回执（ice-config 事件）的超时（ms）。</summary>
    public const int IceConfigAckTimeoutMs = 10000;

    /// <summary>把 TURN 配置下发给页面的方法名（页面里的 window.zxIceConfig 入口）。</summary>
    public const string IceConfigPageMethod = "setIceConfig";

    /// <summary>页面回报"ICE 配置已应用"的事件类型。</summary>
    public const string IceConfigEvent = "ice-config";

    /// <summary>用户配置文件名（写在应用目录，与其它诊断产物同处）。</summary>
    public const string ConfigFileName = "app-config.json";

    /// <summary>信令 WebSocket 路径。</summary>
    public const string SignalPath = "/signal";

    /// <summary>通话页文件名（自检取页用）。</summary>
    public const string CallPageName = "call.html";

    // 显式命名的测试后门（只在显式设置时生效；生效必须写日志）
    public const string NativeVideoEnv = "ZX_NATIVE_VIDEO";
    public const string NativeVideoPortEnv = "ZX_NATIVE_VIDEO_PORT";
    public const string ShareAudioBackdoorEnv = "ZX_SHARE_AUDIO";
    public const string ProbePathEnv = "ZX_PROBE_PATH";
    public const string VideoHelperPathEnv = "ZX_VIDEO_HELPER_PATH";

    /// <summary>共享电脑声音的采样率：唯一来源是采集泵自己的常量（避免两处各写一份）。</summary>
    public static int SharedAudioSampleRate => ShareAudioCapture.SampleRate;

    // =====================================================================
    // ② 用户配置（TURN 中继）
    // =====================================================================

    /// <summary>TURN 服务器地址（可多条，用逗号/分号/空格分隔）；空 = 只用 STUN。</summary>
    public string TurnUrls { get; set; } = "";
    public string TurnUsername { get; set; } = "";
    public string TurnCredential { get; set; } = "";

    // =====================================================================
    // ③ 用户配置（房间列表）—— 产品化：多房间 + 切换 + 记住
    // =====================================================================

    /// <summary>
    /// 我的名字（用户显示名）。
    /// 【为什么必须有】实测：设置里改名字只影响本地显示 —— `SelfDisplayName()` 只读输入框，
    /// 从不写回 `_selfName`（信令广播的名字）、也不存盘 ⇒ 别人看到旧名、重启后丢失。
    /// </summary>
    public string SelfName { get; set; } = "";

    /// <summary>房间列表（含当前选中项）。旧配置没有这个字段时为空，由 EnsureDefault 兜底。</summary>
    public RoomStore RoomList { get; set; } = new();

    /// <summary>用户是否填了中继地址（空 = 不追加任何 TURN 条目）。</summary>
    [JsonIgnore]
    public bool TurnConfigured => TurnUrls.Trim().Length > 0;

    /// <summary>最近一次 Save 失败的原因（成功或未保存时为 null）。失败必须能说出原因。</summary>
    [JsonIgnore]
    public string? LastSaveError { get; private set; }

    /// <summary>配置文件全路径。</summary>
    [JsonIgnore]
    public static string DefaultPath =>
        Path.Combine(AppContext.BaseDirectory, ConfigFileName);

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        WriteIndented = true,
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    /// <summary>从磁盘读配置；文件不存在或损坏时退回默认值（绝不因为配置让应用起不来）。</summary>
    public static AppConfig Load(string? path = null)
    {
        var p = path ?? DefaultPath;
        var cfg = new AppConfig();
        try
        {
            if (!File.Exists(p)) return cfg;
            var dto = JsonSerializer.Deserialize<ConfigDto>(File.ReadAllText(p));
            if (dto is null) return cfg;
            cfg.TurnUrls = dto.TurnUrls ?? "";
            cfg.TurnUsername = dto.TurnUsername ?? "";
            cfg.TurnCredential = dto.TurnCredential ?? "";
            // 房间列表：旧配置没有这段 → 保持空，由调用方 EnsureDefault 建"我的房间"
            if (dto.Rooms is { Count: > 0 })
            {
                cfg.RoomList.Rooms = dto.Rooms;
                cfg.RoomList.CurrentId = dto.CurrentRoomId ?? dto.Rooms[0].Id;
                cfg.RoomList.LastConnectedAddress = dto.LastConnectedAddress ?? "";
            cfg.SelfName = dto.SelfName ?? "";
            }
        }
        catch
        {
            // 配置读坏了就用默认值：跨网段中继缺失只影响直连失败的场景
        }
        return cfg;
    }

    /// <summary>写回磁盘。返回是否成功（调用方据此如实写日志）。</summary>
    public bool Save(string? path = null)
    {
        var p = path ?? DefaultPath;
        try
        {
            var dto = new ConfigDto
            {
                TurnUrls = TurnUrls.Trim(),
                TurnUsername = TurnUsername.Trim(),
                TurnCredential = TurnCredential,
                SelfName = SelfName,
                Rooms = RoomList.Rooms,
                CurrentRoomId = RoomList.CurrentId,
                LastConnectedAddress = RoomList.LastConnectedAddress,
            };
            // 【编码】用**带 BOM 的 UTF-8**：无 BOM 时中文 Windows 上的记事本/PowerShell
            // 会按 ANSI 解读，用户看到房间名是乱码（实测）。.NET 反序列化兼容 BOM。
            File.WriteAllText(p, JsonSerializer.Serialize(dto, JsonOpts),
                              new System.Text.UTF8Encoding(encoderShouldEmitUTF8Identifier: true));
            LastSaveError = null;
            return true;
        }
        catch (Exception ex)
        {
            // 【不再静默】以前是 `catch { return false; }` —— 保存失败查不出原因。
            // 现在把原因留在 LastSaveError，调用方可以写进日志。
            LastSaveError = $"{ex.GetType().Name}: {ex.Message}";
            return false;
        }
    }

    /// <summary>
    /// 生成要下发给页面的 iceServers 列表：**只有用户填了 TURN 才有条目**，
    /// 空配置返回空数组（页面据此保持"只用 STUN"的现状，不出现任何写死的 TURN）。
    /// </summary>
    public IReadOnlyList<IceServerEntry> BuildIceServers()
    {
        if (!TurnConfigured) return Array.Empty<IceServerEntry>();
        var urls = TurnUrls.Split(
            new[] { ',', ';', ' ', '\t', '\r', '\n' },
            StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        if (urls.Length == 0) return Array.Empty<IceServerEntry>();
        return new[]
        {
            new IceServerEntry
            {
                Urls = urls,
                Username = TurnUsername.Trim().Length > 0 ? TurnUsername.Trim() : null,
                Credential = TurnCredential.Length > 0 ? TurnCredential : null,
            },
        };
    }

    /// <summary>日志用摘要：**只报条数**，绝不回显账号/密码。</summary>
    [JsonIgnore]
    public string TurnSummary =>
        TurnConfigured
            ? $"中继条目 {BuildIceServers().Count} 条"
            : "未填中继（只用 STUN）";

    /// <summary>落盘用的 DTO（与内存模型分开，避免把只读派生属性写进文件）。</summary>
    private sealed class ConfigDto
    {
        public string? TurnUrls { get; set; }
        public string? TurnUsername { get; set; }
        public string? TurnCredential { get; set; }

        /// <summary>房间列表（产品化：多房间）。旧配置文件没有这个字段 → null。</summary>
        public List<RoomEntry>? Rooms { get; set; }
        public string? SelfName { get; set; }
        public string? CurrentRoomId { get; set; }
        public string? LastConnectedAddress { get; set; }
    }
}
