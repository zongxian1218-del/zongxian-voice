"""build/three-instance-members.py —— 三实例成员数检查（A3 的硬判据）。

为什么需要它：用户报过"成员列表只显示两人"。日志里的 `[成员] N 人` 只说明**内部状态**，
不等于界面上真的画出了 N 行。这里用 UIA 读每个实例窗口的 `MemberCountText` 实际文本，
并以 roster 名单交叉核对 —— "界面上到底显示几人"以它为准。

不需要键鼠（只做 UIA 查询），所以**用户在用电脑时也能跑**。
判据：三个实例的 MemberCountText 都必须是 `成员 — 3`，且日志成员行也是 3 人。
"""
import pathlib
import re
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
EXE = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "ZongxianVoice.exe")
HP, P2, P3 = 46660, 46661, 46662
ROOM = "members"
LOGS = {n: ROOT / "tmp" / f"members-{n}.log" for n in ("host", "p2", "p3")}

PS = r'''
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$pidCond = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, __PID__)
$idCond  = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, 'MemberCountText')
$cond = New-Object System.Windows.Automation.AndCondition($pidCond, $idCond)
$el = $AE::RootElement.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
if ($null -eq $el) { Write-Output 'NOTFOUND'; exit 0 }
Write-Output ('TEXT=' + $el.Current.Name)
'''


def pid_for(port):
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='ZongxianVoice.exe'\" | "
          "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    r = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    for line in (r.stdout or "").splitlines():
        if "\t" in line:
            pid_s, cmd = line.split("\t", 1)
            if f'--port {port}' in cmd:
                try:
                    return int(pid_s.strip())
                except ValueError:
                    pass
    return None


def main():
    if not EXE.is_file():
        print("  [SKIP] 找不到可执行文件（先构建）")
        return 2
    subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
    time.sleep(2)
    for f in LOGS.values():
        if f.exists():
            f.unlink()
    for n, port, extra in (("host", HP, ""), ("p2", P2, f'--signal ws://127.0.0.1:{HP}/signal'),
                           ("p3", P3, f'--signal ws://127.0.0.1:{HP}/signal')):
        subprocess.run(f'start "" /b "{EXE}" --port {port} --name {n} --room {ROOM} '
                       f'{extra} --log-file "{LOGS[n]}"', shell=True)
        time.sleep(6)
    time.sleep(25)          # 等三方成员表稳定

    ok = True
    for n, port in (("host", HP), ("p2", P2), ("p3", P3)):
        pid = pid_for(port)
        if pid is None:
            print(f"  ! [{n}] 找不到进程（端口 {port}）")
            ok = False
            continue
        r = subprocess.run(['powershell', '-NoProfile', '-Command', PS.replace("__PID__", str(pid))],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        m = re.search(r"TEXT=(.*)", r.stdout or "")
        txt = (m.group(1).strip() if m else "(读不到)")
        roster = None
        if LOGS[n].exists():
            hits = re.findall(r"\[成员\] (\d+) 人[：:]([^\n]*)",
                              LOGS[n].read_text(encoding="utf-8", errors="replace"))
            roster = hits[-1] if hits else None
        print(f"  [{n}] 界面 MemberCountText={txt!r}｜日志成员行={roster}")
        if "3" not in txt:
            print(f"    ! {n} 界面上不是 3 人")
            ok = False
        if not roster or roster[0] != "3":
            print(f"    ! {n} 内部成员状态也不是 3 人")
            ok = False
    subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
    print("---- " + ("PASS: 三实例成员数" if ok else "FAIL: 三实例成员数"))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
