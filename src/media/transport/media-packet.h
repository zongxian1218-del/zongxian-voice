// media-packet.h —— 自建 UDP 传输的包格式与重组逻辑
//
// ============================================================================
// 【设计目标】局域网/Radmin 组网，低丢包，主打低延迟看视频。刻意保持简单：
//   · 固定 32 字节包头，载荷 1200 字节 → 整包 1232 字节，稳在 1500 MTU 以下
//   · 分片 + 序号 + 重组，支持乱序到达
//   · 每帧可选 1 个 XOR 奇偶校验包，能救回任意 1 个丢失分片
//   · 丢帧时报告「缺口」，由消费端决定是否请求 IDR
//
// 【职责边界 —— 这一点踩过坑】
// 传输层只做两件事：把分片重组成完整帧、按 frameId 严格递增的顺序交付。
// 它**不判断**某一帧能不能解码，也**不因为丢帧而停止交付**。
//
// 早先版本让传输层自己进入「等关键帧」状态，丢帧后忽略所有非关键帧。
// 结果这个状态和请求限速互相作用，陷入长时间冻结：丢一帧要等 200ms 的
// 限速周期，期间所有帧被丢弃，实测 60 帧只交付 10~13 帧。
// 现在改为：照常交付 + 如实报告缺口（gapDetected / needsKeyframe），
// 由接收端的解码器决定"缺口之后的非 IDR 帧不喂进去"。
//
// 【为什么丢帧必须请求关键帧，而不是重传】
// H.264 的 P 帧依赖前面的帧。丢一帧之后，后续所有非 IDR 帧都解不出来，
// 重传那一帧也没用（它依赖的已经错过了）。唯一正确的恢复方式是让发送端
// 重新出一个 IDR，接收端从 IDR 重新开始解码。
// ============================================================================

#pragma once

#include <cstdint>
#include <cstring>
#include <vector>

