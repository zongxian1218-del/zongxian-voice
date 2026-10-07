// zxprobe.cpp —— PL0 的命令行验收工具
//
// 为什么要有这个东西：调音频的时候，「改一行 C++ → 编译 → 跑 CLI → 听 WAV」
// 这个循环比「改 C++ → 编译 → 改 Python → 跑 → 看界面」快一个数量级。
// 所以引擎的第一个可执行目标不是 GUI，而是这个探针。
//
// 用法：
//   zxprobe devices                     列出采集/渲染设备
//   zxprobe loopback-probe              探测本机是否支持「单个应用音频」
//   zxprobe apps                        列出正在发声（有音频会话）的进程
//   zxprobe rate-check                  自检采样率契约（不碰设备，纯软件）
//   zxprobe ring-check                  自检 RingBuffer 的单位契约（样本，不碰设备）
//   zxprobe record [选项]
//        --seconds <n>     录制秒数，默认 10
//        --device <id>     指定设备（默认设备留空即可）
//        --kind <k>        mic | loopback | process，默认 mic
//        --pid <n>         kind=process 时必填
//        --children <0|1>  进程回环是否包含子进程，默认 1
//        --ns <0..3>       降噪档位，默认 2（中）
//        --nsbypass <0|1>  打开旁路（数据直通但仍统计），默认 0
//        --out <path>      降噪后 WAV，默认 build/pl0-processed.wav
//        --raw <path>      降噪前 WAV，默认 build/pl0-raw.wav
//        --channels <n>    目标声道数，默认按 kind 决定
//        --want-rate <n>   期望采样率，默认 48000（实际以打印出的为准）
//        --volume <0..1>   主音量（真的会缩放输出，用于验证它不是假开关）
//   zxprobe stream [--kind mic|loopback|process] [--pid n] [--seconds n]
//        [--want-rate n] [--expect-rate n] [--strict-rate 0|1] [--volume f]
//
// 验收看两个东西：
//   1) stats 里的 avgPeriodMs 应≈10.0、maxPeriodMs 应 < 16、discontinuities=0
//   2) 两份 WAV 用耳朵听：底噪应明显变小，且人声不该有「水声」或明显失真
//
// 【采样率契约 2026-10-06】本文件里**不再有 /48000.0 这种硬算**：
// 所有"帧 → 秒"的换算都用从 zx_source_format() 读到的实际采样率
// （helper ParseFormatInt(fmt, "sampleRate", ZX_SAMPLE_RATE)）。
// 44.1k 设备上以前会静默变调 + 时长错（审计真 bug 11）。
#include "zongxian_media.h"

// 直接引引擎内部头，用于 ring-check 验证 RingBuffer 的单位契约。
// （CMake 里 zxprobe 只链 zx_media_core、没有加 src 到 include path，
//  所以用相对路径；相对路径也从源码树构建，不用改构建脚本。）
#include "../src/audio/ring_buffer.h"

#include <chrono>
#include <cmath>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <io.h>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#  include <windows.h>
#endif

