# 方向裁决与优先级（2026-10-06 · 用户口述）

> 记录**用户明确的取舍**，避免下一轮把已作废的方向又捡回来。
> 原话："e作废优先执行是 / e和c作废优先执行s 其次a后d，f暂时废弃"

## 执行顺序（用户指定）

| 顺序 | 范围 | 状态 |
|---|---|---|
| 1 | **S**（S2→S6 模块化迁移） | S2 ✅ 完成（2026-10-06，`59f8055`）；S3–S6 待做 |
| 2 | **A**（用户真机反馈里还没关的功能缺陷） | 待做（A1 共享音频失效 → A2 屏幕共享时好时坏 → A3 成员列表只显示两人 → A4 房间概念 → A5 远端控制） |
| 3 | **D**（测试与验证缺口） | 待做（多对端三实例、跨网段中继真机、引擎并发压测、音质主观判定、点击穿透） |

## 作废（用户明确说"作废"，不要再做）

- **E 作废 —— 长期路线文档**：`docs\voice-collab-design-2026-10-04.md` §976+ 那套
  **libwebrtc 原生栈**阶段表（739 MB 依赖、AEC3、SQLite 历史、DesktopCapturer、1080p30 验收）
  **整体作废**。当前项目走的是 WebView2 + JS WebRTC，不要再按那套推进。
  （文档保留作历史，不再当路线。）
- **C 作废 —— 交付与可用性那几项**：`SettingsWindow.xaml` 拆分、给语音线补使用说明、
  砍本地化目录省体积、安装包/MSI 形态 —— **都不做**。
  注：其中"设置改独立窗口"原本是 `fix-plan` 的待办，用户已有言在先"黑边可后置"；
  本次明确整类作废，设置继续留在主窗口浮层。
- **F 暂时废弃 —— 杂项收尾**：文档里"45 项"改 45 项、`run-checks.py dist` 跑 0 项、
  契约清单手写、引擎重采样策略、开源收尾确认 —— 全部暂缓，等 S/A/D 走完再说。

## 对 S 系列的额外说明

- **S1 的契约登记已被 S2 复用**：`BridgeContract` 是真源，StaticGuard（run-checks 第 15 条）
  从它抽取校验；S2 拆桥没有改契约，启动对账仍 PASS（事件 73 种 / 方法 23 个）。
- **S2 的判据与结论**（可复核）：
  - `MainWindow.xaml.cs`：4108 → **3800 行**；`JsonDocument.Parse` **0 处**；
    `Process.Start` 只剩 **2 处纯 UI 动作**（打开资源管理器 / 打开系统声音设置，已注释说明）。
  - 抽出：`ProbeLocator` / `EngineProbe` / `NetworkProbe` / `FileStore` /
    `SharedAudioService` / `ScreenShareService`。
  - 回归：dotnet build 0 错；界面自检全 PASS；`[契约] 契约对账通过`；
    `[设置] 设备（引擎枚举）：麦克风 3 个、扬声器 6 个`；共享声音采集泵跑到 `块=12 字节=230400`。
- **S2 之后的 S3**：`call.html`（25 行壳 + 15 模块）→ shell + core + 5 模块，`link` 显式化、删单例别名；
  判据：双实例/三实例自检通过 + "共享声音停一次再开仍能出声"。

## S3 进展与"静默失败"这条线的教训（2026-10-06 续）

**进度**（每个版本都跑 16→19 项闸门 + 界面自检 + 双实例，全绿才出包）：

| 版本 | 内容 |
|---|---|
| v40 | `call.html` 拆壳（3247→25 行），`call.js`/`bus.js`/`log.js` |
| v41 | `link.js`（一条链路 = 一个对象）+ 单例别名守卫 + **双实例联机闸门** |
| v42 | `voice.js`（电平表） |
| v43 | `screen.js`（屏幕共享；"给链路挂轨"两份实现收敛成一份） |
| v44 | `selfcheck.js`（信号链自检 / 链路快照） |
| v45 | `file.js`（文件传输；收发状态收进工厂闭包） |
| v46 | `recordSample` 进 `voice.js` + `util.js`（纯工具不再靠注入） |
| v48 | `webrtc.js`（建 PC / ICE），并修掉下面那个严重 bug |

**S3 期间共 11 次"机器抓到的问题"**（都不是人眼看出来的），其中三次直接避免线上事故：

1. **`log.js` 忘了 `export`** ⇒ 整页一行不执行（C# 只看到"未上报 engine-loaded"）。
2. **C# 生成 JS 用裸名 `zxEngine`** ⇒ 模块作用域 `ReferenceError`，共享声音悄悄失效。
3. **多实例窗口偏移把窗口推出屏幕**：`offsetX=(端口−默认端口)×60`，默认端口在中间 ⇒
   实测端口 46101 得到 offsetX=12660、窗口落在 x=13030（屏幕只有 1920 宽）。
   用户看到的就是"点了没反应"，与审计里"进不去主界面"同一类现场。

**最严重的一次：`buildRtcConfig` 缺失导致建链静默失败（v47 翻车 → v48 修）**

- 现象（**没有任何错误日志**）：双方都看到 `[成员] 2 人`，但两侧**链路 0 条**、
  无 `通话判定`、无 `scriptError`。单实例界面自检全绿，只有**双实例闸门**红了。
- 根因：`createPeerConnection` 搬进 `webrtc.js`，而它依赖的 `buildRtcConfig` 还留在 `call.js`
  ⇒ 建链抛 `ReferenceError: buildRtcConfig is not defined`；异常被 `welcome` 分支的
  `try/catch` 吞掉，而 catch 里只调了页面的 `log()` ——**只写隐藏 DOM，应用日志完全看不到**。
- 修法（两条都要）：
  1. `createPeerConnection` 不再依赖外部函数，直接读注入的 `io.state.iceServers`；
  2. 新增 `link-error` 事件：建链失败的原因（含前 3 行堆栈）**必须 post**，进应用日志与界面。
  顺带加 `link-debug`（welcome 时上报"看到几个对端"）—— 正是它把范围从"链路不建"
  缩到"welcome 有 1 个对端但建链抛异常"。

**由此固化的两条规矩**（已写进代码注释与守卫）：

- **`log()` 不算报错**：页面 `log()` 只写隐藏 DOM。任何"用户/自检需要知道"的失败都必须
  `post()` 走应用日志；反之，应用日志里没有的失败 = 不存在（审计"功能悄悄不生效"的成因）。
- **搬运依赖必须整块搬**：把函数搬出去时，它**依赖的私有函数**要么一起搬、要么改成注入。
  单实例自检看不出来（那条路径根本没走到），只有双实例闸门能兜住 —— 这就是双实例闸门的价值。

**新增/加固的守卫（第 16→19 项）**：
`check_page_module_loads`（Node 假浏览器真 import 一次）、`check_page_singletons`
（单例别名只许出现在兼容视图，反例验证过）、`check_two_instance`（双实例联机）。
另外把契约守卫改成**先剥 C# 行注释再取字符串字面量**（注释里写带引号的说明会造假失败）。

### 第二次"零日志"事故：`/signaling.js` 被当信令端点（v48 之后，v49 修）

| 版本 | 内容 |
|---|---|
| v48 | `webrtc.js`（建 PC / ICE）+ `link-error`/`link-debug` 取证通道 |
| v49 | `signaling.js`；修 HTTP 路由；新增**路由冲突守卫**（第 20 项） |

- 现象（依然**没有任何错误日志**）：C# 只报"通话页未上报 engine-loaded"。
  连新加的 `js-error` 通道都没触发 —— 因为**模块图解析失败不会触发 `window.onerror`**。
- 定位过程（值得记住的方法）：Node 假浏览器能加载 → 真浏览器 + 本地 HTTP 也能加载
  （日志显示 `link-debug`/`engine-loaded` 都正常发出）→ 于是**直接拿 HTTP 去问运行中的应用**：
  逐个文件请求，发现 **`/signaling.js` 返回 404**，而 `/webrtc.js` 等同目录文件都是 200。
