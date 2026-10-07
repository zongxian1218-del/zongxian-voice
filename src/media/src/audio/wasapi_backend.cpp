// audio/wasapi_backend.cpp —— Windows 音频采集后端
//
// 覆盖三种采集形态，全部走同一套 IAudioClient 接口：
//   · Mic        麦克风（默认采集设备）
//   · Loopback   全部应用音频（在渲染设备上开 LOOPBACK，Vista+ 就有）
//   · Process    单个应用音频（进程回环，ActivateAudioInterfaceAsync 激活）
//
// 三个容易踩死人的点，都在这份实现里处理了：
//   1) ActivateAudioInterfaceAsync 可能在**同一个线程上同步**调用完成回调。
//      如果实现成「发起后等信号量」，就会自己等自己，直接死锁。
//      所以完成状态用 atomic 标志判断，先检查再等待。
//   2) 进程回环必须用 PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE。
//      Chrome/Electron 的音频在子进程里渲染，只抓主进程会得到断断续续的声音。
//   3) 进程回环的格式由系统定死（48kHz / 32bit float / 2ch，不支持 GetMixFormat），
//      但**必须**自己拿这个格式调一次 IAudioClient::Initialize，并且必须带
//      EVENTCALLBACK —— 否则 SetEventHandle 报 AUDCLNT_E_EVENTHANDLE_NOT_EXPECTED。
//      （旧注释写"不能再调 Initialize"，那句是错的，2026-10-06 已实测更正。）
//   4) 端点采集的采样率不是常量：设备混音格式可能是 44.1k。这里如实把它报给上层
//      （SourceFormat::device_sample_rate），并在设备支持时用 AUTOCONVERTPCM
//      真正重采样到请求的采样率；不支持就回退，绝不假装是 48k。
#include "audio_engine.h"
#include "ring_buffer.h"
#include "text_util.h"

#ifdef _WIN32

#include <windows.h>

// ── Windows 音频头的包含顺序（改之前务必读一遍）─────────────────────────
// 只需要两步，顺序是硬要求：
//   1. mmdeviceapi.h     定义 PROPERTYKEY / DEFINE_PROPERTYKEY 等基础类型。
//   2. functiondiscoverykeys_devpkey.h
//                        里面的 PKEY_* 依赖第 1 步。反了会报
//                        "PKEY_NAME 未声明" 与 "DEFINE_PROPERTYKEY 重定义"，
//                        而且报错点指向 SDK 头文件，极易被误判成 SDK 有问题。
//
// 刻意**不**引入 initguid.h：那些 PKEY_* 在头文件里是 extern 声明，
// 真正的 GUID 定义在系统的 uuid.lib 里（已在本项目的 CMake 里链接）。
// 这样做的好处是不必去动 DEFINE_PROPERTYKEY 宏，也不会有
// initguid.h / cguid.h 的头文件顺序冲突（那个冲突的报错是
// "cguid.h: 语法错误 __uuidof"，离真正原因很远）。
#include <mmdeviceapi.h>

#include <audioclient.h>
#include <audioclientactivationparams.h>
#include <audiopolicy.h>
#include <avrt.h>          // AvSetMmThreadCharacteristicsW（MMCSS "Pro Audio"）
#include <functiondiscoverykeys_devpkey.h>
#include <ksmedia.h>
#include <processthreadsapi.h>
#include <wrl/client.h>
#include <wrl/implements.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

using Microsoft::WRL::ComPtr;

namespace zx {

namespace {

// 进程回环的固定虚拟设备 id。这个字符串不是随便起的，是系统约定的。
const wchar_t* const kProcessLoopbackDeviceId = L"VAD\\Process_Loopback";

// 进程回环的激活参数（PROPVARIANT 里要放这个结构）
AUDIOCLIENT_ACTIVATION_PARAMS MakeProcessLoopbackParams(
    DWORD pid, PROCESS_LOOPBACK_MODE mode) {
    AUDIOCLIENT_ACTIVATION_PARAMS p{};
    p.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    p.ProcessLoopbackParams.TargetProcessId = pid;
    p.ProcessLoopbackParams.ProcessLoopbackMode = mode;
    return p;
}

PROPVARIANT MakeBlobPropVariant(AUDIOCLIENT_ACTIVATION_PARAMS* params) {
    PROPVARIANT pv{};
    pv.vt = VT_BLOB;
    pv.blob.cbSize = sizeof(*params);
    pv.blob.pBlobData = reinterpret_cast<BYTE*>(params);
    return pv;
}

// ---------------------------------------------------------------------------
// 设备枚举
//
// 用 PKEY_Device_FriendlyName 拿面向用户的名字（"麦克风 (USB Audio Device)"），
// 而不是 PKEY_Device_DeviceDesc（"USB Audio Device"）—— 前者是用户在系统设置里
// 看到的名字，让他能对得上是同一个设备。
// ---------------------------------------------------------------------------
std::vector<DeviceInfo> EnumerateByFlow(EDataFlow flow, const char* kind) {
    std::vector<DeviceInfo> out;

    ComPtr<IMMDeviceEnumerator> enumerator;
    HRESULT hr = ::CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr,
                                    CLSCTX_ALL,
                                    IID_PPV_ARGS(enumerator.GetAddressOf()));
    if (FAILED(hr)) return out;

    // 记下默认设备 id，用于给 isDefault 打标
    std::wstring default_id;
    {
        ComPtr<IMMDevice> def;
        if (SUCCEEDED(enumerator->GetDefaultAudioEndpoint(flow, eConsole,
                                                         def.GetAddressOf())) &&
            def) {
            LPWSTR id = nullptr;
            if (SUCCEEDED(def->GetId(&id)) && id) {
                default_id = id;
                ::CoTaskMemFree(id);
            }
        }
    }

    ComPtr<IMMDeviceCollection> collection;
    hr = enumerator->EnumAudioEndpoints(flow, DEVICE_STATE_ACTIVE,
                                        collection.GetAddressOf());
    if (FAILED(hr) || !collection) return out;

    UINT count = 0;
    if (FAILED(collection->GetCount(&count))) return out;

