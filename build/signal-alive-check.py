"""tmp/verify-signal-alive.py —— 复现用户场景：房主建房后信令是否还活着。

用户截图：房主窗口显示"信令断开"，但左栏房间是"已连接"（状态自相矛盾）。
根因：房主建房时 join 会 close 旧 ws，旧 ws 的 onclose 在新连接后触发 ⇒ 误报"信令断开"。
判据：建房后**不许出现**"信令断开"；且必须出现"信令已连接"；加入方能收到 welcome。
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
H, J = 47920, 47921
ROOM = "信令存活测试"
HL, JL = ROOT / "tmp" / "sa-host.log", ROOT / "tmp" / "sa-join.log"


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

print("启动 host（建房保活）…")
subprocess.run('start "" /b "%s" --port %d --name 房主 --create-room-test "%s" '
               '--create-room-keep --log-file "%s"' % (EXE, H, ROOM, HL), shell=True)
time.sleep(24)
print("启动 join…")
subprocess.run('start "" /b "%s" --port %d --name 加入者 --room %s '
               '--signal ws://127.0.0.1:%d/signal --log-file "%s"'
               % (EXE, J, ROOM, H, JL), shell=True)
time.sleep(24)

t = HL.read_text(encoding="utf-8", errors="replace") if HL.exists() else ""
tj = JL.read_text(encoding="utf-8", errors="replace") if JL.exists() else ""

print("\n=== host 信令相关 ===")
for l in t.splitlines():
    if "信令" in l or "重连信令" in l:
        print("   " + l.strip()[:110])

disconnected = [l for l in t.splitlines() if "信令断开" in l]
connected = [l for l in t.splitlines() if "信令已连接" in l]
print("\nhost：信令已连接 %d 次 / 信令断开 %d 次" % (len(connected), len(disconnected)))
print("join：收到 welcome =", "welcome" in tj)

ok = (not disconnected) and len(connected) >= 1 and ("welcome" in tj)
print("---- " + ("PASS: 建房后信令保持连接" if ok else "FAIL: 仍有信令断开或加入方没连上"))
subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
sys.exit(0 if ok else 1)
