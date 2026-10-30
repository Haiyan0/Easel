"""多人控制面的最小 HTTP 边界。

本模块只实现 Task 2 的认证闭环：健康检查、邀请兑换、身份读取和注销。
未知路由不做任何回落；文档接口默认关闭，避免额外公开入口。

SQLite 连接有线程归属：应用在 lifespan 中用一个专用数据库线程打开自己的
Store，并把所有数据库操作提交到该线程执行。这样既不跨线程复用连接，也不
需要关闭 ``check_same_thread``。
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import ipaddress
import json
import re
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, JSONResponse, Response,
                               StreamingResponse)
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..providers.openai import BROKER_ROUTE_TABLE
from ..worker.sessions import MAX_MESSAGE_BYTES
from .admin import AdminService
from .auth import AuthService
from .config import ConfigService
from .policy import authorize, public_route
from .runtime import RuntimeManager
from .sessions import SessionService
from .store import Store
from .stream import (AuthGuardedStreamingResponse,
                     DEFAULT_AUTH_CHECK_SECONDS, run_event_stream)
from .types import DomainError, Identity

SESSION_COOKIE = "__Host-easel_session"
"""__Host- 前缀要求 Secure、Path=/ 且无 Domain。"""

_INTERNAL_BROKER_PREFIX = "/internal/broker/"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_IMPLEMENTED_EXACT = frozenset({
    ("GET", "/api/health"),
    ("GET", "/api/runtime"),
    ("POST", "/api/auth/exchange"),
    ("GET", "/api/auth/me"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/admin/managed-config"),
    ("POST", "/api/admin/managed-config"),
    ("GET", "/api/admin/users"),
    ("POST", "/api/admin/users"),
    ("GET", "/api/admin/invites"),
    ("POST", "/api/admin/invites"),
    ("GET", "/api/settings/models"),
    ("GET", "/api/settings/harness"),
    ("POST", "/api/settings/models/save"),
    ("GET", "/api/sessions"),
    ("POST", "/api/sessions"),
    ("POST", "/api/chat"),
    ("POST", "/api/chat/stream"),
    ("POST", "/api/chat/stop"),
    ("POST", "/api/chat/question/answer"),
    ("POST", "/api/chat/question/status"),
    # Task 5A：基础工作区（status/画像/内容/媒体/上传）
    ("GET", "/api/status"),
    ("GET", "/api/personas"),
    # Task 5C1：技能目录与受管任务入口
    ("GET", "/api/skills"),
    ("POST", "/api/skill"),
    ("POST", "/api/profile/build"),
    ("GET", "/api/outputs"),
    ("GET", "/api/artifacts/archive"),
    ("GET", "/api/upload/limits"),
    ("POST", "/api/upload"),
    # Task 5B：工作台数据（热点/选题/日历/概览）
    ("GET", "/api/trends"),
    ("GET", "/api/schedule"),
    ("POST", "/api/schedule"),
    ("GET", "/api/schedule/context"),
    ("GET", "/api/ideas"),
    ("POST", "/api/ideas"),
})
_IMPLEMENTED_PATTERNS = (
    ("DELETE", re.compile(r"^/api/settings/models/overrides/[^/]+$")),
    ("PATCH", re.compile(r"^/api/admin/users/[^/]+$")),
    ("POST", re.compile(r"^/api/admin/users/[^/]+/revoke-sessions$")),
    ("DELETE", re.compile(r"^/api/admin/invites/[^/]+$")),
    ("PATCH", re.compile(r"^/api/sessions/[^/]+$")),
    ("DELETE", re.compile(r"^/api/sessions/[^/]+$")),
    ("GET", re.compile(r"^/api/sessions/[^/]+/messages$")),
    ("GET", re.compile(r"^/api/sessions/[^/]+/runs$")),
    ("GET", re.compile(r"^/api/chat/jobs/[^/]+/stream$")),
    ("GET", re.compile(r"^/api/persona/[^/]+$")),
    ("DELETE", re.compile(r"^/api/persona/[^/]+$")),
    ("GET", re.compile(r"^/api/persona/[^/]+/files$")),
    ("PUT", re.compile(r"^/api/persona/[^/]+/file$")),
    ("GET", re.compile(r"^/api/skill/[^/]+$")),
    ("GET", re.compile(r"^/api/skill/status/[^/]+$")),
    ("GET", re.compile(r"^/api/profile/build/status/[^/]+$")),
    ("GET", re.compile(r"^/api/output/.+$")),
    ("DELETE", re.compile(r"^/api/output/.+$")),
    ("GET", re.compile(r"^/api/media/.+$")),
    ("HEAD", re.compile(r"^/api/media/.+$")),
    ("PUT", re.compile(r"^/api/schedule/[^/]+$")),
    ("DELETE", re.compile(r"^/api/schedule/[^/]+$")),
    ("PUT", re.compile(r"^/api/ideas/[^/]+$")),
    ("DELETE", re.compile(r"^/api/ideas/[^/]+$")),
    ("GET", re.compile(r"^/$")),
    ("GET", re.compile(r"^/onepage$")),
    ("GET", re.compile(r"^/assets/[^/].*$")),
    ("GET", re.compile(r"^/static/[^/].*$")),
    ("GET", re.compile(r"^/favicon\.svg$")),
    ("GET", re.compile(r"^/icons\.svg$")),
)
"""control 本轮真正装配的路由；其余已分类业务显式 503。"""

_MEMBER_CAPABILITIES = ("workspace",)
_ADMIN_CAPABILITIES = ("workspace", "admin")

_MAX_WORKSPACE_BODY = 60 * 1024 * 1024
_MEDIA_HEADERS = frozenset({
    "content-type", "content-length", "content-range", "content-disposition",
    "accept-ranges", "etag", "last-modified", "cache-control",
    "content-security-policy",
})
_RANGE_RE = re.compile(
    r"^bytes=(?:[0-9]*-[0-9]*)(?:,\s*[0-9]*-[0-9]*)*$")
_ETAG_RE = re.compile(r'^(W/)?"([^"\r\n]+)"$')


def _no_store_json(payload: object, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload, status_code=status_code,
        headers={"Cache-Control": "no-store"})


def _workspace_scope_tag(workspace_id: str) -> str:
    return "easel-" + hashlib.sha256(
        workspace_id.encode("utf-8")).hexdigest()[:12] + "-"


def _scope_etag(value: str, prefix: str) -> str | None:
    match = _ETAG_RE.fullmatch(value.strip())
    if match is None:
        return None
    weak = match.group(1) or ""
    return f'{weak}"{prefix}{match.group(2)}"'


def _unscope_etag_list(value: str, prefix: str) -> str | None:
    """把浏览器回传的 workspace 作用域 ETag 列表还原为 worker 值。

    任意一项不带本 workspace 前缀（含 ``*`` 或日期）时整头丢弃，宁可 200
    让浏览器重新取，也不能拿另一身份的条件请求触发 304。
    """
    tags: list[str] = []
    position = 0
    text = value.strip()
    while position < len(text):
        match = re.match(r'\s*(W/)?"([^"]*)"\s*', text[position:])
        if match is None:
            return None
        weak = match.group(1) or ""
        inner = match.group(2)
        if not inner.startswith(prefix):
            return None
        tags.append(f'{weak}"{inner[len(prefix):]}"')
        position += match.end()
        if position == len(text):
            break
        if text[position] != ",":
            return None
        position += 1
    return ", ".join(tags) if tags else None


def _media_request_headers(request: Request, workspace_id: str
                           ) -> dict[str, str]:
    headers: dict[str, str] = {}
    range_value = request.headers.get("range")
    if range_value is not None:
        if (len(range_value) > 1024
                or _RANGE_RE.fullmatch(range_value) is None):
            raise DomainError("invalid_request", 400)
        headers["Range"] = range_value
    if_none_match = request.headers.get("if-none-match")
    if if_none_match is not None:
        unscoped = _unscope_etag_list(
            if_none_match, _workspace_scope_tag(workspace_id))
        if unscoped is not None:
            headers["If-None-Match"] = unscoped
    if_range = request.headers.get("if-range")
    if if_range is not None:
        # 只透传已带本 workspace 前缀的 ETag；日期条件会跨身份误命中。
        original = _unscope_etag_list(
            if_range, _workspace_scope_tag(workspace_id))
        if original is not None:
            headers["If-Range"] = original
    return headers


def _media_response_headers(headers: Mapping[str, str],
                            workspace_id: str) -> dict[str, str]:
    result = {
        str(name).lower(): str(value)
        for name, value in headers.items()
        if str(name).lower() in _MEDIA_HEADERS
    }
    prefix = _workspace_scope_tag(workspace_id)
    etag = result.get("etag")
    if etag is not None:
        scoped = _scope_etag(etag, prefix)
        if scoped is None:
            result.pop("etag", None)
        else:
            result["etag"] = scoped
    content_type = result.get("content-type", "").lower()
    result["cache-control"] = (
        "private, no-store" if content_type.startswith("text/html")
        else "private, no-cache")
    result["vary"] = "Cookie"
    result["x-content-type-options"] = "nosniff"
    return result


async def _workspace_body(request: Request,
                          maximum: int = _MAX_WORKSPACE_BODY) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise DomainError("invalid_request", 400) from None
        if length < 0:
            raise DomainError("invalid_request", 400)
        if length > maximum:
            raise DomainError("payload_too_large", 413)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > maximum:
            raise DomainError("payload_too_large", 413)
        chunks.append(chunk)
    if total == 0:
        raise DomainError("invalid_request", 400)
    return b"".join(chunks)


def _workbench_query_value(request: Request, name: str) -> str | None:
    values = request.query_params.getlist(name)
    if len(values) > 1:
        raise DomainError("invalid_request", 400)
    return values[0] if values else None


def _workbench_int(request: Request, name: str) -> int | None:
    raw = _workbench_query_value(request, name)
    if raw is None:
        return None
    if not re.fullmatch(r"[0-9]+", raw):
        raise DomainError("invalid_request", 400)
    return int(raw)


def _reject_json_constant(_value: str):
    raise ValueError("non-finite json number")


def _workbench_json_payload(raw: bytes) -> dict:
    try:
        payload = json.loads(raw, parse_constant=_reject_json_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise DomainError("invalid_request", 400) from None
    if not isinstance(payload, dict):
        raise DomainError("invalid_request", 400)
    return payload

_HOST_LABEL_PATTERN = re.compile(r"^[a-z0-9-]+$")


def _normalize_public_origin(value: str) -> str:
    """校验并规范化公共 HTTPS Origin。

    只接受浏览器 Origin 的合法形状：https、主机/可选端口，无用户信息、路径
    （单个根斜杠可规范化）、query 或 fragment。默认端口 443 省略，主机名统一
    小写，避免配置值与浏览器发送的 Origin 无谓不一致。
    """
    if not isinstance(value, str) or not value:
        raise ValueError("public_origin 必须显式配置")
    if any(char.isspace() for char in value) or "\\" in value:
        raise ValueError("public_origin 不得包含空白或反斜杠")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("public_origin 不是合法 URI") from error
    if parsed.scheme.lower() != "https":
        raise ValueError("public_origin 必须使用 https")
    if parsed.netloc.endswith(":"):
        raise ValueError("public_origin 端口非法")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("public_origin 不得包含用户信息")
    if parsed.query or parsed.fragment:
        raise ValueError("public_origin 不得包含 query/fragment")
    if parsed.path not in ("", "/"):
        raise ValueError("public_origin 不得包含路径")
    host = parsed.hostname
    if not host:
        raise ValueError("public_origin 缺少主机")
    if not host.isascii():
        raise ValueError("public_origin 主机必须为 ASCII")
    if ":" in host:
        try:
            address = ipaddress.ip_address(host)
        except ValueError as error:
            raise ValueError("public_origin IPv6 主机非法") from error
        if address.version != 6:
            raise ValueError("public_origin 主机非法")
    else:
        labels = host.rstrip(".").split(".")
        if (host.startswith(".") or ".." in host
                or any(not label or not _HOST_LABEL_PATTERN.fullmatch(label)
                       or label.startswith("-") or label.endswith("-")
                       for label in labels)):
            raise ValueError("public_origin 主机非法")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("public_origin 端口非法")
    host = host.lower()
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != 443:
        netloc += f":{port}"
    return f"https://{netloc}"


@dataclass(frozen=True)
class ControlConfig:
    """控制面公共配置；公共 Origin 必须由管理员显式给出。"""

    public_origin: str

    def __post_init__(self) -> None:
        if not isinstance(self.public_origin, str):
            raise ValueError("public_origin 必须显式配置")
        object.__setattr__(
            self, "public_origin", _normalize_public_origin(self.public_origin))


def _source(request: Request) -> str:
    """限速来源只取真实连接地址，不信任任何转发头。"""
    return request.client.host if request.client is not None else "unknown"


def _origin_allowed(request: Request) -> bool:
    expected = getattr(request.app.state, "public_origin", None)
    origin = request.headers.get("origin")
    return bool(expected) and origin is not None and origin == expected


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _effective_method(method: str) -> str:
    """HEAD 按 GET 分类；其余方法统一大写。"""
    normalized = method.upper()
    return "GET" if normalized == "HEAD" else normalized


def _middleware_error(request: Request, code: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"code": code, "detail": code, "request_id": _request_id(request)},
        status_code=status,
        headers={"Cache-Control": "no-store"})


def _implemented_route(method: str, path: str) -> bool:
    if (method, path) in _IMPLEMENTED_EXACT:
        return True
    return any(method == allowed and pattern.fullmatch(path)
               for allowed, pattern in _IMPLEMENTED_PATTERNS)


def _broker_route_registered(method: str, path: str) -> bool:
    """broker 内部路由使用精确 channel + method + 相对 path 白名单。"""
    if not path.startswith(_INTERNAL_BROKER_PREFIX):
        return False
    rest = path[len(_INTERNAL_BROKER_PREFIX):]
    channel, separator, relative_path = rest.partition("/")
    if not separator:
        return False
    normalized = method.upper()
    return any(
        channel == expected_channel and normalized == expected_method
        and relative_path == expected_path
        for expected_channel, expected_method, expected_path, _body
        in BROKER_ROUTE_TABLE)


async def _forward_workspace_json(request: Request, method: str, path: str,
                                  *, query=None, payload=None,
                                  body=None, content_type=None):
    service = _workspace_service(request)
    response = await service.request(
        _identity(request), method, path, query=query, payload=payload,
        body=body, content_type=content_type)
    try:
        data = response.json()
    except ValueError:
        raise DomainError("upstream_invalid", 502) from None
    return _no_store_json(data)


async def _workspace_media_response(request: Request, path: str):
    service = _workspace_service(request)
    actor = _identity(request)
    method = "HEAD" if request.method.upper() == "HEAD" else "GET"
    headers = _media_request_headers(request, actor.workspace_id)
    stream = await service.open_stream(actor, method, f"/api/media/{path}",
                                       headers=headers)
    response_headers = _media_response_headers(
        stream.headers, actor.workspace_id)
    if method == "HEAD" or stream.status_code == 304:
        await stream.aclose()
        if stream.status_code == 304:
            response_headers.pop("content-length", None)
            response_headers.pop("content-range", None)
        return Response(status_code=stream.status_code,
                        headers=response_headers)
    return StreamingResponse(
        stream.chunks(), status_code=stream.status_code,
        headers=response_headers)


def _workspace_service(request: Request):
    service = getattr(request.app.state, "workspace_api", None)
    if service is None:
        raise DomainError("capability_unavailable", 503)
    return service


def _identity(request: Request) -> Identity:
    identity = getattr(request.state, "identity", None)
    if not isinstance(identity, Identity):
        raise DomainError("unauthorized", 401)
    return identity


def _config_service(request: Request) -> ConfigService:
    service = getattr(request.app.state, "config_service", None)
    if service is None:
        raise DomainError("capability_unavailable", 503)
    return service


def _relay(request: Request):
    """未装配签名密钥/受管配置时，任务入口明确 503，不回退本地 handler。"""
    service = getattr(request.app.state, "relay", None)
    if service is None:
        raise DomainError("capability_unavailable", 503)
    return service


_HARNESS_FIELDS = frozenset({
    "runtime", "revision", "model", "models", "sandbox", "timeout",
    "codexAutoApprove"})
_HARNESS_RUNTIMES = ("acp", "opencode", "codex")
_HARNESS_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")


def _normalize_harness_summary(summary: dict | None) -> dict | None:
    """校验 CLI 传入的只读受管底座摘要；拒绝 argv/env/路径等私有字段。"""
    if summary is None:
        return None
    if not isinstance(summary, dict) or set(summary) - _HARNESS_FIELDS:
        raise ValueError("harness summary fields are invalid")
    runtime = summary.get("runtime")
    revision = summary.get("revision")
    if runtime not in _HARNESS_RUNTIMES:
        raise ValueError("harness summary runtime is invalid")
    if type(revision) is not int or revision < 0:
        raise ValueError("harness summary revision is invalid")
    result: dict = {"runtime": runtime, "revision": revision,
                    "model": None, "models": [], "sandbox": None,
                    "timeout": None, "codexAutoApprove": None}
    model = summary.get("model")
    if model is not None:
        if (not isinstance(model, str) or not model or len(model) > 256
                or any(ord(character) < 32 for character in model)):
            raise ValueError("harness summary model is invalid")
        result["model"] = model
    models = summary.get("models")
    if models is not None:
        if (not isinstance(models, list) or len(models) > 64
                or any(not isinstance(item, str) or not item
                       or len(item) > 256
                       or any(ord(character) < 32 for character in item)
                       for item in models)
                or len(set(models)) != len(models)):
            raise ValueError("harness summary models is invalid")
        result["models"] = list(models)
    sandbox = summary.get("sandbox")
    if sandbox is not None:
        if sandbox not in _HARNESS_SANDBOXES:
            raise ValueError("harness summary sandbox is invalid")
        result["sandbox"] = sandbox
    timeout = summary.get("timeout")
    if timeout is not None:
        if type(timeout) is not int or not 1 <= timeout <= 86400:
            raise ValueError("harness summary timeout is invalid")
        result["timeout"] = timeout
    auto_approve = summary.get("codexAutoApprove")
    if auto_approve is not None:
        if type(auto_approve) is not bool:
            raise ValueError("harness summary auto approve is invalid")
        result["codexAutoApprove"] = auto_approve
    return result


def _harness_view(summary: dict | None) -> dict:
    if summary is None:
        return {
            "locked": True, "configured": False, "runtime": None,
            "revision": None, "model": None, "models": None,
            "sandbox": None, "timeout": None, "codexAutoApprove": None,
            "unavailableReason": "managed_harness_not_configured",
        }
    return {"locked": True, "configured": True, **summary,
            "unavailableReason": None}


def _parse_if_match(value: str | None) -> int:
    if value is None:
        raise DomainError("invalid_if_match", 400)
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    if not value.isdigit():
        raise DomainError("invalid_if_match", 400)
    return int(value)


async def _read_json_body(request: Request, limit: int = 65536) -> dict:
    length = request.headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > limit):
        raise DomainError("invalid_body", 400)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise DomainError("invalid_body", 400)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise DomainError("invalid_body", 400) from None
    if not isinstance(payload, dict):
        raise DomainError("invalid_body", 400)
    return payload


class BrokerStreamingResponse(StreamingResponse):
    """流式转发响应；结束、异常或客户端断开都关闭上游并释放额度。"""

    async def stream_response(self, send) -> None:
        try:
            await super().stream_response(send)
        finally:
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                await aclose()


def _identity_view(identity: Identity, csrf_token: str) -> dict:
    capabilities = (_ADMIN_CAPABILITIES if identity.role == "admin"
                    else _MEMBER_CAPABILITIES)
    return {
        "userId": identity.user_id,
        "workspaceId": identity.workspace_id,
        "role": identity.role,
        "capabilities": list(capabilities),
        "csrfToken": csrf_token,
    }


async def _run_db(request: Request, function, *args, **kwargs):
    """在专用数据库线程执行阻塞操作，保持 SQLite 连接线程边界。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        request.app.state.db_executor,
        functools.partial(function, *args, **kwargs))


