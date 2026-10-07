// sender-probe.cpp —— 发送端完整链路：屏幕采集 → GPU 转 NV12 → 硬件 H.264 编码
//
// ============================================================================
// 前面三步已经分别验证过，这里把它们接起来：
//   1. display-probe  ：DXGI Desktop Duplication 能拿到真实桌面画面
//   2. encode-probe   ：NVIDIA H.264 编码器能出 Annex-B 码流（异步 MFT 协议）
//   3. decode-probe   ：码流能被 H.264 解码器解回正确画面
//
// 本程序补上中间缺的一环：BGRA → NV12 的转换。
//
// 【为什么转换必须放在 GPU 上】
// 走 CPU 的话，1080p 每帧要回读 8.3 MB 的 BGRA，再逐像素转换：
// 30fps 就是 249 MB/s 加每帧几毫秒的 CPU 时间。用 ID3D11VideoProcessor
// 在 GPU 内部转换后只回读 NV12（3.1 MB，少 2.7 倍），CPU 完全不做转换。
// 而且 VideoProcessor 自带缩放 —— 720p / 1080p 分档直接靠它，不需要额外代码。
//
// 【本程序刻意还不做的事】
// 不发网络。先把"能稳定产出指定分辨率/帧率的码流"这件事做扎实，
// UDP 传输、接收渲染、远程控制都在之后接。一次只引入一个新变量。
//
// 用法：sender-probe.exe [秒数] [输出.h264] [宽] [高] [帧率] [码率bps]
//   例：sender-probe.exe 5 out.h264 1280 720 30 4000000     （远程控制档）
//   例：sender-probe.exe 5 out.h264 1920 1080 60 20000000   （看视频档）
// ============================================================================

#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mftransform.h>
#include <mferror.h>
#include <codecapi.h>
// 【ICodecAPI 的声明在 strmif.h（DirectShow 头）里，不在 codecapi.h】
// codecapi.h 只有 CODECAPI_* 这些 GUID 常量。强制关键帧要用 ICodecAPI::SetValue。
#include <strmif.h>
#include <wrl/client.h>

#include "../transport/media-packet.h"
#include "../transport/udp-socket.h"
#include "control-auth.h"

#include <cstdio>
#include <string>
#include <vector>

using namespace Microsoft::WRL;

static FILE* g_log = nullptr;

static void Log(const wchar_t* fmt, ...)
{
    {
        va_list ap; va_start(ap, fmt);
        vwprintf(fmt, ap);
        va_end(ap);
        fflush(stdout);
    }
    if (g_log) {
        va_list ap; va_start(ap, fmt);
        vfwprintf(g_log, fmt, ap);
        va_end(ap);
        fflush(g_log);
    }
}

static std::wstring ExeRelative(const wchar_t* name)
{
    if (!name || !name[0]) return std::wstring();
    if (name[0] == L'\\' || (name[0] && name[1] == L':')) return name;
    wchar_t path[MAX_PATH]{};
    DWORD n = GetModuleFileNameW(nullptr, path, MAX_PATH);
    std::wstring s(path, n);
    const size_t pos = s.find_last_of(L"\\/");
    if (pos != std::wstring::npos) s.resize(pos + 1); else s.clear();
    return s + name;
}

static double NowSeconds()
{
    static LARGE_INTEGER freq = [] { LARGE_INTEGER f; QueryPerformanceFrequency(&f); return f; }();
    LARGE_INTEGER c; QueryPerformanceCounter(&c);
    return double(c.QuadPart) / double(freq.QuadPart);
}

// ===========================================================================
// 采集部分
// ===========================================================================
struct Capture {
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    ComPtr<IDXGIOutputDuplication> dup;
    ComPtr<ID3D11Texture2D> accumulated;   // 累积的桌面画面（BGRA）
    UINT width = 0, height = 0;
    DXGI_FORMAT format = DXGI_FORMAT_B8G8R8A8_UNORM;

    bool Init()
    {
        // 【必须 BGRA_SUPPORT】DXGI Desktop Duplication 要求设备支持 BGRA
        UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
        D3D_FEATURE_LEVEL level = D3D_FEATURE_LEVEL_11_0;
        HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, flags,
                                       nullptr, 0, D3D11_SDK_VERSION,
                                       &device, &level, &context);
        if (FAILED(hr)) {
            Log(L"[X] D3D11CreateDevice 失败 hr=0x%08X\n", hr);
            return false;
        }

        ComPtr<IDXGIDevice> dxgiDev;
        if (FAILED(device.As(&dxgiDev))) { Log(L"[X] 拿不到 IDXGIDevice\n"); return false; }
        ComPtr<IDXGIAdapter> adapter;
        if (FAILED(dxgiDev->GetAdapter(&adapter))) { Log(L"[X] 拿不到适配器\n"); return false; }

        // 只用第一个带输出的适配器（实测就是挂着显示器的 NVIDIA 适配器）
        ComPtr<IDXGIOutput> output;
        if (FAILED(adapter->EnumOutputs(0, &output))) {
            Log(L"[X] 适配器没有输出\n");
            return false;
        }
        ComPtr<IDXGIOutput1> output1;
        if (FAILED(output.As(&output1))) { Log(L"[X] 拿不到 IDXGIOutput1\n"); return false; }

        hr = output1->DuplicateOutput(device.Get(), &dup);
        if (FAILED(hr)) {
            Log(L"[X] DuplicateOutput 失败 hr=0x%08X\n", hr);
            return false;
        }

        DXGI_OUTDUPL_DESC dd{};
        dup->GetDesc(&dd);
        width = dd.ModeDesc.Width;
        height = dd.ModeDesc.Height;
        format = dd.ModeDesc.Format;
        Log(L"[OK] 采集已建立 %ux%u 格式=%u\n", width, height, (UINT)format);

        D3D11_TEXTURE2D_DESC td{};
        td.Width = width;
        td.Height = height;
        td.Format = format;
        td.ArraySize = 1;
        td.MipLevels = 1;
        td.SampleDesc.Count = 1;
        td.Usage = D3D11_USAGE_DEFAULT;
        // 后面要给 VideoProcessor 当输入，必须有 RENDER_TARGET
        td.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
        if (FAILED(device->CreateTexture2D(&td, nullptr, &accumulated))) {
            Log(L"[X] 创建累积纹理失败\n");
            return false;
        }
        return true;
    }

    // 取一帧并累积。返回 true 表示拿到了带画面的新帧。
    // 【要点】AcquireNextFrame 成功不等于有画面：只有指针移动时
    // LastPresentTime == 0 且 AccumulatedFrames == 0，此时纹理里没有桌面图像。
    bool Pump(int timeoutMs, bool& gotFrame)
    {
        gotFrame = false;
        DXGI_OUTDUPL_FRAME_INFO fi{};
        ComPtr<IDXGIResource> res;
        HRESULT hr = dup->AcquireNextFrame(timeoutMs, &fi, &res);
        if (hr == DXGI_ERROR_WAIT_TIMEOUT) return true;
        if (FAILED(hr)) {
            Log(L"[!] AcquireNextFrame hr=0x%08X\n", hr);
            return hr != DXGI_ERROR_ACCESS_LOST;   // ACCESS_LOST 需要重建，交上层处理
        }
        const bool hasNew = (fi.LastPresentTime.QuadPart != 0) || (fi.AccumulatedFrames > 0);
        if (hasNew) {
            ComPtr<ID3D11Texture2D> tex;
            if (SUCCEEDED(res.As(&tex))) {
                context->CopyResource(accumulated.Get(), tex.Get());
                gotFrame = true;
            }
        }
        dup->ReleaseFrame();
        return true;
    }
};

