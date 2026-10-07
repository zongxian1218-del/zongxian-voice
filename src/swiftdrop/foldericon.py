"""给「正在同步的文件夹」换一个专属图标（Windows 资源管理器）。

原理（Windows 的既定行为，不是黑魔法）
------------------------------------
1. 文件夹里放 ``desktop.ini``，写 ``[.ShellClassInfo]`` 的
   ``IconResource`` 与 ``InfoTip``；
2. ``desktop.ini`` 自身要带 ``HIDDEN|SYSTEM`` 属性；
3. **文件夹自身**要带 ``SYSTEM`` 属性，资源管理器才会去读它的 desktop.ini；
4. ``SHChangeNotify`` 通知外壳刷新，图标立刻变。

这三步都是 ``ctypes`` 直调 Win32，纯标准库。

可逆性
------
``unmark_folder`` 删掉 desktop.ini、摘掉文件夹的 SYSTEM 属性、再通知刷新，
文件夹恢复原样。重复 mark / unmark 都幂等。

已知限制
--------
* 只读目录、没有写权限的目录、某些云盘（OneDrive 占位符）目录可能写不进去；
  这时函数会抛 :class:`FolderIconError` 并把原因写清楚，**不会让调用方崩**。
* 想看到变化，资源管理器偶尔需要按 F5 或等一两秒。
"""

from __future__ import annotations

import os
import shutil
import sys

from . import APP_NAME
from . import paths

DEFAULT_TIP = f"{APP_NAME} · 已开启同步"

FILE_ATTRIBUTE_READONLY = 0x01
FILE_ATTRIBUTE_HIDDEN = 0x02
FILE_ATTRIBUTE_SYSTEM = 0x04
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_INVALID = 0xFFFFFFFF

SHCNE_UPDATEDIR = 0x00001000
SHCNE_ASSOCCHANGED = 0x08000000
SHCNF_PATHW = 0x0005
SHCNF_IDLIST = 0x0000

DESKTOP_INI = "desktop.ini"


class FolderIconError(RuntimeError):
    """标记/取消标记文件夹图标失败（原因已写成人话）。"""


# --------------------------------------------------------------------------
# Win32 薄封装（非 Windows 上退化成 no-op，方便在别处 import）
# --------------------------------------------------------------------------

def _kernel32():
    import ctypes

    return ctypes.windll.kernel32


def _get_attrs(path: str) -> int:
    if os.name != "nt":
        return 0
    val = _kernel32().GetFileAttributesW(str(path))
    return FILE_ATTRIBUTE_INVALID if val == FILE_ATTRIBUTE_INVALID else int(val)


def _set_attrs(path: str, attrs: int) -> None:
    if os.name != "nt":
        return
    if not _kernel32().SetFileAttributesW(str(path), int(attrs)):
        raise FolderIconError(f"设置文件属性失败（错误码 {_last_error()}）: {path}")


def _last_error() -> int:
    try:
        import ctypes

        return int(ctypes.GetLastError())
    except Exception:                                     # noqa: BLE001
        return 0


def _add_attr(path: str, flag: int) -> None:
    cur = _get_attrs(path)
    if cur == FILE_ATTRIBUTE_INVALID:
        raise FolderIconError(f"读不到文件属性（可能不存在）: {path}")
    if not cur & flag:
        _set_attrs(path, cur | flag)


def _remove_attr(path: str, flag: int) -> None:
    cur = _get_attrs(path)
    if cur == FILE_ATTRIBUTE_INVALID:
        return
    if cur & flag:
        _set_attrs(path, cur & ~flag)


