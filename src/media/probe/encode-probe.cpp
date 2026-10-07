// encode-probe.cpp —— 硬件 H.264 编码可行性验证（与采集解耦）
//
// ============================================================================
// 【为什么用合成帧而不是真实采集帧】
// 上一阶段的教训：链路里两环一起测，失败时分不清是哪一环。
// 先让编码器吃合成 NV12（带运动图案，保证有码率输出），把编码这一环
// 单独验证通过；之后再接 ID3D11VideoProcessor 做 BGRA→NV12，最后才和
// DXGI 采集拼起来。每一步只引入一个新变量。
//
// 验证内容：
//   1. 系统里到底有哪些视频编码器（名字 + 是否异步 + 是否硬件）
//   2. 选中硬件 H.264 编码器后能否协商成功 1920x1080 NV12 → H.264
//   3. 异步 MFT 的事件协议能否正确走通（METransformNeedInput/HaveOutput）
//   4. 输出是不是真正的 Annex-B H.264 码流（看起始码 00 00 00 01）
//   5. 实际吞吐与码率（判断是否真的走了硬件）
//
// 用法：encode-probe.exe [秒数] [输出.h264]
// 日志：encode-probe-log.txt，每步 flush
// ============================================================================

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mftransform.h>
#include <mferror.h>
#include <codecapi.h>
#include <wrl/client.h>

#include <cstdio>
#include <string>
#include <vector>

using namespace Microsoft::WRL;

// ---------------------------------------------------------------------------
// 日志：va_list 每个消费者各取一次（上一轮踩过的坑）
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

static double NowSeconds()
{
    static LARGE_INTEGER freq = [] { LARGE_INTEGER f; QueryPerformanceFrequency(&f); return f; }();
    LARGE_INTEGER c; QueryPerformanceCounter(&c);
    return double(c.QuadPart) / double(freq.QuadPart);
}

// 把相对文件名解析到 exe 所在目录。
// 【为什么需要】探针的日志与输出原本写在「当前工作目录」，而调用者的工作目录
// 未必等于 exe 目录 —— 已经因此两次把结果写到别处，看起来像程序根本没运行。
static std::wstring ExeRelative(const wchar_t* name)
{
    if (!name || !name[0]) return std::wstring();
    // 已经是绝对路径（D:\... 或 \\server\share）就直接用
    if (name[0] == L'\\' || (name[0] && name[1] == L':')) return name;

    wchar_t path[MAX_PATH]{};
    DWORD n = GetModuleFileNameW(nullptr, path, MAX_PATH);
    std::wstring s(path, n);
    const size_t pos = s.find_last_of(L"\\/");
    if (pos != std::wstring::npos) s.resize(pos + 1); else s.clear();
    return s + name;
}

// ---------------------------------------------------------------------------
// 第 1 步：枚举所有视频编码器
// ---------------------------------------------------------------------------
struct EncoderInfo {
    std::wstring name;
    bool isAsync = false;
    bool isHardware = false;
    ComPtr<IMFActivate> activate;
};