// ===========================================================================
// BGRA → NV12（GPU 内完成，顺便缩放）
// ===========================================================================
struct Nv12Converter {
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> ctx;      // CopyResource 在 ID3D11DeviceContext 上，不在 VideoContext 上
    ComPtr<ID3D11VideoDevice> videoDevice;
    ComPtr<ID3D11VideoContext> videoContext;
    ComPtr<ID3D11VideoProcessorEnumerator> enumerator;
    ComPtr<ID3D11VideoProcessor> processor;
    ComPtr<ID3D11VideoProcessorInputView> inputView;
    ComPtr<ID3D11VideoProcessorOutputView> outputView;
    ComPtr<ID3D11Texture2D> nv12Tex;        // GPU 侧输出
    ComPtr<ID3D11Texture2D> nv12Staging;    // CPU 可读的暂存
    UINT dstW = 0, dstH = 0;
    UINT nv12Pitch = 0;

    bool Init(ID3D11Device* device, ID3D11DeviceContext* context,
              ID3D11Texture2D* bgraInput,
              UINT srcW, UINT srcH, UINT outW, UINT outH, UINT fps)
    {
        dstW = outW; dstH = outH;
        device->QueryInterface(IID_PPV_ARGS(&this->device));
        ctx = context;

        if (FAILED(device->QueryInterface(IID_PPV_ARGS(&videoDevice)))) {
            Log(L"[X] 设备不支持 ID3D11VideoDevice\n");
            return false;
        }
        if (FAILED(context->QueryInterface(IID_PPV_ARGS(&videoContext)))) {
            Log(L"[X] 上下文不支持 ID3D11VideoContext\n");
            return false;
        }

        D3D11_VIDEO_PROCESSOR_CONTENT_DESC cd{};
        cd.InputFrameFormat = D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE;
        cd.InputWidth = srcW;
        cd.InputHeight = srcH;
        cd.OutputWidth = outW;
        cd.OutputHeight = outH;
        cd.InputFrameRate.Numerator = fps;
        cd.InputFrameRate.Denominator = 1;
        cd.OutputFrameRate.Numerator = fps;
        cd.OutputFrameRate.Denominator = 1;

        // 实时共享要低延迟，优先 OPTIMAL_SPEED；不支持就退回 PLAYBACK_NORMAL
        cd.Usage = D3D11_VIDEO_USAGE_OPTIMAL_SPEED;
        HRESULT hr = videoDevice->CreateVideoProcessorEnumerator(&cd, &enumerator);
        if (FAILED(hr)) {
            Log(L"[信息] OPTIMAL_SPEED 不支持 hr=0x%08X，改用 PLAYBACK_NORMAL\n", hr);
            cd.Usage = D3D11_VIDEO_USAGE_PLAYBACK_NORMAL;
            hr = videoDevice->CreateVideoProcessorEnumerator(&cd, &enumerator);
        }
        if (FAILED(hr)) {
            Log(L"[X] CreateVideoProcessorEnumerator 失败 hr=0x%08X\n", hr);
            return false;
        }

        UINT flags = 0;
        enumerator->CheckVideoProcessorFormat(DXGI_FORMAT_NV12, &flags);
        Log(L"      VideoProcessor 支持 NV12 输出: %s (flags=0x%X)\n",
            (flags & D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_OUTPUT) ? L"是" : L"否", flags);
        if (!(flags & D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_OUTPUT)) {
            Log(L"[X] 该 VideoProcessor 不能输出 NV12\n");
            return false;
        }

        hr = videoDevice->CreateVideoProcessor(enumerator.Get(), 0, &processor);
        if (FAILED(hr)) { Log(L"[X] CreateVideoProcessor 失败 hr=0x%08X\n", hr); return false; }

        // 输入视图（BGRA 纹理）
        D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC ivd{};
        ivd.FourCC = 0;
        ivd.ViewDimension = D3D11_VPIV_DIMENSION_TEXTURE2D;
        ivd.Texture2D.MipSlice = 0;
        ivd.Texture2D.ArraySlice = 0;
        hr = videoDevice->CreateVideoProcessorInputView(bgraInput, enumerator.Get(), &ivd, &inputView);
        if (FAILED(hr)) { Log(L"[X] 创建输入视图失败 hr=0x%08X\n", hr); return false; }

        // NV12 输出纹理：VideoProcessor 输出视图要求 RENDER_TARGET
        D3D11_TEXTURE2D_DESC td{};
        td.Width = outW;
        td.Height = outH;
        td.Format = DXGI_FORMAT_NV12;
        td.ArraySize = 1;
        td.MipLevels = 1;
        td.SampleDesc.Count = 1;
        td.Usage = D3D11_USAGE_DEFAULT;
        td.BindFlags = D3D11_BIND_RENDER_TARGET;
        hr = device->CreateTexture2D(&td, nullptr, &nv12Tex);
        if (FAILED(hr)) { Log(L"[X] 创建 NV12 纹理失败 hr=0x%08X\n", hr); return false; }

        D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC ovd{};
        ovd.ViewDimension = D3D11_VPOV_DIMENSION_TEXTURE2D;
        ovd.Texture2D.MipSlice = 0;
        hr = videoDevice->CreateVideoProcessorOutputView(nv12Tex.Get(), enumerator.Get(), &ovd, &outputView);
        if (FAILED(hr)) { Log(L"[X] 创建输出视图失败 hr=0x%08X\n", hr); return false; }

        // CPU 可读的 NV12 暂存纹理
        td.Usage = D3D11_USAGE_STAGING;
        td.BindFlags = 0;
        td.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        hr = device->CreateTexture2D(&td, nullptr, &nv12Staging);
        if (FAILED(hr)) { Log(L"[X] 创建 NV12 暂存纹理失败 hr=0x%08X\n", hr); return false; }

        // 矩形设置：源窗口、目标窗口都铺满
        RECT srcRect{ 0, 0, (LONG)srcW, (LONG)srcH };
        RECT dstRect{ 0, 0, (LONG)outW, (LONG)outH };
        videoContext->VideoProcessorSetStreamFrameFormat(processor.Get(), 0,
                                                        D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE);
        videoContext->VideoProcessorSetStreamSourceRect(processor.Get(), 0, TRUE, &srcRect);
        videoContext->VideoProcessorSetStreamDestRect(processor.Get(), 0, TRUE, &dstRect);
        videoContext->VideoProcessorSetOutputTargetRect(processor.Get(), TRUE, &dstRect);

        Log(L"[OK] VideoProcessor 已就绪 %ux%u → NV12 %ux%u\n", srcW, srcH, outW, outH);
        return true;
    }

    // 转换一帧，结果留在 nv12Staging 里（未 Map，调用方自己 Map）
    bool Convert()
    {
        D3D11_VIDEO_PROCESSOR_STREAM stream{};
        stream.Enable = TRUE;
        stream.pInputSurface = inputView.Get();
        HRESULT hr = videoContext->VideoProcessorBlt(processor.Get(), outputView.Get(), 0, 1, &stream);
        if (FAILED(hr)) {
            Log(L"[!] VideoProcessorBlt 失败 hr=0x%08X\n", hr);
            return false;
        }
        // CopyResource 属于 ID3D11DeviceContext
        ctx->CopyResource(nv12Staging.Get(), nv12Tex.Get());
        return true;
    }
};

