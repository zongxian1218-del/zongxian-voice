"""tmp/run-rename-delete-test.py —— 跑改名/删除验收（走界面同一段代码）。"""
import pathlib
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
(ROOT / "tmp").mkdir(parents=True, exist_ok=True)   # 目录可能不存在（开源仓库不带 tmp/）
EXE = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "ZongxianVoice.exe")


def run(flag, logname, port, extra=""):
    log = ROOT / "tmp" / logname
    if log.exists():
        log.unlink()
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
