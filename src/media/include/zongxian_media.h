/* zongxian_media.h —— 棕仙语音 媒体引擎的 C ABI（唯一对外接口）
 *
 * 设计原则（改动前先读这一节，这几条是刻意的取舍）
 * ---------------------------------------------------------------------------
 * 1. 这是 Python（ctypes）与 C++ 引擎之间**唯一**的边界。任何新功能都必须
 *    在此文件里以 C 函数的形式出现，不要图省事让 Python 直接碰 C++ 类。
 *    边界越窄，出问题时越容易定位。
 *
 * 2. 结构化数据一律用 **UTF-8 JSON**（camelCase 键名）。理由：跨语言传结构体
 *    会碰上 ABI/对齐/枚举宽度/编译器差异，JSON 一次也不会有这些问题。
 *    但**热路径不走 JSON**：音频样本以裸缓冲区传递，不做任何形式转换。
 *    （音频每秒 50 个 20ms 帧，每帧 960 样本；任何按样本的 JSON 都是灾难。）
 *
 * 3. 所有函数返回 zx_result 或一个**精确的样本数**，绝不用「大概的字节数」。
 *    时间就是采样数：延迟、抖动、AEC 对齐全部以样本为单位计算，可以精确复现。
 *
 * 4. 错误信息通过 zx_last_error() 取，**仅当前线程**有效，取走后清空。
 *
 * 5. 内存所有权：凡是以 char** 返回的字符串都由引擎分配，必须用
 *    zx_free_string() 释放。句柄（zx_engine_t* / zx_session_t*）用对应的
 *    _close/_destroy 释放，且必须是**幂等**的（重复关闭不崩）。
 *
 * 6. 线程模型：调用者可以在任意线程调用这些函数（内部各自加锁）。
 *    唯独**回调**要小心 —— 见各自函数的注释。当前尚不回传任何回调给
 *    Python：事件全部通过 zx_*_poll_event() 主动拉取。这是刻意的：
 *    在 ctypes 回调里进 Python 解释器会在实时音频线程上踩 GIL，
 *    是造成爆音和 AEC 失效的经典原因，不要为了少写几行轮询就改掉。
 *    （C 侧 zx_source_set_callback 是通的，只是 Python 绑定刻意不暴露它。）
 *
 * 7. 日志：引擎内部写自己的日志文件，路径由 zx_media_init 的 json 指定。
 *    日志是环形缓冲 + 可一键导出，不依赖调用者。
 *    "logLevel"（trace|debug|info|warn|error）是**真过滤器**，不是摆设。
 *
 * 8. 并发与生命周期契约（2026-10-06 补写，别再只写在实现注释里）：
 *    · Start / Stop / Close **不得**与 zx_source_read 并发。
 *      引擎内部用共享/独占锁串行化（close 会等正在读的那次调用返回），
 *      但"一边读一边关"仍是调用方的用法错误：关掉后句柄立刻失效，
 *      后续调用返回 ZX_ERR_INVALID_ARG。
 *    · 同一个源的 Read 与 zx_ns_* / zx_recorder_* 由内部互斥锁串行化，
 *      配置变更不会与处理中的一块数据交叉（那会造成偶发爆音）。
 *    · zx_source_stats / zx_source_meter / zx_source_format 可随时调用：
 *      实现必须给出一致快照（不撕裂），且不阻塞数据流。
 *    · zx_source_set_callback：只能在 Start 之前设置、Stop 之后清除。
 *
 * 9. 单位契约：热路径上「帧」与「样本(float)」是两个不同的单位，
 *    帧 = 每声道一个样本。C ABI 这里以**帧**为单位（zx_source_read 的
 *    cap_frames / out_frames 都是帧）；引擎内部的 RingBuffer 一律按
 *    **样本** 计数，统计键名也照样本命名（bufferedSamples / sanitizedSamples）。
 *
 * 版本与兼容：ZX_MEDIA_ABI_VERSION 在 ABI 不兼容变更时必须 +1。
 * Python 侧启动时会校验，不匹配直接报错，不做任何猜测性兼容。
 */

