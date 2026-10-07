"""棕仙的传输软件 传输引擎：多流并发发送端 + 接收端（含断点续传与整文件校验）。

设计要点
--------
* **控制连接**：一条 TCP 连接负责 hello / manifest / file_begin / file_ack /
  done；所有非数据消息都走它，所以接收端永远知道哪些文件还没开始。
* **数据流**：另外 N 条 TCP 连接（``--streams``，默认 4）。发送端每条流一个
  线程，从分片工作队列里取活，向接收端发 ``pull``；接收端按请求读原文件，
  用 ``chunk`` 帧回送该分片的原始字节。分片乱序到达，接收端 ``seek+write``
  写到各自偏移。把随机读放在**接收端**（本地磁盘顺序友好、且发送端只读一次），
  两边都不需要额外拷贝。

  之所以是 pull 而不是 push：接收端最清楚哪个分片已经有了，续传时发送端
  只要问一句 ``file_begin`` 就能拿到「已完成分片位图」，天然不重传。

* **完整性**：整文件 SHA-256 必须校验；接收端失败会丢弃状态并可被要求重传
  （发送端带 ``--retries``，默认 3 次）。
* **断点续传**：接收目录下 ``.swiftdrop-state.json`` 记录相对路径 / 大小 /
  已完成分片索引；同目录 ``<名字>.part`` 是临时文件，全部完成且校验通过后
  ``os.replace`` 原子改名。
* **安全**：接收端拒绝 ``..``、绝对路径、盘符、UNC（见 protocol.is_safe_relpath）。
"""

from __future__ import annotations

import os
import queue
import socket
import threading
import time
from collections.abc import Callable, Iterable

from .protocol import (
    CHUNK_SIZE,
    ConnectionClosed,
    DEFAULT_STREAMS,
    PART_SUFFIX,
    PROTOCOL_VERSION,
    ProtocolError,
    ResumeState,
    SpeedMeter,
    chunk_sha256,
    chunks_for,
    file_sha256,
    handshake_client,
    handshake_server,
    hello_message,
    human_bytes,
    is_safe_relpath,
    quick_signature,
    recv_chunk,
    recv_any,
    recv_frame,
    recv_json,
    resolve_under,
    send_chunk,
    send_json,
    tune_socket,
)

# 忽略规则在这里派一份，避免 transfer <-> sync 互相 import
_IGNORE_FILES = {".swiftdrop-state.json"}
_IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".svn", ".hg"}

_ZERO_BLOCK = bytes(4 * 1024 * 1024)


# --------------------------------------------------------------------------
# 连接诊断用的按流计数
# --------------------------------------------------------------------------

