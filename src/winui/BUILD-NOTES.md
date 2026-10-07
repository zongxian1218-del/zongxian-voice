# 棕仙语音 · W0 构建链验证记录

日期：2026-10-04
目的：记录「WinUI 3 到底怎么在本机编起来」这件事的结论，
      避免以后（或换机器时）重复踩同样的坑。

---

## 结论：走 C# + WinUI 3

| 路线 | XAML 编译器从哪来 | 本机可行 |
|---|---|---|
| **C++/WinRT** | 需要 Visual Studio 组件 `Microsoft.VisualStudio.Component.WindowsAppSdkSupport.Cpp` | ❌ **装不上**（见下） |
| **C#** | **`Microsoft.WindowsAppSDK` 的 .NET 包自带** `Microsoft.UI.Xaml.Markup.Compiler` | ✅ |

**决定性差异**：C++ 路线的 XAML 编译工具（`XamlCompiler.exe`）**不在 NuGet 包里**，
必须由 Visual Studio 组件提供；而 C# 路线的同一能力是随包分发的。

---

## C++ 路线为什么卡住（完整记录）

### 已经验证可行的部分

| 步骤 | 结果 |
|---|---|
| `vcvars64.bat` + `MSBuild.exe`（D:\BuildTools） | ✅ |
| 手写 `.vcxproj` | ✅ 结构正确 |
| NuGet 还原 `Microsoft.WindowsAppSDK` 1.7.250909003 | ✅ |
| `cl.exe` 实际开始编译 | ✅ |
| `cppwinrt.exe` | ✅ 在 Windows SDK 里（`Windows Kits\10\bin\10.0.26100.0\x64\`） |
| `makeappx.exe`（MSIX 打包） | ✅ 在 Windows SDK 里 |

### 卡住的那一步

```
error C1083: 无法打开包括文件: "winrt/Microsoft.UI.h"
```

排查结论：
- 包内 `include\winrt\` 里**只有 Interop 头**，没有 `Microsoft.UI.h` / `Microsoft.UI.Xaml.h`
- 这些投影头需要由 **WinUI 的 XAML 编译步骤生成**（`XamlCompiler.exe`）
- 全盘搜索确认 `XamlCompiler.exe` **不存在**，`Microsoft.WinUI.props` / `Microsoft.WindowsAppSDK.props` 也不在 Build Tools 里

### 尝试修复：装组件

```powershell
setup.exe modify --installPath D:\BuildTools `
  --add Microsoft.VisualStudio.Component.WindowsAppSdkSupport.Cpp `
  --quiet --norestart --nocache
