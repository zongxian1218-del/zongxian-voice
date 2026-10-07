# P1-6 第一片：原生视频接进主应用（含我引入的两个 bug）（2026-10-05）

> **【2026-10-06 更正】** 本文里几处「`run-checks.py` 5 项全 PASS」是**当时**（2026-10-05）的事实。
> 现在 `build\run-checks.py` 是 **25 项**（`release` 模式修复前 9 项），以脚本实际输出为准
> （数法见 `build\run-checks.py:421,450-461,477-480,482`）。

> **结果**：`ZX_NATIVE_VIDEO=1` 时，主应用 `src/winui-cs` 的画面区显示**原生 C++ 助手**的视频，
> 覆盖窗口**紧贴主窗口正上方**，不崩、不刷屏。验收证据见第三节。
>
> **代价**：这一片我**自己引入了两个 bug**，都在这份文档里如实记录（一个让应用直接崩、一个让窗口刷屏）。

---

## 一、做了什么

| 位置 | 内容 |
|---|---|
| `src/winui-cs/src/NativeVideoHost.cs`（新增） | 把原型里已验证的覆盖窗口逻辑抽成可复用类：助手查找顺序、按流宽高比 letterbox 算屏幕矩形、最小化发 `hide`、stdout 只认 ASCII 事件行 |
| `src/winui-cs/src/MainWindow.xaml.cs` | **默认关闭**的接线：`ZX_NATIVE_VIDEO=1` 才启用；关着时行为与原来完全一致 |
| `src/media/probe/receiver-probe.cpp` | 新增 `anchor <hwnd>` 命令：把覆盖窗口插到 **应用主窗口正上方** |

### 为什么是"锚点"而不是 TOPMOST

实测这个覆盖窗口拿不到 `WS_EX_TOPMOST`（详见 `winui-overlay-verified-2026-10-05.md` 的后续一节），
而产品真正需要的是"**视频盖在自己应用窗口之上**"，不是"盖住屏幕上所有窗口"。
所以主应用把自己的主窗口句柄发给助手，助手用
`SetWindowPos(覆盖窗口, 主窗口, …)`（= 插到主窗口那一层的正上方）来定位。

---

## 二、我自己引入的两个 bug（都实测复现、都修好并验证）

### bug 1：跨线程回调让应用直接崩（`0xe0434352`）

抽 `NativeVideoHost` 时，**把原型里的 `DispatcherQueue.TryEnqueue` 弄丢了** ——
助手 stdout 的 `OutputDataReceived` 回调跑在**线程池线程**上，我直接在回调里动了状态/回调应用代码。

证据链：

```
事件日志：Faulting module: KERNELBASE.dll   Exception code: 0xe0434352（托管异常）
判别实验：不带 ZX_NATIVE_VIDEO 跑 → 完全正常、有窗口、无崩溃
          带 ZX_NATIVE_VIDEO 跑   → 启动即崩
```

修法：类里持有 `_area.DispatcherQueue`，所有 stdout/Exited 回调先 `TryEnqueue` 切回 UI 线程再动状态。

### bug 2：`LayoutUpdated` 造成 `rect` 刷屏（同一个矩形发了 25 次）

我在 `NativeVideoHost` 里多挂了一个 `_area.LayoutUpdated += (_, _) => Sync()`。
它在**每次布局**都会触发，导致同一个矩形反复发命令、反复 `SetWindowPos`、反复写诊断文件。

证据：

```
修复前：rect 命令行数 = 25（全部是 (632,242)-(1304,620) 672x378）
       并伴随黑屏症状：抓图 nonblack = 50.1%
对比：已验证能出画面的原型只挂 _appWin.Changed + VideoArea_SizeChanged（没有 LayoutUpdated）
修复后：rect 命令行数 = 1，抓图 nonblack = 78.5%
```

修法：去掉 `LayoutUpdated`；并给 `Sync()` 加**矩形去重**（没变就不发），`Start()` 时用 `force: true`。

---

## 三、验收证据（全部实测）

