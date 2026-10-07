"""tmp/verify-file-open-entry.py —— 验证收到文件后，界面上真的出现"打开"入口。

判据（UIA）：join 侧收到文件后，必须能找到「打开文件」/「打开所在文件夹」按钮。
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
H, J = 48050, 48051
ROOM = "打开入口测试"
HL, JL = ROOT / "tmp" / "fo-host.log", ROOT / "tmp" / "fo-join.log"
PAYLOAD = ROOT / "tmp" / "filetest-payload.bin"
CFG = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "app-config.json")

# 查：界面上有没有"打开"按钮（按名字找）
Q = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$root = $AE::RootElement
$c = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, __PID__)
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $c)
$hits = @()
foreach ($w in $wins) {
  $all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
      [System.Windows.Automation.Condition]::TrueCondition)
  foreach ($e in $all) {
    $n = $e.Current.Name
    if ($n -eq '打开文件' -or $n -eq '打开所在文件夹') { $hits += $n }
  }
}
if ($hits.Count -eq 0) { Write-Output 'NONE' } else { Write-Output ($hits -join ',') }
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
PAYLOAD.write_bytes(b'ZXFILETEST' * 20000)

print("启动 host（建房保活 + 发文件）…")
subprocess.run('start "" /b "%s" --port %d --name 房主 --create-room-test "%s" '
               '--create-room-keep --file-test "%s" --log-file "%s"'
               % (EXE, H, ROOM, PAYLOAD, HL), shell=True)
time.sleep(26)
print("启动 join（加入接收）…")
subprocess.run('start "" /b "%s" --port %d --name 加入者 --room %s '
               '--signal ws://127.0.0.1:%d/signal --log-file "%s"'
               % (EXE, J, ROOM, H, JL), shell=True)
time.sleep(32)

jp = pid_for(J)
print("\n=== join 界面上的打开入口（UIA）===")
found = "NONE"
if jp:
    found = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                            '-Command', Q.replace('__PID__', str(jp))],
                           capture_output=True, text=True, encoding='utf-8', errors='replace').stdout.strip()
    print("   " + found)

jt = JL.read_text(encoding="utf-8", errors="replace") if JL.exists() else ""
recv = "文件已保存" in jt
ok = recv and ("打开" in found)
print("\n对端已保存:", recv)
print("界面有打开入口:", found)
print("---- " + ("PASS: 收到文件后有打开入口" if ok else "FAIL: 没有打开入口"))
subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
sys.exit(0 if ok else 1)
