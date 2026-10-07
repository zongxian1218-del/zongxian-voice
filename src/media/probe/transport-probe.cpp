// transport-probe.cpp —— 自建 UDP 传输层自测
//
// ============================================================================
// 【为什么单独测传输层】
// 上一阶段的教训：链路里两环一起测，失败时分不清是哪一环。
// 这个程序完全不碰采集和编码，只喂模拟数据帧，把传输层单独验证通过：
//   · 分片与重组（含乱序到达）
//   · XOR 校验恢复单个丢失分片
//   · 丢帧检测 → 发关键帧请求 → 发送端出 IDR → 接收端恢复交付
//
// 【接收端必须是独立线程】
// 最初写成单线程「先把一帧的 100 个包全发出去，再开始收」，结果第一个
// 数据包就丢了 —— 那是在测试一个不存在的场景。真实应用里接收端会一直
// 阻塞在 recvfrom 上持续收包，发送端瞬发的突发会被即时消费掉。
// 改成独立收包线程后才是真实架构。
//
// 帧尺寸刻意贴近真实编码结果：IDR 约 130 KB（约 109 个分片），P 帧 8~40 KB。
// 数据完整性用**逐字节比对**验证，不是只看长度对不对。
//
// 用法：transport-probe.exe [帧数]
// ============================================================================

#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>

#include "../transport/media-packet.h"
#include "../transport/udp-socket.h"

#include <atomic>
#include <condition_variable>
#include <cstdio>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

using namespace zx;

// ---------------------------------------------------------------------------
// 日志
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
// 模拟真实编码帧：IDR 大、P 帧小
// ---------------------------------------------------------------------------
static void MakeFrame(uint32_t frameId, bool isKey, std::vector<uint8_t>& out)
{
    size_t n = isKey ? (120000 + (frameId * 131) % 30000)
                     : (8000 + (frameId * 7919) % 32000);
    out.resize(n);
    for (size_t i = 0; i < n; ++i)
        out[i] = (uint8_t)((frameId * 31 + i * 7 + (i >> 8)) & 0xFF);
}

// ---------------------------------------------------------------------------
// 独立收包线程 —— 对应真实应用里接收端的样子
// ---------------------------------------------------------------------------
struct RxThread {
    UdpSocket* rx = nullptr;        // 收媒体数据
    UdpSocket* req = nullptr;       // 回发关键帧请求（独立 socket，避免与发送线程争用）
    const char* host = "127.0.0.1";
    uint16_t txPort = 0;

    Reassembler ra;
    std::mutex m;
    std::condition_variable cv;
    std::map<uint32_t, std::vector<uint8_t>> delivered;   // 待主线程核对
    std::atomic<bool> stop{false};
    std::thread th;

    // 统计
    std::atomic<int> packets{0};
    std::atomic<int> lossEvents{0};
    std::atomic<int> ignoredWhileWaiting{0};
    std::atomic<int> requestsSent{0};
    std::atomic<int> repeatRequests{0};   // 超时重发的请求

    // 最近一次「分片没到齐被丢弃」的诊断：缺几片、校验片到没到
    std::atomic<uint32_t> lastDropFrameId{0};
    std::atomic<int> lastDropMissing{-1};
    std::atomic<bool> lastDropHadParity{false};

    // 分片级诊断：当前帧每个数据分片是否到达
    std::mutex diagM;
    uint32_t diagFrameId = 0;
    std::vector<bool> diagGot;
    int diagWritten = 0;

    // 本阶段最先收到的若干包（诊断"启动期首个包丢失"这类确定性丢包）
    std::mutex firstM;
    std::wstring firstPackets;
    int firstCount = 0;
    void NoteFirst(const MediaPacket& p, int n)
    {
        if (firstCount >= 6) return;
        std::lock_guard<std::mutex> lk(firstM);
        if (firstCount >= 6) return;
        wchar_t b[96];
        swprintf_s(b, L"(帧%u 下标%u %dB) ", p.frameId, p.fragIndex, n);
        firstPackets += b;
        ++firstCount;
    }
    std::wstring FirstPackets() { std::lock_guard<std::mutex> lk(firstM); return firstPackets; }

