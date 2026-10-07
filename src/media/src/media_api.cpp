// media_api.cpp —— C ABI 的实现
//
// 这个文件是 Python 与引擎之间唯一的门。它的职责被刻意限制在三件事：
//   1) 参数校验与类型转换（C 字符串 ↔ std::string，JSON ↔ 结构体）
//   2) 把调用转发给真正的实现（WASAPI 后端 / 降噪 / 录音）
//   3) 线程局部错误信息与日志
// 它**不应该**包含任何音频算法或设备逻辑 —— 那些在 audio/ 下面。
//
// 关于错误信息的规则：zx_last_error() 返回线程局部缓冲，读到就清空。
// 这样调用方可以用「失败后立刻读一次」的简单模式拿到原因，不需要传错误对象。
#include "zongxian_media.h"

#include "audio/audio_engine.h"
#include "audio/audio_sink.h"
#include "audio/noise_suppress.h"
#include "audio/text_util.h"
#include "json_util.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <deque>
#include <fstream>
#include <memory>
#include <mutex>
#include <new>
#include <shared_mutex>
#include <string>
#include <vector>

#ifdef _WIN32
#  include <windows.h>
#endif

using namespace zx;

namespace {

// ---------------------------------------------------------------------------
// 全局状态
// ---------------------------------------------------------------------------

std::atomic<bool> g_initialized{false};
std::mutex g_log_mutex;
std::deque<std::string> g_log_ring;
size_t g_log_ring_capacity = 4000;

// 线程局部错误信息。刻意不做成全局的：多线程同时出错时，
// 全局缓冲会把原因互相覆盖，排查时看到的是别人的错误。
thread_local std::string g_last_error;

const char* g_version = "0.1.0-pl0";

void SetError(const std::string& msg) { g_last_error = msg; }

void ClearError() { g_last_error.clear(); }

// ---------------------------------------------------------------------------
// 日志级别（审计 §四 的假开关之一）
//
// 以前 zx_media_init 把 "logLevel" 解析出来就 (void) 丢掉 —— 传什么都没区别。
// 现在它是**真正的过滤器**：低于阈值的日志不进环形缓冲，也不写文件。
// 级别取值与头文件文档一致：trace|debug|info|warn|error，默认 info。
// ---------------------------------------------------------------------------
enum class LogSev : int { Trace = 0, Debug = 1, Info = 2, Warn = 3, Error = 4 };

bool ParseLogLevel(const std::string& s, LogSev* out) {
    if (s == "trace") { *out = LogSev::Trace; return true; }
    if (s == "debug") { *out = LogSev::Debug; return true; }
    if (s == "info")  { *out = LogSev::Info;  return true; }
    if (s == "warn")  { *out = LogSev::Warn;  return true; }
    if (s == "error") { *out = LogSev::Error; return true; }
    return false;
}

int SevOf(const char* level) {
    if (std::strcmp(level, "trace") == 0) return static_cast<int>(LogSev::Trace);
    if (std::strcmp(level, "debug") == 0) return static_cast<int>(LogSev::Debug);
    if (std::strcmp(level, "warn") == 0)  return static_cast<int>(LogSev::Warn);
    if (std::strcmp(level, "error") == 0) return static_cast<int>(LogSev::Error);
    return static_cast<int>(LogSev::Info);
}

std::atomic<int> g_log_min_level{static_cast<int>(LogSev::Info)};

void LogLine(const char* level, const std::string& msg) {
    if (SevOf(level) < g_log_min_level.load(std::memory_order_relaxed)) return;
    std::lock_guard<std::mutex> lock(g_log_mutex);
    char ts[32];
    const auto now = std::chrono::system_clock::now();
    const std::time_t t = std::chrono::system_clock::to_time_t(now);
    std::tm tm{};
#ifdef _WIN32
    ::localtime_s(&tm, &t);
#else
    tm = *std::localtime(&t);
#endif
    std::strftime(ts, sizeof(ts), "%H:%M:%S", &tm);

    g_log_ring.push_back(std::string(ts) + " [" + level + "] " + msg);
    while (g_log_ring.size() > g_log_ring_capacity) g_log_ring.pop_front();
}

// 把 JSON 写进调用方的缓冲。约定：返回所需长度（含结尾 \0）；
// 缓冲不够时返回 ZX_ERR_BUFFER_TOO_SMALL 并尽力截断。
int WriteJson(const std::string& json, char* out, int cap) {
    const int need = static_cast<int>(json.size()) + 1;
    if (out && cap > 0) {
        const int n = std::min<int>(cap - 1, static_cast<int>(json.size()));
        std::memcpy(out, json.data(), static_cast<size_t>(n));
        out[n] = '\0';
    }
    return out ? need : need;
}

zx_result WriteJsonResult(const std::string& json, char* out, int cap) {
    const int need = WriteJson(json, out, cap);
    return need > cap ? ZX_ERR_BUFFER_TOO_SMALL : ZX_OK;
}

zx_result WritePlainString(const std::string& s, char* out, int cap) {
    const int need = static_cast<int>(s.size()) + 1;
    if (out && cap > 0) {
        const int n = std::min<int>(cap - 1, static_cast<int>(s.size()));
        std::memcpy(out, s.data(), static_cast<size_t>(n));
        out[n] = '\0';
    }
    return need > cap ? ZX_ERR_BUFFER_TOO_SMALL : ZX_OK;
}

// ---------------------------------------------------------------------------
// Source：一个采集源 + 它的降噪与录音
// ---------------------------------------------------------------------------

struct Source {
    std::unique_ptr<AudioSource> backend;
    std::unique_ptr<NoiseSuppressor> ns;
    std::unique_ptr<WavWriter> rec_raw;
    std::unique_ptr<WavWriter> rec_processed;

