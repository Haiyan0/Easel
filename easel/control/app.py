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
import hmac
import ipaddress
import json
import re
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..providers.openai import BROKER_ROUTE_TABLE
from ..worker.sessions import MAX_MESSAGE_BYTES
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
    ("POST", "/api/auth/exchange"),
    ("GET", "/api/auth/me"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/admin/managed-config"),
    ("GET", "/api/settings/models"),
    ("POST", "/api/settings/models/save"),
    ("GET", "/api/sessions"),
    ("POST", "/api/sessions"),
    ("POST", "/api/chat"),
    ("POST", "/api/chat/stream"),
    ("POST", "/api/chat/stop"),
    ("POST", "/api/chat/question/answer"),
    ("POST", "/api/chat/question/status"),
})
_IMPLEMENTED_PATTERNS = (
    ("DELETE", re.compile(r"^/api/settings/models/overrides/[^/]+$")),
    ("PATCH", re.compile(r"^/api/sessions/[^/]+$")),
    ("DELETE", re.compile(r"^/api/sessions/[^/]+$")),
    ("GET", re.compile(r"^/api/sessions/[^/]+/messages$")),
    ("GET", re.compile(r"^/api/chat/jobs/[^/]+/stream$")),
)
"""control 本轮真正装配的路由；其余已分类业务显式 503。"""

_MEMBER_CAPABILITIES = ("workspace",)
_ADMIN_CAPABILITIES = ("workspace", "admin")

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
               relay_factory: Callable[..., object] | None = None) -> FastAPI:
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
    """
    control_path = Path(store.path).absolute()
    if broker is not None and config_service is None:
        raise ValueError("broker requires config_service")
    if provisioner is not None and runtime is not None:
        raise ValueError("runtime and provisioner are mutually exclusive")
    public_origin = config.public_origin if config is not None else None
    if public_origin is not None:
        # 允许调用方传入自定义 config 对象，但仍强制 HTTPS Origin 规则
        public_origin = _normalize_public_origin(public_origin)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        executor = ThreadPoolExecutor(max_workers=1,
                                      thread_name_prefix="easel-control-db")
        loop = asyncio.get_running_loop()
        app.state.db_executor = executor
        app.state.public_origin = public_origin
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
                    db_runner=control_db_runner)
            else:
                app.state.runtime = runtime
            app.state.auth = AuthService(app.state.control_store, clock=auth.clock)
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
            if relay_factory is not None:
                # 客户端归 lifespan 所有：连接池随应用退出关闭，不在每次
                # 请求内新建，也不修改共享默认凭证。内部签名包与服务凭证
                # 只走直连，绝不读取宿主环境/注册表代理（否则内网请求可能
                # 被外部代理截走或失败）。
                http_client = httpx.AsyncClient(trust_env=False)
                app.state.relay = relay_factory(
                    app.state.runtime, app.state.sessions,
                    app.state.auth, app.state.control_store,
                    http_client, app.state.broker, control_db_runner)
            else:
                app.state.relay = None
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
        return JSONResponse(
            {"code": error.code, "detail": error.code,
             "request_id": _request_id(request)},
            status_code=error.status,
            headers={"Cache-Control": "no-store"})

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
        return JSONResponse(
            {"messages": messages},
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