| 项 | 结果 |
|---|---|
| 崩不崩 | ✅ 事件日志无崩溃（修 bug 1 之前是启动即崩） |
| 助手是否在解码上屏 | ✅ `presented=1343`、30 fps、解码延迟 1~5 ms |
| 覆盖窗口位置 | ✅ 助手日志 `rect 命令：窗口 -> (632,242)-(1304,620) 672x378`，与应用 `nativevideo-pos.txt` 算出的显示区**逐位一致** |
| **锚点是否生效** | ✅ z 序：`ZxPreviewWnd z#21` 紧邻 `WinUIDesktopWin32WindowClass z#22`（应用主窗口）—— 覆盖窗口**正上方**，且没有被抬到压住别的程序窗口 |
| **画面是否真的出来了** | ✅ 按显示区抓图：`nonblack(>=18)=78.5%`，图里能看到 DSH 界面、浏览器、聊天窗口（即发送端采集的桌面） |
| rect 命令是否刷屏 | ✅ 1 条（修 bug 2 之前 25 条） |
| 回归 | ✅ `run-checks.py` 5 项全 PASS（含 `resources.pri` 守卫） |

---

## 四、第二片：原生视频跟随"画面区可见"起停（2026-10-05 晚）

上一片是"启动就开 + 用环境变量强制画面区可见"，这一片把它接到**真实收口点**上：

| 改动 | 说明 |
|---|---|
| `SetScreenViewVisible(visible)` | 这是"画面区可见性"的**唯一收口点**（开始共享、观看对方、点浮层停止都会经过它）。在这里 `visible ? EnsureNativeVideoStarted() : StopNativeVideo()` |
| `OnShareScreenClickedAsync` | 它为了先放大宿主而**绕过**了 `SetScreenViewVisible`，所以在那里补一次 `EnsureNativeVideoStarted()` |
| 懒启动 | `EnsureNativeVideoStarted()` 幂等；元素还没 `IsLoaded` 时挂一次 `Loaded` 再启动（直接启动会因 `TransformToVisual` 抛异常，实测踩过） |
| 环境变量 | 仍作开关（`ZX_NATIVE_VIDEO=1`），等接了真实共享流程再做成界面设置项 |

### 验收证据

**① 不触发共享时不启动**：带 `ZX_NATIVE_VIDEO=1` 单独启动应用 → `receiver-probe` 进程数 **0**（不再是启动就开）、无崩溃。

**② 走收口点时随共享起、随停止停**：

```
显示画面区期间：receiver-probe UP pid=8784      ✅ 随共享启动
隐藏画面区之后：receiver-probe 已停止            ✅ 随之停止
事件日志：无崩溃                                ✅
```

**③ 验收旗标**：`--native-video-test <秒>`（默认 20）显示画面区 N 秒后隐藏。
**为什么要这个旗标**：真实触发者是「开始共享」和「观看对方」，而这两条都要求已和第二个对端建立聊天
（`_chatReady`）——单实例跑不起来（实测 `--share-real` 因为"聊天未连接"直接跳过）。
这个旗标只替换"**谁来让它可见**"，走的是同一个 `SetScreenViewVisible`，
所以原生的起停逻辑是**真实被验证的**，不是另起一条测试专用路径。

**④ 回归**：`run-checks.py` 5 项全 PASS。

---

## 五、第三片：开关做成界面设置项（2026-10-05 晚）

环境变量不是可交付形态，所以加了界面开关：

| 改动 | 说明 |
|---|---|
| `MainWindow.xaml` | 工具栏共享按钮组里加 `ToggleButton x:Name="NativeVideoToggle"`（紧凑，和旁边的按钮风格一致） |
| `NativeVideoEnabled` | 改成**界面开关或环境变量**（`NativeVideoToggle.IsChecked == true \|\| ZX_NATIVE_VIDEO=1`）—— 环境变量留着给脚本化验收用 |
| `OnNativeVideoToggled(bool)` | 开：画面区已可见就立刻起助手，否则记一行"等它显示时再启动"；关：停掉 |

### 验收：用真实点击（UI Automation + SendInput）

**为什么不用点击坐标硬编码**：控件位置随布局变，硬编码坐标一改布局就失效。

**踩到的坑**：`AutomationElement.FindAll(Descendants)` 在这个 WinUI 应用上**枚举不全**（只返回 20 个元素，
连开关都看不到）。改用**递归原始视图遍历**（`TreeWalker.RawViewWalker` 深度优先）
按 `AutomationId` 找，一次命中 —— `NativeVideoToggle` 在 (998,879) 80x32，且支持 `TogglePattern` ✓。

**实测结果**：

```
① 开关默认关闭（画面区已显示）      receiver-probe = 0     ✅
② 真实点击 → 开关 On                receiver-probe = 1     ✅（界面开关真能起助手）
③ 再点一次 → 开关 Off               receiver-probe = 0     ✅
④ 事件日志                          无崩溃                 ✅
⑤ 应用日志                          [原生视频] 界面开关 → 关 / 原生视频助手已退出 / [原生视频] 已停止 ✅
```

