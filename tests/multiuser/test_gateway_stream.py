"""Task G1 gateway 流式背压、取消、idle 超时与请求体异常。"""
from __future__ import annotations

import asyncio
import secrets

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect, Request

from easel.gateway.app import GatewayLimits, create_gateway_app
from easel.gateway.auth import (
    GATEWAY_ASSERTION_HEADER,
    GatewayAudience,
    GatewayConfig,
    sign_gateway_assertion,
)

NOW = 1_750_000_000
WORKSPACE = "a" * 32
GATEWAY = "gateway-instance-1"
BOOT = "b" * 32
WORKER = "c" * 64
TOKEN = "service-token-secret"
ORIGIN = "http://worker.internal:7860"
RUN = "d" * 32
PRIVATE = Ed25519PrivateKey.generate()


def _audience() -> GatewayAudience:
    return GatewayAudience(WORKSPACE, GATEWAY, BOOT, WORKER, 7)


def _config() -> GatewayConfig:
    return GatewayConfig(
        workspace_id=WORKSPACE,
        gateway_instance_id=GATEWAY,
        boot_generation=BOOT,
        worker_container_id=WORKER,
        config_version=7,
        worker_origin=ORIGIN,
        worker_service_token=TOKEN,
    )


def headers_for(method: str, path: str, *, query=None, body=b"",
                nonce: str | None = None,
                audience: GatewayAudience | None = None,
                private_key: Ed25519PrivateKey = PRIVATE,
                issued_at: int = NOW,
                expires_at: int = NOW + 30) -> dict[str, str]:
    token = sign_gateway_assertion(
        private_key, audience or _audience(), method=method, path=path,
        query=query, body=body, issued_at=issued_at, expires_at=expires_at,
        nonce=nonce or ("nonce-" + secrets.token_hex(8)),
        request_id="request-" + secrets.token_hex(8))
    return {GATEWAY_ASSERTION_HEADER: token}


def make_app(handler, *, limits: GatewayLimits | None = None,
             http_client: httpx.AsyncClient | None = None):
    kwargs = {}
    if http_client is not None:
        kwargs["http_client"] = http_client
    else:
        kwargs["transport"] = httpx.MockTransport(handler)
    return create_gateway_app(
        _config(), PRIVATE.public_key(), clock=lambda: NOW,
        activated=True, limits=limits, **kwargs)


class _BodyStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes):
        self._body = body
        self.closed = False

    async def __aiter__(self):
        yield self._body

    async def aclose(self) -> None:
        self.closed = True


def _response(status: int = 200, body: bytes = b"", headers=None):
    return httpx.Response(status, headers=headers, stream=_BodyStream(body))


class GatedStream(httpx.AsyncByteStream):
    """可控上游字节流；第二块必须放行 gate 才可读，记录关闭状态。"""

    def __init__(self, chunks: list[bytes], gate: asyncio.Event):
        self.chunks = chunks
        self.gate = gate
        self.yielded = 0
        self.closed = asyncio.Event()

    async def __aiter__(self):
        for index, chunk in enumerate(self.chunks):
            if index == 1:
                await self.gate.wait()
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed.set()


class FailingStream(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = asyncio.Event()

    async def __aiter__(self):
        yield b"data: one\n\n"
        raise httpx.ReadError("fixture read failure")

    async def aclose(self) -> None:
        self.closed.set()


class SlowCloseStream(httpx.AsyncByteStream):
    """自然 EOF 后 aclose 先通知再等待 gate；用于关闭所有权反例。"""

    def __init__(self, chunks: list[bytes], close_gate: asyncio.Event):
        self.chunks = chunks
        self.close_gate = close_gate
        self.close_started = asyncio.Event()
        self.closed = asyncio.Event()
        self.aclose_calls = 0
        self.yielded = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.aclose_calls += 1
        self.close_started.set()
        await self.close_gate.wait()
        self.closed.set()


def stream_handler(streams: list[GatedStream], gate: asyncio.Event):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stop"):
            return _response(
                200, b'{"state":"cancelled"}',
                headers={"Content-Type": "application/json"})
        stream = GatedStream([b"data: one\n\n", b"data: two\n\n"], gate)
        streams.append(stream)
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream",
                          "Set-Cookie": "evil=1"}, stream=stream)

    return handler


