"""tmp/run-rename-delete-test.py —— 跑改名/删除验收（走界面同一段代码）。"""
import pathlib
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(r"D:\文档\ai001")
EXE = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "ZongxianVoice.exe")


def run(flag, logname, port, extra=""):
    log = ROOT / "tmp" / logname
    if log.exists():
        log.unlink()
    subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
    time.sleep(1.5)
    cmd = 'start "" /b "%s" --port %d --name 验收 %s "%s" --log-file "%s"' % (
        EXE, port, flag, extra, log)
    if flag == "--delete-room-test":
        cmd = 'start "" /b "%s" --port %d --name 验收 --delete-room-test --log-file "%s"' % (
            EXE, port, log)
    subprocess.run(cmd, shell=True)
    for _ in range(30):
        time.sleep(1)
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq ZongxianVoice.exe'],
                             capture_output=True, text=True).stdout
        if out.count('ZongxianVoice') == 0:
            break
    subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
    t = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    return [l.strip() for l in t.splitlines() if "验收" in l]


print("=== 改名验收 ===")
for l in run("--rename-room-test", "rename-test.log", 47900, "新房间名"):
    print("   " + l[:160])
print("\n=== 删除验收 ===")
for l in run("--delete-room-test", "delete-test.log", 47901):
    print("   " + l[:160])