static std::vector<EncoderInfo> EnumerateEncoders()
{
    std::vector<EncoderInfo> result;

    MFT_REGISTER_TYPE_INFO inType{ MFMediaType_Video, MFVideoFormat_NV12 };
    MFT_REGISTER_TYPE_INFO outType{ MFMediaType_Video, MFVideoFormat_H264 };

    // 用 MFT_ENUM_FLAG_ALL 列出全部，再逐个读属性判断硬/软、同步/异步
    IMFActivate** activates = nullptr;
    UINT32 count = 0;
    HRESULT hr = MFTEnumEx(MFT_CATEGORY_VIDEO_ENCODER, MFT_ENUM_FLAG_ALL,
                           &inType, &outType, &activates, &count);
    if (FAILED(hr)) {
        Log(L"  [X] MFTEnumEx 失败 hr=0x%08X\n", hr);
        return result;
    }

    Log(L"  共找到 %u 个 NV12→H.264 编码器：\n", count);
    for (UINT32 i = 0; i < count; ++i) {
        EncoderInfo info;
        info.activate = activates[i];

        UINT32 async = 0;
        activates[i]->GetUINT32(MF_TRANSFORM_ASYNC, &async);
        info.isAsync = (async != 0);

        // 硬件标志：MFT_ENUM_FLAG_HARDWARE 枚举出来的才算硬件
        // 这里用重枚举的方式判断太绕，改为逐个单独试硬件枚举后比对名字
        LPWSTR name = nullptr; UINT32 len = 0;
        if (SUCCEEDED(activates[i]->GetAllocatedString(MFT_FRIENDLY_NAME_Attribute, &name, &len))) {
            info.name = name;
            CoTaskMemFree(name);
        } else {
            info.name = L"(无名字)";
        }

        Log(L"    [%u] %s   异步=%d\n", i, info.name.c_str(), (int)info.isAsync);
        result.push_back(std::move(info));
    }

    // MFTEnumEx 分配的数组每个元素都要 Release，数组本身 CoTaskMemFree
    for (UINT32 i = 0; i < count; ++i) activates[i]->Release();
    CoTaskMemFree(activates);

    if (result.empty()) Log(L"  [X] 没有任何 NV12→H.264 编码器\n");
    return result;
}

// 单独查一遍「只算硬件」的列表，用于标记哪个是硬件
static bool IsHardwareEncoder(const std::wstring& name)
{
    MFT_REGISTER_TYPE_INFO inType{ MFMediaType_Video, MFVideoFormat_NV12 };
    MFT_REGISTER_TYPE_INFO outType{ MFMediaType_Video, MFVideoFormat_H264 };
    IMFActivate** activates = nullptr;
    UINT32 count = 0;
    HRESULT hr = MFTEnumEx(MFT_CATEGORY_VIDEO_ENCODER,
                           MFT_ENUM_FLAG_HARDWARE | MFT_ENUM_FLAG_SORTANDFILTER,
                           &inType, &outType, &activates, &count);
    bool found = false;
    if (SUCCEEDED(hr)) {
        for (UINT32 i = 0; i < count; ++i) {
            LPWSTR n = nullptr; UINT32 len = 0;
            if (SUCCEEDED(activates[i]->GetAllocatedString(MFT_FRIENDLY_NAME_Attribute, &n, &len))) {
                if (name == n) found = true;
                CoTaskMemFree(n);
            }
            activates[i]->Release();
        }
        CoTaskMemFree(activates);
    }
    return found;
}

// ---------------------------------------------------------------------------
// 合成 NV12 帧：Y 平面做移动的亮块 + 渐变，UV 恒 128（灰）
// 有运动才有码率，静止画面测不出编码是否真的工作
// ---------------------------------------------------------------------------
static void FillSyntheticNV12(std::vector<BYTE>& buf, UINT w, UINT h, int frameIndex)
{
    const size_t ySize = size_t(w) * h;
    if (buf.size() != ySize + ySize / 2) buf.resize(ySize + ySize / 2);

    BYTE* Y = buf.data();
    for (UINT y = 0; y < h; ++y) {
        BYTE* row = Y + size_t(y) * w;
        for (UINT x = 0; x < w; ++x) {
            row[x] = (BYTE)((x + y + frameIndex * 7) & 0xFF);
        }
    }
    // 移动的白色方块：保证每帧都有真实运动和足够码率
    const int bx = (frameIndex * 13) % (int)(w > 200 ? w - 200 : 1);
    const int by = (frameIndex * 9) % (int)(h > 200 ? h - 200 : 1);
    for (int y = by; y < by + 200 && y < (int)h; ++y)
        for (int x = bx; x < bx + 200 && x < (int)w; ++x)
            Y[size_t(y) * w + x] = 235;

    // UV 平面：中性灰，形状随帧微变，避免编码器把色度完全跳过
    BYTE* UV = buf.data() + ySize;
    memset(UV, 128, ySize / 2);
    for (UINT y = 0; y < h / 2; ++y) {
        BYTE* row = UV + size_t(y) * w;
        for (UINT x = 0; x < w / 2; ++x) {
            row[x * 2 + 0] = (BYTE)(128 + ((x + frameIndex) & 0x1F));
            row[x * 2 + 1] = (BYTE)(128 + ((y + frameIndex) & 0x1F));
        }
    }
}

