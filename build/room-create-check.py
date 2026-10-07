"""tmp/run-create-room-test.py —— 跑建房验收（走界面同一条 CreateRoomAsync）。"""
import pathlib
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
(ROOT / "tmp").mkdir(parents=True, exist_ok=True)   # 目录可能不存在（开源仓库不带 tmp/）
EXE = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "ZongxianVoice.exe")
LOG = ROOT / "tmp" / "create-room.log"
NAME = "自检建房" + time.strftime("%H%M%S")

subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
time.sleep(1.5)
if LOG.exists():
    LOG.unlink()
subprocess.run('start "" /b "%s" --port 47030 --create-room-test "%s" --log-file "%s"'
               % (EXE, NAME, LOG), shell=True)
for _ in range(45):
    time.sleep(1)
    out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq ZongxianVoice.exe'],
                         capture_output=True, text=True).stdout
    if out.count('ZongxianVoice') == 0:
        break
subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)

t = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
for l in t.splitlines():
    if "建房验收" in l or "已创建" in l or "房间名" in l:
        print("  " + l.strip()[:150])
ok = "[建房验收] 结果 PASS" in t
print("建房验收:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