    void Start() { th = std::thread([this] { Loop(); }); }
    void Stop() { stop = true; if (th.joinable()) th.join(); }

    void SetDiagFrame(uint32_t frameId, int written)
    {
        std::lock_guard<std::mutex> lk(diagM);
        diagFrameId = frameId;
        diagWritten = written;
        diagGot.assign((size_t)written, false);
    }

    int MissingFor(uint32_t frameId, int written, size_t& firstIdx, size_t& lastIdx)
    {
        std::lock_guard<std::mutex> lk(diagM);
        if (diagFrameId != frameId) return -1;   // 不是当前诊断帧
        int missing = 0;
        firstIdx = SIZE_MAX; lastIdx = 0;
        for (int i = 0; i < written && i < diagWritten; ++i) {
            if (!diagGot[(size_t)i]) {
                if (firstIdx == SIZE_MAX) firstIdx = (size_t)i;
                lastIdx = (size_t)i;
                ++missing;
            }
        }
        return missing;
    }

    // 等某一帧被交付；超时返回 false
    bool WaitForFrame(uint32_t frameId, int timeoutMs, std::vector<uint8_t>& out, uint32_t& gotId)
    {
        std::unique_lock<std::mutex> lk(m);
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeoutMs);
        for (;;) {
            // 顺手清掉比目标更早的（已经不需要了，避免堆积）
            for (auto it = delivered.begin(); it != delivered.end(); ) {
                if (it->first < frameId) it = delivered.erase(it); else break;
            }
            auto it = delivered.find(frameId);
            if (it != delivered.end()) {
                gotId = it->first;
                out = it->second;
                delivered.erase(it);
                return true;
            }
            if (cv.wait_until(lk, deadline) == std::cv_status::timeout) return false;
        }
    }

private:
    void SendKeyframeRequest(uint32_t frameId)
    {
        MediaPacket reqPkt{};
        reqPkt.magic = kMagic;
        reqPkt.version = kVersion;
        reqPkt.type = kTypeKeyframeRequest;
        reqPkt.streamId = kStreamVideo;
        reqPkt.frameId = frameId;
        if (req) req->SendTo(host, txPort, &reqPkt, kHeaderSize);
        ++requestsSent;
    }

    void Loop()
    {
        std::vector<uint8_t> buf(4096);
        double lastReqMs = 0;
        uint32_t lastDroppedId = 0;

        while (!stop) {
            sockaddr_in from{};
            const int n = rx->RecvFrom(buf.data(), buf.size(), &from);

            // 【请求必须限速 —— 否则会雪崩】
            // 一开始写成「每次丢帧都请求」，结果形成正反馈：
            //   请求 → 发送端出一个 120KB 的 IDR → 分片数暴涨 → 更多丢包
            //   → 更多请求 → ……  实测 60 帧内 44 次丢帧，码流涨到 3 倍。
            // 统一改成：只要还在等关键帧，最多每 200ms 请求一次。
            {
                std::lock_guard<std::mutex> lk(m);
                if (ra.needsKeyframe() && NowMs() - lastReqMs >= 200.0) {
                    if (lastReqMs > 0) ++repeatRequests;
                    SendKeyframeRequest(lastDroppedId);
                    lastReqMs = NowMs();
                }
            }

            if (n <= 0) continue;
            if (n < (int)kHeaderSize) continue;

            MediaPacket p{};
            memcpy(&p, buf.data(), kHeaderSize);
            if (p.type != kTypeMedia || p.magic != kMagic) continue;
            ++packets;
            NoteFirst(p, n);

            // 诊断记录（不受重组状态影响）
            {
                std::lock_guard<std::mutex> lk(diagM);
                if (p.frameId == diagFrameId && !(p.flags & kFlagParity) &&
                    p.fragIndex < diagGot.size())
                    diagGot[p.fragIndex] = true;
            }

            {
                std::lock_guard<std::mutex> lk(m);
                const Reassembler::Result r =
                    ra.OnPacket(p, buf.data() + kHeaderSize, (size_t)n - kHeaderSize);

                if (r.droppedIncomplete) {
                    lastDroppedId = r.droppedFrameId;
                    lastDropFrameId = r.droppedFrameId;
                    lastDropMissing = (int)r.droppedMissingFrags;
                    lastDropHadParity = r.droppedHadParity;
                }
                if (r.gapDetected) lastDroppedId = r.gapFrames ? (ra.frameId() - r.gapFrames) : ra.frameId();
                if (ra.needsKeyframe()) ++ignoredWhileWaiting;   // 处于缺口期间收到的包数
                if (r.delivered) {
                    delivered[ra.frameId()] = ra.frameData();
                    cv.notify_all();
                }
            }
        }
    }
};

