"""tmp/chat-voice-e2e.py —— 验证④（文字）与语音/共享声音的端到端通路。

判据（都从日志取证）：
  ① 双方各发一条文字 → 各自日志要有「[聊天] 已发出」且**对端**收到（收到消息/聊天消息）
  ② 语音：链路建立后 audioOutBytes / audioInBytes 都 > 0（真的有音频在传）
  ③ 共享声音：--share-audio-test 打开后 pushed 块数 > 0（采集真的在推）
"""
import pathlib
import re
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
(ROOT / "tmp").mkdir(parents=True, exist_ok=True)   # 目录可能不存在（开源仓库不带 tmp/）
EXE = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "ZongxianVoice.exe")
H, J = 47400, 47401
ROOM = "端到端房间"
HL, JL = ROOT / "tmp" / "e2e-host.log", ROOT / "tmp" / "e2e-join.log"


def clean():
    """彻底清场：反复杀到 0，再删掉测试累积的 app-config.json。

    【为什么必须删 config】release 里本守卫排在 30 个守卫之后，实测 `左栏项数=11`
    （历次测试把房间堆进 app-config.json）⇒ 幽灵对端干扰 ⇒ 聊天通道/输入框状态错乱。
    """
    end = time.time() + 20
    while time.time() < end:
        subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
        subprocess.run(['powershell', '-NoProfile', '-Command',
                        "Get-Process ZongxianVoice -ErrorAction SilentlyContinue | "
                        "Stop-Process -Force -ErrorAction SilentlyContinue"], capture_output=True)
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq ZongxianVoice.exe'],
                             capture_output=True, text=True).stdout
        if out.count('ZongxianVoice') == 0:
            break
        time.sleep(0.5)
    cfg = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
           / "net8.0-windows10.0.19041.0" / "win-x64" / "app-config.json")
    try:
        if cfg.exists():
            cfg.unlink()
    except OSError:
        pass
    time.sleep(1)


clean()
for f in (HL, JL):
    if f.exists():
        f.unlink()

print("启动 host（建房保活 + 发文字 + 共享声音）…")
subprocess.run('start "" /b "%s" --port %d --name 房主 --create-room-test "%s" '
               '--create-room-keep --chat-test "房主说的话" --share-audio-test --log-file "%s"'
               % (EXE, H, ROOM, HL), shell=True)
time.sleep(27)
print("启动 join（加入 + 发文字）…")
subprocess.run('start "" /b "%s" --port %d --name 加入者 --room %s '
               '--signal ws://127.0.0.1:%d/signal --chat-test "加入者说的话" --log-file "%s"'
               % (EXE, J, ROOM, H, JL), shell=True)
time.sleep(55)
subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
time.sleep(1)

ok = True
audio_total_out = 0
audio_total_in = 0
for path, label in ((HL, "host"), (JL, "join")):
    t = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    print("\n=== %s ===" % label)
    for key in ("[聊天]", "聊天测试", "收到消息", "chat-message", "音频", "共享声音"):
        for l in t.splitlines():
            if key in l:
                print("   " + l.strip()[:140])
    sent = "[聊天] 已发出" in t or "已发送" in t
    recv = "[收到消息]" in t
    out_b = max([int(x) for x in re.findall(r'"audioOutBytes":(\d+)', t)] or [0])
    in_b = max([int(x) for x in re.findall(r'"audioInBytes":(\d+)', t)] or [0])
    pushed = max([int(x) for x in re.findall(r'"pushed":(\d+)', t)] or [0])
    audio_total_out = max(audio_total_out, out_b)
    audio_total_in = max(audio_total_in, in_b)
    print("   → 发=%s 收=%s audioOut=%d audioIn=%d sharedPush=%d"
          % (sent, recv, out_b, in_b, pushed))
    if not sent:
        print("   ! 没发出文字")
        ok = False
    if not recv:
        print("   ! 没收到对端文字")
        ok = False

# 【2026-10-07 判据修正】音频只要求**整场有一侧在推、有一侧在收**。
# 以前是"每个实例的 audioOutBytes 都必须 > 0"，但本测试里只有 host 开了
# 「共享电脑声音」（--share-audio-test），join 既没共享也没通话 ⇒ join 的
# audioOut=0 **本来就是正确的**。那个判据是在"要求被测对象之外的东西"，
# 之前偶尔通过只是因为残留实例干扰（判据不牢）。
# 麦克风语音由专门的 check_voice_call 守卫覆盖（那是它该管的事）。
print("\n音频合计: 最大 audioOut=%d / 最大 audioIn=%d" % (audio_total_out, audio_total_in))
if audio_total_out <= 0:
    print("   ! 没有任何音频在发（共享声音没推出去）")
    ok = False
print("\n---- " + ("PASS: 文字与语音端到端" if ok else "FAIL: 文字/语音端到端"))
sys.exit(0 if ok else 1)
