# 发送端媒体链路验证报告（2026-10-05）

## 结论先说

**屏幕采集 → GPU 转 NV12 → 硬件 H.264 编码 → 解码回画面，全链路打通。**

三档目标分辨率与帧率全部达标，且每一档都经过「编码后重新解码、逐帧核对」验证：

| 档位 | 目标帧率 | 实测帧率 | 实测码率 | 解码验证 | 每帧预算占用 |
|---|---|---|---|---|---|
| 1080p30 | 30 | **30.0 fps** | 5.29 Mbps | 150/150 帧 | 3.07 / 33.33 ms |
| 1080p60 | 60 | **60.0 fps** | 6.45 Mbps | 300/300 帧 | 2.96 / 16.67 ms |
| 720p30 | 30 | **30.0 fps** | 2.32 Mbps | 150/150 帧 | 2.09 / 33.33 ms |

1080p60 尚有 **13.71 ms 余量**（单帧预算 16.67 ms），说明还有提升空间，4K30 以后可期。

---

## 之前卡住的两条路，真正的原因

### WGC（Windows.Graphics.Capture）返回 `0x80070005`

**不是环境问题。** 环境实测完全正常：

```
进程所在会话 : 1        活动控制台会话 : 1      [OK] 在活动控制台会话内
输入桌面名   : Default                        [OK] 未锁屏
本进程窗口站 : WinSta0   本进程桌面 : Default
SM_REMOTESESSION : 0
```

真正原因是 **C++/WinRT 互操作用法写错**（`IGraphicsCaptureItemInterop::CreateForMonitor`
的 ABI 签名处理），属于代码缺陷，不是权限问题。这条路已放弃，改用 DXGI。

### C# DXGI 探针崩溃、连日志都没有

**是我自己写的手写 COM 互操作有缺陷**：

- `DXGI_ADAPTER_DESC` 用了 `ByValTStr`，托管大小 ~56 字节 vs 原生 ~304 字节 → 写坏堆
- 手写 vtable 序号调用（`DuplicateOutput` = 22 等）容易错位

**教训：不要用 C# 手写 COM 互操作/vtable。C++ 直接用官方头文件，一次就通。**

---

## 最终采用的技术栈

| 环节 | 方案 | 关键点 |
|---|---|---|
| 采集 | DXGI Desktop Duplication | 设备必须建在**拥有该输出的适配器**上 |
| 转换 + 缩放 | `ID3D11VideoProcessor` | GPU 内 BGRA→NV12，分辨率分档靠它缩放 |
| 编码 | NVIDIA H.264 Encoder MFT | 异步 MFT，Annex-B 输出，可直接走自建 UDP |
| 解码 | Microsoft H264 Video Decoder MFT | 喂访问单元(AU)，不是喂文件 |

### 硬件确认

```
适配器 0: NVIDIA GeForce RTX 3060 Ti   ← 显示器 \\.\DISPLAY1 挂在这里
编码器  : NVIDIA H.264 Encoder MFT     异步=1  D3D11 感知=1
解码器  : Microsoft H264 Video Decoder MFT
```

`\\.\DISPLAY1` 与采集纹理同在 NVIDIA 适配器上，编码走 NVENC 是零跨适配器拷贝。

---

## 这一轮踩到的 5 个坑（都有明确根因，值得记下来）

### 1. `DuplicateOutput` 成功 ≠ 拿到画面

`AcquireNextFrame` 返回成功，但只有鼠标移动时 `LastPresentTime == 0` 且
`AccumulatedFrames == 0`，此时纹理里**没有桌面图像**。

- 症状：存出来的 BMP **全黑**（`mean=0 stddev=0`，只有 1 种颜色）
- 而且旧代码**只存第一帧**，第一帧恰好就是那个黑帧
- 修法：只累积 `LastPresentTime != 0 || AccumulatedFrames > 0` 的帧
- 判据：`349/350` 帧有真实更新，剩下那 1 帧就是黑的

### 2. 诊断工具自己坏了，导致误判

