# 媒体引擎架构（WebView2）与 W1 验证记录

> ⚠️ **架构已被取代（2026-10-05）：视频主路径不再走 WebView2 + `getDisplayMedia`**，
> 改为 **C++ 助手进程 + 自研管线 + 无边框覆盖窗口**（见 [`route-decision-2026-10-05.md`](route-decision-2026-10-05.md)）。
> **仍然有效**的部分：WebView2 里跑语音（降噪/回声消除/自动增益三项实测生效）、文字聊天、文件传输。
> 视频相关的描述请只当历史看。

日期：2026-10-04
状态：**已验证通过** —— WebView2 作为媒体引擎，降噪/回声消除/自动增益三项全部生效
配套：`../src/winui/BUILD-NOTES.md`（WinUI 构建链）、`ui-design-guideline-2026-10-04.md`（界面规范）

---

## 1. 决策：媒体放在 WebView2 里，界面用原生 WinUI

![自检通过](../../build/winui-selfcheck.png)

### 1.1 为什么必须这样

Windows 上能拿到实时音视频只有三条路：

| 路线 | AEC3 回声消除 | NS 降噪 | NetEQ 抖动缓冲 | 屏幕采集 | 本机可行 |
|---|---|---|---|---|---|
| C++ + libwebrtc | ✅ | ✅ | ✅ | ✅ | ❌ 需 VS 专有组件（装不上，见 BUILD-NOTES） |
| C# 纯托管（SIPSorcery 等） | ❌ | ❌ | ❌ | 要自己写 | ⚠️ 能跑，但丢掉了 WebRTC 唯一值钱的部分 |
| **WebView2（Chromium）** | ✅ | ✅ | ✅ | ✅ `getDisplayMedia` | ✅ **Runtime 已在（154.0.4258.53）** |

**选 WebView2**。它不是"图省事的折中"，而是唯一能在本机拿到工业级音频处理的方案。
`SpawnDev.RTC` 之类的 .NET 封装最终仍指向浏览器 WebRTC 或已废弃的
`MixedReality.WebRTC`，并没有绕开这个结论。

### 1.2 关键设计：WebView2 是「媒体服务」，不是界面

```
┌─ WinUI 3 窗口 ─────────────────────────────────────────┐
│  原生控件：导航 / 消息列表 / 成员 / 音量条 / 授权横幅     │
│  （全部符合 Fluent，跟随系统明暗主题）                    │
│                                                        │
│  ┌─ WebView2（0×0 尺寸、透明、不接收点击）──────────┐   │
│  │  getUserMedia / getDisplayMedia                  │   │
│  │  RTCPeerConnection（Opus / NetEQ / AEC3）         │   │
│  │  RTCDataChannel（文字、远端控制输入）              │   │
│  └──────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────┘
                     ↕ 仅一条 JSON 消息通道
```

**音频样本一个都不进 C#**：解码与播放全在 Chromium 内部完成。
C# 只收发控制指令与状态。这样既没有跨语言开销，也不会在音频线程上踩托管 GC。

### 1.3 用虚拟主机映射，而不是 file:// 或本地服务器

`getUserMedia` 要求**安全上下文**。`file://` 不算，Chromium 会拒绝，
而且**失败方式是静默的**（`mediaDevices` 存在但调用报 `NotAllowedError`）。

```csharp
core.SetVirtualHostNameToFolderMapping(
    "zxmedia.local", _mediaRoot, CoreWebView2HostResourceAccessKind.Allow);
core.Navigate("https://zxmedia.local/selfcheck.html");
```

好处：满足安全上下文 + 直接读磁盘文件（改完刷新即生效）+ **不需要起 HTTP 服务**。

---

## 2. 验证结果

自检页 `src/winui-cs/media/selfcheck.html` 在应用内运行，实测输出：

```
✓ 麦克风可打开      — 微克风 (MOONDROP Gaming Maestro) (35d8:012d)
✓ 回声消除生效      — true
✓ 降噪生效          — true
✓ 自动增益生效      — true
✗ 麦克风有信号      — -96.4 dBFS
```

