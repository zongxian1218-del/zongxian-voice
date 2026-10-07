#include "audio_sink.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <vector>

#ifdef _WIN32
#  include <windows.h>
#endif

namespace zx {

namespace {

// UTF-8 路径 → 宽字符。中文用户名/中文目录太常见，用 fopen 会因为 ANSI
// 代码页问题直接失败。这一步不能省，否则「录音存不上」会被误诊成引擎 bug。
std::wstring Utf8ToWide(const std::string& s) {
#ifdef _WIN32
    if (s.empty()) return L"";
    const int need = ::MultiByteToWideChar(CP_UTF8, 0, s.c_str(),
                                           static_cast<int>(s.size()), nullptr, 0);
    if (need <= 0) return L"";
    std::wstring w(static_cast<size_t>(need), L'\0');
    ::MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()),
                          &w[0], need);
    return w;
#else
    return std::wstring(s.begin(), s.end());
#endif
}

std::FILE* OpenWrite(const std::string& path_utf8) {
#ifdef _WIN32
    const std::wstring w = Utf8ToWide(path_utf8);
    if (w.empty()) return nullptr;
    return ::_wfopen(w.c_str(), L"wb");
#else
    return std::fopen(path_utf8.c_str(), "wb");
#endif
}

void PutU32(std::FILE* f, uint32_t v) {
    unsigned char b[4] = {static_cast<unsigned char>(v & 0xFF),
                          static_cast<unsigned char>((v >> 8) & 0xFF),
                          static_cast<unsigned char>((v >> 16) & 0xFF),
                          static_cast<unsigned char>((v >> 24) & 0xFF)};
    std::fwrite(b, 1, 4, f);
}
void PutU16(std::FILE* f, uint16_t v) {
    unsigned char b[2] = {static_cast<unsigned char>(v & 0xFF),
                          static_cast<unsigned char>((v >> 8) & 0xFF)};
    std::fwrite(b, 1, 2, f);
}

inline int16_t FloatToPcm16(float v) {
    // 饱和截断：超过 ±1 的声音宁可削平，也不要回绕成刺耳的爆音。
    if (v > 1.0f) v = 1.0f;
    if (v < -1.0f) v = -1.0f;
    // 32767 而不是 32768：避免 +1.0 映射到溢出边界
    return static_cast<int16_t>(std::lround(v * 32767.0f));
}

}  // namespace

WavWriter::~WavWriter() { Close(); }

bool WavWriter::Open(const std::string& path_utf8, int sample_rate, int channels) {
    Close();
    if (path_utf8.empty() || sample_rate <= 0 || channels <= 0) return false;

    fp_ = OpenWrite(path_utf8);
    if (!fp_) return false;

    path_ = path_utf8;
    sample_rate_ = sample_rate;
    channels_ = channels;
    frames_written_ = 0;
    data_bytes_ = 0;

    // ---- RIFF 头（44 字节），长度字段先写 0，Close 时回填 ----
    std::fwrite("RIFF", 1, 4, fp_);
    PutU32(fp_, 0);                     // 回填：36 + data_bytes
    std::fwrite("WAVE", 1, 4, fp_);
    std::fwrite("fmt ", 1, 4, fp_);
    PutU32(fp_, 16);                    // PCM 的 fmt 块长度
    PutU16(fp_, 1);                     // 1 = PCM
    PutU16(fp_, static_cast<uint16_t>(channels_));
    PutU32(fp_, static_cast<uint32_t>(sample_rate_));
    const uint32_t byte_rate =
        static_cast<uint32_t>(sample_rate_ * channels_ * 2 /*16bit*/);
    PutU32(fp_, byte_rate);
    PutU16(fp_, static_cast<uint16_t>(channels_ * 2));   // 块对齐
    PutU16(fp_, 16);                    // 位深
    std::fwrite("data", 1, 4, fp_);
    PutU32(fp_, 0);                     // 回填：data_bytes

    return true;
}

int WavWriter::WriteFrames(const float* interleaved, int frames) {
    if (!fp_ || !interleaved || frames <= 0) return 0;

    std::lock_guard<std::mutex> lock(mutex_);

    const size_t n = static_cast<size_t>(frames) * static_cast<size_t>(channels_);
    // 复用成员缓冲避免每次分配内存（这个函数在音频路径上被调用）
    static thread_local std::vector<int16_t> pcm;
    pcm.resize(n);
    for (size_t i = 0; i < n; ++i) pcm[i] = FloatToPcm16(interleaved[i]);

    const size_t wrote = std::fwrite(pcm.data(), sizeof(int16_t), n, fp_);
    const int written_frames = static_cast<int>(wrote / static_cast<size_t>(channels_));

    frames_written_ += static_cast<uint64_t>(written_frames);
    data_bytes_ += static_cast<uint32_t>(wrote * sizeof(int16_t));
    return written_frames;
}

void WavWriter::Close() {
    if (!fp_) return;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        // 回填两个长度字段。必须先 flush 再定位，否则缓冲里的数据会被冲掉。
        std::fflush(fp_);
#ifdef _WIN32
        _fseeki64(fp_, 4, SEEK_SET);
#else
        std::fseek(fp_, 4, SEEK_SET);
#endif
        PutU32(fp_, 36u + data_bytes_);
#ifdef _WIN32
        _fseeki64(fp_, 40, SEEK_SET);
#else
        std::fseek(fp_, 40, SEEK_SET);
#endif
        PutU32(fp_, data_bytes_);
        std::fclose(fp_);
        fp_ = nullptr;
    }
}

}  // namespace zx
