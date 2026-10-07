"""tmp/run-user-flow.py —— 按用户真实流程跑端到端。

流程：两边改名 → 房主删掉「我的房间」→ 房主新建房间 → 成员自动搜索加入。
"""
import pathlib
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
(ROOT / "tmp").mkdir(parents=True, exist_ok=True)   # 目录可能不存在（开源仓库不带 tmp/）
EXE = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "ZongxianVoice.exe")
H, J = 48030, 48031
ROOM = "开黑房"
HL, JL = ROOT / "tmp" / "flow-host.log", ROOT / "tmp" / "flow-join.log"


def clean():
    for _ in range(6):
        subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
        time.sleep(1)
        n = subprocess.run(['powershell', '-NoProfile', '-Command',
                            "(Get-Process ZongxianVoice -ErrorAction SilentlyContinue).Count"],
                           capture_output=True, text=True).stdout.strip()
        if n == "0":
            break
    cfg = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
           / "net8.0-windows10.0.19041.0" / "win-x64" / "app-config.json")
    try:
        if cfg.exists():
            cfg.unlink()
    except OSError:
        pass


clean()
for f in (HL, JL):
    if f.exists():
        f.unlink()

print("① 房主：改名 → 删房间 → 建「%s」→ 保活" % ROOM)
subprocess.run('start "" /b "%s" --port %d --name 房主 --user-flow-host "%s" --log-file "%s"'
               % (EXE, H, ROOM, HL), shell=True)
time.sleep(22)

print("② 成员：改名 → 等扫描 → 一键加入")
subprocess.run('start "" /b "%s" --port %d --name 加入者 --user-flow-join "%s" --create-room-keep --log-file "%s"'
               % (EXE, J, ROOM, JL), shell=True)
time.sleep(35)
subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
time.sleep(1)

for f, tag in ((HL, "房主"), (JL, "加入方")):
    t = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
    print("\n=== %s ===" % tag)
    for l in t.splitlines():
        if "[流程]" in l or "[成员]" in l or "信令连接加入" in l or "一键加入" in l \
           or "局域网] 发现" in l or "收到消息" in l:
            print("   " + l.strip()[:135])

ht = HL.read_text(encoding="utf-8", errors="replace") if HL.exists() else ""
jt = JL.read_text(encoding="utf-8", errors="replace") if JL.exists() else ""
host_ok = "[流程] 结果 PASS" in ht
join_ok = "[流程] 结果 PASS" in jt
print("\n房主流程:", "PASS" if host_ok else "FAIL")
print("加入方流程:", "PASS" if join_ok else "FAIL")
sys.exit(0 if (host_ok and join_ok) else 1)