class _StreamCounters:
    """按数据流统计已传字节数（连接诊断面板要的「各条流分别多少」）。

    发送端按固定的 ``streams`` 条流预先开号；接收端每来一条数据连接就
    ``new_stream()`` 开一个新号。所有方法都加锁，可在任意线程调用。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bytes: dict[int, int] = {}
        self._next = 0

    def reset(self, preallocate: int = 0) -> None:
        with self._lock:
            n = max(0, int(preallocate))
            self._bytes = {i: 0 for i in range(n)}
            self._next = n

    def new_stream(self) -> int:
        with self._lock:
            idx = self._next
            self._next += 1
            self._bytes[idx] = 0
            return idx

    def add(self, idx: int | None, n: int) -> None:
        if idx is None or n <= 0:
            return
        with self._lock:
            self._bytes[int(idx)] = self._bytes.get(int(idx), 0) + int(n)

    def snapshot(self) -> dict[int, int]:
        with self._lock:
            return {i: b for i, b in sorted(self._bytes.items())}


def _preallocate(path: str, size: int) -> None:
    """把 ``.part`` 撑到 ``size`` 字节并确保存在。

    这里**不写零**：早先是循环写满 0 把空间落地，对大文件等于把整盘数据多写
    一遍（实测吃掉一半以上吞吐）。续传的权威依据是状态文件里的分片标记，而
    复用 ``.part`` 的前置条件是"长度等于 size"，所以只需要把长度撑到位即可；
    真正的内容全部由后续的 ``seek+write`` 写入，写完还会立刻读回来核对。
    """
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        if size > 0:
            fh.truncate(size)


# --------------------------------------------------------------------------
# 路径收集
# --------------------------------------------------------------------------

def _ignored(name: str) -> bool:
    if name in _IGNORE_FILES or name.endswith(PART_SUFFIX):
        return True
    if name.startswith("~$"):
        return True
    return name in (".DS_Store", "Thumbs.db")


def collect_files(paths: Iterable[str], base: str | None = None
                  ) -> list[tuple[str, str, int, float]]:
    """展开待传路径 → [(绝对路径, 相对路径, 大小, mtime)]。

    :param base: 指定相对路径的基准目录。给了它，传入的文件/目录都会**保留**
        相对 base 的目录结构（同步回传时必须这样，否则深层文件会被拍平到
        接收目录根部）。不给就按老规矩：传目录时相对其父目录、传文件时用文件名。
    """
    out: list[tuple[str, str, int, float]] = []
    seen: set[str] = set()

    def add(abs_path: str, rel_path: str) -> None:
        rel = rel_path.replace(os.sep, "/").lstrip("/")
        if not is_safe_relpath(rel):
            raise ProtocolError(f"无法为 {abs_path!r} 生成合法相对路径: {rel!r}")
        st = os.stat(abs_path)
        key = rel.lower()
        if key in seen:
            return
        seen.add(key)
        out.append((abs_path, rel, st.st_size, st.st_mtime))

    base_abs = os.path.abspath(base) if base else None

    for raw in paths:
        p = os.path.abspath(os.path.expanduser(raw))
        if not os.path.exists(p):
            raise FileNotFoundError(f"路径不存在: {raw}")
        if base_abs is not None:
            rel = os.path.relpath(p, base_abs)
            if rel.startswith(".."):
                raise ProtocolError(f"{p!r} 不在基准目录 {base_abs!r} 之内")
            if os.path.isfile(p):
                add(p, rel)
            else:
                for root, dirs, files in os.walk(p):
                    dirs[:] = [d for d in dirs
                               if d not in _IGNORE_DIRS and not _ignored(d)]
                    for fn in files:
                        if _ignored(fn):
                            continue
                        fp = os.path.join(root, fn)
                        add(fp, os.path.relpath(fp, base_abs))
        elif os.path.isfile(p):
            add(p, os.path.basename(p))
        else:
            base_of_dir = os.path.dirname(p.rstrip("\\/")) or p
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS and not _ignored(d)]
                for fn in files:
                    if _ignored(fn):
                        continue
                    fp = os.path.join(root, fn)
                    add(fp, os.path.relpath(fp, base_of_dir))
    return out


# --------------------------------------------------------------------------
# 发送端
# --------------------------------------------------------------------------

class SendResult:
    def __init__(self):
        self.sent_files = 0
        self.skipped_files = 0
        self.failed_files: list[tuple[str, str]] = []
        self.bytes_sent = 0
        self.bytes_skipped = 0
        self.elapsed = 0.0
        self.remote_name = ""

    @property
    def rate(self) -> float:
        return self.bytes_sent / self.elapsed if self.elapsed > 1e-6 else 0.0

    @property
    def ok(self) -> bool:
        return not self.failed_files


class Sender:
    """把文件/目录推到远端接收端。"""

    def __init__(self, host: str, port: int, paths: Iterable[str], *,
                 streams: int = DEFAULT_STREAMS, chunk_size: int = CHUNK_SIZE,
                 name: str | None = None, retries: int = 3,
                 connect_timeout: float = 15.0,
                 base: str | None = None,
                 on_progress: Callable[[dict], None] | None = None,
                 on_log: Callable[[str], None] | None = None,
                 stop_event: threading.Event | None = None):
        self.host = host
        self.port = int(port)
        self.paths = list(paths)
        self.base = base
        self.streams = max(1, min(8, int(streams)))
        self.chunk_size = max(64 * 1024, int(chunk_size))
        self.name = name or socket.gethostname()
        self.retries = max(1, int(retries))
        self.connect_timeout = connect_timeout
        self.on_progress = on_progress
        self.on_log = on_log
        self.stop_event = stop_event or threading.Event()
        self.result = SendResult()
        self._meter = SpeedMeter(0)
        self._meter_lock = threading.Lock()
        self._failed: dict[str, str] = {}
        self._done_files: list[str] = []
        # -- 连接诊断插桩（只做统计，不参与传输逻辑）----------------------
        self._stream_bytes = _StreamCounters()
        self._diag_lock = threading.Lock()
        self._net_send_seconds = 0.0      # socket 写阻塞累计
        self._net_wait_seconds = 0.0      # 等接收端 chunk_ack 累计
        self._streams_active = 0
        self._streams_peak = 0
        self._local_ip = ""

    # -- 诊断计数 ---------------------------------------------------------
    def _reset_diag(self) -> None:
        """一轮发送开始前把诊断计数清零。"""
        self._stream_bytes.reset(self.streams)
        with self._diag_lock:
            self._net_send_seconds = 0.0
            self._net_wait_seconds = 0.0
            self._streams_active = 0
            self._streams_peak = 0
            self._local_ip = ""

    def _stream_enter(self) -> None:
        with self._diag_lock:
            self._streams_active += 1
            if self._streams_active > self._streams_peak:
                self._streams_peak = self._streams_active

    def _stream_leave(self) -> None:
        with self._diag_lock:
            self._streams_active = max(0, self._streams_active - 1)

    def diag_snapshot(self) -> dict:
        """给诊断面板用的实测快照（发送端只有网络侧数字）。"""
        with self._diag_lock:
            net_send = self._net_send_seconds
            net_wait = self._net_wait_seconds
            active = self._streams_active
            peak = self._streams_peak
            local_ip = self._local_ip
        return {
            "peer_ip": self.host,
            "local_ip": local_ip,
            "streams": peak or self.streams,
            "streams_active": active,
            "stream_bytes": self._stream_bytes.snapshot(),
            "net_send_seconds": net_send,
            "net_wait_seconds": net_wait,
        }

    # -- 小工具 -----------------------------------------------------------
    def log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)

    def _emit(self, **kw) -> None:
        if self.on_progress:
            base = {
                "phase": "send", "total": self._meter.total,
                "done": self._meter.done, "rate": self._meter.rate(),
                "eta": self._meter.eta(), "elapsed": self._meter.elapsed(),
                "sent_files": self.result.sent_files,
                "skipped_files": self.result.skipped_files,
                "failed": len(self._failed),
            }
            base.update(self.diag_snapshot())
            base.update(kw)
            try:
                self.on_progress(base)
            except Exception:  # UI 回调不能影响传输
                pass

    def _connect(self) -> socket.socket:
        sock = socket.create_connection((self.host, self.port),
                                       timeout=self.connect_timeout)
        tune_socket(sock)
        return sock

    # -- 主流程 -----------------------------------------------------------
    def run(self) -> SendResult:
        t0 = time.monotonic()
        files = collect_files(self.paths, base=self.base)
        if not files:
            raise FileNotFoundError("没有可发送的文件（可能都被忽略规则过滤了）")
        total = sum(f[2] for f in files)
        with self._meter_lock:
            self._meter = SpeedMeter(total)
        self._reset_diag()

        ctrl = self._connect()
        try:
            ctrl.settimeout(self.connect_timeout)
            remote = handshake_client(ctrl, self.name, self.port)
            self.result.remote_name = str(remote.get("name", self.host))
            try:
                # 真实的本机出口地址（走的是哪张网卡），拿不到就留空
                sockname = ctrl.getsockname()
                with self._diag_lock:
                    self._local_ip = str(sockname[0]) if sockname else ""
            except OSError:
                pass
            ctrl.settimeout(None)
            self.log(f"已连接 {self.result.remote_name} ({self.host}:{self.port})，"
                     f"{len(files)} 个文件 / {human_bytes(total)}，"
                     f"{self.streams} 流并发")

            manifest = {
                "type": "manifest",
                "files": [{
                    "path": rel, "size": size, "mtime": mtime,
                    "chunks": chunks_for(size, self.chunk_size),
                } for _abs, rel, size, mtime in files],
                "total": total, "streams": self.streams,
                "chunk_size": self.chunk_size,
            }
            send_json(ctrl, manifest)
            ack = recv_json(ctrl)
            if ack.get("type") != "manifest_ok":
                raise ProtocolError(f"manifest 被拒: {ack}")
            accepted = ack.get("accept")
            if accepted is None:
                accepted = [f["path"] for f in manifest["files"]]
            by_rel = {rel: (abs_, rel, size, mtime) for abs_, rel, size, mtime in files}
            accepted = [p for p in accepted if p in by_rel]
            if not accepted:
                self.log("远端不需要任何文件")

            for rel in accepted:
                if self.stop_event.is_set():
                    self.log("收到停止信号，提前结束")
                    break
                abs_path, rel, size, mtime = by_rel[rel]
                self._send_one(ctrl, abs_path, rel, size, mtime)

            # 收尾：告知完成，等远端总确认
            try:
                send_json(ctrl, {"type": "done", "sent": self.result.sent_files,
                                 "skipped": self.result.skipped_files,
                                 "failed": sorted(self._failed)})
                final = recv_json(ctrl)
                if final.get("type") == "done_ok":
                    self.log(f"远端已确认：接收 {final.get('received', '?')} 个文件"
                             f"，失败 {len(final.get('failed', []))} 个")
            except (ProtocolError, OSError):
                pass
        finally:
            try:
                ctrl.close()
            except OSError:
                pass

        self.result.failed_files = sorted(self._failed.items())
        self.result.elapsed = time.monotonic() - t0
        self.result.bytes_sent = self._meter.done
        self._emit(phase="send-done")
        return self.result

    def _send_one(self, ctrl: socket.socket, abs_path: str, rel: str,
                  size: int, mtime: float) -> None:
        chunks = chunks_for(size, self.chunk_size)
        sig = quick_signature(abs_path, size)
        sent_ok = False
        last_err = "未知错误"

        for attempt in range(1, self.retries + 1):
            if self.stop_event.is_set():
                return
            send_json(ctrl, {
                "type": "file_begin", "path": rel, "size": size,
                "mtime": mtime, "chunks": chunks, "chunk_size": self.chunk_size,
                "sha256": self._file_hash(abs_path, rel, size),
                "sig": sig, "attempt": attempt,
            })
            begin = recv_json(ctrl)
            btype = begin.get("type")
            if btype == "file_skip":
                self.result.skipped_files += 1
                self.result.bytes_skipped += size
                self.log(f"跳过（远端已一致）: {rel}")
                self._done_files.append(rel)
                return
            if btype == "file_reject":
                self._failed[rel] = str(begin.get("why", "被拒绝"))
                self.log(f"远端拒绝 {rel}: {begin.get('why')}")
                return
            if btype != "file_begin_ok":
                raise ProtocolError(f"期望 file_begin_ok，收到 {btype!r}")

            have = {int(c) for c in begin.get("have", [])}
            have = {c for c in have if 0 <= c < chunks}
            skipped = sum(
                min(self.chunk_size, size - c * self.chunk_size) for c in have)
            if have:
                self.log(f"续传 {rel}: 跳过 {len(have)}/{chunks} 片 "
                         f"({human_bytes(skipped)})")
                with self._meter_lock:
                    self._meter.add(skipped)
                    self._meter.total = max(1, self._meter.total - skipped)
                self.result.bytes_skipped += skipped

            try:
                self._pump(ctrl, abs_path, rel, size, chunks, have)
                send_json(ctrl, {"type": "file_end", "path": rel,
                                 "sha256": begin.get("expect_sha256") or None})
                ack = recv_json(ctrl)
                if ack.get("type") == "file_ack" and ack.get("ok"):
                    sent_ok = True
                else:
                    last_err = str(ack.get("why") or ack.get("error") or "校验失败")
                    self.log(f"文件校验失败 {rel}: {last_err}"
                             + (f"（重试 {attempt}/{self.retries}）"
                                if attempt < self.retries else ""))
            except (ProtocolError, OSError) as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                self.log(f"传输中断 {rel}: {last_err}"
                         + (f"（重试 {attempt}/{self.retries}）"
                            if attempt < self.retries else ""))

            if sent_ok:
                self.result.sent_files += 1
                self._done_files.append(rel)
                self.log(f"完成 {rel} ({human_bytes(size)})")
                break
            # 重连控制连接后重试（同一连接上的流状态已脏）
            try:
                ctrl = self._reconnect_ctrl(ctrl)
            except OSError as exc:
                last_err = f"重连失败: {exc}"
                break
        else:
            self._failed[rel] = last_err
        if not sent_ok and rel not in self._failed:
            self._failed[rel] = last_err

    def _reconnect_ctrl(self, old: socket.socket) -> socket.socket:
        try:
            old.close()
        except OSError:
            pass
        sock = self._connect()
        handshake_client(sock, self.name, self.port)
        return sock

    def _file_hash(self, abs_path: str, rel: str, size: int) -> str:
        """整文件 SHA-256（重试时可复用，避免重复计算）。"""
        cache = getattr(self, "_hash_cache", None)
        if cache is None:
            cache = self._hash_cache = {}
        key = (abs_path, size, os.path.getmtime(abs_path))
        if key not in cache:
            cache[key] = file_sha256(abs_path)
        return cache[key]

    # -- 数据流 ----------------------------------------------------------
    def _pump(self, ctrl: socket.socket, abs_path: str, rel: str, size: int,
              chunks: int, have: set[int]) -> None:
        """把 ``have`` 之外的分片并发拉完。

        重要：**所有网络读写都发生在各自流的 socket 上**，控制连接在这期间
        一个字节都不碰 —— 否则两条路径同时 recv 会把流撕裂（早期版本的真实
        bug）。每条流会被反复复用直到队列清空；任何分片失败都会立刻打断整轮
        （``stalled``），由上层重连后重试。
        """
        pending: list[int] = [c for c in range(chunks) if c not in have]
        if not pending:
            return
        todo: "queue.Queue[int]" = queue.Queue()
        for c in pending:
            todo.put(c)

        cond = threading.Condition()
        state = {"queued": len(pending), "inflight": 0, "stalled": False}
        errors: list[BaseException] = []

        # 单条流最多容忍几次瞬时抖动，超过就整轮失败重来
        retry_budget = {"left": 2}

        def worker(stream_index: int) -> None:
            stream: socket.socket | None = None
            try:
                stream = self._connect()
                handshake_client(stream, self.name, self.port)
            except (OSError, ProtocolError) as exc:
                with cond:
                    errors.append(exc)
                    state["stalled"] = True
                    cond.notify_all()
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
                return

            self._stream_enter()
            try:
                while True:
                    with cond:
                        while not state["stalled"] and state["queued"] == 0:
                            # 队列空了：要么别人还在飞，要么真的干完了
                            if state["inflight"] == 0:
                                return
                            cond.wait(0.5)
                        if state["stalled"]:
                            return
                        idx = todo.get_nowait()
                        state["queued"] -= 1
                        state["inflight"] += 1
                    try:
                        self._push_one(stream, rel, idx, abs_path, size,
                                       stream_index=stream_index)
                    except (ProtocolError, OSError) as exc:
                        with cond:
                            state["inflight"] -= 1
                            errors.append(exc)
                            if retry_budget["left"] > 0:
                                retry_budget["left"] -= 1
                                todo.put_nowait(idx)
                                state["queued"] += 1
                            else:
                                state["stalled"] = True
                            cond.notify_all()
                            if state["stalled"]:
                                return
                        # 重连这一条流再试
                        try:
                            stream.close()
                        except OSError:
                            pass
                        try:
                            stream = self._connect()
                            handshake_client(stream, self.name, self.port)
                        except (OSError, ProtocolError) as exc2:
                            with cond:
                                errors.append(exc2)
                                state["stalled"] = True
                                cond.notify_all()
                            return
                        continue
                    with cond:
                        state["inflight"] -= 1
                        cond.notify_all()
            finally:
                self._stream_leave()
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass

        threads = [threading.Thread(target=worker, args=(i,),
                                    name=f"stream-{i}", daemon=True)
                   for i in range(self.streams)]
        for t in threads:
            t.start()

        # 主线程只等结果，不碰任何 socket
        with cond:
            while not state["stalled"] and (state["queued"] or state["inflight"]):
                cond.wait(0.5)
                if self.stop_event.is_set():
                    state["stalled"] = True
                    cond.notify_all()
                    break
            stalled = state["stalled"]
            not_done = state["queued"] + state["inflight"]
        for t in threads:
            t.join(timeout=10)

        if errors:
            raise errors[0]
        if stalled or not_done:
            raise ProtocolError(f"{rel} 仍有 {not_done} 个分片未完成")

    def _push_one(self, stream: socket.socket, rel: str, idx: int,
                  abs_path: str, size: int,
                  stream_index: int | None = None) -> None:
        """发送端把第 idx 个分片推给接收端，并等它对账。

        ``stream_index`` 只用于连接诊断的按流计数，不参与协议。
        """
        offset = idx * self.chunk_size
        want = min(self.chunk_size, size - offset)
        with open(abs_path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(want)
        if len(data) != want:
            raise ProtocolError(f"{rel}#{idx} 本地读取不足: {len(data)} != {want}")
        # 实测：socket 写阻塞了多久（上行/链路饱和的直接证据）
        t_send = time.monotonic()
        try:
            send_chunk(stream, {
                "type": "push", "path": rel, "index": idx, "offset": offset,
                "size": want, "sha256": chunk_sha256(data),
            }, data)
        finally:
            with self._diag_lock:
                self._net_send_seconds += time.monotonic() - t_send
        # 接收端只有在分片真正落盘后才回 chunk_ack，因此 file_end 不会抢跑
        t_wait = time.monotonic()
        try:
            ack, _ = recv_frame(stream)
        finally:
            with self._diag_lock:
                self._net_wait_seconds += time.monotonic() - t_wait
        atype = ack.get("type")
        if atype == "chunk_ack" and ack.get("index") == idx:
            pass
        elif atype == "chunk_have" and ack.get("index") == idx:
            # 接收端本来就有这一片（续传），跳过即可
            self.result.bytes_skipped += len(data)
            self._stream_bytes.add(stream_index, len(data))
            with self._meter_lock:
                self._meter.add(len(data))
            return
        else:
            raise ProtocolError(
                f"分片确认异常: {atype} idx={ack.get('index')} why={ack.get('why')}")
        self._stream_bytes.add(stream_index, len(data))
        with self._meter_lock:
            self._meter.add(len(data))
        self._emit(current=rel, chunk=idx)


# --------------------------------------------------------------------------
# 接收端
# --------------------------------------------------------------------------

class ReceiveStats:
    def __init__(self):
        self.files_received = 0
        self.files_skipped = 0
        self.failed: list[tuple[str, str]] = []
        self.bytes_received = 0
        self.bytes_resumed = 0
        self.elapsed = 0.0
        self.remote_name = ""


class ReceiverServer:
    """监听端口，接收一个或多个发送端（顺序处理，每条发送端内并发 N 流）。

    用法::

        srv = ReceiverServer(dest_dir, port=45880)
        srv.start()
        ...
        srv.stop()
    """

    def __init__(self, dest_dir: str, port: int = 0, *,
                 name: str | None = None, chunk_size: int = CHUNK_SIZE,
                 on_progress: Callable[[dict], None] | None = None,
                 on_log: Callable[[str], None] | None = None,
                 stop_event: threading.Event | None = None,
                 once: bool = False, allow_delete: bool = False,
                 sync_dir: str | None = None):
        self.dest = os.path.abspath(dest_dir)
        self.allow_delete = allow_delete
        self.sync_dir = os.path.abspath(sync_dir) if sync_dir else self.dest
        self.name = name or socket.gethostname()
        self.chunk_size = max(64 * 1024, int(chunk_size))
        self.on_progress = on_progress
        self.on_log = on_log
        self.stop_event = stop_event or threading.Event()
        self.once = once
        self.stats = ReceiveStats()
        self._listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tune_socket(self._listen)
        self._listen.bind(("0.0.0.0", int(port)))
        self._listen.listen(64)
        self.port = self._listen.getsockname()[1]
        self._thread: threading.Thread | None = None
        self._state = ResumeState(self.dest)
        self._meter = SpeedMeter(0)
        self._meter_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active: socket.socket | None = None
        self._sessions: dict[str, "_Session"] = {}
        self._sessions_lock = threading.RLock()
        self._conn_threads: list[threading.Thread] = []
        self.sync_responder = None
        self._chunks_written = 0
        self.ready = threading.Event()
        self._closed = False
        # -- 连接诊断插桩（只做统计，不参与传输逻辑）----------------------
        self._stream_bytes = _StreamCounters()
        self._diag_lock = threading.Lock()
        self._disk_seconds = 0.0          # 落盘（写 + 回读核对 + fsync）累计
        self._disk_bytes = 0
        self._diag_peer_ip = ""
        self._diag_local_ip = ""

    # -- 诊断计数 ---------------------------------------------------------
    def _reset_diag(self) -> None:
        """一份新清单到达 = 新一轮接收，诊断计数清零。"""
        self._stream_bytes.reset(0)
        with self._diag_lock:
            self._disk_seconds = 0.0
            self._disk_bytes = 0

    def diag_snapshot(self) -> dict:
        """给诊断面板用的实测快照（接收端只有磁盘侧数字）。"""
        with self._diag_lock:
            disk_seconds = self._disk_seconds
            disk_bytes = self._disk_bytes
            peer_ip = self._diag_peer_ip
            local_ip = self._diag_local_ip
        streams = self._stream_bytes.snapshot()
        return {
            "peer_ip": peer_ip,
            "local_ip": local_ip,
            "streams": len(streams),
            "stream_bytes": streams,
            "disk_seconds": disk_seconds,
            "disk_bytes": disk_bytes,
        }

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> "ReceiverServer":
        os.makedirs(self.dest, exist_ok=True)
        self._thread = threading.Thread(target=self._accept_loop,
                                        name="swiftdrop-recv", daemon=True)
        self._thread.start()
        self.ready.set()
        return self

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop_event.set()
        for sock in (self._listen, self._active):
            try:
                if sock is not None:
                    sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        try:
            self._state.save(force=True)
        except OSError:
            pass

    def serve_forever(self) -> None:
        """阻塞直到 stop()。"""
        self.start()
        while not self.stop_event.is_set():
            time.sleep(0.2)

    def __enter__(self) -> "ReceiverServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)

    def _emit(self, **kw) -> None:
        if self.on_progress:
            base = {
                "phase": "recv", "total": self._meter.total, "done": self._meter.done,
                "rate": self._meter.rate(), "eta": self._meter.eta(),
                "elapsed": self._meter.elapsed(),
                "files_received": self.stats.files_received,
                "files_skipped": self.stats.files_skipped,
                "failed": len(self.stats.failed),
            }
            base.update(self.diag_snapshot())
            base.update(kw)
            try:
                self.on_progress(base)
            except Exception:
                pass

    # -- 接受连接 ---------------------------------------------------------
    def _accept_loop(self) -> None:
        self.log(f"接收目录: {self.dest}")
        self.log(f"监听 0.0.0.0:{self.port}，等待发送端…")
        while not self.stop_event.is_set():
            try:
                conn, addr = self._listen.accept()
            except OSError:
                return
            if self.stop_event.is_set():
                conn.close()
                return
            tune_socket(conn)
            t = threading.Thread(target=self._connection_entry, args=(conn, addr[0]),
                                 name=f"conn-{addr[0]}-{id(conn) % 10000}", daemon=True)
            t.start()
            self._conn_threads.append(t)
            if self.once:
                for th in list(self._conn_threads):
                    th.join(timeout=600)
                return

    # -- 连接入口：先握手，再按第一条业务消息判定角色 ----------------------
    def _connection_entry(self, conn: socket.socket, ip: str) -> None:
        try:
            conn.settimeout(600)
            peer = handshake_server(conn, self.name, self.port)
            msg, first_payload = recv_any(conn)
        except (ProtocolError, ConnectionClosed, OSError) as exc:
            if not self.stop_event.is_set():
                self.log(f"握手失败 {ip}: {exc}")
            try:
                conn.close()
            except OSError:
                pass
            return

        session = self._session_for(ip)
        session.remote = str(peer.get("name", ip))
        self.stats.remote_name = session.remote
        with self._diag_lock:
            self._diag_peer_ip = ip
            try:
                self._diag_local_ip = str(conn.getsockname()[0])
            except OSError:
                pass
        mtype = msg.get("type")
        if mtype in ("push", "pull"):
            # 数据流：挂到已有控制会话上
            try:
                self._pull_loop(conn, session, first=msg, first_payload=first_payload)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
        else:
            self._active = conn
            try:
                self._control_loop(conn, session, ip, first=msg)
            finally:
                self._active = None
                session.closed = True
                try:
                    conn.close()
                except OSError:
                    pass

    def _session_for(self, ip: str) -> "_Session":
        """同一发送端（IP）复用同一个会话；新 IP 就新建。"""
        with self._sessions_lock:
            sess = self._sessions.get(ip)
            if sess is None or sess.closed:
                sess = _Session(self, ip)
                self._sessions[ip] = sess
            return sess

    # -- 控制连接会话 -----------------------------------------------------
    def _control_loop(self, ctrl: socket.socket, session: "_Session", ip: str,
                      first: dict | None = None) -> None:
        t0 = time.monotonic()
        self.log(f"来自 {session.remote} ({ip}) 的控制连接")
        msg = first
        while not self.stop_event.is_set():
            if msg is None:
                try:
                    msg = recv_json(ctrl)
                except ConnectionClosed:
                    break
            mtype = msg.get("type")

            if mtype == "manifest":
                if "files" not in msg:
                    # 对端在**问**我们的清单（同步收尾时用它重新对账、
                    # 决定该删哪些多余文件），而不是在给我们推清单。
                    if self.sync_responder is None:
                        from .sync import SyncResponder
                        self.sync_responder = SyncResponder(
                            self.sync_dir, allow_delete=self.allow_delete,
                            streams=DEFAULT_STREAMS, name=self.name,
                            on_log=self.on_log, on_progress=self.on_progress,
                            stop_event=self.stop_event)
                    self.sync_responder.handle(ctrl, msg, ip)
                    msg = None
                    continue
                files = msg.get("files") or []
                total = int(msg.get("total") or sum(
                    int(f.get("size", 0)) for f in files))
                with self._meter_lock:
                    self._meter = SpeedMeter(total)
                self._reset_diag()
                session.files = {str(f.get("path")): f for f in files}
                accept: list[str] = []
                for path, meta in session.files.items():
                    if not is_safe_relpath(path):
                        self.log(f"拒绝非法路径: {path!r}")
                        continue
                    accept.append(path)
                self.log(f"收到清单：{len(files)} 个文件 / {human_bytes(total)}"
                         f"，接受 {len(accept)} 个")
                send_json(ctrl, {"type": "manifest_ok", "accept": accept,
                                 "name": self.name, "port": self.port})
                self._emit()

            elif mtype == "file_begin":
                self._handle_file_begin(ctrl, session, msg)

            elif mtype == "file_end":
                self._handle_file_end(ctrl, session, msg)

            elif mtype == "done":
                self._state.save()
                send_json(ctrl, {
                    "type": "done_ok", "received": self.stats.files_received,
                    "skipped": self.stats.files_skipped,
                    "failed": [list(x) for x in self.stats.failed],
                })
                self.log(f"对方 {session.remote} 传输结束："
                         f"接收 {self.stats.files_received} / "
                         f"跳过 {self.stats.files_skipped} / "
                         f"失败 {len(self.stats.failed)}")
                break
            elif mtype == "ping":
                send_json(ctrl, {"type": "pong"})
            elif mtype in ("sync_offer", "sync_pull", "delete", "status",
                           "manifest"):
                if self.sync_responder is None:
                    from .sync import SyncResponder
                    self.sync_responder = SyncResponder(
                        self.sync_dir, allow_delete=self.allow_delete,
                        streams=DEFAULT_STREAMS, name=self.name,
                        on_log=self.on_log, on_progress=self.on_progress,
                        stop_event=self.stop_event)
                self.sync_responder.handle(ctrl, msg, ip)
            elif mtype == "quit":
                self.log("对端请求结束会话")
                break
            else:
                self.log(f"忽略未知消息: {mtype!r}")
            msg = None
        self.stats.elapsed += time.monotonic() - t0
        self._emit(phase="recv-idle")

    # -- file_begin -------------------------------------------------------
    def _handle_file_begin(self, ctrl: socket.socket, session: "_Session",
                           msg: dict) -> None:
        rel = str(msg.get("path") or "")
        if not is_safe_relpath(rel):
            send_json(ctrl, {"type": "file_reject", "path": rel,
                             "why": "非法路径（路径穿越）"})
            self.log(f"拒绝非法路径: {rel!r}")
            return
        try:
            size = int(msg.get("size", 0))
            mtime = float(msg.get("mtime", 0.0))
            chunks = int(msg.get("chunks") or chunks_for(size, self.chunk_size))
            chunk_size = int(msg.get("chunk_size") or self.chunk_size)
            expect = msg.get("sha256")
        except (TypeError, ValueError):
            send_json(ctrl, {"type": "file_reject", "path": rel, "why": "元数据非法"})
            return
        chunk_size = max(64 * 1024, chunk_size)

        target = resolve_under(self.dest, rel)
        part = target + PART_SUFFIX

        # 目标已存在且大小一致 → 大概率已完成，先做廉价比对再算 SHA
        if os.path.isfile(target) and os.path.getsize(target) == size:
            same = True
            if expect:
                if msg.get("sig"):
                    same = _sig_of(target, size) == msg["sig"]
                if same:
                    same = file_sha256(target) == expect
            elif msg.get("sig"):
                same = _sig_of(target, size) == msg["sig"]
            if same:
                self.stats.files_skipped += 1
                if expect:
                    with self._state_lock:
                        self._state.mark_done(rel, expect)
                send_json(ctrl, {"type": "file_skip", "path": rel,
                                 "why": "远端已有一致文件"})
                self.log(f"跳过（本地已一致）: {rel}")
                return

        os.makedirs(os.path.dirname(target) or self.dest, exist_ok=True)
        have: set[int] = set()
        if os.path.isfile(part):
            with self._state_lock:
                have = self._state.have_map(rel, size, chunks)
            try:
                part_size = os.path.getsize(part)
            except OSError:
                part_size = 0
            part_size = 0
            if not have and part_size == size:
                # 状态文件丢了但 .part 已经是完整长度：保守起见重新传一遍
                have = set()
        else:
            have = set()

        # 需要（重新）预分配 .part 的情况：
        #   * 文件不存在 / 长度不对
        #   * 状态里没有任何已完成分片（无从判断内容对不对，只能从头来）
        # 只有在状态明确记录了已完成分片时才敢复用。
        need_fresh = (not os.path.isfile(part)
                      or os.path.getsize(part) != size
                      or not have)
        if need_fresh:
            _preallocate(part, size)
            have = set()
        else:
            self.stats.bytes_resumed += sum(
                min(chunk_size, size - c * chunk_size) for c in have)
            self.log(f"续传 {rel}: 已有 {len(have)}/{chunks} 片 "
                     f"({human_bytes(self.stats.bytes_resumed)})")

        with self._state_lock:
            self._state.begin(rel, size, mtime, chunks)
            for c in have:
                self._state.mark_chunk(rel, c)
            self._state.save()

        session.pending[rel] = {
            "size": size, "mtime": mtime, "chunks": chunks,
            "chunk_size": chunk_size, "expect": expect, "have": set(have),
            "part": part, "target": target, "handle": None, "confirmed": 0,
            "lock": threading.Lock(),
        }
        send_json(ctrl, {
            "type": "file_begin_ok", "path": rel, "size": size,
            "chunks": chunks, "chunk_size": chunk_size, "have": sorted(have),
            "expect_sha256": expect,
            "resume_bytes": sum(min(chunk_size, size - c * chunk_size) for c in have),
        })
        self.log(f"开始接收 {rel} ({human_bytes(size)}), "
                 f"{chunks - len(have)}/{chunks} 片待传")
        # 数据流连接已在此前或此刻建立，交给 pull 线程

    # -- file_end ---------------------------------------------------------
    def _handle_file_end(self, ctrl: socket.socket, session: "_Session",
                         msg: dict) -> None:
        rel = str(msg.get("path") or "")
        info = session.pending.get(rel)
        if info is None:
            send_json(ctrl, {"type": "file_ack", "path": rel, "ok": False,
                             "why": "没有对应的 file_begin"})
            return
        try:
            if info["handle"] is not None:
                info["handle"].close()
        except OSError:
            pass
        info["handle"] = None

        # 整文件收尾：统一 fsync 一次（代替逐片 fsync），再做校验与原子改名
        try:
            fd = os.open(info["part"], os.O_RDWR | getattr(os, "O_BINARY", 0))
            try:
                t_fsync = time.monotonic()
                os.fsync(fd)
                with self._diag_lock:
                    self._disk_seconds += time.monotonic() - t_fsync
            finally:
                os.close(fd)
        except OSError:
            pass

        digest = self._verify(rel, info)
        expect = info.get("expect") or msg.get("sha256")
        ok = digest is not None and (not expect or digest == expect)
        if ok:
            self.stats.files_received += 1
            with self._state_lock:
                self._state.mark_done(rel, digest)
                self._state.save()
            try:
                os.replace(info["part"], info["target"])
                if info["mtime"]:
                    os.utime(info["target"], (info["mtime"], info["mtime"]))
            except OSError as exc:
                ok = False
                self.log(f"改名失败 {rel}: {exc}")
            if ok:
                self.log(f"完成 {rel}  校验通过  sha256={digest[:16]}…")
        if not ok:
            why = ("SHA-256 不一致" if digest and expect and digest != expect
                   else "分片缺失或读取失败")
            self.stats.failed.append((rel, why))
            with self._state_lock:
                self._state.drop(rel)
                self._state.save()
            self.log(f"校验失败 {rel}: {why}（已丢弃状态，可重传）")
        session.pending.pop(rel, None)
        send_json(ctrl, {"type": "file_ack", "path": rel, "ok": bool(ok),
                         "sha256": digest, "why": None if ok else why})
        self._emit()

    def _verify(self, rel: str, info: dict) -> str | None:
        """对 .part 全量算 SHA-256 前，先确认每个分片都已落盘。"""
        part = info["part"]
        if not os.path.isfile(part):
            return None
        if os.path.getsize(part) != info["size"]:
            return None
        with self._state_lock:
            e = self._state.entry(rel)
        have = set(e["have"]) if e else set()
        if len(have) != info["chunks"]:
            missing = sorted(set(range(info["chunks"])) - have)[:8]
            self.log(f"{rel} 缺少分片 {missing}…，不予校验")
            return None
        return file_sha256(part)

    # -- 数据流处理（每条流一个线程，接收 push 分片）----------------------
    def _pull_loop(self, conn: socket.socket, session: "_Session",
                   first: dict | None = None,
                   first_payload: bytes | None = None) -> None:
        head = first
        payload: bytes | None = first_payload
        # 连接诊断：一条数据连接 = 一条流（本次接收用了 N 条流、各自多少字节）
        sid = self._stream_bytes.new_stream()
        while not self.stop_event.is_set():
            if head is None:
                try:
                    head, payload = recv_frame(conn)
                except (ConnectionClosed, OSError):
                    break
            mtype = head.get("type")
            if mtype == "ping":
                try:
                    send_json(conn, {"type": "pong"})
                except OSError:
                    break
                head = None
                continue
            if mtype != "push":
                try:
                    send_json(conn, {"type": "error",
                                     "why": f"数据流不接受 {mtype!r} 消息"})
                except OSError:
                    break
                head = None
                continue
            rel = str(head.get("path") or "")
            info = session.pending.get(rel)
            try:
                index = int(head.get("index", -1))
                offset = int(head.get("offset", index * self.chunk_size))
                want = int(head.get("size", 0))
            except (TypeError, ValueError):
                head = None
                continue
            if info is None or not (0 <= index < info["chunks"]):
                try:
                    send_json(conn, {"type": "error", "path": rel,
                                     "index": index,
                                     "why": "未知文件或分片序号"})
                except OSError:
                    break
                head = None
                continue
            try:
                if payload is None:
                    raise ProtocolError("push 帧缺少数据")
                if len(payload) != want:
                    raise ProtocolError(
                        f"分片长度不符: {len(payload)} != {want}")
                if head.get("sha256") and head["sha256"] != chunk_sha256(payload):
                    raise ProtocolError("分片校验和错误")
                # 已经有的分片直接回 chunk_have，不做重复写
                if index in info["have"]:
                    send_chunk(conn, {"type": "chunk_have", "path": rel,
                                      "index": index, "offset": offset,
                                      "size": len(payload)}, b"")
                else:
                    self._write_chunk(info, rel, index, offset, payload)
                    send_chunk(conn, {"type": "chunk_ack", "path": rel,
                                      "index": index, "offset": offset,
                                      "size": len(payload)}, b"")
                self._stream_bytes.add(sid, len(payload))
            except (OSError, ProtocolError) as exc:
                if not self.stop_event.is_set():
                    self.log(f"分片 {rel}#{index} 出错: {exc}")
                try:
                    send_json(conn, {"type": "error", "path": rel,
                                     "index": index, "why": str(exc)})
                except OSError:
                    pass
                break
            head = None
            payload = None

    def _read_chunk(self, info: dict, offset: int, want: int) -> bytes | None:
        """按偏移读 ``.part``。

        用「每次新开句柄 + os.pread」而不是共享句柄 + seek：多条数据流并发时
        不会有 seek 竞态，也不受任何缓冲影响。
        """
        size = info["size"]
        if offset >= size:
            return b""
        length = min(want or self.chunk_size, size - offset)
        part = info["part"]
        try:
            fd = os.open(part, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except OSError:
            return None
        try:
            data = os.pread(fd, length, offset)
        except (OSError, AttributeError):
            try:
                with open(part, "rb") as fh:
                    fh.seek(offset)
                    data = fh.read(length)
            except OSError:
                return None
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        if len(data) != length:
            return None
        return data

    def _write_chunk(self, info: dict, rel: str, index: int, offset: int,
                     data: bytes) -> None:
        """把分片写到 ``.part`` 的指定偏移。

        每次 open("r+b") → seek → write → flush，写完立刻读回来核对；
        不一致就抛错，宁可让上层重传，也不要悄悄产出一个坏文件。
        （不做逐片 fsync：那会把吞吐压到磁盘同步速度；整文件收尾时统一 fsync 一次。）
        """
        if not data:
            return
        part = info["part"]
        written = 0
        back = b""
        # 实测落盘耗时：计时放在锁**里面**，只计真正做 I/O 的那段。多流是被
        # 这把锁串行化的，所以累加值不会超过整体墙钟时间；若把等锁时间也算
        # 进来，几条流就会重复计数（实测会把占比顶到 100%）。
        with info["lock"]:
            t_disk = time.monotonic()
            if not os.path.exists(part):
                _preallocate(part, info["size"])
            with open(part, "r+b") as fh:
                fh.seek(offset)
                written = fh.write(data)
                fh.flush()
                fh.seek(offset)
                back = fh.read(len(data))
            disk_dt = time.monotonic() - t_disk
        with self._diag_lock:
            self._disk_seconds += disk_dt
            self._disk_bytes += len(data)
        if written != len(data) or back != data:
            raise ProtocolError(
                f"{rel}#{index} 落盘校验失败: written={written}/{len(data)} "
                f"offset={offset} part={part}")
        with self._state_lock:
            self._state.mark_chunk(rel, index)
            # 从文件头部起确认写好的字节数：大分片写一半被 kill 也能少传
            self._state.mark_bytes_confirmed(
                rel, max(info.get("confirmed", 0), offset + len(data)))
            self._state.save()
        info["have"].add(index)
        info["confirmed"] = max(info.get("confirmed", 0), offset + len(data))
        # 测试钩子：模拟「写到一半被中断」，让自动化续传测试有个确定性的时点。
        # 不设 SWIFTDROP_STALL_AFTER_CHUNKS 时完全不生效。
        stall = os.environ.get("SWIFTDROP_STALL_AFTER_CHUNKS")
        if stall:
            self._chunks_written += 1
            try:
                limit = int(stall)
            except ValueError:
                limit = 0
            if limit > 0 and self._chunks_written == limit:
                self.log(f"[测试钩子] 已写 {limit} 片，暂停 "
                         f"{os.environ.get('SWIFTDROP_STALL_SECONDS', '2')} 秒等待中断")
                time.sleep(float(os.environ.get("SWIFTDROP_STALL_SECONDS", "2")))
        with self._meter_lock:
            self._meter.add(len(data))
        self._emit(current=rel, chunk=index)


class _Session:
    """一个发送端（按 IP 归并控制连接与数据流）对应的会话状态。"""

    def __init__(self, server: ReceiverServer, ip: str = ""):
        self.server = server
        self.ip = ip
        self.remote = ""
        self.files: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        self.closed = False
        self.lock = threading.Lock()


def _sig_of(path: str, size: int) -> str:
    try:
        return quick_signature(path, size)
    except OSError:
        return ""


# --------------------------------------------------------------------------
# 便捷入口
# --------------------------------------------------------------------------

def send_file(host: str, port: int, paths: Iterable[str], *,
              streams: int = DEFAULT_STREAMS, name: str | None = None,
              on_progress=None, on_log=None,
              stop_event: threading.Event | None = None) -> SendResult:
    return Sender(host, port, paths, streams=streams, name=name,
                  on_progress=on_progress, on_log=on_log,
                  stop_event=stop_event).run()


def recv_forever(dest_dir: str, port: int = 0, *,
                 on_progress=None, on_log=None,
                 stop_event: threading.Event | None = None,
                 name: str | None = None) -> ReceiverServer:
    srv = ReceiverServer(dest_dir, port, on_progress=on_progress,
                         on_log=on_log, stop_event=stop_event, name=name)
    srv.start()
    return srv