#ifndef ZONGXIAN_MEDIA_H
#define ZONGXIAN_MEDIA_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#if defined(_WIN32)
#  define ZX_API __declspec(dllexport)
#else
#  define ZX_API __attribute__((visibility("default")))
#endif

/* ------------------------------------------------------------------ */
/* 版本与常量                                                          */
/* ------------------------------------------------------------------ */

#define ZX_MEDIA_ABI_VERSION 1

/* 全局首选采样率。它是**请求值/默认值**，不是"实际一定是这个"：
 * 实际生效的采样率必须用 zx_source_format() 查（键 "sampleRate"），
 * 设备混音格式的原始采样率在 "deviceSampleRate"，是否被重采样在 "resampled"。
 * 【2026-10-06 定死】任何按帧数算时长/重采样比的地方都必须用查到的实际值，
 * 不许再写 /48000.0 —— 44.1k 设备上那会静默变调 + 时长错（审计真 bug 11）。 */
#define ZX_SAMPLE_RATE 48000

/* 采集/处理块长：10 ms。WASAPI 周期与 RNNoise 内部帧长都恰好是 10 ms。 */
#define ZX_BLOCK_FRAMES 480

/* 音频内部一律 32-bit float 交错。只有写 WAV 或喂 Opus 时才转 16-bit。 */
#define ZX_SAMPLE_BYTES 4

/* 设备名/JSON 缓冲的建议大小。超出会被截断并返回 ZX_ERR_BUFFER_TOO_SMALL。 */
#define ZX_NAME_MAX 512

/* ------------------------------------------------------------------ */
/* 结果码                                                              */
/* ------------------------------------------------------------------ */

typedef enum zx_result {
    ZX_OK                    =  0,
    ZX_ERR_INVALID_ARG       = -1,  /* 参数不对（空指针、负数、非法枚举） */
    ZX_ERR_NOT_INITIALIZED   = -2,  /* 忘了先调 zx_media_init */
    ZX_ERR_NO_DEVICE         = -3,  /* 找不到设备 / 设备被拔了 */
    ZX_ERR_DEVICE_IN_USE     = -4,
    ZX_ERR_UNSUPPORTED       = -5,  /* 本机不支持（如老系统上的进程回环） */
    ZX_ERR_BACKEND_FAILED    = -6,  /* WASAPI 调用失败，细节看 zx_last_error */
    ZX_ERR_BUFFER_TOO_SMALL  = -7,
    ZX_ERR_TIMEOUT           = -8,  /* 本块内没有数据，不是错误，稍后再拉 */
    ZX_ERR_NO_DATA           = -9,
    ZX_ERR_CLOSED            = -10, /* 句柄已关闭 */
    ZX_ERR_INTERNAL          = -11,
} zx_result;

/* ------------------------------------------------------------------ */
/* 不透明句柄                                                          */
/* ------------------------------------------------------------------ */

typedef struct zx_engine  zx_engine_t;
typedef struct zx_source  zx_source_t;
typedef struct zx_session zx_session_t;

/* 采集源的类型。数值进 JSON，不要随意改。 */
typedef enum zx_source_kind {
    ZX_SRC_MIC        = 0,  /* 麦克风（人声，后接 AEC/NS/AGC） */
    ZX_SRC_LOOPBACK   = 1,  /* 全部应用音频（WASAPI loopback，Vista+） */
    ZX_SRC_PROCESS    = 2,  /* 单个应用音频（进程回环，Win10 20348+ / 实测 2004+） */
} zx_source_kind;

/* 降噪档位。用户界面上的「关 / 轻 / 中 / 强」直接对应这四个值。 */
typedef enum zx_ns_level {
    ZX_NS_OFF      = 0,
    ZX_NS_LIGHT    = 1,
    ZX_NS_MODERATE = 2,   /* 默认 */
    ZX_NS_STRONG   = 3,
} zx_ns_level;

/* 处理链里的算法实现。用于对比 A/B，也可在 RNNoise 缺失时降级。 */
typedef enum zx_ns_backend {
    ZX_NSB_BUILTIN = 0,   /* 引擎自带：直流阻断 + 二阶高通 + 谱减 + 噪声门。零依赖。 */
    ZX_NSB_RNNOISE = 1,   /* RNNoise（若编译时带进来；否则自动降级到 BUILTIN） */
} zx_ns_backend;

