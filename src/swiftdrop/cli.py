"""棕仙的传输软件 命令行：参数解析与子命令。

::

    python -m swiftdrop gui
    python -m swiftdrop peers
    python -m swiftdrop recv  [--dir D] [--port 45880]
    python -m swiftdrop send  <ip|名称> <路径...> [--streams 4]
    python -m swiftdrop sync  <ip|名称> <本地目录> [--two-way|--one-way]
                              [--delete-extra] [--watch] [--interval 3]
    python -m swiftdrop webhost [--dir 目录] [--port 8787] [--html 路径]
    python -m swiftdrop relay [--port 8788]
    python -m swiftdrop autosync [--hidden] [--verbose] [--status]
    python -m swiftdrop autostart {on,off,status}
    python -m swiftdrop folders {add,remove,list}

进度一律用「单行原地刷新」的进度条 + MB/s + ETA，不刷屏。
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import socketserver
import sys
import threading
import time

from . import APP_NAME, VERSION
from .discovery import (
    DISCOVERY_PORT,
    DiscoveryService,
    list_peers,
    resolve_target,
)
from .protocol import (
    CHUNK_SIZE,
    DATA_PORT,
    DEFAULT_STREAMS,
    SpeedMeter,
    human_bytes,
    human_time,
)
from .signaling import SignalRelay, WebSocketPeer, handshake_response, is_websocket_upgrade
from .sync import SyncEngine, SyncResponder
from .transfer import ReceiverServer, Sender

# --------------------------------------------------------------------------
# 终端辅助
# --------------------------------------------------------------------------

class _TTY:
    """原地刷新的进度行；非 TTY 时自动退化为低频换行输出。"""

    def __init__(self, enabled: bool = True, interval: float = 0.2):
        self.enabled = enabled and sys.stdout.isatty()
        self.interval = interval
        self._last = 0.0
        self._lock = threading.Lock()
        self._line_len = 0

    def update(self, text: str, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            if not force and now - self._last < self.interval:
                return
            self._last = now
            if self.enabled:
                pad = max(0, self._line_len - len(text))
                sys.stdout.write("\r" + text + " " * pad)
                self._line_len = len(text)
            else:
                sys.stdout.write(text + "\n")
            sys.stdout.flush()

    def finish(self, text: str = "") -> None:
        with self._lock:
            if self.enabled and self._line_len:
                sys.stdout.write("\r" + " " * self._line_len + "\r")
            if text:
                sys.stdout.write(text + "\n")
            sys.stdout.flush()
            self._line_len = 0


def _banner(title: str) -> None:
    print(f"== {APP_NAME} {VERSION} · {title} ==", flush=True)


def _display_width(text: str) -> int:
    """粗略的显示宽度：宽字符（中文等）算 2 列。"""
    return sum(2 if ord(c) > 127 else 1 for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(1, width - _display_width(text))


def _chunk_bytes(chunk_mb: float | None) -> int:
    """把 --chunk-mb 转成字节；不传就用默认 4MB，上限 64MB。"""
    if not chunk_mb or chunk_mb <= 0:
        return CHUNK_SIZE
    from .protocol import MAX_CHUNK_SIZE
    return max(256 * 1024, min(int(chunk_mb * 1024 * 1024), MAX_CHUNK_SIZE))


def _print_peers(peers: list[dict], header: bool = True) -> None:
    if header:
        print(f"{'名称':<22} {'IP':<16} {'数据端口':<10} {'最后出现':<10} 来源",
              flush=True)
        print("-" * 74, flush=True)
    if not peers:
        print("(暂无设备)", flush=True)
        return
    for p in peers:
        name = p["name"][:20]
        print(f"{_pad(name, 22)}{p['ip']:<16} {p['port']:<10} "
              f"{p['age']:>6.1f}s   {p['source']}", flush=True)


# --------------------------------------------------------------------------
# peers
# --------------------------------------------------------------------------

def cmd_peers(args: argparse.Namespace) -> int:
    _banner("发现的设备")
    if args.once:
        peers = list_peers(timeout=args.timeout)
        _print_peers(peers)
        return 0
    if args.duration and args.duration > 0:
        # 定时模式：适合自动化验证 —— 跑够秒数就干净退出
        peers = list_peers(timeout=args.duration)
        _print_peers(peers)
        print(f"\n共 {len(peers)} 台设备（收集 {args.duration:.0f}s）", flush=True)
        return 0
    svc = DiscoveryService(data_port=args.data_port).start()
    print(f"发现服务监听 UDP {svc.port}（正在每秒广播 + 监听），"
          f"Ctrl+C 退出\n", flush=True)
    try:
        while True:
            peers = svc.peer_table()
            sys.stdout.write("\x1b[2J\x1b[H" if sys.stdout.isatty() else "")
            _print_peers(peers)
            print(f"\n刷新于 {time.strftime('%H:%M:%S')} · 共 {len(peers)} 台设备",
                  flush=True)
            time.sleep(2.0)
    except KeyboardInterrupt:
        print("\n退出。", flush=True)
    finally:
        svc.stop()
    return 0


# --------------------------------------------------------------------------
# recv
# --------------------------------------------------------------------------

def cmd_recv(args: argparse.Namespace) -> int:
    _banner("接收模式")
    dest = os.path.abspath(args.dir)
    os.makedirs(dest, exist_ok=True)
    tty = _TTY()
    stop = threading.Event()

    disco = DiscoveryService(data_port=args.port).start()
    srv = ReceiverServer(
        dest, args.port, name=args.name, allow_delete=args.delete_extra,
        sync_dir=args.sync_dir or dest,
        chunk_size=_chunk_bytes(args.chunk_mb),
        on_log=lambda m: tty.finish(f"[接收] {m}"),
        on_progress=None, stop_event=stop)
    srv.start()
    print(f"接收目录: {dest}", flush=True)
    print(f"监听端口: {args.port}   （发现端口 UDP {disco.port}）", flush=True)
    print("等待发送端连接…  Ctrl+C 退出\n", flush=True)

    meter_lock = threading.Lock()
    meter = SpeedMeter(0)

    def progress(ev: dict) -> None:
        nonlocal meter
        with meter_lock:
            if ev.get("total") and ev["total"] != meter.total:
                meter = SpeedMeter(int(ev["total"]))
                meter.add(int(ev.get("done", 0)))
            text = ("接收 " + meter.line()
                    + f"  文件 {ev.get('files_received', 0)}"
                    + f" 跳过 {ev.get('files_skipped', 0)}"
                    + f" 失败 {ev.get('failed', 0)}")
        tty.update(text)

    srv.on_progress = progress
    try:
        while not stop.is_set():
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在关闭…", flush=True)
    finally:
        stop.set()
        srv.stop()
        disco.stop()
        tty.finish()
        print(f"结束。共接收 {srv.stats.files_received} 个文件，"
              f"跳过 {srv.stats.files_skipped} 个，"
              f"失败 {len(srv.stats.failed)} 个。", flush=True)
    return 0


# --------------------------------------------------------------------------
# send
# --------------------------------------------------------------------------

def cmd_group(args: argparse.Namespace) -> int:
    """异地组网：认出虚拟局域网地址，给出可直接用的发送命令。"""
    _banner("异地组网（虚拟局域网）")
    from .netgroup import group_nics, lan_ips, share_url

    nics = group_nics()
    if not nics:
        print("没检测到异地组网网卡。", flush=True)
        print("", flush=True)
        from .netgroup import install_lines

        for line in install_lines():
            print(line, flush=True)
        print("", flush=True)
        print("（另一种选择：双方都在严格 NAT 时用手机热点，或自建中继。详见使用说明。）", flush=True)
        return 1

    print("检测到的异地组网地址：", flush=True)
    for n in nics:
        print(f"  {n.tool:<14} {n.ip}", flush=True)
    print("", flush=True)
    print(f"说明：{nics[0].hint}", flush=True)
    print("用法（对方也在这个组网里，用上面的地址）：", flush=True)
    page = "swiftdrop.html"
    for n in nics:
        print(f"  接收端: 棕仙的传输软件.exe recv --dir D:\\收件", flush=True)
        print(f"  发送端: 棕仙的传输软件.exe send {n.ip} <文件或目录>", flush=True)
        print(f"  网页版: {share_url(n.ip, args.port, page)}", flush=True)
        break
    lan = lan_ips()
    if lan:
        print("", flush=True)
        print("同一 WiFi 下还可以用局域网地址：" + ", ".join(lan), flush=True)
    print("", flush=True)
    print("提示：跨网延迟高时程序会自动提高并发流（最多 8 条）来吃满带宽；"
          "速度上限仍然是发送方的上行带宽。", flush=True)
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    _banner("发送模式")
    host, port = resolve_target(args.target, timeout=args.timeout)
    if args.port:
        port = args.port
    # 并发流：不指定就按到目标的建连延迟自动选（跨网/异地组网多开几条，吃满管道）
    streams = args.streams
    if not streams:
        try:
            from .netgroup import best_streams, tool_of

            streams = best_streams(host, port, timeout=min(3.0, args.timeout or 3.0))
            tool, _hint = tool_of(host)
            if tool and streams > DEFAULT_STREAMS:
                print(f"检测到异地组网（{tool}）：按延迟自动提高到 {streams} 条并发流", flush=True)
        except Exception:
            streams = DEFAULT_STREAMS
    paths = [os.path.abspath(p) for p in args.paths]
    for p in paths:
        if not os.path.exists(p):
            print(f"错误：路径不存在 {p}", file=sys.stderr, flush=True)
            return 2
    total = 0
    for p in paths:
        if os.path.isfile(p):
            total += os.path.getsize(p)
        else:
            for root, _d, files in os.walk(p):
                for fn in files:
                    try:
                        total += os.path.getsize(os.path.join(root, fn))
                    except OSError:
                        pass
    print(f"目标: {host}:{port}   分片: {human_bytes(CHUNK_SIZE)}   "
          f"并发流: {streams}   总量: {human_bytes(total)}", flush=True)
    tty = _TTY()
    stop = threading.Event()
    t0 = time.monotonic()
    try:
        sender = Sender(host, port, paths, streams=streams, name=args.name,
                        retries=args.retries,
                        chunk_size=_chunk_bytes(args.chunk_mb),
                        on_log=lambda m: tty.finish(f"[发送] {m}"),
                        on_progress=lambda ev: tty.update("发送 " + _fmt_ev(ev)),
                        stop_event=stop)
        result = sender.run()
    except KeyboardInterrupt:
        stop.set()
        print("\n收到 Ctrl+C，已中断。", flush=True)
        return 130
    except (OSError, LookupError, ValueError) as exc:
        tty.finish()
        print(f"发送失败：{exc}", file=sys.stderr, flush=True)
        return 1
    tty.finish()
    print(f"完成：成功 {result.sent_files} 个 / 跳过 {result.skipped_files} 个 / "
          f"失败 {len(result.failed_files)} 个", flush=True)
    print(f"实传 {human_bytes(result.bytes_sent)}，"
          f"续传跳过 {human_bytes(result.bytes_skipped)}，"
          f"耗时 {human_time(time.monotonic() - t0)}，"
          f"平均 {human_bytes(result.rate)}/s", flush=True)
    for rel, why in result.failed_files:
        print(f"  失败: {rel} —— {why}", flush=True)
    return 0 if result.ok else 1


def _fmt_ev(ev: dict) -> str:
    total = int(ev.get("total", 0) or 0)
    done = int(ev.get("done", 0) or 0)
    rate = float(ev.get("rate", 0.0) or 0.0)
    frac = 100.0 if total <= 0 else min(100.0, done * 100.0 / total)
    width = 26
    filled = int((0.0 if total <= 0 else done / total) * width)
    bar = "#" * min(width, filled) + "-" * max(0, width - filled)
    eta = (total - done) / rate if rate > 1e-6 else float("inf")
    extra = ""
    if "sent_files" in ev:
        extra = f" 文件 {ev['sent_files']} 跳过 {ev['skipped_files']} 失败 {ev['failed']}"
    elif "files_received" in ev:
        extra = f" 文件 {ev['files_received']} 跳过 {ev['files_skipped']} 失败 {ev['failed']}"
    return (f"[{bar}] {frac:5.1f}%  {human_bytes(done)}/{human_bytes(total)}  "
            f"{human_bytes(rate)}/s  ETA {human_time(eta)}{extra}")


# --------------------------------------------------------------------------
# sync
# --------------------------------------------------------------------------

def _start_local_receiver(dest: str, port: int, args: argparse.Namespace,
                          tty: "_TTY", stop: threading.Event,
                          on_log=None) -> ReceiverServer:
    """起本机接收端；端口被占用时自动退到系统分配的端口而不是直接失败。"""
    on_log = on_log or (lambda m: tty.finish(f"[接收] {m}"))
    try:
        srv = ReceiverServer(
            dest, port, name=args.name, allow_delete=args.delete_extra,
            sync_dir=dest, chunk_size=_chunk_bytes(getattr(args, "chunk_mb", None)),
            on_log=on_log,
            on_progress=lambda ev: tty.update("回传 " + _fmt_ev(ev)),
            stop_event=stop)
        srv.start()
        return srv
    except OSError as exc:
        if port == 0:
            raise
        tty.finish(f"[警告] 端口 {port} 不可用（{exc}），改用临时端口")
        srv = ReceiverServer(
            dest, 0, name=args.name, allow_delete=args.delete_extra,
            sync_dir=dest, chunk_size=_chunk_bytes(getattr(args, "chunk_mb", None)),
            on_log=on_log,
            on_progress=lambda ev: tty.update("回传 " + _fmt_ev(ev)),
            stop_event=stop)
        srv.start()
        return srv


def cmd_sync(args: argparse.Namespace) -> int:
    _banner("目录同步")
    host, port = resolve_target(args.target, timeout=args.timeout)
    if args.port:
        port = args.port
    local = os.path.abspath(args.dir)
    if not os.path.isdir(local):
        print(f"错误：本地目录不存在 {local}", file=sys.stderr, flush=True)
        return 2
    two_way = not args.one_way
    tty = _TTY()
    stop = threading.Event()

    # 本机同时开启接收端：既能作为同步被动方（对端 push），也能接收回传
    disco = None
    srv = None
    if not args.no_recv:
        disco = DiscoveryService(data_port=args.port or DATA_PORT).start()
        srv = _start_local_receiver(
            local, args.port or 0, args, tty, stop,
            on_log=lambda m: tty.finish(f"[同步-接收] {m}"))
        print(f"本机接收/被动同步端口: {srv.port}", flush=True)

    engine = SyncEngine(local, host, port, two_way=two_way,
                        delete_extra=args.delete_extra, streams=args.streams,
                        name=args.name,
                        on_log=lambda m: tty.finish(f"[同步] {m}"),
                        on_progress=lambda ev: tty.update("同步 " + _fmt_ev(ev)),
                        stop_event=stop)
    mode = "双向" if two_way else "单向（本地覆盖远端）"
    print(f"目标: {host}:{port}   模式: {mode}   "
          f"删除多余: {'开' if args.delete_extra else '关'}", flush=True)

    rc = 0
    try:
        if args.watch:
            engine.watch(interval=args.interval)
        else:
            report = engine.run_once()
            tty.finish()
            print("本轮完成: " + report.summary(), flush=True)
            for c in report.conflicts:
                print(f"  冲突跳过: {c['path']} —— {c['why']}", flush=True)
            for rel, why in report.failed:
                print(f"  失败: {rel} —— {why}", flush=True)
            rc = 0 if not report.failed else 1
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在退出…", flush=True)
        stop.set()
    except (OSError, LookupError, ValueError) as exc:
        tty.finish()
        print(f"同步失败：{exc}", file=sys.stderr, flush=True)
        rc = 1
    finally:
        stop.set()
        if srv is not None:
            srv.stop()
        if disco is not None:
            disco.stop()
        tty.finish()
    return rc


# --------------------------------------------------------------------------
# webhost / relay
# --------------------------------------------------------------------------

def cmd_webhost(args: argparse.Namespace) -> int:
    _banner("局域网静态服务 + 信令中继")
    from .webhost import DEFAULT_INDEX, WebHost, default_root

    root = os.path.abspath(args.dir) if args.dir else default_root()
    if not os.path.isdir(root):
        print(f"警告：目录不存在 {root}，改用当前目录", file=sys.stderr, flush=True)
        root = os.getcwd()
    index = DEFAULT_INDEX
    if args.html:
        html = os.path.abspath(args.html)
        if not os.path.isfile(html):
            print(f"错误：--html 指定的文件不存在 {html}", file=sys.stderr, flush=True)
            return 2
        if os.path.dirname(html) != root:
            # 把首页拷到根目录，保证 URL 仍然简单
            target = os.path.join(root, DEFAULT_INDEX)
            try:
                shutil.copyfile(html, target)
                print(f"已复制首页 {html} → {target}", flush=True)
            except OSError as exc:
                print(f"复制首页失败：{exc}", file=sys.stderr, flush=True)
                return 2
    host = WebHost(root, args.port, index)
    host.make_server()                 # 先绑定，端口可能被换成实际值
    host.print_banner()
    try:
        host.serve_forever(banner=False)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_webapp(args: argparse.Namespace) -> int:
    """跨网传输：起内置服务，并用系统 Chromium（Edge/Chrome）的应用窗口打开网页版。

    这一步就是「把网页版装进桌面版」：窗口里跑的是原版网页端，
    因此跨网 P2P 直连、文件夹选择、文件夹同步全部照旧可用；
    同时页面还能通过同源的 /signal 做局域网信令，同一个 WiFi 下手机浏览器也能连。
    """
    _banner("跨网传输（内置网页版）")
    from .webhost import DEFAULT_INDEX, WebHost, default_root, urls_for
    from . import webview

    root = os.path.abspath(args.dir) if args.dir else default_root()
    if not os.path.isdir(root):
        print(f"警告：目录不存在 {root}，改用当前目录", file=sys.stderr, flush=True)
        root = os.getcwd()
    if not os.path.isfile(os.path.join(root, DEFAULT_INDEX)):
        print(f"错误：没找到首页 {os.path.join(root, DEFAULT_INDEX)}", file=sys.stderr, flush=True)
        return 2

    host = WebHost(root, args.port, DEFAULT_INDEX, quiet=bool(args.quiet))
    try:
        host.start(background=True)      # 绑定端口 + 后台线程服务
    except OSError as exc:
        print(f"内置服务启动失败：{exc}", file=sys.stderr, flush=True)
        print("端口可能被占用，换一个试试：webapp --port 8890", file=sys.stderr, flush=True)
        return 2
    lan = urls_for(host.port, host.root)
    local = f"http://127.0.0.1:{host.port}/{DEFAULT_INDEX}"
    # 分享地址优先用**异地组网**地址（Radmin/Tailscale 等）：异地朋友只有这个能打开；
    # 局域网地址作为附带提示一起传给页面。
    from urllib.parse import quote

    from .netgroup import primary_group, share_url
    group = primary_group()
    lan_local = lan[0].split("#")[0] if lan else ""
    if group:
        share = share_url(group.ip, host.port, DEFAULT_INDEX)
        local_win = local + "?lan=" + quote(share, safe="")
        if lan_local:
            local_win += "&lanlocal=" + quote(lan_local, safe="")
    elif lan_local:
        local_win = local + "?lan=" + quote(lan_local, safe="")
    else:
        local_win = local
    print("=" * 64, flush=True)
    print("内置网页版已就绪（跨网 P2P 传输 / 文件夹同步都在这个窗口里操作）", flush=True)
    print(f"  本机窗口地址   : {local}", flush=True)
    if group:
        print(f"  异地组网地址   : {share}   ← 已在同一组网（{group.tool}）的朋友用这个", flush=True)
    print("  同一个 WiFi 下，手机/别的电脑用浏览器直接打开：", flush=True)
    for u in lan:
        print(f"    {u}", flush=True)
    if not group:
        print("  提示：想异地直连，装个免费组网工具（推荐 Tailscale）或加 Radmin 组网，", flush=True)
        print("        然后运行 `棕仙的传输软件.exe group` 看组网地址。", flush=True)
    print("  " + webview.describe(), flush=True)
    print("  提示：生成取件码后，把 9 位取件码发给朋友（对方也用本软件/网页版输入）即可跨网直连。", flush=True)
    print("=" * 64, flush=True)

    proc = None
    try:
        proc, _exe = webview.open_app_window(local_win, size=(args.width, args.height),
                                             use_profile=not args.no_profile,
                                             force_app_window=bool(args.app_window))
        # proc 为 None 表示已交给系统用默认浏览器打开，不需要也不应该去等它
        if proc is not None and webview.exited_quickly(proc, 3.0):
            print("浏览器进程很快退出了，改用系统默认浏览器打开…", flush=True)
            webview._open_default(local_win)
            proc = None
    except Exception as exc:                                # noqa: BLE001
        print(f"打开浏览器窗口失败（{exc}）——服务照常运行，请手动用浏览器打开上面的地址。",
              flush=True)
    if proc is None:
        print("已用系统默认浏览器打开。若没弹出，请手动打开上面的地址。", flush=True)
    else:
        print("已用 Edge/Chrome 的应用窗口打开。", flush=True)
    print("内置服务运行中：直接关掉这个控制台窗口即可结束（或按 Ctrl+C）。", flush=True)
    # 关键：不能等浏览器进程结束——它随时可能把窗口交给已有实例后自身退出。
    # 服务一直跑，直到用户关掉控制台 / Ctrl+C。
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            host.stop()
        except Exception:
            pass
    return 0


class _RelayTCPHandler(socketserver.BaseRequestHandler):
    """裸 TCP：自己解析 HTTP 升级请求，成功就交给 SignalRelay。"""

    def handle(self) -> None:  # noqa: D401
        relay: SignalRelay = self.server.relay          # type: ignore[attr-defined]
        sock = self.request
        sock.settimeout(15)
        try:
            data = b""
            while b"\r\n\r\n" not in data and len(data) < 16384:
                piece = sock.recv(4096)
                if not piece:
                    return
                data += piece
        except OSError:
            return
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if not lines:
            return
        request_line = lines[0].split()
        path = request_line[1] if len(request_line) > 1 else "/"
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
        if "websocket" not in headers.get("upgrade", "").lower():
            body = (f"{APP_NAME} 信令中继：请用 WebSocket 连接 "
                    f"{path}\n").encode("utf-8")
            sock.sendall(b"HTTP/1.1 426 Upgrade Required\r\n"
                         b"Content-Type: text/plain; charset=utf-8\r\n"
                         b"Sec-WebSocket-Version: 13\r\n"
                         + f"Content-Length: {len(body)}\r\n\r\n".encode()
                         + body)
            return
        key = headers.get("sec-websocket-key")
        if not key:
            sock.sendall(b"HTTP/1.1 400 Bad Request\r\n"
                         b"Content-Length: 12\r\n\r\nbad request\n")
            return
        sock.sendall(handshake_response(key))
        peer = WebSocketPeer(sock, self.client_address)
        if rest:
            # 理论上升级请求后不该有数据；有就丢弃已缓冲的一点点
            pass
        relay.serve_peer(peer)


class _RelayServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET

    def __init__(self, addr, handler, relay: SignalRelay):
        self.relay = relay
        super().__init__(addr, handler)


def cmd_relay(args: argparse.Namespace) -> int:
    _banner("WebSocket 信令中继")
    relay = SignalRelay(on_log=lambda m: print(f"[relay] {m}", flush=True)
                        if args.verbose else None)
    srv = _RelayServer(("0.0.0.0", args.port), _RelayTCPHandler, relay)
    port = srv.server_address[1]
    print(f"监听 ws://0.0.0.0:{port}/signal 和 /ws", flush=True)
    from .discovery import all_local_ipv4
    for ip in all_local_ipv4():
        print(f"  ws://{ip}:{port}/signal#t=lan", flush=True)
    try:
        srv.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在关闭…", flush=True)
    finally:
        relay.shutdown()
        srv.server_close()
    return 0


# --------------------------------------------------------------------------
# autosync / autostart / folders（开机自启同步）
# --------------------------------------------------------------------------

def cmd_autosync(args: argparse.Namespace) -> int:
    _banner("开机自启同步（无界面守护进程）")
    from .autostart import run_daemon

    argv: list[str] = []
    if args.hidden:
        argv.append("--hidden")
    if args.verbose:
        argv.append("--verbose")
    if args.status:
        argv.append("--status")
    if args.once:
        argv.append("--once")
    if args.interval:
        argv += ["--interval", str(args.interval)]
    if args.discover_timeout:
        argv += ["--discover-timeout", str(args.discover_timeout)]
    return run_daemon(argv)


def _report_autostart() -> None:
    from . import paths
    from .autostart import autostart_status, load_config

    st = autostart_status()
    cfg = load_config()
    print(f"{APP_NAME} {VERSION} 开机自启状态", flush=True)
    print(f"注册表位置 : {st['key']}", flush=True)
    print(f"值名       : {st['value_name']}", flush=True)
    print(f"当前状态   : {'已开启' if st['enabled'] else '未开启'}", flush=True)
    print(f"注册表命令 : {st['command'] or '(无)'}", flush=True)
    print(f"应当的命令 : {st['expected']}", flush=True)
    print(f"命令是否一致: {'是' if st['matches'] else '否'}", flush=True)
    print(f"配置文件   : {paths.config_path()}", flush=True)
    print(f"配置里标记 : {'autostart=true' if st['config_says'] else 'autostart=false'}",
          flush=True)
    folders = cfg.get("folders") or []
    print(f"已登记文件夹: {len(folders)} 个", flush=True)
    for entry in folders:
        target = entry.get("peer") or entry.get("host") or "(未指定对端)"
        print(f"  - {entry['local']}  →  {target}  [{entry.get('mode')}, "
              f"每 {entry.get('interval')}s, 删除多余 "
              f"{'开' if entry.get('delete_extra') else '关'}]", flush=True)


def cmd_autostart(args: argparse.Namespace) -> int:
    _banner("开机自启开关")
    from . import paths
    from .autostart import (
        autostart_status,
        disable_autostart,
        enable_autostart,
        load_config,
    )

    action = args.action
    if action == "on":
        cmd = enable_autostart()
        print(f"已登记开机自启（值名「{APP_NAME}」）", flush=True)
        print(f"  注册表: HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run",
              flush=True)
        print(f"  命令  : {cmd}", flush=True)
        print(f"  配置  : {paths.config_path()}", flush=True)
        return 0
    if action == "off":
        removed = disable_autostart()
        print("已取消开机自启" + ("（注册表项已删除）" if removed
                                  else "（注册表里本来就没有）"), flush=True)
        cfg = load_config()
        if cfg.get("folders"):
            print(f"提示：仍登记着 {len(cfg['folders'])} 个同步文件夹，"
                  f"可用 `folders list` 查看、`folders remove` 清理", flush=True)
        return 0
    _report_autostart()
    return 0


def _fmt_folder_row(entry: dict) -> str:
    target = entry.get("peer") or entry.get("host") or "(未指定)"
    interval = f"每 {entry.get('interval')}s"
    return (f"{_pad(str(entry.get('local')), 38)} {_pad(str(target), 20)} "
            f"{_pad(str(entry.get('mode')), 10)} {_pad(interval, 10)} "
            f"{_pad('开' if entry.get('delete_extra') else '关', 10)} "
            f"{'开' if entry.get('mark_icon') else '关'}")


def cmd_folders(args: argparse.Namespace) -> int:
    _banner("开机同步的文件夹")
    from . import paths
    from .autostart import (
        add_folder,
        list_folders,
        load_config,
        remove_folder,
        save_config,
    )

    action = args.action
    if action == "list":
        folders = list_folders()
        if args.json:
            import json
            print(json.dumps({"config": paths.config_path(),
                              "folders": folders}, ensure_ascii=False, indent=1),
                  flush=True)
            return 0
        print(f"配置文件: {paths.config_path()}", flush=True)
        print(f"设备名  : {load_config().get('device_name')}", flush=True)
        if not folders:
            print("(还没有登记任何文件夹；用 `folders add` 或 GUI 勾选"
                  "「这个文件夹开机自动同步」)", flush=True)
            return 0
        print(f"{_pad('本地文件夹', 38)} {_pad('对端设备', 20)} "
              f"{_pad('模式', 10)} {_pad('轮询', 10)} {_pad('删除多余', 10)} 图标标记",
              flush=True)
        print("-" * 106, flush=True)
        for entry in folders:
            print(_fmt_folder_row(entry), flush=True)
        print(f"\n共 {len(folders)} 个文件夹", flush=True)
        return 0

    if action == "add":
        local = os.path.abspath(os.path.expandvars(args.local))
        if not os.path.isdir(local):
            print(f"错误：本地文件夹不存在 {local}", file=sys.stderr, flush=True)
            return 2
        if not args.peer and not args.host:
            print("错误：至少要给 --peer（设备名）或 --host（IP）",
                  file=sys.stderr, flush=True)
            return 2
        entry = add_folder({
            "local": local,
            "peer": args.peer or "",
            "host": args.host or "",
            "port": args.port,
            "mode": "one-way" if args.one_way else "two-way",
            "delete_extra": bool(args.delete_extra),
            "interval": args.interval,
            "mark_icon": not args.no_mark_icon,
        })
        print(f"已登记同步文件夹: {entry['local']}", flush=True)
        print(f"  对端  : {entry.get('peer') or entry.get('host')}"
              f"（端口 {entry.get('port')}）", flush=True)
        print(f"  模式  : {entry.get('mode')}，每 {entry.get('interval')}s",
              flush=True)
        if entry.get("mark_icon"):
            try:
                from .foldericon import mark_folder, resolve_icon, reveal_hint
                ico = resolve_icon()
                if ico:
                    mark_folder(entry["local"], ico)
                    print(f"  图标  : 已标记（{ico}）；{reveal_hint()}", flush=True)
                else:
                    print("  图标  : 未找到 ico 文件，已跳过标记", flush=True)
            except Exception as exc:                       # noqa: BLE001
                print(f"  图标  : 标记失败，已跳过：{exc}", flush=True)
        if not args.no_autostart:
            from .autostart import enable_autostart
            cmd = enable_autostart()
            print(f"  自启  : 已开启（{cmd}）", flush=True)
        return 0

    if action == "remove":
        local = os.path.abspath(os.path.expandvars(args.local))
        removed = remove_folder(local)
        if not removed:
            print(f"配置里没有这个文件夹：{local}", file=sys.stderr, flush=True)
            return 2
        print(f"已移除登记: {local}", flush=True)
        if args.keep_icon:
            print("按 --keep-icon 要求，保留了文件夹图标标记", flush=True)
        else:
            try:
                from .foldericon import reveal_hint, unmark_folder
                if unmark_folder(local):
                    print(f"  图标  : 已取消标记；{reveal_hint()}", flush=True)
            except Exception as exc:                       # noqa: BLE001
                print(f"  图标  : 取消标记失败，已跳过：{exc}", flush=True)
        cfg = load_config()
        if not (cfg.get("folders") or []):
            from .autostart import disable_autostart
            disable_autostart()
            print("已经没有登记的文件夹，顺带关掉开机自启", flush=True)
        return 0

    save_config(load_config())
    return 0


# --------------------------------------------------------------------------
# gui
# --------------------------------------------------------------------------

def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from . import gui
    except ImportError as exc:
        print(f"tkinter 不可用（{exc}），已回落到命令行模式。\n"
              f"可用命令：peers / recv / send / sync / webhost / relay / "
              f"autosync / autostart / folders",
              file=sys.stderr, flush=True)
        return cmd_help_fallback()
    return gui.main(port=args.port, dir=args.dir)


def cmd_help_fallback() -> int:
    print("用法: python -m swiftdrop "
          "{gui,peers,recv,send,sync,webhost,relay,autosync,autostart,folders}",
          flush=True)
    return 1


# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=APP_NAME,
        description=f"{APP_NAME} {VERSION} —— 朋友间大文件传输 + 文件夹同步"
                    f"（纯标准库；源码包名 swiftdrop）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    p.add_argument("--version", action="version",
                   version=f"{APP_NAME} {VERSION}")
    sub = p.add_subparsers(
        dest="cmd",
        metavar="{gui,peers,recv,send,sync,webhost,relay,autosync,autostart,folders}")

    g = sub.add_parser("gui", help="打开图形界面")
    g.add_argument("--port", type=int, default=DATA_PORT, help="接收/数据端口")
    g.add_argument("--dir", default=None, help="默认目录")
    g.set_defaults(func=cmd_gui)

    q = sub.add_parser("peers", help="列出发现的设备")
    q.add_argument("--timeout", type=float, default=3.0, help="收集秒数（默认 3）")
    q.add_argument("--once", action="store_true", help="只打印一次就退出")
    q.add_argument("--duration", type=float, default=0.0,
                   help="收集 N 秒后退出（0 = 持续刷新，Ctrl+C 退出）")
    q.add_argument("--data-port", type=int, default=DATA_PORT, help="广播的数据端口")
    q.set_defaults(func=cmd_peers)

    g = sub.add_parser("group", help="异地组网：识别 Radmin/Tailscale 等虚拟局域网地址")
    g.add_argument("--port", type=int, default=8787, help="网页版端口（默认 8787）")
    g.set_defaults(func=cmd_group)

    r = sub.add_parser("recv", help="进入接收模式")
    r.add_argument("--dir", default=os.path.join(os.getcwd(), "swiftdrop-recv"),
                   help="接收目录")
    r.add_argument("--port", type=int, default=DATA_PORT, help="数据端口（默认 45880）")
    r.add_argument("--chunk-mb", type=float, default=None,
                   help="本端接收分片大小（MB，默认 4）")
    r.add_argument("--name", default=None, help="对外显示的设备名")
    r.add_argument("--delete-extra", action="store_true",
                   help="允许对端要求删除多余文件（同步用）")
    r.add_argument("--sync-dir", default=None, help="被动同步目录（默认同 --dir）")
    r.set_defaults(func=cmd_recv)

    s = sub.add_parser("send", help="发送文件/文件夹")
    s.add_argument("target", help="ip、ip:端口 或设备名")
    s.add_argument("paths", nargs="+", help="一个或多个文件/目录")
    s.add_argument("--port", type=int, default=None, help="覆盖数据端口")
    s.add_argument("--streams", type=int, default=None,
                   help=f"并发流数 1-8（默认按到目标的延迟自动选：局域网 {DEFAULT_STREAMS} 条、"
                        f"跨网/异地组网最多 8 条）")
    s.add_argument("--retries", type=int, default=3, help="单文件重试次数")
    s.add_argument("--chunk-mb", type=float, default=None,
                   help="分片大小（MB，默认 4）")
    s.add_argument("--timeout", type=float, default=3.0, help="设备名解析超时")
    s.add_argument("--name", default=None, help="本机显示名")
    s.set_defaults(func=cmd_send)

    y = sub.add_parser("sync", help="目录同步")
    y.add_argument("target", help="ip、ip:端口 或设备名")
    y.add_argument("dir", help="本地目录")
    mode = y.add_mutually_exclusive_group()
    mode.add_argument("--two-way", action="store_true", default=True,
                      help="双向同步（默认）")
    mode.add_argument("--one-way", action="store_true",
                      help="单向：本地覆盖远端")
    y.add_argument("--delete-extra", action="store_true",
                   help="删除另一边没有的文件（默认关）")
    y.add_argument("--watch", action="store_true", help="持续监控同步")
    y.add_argument("--interval", type=float, default=3.0, help="监控轮询秒数")
    y.add_argument("--streams", type=int, default=DEFAULT_STREAMS, help="并发流数")
    y.add_argument("--port", type=int, default=None, help="覆盖数据端口")
    y.add_argument("--timeout", type=float, default=3.0, help="设备名解析超时")
    y.add_argument("--name", default=None, help="本机显示名")
    y.add_argument("--no-recv", action="store_true",
                   help="不启动本机接收端（纯推送模式）")
    y.set_defaults(func=cmd_sync)

    w = sub.add_parser("webhost", help="局域网静态服务 + 信令中继")
    w.add_argument("--dir", default=None, help="根目录（默认 dist 或当前目录）")
    w.add_argument("--port", type=int, default=8787, help="端口（默认 8787）")
    w.add_argument("--html", default=None, help="首页 html 文件路径")
    w.set_defaults(func=cmd_webhost)

    wa = sub.add_parser("webapp", help="跨网传输：内置网页版窗口（用系统 Edge/Chrome 作内核）")
    wa.add_argument("--dir", default=None, help="网页版所在目录（默认 dist 或当前目录）")
    wa.add_argument("--port", type=int, default=8787, help="内置服务端口（默认 8787）")
    wa.add_argument("--width", type=int, default=1220, help="窗口宽（默认 1220）")
    wa.add_argument("--height", type=int, default=880, help="窗口高（默认 880）")
    wa.add_argument("--no-profile", action="store_true",
                    help="用你当前浏览器的配置打开（默认用独立配置，不干扰你自己的浏览器）")
    wa.add_argument("--app-window", action="store_true",
                    help="尝试无地址栏的应用窗口（默认交给系统默认浏览器打开，最稳）")
    wa.add_argument("--quiet", action="store_true", help="少打印日志")
    wa.set_defaults(func=cmd_webapp)

    v = sub.add_parser("relay", help="WebSocket 信令中继（默认端口 8788）")
    v.add_argument("--port", type=int, default=8788, help="端口（默认 8788）")
    v.add_argument("--verbose", action="store_true", help="打印每条中继日志")
    v.set_defaults(func=cmd_relay)

    a = sub.add_parser("autosync", help="开机自启的无界面同步守护进程")
    a.add_argument("--hidden", action="store_true",
                   help="隐藏控制台黑窗口（开机自启命令用的就是这个）")
    a.add_argument("--verbose", action="store_true", help="日志同时打到控制台")
    a.add_argument("--interval", type=float, default=None,
                   help="覆盖配置里的轮询秒数（调试用）")
    a.add_argument("--discover-timeout", type=float, default=3.0,
                   help="设备名解析超时秒数")
    a.add_argument("--status", action="store_true",
                   help="只打印配置与自启状态，然后退出")
    a.add_argument("--once", action="store_true",
                   help="每个文件夹只跑一轮就退出（调试/自测用）")
    a.set_defaults(func=cmd_autosync)

    s2 = sub.add_parser("autostart", help="开机自启开关（注册表 Run 项）")
    s2.add_argument("action", choices=("on", "off", "status"),
                    help="on=开启 / off=关闭 / status=查看")
    s2.set_defaults(func=cmd_autostart)

    f = sub.add_parser("folders", help="管理开机同步的文件夹")
    fsub = f.add_subparsers(dest="action", metavar="{add,remove,list}")
    fa = fsub.add_parser("add", help="登记一个同步文件夹")
    fa.add_argument("local", help="本地文件夹路径")
    fa.add_argument("--peer", default="", help="对端设备名（局域网发现用）")
    fa.add_argument("--host", default="", help="对端 IP（跳过设备发现）")
    fa.add_argument("--port", type=int, default=DATA_PORT, help="对端数据端口")
    fa.add_argument("--one-way", action="store_true", help="单向：本地覆盖远端")
    fa.add_argument("--delete-extra", action="store_true",
                    help="删除另一边没有的文件（默认关）")
    fa.add_argument("--interval", type=float, default=5.0, help="轮询秒数（默认 5）")
    fa.add_argument("--no-mark-icon", action="store_true", help="不改文件夹图标")
    fa.add_argument("--no-autostart", action="store_true",
                    help="只登记文件夹，不动开机自启")
    fa.set_defaults(func=cmd_folders)
    fr = fsub.add_parser("remove", help="取消登记一个同步文件夹")
    fr.add_argument("local", help="本地文件夹路径")
    fr.add_argument("--keep-icon", action="store_true", help="保留文件夹图标标记")
    fr.set_defaults(func=cmd_folders)
    fl = fsub.add_parser("list", help="列出已登记的文件夹")
    fl.add_argument("--json", action="store_true", help="输出 JSON（给自动化用）")
    fl.set_defaults(func=cmd_folders)
    f.set_defaults(func=cmd_folders, action="list")
    return p


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，中文/emoji 路径直接打印会抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    # `python -m swiftdrop` 不带参数 → 直接开 GUI（再不行就打印帮助）
    if not argv:
        argv = ["gui"]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 1
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断。", flush=True)
        return 130
    except (OSError, LookupError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