```

结果：**退出码 0，但什么都没装**。检查实例的 `state.json`，
`selectedPackages` 里没有这个组件（只有 `VC.Tools.x86.x64`、
`Windows11SDK.26100`、`VC.ATL` 等之前装上的）。

原因：**VS 安装器需要提权**。本会话的审批是自动拒绝的，
安装器拿不到提权就静默失败，且仍返回 0。

### 如果将来要走 C++ 路线，需要什么

1. 以管理员权限运行 VS 安装器，添加
   `Microsoft.VisualStudio.Component.WindowsAppSdkSupport.Cpp`
   （它会带入 `Microsoft.WindowsAppSDK.Cpp.Dev17`，即 XamlCompiler）
2. 或者安装完整的 Visual Studio（Community 版即可），用其自带的 WinUI 项目模板

---

## C++ 路线踩过的坑（对 C# 路线同样有参考价值）

### 1. 批处理文件必须纯 ASCII

`build\build-*.cmd` 里出现中文注释会出问题：cmd.exe 按 OEM 代码页（本机 936）
解析 .bat，UTF-8 多字节序列会吞掉换行，导致行粘连、`if` 块报
`do was unexpected at this time`。

**更隐蔽的是**：用编辑器（或脚本）改一次文件后，编码可能变化，
原本能跑的脚本突然就坏了。所以规则是：**批处理文件永远只用 ASCII**。

### 2. C++ 工程不能用 `PackageReference`

NuGet 的 `PackageReference` 按目标框架（TFM）解析包，而 C++ 工程没有 TFM。
硬写 `TargetFramework=native` 也不行，报错是：

```
error : 序列不包含任何元素
error : Your project does not reference "native,Version=0.0" framework
```

完全看不出真正原因。**正确做法**是 `packages.config` + 在 `.vcxproj` 里
显式 `Import` 包内的 `props`/`targets`（官方 WinUI 3 C++ 模板就是这么做的）。

### 3. 不要重复 Import WindowsAppSDK 的子模块

`Microsoft.WindowsAppSDK.props` 内部已经带入了
`Microsoft.WindowsAppSDK.WinUI.props` 等子模块。再手动 Import 一次会报
`warning MSB4011: 无法再次导入`。

### 4. MSBuild 目标里 `Restore` 与 `/restore` 开关是两条路

- `/restore` 开关 → 走 `PackageReference` 路径（C++ 会失败）
- `/t:Restore /p:RestorePackagesConfig=true` → 走 `packages.config` 路径（C++ 正确）

### 5. 中文路径对构建工具的影响（未证实但值得警惕）

工程路径含中文（`D:\文档\ai001`）。C++ 路线里 MSBuild 能正常处理，
但为保险，`build\build-media.cmd` 一直把中间产物放到 `%TEMP%\zx-build`
（纯 ASCII 路径）。C# 路线继续沿用这个策略。

---

## C# 路线要装什么

| 组件 | 来源 | 状态 |
|---|---|---|
| .NET SDK 8 | `winget install Microsoft.DotNet.SDK.8` | 安装中 |
| `Microsoft.WindowsAppSDK`（.NET 包） | NuGet | 自带 XAML 编译器 |
| WebView2 Runtime | 系统已有 **154.0.4258.53** | ✅ |
| MSIX 打包工具 | Windows SDK（`makeappx.exe`） | ✅ |

---

## 架构调整（C# 版）

```
C# + WinUI 3 外壳
  · 原生窗口（含 Mica / 明暗主题跟随）
  · 三栏布局用原生 XAML 控件（NavigationView / ListView / Grid）
  · 文字消息、会话列表、成员列表、设置 —— 全部原生
  · 远端控制授权横幅 —— 原生（信任等级最高，必须一眼可见）
  · 用 AppNotification 发系统通知
    │
    └─ 通话与共享区：WebView2（Chromium）
         · getUserMedia / getDisplayMedia / RTCPeerConnection
         · 只用在这一块，因为它是唯一需要 Chromium WebRTC 的地方
         · 与 C# 侧通过 WebMessage 通信
```

**为什么通话区用 WebView2 而不是全原生**：这是个务实的取舍。
全原生意味着在 .NET 里实现 WebRTC（`SIPSorcery` 是纯托管实现，
没有 AEC3 与 NetEQ），或者再回到 C++ 路线。用 WebView2 只牺牲
"视频画面那块是网页渲染"，换来完整的工业级 WebRTC。

界面其余部分都是真正的原生 WinUI 控件，这也是用户要的"正式封装"。

---

## 附：C# 路线的实测结果（2026-10-04）

### 结论：**全线打通 —— 构建 0 警告 0 错误，应用正常启动并显示窗口**

![三栏骨架窗口](../../build/winui-window.png)

| 环节 | 状态 |
|---|---|
| .NET SDK 8.0.425 | ✅ 已装 |
| NuGet 还原 WindowsAppSDK 1.7 + WebView2 | ✅ |
| XamlCompiler 跑通、生成 `.g.i.cs` | ✅ |
| csc 编译、产出 `ZongxianVoice.dll` / `.exe` | ✅ **0 警告 0 错误** |
| `resources.pri` | ✅ 由构建脚本调 `makepri.exe` 生成 |
| manifest 合并与嵌入 | ✅ **由 WindowsAppSDK 的 targets 自动完成**（见下） |
| **应用启动、显示 1180×760 窗口** | ✅ **成功** |
| 三栏布局 / 语义画刷 / Mica / 跟随系统主题 | ✅ 见截图 |

### 排查过程中被推翻的两个错误判断（值得记）

**1. 以为是 `resources.pri` 缺失**

早期把 `EnableCoreMrtTooling` 关掉后没有 PRI，怀疑是它导致
`XamlParseException`。后来用 `makepri.exe` 补上了 PRI，**仍然失败** ——
说明不是它。PRI 对 WinUI 自有资源（`Microsoft.UI.pri` 等随包分发）
并不是必需的。

**2. 以为是 manifest 没嵌入**

产物目录里没有 `ZongxianVoice.exe.manifest`，于是判断是免注册 WinRT
激活清单缺失。**这个判断是错的**，因为：

```
Microsoft.WindowsAppSDK.SelfContained.targets(292,9):
  mt.exe -nologo -manifest "...\Manifests\WindowsAppSDK.manifest"
                   "merged.manifest" -out:"...\Manifests\app.manifest"
