"""棕仙的传输软件 目录清单 / 差异计算 / 双向同步与持续监控。

清单
----
``manifest_of(dir)`` 返回 ``{relpath: {"size","mtime","sha256"}}``。
sha256 默认**懒计算**：先只比 size+mtime，只有「大小相同但 mtime 接近」这种
判不准的情况才回退去算哈希，所以大目录的清单非常快。

差异
----
``diff(local, remote)`` 返回 ``{to_send, to_recv, same, conflicts}``：

* size/mtime 一致 → same
* mtime 相差 ≥ 2 秒 → 新者胜（本地新进 to_send，远端新进 to_recv）
* mtime 相差 < 2 秒但内容不同 → conflicts（默认跳过不覆盖，列进日志）

同步
----
* ``--two-way``（默认）：两边都补齐
* ``--one-way``：本地单向覆盖远端
* ``--delete-extra``：删除「另一边没有」的文件（默认关，绝不删）
* ``--watch``：``--interval`` 秒轮询一次，Ctrl+C 干净退出

接收方向（把远端的新文件拉回来）用的是「对端反向推」：本机临时起一个
ReceiverServer，然后在控制连接上发 ``sync_offer`` 请对端把文件推过来，
沿用同一套多流并发 + 断点续传代码。
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Callable, Iterable

from .protocol import (
    DEFAULT_STREAMS,
    ConnectionClosed,
    ProtocolError,
    ResumeState,
    file_sha256,
    handshake_client,
    handshake_server,
    human_bytes,
    is_safe_relpath,
    recv_json,
    resolve_under,
    send_json,
)
from .transfer import ReceiverServer, Sender, collect_files

IGNORE_FILES = {".swiftdrop-state.json", ".DS_Store", "Thumbs.db"}
IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".svn", ".hg", ".idea"}
CONFLICT_WINDOW = 2.0          # mtime 相差 < 2 秒视为「判不准」


# --------------------------------------------------------------------------
# 忽略规则
# --------------------------------------------------------------------------

def is_ignored(rel_path: str, name: str | None = None) -> bool:
    """默认忽略规则：状态文件 / *.part / .git/ / node_modules/ / ~$*。"""
    name = name if name is not None else os.path.basename(rel_path)
    if name in IGNORE_FILES:
        return True
    if name.endswith(".part"):
        return True
    if name.startswith("~$"):
        return True
    parts = rel_path.replace("\\", "/").split("/")
    return any(p in IGNORE_DIRS for p in parts if p)


def hash_file(path: str) -> str:
    return file_sha256(path)


# --------------------------------------------------------------------------
# 清单
# --------------------------------------------------------------------------

def manifest_of(root: str, *, hashes: bool = False,
                progress: Callable[[str], None] | None = None) -> dict:
    """递归生成目录清单。

    :param hashes: 是否立刻计算全部 SHA-256（默认 False = 懒计算）
    """
    root = os.path.abspath(root)
    out: dict[str, dict] = {}
    if not os.path.isdir(root):
        raise NotADirectoryError(f"不是目录: {root}")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS)
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if is_ignored(rel, fn):
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            entry = {"size": st.st_size, "mtime": st.st_mtime, "sha256": None}
            if hashes:
                try:
                    entry["sha256"] = hash_file(full)
                except OSError:
                    continue
            out[rel] = entry
            if progress and len(out) % 200 == 0:
                progress(f"已扫描 {len(out)} 个文件…")
    return out


def manifest_from_state(root: str) -> dict:
    """把接收端状态文件转成清单（给 CLI 打印 / 对端比对用）。"""
    state = ResumeState(root)
    out: dict[str, dict] = {}
    for rel, e in state.snapshot().items():
        if not e.get("done"):
            continue
        out[rel] = {"size": e["size"], "mtime": e["mtime"], "sha256": e.get("sha256")}
    return out


def _need_hash(rel: str, local_dir: str, a: dict, b: dict) -> bool:
    """size 相同、mtime 也接近时，只能靠内容判定。"""
    if a.get("size") != b.get("size"):
        return False
    if a.get("sha256") and b.get("sha256"):
        return False
    if abs(float(a.get("mtime", 0)) - float(b.get("mtime", 0))) >= CONFLICT_WINDOW:
        return False
    return True


def _content_hash(root: str, rel: str, cached: str | None) -> str | None:
    if cached:
        return cached
    try:
        return hash_file(resolve_under(root, rel))
    except (OSError, ProtocolError):
        return None


# --------------------------------------------------------------------------
# 差异计算
# --------------------------------------------------------------------------

def diff(local: dict, remote: dict, *, local_dir: str | None = None,
         remote_dir: str | None = None, two_way: bool = True,
         delete_extra: bool = False) -> dict:
    """比较两份清单，返回 ``{to_send,to_recv,same,conflicts,delete_local,delete_remote}``。

    ``local_dir`` / ``remote_dir`` 至少给一个才能对「判不准」的文件做内容比对；
    不给我们宁可保守：记为 conflict 而不是覆盖。
    """
    result: dict[str, list] = {
        "to_send": [], "to_recv": [], "same": [],
        "conflicts": [], "delete_local": [], "delete_remote": [],
    }
    for rel in sorted(set(local) | set(remote)):
        a, b = local.get(rel), remote.get(rel)
        if a is None:
            result["to_recv"].append(rel)
            continue
        if b is None:
            result["to_send"].append(rel)
            continue
        if a["size"] == b["size"] and abs(a["mtime"] - b["mtime"]) < 1e-6:
            result["same"].append(rel)
            continue
        if _need_hash(rel, local_dir or "", a, b):
            ha = _content_hash(local_dir, rel, a.get("sha256")) if local_dir else None
            hb = (_content_hash(remote_dir, rel, b.get("sha256"))
                  if remote_dir else None)
            if ha and hb and ha == hb:
                result["same"].append(rel)
                continue
            if ha and hb and ha != hb:
                # 大小相同、内容不同、mtime 又很近 → 冲突，默认跳过
                result["conflicts"].append({
                    "path": rel, "why": "mtime 相差 <2s 且内容不同",
                    "local_mtime": a["mtime"], "remote_mtime": b["mtime"],
                    "size": a["size"],
                })
                continue
            if ha and hb and ha == hb:
                result["same"].append(rel)
                continue
            if not (ha and hb):
                result["conflicts"].append({
                    "path": rel, "why": "无法读取内容做比对，保守跳过",
                    "local_mtime": a["mtime"], "remote_mtime": b["mtime"],
                    "size": a["size"],
                })
                continue
        dt = a["mtime"] - b["mtime"]
        if abs(dt) < CONFLICT_WINDOW:
            result["conflicts"].append({
                "path": rel, "why": f"mtime 相差 {abs(dt):.2f}s <2s 且大小不同",
                "local_mtime": a["mtime"], "remote_mtime": b["mtime"],
                "size": a["size"],
            })
            continue
        if dt > 0:
            result["to_send"].append(rel)          # 本地更新
        else:
            if two_way:
                result["to_recv"].append(rel)      # 远端更新
            # one-way：本地覆盖远端，旧版本直接重推
            else:
                result["to_send"].append(rel)
    if delete_extra:
        result["delete_remote"] = sorted(set(remote) - set(local))
        if two_way:
            result["delete_local"] = sorted(set(local) - set(remote))
    return result


def describe_diff(d: dict) -> str:
    return (f"待发送 {len(d['to_send'])} / 待接收 {len(d['to_recv'])} / "
            f"一致 {len(d['same'])} / 冲突 {len(d['conflicts'])}"
            + (f" / 待删(本地) {len(d['delete_local'])}"
               f" / 待删(远端) {len(d['delete_remote'])}"
               if d.get("delete_local") or d.get("delete_remote") else ""))


# --------------------------------------------------------------------------
# 远端控制（差异同步）
# --------------------------------------------------------------------------

class RemoteSyncClient:
    """连到对端 ReceiverServer 的控制连接，用 sync_* 消息做双向同步。"""

    def __init__(self, host: str, port: int, name: str | None = None,
                 timeout: float = 30.0):
        self.host = host
        self.port = int(port)
        self.name = name or socket.gethostname()
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.remote_name = ""

    def __enter__(self) -> "RemoteSyncClient":
        self.sock = socket.create_connection((self.host, self.port),
                                            timeout=self.timeout)
        hello = handshake_client(self.sock, self.name, 0)
        self.remote_name = str(hello.get("name", self.host))
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.sock:
                send_json(self.sock, {"type": "quit"})
        except (OSError, ProtocolError):
            pass
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass

    def command(self, msg: dict) -> dict:
        if self.sock is None:
            raise ProtocolError("尚未连接")
        send_json(self.sock, msg)
        return recv_json(self.sock)


# --------------------------------------------------------------------------
# 同步引擎
# --------------------------------------------------------------------------

class SyncReport:
    def __init__(self):
        self.pushed: list[str] = []
        self.pulled: list[str] = []
        self.deleted_local: list[str] = []
        self.deleted_remote: list[str] = []
        self.conflicts: list[dict] = []
        self.others: list[dict] = []       # 远端其他会话的动作
        self.failed: list[tuple[str, str]] = []
        self.bytes_up = 0
        self.bytes_down = 0
        self.elapsed = 0.0

    @property
    def changed(self) -> bool:
        return bool(self.pushed or self.pulled or self.deleted_local
                    or self.deleted_remote or self.failed)

    def summary(self) -> str:
        return (f"推送 {len(self.pushed)} / 拉取 {len(self.pulled)} / "
                f"删除(本地) {len(self.deleted_local)} / "
                f"删除(远端) {len(self.deleted_remote)} / "
                f"冲突 {len(self.conflicts)} / 失败 {len(self.failed)}  "
                f"↑{human_bytes(self.bytes_up)} ↓{human_bytes(self.bytes_down)}")


class SyncEngine:
    """一次同步 = 算清单 → 定差异 → 推 / 拉 / 删。"""

    def __init__(self, local_dir: str, host: str, port: int, *,
                 two_way: bool = True, delete_extra: bool = False,
                 streams: int = DEFAULT_STREAMS, name: str | None = None,
                 on_log: Callable[[str], None] | None = None,
                 on_progress: Callable[[dict], None] | None = None,
                 stop_event: threading.Event | None = None):
        self.local_dir = os.path.abspath(local_dir)
        self.host = host
        self.port = int(port)
        self.two_way = two_way
        self.delete_extra = delete_extra
        self.streams = streams
        self.name = name or socket.gethostname()
        self.on_log = on_log
        self.on_progress = on_progress
        self.stop_event = stop_event or threading.Event()

    def log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)
        else:
            print(msg, flush=True)

    def _emit(self, **kw) -> None:
        if self.on_progress:
            try:
                self.on_progress(kw)
            except Exception:
                pass

    # -- 单轮 -------------------------------------------------------------
    def run_once(self) -> SyncReport:
        t0 = time.monotonic()
        report = SyncReport()
        os.makedirs(self.local_dir, exist_ok=True)
        local = manifest_of(self.local_dir)
        self.log(f"本地清单: {len(local)} 个文件")

        with RemoteSyncClient(self.host, self.port, self.name) as client:
            self.log(f"已连接 {client.remote_name} ({self.host}:{self.port})")
            offer = client.command({
                "type": "sync_offer", "dir": os.path.basename(self.local_dir) or "sync",
                "manifest": local, "two_way": self.two_way,
                "delete_extra": self.delete_extra,
            })
            if offer.get("type") != "sync_ok":
                raise ProtocolError(f"对端拒绝同步: {offer}")
            remote = offer.get("manifest") or {}
            remote_dir = offer.get("dir")
            self.log(f"远端清单: {len(remote)} 个文件（{remote_dir}）")

            d = diff(local, remote, local_dir=self.local_dir,
                     remote_dir=None, two_way=self.two_way,
                     delete_extra=self.delete_extra)
            report.conflicts = list(d["conflicts"])
            for c in d["conflicts"]:
                self.log(f"冲突跳过: {c['path']} —— {c['why']}")
            self.log("差异: " + describe_diff(d))
            self._emit(phase="diff", diff={k: len(v) for k, v in d.items()})

            # 1) 先删除远端多余文件。
            #    必须在推送之前做：远端独有的文件在 one-way 下会被本地版本
            #    「覆盖式重推」，推完就不再是「多余」了，删不掉。
            if self.delete_extra and d.get("delete_remote") \
                    and not self.stop_event.is_set():
                ack = client.command({"type": "delete",
                                      "paths": d["delete_remote"]})
                if ack.get("type") == "delete_ok":
                    report.deleted_remote = list(ack.get("deleted", []))
                    if report.deleted_remote:
                        self.log(f"远端已删除 {len(report.deleted_remote)} 个多余文件")
                    if ack.get("why"):
                        self.log(f"远端提示: {ack['why']}")
                else:
                    self.log(f"远端拒绝删除: {ack.get('why')}")

            # 2) 再删本地多余（two-way 才做；one-way 下本地是权威，不删）
            if self.delete_extra and self.two_way and d.get("delete_local") \
                    and not self.stop_event.is_set():
                report.deleted_local = self._delete_local(d["delete_local"])

            # 3) 推本地更新的文件
            if d["to_send"] and not self.stop_event.is_set():
                paths = [resolve_under(self.local_dir, r) for r in d["to_send"]]
                res = Sender(self.host, self.port, paths, streams=self.streams,
                             name=self.name, base=self.local_dir,
                             on_log=self.log,
                             on_progress=self.on_progress,
                             stop_event=self.stop_event).run()
                report.pushed = [r for r in d["to_send"]
                                 if r not in dict(res.failed_files)]
                report.bytes_up = res.bytes_sent
                report.failed += res.failed_files

            # 4) 拉远端更新的文件（本机临时开接收端，请对端反向推）
            if d["to_recv"] and not self.stop_event.is_set() and self.two_way:
                self._pull_from_remote(client, d["to_recv"], report)

            # 5) 顺便收集对端其它会话的动作（两个方向同时同步时很有用）
            try:
                st = client.command({"type": "status"})
                report.others = list(st.get("actions", []))
            except (ProtocolError, OSError):
                pass
        report.elapsed = time.monotonic() - t0
        return report

    def _reconcile_deletes(self, client: RemoteSyncClient, local: dict,
                           report: SyncReport) -> None:
        """（保留给手工排查用）传输结束后重新取对端清单并对账删除。

        注意：正式流程**不再**用它 —— 删除必须在推送之前做，否则 one-way
        下「远端独有」的文件会先被本地版本覆盖，就再也不算多余了。
        """
        try:
            st = client.command({"type": "manifest"})
        except (ProtocolError, OSError):
            return
        if st.get("type") != "manifest_ok":
            return
        remote_now = {f["path"]: f for f in (st.get("files") or [])}
        local_now = manifest_of(self.local_dir)
        extra_remote = sorted(set(remote_now) - set(local_now))
        if extra_remote:
            ack = client.command({"type": "delete", "paths": extra_remote})
            if ack.get("type") == "delete_ok":
                report.deleted_remote = list(ack.get("deleted", []))
                if report.deleted_remote:
                    self.log(f"远端已删除 {len(report.deleted_remote)} 个多余文件")
                if ack.get("why"):
                    self.log(f"远端提示: {ack['why']}")
            else:
                self.log(f"远端拒绝删除: {ack.get('why')}")

    def _pull_from_remote(self, client: RemoteSyncClient, rels: list[str],
                          report: SyncReport) -> None:
        srv = ReceiverServer(self.local_dir, 0, name=self.name,
                             on_log=self.log, on_progress=self.on_progress,
                             stop_event=self.stop_event)
        srv.start()
        try:
            self.log(f"本机接收端口 {srv.port}，请对端回传 {len(rels)} 个文件")
            ack = client.command({
                "type": "sync_pull", "host": "", "port": srv.port,
                "paths": rels, "streams": self.streams,
                "want_manifest": True,
            })
            if ack.get("type") not in ("sync_result", "sync_ack"):
                raise ProtocolError(f"回传失败: {ack}")
            report.pulled = list(rels)
            report.bytes_down = int(ack.get("bytes", 0) or 0)
            for item in ack.get("failed", []):
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    report.failed.append((str(item[0]), str(item[1])))
            if ack.get("error"):
                self.log(f"对端报告: {ack['error']}")
        finally:
            # 给最后几个分片一点落盘时间，再关服务
            time.sleep(0.15)
            srv.stop()

    def _delete_local(self, rels: Iterable[str]) -> list[str]:
        deleted: list[str] = []
        for rel in rels:
            try:
                path = resolve_under(self.local_dir, rel)
                os.remove(path)
                deleted.append(rel)
                self.log(f"删除本地多余文件: {rel}")
            except (OSError, ProtocolError) as exc:
                self.log(f"删除失败 {rel}: {exc}")
        for dirpath, dirnames, filenames in os.walk(self.local_dir, topdown=False):
            if not dirnames and not filenames and os.path.abspath(dirpath) != self.local_dir:
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
        return deleted

    # -- 持续监控 ---------------------------------------------------------
    def watch(self, interval: float = 3.0) -> None:
        self.log(f"开始持续监控（每 {interval:.1f} 秒一轮），Ctrl+C 退出")
        try:
            while not self.stop_event.is_set():
                try:
                    report = self.run_once()
                    if report.changed or report.conflicts:
                        self.log("本轮: " + report.summary())
                    else:
                        self.log("本轮: 无变化")
                    self._emit(phase="watch-idle", summary=report.summary())
                except (ProtocolError, OSError, LookupError) as exc:
                    self.log(f"本轮失败：{exc}（{interval:.0f}s 后重试）")
                waited = 0.0
                while waited < interval and not self.stop_event.is_set():
                    time.sleep(min(0.2, interval - waited))
                    waited += 0.2
        except KeyboardInterrupt:
            self.log("收到 Ctrl+C，正在退出监控…")
        finally:
            self.log("监控已停止")


# --------------------------------------------------------------------------
# 接收端一侧的 sync_* 处理（由 ReceiverServer 的控制循环调用）
# --------------------------------------------------------------------------

class SyncResponder:
    """挂在 ``ReceiverServer`` 上，让本机也能作为同步的被动方。

    * ``sync_offer``：对端给我清单 → 我算差异，回我的清单；对方会先推、再拉
    * ``sync_pull`` ：对端要我推文件给它 → 我连它的 host:port 用 Sender 推
    * ``delete``    ：对端要求删除多余文件（只有 ``allow_delete`` 打开才执行）
    """

    def __init__(self, local_dir: str, *, allow_delete: bool = False,
                 streams: int = DEFAULT_STREAMS, name: str | None = None,
                 on_log: Callable[[str], None] | None = None,
                 on_progress: Callable[[dict], None] | None = None,
                 stop_event: threading.Event | None = None):
        self.local_dir = os.path.abspath(local_dir)
        self.allow_delete = allow_delete
        self.streams = streams
        self.name = name or socket.gethostname()
        self.on_log = on_log
        self.on_progress = on_progress
        self.stop_event = stop_event or threading.Event()
        self.actions: list[dict] = []
        self.sync_engine: SyncEngine | None = None

    def log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)
        else:
            print(msg, flush=True)

    def handle(self, ctrl: socket.socket, msg: dict, client_ip: str) -> bool:
        mtype = msg.get("type")
        if mtype == "sync_offer":
            self._on_offer(ctrl, msg, client_ip)
            return True
        if mtype == "sync_pull":
            self._on_pull(ctrl, msg, client_ip)
            return True
        if mtype == "delete":
            self._on_delete(ctrl, msg)
            return True
        if mtype == "status":
            send_json(ctrl, {"type": "status_ok", "name": self.name,
                             "dir": self.local_dir, "actions": self.actions[-50:]})
            return True
        if mtype == "manifest":
            local = manifest_of(self.local_dir)
            send_json(ctrl, {
                "type": "manifest_ok", "name": self.name, "dir": self.local_dir,
                "files": [{"path": rel, "size": e["size"], "mtime": e["mtime"],
                           "sha256": e.get("sha256")}
                          for rel, e in sorted(local.items())],
            })
            return True
        return False

    def _on_offer(self, ctrl: socket.socket, msg: dict, client_ip: str) -> None:
        remote = msg.get("manifest")
        if not isinstance(remote, dict):
            send_json(ctrl, {"type": "sync_error", "why": "清单格式非法"})
            return
        two_way = bool(msg.get("two_way", True))
        local = manifest_of(self.local_dir)
        target = self._incoming_dir(msg.get("dir"))
        send_json(ctrl, {
            "type": "sync_ok", "name": self.name, "dir": target,
            "manifest": local, "port": 0, "two_way": two_way,
        })
        self.log(f"同步请求来自 {client_ip}：本地 {len(local)} 个文件，"
                 f"对端 {len(remote)} 个文件")

    def _incoming_dir(self, hint) -> str:
        return self.local_dir

    def _on_pull(self, ctrl: socket.socket, msg: dict, client_ip: str) -> None:
        host = str(msg.get("host") or client_ip)
        port = int(msg.get("port") or 0)
        paths = [str(p) for p in (msg.get("paths") or [])]
        if port <= 0:
            send_json(ctrl, {"type": "sync_result", "ok": False,
                             "why": "缺少接收端口"})
            return
        picked: list[str] = []
        missing: list[str] = []
        for rel in paths:
            try:
                full = resolve_under(self.local_dir, rel)
            except ProtocolError:
                missing.append(rel)
                continue
            if os.path.isfile(full):
                picked.append(full)
            else:
                missing.append(rel)
        if not picked:
            send_json(ctrl, {"type": "sync_result", "ok": False,
                             "why": "没有可发送的文件",
                             "missing": missing, "bytes": 0, "failed": []})
            return
        self.log(f"回传给 {host}:{port} —— {len(picked)} 个文件")
        try:
            res = Sender(host, port, picked, streams=self.streams,
                         name=self.name, base=self.local_dir,
                         on_log=self.log,
                         on_progress=self.on_progress,
                         stop_event=self.stop_event).run()
            failed = res.failed_files
            bytes_sent = res.bytes_sent
            ok = res.ok
        except (OSError, ProtocolError) as exc:
            failed = [(p, str(exc)) for p in picked]
            bytes_sent = 0
            ok = False
        self.actions.append({"t": time.time(), "kind": "push",
                             "files": [os.path.relpath(p, self.local_dir)
                                       for p in picked],
                             "bytes": bytes_sent, "ok": ok})
        send_json(ctrl, {"type": "sync_result", "ok": ok,
                         "missing": missing, "bytes": bytes_sent,
                         "failed": [list(x) for x in failed]})
        self.log(f"回传结束：{len(picked) - len(failed)} 成功 / {len(failed)} 失败")

    def _on_delete(self, ctrl: socket.socket, msg: dict) -> None:
        paths = [str(p) for p in (msg.get("paths") or [])]
        if not self.allow_delete:
            send_json(ctrl, {"type": "delete_ok", "deleted": [],
                             "why": "本端未开启 --delete-extra，已忽略删除请求"})
            self.log("对端请求删除文件，但本端未开启 --delete-extra，已忽略")
            return
        deleted, failed = [], []
        for rel in paths:
            try:
                os.remove(resolve_under(self.local_dir, rel))
                deleted.append(rel)
                self.log(f"按要求删除: {rel}")
            except (OSError, ProtocolError) as exc:
                failed.append([rel, str(exc)])
        self.actions.append({"t": time.time(), "kind": "delete",
                             "files": deleted, "bytes": 0, "ok": not failed})
        send_json(ctrl, {"type": "delete_ok", "deleted": deleted,
                         "failed": failed})


def sync_once(local_dir: str, host: str, port: int, *, two_way: bool = True,
              delete_extra: bool = False, streams: int = DEFAULT_STREAMS,
              on_log=None, on_progress=None,
              stop_event: threading.Event | None = None) -> SyncReport:
    return SyncEngine(local_dir, host, port, two_way=two_way,
                      delete_extra=delete_extra, streams=streams,
                      on_log=on_log, on_progress=on_progress,
                      stop_event=stop_event).run_once()
