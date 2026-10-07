"""局域网 WebSocket 信令中继（RFC6455，纯标准库）。

给「同一局域网、完全没有外网」的网页版当信令服务器用。协议**精确**照下面实现：

握手
    ``GET /signal``，校验 ``Sec-WebSocket-Key``，回
    ``101 Switching Protocols`` + ``Sec-WebSocket-Accept``。

帧
    * 客户端 → 服务端：必须带掩码，解掩码后再解析。
    * 服务端 → 客户端：**不加密/不加掩码**。
    * 支持分片（continuation，opcode 0x0）拼装，单帧最大 1MB。
    * 支持 ping / pong / close 控制帧（控制帧不可分片、负载 ≤125 字节）。

文本帧内容（JSON）
    客户端 → 服务端::

        {"t":"sub","topic":"<字符串>"}
        {"t":"pub","topic":"<字符串>","d":<任意 JSON>}
        {"t":"ping"}

    服务端 → 客户端::

        {"t":"msg","topic":"<字符串>","d":<任意 JSON>}   # 转发给订阅该 topic 的其他连接
        {"t":"pong"}
        {"t":"err","m":"..."}

    * 一个连接可订阅多个 topic；
    * ``d`` 原样透传（不解析语义）；
    * topic 只允许 ``[A-Za-z0-9_\\-]{1,128}``；
    * 不要求 TLS，只监听局域网。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import socket
import struct
import threading
import time
from collections.abc import Callable

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TOPIC_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
MAX_FRAME = 1024 * 1024        # 单帧 1MB
MAX_MESSAGE = 4 * 1024 * 1024  # 分片拼装后的消息上限

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(Exception):
    """握手/帧层错误。"""


# --------------------------------------------------------------------------
# 握手
# --------------------------------------------------------------------------

def accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def is_websocket_upgrade(headers) -> bool:
    """判断 HTTP 头是否是一次 WebSocket 升级请求（headers 为 email.message）。"""
    try:
        upgrade = (headers.get("Upgrade") or "").lower()
        connection = (headers.get("Connection") or "").lower()
    except AttributeError:
        return False
    return "websocket" in upgrade and "upgrade" in connection


def handshake_response(client_key: str) -> bytes:
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_key(client_key)}\r\n"
        "\r\n"
    ).encode("ascii")


def handshake_client(sock: socket.socket, host: str, path: str = "/signal",
                     timeout: float = 10.0) -> bytes:
    """测试/CLI 用的客户端握手，返回服务端响应原文。"""
    key = base64.b64encode(bytes(range(16))).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode("ascii")
    sock.sendall(req)
    buf = b""
    sock.settimeout(timeout)
    while b"\r\n\r\n" not in buf:
        piece = sock.recv(4096)
        if not piece:
            break
        buf += piece
    head, _, rest = buf.partition(b"\r\n\r\n")
    if rest:                                   # 极少数情况响应后跟了帧
        raise WebSocketError("握手响应后带数据，请用 WebSocketPeer 接管")
    if b"101" not in head.split(b"\r\n")[0]:
        raise WebSocketError(f"握手失败: {head!r}")
    return head


# --------------------------------------------------------------------------
# 帧编解码
# --------------------------------------------------------------------------

def encode_frame(payload: bytes, opcode: int = OP_TEXT, *,
                 fin: bool = True, mask: bool = False,
                 mask_key: bytes | None = None) -> bytes:
    """按 RFC6455 编码一帧。服务端发送用 mask=False。"""
    b0 = (0x80 if fin else 0x00) | (opcode & 0x0F)
    length = len(payload)
    out = bytearray([b0])
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        out.append(mask_bit | length)
    elif length < 65536:
        out.append(mask_bit | 126)
        out += struct.pack(">H", length)
    else:
        out.append(mask_bit | 127)
        out += struct.pack(">Q", length)
    if mask:
        key = mask_key or b"\x00\x00\x00\x00"
        out += key
        out += bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    else:
        out += payload
    return bytes(out)


def encode_text(text: str, mask: bool = False) -> bytes:
    return encode_frame(text.encode("utf-8"), OP_TEXT, mask=mask)


def encode_json(obj, mask: bool = False) -> bytes:
    return encode_text(json.dumps(obj, ensure_ascii=False), mask=mask)


class WebSocketPeer:
    """一条已升级的 WebSocket 连接（阻塞式读写）。"""

    def __init__(self, sock: socket.socket, addr=("", 0), *,
                 max_frame: int = MAX_FRAME, timeout: float = 60.0):
        self.sock = sock
        self.addr = addr
        self.max_frame = max_frame
        self.timeout = timeout
        self.subs: set[str] = set()
        self.alive = True
        self._send_lock = threading.Lock()
        self._frag_op: int | None = None
        self._frag_buf = bytearray()
        self._pending: list[bytes] = []      # 待发的控制帧
        try:
            sock.settimeout(timeout)
        except OSError:
            pass

    # -- 读 ---------------------------------------------------------------
    def _read_exact(self, n: int) -> bytes:
        buf = bytearray(n)
        view = memoryview(buf)
        got = 0
        while got < n:
            k = self.sock.recv_into(view[got:], n - got)
            if not k:
                raise WebSocketError("连接已关闭")
            got += k
        return bytes(buf)

    def _read_frame(self) -> tuple[bool, int, bytes]:
        head = self._read_exact(2)
        b0, b1 = head[0], head[1]
        fin = bool(b0 & 0x80)
        rsv = b0 & 0x70
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if rsv:
            raise WebSocketError("RSV 位非零，不支持扩展")
        if length == 126:
            (length,) = struct.unpack(">H", self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack(">Q", self._read_exact(8))
        if length > self.max_frame:
            raise WebSocketError(f"帧过大: {length} > {self.max_frame}")
        if opcode >= OP_CLOSE and (not fin or length > 125):
            raise WebSocketError("控制帧不能分片且负载 ≤125 字节")
        key = self._read_exact(4) if masked else None
        payload = self._read_exact(length) if length else b""
        if masked:
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    # -- 写 ---------------------------------------------------------------
    def send_bytes(self, data: bytes) -> None:
        if not self.alive:
            return
        with self._send_lock:
            try:
                self.sock.sendall(data)          # 服务端→客户端：不加掩码
            except OSError:
                self.alive = False

    def send_text(self, text: str) -> None:
        self.send_bytes(encode_text(text, mask=False))

    def send_json(self, obj) -> None:
        self.send_bytes(encode_json(obj, mask=False))

    def send_error(self, message: str) -> None:
        self.send_json({"t": "err", "m": message})

    def send_pong(self, payload: bytes = b"") -> None:
        self.send_bytes(encode_frame(payload, OP_PONG, mask=False))

    def send_ping(self, payload: bytes = b"") -> None:
        self.send_bytes(encode_frame(payload, OP_PING, mask=False))

    def close(self, code: int = 1000, reason: str = "") -> None:
        if not self.alive:
            return
        payload = struct.pack(">H", code) + reason.encode("utf-8")[:123]
        self.send_bytes(encode_frame(payload, OP_CLOSE, mask=False))
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    # -- 消息 -------------------------------------------------------------
    def recv_message(self, deadline: float | None = None) -> tuple[str, str | bytes]:
        """返回 ``(kind, data)``，kind ∈ {"text","binary"}。

        自动处理：continuation 拼装、ping→pong、pong 忽略、close→断开。
        """
        while True:
            if deadline is not None:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    raise socket.timeout("等待消息超时")
                self.sock.settimeout(remain)
            fin, opcode, payload = self._read_frame()

            if opcode == OP_PING:
                self.send_pong(payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self.alive = False
                try:
                    self.send_bytes(encode_frame(payload[:125], OP_CLOSE, mask=False))
                except OSError:
                    pass
                raise WebSocketError("对端关闭了连接")

            if opcode == OP_CONT:
                if self._frag_op is None:
                    raise WebSocketError("收到无起始帧的 continuation")
                self._frag_buf += payload
                if len(self._frag_buf) > MAX_MESSAGE:
                    raise WebSocketError("分片消息过大")
                if not fin:
                    continue
                op, data = self._frag_op, bytes(self._frag_buf)
                self._frag_op, self._frag_buf = None, bytearray()
            elif opcode in (OP_TEXT, OP_BINARY):
                if self._frag_op is not None:
                    raise WebSocketError("上一条分片消息未结束")
                if fin:
                    op, data = opcode, payload
                else:
                    self._frag_op = opcode
                    self._frag_buf = bytearray(payload)
                    continue
            else:
                raise WebSocketError(f"未知 opcode: {opcode}")

            return ("text" if op == OP_TEXT else "binary"), data


# --------------------------------------------------------------------------
# 中继
# --------------------------------------------------------------------------

class SignalRelay:
    """topic 订阅/发布中继，可挂到任意 accept 循环后面。

    ``serve_peer(peer)`` 在一个线程里跑完这条连接的生命周期。
    """

    def __init__(self, on_log: Callable[[str], None] | None = None,
                 *, max_peers: int = 256):
        self._peers: set[WebSocketPeer] = set()
        self._topics: dict[str, set[WebSocketPeer]] = {}
        self._lock = threading.RLock()
        self._counter = 0
        self.on_log = on_log
        self.max_peers = max_peers
        self.stats = {"connections": 0, "messages": 0, "dropped": 0}

    # -- 日志 -------------------------------------------------------------
    def log(self, msg: str) -> None:
        if self.on_log:
            try:
                self.on_log(msg)
            except Exception:
                pass

    # -- 连接管理 ---------------------------------------------------------
    def register(self, peer: WebSocketPeer) -> int:
        with self._lock:
            if len(self._peers) >= self.max_peers:
                peer.send_error("服务器连接数已满")
                peer.close(1013, "overloaded")
                raise WebSocketError("连接数已满")
            self._counter += 1
            cid = self._counter
            self._peers.add(peer)
            self.stats["connections"] += 1
        self.log(f"信令连接 #{cid} 建立 {peer.addr}")
        return cid

    def unregister(self, peer: WebSocketPeer) -> None:
        with self._lock:
            self._peers.discard(peer)
            for topic in list(peer.subs):
                subs = self._topics.get(topic)
                if subs:
                    subs.discard(peer)
                    if not subs:
                        self._topics.pop(topic, None)
            peer.subs.clear()

    def subscribe(self, peer: WebSocketPeer, topic: str) -> None:
        with self._lock:
            peer.subs.add(topic)
            self._topics.setdefault(topic, set()).add(peer)

    def publish(self, peer: WebSocketPeer, topic: str, data) -> int:
        """转发给订阅了该 topic 的**其他**连接，返回送达数。"""
        with self._lock:
            targets = [p for p in self._topics.get(topic, ())
                       if p is not peer and p.alive]
        frame = encode_json({"t": "msg", "topic": topic, "d": data}, mask=False)
        sent = 0
        for target in targets:
            target.send_bytes(frame)
            if target.alive:
                sent += 1
            else:
                self.stats["dropped"] += 1
        self.stats["messages"] += 1
        return sent

    def broadcast(self, obj) -> None:
        frame = encode_json(obj, mask=False)
        with self._lock:
            peers = list(self._peers)
        for peer in peers:
            peer.send_bytes(frame)

    def peer_count(self) -> int:
        with self._lock:
            return len(self._peers)

    def topic_count(self) -> int:
        with self._lock:
            return len(self._topics)

    def shutdown(self) -> None:
        with self._lock:
            peers = list(self._peers)
        for peer in peers:
            peer.close(1001, "server shutdown")

    # -- 单条连接 ---------------------------------------------------------
    def serve_peer(self, peer: WebSocketPeer) -> None:
        """阻塞处理一条连接直到断开。异常都会被吞掉并清理订阅。"""
        cid = None
        try:
            cid = self.register(peer)
        except WebSocketError:
            return
        try:
            while peer.alive:
                kind, data = peer.recv_message()
                if kind != "text":
                    peer.send_error("只支持文本帧（JSON）")
                    continue
                try:
                    msg = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    peer.send_error("非法 JSON")
                    continue
                if not isinstance(msg, dict):
                    peer.send_error("消息必须是 JSON 对象")
                    continue
                self.handle_message(peer, msg)
        except (WebSocketError, socket.timeout, OSError) as exc:
            if peer.alive:
                self.log(f"信令连接 #{cid} 结束: {exc}")
        finally:
            self.unregister(peer)
            peer.alive = False
            try:
                peer.sock.close()
            except OSError:
                pass
            self.log(f"信令连接 #{cid} 清理完毕，剩余 {self.peer_count()} 条")

    def handle_message(self, peer: WebSocketPeer, msg: dict) -> None:
        t = msg.get("t")
        if t == "sub":
            topic = msg.get("topic")
            if not isinstance(topic, str) or not TOPIC_RE.match(topic):
                peer.send_error("topic 非法（只允许 [A-Za-z0-9_-]{1,128}）")
                return
            self.subscribe(peer, topic)
            self.log(f"{peer.addr} 订阅 {topic}")
            return
        if t == "pub":
            topic = msg.get("topic")
            if not isinstance(topic, str) or not TOPIC_RE.match(topic):
                peer.send_error("topic 非法（只允许 [A-Za-z0-9_-]{1,128}）")
                return
            if "d" not in msg:
                peer.send_error("pub 缺少 d 字段")
                return
            sent = self.publish(peer, topic, msg["d"])
            self.log(f"{peer.addr} 发布 {topic} → 送达 {sent} 条")
            return
        if t == "ping":
            peer.send_json({"t": "pong"})
            return
        peer.send_error(f"未知消息类型: {t!r}")


def make_peer(sock: socket.socket, addr, **kw) -> WebSocketPeer:
    return WebSocketPeer(sock, addr, **kw)