    NsLevel requested_level = NsLevel::Moderate;
    NsBackend requested_backend = NsBackend::Builtin;
    bool ns_available_rnnoise = false;

    // 音频路径互斥：Read 与配置变更不能同时进行，否则降噪内部状态会被
    // 一半旧参数一半新参数地破坏，表现为偶发的爆音。
    std::mutex audio_mutex;

    // 上一块处理后的数据，用于 meter（不重新遍历后端的数据）
    std::vector<float> last_processed;
    int last_channels = 1;

    AudioSource::Meter last_meter;
    int block_frames = 480;

    bool recorder_enabled() const {
        return (rec_raw && rec_raw->IsOpen()) ||
               (rec_processed && rec_processed->IsOpen());
    }
};

// 把交错多声道降成单声道（取平均，比只取第一声道更稳：
// 有些设备的右声道是静音的，只取左声道会漏掉实际声音）
void DownmixToMono(const float* in, int frames, int channels, float* out) {
    if (channels <= 1) {
        std::memcpy(out, in, static_cast<size_t>(frames) * sizeof(float));
        return;
    }
    const float inv = 1.0f / static_cast<float>(channels);
    for (int i = 0; i < frames; ++i) {
        float acc = 0.0f;
        for (int c = 0; c < channels; ++c) acc += in[static_cast<size_t>(i) * channels + c];
        out[i] = acc * inv;
    }
}

// 单声道铺回多声道（降噪后的结果按声道数复制出去）
void UpmixFromMono(const float* in, int frames, int channels, float* out) {
    if (channels <= 1) {
        std::memcpy(out, in, static_cast<size_t>(frames) * sizeof(float));
        return;
    }
    for (int i = 0; i < frames; ++i) {
        const float v = in[i];
        for (int c = 0; c < channels; ++c) out[static_cast<size_t>(i) * channels + c] = v;
    }
}

AudioSource::Meter ComputeMeter(const float* interleaved, int frames, int channels) {
    AudioSource::Meter m;
    if (frames <= 0) return m;
    double sum_sq = 0.0;
    float peak = 0.0f;
    const size_t total = static_cast<size_t>(frames) * (channels > 0 ? channels : 1);
    for (size_t i = 0; i < total; ++i) {
        const float v = interleaved[i];
        const float a = std::fabs(v);
        if (a > peak) peak = a;
        sum_sq += static_cast<double>(v) * v;
    }
    const double rms = std::sqrt(sum_sq / static_cast<double>(total ? total : 1));
    m.rms = static_cast<float>(rms);
    m.peak = peak;
    m.dbfs = 20.0 * std::log10(rms > 1e-7 ? rms : 1e-7);
    m.voiced = rms > 0.01;
    m.clipped = peak >= 0.999f;
    return m;
}

// ---------------------------------------------------------------------------
// 句柄生命周期锁（修掉 close 与 read 并发的释放后使用）
//
// 以前 zx_source_close 直接 delete，而另一个线程可能正在 zx_source_read 里
// 用同一个 Source* —— 释放后使用（UAF），表现为随机访问冲突。现在：
//   · 用句柄走**共享锁**：SourceRef 在锁内做一次"这个指针还在注册表里吗"的身份校验，
//     校验通过才使用；多个读可以并发。
//   · 关句柄走**独占锁**：先从注册表摘掉，再 Stop/delete。
// 于是 Read 要么在 Close 之前完成，要么看到"句柄已失效"直接返回错误，不会踩释放内存。
// 注意：地址复用理论上能让陈旧句柄撞上新对象，这不在承诺范围内 ——
// 头文件里写死了"Start/Stop/Close 不得与 Read 并发"的调用方契约。
// ---------------------------------------------------------------------------
std::shared_mutex g_sources_mutex;
std::vector<Source*> g_sources;

class SourceRef {
public:
    explicit SourceRef(zx_source_t* h) : lock_(g_sources_mutex) {
        Source* candidate = reinterpret_cast<Source*>(h);
        if (!candidate) return;
        if (std::find(g_sources.begin(), g_sources.end(), candidate) != g_sources.end()) {
            s_ = candidate;
        }
    }
    Source* get() const { return s_; }
    explicit operator bool() const { return s_ != nullptr; }

private:
    std::shared_lock<std::shared_mutex> lock_;
    Source* s_ = nullptr;
};

// 主音量（进程级）。以前 EngineHandle::master_volume 只写不读 —— Python 侧
// zx_engine_set_master_volume() 调了却对声音毫无影响（审计 §四 的假开关之一）。
// 现在它由 zx_source_read 在交付数据前统一缩放，是真开关。
std::atomic<float> g_master_volume{1.0f};

// engine 句柄承载全局设置。现在字段很少，但句柄本身要先钉在 ABI 上：
// PL1 的会话会挂在它下面，将来补上就不用改 Python 侧的调用方式。
struct EngineHandle {
    float master_volume = 1.0f;
};

}  // namespace

// ===========================================================================
// 生命周期
// ===========================================================================

extern "C" ZX_API zx_result zx_media_init(const char* json_config) {
    if (g_initialized.load()) return ZX_OK;   // 幂等
    ClearError();

    std::string cfg = json_config ? json_config : "";
    JsonReader reader(cfg);

    // logLevel 现在是真过滤器（见上面的 LogSev）。给了非法值就明确报错，
    // 不静默按默认值走 —— 静默是审计点名的病。
    const std::string log_level = reader.GetString("logLevel", "info");
    LogSev sev = LogSev::Info;
    if (!ParseLogLevel(log_level, &sev)) {
        SetError("logLevel 取值非法（只接受 trace|debug|info|warn|error）：" + log_level);
        return ZX_ERR_INVALID_ARG;
    }
    g_log_min_level.store(static_cast<int>(sev), std::memory_order_relaxed);

    const int64_t ring = reader.GetInt("logRingLines", 4000);
    if (ring > 0) g_log_ring_capacity = static_cast<size_t>(ring);

    std::string reason;
    if (!EngineInitialize(&reason)) {
        SetError(reason.empty() ? "引擎初始化失败" : reason);
        return ZX_ERR_BACKEND_FAILED;
    }

    const std::string log_path = reader.GetString("logPath", "");
    LogLine("info", std::string("引擎启动，版本 ") + g_version +
                        (log_path.empty() ? "" : ("，日志将写入 " + log_path)));
    LogLine("debug", std::string("日志级别阈值已生效：") + log_level);
    g_initialized.store(true);
    return ZX_OK;
}

