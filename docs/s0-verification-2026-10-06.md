# S0 独立复核与验证报告（2026-10-06）

> 复核人：**verifier**（独立于 A/B/C/D 四位改动者；本报告只读 + 跑命令 + 造临时反例并还原）
> 复核对象：S0 四个任务卡（task-1 C# / task-2 页面 / task-3 引擎 / task-4 文档）+ 两条新机制
> 写域：本文件（`docs\s0-verification-2026-10-06.md`）—— 复核期间**未修改任何源码/脚本**
> 取证时间：**第一轮 06:20 – 07:05 / 第二轮 07:09 – 07:14**（+08:00）。**每节标了取证时点的脚本哈希；行号以该时点为准。**
> **第二轮结论已更新（v37 已出包 14/14）：见 §0 顶部与 §10。**

---

## 0. 结论（不可回避，先给答案）

### **第二轮（2026-10-06 07:14 复核后）：可以出 v37。**

| 判定 | 依据（全部可复核，详见 §10） |
|---|---|
| **发版闸门全绿：`汇总: PASS 14 / FAIL 0`（两次）** | 第一次 `tmp\release-v37d.log`（255 行，21,666 B，07:09:21）；**最终** `tmp\release-v37e.log`（256 行，07:11，25 项全 PASS）→ `=== [5/7] 打包 v37 ===` → `✓ 313 个条目全部与 dist 一致，关键文件齐` → `已登记：棕仙语音-测试版-v37.zip（写进「发布物」表；sha256 与条目数都在里头）` → `=== 发版完成：dist\棕仙语音-测试版-v37.zip（313 条目，已与 dist 逐字节校验）===` |
| **最终产物经我独立逐字节校验** | `dist\棕仙语音-测试版-v37.zip`（34,207,545 B / **313** 条目 / mtime 07:11:09）；我自算 sha256 = **`a167ff47b71041dd34605310f613c105accb0754aeb7b5ac14236ba131bf8ae0`**（与 Lead 附注值、与 `dist\RELEASES.md:29` 登记的前缀 `a167ff47b71041dd` 一致）；`media/call.html` 与源码**逐字节相同**（142,147 B）、包内 `tools/zxprobe.exe` == `dist\zxprobe.exe`、`resources.pri` == `dist\winui\resources.pri`；**无 `.webview2`、无任何 `.log`**、无 `received/ recordings/ sent/ incoming/`。**这是第二次发版的包**——第一次（07:09）的 `fd1befa4…2319` 已被取代，原因见 §10.5 |
| **三条未闭合项全部关闭** | ① 界面自检那一红 = **真产品布局 bug**，已修（XAML 改两行 Grid；开关矩形由 `(745,864,112,10)` 变 `(745,726,112,63)`；真点后 `已开始采集` + `泵状态：块=18`）② C# 永久 2 秒轮询已加 `CancellationToken` + 条件轮询（**闲置 14 秒 0 条** `share-audio-debug`）③ 设备枚举改走引擎（`[设置] 设备（引擎枚举）：麦克风 3 个、扬声器 6 个`）—— 每条都有我的**独立取证**（§10.2） |
| 我第一轮报的 3 处文档不符 | **已全部修好并复核正确**（`README.md:120-123`、`dist-policy:56-61`、`dist-policy:64-66`，见 §7.2） |

**唯一保留意见**：D 段「线程/生命周期（无释放后使用）」我**仍未独立证明**（只核对代码意图，未做并发压测）—— 属"未验证"而非"有问题"，不构成 v37 阻塞。

**关于闸门证据的来源**：我**没有**亲自跑 `run-checks.py release`（它发真实键鼠，Lead 统一执行，我按要求不与发版抢鼠标）。闸门日志来自 **Lead 执行的 `build\release.py`**；但我对**产物做了独立校验**（sha256 / 条目数 / 逐字节比对 / 清单交叉核对），并**独立复核了三条修复的代码与运行日志** —— 是"别人的命令输出 + 我自己的验证"，不是照抄结论。

---

### 【以下为第一轮结论（07:05），保留作历史；已被上面的第二轮结论取代】

### 第一轮判定：不能出 v37。

| 判定 | 依据 |
|---|---|
| **发版闸门未全绿：`PASS 13 / FAIL 1`** | `build\release.py` 的 `step_guards()` 在 [4/7] 跑 `run-checks.py release`，唯一红项 = **界面自检（模拟键鼠）**；随后 `✗ 守卫未通过（退出码 1）—— 不产出 zip`。`dist\` 里**没有** `棕仙语音-测试版-v37.zip`。证据：`tmp\release-v37c.log`（235 行，20,614 B，07:00:58）。 |
| **S0 自己的验收标准就没满足** | `docs\architecture-modules-2026-10-06.md:141` 写的 S0 完成判据是「`smoke-ui` + `run-checks` **全绿**」。现在 `run-checks release` = 13/1。 |
| 另有 **2 条 S0 内应清的债未闭合** | ① C# 侧**永久 2 秒轮询**（审计 §五-4）`MainWindow.xaml.cs:2501-2523`；② 设置里**设备列表 0 个**（审计真 bug #1 的**症状**仍在）`MainWindow.xaml.cs:3334` + `call.html:2456-2476`。 |
| 另有 **3 处文档数字/行号仍与代码不符** | `README.md:120-121`、`docs\dist-policy-2026-10-05.md:56-59,63-64`（见 §5）。 |

**放行条件（任意一条满足即可翻盘，但都需要一次新的全量闸门证据）**：
1. 界面自检那一步绿；**并且**
2. 上面 2 条未闭合项要么修掉、要么由你明确降级为「已知不阻塞」并写进文档；**并且**
3. 再跑一次 `python build\release.py --version 37`，把 `汇总: PASS 14 / FAIL 0` 与产出 zip 的 sha256 留档。

**关于唯一致命项的一句话判断**：那一步的失败**更像"测试点击没命中控件"，不是产品坏了**（证据链见 §6.1）——但**这一点尚未被钉死**，所以按闸门原文，它仍然是红的。

---

## 1. 复核环境与方法（可复现）

| 项 | 值 |
|---|---|
| 工作目录 | `D:\文档\ai001` |
| git HEAD | `63213cb`（`D:\BuildTools\MinGit\cmd\git.exe rev-parse --short HEAD`）；`git status --porcelain` = **44 个改动文件**（S0 未提交） |
| Python | `C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe` |
| Node | `C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe` = **v24.21.0** |
| dotnet | `C:\Program Files\dotnet\dotnet.exe` |
| 控制台 | GBK（Python 脚本自带 `stdout.reconfigure(utf-8)`；本报告内引用输出均为原样） |
| 单个守卫的运行方式 | 临时 runner `tmp\zz-run-guard.py`：`importlib.import_module("run-checks")` + 调同名函数 + 打印 `== RESULT <name>: PASS/FAIL`；跑完**已删除** |

**取证时点的被验脚本哈希**（复核结束时再次核对，见 §7.5）：

| 文件 | SHA-256（前 16） | 大小 | mtime |
|---|---|---|---|
| `build\run-checks.py` | `12E163F0011E55DC` | 36,463 | 06:17:18 |
| `build\release.py` | `65972D6BE826A413` | 12,198 | 06:14:36 |
| `build\smoke-ui.py` | `EEC4270B3D3D31FA` | 33,369 | 06:47:48（**期间被 lead 改过，1C 已按新版重验**） |
| `README.md` | `E1B0BD7885C77FD6` | 11,504 | 06:23:19 |
| `docs\share-audio-status-2026-10-06.md` | `2CFC7A236E572882` | 5,542 | 06:23:25 |

**为什么不自己跑 `run-checks.py release`**：它会启动真实界面并**发真键鼠**（`smoke-ui.py` 的 `gui.move/glide/click`）。Lead 明确要求由他统一跑（避免与用户抢鼠标、并与发版流程互锁）。全文的闸门证据因此来自 **Lead 执行的 `build\release.py`**，我只读它的日志并独立分析失败原因；**我没有引用任何人的结论当证据**。

---

## 2. A 段 · 反例测试：守卫"真的会 FAIL"（造完即删）

**基线（未造任何反例时）**

```
$ python tmp\zz-run-guard.py check_script_sanity            → PASS (扫描 37 个脚本)
$ python tmp\zz-run-guard.py check_cmd_line_endings         → PASS (检查 7 个 .cmd)
$ python tmp\zz-run-guard.py check_ui_element_references    → PASS (引用 8 个控件名 / XAML 78 个 x:Name)
$ python tmp\zz-run-guard.py check_residue_rules            → PASS (11 必排 + 7 必留)
$ python tmp\zz-run-guard.py check_releases_manifest_shape   → PASS (116 行)
$ python tmp\zz-run-guard.py check_app_pri                  → PASS (ZongxianVoice.exe 276,992 B / resources.pri 866,648 B)
$ python tmp\zz-run-guard.py check_dist                     → FAIL ← 见 A0
```

**A0 · 基线时 `check_dist()` 本来就 FAIL（**不是我造的**）**

```
  [问题] zongxian_media.dll 没在 dist\RELEASES.md 里登记
