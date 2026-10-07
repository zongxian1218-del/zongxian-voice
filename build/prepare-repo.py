import os
import re

from PIL import Image

ROOT = r"D:\文档\ai001"
DOCS = os.path.join(ROOT, "docs")
os.makedirs(DOCS, exist_ok=True)

# 1) 截图进 docs/：选不含私人内网 IP 的那几张
picks = [
    (r"tmp\ui\web-light-1280.png", "web-light.png"),
    (r"tmp\ui\web-connected-dark-1280.png", "web-dark.png"),
    (r"tmp\ui\desktop-main.png", "desktop.png"),
]
for src, dst in picks:
    p = os.path.join(ROOT, src)
    if not os.path.isfile(p):
        print("  缺图:", src)
        continue
    im = Image.open(p).convert("RGB")
    if im.width > 1400:
        h = int(im.height * 1400 / im.width)
        im = im.resize((1400, h), Image.LANCZOS)
    out = os.path.join(DOCS, dst)
    im.save(out, "PNG", optimize=True)
    print(f"  {dst}: {im.size} {os.path.getsize(out) / 1024:.0f} KB")

# 2) 测试脚本里的真实地址换成占位符（隐私）
subs = {
    r"26.10.20.30": "26.10.20.30",
    r"192.168.1.50": "192.168.1.50",
}
changed = []
for base in (os.path.join(ROOT, "build", "e2e"), os.path.join(ROOT, "build")):
    if not os.path.isdir(base):
        continue
    for dp, dn, fn in os.walk(base):
        if "node_modules" in dp or "downloads" in dp:
            continue
        for f in fn:
            if not f.endswith((".mjs", ".js", ".py", ".md", ".nsi")):
                continue
            fp = os.path.join(dp, f)
            try:
                t = open(fp, encoding="utf-8").read()
            except OSError:
                continue
            o = t
            for a, b in subs.items():
                t = t.replace(a, b)
            if t != o:
                open(fp, "w", encoding="utf-8").write(t)
                changed.append(os.path.relpath(fp, ROOT))
print("  已替换真实地址的文件:", changed or "无")

# 3) 确认仓库里不再有敏感串（字面量拆开写，避免本脚本自己命中/泄露）
bad = ["zongxian" + "123", "26.103" + ".67.21", "192.168" + ".1.104"]
hits = []
for dp, dn, fn in os.walk(ROOT):
    if any(x in dp for x in ("__pycache__", "node_modules", "tmp", "android-sdk", "jdk",
                             "qrvenv", "pack-venv", "net-venv", "downloads", ".dsh",
                             "swiftdrop-recv", "pyi-onedir", "wix", "innosetup")):
        continue
    for f in fn:
        fp = os.path.join(dp, f)
        if os.path.getsize(fp) > 4 * 1024 * 1024:
            continue
        try:
            t = open(fp, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for b in bad:
            if b in t:
                hits.append(f"{os.path.relpath(fp, ROOT)} ← {b}")
print("  仍需注意的敏感串:", hits or "无")
