# 棕仙语音 · 界面元素盘点（2026-10-05）

> **用途**：用户决定"界面整体重做"，开工前先把**现有元素、数据来源、写死内容、缺口**列清楚。
> 本盘点基于 `src/winui-cs/src/MainWindow.xaml`（617 行，全文读完）与
> `MainWindow.xaml.cs`（页面 → C# 共 **48 个事件**，逐条核对）。

---

## 0. 一句话结论

骨架（三栏 + 顶栏 + 通话栏）是**照着 Oopz/Discord 做的、结构合理**；
问题集中在两头：**① 有假数据**（成员列表两行、左栏三张卡、聊天日期）**② 有死按钮**
（中栏顶栏两个图标）**③ 有数据但没用**（`roster` 事件里已经带着每个人的 id/名字，界面只取了人数）。

---

## 1. 窗口骨架

```
Window
└ RootGrid                      2 行：Auto（横幅）/ *
  ├ ControlBanner               远程控制横幅，默认 Collapsed
  └ Grid                        三栏：240px | * | 224px
    ├ 左栏（会话列表）            3 行：48 / * / 56
    ├ 中栏（主区域）              2 行：48（顶栏）/ *
    │   └ Grid                  4 行：画面区(Auto) / 聊天(*) / 输入栏(Auto) / 通话栏(Auto)
    ├ 右栏（成员列表）            2 行：48 / *
    └ SettingsOverlay           v27 新增的设置浮层，默认 Collapsed
```

---

## 2. 元素清单（按区域）

### 2.1 顶部：远程控制横幅

| 元素 | 类型 | 内容 | 数据来源 | 状态 |
|---|---|---|---|---|
| `ControlBanner` | Border | 红底横幅 | 代码控制可见性（`RootGrid.FindName("ControlBanner")`） | ✅ 正常 |
| `ControlBannerText` | TextBlock | "对方正在控制你的电脑" | 代码可改 | ✅ 正常（默认文案写死但会被覆盖） |
| `StopControlButton` | Button | 停止控制 | 有 Click | ✅ 正常 |

### 2.2 左栏：会话列表

| 元素 | 类型 | 内容 | 状态 |
|---|---|---|---|
| 标题 | TextBlock | "棕仙语音" | ✅ 静态标签，正常 |
| 卡片 1 | Border | "和朋友的语音"（高亮） | ❌ **写死**，原型场景卡 |
| 卡片 2 | Border | "晚上看电影" | ❌ **写死** |
| 卡片 3 | Border | "远程帮家人看电脑" | ❌ **写死** |
| 底部自我区 | Grid | 头像"我" + 名字"我" + `MyAddressText` + `CopyAddressButton` | ⚠️ 名字写死"我"（应取设置里的名字）；地址是真实动态 |
| 联机入口 | ComboBox + 3 Button | `RoomListCombo` / `JoinRoomButton` / `PeerAddressBox` / `JoinButton` | ✅ v26 新增，真实动态 |
| `SettingsButton` | Button | 齿轮 | ✅ 打开设置浮层 |

### 2.3 中栏顶栏

| 元素 | 类型 | 内容 | 状态 |
|---|---|---|---|
| 标题 | TextBlock | "和朋友的语音" | ❌ **写死**（应为房间名或对端名） |
| `QualityDot` / `QualityText` | Ellipse + TextBlock | "未连接" | ✅ 动态（连接质量） |
| 图标按钮 ×2 | Button | 共享屏幕 / 共享音频 | ❌ **死按钮**：没有 Click，且与通话栏的真按钮重复 |

### 2.4 共享画面区

| 元素 | 类型 | 内容 | 状态 |
|---|---|---|---|
| `ScreenView` | Grid | 380 高，默认 Collapsed | ✅ 动态 |
| `MediaHost` | WebView2 | 画面本体 | ✅ |
| `ScreenStatusText` / `ScreenDetailText` | TextBlock | "屏幕共享中" / 详情 | ✅ 动态 |
| `ScreenViewStopButton` | Button | 关闭画面 | ✅ 有 Click |

