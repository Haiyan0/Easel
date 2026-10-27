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
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from .store import Store
from .types import WorkerHandle, WorkerLease

MANAGED_LABEL = "easel.managed"
WORKSPACE_LABEL = "easel.workspace-id"
CONFIG_VERSION_LABEL = "easel.config-version"

READY_TIMEOUT_SECONDS = 60.0
CONFIRM_STOP_TIMEOUT_SECONDS = 15.0
IDLE_SECONDS = 900
LEASE_SECONDS = 120
DRAIN_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 0.05
_GATEWAY_ID = re.compile(r"^[0-9a-f]{64}$")
_BOOT_GENERATION = re.compile(r"^[0-9a-f]{32}$")


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

    async def ensure(self, workspace_id: str, config_version: int, *,
                     attempt_id: str = "") -> WorkerHandle:
        """幂等确保该 workspace 的容器已启动；失败必须自行清理半成品。"""

    async def stop(self, workspace_id: str, instance_id: str,
                   gateway_instance_id: str = "") -> None:
        """只停止该实例 ID；不得按同名容器停止新代次或删除持久挂载。"""

    async def activate(self, workspace_id: str, worker_container_id: str,
                       gateway_instance_id: str, boot_generation: str,
                       config_version: int) -> dict:
        """按候选的精确实例/boot 激活；只能在受保护 CAS 之后调用。"""

    async def restore_gateway(self, workspace_id: str,
                              worker_container_id: str,
                              config_version: int, *,
                              attempt_id: str = "") -> WorkerHandle:
        """只为已核验的同一 worker 重配/更换 gateway；绝不创建 worker。"""

    async def abort(self, workspace_id: str, worker_container_id: str,
                    gateway_instance_id: str, *, attempt_id: str, scope: str,
                    worker_created: bool,
                    gateway_created: bool) -> None:
        """激活失败时仅清理当前 attempt 的新建资源。"""

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
                 broker=None,
                 db_runner: Callable[..., Awaitable[object]] | None = None,
                 gateway_required: bool = False):
        self._store = store
        self._provisioner = provisioner
        self._clock = clock or time.time
        self.owner_id = owner_id or uuid.uuid4().hex
        self._lease_seconds = int(lease_seconds)
        self._ready_timeout = float(ready_timeout)
        self._poll_interval = max(0.001, float(poll_interval))
        self._idle_seconds = int(idle_seconds)
        self._drain_timeout = float(drain_timeout)
        # 实例失效是真正的终态：标记残留 Run 时同步撤销其临时 broker 令牌
        self._broker = broker
        # SQLite 连接有线程归属。真实 control 在 lifespan 用专用线程打开
        # Store，因此所有短数据库步骤都必须调度回该线程；容器/网络等待
        # 保持在异步层，不能占用数据库线程或事务。
        self._db_runner = db_runner
        self._gateway_required = bool(gateway_required)

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
                if not self._gateway_required:
                    return self._handle_from_lease(lease, config_version)
                return await self._ready_gateway(
                    workspace_id, config_version, lease, deadline)
            if outcome == "started":
                return await self._start(workspace_id, config_version, lease,
                                         deadline)
            if outcome == "reconcile":
                adopted = await self._reconcile(workspace_id, config_version,
                                                lease)
                if adopted is not None:
                    return adopted
                continue
            if outcome == "refresh":
                if lease is None:
                    raise RuntimeUnavailable("worker_unavailable", 503)
                return await self._refresh_gateway(
                    workspace_id, config_version, lease, deadline)
            if outcome == "upgrade":
                await self._upgrade(workspace_id, lease, deadline)
                continue
            if outcome == "recover_stop":
                await self._recover_stop(workspace_id, lease, deadline)
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

    async def bound_handle(self, workspace_id: str, container_id: str,
                           config_version: int) -> WorkerHandle | None:
        """按不可复用实例 ID 取得既有 Run 的句柄；绝不启动或替换实例。

        ``draining`` 仍允许结束/回答/回放；实例 ID 或配置代次不匹配时返回
        ``None``，调用方按中断收尾，不能把操作发到新代次伪装成原 Run。

        gateway 暂不可用/refresh 进行中必须与 worker 身份消失区分：前者抛
        ``RuntimeUnavailable``（503），绝不返回 None，避免后台对账把仍在
        执行的 Run 误判为实例失效并结算。
        """
        lease = await self._db(self._store.get_worker_lease, workspace_id)
        if (lease is None or lease.container_id != container_id
                or lease.config_version != config_version):
            return None
        if lease.state == "starting":
            if self._gateway_required and lease.container_id:
                raise RuntimeUnavailable(
                    "worker_gateway_refresh_in_progress", 503,
                    retry_after=1)
            return None
        if lease.state == "stopping":
            if (lease.cleanup_purpose == "start_abort"
                    and lease.cleanup_scope == "gateway"):
                # gateway-only abort 恢复期间既有 worker 仍必须保留。
                raise RuntimeUnavailable(
                    "gateway_cleanup_in_progress", 503, retry_after=1)
            return None
        if lease.state not in ("ready", "draining"):
            return None
        if not lease.upstream or not lease.service_token:
            return None
        if lease.gateway_instance_id:
            if lease.gateway_state != "available":
                raise RuntimeUnavailable(
                    "gateway_unavailable", 503, retry_after=5)
            if not self._gateway_fields_valid(lease):
                raise RuntimeUnavailable(
                    "gateway_unavailable", 503, retry_after=5)
        elif self._gateway_required:
            raise RuntimeUnavailable("gateway_unavailable", 503,
                                     retry_after=5)
        return WorkerHandle(
            lease.workspace_id, lease.container_id,
            lease.upstream, lease.service_token, lease.config_version,
            gateway_instance_id=lease.gateway_instance_id,
            gateway_generation=lease.gateway_generation,
            gateway_upstream=lease.gateway_upstream,
            gateway_state=lease.gateway_state)

    async def confirm_instance_stopped(self, workspace_id: str,
                                       container_id: str) -> bool:
        """按不可复用实例 ID 确认旧实例已停止；失败时保持待确认。

        ``provisioner.stop`` 对已不存在的目标幂等成功；只有调用成功才可
        作为“执行已结束”的证据。不得用 ``bound_handle`` 不可用、inspect
        失败或租约状态代替该确认。
        """
        if not isinstance(container_id, str) or not container_id:
            return False
        gateway_instance_id = ""
        if self._gateway_required:
            lease = await self._db(
                self._store.get_worker_lease, workspace_id)
            if (lease is None or lease.container_id != container_id
                    or not lease.gateway_instance_id):
                return False
            gateway_instance_id = lease.gateway_instance_id
        try:
            await asyncio.wait_for(
                self._stop_provisioner(
                    workspace_id, container_id, gateway_instance_id),
                timeout=CONFIRM_STOP_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return True

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
        await self._stop_lease(lease)
        await self._db(self._store.stop_worker, workspace_id, self._now(),
                       generation=lease.generation,
                       expected_state="stopping")

    async def drain_all(self) -> list[str]:
        """为本地备份/升级顺序 drain 所有活动 workspace，返回已处理空间。

        备份调用方必须在本方法返回后重新读取租约；仍处于 starting/stopping
        等中间态时不得开始文件系统备份。单个空间 drain 的失败原样向上抛出，
        不把“部分 drain”伪装成已完成。
        """
        leases = await self._db(self._store.list_worker_leases)
        drained: list[str] = []
        for lease in leases:
            if lease.state not in ("starting", "ready", "draining", "stopping"):
                continue
            await self.drain(lease.workspace_id)
            drained.append(lease.workspace_id)
        return drained

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
                await self._stop_lease(lease)
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

    async def _ready_gateway(self, workspace_id: str, config_version: int,
                             lease: WorkerLease | None,
                             deadline: float) -> WorkerHandle:
        """ready 租约在 gateway 模式下每次核对实际 boot/可用性。"""
        if lease is None:
            raise RuntimeUnavailable("worker_unavailable", 503)
        if self._gateway_fields_valid(lease):
            try:
                report = await self._provisioner.inspect(workspace_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise RuntimeUnavailable(
                    "gateway_inspect_failed", 503, retry_after=5) from error
            if not self._report_worker_matches(
                    report, workspace_id, config_version, lease):
                raise RuntimeUnavailable(
                    "worker_identity_mismatch", 503, retry_after=5)
            # 身份匹配不等于可派发；worker/sidecar 物理未就绪时只拒绝，
            # 不删除 worker，也不把 gateway 刷新当成冷启动。
            if not self._report_worker_ready(report):
                raise RuntimeUnavailable(
                    "worker_not_ready", 503, retry_after=5)
            if self._report_gateway_matches(
                    report, workspace_id, config_version, lease):
                if lease.gateway_state != "available":
                    if not await self._db(
                            self._store.mark_worker_gateway_available,
                            workspace_id, lease.generation, self._now()):
                        raise RuntimeUnavailable(
                            "worker_start_superseded", 503, retry_after=1)
                    refreshed = await self._db(
                        self._store.get_worker_lease, workspace_id)
                    return self._handle_from_lease(
                        refreshed, config_version)
                return self._handle_from_lease(lease, config_version)
        return await self._refresh_gateway(
            workspace_id, config_version, lease, deadline)

    async def _refresh_gateway(self, workspace_id: str, config_version: int,
                               lease: WorkerLease,
                               deadline: float) -> WorkerHandle:
        """重新配置/激活 gateway；CAS 后失败只标 unavailable，不中断 Run。"""
        attempt_id = uuid.uuid4().hex
        try:
            candidate = await self._ensure_candidate(
                workspace_id, config_version, deadline, lease,
                attempt_id=attempt_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            if lease.state == "starting":
                await self._db(
                    self._store.fail_expired_worker_gateway_refresh,
                    workspace_id, lease.generation, self._now())
            else:
                await self._db(
                    self._store.mark_worker_gateway_unavailable,
                    workspace_id, lease.generation, self._now())
            raise
        if candidate.container_id != lease.container_id:
            # 不能把新的 worker 当作旧 Run 的同一实例；保留旧租约等待
            # 可信 inspect/停止证据，不在这里结算 Run。
            raise RuntimeUnavailable("worker_identity_mismatch", 503,
                                     retry_after=5)
        return await self._commit_gateway_candidate(
            workspace_id, config_version, lease, candidate, attempt_id,
            deadline)

    async def restore_gateway(self, workspace_id: str, container_id: str,
                              config_version: int) -> WorkerHandle | None:
        """只为既有 worker 重配/更换 gateway；绝不创建或替换 worker。

        返回 ``None`` 表示持久租约已不指向该实例（调用方可按实例消失处理）；
        gateway 暂时不可用/refresh 在途则抛 ``RuntimeUnavailable``，调用方
        只能返回 503，不得结算在途 Run。
        """
        self._validate_request(workspace_id, config_version)
        lease = await self._db(self._store.get_worker_lease, workspace_id)
        if (lease is None or lease.container_id != container_id
                or lease.config_version != config_version):
            return None
        if lease.state == "starting":
            raise RuntimeUnavailable(
                "worker_gateway_refresh_in_progress", 503, retry_after=1)
        if lease.state == "stopping":
            raise RuntimeUnavailable(
                "worker_stopping", 503, retry_after=1)
        if (lease.state not in ("ready", "draining")
                or not lease.upstream or not lease.service_token):
            return None
        if lease.state == "draining":
            # 不能把 draining 改成 starting 后恢复成 ready，否则会重新接纳
            # 新任务；draining 中的 gateway 故障交给显式 drain/stop 处理。
            raise RuntimeUnavailable(
                "worker_draining", 503, retry_after=1)
        deadline = self._clock() + self._ready_timeout
        attempt_id = uuid.uuid4().hex
        candidate = await self._restore_candidate(
            workspace_id, container_id, config_version, deadline, attempt_id)
        if candidate.container_id != lease.container_id:
            raise RuntimeUnavailable("worker_identity_mismatch", 503,
                                     retry_after=5)
        return await self._commit_gateway_candidate(
            workspace_id, config_version, lease, candidate, attempt_id,
            deadline)

    async def _restore_candidate(self, workspace_id: str, container_id: str,
                                 config_version: int, deadline: float,
                                 attempt_id: str) -> WorkerHandle:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise RuntimeUnavailable("gateway_recovery_timeout", 503,
                                     retry_after=5)
        restore = getattr(self._provisioner, "restore_gateway", None)
        if not callable(restore):
            raise RuntimeUnavailable("gateway_recovery_unavailable", 503,
                                     retry_after=5)
        try:
            handle = await asyncio.wait_for(
                restore(workspace_id, container_id, config_version,
                        attempt_id=attempt_id),
                timeout=remaining)
        except asyncio.TimeoutError:
            raise RuntimeUnavailable("gateway_recovery_timeout", 503,
                                     retry_after=5) from None
        except asyncio.CancelledError:
            raise
        except RuntimeUnavailable:
            raise
        except Exception as error:
            raise RuntimeUnavailable(
                getattr(error, "code", "gateway_recovery_failed"), 503,
                retry_after=5) from error
        problem = self._handle_problem(
            handle, workspace_id, config_version, gateway_required=True)
        if problem is not None:
            raise RuntimeUnavailable(problem, 503, retry_after=5)
        return handle

    async def _commit_gateway_candidate(
            self, workspace_id: str, config_version: int, lease: WorkerLease,
            candidate: WorkerHandle, attempt_id: str,
            deadline: float) -> WorkerHandle:
        """在受保护 CAS 中保存候选、激活并收口；refresh/restore 共用。"""
        staged = await self._db(
            self._store.begin_worker_gateway_refresh,
            workspace_id, lease.generation, self.owner_id, candidate,
            attempt_id, self._now(), self._lease_seconds)
        if staged is None:
            raise RuntimeUnavailable("worker_start_superseded", 503,
                                     retry_after=1)
        try:
            final = await self._activate_candidate(
                workspace_id, config_version, candidate, deadline)
        except asyncio.CancelledError:
            if candidate.worker_created or candidate.gateway_created:
                await self._cleanup_candidate(
                    workspace_id, candidate, staged, attempt_id)
            else:
                await self._db(
                    self._store.fail_worker_gateway_refresh,
                    workspace_id, self.owner_id, staged.generation,
                    self._now())
            raise
        except Exception as error:
            if candidate.worker_created or candidate.gateway_created:
                cleaned = await self._cleanup_candidate(
                    workspace_id, candidate, staged, attempt_id)
                if not cleaned:
                    raise RuntimeUnavailable(
                        "worker_start_superseded", 503,
                        retry_after=1) from error
            else:
                # 无本轮新建资源：仅恢复 ready/unavailable，保留 worker
                # 与在途 Run。
                await self._db(
                    self._store.fail_worker_gateway_refresh,
                    workspace_id, self.owner_id, staged.generation,
                    self._now())
            raise RuntimeUnavailable(
                getattr(error, "code", "gateway_unavailable"), 503,
                retry_after=5) from error
        if not await self._db(
                self._store.complete_worker_gateway_refresh,
                workspace_id, self.owner_id, staged.generation, final,
                self._now(), self._lease_seconds):
            raise RuntimeUnavailable("worker_start_superseded", 503,
                                     retry_after=1)
        return final

    async def _ensure_candidate(self, workspace_id: str, config_version: int,
                                deadline: float, lease: WorkerLease | None,
                                *, attempt_id: str = "") -> WorkerHandle:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise RuntimeUnavailable("worker_start_timeout", 503,
                                     retry_after=5)
        try:
            if self._gateway_required:
                awaitable = self._provisioner.ensure(
                    workspace_id, config_version, attempt_id=attempt_id)
            else:
                awaitable = self._provisioner.ensure(
                    workspace_id, config_version)
            handle = await asyncio.wait_for(awaitable, timeout=remaining)
        except asyncio.TimeoutError:
            raise RuntimeUnavailable("worker_start_timeout", 503,
                                     retry_after=5) from None
        except asyncio.CancelledError:
            raise
        except RuntimeUnavailable:
            raise
        except Exception as error:
            raise RuntimeUnavailable("worker_start_failed", 503,
                                     retry_after=5) from error
        problem = self._handle_problem(
            handle, workspace_id, config_version,
            gateway_required=self._gateway_required)
        if problem is not None:
            raise RuntimeUnavailable(problem, 503, retry_after=5)
        return handle

    async def _activate_candidate(self, workspace_id: str,
                                  config_version: int, candidate: WorkerHandle,
                                  deadline: float) -> WorkerHandle:
        if candidate.gateway_state == "available":
            return replace(candidate, gateway_state="available")
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise RuntimeUnavailable("gateway_activation_timeout", 503,
                                     retry_after=5)
        try:
            status = await asyncio.wait_for(
                self._provisioner.activate(
                    workspace_id, candidate.container_id,
                    candidate.gateway_instance_id,
                    candidate.gateway_generation, config_version),
                timeout=remaining)
        except asyncio.TimeoutError:
            raise RuntimeUnavailable("gateway_activation_timeout", 503,
                                     retry_after=5) from None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise RuntimeUnavailable(
                getattr(error, "code", "gateway_activation_failed"), 503,
                retry_after=5) from error
        gateway = status.get("gateway") if isinstance(status, dict) else None
        if (not isinstance(gateway, dict)
                or gateway.get("gateway_instance_id")
                != candidate.gateway_instance_id
                or gateway.get("boot_generation")
                != candidate.gateway_generation
                or gateway.get("worker_container_id")
                != candidate.container_id
                or gateway.get("config_version") != config_version
                or gateway.get("available") is not True):
            raise RuntimeUnavailable("gateway_activation_invalid", 503,
                                     retry_after=5)
        return replace(candidate, gateway_state="available")

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
        attempt_id = uuid.uuid4().hex
        try:
            if self._gateway_required:
                awaitable = self._provisioner.ensure(
                    workspace_id, config_version, attempt_id=attempt_id)
            else:
                awaitable = self._provisioner.ensure(
                    workspace_id, config_version)
            handle = await asyncio.wait_for(awaitable, timeout=remaining)
        except asyncio.TimeoutError:
            await self._fail_start(workspace_id, lease)
            raise RuntimeUnavailable("worker_start_timeout", 503,
                                     retry_after=5) from None
        except asyncio.CancelledError:
            # 调用方取消也要释放认领，避免遗留 starting 阻塞后续启动
            await self._fail_start(workspace_id, lease)
            raise
        except RuntimeUnavailable:
            await self._fail_start(workspace_id, lease)
            raise
        except Exception as error:
            await self._fail_start(workspace_id, lease)
            raise RuntimeUnavailable("worker_start_failed", 503,
                                     retry_after=5) from error

        problem = self._handle_problem(
            handle, workspace_id, config_version,
            gateway_required=self._gateway_required)
        if problem is not None:
            await self._fail_start(workspace_id, lease)
            raise RuntimeUnavailable(problem, 503, retry_after=5)
        if self._gateway_required:
            if not await self._db(
                    self._store.stage_worker_gateway_start,
                    workspace_id, self.owner_id, lease.generation, handle,
                    attempt_id, self._now()):
                raise RuntimeUnavailable("worker_start_superseded", 503,
                                         retry_after=1)
            try:
                handle = await self._activate_candidate(
                    workspace_id, config_version, handle, deadline)
            except asyncio.CancelledError:
                # 旧 owner 的取消不能清理已接管的新代次；清理认领失败
                # 时原样传播取消，不写 DB。
                await self._cleanup_candidate(
                    workspace_id, handle, lease, attempt_id)
                raise
            except Exception as error:
                cleaned = await self._cleanup_candidate(
                    workspace_id, handle, lease, attempt_id)
                if not cleaned:
                    raise RuntimeUnavailable(
                        "worker_start_superseded", 503,
                        retry_after=1) from error
                raise RuntimeUnavailable(
                    getattr(error, "code", "gateway_activation_failed"), 503,
                    retry_after=5) from error
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
        if self._gateway_required:
            if not self._worker_lease_valid(lease):
                await self._fail_ready(workspace_id, lease)
                raise RuntimeUnavailable("worker_identity_mismatch", 503,
                                         retry_after=5)
            try:
                report = await self._provisioner.inspect(workspace_id)
            except Exception as error:
                raise RuntimeUnavailable(
                    "gateway_inspect_failed", 503,
                    retry_after=5) from error
            if not self._report_worker_matches(
                    report, workspace_id, config_version, lease):
                await self._fail_ready(workspace_id, lease)
                raise RuntimeUnavailable("worker_identity_mismatch", 503,
                                         retry_after=5)
            if not self._report_worker_ready(report):
                raise RuntimeUnavailable("worker_not_ready", 503,
                                         retry_after=5)
            return await self._refresh_gateway(
                workspace_id, config_version, lease,
                self._clock() + self._ready_timeout)
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
        await self._stop_lease(lease)
        await self._db(self._store.stop_worker, workspace_id, self._now(),
                       generation=lease.generation,
                       expected_state="stopping")

    async def _recover_stop(self, workspace_id: str, lease: WorkerLease | None,
                            deadline: float) -> None:
        """停止认领过期后，先按原实例 ID 完成停止，再允许新代次。

        旧 stop 可能仍在途中；在物理实例被确认删除前创建/复用新代次，
        会让旧动作删除新一代正在使用的同一容器。这里复用同一条 ID 约束
        的 stop；目标已不存在时 provisioner 幂等成功。
        """
        if lease is None or lease.state != "stopping":
            raise RuntimeUnavailable("worker_unavailable", 503)
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise RuntimeUnavailable("worker_start_timeout", 503,
                                     retry_after=5)
        purpose = lease.cleanup_purpose
        scope = lease.cleanup_scope
        if purpose == "start_abort":
            if scope not in ("gateway", "worker", "worker_gateway"):
                raise RuntimeUnavailable("gateway_cleanup_incomplete", 503,
                                         retry_after=5)
            if not lease.attempt_id:
                raise RuntimeUnavailable("gateway_abort_unknown_attempt", 503,
                                         retry_after=5)
            abort = getattr(self._provisioner, "abort", None)
            if not callable(abort):
                raise RuntimeUnavailable("gateway_cleanup_unavailable", 503)
            try:
                await asyncio.wait_for(
                    abort(
                        workspace_id, lease.container_id,
                        lease.gateway_instance_id,
                        attempt_id=lease.attempt_id, scope=scope,
                        worker_created=scope in (
                            "worker", "worker_gateway"),
                        gateway_created=scope in (
                            "gateway", "worker_gateway")),
                    timeout=remaining)
            except asyncio.TimeoutError:
                raise RuntimeUnavailable("gateway_cleanup_timeout", 503,
                                         retry_after=5) from None
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise RuntimeUnavailable(
                    getattr(error, "code", "gateway_cleanup_failed"),
                    503, retry_after=5) from error
            if scope == "gateway":
                if not await self._db(
                        self._store.finish_gateway_abort,
                        workspace_id, lease.generation, self._now(),
                        self._lease_seconds):
                    raise RuntimeUnavailable("gateway_cleanup_incomplete", 503,
                                             retry_after=5)
                return
            await self._db(
                self._store.stop_worker, workspace_id, self._now(),
                generation=lease.generation, expected_state="stopping")
            return
        # 旧记录/业务 drain：默认完整 worker+gateway 停止。
        try:
            await asyncio.wait_for(
                self._stop_lease(lease), timeout=remaining)
        except asyncio.TimeoutError:
            raise RuntimeUnavailable("worker_stop_timeout", 503,
                                     retry_after=5) from None
        except Exception as error:
            raise RuntimeUnavailable("worker_stop_failed", 503,
                                     retry_after=5) from error
        # 只有成功确认物理实例已消失/停止后才写 stopped。CAS 失败说明另一
        # 管理者已消解停止承诺，回到认领循环读取新状态即可。
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
                        config_version: int, *,
                        gateway_required: bool = False) -> str | None:
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
        if gateway_required or handle.gateway_instance_id:
            if not RuntimeManager._gateway_fields_valid(handle):
                return "worker_identity_mismatch"
            if handle.gateway_state not in ("staged", "available"):
                return "worker_identity_mismatch"
        return None

    @staticmethod
    def _gateway_fields_valid(value: object) -> bool:
        gateway_id = getattr(value, "gateway_instance_id", "")
        generation = getattr(value, "gateway_generation", "")
        upstream = getattr(value, "gateway_upstream", "")
        return (
            isinstance(gateway_id, str) and _GATEWAY_ID.fullmatch(gateway_id)
            is not None
            and isinstance(generation, str)
            and _BOOT_GENERATION.fullmatch(generation) is not None
            and _valid_upstream(upstream))

    @staticmethod
    def _worker_lease_valid(lease: WorkerLease) -> bool:
        return (lease.state == "ready"
                and _valid_identifier(lease.container_id)
                and _valid_token(lease.service_token)
                and _valid_upstream(lease.upstream))

    @staticmethod
    def _valid_lease_handle(lease: WorkerLease) -> bool:
        if not (lease.state == "ready"
                and _valid_identifier(lease.container_id)
                and _valid_token(lease.service_token)
                and _valid_upstream(lease.upstream)):
            return False
        if lease.gateway_instance_id:
            return (lease.gateway_state == "available"
                    and RuntimeManager._gateway_fields_valid(lease))
        return True

    @staticmethod
    def _report_worker_matches(report: object, workspace_id: str,
                               config_version: int,
                               lease: WorkerLease) -> bool:
        if not isinstance(report, dict):
            return False
        if report.get("managed") is not True:
            return False
        if report.get("workspace_id") != workspace_id:
            return False
        if report.get("config_version") != config_version:
            return False
        if report.get("instance_id") != lease.container_id:
            return False
        labels = report.get("labels")
        if not isinstance(labels, dict):
            return False
        return (labels.get(MANAGED_LABEL) == "true"
                and labels.get(WORKSPACE_LABEL) == workspace_id
                and labels.get(CONFIG_VERSION_LABEL) == str(config_version))

    @staticmethod
    def _report_gateway_matches(report: object, workspace_id: str,
                                config_version: int,
                                lease: WorkerLease) -> bool:
        if not isinstance(report, dict):
            return False
        gateway = report.get("gateway")
        if not isinstance(gateway, dict):
            return False
        return (
            report.get("gateway_available") is True
            and gateway.get("managed") is True
            and gateway.get("available") is True
            and gateway.get("workspace_id") == workspace_id
            and gateway.get("config_version") == config_version
            and gateway.get("worker_container_id") == lease.container_id
            and gateway.get("gateway_instance_id")
            == lease.gateway_instance_id
            and report.get("gateway_generation") == lease.gateway_generation
            and report.get("gateway_upstream") == lease.gateway_upstream)

    @staticmethod
    def _report_matches(report: object, workspace_id: str, config_version: int,
                        lease: WorkerLease) -> bool:
        """只认受管标签和身份响应同时匹配的实际容器。"""
        if not isinstance(report, dict) or report.get("ready") is not True:
            return False
        if report.get("upstream") != lease.upstream:
            return False
        if not RuntimeManager._report_worker_matches(
                report, workspace_id, config_version, lease):
            return False
        if lease.gateway_instance_id or lease.gateway_state != "disabled":
            return RuntimeManager._report_gateway_matches(
                report, workspace_id, config_version, lease)
        return True

    def _handle_from_lease(self, lease: WorkerLease | None,
                           config_version: int) -> WorkerHandle:
        if lease is None or not self._valid_lease_handle(lease):
            raise RuntimeUnavailable("worker_identity_mismatch", 503,
                                     retry_after=5)
        if lease.config_version != config_version:
            raise RuntimeUnavailable("worker_identity_mismatch", 503,
                                     retry_after=5)
        if self._gateway_required and not lease.gateway_instance_id:
            raise RuntimeUnavailable("gateway_unavailable", 503,
                                     retry_after=5)
        return WorkerHandle(
            lease.workspace_id, lease.container_id,
            lease.upstream, lease.service_token, lease.config_version,
            gateway_instance_id=lease.gateway_instance_id,
            gateway_generation=lease.gateway_generation,
            gateway_upstream=lease.gateway_upstream,
            gateway_state=lease.gateway_state)

    async def _has_active_workload(self, workspace_id: str) -> bool:
        lease = await self._db(self._store.get_worker_lease, workspace_id)
        if lease is not None and lease.active_requests > 0:
            return True
        if await self._db(
                self._store.count_worker_request_leases, workspace_id,
                now=self._now()) > 0:
            return True
        return await self._db(
            self._store.count_active_runs_for_workspace, workspace_id) > 0

    async def _fail_start(self, workspace_id: str,
                          lease: WorkerLease) -> None:
        interrupted = await self._db(
            self._store.fail_worker_start, workspace_id, self.owner_id,
            lease.generation, self._now())
        await self._revoke_run_tokens(workspace_id, interrupted)

    async def _fail_ready(self, workspace_id: str,
                          lease: WorkerLease) -> None:
        # 实例失效是真正的终态：同一次受代次保护的事务里把残留 Run 标为
        # interrupted 并释放容量；worker 侧 reason/events 保留审查信息，
        # 不自动重发任务。返回的 Run 还需撤销其临时 broker 令牌。
        interrupted = await self._db(
            self._store.fail_worker_state, workspace_id, self._now(),
            expected_state="ready", generation=lease.generation)
        await self._revoke_run_tokens(workspace_id, interrupted)

    async def _revoke_run_tokens(self, workspace_id: str,
                                 run_ids: list[str]) -> None:
        if self._broker is None:
            return
        for run_id in run_ids:
            await self._db(self._broker.revoke_run, run_id, workspace_id)

    async def _sleep_or_timeout(self, deadline: float, code: str) -> None:
        if self._clock() >= deadline:
            raise RuntimeUnavailable(code, 503, retry_after=5)
        await asyncio.sleep(min(self._poll_interval,
                                max(0.0, deadline - self._clock())))

    async def _stop_provisioner(self, workspace_id: str, container_id: str,
                                gateway_instance_id: str) -> None:
        if gateway_instance_id:
            await self._provisioner.stop(
                workspace_id, container_id, gateway_instance_id)
        else:
            await self._provisioner.stop(workspace_id, container_id)

    async def _cleanup_candidate(self, workspace_id: str,
                                 handle: WorkerHandle, lease: WorkerLease,
                                 attempt_id: str) -> bool:
        """先取得当前 attempt 的停止承诺，再远程删除。

        返回 False 表示 CAS 失败（旧 owner/attempt 已被接管），调用方
        不得再发送 abort；返回 True 表示本 attempt 已完成清理承诺。
        """
        if not self._gateway_required:
            return False
        if not (handle.worker_created or handle.gateway_created):
            return False
        if handle.worker_created and handle.gateway_created:
            scope = "worker_gateway"
        elif handle.worker_created:
            scope = "worker"
        else:
            scope = "gateway"
        claimed = await self._db(
            self._store.begin_worker_abort,
            workspace_id, self.owner_id, lease.generation, attempt_id,
            scope, bool(handle.worker_created),
            bool(handle.gateway_created), self._now())
        if not claimed:
            return False
        abort = getattr(self._provisioner, "abort", None)
        if not callable(abort):
            raise RuntimeUnavailable("gateway_cleanup_unavailable", 503)
        try:
            await asyncio.wait_for(
                abort(
                    workspace_id, handle.container_id,
                    handle.gateway_instance_id, attempt_id=attempt_id,
                    scope=scope, worker_created=handle.worker_created,
                    gateway_created=handle.gateway_created),
                timeout=CONFIRM_STOP_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # 停止目的/范围/attempt 已持久化；新 owner 不会采用，恢复时
            # 必须按同一 scope 继续，不能升级为完整 worker stop。
            raise RuntimeUnavailable(
                getattr(error, "code", "gateway_cleanup_failed"), 503,
                retry_after=5) from error
        if scope == "gateway":
            if not await self._db(
                    self._store.finish_gateway_abort,
                    workspace_id, lease.generation, self._now(),
                    self._lease_seconds):
                raise RuntimeUnavailable("gateway_cleanup_incomplete", 503,
                                         retry_after=5)
            return True
        if not await self._db(
                self._store.stop_worker, workspace_id, self._now(),
                generation=lease.generation, expected_state="stopping"):
            raise RuntimeUnavailable("gateway_cleanup_incomplete", 503,
                                     retry_after=5)
        return True

    @staticmethod
    def _report_worker_ready(report: object) -> bool:
        if not isinstance(report, dict):
            return False
        value = report.get("worker_ready")
        if value is None:
            value = report.get("ready")
        return value is True

    async def _stop_lease(self, lease: WorkerLease) -> None:
        gateway = lease.gateway_instance_id if self._gateway_required else ""
        await self._stop_provisioner(
            lease.workspace_id, lease.container_id, gateway)

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
    ACTIVATE_TIMEOUT = 30.0

    def __init__(self, socket_path: str | Path, *,
                 gateway_required: bool = False):
        self.socket_path = str(socket_path)
        self.gateway_required = bool(gateway_required)

    @property
    def _protocol_version(self) -> int:
        return 2 if self.gateway_required else 1

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

    async def ensure(self, workspace_id: str, config_version: int, *,
                     attempt_id: str = "") -> WorkerHandle:
        payload = {
            "version": self._protocol_version, "action": "ensure",
            "workspace_id": workspace_id,
            "config_version": config_version}
        if self.gateway_required:
            payload["attempt_id"] = attempt_id
        response = await self._call(payload, self.ENSURE_TIMEOUT)
        return self._decode_ensure(
            response, gateway_required=self.gateway_required)

    async def stop(self, workspace_id: str, instance_id: str,
                   gateway_instance_id: str = "") -> None:
        payload = {
            "version": self._protocol_version, "action": "stop",
            "workspace_id": workspace_id, "instance_id": instance_id}
        if self.gateway_required:
            payload["gateway_instance_id"] = gateway_instance_id
        response = await self._call(payload, self.STOP_TIMEOUT)
        if response.get("ok") is not True:
            raise RuntimeUnavailable(
                _safe_error_code(response.get("error")), 503, retry_after=5)

    async def activate(self, workspace_id: str, worker_container_id: str,
                       gateway_instance_id: str, boot_generation: str,
                       config_version: int) -> dict:
        if not self.gateway_required:
            raise RuntimeUnavailable("gateway_disabled", 503)
        response = await self._call({
            "version": 2, "action": "activate", "workspace_id": workspace_id,
            "config_version": config_version,
            "worker_container_id": worker_container_id,
            "gateway_instance_id": gateway_instance_id,
            "boot_generation": boot_generation}, self.ACTIVATE_TIMEOUT)
        if response.get("ok") is not True:
            raise RuntimeUnavailable(
                _safe_error_code(response.get("error")), 503, retry_after=5)
        gateway = response.get("gateway")
        if not isinstance(gateway, dict):
            raise RuntimeUnavailable("provisioner_invalid_response", 503,
                                     retry_after=5)
        return {"gateway": gateway}

    async def restore_gateway(self, workspace_id: str,
                              worker_container_id: str,
                              config_version: int, *,
                              attempt_id: str = "") -> WorkerHandle:
        """受限 v2 恢复：只接受同一 worker ID/config，不创建新 worker。"""
        if not self.gateway_required:
            raise RuntimeUnavailable("gateway_disabled", 503)
        response = await self._call({
            "version": 2, "action": "restore_gateway",
            "workspace_id": workspace_id,
            "worker_container_id": worker_container_id,
            "config_version": config_version,
            "attempt_id": attempt_id}, self.ENSURE_TIMEOUT)
        return self._decode_ensure(response, gateway_required=True)

    async def abort(self, workspace_id: str, worker_container_id: str,
                    gateway_instance_id: str, *, attempt_id: str, scope: str,
                    worker_created: bool, gateway_created: bool) -> None:
        if not self.gateway_required:
            return
        response = await self._call({
            "version": 2, "action": "abort", "workspace_id": workspace_id,
            "worker_container_id": worker_container_id,
            "gateway_instance_id": gateway_instance_id,
            "worker_created": worker_created,
            "gateway_created": gateway_created,
            "attempt_id": attempt_id, "scope": scope}, self.STOP_TIMEOUT)
        if response.get("ok") is not True:
            raise RuntimeUnavailable(
                _safe_error_code(response.get("error")), 503, retry_after=5)

    async def inspect(self, workspace_id: str) -> dict:
        response = await self._call({
            "version": self._protocol_version, "action": "inspect",
            "workspace_id": workspace_id}, self.INSPECT_TIMEOUT)
        if response.get("ok") is not True:
            raise RuntimeUnavailable(
                _safe_error_code(response.get("error")), 503, retry_after=5)
        report = response.get("report")
        if not isinstance(report, dict):
            raise RuntimeUnavailable("provisioner_invalid_response", 503,
                                     retry_after=5)
        return report

    @staticmethod
    def _decode_ensure(response: object, *, gateway_required: bool = False
                       ) -> WorkerHandle:
        if not isinstance(response, dict) or response.get("ok") is not True:
            error = response.get("error") if isinstance(response, dict) else None
            raise RuntimeUnavailable(_safe_error_code(error), 503,
                                     retry_after=5)
        handle = response.get("handle")
        keys = {"workspace_id", "container_id", "upstream", "service_token",
                "config_version"}
        if gateway_required:
            keys |= {
                "gateway_instance_id", "gateway_generation",
                "gateway_upstream", "gateway_state", "worker_created",
                "gateway_created",
            }
        if not isinstance(handle, dict) or set(handle) != keys:
            raise RuntimeUnavailable("provisioner_invalid_response", 503,
                                     retry_after=5)
        if not all(isinstance(handle[key], str)
                   for key in keys - {"config_version", "worker_created",
                                      "gateway_created"}):
            raise RuntimeUnavailable("provisioner_invalid_response", 503,
                                     retry_after=5)
        if type(handle["config_version"]) is not int:
            raise RuntimeUnavailable("provisioner_invalid_response", 503,
                                     retry_after=5)
        if gateway_required and (
                type(handle["worker_created"]) is not bool
                or type(handle["gateway_created"]) is not bool):
            raise RuntimeUnavailable("provisioner_invalid_response", 503,
                                     retry_after=5)
        return WorkerHandle(
            handle["workspace_id"], handle["container_id"],
            handle["upstream"], handle["service_token"],
            handle["config_version"],
            gateway_instance_id=handle.get("gateway_instance_id", ""),
            gateway_generation=handle.get("gateway_generation", ""),
            gateway_upstream=handle.get("gateway_upstream", ""),
            gateway_state=handle.get("gateway_state", "disabled"),
            worker_created=handle.get("worker_created", False),
            gateway_created=handle.get("gateway_created", False))


def _safe_error_code(value: object) -> str:
    """错误码只回显稳定标识，防止把任意文本带进响应/日志。"""
    if isinstance(value, str) and _PROVISIONER_ERROR.fullmatch(value):
        return value
    return "provisioner_error"
