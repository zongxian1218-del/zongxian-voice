# 主应用启动崩溃的真根因：构建产物缺 resources.pri（2026-10-05）

> **一句话**：`ZongxianVoice.exe` 从**构建树（`bin\...`）**直接跑起来必崩
> （`0xc000027b`，`Microsoft.UI.Xaml.dll`，连窗口都没有），原因是构建产物里**没有 `resources.pri`**；
> 而 csproj 里关掉 PRI 生成的那段注释写着"非打包模式下不需要它" —— **这个结论是错的**，实测推翻了它。
>
> **影响面（已更正）**：**打包产物不受影响** —— `build-winui-cs.cmd` 里本来就有一步 makepri
> （dist 那份 868 KB 的 pri 就是这么来的，我一开始误判成"新打包也会崩"，见下面 §三 的更正）。
> 真正的缺口是**开发期从构建树直接跑**：没有任何提示、没有任何守卫，只有一句 `0xc000027b`。

---

## 〇、修复状态：已完成并实测（2026-10-05）

| 项 | 做了什么 | 实测证据 |
|---|---|---|
| 可复用的 pri 生成 | 新增 `build/gen-app-pri.ps1`：自动找 `makepri.exe`（多个 SDK 版本里搜，不再写死版本号）、只在干净暂存目录里索引、**带体积下限校验**（<200 KB 直接报错拒绝安装，防止再产出 14 KB 的残缺 pri） | 生成 **866,648 B**（dist 那份能跑的 868,296 B，同量级）；用它跑应用 → **0 崩溃 + 可见窗口** |
| 打包脚本去重 | `build-winui-cs.cmd` 里那段内联 makepri（写死 `10.0.26100.0`）改成调用上面这个脚本 | 逻辑统一，换 SDK 版本不会再失效 |
| **构建守卫** | `build/run-checks.py` 新增"主应用产物是否带 resources.pri"，缺失直接 FAIL 并打印原因与修法 | 移走 pri → **FAIL**（并打印原因/修法）；生成回来 → **PASS** |

> 这类"能构建、能打包、一跑就崩"的坑必须有自动闸门，所以守卫是这次的重点产出，不只是修一次。

---

## 一、怎么发现的

在做 P1-6（把原生视频搬进主应用）时，应用进程起来了却**连一个窗口都没有**
（枚举含不可见的顶层窗口也是 0 个）。事件日志给出硬证据：

```
[Application Error] id=1000
Faulting application name: ZongxianVoice.exe, version: 1.0.0.0
Faulting module name: Microsoft.UI.Xaml.dll, version: 3.1.7.0
Exception code: 0xc000027b        ← STATUS_STOWED_EXCEPTION
Fault bucket 1620303424059241793
```

进程"活着"是**卡在 WER 里**（和之前发现的 encode-probe 僵尸同类，杀不掉）。

## 二、隔离实验（每一步都有实测结果）

| # | 实验 | 结果 |
|---|---|---|
| 1 | `dist\winui\ZongxianVoice.exe`（03:50 打包） | ✅ **可见窗口**（`hwnd=0xF21FF0 visible=True`），不崩 |
| 2 | `bin\...\win-x64\ZongxianVoice.exe`（13:09 构建） | ❌ 无窗口，崩溃（同 fault bucket，复现 4 次） |
| 3 | 不带 `ZX_NATIVE_VIDEO` 跑（我新加的代码整段跳过） | ❌ **照样崩** → 与 P1-6 改动无关 |
| 4 | 对比两个目录的文件清单 | 构建树是 dist 的**真子集**；缺 `resources.pri`、`ZongxianVoice.exe.manifest`、`NPUDetect.dll`（dist 多出来的其余 188 个是 03:50 测试留下的 txt/log、`.webview2\` 配置目录、media、recordings） |
| 5 | 把这三个文件拷进构建树再跑同一个 exe | ✅ **可见窗口**（`pid=31080 visible=True`） |
| 6 | **只**移走 `resources.pri`（manifest 仍在） | ❌ 0 窗口 + 2 次新崩溃事件 → **pri 是必需的那一个** |

结论：**`resources.pri` 存在与否 = 能不能启动**。

## 三、根因：一个被写错的结论

`src/winui-cs/ZongxianVoice.csproj`：

```xml
<!--  ...（长注释）...
      关掉 MRT Core 的资源索引（PRI）工具链。
      ...
      【关掉的影响】
      不生成 resources.pri。我们不做多语言资源、不按缩放比切图，
      非打包模式下不需要它。代价是将来若要做多语言或被本地化的控件文本，
      需要重新打开并安装对应 VS 组件。
