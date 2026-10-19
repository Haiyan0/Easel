"""Task 3 worker 入口：固定 workspace、独立服务凭证与内部头重建。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import web.app as web
from easel.control.types import DomainError
from easel.worker.guard import (
    SERVICE_MODE_ENV,
    SERVICE_TOKEN_ENV,
    SERVICE_TOKEN_HEADER,
    WORKSPACE_HEADER,
    WORKSPACE_ID_ENV,
    WorkerIdentity,
    build_worker_headers,
    forward_to_worker,
    load_worker_identity,
    sanitize_forward_headers,
    verify_worker_request,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = "ws-a"
TOKEN = "service-token-a"
INTERNAL_HEADERS = {WORKSPACE_HEADER: WORKSPACE, SERVICE_TOKEN_HEADER: TOKEN}


# --------------------------------------------------------------------------
# 启动配置与请求校验
# --------------------------------------------------------------------------

def test_load_worker_identity_defaults_to_local():
    assert load_worker_identity({}) is None
    assert load_worker_identity({SERVICE_MODE_ENV: "local"}) is None


def test_load_worker_identity_reads_explicit_configuration():
    identity = load_worker_identity({
        SERVICE_MODE_ENV: "worker",
        WORKSPACE_ID_ENV: WORKSPACE,
        SERVICE_TOKEN_ENV: TOKEN,
    })
    assert identity == WorkerIdentity(WORKSPACE, TOKEN)
    assert TOKEN not in repr(identity)


@pytest.mark.parametrize("environ", [
    {SERVICE_MODE_ENV: "worker"},
    {SERVICE_MODE_ENV: "worker", WORKSPACE_ID_ENV: WORKSPACE},
    {SERVICE_MODE_ENV: "worker", SERVICE_TOKEN_ENV: TOKEN},
    {SERVICE_MODE_ENV: "control", WORKSPACE_ID_ENV: WORKSPACE,
     SERVICE_TOKEN_ENV: TOKEN},
    {SERVICE_MODE_ENV: "bogus", WORKSPACE_ID_ENV: WORKSPACE,
     SERVICE_TOKEN_ENV: TOKEN},
])
def test_load_worker_identity_rejects_missing_or_invalid_mode(environ):
    with pytest.raises(RuntimeError):
        load_worker_identity(environ)


def test_verify_worker_request_accepts_matching_headers_case_insensitive():
    verify_worker_request({
        "x-EASEL-workspace-id": WORKSPACE,
        "x-easel-service-TOKEN": TOKEN,
    }, WORKSPACE, TOKEN)


@pytest.mark.parametrize("headers", [
    {},
    {WORKSPACE_HEADER: WORKSPACE},
    {SERVICE_TOKEN_HEADER: TOKEN},
    {WORKSPACE_HEADER: "ws-b", SERVICE_TOKEN_HEADER: TOKEN},
    {WORKSPACE_HEADER: WORKSPACE, SERVICE_TOKEN_HEADER: "wrong"},
    {"X-Easel-Role": "admin", "X-User-Id": "u-member"},
    {WORKSPACE_HEADER: WORKSPACE, SERVICE_TOKEN_HEADER: "\ud800"},
])
def test_verify_worker_request_rejects_invalid_headers(headers):
    with pytest.raises(DomainError) as denied:
        verify_worker_request(headers, WORKSPACE, TOKEN)
    assert denied.value.status == 401


# --------------------------------------------------------------------------
# 内部头清洗与真实下游调用边界
# --------------------------------------------------------------------------

def test_sanitize_forward_headers_strips_untrusted_identity_and_forwarding():
    cleaned = sanitize_forward_headers({
        "Authorization": "Bearer evil",
        "Cookie": "__Host-easel_session=evil",
        "X-Easel-Service-Token": "evil",
        "x-easel-workspace-id": "evil",
        "X-Easel-Role": "admin",
        "Forwarded": "for=203.0.113.1",
        "X-Forwarded-For": "203.0.113.1",
        "X-Forwarded-Proto": "https",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    assert cleaned == {"Content-Type": "application/json",
                       "Accept": "application/json"}


def test_forward_to_worker_rebuilds_headers_at_the_actual_call_boundary():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = {key.lower(): value
                               for key, value in request.headers.items()}
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        response = forward_to_worker(
            client, "POST", "http://worker.internal/api/chat/stream",
            headers={
                "Authorization": "Bearer evil",
                "Cookie": "__Host-easel_session=evil",
                "X-Easel-Role": "admin",
                "X-Forwarded-For": "203.0.113.1",
                "Content-Type": "application/json",
            },
            content=b'{"message":"hi"}',
            params={"stream": "1"},
            workspace_id=WORKSPACE, service_token=TOKEN)
    finally:
        client.close()

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert captured["method"] == "POST"
    assert captured["url"] == "http://worker.internal/api/chat/stream?stream=1"
    assert captured["body"] == b'{"message":"hi"}'
    for stripped in ("authorization", "cookie", "x-easel-role",
                     "x-forwarded-for", "forwarded"):
        assert stripped not in captured["headers"]
    assert captured["headers"]["x-easel-workspace-id"] == WORKSPACE
    assert captured["headers"]["x-easel-service-token"] == TOKEN
    assert captured["headers"]["content-type"] == "application/json"


def test_forward_to_worker_strips_client_defaults_without_mutating_client():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = {key.lower(): value
                               for key, value in request.headers.items()}
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={
            "Authorization": "Bearer client-default",
            "X-Easel-Role": "admin",
            "X-Easel-Service-Token": "client-default-token",
            "Forwarded": "for=203.0.113.9",
            "X-Forwarded-Proto": "https",
            "Accept": "application/json",
        },
        cookies={"client-cookie": "evil"},
    )
    before_headers = dict(client.headers)
    before_cookies = dict(client.cookies)
    before_auth = client.auth
    try:
        response = forward_to_worker(
            client, "POST", "http://worker.internal/api/chat/stream",
            headers={"Content-Type": "application/json",
                     "X-Easel-Role": "explicit-evil"},
            content=b'{"message":"hi"}',
            workspace_id=WORKSPACE, service_token=TOKEN)
    finally:
        client.close()

    assert response.status_code == 200
    headers = captured["headers"]
    for stripped in ("authorization", "cookie", "x-easel-role",
                     "forwarded", "x-forwarded-proto"):
        assert stripped not in headers, stripped
    # 客户端默认的内部 token/workspace 必须被服务端固定值覆盖
    assert headers["x-easel-workspace-id"] == WORKSPACE
    assert headers["x-easel-service-token"] == TOKEN
    assert headers["x-easel-service-token"] != "client-default-token"
    assert headers["content-type"] == "application/json"
    assert headers["accept"] == "application/json"

    # 调用不得通过临时清空/恢复共享 client 状态来规避合并
    assert dict(client.headers) == before_headers
    assert dict(client.cookies) == before_cookies
    assert client.auth is before_auth


def test_forward_to_worker_overrides_client_basic_auth():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = {key.lower(): value
                               for key, value in request.headers.items()}
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler),
                          auth=("user", "pass"))
    try:
        response = forward_to_worker(
            client, "GET", "http://worker.internal/api/upload/limits",
            headers={}, workspace_id=WORKSPACE, service_token=TOKEN)
    finally:
        client.close()

    assert response.status_code == 200
    assert "authorization" not in captured["headers"]
    assert captured["headers"]["x-easel-workspace-id"] == WORKSPACE
    assert captured["headers"]["x-easel-service-token"] == TOKEN


def test_build_worker_headers_replaces_every_incoming_easel_header():
    headers = build_worker_headers({
        "X-Easel-Workspace-Id": "evil",
        "X-Easel-Service-Token": "evil",
        "X-Easel-Role": "admin",
    }, workspace_id=WORKSPACE, service_token=TOKEN)
    lowered = {key.lower(): value for key, value in headers.items()}
    assert lowered["x-easel-workspace-id"] == WORKSPACE
    assert lowered["x-easel-service-token"] == TOKEN
    assert "x-easel-role" not in lowered


# --------------------------------------------------------------------------
# web/app.py worker 入口
# --------------------------------------------------------------------------

@pytest.fixture
def worker_client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "_WORKER_IDENTITY",
                        WorkerIdentity(WORKSPACE, TOKEN), raising=False)
    monkeypatch.setattr(web, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web, "OUTPUTS_DIR", tmp_path / "outputs")
    monkeypatch.setattr(web, "ENV_FILE", tmp_path / ".env")
    (tmp_path / ".env").write_text("", encoding="utf-8")
    with TestClient(web.app, base_url="http://127.0.0.1:7860",
                    client=("203.0.113.10", 50000)) as client:
        yield client


def test_worker_direct_access_requires_internal_credentials(worker_client):
    for headers in (
        {},
        {"Cookie": "__Host-easel_session=fake"},
        {"X-Easel-Role": "admin"},
        {WORKSPACE_HEADER: WORKSPACE, SERVICE_TOKEN_HEADER: "wrong"},
        {WORKSPACE_HEADER: "ws-b", SERVICE_TOKEN_HEADER: TOKEN},
    ):
        response = worker_client.get("/api/upload/limits", headers=headers)
        assert response.status_code == 401, headers

    assert worker_client.get(
        "/api/upload/limits", headers=INTERNAL_HEADERS).status_code == 200


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/env"),
    ("POST", "/api/env/install"),
    ("POST", "/api/settings/harness"),
    ("GET", "/api/settings/harness"),
    ("POST", "/api/settings/chat/transport"),
    ("POST", "/api/gateway/restart"),
    ("POST", "/api/settings/local-agents/enable"),
    ("POST", "/api/admin/managed-config"),
    ("POST", "/api/upload/local"),
    ("POST", "/api/clipper"),
    ("GET", "/api/unknown"),
])
def test_worker_valid_token_cannot_reach_managed_or_unknown_routes(
        worker_client, method, path):
    headers = {**INTERNAL_HEADERS, "X-Easel-Role": "admin"}
    response = worker_client.request(method, path, headers=headers, json={})
    assert response.status_code == 403, (method, path, response.text)


def test_worker_allows_own_workspace_routes(worker_client):
    assert worker_client.get(
        "/api/upload/limits", headers=INTERNAL_HEADERS).status_code == 200
    assert worker_client.get(
        "/api/outputs", headers=INTERNAL_HEADERS).status_code == 200


def test_worker_question_status_uses_the_real_post_contract(
        worker_client, monkeypatch):
    monkeypatch.setitem(web._HARNESS_QUESTIONS, "harness_pending",
                        {"status": "pending"})

    response = worker_client.post(
        "/api/chat/question/status",
        headers=INTERNAL_HEADERS,
        json={"questionIds": ["harness_pending", "harness_missing"]},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "questions": {
            "harness_pending": {"status": "pending"},
            "harness_missing": {"status": "not_found"},
        },
    }

    # 没有真实 GET 处理器：有凭证也必须默认拒绝
    assert worker_client.get(
        "/api/chat/question/status", headers=INTERNAL_HEADERS).status_code == 403
    # 缺凭证仍先 401
    assert worker_client.post(
        "/api/chat/question/status",
        json={"questionIds": []}).status_code == 401


def test_worker_upload_cannot_escape_into_managed_acp_file(worker_client, tmp_path):
    harness = tmp_path / "outputs" / "_harness"
    harness.mkdir(parents=True)
    sentinel = harness / "acp.json"
    sentinel.write_bytes(b'{"command":["original"]}')

    response = worker_client.post(
        "/api/upload",
        headers=INTERNAL_HEADERS,
        data={"sessionId": "../../_harness/acp.json"},
        files={"files": ("../../_harness/acp.json",
                         b'{"command":["evil"]}', "application/json")},
    )
    assert response.status_code == 200, response.text
    uploaded = response.json()["files"][0]
    assert uploaded["path"].startswith("_inbox/")
    assert sentinel.read_bytes() == b'{"command":["original"]}'
    assert sorted(path.name for path in harness.iterdir()) == ["acp.json"]


def test_worker_managed_write_cannot_change_env_or_harness(worker_client, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ORIGINAL=1\n", encoding="utf-8")
    harness = tmp_path / "outputs" / "_harness"
    harness.mkdir(parents=True)
    sentinel = harness / "acp.json"
    sentinel.write_bytes(b'{"command":["original"]}')

    response = worker_client.post(
        "/api/env",
        headers={**INTERNAL_HEADERS, "X-Easel-Role": "admin"},
        json={"role": "admin", "env": {"EVIL": "1"}, "argv": ["--danger"]},
    )
    assert response.status_code == 403
    assert env_file.read_text(encoding="utf-8") == "ORIGINAL=1\n"
    assert sentinel.read_bytes() == b'{"command":["original"]}'


def test_worker_mode_without_identity_fails_at_startup():
    env = os.environ.copy()
    env[SERVICE_MODE_ENV] = "worker"
    env.pop(WORKSPACE_ID_ENV, None)
    env.pop(SERVICE_TOKEN_ENV, None)
    env["PYTHONIOENCODING"] = "utf-8"

    result = subprocess.run(
        [sys.executable, "-c", "import web.app"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120)

    assert result.returncode != 0, "worker 缺身份配置必须启动失败"
    assert WORKSPACE_ID_ENV in result.stderr or SERVICE_TOKEN_ENV in result.stderr
    assert TOKEN not in result.stderr


def test_local_mode_does_not_require_internal_headers(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "_WORKER_IDENTITY", None, raising=False)
    monkeypatch.setattr(web, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web, "OUTPUTS_DIR", tmp_path / "outputs")
    monkeypatch.setattr(web, "ENV_FILE", tmp_path / ".env")
    (tmp_path / ".env").write_text("", encoding="utf-8")

    with TestClient(web.app, base_url="http://127.0.0.1:7860",
                    client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/upload/limits").status_code == 200
