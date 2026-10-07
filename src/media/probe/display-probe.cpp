// display-probe.cpp —— 定位屏幕采集失败的确切原因
//
// ============================================================================
// 背景：WGC 的 CreateForMonitor 与 C# DXGI 探针都失败，前者返回 0x80070005
// (E_ACCESSDENIED)。两条完全不同的 API 报同一个错，指向共同的环境原因。
//
// 本探针不做编码、不做传输，只回答一个问题：
//   「到底是什么让采集被拒绝？」
//
// 检查顺序（每步独立，前面失败也继续走后面，全部结果都记进日志）：
//   1. 会话与桌面状态 —— 进程会话号 vs 活动控制台会话号；输入桌面能否打开；
//      桌面名是 Default 还是 Winlogon（Winlogon = 锁屏）
//   2. 枚举所有适配器 × 所有输出，打印显卡名、显示器名、是否接桌面
//   3. 对每一个 (适配器, 输出) 组合，在该适配器上建 D3D11 设备后调用
//      IDXGIOutput1::DuplicateOutput，打印精确 HRESULT
//      —— 这一步能区分「适配器不匹配」和「会话被拒」
//   4. 任意一个组合成功，就抓若干帧，存一张 BMP 作为铁证，并统计实际帧率
//
// 【为什么用 C++ 而不是 C#】
// C# 版需要手写 COM 互操作和 vtable 序号，本会话已经因此错了 5 次
// （含 2 次结构体大小不匹配导致写坏堆 → 无日志静默崩溃）。
// C++ 直接 #include 官方头文件，没有这个问题。
//
// 用法：display-probe.exe [抓帧秒数]
// 日志：display-probe-log.txt（与 exe 同目录），每步 flush，崩溃也能看到进度
// ============================================================================

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wtsapi32.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <wrl/client.h>

#include <cstdio>
#include <string>
#include <vector>
#include <chrono>

using namespace Microsoft::WRL;

// ---------------------------------------------------------------------------
// 日志：同时写文件与控制台，每行 flush
// 【教训】va_list 必须每个消费者各取一次，共用会让第二个消费者拿到空列表
// ---------------------------------------------------------------------------
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

// ---------------------------------------------------------------------------
// 第 1 步：会话与桌面状态
// ---------------------------------------------------------------------------
static void CheckSessionAndDesktop()
{
    Log(L"--- 1. 会话与桌面状态 ---\n");

    DWORD mySession = 0;
    if (ProcessIdToSessionId(GetCurrentProcessId(), &mySession))
        Log(L"  进程所在会话        : %u\n", mySession);
    else
        Log(L"  进程所在会话        : 查询失败 (err=%lu)\n", GetLastError());

    DWORD consoleSession = WTSGetActiveConsoleSessionId();
    Log(L"  活动控制台会话      : %u  (0xFFFFFFFF = 当前没有)\n", consoleSession);

    DWORD wtsSession = 0xFFFFFFFF;
    {
        DWORD bytes = 0;
        wchar_t* buf = nullptr;
        // WTSQuerySessionInformationW 取当前进程的会话号，用于交叉验证
        if (WTSQuerySessionInformationW(WTS_CURRENT_SERVER_HANDLE, WTS_CURRENT_SESSION,
                                        WTSConnectState, &buf, &bytes) && buf) {
            Log(L"  WTS 连接状态        : %u  (0=Active 4=Disconnected)\n",
                *reinterpret_cast<DWORD*>(buf));
            WTSFreeMemory(buf);
        }
    }
    (void)wtsSession;

    if (mySession != consoleSession)
        Log(L"  [!] 进程会话 != 活动控制台会话 —— 这是采集被拒的典型原因\n");
    else
        Log(L"  [OK] 进程在活动控制台会话内\n");

    // 输入桌面：锁屏时 OpenInputDesktop 会失败（err=5 拒绝访问）
    HDESK hDesk = OpenInputDesktop(0, FALSE, DESKTOP_READOBJECTS | DESKTOP_SWITCHDESKTOP);
    if (!hDesk) {
        Log(L"  [X] OpenInputDesktop 失败 err=%lu —— 桌面被锁或不可访问\n", GetLastError());
    } else {
        wchar_t name[256] = L"?";
        DWORD need = 0;
        if (GetUserObjectInformationW(hDesk, UOI_NAME, name, sizeof(name), &need))
            Log(L"  输入桌面名          : %s\n", name);
        Log(L"  [OK] 输入桌面可打开%s\n",
            (_wcsicmp(name, L"Winlogon") == 0) ? L"（但名为 Winlogon = 处于锁屏）" : L"");
        CloseDesktop(hDesk);
    }

    // 本进程所在窗口站/桌面
    {
        HWINSTA hWinSta = GetProcessWindowStation();
        wchar_t wsName[256] = L"?";
        DWORD need = 0;
        if (hWinSta && GetUserObjectInformationW(hWinSta, UOI_NAME, wsName, sizeof(wsName), &need))
            Log(L"  本进程窗口站        : %s\n", wsName);

        HDESK hThreadDesk = GetThreadDesktop(GetCurrentThreadId());
        wchar_t dName[256] = L"?";
        if (hThreadDesk && GetUserObjectInformationW(hThreadDesk, UOI_NAME, dName,
                                                    sizeof(dName), &need))
            Log(L"  本进程桌面          : %s\n", dName);
    }

    Log(L"  SM_REMOTESESSION    : %d\n", GetSystemMetrics(SM_REMOTESESSION));
    Log(L"  GetSystemMetrics 屏幕: %d x %d\n",
        GetSystemMetrics(SM_CXSCREEN), GetSystemMetrics(SM_CYSCREEN));
    Log(L"\n");
}