    for (UINT i = 0; i < count; ++i) {
        ComPtr<IMMDevice> device;
        if (FAILED(collection->Item(i, device.GetAddressOf())) || !device) continue;

        LPWSTR wid = nullptr;
        if (FAILED(device->GetId(&wid)) || !wid) continue;
        const std::wstring wdevice_id = wid;
        ::CoTaskMemFree(wid);

        DeviceInfo info;
        info.id = WideToUtf8(wdevice_id.c_str());
        info.kind = kind;
        info.is_default = (!default_id.empty() && wdevice_id == default_id);

        ComPtr<IPropertyStore> props;
        if (SUCCEEDED(device->OpenPropertyStore(STGM_READ, props.GetAddressOf())) &&
            props) {
            PROPVARIANT pv;
            ::PropVariantInit(&pv);
            if (SUCCEEDED(props->GetValue(PKEY_Device_FriendlyName, &pv)) &&
                pv.vt == VT_LPWSTR && pv.pwszVal) {
                info.name = WideToUtf8(pv.pwszVal);
            }
            ::PropVariantClear(&pv);
        }
        if (info.name.empty()) info.name = info.is_default ? "(默认设备)" : "(未命名设备)";

        // 顺带把混音格式带上：界面要显示「48 kHz / 2 声道」，
        // 而且打开源之前就能知道格式，可以提前避免不必要的重采样。
        ComPtr<IAudioClient> client;
        if (SUCCEEDED(device->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr,
                                       reinterpret_cast<void**>(client.GetAddressOf()))) &&
            client) {
            WAVEFORMATEX* mix = nullptr;
            if (SUCCEEDED(client->GetMixFormat(&mix)) && mix) {
                info.channels = static_cast<int>(mix->nChannels);
                info.sample_rate = static_cast<int>(mix->nSamplesPerSec);
                ::CoTaskMemFree(mix);
            }
        }
        out.push_back(std::move(info));
    }
    return out;
}

// ---------------------------------------------------------------------------
// 完成回调处理器
//
// 关键：ActivateAudioInterfaceAsync 有可能在调用者的线程上**同步**触发回调
// （官方文档明确说了这一点）。所以用一个 atomic 标志 + 互斥量 + 条件变量，
// 等待前先检查标志是否已置位，避免自己等自己。
//
// 为什么必须带上 FtmBase（这是踩过的坑）：
//   系统要把回调接口跨套间传给我们时，会尝试 marshal 这个对象。
//   只实现 ClassicCom 的 RuntimeClass 没有 IMarshal，COM 会以一种
//   "client teardown" 的方式让进程直接崩掉（表现为 0xC0000374 堆损坏），
//   而且崩溃点离真正的原因很远。FtmBase 提供自由线程 marshaler，
//   让这个对象可以被跨套间安全传递。
// ---------------------------------------------------------------------------
class ActivationHandler
    : public Microsoft::WRL::RuntimeClass<
          Microsoft::WRL::RuntimeClassFlags<Microsoft::WRL::ClassicCom>,
          IActivateAudioInterfaceCompletionHandler,
          Microsoft::WRL::FtmBase> {
public:
    ActivationHandler() = default;

    HRESULT STDMETHODCALLTYPE ActivateCompleted(
        IActivateAudioInterfaceAsyncOperation* operation) override {
        HRESULT hr_activate = E_FAIL;
        ComPtr<IUnknown> activated;
        HRESULT hr_get = operation->GetActivateResult(
            &hr_activate, activated.GetAddressOf());

        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (SUCCEEDED(hr_get) && SUCCEEDED(hr_activate)) {
                result_ = activated;
                hr_ = S_OK;
            } else {
                hr_ = SUCCEEDED(hr_get) ? hr_activate : hr_get;
            }
            done_ = true;
        }
        cv_.notify_all();
        return S_OK;
    }

    // 等待完成，并取回激活出的接口。超时返回 false。
    bool WaitFor(std::chrono::milliseconds timeout, ComPtr<IUnknown>* out,
                 HRESULT* hr_out) {
        std::unique_lock<std::mutex> lock(mutex_);
        // 先检查 done_：如果回调是同步发生的，这里已经为 true，直接返回。
        if (!done_) {
            if (!cv_.wait_for(lock, timeout, [this] { return done_; })) {
                return false;
            }
        }
        if (out) *out = result_;
        if (hr_out) *hr_out = hr_;
        return true;
    }

private:
    std::mutex mutex_;
    std::condition_variable cv_;
    bool done_ = false;
    HRESULT hr_ = E_FAIL;
    ComPtr<IUnknown> result_;
};

// ---------------------------------------------------------------------------
// 采样格式转换：任意 WASAPI 混音格式 → float32 交错 / 目标声道数
//
// 支持的输入：32-bit float、16-bit PCM、32-bit PCM、24-bit PCM。
// 这不是过度设计：loopback 在渲染端通常是 float，但进程回环与某些声卡
// 会给出 PCM 格式，硬编码 float 会在别人机器上炸。
// ---------------------------------------------------------------------------
int ConvertToFloatInterleaved(const BYTE* src, UINT32 src_frames,
                              const WAVEFORMATEX* fmt,
                              int target_channels, float* dst) {
    const int src_ch =
        fmt->nChannels > 0 ? static_cast<int>(fmt->nChannels) : 1;
    const int tgt_ch = target_channels > 0 ? target_channels : 1;
    const WORD bits = fmt->wBitsPerSample;

    for (UINT32 f = 0; f < src_frames; ++f) {
        // 目标每个声道都从源里取「同序号或最后一个」声道，保证不会越界
        for (int c = 0; c < tgt_ch; ++c) {
            const int sc = c < src_ch ? c : src_ch - 1;
            float v = 0.0f;

            if (fmt->wFormatTag == WAVE_FORMAT_IEEE_FLOAT && bits == 32) {
                v = reinterpret_cast<const float*>(src)[f * src_ch + sc];
            } else if (bits == 16) {
                const short s = reinterpret_cast<const short*>(src)[f * src_ch + sc];
                v = static_cast<float>(s) / 32768.0f;
            } else if (bits == 32) {
                const int32_t s = reinterpret_cast<const int32_t*>(src)[f * src_ch + sc];
                v = static_cast<float>(s) / 2147483648.0f;
            } else if (bits == 24) {
                const BYTE* p = src + (static_cast<size_t>(f) * src_ch + sc) * 3;
                int32_t s = (static_cast<int32_t>(p[2]) << 24) |
                            (static_cast<int32_t>(p[1]) << 16) |
                            (static_cast<int32_t>(p[0]) << 8);
                v = static_cast<float>(s >> 8) / 8388608.0f;
            }
            dst[f * tgt_ch + c] = v;
        }
    }
    return static_cast<int>(src_frames);
}