- 根因：`SignalingServer` 的路由写的是 `path.StartsWith("/signal")` ——
  新增的 `media\signaling.js` **以 `/signal` 开头**，于是被当成信令 WebSocket 端点 ⇒ 404。
- 修法：路由判据改成"`/signal` 本身、`/signal/…`、`/signal?…`"三种精确形态。
- 新增守卫 `check_media_route_collisions()`：把 `SignalingServer` 里 `Equals("/x")` /
  `StartsWith("/x")` 的路径抽出来，与 media 下每个文件名的请求路径做**按 C# 语义**的匹配。
  两面验证过：把路由改回旧写法 ⇒ 守卫立刻报出这条 bug（并指向 `signaling.js`）。
  守卫自身也踩过一次假失败（我修路由的**注释**里写了 `StartsWith("/signal")`）⇒ 已先剥 C# 注释。

**这两次事故的共同教训（写进规矩）**：
1. **`log()` 不算报错**：任何自检/用户需要知道的失败都必须 `post()`。
2. **搬运依赖必须整块搬**（`buildRtcConfig` 那次）。
3. **页面整体不启动时，先在 HTTP 层取证**：Node/真浏览器都能加载 ≠ 应用能加载；
   应用走的是本地 HTTP + 自己的路由，`/xxx` 这一类前缀很容易被内部端点截胡。

### S4（方案 A，用户 2026-10-06 选定）：模块宿主 + 统一释放

原清单写的是"`IModule`/`IModuleHost` + 5 个模块"，但 S3 实际拆出了 13 个页面模块
（`bus/log/util/link/voice/screen/file/selfcheck/webrtc/signaling/stats/mesh` + `call.js`），
且 `chat`/`shareaudio` 并未独立。用户选定**方案 A**（页面侧纯 JS 约定，不引入跨语言抽象）：

- 新增 `media\modules.js`：`createModuleHost({log,post})`
  - `wire({name, create, deps})` —— 声明式接线，顺序即依赖顺序；
  - `addDisposable(name, fn)` —— 页面自己持有的资源（本地流/ws）；
  - `disposeAll(reason)` —— **按注册相反顺序**逐个释放，逐个 try/catch，
    任何一个模块释放失败都不影响其它模块并上报 `module-error`。
- 各模块的 `dispose()` 是释放真源：`stats`（停统计）、`voice`（停电平表）、
  `screen`（停采集）、`mesh`（关所有链路）、`signaling`（关 ws）。
- `stopAll()` 第一句就是 `moduleHost.disposeAll('stopAll')`；`onPageGone` 同样先释放模块。
- C# 侧：`MediaEngine.DisposePageAsync()` + `AppWindow.Closing` 里 await 它再放行关闭
  （页面自己的 `pagehide` 在 WebView2 关窗时**不可靠** —— 实测走正常关闭路径也没有释放记录；
  且在 `Window.Closed` 里 ExecuteScript 已经执行不到，因为 WebView 正在销毁）。

**判据（新增第 21、22 项守卫，都可复核）**：
- `check_page_modules_shape()`：每个 wire 的模块必须有 dispose 出口；内联 dispose 必须被 import。
- `check_modules_dispose()`：Node 假浏览器加载页面 → 调 `stopAll()` →
  断言发出 `modules-disposed` 且 `failed` 为空。实测输出：
  `{"type":"modules-disposed","released":6,"failed":[],"reason":"stopAll"}`。
- 全场回归：`release.py` **PASS 22 / FAIL 0**（含界面自检、双实例联机）。

**一条踩坑记录**：守卫自己两次假失败 —— ① 把我的注释文字当成了现役代码；
② 用 `re.search("return {…}")` 匹配工厂出口时，因为文件里还有别的 `return` 而认错位置。
最终改成"从工厂函数结尾往回找最后一个 `return {`，在该段内找 dispose"，判据硬且不猜。

---

## A1「共享音频完全失效」—— 已定位并修复（2026-10-06）

### 现象与取证方法
用户原话："这个音频共享功能是完全失效的。"

关键教训：**这条链路的每一段都要单独出数字，否则只能对着"没声音"猜**。分段口径与实测：

| 段 | 证据 | 修复前 | 修复后 |
|---|---|---|---|
| ① 采集（WASAPI 回环） | `[共享声音] 泵状态：块=N 字节=M` | ✅ 在跑（11.5 MB） | ✅ |
| ② 页面收 PCM | `pushed` | ✅ 600 块 | ✅ |
| ③ worklet 出样本 | `nodeRms` / `destRms` / `maxOutPeak` | ✅ 0.32 / 0.32 / 0.55 | ✅ |
| ④ 音轨 → RTP | `outboundDump` / `audioOutBytes` | ❌ **`[]` / 0** | ✅ 1545 包 / 181702 字节 |
| ⑤ 对端接收 | `audioInBytes` / `audioInEnergy` | ❌ 0 / 0 | ✅ 181291 / **29.75** |

（为让 ①有真实音频，测试用 `PlaySoundW` 循环播放一段已知 WAV —— 峰值 0.559，
于是 ③ 的 RMS 0.33 可被解释、不是静音；这一步是本轮能定性的关键。）

### 根因（两处叠加）
1. **异步竞态**：`registerLink` 里写的是
   `io.ensureSharedAudioTrack().then((t) => { if (t) io.attachSharedAudioTrack(id, pc); })`
   —— 调用方紧接着 `createOffer()`，音轨**还没就绪**，所以**首次协商里没有这条轨**。
2. **从不重协商**：`pc.addTrack()` 只是把轨放进 sender，**不会生成 m-line**；
   而代码里那条"重协商"路径在早前被判定"实测反复失败"后**删掉了**。
   ⇒ 结果：`getStats()` 里该链路**一条 `outbound-rtp` 都没有**，RTP 恒 0 字节。

### 修法
- 新增 `prepareSharedAudioForNewLink(id, pc)`：**建链前**先把常驻音轨准备好并挂上，
  调用方**等它完成**再 `createOffer()`/`createAnswer()`（发起方与应答方两条路径都接了）。
- 新增 `attachAndRenegotiateSharedAudio(id, link)`：用户"先建链、后点共享开关"时，
  对已存在链路补挂轨并**重协商**（`createOffer → setLocalDescription → sendSignal`）。
  已经有轨的链路直接跳过，不做多余协商。
- 顺带把两处静默失败改成上报：`pushSharedAudio` 失败（`share-audio-push-error`）、
  重协商失败（`share-audio-error phase=renegotiate`）。

### 新增诊断（都进应用日志，可复核）
`ctxState` / `hasNode` / `nodeRms` / `destRms` / `audioTrackState` /
`worklet{gotMsgs,gotSamples,processedFrames,maxOutPeak,starved,lastN,lastChannels}` /
`outboundDump` / `senderInfo`。

**一条副产品发现**：这个 AudioContext 的输出是**单声道**（`lastChannels: 1`），
而 worklet 里把"每帧 128 帧 × 2 声道"写死 ⇒ 每帧多取一倍样本、奇数位样本被整批丢掉。
已改为按实际声道数消费（`need = n * outCh`）。

---

## A3「成员列表只显示两人」—— 当前代码**已不复现**（2026-10-06 实测）

**结论先说**：三实例实测下，三方界面与内部状态**都是 3 人**。
A3 的症状在 S2/S3 的改动（成员表数据驱动 + mesh 对每位对端建链）之后已经消失，
本轮把它**变成了可复核的判据**，而不是"看起来好了"。

### 取证（三实例：host + p2 + p3）

| 口径 | host | p2 | p3 |
|---|---|---|---|
| 信令侧成员行 | `[成员] 3 人：p2、p3、host（我）` | `host、p3、p2（我）` | `host、p2、p3（我）` |
| 链路注册表 | `2 条` | `2 条` | `2 条` |
| **界面** `MemberCountText`（UIA 读） | `成员 — 3` | `成员 — 3` | `成员 — 3` |

