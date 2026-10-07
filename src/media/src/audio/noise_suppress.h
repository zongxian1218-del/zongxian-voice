// audio/noise_suppress.h —— 降噪（用户点名的功能）
//
// 算法：STFT 谱减（spectral subtraction）+ 自适应噪声底跟踪。
//
// 两个关于数字的硬约束，改之前先想清楚：
//   1) 完美重建要求跳步 hop == 帧长/2（50% 重叠）且窗是周期性 Hann
//      （分母用 N 而不是 N-1）。此时 Σ w² 恒等于 1.5，除以 1.5 就无损。
//      所以 kFft=512 ⇒ kHop=256，不能取 240。
//   2) STFT 的 256 跳步与调用方的 480 样本块**不是整数倍关系**，必须解耦：
//      内部维护输入累积器与输出暂存环，外部永远按它要的块长拿数据。
//      把这两个节拍强行对齐是这类代码最常见的坑。
//
// 为什么先做谱减而不是直接上神经网络：
//   · 零依赖。第一步先把「可验证的降噪」跑通，有了 raw/processed 双路录音
//     这个客观对照物，后面换算法才有比较基准。
//   · 谱减在稳态噪声（风扇、空调、电流声）上效果明确且可预测，
//     而这正是用户说「降噪」时心里指的东西。
//   · backend 切换点已经留好：换 RNNoise 只是换 ProcessFrame 的实现，
//     路由、计量、录音、A/B 对照全部不用动。
//
// 已知局限（如实写，不粉饰）：
//   · 对非稳态噪声（键盘敲击、纸张翻动、脚步）弱于神经网络方法。
//   · 强档有轻微「水声」（谱减的 musical noise），所以默认给中档。
//   · 不处理回声。AEC 需要播放侧参考信号，是 PL1 的活，与降噪是两件事。
#pragma once

#include <complex>
#include <cstdint>
#include <vector>

namespace zx {

// 与 C ABI 的 zx_ns_level / zx_ns_backend 数值一一对应（有静态断言把关）
enum class NsLevel : int { Off = 0, Light = 1, Moderate = 2, Strong = 3 };
enum class NsBackend : int { Builtin = 0, RnNoise = 1 };

// RNNoise 后端**本仓库没有实现**（没有源码、没有链接）。构建时若真把它接进来，
// 定义 ZX_HAVE_RNNOISE=1（例如 CMake 里 add_compile_definitions）。
// 未定义时 media_api 的 zx_ns_set 会明确返回 ZX_ERR_UNSUPPORTED，
// zx_ns_get_config 也会如实报 available=false —— 不许再"静默降级"，
// 那正是审计 §四把 ZX_NSB_RNNOISE 记成假开关的原因。
#ifndef ZX_HAVE_RNNOISE
#  define ZX_HAVE_RNNOISE 0
#endif

struct NsStats {
    double noise_floor_dbfs = -120.0;  // 估计的噪声底（dBFS）
    double suppression_db = 0.0;       // 噪声段被压掉多少 dB（客观证明有效）
    double speech_preservation = 1.0;  // 人声段保留比例，1.0 = 完全不损伤
    double last_frame_rms_dbfs = -120.0;
    bool   voiced = false;             // 能量法 VAD
    uint64_t frames_processed = 0;
};

class NoiseSuppressor {
public:
    // channels: 生产路径**恒为 1** —— media_api.cpp 先把多声道下混成单声道
    // 再送进来（见 zx_source_read 里的 DownmixToMono/UpmixFromMono）。
    // 传 2 只在单测里用过（tools/nstest.cpp 的立体声用例）。
    // 两声道共用同一套增益，不做独立声道处理 —— 那会破坏立体声像，得不偿失。
    explicit NoiseSuppressor(int channels);

    // 预热：提前建好 FFT 表、窗、缓冲，让音频线程全程无内存分配。
    // 必须在 start() 之前调用。
    void WarmUp();

    // 重配。返回实际生效的档位（后端不可用时会降级）。
    NsLevel Configure(NsLevel level, NsBackend backend, bool rnnoise_available);

    void SetBypass(bool on) { bypass_ = on; }
    bool Bypass() const { return bypass_; }