```

**targets 本来就会调 `mt.exe` 合并并嵌入 manifest。** 用
`mt.exe -inputresource:app.exe;#1` 抽取验证，里面确实同时含有
`Microsoft.UI.Xaml.Application`（激活类）与 `requestedExecutionLevel`。

我当时还手工合并了一份 manifest 想用 `ApplicationManifest` 属性嵌入，
结果反而把构建搞坏了（`mt.exe` 报 c1010070）。**教训：先验证机制是否
已经工作，再去补它。**

**3. 真正的元凶：构建脚本漏拷 `.runtimeconfig.json`**

去掉前面两个错误假设之后，`dotnet build` 本身是好的。
真正的问题在**运行**：产物目录缺 `.runtimeconfig.json` 和 `.deps.json`，
导致

```
A fatal error was encountered. The library 'hostpolicy.dll' required to
execute the application was not found in 'C:\Program Files\dotnet\'.
Failed to run as a self-contained app.
```

这条消息**误导性极强**（应用并不是自包含的）。修法：构建脚本收集产物时
包含 `*.json`，并且**不要挑文件，整目录复制**。

### 已修掉的真问题汇总

1. **漏拷 `.runtimeconfig.json` / `.deps.json`** → 应用无法启动，且报错信息误导。
   修法：`for /r ... in (*.exe *.dll *.pri *.json)`，或直接整目录复制。

2. **XAML 的 ItemType 必须显式声明**
   否则报两个看不出因果的错：
   ```
   error CS5001: 程序不包含适合于入口点的静态 "Main" 方法
   error MSB3073: XamlCompiler.exe ... 已退出，代码为 1
   ```
   实际原因：没有任何 XAML 被当成 XAML 处理。
   需要 `<ApplicationDefinition Include="src\App.xaml">` + `<Page Include="src\MainWindow.xaml">`，
   并关掉 .NET SDK 的默认推断：
   `<EnableDefaultPageItems>false</EnableDefaultPageItems>` +
   `<EnableDefaultApplicationDefinitionItems>false</EnableDefaultApplicationDefinitionItems>`
   （只设 `EnableDefaultXamlItems` 管不住 SDK 自己的 glob，会报
   `NETSDK1022: 包含了重复的"Page"项`）

3. **不要手写 `Program.cs`**
   `App.g.i.cs` **会**生成 `Program` 类与 `Main`（带
   `#if !DISABLE_XAML_GENERATED_MAIN` 开关）。手写会撞成
   `CS0101: 命名空间已包含"Program"的定义`。
   （这与 C++/WinRT 不同 —— 那边确实要自己写 main。）

