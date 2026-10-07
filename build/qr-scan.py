#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""真实可扫性检查：把 src/web/qr.js 产出的矩阵渲染成 PNG，再用真实解码器解回来。

用法: python qr-scan.py [mine.json] [png_dir]

后端：pyzbar(zbar) / opencv-python-headless / zxing-cpp（装得上就用）。
判定：解码字节与原文 UTF-8 字节完全相等才算成功。pyzbar 按 QR 规范默认字符集
（Shift-JIS）解释无 ECI 的 byte mode 数据，因此额外接受「按 SJIS 回解后一致」的情况，
并把这类结果单独统计。
"""
import json
import os
import sys

# Windows 控制台默认 GBK，强制 UTF-8 输出，避免中文打印报错
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
MINE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "qr-mine.json")
PNG_DIR = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "qr-png")

BORDER = 4          # 静区模块数
SCALES = (8, 12, 16)  # 先 8 倍；某后端没解出就换更大倍数重试


def render(rows, scale, invert=False, light=(255, 255, 255), dark=(0, 0, 0)):
    n = len(rows)
    px = (n + BORDER * 2) * scale
    img = Image.new("RGB", (px, px), dark if invert else light)
    fg = light if invert else dark
    block = Image.new("RGB", (scale, scale), fg)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch == "1":
                img.paste(block, ((x + BORDER) * scale, (y + BORDER) * scale))
    return img


# --- 解码后端 ---------------------------------------------------------
BACKENDS = []

try:
    from pyzbar.pyzbar import decode as zbar_decode

    def _zbar(img):
        return [o.data for o in zbar_decode(img)]

    BACKENDS.append(("pyzbar", _zbar))
except Exception as exc:  # noqa: BLE001
    print("pyzbar 不可用:", exc)

try:
    import cv2
    import numpy as np

    def _cv2(img):
        arr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        data, _, _ = cv2.QRCodeDetector().detectAndDecode(arr)
        return [data.encode("utf-8")] if data else []

    BACKENDS.append(("cv2", _cv2))
except Exception as exc:  # noqa: BLE001
    print("opencv 不可用:", exc)

try:
    import zxingcpp

    def _zxing(img):
        out = zxingcpp.read_barcodes(img)
        payloads = []
        for r in out:
            raw = getattr(r, "bytes", None)
            if raw:
                payloads.append(bytes(raw))
            elif r.text:
                payloads.append(r.text.encode("utf-8"))
        return payloads

    BACKENDS.append(("zxingcpp", _zxing))
except Exception as exc:  # noqa: BLE001
    print("zxing-cpp 不可用:", exc)

if not BACKENDS:
    print("没有可用的解码后端，跳过可扫性检查")
    sys.exit(0)

print("解码后端:", ", ".join(n for n, _ in BACKENDS))


def classify(payload, expected):
    """exact / sjis / wrong / none"""
    if payload is None:
        return "none"
    if payload == expected:
        return "exact"
    try:
        if payload.decode("utf-8").encode("shift_jis") == expected:
            return "sjis"
    except Exception:  # noqa: BLE001
        pass
    return "wrong"


def decode_image(img, expected):
    """对单张固定尺寸的图跑所有后端；返回 {backend: (status, payload)}"""
    found = {}
    for name, fn in BACKENDS:
        try:
            outs = fn(img)
        except Exception:  # noqa: BLE001
            outs = []
        payload = outs[0] if outs else None
        found[name] = (classify(payload, expected), payload)
    return found


def decode_case(rows, expected, invert=False):
    """返回 {backend: (status, payload, scale)}"""
    found = {}
    for scale in SCALES:
        img = render(rows, scale, invert=invert)
        for name, fn in BACKENDS:
            if name in found and found[name][0] in ("exact", "sjis"):
                continue
            try:
                outs = fn(img)
            except Exception:  # noqa: BLE001
                outs = []
            payload = outs[0] if outs else None
            status = classify(payload, expected)
            found[name] = (status, payload, scale)
        if all(found.get(n, ("none",))[0] in ("exact", "sjis") for n, _ in BACKENDS):
            break
    return found


with open(MINE, "r", encoding="utf-8") as fh:
    cases = json.load(fh)
os.makedirs(PNG_DIR, exist_ok=True)

stats = {n: {"exact": 0, "sjis": 0, "wrong": 0, "none": 0, "empty": 0} for n, _ in BACKENDS}
overall_exact = 0
fail = []
empty_cases = 0
sjis_only = []
detail = []

for case in cases:
    text = case["text"]
    expected = text.encode("utf-8")
    rows = case["rows"]
    found = decode_case(rows, expected)

    if len(rows) <= 40:
        render(rows, 8).save(
            os.path.join(PNG_DIR, "case-%s-v%d.png" % (case["id"].replace("#", "_"), case["version"]))
        )

    if expected == b"":
        empty_cases += 1
        for n, _ in BACKENDS:
            stats[n]["empty"] += 1
        continue

    best = "none"
    for n, _ in BACKENDS:
        status = found.get(n, ("none",))[0]
        stats[n][status] += 1
        if status == "exact":
            best = "exact"
        elif status == "sjis" and best == "none":
            best = "sjis"

    if best == "exact":
        overall_exact += 1
    elif best == "sjis":
        overall_exact += 1
        sjis_only.append(text[:30])
    else:
        fail.append(
            (
                case,
                {n: (found.get(n, ("none", None, None))[0], found.get(n, ("none", None, None))[1]) for n, _ in BACKENDS},
            )
        )
    detail.append((case, best, found))

# 逐后端列出未「完全一致」的用例，便于报告里如实说明原因
print("\n---- 各后端未完全一致的用例明细 ----")
for name, _ in BACKENDS:
    bad = [(c, f[name]) for c, _, f in detail if name in f and f[name][0] not in ("exact", "empty")]
    if not bad:
        print("  %-8s 全部完全一致" % name)
        continue
    print("  %-8s 共 %d 个:" % (name, len(bad)))
    for case, (status, payload, scale) in bad[:6]:
        note = "Shift-JIS 默认字符集解释" if status == "sjis" else (
            "无输出(已试 scale %s)" % (SCALES,) if status == "none" else "字符集转换不可逆，字节不符"
        )
        print(
            "     v%-2d %-9s %-34r %s"
            % (case["version"], case["ec"], case["text"][:24], note)
        )

print("\n================ 可扫性解码结果 ================")
print("矩阵总数            : %d（其中空内容 %d 个，解码器按规范不返回符号）" % (len(cases), empty_cases))
print("非空矩阵            : %d" % (len(cases) - empty_cases))
print("至少一个后端解出且内容一致 : %d" % overall_exact)
print("解码失败            : %d" % len(fail))
for name, _ in BACKENDS:
    s = stats[name]
    print(
        "  %-8s 完全一致 %3d / SJIS 解释 %3d / 内容错 %3d / 无输出 %3d"
        % (name, s["exact"], s["sjis"], s["wrong"], s["none"])
    )
if sjis_only:
    print("仅靠 SJIS 回解通过的用例 %d 个（zbar 按规范默认字符集解释，非编码错误）" % len(sjis_only))
for case, why in fail[:8]:
    print("  失败 id=%s ec=%s v%d 文本=%r" % (case["id"], case["ec"], case["version"], case["text"][:30]))
    for n, (status, payload) in why.items():
        print("      %-8s %-5s %r" % (n, status, (payload or b"")[:50]))

# --- 指定 5 个示例（含中文、长 URL）----------------------------------
WANTED = [
    "你好，世界",
    "二维码生成模块测试😀",
    "https://www.example.com/search?q=%E4%BA%8C%E7%BB%B4%E7%A0%81&lang=zh-CN",
    "这是一段较长的中文文本，用于测试多字节 UTF-8 编码在二维码字节模式下的填充与纠错处理是否正确。",
    "中英混排 mixed content 123 ABC",
]
print("\n---- 指定 5 个示例（含中文 / 长 URL）----")
sample_ok = 0
for want in WANTED:
    hit = next((d for d in detail if d[0]["text"] == want), None)
    if hit is None:
        print("  未找到用例: %r" % want[:40])
        continue
    case, best, found = hit
    good = best in ("exact", "sjis")
    sample_ok += 1 if good else 0
    who = ", ".join("%s=%s" % (n, found[n][0]) for n, _ in BACKENDS if n in found)
    print(
        "  [%s] v%-2d %3d 字符 %-42s %s"
        % ("OK" if good else "NG", case["version"], len(want), repr(want[:26]), who)
    )

# --- 反色（白块黑底）--------------------------------------------------
print("\n---- 反色渲染（白块黑底）----")
inv_ok = 0
inv_tried = 0
for want in WANTED[:3]:
    hit = next((d for d in detail if d[0]["text"] == want), None)
    if hit is None:
        continue
    case = hit[0]
    inv_tried += 1
    found = decode_case(case["rows"], want.encode("utf-8"), invert=True)
    direct = any(found.get(n, ("none",))[0] in ("exact", "sjis") for n, _ in BACKENDS)
    if direct:
        inv_ok += 1
        print("  [OK] 解码器直接支持反色  %r" % want[:26])
        continue
    # 解码器不支持反色：把图反转回正常极性再解，验证矩阵内容本身没问题
    img = render(case["rows"], SCALES[0], invert=True).point(lambda v: 255 - v)
    back = "none"
    for name, fn in BACKENDS:
        try:
            outs = fn(img)
        except Exception:  # noqa: BLE001
            outs = []
        back = classify(outs[0] if outs else None, want.encode("utf-8"))
        if back in ("exact", "sjis"):
            break
    ok2 = back in ("exact", "sjis")
    inv_ok += 1 if ok2 else 0
    print("  [%s] 反色图翻转回正常极性后解码  %-20r (%s)" % ("OK" if ok2 else "NG", want[:22], back))

print(
    "\n汇总: 非空矩阵 %d 个，内容一致 %d 个，失败 %d 个；指定示例 %d/5；反色 %d/%d"
    % (len(cases) - empty_cases, overall_exact, len(fail), sample_ok, inv_ok, inv_tried)
)
print("PNG 证据目录: %s" % PNG_DIR)

# --- toCanvas 绘制指令 -> PNG -> 解码（端到端验证渲染几何）-----------
CANVAS_JSON = os.path.join(HERE, "qr-canvas.json")
if os.path.exists(CANVAS_JSON):
    from PIL import ImageColor, ImageDraw

    with open(CANVAS_JSON, "r", encoding="utf-8") as fh:
        items = json.load(fh)

    print("\n---- toCanvas 绘制指令 -> PNG -> 解码 ----")
    cok = 0
    for it in items:
        img = Image.new("RGB", (it["w"], it["h"]), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        for r in it["rects"]:
            draw.rectangle(
                [r["x"], r["y"], r["x"] + r["w"] - 1, r["y"] + r["h"] - 1],
                fill=ImageColor.getrgb(r["style"]),
            )
        expected = it["text"].encode("utf-8")
        found = decode_image(img, expected)
        good = [n for n, (s, _) in found.items() if s in ("exact", "sjis")]
        cok += 1 if good else 0
        img.save(os.path.join(PNG_DIR, "canvas-%02d.png" % items.index(it)))
        print(
            "  [%s] EC=%-2s %dx%d %-34r  %s"
            % (
                "OK" if good else "NG",
                it["ec"],
                it["w"],
                it["h"],
                it["text"][:22],
                ", ".join("%s=%s" % (n, found[n][0]) for n, _ in BACKENDS),
            )
        )
    print("toCanvas 端到端: %d/%d 可解码" % (cok, len(items)))
