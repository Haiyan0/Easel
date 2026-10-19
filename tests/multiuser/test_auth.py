"""Task 2 认证闭环：管理员引导、邀请兑换、登录生命周期与撤销。

全部使用真实 SQLite；不 mock 数据库、不 mock 角色校验。
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading

import pytest

from easel.control.auth import AuthService
from easel.control.store import SCHEMA_VERSION, Store
from easel.control.types import DomainError, Identity


def assert_error(error: pytest.ExceptionInfo, status: int, code: str = "") -> None:
    assert isinstance(error.value, DomainError)
    assert error.value.status == status, error.value
    if code:
        assert error.value.code == code, error.value


class MutableClock:
    """可推进的注入时钟；测试不通过 sleep 等待边界。"""

    def __init__(self, now: float = 1000.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def scalar(store: Store, sql: str, parameters: tuple = ()):
    return store._connection.execute(sql, parameters).fetchone()[0]


# --------------------------------------------------------------------------
# 管理员本地引导与邀请
# --------------------------------------------------------------------------

def test_bootstrap_creates_one_admin_and_one_invite(tmp_path):
    clock = MutableClock()
    with Store.open(tmp_path / "control.sqlite3") as store:
        auth = AuthService(store, clock=clock)
        admin, invite = auth.bootstrap_admin("Owner")
        assert admin.role == "admin"
        assert scalar(store, "SELECT COUNT(*) FROM users WHERE role = 'admin'") == 1

        session = auth.exchange(invite)
        assert auth.authenticate(session.token) == admin

        with pytest.raises(DomainError) as repeated:
            auth.bootstrap_admin("Second")
        assert_error(repeated, 409, "admin_exists")
        assert scalar(store, "SELECT COUNT(*) FROM users") == 1, "重复引导不得创建用户"


def test_invite_is_single_use_and_login_is_revocable(tmp_path):
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        auth = AuthService(store, clock=lambda: 1000.0)
        invite = auth.create_invite(admin, member.user_id)

        login = auth.exchange(invite)
        assert auth.authenticate(login.token) == member

        with pytest.raises(DomainError) as reused:
            auth.exchange(invite)
        assert_error(reused, 401)

        auth.revoke_all(member.user_id)
        with pytest.raises(DomainError) as revoked:
            auth.authenticate(login.token)
        assert_error(revoked, 401)


def test_expired_revoked_and_disabled_invites_all_return_401(tmp_path):
    clock = MutableClock()
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        auth = AuthService(store, clock=clock)

        expired = auth.create_invite(admin, member.user_id, ttl_seconds=10)
        clock.advance(10)
        with pytest.raises(DomainError) as expired_error:
            auth.exchange(expired)
        assert_error(expired_error, 401)

        revoked = auth.create_invite(admin, member.user_id)
        assert auth.revoke_invite(revoked) is True
        with pytest.raises(DomainError) as revoked_error:
            auth.exchange(revoked)
        assert_error(revoked_error, 401)

        for_invalid_name = auth.create_invite(admin, member.user_id)
        assert for_invalid_name
        for malformed in ("", "not a token", "x" * 1000):
            with pytest.raises(DomainError) as malformed_error:
                auth.exchange(malformed)
            assert_error(malformed_error, 401)

        disabled_invite = auth.create_invite(admin, member.user_id)
        store.disable_user(member.user_id)
        with pytest.raises(DomainError) as disabled_error:
            auth.exchange(disabled_invite)
        assert_error(disabled_error, 401)


def test_two_connections_racing_for_one_invite_allow_one_login(tmp_path):
    path = tmp_path / "control.sqlite3"
    with Store.open(path) as setup:
        admin = setup.create_user("Owner", "admin")
        member = setup.create_user("Member")
        invite = AuthService(setup, clock=lambda: 1000.0).create_invite(
            admin, member.user_id)

    barrier = threading.Barrier(2, timeout=10)
    sessions = []
    errors = []

    def exchange_in_own_connection():
        with Store.open(path) as store:
            auth = AuthService(store, clock=lambda: 1000.0)
            barrier.wait()
            try:
                sessions.append(auth.exchange(invite))
            except DomainError as error:
                errors.append(error)

    threads = [threading.Thread(target=exchange_in_own_connection) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()

    assert len(sessions) == 1, "同一邀请只能兑换一次"
    assert len(errors) == 1 and errors[0].status == 401
    with Store.open(path) as store:
        assert scalar(store, "SELECT COUNT(*) FROM logins") == 1


def test_concurrent_bootstrap_allows_exactly_one_admin(tmp_path):
    path = tmp_path / "control.sqlite3"
    with Store.open(path):
        pass  # 先建库，让两个线程真正竞争管理员存在性判定

    barrier = threading.Barrier(2, timeout=10)
    created = []
    errors = []

    def bootstrap_in_own_connection():
        with Store.open(path) as store:
            barrier.wait()
            try:
                created.append(
                    AuthService(store, clock=lambda: 1000.0).bootstrap_admin("Owner"))
            except DomainError as error:
                errors.append(error)

    threads = [threading.Thread(target=bootstrap_in_own_connection) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()

    assert len(created) == 1, "并发引导只能创建一个管理员"
    assert len(errors) == 1 and errors[0].code == "admin_exists"
    with Store.open(path) as store:
        assert scalar(store, "SELECT COUNT(*) FROM users WHERE role = 'admin'") == 1


# --------------------------------------------------------------------------
# 登录生命周期
# --------------------------------------------------------------------------

def test_idle_expiry_returns_401_and_activity_slides_last_seen(tmp_path):
    clock = MutableClock()
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        auth = AuthService(store, clock=clock)
        login = auth.exchange(auth.create_invite(admin, member.user_id))

        clock.advance(AuthService.SESSION_IDLE_SECONDS - 1)
        assert auth.authenticate(login.token) == member
        last_seen = scalar(store, "SELECT last_seen_at FROM logins WHERE id = ?", (login.id,))
        assert last_seen == clock.now

        clock.advance(AuthService.SESSION_IDLE_SECONDS - 1)
        assert auth.authenticate(login.token) == member, "活动应滑动闲置窗口"

        clock.advance(AuthService.SESSION_IDLE_SECONDS)
        with pytest.raises(DomainError) as expired:
            auth.authenticate(login.token)
        assert_error(expired, 401)


def test_absolute_expiry_is_never_extended_by_activity(tmp_path):
    clock = MutableClock()
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        auth = AuthService(store, clock=clock)
        login = auth.exchange(auth.create_invite(admin, member.user_id))
        absolute = clock.now + AuthService.SESSION_ABSOLUTE_SECONDS

        # 持续活动，但每次都在闲置窗口内，直到绝对期限前夕
        while clock.now + AuthService.SESSION_IDLE_SECONDS - 1 < absolute:
            clock.advance(AuthService.SESSION_IDLE_SECONDS - 1)
            assert auth.authenticate(login.token) == member
        assert scalar(store, "SELECT expires_at FROM logins WHERE id = ?", (login.id,)) == absolute

        clock.now = absolute - 1
        assert auth.authenticate(login.token) == member
        clock.now = absolute
        with pytest.raises(DomainError) as expired:
            auth.authenticate(login.token)
        assert_error(expired, 401)


def test_tokens_and_csrf_are_only_stored_as_digests(tmp_path):
    path = tmp_path / "control.sqlite3"
    with Store.open(path) as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        auth = AuthService(store, clock=lambda: 1000.0)
        invite = auth.create_invite(admin, member.user_id)
        login = auth.exchange(invite)

        assert login.token != invite, "登录 token 与邀请 token 必须不同"
        assert login.csrf_token not in (login.token, invite)
        row = store._connection.execute(
            "SELECT token_digest, csrf_digest FROM logins WHERE id = ?",
            (login.id,)).fetchone()
        assert row["token_digest"] == hashlib.sha256(login.token.encode()).hexdigest()
        assert row["csrf_digest"] == hashlib.sha256(login.csrf_token.encode()).hexdigest()
        assert scalar(
            store, "SELECT COUNT(*) FROM invites WHERE token_digest = ?",
            (hashlib.sha256(invite.encode()).hexdigest(),)) == 1

        # WAL 可能还没合并，主库与 WAL 都要检查
        database_bytes = path.read_bytes()
        wal = path.with_name(path.name + "-wal")
        if wal.exists():
            database_bytes += wal.read_bytes()
        for plaintext in (invite, login.token, login.csrf_token):
            assert plaintext.encode() not in database_bytes, "数据库不得保存令牌明文"


def test_csrf_token_survives_service_restart_and_new_connection(tmp_path):
    path = tmp_path / "control.sqlite3"
    clock = MutableClock()
    with Store.open(path) as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        auth = AuthService(store, clock=clock)
        login = auth.exchange(auth.create_invite(admin, member.user_id))
        csrf = auth.csrf_token(login.token)
        assert csrf == login.csrf_token

    with Store.open(path) as reopened:
        restarted = AuthService(reopened, clock=clock)
        assert restarted.authenticate(login.token) == member
        assert restarted.csrf_token(login.token) == csrf, "刷新/重启后应能恢复同一 CSRF"

    clock.advance(AuthService.SESSION_IDLE_SECONDS)
    with Store.open(path) as reopened:
        with pytest.raises(DomainError) as expired:
            AuthService(reopened, clock=clock).authenticate(login.token)
        assert_error(expired, 401)


# --------------------------------------------------------------------------
# 授权边界
# --------------------------------------------------------------------------

def test_invite_creation_rechecks_real_admin_role_and_target_user(tmp_path):
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        other = store.create_user("Other")
        auth = AuthService(store, clock=lambda: 1000.0)

        forged = Identity(member.user_id, member.workspace_id, "admin")
        with pytest.raises(DomainError) as forged_error:
            auth.create_invite(forged, other.user_id)
        assert_error(forged_error, 403)

        with pytest.raises(DomainError) as missing_target:
            auth.create_invite(admin, "no-such-user")
        assert_error(missing_target, 404)

        store.disable_user(other.user_id)
        with pytest.raises(DomainError) as disabled_target:
            auth.create_invite(admin, other.user_id)
        assert_error(disabled_target, 403)

        store.disable_user(admin.user_id)
        with pytest.raises(DomainError) as disabled_admin:
            auth.create_invite(admin, member.user_id)
        assert_error(disabled_admin, 403)


@pytest.mark.parametrize("ttl", [0, -1, AuthService.MAX_INVITE_TTL_SECONDS + 1, True, 1.5])
def test_invalid_invite_ttl_is_rejected(tmp_path, ttl):
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        auth = AuthService(store, clock=lambda: 1000.0)
        with pytest.raises(DomainError) as invalid:
            auth.create_invite(admin, member.user_id, ttl_seconds=ttl)
        assert_error(invalid, 400, "invalid_ttl")


def test_revoke_all_role_change_and_disable_visible_across_connections(tmp_path):
    path = tmp_path / "control.sqlite3"
    with Store.open(path) as setup:
        admin = setup.create_user("Owner", "admin")
        member = setup.create_user("Member")
        auth = AuthService(setup, clock=lambda: 1000.0)
        first = auth.exchange(auth.create_invite(admin, member.user_id))
        second = auth.exchange(auth.create_invite(admin, member.user_id))

    with Store.open(path) as store:
        other = AuthService(store, clock=lambda: 1000.0)
        assert other.authenticate(first.token) == member

        store._connection.execute(
            "UPDATE users SET role = 'admin' WHERE id = ?", (member.user_id,))
        assert other.authenticate(first.token).role == "admin", "角色必须每次从数据库读取"

        other.revoke_all(member.user_id)
        for token in (first.token, second.token):
            with pytest.raises(DomainError) as revoked:
                other.authenticate(token)
            assert_error(revoked, 401)

    with Store.open(path) as store:
        third_invite = AuthService(store, clock=lambda: 1000.0).create_invite(
            admin, member.user_id)
        third = AuthService(store, clock=lambda: 1000.0).exchange(third_invite)
        store.disable_user(member.user_id)
        with pytest.raises(DomainError) as disabled:
            AuthService(store, clock=lambda: 1000.0).authenticate(third.token)
        assert_error(disabled, 401)


# --------------------------------------------------------------------------
# v1 → v2 迁移
# --------------------------------------------------------------------------

V1_SCHEMA_SQL = """
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    created_at INTEGER NOT NULL,
    disabled_at INTEGER
);
CREATE TABLE workspaces (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK (state IN ('provisioning', 'ready', 'stopped', 'failed')),
    config_version INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE conversations (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
    id TEXT NOT NULL,
    title TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (workspace_id, id)
);
CREATE TABLE runs (
    workspace_id TEXT NOT NULL,
    id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'reserved', 'running', 'awaiting_permission',
        'done', 'failed', 'interrupted', 'cancelled')),
    config_version INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    finished_at INTEGER,
    capacity_released_at INTEGER,
    release_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workspace_id, id),
    UNIQUE (workspace_id, client_request_id),
    FOREIGN KEY (workspace_id, conversation_id)
        REFERENCES conversations(workspace_id, id) ON DELETE RESTRICT
);
CREATE INDEX runs_active_idx ON runs (status, workspace_id);
CREATE INDEX runs_conversation_idx ON runs (workspace_id, conversation_id, created_at);
CREATE TABLE broker_tokens (
    token_digest TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked_at INTEGER,
    FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT
);
CREATE INDEX broker_tokens_run_idx ON broker_tokens (workspace_id, run_id, channel);
CREATE TABLE worker_leases (
    workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE RESTRICT,
    container_id TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'absent', 'starting', 'ready', 'draining', 'stopped', 'failed')),
    heartbeat_at INTEGER NOT NULL,
    started_at INTEGER NOT NULL
);
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);
"""


def make_v1_database(path) -> None:
    """用冻结的 v1 DDL 建旧库并写入 Task 1 数据。"""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(V1_SCHEMA_SQL)
        connection.execute("PRAGMA user_version = 1")
        connection.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (1, 900)")
        connection.execute(
            "INSERT INTO users (id, display_name, role, created_at) VALUES (?, ?, ?, ?)",
            ("admin-1", "旧管理员", "admin", 900))
        connection.execute(
            "INSERT INTO users (id, display_name, role, created_at) VALUES (?, ?, ?, ?)",
            ("member-1", "旧成员", "member", 901))
        connection.execute(
            "INSERT INTO workspaces (id, owner_user_id, state, config_version, created_at)"
            " VALUES (?, ?, 'ready', 3, ?)", ("ws-member", "member-1", 901))
        connection.execute(
            "INSERT INTO workspaces (id, owner_user_id, state, config_version, created_at)"
            " VALUES (?, ?, 'ready', 4, ?)", ("ws-admin", "admin-1", 900))
        connection.execute(
            "INSERT INTO conversations (workspace_id, id, title, archived, created_at, updated_at)"
            " VALUES (?, ?, ?, 0, ?, ?)",
            ("ws-member", "conv-1", "旧会话", 902, 903))
        connection.execute(
            "INSERT INTO runs (workspace_id, id, conversation_id, client_request_id,"
            " input_digest, status, config_version, created_at, finished_at,"
            " capacity_released_at, release_count)"
            " VALUES (?, ?, ?, ?, ?, 'done', 3, ?, ?, ?, 1)",
            ("ws-member", "run-1", "conv-1", "old-request", "old-digest", 904, 905, 905))
        connection.commit()
    finally:
        connection.close()


def test_v1_database_upgrades_in_place_and_preserves_task1_data(tmp_path):
    path = tmp_path / "v1-control.sqlite3"
    make_v1_database(path)

    with Store.open(path) as store:
        assert SCHEMA_VERSION >= 2
        actor = Identity("member-1", "ws-member", "member")
        conversation = store.get_conversation(actor, "conv-1")
        assert conversation.title == "旧会话"
        replayed = store.reserve_run(actor, "conv-1", "old-request", "old-digest", 3)
        assert replayed.id == "run-1", "升级后旧幂等记录必须保留"
        assert store.get_run(actor, "run-1").status == "done"

        # 升级后的库直接支持认证闭环
        admin = store.get_identity("admin-1")
        auth = AuthService(store, clock=lambda: 1000.0)
        session = auth.exchange(auth.create_invite(admin, "member-1"))
        assert auth.authenticate(session.token).user_id == "member-1"

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert [row[0] for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version")] == \
            list(range(1, SCHEMA_VERSION + 1))
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"invites", "logins", "auth_failures"} <= tables
        assert connection.execute(
            "SELECT COUNT(*) FROM users WHERE id IN ('admin-1', 'member-1')").fetchone()[0] == 2
        assert connection.execute(
            "SELECT title FROM conversations WHERE id = 'conv-1'").fetchone()[0] == "旧会话"
    finally:
        connection.close()
