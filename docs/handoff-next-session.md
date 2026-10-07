# 接手须知（2026-10-06 · S0–S4 与 A1/A2/A3/A4 落地后重写）

> 读这一份就能开工。细节：`docs\audit-2026-10-06.md`（审计）、
> `docs\architecture-modules-2026-10-06.md`（架构规范）、
> `docs\direction-decisions-2026-10-06.md`（**方向决定 + 每轮事故与教训**，最重要的变化史）。
> 本文所有数字都是**实测**（采集脚本：`tmp\collect-facts.py`）。

## 0. 三十秒版

**项目**：棕仙语音（Windows 桌面语音 / 屏幕共享 / 文件传输）。三条腿：
C# WinUI 3 应用（`src\winui-cs`）+ WebView2 承载的 WebRTC 页面（`src\winui-cs\media\`）
+ C++ 音频引擎（`src\media`，由应用以子进程 `zxprobe.exe` 调用）。
同仓库还有 A 线产品「棕仙的传输软件」（Python，独立，**别混着改**）。

**现状**：功能可用且**有可交付的包**（v55）；结构与契约都已落地；
**A 系列功能缺陷已修 A1/A2，A3 已不复现、A4 已做局域网列表与邀请串**。

## 1. 现在的状态（实测）

| 维度 | 状态 |
|---|---|
| 功能 | ✅ 联机 / 语音 / 文字 / 文件 / **共享电脑声音**（A1 已修）/ 三实例成员列表（A3 已验）<br>✅ 屏幕共享（A2 已修：`webrtc.js` 跨模块引用导致 ontrack 中断）<br>✅ 局域网房间列表（可筛选 + 回车加入）+ 邀请串 `房间名@地址` |
| 结构 | ✅ `MainWindow.xaml.cs` **4205 行**（C# 侧已拆出 ProbeLocator / EngineProbe / NetworkProbe / FileStore / SharedAudioService / ScreenShareService）<br>✅ 页面拆成 **15 个 JS 模块**，`call.js` 从 3190 → **1685 行**；`call.html` **25 行**（只剩壳）<br>⬜ `wasapi_backend.cpp` **1139 行**（引擎侧仍待拆，见 S5） |
| 契约 | ✅ `BridgeContract.cs` 是唯一登记处 + 启动对账（页面事件 84 种 / 方法 23 个） |
| 测试 | ✅ **release 模式 45 项**（3 构建 + 界面自检前置 `--no-click` + 界面自检（真键鼠）+ **19 条守卫**）<br>✅ 多实例：双实例联机、**共享声音端到端**、**屏幕共享端到端**、三实例成员数（`build\three-instance-members.py`）<br>✅ 环境不满足 → **环境中止**（退出码 2/3），不伪装成产品 FAIL |
| 交付 | ✅ **v55**（`dist\棕仙语音-测试版-v55.zip`，328 条目，sha256 `a82d3ad7ca573aed…`） |
| 文档 | ✅ 本文件 + `direction-decisions`（含 S3/A1/A2/A4 的完整事故记录）已对齐现状 |
| 开源 | ⬜ 未做：LICENSE、排除私有目录、清仓库里的 ffmpeg 二进制 |

**git**：写本文时 HEAD `452f456`、**142 提交**、**403 跟踪文件**、工作区干净。
包版本链：`… → v53(A1) → v54(A3/A4) → v55(A2)`。

## 2. 怎么构建 / 自检 / 发版

```bat
:: 1) 引擎（产出 dist\zxprobe.exe）
build\build-media.cmd

:: 2) 应用（产出 dist\winui，含 resources.pri 与媒体资源）
build\build-winui-cs.cmd

:: 3) 自检（release 严格模式：SKIP 一律按 FAIL；共 45 项）
python build\run-checks.py                  :: 也支持 build / dist / selftest / release
python build\run-checks.py release
::   3 构建 + 界面自检前置(--no-click) + 界面自检(真键鼠) + 39 条守卫：
::     pri / dist 卫生 / media·探针一致性 / 脚本名绑定 / 残留规则 / 界面控件名
::     / RELEASES 表格结构 / cmd 行尾 / 前端资源行尾 / 桥接契约 / 页面模块可加载
::     / 单例别名 / 路由前缀冲突 / 模块形状 / 模块统一释放 / 共享声音端到端
::     / 邀请串解析 / 屏幕共享端到端 / 双实例
::   环境中止（用户在用电脑 / 弹窗 / 游戏在前台）→ 退出码 3，**不是**产品 FAIL

:: 4) 发版（唯一打包路径）
python build\release.py --version 56
```

- ⚠️ **不允许手工打包**：`make-voice-package.py` 只认 `ZX_RELEASE_ENTRY=1`。
- ⚠️ **不许 `git add -A`**：`build\pyi-work`、`build\snapshots` 等曾被误提交（已修 `.gitignore`）。
- ⚠️ **`.gitignore` 必须 LF**：CRLF 会让每行尾部带 `\r` 而**整条规则失效**（本会话真发生过）。

## 3. 三条已被反复验证的工程规矩（比代码更值钱）

1. **`log()` 不算报错**：页面 `log()` 只写隐藏 DOM。任何"用户/自检需要知道"的失败必须 `post()`
   —— 反过来说，**应用日志里没有的失败 = 不存在**。
2. **搬运依赖必须整块搬**：把函数搬出模块时，它依赖的私有函数/变量要么一起搬、要么改成注入。
   本会话因此抓到三次"零日志"事故（`buildRtcConfig` / `createLink` / `remoteVideoEl`）。
3. **判据必须先自证**：新写的守卫要能用反例证明"会红"（本轮多个守卫自己先假失败过：
   注释被当代码、正则认错位置、判据写反）。**判据写错会得到假红/假绿，比没有判据更坏**。

## 4. 尚未完成

| 项 | 说明 | 风险 |
|---|---|---|
| **S5 拆引擎** | `wasapi_backend.cpp` 1139 行 / 7 类职责；线程与生命周期契约要写进头文件 | **高**（C++ 实时音频路径） |
| **S6 文档固化** | 本文件已重写；`docs\` 其余 17 处"闸门 45 项"等旧数字待清 | 低 |
| **A5 远端控制** | 现状：只有 `NativeVideoHost` 传 `--remote-control`，**应用侧无实现** | 范围未定，需先定方向 |
| **D 系列** | 跨网段 TURN 真机、引擎并发压测、音质主观判定、点击穿透 | 需要真机/第二台机器 |
| **开源收尾** | LICENSE、清 ffmpeg 二进制、排除私有目录 | 低 |

## 5. 环境与坑（本轮新增）

- **前台冲突是环境问题，不是产品问题**：用户打游戏时前台被抢，自检按设计**带原因中止**
  （退出码 2/3）。不要去"绕过"它 —— 绕过就等于让不可信的点击继续。
- **窗口选择必须按尺寸过滤**：WinUI 有一个 `160x28`、位于 `(-32000,-32000)` 的辅助窗口，
  标题同样带应用名；只按 pid+标题会选中它，产生"窗口没归位"的**假失败**。
- **`/signal` 路由前缀**：本地 HTTP 服务的信令端点是 `/signal`，页面文件名**不能以它开头**
  （`signaling.js` 曾因此被 404 截胡，整页不启动）。已有守卫 `check_media_route_collisions`。
- **坐标空间**：`GetWindowRect`/UIA 与应用 `AppWindow` 的坐标偶有不一致；自检已加
  "窗口位置归一化 + 稳定后重测"，并把 `BringWindowOnScreen` 的移动过程写进日志。