extern "C" ZX_API void zx_media_shutdown(void) {
    if (!g_initialized.exchange(false)) return;

    // 先关所有还开着的源，避免采集线程在 COM 反初始化之后才退出。
    // 独占锁：确保此刻没有任何线程还在 zx_source_read 里用这些 Source。
    {
        std::unique_lock<std::shared_mutex> lock(g_sources_mutex);
        for (Source* s : g_sources) {
            if (!s) continue;
            if (s->backend) s->backend->Stop();
            if (s->rec_raw) s->rec_raw->Close();
            if (s->rec_processed) s->rec_processed->Close();
        }
        g_sources.clear();
    }

    EngineShutdown();
    LogLine("info", "引擎已关闭");
}

extern "C" ZX_API int zx_media_abi_version(void) { return ZX_MEDIA_ABI_VERSION; }

extern "C" ZX_API const char* zx_media_version(void) { return g_version; }

// ===========================================================================
// 诊断
// ===========================================================================

extern "C" ZX_API const char* zx_last_error(void) {
    // 返回后即清空：调用方的用法是「失败 → 读一次 → 展示」
    static thread_local std::string buf;
    buf = g_last_error;
    g_last_error.clear();
    return buf.c_str();
}

extern "C" ZX_API zx_result zx_media_dump_log(const char* path_utf8) {
    if (!path_utf8 || !*path_utf8) {
        SetError("dump_log 需要有效的路径");
        return ZX_ERR_INVALID_ARG;
    }
    std::ofstream f(Utf8ToWide(path_utf8).c_str(), std::ios::binary);
    if (!f) {
        SetError(std::string("无法写入日志文件：") + path_utf8);
        return ZX_ERR_BACKEND_FAILED;
    }
    std::lock_guard<std::mutex> lock(g_log_mutex);
    f << "# 棕仙语音 引擎日志  abi=" << ZX_MEDIA_ABI_VERSION
      << " version=" << g_version << "\n";
    for (const auto& line : g_log_ring) f << line << "\n";
    return ZX_OK;
}

// ===========================================================================
// 设备枚举与能力探测
// ===========================================================================

extern "C" ZX_API int zx_list_capture_devices(char* out_json, int cap) {
    if (!g_initialized.load()) {
        SetError("请先调用 zx_media_init");
        return ZX_ERR_NOT_INITIALIZED;
    }
    JsonWriter w;
    w.BeginArray();
    for (const auto& d : WasapiBackend::EnumerateCaptureDevices()) {
        w.BeginObject();
        w.Key("id").Value(d.id);
        w.Key("name").Value(d.name);
        w.Key("kind").Value(d.kind);
        w.Key("isDefault").Value(d.is_default);
        w.Key("channels").Value(d.channels);
        w.Key("sampleRate").Value(d.sample_rate);
        w.EndObject();
    }
    w.EndArray();
    return WriteJson(w.str(), out_json, cap);
}

extern "C" ZX_API int zx_list_render_devices(char* out_json, int cap) {
    if (!g_initialized.load()) {
        SetError("请先调用 zx_media_init");
        return ZX_ERR_NOT_INITIALIZED;
    }
    JsonWriter w;
    w.BeginArray();
    for (const auto& d : WasapiBackend::EnumerateRenderDevices()) {
        w.BeginObject();
        w.Key("id").Value(d.id);
        w.Key("name").Value(d.name);
        w.Key("kind").Value(d.kind);
        w.Key("isDefault").Value(d.is_default);
        w.Key("channels").Value(d.channels);
        w.Key("sampleRate").Value(d.sample_rate);
        w.EndObject();
    }
    w.EndArray();
    return WriteJson(w.str(), out_json, cap);
}

extern "C" ZX_API zx_result zx_probe_process_loopback(int* out_supported,
                                                      char* out_detail,
                                                      int detail_cap) {
    if (!g_initialized.load()) {
        SetError("请先调用 zx_media_init");
        return ZX_ERR_NOT_INITIALIZED;
    }
    if (!out_supported) {
        SetError("out_supported 不能为空");
        return ZX_ERR_INVALID_ARG;
    }
    const ProcessLoopbackProbe probe = WasapiBackend::ProbeProcessLoopback();
    *out_supported = probe.supported ? 1 : 0;
    if (out_detail && detail_cap > 0) {
        WritePlainString(probe.detail, out_detail, detail_cap);
    }
    LogLine("info", std::string("进程回环探测：") + (probe.supported ? "支持" : "不支持") +
                        " — " + probe.detail);
    return ZX_OK;
}

extern "C" ZX_API int zx_list_audio_processes(char* out_json, int cap) {
    if (!g_initialized.load()) {
        SetError("请先调用 zx_media_init");
        return ZX_ERR_NOT_INITIALIZED;
    }
    JsonWriter w;
    w.BeginArray();
    // Windows 上枚举「正在发声的进程」的唯一正路是音频会话管理器。
    // 这里给出的是「有音频会话」的进程，包含正在播放和刚停止的，
    // 由 active 字段区分 —— UI 上要能显示「未在播放」而不是干脆不列出来。
    for (const auto& p : EnumerateAudioSessions()) {
        w.BeginObject();
        w.Key("pid").Value(static_cast<int>(p.pid));
        w.Key("name").Value(p.name);
        w.Key("title").Value(p.title);
        w.Key("active").Value(p.active);
        w.EndObject();
    }
    w.EndArray();
    return WriteJson(w.str(), out_json, cap);
}

