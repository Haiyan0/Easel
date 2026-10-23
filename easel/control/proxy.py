"""已认证 control 请求到本人 worker 的授权 relay。

边界：

* control 是唯一认证、资源归属与 ``Store.reserve_run`` 接纳入口；relay 只
  转发已分类的窄路由，不提供浏览器自选 upstream/workspace/run。
* 逐 Run 快照由 control 私钥签名，绑定 workspace/run/会话/输入摘要/任务
  类型/配置修订；worker 侧重新摘要后验签，不能替换内容后执行。
* 派发认领在控制库事务内完成：并发同请求只有一方 POST，重试读取原 Run 与
  原快照，不自动重发任务。
* 本机 SQLite 调用仍由 app 的专用数据库线程执行；网络等待不占用该线程。
"""
from __future__ import annotations

import json
import secrets
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import quote, urlsplit

import httpx

from ..worker.config import run_input_digest, sign_run_snapshot
from ..worker.guard import (SERVICE_TOKEN_HEADER, WORKSPACE_HEADER,
                            no_client_auth, strip_request_headers)
from ..worker.sessions import MAX_ATTACHMENTS, MAX_MESSAGE_BYTES
from .auth import AuthService
from .runtime import RuntimeUnavailable
from .sessions import SessionService
from .store import Store, TERMINAL_RUN_STATUSES
from .tasks import IMPLEMENTED_TASK_TYPES, entry_for_type
from .types import DomainError, Identity, RunRecord, WorkerHandle

_DEFAULT_SNAPSHOT_TTL = 1800
# 派发认领后给 POST 一个宽限期，避免对账抢在 worker 接纳前把结果判死。
_DISPATCH_GRACE_SECONDS = 60
# 任务截止（快照有效期/agent timeout）后的收尾余量。
_RECONCILE_GRACE_SECONDS = 60
# 唯一准备所有权租约；崩溃后由后台按原 Run 收尾，不接管重发。
_PREPARE_LEASE_SECONDS = 60
_ALLOWED_TASK_FIELDS = frozenset(
    {"sessionId", "turnId", "message", "attachments", "persona"})
_MAX_IDENTIFIER = 256
_MAX_PERSONA = 200


@dataclass(frozen=True)
class BrokerDestination:
    """逐 Run broker 目的地；凭证由 BrokerService.issue 在接纳后签发。"""

    url: str
    channel: str

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url:
            raise ValueError("broker url must be a non-empty string")
        parsed = urlsplit(self.url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise ValueError("broker url must be a plain http(s) origin")
        if (not isinstance(self.channel, str) or not self.channel
                or len(self.channel) > 32
                or not all(character.islower() or character.isdigit()
                           or character == "_" for character in self.channel)):
            raise ValueError("broker channel is invalid")
        object.__setattr__(self, "url", self.url.rstrip("/"))


@dataclass(frozen=True)
class PinnedRunConfig:
    """接纳时固定的配置视图；由受管配置 provider 原子读取。

    ``revision`` 是 Run 固定的配置修订，不是 worker 容器的版本、能力覆盖
    版本或原生绑定契约版本。routes/agent 只包含该 Run 所需的最小内容；
    ``broker`` 声明目的地/channel，实际 token 由接纳后的正式装配签发。
    """

    revision: int
    routes: Mapping[str, object]
    agent: Mapping[str, object]
    broker: BrokerDestination | None = None

    def __post_init__(self) -> None:
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")
        if not isinstance(self.routes, Mapping) or not isinstance(
                self.agent, Mapping):
            raise ValueError("routes/agent must be mappings")
        if self.broker is not None:
            if not isinstance(self.broker, BrokerDestination):
                raise ValueError("broker must be a BrokerDestination")
            if "broker_token" in self.routes or "broker_url" in self.routes:
                # 静态凭证与逐 Run 签发不能混用，避免旧 token 覆盖新签发
                raise ValueError("routes must not mix static broker credentials")
        object.__setattr__(self, "routes", MappingProxyType(dict(self.routes)))
        object.__setattr__(self, "agent", MappingProxyType(dict(self.agent)))


PinnedConfigProvider = Callable[[Identity], PinnedRunConfig]


def _text(value: object, *, maximum: int) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(character) < 32 for character in value)):
        raise DomainError("invalid_argument", 400)
    return value


def _optional_text(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, maximum=maximum)


def _attachments(value: object) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
        raise DomainError("invalid_argument", 400)
    result: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            raise DomainError("invalid_argument", 400)
        result.append(dict(item))
    return result


def _encoded_size(value: object) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        raise DomainError("invalid_argument", 400) from None


