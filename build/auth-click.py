"""点被控端（sender-probe）授权窗口上的按钮 —— 供自动化测试用。

用法:
    python build/auth-click.py info              # 只打印窗口与按钮位置
    python build/auth-click.py allow             # 点「允许」（键盘勾选框保持默认不勾）
    python build/auth-click.py allow --keyboard  # 先勾「允许键盘输入」再点「允许」
    python build/auth-click.py deny              # 点「拒绝」

判定方式：**以被控端日志为准**（`本人点了「允许」（仅鼠标，键盘未勾选）` 等）。
不靠"看光标动没动"—— 用户可能同时在用这台机器，光标位置会被外部干扰。
"""
import ctypes
import sys
import time
from ctypes import wintypes

# 【必须最先做】声明 DPI 感知，否则在这台 125% 缩放的机器上：
#   GetSystemMetrics 返回虚拟化的 1536x864（真实 1920x1080），
#   GetWindowRect 返回的窗口/控件坐标也全是虚拟化的（×0.8），
#   gui.move 的归一化按 1536 算 → 点击实际落到 (x*1.25) 的位置，打偏。
# 实测踩过：想点「允许」结果打在窗口空白处。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

sys.path.insert(0, r"D:\文档\ai001\.dsh\skills\gui-automation\references")
import gui  # noqa: E402

u32 = ctypes.windll.user32

# 显式声明签名，避免 64 位下 HWND 被截断成 int
u32.FindWindowW.restype = wintypes.HWND
u32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
u32.FindWindowExW.restype = wintypes.HWND
u32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
u32.GetDlgItem.restype = wintypes.HWND
u32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(gui.RECT)]
u32.GetWindowRect.restype = wintypes.BOOL
u32.IsWindowVisible.argtypes = [wintypes.HWND]

DLG_CLASS = "ZxControlAuthDlg"
ID_ALLOW, ID_DENY, ID_KEYBOARD = 101, 102, 103


def find_dialog(timeout: float = 20.0):
    """等授权窗口出现（最多 timeout 秒）。"""
    end = time.time() + timeout
    while time.time() < end:
        h = u32.FindWindowW(DLG_CLASS, None)
        if h and u32.IsWindowVisible(h):
            return h
        time.sleep(0.3)
    return None


def child_center(hwnd):
    r = gui.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left + r.right) // 2, (r.top + r.bottom) // 2, (r.left, r.top, r.right, r.bottom)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    action = sys.argv[1].lower()

    dlg = find_dialog()
    if not dlg:
        print(f"等不到授权窗口（类 {DLG_CLASS}）—— 是不是还没触发请求？")
        return 1

    allow = u32.GetDlgItem(dlg, ID_ALLOW)
    deny = u32.GetDlgItem(dlg, ID_DENY)
    kb = u32.GetDlgItem(dlg, ID_KEYBOARD)
    for name, h in (("允许", allow), ("拒绝", deny), ("键盘勾选", kb)):
        print(f"  {name}: hwnd=0x{h:X} center={child_center(h)[:2]}")

    if action == "info":
        return 0

    if action == "allow":
        if "--keyboard" in sys.argv:
            cx, cy, _ = child_center(kb)
            gui.click(cx, cy)
            time.sleep(0.4)
            print("  已勾选「允许键盘输入」")
        target, label = allow, "允许"
    elif action == "deny":
        target, label = deny, "拒绝"
    else:
        print(f"未知动作: {action}")
        return 2

    cx, cy, rect = child_center(target)
    # 点得尽量快：用户可能同时在用这台机器，move 与左键之间留缝会被抢走
    gui.move(cx, cy)
    gui._send(gui.INPUT(type=gui.INPUT_MOUSE,
                        mi=gui.MOUSEINPUT(0, 0, 0, gui.MOUSEEVENTF_LEFTDOWN, 0, None)))
    time.sleep(0.03)
    gui._send(gui.INPUT(type=gui.INPUT_MOUSE,
                        mi=gui.MOUSEINPUT(0, 0, 0, gui.MOUSEEVENTF_LEFTUP, 0, None)))
    print(f"  已点击「{label}」@ ({cx},{cy})  rect={rect}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
