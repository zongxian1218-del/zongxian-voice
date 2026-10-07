"""capture-window.py —— 把指定进程的窗口提到前台并截图

内置 Python + Pillow，不依赖第三方库。

【为什么需要"绕过前台锁"这一步】
Windows 有前台锁定（foreground lock）：非前台进程调用
SetForegroundWindow 往往**静默失败**，窗口仍被别的窗口盖着。
结果就是：窗口坐标取对了，截出来的却是别的程序。

绕过的标准做法是 AttachThreadInput —— 把自己的线程输入队列
附加到当前前台线程上，此时本线程就被视为"前台线程"，
SetForegroundWindow 才会生效。用完要记得分离。

这个坑很隐蔽：第一次截图取到了 B 站页面的画面，看起来像"截图脚本坏了"，
实际是应用窗口在下面一层。

用法:
    python capture-window.py <输出路径> [进程名]
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
kernel32 = ctypes.windll.kernel32

DWMWA_EXTENDED_FRAME_BOUNDS = 9
SW_RESTORE = 9


def find_pid(process_name: str) -> int | None:
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, errors="ignore",
    ).stdout
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == process_name.lower():
            try:
                return int(parts[1])
            except ValueError:
                continue
    return None


def find_main_window(pid: int) -> int:
    """列出该进程所有可见且有标题的顶层窗口，取第一个。

    比 Process.MainWindowHandle 可靠：后者在多窗口或特殊框架下常返回 0。
    """
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lparam):
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowTextLengthW(hwnd) == 0:
            return True
        found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else 0


def bring_to_front(hwnd: int) -> None:
    """绕过前台锁把窗口提到最前。"""
    fg = user32.GetForegroundWindow()
    thread_fg = user32.GetWindowThreadProcessId(fg, None)
    thread_me = kernel32.GetCurrentThreadId()

    attached = False
    if thread_fg and thread_fg != thread_me:
        attached = bool(user32.AttachThreadInput(thread_fg, thread_me, True))

    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.SetActiveWindow(hwnd)

    if attached:
        user32.AttachThreadInput(thread_fg, thread_me, False)

    # 等窗口真正重绘完成，否则可能截到动画中间态
    time.sleep(1.2)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rect = RECT()
    # 优先用 DWM 的"不含投影边框"矩形，否则截图会带一圈透明边
    hr = dwmapi.DwmGetWindowAttribute(
        wt.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(rect), ctypes.sizeof(rect),
    )
    if hr != 0:
        user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def main() -> int:
    out_path = sys.argv[1] if len(sys.argv) > 1 else "window.png"
    process_name = sys.argv[2] if len(sys.argv) > 2 else "ZongxianVoice.exe"

    pid = find_pid(process_name)
    if pid is None:
        print(f"找不到进程 {process_name}")
        return 1

    hwnd = find_main_window(pid)
    if not hwnd:
        print(f"进程 {pid} 没有可见的主窗口")
        return 1

    bring_to_front(hwnd)

    fg = user32.GetForegroundWindow()
    if fg != hwnd:
        print(f"警告: 窗口未能提到前台 (foreground={fg}, target={hwnd})")

    left, top, right, bottom = window_rect(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        print(f"窗口尺寸异常: {w}x{h}")
        return 1

    from PIL import ImageGrab

    ImageGrab.grab(bbox=(left, top, right, bottom)).save(out_path)
    print(f"已截图: {out_path}  ({w}x{h})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
