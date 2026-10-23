"""worker 入口守卫、启动身份与内部转发头清洗。

约定：

* ``EASEL_SERVICE_MODE``：``local``（默认）/ ``worker``。control 使用
  ``easel serve-control``，不通过本模块启动；其它取值直接启动失败。
* ``EASEL_WORKSPACE_ID`` / ``EASEL_SERVICE_TOKEN``：worker 模式的固定实例身份，
  只能来自启动配置，不从请求推断。缺少任一项即启动失败，不回退 local。
* ``X-Easel-Workspace-Id`` / ``X-Easel-Service-Token``：control 到 worker 的
  内部头。转发前先剥离所有外来 ``X-Easel-*``，再从服务端持有的 worker 句柄
  重建，绝不透传浏览器提交的值。

服务 token 不进入日志、错误消息或 ``WorkerIdentity`` 的默认 repr。
"""
from __future__ import annotations

import hmac
import os
from dataclasses import dataclass, field
from typing import Mapping

import httpx

from easel.control.types import DomainError

SERVICE_MODE_ENV = "EASEL_SERVICE_MODE"
WORKSPACE_ID_ENV = "EASEL_WORKSPACE_ID"
SERVICE_TOKEN_ENV = "EASEL_SERVICE_TOKEN"

WORKSPACE_HEADER = "X-Easel-Workspace-Id"
SERVICE_TOKEN_HEADER = "X-Easel-Service-Token"

_STRIPPED_HEADERS = frozenset({"authorization", "cookie", "forwarded"})
_STRIPPED_PREFIXES = ("x-easel-", "x-forwarded-")


class _NoAuth(httpx.Auth):
    """显式覆盖共享 client 的自动 auth，避免 Basic/Bearer 被重新注入。"""

    def auth_flow(self, request):  # type: ignore[override]
        yield request


_NO_AUTH = _NoAuth()


@dataclass(frozen=True)
class WorkerIdentity:
    """worker 启动时固定的实例身份；token 不出现在默认 repr。"""

    workspace_id: str
    service_token: str = field(repr=False)


def load_worker_identity(
        environ: Mapping[str, str] | None = None) -> WorkerIdentity | None:
    """读取启动配置；local 返回 None，worker 缺配置或模式非法时启动失败。"""
    env = os.environ if environ is None else environ
    mode = (env.get(SERVICE_MODE_ENV) or "local").strip().lower()
    if mode == "local":
        return None
    if mode != "worker":
        raise RuntimeError(
            f"{SERVICE_MODE_ENV}={mode} 不受 web 后端支持；"
            "control 请使用 easel serve-control")
    workspace_id = (env.get(WORKSPACE_ID_ENV) or "").strip()
    service_token = (env.get(SERVICE_TOKEN_ENV) or "").strip()
    if not workspace_id or not service_token:
        raise RuntimeError(
            f"worker 模式必须配置 {WORKSPACE_ID_ENV} 与 {SERVICE_TOKEN_ENV}")
    if not workspace_id.isascii() or not service_token.isascii():
        raise RuntimeError("worker 身份配置必须为 ASCII")
    return WorkerIdentity(workspace_id, service_token)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """大小写不敏感地取头；非字符串值按缺失处理。"""
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return value if isinstance(value, str) else None
    return None


def verify_worker_request(headers: Mapping[str, str],
                          expected_workspace_id: str,
                          expected_token: str) -> None:
    """同时校验固定 workspace 与独立服务凭证；不接受任何角色头。

    非法输入统一 401，消息中不包含期望值或实际 token。
    """
    if (not expected_workspace_id or not expected_token
            or not expected_workspace_id.isascii()
            or not expected_token.isascii()):
        raise DomainError("worker_misconfigured", 500)
    workspace_id = _header(headers, WORKSPACE_HEADER)
    token = _header(headers, SERVICE_TOKEN_HEADER)
    if (workspace_id is None or token is None
            or not workspace_id.isascii() or not token.isascii()
            or not hmac.compare_digest(workspace_id, expected_workspace_id)
            or not hmac.compare_digest(token, expected_token)):
        raise DomainError("unauthorized", 401)


def sanitize_forward_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """剥离外来凭证与转发链头；大小写不敏感，保留其余业务头。"""
    cleaned: dict[str, str] = {}
    for key, value in headers.items():
        lowered = str(key).lower()
        if (lowered in _STRIPPED_HEADERS
                or lowered.startswith(_STRIPPED_PREFIXES)):
            continue
        cleaned[key] = value
    return cleaned


def build_worker_headers(headers: Mapping[str, str], *,
                         workspace_id: str,
                         service_token: str) -> dict[str, str]:
    """先清洗，再从服务端固定身份重建内部头。"""
    cleaned = sanitize_forward_headers(headers)
    cleaned[WORKSPACE_HEADER] = workspace_id
    cleaned[SERVICE_TOKEN_HEADER] = service_token
    return cleaned


def strip_request_headers(headers: httpx.Headers) -> None:
    """删除最终 Request 上由 client 默认 headers/cookies 合并进来的禁用头。

    同步与异步转发共用同一条终态清洗规则，避免两套实现漂移。
    """
    for key in {name.lower() for name in headers.keys()}:
        if (key in _STRIPPED_HEADERS
                or key.startswith(_STRIPPED_PREFIXES)):
            del headers[key]


def no_client_auth() -> httpx.Auth:
    """返回不注入任何凭证的 auth 覆盖，供 ``send(auth=...)`` 使用。"""
    return _NO_AUTH


def forward_to_worker(client: httpx.Client, method: str, url: str, *,
                      headers: Mapping[str, str],
                      workspace_id: str, service_token: str,
                      content: bytes | str | None = None,
                      params: Mapping[str, str] | None = None
                      ) -> httpx.Response:
    """最小转发适配器；保证最终发出的请求经过清洗。

    不能只清洗传入的 ``headers``：``Client.build_request`` 会把 client 默认
    headers、cookie jar 与 base_url 合并进 Request，``Client.send`` 还会应用
    自动 auth。这里先构建 Request，再从最终 headers 删除禁用项，最后用无操作
    auth 覆盖 client 默认认证；不修改共享 client 的 headers/cookies/auth。
    """
    headers = build_worker_headers(
        headers, workspace_id=workspace_id, service_token=service_token)
    request = client.build_request(method, url, headers=headers,
                                   content=content, params=params)
    # 默认 headers/cookies 已合并；统一删除后再从服务端固定身份重建内部头
    strip_request_headers(request.headers)
    request.headers[WORKSPACE_HEADER] = workspace_id
    request.headers[SERVICE_TOKEN_HEADER] = service_token
    return client.send(request, auth=_NO_AUTH, follow_redirects=False)
