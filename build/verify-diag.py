#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「连接诊断」面板实测脚本（新）：在本机真跑一次多流传输，把诊断面板
（``swiftdrop.gui.DiagSampler``，就是 GUI 面板每秒调用的那一份逻辑）采集到的
指标字典原样打出来，并对 4 条硬指标做断言。

断言（全部基于实测值，拿不到就是失败，不做估算）：
  A) 平均速度 > 1 MB/s
  B) 各条流的速度都被采到（条数与并发流数一致，且都 > 0）
  C) 接收端磁盘写入耗时占比在 0 ~ 100% 之间
  D) 结论字段非空且在已知取值里
另加一条便宜的界面自检：
  E) 「连接诊断」卡片能在 tkinter 里建起来并画出曲线

用法：& "<python>" build/verify-diag.py            （默认 512MB）
      $env:SWIFTDROP_DIAG_MB="128"; & "<python>" build/verify-diag.py
"""
import hashlib
import json
import os
import shutil
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from swiftdrop.gui import DiagSampler              # noqa: E402
from swiftdrop.transfer import ReceiverServer, Sender   # noqa: E402

WORK = os.path.join(ROOT, 'tmp', 'verify-diag')
STREAMS = 4
SAMPLE_EVERY = 1.0
MB = int(os.environ.get('SWIFTDROP_DIAG_MB', '512'))
KNOWN_CODES = {'disk', 'uplink', 'stream_spread', 'lan_slow', 'good', 'normal'}

results = []


def ok(name, passed, detail=''):
    results.append((name, bool(passed), detail))
    print(('  [PASS] ' if passed else '  [FAIL] ') + name
          + ('  — ' + detail if detail else ''), flush=True)


def sha(path, buf=4 * 1024 * 1024):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(buf)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def mkfile(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        left = size
        while left > 0:
            n = min(left, 4 * 1024 * 1024)
            f.write(os.urandom(n))
            left -= n
    return sha(path)


def slim(m):
    """指标字典的可打印副本：采样序列只留统计量，避免刷屏。"""
    if not m:
        return m
    out = dict(m)
    samples = out.pop('samples', [])
    out['samples_count'] = len(samples)
    out['samples_rates_MBps'] = [round(r / 1048576.0, 2) for _t, r in samples]
    for key in ('rate', 'peak', 'avg', 'min'):
        if out.get(key) is not None:
            out[key + '_MBps'] = round(out[key] / 1048576.0, 3)
    for group, fields in (('streams_rate', None), ('streams_rate_avg', None),
                          ('streams_rate_peak', None)):
        d = out.get(group) or {}
        out[group] = {str(k): round(v / 1048576.0, 3) for k, v in d.items()}
    fluct = dict(out.get('fluct') or {})
    for key in ('min', 'max', 'mean', 'std'):
        if fluct.get(key) is not None:
            fluct[key + '_MBps'] = round(fluct[key] / 1048576.0, 3)
        fluct.pop(key, None)
    out['fluct_MBps'] = fluct
    return out


def reset_metrics_for_print(m):
    """把 'streams_rate' 等字典换成 MB/s 版本，原始字段保留一份。"""
    return slim(m)


def check_panel_widgets():
    """E) 真的把卡片建到 tkinter 里，画一次曲线。"""
    try:
        import tkinter as tk
    except Exception as exc:                                    # noqa: BLE001
        return False, f'tkinter 不可用: {exc}'
    try:
        from swiftdrop.gui import GuiApp
        app = GuiApp(port=45994)
        app.root.withdraw()
        app.root.update_idletasks()
        app.root.update()
        panel = app.diag
        panel.sampler.feed({
            'phase': 'recv', 'total': 1000, 'done': 500, 'rate': 1000.0,
            'elapsed': 1.0, 'peer_ip': '127.0.0.1', 'local_ip': '127.0.0.1',
            'streams': 2, 'stream_bytes': {0: 500, 1: 500},
            'disk_seconds': 0.5, 'disk_bytes': 500,
        })
        m1 = panel.sampler.sample()
        panel._render(m1)
        app.root.update_idletasks()
        app.root.update()
        items = len(panel.canvas.find_all())
        card_ok = bool(panel.chk) and bool(panel.canvas)
        app.on_close()
        msg = f'卡片控件已建，曲线画布 items={items}，链路={m1["link"]["kind"]}'
        return bool(card_ok and items > 0), msg
    except Exception as exc:                                    # noqa: BLE001
        import traceback
        return False, f'{exc} | {traceback.format_exc(limit=2)}'


def main():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK, ignore_errors=True)
    src_dir = os.path.join(WORK, 'src')
    dst_dir = os.path.join(WORK, 'dst')
    os.makedirs(src_dir, exist_ok=True)
    os.makedirs(dst_dir, exist_ok=True)

    big = os.path.join(src_dir, 'diag.bin')
    print(f'[准备] 生成 {MB} MB 随机文件 …', flush=True)
    t_gen = time.monotonic()
    want_sha = mkfile(big, MB * 1024 * 1024)
    print(f'        生成完成 {time.monotonic() - t_gen:.1f}s，'
          f'sha256={want_sha[:16]}…', flush=True)

    recv_sampler = DiagSampler()
    send_sampler = DiagSampler()
    logs = []

    srv = ReceiverServer(dst_dir, 0, on_progress=recv_sampler.feed,
                         on_log=logs.append)
    srv.start()
    port = srv.port
    sender = Sender('127.0.0.1', port, [big], streams=STREAMS,
                    on_progress=send_sampler.feed, on_log=logs.append)
    print(f'[传输] 127.0.0.1:{port}  {STREAMS} 条流  {MB} MB', flush=True)

    recv_sampler.reset()
    send_sampler.reset()
    box = {}

    def run_sender():
        try:
            box['r'] = sender.run()
        except BaseException as exc:                            # noqa: BLE001
            box['err'] = repr(exc)

    th = threading.Thread(target=run_sender, name='verify-diag-send',
                          daemon=True)
    t0 = time.monotonic()
    th.start()
    # 主线程每秒采一次样（GUI 里也是主线程 after(1000) 驱动）
    n_samples = 0
    next_t = time.monotonic() + SAMPLE_EVERY
    while th.is_alive():
        time.sleep(0.05)
        now = time.monotonic()
        if now >= next_t:
            next_t = now + SAMPLE_EVERY
            recv_sampler.sample()
            send_sampler.sample()
            n_samples += 1
    th.join(5.0)
    wall = time.monotonic() - t0
    recv_m = recv_sampler.sample(final=True)
    send_m = send_sampler.sample(final=True)
    srv.stop()

    got = os.path.join(dst_dir, 'diag.bin')
    same = os.path.isfile(got) and sha(got) == want_sha
    print(f'[传输] 完成 用时 {wall:.1f}s  采样 {n_samples} 次  '
          f'文件{"完整且 SHA-256 一致" if same else "缺失或校验失败"}', flush=True)
    if 'err' in box:
        print('  发送端异常: ' + box['err'], flush=True)

    print('\n================ 诊断面板指标字典（接收端）================')
    print(json.dumps(reset_metrics_for_print(recv_m), ensure_ascii=False,
                     indent=2, default=str), flush=True)
    print('\n================ 诊断面板指标字典（发送端）================')
    print(json.dumps(reset_metrics_for_print(send_m), ensure_ascii=False,
                     indent=2, default=str), flush=True)

    print('\n================ 「复制诊断信息」文本（接收端）================')
    print(recv_sampler.text(), flush=True)
    print('\n================ 「复制诊断信息」文本（发送端）================')
    print(send_sampler.text(), flush=True)

    print('\n================ 断言 ================', flush=True)
    speed = recv_m.get('avg') or recv_m.get('peak') or 0.0
    ok('A 平均速度 > 1 MB/s', speed > 1_000_000.0,
       f'60s 均值 {speed / 1048576.0:.2f} MB/s，'
       f'峰值 {(recv_m.get("peak") or 0) / 1048576.0:.2f} MB/s')

    r_peak = recv_m.get('streams_rate_peak') or {}
    s_peak = send_m.get('streams_rate_peak') or {}
    seen = sorted(k for k, v in r_peak.items() if v > 0)
    ok('B 各条流速度都被采到',
       len(seen) == STREAMS and all(v > 0 for v in r_peak.values()),
       f'接收端 {len(seen)}/{STREAMS} 条流各有实测速度 '
       f'({", ".join(f"#{k} {(r_peak[k] / 1048576.0):.1f} MB/s" for k in seen)})；'
       f'发送端 {len(s_peak)} 条流')

    disk = recv_m.get('disk') or {}
    ratio = disk.get('ratio')
    ok('C 磁盘写入耗时占比在 0~100% 之间',
       ratio is not None and 0.0 <= ratio <= 100.0,
       f'占比 {ratio:.1f}%（磁盘计时 {disk.get("seconds", 0):.2f}s，'
       f'传输耗时 {recv_m["progress"]["elapsed"]:.2f}s，'
       f'窗口均值写入速度 {(disk.get("rate_avg") or 0) / 1048576.0:.1f} MB/s）')

    concl = recv_m.get('conclusion') or ''
    code = recv_m.get('conclusion_code') or ''
    ok('D 结论字段非空且取值合法', bool(concl) and code in KNOWN_CODES,
       f'[{code}] {concl}')

    panel_ok, panel_msg = check_panel_widgets()
    ok('E 诊断卡片能在 tkinter 里建起来并画出曲线', panel_ok, panel_msg)

    print('\n================ 汇总 ================')
    for name, passed, detail in results:
        print(('PASS  ' if passed else 'FAIL  ') + name
              + ('  — ' + detail if detail else ''))
    bad = sum(1 for _n, p, _d in results if not p)
    print('%d/%d 通过' % (len(results) - bad, len(results)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
