#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""棕仙的传输软件 —— 中继（relay）回退服务器（RFC6455，纯标准库，单文件）。

用途
    P2P 打洞失败（双方都在严格 NAT / 运营商大内网）时，网页端自动改走这台中继，
    由它把一方的 WebSocket 字节原样转发给另一方。只要这台机器有公网 IP 就一定能连上。

配对模型
    客户端连 ``/relay?room=<roomid>``，同一 ``roomid`` 的**前两个**连接配成一对，之后
    互相转发（收到谁的帧就原样转给另一个）。超过 ``--max-room``（默认 2）拒绝。
    一端断开时，另一端会收到一个 ``{"t":"bye"}`` 文本帧并被关闭。

安全
    中继**不解析、不存储**转发内容。它转发的是应用层字节（网页端已在上层用 AES-GCM
    加密，见 ``src/web/relay.js``）。中继只能看到：连接元数据（IP、时间、流量大小）和
    密文；看不到明文，也拿不到密钥。

部署一句话
    在 VPS 上：``python3 relay_server.py --host 0.0.0.0 --port 9443``
    记得防火墙放行 9443/tcp；生产建议用 nginx/caddy 反代加 wss://（TLS），
    因为浏览器里从 https 页面连纯 ws:// 会被当作混合内容拦截（见文件末尾注释）。

用法
    python relay_server.py --host 0.0.0.0 --port 9443 [--max-room 2] [--log]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import re
import socket
import struct
import threading
import time
from urllib.parse import parse_qs, urlsplit

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
ROOM_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

MAX_FRAME = 4 * 1024 * 1024        # 单帧上限 4MB（够加密后的控制消息/数据分片）
MAX_MESSAGE = 4 * 1024 * 1024      # 分片拼装后的消息上限
IDLE_TIMEOUT = 300                 # 空闲连接 5 分钟无上行数据则清理

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class RelayError(Exception):
    """握手/帧层错误。"""


def encode_frame(payload: bytes, opcode: int, *, fin: bool = True) -> bytes:
    """按 RFC6455 编码一帧（服务端→客户端：不加掩码）。"""
    b0 = (0x80 if fin else 0x00) | (opcode & 0x0F)
    n = len(payload)
    out = bytearray([b0])
    if n < 126:
        out.append(n)
    elif n < 65536:
        out.append(126)
        out += struct.pack(">H", n)
    else:
        out.append(127)
        out += struct.pack(">Q", n)
    out += payload
    return bytes(out)


