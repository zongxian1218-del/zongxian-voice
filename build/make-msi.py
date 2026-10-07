#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用 WiX 3 生成标准 MSI 安装包：dist/棕仙的传输软件-安装包-1.0.msi

为什么除了 NSIS 还做 MSI：
  * MSI 由 Windows Installer 服务执行安装，**不需要把自身解压到 %TEMP%**，
    因此在"临时目录被安全策略/杀软限制"的机器上也能装（NSIS 的那种 exe 安装包会报
    "Error launching installer"）；
  * 安装/卸载全程可静默、可回滚，是 Windows 上最"规范"的安装包格式。

用法：
  python build/make-msi.py            # 只编译
  python build/make-msi.py --test     # 编译后静默安装 → 核验 → 静默卸载 → 核验清理
"""
import os
import subprocess
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIX = os.path.join(ROOT, 'build', 'wix')
CANDLE = os.path.join(WIX, 'candle.exe')
LIGHT = os.path.join(WIX, 'light.exe')
APPDIR = os.path.join(ROOT, 'dist', '棕仙的传输软件')
ICON = os.path.join(ROOT, 'dist', 'zongxian.ico')
WXS = os.path.join(ROOT, 'build', 'installer-msi.wxs')
OUTDIR = os.path.join(ROOT, 'build', 'msi-out')
MSI = os.path.join(ROOT, 'dist', '棕仙的传输软件-安装包-1.0.msi')

APP_NAME = '棕仙的传输软件'
APP_EXE = APP_NAME + '.exe'
VERSION = '1.0.0'
UPGRADE_CODE = '{8F2E4C1A-6B3D-4E77-9C5A-2D1F0B7A4E31}'
NS = uuid.UUID('6d1f6c2e-9b3a-4a51-9d0e-6f2b1c8a7e55')

# 本机对工程目录之外的临时目录写入受限
_TMP = os.path.join(ROOT, 'tmp', 'buildtemp')
os.makedirs(_TMP, exist_ok=True)
os.environ['TEMP'] = _TMP
os.environ['TMP'] = _TMP


def guid_for(rel):
    return '{%s}' % str(uuid.uuid5(NS, rel)).upper()


def xml_escape(s):
    return (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
             .replace('"', '&quot;'))


def collect():
    """收集要打包的文件，并为每个文件生成 ASCII 合法的 Component/File Id。

    WiX 的标识符只允许 ASCII 字母数字下划线和点，所以这里用序号做 Id，
    GUID 仍然按相对路径稳定生成（同一文件每次构建得到同一个 GUID）。
    """
    out = []
    for dp, dn, fn in os.walk(APPDIR):
        for f in fn:
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, APPDIR).replace('\\', '/')
            out.append((rel, p, os.path.getsize(p)))
    out.sort()
    rows = []
    for i, (rel, path, size) in enumerate(out):
        rows.append({
            'rel': rel, 'path': path, 'size': size,
            'cid': 'c_%04d' % i, 'fid': 'f_%04d' % i, 'guid': guid_for(rel),
        })
    return rows


def write_wxs(files):
    lines = []
    a = lines.append
    a('<?xml version="1.0" encoding="utf-8"?>')
    a('<Wix xmlns="http://schemas.microsoft.com/wix/2006/wi">')
    a('  <Product Id="*" Name="%s" Language="2052" Version="%s" Manufacturer="棕仙"'
      ' UpgradeCode="%s">' % (xml_escape(APP_NAME), VERSION, UPGRADE_CODE))
    a('    <Package Id="*" InstallerVersion="500" Compressed="yes" InstallScope="perUser"'
      ' Platform="x64" Description="%s 安装程序" Comments="朋友间大文件直传与文件夹同步" />'
      % xml_escape(APP_NAME))
    a('    <MajorUpgrade DowngradeErrorMessage="已经安装了更新版本的 %s，请先卸载它。" />'
      % xml_escape(APP_NAME))
    a('    <MediaTemplate EmbedCab="yes" />')
    a('    <Property Id="ARPPRODUCTICON" Value="AppIcon" />')
    a('    <Property Id="ARPCOMMENTS" Value="免费、不限速、不要公网 IP 的朋友间大文件传输与文件夹同步" />')
    a('    <Property Id="ARPCONTACT" Value="棕仙" />')
    a('    <Property Id="ARPHELPLINK" Value="https://example.invalid/guide" />')
    a('    <Icon Id="AppIcon" SourceFile="%s" />' % xml_escape(ICON))
    a('    <Directory Id="TARGETDIR" Name="SourceDir">')
    a('      <Directory Id="LocalAppDataFolder">')
    a('        <Directory Id="ProgramsFolder" Name="Programs">')
    a('          <Directory Id="INSTALLFOLDER" Name="%s" />' % xml_escape(APP_NAME))
    a('        </Directory>')
    a('      </Directory>')
    a('      <Directory Id="ProgramMenuFolder">')
    a('        <Directory Id="AppMenuFolder" Name="%s" />' % xml_escape(APP_NAME))
    a('      </Directory>')
    a('      <Directory Id="DesktopFolder" />')
    a('    </Directory>')

    # 文件组件
    a('    <DirectoryRef Id="INSTALLFOLDER">')
    for row in files:
        a('      <Component Id="%s" Guid="%s" Win64="yes">' % (row['cid'], row['guid']))
        a('        <File Id="%s" Source="%s" KeyPath="yes" />'
          % (row['fid'], xml_escape(row['path'])))
        if row['rel'] == APP_EXE:
            a('        <Shortcut Id="scDesktop" Directory="DesktopFolder" Name="%s"'
              ' WorkingDirectory="INSTALLFOLDER" Icon="AppIcon" Advertise="no" />'
              % xml_escape(APP_NAME))
        a('      </Component>')
    a('    </DirectoryRef>')

    # 开始菜单组件 + 安装位置登记
    a('    <DirectoryRef Id="AppMenuFolder">')
    a('      <Component Id="c_startmenu" Guid="%s" Win64="yes">' % guid_for('__startmenu__'))
    a('        <Shortcut Id="scStart" Name="%s" Target="[INSTALLFOLDER]%s"'
      ' WorkingDirectory="INSTALLFOLDER" Icon="AppIcon" />'
      % (xml_escape(APP_NAME), xml_escape(APP_EXE)))
    a('        <Shortcut Id="scGuide" Name="使用说明" Target="[INSTALLFOLDER]zongxian-transfer-guide.md"'
      ' WorkingDirectory="INSTALLFOLDER" />')
    a('        <RemoveFolder Id="rmAppMenu" On="uninstall" />')
    a('        <RegistryValue Root="HKCU" Key="Software\\ZongxianTransfer" Name="InstallDir"'
      ' Type="string" Value="[INSTALLFOLDER]" KeyPath="yes" />')
    a('      </Component>')
    a('    </DirectoryRef>')

    a('    <Feature Id="MainFeature" Title="%s" Level="1">' % xml_escape(APP_NAME))
    for row in files:
        a('      <ComponentRef Id="%s" />' % row['cid'])
    a('      <ComponentRef Id="c_startmenu" />')
    a('    </Feature>')
    a('  </Product>')
    a('</Wix>')
    with open(WXS, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def run(cmd):
    print('$ ' + ' '.join(cmd))
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    if r.returncode != 0:
        print((r.stdout or '')[-3000:])
        print((r.stderr or '')[-3000:])
    return r.returncode


def build():
    if not os.path.exists(CANDLE):
        print('缺少 WiX 工具链：%s' % CANDLE)
        return None
    if not os.path.exists(os.path.join(APPDIR, APP_EXE)):
        print('缺少 %s，请先用 build/build-exe.py 打包桌面版' % os.path.join(APPDIR, APP_EXE))
        return None
    files = collect()
    print('打包 %d 个文件' % len(files))
    write_wxs(files)
    os.makedirs(OUTDIR, exist_ok=True)
    wixobj = os.path.join(OUTDIR, 'installer.wixobj')
    if run([CANDLE, '-nologo', '-arch', 'x64', '-out', wixobj, WXS]) != 0:
        return None
    if os.path.exists(MSI):
        os.remove(MSI)
    if run([LIGHT, '-nologo', '-sval', '-ext', os.path.join(WIX, 'WixUIExtension.dll'),
            '-cultures:zh-CN', '-out', MSI, wixobj]) != 0:
        return None
    if os.path.exists(MSI):
        print('完成：%s（%.1f MB）' % (MSI, os.path.getsize(MSI) / 1048576.0))
        return MSI
    return None


def main():
    msi = build()
    if not msi:
        return 1
    if '--test' in sys.argv:
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'build', 'test-msi.py'), msi],
                           cwd=ROOT)
        return r.returncode
    return 0


if __name__ == '__main__':
    sys.exit(main())
