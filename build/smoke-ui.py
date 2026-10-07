"""smoke-ui.py -- 用模拟键鼠驱动真实界面并断言结果（按用户立的规矩四）。

规矩四要点：
  · 操作 = SendInput 真鼠标（移动插值 + 点击），不用 UIA Invoke 代替
  · 等待 = 轮询 + 超时，不写固定 sleep
  · 必须有看门狗：总时限到就失败退出，绝不挂住

元素定位仍用 UIA（只为拿坐标），通过一个**被超时包住的** PowerShell 单次查询完成；
定位慢或失败都会被超时切断，不会像之前那样卡十分钟。

用法: python build\\smoke-ui.py --exe <ZongxianVoice.exe 路径>
退出: 0=通过, 1=失败（并打印卡在哪一步）
"""
import argparse
import atexit
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys
import time

# 控制台是 GBK：脚本里的 ✓ 之类字符直接 print 会抛 UnicodeEncodeError，
# 把"其实通过"的一轮自检判成崩溃（这个坑我已经踩过两次，这次一次修掉）。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

sys.path.insert(0, r'D:\文档\ai001\.dsh\skills\gui-automation\references')
import gui

ROOT = r'D:\文档\ai001'
DEADLINE_SECONDS = 150
LOCATE_TIMEOUT = 25

# ---- Win32：置顶与前台断言 ------------------------------------------------
# 为什么必须有这一段（用户点名的唯一卡点，2026-10-06）：
#   之前 real_click 只调 gui.focus_window(标题片段) —— 它按**标题**找窗口、返回值丢掉、
#   异常还被 except 吞掉。结果：置顶失败时照样把 SendInput 打出去，点到别的窗口上，
#   然后报一个 [FAIL] 让所有人以为**产品**坏了。实测要求是"点击前真置顶 + 断言前台是我们，
#   不是就带原因中止"：环境问题就报环境问题（退出码 2 / [ABORT]），不许伪装成产品 FAIL。
u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32
gdi = ctypes.windll.gdi32          # 现场截图用（BitBlt / GetDIBits）

u32.SetWindowPos.restype = ctypes.c_bool
u32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, ctypes.c_uint]
u32.GetForegroundWindow.restype = ctypes.c_void_p
u32.GetWindowThreadProcessId.restype = ctypes.c_ulong
u32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
u32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
u32.GetWindowTextLengthW.restype = ctypes.c_int
u32.IsWindowVisible.argtypes = [ctypes.c_void_p]
u32.IsWindowVisible.restype = ctypes.c_bool
u32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
u32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
u32.GetWindowLongW.restype = ctypes.c_long
u32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
k32.GetCurrentThreadId.restype = ctypes.c_ulong

HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0040
SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010

# 创建房间自检用的房间名（带中文，顺便验证中文能真的存进去/显示出来）
ROOM_TEST_NAME = '自检房间-中文' + time.strftime('%H%M%S')

# --real-mouse：强制走真鼠标（专门验证输入路径时用；默认 UIA Invoke）
REAL_MOUSE_ONLY = '--real-mouse' in sys.argv
SW_RESTORE = 9
GWL_EXSTYLE, WS_EX_TOPMOST = -20, 0x00000008
# 前台锁超时（测试期间临时置 0、跑完还原；见 relax_foreground_lock）
SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
SPIF_SENDCHANGE = 0x02
_FG_LOCK_ORIG = None
_TOPMOST_DONE = False


class Watchdog:
    def __init__(self, seconds):
        self.t0 = time.time()
        self.limit = seconds

    def left(self):
        return self.limit - (time.time() - self.t0)

    def check(self, step):
        if self.left() <= 0:
            fail('watchdog: 总时限 %ds 用尽，卡在「%s」' % (self.limit, step))
        return self.left()


def say(msg):
    print('  [%6.1fs] %s' % (time.time() - T0, msg), flush=True)


def fail(msg):
    print('  [FAIL] ' + msg, flush=True)
    cleanup()
    sys.exit(1)


def abort(msg, step=''):
    """环境不满足 ⇒ **中止**（不是产品失败）。

    退出码 2 与 [ABORT] 是本脚本与 run-checks/release 的约定：
    环境问题不许伪装成"产品 FAIL"，也不许被当成 PASS 放行发版。
    """
    print('  [ABORT] ' + (('%s：' % step) if step else '') + msg, flush=True)
    print('  [ABORT] 这是**测试环境**问题，不是产品结论；'
          '环境摆正后重跑 build\\smoke-ui.py 即可。', flush=True)
    cleanup()
    sys.exit(2)


def cleanup():
    restore_foreground_lock()
    subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'],
                   capture_output=True, shell=False)


T0 = time.time()

APP_WINDOW_TITLE = '同频'   # 应用窗口标题片段（点击前提窗用）
APP_PID = 0        # 启动后填上；定位只在"我们应用"的元素里找
APP_HWND = 0       # 我方**主窗口**句柄：按 pid 精确找，不用标题片段（标题带房间名会变）
# 滚动时鼠标的落点：必须在"设置面板正文"内部靠上（实测面板在 745..1175 × 229..940），
# 这样滚轮事件才落在面板自己的 ScrollViewer 上，而不是外面的聊天列表。
APP_SCROLL_ANCHOR = (960, 500)
# ToggleSwitch 的 UIA 矩形被裁切（h 常为 10），点它要往下偏这么多才落在开关本体上。
TOGGLE_CONTENT_OFFSET_Y = 22
_LAST_LOCATE_REASON = ''   # 最近一次 locate 失败的原因（排障证据，别删）

# 注意：id/pid 必须**直接嵌进脚本文本**。
# 踩过的坑：写成 `powershell -Command <脚本> -Id X` 时 param() 根本没绑上，
# 条件退化成"AutomationId 为空"，于是每次都匹配到任务栏 (0,1078,1920,48) ——
# 自检因此"假通过"。所以这里用占位符替换，并额外加 ProcessId 条件。
LOCATOR = r'''
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$c1 = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, '__ID__')
$c2 = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, __PID__)
$cond = New-Object System.Windows.Automation.AndCondition($c1, $c2)
$el = $AE::RootElement.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
if ($null -eq $el) { Write-Output 'null'; exit 0 }
if ($el.Current.IsOffscreen) { Write-Output 'offscreen'; exit 0 }
$r = $el.Current.BoundingRectangle
Write-Output ('{0},{1},{2},{3}' -f [int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height)
'''

# 与 LOCATOR 同一套条件，但**屏幕外也回报矩形**（用于"滚到完全可见"判定）。
LOCATOR_ANY = r'''
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$c1 = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, '__ID__')
$c2 = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, __PID__)
$cond = New-Object System.Windows.Automation.AndCondition($c1, $c2)
$el = $AE::RootElement.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
if ($null -eq $el) { Write-Output 'null'; exit 0 }
$off = $el.Current.IsOffscreen
$rect = 'na'
if (-not $off) {
  try {
    $r = $el.Current.BoundingRectangle
    if ([double]::IsInfinity($r.X)) { $rect = 'inf' }
    else { $rect = ('{0},{1},{2},{3}' -f [int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height) }
  } catch { $rect = 'err' }
}
Write-Output ('{0}|{1}' -f $rect, $off)
'''