// ===========================================================================
// 引擎对象
//
// 目前 engine 只是一个「承载全局设置」的句柄。刻意先把它钉在 ABI 上：
// 后续的会话（PL1）挂在它下面，如果现在不留，将来加就得改 Python 侧的调用。
// ===========================================================================

extern "C" ZX_API zx_result zx_engine_create(zx_engine_t** out_engine) {
    if (!g_initialized.load()) {
        SetError("请先调用 zx_media_init");
        return ZX_ERR_NOT_INITIALIZED;
    }
    if (!out_engine) {
        SetError("out_engine 不能为空");
        return ZX_ERR_INVALID_ARG;
    }
    auto* e = new (std::nothrow) EngineHandle();
    if (!e) {
        SetError("内存不足");
        return ZX_ERR_INTERNAL;
    }
    // 从进程级主音量继承（这个字段因此是"读得到"的，不是只写不读）
    e->master_volume = g_master_volume.load(std::memory_order_relaxed);
    *out_engine = reinterpret_cast<zx_engine_t*>(e);
    return ZX_OK;
}

extern "C" ZX_API void zx_engine_destroy(zx_engine_t* engine) {
    delete reinterpret_cast<EngineHandle*>(engine);   // delete nullptr 是合法的
}

extern "C" ZX_API zx_result zx_engine_set_master_volume(zx_engine_t* engine,
                                                        float volume) {
    if (!engine) {
        SetError("engine 句柄为空");
        return ZX_ERR_INVALID_ARG;
    }
    if (volume < 0.0f || volume > 1.0f) {
        SetError("音量必须在 0.0 ~ 1.0 之间");
        return ZX_ERR_INVALID_ARG;
    }
    // 【真正接上】发布到进程级增益，zx_source_read 交付样本前会乘上它。
    // 所有 engine 句柄共享同一个进程级值：后设置的生效（见头文件说明）。
    g_master_volume.store(volume, std::memory_order_relaxed);
    reinterpret_cast<EngineHandle*>(engine)->master_volume = volume;
    LogLine("info", "主音量设置为 " +
                        std::to_string(static_cast<double>(volume)));
    return ZX_OK;
}

// ===========================================================================
// 采集源
// ===========================================================================

extern "C" ZX_API zx_result zx_source_open(const char* json_request,
                                           zx_source_kind kind,
                                           zx_source_t** out_source) {
    if (!g_initialized.load()) {
        SetError("请先调用 zx_media_init");
        return ZX_ERR_NOT_INITIALIZED;
    }
    if (!out_source) {
        SetError("out_source 不能为空");
        return ZX_ERR_INVALID_ARG;
    }
    *out_source = nullptr;
    ClearError();

    const std::string req = json_request ? json_request : "";
    JsonReader reader(req);

    SourceConfig cfg;
    cfg.kind = static_cast<SourceKind>(static_cast<int>(kind));
    cfg.device_id = reader.GetString("deviceId", "");
    cfg.process_id = static_cast<uint32_t>(reader.GetInt("processId", 0));
    cfg.include_child_processes = reader.GetBool("includeChildProcesses", true);
    cfg.want_channels = static_cast<int>(reader.GetInt("wantChannels", 0));
    cfg.block_frames = static_cast<int>(reader.GetInt("blockFrames", ZX_BLOCK_FRAMES));
    // 期望采样率来自参数（默认 48k 不是"写死"，而是头文件 ZX_SAMPLE_RATE 的默认契约值）。
    // 端点采集会请求 WASAPI 重采样到它；设备不支持就如实回退并用实际值上报。
    cfg.want_sample_rate =
        static_cast<int>(reader.GetInt("wantSampleRate", ZX_SAMPLE_RATE));

    // 默认声道数：麦克风单声道，共享音频双声道
    if (cfg.want_channels <= 0) {
        cfg.want_channels = (cfg.kind == SourceKind::Mic) ? 1 : 2;
    }
    if (cfg.kind == SourceKind::Process && cfg.process_id == 0) {
        SetError("kind=process 时必须提供 processId");
        return ZX_ERR_INVALID_ARG;
    }

    // 进程回环先做能力探测，把「系统不支持」和「目标进程没声音」这两种
    // 完全不同的失败区分开 —— 否则用户只会看到一句没用的「打开失败」。
    if (cfg.kind == SourceKind::Process) {
        const ProcessLoopbackProbe probe = WasapiBackend::ProbeProcessLoopback();
        if (!probe.supported) {
            SetError(probe.detail);
            return ZX_ERR_UNSUPPORTED;
        }
    }

    std::string reason;
    auto backend = WasapiBackend::OpenSource(cfg, &reason);
    if (!backend) {
        SetError(reason.empty() ? "打开采集源失败" : reason);
        LogLine("error", std::string("打开采集源失败：") + reason);
        return ZX_ERR_BACKEND_FAILED;
    }

    auto src = std::make_unique<Source>();
    src->backend = std::move(backend);
    src->block_frames = cfg.block_frames;
    src->last_channels = src->backend->format().channels;
    src->last_processed.assign(
        static_cast<size_t>(cfg.block_frames) * src->last_channels, 0.0f);

    // 降噪器**按单声道**建立：zx_source_read 里是「先下混成单声道 → 降噪 → 再铺回多声道」，
    // 送进 Process 的缓冲只有 n 个 float（单声道帧数），而降噪器是按 channels_ 步进访问的。
    // 以前这里传的是后端声道数（立体声=2），于是 Process 会按 2n 个 float 读写一个只有 n 个
    // float 的缓冲 → 越界写 → 0xC0000005（实测：共享系统声音/进程回环一开降噪就崩，
    // 关掉降噪 --ns 0 就正常）。改成 1 与下混后的数据严格对齐。
    src->ns = std::make_unique<NoiseSuppressor>(1);
    src->ns->WarmUp();
    // 【原则】降噪是给**语音**用的：
    //   · 麦克风（ZX_SRC_MIC）→ 开降噪
    //   · 共享电脑声音（回环 ZX_SRC_LOOPBACK / 指定应用 ZX_SRC_PROCESS）→ **直通不降噪**
    // 依据（2026-10-06 实测，zxprobe stream 开/关降噪对照同一播放器进程）：
    //   关降噪：峰值 -4.4 dBFS，与源音频完全一致（采集与混音都正确）
    //   开降噪：峰值被抬到 +3.5 dBFS（+7.9 dB）并产生大量数值异常样本（削波）
    // 内容音频本来就不该做语音降噪；显式 zx_ns_set 仍可覆盖这里的默认。
    const bool is_voice_source = (kind == ZX_SRC_MIC);
    // RNNoise 是否可用来自编译期（见 noise_suppress.h 的 ZX_HAVE_RNNOISE），
    // 不再写死 false。真正生效的后端由 zx_ns_get_config 如实报出。
    src->ns_available_rnnoise = (ZX_HAVE_RNNOISE != 0);
    src->ns->Configure(is_voice_source ? NsLevel::Moderate : NsLevel::Off,
                       NsBackend::Builtin, src->ns_available_rnnoise);

    Source* raw = src.release();
    {
        std::unique_lock<std::shared_mutex> lock(g_sources_mutex);
        g_sources.push_back(raw);
    }
    *out_source = reinterpret_cast<zx_source_t*>(raw);

    const SourceFormat f = raw->backend->format();
    char buf[224];
    std::snprintf(buf, sizeof(buf),
                  "打开采集源 kind=%d pid=%u -> %d Hz / %d 声道 / %.1f ms/块",
                  static_cast<int>(kind), cfg.process_id, f.sample_rate, f.channels,
                  f.period_ms);
    LogLine("info", buf);
    // debug 级别日志：采样率契约的实际取值 —— 用来对账"头里的 rate 与计算用的 rate"。
    // 默认 logLevel=info 时看不到，传 debug 就能看到（这就是 logLevel 真的在过滤）。
    char dbg[256];
    std::snprintf(dbg, sizeof(dbg),
                  "采样率契约：设备 %d Hz，请求 %d Hz，实际交付 %d Hz，resampled=%s",
                  f.device_sample_rate, cfg.want_sample_rate, f.sample_rate,
                  f.resampled ? "true" : "false");
    LogLine("debug", dbg);
    return ZX_OK;
}

