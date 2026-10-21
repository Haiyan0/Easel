"""worker 容器的最小生命周期入口。

本进程只回答"我是谁、是哪个配置版本、是否可接新请求"，**不**加载签名快照、
不执行 prompt，也不接纳任务。真正执行必须走 Task 6 的运行入口：读取合法的
Run 级签名快照并完成执行认领。因此常驻 worker 容器不会把首次快照变成永久
授权，也不会因为 health=200 就能执行任何东西。

认证沿用 control→worker 的内部头（workspace + service token）；缺失或不匹配
统一 401，响应体不回显期望值。payload 不含 token。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Mapping
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from easel.control.types import DomainError
from easel.worker.guard import (SERVICE_TOKEN_ENV, SERVICE_TOKEN_HEADER,
                                WORKSPACE_HEADER, WORKSPACE_ID_ENV,
                                load_worker_identity, verify_worker_request)

CONFIG_VERSION_ENV = "EASEL_CONFIG_VERSION"
CONTAINER_ID_ENV = "EASEL_CONTAINER_ID"

LIFECYCLE_PATH = "/internal/lifecycle/health"
LIFECYCLE_PORT = 7860
LIFECYCLE_HOST = "0.0.0.0"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class LifecycleDraining(Exception):
    """进程已进入 draining；拒绝开始新的请求。"""


@dataclass(frozen=True)
class LifecycleConfig:
    """worker 启动身份；服务 token 不进入默认 repr。"""

    workspace_id: str
    config_version: int
    container_id: str
    service_token: str = field(repr=False)


class RequestCounter:
    """记录进行中的请求，并在 draining 后拒绝新请求。

    health 探测本身不计入；只有真正执行业务的请求通过 ``track()`` 包裹，
    control 据此判断是否还有在途请求，避免提前停止容器。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self._draining = False

    @contextmanager
    def track(self) -> Iterator[None]:
        with self._lock:
            if self._draining:
                raise LifecycleDraining("draining")
            self._active += 1
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1

    def start_drain(self) -> None:
        with self._lock:
            self._draining = True

    def snapshot(self) -> tuple[int, bool]:
        with self._lock:
            return self._active, self._draining


def load_lifecycle_config(
        environ: Mapping[str, str] | None = None) -> LifecycleConfig:
    """读取并严格校验生命周期身份；缺项或非法值启动失败。"""
    env = os.environ if environ is None else environ
    identity = load_worker_identity(env)
    if identity is None:
        raise RuntimeError("lifecycle 入口必须以 worker 模式启动")

    raw_version = (env.get(CONFIG_VERSION_ENV) or "").strip()
    if not raw_version.isascii() or not raw_version.isdecimal():
        raise DomainError("invalid_config_version", 500)
    config_version = int(raw_version)

    container_id = (env.get(CONTAINER_ID_ENV) or "").strip()
    if not _IDENTIFIER.match(container_id):
        raise DomainError("invalid_container_id", 500)
    if not _IDENTIFIER.match(identity.workspace_id):
        raise DomainError("invalid_workspace_id", 500)
    return LifecycleConfig(identity.workspace_id, config_version, container_id,
                           identity.service_token)


def build_payload(config: LifecycleConfig,
                  counter: RequestCounter) -> dict[str, object]:
    """身份 + 版本 + 活动请求数；永远不含服务 token。"""
    active, draining = counter.snapshot()
    return {
        "workspace_id": config.workspace_id,
        "config_version": config.config_version,
        "container_id": config.container_id,
        "ready": not draining,
        "draining": draining,
        "active_requests": active,
    }


def create_lifecycle_app(config: LifecycleConfig,
                         counter: RequestCounter) -> FastAPI:
    """只暴露健康端点的最小 ASGI 应用；不加载快照、不注册任务路由。"""
    app = FastAPI(title="Easel Worker Lifecycle", docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.get(LIFECYCLE_PATH)
    def lifecycle_health(request: Request) -> JSONResponse:
        try:
            verify_worker_request(request.headers, config.workspace_id,
                                  config.service_token)
        except DomainError as error:
            return JSONResponse({"error": error.code},
                                status_code=error.status)
        return JSONResponse(build_payload(config, counter))

    return app


def _install_drain_signals(counter: RequestCounter) -> None:
    """容器收到 SIGTERM 时先进入 draining，让 health 立刻报告不可接新任务。"""
    def handler(signum, frame):  # noqa: ARG001 - signal 回调签名固定
        counter.start_drain()

    for name in ("SIGTERM", "SIGINT"):
        signal_value = getattr(signal, name, None)
        if signal_value is not None:
            try:
                signal.signal(signal_value, handler)
            except (OSError, ValueError):
                # 非主线程或平台不支持时忽略；健康端点仍可工作
                pass


def _healthcheck(config: LifecycleConfig, port: int) -> int:
    """进程内自检：只有身份/版本/容器 ID 匹配且 ready 才返回 0。"""
    request = UrlRequest(
        f"http://127.0.0.1:{port}{LIFECYCLE_PATH}",
        headers={})
    request.add_header(WORKSPACE_HEADER, config.workspace_id)
    request.add_header(SERVICE_TOKEN_HEADER, config.service_token)
    try:
        with urlopen(request, timeout=3) as response:  # noqa: S310 - 固定回环地址
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return 1
    expected = {
        "workspace_id": config.workspace_id,
        "config_version": config.config_version,
        "container_id": config.container_id,
        "ready": True,
    }
    return 0 if all(payload.get(key) == value
                    for key, value in expected.items()) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="easel-worker-lifecycle")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve_parser = subparsers.add_parser("serve", help="启动生命周期入口")
    serve_parser.add_argument("--host", default=LIFECYCLE_HOST)
    serve_parser.add_argument("--port", type=int, default=LIFECYCLE_PORT)
    check_parser = subparsers.add_parser("healthcheck", help="进程内健康自检")
    check_parser.add_argument("--port", type=int, default=LIFECYCLE_PORT)
    args = parser.parse_args(argv)

    config = load_lifecycle_config()
    if args.command == "healthcheck":
        return _healthcheck(config, args.port)

    import uvicorn

    counter = RequestCounter()
    _install_drain_signals(counter)
    app = create_lifecycle_app(config, counter)
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
