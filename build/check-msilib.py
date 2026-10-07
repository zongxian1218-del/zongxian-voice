#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""检查本机 Python 是否自带 msilib（用于生成 MSI 安装包）。"""
import shutil
import sys

print('python', sys.version.split()[0])
try:
    import msilib
    need = ['init_database', 'schema', 'Directory', 'Feature', 'Component',
            'File', 'Shortcut', 'Registry', 'add_data', 'add_stream', 'CAD',
            'Control', 'Dialog', 'RadioButtonGroup']
    have = [x for x in need if hasattr(msilib, x)]
    print('msilib 可用，具备 API：', have)
    print('缺失：', [x for x in need if x not in have])
except Exception as e:
    print('msilib 不可用：', type(e).__name__, e)

print('msiexec:', shutil.which('msiexec') or r'C:\Windows\System32\msiexec.exe')
print('wix:', shutil.which('wix'), ' candle:', shutil.which('candle'), ' makensis:', shutil.which('makensis'))
