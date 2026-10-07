# 屏幕采集验证记录 —— 2026-10-05

## 结论速览

| 采集方案 | 结果 | 卡点 |
|---|---|---|
| **Windows.Graphics.Capture（WGC）** | ❌ 失败 | `CreateForMonitor` 返回 `0x80070005`（E_ACCESSDENIED），两者都失败 |
| **DXGI Desktop Duplication** | ⏳ 未验证完 | C# 版探针在初始化中崩溃（无日志），需改用 C++ 实现 |

**Windows 系统限制与硬件环境全部正常**，问题定位在采集 API 调用本身。

---

## 一、WGC 的失败（已充分排查）

### 现象

```
[OK] DispatcherQueue: 存在
[OK] Windows.Graphics.Capture 支持: True
[信息] 会话 ID      : 1
[信息] 远程会话     : False  (SM_REMOTESESSION)
[信息] 窗口站       : WinSta0
[信息] 交互式桌面   : True
[信息] 进程完整性   : 管理员
[信息] 共 1 个显示器: \\.\DISPLAY1（主）
[X] 创建失败: UnauthorizedAccessException 0x80070005
```

### 已排除的原因（7 项，每项都有实测证据）

| 假设 | 排除依据 |
|---|---|
| DSH 沙箱拦截 | 用户手动双击运行 `Probe.exe`，结果完全相同 |
| 缺 DispatcherQueue | 改为 WinUI 程序后仍失败 |
| 虚拟显示器 | `EnumDisplayMonitors` 只返回 1 个真实显示器 |
| 远程桌面会话 | `SM_REMOTESESSION = False` |
| 无交互桌面 | `WinSta0`，`UserInteractive = True` |
| 权限不足 | 进程完整性为**管理员** |
| WGC 不受支持 | `GraphicsCaptureSession.IsSupported() = True` |

### 剩余可能

1. **未打包（unpackaged）应用的身份限制** —— WinUI 3 `WindowsPackageType=None`
   时，部分 WinRT 采集接口可能要求包身份（`Package.appxmanifest` 里声明
   `graphicsCapture` 能力）
2. **`IGraphicsCaptureItemInterop` 的 C# 封送仍有 bug** —— 手写 P/Invoke
   在本会话已连错三次（CS7036 / CS0039 / CS0199），不能排除还在错

---

## 二、DXGI Desktop Duplication 的尝试

选它的理由：**完全不需要 WinRT 互操作**，未打包应用可用，OBS 等程序使用多年。

代价：只能整屏采集，不能指定单窗口。对本项目够用（需求是"看得见朋友的屏幕"）。

### 状态

C# 版探针 `DxgiCapture.cs` 已完成并编译通过，但**运行时在初始化阶段崩溃**：

- 日志完全没有生成（连第一行都没有）
- WGC 模式下同样的日志机制工作正常，所以不是日志路径问题
- 崩溃点最可能在 `D3D11CreateDevice` 或 vtable 调用

**已知的自身缺陷（写在这里避免重复踩）：**

1. `DXGI_ADAPTER_DESC` 结构体用了 `ByValTStr`，托管大小（~56 字节）
   远小于原生（~304 字节）→ **会写坏堆内存**
2. `IDXGIOutputDuplication::AcquireNextFrame` 的 `DXGI_OUTDUPL_FRAME_INFO`
   结构体定义可能也不准确
3. 手写 vtable 序号调用容易错（`IDXGIOutput1::DuplicateOutput` = 22 等）

---

## 三、已确认可用的基础（重要）

| 项 | 状态 |
|---|---|
| MSVC 编译器 | ✅ `D:\BuildTools\VC\Tools\MSVC\14.44.35207` |
| Windows SDK | ✅ `10.0.26100.0` |
| WindowsAppSDK | ✅ `1.7.250909003`，XAML 编译器可用 |
| `vcvars64.bat` 环境 | ✅ `D:\BuildTools\VC\Auxiliary\Build\vcvars64.bat` |
| D3D11 设备创建 | ✅ 成功，feature level `0xB000` |
| GPU 识别 | ✅ NVIDIA GeForce RTX 3060 Ti（有 NVENC） |
| WGC 支持检测 | ✅ `IsSupported() = True` |
| 硬件 H.264 编码器 | ⏳ 探测代码已写，待采集通了再验证 |

**编译工具链完全跑通**，这是后续所有原生工作的前提。

---

## 四、下一步建议

### 首选：改用 C++ 实现采集（独立进程）

理由：

1. **C++ 探针已经编译并成功运行过**（`capture_probe.cpp` 输出了 D3D11 与
   WGC 支持检测结果），证明 C++ 路线可行
2. **避开 C# 手写 P/Invoke 的全部坑** —— 本会话三次编译错误 + 至少两次
   结构体大小不匹配导致的崩溃，都在 C# 互操作上
3. C++ 直接 `#include <dxgi1_2.h>` 用官方头文件，**不需要手写 vtable**
4. 与最终架构一致：C++ 媒体助手进程（采集+编码+传输）+ C# WinUI 界面

### 备选：排查 WGC 的包身份问题

给探针加 `Package.appxmanifest` 并声明 `graphicsCapture` 能力，切
`WindowsPackageType=MSIX` 重新构建。**但这条路会改变整个应用的打包方式**，
代价较大，建议作为备选。

---

## 五、本会话踩过的坑（供后续参考）

| 坑 | 症状 | 教训 |
|---|---|---|
| `va_list` 被两个 printf 共用 | 日志文件只有 3 字节 BOM，误判成"进程崩溃" | **诊断工具本身也可能是 bug**，先验证工具再信结论 |
| PowerShell 正则替换 `wprintf(` | 把 `vfwprintf` 也改成 `vfLog`，编译失败 | 改代码用 `edit` 工具，不要用正则批量替换 |
| 手写 P/Invoke 三次编译错误 | CS7036 / CS0039 / CS0199 | C++ 用官方头文件比 C# 手写互操作可靠得多 |
| `edit` 因文件变更失败 | "file changed since it was read" | 长会话中每次 edit 前重读文件 |
| 日志写 `AppContext.BaseDirectory` | 运行时可能不可写，日志静默丢失 | 日志写 `Path.GetTempPath()` |
| `Start-Process -Wait` / `Start-Sleep` | 在沙箱里会长时间阻塞 | 用后台任务或直接读产物 |