### 2.5 聊天区（`ChatScroll`）

| 元素 | 类型 | 内容 | 状态 |
|---|---|---|---|
| `SelfCheckPanel` | Border | "媒体引擎"卡片 + `EngineStatus` + `EngineLogToggle` + `EngineLogScroll` | ✅ 动态（v27 把日志收进「详情」） |
| `ChatStatus` | TextBlock | "聊天未连接" | ✅ 动态 |
| `FileProgressPanel` | StackPanel | 文件进度条 | ✅ 动态（默认折叠） |
| `MessagesPanel` | StackPanel | 消息气泡，代码 `AddMessage()` 追加 | ⚠️ 代码驱动 ✅，但里面**写死了一个"今天"分隔** |

### 2.6 输入栏 / 通话栏

| 元素 | 类型 | 内容 | 状态 |
|---|---|---|---|
| `AttachFileButton` / `MessageInput` / `SendMessageButton` | Button/TextBox | 发文件 / 输入 / 发送 | ✅ |
| `CallStateDot` / `CallStateText` | Ellipse + TextBlock | "未通话" | ✅ 动态 |
| `StatsRtt/Loss/Jitter/Bitrate/Path/Samples` | TextBlock ×6 | 延迟/丢包/抖动/码率/链路/采样 | ✅ 动态（初值 "—"） |
| `MicLevelBar` | Border | 麦克风电平条 | ✅ 动态（`mic-level` 事件） |
| `ShareScreenButton` / `StopShareButton` | Button | 共享/停止 | ✅ |
| `DenoiseCombo` | ComboBox | 降噪三档 | ✅ |
| `CallButton` / `MuteButton` / `RecordButton` / `HangupButton` | Button | 通话/静音/录音自检/挂断 | ✅（"录音自检"偏测试，建议改名"录一段试听"或移进设置） |

### 2.7 右栏：成员列表 ← **最像假软件的地方**

| 元素 | 类型 | 内容 | 状态 |
|---|---|---|---|
| `MemberCountText` | TextBlock | "成员 — N" | ✅ 动态（只有人数是真的） |
| 成员行 1 | Border | 头像"A" + "朋友A" + 说话高亮描边 | ❌ **写死** |
| 成员行 2 | Border | 头像"B" + "朋友B" + 静音图标 | ❌ **写死** |
| 成员行 3 | Border | 头像"我" + "我" + "共享中"标记 | ❌ **写死** |

> **数据其实已经在手上**：`case "roster"` 里 `peers` 是数组（现在只取了 `GetArrayLength()`），
> 元素里带着每个人的 id/name —— 重建成员行不需要改协议。

### 2.8 设置浮层（v27 新增，结构正常）

我的名字 / 房间名 / 麦克风设备 / 扬声器设备 / 共享系统声音开关 /
「诊断与实验功能」折叠区（检测麦克风、原生视频、单个应用音频、打开日志、复制诊断）/ 关于文字。

⚠️ 其中"检测单个应用音频""打开日志文件夹""复制诊断信息"仍是**测试性入口** ——
重做时应放"关于/诊断"页，而不是和日常设置并列。

---

## 3. 页面 → C# 事件清单（48 个，按用途分组）

| 分组 | 事件 | 是否有对应 UI |
|---|---|---|
| 引擎生命周期 | `engine-loaded` `ready` `engine-error` `js-error` `js-rejection` | ✅ 日志/状态 |
| 信令 | `signal-open` `signal-closed` `joined` `signal-debug` `signal-test` | ✅ 状态文字 |
| 成员 | `roster`（**只用了人数**）、`link-added` `links` `link-replaced` `link-busy` `link-switched` | ⚠️ **成员详情没用起来** |
| 通话 | `call-state` `call-link` `connection-state` `call-error` | ✅ |
| 麦克风/音频 | `mic-opened` `mic-level` `muted` `denoise-applied` `denoise-needs-reconnect` `recording` `selfcheck` | ✅ |
| 屏幕共享 | `screen-started` `screen-stopped` `screen-error` `screen-probe` `screen-frames` `remote-screen-started/stopped/switched` | ✅ |
| 文件 | `file-start` `file-progress` `file-done` `file-received` `file-error` | ✅ 进度条 |
| 聊天 | `chat-open` `chat-closed` `chat-error` `chat-message` | ✅ |
| 统计 | `stats` `stats-debug` | ✅ 6 项统计 |
| 设备（v27 新增） | `devices` | ✅ 设置里的两个下拉 |

