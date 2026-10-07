// receiver-probe.cpp —— 接收端：UDP 收包 → 重组 → 解码 → 存证画面
//
// ============================================================================
// 这是屏幕共享接收端的完整雏形。链路：
//   UDP 收包 → 分片重组 → 缺口检测 → 请求关键帧 → H.264 解码 → NV12 → BMP
//
// 【为什么单线程收包就够】
// 之前传输层自测里必须用独立收包线程，是因为那个测试是「自己发完再自己收」，
// 不并发就会让接收缓冲溢出。这里是两个进程：接收端一直阻塞在 recvfrom 上
// 持续收包，发送端在另一个进程里发 —— 本身就是真实的收发模型。
//
// 【丢帧恢复】
// 传输层如实报告缺口（gapDetected / droppedIncomplete），本程序据此：
//   1. Flush 解码器（清掉内部参考帧状态）
//   2. 请求发送端出 IDR（限速 200ms 一次，避免请求风暴）
//   3. 跳过所有非 IDR 帧，直到拿到一个完整 IDR 再恢复喂数据
//
// 用法：receiver-probe.exe [秒数] [端口] [宽] [高] [帧率]
// ============================================================================

#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <objbase.h>

#include "../transport/media-packet.h"
#include "../transport/udp-socket.h"
// 【解码器选型】用 libavcodec 而不是 Media Foundation 的解码器 MFT：
// 后者实测有 27 帧固定管线（约 900ms 延迟）且无法通过任何设置降低。
// 因此本程序完全不依赖 Media Foundation。
#include "../decode/ffmpeg-decoder.h"
#include "../render/d3d11-renderer.h"

#include <cstdio>
#include <map>
#include <string>
#include <vector>

using namespace zx;

// ---------------------------------------------------------------------------
// 日志
// ---------------------------------------------------------------------------
static FILE* g_log = nullptr;