界面成员行也确实画了三行（UIA 抓到的名字文本：`p2` / `p3` / `host`，各带首字头像）。

### 为什么日志口径不够、必须读 UIA
`[成员] N 人` 只是 C# **内部状态**；用户看的是界面。两者不一定一致
（历史上就出现过"状态对、界面没画"）。新增 `build\three-instance-members.py`：
用 UIA 读每个实例的 `MemberCountText` 实际文本 + 与 roster 名单交叉核对。
**它只做 UIA 查询、不发键鼠**，所以用户在用电脑时也能跑。

### 本轮顺带加的可观测性
- `roster-update` 事件（page → C#）：每次发 roster 都带"人数/名单/触发原因"，
  用于判定"界面少人"到底是**页面发少了**还是 **C# 漏更新**。实测时序正确：
  `welcome count=0` → `peer-joined count=1` → `peer-joined count=2`。
- 共享声音诊断字段压缩（`outboundDump=[kind,bytes,pkts]`、`senderInfo=[kind,en,muted,isShared]`），
  信息等效但日志更短。

---

## A2「屏幕共享有时能显示、有时显示不出来」—— 已定位并修复（2026-10-06）

### 直接原因（实测抓到，不是推测）
应用日志里反复出现：
```
[JS 异常] Uncaught ReferenceError: remoteVideoEl is not defined @ http://127.0.0.1:46701/webrtc.js:103
          ← at pc.ontrack (webrtc.js:103:7)
```
`webrtc.js` 的 `pc.ontrack`（对方开始共享时触发）里**直接引用了 call.js 的 `remoteVideoEl`** ——
模块化之后那个变量不在本模块作用域 ⇒ 每次收到对方的屏幕轨都抛异常 ⇒ **ontrack 后续代码全部中断**
⇒ 画面永远接不上。这就是"有时候显示不出来"的直接原因（不是"时好时坏"，是"只要走到这条路就断"）。

同一次排查还抓到另一个跨模块引用错误：
`redactIceUrl`（定义在 call.js）被 `webrtc.js` 使用 ⇒ ICE 配置应用路径抛异常。

**修法**：
- `webrtc.js` 改用注入的 `io.state.remoteVideoStream` 判断"是否已在观看"，不再摸别的模块的变量；
- `redactIceUrl` / `summarizeIceServers` 搬到 `webrtc.js`（它们在那里被用），call.js 改为导入。

### 顺带补的可观测性（都是排查必需）
- **采集轨自己结束**（用户点"停止共享"/采集源消失）→ 新增 `screen-track-ended` 事件，
  页面按"共享已停止"走清理，C# 明确记录 —— 用于区分"用户主动停"与"共享莫名断了"。
- **JS 异常必须带文件名**：原来只记行号，13 个模块下"第 103 行"根本判不出是哪个文件
  （本轮就是靠补上 `@ file:line` 才定位到 webrtc.js:103）。

### 判据（新增第 25 项闸门）
`build\screen-share-check.py`：host 用合成画面共享、join 当观众，要求
① host 共享成功 ② join 侧 `收到画面帧 N 帧` 至少 2 条且**单调增长** ③ 两侧无
`screen-error`/`scriptError`/`采集轨结束`。
修复前后对比：**0 条帧记录 → 63/126/190/253/317/381…（持续增长）**。

### 仍然只能人工验的部分（真实 getDisplayMedia）
系统弹窗选源无法自动化。已确认现状可分辨：`screen-error` 会区分
`cancelled`（用户取消/弹窗被挡）与 `notReadable`（采集源被占用，可重试），
界面文案也分开写。下次真人测试时若再现问题，日志里直接看 `screen-error` 的字段即可定性。

---

## 产品化（2026-10-06 晚）：真正的房间系统 + UI 去重 —— v57

> 用户指令："放下后面的任务，做一个真正能用的版本：有房间系统、能加多个房间、UI 功能不重复。"
> 拍板：**A + C（本地房间列表 + 局域网改进）/ 删掉引导浮层 / 房间列表放左栏**。

### 改前的真实状态（UI 审计实测）
- **重复入口**：左栏 `SidebarConnectButton` 与中栏 `SidebarConnectButton2` **文案都为「联机 / 换房间」**，
  且都调同一个 `ShowWelcome()`；`WelcomeOverlay` 一个浮层里混了"起名字 + 选房间 + 填地址 + 不联机进入"。
- **没有房间系统**：只有一个"当前房间"；房间名在设置里改，**改完要重启**（代码注释自认）；
  不能保有多个房间、不能切换、重启即忘。

### 改后（本轮）
| 能力 | 实现 |
|---|---|
| 多房间列表 | `RoomStore`（`src\winui-cs\src\RoomStore.cs`）：增/删/改/切换 + 稳定 id |
| 重启还在 | 持久化进 `app-config.json`（`AppConfig.RoomList`），**离线单测验证往返** |
| 左栏可见 | 每项 = 名字 + 主机地址 + **状态点**（未连接/连接中/已连接）+ 单一入口「新建 / 加入房间」 |
| 点击切换 | 房主项走 `start`、加入项走 `join`（都是已有能力），状态实时更新 |
| 房间名单一来源 | 房间名改动**立即生效**（`ApplyRoomNameAsync`，房主则按新房名重连）—— 不再要求重启 |
| UI 去重 | 删掉重复的 `SidebarConnectButton2`；`WelcomeOverlay` 仅作"新建/加入房间"面板 |

### 判据（都可复核）
- `check_room_store`（第 28 项闸门）：11 条用例，含**持久化往返**（写盘→读回→条数/当前项/名字一致）。
- `check_ui_no_duplicate_entry`（第 27 项）：扫 XAML 所有按钮文案，**同名按钮即失败**。
  实测 18 个按钮、18 种文案、无重复。
- 界面自检（真键鼠）PASS；左栏截图核对过（`tmp\room-list.png`）。

### 本轮顺带抓到的真 bug（都是"静默失败"那一类）
1. **`AppConfig.Save` 把异常完全吞掉**（`catch { return false; }`）⇒ "保存失败"查不出原因。
   本次给 `LastSaveError` 记录原因，才定位到根因 ↓
2. **应用容器不允许写系统 TEMP**（`UnauthorizedAccessException`）⇒ 单测改用应用自身目录。
3. **`Server.MapPath` 式的"死代码"**：无。

---

## 产品化第二轮（2026-10-07 凌晨）：房间可管理 + 双实例验收 + 三处真 bug —— v60

### 本轮修的三个真 bug（都是"用户会遇到"的）
1. **房主建房后 1 秒掉线**（实测 `rooms-host.log`：`信令连接断开` 紧跟 `信令已连接`）。
   原因：验收模式跑完就退出，**没有"建完继续当房主"的形态** ⇒ 双实例联机根本测不了。
   修：加 `--create-room-keep`（建房后保持当房主，不退）。
   **这条也是产品行为的正确形态**：用户点完"创建房间"当然要继续在线等人。
