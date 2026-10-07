# 棕仙项目全貌 · 接手报告（2026-10-05）

> 本报告为**只读盘点**：读完 16 份 `docs/` 文档 + 全部源码树后写成，**未运行任何构建或测试**，未改动任何源代码。
> 所有"实测"数字均来自仓库内文档/日志的自述，不是本次复跑的结论；可疑处已标注。
> 唯一新建文件就是本报告。

---

## 0. 一句话结论

项目有**两条线**：文件传输线（已发布 v1.0，可交付）和语音线（WinUI 3 语音房，进行中）。
语音线的**媒体链路已经打通到"独立进程 + 独立窗口"为止**（采集/编码/传输/解码/上屏全部有真机实测数据），
**唯一阻塞点是"原生视频窗口如何在 WinUI 3 界面里显示"**，且这条正在被本地事实推翻一次（详见 §5）。
比技术上更危险的是：**没有版本控制（仓库里没有 `.git`）、技术路线已经漂移过 5 次、文档之间互相矛盾**——这是接手的最大风险，不是代码。

---

## 1. 两条产品线

| 线 | 是什么 | 状态 | 最后动作 |
|---|---|---|---|
| **A. 文件传输**（棕仙的传输软件 / swiftdrop） | 不装服务器、不要公网 IP 的 P2P 大文件传输；网页版单文件 + Windows 桌面版 + 手机浏览器 | **已发布 v1.0** | 打包于 2026-10-04 14:18 |
| **B. 棕仙语音**（ZongxianVoice，`src/winui-cs/`） | 2~4 人 P2P 语音房：语音降噪 + 文字 + 文件 + 屏幕共享 + 共享应用音频 + 远端控制，Windows 优先 | **原型期，主线卡在 WinUI 内显示** | 主应用最后一次成功构建 2026-10-05 03:50 |

目标形态见 [voice-collab-design-2026-10-04.md](voice-collab-design-2026-10-04.md)（§1、§3-4）；A 线现状见 [README.md](../README.md)。

---

## 2. 时间线（据文件时间戳 + 文档日期）

| 时间 | 事件 |
|---|---|
| 10-03 | 早期 GUI 自动化试验（根目录 `gui_demo*.py`、`gui_proof*.png`、`_mc_opt/`） |
| 10-04 上午 | 文件传输线收尾：二维码比对、网页版截图、装机包（NSIS/MSI） |
| 10-04 14:18 | **A 线 v1.0 打包完成**（`dist/`：portable zip / 自免压 exe / 安装包） |
| 10-04 15:00-17:00 | 环境搭建（VS BuildTools、CMake、.NET 8）+ 语音线 W0/W1：自研 C++ 音频引擎（WASAPI/降噪/FFT）、`zongxian_media.dll` |
| 10-04 16:00-17:00 | WinUI C++ 应用骨架（`src/winui/`）→ **从未编译成功**，被 C# 版取代；声音质量调查 |
| 10-04 18:00-23:00 | W2：聊天/文件/屏幕共享走 WebView2 + WebRTC（`src/winui-cs/media/call.html`） |
| 10-05 00:00-06:00 | 语音线打包 v13/v14/v15/v17；同时**自研原生媒体链路**逐个探针打通（capture→encode→transport→decode→display→sender/receiver） |
| 10-05 06:00-09:38 | 端到端屏幕共享回环通过；解码器换成 libavcodec；上屏用 D3D11 独立窗口验证通过；随后尝试嵌入 WinUI → 失败 → 改用无边框顶层覆盖窗口 |
| **10-05 09:37-09:38（上一轮被中断处）** | `winui-videoprobe` 最后一次构建；`receiver-probe`/`display-probe` 日志刷新；`tmp/winui-embed.png` 截图；**集成验证未通过** |

---

## 3. B 线媒体链路能力矩阵（据 10-05 系列文档自述）

