// h264-decoder.h —— H.264 解码器 MFT 封装（接收端核心）
//
// ============================================================================
// 【为什么不从文件解，而是直接喂访问单元】
// Media Foundation 不自带「裸 Annex-B H.264 文件」的字节流处理器，
// 用 IMFSourceReader 读 .h264 会返回 0xC00D36C4 (MF_E_UNSUPPORTED_BYTESTREAM_TYPE)。
// 两条路都试过都失败：创建属性里设 MF_MT_SUBTYPE、给字节流设 MF_BYTESTREAM_CONTENT_TYPE。
//
// 而接收端本来就不存在"文件"这个概念 —— 从 UDP 收到的是一个个访问单元(AU)。
// 所以直接用解码器 MFT 喂数据，反而更贴合真实链路。
//
// 【关键点】
//   · MF_MT_MPEG_SEQUENCE_HEADER 必须带 SPS/PPS，否则解码器不知道分辨率、
//     profile、参考帧数，SetInputType 直接失败
//   · 解码器输出的高度会被对齐到 16 的倍数（1080 → 1088），必须按实际值读
//   · 丢帧之后要 Flush（MFT_MESSAGE_COMMAND_FLUSH），再从 IDR 继续喂
// ============================================================================

#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <objbase.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mftransform.h>
#include <mferror.h>
// ICodecAPI 的声明在 strmif.h（DirectShow 头）里，不在 codecapi.h
#include <codecapi.h>
#include <strmif.h>
#include <wrl/client.h>

#include <algorithm>
#include <functional>
#include <string>
#include <vector>

namespace zx {

// 【必须放在命名空间作用域】把 using 声明写在类内部会让 MSVC 把
// ComPtr<...> 解析成「缺参数列表的函数模板」(C7568)，之后所有用到它的
// 成员函数都跟着报错。
using Microsoft::WRL::ComPtr;

class H264Decoder {
public:
    struct DecodedFrame {
        const uint8_t* data;    // NV12 平面数据
        UINT stride;            // Y 平面行距；UV 平面紧随 Y 之后
        UINT width;
        UINT height;            // 注意：可能比显示高度大（16 对齐补边）
        uint64_t timestampUs;
    };
    using FrameCallback = std::function<void(const DecodedFrame&)>;

    H264Decoder() = default;
    ~H264Decoder() { Close(); }
    H264Decoder(const H264Decoder&) = delete;
    H264Decoder& operator=(const H264Decoder&) = delete;

    const std::wstring& name() const { return name_; }
    const std::wstring& configNote() const { return cfgNote_; }
    UINT width() const { return w_; }
    UINT height() const { return h_; }
    UINT stride() const { return stride_; }
    int decodedFrames() const { return framesOut_; }
    void SetCallback(FrameCallback cb) { cb_ = std::move(cb); }

