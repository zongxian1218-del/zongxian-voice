# 棕仙语音 · Windows 版实施设计

> ⚠️ **路线已变（2026-10-05）：本文档的"纯 HTML in Edge `--app`"路线已被放弃。**
> 当前唯一权威路线见 [`route-decision-2026-10-05.md`](route-decision-2026-10-05.md)：
> 宿主是 **C# WinUI 3**，媒体是 **C++ 助手进程**（自研管线），视频上屏用**无边框顶层覆盖窗口**。
> 本文档仅作历史参考；其中「**Android 已砍掉**」的结论仍然有效。

日期：2026-10-04
范围：**只做 Windows 桌面版**（Android 已确认砍掉）
状态：选型已定，等待 §6 的自检结果后开工
配套阅读：`voice-collab-design-2026-10-04.md`（总体方案与平台能力边界）、
`../src/media/STATUS.md`（自研原型的实测数据与踩坑结论）

---

## 1. 选型结论（一句话）

**用 Python 做后端与信令，用 Edge(Chromium) 应用模式承载全部实时音视频，
前端是纯 HTML/CSS/JS。**

这样做的直接结果：**原计划那 739 MB 的 libwebrtc 预编译包完全不需要**，
因为 Chromium 内核本身就带着完整的 WebRTC 实现，且它已经在系统里了。

---

## 2. 为什么是这个组合

### 2.1 环境事实（已实测）

| 事实 | 命令/来源 | 影响 |
|---|---|---|
| WebView2 Runtime **154.0.4258.53** 已安装 | 注册表 `EdgeUpdate\Clients\{F3017...}` | Chromium WebRTC 可用 |
| Edge 同版本 **154.0.4258.53** | `msedge.exe` 版本信息 | `--app` 模式可用 |
| 内置 Python **3.12.14**，带 pip / tkinter / ctypes | DSH 运行时 | 不需要装 Python |
| Node v24.21.0 | DSH 运行时 | 备选（本方案不用） |

### 2.2 为什么不用其他方案

| 方案 | 体积 | 需装的东西 | 结论 |
|---|---|---|---|
| **Edge `--app`** | **0** | **无** | ✅ **采用** |
| pywebview | +1 pip 包 | pip | 只是 Edge 的一层薄封装，多一层依赖却不多一个能力 |
| Electron | +250 MB | npm + 打包链 | 系统已有 Chromium，重复携带 |
| Tauri | +10 MB | Rust 工具链 | 要装 Rust；WebRTC 仍走 WebView2，白加复杂度 |
| 原生 libwebrtc | +739 MB | CMake + 链接配置 | 唯一好处是能拿到 `AudioProcessing` 的细粒度参数，代价过大 |
| WebView2 SDK (C++) | +小 | 要写 C++ 宿主 | 语言混用；`--app` 模式已够用 |

**决定性理由**：Chromium 已经把这套东西做好了——AEC3 回声消除、
NS 降噪、Opus 编解码、NetEQ 抖动缓冲、`getDisplayMedia` 屏幕采集、
`RTCDataChannel`。自己再引一份 libwebrtc 或用 C++ 包一层，都是把已经有的东西再搬一遍。

### 2.3 用浏览器能力换来的具体收益

| 功能 | 由谁实现 | 自研代码量 |
|---|---|---|
| 语音降噪（NS） | Chromium `getUserMedia` 约束 | **0** |
| 回声消除（AEC3） | Chromium 内建 | **0** |
| 自动增益（AGC） | Chromium 内建 | **0** |
| 语音编码 / 抖动缓冲 / 丢包隐藏 | Opus + NetEQ | **0** |
| 屏幕共享（窗口/全屏） | `getDisplayMedia` | **0** |
| 系统音频共享 | `getDisplayMedia({audio:true})` | **0**（待 §6 验证） |
| 文字 / 控制指令传输 | `RTCDataChannel` | 少量 |
| NAT 穿透 / ICE | Chromium 内建 | 信令部分 |
| **共享单个应用音频** | ❌ Chromium 不暴露 | **唯一需要自研的部分** |

---

## 3. 架构

