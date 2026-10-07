"""棕仙的传输软件 帧协议 / 握手 / 校验 / 断点续传状态。

线上格式（TCP，长度前缀帧）::

    +----------------+---------------------------+
    | 4 字节大端长度  | 负载（length 字节）        |
    +----------------+---------------------------+

    纯 JSON 帧：负载就是 UTF-8 的 JSON 文本（必带 ``type`` 字段），
    握手、manifest、file_begin、file_end、done、error 都走这一种；
    ``send_json`` / ``recv_json`` 与之对应。

    复合帧（带二进制块）::

        +----------+----------+-------------+-------------+
        | 4B 总长  | 4B 头长  | JSON 头     | 二进制数据   |
        +----------+----------+-------------+-------------+
        总长 = 4 + 头长 + 数据长度（即「总长」字段之后的所有字节）

    ``send_chunk`` / ``recv_frame`` 与之对应。分片数据只走复合帧，
    所以 4MB 的分片可以和它的 JSON 头一次 ``sendall`` 出去，零拷贝。

    接收端第一条业务消息可能是纯 JSON（manifest）也可能是复合帧（pull），
    用 :func:`recv_any` 可以自动区分。

握手消息类型：
    hello / hello_ok                  协议版本与设备名协商
    manifest / manifest_ok            待传文件列表
    file_begin / file_begin_ok        单文件开始（含接收端续传位图）
    file_end / file_ack               单文件结束（含 SHA-256 校验结论）
    pull / chunk                      接收端拉取分片 / 发送端回送分片
    file_skip                         文件已一致，跳过
    done / error                      传输结束 / 错误

所有 JSON 用 UTF-8；``ensure_ascii=False`` 保证中文/emoji 在抓包时也可读
（JSON 本身仍可安全解码）。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import struct
import threading
import time

from . import MAGIC, VERSION

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

PROTOCOL_VERSION = 1
DATA_PORT = 45880

CHUNK_SIZE = 4 * 1024 * 1024          # 4MB 分片
DEFAULT_STREAMS = 4
MAX_STREAMS = 8
STREAM_SOCKET_BUFFER = 4 * 1024 * 1024  # 每流内核缓冲（千兆网靠它吃满）
MAX_JSON_FRAME = 16 * 1024 * 1024     # 单个 JSON 帧上限
MAX_CHUNK_SIZE = 64 * 1024 * 1024     # --chunk-mb 允许的最大分片
MAX_CHUNK_FRAME = MAX_CHUNK_SIZE + 1024 * 1024   # 单个复合帧上限（分片 + JSON 头）
STATE_FILENAME = ".swiftdrop-state.json"
PART_SUFFIX = ".part"

# 需要忽略的路径片段 / 文件名（同步与发送共用）
IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".svn", ".hg"}
IGNORE_PATTERNS = (STATE_FILENAME, "*.part", "~$*", ".DS_Store", "Thumbs.db")


class ProtocolError(Exception):
    """协议级错误（对端不按规矩说话）。"""


class ConnectionClosed(ProtocolError):
    """对端关闭连接。"""


# --------------------------------------------------------------------------
# 基础读写
# --------------------------------------------------------------------------

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """读满 n 字节，短读就继续读；对端关闭抛 ConnectionClosed。"""
    if n == 0:
        return b""
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        try:
            k = sock.recv_into(view[got:], n - got)
        except (ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            raise ConnectionClosed(f"连接中断: {exc}") from exc
        if not k:
            raise ConnectionClosed("对端关闭了连接")
        got += k
    return bytes(buf)


def send_json(sock: socket.socket, obj: dict) -> None:
    """发送一个纯 JSON 帧。"""
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_JSON_FRAME:
        raise ProtocolError(f"JSON 帧过大: {len(payload)}")
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def recv_json(sock: socket.socket, max_len: int = MAX_JSON_FRAME) -> dict:
    """接收一个纯 JSON 帧。"""
    (length,) = struct.unpack(">I", _recv_exact(sock, 4))
    if length > max_len:
        raise ProtocolError(f"帧长度超限: {length}")
    raw = _recv_exact(sock, length)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"非法 JSON 帧: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("帧内容必须是 JSON 对象")
    return obj


def send_chunk(sock: socket.socket, obj: dict, data: bytes) -> None:
    """发送复合帧：``4B总长 | 4B头长 | JSON 头 | 二进制块``。

    "总长" 覆盖它自己的 4 字节之后的所有内容（即 4B头长 + 头 + 数据），
    与 :func:`recv_frame` 严格对称。``data`` 允许为空（pull 这类纯控制消息
    也走复合帧，接收端因此只需要一个解析函数）。
    """
    header = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if len(header) + len(data) + 4 > MAX_CHUNK_FRAME:
        raise ProtocolError(f"chunk 过大: {len(header) + len(data)}")
    total = 4 + len(header) + len(data)
    sock.sendall(struct.pack(">II", total, len(header)) + header + data)


def recv_frame(sock: socket.socket) -> tuple[dict, bytes]:
    """接收复合帧，返回 (JSON 头, 数据)；数据允许为空。"""
    total, hlen = struct.unpack(">II", _recv_exact(sock, 8))
    if total < 4 or total > MAX_CHUNK_FRAME:
        raise ProtocolError(f"复合帧总长非法: {total}")
    if hlen > total - 4 or hlen > MAX_JSON_FRAME:
        raise ProtocolError(f"复合帧头长非法: hlen={hlen} total={total}")
    raw = _recv_exact(sock, hlen)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"非法帧头 JSON: {exc}") from exc
    rest = total - 4 - hlen
    data = _recv_exact(sock, rest) if rest else b""
    return obj, data


# 兼容旧名字：语义与 recv_frame 完全一致
recv_chunk = recv_frame


def recv_message(sock: socket.socket) -> tuple[dict, bytes]:
    """:func:`recv_frame` 的别名（历史入口，行为相同）。"""
    return recv_frame(sock)


def recv_any(sock: socket.socket) -> tuple[dict, bytes]:
    """自动识别「纯 JSON 帧」与「复合帧」。

    两种格式在线上都以 4 字节大端长度开头，但长度含义不同，用 peek 区分：

    * 纯 JSON 帧：``长度 == 紧随其后 JSON 的字节数``
    * 复合帧  ：``长度 == 4 + 头长 + 数据长度``

    客户端握手后第一条消息可能是 ``pull`` 复合帧，也可能是 ``manifest`` 纯
    JSON 帧，所以接收端需要这个函数。
    """
    head = _recv_exact(sock, 4)
    (length,) = struct.unpack(">I", head)
    if length == 0 or length > MAX_CHUNK_FRAME:
        raise ProtocolError(f"帧长度非法: {length}")
    body = _recv_exact(sock, length)
    # 复合帧？body 前 4 字节是头长，且 4+头长 ≤ 总长
    if len(body) >= 4:
        (hlen,) = struct.unpack(">I", body[:4])
        if 0 < hlen <= len(body) - 4 and hlen <= MAX_JSON_FRAME:
            try:
                obj = json.loads(body[4:4 + hlen].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                obj = None
            if isinstance(obj, dict):
                return obj, body[4 + hlen:]
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"无法识别的帧: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("帧内容必须是 JSON 对象")
    return obj, b""


# --------------------------------------------------------------------------
# 握手
# --------------------------------------------------------------------------

def hello_message(name: str, port: int) -> dict:
    return {
        "type": "hello",
        "magic": MAGIC,
        "ver": VERSION,
        "protocol": PROTOCOL_VERSION,
        "name": name,
        "port": port,
        "os": "windows",
    }


def check_hello(msg: dict) -> dict:
    """校验对端 hello，返回校验后的消息。"""
    if msg.get("type") != "hello":
        raise ProtocolError(f"期望 hello，收到 {msg.get('type')!r}")
    if msg.get("magic") != MAGIC:
        raise ProtocolError("magic 不匹配，可能不是本程序的服务")
    proto = int(msg.get("protocol", 0))
    if proto != PROTOCOL_VERSION:
        raise ProtocolError(f"协议版本不一致: 本机 {PROTOCOL_VERSION} / 对端 {proto}")
    return msg


def handshake_server(sock: socket.socket, my_name: str, my_port: int) -> dict:
    """接收端一侧：读 hello，回 hello_ok，返回对端 hello。"""
    peer = check_hello(recv_json(sock))
    send_json(sock, {
        "type": "hello_ok",
        "magic": MAGIC, "ver": VERSION, "protocol": PROTOCOL_VERSION,
        "name": my_name, "port": my_port, "os": "windows",
    })
    return peer


def handshake_client(sock: socket.socket, my_name: str, my_port: int,
                     timeout: float = 20.0) -> dict:
    """发送端一侧：发 hello，读 hello_ok，返回对端 hello_ok。"""
    sock.settimeout(timeout)
    send_json(sock, hello_message(my_name, my_port))
    resp = recv_json(sock)
    if resp.get("type") != "hello_ok":
        raise ProtocolError(f"期望 hello_ok，收到 {resp.get('type')!r}")
    if resp.get("magic") != MAGIC:
        raise ProtocolError("magic 不匹配，可能不是本程序的服务")
    sock.settimeout(None)
    return resp


def tune_socket(sock: socket.socket, bufsize: int = STREAM_SOCKET_BUFFER) -> None:
    """放大收发缓冲并关闭 Nagle，否则千兆网跑不满。"""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, bufsize)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, bufsize)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------

def chunk_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: str, chunk_size: int = 1024 * 1024,
                progress=None) -> str:
    """整文件 SHA-256。progress(done_bytes) 可选回调。"""
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            h.update(block)
            done += len(block)
            if progress is not None:
                progress(done)
    return h.hexdigest()


def quick_signature(path: str, size: int | None = None) -> str:
    """(大小 + 头尾各 256KB) 的廉价指纹，用来在懒校验时快速排除大文件差异。"""
    st = os.stat(path)
    size = st.st_size if size is None else size
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(path, "rb") as fh:
        head = fh.read(256 * 1024)
        h.update(head)
        if size > 512 * 1024:
            fh.seek(max(0, size - 256 * 1024))
            h.update(fh.read(256 * 1024))
    return h.hexdigest()


def is_safe_relpath(relpath: str) -> bool:
    """拒绝 ``..``、绝对路径、盘符、UNC、空路径等穿越/非法形式。"""
    if not relpath or not isinstance(relpath, str):
        return False
    if "\x00" in relpath:
        return False
    if relpath.startswith(("/", "\\")):
        return False
    if len(relpath) >= 2 and relpath[1] == ":":
        return False
    normalized = relpath.replace("\\", "/")
    if normalized.startswith("//"):
        return False
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if not parts:
        return False
    for part in parts:
        if part == "..":
            return False
    return True


def resolve_under(root: str, relpath: str) -> str:
    """把相对路径安全地拼到 root 下，越界直接抛错。"""
    if not is_safe_relpath(relpath):
        raise ProtocolError(f"非法相对路径（疑似路径穿越）: {relpath!r}")
    root_abs = os.path.abspath(root)
    target = os.path.abspath(os.path.join(root_abs, relpath.replace("/", os.sep)))
    if target != root_abs and not target.startswith(root_abs + os.sep):
        raise ProtocolError(f"路径越界: {relpath!r}")
    return target


# --------------------------------------------------------------------------
# 断点续传状态（.swiftdrop-state.json）
# --------------------------------------------------------------------------

class ResumeState:
    """接收目录下的续传状态。线程安全，支持原子落盘。

    结构::

        {"ver":1,"magic":"SWIFTDROP1","files":{
            "<relpath>":{"size":n,"mtime":ts,"chunks":n,"have":[0,1,...],
                         "sha256":"...","done":false}}}
    """

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.path = os.path.join(self.root, STATE_FILENAME)
        self._lock = threading.RLock()
        self._files: dict[str, dict] = {}
        self._dirty = False
        self.load()

    # -- 读写 -------------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            self._files = {}
            if not os.path.isfile(self.path):
                return
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                return
            if data.get("magic") not in (None, MAGIC) and data.get("ver") != 1:
                return
            files = data.get("files")
            if isinstance(files, dict):
                for k, v in files.items():
                    if isinstance(v, dict):
                        self._files[k] = {
                            "size": int(v.get("size", 0)),
                            "mtime": float(v.get("mtime", 0.0)),
                            "chunks": int(v.get("chunks", 0)),
                            "have": sorted({int(c) for c in v.get("have", [])}),
                            "sha256": v.get("sha256") or None,
                            "done": bool(v.get("done", False)),
                        }

    def save(self, force: bool = False) -> None:
        with self._lock:
            if not self._dirty and not force:
                return
            payload = {
                "ver": 1, "magic": MAGIC, "app": "SwiftDrop", "version": VERSION,
                "updated": time.time(), "files": self._files,
            }
            tmp = self.path + ".tmp"
            os.makedirs(self.root, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            self._dirty = False

    # -- 查询 -------------------------------------------------------------
    def entry(self, relpath: str) -> dict | None:
        with self._lock:
            e = self._files.get(relpath)
            return dict(e) if e else None

    def snapshot(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._files.items()}

    def have_map(self, relpath: str, size: int, chunks: int) -> set[int]:
        """该文件已经完整落盘的分片集合（大小或分片数变了就当没有）。

        额外把状态里记录的 ``bytes`` 换算成「已确认区间」补进集合：
        这样即使某个大分片只写了一半就被中断，重启后也能少传一半。
        """
        with self._lock:
            e = self._files.get(relpath)
            if not e or e["size"] != size or e["chunks"] != chunks:
                return set()
            have = set(e["have"])
            confirmed = int(e.get("bytes") or 0)
        have = {c for c in have if 0 <= c < chunks}
        if confirmed > 0:
            full = confirmed // CHUNK_SIZE
            have |= set(range(min(full, chunks)))
        return have

    # -- 写入 -------------------------------------------------------------
    def begin(self, relpath: str, size: int, mtime: float, chunks: int) -> set[int]:
        with self._lock:
            have = self.have_map(relpath, size, chunks)
            confirmed = 0
            if have:
                confirmed = max((c + 1) * CHUNK_SIZE for c in have)
                confirmed = min(confirmed, size)
            self._files[relpath] = {
                "size": size, "mtime": mtime, "chunks": chunks,
                "have": sorted(have), "bytes": confirmed,
                "sha256": None, "done": False,
            }
            self._dirty = True
        return have

    def mark_chunk(self, relpath: str, index: int) -> None:
        with self._lock:
            e = self._files.get(relpath)
            if e is None:
                return
            if index not in e["have"]:
                e["have"].append(index)
                e["have"].sort()
                self._dirty = True

    def mark_bytes_confirmed(self, relpath: str, confirmed: int) -> None:
        """记录「从文件头部起连续确认写好的字节数」。

        大分片只写了一半就被 kill 时，重启后靠它就能少传那半片。
        只允许取「最小的已写位置 + 1 片」以内的值，乱序完成时不会误报。
        """
        with self._lock:
            e = self._files.get(relpath)
            if e is None:
                return
            have = sorted(int(c) for c in e.get("have", []))
            if have:
                confirmed = min(int(confirmed),
                                (have[0] + 1) * CHUNK_SIZE)
            else:
                confirmed = min(int(confirmed), CHUNK_SIZE)
            e["bytes"] = max(int(e.get("bytes") or 0), max(0, int(confirmed)))
            self._dirty = True

    def mark_done(self, relpath: str, sha256: str) -> None:
        with self._lock:
            e = self._files.get(relpath)
            if e is None:
                return
            e["have"] = list(range(e["chunks"]))
            e["bytes"] = e["size"]
            e["sha256"] = sha256
            e["done"] = True
            self._dirty = True

    def drop(self, relpath: str) -> None:
        with self._lock:
            if self._files.pop(relpath, None) is not None:
                self._dirty = True

    def completed_bytes(self) -> int:
        """已落盘字节数（用于报告续传跳过了多少）。"""
        total = 0
        with self._lock:
            for e in self._files.values():
                if e["chunks"] <= 0:
                    continue
                # 分片大小以 CHUNK_SIZE 近似（最后一片小于它，误差可忽略）
                total += sum(
                    CHUNK_SIZE if c < e["chunks"] - 1 else
                    max(0, e["size"] - CHUNK_SIZE * (e["chunks"] - 1))
                    for c in e["have"]
                )
        return total

    def reset(self) -> None:
        with self._lock:
            self._files = {}
            self._dirty = True


def chunks_for(size: int, chunk_size: int = CHUNK_SIZE) -> int:
    """分片数（空文件算 1 片，保证有 file_begin/file_end 流程）。"""
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须为正")
    return max(1, (size + chunk_size - 1) // chunk_size)


def human_bytes(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    n = float(n)
    for u in units:
        if abs(n) < 1024 or u == units[-1]:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024.0
    return f"{n:.1f} TB"


def human_time(seconds: float) -> str:
    if seconds is None or seconds != seconds or seconds < 0 or seconds == float("inf"):
        return "--:--"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


class SpeedMeter:
    """滑动窗口测速 + ETA，cli 与 gui 共用。"""

    def __init__(self, total: int, window: float = 3.0):
        self.total = max(0, total)
        self._window = window
        self._samples: list[tuple[float, int]] = []
        self._lock = threading.Lock()
        self._start = time.monotonic()
        self.done = 0

    def add(self, n: int) -> None:
        with self._lock:
            self.done += n
            now = time.monotonic()
            self._samples.append((now, self.done))
            cutoff = now - self._window
            while len(self._samples) > 2 and self._samples[0][0] < cutoff:
                self._samples.pop(0)

    def rate(self) -> float:
        with self._lock:
            if len(self._samples) < 2:
                return 0.0
            (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
            dt = t1 - t0
            return (b1 - b0) / dt if dt > 1e-6 else 0.0

    def eta(self) -> float:
        r = self.rate()
        if r <= 1e-6:
            return float("inf")
        return max(0.0, (self.total - self.done) / r)

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def bar(self, width: int = 28) -> str:
        frac = 1.0 if self.total <= 0 else min(1.0, self.done / self.total)
        filled = int(frac * width)
        return "#" * filled + "-" * (width - filled)

    def line(self, prefix: str = "") -> str:
        frac = 100.0 if self.total <= 0 else min(100.0, self.done * 100.0 / self.total)
        return (f"{prefix}[{self.bar()}] {frac:5.1f}%  "
                f"{human_bytes(self.done)}/{human_bytes(self.total)}  "
                f"{human_bytes(self.rate())}/s  ETA {human_time(self.eta())}")