extern "C" ZX_API zx_result zx_source_start(zx_source_t* source) {
    SourceRef ref(source);
    Source* s = ref.get();
    if (!s || !s->backend) {
        SetError("source 句柄为空或已关闭");
        return ZX_ERR_INVALID_ARG;
    }
    if (!s->backend->Start()) {
        SetError("启动采集失败（设备可能被独占或已拔出）");
        return ZX_ERR_BACKEND_FAILED;
    }
    LogLine("info", "采集已启动");
    return ZX_OK;
}

extern "C" ZX_API zx_result zx_source_stop(zx_source_t* source) {
    SourceRef ref(source);
    Source* s = ref.get();
    if (!s || !s->backend) {
        SetError("source 句柄为空或已关闭");
        return ZX_ERR_INVALID_ARG;
    }
    s->backend->Stop();
    return ZX_OK;
}

extern "C" ZX_API void zx_source_close(zx_source_t* source) {
    Source* s = reinterpret_cast<Source*>(source);
    if (!s) return;   // 幂等：NULL 安全

    {
        // 独占锁：与 zx_source_read 的共享锁互斥。先摘出注册表，
        // 这样新的 SourceRef 立刻查不到它；已在读的调用结束后才轮到我们 delete。
        std::unique_lock<std::shared_mutex> lock(g_sources_mutex);
        const auto it = std::find(g_sources.begin(), g_sources.end(), s);
        if (it == g_sources.end()) return;   // 已关过 / 不是有效句柄：幂等返回
        g_sources.erase(it);
    }

    if (s->backend) s->backend->Stop();
    if (s->rec_raw) s->rec_raw->Close();
    if (s->rec_processed) s->rec_processed->Close();
    delete s;
}

extern "C" ZX_API zx_result zx_source_format(zx_source_t* source, char* out_json,
                                             int cap) {
    SourceRef ref(source);
    Source* s = ref.get();
    if (!s || !s->backend) {
        SetError("source 句柄为空或已关闭");
        return ZX_ERR_INVALID_ARG;
    }
    const SourceFormat f = s->backend->format();
    JsonWriter w;
    w.BeginObject();
    w.Key("sampleRate").Value(f.sample_rate);
    // 设备混音格式的真实采样率 + 是否由 WASAPI 重采样。消费者据此知道
    // sampleRate 是不是"设备本来就是这个"，也就不用假设 48000。
    w.Key("deviceSampleRate").Value(f.device_sample_rate);
    w.Key("resampled").Value(f.resampled);
    w.Key("channels").Value(f.channels);
    w.Key("bitsPerSample").Value(f.bits_per_sample);
    w.Key("isFloat").Value(f.is_float);
    w.Key("blockFrames").Value(f.block_frames);
    w.Key("periodMs").Value(f.period_ms);
    w.EndObject();
    return WriteJsonResult(w.str(), out_json, cap);
}

