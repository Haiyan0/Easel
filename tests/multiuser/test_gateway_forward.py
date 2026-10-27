"""Task G1 gateway 精确路由、头净化、响应语义与转发边界。"""
from __future__ import annotations

import asyncio
import secrets
from dataclasses import replace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from easel.gateway.app import GatewayLimits, create_gateway_app
from easel.gateway.auth import (
    GATEWAY_ASSERTION_HEADER,
    GatewayAudience,
    GatewayConfig,
    canonical_query,
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
SESSION = "e" * 32
QUESTION = "q-1"
PRIVATE = Ed25519PrivateKey.generate()


def audience(**changes) -> GatewayAudience:
    value = GatewayAudience(WORKSPACE, GATEWAY, BOOT, WORKER, 7)
    return replace(value, **changes)


def config(**changes) -> GatewayConfig:
    value = GatewayConfig(
        workspace_id=WORKSPACE,
        gateway_instance_id=GATEWAY,
        boot_generation=BOOT,
        worker_container_id=WORKER,
        config_version=7,
        worker_origin=ORIGIN,
        worker_service_token=TOKEN,
    )
    return replace(value, **changes)


def headers_for(method: str, path: str, *, query=None, body=b"",
                audience_value: GatewayAudience | None = None,
                nonce: str | None = None, issued_at: int = NOW,
                expires_at: int = NOW + 30) -> dict[str, str]:
    token = sign_gateway_assertion(
        PRIVATE, audience_value or audience(), method=method, path=path,
        query=query, body=body, issued_at=issued_at, expires_at=expires_at,
        nonce=nonce or ("nonce-" + secrets.token_hex(8)),
        request_id="request-" + secrets.token_hex(8))
    return {GATEWAY_ASSERTION_HEADER: token}


def make_app(handler, *, cfg: GatewayConfig | None = None,
             activated: bool = True, limits: GatewayLimits | None = None,
             http_client: httpx.AsyncClient | None = None,
             public_key=None):
    cfg = cfg or config()
    kwargs = {}
    if http_client is not None:
        kwargs["http_client"] = http_client
    else:
        kwargs["transport"] = httpx.MockTransport(handler)
    return create_gateway_app(
        cfg, public_key or PRIVATE.public_key(), clock=lambda: NOW,
        activated=activated, limits=limits, **kwargs)


class _BodyStream(httpx.AsyncByteStream):
    """测试上游的未消费字节流；与真实 HTTP 响应一致，避免 StreamConsumed。"""

    def __init__(self, body: bytes):
        self._body = body
        self.closed = False

    async def __aiter__(self):
        yield self._body

    async def aclose(self) -> None:
        self.closed = True


def _response(status: int = 200, body: bytes = b"", headers=None):
    return httpx.Response(status, headers=headers, stream=_BodyStream(body))


def capture(handler=None, *, status: int = 200,
            body: bytes = b'{"ok":true}', headers=None):
    requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if handler is not None:
            return handler(request)
        return _response(status, body, headers)

    return _handler, requests


ROUTES = [
    ("GET", "/internal/lifecycle/health", None, b"", 200),
    ("POST", f"/internal/runs/{RUN}", None, b'{"envelope":"e"}', 202),
    ("GET", f"/internal/runs/{RUN}/events",
     {"after": "2", "nonce": "n-1"}, b"", 200),
    ("POST", f"/internal/runs/{RUN}/stop", None, b'{"runId":"x"}', 200),
    ("POST", f"/internal/runs/{RUN}/questions/{QUESTION}/answer", None,
     b'{"answers":["yes"]}', 200),
    ("POST", f"/internal/runs/{RUN}/questions/status", None,
     b'{"questionIds":["q-1"]}', 200),
    ("GET", f"/internal/sessions/{SESSION}/messages",
     {"offset": "0", "limit": "10"}, b"", 200),
]


def _request(client: TestClient, method: str, path: str,
             headers: dict[str, str], body: bytes, query=None):
    if method == "GET":
        return client.get(path, headers=headers)
    return client.post(path, headers=headers, content=body)


@pytest.mark.parametrize("method,path,query,body,status", ROUTES)
def test_all_seven_routes_forward_to_the_fixed_worker(method, path,
                                                      query, body, status):
    handler, requests = capture(
        status=status, body=b'{"route":"fixture"}')
    app = make_app(handler)
    request_path = path
    if query:
        request_path += "?" + canonical_query(query)
    with TestClient(app, base_url="http://gateway") as client:
        response = _request(
            client, method, request_path,
            headers_for(method, path, query=query, body=body), body)

    assert response.status_code == status
    assert response.content == b'{"route":"fixture"}'
    assert len(requests) == 1
    forwarded = requests[0]
    expected_query = canonical_query(query or {})
    expected_url = ORIGIN + path + (
        ("?" + expected_query) if expected_query else "")
    assert str(forwarded.url) == expected_url
    assert forwarded.method == method
    assert forwarded.content == body
    assert forwarded.headers["x-easel-workspace-id"] == WORKSPACE
    assert forwarded.headers["x-easel-service-token"] == TOKEN
    assert forwarded.headers["host"] == "worker.internal:7860"
    assert "x-easel-gateway-assertion" not in forwarded.headers


def test_header_sanitation_rebuilds_identity_and_ignores_user_target():
    handler, requests = capture()
    app = make_app(handler)
    path = "/internal/lifecycle/health"
    inbound = {
        **headers_for("GET", path),
        "Authorization": "Bearer browser",
        "Cookie": "session=evil",
        "Forwarded": "for=203.0.113.10",
        "X-Forwarded-For": "203.0.113.10",
        "X-Forwarded-Proto": "https",
        "X-Easel-Workspace-Id": "evil-workspace",
        "X-Easel-Service-Token": "evil-token",
        "X-Easel-Role": "admin",
        "X-Easel-Upstream": "http://evil.invalid",
        "Host": "evil.invalid",
        "Origin": "https://evil.invalid",
        "X-Custom": "keep-me",
    }
    with TestClient(app, base_url="http://gateway") as client:
        response = client.get(path, headers=inbound)

    assert response.status_code == 200
    forwarded = requests[0]
    lowered = {key.lower(): value for key, value in forwarded.headers.items()}
    for stripped in ("authorization", "cookie", "forwarded", "x-forwarded-for",
                     "x-forwarded-proto", "x-easel-role", "x-easel-upstream",
                     "origin", "x-easel-gateway-assertion"):
        assert stripped not in lowered, stripped
    assert lowered["x-easel-workspace-id"] == WORKSPACE
    assert lowered["x-easel-service-token"] == TOKEN
    assert lowered["x-custom"] == "keep-me"
    assert lowered["host"] == "worker.internal:7860"
    assert str(forwarded.url) == ORIGIN + path


def test_missing_malformed_and_tampered_assertions_never_reach_upstream():
    handler, requests = capture()
    app = make_app(handler)
    path = "/internal/runs/" + RUN
    body = b'{"envelope":"e"}'

    with TestClient(app, base_url="http://gateway") as client:
        assert client.post(path, content=body).status_code == 401
        assert client.post(path, headers={
            GATEWAY_ASSERTION_HEADER: "v1.bad"}, content=body).status_code == 401
        tampered = headers_for("POST", path, body=body)
        assert client.post(path, headers=tampered,
                           content=b'{"envelope":"other"}').status_code == 403
        wrong_method = headers_for("GET", path, body=body)
        assert client.post(path, headers=wrong_method,
                           content=body).status_code == 403
        wrong_query = headers_for(
            "GET", f"/internal/runs/{RUN}/events", query={"after": "1"})
        assert client.get(
            f"/internal/runs/{RUN}/events?after=2",
            headers=wrong_query).status_code == 403

    assert requests == []


def test_audience_mismatch_expiry_and_inactive_never_reach_upstream():
    handler, requests = capture()
    path = "/internal/lifecycle/health"
    app = make_app(handler, activated=False)
    with TestClient(app, base_url="http://gateway") as client:
        assert client.get(path, headers=headers_for(
            "GET", path)).status_code == 503
        app.state.gateway.activate()
        assert client.get(path, headers=headers_for(
            "GET", path)).status_code == 200

        wrong_workspace = headers_for(
            "GET", path, audience_value=audience(workspace_id="f" * 32))
        assert client.get(path, headers=wrong_workspace).status_code == 403

        expired = headers_for(
            "GET", path, issued_at=NOW - 60, expires_at=NOW - 30)
        assert client.get(path, headers=expired).status_code == 401

    assert len(requests) == 1


def test_same_workspace_new_worker_generation_is_rejected_before_upstream():
    handler, requests = capture()
    app = make_app(handler)
    path = "/internal/lifecycle/health"
    old = headers_for(
        "GET", path, audience_value=audience(worker_container_id="f" * 64))
    with TestClient(app, base_url="http://gateway") as client:
        assert client.get(path, headers=old).status_code == 403
    assert requests == []


def test_restart_boot_generation_invalidates_old_assertion():
    handler, requests = capture()
    old_headers = headers_for("GET", "/internal/lifecycle/health")
    new_config = config(boot_generation="f" * 32)
    app = make_app(handler, cfg=new_config)
    with TestClient(app, base_url="http://gateway") as client:
        assert client.get(
            "/internal/lifecycle/health", headers=old_headers).status_code == 403
    assert requests == []


def test_duplicate_nonce_is_rejected_after_first_acceptance():
    handler, requests = capture()
    app = make_app(handler)
    path = "/internal/lifecycle/health"
    headers = headers_for("GET", path, nonce="fixed-nonce")
    with TestClient(app, base_url="http://gateway") as client:
        assert client.get(path, headers=headers).status_code == 200
        assert client.get(path, headers=headers).status_code == 401
    assert len(requests) == 1


def test_nonce_capacity_fails_closed_without_evicting_valid_nonce():
    handler, requests = capture()
    app = create_gateway_app(
        config(), PRIVATE.public_key(), clock=lambda: NOW, activated=True,
        transport=httpx.MockTransport(handler), nonce_capacity=1)
    path = "/internal/lifecycle/health"
    with TestClient(app, base_url="http://gateway") as client:
        first = client.get(
            path, headers=headers_for("GET", path, nonce="capacity-one"))
        second = client.get(
            path, headers=headers_for("GET", path, nonce="capacity-two"))
        third = client.get(
            path, headers=headers_for("GET", path, nonce="capacity-one"))
    assert first.status_code == 200
    assert second.status_code == 503
    assert second.json()["error"] == "gateway_nonce_capacity"
    assert third.status_code == 401, "有效 nonce 不得被新 nonce 驱逐"
    assert len(requests) == 1


@pytest.mark.parametrize("method,path,expected", [
    ("GET", "/internal/lifecycle/health/", 400),
    ("GET", "/internal//lifecycle/health", 400),
    ("GET", "/internal/%6cifecycle/health", 400),
    ("GET", f"/internal/runs/{RUN}/events/", 400),
    ("GET", "/internal/runs/%2F/events", 400),
    ("GET", f"/internal/runs/{RUN}/questions/%2F/answer", 400),
    ("GET", f"/internal/runs/{RUN}/questions/%5c/answer", 400),
    ("GET", f"/internal/runs/{RUN}/events?after=1&after=2", 400),
    ("GET", f"/internal/runs/{RUN}/events?after=01", 400),
    ("GET", f"/internal/runs/{RUN}/events?after=%32", 400),
    ("GET", f"/internal/runs/{RUN}/events?after=-1", 400),
    ("GET", f"/internal/runs/{RUN}/events?unknown=1", 400),
    ("GET", f"/internal/runs/{RUN}/events?nonce=", 400),
    ("GET", f"/internal/runs/{RUN}/events?after=1;nonce=x", 400),
    ("GET", f"/internal/sessions/{SESSION}/messages?limit=0", 400),
    ("GET", f"/internal/sessions/{SESSION}/messages?limit=501", 400),
    ("GET", f"/internal/sessions/{SESSION}/messages?offset=-1", 400),
    ("GET", f"/internal/sessions/{SESSION}/messages?limit=1&limit=2", 400),
    ("GET", "/internal/unknown", 404),
    ("POST", "/internal/lifecycle/health", 405),
    ("POST", f"/internal/runs/{RUN}/events", 405),
])
def test_route_and_query_negatives_are_rejected_before_upstream(
        method, path, expected):
    handler, requests = capture()
    app = make_app(handler)
    with TestClient(app, base_url="http://gateway") as client:
        response = client.request(method, path)
    assert response.status_code == expected
    assert requests == []


def test_normal_error_status_and_json_are_passed_through_without_faking():
    handler, requests = capture(
        status=409, body=b'{"error":"conflict","detail":"run_busy"}',
        headers={"Set-Cookie": "a=b", "X-Custom": "keep"})
    app = make_app(handler)
    path = "/internal/runs/" + RUN
    with TestClient(app, base_url="http://gateway") as client:
        response = client.post(
            path, headers=headers_for("POST", path, body=b"{}"),
            content=b"{}")
    assert response.status_code == 409
    assert response.content == b'{"error":"conflict","detail":"run_busy"}'
    assert "set-cookie" not in response.headers
    assert response.headers["x-custom"] == "keep"
    assert len(requests) == 1


def test_upstream_transport_error_returns_static_502_and_releases_slot():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    app = make_app(handler)
    path = "/internal/lifecycle/health"
    with TestClient(app, base_url="http://gateway") as client:
        response = client.get(path, headers=headers_for("GET", path))
    assert response.status_code == 502
    assert response.json() == {"error": "gateway_upstream_unavailable"}
    assert TOKEN not in response.text
    assert app.state.gateway.active_requests == 0
    assert app.state.gateway.active_streams == 0


def test_upstream_redirect_is_a_failure_and_credentials_are_stripped():
    captured = []

    def handler(request):
        response = _response(
            302, b"redirect",
            headers={"Location": "http://evil.invalid", "Set-Cookie": "a=b"})
        captured.append(response)
        return response

    app = make_app(handler)
    path = "/internal/lifecycle/health"
    with TestClient(app, base_url="http://gateway") as client:
        response = client.get(path, headers=headers_for("GET", path))
    assert response.status_code == 502
    assert response.json()["error"] == "gateway_upstream_redirect"
    assert "location" not in response.headers
    assert "set-cookie" not in response.headers
    assert captured[0].is_closed


def test_response_hop_by_hop_and_connection_named_headers_are_stripped():
    handler, _requests = capture(
        headers={
            "Connection": "keep-alive, X-Remove",
            "X-Remove": "remove-me",
            "Keep-Alive": "timeout=5",
            "Proxy-Authenticate": "Basic",
            "Proxy-Authorization": "Basic x",
            "Proxy-Connection": "keep-alive",
            "TE": "trailers",
            "Trailer": "X-Trailer",
            "Upgrade": "websocket",
            "Location": "http://evil.invalid",
            "Set-Cookie": "a=b",
            "X-Keep": "yes",
        })
    app = make_app(handler)
    path = "/internal/lifecycle/health"
    with TestClient(app, base_url="http://gateway") as client:
        response = client.get(path, headers=headers_for("GET", path))
    assert response.status_code == 200
    for name in ("connection", "x-remove", "keep-alive",
                 "proxy-authenticate", "proxy-authorization",
                 "proxy-connection", "te", "trailer", "upgrade",
                 "location", "set-cookie"):
        assert name not in response.headers, name
    assert response.headers["x-keep"] == "yes"


def test_body_limit_and_transfer_encoding_framing_are_rejected_pre_upstream():
    handler, requests = capture()
    app = make_app(handler, limits=GatewayLimits(
        max_body_bytes=4, max_header_bytes=65536,
        connect_timeout=1.0, io_timeout=1.0,
        max_concurrent_requests=8, max_concurrent_streams=7))
    path = "/internal/runs/" + RUN
    body = b"12345"
    with TestClient(app, base_url="http://gateway") as client:
        response = client.post(
            path, headers=headers_for("POST", path, body=body), content=body)
        assert response.status_code == 413

        # 明确的 Transfer-Encoding 与 Content-Length 同时出现也拒绝
        framed = headers_for("POST", path, body=b"{}")
        response = client.post(
            path, headers={**framed, "Transfer-Encoding": "chunked"},
            content=b"{}")
        assert response.status_code == 400
    assert requests == []


def test_concurrent_same_nonce_accepts_once_and_releases_slots():
    handler, requests = capture()
    app = make_app(handler)
    path = "/internal/lifecycle/health"
    headers = headers_for("GET", path, nonce="concurrent-nonce")

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
                transport=transport, base_url="http://gateway") as client:
            responses = await asyncio.gather(
                client.get(path, headers=headers),
                client.get(path, headers=headers),
            )
        await app.state.gateway.aclose()
        return responses

    responses = asyncio.run(scenario())
    assert sorted(response.status_code for response in responses) == [200, 401]
    assert len(requests) == 1
    assert app.state.gateway.active_requests == 0
    assert app.state.gateway.active_streams == 0
