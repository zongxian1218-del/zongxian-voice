"""tmp/verify-filecard-name.py —— 验证用户新报的两条：
  ① 传文件卡片应从"发送中…"变成"已发送"（对端"已接收"）
  ② 左下角名字（SelfNameText）应跟随改名

做法：
  host：--set-name-test 改名 + --file-test 发一个文件 + 保活
  join：加入接收
  然后：① 用 UIA 读 host 的 SelfNameText 文本；② 看两侧日志的文件完成记录。
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
H, J = 48040, 48041
ROOM = "文件与名字"
HL, JL = ROOT / "tmp" / "fn-host.log", ROOT / "tmp" / "fn-join.log"
PAYLOAD = ROOT / "tmp" / "filetest-payload.bin"

Q = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$root = $AE::RootElement
$c = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, __PID__)
foreach ($id in @('SelfNameText','SelfAvatarText')) {
  $c1 = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $id)
  $el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.AndCondition($c1, $c)))
  if ($el -eq $null) { Write-Output ("  " + $id + ": NOT_IN_TREE"); continue }
  Write-Output ("  " + $id + " = '" + $el.Current.Name + "'")
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
# 【必须清 config】否则 --room 会被 config 里遗留的"当前房间"覆盖（实测：命令行给"文件与名字"，
# 应用仍用上次保存的"开黑房"），两边房间名不一致就永远看不到对方。
CFG = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "app-config.json")
try:
    if CFG.exists():
        CFG.unlink()
except OSError:
    pass
for f in (HL, JL):
    if f.exists():
        f.unlink()
# 造一个 ~200KB 的测试文件
PAYLOAD.write_bytes(b'ZXFILETEST' * 20000)

print("启动 host（改名 + 发文件 + 保活）…")
subprocess.run('start "" /b "%s" --port %d --name 房主 --set-name-test "文件测试名" '
               '--create-room-keep --room %s --file-test "%s" --log-file "%s"'
               % (EXE, H, ROOM, PAYLOAD, HL), shell=True)
time.sleep(24)
print("启动 join（加入接收）…")
subprocess.run('start "" /b "%s" --port %d --name 加入者 --room %s '
               '--signal ws://127.0.0.1:%d/signal --log-file "%s"'
               % (EXE, J, ROOM, H, JL), shell=True)
time.sleep(36)

print("\n=== host 左下角名字（UIA）===")
hp = pid_for(H)
if hp:
    out = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                          '-Command', Q.replace('__PID__', str(hp))],
                         capture_output=True, text=True, encoding='utf-8', errors='replace').stdout
    print(out.strip() or "（没读到）")

for f, tag in ((HL, "host"), (JL, "join")):
    t = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
    print("\n=== %s 文件相关 ===" % tag)
    for l in t.splitlines():
        if "文件" in l or "已接收" in l:
            print("   " + l.strip()[:130])

ht = HL.read_text(encoding="utf-8", errors="replace") if HL.exists() else ""
jt = JL.read_text(encoding="utf-8", errors="replace") if JL.exists() else ""
# 端到端证据：发送方"文件发送完成" + 接收方"文件已保存"
file_ok = ("文件发送完成" in ht) and ("文件已保存" in jt)
name_ok = "文件测试名" in (out if hp else "")
print("\n文件卡片状态更新:", "PASS" if file_ok else "FAIL")
print("左下角名字跟随改名:", "PASS" if name_ok else "FAIL")
subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
sys.exit(0 if (file_ok and name_ok) else 1)