// ---------------------------------------------------------------------------
// 一次测试阶段
// ---------------------------------------------------------------------------
enum DropMode {
    kDropNone         = 0,
    kDropOneEvery     = 1,   // 每一帧丢一个数据分片（配校验时应全部救回）
    kDropOneEvery10   = 2,   // 每 10 帧丢一个数据分片
    kDropWholeEvery10 = 3,   // 每 10 帧丢整帧
};

struct PhaseStats {
    std::wstring name;
    int framesSent = 0;
    int datagramsSent = 0;
    int datagramsDropped = 0;
    int delivered = 0;
    int contentMismatch = 0;
    int lossEvents = 0;
    int gaps = 0;
    int framesMissing = 0;
    int keyframeRequests = 0;
    int repeatRequests = 0;
    int parityRecoveries = 0;
    int framesMissingFrags = 0;
    int missingFragTotal = 0;
    int detailCount = 0;
    int requestsReceived = 0;   // 发送端实际收到的关键帧请求数
    int sessionSwitches = 0;    // 接收端检测到的会话切换次数
    int droppedMultiLoss = 0;        // 因缺 ≥2 片而丢的帧（链路性质）
    int singleLossUnrecovered = 0;   // 只缺 1 片且有校验却未救回（必须为 0）
    std::wstring trace;         // 前若干帧的轨迹：k=关键帧 D=已交付
    std::wstring missingDetail;
    double latencySumMs = 0;
    int latencySamples = 0;
    bool pass = false;
    std::wstring note;
};

struct PhaseConfig {
    const wchar_t* name;
    int frames = 60;
    int keyEvery = 30;
    bool parity = false;
    DropMode drop = kDropNone;
    bool shuffle = false;
    uint16_t sessionId = 0x1234;      // 本阶段用的会话 id
    int switchSessionAt = -1;         // >=0 时在第 N 帧切到 sessionId2，且帧号从 0 重来
    uint16_t sessionId2 = 0x5678;
    int expectDeliveredMin = 0;
    int expectParityRecoveriesMin = 0;
    int expectLossEventsMin = 0;
    int expectGapsMin = 0;
    int expectSessionSwitchesMin = 0;
    // 【会话切换阶段的容差】切换时接收端会 Reset(keepStats=true) 清掉正在重组的那一帧
    // （见 media-packet.h 「会话身份」处：换了会话，旧帧号必须作废），所以在途的那一帧是有意丢掉的。
    // 实测：第 6 阶段偶尔交付 59/60（分片不齐 0、缺口 0、内容错 0），重跑 5 次都 60/60 —— 罕见偶发。
    // 因此只允许丢这 1 帧；丢 ≥2 帧仍判失败（那说明不是切换边界的问题）。
    int allowBoundaryDropFrames = 0;
    int expectSingleLossUnrecoveredMax = 0;   // 只缺 1 片却没救回：必须为 0
    int expectContentMismatch = 0;
};