def _validate_task(payload: object, task_type: str) -> dict:
    """公开任务输入的严格白名单校验；浏览器不能注入内部凭证或目标。"""
    if not isinstance(payload, dict):
        raise DomainError("invalid_argument", 400)
    if set(payload) - _ALLOWED_TASK_FIELDS:
        raise DomainError("invalid_argument", 400)
    conversation_id = _text(payload.get("sessionId"),
                            maximum=_MAX_IDENTIFIER)
    message = payload.get("message")
    if not isinstance(message, str) or not message:
        raise DomainError("invalid_argument", 400)
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise DomainError("message_too_large", 413)
    turn_id = _optional_text(payload.get("turnId"),
                             maximum=_MAX_IDENTIFIER)
    persona = _optional_text(payload.get("persona"),
                             maximum=_MAX_PERSONA)
    attachments = _attachments(payload.get("attachments"))
    if _encoded_size({"message": message, "attachments": attachments,
                      "persona": persona}) > MAX_MESSAGE_BYTES:
        raise DomainError("message_too_large", 413)
    return {
        "sessionId": conversation_id,
        "turnId": turn_id,
        "message": message,
        "attachments": attachments,
        "persona": persona,
        "taskType": task_type,
    }


def _safe_code(value: object, fallback: str) -> str:
    if isinstance(value, str) and 1 <= len(value) <= 64 and all(
            character.islower() or character.isdigit() or character == "_"
            for character in value):
        return value
    return fallback


