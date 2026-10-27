"""Task G1 真实 TCP：gateway → 独立临时 worker（真实 worker guard）。

worker 只返回协议夹具，不接真实 Harness；使用随机回环端口，测试后关闭两个
uvicorn 实例并检查线程退出。
"""
from __future__ import annotations

import asyncio
import secrets
import socket
import threading
import time
from contextlib import closing
from dataclasses import replace

import httpx
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from easel.control.gateway_auth import build_gateway_assertion_headers
from easel.control.types import DomainError
from easel.gateway.app import create_gateway_app
from easel.gateway.auth import (
    GatewayAudience,
    GatewayConfig,
)
from easel.worker.guard import verify_worker_request

WORKSPACE = "a" * 32
OTHER_WORKSPACE = "b" * 32
GATEWAY = "gateway-instance-1"
WORKER = "c" * 64
TOKEN = "service-token-secret"
RUN = "d" * 32
SESSION = "e" * 32
PRIVATE = Ed25519PrivateKey.generate()
AUDIENCE = GatewayAudience(WORKSPACE, GATEWAY, "b" * 32, WORKER, 7)


class WorkerState:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.stream_closed = threading.Event()
        self._lock = threading.Lock()

    def record(self, request: Request, body: bytes) -> None:
        with self._lock:
            self.requests.append({
                "method": request.method,
                "path": request.url.path,
                "query": request.url.query,
                "headers": {key.lower(): value
                            for key, value in request.headers.items()},
                "body": body,
            })

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.requests)


def _deny(request: Request) -> JSONResponse | None:
    try:
        verify_worker_request(request.headers, WORKSPACE, TOKEN)
    except DomainError as error:
        return JSONResponse({"error": error.code}, status_code=error.status)
    return None


def create_worker_app(state: WorkerState) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/internal/lifecycle/health")
    async def health(request: Request):
        denied = _deny(request)
        if denied is not None:
            return denied
        state.record(request, b"")
        return {
            "workspace_id": WORKSPACE,
            "config_version": 7,
            "container_id": "d" * 64,
            "ready": True,
            "execution_capable": True,
        }

    @app.post("/internal/runs/{run_id}")
    async def submit(run_id: str, request: Request):
        denied = _deny(request)
        if denied is not None:
            return denied
        body = await request.body()
        state.record(request, body)
        return JSONResponse({"state": "accepted", "runId": run_id},
                            status_code=202)

    @app.get("/internal/runs/{run_id}/events")
    async def events(run_id: str, request: Request,
                     after: int = 0, nonce: str | None = None):
        denied = _deny(request)
        if denied is not None:
            return denied
        state.record(request, b"")
        if nonce == "stream":
            async def body():
                try:
                    yield b"data: one\n\n"
                    await asyncio.sleep(0.03)
                    yield b"data: two\n\n"
                finally:
                    state.stream_closed.set()
            return StreamingResponse(
                body(), media_type="text/event-stream",
                headers={"Set-Cookie": "worker=1"})
        return {
            "runId": run_id,
            "state": "running",
            "events": [{"eventId": 1, "event": "token", "data": "x"}],
            "lastEventId": 1,
        }

    @app.post("/internal/runs/{run_id}/stop")
    async def stop(run_id: str, request: Request):
        denied = _deny(request)
        if denied is not None:
            return denied
        body = await request.body()
        state.record(request, body)
        return {"runId": run_id, "state": "cancelled"}

    @app.post("/internal/runs/{run_id}/questions/{question_id}/answer")
    async def answer(run_id: str, question_id: str, request: Request):
        denied = _deny(request)
        if denied is not None:
            return denied
        state.record(request, await request.body())
        return {"ok": True, "runId": run_id, "questionId": question_id}

    @app.post("/internal/runs/{run_id}/questions/status")
    async def status(run_id: str, request: Request):
        denied = _deny(request)
        if denied is not None:
            return denied
        state.record(request, await request.body())
        return {"ok": True, "runId": run_id, "questions": {}}

    @app.get("/internal/sessions/{session_id}/messages")
    async def messages(session_id: str, request: Request,
                       limit: int = 100, offset: int = 0):
        denied = _deny(request)
        if denied is not None:
            return denied
        state.record(request, b"")
        return {"sessionId": session_id,
                "messages": [{"role": "user", "content": "fixture"}],
                "limit": limit, "offset": offset}

    return app


def free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.02)
    raise AssertionError(f"临时 uvicorn 端口未就绪：{port}")


def start_server(app, port: int):
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
        timeout_graceful_shutdown=1.0))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        wait_port(port)
    except Exception:
        server.should_exit = True
        thread.join(timeout=5)
        raise
    return server, thread


def stop_server(server, thread) -> None:
    if server is None:
        return
    server.should_exit = True
    thread.join(timeout=5)
    assert not thread.is_alive(), "临时 uvicorn 未退出"