// ===========================================================================
// 编码器（与 encode-probe 相同的异步 MFT 事件协议）
// ===========================================================================
struct Encoder {
    ComPtr<IMFTransform> xf;
    ComPtr<ICodecAPI> codec;      // 用于按接收端请求强制出关键帧
    std::wstring name;
    bool async = false;
    bool providesSamples = false;
    DWORD outBufSize = 0;

    // 强制编码器在下一帧产出 IDR —— 接收端丢帧后的唯一恢复手段
    bool ForceKeyframe()
    {
        if (!codec) return false;
        VARIANT v;
        VariantInit(&v);
        v.vt = VT_UI4;
        v.ulVal = 1;
        const HRESULT hr = codec->SetValue(&CODECAPI_AVEncVideoForceKeyFrame, &v);
        VariantClear(&v);
        return SUCCEEDED(hr);
    }

    bool Init(UINT w, UINT h, UINT fps, UINT32 bitrate, std::wstring& err)
    {
        MFT_REGISTER_TYPE_INFO inInfo{ MFMediaType_Video, MFVideoFormat_NV12 };
        MFT_REGISTER_TYPE_INFO outInfo{ MFMediaType_Video, MFVideoFormat_H264 };

        IMFActivate** acts = nullptr;
        UINT32 count = 0;
        HRESULT hr = MFTEnumEx(MFT_CATEGORY_VIDEO_ENCODER,
                               MFT_ENUM_FLAG_HARDWARE | MFT_ENUM_FLAG_SORTANDFILTER,
                               &inInfo, &outInfo, &acts, &count);
        if (FAILED(hr) || count == 0) {
            err = L"找不到硬件 H.264 编码器";
            if (acts) { for (UINT32 i = 0; i < count; ++i) acts[i]->Release(); CoTaskMemFree(acts); }
            return false;
        }

        // 优先 NVIDIA：桌面纹理在 NVIDIA 适配器上，避免跨适配器复制
        ComPtr<IMFActivate> pick = acts[0];
        for (UINT32 i = 0; i < count; ++i) {
            LPWSTR nm = nullptr; UINT32 len = 0;
            if (SUCCEEDED(acts[i]->GetAllocatedString(MFT_FRIENDLY_NAME_Attribute, &nm, &len))) {
                if (wcsstr(nm, L"NVIDIA")) pick = acts[i];
                CoTaskMemFree(nm);
            }
        }
        for (UINT32 i = 0; i < count; ++i) acts[i]->Release();
        CoTaskMemFree(acts);

        hr = pick->ActivateObject(IID_PPV_ARGS(&xf));
        if (FAILED(hr)) { err = L"激活编码器失败"; return false; }

        {
            LPWSTR nm = nullptr; UINT32 len = 0;
            if (SUCCEEDED(pick->GetAllocatedString(MFT_FRIENDLY_NAME_Attribute, &nm, &len))) {
                name = nm; CoTaskMemFree(nm);
            }
        }

        ComPtr<IMFAttributes> attrs;
        if (SUCCEEDED(xf->GetAttributes(&attrs))) {
            UINT32 a = 0;
            attrs->GetUINT32(MF_TRANSFORM_ASYNC, &a);
            async = (a != 0);
            // 异步 MFT 不解锁的话 SetInputType 直接返回 MF_E_TRANSFORM_ASYNC_LOCKED
            if (async) attrs->SetUINT32(MF_TRANSFORM_ASYNC_UNLOCK, TRUE);
        }

        // ICodecAPI 用于实时参数调节与强制关键帧
        xf.As(&codec);

        // 【实时共享的关键配置 —— 必须在协商媒体类型之前设置】
        // ICodecAPI 的值要在 SetOutputType/SetInputType 之前下发，因为编码器
        // 是在协商时就确定配置的。放在协商之后设等于没设 —— 实测踩过这个坑：
        // 参数写在后面，结果接收端解码器里恒定压着 27 帧（约 900ms 延迟）。
        //
        // 默认参数是给「离线转码」用的，直接推流延迟会很难看：
        //   · B 帧：需要重排序，解码器必须等到后续帧才能输出
        //   · 前瞻(lookahead)：编码器攒若干帧再分配码率
        //   · 参考帧数多：解码器的 DPB 要一直兜着这些帧，输出被推迟
        if (codec) {
            auto setU32 = [&](const GUID& g, ULONG v, const wchar_t* tag) {
                VARIANT var; VariantInit(&var);
                var.vt = VT_UI4; var.ulVal = v;
                const HRESULT hr = codec->SetValue(&g, &var);
                VariantClear(&var);
                Log(L"          %s = %u  hr=0x%08X\n", tag, v, (unsigned)hr);
            };
            auto setBool = [&](const GUID& g, bool v, const wchar_t* tag) {
                VARIANT var; VariantInit(&var);
                var.vt = VT_BOOL; var.boolVal = v ? VARIANT_TRUE : VARIANT_FALSE;
                const HRESULT hr = codec->SetValue(&g, &var);
                VariantClear(&var);
                Log(L"          %s = %d  hr=0x%08X\n", tag, (int)v, (unsigned)hr);
            };
            Log(L"  下发实时编码参数（必须在类型协商之前）：\n");
            setU32(CODECAPI_AVEncMPVDefaultBPictureCount, 0, L"B 帧数");
            setU32(CODECAPI_AVEncVideoMaxNumRefFrame, 1, L"参考帧数");
            setBool(CODECAPI_AVEncCommonLowLatency, true, L"低延迟模式");
            setU32(CODECAPI_AVEncCommonRateControlMode, eAVEncCommonRateControlMode_CBR, L"码控");
            setU32(CODECAPI_AVEncMPVGOPSize, fps * 2, L"GOP");
        } else {
            Log(L"  [!] 编码器不支持 ICodecAPI，实时参数未能下发\n");
        }

        ComPtr<IMFMediaType> ot, it;
        MFCreateMediaType(&ot);
        ot->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
        ot->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_H264);
        ot->SetUINT32(MF_MT_AVG_BITRATE, bitrate);
        ot->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
        ot->SetUINT32(MF_MT_MPEG2_PROFILE, eAVEncH264VProfile_High);
        MFSetAttributeSize(ot.Get(), MF_MT_FRAME_SIZE, w, h);
        MFSetAttributeRatio(ot.Get(), MF_MT_FRAME_RATE, fps, 1);
        MFSetAttributeRatio(ot.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
        hr = xf->SetOutputType(0, ot.Get(), 0);
        if (FAILED(hr)) {
            ComPtr<IMFMediaType> avail;
            if (SUCCEEDED(xf->GetOutputAvailableType(0, 0, &avail)))
                hr = xf->SetOutputType(0, avail.Get(), 0);
        }
        if (FAILED(hr)) { err = L"SetOutputType 失败"; return false; }

        MFCreateMediaType(&it);
        it->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
        it->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
        it->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
        MFSetAttributeSize(it.Get(), MF_MT_FRAME_SIZE, w, h);
        MFSetAttributeRatio(it.Get(), MF_MT_FRAME_RATE, fps, 1);
        MFSetAttributeRatio(it.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
        // 输入缓冲里的 Y 行距，稍后按实际 Map 到的 RowPitch 设置
        hr = xf->SetInputType(0, it.Get(), 0);
        if (FAILED(hr)) {
            ComPtr<IMFMediaType> avail;
            if (SUCCEEDED(xf->GetInputAvailableType(0, 0, &avail)))
                hr = xf->SetInputType(0, avail.Get(), 0);
        }
        if (FAILED(hr)) { err = L"SetInputType 失败"; return false; }

        MFT_OUTPUT_STREAM_INFO si{};
        xf->GetOutputStreamInfo(0, &si);
        providesSamples = (si.dwFlags & MFT_OUTPUT_STREAM_PROVIDES_SAMPLES) != 0;
        outBufSize = si.cbSize ? si.cbSize : (4u << 20);

        xf->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);
        xf->ProcessMessage(MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0);
        return true;
    }
};