    // 测试专用：把所有格的增益强制为 1.0，只保留 STFT 分析与重建。
    // 存在的理由：重建归一化（kColaScale）与谱减压制是两件独立的事，
    // 混在一起测就无法判断测到的 -8.6 dB 到底是"定标错了"还是"压制太狠"。
    // 打开这个开关后，重建增益应当精确等于 1.0 —— 不满足就是定标错了。
    void SetUnityGainForTesting(bool on) { unity_gain_test_ = on; }

    // 就地或异地处理。in/out 可以是同一块内存，也可以不同（录音要同时拿两份）。
    // 恒等映射：无论内部跳步是多少，out 与 in 的帧数一定相同。
    void Process(const float* in, float* out, int frames);

    const NsStats& stats() const { return stats_; }
    NsLevel level() const { return level_; }
    NsBackend backend() const { return backend_; }
    int channels() const { return channels_; }

private:
    void ProcessFrame(const float* frame, float* out_frame);

    int channels_ = 1;
    NsLevel level_ = NsLevel::Moderate;
    NsBackend backend_ = NsBackend::Builtin;
    bool bypass_ = false;
    bool unity_gain_test_ = false;   // 仅测试用，见 SetUnityGainForTesting

    // 【2026-10-06 删除】这里原来有一套"内存哨兵"：InstallSentinels() 只把
    // sentinel_installed_ 置 true，SentinelCheck() 是个空函数（(void)where;），
    // guard 数组从没被写过也没被校验过 —— 审计 §四 记的假开关之一。
    // 真正在干活的是 Process 里的 WSet()（Debug 下带下标检查，越界立刻 abort），
    // 以及调用方按「帧×声道」开缓冲的约定。留着空壳只会让人以为有防护。

    // ---- STFT 常量 ----
    static constexpr int kFft = 512;
    static constexpr int kHop = kFft / 2;      // 256；50% 重叠是完美重建的前提
    static constexpr int kBins = kFft / 2 + 1;

    // 重叠相加的定标系数。
    //
    // 这个值不能靠"看起来合理"来填 —— 填错不会崩、不会报错，只会让
    // 降噪后的声音整体变小或变大。必须同时抵消三个因素：
    //   1) IFFT 的 1/N
    //   2) 正弦在正负频率各分一半造成的 √2 幅度差
    //   3) 周期性 Hann 窗相干和 N/2 与 COLA 归一化 1.5 的组合
    // 解析式：(4/3)·√2 / 1.5 ≈ 1.25676
    //
    // 实测依据（tools/nstest.cpp 的「纯重建定标」测试）：把每格增益固定为
    // 1.0 后，重建增益必须 ≈ 1.0。该测试会把这个常数钉死。
    //
    // 历史教训：早期版本这里写的是 1/1.5（把"两个重叠窗逐点之和"误当成
    // 平方和），实测纯重建增益 0.5305（-5.5 dB），听感是"降噪后人声发闷、
    // 变小"，而所有日志都显示一切正常 —— 所以必须有客观测量兜底。
    static constexpr float kColaScale = 1.25676f;

    // ---- 输入侧累积 ----
    // 只保留分析窗、重叠相加累积器、单跳步输出三个缓冲。
    // 刻意**不再**保留"输出暂存"：早期版本用暂存把 STFT 跳步与调用方块长解耦，
    // 结果引入了一个"一次调用可能产出多于输入帧数"的越界写（写坏调用方缓冲），
    // 以及"结果进了暂存却忘了写回"导致输出全 NaN 的问题。
    // 现在的 Process 按跳步就地写出，结构上不可能出现这两类错误。
    std::vector<float> in_win_;      // 长度 kFft，滑动分析窗
    std::vector<float> out_acc_;     // 长度 kFft，重叠相加累积器
    std::vector<float> out_ready_;   // 长度 kHop，单跳步输出
    int out_ready_fill_ = 0;

    // ---- 频域工作区 ----
    std::vector<std::complex<float>> freq_;
    std::vector<float> mag_;
    std::vector<float> noise_;
    std::vector<float> gain_prev_;
    const float* win_ = nullptr;
    int win_len_ = 0;

    int frames_seen_ = 0;

    // ---- 统计（全部用 EMA，避免累积漂移）----
    double noise_in_lin_ = 0.0;
    double noise_out_lin_ = 0.0;
    double voiced_in_lin_ = 0.0;
    double voiced_out_lin_ = 0.0;
    double speech_energy_ = 0.0;
    NsStats stats_;
};

}  // namespace zx
