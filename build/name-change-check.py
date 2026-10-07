"""tmp/name-sync-e2e.py —— 双实例验证"改名后对方能看到新名字"。

host（建房保活）→ join 加入 → host 执行 --set-name-test 改名 → 看 join 侧收到的名字。
注意：--set-name-test 走 OnCloseSettings 同一段代码。
但 host 已经在跑，无法再传开关 —— 所以改成：
  host 直接用 --set-name-test 启动（它会改名+重连，然后退出）
不行，需要双实例同时在线。
改为：host 用 --create-room-keep 保活；join 加入；然后**再起一个 host 的改名进程**？
也不对 —— 改名必须发生在持连接的那个进程里。

结论：这一条必须用**界面操作**（打开设置→改名→关闭）或新的"运行中触发改名"通道。
本脚本先给出可自动化的那部分：单进程改名 + 存盘 + 重连日志证据。
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
LOG = ROOT / "tmp" / "name-test.log"

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
print("启动（--set-name-test 走设置关闭同一段代码）…")
subprocess.run('start "" /b "%s" --port 47850 --name 旧名字 --set-name-test "新名字甲" '
               '--log-file "%s"' % (EXE, LOG), shell=True)
for _ in range(30):
    time.sleep(1)
    out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq ZongxianVoice.exe'],
                         capture_output=True, text=True).stdout
    if out.count('ZongxianVoice') == 0:
        break
subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)

t = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
print("\n=== 名字相关日志 ===")
for l in t.splitlines():
    if any(k in l for k in ("名字验收", "设置]", "信令连接加入", "重连")):
        print("   " + l.strip()[:140])
ok = "[名字验收] 结果 PASS" in t
print("\n名字改名验收:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
