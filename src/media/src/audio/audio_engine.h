// audio/audio_engine.h —— 采集源的中立接口与工厂
//
// 这一层的存在意义：让上层的 C ABI、路由、降噪、录音完全不认识 WASAPI。
// 将来要加别的后端（比如播放侧的 WASAPI 渲染、或 Linux 的 PipeWire），
// 只需要再实现一遍 AudioSource，上层一行不改。
//
// 接口刻意做得很小。加方法之前先问：这个操作是不是所有后端都能实现？
// 如果只有 WASAPI 能实现（比如「按进程抓音频」），它就应该作为 open 的配置项，
// 而不是接口上的方法。
//
// ===========================================================================
// 【契约 · 2026-10-06 写进头文件，别再只写在注释里】
//
// 1) 单位：
//    · Read(out_interleaved, cap_frames, ...) 的 cap_frames 是**音频帧**
//      （帧 = 每声道一个样本），out_interleaved 必须有 cap_frames × 声道数
//      个 float 的空间；返回值也是帧数。
//    · RingBuffer（见 ring_buffer.h）内部一切计数都是**样本(float)**。
//      两者之间的换算只允许出现在 WasapiSource 里，必须自己乘/除声道数。
//
// 2) 采样率：
//    · 交给上层的采样率就是**实际生效的那个**（SourceFormat::sample_rate），
//      不是"我们希望是 48k"。设备混音格式是多少，或者重采样后的目标是多少，
//      以这里报出的数字为准；消费者（应用/工具）必须按它算时长与重采样。
//    · SourceFormat::device_sample_rate 是设备混音格式的采样率，
//      resampled == true 表示我们请求了 AUTOCONVERTPCM，由 WASAPI
//      把 device_sample_rate 重采样到 sample_rate。不许假设 48000。
//
// 3) 线程/生命周期：
//    · Start / Stop / Close **不得与 Read 并发**（调用方责任）。
//      引擎在 C ABI 层用共享/独占锁把这条串行化（见 media_api.cpp），
//      但仍不允许"一边 Read 一边 Close"这种用法。
//    · stats() 允许在任意线程随时调用：实现必须给出**一致快照**，
//      不许让读侧看到撕裂的计数（WasapiSource 用 seqlock）。
//    · 实时采集线程上禁止：分配内存、写文件、进语言运行时。
//      允许：拷贝、算数、写环形缓冲、取锁（RingBuffer 内部有锁）。
// ===========================================================================
#pragma once

#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace zx {

// 与 C ABI 的 zx_source_kind 数值一一对应
enum class SourceKind : int { Mic = 0, Loopback = 1, Process = 2 };

struct DeviceInfo {
    std::string id;              // 不透明设备标识，原样回传即可打开
    std::string name;            // 面向用户的名字
    std::string kind;            // "mic" / "render"
    bool is_default = false;
    int channels = 2;
    int sample_rate = 48000;
};

struct SourceConfig {
    SourceKind kind = SourceKind::Mic;
    std::string device_id;           // 空 = 系统默认设备
    uint32_t process_id = 0;         // kind == Process 时必填
    bool include_child_processes = true;  // 强烈建议 true，见 process_loopback 的注释
    int want_channels = 1;           // 麦克风 1，共享音频 2
    int block_frames = 480;          // 10ms @48kHz
    // 期望的采样率（来自参数，不写死）。设备混音格式和它不同时，
    // 端点采集路径会请求 WASAPI 用 AUTOCONVERTPCM 重采样到它；
    // 重采样不可用时如实回退到设备采样率（不静默变调，见 audio_engine.h 契约 2）。
    int want_sample_rate = 48000;
};

struct SourceFormat {
    int sample_rate = 48000;         // **实际生效**的采样率（消费方按它算时长）
    int device_sample_rate = 0;      // 设备混音格式的采样率（0 = 未知）
    bool resampled = false;          // true = 由 WASAPI 重采样到 sample_rate
    int channels = 1;
    int bits_per_sample = 32;
    bool is_float = true;
    int block_frames = 480;
    double period_ms = 10.0;
};

struct SourceStats {
    uint64_t frames_captured = 0;
    uint64_t frames_dropped = 0;
    uint64_t discontinuities = 0;    // 后端报告的数据断流次数（关键健康指标）
    uint64_t silent_packets = 0;
    double avg_period_ms = 0.0;
    double max_period_ms = 0.0;
    // 环形缓冲里可读的**样本(float)**数 —— 键名与 RingBuffer 的单位契约一致，
    // 不是帧数（见 ring_buffer.h）。
    int buffered_samples = 0;
    uint64_t callback_count = 0;
    // 采集线程上被消毒成静音的越界/NaN 样本数（回环采集会给出未定义内容）。
    // 以前只加不报，现在进统计：界面上能看出"这段采集有多脏"。
    uint64_t sanitized_samples = 0;
    // 采集线程是否成功注册进 MMCSS 的 "Pro Audio"（AvSetMmThreadCharacteristics）。
    bool pro_audio = false;
};