class Peer:
    """一条已升级的 WebSocket 连接（阻塞式读帧 / 线程安全写帧）。"""

    def __init__(self, sock: socket.socket, addr, *,
                 max_frame: int = MAX_FRAME, max_message: int = MAX_MESSAGE,
                 timeout: float = IDLE_TIMEOUT):
        self.sock = sock
        self.addr = addr
        self.max_frame = max_frame
        self.max_message = max_message
        self.alive = True
        self.partner: "Peer | None" = None
        self.room: str | None = None
        self.id = 0
        self.last_active = time.time()
        self._lock = threading.Lock()
        try:
            sock.settimeout(timeout)
        except OSError:
            pass

    # -- 读 ---------------------------------------------------------------
    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RelayError("连接已关闭")
            buf += chunk
        return bytes(buf)

    def read_frame(self) -> tuple[bool, int, bytes]:
        head = self._read_exact(2)
        b0, b1 = head[0], head[1]
        fin = bool(b0 & 0x80)
        rsv = b0 & 0x70
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if rsv:
            raise RelayError("RSV 位非零，不支持扩展")
        if length == 126:
            (length,) = struct.unpack(">H", self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack(">Q", self._read_exact(8))
        if length > self.max_frame:
            raise RelayError("帧过大: %d > %d" % (length, self.max_frame))
        if opcode >= OP_CLOSE and (not fin or length > 125):
            raise RelayError("控制帧不能分片且负载 ≤125 字节")
        key = self._read_exact(4) if masked else None
        payload = self._read_exact(length) if length else b""
        if masked and key:
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def read_message(self) -> tuple[str, bytes]:
        """返回 (kind, data)，kind ∈ {"text","binary"}。自动处理分片/ping/close。"""
        frag_op: int | None = None
        frag_buf = bytearray()
        while True:
            fin, opcode, payload = self.read_frame()
            if opcode == OP_PING:
                self.send_frame(payload, OP_PONG)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                try:
                    self.send_frame(payload[:125], OP_CLOSE)
                except Exception:
                    pass
                self.alive = False
                raise RelayError("对端关闭了连接")
            if opcode == OP_CONT:
                if frag_op is None:
                    raise RelayError("收到无起始帧的 continuation")
                frag_buf += payload
                if len(frag_buf) > self.max_message:
                    raise RelayError("分片消息过大")
                if not fin:
                    continue
                kind = "text" if frag_op == OP_TEXT else "binary"
                data = bytes(frag_buf)
                frag_op, frag_buf = None, bytearray()
                return kind, data
            if opcode in (OP_TEXT, OP_BINARY):
                if frag_op is not None:
                    raise RelayError("上一条分片消息未结束")
                if fin:
                    return ("text" if opcode == OP_TEXT else "binary"), payload
                frag_op = opcode
                frag_buf = bytearray(payload)
                continue
            raise RelayError("未知 opcode: %r" % opcode)

    # -- 写 ---------------------------------------------------------------
    def send_bytes(self, data: bytes) -> None:
        if not self.alive:
            return
        with self._lock:
            try:
                self.sock.sendall(data)          # 服务端→客户端：不加掩码
            except OSError:
                self.alive = False

    def send_frame(self, payload: bytes, opcode: int) -> None:
        self.send_bytes(encode_frame(payload, opcode))

    def send_text(self, text: str) -> None:
        self.send_frame(text.encode("utf-8"), OP_TEXT)

    def send_close(self, code: int = 1000, reason: str = "") -> None:
        if not self.alive:
            return
        payload = struct.pack(">H", code) + reason.encode("utf-8")[:123]
        self.send_bytes(encode_frame(payload, OP_CLOSE))
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class Relay:
    """房间配对中继：同一 room 的前两个连接互转。"""

    def __init__(self, max_room: int = 2, log_enabled: bool = False):
        self.max_room = max_room
        self.log_enabled = log_enabled
        self._rooms: dict[str, list[Peer]] = {}
        self._peers: set[Peer] = set()
        self._lock = threading.RLock()
        self._counter = 0
        self.stats = {"connections": 0, "frames": 0, "bytes": 0}

    def log(self, msg: str) -> None:
        if self.log_enabled:
            try:
                print("[relay] %s" % msg, flush=True)
            except Exception:
                pass

    def room_size(self, room: str) -> int:
        with self._lock:
            return len(self._rooms.get(room, []))

    def room_full(self, room: str) -> bool:
        with self._lock:
            return len(self._rooms.get(room, [])) >= self.max_room

    def register(self, peer: Peer, room: str) -> bool:
        with self._lock:
            lst = self._rooms.setdefault(room, [])
            if len(lst) >= self.max_room:
                return False
            self._counter += 1
            peer.id = self._counter
            peer.room = room
            lst.append(peer)
            self._peers.add(peer)
            if len(lst) == 2:                    # 前两个配成一对
                lst[0].partner = lst[1]
                lst[1].partner = lst[0]
            self.stats["connections"] += 1
            return True

    def disconnect(self, peer: Peer) -> None:
        partner: Peer | None = None
        with self._lock:
            room = peer.room
            lst = self._rooms.get(room)
            if lst and peer in lst:
                lst.remove(peer)
            partner = peer.partner
            if partner:
                partner.partner = None
            peer.partner = None
            peer.alive = False
            self._peers.discard(peer)
            if room and room in self._rooms and not self._rooms[room]:
                del self._rooms[room]
        if partner and partner.alive:
            try:
                partner.send_text('{"t":"bye"}')
            except Exception:
                pass
            try:
                partner.send_close(1000, "peer left")
            except Exception:
                pass
            with self._lock:
                room2 = partner.room
                lst2 = self._rooms.get(room2)
                if lst2 and partner in lst2:
                    lst2.remove(partner)
                self._peers.discard(partner)
                if room2 and room2 in self._rooms and not self._rooms[room2]:
                    del self._rooms[room2]

    def serve(self, peer: Peer) -> None:
        try:
            while peer.alive:
                kind, data = peer.read_message()
                peer.last_active = time.time()
                partner = peer.partner
                if partner and partner.alive:
                    partner.send_frame(data, OP_TEXT if kind == "text" else OP_BINARY)
                    self.stats["frames"] += 1
                    self.stats["bytes"] += len(data)
                # 没有对端（还没配成对 / 对端已走）时直接丢弃，不缓存
        except (RelayError, socket.timeout, OSError, ConnectionError) as exc:
            if peer.alive:
                self.log("连接 #%d (%s) 结束: %s" % (peer.id, peer.addr, exc))
        finally:
            self.disconnect(peer)
            try:
                peer.sock.close()
            except OSError:
                pass
            self.log("连接 #%d 清理，房间 %s 剩余 %d 条连接"
                     % (peer.id, peer.room or "-",
                        self.room_size(peer.room) if peer.room else 0))


# 供 Handler 引用的全局中继实例（在 main() 里赋值）
RELAY: Relay | None = None


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ZongxianRelay/1.0"

    def log_message(self, fmt, *args):   # 关闭默认访问日志，改用 RELAY.log
        pass

    def _body(self, code: int, msg: str) -> None:
        body = (msg + "\n").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/":
            self._body(200, "棕仙的传输软件中继已就绪")
            return
        if path == "/relay":
            qs = parse_qs(parsed.query)
            room = (qs.get("room") or [""])[0]
            if not room or not ROOM_RE.match(room):
                self._body(400, "非法房间号（只允许 [A-Za-z0-9_-]{8,64}）")
                return
            if "websocket" not in (self.headers.get("Upgrade") or "").lower():
                self._body(400, "需要 WebSocket 升级")
                return
            key = self.headers.get("Sec-WebSocket-Key")
            if not key:
                self._body(400, "缺少 Sec-WebSocket-Key")
                return
            if RELAY.room_full(room):
                self._body(400, "房间已满（同一房间最多 %d 个连接）" % RELAY.max_room)
                return
            self._upgrade(key, room)
            return
        self._body(404, "Not Found")

    def _upgrade(self, key: str, room: str) -> None:
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        resp = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: %s\r\n\r\n" % accept
        ).encode("ascii")
        try:
            self.connection.sendall(resp)
        except OSError:
            return
        sock = self.connection                      # 之后直接读原始 socket
        peer = Peer(sock, self.client_address,
                    max_frame=MAX_FRAME, max_message=MAX_MESSAGE, timeout=IDLE_TIMEOUT)
        if not RELAY.register(peer, room):          # 极少数握手并发竞态下兜底
            peer.send_text('{"t":"err","m":"房间已满"}')
            peer.send_close(1013, "room full")
            return
        RELAY.log("连接 #%d %s -> room=%s（当前 %d 连接）"
                  % (peer.id, peer.addr, room, RELAY.room_size(room)))
        RELAY.serve(peer)


