"""Task 2 HTTP 边界：兑换 Cookie、me、CSRF 注销、限速与真实线程。"""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.store import Store

ORIGIN = "https://easel.test"
COOKIE = "__Host-easel_session"


class MutableClock:
    def __init__(self, now: float = 1000.0):
        self.now = float(now)
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


@contextmanager
def control_api(tmp_path, clock=None):
    """真实 Store + 真实 TestClient；https base_url 以验证 Secure Cookie。"""
    store = Store.connect(tmp_path / "control.sqlite3")
    clock = clock or MutableClock()
    try:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        auth = AuthService(store, clock=clock)
        app = create_app(store=store, auth=auth,
                         config=ControlConfig(public_origin=ORIGIN))
        with TestClient(app, base_url=ORIGIN) as client:
            invite = auth.create_invite(admin, member.user_id)
            yield client, store, auth, admin, member, invite
    finally:
        store.close()


def exchange(client: TestClient, token: str, *, origin: str | None = ORIGIN,
             headers: dict | None = None):
    request_headers = dict(headers or {})
    if origin is not None:
        request_headers["Origin"] = origin
    return client.post("/api/auth/exchange", json={"token": token},
                       headers=request_headers)


# --------------------------------------------------------------------------
# 兑换、Cookie 与 me
# --------------------------------------------------------------------------

def test_exchange_sets_host_cookie_and_me_returns_sanitized_identity(tmp_path):
    with control_api(tmp_path) as (client, store, auth, admin, member, invite):
        response = exchange(client, invite)
        assert response.status_code == 200

        cookie_header = response.headers["set-cookie"]
        assert cookie_header.startswith(f"{COOKIE}=")
        assert "Secure" in cookie_header
        assert "HttpOnly" in cookie_header
        assert "SameSite=lax" in cookie_header
        assert "Path=/" in cookie_header
        assert "Domain=" not in cookie_header

        session_token = response.cookies.get(COOKIE)
        assert session_token, "响应必须真实设置 Cookie"
        body = response.json()
        assert body["userId"] == member.user_id
        assert body["workspaceId"] == member.workspace_id
        assert body["role"] == "member"
        assert isinstance(body["capabilities"], list) and body["capabilities"]
        assert body["csrfToken"]
        assert session_token not in response.text, "响应体不得回传登录 token"

        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.headers["cache-control"] == "no-store"
        assert me.json() == body, "me 与兑换应返回同一脱敏视图"
        assert session_token not in me.text


def test_exchange_requires_exact_origin(tmp_path):
    with control_api(tmp_path) as (client, store, auth, admin, member, invite):
        for origin in (None, "https://evil.test", "https://easel.test.evil",
                       "http://easel.test"):
            response = exchange(client, invite, origin=origin)
            assert response.status_code == 403, origin
        # Origin 失败不得消耗邀请
        assert exchange(client, invite).status_code == 200


def test_me_rejects_missing_and_bad_cookie_without_leaking_details(tmp_path):
    with control_api(tmp_path) as (client, store, auth, admin, member, invite):
        missing = client.get("/api/auth/me")
        assert missing.status_code == 401
        assert missing.json()["code"] == "unauthorized"
        assert missing.headers["cache-control"] == "no-store"

        bad = client.get("/api/auth/me",
                         headers={"Cookie": f"{COOKIE}=not-a-real-token"})
        assert bad.status_code == 401
        assert bad.json()["code"] == "unauthorized"


# --------------------------------------------------------------------------
# 注销与 CSRF
# --------------------------------------------------------------------------

