"""Task 5A gateway：基础工作区路由、条件请求与流式清理。"""
from __future__ import annotations

import secrets
from dataclasses import replace
from urllib.parse import quote

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
                audience_value: GatewayAudience | None = None) -> dict[str, str]:
    token = sign_gateway_assertion(
        PRIVATE, audience_value or audience(), method=method, path=path,
        query=query, body=body, issued_at=NOW, expires_at=NOW + 30,
        nonce="nonce-" + secrets.token_hex(8),
        request_id="request-" + secrets.token_hex(8))
    return {GATEWAY_ASSERTION_HEADER: token}


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


def capture(handler=None, *, status: int = 200,
            body: bytes = b'{"ok":true}', headers=None):
    requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if handler is not None:
            return handler(request)
        return _response(status, body, headers)

    return _handler, requests


def make_app(handler, *, limits: GatewayLimits | None = None):
    return create_gateway_app(
        config(), PRIVATE.public_key(), clock=lambda: NOW, activated=True,
        transport=httpx.MockTransport(handler), limits=limits)


def canonical(*segments: str) -> str:
    return "/".join(quote(segment, safe="-._~") for segment in segments)


PERSONA = canonical("测试 画像")
IMAGE = canonical("项目 甲", "封面 图.png")
ZIP = canonical("项目 甲", "素材.zip")
RECORD_ID = "a1b2c3d4e5f6"


ROUTES = [
    ("GET", "/api/status", None, b"", None),
    ("GET", "/api/personas", None, b"", None),
    ("GET", f"/api/persona/{PERSONA}", None, b"", None),
    ("GET", f"/api/persona/{PERSONA}/files", None, b"", None),
    ("PUT", f"/api/persona/{PERSONA}/file", None,
     b'{"filename":"identity.md","content":"x"}', None),
    ("DELETE", f"/api/persona/{PERSONA}", None, b"", None),
    ("GET", "/api/outputs", None, b"", None),
    ("GET", "/api/artifacts/archive", {"path": "项目 甲/素材.zip"}, b"", None),
    ("GET", f"/api/output/{IMAGE}", None, b"", None),
    ("DELETE", f"/api/output/{IMAGE}", None, b"", None),
    ("GET", f"/api/media/{IMAGE}", None, b"", {"Range": "bytes=0-9"}),
    ("HEAD", f"/api/media/{IMAGE}", None, b"", {"Range": "bytes=0-9"}),
    ("GET", "/api/upload/limits", None, b"", None),
    ("POST", "/api/upload", None, b"--x\r\ncontent\r\n--x--\r\n",
     {"Content-Type": "multipart/form-data; boundary=x"}),
    ("GET", "/api/trends", {"platforms": "weibo,douyin", "limit": "15"},
     b"", None),
    ("GET", "/api/schedule", None, b"", None),
    ("POST", "/api/schedule", None,
     b'{"title":"x","date":"2026-10-30"}', None),
    ("GET", "/api/schedule/context", {"days": "14"}, b"", None),
    ("PUT", f"/api/schedule/{RECORD_ID}", None,
     b'{"title":"x","date":"2026-10-31"}', None),
    ("DELETE", f"/api/schedule/{RECORD_ID}", None, b"", None),
    ("GET", "/api/ideas", None, b"", None),
    ("POST", "/api/ideas", None, b'{"title":"x"}', None),
    ("PUT", f"/api/ideas/{RECORD_ID}", None, b'{"title":"x"}', None),
    ("DELETE", f"/api/ideas/{RECORD_ID}", None, b"", None),
    ("GET", "/api/skills", None, b"", None),
    ("GET", "/api/skill/demo", None, b"", None),
    ("POST", "/api/skill/prepare", None,
     b'{"skill":"demo","input":"x"}', None),
    ("POST", "/api/profile/baseline", None,
     b'{"name":"p","form":{}}', None),
]


@pytest.mark.parametrize("method,path,query,body,extra_headers", ROUTES)
def test_workspace_routes_forward_exact_request(method, path, query, body,
                                                extra_headers):
    handler, requests = capture(status=200, body=b'{"route":"workspace"}')
    app = make_app(handler)
    request_path = path
    if query:
        request_path += "?" + canonical_query(query)
    with TestClient(app, base_url="http://gateway") as client:
        response = client.request(
            method, request_path,
            headers={**headers_for(method, path, query=query, body=body),
                     **(extra_headers or {})},
            content=body or None)

    assert response.status_code == 200
    if method == "HEAD":
        assert response.content == b""
    else:
        assert response.content == b'{"route":"workspace"}'
    assert len(requests) == 1
    forwarded = requests[0]
    expected = ORIGIN + path + (
        "?" + canonical_query(query) if query else "")
    assert str(forwarded.url) == expected
    assert forwarded.method == method
    assert forwarded.content == body
    assert forwarded.headers["x-easel-workspace-id"] == WORKSPACE
    assert forwarded.headers["x-easel-service-token"] == TOKEN
    assert "x-easel-gateway-assertion" not in forwarded.headers
    if extra_headers:
        for name, value in extra_headers.items():
            assert forwarded.headers[name] == value