/* ------------------------------------------------------------------ */
/* 生命周期                                                            */
/* ------------------------------------------------------------------ */

/* 全局初始化。json 可为 NULL，字段（全部可选，驼峰命名）：
 *     { "logPath":      "D:/logs/media.log",   // 省略则写临时目录
 *       "logLevel":     "info",                // trace|debug|info|warn|error
 *       "logRingLines": 4000,                  // 环形缓冲行数，便于一键导出
 *       "backend":      "wasapi" }             // 目前只支持 "wasapi"
 * 可重复调用；已初始化时返回 ZX_OK 且忽略新配置（幂等）。
 */
ZX_API zx_result zx_media_init(const char* json_config);

/* 反初始化。会停止并关闭所有仍打开的会话与源。可与 zx_media_init 交替调用。 */
ZX_API void zx_media_shutdown(void);

ZX_API int zx_media_abi_version(void);       /* == ZX_MEDIA_ABI_VERSION */
ZX_API const char* zx_media_version(void);   /* 引擎版本号，静态字符串，不需释放 */

/* ------------------------------------------------------------------ */
/* 诊断                                                                */
/* ------------------------------------------------------------------ */

/* 取当前线程最近一次错误的人话说明（UTF-8）。无错误时返回空字符串。
 * 调用后错误被清空。返回的指针在下次调用前有效。 */
ZX_API const char* zx_last_error(void);

/* 把日志环形缓冲整体写入 path。出问题时让用户点一下这个，比来回猜快得多。 */
ZX_API zx_result zx_media_dump_log(const char* path_utf8);

/* ------------------------------------------------------------------ */
/* 设备枚举与能力探测                                                  */
/* ------------------------------------------------------------------ */

/* 列出采集设备。out_json 收到的形如：
 *   [ { "id":"{0.0.0.00000000}.{...}", "name":"麦克风 (USB Audio)",
 *       "kind":"mic", "isDefault":true, "channels":2, "sampleRate":48000 } ]
 * 注意 "id" 是不透明的设备标识串，直接原样回传给 zx_source_open。
 * 返回所需缓冲长度（含结尾 \0）；若 > cap，则内容被截断且返回 ZX_ERR_BUFFER_TOO_SMALL。
 */
ZX_API int zx_list_capture_devices(char* out_json, int cap);

/* 列出渲染（播放）设备，用于「从哪个音箱出声」的选择。 */
ZX_API int zx_list_render_devices(char* out_json, int cap);

/* 探测本机是否有「单个应用音频」能力（进程回环）。
 *   out_supported 收到 0/1；out_detail 可传 NULL，收到人话说明（失败原因）。
 *
 * 【2026-10-06 更正，旧说明已作废】
 *   旧版本这里写着"默认构建里不是真实探测、线上一直返回写死的'不支持'，
 *   由 ZX_ENABLE_PROCESS_LOOPBACK_PROBE 控制"。**这条已作废**：
 *     · 该宏已不控制任何分支（wasapi_backend.cpp 里查不到它）；
 *     · ProbeProcessLoopback() 现在**默认执行真实探测**
 *       （真的调一次 ActivateAudioInterfaceAsync），见 wasapi_backend.cpp。
 *   真正的崩溃真因也不是"系统 API 的锅"，而是当时在激活之后对指向
 *   **栈变量**的 VT_BLOB 调了 PropVariantClear → CoTaskMemFree(栈指针) →
 *   堆损坏 0xC0000374。删掉那行之后真实探测返回 rc=0 / supported=1，
 *   所以"必须子进程隔离"的结论同样作废（详见 src/media/STATUS.md §4.3）。
 *
 *   仍然成立的一点：堆损坏走 fast-fail，SEH 拦不住；SEH 兜底只针对普通异常。
 */
ZX_API zx_result zx_probe_process_loopback(int* out_supported,
                                           char* out_detail, int detail_cap);

