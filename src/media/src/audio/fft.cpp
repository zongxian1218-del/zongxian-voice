#include "fft.h"

#include <cmath>
#include <mutex>
#include <unordered_map>

namespace zx {

namespace {

constexpr double kPi = 3.14159265358979323846;
constexpr int kMaxFft = 2048;

struct FftTables {
    int n = 0;
    std::vector<int> rev;
    std::vector<std::complex<float>> tw;   // 正变换的旋转因子
    std::vector<float> hann;
};

// 表在第一次用到时建立，之后只读。音频线程首次处理会有一点分配，
// 但在 initialize() 阶段就会预热（见 noise_suppress.cpp 的 WarmUp）。
std::unordered_map<int, FftTables>& TableCache() {
    static std::unordered_map<int, FftTables> cache;
    return cache;
}
std::mutex& TableMutex() {
    static std::mutex m;
    return m;
}

bool IsPow2(int v) { return v > 0 && (v & (v - 1)) == 0; }

// 位反转置换表：rev[i] = i 的二进制反转（只保留 log2(n) 位）
std::vector<int> BuildRev(int n) {
    int bits = 0;
    while ((1 << bits) < n) ++bits;
    std::vector<int> rev(static_cast<size_t>(n));
    for (int i = 0; i < n; ++i) {
        int r = 0;
        for (int b = 0; b < bits; ++b) {
            if (i & (1 << b)) r |= 1 << (bits - 1 - b);
        }
        rev[static_cast<size_t>(i)] = r;
    }
    return rev;
}

const FftTables& GetTables(int n) {
    std::lock_guard<std::mutex> lock(TableMutex());
    auto& cache = TableCache();
    auto it = cache.find(n);
    if (it != cache.end()) return it->second;

    FftTables t;
    t.n = n;
    t.rev = BuildRev(n);
    t.tw.resize(static_cast<size_t>(n / 2));
    for (int i = 0; i < n / 2; ++i) {
        const double angle = -2.0 * kPi * i / n;   // 正变换：e^{-j2pi k/n}
        t.tw[static_cast<size_t>(i)] = {
            static_cast<float>(std::cos(angle)),
            static_cast<float>(std::sin(angle))};
    }
    t.hann.resize(static_cast<size_t>(n));
    for (int i = 0; i < n; ++i) {
        // 周期性 Hann：分母是 n。50% 重叠时 w[i] + w[i+n/2] == 1，完美重建。
        t.hann[static_cast<size_t>(i)] =
            static_cast<float>(0.5 * (1.0 - std::cos(2.0 * kPi * i / n)));
    }
    auto res = cache.emplace(n, std::move(t));
    return res.first->second;
}

void Transform(std::complex<float>* data, int n, bool inverse) {
    const FftTables& t = GetTables(n);

    // 1) 位反转置换
    for (int i = 0; i < n; ++i) {
        const int j = t.rev[static_cast<size_t>(i)];
        if (i < j) std::swap(data[i], data[j]);
    }

    // 2) 迭代蝶形。逆变换用共轭旋转因子，最后统一除以 n。
    for (int len = 2; len <= n; len <<= 1) {
        const int half = len >> 1;
        const int step = n / len;
        for (int i = 0; i < n; i += len) {
            for (int k = 0; k < half; ++k) {
                std::complex<float> w = t.tw[static_cast<size_t>(k * step)];
                if (inverse) w = std::conj(w);
                const std::complex<float> u = data[i + k];
                const std::complex<float> v = data[i + k + half] * w;
                data[i + k] = u + v;
                data[i + k + half] = u - v;
            }
        }
    }

    if (inverse) {
        const float inv = 1.0f / static_cast<float>(n);
        for (int i = 0; i < n; ++i) data[i] *= inv;
    }
}

}  // namespace

void FftForward(std::complex<float>* data, int length) {
    if (!data || !IsPow2(length) || length > kMaxFft) return;
    Transform(data, length, /*inverse=*/false);
}

void FftInverse(std::complex<float>* data, int length) {
    if (!data || !IsPow2(length) || length > kMaxFft) return;
    Transform(data, length, /*inverse=*/true);
}

const std::vector<float>& HannWindow(int length) {
    static const std::vector<float> empty;
    if (!IsPow2(length) || length > kMaxFft) return empty;
    return GetTables(length).hann;
}

}  // namespace zx