// 实时音频线程上的回调。实现里只允许拷内存/算数/写环形缓冲。
// 禁止：分配内存、加锁、写文件、进任何语言运行时。
using SourceCallback = std::function<void(const float*, int, int)>;

class AudioSource {
public:
    virtual ~AudioSource() = default;

    virtual bool Start() = 0;
    virtual void Stop() = 0;

    // 阻塞最多 timeout_ms 毫秒，取最多 cap_frames **帧**。
    // out_interleaved 至少要有 cap_frames × format().channels 个 float。
    // 返回值 > 0 = 取到的帧数，0 = 超时（不是错误）。
    // 不得与 Start/Stop/Close 并发调用（见上面的契约 3）。
    virtual int Read(float* out_interleaved, int cap_frames, int timeout_ms) = 0;

    // 允许任意线程随时调用；实现负责给出一致快照（不撕裂）。
    virtual SourceFormat format() const = 0;
    virtual SourceStats stats() const = 0;

    // 采集回调。只允许在 Start 之前设置、Stop 之后清除 ——
    // 采集线程会读它，运行中改指针就是数据竞争。
    virtual void set_callback(SourceCallback cb) = 0;
    virtual void clear_callback() = 0;

    // 真实电平。可以随时调用，不阻塞、不影响数据流。
    struct Meter {
        float rms = 0.0f;
        float peak = 0.0f;
        double dbfs = -120.0;
        bool voiced = false;
        bool clipped = false;
    };
    virtual Meter meter() const = 0;
};

// 探测器：本机是否支持「单个应用音频」（进程回环）。
// 注意这是**真实探测**（试调一次 ActivateAudioInterfaceAsync），不是读版本号。
// 微软文档写的最低版本是 20348，但社区反馈 Win10 2004+ 实测可用，
// 按版本号判断会冤枉一大批能用的机器。
struct ProcessLoopbackProbe {
    bool supported = false;
    std::string detail;   // 失败原因的人话说明，直接展示给用户
};

// 一个有音频会话的进程。用于「只共享某个应用的声音」的选择列表。
struct SessionProcessInfo {
    uint32_t pid = 0;
    std::string name;      // 可执行文件名，如 "chrome.exe"
    std::string title;     // 会话显示名（不一定有）
    bool active = false;   // true = 此刻正在发声
};

// 枚举当前有音频会话的进程。
// 注意语义：包含「正在播放」和「刚停止」的会话，用 active 区分 ——
// 界面上要能显示「未在播放」，而不是干脆不列出来（用户会以为程序没找到）。
std::vector<SessionProcessInfo> EnumerateAudioSessions();

struct WasapiBackend {
    // 采集设备（麦克风等）
    static std::vector<DeviceInfo> EnumerateCaptureDevices();
    // 渲染设备（播放），供「从哪个音箱出声」选择
    static std::vector<DeviceInfo> EnumerateRenderDevices();

    // 打开一个采集源。失败返回 nullptr，reason 里放人话说明。
    static std::unique_ptr<AudioSource> OpenSource(const SourceConfig& cfg,
                                                   std::string* reason);

    static ProcessLoopbackProbe ProbeProcessLoopback();

    // 真实实现。由 ProbeProcessLoopback 用 SEH 包起来调用 ——
    // 系统的异步激活接口在部分机器上会让进程崩溃而不是返回错误码。
    //
    // 为什么用出参而不是返回值：__try 所在的函数不允许任何需要对象展开的
    // 操作（MSVC 报 C2712），而"按值返回一个含 std::string 的结构体"正好
    // 属于被禁止的那类。改成写进调用方提供的对象就没有展开需求了。
    static void ProbeProcessLoopbackImpl(ProcessLoopbackProbe* out);
};

// 引擎全局初始化/反初始化（COM、日志等）。幂等。
bool EngineInitialize(std::string* reason);
void EngineShutdown();

// 音频会话枚举（「哪些应用在发声」）。放在这里而不是 WasapiBackend 里，
// 因为它跨所有渲染设备，不属于某个具体后端的设备操作。
std::vector<SessionProcessInfo> EnumerateAudioSessions();

}  // namespace zx
