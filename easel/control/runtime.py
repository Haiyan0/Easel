"""worker 生命周期编排：原子认领、就绪校验、drain 与空闲回收。

职责边界：

* 数据库是生命周期状态的唯一事实来源。进程内锁不能跨连接、跨管理进程，
  因此冷启动竞争、重启接管都由 ``Store`` 的租约所有者/代次裁决。
* 本模块不直接接触 Docker。所有容器动作经 ``Provisioner`` 协议转给受限的
  provisioner 进程；返回的句柄必须通过 workspace/配置版本/身份校验才可缓存。
* 任何 await 网络或容器操作都不发生在 Store 事务内；SQLite 事务都是短同步块。
* 停止只回收可再建的容器句柄；持久挂载由 provisioner 的 workspace 根管理，
  本模块从不删除目录、profile 或快照。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Protocol, runtime_checkable
from urllib.parse import urlsplit

from .store import Store
from .types import WorkerHandle, WorkerLease

MANAGED_LABEL = "easel.managed"
WORKSPACE_LABEL = "easel.workspace-id"
CONFIG_VERSION_LABEL = "easel.config-version"

READY_TIMEOUT_SECONDS = 60.0
IDLE_SECONDS = 900
LEASE_SECONDS = 120
DRAIN_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 0.05


class RuntimeUnavailable(Exception):
    """worker 暂时不可用；携带明确错误码、503 状态与 ``Retry-After``。

    错误信息只含状态码字符串，不含 upstream、服务 token 或容器环境。
    """

    def __init__(self, code: str, status: int = 503, retry_after: int = 5):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = int(retry_after)


@runtime_checkable
class Provisioner(Protocol):
    """受限 provisioner 的客户端协议；实现方拥有容器管理权限。"""

    async def ensure(self, workspace_id: str,
                     config_version: int) -> WorkerHandle:
        """幂等确保该 workspace 的容器已启动；失败必须自行清理半成品。"""

    async def stop(self, workspace_id: str, instance_id: str) -> None:
        """只停止该实例 ID；不得按同名容器停止新代次或删除持久挂载。"""

    async def inspect(self, workspace_id: str) -> dict:
        """返回实际容器身份；不含秘密，``container_id`` 为空表示不存在。"""


class RuntimeManager:
    """控制层唯一入口：按数据库租约创建、复用、对账与回收 worker。"""

    def __init__(self, store: Store, provisioner: Provisioner, *,
                 clock: Callable[[], float] | None = None,
                 owner_id: str | None = None,
                 lease_seconds: int = LEASE_SECONDS,
                 ready_timeout: float = READY_TIMEOUT_SECONDS,
                 poll_interval: float = POLL_INTERVAL_SECONDS,
                 idle_seconds: int = IDLE_SECONDS,
                 drain_timeout: float = DRAIN_TIMEOUT_SECONDS,
                 db_runner: Callable[..., Awaitable[object]] | None = None):
        self._store = store
        self._provisioner = provisioner
        self._clock = clock or time.time
        self.owner_id = owner_id or uuid.uuid4().hex
        self._lease_seconds = int(lease_seconds)
        self._ready_timeout = float(ready_timeout)
        self._poll_interval = max(0.001, float(poll_interval))
        self._idle_seconds = int(idle_seconds)
        self._drain_timeout = float(drain_timeout)
        # SQLite 连接有线程归属。真实 control 在 lifespan 用专用线程打开
        # Store，因此所有短数据库步骤都必须调度回该线程；容器/网络等待
        # 保持在异步层，不能占用数据库线程或事务。
        self._db_runner = db_runner

    async def _db(self, function, *args, **kwargs):
        """在 Store 所属线程执行一个短同步数据库步骤。"""
        if self._db_runner is None:
            return function(*args, **kwargs)
        return await self._db_runner(function, *args, **kwargs)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    async def ensure_ready(self, workspace_id: str,
                           config_version: int) -> WorkerHandle:
        """返回已就绪、身份已核验的句柄；不可用时抛 ``RuntimeUnavailable``。"""
        self._validate_request(workspace_id, config_version)
        deadline = self._clock() + self._ready_timeout
        while True:
            outcome, lease = await self._claim(workspace_id, config_version)
            if outcome == "ready":
                return self._handle_from_lease(lease, config_version)
            if outcome == "started":
                return await self._start(workspace_id, config_version, lease,
                                         deadline)
            if outcome == "reconcile":
                adopted = await self._reconcile(workspace_id, config_version,
                                                lease)
                if adopted is not None:
                    return adopted
                continue
            if outcome == "upgrade":
                await self._upgrade(workspace_id, lease, deadline)
                continue
            if outcome == "wait":
                await self._sleep_or_timeout(deadline, "worker_start_timeout")
                continue
            if outcome == "config_busy":
                raise RuntimeUnavailable("worker_config_busy", 503,
                                         retry_after=5)
            if outcome == "draining":
                raise RuntimeUnavailable("worker_draining", 503, retry_after=1)
            raise RuntimeUnavailable("worker_unavailable", 503)

    async def drain(self, workspace_id: str) -> None:
        """拒绝新任务，等活动任务与请求归零后停止容器；超时保留 draining。

        认领、停止与写回都绑定读取到的租约代次。等待或停止期间若新代次
        已接管，旧动作在调用 stop 前放弃，迟到回复也不能写回新租约。
        """
        self._text(workspace_id)
        lease = await self._db(self._store.get_worker_lease, workspace_id)
        if lease is None or lease.state in ("absent", "stopped", "failed"):
            return
        if lease.state == "starting":
            # 启动中的实例无法安全判定归属，拒绝而不是停止一个可能属于新代次的容器
            raise RuntimeUnavailable("worker_starting", 503, retry_after=1)
        if lease.state == "stopping":
            # 另一个 drain 已取得停止认领；不重复调用 stop。
            return
        if lease.state == "ready":
            if not await self._db(
                    self._store.mark_worker_draining, workspace_id,
                    self._now(), generation=lease.generation):
                return
        elif lease.state != "draining":
            return
        deadline = self._clock() + self._drain_timeout
        while await self._has_active_workload(workspace_id):
            if self._clock() >= deadline:
                raise RuntimeUnavailable("worker_drain_timeout", 503,
                                         retry_after=5)
            await asyncio.sleep(self._poll_interval)
        # await 之后复核仍是同一实例/代次的 draining，然后原子认领停止。
        current = await self._db(self._store.get_worker_lease, workspace_id)
        if (current is None or current.generation != lease.generation
                or current.state != "draining"):
            return
        if not await self._db(
                self._store.begin_worker_stop, workspace_id, self._now(),
                generation=lease.generation):
            return
        await self._provisioner.stop(workspace_id, lease.container_id)
        await self._db(self._store.stop_worker, workspace_id, self._now(),
                       generation=lease.generation,
                       expected_state="stopping")

    async def collect_idle(self, now: float) -> list[str]:
        """回收超过空闲阈值且无任务/请求的实例，返回真正停止的 workspace。

        只回收心跳存活、无活跃 Run、无活跃请求的 ready 实例。停止失败不伪报
        成功，状态保留 stopping/draining 等待租约到期后的后续对账处理。
        """
        stamp = int(now)
        collected: list[str] = []
        for lease in await self._db(
                self._store.list_idle_workers, stamp, self._idle_seconds):
            if not await self._db(
                    self._store.mark_worker_draining, lease.workspace_id,
                    stamp, generation=lease.generation):
                continue
            if await self._has_active_workload(lease.workspace_id):
                # 刚进入 draining 又出现任务：回滚 ready，待其结束后再回收。
                await self._db(
                    self._store.resume_worker_ready, lease.workspace_id,
                    stamp, generation=lease.generation)
                continue
            if not await self._db(
                    self._store.begin_worker_stop, lease.workspace_id,
                    stamp, generation=lease.generation):
                continue
            try:
                await self._provisioner.stop(lease.workspace_id,
                                             lease.container_id)
            except Exception:
                # 失败关闭：无法确认容器已停止时不写 stopped
                continue
            await self._db(self._store.stop_worker, lease.workspace_id,
                           self._now(), generation=lease.generation,
                           expected_state="stopping")
            collected.append(lease.workspace_id)
        return collected

    async def heartbeat(self, workspace_id: str, *, generation: int,
                        instance_id: str,
                        active_requests: int | None = None) -> bool:
        """续期调用者已持有的代次/实例；绝不读取“当前租约”代旧持有者授权。

        ``active_requests`` 用于空闲判定。空闲实例仍会续期（进程还活着），
        但不刷新 ``last_activity_at``，因此超过 900 秒无请求、无 Run 时仍
        可被回收。
        """
        self._text(workspace_id)
        self._text(instance_id)
        if type(generation) is not int or generation < 0:
            raise ValueError("generation")
        return await self._db(
            self._store.heartbeat_worker, workspace_id, generation,
            self._now(), self._lease_seconds, active_requests,
            instance_id=instance_id)

    # ------------------------------------------------------------------
    # 启动 / 对账 / 升级
    # ------------------------------------------------------------------

    async def _claim(self, workspace_id: str,
                     config_version: int) -> tuple[str, WorkerLease | None]:
        return await self._db(
            self._store.claim_worker_start,
            workspace_id, config_version, self.owner_id, self._now(),
            self._lease_seconds)

    async def _start(self, workspace_id: str, config_version: int,
                     lease: WorkerLease | None,
                     deadline: float) -> WorkerHandle:
        if lease is None:
            raise RuntimeUnavailable("worker_unavailable", 503)
        remaining = deadline - self._clock()
        if remaining <= 0:
            await self._fail_start(workspace_id, lease)
            raise RuntimeUnavailable("worker_start_timeout", 503, retry_after=5)
        try:
            handle = await asyncio.wait_for(
                self._provisioner.ensure(workspace_id, config_version),
                timeout=remaining)
        except asyncio.TimeoutError:
            await self._fail_start(workspace_id, lease)
            raise RuntimeUnavailable("worker_start_timeout", 503,
                                     retry_after=5) from None
        except asyncio.CancelledError:
            # 调用方取消也要释放认领，避免遗留 starting 阻塞后续启动
            await self._fail_start(workspace_id, lease)
            raise
        except Exception as error:
            await self._fail_start(workspace_id, lease)
            raise RuntimeUnavailable("worker_start_failed", 503,
                                     retry_after=5) from error

        problem = self._handle_problem(handle, workspace_id, config_version)
        if problem is not None:
            await self._fail_start(workspace_id, lease)
            raise RuntimeUnavailable(problem, 503, retry_after=5)
        if not await self._db(
                self._store.complete_worker_start,
                workspace_id, self.owner_id, lease.generation, handle,
                self._now(), self._lease_seconds):
            # 迟到返回：新代次已接管，绝不覆盖
            raise RuntimeUnavailable("worker_start_superseded", 503,
                                     retry_after=1)
        return handle

    async def _reconcile(self, workspace_id: str, config_version: int,
                         lease: WorkerLease | None) -> WorkerHandle | None:
        """租约过期后核对真实容器；身份不符则失败关闭，不接管。"""
        if lease is None:
            raise RuntimeUnavailable("worker_unavailable", 503)
        if not self._valid_lease_handle(lease):
            await self._fail_ready(workspace_id, lease)
            raise RuntimeUnavailable("worker_identity_mismatch", 503,
                                     retry_after=5)
        try:
            report = await self._provisioner.inspect(workspace_id)
        except Exception as error:
            await self._fail_ready(workspace_id, lease)
            raise RuntimeUnavailable("worker_inspect_failed", 503,
                                     retry_after=5) from error
        if not self._report_matches(report, workspace_id, config_version,
                                    lease):
            await self._fail_ready(workspace_id, lease)
            raise RuntimeUnavailable("worker_identity_mismatch", 503,
                                     retry_after=5)
        adopted = await self._db(
            self._store.adopt_worker_ready,
            workspace_id, lease.generation, self.owner_id, self._now(),
            self._lease_seconds)
        if not adopted:
            # 另一个管理进程先接管：重新认领读取新代次
            return None
        refreshed = await self._db(
            self._store.get_worker_lease, workspace_id)
        return self._handle_from_lease(refreshed, config_version)

    async def _upgrade(self, workspace_id: str, lease: WorkerLease,
                       deadline: float) -> None:
        """无活跃任务时安全重建到新配置版本；有任务时认领阶段已拒绝。

        认领可能已发生一段时间，必须携带代次，避免把另一个进程刚建立的
        新代次误标为 draining。
        """
        if not await self._db(
                self._store.mark_worker_draining, workspace_id, self._now(),
                generation=lease.generation):
            return
        drain_deadline = self._clock() + self._drain_timeout
        while await self._has_active_workload(workspace_id):
            if self._clock() >= drain_deadline:
                raise RuntimeUnavailable("worker_drain_timeout", 503,
                                         retry_after=5)
            await asyncio.sleep(self._poll_interval)
        current = await self._db(self._store.get_worker_lease, workspace_id)
        if (current is None or current.generation != lease.generation
                or current.state != "draining"):
            return
        if not await self._db(
                self._store.begin_worker_stop, workspace_id, self._now(),
                generation=lease.generation):
            return
        await self._provisioner.stop(workspace_id, lease.container_id)
        await self._db(self._store.stop_worker, workspace_id, self._now(),
                       generation=lease.generation,
                       expected_state="stopping")

    # ------------------------------------------------------------------
    # 校验与辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_request(workspace_id: str, config_version: int) -> None:
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("workspace_id")
        # bool 是 int 的子类，必须显式排除，否则 True 会被当成版本 1
        if type(config_version) is not int or config_version < 0:
            raise ValueError("config_version")

    @staticmethod
    def _handle_problem(handle: object, workspace_id: str,
                        config_version: int) -> str | None:
        if not isinstance(handle, WorkerHandle):
            return "worker_identity_mismatch"
        if handle.workspace_id != workspace_id:
            return "worker_identity_mismatch"
        if handle.config_version != config_version:
            return "worker_identity_mismatch"
        if not _valid_identifier(handle.container_id):
            return "worker_identity_mismatch"
        if not _valid_token(handle.service_token):
            return "worker_identity_mismatch"
        if not _valid_upstream(handle.upstream):
            return "worker_identity_mismatch"
        return None

    @staticmethod
    def _valid_lease_handle(lease: WorkerLease) -> bool:
        return (lease.state == "ready"
                and _valid_identifier(lease.container_id)
                and _valid_token(lease.service_token)
                and _valid_upstream(lease.upstream))

    @staticmethod
    def _report_matches(report: object, workspace_id: str, config_version: int,
                        lease: WorkerLease) -> bool:
        """只认受管标签和身份响应同时匹配的实际容器。"""
        if not isinstance(report, dict):
            return False
        if report.get("managed") is not True or report.get("ready") is not True:
            return False
        if report.get("workspace_id") != workspace_id:
            return False
        if report.get("config_version") != config_version:
            return False
        if report.get("instance_id") != lease.container_id:
            return False
        if report.get("upstream") != lease.upstream:
            return False
        labels = report.get("labels")
        if not isinstance(labels, dict):
            return False
        return (labels.get(MANAGED_LABEL) == "true"
                and labels.get(WORKSPACE_LABEL) == workspace_id
                and labels.get(CONFIG_VERSION_LABEL) == str(config_version))

    def _handle_from_lease(self, lease: WorkerLease | None,
                           config_version: int) -> WorkerHandle:
        if not self._valid_lease_handle(lease):
            raise RuntimeUnavailable("worker_identity_mismatch", 503,
                                     retry_after=5)
        assert lease is not None
        if lease.config_version != config_version:
            raise RuntimeUnavailable("worker_identity_mismatch", 503,
                                     retry_after=5)
        return WorkerHandle(lease.workspace_id, lease.container_id,
                            lease.upstream, lease.service_token,
                            lease.config_version)

    async def _has_active_workload(self, workspace_id: str) -> bool:
        lease = await self._db(self._store.get_worker_lease, workspace_id)
        if lease is not None and lease.active_requests > 0:
            return True
        return await self._db(
            self._store.count_active_runs_for_workspace, workspace_id) > 0

    async def _fail_start(self, workspace_id: str,
                          lease: WorkerLease) -> None:
        await self._db(self._store.fail_worker_start, workspace_id,
                       self.owner_id, lease.generation, self._now())

    async def _fail_ready(self, workspace_id: str,
                          lease: WorkerLease) -> None:
        await self._db(self._store.fail_worker_state, workspace_id,
                       self._now(), expected_state="ready",
                       generation=lease.generation)

    async def _sleep_or_timeout(self, deadline: float, code: str) -> None:
        if self._clock() >= deadline:
            raise RuntimeUnavailable(code, 503, retry_after=5)
        await asyncio.sleep(min(self._poll_interval,
                                max(0.0, deadline - self._clock())))

    def _now(self) -> int:
        return int(self._clock())

    @staticmethod
    def _text(value: object) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("workspace_id")
        return value


def _valid_identifier(value: object) -> bool:
    return (isinstance(value, str)
            and 0 < len(value) <= 128
            and all(char.isalnum() or char in "-_." for char in value))


def _valid_token(value: object) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 512
            and "\n" not in value and "\r" not in value)


def _valid_upstream(value: object) -> bool:
    """只接受 host:port 形式的 http(s) 地址，拒绝凭证与路径注入。"""
    if not isinstance(value, str) or not value or len(value) > 256:
        return False
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.hostname or parsed.username or parsed.password:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if port is None:
        return False
    return parsed.path in ("", "/") and not parsed.query and not parsed.fragment


_PROVISIONER_ERROR = re.compile(r"^[a-z0-9_]{1,64}$")


class UnixProvisionerClient:
    """受限 provisioner 的本地 Unix socket 客户端。

    只发送结构化动作，不发送镜像/命令/挂载/网络/环境字段；响应按严格
    白名单解码。socket 不存在、权限不足或协议异常都映射为 503，绝不
    回退到进程内 Docker 调用。
    """

    ENSURE_TIMEOUT = 90.0
    STOP_TIMEOUT = 90.0
    INSPECT_TIMEOUT = 15.0

    def __init__(self, socket_path: str | Path):
        self.socket_path = str(socket_path)

    async def _call(self, payload: dict, timeout: float) -> dict:
        try:
            reader, writer = await asyncio.open_unix_connection(
                self.socket_path)
        except (OSError, AttributeError, NotImplementedError) as error:
            raise RuntimeUnavailable("provisioner_unavailable", 503,
                                     retry_after=5) from error
        try:
            writer.write(json.dumps(payload, separators=(",", ":")).encode(
                "utf-8") + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        except asyncio.TimeoutError as error:
            raise RuntimeUnavailable("provisioner_timeout", 503,
                                     retry_after=5) from error
        except OSError as error:
            raise RuntimeUnavailable("provisioner_unavailable", 503,
                                     retry_after=5) from error
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        try:
            response = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeUnavailable("provisioner_invalid_response", 503,
                                     retry_after=5) from None
        if not isinstance(response, dict):
            raise RuntimeUnavailable("provisioner_invalid_response", 503,
                                     retry_after=5)
        return response

    async def ensure(self, workspace_id: str,
                     config_version: int) -> WorkerHandle:
        response = await self._call({
            "version": 1, "action": "ensure", "workspace_id": workspace_id,
            "config_version": config_version}, self.ENSURE_TIMEOUT)
        return self._decode_ensure(response)

    async def stop(self, workspace_id: str, instance_id: str) -> None:
        response = await self._call({
            "version": 1, "action": "stop", "workspace_id": workspace_id,
            "instance_id": instance_id}, self.STOP_TIMEOUT)
        if response.get("ok") is not True:
            raise RuntimeUnavailable(
                _safe_error_code(response.get("error")), 503, retry_after=5)

    async def inspect(self, workspace_id: str) -> dict:
        response = await self._call({
            "version": 1, "action": "inspect", "workspace_id": workspace_id},
            self.INSPECT_TIMEOUT)
        if response.get("ok") is not True:
            raise RuntimeUnavailable(
                _safe_error_code(response.get("error")), 503, retry_after=5)
        report = response.get("report")
        if not isinstance(report, dict):
            raise RuntimeUnavailable("provisioner_invalid_response", 503,
                                     retry_after=5)
        return report

    @staticmethod
    def _decode_ensure(response: object) -> WorkerHandle:
        if not isinstance(response, dict) or response.get("ok") is not True:
            error = response.get("error") if isinstance(response, dict) else None
            raise RuntimeUnavailable(_safe_error_code(error), 503,
                                     retry_after=5)
        handle = response.get("handle")
        keys = {"workspace_id", "container_id", "upstream", "service_token",
                "config_version"}
        if not isinstance(handle, dict) or set(handle) != keys:
            raise RuntimeUnavailable("provisioner_invalid_response", 503,
                                     retry_after=5)
        if not all(isinstance(handle[key], str)
                   for key in keys - {"config_version"}):
            raise RuntimeUnavailable("provisioner_invalid_response", 503,
                                     retry_after=5)
        if type(handle["config_version"]) is not int:
            raise RuntimeUnavailable("provisioner_invalid_response", 503,
                                     retry_after=5)
        return WorkerHandle(handle["workspace_id"], handle["container_id"],
                            handle["upstream"], handle["service_token"],
                            handle["config_version"])


def _safe_error_code(value: object) -> str:
    """错误码只回显稳定标识，防止把任意文本带进响应/日志。"""
    if isinstance(value, str) and _PROVISIONER_ERROR.fullmatch(value):
        return value
    return "provisioner_error"
