// ffmpeg-decoder.h —— 基于 libavcodec 的 H.264 解码器（低延迟）
//
// ============================================================================
// 【为什么换掉 Media Foundation 的解码器】
// 实测（离线、完全不涉及网络）：微软的 Microsoft H264 Video Decoder MFT 有
// **27 帧固定管线深度**，约等于 900ms 延迟：
//
//   管线深度(最大): 27 帧
//   深度轨迹: 20→20 40→27 60→27 80→27 100→25 120→27 140→27
//
// 而且它不接受任何降低管线的设置：
//   · CODECAPI_AVDecNumWorkerThreads = 1  → E_INVALIDARG
//   · CODECAPI_AVLowLatencyMode = TRUE    → E_INVALIDARG
// 系统里也没有任何硬件解码器 MFT（各种过滤条件都枚举过，只有这一个软件解码器）。
//
// libavcodec 可以显式关掉并行与前瞻：
//   · thread_count = 1        不分片并行，避免"每线程压一帧"
//   · AV_CODEC_FLAG_LOW_DELAY 不做前瞻
//   · has_b_frames = 0        码流本身没有 B 帧，无需重排序
// 这样输出应该紧跟输入，延迟量级降到 1 帧。
//
// 接口与 h264-decoder.h 完全一致，可直接替换。
// ============================================================================

#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

// 【必须自己包 extern "C"】FFmpeg 的公开头文件里**没有** extern "C" 保护，
// 直接在 C++ 里 include 会生成 C++ 修饰符号（?av_mallocz@@...），
// 而导入库里只有 C 符号，链接时报一堆 LNK2019。
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/imgutils.h>
#include <libavutil/opt.h>
#include <libswscale/swscale.h>
}

#include <functional>
#include <string>
#include <vector>

namespace zx {

class FfmpegH264Decoder {
public:
    // 输出像素格式：NV12 用于存证/分析，BGRA 用于直接上屏（无需着色器）
    enum class OutputFormat { Nv12, Bgra };

    struct DecodedFrame {
        const uint8_t* data;    // NV12：Y 平面在前，UV 紧随其后；BGRA：紧密排列
        UINT stride;
        UINT width;
        UINT height;
        uint64_t timestampUs;
        OutputFormat format;
    };
    using FrameCallback = std::function<void(const DecodedFrame&)>;

    FfmpegH264Decoder() = default;
    ~FfmpegH264Decoder() { Close(); }
    FfmpegH264Decoder(const FfmpegH264Decoder&) = delete;
    FfmpegH264Decoder& operator=(const FfmpegH264Decoder&) = delete;

    const std::wstring& name() const { return name_; }
    const std::wstring& configNote() const { return cfgNote_; }
    UINT width() const { return w_; }
    UINT height() const { return h_; }
    UINT stride() const { return stride_; }
    int decodedFrames() const { return framesOut_; }
    void SetCallback(FrameCallback cb) { cb_ = std::move(cb); }

    // 切换输出格式（BGRA 用于上屏，NV12 用于存证）。会丢弃已有的转换上下文。
    void SetOutputFormat(OutputFormat f)
    {
        if (f == outFmt_) return;
        outFmt_ = f;
        if (sws_) { sws_freeContext(sws_); sws_ = nullptr; }
        if (w_ && h_) EnsureBuffer(w_, h_);
    }

    bool Open(const uint8_t* seqHeader, size_t seqLen, UINT w, UINT h, UINT fps,
              std::wstring& err)
    {
        (void)fps;

        const AVCodec* codec = avcodec_find_decoder(AV_CODEC_ID_H264);
        if (!codec) { err = L"libavcodec 里找不到 H.264 解码器"; return false; }

        ctx_ = avcodec_alloc_context3(codec);
        if (!ctx_) { err = L"avcodec_alloc_context3 失败"; return false; }

        // 【低延迟核心设置】
        ctx_->thread_count = 1;          // 不并行，避免多帧滞留在解码器里
        ctx_->thread_type = 0;
        ctx_->flags |= AV_CODEC_FLAG_LOW_DELAY;
        ctx_->flags2 |= AV_CODEC_FLAG2_FAST;

        // 序列头作为 extradata 交给解码器，这样第一帧之前它就已知分辨率
        if (seqHeader && seqLen) {
            ctx_->extradata = (uint8_t*)av_mallocz(seqLen + AV_INPUT_BUFFER_PADDING_SIZE);
            if (ctx_->extradata) {
                memcpy(ctx_->extradata, seqHeader, seqLen);
                ctx_->extradata_size = (int)seqLen;
            }
        }

        const int ohr = avcodec_open2(ctx_, codec, nullptr);
        if (ohr < 0) {
            err = L"avcodec_open2 失败，错误码 " + std::to_wstring(ohr);
            return false;
        }

        {
            wchar_t b[256];
            swprintf_s(b, L"%S  线程=%d 低延迟=开 B帧=%d",
                       codec->name, ctx_->thread_count, (int)ctx_->has_b_frames);
            name_ = b;
        }
        cfgNote_ = L"thread_count=1, LOW_DELAY=on";

        pkt_ = av_packet_alloc();
        frame_ = av_frame_alloc();
        if (!pkt_ || !frame_) { err = L"av_packet_alloc / av_frame_alloc 失败"; return false; }

        // 输出分辨率先按期望值建缓冲，实际值以解码帧为准
        w_ = w; h_ = h;
        EnsureBuffer(w, h);
        ok_ = true;
        return true;
    }

