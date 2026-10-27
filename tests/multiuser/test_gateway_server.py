"""G2 gateway 进程内可信配置、状态与激活握手。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.gateway.auth import (
    GATEWAY_ASSERTION_HEADER,
    GatewayAudience,
    sign_gateway_assertion,
)

NOW = 1_750_000_000
WORKSPACE = "a" * 32
BOOT = "b" * 32
WORKER = "c" * 64
TOKEN = "service-token-secret"
ORIGIN = "http://172.30.0.2:7860"
GATEWAY_ID = "d" * 64
PRIVATE = Ed25519PrivateKey.generate()
PUBLIC_KEY_SHA256 = hashlib.sha256(
    PRIVATE.public_key().public_bytes_raw()).hexdigest()


def _payload(**overrides):
    payload = {
        "workspace_id": WORKSPACE,
        "gateway_instance_id": GATEWAY_ID,
        "expected_boot_generation": BOOT,
        "worker_container_id": WORKER,
        "config_version": 7,
        "worker_origin": ORIGIN,
        "worker_service_token": TOKEN,
    }
    payload.update(overrides)
    return payload


def _activation(**overrides):
    payload = {
        "workspace_id": WORKSPACE,
        "gateway_instance_id": GATEWAY_ID,
        "boot_generation": BOOT,
        "worker_container_id": WORKER,
        "config_version": 7,
    }
    payload.update(overrides)
    return payload


class _BodyStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes):
        self._body = body

    async def __aiter__(self):
        yield self._body


class _CloseFailingStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"{}"

    async def aclose(self):
        raise OSError("close failed token=" + TOKEN)


def _response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200, headers={"Content-Type": "application/json"},
        stream=_BodyStream(json.dumps(payload).encode("utf-8")))


def _service(*, boot_generation: str = BOOT, transport=None):
    from easel.gateway.server import GatewayService

    service = GatewayService(
        PRIVATE.public_key(), management_bind="172.30.0.3",
        management_port=8897, boot_generation=boot_generation,
        clock=lambda: NOW,
        transport=transport or httpx.MockTransport(
            lambda request: _response({"ok": True})))
    return service


def _scope(method: str, path: str, *, query: bytes = b"", headers=None):
    return {
        "type": "http",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query,
        "headers": headers or [],
        "client": ("127.0.0.1", 12345),
        "server": ("172.30.0.3", 8897),
        "http_version": "1.1",
    }


async def _http(service, method: str, path: str, *, query: bytes = b"",
                headers=None):
    messages: list[dict] = []
    scope = _scope(method, path, query=query, headers=headers)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await service.asgi(scope, receive, send)
    start = next(item for item in messages
                 if item["type"] == "http.response.start")
    body = b"".join(item.get("body", b"") for item in messages
                    if item["type"] == "http.response.body")
    return start["status"], body, {key.decode(): value.decode()
                                   for key, value in start["headers"]}


def _assertion_headers(method: str, path: str, *, boot=BOOT,
                       gateway_id=GATEWAY_ID, worker=WORKER,
                       workspace=WORKSPACE, version=7, query=None) -> list[
                           tuple[bytes, bytes]]:
    audience = GatewayAudience(workspace, gateway_id, boot, worker, version)
    token = sign_gateway_assertion(
        PRIVATE, audience, method=method, path=path, query=query,
        issued_at=NOW, expires_at=NOW + 30,
        nonce="nonce-" + secrets.token_hex(8),
        request_id="request-" + secrets.token_hex(8), clock=lambda: NOW)
    return [(GATEWAY_ASSERTION_HEADER.lower().encode(), token.encode())]


def test_unconfigured_gateway_returns_inactive_without_secret():
    async def scenario():
        service = _service()
        status, body, headers = await _http(
            service, "GET", "/internal/lifecycle/health")
        assert status == 503
        assert json.loads(body) == {"error": "gateway_inactive"}
        assert headers["cache-control"] == "no-store"
        state = service.status()
        assert state["configured"] is False
        assert state["active"] is False
        assert state["available"] is False
        assert state["boot_generation"] == BOOT
        serialized = json.dumps(state)
        assert TOKEN not in serialized
        assert "service-token" not in serialized
        await service.aclose()

    asyncio.run(scenario())


def test_configure_binds_exact_instance_but_does_not_activate():
    async def scenario():
        service = _service()
        state = await service.configure(_payload())
        assert state["configured"] is True
        assert state["active"] is False
        assert state["available"] is False
        assert state["gateway_instance_id"] == GATEWAY_ID
        assert state["worker_container_id"] == WORKER
        assert state["public_key_sha256"] == PUBLIC_KEY_SHA256

        path = "/internal/lifecycle/health"
        status, body, _ = await _http(
            service, "GET", path, headers=_assertion_headers("GET", path))
        assert status == 503
        assert json.loads(body) == {"error": "gateway_inactive"}
        assert TOKEN not in json.dumps(service.status())
        await service.aclose()

    asyncio.run(scenario())


def test_activate_requires_exact_instance_boot_worker_and_version():
    async def scenario():
        service = _service()
        await service.configure(_payload())

        for override in (
                {"gateway_instance_id": "e" * 64},
                {"boot_generation": "f" * 32},
                {"worker_container_id": "0" * 64},
                {"config_version": 8},
                {"workspace_id": "1" * 32}):
            with pytest.raises(Exception) as rejected:
                await service.activate(_activation(**override))
            assert getattr(rejected.value, "code", "") == \
                "gateway_activation_mismatch"
            assert service.status()["active"] is False

        state = await service.activate(_activation())
        assert state["active"] is True
        assert state["available"] is True
        assert "worker_service_token" not in state
        assert TOKEN not in json.dumps(state)
        await service.aclose()

    asyncio.run(scenario())


def test_activated_gateway_forwards_valid_assertion_only():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _response({"ready": True})

    async def scenario():
        service = _service(transport=httpx.MockTransport(handler))
        await service.configure(_payload())
        await service.activate(_activation())
        path = "/internal/lifecycle/health"
        status, body, _ = await _http(
            service, "GET", path, headers=_assertion_headers("GET", path))
        assert status == 200
        assert json.loads(body) == {"ready": True}
        assert str(captured[0].url) == ORIGIN + path
        assert captured[0].headers["X-Easel-Service-Token"] == TOKEN

        bad = _assertion_headers("GET", path, boot="f" * 32)
        status, body, _ = await _http(
            service, "GET", path, headers=bad)
        assert status in (401, 403)
        assert captured and len(captured) == 1
        await service.aclose()

    asyncio.run(scenario())


def test_new_process_boot_invalidates_old_assertion_even_same_container():
    async def scenario():
        old = _service(boot_generation="a" * 32)
        await old.configure(_payload(expected_boot_generation="a" * 32))
        await old.activate(_activation(boot_generation="a" * 32))
        old_headers = _assertion_headers("GET", "/internal/lifecycle/health",
                                         boot="a" * 32)
        await old.aclose()

        restarted = _service(boot_generation="b" * 32)
        state = restarted.status()
        assert state["configured"] is False
        assert state["boot_generation"] == "b" * 32
        status, _, _ = await _http(
            restarted, "GET", "/internal/lifecycle/health",
            headers=old_headers)
        assert status == 503
        assert json.loads((await _http(
            restarted, "GET", "/internal/lifecycle/health",
            headers=old_headers))[1]) == {"error": "gateway_inactive"}

        await restarted.configure(_payload(expected_boot_generation="b" * 32))
        await restarted.activate(_activation(boot_generation="b" * 32))
        status, body, _ = await _http(
            restarted, "GET", "/internal/lifecycle/health",
            headers=old_headers)
        assert status in (401, 403)
        assert b"gateway_inactive" not in body
        await restarted.aclose()

    asyncio.run(scenario())


def test_status_reports_isolated_close_failure_as_unavailable():
    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"Content-Type": "application/json"},
                stream=_CloseFailingStream())

        service = _service(transport=httpx.MockTransport(handler))
        await service.configure(_payload())
        await service.activate(_activation())
        path = "/internal/lifecycle/health"
        status, body, _ = await _http(
            service, "GET", path, headers=_assertion_headers("GET", path))
        assert status == 502
        assert b"gateway_upstream_close_failed" in body
        state = service.status()
        assert state["active"] is True
        assert state["available"] is False
        serialized = json.dumps(state)
        assert TOKEN not in serialized
        assert "service-token" not in serialized
        await service.aclose()

    asyncio.run(scenario())


def test_configure_refuses_to_replace_active_gateway():
    async def scenario():
        service = _service()
        await service.configure(_payload())
        await service.activate(_activation())
        with pytest.raises(Exception) as rejected:
            await service.configure(_payload())
        assert getattr(rejected.value, "code", "") == "gateway_already_active"
        assert service.status()["active"] is True
        await service.aclose()

    asyncio.run(scenario())


def test_config_rejects_mismatched_boot_and_unknown_fields_without_secret():
    async def scenario():
        service = _service()
        with pytest.raises(Exception) as rejected:
            await service.configure(_payload(expected_boot_generation="f" * 32))
        assert getattr(rejected.value, "code", "") == "gateway_config_mismatch"
        with pytest.raises(Exception) as unknown:
            await service.configure({**_payload(), "image": "evil"})
        assert getattr(unknown.value, "code", "") == "gateway_config_invalid"
        assert TOKEN not in str(rejected.value) + str(unknown.value)
        await service.aclose()

    asyncio.run(scenario())
