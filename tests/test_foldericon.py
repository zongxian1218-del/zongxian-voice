"""棕仙的传输软件 —— 同步文件夹图标标记（desktop.ini）自测（纯标准库）。

    & "<python>" D:\\文档\\ai001\\tests\\test_foldericon.py

覆盖 6 项：
    1. mark_folder 写出的 desktop.ini 内容正确（IconResource / InfoTip）
    2. desktop.ini 带 HIDDEN|SYSTEM 属性，文件夹本身带 SYSTEM 属性
    3. mark 可重复（幂等，不重复写文件）
    4. unmark_folder 清理干净（文件没了、SYSTEM 属性也没了）
    5. resolve_icon() 返回存在的绝对路径（或明确返回空）
    6. 异常路径不崩：不存在的目录、只读/被占用的 desktop.ini

全程只在临时目录里折腾，不碰用户的真实文件夹。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

TMP = tempfile.mkdtemp(prefix="zongxian-icon-test-")
# 隔离配置目录：resolve_icon() 往 %APPDATA%\ZongxianTransfer 复制图标，
# 自测不该往真实用户目录里写东西
os.environ["SWIFTDROP_APPDATA_DIR"] = os.path.join(TMP, "appdata")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from swiftdrop import APP_NAME, VERSION                          # noqa: E402
from swiftdrop import foldericon, paths                          # noqa: E402

ICON = os.path.join(ROOT, "dist", "zongxian-synced.ico")

# 先解析一次，让配置目录里那份副本就位（is_marked() 等会依赖它）
STABLE_ICON = foldericon.resolve_icon(refresh=True) or ICON

RESULTS: list[tuple[str, str, str]] = []

FILE_ATTRIBUTE_HIDDEN = foldericon.FILE_ATTRIBUTE_HIDDEN
FILE_ATTRIBUTE_SYSTEM = foldericon.FILE_ATTRIBUTE_SYSTEM
INVALID = foldericon.FILE_ATTRIBUTE_INVALID


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"== {title}")
    print("=" * 78, flush=True)


def record(name: str, status: str, detail: str) -> None:
    RESULTS.append((name, status, detail))
    print(f"\n---- [{status}] {name}\n     {detail}", flush=True)


def run_test(name: str, fn) -> None:
    banner(name)
    t0 = time.monotonic()
    try:
        detail = fn()
        record(name, "PASS", detail)
    except Exception as exc:                                     # noqa: BLE001
        traceback.print_exc()
        record(name, "FAIL", f"{type(exc).__name__}: {exc}")
    print(f"     ({time.monotonic() - t0:.2f}s)", flush=True)


def attrs_of(path: str) -> int:
    val = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    return INVALID if val == INVALID else int(val)


def describe(attrs: int) -> str:
    if attrs == INVALID:
        return "INVALID(不存在?)"
    names = []
    for flag, name in ((0x01, "R"), (0x02, "H"), (0x04, "S"), (0x10, "D"),
                       (0x20, "A")):
        if attrs & flag:
            names.append(name)
    return f"0x{attrs:02X} [{'+'.join(names) or '-'}]"


def new_folder(name: str) -> str:
    path = os.path.join(TMP, name)
    os.makedirs(path, exist_ok=True)
    return path


def read_ini(folder: str) -> str:
    with open(foldericon.desktop_ini_path(folder), "r",
              encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


# ==========================================================================
# 1. desktop.ini 内容
# ==========================================================================

def test_mark_content() -> str:
    folder = new_folder("同步盘")
    ico = foldericon.mark_folder(folder, STABLE_ICON, "棕仙的传输软件 · 已开启同步")
    assert ico == os.path.abspath(STABLE_ICON), ico
    ini = foldericon.desktop_ini_path(folder)
    assert os.path.isfile(ini), f"没有写出 {ini}"
    text = read_ini(folder)
    print("desktop.ini 内容:", flush=True)
    for line in text.splitlines():
        print("   |" + line, flush=True)
    assert "[.ShellClassInfo]" in text, text
    assert f"IconResource={os.path.abspath(STABLE_ICON)},0" in text, text
    assert "InfoTip=棕仙的传输软件 · 已开启同步" in text, text
    # 默认提示语也应当带上产品名
    assert APP_NAME in foldericon.DEFAULT_TIP, foldericon.DEFAULT_TIP
    return (f"{ini} 内容为 [.ShellClassInfo] / "
            f"IconResource={STABLE_ICON},0 / InfoTip=棕仙的传输软件 · 已开启同步")


# ==========================================================================
# 2. 属性（Explorer 读 desktop.ini 的前提）
# ==========================================================================

def test_attributes() -> str:
    folder = new_folder("照片 备份")
    ini = foldericon.desktop_ini_path(folder)
    before_dir = attrs_of(folder)
    foldericon.mark_folder(folder, STABLE_ICON)
    dir_attrs = attrs_of(folder)
    ini_attrs = attrs_of(ini)
    print(f"文件夹属性: {describe(before_dir)} → {describe(dir_attrs)}", flush=True)
    print(f"desktop.ini 属性: {describe(ini_attrs)}", flush=True)
    assert dir_attrs != INVALID and ini_attrs != INVALID
    assert dir_attrs & FILE_ATTRIBUTE_SYSTEM, \
        f"文件夹没有 SYSTEM 属性，Explorer 不会读图标: {describe(dir_attrs)}"
    assert ini_attrs & FILE_ATTRIBUTE_HIDDEN, describe(ini_attrs)
    assert ini_attrs & FILE_ATTRIBUTE_SYSTEM, describe(ini_attrs)
    assert foldericon.is_marked(folder) is True
    return (f"文件夹 {describe(dir_attrs)}（SYSTEM 已置位）、"
            f"desktop.ini {describe(ini_attrs)}（HIDDEN|SYSTEM）")


# ==========================================================================
# 3. 幂等
# ==========================================================================

def test_idempotent() -> str:
    folder = new_folder("幂等目录")
    ini = foldericon.desktop_ini_path(folder)
    foldericon.mark_folder(folder, ICON)
    first = read_ini(folder)
    stamp1 = os.stat(ini).st_mtime_ns
    time.sleep(0.05)
    foldericon.mark_folder(folder, ICON)
    foldericon.mark_folder(folder, ICON)
    stamp2 = os.stat(ini).st_mtime_ns
    assert read_ini(folder) == first, "重复 mark 改了内容"
    assert stamp1 == stamp2, f"重复 mark 重写了文件（mtime {stamp1} → {stamp2}）"
    # 换一个图标路径时应当真的更新
    other = os.path.join(TMP, "另一个.ico")
    shutil.copyfile(ICON, other)
    foldericon.mark_folder(folder, other)
    assert f"IconResource={os.path.abspath(other)},0" in read_ini(folder)
    assert os.stat(ini).st_mtime_ns != stamp2, "换了图标却没有重写"
    return (f"同一图标重复 mark 三次，desktop.ini 的 mtime 不变"
            f"（{stamp1}）；换成另一个 ico 后确实重写")


# ==========================================================================
# 4. unmark 清理干净
# ==========================================================================

def test_unmark() -> str:
    folder = new_folder("待取消")
    ini = foldericon.desktop_ini_path(folder)
    baseline_dir = attrs_of(folder)
    foldericon.mark_folder(folder, ICON)
    assert os.path.isfile(ini) and attrs_of(folder) & FILE_ATTRIBUTE_SYSTEM
    changed = foldericon.unmark_folder(folder)
    assert changed is True, "unmark 报告没有任何东西被清理"
    assert not os.path.exists(ini), f"{ini} 还在"
    after = attrs_of(folder)
    print(f"文件夹属性: {describe(baseline_dir)} → mark → "
          f"{describe(after)}（unmark 后）", flush=True)
    assert not (after & FILE_ATTRIBUTE_SYSTEM), \
        f"文件夹的 SYSTEM 属性没有摘掉: {describe(after)}"
    assert after == baseline_dir, f"{describe(baseline_dir)} != {describe(after)}"
    assert foldericon.is_marked(folder) is False
    # 再取消一次不应当报错，且报告「没东西可清」
    assert foldericon.unmark_folder(folder) is False
    return (f"unmark 后 desktop.ini 消失、文件夹属性回到 "
            f"{describe(after)}（与标记前的 {describe(baseline_dir)} 一致），"
            f"再 unmark 一次返回 False 不报错")


# ==========================================================================
# 5. resolve_icon
# ==========================================================================

def test_resolve_icon() -> str:
    # 先把复制目标删掉，逼 resolve_icon 走「从别处复制一份过来」的分支
    stable = paths.synced_icon_path()
    assert stable.lower().startswith(TMP.lower()), \
        f"图标复制目标没被隔离: {stable}"
    if os.path.isfile(stable):
        os.remove(stable)
    icon = foldericon.resolve_icon(refresh=True)
    if not icon:
        print("没有找到任何 ico，resolve_icon() 返回空字符串（调用方会跳过标记）",
              flush=True)
        return "本机没有可用的 zongxian-synced.ico，resolve_icon() 明确返回 \"\"（不抛异常）"
    assert os.path.isabs(icon), icon
    assert os.path.isfile(icon), f"resolve_icon 返回了不存在的路径: {icon}"
    size = os.path.getsize(icon)
    cached = foldericon.resolve_icon()
    assert cached == icon, f"两次调用结果不一致：{cached} != {icon}"
    print(f"resolve_icon() → {icon}（{size} 字节，稳定路径={icon == stable}）",
          flush=True)
    notes = [f"返回稳定的绝对路径 {icon}（{size} 字节），二次调用结果一致"]
    if icon == stable:
        notes.append(f"并且确实从别处复制到了配置目录 {stable}")
    return "；".join(notes)


# ==========================================================================
# 6. 异常路径不崩
# ==========================================================================

def test_error_paths() -> str:
    notes: list[str] = []

    # a) 不存在的目录 → FolderIconError，且消息是人话
    missing = os.path.join(TMP, "并不存在", "子目录")
    try:
        foldericon.mark_folder(missing, ICON)
        raise AssertionError("对不存在的目录竟然成功了")
    except foldericon.FolderIconError as exc:
        notes.append(f"不存在的目录 → FolderIconError: {exc}")
        print(notes[-1], flush=True)

    # b) 图标文件不存在 → FolderIconError
    folder = new_folder("坏图标")
    try:
        foldericon.mark_folder(folder, os.path.join(TMP, "没有这个.ico"))
        raise AssertionError("对不存在的图标竟然成功了")
    except foldericon.FolderIconError as exc:
        notes.append(f"图标缺失 → FolderIconError: {exc}")
        print(notes[-1], flush=True)

    # c) desktop.ini 被别的句柄占着（模拟只读/云盘目录）：
    #    要么抛 FolderIconError，要么正常写出——**不能留下半成品**
    folder2 = new_folder("被占用")
    ini = foldericon.desktop_ini_path(folder2)
    with open(ini, "wb") as holder:
        holder.write(b"[.ShellClassInfo]\r\n")
        holder.flush()
        try:
            foldericon.mark_folder(folder2, STABLE_ICON)
            notes.append("desktop.ini 被占用时仍写出成功（CPython 的文件共享宽松）")
        except foldericon.FolderIconError as exc:
            notes.append(f"desktop.ini 被占用 → FolderIconError: {exc}")
        print(notes[-1], flush=True)
    if os.path.isfile(ini):
        text = read_ini(folder2)
        assert "IconResource=" in text, f"占用场景留下了半成品: {text!r}"
        assert attrs_of(ini) & FILE_ATTRIBUTE_HIDDEN, "占用场景没设上隐藏属性"

    # c2) 只读目录（FILE_ATTRIBUTE_READONLY）→ 走异常分支也不能崩
    folder_ro = new_folder("只读目录")
    ctypes.windll.kernel32.SetFileAttributesW(str(folder_ro), 0x01)
    try:
        try:
            foldericon.mark_folder(folder_ro, STABLE_ICON)
            notes.append("只读目录仍标记成功（目录的 R 属性不阻止建文件）")
        except foldericon.FolderIconError as exc:
            notes.append(f"只读目录 → FolderIconError: {exc}")
        print(notes[-1], flush=True)
    finally:
        ctypes.windll.kernel32.SetFileAttributesW(str(folder_ro), 0x10)

    # d) unmark 一个压根没有 desktop.ini 的目录 → 不抛异常
    folder3 = new_folder("没标记过")
    assert foldericon.unmark_folder(folder3) is False
    notes.append("未标记过的目录 unmark → 返回 False，不抛异常")
    print(notes[-1], flush=True)
    return "；".join(notes)


# ==========================================================================
# 主流程
# ==========================================================================

def main() -> int:
    banner(f"{APP_NAME} {VERSION} 文件夹图标标记自测")
    print(f"Python    : {sys.version.split()[0]} @ {sys.executable}", flush=True)
    print(f"临时根目录: {TMP}", flush=True)
    print(f"测试图标  : {ICON}（存在={os.path.isfile(ICON)}）", flush=True)

    try:
        run_test("1. mark_folder 写出的 desktop.ini 内容", test_mark_content)
        run_test("2. desktop.ini 与文件夹的属性", test_attributes)
        run_test("3. mark 幂等（重复调用不重写）", test_idempotent)
        run_test("4. unmark_folder 清理干净（可逆）", test_unmark)
        run_test("5. resolve_icon() 返回稳定绝对路径", test_resolve_icon)
        run_test("6. 异常路径（不存在 / 只读占用）不崩", test_error_paths)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    banner("汇总")
    passed = sum(1 for r in RESULTS if r[1] == "PASS")
    failed = [r for r in RESULTS if r[1] != "PASS"]
    for name, status, _detail in RESULTS:
        print(f"[{status}] {name}", flush=True)
    print(f"\nPASS {passed} / FAIL {len(failed)} / 共 {len(RESULTS)} 项", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n用户中断", flush=True)
        sys.exit(130)