/* 列出当前有音频会话（正在发声或曾经发声）的进程，供「只共享某个应用」选。
 * 形如：[ { "pid":1234, "name":"chrome.exe", "title":"...", "active":true } ]
 * 不支持的平台上返回长度为 2 的 "[]"（不是错误）。
 */
ZX_API int zx_list_audio_processes(char* out_json, int cap);

/* ------------------------------------------------------------------ */
/* 引擎对象                                                            */
/* ------------------------------------------------------------------ */

ZX_API zx_result zx_engine_create(zx_engine_t** out_engine);
ZX_API void      zx_engine_destroy(zx_engine_t* engine);   /* 幂等；NULL 安全 */

/* 主音量（0.0 ~ 1.0）。**真的生效**：zx_source_read() 交付样本前会统一乘上它
 * （录音的 raw 那一路除外 —— raw 永远是原始电平）。
 * 语义：进程级设置，所有 engine 句柄共享同一个值，后设置的生效；
 * 影响所有随后被拉取的源数据。1.0 = 不缩放。
 * 【2026-10-06】此前它只写不读（Python 调了等于没调，审计 §四 的假开关之一）。 */
ZX_API zx_result zx_engine_set_master_volume(zx_engine_t* engine, float volume_0_to_1);

/* ------------------------------------------------------------------ */
/* 采集源                                                              */
/* ------------------------------------------------------------------ */

/* 打开一个采集源。json 字段（全部可选，省略则用默认）：
 *     { "kind":          "mic" | "loopback" | "process",
 *       "deviceId":      "{0.0.0.00000000}.{...}",   // 省略 = 系统默认设备
 *       "processId":     1234,                        // kind=process 时必填
 *       "includeChildProcesses": true,                // 强烈建议 true！Chrome/Electron
 *                                                     // 的音频在子进程里，只抓主进程会断续
 *       "wantChannels":  1,                           // 麦克风用 1，共享音频用 2
 *       "blockFrames":   480,                         // 10 ms，别改
 *       "wantSampleRate":48000 }                      // 期望采样率（可省）
 * 打开后源处于停止状态，需要用 zx_source_start() 启动。
 * 采样率：wantSampleRate 只是**请求**。端点采集（mic）在设备混音格式不是它时
 * 会请求系统重采样（AUTOCONVERTPCM）；设备不支持就回退。无论哪条路，
 * 都以 zx_source_format() 报出的 sampleRate 为**实际值**，调用方不许假设 48000。
 * 进程回环（kind=process）的格式由系统定死，请求采样率不适用。
 *
 * 注意：源**不**从属于 engine 句柄，自己管自己的生命周期。调用前只需保证
 * zx_media_init() 已成功。多条源可以并存（例如同时开麦克风 + 全部应用音频），
 * 每一条独立采集、独立降噪、独立录音。
 */
ZX_API zx_result zx_source_open(const char* json_request,
                                zx_source_kind kind, zx_source_t** out_source);

ZX_API zx_result zx_source_start(zx_source_t* source);
ZX_API zx_result zx_source_stop(zx_source_t* source);
ZX_API void       zx_source_close(zx_source_t* source);   /* 幂等；NULL 安全 */

/* 该源实际生效的格式（可能与请求不同，WASAPI 混音格式是设备说了算）。
 * 形如：{ "sampleRate":44100, "deviceSampleRate":44100, "resampled":false,
 *         "channels":2, "bitsPerSample":32,
 *         "isFloat":true, "blockFrames":480, "periodMs":10.0 }
 * sampleRate 是**实际交付**的采样率；deviceSampleRate 是设备混音格式的原始值；
 * resampled=true 表示 WASAPI 已把 deviceSampleRate 重采样到 sampleRate。
 * 算时长、算重采样比、写 WAV 头，一律用 sampleRate。 */
ZX_API zx_result zx_source_format(zx_source_t* source, char* out_json, int cap);