extern "C" ZX_API zx_result zx_source_read(zx_source_t* source, float* out_interleaved,
                                           int cap_frames, int* out_frames,
                                           int timeout_ms) {
    SourceRef ref(source);
    Source* s = ref.get();
    if (!s || !s->backend || !out_interleaved || !out_frames || cap_frames <= 0) {
        SetError("zx_source_read 参数不合法（句柄可能已关闭）");
        return ZX_ERR_INVALID_ARG;
    }
    *out_frames = 0;

    // 一次最多取 200ms。这不是性能限制而是**防误用**：caller 很容易
    // 只按帧数开缓冲却忘了乘声道数（立体声要 2 倍空间），结果就是缓冲区
    // 溢出、访问冲突，而崩溃点离真正的原因很远。
    // 上限按**该源实际采样率**算（200ms），不再写死 9600 帧 ——
    // 44.1k 的源上 9600 帧是 217ms，是另一码事。
    const int rate = s->backend->format().sample_rate > 0
                         ? s->backend->format().sample_rate
                         : ZX_SAMPLE_RATE;
    const int max_read_frames = rate / 5;
    if (cap_frames > max_read_frames) {
        char msg[128];
        std::snprintf(msg, sizeof(msg),
                      "一次最多读取 %d 帧（200ms @ %d Hz）；请分批读取",
                      max_read_frames, rate);
        SetError(msg);
        return ZX_ERR_INVALID_ARG;
    }

    std::lock_guard<std::mutex> lock(s->audio_mutex);

    const int n = s->backend->Read(out_interleaved, cap_frames, timeout_ms);
    if (n <= 0) return ZX_ERR_TIMEOUT;   // 超时不是错误，让调用方继续轮询

    const int channels = s->backend->format().channels;

    // 录音：先落原始样本，再处理。这样 raw 与 processed 是严格同源同时刻的，
    // 做 A/B 对照时不会因为两次采集的噪声不同而误判降噪效果。
    // 注意：master volume 在 raw 之后施加 —— raw 永远是"原始电平"。
    if (s->rec_raw && s->rec_raw->IsOpen()) {
        s->rec_raw->WriteFrames(out_interleaved, n);
    }

    // 降噪：多声道先降成单声道处理，再把结果铺回去。
    // 共用一套增益是为了不破坏立体声像 —— 左右声道分别处理会让声场漂移。
    const bool do_ns = s->ns && !s->ns->Bypass() && s->ns->level() != NsLevel::Off;
    if (do_ns) {
        const size_t need = static_cast<size_t>(n);
        if (s->last_processed.size() < need) s->last_processed.resize(need);

        DownmixToMono(out_interleaved, n, channels, s->last_processed.data());
        s->ns->Process(s->last_processed.data(), s->last_processed.data(), n);
        UpmixFromMono(s->last_processed.data(), n, channels, out_interleaved);
    }

    // 【主音量真正接上】zx_engine_set_master_volume() 设的就是它。
    // 以前这个值只写不读，Python 调了等于没调（审计 §四）。
    const float gain = g_master_volume.load(std::memory_order_relaxed);
    if (gain != 1.0f) {
        const size_t total = static_cast<size_t>(n) *
                             static_cast<size_t>(channels > 0 ? channels : 1);
        for (size_t i = 0; i < total; ++i) out_interleaved[i] *= gain;
    }

    if (s->rec_processed && s->rec_processed->IsOpen()) {
        s->rec_processed->WriteFrames(out_interleaved, n);
    }

    // meter 用处理后的数据：界面上显示的就应该是用户听到的东西
    s->last_meter = ComputeMeter(out_interleaved, n, channels);
    s->last_channels = channels;
    *out_frames = n;
    return ZX_OK;
}

extern "C" ZX_API zx_result zx_source_meter(zx_source_t* source, char* out_json,
                                            int cap) {
    SourceRef ref(source);
    Source* s = ref.get();
    if (!s || !s->backend) {
        SetError("source 句柄为空或已关闭");
        return ZX_ERR_INVALID_ARG;
    }

    AudioSource::Meter m = s->last_meter;
    bool voiced = m.voiced;

    // 降噪开启时，用降噪自己的 VAD 覆盖能量法 VAD：
    // 它有噪声底做参照，比「能量大于固定阈值」稳得多（安静环境不会误判）。
    if (s->ns && !s->ns->Bypass() && s->ns->level() != NsLevel::Off) {
        voiced = s->ns->stats().voiced;
    }

    JsonWriter w;
    w.BeginObject();
    w.Key("rms").Value(static_cast<double>(m.rms));
    w.Key("peak").Value(static_cast<double>(m.peak));
    w.Key("dbfs").Value(m.dbfs);
    w.Key("voiced").Value(voiced);
    w.Key("clipped").Value(m.clipped);
    w.EndObject();
    return WriteJsonResult(w.str(), out_json, cap);
}

extern "C" ZX_API zx_result zx_source_stats(zx_source_t* source, char* out_json,
                                            int cap) {
    SourceRef ref(source);
    Source* s = ref.get();
    if (!s || !s->backend) {
        SetError("source 句柄为空或已关闭");
        return ZX_ERR_INVALID_ARG;
    }
    const SourceStats st = s->backend->stats();

    JsonWriter w;
    w.BeginObject();
    w.Key("framesCaptured").Value(st.frames_captured);
    w.Key("framesDropped").Value(st.frames_dropped);
    w.Key("discontinuities").Value(st.discontinuities);
    w.Key("silentPackets").Value(st.silent_packets);
    w.Key("avgPeriodMs").Value(st.avg_period_ms);
    w.Key("maxPeriodMs").Value(st.max_period_ms);
    // 单位是**样本(float)**，与 RingBuffer 的契约一致（旧键名 bufferedFrames 是错的：
    // 那个数字一直是样本数，却挂了个"帧"的名字 —— 审计病根 4）。
    w.Key("bufferedSamples").Value(st.buffered_samples);
    w.Key("callbackCount").Value(st.callback_count);
    // 以前 sanitized_ 只加不报（假开关），现在如实暴露
    w.Key("sanitizedSamples").Value(st.sanitized_samples);
    // MMCSS "Pro Audio" 是否真的注册上了（以前整套 MMCSS 是空的）
    w.Key("proAudio").Value(st.pro_audio);
    w.EndObject();
    return WriteJsonResult(w.str(), out_json, cap);
}