---

## 4. 死按钮 / 未接事件

| 位置 | 问题 |
|---|---|
| 中栏顶栏两个图标（共享屏幕 / 共享音频） | **没有 Click** ✗ 且与通话栏重复 |
| `ControlBanner` 的"停止控制" | ✅ 有 Click（核对过） |

---

## 5. 缺口：正常软件该有、现在没有

1. **成员的完整信息**：真实名字、头像首字、说话中/静音/共享中、加入时间
2. **托盘图标 + 最小化到托盘 + 关闭确认**（现在是关窗即退出）
3. **通知**：有人加入、有人共享、文件收到（现在只写日志）
4. **通话可见时长**（现在只有状态文字）
5. **共享源选择**（现在完全依赖系统弹窗；应用里没有"上次共享的窗口"记忆）
6. **首次引导 / 空状态**：左栏三张假卡占了空状态的位置，应换成"还没有房间，扫描中…/ 手动填地址"
7. **成员右键菜单**（静音某人、查看他共享的画面）
8. **网络诊断页**（现在统计挤在通话栏，信息密度过高）
9. **主题/深色模式跟随说明、字号**（现在跟随系统 ✓，但没有开关）
10. **快捷键**（静音 Ctrl+M 等）
11. **关于/版本/更新检查**（现在只有设置里一行字）
12. **错误可读化**：`engine-error` 直接进日志，普通用户看不到"该怎么办"

---

## 6. 重做建议（信息架构）

```
左栏（导航，180~220px）
  ├ 我的状态（头像/名字/设备/麦克风电平）
  ├ 【房间】列表：当前房间（真实）+ 最近加入（真实历史）+ "扫描中…"空状态
  └ 底部：设置 / 关于

中栏（主区，自适应）
  ├ 顶栏：房间名 | 在线人数 | 网络质量 | 设置入口
  ├ 画面区（有人共享时展开，可拖拽高度；无共享时显示空状态卡）
  ├ 聊天/事件混合流（消息、文件进度、系统提示都进这一条时间线，"今天/昨天"按真实日期分隔）
  └ 底部：输入栏 + 通话控制条（发起通话/静音/共享/挂断 + 麦克风电平）

右栏（成员，200~240px）
  ├ "成员 · N 人（我在内）"
  └ 数据驱动行：首字头像 + 真实名字 + 状态徽标（说话中/静音/共享中）
     · 自己固定在最后并标注"我"
     · 右键：静音某人 / 看他共享的画面

设置（右侧滑出面板或独立页）
  ├ 通话：名字、房间、麦克风、扬声器、共享系统声音
  ├ 外观：主题、字号（可选）
  └ 关于/诊断（把自检、日志、诊断、原生视频等实验项都收这里）
```

---

## 7. 重做的约束（必须遵守，否则会踩已踩过的坑）

1. **XAML 编译器在这个环境会静默失败**（只报 `MSB3073`）：
   - 已知根因：**重复的 `x:Name`**（本轮踩过，绕了很久）
   - 改完先用 `tmp\check-dups.py` 查重名，再构建
2. **小步改、每步构建**：一次改一大片会让"哪里坏了"无从定位
3. **不改协议**：成员信息从现成的 `roster.peers` 取，不需要动信令
4. **先做成员列表数据驱动**：这是"假软件感"的最大来源，改完观感立刻不同
