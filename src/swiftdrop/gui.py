"""棕仙的传输软件 图形界面（tkinter，可选加载）。

启动：``python -m swiftdrop gui``

观感目标是 Windows 11 / WinUI 3：``clam`` 主题全量自定义，配色 / 字体 / 间距
常量集中在 :mod:`swiftdrop.uitheme`（窗口底冷调浅灰、卡片白底 + 1px 描边 +
1px 底部投影、主色 #0F6CBD），字体优先 ``Segoe UI Variable`` → ``Segoe UI``
→ ``Microsoft YaHei UI``。

布局：顶部标题栏 / 左「发现的设备」卡片 / 右侧操作区 / 底部细进度条 + 状态行。
窗口默认尺寸按内容自然高度和屏幕可用高度取小值，1080p 下日志区也看得见。

* 传输、同步全部跑在后台线程，UI 只用 ``after()`` 轮询队列，不会卡死；
* 「这个文件夹开机自动同步」勾上 = ``autostart.add_folder`` + 注册表自启，
  同时给文件夹打同步图标（``foldericon``）；
* tkinter 不可用时由 cli 捕获 ImportError 并回落到命令行模式。
"""

from __future__ import annotations

import ctypes
import ipaddress
import math
import os
import queue
import socket
import sys
import threading
import time
import traceback
from tkinter import BOTH, END, LEFT, RIGHT, VERTICAL, X, Y, BooleanVar, StringVar
from tkinter import filedialog, font as tkfont, messagebox, ttk
import tkinter as tk

from . import APP_NAME, VERSION
from .discovery import DiscoveryService
from .protocol import DATA_PORT, DEFAULT_STREAMS, human_bytes, human_time
from .sync import SyncEngine
from .transfer import ReceiverServer, Sender
from .uitheme import (
    CARD_GAP, CLR_ACCENT, CLR_ACCENT_HOVER, CLR_ACCENT_PRESS, CLR_BORDER,
    CLR_BTN, CLR_BTN_BORDER, CLR_BTN_DISABLED, CLR_BTN_HOVER, CLR_BTN_PRESS,
    CLR_ERROR, CLR_INPUT_BORDER, CLR_LOG_BG, CLR_LOG_ERR, CLR_LOG_FG,
    CLR_LOG_TS, CLR_OK, CLR_PANEL, CLR_ROW_ALT, CLR_SELECT, CLR_SHADOW,
    CLR_SURFACE, CLR_SURFACE_ALT, CLR_TEXT, CLR_TEXT_DIM, CLR_TEXT_INVERT,
    CLR_TEXT_MUTE, CLR_TRACK, CLR_WINDOW, CONCLUSION_COLORS,
    CTRL_PAD, CTRL_PAD_ACCENT, CTRL_PAD_SUBTLE, FONT_BODY, FONT_CANDIDATES,
    FONT_LOG, FONT_SMALL, FONT_SUB, FONT_TINY, FONT_TITLE, INPUT_PAD,
    LOG_HEIGHT, MONO_CANDIDATES, PAD_CARD_X, PAD_HEAD_BOT, PAD_HEAD_TOP,
    PAD_ROW_BOT, PAD_ROW_TOP, PAD_ROW_Y, PAD_WINDOW, PROGRESS_H,
    PROGRESS_STATE_COLORS, RADIUS, TREE_ROW_H, lighten,
)

LOG_LIMIT = 2000
ICON_SIZE = 28

# ---------------------------------------------------------------------------
# 「连接诊断」相关常量
# ---------------------------------------------------------------------------
DIAG_WINDOW = 60.0        # 采样滑窗（秒）
DIAG_STALL_FACTOR = 0.2   # 采样速度 < 窗口均值 * 该系数 = 一次卡顿
DIAG_TICK_MS = 1000       # 主线程 after() 采样周期
DIAG_SLOW = 1_000_000.0   # 「速度低」阈值 1 MB/s
DIAG_FAST = 30_000_000.0  # 「速度好」阈值 30 MB/s
DIAG_SPREAD = 3.0         # 各流最大/最小超过该倍数 = 流间差异很大
DIAG_BOTTLENECK = 60.0    # 占比超过该百分数 = 判为瓶颈

# ---------------------------------------------------------------------------
# 观感常量：全部来自 swiftdrop.uitheme（CLR_* / PAD_* / FONT_* 等）
# ---------------------------------------------------------------------------
#: 窗口最小尺寸（再小右侧卡片会被切）
MIN_WINDOW = (900, 620)


def set_dpi_awareness() -> None:
    """进程 DPI 感知；失败无所谓。

    优先 ``PER_MONITOR_AWARE_V2``（高分屏拖动到别的显示器也不会糊），
    再退回 shcore 的 per-monitor / system aware，最后才用老 API。
    """
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(-4)):
            return
    except Exception:                                      # noqa: BLE001
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)     # PER_MONITOR
        return
    except Exception:                                      # noqa: BLE001
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)     # SYSTEM
    except Exception:                                      # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:                                  # noqa: BLE001
            pass


def set_app_user_model_id() -> None:
    """给进程一个稳定的 AppUserModelID。

    否则任务栏会把窗口归到「python.exe」名下，用的也是 python 的图标；
    设过之后任务栏显示的是本产品的名字与 :meth:`GuiApp._set_window_icon`
    里设的图标。
    """
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "ZongxianTransfer.SwiftDrop")
    except Exception:                                      # noqa: BLE001
        pass