    bool Feed(const uint8_t* au, size_t len, uint64_t timestampUs)
    {
        if (!ok_) return false;

        av_packet_unref(pkt_);
        if (av_new_packet(pkt_, (int)len) < 0) return false;
        memcpy(pkt_->data, au, len);
        // 时间戳原样透传，接收端用它做延迟测量
        pkt_->pts = (int64_t)timestampUs;
        pkt_->dts = (int64_t)timestampUs;

        const int shr = avcodec_send_packet(ctx_, pkt_);
        av_packet_unref(pkt_);
        if (shr < 0 && shr != AVERROR(EAGAIN)) return false;

        PumpFrames();
        return true;
    }

    void Flush() { if (ctx_) avcodec_flush_buffers(ctx_); }

    void Drain()
    {
        if (!ok_) return;
        avcodec_send_packet(ctx_, nullptr);   // 送 nullptr 表示流结束，把缓存帧冲出来
        PumpFrames();
    }

    void Close()
    {
        if (sws_) { sws_freeContext(sws_); sws_ = nullptr; }
        if (frame_) { av_frame_free(&frame_); }
        if (pkt_) { av_packet_free(&pkt_); }
        if (ctx_) { avcodec_free_context(&ctx_); }
        buf_.clear();
        ok_ = false;
    }

private:
    void EnsureBuffer(UINT w, UINT h)
    {
        w_ = w; h_ = h;
        if (outFmt_ == OutputFormat::Bgra) {
            stride_ = w * 4;
            buf_.assign(size_t(stride_) * h, 0);
        } else {
            stride_ = w;
            buf_.assign(size_t(w) * h * 3 / 2, 0);
        }
    }

    void PumpFrames()
    {
        const AVPixelFormat want = (outFmt_ == OutputFormat::Bgra) ? AV_PIX_FMT_BGRA
                                                                   : AV_PIX_FMT_NV12;
        for (;;) {
            const int r = avcodec_receive_frame(ctx_, frame_);
            if (r == AVERROR(EAGAIN) || r == AVERROR_EOF) break;
            if (r < 0) break;

            const UINT fw = (UINT)frame_->width;
            const UINT fh = (UINT)frame_->height;

            // 解码器可能给出对齐后的尺寸，按实际值重建缓冲
            if (fw != w_ || fh != h_) EnsureBuffer(fw, fh);

            if (!sws_) {
                sws_ = sws_getContext((int)fw, (int)fh, (AVPixelFormat)frame_->format,
                                      (int)fw, (int)fh, want,
                                      SWS_BILINEAR, nullptr, nullptr, nullptr);
                if (!sws_) { av_frame_unref(frame_); break; }
            }

            uint8_t* dst[4] = { buf_.data(), nullptr, nullptr, nullptr };
            int dstStride[4] = { (int)stride_, 0, 0, 0 };
            if (outFmt_ == OutputFormat::Nv12) {
                // NV12 的 UV 平面紧跟在 Y 之后
                dst[1] = buf_.data() + size_t(stride_) * h_;
                dstStride[1] = (int)stride_;
            }
            sws_scale(sws_, frame_->data, frame_->linesize, 0, (int)fh, dst, dstStride);

            int64_t ts = frame_->pts;
            if (ts == AV_NOPTS_VALUE) ts = frame_->best_effort_timestamp;
            av_frame_unref(frame_);

            if (cb_) {
                DecodedFrame f{};
                f.data = buf_.data();
                f.stride = stride_;
                f.width = w_;
                f.height = h_;
                f.timestampUs = (ts == AV_NOPTS_VALUE) ? 0 : (uint64_t)ts;
                f.format = outFmt_;
                ++framesOut_;
                cb_(f);
            } else {
                ++framesOut_;
            }
        }
    }

    AVCodecContext* ctx_ = nullptr;
    AVPacket* pkt_ = nullptr;
    AVFrame* frame_ = nullptr;
    SwsContext* sws_ = nullptr;
    std::vector<uint8_t> buf_;
    OutputFormat outFmt_ = OutputFormat::Nv12;
    std::wstring name_;
    std::wstring cfgNote_;
    FrameCallback cb_;
    UINT w_ = 0, h_ = 0, stride_ = 0;
    int framesOut_ = 0;
    bool ok_ = false;
};

} // namespace zx
