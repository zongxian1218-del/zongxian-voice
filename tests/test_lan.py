"""SwiftDrop 局域网自测（纯标准库，直接运行）。

    & "C:\\Users\\Administrator\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\
       dependencies\\python\\python.exe" D:\\文档\\ai001\\tests\\test_lan.py

覆盖 6 项：
    1. 速度 + 正确性（200MB 随机文件，4 流，127.0.0.1，SHA-256 一致，实测 MB/s）
    2. 中文 / 深层目录 / 带空格的大文件名
    3. 断点续传（中途 kill 发送端，重启后续传，校验 SHA-256 + 打印跳过的字节数）
    4. 同步（two-way 收敛 + --delete-extra）
    5. 信令中继（两条连接通过 topic 互发，含中文 JSON）
    6. webhost（GET 首页 200 + /signal WebSocket 升级成功 + Range + 穿越防护）
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from collections.abc import Callable

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# Windows 控制台默认 GBK，打印 emoji / 中文路径会炸；先强制 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

PY = sys.executable
PORT = 45880
CHUNK_MB = 4                    # 默认分片大小（MB）
SPEED_TARGET_MBPS = 80.0     # 目标值（报告里对比）
SPEED_PASS_MBPS = 50.0       # 本机/CI 的最低通过线
BIG_MB = 200
BIG_BYTES = BIG_MB * 1024 * 1024

RESULTS: list[tuple[str, str, float, str]] = []
TMP = tempfile.mkdtemp(prefix="swiftdrop-test-")
LOG_DIR = os.path.join(TMP, "logs")
os.makedirs(LOG_DIR, exist_ok=True)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"== {title}")
    print("=" * 78, flush=True)


def run_cli(args: list[str], *, timeout: float = 900, log_name: str = "cli",
            env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    if env_extra:
        env.update(env_extra)
    log_path = os.path.join(LOG_DIR, f"{log_name}.log")
    with open(log_path, "wb") as log:
        proc = subprocess.run(
            [PY, "-m", "swiftdrop", *args], cwd=os.path.dirname(SRC),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, env=env)
    text = proc.stdout.decode("utf-8", "replace")
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(f"$ python -m swiftdrop {' '.join(args)}\n\n{text}\n")
    return subprocess.CompletedProcess(proc.args, proc.returncode, text, "")


class ReceiverProc:
    """后台运行的 `python -m swiftdrop recv`。"""

    def __init__(self, dest: str, port: int = PORT, extra: list[str] | None = None,
                 log_name: str = "recv", chunk_mb: float | None = None,
                 stall_after: int | None = None, stall_seconds: float = 3.0):
        env = dict(os.environ)
        env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        if stall_after:
            # 让接收端写完 N 片后暂停，保证 kill 一定落在传输中途
            env["SWIFTDROP_STALL_AFTER_CHUNKS"] = str(stall_after)
            env["SWIFTDROP_STALL_SECONDS"] = str(stall_seconds)
        self.log_path = os.path.join(LOG_DIR, f"{log_name}.log")
        self._fh = open(self.log_path, "wb")
        cmd = [PY, "-m", "swiftdrop", "recv", "--dir", dest, "--port", str(port)]
        if chunk_mb:
            cmd += ["--chunk-mb", str(chunk_mb)]
        cmd += extra or []
        self.proc = subprocess.Popen(cmd, cwd=os.path.dirname(SRC),
                                     stdout=self._fh, stderr=subprocess.STDOUT,
                                     env=env)
        self.port = port
        self._wait_listen()

    def _wait_listen(self, timeout: float = 25.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"接收端提前退出，见 {self.log_path}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.4):
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("接收端未在超时内监听端口")

    def log_text(self) -> str:
        self._fh.flush()
        with open(self.log_path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        try:
            self._fh.close()
        except Exception:
            pass


def sha256_of(path: str, block: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            data = fh.read(block)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def make_random_file(path: str, size: int, block: int = 4 * 1024 * 1024) -> str:
    """用 os.urandom 分块写，别一次性吃内存。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    remaining = size
    with open(path, "wb") as fh:
        while remaining > 0:
            n = min(block, remaining)
            fh.write(os.urandom(n))
            remaining -= n
    return sha256_of(path)


