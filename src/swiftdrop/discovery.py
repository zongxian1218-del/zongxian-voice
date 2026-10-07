"""棕仙的传输软件 局域网设备发现（UDP 广播 + 主动探测）。

每个实例：
* 监听 45871（被占用则依次退避到 45872-45879）的 UDP 端口；
* 每秒向 255.255.255.255 广播自己的 JSON 名片；
* 收到别人的名片或探测包就记录 / 回复到「在线设备表」。

广播内容（真实端口写在 ``port`` 里，若退避过则不是 45880）：:

    {"magic":"SWIFTDROP1","name":<主机名>,"port":<数据端口>,"os":"windows",
     "ver":"1.0","instance_id":<随机>}

由于退避后的端口对端并不知道，主动探测会**同时打 45871-45879 全部端口**，
所以无论监听方最终落在哪个端口都能被发现。
"""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid

from . import MAGIC, VERSION
from .protocol import DATA_PORT

DISCOVERY_PORT = 45871
DISCOVERY_PORTS = list(range(45871, 45880))   # 退避范围 45871-45879
BROADCAST_INTERVAL = 1.0                      # 每秒广播一次
PEER_TTL = 12.0                               # 超过该秒数没消息就认为离线
PRUNE_INTERVAL = 2.0

MSG_BEACON = "beacon"
MSG_PROBE = "probe"
MSG_REPLY = "reply"


def local_ip() -> str:
    """本机在局域网中用于对外通信的 IPv4（拿不到就回退 127.0.0.1）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def all_local_ipv4() -> list[str]:
    """所有非回环 IPv4 地址（webhost 用来打印可访问 URL）。"""
    ips: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    primary = local_ip()
    if primary and not primary.startswith("127.") and primary not in ips:
        ips.insert(0, primary)
    return ips


def _broadcast_targets() -> list[str]:
    return ["255.255.255.255", "<broadcast>"]


def _interface_candidates() -> list[str]:
    """本机所有可用作源地址的 IPv4（用于逐网卡发广播）。"""
    ips: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    for ip in all_local_ipv4():
        if ip not in ips:
            ips.append(ip)
    return ips


def _send_probe(probe: bytes, port: int, interface: str | None = None) -> None:
    """从一个临时 socket（可指定源网卡）把探测包广播出去。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if interface:
            try:
                s.bind((interface, 0))
            except OSError:
                return
        # 先打本机回环，再打广播：单机多实例 + 局域网都能覆盖
        for target in ("127.0.0.1", "255.255.255.255", "<broadcast>"):
            try:
                s.sendto(probe, (target, port))
            except OSError:
                pass
    finally:
        s.close()


def _open_listener(ports: list[int]) -> tuple[socket.socket, int]:
    """绑定发现端口。

    优先 45871（正是规格里的端口），被占用才依次退避。
    显式设置 ``SO_REUSEADDR``：同一台机器上跑多个实例（或测试里同时开
    recv + peers）时，大家都能绑到 45871 并各收一份广播，这对开发很关键。
    返回 ``(socket, 实际端口)``。
    """
    last: OSError | None = None
    for port in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind(("", port))
            s.settimeout(0.5)
            return s, port
        except OSError as exc:
            last = exc
            s.close()
    raise OSError(f"45871-45879 全部被占用，无法启动发现服务: {last}")


def _beacon(name: str, port: int, instance_id: str) -> bytes:
    return json.dumps({
        "magic": MAGIC, "name": name, "port": int(port), "os": "windows",
        "ver": VERSION, "instance_id": instance_id,
    }, ensure_ascii=False).encode("utf-8")


def _probe(instance_id: str) -> bytes:
    return json.dumps({
        "magic": MAGIC, "kind": MSG_PROBE, "name": socket.gethostname(),
        "os": "windows", "ver": VERSION, "instance_id": instance_id,
        "nonce": uuid.uuid4().hex,
    }, ensure_ascii=False).encode("utf-8")