```
┌─ Edge (Chromium) --app 窗口 ────────────────────────────────────┐
│  前端：纯 HTML / CSS / JS，无构建步骤、无 npm                    │
│                                                                 │
│   getUserMedia ──────► 麦克风 + 降噪/回声消除/AGC               │
│   getDisplayMedia ───► 屏幕画面 + 系统音频（§6 待验证）          │
│   RTCPeerConnection ─► Opus / NetEQ / AEC3 / ICE                │
│   RTCDataChannel ────► 文字消息 / 远端控制输入                   │
│                        · 移动：无序部分可靠（可丢帧）            │
│                        · 点击按键：可靠有序                      │
│   Web Audio API ─────► 每人对等音量、说话电平、共享音频独立音量   │
└───────────────────────────┬─────────────────────────────────────┘
                            │ 本地 HTTP（页面） + WebSocket（控制/信令）
┌─ Python 后端 ─────────────▼─────────────────────────────────────┐
│  复用现有：webhost.py（页面服务 + /signal 信令）                 │
│            transfer.py（文件传输，一行不改）                     │
│            netgroup.py（异地组网地址识别）                       │
│            discovery.py（局域网设备发现）                        │
│  新增：rtc/session.py    会话状态机、房间成员                    │
│        rtc/chat.py       文字消息 + 待发队列                     │
│        rtc/control.py    远端控制授权状态机 + 审计日志            │
│        rtc/store.py      SQLite 落地                             │
│        rtc/launcher.py   起 Edge --app、选端口、生命周期          │
└─────────────────────────────────────────────────────────────────┘
                            │
┌─ 原生助手（仅一个用途）───▼─────────────────────────────────────┐
│  zxaudio_helper.exe：WASAPI 进程回环（抓单个应用音频）           │
│  · 由 Python 以子进程方式拉起，通过管道回传 PCM                  │
│  · **必须子进程隔离**：该 API 会让进程堆损坏，SEH 拦不住          │
│    （已实测，见 src/media/STATUS.md §4.3）                       │
│  · 复用 src/media/src/audio/wasapi_backend.cpp 的现成实现        │
└─────────────────────────────────────────────────────────────────┘
```

### 3.1 为什么后端还要留 Python

因为**它已经写好了**：文件传输（多流 TCP + 4MB 分片 SHA-256 续传）、
组网网卡识别、UDP 发现、`/signal` 信令中继。这些在 `src/swiftdrop/` 里
经过实测（150+ MB/s 局域网、断点续传、400 小文件 3 秒）。

用 Node 或纯 JS 重写一遍是纯粹的浪费。前端只负责"浏览器擅长的部分"，
后端只负责"已经能用的部分"。

### 3.2 前端为什么不做构建步骤

不引 npm、不打包、不用框架。理由：

- 这个界面不复杂（会话列表 + 视频区 + 音量条 + 授权横幅），
  原生 DOM 足够；
- 免构建 = 改一行刷新即生效，调试往返最短；
- 与仓库现有的零依赖网页版（`src/web/`）风格一致；
- 少一层工具链就少一类"构建失败"问题。

---

## 4. 目录规划

```
src/rtc/                           ★新：实时通信（前端 + Python 侧）
  selfcheck.html                   能力自检页（已写，验证环境）
  app/
    index.html                     主界面
    app.js                         会话编排
    rtc.js                         PeerConnection 封装
    audio.js                       设备、降噪档位、音量、电平
    screen.js                      共享源选择与共享轨管理
    control.js                     ★远端控制：输入捕获 + 采样合并
    chat.js                        文字界面
    style.css
  python/
    session.py                     会话状态机、成员、房间
    chat.py                        文字消息 + 待发队列
    control.py                     控制授权状态机 + 审计
    store.py                       SQLite
    launcher.py                    起 Edge --app / 端口 / 生命周期
    server.py                      HTTP + WebSocket（扩展 webhost.py）

src/media/                         C++ 助手（只保留进程回环这一件事）
  tools/
    zxaudio_helper.cpp             ★新：CLI 助手，抓单个应用音频 → PCM 到管道
    zxprobe.cpp                    保留：设备/进程枚举、量化验收
  src/audio/
    wasapi_backend.cpp             ← 复用（设备枚举 + loopback + 进程回环）
    ring_buffer.cpp / fft.cpp / noise_suppress.cpp / audio_sink.cpp
                                   ← 保留为备用/参考，不再演进
  build-media.cmd                  ← 复用（构建脚本思路已跑通）

src/swiftdrop/                     现有后端，继续用
  transfer.py                      文件传输——不改
  netgroup.py discovery.py         组网与发现——不改
  webhost.py                       扩展承载 WebSocket 与信令
  protocol.py                      扩展帧类型
```

