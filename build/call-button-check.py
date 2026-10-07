"""tmp/verify-call-button.py —— 判据：进了房间但还没通话时，「发起通话」按钮**必须可点**。

【为什么必须有】用户报"语音功能不能用"，截图显示：状态已经是"通话中"、而"发起通话"按钮是灰的。
根因：A1 的常驻「共享电脑声音」轨在链路建立时就挂着，`reportCallState` 只看"有没有音频轨"
⇒ 一进房间就判成"通话中" ⇒ 按钮被禁用 ⇒ **用户根本点不了通话**。
本判据：只进房间、不发起通话 ⇒ 按钮 enabled=True。
"""
import pathlib
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(r"D:\文档\ai001")
EXE = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "ZongxianVoice.exe")
H, J = 48080, 48081
ROOM = "按钮可用性"
HL, JL = ROOT / "tmp" / "cb-host.log", ROOT / "tmp" / "cb-join.log"
CFG = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "app-config.json")

Q = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$root = $AE::RootElement
$c = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, __PID__)
foreach ($id in @('CallButton','HangupButton','MuteButton','ShareScreenButton')) {
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

print("启动 host（建房保活，**不**发起通话）…")
subprocess.run('start "" /b "%s" --port %d --name 房主 --create-room-test "%s" '
               '--create-room-keep --log-file "%s"' % (EXE, H, ROOM, HL), shell=True)
time.sleep(17)
print("启动 join（加入，**不**发起通话）…")
subprocess.run('start "" /b "%s" --port %d --name 加入者 --room %s '
               '--signal ws://127.0.0.1:%d/signal --log-file "%s"'
               % (EXE, J, ROOM, H, JL), shell=True)
time.sleep(20)

ok_all = True
for port, tag, log in ((H, "host", HL), (J, "join", JL)):
    pid = pid_for(port)
    print("\n=== %s 按钮状态（UIA）===" % tag)
    if pid:
        out = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                              '-Command', Q.replace('__PID__', str(pid))],
                             capture_output=True, text=True, encoding='utf-8', errors='replace').stdout
        print(out.strip())
        call_line = [l for l in out.splitlines() if l.startswith('CallButton')]
        if not call_line or 'enabled=True' not in call_line[0]:
            print("  ! 发起通话按钮不可点（用户点不了通话）")
            ok_all = False
        t = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        judging = [l.strip() for l in t.splitlines() if "通话判定" in l]
        for l in judging[-2:]:
            print("   " + l[:120])
        if any("通话中=True" in l for l in judging[-2:]):
            print("  ! 还没通话却判成『通话中』")
            ok_all = False

# （『未加入不播放』的门控已回退：它误伤 A1 共享电脑声音 —— 共享轨与对端麦克风
#   走同一条 ontrack 音频通路，无法区分；故这里不再断言。）
print("\n---- " + ("PASS: 未通话时「发起通话」可点" if ok_all else "FAIL: 按钮被禁用或误判通话中"))
subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
sys.exit(0 if ok_all else 1)
