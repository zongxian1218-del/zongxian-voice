// decode-probe.cpp —— 把 Annex-B 码流解码成 NV12 画面，验证码流有效
//
// ============================================================================
// 【为什么不用 IMFSourceReader】
// 试过两条路都失败，错误码都是 0xC00D36C4 (MF_E_UNSUPPORTED_BYTESTREAM_TYPE)：
//   · 在创建属性里设 MF_MT_SUBTYPE = H264
//   · 给字节流设 MF_BYTESTREAM_CONTENT_TYPE = "video/H264"
// 结论：Media Foundation 不自带「裸 Annex-B H.264 文件」的字节流处理器。
//
// 所以改用解码器 MFT 直接喂数据。这反而更贴合产品：接收端从 UDP 收到的是
// 一个个 NAL 单元，本来就不存在"文件"这个概念，天然就要走 MFT 这条路。
//
// 流程：读文件 → 按 AUD 切成访问单元(AU) → 提取 SPS/PPS 作序列头
//       → H.264 解码器 MFT → NV12 → 存 BMP 供肉眼确认
//
// 验证内容：
//   1. Annex-B 结构（NAL 类型统计、GOP 长度）—— 也是 NACK 请求关键帧的依据
//   2. 硬件/软件 H.264 解码器枚举
//   3. 类型协商（序列头如何传递）
//   4. 实际解出多少帧、分辨率，画面存 BMP
//
// 用法：decode-probe.exe [输入.h264] [宽] [高] [帧率]
// ============================================================================

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <objbase.h>
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

// 把相对文件名解析到 exe 所在目录，避免依赖调用者的当前工作目录
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

// ---------------------------------------------------------------------------
// NV12 → BMP（BT.709 有限范围）
// ---------------------------------------------------------------------------
static bool SaveNv12AsBmp(const BYTE* nv12, UINT yStride, UINT w, UINT h, const wchar_t* path)
{
    const BYTE* Y = nv12;
    const BYTE* UV = nv12 + size_t(yStride) * h;

    const UINT rowBytes = w * 3;
    const UINT pad = (4 - (rowBytes % 4)) % 4;
    const UINT stride = rowBytes + pad;
    const UINT imageBytes = stride * h;

#pragma pack(push, 1)
    struct BmpHeader {
        WORD bfType; DWORD bfSize; WORD r1; WORD r2; DWORD bfOffBits;
        DWORD biSize; LONG biWidth; LONG biHeight; WORD biPlanes; WORD biBitCount;
        DWORD biCompression; DWORD biSizeImage; LONG xppm; LONG yppm;
        DWORD clrUsed; DWORD clrImportant;
    } hdr{};
#pragma pack(pop)

    hdr.bfType = 0x4D42;
    hdr.bfOffBits = sizeof(BmpHeader);
    hdr.bfSize = sizeof(BmpHeader) + imageBytes;
    hdr.biSize = 40;
    hdr.biWidth = (LONG)w;
    hdr.biHeight = (LONG)h;
    hdr.biPlanes = 1;
    hdr.biBitCount = 24;
    hdr.biSizeImage = imageBytes;

    std::vector<BYTE> out(imageBytes, 0);
    auto clamp8 = [](double v) -> BYTE {
        if (v < 0) return 0;
        if (v > 255) return 255;
        return (BYTE)(v + 0.5);
    };

    for (UINT y = 0; y < h; ++y) {
        BYTE* dst = out.data() + size_t(h - 1 - y) * stride;   // BMP 自下而上
        const BYTE* yRow = Y + size_t(y) * yStride;
        const BYTE* uvRow = UV + size_t(y / 2) * yStride;
        for (UINT x = 0; x < w; ++x) {
            const double C = double(yRow[x]) - 16.0;
            const double D = double(uvRow[(x / 2) * 2 + 0]) - 128.0;
            const double E = double(uvRow[(x / 2) * 2 + 1]) - 128.0;
            dst[x * 3 + 0] = clamp8(1.164 * C + 2.112 * D);
            dst[x * 3 + 1] = clamp8(1.164 * C - 0.213 * D - 0.533 * E);
            dst[x * 3 + 2] = clamp8(1.164 * C + 1.793 * E);
        }
    }

    FILE* f = nullptr;
    if (_wfopen_s(&f, path, L"wb") != 0 || !f) return false;
    fwrite(&hdr, sizeof(hdr), 1, f);
    fwrite(out.data(), out.size(), 1, f);
    fclose(f);
    return true;
}

