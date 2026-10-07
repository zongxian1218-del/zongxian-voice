#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打包「自解压便携版」exe（NSIS，不需要 WiX）。

产物：dist\\棕仙的传输软件-自解压版.exe
默认解压到桌面，用户可自选目录；不写系统目录、不需要管理员，
个别文件被占用时**跳过而不中断**（这是和老安装包最大的区别）。
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NSIS = os.path.join(ROOT, "build", "nsis", "nsis-3.10", "makensis.exe")
SCRIPT = os.path.join(ROOT, "build", "sfx.nsi")
APPDIR = os.path.join(ROOT, "dist", "棕仙的传输软件")
ICON = os.path.join(ROOT, "dist", "zongxian-synced.ico")
APPEXE = "棕仙的传输软件.exe"
OUT = os.path.join(ROOT, "dist", "棕仙的传输软件-自解压版.exe")

TMP = os.path.join(ROOT, "tmp", "buildtemp")
os.makedirs(TMP, exist_ok=True)
os.environ["TEMP"] = TMP
os.environ["TMP"] = TMP


def _arg(flag, default):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def main() -> int:
    out = _arg("--out", OUT)
    if not os.path.exists(NSIS):
        print("缺少 NSIS：%s" % NSIS)
        return 2
    exe = os.path.join(APPDIR, APPEXE)
    if not os.path.exists(exe):
        print("缺少 %s，请先跑 build/build-exe.py" % exe)
        return 2
    if not os.path.exists(os.path.join(APPDIR, "swiftdrop.html")):
        print("缺少 dist\\棕仙的传输软件\\swiftdrop.html（网页版首页）")
        return 2
    if os.path.exists(out):
        try:
            os.remove(out)
        except OSError as exc:
            print("无法覆盖旧文件（可能正在运行）：%s" % exc)
            return 2
    args = [NSIS, "/INPUTCHARSET", "UTF8", "/V2",
            "/DAPPDIR=%s" % APPDIR, "/DOUTFILE=%s" % out,
            "/DICONFILE=%s" % ICON, "/DAPPEXE=%s" % APPEXE, SCRIPT]
    print("编译自解压版…")
    r = subprocess.run(args, cwd=ROOT)
    if r.returncode != 0 or not os.path.exists(out):
        print("编译失败，返回码 %s" % r.returncode)
        return 1
    print("完成：%s（%.1f MB）" % (out, os.path.getsize(out) / 1048576.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
