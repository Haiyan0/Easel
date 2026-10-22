"""Task 3 路由权限：默认拒绝的 method+path 分类与角色边界。"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.policy import (
    ADMIN_READ,
    ADMIN_WRITE,
    FORBIDDEN,
    MANAGED_READ,
    MANAGED_WRITE,
    PUBLIC,
    RouteDecision,
    TASK,
    UNKNOWN,
    WORKSPACE_READ,
    WORKSPACE_WRITE,
    authorize,
    public_route,
    worker_allowed,
)
from easel.control.store import Store
from easel.control.types import Identity

ORIGIN = "https://easel.test"
MEMBER = Identity("u-member", "w-member", "member")
ADMIN = Identity("u-admin", "w-admin", "admin")

SENSITIVE_WRITES = [
    ("POST", "/api/settings/harness"),
    ("POST", "/api/settings/harness/models/codex"),
    ("POST", "/api/settings/chat/transport"),
    ("POST", "/api/env"),
    ("POST", "/api/env/install"),
    ("POST", "/api/gateway/restart"),
    ("POST", "/api/settings/local-agents/enable"),
    ("POST", "/api/admin/managed-config"),
    ("PUT", "/api/admin/managed-config"),
]


@pytest.mark.parametrize("method,path", SENSITIVE_WRITES)
def test_member_managed_writes_are_forbidden(method, path):
    decision = authorize(MEMBER, method, path)
    assert decision.kind == FORBIDDEN


@pytest.mark.parametrize("method,path", SENSITIVE_WRITES)
def test_admin_managed_writes_are_classified(method, path):
    decision = authorize(ADMIN, method, path)
    assert decision.kind in {MANAGED_WRITE, ADMIN_WRITE}
    assert decision.kind != FORBIDDEN


@pytest.mark.parametrize("actor", [MEMBER, ADMIN])
@pytest.mark.parametrize("method,path", [
    ("GET", "/api/unclassified-new-feature"),
    ("POST", "/api/unclassified-new-feature"),
    ("GET", "/api/env"),
    ("PUT", "/api/settings/harness"),
    ("POST", "/api/env/"),
    ("POST", "/api/env/install/"),
    ("POST", "/api/settings/harness/models/codex/"),
    ("POST", "/api/upload/local/"),
    ("GET", "/api/settings/harness/extra"),
    ("POST", "/API/ENV"),
    ("PATCH", "/api/sessions/s1/extra"),
])
def test_unknown_and_path_variants_never_match(actor, method, path):
    assert authorize(actor, method, path).kind == UNKNOWN


@pytest.mark.parametrize("actor", [MEMBER, ADMIN])
def test_upload_local_and_clipper_are_denied_for_everyone(actor):
    assert authorize(actor, "POST", "/api/upload/local").kind == FORBIDDEN
    assert authorize(actor, "POST", "/api/clipper").kind == FORBIDDEN


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/health"),
    ("POST", "/api/auth/exchange"),
    ("GET", "/"),
    ("GET", "/onepage"),
    ("GET", "/assets/app.js"),
    ("GET", "/static/app.css"),
])
def test_public_routes_are_exact(method, path):
    assert public_route(method, path) is True


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/health"),
    ("GET", "/api/auth/exchange"),
    ("GET", "/api/unknown"),
    ("GET", "/api/health/"),
    ("POST", "/api/auth/me"),
])
def test_non_public_routes_are_not_public(method, path):
    assert public_route(method, path) is False


def test_route_decision_extracts_candidate_resource_only():
    session = authorize(MEMBER, "GET", "/api/chat/last/sess-1")
    assert session == RouteDecision(TASK, "session", "sess-1")

    turn = authorize(MEMBER, "GET", "/api/chat/jobs/turn-1/stream")
    assert turn == RouteDecision(TASK, "turn", "turn-1")

    messages = authorize(MEMBER, "GET", "/api/sessions/s1/messages")
    assert messages.resource_type == "session" and messages.resource_id == "s1"

    output = authorize(MEMBER, "DELETE", "/api/output/project/file.txt")
    assert output == RouteDecision(WORKSPACE_WRITE, "output", "project/file.txt")


def test_member_redacted_read_versus_admin_only_read():
    assert authorize(MEMBER, "GET", "/api/settings/harness").kind == MANAGED_READ
    assert authorize(MEMBER, "GET", "/api/settings/local-agents").kind == FORBIDDEN
    assert authorize(ADMIN, "GET", "/api/settings/local-agents").kind == ADMIN_READ


def test_workspace_routes_allowed_for_both_roles():
    for actor in (MEMBER, ADMIN):
        assert authorize(actor, "GET", "/api/outputs").kind == WORKSPACE_READ
        assert authorize(actor, "POST", "/api/upload").kind == WORKSPACE_WRITE
        assert authorize(actor, "POST", "/api/chat/stream").kind == TASK


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/health"),
    ("GET", "/api/upload/limits"),
    ("POST", "/api/upload"),
    ("POST", "/api/chat/stream"),
    ("GET", "/api/outputs"),
])
def test_worker_allows_public_and_workspace_routes(method, path):
    assert worker_allowed(method, path) is True


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/env"),
    ("POST", "/api/env/install"),
    ("POST", "/api/settings/harness"),
    ("GET", "/api/settings/harness"),
    ("GET", "/api/settings/models"),
    ("GET", "/api/settings/local-agents"),
    ("POST", "/api/upload/local"),
    ("POST", "/api/clipper"),
    ("GET", "/api/unknown"),
    ("GET", "/api/auth/me"),
    ("POST", "/api/admin/managed-config"),
    ("POST", "/api/env/"),
])
def test_worker_denies_managed_control_and_unknown_routes(method, path):
    assert worker_allowed(method, path) is False


def test_head_is_classified_as_read():
    assert public_route("HEAD", "/api/health") is True
    assert authorize(MEMBER, "HEAD", "/api/outputs").kind == WORKSPACE_READ
    assert authorize(MEMBER, "HEAD", "/api/env").kind == UNKNOWN


def test_question_status_uses_the_real_post_contract():
    assert authorize(MEMBER, "POST", "/api/chat/question/status").kind == TASK
    assert worker_allowed("POST", "/api/chat/question/status") is True
    # 没有真实 GET 处理器：不得为旧错误登记保留放行
    assert authorize(MEMBER, "GET", "/api/chat/question/status").kind == UNKNOWN
    assert worker_allowed("GET", "/api/chat/question/status") is False


# --------------------------------------------------------------------------
# control HTTP 边界
# --------------------------------------------------------------------------

class MutableClock:
    def __init__(self, now: float = 1000.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@contextmanager
def control_client(tmp_path, clock=None):
    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        auth = AuthService(store, clock=clock or MutableClock())
        app = create_app(store=store, auth=auth,
                         config=ControlConfig(public_origin=ORIGIN))
        with TestClient(app, base_url=ORIGIN) as client:
            yield client, store, auth, admin, member
    finally:
        store.close()


def login(client, auth, admin, user) -> str:
    invite = auth.create_invite(admin, user.user_id)
    response = client.post("/api/auth/exchange", json={"token": invite},
                           headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    return response.json()["csrfToken"]


def test_control_private_paths_require_authentication_first(tmp_path):
    with control_client(tmp_path) as (client, store, auth, admin, member):
        for method, path in [
            ("GET", "/api/auth/me"),
            ("GET", "/api/env"),
            ("GET", "/api/unknown"),
            ("GET", "/docs"),
            ("GET", "/openapi.json"),
            ("GET", "/api/sessions"),
            ("POST", "/api/env"),
            ("POST", "/api/unknown"),
            ("POST", "/api/auth/logout"),
        ]:
            response = client.request(method, path, headers={"Origin": ORIGIN}, json={})
            assert response.status_code == 401, (method, path, response.text)
            assert response.json()["code"] == "unauthorized"

        # 浏览器提交的身份/worker 凭证头不能替代登录
        forged = {
            "X-Easel-Role": "admin",
            "X-Easel-Workspace-Id": member.workspace_id,
            "X-Easel-Service-Token": "forged-service-token",
            "X-User-Id": admin.user_id,
            "X-Workspace-Id": admin.workspace_id,
        }
        assert client.get("/api/auth/me", headers=forged).status_code == 401


def test_member_sensitive_writes_and_admin_routes_are_denied(tmp_path):
    with control_client(tmp_path) as (client, store, auth, admin, member):
        csrf = login(client, auth, admin, member)
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}

        for method, path in SENSITIVE_WRITES:
            response = client.request(method, path, headers=headers, json={})
            assert response.status_code == 403, (method, path, response.text)
            assert response.json()["code"] == "forbidden"

        assert client.get("/api/settings/local-agents").status_code == 403
        assert client.get("/api/settings/harness").status_code == 503
        assert client.get("/api/settings/models").status_code == 503
        assert client.get("/api/sessions").status_code == 200
        assert client.get("/api/unknown").status_code == 403

        # 伪造 admin 身份头仍不能升级
        escalated = {**headers, "X-Easel-Role": "admin",
                     "X-User-Id": admin.user_id}
        response = client.post("/api/env", headers=escalated, json={})
        assert response.status_code == 403


def test_member_cannot_execute_managed_write_with_forged_fields(tmp_path):
    with control_client(tmp_path) as (client, store, auth, admin, member):
        csrf = login(client, auth, admin, member)
        response = client.post(
            "/api/env",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf,
                     "X-Easel-Role": "admin", "X-User-Id": admin.user_id},
            json={
                "role": "admin",
                "user_id": admin.user_id,
                "workspace_id": admin.workspace_id,
                "env": {"EASEL_CODE_ROOT": "/evil"},
                "argv": ["--danger"],
            })
        assert response.status_code == 403
        assert response.json()["code"] == "forbidden"


def test_admin_managed_writes_are_unavailable_not_success(tmp_path):
    with control_client(tmp_path) as (client, store, auth, admin, member):
        csrf = login(client, auth, admin, admin)
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}

        for method, path in SENSITIVE_WRITES:
            response = client.request(method, path, headers=headers, json={})
            assert response.status_code == 503, (method, path, response.text)
            assert response.json()["code"] == "capability_unavailable"

        assert client.get("/api/settings/harness").status_code == 503
        assert client.get("/api/settings/local-agents").status_code == 503
        assert client.get("/api/unknown").status_code == 403
        assert client.post("/api/unknown", headers=headers, json={}).status_code == 403
        assert client.post("/api/upload/local", headers=headers, json={}).status_code == 403
        assert client.post("/api/clipper", headers=headers, json={}).status_code == 403


def test_control_write_guard_and_no_session_side_effect_on_failure(tmp_path):
    clock = MutableClock()
    with control_client(tmp_path, clock) as (client, store, auth, admin, member):
        csrf = login(client, auth, admin, member)
        clock.advance(10)

        target = "/api/settings/models/save"
        assert client.post(target, json={},
                           headers={"Origin": ORIGIN}).status_code == 403
        assert client.post(target, json={}, headers={
            "Origin": ORIGIN, "X-CSRF-Token": "wrong"}).status_code == 403
        assert client.post(target, json={}, headers={
            "Origin": "https://evil.test", "X-CSRF-Token": csrf}).status_code == 403

        last_seen = store._connection.execute(
            "SELECT MAX(last_seen_at) FROM logins").fetchone()[0]
        assert last_seen == 1000, "失败的写请求不得改变登录活动时间"

        assert client.get("/api/auth/me").status_code == 200
        last_seen = store._connection.execute(
            "SELECT MAX(last_seen_at) FROM logins").fetchone()[0]
        assert last_seen == 1010, "成功的读请求应滑动更新活动时间"

        assert client.post(target, json={}, headers={
            "Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code == 503


def test_public_routes_are_exact_at_http_boundary(tmp_path):
    with control_client(tmp_path) as (client, store, auth, admin, member):
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/health/").status_code == 401
        assert client.post("/api/health", json={},
                           headers={"Origin": ORIGIN}).status_code == 401
        assert client.get("/api/auth/exchange").status_code == 401


def test_question_status_post_is_classified_but_not_implemented(tmp_path):
    with control_client(tmp_path) as (client, store, auth, admin, member):
        csrf = login(client, auth, admin, member)
        payload = {"questionIds": ["harness_missing"]}

        response = client.post(
            "/api/chat/question/status", json=payload,
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "capability_unavailable"

        missing_csrf = client.post(
            "/api/chat/question/status", json=payload,
            headers={"Origin": ORIGIN})
        assert missing_csrf.status_code == 403

        # 没有 GET 处理器，也不得被策略放行
        assert client.get("/api/chat/question/status").status_code == 403
