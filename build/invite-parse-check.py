"""tmp/run-invite-test.py —— 跑离线邀请串解析单测（不需要 GUI/前台）。"""
import pathlib
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
(ROOT / "tmp").mkdir(parents=True, exist_ok=True)   # 目录可能不存在（开源仓库不带 tmp/）
EXE = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "ZongxianVoice.exe")
LOG = ROOT / "tmp" / "invite-test.log"

subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
time.sleep(1)
if LOG.exists():
    LOG.unlink()
subprocess.run('start "" /b "%s" --port 46690 --parse-invite-test --log-file "%s"' % (EXE, LOG),
               shell=True)
for _ in range(40):
    time.sleep(1)
    out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq ZongxianVoice.exe'],
                         capture_output=True, text=True).stdout
    if out.count('ZongxianVoice') == 0:
        break
subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
t = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
lines = [l.strip() for l in t.splitlines() if '邀请解析' in l]
for l in lines:
    print("  " + l[:150])
if not lines:
    print("  (没有任何解析日志 —— 开关没生效？)")
ok = any("6/6" in l for l in lines)
print("邀请串解析单测:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
