"""把 dist/winui 打成语音测试版发布包。

为什么要有这个脚本（而不是手工右键压缩）：
  · v22 是用资源管理器压出来的，结果把 exe 同级攒下的 **37 个测试日志 .txt**
    和 **3 个麦克风自检录音 .wav** 一起打了进去 —— 用户解包会看到一堆日志和我的录音。
  · 排除规则应该写在代码里、可复核，而不是靠打包的人记得。
  · 顺手做完整性自检：缺 resources.pri / BUILD-INFO.txt / media/call.html 直接报错。

用法:
    python build/make-voice-package.py --version 23
    python build/make-voice-package.py --version 23 --src dist/winui --dry-run
"""

import argparse
import pathlib
import sys
import os
import zipfile

# 【只认发版入口】build\release.py 会设置 ZX_RELEASE_ENTRY=1。
# 为什么：手工打包正是"守卫 FAIL 却照样出包"的根源（2026-10-06 审计实证：
# v36 就是带着陈旧页面与测试残留发出去的）。要打包，请走 python build\release.py --version NN
if os.environ.get("ZX_RELEASE_ENTRY") != "1":
    print("[拒绝] 不允许手工打包；请用 python build\\release.py --version NN")
    raise SystemExit(2)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
# 【残留清单只有一份】审计 §7.2：以前 cmd/守卫/打包脚本/文档各有一份定义，互不相同，
# 于是 v36 的 received/zx-filetest.bin 谁都没拦住。现在统一从 residue-rules.py 取。
from importlib import import_module as _imp
residue = _imp("residue-rules")

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

# 必须存在于包内的关键文件（相对 dist/winui）
REQUIRED = [
    "ZongxianVoice.exe",
    "resources.pri",
    "BUILD-INFO.txt",
    "ZongxianVoice.dll",
    "media/call.html",
]


def should_skip(rel: pathlib.Path) -> str | None:
    """兼容旧签名：实际规则在 build\\residue-rules.py（唯一来源）。"""
    return residue.skip_reason(rel)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True, help="版本号，例如 23 → 同频-测试版-v23.zip")
    ap.add_argument("--src", default="dist/winui")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = (ROOT / args.src).resolve()
    if not src.is_dir():
        print(f"[ERROR] 源目录不存在: {src}")
        return 2

    out = DIST / f"同频-测试版-v{args.version}.zip"

    # 先做完整性自检：关键文件不在就别打包（打出来也是坏的）
    missing = [f for f in REQUIRED if not (src / f).is_file()]
    if missing:
        print(f"[ERROR] 源目录缺关键文件，拒绝打包: {', '.join(missing)}")
        print("        （resources.pri 由 build\\build-winui-cs.cmd 生成）")
        return 2

    files = sorted(p for p in src.rglob("*") if p.is_file())
    packed, skipped = [], []
    for p in files:
        rel = p.relative_to(src)
        why = should_skip(rel)
        (skipped if why else packed).append((rel, why))

    print(f"源目录: {src}")
    print(f"  文件总数 {len(files)}  →  打包 {len(packed)}，排除 {len(skipped)}")
    by_reason: dict[str, int] = {}
    for _, why in skipped:
        by_reason[why] = by_reason.get(why, 0) + 1
    for why, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        print(f"    排除 {n:4d} 项：{why}")

    if args.dry_run:
        print("（dry-run：没有写 zip）")
        return 0

    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for rel, _ in packed:
            z.write(src / rel, rel.as_posix())
            total += (src / rel).stat().st_size
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"已写出: {out.name}（{size_mb:.1f} MB，原始 {total/1024/1024:.1f} MB）")

    # 打完之后回读校验：关键文件确实在包内，且没有任何被排除类型混进去
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        bad = [n for n in names if should_skip(pathlib.Path(n))]
        miss = [f for f in REQUIRED if f not in names]
    print(f"  回读校验: 条目 {len(names)}，关键文件缺 {len(miss)}，混入不该有的 {len(bad)}")
    if miss or bad:
        print(f"  [ERROR] 缺={miss} 混入={bad[:5]}")
        return 1
    # 注意：别在这里用 ✅/❌ 这类字符 —— Windows 控制台是 GBK(936)，
    # 打不出来会直接抛 UnicodeEncodeError，把"其实成功"的打包判成失败（实测踩过）。
    print("  [OK] 包内容自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