namespace zx {

// ---------------------------------------------------------------------------
// 常量
// ---------------------------------------------------------------------------
constexpr uint8_t kMagic        = 0x5A;
constexpr uint8_t kVersion      = 1;
constexpr size_t  kMaxPayload   = 1200;   // 单包最大载荷
constexpr size_t  kHeaderSize   = 32;

enum PacketType : uint8_t {
    kTypeMedia           = 0,   // 媒体数据分片
    kTypeKeyframeRequest = 1,   // 「我丢帧了，请出 IDR」
    kTypePing            = 2,   // 保活/测延迟
    kTypeInput           = 3,   // 远程控制输入事件（查看端 → 被控端），载荷是 InputEvent
    kTypeControlState    = 4,   // 被控端的授权状态（被控端 → 查看端），载荷是 1 字节 ControlState
};

// 被控端的授权状态：查看端据此决定还能不能继续发输入
enum ControlState : uint8_t {
    kStatePending = 0,   // 已弹出授权窗口，等待被控端本人决定
    kStateAllowed = 1,   // 已授权（鼠标允许；键盘另看位）
    kStateDenied  = 2,   // 被拒绝 / 30 秒无响应自动拒绝
    kStateStopped = 3,   // 授权后被本人一键停止
};

// 远程控制：输入事件（放在 kTypeInput 包的载荷里）
enum InputKind : uint8_t {
    kInputMouseMove   = 0,
    kInputMouseButton = 1,
    kInputMouseWheel  = 2,
    kInputKey         = 3,
};

enum InputFlags : uint8_t {
    kInputLeft   = 0x01,
    kInputRight  = 0x02,
    kInputMiddle = 0x04,
    kInputDown   = 0x08,   // 置位 = 按下，清位 = 抬起（仅按键类事件用）
};

#pragma pack(push, 1)
struct InputEvent {
    uint8_t  kind;      // InputKind
    uint8_t  flags;     // InputFlags 位组合
    int16_t  nx;        // 归一化坐标 0..32767（相对被控端屏幕，与分辨率无关）
    int16_t  ny;
    int32_t  wheel;     // 滚轮增量（WHEEL_DELTA 的倍数）
    uint16_t vk;        // 键盘虚拟键码（鼠标事件填 0）
};
#pragma pack(pop)
static_assert(sizeof(InputEvent) == 12, "输入事件载荷布局不能变");


enum PacketFlags : uint8_t {
    kFlagKeyframe = 0x01,       // 本帧是 IDR
    kFlagParity   = 0x02,       // 本分片是 XOR 校验分片
};

enum StreamId : uint8_t {
    kStreamVideo = 0,
    kStreamAudio = 1,           // 音频走同一套封装，共同时钟
};

#pragma pack(push, 1)
struct MediaPacket {
    uint8_t  magic;             // 必须是 kMagic
    uint8_t  version;
    uint8_t  type;              // PacketType
    uint8_t  flags;             // PacketFlags 位组合
    uint8_t  streamId;          // StreamId
    uint16_t sessionId;         // 发送端每次运行随机生成；变了 = 新会话（对端重启 / 换发送端）
    uint8_t  reserved0;         // 补齐到 4 字节边界
    uint32_t frameId;           // 单调递增，用于排序与检测丢帧
    uint16_t fragIndex;
    uint16_t fragCount;         // 含校验分片的总数
    uint64_t timestampUs;       // 采集时刻（微秒）—— 音视频共同时钟，用于同步
    uint32_t totalFrameBytes;   // 整帧字节数，接收端据此分配缓冲
    uint16_t payloadSize;
    uint16_t reserved1;
};
#pragma pack(pop)

// 包头一旦变形，两端的兼容性会静默出错，所以用静态断言钉死
static_assert(sizeof(MediaPacket) == kHeaderSize, "包头的 32 字节布局不能变");

// ---------------------------------------------------------------------------
// 分片计算
// ---------------------------------------------------------------------------
inline uint16_t DataFragmentCount(uint32_t frameBytes)
{
    if (frameBytes == 0) return 1;
    return (uint16_t)((frameBytes + kMaxPayload - 1) / kMaxPayload);
}

// 总包数 = 数据分片 + （启用校验且分片多于 1 时）1 个校验包
inline uint16_t TotalFragmentCount(uint32_t frameBytes, bool parity)
{
    const uint16_t data = DataFragmentCount(frameBytes);
    return (parity && data > 1) ? (uint16_t)(data + 1) : data;
}

// 某个数据分片应有的字节数（最后一片可能短一些）
inline size_t FragmentBytes(uint32_t frameBytes, uint16_t fragIndex)
{
    const size_t offset = (size_t)fragIndex * kMaxPayload;
    if (offset >= frameBytes) return 0;
    const size_t remain = frameBytes - offset;
    return remain < kMaxPayload ? remain : kMaxPayload;
}

// ---------------------------------------------------------------------------
// 重组器
//
// 契约：
//   · 只有在「某帧的所有数据分片都到齐」时才交付
//   · 交付顺序严格按 frameId 递增；迟到的老帧丢弃
//   · 中间有帧没到时，如实报告 gapDetected，但完整的帧照常交付
//   · needsKeyframe() 表示「有缺口，后续非 IDR 帧不可解码」，
//     消费端据此请求 IDR，直到交付了一个 IDR 才自动清除
// ---------------------------------------------------------------------------
class Reassembler {
public:
    struct Result {
        bool     delivered        = false;  // 有一整帧可交付
        bool     gapDetected      = false;  // 本帧之前有帧丢失（新的缺口）
        uint32_t gapFrames        = 0;      // 缺口覆盖了多少帧
        bool     droppedIncomplete = false; // 有一帧因为分片没到齐被丢弃
        uint32_t droppedFrameId   = 0;
        // 【诊断用】丢弃那一刻还缺几个数据分片、校验分片到没到。
        // 缺 1 片 + 有校验 => 本该被救回却没救回（真 bug）；缺 ≥2 片 => XOR 本来就救不了（是链路丢了更多）。
        uint16_t droppedMissingFrags = 0;
        bool     droppedHadParity = false;
        bool     parityRecovered  = false;  // 本次靠 XOR 校验救回了分片
        bool     duplicate        = false;  // 重复分片（乱序/重发导致）
        bool     ignored          = false;  // 包头非法/参数不符，已忽略
    };

