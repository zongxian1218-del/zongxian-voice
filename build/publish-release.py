#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""发布 GitHub Release（挂成品附件）+ 建"good first issue"。

用法：
    set GITHUB_TOKEN=ghp_xxx          # 需要一个带 repo 权限的令牌
    python build/publish-release.py --repo zongxian-transfer --tag v1.0 --issues

幂等：已存在的 Release / 标题相同的 issue 会跳过，可重复执行。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"

ASSETS = [
    # (本地文件, 上传后的附件名(ASCII，GitHub 会剥掉中文), 中文标签)
    ("dist/棕仙的传输软件-自解压版.exe", "ZongxianTransfer-1.0-selfextract.exe",
     "Windows 自解压版（推荐，双击即用）"),
    ("dist/zongxian-portable-win64.zip", "zongxian-portable-win64.zip",
     "Windows 便携包（解压即用）"),
    ("dist/swiftdrop.html", "swiftdrop.html",
     "网页版单文件（直接发给朋友）"),
    ("dist/zongxian-transfer-guide.md", "zongxian-transfer-guide.md",
     "使用说明"),
]

ISSUES = [
    ("把 build/e2e 里硬编码的本机路径改成相对路径",
     "现在 `build/e2e/*.mjs` 里有形如 `const ROOT = 'D:\\\\文档\\\\ai001';` 的本机绝对路径，别人克隆后跑不起来。\n\n"
     "**要做的事**：改成按脚本位置推导，例如\n"
     "```js\nimport path from 'node:path';\nimport { fileURLToPath, pathToFileURL } from 'node:url';\n"
     "const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');\n```\n"
     "并确保改完 `node --check` 每个文件。涉及约 9 个脚本。\n\n"
     "_难度：低；这是很好的第一个 PR。_",
     ["good first issue", "help wanted"]),
    ("中继服务器：加 Docker 镜像与 TLS 部署文档",
     "`src/relay/relay_server.py` 已经能用（P2P 打洞失败时自动回退，AES-GCM 加密），但缺少：\n\n"
     "1. Dockerfile / docker-compose 示例；\n2. TLS（wss）配置示例，例如用 Caddy 反代；\n3. 一段「5 分钟部署到一台 5 元/月 VPS」的文档；\n4. 客户端设置里的地址格式说明（`wss://你的域名:端口`）。\n\n"
     "_难度：中。_",
     ["help wanted", "documentation"]),
    ("macOS / Linux 适配",
     "桌面版是纯标准库 Python，理论上跨平台，但目前只在 Windows 实测。需要：\n\n"
     "1. 把 `foldericon.py`（Windows 的 desktop.ini + SHChangeNotify）和 `autostart.py`（HKCU\\\\...\\\\Run）做成平台分支；\n"
     "2. macOS/Linux 下的开机自启与「同步文件夹图标」方案；\n"
     "3. GUI 在 Linux 下的字体/DPI 适配；\n"
     "4. 打包脚本（PyInstaller spec / .app / AppImage）。\n\n"
     "_难度：高，但收益很大。_",
     ["help wanted", "enhancement"]),
    ("浏览器端：研究多流/更好的背压以提升丢包链路吞吐",
     "网页版走 WebRTC 单条 SCTP 流，在丢包链路上吞吐会塌到几十 KB/s（实测 30~60 KB/s）。\n\n"
     "可以尝试：多 DataChannel 并行（注意 SCTP 共享拥塞窗口，未必有效，请用数据说话）、调整分片与背压阈值、"
     "或在 `transfer.js` 里加入更聪明的发送调度。**请附上前后对比的实测数据**（可用内置的「连接诊断」面板取数）。\n\n"
     "_难度：中。_",
     ["enhancement", "performance"]),
    ("英文界面与英文文档",
     "目前界面与文档以中文为主。希望：\n\n"
     "1. `src/web/index.html` 的文案支持中英切换（或英文版）；\n"
     "2. CLI `--help` 英文；\n"
     "3. README 提供完整英文版（现在顶部只有折叠摘要）。\n\n"
     "_难度：低~中，适合第一次参与。_",
     ["good first issue", "documentation", "i18n"]),
]


