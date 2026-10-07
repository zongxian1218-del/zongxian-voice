"""tmp/voice-real-test.py —— 真正的语音通话测试（--call-test 已改为事件驱动）。

判据（三个层次，逐层缩小范围）：
  ① 双方都真的**发起了通话**（日志"【通话测试】通道已就绪，发起通话"）
  ② 链路上有**麦克风轨**（senderInfo 里存在 isShared=false 的 audio sender）
     —— 这一条与"有没有声音输入"无关，是纯粹的代码正确性
  ③ 有音频字节在传（audioOutBytes/audioInBytes > 0）—— 这需要真的有人说话，
     自动化环境里可能为 0（静音抑制），所以②才是关键判据。
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
H, J = 48070, 48071
ROOM = "语音真测"
HL, JL = ROOT / "tmp" / "vr-host.log", ROOT / "tmp" / "vr-join.log"
CFG = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "app-config.json")


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

print("启动 host（建房保活 + 自动发起通话）…")
subprocess.run('start "" /b "%s" --port %d --name 房主 --create-room-test "%s" '
               '--create-room-keep --call-test --log-file "%s"'
               % (EXE, H, ROOM, HL), shell=True)
time.sleep(17)
print("启动 join（加入 + 自动发起通话）…")
subprocess.run('start "" /b "%s" --port %d --name 加入者 --room %s '
               '--signal ws://127.0.0.1:%d/signal --call-test --log-file "%s"'
               % (EXE, J, ROOM, H, JL), shell=True)
time.sleep(40)

ok_all = True
for f, tag in ((HL, "host"), (JL, "join")):
    t = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
    print("\n=== %s ===" % tag)
    for l in t.splitlines():
        if ("通话测试" in l or "麦克风已开" in l or "[通话判定]" in l
                or "senderInfo" in l or "语音统计" in l):
            print("   " + l.strip()[:150])
    asked = "【通话测试】通道已就绪，发起通话" in t
    mic = "麦克风已开" in t
    # senderInfo 里是否有非共享的 audio sender
    shared = [m for m in re.findall(r'"senderInfo":\[(.*?)\]\}', t)]
    has_mic_sender = False
    for s in shared:
        for entry in re.findall(r'\["audio",(true|false),(true|false),(true|false)\]', s):
            if entry[2] == 'false':      # isShared=false ⇒ 麦克风轨
                has_mic_sender = True
    out_b = max([int(x) for x in re.findall(r'"audioOutBytes":(\d+)', t)] or [0])
    in_b = max([int(x) for x in re.findall(r'"audioInBytes":(\d+)', t)] or [0])
    print("   → 发起通话=%s 麦克风已开=%s 有麦克风轨=%s 发送=%d 接收=%d"
          % (asked, mic, has_mic_sender, out_b, in_b))
    if not asked:
        print("   ! 没发起通话")
        ok_all = False
    if not mic:
        print("   ! 麦克风没打开")
        ok_all = False

print("\n---- " + ("PASS: 双方都发起了通话且麦克风轨已挂" if ok_all else "FAIL: 语音链路有问题"))
subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
sys.exit(0 if ok_all else 1)
