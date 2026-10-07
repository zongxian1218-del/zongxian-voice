"""tmp/verify-mute-cycle.py —— 判据：静音→取消静音后，必须恢复发送音频。

用户实测（v75）："双方点静音后再恢复只有房主有声音成员听不到"
根因：setMuted 只改 localStream 的轨，而实际 addTrack 的是**降噪链的输出轨** ⇒ 改错了对象。
判据：通话中 发音频=True → 静音后 发音频=False → 取消静音后 发音频=True。
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
H, J = 48130, 48131
ROOM = "静音往返"
HL, JL = ROOT / "tmp" / "mu-host.log", ROOT / "tmp" / "mu-join.log"
CFG = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "app-config.json")

CLICK = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$root = $AE::RootElement
$c = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, __PID__)
$c1 = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, '__ID__')
$el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
      (New-Object System.Windows.Automation.AndCondition($c1, $c)))
if ($el -eq $null) { Write-Output 'NOTFOUND'; exit }
if (-not $el.Current.IsEnabled) { Write-Output 'DISABLED'; exit }
if ($el.Current.Name) { Write-Output ('name=' + $el.Current.Name) }
$el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
Write-Output 'CLICKED'
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


def click(port, ctrl):
    pid = pid_for(port)
    if not pid:
        return "(no pid)"
    return subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                           '-Command', CLICK.replace('__PID__', str(pid)).replace('__ID__', ctrl)],
                          capture_output=True, text=True, encoding='utf-8', errors='replace').stdout.strip()


def last_sending(log):
    t = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    js = [l.strip() for l in t.splitlines() if "通话判定" in l]
    if not js:
        return "(无判定)"
    last = js[-1]
    return last


clean()
try:
    if CFG.exists():
        CFG.unlink()
except OSError:
    pass
for f in (HL, JL):
    if f.exists():
        f.unlink()

print("host 建房保活+发起通话；join 加入…")
subprocess.run('start "" /b "%s" --port %d --name 房主 --create-room-test "%s" '
               '--create-room-keep --call-test --log-file "%s"' % (EXE, H, ROOM, HL), shell=True)
time.sleep(17)
subprocess.run('start "" /b "%s" --port %d --name 加入者 --room %s '
               '--signal ws://127.0.0.1:%d/signal --log-file "%s"'
               % (EXE, J, ROOM, H, JL), shell=True)
time.sleep(25)

before = last_sending(JL)
print("\n① 通话中，join 的发送状态：", before[:110])
print("② 点 join 的「静音」：", click(J, 'MuteButton'))
time.sleep(6)
muted_state = last_sending(JL)
print("   静音后：", muted_state[:110])
print("③ 再点一次「静音」（取消静音）：", click(J, 'MuteButton'))
time.sleep(10)
after = last_sending(JL)
print("   取消静音后：", after[:110])

t = JL.read_text(encoding="utf-8", errors="replace") if JL.exists() else ""
mute_logs = [l.strip() for l in t.splitlines() if "静音=" in l]
print("\n静音日志：")
for l in mute_logs[-3:]:
    print("   " + l[:120])

# 【收紧判据】静音必须真的让 sending 变 False，取消后必须回到 True。
# 只断言"最后是 True"是不够的 —— 静音压根没生效时最后也是 True（等于没验）。
ok_mute = "发音频=False" in muted_state
ok_unmute = "发音频=True" in after
ok = "发音频=True" in before and ok_mute and ok_unmute
print("\n判据：通话中发=True(%s) → 静音后发=False(%s) → 取消后发=True(%s)"
      % ("发音频=True" in before, ok_mute, ok_unmute))
print("\n---- " + ("PASS: 静音与取消静音都正确生效" if ok else "FAIL: 静音往返不正确"))
subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
sys.exit(0 if ok else 1)