回归：`run-checks.py` 5 项全 PASS。

---

## 六、第四片：两实例联调 —— 真实触发者（2026-10-05 晚）

前三片都是单实例验的，触发者是"验收旗标"。这一片跑**两个真实实例**，让触发者是应用自己的事件。

用法（应用自带的自测 + 合成共享，**不弹系统选择窗口**）：

```
A 房主: ZongxianVoice.exe --selftest-host --port 45910 --room nvtest
          --log-file tmp\A-host.log          （ZX_NATIVE_VIDEO=1, ZX_NATIVE_VIDEO_PORT=41101）
B 加入: ZongxianVoice.exe --selftest-join --port 45911 --room nvtest
          --signal ws://127.0.0.1:45910/signal --log-file tmp\B-join.log
                                              （ZX_NATIVE_VIDEO=1, ZX_NATIVE_VIDEO_PORT=41102）
```

`ZX_NATIVE_VIDEO_PORT` 是**给脚本化测试用的**端口覆盖：两个实例的助手都绑 41001 会冲突
（第二个绑定失败直接退出），双机联调就跑不了。产品默认仍是 41001，界面上不暴露。

### 实测证据

| 端 | 原生视频随共享起停 | 锚点 | TOPMOST |
|---|---|---|---|
| A 房主（助手口 41101） | 起 → **停 14:18:05** ✅ | `收到锚点 0xA229AC` ✅ | ❌ 失败（3 次重试，`0x08000080`） |
| B 加入（助手口 41102） | 起 14:18:26 → **停 14:18:32** ✅ | `收到锚点 0x8926E4` ✅ | ✅ **首次即生效**（`0x08000088`） |

- 两端都 `对端=1`（链路真建立），事件日志**无崩溃**，两个助手分别绑 41101/41102 无冲突。
- 触发链：应用页面报「屏幕共享已开始」→ 应用 `SetScreenViewVisible(true)` →
  `EnsureNativeVideoStarted()` → 助手启动；共享停止 → `SetScreenViewVisible(false)` → 助手退出。
- **注意触发语义**：自测的合成共享让**两侧都进入"我在共享"状态**，所以这次走的是
  「自己共享」这个触发者（页面事件 → 收口点）。「观看对方」走的是**同一个收口点**（代码同路径），
  但那个具体事件这次没有被走到 —— 不夸大。

### 顺带拿到的一条 TOPMOST 新证据

同一次联调、同一份代码：**A 的覆盖窗口拿不到 TOPMOST（3 次重试全失败），B 的首次就成功**。
这进一步支持"**是时序/窗口状态相关，不是窗口样式或调用序列**"（那两种假设已用 9 变体对照实验排除）。
详见 `winui-overlay-verified-2026-10-05.md`。

---

## 七、第五片：「观看对方」这条链也走通了（2026-10-05 晚）

上一片走到的是「自己共享」。这一片换打法把**「观看对方」**也走通：

```
A（共享方，跑自测）: --selftest-host --port 45920 --room viewtest      （助手口 41201）
B（观看方，普通加入）: --port 45921 --room viewtest --signal ws://127.0.0.1:45920/signal
                      —— 不带自测旗标，所以 B 不会去共享，只会看 A 的画面（助手口 41202）
```

B 的日志（`tmp/B-viewer.log`）给出了这条链的完整证据：

```
14:21:37  原生视频助手已启动（显示区 2479,230 1x1，锚点 0x762A92）
14:21:37  [原生视频] 已启动
14:21:37  [按钮] 对端=1 通话中=True 共享中=False 看对方=True   ← ★ 这就是「观看对方」
14:21:37  [信息] 收到锚点：覆盖窗口将插到窗口 0x762A92 正上方
14:21:44  [界面] 对端画面已清除，隐藏共享显示区（ScreenView → Collapsed）
14:21:45  原生视频助手已退出 / [原生视频] 已停止
```

**结论：两种真实触发者（自己共享、观看对方）都验证过了，走的是同一个收口点 `SetScreenViewVisible`。**

### 顺带修掉日志暴露的一个真毛病：第一次 `rect` 是 1x1