/* 从源里取出已采集的音频（阻塞最多 timeout_ms 毫秒）。
 *   out_interleaved : 调用者提供的缓冲，float32 交错
 *   cap_frames      : 缓冲可容纳的**帧数**（帧 = 每声道一个样本；
 *                     缓冲要有 cap_frames × channels 个 float）
 *   out_frames      : 实际写入的帧数
 * 返回 ZX_OK 表示取到数据；ZX_ERR_TIMEOUT 表示这段时间内没有新数据（不是错误）。
 * 一次最多 200 ms（上限按该源**实际采样率**算，不再是写死的 9600 帧）。
 * 这是刻意设计的「拉」模型：实时线程只往环形缓冲写，取数据由调用者决定节奏。
 * 【并发】不得与 Start/Stop/Close 并发调用，见文件头 §8。
 */
ZX_API zx_result zx_source_read(zx_source_t* source, float* out_interleaved,
                                int cap_frames, int* out_frames, int timeout_ms);

/* 该源的实时电平（用于界面上的音量条与说话高亮）。
 * 形如：{ "rms":0.031, "peak":0.42, "dbfs":-30.2, "voiced":false, "clipped":false }
 * 可以随时调用，不会阻塞，也不影响数据流。 */
ZX_API zx_result zx_source_meter(zx_source_t* source, char* out_json, int cap);

/* 采集统计。用于验收与排障，形如：
 *   { "framesCaptured":480000, "framesDropped":0, "discontinuities":0,
 *     "silentPackets":0, "avgPeriodMs":10.02, "maxPeriodMs":11.4,
 *     "bufferedSamples":960, "callbackCount":1000,
 *     "sanitizedSamples":0, "proAudio":true }
 * framesDropped / discontinuities 是判断「10 ms 周期是否真的稳」的关键指标。
 * 【2026-10-06 键名更正】原来叫 bufferedFrames，但那个数字一直是**样本(float)**
 * 数（RingBuffer 的单位），改名为 bufferedSamples 以免"谁照文档改谁引爆"。
 * sanitizedSamples = 采集线程上被判为越界/NaN 而被消毒成静音的样本数；
 * proAudio = 采集线程是否真的注册进了 MMCSS "Pro Audio"。
 * 可以随时调用：实现给出一致快照，不会读到撕裂的计数。
 */
ZX_API zx_result zx_source_stats(zx_source_t* source, char* out_json, int cap);

/* 采集回调（C 函数指针）。在**实时音频线程**上被调用。
 * 实现里只允许：拷内存、算 RMS、写环形缓冲。
 * 禁止：分配内存、加锁（会与其他线程争）、写文件、调用任何语言运行时（含 Python）。
 * 【2026-10-06 核实】这条链路是**通的**：media_api 的 zx_source_set_callback →
 * AudioSource::set_callback → 采集线程 OnSamples() → 回调。
 * 【消费者现状 · 保留 ABI】仓库里**当前没有任何消费者**调用它：C# 侧不调，
 * Python 绑定刻意不暴露（见文件头 §6 的 GIL 理由）。保留它只是为了钉住
 * ABI（删掉会改变导出表），不是"已接上的功能"。将来谁要接，必须先满足
 * 上面的实时线程禁令，并在接上之前把这一行改成消费者的名字。
 * 只能在 Start 之前设置、Stop 之后清除（运行中改指针是数据竞争）。
 */
typedef void (*zx_source_cb)(const float* interleaved, int frames,
                             int channels, void* user);

ZX_API zx_result zx_source_set_callback(zx_source_t* source,
                                        zx_source_cb cb, void* user);
ZX_API zx_result zx_source_clear_callback(zx_source_t* source);

/* ------------------------------------------------------------------ */
/* 降噪与语音前处理                                                    */
/* ------------------------------------------------------------------ */

/* 对一个采集源挂上降噪（就地处理拉出来的数据，不额外拷贝）。
 * level == ZX_NS_OFF 时等于关闭。
 * backend 不可用时**明确返回 ZX_ERR_UNSUPPORTED**，不再静默降级 ——
 * 本仓库没有 RNNoise 实现（ZX_HAVE_RNNOISE=0），所以请求 ZX_NSB_RNNOISE
 * 会拿到这个错误；可用性用 zx_ns_get_config 的 rnnoiseAvailable /
 * builtinAvailable 查。（2026-10-06 之前是"接受请求→悄悄跑 builtin"，
 * 审计 §四 记的"恒降级"假开关。）
 */