namespace {

const char* kOutDir = "build";

// 立即输出并刷新。诊断崩溃时不能依赖 stdout 缓冲 —— 进程异常终止时
// 缓冲区里的内容会全部丢失，那就白跑一趟了。
void Say(const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    std::vfprintf(stdout, fmt, args);
    va_end(args);
    std::fflush(stdout);
}

void PrintError() {
    const char* e = zx_last_error();
    if (e && *e) std::printf("  ! %s\n", e);
}

// 从命令行取值。写得很土，但探针工具要的就是能在任何环境跑起来。
int64_t ArgInt(int argc, char** argv, const char* name, int64_t def) {
    for (int i = 1; i + 1 < argc; ++i) {
        if (std::strcmp(argv[i], name) == 0) return std::strtoll(argv[i + 1], nullptr, 10);
    }
    return def;
}
std::string ArgStr(int argc, char** argv, const char* name, const char* def) {
    for (int i = 1; i + 1 < argc; ++i) {
        if (std::strcmp(argv[i], name) == 0) return argv[i + 1];
    }
    return def;
}

// 浮点参数（音量这类 0..1 的小数不能用 ArgInt —— strtoll 会把 0.5 截成 0）
double ArgFloat(int argc, char** argv, const char* name, double def) {
    for (int i = 1; i + 1 < argc; ++i) {
        if (std::strcmp(argv[i], name) == 0) return std::strtod(argv[i + 1], nullptr);
    }
    return def;
}

// 从 zx_source_format() 的 JSON 里取一个整数键（"sampleRate"/"deviceSampleRate"
// /"channels"...）。找不到就返回 fallback。**所有"帧→秒"的换算都必须经过它**，
// 不许再写 48000 —— 44.1k 设备上那会静默变调。
int ParseFormatInt(const char* json, const char* key, int fallback) {
    if (!json || !key) return fallback;
    char pat[64];
    std::snprintf(pat, sizeof(pat), "\"%s\":", key);
    const char* p = std::strstr(json, pat);
    if (!p) return fallback;
    const int v = std::atoi(p + std::strlen(pat));
    return v > 0 ? v : fallback;
}

// 帧 → 秒。rate 必须来自 ParseFormatInt（实际生效的采样率）。
double FramesToSeconds(uint64_t frames, int rate) {
    return static_cast<double>(frames) / static_cast<double>(rate > 0 ? rate : 1);
}

void Usage() {
    std::printf(
        "棕仙语音 媒体引擎探针 (PL0)\n"
        "  zxprobe devices\n"
        "  zxprobe loopback-probe\n"
        "  zxprobe apps\n"
        "  zxprobe rate-check               采样率契约自检（纯软件）\n"
        "  zxprobe ring-check               RingBuffer 单位契约自检（纯软件）\n"
        "  zxprobe record [--seconds n] [--kind mic|loopback|process] [--pid n]\n"
        "                 [--device id] [--want-rate n] [--volume 0..1]\n"
        "                 [--ns 0..3] [--nsbypass 0|1] [--out path] [--raw path]\n"
        "  zxprobe stream [--kind k] [--pid n] [--seconds n] [--want-rate n]\n"
        "                 [--expect-rate n] [--strict-rate 0|1] [--volume 0..1]\n"
        "  全局：--log-level trace|debug|info|warn|error（默认 debug，真过滤）\n");
}

int CmdDevices() {
    std::vector<char> buf(64 * 1024);

    int n = zx_list_capture_devices(buf.data(), static_cast<int>(buf.size()));
    if (n < 0) { PrintError(); return 2; }
    std::printf("采集设备：\n%s\n\n", buf.data());

    n = zx_list_render_devices(buf.data(), static_cast<int>(buf.size()));
    if (n < 0) { PrintError(); return 2; }
    std::printf("渲染（播放）设备：\n%s\n", buf.data());
    return 0;
}

int CmdLoopbackProbe() {
    // 逐步输出，用来在崩溃时定位是哪一步出的问题。
    // 这个探测会调用系统的异步激活接口，是全项目里最"靠近系统"的一段代码，
    // 值得为它多花几行诊断代码。
    Say("[1/4] 调用 zx_probe_process_loopback ...\n");

    int supported = 0;
    char detail[512] = {0};
    const zx_result rc = zx_probe_process_loopback(&supported, detail, sizeof(detail));

    Say("[2/4] 返回 rc=%d\n", static_cast<int>(rc));
    if (rc != ZX_OK) { PrintError(); return 2; }

    Say("[3/4] supported=%d\n", supported);
    Say("[4/4] detail=%s\n", detail);
    std::printf("单个应用音频（进程回环）：%s\n  %s\n",
                supported ? "支持" : "不支持", detail);
    return supported ? 0 : 1;
}

int CmdApps() {
    std::vector<char> buf(64 * 1024);
    const int n = zx_list_audio_processes(buf.data(), static_cast<int>(buf.size()));
    if (n < 0) { PrintError(); return 2; }
    std::printf("有音频会话的进程：\n%s\n", buf.data());
    return 0;
}

// ---------------------------------------------------------------------------
// rate-check：采样率契约自检（纯软件，不需要任何音频设备）
//
// 证明的是审计真 bug 11 的修法：**"输出头里的 rate"与"计算用的 rate"是同一个值**。
// 两件必须成立的事：
//   1) 按 zx_source_format() 的 JSON 解析采样率，44100 就解析出 44100（不是 48000）；
//   2) 时长换算用的就是这个解析值：44100 帧 @44100 = 1.000 秒，
//      若仍按 48000 硬算会得到 0.919 秒（差 8.8% ⇒ 变调）。
// 任何一项不成立就返回 1，可以进 CI。
// ---------------------------------------------------------------------------
int CmdRateCheck() {
    struct Case { int rate; int frames; };
    const Case cases[] = {{ZX_SAMPLE_RATE, ZX_SAMPLE_RATE},
                          {44100, 44100},
                          {44100, ZX_SAMPLE_RATE},   // 同样的帧数，不同 rate 必须得到不同时长
                          {16000, 16000}};
    int failures = 0;
    std::printf("采样率契约自检（帧→秒 必须用实际 rate）\n");
    std::printf("  %-8s %-10s %-12s %-12s %s\n", "头rate", "帧数", "按头rate(秒)",
                "按48000(秒)", "结论");
    for (const Case& c : cases) {
        // 伪造 zx_source_format 的输出头（真实运行时它是引擎生成的同一形状）
        char fmt[128];
        std::snprintf(fmt, sizeof(fmt),
                      "{\"sampleRate\":%d,\"deviceSampleRate\":%d,\"resampled\":false}",
                      c.rate, c.rate);
        const int parsed = ParseFormatInt(fmt, "sampleRate", ZX_SAMPLE_RATE);
        const double sec_actual = FramesToSeconds(static_cast<uint64_t>(c.frames), parsed);
        const double sec_hardcoded = FramesToSeconds(static_cast<uint64_t>(c.frames), 48000);
        const bool rate_ok = (parsed == c.rate);
        // 期望：头里的 rate 与计算用的一致 ⇒ sec_actual == frames/rate
        const double expected =
            static_cast<double>(c.frames) / static_cast<double>(c.rate);
        const bool math_ok = std::fabs(sec_actual - expected) < 1e-9;
        if (!rate_ok || !math_ok) ++failures;
        std::printf("  %-8d %-10d %-12.6f %-12.6f %s\n", parsed, c.frames, sec_actual,
                    sec_hardcoded,
                    (rate_ok && math_ok) ? "OK" : "FAIL（头里的 rate 与算的不一致）");
    }
    std::printf("结论：%s\n", failures == 0 ? "全部一致（头 rate == 计算 rate）"
                                           : "存在不一致，采样率契约被破坏");
    std::printf("注：44.1k 一行的\"按48000\"列是故意留的对照 —— 差 8.8%% 就是变调来源。\n");
    return failures == 0 ? 0 : 1;
}

// ---------------------------------------------------------------------------
// ring-check：RingBuffer 单位契约自检（纯软件，不需要设备）
//
// 契约（见 src/audio/ring_buffer.h）：一切计数都是**样本(float)**。
// 用立体声验证"写 480 帧 = 960 样本"这条换算：
//   · 写 480 帧 × 2 声道 = 960 样本 → AvailableSamples() 必须是 960（不是 480）
//   · 读回 960 个样本，逐点比对，必须与写入的完全一致（不错位、不丢一半）
//   · 容量语义也按样本：FreeSamples() + AvailableSamples() == CapacitySamples()
// ---------------------------------------------------------------------------
int CmdRingCheck() {
    const int frames = 480;         // 一个 10ms 块
    const int channels = 2;
    const int samples = frames * channels;
    zx::RingBuffer ring(samples * 4);   // 容量按样本给
    std::vector<float> in(static_cast<size_t>(samples));
    for (int i = 0; i < samples; ++i) in[static_cast<size_t>(i)] = static_cast<float>(i) * 0.001f;

    const int wrote = ring.Write(in.data(), samples);
    const int avail = ring.AvailableSamples();
    const int cap = ring.CapacitySamples();
    const int free_now = ring.FreeSamples();

    std::vector<float> out(static_cast<size_t>(samples), -1.0f);
    const int got = ring.Read(out.data(), samples);
    int mismatches = 0;
    for (int i = 0; i < samples; ++i) {
        if (out[static_cast<size_t>(i)] != in[static_cast<size_t>(i)]) ++mismatches;
    }

    const bool ok = (wrote == samples) && (avail == samples) && (got == samples) &&
                    (mismatches == 0) && (free_now + avail == cap);
    std::printf("RingBuffer 单位契约自检（单位：样本 float）\n");
    std::printf("  写入 %d 帧 × %d 声道 = %d 样本 -> Write 返回 %d，AvailableSamples %d\n",
                frames, channels, samples, wrote, avail);
    std::printf("  读回 %d 个样本，逐点不符 %d 个；容量 %d = 可读 %d + 可写 %d\n",
                got, mismatches, cap, avail, free_now);
    std::printf("  判定：%s\n", ok ? "OK（样本单位自洽，立体声不错位）"
                                   : "FAIL（单位被改回帧了？）");
    return ok ? 0 : 1;
}

int CmdRecord(int argc, char** argv) {
    const int seconds = static_cast<int>(ArgInt(argc, argv, "--seconds", 10));
    const std::string kind = ArgStr(argc, argv, "--kind", "mic");
    const int64_t pid = ArgInt(argc, argv, "--pid", 0);
    const int ns = static_cast<int>(ArgInt(argc, argv, "--ns", 2));
    const bool nsbypass = ArgInt(argc, argv, "--nsbypass", 0) != 0;
    const int channels = static_cast<int>(ArgInt(argc, argv, "--channels", 0));
    const bool children = ArgInt(argc, argv, "--children", 1) != 0;
    const std::string out = ArgStr(argc, argv, "--out", "build/pl0-processed.wav");
    const std::string raw = ArgStr(argc, argv, "--raw", "build/pl0-raw.wav");
    // 以前 --device 写在用法里但代码从没读过：指定设备被静默忽略。
    const std::string device = ArgStr(argc, argv, "--device", "");
    const int want_rate = static_cast<int>(ArgInt(argc, argv, "--want-rate", ZX_SAMPLE_RATE));
    const double volume = ArgFloat(argc, argv, "--volume", -1.0);
    const double volume_f = volume < 0 ? 1.0 : volume;

    int kind_value = ZX_SRC_MIC;
    if (kind == "loopback") kind_value = ZX_SRC_LOOPBACK;
    else if (kind == "process") kind_value = ZX_SRC_PROCESS;

    // 组装请求 JSON。字段名与 zongxian_media.h 里文档化的一致（驼峰）。
    char req[1024];
    std::snprintf(req, sizeof(req),
                  "{\"processId\":%lld,\"includeChildProcesses\":%s,"
                  "\"wantChannels\":%d,\"deviceId\":\"%s\",\"wantSampleRate\":%d}",
                  static_cast<long long>(pid), children ? "true" : "false", channels,
                  device.c_str(), want_rate);

    auto t_start = std::chrono::steady_clock::now();

    Say("[1/6] 打开采集源 kind=%s device=%s wantRate=%d ...\n", kind.c_str(),
        device.empty() ? "(默认)" : device.c_str(), want_rate);
    zx_source_t* source = nullptr;
    zx_result rc = zx_source_open(req, static_cast<zx_source_kind>(kind_value), &source);
    if (rc != ZX_OK || !source) {
        std::printf("打开采集源失败 (rc=%d)\n", static_cast<int>(rc));
        PrintError();
        return 2;
    }

    // 主音量：真的会缩放输出（验证它不是"只写不读"的假开关）
    if (volume >= 0) {
        zx_engine_t* engine = nullptr;
        if (zx_engine_create(&engine) == ZX_OK && engine) {
            const zx_result vrc = zx_engine_set_master_volume(engine, static_cast<float>(volume_f));
            Say("[vol ] 主音量 %.2f -> rc=%d（zx_source_read 交付前会乘上它）\n",
                volume_f, static_cast<int>(vrc));
            if (vrc != ZX_OK) PrintError();
            zx_engine_destroy(engine);
        } else {
            std::printf("创建 engine 句柄失败，主音量未设置\n");
            PrintError();
        }
    }

    // 查实际生效的格式：请求的格式和生效的格式可能不同（设备说了算）
    char fmt_buf[512] = {0};
    int channels_effective = 1;
    int rate_effective = ZX_SAMPLE_RATE;   // 兜底：只有查不到格式时才会用到
    if (zx_source_format(source, fmt_buf, sizeof(fmt_buf)) == ZX_OK) {
        Say("[2/6] 实际格式：%s\n", fmt_buf);
        // 从 JSON 里取声道数与**实际采样率**。声道数必须用实际值来算缓冲大小 ——
        // 引擎按「帧 × 声道」写数据，只按帧数开缓冲会溢出（立体声要 2 倍空间），
        // 而且崩在堆上、离原因很远。
        // 采样率必须用实际值来算时长 —— 硬算 48000 在 44.1k 设备上就是静默变调 + 时长错。
        const int c = ParseFormatInt(fmt_buf, "channels", 0);
        if (c > 0 && c <= 8) channels_effective = c;
        rate_effective = ParseFormatInt(fmt_buf, "sampleRate", ZX_SAMPLE_RATE);
        Say("       本次按 %d 声道开缓冲；时长换算用**实际**采样率 %d Hz\n",
            channels_effective, rate_effective);
    }

    // 录音：raw 与 processed 同时开，保证两份文件严格同源同时刻
    Say("[3/6] 开始录音 raw=%s out=%s ...\n", raw.c_str(), out.c_str());
    rc = zx_recorder_open(source, raw.c_str(), out.c_str());
    if (rc != ZX_OK) {
        std::printf("无法开始录音\n");
        PrintError();
        zx_source_close(source);
        return 2;
    }

    rc = zx_ns_set(source, static_cast<zx_ns_level>(ns), ZX_NSB_BUILTIN);
    if (rc != ZX_OK) { PrintError(); }
    zx_ns_set_bypass(source, nsbypass ? 1 : 0);
    Say("[4/6] 启动采集 ...\n");
    rc = zx_source_start(source);
    if (rc != ZX_OK) {
        std::printf("启动采集失败\n");
        PrintError();
        zx_recorder_close(source);
        zx_source_close(source);
        return 2;
    }

    Say("[5/6] 采集中 %d 秒（降噪档位 %d%s）...\n", seconds, ns,
        nsbypass ? " 已旁路" : "");

    // 拉数据。这里是「拉」模型：引擎内部采集线程只往环形缓冲写，
    // 节奏由我们决定。真实应用里这一步在 Opus 编码器前面。
    //
    // 两个关于缓冲的硬性要求（都踩过）：
    //   1) 缓冲必须按 **帧数 × 声道数** 分配。立体声要 2 倍空间，
    //      只按帧数开会溢出到堆上，表现为毫无线索的访问冲突。
    //   2) 结束条件必须用**真实墙钟**。早期版本用 elapsed_ms += 200 计数，
    //      而 zx_source_read 有数据就立刻返回（通常只要 10ms），于是循环
    //      30 次就结束，"录 5 秒"实际只录到 0.3 秒音频。这类错误不报错，
    //      只会让人误判成"采集丢数据"。
    // 一次 200ms；上限按**实际采样率**算（引擎侧同一个规则），不再写死 9600。
    const int read_frames = rate_effective / 5;
    std::vector<float> block(
        static_cast<size_t>(read_frames) * static_cast<size_t>(channels_effective));
    const auto deadline =
        std::chrono::steady_clock::now() + std::chrono::seconds(seconds);
    uint64_t frames_total = 0;
    uint64_t reads_ok = 0;
    uint64_t reads_timeout = 0;
    auto last_report = std::chrono::steady_clock::now();
    const auto t_begin = std::chrono::steady_clock::now();

    while (std::chrono::steady_clock::now() < deadline) {
        int got = 0;
        rc = zx_source_read(source, block.data(), read_frames, &got, 200);
        if (rc == ZX_OK && got > 0) {
            frames_total += static_cast<uint64_t>(got);
            ++reads_ok;
            // 前几次读取打印实际帧数：用来确认 block 的容量与声道数是否吻合。
            // read_frames 是"帧"上限，而 block 按 帧×声道 分配，两者必须对得上。
            if (reads_ok <= 3) {
                Say("  [读#%llu] got=%d 帧（缓冲上限 %d 帧 × %d 声道）\n",
                    static_cast<unsigned long long>(reads_ok), got, read_frames,
                    channels_effective);
            }
        } else if (rc == ZX_ERR_TIMEOUT) {
            ++reads_timeout;
        } else {
            std::printf("读取失败 rc=%d\n", static_cast<int>(rc));
            PrintError();
            break;
        }

        // 每秒打一个进度 + 实时电平，方便确认"确实在收到声音"
        const auto now = std::chrono::steady_clock::now();
        if (now - last_report >= std::chrono::seconds(1)) {
            last_report = now;
            const double elapsed =
                std::chrono::duration<double>(now - t_begin).count();
            char meter[256] = {0};
            zx_source_meter(source, meter, sizeof(meter));
            std::printf("\r  已采集 %.1f 秒  音频 %.2f 秒（@%d Hz）  电平 %s          ",
                        elapsed, FramesToSeconds(frames_total, rate_effective),
                        rate_effective, meter);
            std::fflush(stdout);
        }
    }
    std::printf("\n");

    // 取统计：这些数字是 PL0 的验收依据
    Say("[6/6] 收尾 ...\n");
    char stats[512] = {0};
    zx_source_stats(source, stats, sizeof(stats));
    char nscfg[512] = {0};
    zx_ns_get_config(source, nscfg, sizeof(nscfg));
    char recstats[512] = {0};
    zx_recorder_stats(source, recstats, sizeof(recstats));

    zx_source_stop(source);
    zx_recorder_close(source);
    zx_source_close(source);

    const double wall_s =
        std::chrono::duration<double>(std::chrono::steady_clock::now() - t_start).count();

    std::printf("\n--- 采集统计 ---\n%s\n", stats);
    std::printf("--- 降噪状态 ---\n%s\n", nscfg);
    std::printf("--- 录音统计 ---\n%s\n", recstats);
    std::printf("--- 本次运行 ---\n");
    std::printf("  读到 %llu 次数据 / %llu 次超时；共 %llu 帧（%.2f 秒音频 @ 实际 %d Hz）\n",
                static_cast<unsigned long long>(reads_ok),
                static_cast<unsigned long long>(reads_timeout),
                static_cast<unsigned long long>(frames_total),
                FramesToSeconds(frames_total, rate_effective), rate_effective);
    std::printf("  墙钟 %.1f 秒，音频/墙钟 = %.3f（应≈1.0，明显小于 1 说明丢数据）\n",
                wall_s,
                FramesToSeconds(frames_total, rate_effective) / (wall_s > 0 ? wall_s : 1.0));
    std::printf("  【对账】输出头采样率 %d Hz == 时长换算所用采样率 %d Hz -> %s\n",
                rate_effective, rate_effective, "一致");
    std::printf("\n产出文件：\n  降噪前: %s\n  降噪后: %s\n", raw.c_str(), out.c_str());
    std::printf("请用播放器对比听：底噪应明显变小，人声不应出现「水声」或失真。\n");
    return 0;
}

}  // namespace

