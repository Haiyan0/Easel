"""永久登录码、跨电脑挤号与旧库升级的真实数据库回归。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.store import Store
from easel.control.types import DomainError


def test_default_code_is_permanent_and_reuses_original_workspace(tmp_path):
    now = [1000]
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        auth = AuthService(store, clock=lambda: now[0])
        _, code, expiry = auth.issue_invite(admin, member.user_id)
        assert expiry == 0
        old = auth.exchange(code)
        now[0] += 365 * 86400 * 20
        new = auth.exchange(code)
        assert auth.authenticate(new.token) == member
        assert new.id != old.id and new.token != old.token
        assert store._connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
        assert store._connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 2


def test_login_with_another_code_kicks_all_previous_sessions_for_same_account(tmp_path):
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        other = store.create_user("Other")
        auth = AuthService(store, clock=lambda: 1000)
        code_a = auth.create_invite(admin, member.user_id)
        code_b = auth.create_invite(admin, member.user_id)
        old = auth.exchange(code_a)
        unrelated = auth.exchange(auth.create_invite(admin, other.user_id))
        new = auth.exchange(code_b)
        with pytest.raises(DomainError) as error:
            auth.authenticate(old.token)
        assert error.value.status == 401
        assert auth.authenticate(new.token) == member
        assert auth.authenticate(unrelated.token) == other
        with pytest.raises(DomainError):
            auth.exchange("invalid-code")
        assert auth.authenticate(new.token) == member


def test_used_code_can_be_revoked_and_disabled_account_code_does_not_revive(tmp_path):
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        auth = AuthService(store, clock=lambda: 1000)
        invite_id, code, _ = auth.issue_invite(admin, member.user_id)
        auth.exchange(code)
        assert store.admin_revoke_invite(admin, invite_id)
        assert not store.admin_revoke_invite(admin, invite_id)
        with pytest.raises(DomainError):
            auth.exchange(code)
        other = auth.create_invite(admin, member.user_id)
        auth.exchange(other)
        store.admin_set_user_disabled(admin, member.user_id, True)
        store.admin_set_user_disabled(admin, member.user_id, False)
        with pytest.raises(DomainError):
            auth.exchange(other)


def test_concurrent_code_logins_leave_exactly_one_valid_session(tmp_path):
    path = tmp_path / "control.sqlite3"
    with Store.open(path) as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        code = AuthService(store, clock=lambda: 1000).create_invite(admin, member.user_id)
    barrier = threading.Barrier(2, timeout=10)

    def login(_):
        with Store.open(path) as store:
            barrier.wait()
            return AuthService(store, clock=lambda: 1000).exchange(code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        sessions = list(pool.map(login, range(2)))
    with Store.open(path) as store:
        auth = AuthService(store, clock=lambda: 1000)
        valid = []
        for session in sessions:
            try:
                valid.append(auth.authenticate(session.token))
            except DomainError as error:
                assert error.status == 401
        assert valid == [member]
        assert store._connection.execute(
            "SELECT COUNT(*) FROM logins WHERE revoked_at IS NULL").fetchone()[0] == 1


def test_v15_used_expired_codes_upgrade_but_revoked_and_disabled_codes_stay_invalid(tmp_path):
    path = tmp_path / "control.sqlite3"
    with Store.open(path) as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        disabled = store.create_user("Disabled")
        auth = AuthService(store, clock=lambda: 1000)
        used = auth.create_invite(admin, member.user_id)
        auth.exchange(used)
        revoked = auth.create_invite(admin, member.user_id)
        auth.revoke_invite(revoked)
        disabled_code = auth.create_invite(admin, disabled.user_id)
        store.disable_user(disabled.user_id)
        # 模拟旧 v15 中过期且已兑换的码；撤销标记保留。
        store._connection.execute("UPDATE invites SET expires_at = 1001, redeemed_at = 1000")
        store._connection.execute("PRAGMA user_version = 15")
        store._connection.execute("DELETE FROM schema_migrations WHERE version > 15")
    with Store.open(path) as store:
        auth = AuthService(store, clock=lambda: 10_000_000)
        assert auth.authenticate(auth.exchange(used).token) == member
        with pytest.raises(DomainError):
            auth.exchange(revoked)
        store._connection.execute("UPDATE users SET disabled_at = NULL WHERE id = ?", (disabled.user_id,))
        with pytest.raises(DomainError):
            auth.exchange(disabled_code)


def test_two_browsers_reuse_code_and_old_cookie_gets_401(tmp_path):
    origin = "https://easel.test"
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        auth = AuthService(store, clock=lambda: 1000)
        code = auth.create_invite(admin, member.user_id)
        app = create_app(store=store, auth=auth, config=ControlConfig(public_origin=origin))
        with TestClient(app, base_url=origin) as first:
            # 共用一个 app lifespan，仅隔离两个浏览器的 Cookie 容器。
            second = TestClient(app, base_url=origin)
            try:
                assert first.post("/api/auth/exchange", json={"token": code}, headers={"Origin": origin}).status_code == 200
                assert second.post("/api/auth/exchange", json={"token": code}, headers={"Origin": "https://wrong.test"}).status_code == 403
                assert first.get("/api/auth/me").status_code == 200
                assert second.post("/api/auth/exchange", json={"token": code}, headers={"Origin": origin}).status_code == 200
                assert first.get("/api/auth/me").status_code == 401
                assert second.get("/api/auth/me").json()["workspaceId"] == member.workspace_id
            finally:
                second.close()


def test_v15_upgrade_keeps_only_latest_existing_login(tmp_path):
    path = tmp_path / "control.sqlite3"
    with Store.open(path) as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        auth = AuthService(store, clock=lambda: 1000)
        code = auth.create_invite(admin, member.user_id)
        first = auth.exchange(code)
        last = auth.exchange(code)
        # 旧版允许多设备同时登录，重建该历史状态后再运行 v16 迁移。
        store._connection.execute("UPDATE logins SET revoked_at = NULL")
        assert store._connection.execute("SELECT COUNT(*) FROM logins WHERE revoked_at IS NULL").fetchone()[0] == 2
        store._connection.execute("PRAGMA user_version = 15")
        store._connection.execute("DELETE FROM schema_migrations WHERE version > 15")
    with Store.open(path) as store:
        auth = AuthService(store, clock=lambda: 1001)
        with pytest.raises(DomainError):
            auth.authenticate(first.token)
        assert auth.authenticate(last.token) == member
