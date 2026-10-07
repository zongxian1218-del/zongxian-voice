# 第三方组件与许可声明

本文件列出「棕仙语音 / ZongxianVoice」用到的第三方组件。
**注意区分两类**：随发布包一起分发的（第一节）与仅在开发/构建环境使用的（第二节）。

---

## 一、随发布包分发的组件

发行版 zip 内含 `tools\zxprobe.exe`（自包含探针，约 0.2 MB）
与 WinUI 运行时文件，涉及以下第三方组件：

### Microsoft Windows App SDK（WinUI 3）
- 版本：`1.7.250909003`
- 许可：MIT License
- 主页：https://github.com/microsoft/WindowsAppSDK

### Microsoft Edge WebView2
- 版本：`1.0.3351.48`（通过 NuGet 包 `Microsoft.Web.WebView2`）
- 许可：Microsoft 软件许可条款（可再分发；运行时由终端用户的 WebView2 Runtime 提供）
- 主页：https://developer.microsoft.com/microsoft-edge/webview2/

### .NET 运行时
- 版本：.NET 8
- 许可：MIT License
- 主页：https://github.com/dotnet/runtime

### Opus 音频编解码
- 说明：语音编解码由 WebRTC（WebView2 内置）提供，**未单独分发** Opus 二进制
- 许可：BSD 3-Clause
- 主页：https://opus-codec.org/

---

## 二、仅在开发 / 构建环境使用、**不随发布包分发**的组件

以下组件只出现在**开发机**上（构建产物），**未纳入版本库**（`.gitignore` 已排除），
**也未打进任何发布包** —— 这一点已用自动闸门校验（`check_dist`）。

### FFmpeg（解码探针用）
- 用途：开发期的 H.264 解码探针（`src/media/probe/`），**不是**发布产品的组成部分
- ⚠️ **重要**：本项目当时使用的 Windows 预编译包是 **GPL 构建**
  （二进制中可读到 `--enable-gpl` / `--enable-libx264` / `--enable-libx265`）
- **该二进制既不在版本库中，也不在任何发布包里**，因此不触发传染
- **如果你要自行构建解码探针**：请注意 GPL 的传染性 ——
  **分发**含 GPL 组件的产物会使整体需要以 GPL 授权。
  本项目仅需**解码**，用 **LGPL 构建**（不带 `--enable-gpl`）即可满足；
  官方构建：https://ffmpeg.org/download.html
- 许可：取决于构建配置（LGPL 2.1+ 或 GPL 2+）

### libdatachannel
- 用途：开发期的 WebRTC 数据通道探针
- 许可：MPL 2.0
- 主页：https://github.com/paullouisageneau/libdatachannel

### Mbed TLS
- 版本：`3.6.2`（由 `build/fetch-mbedtls.ps1` 拉取）
- 用途：libdatachannel 的 TLS 后端（开发期）
- 许可：Apache License 2.0
- 主页：https://github.com/Mbed-TLS/mbedtls

### Inno Setup
- 版本：6.7.1（`build/innosetup/`，开发期安装器工具）
- 许可：Inno Setup License（修改版 BSD 类）
- 主页：https://jrsoftware.org/isinfo.php

---

## 三、发布包实际内容（用于核对）

以 `build/release.py` 产出的 zip 为准，其必需项为：

```
ZongxianVoice.exe
ZongxianVoice.dll
resources.pri
tools\zxprobe.exe        ← 自包含，不含 FFmpeg（约 0.2 MB）
BUILD-INFO.txt
（+ WinUI 运行时依赖项）
```

**发布包不含**：FFmpeg 的任何 dll/exe、libdatachannel、Mbed TLS、任何构建中间产物。

---

## 四、美术素材

图标与角色形象（「棕仙」）**归作者所有**，不在本项目的 MIT 代码许可范围内，
也不适用上述任何第三方许可。若要复用，请先取得作者许可。