| 阶段 | 状态 | 实测数据（出处） |
|---|---|---|
| DXGI 桌面采集 | ✅ | 349/350 帧有真实更新；180/180 收发（sender-pipeline、end-to-end-screenshare） |
| GPU 色彩转换 + 缩放（BGRA→NV12） | ✅ | D3D11 VideoProcessor，三档缩放达标（sender-pipeline） |
| H.264 硬件编码 | ✅ | 走 Media Foundation 的 NVIDIA MFT（**不是 NVENC SDK**）：1080p30 30.0fps/5.29Mbps；1080p60 60.0fps/6.45Mbps（余量 13.71ms）；720p30 30.0fps/2.32Mbps；逐帧解码核对 150/150、300/300 |
| 自建 UDP 传输 | ✅ | 5 阶段 60/60；32B 包头 + 1200B 载荷；IDR ≈130KB=109 片；端到端发 3443 收 3442，丢 1 片被 XOR FEC 救回、0 帧丢弃；延迟 0.94~1.15ms |
| libavcodec 低延迟解码 | ✅ | 平均 2.5ms、最大 19.5ms、首包→首帧 17.6ms、2.71ms/帧 |
| D3D11 上屏 | 🟡 | 330 帧全部上屏（1280×720），首帧 9.1ms、平均 3.0ms；**仅"独立进程 + 独立 HWND"验证过** |
| **WinUI 3 界面内显示** | ⬜ → ✅ | ~~未通过（本报告 §5）~~ **2026-10-05 晚已打通**：覆盖窗口贴住 XAML 视频区，截图 nonblack 78.5%（见 §10.1 P0-1） |
| 音频：采集/播放/自写降噪 | 🟡 → 作废 | 48kHz 录音跑通；立体声 + 降噪会崩（STATUS.md 第 5 号缺陷）—— **2026-10-05 核实：该模块属已放弃路线**（`src/media/STATUS.md` §6 明确不再修），当前语音走 WebView2，其 AEC/NS/AGC 由 Chromium 提供 |
| 音频：通话质量 | ⬜ → 🟡 | ~~码率曾只有 15kbps（已改 40kbps 待复测）~~ **2026-10-05：现为 48 kbps 目标**（`call.html` 的 `OPUS_TARGET_BITRATE`）+ SDP `maxaveragebitrate=48000`，另有 strong 档软件降噪链；**只差"真人语音判定音质"**（工具已就绪） |

**结论：能上台面的"实测通过"只到「独立进程 + 独立 HWND 窗口」为止。** WinUI 集成、真实屏幕采集（需人工选窗）、单应用音频、远端控制均未做。

> **2026-10-05 晚更正**：这份"结论"已经过期 —— WinUI 集成**已完成并截图验收**；
> 真实屏幕采集走 WebView2 `getDisplayMedia`（已能用，需人工选窗）；远端控制 + 授权 UI 更早已完成；
> 单应用音频：**子进程隔离在应用里生效**（探测 + 如实降级），但 PCM 通路未做。
> 当前状态以 **§10.1** 为准。

---

## 4. B 线应用层功能矩阵（`src/winui-cs/`，抽样 grep + 关键段精读）

| 功能 | 状态 | 说明 / 位置 |
|---|---|---|
| 语音通话、静音、挂断 | ✅ | `MainWindow.xaml.cs:703,712,720` |
| 降噪三档 | ✅ | `:1706`（实际算法在 `media/call.html:792`） |
| 文字聊天 | ✅ | DataChannel `:838` |
| 文件传输 | 🟡 | base64 走 DataChannel，**>64MB 无方案** `:1197` |
| 屏幕共享（发送/观看） | ✅ | `:1015` / `:1887-1905`，走 WebView2 `getDisplayMedia` |
| 成员列表 | 🟡 | 人数真实（`:2116`），**名字硬编码"朋友 A/B"** `MainWindow.xaml:490-544` |
| 设置项 | 🟡 | 只有降噪下拉；麦克风/耳机/设置/共享音频 4 个按钮**无事件** `MainWindow.xaml:145-159,209-218` |
| 统计面板（RTT/丢包/抖动/码率） | ✅ | `:2238` |
| 录音 / 自检 | ✅ | `:734,:744`；自检页 `media/selfcheck.html` |
| 远端控制 | ⬜ | 横幅被强制隐藏 `:160`，按钮无处理 |
| 原生视频区 | ⬜ | 视频区目前是**嵌了 WebView2 的 `MediaHost`**（`MainWindow.xaml:250`），就地渲染 `call.html` 的 `<video>` |

