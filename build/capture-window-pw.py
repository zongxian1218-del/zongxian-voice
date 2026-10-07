"""capture-window-pw.py —— 用 PrintWindow 直接抓窗口内容，不受遮挡影响

【为什么需要这个而不是 ImageGrab】
前面的 capture-window.py 走的是
    SetForegroundWindow → ImageGrab.grab(屏幕区域)
这条路。它有两个问题：
  1) Windows 的前台锁定会让 SetForegroundWindow **静默失败**，
     窗口仍被盖着，于是截到的是别的程序（本会话踩过两次）。
  2) 即使提到前台，也会打扰用户正在做的事。

PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT) 让窗口把**自己的内容**
画到我们提供的 DC 上，与它在屏幕上的层级无关。对 WinUI 3 这类
DirectComposition 渲染的窗口，必须带 PW_RENDERFULLCONTENT(2)，
否则只会得到一片黑。

用法:
    python capture-window-pw.py <输出目录> [窗口标题关键字]
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi

PW_RENDERFULLCONTENT = 2
DWMWA_EXTENDED_FRAME_BOUNDS = 9
SRCCOPY = 0x00CC0020


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def list_windows(keyword: str):
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if keyword in buf.value:
            out.append((hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = RECT()
    hr = dwmapi.DwmGetWindowAttribute(
        wt.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(rect), ctypes.sizeof(rect))
    if hr != 0:
        user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def capture(hwnd: int, out_path: str) -> bool:
    left, top, right, bottom = window_rect(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        print(f"窗口尺寸异常 {w}x{h}")
        return False

    hdc_window = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
    hbm = gdi32.CreateCompatibleBitmap(hdc_window, w, h)
    gdi32.SelectObject(hdc_mem, hbm)

    ok = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
    if not ok:
        # 某些窗口不支持 FULLCONTENT，退回不带标志的版本
        ok = user32.PrintWindow(hwnd, hdc_mem, 0)

    # 取出位图数据后交给 Pillow 保存（避免自己写 BMP 文件头）
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h          # 负数 = 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0      # BI_RGB

    buf_size = w * h * 4
    buf = ctypes.create_string_buffer(buf_size)
    gdi32.GetDIBits(hdc_mem, hbm, 0, h, buf, ctypes.byref(bmi), 0)

    from PIL import Image
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
    img.convert("RGB").save(out_path)

    gdi32.DeleteObject(hbm)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_window)
    return True


def main() -> int:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    keyword = sys.argv[2] if len(sys.argv) > 2 else "同频"
    only_pid = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    os.makedirs(out_dir, exist_ok=True)

    wins = list_windows(keyword)
    if not wins:
        print(f"没找到标题含 {keyword!r} 的可见窗口")
        return 1

    count = 0
    for hwnd, title in wins:
        # 跳过不可见的通话引擎窗口
        if "通话引擎" in title:
            continue
        # 按 PID 过滤：同名的多实例需要区分
        if only_pid:
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != only_pid:
                continue
        if "房主" in title:
            tag = "A"
        elif "连接" in title:
            tag = "B"
        else:
            tag = f"w{hwnd}"
        path = os.path.join(out_dir, f"call-{tag}.png")
        if capture(hwnd, path):
            print(f"saved {path}   <- {title}")
            count += 1
    return 0 if count else 1


if __name__ == "__main__":
    raise SystemExit(main())