`Log()` 里 `va_list` 被 `wprintf` 和 `vfwprintf` **共用**：第一个消费者耗尽列表，
第二个拿到空列表，日志文件只有 3 字节 BOM。

- 我据此误判成「进程崩溃」
- 修法：每个消费者各取一次 `va_list`

### 3. `MFGetAttributeRatio` 传了 `nullptr`

```cpp
MFGetAttributeRatio(t, MF_MT_FRAME_RATE, &rate, nullptr);  // 写空指针 → 0xC0000005
```

而且崩溃后 **WER 弹窗挂住了进程**，进程不退出 → exe 被锁 → 下次链接报
`LNK1104 无法打开文件`，看起来像编译问题。
修法：两个输出参数都给地址；并在 `wmain` 开头 `SetErrorMode(SEM_NOGPFAULTERRORBOX)`。

### 4. `MF_E_NOTACCEPTING` 被当成真错误

`0xC00D36B5` 带高位，`FAILED()` 判为真，但它其实是「编码器暂时吃不下」。

- 只重试一次 → **1080p60 只编码出 1 帧就退出**
- 改成「取输出 + `Sleep(1)`」重试 → 每帧 25.7 ms
- 根因：**Windows 默认时钟精度 15.6 ms，`Sleep(1)` 实际睡 15 ms**
- 修法：**按墙钟限时自旋等待**（`SwitchToThread`），成本=编码器真实延迟
- 结果：每帧等待降到 **0.04 ms**，1080p60 从 34.5 fps 提到 60.0 fps

### 5. 同步路径的错误假设

以为异步 MFT 会持续派发 `METransformNeedInput`（实测只派发 1 次就没了）。
**结论：不要依赖事件计数，用墙钟限时的自旋等待最稳。**

---

## 踩坑清单（速查）

| 现象 | 根因 | 修法 |
|---|---|---|
| BMP 全黑 | 读了无画面的帧 | 只累积有更新的帧 |
| 日志只有 3 字节 | `va_list` 共用 | 每个消费者各取一次 |
| `0xC0000005` 后 exe 被锁 | WER 挂住进程 | `SetErrorMode` 禁弹窗 |
| `LNK1104` | 上一进程未退出锁住 exe | 构建前先杀进程 |
| `0xC00D36C4` 解码 | MF 无裸 Annex-B 字节流处理器 | 改用解码器 MFT 喂 AU |
| `0xC00D36B5` 提前退出 | 误当错误 / `Sleep(1)` 精度 | 墙钟限时自旋 |
| 输出写到别处 | 依赖当前工作目录 | 按 exe 目录解析路径 |
| 链接拒绝覆盖 exe | 进程残留 | 杀进程后再链接 |

---

## 代码位置

```
src/media/probe/
  display-probe.cpp    采集可行性 + 环境诊断（DXGI 逐适配器×显示器试）
  encode-probe.cpp     编码器枚举 + 异步 MFT 协议 + 合成帧编码
  decode-probe.cpp     解码器 MFT + AU 切分 + NV12 转 BMP
  sender-probe.cpp     ★ 完整发送端：采集→GPU转NV12→编码
  build-*.cmd          对应构建脚本（纯 ASCII，cmd.exe 用 OEM 代码页读）
```

四个探针都是**独立可运行**的，各自打印 `*-probe-log.txt`。
`decode-probe` 可以直接解码 `sender-probe` 的输出做端到端核对。

---

## 下一步

1. **自建 UDP 传输** —— 分片、序号、重组、关键帧 NACK
2. **接收端** —— `decode-probe` 已有解码核心，补 UDP 收包 + WinUI 渲染
3. **接入 WinUI 应用** —— C# 界面 + C++ 助手进程（当前架构：`C# WinUI 3 (UI) + C++ 媒体助手`）
4. **远程控制** —— `SendInput` + 帧号绑定（防陈旧点击）
5. **单独应用共享音频** —— WASAPI 进程回环，必须子进程隔离（硬 `0xC0000374`，SEH 抓不住）

音频与画面同步用同一时钟，这一点用户已确认。
