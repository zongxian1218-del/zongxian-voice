"""build/input-clickable-check.py —— 判据：双实例在线时，输入控件必须真的可用。

判据来源：用户报"文字无法输入（对话框点不动）"、"文件传输点了没反应"。
  · `MessageInput.IsEnabled = _chatReady`（只在聊天通道打开时为 true）
  · 我上一轮的"文字端到端"用 --chat-test **绕过输入框** ⇒ PASS 但用户打不了字
本脚本量**界面控件状态**（UIA 的 IsEnabled / IsKeyboardFocusable），这才是用户能感知的。
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
H, J = 47890, 47891
ROOM = "可交互测试"
HL, JL = ROOT / "tmp" / "il-host.log", ROOT / "tmp" / "il-join.log"

Q = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$root = $AE::RootElement
$c = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, __PID__)
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $c)
foreach ($w in $wins) { Write-Output ("窗口: '" + $w.Current.Name + "'") }
foreach ($id in @('MessageInput','AttachFileButton','SendMessageButton','ShareScreenButton')) {
  $c1 = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $id)
  $el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.AndCondition($c1, $c)))
  if ($el -eq $null) { Write-Output ("  " + $id + ": NOT_IN_TREE"); continue }
  Write-Output ("  " + $id + ": enabled=" + $el.Current.IsEnabled +
                " focusable=" + $el.Current.IsKeyboardFocusable +
                " offscreen=" + $el.Current.IsOffscreen)
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
            return


def pid_for(port):
    out = subprocess.run(['powershell', '-NoProfile', '-Command',
                          "(Get-CimInstance Win32_Process -Filter \"Name='ZongxianVoice.exe'\" | "
                          "Where-Object { $_.CommandLine -like '*--port %d*' } | "
                          "ForEach-Object { $_.ProcessId })" % port],
                         capture_output=True, text=True).stdout
    ids = [int(x) for x in out.split() if x.strip().isdigit()]
    return ids[0] if ids else 0


clean()
for f in (HL, JL):
    if f.exists():
        f.unlink()

subprocess.run('start "" /b "%s" --port %d --name 房主 --create-room-test "%s" '
               '--create-room-keep --log-file "%s"' % (EXE, H, ROOM, HL), shell=True)
time.sleep(16)
subprocess.run('start "" /b "%s" --port %d --name 加入者 --room %s '
               '--signal ws://127.0.0.1:%d/signal --log-file "%s"'
               % (EXE, J, ROOM, H, JL), shell=True)
time.sleep(20)

ok_all = True
for port, tag in ((H, "host"), (J, "join")):
    pid = pid_for(port)
    print("=== %s（pid=%d）===" % (tag, pid))
    if not pid:
        print("  ! 找不到进程")
        ok_all = False
        continue
    out = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                          '-Command', Q.replace('__PID__', str(pid))],
                         capture_output=True, text=True, encoding='utf-8', errors='replace').stdout
    for line in out.splitlines():
        print("  " + line.strip())
    mi = [l for l in out.splitlines() if "MessageInput" in l]
    sb = [l for l in out.splitlines() if "SendMessageButton" in l]
    if not mi or "enabled=True" not in mi[0]:
        print("  ! %s 的输入框不可用（用户打不了字）" % tag)
        ok_all = False
    if not sb or "enabled=True" not in sb[0]:
        print("  ! %s 的发送按钮不可用" % tag)
        ok_all = False

subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
print("---- " + ("PASS: 输入框可用" if ok_all else "FAIL: 输入框不可用"))
sys.exit(0 if ok_all else 1)