2. **预检诊断写进 exe 同目录**（`join-preflight.txt`）：两个坏处 ——
   ① 打包/守卫把它当残留（v60 第一次发布真的被拦下）；
   ② 真装到 `Program Files` 时那目录**只读**，诊断信息反而拿不到。
   修：统一写 `logs\` 子目录（与日志同处），并在 cmd 清理段与服务端守卫里都登记。
3. **状态显示不随实时事件变化**：信令断开后房间仍显示"已连接"（假状态）。
   修：`signal-closed` → 当前房间回「未连接」。

### 本轮新增能力
| 能力 | 实现 |
|---|---|
| 房间可改名 | 房间行右侧铅笔按钮 → `ContentDialog` 输入新名；改当前房间时房间名同步（对方要用同一个名字） |
| 房间可删除 | 房间行右侧垃圾桶按钮 → 从列表移除（附提示：不会断开已建立的连接） |
| 局域网加入带房间名 | 选中局域网房间时**自动切换房间名**（原来只填地址、房间名不动 ⇒ 连上也看不到对方） |
| 局域网房间记进列表 | 加入后自动进房间列表，下次一点即回，不用再扫 |

### 验收（可复核）
- 新增闸门 `check_room_create`（第 29 项）：建房走**界面同一条 `CreateRoomAsync`**，
  断言"房间进列表 + 左栏渲染出该项 + 邀请串含房间名"。
- **双实例实测 PASS**（`tmp\two-instance-rooms.py`）：host 建房保活 → 加入者用邀请串
  `房间名@地址` 加入 → **双方都收到 welcome**（真的在同一房间，不只是记了一笔）。
- 房间列表离线单测 12/12（含持久化往返与 UTF-8 BOM）。

### 仍要你人工确认的
自检里"用模拟鼠标点创建按钮"这条**不作为功能判据**：实测桌面有别的窗口（文件资源管理器等）
压在应用上时，鼠标模拟不可靠（环境问题）。界面自检只断言"入口存在 / 按钮可点 / 坐标在屏幕内"，
功能正确性交给 `check_room_create`。

---

## 用户实机反馈 5 个问题（2026-10-07 凌晨）—— 逐条定位与修复（v61）

用户原话：① 退出房间成员不刷新 ② 房间名不同步 ③ 自动扫描加入不可用 ④ 文字和语音不可用 ⑤ 共享音频不可用。

### ④ 文字不可用 —— **真 bug，时序问题（本轮最重要的修复）**
日志铁证：
```
[聊天诊断] open-scan state=open label=chat err=3e4345f8:null
[聊天] 通道尚未打开，消息已排队
```
根因：`setupChatChannel()` 建完 DataChannel 后立刻执行
`setLinkChat(id, primaryChat())`，而那一刻 `dc.readyState` 还是 `connecting`，
`primaryChat()` 只认 `open` ⇒ **存进 `link.chat` 的是 null**。
通道后来 open 了，但没人再写回去 ⇒ 通道成"孤儿"：能 open、能发，**但 `primaryChat()` 永远找不到它**
⇒ 文字永远发不出去（用户看到"聊天通道未就绪"）。
**修法**：`setupChatChannel(pc, isInitiator, linkId)` 把通道**创建时就绑到所属链路**；
`primaryChat()` 也改成"优先返回任何已 open 的通道"；另加排队机制（通道晚开 1 秒也能发出）。
**验证**：新增闸门 `check_chat_end_to_end`（第 24 条守卫）——双方各发一条，
两边都必须有 `[收到消息]` 与 `[聊天] 已发出`。实测通过。

### ② 房间名不同步 —— 真 bug
`RoomTitleText`（中栏「房间 X」）只在"信令连上"时被写，而**三条改房间名的路径**
（选局域网房间 / 点房间行 / 设置里改名）都不碰它 ⇒ 标题停在旧房间名。
**修法**：新增唯一设置点 `SetCurrentRoom(name, reason)`，三条路径统一走它。

### ③ 自动扫描加入不可用 —— 真 bug（可用性）
扫描**能发现**对方（日志 `[局域网] 发现 1 个房间`），但「加入选中的房间」要求
用户先在下拉框里**选中**一项；没选中时只得到一句"先选一个"，用户就判断功能坏了。
**修法**：加「加入扫描到的房间（一键）」按钮；没有扫描结果时明确说明原因与替代办法。

### ① 退出房间成员不刷新 —— 实测**是会刷新的**（我的测试脚本判据写错了）
实测 3 实例 → 关掉 j2 → host 的成员数 `3 人 → 2 人`（22 秒内），j1 同样 `3 → 2`。
服务端在断开时确实广播 `peer-left`，页面收不到人数不会留旧值。
（用户看到的可能是**幽灵对端**：见下。）

### ⑤ 共享音频不可用 —— 机制验证通过
端到端闸门 `check_share_audio_end_to_end` PASS：两侧各约 18 万字节、能量 29.8；
本轮实测采集泵 `块=922 / 17,769,600 字节`（真在推 PCM）。

### 本轮另外抓到并修掉的两个真 bug
1. **联机面板最底部的按钮被挤出窗口**：加了「方式三：创建房间」后内容变高，
   而面板窗口只有 620 高 ⇒ `WelcomeEnterButton` 在 UIA 里是 **NOT_IN_TREE**（用户点不到）。
   修：内容加 `ScrollViewer` + 窗口高度 620 → 780。实测修后 `offscreen=False rect=774,772`。
2. **自检里的真鼠标点击在本机被证明不可靠**：同一台机器上真鼠标连点 5 次中栏按钮，
   应用日志**一条处理器记录都没有**；换 UIA `Invoke()` 一次就触发。
   修：新增 `ui_click()`（UIA Invoke，默认真鼠标，`--real-mouse` 可强制）。
   这条很重要 —— 否则"点击类判据"会长期处于不可信状态。

### 环境问题（不是产品缺陷，但真实影响用户）
本机有 **11 个 10/05 起就挂着的 ZongxianVoice 僵尸进程**，`taskkill`/`Stop-Process`/WMI 都
**拒绝访问**（ReturnValue=2），只能重启清理。它们仍在参与局域网广播 ⇒ 会污染"自动扫描"结果、
把幽灵对端塞进房间。**用户下次开机（重启）即可清掉。**

---

## 用户追问"改名不刷新、删房间别人还在"——**两个真 bug + 我的一处漏测**（v62）

### 我的错：判据测错了代码路径
上一轮我说"① 退出成员不刷新 = 实测会刷新"，依据是我自己的测试脚本。但**用户实机点的是
房间行右侧的「改名 / 删除」按钮**，走的是 `RenameRoomAsync` / `DeleteRoomAsync`，
而我当时只测了命令行建房路径（`--create-room-test`）—— **两段代码不是一回事**。
这是"判据必须先自证"的又一次翻车：我的判据没有覆盖用户实际点击的那条路径。

### 真 bug 1：改名不重连（`RenameRoomAsync`）
原实现：改 `_rooms` / `_room` / 存盘 / 重绘列表 —— **完全没有碰连接**。
房间名是信令房间的一部分（`ws://…/signal?room=X`）⇒ 不重连的话：
自己停在旧信令连接、对方也停在旧房间名，成员列表永远不变。
**修**：改名当前房间 = 房间名变了 ⇒ 必须重连（抽出 `ReconnectHostRoomAsync`，
与设置里改名走同一段逻辑）。

### 真 bug 2：删房间不断连（`DeleteRoom`）
原实现的注释自己写着"**只删记录，不断开已建立的连接**" —— 所以用户"删了房间别人还在房间"。
**修**：删的是**当前房间** = 用户明确要离开 ⇒ 先 `stopAll`（关信令、清链路、停采集，
对方收到 `peer-left` 从房间消失），再清成员列表、切换/重开剩余房间。
删非当前房间仍只删记录（不影响已建立的连接）。

### 顺带抓到的第 3 个真 bug：`describeIceFailure` 在停机后抛异常
`stopAll` 之后 ICE 状态回调仍会触发，此时 `io.state` 已释放 ⇒
`Uncaught TypeError: Cannot read properties of undefined (reading 'state')`（webrtc.js:35）。
**修**：加空值防御，返回"连接已停止"而不是抛异常刷屏。

### 验收（新增第 31 项闸门 `check_room_rename_delete`）
测试开关**走与界面按钮同一段代码**（`RenameRoomAsync` / `DeleteRoomAsync`）：
```
[改名验收] 结果 PASS（房间名已变=新房间名，重连已触发）
[删除验收] 结果 PASS（房间已删，成员已清空）
```
另外双实例实测：host 停止/删除后，join 侧正确收到"信令断开"、通话态回未连接、成员清到 1 人。

### 教训（已写进 `handoff-next-session.md` §3）
**判据必须覆盖"用户实际点击的那条路径"**。命令行后门与界面按钮即使"逻辑相同"，
也可能因为实现分叉而完全不是一回事 —— 本轮就是分叉了（`ApplyRoomNameAsync` 有重连、
`RenameRoomAsync` 没有）。

