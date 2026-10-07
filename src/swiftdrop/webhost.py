"""局域网静态文件服务（默认 8787），与 WebSocket 信令中继**共用同一个端口**。

* 根目录默认 ``D:\\文档\\ai001\\dist``（不存在则当前工作目录），首页 ``swiftdrop.html``。
* 正确 MIME：``.html/.js/.mjs/.wasm/.json/.css/.svg/...``。
* 支持 ``Range`` 请求（手机浏览器边下边播 / 断点续传）。
* 拒绝一切 ``..`` 穿越（realpath 前缀校验 + 只允许根目录之内）。
* 启动时打印所有局域网 IPv4 的可访问 URL，并带 ``#t=lan`` 片段，
  形如 ``http://192.168.1.5:8787/swiftdrop.html#t=lan``。
* 同端口下 ``/signal``（以及 ``/ws``）走 RFC6455 升级，交给 signaling.SignalRelay。
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import posixpath
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from . import APP_NAME, VERSION
from .discovery import all_local_ipv4
from .signaling import (
    SignalRelay,
    WebSocketError,
    WebSocketPeer,
    handshake_response,
    is_websocket_upgrade,
)

DEFAULT_PORT = 8787
DEFAULT_INDEX = "swiftdrop.html"
SIGNAL_PATHS = ("/signal", "/ws", "/signal/")
PROJECT_DIST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "dist")
# 小于这个大小的静态文件直接读进内存发送，**不持有文件句柄**：
# 这样覆盖安装时不会因为"文件正被使用"而写不进去。
IN_MEMORY_LIMIT = 8 * 1024 * 1024

# 静态资源 MIME 补齐（Windows 注册表有时把 .wasm/.js 配错）
EXTRA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".cjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".wasm": "application/wasm",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


def guess_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in EXTRA_TYPES:
        return EXTRA_TYPES[ext]
    ctype, _ = mimetypes.guess_type(path)
    return ctype or "application/octet-stream"


def default_root() -> str:
    if os.path.isdir(PROJECT_DIST):
        return PROJECT_DIST
    return os.getcwd()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET

    def __init__(self, addr, handler, *, root: str, index: str,
                 relay: SignalRelay, on_log=None, quiet: bool = False):
        super().__init__(addr, handler)
        self.root = os.path.abspath(root)
        self.index = index
        self.relay = relay
        self.on_log = on_log
        self.quiet = quiet


class _RangeReader:
    """把文件对象的读取限制在 [start, start+length) 区间内。

    http.server 的 ``copyfile`` 会从当前文件位置一直读到 EOF，
    所以用这一层把正文裁成请求的范围。
    """

    def __init__(self, fh, length: int, bufsize: int = 64 * 1024):
        self._fh = fh
        self._left = max(0, int(length))
        self._bufsize = bufsize

    def read(self, n: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        want = self._bufsize if n is None or n < 0 else min(n, self._bufsize)
        want = min(want, self._left)
        data = self._fh.read(want)
        self._left -= len(data)
        return data

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """解析单区间 ``Range: bytes=a-b``，返回 (start, length)。

    只支持单区间（手机浏览器只需要这个），越界返回 None 交给调用方回 416。
    """
    if not header:
        return None
    header = header.strip()
    if not header.lower().startswith("bytes="):
        return None
    spec = header[6:].split(",")[0].strip()      # 多区间只取第一段
    if "-" not in spec:
        return None
    start_s, _, end_s = spec.partition("-")
    start_s, end_s = start_s.strip(), end_s.strip()
    try:
        if not start_s:                          # bytes=-N 结尾 N 字节
            n = int(end_s)
            if n <= 0:
                return None
            n = min(n, size)
            return size - n, n
        start = int(start_s)
        if start >= size or start < 0:
            return None
        end = int(end_s) if end_s else size - 1
        end = min(end, size - 1)
        if end < start:
            return None
        return start, end - start + 1
    except ValueError:
        return None


class SwiftDropHandler(SimpleHTTPRequestHandler):
    """静态文件 + ``/signal`` WebSocket 升级 + Range 支持。"""

    # HTTP 头只能是 latin-1，所以 Server 头必须用 ASCII 标识；
    # 给浏览器/用户看的中文名在正文与 /info 里（见 APP_NAME）。
    server_version = f"ZongxianTransfer/{VERSION}"
    protocol_version = "HTTP/1.1"
    # 直接覆盖类属性，避免 SimpleHTTPRequestHandler 走当前工作目录
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, **EXTRA_TYPES}

    # -- 基础 -------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if getattr(self.server, "quiet", False):
            return
        self._log("%s - %s" % (self.address_string(), fmt % args))

    def _log(self, msg: str) -> None:
        cb = getattr(self.server, "on_log", None)
        if cb:
            try:
                cb(msg)
                return
            except Exception:
                pass
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()

    def _root(self) -> str:
        return getattr(self.server, "root", os.getcwd())

    # -- 安全路径 ---------------------------------------------------------
    def translate_path(self, path: str) -> str:
        """只允许根目录之内；``..`` / 绝对路径 / 盘符一律拒绝（返回哨兵）。"""
        root = self._root()
        path = urllib.parse.urlparse(path).path
        path = urllib.parse.unquote(path, errors="surrogatepass")
        path = posixpath.normpath(path)
        parts = [p for p in path.split("/") if p and p not in (".", "..")]
        candidate = os.path.join(root, *parts)
        real = os.path.realpath(candidate)
        root_real = os.path.realpath(root)
        if real != root_real and not real.startswith(root_real + os.sep):
            return os.path.join(root, "\x00__forbidden__")
        return real

    def _safe(self, path: str) -> bool:
        return "\x00" not in path

    # -- 升级 -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        clean = posixpath.normpath(parsed.path)
        if clean in SIGNAL_PATHS or clean.rstrip("/") in ("/signal", "/ws"):
            self._handle_signal()
            return
        if clean in ("/healthz", "/info"):
            self._json_info()
            return
        target = self.translate_path(self.path)
        if not self._safe(target):
            self._deny()
            return
        try:
            is_dir = os.path.isdir(target)
        except (OSError, ValueError):
            self.send_error(HTTPStatus.NOT_FOUND, "路径非法")
            return
        if is_dir:
            index = os.path.join(target, getattr(self.server, "index", DEFAULT_INDEX))
            try:
                has_index = os.path.isfile(index)
            except (OSError, ValueError):
                has_index = False
            if has_index:
                self.path = parsed.path.rstrip("/") + "/" + os.path.basename(index)
                return super().do_GET()
        return super().do_GET()

    def send_head(self):  # noqa: D102 - 覆写以补上 Range 支持
        """在标准静态响应之上加 Range：满足 206 / 416，并写 Content-Range。

        Python 3.12 的 http.server 并不处理 Range（该特性已被回退），
        而手机浏览器下载/播放时需要它，所以这里自己实现。
        """
        target = self.translate_path(self.path)
        if not self._safe(target):
            self._deny()
            return None
        try:
            if not os.path.isfile(target):
                return super().send_head()
            size = os.path.getsize(target)
        except (OSError, ValueError):
            return super().send_head()

        rng = parse_range(self.headers.get("Range"), size)
        if rng is None:
            if self.headers.get("Range"):
                # 语法合法但范围不可满足 → 416
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            # 完整响应也声明支持 Range，客户端才敢发 Range 请求
            previous = self.end_headers
            added = False

            def _end_headers_with_ranges() -> None:
                nonlocal added
                if not added:
                    added = True
                    try:
                        self.send_header("Accept-Ranges", "bytes")
                    except OSError:
                        pass
                previous()

            self.end_headers = _end_headers_with_ranges  # type: ignore[method-assign]
            return super().send_head()

        start, length = rng
        try:
            fh = open(target, "rb")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None
        fh.seek(start)
        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Content-Type", guess_type(target))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{start + length - 1}/{size}")
        self.send_header("Content-Length", str(length))
        self.send_header("Last-Modified", self.date_time_string(
            os.fstat(fh.fileno()).st_mtime))
        self.end_headers()
        if self.command == "HEAD":
            fh.close()
            return None
        # 小文件（网页版首页/说明书）直接读进内存再发：**不持有文件句柄**。
        # 否则浏览器/安装程序可能因为这个句柄报「无法打开要写入的文件」，
        # 导致覆盖安装时写不进 swiftdrop.html（实测踩到过）。
        if length <= IN_MEMORY_LIMIT:
            try:
                blob = fh.read(length)
            finally:
                fh.close()
            return io.BytesIO(blob)
        return _RangeReader(fh, length)

    def do_HEAD(self) -> None:  # noqa: N802
        """HEAD：只发头，不发正文；不做 WebSocket 升级。"""
        parsed = urllib.parse.urlparse(self.path)
        clean = posixpath.normpath(parsed.path)
        if clean in SIGNAL_PATHS:
            body = b"WebSocket endpoint\n"
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def _deny(self) -> None:
        body = f"403 路径越界：{APP_NAME} 静态服务只允许根目录之内的文件\n".encode()
        self.send_response(HTTPStatus.FORBIDDEN)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json_info(self) -> None:
        body = json.dumps({
            "app": APP_NAME, "version": VERSION, "root": self._root(),
            "index": getattr(self.server, "index", DEFAULT_INDEX),
            "signal": "/signal", "peers": self.server.relay.peer_count(),
            "urls": urls_for(self.server.server_address[1], self._root()),
        }, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- WebSocket 升级 ---------------------------------------------------
    def _handle_signal(self) -> None:
        if not is_websocket_upgrade(self.headers):
            body = (f"{APP_NAME} 信令中继在线。请用 WebSocket 连接本路径"
                    "（GET + Upgrade: websocket）。\n").encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        key = self.headers.get("Sec-WebSocket-Key")
        version = (self.headers.get("Sec-WebSocket-Version") or "").strip()
        if not key or version != "13":
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            body = b"missing Sec-WebSocket-Key or bad version\n"
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            self.wfile.write(handshake_response(key))
            self.wfile.flush()
        except OSError:
            return
        # 从这里开始这条连接不再属于 HTTP
        self.close_connection = True
        self._upgraded = True
        relay: SignalRelay = self.server.relay
        peer = WebSocketPeer(self.connection, self.client_address)
        relay.log(f"WebSocket 升级成功 {self.client_address}")
        # socket 的生杀交给 peer；把 handler 的书签清掉，避免它再去 flush/close
        self.connection = None
        self.rfile = None
        self.wfile = None
        relay.serve_peer(peer)

    # -- 升级之后不能再走 HTTP 的收尾流程 ----------------------------------
    # 升级后 rfile/wfile/connection 都交给了 WebSocketPeer，标准实现的
    # flush()/finish() 会拿 None 去调用而抛 AttributeError。
    # 同时 HTTP/1.1 的 keep-alive 由 handle_one_request() 自己根据请求头
    # 决定，所以这里不能事先把 close_connection 置位（置了会丢响应）。
    def handle(self) -> None:
        self.close_connection = True
        while self.rfile is not None:
            self.close_connection = False
            self.handle_one_request()
            if self.close_connection:
                return

    def handle_one_request(self) -> None:
        if self.wfile is None or self.rfile is None:
            self.close_connection = True
            return None
        try:
            return super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                TimeoutError, AttributeError, OSError):
            # WebSocket 升级后 socket 已经交给 peer，客户端关连接时这里会
            # 撞上已关闭的 wfile（AttributeError: NoneType.flush）；
            # 这是正常收尾，不该冒 traceback 到 stderr。
            self.close_connection = True
            self.wfile = None
            self.rfile = None
            return None

    def finish(self) -> None:
        if getattr(self, "_upgraded", False):
            return None
        # 连接已经交出去/已关闭时 wfile/rfile 会是 None：
        # 标准实现在这里会抛 AttributeError 并把 traceback 刷到控制台（虽然不影响服务），
        # 这里直接把收尾跳过，保持日志干净。
        if self.wfile is None or self.rfile is None:
            return None
        return super().finish()


# --------------------------------------------------------------------------
# URL 打印
# --------------------------------------------------------------------------

def urls_for(port: int, root: str | None = None,
             index: str = DEFAULT_INDEX) -> list[str]:
    """所有局域网 IPv4 的可访问 URL（带 #t=lan 片段）。"""
    page = index
    if root:
        real_index = os.path.join(root, index)
        if not os.path.isfile(real_index):
            # 首页不存在就退回根路径，至少能列目录
            page = ""
    base_path = f"/{page}" if page else "/"
    return [f"http://{ip}:{port}{base_path}#t=lan" for ip in all_local_ipv4()]