static void Log(const wchar_t* fmt, ...)
{
    // 【stdout 必须自己转 UTF-8，不能用 vwprintf】
    // 日志文件是用 "w, ccs=UTF-8" 打开的（所以文件里的中文一直是对的），但 **stdout 不是
    // Unicode 流**。vwprintf 于是按默认 "C" 区域把宽字符转字节：一遇到中文就转换失败，
    // 结果是**整行被截断、连那个换行都不写** —— 下一条 ASCII 事件就粘到了这一行末尾。
    // 应用侧因此看到的是 "  [ 1.0 event stats recv=..."，用 StartsWith 匹配永远不中
    // （状态栏一直停在"等待推流…"就是这么来的）。
    // 显式转 UTF-8 之后两边编码一致（C# 侧 StandardOutputEncoding = UTF8）。
    {
        wchar_t buf[4096];
        va_list ap; va_start(ap, fmt);
        _vsnwprintf_s(buf, _countof(buf), _TRUNCATE, fmt, ap);
        va_end(ap);

        char u8[8192];
        const int m = WideCharToMultiByte(CP_UTF8, 0, buf, -1, u8, sizeof(u8) - 1, nullptr, nullptr);
        if (m > 1) {                       // m 含结尾的 NUL
            fwrite(u8, 1, (size_t)(m - 1), stdout);
            fflush(stdout);
        }
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

static double NowMs()
{
    static LARGE_INTEGER freq = [] { LARGE_INTEGER f; QueryPerformanceFrequency(&f); return f; }();
    LARGE_INTEGER c; QueryPerformanceCounter(&c);
    return double(c.QuadPart) * 1000.0 / double(freq.QuadPart);
}

// ---------------------------------------------------------------------------
// Annex-B：从访问单元里抽出 SPS + PPS 作为解码器序列头
// 【必须】没有序列头，解码器不知道分辨率/profile/参考帧数，SetInputType 会失败
// ---------------------------------------------------------------------------
static bool ExtractSeqHeader(const uint8_t* au, size_t len, std::vector<uint8_t>& out)
{
    struct Sc { size_t off; size_t payload; };
    std::vector<Sc> scs;
    size_t i = 0;
    while (i + 3 <= len) {
        if (au[i] == 0 && au[i + 1] == 0 && au[i + 2] == 1) { scs.push_back({ i, i + 3 }); i += 3; }
        else if (i + 4 <= len && au[i] == 0 && au[i + 1] == 0 && au[i + 2] == 0 && au[i + 3] == 1) {
            scs.push_back({ i, i + 4 }); i += 4;
        } else ++i;
    }
    if (scs.empty()) return false;

    bool gotSps = false, gotPps = false;
    for (size_t k = 0; k < scs.size(); ++k) {
        if (scs[k].payload >= len) continue;
        const int type = au[scs[k].payload] & 0x1F;
        if (type != 7 && type != 8) continue;
        const size_t end = (k + 1 < scs.size()) ? scs[k + 1].off : len;
        out.insert(out.end(), au + scs[k].off, au + end);
        if (type == 7) gotSps = true; else gotPps = true;
    }
    return gotSps;   // 至少要拿到 SPS
}

// ---------------------------------------------------------------------------
// 最小 SPS 解析：只取 max_num_ref_frames
// 【为什么要看这个】解码器的 DPB 大小由 SPS 里的 max_num_ref_frames 决定。
// 它直接关系到解码器要压多少帧才敢输出 —— 这是实时共享延迟的主要来源。
// H.264 里这些字段是 exp-Golomb 编码，需要自己去零比特数。
// ---------------------------------------------------------------------------
struct BitReader {
    const uint8_t* p;
    size_t bits;
    size_t pos = 0;
    BitReader(const uint8_t* d, size_t n) : p(d), bits(n * 8) {}
    uint32_t ReadBit()
    {
        if (pos >= bits) return 0;
        const uint32_t v = (p[pos >> 3] >> (7 - (pos & 7))) & 1;
        ++pos;
        return v;
    }
    uint32_t ReadBits(int n)
    {
        uint32_t v = 0;
        for (int i = 0; i < n; ++i) v = (v << 1) | ReadBit();
        return v;
    }
    // 无符号 exp-Golomb：前导零的个数决定后面读多少位
    uint32_t ReadUE()
    {
        int zeros = 0;
        while (zeros < 32 && ReadBit() == 0) ++zeros;
        if (zeros == 0) return 0;
        return (1u << zeros) - 1 + ReadBits(zeros);
    }
    int32_t ReadSE()
    {
        const uint32_t k = ReadUE();
        return (k & 1) ? (int32_t)((k + 1) / 2) : -(int32_t)(k / 2);
    }
};

// 从 Annex-B 数据里找 SPS，解析出 max_num_ref_frames
static int ParseNumRefFrames(const uint8_t* sps, size_t len)
{
    if (len < 4) return -1;
    BitReader br(sps, len);
    const uint32_t profile = br.ReadBits(8);
    br.ReadBits(8);                       // constraint flags
    br.ReadBits(8);                       // level_idc
    br.ReadUE();                          // seq_parameter_set_id

    if (profile == 100 || profile == 110 || profile == 122 || profile == 244 ||
        profile == 44 || profile == 83 || profile == 86 || profile == 118 ||
        profile == 128 || profile == 138 || profile == 139 || profile == 134 ||
        profile == 135) {
        const uint32_t chroma = br.ReadUE();
        if (chroma == 3) br.ReadBit();    // separate_colour_plane_flag
        br.ReadUE();                      // bit_depth_luma_minus8
        br.ReadUE();                      // bit_depth_chroma_minus8
        br.ReadBit();                     // qpprime_y_zero_transform_bypass_flag
        if (br.ReadBit()) return -1;      // seq_scaling_matrix_present_flag：不处理缩放矩阵
    }
    br.ReadUE();                          // log2_max_frame_num_minus4
    const uint32_t pocType = br.ReadUE();
    if (pocType == 0) br.ReadUE();        // log2_max_pic_order_cnt_lsb_minus4
    else if (pocType == 1) return -1;     // 这种情况还要读更多，本项目用不到
    return (int)br.ReadUE();              // max_num_ref_frames
}

// ---------------------------------------------------------------------------
// NV12 → BMP（BT.709 有限范围），用于肉眼确认画面正确
// ---------------------------------------------------------------------------
static bool SaveNv12AsBmp(const uint8_t* nv12, UINT yStride, UINT w, UINT h, UINT visibleH,
                          const wchar_t* path)
{
    if (visibleH == 0 || visibleH > h) visibleH = h;
    const uint8_t* Y = nv12;
    const uint8_t* UV = nv12 + size_t(yStride) * h;

    const UINT rowBytes = w * 3;
    const UINT pad = (4 - (rowBytes % 4)) % 4;
    const UINT stride = rowBytes + pad;
    const UINT imageBytes = stride * visibleH;

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
    hdr.biHeight = (LONG)visibleH;
    hdr.biPlanes = 1;
    hdr.biBitCount = 24;
    hdr.biSizeImage = imageBytes;

    std::vector<uint8_t> out(imageBytes, 0);
    auto clamp8 = [](double v) -> uint8_t {
        if (v < 0) return 0;
        if (v > 255) return 255;
        return (uint8_t)(v + 0.5);
    };

    for (UINT y = 0; y < visibleH; ++y) {
        uint8_t* dst = out.data() + size_t(visibleH - 1 - y) * stride;   // BMP 自下而上
        const uint8_t* yRow = Y + size_t(y) * yStride;
        const uint8_t* uvRow = UV + size_t(y / 2) * yStride;
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
// 预览窗口（--window 模式）
// 【为什么先做独立窗口而不是直接嵌进 WinUI】
// 渲染路径是接进应用前唯一还没验证过的一环。先在独立窗口里跑通
// 「解码 → 上传纹理 → Present」，再把它换成 WinUI 的 SwapChainPanel，
// 一次只引入一个新变量。嵌进 XAML 时这一层换成「往共享纹理写」即可。
// ---------------------------------------------------------------------------
static bool g_windowClosed = false;
static HWND g_previewHwnd = nullptr;   // 提前声明：输入转发的辅助函数要用它
// 父应用主窗口句柄：覆盖窗口会被插到它正上方（见 rect 处说明）。0 = 没有锚点。
static HWND g_overlayAnchor = nullptr;

// ---------------------------------------------------------------------------
// 覆盖窗口置顶的重试（2026-10-05 实测出来的补丁）
//
// 实测：frameless 覆盖窗口在创建时那一次 SetWindowPos(HWND_TOPMOST) 不生效
// （exStyle 读回 0x08000080，没有 TOPMOST 位），而非 frameless 的顶层窗口同一条代码就生效。
// 最小对照程序（build/topmost-selftest.cpp）里把窗口样式、调用序列逐项拆开测了 9 个变体，
// **全部都能置顶**，说明不是样式/序列问题，而是这个进程上下文里的某种时序差异。
// 对照程序里"先显示、泵一会儿消息，再置顶"的变体是成功的，所以这里采用同样的做法：
// 在消息泵里重试若干次，并把每次的结果读回写进日志（不许静默失败）。
// ---------------------------------------------------------------------------
static bool g_overlayTopmostWanted = false;
static int  g_topmostTries = 0;
static double g_lastTopmostTryMs = 0;

static void ReassertOverlayTopmost()
{
    if (!g_overlayTopmostWanted || !g_previewHwnd) return;
    if ((GetWindowLongPtrW(g_previewHwnd, GWL_EXSTYLE) & WS_EX_TOPMOST) != 0) return;  // 已经好了
    if (g_topmostTries >= 3) return;   // 本机实测拿不到，试 3 次就收手（别把日志刷满）

    const double now = NowMs();
    if (g_lastTopmostTryMs != 0 && now - g_lastTopmostTryMs < 300.0) return;          // 限速
    g_lastTopmostTryMs = now;
    ++g_topmostTries;

    SetWindowPos(g_previewHwnd, HWND_TOPMOST, 0, 0, 0, 0,
                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW);
    const bool ok = (GetWindowLongPtrW(g_previewHwnd, GWL_EXSTYLE) & WS_EX_TOPMOST) != 0;
    Log(L"  [信息] 覆盖窗口置顶重试 #%d: %s（exStyle=0x%08llX）%s\n", g_topmostTries,
        ok ? L"已生效" : L"仍未生效",
        (unsigned long long)GetWindowLongPtrW(g_previewHwnd, GWL_EXSTYLE),
        (g_topmostTries >= 3 && !ok) ? L" —— 放弃置顶，详见 docs/winui-overlay-verified-2026-10-05.md" : L"");
}

// ---------------------------------------------------------------------------
// 远程控制（查看端）：把覆盖窗口上的鼠标操作发给被控端
//
// 通道复用已有的 reqSock（接收端 → 发送端），和关键帧请求走同一条路。
// 【为什么每个事件都带帧号】被控端据此判断这个操作是否已经"过期"：
// 查看端看到的是若干帧之前的画面，若被控端画面已经往前走了很多帧，
// 那个坐标对应的界面可能早就变了，这种点击必须丢掉（设计文档 §9.5 的"陈旧点击"防护）。
//
// 这些状态不需要加锁：窗口过程由 PumpMessages() 在主循环里调用，和收包是同一个线程。
// ---------------------------------------------------------------------------
static bool g_remoteControl = false;          // --remote-control 才打开（默认关）
static bool g_remoteKeyboard = false;         // --remote-keyboard 才转发键盘（默认关）
static HHOOK g_kbHook = nullptr;              // 低级键盘钩子
static zx::UdpSocket* g_ctrlSock = nullptr;   // 复用 reqSock
static sockaddr_in g_ctrlAddr{};              // 被控端地址（从收到的媒体包学到）
static bool g_ctrlAddrValid = false;
static uint16_t g_sessionId = 0;
static uint32_t g_lastFrameId = 0;            // 最近交付（≈正在显示）的帧号
static double g_lastMoveMs = 0;
static int g_inputSent = 0, g_inputMoves = 0, g_inputThrottled = 0, g_keyEvents = 0;
// 被控端的授权状态：unknown → 一直发（那是"请求控制"的触发方式）；被拒绝/停止后就不再发
static uint8_t g_peerState = 0xFF;
static bool g_peerBlocked = false;
static int g_inputBlocked = 0;
// 【静默丢弃是排查噩梦】每条提前返回的原因只记一次，避免刷屏
static bool g_diagNoSock = false, g_diagNoAddr = false, g_diagNoRect = false, g_diagTinyRect = false;

static const wchar_t* StateText(uint8_t st)
{
    switch (st) {
    case zx::kStatePending: return L"等待被控端本人决定";
    case zx::kStateAllowed: return L"已允许";
    case zx::kStateDenied:  return L"已拒绝（或 30 秒无响应）—— 查看端停止发送输入";
    case zx::kStateStopped: return L"已被本人停止 —— 查看端停止发送输入";
    default:                return L"未知";
    }
}

static void SendInputEvent(uint8_t kind, uint8_t flags, int x, int y, int32_t wheel, uint16_t vk = 0)
{
    if (!g_remoteControl) return;   // 用户没开远程控制：静默（这是预期状态，不算异常）
    if (!g_ctrlSock || !g_previewHwnd) {
        if (!g_diagNoSock) {
            g_diagNoSock = true;
            Log(L"  [控制] [!] 控制通道或预览窗口还没就绪，输入无法发出（只记这一次）\n");
        }
        return;
    }
    if (!g_ctrlAddrValid) {
        if (!g_diagNoAddr) {
            g_diagNoAddr = true;
            Log(L"  [控制] [!] 还没收到被控端的画面，尚不知道往哪发输入（只记这一次）\n");
        }
        return;
    }
    // 被控端已拒绝/已停止：不要再发（否则控制通道一直被无用地占着）
    if (g_peerBlocked) {
        if (g_inputBlocked == 0)
            Log(L"  [控制] 被控端已拒绝/已停止，输入不再发送（后续同类丢弃只计数）\n");
        ++g_inputBlocked;
        return;
    }

    RECT rc{};
    if (!GetClientRect(g_previewHwnd, &rc)) {
        if (!g_diagNoRect) {
            g_diagNoRect = true;
            Log(L"  [控制] [!] 取覆盖窗口客户区失败，输入无法归一化（只记这一次）\n");
        }
        return;
    }
    const int cw = rc.right - rc.left, ch = rc.bottom - rc.top;
    if (cw <= 1 || ch <= 1) {
        if (!g_diagTinyRect) {
            g_diagTinyRect = true;
            Log(L"  [控制] [!] 覆盖窗口客户区退化了（%dx%d），输入无法归一化（只记这一次）\n", cw, ch);
        }
        return;
    }

    if (kind == zx::kInputMouseMove) {
        // 鼠标移动频率极高：限速，否则会把控制通道打满、也会拖慢被控端
        const double now = NowMs();
        if (now - g_lastMoveMs < 8.0) { ++g_inputThrottled; return; }
        g_lastMoveMs = now;
    }
    // 只有鼠标类事件需要坐标归一化；滚轮/键盘不参与
    if (kind == zx::kInputMouseMove || kind == zx::kInputMouseButton) {
        if (x < 0) x = 0; else if (x >= cw) x = cw - 1;
        if (y < 0) y = 0; else if (y >= ch) y = ch - 1;
    }

    zx::InputEvent e{};
    e.kind = kind;
    e.flags = flags;
    e.nx = (int16_t)((int64_t)x * 32767 / (cw - 1));   // 归一化：与分辨率无关
    e.ny = (int16_t)((int64_t)y * 32767 / (ch - 1));
    e.wheel = wheel;
    e.vk = vk;

    uint8_t pkt[zx::kHeaderSize + sizeof(zx::InputEvent)]{};
    zx::MediaPacket p{};
    p.magic = zx::kMagic;
    p.version = zx::kVersion;
    p.type = zx::kTypeInput;
    p.sessionId = g_sessionId;
    p.frameId = g_lastFrameId;                          // ← 陈旧点击判定的依据
    p.timestampUs = (uint64_t)(NowMs() * 1000.0);
    p.payloadSize = (uint16_t)sizeof(zx::InputEvent);
    memcpy(pkt, &p, zx::kHeaderSize);
    memcpy(pkt + zx::kHeaderSize, &e, sizeof(e));

    if (g_ctrlSock->SendToAddr(g_ctrlAddr, pkt, sizeof(pkt)) > 0) {
        ++g_inputSent;
        if (kind == zx::kInputMouseMove) ++g_inputMoves;
        // 前几次一定打日志：否则"输入到底发没发出去"只能靠猜
        if (g_inputSent <= 3 || (g_inputSent % 50) == 0)
            Log(L"  [控制] 已转发输入 kind=%u 归一化(%d,%d) 事件帧=%u（累计 %d）\n",
                (unsigned)kind, (int)e.nx, (int)e.ny, g_lastFrameId, g_inputSent);
    } else {
        Log(L"  [控制] [!] 转发输入失败（sendto 出错）\n");
    }
}

static LRESULT CALLBACK LowLevelKeyboardProc(int code, WPARAM wp, LPARAM lp)
{
    // 【为什么必须用全局低级键盘钩子】覆盖窗口是 WS_EX_NOACTIVATE（不抢焦点），
    // 永远收不到 WM_KEYDOWN。要转发键盘就只能全局挂钩 —— 这也正是设计文档要求
    // "键盘默认关闭、由用户显式开启"的原因。
    // 【只转发、不吞键】结尾照常 CallNextHookEx，本地该收到的键一个不少。
    if (code == HC_ACTION && g_remoteKeyboard) {
        const KBDLLHOOKSTRUCT* k = reinterpret_cast<const KBDLLHOOKSTRUCT*>(lp);
        if (k) {
            const bool down = (wp == WM_KEYDOWN || wp == WM_SYSKEYDOWN);
            SendInputEvent(zx::kInputKey, down ? (uint8_t)zx::kInputDown : (uint8_t)0,
                           0, 0, 0, (uint16_t)k->vkCode);
            ++g_keyEvents;
            // 前几次一定打日志（否则"钩子到底有没有触发"只能靠猜），之后抽样
            if (g_keyEvents <= 3 || (g_keyEvents % 20) == 0)
                Log(L"  [控制] 键盘钩子触发: vk=0x%02X %s（累计 %d）\n",
                    (unsigned)k->vkCode, down ? L"按下" : L"抬起", g_keyEvents);
        }
    }
    return CallNextHookEx(nullptr, code, wp, lp);
}

static LRESULT CALLBACK PreviewWndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    switch (msg) {
    case WM_CLOSE:  g_windowClosed = true; return 0;
    case WM_DESTROY: PostQuitMessage(0); return 0;
    case WM_ERASEBKGND: return 1;   // 由 D3D 负责绘制，别让 GDI 闪一下

    // ---- 远程控制：把覆盖窗口上的鼠标操作转发给被控端 ----
    case WM_MOUSEMOVE: {
        // 诊断：前 3 次鼠标移动一定留痕，否则"到底有没有收到消息"只能靠猜
        static int seen = 0;
        if (g_remoteControl && seen < 3) {
            ++seen;
            RECT cr{};
            GetClientRect(hwnd, &cr);
            Log(L"  [控制] 覆盖窗口收到鼠标移动 #%d 客户端坐标(%d,%d) 客户区 %ldx%ld\n", seen,
                (int)(short)LOWORD(lp), (int)(short)HIWORD(lp),
                (long)(cr.right - cr.left), (long)(cr.bottom - cr.top));
        }
        SendInputEvent(zx::kInputMouseMove, 0, (int)(short)LOWORD(lp), (int)(short)HIWORD(lp), 0);
        return 0;
    }
    case WM_LBUTTONDOWN: SendInputEvent(zx::kInputMouseButton, zx::kInputLeft  | zx::kInputDown, (int)(short)LOWORD(lp), (int)(short)HIWORD(lp), 0); return 0;
    case WM_LBUTTONUP:   SendInputEvent(zx::kInputMouseButton, zx::kInputLeft,                    (int)(short)LOWORD(lp), (int)(short)HIWORD(lp), 0); return 0;
    case WM_RBUTTONDOWN: SendInputEvent(zx::kInputMouseButton, zx::kInputRight | zx::kInputDown, (int)(short)LOWORD(lp), (int)(short)HIWORD(lp), 0); return 0;
    case WM_RBUTTONUP:   SendInputEvent(zx::kInputMouseButton, zx::kInputRight,                   (int)(short)LOWORD(lp), (int)(short)HIWORD(lp), 0); return 0;
    case WM_MBUTTONDOWN: SendInputEvent(zx::kInputMouseButton, zx::kInputMiddle| zx::kInputDown, (int)(short)LOWORD(lp), (int)(short)HIWORD(lp), 0); return 0;
    case WM_MBUTTONUP:   SendInputEvent(zx::kInputMouseButton, zx::kInputMiddle,                  (int)(short)LOWORD(lp), (int)(short)HIWORD(lp), 0); return 0;
    case WM_MOUSEWHEEL:  SendInputEvent(zx::kInputMouseWheel, 0, 0, 0, (int32_t)(short)HIWORD(wp)); return 0;

    default: return DefWindowProcW(hwnd, msg, wp, lp);
    }
}

// 【两种模式】
//   独立窗口：不传 --parent-hwnd，自己开一个顶层窗口（探针验证用）
//   子窗口  ：传 --parent-hwnd <HWND>，创建 WS_CHILD 挂到父窗口下，
//             由父进程（WinUI 应用）决定位置和大小。这是接进应用的形态。
static HWND CreatePreviewWindow(UINT w, UINT h, const wchar_t* title, bool topmost,
                                HWND parent, int posX, int posY, bool frameless)
{
    static bool classRegistered = false;
    if (!classRegistered) {
        WNDCLASSEXW wc{};
        wc.cbSize = sizeof(wc);
        wc.lpfnWndProc = PreviewWndProc;
        wc.hInstance = GetModuleHandleW(nullptr);
        wc.lpszClassName = L"ZxPreviewWnd";
        wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
        wc.hbrBackground = (HBRUSH)GetStockObject(BLACK_BRUSH);
        if (!RegisterClassExW(&wc)) return nullptr;
        classRegistered = true;
    }
    HINSTANCE inst = GetModuleHandleW(nullptr);

    if (parent) {
        // 【子窗口形态】尺寸必须和视频一致：渲染器用 CopyResource 上屏，
        // 而后台缓冲和源纹理必须同尺寸，所以这里先不做缩放。
        //
        // 【注意】实测这条路径在 WinUI 3 下看不到画面：WinUI 把 XAML 内容渲染在
        // 一个 DesktopChildSiteBridge 子窗口里，我们的窗口是它的兄弟，
        // 而 WinUI 会不断把内容岛提到 z 序最前，把它盖住。
        // 所以接 WinUI 时改用 --frameless 的顶层覆盖窗口（见下）。
        HWND hwnd = CreateWindowExW(0, L"ZxPreviewWnd", title,
                                    WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS,
                                    posX, posY, (int)w, (int)h,
                                    parent, nullptr, inst, nullptr);
        return hwnd;
    }

    if (frameless) {
        // 【无边框覆盖窗口】把它当成一块"贴"在父应用窗口上的视频面板。
        // 位置用屏幕坐标，由父进程（WinUI 应用）算出视频区在屏幕上的位置。
        // TOOLWINDOW 让它不出现在任务栏和 Alt+Tab；NOACTIVATE 避免抢焦点。
        HWND hwnd = CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                                    L"ZxPreviewWnd", title,
                                    WS_POPUP,
                                    posX, posY, (int)w, (int)h,
                                    nullptr, nullptr, inst, nullptr);
        if (hwnd) {
            // 【为什么必须显式显示 + 显式置顶】WS_POPUP 本身不带 WS_VISIBLE，
            // 而这条分支原先既没有 WS_VISIBLE 也没有 ShowWindow/SetWindowPos。
            // 实测后果：窗口矩形完全正确（(196,203)-(1476,923)，与父应用算出的视频区
            // 一致），但 IsWindowVisible=false、WS_EX_TOPMOST 也没设上 ——
            // 助手在正常收包解码，屏幕上却什么都看不到（父应用里就是一片黑）。
            // 不要依赖 CreateWindowEx 的默认行为，这里和下面"顶层窗口形态"保持同一套动作。
            ShowWindow(hwnd, SW_SHOWNOACTIVATE);
            UpdateWindow(hwnd);
            SetWindowPos(hwnd, topmost ? HWND_TOPMOST : HWND_TOP, 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW);

            // 【实测补丁】上面这一次 SetWindowPos 在**无边框分支**上不保证把 WS_EX_TOPMOST 设上：
            // 同一份代码，非 frameless 的顶层窗口量到 TOPMOST=True（exStyle=0x00000108），
            // 而 frameless 覆盖窗口量到 TOPMOST=False（exStyle=0x08000080，只有 TOOLWINDOW|NOACTIVATE）。
            // 后果很实际：覆盖窗口随时会被别的窗口盖住，用户看到的就是"视频没了"。
            // 所以这里显式补上扩展样式位，再置顶一次，并把结果读回写进日志（不许静默失败）。
            if (topmost) {
                LONG_PTR ex = GetWindowLongPtrW(hwnd, GWL_EXSTYLE);
                const bool wasSet = (ex & WS_EX_TOPMOST) != 0;
                if (!wasSet) {
                    SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_TOPMOST);
                    SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
                }
                const bool nowSet = (GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST) != 0;
                Log(L"  [信息] 覆盖窗口置顶: WS_EX_TOPMOST %s（首次 SetWindowPos 后=%s，补设后=%s）"
                    L"—— 交给消息泵里的重试继续兜底\n",
                    nowSet ? L"已生效" : L"设置失败", wasSet ? L"已带" : L"未带", nowSet ? L"已带" : L"未带");
                // 创建时这一次实测不生效，所以标记让消息泵重试（见 ReassertOverlayTopmost）
                g_overlayTopmostWanted = true;
            }
        }
        return hwnd;
    }

    // 顶层窗口形态（探针自测用）：固定尺寸，不做缩放
    const DWORD style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX;
    RECT r{ 0, 0, (LONG)w, (LONG)h };
    AdjustWindowRect(&r, style, FALSE);
    HWND hwnd = CreateWindowExW(0, L"ZxPreviewWnd", title, style,
                                posX, posY,
                                r.right - r.left, r.bottom - r.top,
                                nullptr, nullptr, inst, nullptr);
    if (hwnd) {
        ShowWindow(hwnd, SW_SHOWNORMAL);
        UpdateWindow(hwnd);
        // 提到 z 序顶部但不抢焦点：否则截图时窗口可能被别的窗口盖住
        SetWindowPos(hwnd, topmost ? HWND_TOPMOST : HWND_TOP, 0, 0, 0, 0,
                     SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW);
    }
    return hwnd;
}

static void PumpMessages()
{
    MSG msg;
    while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    // 消息泵每轮都顺手确认一下覆盖窗口还在最上层（创建时那一次实测不生效，见上面的注释）
    ReassertOverlayTopmost();
}

// ---------------------------------------------------------------------------
// stdin 命令通道（父进程用来调整位置 / 停止）
// 只在被父进程以管道方式启动时有内容；手动运行时 PeekNamedPipe 会失败，直接跳过。
// ---------------------------------------------------------------------------
// 说明：g_previewHwnd 在文件上方提前声明了（输入转发那一节要用）。

static void PollStdin()
{
    static std::string pending;
    HANDLE h = GetStdHandle(STD_INPUT_HANDLE);
    if (!h || h == INVALID_HANDLE_VALUE) return;

    DWORD avail = 0;
    if (!PeekNamedPipe(h, nullptr, 0, nullptr, &avail, nullptr) || avail == 0) return;

    char buf[512];
    DWORD got = 0;
    if (!ReadFile(h, buf, sizeof(buf) - 1, &got, nullptr) || got == 0) return;
    buf[got] = 0;
    pending += buf;

    size_t pos;
    while ((pos = pending.find('\n')) != std::string::npos) {
        std::string line = pending.substr(0, pos);
        pending.erase(0, pos + 1);
        while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
        if (line.empty()) continue;

        if (line.rfind("rect ", 0) == 0) {
            int x = 0, y = 0, w = 0, h = 0;
            const int n = sscanf_s(line.c_str() + 5, "%d %d %d %d", &x, &y, &w, &h);
            if (n >= 2 && g_previewHwnd) {
                // 【坐标空间】frameless 顶层窗口用屏幕坐标；子窗口形态下是相对父窗口客户区。
                // 【为什么要带宽高】视频区尺寸由父应用决定，可以小于/大于视频分辨率。
                // 这里只改窗口尺寸，**不动 swapchain**：swapchain 仍保持视频尺寸，
                // 由 DXGI_SCALING_STRETCH（CreateSwapChainForHwnd 的默认值）把画面拉伸到
                // 新的客户区，所以渲染端（CopyResource 同尺寸）完全不用改。
                const bool withSize = (n >= 4 && w > 0 && h > 0);
                // 【带上 SWP_SHOWWINDOW】父应用被最小化后还原时，只要再发一次 rect 就能显示回来，
                // 不需要额外的 show 命令（最小化走下面的 "hide"）。
                //
                // 【锚点：把覆盖窗口插到应用主窗口的"正上方"】
                // 实测这个覆盖窗口拿不到 WS_EX_TOPMOST（见 docs/winui-overlay-verified 的后续一节），
                // 而产品真正需要的是"视频盖在自己应用窗口之上"，不是"盖住屏幕上所有窗口"。
                // 父应用通过 "anchor <hwnd>" 把主窗口句柄告诉这里之后，就把覆盖窗口插到它正上方：
                //   SetWindowPos(hwnd, appHwnd, …) = "放在 appHwnd 之后"，即紧贴它的上一层。
                // 没有锚点时保持原来的行为（SWP_NOZORDER，不动 z 序）。
                const HWND insertAfter = g_overlayAnchor ? g_overlayAnchor : nullptr;
                const UINT flags = (withSize ? (SWP_NOACTIVATE | (insertAfter ? 0u : SWP_NOZORDER))
                                             : (SWP_NOSIZE | SWP_NOACTIVATE | (insertAfter ? 0u : SWP_NOZORDER)))
                                   | SWP_SHOWWINDOW;
                SetWindowPos(g_previewHwnd, insertAfter, x, y,
                             withSize ? w : 0, withSize ? h : 0, flags);
                // 记一行日志：窗口矩形一变就有据可查（父应用那边同时写 videoprobe-pos.txt，
                // 两边对照就能确认"父应用算的显示区"和"窗口实际矩形"是否一致）
                RECT wr{};
                if (GetWindowRect(g_previewHwnd, &wr)) {
                    Log(L"  [信息] rect 命令：窗口 -> (%ld,%ld)-(%ld,%ld)  %ldx%ld\n",
                        wr.left, wr.top, wr.right, wr.bottom,
                        wr.right - wr.left, wr.bottom - wr.top);
                }
            }
        } else if (line.rfind("anchor ", 0) == 0) {
            // 父应用把主窗口句柄告诉助手：覆盖窗口会被插到它正上方（见 rect 处的说明）。
            // 传 0 表示取消锚点。十六进制/十进制都收（父应用用 "0x%llX" 发过来）。
            const unsigned long long v = _strtoui64(line.c_str() + 7, nullptr, 0);
            g_overlayAnchor = (HWND)(uintptr_t)v;
            Log(L"  [信息] 收到锚点：覆盖窗口将插到窗口 0x%llX 正上方\n", v);
            // 立刻按当前矩形重排一次，让锚点马上生效
            if (g_previewHwnd && g_overlayAnchor) {
                RECT wr{};
                GetWindowRect(g_previewHwnd, &wr);
                SetWindowPos(g_previewHwnd, g_overlayAnchor, 0, 0, 0, 0,
                             SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW);
            }
        } else if (line == "hide") {
            // 【父应用最小化时把覆盖窗口藏起来】顶层窗口不会跟着最小化，
            // 不藏的话屏幕上会一直挂着一块"孤儿"画面。
            if (g_previewHwnd) {
                ShowWindow(g_previewHwnd, SW_HIDE);
                Log(L"  [信息] 收到 hide：覆盖窗口已隐藏\n");
            }
        } else if (line == "quit") {
            g_windowClosed = true;
        }
    }
}

int wmain(int argc, wchar_t** argv)
{
    const int seconds = (argc > 1) ? _wtoi(argv[1]) : 8;
    const uint16_t port = (argc > 2) ? (uint16_t)_wtoi(argv[2]) : 41001;
    const UINT expW = (argc > 3) ? (UINT)_wtoi(argv[3]) : 1920;
    const UINT expH = (argc > 4) ? (UINT)_wtoi(argv[4]) : 1080;
    const UINT expFps = (argc > 5) ? (UINT)_wtoi(argv[5]) : 30;

    // --window 打开预览窗口，把解码结果直接上屏（否则只存 BMP 存证）
    // --topmost  让窗口浮在最上层，便于截图取证
    // --parent-hwnd <HWND>  作为子窗口挂到指定父窗口下（WinUI 集成用），隐含 --window
    // --pos <x> <y>         窗口位置（子窗口时相对父窗口客户区）
    bool windowMode = false;
    bool topmost = false;
    HWND parentHwnd = nullptr;
    bool frameless = false;
    int posX = 40, posY = 40;
    for (int i = 1; i < argc; ++i) {
        if (wcscmp(argv[i], L"--window") == 0) {
            windowMode = true;
        } else if (wcscmp(argv[i], L"--topmost") == 0) {
            topmost = true;
        } else if (wcscmp(argv[i], L"--parent-hwnd") == 0 && i + 1 < argc) {
            parentHwnd = (HWND)(uintptr_t)_wcstoui64(argv[++i], nullptr, 0);
            windowMode = true;
        } else if (wcscmp(argv[i], L"--pos") == 0 && i + 2 < argc) {
            posX = _wtoi(argv[++i]);
            posY = _wtoi(argv[++i]);
        } else if (wcscmp(argv[i], L"--frameless") == 0) {
            frameless = true;
        } else if (wcscmp(argv[i], L"--remote-control") == 0) {
            // 允许把覆盖窗口上的鼠标操作转发给被控端（默认关；正式应用里要用户授权后才开）
            g_remoteControl = true;
        } else if (wcscmp(argv[i], L"--remote-keyboard") == 0) {
            // 转发键盘（默认关，且隐含开启远程控制）
            g_remoteControl = true;
            g_remoteKeyboard = true;
        }
    }

    SetConsoleOutputCP(CP_UTF8);
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
    {
        const std::wstring lp = ExeRelative(L"receiver-probe-log.txt");
        _wfopen_s(&g_log, lp.c_str(), L"w, ccs=UTF-8");
    }

    Log(L"=== 接收端：UDP → 重组 → 解码 → 画面 ===\n");
    Log(L"监听端口 %u，运行 %d 秒，期望 %ux%u@%u，预览窗口=%d\n\n",
        port, seconds, expW, expH, expFps, (int)windowMode);

    bool ok = false;
    // libavcodec 不需要 Media Foundation，但 FFmpeg 的 Windows 后端会用到 COM
    HRESULT hrCo = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    (void)hrCo;

    {
        WsaGuard wsa;
        if (!wsa.ok()) { Log(L"[X] WSAStartup 失败\n"); goto done; }

        UdpSocket rx, reqSock;
        // IDR 帧约 130KB（100+ 分片）毫秒级冲进来，接收缓冲必须够大
        // 【必须在 bind 之前设置】Windows 上 bind 后再设可能不生效，仍是 64KB
        if (!rx.Open(port, "0.0.0.0", true, 32 << 20, 1 << 20)) {
            Log(L"[X] 端口 %u 绑定失败\n", port); goto done;
        }
        if (!reqSock.Open((uint16_t)(port + 1), "127.0.0.1", true, 1 << 20, 1 << 20)) {
            Log(L"[X] 请求口绑定失败\n"); goto done;
        }
        g_ctrlSock = reqSock.valid() ? &reqSock : nullptr;   // 远程控制输入走这条通道
        Log(L"接收缓冲实际生效 %d 字节\n\n", rx.GetRecvBuffer());
        rx.SetRecvTimeout(5);
        reqSock.SetRecvTimeout(1);   // 控制状态包要在这个 socket 上轮询，不能阻塞

        sockaddr_in senderAddr{};
        bool haveSender = false;

        Reassembler ra;
        FfmpegH264Decoder dec;
        bool decoderReady = false;
        bool needResync = true;          // 初始也要等第一个 IDR 才能开始解码
        int lastSessionSwitches = 0;     // 会话切换次数（用于日志/事件，见 ra.stats().sessionSwitches）

        int packets = 0, mediaPackets = 0, otherPackets = 0;
        int fedFrames = 0, feedErrors = 0, skippedWhileResync = 0, resyncs = 0;
        double feedCostSum = 0, feedCostMax = 0;
        int bmpSaved = 0;
        uint64_t totalBytes = 0;
        std::wstring decName;
        double lastReqMs = -1e9;
        double firstPacketMs = 0, firstDecodedMs = 0;

        // 只存 3 张：第一帧、约 1 秒处、约 2 秒处
        const int saveAt[] = { 1, 30, 60 };

        // 解码延迟实测：记下每帧「喂入解码器的时刻」，输出时配对求差。
        // 这是接收端延迟的主要来源，必须量出来而不是猜。
        std::map<uint64_t, double> feedTimes;
        double latSumMs = 0, latMaxMs = 0, latLastMs = -1;
        int latSamples = 0;

        // ---- 预览窗口（可选）----
        D3D11WindowRenderer renderer;
        HWND previewHwnd = nullptr;
        if (windowMode) {
            previewHwnd = CreatePreviewWindow(expW, expH, L"棕仙语音 — 接收画面预览",
                                              topmost, parentHwnd, posX, posY, frameless);
            g_previewHwnd = previewHwnd;
            // 把窗口实际矩形记下来：接 WinUI 时位置是父进程算的，
            // 一旦算错（坐标空间搞混）画面上只会看到一片黑，很难定位。
            if (previewHwnd) {
                RECT wr{};
                GetWindowRect(previewHwnd, &wr);
                Log(L"  [信息] 窗口实际矩形 屏幕(%ld,%ld)-(%ld,%ld)  %ldx%ld\n",
                    wr.left, wr.top, wr.right, wr.bottom,
                    wr.right - wr.left, wr.bottom - wr.top);
            }
            if (!previewHwnd) {
                Log(L"[!] 创建预览窗口失败，退回只存 BMP\n");
                windowMode = false;
            } else if (!renderer.Init(previewHwnd, expW, expH)) {
                Log(L"[!] 初始化 D3D11 渲染器失败，退回只存 BMP\n");
                DestroyWindow(previewHwnd);
                previewHwnd = nullptr;
                windowMode = false;
            } else {
                // 上屏用 BGRA，免去着色器与 NV12 双平面处理的麻烦
                dec.SetOutputFormat(FfmpegH264Decoder::OutputFormat::Bgra);
                Log(L"[OK] 预览窗口已创建 %ux%u（%s），渲染路径 = BGRA 纹理直传\n",
                    expW, expH, parentHwnd ? L"子窗口" : (frameless ? L"无边框覆盖窗口" : L"顶层窗口"));
                // 给父进程一个明确的就绪信号
                Log(L"event view-ready hwnd=%llu\n",
                    (unsigned long long)(uintptr_t)previewHwnd);

                // 键盘转发：只有显式开启才装钩子（默认不装，避免任何意外影响本地键盘）
                if (g_remoteKeyboard) {
                    g_kbHook = SetWindowsHookExW(WH_KEYBOARD_LL, LowLevelKeyboardProc,
                                                 GetModuleHandleW(nullptr), 0);
                    Log(L"  [信息] 键盘转发: %s\n",
                        g_kbHook ? L"已装低级键盘钩子（全局；只转发不吞键，本地照常收到）"
                                 : L"钩子安装失败，键盘不会转发");
                }
            }
        }

        // 当前码流分辨率（第一帧解码后才知道）。父应用靠 event stream-size 按真实宽高比算 letterbox。
        UINT streamW = 0, streamH = 0;

        dec.SetCallback([&](const FfmpegH264Decoder::DecodedFrame& f) {
            if (firstDecodedMs == 0) firstDecodedMs = NowMs();

            auto it = feedTimes.find(f.timestampUs);
            if (it != feedTimes.end()) {
                latLastMs = NowMs() - it->second;
                latSumMs += latLastMs;
                if (latLastMs > latMaxMs) latMaxMs = latLastMs;
                ++latSamples;
                feedTimes.erase(feedTimes.begin(), it);   // 清掉更旧的
                feedTimes.erase(it);
            }

            if (windowMode && f.format == FfmpegH264Decoder::OutputFormat::Bgra) {
                // 流分辨率一变就上报：父应用据此重算显示区（否则非 16:9 的流会被拉变形）。
                // 同时把真实尺寸传给渲染器，让它自己把后台缓冲调整到源尺寸。
                if (f.width != streamW || f.height != streamH) {
                    streamW = f.width;
                    streamH = f.height;
                    Log(L"  [信息] 流分辨率 %ux%u\n", streamW, streamH);
                    Log(L"event stream-size %u %u\n", streamW, streamH);
                }
                renderer.PresentBgra(f.data, f.stride, f.width, f.height);
                return;
            }

            for (int k = 0; k < 3; ++k) {
                if (saveAt[k] == dec.decodedFrames()) {
                    wchar_t name[64];
                    swprintf_s(name, L"receiver-frame-%d.bmp", k);
                    const std::wstring p = ExeRelative(name);
                    if (SaveNv12AsBmp(f.data, f.stride, f.width, f.height, expH, p.c_str())) {
                        Log(L"  [OK] 已存证 %s（%ux%u，显示高度 %u）\n",
                            name, f.width, f.height, expH);
                        ++bmpSaved;
                    }
                }
            }
        });

        std::vector<uint8_t> buf(4096);
        const double tStart = NowMs();
        double lastReport = tStart;
        int lastDecoded = 0;

        while (NowMs() - tStart < seconds * 1000.0) {
            // 窗口模式要自己泵消息，否则窗口会卡住不响应
            if (windowMode) {
                PumpMessages();
                PollStdin();
                if (g_windowClosed) { Log(L"  [信息] 收到停止命令或窗口关闭，提前结束\n"); break; }
            }
            // 被控端的授权状态（走 reqSock 回来）：收到"拒绝/停止"就不再发输入
            if (g_remoteControl && g_ctrlSock) {
                for (;;) {
                    uint8_t tmp[64];
                    sockaddr_in from{};
                    const int cn = g_ctrlSock->RecvFrom(tmp, sizeof(tmp), &from);
                    if (cn <= 0) break;
                    if (cn < (int)kHeaderSize + 1) continue;
                    MediaPacket sp{};
                    memcpy(&sp, tmp, kHeaderSize);
                    if (sp.magic != kMagic || sp.type != kTypeControlState) continue;
                    const uint8_t st = tmp[kHeaderSize];
                    if (st != g_peerState) {
                        g_peerState = st;
                        g_peerBlocked = (st == kStateDenied || st == kStateStopped);
                        Log(L"  [控制] 被控端授权状态: %s\n", StateText(st));
                    }
                }
            }
            const int n = rx.RecvFrom(buf.data(), buf.size(), &senderAddr);
            if (n > 0) {
                ++packets;
                totalBytes += (uint64_t)n;
                if (!haveSender) firstPacketMs = NowMs();
                haveSender = true;
                if (n >= (int)kHeaderSize) {
                    MediaPacket p{};
                    memcpy(&p, buf.data(), kHeaderSize);
                    // 学到被控端地址与会话号：远程控制输入要发回这里
                    if (p.magic == kMagic) {
                        if (!g_ctrlAddrValid) {
                            char ip[32]{};
                            InetNtopA(AF_INET, &senderAddr.sin_addr, ip, sizeof(ip));
                            Log(L"  [控制] 已学到被控端地址 %S:%u（输入将发到这里）\n",
                                ip, (unsigned)ntohs(senderAddr.sin_port));
                        }
                        g_ctrlAddr = senderAddr;
                        g_ctrlAddrValid = true;
                        g_sessionId = p.sessionId;
                    }
                    if (p.magic == kMagic && p.type == kTypeMedia) {
                        ++mediaPackets;
                        const Reassembler::Result r =
                            ra.OnPacket(p, buf.data() + kHeaderSize, (size_t)n - kHeaderSize);

                        // 会话切换（对端重启 / 换了一条流）：重组器已经自己重置，
                        // 这里只负责让日志与状态栏能看见，否则现象是"包在收、帧不动"，很难定位。
                        if ((int)ra.stats().sessionSwitches != lastSessionSwitches) {
                            lastSessionSwitches = (int)ra.stats().sessionSwitches;
                            needResync = false;   // 新会话会带自己的 IDR，不必再等旧的
                            Log(L"  [信息] 检测到新会话 sessionId=0x%04X（第 %d 次），"
                                L"重组器已重置\n",
                                (unsigned)p.sessionId, lastSessionSwitches);
                            // 【Bug 修复】被控端换了会话 = 一个新的被控端实例，
                            // 之前"已被拒绝/已停止"的判断不能继承，否则查看端会永远不再发输入。
                            if (g_peerBlocked || g_peerState != 0xFF) {
                                g_peerBlocked = false;
                                g_peerState = 0xFF;
                                Log(L"  [控制] 被控端换了会话，授权状态已复位（可以重新发起控制请求）\n");
                            }
                        }

                        if (r.delivered) {
                            const bool isKey = ra.frameIsKeyframe();
                            // 记录"正在显示"的帧号：远程控制事件带着它，被控端据此判断是否过期
                            g_lastFrameId = ra.frameId();
                            if (r.gapDetected || r.droppedIncomplete) needResync = true;

                            if (needResync && !isKey) {
                                ++skippedWhileResync;
                            } else {
                                if (!decoderReady) {
                                    std::vector<uint8_t> seq;
                                    if (ExtractSeqHeader(ra.frameData().data(),
                                                         ra.frameData().size(), seq)) {
                                        // 顺便看看 SPS 声明了多少参考帧
                                        {
                                            const int nrf = ParseNumRefFrames(seq.data(), seq.size());
                                            Log(L"  [信息] SPS max_num_ref_frames = %d%s\n", nrf,
                                                nrf < 0 ? L"（解析未完成）" : L"");
                                        }
                                        std::wstring err;
                                        if (dec.Open(seq.data(), seq.size(), expW, expH, expFps, err)) {
                                            decoderReady = true;
                                            decName = dec.name();
                                            Log(L"  [OK] 解码器就绪: %s（序列头 %zu 字节）\n",
                                                decName.c_str(), seq.size());
                                            Log(L"       低延迟设置: %s\n", dec.configNote().c_str());
                                        } else {
                                            Log(L"  [!] 打开解码器失败: %s\n", err.c_str());
                                        }
                                    }
                                }
                                if (decoderReady) {
                                    if (needResync) {
                                        dec.Flush();
                                        needResync = false;
                                        ++resyncs;
                                    }
                                    // 【先记时刻再喂】Feed 内部就会取出已解好的帧，
                                    // 回调可能立刻触发，所以时间戳条目必须先建好
                                    feedTimes[ra.frameTimestampUs()] = NowMs();
                                    const double feedStart = NowMs();
                                    const bool fed = dec.Feed(ra.frameData().data(),
                                                              ra.frameData().size(),
                                                              ra.frameTimestampUs());
                                    const double feedCost = NowMs() - feedStart;
                                    feedCostSum += feedCost;
                                    if (feedCost > feedCostMax) feedCostMax = feedCost;
                                    if (fed) ++fedFrames;
                                    else ++feedErrors;
                                }
                            }
                        }
                    } else {
                        ++otherPackets;
                    }
                }
            }

            // 有未恢复的缺口：限速请求关键帧（200ms 一次，避免请求风暴）
            if (ra.needsKeyframe() && haveSender && NowMs() - lastReqMs >= 200.0) {
                MediaPacket req{};
                req.magic = kMagic;
                req.version = kVersion;
                req.type = kTypeKeyframeRequest;
                req.streamId = kStreamVideo;
                reqSock.SendToAddr(senderAddr, &req, kHeaderSize);
                lastReqMs = NowMs();
            }

            // 每秒报一次进度
            const double now = NowMs();
            if (now - lastReport >= 1000.0) {
                const int d = dec.decodedFrames();
                Log(L"  [%4.1f 秒] 收包 %d  已重组帧 %llu  已解码 %d  本秒解码 %d  解码延迟 %.0f ms\n",
                    (now - tStart) / 1000.0, packets,
                    (unsigned long long)ra.stats().framesDelivered, d, d - lastDecoded,
                    latLastMs >= 0 ? latLastMs : 0.0);
                // 机器可读的一行：父应用靠它刷新状态栏。
                // 【为什么必须另发一条 ASCII 事件】中文那行经管道到 C# 时编码对不上
                // （C# 侧按 UTF-8 解码，CRT 写管道用的是 ANSI 代码页），
                // 靠 Contains("已解码") 匹配永远不中 —— 状态栏因此一直停在"等待推流…"。
                Log(L"event stats recv=%d assembled=%llu decoded=%d presented=%u lat=%.0f sessions=%d\n",
                    packets, (unsigned long long)ra.stats().framesDelivered, d,
                    renderer.framesPresented(), latLastMs >= 0 ? latLastMs : 0.0,
                    lastSessionSwitches);
                lastDecoded = d;
                lastReport = now;
            }
        }

        // 收尾排空：把解码器里压着的帧取出来，否则统计会少一截
        dec.Drain();

        // ---- 报告 ----
        const Reassembler::Stats& s = ra.stats();
        Log(L"\n=== 结果 ===\n");
        Log(L"  收包总数        : %d（媒体 %d / 其他 %d）共 %llu 字节\n",
            packets, mediaPackets, otherPackets, (unsigned long long)totalBytes);
        Log(L"  重组交付帧      : %llu\n", (unsigned long long)s.framesDelivered);
        Log(L"  分片不齐丢弃帧  : %llu\n", (unsigned long long)s.framesDropped);
        Log(L"  检测到缺口      : %llu 次，共跨 %llu 帧\n",
            (unsigned long long)s.gaps, (unsigned long long)s.framesMissing);
        Log(L"  XOR 校验救回    : %llu 片\n", (unsigned long long)s.parityRecoveries);
        Log(L"  喂入解码器      : %d 帧（失败 %d）\n", fedFrames, feedErrors);
        Log(L"  解码器重同步    : %d 次，跳过非 IDR %d 帧\n", resyncs, skippedWhileResync);
        Log(L"  解码输出        : %d 帧\n", dec.decodedFrames());
        Log(L"  已存证画面      : %d 张\n", bmpSaved);
        if (windowMode)
            Log(L"  已上屏帧数      : %u 帧（窗口 %ux%u）\n",
                renderer.framesPresented(), renderer.width(), renderer.height());
        if (decoderReady)
            Log(L"  解码器          : %s  %ux%u stride=%u\n",
                decName.c_str(), dec.width(), dec.height(), dec.stride());
        if (firstPacketMs > 0 && firstDecodedMs > 0)
            Log(L"  首包→首帧耗时   : %.1f ms\n", firstDecodedMs - firstPacketMs);
        if (latSamples > 0)
            Log(L"  解码延迟        : 平均 %.1f ms / 最大 %.1f ms（%d 次采样）\n",
                latSumMs / latSamples, latMaxMs, latSamples);
        if (fedFrames > 0)
            Log(L"  解码耗时        : 平均 %.2f ms/帧 / 最大 %.2f ms（1080p30 预算是 33.3 ms）\n",
                feedCostSum / fedFrames, feedCostMax);

        ok = (dec.decodedFrames() > 0);
        // 窗口模式没有 BMP 存证，改用「实际上屏帧数」作为成功判据
        ok = ok && (windowMode ? (renderer.framesPresented() > 0) : (bmpSaved > 0));
        Log(L"\n=== 结论 ===\n");
        if (g_remoteControl) {
            Log(L"  --- 远程控制（查看端）---\n");
            Log(L"  转发鼠标事件 %d 个（其中移动 %d，限速丢弃 %d）\n",
                g_inputSent, g_inputMoves, g_inputThrottled);
            if (g_remoteKeyboard)
                Log(L"  键盘钩子收到按键 %d 个（按下+抬起都算）\n", g_keyEvents);
            if (g_peerState != 0xFF)
                Log(L"  被控端最终授权状态: %s\n", StateText(g_peerState));
            if (g_inputBlocked > 0)
                Log(L"  被拒/被停之后又拦下 %d 个输入（没再发出去）\n", g_inputBlocked);
            if (!g_ctrlAddrValid) Log(L"  [!] 一直没收到被控端地址，输入没发出去\n");
        }
        if (ok) {
            Log(L"  [OK] 接收端可用：UDP 收包 → 重组 → 解码 → 画面，全程走通。\n");
            Log(L"       下一步：把 NV12 接到 WinUI 渲染，并接上远程控制与音频。\n");
        } else {
            Log(L"  [X] 没有解出画面，按上面的数字定位。\n");
        }

        if (g_kbHook) { UnhookWindowsHookEx(g_kbHook); g_kbHook = nullptr; }
        dec.Close();
        rx.Close();
        reqSock.Close();
    }

done:
    CoUninitialize();
    if (g_log) fclose(g_log);
    return ok ? 0 : 1;
}