def _request_scope(path: str, headers: list[tuple[bytes, bytes]],
                   *, method: str = "POST", query: bytes = b"") -> dict:
    return {
        "type": "http",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query,
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("gateway", 8897),
        "http_version": "1.1",
    }


async def _handle(controller, path: str, headers: dict[str, str], *,
                  method: str = "POST", query: bytes = b"",
                  body: bytes = b""):
    scope = _request_scope(
        path, [(key.lower().encode(), value.encode())
               for key, value in headers.items()],
        method=method, query=query)
    chunks = [body]

    async def receive():
        if chunks:
            return {"type": "http.request", "body": chunks.pop(0),
                    "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return await controller.handle(Request(scope, receive))


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("等待 gateway 清理超时")
        await asyncio.sleep(0.01)


def _leftover_tasks() -> list[asyncio.Task]:
    current = asyncio.current_task()
    return [task for task in asyncio.all_tasks()
            if task is not current and not task.done()]


def test_streaming_backpressure_reserved_slot_and_cancel_cleanup():
    async def scenario():
        gate = asyncio.Event()
        streams: list[GatedStream] = []
        app = make_app(
            stream_handler(streams, gate),
            limits=GatewayLimits(
                max_concurrent_requests=2, max_concurrent_streams=1,
                connect_timeout=1.0, io_timeout=5.0))
        controller = app.state.gateway
        path = f"/internal/runs/{RUN}/events"
        headers = headers_for(
            "GET", path, query={"after": "0"}, nonce="stream-one")
        response = await _handle(
            controller, path, headers, method="GET", query=b"after=0")
        assert isinstance(response, StreamingResponse)
        iterator = response.body_iterator
        assert await iterator.__anext__() == b"data: one\n\n"
        assert streams[0].yielded == 1, "gateway 不得预读完整流"
        assert controller.active_requests == 1
        assert controller.active_streams == 1

        second_headers = headers_for(
            "GET", path, query={"after": "0"}, nonce="stream-two")
        second = await _handle(
            controller, path, second_headers, method="GET",
            query=b"after=0")
        assert second.status_code == 429
        assert second.headers["retry-after"] == "1"

        stop_path = f"/internal/runs/{RUN}/stop"
        stop_headers = headers_for("POST", stop_path, body=b"{}")
        stop = await _handle(
            controller, stop_path, stop_headers, body=b"{}")
        assert stop.status_code == 200
        assert b'"state":"cancelled"' in stop.body

        await iterator.aclose()
        assert streams[0].closed.is_set()
        assert controller.active_requests == 0
        assert controller.active_streams == 0
        leftovers = [task for task in asyncio.all_tasks()
                     if task is not asyncio.current_task() and not task.done()]
        assert leftovers == []
        await controller.aclose()

    asyncio.run(scenario())


def test_streaming_idle_timeout_closes_upstream_and_releases_slot():
    async def scenario():
        gate = asyncio.Event()
        streams: list[GatedStream] = []
        app = make_app(
            stream_handler(streams, gate),
            limits=GatewayLimits(
                max_concurrent_requests=2, max_concurrent_streams=1,
                connect_timeout=1.0, io_timeout=0.05))
        controller = app.state.gateway
        path = f"/internal/runs/{RUN}/events"
        response = await _handle(
            controller, path,
            headers_for("GET", path, query={"after": "0"},
                        nonce="stream-timeout"),
            method="GET", query=b"after=0")
        assert isinstance(response, StreamingResponse)
        iterator = response.body_iterator
        assert await iterator.__anext__() == b"data: one\n\n"
        with pytest.raises(StopAsyncIteration):
            await iterator.__anext__()
        assert streams[0].closed.is_set()
        assert controller.active_requests == 0
        assert controller.active_streams == 0
        assert _leftover_tasks() == []
        await controller.aclose()

    asyncio.run(scenario())


def test_streaming_read_exception_closes_upstream_and_releases_slot():
    async def scenario():
        stream = FailingStream()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"},
                stream=stream)

        app = make_app(
            handler, limits=GatewayLimits(
                max_concurrent_requests=2, max_concurrent_streams=1,
                connect_timeout=1.0, io_timeout=5.0))
        controller = app.state.gateway
        path = f"/internal/runs/{RUN}/events"
        response = await _handle(
            controller, path,
            headers_for("GET", path, query={"after": "0"},
                        nonce="stream-read-error"),
            method="GET", query=b"after=0")
        iterator = response.body_iterator
        assert await iterator.__anext__() == b"data: one\n\n"
        with pytest.raises(StopAsyncIteration):
            await iterator.__anext__()
        await _wait_until(stream.closed.is_set)
        await _wait_until(lambda: controller.active_requests == 0)
        assert _leftover_tasks() == []
        await controller.aclose()

    asyncio.run(scenario())