// ---------------------------------------------------------------------------
// WasapiSource
// ---------------------------------------------------------------------------
class WasapiSource : public AudioSource {
public:
    WasapiSource() = default;
    ~WasapiSource() override { Stop(); }

    // 打开。失败时把原因写进 reason，让上层能原样展示给用户。
    bool Open(const SourceConfig& cfg, std::string* reason) {
        cfg_ = cfg;

        ComPtr<IMMDeviceEnumerator> enumerator;
        HRESULT hr = ::CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr,
                                        CLSCTX_ALL,
                                        IID_PPV_ARGS(enumerator.GetAddressOf()));
        if (FAILED(hr)) {
            Fail(reason, "无法创建音频设备枚举器", hr);
            return false;
        }

        ComPtr<IMMDevice> device;
        if (cfg.kind == SourceKind::Process) {
            if (cfg.process_id == 0) {
                if (reason) *reason = "进程回环需要指定 processId";
                return false;
            }
            // 进程回环用的是虚拟设备，但仍要先枚举默认渲染设备做格式参考
            enumerator->GetDefaultAudioEndpoint(eRender, eConsole,
                                                device.GetAddressOf());
        } else {
            const EDataFlow flow =
                (cfg.kind == SourceKind::Loopback) ? eRender : eCapture;
            if (cfg.device_id.empty()) {
                hr = enumerator->GetDefaultAudioEndpoint(flow, eConsole,
                                                        device.GetAddressOf());
            } else {
                const std::wstring wid = Utf8ToWide(cfg.device_id);
                hr = enumerator->GetDevice(wid.c_str(), device.GetAddressOf());
            }
            if (FAILED(hr)) {
                Fail(reason, "找不到指定的音频设备（可能已拔出）", hr);
                return false;
            }
        }

        if (cfg.kind == SourceKind::Process) {
            return OpenProcessLoopback(cfg, reason);
        }
        return OpenEndpointCapture(cfg, reason);
    }

    bool Start() override {
        if (!client_) return false;
        if (running_.load()) return true;
        if (!event_) return false;

        HRESULT hr = client_->Start();
        if (FAILED(hr)) return false;

        running_.store(true);
        thread_ = std::thread([this] { CaptureLoop(); });
        return true;
    }

    void Stop() override {
        if (!running_.exchange(false)) {
            if (thread_.joinable()) thread_.join();
            return;
        }
        // 唤醒可能正在等待事件的采集线程，否则它会一直阻塞到下一个周期
        if (event_) ::SetEvent(static_cast<HANDLE>(event_));
        if (thread_.joinable()) thread_.join();
        if (client_) client_->Stop();
        // 唤醒还在 Read() 里等的调用者
        cv_.notify_all();
    }

    int Read(float* out, int cap_frames, int timeout_ms) override {
        if (!out || cap_frames <= 0) return 0;

        const auto deadline =
            std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        std::unique_lock<std::mutex> lock(read_mutex_);

        // 先尽量把缓冲里的数据都取出来（编码器/录音希望的是一次拿到一批），
        // 不够再等新的。这样既不会为了"凑满"而增加延迟，也不会一次只返回
        // 一个 10ms 块导致调用方循环次数暴增。
        //
        // 【单位】cap_frames 是**帧**，RingBuffer 按**样本(float)**计数
        // （见 ring_buffer.h 的单位契约），所以进出一律换算成样本数，
        // 最后再折算回帧返回。以前直接在帧与样本之间混算（total += n，
        // 而 n 是样本数），结果是返回的帧数与实际写入的数据对不上、
        // 缓冲尾部是旧数据。
        if (!ring_) return 0;   // 没 Open 成功过：没有缓冲可读
        int total_samples = 0;
        for (;;) {
            const int ch = format_.channels > 0 ? format_.channels : 1;
            const int max_samples = cap_frames * ch;
            const int n = ring_->Read(out + total_samples, max_samples - total_samples);
            total_samples += n;
            if (total_samples >= max_samples) return total_samples / ch;
            if (total_samples > 0 && n == 0) return total_samples / ch;   // 已经拿到一些，先交付

            // 缓冲空了：等采集线程写进来。这不是忙等，采集线程会 notify。
            if (!cv_.wait_until(lock, deadline, [this] {
                    return ring_->AvailableSamples() > 0 || !running_.load();
                })) {
                return total_samples / ch;   // 超时；已经拿到的部分照常返回
            }
            if (!running_.load() && ring_->AvailableSamples() == 0) return total_samples / ch;
        }
    }

    SourceFormat format() const override { return format_; }

    // 【原子快照】采集线程写、任意线程读。以前直接读 stats_ 是数据竞争：
    // 读侧可能拿到撕裂的计数（frames_captured 与 avg_period_ms 来自不同的两轮）。
    // 现在写侧用 seqlock（永不阻塞实时线程），读侧重试到拿到一致的一份。
    SourceStats stats() const override {
        const StatsSnapshot snap = SnapshotStats();
        SourceStats s;
        s.frames_captured = snap.frames_captured;
        s.discontinuities = snap.discontinuities;
        s.silent_packets = snap.silent_packets;
        s.callback_count = snap.callback_count;
        s.sanitized_samples = snap.sanitized_samples;
        s.avg_period_ms = snap.avg_period_ms;
        s.max_period_ms = snap.max_period_ms;
        s.pro_audio = snap.pro_audio;
        if (ring_) {
            s.buffered_samples = ring_->AvailableSamples();
            // 环形缓冲的溢出和 WASAPI 的数据断流是两件事，都要如实报出来
            s.frames_dropped = ring_->DroppedSamples();
        }
        return s;
    }

    void set_callback(SourceCallback cb) override { callback_ = std::move(cb); }
    void clear_callback() override { callback_ = nullptr; }

    Meter meter() const override {
        std::lock_guard<std::mutex> lock(meter_mutex_);
        return meter_;
    }