// 判断一个访问单元里是否含 IDR（NAL 类型 5）。
// 【为什么要自己扫】编码器不会在输出样本上标注"这是关键帧"，
// 而传输层需要这个标志来让接收端知道"从这一帧可以重新开始解码"。
static bool IsIdrFrame(const uint8_t* p, DWORD len)
{
    size_t i = 0;
    while (i + 4 < len) {
        if (p[i] == 0 && p[i + 1] == 0 && p[i + 2] == 1) {
            if ((p[i + 3] & 0x1F) == 5) return true;
            i += 3;
        } else if (p[i] == 0 && p[i + 1] == 0 && p[i + 2] == 0 && p[i + 3] == 1) {
            if (i + 4 < len && (p[i + 4] & 0x1F) == 5) return true;
            i += 4;
        } else {
            ++i;
        }
    }
    return false;
}

// ---------------------------------------------------------------------------
// 收集编码输出样本（可选同时经自建 UDP 发出）
// ---------------------------------------------------------------------------
struct OutputSink {
    FILE* out = nullptr;
    size_t totalBytes = 0;
    int samples = 0;
    bool loggedHeader = false;

    // ---- UDP 发送 ----
    zx::UdpSocket* udp = nullptr;
    const char* host = nullptr;
    uint16_t port = 0;
    uint32_t frameId = 0;
    uint16_t sessionId = 0x1111;   // 会话 id：接收端据此识别"换了一条新流"（见 transport/media-packet.h）
    int datagramsSent = 0;
    size_t udpBytes = 0;
    int idrFrames = 0;
    double firstSendMs = -1;

    // 远程控制（被控端一侧的统计）
    zx::ControlAuth* auth = nullptr;   // 授权状态机（见 control-auth.h）；为空表示不要求授权
    int inputEvents = 0;     // 成功注入的事件数
    int inputFailed = 0;     // SendInput 返回 0（被系统拒绝）
    int inputMoves = 0;      // 其中的鼠标移动（日志抽样用）
    int inputStale = 0;      // 因帧号过旧被丢弃的"陈旧点击"
    int inputRejected = 0;   // 未启用注入时收到的事件
    int inputKeys = 0;       // 其中的键盘事件
    int inputAwaitingAuth = 0;  // 因等待授权被丢弃的事件
    int inputBlockedByAuth = 0; // 因已被拒绝/已停止被丢弃的事件（与上一条口径分开）
    int inputDeniedKeys = 0;    // 因"键盘未授权"被丢弃的按键

    std::vector<uint8_t> fragBuf;
    std::vector<size_t> fragSizes;

    void Take(IMFSample* sample)
    {
        ComPtr<IMFMediaBuffer> buf;
        if (FAILED(sample->ConvertToContiguousBuffer(&buf))) return;
        BYTE* p = nullptr; DWORD len = 0;
        if (FAILED(buf->Lock(&p, nullptr, &len))) return;

        if (!loggedHeader && len >= 4) {
            const bool annexB = (p[0] == 0 && p[1] == 0 && (p[2] == 1 || (p[2] == 0 && p[3] == 1)));
            Log(L"          首个输出样本: %u 字节，%s\n", len,
                annexB ? L"Annex-B 起始码正确" : L"非 Annex-B（需要转换）");
            loggedHeader = true;
        }
        if (out) fwrite(p, 1, len, out);
        totalBytes += len;
        ++samples;

        // ---- 经 UDP 发出 ----
        if (udp && udp->valid()) {
            LONGLONG ts100ns = 0;
            sample->GetSampleTime(&ts100ns);
            const uint64_t tsUs = (uint64_t)(ts100ns / 10);

            const bool isIdr = IsIdrFrame(p, len);
            if (isIdr) ++idrFrames;

            // 【默认开启 XOR 校验】回环下都能观测到偶发单分片丢失，
            // 1 个校验包（IDR 约 +1%，P 帧约 +5%）能救回任意 1 片丢失，
            // 比丢一整帧再请求 IDR 便宜得多。
            const uint16_t dataCount = zx::DataFragmentCount(len);
            const uint16_t totalCount = zx::TotalFragmentCount(len, true);
            const size_t cap = size_t(totalCount) * (zx::kHeaderSize + zx::kMaxPayload);
            if (fragBuf.size() < cap) fragBuf.resize(cap);
            if (fragSizes.size() < totalCount) fragSizes.resize(totalCount);

            size_t used = 0;
            const uint16_t written = zx::FragmentFrame(
                fragBuf.data(), fragBuf.size(), &used,
                frameId, zx::kStreamVideo, sessionId,
                isIdr ? (uint8_t)zx::kFlagKeyframe : (uint8_t)0,
                tsUs, p, (uint32_t)len, /*parity=*/true, fragSizes.data());

            size_t off = 0;
            for (uint16_t i = 0; i < written; ++i) {
                if (udp->SendTo(host, port, fragBuf.data() + off, fragSizes[i]) >= 0) {
                    ++datagramsSent;
                    udpBytes += fragSizes[i];
                }
                off += fragSizes[i];
            }
            if (firstSendMs < 0) firstSendMs = NowSeconds() * 1000.0;
            ++frameId;
            (void)dataCount;
        }

        buf->Unlock();
    }

    // 处理接收端发来的控制包：关键帧请求 + 远程控制输入
    bool PollControl(zx::UdpSocket* sock, bool injectInput, uint32_t staleWindow)
    {
        if (!sock || !sock->valid()) return false;
        bool requested = false;
        for (;;) {
            uint8_t tmp[256];
            sockaddr_in from{};
            const int n = sock->RecvFrom(tmp, sizeof(tmp), &from);
            if (n <= 0) break;
            if (n < (int)zx::kHeaderSize) continue;
            zx::MediaPacket p{};
            memcpy(&p, tmp, zx::kHeaderSize);
            if (p.magic != zx::kMagic) continue;

            if (p.type == zx::kTypeKeyframeRequest) {
                requested = true;
            } else if (p.type == zx::kTypeInput) {
                if ((size_t)n < zx::kHeaderSize + sizeof(zx::InputEvent)) continue;
                zx::InputEvent e{};
                memcpy(&e, tmp + zx::kHeaderSize, sizeof(e));

                // 【授权闸门】第一次收到输入就弹窗请求授权；未授权期间一律丢弃、不注入。
                // 默认焦点在「拒绝」、30 秒无响应自动拒绝 —— 见 control-auth.h。
                if (auth) {
                    // 第一次 = 弹窗；被拒/被停之后，换人或过了冷静期可以再次请求（否则进程内永久锁死）
                    if (auth->Idle() || auth->ShouldReask(from)) auth->Ask(from);
                    if (!auth->AllowMouse()) {
                        if (auth->Pending()) {
                            ++inputAwaitingAuth;
                            // 【静默丢弃是排查噩梦】第一次和每 100 次都留痕
                            if (inputAwaitingAuth == 1 || (inputAwaitingAuth % 100) == 0)
                                Log(L"  [授权] 尚未授权，输入被丢弃（累计 %d 个，不注入）\n",
                                    inputAwaitingAuth);
                        } else {
                            // 与"等待授权"分开计数：口径混在一起会误导排查
                            ++inputBlockedByAuth;
                            if (inputBlockedByAuth == 1 || (inputBlockedByAuth % 100) == 0)
                                Log(L"  [授权] 已被拒绝/已停止，输入被丢弃（累计 %d 个）\n",
                                    inputBlockedByAuth);
                        }
                        continue;
                    }
                    if (e.kind == zx::kInputKey && !auth->AllowKeyboard()) {
                        ++inputDeniedKeys;   // 键盘默认关：没勾选就不许注入按键
                        if (inputDeniedKeys <= 2 || (inputDeniedKeys % 20) == 0)
                            Log(L"  [授权] 丢弃按键 vk=%u：授权时没有勾选「允许键盘输入」\n",
                                (unsigned)e.vk);
                        continue;
                    }
                }
                ApplyInput(p, e, injectInput, staleWindow);
            }
        }
        return requested;
    }

