# 共享电脑声音 —— 实现状态（2026-10-06）

用户定的方针：**音频共享是一个独立功能，不是屏幕共享的附属**，而且要的是**指定某个应用**的声音。
本文只记"做到哪一步 + 凭什么这么说"，不写计划。

## 一、结论：端到端打通（2026-10-06 04:39，实测）

判据用的是**字节数**，不是"看到一条音轨" —— 静音轨也会"看到"音轨。

| 环节 | 证据 | 数值 |
|---|---|---|
| 采集（指定应用 / 整机） | `build/analyze-voice.py` 对比源音频 | 峰值 **-4.4 dBFS**，与源一致 |
| 引擎流式输出 | `zxprobe stream` 写文件后逐字节校验 | 2 秒 = **768,000 字节**（正好 2×48000×2ch×4B） |
| 应用侧推给页面 | `share-audio-debug.pushed` | 318 → **478** 块 |
| 采集泵搬运量 | 泵状态日志 | 块=478，**9,177,600 字节**，错误=无 |
| 页面独立音轨 | `trackState` | **live**（kind=audio） |
| 链路发送 | `audioOutBytes` | 11,410 → **59,810**（增长） |
| **对端接收** | `audioInBytes` | 91,996 → **140,396**（增长） |

## 二、这条链路的形状（前后端分离）

```
引擎（C++，zxprobe stream）      采集：WASAPI 回环 / 进程回环 -> float32 48k 立体声裸流
        |  stdout（二进制，_O_BINARY）
应用（C#，ShareAudioCapture）    读二进制 -> 每 50ms 一块 -> base64 -> 编组到 UI 线程
        |  WebView2 ExecuteScriptAsync(zxEngine.pushSharedAudio)
页面（call.html + worklet）       AudioWorklet 缓冲 200ms -> MediaStreamDestination -> 独立音轨
        |  WebRTC addTrack（链路建立时就挂上，避免重新协商）
对端                             普通远端音轨，直接播放
```

**为什么采集必须在应用进程**：Chromium 的 `getDisplayMedia` 只能共享"整个屏幕/标签页"的系统声音，
**做不到"指定某个应用"**。按应用采集只能走 Windows 进程回环（WASAPI），那只有引擎能做。

**为什么音轨一开始就挂上**：事后 `addTrack` 需要重新协商，而重新协商在这套 mesh 里踩了三次坑
（answer 不能新增 m-line、发起方不走 onOffer、negotiationneeded/connectionstatechange 不触发）。
预挂一条静音轨之后，开始/停止共享只是灌/停数据，**根本不需要重新协商**。

## 三、沿途修掉的真问题（10 个，按发现顺序）

| # | 问题 | 症状 |
|---|---|---|
| 1 | 激活后对指向**栈变量**的 `VT_BLOB` 调 `PropVariantClear` | 堆损坏 0xC0000374（曾被文档误判为"系统 API 的锅"） |
| 2 | 进程回环**漏了 `Initialize`（带 EVENTCALLBACK）** | `0x88890011`，采集源打不开 |
| 3 | 降噪器按**后端声道数**建立，而数据已下混成单声道 | 越界写 0xC0000005（连带压垮"共享全部应用的声音"） |
| 4 | 环形缓冲**帧/样本单位混淆** | 写入只进一半、立体声错位；读取单位混算 |
| 5 | stdout **文本模式** | PCM 的 `0x0A` 被翻成 `0x0D 0x0A`，字节错位（"垃圾浮点"的真凶） |
| 6 | **WebView2 调用必须在 UI 线程** | 后台线程调用抛 RPC_E_WRONG_THREAD 且被 catch 吞掉 → 音轨**静音** |
| 7 | 应用目录里的 `zxprobe.exe` 是**旧副本** | 旧探针不认识 `stream` → 启动即退码 1 → 泵零数据 |
| 8 | cmd 脚本里**多行 `if (...)` 块** | 括号配对错乱 → 构建卡死十几分钟 |
| 9 | `powershell` 不在 PATH / `%DIST%` 取空 | `resources.pri` 不生成 → 应用**启动即崩** |
| 10 | dist 里的页面是旧版 | 打包出去等于**没有这个功能** |

> **【2026-10-06 更正 · 上面第 4 行「帧/样本混淆已修」当时只是症状消失】**（本块本身同日稍晚再次更正）
> 6:12 时的状态是：内部按**样本(float)**计数，但 API/注释仍写**帧** —— 谁照 API 名字去「修正」
> 调用点（把样本改回帧）会**二次引爆**。
> **同日已按 S5 §6 第一条统一**：`src\media\src\audio\ring_buffer.h:8-20` 写死
> 「本类一切计数 = **样本(float)**」，API 改名为 `capacity_samples` / `Write(..., int samples)` /
> `Read(..., int cap_samples)` / `CapacitySamples|AvailableSamples|FreeSamples`（`:40,44,48,53-55`）；
> 调用点保持一致：写侧 `wasapi_backend.cpp:758-760` 传 `frames * format_.channels`、
> 读侧 `:375-387` 用 `cap_frames * ch` 并折回帧数；统计键名改为 `bufferedSamples`
> （`media_api.cpp`）。**可复核证据**：`dist\zxprobe.exe ring-check` → 写 480 帧×2 声道 = 960
> 样本，`Write` 返回 960、`AvailableSamples` 960、读回逐点不符 0。
>
> **两层别混**：C ABI（`src\media\include\zongxian_media.h` §8/§9）仍以**帧**为单位
> （`zx_source_read` 的 `cap_frames/out_frames`），引擎内部 `RingBuffer` 一律按**样本**。

**7 与 10 是同一类：版本不一致导致的静默失效** → 已按规矩三变成 `build/run-checks.py` 的
硬闸门 `check_dist_media_and_probe()`（dist 媒体资源必须与源码逐字节相同；两处探针必须同版本）。

## 四、还差什么

1. 界面：**独立的「共享电脑声音」开关 + 应用列表**（列表来自 Windows 音频会话枚举，现成 API）
2. 屏幕共享改用 WebView2 官方 `ScreenCaptureStarting`（免弹窗；SDK 1.0.3351.48 已具备）
3. P3（共享时好时坏）定位：失败原因已可读（"选择窗口未响应（20 秒内没有选择）"）
4. 重新打包（会把 dist 里 28 处陈旧全部刷新，并解决"dist 卫生"的 FAIL）