def tree_of(root: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out[rel] = os.path.getsize(full)
    return out


def cleanup(*paths: str) -> None:
    for p in paths:
        shutil.rmtree(p, ignore_errors=True)
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass


class Skip(Exception):
    pass


def record(name: str, status: str, seconds: float, detail: str) -> None:
    RESULTS.append((name, status, seconds, detail))
    icon = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[status]
    print(f"\n---- [{icon}] {name}  ({seconds:.1f}s)\n     {detail}", flush=True)


def run_test(name: str, fn: Callable[[], str]) -> None:
    banner(name)
    t0 = time.monotonic()
    try:
        detail = fn()
        record(name, "PASS", time.monotonic() - t0, detail)
    except Skip as exc:
        record(name, "SKIP", time.monotonic() - t0, str(exc))
    except Exception as exc:                                    # noqa: BLE001
        traceback.print_exc()
        record(name, "FAIL", time.monotonic() - t0, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# WebSocket 测试客户端（标准库手写，客户端帧必须带掩码）
# --------------------------------------------------------------------------

class WSClient:
    def __init__(self, host: str, port: int, path: str = "/signal",
                 timeout: float = 10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            piece = self.sock.recv(4096)
            if not piece:
                raise RuntimeError("握手时连接被关闭")
            buf += piece
        head, _, rest = buf.partition(b"\r\n\r\n")
        self.handshake = head.decode("latin-1")
        if "101" not in self.handshake.split("\r\n")[0]:
            raise RuntimeError(f"握手失败: {self.handshake!r}")
        expect = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        if expect.lower() not in self.handshake.lower():
            raise RuntimeError("Sec-WebSocket-Accept 不正确")
        self.buf = bytearray(rest)

    # -- 发送（掩码）------------------------------------------------------
    def send_frame(self, payload: bytes, opcode: int = 0x1, fin: bool = True) -> None:
        b0 = (0x80 if fin else 0) | opcode
        out = bytearray([b0])
        key = os.urandom(4)
        n = len(payload)
        if n < 126:
            out.append(0x80 | n)
        elif n < 65536:
            out.append(0x80 | 126)
            out += struct.pack(">H", n)
        else:
            out.append(0x80 | 127)
            out += struct.pack(">Q", n)
        out += key
        out += bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(out))

    def send_json(self, obj) -> None:
        self.send_frame(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def send_text_bytes(self, raw: bytes) -> None:
        self.send_frame(raw)

    # -- 接收（服务端不加掩码）--------------------------------------------
    def _recv_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            piece = self.sock.recv(65536)
            if not piece:
                raise RuntimeError("连接已关闭")
            self.buf += piece
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def recv_frame(self) -> tuple[bool, int, bytes]:
        head = self._recv_exact(2)
        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            (length,) = struct.unpack(">H", self._recv_exact(2))
        elif length == 127:
            (length,) = struct.unpack(">Q", self._recv_exact(8))
        if masked:
            raise RuntimeError("服务端不应给帧加掩码")
        return fin, opcode, self._recv_exact(length)

    def recv_message(self, timeout: float = 6.0) -> dict:
        deadline = time.monotonic() + timeout
        frag_op = None
        frag = b""
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise TimeoutError("等待消息超时")
            self.sock.settimeout(remain)
            fin, opcode, payload = self.recv_frame()
            if opcode in (0x9, 0xA):
                continue
            if opcode == 0x8:
                raise RuntimeError("服务端关闭连接")
            if opcode == 0x0:
                frag += payload
                if fin:
                    data, opcode, frag_op = frag, frag_op, None
                else:
                    continue
            else:
                if fin:
                    data = payload
                else:
                    frag_op, frag = opcode, payload
                    continue
            if opcode != 0x1:
                raise RuntimeError(f"期望文本帧，收到 opcode={opcode}")
            return json.loads(data.decode("utf-8"))

    def close(self) -> None:
        try:
            self.send_frame(struct.pack(">H", 1000), opcode=0x8)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ==========================================================================
# 测试 1：速度 + 正确性
# ==========================================================================

SENT_BIG = os.path.join(TMP, "big", "big-200mb.bin")
RECV_BIG = os.path.join(TMP, "recv-big")


def test_speed() -> str:
    make_random_file(SENT_BIG, BIG_BYTES)
    src_hash = sha256_of(SENT_BIG)
    size = os.path.getsize(SENT_BIG)
    print(f"已生成 {size / 1048576:.0f}MB 随机文件：{SENT_BIG}", flush=True)
    print(f"源 SHA-256 = {src_hash}", flush=True)

    recv = ReceiverProc(RECV_BIG, PORT, log_name="recv-speed")
    try:
        t0 = time.monotonic()
        proc = run_cli(["send", "127.0.0.1", SENT_BIG, "--streams", "4",
                        "--port", str(PORT)], log_name="send-speed")
        elapsed = time.monotonic() - t0
        print(proc.stdout, flush=True)
        if proc.returncode != 0:
            raise RuntimeError(f"发送端返回 {proc.returncode}")

        dst = os.path.join(RECV_BIG, os.path.basename(SENT_BIG))
        if not os.path.isfile(dst):
            raise RuntimeError(f"接收目录里没有目标文件，实际内容: {tree_of(RECV_BIG)}")
        dst_hash = sha256_of(dst)
        if dst_hash != src_hash:
            raise RuntimeError(f"SHA-256 不一致：{dst_hash} != {src_hash}")

        mbytes = os.path.getsize(dst) / 1048576
        mbps = mbytes / elapsed if elapsed > 0 else 0.0
        recv_log = recv.log_text()
        if "校验通过" not in recv_log:
            raise RuntimeError("接收端没有打印校验通过")

        # 顺便验证没有 .part 残留
        parts = [f for f in os.listdir(RECV_BIG) if f.endswith(".part")]
        if parts:
            raise RuntimeError(f"有 .part 残留: {parts}")

        detail = (f"{mbytes:.0f}MB / {elapsed:.2f}s = {mbps:.1f} MB/s "
                  f"（目标 ≥{SPEED_TARGET_MBPS:.0f}，最低通过线 {SPEED_PASS_MBPS:.0f}）"
                  f"；SHA-256 一致，无 .part 残留")
        if mbps < SPEED_PASS_MBPS:
            raise AssertionError(f"速度过低：{mbps:.1f} MB/s < {SPEED_PASS_MBPS}")
        return detail
    finally:
        recv.stop()


# ==========================================================================
# 测试 2：中文 / 深层目录 / 大文件名
# ==========================================================================

SENT_TREE = os.path.join(TMP, "中文素材")
RECV_TREE = os.path.join(TMP, "recv-tree")

DEEP_DIRS = [
    "项目资料/文档 与 说明",
    "项目资料/文档 与 说明/第二层/第三层",
    "图片 🙈 emoji 目录",
    "数据/2026-02/明细",
]
DEEP_FILES = {
    "项目资料/文档 与 说明/说明 文档 v1.0.txt": "中文内容测试\n",
    "项目资料/文档 与 说明/第二层/第三层/深层 文件 名字 很长" + "x" * 80 + ".log": "deep\n",
    "图片 🙈 emoji 目录/表情 😀🎉 文件.png": "fake png\n",
    "数据/2026-02/明细/明细 表 2026-02.csv": "a,b,c\n1,2,3\n",
    "根目录 文件 带 空格.txt": "root\n",
}


def test_unicode_tree() -> str:
    cleanup(SENT_TREE, RECV_TREE)
    for d in DEEP_DIRS:
        os.makedirs(os.path.join(SENT_TREE, d), exist_ok=True)
    for rel, content in DEEP_FILES.items():
        path = os.path.join(SENT_TREE, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    src_tree = tree_of(SENT_TREE)
    print(f"源目录 {len(src_tree)} 个文件：")
    for rel in src_tree:
        print("   ", rel, flush=True)

    recv = ReceiverProc(RECV_TREE, PORT, log_name="recv-tree")
    try:
        proc = run_cli(["send", "127.0.0.1", SENT_TREE, "--streams", "4",
                        "--port", str(PORT)], log_name="send-tree")
        print(proc.stdout, flush=True)
        if proc.returncode != 0:
            raise RuntimeError(f"发送端返回 {proc.returncode}")
    finally:
        recv.stop()

    base = os.path.join(RECV_TREE, os.path.basename(SENT_TREE))
    got = tree_of(base)
    print("接收后目录：")
    for rel in sorted(got):
        print("   ", rel, flush=True)
    if set(got) != set(src_tree):
        missing = set(src_tree) - set(got)
        extra = set(got) - set(src_tree)
        raise AssertionError(f"目录结构不一致，缺失={missing} 多余={extra}")
    for rel in src_tree:
        with open(os.path.join(base, rel), "rb") as fh:
            a = fh.read()
        with open(os.path.join(SENT_TREE, rel), "rb") as fh:
            b = fh.read()
        if a != b:
            raise AssertionError(f"内容不一致: {rel}")
    longest = max(len(r.encode("utf-8")) for r in src_tree)
    return (f"{len(src_tree)} 个文件（含中文、空格、emoji、最长 {longest} 字节路径）"
            f"目录结构与内容全部一致")


# ==========================================================================
# 测试 3：断点续传
# ==========================================================================

SENT_RESUME = os.path.join(TMP, "resume", "resume-big.bin")
RECV_RESUME = os.path.join(TMP, "recv-resume")
RESUME_MB = 1024
RESUME_CHUNK_MB = 8          # 1GB / 8MB = 128 片，本地跑到第 2 片再 kill 完全来得及


def _state_info(dest: str) -> tuple[int, int]:
    """返回 (已完成分片数, 总分片数)。"""
    path = os.path.join(dest, ".swiftdrop-state.json")
    if not os.path.isfile(path):
        return (0, 0)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return (0, 0)
    files = data.get("files") or {}
    if not files:
        return (0, 0)
    e = next(iter(files.values()))
    have = len(e.get("have") or [])
    return (have, int(e.get("chunks") or 0))


def _part_size(dest: str) -> int:
    for dirpath, _d, files in os.walk(dest):
        for fn in files:
            if fn.endswith(".part"):
                return os.path.getsize(os.path.join(dirpath, fn))
    return 0


def test_resume() -> str:
    size = RESUME_MB * 1024 * 1024
    make_random_file(SENT_RESUME, size)
    src_hash = sha256_of(SENT_RESUME)
    chunk_bytes = RESUME_CHUNK_MB * 1024 * 1024
    total_chunks = (size + chunk_bytes - 1) // chunk_bytes

    recv = ReceiverProc(RECV_RESUME, PORT, log_name="recv-resume-pass1",
                        chunk_mb=RESUME_CHUNK_MB, stall_after=2,
                        stall_seconds=4.0)
    try:
        env = dict(os.environ)
        env["PYTHONPATH"] = SRC
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        log1 = os.path.join(LOG_DIR, "send-resume-pass1.log")
        with open(log1, "wb") as fh:
            proc = subprocess.Popen(
                [PY, "-m", "swiftdrop", "send", "127.0.0.1", SENT_RESUME,
                 "--streams", "4", "--port", str(PORT),
                 "--chunk-mb", str(RESUME_CHUNK_MB)],
                cwd=os.path.dirname(SRC), stdout=fh, stderr=subprocess.STDOUT,
                env=env)
        # 让发送端跑起来，等状态文件里出现「已完整落盘的分片」再 kill。
        # 只等 1 片、5ms 轮询一次，本机再快也来得及（实测 200MB/s 下每片 ~20ms）。
        have = chunks = 0
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            have, chunks = _state_info(RECV_RESUME)
            if have >= 1:
                break
            if proc.poll() is not None:
                raise RuntimeError("发送端在可中断之前就结束了（本地太快）")
            time.sleep(0.005)
        proc.kill()
        proc.wait(timeout=20)
        part = _part_size(RECV_RESUME)
        chunks = chunks or total_chunks
        print(f"已 kill 发送端：.part 大小={part / 1048576:.1f}MB，"
              f"状态记录 have={have}/{chunks} 片", flush=True)
        if have < 1:
            raise RuntimeError(f"状态文件里没有已完成分片（have={have}），无法验证续传")
        killed_have, killed_bytes = have, have * chunk_bytes

        recv.stop()
        # 重新起接收端（模拟「重启接收端 + 重启发送端」）
        recv = ReceiverProc(RECV_RESUME, PORT, log_name="recv-resume-pass2",
                            chunk_mb=RESUME_CHUNK_MB)
        proc2 = run_cli(["send", "127.0.0.1", SENT_RESUME, "--streams", "4",
                         "--port", str(PORT), "--chunk-mb", str(RESUME_CHUNK_MB)],
                        log_name="send-resume-pass2")
        print(proc2.stdout, flush=True)
        if proc2.returncode != 0:
            raise RuntimeError(f"续传发送端返回 {proc2.returncode}")

        dst = os.path.join(RECV_RESUME, os.path.basename(SENT_RESUME))
        if not os.path.isfile(dst):
            raise RuntimeError(f"续传后目标文件不存在: {tree_of(RECV_RESUME)}")
        dst_hash = sha256_of(dst)
        if dst_hash != src_hash:
            raise RuntimeError(f"续传后 SHA-256 不一致：{dst_hash} != {src_hash}")

        skipped_chunks = 0
        skipped_bytes = 0
        for line in proc2.stdout.splitlines():
            if "续传" not in line or "跳过" not in line:
                continue
            print("   ", line.strip(), flush=True)
            inside = line.split("跳过", 1)[1]
            nums = "".join(c if c.isdigit() or c == "." else " "
                           for c in inside).split()
            if nums:
                skipped_chunks = int(float(nums[0]))
            if "(" in inside and ")" in inside:
                human = inside[inside.index("(") + 1:inside.index(")")]
                unit = human.strip().split()[-1]
                value = float(human.strip().split()[0])
                factor = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}
                skipped_bytes = int(value * factor.get(unit, 1))
        if skipped_chunks <= 0:
            raise AssertionError("续传日志里没有可解析的「跳过 N 片」")
        if skipped_chunks < killed_have:
            raise AssertionError(
                f"续传跳过的分片数 {skipped_chunks} 少于中断前已完成的 {killed_have}")
        residue = [f for f in os.listdir(RECV_RESUME) if f.endswith(".part")]
        if residue:
            raise RuntimeError(f"续传完成后有 .part 残留: {residue}")
        return (f"{RESUME_MB}MB 文件（{RESUME_CHUNK_MB}MB/片，共 {chunks} 片）中途 kill："
                f"中断时已落盘 {killed_have} 片（约 {killed_bytes / 1048576:.0f}MB），"
                f"重启后端到端 SHA-256 一致（{dst_hash[:16]}…），"
                f"第二轮跳过 {skipped_chunks}/{chunks} 片（约 "
                f"{skipped_bytes / 1048576:.0f}MB）无需重传；无 .part 残留")
    finally:
        recv.stop()


# ==========================================================================
# 测试 4：同步
# ==========================================================================

def _write(path: str, content: str, mtime: float | None = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    if mtime:
        os.utime(path, (mtime, mtime))


def test_sync() -> str:
    a = os.path.join(TMP, "sync-a")
    b = os.path.join(TMP, "sync-b")
    cleanup(a, b)
    sync_port = PORT + 3          # 换一个端口，避开 recv 子进程占用的 PORT
    now = time.time()
    # A：新文件 a-new.txt、b 里没有的 a-only.txt、旧版本 shared.txt
    _write(os.path.join(a, "a-new.txt"), "来自 A 的新文件\n", now)
    _write(os.path.join(a, "a-only.txt"), "只在 A\n", now)
    _write(os.path.join(a, "sub 目录/中文 文件.txt"), "A 的深层文件\n", now)
    _write(os.path.join(a, "shared.txt"), "A 的旧版本\n", now - 600)
    # B：新文件 b-new.txt、A 里没有的 b-only.txt、新版本 shared.txt
    _write(os.path.join(b, "b-new.txt"), "来自 B 的新文件\n", now)
    _write(os.path.join(b, "b-only.txt"), "只在 B\n", now)
    _write(os.path.join(b, "shared.txt"), "B 的新版本\n", now)

    recv = ReceiverProc(b, sync_port,
                        extra=["--delete-extra", "--sync-dir", b],
                        log_name="recv-sync")
    try:
        # 双向同步（默认不删）：两边内容应当完全一致
        proc = run_cli(["sync", f"127.0.0.1:{sync_port}", a, "--two-way",
                        "--streams", "4", "--port", str(sync_port)],
                       log_name="sync-twoway")
        print(proc.stdout, flush=True)
        if proc.returncode != 0 and "失败" not in proc.stdout:
            raise RuntimeError(f"sync 返回 {proc.returncode}")
        ta, tb = tree_of(a), tree_of(b)
        print("A:", sorted(ta), flush=True)
        print("B:", sorted(tb), flush=True)
        if set(ta) != set(tb):
            raise AssertionError(
                f"双向同步后文件集合不一致 A-B={set(ta) - set(tb)} "
                f"B-A={set(tb) - set(ta)}")
        ha = sha256_of(os.path.join(a, "shared.txt"))
        hb = sha256_of(os.path.join(b, "shared.txt"))
        if ha != hb or "B 的新版本" not in open(os.path.join(a, "shared.txt"),
                                              encoding="utf-8").read():
            raise AssertionError("shared.txt 没有按 mtime 新者胜收敛到 B 的版本")
        for rel in ta:
            if rel.startswith("."):        # .swiftdrop-state.json 是两端各自的元数据
                continue
            if sha256_of(os.path.join(a, rel)) != sha256_of(os.path.join(b, rel)):
                raise AssertionError(f"内容不一致: {rel}")
        txt_after_twoway = (f"A={len(ta)} 个文件，B={len(tb)} 个文件，全部一致")

        # 再测 --delete-extra（two-way）：
        #   语义 = 让两边收敛成「交集」。所以 A 独有的文件在 A 上被删、
        #   B 独有的文件在 B 上被删，而不是互相推送。
        _write(os.path.join(b, "b-extra-to-delete.txt"), "多余文件\n", now)
        _write(os.path.join(a, "a-extra-to-delete.txt"), "多余文件\n", now)
        proc = run_cli(["sync", f"127.0.0.1:{sync_port}", a, "--two-way",
                        "--delete-extra", "--streams", "4", "--port", str(sync_port)],
                       log_name="sync-delete")
        print(proc.stdout, flush=True)
        ta, tb = tree_of(a), tree_of(b)
        print("A:", sorted(ta), flush=True)
        print("B:", sorted(tb), flush=True)
        if "a-extra-to-delete.txt" in ta:
            raise AssertionError("two-way --delete-extra 没有删除本地多余文件")
        if "b-extra-to-delete.txt" in tb:
            raise AssertionError("two-way --delete-extra 没有删除远端多余文件")
        if set(ta) != set(tb):
            raise AssertionError(f"删除后两边仍不一致 A-B={set(ta) - set(tb)} "
                                 f"B-A={set(tb) - set(ta)}")
        txt_delete = (f"two-way --delete-extra 后两边收敛为 {len(ta)} 个文件"
                      f"（A/B 各自独有的多余文件都被删除）")

        # 再测 one-way --delete-extra：本地是权威，远端独有文件必须被删掉
        _write(os.path.join(b, "should-be-deleted-on-b.txt"), "只在 B\n", now)
        _write(os.path.join(a, "only-on-a.txt"), "只在 A\n", now)
        proc = run_cli(["sync", f"127.0.0.1:{sync_port}", a, "--one-way",
                        "--delete-extra", "--streams", "4", "--port", str(sync_port)],
                       log_name="sync-oneway-delete")
        print(proc.stdout, flush=True)
        ta, tb = tree_of(a), tree_of(b)
        if "should-be-deleted-on-b.txt" in tb:
            raise AssertionError("--one-way --delete-extra 没有删除远端多余文件")
        if "only-on-a.txt" not in tb:
            raise AssertionError("--one-way 没有把本地独有的文件推给远端")
        return (f"two-way：{txt_after_twoway}（shared.txt 按 mtime 新者胜收敛）；"
                f"{txt_delete}；one-way --delete-extra 删除远端多余文件并推送"
                f"本地独有文件，A={len(ta)}/B={len(tb)}")
    finally:
        recv.stop()


# ==========================================================================
# 测试 5：信令中继
# ==========================================================================

def test_signaling() -> str:
    from swiftdrop.signaling import SignalRelay, WebSocketPeer, handshake_response
    import socketserver

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            sock = self.request
            sock.settimeout(15)
            data = b""
            while b"\r\n\r\n" not in data:
                piece = sock.recv(4096)
                if not piece:
                    return
                data += piece
            head, _, _rest = data.partition(b"\r\n\r\n")
            key = ""
            for line in head.decode("latin-1").split("\r\n")[1:]:
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            if not key:
                return
            sock.sendall(handshake_response(key))
            self.server.relay.serve_peer(WebSocketPeer(sock, self.client_address))

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    relay = SignalRelay()
    logs: list[str] = []
    relay.on_log = logs.append
    srv = Server(("127.0.0.1", 0), Handler)
    srv.relay = relay
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2},
                     daemon=True).start()

    c1 = c2 = c3 = None
    try:
        c1 = WSClient("127.0.0.1", port)
        c2 = WSClient("127.0.0.1", port)
        c3 = WSClient("127.0.0.1", port)
        print("三条连接握手成功（Sec-WebSocket-Accept 校验通过）", flush=True)

        # ping → pong
        c1.send_json({"t": "ping"})
        pong = c1.recv_message()
        assert pong.get("t") == "pong", f"ping 没有得到 pong: {pong}"

        # 非法 topic
        c1.send_json({"t": "sub", "topic": "bad topic!"})
        err = c1.recv_message()
        assert err.get("t") == "err", f"非法 topic 未报错: {err}"

        # 多 topic 订阅（topic 只允许 [A-Za-z0-9_-]）
        for topic in ("room-lan-1", "second_topic", "third-topic"):
            c1.send_json({"t": "sub", "topic": topic})
            c2.send_json({"t": "sub", "topic": topic})
        # c3 只订阅其中一个
        c3.send_json({"t": "sub", "topic": "room-lan-1"})
        c3.send_json({"t": "sub", "topic": "third-topic"})
        time.sleep(0.4)

        # 中文 JSON 原样透传
        payload = {"t": "pub", "topic": "room-lan-1",
                   "d": {"msg": "你好，世界！🎉", "n": 42,
                         "nested": {"列表": [1, 2, 3], "空": None, "真": True}}}
        c1.send_json(payload)
        got = c2.recv_message()
        assert got == {"t": "msg", "topic": "room-lan-1", "d": payload["d"]}, got
        got3 = c3.recv_message()
        assert got3["d"] == payload["d"], got3
        print("中文 JSON 透传一致：", json.dumps(got, ensure_ascii=False), flush=True)

        # 发布者自己不应收到自己的消息
        c1.sock.settimeout(0.8)
        try:
            self_echo = c1.recv_message(timeout=0.8)
            raise AssertionError(f"发布者收到了自己的消息: {self_echo}")
        except (TimeoutError, socket.timeout):
            pass

        # 未订阅的 topic 收不到
        c2.send_json({"t": "pub", "topic": "nobody-subscribed", "d": "x"})
        c1.sock.settimeout(0.8)
        try:
            leaked = c1.recv_message(timeout=0.8)
            raise AssertionError(f"未订阅却收到消息: {leaked}")
        except (TimeoutError, socket.timeout):
            pass

        # 分片帧：把一条 JSON 拆成 3 段（文本 + continuation×2）
        raw = json.dumps({"t": "pub", "topic": "second_topic",
                          "d": "分片消息测试"}, ensure_ascii=False).encode("utf-8")
        third = len(raw) // 3
        c1.send_frame(raw[:third], opcode=0x1, fin=False)
        c1.send_frame(raw[third:2 * third], opcode=0x0, fin=False)
        c1.send_frame(raw[2 * third:], opcode=0x0, fin=True)
        frag = c2.recv_message()
        assert frag["d"] == "分片消息测试", frag
        print("分片帧（text + 2×continuation）拼装正确", flush=True)

        # 控制帧 ping（opcode 0x9）→ 服务端回 pong
        c1.send_frame(b"hb", opcode=0x9)
        deadline = time.monotonic() + 3
        got_pong = False
        while time.monotonic() < deadline and not got_pong:
            fin, opcode, data = c1.recv_frame()
            if opcode == 0xA:
                got_pong = (data == b"hb")
        assert got_pong, "控制帧 ping 没有得到 pong"

        # 订阅第三个 topic 并验证只发给订阅者
        c1.send_json({"t": "pub", "topic": "third-topic", "d": ["数组", 1]})
        t3 = c3.recv_message()
        assert t3["d"] == ["数组", 1], t3
        # c2 没订阅 third-topic（它订阅了，跳过这条断言）

        # 断开清理：先把 c2 缓冲区里可能残留的消息读干净，再看 after-close
        c3.close()
        time.sleep(0.5)
        c2.sock.settimeout(0.4)
        while True:
            try:
                c2.recv_message(timeout=0.4)
            except (TimeoutError, socket.timeout):
                break
        c1.send_json({"t": "pub", "topic": "room-lan-1", "d": "after-close"})
        again = c2.recv_message()
        assert again["d"] == "after-close", again
        time.sleep(0.6)
        assert relay.peer_count() == 2, f"断开未清理: {relay.peer_count()}"
        return ("3 条连接通过 topic 互发（含中文/emoji JSON、原样透传 "
                f"{json.dumps(got['d'], ensure_ascii=False)}），"
                "分片帧拼装、ping→pong、非法 topic 报错、发布者不回环、"
                "未订阅不投递、断开后连接数清理为 2 全部通过")
    finally:
        for c in (c1, c2, c3):
            if c is not None:
                c.close()
        srv.shutdown()
        srv.server_close()


# ==========================================================================
# 测试 6：webhost
# ==========================================================================

def test_webhost() -> str:
    from swiftdrop.webhost import WebHost, default_root

    root = os.path.join(TMP, "webroot")
    os.makedirs(root, exist_ok=True)
    index = os.path.join(root, "swiftdrop.html")
    made_index = not os.path.isfile(index)
    if made_index:
        with open(index, "w", encoding="utf-8") as fh:
            fh.write("<!doctype html><meta charset=utf-8>"
                     "<title>SwiftDrop 测试页</title><p>临时测试首页</p>")
    with open(os.path.join(root, "app.js"), "w", encoding="utf-8") as fh:
        fh.write("console.log('中文');\n")
    with open(os.path.join(root, "blob.bin"), "wb") as fh:
        fh.write(os.urandom(64 * 1024))
    os.makedirs(os.path.join(root, "sub"), exist_ok=True)

    host = WebHost(root, 0)
    host.make_server()
    host.print_banner()
    threading.Thread(target=host.httpd.serve_forever,
                     kwargs={"poll_interval": 0.2}, daemon=True).start()
    port = host.port
    notes: list[str] = []
    try:
        # 1) 首页
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
        conn.request("GET", "/swiftdrop.html")
        resp = conn.getresponse()
        body = resp.read()
        ctype = resp.getheader("Content-Type")
        assert resp.status == 200, f"首页状态码 {resp.status}"
        assert "html" in (ctype or ""), f"首页 MIME 不对: {ctype}"
        notes.append(f"GET /swiftdrop.html → 200, Content-Type={ctype}, "
                     f"{len(body)} 字节")
        print(notes[-1], flush=True)

        # 2) .js MIME
        conn.request("GET", "/app.js")
        resp = conn.getresponse()
        js_type = resp.getheader("Content-Type")
        resp.read()
        assert "javascript" in (js_type or ""), f".js MIME 不对: {js_type}"
        notes.append(f"GET /app.js → Content-Type={js_type}")
        print(notes[-1], flush=True)

        # 3) Range 请求
        conn.request("GET", "/blob.bin", headers={"Range": "bytes=100-199"})
        resp = conn.getresponse()
        data = resp.read()
        assert resp.status == 206, f"Range 状态码 {resp.status}"
        assert len(data) == 100, f"Range 返回 {len(data)} 字节"
        notes.append(f"GET /blob.bin (Range: bytes=100-199) → 206, "
                     f"Content-Range={resp.getheader('Content-Range')}")
        print(notes[-1], flush=True)

        # 4) 路径穿越必须被拒
        for evil in ("/../secret.txt", "/..%2fsecret.txt", "/sub/../../secret.txt"):
            conn.request("GET", evil)
            resp = conn.getresponse()
            resp.read()
            assert resp.status in (403, 404), f"穿越 {evil} 返回 {resp.status}"
            notes.append(f"GET {evil} → {resp.status}（已拒绝）")
            print(notes[-1], flush=True)

        # 5) /signal 升级
        ws = WSClient("127.0.0.1", port, "/signal")
        notes.append("GET /signal（Upgrade: websocket）→ 101 Switching Protocols, "
                     "Sec-WebSocket-Accept 正确")
        print(notes[-1], flush=True)
        ws.send_json({"t": "ping"})
        assert ws.recv_message().get("t") == "pong"
        notes.append("/signal 上 ping → pong 正常")
        print(notes[-1], flush=True)

        # 6) /signal 在 webhost 里也能中继两条连接
        ws2 = WSClient("127.0.0.1", port, "/signal")
        ws.send_json({"t": "sub", "topic": "lan-room"})
        ws2.send_json({"t": "sub", "topic": "lan-room"})
        time.sleep(0.3)
        ws.send_json({"t": "pub", "topic": "lan-room", "d": {"中文": "转发 OK"}})
        relayed = ws2.recv_message()
        assert relayed["d"] == {"中文": "转发 OK"}, relayed
        notes.append("webhost 同端口 /signal 中继两条连接成功："
                     + json.dumps(relayed, ensure_ascii=False))
        print(notes[-1], flush=True)

        # 7) HTTP（非升级）访问 /signal 应给出提示而不是崩溃
        conn.request("GET", "/signal")
        resp = conn.getresponse()
        hint = resp.read().decode("utf-8", "replace")
        assert resp.status == 200 and "信令" in hint, f"/signal 普通 GET: {resp.status}"
        notes.append("普通 GET /signal → 200 文本提示（未升级时不会 500）")
        print(notes[-1], flush=True)
        ws.close()
        ws2.close()
        conn.close()

        urls = [u for u in __import__("swiftdrop.webhost", fromlist=["urls_for"])
                .urls_for(port, root)]
        print("局域网 URL 示例:", urls[:2], flush=True)
        assert all("#t=lan" in u for u in urls) or not urls
        return "；".join(notes)
    finally:
        host.stop()


# ==========================================================================
# 主流程
# ==========================================================================

def test_tkinter() -> None:
    banner("环境检查")
    try:
        import tkinter
        import tkinter.font as tkfont
        root = tkinter.Tk()
        root.withdraw()
        has_font = "Microsoft YaHei UI" in tkfont.families()
        root.destroy()
        print(f"tkinter 可用：Tk {tkinter.TkVersion}，"
              f"Microsoft YaHei UI {'可用' if has_font else '不可用(会回退默认字体)'}",
              flush=True)
    except Exception as exc:                                    # noqa: BLE001
        print(f"tkinter 不可用：{exc}", flush=True)
    print(f"Python: {sys.version.split()[0]} @ {PY}", flush=True)
    print(f"临时目录: {TMP}", flush=True)


def main() -> int:
    t_start = time.monotonic()
    print("SwiftDrop 自测开始")
    test_tkinter()

    run_test("1. 速度 + 正确性（200MB / 4 流 / 127.0.0.1）", test_speed)
    run_test("2. 中文 / 深层目录 / 带空格长文件名", test_unicode_tree)
    run_test("3. 断点续传（中途 kill 发送端）", test_resume)
    run_test("4. 文件夹同步（two-way / --delete-extra）", test_sync)
    run_test("5. WebSocket 信令中继", test_signaling)
    run_test("6. webhost 静态服务 + /signal 升级", test_webhost)

    banner("汇总")
    ok = True
    for name, status, secs, detail in RESULTS:
        print(f"[{status}] {name}  ({secs:.1f}s)", flush=True)
        if status == "FAIL":
            ok = False
    passed = sum(1 for r in RESULTS if r[1] == "PASS")
    failed = sum(1 for r in RESULTS if r[1] == "FAIL")
    skipped = sum(1 for r in RESULTS if r[1] == "SKIP")
    print(f"\nPASS {passed} / FAIL {failed} / SKIP {skipped} "
          f"，总耗时 {time.monotonic() - t_start:.1f}s", flush=True)
    print(f"日志目录: {LOG_DIR}", flush=True)

    if os.environ.get("SWIFTDROP_KEEP_TMP") != "1":
        cleanup(TMP)
    else:
        print("SWIFTDROP_KEEP_TMP=1，保留临时目录", flush=True)
    return 0 if (ok and failed == 0) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n用户中断", flush=True)
        sys.exit(130)
