# 贡献指南 / Contributing

欢迎改进！这个项目还比较年轻，**任何形式的帮助都有用**：报 bug、提需求、改文档、改代码、翻译。

## 最需要的帮助（Roadmap）

| 优先级 | 事情 | 说明 |
|---|---|---|
| ★★★ | **中继服务器一键部署** | `src/relay/relay_server.py` 已写好（打洞失败自动回退 + AES-GCM），但缺少 Docker 镜像、TLS 配置示例与文档 |
| ★★★ | **macOS / Linux 适配** | 桌面版是纯标准库 Python，但只在 Windows 实测过；`foldericon.py`/`autostart.py` 是 Windows 专用，需要各自的实现 |
| ★★☆ | **浏览器端多流** | 网页版走 WebRTC 单条 SCTP 流，丢包链路上吞吐会塌；研究多 DataChannel / 更好的分片与背压策略 |
| ★★☆ | **内网穿透更省心** | 现在推荐用户自己装 Tailscale/Radmin；可以做成"检测未安装 → 一键下载/引导"，或集成 Wintun + 自建协调服务 |
| ★★☆ | **英文界面与文档** | 目前是中文：网页版 `src/web/index.html` 文案、README、CLI `--help` 都可加 i18n |
| ★☆☆ | **单元测试覆盖** | `tests/` 只有 3 个文件，传输/同步/信令的边界条件值得补（断点续传、极端文件名、路径穿越、并发同步冲突） |
| ★☆☆ | **界面细节** | 网页版与桌面端的观感、无障碍（键盘可达性）、深色主题细节 |

## 怎么跑起来（开发环境）

```bash
# 只依赖 Python 3.10+ 标准库；网页版是单文件 HTML，无第三方 JS
set PYTHONPATH=src            # Windows；Linux/macOS: export PYTHONPATH=src
python -m swiftdrop gui       # 桌面版图形界面
python -m swiftdrop webhost   # 局域网网页服务 + 信令中继
python -m swiftdrop group     # 异地组网地址

python build/build-web.py     # 构建 dist/swiftdrop.html（单文件网页版）
```

浏览器端测试需要 Node + Playwright + 本机 Edge：

```bash
cd build/e2e && npm i
node e2e.mjs            # 8 项端到端：建连 / 32MB 校验 / 中文名 / 400 文件 / 续传 / 双向同步 / 反向
node lan-e2e.mjs        # 纯局域网信令建连 + 连接诊断面板
```

提交前请至少跑通：

```bash
python build/verify-desktop.py   # 桌面版 5/5
python build/verify-diag.py      # 诊断面板
node build/e2e/e2e.mjs           # 网页版 8/8
```

> 注意：`tests/test_lan.py` 的第 1 项是"200MB 速度下限 50MB/s"，在负载高的机器上会擦线失败（不是功能问题，见 issue 讨论）。

> 另外：`build/e2e/` 里的浏览器端脚本目前**硬编码了作者本机的工程路径**（形如 `const ROOT = 'D:\\文档\\ai001';`），
> 克隆后请把它改成你自己的路径——或者更推荐改成按脚本位置推导：
> `const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');`
> （欢迎有人提 PR 把所有脚本统一改掉，这是很好的"第一个 PR"。）

## 提交 Pull Request

1. Fork → 新建分支（`fix/xxx`、`feat/xxx`）；
2. **一次 PR 只做一件事**，改动尽量小；带上面"提交前"的测试结果；
3. 说明里写清：**改了什么、为什么、怎么验证的**（有真实输出最好）；
4. 涉及协议/帧格式的改动（`src/web/transfer.js` 与 `src/swiftdrop/transfer.py` 必须保持一致），请同时改两边并说明兼容性。

## 代码风格

- Python：PEP 8，尽量只用标准库；新增依赖请先开 issue 讨论（桌面版刻意保持"零依赖、双击即用"）。
- 前端：单文件、无构建步骤、无 CDN；保持经典 `<script>`（**不要用 ES module**，否则 `file://` 双击打开会失效）。
- 中文注释/文档没问题，英文也欢迎。
- 别把密钥/口令/个人地址提交进来（`.gitignore` 已排除 `build/android/`）。

## 报 Bug 请带上

- 系统与版本（Windows 10/11、浏览器版本）；
- **「连接诊断」面板里「复制诊断信息」的整段文本**（网页版与桌面版都有）——里面有链路类型、RTT、抖动、速度、卡顿与结论，能省掉大量猜测；
- 复现步骤与期望行为。

## 许可证

代码采用 MIT（见 `LICENSE`）。**「棕仙」角色与图标为作者美术作品，不在 MIT 范围内**，转用请自行替换。
