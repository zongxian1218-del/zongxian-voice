# 当前技术路线（唯一权威 · 2026-10-05）

> **这份文档解决的是"仓库里有 5 条互相取代的路线、没有任何一份说明现在用哪条"的问题。**
> 引用任何旧文档之前，先来这里确认它属于哪条路线。

---

## 一、当前采用（唯一路线）

| 层 | 采用 | 位置 |
|---|---|---|
| 产品形态 | **Windows 桌面**（Android 已放弃） | — |
| 应用宿主 | **C# WinUI 3**（Fluent，参考 Oopz/Discord 排版，配色用 WinUI 深浅主题） | `src/winui-cs/` |
| 媒体引擎 | **独立 C++ 助手进程**（进程隔离是硬需求：WASAPI 进程回环会硬崩溃 `0xC0000374`） | `src/media/`、`src/media/probe/` |
| 媒体管线 | DXGI 桌面采集 → GPU 转 NV12/缩放 → 硬件 H.264（MF 的 NVIDIA MFT）→ 自建 UDP（32B 包头 + 分片 + XOR FEC）→ libavcodec 低延迟解码 → D3D11 上屏 | `src/media/probe/sender-probe.cpp`、`receiver-probe.cpp` |
| WinUI 内显示 | **无边框顶层覆盖窗口**（`--frameless --topmost --pos`）+ 应用侧 letterbox + DXGI 拉伸（swapchain 保持视频尺寸） | `src/winui-videoprobe/`、`src/media/probe/receiver-probe.cpp` |
| 应用↔助手协议 | stdin 文本命令 `rect x y [w h]`、`quit`；stdout **ASCII 事件** `event view-ready/view stream-size/stats` | 同上 |
| 语音/聊天/文件 | 仍在 **WebView2** 里（`src/winui-cs/media/call.html`，DataChannel）；视频主路径不再走它 | `src/winui-cs/` |
| 信令 | `/signal` WebSocket 中继（复用网页版那套）；网页版另有 MQTT-over-WS | `src/swiftdrop/webhost.py`、`src/web/signaling.js` |
| 打包 | `build\build-winui-cs.cmd` → `dist/winui` → 压 zip；规则见 `dist-and-packaging-2026-10-05.md` | `build/` |

**已确认不做**：WebRTC/getDisplayMedia 作为视频主路径、libwebrtc 整包、Edge `--app`、Android、
5 人以上 SFU、录制、密钥托管、macOS/Linux 适配、剪贴板同步、无人值守、
**往 WinUI 里塞原生子窗口**（z 序抢不过 `DesktopChildSiteBridge`，实测证伪）。

---

## 二、已被取代的路线（**不要照着做**）

| 路线 | 出现在 | 现状 |
|---|---|---|
| 早期自研 C++ 音频引擎（PL0/PL1） | `voice-collab-design` §15 曾决定"停更自研 `src/media`" | **该决定后来被推翻**：10-05 又以 `src/media` 为核心。此条已作废 |
| libwebrtc（739 MB） | `voice-collab-design` §15 决定拉包 | **放弃** |
| Edge `--app` 跑纯 HTML | `rtc-windows-implementation-2026-10-04.md` | **放弃** |
| WinUI + WebView2 + `getDisplayMedia` 显示视频 | `media-engine-architecture-2026-10-04.md`、`src/winui-cs` 现状 | **仅聊天/语音/文件仍在用**；视频显示已改为 C++ 助手 + 覆盖窗口 |
| 往 WinUI 里塞 `WS_CHILD` 原生子窗口 | `winui-integration-status-2026-10-05.md` §1 | **明确排除**（花了实测时间验证：助手解码 300 帧、界面全黑） |
| `src/rtc/` | `rtc-windows-implementation` 记为"已写" | 实际只有一个浏览器自检页 `selfcheck.html`，**不是 RTC 实现** |

---

## 三、各文档的权威性

| 文档 | 怎么用 |
|---|---|
| `handoff-next-session.md` | **接手第一份**，含工作方式与踩坑表 |
| `winui-overlay-verified-2026-10-05.md` | WinUI 内显示：已打通（第二轮） |
| `video-area-arbitrary-size-2026-10-05.md` | 任意尺寸 + letterbox（第三轮） |
| `helper-protocol-and-feedback-2026-10-05.md` | 应用↔助手协议、状态通道（第三轮） |
| `dist-and-packaging-2026-10-05.md` | 交付物与打包规则 |
| `transport-verified` / `sender-pipeline-verified` / `decoder-switch-ffmpeg` / `render-path-verified` / `end-to-end-screenshare`（均 10-05） | 媒体链路各环节实测数据，**仍然有效** |
| `voice-collab-design-2026-10-04.md` | 只在**音频设计、远端控制、信令、UI 约束**这些没变的部分有效；§15 的路线结论已作废 |
| `ui-design-guideline-2026-10-04.md` | UI 硬约束仍有效（栏宽、主区三形态、语义画刷、被控红条等）；进度停留在 W0，忽略 |
| `media-engine-architecture-2026-10-04.md` | 架构描述已被取代，仅历史参考 |
| `rtc-windows-implementation-2026-10-04.md` | 仅历史参考（Android 砍掉的结论仍有效） |
| `release-notes-v1.0.md` / `announce-zh.md` | 只描述**已发布的文件传输 v1.0**，不代表语音功能已发布 |

---

## 四、这条路线是怎么定下来的（免得又被当成"没记录"）

不是开会定的，是**一路实测淘汰**出来的：每换一条路都留下了一份验证文档（见第三节），
最后活下来的是"自研 C++ 媒体管线 + C# WinUI 宿主 + 覆盖窗口上屏"。
判断依据始终是**能不能测**：编码帧率、端到端帧数、解码延迟、窗口可见性、抓图非黑率。