class WorkerRelay:
    """授权、接纳与转发；所有资源查找回到 control Store 重查归属。"""

    def __init__(self, runtime, sessions: SessionService, auth: AuthService, *,
                 store: Store, signing_key=None,
                 config_provider: PinnedConfigProvider | None = None,
                 http_client: httpx.AsyncClient | None = None,
                 broker=None, db_runner=None,
                 accepted_task_types: Iterable[str] | None = None,
                 request_lease_ttl: int = 60,
                 clock: Callable[[], float] = time.time,
                 system_clock: Callable[[], float] = time.time,
                 dispatch_grace: int = _DISPATCH_GRACE_SECONDS,
                 snapshot_ttl: int = _DEFAULT_SNAPSHOT_TTL) -> None:
        if not isinstance(store, Store):
            raise TypeError("store must be a Store")
        if not isinstance(sessions, SessionService):
            raise TypeError("sessions must be a SessionService")
        if not isinstance(auth, AuthService):
            raise TypeError("auth must be an AuthService")
        if config_provider is not None and not callable(config_provider):
            raise TypeError("config_provider must be callable")
        if not callable(system_clock):
            raise TypeError("system_clock must be callable")
        if type(snapshot_ttl) is not int or not 1 <= snapshot_ttl <= 86400:
            raise ValueError("snapshot_ttl must be an integer in 1..86400")
        if type(request_lease_ttl) is not int or not 1 <= request_lease_ttl <= 3600:
            raise ValueError("request_lease_ttl must be an integer in 1..3600")
        if type(dispatch_grace) is not int or not 0 <= dispatch_grace <= 3600:
            raise ValueError("dispatch_grace must be an integer in 0..3600")
        accepted = (IMPLEMENTED_TASK_TYPES if accepted_task_types is None
                    else frozenset(accepted_task_types))
        if any(entry_for_type(name) is None for name in accepted):
            raise ValueError("accepted_task_types contains unknown task type")
        self._runtime = runtime
        self._sessions = sessions
        self._auth = auth
        self._store = store
        self._signing_key = signing_key
        self._config_provider = config_provider
        self._http = http_client
        self._broker = broker
        self._db_runner = db_runner
        self._accepted_task_types = frozenset(accepted)
        self._request_lease_ttl = request_lease_ttl
        self._clock = clock
        # 对账时间必须与 Store 的 created_at/dispatched_at 同源（真实时钟）；
        # 签名时钟可被测试固定，但两者不得混用。
        self._system_clock = system_clock
        self._dispatch_grace = dispatch_grace
        self._snapshot_ttl = snapshot_ttl

    # ------------------------------------------------------------------
    # 依赖与数据库线程
    # ------------------------------------------------------------------

    async def _db(self, function, *args, **kwargs):
        if self._db_runner is None:
            return function(*args, **kwargs)
        return await self._db_runner(function, *args, **kwargs)

    def _require_capabilities(self) -> None:
        if (self._runtime is None or self._signing_key is None
                or self._config_provider is None or self._http is None):
            raise DomainError("capability_unavailable", 503)
        if not hasattr(self._signing_key, "sign"):
            raise DomainError("capability_unavailable", 503)

    async def _ensure_ready(self, run: RunRecord) -> WorkerHandle:
        try:
            handle = await self._runtime.ensure_ready(
                run.workspace_id, run.config_version)
        except RuntimeUnavailable as error:
            raise DomainError(error.code, 503) from None
        if not isinstance(handle, WorkerHandle):
            raise DomainError("worker_unavailable", 502)
        if (handle.workspace_id != run.workspace_id
                or handle.config_version != run.config_version):
            raise DomainError("worker_mismatch", 502)
        return handle

    async def _ensure_workspace_ready(self, workspace_id: str,
                                      revision: int) -> WorkerHandle:
        try:
            handle = await self._runtime.ensure_ready(workspace_id, revision)
        except RuntimeUnavailable as error:
            raise DomainError(error.code, 503) from None
        if (not isinstance(handle, WorkerHandle)
                or handle.workspace_id != workspace_id
                or handle.config_version != revision):
            raise DomainError("worker_mismatch", 502)
        return handle

    # ------------------------------------------------------------------
    # 接纳与派发
    # ------------------------------------------------------------------

    async def _pinned(self, actor: Identity) -> PinnedRunConfig:
        self._require_capabilities()
        config = await self._db(self._config_provider, actor)
        if not isinstance(config, PinnedRunConfig):
            raise DomainError("capability_unavailable", 503)
        return config

    async def _ensure_broker_capability(self, actor: Identity,
                                        config: PinnedRunConfig) -> None:
        if config.broker is None:
            return
        if self._broker is None:
            raise DomainError("capability_unavailable", 503)
        try:
            await self._db(
                self._broker.ensure_issuable, actor, config.broker.channel)
        except DomainError as error:
            if error.status in (400, 404, 503):
                raise DomainError("capability_unavailable", 503) from None
            raise

    async def _run_routes(self, actor: Identity, run: RunRecord,
                          config: PinnedRunConfig) -> Mapping[str, object]:
        """固定该 Run 的最小 broker 路由。

        正式装配在接纳后调用 ``BrokerService.issue``，token 只绑定本人的
        workspace/run/channel；签发失败（未配置该 channel 等）明确 503，
        不以空 routes 接纳一个看似可运行的在线任务。签发与配置快照在同一
        数据库事务内完成，重试/重启复用原签名包，不会换 token。
        """
        if config.broker is None:
            return config.routes
        if self._broker is None:
            raise DomainError("capability_unavailable", 503)
        try:
            token = await self._db(
                self._broker.issue, actor, run.id, config.broker.channel)
        except DomainError as error:
            if error.status in (400, 404, 503):
                raise DomainError("capability_unavailable", 503) from None
            raise
        return {**config.routes,
                "broker_url": config.broker.url,
                "broker_token": token}

    async def _duplicate(self, actor: Identity, run: RunRecord) -> dict:
        state = await self._db(
            self._store.run_dispatch_state, actor, run.id)
        return {"runId": run.id, "state": state["status"],
                "duplicate": True}

    def _sign(self, run: RunRecord, config: PinnedRunConfig,
              request: Mapping[str, object], task_type: str,
              routes: Mapping[str, object] | None = None) -> str:
        digest = run_input_digest(
            conversation_id=run.conversation_id, task_type=task_type,
            message=str(request["message"]),
            attachments=list(request["attachments"]),
            persona=request.get("persona"))
        envelope = sign_run_snapshot(
            self._signing_key,
            workspace_id=run.workspace_id,
            run_id=run.id,
            conversation_id=run.conversation_id,
            input_digest=digest,
            task_type=task_type,
            revision=run.config_version,
            routes=dict(config.routes if routes is None else routes),
            agent=dict(config.agent),
            expires_at=int(self._clock()) + self._snapshot_ttl,
            nonce=secrets.token_urlsafe(24))
        return json.dumps(envelope, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _stored_envelope(text: str, run: RunRecord, task_type: str) -> dict:
        """复核持久快照没有指向别的 Run/会话；持久内容不可被并发替换。"""
        try:
            envelope = json.loads(text)
        except (TypeError, ValueError):
            raise DomainError("snapshot_invalid", 500) from None
        if (not isinstance(envelope, dict)
                or envelope.get("workspace_id") != run.workspace_id
                or envelope.get("run_id") != run.id
                or envelope.get("conversation_id") != run.conversation_id
                or envelope.get("task_type") != task_type
                or envelope.get("revision") != run.config_version):
            raise DomainError("snapshot_invalid", 500)
        return envelope

    async def submit(self, actor: Identity, payload: object) -> dict:
        """对话任务的统一入口；实际接纳走 :meth:`submit_task`。"""
        return await self.submit_task(actor, "chat", payload)

    async def submit_task(self, actor: Identity, task_type: str,
                          payload: object) -> dict:
        """校验 → 归属 → worker 就绪 → 接纳 → 固定签名快照 → 原子派发。

        只有 ``accepted_task_types`` 中且装配了业务适配器的类型可以进入账本；
        未装配类型在保留任何容量、启动 worker 之前明确 503。worker 就绪检查
        早于 ``reserve_run``，drain 期间的新任务不会留下残留接纳。
        """
        entry = entry_for_type(task_type)
        if entry is None:
            raise DomainError("invalid_argument", 400)
        if task_type not in self._accepted_task_types:
            raise DomainError("capability_unavailable", 503)
        request = _validate_task(payload, task_type)
        conversation_id = str(request["sessionId"])
        # 归属必须早于任何配置读取或 worker 启动；reserve_run 会再复查
        await self._db(self._sessions.get_conversation, actor, conversation_id)
        client_request_id = request["turnId"] or uuid.uuid4().hex
        digest = run_input_digest(
            conversation_id=conversation_id, task_type=task_type,
            message=request["message"], attachments=request["attachments"],
            persona=request["persona"])

        # 幂等识别先于读取最新配置：同输入重试返回原 Run/原快照，管理员
        # 更新配置或新 worker 不可用都不能改变已固定的接纳。
        existing = await self._db(
            self._store.lookup_run, actor, conversation_id,
            client_request_id, digest)
        if existing is not None:
            return await self._duplicate(actor, existing)

        config = await self._pinned(actor)
        # 能力检查先于启动 worker 与占用容量：缺受管 broker 路由时明确
        # 503，不留下一个永远无法派发的 reserved Run。
        await self._ensure_broker_capability(actor, config)
        handle = await self._ensure_workspace_ready(
            actor.workspace_id, config.revision)
        run = await self._db(
            self._store.reserve_run, actor, conversation_id,
            client_request_id, digest, config.revision)
        if (handle.workspace_id != run.workspace_id
                or handle.config_version != run.config_version):
            raise DomainError("worker_mismatch", 502)

        # 并发同请求：reserve_run 可能返回另一方刚创建的 Run。准备所有权
        # 在签发/固定快照之前原子认领；非所有者只读原 admission，既不铸造
        # 第二个 token，也不能补偿性地中断所有者已派发的 Run。
        prepare_owner = secrets.token_urlsafe(24)
        acquired = await self._db(
            self._store.claim_run_preparation, actor, run.id,
            prepare_owner, int(self._system_clock()),
            _PREPARE_LEASE_SECONDS)
        if not acquired:
            return await self._duplicate(actor, run)
        stored_existing = await self._db(
            self._store.get_run_snapshot, actor, run.id)
        if stored_existing is not None:
            return await self._duplicate(actor, run)
        if run.config_version != config.revision:
            return await self._duplicate(actor, run)

        try:
            routes = await self._run_routes(actor, run, config)
        except DomainError:
            # 只有准备所有者、且尚未派发时才能补偿；非所有者不会走到这里
            await self._converge(actor, run, "interrupted")
            raise
        signed = self._sign(run, config, request, task_type, routes)
        stored = await self._db(
            self._store.save_run_snapshot, actor, run.id, signed)
        if stored != signed:
            # 已有另一份固定快照：只读原 admission，不中断其执行
            return await self._duplicate(actor, run)
        envelope = self._stored_envelope(stored, run, task_type)

        claimed = await self._db(
            self._store.claim_run_dispatch, actor, run.id,
            envelope["nonce"], handle.container_id,
            prepare_owner=prepare_owner,
            now=int(self._system_clock()))
        if not claimed:
            state = await self._db(
                self._store.run_dispatch_state, actor, run.id)
            return {"runId": run.id, "state": state["status"],
                    "duplicate": True}

        body = {
            "envelope": envelope,
            "request": {
                "sessionId": request["sessionId"],
                "turnId": request["turnId"],
                "message": request["message"],
                "attachments": request["attachments"],
                "persona": request["persona"],
                "taskType": request["taskType"],
                "inputDigest": envelope["input_digest"],
            },
        }
        response = await self._worker_request(
            handle, "POST", f"/internal/runs/{run.id}", body)
        try:
            result = response.json()
        except ValueError:
            result = {}
        if not isinstance(result, dict):
            result = {}
        await self._db(self._store.mark_run_dispatched, actor, run.id,
                       envelope["nonce"])
        try:
            await self._db(self._store.mark_running, actor, run.id)
        except DomainError:
            pass
        return {"runId": run.id,
                "state": _safe_code(result.get("state"), "accepted"),
                "duplicate": False}

    # ------------------------------------------------------------------
    # 停止、事件与问题
    # ------------------------------------------------------------------

    def _require_run_id(self, payload: object) -> str:
        if not isinstance(payload, dict):
            raise DomainError("invalid_argument", 400)
        return _text(payload.get("runId"), maximum=128)

    async def _owned_run(self, actor: Identity, run_id: str) -> RunRecord:
        """先回 Store 重查归属；外空间/未知 Run 在调用 worker 前 404。"""
        return await self._db(self._sessions.assert_run, actor, run_id)

    async def _sync_worker_state(self, actor: Identity, run: RunRecord,
                                 state: object) -> None:
        """把 worker 的持久状态写回控制库。

        控制库已是终态时不回退；真正终态统一走 ``finish_run``（容量只释放
        一次）并撤销该 Run 的临时 broker 令牌。撤销是幂等的，重复同步安全。
        """
        if state in ("accepted", "reserved", "running"):
            try:
                await self._db(self._store.mark_running, actor, run.id)
            except DomainError:
                pass
            return
        if state == "awaiting_permission":
            try:
                await self._db(
                    self._store.mark_awaiting_permission, actor, run.id)
            except DomainError:
                pass
            return
        if state in TERMINAL_RUN_STATUSES:
            await self._db(
                self._store.finish_run, actor, run.id, str(state))
            if self._broker is not None:
                await self._db(
                    self._broker.revoke_run, run.id, run.workspace_id)

    async def _binding(self, actor: Identity, run: RunRecord) -> dict:
        return await self._db(
            self._store.run_dispatch_state, actor, run.id)

    async def _existing_handle(self, run: RunRecord,
                               binding: Mapping[str, object]
                               ) -> WorkerHandle | None:
        """按持久 worker 实例 ID 取句柄；draining 仍允许既有 Run 收尾。

        不调用 ``ensure_ready``：既有 Run 不能启动、升级或替换实例，也
        不能在原实例失效后把操作发到另一代次伪装成原 Run。
        """
        container = binding.get("workerContainerId")
        if not isinstance(container, str) or not container:
            return None
        handle = await self._lookup_existing(
            run.workspace_id, container, run.config_version)
        if handle is None:
            raise DomainError("worker_instance_gone", 409)
        return handle

    async def _lookup_existing(self, workspace_id: str, container: str,
                               config_version: int) -> WorkerHandle | None:
        """按不可复用实例 ID 取句柄；runtime 可提供更强的存活核对。

        ``RuntimeManager.bound_handle`` 以持久租约/代次实现；自定义 runtime
        可通过同名方法声明实例存活。两条路径都绝不调用 ``ensure_ready``，
        因此不会启动、升级或替换实例。
        """
        lookup = getattr(self._runtime, "bound_handle", None)
        if callable(lookup):
            handle = await lookup(
                workspace_id, container, config_version)
            if (not isinstance(handle, WorkerHandle)
                    or handle.workspace_id != workspace_id
                    or handle.container_id != container
                    or handle.config_version != config_version):
                return None
            return handle
        lease = await self._db(self._store.get_worker_lease, workspace_id)
        if (lease is None or lease.container_id != container
                or lease.state not in ("ready", "draining")
                or lease.config_version != config_version
                or not lease.upstream or not lease.service_token):
            return None
        return WorkerHandle(workspace_id, lease.container_id,
                            lease.upstream, lease.service_token,
                            config_version)

    async def _converge(self, actor: Identity, run: RunRecord,
                        state: str) -> None:
        await self._db(self._store.finish_run, actor, run.id, state)
        await self._revoke_run_token(run)

    async def _revoke_run_token(self, run: RunRecord) -> None:
        if self._broker is not None:
            await self._db(self._broker.revoke_run, run.id, run.workspace_id)

    @staticmethod
    def _local_page(run: RunRecord) -> dict:
        return {"runId": run.id, "state": run.status,
                "events": [], "lastEventId": 0}

    async def stop(self, actor: Identity, payload: object) -> dict:
        run = await self._owned_run(actor, self._require_run_id(payload))
        if run.status in TERMINAL_RUN_STATUSES:
            return {"runId": run.id, "state": run.status}
        self._require_capabilities()
        binding = await self._binding(actor, run)
        if not binding.get("workerContainerId"):
            # 从未派发：没有外部执行，直接本地终态并撤销预留凭证
            await self._converge(actor, run, "cancelled")
            return {"runId": run.id, "state": "cancelled"}
        try:
            handle = await self._existing_handle(run, binding)
        except DomainError as error:
            if error.code != "worker_instance_gone":
                raise
            if not await self._confirm_instance_stopped(
                    run.workspace_id,
                    str(binding.get("workerContainerId") or "")):
                raise DomainError("worker_instance_unconfirmed", 503) \
                    from None
            await self._converge(actor, run, "interrupted")
            return {"runId": run.id, "state": "interrupted"}
        try:
            response = await self._worker_request(
                handle, "POST", f"/internal/runs/{run.id}/stop",
                {"runId": run.id, "sessionId": run.conversation_id})
        except DomainError as error:
            if error.code != "not_found":
                raise
            await self._converge(actor, run, "interrupted")
            return {"runId": run.id, "state": "interrupted"}
        result = self._json_object(response)
        state = _safe_code(result.get("state"), "cancelled")
        await self._sync_worker_state(actor, run, state)
        return {"runId": run.id, "state": state}

    async def events(self, actor: Identity, run_id: str, *,
                     after: int = 0) -> dict:
        if type(after) is not int or after < 0:
            raise DomainError("invalid_argument", 400)
        run = await self._owned_run(actor, _text(run_id, maximum=128))
        self._require_capabilities()
        binding = await self._binding(actor, run)
        try:
            handle = await self._existing_handle(run, binding)
        except DomainError as error:
            if error.code != "worker_instance_gone":
                raise
            if not await self._confirm_instance_stopped(
                    run.workspace_id,
                    str(binding.get("workerContainerId") or "")):
                raise DomainError("worker_instance_unconfirmed", 503) \
                    from None
            await self._converge(actor, run, "interrupted")
            return self._local_page(await self._owned_run(actor, run.id))
        if handle is None:
            return self._local_page(run)
        try:
            response = await self._worker_request(
                handle, "GET",
                f"/internal/runs/{run.id}/events?after={int(after)}", None)
        except DomainError as error:
            if error.code != "not_found":
                raise
            await self._converge(actor, run, "interrupted")
            return self._local_page(await self._owned_run(actor, run.id))
        result = self._json_object(response)
        await self._sync_worker_state(actor, run, result.get("state"))
        return result

    async def answer(self, actor: Identity, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise DomainError("invalid_argument", 400)
        run = await self._owned_run(actor, self._require_run_id(payload))
        question_id = _text(payload.get("questionId"), maximum=256)
        answers = payload.get("answers")
        if not isinstance(answers, dict):
            raise DomainError("invalid_argument", 400)
        self._require_capabilities()
        binding = await self._binding(actor, run)
        try:
            handle = await self._existing_handle(run, binding)
        except DomainError as error:
            if error.code != "worker_instance_gone":
                raise
            if not await self._confirm_instance_stopped(
                    run.workspace_id,
                    str(binding.get("workerContainerId") or "")):
                raise DomainError("worker_instance_unconfirmed", 503) \
                    from None
            await self._converge(actor, run, "interrupted")
            raise DomainError("run_interrupted", 409) from None
        if handle is None:
            raise DomainError("run_not_dispatched", 409)
        try:
            response = await self._worker_request(
                handle, "POST",
                f"/internal/runs/{run.id}/questions/{question_id}/answer",
                {"runId": run.id, "questionId": question_id,
                 "answers": dict(answers)})
        except DomainError as error:
            if error.code != "not_found":
                raise
            await self._converge(actor, run, "interrupted")
            raise DomainError("run_interrupted", 409) from None
        result = self._json_object(response)
        # 同步 worker 的真实状态：仍有其它未决问题时保持 awaiting_permission
        await self._sync_worker_state(
            actor, run, result.get("state", "running"))
        return result

    async def question_status(self, actor: Identity, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise DomainError("invalid_argument", 400)
        run = await self._owned_run(actor, self._require_run_id(payload))
        question_ids = payload.get("questionIds")
        if (not isinstance(question_ids, list)
                or len(question_ids) > 100
                or any(not isinstance(item, str) or not item
                       or len(item) > 256 for item in question_ids)):
            raise DomainError("invalid_argument", 400)
        self._require_capabilities()
        binding = await self._binding(actor, run)
        try:
            handle = await self._existing_handle(run, binding)
        except DomainError as error:
            if error.code != "worker_instance_gone":
                raise
            if not await self._confirm_instance_stopped(
                    run.workspace_id,
                    str(binding.get("workerContainerId") or "")):
                raise DomainError("worker_instance_unconfirmed", 503) \
                    from None
            await self._converge(actor, run, "interrupted")
            raise DomainError("run_interrupted", 409) from None
        if handle is None:
            raise DomainError("run_not_dispatched", 409)
        response = await self._worker_request(
            handle, "POST",
            f"/internal/runs/{run.id}/questions/status",
            {"runId": run.id, "questionIds": list(question_ids)})
        return self._json_object(response)

    async def messages(self, actor: Identity, session_id: str, *,
                       limit: int = 100, offset: int = 0) -> dict:
        """读取本人 worker 的持久快照；无 Run 时为空历史，不启动执行。

        读取只回到 control 库定位最近一次已派发 Run 对应的本人 worker，再把
        分页参数交给其内部快照路由；不会创建 Run、启动 prompt 或读取客户端
        给出的任意文件根。draining 期间按原实例读取，不触发新容器。
        """
        conversation = await self._db(
            self._sessions.get_conversation, actor, session_id)
        self._require_capabilities()
        run = await self._db(
            self._store.latest_dispatched_run_for_conversation,
            actor, conversation.id)
        if run is None:
            # 兼容早期未记录实例 ID 的 Run：仍定位最近 Run 读取
            run = await self._db(
                self._store.latest_run_for_conversation,
                actor, conversation.id)
            if run is None:
                return {"sessionId": conversation.id, "messages": []}
            handle = await self._ensure_ready(run)
        else:
            binding = await self._binding(actor, run)
            try:
                handle = await self._existing_handle(run, binding)
            except DomainError as error:
                if error.code != "worker_instance_gone":
                    raise
                raise DomainError("worker_unavailable", 502) from None
            if handle is None:
                return {"sessionId": conversation.id, "messages": []}
        response = await self._worker_request(
            handle, "GET",
            f"/internal/sessions/{conversation.id}/messages"
            f"?limit={int(limit)}&offset={int(offset)}", None)
        return self._json_object(response)

    # ------------------------------------------------------------------
    # 后台持久对账（与 SSE/登录状态无关）
    # ------------------------------------------------------------------

    async def reconcile_once(self) -> dict:
        """核对所有未终态 Run；完成、崩溃与派发未知都有明确收敛。

        不依赖浏览器 events/stop、Cookie 或用户在线状态；control 重启后
        从控制库继续。派发中的 Run 先等认领宽限期，再按持久 nonce 与不可
        复用 worker 实例 ID 查询；从不自动重发任务 POST。
        """
        rows = await self._db(self._store.list_active_run_bindings)
        finished = 0
        for row in rows:
            if await self._reconcile_run(row):
                finished += 1
        return {"checked": len(rows), "finished": finished}

    async def _reconcile_run(self, row: Mapping[str, object]) -> bool:
        now = int(self._system_clock())
        workspace_id = str(row["workspaceId"])
        run_id = str(row["runId"])
        container = str(row.get("workerContainerId") or "")
        dispatch_state = str(row.get("dispatchState") or "")
        created_at = int(row.get("createdAt") or now)
        if not container:
            # pending：从未认领派发。准备所有权持有者崩溃后，只能用持久
            # 租约到期 + 宽限期判断“准备未完成”，再由后台中断；绝不把
            # 同一 Run 交给另一请求重新签发/重发。
            prepare_expires = row.get("prepareExpiresAt")
            if (isinstance(prepare_expires, int)
                    and now < prepare_expires + self._dispatch_grace):
                return False
            if (dispatch_state == "pending"
                    and now - created_at < self._dispatch_grace):
                return False
            # 扫描到的 pending 行可能已被合法准备/派发；收尾必须在同一
            # 写事务内比较全部观察前提，CAS 不匹配就放弃本次旧判断。
            interrupted = await self._db(
                self._store.interrupt_unstarted_run, workspace_id, run_id,
                expected_status=str(row.get("status") or ""),
                expected_dispatch_state=dispatch_state,
                expected_container=row.get("workerContainerId"),
                expected_prepare_owner=row.get("prepareOwner"),
                expected_prepare_expires_at=row.get("prepareExpiresAt"))
            return bool(interrupted)
        dispatched_at = row.get("dispatchedAt")
        if (dispatch_state == "dispatching"
                and isinstance(dispatched_at, int)
                and now - dispatched_at < self._dispatch_grace):
            # 派发结果未知：先等本次 POST 的宽限期，不抢在接纳前查询
            return False
        handle = await self._lookup_existing(
            workspace_id, container, int(row["configVersion"]))
        if handle is None:
            # 句柄不可用不是“执行已结束”的证据；只有按原实例 ID 确认停止
            # （provisioner 幂等 stop 成功）才允许中断并释放容量。
            if await self._confirm_instance_stopped(workspace_id, container):
                await self._finish_system(
                    workspace_id, run_id, "interrupted")
                return True
            return False
        deadline = self._run_deadline(row)
        timed_out = now >= deadline
        if timed_out:
            # 截止后先撤销凭证限制新的模型调用，但仍保留任务预算直到
            # 停止被确认；撤销本身不能证明外部副作用已结束。
            await self._revoke_token_system(workspace_id, run_id)
        nonce = str(row.get("dispatchNonce") or "")
        suffix = f"&nonce={quote(nonce, safe='')}" if nonce else ""
        try:
            response = await self._worker_request(
                handle, "GET",
                f"/internal/runs/{run_id}/events?after=0{suffix}", None)
        except DomainError as error:
            if error.code == "not_found":
                # 同一原实例明确没有该 Run：派发未落地，安全收敛
                await self._finish_system(
                    workspace_id, run_id, "interrupted")
                return True
            if not timed_out:
                return False
            return await self._confirm_stop(
                workspace_id, run_id, handle)
        result = self._json_object(response)
        state = result.get("state")
        if state in TERMINAL_RUN_STATUSES:
            await self._finish_system(
                workspace_id, run_id, str(state))
            return True
        if state in ("accepted", "reserved", "running"):
            await self._sync_system_status(workspace_id, run_id, "running")
        elif state == "awaiting_permission":
            await self._sync_system_status(
                workspace_id, run_id, "awaiting_permission")
        if not timed_out:
            return False
        return await self._confirm_stop(workspace_id, run_id, handle)

    async def _confirm_stop(self, workspace_id: str, run_id: str,
                            handle: WorkerHandle) -> bool:
        """请求原实例停止并等待可验证终态；失败/非终态保持占用。"""
        try:
            response = await self._worker_request(
                handle, "POST", f"/internal/runs/{run_id}/stop",
                {"runId": run_id})
        except DomainError:
            return False
        result = self._json_object(response)
        state = result.get("state")
        if state in TERMINAL_RUN_STATUSES:
            await self._finish_system(
                workspace_id, run_id, str(state))
            return True
        return False

    async def _confirm_instance_stopped(self, workspace_id: str,
                                        container_id: str) -> bool:
        confirm = getattr(self._runtime, "confirm_instance_stopped", None)
        if not callable(confirm):
            return False
        try:
            return bool(await confirm(workspace_id, container_id))
        except Exception:
            return False

    async def _revoke_token_system(self, workspace_id: str,
                                   run_id: str) -> None:
        if self._broker is None:
            return
        try:
            await self._db(self._broker.revoke_run, run_id, workspace_id)
        except DomainError:
            pass

    async def _sync_system_status(self, workspace_id: str, run_id: str,
                                  status: str) -> None:
        try:
            await self._db(self._store.sync_run_status,
                           workspace_id, run_id, status)
        except DomainError:
            pass

    async def _finish_system(self, workspace_id: str, run_id: str,
                             status: str) -> None:
        """后台终态：容量只释放一次，并撤销该 Run 的全部 broker token。"""
        try:
            await self._db(self._store.sync_run_status,
                           workspace_id, run_id, status)
        except DomainError:
            pass
        if self._broker is not None:
            try:
                await self._db(
                    self._broker.revoke_run, run_id, workspace_id)
            except DomainError:
                pass

    @staticmethod
    def _run_deadline(row: Mapping[str, object]) -> int:
        """任务截止：快照有效期与 agent timeout 的较大者加收尾余量。"""
        created = int(row.get("createdAt") or 0)
        expires = 0
        timeout = 0
        raw = row.get("snapshotJson")
        if isinstance(raw, str):
            try:
                envelope = json.loads(raw)
                if isinstance(envelope, dict):
                    value = envelope.get("expires_at")
                    if type(value) is int:
                        expires = value
                    agent = envelope.get("agent")
                    if isinstance(agent, Mapping):
                        budget = agent.get("timeout")
                        if type(budget) is int:
                            timeout = budget
            except ValueError:
                pass
        return max(expires, created + timeout) + _RECONCILE_GRACE_SECONDS

    # ------------------------------------------------------------------
    # worker HTTP
    # ------------------------------------------------------------------

    @staticmethod
    def _upstream(handle: WorkerHandle) -> str:
        value = handle.upstream.rstrip("/")
        if not isinstance(value, str) or not value:
            raise DomainError("worker_unavailable", 502)
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise DomainError("worker_unavailable", 502)
        return value

    async def _worker_request(self, handle: WorkerHandle, method: str,
                              path: str, payload: object) -> httpx.Response:
        """在途短请求持有 workspace 租约；无论成败都在 finally 释放。

        租约只用于空闲回收判定：请求进行中不得回收该 worker。释放失败时
        保留至 TTL 过期，不覆盖原始异常，也不永久阻塞后续回收。
        """
        lease_id = await self._db(
            self._store.begin_worker_request_lease,
            handle.workspace_id, self._request_lease_ttl)
        try:
            return await self._send(handle, method, path, payload)
        finally:
            try:
                await self._db(
                    self._store.end_worker_request_lease, lease_id)
            except Exception:
                pass

    async def _send(self, handle: WorkerHandle, method: str,
                    path: str, payload: object) -> httpx.Response:
        """固定 WorkerHandle 重建内部身份；先清洗最终合并的 Request。

        仅自行构造 headers 不够：``AsyncClient.build_request/send`` 会合并
        client 默认 headers、cookie jar 与自动 auth。这里在最终 Request 上
        删除 Cookie/Authorization/Forwarded/X-Easel-* 等外来凭证，再用无操作
        auth 覆盖默认认证，然后从服务端句柄重建内部头。不跟随重定向，也不
        修改共享 client 的 headers/cookies/auth。
        """
        url = self._upstream(handle) + path
        headers = {
            WORKSPACE_HEADER: handle.workspace_id,
            SERVICE_TOKEN_HEADER: handle.service_token,
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            request = self._http.build_request(
                method, url, headers=headers, json=payload)
            strip_request_headers(request.headers)
            request.headers[WORKSPACE_HEADER] = handle.workspace_id
            request.headers[SERVICE_TOKEN_HEADER] = handle.service_token
            response = await self._http.send(
                request, auth=no_client_auth(), follow_redirects=False)
        except httpx.HTTPError:
            raise DomainError("upstream_unavailable", 502) from None
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise DomainError("upstream_redirect", 502)
        if response.status_code >= 400:
            code = "upstream_unavailable"
            status = 502
            if response.status_code == 404:
                code, status = "not_found", 404
            elif response.status_code in (400, 413, 415, 422):
                code, status = "invalid_request", 400
            elif response.status_code in (401, 403):
                code, status = "worker_rejected", 502
            elif response.status_code == 409:
                code, status = "conflict", 409
            await response.aclose()
            raise DomainError(code, status)
        return response

    @staticmethod
    def _json_object(response: httpx.Response) -> dict:
        try:
            value = response.json()
        except ValueError:
            value = None
        if not isinstance(value, dict):
            raise DomainError("upstream_invalid", 502)
        return value
