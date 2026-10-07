"""build/no-js-error-check.py —— 守卫（运行时）：完整流程里不许出现 JS 未处理拒绝。

【为什么用运行时判据而不是静态扫描】
我先写了个"跨模块裸引用"静态检查，结果误报一大片 —— 各模块都从 `io` 解构依赖
（`const { post, log, state } = io;`），静态分析分不清"解构来的局部变量"和"裸引用"。
而这类 bug 的**真实症状**很明确：页面抛 ReferenceError ⇒ C# 记下 `[JS 未处理拒绝] xxx`。
所以直接跑一遍完整流程，断言日志里没有它 —— 可靠且零误报。

历史同类 bug（都是这样被抓到的）：
  · webrtc.js 引用 call.js 的 `remoteVideoEl` ⇒ 每次对方共享都中断 ontrack（画面接不上）
  · mesh.js  引用 call.js 的 `remoteAudioEl` ⇒ hangup 中断 ⇒ 两端状态卡在"通话中"
"""
import pathlib
import re
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(r"D:\文档\ai001")
EXE = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "ZongxianVoice.exe")
H, J = 48110, 48111
ROOM = "JS错误检查"
HL, JL = ROOT / "tmp" / "js-host.log", ROOT / "tmp" / "js-join.log"
CFG = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "app-config.json")
PAYLOAD = ROOT / "tmp" / "filetest-payload.bin"


def clean():
    for _ in range(6):
        subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
        time.sleep(1)
        n = subprocess.run(['powershell', '-NoProfile', '-Command',
                            "(Get-Process ZongxianVoice -ErrorAction SilentlyContinue).Count"],
                           capture_output=True, text=True).stdout.strip()
        if n == "0":
            break


clean()
try:
    if CFG.exists():
        CFG.unlink()
except OSError:
    pass
for f in (HL, JL):
    if f.exists():
        f.unlink()
PAYLOAD.write_bytes(b'ZXERRORCHECK' * 10000)

# 覆盖：通话 + 共享声音 + 文件 + 挂断（挂断走 UIA 点击，能触发 hangup 的清理路径）
print("host：建房保活 + 发起通话 + 共享声音 + 发文件…")
subprocess.run('start "" /b "%s" --port %d --name 房主 --create-room-test "%s" '
               '--create-room-keep --call-test --share-audio-test --file-test "%s" --log-file "%s"'
               % (EXE, H, ROOM, PAYLOAD, HL), shell=True)
time.sleep(18)
print("join：加入…")
subprocess.run('start "" /b "%s" --port %d --name 加入者 --room %s '
               '--signal ws://127.0.0.1:%d/signal --log-file "%s"'
               % (EXE, J, ROOM, H, JL), shell=True)
time.sleep(30)

# 用 UIA 点 join 的「挂断」—— 这是 remoteAudioEl 那条路径的入口
invoke = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$root = $AE::RootElement
$c = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, __PID__)
$c1 = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, 'HangupButton')
$el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
      (New-Object System.Windows.Automation.AndCondition($c1, $c)))
if ($el -ne $null -and $el.Current.IsEnabled) {
  $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
  Write-Output 'INVOKED'
} else { Write-Output 'SKIP' }
"""
out = subprocess.run(['powershell', '-NoProfile', '-Command',
                      "(Get-CimInstance Win32_Process -Filter \"Name='ZongxianVoice.exe'\" | "
                      "Where-Object { $_.CommandLine -like '*--port %d*' } | "
                      "ForEach-Object { $_.ProcessId })" % J],
                     capture_output=True, text=True).stdout
ids = [int(x) for x in out.split() if x.strip().isdigit()]
if ids:
    r = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                        '-Command', invoke.replace('__PID__', str(ids[0]))],
                       capture_output=True, text=True, encoding='utf-8', errors='replace').stdout.strip()
    print("点 join 的挂断:", r)
time.sleep(10)

bad = []
for log, tag in ((HL, "host"), (JL, "join")):
    t = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    for l in t.splitlines():
        if "JS 未处理拒绝" in l or "Unhandled" in l:
            bad.append("%s: %s" % (tag, l.strip()[:120]))

if bad:
    print("\n发现 JS 未处理拒绝（%d 条）：" % len(bad))
    for b in bad[:8]:
        print("  ! " + b)
else:
    print("\n没有 JS 未处理拒绝")
print("---- " + ("FAIL: 流程中有 JS 未处理拒绝（会中断功能）" if bad else "PASS: 无 JS 未处理拒绝"))
subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
sys.exit(1 if bad else 0)
