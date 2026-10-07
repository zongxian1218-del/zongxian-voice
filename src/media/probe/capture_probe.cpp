// capture_probe.cpp —— 屏幕采集 + 硬件编码可行性验证
//
// ============================================================================
// 这个程序只做一件事：确认「Windows.Graphics.Capture 采集 → Media Foundation
// 硬件 H.264 编码」在目标机器上真的能跑通。
//
// 【为什么先写这个而不是直接写完整功能】
// 前面已经吃过教训：链路里任何一环不通，整条路都看不到结果，然后在错误的
// 方向上反复修改。所以这里把最长的那一环（采集+编码）单独拎出来验证，
// 通过之后再接 UDP 传输、解码、渲染。
//
// 验证内容：
//   1. 能否枚举到可采集的显示器
//   2. WGC 能否持续给出帧（含帧率统计）
//   3. Media Foundation 能否创建 H.264 硬件编码器
//   4. 编码器实际选了哪个 MFT（硬编还是软编）
//   5. 编码输出是否正常（字节数、帧数、估算码率）
//
// 用法：capture_probe.exe [秒数] [输出文件]
// ============================================================================

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mftransform.h>
#include <mferror.h>
#include <codecapi.h>
#include <wrl/client.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <windows.graphics.capture.interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>

#include <cstdio>
#include <chrono>
#include <vector>

// 同时输出到控制台与日志文件，每步 flush —— 崩溃时也能看到进行到哪一步
static FILE* g_log = nullptr;
static void Log(const wchar_t* fmt, ...)
{
    // 【va_list 必须每个消费者各取一次】
    // 先前写成 va_start 一次、wprintf 与 vfwprintf 共用一个 ap：
    // wprintf 会把 ap 消耗掉，vfwprintf 拿到的是已耗尽的列表，
    // 结果是控制台输出错乱、日志文件只有 BOM（实测 3 字节）。
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
    if (g_log) fflush(g_log);
}
using namespace Microsoft::WRL;
using namespace winrt::Windows::Graphics::Capture;
using namespace winrt::Windows::Graphics::DirectX::Direct3D11;

// 将 D3D11 设备包装成 WinRT 的 IDirect3DDevice，供 WGC 使用
static IDirect3DDevice CreateWinRTDevice(ID3D11Device* d3dDevice)
{
    ComPtr<IDXGIDevice> dxgiDevice;
    if (FAILED(d3dDevice->QueryInterface(IID_PPV_ARGS(&dxgiDevice)))) return nullptr;

    ComPtr<IInspectable> inspectable;
    if (FAILED(CreateDirect3D11DeviceFromDXGIDevice(dxgiDevice.Get(), &inspectable)))
        return nullptr;

    // ComPtr 没有 .as()，用 QueryInterface 取 WinRT 接口
    IDirect3DDevice result{ nullptr };
    inspectable->QueryInterface(winrt::guid_of<IDirect3DDevice>(),
                                winrt::put_abi(result));
    return result;
}

// 从 GraphicsCaptureItem 取出它的 size
static bool GetItemSize(GraphicsCaptureItem const& item, int& w, int& h)
{
    auto size = item.Size();
    w = size.Width;
    h = size.Height;
    return w > 0 && h > 0;
}