// ---------------------------------------------------------------------------
// stream：把采集到的 PCM 以 **float32 小端交叉**的裸流写到 stdout。
//
// 用途：这就是「共享电脑声音」的采集端 —— 应用侧（C#）读这个流，交给页面
// 生成一条**独立音频轨**发给对端，与麦克风语音、屏幕共享都互不干扰。
//
// 约定（应用侧按这个解析，必须一致）：
//   · stdout 只放 PCM，不放任何文字（日志一律走 stderr）
//   · 采样格式 float32 / 小端 / 交叉
//   · 【2026-10-06 修正】采样率**不是常量 48k**：以 stderr 头里打印的
//     rate 为准（= zx_source_format 的 sampleRate），消费者必须按它解析，
//     否则 44.1k 设备上就是静默变调（审计真 bug 11，旧注释写死 48kHz 是错的）。
//     --expect-rate（默认 48000）用来声明消费者期望的值；实际不一致时
//     默认**大声警告**，加 --strict-rate 1 则直接以 rc=4 失败（宁可不出声也不变调）。
//   · 停止：父进程关管道或杀进程；也支持 --seconds 限时（便于自测）
// ---------------------------------------------------------------------------
int CmdStream(int argc, char** argv) {
    // 【必须】Windows 上 stdout 默认是**文本模式**：二进制 PCM 里的 0x0A 会被翻译成
    // 0x0D 0x0A，流被撑长、字节错位 —— 表现为读出来全是异常浮点值。
    // 实测：修前 14.59% 样本越界、文件长度不是 4 的倍数；修后生产端/文件端都是 0%。
#ifdef _WIN32
    _setmode(_fileno(stdout), _O_BINARY);
#endif
    const std::string kind = ArgStr(argc, argv, "--kind", "loopback");
    const long long pid = ArgInt(argc, argv, "--pid", 0);
    const int seconds = static_cast<int>(ArgInt(argc, argv, "--seconds", 0));
    const int ns = static_cast<int>(ArgInt(argc, argv, "--ns", -1));
    const int nsbypass = static_cast<int>(ArgInt(argc, argv, "--nsbypass", 0));
    const bool children = std::string(ArgStr(argc, argv, "--children", "1")) != "0";
    const int want_rate = static_cast<int>(ArgInt(argc, argv, "--want-rate", ZX_SAMPLE_RATE));
    const int expect_rate = static_cast<int>(ArgInt(argc, argv, "--expect-rate", ZX_SAMPLE_RATE));
    const bool strict_rate = ArgInt(argc, argv, "--strict-rate", 0) != 0;
    const double volume = ArgFloat(argc, argv, "--volume", -1.0);

    int kind_value = ZX_SRC_LOOPBACK;
    if (kind == "mic") kind_value = ZX_SRC_MIC;
    else if (kind == "process") kind_value = ZX_SRC_PROCESS;

    char req[768];
    std::snprintf(req, sizeof(req),
                  "{\"processId\":%lld,\"includeChildProcesses\":%s,"
                  "\"wantChannels\":2,\"wantSampleRate\":%d}",
                  static_cast<long long>(pid), children ? "true" : "false", want_rate);

    zx_source_t* source = nullptr;
    zx_result rc = zx_source_open(req, static_cast<zx_source_kind>(kind_value), &source);
    if (rc != ZX_OK || !source) {
        Say("[stream] 打开采集源失败 rc=%d\n", static_cast<int>(rc));
        PrintError();
        return 2;
    }

    if (volume >= 0) {
        zx_engine_t* engine = nullptr;
        if (zx_engine_create(&engine) == ZX_OK && engine) {
            const zx_result vrc =
                zx_engine_set_master_volume(engine, static_cast<float>(volume));
            std::fprintf(stderr, "[stream] 主音量 %.2f -> rc=%d\n", volume,
                         static_cast<int>(vrc));
            zx_engine_destroy(engine);
        }
    }

    char fmt_buf[512] = {0};
    int channels = 2;
    int rate = ZX_SAMPLE_RATE;   // 会在下面被实际值覆盖；仅当查格式失败时兜底
    if (zx_source_format(source, fmt_buf, sizeof(fmt_buf)) == ZX_OK) {
        std::fprintf(stderr, "[stream] 实际格式：%s\n", fmt_buf);
        const int c = ParseFormatInt(fmt_buf, "channels", 0);
        if (c > 0 && c <= 8) channels = c;
        rate = ParseFormatInt(fmt_buf, "sampleRate", ZX_SAMPLE_RATE);
        // 只要不是 float32 就明确报错，绝不猜 —— 猜错会让对端听到噪音
        if (std::strstr(fmt_buf, "\"isFloat\":true") == nullptr ||
            std::strstr(fmt_buf, "\"bitsPerSample\":32") == nullptr) {
            std::fprintf(stderr,
                         "[stream] 该采集源的格式不是 float32，本命令不支持（避免传出噪音）\n");
            zx_source_close(source);
            return 3;
        }
    }

    // 采样率不一致的处理：默认警告；--strict-rate 1 时失败退出。
    // 这一步的存在就是为了让"静默变调"再也发生不了。
    if (rate != expect_rate) {
        std::fprintf(stderr,
                     "[stream] 警告：实际采样率 %d Hz != 期望 %d Hz —— "
                     "消费者必须按 %d Hz 解析，否则会变调/时长错。\n",
                     rate, expect_rate, rate);
        if (strict_rate) {
            std::fprintf(stderr, "[stream] --strict-rate 1：拒绝在不一致的采样率下输出\n");
            zx_source_close(source);
            return 4;
        }
    }

    rc = zx_source_start(source);
    if (rc != ZX_OK) {
        std::fprintf(stderr, "[stream] 启动采集失败\n");
        PrintError();
        zx_source_close(source);
        return 2;
    }
    // 降噪档位：只有显式传 --ns 才覆盖引擎默认（mic 降噪 / 内容源直通）
    if (ns >= 0) {
        zx_ns_set(source, static_cast<zx_ns_level>(ns), ZX_NSB_BUILTIN);
    }
    zx_ns_set_bypass(source, nsbypass ? 1 : 0);
    std::fprintf(stderr,
                 "[stream] 开始输出 PCM（float32 / %d 声道 / %d Hz 实际采样率）\n",
                 channels, rate);
    std::fflush(stderr);

    const int read_frames = rate / 10;   // 100ms 一批（按实际采样率算）
    std::vector<float> block(static_cast<size_t>(read_frames) * channels);
    const auto deadline = seconds > 0
        ? std::chrono::steady_clock::now() + std::chrono::seconds(seconds)
        : std::chrono::steady_clock::time_point::max();
    uint64_t frames_total = 0;
    int reads_done = 0;   // 一次性探针用：只打印前几次读取的内容
    // 【生产端统计】不从写出去的文件反推，直接在"数据还在手里"时统计 ——
    // 文件/重定向/解析这一层有太多假象（实测踩过两次）。
    uint64_t samples_total = 0;
    uint64_t samples_bad = 0;
    float max_abs = 0.0f;

    while (std::chrono::steady_clock::now() < deadline) {
        int got = 0;
        rc = zx_source_read(source, block.data(), read_frames, &got, 200);
        if (rc == ZX_OK && got > 0) {
            const size_t bytes = static_cast<size_t>(got) * channels * sizeof(float);
            // 【一次性探针】看前几次读取的实际内容：如果出现 FLT_MAX(3.4e38) 这种哨兵值，
            // 说明环形缓冲里有"没写过的槽位"被当成有效数据读出来了（不是命令行的问题）。
            if (reads_done < 3) {
                std::fprintf(stderr, "[stream] 读#%d got=%d 帧 字节=%zu 头6个=%g %g %g %g %g %g\n",
                             reads_done + 1, got, bytes,
                             static_cast<double>(block[0]), static_cast<double>(block[1]),
                             static_cast<double>(block[2]), static_cast<double>(block[3]),
                             static_cast<double>(block[4]), static_cast<double>(block[5]));
                std::fflush(stderr);
            }
            ++reads_done;
            {
                const size_t nsamp = static_cast<size_t>(got) * static_cast<size_t>(channels);
                for (size_t i = 0; i < nsamp; ++i) {
                    const float v = block[i];
                    if (!(v >= -1.0f && v <= 1.0f)) {   // NaN 也会落到这里
                        ++samples_bad;
                    } else {
                        if (v > max_abs) max_abs = v;
                        if (-v > max_abs) max_abs = -v;
                    }
                }
                samples_total += nsamp;
            }
            // 【必须循环写】fwrite 在管道上可能**只写一部分**（管道缓冲区满时），
            // 早先把它当成"管道断了"直接 break —— 结果是流尾部丢数据，
            // 总长度还不是帧长的整数倍（实测 385,188 字节 %8 = 4）。
            // 只有真的写不进去（返回 0 / 出错）才算管道断。
            size_t written = 0;
            bool pipe_gone = false;
            while (written < bytes) {
                const size_t n = std::fwrite(block.data() + written / sizeof(float),
                                            1, bytes - written, stdout);
                if (n == 0) { pipe_gone = true; break; }
                written += n;
            }
            if (pipe_gone) {
                break;   // 父进程不要了，正常收工
            }
            std::fflush(stdout);
            frames_total += static_cast<uint64_t>(got);
        } else if (rc == ZX_ERR_TIMEOUT) {
            continue;
        } else {
            std::fprintf(stderr, "[stream] 读取失败 rc=%d\n", static_cast<int>(rc));
            break;
        }
    }

    zx_source_stop(source);
    zx_source_close(source);
    std::fprintf(stderr,
                 "[stream] 生产端统计：样本 %llu，越界 %llu（%.2f%%），最大 |v| = %.4f\n",
                 static_cast<unsigned long long>(samples_total),
                 static_cast<unsigned long long>(samples_bad),
                 samples_total ? 100.0 * static_cast<double>(samples_bad) /
                                     static_cast<double>(samples_total) : 0.0,
                 static_cast<double>(max_abs));
    // 时长用**实际采样率**换算（以前写死 ZX_SAMPLE_RATE：44.1k 上时长错 8.8%）
    std::fprintf(stderr, "[stream] 结束：共输出 %llu 帧（%.2f 秒 @ 实际 %d Hz）\n",
                 static_cast<unsigned long long>(frames_total),
                 FramesToSeconds(frames_total, rate), rate);

    // 机器可读的一行：给打包后的自动化对账用（stderr，不会污染 PCM）
    std::fprintf(stderr,
                 "[stream-rate] actual=%d expected=%d channels=%d seconds=%.6f frames=%llu\n",
                 rate, expect_rate, channels, FramesToSeconds(frames_total, rate),
                 static_cast<unsigned long long>(frames_total));
    return 0;
}