def _query_int(request: Request, name: str, default: int, *,
               minimum: int, maximum: int) -> int:
    value = request.query_params.get(name)
    if value is None:
        return default
    if not (value.isascii() and value.isdigit()):
        raise DomainError("invalid_argument", 400)
    number = int(value)
    if not minimum <= number <= maximum:
        raise DomainError("invalid_argument", 400)
    return number


def _query_bool(request: Request, name: str, default: bool = False) -> bool:
    value = request.query_params.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in ("1", "true"):
        return True
    if normalized in ("0", "false"):
        return False
    raise DomainError("invalid_argument", 400)


def _session_view(conversation) -> dict:
    return {
        "id": conversation.id,
        "workspaceId": conversation.workspace_id,
        "title": conversation.title,
        "archived": conversation.archived,
    }


def _session_title(value: object) -> str:
    if not isinstance(value, str):
        raise DomainError("invalid_argument", 400)
    title = value.strip()
    if (not title or len(title) > 200
            or any(ord(character) < 32 for character in title)):
        raise DomainError("invalid_argument", 400)
    return title


async def _read_invite_token(request: Request) -> str:
    """从 JSON 请求体读取 token；非法/超限统一按空 token 处理。"""
    length = request.headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > 4096):
        return ""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 4096:
            return ""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    token = payload.get("token")
    return token if isinstance(token, str) else ""


