#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 src/web 下的分离文件打包成单文件 dist/swiftdrop.html（可直接发给朋友双击打开）。

产物文件名保持 ASCII（swiftdrop.html），但页面里显示的产品名是「棕仙的传输软件」。
"""
import io
import os
import re
import sys
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, 'src', 'web')
DIST = os.path.join(ROOT, 'dist')

ORDER = ['util.js', 'qr.js', 'signaling.js', 'rtc.js', 'relay.js', 'transfer.js', 'sync.js', 'app.js']
VERSION = '1.0'
APP_NAME = '棕仙的传输软件'


def read(p):
    with io.open(p, 'r', encoding='utf-8') as f:
        return f.read()


def icon_data_uri(size=64, fallback=None):
    """把指定尺寸的透明 PNG 图标内联成 data URI（单文件网页版必须自包含）。

    size=64  收藏夹/favicon 用（体积小）
    size=192 标题栏与首屏品牌图用（在高分屏上不糊）
    """
    import base64
    for name in ('app-transparent-%d.png' % size, fallback or ''):
        if not name:
            continue
        p = os.path.join(ROOT, 'dist', 'icons', name)
        if os.path.exists(p):
            with open(p, 'rb') as f:
                return 'data:image/png;base64,' + base64.b64encode(f.read()).decode('ascii')
    return ''


def main():
    html = read(os.path.join(WEB, 'index.html'))
    css = read(os.path.join(WEB, 'style.css'))

    parts = []
    missing = []
    for name in ORDER:
        p = os.path.join(WEB, name)
        if not os.path.exists(p):
            missing.append(name)
            continue
        code = read(p)
        code = code.replace('</script>', '<\\/script>')
        parts.append('/* ===== %s ===== */\n%s' % (name, code))

    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    scripts = ('<script>\nwindow.SD_VERSION = %r;\nwindow.SD_BUILD = %r;\nwindow.SD_APP_NAME = %r;\n%s\n</script>'
               % (VERSION, stamp, APP_NAME, '\n'.join(parts)))

    html = html.replace('<link rel="stylesheet" href="style.css">', '<style>\n' + css + '\n</style>')
    if '<!--SCRIPTS-->' not in html:
        print('ERROR: index.html 缺少 <!--SCRIPTS--> 标记')
        return 2
    html = html.replace('<!--SCRIPTS-->', scripts)

    icon_small = icon_data_uri(64)
    icon_big = icon_data_uri(192, fallback='app-transparent-256.png') or icon_small
    blank = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E"
    html = (html.replace('__APP_ICON_SMALL__', icon_small or blank)
                .replace('__APP_ICON__', icon_big or blank)
                .replace('__APP_NAME__', APP_NAME)
                .replace('__APP_VER__', VERSION)
                .replace('__APP_BUILD__', stamp))
    if not icon_small or not icon_big:
        print('提示：dist/icons/app-transparent-64.png 不存在，图标占位为空')

    if not os.path.isdir(DIST):
        os.makedirs(DIST)
    out = os.path.join(DIST, 'swiftdrop.html')
    with io.open(out, 'w', encoding='utf-8') as f:
        f.write(html)

    kb = os.path.getsize(out) / 1024.0
    print('已生成 %s  (%.1f KB)' % (out, kb))
    if missing:
        print('注意：以下文件不存在，已跳过 -> %s' % ', '.join(missing))
    # 粗检查：不能残留外链（data: 内联资源不算）
    probe = html.replace('wss://', '').replace('ws://', '').replace('turn:', '').replace('stun:', '')
    probe = re.sub(r'src="data:[^"]*"', '', probe)
    probe = re.sub(r'href="data:[^"]*"', '', probe)
    for bad in ['src="', 'href="style.css"', 'cdn.']:
        if bad in probe:
            print('警告：产物中仍出现 %r，可能不是自包含文件' % bad)
    return 0


if __name__ == '__main__':
    sys.exit(main())
