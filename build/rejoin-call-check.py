"""tmp/verify-rejoin-call.py —— 判据：挂断后再「加入语音」，必须重新变成通话中（发音频=True）。

用户实测（v74）："挂断一次再加入会变成仅收听"、"房主麦克风没声音只有成员有"。
根因：挂断 stop() 了麦克风轨，但降噪链里留着那条死轨 ⇒ addTrack 死轨 ⇒ 发不出去。
判据：① 合并键文案随状态切换（加入语音 ⇄ 挂断）
      ② 挂断后再加入 ⇒ 最后判定 发音频=True 且 通话中=True（而不是"仅收听"）
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
H, J = 48120, 48121
ROOM = "再次加入"
HL, JL = ROOT / "tmp" / "rj-host.log", ROOT / "tmp" / "rj-join.log"
CFG = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "app-config.json")

CLICK = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$root = $AE::RootElement
$c = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, __PID__)
$c1 = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, 'CallButton')
$el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
      (New-Object System.Windows.Automation.AndCondition($c1, $c)))
if ($el -eq $null) { Write-Output 'NOTFOUND'; exit }
Write-Output ('name=' + $el.Current.Name + ' enabled=' + $el.Current.IsEnabled)
if ($el.Current.IsEnabled) {
  $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
  Write-Output 'CLICKED'
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


def click(pid):
    return subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                           '-Command', CLICK.replace('__PID__', str(pid))],
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

print("host 建房保活 + 发起通话；join 加入…")
subprocess.run('start "" /b "%s" --port %d --name 房主 --create-room-test "%s" '
               '--create-room-keep --call-test --log-file "%s"' % (EXE, H, ROOM, HL), shell=True)
time.sleep(26)
subprocess.run('start "" /b "%s" --port %d --name 加入者 --room %s '
               '--signal ws://127.0.0.1:%d/signal --log-file "%s"'
               % (EXE, J, ROOM, H, JL), shell=True)
time.sleep(36)

print("\n① 通话中，host 的合并键应该是「挂断」：")
r1 = click(pid_for(H)) if False else subprocess.run(
    ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
     CLICK.replace('__PID__', str(pid_for(H))).replace('if ($el.Current.IsEnabled) {', 'if ($false) {')],
    capture_output=True, text=True, encoding='utf-8', errors='replace').stdout.strip()
print("   " + r1.replace("\n", "\n   "))

print("\n② 点一次（挂断）…")
print("   " + click(pid_for(H)).replace("\n", "\n   "))
time.sleep(8)

print("\n③ 再点一次（加入语音）…")
print("   " + click(pid_for(H)).replace("\n", "\n   "))
time.sleep(42)

hl = HL.read_text(encoding="utf-8", errors="replace") if HL.exists() else ""
jl = JL.read_text(encoding="utf-8", errors="replace") if JL.exists() else ""
hj = [l.strip() for l in hl.splitlines() if "通话判定" in l]
jj = [l.strip() for l in jl.splitlines() if "通话判定" in l]
print("\n④ host 最后 3 次判定：")
for l in hj[-3:]:
    print("   " + l[:140])
print("   join 最后一次：")
for l in jj[-1:]:
    print("   " + l[:140])

last = hj[-1] if hj else ""
ok = ("发音频=True" in last and "通话中=True" in last
      and "仅收听" not in hl.split("通话判定")[-1])
if "重建麦克风处理链" in hl:
    print("\n   （日志显示确实走了'重建麦克风处理链'这条修复路径）")
print("\n---- " + ("PASS: 挂断后再加入仍是通话中（不是仅收听）" if ok else "FAIL: 再加入后没能发音频"))
subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
sys.exit(0 if ok else 1)