4. **必须关掉 `EnableCoreMrtTooling`**
   否则构建需要 Visual Studio 专有的 MSBuild 任务程序集
   （`Microsoft.Build.Packaging.Pri.Tasks.dll`、
   `Microsoft.Build.AppxPackage.dll`），而它们既不在 .NET SDK 里也不在
   Build Tools 里，报连续两条 `MSB4062`。
   关掉之后 `resources.pri` 由构建脚本用 `makepri.exe` 单独生成。

### 验证手段（这部分沉淀下来复用）

| 用途 | 命令/脚本 |
|---|---|
| 编译 | `build\build-winui-cs.cmd`（dotnet + makepri + 整目录收产物） |
| 看启动异常 | `dist\winui\startup-error.log`（应用自己写的，见下） |
| 确认窗口存在 | 用 Python `EnumWindows` 按 PID 枚举，比 `MainWindowHandle` 可靠 |
| 截图 | `build\capture-window.py`（含前台锁绕过） |

### 两个诊断经验值得单独记

**1. WinUI 3 的启动失败是"哑"的。**
进程以 `0xC000027B`（`STATUS_STOWED_EXCEPTION`）退出，stderr 为空，
事件日志也可能一条都不写。**唯一可靠的诊断手段是自己写异常日志** ——
`App.OnLaunched` 里包 try/catch，把异常写到 exe 同目录的
`startup-error.log`。这一步直接省掉了几小时的盲猜，强烈建议保留。

**2. 截图要先绕过前台锁。**
Windows 的前台锁定会让非前台进程的 `SetForegroundWindow` **静默失败**，
于是"窗口坐标取对了，截出来却是别的程序"。第一次截图抓到的是浏览器
画面，看起来像脚本坏了，实际是应用窗口被盖在下面。
修法：`AttachThreadInput` 把自己的输入队列附加到前台线程，
调用完再分离。见 `build\capture-window.py`。

**3. 不要用 PowerShell 的 `Start-Process` 启动这类"启动即失败但进程短暂
存活"的应用** —— 表现为 PowerShell 命令卡死（本会话卡过两次，
每次都要等超时才被移到后台）。用 `cmd /c "app.exe > out 2> err"` 拿退出码，
或用 `cmd /c start` 分离启动。

---

## 复用对照：C++ 版代码里哪些还有价值

| 资产 | 价值 |
|---|---|
| `docs/ui-design-guideline-2026-10-04.md` | ✅ **完全复用**：三栏栏宽、间距、语义画刷用法、控制权横幅的交互要求 |
| `src/winui/src/App.xaml` 的 ThemeDictionaries 设计 | ✅ 复用思路（只定义 2 个业务语义色，其余用内建），已移植到 `src/winui-cs/src/App.xaml` |
| `src/winui/src/MainWindow.xaml` 的布局 | ✅ 已完整移植到 `src/winui-cs/src/MainWindow.xaml`（458 行，三栏骨架） |
| `src/winui/src/*.cpp` / `*.h` | ❌ 作废（约 600 行），但其中关于 Mica、标题栏主题、初始化顺序的注释有参考价值 |
| `zongxian_voice.vcxproj` / `packages.config` | ❌ 作废，但本文档前半部分的 5 条坑值得保留 |
| `build\build-winui.cmd` | ❌ 作废（C++ 专用），保留作参考 |
| `build\build-winui-cs.cmd` | ✅ **当前使用的构建脚本** |
| `build\capture-window.py` | ✅ 窗口截图（含前台锁绕过），UI 评审与回归都用得上 |

## W0 遗留的待办（转入 W1）

1. **PRI 里的 XAML 资源**：当前 `resources.pri` 是 `makepri` 用默认配置扫
   整个目录生成的。等界面有 `.resw` 本地化字符串或图片资源时，
   需要重新审视这份配置（`makepri createconfig` 的默认值未必合适）。
2. **MSIX 打包**：发布到商店需要打包模式 + `Package.appxmanifest`，
   以及重新打开 `EnableCoreMrtTooling`（那需要装 VS 组件，要提权）。
3. **`.gitignore`**：`src/winui-cs/obj`、`bin`、`dist/winui` 都该忽略。