**`MediaEngine.cs` 只有 `InitializeAsync / WaitForEngineAsync / CallAsync / Shutdown` + 两个事件（`:76-88`），没有任何原生视频接口；主应用全项目无 D3D / GraphicsCapture / MF 互操作代码。** 也就是说：自研 C++ 媒体链路目前和主应用**还没有接上**，唯一的对接点是独立验证应用 `winui-videoprobe`。

三个验证应用的关系（易混，务必分清）：

- `src/winui/`（C++/WinRT）：UI 骨架，**从未编译成功**（缺 XamlCompiler.exe，见其 BUILD-NOTES），已废弃，仅注释有参考价值。
- `src/winui-probe/`（C#）：预研 WGC 采集与硬编枚举，可用，与主应用无依赖。
- `src/winui-videoprobe/`（C#，**最新，10-05 09:37 构建**）：拉起 `receiver-probe.exe`，把无边框覆盖窗口按 XAML 视频区坐标定位——**这是主应用未来原生视频区的原型**，也是被中断的那一步。

---

## 5. 当前唯一卡点：WinUI 3 里的原生视频显示

上一轮的完整结论见 [winui-integration-status-2026-10-05.md](winui-integration-status-2026-10-05.md)。要点：

1. **子窗口嵌入这条路已被证伪，不要再试。** 用 `--parent-hwnd` 建 `WS_CHILD`：助手侧日志正常（收包 4243、解码 300 帧），但界面一片黑。根因：WinUI 3 把 XAML 内容渲染在 `DesktopChildSiteBridge` 里，我们的窗口是它的**兄弟窗口**，内容岛持续占据 z 序最前，`SetWindowPos(HWND_TOP)` 抢不过。
2. **无边框顶层覆盖窗口单测通过**：`--frameless --topmost --pos x y` 建 `WS_POPUP + WS_EX_TOOLWINDOW + WS_EX_NOACTIVATE`，在 (200,150) 正确显示出推流画面。
3. **但"WinUI 应用 + 覆盖窗口"的集成未验证通过**：界面视频区仍黑，且 C# 侧位置诊断文件没生成 → 说明 `RootGrid.Loaded` 里的自动启动**根本没执行**。

我在源码里复核到了第 3 点的确切位置：`src/winui-videoprobe/src/MainWindow.xaml.cs:48`

```csharp
RootGrid.Loaded += (_, _) => Start_Click(this, new RoutedEventArgs());
```

这行就是"下一轮第 1 项"。**修复方向明确：改用 `App.OnLaunched` 或按钮触发，不要依赖 `Loaded`。** 另外 `rect` 命令的坐标系不统一（源码注释写父窗口客户区坐标，C# 端 `:73` 发的是屏幕坐标）——只有 frameless 形态是对的，值得顺手统一并写进注释。

**注意**：`docs` 里没有"覆盖窗口在 WinUI 场景下可见"的证据，目前**不确认它可见、也不确认位置正确**。所以这一步的验收标准必须是**一张截图**，不是日志。

---

## 6. 最大风险不是 bug，是路线漂移与文档过期

仓库里能数出**5 条依次替换的技术路线**：自研 C++ 引擎 → libwebrtc（739MB）→ Edge `--app` → WinUI + WebView2 → 自建 C++ 管线。而：

- [voice-collab-design-2026-10-04.md](voice-collab-design-2026-10-04.md) §15 曾决定**停更自研 `src/media`**，10-05 的文档却**以它为核心**——是否推翻该决定，**文档里没有说明**。
- 同文档 §9.2/9.3 仍写"不引 ICE/DTLS/SRTP、自研包格式"，与 §15"全面改用 WebRTC"互斥（旧路线残留）。
- 10-05 各文档**全文未提 WebRTC / WebView2 / Edge**，路线切换没有任何正式记录，也没有一份"当前采用哪条"的决策文档。
- [ui-design-guideline-2026-10-04.md](ui-design-guideline-2026-10-04.md) 说 UI 载体是 WinUI3/C++，[rtc-windows-implementation-2026-10-04.md](rtc-windows-implementation-2026-10-04.md) 说纯 HTML in Edge，实际 `src/winui-cs` 是 C#。
- Android：设计文档有完整一章，实现文档宣告整体砍掉。
- 文件传输复用：一处说"`transfer.py` 一行不改"，另一处说"未复用，改走 DataChannel"。
- UI 指南的进度仍停在 W0，另两篇已称 W1/W2 完成。
- 帧数数字不能混用：W2 的 3777 帧是 WebRTC 合成源链路，10-05 的 330 帧是自研链路。
- [release-notes-v1.0.md](release-notes-v1.0.md) 与 [announce-zh.md](announce-zh.md) 只描述**已发布的文件传输 v1.0**，容易被误读成语音功能已发布。

