"""residue-rules.py —— "什么算测试残留"的**唯一一份定义**（2026-10-06 立）。

【为什么必须收敛到一处】审计 §7.2 的原话：
  "'什么算残留'在四处各有一份定义（cmd / run-checks / 打包脚本 / dist-policy 文档），互不相同。"
后果是实测踩到的两件事：
  · v22：exe 同级 37 个引擎日志 .txt 与 3 个自检录音 .wav 进了发布包；
  · v36：`received/zx-filetest.bin`（184320 B）进了发布包 —— 因为**两份清单都不认识 received/**，
    换成别的目录名就复发。
所以：以后任何"要不要排除"的判断，只许 import 本文件，不许再抄一份常量。

用法（两个消费者）：
    build\\make-voice-package.py  →  skip_reason()   决定什么不进 zip
    build\\run-checks.py          →  dist_residue_reason()  决定什么让 dist 守卫 FAIL
"""
from __future__ import annotations

import pathlib

# 目录名：出现在路径的任何一层就排除（大小写不敏感）
EXCLUDE_DIRS = {
    ".webview2",     # WebView2 浏览器缓存（含 LevelDB 日志）
    "recordings",    # 录音自检产物
    "audio-probe-tmp",
    "obj",
    "bin",
    "received",      # 【2026-10-06 补】文件传输落盘目录：v36 就是这里漏出去的
    "sent",
    "incoming",
}

# 后缀：一律排除
EXCLUDE_SUFFIX = {
    ".log", ".wav", ".bmp", ".h264", ".tmp", ".obj", ".pdb", ".ilk", ".exp", ".lib",
}

# .txt：只允许打包溯源文件
ALLOW_TXT = {"BUILD-INFO.txt"}

# dist 里允许存在的 .webview2 位置（只有应用目录下这一处是"运行时配置"）
WEBVIEW2_RELATIVE_ALLOWED = ("winui",)


def _parts_lower(rel: pathlib.PurePath) -> list[str]:
    return [str(p).lower() for p in rel.parts]


def has_excluded_dir(rel: pathlib.PurePath, include_last: bool = True) -> bool:
    """路径里是否含排除目录。include_last=False 时忽略文件名自身。"""
    parts = _parts_lower(rel)
    if not include_last:
        parts = parts[:-1]
    return any(p in EXCLUDE_DIRS for p in parts)


def is_webview2_runtime(rel: pathlib.PurePath) -> bool:
    """dist\\winui\\.webview2\\** —— 这是 WebView2 自己的运行时目录，不算测试残留。

    （它的 LevelDB 里有 `000003.log`，那是浏览器的日志，不是我们的构建垃圾。）
    """
    parts = _parts_lower(rel)
    return len(parts) >= 2 and parts[0] in WEBVIEW2_RELATIVE_ALLOWED and parts[1] == ".webview2"


def skip_reason(rel: pathlib.PurePath) -> str | None:
    """打包判定：返回排除原因；None = 允许进包。"""
    # .webview2 先判：整个目录都不进包（与 dist_residue_reason 的差别正在这里）
    if has_excluded_dir(rel):
        return "排除目录"
    if rel.suffix.lower() in EXCLUDE_SUFFIX:
        return f"{rel.suffix} 后缀"
    if rel.suffix.lower() == ".txt" and rel.name not in ALLOW_TXT:
        return "测试残留 .txt"
    return None


def dist_residue_reason(rel: pathlib.PurePath) -> str | None:
    """dist 守卫判定：返回"这是残留"的原因；None = 干净。

    与 skip_reason 的区别只在 .webview2 的处理：包装时整个 .webview2 都不要，
    而 dist 里它是合法的运行时目录（所以先放行再判断）。
    """
    if is_webview2_runtime(rel):
        return None
    if has_excluded_dir(rel, include_last=False):
        return "测试残留目录（文件传输落盘/录音/中间产物）"
    if rel.suffix.lower() in EXCLUDE_SUFFIX:
        return "测试残留/中间产物"
    if rel.suffix.lower() == ".txt" and rel.name not in ALLOW_TXT:
        return "测试残留 .txt"
    return None