---- FAIL: dist 卫生
```
事实：`dist\zongxian_media.dll`（9,728 B）确实躺在 dist 根，而 `dist\RELEASES.md` 没登记它 —— 审计 §7.3 预告过的老问题。
**重要限制条件**：`release.py:225-240 step_normalize_dist()` 会在跑守卫**之前**把它挪进隔离区，所以走 `release.py` 时这条不红（§6.2 的闸门结果里 `---- PASS: dist 卫生` 证实了）。**但手工跑 `run-checks.py release` 会因这条 exit=1。**

### A1 · 缺 `import re` 的假文件 → `check_script_sanity()` FAIL

造：`build\zz-probe.py`
```python
def probe():
    return re.search(r"x", "x")
```
```
$ python tmp\zz-run-guard.py check_script_sanity
== 守卫：build\*.py 名字绑定（防 NameError 型死断言）
  扫描 38 个脚本
  ! zz-probe.py:2 用了 `re.…` 但全文没有绑定 `re`
---- FAIL: build 脚本名字绑定
== RESULT check_script_sanity: FAIL        EXIT=1
```
**还原**：删除该文件 → 复跑 `扫描 37 个脚本 / ---- PASS`，`EXIT=0`；`Test-Path build\zz-probe.py` = `False`。

### A2 · `build\build-media.cmd` 改成 LF → `check_cmd_line_endings()` FAIL

| | CR | LF | size | sha256 |
|---|---|---|---|---|
| 基线 | 97 | 97 | 3,392 | `4571BC520532B2A89C753C1ABC42DBC5C0347D65A1CAECADFD80106646D6F95A` |
| 改后（去掉全部 0x0D） | 0 | 97 | 3,295 | — |
| **还原后** | 97 | 97 | 3,392 | **`4571BC52…F95A`（与基线逐字节相同）** |

```
$ python tmp\zz-run-guard.py check_cmd_line_endings
  检查 7 个 .cmd
  ! build-media.cmd: CR=0 / LF=97 —— 有裸 LF 行尾，cmd 会解析错位
  ! build-media.cmd: 文件末尾没有换行（cmd 在最后一行会报 'inside was unexpected'）
---- FAIL: cmd 行尾
== RESULT check_cmd_line_endings: FAIL     EXIT=1
（还原后复跑：---- PASS: cmd 行尾，EXIT=0）
```

### A3 · `dist\winui\received\zz.bin` → `check_dist()` 报残留

`dist\winui\received` 造之前**不存在**（`Test-Path` = `False`），我建目录 + 64 B 假文件。
```
$ python tmp\zz-run-guard.py check_dist        （仅摘问题行）
  [问题] 测试残留目录（文件传输落盘/录音/中间产物）进 dist：winui\received\zz.bin
  [问题] zongxian_media.dll 没在 dist\RELEASES.md 里登记      ← A0 的基线项，仍在
---- FAIL: dist 卫生
（删除文件与目录后复跑：只剩 A0 那一条问题行；received 存在? False）
```

### A4 · `smoke-ui.py` 引用不存在的控件 → `check_ui_element_references()` FAIL

把 `smoke-ui.py:473`（当时版本 `7EE8CC28…`）的 `toggle = locate('ShareAudioToggle')` 改成 `locate('NoSuchControlXYZ')`：
```
$ python tmp\zz-run-guard.py check_ui_element_references
  smoke-ui 引用 9 个控件名；MainWindow.xaml 定义 78 个 x:Name
  ! smoke-ui 断言了 MainWindow.xaml 里不存在的控件 NoSuchControlXYZ（测试写错，不是产品坏）
---- FAIL: 界面自检控件名
== RESULT check_ui_element_references: FAIL   EXIT=1
```
**还原**：从备份写回 → sha256 = `7EE8CC289C78C428A6A1310BA685C0BC318CFB0DB9C8D945B5E6FE39F28075E5`（同基线）；`NoSuchControlXYZ` 命中数 **0**；复跑 PASS。

### A 段小结
四条反例**全部如期 FAIL**，且**全部逐字节还原**（A2/A4 用 sha256 证明）。守卫不是"注释型"闸门，是**真闸门**。

---

## 3. B 段 · C# 应用层逐条核实（`src\winui-cs\src`）

### ① `SetCallState` 已无文案参数 ✓
`MainWindow.xaml.cs:2012`
```csharp
private void SetCallState(bool active, bool error = false)
```
函数体 `:2014-2032` 只做三件事：`CallStateDot.Fill`、`_inCall` 变更日志、`UpdateActionButtons()` —— **一行都不碰 `CallStateText`**。
4 个调用点全是布尔：`:2544 SetCallState(active: false)`、`:2567 (active: false, error: true)`、`:2700 (active: active)`、`:2786 (active: false, error: true)`。
调用顺序 `:2698-2704`：先 `SetCallState`，再
```csharp
SetCallText(active ? (sending && receiving ? "通话中" : sending ? "我在说话" : "仅收听")
                   : (link == "connected" ? "已连接（未通话）" : "未通话"));
```
⇒ 「我在说话 / 仅收听 / 已连接（未通话）」不再会被"通话中/未通话"无条件覆盖。

### ② `UpdateChatStatus` 真的优先显示未过期提示 ✓
```csharp
:1186  private string? _chatTip;
:1187  private DateTime _chatTipUntil;
:1191  private void ShowChatTip(string text)          // _chatTipUntil = UtcNow + AppConfig.ChatTipSeconds
:1200      _chatTipTimer = DispatcherQueue.CreateTimer(); IsRepeating = false;
:1204      Tick → { _chatTip = null; UpdateChatStatus(); }   // 过期即恢复
:1213  private void UpdateChatStatus()
:1216      if (_chatTip is { } tip && DateTime.UtcNow < _chatTipUntil) { ChatStatus.Text = tip; }
:1220      else { _chatTip = null; /* 事实文案分支 */ }
```
存活秒数来自 `src\winui-cs\src\AppConfig.cs:69 public const int ChatTipSeconds = 8;`（**非写死**）。
调用点 `:2162 ShowChatTip("捕获被占用，已清理，可重试")`、`:2432 ShowChatTip(m)`。
⇒ **"谁在什么条件下不覆盖它"= `:1216` 的未过期分支**；过期由 `:1204` 的 DispatcherQueueTimer 主动恢复。

### ③ `ShareAudioSwitch` / `StopControlButton` / `ControlBanner` —— **不是**全仓库 0 命中（需分线）
| 名字 | `src\winui-cs\**`（出货线） | 其他位置 |
|---|---|---|
| `ShareAudioSwitch` | **0 命中** ✓ | `build\smoke-ui.ps1:142,144`（**已被 lead 删除**，见 §5.5）；`build\smoke-ui.py:449`、`build\run-checks.py:460` 仅注释；docs 提及 |
| `StopControlButton` | **0 命中** ✓ | `src\winui\src\MainWindow.xaml:56`（**旧 C++/WinRT 线**）；**无任何 `.cpp` 引用**，且该 `Button` 没有 `Click=` |
| `ControlBanner` | **0 命中** ✓ | `src\winui\src\MainWindow.xaml:34,48` + `src\winui\src\MainWindow.cpp:105 FindName(L"ControlBanner")` |

`src\winui\src\MainWindow.xaml:34-58` 的"停止控制"横幅**与审计真 bug #6 描述逐字相同**（`Visibility="Collapsed"` 默认、`StopControlButton` 无 `Click`、代码只把它设成本来就是的 Collapsed）。
⇒ 结论：**出货线（winui-cs）已删干净 ✓**；但**同一"假控件"在废弃重复线 `src\winui` 里原样保留** —— 而审计 §7.3 记着 `build-winui.cmd` 与主线**写同一个 `dist\winui`**。建议：删除该废弃线，或至少写进禁止清单（不影响本次 C# 验收，但属于"同一 bug 仍在仓库"）。

### ④ `CopyDiagnostics` 无 UI 线程同步等待 ✓
`MainWindow.xaml.cs:3657-3680`：UI 线程只做 `CopyToClipboard(BuildDiagnosticsText(cap: null))`（`cap=null` 如实写"探测中…"，`:3690-3692`），探测走
```csharp
:3661  var probe = System.Threading.Tasks.Task.Run(() => AppAudioProbe.Probe());
:3662  probe.ContinueWith(t => DispatcherQueue.TryEnqueue(() => { ... }));
```
全文 `AppAudioProbe.Probe()` 只有两处：`:3661`（Task.Run 内）与 `:1283`（也在 `:1281 Task.Run(...)` 内）。
`WaitForExit` 只出现在 `AppAudioProbe.cs:104`（`AppConfig.SubprocessProbeTimeoutMs`）与 `NativeVideoHost.cs:259`（`HelperExitWaitMs`），都在后台/非 UI 路径。**UI 线程不等待** ✓

### ⑤ 开发机绝对路径 = 0 命中 ✓
`grep 'D:' src\winui-cs\src\*.cs` → **No matches found**（连 `D:` 子串都没有）。
查找顺序已改为"应用目录 / tools + 显式命名后门"：`AppAudioProbe.cs:153-164`（`ZX_PROBE_PATH`）、`NativeVideoHost.cs:87-98`（`ZX_VIDEO_HELPER_PATH`）。

### ⑥ 非注释的 `45890` 只剩 `AppConfig` 常量 ✓
`grep 45890|45891 src\winui-cs\src\*.cs` 共 9 命中，逐条判：
- **代码**：`AppConfig.cs:39 public const int DefaultSignalPort = 45890;` ← **唯一**
- 注释：`MainWindow.xaml.cs:10,17,18,19`（文件头示例）、`:3537`（"// 三种写法都收：192.168.1.5:45891 …"）、`AppConfig.cs:4,5`（说明为什么收敛）
- `MediaEngine.cs:93` 注释里的示例
自检路径已全部改用 `_signalPort`，含推导 `:{_signalPort + 1}`（`:1655,1661`）；`:128 _signalPort = AppConfig.DefaultSignalPort`。

### ⑦ TURN 下发链路闭环 ✓
```
设置界面读/写：:3004-3006（TurnUrlBox/TurnUserBox/TurnPasswordBox ← _config）
关设置写回：  :3238-3244  _config.TurnUrls/... = ...; _config.Save(); _ = PushIceConfigAsync("设置关闭");
下发：        :3262-3292 PushIceConfigAsync
                :3264 servers = _config.BuildIceServers()      // AppConfig.cs:177-193
                :3275 await _media.CallAsync(AppConfig.IceConfigPageMethod, new { iceServers = servers });
                      AppConfig.cs:83 IceConfigPageMethod = "setIceConfig"
