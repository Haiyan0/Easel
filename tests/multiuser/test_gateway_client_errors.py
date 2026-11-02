"""T5A-R1-03：流式错误响应必须有界收尾并可关闭。"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.control.gateway_client import GatewayClient
from easel.control.store import Store
from easel.control.types import DomainError, Identity, WorkerHandle
from easel.control.workspace_api import WorkspaceApi


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes, tracker: dict):
        self._body = body
        self._tracker = tracker

    async def __aiter__(self):
        self._tracker["iterations"] += 1
        yield self._body

    async def aclose(self) -> None:
        self._tracker["closed"] = True


def handle() -> WorkerHandle:
    return WorkerHandle(
        "a" * 32, "c" * 64, "http://127.0.0.1:9999", "fake", 0,
        gateway_instance_id="gateway", gateway_generation="generation",
        gateway_upstream="http://127.0.0.1:8897",
        gateway_state="available")


def _client(body: bytes, status: int, tracker: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, headers={"Content-Type": "application/json"},
            stream=TrackingStream(body, tracker), request=request)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False)


@pytest.mark.parametrize("status,body", [
    (404, b'{"detail":"not found"}'),
    (403, b'{"detail":"forbidden"}'),
    (413, b'{"detail":"too large"}'),
    (429, b'{"detail":"busy"}'),
    (503, b'{"detail":"unavailable"}'),
])
def test_stream_workspace_errors_are_typed_and_closed(status, body):
    tracker = {"closed": False, "iterations": 0}

    async def scenario():
        client = _client(body, status, tracker)
        try:
            with pytest.raises(DomainError) as caught:
                await GatewayClient(
                    Ed25519PrivateKey.generate(), client).request(
                    handle(), "GET", "/api/media/missing.png",
                    stream=True, status_mode="workspace")
            assert caught.value.status is not None
            assert caught.value.status != 500
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert tracker["closed"] is True
    assert tracker["iterations"] == 0, "workspace 错误不应读取 worker 正文"


def test_stream_relay_gateway_error_is_bounded_and_closed():
    tracker = {"closed": False, "iterations": 0}
    body = b'{"error":"gateway_inactive"}'

    async def scenario():
        client = _client(body, 503, tracker)
        try:
            with pytest.raises(DomainError) as caught:
                await GatewayClient(
                    Ed25519PrivateKey.generate(), client).request(
                    handle(), "GET", "/internal/lifecycle/health",
                    stream=True, status_mode="relay")
            assert caught.value.code == "worker_gateway_unavailable"
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert tracker["closed"] is True
    assert tracker["iterations"] == 1


@pytest.mark.parametrize("error", [
    DomainError("not_found", 404),
    asyncio.CancelledError(),
])
def test_stream_request_failure_releases_workspace_lease(tmp_path, error):
    store = Store.connect(tmp_path / "control.sqlite3")
    user = store.create_user("lease-member")
    actor = Identity(user.user_id, user.workspace_id, "member")
    handle_value = handle()

    class Runtime:
        async def ensure_ready(self, workspace_id, config_version):
            return WorkerHandle(
                workspace_id, handle_value.container_id,
                handle_value.upstream, handle_value.service_token,
                config_version,
                gateway_instance_id=handle_value.gateway_instance_id,
                gateway_generation=handle_value.gateway_generation,
                gateway_upstream=handle_value.gateway_upstream,
                gateway_state="available")

    class FailingGateway:
        async def request(self, *_args, **_kwargs):
            raise error

    api = WorkspaceApi(
        Runtime(), store, FailingGateway(), revision=0,
        gateway_required=True)

    async def scenario():
        try:
            await api.open_stream(
                actor, "GET", "/api/media/missing.png")
        except BaseException as caught:
            return caught

    try:
        caught = asyncio.run(scenario())
        assert isinstance(caught, type(error))
        assert store.count_worker_request_leases(actor.workspace_id) == 0
    finally:
        store.close()


def test_oversized_error_body_is_still_closed_without_unbounded_read():
    tracker = {"closed": False, "iterations": 0}
    body = b'{"error":"gateway_inactive","padding":"' + b"x" * (70 * 1024) + b'"}'

    async def scenario():
        client = _client(body, 503, tracker)
        try:
            with pytest.raises(DomainError) as caught:
                await GatewayClient(
                    Ed25519PrivateKey.generate(), client).request(
                    handle(), "GET", "/internal/lifecycle/health",
                    stream=True, status_mode="relay")
            assert caught.value.status == 502
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert tracker["closed"] is True
