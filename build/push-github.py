#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用 GitHub 官方 API 把本仓库的**源码与文档**一次性提交上去（不需要安装 git）。

用法：
    python build/push-github.py --user <GitHub用户名> --repo <仓库名> --token <令牌> [--public]

说明：
  * 令牌只需要目标仓库的 **Contents: Read and write** 权限（细粒度令牌）；
    如果要让脚本顺带创建仓库，还需要 **Administration: Read and write**（或直接用经典令牌的 repo 权限）。
  * 只上传源码/文档/测试/图标：**不含** 构建产物、虚拟环境、安卓签名私钥、测试残留（见 .gitignore）。
  * 一次调用生成一个 commit（Git Data API：blobs → tree → commit → ref）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://api.github.com"

#: 明确要上传的路径（文件或目录）；目录会递归收集
INCLUDE = [
    "README.md",
    "CONTRIBUTING.md",
    "LICENSE",
    ".gitignore",
    "docs",
    "src",
    "tests",
    "build/e2e",
    "build/build-web.py",
    "build/build-exe.py",
    "build/make-sfx.py",
    "build/make-installer.py",
    "build/make-msi.py",
    "build/make-icons.py",
    "build/verify-desktop.py",
    "build/verify-diag.py",
    "build/push-github.py",
    "build/publish-release.py",
    "build/entry.py",
    "build/installer.nsi",
    "build/sfx.nsi",
    "build/android-README.md",
    "dist/zongxian-synced.ico",
    "dist/zongxian.ico",
    "dist/zongxian-icon.png",
    "dist/icons",
    "dist/zongxian-transfer-guide.md",
]

#: 黑名单：即使被上面的目录递归收进来也不要
EXCLUDE_DIRS = {
    "__pycache__", "node_modules", ".git", "tmp", "swiftdrop-recv",
    "android", "android-sdk", "jdk", "nsis", "wix", "innosetup", "downloads",
    "qrvenv", "pack-venv", "net-venv", "pyi-onedir", "msi-out",
}
EXCLUDE_EXT = {".exe", ".zip", ".msi", ".apk", ".pyc", ".log", ".part"}


def collect() -> list[str]:
    out: list[str] = []
    for item in INCLUDE:
        p = os.path.join(ROOT, item.replace("/", os.sep))
        if os.path.isfile(p):
            out.append(item)
        elif os.path.isdir(p):
            for dp, dn, fn in os.walk(p):
                dn[:] = [d for d in dn if d not in EXCLUDE_DIRS]
                for f in fn:
                    if os.path.splitext(f)[1].lower() in EXCLUDE_EXT:
                        continue
                    fp = os.path.join(dp, f)
                    rel = os.path.relpath(fp, ROOT).replace(os.sep, "/")
                    out.append(rel)
    return sorted(set(out))


