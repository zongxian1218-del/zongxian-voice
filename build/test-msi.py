#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MSI 安装包端到端自测：静默安装 → 核验 → 运行 → 静默卸载 → 核验清理。

MSI 由 Windows Installer 服务执行，所以注册表和快捷方式的写入**不受本机对子进程的文件沙箱限制**，
这里可以完整核验（不像 NSIS 的 exe 安装包只能在工程内验证文件层面）。

用法：python build/test-msi.py [msi路径]
"""
import os
import subprocess
import sys
import time
import winreg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MSI = os.path.join(ROOT, 'dist', '棕仙的传输软件-安装包-1.0.msi')
APP_NAME = '棕仙的传输软件'
APP_EXE = APP_NAME + '.exe'
INSTALL_DIR = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', APP_NAME)
SM_DIR = os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Windows', 'Start Menu',
                      'Programs', APP_NAME)
DT_LNK = os.path.join(os.environ.get('USERPROFILE', ''), 'Desktop', APP_NAME + '.lnk')
UNINST = r'Software\Microsoft\Windows\CurrentVersion\Uninstall'

results = []


def check(name, passed, detail=''):
    results.append((name, bool(passed), detail))
    print('  [%s] %s%s' % ('PASS' if passed else 'FAIL', name, ('  — ' + detail) if detail else ''))


def find_product():
    """在 HKCU 卸载项里找本产品，返回 (product_code, 值字典)。"""
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINST)
    except OSError:
        return None, {}
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(k, i)
            i += 1
        except OSError:
            break
        try:
            sk = winreg.OpenKey(k, sub)
            try:
                dn, _ = winreg.QueryValueEx(sk, 'DisplayName')
            except OSError:
                dn = None
            if dn == APP_NAME:
                vals = {}
                j = 0
                while True:
                    try:
                        n, v, _ = winreg.EnumValue(sk, j)
                        vals[n] = v
                        j += 1
                    except OSError:
                        break
                winreg.CloseKey(sk)
                winreg.CloseKey(k)
                return sub, vals
            winreg.CloseKey(sk)
        except OSError:
            continue
    winreg.CloseKey(k)
    return None, {}


def msiexec(args, timeout=900):
    r = subprocess.run(['msiexec'] + args, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=timeout)
    return r


def main():
    msi = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith('--') else DEFAULT_MSI
    if not os.path.exists(msi):
        print('找不到 MSI：%s' % msi)
        return 2
    print('MSI：%s（%.1f MB）' % (msi, os.path.getsize(msi) / 1048576.0))

    # 先清理可能存在的旧安装
    pc, _ = find_product()
    if pc:
        print('清理已安装的旧版本 %s …' % pc)
        msiexec(['/x', pc, '/qn', '/norestart'])
        time.sleep(3)

    print('\n--- 静默安装（msiexec /i /qn）---')
    log = os.path.join(ROOT, 'tmp', 'msi-install.log')
    os.makedirs(os.path.dirname(log), exist_ok=True)
    r = msiexec(['/i', msi, '/qn', '/norestart', '/l*v', log])
    exe = os.path.join(INSTALL_DIR, APP_EXE)
    ok_install = r.returncode == 0 and os.path.exists(exe)
    check('安装成功（退出码 0 且主程序落地）', ok_install,
          'exit=%s exe=%s' % (r.returncode, os.path.exists(exe)))
    if not ok_install:
        print('  安装日志尾部：')
        try:
            with open(log, encoding='utf-16', errors='replace') as f:
                tail = f.readlines()[-25:]
            print('   ' + ''.join(tail).replace('\n', '\n   '))
        except OSError:
            pass
        print('=== 自测失败 ===')
        return 1

    time.sleep(2)
    inner = os.path.join(INSTALL_DIR, '_internal')
    need = ['swiftdrop.html', 'zongxian-transfer-guide.md']
    missing = [n for n in need if not os.path.exists(os.path.join(INSTALL_DIR, n))]
    check('配套文件齐备（网页版/说明/_internal）', not missing and os.path.isdir(inner),
          ('缺 ' + ','.join(missing)) if missing else '_internal 存在')

    pc, vals = find_product()
    check('“设置→应用”里有卸载项', bool(pc) and vals.get('DisplayName') == APP_NAME,
          'ProductCode=%s DisplayVersion=%s EstimatedSize=%s' % (pc, vals.get('DisplayVersion'), vals.get('EstimatedSize')))

    check('开始菜单快捷方式', os.path.exists(os.path.join(SM_DIR, APP_NAME + '.lnk')),
          os.path.join(SM_DIR, APP_NAME + '.lnk'))
    check('桌面快捷方式', os.path.exists(DT_LNK), DT_LNK)

    try:
        r2 = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=180,
                            encoding='utf-8', errors='replace')
        out = (r2.stdout or '').strip()
        run_ok = r2.returncode == 0 and '1.0' in out
    except Exception as e:
        out, run_ok = str(e), False
    check('安装后的程序可运行', run_ok, out)

    print('\n--- 静默卸载（msiexec /x /qn）---')
    r3 = msiexec(['/x', pc, '/qn', '/norestart'])
    time.sleep(3)
    gone = not os.path.exists(exe)
    pc2, _ = find_product()
    check('卸载成功且清空安装目录', r3.returncode == 0 and gone,
          'exit=%s 主程序已删=%s' % (r3.returncode, gone))
    check('卸载项已清除', pc2 is None, '剩余=%s' % pc2)
    check('开始菜单目录已清除', not os.path.exists(SM_DIR))
    check('桌面快捷方式已清除', not os.path.exists(DT_LNK))

    bad = sum(1 for _, p, _ in results if not p)
    print('\n================ 汇总 ================')
    for n, p, d in results:
        print(('PASS  ' if p else 'FAIL  ') + n + (('  — ' + d) if d else ''))
    print('%d/%d 通过' % (len(results) - bad, len(results)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