def notify_shell(path: str | None = None) -> None:
    """让资源管理器刷新（失败就算了，不影响功能）。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        shell32 = ctypes.windll.shell32
        if path:
            shell32.SHChangeNotify(SHCNE_UPDATEDIR, SHCNF_PATHW, str(path), None)
        else:
            shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
    except Exception:                                     # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 图标定位
# --------------------------------------------------------------------------

_ICON_CACHE: str | None = None


def _copy_icon(src: str, dst: str) -> bool:
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        return os.path.isfile(dst) and os.path.getsize(dst) > 0
    except OSError:
        return False


def resolve_icon(refresh: bool = False) -> str:
    """按顺序找同步图标，返回**稳定的绝对路径**；找不到返回 ``""``。

    ① ``%LOCALAPPDATA%\\Programs\\棕仙的传输软件\\zongxian-synced.ico``（安装后）
    ② exe 同级目录
    ③ ``%APPDATA%\\ZongxianTransfer\\zongxian-synced.ico``
       不存在就从别处复制一份过去（这份就是最终稳定路径）
    ④ 开发期回退 ``D:\\文档\\ai001\\dist\\zongxian-synced.ico``
    """
    global _ICON_CACHE
    if _ICON_CACHE and not refresh and os.path.isfile(_ICON_CACHE):
        return _ICON_CACHE

    installed = paths.installed_icon_path()
    exe_dir = paths.exe_dir_icon_path()
    stable = paths.synced_icon_path()

    # ① 装好的位置 + ② exe 同级：有就直接用
    for cand in (installed, exe_dir):
        if cand and os.path.isfile(cand):
            _ICON_CACHE = os.path.abspath(cand)
            return _ICON_CACHE

    # ③ 配置目录里的那份（稳定路径）
    if os.path.isfile(stable):
        _ICON_CACHE = os.path.abspath(stable)
        return _ICON_CACHE

    # 从 ③ 之外的来源复制一份过去
    sources = [p for p in (exe_dir, installed) if p and os.path.isfile(p)]
    sources.append(paths.DEV_ICON_PATH)
    # 打包成 exe 时 PyInstaller 的临时解包目录也可能有
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        sources.append(os.path.join(meipass, paths.SYNCED_ICON_NAME))
    for src in sources:
        if src and os.path.isfile(src) and _copy_icon(src, stable):
            _ICON_CACHE = os.path.abspath(stable)
            return _ICON_CACHE

    # ④ 开发期回退：路径本身可用就行
    if os.path.isfile(paths.DEV_ICON_PATH):
        _ICON_CACHE = os.path.abspath(paths.DEV_ICON_PATH)
        return _ICON_CACHE

    return ""


# --------------------------------------------------------------------------
# 标记 / 取消标记
# --------------------------------------------------------------------------

def desktop_ini_path(folder: str) -> str:
    return os.path.join(os.path.abspath(folder), DESKTOP_INI)


def _write_desktop_ini(folder: str, ico_path: str, tip: str) -> str:
    ini = desktop_ini_path(folder)
    # 已经写对就不要重复写（避免无谓地改 mtime、惹同步引擎误判）
    want = (
        "[.ShellClassInfo]\r\n"
        f"IconResource={ico_path},0\r\n"
        f"InfoTip={tip}\r\n"
    )
    try:
        if os.path.isfile(ini):
            with open(ini, "r", encoding="utf-8-sig", errors="replace") as fh:
                if fh.read() == want:
                    return ini
    except OSError:
        pass
    if os.path.isfile(ini):
        # 目标是隐藏+系统文件，先摘掉属性才好覆写
        try:
            _remove_attr(ini, FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)
        except FolderIconError:
            pass
    try:
        with open(ini, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(want)
    except OSError as exc:
        raise FolderIconError(
            f"写 desktop.ini 失败（目录可能只读、被 OneDrive 接管或没有权限）：{exc}"
        ) from exc
    return ini


def _same_icon(folder: str, ico_path: str) -> bool:
    ini = desktop_ini_path(folder)
    if not os.path.isfile(ini):
        return False
    try:
        with open(ini, "r", encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return False
    return f"IconResource={ico_path},0" in text


def mark_folder(folder: str, ico_path: str | None = None,
                tip: str | None = None) -> str:
    """给文件夹打上同步图标。返回实际使用的 ico 绝对路径。

    幂等：重复调用不重复写文件（除非内容需要更新）。
    """
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise FolderIconError(f"不是目录（或不存在）: {folder}")
    tip = tip or DEFAULT_TIP
    ico = ico_path or resolve_icon()
    if not ico:
        raise FolderIconError(
            "找不到同步图标文件 zongxian-synced.ico"
            f"（找过安装目录、exe 同级、{paths.synced_icon_path()} 和开发目录）"
        )
    ico = os.path.abspath(ico)
    if not os.path.isfile(ico):
        raise FolderIconError(f"图标文件不存在: {ico}")

    if _same_icon(folder, ico) and _tk_ok(folder):
        return ico

    ini = _write_desktop_ini(folder, ico, tip)
    try:
        _add_attr(ini, FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)
        _add_attr(folder, FILE_ATTRIBUTE_SYSTEM)
    except FolderIconError as exc:
        raise FolderIconError(
            f"desktop.ini 已写，但设置属性失败（Explorer 不会读图标）：{exc}"
        ) from exc
    notify_shell(folder)
    return ico


def _tk_ok(folder: str) -> bool:
    """文件夹是否已经带上 SYSTEM 属性（Explorer 读 desktop.ini 的前提）。"""
    attrs = _get_attrs(folder)
    if attrs == FILE_ATTRIBUTE_INVALID:
        return False
    if os.name != "nt":
        return True
    return bool(attrs & FILE_ATTRIBUTE_SYSTEM)


def unmark_folder(folder: str) -> bool:
    """取消同步图标标记，恢复文件夹原样。返回是否真的有东西被清掉。"""
    folder = os.path.abspath(folder)
    changed = False
    ini = desktop_ini_path(folder)
    if os.path.isfile(ini):
        try:
            _remove_attr(ini, FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)
        except FolderIconError:
            pass
        try:
            os.remove(ini)
            changed = True
        except OSError as exc:
            raise FolderIconError(
                f"删不掉 desktop.ini（目录可能只读或被占用）：{exc}"
            ) from exc
    if os.path.isdir(folder):
        before = _get_attrs(folder)
        try:
            _remove_attr(folder, FILE_ATTRIBUTE_SYSTEM)
            if before != _get_attrs(folder):
                changed = True
        except FolderIconError:
            pass
    notify_shell(folder)
    return changed


def is_marked(folder: str) -> bool:
    """文件夹当前是否带着本产品的同步标记。"""
    if not _same_icon(os.path.abspath(folder), resolve_icon() or "\x00"):
        return False
    return _tk_ok(os.path.abspath(folder))


def reveal_hint() -> str:
    """给用户的一句提示。"""
    return "若资源管理器没立刻变，按 F5 刷新"