def call(method: str, url: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"message": body[:400]}
    except urllib.error.URLError as e:
        return 0, {"message": str(e)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="", help="仓库所有者（默认 = 令牌对应的账号）")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""),
                    help="访问令牌；也可用环境变量 GITHUB_TOKEN（推荐，避免出现在命令行里）")
    ap.add_argument("--public", action="store_true", help="公开仓库（默认私有）")
    ap.add_argument("--message", default="棕仙的传输软件：源码与文档")
    ap.add_argument("--dry-run", action="store_true", help="只列出会上传的文件，不联网")
    args = ap.parse_args()
    if not args.dry_run and not args.token:
        print("缺少令牌：用 --token 或环境变量 GITHUB_TOKEN 提供")
        return 2

    files = collect()
    total = sum(os.path.getsize(os.path.join(ROOT, f.replace("/", os.sep))) for f in files)
    print(f"准备上传 {len(files)} 个文件，合计 {total / 1024:.0f} KB")
    if args.dry_run:
        for f in files:
            print("   ", f)
        return 0

    st, me = call("GET", f"{API}/user", args.token)
    if st != 200:
        print(f"令牌校验失败（HTTP {st}）：{me.get('message')}")
        return 2
    print(f"已认证：{me.get('login')}")
    if not args.user:
        args.user = me.get("login") or ""
    if not args.user:
        print("拿不到账号名，请显式传 --user")
        return 2

    # LICENSE 里的版权人占位符填成 GitHub 用户名（跟着仓库走，不泄露个人邮箱）
    lic = os.path.join(ROOT, "LICENSE")
    if os.path.isfile(lic):
        t = open(lic, encoding="utf-8").read()
        if "__AUTHOR__" in t:
            open(lic, "w", encoding="utf-8").write(t.replace("__AUTHOR__", me.get("login") or ""))
            print(f"LICENSE 版权人已填：{me.get('login')}")

    # 仓库不存在就创建（需要 Administration 权限；失败则提示手动建）
    st, repo = call("GET", f"{API}/repos/{args.user}/{args.repo}", args.token)
    if st == 404:
        print(f"仓库 {args.user}/{args.repo} 不存在，尝试创建…")
        st, repo = call("POST", f"{API}/user/repos", args.token, {
            "name": args.repo,
            "private": not args.public,
            "auto_init": False,
            "description": "朋友之间传大文件：网页版单文件 / Windows 桌面版 / 局域网多流 TCP + 异地组网直传",
        })
        if st not in (200, 201):
            print(f"创建仓库失败（HTTP {st}）：{repo.get('message')}")
            print("  提示：请先在网页上手动新建空仓库（不要勾选 README），再重跑本脚本。")
            return 2
        print("仓库已创建")
    elif st != 200:
        print(f"读取仓库失败（HTTP {st}）：{repo.get('message')}")
        return 2

    # 空仓库（刚创建、没有初始提交）不能用 Git Data API：会返回 409 Git Repository is empty。
    # 先用 Contents API 提交一次 README，把默认分支 main 生出来。
    st, _ = call("GET", f"{API}/repos/{args.user}/{args.repo}/git/ref/heads/main", args.token)
    if st != 200:
        print("空仓库：先创建初始提交（README）以生成 main 分支…")
        readme = os.path.join(ROOT, "README.md")
        with open(readme, "rb") as fh:
            content = base64.b64encode(fh.read()).decode()
        st2, r2 = call("PUT", f"{API}/repos/{args.user}/{args.repo}/contents/README.md",
                       args.token, {"message": "初始化仓库", "content": content, "branch": "main"})
        if st2 not in (200, 201):
            print(f"初始化失败（HTTP {st2}）：{r2.get('message')}")
            return 1
        print("初始提交完成")

    # 1) blobs
    tree = []
    for i, rel in enumerate(files, 1):
        fp = os.path.join(ROOT, rel.replace("/", os.sep))
        with open(fp, "rb") as fh:
            raw = fh.read()
        st, blob = call("POST", f"{API}/repos/{args.user}/{args.repo}/git/blobs", args.token, {
            "content": base64.b64encode(raw).decode(),
            "encoding": "base64",
        })
        if st not in (200, 201):
            print(f"  上传 {rel} 失败（HTTP {st}）：{blob.get('message')}")
            return 1
        tree.append({"path": rel, "mode": "100755" if fp.endswith(".py") else "100644",
                     "type": "blob", "sha": blob["sha"]})
        if i % 10 == 0 or i == len(files):
            print(f"  已上传 {i}/{len(files)}")

    # 2) tree
    st, t = call("POST", f"{API}/repos/{args.user}/{args.repo}/git/trees", args.token,
                 {"tree": tree})
    if st not in (200, 201):
        print(f"创建 tree 失败：{t.get('message')}")
        return 1

    # 3) commit（有历史就在其之上，没有就是首个提交）
    #    作者邮箱用 GitHub 的 noreply，避免把个人邮箱写进公开提交历史
    login = me.get("login") or args.user
    who = {"name": login, "email": f"{login}@users.noreply.github.com"}
    parents = []
    st, ref = call("GET", f"{API}/repos/{args.user}/{args.repo}/git/ref/heads/main", args.token)
    if st == 200:
        parents = [ref["object"]["sha"]]
    payload = {"message": args.message, "tree": t["sha"], "author": who, "committer": who}
    if parents:
        payload["parents"] = parents
    st, c = call("POST", f"{API}/repos/{args.user}/{args.repo}/git/commits", args.token, payload)
    if st not in (200, 201):
        print(f"创建 commit 失败：{c.get('message')}")
        return 1

    # 4) ref
    if parents:
        st, _ = call("PATCH", f"{API}/repos/{args.user}/{args.repo}/git/refs/heads/main",
                     args.token, {"sha": c["sha"], "force": False})
    else:
        st, _ = call("POST", f"{API}/repos/{args.user}/{args.repo}/git/refs", args.token,
                     {"ref": "refs/heads/main", "sha": c["sha"]})
    if st not in (200, 201):
        print(f"更新分支失败（HTTP {st}）")
        return 1

    print(f"完成：https://github.com/{args.user}/{args.repo}")
    print(f"  提交 {c['sha'][:8]}，{'公开' if args.public else '私有'}仓库")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