// ---------------------------------------------------------------------------
// 工具：创建 D3D11 设备。adapter 传 nullptr 表示用默认适配器
// ---------------------------------------------------------------------------
static HRESULT CreateDevice(IDXGIAdapter* adapter,
                            ComPtr<ID3D11Device>* outDevice,
                            ComPtr<ID3D11DeviceContext>* outContext,
                            D3D_FEATURE_LEVEL* outLevel)
{
    UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
    D3D_DRIVER_TYPE driverType = adapter ? D3D_DRIVER_TYPE_UNKNOWN : D3D_DRIVER_TYPE_HARDWARE;
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    D3D_FEATURE_LEVEL level = D3D_FEATURE_LEVEL_11_0;
    // 【必须用 D3D_DRIVER_TYPE_UNKNOWN 配合显式 adapter】
    // 指定 adapter 时若仍传 D3D_DRIVER_TYPE_HARDWARE，调用会失败
    HRESULT hr = D3D11CreateDevice(adapter, driverType, nullptr, flags,
                                   nullptr, 0, D3D11_SDK_VERSION,
                                   &device, &level, &context);
    if (SUCCEEDED(hr)) {
        *outDevice = device;
        *outContext = context;
        if (outLevel) *outLevel = level;
    }
    return hr;
}

// ---------------------------------------------------------------------------
// 抓帧并存 BMP（证明采集真的出画面，而不只是 API 返回成功）
// ---------------------------------------------------------------------------
static bool SaveBmp(ID3D11Device* device, ID3D11DeviceContext* context,
                    ID3D11Texture2D* src, const wchar_t* path,
                    UINT& outW, UINT& outH, std::string& outErr)
{
    D3D11_TEXTURE2D_DESC desc{};
    src->GetDesc(&desc);
    outW = desc.Width;
    outH = desc.Height;

    D3D11_TEXTURE2D_DESC sd = desc;
    sd.Usage = D3D11_USAGE_STAGING;
    sd.BindFlags = 0;
    sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    sd.MiscFlags = 0;
    sd.ArraySize = 1;
    sd.MipLevels = 1;
    sd.SampleDesc.Count = 1;
    sd.SampleDesc.Quality = 0;

    ComPtr<ID3D11Texture2D> staging;
    HRESULT hr = device->CreateTexture2D(&sd, nullptr, &staging);
    if (FAILED(hr)) { outErr = "CreateTexture2D(staging) 失败"; return false; }

    context->CopyResource(staging.Get(), src);

    D3D11_MAPPED_SUBRESOURCE mapped{};
    hr = context->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped);
    if (FAILED(hr)) { outErr = "Map 失败"; return false; }

    const UINT W = desc.Width, H = desc.Height;
    const UINT rowBytes = W * 3;
    const UINT pad = (4 - (rowBytes % 4)) % 4;
    const UINT stride = rowBytes + pad;
    const UINT imageBytes = stride * H;