ZX_API zx_result zx_ns_set(zx_source_t* source, zx_ns_level level,
                           zx_ns_backend backend);

/* 查实际生效的配置（后端可用性如实报出），并给出降噪前后的电平平移：
 *   { "level":2, "backend":"builtin", "requestedBackend":"builtin",
 *     "degraded":false, "rnnoiseAvailable":false, "builtinAvailable":true,
 *     "bypass":false, "noiseFloorDbfs":-62.4,
 *     "suppressionDb":11.8, "speechPreservation":0.97, "voiced":false,
 *     "framesProcessed":12000 }
 * suppressionDb 是「噪声段被压掉了多少 dB」，用来客观证明降噪有效。
 */
ZX_API zx_result zx_ns_get_config(zx_source_t* source, char* out_json, int cap);

/* 旁路对比：跳过降噪处理（数据直通），但**仍然统计噪声底与压制量**。
 * 配合 zx_recorder_open 的双路录音，可以录出「有降噪 / 无降噪」两份 WAV 做 A/B。 */
ZX_API zx_result zx_ns_set_bypass(zx_source_t* source, int bypass_on);

/* 逐个模块的开关。AEC 与 AGC 属于「语音通话」阶段（PL1）的能力，
 * 当前版本返回 ZX_ERR_UNSUPPORTED —— 明确报错好过静默忽略。
 * enable 取 -1 表示不改动该项。
 */
ZX_API zx_result zx_ns_set_modules(zx_source_t* source, int aec, int agc, int vad);

/* ------------------------------------------------------------------ */
/* 录音落盘（PL0 的验收工具：出降噪前/降噪后两个 WAV 做对比）           */
/* ------------------------------------------------------------------ */

/* 同时开两路输出：raw 写降噪前的原始样本，processed 写降噪后的样本。
 * 两个路径都可以传 NULL 表示不录那一路。文件为 16-bit PCM WAV，格式由源决定。
 * 传 NULL 整体关闭录音。
 */
ZX_API zx_result zx_recorder_open(zx_source_t* source,
                                  const char* raw_wav_path_utf8,
                                  const char* processed_wav_path_utf8);
ZX_API zx_result zx_recorder_close(zx_source_t* source);
ZX_API zx_result zx_recorder_stats(zx_source_t* source, char* out_json, int cap);

/* ------------------------------------------------------------------ */
/* 会话（PL1 起才真正有内容，先把接口钉住）                             */
/* ------------------------------------------------------------------ */

/* 建会话。json：
 *   { "sessionName":"和朋友的语音", "displayName":"我这边的名字",
 *     "maxPeers":3, "features":["chat","voice","screen"] } */
ZX_API zx_result zx_session_create(zx_engine_t* engine, const char* json_request,
                                   zx_session_t** out_session);
ZX_API void      zx_session_destroy(zx_session_t* session);   /* 幂等；NULL 安全 */

/* 拉取一个待处理事件（JSON，一行一条）。没有事件时返回 ZX_ERR_NO_DATA。
 * 事件形如：{ "type":"peer_joined", "peerId":"...", "displayName":"..." }
 * 调用者应该在一个循环里拉，直到 ZX_ERR_NO_DATA。 */
ZX_API zx_result zx_session_poll_event(zx_session_t* session, char* out_json, int cap);

/* 会话状态快照。形如：
 *   { "state":"idle|connecting|connected|reconnecting|closed",
 *     "peers":[{"peerId":"...","displayName":"...","rttMs":42,"lossPct":0.3}],
 *     "audio":{"txKbps":31.2,"rxKbps":28.7,"jitterMs":18,"codec":"opus"} } */
ZX_API zx_result zx_session_get_state(zx_session_t* session, char* out_json, int cap);

/* 发送一条控制/文字消息（PL2 用）。msg_json 需带 "type" 字段。 */
ZX_API zx_result zx_session_send_message(zx_session_t* session, const char* msg_json);

/* ------------------------------------------------------------------ */
/* 内存释放                                                            */
/* ------------------------------------------------------------------ */

ZX_API void zx_free_string(char* s);

#ifdef __cplusplus
}  /* extern "C" */
#endif

#endif /* ZONGXIAN_MEDIA_H */