int main(int argc, char** argv) {
#ifdef _WIN32
    // 控制台输出用 UTF-8，否则中文日志会乱码
    ::SetConsoleOutputCP(CP_UTF8);
#endif

    if (argc < 2) { Usage(); return 1; }

    char log_path[512];
    std::snprintf(log_path, sizeof(log_path), "%s/pl0-probe.log", kOutDir);
    // --log-level 直接进 zx_media_init 的 logLevel：用来验证它**真的在过滤**
    // （审计 §四 记的"解析后丢弃"假开关）。默认 debug 便于排障。
    const std::string log_level = ArgStr(argc, argv, "--log-level", "debug");
    char init_json[768];
    std::snprintf(init_json, sizeof(init_json),
                  "{\"logPath\":\"%s\",\"logLevel\":\"%s\",\"logRingLines\":4000}",
                  log_path, log_level.c_str());

    if (zx_media_init(init_json) != ZX_OK) {
        std::printf("引擎初始化失败：%s\n", zx_last_error());
        return 2;
    }

    const std::string cmd = argv[1];
    int rc = 0;
    if (cmd == "devices")             rc = CmdDevices();
    else if (cmd == "loopback-probe") rc = CmdLoopbackProbe();
    else if (cmd == "apps")           rc = CmdApps();
    else if (cmd == "rate-check")     rc = CmdRateCheck();
    else if (cmd == "ring-check")     rc = CmdRingCheck();
    else if (cmd == "record")         rc = CmdRecord(argc, argv);
    else if (cmd == "stream")         rc = CmdStream(argc, argv);
    else { Usage(); rc = 1; }

    zx_media_dump_log(log_path);
    zx_media_shutdown();
    return rc;
}
