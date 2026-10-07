#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把要上传到 GitHub 的文件**复制**到一个干净目录（供网页端"拖拽上传"，不需要令牌/git）。

用法：python build/export-repo.py [目标目录]        默认：桌面\\zongxian-transfer
"""
from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INCLUDE = [
    "README.md", "CONTRIBUTING.md", "LICENSE", ".gitignore", "docs", "src", "tests",
    "build/e2e", "build/build-web.py", "build/build-exe.py",
    "build/make-sfx.py", "build/make-installer.py", "build/make-msi.py",
    "build/make-icons.py", "build/verify-desktop.py", "build/verify-diag.py",
    "build/entry.py", "build/installer.nsi", "build/sfx.nsi", "build/push-github.py",
    "build/publish-release.py",
    "dist/zongxian-synced.ico", "dist/zongxian.ico", "dist/zongxian-icon.png",
    "dist/icons", "dist/zongxian-transfer-guide.md",
]
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
                    out.append(os.path.relpath(os.path.join(dp, f), ROOT).replace(os.sep, "/"))
    return sorted(set(out))


def main() -> int:
    dest = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.expanduser("~"), "Desktop", "zongxian-transfer"))
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    files = collect()
    total = 0
    for rel in files:
        src = os.path.join(ROOT, rel.replace("/", os.sep))
        dst = os.path.join(dest, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        total += os.path.getsize(src)
    print(f"已导出 {len(files)} 个文件（{total / 1024:.0f} KB）到：\n  {dest}")
    print("把这个文件夹整个拖进 GitHub 网页的 Upload files 即可（目录结构会保留）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