def headers_for(method: str, path: str, *, query=None, body=b"",
                audience: GatewayAudience = AUDIENCE,
                private_key: Ed25519PrivateKey = PRIVATE) -> dict[str, str]:
    now = int(time.time())
    return build_gateway_assertion_headers(
        private_key, audience, method=method, path=path, query=query,
        body=body, issued_at=now, expires_at=now + 30,
        nonce="nonce-" + secrets.token_hex(8),
        request_id="request-" + secrets.token_hex(8))


def test_real_tcp_gateway_forwards_to_guarded_worker_and_rejects_outer_scope():
    state = WorkerState()
    worker_port = free_port()
    gateway_port = free_port()
    worker_server, worker_thread = start_server(
        create_worker_app(state), worker_port)
    gateway_server = None
    gateway_thread = None
    try:
        gateway_config = GatewayConfig(
            workspace_id=WORKSPACE,
            gateway_instance_id=GATEWAY,
            boot_generation=AUDIENCE.boot_generation,
            worker_container_id=WORKER,
            config_version=7,
            worker_origin=f"http://127.0.0.1:{worker_port}",
            worker_service_token=TOKEN,
        )
        gateway_app = create_gateway_app(
            gateway_config, PRIVATE.public_key(), activated=True)
        gateway_server, gateway_thread = start_server(gateway_app, gateway_port)
        base = f"http://127.0.0.1:{gateway_port}"

        with httpx.Client(trust_env=False, timeout=5,
                          follow_redirects=False) as client:
            health = "/internal/lifecycle/health"
            response = client.get(
                base + health, headers=headers_for("GET", health))
            assert response.status_code == 200, response.text
            assert response.json()["workspace_id"] == WORKSPACE
            assert len(state.snapshot()) == 1
            forwarded = state.snapshot()[0]
            assert forwarded["path"] == health
            assert forwarded["headers"]["x-easel-workspace-id"] == WORKSPACE
            assert forwarded["headers"]["x-easel-service-token"] == TOKEN
            assert forwarded["headers"]["host"] == \
                f"127.0.0.1:{worker_port}"
            for stripped in ("authorization", "cookie",
                             "x-easel-gateway-assertion"):
                assert stripped not in forwarded["headers"]

            # 真实 worker guard：直连缺少内部凭证必须 401
            with httpx.Client(trust_env=False, timeout=5) as direct:
                assert direct.get(
                    f"http://127.0.0.1:{worker_port}" + health
                ).status_code == 401

            before = len(state.snapshot())
            wrong_workspace = headers_for(
                "GET", health,
                audience=replace(AUDIENCE, workspace_id=OTHER_WORKSPACE))
            assert client.get(
                base + health, headers=wrong_workspace).status_code == 403
            wrong_key = headers_for(
                "GET", health, private_key=Ed25519PrivateKey.generate())
            assert client.get(
                base + health, headers=wrong_key).status_code == 401
            assert len(state.snapshot()) == before

            stop_path = f"/internal/runs/{RUN}/stop"
            stop_body = b'{"runId":"' + RUN.encode() + b'"}'
            stop = client.post(
                base + stop_path,
                headers=headers_for("POST", stop_path, body=stop_body),
                content=stop_body)
            assert stop.status_code == 200, stop.text
            assert stop.json()["state"] == "cancelled"
            assert state.snapshot()[-1]["path"] == stop_path
            assert state.snapshot()[-1]["body"] == stop_body

            messages_path = f"/internal/sessions/{SESSION}/messages"
            messages = client.get(
                base + messages_path + "?limit=10&offset=0",
                headers=headers_for(
                    "GET", messages_path,
                    query={"limit": "10", "offset": "0"}))
            assert messages.status_code == 200, messages.text
            assert messages.json()["messages"][0]["content"] == "fixture"
            assert state.snapshot()[-1]["query"] == "limit=10&offset=0"

            events_path = f"/internal/runs/{RUN}/events"
            json_events = client.get(
                base + events_path + "?after=0",
                headers=headers_for(
                    "GET", events_path, query={"after": "0"}))
            assert json_events.status_code == 200, json_events.text
            assert json_events.json()["events"][0]["event"] == "token"
            assert json_events.headers["content-type"].startswith(
                "application/json")

            with client.stream(
                    "GET", base + events_path + "?after=0&nonce=stream",
                    headers=headers_for(
                        "GET", events_path,
                        query={"after": "0", "nonce": "stream"})) as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith(
                    "text/event-stream")
                assert "set-cookie" not in response.headers
                lines = [line for line in response.iter_lines() if line]
                assert lines == ["data: one", "data: two"]
            assert state.stream_closed.wait(2), "真实 TCP SSE 未正常关闭"
    finally:
        stop_server(gateway_server, gateway_thread)
        stop_server(worker_server, worker_thread)