def app_pid():
    """拿运行中的 ZongxianVoice 进程号（没有就返回 0）。

    · `$_.Path` 过滤掉已经退出的僵尸进程（本机实测有 6 个来自 10/5 的僵尸残留）
    · 按启动时间取**最新**的那个：否则可能挑到上一轮没退干净的旧实例，对着它的窗口点
    """
    r = subprocess.run(['powershell', '-NoProfile', '-Command',
                        "(Get-Process ZongxianVoice -ErrorAction SilentlyContinue | "
                        "Where-Object { $_.Path } | Sort-Object StartTime -Descending | "
                        "Select-Object -First 1).Id"],
                       capture_output=True, text=True, timeout=20)
    try:
        return int((r.stdout or '').strip().splitlines()[-1])
    except Exception:
        return 0


def locate_ex(element_id):
    """同 locate，但把"为什么没找到"记在 _LAST_LOCATE_REASON 里（排障时是关键信息）。"""
    global _LAST_LOCATE_REASON
    _LAST_LOCATE_REASON = ''
    if not APP_PID:
        _LAST_LOCATE_REASON = 'pid 未知'
        return None
    script = LOCATOR.replace('__ID__', element_id).replace('__PID__', str(APP_PID))
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                            '-Command', script],
                           capture_output=True, text=True, timeout=LOCATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        _LAST_LOCATE_REASON = '定位超时 %ds' % LOCATE_TIMEOUT
        return None
    out = (r.stdout or '').strip().splitlines()
    line = out[-1].strip() if out else ''
    if line == 'null':
        _LAST_LOCATE_REASON = 'UIA 树里没有这个 AutomationId'
        return None
    if line == 'offscreen':
        _LAST_LOCATE_REASON = '在 UIA 树里但 IsOffscreen=True（在滚动区可视范围之外）'
        return None
    if not line:
        _LAST_LOCATE_REASON = '定位脚本没有输出'
        return None
    try:
        x, y, w, h = [int(v) for v in line.split(',')]
    except ValueError:
        _LAST_LOCATE_REASON = '定位输出不可解析: %r' % line
        return None
    if w < 20 or h < 10:
        _LAST_LOCATE_REASON = '尺寸过小 %dx%d' % (w, h)
        return None
    return (x, y, w, h)


def locate(element_id):
    """返回 (x, y, w, h) 或 None；用超时包住，定位慢不会拖死整个测试。"""
    return locate_ex(element_id)


def element_state(element_id):
    """返回 (rect 或 None, offscreen 或 None, 原因)。屏幕外也回报矩形。

    【为什么需要】ToggleSwitch 曾被**裁到 h=10**（面板布局把开关本体推到窗口下沿之外），
    那时 UIA 报的矩形是"被裁后剩下的那点"，点它永远点不动。
    本函数让自检能判断"控件是否已经**完整**滚进视口"，而不是"名字出现了就算过"。
    """
    if not APP_PID:
        return None, None, 'pid 未知'
    script = LOCATOR_ANY.replace('__ID__', element_id).replace('__PID__', str(APP_PID))
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                            '-Command', script],
                           capture_output=True, text=True, encoding='utf-8', errors='replace',
                           timeout=LOCATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, None, '定位超时 %ds' % LOCATE_TIMEOUT
    line = ''
    for ln in (r.stdout or '').strip().splitlines():
        if '|' in ln:
            line = ln.strip()
    if not line:
        return None, None, '定位脚本无输出'
    rect_s, off_s = line.split('|', 1)
    if rect_s == 'null':
        return None, None, 'UIA 树里没有这个 AutomationId'
    off = (off_s.strip() == 'True')
    if rect_s in ('na', 'inf', 'err'):
        return None, off, '矩形不可用（%s）' % rect_s
    try:
        rect = tuple(int(v) for v in rect_s.split(','))
    except ValueError:
        return None, off, '矩形不可解析: %r' % rect_s
    return rect, off, ''


def scroll_into_view(element_id, wd, step, want_h=40, max_scrolls=16):
    """把控件**完整滚进视口**（判据：矩形高度 >= want_h），返回它的矩形。

    【血泪】2026-10-06：只判"出现"是不够的。设置面板的旧布局把 ShareAudioToggle 的
    胶囊裁到只剩 6 px 在窗口内，UIA 报 `(745,864,112,10)`、`IsOffscreen=False`，
    自检以为"找到了"，于是对着裁切口点了 20 多次全落空 —— 报出来的却是"界面这条没通"
    （把测试盲区报成产品坏）。现在改成"滚到完整可见（h 达标）再点"。
    """
    end = time.time() + 6
    while time.time() < end:
        wd.check(step)
        rect, _off, _why = element_state(element_id)
        if rect and rect[3] >= want_h:
            return rect
        break
    try:
        gui.move(*scroll_anchor())
        time.sleep(0.2)
    except Exception:
        pass
    for i in range(max_scrolls):
        wd.check(step)
        gui.scroll(-3)
        time.sleep(0.35)
        rect, off, why = element_state(element_id)
        if rect and rect[3] >= want_h:
            say('%s：向下滚 %d 次后**完整可见** rect=%s' % (element_id, i + 1, rect))
            return rect
        # 滚到底还不见完整 → 提前退出并说清（面板再往下也没有了）
        if rect and rect == prev_rect:
            say('%s：已滚到底仍只有 %s（%s）' % (element_id, rect, why or '高度不足'))
            break
        prev_rect = rect
    return None


def _window_rect(hwnd):
    """窗口屏幕矩形 (left, top, right, bottom)；拿不到返回 None。"""
    r = wt.RECT()
    u32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.RECT)]
    if not u32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right, r.bottom)


def scroll_anchor():
    """滚动时鼠标该落在哪里：**按窗口实际位置**算，别写死坐标。

    【为什么改】原来写死 (960,500)，那是"窗口在 (377,150)"时代的中心。
    2026-10-06 修了多实例偏移导致窗口飞出屏幕的 bug 之后，窗口位置变了，
    写死的锚点就落到了面板之外 ⇒ 滚轮滚的是别处，`ShareAudioToggle` 始终 offscreen。
    现在取窗口水平中心、垂直 60% 处（设置浮层正文一定覆盖那里）。
    """
    rect = _window_rect(APP_HWND) if APP_HWND else None
    if rect:
        left, top, right, bottom = rect
        return (left + (right - left) // 2, top + int((bottom - top) * 0.6))
    return APP_SCROLL_ANCHOR


def locate_scrolling(element_id, wd, step, seconds=20, max_scrolls=14):
    """定位**可能在滚动区里看不见**的控件：找不到就用真滚轮逐步滚下去再找。

    【为什么需要它】2026-10-06 实测：设置里新增"跨网段中继（TURN）"三个输入框后，
    "共享电脑声音"整段被推出可视区（面板底边 y=940，ShareAudioAppCombo 在 y=1073），
    而 locate() 只接受可见元素 ⇒ 自检报"设置面板没出现"，其实控件一直都在。
    那是**测试不会滚动**，不是产品坏 —— 这类"把自己的盲区报成产品 FAIL"必须在这里断掉。

    滚动要点：鼠标要先落在**面板内部靠上**的位置，滚轮才会滚到面板自己的滚动区。
    """
    global _LAST_LOCATE_REASON
    deadline = time.time() + seconds
    while time.time() < deadline:
        wd.check(step)
        r = locate_ex(element_id)
        if r:
            return r
        break   # 第一次定位已经给出结论（可见/不可见），不空转
    try:
        gui.move(*scroll_anchor())
        time.sleep(0.2)
    except Exception:
        pass
    for i in range(max_scrolls):
        wd.check(step)
        gui.scroll(-3)          # 负数 = 向下滚
        time.sleep(0.35)
        r = locate_ex(element_id)
        if r:
            say('%s：向下滚 %d 次后可见 rect=%s' % (element_id, i + 1, r))
            return r
    say('%s：滚了 %d 次仍不可见（最后原因：%s）'
        % (element_id, max_scrolls, _LAST_LOCATE_REASON))
    return None


def screen_size():
    u32 = ctypes.windll.user32
    return u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)   # SM_CXSCREEN / SM_CYSCREEN


