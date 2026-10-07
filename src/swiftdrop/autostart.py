"""开机自启 + 文件夹同步登记 + 无界面守护进程 ``autosync``。

三块东西
--------
1. **配置持久化**：``%APPDATA%\\ZongxianTransfer\\config.json``（ASCII 目录名）
   结构见 :func:`default_config`；
2. **开机自启**：写 ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``
   的值 ``棕仙的传输软件``，只用标准库 ``winreg``；
3. **守护进程**：``autosync`` 子命令。对 config 里每个文件夹按名字（或 host）
   找设备 → 复用 :class:`swiftdrop.sync.SyncEngine` 跑一轮双向同步 → 等
   ``interval`` 秒再来。找不到设备就退避重试（5s→10s→30s 封顶），**永不崩**。

命令构造
--------
* 冻结成 exe：``"<sys.executable>" autosync --hidden``
* 源码运行：``"<python.exe>" "<项目>\\build\\entry.py" autosync --hidden``
  （entry.py 会把 ``src`` 塞进 ``sys.path``，所以源码下也能跑）

单实例
------
``%APPDATA%\\ZongxianTransfer\\autosync.lock`` 里记 pid；启动时若发现同一个 pid
还活着，就直接打印原因退出（退出码 3）。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from collections.abc import Callable

from . import APP_NAME, VERSION
from . import paths

#: 注册表 Run 键下的值名 = 用户可见的产品名
RUN_VALUE_NAME = APP_NAME
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

MODE_TWO_WAY = "two-way"
MODE_ONE_WAY = "one-way"
VALID_MODES = (MODE_TWO_WAY, MODE_ONE_WAY)

RETRY_STEPS = (5.0, 10.0, 30.0)        # 找不到设备时的退避序列（秒）
DEFAULT_INTERVAL = 5.0                 # 找不到 interval 时的默认轮询秒数
LOG_MAX_BYTES = 2 * 1024 * 1024        # 单个日志文件上限 2MB
EXIT_ALREADY_RUNNING = 3

CONFIG_KEYS = ("device_name", "autostart", "folders")
FOLDER_DEFAULTS: dict = {
    "local": "",
    "peer": "",
    "host": "",
    "port": 45880,
    "mode": MODE_TWO_WAY,
    "delete_extra": False,
    "interval": 5,
    "mark_icon": True,
}


# ==========================================================================
# 配置读写
# ==========================================================================

def default_config() -> dict:
    return {
        "device_name": socket.gethostname(),
        "autostart": False,
        "folders": [],
    }


def _clean_folder(raw) -> dict | None:
    """把一条文件夹记录规整成标准结构；缺 local 就丢弃。"""
    if not isinstance(raw, dict):
        return None
    entry = dict(FOLDER_DEFAULTS)
    entry.update({k: v for k, v in raw.items() if k in FOLDER_DEFAULTS})
    local = str(entry.get("local") or "").strip()
    if not local:
        return None
    entry["local"] = os.path.abspath(os.path.expandvars(local))
    entry["peer"] = str(entry.get("peer") or "").strip()
    entry["host"] = str(entry.get("host") or "").strip()
    mode = str(entry.get("mode") or MODE_TWO_WAY).strip().lower()
    entry["mode"] = mode if mode in VALID_MODES else MODE_TWO_WAY
    try:
        entry["port"] = max(0, min(65535, int(entry.get("port") or 0))) or 45880
    except (TypeError, ValueError):
        entry["port"] = 45880
    entry["delete_extra"] = bool(entry.get("delete_extra"))
    try:
        entry["interval"] = max(1.0, float(entry.get("interval") or DEFAULT_INTERVAL))
    except (TypeError, ValueError):
        entry["interval"] = DEFAULT_INTERVAL
    entry["mark_icon"] = bool(entry.get("mark_icon", True))
    return entry


def normalize_config(raw) -> dict:
    cfg = default_config()
    if isinstance(raw, dict):
        for key in CONFIG_KEYS:
            if key in raw:
                cfg[key] = raw[key]
    cfg["device_name"] = str(cfg.get("device_name") or socket.gethostname())
    cfg["autostart"] = bool(cfg.get("autostart"))
    folders = cfg.get("folders")
    cleaned: list[dict] = []
    seen: set[str] = set()
    if isinstance(folders, list):
        for item in folders:
            entry = _clean_folder(item)
            if entry is None:
                continue
            key = entry["local"].lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(entry)
    cfg["folders"] = cleaned
    return cfg


def load_config() -> dict:
    """读配置；文件不存在/坏了都返回可用默认值（**永不抛异常**）。"""
    path = paths.config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return default_config()
    except (OSError, json.JSONDecodeError):
        # 坏了就备份一份，别直接覆盖用户的原始文件
        try:
            if os.path.isfile(path):
                os.replace(path, path + ".bad")
        except OSError:
            pass
        return default_config()
    return normalize_config(raw)


def save_config(cfg: dict) -> str:
    """原子写配置（先写 .tmp 再 os.replace）。返回配置文件路径。"""
    cfg = normalize_config(cfg)
    path = paths.config_path()
    paths.ensure_appdata()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    payload = json.dumps(cfg, ensure_ascii=False, indent=1)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
    os.replace(tmp, path)
    return path


# ==========================================================================
# 文件夹登记
# ==========================================================================

def list_folders() -> list[dict]:
    return list(load_config().get("folders") or [])


def _find_index(folders: list[dict], local_path: str) -> int:
    key = os.path.abspath(os.path.expandvars(str(local_path))).lower()
    for i, entry in enumerate(folders):
        if str(entry.get("local") or "").lower() == key:
            return i
    return -1


def add_folder(entry: dict) -> dict:
    """新增/更新一条同步文件夹记录，返回写进配置的那一条。"""
    clean = _clean_folder(entry)
    if clean is None:
        raise ValueError("缺少 local（本地文件夹路径），无法登记")
    cfg = load_config()
    folders = cfg.get("folders") or []
    idx = _find_index(folders, clean["local"])
    if idx >= 0:
        # 只覆盖调用方明确给出的字段，其余保留（比如已有 interval）
        merged = dict(folders[idx])
        merged.update({k: v for k, v in (entry or {}).items() if k in FOLDER_DEFAULTS})
        merged["local"] = clean["local"]
        folders[idx] = _clean_folder(merged) or clean
    else:
        folders.append(clean)
    cfg["folders"] = folders
    save_config(cfg)
    return clean


def remove_folder(local_path: str) -> bool:
    """按本地路径移除登记；返回是否真的删掉了。"""
    cfg = load_config()
    folders = cfg.get("folders") or []
    idx = _find_index(folders, local_path)
    if idx < 0:
        return False
    folders.pop(idx)
    cfg["folders"] = folders
    save_config(cfg)
    return True


def set_device_name(name: str) -> dict:
    cfg = load_config()
    cfg["device_name"] = str(name or "").strip() or socket.gethostname()
    save_config(cfg)
    return cfg


# ==========================================================================
# 注册表开机自启
# ==========================================================================

def autostart_command() -> str:
    """开机自启要执行的命令行（含引号，可直接进注册表）。"""
    exe = os.path.abspath(sys.executable)
    if getattr(sys, "frozen", False):
        return f'"{exe}" autosync --hidden'
    entry = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "build", "entry.py")
    if not os.path.isfile(entry):
        # 兜底：源码运行但 entry.py 不在（例如被单独拷走）
        return f'"{exe}" -m swiftdrop autosync --hidden'
    return f'"{exe}" "{os.path.abspath(entry)}" autosync --hidden'


def _run_key(create: bool = False):
    import winreg

    access = winreg.KEY_READ
    if create:
        access |= winreg.KEY_WRITE
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def enable_autostart(command: str | None = None) -> str:
    """写注册表 Run 项，并记 autostart=True。返回写入的命令行。"""
    import winreg

    cmd = command or autostart_command()
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                             winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, cmd)
    finally:
        winreg.CloseKey(key)
    cfg = load_config()
    cfg["autostart"] = True
    save_config(cfg)
    return cmd


def disable_autostart() -> bool:
    """删注册表 Run 项，并记 autostart=False。返回是否真的删掉了。"""
    import winreg

    removed = False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                             winreg.KEY_SET_VALUE)
    except FileNotFoundError:
        key = None
    if key is not None:
        try:
            winreg.DeleteValue(key, RUN_VALUE_NAME)
            removed = True
        except FileNotFoundError:
            pass
        finally:
            winreg.CloseKey(key)
    cfg = load_config()
    cfg["autostart"] = False
    save_config(cfg)
    return removed


def autostart_status() -> dict:
    """返回 ``{"enabled","command","expected","matches","config_says"}``。"""
    import winreg

    command = ""
    try:
        key = _run_key()
        try:
            command, _kind = winreg.QueryValueEx(key, RUN_VALUE_NAME)
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        command = ""
    except OSError:
        command = ""
    expected = autostart_command()
    return {
        "enabled": bool(command),
        "command": command or "",
        "expected": expected,
        "matches": bool(command) and command.strip() == expected.strip(),
        "value_name": RUN_VALUE_NAME,
        "key": f"HKCU\\{RUN_KEY}",
        "config_says": bool(load_config().get("autostart")),
    }


# ==========================================================================
# 单实例锁
# ==========================================================================

def _pid_alive(pid: int) -> bool:
    """pid 是不是**别的**活着的进程。

    故意把「pid == 本进程」当作不存活：锁文件里留着上次崩溃时的 pid，正好
    被回收成本进程 pid 是很常见的事，那种情况下本进程当然应该能拿锁
    （真正的单实例保护靠 :meth:`InstanceLock.acquire` 里的 holder != 自己 判断）。
    """
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class InstanceLock:
    """``autosync.lock`` 里记 pid 的单实例锁。"""

    def __init__(self, path: str | None = None):
        self.path = path or paths.lock_path()
        self.holder_pid = 0
        self.acquired = False

    def read_holder(self) -> int:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return int((fh.read() or "0").strip() or 0)
        except (OSError, ValueError):
            return 0

    def acquire(self) -> bool:
        holder = self.read_holder()
        if holder and holder != os.getpid() and _pid_alive(holder):
            self.holder_pid = holder
            return False
        paths.ensure_appdata()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
                fh.flush()
        except OSError:
            pass
        self.acquired = True
        self.holder_pid = os.getpid()
        return True

    def release(self) -> None:
        if not self.acquired:
            return
        self.acquired = False
        try:
            if self.read_holder() == os.getpid():
                os.remove(self.path)
        except OSError:
            pass


# ==========================================================================
# 轮转日志
# ==========================================================================

class RotatingLogger:
    """写 ``autosync.log``；超过 2MB 轮转一次，保留 1 个备份（``.1``）。"""

    def __init__(self, path: str | None = None, *,
                 max_bytes: int = LOG_MAX_BYTES,
                 echo: bool = False,
                 echo_func: Callable[[str], None] | None = None):
        self.path = path or paths.log_path()
        self.max_bytes = max(4096, int(max_bytes))
        self.echo = echo
        self.echo_func = echo_func or (lambda line: print(line, flush=True))
        self._lock = threading.Lock()
        self._fh = None
        paths.ensure_appdata()
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        except OSError:
            pass

    # -- 文件 -------------------------------------------------------------
    def _open(self):
        if self._fh is None:
            self._fh = open(self.path, "a", encoding="utf-8", errors="replace")
        return self._fh

    def _rotate_if_needed(self) -> None:
        try:
            if os.path.getsize(self.path) < self.max_bytes:
                return
        except OSError:
            return
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        backup = self.path + ".1"
        try:
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(self.path, backup)
        except OSError:
            pass

    def write(self, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
        with self._lock:
            try:
                self._rotate_if_needed()
                fh = self._open()
                fh.write(line + "\n")
                fh.flush()
            except OSError:
                pass
            if self.echo:
                try:
                    self.echo_func(line)
                except Exception:                          # noqa: BLE001
                    pass

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None


# ==========================================================================
# 守护进程
# ==========================================================================

def _looks_like_ip(text: str) -> bool:
    parts = text.split(".")
    if len(parts) != 4:
        return False
    return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


class AutosyncDaemon:
    """无界面同步守护：按 config 里登记的文件夹反复同步。

    每个文件夹一个线程（各自可能有不同 interval），共享一个 stop 事件。
    """

    def __init__(self, config: dict | None = None, *,
                 logger: RotatingLogger | None = None,
                 interval_override: float | None = None,
                 hidden: bool = False,
                 verbose: bool = False,
                 discover_timeout: float = 3.0,
                 once: bool = False):
        self.cfg = normalize_config(config) if config else load_config()
        self.logger = logger or RotatingLogger(
            echo=verbose, echo_func=print if verbose else None)
        self.interval_override = interval_override
        self.hidden = hidden
        self.verbose = verbose
        self.discover_timeout = discover_timeout
        self.once = once
        self.stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._disco = None
        self._disco_lock = threading.Lock()
        self._lock = InstanceLock()
        self.interrupted = False

    # -- 日志 -------------------------------------------------------------
    def log(self, msg: str) -> None:
        self.logger.write(msg)

    # -- 设备发现 ---------------------------------------------------------
    def discovery(self):
        """懒启动一个常驻发现服务（只为了拿设备表；不额外开数据端口）。"""
        with self._disco_lock:
            if self._disco is None:
                from .discovery import DiscoveryService
                try:
                    self._disco = DiscoveryService(
                        name=self.cfg.get("device_name") or None,
                        data_port=0).start()
                    self.log(f"设备发现已启动（UDP {self._disco.port}）")
                except OSError as exc:
                    self.log(f"设备发现启动失败：{exc}（本机不会出现在别人的列表里）")
                    self._disco = None
            return self._disco

    def resolve_peer(self, entry: dict) -> tuple[str, int, str] | None:
        """按 host / peer 名字解析目标；找不到返回 None。"""
        peer_name = str(entry.get("peer") or "").strip()
        host = str(entry.get("host") or "").strip()
        port = int(entry.get("port") or 45880)

        if host:
            if ":" in host and host.count(":") == 1:
                h, _, p = host.rpartition(":")
                if h and p.isdigit():
                    return h, int(p), (peer_name or host)
            return host, port, (peer_name or host)

        if not peer_name:
            return None

        disco = self.discovery()
        if disco is None:
            return None
        low = peer_name.lower()
        table = disco.peer_table()
        for p in table:                       # 先精确
            if low in (str(p["name"]).lower(), str(p["ip"]).lower(),
                       f'{p["ip"]}:{p["port"]}'.lower()):
                return p["ip"], int(p["port"]), str(p["name"])
        for p in table:                       # 再模糊
            if low in str(p["name"]).lower():
                return p["ip"], int(p["port"]), str(p["name"])
        if _looks_like_ip(peer_name):
            return peer_name, port, peer_name
        return None

    # -- 单轮 -------------------------------------------------------------
    def sync_once(self, entry: dict) -> str:
        """跑一轮同步，返回给人看的结果摘要。"""
        from .sync import SyncEngine

        local = str(entry.get("local") or "")
        os.makedirs(local, exist_ok=True)
        label = os.path.basename(local.rstrip("\\/")) or local
        target = self.resolve_peer(entry)
        if target is None:
            who = entry.get("peer") or entry.get("host") or "?"
            raise LookupError(f"没找到设备 {who!r}")
        host, port, shown = target
        two_way = str(entry.get("mode") or MODE_TWO_WAY) != MODE_ONE_WAY
        engine = SyncEngine(
            local, host, int(port),
            two_way=two_way,
            delete_extra=bool(entry.get("delete_extra")),
            name=self.cfg.get("device_name") or None,
            on_log=lambda m: self.log(f"[{label}] {m}"),
            stop_event=self.stop)
        self.log(f"[{label}] 目标 {shown} ({host}:{port})，"
                 f"开始{'双向' if two_way else '单向'}同步")
        report = engine.run_once()
        summary = report.summary()
        self.log(f"[{label}] 本轮完成: {summary}")
        return summary

    def _folder_loop(self, entry: dict) -> None:
        label = os.path.basename(str(entry.get("local") or "")) or str(entry.get("local"))
        base_interval = float(self.interval_override or entry.get("interval")
                              or DEFAULT_INTERVAL)
        step = 0
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.sync_once(entry)
                step = 0
                wait = base_interval
            except (OSError, LookupError, ValueError) as exc:
                wait = RETRY_STEPS[min(step, len(RETRY_STEPS) - 1)]
                step += 1
                self.log(f"[{label}] 本轮失败：{exc}（{wait:.0f}s 后重试）")
            except Exception as exc:                       # noqa: BLE001
                wait = RETRY_STEPS[min(step, len(RETRY_STEPS) - 1)]
                step += 1
                self.log(f"[{label}] 意外错误：{type(exc).__name__}: {exc}"
                         f"（{wait:.0f}s 后重试）")
            elapsed = time.monotonic() - started
            self._sleep(max(1.0, wait - elapsed) if wait > elapsed else 1.0)

    def _sleep(self, seconds: float) -> None:
        """可被 stop 打断的 sleep。"""
        self.stop.wait(max(0.0, float(seconds)))

    # -- 生命周期 ---------------------------------------------------------
    def _signal_handler(self, signum, _frame=None) -> None:    # noqa: ANN001
        """收到 SIGINT / SIGTERM / Ctrl+Break 时的统一处理。"""
        self.interrupted = True
        self.log(f"收到信号 {signum}，正在干净退出…")
        self.stop.set()

    def install_signal_handlers(self) -> None:
        """尽量把「关掉这个进程」的几种方式都接住。

        Windows 上控制台里按 Ctrl+C 得到 SIGINT、按 Ctrl+Break 得到
        SIGBREAK（`taskkill` 之类是 TerminateProcess，谁都接不住）；两个都
        注册，才能保证关窗口/按快捷键时把日志写完、socket 关掉、锁删掉。
        """
        import signal

        sigs = [signal.SIGINT, signal.SIGTERM]
        for name in ("SIGBREAK", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is not None and sig not in sigs:
                sigs.append(sig)
        for sig in sigs:
            try:
                signal.signal(sig, self._signal_handler)
            except (ValueError, OSError, AttributeError):
                pass

    def hide_console(self) -> bool:
        """``--hidden``：把黑窗口藏起来。pythonw 启动时本来就没有控制台。"""
        if not self.hidden:
            return False
        try:
            import ctypes

            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)
                return True
        except Exception:                                  # noqa: BLE001
            pass
        return False

    def run_once_all(self) -> int:
        """把每个文件夹各跑一轮就退出（调试/自测用，不常驻）。"""
        if not self._lock.acquire():
            self.log(f"已有一个 autosync 实例在跑（pid {self._lock.holder_pid}），"
                     f"本进程直接退出")
            self.logger.close()
            return EXIT_ALREADY_RUNNING
        self.install_signal_handlers()
        folders = self.cfg.get("folders") or []
        self.log(f"{APP_NAME} {VERSION} autosync --once 启动 pid={os.getpid()}"
                 f"（{len(folders)} 个同步文件夹）")
        try:
            for entry in folders:
                try:
                    self.sync_once(entry)
                except Exception as exc:                   # noqa: BLE001
                    self.log(f"本轮失败：{type(exc).__name__}: {exc}")
        finally:
            with self._disco_lock:
                if self._disco is not None:
                    try:
                        self._disco.stop()
                    except Exception:                      # noqa: BLE001
                        pass
                    self._disco = None
            self._lock.release()
            self.log("autosync --once 结束")
            self.logger.close()
        return 0

    def run(self) -> int:
        if self.once:
            return self.run_once_all()
        if not self._lock.acquire():
            self.log(f"已有一个 autosync 实例在跑（pid {self._lock.holder_pid}），"
                     f"本进程直接退出")
            self.logger.close()
            return EXIT_ALREADY_RUNNING

        hidden = self.hide_console()
        self.install_signal_handlers()
        folders = self.cfg.get("folders") or []
        self.log(f"{APP_NAME} {VERSION} autosync 启动 pid={os.getpid()}"
                 f"（设备名 {self.cfg.get('device_name')}，"
                 f"{len(folders)} 个同步文件夹"
                 + ("，已隐藏控制台" if hidden else "") + "）")
        if not folders:
            self.log("配置里还没有登记任何同步文件夹"
                     "（用 `folders add` 或 GUI 勾选「开机自动同步」），"
                     "继续待命，每 30s 重新读一次配置")
        try:
            for entry in folders:
                t = threading.Thread(target=self._folder_loop, args=(entry,),
                                     name=f"autosync-{os.path.basename(str(entry.get('local')))}",
                                     daemon=True)
                t.start()
                self._threads.append(t)

            if folders:
                self.discovery()
                # 主线程只负责等待退出信号；没有文件夹时定期重读配置
                while not self.stop.is_set():
                    self._sleep(1.0)
            else:
                while not self.stop.is_set():
                    self._sleep(5.0)
                    cfg = load_config()
                    new = cfg.get("folders") or []
                    if new:
                        self.log(f"检测到新登记 {len(new)} 个同步文件夹，开始工作")
                        self.cfg = cfg
                        for entry in new:
                            t = threading.Thread(target=self._folder_loop,
                                                 args=(entry,), daemon=True)
                            t.start()
                            self._threads.append(t)
                        self.discovery()
                        break
        except KeyboardInterrupt:
            self.interrupted = True
            self.log("收到 Ctrl+C，正在干净退出…")
        except Exception as exc:                           # noqa: BLE001
            self.log(f"守护进程异常退出（已记录，不会静默）："
                     f"{type(exc).__name__}: {exc}")
        finally:
            self.stop.set()
            for t in self._threads:
                t.join(timeout=5.0)
            with self._disco_lock:
                if self._disco is not None:
                    try:
                        self._disco.stop()
                        self.log("设备发现 socket 已关闭")
                    except Exception:                      # noqa: BLE001
                        pass
                    self._disco = None
            self._lock.release()
            self.log("autosync 已退出，日志与锁都已落盘")
            self.logger.close()
        return 0


def run_daemon(argv: list[str] | None = None) -> int:
    """``autosync`` 子命令入口（cli.py 调用）。"""
    import argparse

    parser = argparse.ArgumentParser(
        prog="swiftdrop autosync",
        description=f"{APP_NAME} —— 开机自启的无界面同步守护进程")
    parser.add_argument("--hidden", action="store_true",
                        help="隐藏控制台黑窗口（开机自启用）")
    parser.add_argument("--verbose", action="store_true",
                        help="同时把日志打到控制台")
    parser.add_argument("--interval", type=float, default=None,
                        help="覆盖配置里的轮询秒数（调试用）")
    parser.add_argument("--discover-timeout", type=float, default=3.0,
                        help="设备名解析的超时秒数")
    parser.add_argument("--once", action="store_true",
                        help="每个文件夹只跑一轮就退出（调试/自测用）")
    parser.add_argument("--status", action="store_true",
                        help="只打印配置与自启状态，然后退出")
    args = parser.parse_args(argv)

    if args.status:
        cfg = load_config()
        print(f"{APP_NAME} {VERSION} autosync 状态")
        print(f"配置文件 : {paths.config_path()}")
        print(f"日志文件 : {paths.log_path()}")
        print(f"设备名   : {cfg.get('device_name')}")
        folders = cfg.get("folders") or []
        print(f"同步文件夹: {len(folders)} 个")
        for entry in folders:
            print(f"  - {entry['local']} → {entry.get('peer') or entry.get('host')}"
                  f" ({entry.get('mode')}, 每 {entry.get('interval')}s)")
        st = autostart_status()
        print(f"开机自启 : {'已开启' if st['enabled'] else '未开启'}")
        if st["enabled"]:
            print(f"  命令  : {st['command']}")
        return 0

    daemon = AutosyncDaemon(interval_override=args.interval,
                            hidden=args.hidden, verbose=args.verbose,
                            discover_timeout=args.discover_timeout,
                            once=args.once)
    return daemon.run()