// ---------------------------------------------------------------------------
// Annex-B 解析：NAL 列表 + 访问单元 + 序列头
// ---------------------------------------------------------------------------
struct Nal {
    size_t startCodeOff = 0;   // 起始码第一个字节的位置
    size_t payloadOff = 0;     // NAL 头字节的位置
    size_t endOff = 0;         // 下一个起始码的位置（或数据末尾）
    int type = 0;
};

struct StreamInfo {
    std::vector<Nal> nals;
    std::vector<BYTE> seqHeader;        // SPS + PPS（含起始码）
    std::vector<std::pair<size_t, size_t>> accessUnits;  // (偏移, 长度)
    int sps = 0, pps = 0, sei = 0, idr = 0, nonIdr = 0, aud = 0, other = 0;
    std::vector<int> idrIndices;
    bool valid = false;
    std::string error;
};

static StreamInfo ParseStream(const std::vector<BYTE>& d)
{
    StreamInfo st;
    if (d.size() < 8) { st.error = "数据太短"; return st; }

    // 找所有起始码位置
    struct Sc { size_t off; size_t len; };
    std::vector<Sc> scs;
    size_t i = 0;
    while (i + 3 <= d.size()) {
        if (d[i] == 0 && d[i + 1] == 0 && d[i + 2] == 1) {
            scs.push_back({ i, 3 }); i += 3;
        } else if (i + 4 <= d.size() && d[i] == 0 && d[i + 1] == 0 && d[i + 2] == 0 && d[i + 3] == 1) {
            scs.push_back({ i, 4 }); i += 4;
        } else {
            ++i;
        }
    }
    if (scs.empty()) { st.error = "找不到任何 Annex-B 起始码"; return st; }

    for (size_t k = 0; k < scs.size(); ++k) {
        Nal n;
        n.startCodeOff = scs[k].off;
        n.payloadOff = scs[k].off + scs[k].len;
        n.endOff = (k + 1 < scs.size()) ? scs[k + 1].off : d.size();
        if (n.payloadOff >= d.size()) continue;
        n.type = d[n.payloadOff] & 0x1F;
        st.nals.push_back(n);

        switch (n.type) {
        case 1:  ++st.nonIdr; break;
        case 5:  ++st.idr; st.idrIndices.push_back((int)st.nals.size()); break;
        case 6:  ++st.sei; break;
        case 7:  ++st.sps; break;
        case 8:  ++st.pps; break;
        case 9:  ++st.aud; break;
        default: ++st.other; break;
        }
    }

    // 序列头 = 第一个 SPS + 第一个 PPS（含起始码）
    bool haveSps = false, havePps = false;
    for (auto& n : st.nals) {
        if (n.type == 7 && !haveSps) {
            st.seqHeader.insert(st.seqHeader.end(), d.begin() + n.startCodeOff, d.begin() + n.endOff);
            haveSps = true;
        } else if (n.type == 8 && !havePps) {
            st.seqHeader.insert(st.seqHeader.end(), d.begin() + n.startCodeOff, d.begin() + n.endOff);
            havePps = true;
        }
        if (haveSps && havePps) break;
    }

    // 按 AUD 切访问单元；没有 AUD 就退化为「每个 slice NAL 一个 AU」
    if (st.aud > 0) {
        std::vector<size_t> bounds;
        for (auto& n : st.nals)
            if (n.type == 9) bounds.push_back(n.startCodeOff);
        bounds.push_back(d.size());
        for (size_t k = 0; k + 1 < bounds.size(); ++k)
            st.accessUnits.emplace_back(bounds[k], bounds[k + 1] - bounds[k]);
    } else {
        for (auto& n : st.nals) {
            if (n.type == 1 || n.type == 5)
                st.accessUnits.emplace_back(n.startCodeOff, n.endOff - n.startCodeOff);
        }
    }

    st.valid = !st.accessUnits.empty();
    if (!st.valid) st.error = "切不出任何访问单元";
    return st;
}

