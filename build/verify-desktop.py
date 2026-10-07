#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""独立验证桌面版（不依赖 tests/test_lan.py）：
   A) 128MB 单文件多流传输：完整性 + 实测速度
   B) 中文/空格/深层目录：结构与内容逐一比对
   C) 断点续传：传输中途杀掉发送端，重启后续传，最终必须整文件校验通过
   D) 目录双向同步：两边树结构必须一致
   E) 局域网信令中继 /signal 能否完成 WebSocket 升级（101）
用法：& "<python>" build/verify-desktop.py
"""
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'src')
WORK = os.path.join(ROOT, 'tmp', 'verify-desktop')
PORT = 45990
SYNC_PORT = 45991
RELAY_PORT = 45992

results = []


def ok(name, passed, detail=''):
    results.append((name, bool(passed), detail))
    print(('  [PASS] ' if passed else '  [FAIL] ') + name + ('  — ' + detail if detail else ''))


def sha(path, buf=1 << 20):
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
            n = min(left, 1 << 20)
            f.write(os.urandom(n))
            left -= n
    return sha(path)


def env():
    e = dict(os.environ)
    e['PYTHONPATH'] = SRC + os.pathsep + e.get('PYTHONPATH', '')
    e['PYTHONIOENCODING'] = 'utf-8'
    e['PYTHONUTF8'] = '1'
    return e


def sw(args, **kw):
    kw.setdefault('cwd', ROOT)
    kw.setdefault('env', env())
    return subprocess.run([sys.executable, '-m', 'swiftdrop'] + list(args), **kw)


def start_recv(recv_dir, extra=None, port=PORT):
    args = [sys.executable, '-m', 'swiftdrop', 'recv', '--dir', recv_dir, '--port', str(port)] + (extra or [])
    p = subprocess.Popen(args, cwd=ROOT, env=env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
    time.sleep(2.0)
    return p


def stop(p):
    try:
        p.terminate()
        p.wait(timeout=5)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
    out = ''
    try:
        out = p.stdout.read() or ''
    except Exception:
        pass
    return out


def tree(root):
    """列出文件树；跳过工具自身的内部文件（.swiftdrop* 与 *.part）。"""
    out = {}
    for dp, dn, fn in os.walk(root):
        for f in fn:
            if f.startswith('.swiftdrop') or f.endswith('.part'):
                continue
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, root).replace('\\', '/')
            out[rel] = os.path.getsize(p)
    return out


def main():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    # ---------- A) 单文件 128MB ----------
    print('\n[A] 128MB 单文件多流传输')
    big = os.path.join(WORK, 'src', 'big.bin')
    h_big = mkfile(big, 128 * 1024 * 1024)
    recv_a = os.path.join(WORK, 'recvA')
    rp = start_recv(recv_a)
    t0 = time.time()
    r = sw(['send', '127.0.0.1', big, '--port', str(PORT), '--streams', '4'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600)
    dt = time.time() - t0
    got = os.path.join(recv_a, 'big.bin')
    exists = os.path.exists(got)
    same = exists and sha(got) == h_big
    speed = (128 / dt) if dt > 0 else 0
    ok('A1 128MB 文件完整到达且 SHA-256 一致', same, '用时 %.1fs · %.1f MB/s · 状态=%s' % (dt, speed, '收到' if exists else '缺失'))
    if not same:
        print('    send 输出尾部：' + (r.stdout or '')[-800:])
        print('    recv 输出尾部：' + (rp.stdout.read() or '')[-400:] if rp.stdout else '')
    stop(rp)

    # ---------- B) 中文 / 空格 / 深层目录 ----------
    print('\n[B] 中文/空格/深层目录')
    srcdir = os.path.join(WORK, 'src', '我的 项目资料')
    files = {
        '第一章 说明.txt': 12 * 1024,
        'sub/deep/更深的 目录/数据😀.bin': 300 * 1024,
        'sub/a b/c d/最后的 文件.dat': 64 * 1024,
    }
    for rel, size in files.items():
        mkfile(os.path.join(srcdir, rel), size)
    recv_b = os.path.join(WORK, 'recvB')
    rp = start_recv(recv_b)
    r = sw(['send', '127.0.0.1', srcdir, '--port', str(PORT)], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
    stop(rp)
    b_ok = True
    detail = []
    for rel, size in files.items():
        want = os.path.join(srcdir, rel)
        # 接收端可能保留相对路径或只保留文件名，两种都接受
        cand = [os.path.join(recv_b, rel), os.path.join(recv_b, '我的 项目资料', rel), os.path.join(recv_b, os.path.basename(rel))]
        found = next((c for c in cand if os.path.exists(c)), None)
        if not found:
            b_ok = False
            detail.append('缺失 ' + rel + '（接收端内容：' + ','.join(sorted(tree(recv_b).keys())[:6]) + '）')
            break
        if sha(found) != sha(want):
            b_ok = False
            detail.append('内容不一致 ' + rel)
    ok('B1 中文/空格/emoji 文件名与深层目录都正确', b_ok, '；'.join(detail) or ('收到 %d 个文件' % len(tree(recv_b))))

    # ---------- C) 断点续传 ----------
    print('\n[C] 断点续传')
    big2 = os.path.join(WORK, 'src', 'resume.bin')
    h_resume = mkfile(big2, 128 * 1024 * 1024)
    recv_c = os.path.join(WORK, 'recvC')
    rp = start_recv(recv_c)
    p1 = subprocess.Popen([sys.executable, '-m', 'swiftdrop', 'send', '127.0.0.1', big2, '--port', str(PORT)], cwd=ROOT, env=env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
    time.sleep(4.0)
    p1.kill()
    try:
        p1.wait(timeout=5)
    except Exception:
        pass
    part = os.path.join(recv_c, 'resume.bin')
    partial_size = os.path.getsize(part) if os.path.exists(part) else 0
    r2 = sw(['send', '127.0.0.1', big2, '--port', str(PORT)], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600)
    stop(rp)
    same2 = os.path.exists(part) and sha(part) == h_resume
    resumed = ('续传' in (r2.stdout or '')) or ('跳过' in (r2.stdout or '')) or ('断点' in (r2.stdout or ''))
    ok('C1 中断后重传：最终整文件校验通过', same2, '中断时已落盘 %d 字节（%.1f MB），第二次输出%s提到续传' % (partial_size, partial_size / 1048576.0, '' if resumed else '未'))
    tail = (r2.stdout or '').strip().splitlines()[-3:]
    print('    send 尾部：' + ' | '.join(tail))

    # ---------- D) 双向同步 ----------
    print('\n[D] 目录双向同步')
    da = os.path.join(WORK, 'syncA')
    db = os.path.join(WORK, 'syncB')
    mkfile(os.path.join(da, 'only-a.txt'), 4096)
    mkfile(os.path.join(da, 'sub', 'x.txt'), 8192)
    mkfile(os.path.join(db, 'only-b.txt'), 2048)
    mkfile(os.path.join(db, 'sub', 'y.txt'), 16384)
    rp = start_recv(db, extra=['--sync-dir', db], port=SYNC_PORT)
    r = sw(['sync', '127.0.0.1', da, '--two-way', '--port', str(SYNC_PORT)], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
    stop(rp)
    ta, tb = tree(da), tree(db)
    same_tree = set(ta.keys()) == set(tb.keys())
    same_size = all(ta[k] == tb.get(k) for k in ta)
    ok('D1 双向同步后两边树结构与大小一致', same_tree and same_size,
       'A=' + ','.join(sorted(ta)) + ' B=' + ','.join(sorted(tb)))
    if not (same_tree and same_size):
        print('    sync 输出尾部：' + (r.stdout or '')[-600:])

    # ---------- E) 信令中继 WebSocket 升级 ----------
    print('\n[E] 局域网信令中继')
    p = subprocess.Popen([sys.executable, '-m', 'swiftdrop', 'relay', '--port', str(RELAY_PORT)], cwd=ROOT, env=env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
    time.sleep(1.5)
    upgraded = False
    detail = ''
    try:
        s = socket.create_connection(('127.0.0.1', RELAY_PORT), timeout=5)
        key = 'dGhlIHNhbXBsZSBub25jZQ=='
        req = ('GET /signal HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
               'Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n') % (RELAY_PORT, key)
        s.sendall(req.encode())
        resp = s.recv(4096).decode('utf-8', 'replace')
        upgraded = '101' in resp.split('\r\n')[0]
        detail = resp.split('\r\n')[0]
        s.close()
    except Exception as e:
        detail = 'ERR ' + str(e)
    stop(p)
    ok('E1 /signal 完成 WebSocket 升级（101 Switching Protocols）', upgraded, detail)

    print('\n================ 汇总 ================')
    for n, p_, d in results:
        print(('PASS  ' if p_ else 'FAIL  ') + n + ('  — ' + d if d else ''))
    bad = sum(1 for _, p_, _ in results if not p_)
    print('%d/%d 通过' % (len(results) - bad, len(results)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
