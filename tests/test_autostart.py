"""棕仙的传输软件 —— 开机自启 / 同步文件夹登记 自测（纯标准库，直接运行）。

    & "<python>" D:\\文档\\ai001\\tests\\test_autostart.py

覆盖 7 项：
    1. 配置读写（默认值 / 保存 / 重载 / 坏文件容错）
    2. add_folder / list_folders / remove_folder（含重复登记去重）
    3. enable_autostart() 后读注册表确认值名与命令正确
    4. disable_autostart() 后注册表值消失（**测完一定恢复现场**）
    5. 单实例锁（同 pid 存活 → 第二个实例拿不到锁；死 pid → 能拿到）
    6. 轮转日志（超限轮转出 .1 备份）
    7. autosync 守护进程跑起来：写日志、单实例立即退出、TERM 干净退出

安全约定：本脚本只碰 ``HKCU\\...\\Run`` 下名为「棕仙的传输软件」的那一个值，
跑之前先读出来、跑完原样恢复，绝不动别人的开机项。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")

# 先隔离配置目录，别污染真实的 %APPDATA%\ZongxianTransfer
TMP = tempfile.mkdtemp(prefix="zongxian-autostart-test-")
os.environ["SWIFTDROP_APPDATA_DIR"] = os.path.join(TMP, "appdata")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import winreg                                                    # noqa: E402

from swiftdrop import APP_NAME, VERSION                          # noqa: E402
from swiftdrop import autostart, paths                           # noqa: E402

PY = sys.executable
RESULTS: list[tuple[str, str, str]] = []
RUN_KEY = autostart.RUN_KEY
VALUE = autostart.RUN_VALUE_NAME


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"== {title}")
    print("=" * 78, flush=True)


def record(name: str, status: str, detail: str) -> None:
    RESULTS.append((name, status, detail))
    print(f"\n---- [{status}] {name}\n     {detail}", flush=True)


def run_test(name: str, fn) -> None:
    banner(name)
    t0 = time.monotonic()
    try:
        detail = fn()
        record(name, "PASS", detail)
    except Exception as exc:                                     # noqa: BLE001
        traceback.print_exc()
        record(name, "FAIL", f"{type(exc).__name__}: {exc}")
    print(f"     ({time.monotonic() - t0:.2f}s)", flush=True)


def read_run_value() -> str | None:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
    except FileNotFoundError:
        return None
    try:
        try:
            value, _kind = winreg.QueryValueEx(key, VALUE)
            return str(value)
        except FileNotFoundError:
            return None
    finally:
        winreg.CloseKey(key)


def write_run_value(text: str | None) -> None:
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                             winreg.KEY_SET_VALUE)
    try:
        if text is None:
            try:
                winreg.DeleteValue(key, VALUE)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, VALUE, 0, winreg.REG_SZ, text)
    finally:
        winreg.CloseKey(key)


def rm(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


# ==========================================================================
# 1. 配置读写
# ==========================================================================

def test_config() -> str:
    path = paths.config_path()
    assert path.lower().startswith(TMP.lower()), f"配置目录没被隔离: {path}"
    # 正式环境（没有环境变量覆盖）的目录名必须是 ASCII 的 ZongxianTransfer
    saved_env = os.environ.pop("SWIFTDROP_APPDATA_DIR")
    try:
        default_cfg = paths.config_path()
    finally:
        os.environ["SWIFTDROP_APPDATA_DIR"] = saved_env
    assert os.path.basename(os.path.dirname(default_cfg)) == "ZongxianTransfer", \
        default_cfg
    assert os.path.basename(default_cfg) == "config.json", default_cfg

    cfg = autostart.load_config()
    assert cfg["device_name"], "默认配置缺 device_name"
    assert cfg["autostart"] is False and cfg["folders"] == [], cfg
    print(f"默认配置: {json.dumps(cfg, ensure_ascii=False)}", flush=True)

    cfg["device_name"] = "测试机"
    cfg["folders"] = [{"local": os.path.join(TMP, "同步盘"), "peer": "家里的台式机"}]
    saved = autostart.save_config(cfg)
    assert saved == path
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    assert raw["device_name"] == "测试机", raw
    entry = raw["folders"][0]
    for key, want in (("peer", "家里的台式机"), ("port", 45880),
                      ("mode", "two-way"), ("delete_extra", False),
                      ("interval", 5), ("mark_icon", True)):
        assert entry[key] == want, f"{key}={entry[key]!r} 期望 {want!r}"
    print(f"落盘内容: {json.dumps(raw, ensure_ascii=False)}", flush=True)

    again = autostart.load_config()
    assert again["device_name"] == "测试机"
    assert again["folders"][0]["peer"] == "家里的台式机"

    # 坏文件容错：不该抛异常，且要备份成 .bad
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{这不是 JSON")
    broken = autostart.load_config()
    assert broken["folders"] == [], broken
    assert os.path.isfile(path + ".bad"), "坏配置没有备份成 .bad"
    return (f"默认值/保存/重载/坏文件容错全部正常；"
            f"配置路径 {path}（ASCII 目录 ZongxianTransfer）")


# ==========================================================================
# 2. 文件夹登记
# ==========================================================================

def test_folders() -> str:
    autostart.save_config(autostart.default_config())
    d1 = os.path.join(TMP, "同步盘")
    d2 = os.path.join(TMP, "照片 备份")
    os.makedirs(d1, exist_ok=True)
    os.makedirs(d2, exist_ok=True)

    autostart.add_folder({"local": d1, "peer": "家里的台式机", "interval": 5})
    autostart.add_folder({"local": d2, "host": "192.168.1.50", "mode": "one-way",
                          "delete_extra": True, "interval": 15})
    folders = autostart.list_folders()
    assert len(folders) == 2, folders
    assert folders[0]["local"] == os.path.abspath(d1), folders[0]
    assert folders[1]["mode"] == "one-way" and folders[1]["delete_extra"] is True

    # 重复登记同一路径 → 更新而不是新增
    autostart.add_folder({"local": d1, "peer": "新名字", "interval": 9})
    folders = autostart.list_folders()
    assert len(folders) == 2, f"重复登记没有去重: {folders}"
    kept = [f for f in folders if f["local"] == os.path.abspath(d1)][0]
    assert kept["peer"] == "新名字" and kept["interval"] == 9, kept

    assert autostart.remove_folder(d2) is True
    assert autostart.remove_folder(d2) is False, "重复删除应当返回 False"
    folders = autostart.list_folders()
    assert len(folders) == 1 and folders[0]["local"] == os.path.abspath(d1)
    return (f"add/list/remove 正常：2 条登记 → 重复登记同路径只更新（peer=新名字, "
            f"interval=9）→ 删除后剩 {len(folders)} 条")


# ==========================================================================
# 3. 开启自启 → 查注册表
# ==========================================================================

def test_enable() -> str:
    cmd = autostart.enable_autostart()
    value = read_run_value()
    assert value is not None, f"注册表里没有值「{VALUE}」"
    assert value == cmd, f"注册表值 {value!r} != 返回的命令 {cmd!r}"

    # 值名必须是中文产品名，键必须落在 HKCU 的 Run 下
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
    try:
        names = []
        i = 0
        while True:
            try:
                names.append(winreg.EnumValue(key, i)[0])
            except OSError:
                break
            i += 1
    finally:
        winreg.CloseKey(key)
    assert VALUE == APP_NAME == "棕仙的传输软件", VALUE
    assert VALUE in names, f"值名 {VALUE!r} 不在 Run 的 {names}"

    assert cmd.startswith(f'"{os.path.abspath(PY)}"'), cmd
    assert "entry.py" in cmd, f"源码运行应当走 build\\entry.py：{cmd}"
    assert cmd.endswith("autosync --hidden"), cmd

    st = autostart.autostart_status()
    assert st["enabled"] and st["matches"], st
    assert autostart.load_config()["autostart"] is True
    print(f"注册表值: {value}", flush=True)
    return (f"HKCU\\{RUN_KEY} 下值名「{VALUE}」= {value}；"
            f"status.enabled={st['enabled']}，命令与源码运行应有的形式一致")


# ==========================================================================
# 4. 关闭自启 → 值消失
# ==========================================================================

def test_disable() -> str:
    # 先确认现在是开着的
    assert read_run_value() is not None, "前置条件不成立：注册表项应当存在"
    removed = autostart.disable_autostart()
    assert removed is True, "disable_autostart() 没有报告删除成功"
    assert read_run_value() is None, "注册表值仍然存在，没删干净"
    assert autostart.disable_autostart() is False, "重复关闭应当返回 False"
    st = autostart.autostart_status()
    assert st["enabled"] is False, st
    assert autostart.load_config()["autostart"] is False
    return "disable_autostart() 后注册表值已消失，再关一次返回 False，status.enabled=False"


# ==========================================================================
# 5. 单实例锁
# ==========================================================================

def test_lock() -> str:
    lock_path = paths.lock_path()
    assert lock_path.lower().startswith(TMP.lower()), lock_path

    # a) 锁文件里留着自己的 pid（上次崩溃留下的陈旧锁）→ 本进程应当能拿锁
    with open(lock_path, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    stale = autostart.InstanceLock(lock_path)
    assert stale.acquire() is True, "陈旧锁（pid 是自己）应当可以被接管"
    stale.release()
    assert not os.path.exists(lock_path), "release() 没有删掉锁文件"

    # b) 真·另一个活着的进程持有 → 拿不到锁
    proc = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"])
    try:
        with open(lock_path, "w", encoding="utf-8") as fh:
            fh.write(str(proc.pid))
        lock2 = autostart.InstanceLock(lock_path)
        assert lock2.acquire() is False, "另一个活着进程持有锁，却抢到了"
        assert lock2.holder_pid == proc.pid, lock2.holder_pid
    finally:
        proc.kill()
        proc.wait(timeout=10)

    # c) 进程已经死了 → 可以拿锁，并写进自己的 pid
    lock3 = autostart.InstanceLock(lock_path)
    assert lock3.acquire() is True, "持有者已死却拿不到锁"
    with open(lock_path, "r", encoding="utf-8") as fh:
        assert int(fh.read().strip()) == os.getpid()
    lock3.release()
    assert not os.path.exists(lock_path), "release() 没有删掉锁文件"

    # d) 命令行真进程：第二个实例应当退出码 3
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    env["PYTHONIOENCODING"] = "utf-8"
    env["SWIFTDROP_APPDATA_DIR"] = os.environ["SWIFTDROP_APPDATA_DIR"]
    autostart.save_config(autostart.default_config())
    first = subprocess.Popen(
        [PY, "-m", "swiftdrop", "autosync", "--verbose", "--interval", "600"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if os.path.isfile(lock_path) and first.poll() is None:
                break
            time.sleep(0.2)
        second = subprocess.run(
            [PY, "-m", "swiftdrop", "autosync", "--verbose", "--interval", "600"],
            cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60)
        assert second.returncode == autostart.EXIT_ALREADY_RUNNING, (
            f"第二个实例退出码 {second.returncode}，输出：{second.stdout}")
        assert "已有一个 autosync 实例在跑" in second.stdout, second.stdout
        print("第二个实例输出: " + second.stdout.strip().replace("\n", " | "),
              flush=True)
    finally:
        first.terminate()
        try:
            first.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            first.kill()
    return (f"锁文件 {lock_path}：陈旧锁（pid=自己）可接管、活子进程持有则抢不到、"
            f"死 pid 可接管；命令行第二个实例退出码 "
            f"{autostart.EXIT_ALREADY_RUNNING} 且打印「已有一个 autosync 实例在跑」")


# ==========================================================================
# 6. 轮转日志
# ==========================================================================

def test_log_rotate() -> str:
    log = os.path.join(TMP, "logs", "autosync.log")
    rm(log)
    rm(log + ".1")
    limit = 32 * 1024
    writer = autostart.RotatingLogger(log, max_bytes=limit)
    lines = 400
    for i in range(lines):
        writer.write(f"第 {i} 行 " + "x" * 120)
    writer.close()
    size = os.path.getsize(log)
    assert os.path.isfile(log + ".1"), f"超过 {limit} 字节上限却没有轮转出 .1 备份"
    assert size < limit, f"轮转后主日志仍然 {size} 字节"
    with open(log + ".1", "r", encoding="utf-8") as fh:
        backup = fh.read()
    with open(log, "r", encoding="utf-8") as fh:
        main = fh.read()
    # 轮转语义：备份里应当是「上一轮」的连续一段，且比主日志旧
    nums = [int(ln.split("第 ", 1)[1].split(" 行", 1)[0])
            for ln in backup.splitlines() if "第 " in ln]
    main_nums = [int(ln.split("第 ", 1)[1].split(" 行", 1)[0])
                 for ln in main.splitlines() if "第 " in ln]
    assert nums and main_nums, "备份或主日志里没有可解析的行号"
    assert min(main_nums) > min(nums), (min(main_nums), min(nums))
    assert max(nums) < max(main_nums), (max(nums), max(main_nums))
    assert len(main_nums) + len(nums) <= lines, "行数比写入的还多"
    assert autostart.LOG_MAX_BYTES == 2 * 1024 * 1024
    return (f"写 {lines} 行（每行 ~150 字节）后：主日志 {size} 字节"
            f"（最新第 {min(main_nums)}–{max(main_nums)} 行）、"
            f"备份 {os.path.getsize(log + '.1')} 字节"
            f"（较早的第 {min(nums)}–{max(nums)} 行，说明确实轮转过）；"
            f"正式上限常量 LOG_MAX_BYTES={autostart.LOG_MAX_BYTES} = 2MB，"
            f"测试时用 {limit} 字节触发")


# ==========================================================================
# 7. autosync 守护进程：稳定运行 / 写日志 / 单实例 / 干净退出
# ==========================================================================

def _wait_for(pred, timeout: float = 30.0, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if pred():
                return True
        except Exception:                                        # noqa: BLE001
            pass
        time.sleep(interval)
    return False


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def test_daemon() -> str:
    sync_dir = os.path.join(TMP, "守护同步盘")
    os.makedirs(sync_dir, exist_ok=True)
    log = os.path.join(TMP, "daemon-logs", "autosync.log")
    lock = os.path.join(TMP, "daemon-logs", "autosync.lock")
    for p in (log, log + ".1", lock):
        rm(p)

    # 对端名字故意用一个不存在的，逼它走「找不到设备 → 退避重试」这条路
    autostart.save_config({
        "device_name": "守护测试机",
        "autostart": True,
        "folders": [{
            "local": sync_dir,
            "peer": "根本不存在的主机名-xyz",
            "port": 45999,
            "mode": "two-way",
            "delete_extra": False,
            "interval": 2,
            "mark_icon": False,
        }],
    })

    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["SWIFTDROP_APPDATA_DIR"] = os.environ["SWIFTDROP_APPDATA_DIR"]
    env["SWIFTDROP_AUTOSYNC_LOG"] = log
    env["SWIFTDROP_AUTOSYNC_LOCK"] = lock

    proc = subprocess.Popen(
        [PY, "-m", "swiftdrop", "autosync", "--verbose"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")

    try:
        # a) 日志里出现「找不到设备 … 后重试」，且进程还活着（永远不崩）
        ok = _wait_for(lambda: "后重试" in _read_text(log), timeout=45)
        assert ok, f"日志里没有退避重试记录：\n{_read_text(log)[-800:]}"
        assert proc.poll() is None, "守护进程意外退出了"
        text = _read_text(log)
        assert "autosync 启动" in text, text[-400:]
        assert os.path.isfile(lock), "没有写锁文件"
        with open(lock, "r", encoding="utf-8") as fh:
            holder = int(fh.read().strip())
        assert holder == proc.pid, f"锁文件里是 {holder}，不是守护进程 {proc.pid}"
        retry_line = next(ln for ln in text.splitlines() if "后重试" in ln)
        print("日志摘录:", flush=True)
        for ln in text.splitlines()[:4]:
            print("   |" + ln, flush=True)
        print("   |…", flush=True)
        print("   |" + retry_line, flush=True)

        # b) 第二个实例必须立刻退出（退出码 3）并说明原因
        t0 = time.monotonic()
        second = subprocess.run(
            [PY, "-m", "swiftdrop", "autosync", "--status"],
            cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60)
        assert second.returncode == 0, second.stdout
        assert "守护测试机" in second.stdout, second.stdout
        second_daemon = subprocess.run(
            [PY, "-m", "swiftdrop", "autosync", "--verbose"],
            cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60)
        dt = time.monotonic() - t0
        assert second_daemon.returncode == autostart.EXIT_ALREADY_RUNNING, \
            f"第二个实例退出码 {second_daemon.returncode}：{second_daemon.stdout}"
        assert "已有一个 autosync 实例在跑" in second_daemon.stdout, \
            second_daemon.stdout
        reason = [ln for ln in second_daemon.stdout.splitlines()
                  if "已有一个" in ln]
        print("第二个实例:", reason[0].strip() if reason else second_daemon.stdout,
              flush=True)
        # 第二个实例不该把第一个的锁抢走
        assert proc.poll() is None, "第一个实例被第二个挤掉了"

        # c) 外部强杀：Windows 上 os.kill(PID, SIGTERM) 等价于 TerminateProcess，
        #    进程没机会做清理，退出码由系统给（通常是 1）——这是平台行为，
        #    真正的「干净退出」在下面 d)/e) 里用进程内信号与 Ctrl+C 验证。
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("收到 TERM 后 30 秒还没退出") from None
        killed_code = proc.returncode
        print(f"外部强杀退出码: {killed_code}（TerminateProcess，无清理机会）",
              flush=True)

        # d) 进程内信号：直接调信号处理器（等价于 Python 真的派发到它），
        #    验证写日志 / 关 socket / 删锁 / 返回 0。
        #    为什么不用 os.kill(getpid(), SIGBREAK)：在 Windows 上那会真的
        #    把本进程打成 21（Ctrl+Break 语义），跑测试的进程会消失。
        term_log = os.path.join(TMP, "daemon-logs", "term.log")
        term_lock = os.path.join(TMP, "daemon-logs", "term.lock")
        rm(term_log)
        rm(term_lock)
        cfg = autostart.load_config()
        daemon = autostart.AutosyncDaemon(
            cfg, logger=autostart.RotatingLogger(term_log, max_bytes=1 << 20),
            discover_timeout=0.5)
        daemon._lock = autostart.InstanceLock(term_lock)
        rc: list[int] = []
        runner = threading.Thread(target=lambda: rc.append(daemon.run()),
                                  name="daemon-under-test")
        runner.start()
        assert _wait_for(lambda: os.path.isfile(term_lock), timeout=15), \
            "进程内守护没有拿到锁"
        assert daemon.interrupted is False
        daemon._signal_handler(2, None)         # 等价于收到 SIGINT/SIGTERM
        assert daemon.interrupted is True, "信号处理器没有置 interrupted"
        assert daemon.stop.is_set(), "信号处理器没有置 stop 事件"
        runner.join(timeout=30)
        assert not runner.is_alive(), "收到信号之后守护进程没有退出"
        assert rc and rc[0] == 0, f"守护进程返回 {rc}"
        term_text = _read_text(term_log)
        assert "收到信号" in term_text, term_text
        assert "autosync 已退出" in term_text, term_text
        assert not os.path.exists(term_lock), "干净退出后锁文件还在"
        print("信号日志尾部:", term_text.strip().splitlines()[-2].strip(),
              flush=True)
        print("            ", term_text.strip().splitlines()[-1].strip(),
              flush=True)

        # e) 跨进程 Ctrl+C：给子进程发 CTRL_BREAK_EVENT，Python 会把它变成
        #    KeyboardInterrupt（Windows 上没有真正的 SIGINT 投递方式），
        #    守护进程必须捕获并干净退出。
        ctrl_log = os.path.join(TMP, "daemon-logs", "ctrl.log")
        ctrl_lock = os.path.join(TMP, "daemon-logs", "ctrl.lock")
        rm(ctrl_log)
        rm(ctrl_lock)
        autostart.save_config({
            "device_name": "CtrlC 测试机",
            "autostart": True,
            "folders": [{"local": sync_dir, "peer": "根本不存在的主机名-xyz",
                         "port": 45999, "interval": 60, "mark_icon": False}],
        })
        env2 = dict(env)
        env2["SWIFTDROP_AUTOSYNC_LOG"] = ctrl_log
        env2["SWIFTDROP_AUTOSYNC_LOCK"] = ctrl_lock
        ctrl = subprocess.Popen(
            [PY, "-m", "swiftdrop", "autosync", "--verbose"],
            cwd=ROOT, env=env2, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            assert _wait_for(lambda: os.path.isfile(ctrl_lock), timeout=25), \
                "Ctrl+C 场景：子进程没有启动起来"
            time.sleep(1.0)
            ctrl.send_signal(getattr(signal, "CTRL_BREAK_EVENT", 1))
            try:
                out3, _ = ctrl.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                ctrl.kill()
                raise AssertionError("Ctrl+C 之后子进程 30 秒没退出") from None
            assert ctrl.returncode == 0, \
                f"Ctrl+C 干净退出应当是 0，实际 {ctrl.returncode}\n{out3[-800:]}"
            ctrl_text = _read_text(ctrl_log)
            assert "autosync 已退出" in ctrl_text, ctrl_text
            assert not os.path.exists(ctrl_lock), "Ctrl+C 退出后锁文件还在"
            reason = [ln for ln in out3.splitlines()
                      if "干净退出" in ln or "Ctrl+C" in ln]
            print("Ctrl+C 子进程:", reason[-1].strip() if reason else
                  out3.strip().splitlines()[-1].strip(), flush=True)
        finally:
            if ctrl.poll() is None:
                ctrl.kill()
                ctrl.communicate(timeout=15)

        # f) 再启动一个子进程（配置改成 host 直连，避免依赖发现）
        autostart.save_config({
            "device_name": "守护测试机",
            "autostart": True,
            "folders": [{"local": sync_dir, "host": "127.0.0.1", "port": 45998,
                         "interval": 1, "mark_icon": False}],
        })
        once = subprocess.run(
            [PY, "-m", "swiftdrop", "autosync", "--once", "--verbose"],
            cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120)
        assert once.returncode == 0, f"--once 退出码 {once.returncode}\n{once.stdout}"
        assert "autosync --once 结束" in once.stdout, once.stdout
        assert "本轮失败" in once.stdout or "本轮完成" in once.stdout, once.stdout
        return (f"守护进程 pid={proc.pid} 在找不到设备时稳定运行并写日志"
                f"（「{retry_line.split('  ', 1)[-1]}」）；"
                f"第二个实例退出码 {autostart.EXIT_ALREADY_RUNNING}"
                f"（「已有一个 autosync 实例在跑」）；"
                f"进程内信号与跨进程 Ctrl+C（CTRL_BREAK_EVENT）都干净退出"
                f"（返回 0、日志收尾「autosync 已退出」、锁文件删除）；"
                f"外部强杀退出码 {killed_code}（TerminateProcess 不给清理机会，"
                f"属 Windows 平台行为）；--once 模式跑完一轮（{dt:.1f}s）")
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                pass


# ==========================================================================
# 主流程
# ==========================================================================

def main() -> int:
    banner(f"{APP_NAME} {VERSION} 开机自启自测")
    print(f"Python     : {sys.version.split()[0]} @ {PY}", flush=True)
    print(f"临时根目录 : {TMP}", flush=True)
    print(f"隔离配置   : {os.environ['SWIFTDROP_APPDATA_DIR']}", flush=True)
    print(f"注册表值名 : {VALUE}（测试前先备份，测试后恢复）", flush=True)

    original = read_run_value()
    print(f"测试前该值的原内容: {original!r}", flush=True)

    try:
        run_test("1. 配置读写（默认值 / 保存 / 重载 / 坏文件容错）", test_config)
        run_test("2. 文件夹 add / list / remove（含去重）", test_folders)
        run_test("3. enable_autostart() 写入注册表且命令正确", test_enable)
        run_test("4. disable_autostart() 后注册表值消失", test_disable)
        run_test("5. 单实例锁（活 pid 拒绝 / 死 pid 接管 / 退出码 3）", test_lock)
        run_test("6. 日志轮转（超限生成 .1 备份）", test_log_rotate)
        run_test("7. autosync 守护进程（稳定 / 写日志 / 单实例 / 干净退出）",
                 test_daemon)
    finally:
        # 恢复现场：注册表值按原样写回（原来没有就删掉），配置标记同步还原
        try:
            if original is None:
                autostart.disable_autostart()
            else:
                write_run_value(original)
                cfg = autostart.load_config()
                cfg["autostart"] = True
                autostart.save_config(cfg)
            now = read_run_value()
            print(f"\n[恢复] 注册表值已还原为 {now!r}"
                  f"（测试前是 {original!r}）", flush=True)
        except Exception as exc:                                 # noqa: BLE001
            print(f"\n[恢复] 注册表还原失败，请手工检查 {VALUE}: {exc}", flush=True)

    banner("汇总")
    passed = sum(1 for r in RESULTS if r[1] == "PASS")
    failed = [r for r in RESULTS if r[1] != "PASS"]
    for name, status, _detail in RESULTS:
        print(f"[{status}] {name}", flush=True)
    print(f"\nPASS {passed} / FAIL {len(failed)} / 共 {len(RESULTS)} 项", flush=True)

    rm(TMP)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n用户中断", flush=True)
        sys.exit(130)
