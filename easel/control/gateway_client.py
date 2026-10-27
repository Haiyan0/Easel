"""control→gateway 的正式签名 HTTP 客户端。

边界：

* 目标只来自受信 ``WorkerHandle.gateway_upstream``，不读取请求体、环境或
  客户端地址；缺少 gateway 实例/代次/上游时明确失败关闭。
* URL、canonical query 与一次性序列化的 UTF-8 body 是签名输入，发送的最终
  ``httpx.Request`` 必须与签名输入完全一致。
* 浏览器 Cookie、Authorization、Forwarded、``X-Easel-*`` 等默认头在最终
  Request 上被删除；只注入唯一断言头和必要的 JSON 内容头。
* 不重试、不跟随重定向；错误只返回稳定 code，保留合理的 ``Retry-After``，
  不暴露 gateway/worker 地址、ID、断言或 token。
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..gateway.auth import (
    GatewayAudience,
    GatewayError,
    canonical_query,
)
from ..worker.guard import no_client_auth
from .gateway_auth import build_gateway_assertion_headers
from .types import DomainError, WorkerHandle

_MAX_RETRY_AFTER = 3600

_STRIP_DEFAULT_HEADERS = frozenset({
    "cookie", "authorization", "forwarded", "origin", "referer", "via",
    "x-real-ip",
})


def _retry_after(response: httpx.Response) -> int | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    value = value.strip()
    if not value.isascii() or not value.isdigit():
        return None
    number = int(value)
    if not 1 <= number <= _MAX_RETRY_AFTER:
        return None
    return number


def _gateway_error(response: httpx.Response) -> str:
    """只读取有界的错误码；不把响应正文或底层异常带出本模块。"""
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("error")
    if (not isinstance(value, str) or not value.startswith("gateway_")
            or len(value) > 64
            or not value.isascii()
            or any(not (character.islower() or character.isdigit()
                        or character == "_") for character in value)):
        return ""
    return value


def _serialize_payload(payload: object) -> bytes:
    try:
        return json.dumps(
            payload, ensure_ascii=False, allow_nan=False,
            separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise DomainError("invalid_request", 400) from None


class GatewayClient:
    """以一次请求一个断言的方式调用固定 gateway。"""

    def __init__(self, signing_key: Ed25519PrivateKey,
                 http_client: httpx.AsyncClient, *,
                 clock: Callable[[], float] = time.time) -> None:
        if not isinstance(signing_key, Ed25519PrivateKey):
            raise TypeError("signing_key must be Ed25519PrivateKey")
        if not isinstance(http_client, httpx.AsyncClient):
            raise TypeError("http_client must be AsyncClient")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._key = signing_key
        self._http = http_client
        self._clock = clock

    async def request(self, handle: WorkerHandle, method: str, path: str, *,
                      query: Mapping[str, str]
                      | Iterable[tuple[str, str]] | None = None,
                      payload: object | None = None) -> httpx.Response:
        audience = self._audience(handle)
        normalized_method = method.upper() if isinstance(method, str) else ""
        if normalized_method not in ("GET", "POST"):
            raise DomainError("invalid_request", 400)
        if not isinstance(path, str) or not path.startswith("/") \
                or "?" in path or "#" in path or "\\" in path:
            raise DomainError("invalid_request", 400)
        query_text = canonical_query({} if query is None else query)
        body = b"" if payload is None else _serialize_payload(payload)
        try:
            assertion = build_gateway_assertion_headers(
                self._key, audience, method=normalized_method, path=path,
                query=query, body=body, clock=self._clock)
        except (ValueError, GatewayError):
            raise DomainError("invalid_request", 400) from None
        upstream = self._origin(handle.gateway_upstream)
        url = upstream + path
        if query_text:
            url += "?" + query_text
        headers = dict(assertion)
        if body:
            headers["Content-Type"] = "application/json"
        try:
            request = self._http.build_request(
                normalized_method, url, headers=headers, content=body)
            # build_request 会合并 client 默认 params/headers/cookies/auth；
            # 最终 Request 必须精确恢复到已签名 URL 与最小头集合。
            request.url = httpx.URL(url)
            self._clean_headers(request, assertion)
            response = await self._http.send(
                request, auth=no_client_auth(), follow_redirects=False)
        except DomainError:
            raise
        except httpx.HTTPError:
            raise DomainError(
                "worker_gateway_unavailable", 503,
                retry_after=5) from None
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise DomainError("upstream_redirect", 502)
        if response.status_code >= 400:
            await self._raise_for_status(response)
        return response

    async def health(self, handle: WorkerHandle) -> bool:
        """经固定 lifecycle 路由确认 gateway 确实能到达同一 worker。"""
        try:
            response = await self.request(
                handle, "GET", "/internal/lifecycle/health")
        except DomainError:
            return False
        try:
            if not 200 <= response.status_code < 300:
                return False
            try:
                payload = response.json()
            except ValueError:
                return False
            return bool(
                isinstance(payload, dict)
                and payload.get("workspace_id") == handle.workspace_id
                and payload.get("container_id") == handle.container_id
                and payload.get("config_version") == handle.config_version
                and payload.get("ready") is True
                and payload.get("execution_capable") is True)
        finally:
            await response.aclose()

    @staticmethod
    def _audience(handle: WorkerHandle) -> GatewayAudience:
        if not isinstance(handle, WorkerHandle):
            raise DomainError("worker_gateway_unavailable", 503)
        if (not handle.gateway_instance_id
                or not handle.gateway_generation
                or not handle.gateway_upstream
                or handle.gateway_state != "available"):
            raise DomainError("worker_gateway_unavailable", 503)
        try:
            return GatewayAudience(
                handle.workspace_id, handle.gateway_instance_id,
                handle.gateway_generation, handle.container_id,
                handle.config_version)
        except ValueError:
            raise DomainError("worker_gateway_unavailable", 503) from None

    @staticmethod
    def _origin(value: object) -> str:
        if not isinstance(value, str) or not value:
            raise DomainError("worker_gateway_unavailable", 503)
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError:
            raise DomainError("worker_gateway_unavailable", 503) from None
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query
                or parsed.fragment or port is None):
            raise DomainError("worker_gateway_unavailable", 503)
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _clean_headers(request: httpx.Request,
                       assertion: Mapping[str, str]) -> None:
        assert_header = next(iter(assertion))
        allowed = {assert_header.lower(), "host"}
        if (request.headers.get("content-length") not in (None, "0")
                or request.headers.get("content-type") is not None):
            allowed.update({"content-type", "content-length"})
        for name in {key.lower() for key in request.headers}:
            if name not in allowed:
                try:
                    del request.headers[name]
                except KeyError:
                    pass
        request.headers[assert_header] = assertion[assert_header]
        if "content-length" not in {key.lower() for key in request.headers} \
                and request.method == "POST":
            request.headers["Content-Length"] = str(
                len(request.content or b""))

    async def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        retry_after = _retry_after(response)
        gateway_error = _gateway_error(response)
        await response.aclose()
        if status == 429:
            raise DomainError(
                "worker_gateway_busy", 429,
                retry_after=retry_after or 1)
        if status in (401, 403):
            if gateway_error:
                raise DomainError(
                    "worker_gateway_unavailable", 503,
                    retry_after=retry_after or 5)
            raise DomainError("worker_rejected", 502)
        if status == 503:
            if gateway_error in (
                    "gateway_inactive", "gateway_unavailable",
                    "gateway_nonce_capacity", "gateway_unauthorized",
                    "gateway_forbidden"):
                raise DomainError(
                    "worker_gateway_unavailable", 503,
                    retry_after=retry_after or 5)
            raise DomainError("upstream_unavailable", 502)
        if status == 504:
            raise DomainError("upstream_timeout", 504)
        if status in (400, 413, 415, 422):
            raise DomainError("invalid_request", 400)
        if status == 404:
            raise DomainError("not_found", 404)
        if status == 409:
            raise DomainError("conflict", 409)
        if status >= 500:
            raise DomainError("upstream_unavailable", 502)
        raise DomainError("upstream_invalid", 502)
