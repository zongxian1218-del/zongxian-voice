"""源码快照 —— 本机没装 git，用它当回滚点。

用法:
    python build/snapshot.py [备注]

做什么:
    · 把 src/ 、docs/ 、build/ 脚本、顶层文档 打进
      build/snapshots/source-YYYY-MM-DD-HHMM[-备注].zip
    · 只保留最近 8 个快照（更旧的自动删除）
    · 排除构建产物、依赖、虚拟环境、大二进制、快照自身与 dist 隔离区

为什么不用 git:
    这台机器上没有任何 git（where git 找不到，常见安装位置也扫过）。
    装 git 需要联网与系统改动，所以先用这个脚本兜底；
    真正的版本控制建议: winget install --id Git.Git -e

恢复方式:
    这里存的是**源码与文档**，不是完整工作树。恢复某个文件:
        tar -xf build/snapshots/source-<日期>.zip <相对路径>
    或直接把 zip 解到临时目录里比对。
"""
import datetime
import os
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "build" / "snapshots"
KEEP = 8

INCLUDE_DIRS = ["src", "docs", "build"]
INCLUDE_TOP = ["README.md", "CONTRIBUTING.md", "LICENSE", ".gitignore"]

# 目录名级排除（任何层级）
EXCLUDE_DIR_NAMES = {
    "bin", "obj", ".webview2", "__pycache__", "node_modules",
    "net-venv", "qrvenv", "pack-venv", "wix", "nsis", "innosetup",
    "jdk", "android-sdk", "downloads", "snapshots", "shots",
}
# 路径级排除（相对仓库根）
EXCLUDE_PATHS = {
    "src/media/probe/build",   # 探针构建产物与 .h264/.bmp 存证图
}
# 扩展名级排除（构建产物 / 大二进制）
EXCLUDE_EXT = {
    ".exe", ".dll", ".lib", ".obj", ".pdb", ".ilk", ".exp",
    ".h264", ".bmp", ".wav", ".zip", ".7z", ".msi", ".apk",
    ".log", ".pyc", ".pma",
}
# 这些扩展名在 docs/ 下要保留（是验证证据，不是垃圾）
KEEP_EXT_IN_DOCS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

MAX_FILE_BYTES = 8 * 1024 * 1024


def iter_files():
    for rel_dir in INCLUDE_DIRS:
        base = ROOT / rel_dir
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            here = pathlib.Path(dirpath)
            rel_here = here.relative_to(ROOT).as_posix()
            # 就地裁剪要遍历的目录
            dirnames[:] = [
                d for d in dirnames
                if d not in EXCLUDE_DIR_NAMES
                and f"{rel_here}/{d}" not in EXCLUDE_PATHS
                and not d.startswith("dist-quarantine-")
            ]
            for name in filenames:
                p = here / name
                rel = p.relative_to(ROOT).as_posix()
                if rel in EXCLUDE_PATHS:
                    continue
                ext = p.suffix.lower()
                in_docs = rel.startswith("docs/")
                if ext in EXCLUDE_EXT and not (in_docs and ext in KEEP_EXT_IN_DOCS):
                    continue
                try:
                    if p.stat().st_size > MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                yield p, rel
    for name in INCLUDE_TOP:
        p = ROOT / name
        if p.is_file():
            yield p, name


def main():
    note = sys.argv[1] if len(sys.argv) > 1 else ""
    note = "".join(ch for ch in note if ch.isalnum() or ch in "-_")[:24]
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"source-{stamp}{('-' + note) if note else ''}.zip"

    count = 0
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path, rel in iter_files():
            z.write(path, rel)
            count += 1
            total += path.stat().st_size

    print(f"snapshot: {out}")
    print(f"  files={count}  raw={total/1024/1024:.2f} MB  zip={out.stat().st_size/1024/1024:.2f} MB")

    # 只留最近 KEEP 个
    snaps = sorted(OUT_DIR.glob("source-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in snaps[KEEP:]:
        old.unlink()
        print(f"  pruned: {old.name}")
    print(f"  kept {min(len(snaps), KEEP)} snapshot(s) in {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