def test_injected_client_defaults_are_cleaned_at_final_request_boundary():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _response(200, b'{"ok":true}',
                         headers={"Content-Type": "application/json"})

    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={
            "Authorization": "Bearer client-default",
            "X-Easel-Role": "admin",
            "X-Easel-Service-Token": "client-default-token",
            "X-Easel-Upstream": "http://evil.invalid",
            "Forwarded": "for=203.0.113.9",
            "Host": "evil.invalid",
            "Connection": "X-Client-Hop",
            "X-Client-Hop": "drop",
            "X-Custom": "keep",
        },
        cookies={"client-cookie": "evil"},
        auth=("user", "pass"),
    )
    app = make_app(None, http_client=upstream)
    path = "/internal/lifecycle/health"

    async def scenario():
        try:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://gateway") as client:
                return await client.get(path, headers=headers_for("GET", path))
        finally:
            await app.state.gateway.aclose()
            await upstream.aclose()

    response = asyncio.run(scenario())
    assert response.status_code == 200
    lowered = {key.lower(): value for key, value in captured[0].headers.items()}
    for stripped in ("authorization", "cookie", "forwarded", "x-easel-role",
                     "x-easel-upstream", "x-easel-gateway-assertion",
                     "x-client-hop"):
        assert stripped not in lowered, stripped
    assert lowered["x-custom"] == "keep"
    assert lowered["x-easel-workspace-id"] == WORKSPACE
    assert lowered["x-easel-service-token"] == TOKEN
    assert lowered["host"] == "worker.internal:7860"


def capture_handler():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response(200, b"{}")

    return handler, requests


def test_body_limit_accumulates_without_content_length_and_releases_slot():
    async def scenario():
        handler, requests = capture_handler()
        app = make_app(handler, limits=GatewayLimits(
            max_body_bytes=4, max_header_bytes=65536,
            connect_timeout=1.0, io_timeout=1.0,
            max_concurrent_requests=8, max_concurrent_streams=7))
        chunks = [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"456", "more_body": False},
        ]

        async def receive():
            return chunks.pop(0)

        path = f"/internal/runs/{RUN}"
        headers = headers_for("POST", path, body=b"123456")
        scope = _request_scope(path, [
            (key.lower().encode(), value.encode())
            for key, value in headers.items()])
        response = await app.state.gateway.handle(Request(scope, receive))
        assert response.status_code == 413
        assert requests == []
        assert app.state.gateway.active_requests == 0
        await app.state.gateway.aclose()

    asyncio.run(scenario())


def test_gateway_limits_match_the_approved_defaults():
    limits = GatewayLimits()
    assert limits.max_header_bytes == 65536
    assert limits.max_body_bytes == 67108864
    assert limits.connect_timeout == 10.0
    assert limits.io_timeout == 120.0
    assert limits.max_concurrent_requests == 8
    assert limits.max_concurrent_streams == 7


def test_declared_body_and_header_limits_reject_before_upstream():
    async def scenario():
        handler, requests = capture_handler()
        app = make_app(handler, limits=GatewayLimits(
            max_header_bytes=32, max_body_bytes=4,
            connect_timeout=1.0, io_timeout=1.0,
            max_concurrent_requests=8, max_concurrent_streams=7))
        path = f"/internal/runs/{RUN}"

        scope = _request_scope(path, [(b"content-length", b"5")])
        response = await app.state.gateway.handle(Request(scope, None))
        assert response.status_code == 413

        scope = _request_scope(path, [(b"x-large", b"a" * 64)])
        response = await app.state.gateway.handle(Request(scope, None))
        assert response.status_code == 431
        assert requests == []
        assert app.state.gateway.active_requests == 0
        await app.state.gateway.aclose()

    asyncio.run(scenario())