    // 打开并协商。seqHeader 是带起始码的 SPS+PPS
    bool Open(const uint8_t* seqHeader, size_t seqLen, UINT w, UINT h, UINT fps,
              std::wstring& err)
    {
        // ---- 枚举 H.264 → NV12 解码器 ----
        MFT_REGISTER_TYPE_INFO inInfo{ MFMediaType_Video, MFVideoFormat_H264 };
        MFT_REGISTER_TYPE_INFO outInfo{ MFMediaType_Video, MFVideoFormat_NV12 };
        IMFActivate** acts = nullptr;
        UINT32 actCount = 0;
        HRESULT hr = MFTEnumEx(MFT_CATEGORY_VIDEO_DECODER, MFT_ENUM_FLAG_ALL,
                               &inInfo, &outInfo, &acts, &actCount);
        if (FAILED(hr) || actCount == 0) {
            err = L"找不到 H.264 解码器";
            if (acts) { for (UINT32 i = 0; i < actCount; ++i) acts[i]->Release(); CoTaskMemFree(acts); }
            return false;
        }

        // 优先硬件解码器（NVIDIA/Intel），失败再退回软件解码器
        auto rank = [](const std::wstring& n) -> int {
            if (n.find(L"NVIDIA") != std::wstring::npos) return 0;
            if (n.find(L"Intel") != std::wstring::npos) return 1;
            return 2;
        };
        std::vector<UINT32> order(actCount);
        for (UINT32 i = 0; i < actCount; ++i) order[i] = i;
        std::stable_sort(order.begin(), order.end(), [&](UINT32 a, UINT32 b) {
            auto nm = [&](UINT32 idx) {
                LPWSTR s = nullptr; UINT32 len = 0; std::wstring r = L"";
                if (SUCCEEDED(acts[idx]->GetAllocatedString(MFT_FRIENDLY_NAME_Attribute, &s, &len))) {
                    r = s; CoTaskMemFree(s);
                }
                return r;
            };
            return rank(nm(a)) < rank(nm(b));
        });

        bool opened = false;
        for (UINT32 k = 0; k < actCount && !opened; ++k) {
            IMFActivate* a = acts[order[k]];
            ComPtr<IMFTransform> t;
            if (FAILED(a->ActivateObject(IID_PPV_ARGS(&t))) || !t) continue;

            std::wstring nm;
            {
                LPWSTR s = nullptr; UINT32 len = 0;
                if (SUCCEEDED(a->GetAllocatedString(MFT_FRIENDLY_NAME_Attribute, &s, &len))) {
                    nm = s; CoTaskMemFree(s);
                }
            }
            if (TryNegotiate(t.Get(), seqHeader, seqLen, w, h, fps, nm)) {
                dec_ = t;
                name_ = nm;
                opened = true;
            }
        }

        for (UINT32 i = 0; i < actCount; ++i) acts[i]->Release();
        CoTaskMemFree(acts);

        if (!opened) { err = L"所有解码器都无法协商类型"; return false; }

        hasEvents_ = SUCCEEDED(dec_.As(&events_));
        {
            MFT_OUTPUT_STREAM_INFO si{};
            dec_->GetOutputStreamInfo(0, &si);
            providesSamples_ = (si.dwFlags & MFT_OUTPUT_STREAM_PROVIDES_SAMPLES) != 0;
            outBufSize_ = si.cbSize ? si.cbSize : (4u << 20);
            if (outBufSize_ < (1u << 20)) outBufSize_ = (1u << 20);
        }

        dec_->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);
        dec_->ProcessMessage(MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0);
        ok_ = true;
        return true;
    }

    // 喂一个访问单元
    bool Feed(const uint8_t* au, size_t len, uint64_t timestampUs)
    {
        if (!ok_ || !dec_) return false;

        ComPtr<IMFMediaBuffer> mb;
        if (FAILED(MFCreateMemoryBuffer((DWORD)len, &mb))) return false;
        BYTE* p = nullptr;
        if (FAILED(mb->Lock(&p, nullptr, nullptr))) return false;
        memcpy(p, au, len);
        mb->Unlock();
        mb->SetCurrentLength((DWORD)len);

        ComPtr<IMFSample> sample;
        if (FAILED(MFCreateSample(&sample))) return false;
        sample->AddBuffer(mb.Get());
        sample->SetSampleTime((LONGLONG)(timestampUs * 10));   // 微秒 → 100ns
        sample->SetSampleDuration((LONGLONG)(10000000 / 30));

        // MF_E_NOTACCEPTING(0xC00D36B5) 是「暂时吃不下」，必须按墙钟限时重试。
        // 用 Sleep(1) 会撞上 Windows 默认 15.6ms 的时钟精度，每帧白等十几毫秒。
        const double t0 = NowMs();
        HRESULT ihr = dec_->ProcessInput(0, sample.Get(), 0);
        while (ihr == MF_E_NOTACCEPTING) {
            if (NowMs() - t0 > 500.0) break;
            PumpOutputs();
            SwitchToThread();
            ihr = dec_->ProcessInput(0, sample.Get(), 0);
        }
        if (FAILED(ihr)) return false;

        PumpOutputs();
        return true;
    }

    // 丢帧后调用：清空解码器内部状态，之后必须从 IDR 重新开始喂
    void Flush()
    {
        if (!ok_ || !dec_) return;
        dec_->ProcessMessage(MFT_MESSAGE_COMMAND_FLUSH, 0);
        dec_->ProcessMessage(MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0);
    }

    // 收尾排空：把解码器里缓存的帧全部取出来。
    // 【为什么需要】解码器内部会压着若干帧（DPB/重排序）。不排空的话
    // 最后几帧永远拿不到 —— 实测喂入 180 帧只解出 153 帧，差的就是这些。
    void Drain()
    {
        if (!ok_ || !dec_) return;
        dec_->ProcessMessage(MFT_MESSAGE_NOTIFY_END_OF_STREAM, 0);
        dec_->ProcessMessage(MFT_MESSAGE_COMMAND_DRAIN, 0);
        for (int i = 0; i < 2000; ++i) {
            if (PumpOutputs() == 0) break;
        }
    }

