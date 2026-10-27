"""常驻 worker 的单一执行入口。

本模块把 ``web.app`` 的常驻 RunManager 路由与 lifecycle health 合并到同一个
FastAPI 进程；不另建第二套 Run 状态机，也不把健康端点拆到独立服务器。

启动前必须已有固定 worker 身份和只读公钥；``web.app`` 导入时已完成
RunManager 初始化。healthcheck 子命令只读回环 health，输出最小 JSON 供
provisioner 复核，不含服务 token。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from easel.control.types import DomainError
from easel.worker.guard import (
    SERVICE_TOKEN_HEADER,
    WORKSPACE_HEADER,
    verify_worker_request,
)
from easel.worker.lifecycle import (
    LIFECYCLE_PATH,
    LifecycleConfig,
    RequestCounter,
    build_payload,
    load_lifecycle_config,
)

PUBLIC_KEY_CONTAINER_PATH = "/keys/worker-public.key"
PUBLIC_KEY_ENV = "EASEL_WORKER_PUBLIC_KEY_FILE"
HEALTHCHECK_TIMEOUT_SECONDS = 3.0


def _attach_lifecycle(app: FastAPI, config: LifecycleConfig,
                      counter: RequestCounter, run_manager: Any) -> None:
    """把 lifecycle 路由挂到已有 app；active_runs 来自真实 RunManager。"""
    if any(getattr(route, "path", None) == LIFECYCLE_PATH
           for route in app.routes):
        return

    @app.get(LIFECYCLE_PATH)
    def lifecycle_health(request: Request) -> JSONResponse:
        try:
            verify_worker_request(
                request.headers, config.workspace_id,
                config.service_token)
        except DomainError as error:
            return JSONResponse({"error": error.code},
                                status_code=error.status)
        return JSONResponse(build_payload(
            config, counter,
            active_runs=run_manager.active_runs(),
            execution_capable=True,
        ))

    return


class WorkerServer(uvicorn.Server):
    """把 drain 接到 Uvicorn 自己的信号处理器，避免被外层注册覆盖。

    Uvicorn 在 ``capture_signals`` 中把 SIGTERM/SIGINT 指向
    ``Server.handle_exit``；这里先标记 counter/RunManager draining，再交给
    父类设置 ``should_exit``。因此首个 SIGTERM 在任何 lifespan shutdown
    或 RunManager.aclose 之前生效。重复信号进入强制停止，跳过剩余宽限期。
    """

    def __init__(self, config: uvicorn.Config, *,
                 counter: RequestCounter, run_manager: Any,
                 shutdown_grace: float | None = None) -> None:
        super().__init__(config)
        self._lifecycle_counter = counter
        self._run_manager = run_manager
        self.shutdown_grace = (
            float(shutdown_grace) if shutdown_grace is not None else
            float(getattr(run_manager, "_shutdown_grace", 0.0)))
        self._signal_count = 0
        self._force_requested = False

    def handle_exit(self, sig, frame) -> None:
        self._signal_count += 1
        self._lifecycle_counter.start_drain()
        self._run_manager.begin_drain()
        if self._signal_count > 1:
            self._force_requested = True
            self._run_manager.request_force_stop()
        super().handle_exit(sig, frame)

    async def shutdown(self, sockets=None) -> None:
        """在关闭监听 socket 前完成 drain 宽限。

        Uvicorn 默认先关闭监听再执行 lifespan；那样宽限期内 health/新请求
        只会看到 connection refused，无法返回明确 503。先让 RunManager 在
        仍可服务的窗口内等待/取消，再交给父类关闭连接和 lifespan。
        """
        self._lifecycle_counter.start_drain()
        self._run_manager.begin_drain()
        await self._run_manager.aclose()
        await super().shutdown(sockets)


def _load_combined_app():
    """按当前环境导入 web.app；缺身份/公钥/RunManager 时失败关闭。"""
    import web.app as web_app

    identity = getattr(web_app, "_WORKER_IDENTITY", None)
    run_manager = getattr(web_app, "_RUNS", None)
    if identity is None or run_manager is None:
        raise RuntimeError(
            "worker server 需要固定身份、只读公钥和常驻 RunManager")
    return web_app


def _healthcheck(port: int) -> int:
    """进程内自检：校验身份、配置、容器名、就绪和执行能力。"""
    config = load_lifecycle_config()
    request = UrlRequest(
        f"http://127.0.0.1:{port}{LIFECYCLE_PATH}", headers={})
    request.add_header(WORKSPACE_HEADER, config.workspace_id)
    request.add_header(SERVICE_TOKEN_HEADER, config.service_token)
    try:
        with urlopen(request, timeout=HEALTHCHECK_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - 固定回环探针失败即未就绪
        return 1
    if not isinstance(payload, dict):
        return 1
    expected = {
        "workspace_id": config.workspace_id,
        "config_version": config.config_version,
        "container_id": config.container_id,
        "ready": True,
        "execution_capable": True,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        return 1
    # 脱敏最小输出：仅身份/就绪/执行能力，不输出 token。
    print(json.dumps({
        "workspace_id": payload["workspace_id"],
        "config_version": payload["config_version"],
        "container_id": payload["container_id"],
        "ready": payload["ready"],
        "execution_capable": payload["execution_capable"],
    }, separators=(",", ":"), sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="easel-worker-server")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="启动合并的 worker 服务")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=7860)
    check = subparsers.add_parser("healthcheck", help="受控容器内自检")
    check.add_argument("--port", type=int, default=7860)
    args = parser.parse_args(argv)

    if args.command == "healthcheck":
        return _healthcheck(args.port)

    web_app = _load_combined_app()
    config = load_lifecycle_config()
    counter = RequestCounter()
    run_manager = web_app._RUNS
    _attach_lifecycle(web_app.app, config, counter, run_manager)
    uvicorn_config = uvicorn.Config(
        web_app.app, host=args.host, port=args.port,
        proxy_headers=False, timeout_graceful_shutdown=5.0)
    WorkerServer(
        uvicorn_config, counter=counter, run_manager=run_manager,
        shutdown_grace=getattr(run_manager, "_shutdown_grace", 0.0)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
