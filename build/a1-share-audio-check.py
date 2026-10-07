"""A1 判据守卫：共享电脑声音端到端（双实例 + 已知音频 → 两侧都有收发字节）。

为什么必须用"已知音频"：默认播放设备可能是虚拟声卡，回环采到的是**数字静音**，
那时 RTP 为 0 是"没声音可发"，不是 bug —— 会让判据假红。
所以测试期间用 PlaySoundW 循环播一段已知 WAV（自带非零峰值），
并先用 worklet 的 nodeRms/destRms 证明"音轨里确实有声音"，再看 RTP 字节。
"""
import ctypes
import pathlib
import re
import struct
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
(ROOT / "tmp").mkdir(parents=True, exist_ok=True)   # 目录可能不存在（开源仓库不带 tmp/）
EXE = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
       / "net8.0-windows10.0.19041.0" / "win-x64" / "ZongxianVoice.exe")
H, J = 46500, 46501
HL, JL = ROOT / "tmp" / "a1g-host.log", ROOT / "tmp" / "a1g-join.log"
WAV = next((p for p in (ROOT / "build").rglob("*.wav")
            if p.is_file() and p.stat().st_size > 100000), None)


def wav_peak(p):
    d = p.read_bytes()
    i = d.find(b"data")
    if i < 0:
        return 0.0
    peak = 0.0
    for k in range(min((len(d) - i - 8) // 2, 96000)):
        v = abs(struct.unpack_from("<h", d, i + 8 + k * 2)[0]) / 32768.0
        peak = max(peak, v)
    return peak


def main():
    if not WAV:
        print("  [SKIP] 找不到可用的已知 WAV")
        return 2
    peak = wav_peak(WAV)
    print(f"  已知音频 {WAV.name}，峰值 {peak:.3f}")
    if peak < 0.05:
        print("  [SKIP] 这段 WAV 基本是静音，不能当测试信号")
        return 2

    # 【2026-10-07 强化清场】反复杀到进程数为 0 再继续。
    #   原来只 taskkill 一次 + sleep：前一个守卫的实例没退干净就抢走端口/房间，
    #   后一个守卫会报假失败（实测：release 里"共享已开始=False"，单跑却 PASS）。
    for _ in range(10):
        subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
        _n = subprocess.run(['powershell', '-NoProfile', '-Command',
                             "(Get-Process ZongxianVoice -ErrorAction SilentlyContinue).Count"],
                            capture_output=True, text=True).stdout.strip()
        if _n == "0":
            break
        time.sleep(0.7)
    time.sleep(0.5)
    for f in (HL, JL):
        if f.exists():
            f.unlink()
    subprocess.run('start "" /b "%s" --port %d --name host --room a1g --share-audio-test '
                   '--log-file "%s"' % (EXE, H, HL), shell=True)
    time.sleep(6)
    subprocess.run('start "" /b "%s" --port %d --name joiner --room a1g '
                   '--signal ws://127.0.0.1:%d/signal --share-audio-test --log-file "%s"'
                   % (EXE, J, H, JL), shell=True)
    time.sleep(10)
    winmm = ctypes.windll.winmm
    SND_ASYNC, SND_FILENAME, SND_LOOP = 0x0001, 0x00020000, 0x0008
    winmm.PlaySoundW(str(WAV), None, SND_FILENAME | SND_ASYNC | SND_LOOP)
    time.sleep(32)
    winmm.PlaySoundW(None, None, 0)
    time.sleep(1)
    subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
    time.sleep(1)

    ok = True
    for path, label in ((HL, "host"), (JL, "join")):
        if not path.exists():
            print(f"  ! {label} 无日志")
            ok = False
            continue
        t = path.read_text(encoding="utf-8", errors="replace")
        out = max([int(x) for x in re.findall(r'"audioOutBytes":(\d+)', t)] or [0])
        inn = max([int(x) for x in re.findall(r'"audioInBytes":(\d+)', t)] or [0])
        energy = max([float(x) for x in re.findall(r'"audioInEnergy":([\d.]+)', t)] or [0])
        rms = max([float(x) for x in re.findall(r'"destRms":([\d.]+)', t)] or [0])
        # outboundDump 现在是 [[kind,bytes,pkts], ...]（为省日志体积压过字段），
        # 旧形态是 [{kind:...}] —— 两种都算，避免"诊断格式变了"造成假红。
        dump = len(re.findall(r'"outboundDump":\[\[', t)) + len(re.findall(r'"outboundDump":\[\{', t))
        print(f"  [{label}] destRms={rms:.3f} 发送={out} 接收={inn} 能量={energy:.2f} outboundDump条={dump}")
        if out <= 0:
            print(f"    ! {label} 没有发出任何 RTP 字节")
            ok = False
        if inn <= 0 or energy <= 0:
            print(f"    ! {label} 没有收到有效音频（字节或能量为 0）")
            ok = False
        if dump == 0:
            print(f"    ! {label} getStats 里没有 outbound-rtp 条目（音轨没被协商）")
            ok = False
    print("---- " + ("PASS: 共享电脑声音端到端" if ok else "FAIL: 共享电脑声音端到端"))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