C#→页面通道： MediaEngine.cs:289-306  ExecuteScriptAsync("zxEngine.setIceConfig(JSON.parse(...));")
页面收：      call.html:2387 setIceConfig(cfg){ return window.zxIceConfig(cfg||{}); }  → :1419 window.zxIceConfig
              → :1382 setIceConfigImpl → :1362-1376 applyIceServers（对 state.pc 与 links 全部 pc 调 setConfiguration）
回执：        :1400 post({type:'ice-config', …}) → C# :2721-2739 case AppConfig.IceConfigEvent → _iceConfigAck?.TrySetResult
超时日志：    :3285-3289 等 ack 最多 AppConfig.IceConfigAckTimeoutMs(10000)，超时写"页面未就绪、ICE 配置未下发"
候选错误：    页面 :1477 post ice-candidate-error → C# :2742-2757 写 [TURN] 候选出错…
隐私：        :3693 只报 _config.TurnSummary（条数），AppConfig.cs:195-200 绝不回显账密
```
**闭环成立**，且**空配置 = 只用 STUN**（`AppConfig.cs:179` 返回空数组；页面 `call.html:1386` 回退 `DEFAULT_ICE_SERVERS`）。

### ⑧ 共享电脑声音入口优先级明确 ✓（附两条**新发现**）
`MainWindow.xaml.cs:2524-2536`
```csharp
var uiPicked = ShareAudioAppCombo.SelectedItem as ShareSource;
var backdoorSpec = Environment.GetEnvironmentVariable(AppConfig.ShareAudioBackdoorEnv);   // ZX_SHARE_AUDIO
if (uiPicked is null && !string.IsNullOrEmpty(backdoorSpec)) {
    AppendEngineLog($"[共享声音] 界面没有选择来源 → 走测试后门（ZX_SHARE_AUDIO={backdoorSpec}）");
    _ = StartShareAudioAsync(backdoorSpec);
}
```
⇒ 界面优先、后门仅在"界面无选择"时生效、生效必写日志。**优先级明确 ✓**

**新发现 ⑧-b（审计 §五-4 未闭合）→ 第二轮已关闭（§10.2）：C# 侧永久 2 秒轮询**
`MainWindow.xaml.cs:2497-2523`（`case "ready":`）
```csharp
_ = Task.Run(async () => {
    while (true) {                                            // 无 CancellationToken
        await Task.Delay(AppConfig.ShareAudioStatusPollMs);   // 2000 ms
        DispatcherQueue.TryEnqueue(async () => {
            try { var cap = _shareAudio;
                  if (cap is not null) AppendEngineLog("[共享声音] 泵状态：…");
                  await _media.CallAsync("shareAudioDebug");  // 无条件每 2 秒一次
            } catch { }
        });
    }
});
```
实证（**应用自己写的日志**，非构造）：`tmp\smoke-ui.log`（06:40 那轮，22 行）
```
06:40:55  [共享声音] share-audio-debug: {"info":{"active":false,"links":0,"attached":0,"pushed":0,"trackState":"none",…}}
06:40:57 / 06:40:59 / 06:41:01 / 06:41:03 / 06:41:05   共 6 条，间隔正好 2 秒
```
统计：`share-audio-debug` 6 行、`泵状态` **0** 行、`已开始采集` **0** 行。全套件里只有 1 处 `CancellationTokenSource`（`:1783`，与此无关）。
后果：① 关窗后仍在跑；② `ready` 再次触发会**再起一条**（叠加）；③ 这条 spam 会挤掉 `AppendEngineLog` 的 120 行可视缓冲（`:3703-3711`），而界面自检恰恰**靠日志取证**（`wait_log(log, '已开始采集')`）。
页面侧同类问题**已修**（`call.html` 只剩 micMeter `:676`、statsTimer `:2217`(1 s)、decodeTimer `:2816` 三个 `setInterval`，且 `:715/:2244/:2826/:2830/:2882` 都有 `clearInterval`，`hangup :2178-2179`、`stopAll :3141-3143`、`pagehide/beforeunload :3233-3240` 都收口）⇒ **漏的是 C# 这一条**。

**新发现 ⑧-c（审计真 bug #1 症状仍在）→ 第二轮已关闭（§10.2）：设置里设备列表 0 个**
实证：`tmp\smoke-ui.log`（07:00 那轮）最后一行
```
07:00:23  [设置] 设备：麦克风 0 个、扬声器 0 个
```
该行由 `MainWindow.xaml.cs:3348` 打印，而**同一台机器** `dist\zxprobe.exe devices` 我实测返回 **3 个麦克风 + 6 个播放设备**（含 `麦克风 (Steam Streaming Microphone) channels=1 sampleRate=44100`）。
代码路径：
- `:2635-2640 case "devices" → OnDevicesReported(payload)` ← task-1 补的 handler **确实存在** ✓
- `OnDevicesReported :3322-3350`，关键在 **`:3334`**：`if (string.IsNullOrEmpty(id)) continue;`（空 `deviceId` 直接丢）
- 页面 `call.html:2456-2476 listDevices()`：`navigator.mediaDevices.enumerateDevices()` → `id: d.deviceId`，只有 `label` 有空值兜底（`:2454` 注释也只预见到 label 为空）
- **时序**：`listDevices` 只被 `RefreshDevicesAsync()`（`:3314-3319`）调用，唯一调用点 `:3029`（**打开设置时**）；而麦克风权限只在页面真正 `getUserMedia`（`call.html:1068`）时才由 `MediaEngine.cs:185-198 PermissionRequested → Allow` 授予
⇒ 只要"启动后先开设置"，`enumerateDevices()` 就在**未授权**状态下跑。Chromium 未授权时 `MediaDeviceInfo.deviceId` 返回空串，于是 `:3334` 把条目全丢 → **0/0**（另一种可能是未授权时直接返回空数组；两者结论相同）。**要把根因钉到 100%，只需在 `OnDevicesReported` 开头临时打印 `payload` 原文跑一次**——我没有跑 GUI，不替它钉死。
⇒ 现象与审计真 bug #1 **逐字一致**（"设置里麦克风/扬声器下拉永远是空的，且不报错"）：**handler 补上了，用户看到的结果没变**。

---

## 4. C 段 · 页面（`src\winui-cs\media\call.html`，3,247 行）

### ⑨ 语法校验通过 ✓
抽出唯一内联 `<script>`（1 块、classic、3,224 行）→ `node --check`：
```
$ node.exe --check tmp\zz-calljs\block0.js     → exit=0（无输出）
正对照：自造 function ( {  → exit=1（node v24.21.0 报语法错）
```

### ⑩ `renegotiateSharedAudio` 零定义零调用 ✓
`src\winui-cs\media\` 内只剩 `:1633` 一句注释：「**【死代码已删】原来这里写着"对已稳定的链路由 renegotiateSharedAudio 单独发 offer"**」。
（`dist\棕仙语音-测试版-v36\media\call.html:1366,1384` 仍有旧实现 —— 那是**已作废的 v36 解压副本**，非源码。）

### ⑪ ICE 有 TURN 合并路径、无写死 TURN 地址 ✓
- 默认只有 STUN：`:1289-1292 DEFAULT_ICE_SERVERS = [stun.l.google.com:19302, stun1…]`
- 合并：`:1325-1340 makeIceServerList()`（下发项在前 + 默认 STUN 兜底 + 按 urls 去重）；`:1362-1376 applyIceServers()` 对**已有**的每条 pc 调 `setConfiguration` 立即生效
- 写死地址检查：`grep 'turn:[a-zA-Z0-9]'` → **仅 1 命中**，是 `:1279` 注释里的占位 `'turn:host:3478?transport=udp'`（其余 `turn:` 命中是 `:1353-1354` 的正则判断与 `:1440-1446` 的失败原因文案）
- 失败原因上报：`:1432-1446 reportIceFailure` 区分"没配 TURN / 配了但没拿到 relay / TURN 拒绝"，经 `:1477` `ice-candidate-error` 上报

### ⑫ 定时器全部可取消 ✓
`setInterval` 仅 3 处：micMeter `:676`、statsTimer `:2217`、decodeTimer `:2816`；`clearInterval` 见 `:715,2244,2826,2830,2882`；`hangup` 调 `stopStatsLoop/stopMicMeter`（`:2178-2179`）；`zxEngine.stopAll()`（`:3141-3143`）；`pagehide`/`beforeunload` → `onPageGone` → `zxEngine.stopAll()`（`:3233-3240`）。
statsTimer 回调显式 `catch` 并 `post scriptError`（`:2225-2234`），未静默吞异常。

### ⑬ 页面侧后门已清 ✓
`grep 'ZX_SHARE_AUDIO|location.search|URLSearchParams'` → 仅 `:188` 一句注释："页面**没有**环境变量/URL 参数入口（已核对：无 ZX_SHARE_AUDIO、无 location/search 分支）"。

---

## 5. D 段 · 引擎（`src\media`）

### 5.1 编译证据（**我自跑**）
```
$ cmd /c build\build-media.cmd          （workdir = D:\文档\ai001）
  [2/5] Compiler env: D:\BuildTools\VC\Auxiliary\Build\vcvars64.bat
  [3/5] cmake: D:\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe
  [4/5] Configuring ...  -- Selecting Windows SDK version 10.0.26100.0 / 编译器 MSVC 19.44.35229.0
  [5/5] Building ...
    zx_media_core.vcxproj -> …\zx_media_core.lib
    zongxian_media.vcxproj -> …\zongxian_media.dll
    zxprobe.vcxproj        -> …\zxprobe.exe
    nstest.vcxproj         -> …\nstest.exe
  === Build OK ===
  BUILD_MEDIA_EXIT=0
```
（唯一告警 `MSB8029`：输出目录在 `%TEMP%` 下，与本项目长期做法一致。）

### 5.2 采样率契约（审计真 bug 11）—— `48000` 只剩"故意对照列" ✓
```
$ dist\zxprobe.exe rate-check
采样率契约自检（帧→秒 必须用实际 rate）
  头rate  帧数     按头rate(秒) 按48000(秒) 结论
  48000    48000      1.000000     1.000000     OK
  44100    44100      1.000000     0.918750     OK
  44100    48000      1.088435     1.000000     OK
  16000    16000      1.000000     0.333333     OK
结论：全部一致（头 rate == 计算 rate）
注：44.1k 一行的"按48000"列是故意留的对照 —— 差 8.8% 就是变调来源。
EXIT=0
```
`grep 48000 src\media\tools\zxprobe.cpp` 10 命中逐条判：**7 条注释**（`:24,33,104,182,184,327,469`）+ **表头 `:196`** + **对照列 `:205 FramesToSeconds(frames, 48000)`** + **说明 `:218`**。
真正换算用实际值：`:330 rate_effective = ParseFormatInt(fmt_buf, "sampleRate", ZX_SAMPLE_RATE)`，时长/吞吐全走 `FramesToSeconds(..., rate_effective)`（`:413,444,447,648,653`）；纯软件自检 `CmdRateCheck :187-219` 断言"头 rate == 计算 rate"。
**旁证**：`dist\zxprobe.exe devices` 输出里真有非 48k 设备 —— `麦克风 (Steam Streaming Microphone) channels=1 sampleRate=44100` ⇒ 这不是纸上问题。

### 5.3 RingBuffer 单位契约与调用点一致 ✓
```
$ dist\zxprobe.exe ring-check
RingBuffer 单位契约自检（单位：样本 float）
  写入 480 帧 × 2 声道 = 960 样本 -> Write 返回 960，AvailableSamples 960
  读回 960 个样本，逐点不符 0 个；容量 4096 = 可读 960 + 可写 3136
  判定：OK（样本单位自洽，立体声不错位）
EXIT=0
```
- 契约：`src\media\src\audio\ring_buffer.h:8-20`「这个类里的一切计数单位都是**样本(float)**」；API `:40 RingBuffer(int capacity_samples)`、`:44 Write(..., int samples)`、`:48 Read(..., int cap_samples)`、`:53-55 CapacitySamples/AvailableSamples/FreeSamples`
- 写侧：`wasapi_backend.cpp:758-760 ring_->Write(scratch_.data(), frames * format_.channels)`
- 读侧：`wasapi_backend.cpp:375-387 max_samples = cap_frames * ch` → `total_samples / ch` 折回帧
- **两层别混**：C ABI 仍以帧为单位（`zongxian_media.h:265-274 cap_frames/out_frames`）—— 与 §5.6 的文档说法一致

### 5.4 假开关逐条现状（8 条，每条给"删了/接上了"）

| # | 开关 | 判定 | 证据 |
|---|---|---|---|
| 1 | `master_volume` | **接上** | `media_api.cpp:247,480` 存值；`:725-732` 在**读取路径**上真正施加增益（`gain != 1.0f` 时逐样本乘） |
| 2 | `logLevel` | **接上** | `media_api.cpp:268-276` 解析→`g_log_min_level:86`；`LogLine:89` 按阈值真过滤；非法值 `:273` 直接 `ZX_ERR_INVALID_ARG`（不再静默） |
| 3 | `ZX_NSB_RNNOISE` | **改成明确报错**（不再静默降级） | `media_api.cpp:848-851`：`ZX_HAVE_RNNOISE=0` 时 `SetError("本构建未包含 RNNoise 后端…")`；`:885 rnnoiseAvailable` 如实暴露 |
| 4 | `AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM` | **接上** | `wasapi_backend.cpp:504-514` 仅端点采集请求 `AUTOCONVERTPCM\|SRC_DEFAULT_QUALITY`（目标格式真的换了采样率）；`:518-527` 驱动不支持时**如实回退**并记录；loopback 按定义不走（`:493-497`） |
| 5 | MMCSS / Pro Audio | **接上** | `wasapi_backend.cpp:683-691` 真调 `AvSetMmThreadCharacteristicsW(L"Pro Audio", &task)`，结果进 `snap.pro_audio` |
| 6 | `InstallSentinels` / `SentinelCheck` | **删除** | `noise_suppress.h:97-99` 注释记录删除原因；全仓**无代码命中** |
| 7 | `sanitized_` | **接上** | `media_api.cpp:796-797` 暴露 `sanitizedSamples`；`wasapi_backend.cpp:740` 计数 |
| 8 | `zx_source_set_callback` | **现在有实现（原先"无消费者"）** | `media_api.cpp:807` 实现、`:826` 清空路径；`zongxian_media.h:298` 记「【2026-10-06 核实】这条链路是**通的**」 |

### 5.5 线程/生命周期（抽查）
- stats 快照：`wasapi_backend.cpp:402-406`（seqlock，读侧重试，注释说明原来直接读 `stats_` 是数据竞争）
- `ring_buffer.h:64-66` 说明"一把锁管读写两端"、`Read` 串行化
- 我**未**做并发压测（`nstest` 等），因此"无释放后使用"这一条**未被我独立证明**，只核对了代码意图。

---

## 6. E 段 · 机制复核（两条新机制 + 发版闸门）

### 6.1 `build\smoke-ui.py`
> 第一轮被验版 `EEC4270B…`（33,369 B，06:47:48）；**第二轮复核的当前版 `AFD8C7CF…`（38,020 B，07:05:40，814 行）**，下表行号按当前版。

| 断言 | 结论（当前版 `AFD8C7CF…`） |
|---|---|
| 环境中止 = `[ABORT]` + 退出码 2 | ✓ `abort() :101-111`（打印 2 行 `[ABORT]` + `cleanup()` + `sys.exit(2)`）；`fail()` = `[FAIL]` + exit 1 |
| `--no-click` 完全不发键鼠 | ✓ `if a.no_click:` **:683** → `assert_foreground` → 打印「未发送任何键鼠」→ `cleanup()` → `return 0` **:688**；**所有注入键鼠的调用都在 :683 之后**：`real_click` 调用点 `:694,721,735,760`；`scroll_into_view`（内含 `gui.move` + `gui.scroll(-3) :290`）调用点 `:747,756`；`gui.glide :625` / `gui.click :628` 只在 `real_click :566` 内部 |
| 但 `--no-click` **并非零副作用** | ⚠ `:677` 在 `no_click :683` **之前**调 `relax_foreground_lock() :443-463` → `SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0)` **改系统设置**；同时 `SetWindowPos(HWND_TOPMOST)` + `SetForegroundWindow` 会抢焦点。还原：`cleanup() :114-116 → restore_foreground_lock() :466-477`（`_FG_LOCK_ORIG is None` 时 no-op），**且 `:808 atexit.register(restore_foreground_lock)`** ⇒ Ctrl-C / 异常退出也会还原 ✓；只剩"硬杀进程（`taskkill /F`）"无解 —— 代码 `:803-807` 已如实写明。闸门日志证明还原生效：`已还原前台锁超时 = 2147483647 ms` / `2776762776 ms` |
| 第一轮我提的两条"测试侧"可疑点 | ① `content_offset_y` 经验偏移**已不再用于开关点击**（`:760` 不带该参数，默认 0）；② 旧的 `locate_scrolling :304` 现在**已无任何调用点**（被 `scroll_into_view :268` 取代）⇒ 死函数（见 §10.4） |

### 6.2 `build\run-checks.py`（当前版 `12E163F0…`，36,463 B）
| 断言 | 结论 |
|---|---|
| 环境中止 → 退出码 3 | ✓ 判定 `:61 if env_abort and (proc.returncode == 2 or "[ABORT]" in out)` → `raise EnvBlocked`；`main` 捕获 `:645-647` → `fmt_env_blocked(...)` → `return 3` |
| `fmt_env_blocked` 话术 | ✓ `:575-586`：原因 / "这一项**无法验证**（不是失败也不是通过）" / 典型触发 / 排障命令（含 `--no-click`）/ **"发版：不发。'自检不过不发包'包含'没法自检'"** / **"退出码：3"** |
| `release` 模式也跑传输层 | ✓ `:664 if what in ("all","selftest","release")`；`:672-677` 在 release 下把"找不到探针"记 `results.append(False)`（SKIP 不许放行） |
| **25 项的**数法（关键） | `results.append` 共 16 处：`all\|build\|release` 分支 = 构建 `:597,604,611` + 界面自检前置/全量 `:629,637`（二者互斥于 `:650` 的补 `False`）+ 守卫 `:652-659`（8 条）；`all\|selftest\|release` 分支 = 传输层 `:692`（+ `:677` 兜底）。**正常路径 = 3 + 2 + 8 + 1 = 14**，与闸门日志 `汇总: PASS 13 / FAIL 1`（共 14 行 `[PASS]/[FAIL]`）**逐项吻合** ✓ |
| 小坑 | `:591` 白名单收 `"dist"`，但两个分支都不匹配 ⇒ `run-checks.py dist` = **0 项**、打印 `汇总: PASS 0 / FAIL 0`；好在 `:699 return 0 if all(results) and results else 1` 会返回 **1**，不会假绿，但"跑一个什么都不干的模式"仍是坑（审计 §7.2 同类） |

### 6.3 `build\release.py`：闸门真的会拦 ✓
`step_normalize_dist()`（`:225-240`，把 `zongxian_media.dll` 挪进隔离区）→ `step_sync_media()`（`:84-101`，同步页面 + `tools\zxprobe.exe`）→ `step_guards()`（`:122-128`）：
```python
rc = run([PY, str(ROOT / 'build' / 'run-checks.py'), 'release'])
if rc != 0:
    say('  ✗ 守卫未通过（退出码 %d）—— 不产出 zip' % rc)
    return False
```
`main` 里顺序为 build(`:279`) → normalize(`:282`) → sync_media(`:284`) → build_info(`:286`) → guards(`:288`) → package(`:290`) ⇒ **FAIL(1) 与环境中止(3) 都不出包**。
**实测印证**（§7）：日志末尾就是 `✗ 守卫未通过（退出码 1）—— 不产出 zip`，且 `dist\` 没有 v37 zip。

### 6.4 发版闸门实测汇总（**Lead 执行**，我只读日志与分析）
`tmp\release-v37c.log`（235 行，20,614 B，07:00:58）：
```
---- PASS: C++ 助手构建（receiver-probe）
---- PASS: C# 视频原型构建（winui-videoprobe）
---- PASS: C# 主应用构建（winui-cs / 棕仙语音）
---- PASS: 界面自检前置（只验启动+真置顶+前台断言，不发键鼠）
---- FAIL: 界面自检（模拟键鼠：启动→进入→设置；元素必须在屏幕内）  <- exit code 1; 输出里出现 '[FAIL]'; 输出里缺少 '[PASS] smoke-ui'
---- PASS: 主应用产物资源索引（resources.pri）
---- PASS: dist 卫生                                        ← 我报的 A0 红已被 normalize 消掉
---- PASS: 媒体资源与引擎探针版本一致性                     ← 我报的探针旧副本红已被 sync 消掉
---- PASS: build 脚本名字绑定
---- PASS: 残留规则反例（11 必排 + 7 必留）
---- PASS: 界面自检控件名
---- PASS: RELEASES 表格结构（115 行）
---- PASS: cmd 行尾
（传输层）6 个阶段通过，0 个失败 → ---- PASS: 传输层 6 阶段自测
汇总: PASS 13 / FAIL 1
  ✗ 守卫未通过（退出码 1）—— 不产出 zip
```
失败点原文（`:136-144`）：
```
[  17.8s] ShareAudioToggle：向下滚 1 次后可见 rect=(745, 864, 112, 10)
[  18.2s] 共享电脑声音：独立开关 + 来源列表都在（经滚动确认）✓
[  19.4s] 已用真鼠标点击 (801,891)（含内容偏移 +22） -- 拨开「共享电脑声音」开关
[FAIL] 拨开开关后，应用日志里没有"已开始采集"（界面这条路没通）
[  44.4s] 已还原前台锁超时 = 2776762776 ms
```

### 6.5 对唯一 FAIL 的独立分析（**第二轮已定性并关闭**：见 §10.1 / §10.2）
`OnShareAudioToggledAsync`（`MainWindow.xaml.cs:3206-3218`）的**每一条分支都无条件写日志**：
- `IsOn == true` → `StartShareAudioAsync`：失败也写 —— `:3084 [共享声音] 找不到 zxprobe.exe，无法采集` / `:3092 [共享声音] 启动引擎采集失败`；成功写 `:3098 [共享声音] 已开始采集（…）`
- `IsOn == false` → `StopShareAudioAsync` → `:3227 [共享声音] 已停止`

而那次运行的**应用日志** `tmp\smoke-ui.log`（17 行，07:00:08 起）**最后一行是 07:00:23 的开设置记录**，点击发生在 07:00:27 前后，**之后一行都没有**，上面 4 种日志**一条都没出现**。
⇒ 唯一自洽的解释是：**ToggleSwitch 的 `Toggled` 根本没触发** ⇒ 真鼠标点 `(801,891)` 没落在控件上（**测试侧坐标问题**），而不是"点了但采集起不来"。
可疑点：UIA 矩形 `h=10` 是被裁剪的，`content_offset_y=+22` 这个经验偏移是主要嫌疑；且 `x=801` 是 112 px 宽矩形的**中心**，WinUI `ToggleSwitch` 的开关本体在右侧。
**一行插桩即可定性**：点击后 `say('IsOn=%s' % ShareAudioToggle.IsOn)`（或在 `OnShareAudioToggledAsync` 入口无条件写一行日志）。我没有跑 GUI，因此**只判到"高度可能"**，不替它下结论。

---

## 7. F 段 · 文档 vs 代码（抽查 6 处，其中 3 处**已修**、3 处**仍不符**）

### 7.1 已修且我复核为**正确** ✓
1. `docs\share-audio-status-2026-10-06.md:54-66`（sha `2CFC7A23…`，06:23:25）—— 原先断言"API/注释仍写**帧**（`ring_buffer.h:25 capacity_frames`、`:39-41 CapacityFrames…`）"，**与代码相反**（代码已改名）。现已改为：`ring_buffer.h:8-20` 写死"样本(float)"、API `capacity_samples`/`samples`/`cap_samples`/`CapacitySamples|AvailableSamples|FreeSamples`、写侧 `wasapi_backend.cpp:758-760`、读侧 `:375-387`、统计键 `bufferedSamples`、并注明"**C ABI 仍以帧为单位**"。逐条与代码事实一致 ⇒ **该 finding 关闭**。
2. `docs\handoff-next-session.md:125` —— 现写「`zongxian_media.h` 与 `noise_suppress.h` 的两处相反说法**同日已改**」。代码事实：`src\media\include\zongxian_media.h:188-197`「【2026-10-06 更正，旧说明已作废】…该宏已不控制任何分支…ProbeProcessLoopback() 现在默认执行真实探测…真因是对指向栈变量的 VT_BLOB 调 PropVariantClear」；`src\media\src\audio\noise_suppress.h:55-61`「channels: 生产路径**恒为 1** —— media_api.cpp 先把多声道下混成单声道」，与 `media_api.cpp:720 DownmixToMono` 一致 ⇒ **正确**。
   （附注：审计原文引用的 `zongxian_media.h:166-169` 已因文件改动**移位**，那两处现在是 `zx_last_error` / `zx_media_dump_log`；作废说明在 `:188-197`。）
3. `docs\handoff-next-session.md:126` —— 现写「已统一：`ring_buffer.h:8-20` 写死…API 已改名…调用点传 `frames * channels`（`wasapi_backend.cpp:758-760`）。**注意**：C ABI（`zongxian_media.h`）仍以**帧**为单位」⇒ 与 §5.3 逐条一致，**正确**。
4. `docs\handoff-next-session.md:102,105,107` 的行号 —— `smoke-ui.py:281-307 / 310-319 / 94-104`、`run-checks.py:626-636 / 60-63 / 575-586 / 645-647`，我按当前文件逐一核对**全部命中** ✓。
5. `docs\handoff-next-session.md:105` 补充说明「它仍然会 `SetWindowPos`/`SetForegroundWindow` 抢焦点，但**不注入任何键鼠**」—— 与 §6.1 独立结论一致 ✓。

### 7.2 第一轮报的 3 处不符 —— **第二轮已全部修好，我逐条复核为正确** ✓
1. `README.md:120-121`（sha `E1B0BD78…`）：写 **25 项**，但括号里只枚举 **10** 项：
   > `25 项（3 构建 + 界面自检前置 --no-click + 界面自检（模拟键鼠）+ pri 守卫 + dist 卫生 + 媒体/探针一致性 + build 脚本名字绑定 + 传输层 6 阶段）`
   **漏了 4 条**：残留规则 / 界面控件名 / RELEASES 表格结构 / cmd 行尾（对应 `run-checks.py:656-659`）。
   （`docs\handoff-next-session.md:45-48` 的枚举是**完整**的：3 构建 + 2 界面自检 + 8 守卫 + 传输层 = 14 —— 可照抄。）
2. `docs\dist-policy-2026-10-05.md:56-58`：同样写 14、括号只枚举 10（同上漏 4 条）；`:58` 还留着"修复前=9 项"（历史数字，**无法从当前代码复现**，建议改成像 handoff:51 那样的相对说法）。
3. `docs\dist-policy-2026-10-05.md:59` **算错了**：
   > `python build\run-checks.py build    # 只跑构建 + 界面自检前置/界面自检 + 4 个守卫（9 项，不含传输层）`
   代码事实：`build` 走 `run-checks.py:596-659` 同一块，**8 条守卫全跑**（`:652-659`），只是**不含传输层**（`:664` 只对 `all/selftest/release`）⇒ **`build` = 13 项**，不是 9。
   同段 `:63-64` 的"数法"行号也失效：`:421`（构建块→实为 596-617）、`:450-461`（两条界面自检→628-644）、`:477-480`（"4 个守卫"→实为 8 条 652-659）、`:482`（传输层→664-692）、`:505`（打印项名→694-698）。

**第二轮复核（07:12，逐条对照当前文件，全部已成修）：**
- `README.md:120-123`：现在是
  > `冒烟检查：25 项 = 3 构建 + 界面自检前置 --no-click + 界面自检（模拟键鼠）` / `+ 8 条守卫（pri / dist 卫生 / 媒体·探针一致性 / 脚本名字绑定 / 残留规则 / 界面控件名 / RELEASES 表格结构 / cmd 行尾）` / `+ 传输层 6 阶段；release 模式同为 25 项`
  ⇒ 25 项与 `run-checks.py:597-692` 的 `results.append` **逐项对齐** ✓
- `docs\dist-policy-2026-10-05.md:56-61`：同样是完整的"3 构建 + 2 界面自检 + 8 守卫（逐条点名）+ 传输层 6 阶段"；**`:61` 现写 `run-checks.py build` = 「只跑构建 + 两条界面自检 + 8 条守卫 = 13 项（不含传输层）」** ⇒ 与我独立算出的 **13** 一致 ✓（第一轮它写的是 9 项／4 个守卫，已改对）
- `dist-policy:64-66` 的"数法"行号：`构建 :597,604,611` / `界面自检 :629,637 与缺 smoke 时的 :650` / `8 条守卫 :652-659` / `传输层 :664-692` ⇒ 我按当前 `run-checks.py` 逐一核对，**全部命中** ✓

### 7.3 其它核实到的现状（供留档）
- `src\media\STATUS.md:136-148` 已在 §4.3 标「**已作废 + 真因**」（真因 = 对指向栈变量的 `VT_BLOB` 调 `PropVariantClear`），与代码 `wasapi_backend.cpp:949` 注释「`ZX_ENABLE_PROCESS_LOOPBACK_PROBE` 这种开关（该宏已不控制任何分支）」一致 ✓
- `dist\RELEASES.md:10-17` 已删除 v36 登记行并注明「v37 被发版闸门拦住 → 当前没有语音测试版包」，与 `dist\` 事实（无 v36/v37 zip、v36 在 `build\dist-quarantine-2026-10-06\`）一致 ✓
- `docs\测试清单-共享电脑声音.md:4-6` 已标注 v36 作废、v37 被拦、并说明"直接进主界面"与现行设计一致 ⇒ 审计 §7.3 那条"RELEASES vs 测试清单说法相反"**已解决** ✓
- `build\smoke-ui.ps1` **已被 lead 删除**：依据 `docs\coding-rules-2026-10-06.md:86`「现有实现 `build\smoke-ui.ps1`（PowerShell+UIA）**作废**，改为 Python 版 `build\smoke-ui.py`」⇒ **删除有据**；`git status --porcelain -- build/smoke-ui.ps1` = `D  build/smoke-ui.ps1`（**已 staged 的删除**，可 `git restore` 取回）。副作用：`docs\fix-plan-2026-10-06.md:13` 的 D1 行仍把 `build\smoke-ui.ps1` 当"现有实现"引用 → 成了**悬空路径**，建议补一句"（已删，改用 smoke-ui.py）"。

---

## 8. G 段 · 独立结论与放行条件

### 8.1 各项判定汇总

| 项 | 判定 | 一句话 |
|---|---|---|
| A 反例守卫（4 条） | **通过** | 4 条全部如期 FAIL，全部逐字节还原 |
| B C# ①②③④⑤⑥⑦ | **通过** | 出货线（`src\winui-cs`）内逐条成立 |
| B ⑧ 入口优先级 | **通过** | 界面优先 + 后门仅兜底 + 必写日志 |
| C 页面 ⑨⑩⑪⑫⑬ | **通过** | 语法/死代码/TURN 合并/定时器/无后门 |
| D 引擎 5.1–5.4 | **通过** | 编译 0 错；rate-check/ring-check 通过；7+1 个假开关逐条落地 |
| D 5.5 线程/生命周期 | **未独立证明** | 只核对了代码意图与注释，未做并发压测 |
| E 机制（smoke/run-checks/release） | **通过（2 条观察留档）** | 退出码 2/3、不发键鼠、release 拒绝出包均成立；`--no-click` 会改系统前台锁但**可逆**（已加 `atexit` 兜底，仅硬杀无解）；`run-checks.py dist` 仍跑 0 项（返回 1 不会假绿，Lead 决定留到 S1） |
| F 文档 | **通过（第二轮）** | 第一轮报的 3 处不符已全修，逐条复核正确（§7.2） |
| **G 全量闸门** | **PASS 14/0（第二轮）** | `tmp\release-v37d.log`；产物 313 条目已与 dist 逐字节回读校验 |
| 未闭合项 1（2 秒轮询） | **已关闭** | `MainWindow.xaml.cs:2513-2545` 可取消 + 条件轮询；闲置期 0 条实证（§10.2） |
| 未闭合项 2（设备 0 个） | **已关闭** | 设备枚举改走引擎；`[设置] 设备（引擎枚举）：麦克风 3 个、扬声器 6 个`（§10.2） |
| 未闭合项 3（界面自检红） | **已关闭** | 真因 = 设置浮层布局把开关裁到只剩 6 px；XAML 两行 Grid 修复；真点后 `泵状态：块=18`（§10.1/§10.2） |

### 8.2 最终结论（第二轮）

> **S0 可以出 v37**（**最终**包于 2026-10-06 07:11 产出；两次发版均 `release.py` 退出码 0、守卫 14/14）。
> 依据：① `build\release.py` 守卫 **14/14 全 PASS**（`tmp\release-v37d.log` 与最终 `tmp\release-v37e.log`）；② 产物 `dist\棕仙语音-测试版-v37.zip` 经我**独立** sha256 / 条目数 / 逐字节 / 清单交叉校验（313 条目、**`a167ff47…8ae0`**、`media/call.html` 与源码同字节、无 `.webview2`/`.log`/残留目录、`RELEASES.md:29` 登记行在表内且 sha 前缀一致）；③ 我第一轮报的三条未闭合项**逐条有独立取证地关闭**（§10.2）。
> 第一轮的"不能出"判定**基于当时的事实是正确的**：13/1 时确实不该出包，`release.py` 也确实拦住了（闸门机制有效）。第二轮把那一红查明为**真产品布局缺陷**（不是测试写错），修完后全绿 —— 这条路径本身证明闸门有价值。

### 8.3 交付后仍未独立证明的一件事（**非阻塞**，建议列入下一轮/S5）
- **引擎线程/生命周期**：`zx_source_close` 与 `zx_source_read` 互斥、"无释放后使用"这条我只核对了代码意图，**没有并发/压力测试证据**（`nstest` 等未跑）。

---

## 9. 取证可复核性（怎么复查本报告）

- **反例脚本**：已删除（`build\zz-probe.py`、`dist\winui\received\zz.bin`、`tmp\zz-*` 全部清理，`tmp\zz*` 残留计数 = 0）。
- **被改文件的最终哈希**：`build\build-media.cmd` = `4571BC52…F95A`（= 本条基线）、`build\smoke-ui.py` 经历三版：`7EE8CC28…75E5`（A4 反例前）→ `EEC4270B…`（第一轮 1C 被验版）→ **`AFD8C7CF…`（38,020 B，07:05:40，第二轮被验版）**；`build\run-checks.py` = `12E163F0…`（06:17:18，两轮未变 ⇒ 本报告引用的行号对两轮都成立）。
- **闸门日志**：第一轮 `tmp\release-v37c.log`（235 行，13/1）；第二轮 `tmp\release-v37d.log`（255 行，21,666 B，07:09:21，14/0）；**最终 `tmp\release-v37e.log`（256 行，07:11，14/0）**。注意：本轮 `build\release-v37.log` 等曾在我读取前被移走（`release-v37*.log` 现均在 `tmp\`）——若复查时找不到，请以 `tmp\release-v37*.log` 为准。
- **应用现场日志**：`tmp\smoke-ui.log`（三轮：06:40 那轮 22 行 = 2 秒轮询 spam 实证；07:00 那轮 17 行 = 设备 0 个 + 开关点击无反应实证；**07:10 那轮 = 三条修复全部生效的实证，见 §10.2**）。
- **我未做的事**（如实声明）：未亲自跑 `run-checks.py all/release`（发真实键鼠，Lead 统一执行，我不抢鼠标）；未做并发/压力测试；未验证 `resources.pri` 之外的应用启动形态；未对 `src\winui` 旧线做功能验证（只做 grep）。

---

## 10. 第二轮复核（2026-10-06 07:09 – 07:13）

### 10.1 闸门（第二轮）：两次发版都是 `汇总: PASS 14 / FAIL 0`
`tmp\release-v37d.log`（255 行，21,666 B，07:09:21）与 **最终** `tmp\release-v37e.log`（256 行，07:11）都是 25 项全 PASS：
```
---- PASS: C++ 助手构建（receiver-probe）
---- PASS: C# 视频原型构建（winui-videoprobe）
---- PASS: C# 主应用构建（winui-cs / 棕仙语音）
---- PASS: 界面自检前置（只验启动+真置顶+前台断言，不发键鼠）
---- PASS: 界面自检（模拟键鼠：启动→进入→设置；元素必须在屏幕内）
---- PASS: 主应用产物资源索引（resources.pri）
---- PASS: dist 卫生 / 媒体资源与引擎探针版本一致性 / build 脚本名字绑定
---- PASS: 残留规则反例（11 必排 + 7 必留）
---- PASS: 界面自检控件名
---- PASS: RELEASES 表格结构（117 行）
---- PASS: cmd 行尾
---- PASS: 传输层 6 阶段自测
汇总: PASS 14 / FAIL 0
```
界面自检那一步的关键行（07:09 那次）：
```
[PASS] smoke-ui --no-click（置顶成功、前台=我方 pid=29448；未发送任何键鼠）
[  16.4s] 设置面板已打开（判据=SettingsCloseButton）
[  17.7s] ShareAudioToggle：向下滚 1 次后**完整可见** rect=(745, 726, 112, 63)
[  21.0s] 采集泵在跑：块=22 ✓
[PASS] smoke-ui（前台断言：0 次失败，置顶走真 SetWindowPos）
```
对照第一轮同一步：`rect=(745, 864, 112, 10)` → 点击 → `[FAIL] …没有"已开始采集"`。

### 10.2 三条未闭合项逐条关闭（代码事实 + 运行证据，均为我独立取证）

**① 界面自检那一红 = 真产品布局 bug（不是测试写错）**
- 代码事实：`src\winui-cs\src\MainWindow.xaml:503-525` 现为 `SettingsOverlay → Border → Grid(MaxWidth=520, VerticalAlignment=Stretch) → RowDefinitions[Auto,*] → 表头行 + ScrollViewer Grid.Row="1"`；`MaxHeight="820"` **只剩 `:508` 的注释**（`:576` 的 `EngineLogScroll MaxHeight="200"` 是另一个控件）。注释原文即第一轮的现象："开关本体只剩 6 px 在可视区…用户**点不到**它、自检也点不动"。
- 测试侧同步：`build\smoke-ui.py:268-301 scroll_into_view(..., want_h=40, max_scrolls=16)`，判据是"矩形高度 ≥ 40 才算完整可见"；`:745-760` 两个控件都改用它，`:760` 的 `real_click` **不再传 `content_offset_y`**。
- 运行证据：`tmp\smoke-ui.log` 07:10 那轮 —— 开关矩形 `(745, 726, 112, 63)`（第一轮 `(745,864,112,10)`）、`[共享声音] 已开始采集（loopback）`、`泵状态：块=18 字节=345600 退出码=运行中`、`share-audio-debug … "active":true,"pushed":18,"trackState":"live"`。
- **诚实记录**：我第一轮判"更像测试点击没命中"——**方向对（确实点不中），但我没定位到产品根因**（设置浮层溢出窗口下沿把开关裁剩 6 px）。这条是 Lead 用像素取证定性的。

**② C# 永久 2 秒轮询（审计 §五-4）**
- 代码事实：`MainWindow.xaml.cs:2513-2515` 先 `Cancel()` 再 `new CancellationTokenSource()` ⇒ `ready` 重复触发**不会叠加**；`:2516-2545` `while (!pollToken.IsCancellationRequested)` + `Task.Delay(..., pollToken)` + `catch (OperationCanceledException) { break; }`；`:2531 if (_shareAudio is null) return;`（注释说明为何不判 `IsChecked`：后门路径也要覆盖）；`:303 Closed += … Cancel/Dispose`。
- 运行证据（我读应用日志，非引用结论）：`tmp\smoke-ui.log` 07:10 那轮，`07:10:31` 就绪 → `07:10:45` 开设置，**14 秒内 0 条** `share-audio-debug` / `泵状态`（第一轮同区间是每 2 秒一条、共 6 条）；`07:10:48` 开始采集后恢复 `泵状态：块=18 …` ⇒ "闲置不刷、共享时照常"两条都成立。

**③ 设置里设备 0 个（审计真 bug #1 症状）**
- 代码事实：`RefreshDevicesAsync :3330-3336`（**引擎优先 → 页面兜底**）；`RefreshDevicesFromEngineAsync :3339-3412`（起 `zxprobe devices`、UTF-8 读 stdout、`JsonArrays()` 切段、`kind` 取 `mic`/`render`、标签带 `kHz/ch/默认`、`:3401` 写日志、`:3387-3391` 空列表也退回并写日志）；`JsonArrays :3423-3457`（配对括号 + 字符串/转义感知）；`OnDevicesReported :3463-3467`（引擎已填则忽略页面占位项）；`:3483-3494`（页面兜底路径**不再按空 `id` 丢弃**，改为标注"（需先授权）"）。
- 运行证据：`tmp\smoke-ui.log` `07:10:45 [设置] 设备（引擎枚举）：麦克风 3 个、扬声器 6 个`，并逐条列出真名（含 `麦克风 (Steam Streaming Microphone) · 44.1kHz/1ch`）。
- **我的独立复现（不依赖 GUI）**：用 Python 直接跑 `dist\zxprobe.exe devices`（exit 0，1,549 B）→ ① 整段 `json.loads` **失败**；② 按 `JsonArrays` 同算法配对括号切出 **2** 段 → **3 mic / 6 render**，标签与日志逐条一致。
  *（诚实说明：我的复现报 `JSONDecodeError: Expecting value: line 1 column 1`，Lead 报的是 .NET 的 `'0xE6' is invalid after a single JSON value`；**报错文本不同、结论同一**——整段不可直接解析，必须切段。）*

### 10.3 最终产物独立校验（都是我自己算的）
| 项 | 我算出来的值 |
|---|---|
| 文件 / 大小 / mtime | `dist\棕仙语音-测试版-v37.zip` / **34,207,545 B** / 2026-10-06 07:11:09 |
| **sha256** | **`a167ff47b71041dd34605310f613c105accb0754aeb7b5ac14236ba131bf8ae0`** |
| 条目数 | **313**（目录条目 0） |
| `media/call.html` | zip 142,147 B == 源码 142,147 B（`bytes_equal=True`） |
| 探针 | zip 内 `tools/zxprobe.exe` == `dist\zxprobe.exe`（193,536 B） |
| 资源索引 | zip `resources.pri` == `dist\winui\resources.pri` |
| `BUILD-INFO.txt` | `commit: 63213cb`（== HEAD）/ `built: 2026/10/06 07:10:13` / `sources: src/winui-cs, src/media` |
| 残留 | **无** `.webview2`、无任何 `.log`、无 `received/ recordings/ sent/ incoming/` |
| 清单 | `dist\RELEASES.md:29` 登记 `棕仙语音-测试版-v37.zip | 32.6 MB | 2026-10-06 07:11 | sha256 a167ff47b71041dd | 313 条目`，**行在「发布物」表内**（前后皆为表格行） |

*自查纠错*：我第一轮脚本曾把 `.webview2` 报成"包内命中"，实际是匹配到了 `Microsoft.Web.WebView2.Core.dll` —— **是我的误报，已纠正**（真 `.webview2` 目录在 `dist\winui` 有、在包里没有）。

### 10.4 第二轮新增观察（非阻塞，供下一轮）
1. `build\smoke-ui.py:129 TOGGLE_CONTENT_OFFSET_Y = 22` 已成**死常量**（全文仅此一处），`:304 locate_scrolling` 成**死函数**（无调用点，被 `:268 scroll_into_view` 取代）。仓库没有"死常量/死函数"守卫，`check_script_sanity` 只查"用了但没绑定"。
2. `MainWindow.xaml.cs:2531 if (_shareAudio is null) return;` 在 `DispatcherQueue` 回调里读字段，与 `StartShareAudioAsync` 的赋值有**无害竞态**（最坏漏一拍统计）；不需修，属"未加锁共享状态"。
3. `_devicesFromEngine` 每次刷新先复位（`:3341`）⇒ 若引擎某次失败，下拉会退回页面的占位项（名字变差）。行为有日志、不是静默失败，但值得留意。
4. `.gitattributes`（`*.cmd text eol=crlf`，sha `3BCCFB8B…`）+ `check_cmd_line_endings()` 构成行尾**双保险**；本轮 7 个 `build\*.cmd` 实测 CR=LF、tail CRLF=True。

### 10.5 关于"第二次发版"与你附注里那条缺陷（**我第一轮没覆盖到，如实标注**）
- Lead 在 07:11 修了 `release.py` 的一处**登记缺陷**：旧实现把 v37 登记行 append 到 `dist\RELEASES.md` **表外**，被既有守卫 `check_releases_manifest_shape()` 抓成"孤立表格行"；修完重跑发版 ⇒ 最终包换成 `a167ff47…8ae0`。
- 我独立复核这一点：① 最终日志 `tmp\release-v37e.log` = `汇总: PASS 14 / FAIL 0` + `已登记：棕仙语音-测试版-v37.zip（写进「发布物」表；sha256 与条目数都在里头）`；② **我自己跑** `check_releases_manifest_shape()` → `---- PASS: RELEASES 表格结构（117 行）`；③ `RELEASES.md:29` 的登记行前后都是表格行 ⇒ **确在表内**。
- **诚实标注**：这条缺陷**不在我第一轮的发现清单里** —— 我当时的 F 段只做"文档 vs 代码"抽查，**没有对 `release.py` 的"写清单"逻辑造反例**。抓到它的是仓库**自己的守卫**，这正面印证了"守卫比人可靠"（也让 §2 的反例测试结论更有分量：守卫是有效的，代价是覆盖面取决于规则写得多细）。

---

## 附注（Lead 追加，2026-10-06 07:12）

> *（复核者说明：本节为 Lead 原文，保留不改；其中"§8.2 引用 fd1befa4…"指的是本报告修订前的版本——我已在 §0/§8.2/§10.3 把 sha256 更正为最终包 `a167ff47…8ae0`，并在 §10.5 独立复核了这条登记缺陷的修复。）*

- 本报告 §8.2 引用的 `fd1befa4…2319` 来自**第一次**成功发版（07:09）。随后修了 `release.py`
  的一处登记缺陷（旧实现把登记行 append 到 `dist\RELEASES.md` **表外**，`check_releases_manifest_shape()`
  当场抓到"孤立表格行"），并**重跑一次发版**验证该修复，所以最终交付物是第二次的包：
  - `dist\棕仙语音-测试版-v37.zip`，**313 条目**，32.6 MB，07:11
  - sha256 `a167ff47b71041dd34605310f613c105accb0754aeb7b5ac14236ba131bf8ae0`
  - 登记行已在「发布物」表**表内**；`check_releases_manifest_shape()` = PASS
  - 两次发版都是 `release.py` 退出码 0、守卫 14/14；第二次日志 `tmp\release-v37e.log`
- 报告 §8.3 那条"引擎线程/生命周期未独立证明"仍然成立、**不阻塞本次交付**，已列入 S5 待办。
