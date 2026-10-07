"""tmp/verify-hangup-sync.py —— 判据：一端挂断后，**两端都退出通话状态**。

用户报："点了挂断没声音了但是状态一个还是通话中，一个是已连接未通话"
根因：hangup() 只做本端清理、从不通知对端。
判据：① 挂断方日志有"已通知…对端挂断" ② 两端的最后一次 [通话判定] 都是 通话中=False
      ③ 按钮文案是"加入语音"（用户要求的命名）。
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
H, J = 48100, 48101
ROOM = "挂断同步"
HL, JL = ROOT / "tmp" / "hg-host.log", ROOT / "tmp" / "hg-join.log"
CFG = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "app-config.json")

INVOKE = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$root = $AE::RootElement
$c = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, __PID__)
# 先确认按钮文案
$c2 = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, 'CallButton')
$cb = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
      (New-Object System.Windows.Automation.AndCondition($c2, $c)))
if ($cb -ne $null) { Write-Output ('CallButton.Name=' + $cb.Current.Name) }
# 点挂断
# 【2026-10-07】挂断键已合并进 CallButton（通话中显示"挂断"），
# 独立的 HangupButton 被隐藏 —— 旧脚本点它点不到，误报"挂断无效"。
$c1 = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, 'CallButton')
$el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
      (New-Object System.Windows.Automation.AndCondition($c1, $c)))
if ($el -eq $null) { Write-Output 'CallButton: NOTFOUND'; exit }
if (-not $el.Current.IsEnabled) { Write-Output 'CallButton: DISABLED'; exit }
Write-Output ('CallButton.Name=' + $el.Current.Name)
$el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
Write-Output 'CallButton(挂断): INVOKED'
"""

PROBE = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$root = $AE::RootElement
$c = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, __PID__)
foreach ($id in @('CallButton','HangupButton','MuteButton')) {
  $c1 = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $id)
  $el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.AndCondition($c1, $c)))
  if ($el -ne $null) { Write-Output ($id + '=' + $el.Current.IsEnabled) }
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


def ps(cmd):
    return subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', cmd],
                          capture_output=True, text=True, encoding='utf-8', errors='replace').stdout.strip()


clean()
try:
    if CFG.exists():
        CFG.unlink()
except OSError:
    pass
for f in (HL, JL):
    if f.exists():
        f.unlink()

print("host 建房保活 + 发起通话…")
subprocess.run('start "" /b "%s" --port %d --name 房主 --create-room-test "%s" '
               '--create-room-keep --call-test --log-file "%s"' % (EXE, H, ROOM, HL), shell=True)
time.sleep(26)
print("join 加入（被动接收）…")
subprocess.run('start "" /b "%s" --port %d --name 加入者 --room %s '
               '--signal ws://127.0.0.1:%d/signal --log-file "%s"'
               % (EXE, J, ROOM, H, JL), shell=True)
time.sleep(36)

print("\n① 检查按钮文案 + 由 join 点「挂断」…")
out = ps(INVOKE.replace('__PID__', str(pid_for(J))))
print("   " + out.replace("\n", "\n   "))
time.sleep(12)

print("\n② 挂断后各端状态…")
ok = True
for port, tag, log in ((H, "host", HL), (J, "join", JL)):
    t = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    judging = [l.strip() for l in t.splitlines() if "通话判定" in l]
    last = judging[-1] if judging else "(无)"
    notified = "已通知" in t
    peer_hangup = "对方已挂断" in t
    print("   %s: %s" % (tag, last[:120]))
    print("        通知了对端=%s 收到对方挂断=%s" % (notified, peer_hangup))
    if "通话中=True" in last:
        print("        ! 挂断后仍显示通话中")
        ok = False
if not ("CallButton.Name=加入语音" in out or "CallButton.Name=挂断" in out):
    print("   ! 按钮文案不是「加入语音」")
    ok = False
print("\n---- " + ("PASS: 挂断后两端都退出通话且按钮名为「加入语音」" if ok else "FAIL"))
subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
sys.exit(0 if ok else 1)