def _round_rect(canvas: tk.Canvas, x0: float, y0: float, x1: float,
                y1: float, radius: float, color: str, tags: str = "") -> None:
    """在 Canvas 上画一个圆角矩形（两端半圆 + 中间方块拼出来）。"""
    if x1 - x0 < 1:
        return
    r = max(0.0, min(float(radius), (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    if r < 0.5:
        canvas.create_rectangle(x0, y0, x1, y1, fill=color, outline="",
                                tags=tags)
        return
    canvas.create_oval(x0, y0, x0 + 2 * r, y1, fill=color, outline="",
                       tags=tags)
    canvas.create_oval(x1 - 2 * r, y0, x1, y1, fill=color, outline="",
                       tags=tags)
    if x1 - x0 > 2 * r:
        canvas.create_rectangle(x0 + r, y0, x1 - r, y1, fill=color, outline="",
                                tags=tags)


CHECK_SIZE = 15      # 勾选框指示器边长（96 DPI 逻辑像素）


def _put_px(img: tk.PhotoImage, x: int, y: int, color: str) -> None:
    img.put(color, to=(x, y, x + 1, y + 1))


def _check_box_image(fill: str, border: str,
                     mark: str | None = None) -> tk.PhotoImage:
    """画一个指示器小方块：``mark`` 给了就在中间画对勾。纯 PhotoImage。"""
    n = CHECK_SIZE
    img = tk.PhotoImage(width=n, height=n)
    img.put(fill, to=(0, 0, n, n))
    for i in range(n):
        _put_px(img, i, 0, border)
        _put_px(img, i, n - 1, border)
        _put_px(img, 0, i, border)
        _put_px(img, n - 1, i, border)
    if mark:
        # 对勾 = 两笔画出来的折线，每点加粗成 2x2，小尺寸下也看得清
        for x, y in ((3, 7), (4, 8), (5, 9), (6, 10), (7, 9), (8, 8), (9, 7),
                     (10, 6), (11, 5), (12, 4)):
            for dx in (0, 1):
                for dy in (0, 1):
                    if 0 <= x + dx < n and 0 <= y + dy < n:
                        _put_px(img, x + dx, y + dy, mark)
    return img


def make_check_images() -> dict[str, tk.PhotoImage]:
    """勾选框的 6 张指示器图（未选 / 选中 / hover / 禁用）。

    clam 自带的选中标记是个「✗」，在 Windows 11 上看着很旧；这里自己画一套
    WinUI 观感的（主色方块 + 白色对勾），零第三方依赖。
    """
    return {
        "off": _check_box_image("#FFFFFF", "#7C8798"),
        "off_active": _check_box_image("#FFFFFF", CLR_ACCENT),
        "off_disabled": _check_box_image(CLR_BTN_DISABLED, "#C8CFD9"),
        "on": _check_box_image(CLR_ACCENT, CLR_ACCENT, "#FFFFFF"),
        "on_active": _check_box_image(CLR_ACCENT_HOVER, CLR_ACCENT_HOVER,
                                      "#FFFFFF"),
        "on_disabled": _check_box_image("#C3CBD6", "#C3CBD6", "#F3F6FA"),
    }


class ThinProgress:
    """细进度条（tk.Canvas 手绘圆角，ttk.Progressbar 做不出这个高度）。

    接口模仿 ttk.Progressbar 的那几个方法，方便直接替换：
    ``configure(value=...)`` / ``cget("value")`` / ``pack()`` / ``place()``。

    在原有接口上多了一个**纯外观**的 :meth:`set_state`（idle / busy / done /
    error），只决定填充颜色，不参与任何进度计算。
    """

    def __init__(self, parent, height: int = PROGRESS_H,
                 background: str = CLR_TRACK, fill: str = CLR_ACCENT,
                 maximum: float = 100.0):
        self.maximum = float(maximum)
        self.value = 0.0
        self._state = "busy"
        self._fill = fill
        self.fill = fill
        self._track = background
        self._height = max(1, int(height))
        self.widget = tk.Canvas(parent, height=self._height, highlightthickness=0,
                                borderwidth=0, background=background)
        self.widget.bind("<Configure>", lambda _e: self._redraw(force=True))
        self._last_key: tuple | None = None

    # -- 代理常用 widget 方法 --------------------------------------------
    def pack(self, **kw):
        self.widget.pack(**kw)
        return self

    def grid(self, **kw):
        self.widget.grid(**kw)
        return self

    def place(self, **kw):
        self.widget.place(**kw)
        return self

    def configure(self, **kw):
        if "value" in kw:
            self.value = float(kw.pop("value") or 0.0)
            self._redraw()
        if "maximum" in kw:
            self.maximum = float(kw.pop("maximum") or 100.0)
            self._redraw()
        if "background" in kw:
            self._track = str(kw.pop("background"))
            self.widget.configure(background=self._track)
            self._redraw(force=True)
        if kw:
            self.widget.configure(**kw)
        return self

    config = configure

    def cget(self, key):
        if key == "value":
            return self.value
        if key == "maximum":
            return self.maximum
        return self.widget.cget(key)

    # -- 语义状态（纯颜色）-----------------------------------------------
    def set_state(self, state: str) -> None:
        """idle / busy / done / error / warn —— 只改填充色。"""
        if state not in PROGRESS_STATE_COLORS or state == self._state:
            return
        self._state = state
        self._redraw(force=True)

    @property
    def state(self) -> str:
        return self._state

    # -- 绘制 -------------------------------------------------------------
    def _redraw(self, force: bool = False) -> None:
        try:
            width = self.widget.winfo_width()
            if width <= 1:
                width = max(1, self.widget.winfo_reqwidth())
            frac = 0.0 if self.maximum <= 0 else max(0.0, min(1.0,
                                                              self.value / self.maximum))
            key = (width, self._height, int(frac * 1000), self._state)
            if not force and key == self._last_key:
                return
            self._last_key = key
            color = PROGRESS_STATE_COLORS.get(self._state, self._fill)
            h = self._height
            self.widget.delete("all")
            _round_rect(self.widget, 0, 0, width, h, RADIUS, self._track)
            filled = width * frac
            if filled >= 1:
                _round_rect(self.widget, 0, 0, filled, h, RADIUS, color)
                # 顶部一道很淡的高光，让细条有点体积感（1px 高，不喧宾夺主）
                if h >= 5 and filled > 6:
                    _round_rect(self.widget, 2, 1, filled - 1, 2.4,
                                max(0.0, RADIUS - 1.4), lighten(color, 0.34))
        except tk.TclError:
            pass


def _ip_prefix(ip: str) -> str:
    return ip.rsplit(".", 1)[0] if ip.count(".") == 3 else ip


def _lan_kind(local_ip: str, peer_ip: str) -> str:
    """按**真实地址**判断链路类型；地址拿不到就返回「未知」，不猜。"""
    if not local_ip or not peer_ip:
        return "未知"
    try:
        peer = ipaddress.ip_address(peer_ip)
    except ValueError:
        return "未知"
    if peer.is_loopback:
        return "本机回环"
    if not (peer.is_private or peer.is_link_local):
        return "跨网/公网"
    try:
        local = ipaddress.ip_address(local_ip)
    except ValueError:
        return "私有网段"
    if (peer.version == 4 and local.version == 4
            and _ip_prefix(local_ip) == _ip_prefix(peer_ip)):
        return "局域网(同网段)"
    return "私有网段(不同网段)"


def _rate_text(value) -> str:
    return "未知" if value is None else human_bytes(value) + "/s"


def _pct_text(value) -> str:
    return "未知" if value is None else f"{value:.1f}%"


class DiagSampler:
    """「连接诊断」的采集与聚合引擎（纯标准库，不碰 tkinter）。

    线程约定：后台线程只调 :meth:`feed` 把最新的**原始**进度字典塞进来；
    主线程（tkinter ``after``）每秒调一次 :meth:`sample` 做统计。
    所有数字都来自真实回调或真实计时，拿不到的字段一律 ``None``，
    界面显示「未知」——**不做任何估算或编造**。
    """

    def __init__(self, window: float = DIAG_WINDOW):
        self.window = float(window)
        self._lock = threading.Lock()
        self._raw: dict | None = None
        self.samples: list[tuple[float, float]] = []
        self.metrics: dict = {}
        self._t_prev: float | None = None
        self._done_prev: int | None = None
        self._stream_prev: dict[int, int] = {}
        self._stream_hist: dict[int, list[tuple[float, float]]] = {}
        self._disk_prev: tuple[float, int] | None = None

    # ---- 原始数据（后台线程）--------------------------------------------
    def feed(self, ev: dict) -> None:
        """后台线程喂原始数据：只存一份，不做任何计算（极轻）。"""
        if not isinstance(ev, dict):
            return
        with self._lock:
            self._raw = dict(ev)

    def reset(self) -> None:
        """新一轮传输开始：窗口、按流计数、磁盘计数全部重来。"""
        with self._lock:
            self._raw = None
        self.samples = []
        self.metrics = {}
        self._t_prev = None
        self._done_prev = None
        self._stream_prev = {}
        self._stream_hist = {}
        self._disk_prev = None

    # ---- 采样（主线程）--------------------------------------------------
    def sample(self, now: float | None = None, final: bool = False) -> dict:
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            ev = dict(self._raw) if self._raw else None
        if ev is None:
            self.metrics = self._blank_metrics("还没有开始传输，无实测数据")
            return self.metrics

        phase = str(ev.get("phase") or "")
        done = int(ev.get("done") or 0)
        if self._done_prev is not None and done < self._done_prev:
            # 计数器被清零 = 又开了一轮，窗口重开
            self.reset()
        dt: float | None = None
        if self._t_prev is not None:
            delta_t = now - self._t_prev
            if delta_t > 1e-6:
                dt = delta_t
        moved: int | None = None
        if dt is not None and self._done_prev is not None:
            moved = done - self._done_prev
        # 收尾采样：这一秒一个字节都没动 = 传输已经结束，别把 0 塞进窗口
        # （0 的含义是「没在传」，不是「传得慢」）
        finished = bool(final and dt is not None
                        and moved is not None and moved <= 0)
        rate: float | None = None
        if finished:
            rate = 0.0
        elif dt is not None and moved is not None and moved >= 0:
            rate = moved / dt
        if rate is None:
            # 第一个采样点还没有时间区间，退回引擎自己 3 秒窗口的真实速度
            rate = float(ev.get("rate") or 0.0)
        self._t_prev = now
        self._done_prev = done

        # dt 已知 = 两个采样点之间真的量过一个区间（0 就是真卡顿，要入窗）；
        # dt 未知（第一个采样点）时只有拿到正的引擎速度才入窗——那时的 0 只
        # 代表测速窗口还没热起来，不是卡顿。
        if not finished and (dt is not None or rate > 0):
            self.samples.append((now, max(0.0, float(rate))))
        cutoff = now - self.window
        self.samples = [s for s in self.samples if s[0] >= cutoff]
        samples = self.samples

        rates = [r for _t, r in samples]
        n = len(rates)
        if n:
            r_min, r_max = min(rates), max(rates)
            mean = sum(rates) / n
            std = math.sqrt(sum((r - mean) ** 2 for r in rates) / n)
            num = den = 0.0
            prev_t = None
            for t, r in samples:
                if prev_t is not None and t > prev_t:
                    num += r * (t - prev_t)
                    den += t - prev_t
                prev_t = t
            avg = num / den if den > 1e-9 else mean
        else:
            r_min = r_max = mean = std = avg = None

        stalls: int | None = None
        if avg is not None:
            stalls = 0
            low_before = False
            for _t, r in samples:
                low = avg > 0 and r < DIAG_STALL_FACTOR * avg
                if low and not low_before:
                    stalls += 1
                low_before = low

        # ---- 各条流（真实按流计数）------------------------------------
        cur_streams: dict[int, float] = {}
        for key, val in (ev.get("stream_bytes") or {}).items():
            try:
                idx, total = int(key), int(val)
            except (TypeError, ValueError):
                continue
            before = self._stream_prev.get(idx)
            if dt is not None and before is not None and not finished:
                cur_streams[idx] = max(0.0, (total - before) / dt)
                self._stream_hist.setdefault(idx, []).append(
                    (now, cur_streams[idx]))
            self._stream_prev[idx] = total
        for idx in list(self._stream_hist):
            hist = [s for s in self._stream_hist[idx] if s[0] >= cutoff]
            if hist:
                self._stream_hist[idx] = hist
            else:
                self._stream_hist.pop(idx, None)
        sr_avg: dict[int, float] = {}
        sr_peak: dict[int, float] = {}
        for idx, hist in self._stream_hist.items():
            vals = [r for _t, r in hist]
            if vals:
                sr_avg[idx] = sum(vals) / len(vals)
                sr_peak[idx] = max(vals)

        elapsed = float(ev.get("elapsed") or 0.0)

        # ---- 接收端磁盘侧（真实计时）----------------------------------
        disk = None
        disk_s = ev.get("disk_seconds")
        disk_b = ev.get("disk_bytes")
        if disk_s is not None and disk_b is not None:
            disk_s, disk_b = float(disk_s), int(disk_b)
            ratio = None
            if elapsed > 1e-6:
                ratio = max(0.0, min(100.0, disk_s / elapsed * 100.0))
            rate_now = None
            if dt is not None and self._disk_prev is not None:
                d_s = disk_s - self._disk_prev[0]
                d_b = disk_b - self._disk_prev[1]
                if d_s > 1e-6 and d_b >= 0:
                    rate_now = d_b / d_s
            self._disk_prev = (disk_s, disk_b)
            disk = {"seconds": disk_s, "bytes": disk_b, "ratio": ratio,
                    "rate_now": rate_now,
                    "rate_avg": (disk_b / disk_s) if disk_s > 1e-6 else None}

        # ---- 发送端网络侧（真实计时）----------------------------------
        net = None
        net_send = ev.get("net_send_seconds")
        if net_send is not None:
            net_send = float(net_send)
            net_wait = ev.get("net_wait_seconds")
            net_wait = float(net_wait) if net_wait is not None else None
            streams = max(1, int(ev.get("streams") or 1))
            denom = elapsed * streams
            net = {
                "send_seconds": net_send,
                "wait_seconds": net_wait,
                "streams": streams,
                "send_ratio": (max(0.0, min(100.0, net_send / denom * 100.0))
                               if denom > 1e-6 else None),
                "wait_ratio": (max(0.0, min(100.0, net_wait / denom * 100.0))
                               if (net_wait is not None and denom > 1e-6)
                               else None),
            }

        local_ip = str(ev.get("local_ip") or "")
        peer_ip = str(ev.get("peer_ip") or "")
        recv_side = phase.startswith("recv")
        streams_n = int(ev.get("streams") or 0) or None
        link = {
            "kind": _lan_kind(local_ip, peer_ip),
            "transport": "TCP",
            "streams": streams_n,
            "streams_active": ev.get("streams_active"),
            "local_ip": local_ip or None,
            "peer_ip": peer_ip or None,
            "side": "recv" if recv_side else "send",
            "direction": (("对方 → 本机" if recv_side else "本机 → 对方")
                          if peer_ip else None),
        }

        total = int(ev.get("total") or 0)
        m: dict = {
            "ts": time.time(),
            "phase": phase,
            "side": link["side"],
            "finished": finished,
            "progress": {
                "total": total, "done": done, "elapsed": elapsed,
                "percent": (100.0 if total <= 0
                            else min(100.0, done * 100.0 / total)),
            },
            "link": link,
            "rate": rate,
            "peak": r_max,
            "avg": avg,
            "min": r_min,
            "fluct": {"min": r_min, "max": r_max, "mean": mean, "std": std,
                      "n": n, "stalls": stalls,
                      "window": self.window},
            "streams_rate": cur_streams,
            "streams_rate_avg": sr_avg,
            "streams_rate_peak": sr_peak,
            "disk": disk,
            "net": net,
            "samples": list(samples),
        }
        m["unknown"] = self._unknowns(m)
        m["conclusion_code"], m["conclusion"] = self._conclude(m)
        self.metrics = m
        return m

    # ---- 结论（启发式，按题目给定优先级）--------------------------------
    @staticmethod
    def _conclude(m: dict) -> tuple[str, str]:
        speed = m.get("avg")
        if speed is None:
            speed = m.get("rate")
        disk_ratio = (m.get("disk") or {}).get("ratio")
        net_ratio = (m.get("net") or {}).get("send_ratio")
        per_stream = [v for v in (m.get("streams_rate_avg") or {}).values() if v > 0]
        spread = None
        if len(per_stream) >= 2 and min(per_stream) > 0:
            spread = max(per_stream) / min(per_stream)
        kind = (m.get("link") or {}).get("kind")
        lan = bool(kind) and kind != "跨网/公网" and kind != "未知"
        if disk_ratio is not None and disk_ratio > DIAG_BOTTLENECK:
            return "disk", "接收方磁盘/杀毒扫描是瓶颈"
        if net_ratio is not None and net_ratio > DIAG_BOTTLENECK:
            return "uplink", "发送方上行/链路是瓶颈"
        if (speed is not None and speed < DIAG_SLOW
                and spread is not None and spread > DIAG_SPREAD):
            return "stream_spread", "单连接被限速或有丢包"
        if speed is not None and speed < DIAG_SLOW and lan:
            return "lan_slow", ("局域网却低于 1MB/s：检查是否连了 2.4G WiFi / "
                                "网线百兆 / 杀毒软件实时扫描")
        if speed is not None and speed >= DIAG_FAST:
            return "good", "链路与磁盘状态良好"
        return "normal", "瓶颈不明显，属正常波动"

    @staticmethod
    def _unknowns(m: dict) -> list[str]:
        unknown: list[str] = []
        link = m.get("link") or {}
        if not link.get("local_ip") or not link.get("peer_ip"):
            unknown.append("链路地址（没拿到本机/对方 IP）")
        if m.get("disk") is None:
            unknown.append("接收端磁盘耗时（本端是发送端，没有磁盘侧插桩）")
        if m.get("net") is None:
            unknown.append("发送端 socket 写阻塞（本端是接收端，没有该侧插桩）")
        if (m.get("fluct") or {}).get("n", 0) < 2:
            unknown.append("速度波动统计（采样不足 2 个点）")
        if not m.get("streams_rate_avg"):
            unknown.append("各条流速度（还没有两次按流计数）")
        return unknown

    @staticmethod
    def _blank_metrics(note: str) -> dict:
        return {
            "ts": time.time(), "phase": "", "side": "", "finished": False,
            "progress": {"total": 0, "done": 0, "elapsed": 0.0, "percent": 0.0},
            "link": {"kind": "未知", "transport": "TCP", "streams": None,
                     "streams_active": None, "local_ip": None, "peer_ip": None,
                     "side": "", "direction": None},
            "rate": None, "peak": None, "avg": None, "min": None,
            "fluct": {"min": None, "max": None, "mean": None, "std": None,
                      "n": 0, "stalls": None, "window": DIAG_WINDOW},
            "streams_rate": {}, "streams_rate_avg": {}, "streams_rate_peak": {},
            "disk": None, "net": None, "samples": [],
            "unknown": [note],
            "conclusion_code": "none", "conclusion": "",
        }

    # ---- 一屏文本 / 剪贴板内容 -------------------------------------------
    def text(self) -> str:
        m = self.metrics or self.sample()
        link = m.get("link") or {}
        prog = m.get("progress") or {}
        fluct = m.get("fluct") or {}
        disk = m.get("disk") or {}
        net = m.get("net") or {}

        streams = link.get("streams")
        finished = bool(m.get("finished"))
        rate_text = _rate_text(m.get("rate"))
        if finished and not m.get("rate"):
            rate_text = "0 B/s（传输已结束，本秒无字节移动）"
        link_bits = [link.get("kind") or "未知",
                     f"{link.get('transport') or 'TCP'}"
                     + (f"，{streams} 条流" if streams else "")]
        if link.get("direction"):
            link_bits.append(link["direction"])
        lines = [
            f"{APP_NAME} · 连接诊断（全部为实测值）",
            "采集时间: " + time.strftime("%Y-%m-%d %H:%M:%S",
                                         time.localtime(m.get("ts") or time.time())),
            "链路: " + " · ".join(link_bits),
            "方向/地址: " + (f"{link.get('direction') or '未知'}   本机 "
                            f"{link.get('local_ip') or '未知'} · 对方 "
                            f"{link.get('peer_ip') or '未知'}"),
            (f"进度: {prog.get('percent', 0.0):.1f}%  "
             f"{human_bytes(prog.get('done') or 0)} / "
             f"{human_bytes(prog.get('total') or 0)}   "
             f"耗时 {prog.get('elapsed') or 0.0:.1f}s"),
            (f"实时速度: {rate_text}   "
             f"峰值(60s): {_rate_text(m.get('peak'))}   "
             f"均值(60s): {_rate_text(m.get('avg'))}"),
        ]
        per_now = m.get("streams_rate") or {}
        per_avg = m.get("streams_rate_avg") or {}
        per_peak = m.get("streams_rate_peak") or {}
        if per_avg or per_now:
            def _join(d):
                return "  ".join(f"#{i} {human_bytes(v)}/s"
                                 for i, v in sorted(d.items()))
            if per_now:
                lines.append("各条流(本次采样): " + _join(per_now))
            lines.append("各条流(60s 均值): " + (_join(per_avg) or "未知"))
            lines.append("各条流(60s 峰值): " + (_join(per_peak) or "未知"))
        else:
            lines.append("各条流: 未知（没有按流计数）")
        lines.append(
            "速度波动(60s): 最小 {} · 最大 {} · 标准差 {} · 采样 {} 个 · "
            "卡顿 {} 次".format(
                _rate_text(fluct.get("min")), _rate_text(fluct.get("max")),
                "未知" if fluct.get("std") is None
                else human_bytes(fluct["std"]) + "/s",
                fluct.get("n") or 0,
                "未知" if fluct.get("stalls") is None else fluct["stalls"]))
        if disk:
            now_rate = disk.get("rate_now")
            if now_rate is None and finished:
                now_rate = disk.get("rate_avg")   # 收尾这一秒没写盘，用窗口均值
            lines.append(
                "接收端磁盘: 写入耗时占比 {}   当前写入 {}   窗口均值 {}"
                "（累计 {:.2f}s / {}）".format(
                    _pct_text(disk.get("ratio")), _rate_text(now_rate),
                    _rate_text(disk.get("rate_avg")), disk.get("seconds") or 0.0,
                    human_bytes(disk.get("bytes") or 0)))
        else:
            lines.append("接收端磁盘: 未知（本端没有磁盘侧插桩）")
        if net:
            lines.append(
                "发送端网络: socket 写阻塞占比 {}   等对端确认占比 {}"
                "（{} 条流平均口径，累计写阻塞 {:.2f}s）".format(
                    _pct_text(net.get("send_ratio")),
                    _pct_text(net.get("wait_ratio")), net.get("streams") or 0,
                    net.get("send_seconds") or 0.0))
        else:
            lines.append("发送端网络: 未知（本端没有该侧插桩）")
        lines.append("结论: " + (m.get("conclusion") or "未知"))
        unknown = m.get("unknown") or []
        lines.append("未知项: " + ("；".join(unknown) if unknown else "（无）"))
        return "\n".join(lines)


class DiagPanel:
    """界面上的「连接诊断」卡片（放在右侧栏「跨网传输」卡片下方）。

    采样一律在**主线程**用 ``after()`` 驱动；后台线程只把原始进度字典塞进
    :class:`DiagSampler`。传输中自动开始采样，空闲时自动停（不白占 CPU）。
    """

    CURVE_H = 22

    def __init__(self, app: "GuiApp", parent):
        self.app = app
        self.sampler = DiagSampler()
        self.enabled = tk.BooleanVar(master=app.root, value=False)
        self._auto = False
        self._user_off = False
        self._last_phase = ""

        box = app._pack_card(parent, "连接诊断", "全部实测，取不到就显示未知",
                             fill=X, pady=(CARD_GAP, 0))
        top = ttk.Frame(box, style="Card.TFrame")
        top.pack(fill=X, padx=PAD_CARD_X, pady=(PAD_HEAD_BOT, 3))
        self.chk = ttk.Checkbutton(top, text="每秒采样（传输中自动开始）",
                                   variable=self.enabled,
                                   style="Card.TCheckbutton",
                                   command=self._on_toggle)
        self.chk.pack(side=LEFT)
        ttk.Button(top, text="复制诊断信息", style="Subtle.TButton",
                   command=self.copy_report).pack(side=RIGHT)
        self.lbl_link = ttk.Label(box, text="链路：未知", style="CardTiny.TLabel")
        self.lbl_link.pack(fill=X, padx=PAD_CARD_X, pady=(1, 1))
        self.lbl_perf = ttk.Label(box, text="速度 / 各流 / 磁盘 / 网络：未知",
                                  style="CardTiny.TLabel")
        self.lbl_perf.pack(fill=X, padx=PAD_CARD_X, pady=(1, 1))
        self.canvas = tk.Canvas(box, height=self.CURVE_H, highlightthickness=1,
                                borderwidth=0, background=CLR_PANEL,
                                highlightbackground=CLR_BORDER)
        self.canvas.pack(fill=X, padx=PAD_CARD_X, pady=(3, 2))
        self.lbl_concl = ttk.Label(box, text="结论：等待采样…",
                                   style="Card.TLabel",
                                   font=(app.font_family, FONT_SMALL, "bold"))
        self.lbl_concl.pack(fill=X, padx=PAD_CARD_X, pady=(0, PAD_ROW_BOT))
        self.canvas.bind("<Configure>", lambda _e: self._draw(self.sampler.metrics))
        self._draw(None)

    # ---- 主线程采样循环 -------------------------------------------------
    def tick(self) -> None:
        try:
            if self.enabled.get():
                self._render(self.sampler.sample())
        except tk.TclError:
            return
        except Exception:                                  # noqa: BLE001
            pass
        try:
            self.app.root.after(DIAG_TICK_MS, self.tick)
        except tk.TclError:
            pass

    # ---- 传输事件（主线程，由 GuiApp._apply_progress 调）----------------
    def on_progress(self, ev: dict) -> None:
        phase = str(ev.get("phase") or "")
        active = phase in ("send", "recv")
        if active and self._last_phase not in ("send", "recv"):
            # 新一轮传输：重开窗口；用户没手动关就自动开始采样
            self.sampler.reset()
            if not self.enabled.get() and not self._user_off:
                self._auto = True
                self.enabled.set(True)
        self._last_phase = phase
        self.sampler.feed(ev)
        if phase in ("send-done", "recv-idle"):
            self.on_transfer_end()

    def on_transfer_end(self) -> None:
        """一轮传输结束：补最后一次采样，然后按用户意图决定是否继续采。"""
        try:
            if self.enabled.get():
                self._render(self.sampler.sample(final=True))
        except Exception:                                  # noqa: BLE001
            pass
        if self._auto:
            self._auto = False
            self.enabled.set(False)
        self._user_off = False
        self._last_phase = ""

    # ---- 交互 -----------------------------------------------------------
    def _on_toggle(self) -> None:
        self._auto = False
        if self.enabled.get():
            self._user_off = False
            self.sampler.reset()
            self.app.log("连接诊断：开始每秒采样")
        else:
            self._user_off = True
            self.app.log("连接诊断：已关闭采样（空闲不采样）")

    def copy_report(self) -> None:
        text = self.sampler.text()
        try:
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append(text)
        except tk.TclError as exc:
            self.app.log(f"复制诊断信息失败：{exc}")
            return
        self.app.log("已复制诊断信息到剪贴板：")
        for line in text.splitlines():
            self.app.log("    " + line)

    # ---- 渲染 -----------------------------------------------------------
    def _render(self, m: dict) -> None:
        link = m.get("link") or {}
        fluct = m.get("fluct") or {}
        disk = m.get("disk") or {}
        net = m.get("net") or {}
        per = m.get("streams_rate") or m.get("streams_rate_avg") or {}
        try:
            bits = [link.get("kind") or "未知", link.get("transport") or "TCP"]
            if link.get("streams"):
                bits.append(f"{link['streams']} 条流")
            if link.get("direction"):
                bits.append(link["direction"])
            if link.get("peer_ip"):
                bits.append(f"{link.get('local_ip') or '未知'} → {link['peer_ip']}")
            self.lbl_link.configure(text="链路：" + " · ".join(bits))
        except tk.TclError:
            return
        std = fluct.get("std")
        perf = [
            "速度 {}".format(
                "0 B/s（已结束）" if (m.get("finished") and not m.get("rate"))
                else _rate_text(m.get("rate"))),
            "峰值 {}".format(_rate_text(m.get("peak"))),
            "均值 {}".format(_rate_text(m.get("avg"))),
            "σ {}".format("未知" if std is None
                          else human_bytes(std) + "/s"),
            "卡顿 {}".format("未知" if fluct.get("stalls") is None
                             else fluct["stalls"]),
        ]
        if per:
            perf.append("各流 " + "/".join(
                f"{v / 1048576.0:.1f}" for _i, v in sorted(per.items()))
                + " MB/s")
        else:
            perf.append("各流 未知")
        if disk:
            perf.append("磁盘写入占比 " + _pct_text(disk.get("ratio")))
            if disk.get("rate_now") is not None:
                perf.append("当前写入 " + _rate_text(disk.get("rate_now")))
        if net:
            perf.append("网络写阻塞 " + _pct_text(net.get("send_ratio")))
        if not disk and not net:
            perf.append(m.get("unknown", ["没有本端插桩"])[0])
        self.lbl_perf.configure(text=" · ".join(perf))
        # 结论按语义着色（外观，不影响任何取值/判断）
        try:
            self.lbl_concl.configure(
                text="结论：" + (m.get("conclusion") or "等待采样…"),
                foreground=CONCLUSION_COLORS.get(
                    str(m.get("conclusion_code") or "none"), CLR_TEXT))
        except tk.TclError:
            return
        self._draw(m)

    def _draw(self, m: dict | None) -> None:
        canvas = getattr(self, "canvas", None)
        if canvas is None:
            return
        try:
            width = max(80, canvas.winfo_width())
        except tk.TclError:
            return
        height = self.CURVE_H
        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill=CLR_PANEL, outline="")
        samples = (m or {}).get("samples") or []
        if not samples:
            canvas.create_text(width // 2, height // 2, text="暂无采样（60 秒窗口）",
                               fill=CLR_TEXT_MUTE,
                               font=(self.app.font_family, FONT_TINY))
            return
        rates = [r for _t, r in samples]
        top = max(rates) or 1.0
        # x 轴固定按 60 秒画：一根柱子 = 一秒，采样少的时候左边先满，
        # 不会把 4 个采样点拉满整条曲线骗人。
        step = width / max(1.0, float(self.sampler.window))
        bar_w = max(1.0, step - 1.0)
        for i, rate in enumerate(rates):
            bar_h = max(1.0, (rate / top) * (height - 13))
            x0 = i * step
            # 柱子顶端 1px 用浅一点的主色，柱身用主色，看起来更“有头”
            canvas.create_rectangle(x0, height - 1 - bar_h, x0 + bar_w,
                                    height - 1, fill=CLR_ACCENT, outline="")
            if bar_h > 3:
                canvas.create_rectangle(x0, height - 1 - bar_h, x0 + bar_w,
                                        height - 1 - bar_h + 1.4,
                                        fill=lighten(CLR_ACCENT, 0.35),
                                        outline="")
        canvas.create_line(0, height - 1, width, height - 1, fill=CLR_BORDER)
        canvas.create_text(3, 1, anchor="nw",
                           text="最近 60 秒 · 峰值 " + human_bytes(top) + "/s",
                           fill=CLR_TEXT_MUTE,
                           font=(self.app.font_family, FONT_TINY))


class GuiApp:
    def __init__(self, port: int = DATA_PORT, default_dir: str | None = None):
        set_dpi_awareness()
        set_app_user_model_id()
        self.port = int(port)
        self.default_dir = default_dir or os.path.join(os.getcwd(), "swiftdrop-recv")
        self.events: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.discovery: DiscoveryService | None = None
        self.receiver: ReceiverServer | None = None
        self.sync_stop: threading.Event | None = None
        self.worker: threading.Thread | None = None
        self.recv_running = False
        self._fail_count = 0
        self._autostart_guard = False
        self._icon_warned = False
        self.logo_image = None
        self.diag: DiagPanel | None = None

        self.root = tk.Tk()
        # 标题栏只留产品名 + 版本：Windows 11 的习惯，任务栏里也不会被截成
        # 「棕仙的传输软件 1.0 —— 局域网大文件…」。详细说明在界面副标题上。
        self.root.title(f"{APP_NAME} {VERSION}")
        self.root.minsize(*MIN_WINDOW)
        self.root.configure(background=CLR_WINDOW)
        self._init_style()
        self._build()
        self._fit_window()
        self._sync_autostart_ui()
        self._start_discovery()
        self.root.after(120, self._pump)
        self.root.after(DIAG_TICK_MS, self.diag.tick)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _fit_window(self) -> None:
        """按内容自然尺寸和屏幕可用区域取小值定窗口大小。

        以前是写死 1120x980：1080p 上客户端高度只剩不到 950，而内容自然
        高度要 1100+，底部日志卡片直接被切掉。这里改成——

        * 高度 = ``min(内容需要的高度, 屏幕高 - 96)``：屏幕够大就一次看全
          （含日志），屏幕小就顶到可用高度；
        * 屏幕实在矮：**先压缩日志区**（一行一行减，最少留 2 行），让每张
          卡片都还在窗口里，而不是把日志整块切掉；
        * 宽度 = ``min(max(内容需要, 1000), 屏幕宽 - 120)``：给日志框留宽。
        """
        try:
            self.root.update_idletasks()
            scr_w = self.root.winfo_screenwidth()
            scr_h = self.root.winfo_screenheight()
            need_w = max(1000, int(self.root.winfo_reqwidth()) + 60)
            need_h = int(self.root.winfo_reqheight())
            avail_w = max(MIN_WINDOW[0], scr_w - 120)
            avail_h = max(MIN_WINDOW[1], scr_h - 96)
            lines = LOG_HEIGHT
            while need_h > avail_h and lines > 2:
                lines -= 1
                self.txt.configure(height=lines)
                self.root.update_idletasks()
                shrunk = int(self.root.winfo_reqheight())
                if shrunk >= need_h:            # 压不动了，别再循环
                    break
                need_h = shrunk
            width = min(need_w, avail_w)
            height = min(need_h, avail_h)
            x = max(0, (scr_w - width) // 2)
            y = max(0, (scr_h - height) // 3)
            self.root.geometry(f"{width}x{height}+{x}+{y}")
        except tk.TclError:
            self.root.geometry("1040x940")

    # -- 样式 -------------------------------------------------------------
    def _init_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        families = set(tkfont.families())
        self.font_family = next((f for f in FONT_CANDIDATES if f in families),
                                "TkDefaultFont")
        self.mono_family = next((f for f in MONO_CANDIDATES if f in families),
                                "Courier")
        try:
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                         "TkHeadingFont"):
                tkfont.nametofont(name).configure(family=self.font_family,
                                                  size=FONT_BODY)
            self.mono = tkfont.Font(family=self.mono_family, size=FONT_LOG)
        except Exception:                                  # noqa: BLE001
            self.mono = None

        self.root.option_add("*Font", (self.font_family, FONT_BODY))

        style.configure(".", background=CLR_WINDOW, foreground=CLR_TEXT,
                        fieldbackground=CLR_SURFACE, bordercolor=CLR_BORDER,
                        lightcolor=CLR_BORDER, darkcolor=CLR_BORDER,
                        troughcolor=CLR_TRACK, focuscolor=CLR_ACCENT,
                        font=(self.font_family, FONT_BODY))
        style.configure("TFrame", background=CLR_WINDOW)

        # ---- 文字层级：一级（标题/数值）/ 二级（说明）/ 三级（次要）--------
        style.configure("TLabel", background=CLR_WINDOW, foreground=CLR_TEXT)
        style.configure("Card.TFrame", background=CLR_SURFACE)
        style.configure("CardHead.TFrame", background=CLR_SURFACE)
        style.configure("Card.TLabel", background=CLR_SURFACE,
                        foreground=CLR_TEXT)
        style.configure("CardDim.TLabel", background=CLR_SURFACE,
                        foreground=CLR_TEXT_DIM)
        style.configure("CardMute.TLabel", background=CLR_SURFACE,
                        foreground=CLR_TEXT_MUTE, font=(self.font_family,
                                                         FONT_SMALL))
        # 「连接诊断」卡片用的超小号说明文字（卡片要尽量紧凑）
        style.configure("CardTiny.TLabel", background=CLR_SURFACE,
                        foreground=CLR_TEXT_DIM, font=(self.font_family,
                                                       FONT_TINY))
        style.configure("Title.TLabel", background=CLR_WINDOW,
                        foreground=CLR_TEXT,
                        font=(self.font_family, FONT_TITLE, "bold"))
        style.configure("Subtitle.TLabel", background=CLR_WINDOW,
                        foreground=CLR_TEXT_MUTE,
                        font=(self.font_family, FONT_SUB))
        # 状态行：默认（空闲）二级灰；忙 / 完成 / 失败各有语义色
        style.configure("Status.TLabel", background=CLR_WINDOW,
                        foreground=CLR_TEXT_DIM,
                        font=(self.font_family, FONT_SMALL))
        style.configure("StatusBusy.TLabel", background=CLR_WINDOW,
                        foreground=CLR_ACCENT,
                        font=(self.font_family, FONT_SMALL, "bold"))
        style.configure("StatusDone.TLabel", background=CLR_WINDOW,
                        foreground=CLR_OK,
                        font=(self.font_family, FONT_SMALL, "bold"))
        style.configure("StatusError.TLabel", background=CLR_WINDOW,
                        foreground=CLR_ERROR,
                        font=(self.font_family, FONT_SMALL, "bold"))
        style.configure("Hint.TLabel", background=CLR_WINDOW,
                        foreground=CLR_TEXT_MUTE,
                        font=(self.font_family, FONT_SMALL))
        style.configure("Self.TLabel", background=CLR_WINDOW,
                        foreground=CLR_TEXT_DIM,
                        font=(self.font_family, FONT_SMALL))

        # ---- 次按钮：白底 + 1px 实描边（clam 只有 relief=solid 才画出描边）--
        style.configure("TButton", background=CLR_BTN, foreground=CLR_TEXT,
                        bordercolor=CLR_BTN_BORDER, lightcolor=CLR_BTN,
                        darkcolor=CLR_BTN, relief="solid", padding=CTRL_PAD,
                        font=(self.font_family, FONT_BODY), anchor="center")
        style.map("TButton",
                  relief=[("pressed", "flat")],
                  background=[("disabled", CLR_BTN_DISABLED),
                              ("pressed", CLR_BTN_PRESS),
                              ("active", CLR_BTN_HOVER)],
                  foreground=[("disabled", CLR_TEXT_MUTE)],
                  bordercolor=[("active", CLR_ACCENT),
                               ("disabled", CLR_BORDER)],
                  lightcolor=[("active", CLR_ACCENT),
                              ("pressed", CLR_BTN_PRESS)],
                  darkcolor=[("active", CLR_ACCENT),
                             ("pressed", CLR_BTN_PRESS)])
        # ---- 主按钮：主色填充 + 白字 + 更宽的内边距（视觉重量更大）--------
        style.configure("Accent.TButton", background=CLR_ACCENT,
                        foreground=CLR_TEXT_INVERT, bordercolor=CLR_ACCENT,
                        lightcolor=CLR_ACCENT, darkcolor=CLR_ACCENT,
                        relief="flat", padding=CTRL_PAD_ACCENT,
                        font=(self.font_family, FONT_BODY, "bold"))
        style.map("Accent.TButton",
                  background=[("disabled", lighten(CLR_ACCENT, 0.55)),
                              ("pressed", CLR_ACCENT_PRESS),
                              ("active", CLR_ACCENT_HOVER)],
                  foreground=[("disabled", "#F4F7FB")],
                  lightcolor=[("active", CLR_ACCENT_HOVER),
                              ("pressed", CLR_ACCENT_PRESS)],
                  darkcolor=[("active", CLR_ACCENT_HOVER),
                             ("pressed", CLR_ACCENT_PRESS)],
                  bordercolor=[("disabled", lighten(CLR_ACCENT, 0.55))])
        # ---- 小号按钮（卡片里的工具按钮）：细描边 + 小字，克制不抢视线 ------
        style.configure("Subtle.TButton", background=CLR_SURFACE,
                        foreground=CLR_TEXT_DIM, bordercolor="#DCE2EB",
                        lightcolor=CLR_SURFACE, darkcolor=CLR_SURFACE,
                        relief="solid", padding=CTRL_PAD_SUBTLE,
                        font=(self.font_family, FONT_SMALL))
        style.map("Subtle.TButton",
                  relief=[("pressed", "flat")],
                  background=[("pressed", CLR_BTN_PRESS),
                              ("active", CLR_BTN_HOVER)],
                  foreground=[("disabled", CLR_TEXT_MUTE),
                              ("active", CLR_TEXT)],
                  bordercolor=[("active", CLR_ACCENT),
                               ("disabled", CLR_BORDER)],
                  lightcolor=[("active", CLR_BTN_HOVER)],
                  darkcolor=[("active", CLR_BTN_HOVER)])

        # ---- 勾选框：自绘指示器（clam 原生的「✗」太旧），方框跟随主色 -------
        self._check_imgs = make_check_images()
        self._style_checkbutton("TCheckbutton", CLR_WINDOW)
        self._style_checkbutton("Card.TCheckbutton", CLR_SURFACE)

        # ---- 输入类：白底、1px 描边，聚焦时描边变主色 ----------------------
        for name in ("TEntry", "TSpinbox", "TCombobox"):
            style.configure(name, fieldbackground=CLR_SURFACE,
                            background=CLR_SURFACE,
                            foreground=CLR_TEXT,
                            bordercolor=CLR_INPUT_BORDER,
                            lightcolor=CLR_INPUT_BORDER,
                            darkcolor=CLR_INPUT_BORDER,
                            insertcolor=CLR_ACCENT, padding=INPUT_PAD,
                            relief="flat", arrowcolor=CLR_TEXT_DIM,
                            selectbackground=CLR_SELECT,
                            selectforeground=CLR_TEXT)
            style.map(name, bordercolor=[("focus", CLR_ACCENT),
                                         ("hover", CLR_ACCENT)],
                      lightcolor=[("focus", CLR_ACCENT),
                                  ("hover", CLR_ACCENT)],
                      darkcolor=[("focus", CLR_ACCENT),
                                 ("hover", CLR_ACCENT)],
                      fieldbackground=[("disabled", CLR_BTN_DISABLED)],
                      foreground=[("disabled", CLR_TEXT_MUTE)])
        # Combobox 的下拉列表（有的话）也跟随主题
        try:
            self.root.option_add("*TCombobox*Listbox.background", CLR_SURFACE)
            self.root.option_add("*TCombobox*Listbox.foreground", CLR_TEXT)
            self.root.option_add("*TCombobox*Listbox.selectBackground",
                                 CLR_SELECT)
            self.root.option_add("*TCombobox*Listbox.selectForeground", CLR_TEXT)
        except tk.TclError:
            pass

        # ---- 滚动条：细一点、无箭头底色 ------------------------------------
        for orient in ("Vertical", "Horizontal"):
            name = f"{orient}.TScrollbar"
            try:
                style.configure(name, background="#DCE2EB",
                                troughcolor=CLR_SURFACE,
                                bordercolor=CLR_SURFACE,
                                arrowcolor=CLR_TEXT_MUTE, relief="flat",
                                arrowsize=12, width=11)
            except tk.TclError:
                style.configure(name, background="#DCE2EB",
                                troughcolor=CLR_SURFACE,
                                bordercolor=CLR_SURFACE,
                                arrowcolor=CLR_TEXT_MUTE, relief="flat")
            style.map(name, background=[("active", "#C4CDDA"),
                                        ("pressed", CLR_ACCENT)])

        # 底部进度条改用 tk.Canvas 手绘（见 ThinProgress），这里只留一份
        # 兼容样式名，免得老引用报错。
        style.configure("Thin.Horizontal.TProgressbar",
                        background=CLR_ACCENT, troughcolor=CLR_TRACK,
                        bordercolor=CLR_WINDOW, lightcolor=CLR_ACCENT,
                        darkcolor=CLR_ACCENT, thickness=PROGRESS_H)

        # ---- 设备列表 -------------------------------------------------------
        style.configure("Peers.Treeview", background=CLR_SURFACE,
                        fieldbackground=CLR_SURFACE, foreground=CLR_TEXT,
                        bordercolor=CLR_BORDER, lightcolor=CLR_BORDER,
                        darkcolor=CLR_BORDER, rowheight=TREE_ROW_H,
                        relief="flat", font=(self.font_family, FONT_BODY))
        style.map("Peers.Treeview",
                  background=[("selected", CLR_SELECT)],
                  foreground=[("selected", CLR_TEXT)])
        style.configure("Peers.Treeview.Heading", background=CLR_SURFACE_ALT,
                        foreground=CLR_TEXT_MUTE, relief="flat",
                        font=(self.font_family, FONT_SMALL),
                        padding=(6, 7), bordercolor=CLR_BORDER)
        style.map("Peers.Treeview.Heading",
                  background=[("active", CLR_BTN_HOVER)])
        try:
            style.layout("Peers.Treeview", [
                ("Peers.Treeview.treearea", {"sticky": "nswe"})])
        except tk.TclError:
            pass

    def _style_checkbutton(self, name: str, bg: str) -> None:
        """给一个 TCheckbutton 变体装上自绘指示器 + 自定义布局。

        布局 = [内边距 [指示器 焦点框 [文字]]]，和 clam 默认结构一致，只是把
        指示器元素换成图片元素；元素创建失败就退回 clam 默认外观（不影响功能）。
        """
        imgs = self._check_imgs
        style = ttk.Style(self.root)
        indicator = name + ".indicator"
        try:
            style.element_create(
                indicator, "image", imgs["off"],
                ("disabled", imgs["off_disabled"]),
                ("selected", imgs["on"]),
                ("selected disabled", imgs["on_disabled"]),
                ("active", imgs["off_active"]),
                ("selected active", imgs["on_active"]),
                padding=(0, 0, 6, 0), sticky="")
            style.layout(name, [
                ("Checkbutton.padding", {"sticky": "nswe", "children": [
                    (indicator, {"side": "left", "sticky": ""}),
                    ("Checkbutton.focus", {"side": "left", "sticky": "nswe",
                                           "children": [
                                               ("Checkbutton.label",
                                                {"sticky": "nswe"})]})]})])
        except tk.TclError:
            pass
        style.configure(name, background=bg, foreground=CLR_TEXT,
                        focuscolor=bg, padding=(2, 1),
                        font=(self.font_family, FONT_BODY))
        style.map(name,
                  background=[("active", bg), ("pressed", bg)],
                  foreground=[("disabled", CLR_TEXT_MUTE)])

    # -- 小工具 -----------------------------------------------------------
    def _new_card(self, parent, title: str,
                  subtitle: str = "") -> tuple[tk.Frame, tk.Frame]:
        """建一张白底卡片（1px 描边 + 1px 底部投影）。

        ttk 没有圆角也没有阴影，用「最外层一条深色线做投影 + 中间一层描边色
        + 内层白色表面」三层营造卡片感，底部多出的那 1px 就是廉价但有效的
        投影。返回 ``(外框, 内容区)``：**外框负责 pack/grid，内容往内容区里
        放**。
        """
        shadow = tk.Frame(parent, background=CLR_SHADOW)
        frame = tk.Frame(shadow, background=CLR_BORDER)
        frame.pack(fill=BOTH, expand=True, pady=(0, 1))
        inner = tk.Frame(frame, background=CLR_SURFACE)
        inner.pack(fill=BOTH, expand=True, padx=1, pady=1)
        if title:
            head = ttk.Frame(inner, style="CardHead.TFrame")
            head.pack(fill=X, padx=PAD_CARD_X, pady=(PAD_HEAD_TOP, 0))
            ttk.Label(head, text=title, style="Card.TLabel",
                      font=(self.font_family, FONT_BODY, "bold")).pack(side=LEFT)
            if subtitle:
                ttk.Label(head, text="  " + subtitle,
                          style="CardMute.TLabel").pack(side=LEFT)
        return shadow, inner

    def _pack_card(self, parent, title: str, subtitle: str = "",
                   **pack_kw) -> tk.Frame:
        """建卡片并按 ``pack_kw`` 放进 ``parent``，返回内容区。"""
        outer, inner = self._new_card(parent, title, subtitle)
        outer.pack(**pack_kw)
        return inner

    def _build_left_card(self, parent) -> tk.Frame:
        """左侧「发现的设备」卡片（固定宽度的 WhiteSurface 列表）。"""
        outer, inner = self._new_card(parent, "发现的设备", "自动刷新")
        outer.configure(width=384)
        outer.pack(side=LEFT, fill=Y)
        outer.pack_propagate(False)
        return inner

    def _build_log_card(self, parent) -> tk.Frame:
        outer, inner = self._new_card(parent, "日志", f"最多保留 {LOG_LIMIT} 行")

        outer.pack(fill=BOTH, expand=True, pady=(CARD_GAP, 0))
        return inner

    def _load_logo(self) -> None:
        """标题栏图标（找不到就留空，不能因此崩）。"""
        from . import paths

        candidates = []
        for base in (os.path.dirname(str(paths.installed_icon_path() or "")),
                     os.path.dirname(str(paths.exe_dir_icon_path() or ""))):
            if base:
                candidates.append(os.path.join(base, "zongxian-icon.png"))
        candidates.append(os.path.join(os.path.dirname(paths.DEV_ICON_PATH),
                                       "zongxian-icon.png"))
        for path in candidates:
            if not path or not os.path.isfile(path):
                continue
            try:
                image = tk.PhotoImage(file=path)
                factor = max(1, image.width() // ICON_SIZE)
                if factor > 1:
                    image = image.subsample(factor, factor)
                self.logo_image = image
                return
            except Exception:                              # noqa: BLE001
                continue

    def _icon_candidates(self) -> list[str]:
        """窗口/任务栏图标按优先级列出候选（都是 zongxian-synced.ico）。"""
        from . import foldericon, paths

        cands: list[str] = []
        try:
            cands.append(foldericon.resolve_icon())
        except Exception:                                  # noqa: BLE001
            pass
        cands.extend([paths.installed_icon_path(), paths.exe_dir_icon_path(),
                      paths.synced_icon_path(), paths.DEV_ICON_PATH])
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            cands.append(os.path.join(meipass, paths.SYNCED_ICON_NAME))
        out: list[str] = []
        for c in cands:
            if c and os.path.isfile(c) and os.path.abspath(c) not in out:
                out.append(os.path.abspath(c))
        return out

    def _set_window_icon(self) -> None:
        """标题栏 / 任务栏图标：统一的 zongxian-synced.ico。

        ``iconbitmap(default=...)`` 会把图标挂到所有顶层窗口（含以后弹出的
        对话框），任务栏因此也显示它；再补一张 32px 的 iconphoto 作保险，
        某些 Windows 版本对 .ico 的多尺寸帧挑食。
        """
        cands = self._icon_candidates()
        if not cands:
            return
        used = cands[0]
        for cand in cands:
            try:
                self.root.iconbitmap(default=cand)
                used = cand
                break
            except Exception:                              # noqa: BLE001
                try:
                    self.root.iconbitmap(cand)
                    used = cand
                    break
                except Exception:                          # noqa: BLE001
                    continue
        png = os.path.join(os.path.dirname(used), "zongxian-icon.png")
        if not os.path.isfile(png):
            return
        try:
            img = tk.PhotoImage(file=png)
            factor = max(1, img.width() // 32)
            if factor > 1:
                img = img.subsample(factor, factor)
            self._icon_photo = img                          # 防 GC
            self.root.iconphoto(True, img)
        except Exception:                                  # noqa: BLE001
            pass

    # -- 界面 -------------------------------------------------------------
    def _build(self) -> None:
        self._set_window_icon()
        self._load_logo()

        outer = ttk.Frame(self.root, padding=PAD_WINDOW)
        outer.pack(fill=BOTH, expand=True)

        # ---- 顶部标题栏（图标 + 产品名 + 版本）
        header = ttk.Frame(outer)
        header.pack(fill=X)
        if self.logo_image is not None:
            tk.Label(header, image=self.logo_image, background=CLR_WINDOW,
                     borderwidth=0).pack(side=LEFT, padx=(2, 12))
        titlebox = ttk.Frame(header)
        titlebox.pack(side=LEFT)
        ttk.Label(titlebox, text=APP_NAME, style="Title.TLabel").pack(anchor="w",
                                                                      pady=(1, 0))
        ttk.Label(titlebox,
                  text=f"版本 {VERSION} · 朋友间大文件传输 + 文件夹同步"
                       f"（局域网 / 纯标准库）",
                  style="Subtitle.TLabel").pack(anchor="w")
        self.lbl_self = ttk.Label(header, text="", style="Self.TLabel")
        self.lbl_self.pack(side=RIGHT, anchor="e")

        # 标题栏和主体之间一条 1px 分隔线，让头部有“工具栏”的收口感
        tk.Frame(outer, background=CLR_BORDER, height=1).pack(fill=X, pady=(8, 0))

        # ---- 主体
        body = ttk.Frame(outer)
        body.pack(fill=BOTH, expand=True, pady=(8, CARD_GAP))

        # 左：发现的设备（WhiteSurface 卡片 + 选中高亮）
        left_card = self._build_left_card(body)
        tree_wrap = tk.Frame(left_card, background=CLR_SURFACE)
        tree_wrap.pack(fill=BOTH, expand=True, padx=PAD_CARD_X,
                       pady=(PAD_HEAD_BOT, PAD_ROW_Y))
        self.tree = ttk.Treeview(tree_wrap, columns=("ip", "port", "age"),
                                 show="tree headings", height=14,
                                 style="Peers.Treeview", selectmode="browse")
        self.tree.heading("#0", text="名称", anchor="w")
        self.tree.heading("ip", text="IP", anchor="w")
        self.tree.heading("port", text="端口", anchor="center")
        self.tree.heading("age", text="上次", anchor="center")
        # 列宽之和要放得下（含右侧滚动条），否则「上次」那一列会被切掉看不见
        self.tree.column("#0", width=132, minwidth=90, stretch=True)
        self.tree.column("ip", width=106, minwidth=90, anchor="w", stretch=False)
        self.tree.column("port", width=50, minwidth=44, anchor="center",
                         stretch=False)
        self.tree.column("age", width=46, minwidth=40, anchor="center",
                         stretch=False)
        self.tree.tag_configure("odd", background=CLR_ROW_ALT)
        sb = ttk.Scrollbar(tree_wrap, orient=VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill=Y)
        self.tree.pack(side=LEFT, fill=BOTH, expand=True)
        self.tree.bind("<Double-1>", lambda _e: self.use_selected_peer())
        peer_bar = ttk.Frame(left_card, style="Card.TFrame")
        peer_bar.pack(fill=X, padx=PAD_CARD_X, pady=(0, PAD_ROW_BOT))
        ttk.Button(peer_bar, text="刷新", style="Subtle.TButton",
                   command=self.refresh_peers_once).pack(side=LEFT)
        ttk.Button(peer_bar, text="使用选中", style="Subtle.TButton",
                   command=self.use_selected_peer).pack(side=LEFT, padx=(6, 0))

        # 右：操作区
        right = ttk.Frame(body)
        right.pack(side=LEFT, fill=BOTH, expand=True, padx=(12, 0))

        # 目标设备
        conn = self._pack_card(right, "目标设备", fill=X)
        row = ttk.Frame(conn, style="Card.TFrame")
        row.pack(fill=X, padx=PAD_CARD_X, pady=(PAD_ROW_TOP, PAD_ROW_BOT))
        ttk.Label(row, text="IP:端口", style="Card.TLabel").pack(side=LEFT)
        self.var_target = StringVar(value="")
        self.var_port = StringVar(value=str(self.port))
        ttk.Entry(row, textvariable=self.var_target, width=22).pack(
            side=LEFT, padx=(8, 14))
        ttk.Label(row, text="数据端口", style="Card.TLabel").pack(side=LEFT)
        ttk.Entry(row, textvariable=self.var_port, width=7).pack(
            side=LEFT, padx=(8, 14))
        ttk.Label(row, text="并发流", style="Card.TLabel").pack(side=LEFT)
        self.var_streams = StringVar(value=str(DEFAULT_STREAMS))
        ttk.Spinbox(row, from_=1, to=8, width=4,
                    textvariable=self.var_streams).pack(side=LEFT, padx=(8, 0))

        # 发送
        send_box = self._pack_card(right, "发送文件 / 文件夹", fill=X,
                                   pady=(CARD_GAP, 0))
        row = ttk.Frame(send_box, style="Card.TFrame")
        row.pack(fill=X, padx=PAD_CARD_X, pady=(PAD_ROW_TOP, PAD_ROW_BOT))
        self.var_send = StringVar(value="")
        ttk.Entry(row, textvariable=self.var_send).pack(
            side=LEFT, fill=X, expand=True)
        ttk.Button(row, text="选文件", style="Subtle.TButton",
                   command=self.pick_files).pack(side=LEFT, padx=(8, 6))
        ttk.Button(row, text="选文件夹", style="Subtle.TButton",
                   command=self.pick_send_dir).pack(side=LEFT, padx=(0, 8))
        self.btn_send = ttk.Button(row, text="开始发送", style="Accent.TButton",
                                   command=self.start_send)
        self.btn_send.pack(side=LEFT)

        # 接收
        recv_box = self._pack_card(right, "接收", fill=X, pady=(CARD_GAP, 0))
        row = ttk.Frame(recv_box, style="Card.TFrame")
        row.pack(fill=X, padx=PAD_CARD_X, pady=(PAD_ROW_TOP, PAD_ROW_BOT))
        self.var_recv = StringVar(value=self.default_dir)
        ttk.Entry(row, textvariable=self.var_recv).pack(
            side=LEFT, fill=X, expand=True)
        ttk.Button(row, text="选目录", style="Subtle.TButton",
                   command=self.pick_recv_dir).pack(side=LEFT, padx=(8, 6))
        self.btn_recv = ttk.Button(row, text="开始接收",
                                   command=self.toggle_recv)
        self.btn_recv.pack(side=LEFT)

        # 同步
        sync_box = self._pack_card(right, "同步目录", fill=X, pady=(CARD_GAP, 0))
        row = ttk.Frame(sync_box, style="Card.TFrame")
        row.pack(fill=X, padx=PAD_CARD_X, pady=(PAD_ROW_TOP, PAD_ROW_Y))
        self.var_sync = StringVar(value="")
        ttk.Entry(row, textvariable=self.var_sync).pack(
            side=LEFT, fill=X, expand=True)
        ttk.Button(row, text="选目录", style="Subtle.TButton",
                   command=self.pick_sync_dir).pack(side=LEFT, padx=(8, 6))
        self.btn_sync = ttk.Button(row, text="开始同步", style="Accent.TButton",
                                   command=self.start_sync)
        self.btn_sync.pack(side=LEFT)

        opt = ttk.Frame(sync_box, style="Card.TFrame")
        opt.pack(fill=X, padx=PAD_CARD_X, pady=(0, PAD_ROW_Y))
        self.var_watch = BooleanVar(value=False)
        self.var_two_way = BooleanVar(value=True)
        self.var_delete = BooleanVar(value=False)
        ttk.Checkbutton(opt, text="持续监控", variable=self.var_watch,
                        style="Card.TCheckbutton").pack(side=LEFT)
        ttk.Checkbutton(opt, text="双向同步", variable=self.var_two_way,
                        style="Card.TCheckbutton").pack(side=LEFT, padx=14)
        ttk.Checkbutton(opt, text="删除多余", variable=self.var_delete,
                        style="Card.TCheckbutton").pack(side=LEFT)

        # 开机自启同步（本任务新增）
        auto = ttk.Frame(sync_box, style="Card.TFrame")
        auto.pack(fill=X, padx=PAD_CARD_X, pady=(0, PAD_ROW_Y))
        self.var_autostart = BooleanVar(value=False)
        self.chk_autostart = ttk.Checkbutton(
            auto, text="这个文件夹开机自动同步", variable=self.var_autostart,
            style="Card.TCheckbutton", command=self.on_autostart_toggle)
        self.chk_autostart.pack(side=LEFT)
        self.btn_autostart_off = ttk.Button(
            auto, text="移除开机同步", style="Subtle.TButton",
            command=self.remove_autostart)
        self.btn_autostart_off.pack(side=RIGHT)
        self.btn_unmark = ttk.Button(
            auto, text="取消同步标记", style="Subtle.TButton",
            command=self.unmark_sync_icon)
        self.btn_unmark.pack(side=RIGHT, padx=(0, 6))
        self.btn_view_log = ttk.Button(
            auto, text="查看同步日志", style="Subtle.TButton",
            command=self.open_sync_log)
        self.btn_view_log.pack(side=RIGHT, padx=(0, 6))

        stop_row = ttk.Frame(sync_box, style="Card.TFrame")
        stop_row.pack(fill=X, padx=PAD_CARD_X, pady=(0, PAD_ROW_BOT))
        ttk.Label(stop_row, text="勾选后写入 HKCU\\...\\Run，开机自动后台同步",
                  style="CardMute.TLabel").pack(side=LEFT)
        self.btn_stop = ttk.Button(stop_row, text="停止", style="Subtle.TButton",
                                   command=self.stop_all, state="disabled")
        self.btn_stop.pack(side=RIGHT)

        # 跨网传输：把原版网页版装进一个独立窗口（用系统 Edge/Chrome 作内核）
        web_box = self._pack_card(right, "跨网传输（不同网络之间）", fill=X,
                                  pady=(CARD_GAP, 0))
        web_row = ttk.Frame(web_box, style="Card.TFrame")
        web_row.pack(fill=X, padx=PAD_CARD_X, pady=(PAD_ROW_TOP, PAD_ROW_Y))
        self.btn_webapp = ttk.Button(web_row, text="打开内置网页版",
                                     style="Accent.TButton", command=self.open_webapp)
        self.btn_webapp.pack(side=LEFT)
        ttk.Button(web_row, text="复制局域网地址", style="Subtle.TButton",
                   command=self.copy_lan_url).pack(side=LEFT, padx=(8, 0))
        ttk.Button(web_row, text="组网工具下载", style="Subtle.TButton",
                   command=self.open_group_tools).pack(side=LEFT, padx=(8, 0))
        ttk.Label(web_box, style="CardMute.TLabel", wraplength=420, justify=LEFT,
                  text="局域网用上面的多路 TCP（最快）；不同网络时用这个窗口：\n"
                       "一方点“生成取件码”，另一方输入取件码，即可跨网直连传文件；\n"
                       "同一个 WiFi 下手机浏览器也能直接打开本机地址。").pack(
            fill=X, padx=PAD_CARD_X, pady=(0, PAD_ROW_BOT))

        # 连接诊断：判断「慢是慢在哪」（网络 / 磁盘 / 链路质量），全部实测
        self.diag = DiagPanel(self, right)

        # ---- 底部：细进度条 + 状态行
        prog_wrap = ttk.Frame(outer)
        prog_wrap.pack(fill=X, pady=(0, 6))
        self.pb = ThinProgress(prog_wrap, height=PROGRESS_H)
        self.pb.pack(fill=X)
        self.lbl_prog = ttk.Label(prog_wrap, text="就绪", style="Status.TLabel")
        self.lbl_prog.pack(fill=X, pady=(4, 0))

        log_card = self._build_log_card(outer)
        text_wrap = tk.Frame(log_card, background=CLR_SURFACE)
        text_wrap.pack(fill=BOTH, expand=True, padx=PAD_CARD_X,
                       pady=(PAD_HEAD_BOT, PAD_ROW_BOT))
        self.txt = tk.Text(text_wrap, height=LOG_HEIGHT, wrap="none",
                           state="disabled",
                           background=CLR_LOG_BG, foreground=CLR_LOG_FG,
                           insertbackground=CLR_TEXT, relief="flat",
                           borderwidth=0, padx=10, pady=8,
                           highlightthickness=1,
                           highlightbackground=CLR_BORDER,
                           highlightcolor=CLR_ACCENT,
                           spacing1=1, spacing3=1,
                           selectbackground=CLR_SELECT,
                           selectforeground=CLR_TEXT,
                           font=self.mono or (self.font_family, FONT_LOG))
        # 时间戳用更淡的颜色，正文正常——日志一眼能扫到“说了什么”
        self.txt.tag_configure("ts", foreground=CLR_LOG_TS)
        self.txt.tag_configure("err", foreground=CLR_LOG_ERR)
        self.txt.tag_configure("msg_err", foreground=CLR_LOG_ERR)
        sb = ttk.Scrollbar(text_wrap, orient=VERTICAL, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill=Y, padx=(2, 0))
        self.txt.pack(side=LEFT, fill=BOTH, expand=True)

        self.lbl_self.configure(
            text=f"本机 {socket.gethostname()} · 默认端口 {self.port}")
        self.log(f"{APP_NAME} {VERSION} 已启动")
        self.log("提示：先在对方机器执行 `python -m swiftdrop recv`，"
                 "再在左侧选择设备发送。")

    # -- 日志 -------------------------------------------------------------
    def log(self, msg: str) -> None:
        self.events.put(("log", msg))

    def _append_log(self, msg: str) -> None:
        # 时间戳单独打一个淡色标签，正文保持正常对比度（只影响观感）
        err = msg.startswith(("发送失败", "同步失败", "接收失败"))
        self.txt.configure(state="normal")
        self.txt.insert(END, f"{time.strftime('%H:%M:%S')}  ", ("ts",))
        self.txt.insert(END, f"{msg}\n", ("msg_err",) if err else ())
        lines = int(self.txt.index("end-1c").split(".")[0])
        if lines > LOG_LIMIT:
            self.txt.delete("1.0", f"{lines - LOG_LIMIT}.0")
        self.txt.see(END)
        self.txt.configure(state="disabled")
        if err:
            self._set_status_state("error")

    # -- 设备发现 ---------------------------------------------------------
    def _start_discovery(self) -> None:
        try:
            self.discovery = DiscoveryService(data_port=self.port).start()
            self.log(f"设备发现已启动（UDP {self.discovery.port}）")
        except OSError as exc:
            self.log(f"设备发现启动失败：{exc}（仍可手动输入 IP:端口）")

    def refresh_peers_once(self) -> None:
        if not self.discovery:
            return
        peers = self.discovery.peer_table()
        self._fill_peers(peers)

    def _fill_peers(self, peers: list[dict]) -> None:
        selected = None
        sel = self.tree.selection()
        if sel:
            selected = self.tree.item(sel[0], "text")
        self.tree.delete(*self.tree.get_children())
        for i, p in enumerate(peers):
            iid = f"{p['ip']}:{p['port']}"
            # 隔行淡底：纯观感，方便横扫多设备（选中高亮不受影响）
            self.tree.insert("", END, iid=iid, text=p["name"],
                             values=(p["ip"], p["port"], f"{p['age']:.0f}s"),
                             tags=("odd",) if i % 2 else ())
            if selected and p["name"] == selected:
                self.tree.selection_set(iid)

    def use_selected_peer(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        vals = self.tree.item(iid, "values")
        self.var_target.set(f"{vals[0]}")
        self.var_port.set(str(vals[1]))
        self.log(f"已选择目标 {self.tree.item(iid, 'text')} {vals[0]}:{vals[1]}")

    # -- 选择路径 ---------------------------------------------------------
    def pick_files(self) -> None:
        paths = filedialog.askopenfilenames(title="选择要发送的文件")
        if paths:
            self.var_send.set(";".join(paths))

    def pick_send_dir(self) -> None:
        path = filedialog.askdirectory(title="选择要发送的文件夹")
        if path:
            self.var_send.set(path)

    def pick_recv_dir(self) -> None:
        path = filedialog.askdirectory(title="选择接收目录")
        if path:
            self.var_recv.set(path)

    def pick_sync_dir(self) -> None:
        path = filedialog.askdirectory(title="选择同步目录")
        if not path:
            return
        self.var_sync.set(path)
        # 勾着「开机自动同步」时先把图标打上，用户能立刻在资源管理器看到
        if self.var_autostart.get():
            self._mark_icon(path)
        self._sync_autostart_ui()

    # -- 开机自启同步（任务 2 / 任务 3 的 UI）-----------------------------
    @staticmethod
    def _looks_like_ip(text: str) -> bool:
        parts = (text or "").split(".")
        return (len(parts) == 4
                and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts))

    def _sync_autostart_ui(self) -> None:
        """把界面状态对齐到配置文件（不触发写配置）。"""
        try:
            from .autostart import load_config
            cfg = load_config()
        except Exception:                                  # noqa: BLE001
            cfg = {"folders": []}
        raw = self.var_sync.get().strip()
        hit: list[dict] = []
        if raw:
            key = os.path.normcase(os.path.abspath(raw))
            hit = [f for f in cfg.get("folders") or []
                   if os.path.normcase(str(f.get("local"))) == key]
        self._autostart_guard = True
        try:
            self.var_autostart.set(bool(hit))
        finally:
            self._autostart_guard = False
        n = len(cfg.get("folders") or [])
        self.chk_autostart.configure(
            text=f"这个文件夹开机自动同步（已登记 {n} 个）" if n
            else "这个文件夹开机自动同步")
        self._apply_status(f"已登记 {n} 个开机同步文件夹")

    def status(self, text: str) -> None:
        self.events.put(("status", text))

    #: 语义状态 → 状态行样式（纯配色，不参与任何逻辑）
    STATUS_STYLES = {
        "idle": "Status.TLabel",
        "busy": "StatusBusy.TLabel",
        "done": "StatusDone.TLabel",
        "warn": "StatusDone.TLabel",
        "error": "StatusError.TLabel",
    }

    def _set_status_state(self, state: str) -> None:
        """进度条 + 状态行的状态配色（忙碌蓝 / 完成绿 / 失败红 / 空闲灰）。"""
        style = self.STATUS_STYLES.get(state, "Status.TLabel")
        label = getattr(self, "lbl_prog", None)
        if label is not None:
            try:
                if str(label.cget("style")) != style:
                    label.configure(style=style)
            except tk.TclError:
                pass
        bar = getattr(self, "pb", None)
        if bar is not None:
            bar.set_state(state if state in PROGRESS_STATE_COLORS else "busy")

    def _apply_status(self, text: str, state: str = "idle") -> None:
        try:
            self.lbl_prog.configure(text=text)
        except tk.TclError:
            return
        self._set_status_state(state)

    def on_autostart_toggle(self) -> None:
        if self._autostart_guard:
            return
        local = self.var_sync.get().strip()
        if not os.path.isdir(local):
            messagebox.showwarning("目录无效", "请先选择一个存在的同步目录")
            self._autostart_guard = True
            try:
                self.var_autostart.set(False)
            finally:
                self._autostart_guard = False
            return
        if self.var_autostart.get():
            self._register_autostart(local)
        else:
            self._unregister_autostart(local)

    def _register_autostart(self, local: str) -> None:
        try:
            from .autostart import add_folder, enable_autostart
        except Exception as exc:                           # noqa: BLE001
            messagebox.showerror("不可用", f"开机自启模块加载失败：{exc}")
            return
        peer = self.var_target.get().strip()
        try:
            port = int(self.var_port.get().strip() or self.port)
        except ValueError:
            port = self.port
        is_host = ":" in peer or self._looks_like_ip(peer)
        try:
            add_folder({
                "local": os.path.abspath(local),
                "peer": "" if is_host else peer,
                "host": peer if is_host else "",
                "port": port,
                "mode": "two-way" if self.var_two_way.get() else "one-way",
                "delete_extra": bool(self.var_delete.get()),
                "interval": 5,
                "mark_icon": True,
            })
            cmd = enable_autostart()
        except Exception as exc:                           # noqa: BLE001
            messagebox.showerror("登记失败", f"写配置/注册表失败：{exc}")
            self.log(f"开机自启登记失败：{exc}")
            return
        self.log(f"已登记开机同步文件夹：{local}")
        self.log(f"注册表 HKCU\\...\\Run 的「{APP_NAME}」= {cmd}")
        self._mark_icon(local)
        self._sync_autostart_ui()
        self.log("开机自启同步已开启；注销后重新登录会自动后台同步。")

    def _unregister_autostart(self, local: str) -> None:
        try:
            from .autostart import disable_autostart, list_folders, remove_folder
            remove_folder(local)
            remaining = list_folders()
            if not remaining:
                disable_autostart()
        except Exception as exc:                           # noqa: BLE001
            self.log(f"移除开机同步失败：{exc}")
            return
        self.log(f"已移除开机同步登记：{local}")
        self._unmark_icon(local)
        self._sync_autostart_ui()

    def remove_autostart(self) -> None:
        """按钮：移除当前同步目录的开机同步登记。"""
        local = self.var_sync.get().strip()
        if not os.path.isdir(local):
            messagebox.showwarning("目录无效", "请先选择一个存在的同步目录")
            return
        self._unregister_autostart(local)

    def _mark_icon(self, folder: str) -> None:
        try:
            from .foldericon import mark_folder, resolve_icon, reveal_hint
            ico = resolve_icon()
            if not ico:
                self.log("未找到同步图标文件，跳过文件夹图标标记")
                return
            mark_folder(folder, ico)
            self.log(f"已给「{os.path.basename(folder) or folder}」打上同步图标"
                     f"（{reveal_hint()}）")
        except Exception as exc:                           # noqa: BLE001
            self.log(f"文件夹图标标记跳过：{exc}")
            if not self._icon_warned:
                self._icon_warned = True

    def _unmark_icon(self, folder: str) -> None:
        try:
            from .foldericon import reveal_hint, unmark_folder
            if unmark_folder(folder):
                self.log(f"已取消「{os.path.basename(folder) or folder}」"
                         f"的同步图标（{reveal_hint()}）")
        except Exception as exc:                           # noqa: BLE001
            self.log(f"取消同步标记失败：{exc}")

    def unmark_sync_icon(self) -> None:
        """按钮：只取消文件夹图标标记，不动同步登记。"""
        local = self.var_sync.get().strip()
        if not local:
            messagebox.showwarning("未选目录", "请先选择同步目录")
            return
        self._unmark_icon(local)

    # ---- 跨网传输：内置网页版窗口 ----------------------------------------
    def _ensure_web_host(self):
        """按需启动内置局域网服务（页面 + /signal 信令中继），只启动一次。"""
        if getattr(self, "_web_host", None) is not None:
            return self._web_host
        from .webhost import DEFAULT_INDEX, WebHost, default_root, urls_for

        root = default_root()
        if not os.path.isfile(os.path.join(root, DEFAULT_INDEX)):
            root = os.getcwd()
        host = WebHost(root, 8787, DEFAULT_INDEX, quiet=True,
                       on_log=lambda m: self.log("[内置网页版] " + m))
        try:
            host.start(background=True)
        except OSError as exc:
            self.log(f"内置网页版服务启动失败（端口被占用？）：{exc}")
            return None
        self._web_host = host
        self._web_lan = urls_for(host.port, host.root)
        for u in self._web_lan:
            self.log("局域网可访问：" + u)
        try:
            from .netgroup import group_nics

            for n in group_nics():
                self.log(f"异地组网可访问（{n.tool}）：http://{n.ip}:{host.port}/swiftdrop.html"
                         f"  ← 异地的朋友用这个")
        except Exception:
            pass
        return host

    def open_webapp(self) -> None:
        """按钮：用系统 Edge/Chrome 的应用窗口打开内置网页版（跨网传输用它）。"""
        try:
            from urllib.parse import quote

            from . import webview
            from .netgroup import primary_group
            from .webhost import DEFAULT_INDEX

            host = self._ensure_web_host()
            if host is None:
                return
            local = f"http://127.0.0.1:{host.port}/{DEFAULT_INDEX}"
            lan = getattr(self, "_web_lan", [])
            group = primary_group()
            if group:
                # 分享地址给"异地组网"地址：异地朋友只有这个能打开
                base = f"http://{group.ip}:{host.port}/{DEFAULT_INDEX}"
                url = local + "?lan=" + quote(base, safe="")
                if lan:
                    url += "&lanlocal=" + quote(lan[0].split("#")[0], safe="")
            elif lan:
                url = local + "?lan=" + quote(lan[0].split("#")[0], safe="")
            else:
                url = local
            proc, exe = webview.open_app_window(url)
            if proc is None:
                self.log("未找到 Edge/Chrome，已用系统默认浏览器打开：" + local)
            else:
                self.log("已打开内置网页版窗口：" + local)
            self.log("跨网传输：一方点「生成取件码」，另一方输入取件码点「连接」；"
                     "同一个 WiFi 下手机浏览器也能直接打开上面的局域网地址。")
        except Exception as exc:                            # noqa: BLE001
            self.log("打开内置网页版失败：" + str(exc))

    def copy_lan_url(self) -> None:
        """按钮：复制分享地址（有异地组网就优先给组网地址，否则给局域网地址）。"""
        from .netgroup import primary_group, share_url

        if not getattr(self, "_web_lan", None):
            self._ensure_web_host()
        urls = getattr(self, "_web_lan", None) or []
        host = getattr(self, "_web_host", None)
        port = host.port if host is not None else 8787
        group = primary_group()
        if group:
            text = share_url(group.ip, port)
            tip = f"（异地组网 {group.tool} 地址，对方也要在同一个组网里）"
        elif urls:
            text = urls[0].split("#")[0]
            tip = "（同一个 WiFi 下用）"
        else:
            self.log("暂时没有可分享的地址")
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.log("已复制分享地址：" + text + tip)
            if group and urls:
                self.log("  同一 WiFi 下也可以用：" + urls[0].split("#")[0])
        except Exception as exc:                            # noqa: BLE001
            self.log("复制失败：" + str(exc) + "  地址是：" + text)

    def open_group_tools(self) -> None:
        """按钮：异地组网工具（Tailscale/Radmin/ZeroTier）的下载页与用法。"""
        from .netgroup import TOOLS, group_nics

        nics = group_nics()
        if nics:
            n = nics[0]
            self.log(f"已检测到异地组网：{n.tool} → {n.ip}（异地的朋友用这个地址，或点「复制局域网地址」拿它）")
        else:
            self.log("还没检测到异地组网网卡。推荐装下面任意一个（都免费），两边加入同一个网络即可：")
        for name, url, plat, why in TOOLS:
            self.log(f"  · {name}（{plat}）{url} —— {why}")
        # 打开最推荐的那个（手机要参与就是 Tailscale；只有 Windows 电脑之间用 Radmin 也一样好）
        target = TOOLS[0][1]
        try:
            import webbrowser

            webbrowser.open(target)
            self.log("已在浏览器打开推荐工具的下载页：" + target)
        except Exception as exc:                            # noqa: BLE001
            self.log("打开浏览器失败：" + str(exc))

    def open_sync_log(self) -> None:
        """按钮：用记事本打开 autosync 日志。"""
        from . import paths

        path = paths.log_path()
        if not os.path.isfile(path):
            messagebox.showinfo("暂无日志",
                                f"还没有 autosync 日志：\n{path}\n\n"
                                f"勾选「这个文件夹开机自动同步」并重启（或运行 "
                                f"`python -m swiftdrop autosync --verbose`）"
                                f"之后就会有。")
            return
        try:
            os.startfile(path)                             # noqa: S606
            self.log(f"已用默认程序打开日志：{path}")
        except OSError as exc:
            messagebox.showerror("打不开日志", f"{path}\n{exc}")

    # -- 动作 -------------------------------------------------------------
    def _busy(self, busy: bool, stop_enabled: bool = True) -> None:
        state = "disabled" if busy else "normal"
        for btn in (self.btn_send, self.btn_sync):
            btn.configure(state=state)
        self.btn_stop.configure(
            state="normal" if (busy and stop_enabled) else "disabled")
        if busy:
            # 一开始传就让进度条/状态行切到“进行中”的语义色
            self._set_status_state("busy")

    def _target(self) -> tuple[str, int] | None:
        host = self.var_target.get().strip()
        if not host:
            messagebox.showwarning("缺少目标", "请先选择设备或输入 IP:端口")
            return None
        port_s = self.var_port.get().strip()
        if ":" in host:
            host, _, ps = host.rpartition(":")
            port_s = ps or port_s
        try:
            port = int(port_s)
        except ValueError:
            messagebox.showwarning("端口非法", f"端口必须是数字：{port_s!r}")
            return None
        return host, port

    def _streams(self) -> int:
        try:
            return max(1, min(8, int(self.var_streams.get())))
        except ValueError:
            return DEFAULT_STREAMS

    def start_send(self) -> None:
        target = self._target()
        if not target:
            return
        raw = self.var_send.get().strip()
        if not raw:
            messagebox.showwarning("缺少内容", "请选择要发送的文件或文件夹")
            return
        paths = [p for p in raw.split(";") if p.strip()]
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            messagebox.showwarning("路径不存在", "\n".join(missing))
            return
        host, port = target
        self.stop.clear()
        self._busy(True)

        def work() -> None:
            try:
                Sender(host, port, paths, streams=self._streams(),
                       on_log=self.log, on_progress=self._on_progress,
                       stop_event=self.stop).run()
            except Exception as exc:                       # noqa: BLE001
                self.log(f"发送失败：{exc}")
                self.log(traceback.format_exc(limit=3))
            finally:
                self.events.put(("busy", False))

        self.log(f"开始发送 {len(paths)} 项 → {host}:{port}")
        self.worker = threading.Thread(target=work, name="gui-send", daemon=True)
        self.worker.start()

    def toggle_recv(self) -> None:
        if self.recv_running:
            self._stop_recv()
            return
        dest = self.var_recv.get().strip() or self.default_dir
        try:
            port = int(self.var_port.get().strip() or self.port)
        except ValueError:
            port = self.port
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("目录不可用", str(exc))
            return
        self.stop.clear()
        try:
            self.receiver = ReceiverServer(
                dest, port, on_log=self.log,
                on_progress=self._effective_progress,
                stop_event=self.stop)
            self.receiver.start()
        except OSError as exc:
            messagebox.showerror("端口占用", f"无法监听 {port}：{exc}")
            return
        self.recv_running = True
        self.btn_recv.configure(text="停止接收")
        self.log(f"开始接收，监听 {port} → {dest}")

    def _stop_recv(self) -> None:
        if self.receiver:
            self.receiver.stop()
            self.receiver = None
        self.recv_running = False
        self.btn_recv.configure(text="开始接收")
        self.log("接收已停止")

    def start_sync(self) -> None:
        target = self._target()
        if not target:
            return
        local = self.var_sync.get().strip()
        if not local or not os.path.isdir(local):
            messagebox.showwarning("目录无效", "请选择存在的本地同步目录")
            return
        host, port = target
        self.sync_stop = threading.Event()
        self._busy(True)
        engine = SyncEngine(
            local, host, port, two_way=bool(self.var_two_way.get()),
            delete_extra=bool(self.var_delete.get()), streams=self._streams(),
            on_log=self.log, on_progress=self._on_progress,
            stop_event=self.sync_stop)

        def work() -> None:
            try:
                if self.var_watch.get():
                    engine.watch(interval=3.0)
                else:
                    report = engine.run_once()
                    self.log("同步完成: " + report.summary())
                    for c in report.conflicts:
                        self.log(f"冲突跳过: {c['path']} —— {c['why']}")
            except Exception as exc:                       # noqa: BLE001
                self.log(f"同步失败：{exc}")
                self.log(traceback.format_exc(limit=3))
            finally:
                self.events.put(("busy", False))

        mode = "双向" if self.var_two_way.get() else "单向"
        watch = "（持续监控）" if self.var_watch.get() else ""
        self.log(f"开始{mode}同步{watch}: {local} ↔ {host}:{port}")
        self.worker = threading.Thread(target=work, name="gui-sync", daemon=True)
        self.worker.start()

    def stop_all(self) -> None:
        self.stop.set()
        if self.sync_stop:
            self.sync_stop.set()
        self.log("已发送停止信号…")
        self.btn_stop.configure(state="disabled")

    # -- 进度 -------------------------------------------------------------
    def _effective_progress(self, ev: dict) -> None:
        """接收端的进度回调：接收端内部也有进度，这里统一转发。"""
        self._on_progress(ev)

    def _on_progress(self, ev: dict) -> None:
        self.events.put(("progress", ev))

    #: 传输阶段 → 进度条 / 状态行配色（纯外观映射，不参与任何计算）
    PHASE_STATES = {"send": "busy", "recv": "busy", "send-done": "done",
                    "recv-idle": "idle"}

    def _apply_progress(self, ev: dict) -> None:
        phase = ev.get("phase", "")
        # 连接诊断：主线程喂原始数据 + 传输中自动开始采样
        if self.diag is not None:
            self.diag.on_progress(ev)
        stage_state = self.PHASE_STATES.get(str(phase))
        if stage_state:
            self._set_status_state(stage_state)
        if phase == "recv-idle":
            self.pb.configure(value=0)
            self.lbl_prog.configure(text="接收空闲，等待发送端…")
            return
        total = int(ev.get("total", 0) or 0)
        done = int(ev.get("done", 0) or 0)
        rate = float(ev.get("rate", 0.0) or 0.0)
        frac = 100.0 if total <= 0 else min(100.0, done * 100.0 / total)
        self.pb.configure(value=frac)
        eta = (total - done) / rate if rate > 1e-6 else float("inf")
        extra = ""
        if "sent_files" in ev:
            extra = (f"  文件 {ev.get('sent_files', 0)}"
                     f" 跳过 {ev.get('skipped_files', 0)}"
                     f" 失败 {ev.get('failed', 0)}")
        elif "files_received" in ev:
            extra = (f"  文件 {ev.get('files_received', 0)}"
                     f" 跳过 {ev.get('files_skipped', 0)}"
                     f" 失败 {ev.get('failed', 0)}")
        cur = ev.get("current")
        self.lbl_prog.configure(
            text=(f"{frac:5.1f}%   {human_bytes(done)} / {human_bytes(total)}   "
                  f"{human_bytes(rate)}/s   剩余 {human_time(eta)}{extra}"
                  + (f"   当前 {cur}" if cur else "")))
        if int(ev.get("failed", 0) or 0) > 0:
            # 有失败项：整行转红（只换颜色，文案不动）
            self._set_status_state("error")

    # -- 事件循环 ---------------------------------------------------------
    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind == "progress":
                    self._apply_progress(payload)
                elif kind == "status":
                    self._apply_status(str(payload))
                elif kind == "busy":
                    self._busy(bool(payload), stop_enabled=False)
                    if self.diag is not None and not payload:
                        self.diag.on_transfer_end()
        except queue.Empty:
            pass
        except Exception:                                  # noqa: BLE001
            pass
        self._fail_count += 1
        if self._fail_count % 15 == 0:
            self.refresh_peers_once()
        self.root.after(120, self._pump)

    def on_close(self) -> None:
        try:
            self.stop.set()
            if self.sync_stop:
                self.sync_stop.set()
            if self.receiver:
                self.receiver.stop()
            if self.discovery:
                self.discovery.stop()
        finally:
            self.root.destroy()

    def run(self) -> int:
        self.root.mainloop()
        return 0


def main(port: int = DATA_PORT, dir: str | None = None) -> int:
    try:
        app = GuiApp(port=port, default_dir=dir)
    except Exception as exc:                               # noqa: BLE001
        print(f"无法启动图形界面：{exc}", flush=True)
        print("可能原因：无显示环境、tkinter 缺失或被 Tcl 初始化错误打断。"
              "可改用命令行：peers / recv / send / sync / webhost / relay / "
              "autosync / autostart / folders", flush=True)
        return 1
    return app.run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
