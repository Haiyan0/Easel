"""A4.1：control→gateway 正式签名传输的红灯测试。

覆盖最终 URL/query/body 与签名输入一致、client 默认凭证不泄漏、缺失句柄
字段失败关闭、错误码脱敏与不重试/不跟随重定向。
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.control.gateway_client import GatewayClient
from easel.control.types import DomainError, WorkerHandle
from easel.gateway.auth import (
    GATEWAY_ASSERTION_HEADER,
    GatewayAudience,
    verify_gateway_assertion,
)

WORKSPACE = "a" * 32
WORKER = "b" * 64
GATEWAY = "c" * 64
BOOT = "d" * 32
UPSTREAM = "http://172.31.0.9:8897"


def _handle(**overrides) -> WorkerHandle:
    values = {
        "workspace_id": WORKSPACE,
        "container_id": WORKER,
        "upstream": "http://172.32.0.2:7860",
        "service_token": "worker-service-token",
        "config_version": 3,
        "gateway_instance_id": GATEWAY,
        "gateway_generation": BOOT,
        "gateway_upstream": UPSTREAM,
        "gateway_state": "available",
    }
    values.update(overrides)
    return WorkerHandle(**values)


class CaptureTransport(httpx.AsyncBaseTransport):
    def __init__(self, response_factory=None) -> None:
        self.requests: list[dict] = []
        self._response_factory = response_factory or (
            lambda request: httpx.Response(
                200, json={"state": "running"},
                headers={"content-type": "application/json"},
                request=request))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        self.requests.append({
            "method": request.method,
            "url": str(request.url),
            "headers": dict(request.headers),
            "body": body,
        })
        return self._response_factory(request)


def _audience(handle: WorkerHandle | None = None) -> GatewayAudience:
    handle = handle or _handle()
    return GatewayAudience(
        handle.workspace_id, handle.gateway_instance_id,
        handle.gateway_generation, handle.container_id,
        handle.config_version)


def _decode_assertion(
        key: Ed25519PrivateKey, captured: dict, *, body_hash: str | None = None,
        query: str = "") -> dict:
    url = httpx.URL(captured["url"])
    raw_path = url.raw_path.decode("ascii").split("?", 1)[0]
    assertion = verify_gateway_assertion(
        key.public_key(),
        captured["headers"][GATEWAY_ASSERTION_HEADER.lower()],
        audience=_audience(),
        method=captured["method"],
        path=raw_path,
        canonical_query=query,
        body_sha256=body_hash or hashlib.sha256(captured["body"]).hexdigest(),
        now=1000)
    return assertion


def test_client_signs_exact_final_url_query_and_body_once():
    key = Ed25519PrivateKey.generate()
    transport = CaptureTransport()

    async def scenario() -> None:
        async with httpx.AsyncClient(
                transport=transport,
                headers={"Cookie": "browser-cookie",
                         "X-Easel-Service-Token": "must-not-leak",
                         "X-Forwarded-For": "203.0.113.1"},
                params={"injected": "1"},
                cookies={"session": "secret"},
                auth=("user", "password")) as client:
            gateway = GatewayClient(key, client, clock=lambda: 1000)
            response = await gateway.request(
                _handle(), "POST", "/internal/runs/" + "f" * 32,
                payload={"message": "你好"})
            assert response.status_code == 200
            await response.aclose()

    asyncio.run(scenario())
    assert len(transport.requests) == 1
    captured = transport.requests[0]
    body = json.dumps({"message": "你好"}, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    assert captured["body"] == body
    assert captured["url"] == UPSTREAM + "/internal/runs/" + "f" * 32
    lowered = {name.lower(): value for name, value in captured["headers"].items()}
    assert "cookie" not in lowered
    assert "authorization" not in lowered
    assert "x-easel-service-token" not in lowered
    assert "x-easel-workspace-id" not in lowered
    assert "x-forwarded-for" not in lowered
    assertion = _decode_assertion(key, captured)
    assert assertion.path == "/internal/runs/" + "f" * 32


def test_client_events_nonce_query_is_canonical_and_signed():
    key = Ed25519PrivateKey.generate()
    transport = CaptureTransport()

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            gateway = GatewayClient(key, client, clock=lambda: 1000)
            response = await gateway.request(
                _handle(), "GET",
                "/internal/runs/" + "f" * 32 + "/events",
                query={"nonce": "n 1", "after": "7"})
            await response.aclose()

    asyncio.run(scenario())
    captured = transport.requests[0]
    expected_query = "after=7&nonce=n%201"
    assert captured["url"].endswith("?" + expected_query)
    assertion = _decode_assertion(key, captured, query=expected_query)
    assert assertion.canonical_query == expected_query


def test_client_get_has_empty_body_and_canonical_sorted_query():
    key = Ed25519PrivateKey.generate()
    transport = CaptureTransport()

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            gateway = GatewayClient(key, client, clock=lambda: 1000)
            response = await gateway.request(
                _handle(), "GET",
                "/internal/sessions/" + "e" * 32 + "/messages",
                query={"offset": "3", "limit": "10"})
            await response.aclose()

    asyncio.run(scenario())
    captured = transport.requests[0]
    assert captured["body"] == b""
    assert captured["url"].endswith("?limit=10&offset=3")
    lowered = {name.lower() for name in captured["headers"]}
    assert "content-type" not in lowered
    assertion = _decode_assertion(key, captured, query="limit=10&offset=3")
    assert assertion.body_sha256 == hashlib.sha256(b"").hexdigest()


def test_client_encodes_question_id_like_gateway_contract():
    key = Ed25519PrivateKey.generate()
    transport = CaptureTransport()

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            gateway = GatewayClient(key, client, clock=lambda: 1000)
            path = ("/internal/runs/" + "f" * 32
                    + "/questions/ask%20me/answer")
            response = await gateway.request(
                _handle(), "POST", path,
                payload={"runId": "f" * 32, "questionId": "ask me",
                         "answers": {}})
            await response.aclose()

    asyncio.run(scenario())
    captured = transport.requests[0]
    assert "/questions/ask%20me/answer" in captured["url"]
    assertion = _decode_assertion(key, captured)
    assert assertion.path.endswith("/questions/ask%20me/answer")


@pytest.mark.parametrize("overrides", [
    {"gateway_instance_id": ""},
    {"gateway_generation": ""},
    {"gateway_upstream": ""},
    {"gateway_state": "unavailable"},
    {"gateway_state": "staged"},
])
def test_client_rejects_missing_or_unavailable_gateway_fields_before_http(
        overrides):
    key = Ed25519PrivateKey.generate()
    transport = CaptureTransport()

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            gateway = GatewayClient(key, client, clock=lambda: 1000)
            with pytest.raises(DomainError) as rejected:
                await gateway.request(
                    _handle(**overrides), "GET", "/internal/lifecycle/health")
            assert rejected.value.code == "worker_gateway_unavailable"
            assert rejected.value.status == 503

    asyncio.run(scenario())
    assert transport.requests == []


def _error_factory(status: int, error: str, *, headers=None):
    def factory(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, json={"error": error}, headers=headers or {}, request=request)
    return factory


@pytest.mark.parametrize(("status", "error", "code", "expected"), [
    (401, "gateway_unauthorized", "worker_gateway_unavailable", 503),
    (403, "gateway_forbidden", "worker_gateway_unavailable", 503),
    (503, "gateway_inactive", "worker_gateway_unavailable", 503),
    (503, "gateway_unavailable", "worker_gateway_unavailable", 503),
    (502, "gateway_upstream_unavailable", "upstream_unavailable", 502),
    (504, "gateway_upstream_timeout", "upstream_timeout", 504),
    (429, "gateway_busy", "worker_gateway_busy", 429),
    (404, "run_not_found", "not_found", 404),
    (409, "run_conflict", "conflict", 409),
])
def test_client_maps_gateway_errors_without_retry(
        status, error, code, expected):
    key = Ed25519PrivateKey.generate()
    transport = CaptureTransport(_error_factory(status, error))

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            gateway = GatewayClient(key, client, clock=lambda: 1000)
            with pytest.raises(DomainError) as rejected:
                await gateway.request(
                    _handle(), "POST", "/internal/runs/" + "f" * 32,
                    payload={"message": "x"})
            assert rejected.value.code == code
            assert rejected.value.status == expected

    asyncio.run(scenario())
    assert len(transport.requests) == 1


def test_client_preserves_retry_after_and_refuses_redirects():
    key = Ed25519PrivateKey.generate()
    transport = CaptureTransport(
        _error_factory(429, "gateway_busy", headers={"Retry-After": "7"}))
    redirect = CaptureTransport(lambda request: httpx.Response(
        302, headers={"Location": "http://evil.test/"}, request=request))

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            gateway = GatewayClient(key, client, clock=lambda: 1000)
            with pytest.raises(DomainError) as busy:
                await gateway.request(
                    _handle(), "GET", "/internal/lifecycle/health")
            assert busy.value.retry_after == 7
        async with httpx.AsyncClient(transport=redirect) as client:
            gateway = GatewayClient(key, client, clock=lambda: 1000)
            with pytest.raises(DomainError) as moved:
                await gateway.request(
                    _handle(), "GET", "/internal/lifecycle/health")
            assert moved.value.code == "upstream_redirect"
            assert moved.value.status == 502

    asyncio.run(scenario())
    assert len(transport.requests) == 1
    assert len(redirect.requests) == 1


def test_client_transport_failure_is_sanitized_503():
    key = Ed25519PrivateKey.generate()

    class Broken(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("secret upstream detail", request=request)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=Broken()) as client:
            gateway = GatewayClient(key, client, clock=lambda: 1000)
            with pytest.raises(DomainError) as rejected:
                await gateway.request(
                    _handle(), "GET", "/internal/lifecycle/health")
            assert rejected.value.code == "worker_gateway_unavailable"
            assert rejected.value.status == 503
            assert "secret" not in str(rejected.value)

    asyncio.run(scenario())


def test_client_health_uses_lifecycle_route_and_validates_identity():
    key = Ed25519PrivateKey.generate()
    transport = CaptureTransport(lambda request: httpx.Response(
        200, json={"workspace_id": WORKSPACE, "container_id": WORKER,
                   "config_version": 3, "ready": True,
                   "execution_capable": True},
        request=request))

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            gateway = GatewayClient(key, client, clock=lambda: 1000)
            assert await gateway.health(_handle()) is True
            assert await gateway.health(
                _handle(workspace_id="f" * 32)) is False

    asyncio.run(scenario())
    assert transport.requests[0]["url"] == \
        UPSTREAM + "/internal/lifecycle/health"