    struct Stats {
        uint64_t packetsAccepted = 0;
        uint64_t duplicates = 0;
        uint64_t framesDelivered = 0;
        uint64_t framesDropped = 0;     // 因分片没到齐而丢弃的帧
        uint64_t framesMissing = 0;     // 缺口累计跨过的帧数
        uint64_t lossEvents = 0;        // 丢失「事件」次数（≠ 被弃帧数）
        uint64_t gaps = 0;
        uint64_t parityRecoveries = 0;
        uint64_t sessionSwitches = 0;   // 检测到对端换了会话（重组器重置）的次数
        // 【这两个才是"校验恢复到底有没有失灵"的判据】
        // 单校验片只能救回 1 片，所以"缺 ≥2 片"救不回来是链路的性质，不是代码缺陷；
        // 而"只缺 1 片、校验片也已到、却没救回来"就一定是恢复逻辑的 bug —— 它必须永远是 0。
        uint64_t droppedMultiLoss = 0;        // 因缺 ≥2 片而丢弃的帧
        uint64_t singleLossUnrecovered = 0;   // 只缺 1 片且有校验片却仍被丢弃的帧（必须为 0）
    };

    // keepStats=true 用于「同一接收端的会话切换」：清掉半成品帧与帧号期望，
    // 但保留累计统计 —— 否则每秒日志里的"已重组帧/已解码"会突然归零，看起来像出了故障。
    void Reset(bool keepStats = false)
    {
        frame_.clear();
        have_.clear();
        parity_.clear();
        assembling_ = false;
        needsKeyframe_ = false;
        expectedFrameId_ = 0;
        haveExpected_ = false;
        curFrameId_ = 0;
        curTimestampUs_ = 0;
        curKeyframe_ = false;
        curFrameBytes_ = 0;
        dataFragCount_ = 0;
        haveCount_ = 0;
        parityHave_ = false;
        if (!keepStats) stats_ = Stats{};
    }

    const Stats& stats() const { return stats_; }

    // 有缺口未恢复：消费端应请求关键帧，并暂停向解码器喂非 IDR 帧
    bool needsKeyframe() const { return needsKeyframe_; }

    // 交付内容（仅在 delivered == true 时有效）
    const std::vector<uint8_t>& frameData() const { return frame_; }
    uint32_t frameId() const { return curFrameId_; }
    uint64_t frameTimestampUs() const { return curTimestampUs_; }
    bool frameIsKeyframe() const { return curKeyframe_; }