---

## ⚠️ 我自己引入的回归 + "名字修改完全失效"根因（v63）

用户反馈："bug 越来越多了"、"只修好了音频分享"。逐条查后**确认其中一批是我上一轮引入的**。

### 回归源：`selectBestPrimary()` —— 聊天通道打开时切换了**媒体主链路**
v61 我为了修"文字发不出去"，加了 `selectBestPrimary()`：在聊天 DataChannel `onopen` 时
**把主链路切到"有 open 聊天通道"的那条**。这是错的：

- **主链路是媒体（语音 / 屏幕共享 / 文件传输）走的路**；
- 聊天**不需要**它 —— `primaryChat()` 已改成"在所有链路里找 open 的通道"；
- 房间里有多条链路时，它把主链路切到错误的一条
  ⇒ **屏幕共享、文件传输、语音同时失效**。

对应用户报的："3 语音通话不可用 / 4 原本好的分享屏幕也坏了 / 6 文件传输用不了了"。
**修法**：删掉该调用，并把函数体标为"禁止调用"（保留仅为可追溯）。
新增判据：文字端到端、屏幕共享端到端（合成画面）复跑均 PASS。

**教训**：为修一个问题而**动到别的功能所依赖的共享状态**（这里是"媒体主链路"）时，
必须把受影响的端到端判据**全部复跑**。我上一轮只跑了文字判据就收工了。

### "名字修改完全失效"根因（真 bug，与回归无关）
```csharp
private string SelfDisplayName() {
    var n = (SelfNameBox.Text ?? "").Trim();
    if (n.Length > 0) return n;      // ← 只用于本地显示
    return ...;
}
```
- `SelfNameBox` **从不写回 `_selfName`**（信令 join/start 广播给别人的名字）；
- **也不存盘** —— `AppConfig` 里根本没有名字字段；
- 后果（与用户截图完全吻合）：自己看到「我11」，**别人看到的仍是「我」**；重启后名字丢失。

**修法**：
1. `AppConfig` 加 `SelfName`（字段 + Load + Save + DTO）；
2. `OnCloseSettings()` 读完名字后写回 `_selfName` + 存盘 + **重连**
   （名字是 join/start 广播的参数，不重连对方感知不到）；
3. 启动时从配置恢复名字（命令行 `--name` 优先）。

**验收**（新增第 32 项闸门 `check_name_change`，走设置面板「关闭」同一段代码）：
```
信令连接加入 room=端到端房间 name=旧名字      ← 改名前
[设置] 已保存：名字「新名字甲」· 写盘成功
[设置] 按新名字重连（本机当房主 / 房间 端到端房间）
信令连接加入 room=端到端房间 name=新名字甲    ← 重连后广播新名字 ✓
[名字验收] 结果 PASS（内存=新名字甲 配置=新名字甲，已重连广播）
```

### 同时修的：设置里改房间名也不存盘
`OnCloseSettings()` 里房间名只改 `_room` + 标题，**没走 `SetCurrentRoom`、没存盘**，
注释还写着"重启后完全生效"（假的）。已改为统一走 `SetCurrentRoom` + 存盘 + 重连。

### 仍待确认（用户报但尚未定位的）
- **"列表问题未解决"**：表述不够具体，需要用户说明是"房间列表不刷新"还是"局域网扫描列表"。
- **"本机无法屏幕共享、另一个可以但没有画面"**：
  截图提示"捕获被占用，已清理，可重试"=`NotReadableError`。这通常是**同一会话里已有
  另一个实例正在采集屏幕**（Windows 侧互斥）。"没有画面"部分可能同样是上面的主链路回归，
  已在 v63 修复，需复测。

---

## v64：4 个真 bug（其中 3 个是我上一轮引入或漏测的）

用户原话："重启电脑过了还是一样 / 1 语音不可用 2 文件传输点了没反应 3 文字无法输入
（对话框点不动）4 成员栏完全错乱 5 房间名依旧没同步"，并指出"bug 越来越多了"。

### 【真 bug 1】输入框被禁用 ⇒ "打不了字"（**我漏测**）
`MessageInput.IsEnabled = _chatReady`，而 `_chatReady` 只在收到 `chat-open` 时为 true。
根因（mesh.js）：**只有"本端第一条链路"才绑定聊天通道**
```js
const isPrimary = io.links.size === 1;
if (isPrimary) { setupChatChannel(...); } else { /* 只记日志，不建通道 */ }
```
当 `links.size > 1`（房间里有多条链路/幽灵对端）时，**房主永远不会有聊天通道**
⇒ `_chatReady` 恒 false ⇒ 输入框禁用。**这解释了"时好时坏"**（取决于当时几条链路）。
**修**：非主链路在"当前没有已 open 的通道"时也补建（注意判据要用 `readyState==='open'`，
`primaryChat()` 的兜底会返回未 open 的通道对象，用它判断会永远不补建 —— 本轮踩到）。
**新增守卫 `check_input_clickable`**：双实例在线时量 UIA 的 `IsEnabled`。

**教训（第二次同类错误）**：我的"文字端到端"守卫用 `--chat-test` **直接调 SendChatAsync，
绕过了输入框** ⇒ 判据 PASS 但用户打不了字。**判据必须走用户实际路径**。

### 【真 bug 2】同一实例在房间里有两个身份 ⇒ "成员栏完全错乱（7 人）"
`CreateRoomAsync` 建房时会**再调一次 `start`**，而页面的 `start()` **不关闭旧信令连接**
（`join()` 会关）⇒ 同一实例在服务器里留下两个 id。
实测日志：`peer-joined ids=[867a2e1c]` 与 `welcome ids=[4fccccbe]` 并存，
随后"**与 867a2e1c 建立第 2 条媒体链路**" —— 自己跟自己建链。
**修**：房主的所有重连（建房/改名/切房间/设置改名）统一改走 `join()`
（它有"先收旧再连新"的完整顺序）。
⚠️ 途中还踩到一个**我自己引入的坑**：在 `start()` 里直接 `close()` 旧 ws 反而更有害 ——
旧 ws 的 `onclose` 会在新连接**之后**回调，把状态判成"信令断开"、链路被清空
（实测 `[链路] 注册表 0 条`）。已回退，改为走 join。

### 【真 bug 3】加入方的名字传不到对端 ⇒ 成员名显示成 id 前 6 位（"147148"）
页面 `start(cfg)` 会设 `state.selfName`，但 **`join(cfg)` 完全不碰它**（初值空串）
⇒ 加入方拼信令 URL 时 `name=` 为空 ⇒ 服务端 `DisplayName` 空 ⇒ 对端只能显示
`Short(x.Id)`（用户截图里的 `147148` 就是这个）。**修**：`join` 设置 selfName，
C# 侧 3 处 join 调用都传 `selfName`。新增守卫 `check_name_change` + 双实例验证双方能看到对方名字。

### 【真 bug 4】测试自己不可信 ⇒ "文字端到端时好时坏"（**我的问题**）
`--chat-test` 的等待循环跑在 `InitializeMediaAsync` 里，而**建房在它之后** ⇒
等待时房间里还没有对端 ⇒ 15 秒必然超时。之前那些 PASS 是**靠残留实例误打误撞连上来**。
**修**：改成**事件驱动** —— `chat-open` 一触发就发送，与任何执行顺序无关。

### 环境教训
`release` 里本守卫排在 30 个守卫之后，`app-config.json` 会累积历次测试房间
（实测 `左栏项数=11`）⇒ 幽灵对端干扰。已在守卫里强化清场（杀到 0 + 删 config）。

---

## v65：房主"信令断开" —— **我上一轮改动引入的**（根因在 connectSignal）

用户截图：房主窗口左下角红点 **信令断开**，但左栏房间是 **已连接**、媒体引擎显示
"等待他人加入（端口 45890 / 房间 faf）" —— **状态自相矛盾**，并且"根本无法连接"。

