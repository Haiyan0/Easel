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
from urllib.parse import urlsplit

import httpx

from ..worker.config import run_input_digest, sign_run_snapshot
from ..worker.sessions import MAX_ATTACHMENTS, MAX_MESSAGE_BYTES
from .auth import AuthService
from .runtime import RuntimeUnavailable
from .sessions import SessionService
from .store import Store, TERMINAL_RUN_STATUSES
from .tasks import IMPLEMENTED_TASK_TYPES, entry_for_type
from .types import DomainError, Identity, RunRecord, WorkerHandle

_DEFAULT_SNAPSHOT_TTL = 1800
_ALLOWED_TASK_FIELDS = frozenset(
    {"sessionId", "turnId", "message", "attachments", "persona"})
_MAX_IDENTIFIER = 256
_MAX_PERSONA = 200


@dataclass(frozen=True)
class PinnedRunConfig:
    """接纳时固定的配置视图；由受管配置 provider 原子读取。

    ``revision`` 是 Run 固定的配置修订，不是 worker 容器的版本、能力覆盖
    版本或原生绑定契约版本。routes/agent 只包含该 Run 所需的最小内容。
    """

    revision: int
    routes: Mapping[str, object]
    agent: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")
        if not isinstance(self.routes, Mapping) or not isinstance(
                self.agent, Mapping):
            raise ValueError("routes/agent must be mappings")
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
                 snapshot_ttl: int = _DEFAULT_SNAPSHOT_TTL) -> None:
        if not isinstance(store, Store):
            raise TypeError("store must be a Store")
        if not isinstance(sessions, SessionService):
            raise TypeError("sessions must be a SessionService")
        if not isinstance(auth, AuthService):
            raise TypeError("auth must be an AuthService")
        if config_provider is not None and not callable(config_provider):
            raise TypeError("config_provider must be callable")
        if type(snapshot_ttl) is not int or not 1 <= snapshot_ttl <= 86400:
            raise ValueError("snapshot_ttl must be an integer in 1..86400")
        if type(request_lease_ttl) is not int or not 1 <= request_lease_ttl <= 3600:
            raise ValueError("request_lease_ttl must be an integer in 1..3600")
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
        self._snapshot_ttl = snapshot_ttl

    # ------------------------------------------------------------------
    # 依赖与数据库线程
    # ------------------------------------------------------------------

    async def _db(self, function, *args):
        if self._db_runner is None:
            return function(*args)
        return await self._db_runner(function, *args)

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

    def _sign(self, run: RunRecord, config: PinnedRunConfig,
              request: Mapping[str, object], task_type: str) -> str:
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
            routes=dict(config.routes),
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
        config = await self._pinned(actor)
        conversation_id = str(request["sessionId"])
        # 归属必须早于 worker 启动；reserve_run 在事务内会再次复查
        await self._db(self._sessions.get_conversation, actor, conversation_id)
        handle = await self._ensure_workspace_ready(
            actor.workspace_id, config.revision)
        client_request_id = request["turnId"] or uuid.uuid4().hex
        digest = run_input_digest(
            conversation_id=conversation_id, task_type=task_type,
            message=request["message"], attachments=request["attachments"],
            persona=request["persona"])

        run = await self._db(
            self._store.reserve_run, actor, conversation_id,
            client_request_id, digest, config.revision)
        if run.config_version != config.revision:
            # 同一 client_request_id 已按旧修订固定，不能用新配置重建
            raise DomainError("configuration_changed", 409)
        if (handle.workspace_id != run.workspace_id
                or handle.config_version != run.config_version):
            raise DomainError("worker_mismatch", 502)

        signed = self._sign(run, config, request, task_type)
        stored = await self._db(
            self._store.save_run_snapshot, actor, run.id, signed)
        envelope = self._stored_envelope(stored, run, task_type)

        state = await self._db(self._store.run_dispatch_state, actor, run.id)
        if state["dispatchState"] != "pending":
            return {"runId": run.id, "state": state["status"],
                    "duplicate": True}

        claimed = await self._db(
            self._store.claim_run_dispatch, actor, run.id,
            envelope["nonce"], handle.container_id)
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

    async def stop(self, actor: Identity, payload: object) -> dict:
        run = await self._owned_run(actor, self._require_run_id(payload))
        if run.status in TERMINAL_RUN_STATUSES:
            return {"runId": run.id, "state": run.status}
        self._require_capabilities()
        handle = await self._ensure_ready(run)
        response = await self._worker_request(
            handle, "POST", f"/internal/runs/{run.id}/stop",
            {"runId": run.id, "sessionId": run.conversation_id})
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
        handle = await self._ensure_ready(run)
        response = await self._worker_request(
            handle, "GET",
            f"/internal/runs/{run.id}/events?after={int(after)}", None)
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
        handle = await self._ensure_ready(run)
        response = await self._worker_request(
            handle, "POST",
            f"/internal/runs/{run.id}/questions/{question_id}/answer",
            {"runId": run.id, "questionId": question_id,
             "answers": dict(answers)})
        result = self._json_object(response)
        # 回答成功后 worker 已回到 running；控制库同步活跃状态
        await self._sync_worker_state(actor, run, "running")
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
        handle = await self._ensure_ready(run)
        response = await self._worker_request(
            handle, "POST",
            f"/internal/runs/{run.id}/questions/status",
            {"runId": run.id, "questionIds": list(question_ids)})
        return self._json_object(response)

    async def messages(self, actor: Identity, session_id: str, *,
                       limit: int = 100, offset: int = 0) -> dict:
        """读取本人 worker 的持久快照；无 Run 时为空历史，不启动执行。

        读取只回到 control 库定位最近一次 Run 对应的本人 worker，再把分页
        参数交给其内部快照路由；不会创建 Run、启动 prompt 或读取客户端给出
        的任意文件根。
        """
        conversation = await self._db(
            self._sessions.get_conversation, actor, session_id)
        self._require_capabilities()
        run = await self._db(
            self._store.latest_run_for_conversation, actor, conversation.id)
        if run is None:
            # 尚未接纳过任务：没有任何 worker 快照可读，也不为其启动容器。
            return {"sessionId": conversation.id, "messages": []}
        handle = await self._ensure_ready(run)
        response = await self._worker_request(
            handle, "GET",
            f"/internal/sessions/{conversation.id}/messages"
            f"?limit={int(limit)}&offset={int(offset)}", None)
        return self._json_object(response)

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
        """固定 WorkerHandle 重建内部头；不跟随重定向，不暴露响应体。"""
        url = self._upstream(handle) + path
        headers = {
            "X-Easel-Workspace-Id": handle.workspace_id,
            "X-Easel-Service-Token": handle.service_token,
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            request = self._http.build_request(
                method, url, headers=headers, json=payload)
            response = await self._http.send(request, follow_redirects=False)
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