> **接手人须知**：以日期最新的文档为准；引用任何旧结论前先确认它属于哪条路线。

---

## 7. 交付物与构建产物现状

**A 线（传输）· 2026-10-04 14:18 打包，齐全可用**
`dist/`：`zongxian-portable-win64.zip`(13.4MB)、`棕仙的传输软件-自解压版.exe`(9.76MB)、`棕仙的传输软件-安装包-1.0.exe`(9.56MB)、`swiftdrop.html`(363KB)、`zongxian-transfer-guide.md`(21KB)、图标若干、`棕仙的传输软件/`（exe + `_internal`）。

**B 线（语音）· 产物混乱**
- 包只有两个：`棕仙语音-测试版-v13 - 副本.zip`(34.0MB, 10-05 00:21)、`棕仙语音-测试版-v21.zip`(34.0MB, **10-05 03:39**)。
- **v21 包比它自己的源码旧**：`dist/winui/ZongxianVoice.exe` 03:38 打的包，而 `src/winui-cs` 源码 03:50 还在改 —— 也就是说**最新包不含最后一次改动**。
- 另有 5 个 100MB 级的 WindowsAppSDK 展开目录（`_pkg`、`winui`、`测试/…`、`v13`、`v13 - 副本`、`v15`、`v17`）和 WebView2 用户数据缓存（`dist/winui/.webview2/`），**约 700MB+ 的重复/缓存物混在交付目录里**，无法一眼看出"到底哪个是要发出去的"。
- 噪音文件：`nstest.exe`、`zxprobe.exe`、`zongxian_media.dll`（调试产物误入 `dist/`）。

**A 线测试现状（有反例）**
- `tests/test_lan.py`：最近一次实测日志（`tmp/testlan3.log`, 10-04 13:20）**PASS 5 / FAIL 1**，失败项是速度 32.7 MB/s 低于 50 阈值；而 [README.md](../README.md) 写"实测 150+ MB/s"——**宣传数字与实测冲突**。
- `build/verify-desktop.py`（声称 5/5）、`build/verify-diag.py`、`build/e2e/*.mjs`（18 个脚本）：**仓库里找不到运行日志**，只有文档自述。

---

## 8. 环境与可复现性

| 项 | 现状 |
|---|---|
| .NET SDK | 8.0.425（`C:\Program Files\dotnet`） |
| Windows App SDK 运行时 | 1.7 已安装；项目用 `Microsoft.WindowsAppSDK 1.7.250909003`，非打包（`WindowsPackageType=None`） |
| WebView2 运行时 | 154.0.4258.53 已安装 |
| MSVC | VS2022 Build Tools 14.44，装在 `D:\BuildTools`；CMake 生成器 VS17 |
| FFmpeg | 9.0.2 shared，`D:\BuildTools\ffmpeg\ffmpeg-9.0.2-full_build-shared`（**只有 receiver-probe 需要**） |
| Python / Node | **都不在 PATH**（PATH 里只有 Microsoft Store 的 python 占位符）。必须用 DSH 自带：`~/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/{python,node}` |
| 版本控制 | ~~**没有 `.git`，git 命令也不可用**~~**【2026-10-06 更正：已建库** —— 106 提交 / 369 跟踪文件，HEAD 见 `git rev-parse --short HEAD`；本机 git 全路径 `D:\BuildTools\MinGit\cmd\git.exe`**】**；仍无 `.github/`，无 CI（用 `build\run-checks.py` 当最小可用 CI） |
| 构建方式 | WinUI 用 `build/build-winui-cs.cmd`；媒体库用 `build/build-media.cmd`（CMake）；各 probe 各有手写 `build-*.cmd`（`cl.exe /std:c++20 /O2 /MD` + 手动链接） |
| 硬编码路径风险 | `MainWindow.xaml.cs:18` 把 `D:\文档\ai001\src\media\probe\build\receiver-probe.exe` 写死；`build-*.cmd` 里写死 `D:\BuildTools\...` |
| 构建产物证据 | 无 cl.exe 构建日志（全仓唯一 `build.log` 属 Android APK）；成功与否只能看 exe/obj 时间戳和脚本自报的 `BUILD OK/FAILED` |