class Peer:
    __slots__ = ("ip", "name", "port", "last_seen", "instance_id", "source")

    def __init__(self, ip: str, name: str, port: int, instance_id: str,
                 source: str = "broadcast"):
        self.ip = ip
        self.name = name
        self.port = int(port)
        self.instance_id = instance_id
        self.source = source
        self.last_seen = time.time()

    def touch(self) -> None:
        self.last_seen = time.time()

    @property
    def key(self) -> str:
        return self.instance_id or f"{self.ip}:{self.port}"

    def as_dict(self) -> dict:
        return {
            "ip": self.ip, "name": self.name, "port": self.port,
            "last_seen": self.last_seen, "instance_id": self.instance_id,
            "source": self.source,
            "age": round(time.time() - self.last_seen, 1),
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Peer {self.name} {self.ip}:{self.port}>"


class DiscoveryService:
    """后台常驻的发现服务：广播自己 + 收集别人。"""

    def __init__(self, name: str | None = None, data_port: int = 0,
                 port_range: list[int] | None = None, broadcast: bool = True):
        self.name = name or socket.gethostname()
        self.data_port = int(data_port)
        self.instance_id = uuid.uuid4().hex
        self.sock, self.port = _open_listener(port_range or DISCOVERY_PORTS)
        self.broadcast_enabled = broadcast
        self._peers: dict[str, Peer] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_prune = 0.0

    # -- 设备表 -----------------------------------------------------------
    def peer_table(self) -> list[dict]:
        """快照，给 GUI / CLI 用（按名字排序）。"""
        with self._lock:
            self._prune_locked()
            return sorted((p.as_dict() for p in self._peers.values()),
                          key=lambda d: (d["name"], d["ip"]))

    def peers(self) -> list[Peer]:
        with self._lock:
            self._prune_locked()
            return list(self._peers.values())

    def _prune_locked(self) -> None:
        now = time.time()
        if now - self._last_prune < PRUNE_INTERVAL:
            return
        self._last_prune = now
        for key in [k for k, p in self._peers.items() if now - p.last_seen > PEER_TTL]:
            self._peers.pop(key, None)

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> "DiscoveryService":
        for target in (self._listen_loop, self._broadcast_loop):
            t = threading.Thread(target=target, name=f"discovery-{target.__name__}",
                                 daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        for t in self._threads:
            t.join(timeout=1.5)
        self._threads.clear()

    def __enter__(self) -> "DiscoveryService":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- 内部循环 ---------------------------------------------------------
    def _broadcast_loop(self) -> None:
        payload = _beacon(self.name, self.data_port, self.instance_id)
        interfaces = [None] + _interface_candidates()
        while not self._stop.is_set():
            if self.broadcast_enabled:
                # 打全部发现端口：别人退避到 45872-45879 时也能听到我们
                for port in DISCOVERY_PORTS:
                    for iface in interfaces:
                        _send_probe(payload, port, iface)
            self._stop.wait(BROADCAST_INTERVAL)

    def _listen_loop(self) -> None:
        my_ips = {"127.0.0.1", "0.0.0.0", local_ip()}
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                time.sleep(0.1)
                continue
            try:
                msg = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(msg, dict) or msg.get("magic") != MAGIC:
                continue
            if msg.get("instance_id") == self.instance_id:
                continue  # 自己发的包，忽略
            kind = msg.get("kind")
            if kind == MSG_PROBE:
                # 有人在找服务 → 单播回复我们的名片
                try:
                    self.sock.sendto(
                        _beacon(self.name, self.data_port, self.instance_id), addr)
                except OSError:
                    pass
                continue
            name = str(msg.get("name") or addr[0])
            try:
                port = int(msg.get("port", 0))
            except (TypeError, ValueError):
                continue
            if port <= 0:
                continue
            key = str(msg.get("instance_id") or f"{addr[0]}:{port}")
            # 同机多实例：IP 可能都是本机地址，靠 instance_id 区分，不去重
            source = "reply" if kind == MSG_REPLY else "broadcast"
            if addr[0] in my_ips and msg.get("instance_id") is None:
                continue
            with self._lock:
                peer = self._peers.get(key)
                if peer is None:
                    self._peers[key] = Peer(addr[0], name, port, key, source)
                else:
                    peer.touch()
                    peer.port = port
                    peer.name = name
                    peer.ip = addr[0]
                    peer.source = source


# --------------------------------------------------------------------------
# 阻塞式收集（CLI peers / send / sync 用）
# --------------------------------------------------------------------------

def _reply_to_peer(data: bytes, addr, out: dict[str, Peer]) -> None:
    try:
        msg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if not isinstance(msg, dict) or msg.get("magic") != MAGIC:
        return
    if msg.get("kind") == MSG_PROBE:
        return
    try:
        port = int(msg.get("port", 0))
    except (TypeError, ValueError):
        return
    if port <= 0:
        return
    key = str(msg.get("instance_id") or f"{addr[0]}:{port}")
    peer = out.get(key)
    if peer is None:
        out[key] = Peer(addr[0], str(msg.get("name") or addr[0]), port, key,
                        "reply" if msg.get("kind") == MSG_REPLY else "broadcast")
    else:
        peer.touch()


def list_peers(timeout: float = 3.0, self_id: str | None = None,
               name: str | None = None) -> list[dict]:
    """阻塞收集 ``timeout`` 秒内听到的所有设备（含主动探测）。

    做法：把 45871-45879 **每一端口**都绑一个临时 socket，逐个网卡广播探测
    包。对端无论在哪个端口监听都会收到探测，而它的单播回复会回到我们绑定
    的那个端口；同时它每秒的广播也会落到对应端口上。
    """
    self_id = self_id or uuid.uuid4().hex
    socks: list[socket.socket] = []
    found: dict[str, Peer] = {}
    try:
        for port in DISCOVERY_PORTS:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.bind(("", port))
            except OSError:
                s.close()
                continue
            s.settimeout(0.2)
            socks.append(s)

        if not socks:                      # 极端情况：端口全被占用，退化成临时端口
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind(("", 0))
            s.settimeout(0.2)
            socks.append(s)

        probe = _probe(self_id)
        interfaces = [None] + _interface_candidates()
        deadline = time.monotonic() + max(0.2, timeout)
        next_probe = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_probe:
                for port in DISCOVERY_PORTS:
                    for iface in interfaces:
                        _send_probe(probe, port, iface)
                next_probe = now + 1.0
            for s in socks:
                try:
                    data, addr = s.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    continue
                try:
                    msg = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(msg, dict) and msg.get("instance_id") == self_id:
                    continue
                _reply_to_peer(data, addr, found)
    finally:
        for s in socks:
            try:
                s.close()
            except OSError:
                pass
    return sorted((p.as_dict() for p in found.values()),
                  key=lambda d: (d["name"], d["ip"]))


def resolve_target(target: str, timeout: float = 3.0) -> tuple[str, int]:
    """把 ``IP`` / ``IP:端口`` / ``设备名`` 解析成 (ip, data_port)。

    找不到名字直接抛 ``LookupError``，让 CLI 给人类可读的提示。
    """
    target = (target or "").strip()
    if not target:
        raise LookupError("目标为空")
    # 直连 IP:端口
    if ":" in target and target.count(":") == 1:
        host, _, port_s = target.rpartition(":")
        if host and port_s.isdigit():
            return host, int(port_s)
    if target.count(".") == 3 and all(
            p.isdigit() and 0 <= int(p) <= 255 for p in target.split(".")):
        return target, DATA_PORT
    peers = list_peers(timeout=timeout)
    low = target.lower()
    for p in peers:                       # 先精确匹配
        if low in (p["name"].lower(), p["ip"].lower(),
                   f'{p["ip"]}:{p["port"]}'.lower()):
            return p["ip"], p["port"]
    for p in peers:                       # 再模糊匹配
        if low in p["name"].lower():
            return p["ip"], p["port"]
    names = "、".join(f'{p["name"]}({p["ip"]}:{p["port"]})' for p in peers) or "（无）"
    raise LookupError(f"没找到设备 {target!r}；当前在线: {names}")