### 根因（`signaling.js` 的 `connectSignal`）
```js
function connectSignal() {
  const ws = new WebSocket(url);
  state.ws = ws;                              // ← 直接覆盖，完全不管旧连接
  ws.onclose = () => { post({type:'signal-closed'}); };   // ← 旧 ws 的回调仍然活着
  ...
}
```
v64 我把**房主的重连从 `start` 改成 `join`**，而 `join` 会 `close()` 旧 ws 再连新的。
于是：**旧 ws 的 `onclose` 在新连接建立之后才触发** ⇒ `post(signal-closed)` ⇒
界面显示"信令断开"（而实际连接是好的）。建房/改名/切房间都会触发。

**修法**：`connectSignal()` 在新建连接前，**先把旧连接的所有回调摘掉再关**
（`onopen/onerror/onclose/onmessage = null`），旧连接从此不再干扰新连接的状态。
同时删掉 `join()` 里那句裸 `state.ws.close()`（交给 connectSignal 统一处理）。

### 验收（新增第 34 项闸门 `check_signal_stays_alive`）
```
host：信令已连接 2 次 / 信令断开 0 次     ← 不再误报
join：收到 welcome = True                  ← 加入方正常连上
```

### 这是本轮最有价值的一处修复
"重连把状态搞乱"是这个项目反复出现的一类问题（本轮先后踩了三次：
① start 不关旧连接 ⇒ 两个身份；② start 里 close ⇒ onclose 误报；
③ join 里 close ⇒ onclose 误报）。**根因都在 `connectSignal` 不管旧连接**。
集中修在那一处之后，上述三种表现一起消失。

---

## v67：用户给的步骤直接指出"房间名"这条根因（**用户判断是对的**）

用户原话：
> "我把我的步骤告诉你，两边打开应用会先给自己改名，然后房主删掉我的房间再新建立一个房间，
>   成员再自动搜索加入"
> "也就是说这个bug是因为房间名这个bug一直没修好导致的"

### 按用户顺序复现 → 一击命中
```
房主：信令连接加入 room=开黑房 ✓（房主自己确实进了新房间）
加入方：扫描到 1 个房间：房间 default · ... · 端口 48030   ← 扫到的是 default！
加入方：一键加入 → 结果 FAIL（房间=default 成员=1）
```

### 根因（两层，都在"房间名"上）
1. **局域网广播的房间名在构造时被固定**：
   `_lan = new LanDiscovery(_signalPort, _room)` —— 房间名是构造参数，
   而 `EnsureLanDiscovery()` 只在启动时调一次 ⇒ **改名/建房后广播仍是旧名**。
2. **`CreateRoomAsync` 跳过了统一设置点**：
   ```csharp
   _rooms.CurrentId = room.Id;
   _room = name;              // ← 直接写，绕过了 SetCurrentRoom
   ```
   于是我在 `SetCurrentRoom` 里加的"房间名变了就重启广播"**永远不执行**。
   （我第一次修正是加 `RestartLanDiscovery()`，但它没被调用；第二次改用 `_lanRoomName`
   独立字段判断，仍不生效 —— 直到发现 `CreateRoomAsync` 根本没走设置点。）

### 修法
1. 加 `RestartLanDiscovery()`：按**当前**房间名重建广播
   （**不能**因为 `_lan == null` 就早退 —— 用户的流程里改名发生在广播启动之前）；
2. 用独立字段 `_lanRoomName` 记录"广播里实际用的房间名"，与目标名不同就重启；
3. **`CreateRoomAsync` 改走 `SetCurrentRoom(name, "创建房间")`** —— 不再直接写 `_room`。

### 验收（新增第 36 项闸门 `check_user_flow`，按用户原话顺序跑）
```
房主：改名 → 删「我的房间」→ 新建「开黑房」→ 保活          PASS
加入方：改名 → 扫描到「房间 开黑房」✓ → 一键加入 ✓          PASS
双方成员：2 人（加入方改名、房主改名）✓                     三者一致
```

### 这条根因解释了用户报的多个症状
- **自动搜索加入不可用**：扫到旧房间名 → 进错房间
- **房间名不同步**：左栏/顶栏/媒体引擎/广播各拿着不同时期的房间名
- **成员栏"错乱/看到不存在的人"**：进错房间后看到的是别的房间的成员

### 但**不是所有**症状都出自这条根因（要分清）
| 症状 | 是否同一根因 | 真正的根因 |
|---|---|---|
| 自动搜索加入不可用 | ✅ 是 | 广播房间名不更新（本文件） |
| 房间名不同步 | ✅ 是 | 同上 + 各显示点没走统一设置点 |
| 成员栏错乱 | 🟡 部分 | 还叠加了"同一实例两个身份"（v64） |
| 信令断开 | ❌ 否 | `connectSignal` 不管旧连接（v65） |
| 打不了字 | ❌ 否 | 聊天通道只在第一条链路建 + 输入框靠 `_chatReady`（v64） |

---

## v68：用户新报两条（文件卡片状态 / 左下角名字）+ 补齐"文件传输"真实验证

用户反馈："房间是没问题了就是那几个老bug没解决"，另外新发现：
> 1 传文件传出去一直在发送中但是别人已经接收
> 2 左下角的名字已经没改

### 【真 bug 1】文件卡片永远停在"发送中…"
`AddFileMessage(..., "发送中…")` **只被调用一次**，而 `file-done` 事件只隐藏底部进度条、
**从不更新那张卡片** ⇒ 用户看到"一直发送中"，而对端早已"已接收"。
**修**：`AddFileMessage` 回传状态 `TextBlock`（`_lastSendFileStatus`），
`file-done` 时把文本改成"已发送"。

### 【真 bug 2】左下角名字写死
XAML 里底部那两处（头像字 + 名字）是 **硬编码 `Text="我"`**，改名字后不变。
**修**：加 `x:Name="SelfAvatarText" / "SelfNameText"`，在 `RefreshMemberList` 里用
`SelfDisplayName()` / `FirstChar(...)` 更新。

### 【同类问题·第三次】`--file-test` 的等待时机也是错的
与 `--chat-test` 完全同一个毛病：等待循环跑在 `InitializeMediaAsync` 里，
而加入方在之后才连上 ⇒ 15 秒必然超时（"链路未就绪"）⇒
**文件传输从来没有被真正验证过**（这正是它坏了却一直没被发现的原因）。
**修**：改成**事件驱动**（`chat-open` 时触发 `RunFileTestAsync`）。

修完后文件传输**首次得到真实的端到端验证**：
```
host: 【文件测试】发送 filetest-payload.bin（200000 字节）→ 文件发送完成
join: 文件已保存: ...\received\filetest-payload.bin（200000 字节）
```

### 新增闸门（第 37 项）`check_file_and_selfname`
- 文件传输端到端（发送完成 + 对端已保存）
- 左下角名字跟随改名（UIA 读 `SelfNameText` / `SelfAvatarText`）

### 测试卫生（本轮又踩到两次）
1. **测试前必须清 `app-config.json`**：否则 `--room` 会被上次保存的"当前房间"覆盖，
   两边房间名不一致 ⇒ 永远看不到对方（本次就是这样白跑了两轮）。
2. 判据要写对：我第一次把"卡片变成已发送"写成"日志里出现'已发送'"，
   而那个变化只在 UI 上 —— 判据写错会得到假红。

---

## v69：文件"打开方式"入口（用户："文件传输没问题了但是没有给我打开方式"）

用户反馈整体很好："文件传输没问题了…文字都没问题，成员共享屏幕音频都没问题，
改名房间加入删除什么的都没问题"。

### 缺的入口（按用户规矩"功能必须有界面入口"）
`SaveReceivedFile` 明明已经有**完整保存路径** `path`，但调用
`AddFileMessage(savedName, bytes, isSelf: false, "已接收")` 时**没把路径传进去**
⇒ 用户收到文件后**没有任何办法打开它**。
**修**：`AddFileMessage(..., savedPath)` 在卡片右侧给出两个按钮
「打开文件」（交给系统按扩展名选默认程序）与「打开所在文件夹」（`explorer /select,` 选中该文件）。