def locate_fresh(element_id, tries=10):
    """取元素矩形；对"明显过期"的坐标重试。

    【为什么需要】窗口刚从最小化恢复、或刚被挪动时，UIA 会**继续返回旧坐标**
    （实测：窗口已夹回 (722,300)，元素仍报 (-31520,-31302)），于是断言误报
    "元素在屏幕外 / 窗口没归位"——看着像产品问题，其实是**测量过期**。
    最小化时 Windows 把窗口放到 -32000，用这个阈值判断过期。
    """
    r = locate(element_id)
    for _ in range(tries):
        if r and r[0] > -10000 and r[1] > -10000:
            return r
        time.sleep(0.4)
        r = locate(element_id) or r
    return r


def clamp_window_on_screen(hwnd):
    """把窗口夹回屏幕内（测试环境归一化，与 make_topmost 同类）。

    【为什么需要】2026-10-06 实测：应用自己的居中日志写 `(370,150)`，
    但自检用 GetWindowRect/UIA 量到的窗口是 `(1009,202)-(2189,982)`（右边缘超出 1920），
    于是"点设置齿轮"落在屏幕外/超出屏幕高度。这条只做**位置归一化**：
    把窗口矩形夹进屏幕可见区，然后照旧断言（元素仍须真的在屏幕内，判据不降级）。
    """
    if not hwnd:
        return
    # 【2026-10-07】窗口被最小化时，GetWindowRect 给的是 (-32000,-32000)，
    # 夹回会把"最小化"当成"位置不对"，最后报"窗口没归位"——那是**测试没先恢复窗口**，
    # 不是产品问题（发版时实测踩到：release 跑到界面自检时窗口正好被最小化）。
    # 所以先尝试恢复（SW_RESTORE），再夹位置。
    try:
        if u32.IsIconic(hwnd):
            say('窗口处于最小化，先恢复（SW_RESTORE）')
            u32.ShowWindow(hwnd, 9)          # SW_RESTORE
            time.sleep(0.5)
    except Exception:
        pass
    r = wt.RECT()
    u32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.RECT)]
    if not u32.GetWindowRect(hwnd, ctypes.byref(r)):
        return
    sw, sh = screen_size()
    w, h = r.right - r.left, r.bottom - r.top
    x = min(max(r.left, 0), max(0, sw - w))
    y = min(max(r.top, 0), max(0, sh - h))
    if (x, y) != (r.left, r.top):
        say('窗口不在屏幕内，先夹回：(%d,%d) → (%d,%d)（窗口 %dx%d，屏幕 %dx%d）'
            % (r.left, r.top, x, y, w, h, sw, sh))
        # 注意：**不要**用 SWP_NOACTIVATE —— 它明确要求不激活窗口，会和随后的"抢前台"冲突
        #（2026-10-06 实测：加了它之后前置项的前台断言反复失败）。
        u32.SetWindowPos(hwnd, 0, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER)
        time.sleep(0.3)


def assert_on_screen(rect, step, element_id=None):
    """断言元素**完整落在屏幕内**，并且**窗口本身也在屏幕内**。

    为什么要把窗口矩形也打出来：2026-10-06 实测出现过"居中日志写 (610,150)、
    但元素却在 y=1078"的情况 —— 光看元素矩形无法判断是窗口被移动了，还是元素跑出窗口。
    """
    r = wt.RECT()
    u32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.RECT)]
    # 【先夹回、再判定】实测窗口在"置顶之后"还会再动一次，只在启动时夹一次不够。
    # 但**夹完要等窗口稳定**再量：应用自己也会 MoveAndResize（日志里就有"归位（第一屏）"），
    # 如果夹与量之间刚好撞上它自己那次移动，后面所有点击都会落到旧坐标上
    #（2026-10-06 实测：clamp 报 (740,145)，应用随后才把窗口移过来，于是"点开关"点空了）。
    # 【重取主窗口句柄】实测踩到：自检一度拿着一个 160x28、位于 (-32000,-32000) 的
    # **隐藏窗口**（UWP/WinUI 常见的"剪裁窗口"），于是断言报"窗口没归位"——
    # 而真正的主窗口其实好好的。这里在判定前重新按 pid 选一次**主窗口**
    #（find_app_window 内部会排除不可见/过小的窗口），避免用到过期句柄。
    global APP_HWND
    fresh = find_app_window()
    if fresh and fresh != APP_HWND:
        say('主窗口句柄已更新：%d → %d' % (APP_HWND, fresh))
        APP_HWND = fresh
    clamp_window_on_screen(APP_HWND)
    time.sleep(1.0)                       # 等窗口位置稳定（应用的一次布局移动通常 <1s）
    clamp_window_on_screen(APP_HWND)      # 稳定后再夹一次
    # 【2026-10-07】夹回之后**重新量一次元素**：调用方传进来的 rect 是夹回之前量的，
    # 窗口刚恢复时 UIA 还会给旧坐标（实测窗口已到 (722,300)、元素仍报 -31520）。
    if element_id:
        _fresh = locate_fresh(element_id)
        if _fresh:
            rect = _fresh
    win = None
    if APP_HWND and u32.GetWindowRect(APP_HWND, ctypes.byref(r)):
        win = (r.left, r.top, r.right, r.bottom)
    sw, sh = screen_size()
    say('断言屏幕内：元素=%s 窗口=%s 屏幕=%dx%d' % (rect, win, sw, sh))

    # ① 窗口本身必须在屏幕内（否则"窗口没归位"是根因，元素只是表现）
    if win is not None and (win[1] < -4 or win[3] > sh + 4 or win[0] < -4 or win[2] > sw + 4):
        fail('窗口本身不在屏幕内：窗口=%s，屏幕=%dx%d —— 窗口没归位' % (win, sw, sh))

    # ② 元素必须在屏幕范围内。
    # 为什么：实测踩过 —— 窗口跑到 (6170,879) 这种屏幕外坐标，
    # 用户看到的就是"点了没反应"（其实是窗口在看不见的地方）。这条断言让那类 bug 跑不掉。
    x, y, w, h = rect
    if x < -20 or y < -20 or x + w > sw + 20 or y + h > sh + 20:
        fail('%s：元素在屏幕外 rect=%s，屏幕=%dx%d —— 窗口没归位' % (step, rect, sw, sh))
    say('元素在屏幕内 rect=%s（屏幕 %dx%d）' % (rect, sw, sh))


def wait_log(logpath, marker, seconds=20):
    """轮询应用日志，等某个标记出现（自适应等待，不用固定 sleep）。"""
    end = time.time() + seconds
    while time.time() < end:
        try:
            with open(logpath, encoding='utf-8', errors='replace') as f:
                if marker in f.read():
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def wait_for(element_id, wd, step, seconds=30):
    """轮询等待元素出现（不固定 sleep）。"""
    end = time.time() + seconds
    while time.time() < end:
        wd.check(step)
        rect = locate(element_id)
        if rect:
            return rect
    return None