async def _guard_request(request: Request, call_next):
    """先认证、再路由授权；未登记路由与受管写入默认拒绝。

    公开路由仅限 policy 明确登记的精确 method+path；其余（包括未知路径）
    必须先有有效 Cookie 登录。写请求在授权前校验精确 Origin 与 CSRF，并用
    ``inspect``（不更新 last_seen）避免失败请求改变有效会话。
    """
    method = _effective_method(request.method)
    path = request.url.path

    if path.startswith(_INTERNAL_BROKER_PREFIX):
        # broker 使用独立 opaque token 分支，不读取浏览器 Cookie/CSRF；
        # 未登记的 /internal/broker/* 一律 404，不回落到 public /api 边界。
        if _broker_route_registered(method, path):
            return await call_next(request)
        return _middleware_error(request, "not_found", 404)

    if public_route(method, path):
        if not _implemented_route(method, path):
            return _middleware_error(request, "capability_unavailable", 503)
        return await call_next(request)

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return _middleware_error(request, "unauthorized", 401)
    service: AuthService = request.app.state.auth
    try:
        identity = await _run_db(request, service.inspect, token)
    except DomainError:
        return _middleware_error(request, "unauthorized", 401)

    if request.method.upper() not in _SAFE_METHODS:
        if not _origin_allowed(request):
            return _middleware_error(request, "origin_denied", 403)
        try:
            expected = await _run_db(request, service.csrf_token, token)
        except DomainError:
            return _middleware_error(request, "unauthorized", 401)
        provided = request.headers.get("x-csrf-token", "")
        if (not provided or not provided.isascii()
                or not hmac.compare_digest(provided, expected)):
            return _middleware_error(request, "csrf_invalid", 403)

    decision = authorize(identity, method, path)
    if decision.kind in ("unknown", "forbidden"):
        return _middleware_error(request, "forbidden", 403)
    if not _implemented_route(method, path):
        # 已分类但本轮未装配：明确 503，不伪造成功，也不透传 local
        return _middleware_error(request, "capability_unavailable", 503)
    request.state.identity = identity
    return await call_next(request)