    Result OnPacket(const MediaPacket& p, const uint8_t* payload, size_t len)
    {
        Result r;
        if (p.magic != kMagic || p.version != kVersion) { r.ignored = true; return r; }
        if (p.type != kTypeMedia) { r.ignored = true; return r; }
        if (len < p.payloadSize) { r.ignored = true; return r; }
        len = p.payloadSize;

        // 【会话身份：换了对端就必须重置重组状态】
        // 对端重启、或换了一个发送端之后，frameId 会从 0 重来。没有这一步的话，下面
        // 「frameId < expectedFrameId_ 的包按陈旧数据丢弃」会把整条新流丢光 ——
        // 实测：收包从 39754 一路涨到 53123，而已重组/已解码冻在 1192/1156。
        // 注意 Reset 不碰 sessionId_/haveSession_，否则下一包又会被判成"刚换会话"。
        if (!haveSession_ || p.sessionId != sessionId_) {
            const bool isSwitch = haveSession_;
            haveSession_ = true;
            sessionId_ = p.sessionId;
            Reset(/*keepStats=*/true);
            if (isSwitch) ++stats_.sessionSwitches;
        }

        ++stats_.packetsAccepted;

        const bool isParity = (p.flags & kFlagParity) != 0;

        // 【迟到的旧帧分片必须直接忽略】
        // 真实网络里跨帧乱序很常见（比如帧 N 的校验包在帧 N+1 开始组装后才到）。
        // 如果不判断，这个旧包会被当成「新帧」处理，把正在组装的 N+1 重置成 N，
        // 于是 N+1 的后续分片又触发一次「新帧」…… 陷入churn。
        // 实测表现为「交付 60/60 却报告 58 次丢帧」这种自相矛盾的数字。
        if (haveExpected_ && p.frameId < expectedFrameId_) { r.ignored = true; return r; }
        if (assembling_ && p.frameId < curFrameId_) { r.ignored = true; return r; }

        // ---- 是否该开启新的一帧 ----
        if (!assembling_ || p.frameId != curFrameId_) {
            if (assembling_) {
                // 上一帧没凑齐就来了新帧 —— 如实报告一次丢帧，但不停止工作
                r.droppedIncomplete = true;
                r.droppedFrameId = curFrameId_;
                r.droppedMissingFrags = (uint16_t)(dataFragCount_ - haveCount_);
                r.droppedHadParity = parityHave_;
                // 分类记一笔：缺 ≥2 片是链路性质；只缺 1 片且校验已到却没救回 = 恢复逻辑有 bug
                if (r.droppedMissingFrags >= 2) ++stats_.droppedMultiLoss;
                else if (r.droppedMissingFrags == 1 && parityHave_) ++stats_.singleLossUnrecovered;
                ++stats_.framesDropped;
                ++stats_.lossEvents;
                if (!needsKeyframe_) needsKeyframe_ = true;
            }
            BeginFrame(p);
        }

        // ---- 收下这个分片 ----
        // 【注意】不能用 have_.size() 判断数据分片下标：校验分片的
        // fragIndex 恰好等于数据分片数，会被误判为越界而丢弃。
        if (isParity) {
            if (parityHave_) { ++stats_.duplicates; r.duplicate = true; }
            else { parity_.assign(payload, payload + len); parityHave_ = true; }
        } else {
            if (p.fragIndex >= dataFragCount_) { r.ignored = true; return r; }
            if (have_[p.fragIndex]) {
                ++stats_.duplicates;
                r.duplicate = true;
            } else {
                const size_t off = (size_t)p.fragIndex * kMaxPayload;
                const size_t n = FragmentBytes(curFrameBytes_, p.fragIndex);
                const size_t copy = (len < n) ? len : n;
                if (off + copy <= frame_.size()) memcpy(frame_.data() + off, payload, copy);
                have_[p.fragIndex] = true;
                ++haveCount_;
            }
        }

        // ---- 尝试用校验包救回缺失分片 ----
        if (parityHave_ && dataFragCount_ > 1 && haveCount_ == (size_t)dataFragCount_ - 1) {
            int missing = -1;
            for (uint16_t i = 0; i < dataFragCount_; ++i)
                if (!have_[i]) { missing = i; break; }
            if (missing >= 0) {
                RecoverWithParity((uint16_t)missing);
                if (have_[(uint16_t)missing]) {
                    r.parityRecovered = true;
                    ++stats_.parityRecoveries;
                }
            }
        }

        // ---- 收齐了就按序交付 ----
        if (haveCount_ != dataFragCount_) return r;

        if (haveExpected_) {
            if (curFrameId_ < expectedFrameId_) {
                // 迟到的老帧：更新的已经交付过了，丢弃
                ++stats_.duplicates;
                r.duplicate = true;
                assembling_ = false;
                return r;
            }
            if (curFrameId_ > expectedFrameId_) {
                // 中间有帧没到。这一帧本身是完整的，照常交付，
                // 但要把缺口如实报出去，让消费端去请求 IDR。
                r.gapDetected = true;
                r.gapFrames = curFrameId_ - expectedFrameId_;
                stats_.framesMissing += r.gapFrames;
                ++stats_.gaps;
                if (!needsKeyframe_) needsKeyframe_ = true;
            }
        }

        expectedFrameId_ = curFrameId_ + 1;
        haveExpected_ = true;
        assembling_ = false;
        // IDR 自带全部依赖，交付它就意味着可以重新开始解码
        if (curKeyframe_) needsKeyframe_ = false;
        ++stats_.framesDelivered;
        r.delivered = true;
        return r;
    }

private:
    void BeginFrame(const MediaPacket& p)
    {
        curFrameId_ = p.frameId;
        curTimestampUs_ = p.timestampUs;
        curKeyframe_ = (p.flags & kFlagKeyframe) != 0;
        curFrameBytes_ = p.totalFrameBytes;
        // 【只按 totalFrameBytes 推导数据分片数】两端算法一致，最可靠。
        // 早先用 fragCount 推导是错的：校验包存在时 fragCount = 数据分片+1，
        // 数据包里也带着这个值，会把分片数算多一片，导致永远凑不齐。
        dataFragCount_ = DataFragmentCount(p.totalFrameBytes);
        frame_.assign(p.totalFrameBytes, 0);
        have_.assign(dataFragCount_, false);
        haveCount_ = 0;
        parity_.clear();
        parityHave_ = false;
        assembling_ = true;
    }