def call(method, url, token, payload=None, raw=None, ctype=None):
    data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", ctype or "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(body) if body.strip().startswith(("{", "[")) else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"message": body[:300]}
    except urllib.error.URLError as e:
        return 0, {"message": str(e)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tag", default="v1.0")
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    ap.add_argument("--issues", action="store_true", help="同时创建 good first issue")
    ap.add_argument("--assets", action="store_true", default=True)
    args = ap.parse_args()
    if not args.token:
        print("缺少令牌：设环境变量 GITHUB_TOKEN")
        return 2

    st, me = call("GET", f"{API}/user", args.token)
    if st != 200:
        print(f"令牌无效（HTTP {st}）：{me.get('message')}")
        return 2
    user = args.user or me["login"]
    print(f"已认证：{user}")

    # 1) Release
    st, rel = call("GET", f"{API}/repos/{user}/{args.repo}/releases/tags/{args.tag}", args.token)
    if st == 200:
        print(f"Release {args.tag} 已存在，跳过创建（{rel.get('html_url')}）")
    else:
        notes_path = os.path.join(ROOT, "docs", "release-notes-v1.0.md")
        body = open(notes_path, encoding="utf-8").read() if os.path.isfile(notes_path) else ""
        st, rel = call("POST", f"{API}/repos/{user}/{args.repo}/releases", args.token, {
            "tag_name": args.tag,
            "target_commitish": "main",
            "name": f"棕仙的传输软件 {args.tag}",
            "body": body,
            "draft": False,
            "prerelease": False,
        })
        if st not in (200, 201):
            print(f"创建 Release 失败（HTTP {st}）：{rel.get('message')}")
            return 1
        print(f"Release 已创建：{rel.get('html_url')}")

    # 2) 附件
    if args.assets:
        st, have = call("GET", f"{API}/repos/{user}/{args.repo}/releases/{rel['id']}/assets", args.token)
        names = {a["name"] for a in have} if isinstance(have, list) else set()
        for rel_path, asset_name, label in ASSETS:
            p = os.path.join(ROOT, rel_path.replace("/", os.sep))
            if not os.path.isfile(p):
                print(f"  跳过（文件不存在）：{rel_path}")
                continue
            if asset_name in names:
                print(f"  已存在，跳过：{asset_name}")
                continue
            raw = open(p, "rb").read()
            ctype = mimetypes.guess_type(asset_name)[0] or "application/octet-stream"
            st, up = call("POST",
                          f"{UPLOADS}/repos/{user}/{args.repo}/releases/{rel['id']}/assets"
                          f"?name={urllib.parse.quote(asset_name)}&label={urllib.parse.quote(label)}",
                          args.token, raw=raw, ctype=ctype)
            print(f"  {'已上传' if st in (200, 201) else '上传失败'} {asset_name}"
                  f"（{len(raw) / 1048576:.2f} MB）" + ("" if st in (200, 201) else f"：{up.get('message')}"))

    # 3) good first issue
    if args.issues:
        st, existing = call("GET", f"{API}/repos/{user}/{args.repo}/issues?state=all&per_page=100", args.token)
        titles = {i["title"] for i in existing} if isinstance(existing, list) else set()
        for title, body, labels in ISSUES:
            if title in titles:
                print(f"  issue 已存在，跳过：{title}")
                continue
            st, r = call("POST", f"{API}/repos/{user}/{args.repo}/issues", args.token,
                         {"title": title, "body": body, "labels": labels})
            print(f"  {'已创建' if st in (200, 201) else '创建失败'} issue：{title}"
                  + ("" if st in (200, 201) else f"（{r.get('message')}）"))
    return 0


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  （放这里避免遮挡上面的 import 顺序）

    raise SystemExit(main())