int wmain(int argc, wchar_t** argv)
{
    int seconds = (argc > 1) ? _wtoi(argv[1]) : 5;
    const wchar_t* outPath = (argc > 2) ? argv[2] : L"capture_probe.h264";
    if (seconds < 1) seconds = 5;

    _wfopen_s(&g_log, L"probe-log.txt", L"w, ccs=UTF-8");
    Log(L"=== 屏幕采集 + 硬件编码 验证 ===\n");
    Log(L"时长 %d 秒，输出 %s\n\n", seconds, outPath);

    // ---- 初始化 ----
    // 【只初始化一次 COM 单元】
    // 之前同时调用 CoInitializeEx(MTA) 与 winrt::init_apartment(MTA)，
    // 重复初始化会让 WGC 的互操作接口返回 E_ACCESSDENIED (0x80070005)。
    // 交给 C++/WinRT 统一初始化，并且用 STA —— 采集互操作在 STA 上更可靠。
    winrt::init_apartment(winrt::apartment_type::single_threaded);
    if (FAILED(MFStartup(MF_VERSION))) {
        Log(L"[X] MFStartup 失败\n");
        return 1;
    }

    // ---- 创建 D3D11 设备 ----
    // 采集到的帧是 GPU 纹理，编码器也从 GPU 取，避免回读内存
    ComPtr<ID3D11Device> d3dDevice;
    ComPtr<ID3D11DeviceContext> d3dContext;
    D3D_FEATURE_LEVEL featureLevel;
    UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
    HRESULT hr = D3D11CreateDevice(
        nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, flags,
        nullptr, 0, D3D11_SDK_VERSION,
        &d3dDevice, &featureLevel, &d3dContext);
    if (FAILED(hr)) {
        Log(L"[X] D3D11CreateDevice 失败 hr=0x%08X\n", hr);
        return 1;
    }
    Log(L"[OK] D3D11 设备已创建（feature level 0x%04X）\n", featureLevel);

    // 打印用的显卡名
    {
        ComPtr<IDXGIDevice> dxgiDev;
        if (SUCCEEDED(d3dDevice->QueryInterface(IID_PPV_ARGS(&dxgiDev)))) {
            ComPtr<IDXGIAdapter> adapter;
            if (SUCCEEDED(dxgiDev->GetAdapter(&adapter))) {
                DXGI_ADAPTER_DESC desc{};
                if (SUCCEEDED(adapter->GetDesc(&desc)))
                    Log(L"     适配器: %s\n", desc.Description);
            }
        }
    }

    auto winrtDevice = CreateWinRTDevice(d3dDevice.Get());
    if (!winrtDevice) {
        Log(L"[X] 无法创建 WinRT D3D 设备\n");
        return 1;
    }

    // ---- 确认 WGC 可用并选一个采集源 ----
    if (!GraphicsCaptureSession::IsSupported()) {
        Log(L"[X] 当前系统不支持 Windows.Graphics.Capture\n");
        return 1;
    }
    Log(L"[OK] Windows.Graphics.Capture 受支持\n");

    // 用主显示器的 GraphicsCaptureItem。
    //
    // 【互操作写法很讲究】
    // IGraphicsCaptureItemInterop::CreateForMonitor 的 ABI 签名是
    //   HRESULT CreateForMonitor(HMONITOR, REFIID, void**)
    // 用 winrt::put_abi(item) 传参不对（会被当成 IInspectable* 处理），
    // 实测直接崩溃、连错误码都打不出来。必须显式要 IInspectable 的 IID，
    // 拿到裸指针后用 attach_abi 交给 C++/WinRT 托管。
    GraphicsCaptureItem item{ nullptr };
    {
        HMONITOR hmon = nullptr;
        {
            ComPtr<IDXGIDevice> dxgiDev;
            d3dDevice->QueryInterface(IID_PPV_ARGS(&dxgiDev));
            ComPtr<IDXGIAdapter> adapter;
            dxgiDev->GetAdapter(&adapter);
            ComPtr<IDXGIOutput> output;
            if (FAILED(adapter->EnumOutputs(0, &output))) {
                Log(L"[X] 找不到显示器输出\n");
                return 1;
            }
            DXGI_OUTPUT_DESC odesc{};
            output->GetDesc(&odesc);
            hmon = odesc.Monitor;
        }
        if (!hmon) { Log(L"[X] 拿不到 HMONITOR\n"); return 1; }

        auto interop = winrt::get_activation_factory<
            GraphicsCaptureItem, IGraphicsCaptureItemInterop>();
        void* raw = nullptr;
        hr = interop->CreateForMonitor(hmon, winrt::guid_of<::IInspectable>(), &raw);
        if (FAILED(hr) || !raw) {
            Log(L"[X] CreateForMonitor 失败 hr=0x%08X\n", hr);
            return 1;
        }
        winrt::attach_abi(item, raw);
    }

    int cw = 0, ch = 0;
    GetItemSize(item, cw, ch);
    Log(L"[OK] 采集源: %d x %d\n", cw, ch);

    // ---- 创建 H.264 编码器 ----
    // 显式要求硬件加速；若不可用会返回失败，便于区分软编硬编
    ComPtr<IMFTransform> encoder;
    UINT32 encoderFlags = 0;
    {
        MFT_REGISTER_TYPE_INFO inType{ MFMediaType_Video, MFVideoFormat_NV12 };
        MFT_REGISTER_TYPE_INFO outType{ MFMediaType_Video, MFVideoFormat_H264 };
        IMFActivate** activates = nullptr;
        UINT32 count = 0;

        // 先试硬件
        hr = MFTEnumEx(MFT_CATEGORY_VIDEO_ENCODER,
                       MFT_ENUM_FLAG_HARDWARE | MFT_ENUM_FLAG_SORTANDFILTER,
                       &inType, &outType, &activates, &count);
        if (SUCCEEDED(hr) && count > 0) {
            activates[0]->ActivateObject(IID_PPV_ARGS(&encoder));
            encoderFlags = MFT_ENUM_FLAG_HARDWARE;
            Log(L"[OK] 找到硬件 H.264 编码器（%u 个候选）\n", count);
        }
        if (activates) {
            for (UINT32 i = 0; i < count; ++i) activates[i]->Release();
            CoTaskMemFree(activates);
        }

        if (!encoder) {
            Log(L"[!] 没有硬件编码器，改试软件编码\n");
            activates = nullptr; count = 0;
            hr = MFTEnumEx(MFT_CATEGORY_VIDEO_ENCODER, MFT_ENUM_FLAG_SYNCMFT,
                           &inType, &outType, &activates, &count);
            if (SUCCEEDED(hr) && count > 0) {
                activates[0]->ActivateObject(IID_PPV_ARGS(&encoder));
                encoderFlags = 0;
            }
            if (activates) {
                for (UINT32 i = 0; i < count; ++i) activates[i]->Release();
                CoTaskMemFree(activates);
            }
        }
        if (!encoder) {
            Log(L"[X] 找不到任何 H.264 编码器\n");
            return 1;
        }
    }

    // 打印编码器名字
    {
        ComPtr<IMFAttributes> attrs;
        if (SUCCEEDED(encoder->GetAttributes(&attrs))) {
            LPWSTR name = nullptr; UINT32 len = 0;
            if (SUCCEEDED(attrs->GetAllocatedString(MFT_FRIENDLY_NAME_Attribute, &name, &len))) {
                Log(L"     编码器: %s\n", name);
                CoTaskMemFree(name);
            }
        }
    }

    // ---- 配置编码器 ----
    // 1080p30 目标：验证 1080p 这一档（4K 押后）
    const UINT32 targetW = 1920, targetH = 1080, targetFps = 30;
    const UINT32 bitrate = 12000000;   // 12 Mbps

    ComPtr<IMFMediaType> inMT, outMT;
    MFCreateMediaType(&inMT);
    inMT->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    inMT->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
    MFSetAttributeSize(inMT.Get(), MF_MT_FRAME_SIZE, targetW, targetH);
    MFSetAttributeRatio(inMT.Get(), MF_MT_FRAME_RATE, targetFps, 1);
    MFSetAttributeRatio(inMT.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
    inMT->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
    // 必须设平均码率，否则编码器默认码率很低
    inMT->SetUINT32(MF_MT_AVG_BITRATE, bitrate);

    MFCreateMediaType(&outMT);
    outMT->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    outMT->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_H264);
    MFSetAttributeSize(outMT.Get(), MF_MT_FRAME_SIZE, targetW, targetH);
    MFSetAttributeRatio(outMT.Get(), MF_MT_FRAME_RATE, targetFps, 1);
    MFSetAttributeRatio(outMT.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
    outMT->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
    outMT->SetUINT32(MF_MT_AVG_BITRATE, bitrate);
    outMT->SetUINT32(MF_MT_MPEG2_PROFILE, eAVEncH264VProfile_High);

    hr = encoder->SetOutputType(0, outMT.Get(), 0);
    if (FAILED(hr)) { Log(L"[X] SetOutputType 失败 hr=0x%08X\n", hr); return 1; }
    hr = encoder->SetInputType(0, inMT.Get(), 0);
    if (FAILED(hr)) { Log(L"[X] SetInputType 失败 hr=0x%08X\n", hr); return 1; }
    Log(L"[OK] 编码器已配置 %ux%u @%ufps  %.1f Mbps\n",
            targetW, targetH, targetFps, bitrate / 1e6);

    // ---- 建立采集会话并统计 ----
    std::vector<BYTE> fileOut;
    LONGLONG encodedFrames = 0;
    LONGLONG capturedFrames = 0;
    LARGE_INTEGER freq; QueryPerformanceFrequency(&freq);
    LARGE_INTEGER t0; QueryPerformanceCounter(&t0);

    auto session = GraphicsCaptureSession::IsSupported()
        ? nullptr : nullptr;   // 占位，实际会话在下面创建

    {
        auto interop2 = item.as<IGraphicsCaptureItemInterop>();
        // 创建帧池
        auto framePool = Direct3D11CaptureFramePool::CreateFreeThreaded(
            winrtDevice, winrt::Windows::Graphics::DirectX::DirectXPixelFormat::B8G8R8A8UIntNormalized,
            2, item.Size());
        auto sess = framePool.CreateCaptureSession(item);
        // Win11 上可以关掉黄色边框；Win10 会失败，忽略
        try { sess.IsBorderRequired(false); } catch (...) { }
        try { sess.IsCursorCaptureEnabled(true); } catch (...) { }

        bool running = true;
        framePool.FrameArrived([&](auto&& pool, auto&&) {
            auto frame = pool.TryGetNextFrame();
            if (!frame) return;
            ++capturedFrames;

            // 这里只统计采集到的帧；真正的 NV12 转换与编码需要
            // 把 IDirect3DSurface 转成 ID3D11Texture2D 再走 VideoProcessor。
            // 本探针先确认"采集是否稳定出帧"，转换与编码在下一步实现。
            auto surface = frame.Surface();
            (void)surface;

            LARGE_INTEGER now; QueryPerformanceCounter(&now);
            double sec = double(now.QuadPart - t0.QuadPart) / freq.QuadPart;
            if (sec >= seconds) running = false;
        });

        sess.StartCapture();
        Log(L"[OK] 采集会话已启动，运行 %d 秒…\n", seconds);

        // 等待。FrameArrived 是自由线程回调，这里简单轮询等待
        while (running) {
            Sleep(100);
        }
        sess.Close();
        framePool.Close();
    }

    LARGE_INTEGER t1; QueryPerformanceCounter(&t1);
    double elapsed = double(t1.QuadPart - t0.QuadPart) / freq.QuadPart;

    // ---- 报告 ----
    Log(L"\n=== 结果 ===\n");
    Log(L"  时长        : %.2f 秒\n", elapsed);
    Log(L"  采集到帧数  : %lld\n", capturedFrames);
    Log(L"  实测帧率    : %.1f fps\n",
            elapsed > 0 ? capturedFrames / elapsed : 0.0);
    Log(L"  编码器类型  : %s\n",
            encoderFlags == MFT_ENUM_FLAG_HARDWARE ? L"硬件" : L"软件");
    Log(L"  分辨率      : %ux%u\n", targetW, targetH);

    // 注意：本探针尚未做 NV12 转换，所以这里不声称编码成功。
    // 它验证的是采集这一段是否稳定。
    if (capturedFrames > 0 && elapsed > 0) {
        double fps = capturedFrames / elapsed;
        Log(L"\n  判定：采集可用");
        if (fps >= 25) Log(L"，帧率达标（>=25fps）\n");
        else Log(L"，但帧率偏低（%.1f fps）—— 需要查为什么掉帧\n", fps);
    } else {
        Log(L"\n  判定：采集没有拿到帧 —— 必须查清楚再往下做\n");
    }

    MFShutdown();
    CoUninitialize();
    return 0;
}