上面那行 `显示区 2479,230 1x1` 是 bug：观看方是页面事件驱动显示画面区，
那一刻 `ScreenView` 刚被设为 Visible、**布局还没量到尺寸**，于是算出的矩形是 1x1，
覆盖窗口被瞬间设成 1x1（虽然同一个时刻又被纠正成 672x378，但闪一下不该有）。

修法（`NativeVideoHost.Sync()`）：**退化矩形（任一边 < 8px）直接不发**，记一行"布局未就绪"，
等布局完成后的 `SizeChanged` 再同步。回归 3 轮全绿。

---

## 八、联调 3 发现的两个问题（2026-10-05 晚）

这一轮本来只想判定"TOPMOST 是角色问题还是启动顺序问题"（做法：一个房主 + 两个**角色相同**的加入方），
结果抓到两个更值钱的东西。

### 问题 1（真产品缺口）：中途加入的人**看不到正在进行的共享**

证据（`tmp/X-first-join.log`，X 是在房主开始共享**之后**才连上的）：

```
14:27:48  信令已连接
14:27:48  [按钮] 对端=2 通话中=False 共享中=False 看对方=False    ← 从未进入"看对方"
14:27:57  [界面] 对端画面已清除，隐藏共享显示区（ScreenView → Collapsed）   ← 却收到了"停止"
```

对照 Y（在共享开始**之前**就位）：`看对方=True` → 原生视频启动 → 正常观看。

原因（**已定位到代码路径，但"为什么晚加入拿不到轨道"只是推断**）：
`remote-screen-started` 是**观看端在远端轨道到达时本地发出的一次性事件**（`call.html` 里 `post({type:'remote-screen-started'})`），
**没有"当前共享状态"的补发/同步**；共享端是在共享那一刻把屏幕轨道挂到 `state.pc`
（`replaceTrack` / `addTrack`，见 `call.html` 行 267-286），所以之后才加入的对端不会自动拿到这条轨道。
→ 这个要动**媒体引擎的多人共享架构**（属于"真实采集源/多人共享"那块），
按项目规矩**先定设计再动**，本轮只记录不改。

**顺带修掉一个误导性日志**：从没观看过的实例收到 `remote-screen-stopped` 时，
原来会照样打"对端画面已清除"并走一遍隐藏流程（日志里看起来像"曾经看到过画面又没了"）。
现在改成 `if (_viewingRemoteScreen)` 才走，否则记一行"本端本来就没在看画面，忽略"。

### 问题 2：TOPMOST 的角色相关性又多了两组样本

房主 4/4 失败、加入方 4/4 成功（见 `winui-overlay-verified` 的后续一节）。
"启动顺序"这个变量这次**没能干净地验掉** —— 因为后起的那个加入方根本没进观看状态（问题 1）。

---

## 九、联调 4：查清"中途加入看不到共享"的根因 —— **媒体是一对一（单 PC）**

### 根因（代码 + 日志双向确认）

```js
// call.html
async function ensureLink(remoteId) {
  if (state.pc) return true;      // ← 已经有 PC 就直接"成功"返回
```

- 页面里 `state.pc` / `state.remoteId` **都是单数**：一条 PeerConnection 只服务一位对端。
- 房间的成员列表（`state.peers`）是**多人的**，所以应用侧会显示"对端=2"，按钮也可点 ——
  但第二位及以后的成员**拿不到媒体与聊天通道**。
- 四个 `ensureLink` 调用点全都传 `first.id`（第一位对端）。
- 结论：**"中途加入看不到共享"不是补发状态能解决的，是 1:1 媒体架构的直接后果。**

### 这一轮修掉的两个真 bug

**bug A（基础设施级，影响面比看上去大）：`media/` 从来没随构建部署过**

`ZongxianVoice.csproj` 里没有 `media/` 的任何条目 → 输出目录里那份 `call.html` 是**手工拷的旧文件**。
实测对比：源 91,786 B（14:32）vs 产物 90,924 B（01:18）→ **改了页面根本不会生效**。
修法：csproj 加 `<Content Include="media\**" CopyToOutputDirectory="PreserveNewest" />`，
构建后源/产物 MD5 一致 ✅。
（这条要记牢：以前任何"改了 call.html 却没效果"的现象都可能是这个原因。）

**bug B：`onOffer` 无条件把任何 offer 当"同一位对端的重协商"**

```js
if (state.pc) {                                  // ← 没有判断 msg.from 是不是当前对端
  await state.pc.setRemoteDescription(...)       // 于是第二位对端的全新 offer 被塞进第一位对端的 PC
```