### 验收（新增第 38 项闸门 `check_file_open_entry`）
UIA 实测收到文件后界面上能找到：
```
打开文件,打开所在文件夹
```

### 关于"本机不能共享屏幕"（用户说暂时不管，但我先定位）
截图提示"捕获被占用，已清理，可重试" = `NotReadableError`。
**可疑根因（待验证）**：WebView2 的用户数据目录是**全实例共用**的：
```csharp
var udf = Path.Combine(AppContext.BaseDirectory, ".webview2");
```
同机多实例共用同一个 Chromium profile ⇒ 屏幕采集上下文互相冲突。
若验证成立，修法是让 UDF 按实例区分（例如带端口号）。

---

## v71：语音 —— **我的判据一直是错的**（用共享声音的字节冒充语音证据）

用户："语音功能还不能用呢"。

### 我犯的错（第三次同类）
我说"语音 PASS（双向 26 万字节）"时，用的是 `check_chat_end_to_end` 里的
`audioOutBytes` —— 而那个测试带着 **`--share-audio-test`**，
所以那些字节是**共享电脑声音**，**不是麦克风语音**。真正的语音测试（`--call-test`）
一直跑在错误时机（`InitializeMediaAsync` 里、加入方连接之前）⇒ 每次都是
"链路未就绪，放弃" ⇒ **语音从来没有被真正验证过**。
（同一模式这已是第三次：`--chat-test`、`--file-test`、`--call-test` 都跑在错误时机。）

### 修
`--call-test` 改为**事件驱动**（`chat-open` 时调 `RunCallTestAsync` → `OnCallClickedAsync`，
与界面按钮同一条路径）。

### 实测结论（首次真正的语音验证）
```
host: 发起通话=True 麦克风已开=True 有麦克风轨=True 发送=66422 接收=68668 字节
join: 发起通话=True 麦克风已开=True 有麦克风轨=True 发送=69138 接收=66912 字节
```
判据三层：① 双方真的发起通话 ② `senderInfo` 里存在 **isShared=false** 的音频 sender
（即麦克风轨真的挂上了）③ 音频字节 > 0。
**结论：语音链路上是通的**（数据双向在传、麦克风轨挂了、`play()` 也没报"播放被拦"）。

### 所以"听不到"最可能在**扬声器/音量**这一环
按用户规矩（功能必须有界面入口）加了诊断入口：
设置面板 →「**测试扬声器（播 1 秒提示音）**」，播放 1 秒 440Hz 到**当前选定的扬声器**。
- 能听到 ⇒ 设备没问题，"听不到对方"要往别处查
- 听不到 ⇒ 问题就在扬声器选择/音量

### 必须说清的边界
自动化能证明的是"**数据在传、轨道挂了、播放没报错**"；
"**人耳能不能听到**"我无法自动证明 —— 那需要用户点一下测试音。

### 顺带修了契约守卫的一个盲区
`window.zxEngine = { testSpeaker, ... }` 这种**简写属性**不被原正则
（只认 `名字(`）识别 ⇒ 误报"页面没有这个方法"。
已在守卫里补上简写属性的匹配（在 zxEngine 对象体里，`名字,` 就是挂同名函数，安全）。

---

## v72：**"发起通话"按钮点不了** —— 这才是"语音功能不能用"的真正根因

用户截图：状态已经是 **`通话中`**，而 **`发起通话` 按钮是灰的（点不了）**，原话"根本就点不了通话"。

### 根因（`call.js` 的 `reportCallState`）
```js
const audioSenders = primaryPc().getSenders().filter(s => s.track.kind === 'audio');
const sending = audioSenders.some(s => s.track.enabled && !s.track.muted);
active: sending || receiving          // ← 只看"有没有音频发送轨"
```
**A1 的「共享电脑声音」在链路建立时就会常驻挂一条音频轨**（活跃合成轨，
`enabled=true` / `muted=false`）⇒ `sending` 恒为 true ⇒ `active` 恒为 true ⇒
**一进房间界面就显示"通话中"** ⇒ `SetCallState(active:true)` 禁用「发起通话」
⇒ **用户根本点不了通话，麦克风从来没被打开过**。

### 修
1. **判据排除共享轨**：`micSenders = allAudioSenders.filter(s => s.track !== state.sharedAudio.track)`；
2. **通话中改由明确状态决定**：`active: state.inCall || sending`
   （`state.inCall` = 用户点了「发起通话」或收到对方 `purpose=call`）。
   绝不能用"有音频轨在传"当判据。

### 验收（新增第 40 项闸门 `check_call_button_clickable`）
进房间但**不**发起通话：
```
CallButton: enabled=True
[通话判定] 链路=connected 发音频=False 收音频=True → 通话中=False
```
而真实通话仍然通过（`check_voice_call`）：双向 6.8–7.4 万字节。

### 顺带修掉一个真 bug：「一键加入」加错房间
`OnJoinScannedAsync` 先 `RoomListCombo.SelectedItem = _allRooms[0]`、日志也打
`_allRooms[0].Display`，但真正加入的 `OnJoinSelectedRoomAsync()` 读的是
**`RoomListCombo.SelectedItem`** —— 而扫描列表**每秒刷新**会重置选中项
⇒ **日志显示 A、实际加入 B**（实测：日志"开黑房"、结果房间=dggdf）。
**修**：把目标房间**显式传参**下去，不再依赖 UI 选中状态。

### 测试卫生（真机环境的干扰）
用户自己的实例（`LAPTOP-95919P08:45890`，房间 `dggdf`）一直在广播，
它会排进我的扫描列表。**"一键加入"取第一个是正确产品语义**，
但测试不该假设"第一个就是我建的房间" ⇒ `--user-flow-join` 现在接受**期望房间名**，
按名筛选后再加入（与用户手点列表项效果一致），测试由此变确定。

---

## v73：一端卡在"建立连接…"、且挂断/静音点不动

用户反馈："能用了但是一段会一直显示建立连接而且无法挂断和静音，另一端无法挂断"。

### 根因（两个，互相放大）
1. **通话状态只由页面 `call-link` 事件驱动**，而页面的 `setInCall()` **只写日志、不上报**：
   ```js
   function setInCall(value, reason) {
     if (state.inCall === value) return;
     state.inCall = value;
     log(...);        // ← 没有任何 post / reportCallState
   }
   ```
   于是只要**任何一条路径**漏了 `reportCallState()`，界面就永远停在旧文案
   （"建立连接…"）—— 状态位与真实情况脱节。
2. **挂断/静音按钮完全依赖 `_inCall`**：
   ```csharp
   MuteButton.IsEnabled = _inCall;
   HangupButton.IsEnabled = _inCall;
   ```
   ⇒ 一旦上面的判定出偏差，用户**既挂不断也静不了**（实测反馈）。

### 修
1. **按钮改成"有对端即可用"**（永远不会错的下限）：
   `MuteButton/HangupButton.IsEnabled = ready && hasPeer`。
   没有对端时仍是灰的，符合直觉。
2. **`active` 增加兜底**：`state.peerCallRequested && receiving`
   —— "对方明确发起过通话（信令里的 `purpose=call`）**且**本端确实收到音频"也算通话中。
   不能直接用 `receiving`：A1 的共享电脑声音也会让它为真，那正是 v72 修的原始误判。
3. `[通话判定]` 日志补上判定依据（`页面inCall=` / `共享轨在发=`），以后再出这类问题一眼可定位。

### 验收（新增第 41 项闸门 `check_one_side_call`）
只有一端点「发起通话」时：
```
被动方：CallButton=禁用 HangupButton=enabled MuteButton=enabled
        最后判定 通话中=True
```
**同时发现一个关键数字**：修好后双向通话字节从 **6.8 万 → 42 万**（6 倍）。
说明此前那 6.8 万字节的"通话"其实一直是**半通**状态 —— 正好对应用户看到
"一端建立连接…"的那一侧（麦克风实际没真正参与）。

