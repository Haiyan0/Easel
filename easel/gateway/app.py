"""独立 gateway ASGI 应用：精确路由、固定 worker 绑定与流式清理。

gateway 只接受已验签的 control 断言；所有目标、身份头和代次都来自受信
``GatewayConfig``。普通请求不能提供 worker 地址、Host 或内部凭证。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from urllib.parse import quote, unquote, urlsplit

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
    verify_gateway_assertion_metadata,
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
    methods: frozenset[str]
    path: str
    stream: bool
    allowed_query: frozenset[str]


_ROUTES: dict[str, _Route] = {
    "health": _Route("health", frozenset({"GET"}),
                     "/internal/lifecycle/health", False, frozenset()),
    "submit": _Route("submit", frozenset({"POST"}), "", False, frozenset()),
    "events": _Route("events", frozenset({"GET"}), "", True,
                     frozenset({"after", "nonce"})),
    "stop": _Route("stop", frozenset({"POST"}), "", False, frozenset()),
    "answer": _Route("answer", frozenset({"POST"}), "", False, frozenset()),
    "status": _Route("status", frozenset({"POST"}), "", False, frozenset()),
    "messages": _Route("messages", frozenset({"GET"}), "", False,
                       frozenset({"limit", "offset"})),
}

# 基础工作区/工作台路由只覆盖 Task 5A/5B 已实现的精确入口；skills/accounts
# 等未接组不在 gateway 放行，避免用前缀代理扩大边界。
_WS_OUTPUT_INTERNAL = frozenset({"analytics", "wechat"})
_WS_RECORD_ID = re.compile(r"^[0-9a-f]{12}$")
_WS_TREND_PLATFORMS = re.compile(
    r"^(?:weibo|douyin|zhihu|bilibili|baidu|toutiao)"
    r"(?:,(?:weibo|douyin|zhihu|bilibili|baidu|toutiao))*$")
_WS_SIMPLE: dict[tuple[str, ...], _Route] = {
    ("api", "status"): _Route("ws_status", frozenset({"GET"}),
                              "/api/status", False, frozenset()),
    ("api", "personas"): _Route("ws_personas", frozenset({"GET"}),
                                "/api/personas", False, frozenset()),
    ("api", "outputs"): _Route("ws_outputs", frozenset({"GET"}),
                               "/api/outputs", False, frozenset()),
    ("api", "upload"): _Route("ws_upload", frozenset({"POST"}),
                              "/api/upload", False, frozenset()),
    ("api", "upload", "limits"): _Route(
        "ws_upload_limits", frozenset({"GET"}), "/api/upload/limits",
        False, frozenset()),
    ("api", "artifacts", "archive"): _Route(
        "ws_archive", frozenset({"GET"}), "/api/artifacts/archive",
        False, frozenset({"path"})),
    ("api", "trends"): _Route(
        "ws_trends", frozenset({"GET"}), "/api/trends", False,
        frozenset({"platforms", "limit"})),
    ("api", "schedule"): _Route(
        "ws_schedule", frozenset({"GET", "POST"}), "/api/schedule", False,
        frozenset()),
    ("api", "schedule", "context"): _Route(
        "ws_schedule_context", frozenset({"GET"}), "/api/schedule/context",
        False, frozenset({"days"})),
    ("api", "ideas"): _Route(
        "ws_ideas", frozenset({"GET", "POST"}), "/api/ideas", False,
        frozenset()),
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

    def isolate(self) -> None:
        self._limiter.isolate()


class _CleanupState(Enum):
    RUNNING = "running"
    CLOSED = "closed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class _ResponseCleanup:
    """每请求唯一的上游关闭任务；所有等待者共享同一完成终态。

    httpx 的 ``response.is_closed`` 会在底层 ``stream.aclose`` 完成前置真；
    因此这里不把它当关闭完成证据，也不让读取协程取得 EOF 关闭所有权。
    只有本任务的 ``response.aclose`` 成功后才释放槽位；关闭失败或取消时
    保留失败终态、隔离实例并继续占用槽位。等待者取消不会取消这个任务。
    """

    def __init__(self, response: httpx.Response, slot: _Slot) -> None:
        self.response = response
        self._slot = slot
        self._state = _CleanupState.RUNNING
        self._task: asyncio.Task | None = None

    def start(self) -> asyncio.Task:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
            self._task.add_done_callback(self._retrieve_result)
        return self._task

    async def close(self) -> None:
        task = self.start()
        await asyncio.shield(task)

    @staticmethod
    def _retrieve_result(task: asyncio.Task) -> None:
        # 取消路径只 start() 不等待；这里取回异常，避免后台静默丢失。
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        try:
            await self.response.aclose()
        except asyncio.CancelledError:
            self._state = _CleanupState.CANCELLED
            self._slot.isolate()
            LOGGER.error("gateway 上游响应关闭被取消，实例已隔离")
            raise
        except Exception:  # noqa: BLE001 - 关闭失败必须进入失败终态
            self._state = _CleanupState.FAILED
            self._slot.isolate()
            LOGGER.error("gateway 上游响应关闭失败，实例已隔离")
            # 不链接底层异常：其中可能含地址、token 或响应内容。
            raise GatewayError(
                "gateway_upstream_close_failed", 502) from None
        else:
            self._state = _CleanupState.CLOSED
            await self._slot.release()


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
        self._isolated = False

    def acquire(self, stream: bool) -> _Slot:
        if self._isolated:
            # 关闭失败后上游资源是否释放未知，禁收新请求；直到实例重启。
            raise GatewayError("gateway_unavailable", 503)
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

    def isolate(self) -> None:
        self._isolated = True

    @property
    def isolated(self) -> bool:
        return self._isolated

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


def _workspace_segment(segment: str, *, persona: bool) -> str:
    """严格解码一个工作区路径段并返回规范编码形式。

    gateway 是第二道边界：拒绝编码 slash/反斜杠/NUL、点段、编码 unreserved
    别名与内部保留名；最终安全路径仍由 worker 的既有路径工具复核。
    """
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
                # 拒绝 %61 这类与字面量同义的编码别名。
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
    if (not value or value in (".", "..")
            or any(character in value for character in "/\\:\x00\r\n")
            or any(ord(character) < 32 or ord(character) == 127
                   for character in value)
            or len(value) > 255
            or value.startswith(".")):
        raise _invalid_path()
    if persona:
        if (value.startswith("_") or len(value) > 200
                or value != value.strip()):
            raise _invalid_path()
    return quote(value, safe="-._~", encoding="utf-8", errors="strict")


def _workspace_record_id(segment: str) -> str:
    """工作台记录 id 只接受 12 位小写十六进制，不允许路径别名。"""
    encoded = _workspace_segment(segment, persona=False)
    value = unquote(encoded, encoding="utf-8", errors="strict")
    if not _WS_RECORD_ID.fullmatch(value):
        raise _invalid_path()
    return encoded


def _workspace_output_path(parts: list[str]) -> str:
    if not parts:
        raise _invalid_path()
    first = _workspace_segment(parts[0], persona=False)
    decoded_first = unquote(first, encoding="utf-8", errors="strict")
    if (decoded_first.startswith("_")
            or decoded_first in _WS_OUTPUT_INTERNAL):
        raise _invalid_path()
    canonical = [first]
    for part in parts[1:]:
        canonical.append(_workspace_segment(part, persona=False))
    return "/".join(canonical)


def _workspace_route(parts: list[str],
                     method: str) -> _Route | None:
    key = tuple(parts)
    simple = _WS_SIMPLE.get(key)
    if simple is not None:
        return simple
    if len(parts) == 3 and parts[1] == "persona":
        name = _workspace_segment(parts[2], persona=True)
        return _Route("ws_persona", frozenset({"GET", "DELETE"}),
                      f"/api/persona/{name}", False, frozenset())
    if (len(parts) == 4 and parts[1] == "persona"
            and parts[3] in ("files", "file")):
        name = _workspace_segment(parts[2], persona=True)
        if parts[3] == "files":
            return _Route("ws_persona_files", frozenset({"GET"}),
                          f"/api/persona/{name}/files", False, frozenset())
        return _Route("ws_persona_file", frozenset({"PUT"}),
                      f"/api/persona/{name}/file", False, frozenset())
    if len(parts) == 3 and parts[1] in ("schedule", "ideas"):
        record = _workspace_record_id(parts[2])
        name = "ws_schedule_item" if parts[1] == "schedule" \
            else "ws_idea_item"
        return _Route(name, frozenset({"PUT", "DELETE"}),
                      f"/api/{parts[1]}/{record}", False, frozenset())
    if len(parts) >= 3 and parts[1] in ("output", "media"):
        path = _workspace_output_path(parts[2:])
        if parts[1] == "output":
            return _Route("ws_output", frozenset({"GET", "DELETE"}),
                          f"/api/output/{path}", False, frozenset())
        return _Route("ws_media", frozenset({"GET", "HEAD"}),
                      f"/api/media/{path}", True, frozenset())
    return None


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
    if parts and parts[0] == "internal":
        for index, segment in enumerate(parts):
            if "%" in segment and index != question_index:
                raise _invalid_path()
    if parts and parts[0] == "api":
        route = _workspace_route(parts, method)
        if route is None:
            raise _not_found()
    elif len(parts) == 3 and parts[0] == "internal" \
            and parts[1] == "lifecycle" and parts[2] == "health":
        route = _ROUTES["health"]
    elif len(parts) == 3 and parts[0] == "internal" and parts[1] == "runs":
        run_id = _run_or_session_id(parts[2])
        route = _Route("submit", frozenset({"POST"}),
                       f"/internal/runs/{run_id}", False, frozenset())
    elif len(parts) == 4 and parts[0] == "internal" and parts[1] == "runs":
        run_id = _run_or_session_id(parts[2])
        if parts[3] == "events":
            route = _Route("events", frozenset({"GET"}),
                           f"/internal/runs/{run_id}/events", True,
                           _ROUTES["events"].allowed_query)
        elif parts[3] == "stop":
            route = _Route("stop", frozenset({"POST"}),
                           f"/internal/runs/{run_id}/stop", False,
                           frozenset())
        else:
            raise _not_found()
    elif len(parts) == 5 and parts[0] == "internal" and parts[1] == "runs":
        run_id = _run_or_session_id(parts[2])
        if parts[3] != "questions":
            raise _not_found()
        if parts[4] == "status":
            route = _Route("status", frozenset({"POST"}),
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
            "answer", frozenset({"POST"}),
            f"/internal/runs/{run_id}/questions/{question_id}/answer",
            False, frozenset())
    elif (len(parts) == 4 and parts[0] == "internal"
          and parts[1] == "sessions" and parts[3] == "messages"):
        session_id = _run_or_session_id(parts[2])
        route = _Route(
            "messages", frozenset({"GET"}),
            f"/internal/sessions/{session_id}/messages", False,
            _ROUTES["messages"].allowed_query)
    else:
        raise _not_found()
    if method not in route.methods:
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
        elif key == "path":
            if (not 1 <= len(value) <= 2048
                    or value.startswith(("/", "\\"))
                    or "\\" in value or ":" in value
                    or any(ord(character) < 32 or ord(character) == 127
                           for character in value)):
                raise _invalid_query()
            parts = value.split("/")
            if any(not part or part in (".", "..")
                   or part.startswith(".") for part in parts):
                raise _invalid_query()
            if parts[0].startswith("_") or parts[0] in _WS_OUTPUT_INTERNAL:
                raise _invalid_query()
        elif key == "platforms":
            if (not 1 <= len(value) <= 200
                    or not _WS_TREND_PLATFORMS.fullmatch(value)):
                raise _invalid_query()
        elif key == "days":
            values[key] = _canonical_int(value, minimum=1, maximum=90)
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
        # 先按十进制位数有界比较，避免超长数字触发 int 转换限制/异常。
        if len(raw_length) > len(str(limits.max_body_bytes)):
            raise GatewayError("gateway_body_too_large", 413)
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

    @property
    def isolated(self) -> bool:
        """关闭失败后实例已隔离；状态接口不能只读 active 标志。"""
        return self._slots.isolated

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
        # 先验签并绑定固定请求元数据，再读取 body、占用受保护槽位。
        # 未认证慢 body / 错误 audience / 过期断言在这里快速失败。
        assertion = verify_gateway_assertion_metadata(
            self._public_key, parsed_headers.assertion or "",
            audience=self._config.audience, method=method,
            path=route.path, canonical_query=canonical_query,
            now=self._now())
        slot = self._slots.acquire(route.stream)
        guard: _ResponseCleanup | None = None
        streaming = False
        cancelled = False
        try:
            body = await _read_body(
                request, parsed_headers.content_length, self._limits)
            now = self._now()
            if assertion.expires_at <= now:
                # 慢 body 不能延长已验签断言的有效期。
                raise GatewayError("gateway_unauthorized", 401)
            actual_digest = hashlib.sha256(body).hexdigest()
            if not hmac.compare_digest(
                    assertion.body_sha256, actual_digest):
                raise GatewayError("gateway_forbidden", 403)
            self._nonces.accept(
                assertion.nonce, expires_at=assertion.expires_at, now=now)
            response = await self._send_upstream(
                route, method, canonical_query, parsed_headers, body)
            guard = _ResponseCleanup(response, slot)
            # 304 是合法条件响应，不是重定向；其它 3xx 仍失败关闭。
            if (300 <= response.status_code < 400
                    and response.status_code != 304):
                await guard.close()
                raise GatewayError("gateway_upstream_redirect", 502)
            if response.is_stream_consumed:
                await guard.close()
                raise GatewayError("gateway_upstream_unavailable", 502)
            if method == "HEAD":
                headers = _stream_response_headers(response.headers)
                await guard.close()
                return Response(status_code=response.status_code,
                                headers=headers)
            if response.status_code == 304:
                headers = _clean_response_headers(response.headers)
                await guard.close()
                return Response(status_code=304, headers=headers)
            if route.stream or _is_streaming_response(response):
                try:
                    streamed = self._streaming_response(guard)
                except BaseException:
                    await guard.close()
                    raise
                streaming = True
                return streamed
            return await self._buffered_response(guard)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if guard is None:
                await slot.release()
            elif not streaming:
                # 取消路径只启动唯一 cleanup，不在已取消任务里等待慢关闭；
                # 不取消 cleanup 任务，槽位由它完成后释放。
                if cancelled:
                    guard.start()
                else:
                    await guard.close()

    async def _send_upstream(self, route: _Route, method: str,
                             canonical_query: str,
                             headers: _InboundHeaders,
                             body: bytes) -> httpx.Response:
        url = self._config.worker_origin + route.path
        if canonical_query:
            url += "?" + canonical_query
        host = urlsplit(self._config.worker_origin).netloc
        try:
            expected_url = httpx.URL(url)
            request = self._client.build_request(
                method, url, headers=headers.forwarded, content=body)
            # build_request 会合并注入 client 的默认 params；必须在最终
            # Request 上恢复已验签的精确 URL，不能只清洗 headers。
            request.url = expected_url
            _clean_request_headers(request.headers, headers.connection_tokens)
            request.headers["Host"] = host
            if body or method == "POST":
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

    async def _buffered_response(self, guard: _ResponseCleanup) -> Response:
        response = guard.response
        headers = _clean_response_headers(response.headers)
        chunks: list[bytes] = []

        async def collect() -> None:
            # 直接消费原始 AsyncByteStream；aiter_raw 会在自然 EOF 自行
            # 取得首次 aclose 所有权，关闭可能被取消截断。
            iterator = response.stream.__aiter__()
            while True:
                try:
                    chunk = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                chunks.append(chunk)

        cancelled = False
        try:
            await asyncio.wait_for(collect(), timeout=self._limits.io_timeout)
        except asyncio.CancelledError:
            cancelled = True
            guard.start()
            raise
        except asyncio.TimeoutError:
            raise GatewayError("gateway_upstream_timeout", 504) from None
        except httpx.HTTPError:
            raise GatewayError("gateway_upstream_unavailable", 502) from None
        finally:
            if not cancelled:
                await guard.close()
        return Response(content=b"".join(chunks),
                        status_code=response.status_code, headers=headers)

    def _streaming_response(self, guard: _ResponseCleanup) -> Response:
        response = guard.response
        headers = _stream_response_headers(response.headers)

        async def body_stream() -> AsyncIterator[bytes]:
            try:
                iterator = response.stream.__aiter__()
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
                # 唯一 cleanup 任务负责关闭与释放槽位；等待者取消只
                # 取消本生成器，不取消 cleanup。
                await guard.close()

        return StreamingResponse(
            body_stream(), status_code=response.status_code, headers=headers,
            background=BackgroundTask(guard.close))


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
