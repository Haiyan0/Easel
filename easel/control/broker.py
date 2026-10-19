"""Run 范围的 provider broker：opaque 令牌、不可变快照与并发预算。

边界：

* 只有服务器内部代码调用 :meth:`issue`；不提供浏览器任选 workspace/run/channel
  的签发接口。
* 令牌是独立高熵 opaque 值，数据库只保存 SHA-256 摘要；默认 1800 秒过期，
  绑定 workspace/run/channel 与签发时的完整路由快照。
* 快照包含公共路由字段与重新加密的凭证 bundle；管理员更新、个人覆盖更新或
  清除都不会改变已有令牌。任何 repr/日志/响应都不包含管理员 Key。
* 请求额度在数据库事务内原子检查（每空间 2、整机 8），异常/取消/断开由调用方
  在 finally 中释放。
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable, Mapping

import httpx

from ..providers.adapters import AdapterRoute, ProviderAdapter
from ..providers.openai import adapter_for_route
from .config import ConfigService
from .egress import EgressGateway
from .store import ACTIVE_RUN_STATUSES, Store
from .types import DomainError, EffectiveRoute, Identity

DEFAULT_TOKEN_TTL = 1800
_MAX_TOKEN_LENGTH = 512
_JSON_BODY_LIMIT = 8 * 1024 * 1024
_MULTIPART_PREFETCH_LIMIT = 1024 * 1024

_FORBIDDEN_BODY_KEYS = frozenset({
    "base_url", "baseurl", "url", "endpoint", "api_base", "apibase",
    "api_key", "apikey", "key", "headers", "auth", "authorization",
    "host", "credential_ref", "credentialref", "proxy", "path", "target",
})
_FORBIDDEN_FORM_FIELDS = (
    b"base_url", b"url", b"endpoint", b"api_key", b"apikey",
    b"authorization", b"headers", b"host", b"credential_ref",
)
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
})
_SENSITIVE_RESPONSE_HEADERS = frozenset({
    "set-cookie", "set-cookie2", "authorization", "proxy-authorization",
    "x-api-key", "api-key", "x-auth-token", "x-easel-broker-token",
    "x-easel-workspace-id", "x-easel-run-id",
})
_BOUNDARY_RE = re.compile(
    r'(?:^|;)\s*boundary=(?:"([^"]+)"|([^;\s]+))', re.IGNORECASE)
_SAFE_BOUNDARY = re.compile(r"^[0-9A-Za-z'()+_,\-./:=? ]{1,70}$")


def _token_digest(token: object) -> str:
    """令牌只以 SHA-256 摘要入库；非法形状统一 401，不回显原值。"""
    if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_LENGTH:
        raise DomainError("invalid_token", 401)
    try:
        data = token.encode("ascii")
    except UnicodeEncodeError:
        raise DomainError("invalid_token", 401) from None
    return hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        raise DomainError("configuration_invalid", 500) from None


@dataclass
class BrokerResponse:
    """已打开的上游响应；chunks() 流式产出并在结束/异常时释放额度。"""

    status_code: int
    headers: dict[str, str]
    _response: httpx.Response = field(repr=False)
    _context: object = field(repr=False)
    _client: httpx.AsyncClient = field(repr=False)
    _release: Callable[[], Awaitable[None]] = field(repr=False)
    _closed: bool = field(default=False, repr=False)

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._response.aiter_raw():
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._response.aclose()
        finally:
            try:
                await self._context.__aexit__(None, None, None)
            except Exception:
                pass
            try:
                await self._client.aclose()
            finally:
                await self._release()


async def _read_bounded(body, limit: int) -> bytes:
    if body is None:
        return b""
    chunks: list[bytes] = []
    total = 0
    async for chunk in body:
        total += len(chunk)
        if total > limit:
            raise DomainError("request_too_large", 413)
        chunks.append(chunk)
    return b"".join(chunks)


def _boundary_from_content_type(content_type: str) -> str | None:
    match = _BOUNDARY_RE.search(content_type)
    if match is None:
        return None
    boundary = match.group(1) or match.group(2)
    if not boundary or _SAFE_BOUNDARY.fullmatch(boundary) is None:
        return None
    return boundary


def _iter_multipart_parts(data: bytes, boundary: bytes):
    """按 part 边界解析完整的 multipart 段；未完整的部分不产出。"""
    delimiter = b"--" + boundary
    position = 0
    while True:
        start = data.find(delimiter, position)
        if start < 0:
            return
        after = start + len(delimiter)
        if data[after:after + 2] == b"--":
            return
        if data[after:after + 2] == b"\r\n":
            header_start = after + 2
            separator = b"\r\n\r\n"
        elif data[after:after + 1] == b"\n":
            header_start = after + 1
            separator = b"\n\n"
        else:
            return
        header_end = data.find(separator, header_start)
        if header_end < 0:
            return
        value_start = header_end + len(separator)
        next_boundary = data.find(b"\r\n" + delimiter, value_start)
        advance = 2
        if next_boundary < 0:
            next_boundary = data.find(b"\n" + delimiter, value_start)
            advance = 1
        if next_boundary < 0:
            # 当前 part 的值尚未接收完整
            return
        yield data[header_start:header_end], data[value_start:next_boundary]
        position = next_boundary + advance


def _find_multipart_field(data: bytes, boundary: bytes, field: bytes) -> str | None:
    """只解析 part 头部的 Content-Disposition，忽略文件内容里的伪字段。"""
    for headers, value in _iter_multipart_parts(data, boundary):
        disposition = b""
        for line in headers.splitlines():
            if line.lower().startswith(b"content-disposition:"):
                disposition = line
                break
        match = re.search(rb'name="([^"]*)"|name=([^;\s]+)', disposition,
                          re.IGNORECASE)
        if match is None:
            continue
        name = match.group(1) if match.group(1) is not None else match.group(2)
        if name in _FORBIDDEN_FORM_FIELDS:
            raise DomainError("invalid_request", 400)
        if name == field:
            try:
                decoded = value.decode("utf-8")
            except UnicodeDecodeError:
                raise DomainError("invalid_request", 400) from None
            if len(decoded) > 256 or any(char in decoded for char in "\r\n\0"):
                raise DomainError("invalid_request", 400)
            return decoded
    return None


def _response_headers(response: httpx.Response) -> dict[str, str]:
    """保留合法响应头；剥离 hop-by-hop、Connection 指定头与凭证类头。"""
    connection_tokens = {
        token.strip().lower()
        for token in response.headers.get("connection", "").split(",")
        if token.strip()
    }
    headers: dict[str, str] = {}
    for name, value in response.headers.items():
        lower = name.lower()
        if (lower in _HOP_BY_HOP or lower in connection_tokens
                or lower in _SENSITIVE_RESPONSE_HEADERS):
            continue
        headers[name] = value
    headers["Cache-Control"] = "no-store"
    return headers


def _request_headers(headers: Mapping[str, str], *, content_length: str | None = None
                     ) -> dict[str, str]:
    """客户端请求头只保留内容协商相关字段，其余全部重建。"""
    result: dict[str, str] = {}
    for name in ("content-type", "accept"):
        value = headers.get(name) if hasattr(headers, "get") else None
        if isinstance(value, str) and value and "\r" not in value and "\n" not in value:
            result[name] = value
    if content_length is not None:
        result["content-length"] = content_length
    return result


class BrokerService:
    """按受管路由签发和校验调用令牌；目标与凭证均由服务端决定。"""

    def __init__(self, store: Store, config: ConfigService,
                 clock: Callable[[], float] = time.time, *,
                 egress: EgressGateway | None = None,
                 token_ttl: int = DEFAULT_TOKEN_TTL,
                 db_runner=None):
        if not isinstance(store, Store):
            raise TypeError("store must be a Store")
        if not isinstance(config, ConfigService):
            raise TypeError("config must be a ConfigService")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if type(token_ttl) is not int or not 1 <= token_ttl <= 86400:
            raise ValueError("token_ttl must be an integer in 1..86400")
        if db_runner is not None and not callable(db_runner):
            raise TypeError("db_runner must be callable")
        if egress is None:
            egress = EgressGateway(policy=getattr(config, "egress_policy", None))
        if not isinstance(egress, EgressGateway):
            raise TypeError("egress must be an EgressGateway")
        if egress.policy != config.egress_policy:
            # 保存/签发/实际请求必须使用同一出口策略，避免成员或配置绕过例外
            raise ValueError("egress policy must match config egress policy")
        self._store = store
        self._config = config
        self._clock = clock
        self._token_ttl = token_ttl
        self._egress = egress
        self._db_runner = db_runner

    @property
    def egress(self) -> EgressGateway:
        return self._egress

    def with_store(self, store: Store, config: ConfigService | None = None, *,
                   db_runner=None) -> "BrokerService":
        """应用线程用同一 master key 重建；保留出口与测试 transport。"""
        return BrokerService(
            store,
            config if config is not None else self._config.with_store(store),
            clock=self._clock,
            egress=self._egress,
            token_ttl=self._token_ttl,
            db_runner=self._db_runner if db_runner is None else db_runner)

    # ------------------------------------------------------------------
    # 签发与授权
    # ------------------------------------------------------------------

    def issue(self, actor: Identity, run_id: str, channel: str) -> str:
        """为本人活跃 Run 签发调用令牌；不创建 Run、不下发任何凭证。"""
        if not isinstance(run_id, str) or not run_id:
            raise DomainError("not_found", 404)
        if not isinstance(channel, str) or not channel:
            raise DomainError("invalid_channel", 400)
        identity = self._store.require_identity(actor)
        run = self._store.get_run(identity, run_id)
        if run.status not in ACTIVE_RUN_STATUSES:
            raise DomainError("terminal_run", 409)

        route = self._config.resolve(identity, channel)
        adapter = adapter_for_route(route)
        if route.protocol not in adapter.protocols:
            raise DomainError("capability_unavailable", 503)
        # 签发时再按当前出口策略校验一次，与保存/实际请求保持一致
        self._egress.policy.validate_url(route.base_url, resolve=False)
        bundle = self._config.export_credentials(route)

        now = int(self._clock())
        token = secrets.token_urlsafe(32)
        snapshot_id = uuid.uuid4().hex
        encrypted = self._config.seal_broker_snapshot(bundle, snapshot_id)
        allowed_models = tuple(route.allowed_models) or (route.model,)
        route_json = _canonical({
            "channel": route.channel,
            "provider": route.provider,
            "model": route.model,
            "base_url": route.base_url,
            "protocol": route.protocol,
            "source": route.source,
            "version": int(route.version),
            "options": dict(route.options),
            "allowed_models": list(allowed_models),
        })
        self._store.create_broker_token(
            identity, run.id, channel,
            token_digest=_token_digest(token),
            snapshot_id=snapshot_id,
            route_json=route_json,
            encrypted_secrets=encrypted,
            config_version=int(route.version),
            now=now,
            expires_at=now + self._token_ttl)
        return token

    def authorize(self, token: str, workspace_id: str, run_id: str,
                  channel: str) -> EffectiveRoute:
        """按数据库复查令牌范围与活跃状态，返回不可变快照路由。"""
        for value in (workspace_id, run_id, channel):
            if not isinstance(value, str) or not value:
                raise DomainError("not_found", 404)
        row = self._store.consume_broker_token(
            _token_digest(token), workspace_id, run_id, channel,
            int(self._clock()))
        return self._route_from_row(row)

    @staticmethod
    def _route_from_row(row) -> EffectiveRoute:
        try:
            payload = json.loads(row["route_json"])
            if not isinstance(payload, dict):
                raise ValueError
            if payload.get("channel") != row["channel"]:
                raise ValueError
            provider = payload["provider"]
            model = payload["model"]
            base_url = payload["base_url"]
            protocol = payload["protocol"]
            source = payload["source"]
            options = payload["options"]
            allowed = payload["allowed_models"]
            if (not all(isinstance(value, str) and value for value in
                        (provider, model, base_url, protocol))
                    or source not in ("admin", "user")
                    or not isinstance(options, dict)
                    or not isinstance(allowed, list)
                    or not all(isinstance(item, str) and item for item in allowed)):
                raise ValueError
            allowed_models = tuple(allowed) or (model,)
        except (KeyError, TypeError, ValueError):
            # 不序列化内部行或密文
            raise DomainError("configuration_invalid", 500) from None
        return EffectiveRoute(
            channel=row["channel"],
            provider=provider,
            model=model,
            base_url=base_url,
            protocol=protocol,
            credential_ref=f"snapshot:{row['snapshot_id']}",
            source=source,
            version=int(row["config_version"]),
            options=dict(options),
            allowed_models=allowed_models)

    def revoke_run(self, run_id: str, workspace_id: str) -> None:
        """撤销某 Run 的全部令牌；数据库提交后对其他连接立即生效。"""
        if not isinstance(run_id, str) or not run_id:
            raise DomainError("not_found", 404)
        if not isinstance(workspace_id, str) or not workspace_id:
            raise DomainError("not_found", 404)
        self._store.revoke_broker_tokens(
            workspace_id, run_id, int(self._clock()))

    # ------------------------------------------------------------------
    # 请求额度
    # ------------------------------------------------------------------

    def acquire_lease(self, token: str, workspace_id: str, run_id: str,
                      channel: str) -> str:
        for value in (workspace_id, run_id, channel):
            if not isinstance(value, str) or not value:
                raise DomainError("not_found", 404)
        return self._store.acquire_broker_lease(
            _token_digest(token), workspace_id, run_id, channel,
            int(self._clock()))

    def release_lease(self, lease_id: str) -> None:
        if not isinstance(lease_id, str) or not lease_id:
            raise DomainError("invalid_argument", 400)
        self._store.release_broker_lease(lease_id)

    # ------------------------------------------------------------------
    # 受限 HTTP 转发
    # ------------------------------------------------------------------

    async def _db(self, function, *args):
        if self._db_runner is None:
            return function(*args)
        return await self._db_runner(function, *args)

    async def forward(self, *, token: str, workspace_id: str, run_id: str,
                      channel: str, method: str, relative_path: str,
                      headers: Mapping[str, str], body=None) -> BrokerResponse:
        """校验令牌与模型约束后，流式转发到服务端确定的上游。

        ``body`` 是请求体的异步迭代器；JSON 会完整校验后重编码，multipart
        在找到 model 字段前只缓冲有限前缀，其余字节原样流式转发。
        """
        route = await self._db(self.authorize, token, workspace_id, run_id,
                               channel)
        adapter_cls = adapter_for_route(route)
        normalized_method = method.upper() if isinstance(method, str) else ""
        if route.protocol not in adapter_cls.protocols:
            raise DomainError("capability_unavailable", 503)
        matched = next(
            (item for item in adapter_cls.routes
             if item.method == normalized_method
             and item.relative_path == relative_path), None)
        if matched is None:
            raise DomainError("not_found", 404)

        snapshot_id = self._snapshot_id(route)
        bundle = await self._db(self._config.open_broker_snapshot, snapshot_id)
        adapter = adapter_cls(bundle)
        prepared_body, request_headers = await self._prepare_request(
            route, adapter, matched, headers, body)
        upstream = adapter.prepare(route, normalized_method,
                                   matched.relative_path, request_headers)

        # 保存时只是前置检查；实际请求前按当前策略再校验一次
        self._egress.policy.validate_url(route.base_url, resolve=False)
        lease_id = await self._db(
            self.acquire_lease, token, workspace_id, run_id, channel)
        released = False

        async def release_once() -> None:
            nonlocal released
            if released:
                return
            released = True
            await self._db(self.release_lease, lease_id)

        try:
            client = self._egress.client()
            context = client.stream(
                upstream.method, upstream.url, headers=upstream.headers,
                content=prepared_body, follow_redirects=False)
            try:
                response = await context.__aenter__()
            except BaseException:
                try:
                    await context.__aexit__(None, None, None)
                except Exception:
                    pass
                await client.aclose()
                raise
            if 300 <= response.status_code < 400:
                await response.aclose()
                try:
                    await context.__aexit__(None, None, None)
                except Exception:
                    pass
                await client.aclose()
                # 默认不跟随重定向，也绝不把上游 Key 发到跳转目标
                raise DomainError("upstream_redirect", 502)
            return BrokerResponse(
                status_code=response.status_code,
                headers=_response_headers(response),
                _response=response,
                _context=context,
                _client=client,
                _release=release_once,
            )
        except DomainError:
            await release_once()
            raise
        except httpx.HTTPError:
            await release_once()
            # 不向上暴露底层连接细节
            raise DomainError("upstream_unavailable", 502) from None
        except BaseException:
            await release_once()
            raise

    @staticmethod
    def _snapshot_id(route: EffectiveRoute) -> str:
        prefix, separator, snapshot_id = route.credential_ref.partition(":")
        if prefix != "snapshot" or not separator or not snapshot_id:
            raise DomainError("configuration_invalid", 500)
        return snapshot_id

    async def _prepare_request(
            self, route: EffectiveRoute, adapter: ProviderAdapter,
            matched: AdapterRoute, headers: Mapping[str, str], body
    ) -> tuple[object, dict[str, str]]:
        if matched.body == "none":
            return None, _request_headers(headers)

        content_type = headers.get("content-type") if hasattr(headers, "get") else None
        if not isinstance(content_type, str):
            content_type = ""

        if matched.body == "json":
            if not content_type.lower().startswith("application/json"):
                raise DomainError("unsupported_media_type", 415)
            declared = headers.get("content-length")
            if declared is not None:
                if not str(declared).isdigit() or int(declared) > _JSON_BODY_LIMIT:
                    raise DomainError("request_too_large", 413)
            raw = await _read_bounded(body, _JSON_BODY_LIMIT)
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError, UnicodeDecodeError):
                raise DomainError("invalid_request", 400) from None
            if not isinstance(payload, dict):
                raise DomainError("invalid_request", 400)
            if any(str(key).lower() in _FORBIDDEN_BODY_KEYS for key in payload):
                raise DomainError("invalid_request", 400)
            allowed = tuple(route.allowed_models) or (route.model,)
            if "model" in payload:
                model = payload["model"]
                if not isinstance(model, str) or model not in allowed:
                    raise DomainError("invalid_request", 400)
            else:
                if route.model not in allowed:
                    raise DomainError("invalid_request", 400)
                payload["model"] = route.model
            for key, value in route.options.items():
                payload.setdefault(key, value)
            try:
                encoded = json.dumps(
                    payload, ensure_ascii=False, allow_nan=False,
                    separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError):
                raise DomainError("invalid_request", 400) from None
            prepared = _request_headers(headers)
            prepared["content-type"] = "application/json"
            return encoded, prepared

        # multipart：先找到并校验 model，再把原始字节（含 boundary）原样转发
        if not content_type.lower().startswith("multipart/form-data"):
            raise DomainError("unsupported_media_type", 415)
        boundary = _boundary_from_content_type(content_type)
        if boundary is None:
            raise DomainError("invalid_request", 400)
        buffered, model, rest = await self._prefetch_multipart(body, boundary)
        allowed = tuple(route.allowed_models) or (route.model,)
        if model not in allowed:
            raise DomainError("invalid_request", 400)

        declared = headers.get("content-length")
        content_length = (
            str(declared) if declared is not None and str(declared).isdigit()
            else None)
        prepared = _request_headers(headers, content_length=content_length)
        prepared["content-type"] = content_type

        async def replay() -> AsyncIterator[bytes]:
            for chunk in buffered:
                yield chunk
            async for chunk in rest:
                yield chunk

        return replay(), prepared

    async def _prefetch_multipart(self, body, boundary: str):
        """只缓冲到 model 字段完整出现为止，避免把大文件整体读入内存。"""
        if body is None:
            raise DomainError("invalid_request", 400)
        try:
            marker = boundary.encode("ascii")
        except UnicodeEncodeError:
            raise DomainError("invalid_request", 400) from None
        buffer = bytearray()
        async for chunk in body:
            buffer.extend(chunk)
            if len(buffer) > _MULTIPART_PREFETCH_LIMIT:
                raise DomainError("invalid_request", 400)
            model = _find_multipart_field(bytes(buffer), marker, b"model")
            if model is not None:
                return [bytes(buffer)], model, body
        raise DomainError("invalid_request", 400)