    void Close()
    {
        if (dec_) {
            // 【顺序很重要】MFT 必须在 MFShutdown 之前释放，否则进程退出时 0xC0000005
            dec_->ProcessMessage(MFT_MESSAGE_NOTIFY_END_STREAMING, 0);
            dec_.Reset();
        }
        events_.Reset();
        ok_ = false;
    }

private:
    static double NowMs()
    {
        static LARGE_INTEGER freq = [] { LARGE_INTEGER f; QueryPerformanceFrequency(&f); return f; }();
        LARGE_INTEGER c; QueryPerformanceCounter(&c);
        return double(c.QuadPart) * 1000.0 / double(freq.QuadPart);
    }

    bool TryNegotiate(IMFTransform* t, const uint8_t* seqHeader, size_t seqLen,
                      UINT w, UINT h, UINT fps, std::wstring& outName)
    {
        // 异步解码器必须先解锁，否则 SetInputType 返回 MF_E_TRANSFORM_ASYNC_LOCKED
        {
            ComPtr<IMFAttributes> attrs;
            if (SUCCEEDED(t->GetAttributes(&attrs))) {
                UINT32 isAsync = 0;
                attrs->GetUINT32(MF_TRANSFORM_ASYNC, &isAsync);
                if (isAsync) attrs->SetUINT32(MF_TRANSFORM_ASYNC_UNLOCK, TRUE);
            }
        }

        // 【低延迟配置 —— 实时共享的关键】
        // 微软的 H.264 解码器默认按 CPU 核数开多线程并行解码，为了吞吐会
        // 一口气缓冲几十帧，实测造成 **平均 1.1 秒** 的解码延迟，对实时共享
        // 完全不可接受。用 ICodecAPI 把工作线程数压下来即可。
        // 这些值必须在协商媒体类型之前设置。
        cfgNote_.clear();
        {
            ComPtr<ICodecAPI> capi;
            if (SUCCEEDED(t->QueryInterface(IID_PPV_ARGS(&capi)))) {
                auto note = [&](const wchar_t* tag, HRESULT r) {
                    wchar_t b[96];
                    swprintf_s(b, L"%s=%08X ", tag, (unsigned)r);
                    cfgNote_ += b;
                };
                {
                    VARIANT var; VariantInit(&var);
                    var.vt = VT_UI4; var.ulVal = 1;
                    note(L"线程1", capi->SetValue(&CODECAPI_AVDecNumWorkerThreads, &var));
                    VariantClear(&var);
                }
                {
                    VARIANT var; VariantInit(&var);
                    var.vt = VT_BOOL; var.boolVal = VARIANT_TRUE;
                    note(L"低延迟", capi->SetValue(&CODECAPI_AVLowLatencyMode, &var));
                    VariantClear(&var);
                }
            } else {
                cfgNote_ = L"该解码器不支持 ICodecAPI";
            }
        }

        ComPtr<IMFMediaType> inType;
        MFCreateMediaType(&inType);
        inType->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
        inType->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_H264);
        inType->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
        MFSetAttributeSize(inType.Get(), MF_MT_FRAME_SIZE, w, h);
        MFSetAttributeRatio(inType.Get(), MF_MT_FRAME_RATE, fps, 1);
        MFSetAttributeRatio(inType.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
        if (seqHeader && seqLen)
            inType->SetBlob(MF_MT_MPEG_SEQUENCE_HEADER, seqHeader, (UINT32)seqLen);

        HRESULT hr = t->SetInputType(0, inType.Get(), 0);
        if (FAILED(hr)) {
            ComPtr<IMFMediaType> avail;
            if (SUCCEEDED(t->GetInputAvailableType(0, 0, &avail))) {
                if (seqHeader && seqLen)
                    avail->SetBlob(MF_MT_MPEG_SEQUENCE_HEADER, seqHeader, (UINT32)seqLen);
                hr = t->SetInputType(0, avail.Get(), 0);
            }
        }
        if (FAILED(hr)) { outName += L"（输入类型协商失败）"; return false; }

        ComPtr<IMFMediaType> outType;
        MFCreateMediaType(&outType);
        outType->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
        outType->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
        outType->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
        MFSetAttributeSize(outType.Get(), MF_MT_FRAME_SIZE, w, h);
        MFSetAttributeRatio(outType.Get(), MF_MT_FRAME_RATE, fps, 1);
        MFSetAttributeRatio(outType.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);

        hr = t->SetOutputType(0, outType.Get(), 0);
        if (FAILED(hr)) {
            ComPtr<IMFMediaType> avail;
            if (SUCCEEDED(t->GetOutputAvailableType(0, 0, &avail)))
                hr = t->SetOutputType(0, avail.Get(), 0);
        }
        if (FAILED(hr)) { outName += L"（输出类型协商失败）"; return false; }

        // 读回实际生效的分辨率（解码器会做 16 对齐，1080 → 1088）
        ComPtr<IMFMediaType> cur;
        if (SUCCEEDED(t->GetOutputCurrentType(0, &cur))) {
            MFGetAttributeSize(cur.Get(), MF_MT_FRAME_SIZE, &w_, &h_);
            if (FAILED(cur->GetUINT32(MF_MT_DEFAULT_STRIDE, &stride_)) || stride_ == 0)
                stride_ = w_;
        }
        if (w_ == 0 || h_ == 0) { w_ = w; h_ = h; stride_ = w; }
        return true;
    }

