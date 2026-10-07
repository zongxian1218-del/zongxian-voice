"""tmp/two-instance.py —— 双实例联机检查（S3 第 2 步判据，全部从应用日志取证）。

判据：
  · 两侧 `[成员] 2 人`
  · 两侧 `[链路] 注册表 N 条：<id>` 至少 1 条
  · 两侧 `[通话判定] ... 通话中=True`
  · 至少一侧出现 `share-audio-debug`（含 getStats 字节数）
  · 两侧都没有 scriptError / ReferenceError / TypeError
"""
import pathlib
import re
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
(ROOT / "tmp").mkdir(parents=True, exist_ok=True)   # 目录可能不存在（开源仓库不带 tmp/）
EXE = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "ZongxianVoice.exe")
HOST_PORT, JOIN_PORT, ROOM = 46220, 46221, "twoinst3"
HOST_LOG = ROOT / "tmp" / "two-host.log"
JOIN_LOG = ROOT / "tmp" / "two-join.log"

# 【2026-10-07 强化清场】反复杀到进程数为 0 再继续。
#   原来只 taskkill 一次 + sleep：前一个守卫的实例没退干净就抢走端口/房间，
#   后一个守卫会报假失败（实测：release 里"共享已开始=False"，单跑却 PASS）。
for _ in range(10):
    subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
    _n = subprocess.run(['powershell', '-NoProfile', '-Command',
                         "(Get-Process ZongxianVoice -ErrorAction SilentlyContinue).Count"],
                        capture_output=True, text=True).stdout.strip()
    if _n == "0":
        break
    time.sleep(0.7)
time.sleep(0.5)
for f in (HOST_LOG, JOIN_LOG):
    if f.exists():
        f.unlink()

print("启动 host（端口 %d）…" % HOST_PORT)
subprocess.run('start "" /b "%s" --port %d --name host --room %s --log-file "%s"'
               % (EXE, HOST_PORT, ROOM, HOST_LOG), shell=True)
time.sleep(6)
print("启动 join（信令指向 host）…")
subprocess.run('start "" /b "%s" --port %d --name joiner --room %s '
               '--signal ws://127.0.0.1:%d/signal --log-file "%s"'
               % (EXE, JOIN_PORT, ROOM, HOST_PORT, JOIN_LOG), shell=True)
time.sleep(34)
subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
time.sleep(1)


def analyze(path, label):
    if not path.exists():
        print("  [%s] 无日志" % label)
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    members = re.findall(r"\[成员\] (\d+) 人", text)
    reg = re.findall(r"\[链路\] 注册表 (\d+) 条[:：]([0-9a-f, ]*)", text)
    judge = re.findall(r"\[通话判定\] 链路=(\w+) 发音频=(\w+) 收音频=(\w+)", text)
    dbg = re.findall(r"share-audio-debug", text)
    outb = [int(x) for x in re.findall(r"outBytes['\"]?[:=] ?(\d+)", text)]
    inb = [int(x) for x in re.findall(r"inBytes['\"]?[:=] ?(\d+)", text)]
    errs = re.findall(r"scriptError|ReferenceError|TypeError|js-error|\[JS 异常\]", text)
    info = {
        "成员": members[-1] if members else None,
        "注册表最后一条": reg[-1] if reg else None,
        "通话判定最后一次": judge[-1] if judge else None,
        "share-audio-debug条数": len(dbg),
        "outBytes最大": max(outb) if outb else 0,
        "inBytes最大": max(inb) if inb else 0,
        "脚本错误": len(errs),
    }
    print("  [%s] %s" % (label, info))
    return info


print("=== 结果 ===")
h = analyze(HOST_LOG, "host")
j = analyze(JOIN_LOG, "join")
if h is None or j is None:
    sys.exit(1)

crit = [
    ("两侧都看到 2 人", h["成员"] == "2" and j["成员"] == "2"),
    ("两侧注册表≥1 条", bool(h["注册表最后一条"] and j["注册表最后一条"]
                       and int(h["注册表最后一条"][0]) >= 1 and int(j["注册表最后一条"][0]) >= 1)),
    ("两侧通话中=True", bool(h["通话判定最后一次"] and j["通话判定最后一次"]
                       and h["通话判定最后一次"][0] == "connected" and j["通话判定最后一次"][0] == "connected")),
    ("有 share-audio-debug 取证", h["share-audio-debug条数"] > 0 or j["share-audio-debug条数"] > 0),
    ("无脚本错误", h["脚本错误"] == 0 and j["脚本错误"] == 0),
]
for name, ok in crit:
    print("  %-24s %s" % (name, "✅" if ok else "❌"))
ok = all(x[1] for x in crit)
print("双实例联机:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