// ---------------------------------------------------------------------------
// 输出样本收集
// ---------------------------------------------------------------------------
static void CollectOutputSample(IMFSample* sample, FILE* out, size_t& totalBytes,
                                bool& loggedHeader, int frameNo)
{
    ComPtr<IMFMediaBuffer> buf;
    if (FAILED(sample->ConvertToContiguousBuffer(&buf))) return;

    BYTE* p = nullptr; DWORD len = 0;
    if (FAILED(buf->Lock(&p, nullptr, &len))) return;

    if (!loggedHeader && len >= 8) {
        Log(L"          编码输出前 12 字节: ");
        for (DWORD i = 0; i < 12 && i < len; ++i) Log(L"%02X ", p[i]);
        // Annex-B 起始码是 00 00 00 01 或 00 00 01
        bool annexB = (len >= 4 && p[0] == 0 && p[1] == 0 && (p[2] == 1 || (p[2] == 0 && p[3] == 1)));
        Log(L"\n          格式判定: %s\n", annexB ? L"Annex-B（带起始码，可直接走自建 UDP）"
                                                  : L"非 Annex-B —— 需要转换或改配置");
        loggedHeader = true;
    }

    if (out) fwrite(p, 1, len, out);

    LONGLONG ts = 0;
    sample->GetSampleTime(&ts);
    if (frameNo >= 0 && frameNo < 3)
        Log(L"          输出样本 #%d  %u 字节  时间戳 %lld\n", frameNo, len, ts);

    totalBytes += len;
    buf->Unlock();
}

// ---------------------------------------------------------------------------
// 第 3 步：喂帧 + 取码流
// ---------------------------------------------------------------------------
struct EncodeStats {
    int framesSent = 0;
    int samplesOut = 0;
    size_t totalBytes = 0;
    bool usedEvents = false;
    bool ok = false;
    std::wstring error;
};

