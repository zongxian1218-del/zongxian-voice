# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:/文档/ai001/build/entry.py'],
    pathex=['D:/文档/ai001/src'],
    binaries=[],
    datas=[('D:/文档/ai001/dist/swiftdrop.html', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'pandas', 'PIL', 'matplotlib', 'scipy', 'PySide6', 'PyQt5'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SwiftDrop',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['D:/文档/ai001/dist/swiftdrop.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SwiftDrop',
)