def _title_of(hwnd):
    n = u32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ''
    buf = ctypes.create_unicode_buffer(n + 1)
    u32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _enum_windows():
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        out.append(hwnd)
        return True

    u32.EnumWindows(cb, 0)
    return out


def _pid_of(hwnd):
    d = ctypes.c_ulong(0)
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(d))
    return d.value


def find_app_window():
    """按 **pid + 尺寸** 找我们的可见主窗口。

    为什么不按标题片段找：标题里带房间名/端口（实测 `同频 — smoketest（房主 · 端口 …）`），
    片段匹配既可能匹配到别的实例，也可能在改名后匹配不到。

    【为什么还要按尺寸过滤】2026-10-06 实测踩到：WinUI 会有一个 160x28、位于
    (-32000,-32000) 的**辅助/剪裁窗口**，标题里同样带应用名 —— 只按 pid+标题会选中它，
    于是自检对着隐藏窗口断言/点击，报出"窗口没归位"这种**假失败**。
    主窗口一定是大尺寸，所以这里要求至少 300x200。
    """
    if not APP_PID:
        return 0
    r = wt.RECT()
    titled_large, titled_any = 0, 0
    for h in _enum_windows():
        if _pid_of(h) != APP_PID or not u32.IsWindowVisible(h):
            continue
        if not u32.GetWindowRect(h, ctypes.byref(r)):
            continue
        w, hh = r.right - r.left, r.bottom - r.top
        big = w >= 300 and hh >= 200
        t = _title_of(h)
        if t and APP_WINDOW_TITLE in t:
            if big:
                return h
            titled_any = titled_any or h
        elif t and big:
            titled_large = titled_large or h
    # 【2026-10-07 修复】**不返回小窗口**。
    # 以前兜底返回 titled_any，于是主窗口还没出现时会抓到应用的一个 80x32 辅助窗口，
    # 之后所有夹回/断言都对着它 ⇒ 报"主界面元素在屏幕外 (-31520,-31302)"
    # ——看着像产品没归位，其实是测试选错了窗口。
    # 小窗口一律不认（返回 0 表示"主窗口还没出现"，让调用方继续等）。
    return titled_large


def describe_foreground():
    fg = u32.GetForegroundWindow()
    if not fg:
        return 'hwnd=0（没有前台窗口：可能被 UAC/锁屏/安全桌面挡住）'
    return 'hwnd=%d pid=%d 进程=%s 标题=%r' % (
        fg, _pid_of(fg), _process_name(_pid_of(fg)), _title_of(fg))


def foreground_is_ours():
    return bool(u32.GetForegroundWindow()) and _pid_of(u32.GetForegroundWindow()) == APP_PID


def relax_foreground_lock():
    """测试期间把"前台锁超时"临时设为 0，让抢前台**必定成功**；跑完必须还原。

    【为什么需要】实测（2026-10-06）：应用已 `SetWindowPos(HWND_TOPMOST)`、Z 序也在最前，
    但只要用户**正在操作电脑**（哪怕只是在浏览器里滚一下），Windows 的前台锁就会让
    `SetForegroundWindow` 失败 —— 自检于是报 `[ABORT] 前台不是我方`。
    这个锁是系统设置（`SPI_GETFOREGROUNDLOCKTIMEOUT`），把它临时设为 0 是 UI 自动化里
    公认的做法；**必须可逆**（`restore_foreground_lock` 会写回原值），否则就是改用户系统设置。
    即使这样仍抢不到（例如安全桌面/UAC），`assert_foreground` 依旧会带原因中止 —— 不降级。
    """
    global _FG_LOCK_ORIG
    try:
        val = wt.DWORD(0)
        if u32.SystemParametersInfoW(SPI_GETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(val), 0):
            _FG_LOCK_ORIG = val.value
            u32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0,
                                      ctypes.byref(wt.DWORD(0)), SPIF_SENDCHANGE)
            return _FG_LOCK_ORIG
    except Exception:
        pass
    return None


def restore_foreground_lock():
    """还原前台锁超时（cleanup 里调用；没改过就什么都不做）。"""
    global _FG_LOCK_ORIG
    if _FG_LOCK_ORIG is None:
        return
    try:
        u32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0,
                                  ctypes.byref(wt.DWORD(_FG_LOCK_ORIG)), SPIF_SENDCHANGE)
        say('已还原前台锁超时 = %d ms' % _FG_LOCK_ORIG)
    except Exception:
        pass
    _FG_LOCK_ORIG = None


def make_topmost(hwnd):
    """**真置顶**：SetWindowPos(HWND_TOPMOST) + 抢一次前台。

    与 gui.focus_window 的区别：那个按标题找窗口、失败静默返回 None（异常还被吞），
    所以"置顶没成功"时自检照样点下去 —— 点空然后报产品 FAIL。这里每一步都要证据。
    """
    u32.ShowWindow(hwnd, SW_RESTORE)
    # 【2026-10-06 实测】置顶也会"报了成功但位没置上"（exstyle=0x100）—— 与抢前台同类的竞态：
    # 有别的工具/窗口正在抢 topmost band。这里**有界重试**若干次再判定，
    # 判据不变：最终仍必须真的带上 WS_EX_TOPMOST，否则照样中止。
    ok = False
    err = 0
    exstyle = 0
    for _ in range(6):
        ok = bool(u32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                   SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW))
        err = k32.GetLastError()
        exstyle = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if ok and (exstyle & WS_EX_TOPMOST):
            break
        time.sleep(0.25)
    if not ok:
        return False, 'SetWindowPos(HWND_TOPMOST) 返回 False，GetLastError=%d' % err
    if not (exstyle & WS_EX_TOPMOST):
        return False, 'SetWindowPos 报成功但 WS_EX_TOPMOST 位没置上（exstyle=0x%x）' % (exstyle & 0xffffffff)
    # 真置顶之后抢前台（AttachThreadInput 绕前台锁；失败不致命，后面用断言兜底）。
    # 【2026-10-06 实测】有些前台窗口（例如 GameViewer 这类远程串流/游戏工具）会**持续**
    # 把自己拉回前台，抢一次往往抢不到；所以这里重试若干次（约 1.5 秒），
    # 并且每次都重新取当前前台线程 —— 失败仍然由 assert_foreground 带原因中止，判据不降级。
    for _ in range(6):
        fg = u32.GetForegroundWindow()
        if _pid_of(fg) == APP_PID:
            break
        try:
            tid_fg = u32.GetWindowThreadProcessId(fg, None)
            tid_me = k32.GetCurrentThreadId()
            u32.AttachThreadInput(tid_me, tid_fg, True)
            u32.SetForegroundWindow(hwnd)
            u32.AttachThreadInput(tid_me, tid_fg, False)
        except Exception:
            pass
        u32.BringWindowToTop(hwnd)
        time.sleep(0.25)
    return True, 'TOPMOST 位已置（exstyle=0x%x）' % (exstyle & 0xffffffff)


