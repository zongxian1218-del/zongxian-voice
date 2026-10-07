#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""安装包端到端自测：静默安装 → 逐项核验 → 静默卸载 → 核验清理。

不用 PowerShell 的 Start-Process（本环境会挂住），改用 `cmd /c start /b` 分离启动 + 轮询文件；
注册表一律用 winreg 读，避免控制台编码干扰。

用法：python build/test-installer.py [安装包路径] [--dir <指定安装目录>]
"""
import os
import subprocess
import sys
import time
import winreg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MSI = os.path.join(ROOT, 'dist', '棕仙的传输软件-安装包-1.0.exe')
APP_NAME = '棕仙的传输软件'
APPEXE = '棕仙的传输软件.exe'
UNINST_SUB = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\ZongxianTransfer'
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
REG_KEY = r'Software\ZongxianTransfer'
DEFAULT_DIR = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', APP_NAME)

# 本机对工程目录之外写入受限，编译/安装都需要工程内的 TEMP
_TMP = os.path.join(ROOT, 'tmp', 'buildtemp')
os.makedirs(_TMP, exist_ok=True)
os.environ['TEMP'] = _TMP
os.environ['TMP'] = _TMP

results = []


def check(name, passed, detail=''):
    results.append((name, bool(passed), detail))
    print('  [%s] %s%s' % ('PASS' if passed else 'FAIL', name, ('  — ' + detail) if detail else ''))


def launch_detached(exe, args):
    """分离启动，避免依赖会被沙箱挂住的进程句柄。"""
    subprocess.run('start "" /b "%s" %s' % (exe, ' '.join(args)), shell=True, timeout=120)


def wait_for(pred, timeout=90, interval=1.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(interval)
    return False


def reg_read(root, path, name=None):
    try:
        k = winreg.OpenKey(root, path)
    except OSError:
        return None
    try:
        if name is None:
            out = {}
            i = 0
            while True:
                try:
                    n, v, _ = winreg.EnumValue(k, i)
                    out[n] = v
                    i += 1
                except OSError:
                    break
            return out
        v, _ = winreg.QueryValueEx(k, name)
        return v
    except OSError:
        return None
    finally:
        winreg.CloseKey(k)


def find_uninstall_keys():
    """在 Uninstall 下找所有名字里含 Zongxian 的子键（含 Wow6432Node 视角）。"""
    found = []
    base = r'Software\Microsoft\Windows\CurrentVersion\Uninstall'
    for root, label in ((winreg.HKEY_CURRENT_USER, 'HKCU'), (winreg.HKEY_LOCAL_MACHINE, 'HKLM')):
        for path in (base, r'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'):
            try:
                k = winreg.OpenKey(root, path)
            except OSError:
                continue
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                    i += 1
                except OSError:
                    break
                if 'zongxian' in sub.lower() or '棕仙' in sub:
                    found.append('%s\\%s\\%s' % (label, path, sub))
            winreg.CloseKey(k)
    return found


def main():
    msi = DEFAULT_MSI
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if args:
        msi = args[0]
    target = DEFAULT_DIR
    if '--dir' in sys.argv:
        target = sys.argv[sys.argv.index('--dir') + 1]
    exe_name = APPEXE
    if '--appexe' in sys.argv:
        exe_name = sys.argv[sys.argv.index('--appexe') + 1]
    if not os.path.exists(msi):
        print('找不到安装包：%s' % msi)
        return 2

    print('安装包：%s（%.1f MB）' % (msi, os.path.getsize(msi) / 1048576.0))
    print('安装目录：%s' % target)
    print('主程序名：%s' % exe_name)

    # 清理现场
    exe = os.path.join(target, exe_name)
    un = os.path.join(target, 'Uninstall.exe')
    if os.path.exists(un):
        launch_detached(un, ['/S', '_?=%s' % target])
        wait_for(lambda: not os.path.exists(exe), 60)
    sm_dir = os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Windows', 'Start Menu', 'Programs', APP_NAME)
    dt_lnk = os.path.join(os.environ.get('USERPROFILE', ''), 'Desktop', APP_NAME + '.lnk')

    print('\n--- 静默安装 ---')
    extra = [] if target == DEFAULT_DIR else ['/D=%s' % target]
    launch_detached(msi, ['/S'] + extra)
    installed = wait_for(lambda: os.path.exists(exe), 120)
    check('主程序已安装', installed, exe)

    if not installed:
        # 退一步：确认是不是本环境禁止写该目录
        try:
            os.makedirs(os.path.join(os.path.dirname(target), '_probe_write'), exist_ok=True)
            check('（诊断）宿主能写安装目录的父目录', True, os.path.dirname(target))
        except OSError as e:
            check('（诊断）宿主能写安装目录的父目录', False, str(e))
        print('=== 自测失败：安装包没有落地 ===')
        return 1

    # 关键：文件是边解压边复制的，注册表/快捷方式在复制**之后**才写。
    # 必须等到最后一个写操作出现（开机自启项）才算安装结束，否则会误判。
    settled = wait_for(lambda: reg_read(winreg.HKEY_CURRENT_USER, RUN_KEY, APP_NAME) is not None, 180)
    if not settled:
        # 也许用户没勾选自启（静默安装默认全选，这里只是兜底）：再等卸载项
        settled = wait_for(lambda: reg_read(winreg.HKEY_CURRENT_USER, UNINST_SUB, 'DisplayName') is not None, 60)
    time.sleep(3)
    print('  （安装收敛等待：%s）' % ('完成' if settled else '超时'))

    # 配套文件
    # 【2026-10-05 修正】这里原来找 swiftdrop-guide.md —— 那是改名前的旧文件名，
    # 而 installer.nsi 装的是 zongxian-transfer-guide.md，于是这条检查永远报"缺"。
    # 顺手确认：zongxian-synced.ico / zongxian-icon.png / zongxian.ico 也要在（窗口与同步图标靠它们）。
    #
    # ⚠️ 下面"快捷方式 / 卸载注册表项 / 安装目录可写"这几条在**本机开发环境里必然失败**：
    #    本环境禁止写工程目录之外的位置，而桌面、开始菜单、注册表都在工程外，写入会被静默拒绝。
    #    证据：用 NSIS 的 TRACE 宏编译后静默安装，追踪文件里三行都在
    #    （桌面快捷方式 / 卸载注册表项 / 开始菜单快捷方式），但装完去查什么都没有。
    #    同类现象：NSIS 临时目录在工程外时编译会报 "error creating mmap"，
    #    把 TEMP 指到工程内就正常（build\make-installer.py 第 24–31 行早就这么做了）。
    #    → 这几条要真正确认，得在不受此限制的机器上装一次；不要在这里反复追。
    inner = os.path.join(target, '_internal')
    need = ['zongxian-transfer-guide.md', 'swiftdrop.html',
            'zongxian-synced.ico', 'zongxian-icon.png']
    missing = [n for n in need if not os.path.exists(os.path.join(target, n))]
    check('配套文件齐备（说明/网页版/图标/_internal）', not missing and os.path.isdir(inner),
          ('缺 ' + ','.join(missing)) if missing else '_internal 存在')

    # 控制面板卸载项
    dn = reg_read(winreg.HKEY_CURRENT_USER, UNINST_SUB, 'DisplayName')
    ver = reg_read(winreg.HKEY_CURRENT_USER, UNINST_SUB, 'DisplayVersion')
    us = reg_read(winreg.HKEY_CURRENT_USER, UNINST_SUB, 'UninstallString')
    es = reg_read(winreg.HKEY_CURRENT_USER, UNINST_SUB, 'EstimatedSize')
    keys = find_uninstall_keys()
    check('“程序和功能”里有卸载项', dn == APP_NAME and bool(us),
          'DisplayName=%r DisplayVersion=%r EstimatedSize=%r 命中键=%s' % (dn, ver, es, keys))

    # 快捷方式
    sm_ok = os.path.exists(os.path.join(sm_dir, APP_NAME + '.lnk'))
    dt_ok = os.path.exists(dt_lnk)
    check('开始菜单快捷方式', sm_ok, os.path.join(sm_dir, APP_NAME + '.lnk'))
    check('桌面快捷方式', dt_ok, dt_lnk)

    # 开机自启
    rv = reg_read(winreg.HKEY_CURRENT_USER, RUN_KEY, APP_NAME)
    auto_ok = bool(rv) and 'autosync' in str(rv)
    check('开机自启项已写入', auto_ok, repr(rv))

    # 装好的程序可运行
    try:
        r = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=120,
                           encoding='utf-8', errors='replace', env=dict(os.environ))
        out = (r.stdout or '').strip()
        run_ok = r.returncode == 0 and '1.0' in out
    except Exception as e:
        out, run_ok = str(e), False
    check('安装后的程序可运行', run_ok, out)

    # 网页服务可用（打包后的 html 与 exe 同级）
    html_ok = os.path.exists(os.path.join(target, 'swiftdrop.html'))
    check('网页版随安装包一起装上', html_ok)

    print('\n--- 静默卸载 ---')
    launch_detached(un, ['/S', '_?=%s' % target])
    gone = wait_for(lambda: not os.path.exists(exe), 120)
    check('主程序已删除', gone)
    check('卸载项已清除', reg_read(winreg.HKEY_CURRENT_USER, UNINST_SUB, 'DisplayName') is None,
          '剩余命中键=%s' % find_uninstall_keys())
    check('自启项已清除', reg_read(winreg.HKEY_CURRENT_USER, RUN_KEY, APP_NAME) is None)
    check('开始菜单目录已清除', not os.path.exists(sm_dir))
    check('桌面快捷方式已清除', not os.path.exists(dt_lnk))

    bad = sum(1 for _, p, _ in results if not p)
    print('\n================ 汇总 ================')
    for n, p, d in results:
        print(('PASS  ' if p else 'FAIL  ') + n + (('  — ' + d) if d else ''))
    print('%d/%d 通过' % (len(results) - bad, len(results)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