---

## 9. 风险清单（按严重度）

| # | 风险 | 影响 | 建议 |
|---|---|---|---|
| R1 | ~~**无版本控制、无 CI**~~**【2026-10-06 已部分解决：git 已建库**（106 提交 / 369 跟踪文件，本机 git 在 `D:\BuildTools\MinGit\cmd\git.exe`），仍无 CI**】**，全部改动只在本机磁盘 | 任何一次误改/误删不可恢复；多人协作无法进行 | ~~**立刻 `git init` + 首次提交**（先核对 `.gitignore`，排除 `dist/`、`build/` 大物）~~ 已完成 |
| R2 | 技术路线漂移 5 次 + 文档互相矛盾、无决策记录 | 接手人极易按废弃路线干活，返工成本极高 | 写一份**当前路线决策文档**，旧文档头部加"已废弃"标注 |
| R3 | WinUI 内显示未通过（主线阻塞） | 语音线无法形成可用产品 | 先修 `Loaded` 自动启动，跑通并用**截图**验收 |
| R4 | 构建可复现性差：硬编码绝对路径、无构建日志、`src/winui` 从未编译成功 | 换机器/换目录即不可构建 | 把路径改为相对/环境变量；补一份 BUILD.md |
| R5 | `dist/` 混乱：5 个 100MB 级展开目录 + 缓存 + 旧 zip，最新包比源码旧 | 交付物不可信，仓库体积失控 | 清理展开目录；发布物只留 zip/exe；覆盖前先备份到 `dist/.backup/` |
| R6 | 已知缺陷未修：降噪立体声崩溃、WGC `0x80070005`、枚举不到硬解（仅软解）、FFmpeg 104MB GPL 随包分发、单应用音频 `0xC0000374` 崩溃、无本机预览 | 语音体验与合规 | 排进 P1/P2，逐条有验收标准 |
| R7 | A 线速度测试 FAIL、README 数字夸大 | 对外承诺与实际不符 | 修性能或改 README（二选一，别两头挂着） |
| R8 | 临时物混入仓库根/`build/`（`gui_demo*.py`、`gui_proof*.png`、`_mc_opt/`、`.wav`、`.png`、工具链缓存 wix/jdk/android-sdk） | 干扰阅读，误判进度 | 归档到 `tmp/` 或直接删 |

---

## 10. 待办清单

**P0（阻塞主线，先做）**
1. 修 `winui-videoprobe` 自动启动（`RootGrid.Loaded` → `App.OnLaunched` 或按钮），跑一次并**截图**验收：覆盖窗口在 WinUI 窗口上层可见 + 与 XAML 视频区坐标对齐（两侧日志对齐矩形）。
2. `git init` + 首次提交；核对 `.gitignore`。
3. 写"当前技术路线"决策文档，标注旧文档废弃状态。

**P1（主线推进）**
4. 覆盖窗口支持任意尺寸：`CopyResource` 要求后台缓冲与视频同尺寸，需改用 VideoProcessor 做 BGRA→BGRA 缩放（现在视频区必须正好 1280×720）。
5. 覆盖窗口跟随主窗口：移动/最小化同步（`AppWindow.Changed` 已挂 rect 同步，最小化未处理）；点击穿透策略（远程控制需要，但要区分本地/远程注入）。
6. 把原型搬进主应用 `src/winui-cs`：给 `MediaEngine` 加原生视频接口，视频区从 WebView2 `MediaHost` 换成原生窗口。
7. 音频：修降噪立体声崩溃 + 补 AEC（PL1）+ 40kbps 改动复测 + 用真人语音样本判定音质。

