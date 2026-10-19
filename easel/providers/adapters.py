"""Provider broker 的协议适配契约。

适配器只负责按精确 method + 相对 path 构造上游请求；目标地址来自服务端
解析后的路由，客户端提交的 Host、Authorization、目标 URL、转发头一律不参与。
凭证只在 adapter 内从 token 快照解密，不返回给 worker。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Mapping
from urllib.parse import urlsplit

from ..control.types import DomainError, EffectiveRoute

_BODY_KINDS = ("json", "multipart", "none")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9._~!$&'()*+,;=:@/-]+$")
_SAFE_HEADER = re.compile(r"^[A-Za-z0-9-]+$")
_FORBIDDEN_UPSTREAM_HEADERS = frozenset({
    "host", "authorization", "proxy-authorization", "cookie", "set-cookie",
    "connection", "keep-alive", "proxy-connection", "te", "trailer",
    "transfer-encoding", "upgrade", "content-length", "forwarded",
    "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
    "x-real-ip", "cf-connecting-ip", "true-client-ip",
})


@dataclass(frozen=True)
class UpstreamRequest:
    """发给上游的最终请求；headers 含认证信息，默认 repr 不显示。"""

    method: str
    url: str
    headers: dict[str, str] = field(repr=False)


@dataclass(frozen=True)
class AdapterRoute:
    """一条精确允许的 broker 路由。"""

    method: str
    relative_path: str
    body: Literal["json", "multipart", "none"] = "json"


def _relative_path_safe(relative_path: object) -> bool:
    if not isinstance(relative_path, str) or not relative_path:
        return False
    if (relative_path.startswith("/") or "\\" in relative_path
            or "?" in relative_path or "#" in relative_path
            or "//" in relative_path
            or any(segment in ("", ".", "..") for segment in relative_path.split("/"))
            or _SAFE_PATH.fullmatch(relative_path) is None):
        return False
    return True


def _join_base_url(base_url: str, relative_path: str) -> str:
    """按已验证的 base URL 拼接精确相对路径，不允许改变主机/协议。"""
    if not _relative_path_safe(relative_path):
        raise DomainError("invalid_request", 400)
    try:
        parsed = urlsplit(base_url)
    except (TypeError, ValueError):
        raise DomainError("invalid_destination", 400) from None
    if (parsed.scheme.lower() != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise DomainError("invalid_destination", 400)
    base_path = parsed.path.rstrip("/")
    return f"https://{parsed.netloc}{base_path}/{relative_path}"


class ProviderAdapter:
    """受限适配器基类；子类声明 adapter_id、协议与精确路由。"""

    adapter_id: str = ""
    protocols: frozenset[str] = frozenset()
    routes: tuple[AdapterRoute, ...] = ()
    auth_env: str = ""

    def __init__(self, credentials: Mapping[str, str] | None = None):
        self._credentials = dict(credentials or {})

    def route_for(self, method: str, relative_path: str) -> AdapterRoute | None:
        if not isinstance(method, str) or not _relative_path_safe(relative_path):
            return None
        normalized = method.upper()
        for route in self.routes:
            if route.method == normalized and route.relative_path == relative_path:
                return route
        return None

    def accepts(self, method: str, relative_path: str) -> bool:
        return self.route_for(method, relative_path) is not None

    def prepare(self, route: EffectiveRoute, method: str, relative_path: str,
                headers: Mapping[str, str]) -> UpstreamRequest:
        if route.protocol not in self.protocols:
            raise DomainError("capability_unavailable", 503)
        matched = self.route_for(method, relative_path)
        if matched is None:
            raise DomainError("not_found", 404)
        url = _join_base_url(route.base_url, matched.relative_path)
        prepared = self._sanitized_headers(headers)
        prepared.update(self._auth_headers())
        url = self._adjust_url(url)
        return UpstreamRequest(matched.method, url, prepared)

    # ------------------------------------------------------------------
    # 子类钩子
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        key = self._credentials.get(self.auth_env, "")
        if not key:
            raise DomainError("secret_unavailable", 500)
        return {"authorization": f"Bearer {key}"}

    def _adjust_url(self, url: str) -> str:
        return url

    def _extra_headers(self) -> dict[str, str]:
        return {}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _sanitized_headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        """只保留合法内容协商头；认证/身份/转发头全部重建。"""
        result: dict[str, str] = {}
        for name in ("content-type", "accept", "content-length"):
            value = headers.get(name) if hasattr(headers, "get") else None
            if isinstance(value, str) and value and "\r" not in value and "\n" not in value:
                result[name] = value
        result.setdefault("user-agent", "Easel-Broker/1")
        result.update(self._extra_headers())
        return result

    def _credential(self, name: str) -> str:
        value = self._credentials.get(name, "")
        if not isinstance(value, str) or not value:
            raise DomainError("secret_unavailable", 500)
        return value


def safe_header_name(name: object) -> str | None:
    if not isinstance(name, str) or not name or name.lower() in _FORBIDDEN_UPSTREAM_HEADERS:
        return None
    if _SAFE_HEADER.fullmatch(name) is None:
        return None
    return name