def test_media_range_206_and_headers_stream_through():
    handler, requests = capture(
        status=206,
        body=b"0123456789",
        headers={
            "Content-Type": "image/png",
            "Content-Range": "bytes 0-9/100",
            "Accept-Ranges": "bytes",
            "Content-Length": "10",
            "ETag": '"abc"',
        })
    app = make_app(handler)
    path = f"/api/media/{IMAGE}"
    with TestClient(app, base_url="http://gateway") as client:
        response = client.get(
            path, headers={**headers_for(
                "GET", path, body=b""), "Range": "bytes=0-9"})
    assert response.status_code == 206
    assert response.content == b"0123456789"
    assert response.headers["content-range"] == "bytes 0-9/100"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == "10"
    assert requests[0].headers["range"] == "bytes=0-9"
    assert app.state.gateway.active_requests == 0
    assert app.state.gateway.active_streams == 0


def test_media_head_preserves_length_without_body():
    handler, requests = capture(
        body=b"should-not-be-sent",
        headers={"Content-Type": "image/png", "Content-Length": "19"})
    app = make_app(handler)
    path = f"/api/media/{IMAGE}"
    with TestClient(app, base_url="http://gateway") as client:
        response = client.head(path, headers=headers_for("HEAD", path))
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == "19"
    assert requests[0].method == "HEAD"
    assert app.state.gateway.active_requests == 0


def test_media_304_is_not_treated_as_redirect():
    handler, requests = capture(
        status=304, body=b"",
        headers={"ETag": '"abc"', "Cache-Control": "private, no-cache"})
    app = make_app(handler)
    path = f"/api/media/{IMAGE}"
    headers = {
        **headers_for("GET", path, body=b""),
        "If-None-Match": '"abc"',
    }
    with TestClient(app, base_url="http://gateway") as client:
        response = client.get(path, headers=headers)
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == '"abc"'
    assert requests[0].headers["if-none-match"] == '"abc"'
    assert app.state.gateway.active_requests == 0


@pytest.mark.parametrize("method,path,expected", [
    ("GET", "/api/output/../secret", (400, 404)),
    ("GET", "/api/output/%2e%2e/secret", 400),
    ("GET", "/api/output/a/%2F/b", 400),
    ("GET", "/api/output/a/%5c/b", 400),
    ("GET", "/api/output/.easel.json", 400),
    ("GET", "/api/output/_login/qr.png", 400),
    ("GET", "/api/media/analytics/a.png", 400),
    ("GET", "/api/media/a//b.png", 400),
    ("GET", "/api/persona/../files", (400, 404)),
    ("GET", "/api/persona/%2e%2e/files", 400),
    ("GET", "/api/persona/%2F/files", 400),
    ("GET", "/api/artifacts/archive?path=", 400),
    ("GET", "/api/artifacts/archive?path=%2e%2e%2fx", 400),
    ("GET", "/api/accounts", 404),
    ("POST", "/api/outputs", 405),
    ("POST", "/api/status", 405),
    ("PUT", "/api/personas", 405),
    ("GET", "/api/upload", 405),
    ("GET", "/api/trends?platforms=weibo,evil", 400),
    ("GET", "/api/trends?platforms=weibo&platforms=douyin", 400),
    ("GET", "/api/trends?limit=0", 400),
    ("GET", "/api/schedule/context?days=91", 400),
    ("GET", "/api/schedule/context?days=0", 400),
    ("PUT", "/api/ideas/not-a-record", 400),
    ("PUT", "/api/ideas/a1b2c3d4e5f", 400),
    ("DELETE", "/api/schedule/a1b2c3d4e5f6z", 400),
    ("GET", f"/api/ideas/{RECORD_ID}", 405),
    ("POST", f"/api/ideas/{RECORD_ID}", 405),
    ("GET", f"/api/schedule/{RECORD_ID}", 405),
])
def test_workspace_route_negatives_never_reach_upstream(method, path, expected):
    handler, requests = capture()
    app = make_app(handler)
    with TestClient(app, base_url="http://gateway") as client:
        response = client.request(method, path)
    allowed = expected if isinstance(expected, tuple) else (expected,)
    assert response.status_code in allowed, response.text
    assert requests == []


def test_workspace_upload_body_limit_and_type():
    handler, requests = capture()
    app = make_app(handler, limits=GatewayLimits(
        max_body_bytes=8, max_header_bytes=65536,
        connect_timeout=1.0, io_timeout=1.0,
        max_concurrent_requests=8, max_concurrent_streams=7))
    path = "/api/upload"
    body = b"123456789"
    with TestClient(app, base_url="http://gateway") as client:
        response = client.post(
            path, headers=headers_for("POST", path, body=body), content=body)
    assert response.status_code == 413
    assert requests == []


def test_workspace_response_status_is_passed_through_sanitized():
    handler, _requests = capture(
        status=403, body=b'{"detail":"forbidden"}',
        headers={"Set-Cookie": "a=b", "Location": "http://evil.invalid"})
    app = make_app(handler)
    path = "/api/outputs"
    with TestClient(app, base_url="http://gateway") as client:
        response = client.get(path, headers=headers_for("GET", path))
    assert response.status_code == 403
    assert response.json() == {"detail": "forbidden"}
    assert "set-cookie" not in response.headers
    assert "location" not in response.headers
