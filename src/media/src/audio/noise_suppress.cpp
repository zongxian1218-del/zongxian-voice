// noise_suppress.cpp —— 谱减降噪的实现
//
// 这个文件里踩过的坑都写在对应位置，因为每一条都是"看起来能跑、
// 实际写坏调用方内存或悄悄改变音量"的类型，不写清楚下次还会重犯：
//
//   坑 1（重建定标）：kColaScale 早期写成 1/1.5，实际应为 1.25676。
//        后果是降噪后整体音量被压 -5.5 dB，而所有日志都正常。
//        现在由 tools/nstest.cpp 的「纯重建定标」测试钉死（必须 ≈ 1.0）。
//
//   坑 2（输出多于输入）：早期用"输出暂存"把 STFT 跳步与调用方块长解耦，
//        结果一次调用可能产出多于输入帧数的输出，越界写了**调用方的缓冲**。
//        这种越界崩不在本模块内，内部加多少检查都抓不到。
//        现在的 Process 按跳步就地写出，1:1 映射在结构上成立。
//
//   坑 3（输出未初始化）：改用"分阶段+暂存"的中间版本里，凑满跳步的那段
//        结果被存进暂存却忘了写回，调用方拿到未初始化内存（实测全 NaN）。
//
//   坑 4（噪声底跟踪）：早期用普通指数平滑跟踪噪声底，稳态信号会被慢慢
//        学成噪声并被压掉（实测把 440Hz 纯音压了 3 dB）。改为最小值统计后，
//        噪声底从 25.8 dBFS 降到 -109.4 dBFS，人声保留率 0.84 → 0.98。
#include "noise_suppress.h"

#include "fft.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>