static EncodeStats RunEncode(IMFTransform* xf, UINT w, UINT h, UINT fps, int totalFrames,
                             FILE* out)
{
    EncodeStats st;

    // 编码器要求先 SetOutputType 再 SetInputType
    ComPtr<IMFMediaType> outType, inType;

    // ---- 重新协商输出类型（ProcessOutput 可能要求换类型，这里统一处理）----
    auto renegotiateOutput = [&]() -> HRESULT {
        ComPtr<IMFMediaType> avail;
        HRESULT hr = xf->GetOutputAvailableType(0, 0, &avail);
        if (FAILED(hr)) return hr;
        return xf->SetOutputType(0, avail.Get(), 0);
    };

    xf->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);
    xf->ProcessMessage(MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0);

    // ---- 准备输入样本 ----
    // 合成 NV12：Y 平面紧密排列（stride = w），UV 平面紧随其后
    std::vector<BYTE> nv12;
    const DWORD inputBytes = DWORD(size_t(w) * h * 3 / 2);

    ComPtr<IMFMediaEventGenerator> events;
    bool hasEvents = SUCCEEDED(xf->QueryInterface(IID_PPV_ARGS(&events)));
    st.usedEvents = hasEvents;
    Log(L"          事件驱动模式: %s\n", hasEvents ? L"是（异步 MFT 标准做法）" : L"否（改用轮询）");

    const double frameDurationHns = 10000000.0 / fps;
    const double tStart = NowSeconds();

    // 计算输出样本缓冲区大小
    MFT_OUTPUT_STREAM_INFO si{};
    xf->GetOutputStreamInfo(0, &si);
    const bool providesSamples = (si.dwFlags & MFT_OUTPUT_STREAM_PROVIDES_SAMPLES) != 0;
    DWORD outBufSize = si.cbSize ? si.cbSize : (4u << 20);
    if (outBufSize < (1u << 20)) outBufSize = (1u << 20);

    int sent = 0;
    bool draining = false;
    bool loggedHeader = false;
    const double kTimeout = 60.0;

    auto feedOne = [&]() -> bool {
        if (sent >= totalFrames) return false;
        FillSyntheticNV12(nv12, w, h, sent);

        ComPtr<IMFMediaBuffer> mb;
        if (FAILED(MFCreateMemoryBuffer(inputBytes, &mb))) { st.error = L"MFCreateMemoryBuffer 失败"; return false; }
        BYTE* p = nullptr;
        if (FAILED(mb->Lock(&p, nullptr, nullptr))) { st.error = L"输入缓冲 Lock 失败"; return false; }
        memcpy(p, nv12.data(), inputBytes);
        mb->Unlock();
        mb->SetCurrentLength(inputBytes);

        ComPtr<IMFSample> sample;
        if (FAILED(MFCreateSample(&sample))) { st.error = L"MFCreateSample 失败"; return false; }
        sample->AddBuffer(mb.Get());
        sample->SetSampleTime((LONGLONG)(sent * frameDurationHns));
        sample->SetSampleDuration((LONGLONG)frameDurationHns);

        HRESULT hr = xf->ProcessInput(0, sample.Get(), 0);
        if (hr == MF_E_NOTACCEPTING) {
            // 编码器暂时吃不下，这不是错误：调用方排空输出后会重试
            return false;
        }
        if (FAILED(hr)) {
            wchar_t b[128];
            swprintf_s(b, L"ProcessInput 失败 hr=0x%08X（第 %d 帧）", hr, sent);
            st.error = b;
            return false;
        }
        ++sent;
        st.framesSent = sent;
        return true;
    };

    // 【所有权规则】自己创建的样本用裸指针管理，保证每个样本只 Release 一次。
    // 之前用 ComPtr 包着再手工 Release，同一样本被释放两次。
    auto drainOutputs = [&]() -> int {
        int got = 0;
        for (;;) {
            // 【边界】已经取够样本就不要再问编码器要了。
            // 流排空后 NVENC 的 ProcessOutput 返回 E_UNEXPECTED(0x8000FFFF)，
            // 而不是 MF_E_TRANSFORM_NEED_MORE_INPUT；多问这一次会污染错误状态。
            if (st.samplesOut >= totalFrames) break;

            MFT_OUTPUT_DATA_BUFFER db{};
            db.dwStreamID = 0;
            IMFSample* ours = nullptr;   // 仅在 MFT 不提供样本时自己创建

            if (!providesSamples) {
                IMFMediaBuffer* ob = nullptr;
                if (FAILED(MFCreateMemoryBuffer(outBufSize, &ob))) break;
                if (FAILED(MFCreateSample(&ours))) { ob->Release(); break; }
                ours->AddBuffer(ob);
                ob->Release();
                db.pSample = ours;
            }

            DWORD status = 0;
            HRESULT hr = xf->ProcessOutput(0, 1, &db, &status);

            // MFT 若自己提供了样本，db.pSample 指向新样本；否则就是我们传进去的 ours。
            // 无论哪种情况，这里拿到唯一的释放责任。
            IMFSample* toRelease = db.pSample;
            if (db.pEvents) { db.pEvents->Release(); db.pEvents = nullptr; }

            if (hr == MF_E_TRANSFORM_NEED_MORE_INPUT) {
                if (toRelease) toRelease->Release();
                break;
            }
            if (hr == MF_E_TRANSFORM_STREAM_CHANGE) {
                if (toRelease) toRelease->Release();
                Log(L"          [信息] MFT 要求更换输出类型，重新协商…\n");
                if (FAILED(renegotiateOutput())) { st.error = L"重新协商输出类型失败"; break; }
                continue;
            }
            if (FAILED(hr)) {
                if (toRelease) toRelease->Release();
                wchar_t b[128];
                swprintf_s(b, L"ProcessOutput 失败 hr=0x%08X", hr);
                st.error = b;
                break;
            }
            if (db.pSample) {
                CollectOutputSample(db.pSample, out, st.totalBytes, loggedHeader, st.samplesOut);
                ++st.samplesOut;
                ++got;
            }
            if (toRelease) toRelease->Release();
        }
        return got;
    };

    // 【异步 MFT 的调用协议】每个 METransformHaveOutput 事件只能调用一次
    // ProcessOutput。多调一次不会返回 MF_E_TRANSFORM_NEED_MORE_INPUT，
    // 而是返回 E_UNEXPECTED(0x8000FFFF) —— 这是实测踩到的坑。
    // 所以异步路径用 collectOne（取一个），同步路径才用 drainOutputs（循环取空）。
    auto collectOne = [&]() -> bool {
        MFT_OUTPUT_DATA_BUFFER db{};
        db.dwStreamID = 0;
        IMFSample* ours = nullptr;

        if (!providesSamples) {
            IMFMediaBuffer* ob = nullptr;
            if (FAILED(MFCreateMemoryBuffer(outBufSize, &ob))) return false;
            if (FAILED(MFCreateSample(&ours))) { ob->Release(); return false; }
            ours->AddBuffer(ob);
            ob->Release();
            db.pSample = ours;
        }

        DWORD status = 0;
        HRESULT hr = xf->ProcessOutput(0, 1, &db, &status);
        IMFSample* toRelease = db.pSample;
        if (db.pEvents) { db.pEvents->Release(); db.pEvents = nullptr; }

        if (hr == MF_E_TRANSFORM_STREAM_CHANGE) {
            if (toRelease) toRelease->Release();
            Log(L"          [信息] MFT 要求更换输出类型，重新协商…\n");
            if (FAILED(renegotiateOutput())) { st.error = L"重新协商输出类型失败"; return false; }
            return false;
        }
        if (hr == MF_E_TRANSFORM_NEED_MORE_INPUT) {
            if (toRelease) toRelease->Release();
            return false;
        }
        if (FAILED(hr)) {
            if (toRelease) toRelease->Release();
            wchar_t b[128];
            swprintf_s(b, L"ProcessOutput 失败 hr=0x%08X", hr);
            st.error = b;
            return false;
        }
        if (db.pSample) {
            CollectOutputSample(db.pSample, out, st.totalBytes, loggedHeader, st.samplesOut);
            ++st.samplesOut;
        }
        if (toRelease) toRelease->Release();
        return true;
    };

    if (hasEvents) {
        // ---- 异步 MFT：完全事件驱动，不做任何"猜着要"的调用 ----
        while (st.samplesOut < totalFrames) {
            if (NowSeconds() - tStart > kTimeout) { st.error = L"等待编码输出超时"; break; }

            ComPtr<IMFMediaEvent> ev;
            HRESULT ehr = events->GetEvent(MF_EVENT_FLAG_NO_WAIT, &ev);
            if (ehr == MF_E_NO_EVENTS_AVAILABLE) {
                // 【不要在这里调 ProcessOutput】没有事件就说明没有输出可取
                Sleep(1);
                continue;
            }
            if (FAILED(ehr)) {
                wchar_t b[128];
                swprintf_s(b, L"GetEvent 失败 hr=0x%08X", ehr);
                st.error = b;
                break;
            }

            MediaEventType type = MEUnknown;
            ev->GetType(&type);

            if (type == METransformNeedInput) {
                if (!draining && sent < totalFrames) {
                    // 喂不进去但不是硬错误（如 MF_E_NOTACCEPTING），等下一个事件
                    if (!feedOne() && !st.error.empty()) break;
                } else if (!draining) {
                    // 帧喂完了：通知流结束并请求排空
                    xf->ProcessMessage(MFT_MESSAGE_NOTIFY_END_OF_STREAM, 0);
                    xf->ProcessMessage(MFT_MESSAGE_COMMAND_DRAIN, 0);
                    draining = true;
                }
            } else if (type == METransformHaveOutput) {
                collectOne();
            } else if (type == METransformDrainComplete) {
                break;
            }
        }
    } else {
        // ---- 同步 MFT：轮询 ----
        while (st.samplesOut < totalFrames) {
            if (NowSeconds() - tStart > kTimeout) { st.error = L"编码超时"; break; }

            // 先排空，给编码器腾空间
            drainOutputs();

            if (sent < totalFrames) {
                // 排空后仍喂不进去，说明真的失败了
                if (!feedOne()) {
                    drainOutputs();
                    if (!feedOne()) break;
                }
            } else {
                xf->ProcessMessage(MFT_MESSAGE_NOTIFY_END_OF_STREAM, 0);
                xf->ProcessMessage(MFT_MESSAGE_COMMAND_DRAIN, 0);
                drainOutputs();
                if (st.samplesOut >= st.framesSent) break;
                Sleep(2);
            }
        }
        // 同步路径可以安全地循环排空到 NEED_MORE_INPUT
        if (st.samplesOut < totalFrames) drainOutputs();
    }

    const double elapsed = NowSeconds() - tStart;
    Log(L"          喂入帧数 %d / 取回编码样本 %d / 共 %zu 字节\n",
        st.framesSent, st.samplesOut, st.totalBytes);
    Log(L"          耗时 %.3f 秒  编码速度 %.1f fps（%.2f 倍实时）\n",
        elapsed, elapsed > 0 ? st.framesSent / elapsed : 0.0,
        elapsed > 0 ? (st.framesSent / elapsed) / fps : 0.0);
    if (elapsed > 0 && st.totalBytes > 0)
        Log(L"          实测码率 %.2f Mbps\n", st.totalBytes * 8.0 / elapsed / 1e6);

    st.ok = (st.samplesOut > 0 && st.totalBytes > 0 && st.error.empty());
    return st;
}