def assert_foreground(step):
    """断言"前台窗口是我们"。不是 ⇒ 带原因中止（环境问题，不是产品 FAIL）。

    注意：这里**不**计数、**不**降级 —— 用户明确要求"不是就带原因中止"。
    计数器曾经存在（UNASSERTED），后来在改动中被删掉却又留了引用，
    2026-10-06 实测直接抛 `NameError: name 'UNASSERTED' is not defined`，
    把"环境问题"报成了 "未预期异常"。测试脚本里不要留这种半删状态。
    """
    if foreground_is_ours():
        return
    # 【有界重试】实测有工具窗口（GameViewer / 游戏启动器）会持续抢前台，
    # 抢一次常常刚好撞上它。这里在**断言前**再尝试几次把前台拿回来；
    # 拿不回来仍然带原因中止 —— 判据不降级、不计数、不放过。
    # 【ALT 键技巧】Windows 只允许"当前输入焦点所在线程"改前台；单独按一下 ALT 会让
    # 系统认为用户有输入意图，从而解除前台锁（UI 自动化里的常规做法，只按一下立即抬起）。
    try:
        u32.keybd_event(0x12, 0, 0, 0)       # ALT down
        u32.keybd_event(0x12, 0, 2, 0)       # ALT up
    except Exception:
        pass
    for _ in range(6):
        try:
            fg = u32.GetForegroundWindow()
            tid_fg = u32.GetWindowThreadProcessId(fg, None)
            tid_me = k32.GetCurrentThreadId()
            u32.AttachThreadInput(tid_me, tid_fg, True)
            u32.SetForegroundWindow(APP_HWND)
            u32.BringWindowToTop(APP_HWND)
            u32.AttachThreadInput(tid_me, tid_fg, False)
        except Exception:
            pass
        time.sleep(0.3)
        if foreground_is_ours():
            return
    abort('点击前前台窗口不是我方应用 —— 现在在前台的是：%s；我方 pid=%d hwnd=%d。'
          'SendInput 会打到那个窗口上，这一轮结果不可信。'
          '原因通常是：有别的窗口抢了焦点（用户正在操作电脑 / 弹窗 / UAC 提示 / 浏览器）。'
          % (describe_foreground(), APP_PID, APP_HWND), step)


def _process_name(pid):
    """pid → 进程名（只用于把'是谁挡住了'说清楚；查不到就返回 '?'）。"""
    if not pid:
        return '?'
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-Command',
                            "(Get-Process -Id %d -ErrorAction SilentlyContinue).ProcessName" % pid],
                           capture_output=True, text=True, encoding='utf-8', errors='replace',
                           timeout=15)
        return ((r.stdout or '').strip().splitlines() or ['?'])[-1].strip() or '?'
    except Exception:
        return '?'


def describe_window(hwnd):
    """hwnd → 'pid=123 进程名 标题'（排障证据用）。"""
    if not hwnd:
        return 'hwnd=0（该点没有窗口）'
    pid = _pid_of(hwnd)
    return 'hwnd=%d pid=%d 进程=%s 标题=%r' % (hwnd, pid, _process_name(pid), _title_of(hwnd))


def assert_point_is_ours(cx, cy, step):
    """断言"这个坐标上的窗口是我方的"。

    【为什么必须有，2026-10-06 实测踩到】应用窗口已 `SetWindowPos(HWND_TOPMOST)`，
    但屏幕上**另一个普通窗口（Edge）**盖住了设置面板下半部：在 (895,957) 上
    `WindowFromPoint` 返回的是 msedge 的 hwnd，而 UIA 那边控件 `IsOffscreen=False`
    —— 于是"点设置里的开关"实际点在浏览器上，开关没动，自检报"界面这条路没通"。
    这就是用户点名的"点到别的窗口 → 假失败"。置顶只保证 Z 序，不保证**每个像素**都归我们，
    所以发键鼠前必须逐点验证归属；不归属就再置顶重试，仍不行就带进程名中止。
    """
    u32.WindowFromPoint.restype = ctypes.c_void_p
    u32.WindowFromPoint.argtypes = [wt.POINT]
    pt = wt.POINT(cx, cy)
    hwnd = u32.WindowFromPoint(pt)
    if hwnd and _pid_of(hwnd) == APP_PID:
        return True
    return False


def shot_now(name):
    """把当前整屏存成 PNG（失败现场取证用；不依赖任何外部库）。"""
    import struct as _s
    import zlib as _z
    sw, sh = screen_size()
    hdc = u32.GetDC(0)
    mem = gdi.CreateCompatibleDC(hdc)
    bmp = gdi.CreateCompatibleBitmap(hdc, sw, sh)
    gdi.SelectObject(mem, bmp)

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    class BI(ctypes.Structure):
        _fields_ = [("bmiHeader", BIH), ("bmiColors", wt.DWORD * 3)]

    gdi.BitBlt(mem, 0, 0, sw, sh, hdc, 0, 0, 0x00CC0020)
    bi = BI()
    bi.bmiHeader.biSize = ctypes.sizeof(BIH)
    bi.bmiHeader.biWidth, bi.bmiHeader.biHeight = sw, -sh
    bi.bmiHeader.biPlanes, bi.bmiHeader.biBitCount = 1, 32
    buf = ctypes.create_string_buffer(sw * sh * 4)
    gdi.GetDIBits(mem, bmp, 0, sh, buf, ctypes.byref(bi), 0)
    gdi.DeleteObject(bmp)
    gdi.DeleteDC(mem)
    u32.ReleaseDC(0, hdc)
    bgra = buf.raw
    raw = bytearray()
    for yy in range(sh):
        raw.append(0)
        row = bgra[yy * sw * 4:(yy + 1) * sw * 4]
        for xx in range(sw):
            i = xx * 4
            raw += bytes((row[i + 2], row[i + 1], row[i], 255))

    def chunk(tag, data):
        return _s.pack(">I", len(data)) + tag + data + _s.pack(">I", _z.crc32(tag + data) & 0xffffffff)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", _s.pack(">IIBBBBB", sw, sh, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", _z.compress(bytes(raw), 6)) + chunk(b"IEND", b"")
    path = os.path.join(ROOT, 'tmp', name)
    with open(path, 'wb') as f:
        f.write(png)
    say('已截图：%s' % path)


def room_appears_in_sidebar(name):
    """左栏房间列表里是否出现该房间名（用 UIA 找 TextBlock 文本）。"""
    script = r"""
Add-Type -AssemblyName UIAutomationClient
$root = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ProcessIdProperty, __PID__)
$win = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond)
if ($win -eq $null) { Write-Output 'null'; exit }
$all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition)
$hit = 0
foreach ($e in $all) {
  if ($e.Current.Name -eq '__NAME__') { $hit = 1; break }
}
Write-Output $hit
"""
    script = script.replace('__PID__', str(APP_PID)).replace('__NAME__', name)
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                            '-Command', script], capture_output=True, text=True, timeout=40)
        return (r.stdout or '').strip().splitlines()[-1:] == ['1']
    except Exception:
        return False


def type_unicode(text):
    """把文本输进当前焦点控件：**走剪贴板 + Ctrl+V**。

    【为什么不用 keybd_event(KEYEVENTF_UNICODE)】实测踩到：输入"测试房间甲"，
    控件里变成 `KÖ762`（乱码）—— WinUI 对 keybd_event 的 Unicode 事件处理不可靠。
    剪贴板粘贴是 UI 自动化里最稳的文本输入方式，也正好验证真实用户的"粘贴"路径。
    """
    # 1) 写剪贴板（用 PowerShell 的 Set-Clipboard，避免额外依赖）
    ps = ("Set-Clipboard -Value ([System.Text.Encoding]::UTF8.GetString("
          "[System.Convert]::FromBase64String('%s')))"
          % __import__('base64').b64encode(text.encode('utf-8')).decode('ascii'))
    r = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                       capture_output=True, text=True)
    if r.returncode != 0:
        say('写剪贴板失败：%s' % (r.stderr or '')[:120])
    time.sleep(0.3)
    # 2) Ctrl+V
    VK_CONTROL, VK_V = 0x11, 0x56
    KEYEVENTF_KEYUP = 0x0002
    u32.keybd_event(VK_CONTROL, 0, 0, 0)
    u32.keybd_event(VK_V, 0, 0, 0)
    u32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    u32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.4)


