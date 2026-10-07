# `dist/` 发布物清单（棕仙语音）

> **这份清单是给守卫看的**：`build/run-checks.py` 会检查
> ① `dist/` 顶层的每一项都在本文件里出现过；② 本文件里点名的产物都真的存在。
> 往 `dist/` 里放新东西之前先在这里登记；拿掉东西之后也要同步删掉对应行。

---

## 发布物（产物，不入 git）

| 名称 | 发布线 | 体积 | 构建/打包时间 | 溯源 | 备注 |
|---|---|---|---|---|---|
| `winui\` | — | — | — | 由 `build\release.py` 生成 | WinUI 打包目录（由 build\release.py 生成；内含 tools\zxprobe.exe，守卫会校验与 dist\zxprobe.exe 逐字节一致） |

> 版本发布记录由 `build/release.py --version NN` 自动追加到本表。
> 本仓库从 0 开始；此前属于「文件传输」产品的登记已挪到
> `build/dist-quarantine-2026-10-07/RELEASES-old-from-transfer.md` 留档。

## 源素材（入 git 跟踪，不是产物）

| 名称 | 用途 |
|---|---|
| `icons` | 应用图标素材（6 个尺寸），被打包脚本使用 |
| `zongxian-synced.ico` | 同步状态图标 |
| `RELEASES.md` | 本文件（自身也在 dist 顶层，故需登记） |
