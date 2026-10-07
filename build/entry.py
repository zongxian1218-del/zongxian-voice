#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SwiftDrop.exe 的入口脚本（源码运行时等价于 python -m swiftdrop）。

打包成单文件 exe 后：
* 会把内嵌的 swiftdrop.html 释放到 exe 同级目录（找不到时才释放），
  并把工作目录切到 exe 所在目录，这样 webhost 的默认根目录就是它；
* 双击（没有参数、且控制台是本进程独占的）时自动隐藏黑窗口并直接开图形界面；
* 从终端调用时保留完整命令行输出。
"""
import os
import sys


def _frozen():
    return getattr(sys, 'frozen', False)


def _owns_console():
    """控制台是否为本进程独占（= 双击启动），而不是从已有终端继承的。"""
    try:
        import ctypes
        k = ctypes.windll.kernel32
        arr = (ctypes.c_uint * 8)()
        n = k.GetConsoleProcessList(arr, 8)
        return n <= 1
    except Exception:
        return False


def _hide_console():
    try:
        import ctypes
        k = ctypes.windll.kernel32
        u = ctypes.windll.user32
        hwnd = k.GetConsoleWindow()
        if hwnd:
            u.ShowWindow(hwnd, 0)
    except Exception:
        pass


def _prepare_html():
    """把内嵌的网页版释放到 exe 同级目录。"""
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    target = os.path.join(exe_dir, 'swiftdrop.html')
    if os.path.exists(target):
        return
    for base in (getattr(sys, '_MEIPASS', None), os.path.dirname(os.path.abspath(__file__))):
        if not base:
            continue
        src = os.path.join(base, 'swiftdrop.html')
        if os.path.exists(src):
            try:
                import shutil
                shutil.copyfile(src, target)
            except Exception:
                pass
            return


def main():
    if _frozen():
        try:
            _prepare_html()
            os.chdir(os.path.dirname(os.path.abspath(sys.executable)))
        except Exception:
            pass
        if len(sys.argv) == 1 and _owns_console():
            _hide_console()
            sys.argv.append('gui')

    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(os.path.dirname(here), 'src')
    for p in (src, here):
        if p not in sys.path:
            sys.path.insert(0, p)
    from swiftdrop.cli import main as cli_main
    return cli_main()


if __name__ == '__main__':
    sys.exit(main())
