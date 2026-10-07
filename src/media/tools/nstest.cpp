// tools/nstest.cpp —— 降噪模块的最小单元测试
//
// 存在的理由：立体声 + 降噪在集成测试里表现为访问冲突，但崩溃点离真正
// 的原因很远。把它从设备、线程、COM 里彻底剥离出来单独跑，几秒钟就能
// 定位到底哪一步越界 —— 比在整体流程里加日志猜要快一个数量级。
//
// 这个测试同时是**回归测试**：任何一次改动后都应该先跑它，再跑设备测试。
//
// 用法：nstest
//   全部通过返回 0；任何一项失败返回 1。
#include "audio/fft.h"
#include "audio/noise_suppress.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <random>
#include <vector>

using namespace zx;

namespace {

int g_failures = 0;

void Check(bool ok, const char* what) {
    std::printf("  [%s] %s\n", ok ? "通过" : "失败", what);
    if (!ok) ++g_failures;
}

// 造一段测试信号：白噪声 + 一个 440Hz 正弦（模拟人声）。
void FillSignal(std::vector<float>& buf, int frames, int channels, unsigned seed) {
    std::mt19937 rng(seed);
    std::normal_distribution<float> noise(0.0f, 0.02f);
    for (int i = 0; i < frames; ++i) {
        const float tone = 0.3f * std::sin(2.0f * 3.14159265f * 440.0f * i / 48000.0f);
        for (int c = 0; c < channels; ++c) {
            buf[static_cast<size_t>(i) * channels + c] = tone + noise(rng);
        }
    }
}

// 用各种块长压一个降噪实例。块长组合刻意包含"与 STFT 跳步不成整数倍"的
// 那些值 —— 输出暂存机制就是为它们写的，也是最容易越界的地方。
void StressChannels(int channels) {
    std::printf("\n=== channels=%d ===\n", channels);
    NoiseSuppressor ns(channels);
    ns.WarmUp();
    ns.Configure(NsLevel::Moderate, NsBackend::Builtin, false);

    const int block_sizes[] = {1, 7, 100, 240, 256, 479, 480, 481, 512, 960, 9600};
    for (int block : block_sizes) {
        std::vector<float> in(static_cast<size_t>(block) * channels);
        std::vector<float> out(static_cast<size_t>(block) * channels);
        // 反复喂，让内部 STFT 状态真正滚动起来（只喂一次测不出问题）
        for (int iter = 0; iter < 20; ++iter) {
            FillSignal(in, block, channels, static_cast<unsigned>(iter * 31 + block));
            std::memset(out.data(), 0, out.size() * sizeof(float));
            ns.Process(in.data(), out.data(), block);
        }
        // 检查输出：没有 NaN/Inf 是硬要求。
        // "必须全有输出"只在块长足够时才要求 —— STFT 的跳步是 256 帧，
        // 当单次调用的块长远小于它时，前若干次调用还没有凑满一帧，
        // 输出暂存里自然是空的（这属于冷启动延迟，不是错误）。
        // 真实音频按 480 帧/块喂进来，不会落在这个区间。
        bool any_nonzero = false, any_bad = false;
        for (float v : out) {
            if (v != 0.0f) any_nonzero = true;
            if (!std::isfinite(v)) any_bad = true;
        }
        const bool require_output = block >= 256;
        char label[160];
        if (require_output) {
            std::snprintf(label, sizeof(label),
                          "块长 %5d 帧：有输出=%s 无 NaN/Inf=%s", block,
                          any_nonzero ? "是" : "否", any_bad ? "否" : "是");
            Check(any_nonzero && !any_bad, label);
        } else {
            // 小块的冷启动：只要求数值健全，不要求当次就有输出
            std::snprintf(label, sizeof(label),
                          "块长 %5d 帧：数值健全=%s（小块允许冷启动无输出）", block,
                          any_bad ? "否" : "是");
            Check(!any_bad, label);
        }
    }

    const NsStats& st = ns.stats();
    std::printf("  统计：噪声底 %.1f dBFS  压制 %.1f dB  人声保留 %.3f\n",
                st.noise_floor_dbfs, st.suppression_db, st.speech_preservation);
}

// 验证 STFT 重建是否无损：把降噪关到"每格增益恒为 1"的情形无法直接构造，
// 所以改为验证 FFT 往返（正变换 + 逆变换应还原原信号）。
void TestFftRoundtrip() {
    std::printf("\n=== FFT 往返 ===\n");
    for (int n : {512, 1024}) {
        std::vector<std::complex<float>> buf(static_cast<size_t>(n));
        std::vector<float> orig(static_cast<size_t>(n));
        std::mt19937 rng(1234);
        std::normal_distribution<float> dist(0.0f, 1.0f);
        for (int i = 0; i < n; ++i) {
            orig[static_cast<size_t>(i)] = dist(rng);
            buf[static_cast<size_t>(i)] = {orig[static_cast<size_t>(i)], 0.0f};
        }
        FftForward(buf.data(), n);
        FftInverse(buf.data(), n);
        double max_err = 0.0;
        for (int i = 0; i < n; ++i) {
            max_err = std::max(max_err,
                               std::fabs(static_cast<double>(buf[static_cast<size_t>(i)].real()) -
                                         orig[static_cast<size_t>(i)]));
        }
        char label[128];
        std::snprintf(label, sizeof(label), "n=%d 最大误差 %.3e（应 < 1e-4）", n, max_err);
        Check(max_err < 1e-4, label);
    }
}

// 验证 Hann 窗的 COLA 条件：50% 重叠时 w[i] + w[i+hop] 必须恒为 1。
// 这是"降噪后音量不变、无周期性起伏"的数学前提。
void TestCola() {
    std::printf("\n=== Hann 窗 COLA ===\n");
    const int n = 512, hop = 256;
    const std::vector<float>& w = HannWindow(n);

    // 先做基本健全性检查：win 必须是长度 512 的窗函数，取值在 [0,1]。
    // 数学上 Σw² 必然等于 1.5（Hann 窗的确定值），
    // 一旦出现 192 这种"整数"结果，说明 window 数据本身已被写坏 ——
    // 这类现象在集成测试里表现为访问冲突，极难归因，所以在这里就直接暴露。
    std::printf("  win.size=%zu  w[0]=%.6f  w[128]=%.6f  w[256]=%.6f  w[511]=%.6f\n",
                w.size(), w.empty() ? -1.0f : w[0], w.size() > 128 ? w[128] : -1.0f,
                w.size() > 256 ? w[256] : -1.0f,
                w.size() > 511 ? w[511] : -1.0f);
    double wmin = 1e9, wmax = -1e9;
    for (float v : w) {
        wmin = std::min(wmin, static_cast<double>(v));
        wmax = std::max(wmax, static_cast<double>(v));
    }
    std::printf("  win min=%.6f max=%.6f（应分别为 0 与 1）\n", wmin, wmax);
    Check(w.size() == static_cast<size_t>(n), "win 长度为 512");
    Check(wmin >= -1e-6 && wmax <= 1.0 + 1e-6, "win 取值都在 [0,1]");

    double max_dev = 0.0;
    for (int i = 0; i < hop; ++i) {
        const double sum = static_cast<double>(w[static_cast<size_t>(i)]) +
                           w[static_cast<size_t>(i + hop)];
        max_dev = std::max(max_dev, std::fabs(sum - 1.0));
    }
    char label[160];
    std::snprintf(label, sizeof(label),
                  "w[i]+w[i+hop]==1 最大偏差 %.3e（应 < 1e-5）", max_dev);
    Check(max_dev < 1e-5, label);

    // Σw² 的确定值：周期性 Hann 窗满足 Σ_{i=0}^{N-1} w² = 3N/8。
    // 这个常量曾经被写错成 1.5（那是"两个重叠窗逐点之和"的量级，
    // 不是平方和），导致 noise_suppress 里的重建定标差了 128 倍
    // （-42 dB）：表现是"降噪之后声音几乎听不见"，而且不报任何错。
    // 所以这里必须把理论值钉死，不能只检查"看起来合理"。
    double sum_sq = 0.0;
    double sum_w = 0.0;
    for (int i = 0; i < n; ++i) {
        const double v = static_cast<double>(w[static_cast<size_t>(i)]);
        sum_sq += v * v;
        sum_w += v;
    }
    const double expected_sum_sq = 3.0 * n / 8.0;   // 512 -> 192
    char label2[192];
    std::snprintf(label2, sizeof(label2), "Σw = %.4f（理论 N/2 = %.1f）",
                  sum_w, n / 2.0);
    Check(std::fabs(sum_w - n / 2.0) < 1e-2, label2);
    std::snprintf(label2, sizeof(label2), "Σw² = %.4f（理论 3N/8 = %.1f）",
                  sum_sq, expected_sum_sq);
    Check(std::fabs(sum_sq - expected_sum_sq) < 1e-2, label2);
}

// 重建增益测试 —— 降噪最容易出错、也最容易被忽略的一环。
//
// 目的：确认"经过降噪处理后，人声的响度基本不变"。
// 定标算错不会崩溃、不会报错，只会让声音变小或破音，
// 只能靠客观测量发现 —— 所以这个测试是必需的，不是可选的。
//
// 做法：喂一段 -20 dBFS 的 440Hz 纯音（信噪比很高，谱减几乎不压它），
//       先跑 2 秒让内部噪声底与增益平滑收敛，再统计 1 秒的输入输出功率。
void TestReconstructionGain() {
    std::printf("\n=== 重建增益（降噪不应改变人声响度）===\n");

    const int channels = 1;
    const int block = 480;
    const double freq = 440.0;
    const float amp = 0.1f;                 // -20 dBFS
    const double two_pi = 6.283185307179586;

    NoiseSuppressor ns(channels);
    ns.WarmUp();
    ns.Configure(NsLevel::Light, NsBackend::Builtin, false);

    std::vector<float> in(block), out(block);
    double in_energy = 0.0, out_energy = 0.0;
    const int warm_blocks = static_cast<int>(2.0 * 48000 / block);      // 收敛期
    const int measure_blocks = static_cast<int>(1.0 * 48000 / block);   // 统计期
    double phase = 0.0;

    for (int b = 0; b < warm_blocks + measure_blocks; ++b) {
        for (int i = 0; i < block; ++i) {
            in[static_cast<size_t>(i)] = amp * static_cast<float>(std::sin(phase));
            phase += two_pi * freq / 48000.0;
            if (phase > two_pi) phase -= two_pi;
        }
        ns.Process(in.data(), out.data(), block);

        if (b >= warm_blocks) {
            for (int i = 0; i < block; ++i) {
                const double x = in[static_cast<size_t>(i)];
                const double y = out[static_cast<size_t>(i)];
                in_energy += x * x;
                out_energy += y * y;
            }
        }
    }

    const double gain = std::sqrt(out_energy / (in_energy > 1e-30 ? in_energy : 1e-30));
    const double gain_db = 20.0 * std::log10(gain > 1e-9 ? gain : 1e-9);
    std::printf("  输入能量 %.6e  输出能量 %.6e\n", in_energy, out_energy);
    std::printf("  重建增益 = %.4f（%.2f dB）—— 应接近 1.0（0.00 dB）\n",
                gain, gain_db);

    // 容许 ±1.5 dB：谱减本身会动一点增益，但绝不该出现 -42 dB 那种量级
    char label[192];
    std::snprintf(label, sizeof(label), "重建增益在 ±1.5 dB 内（实测 %.2f dB）",
                  gain_db);
    Check(std::fabs(gain_db) < 1.5, label);
}

// 纯重建定标测试：把每格增益强制为 1.0，只验证 STFT 分析→合成链路。
//
// 为什么必须与上一个测试分开：
//   上一个测试开的是真实谱减，测到的增益同时包含"归一化误差"和"压制量"
//   两种成分。实测 -8.6 dB 时无法判断该改哪个。把增益固定为 1 之后，
//   剩下的偏差就百分之百是重建定标（kColaScale）的问题 —— 这才是能定值的东西。
//
// 判定：增益必须非常接近 1.0（±0.2 dB）。这是纯粹的数学校验，不该有容差余地。
void TestReconstructionScaleOnly() {
    std::printf("\n=== 纯重建定标（单位增益，只验证 STFT 分析→合成）===\n");
    std::printf("  [步骤] 构造 NoiseSuppressor(1)\n");

    const int block = 480;
    const double freq = 440.0;
    const float amp = 0.1f;
    const double two_pi = 6.283185307179586;

    NoiseSuppressor ns(1);
    std::printf("  [步骤] WarmUp\n");
    ns.WarmUp();
    std::printf("  [步骤] Configure\n");
    ns.Configure(NsLevel::Moderate, NsBackend::Builtin, false);
    ns.SetUnityGainForTesting(true);      // 关键：跳过谱减
    std::printf("  [步骤] 开始处理\n");

    std::vector<float> in(block), out(block);
    double in_energy = 0.0, out_energy = 0.0;
    const int warm_blocks = static_cast<int>(1.0 * 48000 / block);
    const int measure_blocks = static_cast<int>(1.0 * 48000 / block);
    double phase = 0.0;

    for (int b = 0; b < warm_blocks + measure_blocks; ++b) {
        for (int i = 0; i < block; ++i) {
            in[static_cast<size_t>(i)] = amp * static_cast<float>(std::sin(phase));
            phase += two_pi * freq / 48000.0;
            if (phase > two_pi) phase -= two_pi;
        }
        ns.Process(in.data(), out.data(), block);
        if (b >= warm_blocks) {
            for (int i = 0; i < block; ++i) {
                const double x = in[static_cast<size_t>(i)];
                const double y = out[static_cast<size_t>(i)];
                in_energy += x * x;
                out_energy += y * y;
            }
        }
    }

    const double gain = std::sqrt(out_energy / (in_energy > 1e-30 ? in_energy : 1e-30));
    const double gain_db = 20.0 * std::log10(gain > 1e-9 ? gain : 1e-9);
    // 反推 kColaScale 应该是多少：当前 kColaScale=1/1.5 得到 gain，
    // 想要 gain=1，则应把 kColaScale 乘上 1/gain。
    std::printf("  单位增益下的重建增益 = %.6f（%.3f dB）\n", gain, gain_db);
    std::printf("  若要让增益为 1.0，kColaScale 需乘以 %.6f（当前 1/1.5=%.6f，"
                "应为 %.6f）\n",
                1.0 / gain, 1.0 / 1.5, (1.0 / 1.5) / gain);

    char label[192];
    std::snprintf(label, sizeof(label),
                  "纯重建增益 ≈ 1.0（实测 %.4f，偏差 %.3f dB）", gain, gain_db);
    Check(std::fabs(gain_db) < 0.2, label);
}

// 复现引擎实际调用方式的测试：立体声 + 一次性喂 200ms（9600 帧）。
//
// 为什么单独写这一项：单元测试按 480 帧/块喂一直是过的，但集成环境里
// 引擎一次要 9600 帧就崩。块长从 480 变到 9600 会跨过很多内部边界
// （跳步、暂存、重叠相加的轮数），必须把这个组合单独钉住。
//
// 同时模拟引擎里 "交错立体声 → 降单声道 → 降噪 → 铺回立体声" 的完整路径，
// 因为越界也可能发生在这一段（而不是降噪器内部）。
void TestEngineLikeStereoLargeBlock() {
    std::printf("\n=== 引擎式调用：立体声 + 9600 帧/次 ===\n");

    const int channels = 2;
    const int block = 9600;
    const int iterations = 20;

    std::printf("  [步骤] 构造 NoiseSuppressor(2)\n");
    NoiseSuppressor ns(channels);
    std::printf("  [步骤] WarmUp\n");
    ns.WarmUp();
    std::printf("  [步骤] Configure\n");
    ns.Configure(NsLevel::Moderate, NsBackend::Builtin, false);

    std::printf("  [步骤] 分配缓冲：interleaved=%d mono=%d\n", block * channels,
                block);
    std::vector<float> interleaved(static_cast<size_t>(block) * channels);
    std::vector<float> mono(static_cast<size_t>(block));
    std::mt19937 rng(7);
    std::normal_distribution<float> dist(0.0f, 0.05f);

    for (int it = 0; it < iterations; ++it) {
        std::printf("  [轮 %d] 填充\n", it);
        for (auto& v : interleaved) v = dist(rng);

        std::printf("  [轮 %d] 降单声道\n", it);
        for (int i = 0; i < block; ++i) {
            mono[static_cast<size_t>(i)] =
                0.5f * (interleaved[static_cast<size_t>(i) * channels] +
                        interleaved[static_cast<size_t>(i) * channels + 1]);
        }

        std::printf("  [轮 %d] Process(9600) ...\n", it);
        std::fflush(stdout);
        ns.Process(mono.data(), mono.data(), block);

        std::printf("  [轮 %d] 铺回立体声\n", it);
        for (int i = 0; i < block; ++i) {
            interleaved[static_cast<size_t>(i) * channels] = mono[static_cast<size_t>(i)];
            interleaved[static_cast<size_t>(i) * channels + 1] = mono[static_cast<size_t>(i)];
        }
    }

    bool any_bad = false;
    double energy = 0.0;
    for (float v : interleaved) {
        if (!std::isfinite(v)) any_bad = true;
        energy += static_cast<double>(v) * v;
    }
    char label[192];
    std::snprintf(label, sizeof(label),
                  "20 轮 9600 帧立体声：数值健全=%s 能量=%.4f（应 > 0）",
                  any_bad ? "否" : "是", energy);
    Check(!any_bad && energy > 0.0, label);
}

}  // namespace

int main() {
    // 关掉缓冲：崩溃时缓冲区里的内容会全部丢失，那就白跑了。
    // 这个测试专门用来定位崩溃位置，所以必须保证"打到哪就是执行到哪"。
    std::setvbuf(stdout, nullptr, _IONBF, 0);
    std::setvbuf(stderr, nullptr, _IONBF, 0);

    std::printf("降噪模块单元测试\n");
    std::printf("[开始] 即将运行 FFT 往返测试\n");
    TestFftRoundtrip();
    std::printf("[开始] 即将运行 Hann 窗 COLA 测试\n");
    TestCola();
    std::printf("[开始] 即将运行纯重建定标测试\n");
    TestReconstructionScaleOnly();
    std::printf("[开始] 即将运行重建增益测试\n");
    TestReconstructionGain();
    std::printf("[开始] 即将运行通道压力测试\n");
    StressChannels(1);
    StressChannels(2);
    std::printf("[开始] 即将运行引擎式大块测试\n");
    TestEngineLikeStereoLargeBlock();

    std::printf("\n=== 结果：%s（失败 %d 项）===\n",
                g_failures == 0 ? "全部通过" : "有失败", g_failures);
    return g_failures == 0 ? 0 : 1;
}