private:
    void Fail(std::string* reason, const char* what, HRESULT hr) {
        char buf[256];
        std::snprintf(buf, sizeof(buf), "%s (HRESULT 0x%08lX)",
                      what, static_cast<unsigned long>(hr));
        if (reason) *reason = buf;
    }

    // 打开普通端点采集（麦克风 / loopback）
    bool OpenEndpointCapture(const SourceConfig& cfg, std::string* reason) {
        ComPtr<IMMDevice> device;
        ComPtr<IMMDeviceEnumerator> enumerator;
        ::CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
                           IID_PPV_ARGS(enumerator.GetAddressOf()));
        const EDataFlow flow =
            (cfg.kind == SourceKind::Loopback) ? eRender : eCapture;
        if (cfg.device_id.empty()) {
            enumerator->GetDefaultAudioEndpoint(flow, eConsole, device.GetAddressOf());
        } else {
            enumerator->GetDevice(Utf8ToWide(cfg.device_id).c_str(),
                                  device.GetAddressOf());
        }
        if (!device) {
            if (reason) *reason = "无法取得音频设备";
            return false;
        }

        HRESULT hr = device->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr,
                                      reinterpret_cast<void**>(client_.GetAddressOf()));
        if (FAILED(hr)) {
            Fail(reason, "无法激活音频客户端", hr);
            return false;
        }

        // 取设备混音格式。loopback 必须用这个格式，不能自己指定。
        WAVEFORMATEX* mix = nullptr;
        hr = client_->GetMixFormat(&mix);
        if (FAILED(hr) || !mix) {
            Fail(reason, "无法获取设备混音格式", hr);
            return false;
        }
        const int device_rate = static_cast<int>(mix->nSamplesPerSec);

        // ── 采样率契约（审计真 bug 11 / 架构 §6）───────────────────────────────
        // 以前这里无论设备是多少都直接用混音格式，于是 44.1k 设备上流就是 44.1k，
        // 而工具与应用按 48000 硬算 ⇒ 静默变调 + 时长错。
        // 现在两条同时做：
        //   1) 端点采集（麦克风）请求想要的采样率（来自参数，默认 48k），
        //      并真正用上 AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM｜SRC_DEFAULT_QUALITY
        //      —— 这两个标志以前永远空转（请求的就是混音格式，等于没让系统转）。
        //      由系统的 SRC 重采样到目标采样率，音高因此正确。
        //   2) 系统不支持时**回退**到设备混音格式，并把 device_sample_rate /
        //      resampled 如实报给上层；上层按 format().sample_rate 算时长与
        //      重采样，绝不假设 48000。
        // loopback 不能走 1)：回环必须用设备混音格式，叠加 AUTOCONVERTPCM 行为未定义。
        const int want_rate = cfg.want_sample_rate > 0 ? cfg.want_sample_rate : device_rate;
        const bool can_resample = (cfg.kind != SourceKind::Loopback);

        const int period_ms = 10;
        const REFERENCE_TIME dur = period_ms * 10000;   // 100ns 单位
        DWORD flags = AUDCLNT_STREAMFLAGS_EVENTCALLBACK;
        if (cfg.kind == SourceKind::Loopback) {
            flags |= AUDCLNT_STREAMFLAGS_LOOPBACK;
            // 回环本来就必须用设备混音格式（下面用的就是 GetMixFormat），
            // 不需要也不能重采样。
        }

        // 目标格式 = 混音格式换一个采样率（声道/位深/channel mask 原样保留，
        // 所以必须连 cbSize 后面的扩展数据一起拷）。
        std::vector<BYTE> target_bytes;
        WAVEFORMATEX* init_fmt = mix;
        bool requested_resample = false;
        if (can_resample && want_rate != device_rate) {
            target_bytes.assign(reinterpret_cast<BYTE*>(mix),
                                reinterpret_cast<BYTE*>(mix) + sizeof(WAVEFORMATEX) +
                                    mix->cbSize);
            WAVEFORMATEX* target = reinterpret_cast<WAVEFORMATEX*>(target_bytes.data());
            target->nSamplesPerSec = static_cast<DWORD>(want_rate);
            target->nAvgBytesPerSec = target->nSamplesPerSec * target->nBlockAlign;
            init_fmt = target;
            flags |= AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM |
                     AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY;
            requested_resample = true;
        }

        hr = client_->Initialize(AUDCLNT_SHAREMODE_SHARED, flags, dur, 0, init_fmt, nullptr);
        if (FAILED(hr) && requested_resample) {
            // 这台设备的驱动不支持 AUTOCONVERTPCM：**如实回退**到设备混音格式，
            // 由上层按真实采样率处理，而不是假装成功（更不许静默变调）。
            flags = AUDCLNT_STREAMFLAGS_EVENTCALLBACK;
            if (cfg.kind == SourceKind::Loopback) flags |= AUDCLNT_STREAMFLAGS_LOOPBACK;
            init_fmt = mix;
            requested_resample = false;
            hr = client_->Initialize(AUDCLNT_SHAREMODE_SHARED, flags, dur, 0, init_fmt,
                                     nullptr);
        }
        if (FAILED(hr)) {
            Fail(reason, "音频客户端初始化失败", hr);
            ::CoTaskMemFree(mix);
            return false;
        }

        // 事件句柄必须在 Start 之前设置，否则 Start 会返回 AUDCLNT_E_EVENTHANDLE_NOT_SET
        event_ = ::CreateEventW(nullptr, FALSE, FALSE, nullptr);
        if (!event_) {
            if (reason) *reason = "无法创建音频事件句柄";
            ::CoTaskMemFree(mix);
            return false;
        }
        hr = client_->SetEventHandle(static_cast<HANDLE>(event_));
        if (FAILED(hr)) {
            Fail(reason, "设置音频事件句柄失败", hr);
            ::CoTaskMemFree(mix);
            return false;
        }

        hr = client_->GetService(IID_PPV_ARGS(capture_.GetAddressOf()));
        if (FAILED(hr)) {
            Fail(reason, "无法取得采集客户端接口", hr);
            ::CoTaskMemFree(mix);
            return false;
        }

        // 记下格式，供转换使用。这里用的是**实际 Initialize 成功的那份格式**
        // （可能是重采样后的目标格式，也可能是回退后的设备混音格式）。
        src_format_ = *init_fmt;
        src_format_bytes_.assign(reinterpret_cast<BYTE*>(init_fmt),
                                 reinterpret_cast<BYTE*>(init_fmt) + sizeof(WAVEFORMATEX) +
                                     init_fmt->cbSize);
        ::CoTaskMemFree(mix);

        format_.sample_rate = static_cast<int>(src_format_.nSamplesPerSec);
        format_.device_sample_rate = device_rate;
        format_.resampled = (format_.sample_rate != device_rate);
        format_.channels = cfg.want_channels > 0 ? cfg.want_channels : 1;
        format_.bits_per_sample = 32;
        format_.is_float = true;
        format_.block_frames = cfg.block_frames;
        format_.period_ms = static_cast<double>(period_ms);
        ring_ = std::make_unique<RingBuffer>(
            format_.sample_rate * (format_.channels > 0 ? format_.channels : 1));
        return true;
    }

    // 打开进程回环（单个应用音频）。需要 Windows 10 20348+（实测 2004+ 常可用）。
    bool OpenProcessLoopback(const SourceConfig& cfg, std::string* reason) {
        AUDIOCLIENT_ACTIVATION_PARAMS params = MakeProcessLoopbackParams(
            cfg.process_id,
            cfg.include_child_processes
                ? PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
                : PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE);
        PROPVARIANT pv = MakeBlobPropVariant(&params);

        auto handler = Microsoft::WRL::Make<ActivationHandler>();
        ComPtr<IActivateAudioInterfaceAsyncOperation> op;
        HRESULT hr = ::ActivateAudioInterfaceAsync(
            kProcessLoopbackDeviceId, __uuidof(IAudioClient), &pv,
            handler.Get(), op.GetAddressOf());
        // 【真凶已定位 2026-10-06】这里**绝对不能**调 PropVariantClear：
        // pv 是 VT_BLOB，pBlobData 指向**本函数栈上**的 params；VT_BLOB 的清理会
        // 对 pBlobData 调 CoTaskMemFree —— 对栈指针调释放 = 堆损坏 0xC0000374。
        // 证据：官方写法的最小复现（不清理）成功激活；我们的代码（清理）必崩。
        (void)params;   // params 是栈变量，生命周期由作用域管，无需释放
        if (FAILED(hr)) {
            Fail(reason, "进程回环激活失败（系统可能不支持该功能）", hr);
            return false;
        }

        ComPtr<IUnknown> activated;
        HRESULT activation_hr = E_FAIL;
        if (!handler->WaitFor(std::chrono::milliseconds(5000), &activated,
                             &activation_hr)) {
            if (reason) *reason = "进程回环激活超时（目标进程可能没有音频输出）";
            return false;
        }
        if (FAILED(activation_hr) || !activated) {
            Fail(reason,
                 "系统拒绝进程回环激活（需要 Windows 10 Build 20348+；"
                 "请用 zx_probe_process_loopback 先探测）",
                 activation_hr);
            return false;
        }
        hr = activated.As(&client_);
        if (FAILED(hr) || !client_) {
            Fail(reason, "进程回环返回的接口类型不对", hr);
            return false;
        }

        // 【2026-10-06 修】进程回环**必须自己调 Initialize**，而且**必须**带
        // AUDCLNT_STREAMFLAGS_EVENTCALLBACK —— 否则后面的 SetEventHandle 会返回
        // AUDCLNT_E_EVENTHANDLE_NOT_EXPECTED(0x88890011)（实测踩到）。
        // 原来这里写着"不能再调 Initialize，格式由系统定死"，那句话是错的：
        // 格式确实是系统定死的（进程回环不支持 GetMixFormat），但要拿这个格式去 Initialize。
        // 下面这三个数字**不是我们挑的**，是进程回环的固定格式（系统定义的不变量），
        // 所以它们不来自参数也不该来自参数；实际值依旧如实经 format() 报出。
        std::memset(&src_format_, 0, sizeof(src_format_));
        src_format_.wFormatTag = WAVE_FORMAT_IEEE_FLOAT;
        src_format_.nChannels = 2;
        src_format_.nSamplesPerSec = 48000;
        src_format_.wBitsPerSample = 32;
        src_format_.nBlockAlign = 2 * 4;
        src_format_.nAvgBytesPerSec = 48000 * 2 * 4;

        hr = client_->Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
            200000, 0, &src_format_, nullptr);
        if (FAILED(hr)) {
            Fail(reason, "进程回环 Initialize 失败", hr);
            return false;
        }

        event_ = ::CreateEventW(nullptr, FALSE, FALSE, nullptr);
        if (!event_) {
            if (reason) *reason = "无法创建音频事件句柄";
            return false;
        }
        hr = client_->SetEventHandle(static_cast<HANDLE>(event_));
        if (FAILED(hr)) {
            Fail(reason, "设置音频事件句柄失败", hr);
            return false;
        }

        hr = client_->GetService(IID_PPV_ARGS(capture_.GetAddressOf()));
        if (FAILED(hr)) {
            Fail(reason, "无法取得采集客户端接口", hr);
            return false;
        }

        format_.sample_rate = static_cast<int>(src_format_.nSamplesPerSec);
        // 进程回环的格式是系统定死的（不支持 GetMixFormat，也不接受重采样），
        // 所以"设备采样率"就等于实际采样率，resampled 恒为 false。
        format_.device_sample_rate = format_.sample_rate;
        format_.resampled = false;
        format_.channels = cfg.want_channels > 0 ? cfg.want_channels : 2;
        format_.bits_per_sample = 32;
        format_.is_float = true;
        format_.block_frames = cfg.block_frames;
        format_.period_ms = 10.0;
        ring_ = std::make_unique<RingBuffer>(
            format_.sample_rate * (format_.channels > 0 ? format_.channels : 1));
        return true;
    }

    // 采集线程：事件驱动，每个周期把数据搬进环形缓冲
    void CaptureLoop() {
        // 这个线程不调 CoCreateInstance，但 IAudioClient 属于 COM 对象，
        // 在未初始化的线程上使用是未定义行为。显式初始化成 MTA，
        // 失败也不致命（主线程已初始化过 COM 时这里会返回 RPC_E_CHANGED_MODE）。
        const HRESULT com_hr = ::CoInitializeEx(nullptr, COINIT_MULTITHREADED);

        // ── MMCSS "Pro Audio"（审计 §四 的假开关之一）────────────────────────
        // 以前只有 engine.cpp 里一句 SetPriorityClass(进程级)，采集线程本身
        // 从没注册过 MMCSS —— 所以注释里说的"音频线程优先级"是空的。
        // 这里真正注册：系统会给这个线程更高的调度优先级和更细的定时器粒度。
        // 拿不到句柄不算致命（系统忙/被策略禁用），如实记进 stats.pro_audio。
        StatsSnapshot snap;
        DWORD mmcss_task = 0;
        HANDLE mmcss = ::AvSetMmThreadCharacteristicsW(L"Pro Audio", &mmcss_task);
        snap.pro_audio = (mmcss != nullptr);

        auto last_tick = std::chrono::steady_clock::now();
        double total_ms = 0.0;
        uint64_t ticks = 0;
        double max_ms = 0.0;

        while (running_.load()) {
            const DWORD wait = ::WaitForSingleObject(static_cast<HANDLE>(event_), 200);
            if (!running_.load()) break;
            if (wait != WAIT_OBJECT_0) {
                // 超时：设备可能被拔了或驱动卡住，记一次断流
                ++snap.discontinuities;
                PublishStats(snap);
                continue;
            }

            for (;;) {
                BYTE* data = nullptr;
                UINT32 frames = 0;
                DWORD flags = 0;
                HRESULT hr = capture_->GetBuffer(&data, &frames, &flags, nullptr, nullptr);
                if (hr == AUDCLNT_S_BUFFER_EMPTY) break;
                if (FAILED(hr)) {
                    ++snap.discontinuities;
                    break;
                }

                if (frames > 0) {
                    // 一次性缓冲，避免在实时线程上反复分配
                    const size_t need =
                        static_cast<size_t>(frames) * format_.channels;
                    if (scratch_.size() < need) scratch_.resize(need);

                    if (data && !(flags & AUDCLNT_BUFFERFLAGS_SILENT)) {
                        ConvertToFloatInterleaved(data, frames, &src_format_,
                                                  format_.channels, scratch_.data());
                        // 【消毒】音频的合法范围就是 [-1, 1]（浮点交叉样本）。
                        // 实测：回环采集在"目标没有输出"的时段会给出**未定义内容**且**不置
                        // SILENT 标志**，于是流里出现 NaN / 3.4e38(FLT_MAX) 这类值
                        // （进程回环 14.6%、整机回环 2.6% 的样本）。这类值不是音频，
                        // 送出去会让对端听到爆音；按静音处理比原样传好，并如实计数。
                        // 计数进 stats.sanitized_samples（以前只加不报 = 假开关）。
                        const size_t total_samples =
                            static_cast<size_t>(frames) * format_.channels;
                        for (size_t i = 0; i < total_samples; ++i) {
                            const float v = scratch_[i];
                            if (!(v >= -1.0f && v <= 1.0f)) {   // 同时挡住 NaN（NaN 比较为 false）
                                scratch_[i] = 0.0f;
                                ++snap.sanitized_samples;
                            }
                        }
                    } else {
                        std::fill(scratch_.begin(), scratch_.begin() + need, 0.0f);
                        ++snap.silent_packets;
                    }
                    if (flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY) {
                        ++snap.discontinuities;
                    }

                    // 电平统计：在采集线程内做，避免读线程再遍历一遍
                    UpdateMeter(scratch_.data(), static_cast<int>(frames));
                    OnSamples(scratch_.data(), static_cast<int>(frames));

                    // 【单位必须是样本】RingBuffer 内部按 **float(样本)** 计数（buffer_ 长 capacity_ 个
                    // float，Write/Read 的返回值也是样本数）。这里以前传的是**音频帧数**，
                    // 于是立体声只写进去一半样本、左右交错错位（麦克风路径同样受影响）。
                    if (ring_) {
                        ring_->Write(scratch_.data(),
                                     static_cast<int>(frames) * format_.channels);
                    }
                    snap.frames_captured += frames;
                    ++snap.callback_count;
                    cv_.notify_all();
                }

                hr = capture_->ReleaseBuffer(frames);
                if (FAILED(hr)) break;
            }

            // 周期统计：这个数字是判断「10ms 是否真的稳」的直接证据
            const auto now = std::chrono::steady_clock::now();
            const double dt =
                std::chrono::duration<double, std::milli>(now - last_tick).count();
            last_tick = now;
            total_ms += dt;
            ++ticks;
            if (dt > max_ms) max_ms = dt;
            snap.avg_period_ms = ticks ? total_ms / static_cast<double>(ticks) : 0.0;
            snap.max_period_ms = max_ms;
            PublishStats(snap);
        }

        if (mmcss) ::AvRevertMmThreadCharacteristics(mmcss);   // 必须成对，否则线程特性泄漏
        if (SUCCEEDED(com_hr)) ::CoUninitialize();
    }

    // ---- 统计快照（seqlock）------------------------------------------------
    // 写侧：采集线程。奇数序号表示"正在写"，写完 +2 表示"这份是完整的"。
    // 读侧：重试到序号为偶数且前后一致。实时线程永不被阻塞。
    struct StatsSnapshot {
        uint64_t frames_captured = 0;
        uint64_t discontinuities = 0;
        uint64_t silent_packets = 0;
        uint64_t callback_count = 0;
        uint64_t sanitized_samples = 0;
        double avg_period_ms = 0.0;
        double max_period_ms = 0.0;
        bool pro_audio = false;
    };

    void PublishStats(const StatsSnapshot& s) {
        const uint32_t seq = stats_seq_.load(std::memory_order_relaxed);
        stats_seq_.store(seq + 1, std::memory_order_relaxed);   // 奇 = 写入中
        std::atomic_thread_fence(std::memory_order_release);
        stats_live_ = s;
        std::atomic_thread_fence(std::memory_order_release);
        stats_seq_.store(seq + 2, std::memory_order_relaxed);
    }

    StatsSnapshot SnapshotStats() const {
        StatsSnapshot s;
        for (;;) {
            const uint32_t before = stats_seq_.load(std::memory_order_acquire);
            if (before & 1u) continue;                 // 写侧正在写：等下一次
            s = stats_live_;
            std::atomic_thread_fence(std::memory_order_acquire);
            if (stats_seq_.load(std::memory_order_relaxed) == before) return s;
        }
    }


    void UpdateMeter(const float* samples, int frames) {
        double sum_sq = 0.0;
        float peak = 0.0f;
        for (int i = 0; i < frames * format_.channels; ++i) {
            const float v = samples[i];
            const float a = std::fabs(v);
            if (a > peak) peak = a;
            sum_sq += static_cast<double>(v) * v;
        }
        const double rms =
            std::sqrt(sum_sq / std::max(1, frames * format_.channels));

        std::lock_guard<std::mutex> lock(meter_mutex_);
        // 指数平滑，避免音量条抖得看不清
        meter_.rms = static_cast<float>(0.7 * meter_.rms + 0.3 * rms);
        meter_.peak = peak;
        meter_.dbfs = 20.0 * std::log10(meter_.rms > 1e-7 ? meter_.rms : 1e-7);
        meter_.voiced = meter_.rms > 0.01f;
        meter_.clipped = peak >= 0.999f;
    }

    void OnSamples(const float* samples, int frames) {
        if (callback_) callback_(samples, frames, format_.channels);
    }

    SourceConfig cfg_;
    ComPtr<IAudioClient> client_;
    ComPtr<IAudioCaptureClient> capture_;
    HANDLE event_ = nullptr;

    SourceFormat format_;
    WAVEFORMATEX src_format_{};
    std::vector<BYTE> src_format_bytes_;

    // 环形缓冲：容量 = 实际采样率 × 声道数 = **1 秒的样本(float)**，足够吸收
    // 任何调度抖动。以前这里写死 `RingBuffer ring_{48000}` 且注释说"1 秒"——
    // 单位其实被当成帧用，立体声就只有半秒；而且在 44.1k 设备上容量也不对。
    // 现在按实际生效的格式在 Open 里建，容量与采样率无关地保持 1 秒。
    std::unique_ptr<RingBuffer> ring_;
    std::atomic<bool> running_{false};
    std::thread thread_;
    SourceCallback callback_;

    std::mutex read_mutex_;
    std::condition_variable cv_;

    mutable std::mutex meter_mutex_;
    Meter meter_;

    // 统计快照（seqlock）。写侧只碰这两个成员，读侧见 SnapshotStats()。
    mutable std::atomic<uint32_t> stats_seq_{0};
    StatsSnapshot stats_live_{};
    std::vector<float> scratch_;
};

}  // namespace

