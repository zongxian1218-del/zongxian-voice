"""棕仙语音 媒体引擎的 Python 绑定（ctypes）。

定位说明（重要，改这个文件前先读）
--------------------------------------------------------------------------
这一层是**很薄的转换层**，只做三件事：

  1. 加载 ``zongxian_media.dll`` 并声明函数签名
  2. 把 Python 的 str/dict 转成 C 侧要的 UTF-8 JSON 与缓冲区
  3. 把 C 侧返回的 JSON 与样本缓冲转回 Python 对象

它**不包含**任何音频处理逻辑，也**不应该**出现按样本的 Python 循环。
理由很实在：48kHz 双声道每秒 9.6 万个样本，Python 层的循环一旦进入热路径，
GIL 带来的抖动会直接毁掉回声消除（AEC 要求采集与播放采样级对齐）。
所有重活都在 C++ 引擎里，这里只负责搬运。

对应关系：``include/zongxian_media.h`` 是唯一权威的接口定义。
这个文件里的函数名与那个头文件一一对应，头文件改了这里必须跟着改。

设计取舍
--------------------------------------------------------------------------
* **只用标准库**（ctypes / json / os）—— 与现有 ``src/swiftdrop`` 保持一致，
  桌面版不引入任何 pip 依赖。
* **句柄用上下文管理器**：``with Engine() as eng, eng.open_source(...) as src:``
  这样即使中途抛异常，也不会漏掉 ``*_close`` 而导致设备被占住。
* **读音频返回 memoryview 切片**，不是新数组：调用方通常在后面紧接着
  喂给编码器或做统计，多一次拷贝在实时路径上是浪费。
* **错误一律抛 MediaError**，消息取自 ``zx_last_error()``。绝不返回错误码
  让调用方自己判断 —— 那种写法在实践中一定会漏判。
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "MediaError",
    "ABI_VERSION",
    "SAMPLE_RATE",
    "BLOCK_FRAMES",
    "SourceKind",
    "NsLevel",
    "NsBackend",
    "Engine",
    "Source",
    "load_library",
]

# ---------------------------------------------------------------------------
# 常量（必须与 zongxian_media.h 保持一致）
# ---------------------------------------------------------------------------

ABI_VERSION = 1
SAMPLE_RATE = 48000
BLOCK_FRAMES = 480            # 10ms
SAMPLE_BYTES = 4              # float32


class SourceKind:
    """采集源类型。数值与 C 侧的 ``zx_source_kind`` 对应。"""

    MIC = 0          # 麦克风
    LOOPBACK = 1     # 全部应用音频
    PROCESS = 2      # 单个应用音频（进程回环，需要 Win10 20348+ / 实测 2004+）

    _LABELS = {0: "麦克风", 1: "全部应用音频", 2: "指定应用音频"}

    @classmethod
    def label(cls, value: int) -> str:
        return cls._LABELS.get(value, f"未知({value})")


class NsLevel:
    """降噪档位，对应界面上的「关 / 轻 / 中 / 强」。"""

    OFF = 0
    LIGHT = 1
    MODERATE = 2
    STRONG = 3

    _LABELS = {0: "关", 1: "轻", 2: "中", 3: "强"}

    @classmethod
    def label(cls, value: int) -> str:
        return cls._LABELS.get(value, f"未知({value})")


class NsBackend:
    """降噪实现。BUILTIN 是引擎自带的谱减，RNNOISE 需要构建时带进来。"""

    BUILTIN = 0
    RNNOISE = 1


# 结果码（与 zx_result 对应）
_ZX_OK = 0
_ZX_ERR_TIMEOUT = -8
_ZX_ERR_NO_DATA = -9


class MediaError(RuntimeError):
    """引擎返回的错误。消息已经是从 ``zx_last_error()`` 取到的人话说明。"""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# 加载动态库
# ---------------------------------------------------------------------------

# 候选路径。顺序体现优先级：构建产物优先于已安装的产物，
# 这样开发时改了 C++ 立刻生效，不会误加载旧版本。
_SEARCH_RELATIVE = (
    Path("build") / "media" / "bin" / "zongxian_media.dll",
    Path("build") / "media" / "zongxian_media.dll",
    Path("dist") / "zongxian_media.dll",
    Path("src") / "media" / "build" / "bin" / "zongxian_media.dll",
)


def _candidate_paths() -> Iterable[Path]:
    roots: list[Path] = []
    env = os.environ.get("ZX_MEDIA_BUILD")
    if env:
        roots.append(Path(env))
    roots.append(Path(__file__).resolve().parents[2])   # 仓库根目录
    for root in roots:
        for rel in _SEARCH_RELATIVE:
            yield root / rel


_library: ctypes.CDLL | None = None


def load_library(path: str | os.PathLike[str] | None = None) -> ctypes.CDLL:
    """加载并配置引擎动态库。重复调用返回同一个句柄。

    显式传 path 可以指定任意位置；否则按 ``_SEARCH_RELATIVE`` 顺序查找，
    并支持 ``ZX_MEDIA_BUILD`` 环境变量覆盖根目录。
    """
    global _library
    if _library is not None and path is None:
        return _library

    if path is not None:
        lib_path = Path(path)
        if not lib_path.is_file():
            raise MediaError(f"找不到媒体引擎库：{lib_path}")
    else:
        lib_path = next((p for p in _candidate_paths() if p.is_file()), None)
        if lib_path is None:
            tried = "\n".join(f"  - {p}" for p in _candidate_paths())
            raise MediaError(
                "找不到 zongxian_media.dll。先构建引擎：\n"
                "  cmake -S src/media -B build/media -G Ninja\n"
                "  cmake --build build/media\n"
                f"已尝试的路径：\n{tried}"
            )

    if sys.platform == "win32":
        # Python 3.8+ 在 Windows 上默认不搜索 PATH 与 DLL 所在目录，
        # 必须显式加进来，否则引擎依赖的系统库可能加载失败。
        os.add_dll_directory(str(lib_path.parent))

    lib = ctypes.CDLL(str(lib_path))
    _configure(lib)

    abi = lib.zx_media_abi_version()
    if abi != ABI_VERSION:
        raise MediaError(
            f"引擎 ABI 版本不匹配：库是 {abi}，Python 绑定期望 {ABI_VERSION}。"
            "请重新构建引擎（两边必须一起更新）。"
        )
    _library = lib
    return lib


def _configure(lib: ctypes.CDLL) -> None:
    """声明所有函数的签名。

    这一步不能省：ctypes 默认把返回值当 int、参数当 int，
    指针会被截断成 32 位，在 64 位进程里表现为随机的访问冲突 ——
    而且报错位置离真正的原因很远，非常难查。
    """
    c_void_p = ctypes.c_void_p
    c_char_p = ctypes.c_char_p
    c_int = ctypes.c_int
    c_float = ctypes.c_float

    # --- 生命周期 ---
    lib.zx_media_init.argtypes = [c_char_p]
    lib.zx_media_init.restype = c_int
    lib.zx_media_shutdown.argtypes = []
    lib.zx_media_shutdown.restype = None
    lib.zx_media_abi_version.argtypes = []
    lib.zx_media_abi_version.restype = c_int
    lib.zx_media_version.argtypes = []
    lib.zx_media_version.restype = c_char_p

    # --- 诊断 ---
    lib.zx_last_error.argtypes = []
    lib.zx_last_error.restype = c_char_p
    lib.zx_media_dump_log.argtypes = [c_char_p]
    lib.zx_media_dump_log.restype = c_int

    # --- 设备枚举与能力探测 ---
    for name in (
        "zx_list_capture_devices",
        "zx_list_render_devices",
        "zx_list_audio_processes",
    ):
        fn = getattr(lib, name)
        fn.argtypes = [c_char_p, c_int]
        fn.restype = c_int
    lib.zx_probe_process_loopback.argtypes = [
        ctypes.POINTER(c_int), c_char_p, c_int
    ]
    lib.zx_probe_process_loopback.restype = c_int

    # --- 引擎对象 ---
    lib.zx_engine_create.argtypes = [ctypes.POINTER(c_void_p)]
    lib.zx_engine_create.restype = c_int
    lib.zx_engine_destroy.argtypes = [c_void_p]
    lib.zx_engine_destroy.restype = None
    lib.zx_engine_set_master_volume.argtypes = [c_void_p, c_float]
    lib.zx_engine_set_master_volume.restype = c_int

    # --- 采集源 ---
    lib.zx_source_open.argtypes = [c_char_p, c_int, ctypes.POINTER(c_void_p)]
    lib.zx_source_open.restype = c_int
    lib.zx_source_start.argtypes = [c_void_p]
    lib.zx_source_start.restype = c_int
    lib.zx_source_stop.argtypes = [c_void_p]
    lib.zx_source_stop.restype = c_int
    lib.zx_source_close.argtypes = [c_void_p]
    lib.zx_source_close.restype = None
    lib.zx_source_format.argtypes = [c_void_p, c_char_p, c_int]
    lib.zx_source_format.restype = c_int
    lib.zx_source_read.argtypes = [
        c_void_p,
        ctypes.POINTER(c_float),
        c_int,
        ctypes.POINTER(c_int),
        c_int,
    ]
    lib.zx_source_read.restype = c_int
    lib.zx_source_meter.argtypes = [c_void_p, c_char_p, c_int]
    lib.zx_source_meter.restype = c_int
    lib.zx_source_stats.argtypes = [c_void_p, c_char_p, c_int]
    lib.zx_source_stats.restype = c_int
    lib.zx_source_clear_callback.argtypes = [c_void_p]
    lib.zx_source_clear_callback.restype = c_int
    # zx_source_set_callback 刻意不暴露给 Python：
    # 在 ctypes 回调里进解释器会踩 GIL，是实时音频线程上爆音的经典原因。
    # 需要事件时用 zx_*_poll_event 主动拉取（见头文件里的说明）。

    # --- 降噪 ---
    lib.zx_ns_set.argtypes = [c_void_p, c_int, c_int]
    lib.zx_ns_set.restype = c_int
    lib.zx_ns_get_config.argtypes = [c_void_p, c_char_p, c_int]
    lib.zx_ns_get_config.restype = c_int
    lib.zx_ns_set_bypass.argtypes = [c_void_p, c_int]
    lib.zx_ns_set_bypass.restype = c_int
    lib.zx_ns_set_modules.argtypes = [c_void_p, c_int, c_int, c_int]
    lib.zx_ns_set_modules.restype = c_int

    # --- 录音 ---
    lib.zx_recorder_open.argtypes = [c_void_p, c_char_p, c_char_p]
    lib.zx_recorder_open.restype = c_int
    lib.zx_recorder_close.argtypes = [c_void_p]
    lib.zx_recorder_close.restype = c_int
    lib.zx_recorder_stats.argtypes = [c_void_p, c_char_p, c_int]
    lib.zx_recorder_stats.restype = c_int

    # --- 会话（PL1 起才有内容）---
    lib.zx_session_create.argtypes = [c_void_p, c_char_p, ctypes.POINTER(c_void_p)]
    lib.zx_session_create.restype = c_int
    lib.zx_session_destroy.argtypes = [c_void_p]
    lib.zx_session_destroy.restype = None
    lib.zx_session_poll_event.argtypes = [c_void_p, c_char_p, c_int]
    lib.zx_session_poll_event.restype = c_int
    lib.zx_session_get_state.argtypes = [c_void_p, c_char_p, c_int]
    lib.zx_session_get_state.restype = c_int
    lib.zx_session_send_message.argtypes = [c_void_p, c_char_p]
    lib.zx_session_send_message.restype = c_int


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _encode(obj: Any) -> bytes:
    """把配置对象编成 UTF-8 JSON。

    ``ensure_ascii=False`` 是刻意的：引擎内部所有字符串都用 UTF-8，
    保持中文直出可以让日志、错误信息、抓包内容都能直接读懂。
    """
    if obj is None:
        return b""
    if isinstance(obj, bytes):
        return obj
    if isinstance(obj, str):
        return obj.encode("utf-8")
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _check(code: int, what: str, lib: ctypes.CDLL) -> None:
    """结果码 → 异常。消息优先取引擎给出的人话说明。"""
    if code == _ZX_OK:
        return
    raw = lib.zx_last_error()
    detail = raw.decode("utf-8", "replace") if raw else ""
    raise MediaError(detail or f"{what}失败（错误码 {code}）", code=code)


def _read_json(lib: ctypes.CDLL, fn, handle, what: str, hint: int = 4096) -> Any:
    """调用一个「先给容量、不够再放大」的 JSON 输出函数。

    引擎的约定是：返回所需长度（含结尾 NUL）。所以先给一个合理大小，
    不够就按返回的长度重试一次 —— 不猜、不循环放大。
    """
    size = hint
    for _ in range(3):
        buf = ctypes.create_string_buffer(size)
        need = fn(handle, buf, size)
        if need < 0:
            _check(need, what, lib)
        if need <= size:
            text = buf.value.decode("utf-8", "replace")
            return json.loads(text) if text.strip() else None
        size = need + 16
    raise MediaError(f"{what}：输出过长，连续三次都没能取到完整结果")


# ---------------------------------------------------------------------------
# 采集源
# ---------------------------------------------------------------------------


@dataclass
class SourceFormat:
    """源实际生效的格式。

    注意：这**不一定**等于你请求的格式 —— WASAPI 共享模式下混音格式由设备
    决定。需要精确控制时必须以这里的值为准，不要假设 48kHz/单声道。
    """

    sample_rate: int = SAMPLE_RATE
    channels: int = 1
    bits_per_sample: int = 32
    is_float: bool = True
    block_frames: int = BLOCK_FRAMES
    period_ms: float = 10.0


class Source:
    """一个采集源。

    生命周期：``Engine.open_source`` 创建 → ``start`` → 反复 ``read`` →
    ``stop`` → ``close``。用 ``with`` 语句可以自动处理最后两步。
    """

    def __init__(self, engine: "Engine", handle: int, kind: int, request: dict):
        self._engine = engine
        self._lib = engine.lib
        self._handle = handle
        self.kind = kind
        self.request = request
        self._closed = False
        self._started = False
        self._format: SourceFormat | None = None
        # 读缓冲复用一个 ctypes 数组：每 10ms 就要读一次，
        # 每次重新分配会让 GC 压力和内存局部性都变差。
        self._read_capacity = max(int(request.get("blockFrames", BLOCK_FRAMES)), 64)
        self._read_buf = (ctypes.c_float * (self._read_capacity * 8))()
        self._frames_out = ctypes.c_int(0)

    # -- 属性 --

    @property
    def format(self) -> SourceFormat:
        if self._format is None:
            data = _read_json(self._lib, self._lib.zx_source_format, self._handle,
                              "查询源格式")
            self._format = SourceFormat(**(data or {}))
        return self._format

    @property
    def label(self) -> str:
        return SourceKind.label(self.kind)

    # -- 上下文管理 --

    def __enter__(self) -> "Source":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 操作 --

    def start(self) -> None:
        if self._closed:
            raise MediaError("源已关闭")
        _check(self._lib.zx_source_start(self._handle), "启动采集", self._lib)
        self._started = True
        # 启动后重新取格式：后端可能在上报时有变化（例如进程回环的固定格式）
        self._format = None

    def stop(self) -> None:
        if self._closed or not self._started:
            return
        _check(self._lib.zx_source_stop(self._handle), "停止采集", self._lib)
        self._started = False

    def close(self) -> None:
        """关闭并释放设备。幂等 —— 重复调用不会出错。"""
        if self._closed:
            return
        self._closed = True
        self._lib.zx_source_close(self._handle)
        self._handle = None

    # -- 数据 --

    def read(self, frames: int = BLOCK_FRAMES, timeout_ms: int = 200) -> memoryview | None:
        """读取最多 ``frames`` 帧交错 float32 数据。

        返回 ``memoryview`` 切片（不是新数组，避免实时路径上的额外拷贝）；
        超时没有数据时返回 ``None`` —— **超时不是错误**，调用方应该继续轮询。

        用法::

            buf = src.read()
            if buf is not None:
                samples = np.frombuffer(buf, dtype=np.float32)
        """
        if self._closed or not self._started:
            raise MediaError("源未启动或已关闭")
        capacity = self._read_capacity * 8
        if frames > capacity:
            raise MediaError(f"一次最多读 {capacity} 帧（缓冲上限）")

        code = self._lib.zx_source_read(
            self._handle, self._read_buf, frames,
            ctypes.byref(self._frames_out), timeout_ms,
        )
        if code == _ZX_ERR_TIMEOUT:
            return None
        if code != _ZX_OK:
            _check(code, "读取音频", self._lib)

        got = self._frames_out.value
        if got <= 0:
            return None
        total = got * self.format.channels
        return memoryview(self._read_buf).cast("B")[: total * SAMPLE_BYTES].cast("f")

    def read_into(self, out: Any, timeout_ms: int = 200) -> int:
        """把数据读到调用方提供的 numpy 数组里，返回实际帧数。

        这是给实时路径用的版本：零分配、零拷贝。``out`` 必须是
        ``numpy.float32`` 且 C 连续的一维数组。
        """
        if self._closed or not self._started:
            raise MediaError("源未启动或已关闭")
        if not out.flags["C_CONTIGUOUS"]:
            raise MediaError("输出数组必须是 C 连续的")
        if out.dtype.name != "float32":
            raise MediaError("输出数组必须是 float32")

        channels = self.format.channels
        frames = out.size // channels
        if frames <= 0:
            raise MediaError("输出数组太小")

        ptr = out.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        code = self._lib.zx_source_read(
            self._handle, ptr, frames, ctypes.byref(self._frames_out), timeout_ms
        )
        if code == _ZX_ERR_TIMEOUT:
            return 0
        if code != _ZX_OK:
            _check(code, "读取音频", self._lib)
        return self._frames_out.value

    def meter(self) -> dict:
        """实时电平。可以随时调用，不会阻塞、也不影响数据流。"""
        return _read_json(self._lib, self._lib.zx_source_meter, self._handle,
                          "读取电平", hint=512) or {}

    def stats(self) -> dict:
        """采集统计。

        重点看两个数：``avgPeriodMs``（应≈10.0）与 ``discontinuities``（应为 0）。
        这两个数字是「采集周期稳不稳」的直接证据，比主观听感可靠。
        """
        return _read_json(self._lib, self._lib.zx_source_stats, self._handle,
                          "读取统计", hint=1024) or {}

    # -- 降噪 --

    def set_denoise(self, level: int = NsLevel.MODERATE,
                    backend: int = NsBackend.BUILTIN) -> None:
        _check(self._lib.zx_ns_set(self._handle, level, backend), "设置降噪", self._lib)

    def denoise_config(self) -> dict:
        """降噪的实际生效状态。

        ``degraded=True`` 表示请求的后端不可用已降级（例如没编进 RNNoise）。
        ``suppressionDb`` 是噪声段被压掉的 dB 数 —— 用来客观证明降噪有效，
        而不是只靠「听着好像小了点」。
        """
        return _read_json(self._lib, self._lib.zx_ns_get_config, self._handle,
                          "读取降噪状态", hint=1024) or {}

    def set_bypass(self, on: bool) -> None:
        """旁路：跳过降噪处理，但仍然统计噪声底（用于 A/B 对照）。"""
        _check(self._lib.zx_ns_set_bypass(self._handle, 1 if on else 0),
               "设置降噪旁路", self._lib)

    def set_modules(self, aec: int = -1, agc: int = -1, vad: int = -1) -> None:
        """逐模块开关。

        AEC / AGC 属于 PL1（语音通话），当前会抛 ``MediaError``。
        刻意明确报错而不是静默忽略 —— 静默忽略会让调用方以为已经生效。
        """
        code = self._lib.zx_ns_set_modules(self._handle, aec, agc, vad)
        if code != _ZX_OK:
            raw = self._lib.zx_last_error()
            detail = raw.decode("utf-8", "replace") if raw else ""
            raise MediaError(detail or "设置处理模块失败", code=code)

    # -- 录音 --

    def start_recording(self, raw_wav: str | None = None,
                        processed_wav: str | None = None) -> None:
        """同时开两路录音：降噪前与降噪后。

        两份文件严格同源同时刻，这样才能做有意义的 A/B 对比 ——
        分两次录会因为环境噪声不同而误判降噪效果。
        """
        _check(
            self._lib.zx_recorder_open(
                self._handle,
                _encode(raw_wav) if raw_wav else None,
                _encode(processed_wav) if processed_wav else None,
            ),
            "开始录音", self._lib,
        )

    def stop_recording(self) -> dict:
        _check(self._lib.zx_recorder_close(self._handle), "结束录音", self._lib)
        return self.recording_stats()

    def recording_stats(self) -> dict:
        return _read_json(self._lib, self._lib.zx_recorder_stats, self._handle,
                          "读取录音统计", hint=1024) or {}


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class Engine:
    """引擎句柄。管理 COM 与全局配置。

    整个进程只需要一个。用 ``with`` 语句保证 ``shutdown`` 一定被调用 ——
    漏掉它会让采集线程在 COM 反初始化之后才退出，表现为进程退出时卡住。
    """

    def __init__(self, config: dict | None = None, library: str | None = None):
        self.lib = load_library(library)
        self._config = config or {}
        self._handle = ctypes.c_void_p()
        self._sources: list[Source] = []
        self._closed = False

        _check(self.lib.zx_media_init(_encode(self._config)), "初始化引擎", self.lib)
        _check(self.lib.zx_engine_create(ctypes.byref(self._handle)),
               "创建引擎对象", self.lib)

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def version(self) -> str:
        raw = self.lib.zx_media_version()
        return raw.decode("utf-8", "replace") if raw else ""

    def close(self) -> None:
        """关闭引擎。幂等。会先关掉所有仍打开的音源。"""
        if self._closed:
            return
        self._closed = True
        for src in list(self._sources):
            try:
                src.close()
            except Exception:      # 关闭路径上不该因为单个源出错而中断整体清理
                pass
        self._sources.clear()
        if self._handle:
            self.lib.zx_engine_destroy(self._handle)
            self._handle = None
        self.lib.zx_media_shutdown()

    # -- 全局设置 --

    def set_master_volume(self, volume: float) -> None:
        if not 0.0 <= volume <= 1.0:
            raise MediaError("音量必须在 0.0 ~ 1.0 之间")
        _check(self.lib.zx_engine_set_master_volume(self._handle, volume),
               "设置总音量", self.lib)

    def dump_log(self, path: str) -> None:
        """把引擎的日志环形缓冲写出来。

        排障的第一件事：让用户点一下这个，比来回猜快得多。
        """
        _check(self.lib.zx_media_dump_log(_encode(path)), "导出日志", self.lib)

    # -- 设备与探测 --

    def capture_devices(self) -> list[dict]:
        return self._list(self.lib.zx_list_capture_devices, "枚举采集设备")

    def render_devices(self) -> list[dict]:
        return self._list(self.lib.zx_list_render_devices, "枚举播放设备")

    def audio_processes(self) -> list[dict]:
        """有音频会话的进程（``active`` 为真表示此刻正在发声）。

        这是「只共享某个应用的声音」在选择界面上能列出候选的前提。
        """
        return self._list(self.lib.zx_list_audio_processes, "枚举音频进程")

    def _list(self, fn, what: str, hint: int = 16384) -> list[dict]:
        # 这里传的是「无句柄」的枚举函数，签名上第一个参数是输出缓冲，
        # 所以不能复用 _read_json（它会多传一个 handle）。手动展开一次。
        size = hint
        for _ in range(3):
            buf = ctypes.create_string_buffer(size)
            need = fn(buf, size)
            if need < 0:
                _check(need, what, self.lib)
            if need <= size:
                text = buf.value.decode("utf-8", "replace")
                return json.loads(text) if text.strip() else []
            size = need + 16
        raise MediaError(f"{what}：结果过长")

    def probe_process_loopback(self) -> tuple[bool, str]:
        """探测本机是否支持「单个应用音频」。

        这是**真实探测**（试调一次系统接口），不是读系统版本号。
        微软文档写最低版本是 Win10 Build 20348，但实测 2004+ 常可用，
        按版本号判断会冤枉一大批能用的机器。

        返回 ``(是否支持, 人话说明)``。
        """
        supported = ctypes.c_int(0)
        detail = ctypes.create_string_buffer(512)
        _check(
            self.lib.zx_probe_process_loopback(ctypes.byref(supported), detail,
                                               len(detail)),
            "探测进程回环", self.lib,
        )
        return bool(supported.value), detail.value.decode("utf-8", "replace")

    # -- 音源 --

    def open_source(self, kind: int = SourceKind.MIC, *, device_id: str | None = None,
                    process_id: int | None = None, include_children: bool = True,
                    channels: int | None = None,
                    block_frames: int = BLOCK_FRAMES) -> Source:
        """打开一个采集源（未启动）。

        :param kind: ``SourceKind.MIC`` / ``LOOPBACK`` / ``PROCESS``
        :param device_id: 不透明设备标识；省略表示系统默认设备
        :param process_id: ``kind=PROCESS`` 时必填
        :param include_children: 进程回环是否包含子进程。

            **强烈建议保持 True**：Chrome/Electron/大多数游戏把音频渲染放在
            子进程里，只抓主进程会得到断断续续的声音，而且这个现象很难归因。
        :param channels: 目标声道数；省略则麦克风 1、共享音频 2
        """
        request: dict[str, Any] = {
            "includeChildProcesses": include_children,
            "blockFrames": block_frames,
        }
        if device_id:
            request["deviceId"] = device_id
        if process_id is not None:
            request["processId"] = int(process_id)
        if channels is not None:
            request["wantChannels"] = int(channels)

        handle = ctypes.c_void_p()
        _check(
            self.lib.zx_source_open(_encode(request), int(kind), ctypes.byref(handle)),
            "打开采集源", self.lib,
        )
        src = Source(self, handle, int(kind), request)
        self._sources.append(src)
        return src
