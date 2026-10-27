"""独立 gateway ASGI 应用：精确路由、固定 worker 绑定与流式清理。

gateway 只接受已验签的 control 断言；所有目标、身份头和代次都来自受信
``GatewayConfig``。普通请求不能提供 worker 地址、Host 或内部凭证。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect

from easel.gateway.auth import (
    DEFAULT_NONCE_CAPACITY,
    GATEWAY_ASSERTION_HEADER,
    MAX_ASSERTION_BYTES,
    MAX_PATH_BYTES,
    MAX_QUERY_BYTES,
    GatewayConfig,
    GatewayError,
    GatewayNonceCache,
    verify_gateway_assertion,
)
from easel.worker.guard import SERVICE_TOKEN_HEADER, WORKSPACE_HEADER

LOGGER = logging.getLogger(__name__)

RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_CANONICAL_INT = re.compile(r"^(0|[1-9][0-9]*)$")
_HEX = frozenset("0123456789abcdefABCDEF")
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_VISIBLE = frozenset(chr(code) for code in range(33, 127))

_STRIP_REQUEST_HEADERS = frozenset({
    "host", "content-length", "transfer-encoding", "connection",
    "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailer", "upgrade", "expect", "proxy-connection", "cookie",
    "authorization", "forwarded", "origin", "referer", "via",
    "x-real-ip",
})
_STRIP_RESPONSE_HEADERS = frozenset({
    "set-cookie", "location", "connection", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "proxy-connection",
    "te", "trailer", "transfer-encoding", "upgrade",
})


@dataclass(frozen=True)
class GatewayLimits:
    """已批准的 gateway 资源上限；测试可注入更小值。"""

    max_header_bytes: int = 65536
    max_body_bytes: int = 67108864
    connect_timeout: float = 10.0
    io_timeout: float = 120.0
    max_concurrent_requests: int = 8
    max_concurrent_streams: int = 7
    max_path_bytes: int = MAX_PATH_BYTES
    max_query_bytes: int = MAX_QUERY_BYTES
    max_assertion_bytes: int = MAX_ASSERTION_BYTES

    def __post_init__(self) -> None:
        if (type(self.max_header_bytes) is not int or self.max_header_bytes < 1
                or type(self.max_body_bytes) is not int
                or self.max_body_bytes < 0
                or type(self.max_concurrent_requests) is not int
                or self.max_concurrent_requests < 1
                or type(self.max_concurrent_streams) is not int
                or self.max_concurrent_streams < 0
                or type(self.max_path_bytes) is not int
                or self.max_path_bytes < 1
                or type(self.max_query_bytes) is not int
                or self.max_query_bytes < 0
                or type(self.max_assertion_bytes) is not int
                or self.max_assertion_bytes < 1):
            raise ValueError("gateway limits are invalid")
        for value in (self.connect_timeout, self.io_timeout):
            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or value <= 0:
                raise ValueError("gateway timeout is invalid")


class _NoAuth(httpx.Auth):
    """覆盖注入 client 的默认 auth，避免 Basic/Bearer 被自动加入。"""

    def auth_flow(self, request):  # type: ignore[override]
        yield request


_NO_AUTH = _NoAuth()


@dataclass(frozen=True)
class _Route:
    name: str
    method: str
    path: str
    stream: bool
    allowed_query: frozenset[str]


_ROUTES: dict[str, _Route] = {
    "health": _Route("health", "GET", "/internal/lifecycle/health",
                     False, frozenset()),
    "submit": _Route("submit", "POST", "", False, frozenset()),
    "events": _Route("events", "GET", "", True,
                     frozenset({"after", "nonce"})),
    "stop": _Route("stop", "POST", "", False, frozenset()),
    "answer": _Route("answer", "POST", "", False, frozenset()),
    "status": _Route("status", "POST", "", False, frozenset()),
    "messages": _Route("messages", "GET", "", False,
                       frozenset({"limit", "offset"})),
}


class _Slot:
    def __init__(self, limiter: _ConcurrencyLimiter, stream: bool) -> None:
        self._limiter = limiter
        self.stream = stream
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._limiter.release(self.stream)


class _ConcurrencyLimiter:
    """有界槽；流请求最多占用 max-1 个，给 stop/权限回答留出槽位。

    ASGI 单事件循环内 check+increment 不含 await，天然原子；不使用可取消的
    async lock，避免请求被取消时槽位释放卡在锁等待上。
    """

    def __init__(self, maximum: int, streams: int) -> None:
        self._maximum = maximum
        self._stream_limit = min(streams, max(0, maximum - 1))
        self._active = 0
        self._active_streams = 0

    def acquire(self, stream: bool) -> _Slot:
        if self._active >= self._maximum:
            raise GatewayError("gateway_busy", 429, retry_after=1)
        if stream and self._active_streams >= self._stream_limit:
            raise GatewayError("gateway_busy", 429, retry_after=1)
        self._active += 1
        if stream:
            self._active_streams += 1
        return _Slot(self, stream)

    def release(self, stream: bool) -> None:
        self._active = max(0, self._active - 1)
        if stream:
            self._active_streams = max(0, self._active_streams - 1)

    @property
    def active(self) -> int:
        return self._active

    @property
    def active_streams(self) -> int:
        return self._active_streams


@dataclass(frozen=True)
class _InboundHeaders:
    assertion: str | None
    content_length: int | None
    forwarded: dict[str, str]
    connection_tokens: frozenset[str]


def _error_response(error: GatewayError) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if error.retry_after is not None:
        headers["Retry-After"] = str(int(error.retry_after))
    return JSONResponse({"error": error.code}, status_code=error.status,
                        headers=headers)


def _invalid_path() -> GatewayError:
    return GatewayError("gateway_invalid_path", 400)


def _invalid_query() -> GatewayError:
    return GatewayError("gateway_invalid_query", 400)


def _not_found() -> GatewayError:
    return GatewayError("gateway_not_found", 404)


def _method_not_allowed() -> GatewayError:
    return GatewayError("gateway_method_not_allowed", 405)


def _validate_raw_path(raw_path: bytes, limits: GatewayLimits) -> str:
    if not isinstance(raw_path, (bytes, bytearray)):
        raise _invalid_path()
    if len(raw_path) > limits.max_path_bytes:
        raise GatewayError("gateway_uri_too_long", 414)
    try:
        path = bytes(raw_path).decode("ascii")
    except UnicodeDecodeError:
        raise _invalid_path() from None
    if (not path or not path.startswith("/") or path == "/"
            or path.endswith("/") or "//" in path or "\\" in path
            or "?" in path or "#" in path
            or any(ord(character) < 32 or ord(character) == 127
                   for character in path)):
        raise _invalid_path()
    lowered = path.lower()
    if any(marker in lowered for marker in ("%00", "%2f", "%5c", "%0a", "%0d")):
        raise _invalid_path()
    index = 0
    while index < len(path):
        if path[index] == "%":
            if (index + 2 >= len(path) or path[index + 1] not in _HEX
                    or path[index + 2] not in _HEX):
                raise _invalid_path()
            index += 3
            continue
        index += 1
    return path


def _run_or_session_id(segment: str) -> str:
    if "%" in segment or not RUN_ID.fullmatch(segment):
        raise _invalid_path()
    return segment


def _decode_path_segment(segment: str) -> str:
    """解码单段 question_id；拒绝编码 slash/反斜杠/NUL 与编码 unreserved。"""
    decoded = bytearray()
    index = 0
    while index < len(segment):
        character = segment[index]
        if character == "%":
            if (index + 2 >= len(segment)
                    or segment[index + 1] not in _HEX
                    or segment[index + 2] not in _HEX):
                raise _invalid_path()
            byte = int(segment[index + 1:index + 3], 16)
            if chr(byte) in _UNRESERVED:
                raise _invalid_path()
            decoded.append(byte)
            index += 3
            continue
        if character not in _UNRESERVED:
            raise _invalid_path()
        decoded.extend(character.encode("ascii"))
        index += 1
    try:
        value = bytes(decoded).decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid_path() from None
    if (not 1 <= len(decoded) <= 256 or value in (".", "..")
            or any(character in value for character in "/\\?#\x00\r\n")
            or any(ord(character) < 32 or ord(character) == 127
                   for character in value)):
        raise _invalid_path()
    return value


def _question_segment(segment: str) -> str:
    value = _decode_path_segment(segment)
    canonical = quote(value, safe="-._~", encoding="utf-8", errors="strict")
    if not canonical or canonical in (".", ".."):
        raise _invalid_path()
    return canonical


def _match_path(raw_path: bytes, method: str,
                limits: GatewayLimits) -> _Route:
    path = _validate_raw_path(raw_path, limits)
    parts = path.split("/")[1:]
    # 只有 answer 路由的 question_id 段允许百分号编码；其他段一律拒绝，
    # 避免编码字面量形成同一路由的别名。
    question_index = None
    if (len(parts) == 6 and parts[0] == "internal" and parts[1] == "runs"
            and parts[3] == "questions" and parts[5] == "answer"):
        question_index = 4
    for index, segment in enumerate(parts):
        if "%" in segment and index != question_index:
            raise _invalid_path()
    if len(parts) == 3 and parts[0] == "internal" \
            and parts[1] == "lifecycle" and parts[2] == "health":
        route = _ROUTES["health"]
    elif len(parts) == 3 and parts[0] == "internal" and parts[1] == "runs":
        run_id = _run_or_session_id(parts[2])
        route = _Route("submit", "POST", f"/internal/runs/{run_id}",
                       False, frozenset())
    elif len(parts) == 4 and parts[0] == "internal" and parts[1] == "runs":
        run_id = _run_or_session_id(parts[2])
        if parts[3] == "events":
            route = _Route("events", "GET",
                           f"/internal/runs/{run_id}/events", True,
                           _ROUTES["events"].allowed_query)
        elif parts[3] == "stop":
            route = _Route("stop", "POST",
                           f"/internal/runs/{run_id}/stop", False,
                           frozenset())
        else:
            raise _not_found()
    elif len(parts) == 5 and parts[0] == "internal" and parts[1] == "runs":
        run_id = _run_or_session_id(parts[2])
        if parts[3] != "questions":
            raise _not_found()
        if parts[4] == "status":
            route = _Route("status", "POST",
                           f"/internal/runs/{run_id}/questions/status",
                           False, frozenset())
        else:
            raise _not_found()
    elif len(parts) == 6 and parts[0] == "internal" and parts[1] == "runs":
        run_id = _run_or_session_id(parts[2])
        if parts[3] != "questions" or parts[5] != "answer":
            raise _not_found()
        question_id = _question_segment(parts[4])
        route = _Route(
            "answer", "POST",
            f"/internal/runs/{run_id}/questions/{question_id}/answer",
            False, frozenset())
    elif (len(parts) == 4 and parts[0] == "internal"
          and parts[1] == "sessions" and parts[3] == "messages"):
        session_id = _run_or_session_id(parts[2])
        route = _Route(
            "messages", "GET",
            f"/internal/sessions/{session_id}/messages", False,
            _ROUTES["messages"].allowed_query)
    else:
        raise _not_found()
    if method != route.method:
        raise _method_not_allowed()
    return route


def _decode_query_value(raw: str) -> str:
    if not raw:
        raise _invalid_query()
    decoded = bytearray()
    index = 0
    while index < len(raw):
        character = raw[index]
        if character == "%":
            if (index + 2 >= len(raw) or raw[index + 1] not in "0123456789ABCDEF"
                    or raw[index + 2] not in "0123456789ABCDEF"):
                raise _invalid_query()
            byte = int(raw[index + 1:index + 3], 16)
            if chr(byte) in _UNRESERVED:
                raise _invalid_query()
            decoded.append(byte)
            index += 3
            continue
        if character not in _UNRESERVED:
            raise _invalid_query()
        decoded.extend(character.encode("ascii"))
        index += 1
    try:
        value = bytes(decoded).decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid_query() from None
    if any(ord(character) < 32 for character in value):
        raise _invalid_query()
    return value


def _canonical_int(value: str, *, minimum: int, maximum: int | None = None
                   ) -> str:
    if not _CANONICAL_INT.fullmatch(value):
        raise _invalid_query()
    try:
        number = int(value)
    except ValueError:
        raise _invalid_query() from None
    if number < minimum or (maximum is not None and number > maximum):
        raise _invalid_query()
    return str(number)


def _parse_query(raw_query: bytes, route: _Route,
                 limits: GatewayLimits) -> str:
    if not isinstance(raw_query, (bytes, bytearray)):
        raise _invalid_query()
    if len(raw_query) > limits.max_query_bytes:
        raise GatewayError("gateway_uri_too_long", 414)
    try:
        text = bytes(raw_query).decode("ascii")
    except UnicodeDecodeError:
        raise _invalid_query() from None
    if not text:
        return ""
    values: dict[str, str] = {}
    if text.endswith("&") or "&&" in text:
        raise _invalid_query()
    for part in text.split("&"):
        if not part or part.count("=") != 1:
            raise _invalid_query()
        key, raw_value = part.split("=", 1)
        if key in values or key not in route.allowed_query:
            raise _invalid_query()
        values[key] = _decode_query_value(raw_value)
    for key, value in values.items():
        if key == "after":
            values[key] = _canonical_int(value, minimum=0)
        elif key == "limit":
            values[key] = _canonical_int(value, minimum=1, maximum=500)
        elif key == "offset":
            values[key] = _canonical_int(value, minimum=0)
        elif key == "nonce":
            if (not 1 <= len(value) <= 128
                    or any(character not in _VISIBLE for character in value)):
                raise _invalid_query()
        else:
            raise _invalid_query()
    return "&".join(
        f"{key}={quote(value, safe='-._~', encoding='utf-8', errors='strict')}"
        for key, value in sorted(values.items()))


def _parse_inbound_headers(raw_headers: list[tuple[bytes, bytes]],
                           limits: GatewayLimits) -> _InboundHeaders:
    total = 0
    assertion_values: list[str] = []
    content_lengths: list[str] = []
    connection_tokens: set[str] = set()
    rows: list[tuple[str, str]] = []
    for raw_name, raw_value in raw_headers:
        total += len(raw_name) + len(raw_value) + 4
        if total > limits.max_header_bytes:
            raise GatewayError("gateway_headers_too_large", 431)
        try:
            name = raw_name.decode("ascii").lower()
            value = raw_value.decode("latin-1")
        except (UnicodeDecodeError, AttributeError):
            raise GatewayError("gateway_invalid_headers", 400) from None
        if (not name or any(ord(character) < 33 or ord(character) > 126
                            for character in name)
                or any((ord(character) < 32 and character != "\t")
                       or ord(character) == 127 for character in value)):
            raise GatewayError("gateway_invalid_headers", 400)
        rows.append((name, value))
        if name == GATEWAY_ASSERTION_HEADER.lower():
            assertion_values.append(value)
        elif name == "content-length":
            content_lengths.append(value)
        elif name == "transfer-encoding":
            raise GatewayError("gateway_framing_ambiguous", 400)
        elif name == "connection":
            for token in value.split(","):
                token = token.strip().lower()
                if token:
                    connection_tokens.add(token)
    if len(assertion_values) > 1:
        raise GatewayError("gateway_unauthorized", 401)
    if len(content_lengths) > 1:
        raise GatewayError("gateway_framing_ambiguous", 400)
    if connection_tokens & {"content-length", "transfer-encoding"}:
        raise GatewayError("gateway_framing_ambiguous", 400)
    content_length: int | None = None
    if content_lengths:
        raw_length = content_lengths[0]
        if not re.fullmatch(r"0|[1-9][0-9]*", raw_length):
            raise GatewayError("gateway_framing_ambiguous", 400)
        content_length = int(raw_length)
        if content_length > limits.max_body_bytes:
            raise GatewayError("gateway_body_too_large", 413)
    forwarded: dict[str, str] = {}
    for name, value in rows:
        if (name in _STRIP_REQUEST_HEADERS
                or name.startswith(("x-easel-", "x-forwarded-"))
                or name in connection_tokens):
            continue
        forwarded[name] = value
    assertion = assertion_values[0] if assertion_values else None
    return _InboundHeaders(assertion, content_length, forwarded,
                          frozenset(connection_tokens))


async def _read_body(request: Request, declared: int | None,
                     limits: GatewayLimits) -> bytes:
    if declared is not None and declared > limits.max_body_bytes:
        raise GatewayError("gateway_body_too_large", 413)
    chunks: list[bytes] = []
    total = 0

    async def consume() -> None:
        nonlocal total
        async for chunk in request.stream():
            if not chunk:
                continue
            total += len(chunk)
            if total > limits.max_body_bytes:
                raise GatewayError("gateway_body_too_large", 413)
            chunks.append(chunk)

    try:
        await asyncio.wait_for(consume(), timeout=limits.io_timeout)
    except asyncio.TimeoutError:
        raise GatewayError("gateway_request_timeout", 408) from None
    except ClientDisconnect:
        raise GatewayError("gateway_body_incomplete", 400) from None
    body = b"".join(chunks)
    if declared is not None and len(body) != declared:
        raise GatewayError("gateway_framing_ambiguous", 400)
    return body


def _clean_request_headers(headers: httpx.Headers,
                           connection_tokens: frozenset[str]) -> None:
    tokens = set(connection_tokens)
    for value in headers.get_list("connection"):
        for token in value.split(","):
            token = token.strip().lower()
            if token:
                tokens.add(token)
    for name in {key.lower() for key in headers}:
        if (name in _STRIP_REQUEST_HEADERS
                or name.startswith(("x-easel-", "x-forwarded-"))
                or name in tokens):
            try:
                del headers[name]
            except KeyError:
                pass


def _clean_response_headers(headers: httpx.Headers) -> dict[str, str]:
    connection_tokens: set[str] = set()
    for name, value in headers.multi_items():
        if name.lower() == "connection":
            for token in value.split(","):
                token = token.strip().lower()
                if token:
                    connection_tokens.add(token)
    cleaned: dict[str, str] = {}
    for name, value in headers.multi_items():
        lowered = name.lower()
        if (lowered in _STRIP_RESPONSE_HEADERS
                or lowered in connection_tokens):
            continue
        cleaned[lowered] = value
    # 缓冲响应由 Starlette 重新计算长度；流式响应保留上游长度值。
    if "content-length" in cleaned:
        cleaned.pop("content-length", None)
    return cleaned


def _stream_response_headers(headers: httpx.Headers) -> dict[str, str]:
    connection_tokens: set[str] = set()
    for name, value in headers.multi_items():
        if name.lower() == "connection":
            for token in value.split(","):
                token = token.strip().lower()
                if token:
                    connection_tokens.add(token)
    cleaned: dict[str, str] = {}
    for name, value in headers.multi_items():
        lowered = name.lower()
        if (lowered in _STRIP_RESPONSE_HEADERS
                or lowered in connection_tokens):
            continue
        cleaned[lowered] = value
    return cleaned


class GatewayController:
    """单实例 gateway 控制器；activate 之前拒绝所有管理请求。"""

    def __init__(self, config: GatewayConfig, public_key: Ed25519PublicKey,
                 *, clock: Callable[[], float] = time.time,
                 limits: GatewayLimits | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 http_client: httpx.AsyncClient | None = None,
                 activated: bool = False,
                 nonce_capacity: int = DEFAULT_NONCE_CAPACITY) -> None:
        if not isinstance(config, GatewayConfig):
            raise TypeError("config must be GatewayConfig")
        if not isinstance(public_key, Ed25519PublicKey):
            raise TypeError("public_key must be Ed25519PublicKey")
        if transport is not None and http_client is not None:
            raise ValueError("transport and http_client are mutually exclusive")
        self._config = config
        self._public_key = public_key
        self._clock = clock
        self._limits = limits or GatewayLimits()
        self._nonces = GatewayNonceCache(nonce_capacity)
        self._slots = _ConcurrencyLimiter(
            self._limits.max_concurrent_requests,
            self._limits.max_concurrent_streams)
        self._active = bool(activated)
        self._owns_client = http_client is None
        if http_client is not None:
            self._client = http_client
        else:
            timeout = httpx.Timeout(
                self._limits.io_timeout,
                connect=self._limits.connect_timeout,
                read=self._limits.io_timeout,
                write=self._limits.io_timeout,
                pool=self._limits.connect_timeout)
            self._client = httpx.AsyncClient(
                transport=transport, timeout=timeout,
                follow_redirects=False, trust_env=False)

    @property
    def active(self) -> bool:
        return self._active

    @property
    def active_requests(self) -> int:
        return self._slots.active

    @property
    def active_streams(self) -> int:
        return self._slots.active_streams

    def activate(self) -> None:
        self._active = True

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def handle(self, request: Request) -> Response:
        try:
            return await self._dispatch(request)
        except GatewayError as error:
            return _error_response(error)
        except Exception:  # noqa: BLE001 - 不回显内部异常/秘密
            LOGGER.error("gateway 内部错误")
            return JSONResponse({"error": "gateway_internal"},
                                status_code=500,
                                headers={"Cache-Control": "no-store"})

    def _now(self) -> int:
        value = self._clock()
        if type(value) not in (int, float):
            return 0
        return int(value)

    async def _dispatch(self, request: Request) -> Response:
        if not self._active:
            raise GatewayError("gateway_inactive", 503)
        method = str(request.scope.get("method", "")).upper()
        raw_path = request.scope.get("raw_path")
        if not isinstance(raw_path, (bytes, bytearray)):
            # 不退回已解码 path：否则编码 slash 可能在非合规 ASGI 实现中
            # 被还原成普通分隔符，绕过精确路由。
            raise _invalid_path()
        route = _match_path(bytes(raw_path), method, self._limits)
        canonical_query = _parse_query(
            request.scope.get("query_string", b""), route, self._limits)
        parsed_headers = _parse_inbound_headers(
            list(request.scope.get("headers", [])), self._limits)
        if parsed_headers.assertion is not None \
                and len(parsed_headers.assertion) > self._limits.max_assertion_bytes:
            raise GatewayError("gateway_unauthorized", 401)
        slot = self._slots.acquire(route.stream)
        streaming = False
        try:
            body = await _read_body(
                request, parsed_headers.content_length, self._limits)
            now = self._now()
            assertion = verify_gateway_assertion(
                self._public_key, parsed_headers.assertion or "",
                audience=self._config.audience, method=method,
                path=route.path, canonical_query=canonical_query,
                body_sha256=hashlib.sha256(body).hexdigest(), now=now)
            self._nonces.accept(
                assertion.nonce, expires_at=assertion.expires_at, now=now)
            response = await self._send_upstream(
                route, canonical_query, parsed_headers, body)
            if 300 <= response.status_code < 400:
                await response.aclose()
                raise GatewayError("gateway_upstream_redirect", 502)
            if response.is_stream_consumed:
                await response.aclose()
                raise GatewayError("gateway_upstream_unavailable", 502)
            if route.stream or _is_streaming_response(response):
                try:
                    streamed = self._streaming_response(response, slot)
                except BaseException:
                    await response.aclose()
                    raise
                streaming = True
                return streamed
            return await self._buffered_response(response)
        finally:
            if not streaming:
                await slot.release()

    async def _send_upstream(self, route: _Route, canonical_query: str,
                             headers: _InboundHeaders,
                             body: bytes) -> httpx.Response:
        url = self._config.worker_origin + route.path
        if canonical_query:
            url += "?" + canonical_query
        host = urlsplit(self._config.worker_origin).netloc
        try:
            request = self._client.build_request(
                route.method, url, headers=headers.forwarded, content=body)
            _clean_request_headers(request.headers, headers.connection_tokens)
            request.headers["Host"] = host
            if body or route.method == "POST":
                request.headers["Content-Length"] = str(len(body))
            else:
                try:
                    del request.headers["Content-Length"]
                except KeyError:
                    pass
            request.headers[WORKSPACE_HEADER] = self._config.workspace_id
            request.headers[SERVICE_TOKEN_HEADER] = \
                self._config.worker_service_token
            return await self._client.send(
                request, stream=True, auth=_NO_AUTH,
                follow_redirects=False)
        except httpx.TimeoutException:
            raise GatewayError("gateway_upstream_timeout", 504) from None
        except httpx.HTTPError:
            raise GatewayError("gateway_upstream_unavailable", 502) from None

    async def _buffered_response(self, response: httpx.Response) -> Response:
        headers = _clean_response_headers(response.headers)
        chunks: list[bytes] = []

        async def collect() -> None:
            async for chunk in response.aiter_raw():
                chunks.append(chunk)

        try:
            await asyncio.wait_for(collect(), timeout=self._limits.io_timeout)
        except asyncio.TimeoutError:
            raise GatewayError("gateway_upstream_timeout", 504) from None
        except httpx.HTTPError:
            raise GatewayError("gateway_upstream_unavailable", 502) from None
        finally:
            await response.aclose()
        return Response(content=b"".join(chunks),
                        status_code=response.status_code, headers=headers)

    def _streaming_response(self, response: httpx.Response,
                            slot: _Slot) -> Response:
        headers = _stream_response_headers(response.headers)

        async def body_stream() -> AsyncIterator[bytes]:
            try:
                iterator = response.aiter_raw()
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            iterator.__anext__(), timeout=self._limits.io_timeout)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        LOGGER.error("gateway 上游流读取超时")
                        break
                    except httpx.HTTPError:
                        LOGGER.error("gateway 上游流读取失败")
                        break
                    yield chunk
            finally:
                try:
                    await response.aclose()
                finally:
                    await slot.release()

        async def cleanup() -> None:
            # generator 可能尚未开始迭代就被客户端断开；后台任务兜底关闭
            # 上游响应并释放槽位（与 generator finally 幂等）。
            try:
                await response.aclose()
            finally:
                await slot.release()

        return StreamingResponse(
            body_stream(), status_code=response.status_code, headers=headers,
            background=BackgroundTask(cleanup))


def _is_streaming_response(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "").strip().lower()
    return content_type.startswith("text/event-stream")


@asynccontextmanager
async def _lifespan(controller: GatewayController):
    try:
        yield
    finally:
        await controller.aclose()


def create_gateway_app(config: GatewayConfig, public_key: Ed25519PublicKey,
                       *, clock: Callable[[], float] | None = None,
                       limits: GatewayLimits | None = None,
                       transport: httpx.AsyncBaseTransport | None = None,
                       http_client: httpx.AsyncClient | None = None,
                       activated: bool = False,
                       nonce_capacity: int = DEFAULT_NONCE_CAPACITY) -> FastAPI:
    """创建独立 gateway ASGI 应用；默认未激活，需显式 activate。"""
    if clock is None:
        clock = time.time
    controller = GatewayController(
        config, public_key, clock=clock, limits=limits,
        transport=transport, http_client=http_client,
        activated=activated, nonce_capacity=nonce_capacity)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with _lifespan(controller):
            yield

    app = FastAPI(title="Easel Gateway", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.gateway = controller
    app.state.gateway_controller = controller

    @app.middleware("http")
    async def dispatch(request: Request, call_next):
        return await controller.handle(request)

    return app
