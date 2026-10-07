"""tmp/test-one-side-call.py —— 复现用户场景：**只有一端点「发起通话」**，另一端被动接收。

用户现象：一端"通话中"，另一端一直"建立连接…"，且挂断/静音点不动。
怀疑：被动方的 `state.inCall` 没置位 ⇒ active=false ⇒ 文案卡在"建立连接…"、
      按钮也被 UpdateActionButtons 禁用。
判据：被动方必须也进入"通话中"（_inCall=true、按钮可用）。
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
H, J = 48090, 48091
ROOM = "单向发起"
HL, JL = ROOT / "tmp" / "oc-host.log", ROOT / "tmp" / "oc-join.log"
CFG = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "app-config.json")

Q = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$root = $AE::RootElement
$c = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, __PID__)
foreach ($id in @('CallButton','HangupButton','MuteButton')) {
  $c1 = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $id)
  $el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.AndCondition($c1, $c)))
  if ($el -eq $null) { Write-Output ($id + ': NOT_IN_TREE'); continue }
  Write-Output ($id + ': enabled=' + $el.Current.IsEnabled)
}
"""


def clean():
    for _ in range(6):
        subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
        time.sleep(1)
        n = subprocess.run(['powershell', '-NoProfile', '-Command',
                            "(Get-Process ZongxianVoice -ErrorAction SilentlyContinue).Count"],
                           capture_output=True, text=True).stdout.strip()
        if n == "0":
            break


def pid_for(port):
    out = subprocess.run(['powershell', '-NoProfile', '-Command',
                          "(Get-CimInstance Win32_Process -Filter \"Name='ZongxianVoice.exe'\" | "
                          "Where-Object { $_.CommandLine -like '*--port %d*' } | "
                          "ForEach-Object { $_.ProcessId })" % port],
                         capture_output=True, text=True).stdout
    ids = [int(x) for x in out.split() if x.strip().isdigit()]
    return ids[0] if ids else 0


clean()
try:
    if CFG.exists():
        CFG.unlink()
except OSError:
    pass
for f in (HL, JL):
    if f.exists():
        f.unlink()

print("host：只建房保活（**不**发起通话）…")
subprocess.run('start "" /b "%s" --port %d --name 房主 --create-room-test "%s" '
               '--create-room-keep --log-file "%s"' % (EXE, H, ROOM, HL), shell=True)
time.sleep(26)
print("join：加入 + **发起通话**（只有这一端发起）…")
subprocess.run('start "" /b "%s" --port %d --name 加入者 --room %s '
               '--signal ws://127.0.0.1:%d/signal --call-test --log-file "%s"'
               % (EXE, J, ROOM, H, JL), shell=True)
time.sleep(42)

ok_all = True
for port, tag, log, role in ((H, "host", HL, "被动方（应进入通话中）"),
                             (J, "join", JL, "主动方")):
    pid = pid_for(port)
    print("\n=== %s（%s）===" % (tag, role))
    if pid:
        out = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                              '-Command', Q.replace('__PID__', str(pid))],
                             capture_output=True, text=True, encoding='utf-8', errors='replace').stdout
        print(out.strip())
    t = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    judging = [l.strip() for l in t.splitlines() if "通话判定" in l]
    for l in judging[-3:]:
        print("   " + l[:135])
    in_call = any("通话中=True" in l for l in judging[-3:])
    print("   → 最后判定：通话中=%s" % in_call)
    if tag == "host" and not in_call:
        print("   ! 被动方没进入通话中（用户现象：一直显示'建立连接…'、按钮点不动）")
        ok_all = False

print("\n---- " + ("PASS: 单端发起也能双向进入通话" if ok_all else "FAIL: 被动方没进入通话（复现用户问题）"))
subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
sys.exit(0 if ok_all else 1)
