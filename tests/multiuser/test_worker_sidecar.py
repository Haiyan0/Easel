"""Task 8：每空间 sidecar 的 broker 精确转发与出口代理复用。"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "multiuser"
for entry in (str(ROOT), str(DEPLOY)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

spec = importlib.util.spec_from_file_location(
    "easel_worker_sidecar", DEPLOY / "worker_sidecar.py")
sidecar = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sidecar
spec.loader.exec_module(sidecar)

from easel.control.egress import EgressPolicy  # noqa: E402


class FakeUpstream:
    def __init__(self):
        self.requests: list[dict] = []
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self):
        self.server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            line = await reader.readline()
            headers = {}
            while True:
                raw = await reader.readline()
                if raw in (b"\r\n", b"\n", b""):
                    break
                name, value = raw.decode("latin-1").split(":", 1)
                headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            body = await reader.readexactly(length) if length else b""
            self.requests.append({
                "line": line.decode("latin-1").strip(),
                "headers": headers,
                "body": body,
            })
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                b"Connection: close\r\n\r\n{}")
            await writer.drain()
        finally:
            writer.close()


async def _broker_roundtrip(port: int, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    await writer.wait_closed()
    return data


def _request(method: str, target: str, body: bytes = b"",
             extra: str = "") -> bytes:
    lines = [f"{method} {target} HTTP/1.1", "Host: sidecar"]
    if extra:
        lines.append(extra)
    if body:
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def test_allowed_broker_route_forwards_exact_path_body_and_token():
    async def scenario():
        upstream = FakeUpstream()
        await upstream.start()
        server = await asyncio.start_server(
            lambda reader, writer: sidecar._handle_broker(
                reader, writer, ("127.0.0.1", upstream.port)),
            "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            body = b'{"model":"m","messages":[]}'
            response = await _broker_roundtrip(port, _request(
                "POST", "/internal/broker/chat/chat/completions", body,
                extra=("x-easel-broker-token: token-sentinel\r\n"
                       "x-easel-workspace-id: " + "a" * 32 + "\r\n"
                       "x-easel-run-id: run-1")))
            assert b"200 OK" in response.split(b"\r\n", 1)[0]
            assert b"{}" in response
            assert len(upstream.requests) == 1
            received = upstream.requests[0]
            assert received["line"] == (
                "POST /internal/broker/chat/chat/completions HTTP/1.1")
            assert received["headers"]["x-easel-broker-token"] == (
                "token-sentinel")
            assert received["body"] == body
        finally:
            server.close()
            await server.wait_closed()
            await upstream.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("method,target", [
    ("GET", "/internal/broker/chat/chat/completions"),
    ("POST", "/internal/broker/chat/models"),
    ("POST", "/internal/broker/chat/admin"),
    ("POST", "/internal/broker/chat/chat/completions?x=1"),
    ("POST", "/internal/broker/chat/chat%2fcompletions"),
    ("POST", "/internal/broker/chat/../chat/completions"),
])
def test_broker_relay_rejects_non_allowlisted_routes(method, target):
    async def scenario():
        upstream = FakeUpstream()
        await upstream.start()
        server = await asyncio.start_server(
            lambda reader, writer: sidecar._handle_broker(
                reader, writer, ("127.0.0.1", upstream.port)),
            "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            response = await _broker_roundtrip(
                port, _request(method, target, b"{}"))
            assert b"404" in response.split(b"\r\n", 1)[0]
            assert upstream.requests == []
        finally:
            server.close()
            await server.wait_closed()
            await upstream.stop()
    asyncio.run(scenario())


def test_broker_relay_rejects_chunked_body_before_upstream():
    async def scenario():
        upstream = FakeUpstream()
        await upstream.start()
        server = await asyncio.start_server(
            lambda reader, writer: sidecar._handle_broker(
                reader, writer, ("127.0.0.1", upstream.port)),
            "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            request = (
                b"POST /internal/broker/chat/chat/completions HTTP/1.1\r\n"
                b"Host: sidecar\r\nTransfer-Encoding: chunked\r\n\r\n"
                b"0\r\n\r\n")
            response = await _broker_roundtrip(port, request)
            assert b"411" in response.split(b"\r\n", 1)[0]
            assert upstream.requests == []
        finally:
            server.close()
            await server.wait_closed()
            await upstream.stop()
    asyncio.run(scenario())


def test_broker_health_endpoint_is_available():
    async def scenario():
        upstream = FakeUpstream()
        await upstream.start()
        server = await asyncio.start_server(
            lambda reader, writer: sidecar._handle_broker(
                reader, writer, ("127.0.0.1", upstream.port)),
            "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            response = await _broker_roundtrip(
                port, b"GET /healthz HTTP/1.1\r\nHost: sidecar\r\n\r\n")
            assert b"200 OK" in response.split(b"\r\n", 1)[0]
        finally:
            server.close()
            await server.wait_closed()
            await upstream.stop()
    asyncio.run(scenario())


def test_sidecar_egress_uses_task5_private_target_denial():
    async def scenario():
        policy = EgressPolicy()
        server = await asyncio.start_server(
            lambda reader, writer: sidecar.egress_proxy._handle(
                reader, writer, policy),
            "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            response = await _broker_roundtrip(
                port, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")
            assert b"403" in response.split(b"\r\n", 1)[0]
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())