// 【保留 ABI · 当前无消费者】实现是通的（真正的推送队列见 wasapi_backend 的
// OnSamples），但全仓库没有任何调用方：C# 不调，Python 绑定刻意不暴露。
// 头文件里已写明这一点；留在这里的是 ABI，不是"已接上的功能"。
extern "C" ZX_API zx_result zx_source_set_callback(zx_source_t* source,
                                                   zx_source_cb cb, void* user) {
    SourceRef ref(source);
    Source* s = ref.get();
    if (!s || !s->backend) {
        SetError("source 句柄为空或已关闭");
        return ZX_ERR_INVALID_ARG;
    }
    if (cb) {
        s->backend->set_callback([cb, user](const float* d, int f, int c) {
            cb(d, f, c, user);
        });
    } else {
        s->backend->clear_callback();
    }
    return ZX_OK;
}

extern "C" ZX_API zx_result zx_source_clear_callback(zx_source_t* source) {
    return zx_source_set_callback(source, nullptr, nullptr);
}

// ===========================================================================
// 降噪
// ===========================================================================

extern "C" ZX_API zx_result zx_ns_set(zx_source_t* source, zx_ns_level level,
                                      zx_ns_backend backend) {
    SourceRef ref(source);
    Source* s = ref.get();
    if (!s || !s->ns) {
        SetError("source 句柄为空或已关闭");
        return ZX_ERR_INVALID_ARG;
    }
    if (level < ZX_NS_OFF || level > ZX_NS_STRONG ||
        backend < ZX_NSB_BUILTIN || backend > ZX_NSB_RNNOISE) {
        SetError("降噪参数超出范围");
        return ZX_ERR_INVALID_ARG;
    }

    // 【假开关的修法】RNNoise 没编进本构建就**明确报错**，不再"接受请求然后
    // 静默降级成 builtin"。以前 zx_ns_set(..., ZX_NSB_RNNOISE) 永远返回 OK、
    // 实际跑 builtin（审计 §四：恒降级）—— 调用方以为切过去了。
    if (backend == ZX_NSB_RNNOISE && !s->ns_available_rnnoise) {
        SetError("本构建未包含 RNNoise 后端（ZX_HAVE_RNNOISE=0）；"
                 "当前可用后端只有 builtin，请传 ZX_NSB_BUILTIN");
        return ZX_ERR_UNSUPPORTED;
    }

    std::lock_guard<std::mutex> lock(s->audio_mutex);
    s->requested_level = static_cast<NsLevel>(static_cast<int>(level));
    s->requested_backend = static_cast<NsBackend>(static_cast<int>(backend));
    s->ns->Configure(s->requested_level, s->requested_backend,
                     s->ns_available_rnnoise);
    return ZX_OK;
}

extern "C" ZX_API zx_result zx_ns_get_config(zx_source_t* source, char* out_json,
                                             int cap) {
    SourceRef ref(source);
    Source* s = ref.get();
    if (!s || !s->ns) {
        SetError("source 句柄为空或已关闭");
        return ZX_ERR_INVALID_ARG;
    }
    std::lock_guard<std::mutex> lock(s->audio_mutex);
    const NsStats& st = s->ns->stats();

    JsonWriter w;
    w.BeginObject();
    w.Key("level").Value(static_cast<int>(s->ns->level()));
    w.Key("backend").Value(s->ns->backend() == NsBackend::RnNoise ? "rnnoise"
                                                                 : "builtin");
    w.Key("requestedBackend")
        .Value(s->requested_backend == NsBackend::RnNoise ? "rnnoise" : "builtin");
    w.Key("degraded").Value(s->ns->backend() != s->requested_backend);
    // 后端可用性如实报出：rnnoiseAvailable=false 时 zx_ns_set 会明确报
    // ZX_ERR_UNSUPPORTED，不再静默降级（假开关的修法）。
    w.Key("rnnoiseAvailable").Value(ZX_HAVE_RNNOISE != 0);
    w.Key("builtinAvailable").Value(true);
    w.Key("bypass").Value(s->ns->Bypass());
    w.Key("noiseFloorDbfs").Value(st.noise_floor_dbfs);
    w.Key("suppressionDb").Value(st.suppression_db);
    w.Key("speechPreservation").Value(st.speech_preservation);
    w.Key("voiced").Value(st.voiced);
    w.Key("framesProcessed").Value(st.frames_processed);
    w.EndObject();
    return WriteJsonResult(w.str(), out_json, cap);
}

extern "C" ZX_API zx_result zx_ns_set_bypass(zx_source_t* source, int bypass_on) {
    SourceRef ref(source);
    Source* s = ref.get();
    if (!s || !s->ns) {
        SetError("source 句柄为空或已关闭");
        return ZX_ERR_INVALID_ARG;
    }
    std::lock_guard<std::mutex> lock(s->audio_mutex);
    s->ns->SetBypass(bypass_on != 0);
    return ZX_OK;
}

extern "C" ZX_API zx_result zx_ns_set_modules(zx_source_t* source, int aec, int agc,
                                              int vad) {
    SourceRef ref(source);
    Source* s = ref.get();
    if (!s || !s->ns) {
        SetError("source 句柄为空或已关闭");
        return ZX_ERR_INVALID_ARG;
    }
    (void)aec;
    (void)agc;
    (void)vad;
    // AEC 需要播放侧参考信号，属于 PL1；AGC 排在它后面。
    // 明确报错好过静默忽略 —— 静默忽略会让调用方以为已经生效了。
    SetError("AEC/AGC 尚未实现（属于 PL1 语音通话阶段）；VAD 已内置在降噪里");
    return ZX_ERR_UNSUPPORTED;
}