**结论：降噪与回声消除均已生效，可以直接用于通话。**

最后一项显示 -96.4 dBFS 是**预期的**：采样那 2 秒钟没有对着麦克风说话。
这条检测的意义在于区分"拿到了一条轨道"和"轨道真的有声音" ——
WebView2 有可能返回一条永远静音的轨道，光看前四项分不出来。
接真实通话时若发现对方听不到声音，这一项是第一个要看的。

### 2.1 为什么必须读 `getSettings()` 的实际值

请求 `noiseSuppression: true` **不等于**它生效。浏览器可能：
- 忽略该约束（返回的轨道里 `noiseSuppression` 为 `undefined`）
- 报告 `false`
- 接受但底层设备不支持

所以自检页打印的是**请求值 vs 实际值**的对照，而不是"我请求了什么"。

---

## 3. 踩过的坑

### 3.1 WebView2 的 user data folder 不要放 `%LOCALAPPDATA%`

最初把 UDF 设成
`%LOCALAPPDATA%\ZongxianVoice\WebView2`，初始化直接失败：

```
UnauthorizedAccessException: Access to the path
'C:\Users\Administrator\AppData\Local\ZongxianVoice\WebView2' is denied.
```

但**手工创建同一个路径却成功** —— 说明与 WebView2 内部创建 UDF 的时序
或子进程的继承权限有关，不是简单的目录权限问题。

改为 `AppContext.BaseDirectory\.webview2`（exe 同目录）后正常。

顺带的收益：便携性更好，整个文件夹拷走就能用，不留系统残留。

### 3.2 `WebView2` 不实现 `IDisposable`

它是 XAML 控件，生命周期由视觉树管理。硬套 Dispose 模式会编译失败：

```
error CS1061: "WebView2"未包含"Dispose"的定义
```

正确做法是只退订事件、停掉正在跑的媒体轨道（`Shutdown()`），
把控件本身的释放交给视觉树。

### 3.3 错误信息要带 `InnerException` 与路径

WebView2 的初始化失败原因常藏在 `InnerException` 里，
只留 `ex.Message` 会丢失关键信息。现在的错误串包含：

```
类型: 消息 | Inner: 类型: 消息 | HRESULT=0x... | 媒体目录=...
```

`3.1` 那个问题就是靠这条完整信息才定位的（能立刻看出是 UDF 路径的问题）。

---

## 4. 目录与文件

```
src/winui-cs/
  media/
    selfcheck.html          媒体引擎自检页（已验证）
    （后续：rtc.js / audio.js / screen.js / control.js 将放这里）
  src/
    MediaEngine.cs          WebView2 封装：虚拟主机映射、权限、消息桥
    MainWindow.xaml(.cs)    三栏界面 + 自检面板
    Program.cs → 由 XAML 编译器生成，不要手写（见 BUILD-NOTES）
  ZongxianVoice.csproj      含 Microsoft.Web.WebView2 引用
build/
  build-winui-cs.cmd        构建 + 部署 media 资源 + 生成 resources.pri
  capture-window.py         窗口截图（含前台锁绕过）
```

---

## 5. W1 剩余工作

| 项 | 说明 |
|---|---|
| 信令 | 复用 `src/swiftdrop/webhost.py` 的 `/signal` WebSocket 中继；Python 后端由 C# 拉起或独立进程 |
| SDP / ICE 交换 | C# 通过消息桥拿 SDP，经信令通道转发给对端 |
| 静音 / 音量 | C# 调 `zxEngine.setMuted()` / 轨道音量；界面已有控件位 |
| 通话界面 | 把自检面板替换成通话区（头像 + 说话高亮），沿用 ui-design-guideline §3.2 |
| 连接质量 | 从 `RTCPeerConnection.getStats()` 取 RTT/丢包/抖动，显示在顶栏 |
| 降噪档位 | 浏览器只给开关（`noiseSuppression: true/false`），所以"轻/中/强"需要在应用层用 Web Audio 的 `DynamicsCompressorNode` + 门限自行实现，或明确只提供"开/关"两档 |
