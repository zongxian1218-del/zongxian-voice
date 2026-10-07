#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成 dist/swiftdrop.ico（程序图标：蓝色渐变圆角方块 + 白色闪电）。"""
import os
import math
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'dist', 'swiftdrop.ico')
S = 512


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def make():
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    # 渐变底
    grad = Image.new('RGBA', (S, S))
    d = ImageDraw.Draw(grad)
    c1, c2 = (79, 140, 255), (124, 92, 255)
    for y in range(S):
        d.line([(0, y), (S, y)], fill=lerp(c1, c2, y / (S - 1)) + (255,))
    # 圆角遮罩
    mask = Image.new('L', (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.24), fill=255)
    img.paste(grad, (0, 0), mask)
    # 白色闪电
    d2 = ImageDraw.Draw(img)
    pts = [(0.585, 0.075), (0.245, 0.545), (0.455, 0.545), (0.385, 0.925), (0.755, 0.435), (0.525, 0.435)]
    d2.polygon([(x * S, y * S) for x, y in pts], fill=(255, 255, 255, 255))
    if not os.path.isdir(os.path.dirname(OUT)):
        os.makedirs(os.path.dirname(OUT))
    img.save(OUT, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    # 顺便存一张 png 给网页/文档用
    img.resize((256, 256), Image.LANCZOS).save(os.path.join(ROOT, 'dist', 'swiftdrop-icon.png'))
    print('已生成 ' + OUT)


if __name__ == '__main__':
    make()