#pragma pack(push, 1)
    struct BmpHeader {
        WORD   bfType;
        DWORD  bfSize;
        WORD   bfReserved1;
        WORD   bfReserved2;
        DWORD  bfOffBits;
        DWORD  biSize;
        LONG   biWidth;
        LONG   biHeight;
        WORD   biPlanes;
        WORD   biBitCount;
        DWORD  biCompression;
        DWORD  biSizeImage;
        LONG   biXPelsPerMeter;
        LONG   biYPelsPerMeter;
        DWORD  biClrUsed;
        DWORD  biClrImportant;
    } hdr{};
#pragma pack(pop)

    hdr.bfType = 0x4D42;              // "BM"
    hdr.bfOffBits = sizeof(BmpHeader);
    hdr.bfSize = sizeof(BmpHeader) + imageBytes;
    hdr.biSize = 40;
    hdr.biWidth = (LONG)W;
    hdr.biHeight = (LONG)H;           // 正值 = 自下而上
    hdr.biPlanes = 1;
    hdr.biBitCount = 24;
    hdr.biCompression = 0;            // BI_RGB
    hdr.biSizeImage = imageBytes;

    std::vector<BYTE> out(imageBytes, 0);
    const BYTE* base = static_cast<const BYTE*>(mapped.pData);
    for (UINT y = 0; y < H; ++y) {
        // D3D 纹理自上而下，BMP 自下而上 → 翻转
        const BYTE* srcRow = base + (size_t)y * mapped.RowPitch;
        BYTE* dstRow = out.data() + (size_t)(H - 1 - y) * stride;
        for (UINT x = 0; x < W; ++x) {
            // 源为 BGRA，BMP 24bpp 也是 BGR，直接取前 3 字节
            dstRow[x * 3 + 0] = srcRow[(size_t)x * 4 + 0];
            dstRow[x * 3 + 1] = srcRow[(size_t)x * 4 + 1];
            dstRow[x * 3 + 2] = srcRow[(size_t)x * 4 + 2];
        }
    }
    context->Unmap(staging.Get(), 0);

    FILE* f = nullptr;
    if (_wfopen_s(&f, path, L"wb") != 0 || !f) { outErr = "打开 BMP 文件失败"; return false; }
    fwrite(&hdr, sizeof(hdr), 1, f);
    fwrite(out.data(), out.size(), 1, f);
    fclose(f);
    return true;
}

