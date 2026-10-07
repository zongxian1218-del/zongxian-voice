"""tmp/verify-auto-reconnect.py —— 验证信令被动断开后会**自动重连**（而不是永远显示"断开"）。

流程：host 建房保活 + join 加入 → 杀掉 host → join 的信令应被动断开
      → 日志里必须出现"将在 N 秒后自动重连"（说明自动重连被触发）。
"""
import pathlib
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(r"D:\文档\ai001")
EXE = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "ZongxianVoice.exe")
H, J = 48020, 48021
ROOM = "重连测试"
JL = ROOT / "tmp" / "recon-join.log"
HL = ROOT / "tmp" / "recon-host.log"


def clean():
    for _ in range(6):
        subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
        time.sleep(1)
        n = subprocess.run(['powershell', '-NoProfile', '-Command',
                            "(Get-Process ZongxianVoice -ErrorAction SilentlyContinue).Count"],
                           capture_output=True, text=True).stdout.strip()
        if n == "0":
            return


def pid_for(port):
    out = subprocess.run(['powershell', '-NoProfile', '-Command',
                          "(Get-CimInstance Win32_Process -Filter \"Name='ZongxianVoice.exe'\" | "
                          "Where-Object { $_.CommandLine -like '*--port %d*' } | "
                          "ForEach-Object { $_.ProcessId })" % port],
                         capture_output=True, text=True).stdout
    ids = [int(x) for x in out.split() if x.strip().isdigit()]
    return ids[0] if ids else 0


clean()
for f in (JL, HL):
    if f.exists():
        f.unlink()

print("启动 host（建房保活）…")
subprocess.run('start "" /b "%s" --port %d --name 房主 --create-room-test "%s" '
               '--create-room-keep --log-file "%s"' % (EXE, H, ROOM, HL), shell=True)
time.sleep(16)
print("启动 join…")
subprocess.run('start "" /b "%s" --port %d --name 加入者 --room %s '
               '--signal ws://127.0.0.1:%d/signal --log-file "%s"'
               % (EXE, J, ROOM, H, JL), shell=True)
time.sleep(15)

print("杀掉 host（模拟服务端消失）…")
hp = pid_for(H)
if hp:
    subprocess.run(['taskkill', '/F', '/PID', str(hp)], capture_output=True)
    print("  host pid=%d 已结束" % hp)
time.sleep(12)

t = JL.read_text(encoding="utf-8", errors="replace") if JL.exists() else ""
print("\n=== join 侧信令日志 ===")
for l in t.splitlines():
    if "信令" in l or "重连" in l:
        print("   " + l.strip()[:130])

ok = "正在自动重连" in t
print("\n---- " + ("PASS: 断开后自动重连已触发" if ok else "FAIL: 没有自动重连"))
subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
sys.exit(0 if ok else 1)