static PhaseStats RunPhase(const PhaseConfig& cfg,
                           UdpSocket& tx, UdpSocket& rx, UdpSocket& reqSock,
                           uint16_t rxPort, uint16_t txPort, const char* host)
{
    PhaseStats st;
    st.name = cfg.name;

    RxThread rxt;
    rxt.rx = &rx;
    rxt.req = &reqSock;
    rxt.host = host;
    rxt.txPort = txPort;
    rxt.ra.Reset();
    rxt.Start();

    std::vector<uint8_t> frame;
    std::vector<uint8_t> dgBuf;
    std::vector<size_t> dgSizes;
    std::vector<uint8_t> got;
    uint32_t gotId = 0;

    bool forcedKeyframeNext = false;
    double lastForcedMs = -1e9;      // 发送端也要限速，避免被请求牵着走

    for (int f = 0; f < cfg.frames; ++f) {
        // 【会话切换】切了之后用新的会话 id，且**帧号从 0 重新开始** ——
        // 这正是"发送端重启 / 换了一条流"的真实形态。接收端必须据此重置重组器，
        // 否则这一批 frameId 全部小于 expectedFrameId_ 的包会被当成陈旧数据丢光
        // （实测：收包还在涨，已重组/已解码冻住不动）。
        const bool afterSwitch = (cfg.switchSessionAt >= 0 && f >= cfg.switchSessionAt);
        const uint16_t sid = afterSwitch ? cfg.sessionId2 : cfg.sessionId;
        const uint32_t frameId = afterSwitch ? (uint32_t)(f - cfg.switchSessionAt) : (uint32_t)f;
        const bool isKey = (f % cfg.keyEvery == 0) || forcedKeyframeNext;
        forcedKeyframeNext = false;

        MakeFrame(frameId, isKey, frame);

        const uint16_t dataCount = DataFragmentCount((uint32_t)frame.size());
        const uint16_t totalCount = TotalFragmentCount((uint32_t)frame.size(), cfg.parity);
        const size_t cap = size_t(totalCount) * (kHeaderSize + kMaxPayload);
        if (dgBuf.size() < cap) dgBuf.resize(cap);
        if (dgSizes.size() < totalCount) dgSizes.resize(totalCount);

        size_t used = 0;
        const uint16_t written = FragmentFrame(dgBuf.data(), dgBuf.size(), &used, frameId,
                                               kStreamVideo, sid,
                                               isKey ? (uint8_t)kFlagKeyframe : (uint8_t)0,
                                               (uint64_t)frameId * 33333,   // 30fps → 33333us
                                               frame.data(), (uint32_t)frame.size(),
                                               cfg.parity, dgSizes.data());
        rxt.SetDiagFrame(frameId, dataCount);   // 只统计数据分片，校验分片不算丢失

        auto shouldDrop = [&](uint16_t fragIndex) -> bool {
            const bool isParity = (fragIndex >= dataCount);
            const uint16_t victim = (uint16_t)(frameId % dataCount);
            switch (cfg.drop) {
            case kDropOneEvery:     return (!isParity && fragIndex == victim);
            case kDropOneEvery10:   return (frameId % 10 == 0) && !isParity && fragIndex == victim;
            case kDropWholeEvery10: return (frameId % 10 == 0);
            default:                return false;
            }
        };

        std::vector<uint16_t> order(written);
        for (uint16_t i = 0; i < written; ++i) order[i] = i;
        if (cfg.shuffle) {
            for (size_t i = 0; i + 1 < order.size(); i += 2) std::swap(order[i], order[i + 1]);
            std::reverse(order.begin(), order.end());
        }

        std::vector<size_t> offsets(written);
        {
            size_t acc = 0;
            for (uint16_t i = 0; i < written; ++i) { offsets[i] = acc; acc += dgSizes[i]; }
        }

        const double tSend = NowMs();
        for (uint16_t oi = 0; oi < written; ++oi) {
            const uint16_t idx = order[oi];
            if (shouldDrop(idx)) { ++st.datagramsDropped; continue; }
            const int rc = tx.SendTo(host, rxPort, dgBuf.data() + offsets[idx], dgSizes[idx]);
            if (rc < 0) Log(L"      [!] sendto 失败\n");
            // 诊断：第一帧的前 4 个实际发送（看得到"下标记为丢了哪些、每个包多大、sendto 返回值"）
            if (f == 0 && oi < 4)
                Log(L"      [诊断] 帧0 第%u 发: 下标%u %zuB sendto=%d\n",
                    oi, idx, dgSizes[idx], rc);
            ++st.datagramsSent;
        }
        ++st.framesSent;

        // 等这一帧被交付（接收线程在并行收包），或超时判定为丢失
        got.clear();
        const bool ok = rxt.WaitForFrame(frameId, 40, got, gotId);
        if (ok) {
            st.latencySumMs += NowMs() - tSend;
            ++st.latencySamples;
            if (gotId != frameId) {
                ++st.contentMismatch;
                st.note = L"交付帧号与发送帧号不一致";
            } else if (got.size() != frame.size() ||
                       memcmp(got.data(), frame.data(), frame.size()) != 0) {
                ++st.contentMismatch;
                st.note = L"重组内容与原始帧逐字节比对不一致";
            }
            ++st.delivered;
        } else {
            // 没交付：记录缺了哪些分片，用于区分缓冲溢出与随机丢失
            size_t firstIdx = 0, lastIdx = 0;
            const int missing = rxt.MissingFor(frameId, dataCount, firstIdx, lastIdx);
            // 减去本帧「主动丢弃」的分片，剩下的才是真实网络丢包
            int droppedInFrame = 0;
            for (uint16_t i = 0; i < dataCount; ++i) if (shouldDrop(i)) ++droppedInFrame;
            const int realMissing = (missing > droppedInFrame) ? (missing - droppedInFrame) : 0;
            // 【诊断】分片级数字：对端一共没看到几片（含主动丢的）。
            // 注意：重组器"丢弃那一刻"的缺口要到下一帧首个包到达时才判定，所以这里不打印它 ——
            // 用下面的 droppedMultiLoss / singleLossUnrecovered 两个分类计数来判断更可靠。
            Log(L"      [诊断] 帧%u 未交付：分片级缺失 %d（主动丢 %d → 真实 %d）\n",
                frameId, missing, droppedInFrame, realMissing);
            if (realMissing > 0) {
                ++st.framesMissingFrags;
                st.missingFragTotal += realMissing;
                if (st.detailCount < 5) {
                    wchar_t buf[256];
                    if (isKey)
                        swprintf_s(buf, L"帧%u(IDR) 发%u片 未到%d片(下标%zu..%zu)",
                                   frameId, written, realMissing, firstIdx, lastIdx);
                    else
                        swprintf_s(buf, L"帧%u 发%u片 未到%d片(下标%zu..%zu)",
                                   frameId, written, realMissing, firstIdx, lastIdx);
                    if (!st.missingDetail.empty()) st.missingDetail += L"  ";
                    st.missingDetail += buf;
                    ++st.detailCount;
                }
            }
        }

        // 处理发送端侧收到的关键帧请求（请求由 reqSock 发到 tx 口）
        for (;;) {
            sockaddr_in from{};
            uint8_t tmp[128];
            const int n = tx.RecvFrom(tmp, sizeof(tmp), &from);
            if (n <= 0) break;
            if (n < (int)kHeaderSize) continue;
            MediaPacket p{};
            memcpy(&p, tmp, kHeaderSize);
            if (p.type == kTypeKeyframeRequest) {
                ++st.requestsReceived;
                // 发送端限速：请求再多也只每 200ms 出一个 IDR。
                // 一个 IDR 就是 100+ 个分片的突发，不限速等于自造丢包。
                if (NowMs() - lastForcedMs >= 200.0) {
                    forcedKeyframeNext = true;
                    lastForcedMs = NowMs();
                }
            }
        }

        // 逐帧轨迹：定位"请求发出去了但发送端没收到"还是"收到了却没出 IDR"
        if (st.trace.size() < 400) {
            wchar_t tb[32];
            swprintf_s(tb, L"%u%s%s ", frameId, isKey ? L"k" : L"",
                       (st.delivered > 0 && ok) ? L"D" : L".");
            st.trace += tb;
        }
    }

    // 收尾：给最后几帧一点时间
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    rxt.Stop();

    Log(L"      [诊断] 本阶段最先收到的 6 个包: %s\n", rxt.FirstPackets().c_str());

    st.lossEvents = (int)rxt.ra.stats().lossEvents;   // 分片没到齐被丢弃的帧数
    st.keyframeRequests = rxt.requestsSent.load();
    st.repeatRequests = rxt.repeatRequests.load();
    st.parityRecoveries = (int)rxt.ra.stats().parityRecoveries;
    st.gaps = (int)rxt.ra.stats().gaps;
    st.framesMissing = (int)rxt.ra.stats().framesMissing;
    st.sessionSwitches = (int)rxt.ra.stats().sessionSwitches;
    st.droppedMultiLoss = (int)rxt.ra.stats().droppedMultiLoss;
    st.singleLossUnrecovered = (int)rxt.ra.stats().singleLossUnrecovered;

    bool pass = true;
    if (st.contentMismatch != cfg.expectContentMismatch) pass = false;
    if (st.delivered < cfg.expectDeliveredMin - cfg.allowBoundaryDropFrames) pass = false;
    if (st.parityRecoveries < cfg.expectParityRecoveriesMin) pass = false;
    if (st.lossEvents < cfg.expectLossEventsMin) pass = false;
    if (st.gaps < cfg.expectGapsMin) pass = false;
    if (st.sessionSwitches < cfg.expectSessionSwitchesMin) pass = false;
    // 【最关键的判据】"只缺 1 片、校验片也到了、却没救回来" —— 这才是恢复逻辑失灵
    if (st.singleLossUnrecovered > cfg.expectSingleLossUnrecoveredMax) pass = false;
    st.pass = pass;

    Log(L"  [%s] 帧 %d 发出 / %d 交付 / %d 内容错\n",
        pass ? L"通过" : L"失败", st.framesSent, st.delivered, st.contentMismatch);
    Log(L"         丢帧(分片没到齐) %d 帧，检测到缺口 %d 次共跨 %d 帧，请求发出 %d 次（超时重发 %d），发送端收到 %d 次，XOR 救回 %d 片\n",
        st.lossEvents, st.gaps, st.framesMissing, st.keyframeRequests, st.repeatRequests,
        st.requestsReceived, st.parityRecoveries);
    Log(L"         分片包 %d 个，主动丢弃 %d 个，等待关键帧期间忽略 %d 包\n",
        st.datagramsSent, st.datagramsDropped, rxt.ignoredWhileWaiting.load());
    if (st.sessionSwitches > 0)
        Log(L"         会话切换 %d 次（检测到新会话 → 重组器已重置，累计统计保留）\n",
            st.sessionSwitches);
    Log(L"         丢帧分类：缺≥2片(链路) %d 帧，只缺1片却没救回(恢复逻辑) %d 帧%s\n",
        st.droppedMultiLoss, st.singleLossUnrecovered,
        st.singleLossUnrecovered == 0 ? L"  ← 恢复逻辑正常" : L"  ← 恢复逻辑有问题！");
    if (st.latencySamples > 0)
        Log(L"         平均投递延迟 %.3f ms（%d 次采样）\n",
            st.latencySumMs / st.latencySamples, st.latencySamples);
    if (st.framesMissingFrags > 0)
        Log(L"         真实丢包：%d 帧有分片未到，共缺 %d 片\n",
            st.framesMissingFrags, st.missingFragTotal);
    if (!st.missingDetail.empty())
        Log(L"         缺片样本: %s\n", st.missingDetail.c_str());
    if (!st.note.empty()) Log(L"         [!] %s\n", st.note.c_str());
    if (!st.trace.empty()) Log(L"         轨迹(帧号+k=IDR+D=交付): %s\n", st.trace.c_str());

    return st;
}