def test_body_disconnect_and_framing_ambiguity_are_rejected_and_released():
    async def scenario():
        handler, requests = capture_handler()
        app = make_app(handler)
        path = f"/internal/runs/{RUN}"

        async def disconnect_receive():
            raise ClientDisconnect()

        headers = headers_for("POST", path, body=b"")
        scope = _request_scope(path, [
            (key.lower().encode(), value.encode())
            for key, value in headers.items()])
        response = await app.state.gateway.handle(
            Request(scope, disconnect_receive))
        assert response.status_code == 400
        assert app.state.gateway.active_requests == 0

        for headers in (
                [(b"content-length", b"1"), (b"content-length", b"2")],
                [(b"transfer-encoding", b"chunked")],
                [(b"connection", b"content-length"),
                 (b"content-length", b"0")]):
            scope = _request_scope(path, headers)
            response = await app.state.gateway.handle(
                Request(scope, disconnect_receive))
            assert response.status_code == 400, headers
            assert app.state.gateway.active_requests == 0

        assert requests == []
        await app.state.gateway.aclose()

    asyncio.run(scenario())


async def _drain_stream(response):
    if isinstance(response, StreamingResponse):
        async for _ in response.body_iterator:
            pass
        await response.background()


def _slow_close_handler(stream: SlowCloseStream, content_type: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Type": content_type}, stream=stream)

    return handler


def test_streaming_natural_eof_holds_slot_until_unique_close_finishes():
    async def scenario():
        gate = asyncio.Event()
        stream = SlowCloseStream([b"data: one\n\n"], gate)
        app = make_app(
            _slow_close_handler(stream, "text/event-stream"),
            limits=GatewayLimits(
                max_concurrent_requests=2, max_concurrent_streams=1,
                connect_timeout=1.0, io_timeout=5.0))
        controller = app.state.gateway
        path = f"/internal/runs/{RUN}/events"
        response = await _handle(
            controller, path,
            headers_for("GET", path, query={"after": "0"},
                        nonce="slow-eof"),
            method="GET", query=b"after=0")
        assert isinstance(response, StreamingResponse)
        iterator = response.body_iterator
        assert await iterator.__anext__() == b"data: one\n\n"
        eof = asyncio.create_task(iterator.__anext__())
        try:
            await asyncio.wait_for(stream.close_started.wait(), 1)
            assert not stream.closed.is_set()
            assert controller.active_requests == 1
            gate.set()
            with pytest.raises(StopAsyncIteration):
                await eof
            await _wait_until(stream.closed.is_set)
            await _wait_until(lambda: controller.active_requests == 0)
            await response.background()
            assert stream.aclose_calls == 1
            assert _leftover_tasks() == []
        finally:
            gate.set()
            if not eof.done():
                eof.cancel()
            await controller.aclose()

    asyncio.run(scenario())


def test_streaming_eof_cancel_does_not_truncate_unique_close_or_slot():
    async def scenario():
        gate = asyncio.Event()
        stream = SlowCloseStream([b"data: one\n\n"], gate)
        app = make_app(
            _slow_close_handler(stream, "text/event-stream"),
            limits=GatewayLimits(
                max_concurrent_requests=2, max_concurrent_streams=1,
                connect_timeout=1.0, io_timeout=5.0))
        controller = app.state.gateway
        path = f"/internal/runs/{RUN}/events"
        response = await _handle(
            controller, path,
            headers_for("GET", path, query={"after": "0"},
                        nonce="slow-eof-cancel"),
            method="GET", query=b"after=0")
        iterator = response.body_iterator
        assert await iterator.__anext__() == b"data: one\n\n"
        eof = asyncio.create_task(iterator.__anext__())
        background = None
        try:
            await asyncio.wait_for(stream.close_started.wait(), 1)
            eof.cancel()
            with pytest.raises(asyncio.CancelledError):
                await eof
            background = asyncio.create_task(response.background())
            await asyncio.sleep(0)
            # EOF close 的第一等待者取消后，唯一 cleanup 仍须占用槽位
            assert controller.active_requests == 1
            assert not stream.closed.is_set()
            gate.set()
            await asyncio.wait_for(background, 2)
            await _wait_until(stream.closed.is_set)
            await _wait_until(lambda: controller.active_requests == 0)
            assert stream.aclose_calls == 1
            assert _leftover_tasks() == []
        finally:
            gate.set()
            if background is not None and not background.done():
                await background
            await controller.aclose()

    asyncio.run(scenario())


def test_streaming_background_close_before_iteration_is_unique_and_complete():
    async def scenario():
        gate = asyncio.Event()
        stream = SlowCloseStream([b"data: one\n\n"], gate)
        app = make_app(
            _slow_close_handler(stream, "text/event-stream"),
            limits=GatewayLimits(
                max_concurrent_requests=2, max_concurrent_streams=1,
                connect_timeout=1.0, io_timeout=5.0))
        controller = app.state.gateway
        path = f"/internal/runs/{RUN}/events"
        response = await _handle(
            controller, path,
            headers_for("GET", path, query={"after": "0"},
                        nonce="slow-background"),
            method="GET", query=b"after=0")
        background = asyncio.create_task(response.background())
        try:
            await asyncio.wait_for(stream.close_started.wait(), 1)
            assert not stream.closed.is_set()
            assert controller.active_requests == 1
            gate.set()
            await asyncio.wait_for(background, 2)
            await _wait_until(stream.closed.is_set)
            await _wait_until(lambda: controller.active_requests == 0)
            assert stream.aclose_calls == 1
        finally:
            gate.set()
            if not background.done():
                await background
            await controller.aclose()

    asyncio.run(scenario())


def test_buffered_natural_eof_holds_slot_until_close_finishes():
    async def scenario():
        gate = asyncio.Event()
        stream = SlowCloseStream([b'{"ok":true}'], gate)
        app = make_app(
            _slow_close_handler(stream, "application/json"),
            limits=GatewayLimits(
                max_concurrent_requests=2, max_concurrent_streams=1,
                connect_timeout=1.0, io_timeout=5.0))
        controller = app.state.gateway
        path = "/internal/lifecycle/health"
        request = Request(
            _request_scope(
                path,
                [(GATEWAY_ASSERTION_HEADER.encode(),
                  headers_for("GET", path)[GATEWAY_ASSERTION_HEADER].encode())],
                method="GET"),
            _empty_receive)
        task = asyncio.create_task(controller.handle(request))
        try:
            await asyncio.wait_for(stream.close_started.wait(), 1)
            assert controller.active_requests == 1
            assert not task.done()
            gate.set()
            response = await asyncio.wait_for(task, 2)
            assert response.status_code == 200
            await _wait_until(stream.closed.is_set)
            await _wait_until(lambda: controller.active_requests == 0)
            assert stream.aclose_calls == 1
        finally:
            gate.set()
            if not task.done():
                task.cancel()
            await controller.aclose()

    asyncio.run(scenario())


def test_buffered_cancel_holds_slot_until_unique_close_finishes():
    async def scenario():
        gate = asyncio.Event()
        stream = SlowCloseStream([b'{"ok":true}'], gate)
        app = make_app(
            _slow_close_handler(stream, "application/json"),
            limits=GatewayLimits(
                max_concurrent_requests=2, max_concurrent_streams=1,
                connect_timeout=1.0, io_timeout=5.0))
        controller = app.state.gateway
        path = "/internal/lifecycle/health"
        request = Request(
            _request_scope(
                path,
                [(GATEWAY_ASSERTION_HEADER.encode(),
                  headers_for("GET", path)[GATEWAY_ASSERTION_HEADER].encode())],
                method="GET"),
            _empty_receive)
        task = asyncio.create_task(controller.handle(request))
        try:
            await asyncio.wait_for(stream.close_started.wait(), 1)
            assert controller.active_requests == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            assert not stream.closed.is_set()
            assert controller.active_requests == 1
            gate.set()
            await _wait_until(stream.closed.is_set)
            await _wait_until(lambda: controller.active_requests == 0)
            assert stream.aclose_calls == 1
            assert _leftover_tasks() == []
        finally:
            gate.set()
            if not task.done():
                task.cancel()
            await controller.aclose()

    asyncio.run(scenario())


async def _empty_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def test_real_asgi_disconnect_before_iteration_closes_upstream():
    async def scenario():
        gate = asyncio.Event()
        streams: list[GatedStream] = []
        app = make_app(
            stream_handler(streams, gate),
            limits=GatewayLimits(
                max_concurrent_requests=2, max_concurrent_streams=1,
                connect_timeout=1.0, io_timeout=5.0))
        path = f"/internal/runs/{RUN}/events"
        scope = _request_scope(
            path,
            [(key.lower().encode(), value.encode())
             for key, value in headers_for(
                 "GET", path, query={"after": "0"},
                 nonce="asgi-before-iteration").items()],
            method="GET", query=b"after=0")
        request_sent = False

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"",
                        "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            return None

        try:
            await asyncio.wait_for(app(scope, receive, send), 2)
            await _wait_until(lambda: streams and streams[0].closed.is_set())
            await _wait_until(
                lambda: app.state.gateway.active_requests == 0)
            assert streams[0].closed.is_set()
            assert _leftover_tasks() == []
        finally:
            await app.state.gateway.aclose()

    asyncio.run(scenario())


def test_real_asgi_send_backpressure_and_disconnect_closes_upstream():
    async def scenario():
        gate = asyncio.Event()
        streams: list[GatedStream] = []
        app = make_app(
            stream_handler(streams, gate),
            limits=GatewayLimits(
                max_concurrent_requests=2, max_concurrent_streams=1,
                connect_timeout=1.0, io_timeout=5.0))
        path = f"/internal/runs/{RUN}/events"
        scope = _request_scope(
            path,
            [(key.lower().encode(), value.encode())
             for key, value in headers_for(
                 "GET", path, query={"after": "0"},
                 nonce="asgi-send").items()],
            method="GET", query=b"after=0")
        request_sent = False
        disconnect = asyncio.Event()
        first_sent = asyncio.Event()
        send_gate = asyncio.Event()

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"",
                        "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body:
                    first_sent.set()
                    await send_gate.wait()

        app_task = asyncio.create_task(app(scope, receive, send))
        try:
            await asyncio.wait_for(first_sent.wait(), 2)
            # send 被 ASGI backpressure 卡住时，上游不得预读第二块。
            assert streams[0].yielded == 1
            assert app.state.gateway.active_requests == 1
            disconnect.set()
            send_gate.set()
            await asyncio.wait_for(app_task, 2)
            await _wait_until(streams[0].closed.is_set)
            await _wait_until(
                lambda: app.state.gateway.active_requests == 0)
            assert _leftover_tasks() == []
        finally:
            disconnect.set()
            send_gate.set()
            if not app_task.done():
                app_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await app_task
            await app.state.gateway.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "default_params,method,path,query,query_bytes,expected",
    [
        ([("after", "99"), ("unknown", "1")], "GET",
         f"/internal/runs/{RUN}/events", {"after": "1"}, b"after=1",
         "?after=1"),
        ([("extra", "1")], "GET", "/internal/lifecycle/health", None, b"",
         ""),
        ([("limit", "1"), ("offset", "99"), ("z", "1")], "GET",
         f"/internal/sessions/{RUN}/messages",
         {"limit": "10", "offset": "0"}, b"limit=10&offset=0",
         "?limit=10&offset=0"),
        ([("nonce", "evil"), ("after", "99")], "GET",
         f"/internal/runs/{RUN}/events",
         {"after": "1", "nonce": "n-1"}, b"after=1&nonce=n-1",
         "?after=1&nonce=n-1"),
        ([("nonce", "evil"), ("after", "99")], "GET",
         f"/internal/runs/{RUN}/events",
         {"after": "1", "nonce": "a/b"}, b"after=1&nonce=a%2Fb",
         "?after=1&nonce=a%2Fb"),
    ],
)
def test_client_default_params_cannot_rewrite_signed_final_url(
        default_params, method, path, query, query_bytes, expected):
    async def scenario():
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _response(200, b"{}")

        upstream = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), params=default_params)
        before = list(upstream.params.multi_items())
        app = make_app(None, http_client=upstream)
        controller = app.state.gateway
        try:
            response = await _handle(
                controller, path, headers_for(method, path, query=query),
                method=method, query=query_bytes)
            await _drain_stream(response)
            assert response.status_code == 200
            assert str(captured[0].url) == ORIGIN + path + expected
            assert list(upstream.params.multi_items()) == before
            assert not upstream.is_closed

            second = await _handle(
                controller, path, headers_for(method, path, query=query),
                method=method, query=query_bytes)
            await _drain_stream(second)
            assert second.status_code == 200
            assert str(captured[1].url) == ORIGIN + path + expected
            assert not upstream.is_closed
        finally:
            await controller.aclose()
            await upstream.aclose()

    asyncio.run(scenario())


