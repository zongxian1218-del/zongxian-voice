#include "ring_buffer.h"

#include <algorithm>

namespace zx {

namespace {
// 向上取整到 2 的幂，最小 2。单位：样本(float)。
int NextPow2(int v) {
    int p = 2;
    while (p < v && p < (1 << 30)) p <<= 1;
    return p;
}
}  // namespace

RingBuffer::RingBuffer(int capacity_samples)
    : capacity_(NextPow2(capacity_samples > 2 ? capacity_samples : 2)),
      mask_(capacity_ - 1),
      buffer_(static_cast<size_t>(capacity_), 0.0f) {}

// 单位：样本(float)。调用点必须传 帧数 × 声道数，见头文件的单位契约。
int RingBuffer::Write(const float* interleaved, int samples) {
    if (!interleaved || samples <= 0) return 0;

    std::lock_guard<std::mutex> lock(mutex_);

    const int available = (write_sample_ - read_sample_ + capacity_) & mask_;
    const int free_samples = capacity_ - available;
    const int writable = std::min(samples, free_samples);

    for (int i = 0; i < writable; ++i) {
        buffer_[static_cast<size_t>((write_sample_ + i) & mask_)] = interleaved[i];
    }
    write_sample_ = (write_sample_ + writable) & mask_;

    // 溢出时丢的是**新来的**数据（保留已采到的）。如实记账，统计里要能看到。
    if (writable < samples) {
        dropped_.fetch_add(static_cast<uint64_t>(samples - writable),
                           std::memory_order_relaxed);
    }
    return writable;
}

// 单位：样本(float)。cap_samples 是样本数，不是帧数。
int RingBuffer::Read(float* out_interleaved, int cap_samples) {
    if (!out_interleaved || cap_samples <= 0) return 0;

    std::lock_guard<std::mutex> lock(mutex_);

    const int available = (write_sample_ - read_sample_ + capacity_) & mask_;
    const int n = std::min(cap_samples, available);

    for (int i = 0; i < n; ++i) {
        out_interleaved[i] = buffer_[static_cast<size_t>((read_sample_ + i) & mask_)];
    }
    read_sample_ = (read_sample_ + n) & mask_;
    return n;
}

void RingBuffer::Clear() {
    std::lock_guard<std::mutex> lock(mutex_);
    read_sample_ = 0;
    write_sample_ = 0;
    // dropped_ 是累计诊断量，刻意不清零。
}

int RingBuffer::AvailableSamples() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return (write_sample_ - read_sample_ + capacity_) & mask_;
}

int RingBuffer::FreeSamples() const {
    std::lock_guard<std::mutex> lock(mutex_);
    const int available = (write_sample_ - read_sample_ + capacity_) & mask_;
    return capacity_ - available;
}

}  // namespace zx