def test_logout_requires_origin_and_csrf_before_revoking(tmp_path):
    with control_api(tmp_path) as (client, store, auth, admin, member, invite):
        response = exchange(client, invite)
        csrf = response.json()["csrfToken"]
        session_token = response.cookies.get(COOKIE)

        missing_csrf = client.post("/api/auth/logout", headers={"Origin": ORIGIN})
        assert missing_csrf.status_code == 403
        assert "set-cookie" not in missing_csrf.headers
        assert client.get("/api/auth/me").status_code == 200

        wrong_csrf = client.post(
            "/api/auth/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": "wrong-token"})
        assert wrong_csrf.status_code == 403
        assert "set-cookie" not in wrong_csrf.headers
        assert client.get("/api/auth/me").status_code == 200

        wrong_origin = client.post(
            "/api/auth/logout",
            headers={"Origin": "https://evil.test", "X-CSRF-Token": csrf})
        assert wrong_origin.status_code == 403
        assert client.get("/api/auth/me").status_code == 200

        logged_out = client.post(
            "/api/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
        assert logged_out.status_code == 204
        clear_header = logged_out.headers["set-cookie"]
        assert clear_header.startswith(f'{COOKIE}=""')
        assert "Max-Age=0" in clear_header
        assert "Path=/" in clear_header
        assert client.get("/api/auth/me").status_code == 401

        # 旧 token 在全新请求中不能再认证
        replay = client.get("/api/auth/me",
                            headers={"Cookie": f"{COOKIE}={session_token}"})
        assert replay.status_code == 401


def test_revoke_all_role_change_and_disable_are_visible_through_http(tmp_path):
    with control_api(tmp_path) as (client, store, auth, admin, member, invite):
        assert exchange(client, invite).status_code == 200
        assert client.get("/api/auth/me").json()["role"] == "member"

        store._connection.execute(
            "UPDATE users SET role = 'admin' WHERE id = ?", (member.user_id,))
        assert client.get("/api/auth/me").json()["role"] == "admin", \
            "角色必须每次来自数据库"

        auth.revoke_all(member.user_id)
        assert client.get("/api/auth/me").status_code == 401

        # 新登录后禁用用户，立即失效
        new_invite = auth.create_invite(admin, member.user_id)
        assert exchange(client, new_invite).status_code == 200
        assert client.get("/api/auth/me").status_code == 200
        store.disable_user(member.user_id)
        assert client.get("/api/auth/me").status_code == 401


def test_logout_revokes_only_the_current_login(tmp_path):
    with control_api(tmp_path) as (client, store, auth, admin, member, invite):
        first = exchange(client, invite)
        first_cookie = first.cookies.get(COOKIE)

        second_invite = auth.create_invite(admin, member.user_id)
        second = exchange(client, second_invite)
        second_csrf = second.json()["csrfToken"]

        logged_out = client.post(
            "/api/auth/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": second_csrf})
        assert logged_out.status_code == 204
        assert client.get("/api/auth/me").status_code == 401

        client.cookies.clear()
        still_valid = client.get(
            "/api/auth/me", headers={"Cookie": f"{COOKIE}={first_cookie}"})
        assert still_valid.status_code == 200, "注销只撤销当前登录"


def test_exchange_does_not_accept_token_in_url(tmp_path):
    with control_api(tmp_path) as (client, store, auth, admin, member, invite):
        response = client.post(
            f"/api/auth/exchange?token={invite}", headers={"Origin": ORIGIN})
        assert response.status_code == 401
        assert exchange(client, invite).status_code == 200, "URL 失败不得消耗邀请"


def test_exchange_rejects_oversized_and_malformed_bodies(tmp_path):
    with control_api(tmp_path) as (client, store, auth, admin, member, invite):
        oversized = client.post(
            "/api/auth/exchange", content=b"x" * 5000,
            headers={"Origin": ORIGIN, "Content-Type": "application/json"})
        assert oversized.status_code == 401

        malformed = client.post(
            "/api/auth/exchange", content=b"{",
            headers={"Origin": ORIGIN, "Content-Type": "application/json"})
        assert malformed.status_code == 401

        assert exchange(client, invite).status_code == 200, "失败请求不得消耗邀请"


# --------------------------------------------------------------------------
# 失败限速
# --------------------------------------------------------------------------

def test_exchange_failures_are_uniform_401_then_rate_limited(tmp_path):
    clock = MutableClock()
    with control_api(tmp_path, clock) as (client, store, auth, admin, member, invite):
        for index in range(10):
            response = exchange(
                client, "invalid-invite-token",
                headers={"X-Forwarded-For": f"203.0.113.{index}"})
            assert response.status_code == 401
            assert response.json()["code"] == "unauthorized"

        blocked = exchange(
            client, "invalid-invite-token",
            headers={"X-Forwarded-For": "198.51.100.9"})
        assert blocked.status_code == 429, "伪造转发头不能绕过来源约束"
        assert blocked.json()["code"] == "rate_limited"
        assert int(blocked.headers["retry-after"]) == 600
        assert blocked.headers["cache-control"] == "no-store"

        # 窗口内合法邀请也不放行（限速先于兑换）
        assert exchange(client, invite).status_code == 429

        clock.advance(600)
        assert exchange(client, "invalid-invite-token").status_code == 401
        assert exchange(client, invite).status_code == 200