def test_unauthenticated_slow_body_rejected_before_read_or_slot():
    async def scenario():
        handler, requests = capture_handler()
        limits = GatewayLimits(
            max_concurrent_requests=2, max_concurrent_streams=1,
            connect_timeout=1.0, io_timeout=0.05)
        app = make_app(handler, limits=limits)
        controller = app.state.gateway
        path = f"/internal/runs/{RUN}"
        read_count = 0

        async def slow_receive():
            nonlocal read_count
            read_count += 1
            await asyncio.Event().wait()

        bad_headers = [
            {},
            {GATEWAY_ASSERTION_HEADER: "v1.bad"},
            headers_for(
                "POST", path, body=b"",
                audience=GatewayAudience(
                    "f" * 32, GATEWAY, BOOT, WORKER, 7),
                nonce="bad-audience"),
            headers_for(
                "POST", path, body=b"", issued_at=NOW - 60,
                expires_at=NOW - 30, nonce="expired"),
            headers_for(
                "POST", path, body=b"",
                private_key=Ed25519PrivateKey.generate(), nonce="bad-key"),
        ]
        try:
            for headers in bad_headers:
                scope = _request_scope(
                    path,
                    [(key.lower().encode(), value.encode())
                     for key, value in headers.items()],
                    method="POST")
                response = await controller.handle(Request(scope, slow_receive))
                assert response.status_code in (401, 403), response.body
                assert read_count == 0
                assert controller.active_requests == 0

            # 两个无断言慢 POST 不得占住两个槽位；合法 stop 仍可执行。
            scopes = [
                _request_scope(path, [], method="POST"),
                _request_scope(path, [], method="POST"),
            ]
            responses = await asyncio.gather(*[
                controller.handle(Request(scope, slow_receive))
                for scope in scopes
            ])
            assert [response.status_code for response in responses] == [401, 401]
            assert read_count == 0
            assert controller.active_requests == 0

            stop_path = f"/internal/runs/{RUN}/stop"
            stop = await _handle(
                controller, stop_path,
                headers_for("POST", stop_path, body=b"{}"), body=b"{}")
            assert stop.status_code == 200
            assert len(requests) == 1
            assert controller.active_requests == 0
        finally:
            await controller.aclose()

    asyncio.run(scenario())


