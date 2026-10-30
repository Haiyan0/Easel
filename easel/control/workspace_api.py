"""Task 5A：control 对本人基础工作区的受限转发。

本模块只做三件事：

* 按已认证 Identity 选择 workspace/config revision，绝不从 URL/body 选目标；
* 经既有 RuntimeManager 取得固定实例，通过 GatewayClient 逐请求签名；
* 短请求登记 ``request_leases``，流式响应在关闭/断开时释放，防止下载中
  被空闲回收。

路径值在这里再次规范化；真正的文件边界仍由 worker 的既有路径工具复核。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from urllib.parse import quote

from .gateway_client import GatewayClient
from .runtime import RuntimeUnavailable
from .store import Store
from .types import DomainError, Identity, WorkerHandle, WorkerLease

_OUTPUT_INTERNAL = frozenset({"analytics", "wechat"})
_MAX_UPLOAD_BYTES = 60 * 1024 * 1024
_MAX_PERSONA = 200
_MAX_PATH = 2048


def canonical_persona_name(value: object) -> str:
    """校验画像名并返回规范百分号编码；不接受路径或多段。"""
    if (not isinstance(value, str) or not value
            or len(value) > _MAX_PERSONA
            or value != value.strip()
            or value.startswith((".", "_"))
            or any(character in value for character in "/\\:\x00\r\n")
            or any(ord(character) < 32 or ord(character) == 127
                   for character in value)):
        raise DomainError("invalid_request", 400)
    return quote(value, safe="-._~", encoding="utf-8", errors="strict")


def canonical_output_path(value: object) -> str:
    """校验 outputs 相对路径并返回逐段规范编码。

    拒绝绝对路径、反斜杠、点段、隐藏首段、内部系统目录与空段；不解析
    symlink/junction——那必须由 worker 在本人根目录内复核。
    """
    if (not isinstance(value, str) or not value or len(value) > _MAX_PATH
            or value.startswith(("/", "\\"))
            or any(character in value for character in "\\:\x00\r\n")
            or any(ord(character) < 32 or ord(character) == 127
                   for character in value)):
        raise DomainError("invalid_request", 400)
    parts = value.split("/")
    if any(not part or part in (".", "..") or part.startswith(".")
           for part in parts):
        raise DomainError("invalid_request", 400)
    if parts[0].startswith("_") or parts[0] in _OUTPUT_INTERNAL:
        raise DomainError("invalid_request", 400)
    return "/".join(
        quote(part, safe="-._~", encoding="utf-8", errors="strict")
        for part in parts)


class WorkspaceStream:
    """一个持有 request lease 的上游流；关闭或断开时统一收尾。"""

    def __init__(self, response, lease_id: str, store: Store, db_runner):
        self.status_code = response.status_code
        self.headers = {
            str(key).lower(): str(value)
            for key, value in response.headers.items()
        }
        self._response = response
        self._lease_id = lease_id
        self._store = store
        self._db_runner = db_runner
        self._cleanup: asyncio.Task | None = None

    async def _db(self, function, *args):
        if self._db_runner is None:
            return function(*args)
        return await self._db_runner(function, *args)

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._response.aiter_raw():
                yield chunk
        finally:
            await self.aclose()

    async def _run_cleanup(self) -> None:
        try:
            await self._response.aclose()
        finally:
            try:
                await self._db(
                    self._store.end_worker_request_lease, self._lease_id)
            except Exception:
                # 释放失败保留到 TTL 过期，不覆盖原始读取错误。
                pass

    def _start_cleanup(self) -> asyncio.Task:
        if self._cleanup is None:
            self._cleanup = asyncio.create_task(self._run_cleanup())
        return self._cleanup

    async def aclose(self) -> None:
        task = self._start_cleanup()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # 客户端断开时 cleanup 仍作为独立任务完成，lease 不残留。
            raise


class WorkspaceApi:
    def __init__(self, runtime, store: Store, gateway: GatewayClient | None, *,
                 revision: int, db_runner=None, gateway_required: bool = True,
                 request_lease_ttl: int = 600,
                 max_upload_bytes: int = _MAX_UPLOAD_BYTES):
        self._runtime = runtime
        self._store = store
        self._gateway = gateway
        self._default_revision = int(revision)
        self._db_runner = db_runner
        self._gateway_required = bool(gateway_required)
        self._request_lease_ttl = int(request_lease_ttl)
        self._max_upload_bytes = int(max_upload_bytes)

    async def _db(self, function, *args):
        if self._db_runner is None:
            return function(*args)
        return await self._db_runner(function, *args)

    async def _revision(self, workspace_id: str) -> int:
        versions = await self._db(self._store.workspace_config_versions)
        revision = versions.get(workspace_id)
        if type(revision) is not int or revision < 0:
            raise DomainError("workspace_unavailable", 404)
        return revision

    def _require_gateway(self) -> GatewayClient:
        if self._gateway is None or self._runtime is None:
            raise DomainError("capability_unavailable", 503)
        return self._gateway

    def _valid_handle(self, handle: object, workspace_id: str,
                      revision: int) -> WorkerHandle:
        if (not isinstance(handle, WorkerHandle)
                or handle.workspace_id != workspace_id
                or handle.config_version != revision):
            raise DomainError("worker_mismatch", 502)
        if self._gateway_required and (
                not handle.gateway_instance_id
                or not handle.gateway_generation
                or not handle.gateway_upstream
                or handle.gateway_state != "available"):
            raise DomainError("worker_gateway_unavailable", 503)
        return handle

    async def _ensure_handle(self, actor: Identity) -> WorkerHandle:
        gateway = self._require_gateway()
        del gateway
        revision = await self._revision(actor.workspace_id)
        try:
            handle = await self._runtime.ensure_ready(
                actor.workspace_id, revision)
        except RuntimeUnavailable as error:
            raise DomainError(error.code, 503,
                              retry_after=error.retry_after) from None
        return self._valid_handle(handle, actor.workspace_id, revision)

    def _handle_from_lease(self, lease: WorkerLease,
                           revision: int) -> WorkerHandle | None:
        if (lease is None or lease.state not in ("ready", "draining")
                or lease.config_version != revision
                or not lease.container_id or not lease.upstream
                or not lease.service_token):
            return None
        return self._valid_handle(
            WorkerHandle(
                lease.workspace_id, lease.container_id, lease.upstream,
                lease.service_token, lease.config_version,
                gateway_instance_id=lease.gateway_instance_id,
                gateway_generation=lease.gateway_generation,
                gateway_upstream=lease.gateway_upstream,
                gateway_state=lease.gateway_state),
            lease.workspace_id, revision)

    async def _request_with_handle(
            self, handle: WorkerHandle, method: str, path: str, *,
            query=None, payload=None, body=None, content_type=None,
            headers=None, stream: bool = False,
            lease_ttl: int | None = None):
        gateway = self._require_gateway()
        lease_id = await self._db(
            self._store.begin_worker_request_lease,
            handle.workspace_id,
            self._request_lease_ttl if lease_ttl is None else lease_ttl)
        try:
            response = await gateway.request(
                handle, method, path, query=query, payload=payload,
                body=body, content_type=content_type, headers=headers,
                stream=stream, status_mode="workspace")
        except BaseException:
            try:
                await self._db(
                    self._store.end_worker_request_lease, lease_id)
            except Exception:
                pass
            raise
        if stream:
            return response, lease_id
        try:
            await self._db(
                self._store.end_worker_request_lease, lease_id)
        except Exception:
            pass
        return response

    async def request(self, actor: Identity, method: str, path: str, *,
                      query=None, payload=None, body=None,
                      content_type=None, headers=None):
        handle = await self._ensure_handle(actor)
        return await self._request_with_handle(
            handle, method, path, query=query, payload=payload, body=body,
            content_type=content_type, headers=headers)

    async def open_stream(self, actor: Identity, method: str, path: str, *,
                          headers=None) -> WorkspaceStream:
        handle = await self._ensure_handle(actor)
        # 流式文件可能下载超过 10 分钟；用允许的最大租约覆盖整个传输，
        # 关闭/断开时立即释放，异常退出则最迟 1 小时后自动解除。
        response, lease_id = await self._request_with_handle(
            handle, method, path, headers=headers, stream=True,
            lease_ttl=3600)
        return WorkspaceStream(response, lease_id, self._store, self._db_runner)

    async def upload(self, actor: Identity, body: bytes,
                     content_type: str):
        if len(body) > self._max_upload_bytes:
            raise DomainError("payload_too_large", 413)
        return await self.request(
            actor, "POST", "/api/upload", body=body,
            content_type=content_type)

    async def status(self, actor: Identity) -> dict:
        base = {
            "gateway": False,
            "transport": "control",
            "runtime": "control",
            "skills": [],
            "personas": [],
            "state": "unavailable",
            "detail": "",
        }
        if self._runtime is None or self._gateway is None:
            base["detail"] = "工作区服务未装配。"
            return base
        try:
            revision = await self._revision(actor.workspace_id)
        except DomainError:
            base["detail"] = "工作区不存在或不可用。"
            return base
        lease = await self._db(
            self._store.get_worker_lease, actor.workspace_id)
        if lease is None or lease.state in ("absent", "stopped"):
            base["state"] = "not_started"
            base["detail"] = "工作区尚未启动；首次打开画像或内容库时按需启动。"
            return base
        if lease.state == "starting":
            base["state"] = "starting"
            base["detail"] = "工作区正在启动。"
            return base
        if lease.state == "stopping":
            base["state"] = "stopping"
            base["detail"] = "工作区正在停止。"
            return base
        if lease.state == "failed":
            base["detail"] = "工作区启动失败或已不可用。"
            return base
        if lease.state == "draining":
            # draining 仍服务既有 Run/文件读取，但不声称新实例可启动。
            base["state"] = "ready"
            base["detail"] = "工作区正在停止接纳新任务。"
        try:
            handle = self._handle_from_lease(lease, revision)
        except DomainError:
            handle = None
        if handle is None:
            base["detail"] = "工作区实例尚未就绪或 gateway 不可用。"
            return base
        try:
            response = await self._request_with_handle(
                handle, "GET", "/api/status")
        except DomainError as error:
            base["detail"] = f"工作区状态读取失败（{error.code}）。"
            return base
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            base["detail"] = "工作区状态响应无效。"
            return base
        result = dict(base)
        result.update({
            # 只暴露本 workspace 的可用性/目录；宿主安装的 runtime 名称不是
            # 用户配置，control 统一报告自己的传输层。
            "gateway": payload.get("gateway") is True,
            "transport": "control",
            "runtime": "control",
            "skills": payload.get("skills")
            if isinstance(payload.get("skills"), list) else [],
            "personas": payload.get("personas")
            if isinstance(payload.get("personas"), list) else [],
            "state": "ready",
        })
        result["detail"] = base["detail"]
        return result