    // 把查看端发来的输入事件注入本机。
    // 【帧号绑定 = 防"陈旧点击"】事件的 frameId 是查看端**当时正在看的那一帧**。
    // 如果本机画面已经往前走了很多帧，那个坐标对应的界面多半已经变了，
    // 这种过期操作必须丢掉 —— 否则会在错误的界面位置上点下去。
    void ApplyInput(const zx::MediaPacket& p, const zx::InputEvent& e,
                    bool injectInput, uint32_t staleWindow)
    {
        const uint32_t cur = frameId;              // 本端已发出的帧数
        const uint32_t age = (cur >= p.frameId) ? (cur - p.frameId) : 0;
        if (age > staleWindow) {
            ++inputStale;
            Log(L"  [控制] 丢弃陈旧输入：事件帧 %u，当前已发 %u（差 %u > 窗口 %u）\n",
                p.frameId, cur, age, staleWindow);
            return;
        }
        if (!injectInput) { ++inputRejected; return; }   // 没启用注入（默认关闭）

        INPUT in{};
        in.type = INPUT_MOUSE;

        // 【一次性自检】结构布局必须和 Windows 期望的一致：x64 下 sizeof(INPUT)==40、
        // dwFlags 在偏移 20。布局一旦被 #pragma pack 之类搞坏，SendInput 仍然返回 1，
        // 但字段会被系统按错误偏移读取 —— 表现就是"注入成功却没有效果"。
        static bool selfChecked = false;
        if (!selfChecked) {
            selfChecked = true;
            Log(L"  [控制] 自检 sizeof(INPUT)=%zu  sizeof(MOUSEINPUT)=%zu  offsetof(mi)=%zu  "
                L"offsetof(mi.dwFlags)=%zu\n",
                sizeof(INPUT), sizeof(MOUSEINPUT), offsetof(INPUT, mi),
                offsetof(INPUT, mi.dwFlags));
        }

        if (e.kind == zx::kInputMouseMove) {
            // 归一化 0..32767 → 绝对坐标 0..65535；VIRTUALDESK 让多屏也能覆盖
            in.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK;
            in.mi.dx = (LONG)e.nx * 65535 / 32767;
            in.mi.dy = (LONG)e.ny * 65535 / 32767;
        } else if (e.kind == zx::kInputMouseButton) {
            const bool down = (e.flags & zx::kInputDown) != 0;
            if (e.flags & zx::kInputLeft)         in.mi.dwFlags = down ? MOUSEEVENTF_LEFTDOWN  : MOUSEEVENTF_LEFTUP;
            else if (e.flags & zx::kInputRight)   in.mi.dwFlags = down ? MOUSEEVENTF_RIGHTDOWN : MOUSEEVENTF_RIGHTUP;
            else if (e.flags & zx::kInputMiddle)  in.mi.dwFlags = down ? MOUSEEVENTF_MIDDLEDOWN: MOUSEEVENTF_MIDDLEUP;
            else return;
        } else if (e.kind == zx::kInputMouseWheel) {
            in.mi.dwFlags = MOUSEEVENTF_WHEEL;
            in.mi.mouseData = (DWORD)e.wheel;
        } else if (e.kind == zx::kInputKey) {
            // 键盘注入：wVk 给虚拟键码，wScan 交给系统映射
            //（少数游戏/远程桌面场景只认扫描码，这里先按最通用的做法）
            in.type = INPUT_KEYBOARD;
            in.ki.wVk = (WORD)e.vk;
            in.ki.wScan = (WORD)MapVirtualKeyW(e.vk, MAPVK_VK_TO_VSC);
            in.ki.dwFlags = (e.flags & zx::kInputDown) ? 0 : KEYEVENTF_KEYUP;
        } else {
            return;
        }

        const UINT sent = SendInput(1, &in, sizeof(INPUT));
        // 【注入后立刻读回光标】SendInput 返回 1 只代表"事件进了输入流"，不代表真的生效
        //（UIPI/桌面不匹配等情况下会被静默丢掉）。读回才是客观判据。
        POINT ptAfter{};
        GetCursorPos(&ptAfter);
        const DWORD lastErr = (sent == 1) ? 0 : GetLastError();
        ++inputEvents;
        if (e.kind == zx::kInputKey) ++inputKeys;
        if (sent != 1) ++inputFailed;
        if (e.kind == zx::kInputMouseMove) {
            // 鼠标移动频率很高，日志抽样，否则日志本身会拖慢循环
            ++inputMoves;
            if (inputMoves > 5 && (inputMoves % 25) != 0) return;
        }
        Log(L"  [控制] 注入 kind=%u 归一化(%d,%d) vk=%u 事件帧=%u 已发帧=%u SendInput=%u err=%u 光标=(%ld,%ld)\n",
            (unsigned)e.kind, (int)e.nx, (int)e.ny, (unsigned)e.vk, p.frameId, cur, sent, lastErr,
            ptAfter.x, ptAfter.y);
    }
};