def main() -> int:
    global RELAY
    ap = argparse.ArgumentParser(description="棕仙的传输软件 中继服务器")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    ap.add_argument("--port", type=int, default=9443, help="监听端口（默认 9443）")
    ap.add_argument("--max-room", type=int, default=2, help="每房间最大连接数（默认 2）")
    ap.add_argument("--log", action="store_true", help="输出详细转发日志")
    args = ap.parse_args()

    RELAY = Relay(max_room=args.max_room, log_enabled=args.log)

    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.allow_reuse_address = True
    print("棕仙的传输软件中继已就绪  http://%s:%d/" % (args.host, args.port))
    print("配对：同一 room 前 %d 个连接互转；超限拒绝；空闲 %d 秒清理"
          % (args.max_room, IDLE_TIMEOUT))
    if not args.log:
        print("（默认仅记录连接/断开事件，加 --log 输出每次转发统计）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
# 部署 / TLS 建议
# ---------------------------------------------------------------------------
# 1) 纯 ws://（无 TLS）：浏览器里从 https 页面打开会因「混合内容」被拦截。
#    仅建议本地测试（http://localhost 或 http://127.0.0.1 页面可用 ws://）。
# 2) 生产：给中继加 wss://（TLS）。最简用 caddy 一行：
#        caddy reverse-proxy --from relay.example.com --to 127.0.0.1:9443
#    或用 nginx：
#        location / { proxy_pass http://127.0.0.1:9443;
#                     proxy_http_version 1.1;
#                     proxy_set_header Upgrade $http_upgrade;
#                     proxy_set_header Connection "upgrade"; }
#    然后在网页设置里填 wss://relay.example.com （不带 /relay 后缀）。
# 3) 防火墙放行你选的端口（如 9443/tcp），并只让中继监听公网、其余端口收紧。
# 4) 中继带宽上限 = 那台 VPS 的带宽；转发是双向的，上传+下载都会走 VPS 流量。