def test_rate_limit_is_shared_across_app_instances_and_token_rotation(tmp_path):
    clock = MutableClock()
    path = tmp_path / "control.sqlite3"
    store = Store.connect(path)
    try:
        store.create_user("Owner", "admin")
        store.create_user("Member")
        config = ControlConfig(public_origin=ORIGIN)
        first_app = create_app(
            store=store, auth=AuthService(store, clock=clock), config=config)
        second_app = create_app(
            store=store, auth=AuthService(store, clock=clock), config=config)
        with TestClient(first_app, base_url=ORIGIN) as first, \
                TestClient(second_app, base_url=ORIGIN) as second:
            for _ in range(5):
                assert exchange(first, "shared-invalid").status_code == 401
            for _ in range(5):
                assert exchange(second, "shared-invalid").status_code == 401

            assert exchange(second, "shared-invalid").status_code == 429
            # 换一个猜测 token 不能获得新额度：来源维度也已达到上限
            assert exchange(second, "rotated-invalid").status_code == 429
    finally:
        store.close()


def test_concurrent_invalid_exchanges_are_atomically_capped(tmp_path):
    """同一来源 20 个并发错误请求：恰好 10 个 401，其余 429。"""
    clock = MutableClock()
    with control_api(tmp_path, clock) as (client, store, auth, admin, member, invite):
        barrier = threading.Barrier(20, timeout=30)
        results = []
        lock = threading.Lock()

        def attempt(index):
            barrier.wait()
            response = exchange(client, f"concurrent-http-{index}")
            with lock:
                results.append(
                    (response.status_code, response.headers.get("retry-after")))

        threads = [threading.Thread(target=attempt, args=(index,)) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive()

        statuses = [status for status, _ in results]
        assert statuses.count(401) == 10
        assert statuses.count(429) == 10
        retry_values = [int(value) for status, value in results if status == 429]
        assert retry_values and all(0 < value <= 600 for value in retry_values)
        assert store._connection.execute("SELECT COUNT(*) FROM logins").fetchone()[0] == 0

        # 合法邀请在限速期间不得被消耗；窗口过后恢复
        assert exchange(client, invite).status_code == 429
        clock.advance(600)
        assert exchange(client, invite).status_code == 200


def test_concurrent_invalid_exchanges_across_two_apps_share_one_cap(tmp_path):
    """两个 app 连接同一库并发失败请求：共享同一个十次上限。"""
    clock = MutableClock()
    path = tmp_path / "control.sqlite3"
    store = Store.connect(path)
    try:
        store.create_user("Owner", "admin")
        store.create_user("Member")
        config = ControlConfig(public_origin=ORIGIN)
        first_app = create_app(
            store=store, auth=AuthService(store, clock=clock), config=config)
        second_app = create_app(
            store=store, auth=AuthService(store, clock=clock), config=config)
        with TestClient(first_app, base_url=ORIGIN) as first, \
                TestClient(second_app, base_url=ORIGIN) as second:
            barrier = threading.Barrier(20, timeout=30)
            statuses = []
            lock = threading.Lock()

            def attempt(index):
                barrier.wait()
                client = first if index % 2 == 0 else second
                response = exchange(client, f"two-app-invalid-{index}")
                with lock:
                    statuses.append(response.status_code)

            threads = [threading.Thread(target=attempt, args=(index,))
                       for index in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
                assert not thread.is_alive()

            assert statuses.count(401) == 10
            assert statuses.count(429) == 10
    finally:
        store.close()


MALFORMED_BODIES = [
    pytest.param(b'{"token": "\\ud800"}', id="lone-high-surrogate"),
    pytest.param(b'{"token": "\\udfff"}', id="lone-low-surrogate"),
    pytest.param(b'{"token": null}', id="null"),
    pytest.param(b'{"token": 123}', id="number"),
    pytest.param(b'{"token": []}', id="array"),
    pytest.param(b'{"token": {}}', id="object"),
    pytest.param(b'{"token": ""}', id="empty"),
    pytest.param(b'{"token": "' + b"x" * 600 + b'"}', id="overlong"),
]


@pytest.mark.parametrize("body", MALFORMED_BODIES)
def test_malformed_exchange_bodies_are_401_without_side_effects(tmp_path, body):
    with control_api(tmp_path) as (client, store, auth, admin, member, invite):
        response = client.post(
            "/api/auth/exchange", content=body,
            headers={"Origin": ORIGIN, "Content-Type": "application/json"})
        assert response.status_code == 401, response.text
        assert response.json()["code"] == "unauthorized"
        assert "set-cookie" not in response.headers
        assert store._connection.execute("SELECT COUNT(*) FROM logins").fetchone()[0] == 0
        assert exchange(client, invite).status_code == 200, "失败请求不得消耗有效邀请"


def test_repeated_malformed_surrogate_is_counted_and_rate_limited(tmp_path):
    clock = MutableClock()
    with control_api(tmp_path, clock) as (client, store, auth, admin, member, invite):
        body = b'{"token": "\\ud800"}'
        for _ in range(10):
            response = client.post(
                "/api/auth/exchange", content=body,
                headers={"Origin": ORIGIN, "Content-Type": "application/json"})
            assert response.status_code == 401

        blocked = client.post(
            "/api/auth/exchange", content=body,
            headers={"Origin": ORIGIN, "Content-Type": "application/json"})
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) == 600
        assert exchange(client, invite).status_code == 429

        clock.advance(600)
        assert exchange(client, invite).status_code == 200


# --------------------------------------------------------------------------
# 公共 Origin 配置
# --------------------------------------------------------------------------

@pytest.mark.parametrize("origin", [
    "http://easel.test",
    "https://",
    "https:///path",
    "https://user:pass@easel.test",
    "https://easel.test/path",
    "https://easel.test?query=1",
    "https://easel.test#fragment",
    " https://easel.test",
    "https://easel.test ",
    "https://easel test",
    "https://easel.test\\path",
    "https://.easel.test",
    "https://easel..test",
    "https://easel.test:notaport",
    "https://easel.test:70000",
    "https://easel.test:",
    "",
])
def test_control_config_rejects_invalid_public_origins(origin):
    with pytest.raises(ValueError):
        ControlConfig(public_origin=origin)


@pytest.mark.parametrize("origin,expected", [
    ("https://easel.test", "https://easel.test"),
    ("HTTPS://EASEL.TEST/", "https://easel.test"),
    ("https://easel.test:443", "https://easel.test"),
    ("https://easel.test:8443", "https://easel.test:8443"),
    ("https://127.0.0.1", "https://127.0.0.1"),
    ("https://[::1]:8443", "https://[::1]:8443"),
])
def test_control_config_normalizes_valid_https_origins(origin, expected):
    assert ControlConfig(public_origin=origin).public_origin == expected


# --------------------------------------------------------------------------
# 公开路由与线程/lifespan
# --------------------------------------------------------------------------

def test_health_is_minimal_and_unknown_routes_do_not_fall_back(tmp_path):
    with control_api(tmp_path) as (client, store, auth, admin, member, invite):
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        for path in ("/api/sessions", "/docs", "/openapi.json",
                     "/api/admin/bootstrap", "/api/unknown"):
            response = client.get(path)
            assert response.status_code == 404, path
            assert response.json()["code"] == "not_found"
            assert response.json()["request_id"]
        assert client.post("/api/admin/bootstrap").status_code == 404


def test_api_serializes_database_access_on_its_own_thread(tmp_path):
    with control_api(tmp_path) as (client, store, auth, admin, member, invite):
        assert exchange(client, invite).status_code == 200

        def read_me(_index: int) -> int:
            return client.get("/api/auth/me").status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(read_me, range(24)))
        assert statuses == [200] * 24, "并发请求不得触发跨线程 SQLite 错误"


def test_lifespan_closes_the_store_it_owns(tmp_path):
    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        app = create_app(store=store, auth=AuthService(store),
                         config=ControlConfig(public_origin=ORIGIN))
        with TestClient(app, base_url=ORIGIN) as client:
            assert client.get("/api/health").status_code == 200
            app_store = app.state.control_store
        with pytest.raises(sqlite3.ProgrammingError):
            app_store._connection.execute("SELECT 1")
    finally:
        store.close()
