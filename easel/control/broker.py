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

import asyncio
import hashlib
import json
import os
import re
import secrets
import tempfile
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
DEFAULT_LEASE_TTL = 120
_MAX_TOKEN_LENGTH = 512
_JSON_BODY_LIMIT = 8 * 1024 * 1024
_MULTIPART_SPOOL_MEMORY = 1024 * 1024
_MULTIPART_MAX_BYTES = 512 * 1024 * 1024
_MULTIPART_HEADER_LIMIT = 16 * 1024
_MULTIPART_LINE_LIMIT = 8192

_FORBIDDEN_BODY_KEYS = frozenset({
    "base_url", "baseurl", "url", "endpoint", "api_base", "apibase",
    "api_key", "apikey", "key", "headers", "auth", "authorization",
    "host", "credential_ref", "credentialref", "proxy", "path", "target",
})
_FORBIDDEN_FORM_FIELDS = frozenset({
    b"base_url", b"url", b"endpoint", b"api_key", b"apikey",
    b"authorization", b"headers", b"host", b"credential_ref",
})
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


class _SpoolBody(httpx.AsyncByteStream):
    """把已完整校验的 multipart 临时文件流式送给上游。"""

    def __init__(self, file, size: int):
        self._file = file
        self.size = size
        self._closed = False

    async def __aiter__(self):
        while True:
            chunk = self._file.read(65536)
            if not chunk:
                break
            yield chunk

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self._file.close()


def _read_mp_line(stream, limit: int = _MULTIPART_LINE_LIMIT) -> bytes:
    line = stream.readline(limit + 1)
    if len(line) > limit:
        raise DomainError("invalid_request", 400)
    return line


def _parse_mp_headers(stream) -> dict[bytes, bytes]:
    headers: dict[bytes, bytes] = {}
    total = 0
    while True:
        line = _read_mp_line(stream)
        if line in (b"\r\n", b"\n"):
            return headers
        if not line:
            raise DomainError("invalid_request", 400)
        total += len(line)
        if total > _MULTIPART_HEADER_LIMIT or b":" not in line:
            raise DomainError("invalid_request", 400)
        name, value = line.split(b":", 1)
        key = name.strip().lower()
        if not key or key in headers:
            raise DomainError("invalid_request", 400)
        headers[key] = value.strip()


def _skip_mp_value(stream, boundary: bytes,
                   capture_limit: int | None = None) -> bytes | None:
    """读取到下一个 part 边界，返回边界后缀前的值（可选捕获）。"""
    crlf = b"\r\n--" + boundary
    lf = b"\n--" + boundary
    keep = max(len(crlf), len(lf)) - 1
    tail = b""
    captured = bytearray()
    while True:
        chunk = stream.read(65536)
        if not chunk:
            # 缺少结束边界
            raise DomainError("invalid_request", 400)
        data = tail + chunk
        i_crlf = data.find(crlf)
        i_lf = data.find(lf)
        delimiter = None
        index = -1
        if i_crlf >= 0:
            index, delimiter = i_crlf, crlf
        if i_lf >= 0 and (i_crlf < 0 or i_lf < i_crlf):
            index, delimiter = i_lf, lf
        if delimiter is not None:
            value_part = data[:index]
            if capture_limit is not None:
                if len(captured) + len(value_part) > capture_limit:
                    raise DomainError("invalid_request", 400)
                captured.extend(value_part)
            suffix_offset = index + len(delimiter)
            stream.seek(suffix_offset - len(data), os.SEEK_CUR)
            return bytes(captured) if capture_limit is not None else None

        if keep:
            value_part, tail = data[:-keep], data[-keep:]
        else:
            value_part, tail = data, b""
        if capture_limit is not None:
            if len(captured) + len(value_part) > capture_limit:
                raise DomainError("invalid_request", 400)
            captured.extend(value_part)


def _read_mp_suffix(stream, boundary: bytes) -> str:
    suffix = stream.read(2)
    if suffix == b"--":
        # 结束 boundary 之后只允许空白；分块读取，避免 epilogue 撑爆内存
        while True:
            rest = stream.read(65536)
            if not rest:
                break
            if rest.strip():
                raise DomainError("invalid_request", 400)
        return "close"
    if suffix in (b"\r\n", b"\n"):
        return "next"
    raise DomainError("invalid_request", 400)


_DISPOSITION_NAME = re.compile(
    rb'(?:^|;)\s*name=(?:"([^"]*)"|([^;\s]+))', re.IGNORECASE)
_DISPOSITION_FILENAME = re.compile(
    rb'(?:^|;)\s*filename=(?:"([^"]*)"|([^;\s]+))', re.IGNORECASE)