# --------------------------------------------------------------------------
# 服务封装
# --------------------------------------------------------------------------

class WebHost:
    """静态服务 + 信令中继（同端口）。可作为库使用，也可由 CLI 驱动。"""

    def __init__(self, root: str | None = None, port: int = DEFAULT_PORT,
                 index: str = DEFAULT_INDEX, *,
                 on_log: Callable[[str], None] | None = None,
                 quiet: bool = False, host: str = "0.0.0.0"):
        self.root = os.path.abspath(root or default_root())
        self.port = int(port)
        self.index = index or DEFAULT_INDEX
        self.host = host
        self.on_log = on_log
        self.quiet = quiet
        self.relay = SignalRelay(on_log=self._log_raw)
        self.httpd: _Server | None = None
        self._thread: threading.Thread | None = None

    # -- 日志 -------------------------------------------------------------
    def _log_raw(self, msg: str) -> None:
        if self.on_log:
            try:
                self.on_log(msg)
                return
            except Exception:
                pass
        if not self.quiet:
            print(f"[webhost] {msg}", flush=True)

    def log(self, msg: str) -> None:
        self._log_raw(msg)

    # -- 生命周期 ---------------------------------------------------------
    def make_server(self) -> _Server:
        handler = SwiftDropHandler
        httpd = _Server((self.host, self.port), handler, root=self.root,
                        index=self.index, relay=self.relay,
                        on_log=self.on_log, quiet=self.quiet)
        self.httpd = httpd
        self.port = httpd.server_address[1]
        return httpd

    def start(self, background: bool = True) -> "WebHost":
        httpd = self.make_server()
        if background:
            self._thread = threading.Thread(target=httpd.serve_forever,
                                            kwargs={"poll_interval": 0.3},
                                            name="swiftdrop-webhost", daemon=True)
            self._thread.start()
        return self

    def serve_forever(self, banner: bool = True) -> None:
        httpd = self.httpd or self.make_server()
        if banner:
            self.print_banner()
        try:
            httpd.serve_forever(poll_interval=0.3)
        except KeyboardInterrupt:
            self.log("收到 Ctrl+C，正在关闭…")
        finally:
            self.stop()

    def stop(self) -> None:
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
            try:
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None
        try:
            self.relay.shutdown()
        except Exception:
            pass

    def print_banner(self) -> None:
        root = self.root
        exists_index = os.path.isfile(os.path.join(root, self.index))
        self.log(f"根目录: {root}")
        self.log(f"首页  : {self.index}"
                 + ("" if exists_index else "（不存在，将回退为目录列表）"))
        self.log(f"信令  : ws://<本机IP>:{self.port}/signal")
        self.log("局域网可访问地址：")
        urls = urls_for(self.port, root, self.index)
        if not urls:
            self.log(f"  http://127.0.0.1:{self.port}/#{'t=lan'}")
        for url in urls:
            self.log(f"  {url}")
        if not exists_index:
            self.log(f"提示：把网页版放到 {root}\\{self.index} 即可直接打开上面的地址")


def create_server(root: str | None = None, port: int = DEFAULT_PORT,
                  index: str = DEFAULT_INDEX) -> _Server:
    """给测试用：只建服务不启动。"""
    host = WebHost(root, port, index, quiet=True)
    return host.make_server()


def serve(root: str | None = None, port: int = DEFAULT_PORT,
          index: str = DEFAULT_INDEX) -> None:
    WebHost(root, port, index).serve_forever()


if __name__ == "__main__":  # pragma: no cover
    serve()
