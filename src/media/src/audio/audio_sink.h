// audio/audio_sink.h —— 采集侧数据的落盘句柄（WAV）
//
// 用途很具体：PL0 的验收标准是「降噪前后能听出差别」，而「能听出差别」
// 需要一个客观、可复现的对比物。所以录音不是附属功能，是把主观感受变成
// 可检查证据的手段：同一时刻录 raw（降噪前）和 processed（降噪后）两份。
//
// 只写 16-bit PCM WAV —— 这是所有播放器都能直接打开的格式，方便直接发给别人听。
#pragma once

#include <cstdint>
#include <cstdio>
#include <mutex>
#include <string>

namespace zx {

class WavWriter {
public:
    WavWriter() = default;
    ~WavWriter();

    WavWriter(const WavWriter&) = delete;
    WavWriter& operator=(const WavWriter&) = delete;

    // 打开文件并写好占位头（真正的长度在 Close 时回填）。
    bool Open(const std::string& path_utf8, int sample_rate, int channels);
    void Close();
    bool IsOpen() const { return fp_ != nullptr; }

    // 写入交错 float32（-1..1），内部转成 16-bit 并做饱和截断。
    int WriteFrames(const float* interleaved, int frames);

    uint64_t FramesWritten() const { return frames_written_; }
    const std::string& Path() const { return path_; }

private:
    std::FILE* fp_ = nullptr;
    std::string path_;
    int sample_rate_ = 48000;
    int channels_ = 1;
    uint64_t frames_written_ = 0;
    uint32_t data_bytes_ = 0;
    std::mutex mutex_;
};

}  // namespace zx