// ---------------------------------------------------------------------------
// 第 3 步：对每个 (适配器, 输出) 试 DuplicateOutput，成功则抓帧
// ---------------------------------------------------------------------------
static bool TryDuplicate(IDXGIAdapter* adapter, IDXGIOutput* output, int grabSeconds,
                         int adapterIndex, int outputIndex)
{
    DXGI_OUTPUT_DESC odesc{};
    output->GetDesc(&odesc);
    Log(L"    [%d/%d] 显示器 \"%s\"  桌面坐标 (%ld,%ld)-(%ld,%ld)  接桌面=%d\n",
        adapterIndex, outputIndex, odesc.DeviceName,
        odesc.DesktopCoordinates.left, odesc.DesktopCoordinates.top,
        odesc.DesktopCoordinates.right, odesc.DesktopCoordinates.bottom,
        (int)odesc.AttachedToDesktop);

    if (!odesc.AttachedToDesktop) {
        Log(L"          跳过：未接到桌面\n");
        return false;
    }

    // 【关键】设备必须建在拥有该输出的适配器上
    // 否则 DuplicateOutput 返回 DXGI_ERROR_UNSUPPORTED 或 E_ACCESSDENIED
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    D3D_FEATURE_LEVEL level = D3D_FEATURE_LEVEL_11_0;
    HRESULT hr = CreateDevice(adapter, &device, &context, &level);
    if (FAILED(hr)) {
        Log(L"          建 D3D11 设备失败 hr=0x%08X\n", hr);
        return false;
    }

    ComPtr<IDXGIOutput1> output1;
    hr = output->QueryInterface(IID_PPV_ARGS(&output1));
    if (FAILED(hr)) {
        Log(L"          QueryInterface(IDXGIOutput1) 失败 hr=0x%08X（驱动太旧？）\n", hr);
        return false;
    }

    ComPtr<IDXGIOutputDuplication> dup;
    hr = output1->DuplicateOutput(device.Get(), &dup);
    if (FAILED(hr)) {
        const wchar_t* why = L"未知";
        switch (hr) {
        case E_ACCESSDENIED:                          why = L"E_ACCESSDENIED —— 会话/桌面不允许，或输出受保护"; break;
        case DXGI_ERROR_UNSUPPORTED:                  why = L"DXGI_ERROR_UNSUPPORTED —— 该适配器不拥有这个输出"; break;
        case DXGI_ERROR_NOT_CURRENTLY_AVAILABLE:      why = L"DXGI_ERROR_NOT_CURRENTLY_AVAILABLE —— 占用数已满(同输出最多4个)"; break;
        case DXGI_ERROR_INVALID_CALL:                 why = L"DXGI_ERROR_INVALID_CALL"; break;
        case DXGI_ERROR_SESSION_DISCONNECTED:         why = L"DXGI_ERROR_SESSION_DISCONNECTED —— 会话已断开"; break;
        case E_INVALIDARG:                            why = L"E_INVALIDARG"; break;
        case E_OUTOFMEMORY:                           why = L"E_OUTOFMEMORY"; break;
        }
        Log(L"          [X] DuplicateOutput 失败 hr=0x%08X  %s\n", hr, why);
        return false;
    }

    DXGI_OUTDUPL_DESC dd{};
    dup->GetDesc(&dd);
    Log(L"          [OK] DuplicateOutput 成功！%ux%u  格式=%u  模式=%s\n",
        dd.ModeDesc.Width, dd.ModeDesc.Height, dd.ModeDesc.Format,
        dd.DesktopImageInSystemMemory ? L"可读回" : L"仅GPU");

    // ---- 抓帧 ----
    // 【关键教训】AcquireNextFrame 返回成功，不代表画面有更新。
    // 只有鼠标移动、桌面没变时，LastPresentTime == 0 且 AccumulatedFrames == 0，
    // 这种情况下返回的纹理里根本没有桌面图像 —— 直接读它是全黑
    // （实测 mean=0 stddev=0，只有 1 种颜色，6420854 字节的纯黑 BMP）。
    // 正确做法：只把「有更新」的帧累积进自己的纹理，报告时用累积结果。
    LARGE_INTEGER freq; QueryPerformanceFrequency(&freq);
    LARGE_INTEGER t0;   QueryPerformanceCounter(&t0);

    // 累积纹理：保存最后一次真正更新的桌面画面
    D3D11_TEXTURE2D_DESC accDesc{};
    accDesc.Width = dd.ModeDesc.Width;
    accDesc.Height = dd.ModeDesc.Height;
    accDesc.Format = dd.ModeDesc.Format;
    accDesc.ArraySize = 1;
    accDesc.MipLevels = 1;
    accDesc.SampleDesc.Count = 1;
    accDesc.SampleDesc.Quality = 0;
    accDesc.Usage = D3D11_USAGE_DEFAULT;
    accDesc.BindFlags = 0;
    accDesc.CPUAccessFlags = 0;
    accDesc.MiscFlags = 0;

    ComPtr<ID3D11Texture2D> accumulated;
    HRESULT chr = device->CreateTexture2D(&accDesc, nullptr, &accumulated);
    if (FAILED(chr)) {
        Log(L"          [X] 创建累积纹理失败 hr=0x%08X\n", chr);
        return false;
    }

    int acquired = 0, timedOut = 0, errors = 0, realPresents = 0, cursorOnly = 0;
    bool haveImage = false;
    std::wstring bmpPath = L"display-probe-frame.bmp";

    for (;;) {
        LARGE_INTEGER now; QueryPerformanceCounter(&now);
        double sec = double(now.QuadPart - t0.QuadPart) / freq.QuadPart;
        if (sec >= grabSeconds) break;

        DXGI_OUTDUPL_FRAME_INFO fi{};
        ComPtr<IDXGIResource> resource;
        HRESULT ahr = dup->AcquireNextFrame(500, &fi, &resource);
        if (ahr == DXGI_ERROR_WAIT_TIMEOUT) { ++timedOut; continue; }
        if (FAILED(ahr)) {
            ++errors;
            if (errors <= 3) Log(L"          AcquireNextFrame hr=0x%08X\n", ahr);
            if (ahr == DXGI_ERROR_ACCESS_LOST) break;   // 需要重建 duplication
            continue;
        }
        ++acquired;

        // 前 8 帧打印现场：用来区分「有帧」和「有画面」
        if (acquired <= 8) {
            Log(L"          帧%-2d LastPresent=%lld Accumulated=%u 指针可见=%d 元数据=%u 字节\n",
                acquired, fi.LastPresentTime.QuadPart, fi.AccumulatedFrames,
                (int)fi.PointerPosition.Visible, fi.TotalMetadataBufferSize);
        }

        const bool hasNewImage =
            (fi.LastPresentTime.QuadPart != 0) || (fi.AccumulatedFrames > 0);
        if (hasNewImage) {
            ComPtr<ID3D11Texture2D> tex;
            if (SUCCEEDED(resource.As(&tex))) {
                context->CopyResource(accumulated.Get(), tex.Get());
                ++realPresents;
                haveImage = true;
            }
        } else {
            ++cursorOnly;
        }
        dup->ReleaseFrame();
    }

    LARGE_INTEGER t1; QueryPerformanceCounter(&t1);
    double elapsed = double(t1.QuadPart - t0.QuadPart) / freq.QuadPart;
    Log(L"          抓帧统计: 共 %d 帧 / %.2f 秒 = %.1f fps\n",
        acquired, elapsed, elapsed > 0 ? acquired / elapsed : 0.0);
    Log(L"          有画面更新 %d 帧 / 仅指针无更新 %d 帧 / 超时 %d 次 / 错误 %d 次\n",
        realPresents, cursorOnly, timedOut, errors);

    bool savedOk = false;
    if (haveImage) {
        UINT w = 0, h = 0; std::string err;
        if (SaveBmp(device.Get(), context.Get(), accumulated.Get(), bmpPath.c_str(), w, h, err)) {
            Log(L"          [OK] 已存证 BMP %s (%ux%u)\n", bmpPath.c_str(), w, h);
            savedOk = true;
        } else {
            Log(L"          [!] 存 BMP 失败: %S\n", err.c_str());
        }
    } else {
        Log(L"          [!] 整段时间内没有任何带画面的帧 —— 只有指针更新\n");
    }

    // 必须真的拿到有画面的帧，这条组合才算可用
    return savedOk;
}