// ---------------------------------------------------------------------------
// 后端对外接口
// ---------------------------------------------------------------------------

std::vector<DeviceInfo> WasapiBackend::EnumerateCaptureDevices() {
    return EnumerateByFlow(eCapture, "mic");
}

std::vector<DeviceInfo> WasapiBackend::EnumerateRenderDevices() {
    return EnumerateByFlow(eRender, "render");
}

std::unique_ptr<AudioSource> WasapiBackend::OpenSource(const SourceConfig& cfg,
                                                       std::string* reason) {
    auto src = std::make_unique<WasapiSource>();
    if (!src->Open(cfg, reason)) return nullptr;
    return src;
}

// ---------------------------------------------------------------------------
// 进程回环能力探测
//
// 为什么要真实探测而不是读系统版本号：
//   微软文档写的最低版本是 Windows 10 Build 20348，但社区普遍反馈
//   Win10 2004（19041）及以后实际就能用。按版本号判断会冤枉一大批能用的
//   机器，让用户以为自己的系统不支持。
//
// 为什么用 SEH 兜底：
//   这段代码要调系统的异步激活接口，而该接口在部分系统配置下会让进程
//   直接崩掉（0xC0000374 堆损坏）而不是返回错误码。用户只是想看看能不能
//   用「单个应用的声音」，为此崩溃是不可接受的 —— 有兜底就退化成"不支持"。
//
// 为什么 SEH 必须单独放一个函数（踩过两次的坑）：
//   MSVC 规定 __try 所在的函数里不能出现任何"需要对象展开"的操作
//   （错误 C2712）。而 std::string 的赋值、按值返回对象都算。
//   所以这里把 __try 关进一个**完全不做展开**的静态函数：
//   它只收一个出参指针、只返回 DWORD 异常码。
// ---------------------------------------------------------------------------
namespace {

// 返回值：0 表示正常返回；非 0 是捕获到的 SEH 异常码。
// 这个函数里刻意不出现任何带析构函数的局部对象、不按值返回对象。
DWORD CallProbeUnderSeh(ProcessLoopbackProbe* out) {
#ifdef _WIN32
    __try {
        WasapiBackend::ProbeProcessLoopbackImpl(out);
        return 0;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return static_cast<DWORD>(::GetExceptionCode());
    }
#else
    WasapiBackend::ProbeProcessLoopbackImpl(out);
    return 0;
#endif
}

}  // namespace

