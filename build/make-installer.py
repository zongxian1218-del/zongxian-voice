#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用 NSIS 生成 Windows 安装包 dist/棕仙的传输软件-安装包-1.0.exe。

用法：
  python build/make-installer.py            # 只编译
  python build/make-installer.py --test     # 编译后静默安装 → 校验 → 静默卸载 → 校验清理
"""
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NSIS = os.path.join(ROOT, 'build', 'nsis', 'nsis-3.10', 'makensis.exe')
SCRIPT = os.path.join(ROOT, 'build', 'installer.nsi')
APPDIR = os.path.join(ROOT, 'dist', '棕仙的传输软件')
OUT = os.path.join(ROOT, 'dist', '棕仙的传输软件-安装包-1.0.exe')
ICON = os.path.join(ROOT, 'dist', 'zongxian-synced.ico')
APPEXE = '棕仙的传输软件.exe'
INSTALL_DIR = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', '棕仙的传输软件')

# 本机对工程目录之外的临时目录写入受限，编译/安装/卸载都需要把 TEMP 指到工程内
_TMP = os.path.join(ROOT, 'tmp', 'buildtemp')
try:
    os.makedirs(_TMP, exist_ok=True)
    os.environ['TEMP'] = _TMP
    os.environ['TMP'] = _TMP
except OSError:
    pass


def _arg(flag, default):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def compile_installer():
    if not os.path.exists(NSIS):
        print('缺少 NSIS：%s' % NSIS)
        return None
    if not os.path.exists(os.path.join(APPDIR, APPEXE)):
        print('缺少 %s，请先用 build/build-exe.py 打包桌面版' % os.path.join(APPDIR, APPEXE))
        return None
    if os.path.exists(OUT):
        os.remove(OUT)
    args = [NSIS, '/INPUTCHARSET', 'UTF8', '/V2',
            '/DAPPDIR=%s' % APPDIR, '/DOUTFILE=%s' % OUT,
            '/DICONFILE=%s' % ICON, '/DAPPEXE=%s' % APPEXE, SCRIPT]
    print('编译安装包…')
    r = subprocess.run(args, cwd=ROOT)
    if r.returncode != 0 or not os.path.exists(OUT):
        print('编译失败，返回码 %s' % r.returncode)
        return None
    print('完成：%s（%.1f MB）' % (OUT, os.path.getsize(OUT) / 1048576.0))
    return OUT


def reg_query(name):
    try:
        import winreg
    except ImportError:
        return None
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r'Software\Microsoft\Windows\CurrentVersion\Uninstall\ZongxianTransfer')
        v, _ = winreg.QueryValueEx(k, name)
        winreg.CloseKey(k)
        return v
    except OSError:
        return None


def run_test(msi_exe):
    print('\n=== 安装包自测 ===')
    ok = True

    # 1) 静默安装
    print('静默安装（/S）…')
    subprocess.run([msi_exe, '/S'], timeout=600)
    time.sleep(3)
    exe = os.path.join(INSTALL_DIR, APPEXE)
    exists = os.path.exists(exe)
    ok &= exists
    print('  [%s] 主程序已安装：%s' % ('PASS' if exists else 'FAIL', exe))

    # 2) 配套文件
    need = ['swiftdrop-guide.md', 'Uninstall.exe']
    inner = os.path.join(INSTALL_DIR, '_internal')
    files_ok = all(os.path.exists(os.path.join(INSTALL_DIR, n)) for n in need) and os.path.isdir(inner)
    ok &= files_ok
    print('  [%s] 使用说明/卸载器/运行库目录齐备（_internal 存在=%s）' % ('PASS' if files_ok else 'FAIL', os.path.isdir(inner)))

    # 3) 控制面板卸载项
    dn = reg_query('DisplayName')
    ver = reg_query('DisplayVersion')
    us = reg_query('UninstallString')
    arp_ok = dn == '棕仙的传输软件' and bool(us)
    ok &= arp_ok
    print('  [%s] “程序和功能”里有卸载项：DisplayName=%r DisplayVersion=%r UninstallString=%r' % ('PASS' if arp_ok else 'FAIL', dn, ver, us))

    # 4) 快捷方式
    sm = os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Windows', 'Start Menu', 'Programs', '棕仙的传输软件', '棕仙的传输软件.lnk')
    dt = os.path.join(os.environ.get('USERPROFILE', ''), 'Desktop', '棕仙的传输软件.lnk')
    sc_ok = os.path.exists(sm)
    ok &= sc_ok
    print('  [%s] 开始菜单快捷方式：%s ；桌面快捷方式：%s' % ('PASS' if sc_ok else 'FAIL', os.path.exists(sm), os.path.exists(dt)))

    # 5) 开机自启项
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run')
        rv, _ = winreg.QueryValueEx(k, '棕仙的传输软件')
        winreg.CloseKey(k)
    except OSError:
        rv = None
    auto_ok = bool(rv) and 'autosync' in str(rv)
    ok &= auto_ok
    print('  [%s] 开机自启项：%r' % ('PASS' if auto_ok else 'FAIL', rv))

    # 6) 装好的程序能跑
    try:
        r = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=120, encoding='utf-8', errors='replace')
        run_ok = r.returncode == 0 and '1.0' in (r.stdout or '')
    except Exception as e:
        run_ok = False
        r = None
    ok &= run_ok
    print('  [%s] 安装后的 exe 可运行：%s' % ('PASS' if run_ok else 'FAIL', (r.stdout or '').strip() if r else ''))

    # 7) 静默卸载
    print('静默卸载…')
    un = os.path.join(INSTALL_DIR, 'Uninstall.exe')
    if os.path.exists(un):
        subprocess.run([un, '/S', '_?=%s' % INSTALL_DIR], timeout=600)
        time.sleep(4)
    gone = not os.path.exists(os.path.join(INSTALL_DIR, APPEXE))
    arp_gone = reg_query('DisplayName') is None
    sm_gone = not os.path.exists(os.path.dirname(sm))
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run')
        try:
            winreg.QueryValueEx(k, '棕仙的传输软件')
            auto_gone = False
        except OSError:
            auto_gone = True
        winreg.CloseKey(k)
    except OSError:
        auto_gone = True
    ok &= gone and arp_gone and auto_gone
    print('  [%s] 卸载干净：主程序已删=%s 卸载项已删=%s 开始菜单已删=%s 自启项已删=%s' %
          ('PASS' if (gone and arp_gone and sm_gone and auto_gone) else 'FAIL', gone, arp_gone, sm_gone, auto_gone))

    if not gone:
        print('  （残留目录：%s，为不污染系统这里只报告不强制删除）' % INSTALL_DIR)
    print('=== 自测%s ===' % ('全部通过' if ok else '存在失败项'))
    return ok


def main():
    global APPDIR, OUT, APPEXE
    APPDIR = _arg('--appdir', APPDIR)
    OUT = _arg('--out', OUT)
    APPEXE = _arg('--appexe', APPEXE)
    exe = compile_installer()
    if not exe:
        return 1
    if '--test' in sys.argv:
        return 0 if run_test(exe) else 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
