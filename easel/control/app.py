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
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import AuthService
from .store import Store
from .types import DomainError, Identity

SESSION_COOKIE = "__Host-easel_session"
"""__Host- 前缀要求 Secure、Path=/ 且无 Domain。"""

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


async def _run_db(request: Request, function, *args):
    """在专用数据库线程执行阻塞操作，保持 SQLite 连接线程边界。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        request.app.state.db_executor, functools.partial(function, *args))


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


def create_app(*, store: Store, auth: AuthService,
               config: ControlConfig | None = None,
               runtime=None, broker=None) -> FastAPI:
    """装配最小 control API；未实现的路由保持 404，不回退 local。

    传入的 ``store`` 提供控制库位置；应用在 lifespan 中用专用数据库线程打开
    自己的连接并在退出时关闭，以保持 SQLite 的线程归属。未提供 ``config`` 时
    Origin 校验失败关闭。
    """
    control_path = Path(store.path).absolute()
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
        app.state.runtime = runtime
        app.state.broker = broker
        try:
            app.state.control_store = await loop.run_in_executor(
                executor, Store.connect, control_path)
            app.state.auth = AuthService(app.state.control_store, clock=auth.clock)
            yield
        finally:
            try:
                current_store = getattr(app.state, "control_store", None)
                if current_store is not None:
                    await loop.run_in_executor(executor, current_store.close)
            finally:
                executor.shutdown(wait=True)

    app = FastAPI(title="Easel Control", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def request_identity(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
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

    return app
