# v1.0 —— 首个公开版本

朋友之间传大文件：**不装服务器、不要公网 IP、不限速、无广告**。

## 下载

> 说明：GitHub 会剥掉附件名里的中文，所以文件名是英文的，对应关系如下。

| 文件 | 适合谁 | 说明 |
|---|---|---|
| `ZongxianTransfer-1.0-selfextract.exe` | **推荐** | 双击 → 选目录（默认桌面）→ 解压即用，自动建桌面/开始菜单快捷方式，可卸载（就是"自解压便携版"） |
| `zongxian-portable-win64.zip` | 喜欢绿色版 | 解压到任意目录，双击里面的 `棕仙的传输软件.exe` |
| `swiftdrop.html` | 发给朋友 | **单文件网页版**，对方双击打开、输入 9 位取件码即可互传；也可放在任意网址上 |
| `zongxian-transfer-guide.md` | 想先看说明 | 使用说明（含异地传输、速度预期、常见问题） |

## 这个版本有什么

- **局域网**：多路并行 TCP（默认 4 条，跨网自动升到最多 8 条），8MB 分片、4MB 块 SHA-256 校验，**断点续传**；本机实测 200MB **~150 MB/s**。
- **异地**：推荐用免费组网工具（Tailscale / Radmin VPN / ZeroTier）把两台设备放进同一虚拟局域网，本软件会**自动识别组网地址**并像局域网一样多流直传；网页版另有 WebRTC 打洞（一边是全锥形 NAT 时可用）。
- **文件夹同步**：双向/单向、`--delete-extra`、持续监控、**开机自启**，并且**同步文件夹会自动换成带徽标的图标**方便辨认。
- **连接诊断面板**（网页版 + 桌面版）：真实 RTT / 抖动 / 可用带宽 / 各流速度 / 磁盘写入占比 / 卡顿次数，并直接给结论（上行瓶颈 / 链路丢包 / 中继受限 / 接收方磁盘），一键复制诊断信息。
- **内置网页版窗口**：桌面版可直接用系统 Edge 打开原版网页端（跨网传输不用装浏览器插件）。
- **零依赖**：桌面版只用 Python 标准库；网页版是单文件 HTML，无 CDN、无第三方 JS。

## 已知限制（如实写）

- **双方都在运营商大内网（CGNAT）时纯 P2P 打不通** —— 这是物理限制，请用组网工具/手机热点/自建中继。
- **异地速度上限 = 发送方上行带宽**（家宽上行 20~30 Mbps 时约 2~4 MB/s）。
- 中继服务器（`src/relay/`）需自备一台有公网 IP 的机器。
- 只在 Windows 上打包与实测；macOS/Linux 未适配（欢迎 PR）。
- 安卓客户端已停止维护。

## 验证记录

| 项 | 结果 |
|---|---|
| `build/verify-desktop.py` | 5/5（128MB SHA-256 一致、断点续传、双向同步、`/signal` 101） |
| `build/e2e/e2e.mjs`（浏览器端到端） | 8/8（建连、32MB 校验、中文/emoji 文件名、400 个小文件、续传、双向同步、反向） |
| `build/verify-diag.py` | 5/5（诊断面板各项指标实测） |

---

**English**: Zongxian Transfer v1.0 — no-server, no-public-IP file transfer between friends. Three forms: single-file web app, Windows desktop app (multi-stream TCP + folder sync), any phone browser. Best over a virtual LAN (Tailscale / Radmin VPN). Desktop side is Python-stdlib only; web side is a dependency-free single HTML file. MIT licensed (artwork excluded). Contributions welcome.