// ---------------------------------------------------------------------------
// 进程回环崩溃那件事 —— 【2026-10-06 结论已推翻旧版本，别再按旧结论动手】
//
// 旧版本这里写着"直接调用进程回环激活会让进程以 0xC0000374（堆损坏）终止 ⇒
// 必须子进程隔离、默认不做真实探测"。**那是错的归因**，已作废：
//
//   · 真凶：激活之后对指向**本函数栈变量**的 VT_BLOB 调了 PropVariantClear
//     → CoTaskMemFree(栈指针) → 堆损坏 0xC0000374。
//   · 证据：删掉那行调用后，同一台机器上真实探测返回 rc=0 / supported=1，
//     OpenProcessLoopback 也能正常激活采集（2026-10-06 实测）。
//   · 所以既不缺 FtmBase，也不需要子进程隔离，更不需要
//     ZX_ENABLE_PROCESS_LOOPBACK_PROBE 这种开关（该宏已不控制任何分支）。
//   · 唯一还成立的一点：堆损坏走 fast-fail，SEH **拦不住**，
//     所以 CallProbeUnderSeh 只是兜住普通异常，不是安全网。
//
// 今天的默认行为：**执行真实探测**（见下面 ProbeProcessLoopback）。
// ---------------------------------------------------------------------------
ProcessLoopbackProbe WasapiBackend::ProbeProcessLoopback() {
    ProcessLoopbackProbe out;
    // 【2026-10-06 已修，默认走真实探测】以前这里默认返回"不支持"，
    // 因为进程回环激活必崩（0xC0000374 堆损坏）。
    // 真凶不是系统 API（Windows 11 26200 上官方写法完全正常），
    // 而是我们在激活之后对指向**栈变量**的 VT_BLOB 调了 PropVariantClear
    // （→ CoTaskMemFree(栈指针) → 堆损坏）。删掉那行后探测返回 rc=0 / supported=1。
    // 所以现在默认执行真实探测；SEH 仍作为兜底（能拦普通异常，拦不住 fast-fail）。
    const DWORD seh = CallProbeUnderSeh(&out);
    if (seh != 0) {
        out.supported = false;
        char buf[224];
        std::snprintf(buf, sizeof(buf),
                      "探测「单个应用音频」时系统抛出异常（代码 0x%08lX）。"
                      "已按不支持处理，可改用「共享全部应用的声音」。",
                      static_cast<unsigned long>(seh));
        out.detail = buf;
    }
    return out;
}