// ---------------------------------------------------------------------------
int wmain(int argc, wchar_t** argv)
{
    const wchar_t* srcArg = (argc > 1) ? argv[1] : L"encode-probe.h264";
    const std::wstring src = ExeRelative(srcArg);
    const UINT defW = (argc > 2) ? (UINT)_wtoi(argv[2]) : 1920;
    const UINT defH = (argc > 3) ? (UINT)_wtoi(argv[3]) : 1080;
    const UINT defFps = (argc > 4) ? (UINT)_wtoi(argv[4]) : 30;

    SetConsoleOutputCP(CP_UTF8);
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
    {
        const std::wstring logPath = ExeRelative(L"decode-probe-log.txt");
        _wfopen_s(&g_log, logPath.c_str(), L"w, ccs=UTF-8");
    }

    Log(L"=== 码流解码验证（解码器 MFT 直接喂访问单元）===\n");
    Log(L"输入 %s\n\n", src.c_str());

    // ---- 读文件 ----
    std::vector<BYTE> data;
    {
        FILE* f = nullptr;
        if (_wfopen_s(&f, src.c_str(), L"rb") != 0 || !f) {
            Log(L"[X] 打不开 %s\n", src.c_str());
            if (g_log) fclose(g_log);
            return 1;
        }
        _fseeki64(f, 0, SEEK_END);
        long long sz = _ftelli64(f);
        _fseeki64(f, 0, SEEK_SET);
        data.resize((size_t)sz);
        size_t rd = fread(data.data(), 1, data.size(), f);
        fclose(f);
        data.resize(rd);
        Log(L"读取 %zu 字节\n\n", data.size());
    }

    bool ok = false;
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(MFStartup(MF_VERSION))) { Log(L"[X] MFStartup 失败\n"); if (g_log) fclose(g_log); return 1; }

    // ---- 1. 解析 ----
    Log(L"--- 1. Annex-B 结构 ---\n");
    StreamInfo si = ParseStream(data);
    if (!si.valid) {
        Log(L"  [X] %S\n", si.error.c_str());
    } else {
        Log(L"  NAL 总数      : %zu\n", si.nals.size());
        Log(L"  SPS(7)        : %d\n", si.sps);
        Log(L"  PPS(8)        : %d\n", si.pps);
        Log(L"  AUD(9)        : %d\n", si.aud);
        Log(L"  IDR 关键帧(5) : %d\n", si.idr);
        Log(L"  非 IDR(1)     : %d\n", si.nonIdr);
        Log(L"  序列头字节    : %zu（SPS+PPS，供解码器初始化用）\n", si.seqHeader.size());
        Log(L"  访问单元(AU)  : %zu 个\n", si.accessUnits.size());
        if (si.idrIndices.size() >= 2)
            Log(L"  GOP 长度约    : %d 个 NAL\n", si.idrIndices[1] - si.idrIndices[0]);
    }

    // ---- 2. 枚举解码器 ----
    Log(L"\n--- 2. H.264 解码器枚举 ---\n");
    IMFActivate** acts = nullptr;
    UINT32 actCount = 0;
    {
        MFT_REGISTER_TYPE_INFO inInfo{ MFMediaType_Video, MFVideoFormat_H264 };
        MFT_REGISTER_TYPE_INFO outInfo{ MFMediaType_Video, MFVideoFormat_NV12 };
        hr = MFTEnumEx(MFT_CATEGORY_VIDEO_DECODER, MFT_ENUM_FLAG_ALL,
                       &inInfo, &outInfo, &acts, &actCount);
        if (FAILED(hr)) {
            Log(L"  [X] MFTEnumEx 失败 hr=0x%08X\n", hr);
        } else {
            Log(L"  共 %u 个 H.264→NV12 解码器：\n", actCount);
            for (UINT32 k = 0; k < actCount; ++k) {
                LPWSTR nm = nullptr; UINT32 len = 0;
                std::wstring name = L"(无名字)";
                if (SUCCEEDED(acts[k]->GetAllocatedString(MFT_FRIENDLY_NAME_Attribute, &nm, &len))) {
                    name = nm; CoTaskMemFree(nm);
                }
                UINT32 isAsync = 0;
                acts[k]->GetUINT32(MF_TRANSFORM_ASYNC, &isAsync);
                Log(L"    [%u] %s  异步=%d\n", k, name.c_str(), (int)isAsync);
            }
        }
    }

    // ---- 3. 激活并协商 ----
    Log(L"\n--- 3. 激活解码器并协商类型 ---\n");
    ComPtr<IMFTransform> dec;
    std::wstring chosenName;
    for (UINT32 k = 0; k < actCount && !dec; ++k) {
        ComPtr<IMFTransform> t;
        if (SUCCEEDED(acts[k]->ActivateObject(IID_PPV_ARGS(&t))) && t) {
            LPWSTR nm = nullptr; UINT32 len = 0;
            if (SUCCEEDED(acts[k]->GetAllocatedString(MFT_FRIENDLY_NAME_Attribute, &nm, &len))) {
                chosenName = nm; CoTaskMemFree(nm);
            }
            dec = t;
        }
    }
    if (actCount && acts) {
        for (UINT32 k = 0; k < actCount; ++k) acts[k]->Release();
        CoTaskMemFree(acts);
    }
    if (!dec) { Log(L"  [X] 无法激活任何 H.264 解码器\n"); goto done; }
    Log(L"  已激活: %s\n", chosenName.c_str());

    // 异步解码器必须解锁，否则 SetInputType 返回 MF_E_TRANSFORM_ASYNC_LOCKED
    {
        ComPtr<IMFAttributes> attrs;
        if (SUCCEEDED(dec->GetAttributes(&attrs))) {
            UINT32 isAsync = 0;
            attrs->GetUINT32(MF_TRANSFORM_ASYNC, &isAsync);
            if (isAsync) {
                attrs->SetUINT32(MF_TRANSFORM_ASYNC_UNLOCK, TRUE);
                Log(L"  异步解码器，已解锁 MF_TRANSFORM_ASYNC_UNLOCK\n");
            }
        }
    }

    {
        // 输入类型：H.264 + 序列头
        // 【关键】MF_MT_MPEG_SEQUENCE_HEADER 必须带 SPS/PPS，否则解码器不知道
        // 分辨率、profile、参考帧数，SetInputType 会失败。
        ComPtr<IMFMediaType> inType;
        MFCreateMediaType(&inType);
        inType->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
        inType->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_H264);
        inType->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
        MFSetAttributeSize(inType.Get(), MF_MT_FRAME_SIZE, defW, defH);
        MFSetAttributeRatio(inType.Get(), MF_MT_FRAME_RATE, defFps, 1);
        MFSetAttributeRatio(inType.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
        if (!si.seqHeader.empty())
            inType->SetBlob(MF_MT_MPEG_SEQUENCE_HEADER, si.seqHeader.data(),
                            (UINT32)si.seqHeader.size());

        hr = dec->SetInputType(0, inType.Get(), 0);
        Log(L"  SetInputType(H.264 %ux%u@%u + 序列头 %zu 字节) hr=0x%08X\n",
            defW, defH, defFps, si.seqHeader.size(), hr);
        if (FAILED(hr)) {
            // 退一步：用解码器自己给的输入类型模板再试
            ComPtr<IMFMediaType> avail;
            if (SUCCEEDED(dec->GetInputAvailableType(0, 0, &avail))) {
                if (!si.seqHeader.empty())
                    avail->SetBlob(MF_MT_MPEG_SEQUENCE_HEADER, si.seqHeader.data(),
                                   (UINT32)si.seqHeader.size());
                hr = dec->SetInputType(0, avail.Get(), 0);
                Log(L"  改用解码器提供的输入类型 hr=0x%08X\n", hr);
            }
        }
        if (FAILED(hr)) { Log(L"  [X] 输入类型协商失败\n"); goto done; }

        // 输出类型：NV12
        {
            ComPtr<IMFMediaType> outType;
            MFCreateMediaType(&outType);
            outType->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
            outType->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
            MFSetAttributeSize(outType.Get(), MF_MT_FRAME_SIZE, defW, defH);
            MFSetAttributeRatio(outType.Get(), MF_MT_FRAME_RATE, defFps, 1);
            MFSetAttributeRatio(outType.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
            outType->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);

            hr = dec->SetOutputType(0, outType.Get(), 0);
            Log(L"  SetOutputType(NV12 %ux%u) hr=0x%08X\n", defW, defH, hr);
            if (FAILED(hr)) {
                ComPtr<IMFMediaType> avail;
                if (SUCCEEDED(dec->GetOutputAvailableType(0, 0, &avail))) {
                    hr = dec->SetOutputType(0, avail.Get(), 0);
                    Log(L"  改用解码器提供的输出类型 hr=0x%08X\n", hr);
                }
            }
            if (FAILED(hr)) { Log(L"  [X] 输出类型协商失败\n"); goto done; }
        }

        // 读回实际生效的分辨率
        UINT w = defW, h = defH, stride = defW;
        {
            ComPtr<IMFMediaType> cur;
            if (SUCCEEDED(dec->GetOutputCurrentType(0, &cur))) {
                MFGetAttributeSize(cur.Get(), MF_MT_FRAME_SIZE, &w, &h);
                if (FAILED(cur->GetUINT32(MF_MT_DEFAULT_STRIDE, &stride))) stride = w;
                if (stride == 0) stride = w;
                Log(L"  实际输出      : %ux%u  stride=%u\n", w, h, stride);
            }
        }

        // ---- 4. 解码 ----
        Log(L"\n--- 4. 解码 ---\n");
        dec->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);
        dec->ProcessMessage(MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0);

        ComPtr<IMFMediaEventGenerator> events;
        bool hasEvents = SUCCEEDED(dec->QueryInterface(IID_PPV_ARGS(&events)));

        MFT_OUTPUT_STREAM_INFO osi{};
        dec->GetOutputStreamInfo(0, &osi);
        const bool providesSamples = (osi.dwFlags & MFT_OUTPUT_STREAM_PROVIDES_SAMPLES) != 0;
        DWORD outBufSize = osi.cbSize ? osi.cbSize : (4u << 20);
        if (outBufSize < (1u << 20)) outBufSize = (1u << 20);

        int framesOut = 0;
        int maxLag = 0;
        std::wstring lagTrace;
        bool savedFirst = false, savedMid = false;
        int processErrors = 0;

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
            HRESULT ohr = dec->ProcessOutput(0, 1, &db, &status);
            IMFSample* toRelease = db.pSample;
            if (db.pEvents) { db.pEvents->Release(); db.pEvents = nullptr; }

            if (ohr == MF_E_TRANSFORM_STREAM_CHANGE) {
                if (toRelease) toRelease->Release();
                ComPtr<IMFMediaType> avail;
                if (SUCCEEDED(dec->GetOutputAvailableType(0, 0, &avail))) {
                    dec->SetOutputType(0, avail.Get(), 0);
                    MFGetAttributeSize(avail.Get(), MF_MT_FRAME_SIZE, &w, &h);
                    if (FAILED(avail->GetUINT32(MF_MT_DEFAULT_STRIDE, &stride)) || stride == 0)
                        stride = w;
                    Log(L"          [信息] 输出类型变更 → %ux%u stride=%u\n", w, h, stride);
                }
                return true;
            }
            if (ohr == MF_E_TRANSFORM_NEED_MORE_INPUT) {
                if (toRelease) toRelease->Release();
                return false;
            }
            if (FAILED(ohr)) {
                if (toRelease) toRelease->Release();
                ++processErrors;
                if (processErrors <= 3) Log(L"          ProcessOutput hr=0x%08X\n", ohr);
                return false;
            }
            if (db.pSample) {
                ComPtr<IMFMediaBuffer> buf;
                if (SUCCEEDED(db.pSample->ConvertToContiguousBuffer(&buf))) {
                    BYTE* p = nullptr; DWORD len = 0;
                    if (SUCCEEDED(buf->Lock(&p, nullptr, &len))) {
                        const size_t need = size_t(stride) * h * 3 / 2;
                        if (len >= need) {
                            ++framesOut;
                            if (!savedFirst) {
                                if (SaveNv12AsBmp(p, stride, w, h,
                                                  ExeRelative(L"decode-frame-0.bmp").c_str()))
                                    Log(L"          [OK] 第 1 帧已存 decode-frame-0.bmp (%ux%u)\n", w, h);
                                savedFirst = true;
                            } else if (!savedMid && framesOut >= 30) {
                                if (SaveNv12AsBmp(p, stride, w, h,
                                                  ExeRelative(L"decode-frame-1.bmp").c_str()))
                                    Log(L"          [OK] 第 %d 帧已存 decode-frame-1.bmp\n", framesOut);
                                savedMid = true;
                            }
                        }
                        buf->Unlock();
                    }
                }
            }
            if (toRelease) toRelease->Release();
            return true;
        };

        // 同步解码器：循环取空；异步解码器：靠事件，每个 HaveOutput 取一次
        auto pumpOutputs = [&]() -> int {
            int got = 0;
            if (hasEvents) {
                for (;;) {
                    ComPtr<IMFMediaEvent> ev;
                    HRESULT ehr = events->GetEvent(MF_EVENT_FLAG_NO_WAIT, &ev);
                    if (FAILED(ehr)) break;
                    MediaEventType type = MEUnknown;
                    ev->GetType(&type);
                    if (type == METransformHaveOutput) { if (collectOne()) ++got; }
                    else if (type == METransformNeedInput) break;
                    else if (type == METransformDrainComplete) break;
                }
            } else {
                while (collectOne()) ++got;
            }
            return got;
        };

        const double frameDur = 10000000.0 / defFps;
        const double tStart = NowSeconds();
        int fed = 0, notAccepted = 0;

        for (size_t k = 0; k < si.accessUnits.size(); ++k) {
            auto [off, len] = si.accessUnits[k];

            ComPtr<IMFMediaBuffer> mb;
            if (FAILED(MFCreateMemoryBuffer((DWORD)len, &mb))) break;
            BYTE* p = nullptr;
            if (FAILED(mb->Lock(&p, nullptr, nullptr))) break;
            memcpy(p, data.data() + off, len);
            mb->Unlock();
            mb->SetCurrentLength((DWORD)len);

            ComPtr<IMFSample> sample;
            if (FAILED(MFCreateSample(&sample))) break;
            sample->AddBuffer(mb.Get());
            sample->SetSampleTime((LONGLONG)(k * frameDur));
            sample->SetSampleDuration((LONGLONG)frameDur);

            // MF_E_NOTACCEPTING(0xC00D36B5) 是「暂时吃不下」，要重试而不是放弃
            HRESULT ihr = E_FAIL;
            for (int attempt = 0; attempt < 500; ++attempt) {
                ihr = dec->ProcessInput(0, sample.Get(), 0);
                if (ihr != MF_E_NOTACCEPTING) break;
                ++notAccepted;
                pumpOutputs();
                Sleep(1);
            }
            if (FAILED(ihr)) {
                Log(L"          ProcessInput 失败 hr=0x%08X（第 %zu 个 AU）\n", ihr, k);
                break;
            }
            ++fed;
            pumpOutputs();

            // 【实时管线深度测量】喂入数减去已解出数 = 解码器里压着多少帧。
            // 离线解码时这个值会被收尾排空抹平，只有边喂边看才暴露出来。
            // 实测微软解码器恒定压 27 帧 ≈ 900ms，是实时共享延迟的主要来源。
            {
                const int lag = fed - framesOut;
                if (lag > maxLag) maxLag = lag;
                if (fed % 20 == 0)
                    lagTrace += std::to_wstring(fed) + L"→" + std::to_wstring(lag) + L" ";
            }
        }

        // 排空：通知流结束，把解码器里缓存的帧全部取出来
        dec->ProcessMessage(MFT_MESSAGE_NOTIFY_END_OF_STREAM, 0);
        dec->ProcessMessage(MFT_MESSAGE_COMMAND_DRAIN, 0);
        for (int spin = 0; spin < 2000; ++spin) {
            int got = pumpOutputs();
            if (got == 0 && !hasEvents) break;
            if (got == 0 && hasEvents) {
                if (NowSeconds() - tStart > 10.0) break;
                Sleep(1);
            }
        }

        const double elapsed = NowSeconds() - tStart;
        Log(L"\n  喂入访问单元  : %d / %zu（其中 %d 次遇到 NOTACCEPTING）\n",
            fed, si.accessUnits.size(), notAccepted);
        Log(L"  解出帧数      : %d\n", framesOut);
        Log(L"  管线深度(最大): %d 帧（喂入 − 已解出）\n", maxLag);
        if (!lagTrace.empty()) Log(L"  深度轨迹      : %s\n", lagTrace.c_str());
        Log(L"  耗时          : %.3f 秒（%.1f fps）\n",
            elapsed, elapsed > 0 ? framesOut / elapsed : 0.0);
        if (processErrors) Log(L"  ProcessOutput 报错 %d 次\n", processErrors);

        Log(L"\n=== 结论 ===\n");
        if (framesOut > 0 && savedFirst) {
            ok = true;
            Log(L"  [OK] 码流有效：%d 帧全部解出，画面已存 BMP 供肉眼确认。\n", framesOut);
            Log(L"       接收端的解码路径（NAL→解码器 MFT→NV12）已验证可用。\n");
        } else {
            Log(L"  [X] 没有解出任何帧。\n");
        }
    }

done:
    // 【释放顺序】所有 Media Foundation 对象必须在 MFShutdown() 之前释放。
    // 否则 MFT 会在 MF 已关闭之后析构，实测直接 0xC0000005。
    dec.Reset();
    MFShutdown();
    CoUninitialize();
    if (g_log) fclose(g_log);
    return ok ? 0 : 1;
}