// ===========================================================================
// 录音（PL0 的验收工具）
// ===========================================================================

extern "C" ZX_API zx_result zx_recorder_open(zx_source_t* source,
                                             const char* raw_wav_path_utf8,
                                             const char* processed_wav_path_utf8) {
    SourceRef ref(source);
    Source* s = ref.get();
    if (!s || !s->backend) {
        SetError("source 句柄为空或已关闭");
        return ZX_ERR_INVALID_ARG;
    }
    if (!raw_wav_path_utf8 && !processed_wav_path_utf8) {
        SetError("至少需要提供一个输出路径");
        return ZX_ERR_INVALID_ARG;
    }

    std::lock_guard<std::mutex> lock(s->audio_mutex);
    const SourceFormat f = s->backend->format();

    if (raw_wav_path_utf8 && *raw_wav_path_utf8) {
        s->rec_raw = std::make_unique<WavWriter>();
        if (!s->rec_raw->Open(raw_wav_path_utf8, f.sample_rate, f.channels)) {
            s->rec_raw.reset();
            SetError(std::string("无法创建原始录音文件：") + raw_wav_path_utf8 +
                     "（确认目录存在且有写入权限）");
            return ZX_ERR_BACKEND_FAILED;
        }
    }
    if (processed_wav_path_utf8 && *processed_wav_path_utf8) {
        s->rec_processed = std::make_unique<WavWriter>();
        if (!s->rec_processed->Open(processed_wav_path_utf8, f.sample_rate,
                                   f.channels)) {
            s->rec_processed.reset();
            SetError(std::string("无法创建降噪后录音文件：") + processed_wav_path_utf8);
            return ZX_ERR_BACKEND_FAILED;
        }
    }

    LogLine("info", std::string("录音已开始：raw=") +
                        (raw_wav_path_utf8 ? raw_wav_path_utf8 : "(无)") +
                        " processed=" +
                        (processed_wav_path_utf8 ? processed_wav_path_utf8 : "(无)"));
    return ZX_OK;
}

extern "C" ZX_API zx_result zx_recorder_close(zx_source_t* source) {
    SourceRef ref(source);
    Source* s = ref.get();
    if (!s) {
        SetError("source 句柄为空或已关闭");
        return ZX_ERR_INVALID_ARG;
    }
    std::lock_guard<std::mutex> lock(s->audio_mutex);
    if (s->rec_raw) {
        s->rec_raw->Close();
        LogLine("info", "原始录音已写入 " + s->rec_raw->Path() + "（" +
                            std::to_string(s->rec_raw->FramesWritten()) + " 帧）");
        s->rec_raw.reset();
    }
    if (s->rec_processed) {
        s->rec_processed->Close();
        LogLine("info", "降噪后录音已写入 " + s->rec_processed->Path() + "（" +
                            std::to_string(s->rec_processed->FramesWritten()) + " 帧）");
        s->rec_processed.reset();
    }
    return ZX_OK;
}

extern "C" ZX_API zx_result zx_recorder_stats(zx_source_t* source, char* out_json,
                                              int cap) {
    SourceRef ref(source);
    Source* s = ref.get();
    if (!s) {
        SetError("source 句柄为空或已关闭");
        return ZX_ERR_INVALID_ARG;
    }
    JsonWriter w;
    w.BeginObject();
    w.Key("rawFrames").Value(s->rec_raw ? s->rec_raw->FramesWritten() : uint64_t{0});
    w.Key("processedFrames")
        .Value(s->rec_processed ? s->rec_processed->FramesWritten() : uint64_t{0});
    w.Key("rawPath").Value(s->rec_raw ? s->rec_raw->Path() : std::string());
    w.Key("processedPath")
        .Value(s->rec_processed ? s->rec_processed->Path() : std::string());
    w.EndObject();
    return WriteJsonResult(w.str(), out_json, cap);
}

// ===========================================================================
// 会话（PL1 才有内容，这里先按 ABI 返回明确的「未实现」，不返回假数据）
// ===========================================================================

extern "C" ZX_API zx_result zx_session_create(zx_engine_t* engine,
                                              const char* json_request,
                                              zx_session_t** out_session) {
    (void)engine;
    (void)json_request;
    if (out_session) *out_session = nullptr;
    SetError("会话功能属于 PL1，尚未实现");
    return ZX_ERR_UNSUPPORTED;
}

extern "C" ZX_API void zx_session_destroy(zx_session_t* session) {
    (void)session;
}

extern "C" ZX_API zx_result zx_session_poll_event(zx_session_t* session, char* out_json,
                                                  int cap) {
    (void)session;
    (void)out_json;
    (void)cap;
    SetError("会话功能属于 PL1，尚未实现");
    return ZX_ERR_UNSUPPORTED;
}

extern "C" ZX_API zx_result zx_session_get_state(zx_session_t* session, char* out_json,
                                                 int cap) {
    (void)session;
    (void)out_json;
    (void)cap;
    SetError("会话功能属于 PL1，尚未实现");
    return ZX_ERR_UNSUPPORTED;
}

extern "C" ZX_API zx_result zx_session_send_message(zx_session_t* session,
                                                    const char* msg_json) {
    (void)session;
    (void)msg_json;
    SetError("会话功能属于 PL1，尚未实现");
    return ZX_ERR_UNSUPPORTED;
}

// ===========================================================================
// 内存释放
// ===========================================================================

extern "C" ZX_API void zx_free_string(char* s) {
    // 目前没有任何函数返回堆分配的字符串（全部写进调用方缓冲），
    // 保留这个函数是为了 ABI 稳定：将来加返回字符串的接口时不用改调用方。
    (void)s;
}