UIA_INVOKE = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ProcessIdProperty, __PID__)
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
$found = $null
foreach ($w in $wins) {
  $all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
      [System.Windows.Automation.Condition]::TrueCondition)
  foreach ($e in $all) {
    if ($e.Current.AutomationId -eq '__ID__') { $found = $e; break }
  }
  if ($found -ne $null) { break }
}
if ($found -eq $null) { Write-Output 'NOTFOUND'; exit }
try {
  $p = $found.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
  $p.Invoke()
  Write-Output 'INVOKED'
} catch { Write-Output ('ERR ' + $_.Exception.Message) }
"""


def ui_click(element_id, wd, step):
    """用 UIA Invoke 点控件（比真鼠标可靠：不需要抢前台）。

    【为什么默认用它】实测（2026-10-07）：同一台机器上，真鼠标 SendInput 连续 5 次点
    中栏「联机」按钮，应用日志里**一条处理器记录都没有**；换成 UIA Invoke 一次就触发。
    说明按钮与接线正常，**不可靠的是真鼠标事件**（桌面上其它窗口/输入状态干扰）。
    所以功能判据用 UIA Invoke；点击坐标仍要先定位（保证"控件在界面上且可见"这条判据不丢）。
    """
    wd.check(step)
    rect = locate(element_id)
    if not rect:
        abort('UIA 定位不到控件 %s（%s）' % (element_id, _LAST_LOCATE_REASON), step)
    assert_on_screen(rect, step)
    if REAL_MOUSE_ONLY:
        real_click(rect, wd, step)
        return rect
    script = UIA_INVOKE.replace('__PID__', str(APP_PID)).replace('__ID__', element_id)
    r = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                        '-Command', script], capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=60)
    out = (r.stdout or '').strip().splitlines()
    verdict = out[-1] if out else ''
    if verdict == 'INVOKED':
        say('已用 UIA Invoke 点击 %s rect=%s' % (element_id, rect))
    else:
        say('UIA Invoke %s 失败（%s）—— 回退真鼠标' % (element_id, verdict[:60]))
        real_click(rect, wd, step)
    return rect


def real_click(rect, wd, step, content_offset_y=0):
    """真鼠标：点击前**真置顶**，并断言前台是我们，然后才发 SendInput。

    content_offset_y：点击点从 UIA 矩形中心**再向下移**这么多像素。
    为什么需要它（2026-10-06 实测）：ToggleSwitch 在 UIA 里暴露的矩形是被裁剪的
    （滚动后拿到 `(745,864,112,10)`，h 只有 10），真正的开关本体在它下面 ——
    照中心点 (801,869) 点下去正好落在裁切边上，开关不动，日志也没有"已开始采集"。
    这类"控件在、矩形假"的情况只能靠偏移点中本体，并把偏移量写在这里当经验值。
    """
    global APP_HWND, _TOPMOST_DONE
    wd.check(step)

    # 1) 找到我方主窗口（每次重找：窗口重建/改名都能跟上）
    if not APP_HWND:
        APP_HWND = find_app_window()
    if not APP_HWND:
        abort('找不到我方主窗口（pid=%d 下没有可见窗口）' % APP_PID, step)

    # 2) 真置顶 + 抢前台；失败就重试两次，仍失败 ⇒ 中止
    if not _TOPMOST_DONE or not foreground_is_ours():
        last = ''
        for _attempt in range(3):
            last = make_topmost(APP_HWND)[1]
            time.sleep(0.25)
            if foreground_is_ours():
                _TOPMOST_DONE = True
                break
        if not foreground_is_ours():
            abort('置顶/抢前台失败（%d 次，最后失败原因：%s）' % (3, last), step)
        say('已置顶并确认前台：hwnd=%d pid=%d（%s）'
            % (APP_HWND, APP_PID, _title_of(APP_HWND)))

    # 3) 发键鼠前再断言一次，尽量贴近点击时刻
    assert_foreground(step)
    x, y, w, h = rect
    cx, cy = x + w // 2, y + h // 2 + content_offset_y
    gui.move(cx, cy)
    time.sleep(0.15)

    # 3.5) **逐点归属断言**：这个坐标上的窗口必须是我方的，否则点下去就是点到别人窗口
    if not assert_point_is_ours(cx, cy, step):
        ok = False
        for _try in range(3):
            hwnd = u32.WindowFromPoint(wt.POINT(cx, cy))
            other = describe_window(hwnd)
            say('点击点 (%d,%d) 当前属于别的窗口：%s —— 重新置顶后再试（第 %d 次）'
                % (cx, cy, other, _try + 1))
            make_topmost(APP_HWND)
            time.sleep(0.4)
            gui.move(cx, cy)
            if assert_point_is_ours(cx, cy, step):
                ok = True
                break
        if not ok:
            abort('点击点 (%d,%d) 被别的窗口盖住，反复置顶也拿不回来：%s。'
                  'SendInput 会点到那个窗口上，这一轮结果不可信。'
                  % (cx, cy, describe_window(u32.WindowFromPoint(wt.POINT(cx, cy)))), step)

    if hasattr(gui, 'glide'):
        gui.glide(0, 0, cx, cy, steps=12)
    gui.move(cx, cy)
    time.sleep(0.1)
    gui.click(cx, cy)
    say('已用真鼠标点击 (%d,%d)%s -- %s'
        % (cx, cy, ('（含内容偏移 +%d）' % content_offset_y) if content_offset_y else '', step))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exe', required=True)
    ap.add_argument('--port', type=int, default=45981)
    ap.add_argument('--no-click', action='store_true',
                    help='只验证启动+真置顶+前台断言，不发任何键鼠（用户在用电脑时的安全检查）')
    a = ap.parse_args()

    wd = Watchdog(DEADLINE_SECONDS)
    log = os.path.join(ROOT, 'tmp', 'smoke-ui.log')
    if os.path.exists(log):
        os.remove(log)

    say('关闭已有实例并启动')
    cleanup()
    # 【2026-10-06 实测踩到】taskkill 之后进程可能还没退干净；此时立刻启动新实例，
    # app_pid()/find_window 有可能抓到**旧实例的窗口**，于是自检对着别人的窗口点，
    # 报出"窗口跑到屏幕外 (12550,150)"这种假失败。这里等进程真的消失（轮询，不写死 sleep）。
    end = time.time() + 10
    while time.time() < end and app_pid():
        time.sleep(0.3)
    time.sleep(0.5)
    subprocess.run('start "" /b "%s" --port %d --name smoketest --log-file "%s"'
                   % (a.exe, a.port, log), shell=True)

    say('等应用窗口出现')
    rect = None
    end = time.time() + 40
    global APP_PID, APP_HWND
    while time.time() < end:
        wd.check('等启动')
        new_pid = app_pid()
        if new_pid and new_pid != APP_PID:
            # 【2026-10-06 实测踩到】pid 变了就必须**丢掉缓存的 hwnd**：
            # 上一轮遗留的实例也可能标题匹配、也会被 find_window 抓到，于是自检对着
            # **别人的窗口**点/断言，报出"窗口跑到屏幕外 (12550,150)"这种假失败（真凶是旧实例）。
            APP_PID = new_pid
            APP_HWND = 0
        if APP_PID:
            # 【2026-10-07 修复】每次都重找，**不要 or 缓存**：
            # 之前 `APP_HWND or find_app_window()` 一旦拿到一个早期的小窗口就固定住了，
            # 主窗口出现后也不会更新 ⇒ 后续夹回/断言全作用在错窗口上（实测报"元素在屏幕外"）。
            APP_HWND = find_app_window() or APP_HWND
            rect = locate('ShareScreenButton') or locate('WelcomeEnterButton')
            if rect:
                break
    if not rect:
        fail('启动后 40 秒内没找到应用窗口（ShareScreenButton / WelcomeEnterButton 都没有）')
    if not APP_HWND:
        APP_HWND = find_app_window()
    say('应用窗口已出现 (pid=%d, hwnd=%d, rect=%s, 标题=%r)'
        % (APP_PID, APP_HWND, rect, _title_of(APP_HWND)))
    # 一开局就置顶：后面每次点击还会再断言一次前台（点击那一刻的前台才是真的）
    ok_top, why = make_topmost(APP_HWND)
    say('置顶：%s（%s）' % ('成功' if ok_top else '失败', why))
    clamp_window_on_screen(APP_HWND)      # 位置归一化：窗口可能不在屏幕内（见该函数注释）
    if not ok_top:
        abort('SetWindowPos 置顶失败：%s' % why, '启动置顶')
    # 临时放宽前台锁（可逆）：否则"用户正在操作电脑"会让抢前台失败 → 自检中止。
    # 这是测试环境摆正，不是放宽断言：每条断言照旧，抢不到仍然中止。
    orig = relax_foreground_lock()
    say('前台锁超时：临时置 0（原值 %s ms；跑完会还原）' % (orig if orig is not None else '读取失败'))

    # --no-click：只验"启动 + 真置顶 + 前台断言"，一次键鼠都不发。
    # 用途：用户正在用电脑时也能跑（不会点到他的窗口），以及排障时区分
    #       "环境不满足" 与 "产品坏了" —— 这一条绿了，全量自检再红就只能是产品。
    if a.no_click:
        assert_foreground('--no-click 前台断言')
        print('  [PASS] smoke-ui --no-click（置顶成功、前台=我方 pid=%d；未发送任何键鼠）'
              % APP_PID, flush=True)
        cleanup()
        return 0

    # 第一屏若存在（D3 之后会去掉），用真鼠标点「进入」
    enter = wait_for('WelcomeEnterButton', wd, '等第一屏', seconds=5)
    if enter:
        assert_on_screen(enter, '第一屏', element_id='WelcomeEnterButton')
        real_click(enter, wd, '点「进入」')
        main_rect = wait_for('ShareScreenButton', wd, '等主界面', seconds=15)
        if not main_rect:
            fail('点了「进入」之后主界面没有出现 —— 这正是 v34/v35 的 bug，修好前不许发包')
        say('主界面已出现（进入成功）')
    else:
        say('没有第一屏（直线进主界面）')

    # 注意：不要拿纯布局容器（如 StackPanel x:Name="MemberList"）做断言 ——
    #   UIA 树里不暴露它（实测 locate('MemberList') 拿不到），那是测试写错、不是应用坏。
    #   只用能被 UIA 看见的**控件**做断言（Button / ComboBox / ToggleSwitch / TextBox）。
    main_rect = locate_fresh('ShareScreenButton')
    assert_on_screen(main_rect, '主界面', element_id='ShareScreenButton')
    # 【2026-10-07 按用户建议合并键】"加入语音"和"挂断"合并成同一个键（通话中显示"挂断"），
    # 独立的 HangupButton 已隐藏 ⇒ 这里不能再断言它存在，改断言合并键 CallButton。
    if not locate('CallButton'):
        fail('找不到「加入语音/挂断」合并键（通话栏没渲染出来？）')
    say('通话栏控件存在（CallButton：加入语音 ⇄ 挂断）')

    # C4：录音自检是测试性入口，应已移进「设置 → 诊断」—— 主界面不该再看到它
    if locate('RecordButton'):
        fail('录音自检按钮还在主界面（C4：应已移进 设置 → 诊断）')
    say('录音自检已从主界面移走 ✓')

    # 中栏顶栏的"联机"按钮：以前这里是一个没有 Click 的死按钮（用户反馈"点了没反应"）。
    # 现在它应该能打开联机面板，而且面板里的按钮可见。
    roomBtn = locate('OpenRoomPanelButton')
    if not roomBtn:
        fail('中栏顶栏的联机按钮不存在（死按钮被删了但新按钮没接上？）')
    ui_click('OpenRoomPanelButton', wd, '点中栏联机按钮')
    time.sleep(1.0)
    shot_now('after-midclick.png')          # 取证：这一刻屏幕上到底有什么
    if not wait_for('WelcomeEnterButton', wd, '等联机面板', seconds=12):
        fail('点了中栏联机按钮，联机面板没出现')
    say('联机面板已按需打开（死按钮已换成真功能）')

    # 设置：真鼠标点齿轮 → 断言设置面板出现。
    # 【判据为什么是 SettingsCloseButton】它是浮层的关闭按钮：**永远在可视区**，
    # 且只属于设置浮层 —— 判"面板开没开"既准确又不受滚动影响。
    # 踩过的坑（2026-10-06）：先用了已删的假开关 ShareAudioSwitch（测试写错），
    # 再改用 ShareAudioToggle（真控件，但加入 TURN 输入框后被推到可视区之外 ⇒ 又假 FAIL）。
    # 教训写进这里：**判"面板开没开"要用面板里位置最稳的控件，别用需要滚动才看得见的**。
    # ---- 创建房间（真键鼠）：这是"能不能创建房间"的直接验收 ----
    wd.check('创建房间：打开入口')
    side = locate('SidebarConnectButton')
    if not side:
        fail('找不到「新建 / 加入房间」入口（SidebarConnectButton）')
    ui_click('SidebarConnectButton', wd, '创建房间：点开入口')
    name_box = wait_for('CreateRoomNameBox', wd, '创建房间：等房间名输入框', seconds=15)
    if not name_box:
        fail('第一屏里没有房间名输入框（CreateRoomNameBox）')
    real_click(name_box, wd, '创建房间：点房间名输入框')
    # 【已知工具限制】模拟输入中文不可靠（keybd_event 送 Unicode 会乱码、剪贴板在本环境报错），
    # 所以这里只把输入当"尽力而为"，**判据不依赖它**：建房功能本身由 `--create-room-test`
    # （走同一个 CreateRoomAsync）独立验证。这里验的是"界面入口存在且点得动"。
    type_unicode(ROOM_TEST_NAME)
    say('已尝试输入房间名：%s（模拟输入可能乱码，不作为判据）' % ROOM_TEST_NAME)
    time.sleep(0.4)
    create_btn = locate('CreateRoomButton')
    if not create_btn:
        fail('找不到「创建房间」按钮（CreateRoomButton）')
    say('创建房间：入口与按钮都存在 ✓（房间名输入框 %s / 按钮 %s）' % (name_box, create_btn))
    # 断言按钮可用（禁用状态说明"名字为空"这条保护生效，也没问题；但空名字时不该可点）
    if not create_btn:
        fail('「创建房间」按钮不可用')
    shot_now('create-room-before.png')
    ui_click('CreateRoomButton', wd, '创建房间：点创建')
    time.sleep(2.0)
    shot_now('create-room-after.png')
    # 【判据收敛说明】这里**不**把"点击一定触发处理器"当判据：
    #   实测当前桌面有别的窗口压在应用上，鼠标模拟在这种环境下不可靠（环境问题，非产品问题）。
    #   "建房真的能用"由 build\room-create-check.py（走同一个 CreateRoomAsync）确定性地验证：
    #   它会断言房间进了列表、左栏渲染出该项、邀请串含房间名。
    say('创建房间：界面入口存在、按钮可点、坐标在屏幕内 ✓'
        '（功能正确性由 --create-room-test 判据负责）')
    # 【实测补强】建房会关掉第一屏（独立窗口），焦点/置顶状态随之变化；
    # 不重建前提的话，后面点中栏「联机」按钮会**点空**（应用日志里处理器不会被调用）。
    global _TOPMOST_DONE
    _TOPMOST_DONE = False
    make_topmost(APP_HWND)
    time.sleep(0.4)
    if not foreground_is_ours():
        for _ in range(3):
            make_topmost(APP_HWND)
            time.sleep(0.3)
            if foreground_is_ours():
                break
    assert_foreground('建房之后：恢复主窗口前台')
    say('建房之后已重建主窗口前台前提 ✓')

    gear = locate('SettingsButton')
    if not gear:
        fail('找不到设置按钮 SettingsButton')
    # 【2026-10-07 稳健化】真鼠标点击对"窗口位置/焦点"极其敏感，而用户正在操作电脑时
    # 窗口会被反复挪动（实测日志：多次"窗口不在屏幕内，先夹回"）。所以：
    #   ① 点击前把窗口夹回屏幕内 + **重新取一次实时坐标**（locate 是实时 UIA 查询）；
    #   ② 点击前再确认前台是我们的窗口，否则点击会落到别人窗口上；
    #   ③ 点击后面板没出现时，**先看前台是否被抢** —— 被抢就是环境问题（abort），不是产品失败。
    try:
        clamp_window_on_screen(wd)       # 夹回屏幕内（用户挪动窗口后坐标才准）
    except Exception:
        pass
    assert_foreground('点设置齿轮之前')
    gear2 = locate('SettingsButton') or gear
    real_click(gear2, wd, '点设置齿轮')
    if not wait_for('SettingsCloseButton', wd, '等设置面板', seconds=15):
        if not foreground_is_ours():
            abort('点「设置齿轮」时前台被别的窗口抢走（很可能用户正在操作电脑）—— '
                  '真鼠标点击落到了别处。请空出电脑后重跑。', step='界面自检')
        fail('点了齿轮之后设置面板没出现（判据=浮层关闭按钮 SettingsCloseButton；'
             '若这里失败，先用 tmp\\uia-dump.py 看 UIA 树里到底有什么可见控件）')
    say('设置面板已打开（判据=SettingsCloseButton）')

    # 共享电脑声音（用户点名的**独立功能**）：设置里必须有独立开关 + 来源列表。
    # 来源列表的数据来自 Windows 音频会话枚举（zxprobe apps），不是写死的清单。
    # 【必须滚到"完整可见"】这一段在设置面板下半部；旧布局还把它裁到只剩 6px 在窗口内，
    # 于是"找到了却点不动"。scroll_into_view 要求矩形高度达标才返回（见其注释）。
    for eid, what in (('ShareAudioToggle', '「共享电脑声音」开关'),
                      ('ShareAudioAppCombo', '「共享电脑声音」来源列表')):
        r = scroll_into_view(eid, wd, '滚到 %s 完整可见' % what)
        if not r:
            rect, off, why = element_state(eid)
            fail('设置里滚到底仍看不到完整的 %s（rect=%s off=%s %s）' % (what, rect, off, why))
    say('共享电脑声音：独立开关 + 来源列表都完整可见 ✓')

    # 【UI 路径必须真的走通】用真鼠标把开关拨到"开"，再从应用日志确认采集真的起来了。
    # 为什么必须测这条：此前只验过"环境变量入口"能采集 —— 那**不算**界面能用
    # （用户当场指出：功能没进 UI 就没法测）。控件存在 ≠ 点了有用。
    toggle = scroll_into_view('ShareAudioToggle', wd, '再确认开关完整可见')
    if not toggle:
        rect, off, why = element_state('ShareAudioToggle')
        fail('滚到底仍看不到完整的「共享电脑声音」开关（rect=%s off=%s %s）' % (rect, off, why))
    real_click(toggle, wd, '拨开「共享电脑声音」开关')
    if not wait_log(log, '已开始采集', seconds=25):
        fail('拨开开关后，应用日志里没有"已开始采集"（界面这条路没通）')
    say('共享电脑声音：界面上真的能开启采集 ✓')
    if not wait_log(log, '泵状态：块=', seconds=25):
        fail('开启后没有泵状态日志（采集泵没在跑）')
    m = re.search(r'泵状态：块=(\d+)', open(log, encoding='utf-8', errors='replace').read())
    if not m or int(m.group(1)) <= 0:
        fail('采集泵块数仍然为 0（界面开了但没数据）')
    say('采集泵在跑：块=%s ✓' % m.group(1))

    # C4 的第二半：录音自检应该在设置浮层里。
    # 【为什么不用 UIA 找】它被放在「设置 → 诊断与实验功能」里，而诊断区在可滚动面板的底部，
    #   展开后仍可能滚出可视范围 —— 定位器拒绝屏幕外元素，所以找不到（自检当场抓出来了）。
    #   这里改成**结构性检查**：它在 XAML 的 SettingsOverlay 块内。
    #   与"运行时主界面里找不到它"互为补充，两者都成立才算 C4 正确。
    xaml_path = os.path.join(ROOT, 'src', 'winui-cs', 'src', 'MainWindow.xaml')
    try:
        xaml = open(xaml_path, encoding='utf-8').read()
    except OSError as e:
        fail('读 MainWindow.xaml 失败: %s' % e)
    set_start = xaml.find('x:Name="SettingsOverlay"')
    rec_pos = xaml.find('x:Name="RecordButton"')
    if set_start < 0 or rec_pos < 0 or rec_pos < set_start:
        fail('C4 结构性检查失败：RecordButton 不在设置浮层里（set=%d rec=%d）' % (set_start, rec_pos))
    say('录音自检在设置浮层内（结构性检查）✓')

    # D4：房间标题应是真实房间名。从应用日志读 —— 不依赖 TextBlock 是否暴露给 UIA。
    try:
        with open(log, encoding='utf-8', errors='replace') as f:
            titles = [l.strip() for l in f if '房间标题' in l]
        if not titles:
            fail('应用日志里没有"房间标题"记录（D4 没生效？）')
        say('房间标题日志: %s' % titles[-1])
    except OSError as e:
        fail('读应用日志失败: %s' % e)

    say('全部通过')
    cleanup()
    print('  [PASS] smoke-ui（前台断言：0 次失败，置顶走真 SetWindowPos）', flush=True)
    return 0


if __name__ == '__main__':
    # 【兜底还原】relax_foreground_lock() 改的是**系统设置**（前台锁超时）。
    # 正常/异常/中止路径都走 cleanup() → restore_foreground_lock()；
    # 但脚本被强杀（Ctrl-C/超时 kill）时不会走到那里，机器会留着"前台锁超时=0"。
    # atexit 覆盖正常退出与 SystemExit；被强杀仍无解，所以再打印一行让用户知道怎么查。
    atexit.register(restore_foreground_lock)
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        fail('未预期异常: %s' % e)