**P2（收尾与发布）**
8. `dist/` 清理与发布物规范；重建语音包（新版本号），明确"传输 v1.0"与"语音测试版"两条发布线。
9. 真实屏幕采集（人工选窗/权限流程）、单应用音频（WASAPI 进程回环 + 子进程隔离）、远端控制。
   **2026-10-05 更新**：单应用音频的"崩溃"已用**打开真实探测的独立进程**复验 ——
   `0xC0000374`（`ntdll.dll`，崩在激活调用路径内），**父进程毫发无伤**、SEH 拦不住；
   复现脚本 `build\build-media-loopback-probe.cmd`，结论与设计草图见
   `src/media/STATUS.md` §7。要做这个功能，必须做成"**专门的捕获子进程**"（句柄不能跨进程）。
10. A 线：速度测试达标或修正 README；补跑 `verify-desktop.py` / `build/e2e/*.mjs` 并留日志。
   **2026-10-05 已跑**：`verify-desktop.py` 5/5、`tests/test_lan.py` 6/6（200MB/4 流平均 **150.6 MB/s**）、
   `build/e2e/e2e.mjs` 8/8（P2P 直连、32MB 27.7 MB/s、400 文件 3.8s、续传、双向同步、反向）；
   日志在 `tmp/aline-*.log`，README 里 145.5 MB/s 的声明**没夸大**。

### 10.1 状态更新（2026-10-05 晚 · 接管会话结束时）

> **【2026-10-06 更正】** 本节是 **2026-10-05 晚的快照**，不是当期状态。已核实过时的点：
> 语音包版本号（本节写 v24，实际此后到 v36 且 **v36 已作废**、v37 被闸门拦住）、
> `run-checks.py` 项数（本节未写数，但同期文档写 4/5/6 项；2026-10-06 实际为 **25 项**，
> release 模式修复前 9 项，以 `build\run-checks.py` 输出为准）、§8 的"没有 git"。
> **当期状态只看 `docs\handoff-next-session.md`。**

**这一节覆盖上面那份清单的当前状态**（上面原文保留，便于对照"当时以为要做什么"）。
每项的详细证据在对应文档里；一句话结论见 `handoff-next-session.md` §0.1。