    bool CollectOne()
    {
        MFT_OUTPUT_DATA_BUFFER db{};
        db.dwStreamID = 0;
        IMFSample* ours = nullptr;

        if (!providesSamples_) {
            IMFMediaBuffer* ob = nullptr;
            if (FAILED(MFCreateMemoryBuffer(outBufSize_, &ob))) return false;
            if (FAILED(MFCreateSample(&ours))) { ob->Release(); return false; }
            ours->AddBuffer(ob);
            ob->Release();
            db.pSample = ours;
        }

        DWORD status = 0;
        HRESULT hr = dec_->ProcessOutput(0, 1, &db, &status);
        IMFSample* rel = db.pSample;
        if (db.pEvents) { db.pEvents->Release(); db.pEvents = nullptr; }

        if (hr == MF_E_TRANSFORM_STREAM_CHANGE) {
            if (rel) rel->Release();
            ComPtr<IMFMediaType> avail;
            if (SUCCEEDED(dec_->GetOutputAvailableType(0, 0, &avail))) {
                dec_->SetOutputType(0, avail.Get(), 0);
                MFGetAttributeSize(avail.Get(), MF_MT_FRAME_SIZE, &w_, &h_);
                if (FAILED(avail->GetUINT32(MF_MT_DEFAULT_STRIDE, &stride_)) || stride_ == 0)
                    stride_ = w_;
            }
            return true;   // 还有活干
        }
        if (hr == MF_E_TRANSFORM_NEED_MORE_INPUT) { if (rel) rel->Release(); return false; }
        if (FAILED(hr)) { if (rel) rel->Release(); return false; }

        if (db.pSample) {
            ComPtr<IMFMediaBuffer> buf;
            if (SUCCEEDED(db.pSample->ConvertToContiguousBuffer(&buf))) {
                BYTE* p = nullptr; DWORD len = 0;
                if (SUCCEEDED(buf->Lock(&p, nullptr, &len))) {
                    const size_t need = size_t(stride_) * h_ * 3 / 2;
                    if (len >= need && cb_) {
                        DecodedFrame f{};
                        f.data = p;
                        f.stride = stride_;
                        f.width = w_;
                        f.height = h_;
                        LONGLONG ts = 0;
                        db.pSample->GetSampleTime(&ts);
                        f.timestampUs = (uint64_t)(ts / 10);
                        ++framesOut_;
                        cb_(f);
                    }
                    buf->Unlock();
                }
            }
        }
        if (rel) rel->Release();
        return true;
    }

    int PumpOutputs()
    {
        int got = 0;
        if (hasEvents_) {
            for (;;) {
                ComPtr<IMFMediaEvent> ev;
                if (FAILED(events_->GetEvent(MF_EVENT_FLAG_NO_WAIT, &ev))) break;
                MediaEventType t = MEUnknown;
                ev->GetType(&t);
                if (t == METransformHaveOutput) { if (CollectOne()) ++got; }
                else break;
            }
        } else {
            while (CollectOne()) ++got;
        }
        return got;
    }

    ComPtr<IMFTransform> dec_;
    ComPtr<IMFMediaEventGenerator> events_;
    std::wstring name_;
    std::wstring cfgNote_;
    FrameCallback cb_;
    UINT w_ = 0, h_ = 0, stride_ = 0;
    bool providesSamples_ = false;
    bool hasEvents_ = false;
    DWORD outBufSize_ = 0;
    int framesOut_ = 0;
    bool ok_ = false;
};

} // namespace zx
