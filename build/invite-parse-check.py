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