-->
<EnableCoreMrtTooling>false</EnableCoreMrtTooling>
```

- 关掉它的**原因是真的**：开着会报两条 `MSB4062`，因为
  `Microsoft.Build.Packaging.Pri.Tasks.dll` / `Microsoft.Build.AppxPackage.dll` 只在**完整 VS** 里有，
  .NET SDK 与 Build Tools 都没有。
- 但"**非打包模式下不需要它**"是**错的**：WinUI 3 非打包模式同样要 `resources.pri` 才能加载 XAML，
  否则就是上面的 `0xc000027b`。

**影响面（更正）**：
- ✅ **打包产物不受影响**：`build-winui-cs.cmd` 里本来就有 makepri 那一步，
  所以 dist 里的 pri 是**打包时生成的**（这也是为什么 dist 能跑）。
  我第一版文档写成"新打包也会崩"，那是**没读完打包脚本就下的结论**，属于我的错，已更正。
- ❌ **开发期从构建树直接跑必崩**：`dotnet build` 不生成 pri（因为 `EnableCoreMrtTooling=false`），
  于是从 `bin\...` 启动就是一句 `0xc000027b` + 无窗口，**没有任何提示**。
- ⚠️ 另外那段内联 makepri 把 SDK 版本号**写死**成 `10.0.26100.0`，换机器/换 SDK 会静默失效
  （只打印一句 WARN 就继续打包）。

**下一步（已完成，见 §〇）**：把 pri 生成做成可复用脚本 + 加构建守卫。

## 四、当时的选路与最终选择（留档）

原本列了三条路：
- a) 装 VS 的"Windows 应用打包"组件 → 去掉 `EnableCoreMrtTooling=false`；
- b) 用 Windows SDK 的 `makepri.exe` 构建后生成；
- c) 从 WindowsAppSDK 的 `Microsoft.Build.Msix` 包补那两个 task DLL。

**实测后的选择：走 b**。理由：
- `makepri.exe` 本机**确实存在**：`C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\makepri.exe`
  （`D:\BuildTools\SDK` 下没有，一开始只看了那里，白绕了一下）；
- 那两个 VS 专有 task DLL **全盘都找不到**（搜了 `C:\Program Files*` 与 `D:\BuildTools`），
  所以 a) 要先装组件、c) 要引新包，都更重；
- 而 b) 与打包脚本里**已经在用**的做法一致（dist 那份能跑的 pri 就是 makepri 的产物），
  只是把它从"打包脚本里的内联代码"提成可复用脚本 + 守卫。

> 另外把"**不要出新包**"这条警告**撤回**：打包脚本本来就生成 pri，包是好的。当时那么写是误判。

---

## 附：P1-6（原生视频搬进主应用）本轮的进度

- 新增 `src/winui-cs/src/NativeVideoHost.cs`：把覆盖窗口逻辑从原型抽成可复用类
  （助手查找顺序、letterbox 显示区、最小化发 `hide`、stdout 只认 ASCII 事件行）。
- `MainWindow` 里接线，**默认关闭**，`ZX_NATIVE_VIDEO=1` 才启用（关着时与原来完全一致）。
- 构建：`dotnet build` **0 警告 0 错误**。
- 运行验证（在 pri 补齐之后）：助手起来了，覆盖窗口 `ZxPreviewWnd visible=True`
  位于 **(632,242) 672x378**，与应用算出的显示矩形**逐位一致**
  （画面区 674x378 + 1280x720 letterbox → 672x378；`nativevideo-pos.txt` 里有对照数据）。

### 两个未解项（如实记录，不编原因）

1. **覆盖窗口的 `WS_EX_TOPMOST` 没设上**：实测 `exStyle=0x08000080`（只有 `NOACTIVATE|TOOLWINDOW`）。
   而**同一条代码路径**在原型 `winui-videoprobe` 里实测是 `topmost=True`。两次都是实测，
   差异来源待查；在查清之前，覆盖窗口可能被别的窗口盖住（本轮截图就被一个"Windows 远程协助"窗口盖了）。
   注意 `rect` 命令用的是 `SWP_NOZORDER`、创建路径用的是 `HWND_TOPMOST`，从代码上看不该掉。
2. **WER 僵尸**：崩过的 `ZongxianVoice.exe` 进程杀不掉（累计 5 个），会干扰后续窗口枚举判断。
