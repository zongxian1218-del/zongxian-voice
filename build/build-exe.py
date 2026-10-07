#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把桌面版打包成 exe。

两种形态：
  --onedir  （默认，推荐）生成 dist/棕仙的传输软件/ 目录，并压成 dist/zongxian-portable-win64.zip
                         启动时不需要解压到 %TEMP%，在受限环境里更稳、启动也更快
  --onefile            生成 dist/zongxian-onefile.exe 单文件（方便放 U 盘/单发）
用法：python build/build-exe.py [--onefile]

（2026-10-05 修正：这里的文档原先写的是 dist/SwiftDrop/ 与 swiftdrop-portable-win64.zip，
  与代码实际产出的名字不符 —— 改名之后文档没跟上。）
"""
import datetime
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(ROOT, 'build', 'pack-venv', 'Scripts', 'python.exe')
ENTRY = os.path.join(ROOT, 'build', 'entry.py')
HTML = os.path.join(ROOT, 'dist', 'swiftdrop.html')
ICO = os.path.join(ROOT, 'dist', 'zongxian-synced.ico')
DIST = os.path.join(ROOT, 'dist')
APP_NAME = '棕仙的传输软件'
EXCLUDES = ['numpy', 'pandas', 'PIL', 'matplotlib', 'scipy', 'PySide6', 'PyQt5']
# 这份产物由哪些源码决定 —— 写进 BUILD-INFO，守卫用它判断"产物之后源码有没有又改过"
SOURCES = 'src/swiftdrop, src/web, dist/swiftdrop.html, build/entry.py'


def git_out(args):
    for cand in (r'D:\BuildTools\MinGit\cmd\git.exe', 'git'):
        try:
            r = subprocess.run([cand, *args], cwd=ROOT, capture_output=True, text=True,
                               encoding='utf-8', errors='replace')
            if r.returncode == 0:
                return r.stdout.strip()
        except FileNotFoundError:
            continue
    return ''


def write_build_info(out_dir):
    """在便携目录里写溯源文件（和 WinUI 那边同一套约定）。

    为什么：这份产物以前**没有任何"是哪次构建"的记录**，只能靠文件时间猜，
    而本仓库是 2026-10-05 才初始化的，"时间"这条路更不可靠。
    """
    commit = git_out(['rev-parse', '--short', 'HEAD']) or 'unknown'
    if git_out(['status', '--porcelain']):
        commit += '+dirty'
    lines = [
        'commit: %s' % commit,
        'built:  %s' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'config: PyInstaller onedir / --console / Python %s' % sys.version.split()[0],
        'sources: %s' % SOURCES,
        'rebuild: python build\\build-exe.py （用 build\\pack-venv 里的 python）',
    ]
    with open(os.path.join(out_dir, 'BUILD-INFO.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print('BUILD-INFO.txt: commit=%s' % commit)


def run_pyi(extra, name, distpath, workpath):
    args = [VENV_PY, '-m', 'PyInstaller', '--noconfirm', '--clean',
            '--name', name, '--console', '--icon', ICO,
            '--add-data', '%s%s.' % (HTML, os.pathsep),
            '--paths', os.path.join(ROOT, 'src'),
            '--distpath', distpath, '--workpath', workpath,
            '--specpath', os.path.join(ROOT, 'build')]
    for m in EXCLUDES:
        args += ['--exclude-module', m]
    args += extra + [ENTRY]
    print('打包 %s …' % name)
    r = subprocess.run(args, cwd=ROOT)
    return r.returncode


def main():
    onefile_only = '--onefile' in sys.argv
    if not os.path.exists(VENV_PY):
        print('缺少打包环境 %s（先建 venv 并 pip install pyinstaller）' % VENV_PY)
        return 2
    if not os.path.exists(HTML):
        print('缺少 %s，请先运行 build/build-web.py' % HTML)
        return 2

    rc = 0
    if onefile_only:
        rc |= run_pyi(['--onefile'], APP_NAME, DIST, os.path.join(ROOT, 'build', 'pyi-onefile'))
        exe = os.path.join(DIST, APP_NAME + '.exe')
        if os.path.exists(exe):
            target = os.path.join(DIST, 'zongxian-onefile.exe')
            shutil.move(exe, target)
            print('完成：%s (%.1f MB)' % (target, os.path.getsize(target) / 1048576.0))
    else:
        rc |= run_pyi(['--onedir'], APP_NAME, os.path.join(ROOT, 'build', 'pyi-onedir'), os.path.join(ROOT, 'build', 'pyi-work'))
        src_dir = os.path.join(ROOT, 'build', 'pyi-onedir', APP_NAME)
        out_dir = os.path.join(DIST, APP_NAME)
        if os.path.isdir(src_dir):
            if os.path.isdir(out_dir):
                shutil.rmtree(out_dir, ignore_errors=True)
            shutil.copytree(src_dir, out_dir)
            # 使用说明放进便携目录，随安装包/压缩包一起分发自解释
            guide = os.path.join(DIST, 'zongxian-transfer-guide.md')
            if os.path.exists(guide):
                shutil.copyfile(guide, os.path.join(out_dir, 'zongxian-transfer-guide.md'))
            # 图标资源也要随目录一起发：窗口图标用 zongxian-icon.png，
            # "同步文件夹自动换图标"用 zongxian-synced.ico（缺了这两个功能会静默失效）
            for extra in ('zongxian-synced.ico', 'zongxian-icon.png', 'zongxian.ico'):
                src = os.path.join(DIST, extra)
                if os.path.exists(src):
                    shutil.copyfile(src, os.path.join(out_dir, extra))
                else:
                    print('提示：缺少 %s（窗口图标/同步文件夹图标可能不可用）' % src)
            size = sum(os.path.getsize(os.path.join(dp, f)) for dp, dn, fn in os.walk(out_dir) for f in fn)
            print('完成：%s  (%.1f MB)' % (out_dir, size / 1048576.0))
            # 溯源文件必须在压缩之前写进去，这样 zip 里也带着它
            write_build_info(out_dir)
            zip_base = os.path.join(DIST, 'zongxian-portable-win64')
            if os.path.exists(zip_base + '.zip'):
                os.remove(zip_base + '.zip')
            print('压缩 zip …')
            shutil.make_archive(zip_base, 'zip', root_dir=DIST, base_dir=APP_NAME)
            zp = zip_base + '.zip'
            if os.path.exists(zp):
                print('完成：%s (%.1f MB)' % (zp, os.path.getsize(zp) / 1048576.0))
        else:
            print('打包失败：没有生成 %s' % src_dir)
            rc = 1
    return rc


if __name__ == '__main__':
    sys.exit(main())