    void RecoverWithParity(uint16_t missing)
    {
        if (parity_.empty()) return;
        const size_t missingBytes = FragmentBytes((uint32_t)frame_.size(), missing);
        if (missingBytes == 0) return;

        // XOR 所有已收到的数据分片（不足 1200 字节的按 0 补齐），再异或校验分片
        std::vector<uint8_t> acc(kMaxPayload, 0);
        for (uint16_t i = 0; i < dataFragCount_; ++i) {
            if (i == missing || !have_[i]) continue;
            const size_t off = (size_t)i * kMaxPayload;
            const size_t n = FragmentBytes((uint32_t)frame_.size(), i);
            for (size_t k = 0; k < n; ++k) acc[k] ^= frame_[off + k];
        }
        const size_t pn = parity_.size() < kMaxPayload ? parity_.size() : kMaxPayload;
        for (size_t k = 0; k < pn; ++k) acc[k] ^= parity_[k];

        const size_t off = (size_t)missing * kMaxPayload;
        memcpy(frame_.data() + off, acc.data(), missingBytes);
        have_[missing] = true;
        ++haveCount_;
    }

    // 当前正在组装的帧
    uint32_t curFrameId_ = 0;
    uint64_t curTimestampUs_ = 0;
    bool     curKeyframe_ = false;
    uint32_t curFrameBytes_ = 0;
    uint16_t dataFragCount_ = 0;
    std::vector<uint8_t> frame_;
    std::vector<bool> have_;
    size_t   haveCount_ = 0;
    std::vector<uint8_t> parity_;
    bool     parityHave_ = false;
    bool     assembling_ = false;

    // 是否有未恢复的缺口
    bool     needsKeyframe_ = false;
    uint32_t expectedFrameId_ = 0;
    bool     haveExpected_ = false;
    uint16_t sessionId_ = 0;        // 当前会话（由 OnPacket 维护，Reset 不清）
    bool     haveSession_ = false;

    Stats stats_{};
};

// ---------------------------------------------------------------------------
// 分片发送辅助：把一整帧切成若干个 MediaPacket 写进 out
// 返回实际生成的包数
// ---------------------------------------------------------------------------
inline uint16_t FragmentFrame(uint8_t* out, size_t outCap, size_t* outUsed,
                              uint32_t frameId, uint8_t streamId, uint16_t sessionId,
                              uint8_t baseFlags, uint64_t timestampUs, const uint8_t* frame,
                              uint32_t frameBytes, bool parity, size_t* outSizes)
{
    const uint16_t dataCount = DataFragmentCount(frameBytes);
    const bool useParity = parity && dataCount > 1;
    const uint16_t total = useParity ? (uint16_t)(dataCount + 1) : dataCount;

    std::vector<uint8_t> par;
    if (useParity) par.assign(kMaxPayload, 0);

    uint16_t written = 0;
    size_t used = 0;
    for (uint16_t i = 0; i < dataCount; ++i) {
        const size_t off = (size_t)i * kMaxPayload;
        const size_t n = FragmentBytes(frameBytes, i);
        if (used + kHeaderSize + n > outCap) break;

        MediaPacket p{};
        p.magic = kMagic; p.version = kVersion;
        p.type = kTypeMedia;
        p.flags = baseFlags;
        p.streamId = streamId;
        p.sessionId = sessionId;
        p.frameId = frameId;
        p.fragIndex = i;
        p.fragCount = total;
        p.timestampUs = timestampUs;
        p.totalFrameBytes = frameBytes;
        p.payloadSize = (uint16_t)n;

        uint8_t* dst = out + used;
        memcpy(dst, &p, kHeaderSize);
        memcpy(dst + kHeaderSize, frame + off, n);
        outSizes[written] = kHeaderSize + n;
        used += kHeaderSize + n;
        ++written;

        if (useParity)
            for (size_t k = 0; k < n; ++k) par[k] ^= frame[off + k];
    }

    if (useParity && written == dataCount) {
        MediaPacket p{};
        p.magic = kMagic; p.version = kVersion;
        p.type = kTypeMedia;
        p.flags = (uint8_t)(baseFlags | kFlagParity);
        p.streamId = streamId;
        p.sessionId = sessionId;
        p.frameId = frameId;
        p.fragIndex = dataCount;
        p.fragCount = total;
        p.timestampUs = timestampUs;
        p.totalFrameBytes = frameBytes;
        p.payloadSize = (uint16_t)kMaxPayload;

        uint8_t* dst = out + used;
        memcpy(dst, &p, kHeaderSize);
        memcpy(dst + kHeaderSize, par.data(), kMaxPayload);
        outSizes[written] = kHeaderSize + kMaxPayload;
        used += kHeaderSize + kMaxPayload;
        ++written;
    }

    if (outUsed) *outUsed = used;
    return written;
}

} // namespace zx
