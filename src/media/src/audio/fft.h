// audio/fft.h —— 只为降噪服务的最小 FFT
//
// 为什么自己写而不用第三方：谱减降噪只需要实数信号的 FFT/IFFT 和窗函数的
// COLA 校验，加起来不到 120 行。为此引一个 FFTW/KissFFT 依赖，会让
// 「零依赖构建」这条底线破掉，得不偿失。
//
// 规格固定：2 的幂长度，最大 2048（本引擎只用到 512）。所有表在首次调用时
// 生成并缓存 —— 音频线程只读，不做每次分配。
#pragma once

#include <complex>
#include <vector>

namespace zx {

// 正变换。length 必须是 2 的幂且 <= 2048。
// data 原地被覆盖为频域结果（未归一化）。
void FftForward(std::complex<float>* data, int length);

// 逆变换。包含 1/length 归一化，所以 Forward 后紧跟 Inverse 能还原。
void FftInverse(std::complex<float>* data, int length);

// 生成周期性的 Hann 窗（分母用 length 而非 length-1）。
// 这样在 50% 重叠相加时，w[n] + w[n+hop] 恰好恒等于 1，即完美重建（COLA）。
// 返回的向量长度 = length。
const std::vector<float>& HannWindow(int length);

}  // namespace zx
