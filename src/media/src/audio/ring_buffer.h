// audio/ring_buffer.h —— 采集线程与消费线程之间唯一的通道
//
// 为什么需要它：WASAPI 的回调以 10ms 为周期把数据推过来，而消费方（Python、
// 录音线程、将来的 Opus 编码器）节奏完全不同。中间必须有一个**明确的缓冲边界**，
// 否则实时线程要么被阻塞（爆音），要么被逼着自己分配内存（更糟）。
//
// ===========================================================================
// 【单位契约 · 2026-10-06 定死，别改回去】
//
//   这个类里的一切计数单位都是**样本(float)**，不是音频帧：
//     · CapacitySamples() / AvailableSamples() / FreeSamples() / DroppedSamples()
//     · Write(const float*, int samples) / Read(float*, int cap_samples)
//   一个立体声块 = 帧数 × 声道数 个 float。buffer_ 的长度就是 capacity 个
//   float，只有按样本计数才自洽。
//
//   历史教训（审计病根 4）：API 与注释写「帧」、两个调用点按「样本」用，
//   结果立体声只写进去一半样本、左右交错错位；谁照注释去"修正"调用点就会
//   二次引爆。调用点必须自己乘声道数 —— 见 wasapi_backend.cpp 的
//   CaptureLoop（写入）与 WasapiSource::Read（读出）。
// ===========================================================================
//
// 设计取舍：
//   · 容量取 2 的幂，用位与代替取模 —— 这一层每 10ms 都要跑，省一点是一点。
//   · 读写共一把锁，不做无锁。10ms 一次的争用完全不存在，可读性远比那点
//     纳秒重要。（真到了需要无锁的时候，这里也是最容易换掉的地方。）
//   · 溢出策略是**丢最新**（在 Write 里），保留已采到的；统计里如实记账。
//   · dropped_ 用原子量暴露给诊断，方便在 stats 里如实报告"到底丢了多少"。
#pragma once

#include <atomic>
#include <cstdint>
#include <mutex>
#include <vector>

namespace zx {

class RingBuffer {
public:
    // capacity_samples 是**样本(float)数**，会被向上取整到 2 的幂。
    explicit RingBuffer(int capacity_samples);

    // 写入 samples 个交错样本。返回真正写入的样本数；小于 samples 说明溢出了。
    // 只由采集线程调用。
    int Write(const float* interleaved, int samples);

    // 拉取最多 cap_samples 个样本。返回实际取到的样本数（可能为 0）。
    // 由消费线程调用；多线程同时读会被串行化。
    int Read(float* out_interleaved, int cap_samples);

    // 丢掉未读数据（例如切换降噪档位时清空，避免新旧数据混在一起）。
    void Clear();

    int  CapacitySamples() const { return capacity_; }
    int  AvailableSamples() const;   // 可读样本数
    int  FreeSamples() const;        // 可写样本数

    // 因为溢出而被丢弃的累计样本数。这个数字必须能在统计里看到 ——
    // 「采集周期稳不稳」就靠它和 Discontinuities 说话。
    uint64_t DroppedSamples() const { return dropped_.load(std::memory_order_relaxed); }

private:
    int capacity_;
    int mask_;
    // 一把锁管读写两端。10ms 一次的争用在真实场景里根本测不出来，
    // 而单锁让「可写多少」的计算是绝对可靠的 —— 双锁下 read_sample_ 会被
    // 无锁读，那是数据竞争，不能留。
    mutable std::mutex mutex_;
    std::vector<float> buffer_;     // 长度 = capacity_（单位：样本 float）
    int read_sample_ = 0;           // 以**样本**为单位的下标
    int write_sample_ = 0;
    std::atomic<uint64_t> dropped_{0};
};

}  // namespace zx
