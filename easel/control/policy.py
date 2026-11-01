"""control 路由权限分类：显式 method+path 注册表，默认拒绝。

``authorize`` 只回答“这个身份能否尝试这类路由”，不证明资源归属。真正的
资源访问必须由 Store 按当前身份重查；``RouteDecision.resource_id`` 只是从
路径中提取的候选标识，不能当作已授权对象。

分类规则全部锚定完整路径并逐方法登记，禁止用 ``/api`` 前缀或 ``startswith``
放行。未登记的方法、尾斜杠变体、相似前缀一律返回 ``unknown``。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .types import Identity

# 路由类别
PUBLIC = "public"
CONTROL_READ = "control_read"
CONTROL_WRITE = "control_write"
ADMIN_READ = "admin_read"
ADMIN_WRITE = "admin_write"
MANAGED_READ = "managed_read"
MANAGED_WRITE = "managed_write"
WORKSPACE_READ = "workspace_read"
WORKSPACE_WRITE = "workspace_write"
TASK = "task"
INTERNAL = "internal"
FORBIDDEN = "forbidden"
UNKNOWN = "unknown"

_ADMIN_ONLY = frozenset({ADMIN_READ, ADMIN_WRITE, MANAGED_WRITE})
_ALWAYS_DENIED = frozenset({FORBIDDEN, INTERNAL})
_WORKER_ALLOWED = frozenset({
    PUBLIC, WORKSPACE_READ, WORKSPACE_WRITE, TASK, INTERNAL})


@dataclass(frozen=True)
class RouteDecision:
    """候选路由分类结果；不包含授权结论之外的资源证明。"""

    kind: str
    resource_type: str | None = None
    resource_id: str | None = None


@dataclass(frozen=True)
class _Route:
    methods: frozenset[str]
    pattern: re.Pattern
    kind: str
    resource_type: str | None = None
    resource_id_group: int | None = None


def _route(methods: str | Iterable[str], pattern: str, kind: str,
           resource_type: str | None = None,
           resource_id_group: int | None = None) -> _Route:
    if isinstance(methods, str):
        methods = (methods,)
    return _Route(frozenset(method.upper() for method in methods),
                  re.compile(pattern), kind, resource_type, resource_id_group)


# 顺序不影响正确性（模式均锚定完整路径），但公开/精确路由放在前面便于阅读。
_RULES: tuple[_Route, ...] = (
    # 公开：仅这些明确路由无需登录
    _route("GET", r"^/api/health$", PUBLIC),
    _route("GET", r"^/api/runtime$", PUBLIC),
    _route("POST", r"^/api/auth/exchange$", PUBLIC),
    _route("GET", r"^/$", PUBLIC),
    _route("GET", r"^/onepage$", PUBLIC),
    _route("GET", r"^/assets/(.+)$", PUBLIC),
    _route("GET", r"^/static/(.+)$", PUBLIC),
    _route("GET", r"^/favicon\.svg$", PUBLIC),
    _route("GET", r"^/icons\.svg$", PUBLIC),

    # 身份
    _route("GET", r"^/api/auth/me$", CONTROL_READ),
    _route("POST", r"^/api/auth/logout$", CONTROL_WRITE),

    # 管理员用户/邀请/受管配置/worker
    _route("GET", r"^/api/admin/users$", ADMIN_READ),
    _route("POST", r"^/api/admin/users$", ADMIN_WRITE),
    _route("GET", r"^/api/admin/users/([^/]+)$", ADMIN_READ, "user", 1),
    _route(("PATCH", "DELETE"), r"^/api/admin/users/([^/]+)$",
           ADMIN_WRITE, "user", 1),
    _route("POST", r"^/api/admin/users/([^/]+)/revoke-sessions$",
           ADMIN_WRITE, "user", 1),
    _route("GET", r"^/api/admin/invites$", ADMIN_READ),
    _route("POST", r"^/api/admin/invites$", ADMIN_WRITE),
    _route("DELETE", r"^/api/admin/invites/([^/]+)$", ADMIN_WRITE, "invite", 1),
    _route("GET", r"^/api/admin/managed-config$", ADMIN_READ),
    _route(("POST", "PUT"), r"^/api/admin/managed-config$", ADMIN_WRITE),
    _route("GET", r"^/api/admin/workers$", ADMIN_READ),

    # control 会话目录
    _route("GET", r"^/api/sessions$", CONTROL_READ),
    _route("POST", r"^/api/sessions$", CONTROL_WRITE),
    _route(("PATCH", "DELETE"), r"^/api/sessions/([^/]+)$",
           CONTROL_WRITE, "session", 1),
    _route("GET", r"^/api/sessions/([^/]+)/messages$",
           CONTROL_READ, "session", 1),
    _route("GET", r"^/api/sessions/([^/]+)/runs$",
           CONTROL_READ, "session", 1),

    # 个人模型设置（control 权限/覆盖服务）
    _route("GET", r"^/api/settings/models$", CONTROL_READ),
    _route("POST", r"^/api/settings/models/save$", CONTROL_WRITE),
    _route("POST", r"^/api/settings/models/available$", CONTROL_WRITE),
    _route("POST", r"^/api/settings/models/selftest$", CONTROL_WRITE),
    _route("DELETE", r"^/api/settings/models/overrides/([^/]+)$",
           CONTROL_WRITE, "channel", 1),

    # 受管底座
    _route("GET", r"^/api/settings/harness$", MANAGED_READ),
    _route("POST", r"^/api/settings/harness$", MANAGED_WRITE),
    _route("POST", r"^/api/settings/harness/models/([^/]+)$",
           MANAGED_WRITE, "runtime", 1),
    _route("POST", r"^/api/settings/chat/transport$", MANAGED_WRITE),
    _route("GET", r"^/api/settings/bootstrap$", ADMIN_READ),
    _route("GET", r"^/api/settings/local-agents$", ADMIN_READ),
    _route("POST", r"^/api/settings/local-agents/enable$", MANAGED_WRITE),

    # 环境与 gateway
    _route("POST", r"^/api/env$", MANAGED_WRITE),
    _route("POST", r"^/api/env/install$", MANAGED_WRITE),
    _route("GET", r"^/api/env/tools$", ADMIN_READ),
    _route("GET", r"^/api/env/job/([^/]+)$", ADMIN_READ, "job", 1),
    _route("POST", r"^/api/gateway/restart$", MANAGED_WRITE),

    # 本地状态
    _route("GET", r"^/api/status$", WORKSPACE_READ),

    # 任务/运行
    _route("GET", r"^/api/chat/last/([^/]+)$", TASK, "session", 1),
    _route("GET", r"^/api/chat/jobs/([^/]+)/stream$", TASK, "turn", 1),
    _route("POST", r"^/api/chat/stream$", TASK),
    _route("POST", r"^/api/chat/question/answer$", TASK),
    _route("POST", r"^/api/chat/question/status$", TASK),
    _route("POST", r"^/api/chat/stop$", TASK),
    _route("POST", r"^/api/chat$", TASK),
    _route("POST", r"^/api/skill$", TASK),
    _route("DELETE", r"^/api/session/([^/]+)$", TASK, "session", 1),

    # 本人 workspace 内容
    _route("GET", r"^/api/personas$", WORKSPACE_READ),
    _route("GET", r"^/api/persona/([^/]+)$", WORKSPACE_READ, "persona", 1),
    _route("GET", r"^/api/persona/([^/]+)/files$",
           WORKSPACE_READ, "persona", 1),
    _route(("PUT", "DELETE"), r"^/api/persona/([^/]+)/file$",
           WORKSPACE_WRITE, "persona", 1),
    _route("DELETE", r"^/api/persona/([^/]+)$",
           WORKSPACE_WRITE, "persona", 1),
    _route("GET", r"^/api/skills$", WORKSPACE_READ),
    _route("GET", r"^/api/skill/([^/]+)$", WORKSPACE_READ, "skill", 1),
    _route("POST", r"^/api/skill/prepare$", WORKSPACE_WRITE),
    _route("GET", r"^/api/skill/status/([^/]+)$",
           WORKSPACE_READ, "skill", 1),
    _route("POST", r"^/api/profile/baseline$", WORKSPACE_WRITE),
    _route("GET", r"^/api/outputs$", WORKSPACE_READ),
    _route("GET", r"^/api/artifacts/archive$", WORKSPACE_READ),
    _route("GET", r"^/api/output/(.+)$", WORKSPACE_READ, "output", 1),
    _route("GET", r"^/api/media/(.+)$", WORKSPACE_READ, "media", 1),
    _route("DELETE", r"^/api/output/(.+)$", WORKSPACE_WRITE, "output", 1),
    _route("POST", r"^/api/upload$", WORKSPACE_WRITE),
    _route("GET", r"^/api/upload/limits$", WORKSPACE_READ),
    # 多人模式明确拒绝：任意服务器路径采用文件
    _route("POST", r"^/api/upload/local$", FORBIDDEN),
    _route("GET", r"^/api/accounts$", WORKSPACE_READ),
    # 登录/发布/抓取会启动后台或浏览器任务：统一按 TASK 分类并经过 Run 账本
    _route("POST", r"^/api/login/([^/]+)$", TASK, "platform", 1),
    _route("GET", r"^/api/login/([^/]+)/status$",
           WORKSPACE_READ, "platform", 1),
    _route("POST", r"^/api/login/([^/]+)/sms$", TASK, "platform", 1),
    _route("POST", r"^/api/login/([^/]+)/stop$", TASK, "platform", 1),
    _route("GET", r"^/api/accounts/([^/]+)/credentials$",
           WORKSPACE_READ, "platform", 1),
    _route("POST", r"^/api/accounts/([^/]+)/credentials$",
           TASK, "platform", 1),
    _route("POST", r"^/api/accounts/([^/]+)/mp-login$",
           TASK, "platform", 1),
    _route("GET", r"^/api/accounts/([^/]+)/mp-login/status$",
           WORKSPACE_READ, "platform", 1),
    _route("POST", r"^/api/accounts/([^/]+)/mp-login/stop$",
           TASK, "platform", 1),
    _route("GET", r"^/api/accounts/([^/]+)/qr$",
           WORKSPACE_READ, "platform", 1),
    _route("GET", r"^/api/accounts/([^/]+)/mp-login/qr$",
           WORKSPACE_READ, "platform", 1),
    _route("POST", r"^/api/accounts/([^/]+)/whoami$",
           TASK, "platform", 1),
    _route("GET", r"^/api/accounts/([^/]+)/whoami$",
           WORKSPACE_READ, "platform", 1),
    _route("POST", r"^/api/logout/([^/]+)$", TASK, "platform", 1),
    _route("GET", r"^/api/analytics/platforms$", WORKSPACE_READ),
    # GET 只读已完成的缓存；启动浏览器抓取必须是显式 POST。
    _route("GET", r"^/api/analytics/([^/]+)$",
           WORKSPACE_READ, "platform", 1),
    _route("POST", r"^/api/analytics/([^/]+)$", TASK, "platform", 1),
    _route("GET", r"^/api/publishing-policy$", WORKSPACE_READ),
    _route("POST", r"^/api/manual-publish/([^/]+)/export$",
           TASK, "platform", 1),
    _route("GET", r"^/api/publish/([^/]+)/status$",
           WORKSPACE_READ, "platform", 1),
    _route("POST", r"^/api/publish/([^/]+)/sms$", TASK, "platform", 1),
    _route("POST", r"^/api/publish/([^/]+)$", TASK, "platform", 1),
    _route("POST", r"^/api/profile/build$", TASK),
    _route("GET", r"^/api/profile/build/status/([^/]+)$",
           WORKSPACE_READ, "profile", 1),
    _route("GET", r"^/api/trends$", WORKSPACE_READ),
    _route("GET", r"^/api/schedule$", WORKSPACE_READ),
    _route("POST", r"^/api/schedule$", WORKSPACE_WRITE),
    _route("GET", r"^/api/schedule/context$", WORKSPACE_READ),
    _route(("PUT", "DELETE"), r"^/api/schedule/([^/]+)$",
           WORKSPACE_WRITE, "schedule", 1),
    _route("GET", r"^/api/ideas$", WORKSPACE_READ),
    _route("POST", r"^/api/ideas$", WORKSPACE_WRITE),
    _route(("PUT", "DELETE"), r"^/api/ideas/([^/]+)$",
           WORKSPACE_WRITE, "idea", 1),
    # 剪藏扩展是特殊通道，多人模式默认关闭
    _route("POST", r"^/api/clipper$", FORBIDDEN),

    # worker 内部接口：只允许已验证服务凭证 + 逐 Run 签名的 relay 调用；
    # control 自身的 authorize 对其它身份一律拒绝。
    _route("GET", r"^/internal/lifecycle/health$", INTERNAL),
    _route("POST", r"^/internal/runs/([^/]+)$", INTERNAL, "run", 1),
    _route("GET", r"^/internal/runs/([^/]+)/events$", INTERNAL, "run", 1),
    _route("POST", r"^/internal/runs/([^/]+)/stop$", INTERNAL, "run", 1),
    _route("POST", r"^/internal/runs/([^/]+)/questions/([^/]+)/answer$",
           INTERNAL, "run", 1),
    _route("POST", r"^/internal/runs/([^/]+)/questions/status$",
           INTERNAL, "run", 1),
    _route("GET", r"^/internal/sessions/([^/]+)/messages$",
           INTERNAL, "session", 1),
)


def _method_key(method: str) -> str:
    """HEAD 按 GET 分类；其他方法大写。"""
    normalized = method.upper()
    return "GET" if normalized == "HEAD" else normalized


def _find(method: str, path: str) -> tuple[_Route, re.Match] | None:
    normalized = _method_key(method)
    for rule in _RULES:
        if normalized in rule.methods:
            match = rule.pattern.fullmatch(path)
            if match is not None:
                return rule, match
    return None


def public_route(method: str, path: str) -> bool:
    """只有注册为 public 的精确路由无需登录。"""
    found = _find(method, path)
    return found is not None and found[0].kind == PUBLIC


def authorize(actor: Identity, method: str, path: str) -> RouteDecision:
    """分类候选路由并按角色做路由级授权。

    ``actor`` 必须是认证层刚从数据库读出的身份。返回 ``unknown`` 表示该
    method+path 根本未登记；调用方应默认拒绝。资源归属仍需 Store 另查。
    """
    found = _find(method, path)
    if found is None:
        return RouteDecision(UNKNOWN)
    rule, match = found
    resource_id = (match.group(rule.resource_id_group)
                   if rule.resource_id_group is not None else None)
    if rule.kind == PUBLIC:
        return RouteDecision(PUBLIC, rule.resource_type, resource_id)
    if rule.kind in _ALWAYS_DENIED:
        return RouteDecision(FORBIDDEN, rule.resource_type, resource_id)
    if actor.role != "admin" and rule.kind in _ADMIN_ONLY:
        return RouteDecision(FORBIDDEN, rule.resource_type, resource_id)
    return RouteDecision(rule.kind, rule.resource_type, resource_id)


def worker_allowed(method: str, path: str) -> bool:
    """worker 只服务本人 workspace 的读写与任务路由。

    受管底座、环境、管理员、control 专属路由以及未知路由一律拒绝；内部
    服务凭证只证明“到达本 worker 的资格”，不授予这些能力。
    """
    found = _find(method, path)
    return found is not None and found[0].kind in _WORKER_ALLOWED