def test_actual_body_digest_mismatch_does_not_reach_worker():
    async def scenario():
        handler, requests = capture_handler()
        app = make_app(handler)
        controller = app.state.gateway
        path = f"/internal/runs/{RUN}"
        try:
            response = await _handle(
                controller, path,
                headers_for("POST", path,
                            body=b'{"a":1}', nonce="body-bind"),
                body=b'{"a":2}')
            assert response.status_code == 403
            assert requests == []
            assert controller.active_requests == 0
        finally:
            await controller.aclose()

    asyncio.run(scenario())


def test_oversized_declared_content_length_is_bounded_4xx():
    async def scenario():
        handler, requests = capture_handler()
        app = make_app(handler)
        controller = app.state.gateway
        path = f"/internal/runs/{RUN}"
        try:
            scope = _request_scope(
                path, [(b"content-length", b"1" * 5000)])
            response = await controller.handle(Request(scope, None))
            assert response.status_code in (400, 413), response.body
            assert requests == []
            assert controller.active_requests == 0

            scope = _request_scope(
                path, [(b"content-length", b"67108865")])
            response = await controller.handle(Request(scope, None))
            assert response.status_code == 413
        finally:
            await controller.aclose()

    asyncio.run(scenario())


def test_absolute_uri_is_rejected_as_a_path():
    async def scenario():
        handler, requests = capture_handler()
        app = make_app(handler)
        scope = _request_scope(
            "http://evil.invalid/internal/lifecycle/health", [],
            method="GET")
        scope["raw_path"] = b"http://evil.invalid/internal/lifecycle/health"
        response = await app.state.gateway.handle(Request(scope, None))
        assert response.status_code == 400

        scope = _request_scope(
            "/internal/lifecycle/health", [], method="GET")
        scope.pop("raw_path")
        response = await app.state.gateway.handle(Request(scope, None))
        assert response.status_code == 400

        assert requests == []
        await app.state.gateway.aclose()

    asyncio.run(scenario())