后果不只是"第二个人看不到"，**还可能把第一位对端那条正常链路弄坏**。
实测到的症状：房主侧 `链路状态: connecting`，而加入方侧显示 `connected`（两边说法不一致）。

**修法**：在 offer 路径上按对端身份判一下，不同对端就忽略并上报应用（`link-busy`）；
应用侧新增 `link-busy` 处理，明确写日志 + 更新提示文字（不再静默）。

### ✅ 已解决（同一轮内）：信令中继**根本不带发送者 id**

**先加诊断、再下结论**（教训：上一版守卫是靠猜写的，所以没触发）。
诊断走 `post({type:'signal-debug'})` —— **页面的 `log()` 只写页面自己的 DOM**
（`const log = s => { out.textContent += s }`），应用侧根本看不到，所以第一版诊断白打了。

```
修复前:  [信令] offer from=undefined to=1942c294 键=type,to,sdp,purpose
         [信令] answer from=undefined to=undefined 键=type,sdp          ← 只能广播
修复后:  [信令] offer from=0c444e8e to=77319925 键=from,type,to,sdp,purpose
         [信令] answer from=77319925 to=0c444e8e 键=from,type,to,sdp   ← 变单播
```

**根因**：`SignalingServer.cs` 的中继把消息**原样转发**，从不补发送者；页面发的信令只有
`{type,to,sdp,purpose}`。三条后果一次说清：

1. 接收端无法判断"这条 offer 属于哪位对端"→ 我上一版守卫的条件永不成立（**这是它没触发的原因**）；
2. 应答方 `sendSignal({type:'answer', to: msg.from})` 的 `to` 也是 undefined → answer **广播给全房间**
   → 多余的 answer 落到不期待的端上 → `setRemoteDescription ... Called in wrong state: stable`；
3. 房主无法区分"同一位对端的重协商"和"另一位对端的新连接"。

**修法**：`SignalingServer.cs` 新增 `AddFrom()`，转发前把 `from = 发送者 id` 注入 JSON
（解析失败就原样转发 —— 不能因为加字段把信令本身弄坏）；
`onAnswer` 另加"信令状态不是 `have-local-offer` 就丢掉"的守卫 + try/catch。

**验证（三实例 H6/X6/Y6）**：

```
X6: [信令] offer from=09b13bd3 to=0c444e8e 我方remoteId=77319925
X6: [链路] 当前版本同一时间只与一位对端建立音视频：已连着 77319925，
           不与会话 09b13bd3 另建链路（房间其他成员只能看到在线状态）
```

- 守卫**这次真的触发了**，并把"不同对端被忽略"如实报给应用 ✅
- 三份日志里 `未处理拒绝` / `wrong state` 共 **0 条**（修复前 H3 有一条）✅
- answer 从广播变单播 ✅；回归 5 项全 PASS ✅

### 建议（需要你定方向）

| 方案 | 内容 | 成本 |
|---|---|---|
| **A 诚实化（推荐先做）** | 保留 1:1，但界面/日志明确"同一时间只与一位对端建立音视频"，并修掉上面的 bug B 与未处理拒绝 | 小 |
| **C 修 1:1 下的重连** | 对端重连（同一个 id 重新 offer）时替换掉死掉的 PC —— "我重连后看不到对方屏幕"是真实用户路径 | 中小 |
| **B 做 mesh（每个对端一条 PC）** | 页面媒体状态从单数改成按对端 Map，共享要扇出到所有对端，界面聚合 N 路远端画面 | 大（架构级） |
| D 上 SFU / libdatachannel | 与既定路线（不用 libwebrtc 整包）冲突，不建议 | — |

**该由你决定的是 B**：产品到底需不需要"3 人及以上同时看到同一路共享"？
若不需要，A + C 就够了；若需要，B 是一次架构改造，得单独立项。

---

## 十、还没做

1. **真实采集源**：现在助手接的是 `sender-probe`（桌面采集）；正式应用要接屏幕/单窗口采集（含权限流程）。
   这其实是**发送端架构问题**（谁来采集），要先定设计再动。
2. **多显示器 / 负坐标**：letterbox 计算基于 `ClientToScreen`，多屏负坐标还没测。
3. TOPMOST 本身仍未解决（见 `winui-overlay-verified` 的后续一节），但产品需要的那一层已由锚点满足。
   **新线索**：两次联调里"房主总是失败、加入方总是成功"（4/4），下一步用"独立信令 + 两个加入方"判定
   是角色还是启动顺序。