---

## 5. 实施路线（Windows-only，重排）

> 原则不变：每阶段都有能跑、能给人用的东西。

| 阶段 | 内容 | 预估 | 交付物 |
|---|---|---|---|
| **W0** | 选型验证（本文件 §6 的自检）+ 起 Edge `--app` + Python 服务骨架 | 2~3 天 | 能打开一个自绘窗口、跑到自检页 |
| **W1** | 语音通话：信令 + PeerConnection + 降噪档位 + 音量/电平 | 1.5~2 周 | 两台电脑能通话，降噪可调 |
| **W2** | 文字 + 文件：复用 `transfer.py`，前端接上 | 1~1.5 周 | 通话中发文件、发消息 |
| **W3** | 屏幕共享：`getDisplayMedia` + 源选择 + 预览 | 1 周 | 能共享窗口/全屏 |
| **W4** | 共享音频：系统音频（浏览器）+ 单应用音频（原生助手） | 1~1.5 周 | 两条独立音轨，可分别调音量 |
| **W5** | 远端控制：输入捕获 + 注入 + **过期点击拦截** + 授权 UI | 2~3 周 | 可控可撤，五条安全测试通过 |

**合计约 7~9 周**（比原计划 2.5~3 个月显著缩短，因为绝大多数媒体能力由 Chromium 提供）。

---

## 6. 开工前必须验证的一件事实（自检页第 3 节）

**`getDisplayMedia({audio:true})` 在 Edge/WebView2 上能否真的拿到系统音频。**

| 结果 | 后果 |
|---|---|
| ✅ 有音频轨**且有真实信号** | "共享全部应用音频"零成本实现，W4 只剩"单应用音频"要写原生助手 |
| ⚠️ 有音频轨但**永远静音** | 视为不支持，W4 走原生 WASAPI loopback（整机混音） |
| ❌ 完全没有音频轨 | 同上，且要在 UI 上说明需较新的 Chromium |

自检页 `src/rtc/selfcheck.html` 会同时验证：
- WebRTC 相关 API 是否齐全
- `getSupportedConstraints()` 里 `noiseSuppression` / `echoCancellation` /
  `suppressLocalAudioPlayback` 等**精细参数是否支持**
- 麦克风请求的约束与**实际生效值**是否一致（浏览器不一定会照做）
- 屏幕音频轨的 2 秒电平采样（区分"给了轨道"与"真的有声音"）

> 之所以要专门验证：WebRTC 的能力在不同 Chromium 版本/平台上差异很大，
> 而且失败方式是**静默的**（给一条静音轨道），不像报错那样容易发现。

---

## 7. 与总体方案文档的关系

本文件在**平台范围**与**技术实现**上取代
`voice-collab-design-2026-10-04.md` 的以下部分：

| 原文档章节 | 状态 |
|---|---|
| §0.5 路线变更记录 | 仍有效（WebRTC 决策的历史） |
| §0 / §1 平台范围 | **本文件取代**：Android 全部砍掉 |
| §2 平台能力边界 | §2.1/§2.2（Windows）仍有效；§2.3（Android）作废 |
| §5 音频管线 | **由 Chromium 承担**；原自研管线的实测数据留作参考 |
| §6 屏幕共享 | **由 `getDisplayMedia` 承担** |
| §7 单应用音频 | 仍有效，**仍需自研**（含崩溃隔离） |
| §8 Android | 作废 |
| **§9.5 远端控制** | **完整有效，是本文件 W5 的设计依据** |
| §11~§12 模块与路线 | **本文件取代** |
| §13 风险清单 | R-01/R-04/R-09/R-11 大幅降级；**R-02/R-12~R-16 全部仍有效** |
| §14 验收标准 | T1~T3/T5~T13 按新架构调整；**T14~T18（控制安全）不变** |