void WasapiBackend::ProbeProcessLoopbackImpl(ProcessLoopbackProbe* out) {
    if (!out) return;
    out->supported = false;
    out->detail.clear();

    // 用当前进程试激活一次。这是真实探测，不是读版本号 ——
    // 微软文档写 20348，但 Win10 2004+ 实测可用，按版本判断会冤枉人。
    AUDIOCLIENT_ACTIVATION_PARAMS params = MakeProcessLoopbackParams(
        ::GetCurrentProcessId(), PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE);
    PROPVARIANT pv = MakeBlobPropVariant(&params);

    auto handler = Microsoft::WRL::Make<ActivationHandler>();
    ComPtr<IActivateAudioInterfaceAsyncOperation> op;
    HRESULT hr = ::ActivateAudioInterfaceAsync(
        kProcessLoopbackDeviceId, __uuidof(IAudioClient), &pv,
        handler.Get(), op.GetAddressOf());
    // 【真凶已定位 2026-10-06】同 OpenProcessLoopback：不能对指向栈变量的
    // VT_BLOB 调 PropVariantClear，否则 CoTaskMemFree(栈指针) → 0xC0000374。
    (void)params;

    if (FAILED(hr)) {
        char buf[160];
        std::snprintf(buf, sizeof(buf),
                      "本机不支持单个应用音频（激活调用失败 0x%08lX）",
                      static_cast<unsigned long>(hr));
        out->detail = buf;
        return;
    }

    ComPtr<IUnknown> activated;
    HRESULT activation_hr = E_FAIL;
    if (!handler->WaitFor(std::chrono::milliseconds(3000), &activated, &activation_hr)) {
        out->detail = "探测超时：系统未在 3 秒内响应进程回环激活";
        return;
    }
    if (FAILED(activation_hr) || !activated) {
        char buf[256];
        std::snprintf(buf, sizeof(buf),
                      "本机不支持单个应用音频（激活被拒 0x%08lX）。"
                      "需要 Windows 10 Build 20348 及以上；"
                      "低版本请改用「共享全部应用的声音」。",
                      static_cast<unsigned long>(activation_hr));
        out->detail = buf;
        return;
    }

    out->supported = true;
    out->detail = "支持单个应用音频（进程回环）";
}