int wmain(int argc, wchar_t** argv)
{
    int seconds = (argc > 1) ? _wtoi(argv[1]) : 5;
    if (seconds < 1) seconds = 5;
    const wchar_t* outPathArg = (argc > 2) ? argv[2] : L"encode-probe.h264";
    const std::wstring outPath = ExeRelative(outPathArg);

    SetConsoleOutputCP(CP_UTF8);
    // 【禁用 WER 崩溃对话框】否则访问违例时 Windows 会弹窗并挂住进程，
    // 进程不退出 → exe 被锁 → 下一次链接报 LNK1104，看起来像编译问题。
    // 实测：崩溃后 encode-probe.exe 仍存活，链接器无法覆盖它。
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
    {
        const std::wstring logPath = ExeRelative(L"encode-probe-log.txt");
        _wfopen_s(&g_log, logPath.c_str(), L"w, ccs=UTF-8");
    }

    const UINT W = 1920, H = 1080, FPS = 30;
    const UINT32 bitrate = 12000000;

    Log(L"=== 硬件 H.264 编码验证（合成输入，与采集解耦）===\n");
    Log(L"目标 %ux%u @%ufps  %.1f Mbps  时长 %d 秒  输出 %s\n\n",
        W, H, FPS, bitrate / 1e6, seconds, outPath.c_str());

    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    Log(L"CoInitializeEx hr=0x%08X\n", hr);
    if (FAILED(MFStartup(MF_VERSION))) {
        Log(L"[X] MFStartup 失败\n");
        return 1;
    }

    // ---- 1. 枚举 ----
    Log(L"--- 1. 编码器枚举 ---\n");
    auto encoders = EnumerateEncoders();
    for (auto& e : encoders) e.isHardware = IsHardwareEncoder(e.name);

    Log(L"\n  硬件编码器:\n");
    bool anyHw = false;
    for (auto& e : encoders) {
        if (e.isHardware) { Log(L"    * %s  异步=%d\n", e.name.c_str(), (int)e.isAsync); anyHw = true; }
    }
    if (!anyHw) Log(L"    （无）—— 只能走软件编码\n");

    // ---- 2. 激活选中的编码器 ----
    // 【选型依据】桌面采集纹理在 NVIDIA 适配器上（显示器挂在 RTX 3060 Ti）。
    // 选 NVIDIA 编码器可以做到 GPU 内部零拷贝；选 Intel QSV 则要跨适配器
    // 复制一次，1080p60 下很浪费。所以优先级：NVIDIA 硬件 > 其它硬件 > 软件。
    Log(L"\n--- 2. 激活编码器 ---\n");

    std::vector<size_t> order;
    for (size_t i = 0; i < encoders.size(); ++i)
        if (encoders[i].isHardware && encoders[i].name.find(L"NVIDIA") != std::wstring::npos)
            order.push_back(i);
    for (size_t i = 0; i < encoders.size(); ++i)
        if (encoders[i].isHardware && encoders[i].name.find(L"NVIDIA") == std::wstring::npos)
            order.push_back(i);
    for (size_t i = 0; i < encoders.size(); ++i)
        if (!encoders[i].isHardware) order.push_back(i);

    ComPtr<IMFTransform> xf;
    std::wstring chosen;
    for (size_t idx : order) {
        ComPtr<IMFTransform> t;
        HRESULT ahr = encoders[idx].activate->ActivateObject(IID_PPV_ARGS(&t));
        if (SUCCEEDED(ahr) && t) {
            xf = t;
            chosen = encoders[idx].name;
            Log(L"  优先顺序: ");
            for (size_t k = 0; k < order.size(); ++k)
                Log(L"%s%s", k ? L" > " : L"", encoders[order[k]].name.c_str());
            Log(L"\n");
            break;
        }
        Log(L"    [!] 激活 %s 失败 hr=0x%08X，试下一个\n", encoders[idx].name.c_str(), ahr);
    }
    if (!xf) { Log(L"  [X] 无法激活任何编码器\n"); MFShutdown(); if (g_log) fclose(g_log); return 1; }
    Log(L"  已激活: %s\n", chosen.c_str());

    // 【异步 MFT 必须解锁】否则 SetInputType 直接返回 MF_E_TRANSFORM_ASYNC_LOCKED
    {
        ComPtr<IMFAttributes> attrs;
        if (SUCCEEDED(xf->GetAttributes(&attrs))) {
            UINT32 isAsync = 0;
            attrs->GetUINT32(MF_TRANSFORM_ASYNC, &isAsync);
            UINT32 d3dAware = 0;
            attrs->GetUINT32(MF_SA_D3D11_AWARE, &d3dAware);
            Log(L"  异步 MFT: %d   D3D11 感知: %d\n", (int)isAsync, (int)d3dAware);
            if (isAsync) {
                HRESULT uhr = attrs->SetUINT32(MF_TRANSFORM_ASYNC_UNLOCK, TRUE);
                Log(L"  设置 MF_TRANSFORM_ASYNC_UNLOCK hr=0x%08X\n", uhr);
            }
        }
    }

    // ---- 3. 协商类型 ----
    Log(L"\n--- 3. 协商媒体类型 ---\n");
    ComPtr<IMFMediaType> outType, inType;

    MFCreateMediaType(&outType);
    outType->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    outType->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_H264);
    outType->SetUINT32(MF_MT_AVG_BITRATE, bitrate);
    outType->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
    // NVENC 的 MFT 要求显式给 profile，否则可能协商失败
    outType->SetUINT32(MF_MT_MPEG2_PROFILE, eAVEncH264VProfile_High);
    MFSetAttributeSize(outType.Get(), MF_MT_FRAME_SIZE, W, H);
    MFSetAttributeRatio(outType.Get(), MF_MT_FRAME_RATE, FPS, 1);
    MFSetAttributeRatio(outType.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);

    hr = xf->SetOutputType(0, outType.Get(), 0);
    Log(L"  SetOutputType(H.264 %ux%u@%u %.1fMbps) hr=0x%08X\n", W, H, FPS, bitrate / 1e6, hr);
    if (FAILED(hr)) {
        // 换用编码器自己给出的可用类型
        ComPtr<IMFMediaType> avail;
        if (SUCCEEDED(xf->GetOutputAvailableType(0, 0, &avail))) {
            hr = xf->SetOutputType(0, avail.Get(), 0);
            Log(L"  改用编码器提供的输出类型 hr=0x%08X\n", hr);
        }
    }
    if (FAILED(hr)) { Log(L"  [X] 输出类型协商失败\n"); MFShutdown(); if (g_log) fclose(g_log); return 1; }

    MFCreateMediaType(&inType);
    inType->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    inType->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
    inType->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
    // 紧密排列：stride = 宽度，缓冲里 Y 之后紧跟 UV
    inType->SetUINT32(MF_MT_DEFAULT_STRIDE, W);
    MFSetAttributeSize(inType.Get(), MF_MT_FRAME_SIZE, W, H);
    MFSetAttributeRatio(inType.Get(), MF_MT_FRAME_RATE, FPS, 1);
    MFSetAttributeRatio(inType.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);

    hr = xf->SetInputType(0, inType.Get(), 0);
    Log(L"  SetInputType(NV12 %ux%u stride=%u) hr=0x%08X\n", W, H, W, hr);
    if (FAILED(hr)) {
        ComPtr<IMFMediaType> avail;
        if (SUCCEEDED(xf->GetInputAvailableType(0, 0, &avail))) {
            hr = xf->SetInputType(0, avail.Get(), 0);
            Log(L"  改用编码器提供的输入类型 hr=0x%08X\n", hr);
        }
    }
    if (FAILED(hr)) { Log(L"  [X] 输入类型协商失败\n"); MFShutdown(); if (g_log) fclose(g_log); return 1; }

    // 打印最终生效的类型
    {
        ComPtr<IMFMediaType> actual;
        if (SUCCEEDED(xf->GetOutputCurrentType(0, &actual))) {
            UINT32 aw = 0, ah = 0, aRateNum = 0, aRateDen = 0, aBr = 0;
            MFGetAttributeSize(actual.Get(), MF_MT_FRAME_SIZE, &aw, &ah);
            // 【必须两个输出参数都给地址】传 nullptr 会写坏空指针 → 0xC0000005
            MFGetAttributeRatio(actual.Get(), MF_MT_FRAME_RATE, &aRateNum, &aRateDen);
            if (FAILED(actual->GetUINT32(MF_MT_AVG_BITRATE, &aBr))) aBr = 0;
            GUID sub{}; actual->GetGUID(MF_MT_SUBTYPE, &sub);
            Log(L"  实际输出: %ux%u @%u/%u fps  %.2f Mbps\n",
                aw, ah, aRateNum, aRateDen, aBr / 1e6);
        }
    }

    // ---- 4. 编码 ----
    Log(L"\n--- 4. 编码合成帧 ---\n");
    FILE* out = nullptr;
    if (_wfopen_s(&out, outPath.c_str(), L"wb") != 0) {
        Log(L"  [X] 无法打开输出文件 %s\n", outPath.c_str());
        MFShutdown(); if (g_log) fclose(g_log); return 1;
    }

    const int totalFrames = FPS * seconds;
    EncodeStats st = RunEncode(xf.Get(), W, H, FPS, totalFrames, out);
    fflush(out);
    fclose(out);

    Log(L"\n=== 结果 ===\n");
    Log(L"  编码器      : %s\n", chosen.c_str());
    Log(L"  喂入 / 输出 : %d / %d\n", st.framesSent, st.samplesOut);
    Log(L"  码流字节    : %zu\n", st.totalBytes);
    if (!st.error.empty()) Log(L"  错误        : %s\n", st.error.c_str());

    // 文件大小作为独立佐证
    {
        FILE* f = nullptr;
        if (_wfopen_s(&f, outPath.c_str(), L"rb") == 0 && f) {
            _fseeki64(f, 0, SEEK_END);
            long long sz = _ftelli64(f);
            fclose(f);
            Log(L"  文件大小    : %lld 字节 (%s)\n", sz, outPath.c_str());
        }
    }

    if (st.ok) {
        Log(L"\n  [OK] 硬件 H.264 编码可用，输出是可直接走自建 UDP 的裸码流。\n");
        Log(L"       下一步：把 DXGI 采集的 BGRA 用 ID3D11VideoProcessor 转 NV12 接进来。\n");
    } else {
        Log(L"\n  [X] 编码未通过 —— 按上面的错误定位。\n");
    }

    xf->ProcessMessage(MFT_MESSAGE_NOTIFY_END_STREAMING, 0);
    xf.Reset();
    MFShutdown();
    if (g_log) fclose(g_log);
    return st.ok ? 0 : 1;
}