---

## v74：挂断后两端状态不一致 + 按钮改名「加入语音」

用户反馈："发起通话后确实有效果但是无法挂断……点了挂断没声音了但是状态一个还是通话中，
一个是已连接未通话，修了 bug 后挂断和发起通话合 2 为一，还有不应该叫发起通话应该叫加入语音"。

### 根因（两层，第二层才是致命的）
1. **`hangup()` 从不通知对端** —— 只做本端清理，对端不知道 ⇒ 一直显示"通话中"。
2. **`hangup()` 中途抛 ReferenceError**（真正让状态卡死的原因）：
   ```js
   // mesh.js 的 hangup 里
   if (remoteAudioEl) { remoteAudioEl.srcObject = null; }   // ← remoteAudioEl 是 call.js 的模块级变量
   ```
   实测日志：
   ```
   [链路] fe443b45 已释放（挂断）
   [JS 未处理拒绝] remoteAudioEl is not defined   ×4
   ```
   ⇒ hangup 在这里**中断** ⇒ 后面的 `io.setInCall(false)` / `reportCallState('已挂断')`
   **全都没执行** ⇒ 状态停在"通话中"。

### 修
1. `hangup()` **先通知所有对端**（`sendSignal({type:'hangup'})`），再清理本端；
2. `signaling.js` 处理收到的 `hangup` → `io.hangup('对方已挂断')`；
3. `mesh.js` 不再裸引用别的模块的变量，改用注入的 `io.detachRemoteAudio()`；
4. 按钮文案 **「发起通话」→「加入语音」**（用户要求：语义上这就是加入/退出语音）。

### 这是冲突模块裸引用的第三次
| 次数 | 位置 | 后果 |
|---|---|---|
| ① | webrtc.js 引用 call.js 的 `remoteVideoEl` | 每次对方共享都中断 ontrack ⇒ **画面接不上** |
| ② | webrtc.js 引用 call.js 的 `redactIceUrl` | ICE 诊断失败 |
| ③ | **mesh.js 引用 call.js 的 `remoteAudioEl`** | **hangup 中断 ⇒ 状态卡"通话中"** |

**我一开始想用静态扫描拦住它，但误报太多**（各模块都从 `io` 解构依赖
`const { post, log, state } = io;`，静态分析分不清"解构来的局部变量"和"裸引用"）。
**改用运行时判据**（新增第 43 项闸门）：完整流程跑一遍，
断言日志里没有 `[JS 未处理拒绝]` —— 这正是这三类 bug 的真实症状，可靠且零误报。

### 验收（新增第 42 项闸门 `check_hangup_sync`）
```
CallButton.Name=加入语音
挂断后 host: 链路=none 通话中=False
挂断后 join: 链路=none 通话中=False
两端都收到"对方已挂断"
```

---

## v75：挂断后再「加入语音」变成"仅收听" + 按用户建议合并按钮

用户反馈（v74 实测）：
> 1 挂断一次再加入会变成仅收听
> 2 一边挂断另外一边也挂断
> 3 我这边房主麦克风没声音只有成员有但是我不知道是不是我自己的原因
> 建议取消挂断键让加入语音被点击后自动变成挂断键

### 【真 bug】第 1、3 条是同一个根因：`outgoingTrack()` 返回了**已死的轨道**
```js
function outgoingTrack() {
  if (localAudioChain?.destination) {
    const t = localAudioChain.destination.stream.getAudioTracks()[0];
    if (t) return t;          // ← 不检查 readyState
  }
  ...
}
```
挂断会 `localStream.getTracks().forEach(t => t.stop())`（轨道变成 `ended`），
**但降噪链 `localAudioChain` 没被清理**，里面还留着那条死轨。
再次「加入语音」时 `addTrack(死轨)` ⇒ **媒体根本发不出去** ⇒ 界面"仅收听"、
对方听不到本端（正是用户说的"房主麦克风没声音"）。

**修**：
1. `outgoingTrack()` 只认 `readyState === 'live'` 的轨；
2. 新增 `resetAudioChain()`（丢弃旧链）；
3. `callPeer` 拿不到活轨时：重置链 → 重开麦克风 → 重新取轨。

### 第 2 条不是 bug，是 v74 的**正确修复**
"一边挂断另外一边也挂断" —— 这正是 v74 加的 `hangup` 信令通知对端（挂断本就该双向）。

### 按钮合并（按用户建议）
「加入语音」与「挂断」合并为**同一个键**：未通话显示`加入语音`、通话中显示`挂断`，
独立的挂断键隐藏（用户原话："取消挂断键让加入语音被点击后自动变成挂断键"）。

### 验收（新增第 44 项闸门 `check_rejoin_call`）
```
通话中：  CallButton.Name=挂断     enabled=True
点一次 → 挂断
挂断后：  CallButton.Name=加入语音 enabled=True
再点一次 → 再加入
最后判定：发音频=True 收音频=True 通话中=True     ← 不再是"仅收听"
```

### 顺带修了两处**测试脚本过时**（隐藏挂断键导致）
- `hangup-sync-check.py`：改点合并键 `CallButton`；
- `build/smoke-ui.py`：断言从 `HangupButton` 改为 `CallButton`。
（`smoke-ui.py` 那条一度被误判成产品问题，实际是"界面自检引用的控件名"过时 ——
  项目里已有 `check_smoke_ui_control_names` 守卫专门防这类；这次是我改了 XAML 没同步脚本。）

---

## v76：静音修复（成功）+ "不加入也能听" 的一次失败尝试（已回退，留档）

用户反馈（v75）：
> 能用了，但是不加入也能听到语音不能说话相当于静音，
> 双方点静音后在恢复只有房主有声音成员听不到

### ✅ 第 2 条：静音恢复后成员说不了话 —— 真 bug，已修
```js
setMuted(arg) {
  if (state.localStream) {
    state.localStream.getAudioTracks().forEach(t => { t.enabled = !state.muted; });
  }        // ← 只改 localStream 的轨，但实际 addTrack 的是「降噪链」的输出轨
}
```
开了降噪链时，**实际送出去的轨道**是 `localAudioChain.destination.stream` 的轨，
而这里只改 `state.localStream` 的轨 —— 两者不是同一条 ⇒ 静音/取消静音对真实发送
**没作用**（用户看到的"恢复不过来"）。而且改完**不重新上报**，界面也不刷新。
**修**：改**实际发送轨**（`outgoingTrack()`）+ 改完 `reportCallState()`。
**验收**（新增第 45 项闸门 `check_mute_cycle`，判据收紧）：
```
通话中 发音频=True → 静音后 发音频=False → 取消静音后 发音频=True
```

### ❌ 第 1 条：不加入也能听到对方 —— **尝试修复但失败，已回退**
我加了"只有本端已加入语音（`state.inCall`）才 `attachRemoteAudio`"。
结果 **误伤 A1 共享电脑声音**（守卫立刻抓到）：
```
[host] destRms=0.343 发送=130698 接收=125299 能量=0.00
字节都在，但一个都没播。
```
**原因**：A1 的共享电脑声音轨与对端麦克风轨，到了本端**都是普通的 audio receiver，
走同一条 `ontrack` 音频通路，无法区分**。要真正做"不加入就完全听不到"，
必须先让 **A1 共享声音走独立音轨**（类似 `attachScreenAudio` 那样独立的 audio 元素）——
那是更大的改动，**已回退并留档，等用户决定要不要做**。

### 顺带修：GUI 自检的真鼠标点击失准
`[FAIL] 点了齿轮之后设置面板没出现` —— 实测点击坐标是**过期**的 `(43,1056)`，
而正确位置是 `(963,899)`（用户操作电脑时窗口被反复挪动）。
**修**：点击前 `clamp_window_on_screen` + **重新 locate 取实时坐标**；且点击后若面板未出现
**先检查前台是否被抢** ⇒ 是则 `abort`（环境中止，退出码 2），不再伪装成产品 FAIL。