def _validate_multipart(stream, boundary: bytes,
                        allowed_models: tuple[str, ...]) -> None:
    """完整解析所有 part，保证上游只会收到一个已授权的 model。"""
    stream.seek(0)
    first = _read_mp_line(stream, 1024)
    delimiter = b"--" + boundary
    if not first.startswith(delimiter):
        raise DomainError("invalid_request", 400)
    rest = first[len(delimiter):]
    if rest.rstrip(b"\r\n") == b"--":
        # 空 multipart：没有 model
        raise DomainError("invalid_request", 400)
    if rest.strip(b"\r\n"):
        # 不允许 preamble
        raise DomainError("invalid_request", 400)

    model_values: list[str] = []
    while True:
        headers = _parse_mp_headers(stream)
        disposition = headers.get(b"content-disposition")
        if disposition is None:
            raise DomainError("invalid_request", 400)
        name_matches = list(_DISPOSITION_NAME.finditer(disposition))
        if len(name_matches) != 1:
            # 缺失或重复 name 参数都会让上游解析产生歧义
            raise DomainError("invalid_request", 400)
        name_match = name_matches[0]
        name = (name_match.group(1) if name_match.group(1) is not None
                else name_match.group(2))
        filename_matches = list(_DISPOSITION_FILENAME.finditer(disposition))
        if len(filename_matches) > 1:
            raise DomainError("invalid_request", 400)
        has_filename = bool(filename_matches)

        lowered = name.lower()
        if lowered in _FORBIDDEN_FORM_FIELDS:
            raise DomainError("invalid_request", 400)
        if lowered == b"model":
            if name != b"model" or has_filename:
                # 大小写变体或带 filename 的 model 存在解析歧义
                raise DomainError("invalid_request", 400)
            if model_values:
                # 重复 model：上游可能采用后一个值
                raise DomainError("invalid_request", 400)
            raw = _skip_mp_value(stream, boundary, capture_limit=257)
            try:
                value = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise DomainError("invalid_request", 400) from None
            if (not value or len(value) > 256
                    or any(char in value for char in "\r\n\0")):
                raise DomainError("invalid_request", 400)
            if value not in allowed_models:
                raise DomainError("invalid_request", 400)
            model_values.append(value)
        else:
            _skip_mp_value(stream, boundary)

        if _read_mp_suffix(stream, boundary) == "close":
            break

    if len(model_values) != 1:
        raise DomainError("invalid_request", 400)


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
                 lease_ttl: int = DEFAULT_LEASE_TTL,
                 lease_clock: Callable[[], float] | None = None,
                 db_runner=None):
        if not isinstance(store, Store):
            raise TypeError("store must be a Store")
        if not isinstance(config, ConfigService):
            raise TypeError("config must be a ConfigService")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if type(token_ttl) is not int or not 1 <= token_ttl <= 86400:
            raise ValueError("token_ttl must be an integer in 1..86400")
        if type(lease_ttl) is not int or not 1 <= lease_ttl <= 86400:
            raise ValueError("lease_ttl must be an integer in 1..86400")
        if lease_clock is not None and not callable(lease_clock):
            raise TypeError("lease_clock must be callable")
        if db_runner is not None and not callable(db_runner):
            raise TypeError("db_runner must be callable")
        if egress is None:
            egress = EgressGateway(
                config.egress_policy,
                policy_explicit=config.egress_policy_configured)
        if not isinstance(egress, EgressGateway):
            raise TypeError("egress must be an EgressGateway")
        if (egress.policy != config.egress_policy
                or egress.policy_configured
                != config.egress_policy_configured):
            # 保存/签发/实际请求必须使用同一出口策略，避免成员或配置绕过例外
            raise ValueError("egress policy must match config egress policy")
        self._store = store
        self._config = config
        self._clock = clock
        self._token_ttl = token_ttl
        self._lease_ttl = lease_ttl
        self._lease_clock = lease_clock or time.time
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
            lease_ttl=self._lease_ttl,
            lease_clock=self._lease_clock,
            db_runner=self._db_runner if db_runner is None else db_runner)

    # ------------------------------------------------------------------
    # 签发与授权
    # ------------------------------------------------------------------

    def issue(self, actor: Identity, run_id: str, channel: str) -> str:
        """为本人活跃 Run 签发调用令牌；不创建 Run、不下发任何凭证。

        路由、凭证与 token 快照在同一个写事务中取得/写入；其他连接的个人
        覆盖更新或管理员发布必须等待该事务提交，不会混配地址与 Key。
        """
        if not isinstance(run_id, str) or not run_id:
            raise DomainError("not_found", 404)
        if not isinstance(channel, str) or not channel:
            raise DomainError("invalid_channel", 400)
        identity = self._store.require_identity(actor)
        now = int(self._clock())
        token = secrets.token_urlsafe(32)
        snapshot_id = uuid.uuid4().hex

        def build(workspace_id: str, snapshot_channel: str,
                  managed_row, override_row):
            route, bundle = self._config.snapshot_from_rows(
                workspace_id, snapshot_channel, managed_row, override_row)
            adapter = adapter_for_route(route)
            if route.protocol not in adapter.protocols:
                raise DomainError("capability_unavailable", 503)
            # 签发时再按当前出口策略校验一次，与保存/实际请求保持一致
            self._egress.policy.validate_url(route.base_url, resolve=False)
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
            return route_json, encrypted, int(route.version)

        self._store.create_broker_token_atomic(
            identity, run_id, channel,
            token_digest=_token_digest(token),
            snapshot_id=snapshot_id,
            now=now,
            expires_at=now + self._token_ttl,
            build=build)
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
            int(self._clock()), int(self._lease_clock()), self._lease_ttl)

    async def _renew_lease_loop(self, lease_id: str, on_failure=None) -> None:
        """流未关闭期间持续续期；续期失败时终止在途流并释放额度。"""
        interval = max(1.0, self._lease_ttl / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                expires_at = int(self._lease_clock()) + self._lease_ttl
                await self._db(self._store.renew_broker_lease, lease_id,
                               expires_at)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 无法续期时不能继续无额度运行：先关闭上游再释放
                if on_failure is not None:
                    try:
                        await on_failure()
                    except Exception:
                        pass
                await self._db(self.release_lease, lease_id)
                return

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
        if not self._egress.configured:
            # 缺代理或显式策略时，在占用额度/创建真实连接前明确失败
            raise DomainError("egress_unavailable", 503)
        lease_id = await self._db(
            self.acquire_lease, token, workspace_id, run_id, channel)
        abort_box: dict = {}

        async def abort_stream() -> None:
            abort = abort_box.get("abort")
            if abort is not None:
                await abort()

        heartbeat = asyncio.create_task(
            self._renew_lease_loop(lease_id, abort_stream))
        released = False

        async def release_once() -> None:
            nonlocal released
            if released:
                return
            released = True
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            await self._db(self.release_lease, lease_id)

        async def close_body() -> None:
            closer = getattr(prepared_body, "aclose", None)
            if closer is not None:
                await closer()

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
                await close_body()
                await client.aclose()
                raise
            # 续期失败时先关闭已打开的上游流，再由心跳释放额度
            async def abort_opened() -> None:
                await response.aclose()
                try:
                    await context.__aexit__(None, None, None)
                except Exception:
                    pass
                await client.aclose()

            abort_box["abort"] = abort_opened
            # 请求体已发送完毕，关闭 multipart 临时 spool
            await close_body()
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
            await close_body()
            await release_once()
            raise
        except httpx.HTTPError:
            await close_body()
            await release_once()
            # 不向上暴露底层连接细节
            raise DomainError("upstream_unavailable", 502) from None
        except BaseException:
            await close_body()
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

        # multipart：完整解析/校验后，把原始字节（含 boundary）从 spool 转发
        if not content_type.lower().startswith("multipart/form-data"):
            raise DomainError("unsupported_media_type", 415)
        boundary = _boundary_from_content_type(content_type)
        if boundary is None:
            raise DomainError("invalid_request", 400)
        allowed = tuple(route.allowed_models) or (route.model,)
        prepared_body = await self._spool_and_validate_multipart(
            body, boundary, allowed)
        prepared = _request_headers(headers)
        prepared["content-type"] = content_type
        prepared["content-length"] = str(prepared_body.size)
        return prepared_body, prepared

    async def _spool_and_validate_multipart(
            self, body, boundary: str, allowed_models: tuple[str, ...]):
        """完整校验 multipart，再有界地 spool 原始字节供上游读取。

        先落盘再校验，保证重复 model、后置禁用字段和畸形结束边界不会在
        校验前被上游看到；SpooledTemporaryFile 在 1 MiB 后自动转磁盘。
        """
        if body is None:
            raise DomainError("invalid_request", 400)
        try:
            marker = boundary.encode("ascii")
        except UnicodeEncodeError:
            raise DomainError("invalid_request", 400) from None
        spool = tempfile.SpooledTemporaryFile(
            max_size=_MULTIPART_SPOOL_MEMORY, mode="w+b")
        total = 0
        try:
            async for chunk in body:
                total += len(chunk)
                if total > _MULTIPART_MAX_BYTES:
                    raise DomainError("request_too_large", 413)
                spool.write(chunk)
            spool.flush()
            _validate_multipart(spool, marker, allowed_models)
            spool.seek(0)
            return _SpoolBody(spool, total)
        except BaseException:
            spool.close()
            raise