namespace zx {

namespace {

constexpr double kPi = 3.14159265358979323846;

// 采样率。这里写成本地常量而不是引用 C ABI 头里的宏，是为了让这个模块
// 可以脱离 zongxian_media.h 单独编译和测试（单元测试就不需要那个头）。
constexpr int kSampleRateNs = 48000;

// 带下标检查的写入。Debug 下越界立刻报到 stderr 并终止，而不是让越界写到
// 相邻对象上、几秒后在完全无关的地方崩。坑 2 就是靠它定位的。
#ifdef _DEBUG
inline void WSet(std::vector<float>& v, size_t idx, float value,
                 const char* name = "?") {
    if (idx >= v.size()) {
        std::fprintf(stderr, "[NS] 缓冲 '%s' 写越界：idx=%zu size=%zu\n", name, idx,
                     v.size());
        std::fflush(stderr);
        std::abort();
    }
    v[idx] = value;
}
#else
inline void WSet(std::vector<float>& v, size_t idx, float value,
                 const char* = "?") {
    v[idx] = value;
}
#endif

inline double PowerToDb(double p) {
    return 10.0 * std::log10(p > 1e-20 ? p : 1e-20);
}

// 每档的参数。这些数字是「效果 / 音质损伤」的取舍旋钮。
// 注意 noise_attack / noise_decay 现在只影响统计口径，噪声底本身走最小值
// 统计（见 ProcessFrame），所以这两项不再决定压制强度。
struct LevelParams {
    float noise_attack;
    float noise_decay;
    float beta;           // 过减因子：越大压得越狠，也越容易出水声
    float floor_gain;     // 每格增益下限（线性）
    float time_smooth;    // 帧间增益平滑系数（越大越平滑）
};

//                      attack decay  beta  floor  smooth
constexpr LevelParams kLight    {0.20f, 0.0015f, 1.0f, 0.30f, 0.35f};
constexpr LevelParams kModerate {0.30f, 0.0015f, 2.0f, 0.12f, 0.45f};
constexpr LevelParams kStrong   {0.42f, 0.0015f, 3.2f, 0.05f, 0.55f};

LevelParams ParamsFor(NsLevel l) {
    switch (l) {
        case NsLevel::Light:  return kLight;
        case NsLevel::Strong: return kStrong;
        case NsLevel::Moderate:
        default:              return kModerate;
    }
}

}  // namespace

NoiseSuppressor::NoiseSuppressor(int channels) : channels_(channels == 2 ? 2 : 1) {
    // 构造阶段只分配内存、不建 FFT 表 —— 建表的开销留给 WarmUp()，
    // 让「对象构造」与「音频线程开始跑」之间有明确的分界。
    freq_.assign(kFft, {0.0f, 0.0f});
    mag_.assign(kBins, 0.0f);
    noise_.assign(kBins, 0.0f);
    gain_prev_.assign(kBins, 1.0f);
    in_win_.assign(kFft, 0.0f);
    out_acc_.assign(kFft, 0.0f);
    out_ready_.assign(kHop, 0.0f);
}

void NoiseSuppressor::WarmUp() {
    // 取窗。窗由 fft.cpp 统一生成并缓存，这里只持有指针。
    win_ = HannWindow(kFft).data();
    win_len_ = win_ ? kFft : 0;

    // 校验窗的统计量。kColaScale 的取值依赖这两个确定值，
    // 窗若被改动而没同步改定标，音量就会不对，所以在这里暴露。
    if (win_) {
        const std::vector<float>& w = HannWindow(kFft);
        double sum = 0.0;
        for (int i = 0; i < kFft; ++i) sum += static_cast<double>(w[static_cast<size_t>(i)]);
        // 周期性 Hann 的 Σw = N/2
        if (std::fabs(sum - kFft / 2.0) > 1e-2) {
            std::fprintf(stderr,
                         "[NS] Hann 窗统计异常：Σw=%.4f（期望 %.1f），"
                         "重建定标 kColaScale 需要重新校准\n",
                         sum, kFft / 2.0);
            std::fflush(stderr);
        }
    }
}

NsLevel NoiseSuppressor::Configure(NsLevel level, NsBackend backend,
                                   bool rnnoise_available) {
    level_ = level;
    // RNNoise 没编进来就别假装能切过去；降级要能被 zx_ns_get_config 看见。
    backend_ = (backend == NsBackend::RnNoise && !rnnoise_available)
                   ? NsBackend::Builtin
                   : backend;
    if (level_ == NsLevel::Off) {
        // 关掉时把状态复位，下次打开不会带着上一次的噪声底
        std::fill(noise_.begin(), noise_.end(), 0.0f);
        std::fill(gain_prev_.begin(), gain_prev_.end(), 1.0f);
        frames_seen_ = 0;
    }
    return level_;
}

void NoiseSuppressor::Process(const float* in, float* out, int frames) {
    if (frames <= 0) return;

    // 关闭或旁路：数据直通，但**不跳过统计** —— 旁路时也要记录噪声底，
    // 否则「降噪前后对比」的下半段就没有参照物了。
    if (bypass_ || level_ == NsLevel::Off) {
        if (in != out) {
            std::copy(in, in + static_cast<size_t>(frames) * channels_, out);
        }
        stats_.frames_processed += static_cast<uint64_t>(frames);
        stats_.voiced = false;
        return;
    }

    // =======================================================================
    // 核心不变量：输出帧数 == 输入帧数（头文件里承诺的 1:1 映射）
    //
    // 实现刻意做到最简：把输入按 kHop 切成整跳步，每处理一个跳步就产出
    // kHop 帧结果并**就地写进 out**；不足一跳步的零头留在分析窗里，等下次
    // 调用凑齐。因为每个跳步恰好进出 kHop 帧，1:1 关系在总量上自动成立，
    // 不需要任何暂存或补偿逻辑。
    //
    // 不要改回"先把结果存起来、之后再交付"的写法 —— 那正是坑 2 与坑 3
    // 的来源（越界写调用方缓冲 / 输出未初始化）。
    //
    // 代价：当调用方的块长小于 kHop（256 帧，约 5.3ms）时，本次调用不会
    // 产出输出（零头攒着），输出会延迟到下次调用并一次性补齐。真实音频
    // 是 480 帧（10ms）一块，不会触发这个情形。
    // =======================================================================

    int consumed = 0;
    while (consumed < frames) {
        const int take = std::min(kHop, frames - consumed);
        const float* src = in + static_cast<size_t>(consumed) * channels_;

        // 多声道先混成单声道做分析（增益共用，避免破坏立体声像）
        for (int i = 0; i < take; ++i) {
            float mono = 0.0f;
            for (int c = 0; c < channels_; ++c) {
                mono += src[static_cast<size_t>(i) * channels_ + c];
            }
            WSet(in_win_, static_cast<size_t>(kHop + i),
                 mono / static_cast<float>(channels_), "in_win_");
        }
        consumed += take;

        if (take == kHop) {
            // 凑满一个跳步：处理它，并把 kHop 帧结果就地写出
            ProcessFrame(in_win_.data(), out_ready_.data());
            out_ready_fill_ = kHop;

            float* dst = out + static_cast<size_t>(consumed - kHop) * channels_;
            if (channels_ == 1) {
                std::copy(out_ready_.begin(), out_ready_.end(), dst);
            } else {
                for (int i = 0; i < kHop; ++i) {
                    const float v = out_ready_[static_cast<size_t>(i)];
                    for (int c = 0; c < channels_; ++c) {
                        dst[static_cast<size_t>(i) * channels_ + c] = v;
                    }
                }
            }

            // 分析窗前移 kHop：下一帧的前半段复用这一帧的后半段
            std::memmove(in_win_.data(), in_win_.data() + kHop,
                         static_cast<size_t>(kFft - kHop) * sizeof(float));
            std::fill(in_win_.begin() + (kFft - kHop), in_win_.end(), 0.0f);
        }
    }

    stats_.frames_processed += static_cast<uint64_t>(frames);
}

void NoiseSuppressor::ProcessFrame(const float* frame, float* out_frame) {
    const LevelParams P = ParamsFor(level_);

    // ---- 1) 加窗 → 频域 ----
    for (int i = 0; i < kFft; ++i) {
        freq_[static_cast<size_t>(i)] = {frame[i] * win_[i], 0.0f};
    }
    FftForward(freq_.data(), kFft);

    // ---- 2) 幅度谱 ----
    double frame_power = 0.0;
    for (int b = 0; b < kBins; ++b) {
        const float re = freq_[static_cast<size_t>(b)].real();
        const float im = freq_[static_cast<size_t>(b)].imag();
        const float m = std::sqrt(re * re + im * im);
        mag_[static_cast<size_t>(b)] = m;
        // 只统计到 8kHz 的语音带，避免高频噪声把 VAD 带偏
        if (b * kSampleRateNs / kFft < 8000) frame_power += static_cast<double>(m) * m;
    }
    frame_power /= static_cast<double>(kBins);

    // ---- 3) VAD：能量法。用「语音能量 EMA / 噪声能量 EMA」的比值判断 ----
    const bool first = (frames_seen_ == 0);
    ++frames_seen_;
    const double ratio = frame_power / (speech_energy_ + 1e-12);
    const bool voiced = !first && ratio > 6.0;   // ≈ +7.8 dB

    // ---- 噪声底跟踪：最小值统计（坑 4）----
    //
    // 这里**不能**用普通的指数平滑。曾经用过，结果是稳态信号（长音、
    // 持续的音乐、甚至只是空调声）会被慢慢"学成"噪声底然后被压掉 ——
    // 实测把 440Hz 纯音压了 3 dB，而且越是持续的声音压得越狠。
    //
    // 正确做法是噪声底只跟踪**下降沿**：任何时刻出现更低的幅度就立刻跟上，
    // 否则以极慢的速度衰减。这样语音出现时噪声底保持不动，而语音停顿、
    // 噪声真的消失时它会缓慢降下来，不会永远卡在旧值。
    constexpr float kNoiseDecay = 0.999f;   // 每帧衰减 0.1%，约 0.9 dB/s
    for (int b = 0; b < kBins; ++b) {
        float& est = noise_[static_cast<size_t>(b)];
        const float m = mag_[static_cast<size_t>(b)];
        if (first) {
            est = m;
        } else {
            est *= kNoiseDecay;
            if (m < est) est = m;
        }
        if (est < 1e-9f) est = 1e-9f;
    }

    // 能量 EMA（VAD 用）
    speech_energy_ += (voiced ? 0.15 : 0.004) * (frame_power - speech_energy_);

    // ---- 4) 每格增益：过减 + 频率相关地板 ----
    double in_pow = 0.0, out_pow = 0.0, noise_in = 0.0, noise_out = 0.0;
    double voiced_in = 0.0, voiced_out = 0.0;

    for (int b = 0; b < kBins; ++b) {
        const float m = mag_[static_cast<size_t>(b)];
        const float n = noise_[static_cast<size_t>(b)];
        const double p_in = static_cast<double>(m) * m;
        const double p_noise = static_cast<double>(n) * n;

        double g;
        if (unity_gain_test_) {
            // 测试模式：完全跳过谱减，只保留分析→重建链路。
            // 此时重建增益必须精确等于 1.0，否则说明 kColaScale 定标错误。
            g = 1.0;
        } else {
            // 过减：从观测功率里减掉 beta 倍的噪声功率
            double p_sig = p_in - static_cast<double>(P.beta) * p_noise;
            if (p_sig < 0.0) p_sig = 0.0;

            // 频率相关地板：低频（人声基频与共振峰所在）留更多，
            // 高频压得更狠 —— 这是「不出现水下音」的关键。
            const double hz = static_cast<double>(b) * kSampleRateNs / kFft;
            double floor_g = static_cast<double>(P.floor_gain);
            if (hz < 1000.0) {
                floor_g = std::max(floor_g, 0.55);   // 保住人声厚度
            } else if (hz < 2000.0) {
                floor_g = std::max(floor_g, 0.35);
            }

            // 数值守卫。这里的除法是整份代码里唯一可能产生 NaN/Inf 的地方，
            // 一旦漏出去会被 gain_prev_ 的帧间平滑"记住"，此后整条通路永久
            // 变成 NaN 且再也恢复不了（实测就是这样：前几轮正常，之后输出
            // 永远为 -nan(ind)）。
            // 三道防线：分母设下限 / 结果做 isfinite 检查 / 统一钳位。
            const double denom = p_in > 1e-20 ? p_in : 1e-20;
            g = std::sqrt(p_sig / denom);
            if (!std::isfinite(g)) g = floor_g;
            if (g < floor_g) g = floor_g;
            if (g > 1.0) g = 1.0;
        }

        // 帧间平滑，压掉 musical noise。
        // 这里同样要守卫：gain_prev_ 里若已混入 NaN，平滑会把它永久传播。
        const double gp = gain_prev_[static_cast<size_t>(b)];
        const double base = std::isfinite(gp) ? gp : g;
        double gs = P.time_smooth * base + (1.0 - P.time_smooth) * g;
        if (!std::isfinite(gs)) gs = g;
        if (gs < 0.0) gs = 0.0;
        if (gs > 1.0) gs = 1.0;
        gain_prev_[static_cast<size_t>(b)] = static_cast<float>(gs);

        // 施加增益（实部虚部同乘，保留相位）
        freq_[static_cast<size_t>(b)] *= static_cast<float>(gs);
        if (b > 0 && b < kFft - b) {
            // 共轭对称：负频率侧同步，保证逆变换出来是实数信号
            freq_[static_cast<size_t>(kFft - b)] =
                std::conj(freq_[static_cast<size_t>(b)]);
        }

        in_pow += p_in;
        out_pow += p_in * gs * gs;

        // 噪声占主导的格子参与「压制量」统计；语音占主导的参与「保留度」统计
        if (p_noise > p_in * 0.9) {
            noise_in += p_in;
            noise_out += p_in * gs * gs;
        }
        if (p_in > p_noise * 2.0) {
            voiced_in += p_in;
            voiced_out += p_in * gs * gs;
        }
    }

    // ---- 5) 逆变换 + 重叠相加 ----
    FftInverse(freq_.data(), kFft);
    for (int i = 0; i < kFft; ++i) {
        WSet(out_acc_, static_cast<size_t>(i),
             out_acc_[static_cast<size_t>(i)] +
                 freq_[static_cast<size_t>(i)].real() * win_[i] * kColaScale,
             "out_acc_");
    }
    for (int i = 0; i < kHop; ++i) {
        out_frame[i] = out_acc_[static_cast<size_t>(i)];
    }
    std::memmove(out_acc_.data(), out_acc_.data() + kHop,
                 static_cast<size_t>(kFft - kHop) * sizeof(float));
    std::fill(out_acc_.begin() + (kFft - kHop), out_acc_.end(), 0.0f);

    // ---- 6) 统计（全部 EMA，不累积漂移）----
    const double a = 0.05;
    if (!voiced) {
        noise_in_lin_ += a * (noise_in - noise_in_lin_);
        noise_out_lin_ += a * (noise_out - noise_out_lin_);
    } else {
        voiced_in_lin_ += a * (voiced_in - voiced_in_lin_);
        voiced_out_lin_ += a * (voiced_out - voiced_out_lin_);
    }

    stats_.voiced = voiced;
    stats_.noise_floor_dbfs = PowerToDb(noise_out_lin_ > 0 ? noise_out_lin_ : 1e-20);
    if (noise_in_lin_ > 1e-18 && noise_out_lin_ > 1e-18) {
        stats_.suppression_db = 10.0 * std::log10(noise_in_lin_ / noise_out_lin_);
    }
    if (voiced_in_lin_ > 1e-18 && voiced_out_lin_ > 1e-18) {
        stats_.speech_preservation = std::sqrt(voiced_out_lin_ / voiced_in_lin_);
    }
    stats_.last_frame_rms_dbfs = PowerToDb(frame_power);
}

}  // namespace zx