int wmain(int argc, wchar_t** argv)
{
    int grabSeconds = (argc > 1) ? _wtoi(argv[1]) : 3;
    if (grabSeconds < 1) grabSeconds = 3;

    SetConsoleOutputCP(CP_UTF8);
    _wfopen_s(&g_log, L"display-probe-log.txt", L"w, ccs=UTF-8");

    Log(L"=== 屏幕采集失败原因定位 ===\n");
    Log(L"抓帧时长 %d 秒，日志 display-probe-log.txt\n\n", grabSeconds);
    Log(L"进程 ID %lu，64 位=%d\n\n", GetCurrentProcessId(), (int)(sizeof(void*) == 8));

    // COM 用 MTA：本探针没有 UI，MTA 足够且避免与 WinUI 的 STA 混淆
    HRESULT hrCo = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    Log(L"CoInitializeEx hr=0x%08X\n\n", hrCo);

    CheckSessionAndDesktop();

    // ---- 2. 枚举适配器 × 输出 ----
    Log(L"--- 2. 适配器与显示器枚举 ---\n");

    ComPtr<IDXGIFactory1> factory;
    HRESULT hr = CreateDXGIFactory1(IID_PPV_ARGS(&factory));
    if (FAILED(hr)) {
        Log(L"[X] CreateDXGIFactory1 失败 hr=0x%08X\n", hr);
        if (g_log) fclose(g_log);
        return 1;
    }

    // 先记录默认适配器是谁，再看它有没有输出
    {
        ComPtr<IDXGIAdapter> defAdapter;
        if (SUCCEEDED(factory->EnumAdapters(0, &defAdapter))) {
            DXGI_ADAPTER_DESC ad{};
            if (SUCCEEDED(defAdapter->GetDesc(&ad)))
                Log(L"  默认适配器(索引0)   : %s\n", ad.Description);
        }
    }
    // 默认 D3D11 设备落在哪个适配器上
    {
        ComPtr<ID3D11Device> dev;
        ComPtr<ID3D11DeviceContext> ctx;
        D3D_FEATURE_LEVEL lvl;
        if (SUCCEEDED(CreateDevice(nullptr, &dev, &ctx, &lvl))) {
            ComPtr<IDXGIDevice> dxgiDev;
            dev->QueryInterface(IID_PPV_ARGS(&dxgiDev));
            ComPtr<IDXGIAdapter> owner;
            if (dxgiDev && SUCCEEDED(dxgiDev->GetAdapter(&owner))) {
                DXGI_ADAPTER_DESC ad{};
                owner->GetDesc(&ad);
                Log(L"  D3D11 默认设备归属   : %s  (feature level 0x%04X)\n",
                    ad.Description, lvl);
            }
        }
    }
    Log(L"\n");

    bool anySuccess = false;
    int adapterIndex = 0;
    for (;;) {
        ComPtr<IDXGIAdapter1> adapter;
        if (factory->EnumAdapters1(adapterIndex, &adapter) == DXGI_ERROR_NOT_FOUND) break;

        DXGI_ADAPTER_DESC1 desc{};
        adapter->GetDesc1(&desc);
        Log(L"  适配器 %d: %s\n", adapterIndex, desc.Description);
        Log(L"         显存 %llu MB  厂商 0x%04X  设备 0x%04X  标志 0x%X%s\n",
            (unsigned long long)(desc.DedicatedVideoMemory / (1024 * 1024)),
            desc.VendorId, desc.DeviceId, desc.Flags,
            (desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) ? L" [软件]" : L"");
        {
            LUID luid = desc.AdapterLuid;
            Log(L"         LUID 0x%08X:0x%08X\n", luid.HighPart, luid.LowPart);
        }

        int outputIndex = 0;
        bool adapterHadOutput = false;
        for (;;) {
            ComPtr<IDXGIOutput> output;
            HRESULT ohr = adapter->EnumOutputs(outputIndex, &output);
            if (ohr == DXGI_ERROR_NOT_FOUND) break;
            if (FAILED(ohr)) {
                Log(L"    EnumOutputs(%d) 失败 hr=0x%08X\n", outputIndex, ohr);
                break;
            }
            adapterHadOutput = true;
            if (TryDuplicate(adapter.Get(), output.Get(), grabSeconds,
                             adapterIndex, outputIndex))
                anySuccess = true;
            ++outputIndex;
        }
        if (!adapterHadOutput)
            Log(L"         （此适配器没有输出）\n");
        Log(L"\n");
        ++adapterIndex;
    }

    // ---- 结论 ----
    Log(L"=== 结论 ===\n");
    if (anySuccess) {
        Log(L"  [OK] 至少有一个 (适配器, 显示器) 组合可以采集。\n");
        Log(L"       下一步：用这个组合做 NV12 转换 + 硬件 H.264 编码。\n");
    } else {
        Log(L"  [X] 所有组合都失败。按上面的错误码判断：\n");
        Log(L"      · 全部 E_ACCESSDENIED 且桌面为 Winlogon / 会话不匹配\n");
        Log(L"        → 环境问题：需要在已解锁的活动控制台会话中运行\n");
        Log(L"      · 全部 DXGI_ERROR_UNSUPPORTED\n");
        Log(L"        → 适配器与显示器的对应关系问题\n");
        Log(L"      · 全部 NOT_CURRENTLY_AVAILABLE\n");
        Log(L"        → 有别的程序（录屏/远程软件/虚拟显示器驱动）占满了\n");
    }

    if (g_log) fclose(g_log);
    if (SUCCEEDED(hrCo)) CoUninitialize();
    return anySuccess ? 0 : 1;
}