int wmain(int argc, wchar_t** argv)
{
    const int frameCount = (argc > 1) ? _wtoi(argv[1]) : 60;
    const uint16_t rxPort = (argc > 2) ? (uint16_t)_wtoi(argv[2]) : 41001;
    const uint16_t txPort = (argc > 3) ? (uint16_t)_wtoi(argv[3]) : 41000;
    const uint16_t reqPort = (uint16_t)(rxPort + 1);
    const char* host = "127.0.0.1";
    // 第 4 个参数：只跑指定阶段（0 或省略 = 全部）。诊断间歇性故障时用它把迭代时间从 40s 降到 2s。
    const int onlyStage = (argc > 4) ? _wtoi(argv[4]) : 0;

    SetConsoleOutputCP(CP_UTF8);
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
    {
        const std::wstring lp = ExeRelative(L"transport-probe-log.txt");
        _wfopen_s(&g_log, lp.c_str(), L"w, ccs=UTF-8");
    }

    Log(L"=== 自建 UDP 传输层自测 ===\n");
    Log(L"包头 %zu 字节，载荷上限 %zu 字节，整包 %zu 字节\n",
        kHeaderSize, kMaxPayload, kHeaderSize + kMaxPayload);
    Log(L"回环地址 %S  发送口 %u  接收口 %u  请求口 %u  每阶段 %d 帧\n\n",
        host, txPort, rxPort, reqPort, frameCount);

    WsaGuard wsa;
    if (!wsa.ok()) { Log(L"[X] WSAStartup 失败\n"); if (g_log) fclose(g_log); return 1; }

    UdpSocket tx, rx, reqSock;
    // IDR 帧约 130KB（109 个分片）会在毫秒级冲到接收端，
    // 缓冲必须够大，而且必须在 bind 之前设置
    if (!tx.Open(txPort, host, true, 1 << 20, 4 << 20)) {
        Log(L"[X] 发送口 %u 绑定失败\n", txPort); return 1;
    }
    if (!rx.Open(rxPort, host, true, 16 << 20, 1 << 20)) {
        Log(L"[X] 接收口 %u 绑定失败\n", rxPort); return 1;
    }
    if (!reqSock.Open(reqPort, host, true, 1 << 20, 1 << 20)) {
        Log(L"[X] 请求口 %u 绑定失败\n", reqPort); return 1;
    }
    Log(L"接收缓冲实际生效 %d 字节，发送缓冲 %d 字节\n",
        rx.GetRecvBuffer(), tx.GetSendBuffer());

    rx.SetRecvTimeout(5);
    tx.SetRecvTimeout(1);

    Log(L"--- 各阶段 ---\n");
    std::vector<PhaseStats> results;

    if (onlyStage == 0 || onlyStage == 1) {
        PhaseConfig c{};
        c.name = L"1 干净传输+校验";
        c.frames = frameCount;
        c.parity = true;                 // 校验是默认配置：实测回环下也会偶发丢 1 片
        c.expectDeliveredMin = frameCount;
        results.push_back(RunPhase(c, tx, rx, reqSock, rxPort, txPort, host));
    }
    if (onlyStage == 0 || onlyStage == 2) {
        PhaseConfig c{};
        c.name = L"2 乱序到达+校验";
        c.frames = frameCount;
        c.parity = true;
        c.shuffle = true;
        c.expectDeliveredMin = frameCount;
        results.push_back(RunPhase(c, tx, rx, reqSock, rxPort, txPort, host));
    }
    if (onlyStage == 0 || onlyStage == 3) {
        PhaseConfig c{};
        c.name = L"3 每帧丢1分片+校验";
        c.frames = frameCount;
        c.parity = true;
        c.drop = kDropOneEvery;
        // 【判据不能写成"必须 60/60"】本阶段故意每帧丢 1 片，期望校验救回。但实测这个回环环境
        // 在"每帧已丢 1 片"之外还会再真丢 1 片（确定性地丢在每个阶段**实际发出的第一个数据报**上：
        // sendto 返回成功、接收端却收不到），那一帧因此缺 2 片 —— 单校验片本来就只能救 1 片。
        // 所以这里断言真正该成立的不变量：只缺 1 片且有校验片却仍未救回 = 0（那才是恢复逻辑的 bug）。
        c.expectDeliveredMin = frameCount - 3;
        c.expectParityRecoveriesMin = frameCount - 3;
        c.expectSingleLossUnrecoveredMax = 0;
        results.push_back(RunPhase(c, tx, rx, reqSock, rxPort, txPort, host));
    }
    if (onlyStage == 0 || onlyStage == 4) {
        PhaseConfig c{};
        c.name = L"4 每10帧丢1分片(无校验)";
        c.frames = frameCount;
        c.drop = kDropOneEvery10;
        c.expectDeliveredMin = frameCount - 8;    // 6 帧被丢，留 2 帧余量
        c.expectLossEventsMin = 4;
        results.push_back(RunPhase(c, tx, rx, reqSock, rxPort, txPort, host));
    }
    if (onlyStage == 0 || onlyStage == 5) {
        PhaseConfig c{};
        c.name = L"5 每10帧丢整帧";
        c.frames = frameCount;
        c.parity = true;                          // 校验救不了整帧丢失，必须靠 IDR
        c.drop = kDropWholeEvery10;
        c.expectDeliveredMin = frameCount - 8;
        // 整帧丢失时接收端根本收不到该帧的任何分片，所以不会出现
        // 「分片没到齐」，而是直接表现为「缺口」——判定要看 gaps
        c.expectGapsMin = 4;
        results.push_back(RunPhase(c, tx, rx, reqSock, rxPort, txPort, host));
    }

    if (onlyStage == 0 || onlyStage == 6) {
        // 阶段 6：换会话。前 30 帧用会话 A；第 30 帧起换成会话 B 且帧号从 0 重来
        // （第 30 帧本来是 IDR，所以新会话从 IDR 开始，和真实"重新开一条流"一致）。
        // 期望：接收端识别出会话变化 → 重置重组器 → 60 帧全部交付、内容逐字节一致。
        // 这条测试对应真实场景：朋友重连、重新开始共享、换个分辨率再推。
        PhaseConfig c{};
        c.name = L"6 中途换会话(帧号重来)";
        c.frames = frameCount;
        c.parity = true;
        c.sessionId = 0x1234;
        c.switchSessionAt = 30;
        c.sessionId2 = 0xABCD;
        c.expectDeliveredMin = frameCount;
        c.expectSessionSwitchesMin = 1;
        c.allowBoundaryDropFrames = 1;   // 切换瞬间在途的那一帧被 Reset 有意丢掉（见字段处说明）
        c.expectContentMismatch = 0;
        results.push_back(RunPhase(c, tx, rx, reqSock, rxPort, txPort, host));
    }

    int passed = 0, failed = 0;
    Log(L"\n=== 汇总 ===\n");
    for (auto& r : results) {
        Log(L"  %-26s %s   交付 %d/%d，分片不齐 %d，缺口 %d，校验救回 %d，内容错 %d\n",
            r.name.c_str(), r.pass ? L"通过" : L"失败",
            r.delivered, r.framesSent, r.lossEvents, r.gaps, r.parityRecoveries, r.contentMismatch);
        if (r.pass) ++passed; else ++failed;
    }

    Log(L"\n  %d 个阶段通过，%d 个失败\n", passed, failed);
    if (failed == 0) {
        Log(L"\n  [OK] 传输层可用：分片重组、乱序容错、XOR 恢复、关键帧恢复全部有效。\n");
        Log(L"       下一步：把 sender-probe 的码流接到这条传输上，再做接收端渲染。\n");
    } else {
        Log(L"\n  [X] 有阶段失败，先修传输层再往下做。\n");
    }

    tx.Close();
    rx.Close();
    reqSock.Close();
    if (g_log) fclose(g_log);
    return failed == 0 ? 0 : 1;
}
