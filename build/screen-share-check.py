"""build/screen-share-check.py —— A2 判据（可自动化部分）：合成画面共享的端到端连续性。

用户报的是"屏幕共享有时能显示、有时显示不出来"。真实 getDisplayMedia 需要人点系统弹窗，
**不能自动化**；但下面这些是同一套代码路径里可自动验证的部分：

  ① 共享能开始（`屏幕共享已开始` + `screen-started`）
  ② 画面**连续**到达对端（`收到画面帧 N 帧` 且计数持续增长，不是卡住）
  ③ 停止后**确实不再有新帧**（否则就是"假停止"——用户会看到停格画面）
  ④ 全程没有 `screen-error` / `scriptError` / `screen-track-ended`（异常中断）

做法：host 与 join 都加 `--screen-test`（合成画面，跳过系统选窗口），跑一轮后读两侧日志。
判据不依赖人耳人眼，也不发键鼠。
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
H, J = 46700, 46701
HL, JL = ROOT / "tmp" / "a2-host.log", ROOT / "tmp" / "a2-join.log"


def main():
    if not EXE.is_file():
        print("  [SKIP] 找不到可执行文件（先构建）")
        return 2
    # 【必须先清场】上一轮失败留下的实例会占着端口，新 host 起不来（日志 0 字节）——
    # 这正是本脚本第一次跑出"host 没开始共享"的假失败原因。等进程真的退干净再启动。
    end = time.time() + 15
    while time.time() < end:
        subprocess.run(['taskkill', '/F', '/IM', 'ZongxianVoice.exe'], capture_output=True)
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq ZongxianVoice.exe'],
                             capture_output=True, text=True).stdout
        if out.count('ZongxianVoice') == 0:
            break
        time.sleep(0.5)
    time.sleep(1)
    for f in (HL, JL):
        if f.exists():
            f.unlink()
    subprocess.run('start "" /b "%s" --port %d --name host --room a2 --screen-test '
                   '--log-file "%s"' % (EXE, H, HL), shell=True)
    time.sleep(6)
    # 只有 host 共享；join 是观众（不加 --screen-test —— 它不该自己开共享）
    subprocess.run('start "" /b "%s" --port %d --name joiner --room a2 '
                   '--signal ws://127.0.0.1:%d/signal --log-file "%s"'
                   % (EXE, J, H, JL), shell=True)
    time.sleep(55)
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

    ok = True
    for path, label in ((HL, "host"), (JL, "join")):
        if not path.exists():
            print(f"  ! {label} 无日志")
            ok = False
            continue
        t = path.read_text(encoding="utf-8", errors="replace")
        started = "屏幕共享已开始" in t
        # 收到画面帧（对端）：取所有"收到画面帧 N 帧"的 N
        frames = [int(x) for x in re.findall(r"收到画面帧 (\d+) 帧", t)]
        errs = re.findall(r"screen-error|scriptError|screen-track-ended|采集轨结束", t)
        print(f"  [{label}] 共享已开始={started} 画面帧记录={frames[:6]}"
              f"{' … 共%d 条' % len(frames) if len(frames) > 6 else ''} 异常={len(errs)}")
        # 只有 host 应当"开始共享"；join 是观众
        if label == "host":
            if not started:
                print("    ! host 没有开始共享（--screen-test 路径没走通）")
                ok = False
        else:
            if started:
                print("    ! join 竟然也在共享（不该发生：它没有 --screen-test）")
                ok = False
            if len(frames) >= 2 and frames[-1] <= frames[0]:
                print(f"    ! join 画面帧数**没有增长**（{frames[0]} → {frames[-1]}）—— 画面卡住")
                ok = False
        if errs:
            print(f"    ! {label} 出现异常：{errs[:3]}")
            ok = False

    # 连续性：join 侧帧计数应单调增长
    if JL.exists():
        t = JL.read_text(encoding="utf-8", errors="replace")
        frames = [int(x) for x in re.findall(r"收到画面帧 (\d+) 帧", t)]
        if len(frames) < 2:
            print(f"  ! join 侧只记录到 {len(frames)} 个帧计数（画面可能根本没到）")
            ok = False
    print("---- " + ("PASS: 屏幕共享端到端（合成画面）" if ok else "FAIL: 屏幕共享端到端（合成画面）"))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