// ---------------------------------------------------------------------------
// 音频会话枚举：哪些进程正在发声
//
// 走 IAudioSessionManager2。这是 Windows 上拿到「应用 → 声音」映射的正路，
// 也是「只共享某个应用」这个功能在 UI 上能列出候选的前提。
// ---------------------------------------------------------------------------
std::vector<SessionProcessInfo> EnumerateAudioSessions() {
    std::vector<SessionProcessInfo> out;

    ComPtr<IMMDeviceEnumerator> enumerator;
    if (FAILED(::CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
                                  IID_PPV_ARGS(enumerator.GetAddressOf()))) ||
        !enumerator) {
        return out;
    }

    ComPtr<IMMDeviceCollection> devices;
    if (FAILED(enumerator->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE,
                                              devices.GetAddressOf())) ||
        !devices) {
        return out;
    }

    UINT device_count = 0;
    if (FAILED(devices->GetCount(&device_count))) return out;

    // 同一个进程可能在多个渲染设备上都有会话，用 pid 去重
    std::vector<uint32_t> seen_pids;

    for (UINT di = 0; di < device_count; ++di) {
        ComPtr<IMMDevice> device;
        if (FAILED(devices->Item(di, device.GetAddressOf())) || !device) continue;

        ComPtr<IAudioSessionManager2> mgr;
        if (FAILED(device->Activate(__uuidof(IAudioSessionManager2), CLSCTX_ALL,
                                    nullptr,
                                    reinterpret_cast<void**>(mgr.GetAddressOf()))) ||
            !mgr) {
            continue;
        }

        ComPtr<IAudioSessionEnumerator> sessions;
        if (FAILED(mgr->GetSessionEnumerator(sessions.GetAddressOf())) || !sessions) {
            continue;
        }

        int count = 0;
        if (FAILED(sessions->GetCount(&count))) continue;

        for (int i = 0; i < count; ++i) {
            ComPtr<IAudioSessionControl> control;
            if (FAILED(sessions->GetSession(i, control.GetAddressOf())) || !control) {
                continue;
            }

            AudioSessionState state = AudioSessionStateInactive;
            control->GetState(&state);

            uint32_t pid = 0;
            ComPtr<IAudioSessionControl2> control2;
            if (SUCCEEDED(control.As(&control2)) && control2) {
                DWORD p = 0;
                if (SUCCEEDED(control2->GetProcessId(&p))) pid = p;
            }

            // 系统声音（pid 0）与自己的会话不列出：前者不是「应用」，
            // 后者列出来会让用户困惑（共享自己会形成回环）
            if (pid == 0 || pid == ::GetCurrentProcessId()) continue;
            if (std::find(seen_pids.begin(), seen_pids.end(), pid) != seen_pids.end()) {
                continue;
            }

            SessionProcessInfo info;
            info.pid = pid;
            info.active = (state == AudioSessionStateActive);

            // 进程名：OpenProcess + QueryFullProcessImageNameW
            if (HANDLE h = ::OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid)) {
                wchar_t buf[MAX_PATH] = {0};
                DWORD size = MAX_PATH;
                if (::QueryFullProcessImageNameW(h, 0, buf, &size)) {
                    const std::wstring full(buf);
                    const size_t slash = full.find_last_of(L"\\/");
                    info.name = WideToUtf8(
                        slash == std::wstring::npos ? full.c_str() : full.c_str() + slash + 1);
                }
                ::CloseHandle(h);
            }
            if (info.name.empty()) info.name = "未知进程";

            // 会话显示名（有些应用会设置为歌曲名之类）
            LPWSTR display = nullptr;
            if (SUCCEEDED(control->GetDisplayName(&display)) && display) {
                if (*display) info.title = WideToUtf8(display);
                ::CoTaskMemFree(display);
            }

            seen_pids.push_back(pid);
            out.push_back(std::move(info));
        }
    }

    // 正在发声的排前面，方便用户一眼找到目标
    std::stable_sort(out.begin(), out.end(),
                     [](const SessionProcessInfo& a, const SessionProcessInfo& b) {
                         return a.active && !b.active;
                     });
    return out;
}

}  // namespace zx

#endif  // _WIN32