| 项 | 原状态 | 现在 | 证据 / 说明 |
|---|---|---|---|
| **P0-1** 原生视频进 WinUI + 截图验收 | ⬜ | ✅ **完成** | 覆盖窗口 + 任意尺寸；截图 `nonblack 78.5%`；窗口矩形与 XAML 视频区**逐位一致**（`winui-overlay-verified-…`、`p1-6-native-video-in-main-app-…`） |
| **P0-2** git + `.gitignore` | ⬜ | ✅ **完成** | 44 个提交、348 个跟踪文件、**0 个跟踪二进制**；构建产物一律不入库 |
| **P0-3** 路线决策文档 | ⬜ | ✅ **完成** | `route-decision-2026-10-05.md`（含"已确认不做"清单） |
| **P1-4** 覆盖窗口任意尺寸 | ⬜ | ✅ **完成** | 协议 `rect x y w h` + DXGI 拉伸 + 应用侧 letterbox，不需要 VideoProcessor |
| **P1-5** 跟随主窗口 / 点击穿透 | 🟡 | 🟡 **部分** | 移动/尺寸/最小化/锚点都已同步（z 序紧邻应用窗口）；**点击穿透策略仍待**"本地操作 vs 远程注入"的区分 |
| **P1-6** 原型搬进主应用 | ⬜ | ✅ **完成（并超出）** | 原生视频接进 `src/winui-cs`、开关界面化、跟随"画面区可见"起停；**另做了 mesh 多人共享：3 人同看一路共享已实测**（`multi-peer-mesh-design-…`） |
| **P1-7** 音频 | 🟡 | 🟡 **大半作废，剩一项** | "降噪立体声崩溃 / 补 AEC"属**已放弃的自研路线**（`src/media/STATUS.md` §6 明确不再修）；码率现为 48 kbps 目标；**只剩"真人语音判定音质"** —— 分析工具已就绪（`build\analyze-voice.py`），需要人对着麦克风说话 |
| **P2-8** `dist/` 规范 | ⬜ | ✅ **完成** | `dist-policy-…` + 守卫 6 条 + `BUILD-INFO` 溯源 + 清单双向核对；~~语音包已到 **v24**~~【2026-10-06 事实：包已到 **v36**，且 v36 **已作废**（陈旧页面 + 测试残留，挪进 `build\dist-quarantine-2026-10-06\`）、v37 被 `build\release.py` 闸门拦住 → **当前没有可用语音包**，见 `docs\audit-2026-10-06.md` §7.4】 |
| **P2-9** 真实采集 / 单应用音频 / 远端控制 | ⬜ | 🟡 **部分** | 真实采集走 WebView2 `getDisplayMedia`（已能用）；远端控制 + 授权 UI 早已完成；单应用音频：**子进程隔离已在应用里生效**（探测 + 降级如实上报），**PCM 通路未做** |
| **P2-10** A 线验收与发布物 | ⬜ | ✅ **完成** | `verify-desktop.py` 5/5、`test_lan.py` 6/6（200MB/4 流 **150.6 MB/s**）、`e2e.mjs` 8/8；便携版由源码重建并**用产物本体收发 64 MB，SHA-256 一致** |

**还开着的（按优先级）**：
1. 真人语音判定音质（工具已就绪，只差人说话）
2. 单应用音频的 PCM 通路（子进程 → 主进程 → 页面）；且降级路径需**真机**复测（本环境音频流起不来）
3. mesh 的 S4–S6（每对端聊天 / 多路画面 / 应用侧共享者集合）
4. 覆盖窗口 `WS_EX_TOPMOST`（间歇性；锚点已满足当前需要）
5. A 线安装包的快捷方式/卸载项需**真机**确认（本环境禁止写工程外，已证明代码执行到）

---

## 11. 我建议的下一步

**（2026-10-05 晚更新：下面这段"建议"已经全部做完了，保留作对照）**

**直接从 P0-1 接着干**（就是上一轮被中断的那一步）：改 `App.OnLaunched` 启动 → 构建 → 运行 → 截图 → 对齐坐标。验收标准是"截图里能看到推流的画面，且位置贴合 XAML 视频区"，不是"日志显示收到数据"。

需要你点头的方向问题只有一个：**P0-2（建 git 仓库并首次提交）要不要现在做**——它对本机文件有写入且会生成 `.git`，但这是目前性价比最高的一步保险。

**现在的下一步**请直接看 `handoff-next-session.md` §0.1（本会话成果与遗留）+ 上面 §10.1 的"还开着的"五项。
最省事的第一步是**真人语音判定**：~~装好 v24 包~~【2026-10-06 更正：v24 早已过时，此后到 v36 且
**v36 已作废**、v37 被闸门拦住 —— 当前没有可用语音包，别照这句话去找 v24】，对着麦克风录一段（应用里有"录音自检"按钮），然后

```powershell
python build\analyze-voice.py --compare <处理前.wav> <处理后.wav>
```

工具会给 RMS / 噪声底 / 语音带占比 / 谱质心的对比数字，并**明确提醒"没有真人说话时这些数字只反映底噪怎么被处理了"**。

---

*报告依据：[winui-integration-status-2026-10-05.md](winui-integration-status-2026-10-05.md)、[render-path-verified-2026-10-05.md](render-path-verified-2026-10-05.md)、[decoder-switch-ffmpeg-2026-10-05.md](decoder-switch-ffmpeg-2026-10-05.md)、[end-to-end-screenshare-2026-10-05.md](end-to-end-screenshare-2026-10-05.md)、[transport-verified-2026-10-05.md](transport-verified-2026-10-05.md)、[sender-pipeline-verified-2026-10-05.md](sender-pipeline-verified-2026-10-05.md)、[capture-probe-findings-2026-10-05.md](capture-probe-findings-2026-10-05.md)、[w2-chat-file-screen-2026-10-04.md](w2-chat-file-screen-2026-10-04.md)、[audio-quality-investigation-2026-10-04.md](audio-quality-investigation-2026-10-04.md)、[media-engine-architecture-2026-10-04.md](media-engine-architecture-2026-10-04.md)、[rtc-windows-implementation-2026-10-04.md](rtc-windows-implementation-2026-10-04.md)、[ui-design-guideline-2026-10-04.md](ui-design-guideline-2026-10-04.md)、[voice-collab-design-2026-10-04.md](voice-collab-design-2026-10-04.md)、[release-notes-v1.0.md](release-notes-v1.0.md)、`src/winui-cs/`、`src/winui-videoprobe/`、`src/media/`、`tests/`、`build/`、`dist/`。*