int wmain(int argc, wchar_t** argv)
{
    const int seconds = (argc > 1) ? _wtoi(argv[1]) : 5;
    const std::wstring outPath = ExeRelative((argc > 2) ? argv[2] : L"sender-probe.h264");
    const UINT dstW = (argc > 3) ? (UINT)_wtoi(argv[3]) : 1920;
    const UINT dstH = (argc > 4) ? (UINT)_wtoi(argv[4]) : 1080;
    const UINT fps = (argc > 5) ? (UINT)_wtoi(argv[5]) : 30;
    const UINT32 bitrate = (argc > 6) ? (UINT32)_wtoi(argv[6]) : 12000000;

    // 可选：把码流经自建 UDP 发给接收端
    //   --send <主机> <端口>    接收端地址
    //   --local <端口>          本端绑定端口（接收端的关键帧请求与远程控制输入会回到这里）
    //   --remote-control        允许接收端注入操作
    //   --no-auth               跳过授权弹窗（**只给自动化测试用**；生产绝不要加）
    //   --auth-timeout <秒>     授权窗口等待时长（默认 30，测试时可调小）
    const wchar_t* sendHost = nullptr;
    uint16_t sendPort = 0;
    uint16_t localPort = 41000;
    bool sendMode = false;
    bool injectInput = false;
    bool requireAuth = true;      // 默认要求授权：第一次收到输入就弹窗
    int authTimeoutSec = 30;
    int authCooldownSec = 30;     // 同一请求方被拒后多久才能再次弹窗
    for (int i = 1; i < argc; ++i) {
        if (wcscmp(argv[i], L"--send") == 0 && i + 2 < argc) {
            sendHost = argv[i + 1];
            sendPort = (uint16_t)_wtoi(argv[i + 2]);
            sendMode = true;
        } else if (wcscmp(argv[i], L"--local") == 0 && i + 1 < argc) {
            localPort = (uint16_t)_wtoi(argv[i + 1]);
        } else if (wcscmp(argv[i], L"--remote-control") == 0) {
            injectInput = true;
        } else if (wcscmp(argv[i], L"--no-auth") == 0) {
            requireAuth = false;
        } else if (wcscmp(argv[i], L"--auth-timeout") == 0 && i + 1 < argc) {
            authTimeoutSec = _wtoi(argv[++i]);
        } else if (wcscmp(argv[i], L"--auth-cooldown") == 0 && i + 1 < argc) {
            authCooldownSec = _wtoi(argv[++i]);
        }
    }

    // 每次运行生成一个会话 id：接收端据此识别"这是一个新会话"并重置重组器。
    // 【为什么必须要有】发送端重启或换流之后 frameId 会从 0 重来，没有会话身份的话，
    // 接收端会把整条新流当成"跨帧迟到的陈旧分片"全部丢掉（实测收包还在涨、帧数冻住）。
    const uint16_t sessionId = (uint16_t)(GetTickCount64() ^ (GetTickCount64() >> 17) ^
                                          ((uint64_t)GetCurrentProcessId() << 3));

    SetConsoleOutputCP(CP_UTF8);
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
    {
        const std::wstring lp = ExeRelative(L"sender-probe-log.txt");
        _wfopen_s(&g_log, lp.c_str(), L"w, ccs=UTF-8");
    }

    Log(L"=== 发送端链路：采集 → GPU 转 NV12 → 硬件编码 ===\n");
    Log(L"目标 %ux%u @%ufps  %.1f Mbps  时长 %d 秒\n输出 %s\n\n",
        dstW, dstH, fps, bitrate / 1e6, seconds, outPath.c_str());

    bool ok = false;
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(MFStartup(MF_VERSION))) { Log(L"[X] MFStartup 失败\n"); if (g_log) fclose(g_log); return 1; }

    // 【声明必须放在 goto 之前】C++ 不允许 goto 跳过带初始化的变量声明
    Capture cap;
    Nv12Converter conv;
    Encoder enc;
    OutputSink sink;
    zx::ControlAuth auth;   // 授权状态机（声明必须在 goto 之前）
    FILE* outFile = nullptr;

    // Winsock 必须在任何 socket 之前初始化
    zx::WsaGuard wsa;
    zx::UdpSocket sendSock;
    if (sendMode) {
        if (!wsa.ok()) { Log(L"[X] WSAStartup 失败\n"); goto done; }
        // 接收端会把关键帧请求发回本端口的来源地址，所以本端要 bind
        if (!sendSock.Open(localPort, "0.0.0.0", true, 1 << 20, 4 << 20)) {
            Log(L"[X] 本端端口 %u 绑定失败\n", localPort);
            goto done;
        }
        sendSock.SetRecvTimeout(1);
        Log(L"UDP 发送已启用：接收端 %S:%u，本端 %u\n", sendHost, sendPort, localPort);
    }

    // ---- 1. 采集 ----
    Log(L"--- 1. 屏幕采集 ---\n");
    if (!cap.Init()) goto done;

    // ---- 2. GPU 转换 ----
    Log(L"\n--- 2. BGRA → NV12（GPU）---\n");
    if (!conv.Init(cap.device.Get(), cap.context.Get(), cap.accumulated.Get(),
                   cap.width, cap.height, dstW, dstH, fps))
        goto done;

    // ---- 3. 编码器 ----
    Log(L"\n--- 3. 硬件编码器 ---\n");
    {
        std::wstring err;
        if (!enc.Init(dstW, dstH, fps, bitrate, err)) {
            Log(L"[X] %s\n", err.c_str());
            goto done;
        }
        Log(L"[OK] 编码器: %s  异步=%d  自带输出样本=%d\n",
            enc.name.c_str(), (int)enc.async, (int)enc.providesSamples);
    }

    // ---- 4. 主循环 ----
    Log(L"\n--- 4. 主循环 ---\n");
    if (_wfopen_s(&outFile, outPath.c_str(), L"wb") != 0) {
        Log(L"[X] 无法创建输出文件\n");
        goto done;
    }
    sink.out = outFile;
    if (sendMode) {
        sink.udp = &sendSock;
        sink.host = "127.0.0.1";     // 由 sendHost 覆盖
        {
            // SendTo 需要 const char*，这里把宽字符主机名转成窄字符
            static char hostBuf[64] = { 0 };
            WideCharToMultiByte(CP_ACP, 0, sendHost, -1, hostBuf, sizeof(hostBuf), nullptr, nullptr);
            sink.host = hostBuf;
        }
        sink.port = sendPort;
        sink.sessionId = sessionId;   // 本次运行的会话 id（接收端靠它识别新会话）

        // ---- 远程控制授权（设计文档 §9.5）----
        // 默认要求授权：第一次收到输入就弹窗；默认焦点在「拒绝」、30 秒无响应自动拒绝、
        // 键盘默认关。--no-auth 只给自动化测试用，生产绝不要加。
        if (injectInput && requireAuth) {
            auth.SetLogger([&](const std::wstring& s) { Log(L"%s\n", s.c_str()); });
            auth.SetTimeoutSeconds(authTimeoutSec);
            auth.SetCooldownSeconds(authCooldownSec);
            auth.StateSender = [&](uint8_t st) {
                // 把授权状态告知查看端：它据此停止继续发输入
                uint8_t pkt[zx::kHeaderSize + 1]{};
                zx::MediaPacket sp{};
                sp.magic = zx::kMagic;
                sp.version = zx::kVersion;
                sp.type = zx::kTypeControlState;
                sp.sessionId = sessionId;
                sp.payloadSize = 1;
                memcpy(pkt, &sp, zx::kHeaderSize);
                pkt[zx::kHeaderSize] = st;
                // 【发送结果必须看】状态包发不出去的话，查看端会一直傻等（实测踩过）
                const int rc = sendSock.SendToAddr(auth.Peer(), pkt, sizeof(pkt));
                wchar_t peerTxt[64]{};
                char ip[32]{};
                InetNtopA(AF_INET, &auth.Peer().sin_addr, ip, sizeof(ip));
                swprintf_s(peerTxt, L"%S:%u", ip, (unsigned)ntohs(auth.Peer().sin_port));
                Log(L"  [授权] 已把状态 %u 告知查看端 %s（sendto=%d，%d 字节）\n",
                    (unsigned)st, peerTxt, rc, (int)sizeof(pkt));
            };
            sink.auth = &auth;
            Log(L"  [授权] 已启用：第一次收到输入时弹窗（默认拒绝，%d 秒超时，键盘默认关，被拒后 %d 秒冷静期）\n",
                authTimeoutSec, authCooldownSec);
        } else if (injectInput) {
            Log(L"  [授权] [!] 已用 --no-auth 跳过授权（仅限自动化测试）\n");
        }
    }

    {
        ComPtr<IMFMediaEventGenerator> events;
        const bool hasEvents = SUCCEEDED(enc.xf->QueryInterface(IID_PPV_ARGS(&events)));

        auto collectOne = [&]() -> bool {
            MFT_OUTPUT_DATA_BUFFER db{};
            db.dwStreamID = 0;
            IMFSample* ours = nullptr;
            if (!enc.providesSamples) {
                IMFMediaBuffer* ob = nullptr;
                if (FAILED(MFCreateMemoryBuffer(enc.outBufSize, &ob))) return false;
                if (FAILED(MFCreateSample(&ours))) { ob->Release(); return false; }
                ours->AddBuffer(ob);
                ob->Release();
                db.pSample = ours;
            }
            DWORD status = 0;
            HRESULT ohr = enc.xf->ProcessOutput(0, 1, &db, &status);
            IMFSample* rel = db.pSample;
            if (db.pEvents) { db.pEvents->Release(); db.pEvents = nullptr; }
            if (ohr == MF_E_TRANSFORM_NEED_MORE_INPUT) { if (rel) rel->Release(); return false; }
            if (FAILED(ohr)) {
                if (rel) rel->Release();
                Log(L"          ProcessOutput hr=0x%08X\n", ohr);
                return false;
            }
            if (db.pSample) sink.Take(db.pSample);
            if (rel) rel->Release();
            return true;
        };

        // 异步 MFT：每个 METransformHaveOutput 只调一次 ProcessOutput
        bool drainComplete = false;
        bool needInput = true;   // 启动时允许先喂一帧
        int needInputEvents = 0;
        auto pumpOutputs = [&]() -> int {
            int got = 0;
            if (hasEvents) {
                for (;;) {
                    ComPtr<IMFMediaEvent> ev;
                    if (FAILED(events->GetEvent(MF_EVENT_FLAG_NO_WAIT, &ev))) break;
                    MediaEventType t = MEUnknown;
                    ev->GetType(&t);
                    if (t == METransformHaveOutput) { if (collectOne()) ++got; }
                    else if (t == METransformNeedInput) { needInput = true; ++needInputEvents; break; }
                    else if (t == METransformDrainComplete) { drainComplete = true; break; }
                }
            } else {
                while (collectOne()) ++got;
            }
            return got;
        };

        const double frameDurHns = 10000000.0 / fps;
        const double tStart = NowSeconds();
        int framesEncoded = 0, framesCaptured = 0, timeouts = 0;
        double nextDue = 0.0;
        bool draining = false;

        // 分段计时：不靠猜，直接量出瓶颈在哪一段
        double tPump = 0, tConvert = 0, tReadback = 0, tSubmit = 0, tOutput = 0;
        int notAcceptingHits = 0;
        double totalWait = 0;
        int forcedKeyframes = 0, suppressedRequests = 0;
        double lastForcedMs = -1e9;

        while (true) {
            const double now = NowSeconds() - tStart;
            if (now >= seconds) break;
            if (now > 120.0) { Log(L"          [!] 超时保护触发\n"); break; }

            // 【关键帧请求处理】接收端丢帧后会请求 IDR，这是唯一的恢复手段。
            // 但必须限速：一个 IDR 就是 100+ 个分片的突发，不限速会形成
            // 「请求 → 大 IDR → 更多丢包 → 更多请求」的正反馈雪崩。
            // 【授权 UI 必须在每轮都泵消息】否则弹窗不响应、30 秒倒计时也不会走。
            if (sendMode) auth.Poll();

            if (sendMode && sink.PollControl(&sendSock, injectInput, /*staleWindow=*/3)) {
                const double nowMs = NowSeconds() * 1000.0;
                if (nowMs - lastForcedMs >= 200.0) {
                    if (enc.ForceKeyframe()) {
                        lastForcedMs = nowMs;
                        ++forcedKeyframes;
                    }
                } else {
                    ++suppressedRequests;
                }
            }

            // 抓帧（超时 5ms，保证节奏不被采集卡死）
            bool gotFrame = false;
            {
                const double a = NowSeconds();
                if (!cap.Pump(5, gotFrame)) { Log(L"          [!] 采集丢失，重建\n"); break; }
                tPump += NowSeconds() - a;
            }
            if (gotFrame) ++framesCaptured; else ++timeouts;

            if (now < nextDue) continue;

            // GPU 转 NV12
            {
                const double a = NowSeconds();
                if (!conv.Convert()) break;
                tConvert += NowSeconds() - a;
            }

            // 回读 NV12 并送给编码器
            const double rbStart = NowSeconds();
            D3D11_MAPPED_SUBRESOURCE mapped{};
            if (FAILED(cap.context->Map(conv.nv12Staging.Get(), 0, D3D11_MAP_READ, 0, &mapped))) {
                Log(L"          [!] NV12 Map 失败\n");
                break;
            }
            const UINT pitch = mapped.RowPitch;
            if (conv.nv12Pitch == 0) {
                conv.nv12Pitch = pitch;
                Log(L"          NV12 行距 %u（%ux%u 期望 %u）\n", pitch, dstW, dstH, dstW);
            }
            const DWORD bufBytes = pitch * dstH * 3 / 2;

            {
                ComPtr<IMFMediaBuffer> mb;
                if (FAILED(MFCreateMemoryBuffer(bufBytes, &mb))) { cap.context->Unmap(conv.nv12Staging.Get(), 0); break; }
                BYTE* p = nullptr;
                if (FAILED(mb->Lock(&p, nullptr, nullptr))) { cap.context->Unmap(conv.nv12Staging.Get(), 0); break; }
                memcpy(p, mapped.pData, bufBytes);
                mb->Unlock();
                mb->SetCurrentLength(bufBytes);
                cap.context->Unmap(conv.nv12Staging.Get(), 0);
                tReadback += NowSeconds() - rbStart;

                ComPtr<IMFSample> sample;
                if (FAILED(MFCreateSample(&sample))) break;
                sample->AddBuffer(mb.Get());
                sample->SetSampleTime((LONGLONG)(framesEncoded * frameDurHns));
                sample->SetSampleDuration((LONGLONG)frameDurHns);

                // 【等待方式决定成败】MF_E_NOTACCEPTING(0xC00D36B5) 不是错误，
                // 而是「编码器暂时吃不下」。踩过的两个坑：
                //   1. 只重试一次就放弃 → 1080p60 只出 1 帧
                //   2. 取输出 + Sleep(1) 重试 → 每帧 25.7ms，因为 Windows 默认
                //      时钟精度 15.6ms，Sleep(1) 实际睡 15ms
                //   3. 只等 METransformNeedInput 事件 → 实测该 MFT 只派发 1 次，
                //      之后不再派发，靠事件会永久卡住
                // 结论：按墙钟限时自旋等待，成本等于编码器真实延迟，
                // 既不引入 15ms 的休眠惩罚，也不依赖不可靠的事件数。
                const double subStart = NowSeconds();
                HRESULT ihr = enc.xf->ProcessInput(0, sample.Get(), 0);
                double waited = 0;
                while (ihr == MF_E_NOTACCEPTING) {
                    waited = NowSeconds() - subStart;
                    if (waited > 0.5) break;   // 0.5 秒还吃不下就是真卡住了
                    ++notAcceptingHits;
                    pumpOutputs();             // 取走已完成的输出，腾出空间
                    SwitchToThread();
                    ihr = enc.xf->ProcessInput(0, sample.Get(), 0);
                }
                if (SUCCEEDED(ihr)) { needInput = false; totalWait += waited; }
                tSubmit += NowSeconds() - subStart;
                if (FAILED(ihr)) {
                    Log(L"          ProcessInput hr=0x%08X（第 %d 帧）%s\n",
                        ihr, framesEncoded,
                        (ihr == MF_E_NOTACCEPTING) ? L"= 0.5 秒内一直 NOTACCEPTING" : L"");
                    break;
                }
                ++framesEncoded;
            }
            {
                const double oa = NowSeconds();
                pumpOutputs();
                tOutput += NowSeconds() - oa;
            }

            // 节奏控制：落后就重置基准，避免越积越多
            nextDue += 1.0 / fps;
            if (nextDue < now) nextDue = now + 1.0 / fps;
        }

        // 排空：先通知流结束，把编码器里剩余的帧冲洗出来。
        // 【退出条件必须明确】最初只靠 "seconds + 10" 超时退出，
        // 结果 4 秒的采集跑了 14 秒 —— 全部时间都耗在这个循环里。
        enc.xf->ProcessMessage(MFT_MESSAGE_NOTIFY_END_OF_STREAM, 0);
        enc.xf->ProcessMessage(MFT_MESSAGE_COMMAND_DRAIN, 0);
        for (int spin = 0; spin < 3000; ++spin) {
            if (drainComplete) break;
            if (sink.samples >= framesEncoded) break;   // 每帧一个样本，取齐即完成
            const int got = pumpOutputs();
            if (drainComplete || sink.samples >= framesEncoded) break;
            if (got == 0) {
                if (!hasEvents) break;
                if (NowSeconds() - tStart > seconds + 5.0) break;
                Sleep(1);
            }
        }

        const double elapsed = NowSeconds() - tStart;
        Log(L"\n  采集到新画面  : %d 帧（其中 %d 次无更新超时）\n", framesCaptured, timeouts);
        Log(L"  编码帧数      : %d\n", framesEncoded);
        Log(L"  编码样本数    : %d  共 %zu 字节\n", sink.samples, sink.totalBytes);
        Log(L"  耗时          : %.3f 秒  实际帧率 %.1f fps\n",
            elapsed, elapsed > 0 ? framesEncoded / elapsed : 0.0);
        if (elapsed > 0 && sink.totalBytes > 0)
            Log(L"  实测码率      : %.2f Mbps（目标 %.2f）\n",
                sink.totalBytes * 8.0 / elapsed / 1e6, bitrate / 1e6);

        if (sendMode) {
            Log(L"\n  --- UDP 发送 ---\n");
            Log(L"  发送帧 %u 个（其中 IDR %d 个）\n", sink.frameId, sink.idrFrames);
            Log(L"  分片数据报 %d 个，共 %zu 字节（含每帧 1 个 XOR 校验包）\n",
                sink.datagramsSent, sink.udpBytes);
            Log(L"  按接收端请求强制 IDR %d 次（限速抑制 %d 次）\n",
                forcedKeyframes, suppressedRequests);
            if (sink.inputEvents > 0 || sink.inputStale > 0 || sink.inputRejected > 0 ||
                sink.inputAwaitingAuth > 0 || sink.inputBlockedByAuth > 0 ||
                sink.inputDeniedKeys > 0) {
                Log(L"\n  --- 远程控制（被控端）---\n");
                Log(L"  注入鼠标事件 %d 个（其中移动 %d，SendInput 被拒 %d）\n",
                    sink.inputEvents - sink.inputKeys, sink.inputMoves, sink.inputFailed);
                Log(L"  注入键盘事件 %d 个%s\n", sink.inputKeys,
                    sink.inputKeys > 0 ? L"（含按下与抬起）" : L"");
                Log(L"  丢弃陈旧输入 %d 个（帧号超出新鲜度窗口 %u）\n",
                    sink.inputStale, 3u);
                if (sink.inputRejected > 0)
                    Log(L"  收到但未启用注入 %d 个（本端没带 --remote-control）\n", sink.inputRejected);
                if (sink.inputAwaitingAuth > 0)
                    Log(L"  因等待/未获授权被丢弃 %d 个（授权状态下不会注入）\n", sink.inputAwaitingAuth);
                if (sink.inputBlockedByAuth > 0)
                    Log(L"  因已被拒绝/已停止被丢弃 %d 个（要重新控制需再次请求）\n",
                        sink.inputBlockedByAuth);
                if (sink.inputDeniedKeys > 0)
                    Log(L"  因键盘未授权被丢弃 %d 个（授权窗口里没勾「允许键盘输入」）\n",
                        sink.inputDeniedKeys);
                if (auth.State() != 0xFF)
                    Log(L"  最终授权状态: %u（0=等待 1=已允许 2=已拒绝 3=已被本人停止）\n",
                        (unsigned)auth.State());
            }
        }

        // 分段耗时：判断瓶颈在哪一环
        const int n = framesEncoded > 0 ? framesEncoded : 1;
        Log(L"\n  --- 每帧耗时分解（共 %d 帧）---\n", framesEncoded);
        Log(L"    采集等待      : %6.2f ms/帧   合计 %.3f 秒\n", tPump / n * 1000, tPump);
        Log(L"    GPU 转 NV12   : %6.2f ms/帧   合计 %.3f 秒\n", tConvert / n * 1000, tConvert);
        Log(L"    回读+拷入缓冲 : %6.2f ms/帧   合计 %.3f 秒\n", tReadback / n * 1000, tReadback);
        Log(L"    提交编码      : %6.2f ms/帧   合计 %.3f 秒\n", tSubmit / n * 1000, tSubmit);
        Log(L"      其中等编码器: %6.2f ms/帧   合计 %.3f 秒（NOTACCEPTING %d 次，NeedInput 事件 %d 个）\n",
            totalWait / n * 1000, totalWait, notAcceptingHits, needInputEvents);
        Log(L"    取编码输出    : %6.2f ms/帧   合计 %.3f 秒\n", tOutput / n * 1000, tOutput);
        {
            const double frameBudget = 1000.0 / fps;
            const double used = (tConvert + tReadback + tSubmit + tOutput) / n * 1000;
            Log(L"    帧预算 %.2f ms，实际占用 %.2f ms（余量 %.2f ms）\n",
                frameBudget, used, frameBudget - used);
        }

        ok = (framesEncoded > 0 && sink.samples > 0);
        if (sendMode) ok = ok && (sink.datagramsSent > 0);
    }

    Log(L"\n=== 结论 ===\n");
    if (ok) {
        Log(L"  [OK] 发送端链路打通：真实桌面 → NV12 → 硬件 H.264 码流。\n");
        Log(L"       下一步：接自建 UDP 传输，再做接收端解码渲染。\n");
    } else {
        Log(L"  [X] 链路没有跑通，按上面的错误定位。\n");
    }

done:
    if (outFile) { fflush(outFile); fclose(outFile); }
    sendSock.Close();
    enc.codec.Reset();
    enc.xf.Reset();
    conv.processor.Reset();
    conv.videoContext.Reset();
    conv.videoDevice.Reset();
    cap.dup.Reset();
    cap.context.Reset();
    cap.device.Reset();
    MFShutdown();
    CoUninitialize();
    if (g_log) fclose(g_log);
    return ok ? 0 : 1;
}
