"""zx_picker.py —— 检查能否定位并操作 WebView2 的「选择共享源」弹窗

【为什么写这个】
屏幕共享的真实采集必须有人在系统弹窗里选窗口，这是唯一无法自动化的步骤。
如果我能定位到这个弹窗并程序化地选第一项，就能把"真实采集"也纳入自动化测试，
否则用户每次都要手动点，且无法复现同一序列。

做法：先在应用里触发 getDisplayMedia（--screen-probe），
然后枚举所有顶层窗口，找出弹窗候选（大小/类名/所属进程），
打印出来供判断。不猜、不硬编码。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def processes_by_name(name: str) -> set[int]:
    """返回指定进程名的所有 PID（用 tasklist，避免依赖 psutil）。"""
    import subprocess
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, errors="ignore").stdout
    pids = set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == name.lower():
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


def enum_windows() -> list[tuple[int, str, str, int, tuple[int, int, int, int]]]:
    """列出全部顶层窗口：(hwnd, 标题, 类名, pid, 矩形)。"""
    result = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        r = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        result.append((hwnd, buf.value, cls.value, pid.value,
                       (r.left, r.top, r.right - r.left, r.bottom - r.top)))
        return True

    user32.EnumWindows(cb, 0)
    return result


def main() -> None:
    app_pids = processes_by_name("ZongxianVoice.exe")
    webview_pids = processes_by_name("msedgewebview2.exe")

    print(f"ZongxianVoice PID: {sorted(app_pids)}")
    print(f"msedgewebview2 PID 数: {len(webview_pids)}")
    print()
    print("=== 可见的顶层窗口（按所属进程分组）===")

    interesting = []
    for hwnd, title, cls, pid, (x, y, w, h) in enum_windows():
        if not user32.IsWindowVisible(hwnd):
            continue
        owner = ("APP" if pid in app_pids
                 else "WEBVIEW" if pid in webview_pids
                 else "")
        if owner:
            interesting.append((owner, hwnd, title, cls, pid, x, y, w, h))

    for owner, hwnd, title, cls, pid, x, y, w, h in sorted(interesting):
        print(f"[{owner:7}] pid={pid:<6} {w:>5}x{h:<5} @({x},{y})  "
              f"cls={cls:<32} title={title!r}")

    if not interesting:
        print("  （没有找到应用或 WebView2 的可见窗口）")

    print()
    print("=== 判定 ===")
    print("  如果弹窗出现在上面的列表里（多半属于 WEBVIEW 或 APP 进程，")
    print("  且尺寸接近整个应用窗口），就可以用键盘/鼠标自动化它。")
    print("  如果列表里没有弹窗，说明它在 WebView2 内部的合成器里渲染，")
    print("  没有独立的顶层窗口 —— 那种情况下无法从外部自动化，")


if __name__ == "__main__":
    main()
