"""control 侧 gateway 逐请求签名工具。

本轮只提供签名函数，不接入现有 control 路由或 WorkerRelay。私钥只由
control 的签名进程持有；gateway 只加载对应公钥。
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.gateway.auth import (
    GATEWAY_ASSERTION_HEADER,
    GatewayAudience,
    sign_gateway_assertion,
)


def build_gateway_assertion_headers(
        private_key: Ed25519PrivateKey,
        audience: GatewayAudience, *,
        method: str,
        path: str,
        query: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        body: bytes = b"",
        issued_at: int | None = None,
        expires_at: int | None = None,
        nonce: str | None = None,
        request_id: str | None = None,
        clock: Callable[[], float] = time.time) -> dict[str, str]:
    """生成唯一内部断言头；不负责发送请求或保存私钥。"""
    token = sign_gateway_assertion(
        private_key, audience, method=method, path=path, query=query,
        body=body, issued_at=issued_at, expires_at=expires_at, nonce=nonce,
        request_id=request_id, clock=clock)
    return {GATEWAY_ASSERTION_HEADER: token}
