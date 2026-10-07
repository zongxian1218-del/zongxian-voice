"""P4 端到端：真共享"整个屏幕" + 打开「使用系统音频共享」，证明对端收到屏幕音频。

关键教训（上一轮）：**不要用截图坐标点弹窗** —— 切标签页后弹窗会重新布局，
旧坐标就失效了（上一轮就是这么 20 秒超时的）。这里一律按**控件文字**（UIA Name）定位。

用法: python build\\share-audio-test.py --exe <ZongxianVoice.exe 路径>
退出: 0=通过, 1=失败（打印卡在哪一步 + 两端日志关键行）
"""
import argparse
import ctypes
import os
import subprocess
import sys
import time

sys.path.insert(0, r'D:\文档\ai001\.dsh\skills\gui-automation\references')
import gui

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

ROOT = r'D:\文档\ai001'
T0 = time.time()


def say(msg):
    print('  [%5.1fs] %s' % (time.time() - T0, msg), flush=True)


def fail(msg, extra=''):
    print('  [FAIL] ' + msg, flush=True)
    if extra:
        print(extra, flush=True)
    subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
    sys.exit(1)


def app_pid():
    r = subprocess.run(['powershell', '-NoProfile', '-Command',
                        "(Get-Process ZongxianVoice -ErrorAction SilentlyContinue | "
                        "Where-Object { $_.Path } | Select-Object -First 1).Id"],
                       capture_output=True, text=True, timeout=20)
    try:
        return int((r.stdout or '').strip().splitlines()[-1])
    except Exception:
        return 0


LOCATE_NAME = r'''
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$c1 = New-Object System.Windows.Automation.PropertyCondition($AE::NameProperty, '__NAME__')
$c2 = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, __PID__)
$cond = New-Object System.Windows.Automation.AndCondition($c1, $c2)
$el = $AE::RootElement.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
if ($null -eq $el) { Write-Output 'null'; exit 0 }
try { if ($el.Current.IsOffscreen) { Write-Output 'offscreen'; exit 0 } } catch { Write-Output 'null'; exit 0 }
$r = $el.Current.BoundingRectangle
Write-Output ('{0},{1},{2},{3}' -f [int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height)
'''

LOCATE_ID = r'''
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$c1 = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, '__ID__')
$c2 = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, __PID__)
$cond = New-Object System.Windows.Automation.AndCondition($c1, $c2)
$el = $AE::RootElement.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
if ($null -eq $el) { Write-Output 'null'; exit 0 }
try { if ($el.Current.IsOffscreen) { Write-Output 'offscreen'; exit 0 } } catch { Write-Output 'null'; exit 0 }
$r = $el.Current.BoundingRectangle
Write-Output ('{0},{1},{2},{3}' -f [int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height)
'''


def _locate(template, key, value, pid):
    script = template.replace(key, value).replace('__PID__', str(pid))
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                            '-Command', script], capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return None
    line = (r.stdout or '').strip().splitlines()
    line = line[-1].strip() if line else ''
    if line in ('', 'null', 'offscreen'):
        return None
    try:
        x, y, w, h = [int(v) for v in line.split(',')]
    except ValueError:
        return None
    return (x, y, w, h) if w > 5 and h > 5 else None


def locate_name(text, pid):
    return _locate(LOCATE_NAME, '__NAME__', text, pid)


def locate_id(aid, pid):
    return _locate(LOCATE_ID, '__ID__', aid, pid)


def click_rect(rect, what):
    x, y, w, h = rect
    cx, cy = x + w // 2, y + h // 2
    gui.move(cx, cy)
    time.sleep(0.2)
    gui.click(cx, cy)
    say('点击「%s」(%d,%d)' % (what, cx, cy))


def click_name(text, pid, what=None, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        r = locate_name(text, pid)
        if r:
            click_rect(r, what or text)
            return True
        time.sleep(0.8)
    return False


def log_lines(path, keys):
    try:
        lines = open(path, encoding='utf-8', errors='replace').read().splitlines()
    except OSError:
        return []
    return [l for l in lines if any(k in l for k in keys)]


def dump(tag, a, b):
    out = ['=== %s ===' % tag]
    for name, path in (('A(房主)', a), ('B(加入)', b)):
        keep = log_lines(path, ('共享', '轨道', '屏幕声音', '屏幕共享失败'))
        out.append('--- %s ---' % name)
        out += ['  ' + l for l in keep[-12:]]
    txt = '\n'.join(out) + '\n'
    open(os.path.join(ROOT, 'tmp', 'p4-final.txt'), 'w', encoding='utf-8').write(txt)
    return txt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exe', required=True)
    ap.add_argument('--portA', type=int, default=46091)
    ap.add_argument('--portB', type=int, default=46092)
    a = ap.parse_args()
    logA = os.path.join(ROOT, 'tmp', 'p4f-A.log')
    logB = os.path.join(ROOT, 'tmp', 'p4f-B.log')
    for p in (logA, logB):
        if os.path.exists(p):
            os.remove(p)

    subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
    time.sleep(1)
    subprocess.run('start "" /b "%s" --port %d --room p4f --name 阿伟 --log-file "%s"'
                   % (a.exe, a.portA, logA), shell=True)
    say('房主 A 已启动')
    time.sleep(9)
    subprocess.run('start "" /b "%s" --port %d --room p4f --name 小棕 --signal ws://127.0.0.1:%d/signal --log-file "%s"'
                   % (a.exe, a.portB, a.portA, logB), shell=True)
    say('加入方 B 已启动，等两边连上')

    pid = 0
    for _ in range(30):
        pid = app_pid()
        if pid and locate_id('ShareScreenButton', pid):
            break
        time.sleep(1)
    if not pid:
        fail('应用没起来')
    say('应用就绪 pid=%d' % pid)

    # 等对端进入（成员数变 2 才会启用共享按钮）
    time.sleep(8)
    share = locate_id('ShareScreenButton', pid)
    if not share:
        fail('找不到共享屏幕按钮')
    click_rect(share, '共享屏幕')
    say('已请求共享，等系统弹窗…')

    # 弹窗里：整个屏幕 -> 使用系统音频共享 -> 共享（全部按文字定位）
    if not click_name('整个屏幕', pid, '整个屏幕标签', timeout=25):
        gui.screenshot(os.path.join(ROOT, 'tmp', 'p4f-picker.png'))
        fail('弹窗里没找到「整个屏幕」（截图 tmp/p4f-picker.png）')
    time.sleep(1.5)
    if not click_name('使用系统音频共享', pid, '使用系统音频共享开关', timeout=15):
        gui.screenshot(os.path.join(ROOT, 'tmp', 'p4f-picker2.png'))
        fail('弹窗里没找到「使用系统音频共享」（截图 tmp/p4f-picker2.png）')
    time.sleep(1.5)
    if not click_name('共享', pid, '共享按钮', timeout=15):
        fail('弹窗里没找到「共享」按钮')

    say('已点共享，等采集与传输…')
    time.sleep(10)
    gui.screenshot(os.path.join(ROOT, 'tmp', 'p4f-sharing.png'))

    txt = dump('P4 e2e', logA, logB)
    print(txt)
    subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)

    ok_a = any('采集轨道：音频=1' in l for l in log_lines(logA, ('轨道',)))
    ok_b = any('屏幕声音已到达' in l for l in log_lines(logB, ('屏幕声音',)))
    if ok_a and ok_b:
        print('  [PASS] 共享音频端到端：房主采到音频轨 + 对端收到并播放', flush=True)
        return 0
    print('  [FAIL] 采集到音频=%s，对端收到屏幕声音=%s' % (ok_a, ok_b), flush=True)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
