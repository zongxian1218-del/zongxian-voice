# 交付物与打包规范（2026-10-05 清理记录）

## 一句话

`dist/` 现在**只放准备交给别人的文件**。清理前 **853.6 MB → 清理后 225.9 MB**（释放 627.7 MB），
移走的东西全部保留在 `build/dist-quarantine-2026-10-05/`，随时可以移回来。

---

## 先搞清楚：`dist/` 里哪些是"打包暂存"、哪些是"交付物"

按代码核实（不是猜）：

- **`dist/winui/` = 主应用（棕仙语音）的打包暂存目录**，由 `build/build-winui-cs.cmd` 写入
  （脚本里 `set "DIST=%~dp0..\dist\winui"`，第 28 行）。**它是暂存区，不是要发出去的东西**；
  交付物是从它压出来的 zip。
- 打包流程：`build\build-winui-cs.cmd` → 产物收进 `dist/winui`（含 `media/`、`resources.pri`）→ 压 zip 到 `dist/`。
- `dist/` 下**其它**目录（`_pkg`、`测试/`、各版本号目录）在仓库脚本里**没有任何引用**，是历史暂存副本。

---

## 本次移走的东西（移动到，不是删除）

| 名称 | 大小 | 为什么移走 |
|---|---|---|
| `_pkg` | 95.7 MB | WindowsAppSDK 展开副本，全仓脚本无引用 |
| `棕仙语音-测试版-v13`、`v13 - 副本`、`v15`、`v17` | 各约 100 MB | 历史版本暂存目录，已被 `dist/winui` 取代 |
| `测试/棕仙语音-测试版-v14` | 约 106 MB | 同上（多层嵌套，更容易误读） |
| `nstest.exe`、`zxprobe.exe`、`zongxian_media.dll` | 小 | 调试产物误入 `dist/` |
| `dist/winui/.webview2/` | 约 50 MB | WebView2 用户数据缓存，运行时自动重建 |

回退方式：把 `build/dist-quarantine-2026-10-05/` 里的对应项移回 `dist/` 即可。

---

## 当前 `dist/` 清单（交付物）

| 文件 | 大小 | 时间 | 是什么 |
|---|---|---|---|
| `棕仙的传输软件-自解压版.exe` | 9.3 MB | 10-04 14:18 | **A 线 v1.0**：自解压版（双击→选目录→解压即用） |
| `棕仙的传输软件-安装包-1.0.exe` | 9.1 MB | 10-04 14:18 | **A 线 v1.0**：NSIS 安装包 |
| `zongxian-portable-win64.zip` | 12.8 MB | 10-04 14:18 | **A 线 v1.0**：便携 zip |
| `棕仙的传输软件/` | 32.4 MB | 10-04 14:18 | 便携版解压后的目录（exe + `_internal`） |
| `swiftdrop.html` | 0.3 MB | 10-04 14:05 | 网页版单文件（发给朋友双击即用） |
| `zongxian-transfer-guide.md` | 21 KB | 10-04 13:54 | 传输线使用说明 |
| `zongxian.ico` / `zongxian-synced.ico` / `zongxian-icon.png` / `icons/` | 合计约 0.5 MB | 10-04 | 图标（美术资源，不在 MIT 范围） |
| **`棕仙语音-测试版-v22.zip`** | **32.9 MB** | **10-05 10:09** | **B 线语音：当前最新测试包（349 项，根级结构）** |
| `棕仙语音-测试版-v21.zip` | 32.5 MB | 10-05 03:39 | 旧测试包，**包内 exe 早于源码，不要再发** |
| `棕仙语音-测试版-v13 - 副本.zip` | 32.5 MB | 10-05 00:21 | 更旧，历史留存 |
| `dist/winui/` | 约 108 MB | 10-05 10:09 | **打包暂存区**（不是交付物） |

---

## 顺手修掉的问题：包比源码旧

- 旧包 `棕仙语音-测试版-v21.zip` 里 `ZongxianVoice.exe` 的时间戳是 **03:38:08**，
  而主应用源码最后一处改动是 **03:50:40**（`src/winui-cs/src/MainWindow.xaml.cs`）—— **包确实早于源码**。
- 处理：跑 `build\build-winui-cs.cmd` 重新收产物到 `dist/winui`
  （exe/dll = **03:50:53**，晚于源码 03:50:40；`resources.pri` 于 10:09:17 重新生成），
  再压出 **`棕仙语音-测试版-v22.zip`**（32.9 MB / 349 项 / 10:09:58）。
- 校验：v22 内 `ZongxianVoice.exe`、`.dll`、`.runtimeconfig.json`、`resources.pri`、`media/call.html` 全部存在；
  条目为根级（与 v21 一致）；**不含** `.webview2` 缓存。

---

## 规矩（写下来，免得下次又乱）

1. 交付物只放 `dist/` 顶层；版本靠版本号/日期区分，**不写 `final`/`最新`/`v2` 这类词**。
2. `dist/winui/` 是**暂存区**，随时会被 `build-winui-cs.cmd` 覆盖 —— 不要往里放别的东西，也不要当交付物发。
3. 覆盖已有交付物前，先备份到 `dist/.backup/`。
4. **打包后必须核对"包内 exe/dll 的时间戳晚于对应源码"** —— 本次就是靠这条发现 v21 是旧包。
5. 调试产物不要进 `dist/`（`nstest.exe`/`zxprobe.exe`/`zongxian_media.dll` 就是这么混进去的）。
6. 版本目录用完即清，或者在压包后直接删；不要留一堆 100 MB 级的展开副本。
