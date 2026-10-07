"""共享路径：配置目录 / 日志 / 锁 / 图标。

产品名是中文「棕仙的传输软件」，但**落盘目录名一律用 ASCII**
``%APPDATA%\\ZongxianTransfer``，免得各种工具（打包器、注册表脚本、
别的语言写的安装包）在 Unicode 路径上踩坑。

目录可以用环境变量覆盖，方便自测不污染真实用户配置::

    SWIFTDROP_APPDATA_DIR   覆盖配置目录（默认 %APPDATA%\\ZongxianTransfer）
    SWIFTDROP_CONFIG        覆盖 config.json 的完整路径
    SWIFTDROP_AUTOSYNC_LOG  覆盖 autosync.log 的完整路径
    SWIFTDROP_AUTOSYNC_LOCK 覆盖 autosync.lock 的完整路径
"""

from __future__ import annotations

import os

APP_DIR_NAME = "ZongxianTransfer"
CONFIG_NAME = "config.json"
LOG_NAME = "autosync.log"
LOCK_NAME = "autosync.lock"
SYNCED_ICON_NAME = "zongxian-synced.ico"

#: 打包成 exe 后，安装位置优先找这里（%LOCALAPPDATA%\Programs\棕仙的传输软件）
INSTALL_DIR_NAME = "棕仙的传输软件"
PROGRAMS_DIR_NAME = "Programs"

#: 开发期（源码运行）的图标回退位置：仓库根目录下的 dist/
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEV_ICON_PATH = os.path.join(_REPO_ROOT, "dist", SYNCED_ICON_NAME)


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def appdata_root() -> str:
    """配置目录（ASCII 名）。取不到 APPDATA 时退到用户主目录。"""
    override = _env("SWIFTDROP_APPDATA_DIR")
    if override:
        return os.path.abspath(override)
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_DIR_NAME)


def ensure_appdata() -> str:
    root = appdata_root()
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        pass
    return root


def config_path() -> str:
    return _env("SWIFTDROP_CONFIG") or os.path.join(appdata_root(), CONFIG_NAME)


def log_path() -> str:
    return _env("SWIFTDROP_AUTOSYNC_LOG") or os.path.join(appdata_root(), LOG_NAME)


def lock_path() -> str:
    return _env("SWIFTDROP_AUTOSYNC_LOCK") or os.path.join(appdata_root(), LOCK_NAME)


def synced_icon_path() -> str:
    """稳定的绝对图标路径（装好之后就在配置目录里）。"""
    return os.path.join(appdata_root(), SYNCED_ICON_NAME)


def installed_icon_path() -> str:
    """安装后的图标位置：%LOCALAPPDATA%\\Programs\\棕仙的传输软件\\zongxian-synced.ico"""
    base = (os.environ.get("LOCALAPPDATA") or "").strip()
    if not base:
        return ""
    return os.path.join(base, PROGRAMS_DIR_NAME, INSTALL_DIR_NAME, SYNCED_ICON_NAME)


def exe_dir_icon_path() -> str:
    """exe 同级目录里的图标。"""
    import sys

    exe = getattr(sys, "executable", "") or ""
    if not exe:
        return ""
    return os.path.join(os.path.dirname(os.path.abspath(exe)), SYNCED_ICON_NAME)