def create_app(*, store: Store, auth: AuthService,
               config: ControlConfig | None = None,
               config_service: ConfigService | None = None,
               runtime=None, broker=None, provisioner=None,
               gateway_required: bool = False,
               frontend_dir: str | Path | None = None,
               relay_factory: Callable[..., object] | None = None,
               workspace_api=None,
               workspace_factory: Callable[..., object] | None = None,
               harness_summary: dict | None = None) -> FastAPI:
    """装配最小 control API；未实现的路由保持 404，不回退 local。

    传入的 ``store`` 提供控制库位置；应用在 lifespan 中用专用数据库线程打开
    自己的连接并在退出时关闭，以保持 SQLite 的线程归属。未提供 ``config`` 时
    Origin 校验失败关闭；未提供 ``config_service`` 时配置业务明确 503。

    ``provisioner`` 是受限 provisioner 的客户端；提供时在 lifespan 内用应用
    自己的数据库连接构造 ``RuntimeManager``，避免跨线程复用启动前的连接。

    ``relay_factory`` 接收
    ``(runtime, sessions, auth, store, http_client, broker, db_runner)`` 并以
    位置参数调用；``http_client`` 由 lifespan 创建并关闭，``db_runner`` 保证
    所有 SQLite 调用仍回到专用数据库线程。未提供 factory 时任务入口保持
    503，绝不直通旧本地 handler。

    ``workspace_factory`` 接收
    ``(runtime, store, http_client, db_runner)``；它装配 Task 5A/5B 的
    status/画像/内容/媒体/上传与工作台数据路由。``workspace_api`` 可直接注入测试实现，
    但与 factory 互斥。两者都未提供时这些已分类路由明确 503。
    """
    control_path = Path(store.path).absolute()
    if broker is not None and config_service is None:
        raise ValueError("broker requires config_service")
    if provisioner is not None and runtime is not None:
        raise ValueError("runtime and provisioner are mutually exclusive")
    if workspace_api is not None and workspace_factory is not None:
        raise ValueError("workspace_api and workspace_factory are exclusive")
    frontend_root: Path | None = None
    if frontend_dir is not None:
        frontend_root = Path(frontend_dir).resolve()
        if (not frontend_root.is_dir()
                or not (frontend_root / "index.html").is_file()):
            # 固定构建目录缺失时明确失败，不回退任意服务器路径
            raise ValueError("frontend_build_missing")
    public_origin = config.public_origin if config is not None else None
    if public_origin is not None:
        # 允许调用方传入自定义 config 对象，但仍强制 HTTPS Origin 规则
        public_origin = _normalize_public_origin(public_origin)
    normalized_harness = _normalize_harness_summary(harness_summary)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        executor = ThreadPoolExecutor(max_workers=1,
                                      thread_name_prefix="easel-control-db")
        loop = asyncio.get_running_loop()
        app.state.db_executor = executor
        app.state.public_origin = public_origin
        app.state.harness_summary = normalized_harness
        http_client: httpx.AsyncClient | None = None
        try:
            app.state.control_store = await loop.run_in_executor(
                executor, Store.connect, control_path)

            def control_db_runner(function, *args, **kwargs):
                # SQLite 连接只在 lifespan 的专用线程打开，所有短数据库
                # 步骤都必须回到该线程；网络/容器等待不得占用它。
                return loop.run_in_executor(
                    executor, functools.partial(function, *args, **kwargs))

            if provisioner is not None:
                app.state.runtime = RuntimeManager(
                    app.state.control_store, provisioner,
                    db_runner=control_db_runner,
                    gateway_required=gateway_required)
            else:
                app.state.runtime = runtime
            app.state.auth = AuthService(app.state.control_store, clock=auth.clock)
            app.state.admin_service = AdminService(
                app.state.control_store, app.state.auth)
            app.state.sessions = SessionService(app.state.control_store)
            if config_service is not None:
                # with_store 会读取并校验已有密文，必须在数据库专用线程执行
                app.state.config_service = await loop.run_in_executor(
                    executor, config_service.with_store, app.state.control_store)
            else:
                app.state.config_service = None
            if broker is not None:
                # 阻塞数据库调用仍走专用线程；流式释放也要走同一线程
                app.state.broker = broker.with_store(
                    app.state.control_store, app.state.config_service,
                    db_runner=control_db_runner)
            else:
                app.state.broker = None
            if relay_factory is not None or workspace_factory is not None:
                # 客户端归 lifespan 所有：连接池随应用退出关闭，不在每次
                # 请求内新建，也不修改共享默认凭证。内部签名包与服务凭证
                # 只走直连，绝不读取宿主环境/注册表代理（否则内网请求可能
                # 被外部代理截走或失败）。
                http_client = httpx.AsyncClient(trust_env=False)
            if relay_factory is not None:
                app.state.relay = relay_factory(
                    app.state.runtime, app.state.sessions,
                    app.state.auth, app.state.control_store,
                    http_client, app.state.broker, control_db_runner)
            else:
                app.state.relay = None
            if workspace_api is not None:
                app.state.workspace_api = workspace_api
            elif workspace_factory is not None and http_client is not None:
                app.state.workspace_api = workspace_factory(
                    app.state.runtime, app.state.control_store,
                    http_client, control_db_runner)
            else:
                app.state.workspace_api = None
            from .workspace_tasks import WorkspaceTasks
            app.state.workspace_tasks = WorkspaceTasks(
                app.state.control_store, app.state.sessions,
                app.state.relay, app.state.workspace_api,
                control_db_runner)
            # 后台对账与 SSE/登录解耦：应用启动即恢复扫描，退出时先停止
            # 循环再关闭它使用的 HTTP 连接池。
            app.state.reconciler = None
            if (app.state.relay is not None
                    and callable(getattr(
                        app.state.relay, "reconcile_once", None))):
                from .reconcile import RunReconciler
                app.state.reconciler = RunReconciler(app.state.relay)
                await app.state.reconciler.start()
            yield
        finally:
            try:
                reconciler = getattr(app.state, "reconciler", None)
                if reconciler is not None:
                    await reconciler.stop()
                if http_client is not None:
                    await http_client.aclose()
                current_store = getattr(app.state, "control_store", None)
                if current_store is not None:
                    await loop.run_in_executor(executor, current_store.close)
            finally:
                executor.shutdown(wait=True)

    app = FastAPI(title="Easel Control", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def control_guard(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        response = await _guard_request(request, call_next)
        response.headers.setdefault("X-Request-ID", request.state.request_id)
        return response

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, error: DomainError):
        # 只暴露错误码与请求 id；不回显令牌、SQL 或内部对象
        headers = {"Cache-Control": "no-store"}
        if error.retry_after is not None:
            headers["Retry-After"] = str(int(error.retry_after))
        return JSONResponse(
            {"code": error.code, "detail": error.code,
             "request_id": _request_id(request)},
            status_code=error.status, headers=headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, error: StarletteHTTPException):
        """未知路由/方法统一显式 404/405，不回落任何原有 Web 应用。"""
        code = {404: "not_found", 405: "method_not_allowed"}.get(
            error.status_code, "http_error")
        headers = {"Cache-Control": "no-store"}
        if error.headers:
            headers.update(error.headers)
        return JSONResponse(
            {"code": code, "detail": code, "request_id": _request_id(request)},
            status_code=error.status_code, headers=headers)

    @app.get("/api/health")
    async def health():
        return JSONResponse({"status": "ok"},
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/runtime")
    async def runtime_mode():
        """公开可信启动信息；未知/缺失时前端失败关闭，不猜测 local。"""
        return JSONResponse({"mode": "control"},
                            headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------
    # Task 5A：基础工作区（画像/内容/媒体/附件）
    # ------------------------------------------------------------------

    @app.get("/api/status")
    async def workspace_status(request: Request):
        service = _workspace_service(request)
        return _no_store_json(
            await service.status(_identity(request)))

    @app.get("/api/personas")
    async def workspace_personas(request: Request):
        return await _forward_workspace_json(
            request, "GET", "/api/personas")

    @app.get("/api/persona/{name}")
    async def workspace_persona(request: Request, name: str):
        from .workspace_api import canonical_persona_name

        encoded = canonical_persona_name(name)
        return await _forward_workspace_json(
            request, "GET", f"/api/persona/{encoded}")

    @app.get("/api/persona/{name}/files")
    async def workspace_persona_files(request: Request, name: str):
        from .workspace_api import canonical_persona_name

        encoded = canonical_persona_name(name)
        return await _forward_workspace_json(
            request, "GET", f"/api/persona/{encoded}/files")

    @app.put("/api/persona/{name}/file")
    async def workspace_persona_file_save(request: Request, name: str):
        from .workspace_api import canonical_persona_name

        encoded = canonical_persona_name(name)
        identity = _identity(request)
        tasks = request.app.state.workspace_tasks
        # 声明与检查在同一事件循环步骤内完成；基线准备期也覆盖。
        if not tasks.claim_profile(identity, name):
            raise DomainError("profile_busy", 409)
        try:
            if await tasks.profile_active(identity, name):
                raise DomainError("profile_busy", 409)
            raw = await _workspace_body(request, maximum=2 * 1024 * 1024)
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise DomainError("invalid_request", 400) from None
            if not isinstance(payload, dict):
                raise DomainError("invalid_request", 400)
            return await _forward_workspace_json(
                request, "PUT", f"/api/persona/{encoded}/file",
                payload=payload)
        finally:
            tasks.release_profile(identity, name)

    @app.delete("/api/persona/{name}")
    async def workspace_persona_delete(request: Request, name: str):
        from .workspace_api import canonical_persona_name

        encoded = canonical_persona_name(name)
        identity = _identity(request)
        tasks = request.app.state.workspace_tasks
        if not tasks.claim_profile(identity, name):
            raise DomainError("profile_busy", 409)
        try:
            if await tasks.profile_active(identity, name):
                raise DomainError("profile_busy", 409)
            return await _forward_workspace_json(
                request, "DELETE", f"/api/persona/{encoded}")
        finally:
            tasks.release_profile(identity, name)

    @app.get("/api/outputs")
    async def workspace_outputs(request: Request):
        return await _forward_workspace_json(
            request, "GET", "/api/outputs")

    @app.get("/api/artifacts/archive")
    async def workspace_archive(request: Request):
        from .workspace_api import canonical_output_path

        path = request.query_params.get("path")
        if path is None:
            raise DomainError("invalid_request", 400)
        canonical_output_path(path)
        return await _forward_workspace_json(
            request, "GET", "/api/artifacts/archive",
            query={"path": path})

    @app.get("/api/output/{path:path}")
    async def workspace_output(request: Request, path: str):
        from .workspace_api import canonical_output_path

        encoded = canonical_output_path(path)
        return await _forward_workspace_json(
            request, "GET", f"/api/output/{encoded}")

    @app.api_route("/api/media/{path:path}", methods=["GET", "HEAD"])
    async def workspace_media(request: Request, path: str):
        from .workspace_api import canonical_output_path

        encoded = canonical_output_path(path)
        return await _workspace_media_response(request, encoded)

    @app.delete("/api/output/{path:path}")
    async def workspace_output_delete(request: Request, path: str):
        from .workspace_api import canonical_output_path

        encoded = canonical_output_path(path)
        return await _forward_workspace_json(
            request, "DELETE", f"/api/output/{encoded}")

    @app.get("/api/upload/limits")
    async def workspace_upload_limits(request: Request):
        return await _forward_workspace_json(
            request, "GET", "/api/upload/limits")

    @app.post("/api/upload")
    async def workspace_upload(request: Request):
        content_type = request.headers.get("content-type", "")
        if (not content_type.lower().startswith("multipart/form-data;")
                or "boundary=" not in content_type.lower()
                or len(content_type) > 256):
            raise DomainError("invalid_request", 400)
        body = await _workspace_body(request)
        service = _workspace_service(request)
        response = await service.upload(
            _identity(request), body, content_type)
        try:
            data = response.json()
        except ValueError:
            raise DomainError("upstream_invalid", 502) from None
        return _no_store_json(data)

    # ------------------------------------------------------------------
    # Task 5B：工作台数据（热点/选题/日历/概览）
    # ------------------------------------------------------------------

    @app.get("/api/trends")
    async def workspace_trends(request: Request):
        from .workspace_api import canonical_trends_query

        platforms = _workbench_query_value(request, "platforms")
        limit = _workbench_int(request, "limit")
        query = canonical_trends_query(platforms, limit)
        return await _forward_workspace_json(
            request, "GET", "/api/trends", query=query)

    @app.get("/api/schedule")
    async def workspace_schedule(request: Request):
        return await _forward_workspace_json(
            request, "GET", "/api/schedule")

    @app.post("/api/schedule")
    async def workspace_schedule_create(request: Request):
        from .workspace_api import canonical_schedule_payload

        payload = _workbench_json_payload(
            await _workspace_body(request, maximum=64 * 1024))
        clean = canonical_schedule_payload(payload)
        return await _forward_workspace_json(
            request, "POST", "/api/schedule", payload=clean)

    @app.get("/api/schedule/context")
    async def workspace_schedule_context(request: Request):
        from .workspace_api import canonical_schedule_days

        days = canonical_schedule_days(_workbench_int(request, "days"))
        return await _forward_workspace_json(
            request, "GET", "/api/schedule/context",
            query={"days": str(days)})

    @app.put("/api/schedule/{sid}")
    async def workspace_schedule_update(request: Request, sid: str):
        from .workspace_api import (
            canonical_schedule_payload, canonical_workbench_id,
        )

        record_id = canonical_workbench_id(sid)
        payload = _workbench_json_payload(
            await _workspace_body(request, maximum=64 * 1024))
        clean = canonical_schedule_payload(payload)
        return await _forward_workspace_json(
            request, "PUT", f"/api/schedule/{record_id}", payload=clean)

    @app.delete("/api/schedule/{sid}")
    async def workspace_schedule_delete(request: Request, sid: str):
        from .workspace_api import canonical_workbench_id

        record_id = canonical_workbench_id(sid)
        return await _forward_workspace_json(
            request, "DELETE", f"/api/schedule/{record_id}")

    @app.get("/api/ideas")
    async def workspace_ideas(request: Request):
        return await _forward_workspace_json(
            request, "GET", "/api/ideas")

    @app.post("/api/ideas")
    async def workspace_idea_create(request: Request):
        from .workspace_api import canonical_idea_payload

        payload = _workbench_json_payload(
            await _workspace_body(request, maximum=64 * 1024))
        clean = canonical_idea_payload(payload)
        return await _forward_workspace_json(
            request, "POST", "/api/ideas", payload=clean)

    @app.put("/api/ideas/{iid}")
    async def workspace_idea_update(request: Request, iid: str):
        from .workspace_api import (
            canonical_idea_payload, canonical_workbench_id,
        )

        record_id = canonical_workbench_id(iid)
        payload = _workbench_json_payload(
            await _workspace_body(request, maximum=64 * 1024))
        clean = canonical_idea_payload(payload)
        return await _forward_workspace_json(
            request, "PUT", f"/api/ideas/{record_id}", payload=clean)

    @app.delete("/api/ideas/{iid}")
    async def workspace_idea_delete(request: Request, iid: str):
        from .workspace_api import canonical_workbench_id

        record_id = canonical_workbench_id(iid)
        return await _forward_workspace_json(
            request, "DELETE", f"/api/ideas/{record_id}")

    # ------------------------------------------------------------------
    # Task 5C1：技能目录/执行与画像基线/增强
    # ------------------------------------------------------------------

    @app.get("/api/skills")
    async def workspace_skills(request: Request):
        return await _forward_workspace_json(
            request, "GET", "/api/skills")

    @app.get("/api/skill/status/{name}")
    async def workspace_skill_status(request: Request, name: str):
        service = request.app.state.workspace_tasks
        return _no_store_json(
            await service.skill_status(_identity(request), name))

    @app.get("/api/skill/{name}")
    async def workspace_skill_detail(request: Request, name: str):
        from .workspace_tasks import canonical_skill_name

        encoded = quote(canonical_skill_name(name), safe="-._~")
        return await _forward_workspace_json(
            request, "GET", f"/api/skill/{encoded}")

    @app.post("/api/skill")
    async def workspace_skill_run(request: Request):
        service = request.app.state.workspace_tasks
        payload = await _read_json_body(
            request, limit=64 * 1024 + 65536)
        result = await service.submit_skill(
            _identity(request), payload)
        return JSONResponse(result, status_code=202,
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/profile/build")
    async def workspace_profile_build(request: Request):
        service = request.app.state.workspace_tasks
        payload = await _read_json_body(
            request, limit=256 * 1024 + 65536)
        result = await service.build_profile(
            _identity(request), payload)
        return JSONResponse(result, status_code=200,
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/profile/build/status/{name}")
    async def workspace_profile_status(request: Request, name: str):
        service = request.app.state.workspace_tasks
        return _no_store_json(
            await service.profile_status(_identity(request), name))

    @app.post("/api/auth/exchange")
    async def exchange(request: Request):
        if not _origin_allowed(request):
            raise DomainError("origin_denied", 403)
        invite = await _read_invite_token(request)
        source = _source(request)
        service: AuthService = request.app.state.auth

        # 限速准入、兑换与失败记账在同一数据库事务内完成
        outcome = await _run_db(
            request, service.exchange_with_rate_limit, source, invite)
        if outcome.status == "rate_limited":
            return JSONResponse(
                {"code": "rate_limited", "detail": "rate_limited",
                 "request_id": _request_id(request)},
                status_code=429,
                headers={"Retry-After": str(outcome.retry_after),
                         "Cache-Control": "no-store"})
        if outcome.status == "denied":
            raise DomainError("unauthorized", 401)

        session = outcome.session
        identity = outcome.identity
        if session is None or identity is None:
            raise DomainError("unauthorized", 401)
        response = JSONResponse(
            _identity_view(identity, session.csrf_token),
            headers={"Cache-Control": "no-store"})
        response.set_cookie(
            SESSION_COOKIE, session.token,
            max_age=AuthService.SESSION_ABSOLUTE_SECONDS,
            path="/", secure=True, httponly=True, samesite="lax")
        return response

    @app.get("/api/auth/me")
    async def me(request: Request):
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise DomainError("unauthorized", 401)
        service: AuthService = request.app.state.auth
        identity = await _run_db(request, service.authenticate, token)
        csrf = await _run_db(request, service.csrf_token, token)
        return JSONResponse(_identity_view(identity, csrf),
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise DomainError("unauthorized", 401)
        if not _origin_allowed(request):
            raise DomainError("origin_denied", 403)

        service: AuthService = request.app.state.auth
        # csrf_token 只校验会话、不刷新活动时间；错误 CSRF 不能改变有效登录
        expected = await _run_db(request, service.csrf_token, token)
        provided = request.headers.get("x-csrf-token", "")
        # compare_digest 不接受非 ASCII 字符串；原始字节头也可能解码出非 ASCII
        if (not provided or not provided.isascii()
                or not hmac.compare_digest(provided, expected)):
            raise DomainError("csrf_invalid", 403)

        await _run_db(request, service.revoke_current, token)
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/",
                               secure=True, httponly=True, samesite="lax")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/sessions")
    async def list_sessions(request: Request):
        service: SessionService = request.app.state.sessions
        limit = _query_int(request, "limit", 50, minimum=1, maximum=200)
        offset = _query_int(request, "offset", 0,
                            minimum=0, maximum=1_000_000)
        include_archived = _query_bool(request, "include_archived")
        sessions = await _run_db(
            request, service.list_conversations, request.state.identity,
            limit=limit, offset=offset,
            include_archived=include_archived)
        next_offset = offset + len(sessions) if len(sessions) == limit else None
        return JSONResponse(
            {"sessions": [_session_view(item) for item in sessions],
             "nextOffset": next_offset},
            headers={"Cache-Control": "no-store"})

    @app.post("/api/sessions")
    async def create_session(request: Request):
        service: SessionService = request.app.state.sessions
        payload = await _read_json_body(request)
        title = _session_title(payload.get("title", "新对话"))
        session = await _run_db(
            request, service.create_conversation,
            request.state.identity, title)
        return JSONResponse(
            _session_view(session), status_code=201,
            headers={"Cache-Control": "no-store"})

    @app.patch("/api/sessions/{session_id}")
    async def update_session(request: Request, session_id: str):
        service: SessionService = request.app.state.sessions
        payload = await _read_json_body(request)
        title = None
        archived = None
        if "title" in payload:
            title = _session_title(payload["title"])
        if "archived" in payload:
            if not isinstance(payload["archived"], bool):
                raise DomainError("invalid_argument", 400)
            archived = payload["archived"]
        if title is None and archived is None:
            raise DomainError("invalid_argument", 400)
        session = await _run_db(
            request, service.update_conversation,
            request.state.identity, session_id,
            title=title, archived=archived)
        return JSONResponse(
            _session_view(session),
            headers={"Cache-Control": "no-store"})

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(request: Request, session_id: str):
        service: SessionService = request.app.state.sessions
        await _run_db(
            request, service.delete_conversation,
            request.state.identity, session_id)
        return Response(status_code=204,
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/sessions/{session_id}/runs")
    async def session_runs(request: Request, session_id: str):
        """本人会话的 Run 元数据窄路由；稳定分页、不含任何凭证。"""
        service: SessionService = request.app.state.sessions
        limit = _query_int(request, "limit", 50, minimum=1, maximum=200)
        offset = _query_int(request, "offset", 0,
                            minimum=0, maximum=1_000_000)
        active_only = _query_bool(request, "active")
        runs = await _run_db(
            request, service.list_runs, request.state.identity, session_id,
            limit=limit, offset=offset, active_only=active_only)
        next_offset = offset + len(runs) if len(runs) == limit else None
        return JSONResponse(
            {"runs": [{"runId": run.id,
                       "conversationId": run.conversation_id,
                       "state": run.status} for run in runs],
             "nextOffset": next_offset},
            headers={"Cache-Control": "no-store"})

    @app.get("/api/sessions/{session_id}/messages")
    async def session_messages(request: Request, session_id: str):
        """读取本人 worker 的持久消息快照；读取不会启动新 prompt。

        归属检查先于 relay 能力检查：外空间/未知会话始终 404，且不触发任何
        worker 调用。真正的消息内容只来自本人内部路由，control 不读取由
        客户端指定的服务器文件路径。
        """
        service: SessionService = request.app.state.sessions
        limit = _query_int(request, "limit", 100, minimum=1, maximum=500)
        offset = _query_int(request, "offset", 0,
                            minimum=0, maximum=1_000_000)
        await _run_db(
            request, service.get_conversation,
            request.state.identity, session_id)
        relay = _relay(request)
        result = await relay.messages(
            request.state.identity, session_id, limit=limit, offset=offset)
        messages = result.get("messages") if isinstance(result, dict) else None
        if not isinstance(messages, list):
            raise DomainError("upstream_invalid", 502)
        total = result.get("total")
        if type(total) is not int or total < len(messages):
            total = len(messages)
        return JSONResponse(
            {"messages": messages, "total": total},
            headers={"Cache-Control": "no-store"})

    @app.post("/api/chat")
    async def submit_chat(request: Request):
        """对话任务唯一公开入口：控制面接纳后由 relay 派发给本人 worker。

        传输上限在消息上限之外预留 JSON 包装余量；真正的输入校验与摘要
        仍在 relay 内完成，避免浏览器绕过 Run 账本或注入内部字段。
        """
        service = _relay(request)
        payload = await _read_json_body(
            request, limit=MAX_MESSAGE_BYTES + 65536)
        result = await service.submit(request.state.identity, payload)
        return JSONResponse(result, status_code=202,
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/chat/stream")
    async def submit_chat_stream(request: Request):
        """流式入口同样先经接纳账本；客户端随后用 jobs/{runId}/stream 回放。"""
        service = _relay(request)
        payload = await _read_json_body(
            request, limit=MAX_MESSAGE_BYTES + 65536)
        result = await service.submit(request.state.identity, payload)
        return JSONResponse(result, status_code=202,
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/chat/stop")
    async def stop_chat(request: Request):
        service = _relay(request)
        payload = await _read_json_body(request)
        result = await service.stop(request.state.identity, payload)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.post("/api/chat/question/answer")
    async def answer_chat_question(request: Request):
        service = _relay(request)
        payload = await _read_json_body(request)
        result = await service.answer(request.state.identity, payload)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.post("/api/chat/question/status")
    async def chat_question_status(request: Request):
        service = _relay(request)
        payload = await _read_json_body(request)
        result = await service.question_status(
            request.state.identity, payload)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get("/api/chat/jobs/{run_id}/stream")
    async def stream_chat_job(request: Request, run_id: str):
        """可恢复 SSE：首帧前校验游标与归属，每 4 秒复查登录撤销。"""
        service = _relay(request)
        after = _query_int(request, "after", 0, minimum=0,
                           maximum=1_000_000_000)
        header = request.headers.get("last-event-id")
        if header is not None:
            if not (header.isascii() and header.isdigit()):
                raise DomainError("invalid_argument", 400)
            after = int(header)
        identity = request.state.identity
        # 首次读取在开始流式响应前完成：无效游标/外空间 Run 返回明确的 JSON 错误
        first = await service.events(identity, run_id, after=after)
        token = request.cookies.get(SESSION_COOKIE) or ""
        auth_service: AuthService = request.app.state.auth
        sessions: SessionService = request.app.state.sessions

        async def recheck() -> None:
            current = await _run_db(request, auth_service.inspect, token)
            await _run_db(request, sessions.assert_run, current, run_id)

        return AuthGuardedStreamingResponse(
            run_event_stream(relay=service, actor=identity, run_id=run_id,
                             page=first, recheck=recheck),
            recheck=recheck,
            auth_check_seconds=DEFAULT_AUTH_CHECK_SECONDS,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/settings/models")
    async def get_model_settings(request: Request):
        service = _config_service(request)
        settings = await _run_db(
            request, service.public_settings, request.state.identity)
        return JSONResponse(settings,
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/settings/harness")
    async def get_harness_settings(request: Request):
        """本人可读的受管底座脱敏摘要；不返回 argv/env/内部地址。"""
        return JSONResponse(
            _harness_view(request.app.state.harness_summary),
            headers={"Cache-Control": "no-store"})

    @app.post("/api/settings/models/save")
    async def save_model_settings(request: Request):
        service = _config_service(request)
        payload = await _read_json_body(request)
        channel = payload.get("channel")
        route = payload.get("route")
        if not isinstance(channel, str) or not isinstance(route, dict):
            raise DomainError("invalid_route", 400)
        await _run_db(request, service.save_override,
                      request.state.identity, channel, route)
        return JSONResponse({"ok": True},
                            headers={"Cache-Control": "no-store"})

    @app.delete("/api/settings/models/overrides/{channel}")
    async def clear_model_override(request: Request, channel: str):
        service = _config_service(request)
        await _run_db(request, service.clear_override,
                      request.state.identity, channel)
        return Response(status_code=204,
                        headers={"Cache-Control": "no-store"})

    @app.post("/api/admin/managed-config")
    async def publish_managed_config(request: Request):
        service = _config_service(request)
        expected_version = _parse_if_match(request.headers.get("if-match"))
        settings = await _read_json_body(request)
        version = await _run_db(
            request, service.publish, request.state.identity,
            settings, expected_version)
        return JSONResponse({"version": version},
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/admin/managed-config")
    async def get_managed_config(request: Request):
        service = _config_service(request)
        settings = await _run_db(
            request, service.public_managed_settings,
            request.state.identity)
        return JSONResponse(settings,
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/admin/users")
    async def list_admin_users(request: Request):
        service: AdminService = request.app.state.admin_service
        limit = _query_int(request, "limit", 50, minimum=1, maximum=200)
        offset = _query_int(request, "offset", 0,
                            minimum=0, maximum=1_000_000)
        result = await _run_db(
            request, service.list_users, request.state.identity,
            limit=limit, offset=offset)
        return JSONResponse(result,
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/admin/users")
    async def create_admin_user(request: Request):
        service: AdminService = request.app.state.admin_service
        payload = await _read_json_body(request)
        if set(payload) != {"displayName"}:
            raise DomainError("invalid_argument", 400)
        user = await _run_db(
            request, service.create_member, request.state.identity,
            payload.get("displayName"))
        return JSONResponse(user, status_code=201,
                            headers={"Cache-Control": "no-store"})

    @app.patch("/api/admin/users/{user_id}")
    async def update_admin_user(request: Request, user_id: str):
        service: AdminService = request.app.state.admin_service
        payload = await _read_json_body(request)
        if set(payload) != {"disabled"}:
            raise DomainError("invalid_argument", 400)
        user = await _run_db(
            request, service.set_member_disabled, request.state.identity,
            user_id, payload["disabled"])
        return JSONResponse(user,
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/admin/users/{user_id}/revoke-sessions")
    async def revoke_admin_user_sessions(request: Request, user_id: str):
        service: AdminService = request.app.state.admin_service
        result = await _run_db(
            request, service.revoke_member_sessions,
            request.state.identity, user_id)
        return JSONResponse(result,
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/admin/invites")
    async def list_admin_invites(request: Request):
        service: AdminService = request.app.state.admin_service
        limit = _query_int(request, "limit", 50, minimum=1, maximum=200)
        offset = _query_int(request, "offset", 0,
                            minimum=0, maximum=1_000_000)
        result = await _run_db(
            request, service.list_invites, request.state.identity,
            limit=limit, offset=offset)
        return JSONResponse(result,
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/admin/invites")
    async def create_admin_invite(request: Request):
        service: AdminService = request.app.state.admin_service
        payload = await _read_json_body(request)
        if set(payload) != {"userId"}:
            raise DomainError("invalid_argument", 400)
        invite = await _run_db(
            request, service.create_invite, request.state.identity,
            payload.get("userId"))
        return JSONResponse(invite, status_code=201,
                            headers={"Cache-Control": "no-store"})

    @app.delete("/api/admin/invites/{invite_id}")
    async def revoke_admin_invite(request: Request, invite_id: str):
        service: AdminService = request.app.state.admin_service
        await _run_db(
            request, service.revoke_invite, request.state.identity,
            invite_id)
        return Response(status_code=204,
                        headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------
    # 固定前端构建目录：只公开索引、assets/static 与两个根文件
    # ------------------------------------------------------------------

    if frontend_root is not None:
        def frontend_file(base: Path, relative: str) -> Path:
            try:
                candidate = (base / relative).resolve()
            except OSError:
                raise DomainError("not_found", 404) from None
            if candidate != base and base not in candidate.parents:
                raise DomainError("not_found", 404)
            if not candidate.is_file():
                raise DomainError("not_found", 404)
            return candidate

        @app.get("/")
        @app.get("/onepage")
        async def frontend_index():
            return FileResponse(
                frontend_root / "index.html", media_type="text/html",
                headers={"Cache-Control": "no-store"})

        @app.get("/assets/{asset_path:path}")
        async def frontend_asset(asset_path: str):
            return FileResponse(
                frontend_file(frontend_root / "assets", asset_path),
                headers={"Cache-Control":
                         "public, max-age=31536000, immutable"})

        @app.get("/static/{asset_path:path}")
        async def frontend_static(asset_path: str):
            return FileResponse(
                frontend_file(frontend_root / "static", asset_path))

        @app.get("/favicon.svg")
        async def frontend_favicon():
            return FileResponse(
                frontend_file(frontend_root, "favicon.svg"),
                media_type="image/svg+xml")

        @app.get("/icons.svg")
        async def frontend_icons():
            return FileResponse(
                frontend_file(frontend_root, "icons.svg"),
                media_type="image/svg+xml")

    # ------------------------------------------------------------------
    # provider broker：独立 opaque token 分支，精确注册首批 adapter 路由
    # ------------------------------------------------------------------

    def _make_broker_handler(channel: str, route_method: str,
                             relative_path: str):
        async def handler(request: Request):
            service = getattr(request.app.state, "broker", None)
            if service is None:
                raise DomainError("capability_unavailable", 503)
            token = request.headers.get("x-easel-broker-token", "")
            workspace_id = request.headers.get("x-easel-workspace-id", "")
            run_id = request.headers.get("x-easel-run-id", "")
            if not token or not workspace_id or not run_id:
                raise DomainError("invalid_token", 401)

            body = None
            if route_method in ("POST", "PUT", "PATCH"):
                async def body_stream():
                    async for chunk in request.stream():
                        yield chunk
                body = body_stream()

            result = await service.forward(
                token=token, workspace_id=workspace_id, run_id=run_id,
                channel=channel, method=route_method,
                relative_path=relative_path, headers=request.headers,
                body=body)
            return BrokerStreamingResponse(
                result.chunks(), status_code=result.status_code,
                headers=result.headers)

        return handler

    for _channel, _method, _relative_path, _body_kind in BROKER_ROUTE_TABLE:
        app.add_api_route(
            f"/internal/broker/{_channel}/{_relative_path}",
            _make_broker_handler(_channel, _method, _relative_path),
            methods=[_method], include_in_schema=False)

    return app
