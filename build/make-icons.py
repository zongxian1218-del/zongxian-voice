#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从用户提供的原图生成全套图标资产。

产出（都在 dist/icons/ 下）：
  app-transparent-{64,128,256,512,1024}.png  去底透明版（网页头部/收藏夹图标用）
  app-square-{48,72,96,144,192,512}.png      保留原底方图（Android 启动图标用）
  synced-{16,32,48,64,128,256}.png           同步文件夹标记（原图 + 绿色同步角标）
  以及 dist/zongxian.ico / dist/zongxian-synced.ico / dist/zongxian-icon.png
用法：python build/make-icons.py <原图路径>
"""
import os
import sys
from collections import deque

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICONS = os.path.join(ROOT, 'dist', 'icons')
ICO = os.path.join(ROOT, 'dist', 'zongxian.ico')
ICO_SYNCED = os.path.join(ROOT, 'dist', 'zongxian-synced.ico')
PNG_MAIN = os.path.join(ROOT, 'dist', 'zongxian-icon.png')


def punch_background(img):
    """把四周的纯色背景抠成透明（从四角泛洪，容差由内到外自适应）。"""
    img = img.convert('RGBA')
    w, h = img.size
    px = img.load()

    # 以四角的平均色作为背景参考色
    corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
    base = tuple(sum(c[i] for c in corners) // 4 for i in range(3))

    def is_bg(c):
        # 背景是浅蓝渐变：亮度高、蓝色分量不低于红色
        d = abs(c[0] - base[0]) + abs(c[1] - base[1]) + abs(c[2] - base[2])
        return d < 150 and c[2] >= c[0] - 6 and c[1] >= c[0]

    seen = bytearray(w * h)
    dq = deque()
    for x in range(w):
        for y in (0, h - 1):
            if not seen[y * w + x] and is_bg(px[x, y][:3]):
                seen[y * w + x] = 1
                dq.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if not seen[y * w + x] and is_bg(px[x, y][:3]):
                seen[y * w + x] = 1
                dq.append((x, y))

    while dq:
        x, y = dq.popleft()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h:
                i = ny * w + nx
                if not seen[i] and is_bg(px[nx, ny][:3]):
                    seen[i] = 1
                    dq.append((nx, ny))

    alpha = Image.new('L', (w, h), 255)
    ap = alpha.load()
    for y in range(h):
        row = y * w
        for x in range(w):
            if seen[row + x]:
                ap[x, y] = 0
    # 边缘轻微羽化，缩小后不会出现硬锯齿
    alpha = alpha.filter(ImageFilter.GaussianBlur(1.2))
    img.putalpha(alpha)
    return img


def trim(img, pad_ratio=0.04):
    """裁掉四周透明空白，再留一点边距，让小尺寸图标也更饱满。"""
    bbox = img.getbbox()
    if not bbox:
        return img
    img = img.crop(bbox)
    w, h = img.size
    side = max(w, h)
    canvas = Image.new('RGBA', (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - w) // 2, (side - h) // 2), img)
    pad = int(side * pad_ratio)
    out = Image.new('RGBA', (side + pad * 2, side + pad * 2), (0, 0, 0, 0))
    out.paste(canvas, (pad, pad), canvas)
    return out


def square(img, size):
    """把原图裁成正方形（保留背景），用于 Android 启动图标。"""
    w, h = img.size
    side = min(w, h)
    img = img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
    return img.convert('RGBA').resize((size, size), Image.LANCZOS)


def rounded(img, radius_ratio=0.22):
    """给方图加圆角（Android 老式图标常见做法）。"""
    w, h = img.size
    mask = Image.new('L', (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=int(w * radius_ratio), fill=255)
    out = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def make_synced_badge(base, size):
    """右下角加一个绿色同步角标，用来标记"正在同步的文件夹"。"""
    img = base.copy().resize((size, size), Image.LANCZOS)
    d = ImageDraw.Draw(img)
    r = int(size * 0.42)
    cx, cy = size - r // 2 - int(size * 0.04), size - r // 2 - int(size * 0.04)
    # 白色描边 + 绿色圆底
    d.ellipse([cx - r // 2 - 2, cy - r // 2 - 2, cx + r // 2 + 2, cy + r // 2 + 2], fill=(255, 255, 255, 255))
    d.ellipse([cx - r // 2, cy - r // 2, cx + r // 2, cy + r // 2], fill=(16, 137, 62, 255))
    # 两个白色箭头（首尾相接的循环箭头）
    a = int(r * 0.30)
    lw = max(1, int(size * 0.035))
    d.arc([cx - a, cy - a, cx + a, cy + a], start=200, end=340, fill=(255, 255, 255, 255), width=lw)
    d.arc([cx - a, cy - a, cx + a, cy + a], start=20, end=160, fill=(255, 255, 255, 255), width=lw)
    t = int(a * 0.55)
    d.polygon([(cx + a - t, cy - a - lw), (cx + a + t, cy - a + lw // 2), (cx + a - t, cy - a + lw * 2)], fill=(255, 255, 255, 255))
    d.polygon([(cx - a + t, cy + a + lw), (cx - a - t, cy + a - lw // 2), (cx - a + t, cy + a - lw * 2)], fill=(255, 255, 255, 255))
    return img


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'build', 'icon-src.webp')
    if not os.path.exists(src):
        print('找不到原图：%s' % src)
        return 2
    if not os.path.isdir(ICONS):
        os.makedirs(ICONS)

    raw = Image.open(src)
    print('原图：%s %s' % (src, raw.size))

    cut = trim(punch_background(raw))
    print('去底后尺寸：%s' % (cut.size,))

    for s in (64, 128, 256, 512, 1024):
        cut.resize((s, s), Image.LANCZOS).save(os.path.join(ICONS, 'app-transparent-%d.png' % s))
    for s in (48, 72, 96, 144, 192, 512):
        rounded(square(raw, s)).save(os.path.join(ICONS, 'app-square-%d.png' % s))

    synced = {}
    for s in (16, 32, 48, 64, 128, 256):
        im = make_synced_badge(cut, s)
        synced[s] = im
        im.save(os.path.join(ICONS, 'synced-%d.png' % s))

    # Windows 图标（多尺寸）
    cut.resize((256, 256), Image.LANCZOS).save(
        ICO, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    synced[256].save(
        ICO_SYNCED, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    cut.resize((512, 512), Image.LANCZOS).save(PNG_MAIN)

    print('已生成：')
    for f in sorted(os.listdir(ICONS)):
        print('  icons/%s  %d 字节' % (f, os.path.getsize(os.path.join(ICONS, f))))
    for f in (ICO, ICO_SYNCED, PNG_MAIN):
        print('  %s  %d 字节' % (os.path.basename(f), os.path.getsize(f)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
