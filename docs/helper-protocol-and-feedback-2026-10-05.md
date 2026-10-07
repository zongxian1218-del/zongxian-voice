# 应用 ↔ 助手：协议与状态反馈（2026-10-05 第三轮）

## 一、先修一个"看不见"的坏功能：应用状态栏一直是死的

**现象**：画面已经正常显示、助手每秒都在报解码帧数，但应用底栏永远停在"助手已启动，等待推流…"。

**逐层排除（每层都有实测证据）**：

1. 应用原来靠 `a.Data.Contains("已解码")` 匹配助手的中文日志 → 永远不中。
2. 加了 stdout 原始记录后看到**真正到达应用的文本**是：
   ```
     [ 1.0 event stats recv=844 assembled=36 decoded=21 presented=21 lat=3
   ```
   即：**中文那一行被截断、连它的换行都没写**，下一条 ASCII 事件被粘到了同一行末尾。
   所以换成 `StartsWith("event stats ")` 也一样不中。
3. **根因**：`receiver-probe.cpp` 的 `Log()` 用 `vwprintf(fmt, ap)` + `fflush(stdout)` 写标准输出。
   日志**文件**是用 `"w, ccs=UTF-8"` 打开的（所以文件里的中文一直是对的），
   但 **stdout 不是 Unicode 流** —— `vwprintf` 按默认 "C" 区域做宽→窄转换，**一遇到中文就失败**，
   整行截断、换行丢失。

**修法**（`src/media/probe/receiver-probe.cpp` → `Log()`）：stdout 这一路自己
`WideCharToMultiByte(CP_UTF8, …)` + `fwrite`；日志文件那一路保持 `vfwprintf` 不动。

**验证**（用 UI Automation 读真实控件文字，不靠 OCR、不靠截图）：

```
[ControlType.Text] 接收中  recv=22417 assembled=1001 decoded=965 presented=965 lat=4
```

`presented == decoded` → 解码出来的每一帧都上屏了。

---

## 二、当前协议

**stdin（父应用 → 助手）**

| 命令 | 含义 |
|---|---|
| `rect x y [w h]` | 移动窗口；带 `w h` 时同时改窗口尺寸（**不动 swapchain**，DXGI 负责拉伸）。命令**自带 `SWP_SHOWWINDOW`** —— 所以从最小化还原时只要再发一次 `rect` 就显示回来了 |
| `hide` | 隐藏覆盖窗口。父应用最小化时用（顶层窗口不会跟着应用一起最小化，不藏就会在屏幕上留一块"孤儿"画面） |
| `quit` | 退出 |

**stdout（助手 → 父应用）：只认 ASCII 事件行**

| 事件 | 何时发 | 用途 |
|---|---|---|
| `event view-ready hwnd=<十进制>` | 窗口创建后 | 应用知道助手就绪 |
| `event stream-size <w> <h>` | 第一帧解码后 / 分辨率变化时 | 应用按真实宽高比重算 letterbox |
| `event stats recv=… assembled=… decoded=… presented=… lat=…` | 每秒 | 应用状态栏 |

中文日志行仍会写 stdout（现在编码是正确的），但 **应用只允许匹配 `event ` 开头的行**——
**不要再用中文子串匹配**，那正是状态栏一直不刷新的原因。

---

## 三、顺带修掉的一个真问题：后台缓冲尺寸不符会裁掉画面

- 解码器输出的是**码流本身的分辨率**，而渲染器的后台缓冲是按"启动时传入的预期尺寸"建的。
- 于是：助手按 1280x720 启动、实际推来 1920x1080 时，`CopyResource` 只会拷到左上角一块 —— **画面被裁**。
- 修法（`src/media/render/d3d11-renderer.h`）：`PresentBgra(data, pitch, srcW, srcH)`，
  发现源尺寸与后台缓冲不符就 `Resize` 重建；**窗口尺寸不受影响**（DXGI 拉伸负责显示尺寸）。
- 实测：1920x1080 流进"按 720p 配置"的助手，抓图 mean 207 / 非黑 89.7%，画面完整。

---

## 四、会话/流身份：**已解决**（2026-10-05 同日）

**当时的实测现象**：把 720p 发送端换成 1080p 发送端（应用与助手都不重启）之后：

```
[63.4 秒] 收包 39754  已重组帧 1192  已解码 1156  本秒解码 0   ← 包还在涨
[69.5 秒] 收包 43663  已重组帧 1192  已解码 1156  本秒解码 0   ← 帧数冻住
```

**根因**：重组器把 `frameId < expectedFrameId_` 的包当成"跨帧迟到的旧分片"丢弃
（这条规则本身是对的，见 `transport-verified-2026-10-05.md` / 交接文档 §7.3），
而**新发送端的帧号从 0 重新开始** → 整条新流被判成陈旧数据，一帧都进不来。

**修法**（已实施）：
- `media-packet.h`：包头里原本 `reserved0[3]` 空着，现在用其中 2 字节做 **`uint16_t sessionId`**
  （**不扩容、不改 `kVersion`**，仍是 32 字节，静态断言没动）。
- 发送端每次运行生成一个随机 `sessionId`（`sender-probe.cpp`，`GetTickCount64 ^ pid`）。
- 重组器在 `OnPacket` 里发现 `sessionId` 变化 → `Reset(keepStats=true)`，
  清掉半成品帧与帧号期望，**保留累计统计**（否则每秒日志里的帧数会突然归零，看起来像故障）。
- 助手日志与状态事件都带上会话信息：`[信息] 检测到新会话 sessionId=0xXXXX（第 N 次），重组器已重置`，
  `event stats … sessions=N`。

**验证（两层，都有实测）**：
1. `transport-probe` 新增**阶段 6「中途换会话(帧号重来)」**：前 30 帧会话 A、第 30 帧起换会话 B 且帧号从 0 重来。
   连跑两次都是 `通过   交付 60/60，内容错 0`，并打出 `会话切换 1 次（检测到新会话 → 重组器已重置，累计统计保留）`。
2. **真实场景复验**（应用 + 助手不重启，中途把发送端从 720p 换成 1080p 的新进程）：

   | | 切换前（720p） | 切换后（1080p，帧号从 0 重来） |
   |---|---|---|
   | `stream` | 1280x720 | **1920x1080** |
   | `sessions` | 0 | **1** |
   | 已重组 / 已解码 | 247 / 189 | **306 / 248**（继续增长） |
   | 抓图 | — | mean 218.40 / 非黑 **96.8%** |

   助手内部日志：`[信息] 检测到新会话 sessionId=0x275B（第 1 次），重组器已重置` → `[信息] 流分辨率 1920x1080`。

> 仍存在的相关缺陷：`transport-probe` **阶段 3「每帧丢 1 片 + 校验」间歇性失败**（与本次改动无关，
> 见 `versioning-and-checks-2026-10-05.md` §4）。

---

## 五、验证方法（已固化到仓库，别再放 tmp/）

| 工具 | 用途 |
|---|---|
| `build/grab-region.py <x> <y> <w> <h> out.png` | 抓图 + 亮度统计；**非黑率是"画面到底出来没有"的客观判据** |
| `build/inspect-windows.ps1` | EnumWindows 列 z 序 / 可见性 / 置顶 / 矩形（诊断"窗口建了却看不见"） |
| UI Automation | 读应用状态栏等真实控件文字，比截图可靠 |

`inspect-windows.ps1` 需要先 `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`。
