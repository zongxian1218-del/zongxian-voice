#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「内置网页版」窗口：用系统自带的 Edge（Chromium 内核）把网页版当成原生应用窗口打开。

为什么这么做：与其在 Python 里重造 WebRTC，不如直接借 Windows 自带的那颗 Chromium ——
网页版的跨网 P2P、文件夹选择、文件夹同步全都能用，而且**零额外依赖、不用打包浏览器内核**。

Edge 在 Windows 10/11 上必然存在；万一找不到就退回系统默认浏览器。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser

from .paths import appdata_root


CANDIDATES = [
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 "Microsoft", "Edge", "Application", "msedge.exe"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                 "Microsoft", "Edge", "Application", "msedge.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""),
                 "Microsoft", "Edge", "Application", "msedge.exe"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                 "Google", "Chrome", "Application", "chrome.exe"),
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 "Google", "Chrome", "Application", "chrome.exe"),
]


def find_browser() -> str | None:
    """找一个可用的 Chromium 内核浏览器（优先 Edge，其次 Chrome）。"""
    for p in CANDIDATES:
        if p and os.path.isfile(p):
            return p
    # 注册表兜底（Edge 一定注册了 App Paths）
    try:
        import winreg
        for hive, sub in (
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
        ):
            try:
                k = winreg.OpenKey(hive, sub)
                v, _ = winreg.QueryValueEx(k, "")
                winreg.CloseKey(k)
                if v and os.path.isfile(v):
                    return v
            except OSError:
                continue
    except ImportError:
        pass
    for name in ("msedge", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    return None


def profile_dir() -> str | None:
    """独立浏览器配置目录；建不出来就返回 None（改用用户自己的浏览器配置）。

    有些环境（受限权限、被安全软件锁定的 %APPDATA%）会拒绝创建目录，
    这时绝不能因此让整个程序退出——只是退回默认配置而已。
    """
    d = os.path.join(appdata_root(), "browser-profile")
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        return None


def build_args(url: str, size: tuple[int, int], use_profile: bool,
               extra_args: list[str] | None = None) -> list[str]:
    args = [f"--app={url}", f"--window-size={size[0]},{size[1]}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-features=Translate,MediaRouter"]
    if use_profile:
        d = profile_dir()
        if d:
            args.append(f"--user-data-dir={d}")
    if extra_args:
        args.extend(extra_args)
    return args


def _shell_execute(exe: str, args: list[str]) -> bool:
    """用 Windows 外壳启动浏览器。

    为什么不用 subprocess：打包成 exe 后（PyInstaller）进程环境里带着打包目录，
    直接 Popen 出去的 Edge 会加载到打包目录里的 DLL 而**静默挂掉**（实测）。
    ShellExecute 由外壳（Explorer）创建进程，环境是用户原本的干净环境，最稳。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        params = " ".join(f'"{a}"' if " " in a else a for a in args)
        r = ctypes.windll.shell32.ShellExecuteW(None, "open", exe, params, None, 1)
        return int(r) > 32
    except Exception:
        return False


def clean_env() -> dict:
    """给浏览器一个干净的环境。

    打包成 exe 后（PyInstaller onedir），环境里的 PATH 会被加上打包目录，
    Edge 启动时可能从那里加载到同名 DLL 而**静默退出**（实测就是这个原因）。
    这里把打包目录、PyInstaller 变量都剔除掉。
    """
    env = dict(os.environ)
    for k in ("_MEIPASS", "_MEIPASS2", "_PYI_APPLICATION_HOME_DIR", "PYTHONHOME", "PYTHONPATH"):
        env.pop(k, None)
    bad = set()
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            bad.add(os.path.normcase(os.path.abspath(meipass)))
        exe = getattr(sys, "executable", "")
        if exe:
            bad.add(os.path.normcase(os.path.dirname(os.path.abspath(exe))))
    if bad:
        keep = []
        for p in (env.get("PATH") or "").split(os.pathsep):
            if not p.strip():
                continue
            if os.path.normcase(os.path.abspath(p)) in bad:
                continue
            keep.append(p)
        env["PATH"] = os.pathsep.join(keep)
    return env


def open_app_window(url: str, *, size: tuple[int, int] = (1220, 880),
                    use_profile: bool = True, extra_args: list[str] | None = None,
                    force_app_window: bool = False):
    """打开内置网页版。

    默认策略（最可靠）：交给系统用**默认浏览器**打开这个本地地址——任何 Windows 都能用。
    传 force_app_window=True（CLI 的 --app-window）时才尝试无地址栏的应用窗口；
    打包成 exe 后我们自己拉起浏览器在部分受限环境会被拦（实测），所以不作为默认。

    返回 (handle, exe)；handle 为 Popen 对象表示是我们拉起的窗口，None 表示交给了系统。
    """
    exe = find_browser()
    frozen = bool(getattr(sys, "frozen", False))
    if exe and (force_app_window or not frozen):
        args = build_args(url, size, use_profile, extra_args)
        for attempt_env in (clean_env(), None):
            try:
                proc = subprocess.Popen([exe] + args, close_fds=True, env=attempt_env)
                return proc, exe
            except Exception:
                continue
    # 交给系统：默认浏览器打开（ShellExecute 打开 URL 会走文件关联，最稳）
    _open_default(url)
    return None, exe


def _open_default(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


def describe() -> str:
    exe = find_browser()
    if not exe:
        return "未找到 Edge/Chrome，将使用系统默认浏览器打开"
    return f"将使用 {os.path.basename(exe)}（{exe}）作为内置浏览器内核"


def exited_quickly(proc, seconds: float = 2.5) -> bool:
    """启动进程是否很快就退了。

    Edge/Chrome 被启动后经常把窗口交给"已有实例"然后自身立刻退出，
    这时**不能**把它的退出当成"窗口关了"——否则内置服务会跟着停掉。
    """
    import time

    if proc is None:
        return True
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.2)
    return proc.poll() is not None
