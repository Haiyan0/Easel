"""SQLite 控制元数据：资源归属、任务幂等与并发接纳。

设计要点：

* 全部 SQL 参数化，事务显式 ``BEGIN IMMEDIATE``；并发约束（每空间 2、全局 8、
  每会话 1）在事务内判定，不依赖进程内字典。
* 每个连接显式开启外键与 ``busy_timeout``；WAL 是库的持久属性，只在确认
  数据库版本可接受、且已取得迁移写锁之后转换一次。
* 授权一律回溯数据库：``Identity`` 只提供 user_id/workspace_id，是否有效、
  是否被禁用、workspace 是否属于该用户，都以库内记录为准。归属校验与
  "允许接纳新任务的空间状态"是两件事：非 ready 空间不再接纳新任务，但已接纳
  任务的收尾必须照常完成，否则并发额度会被永久占用。
* 幂等（同一 ``client_request_id``）最先识别；随后是每会话单任务互斥，最后才是
  容量判定。重试不会多占并发预算，也不会被互斥或容量挡下。
* 未知的新版本数据库在读取版本后立即拒绝打开，此时连接尚未执行任何写入、
  也不改变 journal 模式，不重建、不覆盖。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal

from .types import (ConversationRecord, DomainError, Identity, RunRecord,
                    WorkerHandle, WorkerLease)

SCHEMA_VERSION = 10
"""当前控制库 schema 版本；打开时写入 ``PRAGMA user_version``。"""

# 兼容计划里 CURRENT_SCHEMA_VERSION 的写法
CURRENT_SCHEMA_VERSION = SCHEMA_VERSION

ROLES = ("admin", "member")

ACTIVE_RUN_STATUSES = ("reserved", "running", "awaiting_permission")
"""占用并发额度、尚未进入终态的任务状态。"""

TERMINAL_RUN_STATUSES = ("done", "failed", "interrupted", "cancelled")
"""终态；容量已释放，重复收尾必须安全且不重复释放。"""

WORKSPACE_STATES = ("provisioning", "ready", "stopped", "failed")
"""workspace 状态机；与 schema 的 CHECK 约束保持一致。"""

READY_WORKSPACE_STATES = ("ready",)
"""允许接纳新任务的 workspace 状态；收尾与归属读取不受它限制。"""

_BUSY_TIMEOUT_MS = 5000

_WAL_ATTEMPTS = 20
_WAL_RETRY_SECONDS = 0.05
"""冷库并发首次打开时，WAL 转换会短暂失败；有限重试而不是永久等待。"""

_IDENTITY_COLUMNS = "users.id, users.display_name, users.role, workspaces.id AS workspace_id"

_WORKER_LEASE_COLUMNS = (
    "workspace_id, container_id, config_version, state, heartbeat_at,"
    " started_at, owner_id, generation, lease_expires_at, updated_at,"
    " last_activity_at, active_requests, upstream, service_token")


def _now() -> int:
    """UTC epoch 秒；控制库内所有时间统一使用它。"""
    return int(time.time())


def _placeholders(values: tuple) -> str:
    """按值个数生成占位符，避免把值拼进 SQL。"""
    return ", ".join("?" * len(values))


def _text(value: object) -> str:
    """入参统一按文本处理，避免非字符串静默绑定成别的 SQLite 类型。"""
    if not isinstance(value, str) or not value:
        raise DomainError("invalid_argument", 400)
    return value


# 迁移步骤从 1 起编号，索引 i 对应版本 i + 1；只追加新版本，不改写历史步骤。
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    # 版本 1：spec 第 5 节资源表，外加 schema_migrations、broker_tokens、worker_leases
    (
        """
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
            created_at INTEGER NOT NULL,
            disabled_at INTEGER
        )
        """,
        """
        CREATE TABLE workspaces (
            id TEXT PRIMARY KEY,
            owner_user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
            state TEXT NOT NULL CHECK (state IN ('provisioning', 'ready', 'stopped', 'failed')),
            config_version INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE conversations (
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
            id TEXT NOT NULL,
            title TEXT NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (workspace_id, id)
        )
        """,
        """
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
        )
        """,
        """
        CREATE INDEX runs_active_idx ON runs (status, workspace_id)
        """,
        """
        CREATE INDEX runs_conversation_idx ON runs (workspace_id, conversation_id, created_at)
        """,
        """
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
        )
        """,
        """
        CREATE INDEX broker_tokens_run_idx ON broker_tokens (workspace_id, run_id, channel)
        """,
        """
        CREATE TABLE worker_leases (
            workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE RESTRICT,
            container_id TEXT NOT NULL,
            config_version INTEGER NOT NULL,
            state TEXT NOT NULL CHECK (state IN (
                'absent', 'starting', 'ready', 'draining', 'stopped', 'failed')),
            heartbeat_at INTEGER NOT NULL,
            started_at INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at INTEGER NOT NULL
        )
        """,
    ),
    # 版本 2：邀请、浏览器登录与失败限速；只追加，不改写版本 1。
    (
        """
        CREATE TABLE invites (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            token_digest TEXT NOT NULL UNIQUE,
            expires_at INTEGER NOT NULL,
            redeemed_at INTEGER,
            revoked_at INTEGER,
            created_at INTEGER NOT NULL,
            created_by TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE INDEX invites_user_idx ON invites (user_id, created_at)
        """,
        """
        CREATE TABLE logins (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            token_digest TEXT NOT NULL UNIQUE,
            csrf_digest TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked_at INTEGER
        )
        """,
        """
        CREATE INDEX logins_user_idx ON logins (user_id, revoked_at)
        """,
        """
        CREATE TABLE auth_failures (
            source TEXT NOT NULL,
            token_digest TEXT NOT NULL,
            attempted_at INTEGER NOT NULL
        )
        """,
        """
        CREATE INDEX auth_failures_lookup_idx
            ON auth_failures (source, token_digest, attempted_at)
        """,
    ),
    # 版本 3：管理员默认配置版本与个人覆盖；只追加，不改写版本 1/2。
    (
        """
        CREATE TABLE managed_revisions (
            version INTEGER PRIMARY KEY,
            public_json TEXT NOT NULL,
            encrypted_secrets BLOB NOT NULL,
            created_by TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            created_at INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE workspace_overrides (
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
            channel TEXT NOT NULL CHECK (channel IN ('speech', 'image', 'video', 'music')),
            route_kind TEXT NOT NULL CHECK (route_kind IN ('complete', 'managed-model')),
            public_json TEXT NOT NULL,
            encrypted_secrets BLOB,
            version INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (workspace_id, channel)
        )
        """,
    ),
    # 版本 4：broker 不可变配置快照与请求并发租约；只追加，不改写旧迁移。
    (
        "ALTER TABLE broker_tokens ADD COLUMN snapshot_id TEXT",
        "ALTER TABLE broker_tokens ADD COLUMN route_json TEXT",
        "ALTER TABLE broker_tokens ADD COLUMN encrypted_secrets BLOB",
        """
        CREATE UNIQUE INDEX broker_tokens_snapshot_idx
            ON broker_tokens (snapshot_id)
        """,
        """
        CREATE TABLE broker_leases (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            token_digest TEXT NOT NULL,
            acquired_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            FOREIGN KEY (workspace_id, run_id)
                REFERENCES runs(workspace_id, id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE INDEX broker_leases_workspace_idx
            ON broker_leases (workspace_id, expires_at)
        """,
        """
        CREATE INDEX broker_leases_expiry_idx ON broker_leases (expires_at)
        """,
    ),
    # 版本 5：worker 生命周期租约的所有者/代次、心跳期限与就绪句柄；
    # 只追加，不改写历史迁移。旧库的已有租约按空所有者、0 代次升级。
    (
        "ALTER TABLE worker_leases ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE worker_leases ADD COLUMN generation INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE worker_leases ADD COLUMN lease_expires_at INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE worker_leases ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE worker_leases ADD COLUMN last_activity_at INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE worker_leases ADD COLUMN active_requests INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE worker_leases ADD COLUMN upstream TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE worker_leases ADD COLUMN service_token TEXT NOT NULL DEFAULT ''",
        """
        CREATE INDEX worker_leases_state_idx
            ON worker_leases (state, lease_expires_at)
        """,
    ),
    # 版本 6：drain 的 stop 认领需要 transient stopping 状态；SQLite 不能
    # 原地修改 CHECK，因此按既有列重建表并保留所有行。
    (
        "ALTER TABLE worker_leases RENAME TO worker_leases_v5",
        """
        CREATE TABLE worker_leases (
            workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE RESTRICT,
            container_id TEXT NOT NULL,
            config_version INTEGER NOT NULL,
            state TEXT NOT NULL CHECK (state IN (
                'absent', 'starting', 'ready', 'draining', 'stopping',
                'stopped', 'failed')),
            heartbeat_at INTEGER NOT NULL,
            started_at INTEGER NOT NULL,
            owner_id TEXT NOT NULL DEFAULT '',
            generation INTEGER NOT NULL DEFAULT 0,
            lease_expires_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0,
            last_activity_at INTEGER NOT NULL DEFAULT 0,
            active_requests INTEGER NOT NULL DEFAULT 0,
            upstream TEXT NOT NULL DEFAULT '',
            service_token TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        INSERT INTO worker_leases (
            workspace_id, container_id, config_version, state, heartbeat_at,
            started_at, owner_id, generation, lease_expires_at, updated_at,
            last_activity_at, active_requests, upstream, service_token)
        SELECT workspace_id, container_id, config_version, state, heartbeat_at,
            started_at, owner_id, generation, lease_expires_at, updated_at,
            last_activity_at, active_requests, upstream, service_token
        FROM worker_leases_v5
        """,
        "DROP TABLE worker_leases_v5",
        """
        CREATE INDEX worker_leases_state_idx
            ON worker_leases (state, lease_expires_at)
        """,
    ),
    # 版本 7：会话目录软删除。保留 conversations 行，避免已有 Run 外键被
    # 破坏；删除只影响目录可见性，原生历史与持久产物另由 worker 管。
    (
        "ALTER TABLE conversations ADD COLUMN deleted_at INTEGER",
    ),
    # 版本 8：逐 Run 签名快照与唯一派发标记。快照首次写入后不可替换；派发
    # 认领在数据库事务内完成，重试不得再次向 worker 发布任务。
    (
        "ALTER TABLE runs ADD COLUMN snapshot_json TEXT",
        "ALTER TABLE runs ADD COLUMN dispatch_state TEXT NOT NULL DEFAULT 'pending'",
        "ALTER TABLE runs ADD COLUMN dispatch_nonce TEXT",
        "ALTER TABLE runs ADD COLUMN worker_container_id TEXT",
        "ALTER TABLE runs ADD COLUMN dispatched_at INTEGER",
    ),
    # 版本 9：短请求租约。control 转发同步读/写时先登记带过期时间的租约，
    # 异常或断线在 finally 释放；进程崩溃后租约按 expires_at 自行失效，避免
    # 空闲回收误删仍有在途请求的 worker。
    (
        """
        CREATE TABLE request_leases (
            lease_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        )
        """,
        """
        CREATE INDEX request_leases_workspace_idx
            ON request_leases (workspace_id, expires_at)
        """,
        "CREATE INDEX request_leases_expiry_idx ON request_leases (expires_at)",
    ),
    # 版本 10：同一 Run 的唯一准备所有权。并发同请求只允许一个调用签发
    # token/固定快照；其他调用只读原 admission，不能补偿性地中断共享 Run。
    (
        "ALTER TABLE runs ADD COLUMN prepare_owner TEXT",
        "ALTER TABLE runs ADD COLUMN prepare_expires_at INTEGER",
    ),
)


class Store:
    """控制元数据存储；每个实例持有一条 SQLite 连接。"""

    max_active_runs_per_workspace = 2
    max_active_runs_total = 8

    def __init__(self, connection: sqlite3.Connection, path: Path):
        self._connection = connection
        self.path = path

    # ------------------------------------------------------------------
    # 打开、关闭与迁移
    # ------------------------------------------------------------------

    @classmethod
    @contextmanager
    def open(cls, path: Path | str) -> Iterator["Store"]:
        """打开（必要时创建）控制库；退出上下文时关闭。"""
        store = cls.connect(path)
        try:
            yield store
        finally:
            store.close()

    @classmethod
    def connect(cls, path: Path | str) -> "Store":
        """打开控制库并完成迁移；未知的新版本会被拒绝。

        拒绝路径必须完全没有副作用，因此分两步：已存在的库先用**只读**连接做
        版本预检，只有版本可迁移才打开读写连接。读写连接即使不执行写 SQL，
        关闭时也可能把未 checkpoint 的 WAL 合并进主库——那会毁掉崩溃恢复所需的
        日志，所以判定必须发生在它之前。

        随后读写连接在取得迁移写锁后重新读取版本，迁移与 ``user_version``
        在同一事务内提交，避免并发重复迁移。
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _restrict_directory(target.parent)
        if target.exists():
            stored_version = _read_version_without_side_effects(target)
            if stored_version > SCHEMA_VERSION:
                raise DomainError("unsupported_schema", 500)
        timeout_seconds = _BUSY_TIMEOUT_MS / 1000
        connection = sqlite3.connect(str(target), timeout=timeout_seconds,
                                     isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        store = cls(connection, target)
        try:
            store._migrate()
        except BaseException:
            connection.close()
            raise
        return store

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exception) -> Literal[False]:
        self.close()
        return False

    def _migrate(self) -> None:
        """按版本推进 schema；比本代码更新的库一律拒绝，不做破坏性重建。"""
        self._require_supported_version()
        self._apply_migrations()
        self._enable_wal()

    def _require_supported_version(self) -> int:
        """只读取版本；未知的新版本在此拒绝，此时尚未发生任何写入或模式转换。"""
        current = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise DomainError("unsupported_schema", 500)
        return current

    def _apply_migrations(self) -> None:
        """在写事务内重新读取版本并补齐迁移。

        版本必须在取得写锁之后再读：否则两个并发连接会同时读到 0，
        后拿锁的一方重复执行 migration 1。迁移与版本号在同一个事务里提交，
        要么全部生效，要么全部回滚。
        """
        with self._transaction():
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise DomainError("unsupported_schema", 500)
            for target in range(version + 1, SCHEMA_VERSION + 1):
                statements = MIGRATIONS[target - 1] if target <= len(MIGRATIONS) else ()
                if not statements:
                    raise DomainError("unsupported_schema", 500)
                for statement in statements:
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT OR REPLACE INTO schema_migrations (version, applied_at)"
                    " VALUES (?, ?)", (target, _now()))
                # user_version 必须在事务内推进，避免提交了表却漏掉版本
                self._connection.execute(f"PRAGMA user_version = {target}")

    def _convert_to_wal(self) -> bool:
        """尝试一次 WAL 转换，返回本次是否已（或已是）WAL。

        ``PRAGMA journal_mode = WAL`` 不能在事务内执行，也不受 busy_timeout
        保护：锁被占用时直接 ``database is locked`` 而不等待。锁竞争会向上抛出
        ``OperationalError``，由调用方决定是否重试；单次尝试不吞任何异常。
        """
        mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        return isinstance(mode, str) and mode.lower() == "wal"

    def _enable_wal(self) -> None:
        """把库转换为 WAL；只对可恢复的锁竞争做有限重试。

        重试耗尽即视为打开失败：关闭连接并抛出 ``DomainError``，绝不返回一个
        未启用 WAL 的 Store。非锁类的 ``OperationalError``（例如库损坏）既不
        重试也不吞掉，原样抛出以便定位。
        """
        for attempt in range(_WAL_ATTEMPTS):
            try:
                if self._convert_to_wal():
                    return
            except sqlite3.OperationalError as error:
                if not _is_lock_contention(error):
                    raise
                if attempt + 1 >= _WAL_ATTEMPTS:
                    self.close()
                    raise DomainError("wal_unavailable", 503) from error
                time.sleep(_WAL_RETRY_SECONDS * (attempt + 1))
                continue
            # 语句执行成功但未转换到 WAL：短暂状态，按同样节奏重试
            if attempt + 1 >= _WAL_ATTEMPTS:
                self.close()
                raise DomainError("wal_unavailable", 503)
            time.sleep(_WAL_RETRY_SECONDS * (attempt + 1))

    # ------------------------------------------------------------------
    # 用户与 workspace
    # ------------------------------------------------------------------

    def create_user(self, display_name: str, role: str = "member") -> Identity:
        """创建用户，并在同一事务内创建其唯一私有 workspace。"""
        if role not in ROLES:
            raise DomainError("invalid_role", 400)
        user_id = uuid.uuid4().hex
        workspace_id = uuid.uuid4().hex
        stamp = _now()
        with self._transaction():
            self._connection.execute(
                "INSERT INTO users (id, display_name, role, created_at) VALUES (?, ?, ?, ?)",
                (user_id, display_name, role, stamp))
            self._connection.execute(
                "INSERT INTO workspaces (id, owner_user_id, state, config_version, created_at)"
                " VALUES (?, ?, 'ready', 0, ?)", (workspace_id, user_id, stamp))
        return Identity(user_id, workspace_id, role)

    def disable_user(self, user_id: str) -> bool:
        """禁用用户；已有数据保持不变。返回本次调用是否真的改变了状态。"""
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE users SET disabled_at = ? WHERE id = ? AND disabled_at IS NULL",
                (_now(), user_id))
        return cursor.rowcount == 1

    def set_workspace_state(self, workspace_id: str, state: str) -> bool:
        """按状态机切换 workspace 状态；返回该 workspace 是否存在。

        非 ready 状态不再接纳新任务，但已接纳任务仍可收尾（见 ``_require_actor``）。
        """
        if state not in WORKSPACE_STATES:
            raise DomainError("invalid_state", 400)
        with self._transaction():
            self._connection.execute(
                "UPDATE workspaces SET state = ? WHERE id = ?",
                (state, _text(workspace_id)))
            return self._workspace_exists(workspace_id)

    def _workspace_exists(self, workspace_id: str) -> bool:
        return self._connection.execute(
            "SELECT 1 FROM workspaces WHERE id = ?", (workspace_id,)).fetchone() is not None

    # ------------------------------------------------------------------
    # worker 生命周期租约
    # ------------------------------------------------------------------

    def get_worker_lease(self, workspace_id: str) -> WorkerLease | None:
        """读取单个 workspace 的持久生命周期租约。"""
        return self._worker_lease_row(_text(workspace_id))

    def list_worker_leases(self) -> list[WorkerLease]:
        rows = self._connection.execute(
            f"SELECT {_WORKER_LEASE_COLUMNS} FROM worker_leases"
            " ORDER BY workspace_id").fetchall()
        return [lease for lease in (_worker_lease(row) for row in rows)
                if lease is not None]

    def claim_worker_start(self, workspace_id: str, config_version: int,
                           owner_id: str, now: int,
                           lease_seconds: int) -> tuple[str, WorkerLease | None]:
        """原子认领一次启动，或返回可复用的就绪状态。

        返回值：

        * ``started``：调用方取得启动权，必须在租约期限内完成并携带
          ``(owner_id, generation)`` 回写；旧持有者不匹配时不能覆盖新代次；
        * ``ready``：同一配置版本的存活实例可直接复用；
        * ``reconcile``：租约已过期，调用方必须先 inspect 实际容器；
        * ``wait``：另一个管理进程正在启动，调用方等待其完成；
        * ``upgrade``：配置版本变化且当前无活跃任务，可安全重建；
        * ``config_busy``：配置版本变化但仍有活跃任务，禁止覆盖；
        * ``draining``：正在 drain，拒绝新任务；
        * ``recover_stop``：停止认领已过期，必须先按原实例 ID 完成停止，
          才能创建新代次，避免同一物理实例被旧 stop 与新一代同时使用。
        """
        workspace_id = _text(workspace_id)
        owner_id = _text(owner_id)
        if type(config_version) is not int or config_version < 0:
            raise DomainError("invalid_argument", 400)
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise DomainError("invalid_argument", 400)
        now = int(now)
        with self._transaction():
            self._require_ready_workspace(workspace_id)
            lease = self._worker_lease_row(workspace_id)
            if lease is None:
                self._claim_new_generation(workspace_id, config_version,
                                           owner_id, now, lease_seconds)
                return "started", self._worker_lease_row(workspace_id)

            if lease.state == "ready":
                if lease.config_version == config_version:
                    if lease.lease_expires_at > now:
                        self._connection.execute(
                            "UPDATE worker_leases SET heartbeat_at = ?,"
                            " lease_expires_at = ?, updated_at = ?,"
                            " last_activity_at = ? WHERE workspace_id = ?",
                            (now, now + lease_seconds, now, now, workspace_id))
                        return "ready", self._worker_lease_row(workspace_id)
                    # 心跳过期：不能直接相信旧句柄，必须核对真实容器身份
                    return "reconcile", lease
                if self._has_active_workload(workspace_id, lease):
                    return "config_busy", lease
                return "upgrade", lease

            if lease.state == "starting":
                if lease.lease_expires_at > now:
                    return "wait", lease
                self._claim_new_generation(workspace_id, config_version,
                                           owner_id, now, lease_seconds)
                return "started", self._worker_lease_row(workspace_id)

            if lease.state == "draining":
                return "draining", lease
            if lease.state == "stopping":
                if lease.lease_expires_at > now:
                    return "draining", lease
                # 停止承诺必须先被消解。调用方按旧实例 ID 实际 stop 并
                # CAS 写回 stopped 后，才能重新认领新代次；不能直接复用
                # 一个可能仍有旧 stop 在途的物理实例。
                return "recover_stop", lease

            # absent / stopped / failed：可以安全创建新代次
            self._claim_new_generation(workspace_id, config_version,
                                       owner_id, now, lease_seconds)
            return "started", self._worker_lease_row(workspace_id)

    def complete_worker_start(self, workspace_id: str, owner_id: str,
                              generation: int, handle: WorkerHandle,
                              now: int, lease_seconds: int) -> bool:
        """仅当仍是本次认领的所有者/代次时写入 ready。"""
        workspace_id = _text(workspace_id)
        owner_id = _text(owner_id)
        if handle.workspace_id != workspace_id:
            raise DomainError("invalid_argument", 400)
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise DomainError("invalid_argument", 400)
        now = int(now)
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE worker_leases SET state = 'ready', container_id = ?,"
                " upstream = ?, service_token = ?, config_version = ?,"
                " heartbeat_at = ?, lease_expires_at = ?, updated_at = ?,"
                " last_activity_at = ?, active_requests = 0"
                " WHERE workspace_id = ? AND owner_id = ? AND generation = ?"
                " AND state = 'starting'",
                (handle.container_id, handle.upstream, handle.service_token,
                 int(handle.config_version), now, now + lease_seconds, now,
                 now, workspace_id, owner_id, int(generation)))
        return cursor.rowcount == 1

    def fail_worker_start(self, workspace_id: str, owner_id: str,
                          generation: int, now: int) -> list[str]:
        """标记本次启动失败，并返回被转为 interrupted 的残留任务。

        只有仍持有当前代次时才允许改写；被新代次接管的旧启动返回空列表，
        不会覆盖新状态。返回值供调用方撤销这些 Run 的临时 broker 令牌。
        """
        workspace_id = _text(workspace_id)
        owner_id = _text(owner_id)
        now = int(now)
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE worker_leases SET state = 'failed', container_id = '',"
                " upstream = '', service_token = '', active_requests = 0,"
                " lease_expires_at = 0, updated_at = ?"
                " WHERE workspace_id = ? AND owner_id = ? AND generation = ?"
                " AND state = 'starting'",
                (now, workspace_id, owner_id, int(generation)))
            if cursor.rowcount != 1:
                return []
            return self._interrupt_active_runs_locked(workspace_id, now)

    def fail_worker_state(self, workspace_id: str, now: int, *,
                          expected_state: str | None = None,
                          generation: int | None = None) -> list[str]:
        """对账发现容器不可信时失败关闭；不接管、不缓存旧句柄。

        ``generation`` 用于防止过期的对账者失败掉另一个管理进程刚接管的新
        代次。只清可再建句柄，不触碰持久挂载。返回本次被转为 interrupted 的
        残留任务 ID，供调用方撤销这些 Run 的临时 broker 令牌。
        """
        workspace_id = _text(workspace_id)
        now = int(now)
        where = "workspace_id = ?"
        values: list[object] = [workspace_id]
        if expected_state is not None:
            where += " AND state = ?"
            values.append(expected_state)
        if generation is not None:
            where += " AND generation = ?"
            values.append(int(generation))
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE worker_leases SET state = 'failed', container_id = '',"
                " upstream = '', service_token = '', active_requests = 0,"
                " lease_expires_at = 0, updated_at = ? WHERE " + where,
                (now, *values))
            if cursor.rowcount != 1:
                return []
            return self._interrupt_active_runs_locked(workspace_id, now)

    def mark_worker_draining(self, workspace_id: str, now: int, *,
                             generation: int | None = None) -> bool:
        """从 ready 原子进入 draining；并发认领不会在此时创建新实例。"""
        where = "workspace_id = ? AND state = 'ready'"
        values: list[object] = [int(now), _text(workspace_id)]
        if generation is not None:
            where += " AND generation = ?"
            values.append(int(generation))
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE worker_leases SET state = 'draining', updated_at = ?"
                " WHERE " + where, (*values,))
        return cursor.rowcount == 1

    def begin_worker_stop(self, workspace_id: str, now: int, *,
                          generation: int) -> bool:
        """从 draining 原子认领停止动作；并发 drain 只有一个能调用 stop。

        ``stopping`` 在旧租约到期前让新 ensure 继续等待，确保旧 stop 的
        物理动作与其写回绑定到同一实例/代次。
        """
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE worker_leases SET state = 'stopping', updated_at = ?"
                " WHERE workspace_id = ? AND state = 'draining'"
                " AND generation = ?",
                (int(now), _text(workspace_id), int(generation)))
        return cursor.rowcount == 1

    def resume_worker_ready(self, workspace_id: str, now: int, *,
                            generation: int | None = None) -> bool:
        """draining 认领后发现有活跃工作：回滚为 ready。

        空闲回收与发布/权限等待并发时，不能让 workspace 永久停在 draining；
        恢复后仍受心跳过期与后续空闲检查约束。
        """
        where = "workspace_id = ? AND state = 'draining'"
        values: list[object] = [int(now), _text(workspace_id)]
        if generation is not None:
            where += " AND generation = ?"
            values.append(int(generation))
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE worker_leases SET state = 'ready', updated_at = ?"
                " WHERE " + where, (*values,))
        return cursor.rowcount == 1

    def stop_worker(self, workspace_id: str, now: int, *,
                    generation: int | None = None,
                    state: str = "stopped",
                    expected_state: str | None = None) -> bool:
        """容器已回收后写入终态；只清可再建句柄，不触碰持久数据。

        ``expected_state`` 让 drain/回收写回与认领的 ``stopping`` 状态绑定；
        迟到的旧回复不能覆盖已接管的新代次。
        """
        if state not in ("stopped", "failed"):
            raise DomainError("invalid_state", 400)
        workspace_id = _text(workspace_id)
        where = "workspace_id = ?"
        values: list[object] = [workspace_id]
        if generation is not None:
            where += " AND generation = ?"
            values.append(int(generation))
        if expected_state is not None:
            where += " AND state = ?"
            values.append(expected_state)
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE worker_leases SET state = ?, container_id = '',"
                " upstream = '', service_token = '', active_requests = 0,"
                " lease_expires_at = 0, updated_at = ? WHERE " + where,
                (state, int(now), *values))
        return cursor.rowcount == 1

    def heartbeat_worker(self, workspace_id: str, generation: int, now: int,
                         lease_seconds: int,
                         active_requests: int | None = None, *,
                         instance_id: str) -> bool:
        """按调用者持有的代次与实例身份续期，原子拒绝旧持有者。

        空闲回收必须区分“进程仍在心跳”和“确有请求/任务在运行”。仅续期而
        不刷新活动时间，才能让无任务实例在 900 秒后真正进入回收。
        """
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise DomainError("invalid_argument", 400)
        if type(generation) is not int or generation < 0:
            raise DomainError("invalid_argument", 400)
        if active_requests is not None and (
                type(active_requests) is not int or active_requests < 0):
            raise DomainError("invalid_argument", 400)
        instance_id = _text(instance_id)
        now = int(now)
        where = (
            " WHERE workspace_id = ? AND generation = ?"
            " AND container_id = ? AND state IN ('ready', 'draining')")
        identity = (_text(workspace_id), int(generation), instance_id)
        with self._transaction():
            if active_requests is None:
                cursor = self._connection.execute(
                    "UPDATE worker_leases SET heartbeat_at = ?,"
                    " lease_expires_at = ?, updated_at = ?" + where,
                    (now, now + lease_seconds, now, *identity))
            else:
                last_activity = now if active_requests > 0 else None
                cursor = self._connection.execute(
                    "UPDATE worker_leases SET heartbeat_at = ?,"
                    " lease_expires_at = ?, updated_at = ?,"
                    " active_requests = ?,"
                    " last_activity_at = COALESCE(?, last_activity_at)"
                    + where,
                    (now, now + lease_seconds, now, active_requests,
                     last_activity, *identity))
        return cursor.rowcount == 1

    def adopt_worker_ready(self, workspace_id: str, generation: int,
                           owner_id: str, now: int, lease_seconds: int) -> bool:
        """inspect 确认身份后接管旧实例并续期。

        接管会递增代次：两个管理进程同时 reconcile 时，只有一个能接管成功，
        另一个回到认领循环读取新的存活租约，避免两个所有者继续续期同一实例。
        """
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise DomainError("invalid_argument", 400)
        now = int(now)
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE worker_leases SET state = 'ready', owner_id = ?,"
                " generation = generation + 1, heartbeat_at = ?,"
                " lease_expires_at = ?, updated_at = ?, last_activity_at = ?"
                " WHERE workspace_id = ? AND generation = ? AND state = 'ready'",
                (_text(owner_id), now, now + lease_seconds, now, now,
                 _text(workspace_id), int(generation)))
        return cursor.rowcount == 1

    def list_idle_workers(self, now: int, idle_seconds: int) -> list[WorkerLease]:
        """返回可回收实例：心跳存活、无活跃任务/请求且空闲已超阈值。

        在途短请求由 ``request_leases`` 计数；租约带过期时间，控制进程崩溃
        未执行释放时也不会永久阻止回收。
        """
        if type(idle_seconds) is not int or idle_seconds < 0:
            raise DomainError("invalid_argument", 400)
        now = int(now)
        rows = self._connection.execute(
            f"SELECT {_WORKER_LEASE_COLUMNS} FROM worker_leases AS lease"
            " WHERE lease.state = 'ready' AND lease.lease_expires_at > ?"
            " AND lease.active_requests = 0 AND lease.last_activity_at <= ?"
            " AND NOT EXISTS (SELECT 1 FROM runs AS run"
            "  WHERE run.workspace_id = lease.workspace_id"
            f"  AND run.status NOT IN ({_placeholders(TERMINAL_RUN_STATUSES)}))"
            " AND NOT EXISTS (SELECT 1 FROM request_leases AS lease_request"
            "  WHERE lease_request.workspace_id = lease.workspace_id"
            "  AND lease_request.expires_at > ?)"
            " ORDER BY lease.workspace_id",
            (now, now - int(idle_seconds),
             *TERMINAL_RUN_STATUSES, now)).fetchall()
        return [lease for lease in (_worker_lease(row) for row in rows)
                if lease is not None]

    # -- 短请求租约 ---------------------------------------------------------

    def begin_worker_request_lease(self, workspace_id: str, ttl_seconds: int,
                                   *, now: int | None = None) -> str:
        """登记一条有期限的在途请求租约；调用方必须在 finally 释放。

        租约只证明“control 正在向该 workspace 转发一个同步短请求”，不授予
        任何业务权限。过期行在下次登记时顺带清理。
        """
        workspace_id = _text(workspace_id)
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
            raise DomainError("invalid_argument", 400)
        stamp = _now() if now is None else int(now)
        lease_id = uuid.uuid4().hex
        with self._transaction():
            self._connection.execute(
                "DELETE FROM request_leases WHERE expires_at <= ?", (stamp,))
            self._connection.execute(
                "INSERT INTO request_leases (lease_id, workspace_id, expires_at)"
                " VALUES (?, ?, ?)",
                (lease_id, workspace_id, stamp + ttl_seconds))
        return lease_id

    def end_worker_request_lease(self, lease_id: str) -> bool:
        """释放租约；重复或过期释放返回 False，不产生新状态。"""
        with self._transaction():
            cursor = self._connection.execute(
                "DELETE FROM request_leases WHERE lease_id = ?",
                (_text(lease_id),))
        return cursor.rowcount == 1

    def count_worker_request_leases(self, workspace_id: str, *,
                                    now: int | None = None) -> int:
        """统计尚未过期的在途请求；回收判定与异常恢复共用。"""
        stamp = _now() if now is None else int(now)
        row = self._connection.execute(
            "SELECT COUNT(*) AS total FROM request_leases"
            " WHERE workspace_id = ? AND expires_at > ?",
            (_text(workspace_id), stamp)).fetchone()
        return int(row["total"])


    def count_active_runs_for_workspace(self, workspace_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS total FROM runs WHERE workspace_id = ?"
            f" AND status NOT IN ({_placeholders(TERMINAL_RUN_STATUSES)})",
            (_text(workspace_id), *TERMINAL_RUN_STATUSES)).fetchone()
        return int(row["total"])

    def interrupt_active_runs(self, workspace_id: str, now: int) -> list[str]:
        """把崩溃/回收后的残留任务标为 interrupted；不自动重发。

        返回本次真正从活跃转为终态的 Run ID，供调用方撤销其 broker 令牌。
        """
        with self._transaction():
            return self._interrupt_active_runs_locked(
                _text(workspace_id), int(now))

    def _has_active_workload(self, workspace_id: str,
                             lease: WorkerLease) -> bool:
        return (lease.active_requests > 0
                or self.count_active_runs_for_workspace(workspace_id) > 0)

    def _claim_new_generation(self, workspace_id: str, config_version: int,
                              owner_id: str, now: int,
                              lease_seconds: int) -> None:
        self._connection.execute(
            "INSERT INTO worker_leases"
            " (workspace_id, container_id, config_version, state, heartbeat_at,"
            "  started_at, owner_id, generation, lease_expires_at, updated_at,"
            "  last_activity_at, active_requests, upstream, service_token)"
            " VALUES (?, '', ?, 'starting', ?, ?, ?, 1, ?, ?, ?, 0, '', '')"
            " ON CONFLICT(workspace_id) DO UPDATE SET"
            "  container_id = '', config_version = excluded.config_version,"
            "  state = 'starting', heartbeat_at = excluded.heartbeat_at,"
            "  started_at = excluded.started_at, owner_id = excluded.owner_id,"
            "  generation = worker_leases.generation + 1,"
            "  lease_expires_at = excluded.lease_expires_at,"
            "  updated_at = excluded.updated_at,"
            "  last_activity_at = excluded.last_activity_at,"
            "  active_requests = 0, upstream = '', service_token = ''",
            (workspace_id, int(config_version), now, now, owner_id,
             now + lease_seconds, now, now))

    def _interrupt_active_runs_locked(self, workspace_id: str,
                                      now: int) -> list[str]:
        rows = self._connection.execute(
            "SELECT id FROM runs WHERE workspace_id = ? AND status NOT IN"
            f" ({_placeholders(TERMINAL_RUN_STATUSES)})",
            (workspace_id, *TERMINAL_RUN_STATUSES)).fetchall()
        run_ids = [str(row["id"]) for row in rows]
        if run_ids:
            self._connection.execute(
                "UPDATE runs SET status = 'interrupted', finished_at = ?,"
                " capacity_released_at = ?, release_count = release_count + 1"
                " WHERE workspace_id = ? AND id IN"
                f" ({','.join('?' for _ in run_ids)})",
                (now, now, workspace_id, *run_ids))
            for run_id in run_ids:
                # 终态与 token 撤销同事务提交，崩溃窗口内也不会留下可用凭证
                self._revoke_tokens_locked(workspace_id, run_id, now)
        return run_ids

    def _worker_lease_row(self, workspace_id: str) -> WorkerLease | None:
        row = self._connection.execute(
            f"SELECT {_WORKER_LEASE_COLUMNS} FROM worker_leases"
            " WHERE workspace_id = ?", (workspace_id,)).fetchone()
        return _worker_lease(row)

    # ------------------------------------------------------------------
    # 认证：本地引导、邀请与登录
    # ------------------------------------------------------------------

    def get_identity(self, user_id: str) -> Identity:
        """按数据库读取用户当前身份；角色与 workspace 不信任调用方传入值。"""
        row = self._connection.execute(
            "SELECT users.id, users.role, workspaces.id AS workspace_id"
            " FROM users JOIN workspaces ON workspaces.owner_user_id = users.id"
            " WHERE users.id = ?", (_text(user_id),)).fetchone()
        if row is None:
            raise DomainError("not_found", 404)
        return Identity(row["id"], row["workspace_id"], row["role"])

    def bootstrap_admin(self, display_name: str, token_digest: str,
                        now: int, expires_at: int) -> tuple[Identity, str]:
        """本地引导首个管理员并签发首个邀请；已有管理员时整体拒绝。

        管理员存在性判定、用户创建与邀请写入在同一写事务内完成，避免两个
        并发的 bootstrap 各建一个管理员。
        """
        user_id = uuid.uuid4().hex
        workspace_id = uuid.uuid4().hex
        invite_id = uuid.uuid4().hex
        with self._transaction():
            existing = self._connection.execute(
                "SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone()
            if existing is not None:
                raise DomainError("admin_exists", 409)
            self._connection.execute(
                "INSERT INTO users (id, display_name, role, created_at) VALUES (?, ?, 'admin', ?)",
                (user_id, display_name, now))
            self._connection.execute(
                "INSERT INTO workspaces (id, owner_user_id, state, config_version, created_at)"
                " VALUES (?, ?, 'ready', 0, ?)", (workspace_id, user_id, now))
            self._connection.execute(
                "INSERT INTO invites (id, user_id, token_digest, expires_at, created_at, created_by)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (invite_id, user_id, token_digest, expires_at, now, user_id))
        return Identity(user_id, workspace_id, "admin"), invite_id

    def create_invite(self, actor: Identity, user_id: str, token_digest: str,
                      now: int, expires_at: int) -> str:
        """管理员为真实存在且未禁用的用户创建邀请，返回 invite id。"""
        invite_id = uuid.uuid4().hex
        target_user_id = _text(user_id)
        with self._transaction():
            # 角色必须查库，不能信任 Identity.role；禁用状态与 workspace 归属同时复查
            self._require_actor(actor)
            row = self._connection.execute(
                "SELECT role FROM users WHERE id = ?", (actor.user_id,)).fetchone()
            if row is None or row["role"] != "admin":
                raise DomainError("forbidden", 403)
            target = self._connection.execute(
                "SELECT disabled_at FROM users WHERE id = ?", (target_user_id,)).fetchone()
            if target is None:
                raise DomainError("not_found", 404)
            if target["disabled_at"] is not None:
                raise DomainError("user_disabled", 403)
            self._connection.execute(
                "INSERT INTO invites (id, user_id, token_digest, expires_at, created_at, created_by)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (invite_id, target_user_id, token_digest, expires_at, now, actor.user_id))
        return invite_id

    def revoke_invite(self, token_digest: str, now: int) -> bool:
        """撤销尚未兑换的邀请；返回本次调用是否改变了状态。"""
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE invites SET revoked_at = ?"
                " WHERE token_digest = ? AND redeemed_at IS NULL AND revoked_at IS NULL",
                (now, _text(token_digest)))
        return cursor.rowcount == 1

    def _redeem_invite_locked(self, token_digest: str, login_id: str,
                              login_token_digest: str, csrf_digest: str,
                              now: int, expires_at: int) -> Identity | None:
        """在已持有写事务时尝试兑换；任何失效原因返回 None 且不写入。"""
        row = self._connection.execute(
            "SELECT invites.id, invites.user_id, invites.expires_at,"
            " invites.redeemed_at, invites.revoked_at,"
            " users.role, users.disabled_at, workspaces.id AS workspace_id"
            " FROM invites"
            " JOIN users ON users.id = invites.user_id"
            " JOIN workspaces ON workspaces.owner_user_id = users.id"
            " WHERE invites.token_digest = ?", (_text(token_digest),)).fetchone()
        if (row is None or row["redeemed_at"] is not None
                or row["revoked_at"] is not None or row["expires_at"] <= now
                or row["disabled_at"] is not None):
            return None
        cursor = self._connection.execute(
            "UPDATE invites SET redeemed_at = ?"
            " WHERE id = ? AND redeemed_at IS NULL AND revoked_at IS NULL",
            (now, row["id"]))
        if cursor.rowcount != 1:
            return None
        self._connection.execute(
            "INSERT INTO logins (id, user_id, token_digest, csrf_digest,"
            " created_at, last_seen_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (login_id, row["user_id"], login_token_digest, csrf_digest,
             now, now, expires_at))
        return Identity(row["user_id"], row["workspace_id"], row["role"])

    def redeem_invite(self, token_digest: str, login_id: str,
                      login_token_digest: str, csrf_digest: str,
                      now: int, expires_at: int) -> Identity:
        """事务内单次兑换邀请并创建登录；任何失效原因统一 401。

        ``BEGIN IMMEDIATE`` 使两个连接对同一邀请串行化：后到者看到的
        ``redeemed_at`` 已非空，只能失败。
        """
        with self._transaction():
            identity = self._redeem_invite_locked(
                token_digest, login_id, login_token_digest, csrf_digest,
                now, expires_at)
            if identity is None:
                raise DomainError("unauthorized", 401)
        return identity

    def redeem_invite_limited(self, *, source: str, token_digest: str,
                              login_id: str, login_token_digest: str,
                              csrf_digest: str, now: int, expires_at: int,
                              window_seconds: int, max_failures: int
                              ) -> tuple[str, Identity | None, int]:
        """限速准入、邀请兑换与失败记账在同一个写事务内完成。

        返回 ``(status, identity, retry_after)``：

        * ``rate_limited``：额度已满，未读取/消耗邀请；
        * ``denied``：邀请无效，失败记录已随事务提交；
        * ``ok``：兑换成功。

        拒绝路径不能靠抛异常回滚，否则失败计数会一起丢失；因此返回状态，
        由调用方在事务提交后映射为 401。
        """
        with self._transaction():
            self._prune_auth_failures(now, window_seconds)
            retry_after = self._rate_limit_retry_after_locked(
                source, token_digest, now, window_seconds, max_failures)
            if retry_after:
                return "rate_limited", None, retry_after
            identity = self._redeem_invite_locked(
                token_digest, login_id, login_token_digest, csrf_digest,
                now, expires_at)
            if identity is None:
                self._connection.execute(
                    "INSERT INTO auth_failures (source, token_digest, attempted_at)"
                    " VALUES (?, ?, ?)", (source, token_digest, now))
                return "denied", None, 0
            return "ok", identity, 0

    def _prune_auth_failures(self, now: int, window_seconds: int) -> None:
        self._connection.execute(
            "DELETE FROM auth_failures WHERE attempted_at <= ?",
            (now - window_seconds,))

    def _rate_limit_retry_after_locked(self, source: str, token_digest: str,
                                       now: int, window_seconds: int,
                                       max_failures: int) -> int:
        """在已持有写事务时计算限速结果；0 表示允许。"""
        by_source = self._connection.execute(
            "SELECT COUNT(*) AS total, MIN(attempted_at) AS oldest"
            " FROM auth_failures WHERE source = ?", (source,)).fetchone()
        by_digest = self._connection.execute(
            "SELECT COUNT(*) AS total, MIN(attempted_at) AS oldest"
            " FROM auth_failures WHERE token_digest = ?",
            (token_digest,)).fetchone()
        retry_after = 0
        if by_source["total"] >= max_failures:
            retry_after = max(
                retry_after, by_source["oldest"] + window_seconds - now)
        if by_digest["total"] >= max_failures:
            retry_after = max(
                retry_after, by_digest["oldest"] + window_seconds - now)
        return max(1, retry_after) if retry_after else 0

    def check_auth_rate_limit(self, source: str, token_digest: str, now: int,
                              window_seconds: int, max_failures: int) -> int:
        """检查失败计数；返回 0 允许尝试，否则返回 Retry-After 秒数。

        同时按来源和令牌摘要计数，任一维度达到上限即拒绝：只按
        ``(source, token_digest)`` 组合计数会让攻击者换一个猜测 token 就获得
        新额度。计数落库，多个 AuthService 或新连接不能绕过；窗口外记录在
        检查时清理。
        """
        with self._transaction():
            self._prune_auth_failures(now, window_seconds)
            return self._rate_limit_retry_after_locked(
                source, token_digest, now, window_seconds, max_failures)

    def record_auth_failure(self, source: str, token_digest: str, now: int,
                            window_seconds: int) -> None:
        """记录一次兑换失败；不保存原始 token。"""
        with self._transaction():
            self._prune_auth_failures(now, window_seconds)
            self._connection.execute(
                "INSERT INTO auth_failures (source, token_digest, attempted_at)"
                " VALUES (?, ?, ?)", (source, token_digest, now))

    # ------------------------------------------------------------------
    # 配置：默认版本与个人覆盖
    # ------------------------------------------------------------------

    def require_identity(self, actor: Identity) -> Identity:
        """按数据库重查演员身份（含禁用与 workspace 归属），返回当前角色。"""
        workspace_id = self._require_actor(actor)
        row = self._connection.execute(
            "SELECT role FROM users WHERE id = ?", (actor.user_id,)).fetchone()
        if row is None:
            raise DomainError("unauthorized", 403)
        return Identity(actor.user_id, workspace_id, row["role"])

    def publish_managed_revision(self, actor: Identity, public_json: str,
                                 encrypted_secrets: bytes, expected_version: int,
                                 now: int) -> int:
        """管理员发布默认配置；版本比较与写入在同一写事务内。"""
        with self._transaction():
            self._require_actor(actor)
            row = self._connection.execute(
                "SELECT role FROM users WHERE id = ?", (actor.user_id,)).fetchone()
            if row is None or row["role"] != "admin":
                raise DomainError("forbidden", 403)
            current = self._connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM managed_revisions").fetchone()[0]
            if current != expected_version:
                raise DomainError("version_conflict", 409)
            new_version = expected_version + 1
            self._connection.execute(
                "INSERT INTO managed_revisions"
                " (version, public_json, encrypted_secrets, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (new_version, public_json, encrypted_secrets, actor.user_id, now))
        return new_version

    def latest_managed_revision(self) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT version, public_json, encrypted_secrets, created_by, created_at"
            " FROM managed_revisions ORDER BY version DESC LIMIT 1").fetchone()

    def get_workspace_override(self, workspace_id: str,
                               channel: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT workspace_id, channel, route_kind, public_json,"
            " encrypted_secrets, version, created_at, updated_at"
            " FROM workspace_overrides WHERE workspace_id = ? AND channel = ?",
            (_text(workspace_id), _text(channel))).fetchone()

    def list_workspace_overrides(self, workspace_id: str) -> list[sqlite3.Row]:
        return self._connection.execute(
            "SELECT workspace_id, channel, route_kind, public_json,"
            " encrypted_secrets, version, created_at, updated_at"
            " FROM workspace_overrides WHERE workspace_id = ? ORDER BY channel",
            (_text(workspace_id),)).fetchall()

    def first_encrypted_override(self) -> sqlite3.Row | None:
        """启动校验 key 时取一条真实密文；没有密文返回 None。"""
        return self._connection.execute(
            "SELECT workspace_id, channel, encrypted_secrets"
            " FROM workspace_overrides WHERE encrypted_secrets IS NOT NULL"
            " ORDER BY workspace_id, channel LIMIT 1").fetchone()

    def read_config_rows(self, workspace_id: str, channel: str):
        """在同一读事务中读取最新受管版本与个人覆盖行。

        SQLite WAL 下读事务对两行提供一致快照；返回的行值不会随后续写入
        改变。chat 不使用个人覆盖。
        """
        with self._read_transaction():
            managed = self.latest_managed_revision()
            override = (None if channel == "chat"
                        else self.get_workspace_override(workspace_id, channel))
        return managed, override

    def save_workspace_override(self, actor: Identity, channel: str,
                                route_kind: str, public_json: str,
                                encrypted_secrets: bytes | None,
                                keep_existing_secret: dict | None,
                                now: int) -> int:
        """原子保存个人覆盖；空 Key 仅在既有个人凭证作用域相同时复用密文。"""
        if route_kind not in ("complete", "managed-model"):
            raise DomainError("invalid_route", 400)
        with self._transaction():
            workspace_id = self._require_actor(actor)
            existing = self._connection.execute(
                "SELECT route_kind, public_json, encrypted_secrets, version"
                " FROM workspace_overrides WHERE workspace_id = ? AND channel = ?",
                (workspace_id, _text(channel))).fetchone()
            if encrypted_secrets is None and keep_existing_secret is not None:
                if existing is None or existing["route_kind"] != "complete":
                    raise DomainError("personal_credentials_required", 400)
                try:
                    old_public = json.loads(existing["public_json"])
                except (TypeError, ValueError):
                    raise DomainError("personal_credentials_required", 400) from None
                if any(old_public.get(key) != keep_existing_secret.get(key)
                       for key in ("provider", "base_url", "protocol")):
                    raise DomainError("personal_credentials_required", 400)
                encrypted_secrets = existing["encrypted_secrets"]
            if route_kind == "complete" and encrypted_secrets is None:
                raise DomainError("personal_credentials_required", 400)
            if route_kind == "managed-model":
                encrypted_secrets = None
            revision = (existing["version"] if existing is not None else 0) + 1
            self._connection.execute(
                "INSERT INTO workspace_overrides"
                " (workspace_id, channel, route_kind, public_json, encrypted_secrets,"
                " version, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(workspace_id, channel) DO UPDATE SET"
                " route_kind = excluded.route_kind,"
                " public_json = excluded.public_json,"
                " encrypted_secrets = excluded.encrypted_secrets,"
                " version = excluded.version,"
                " updated_at = excluded.updated_at",
                (workspace_id, _text(channel), route_kind, public_json,
                 encrypted_secrets, revision, now, now))
        return revision

    def clear_workspace_override(self, actor: Identity, channel: str,
                                 now: int) -> bool:
        """清除本人覆盖；只能删除 actor 自己的 workspace 行。"""
        with self._transaction():
            workspace_id = self._require_actor(actor)
            cursor = self._connection.execute(
                "DELETE FROM workspace_overrides"
                " WHERE workspace_id = ? AND channel = ?",
                (workspace_id, _text(channel)))
        return cursor.rowcount == 1

    def get_managed_revision(self, version: int) -> sqlite3.Row | None:
        """按版本读取不可变管理员配置快照；旧版本永不覆盖。"""
        if type(version) is not int or version < 1:
            return None
        return self._connection.execute(
            "SELECT version, public_json, encrypted_secrets, created_by, created_at"
            " FROM managed_revisions WHERE version = ?", (version,)).fetchone()

    # ------------------------------------------------------------------
    # Provider broker：令牌、不可变快照与并发租约
    # ------------------------------------------------------------------

    def create_broker_token_atomic(self, actor: Identity, run_id: str,
                                   channel: str, *, token_digest: str,
                                   snapshot_id: str, now: int, expires_at: int,
                                   build) -> None:
        """在同一个写事务内读取配置行、构建快照并写入令牌。

        ``build(workspace_id, channel, managed_row, override_row)`` 必须只使用
        传入的行和纯计算（解密/重新加密），不得再次查询数据库。这样路由与
        凭证来自同一快照，其他连接无法在两者之间插入更新。
        """
        channel = _text(channel)
        with self._transaction():
            workspace_id = self._require_actor(actor)
            run = self._fetch_run(workspace_id, _text(run_id))
            if run.status not in ACTIVE_RUN_STATUSES:
                raise DomainError("terminal_run", 409)
            managed = self.latest_managed_revision()
            override = (None if channel == "chat"
                        else self.get_workspace_override(workspace_id, channel))
            route_json, encrypted_secrets, config_version = build(
                workspace_id, channel, managed, override)
            self._connection.execute(
                "INSERT INTO broker_tokens"
                " (token_digest, workspace_id, run_id, channel, config_version,"
                " created_at, expires_at, revoked_at, snapshot_id, route_json,"
                " encrypted_secrets)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (_text(token_digest), workspace_id, run.id, channel,
                 int(config_version), int(now), int(expires_at),
                 _text(snapshot_id), _text(route_json),
                 bytes(encrypted_secrets)))

    def get_broker_snapshot(self, snapshot_id: str) -> sqlite3.Row | None:
        """按快照 ID 读取密文；明文只允许 ConfigService 在服务端解密。"""
        return self._connection.execute(
            "SELECT encrypted_secrets FROM broker_tokens WHERE snapshot_id = ?",
            (_text(snapshot_id),)).fetchone()

    def _broker_token_row(self, token_digest: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT broker_tokens.token_digest, broker_tokens.workspace_id,"
            " broker_tokens.run_id, broker_tokens.channel,"
            " broker_tokens.config_version, broker_tokens.created_at,"
            " broker_tokens.expires_at, broker_tokens.revoked_at,"
            " broker_tokens.snapshot_id, broker_tokens.route_json,"
            " broker_tokens.encrypted_secrets,"
            " runs.status AS run_status, users.disabled_at AS user_disabled_at"
            " FROM broker_tokens"
            " JOIN runs ON runs.workspace_id = broker_tokens.workspace_id"
            " AND runs.id = broker_tokens.run_id"
            " JOIN workspaces ON workspaces.id = broker_tokens.workspace_id"
            " JOIN users ON users.id = workspaces.owner_user_id"
            " WHERE broker_tokens.token_digest = ?",
            (_text(token_digest),)).fetchone()

    def _require_broker_token_locked(self, token_digest: str, workspace_id: str,
                                     run_id: str, channel: str,
                                     now: int) -> sqlite3.Row:
        """令牌校验：先认证摘要，再核对 Run/workspace/channel 与活跃状态。

        失败信息只含错误码，不回显摘要、令牌或凭证。
        """
        row = self._broker_token_row(token_digest)
        if row is None or int(now) >= int(row["expires_at"]):
            raise DomainError("invalid_token", 401)
        # 外空间/外 Run 资源统一 404，避免泄露令牌归属
        if row["workspace_id"] != workspace_id or row["run_id"] != run_id:
            raise DomainError("not_found", 404)
        if row["channel"] != channel:
            raise DomainError("not_found", 404)
        if row["user_disabled_at"] is not None:
            raise DomainError("user_disabled", 403)
        if row["run_status"] not in ACTIVE_RUN_STATUSES:
            # 终态与撤销在同一事务写入；先报终态，保持既有 API 契约
            raise DomainError("terminal_run", 409)
        if row["revoked_at"] is not None:
            raise DomainError("invalid_token", 401)
        if (row["snapshot_id"] is None or row["route_json"] is None
                or row["encrypted_secrets"] is None):
            raise DomainError("invalid_token", 401)
        return row

    def consume_broker_token(self, token_digest: str, workspace_id: str,
                             run_id: str, channel: str, now: int) -> sqlite3.Row:
        """读取并校验令牌；不消耗令牌，活跃 Run 可在有效期内多次调用。"""
        return self._require_broker_token_locked(
            token_digest, workspace_id, run_id, channel, int(now))

    def _revoke_tokens_locked(self, workspace_id: str, run_id: str,
                              now: int) -> int:
        cursor = self._connection.execute(
            "UPDATE broker_tokens SET revoked_at = ?"
            " WHERE workspace_id = ? AND run_id = ? AND revoked_at IS NULL",
            (int(now), _text(workspace_id), _text(run_id)))
        return cursor.rowcount

    def revoke_broker_tokens(self, workspace_id: str, run_id: str,
                             now: int) -> int:
        """撤销某 Run 的全部令牌；跨连接立即生效。"""
        with self._transaction():
            return self._revoke_tokens_locked(workspace_id, run_id, now)

    def acquire_broker_lease(self, token_digest: str, workspace_id: str,
                             run_id: str, channel: str, token_now: int,
                             lease_now: int, lease_ttl: int) -> str:
        """原子检查并占用 broker 请求额度（每空间 2、整机 8）。

        token_now 只用于校验令牌是否仍接纳新调用；lease_now/lease_ttl 是
        独立的崩溃恢复时钟。租约在流关闭前由心跳续期，不随 token 到期释放。
        """
        if type(lease_ttl) is not int or lease_ttl <= 0:
            raise DomainError("invalid_argument", 400)
        token_now = int(token_now)
        lease_now = int(lease_now)
        with self._transaction():
            self._require_broker_token_locked(
                token_digest, workspace_id, run_id, channel, token_now)
            # 只回收心跳停止（例如进程崩溃）的过期租约
            self._connection.execute(
                "DELETE FROM broker_leases WHERE expires_at <= ?", (lease_now,))
            workspace_active = self._connection.execute(
                "SELECT COUNT(*) AS total FROM broker_leases"
                " WHERE workspace_id = ? AND expires_at > ?",
                (workspace_id, lease_now)).fetchone()["total"]
            if workspace_active >= self.max_active_runs_per_workspace:
                raise DomainError("workspace_capacity", 429)
            total = self._connection.execute(
                "SELECT COUNT(*) AS total FROM broker_leases"
                " WHERE expires_at > ?", (lease_now,)).fetchone()["total"]
            if total >= self.max_active_runs_total:
                raise DomainError("global_capacity", 429)

            lease_id = uuid.uuid4().hex
            self._connection.execute(
                "INSERT INTO broker_leases"
                " (id, workspace_id, run_id, channel, token_digest,"
                " acquired_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (lease_id, workspace_id, run_id, channel, _text(token_digest),
                 lease_now, lease_now + lease_ttl))
        return lease_id

    def renew_broker_lease(self, lease_id: str, expires_at: int) -> int:
        """在途流心跳续期；返回受影响行数，0 表示租约已丢失。"""
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE broker_leases SET expires_at = ? WHERE id = ?",
                (int(expires_at), _text(lease_id)))
        return cursor.rowcount

    def release_broker_lease(self, lease_id: str) -> None:
        """释放额度；重复释放安全。"""
        with self._transaction():
            self._connection.execute(
                "DELETE FROM broker_leases WHERE id = ?", (_text(lease_id),))

    def count_broker_leases(self, now: int | None = None) -> int:
        if now is None:
            now = int(time.time())
        return self._connection.execute(
            "SELECT COUNT(*) AS total FROM broker_leases WHERE expires_at > ?",
            (int(now),)).fetchone()["total"]

    def _active_login(self, token_digest: str, now: int, idle_seconds: int) -> sqlite3.Row:
        """读取仍有效的登录行；失效原因统一 401。"""
        row = self._connection.execute(
            "SELECT logins.id, logins.user_id, logins.csrf_digest,"
            " logins.last_seen_at, logins.expires_at, logins.revoked_at,"
            " users.role, users.disabled_at, workspaces.id AS workspace_id"
            " FROM logins"
            " JOIN users ON users.id = logins.user_id"
            " JOIN workspaces ON workspaces.owner_user_id = users.id"
            " WHERE logins.token_digest = ?", (_text(token_digest),)).fetchone()
        if (row is None or row["revoked_at"] is not None
                or row["disabled_at"] is not None
                or now >= row["expires_at"]
                or now - row["last_seen_at"] >= idle_seconds):
            raise DomainError("unauthorized", 401)
        return row

    def authenticate_login(self, token_digest: str, now: int, idle_seconds: int,
                           *, touch: bool = True) -> Identity:
        """校验登录并按需滑动更新 last_seen；绝不延长绝对期限。"""
        with self._transaction():
            row = self._active_login(token_digest, now, idle_seconds)
            if touch:
                self._connection.execute(
                    "UPDATE logins SET last_seen_at = ?"
                    " WHERE id = ? AND revoked_at IS NULL", (now, row["id"]))
        return Identity(row["user_id"], row["workspace_id"], row["role"])

    def session_csrf_digest(self, token_digest: str, now: int,
                            idle_seconds: int) -> str:
        """读取有效登录的 CSRF 摘要；不更新活动时间。"""
        row = self._active_login(token_digest, now, idle_seconds)
        return row["csrf_digest"]

    def revoke_login(self, token_digest: str, now: int, idle_seconds: int) -> None:
        """持久撤销当前登录；无效登录返回 401 且不改变任何行。"""
        with self._transaction():
            row = self._active_login(token_digest, now, idle_seconds)
            self._connection.execute(
                "UPDATE logins SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (now, row["id"]))

    def revoke_all_logins(self, user_id: str, now: int) -> int:
        """撤销该用户的全部登录；返回受影响条数。"""
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE logins SET revoked_at = ?"
                " WHERE user_id = ? AND revoked_at IS NULL", (now, _text(user_id)))
        return cursor.rowcount

    def check_auth_rate_limit(self, source: str, token_digest: str, now: int,
                              window_seconds: int, max_failures: int) -> int:
        """检查失败计数；返回 0 允许尝试，否则返回 Retry-After 秒数。

        同时按来源和令牌摘要计数，任一维度达到上限即拒绝：只按
        ``(source, token_digest)`` 组合计数会让攻击者换一个猜测 token 就获得
        新额度。计数落库，多个 AuthService 或新连接不能绕过；窗口外记录在
        检查时清理。
        """
        with self._transaction():
            self._connection.execute(
                "DELETE FROM auth_failures WHERE attempted_at <= ?", (now - window_seconds,))
            by_source = self._connection.execute(
                "SELECT COUNT(*) AS total, MIN(attempted_at) AS oldest"
                " FROM auth_failures WHERE source = ?", (source,)).fetchone()
            by_digest = self._connection.execute(
                "SELECT COUNT(*) AS total, MIN(attempted_at) AS oldest"
                " FROM auth_failures WHERE token_digest = ?",
                (token_digest,)).fetchone()
            retry_after = 0
            if by_source["total"] >= max_failures:
                retry_after = max(
                    retry_after, by_source["oldest"] + window_seconds - now)
            if by_digest["total"] >= max_failures:
                retry_after = max(
                    retry_after, by_digest["oldest"] + window_seconds - now)
            return max(1, retry_after) if retry_after else 0

    def record_auth_failure(self, source: str, token_digest: str, now: int,
                            window_seconds: int) -> None:
        """记录一次兑换失败；不保存原始 token。"""
        with self._transaction():
            self._connection.execute(
                "DELETE FROM auth_failures WHERE attempted_at <= ?", (now - window_seconds,))
            self._connection.execute(
                "INSERT INTO auth_failures (source, token_digest, attempted_at)"
                " VALUES (?, ?, ?)", (source, token_digest, now))

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------

    def create_conversation(self, actor: Identity, title: str = "新对话") -> ConversationRecord:
        workspace_id = self._require_actor(actor)
        self._require_ready_workspace(workspace_id)
        if not isinstance(title, str) or not title:
            raise DomainError("invalid_argument", 400)
        stamp = _now()
        conversation_id = uuid.uuid4().hex
        # 归属、禁用状态与空间状态在同一写事务内复查，避免检查后被禁用仍写入
        with self._transaction():
            self._require_actor(actor)
            self._require_ready_workspace(workspace_id)
            self._connection.execute(
                "INSERT INTO conversations"
                " (workspace_id, id, title, archived, created_at, updated_at)"
                " VALUES (?, ?, ?, 0, ?, ?)",
                (workspace_id, conversation_id, title, stamp, stamp))
        return ConversationRecord(conversation_id, workspace_id, title, False)

    def get_conversation(self, actor: Identity, conversation_id: str) -> ConversationRecord:
        workspace_id = self._require_actor(actor)
        row = self._connection.execute(
            "SELECT id, workspace_id, title, archived FROM conversations"
            " WHERE workspace_id = ? AND id = ? AND deleted_at IS NULL",
            (workspace_id, _text(conversation_id))).fetchone()
        if row is None:
            raise DomainError("not_found", 404)
        return _conversation_record(row)

    def list_conversations(self, actor: Identity, *, limit: int = 50,
                           offset: int = 0,
                           include_archived: bool = False) -> list[ConversationRecord]:
        """按更新时间倒序列出本空间目录；每页有上限，排序以 id 稳定打破平局。"""
        workspace_id = self._require_actor(actor)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise DomainError("invalid_argument", 400)
        if type(offset) is not int or offset < 0:
            raise DomainError("invalid_argument", 400)
        if not isinstance(include_archived, bool):
            raise DomainError("invalid_argument", 400)
        clauses = ["workspace_id = ?", "deleted_at IS NULL"]
        parameters: list[object] = [workspace_id]
        if not include_archived:
            clauses.append("archived = 0")
        rows = self._connection.execute(
            "SELECT id, workspace_id, title, archived FROM conversations"
            f" WHERE {' AND '.join(clauses)}"
            " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (*parameters, limit, offset)).fetchall()
        return [_conversation_record(row) for row in rows]

    def update_conversation(self, actor: Identity, conversation_id: str, *,
                            title: str | None = None,
                            archived: bool | None = None) -> ConversationRecord:
        """原子更新标题/归档标志；至少给出一项，删除后的会话为 404。"""
        workspace_id = self._require_actor(actor)
        if title is None and archived is None:
            raise DomainError("invalid_argument", 400)
        if title is not None and (not isinstance(title, str) or not title):
            raise DomainError("invalid_argument", 400)
        if archived is not None and not isinstance(archived, bool):
            raise DomainError("invalid_argument", 400)
        conversation_id = _text(conversation_id)
        stamp = _now()
        with self._transaction():
            # 身份/归属与变更共用同一写事务；事务外预检不能阻止并发禁用。
            self._require_actor(actor)
            row = self._connection.execute(
                "SELECT id, workspace_id, title, archived FROM conversations"
                " WHERE workspace_id = ? AND id = ? AND deleted_at IS NULL",
                (workspace_id, conversation_id)).fetchone()
            if row is None:
                raise DomainError("not_found", 404)
            if archived is True and not row["archived"]:
                busy = self._connection.execute(
                    "SELECT id FROM runs WHERE workspace_id = ?"
                    " AND conversation_id = ?"
                    f" AND status NOT IN ({_placeholders(TERMINAL_RUN_STATUSES)})"
                    " LIMIT 1",
                    (workspace_id, conversation_id,
                     *TERMINAL_RUN_STATUSES)).fetchone()
                if busy is not None:
                    raise DomainError("conversation_busy", 409)
            next_title = title if title is not None else row["title"]
            next_archived = (1 if archived is True
                             else 0 if archived is False
                             else row["archived"])
            self._connection.execute(
                "UPDATE conversations SET title = ?, archived = ?, updated_at = ?"
                " WHERE workspace_id = ? AND id = ? AND deleted_at IS NULL",
                (next_title, next_archived, stamp, workspace_id,
                 conversation_id))
        return ConversationRecord(
            conversation_id, workspace_id, next_title, bool(next_archived))

    def delete_conversation(self, actor: Identity, conversation_id: str) -> None:
        """软删除目录元数据；不删除 Run、原生历史或持久产物。"""
        workspace_id = self._require_actor(actor)
        conversation_id = _text(conversation_id)
        stamp = _now()
        with self._transaction():
            # 身份/归属与删除共用同一写事务；事务外预检不能阻止并发禁用。
            self._require_actor(actor)
            row = self._connection.execute(
                "SELECT id FROM conversations WHERE workspace_id = ? AND id = ?"
                " AND deleted_at IS NULL",
                (workspace_id, conversation_id)).fetchone()
            if row is None:
                raise DomainError("not_found", 404)
            busy = self._connection.execute(
                "SELECT id FROM runs WHERE workspace_id = ? AND conversation_id = ?"
                f" AND status NOT IN ({_placeholders(TERMINAL_RUN_STATUSES)}) LIMIT 1",
                (workspace_id, conversation_id, *TERMINAL_RUN_STATUSES)).fetchone()
            if busy is not None:
                raise DomainError("conversation_busy", 409)
            self._connection.execute(
                "UPDATE conversations SET deleted_at = ?, updated_at = ?"
                " WHERE workspace_id = ? AND id = ? AND deleted_at IS NULL",
                (stamp, stamp, workspace_id, conversation_id))

    # ------------------------------------------------------------------
    # 任务接纳
    # ------------------------------------------------------------------

    def lookup_run(self, actor: Identity, conversation_id: str,
                   client_request_id: str,
                   input_digest: str) -> RunRecord | None:
        """先按幂等键查询原 Run；同输入返回原记录，异输入 409。

        查询只发生在当前 actor/会话归属校验之后，不能借已有 request_id
        跳过身份检查；也不需要读取最新配置或要求新 worker 可用。
        """
        workspace_id = self._require_actor(actor)
        self._require_ready_workspace(workspace_id)
        conversation_id = _text(conversation_id)
        client_request_id = _text(client_request_id)
        input_digest = _text(input_digest)
        with self._transaction():
            self._require_actor(actor)
            self._require_ready_workspace(workspace_id)
            self._require_conversation(workspace_id, conversation_id)
            existing = self._connection.execute(
                "SELECT workspace_id, id, conversation_id, client_request_id,"
                " input_digest, status, config_version FROM runs"
                " WHERE workspace_id = ? AND client_request_id = ?",
                (workspace_id, client_request_id)).fetchone()
            if existing is None:
                return None
            if (existing["conversation_id"] != conversation_id
                    or existing["input_digest"] != input_digest):
                raise DomainError("request_conflict", 409)
            return _run_record(existing)

    def reserve_run(self, actor: Identity, conversation_id: str, client_request_id: str,
                    input_digest: str, config_version: int) -> RunRecord:
        """幂等接纳一次任务；重复请求返回原 Run，不重复占用并发预算。

        判定顺序固定为：授权 → 会话归属 → 幂等 → 每会话互斥 → 容量。
        幂等必须排在互斥与容量之前，否则合法重试会被挡下。
        """
        workspace_id = self._require_actor(actor)
        self._require_ready_workspace(workspace_id)
        conversation_id = _text(conversation_id)
        client_request_id = _text(client_request_id)
        input_digest = _text(input_digest)
        with self._transaction():
            # 授权、互斥与容量判定共用同一个写事务，避免检查后被禁用仍写入
            self._require_actor(actor)
            self._require_ready_workspace(workspace_id)
            self._require_conversation(workspace_id, conversation_id)
            existing = self._connection.execute(
                "SELECT workspace_id, id, conversation_id, client_request_id, input_digest,"
                " status, config_version FROM runs"
                " WHERE workspace_id = ? AND client_request_id = ?",
                (workspace_id, client_request_id)).fetchone()
            if existing is not None:
                # 幂等重试必须在互斥与容量检查之前识别，否则重试会被 409/429 挡住
                if (existing["conversation_id"] != conversation_id
                        or existing["input_digest"] != input_digest):
                    raise DomainError("request_conflict", 409)
                return _run_record(existing)
            self._require_conversation_free(workspace_id, conversation_id)
            self._require_capacity(workspace_id)
            run_id = uuid.uuid4().hex
            self._connection.execute(
                "INSERT INTO runs (workspace_id, id, conversation_id, client_request_id,"
                " input_digest, status, config_version, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?)",
                (workspace_id, run_id, conversation_id, client_request_id, input_digest,
                 int(config_version), _now()))
        return RunRecord(run_id, workspace_id, conversation_id, "reserved", int(config_version))

    def save_run_snapshot(self, actor: Identity, run_id: str,
                          envelope_json: str) -> str:
        """首次写入逐 Run 签名快照；重试读取原快照，绝不替换。

        快照固定接纳时的配置、会话与输入摘要。并发首次提交只有一行能写入，
        另一方读取同一份已签名内容，避免重试拼出第二份配置。
        """
        workspace_id = self._require_actor(actor)
        if (not isinstance(envelope_json, str) or not envelope_json
                or len(envelope_json) > 512 * 1024):
            raise DomainError("snapshot_invalid", 400)
        with self._transaction():
            self._require_actor(actor)
            row = self._connection.execute(
                "SELECT snapshot_json FROM runs"
                " WHERE workspace_id = ? AND id = ?",
                (workspace_id, _text(run_id))).fetchone()
            if row is None:
                raise DomainError("not_found", 404)
            if row["snapshot_json"] is not None:
                return row["snapshot_json"]
            self._connection.execute(
                "UPDATE runs SET snapshot_json = ?"
                " WHERE workspace_id = ? AND id = ? AND snapshot_json IS NULL",
                (envelope_json, workspace_id, run_id))
            return envelope_json

    def get_run_snapshot(self, actor: Identity, run_id: str) -> str | None:
        """读取已固定快照；查询不禁用用户也能读取，但不产生新快照。"""
        workspace_id = self._require_actor(actor, allow_disabled=True)
        row = self._connection.execute(
            "SELECT snapshot_json FROM runs"
            " WHERE workspace_id = ? AND id = ?",
            (workspace_id, _text(run_id))).fetchone()
        if row is None:
            raise DomainError("not_found", 404)
        value = row["snapshot_json"]
        return value if isinstance(value, str) else None

    def claim_run_preparation(self, actor: Identity, run_id: str,
                              owner: str, now: int, ttl_seconds: int) -> bool:
        """为同一 Run 建立唯一、持久、不可接管的准备所有权。

        只有第一个并发提交能签发 token/固定快照；其他提交读取原 admission。
        所有权不因租约过期而被另一请求接管：持有者崩溃后由后台对账按原
        Run 收尾，绝不盲目重发业务。
        """
        workspace_id = self._require_actor(actor)
        if (not isinstance(owner, str) or not owner or len(owner) > 128
                or not owner.isascii()):
            raise DomainError("invalid_argument", 400)
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
            raise DomainError("invalid_argument", 400)
        now = int(now)
        with self._transaction():
            self._require_actor(actor)
            cursor = self._connection.execute(
                "UPDATE runs SET prepare_owner = ?, prepare_expires_at = ?"
                " WHERE workspace_id = ? AND id = ? AND prepare_owner IS NULL"
                " AND capacity_released_at IS NULL AND status NOT IN"
                f" ({_placeholders(TERMINAL_RUN_STATUSES)})",
                (owner, now + ttl_seconds, workspace_id, _text(run_id),
                 *TERMINAL_RUN_STATUSES))
            return cursor.rowcount == 1

    def claim_run_dispatch(self, actor: Identity, run_id: str, nonce: str,
                           container_id: str, *,
                           prepare_owner: str | None = None,
                           now: int | None = None) -> bool:
        """原子认领唯一派发；只有 pending 行能进入 dispatching。

        认领在数据库事务内完成，两个并发提交同输入时只有一个返回 True；
        另一个必须读取持久状态，不能再次向 worker 发布任务。提供
        ``prepare_owner`` 时只有仍持有准备所有权的调用者可以派发；成功后
        清除准备标记。
        """
        workspace_id = self._require_actor(actor)
        if (not isinstance(nonce, str) or not nonce or len(nonce) > 128
                or not isinstance(container_id, str) or not container_id
                or len(container_id) > 256):
            raise DomainError("invalid_argument", 400)
        where = (
            " WHERE workspace_id = ? AND id = ? AND dispatch_state = 'pending'"
            " AND status NOT IN"
            f" ({_placeholders(TERMINAL_RUN_STATUSES)})")
        values: list[object] = [nonce, container_id, _now(), workspace_id,
                                _text(run_id), *TERMINAL_RUN_STATUSES]
        if prepare_owner is not None:
            if (not isinstance(prepare_owner, str) or not prepare_owner
                    or len(prepare_owner) > 128):
                raise DomainError("invalid_argument", 400)
            if type(now) is not int:
                raise DomainError("invalid_argument", 400)
            where += (" AND prepare_owner = ?"
                      " AND prepare_expires_at IS NOT NULL"
                      " AND prepare_expires_at > ?")
            values.extend((prepare_owner, now))
        with self._transaction():
            self._require_actor(actor)
            cursor = self._connection.execute(
                "UPDATE runs SET dispatch_state = 'dispatching',"
                " dispatch_nonce = ?, worker_container_id = ?, dispatched_at = ?,"
                " prepare_owner = NULL, prepare_expires_at = NULL"
                + where,
                tuple(values))
            return cursor.rowcount == 1

    def mark_run_dispatched(self, actor: Identity, run_id: str,
                            nonce: str) -> bool:
        """worker 已接受任务后落持久标记；只匹配本次认领的 nonce。"""
        workspace_id = self._require_actor(actor, allow_disabled=True)
        if not isinstance(nonce, str) or not nonce:
            raise DomainError("invalid_argument", 400)
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE runs SET dispatch_state = 'dispatched'"
                " WHERE workspace_id = ? AND id = ?"
                " AND dispatch_state = 'dispatching' AND dispatch_nonce = ?"
                " AND status NOT IN"
                f" ({_placeholders(TERMINAL_RUN_STATUSES)})",
                (workspace_id, _text(run_id), nonce, *TERMINAL_RUN_STATUSES))
            return cursor.rowcount == 1

    def run_dispatch_state(self, actor: Identity, run_id: str) -> dict:
        """读取派发与任务状态的窄视图；供重试对账，不创建新执行。"""
        workspace_id = self._require_actor(actor, allow_disabled=True)
        row = self._connection.execute(
            "SELECT id, status, dispatch_state, dispatch_nonce, config_version,"
            " input_digest, client_request_id, worker_container_id FROM runs"
            " WHERE workspace_id = ? AND id = ?",
            (workspace_id, _text(run_id))).fetchone()
        if row is None:
            raise DomainError("not_found", 404)
        return {
            "runId": row["id"],
            "status": row["status"],
            "dispatchState": row["dispatch_state"],
            "dispatchNonce": row["dispatch_nonce"] or "",
            "workerContainerId": row["worker_container_id"] or "",
            "configVersion": int(row["config_version"]),
            "inputDigest": row["input_digest"],
            "clientRequestId": row["client_request_id"],
        }

    def get_run(self, actor: Identity, run_id: str) -> RunRecord:
        """读取本空间单个任务；任务查询属于收尾路径，不禁用用户也能读取。"""
        workspace_id = self._require_actor(actor, allow_disabled=True)
        return self._fetch_run(workspace_id, run_id)

    def assert_run(self, actor: Identity, run_id: str) -> RunRecord:
        """会话/任务续接前的当前身份校验；禁用用户不能继续获得任务句柄。"""
        workspace_id = self._require_actor(actor)
        return self._fetch_run(workspace_id, _text(run_id))

    def list_runs(self, actor: Identity, conversation_id: str) -> list[RunRecord]:
        """按创建顺序列出本空间某个会话的任务。"""
        workspace_id = self._require_actor(actor)
        rows = self._connection.execute(
            "SELECT workspace_id, id, conversation_id, client_request_id, input_digest,"
            " status, config_version FROM runs"
            " WHERE workspace_id = ? AND conversation_id = ? ORDER BY created_at, id",
            (workspace_id, _text(conversation_id))).fetchall()
        return [_run_record(row) for row in rows]

    def list_run_page(self, actor: Identity, conversation_id: str, *,
                      limit: int = 50, offset: int = 0) -> list[RunRecord]:
        """稳定分页读取本人会话的 Run 元数据；不含 token/签名/内部地址。"""
        workspace_id = self._require_actor(actor, allow_disabled=True)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise DomainError("invalid_argument", 400)
        if type(offset) is not int or offset < 0:
            raise DomainError("invalid_argument", 400)
        self._require_conversation(workspace_id, _text(conversation_id))
        rows = self._connection.execute(
            "SELECT workspace_id, id, conversation_id, client_request_id,"
            " input_digest, status, config_version FROM runs"
            " WHERE workspace_id = ? AND conversation_id = ?"
            " ORDER BY created_at, rowid LIMIT ? OFFSET ?",
            (workspace_id, conversation_id, int(limit),
             int(offset))).fetchall()
        return [_run_record(row) for row in rows]

    def latest_run_for_conversation(self, actor: Identity,
                                    conversation_id: str) -> RunRecord | None:
        """读取会话最近一次 Run；用于定位本人 worker，不产生新执行。"""
        workspace_id = self._require_actor(actor, allow_disabled=True)
        row = self._connection.execute(
            "SELECT workspace_id, id, conversation_id, client_request_id, input_digest,"
            " status, config_version FROM runs"
            " WHERE workspace_id = ? AND conversation_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (workspace_id, _text(conversation_id))).fetchone()
        return _run_record(row) if row is not None else None

    def latest_dispatched_run_for_conversation(
            self, actor: Identity, conversation_id: str) -> RunRecord | None:
        """最近一次已绑定 worker 实例的 Run；用于读取无副作用的消息快照。"""
        workspace_id = self._require_actor(actor, allow_disabled=True)
        row = self._connection.execute(
            "SELECT workspace_id, id, conversation_id, client_request_id,"
            " input_digest, status, config_version FROM runs"
            " WHERE workspace_id = ? AND conversation_id = ?"
            " AND worker_container_id IS NOT NULL AND worker_container_id != ''"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (workspace_id, _text(conversation_id))).fetchone()
        return _run_record(row) if row is not None else None

    def list_active_run_bindings(self) -> list[dict]:
        """后台对账的窄视图；跨 workspace 列出未终态 Run 与原实例绑定。

        仅供 control 进程内的持久对账使用，不经 HTTP 暴露；不返回凭证。
        """
        rows = self._connection.execute(
            "SELECT workspace_id, id, conversation_id, status, config_version,"
            " dispatch_state, dispatch_nonce, worker_container_id, created_at,"
            " dispatched_at, prepare_owner, prepare_expires_at,"
            " snapshot_json FROM runs"
            f" WHERE status NOT IN ({_placeholders(TERMINAL_RUN_STATUSES)})"
            " ORDER BY created_at, rowid",
            TERMINAL_RUN_STATUSES).fetchall()
        return [{
            "workspaceId": row["workspace_id"],
            "runId": row["id"],
            "conversationId": row["conversation_id"],
            "status": row["status"],
            "configVersion": int(row["config_version"]),
            "dispatchState": row["dispatch_state"],
            "dispatchNonce": row["dispatch_nonce"] or "",
            # 保留 NULL 与空串的区别：后台 CAS 必须比较原值
            "workerContainerId": row["worker_container_id"],
            "createdAt": int(row["created_at"]),
            "dispatchedAt": (int(row["dispatched_at"])
                             if row["dispatched_at"] is not None else None),
            "prepareOwner": row["prepare_owner"],
            "prepareExpiresAt": (int(row["prepare_expires_at"])
                                 if row["prepare_expires_at"] is not None
                                 else None),
            "snapshotJson": row["snapshot_json"],
        } for row in rows]

    def sync_run_status(self, workspace_id: str, run_id: str,
                        status: str) -> bool:
        """后台对账写回状态；终态只释放一次容量，非终态不回退终态。

        无 actor 的系统路径：只有监视器调用，所有条件仍在单个事务内判定。
        """
        if status not in (*ACTIVE_RUN_STATUSES, *TERMINAL_RUN_STATUSES):
            raise DomainError("invalid_status", 400)
        workspace_id = _text(workspace_id)
        run_id = _text(run_id)
        stamp = _now()
        with self._transaction():
            if status in TERMINAL_RUN_STATUSES:
                cursor = self._connection.execute(
                    "UPDATE runs SET status = ?, finished_at = ?,"
                    " capacity_released_at = ?,"
                    " release_count = release_count + 1"
                    " WHERE workspace_id = ? AND id = ?"
                    " AND capacity_released_at IS NULL AND status NOT IN"
                    f" ({_placeholders(TERMINAL_RUN_STATUSES)})",
                    (status, stamp, stamp, workspace_id, run_id,
                     *TERMINAL_RUN_STATUSES))
                if cursor.rowcount == 1:
                    self._revoke_tokens_locked(
                        workspace_id, run_id, stamp)
            else:
                cursor = self._connection.execute(
                    "UPDATE runs SET status = ? WHERE workspace_id = ?"
                    " AND id = ? AND status NOT IN"
                    f" ({_placeholders(TERMINAL_RUN_STATUSES)})",
                    (status, workspace_id, run_id, *TERMINAL_RUN_STATUSES))
            return cursor.rowcount == 1

    def interrupt_unstarted_run(
            self, workspace_id: str, run_id: str, *,
            expected_status: str, expected_dispatch_state: str,
            expected_container: str | None,
            expected_prepare_owner: str | None,
            expected_prepare_expires_at: int | None) -> bool:
        """原子收尾“仍未派发”的 Run；旧扫描不得改写已准备/已派发的行。

        所有观察前提（状态、派发状态、实例绑定、准备所有者/期限）都在同一
        写事务内比较；任一字段变化即放弃本次判断。容量释放与 token 撤销也
        在同一事务提交，绝不基于进程内锁或事后再查询。
        """
        workspace_id = _text(workspace_id)
        run_id = _text(run_id)
        if not isinstance(expected_status, str) or not expected_status:
            raise DomainError("invalid_argument", 400)
        if not isinstance(expected_dispatch_state, str) \
                or not expected_dispatch_state:
            raise DomainError("invalid_argument", 400)
        stamp = _now()
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE runs SET status = 'interrupted', finished_at = ?,"
                " capacity_released_at = ?,"
                " release_count = release_count + 1"
                " WHERE workspace_id = ? AND id = ? AND status = ?"
                " AND capacity_released_at IS NULL AND status NOT IN"
                f" ({_placeholders(TERMINAL_RUN_STATUSES)})"
                " AND dispatch_state = ?"
                " AND worker_container_id IS ?"
                " AND prepare_owner IS ?"
                " AND prepare_expires_at IS ?",
                (stamp, stamp, workspace_id, run_id, expected_status,
                 *TERMINAL_RUN_STATUSES, expected_dispatch_state,
                 expected_container, expected_prepare_owner,
                 expected_prepare_expires_at))
            if cursor.rowcount != 1:
                return False
            self._revoke_tokens_locked(workspace_id, run_id, stamp)
            return True

    def count_active_runs(self) -> int:
        """整机当前活跃任务数。"""
        row = self._connection.execute(
            "SELECT COUNT(*) AS total FROM runs"
            f" WHERE status NOT IN ({_placeholders(TERMINAL_RUN_STATUSES)})",
            TERMINAL_RUN_STATUSES).fetchone()
        return row["total"]

    # ------------------------------------------------------------------
    # 任务状态推进
    # ------------------------------------------------------------------

    def mark_running(self, actor: Identity, run_id: str) -> RunRecord:
        """把已接纳的任务推进到 running；终态不能被重新激活。"""
        workspace_id = self._require_actor(actor)
        with self._transaction():
            current = self._fetch_run(workspace_id, run_id)
            if current.status in TERMINAL_RUN_STATUSES:
                raise DomainError("terminal_run", 409)
            if current.status != "running":
                self._connection.execute(
                    "UPDATE runs SET status = 'running'"
                    " WHERE workspace_id = ? AND id = ? AND status NOT IN"
                    f" ({_placeholders(TERMINAL_RUN_STATUSES)})",
                    (workspace_id, run_id, *TERMINAL_RUN_STATUSES))
            return self._fetch_run(workspace_id, run_id)

    def mark_awaiting_permission(self, actor: Identity, run_id: str) -> RunRecord:
        """任务进入权限/问答等待；仍属于活跃状态，继续占用容量。

        终态不可回退到等待；重复推进是幂等的，回放不改变任务状态。
        """
        workspace_id = self._require_actor(actor)
        with self._transaction():
            current = self._fetch_run(workspace_id, run_id)
            if current.status in TERMINAL_RUN_STATUSES:
                raise DomainError("terminal_run", 409)
            self._connection.execute(
                "UPDATE runs SET status = 'awaiting_permission'"
                " WHERE workspace_id = ? AND id = ? AND status NOT IN"
                f" ({_placeholders(TERMINAL_RUN_STATUSES)})",
                (workspace_id, run_id, *TERMINAL_RUN_STATUSES))
            return self._fetch_run(workspace_id, run_id)

    def mark_interrupted(self, actor: Identity, run_id: str) -> bool:
        """worker 异常退出后把残留任务标为 interrupted；重复调用返回 False。"""
        # 收尾路径不因用户被禁用而失败，否则容量会被永久占用
        workspace_id = self._require_actor(actor, allow_disabled=True)
        stamp = _now()
        with self._transaction():
            current = self._fetch_run(workspace_id, run_id)
            if current.status not in ACTIVE_RUN_STATUSES:
                return False
            self._connection.execute(
                "UPDATE runs SET status = 'interrupted', finished_at = ?,"
                " capacity_released_at = ?, release_count = release_count + 1"
                " WHERE workspace_id = ? AND id = ? AND status NOT IN"
                f" ({_placeholders(TERMINAL_RUN_STATUSES)})",
                (stamp, stamp, workspace_id, run_id, *TERMINAL_RUN_STATUSES))
            self._revoke_tokens_locked(workspace_id, run_id, stamp)
        return True

    def finish_run(self, actor: Identity, run_id: str, status: str) -> None:
        """结束任务并释放并发额度；可安全重复调用，终态不会被改写。"""
        if status not in TERMINAL_RUN_STATUSES:
            raise DomainError("invalid_status", 400)
        # 已接纳任务的内部收尾仍需可执行，否则禁用用户会永久占用容量
        workspace_id = self._require_actor(actor, allow_disabled=True)
        stamp = _now()
        with self._transaction():
            self._fetch_run(workspace_id, run_id)
            # 只有尚未释放容量的行会被改写：重复调用既不换状态也不重复释放
            cursor = self._connection.execute(
                "UPDATE runs SET status = ?, finished_at = ?, capacity_released_at = ?,"
                " release_count = release_count + 1"
                " WHERE workspace_id = ? AND id = ? AND capacity_released_at IS NULL"
                " AND status NOT IN"
                f" ({_placeholders(TERMINAL_RUN_STATUSES)})",
                (status, stamp, stamp, workspace_id, run_id, *TERMINAL_RUN_STATUSES))
            if cursor.rowcount == 1:
                # 终态与 token 撤销同事务：控制进程在两者之间崩溃也不会
                # 留下仍可调用的 Run 凭证。
                self._revoke_tokens_locked(workspace_id, run_id, stamp)

    # ------------------------------------------------------------------
    # 内部校验
    # ------------------------------------------------------------------

    def _fetch_run(self, workspace_id: str, run_id: str) -> RunRecord:
        row = self._connection.execute(
            "SELECT workspace_id, id, conversation_id, client_request_id, input_digest,"
            " status, config_version FROM runs WHERE workspace_id = ? AND id = ?",
            (workspace_id, _text(run_id))).fetchone()
        if row is None:
            raise DomainError("not_found", 404)
        return _run_record(row)

    def _require_conversation(self, workspace_id: str, conversation_id: str) -> None:
        row = self._connection.execute(
            "SELECT archived, deleted_at FROM conversations"
            " WHERE workspace_id = ? AND id = ?",
            (workspace_id, conversation_id)).fetchone()
        if row is None or row["deleted_at"] is not None:
            raise DomainError("not_found", 404)
        if row["archived"]:
            raise DomainError("conversation_archived", 409)

    def _require_conversation_free(self, workspace_id: str, conversation_id: str) -> None:
        """同一会话同时只允许一个活跃 prompt。

        活跃定义为尚未进入终态；终态任务不再占用会话额度，因此同一会话可以
        在上一轮结束后继续下一轮。
        """
        busy = self._connection.execute(
            "SELECT id FROM runs WHERE workspace_id = ? AND conversation_id = ?"
            f" AND status NOT IN ({_placeholders(TERMINAL_RUN_STATUSES)}) LIMIT 1",
            (workspace_id, conversation_id, *TERMINAL_RUN_STATUSES)).fetchone()
        if busy is not None:
            raise DomainError("conversation_busy", 409)

    def _require_capacity(self, workspace_id: str) -> None:
        """在事务内原子检查每空间 2 / 全局 8 的活跃任务预算。"""
        workspace_active = self._connection.execute(
            "SELECT COUNT(*) AS total FROM runs WHERE workspace_id = ? AND status NOT IN"
            f" ({_placeholders(TERMINAL_RUN_STATUSES)})",
            (workspace_id, *TERMINAL_RUN_STATUSES)).fetchone()["total"]
        if workspace_active >= self.max_active_runs_per_workspace:
            raise DomainError("workspace_capacity", 429)
        if self.count_active_runs() >= self.max_active_runs_total:
            raise DomainError("global_capacity", 429)

    def _require_actor(self, actor: Identity, *, allow_disabled: bool = False) -> str:
        """按 user_id + workspace_id 重查归属，返回该 workspace_id。

        Identity 只是传输对象：用户必须存在，workspace 必须确实属于该用户。
        本方法**不**检查 workspace 状态，也不默认检查用户是否被禁用：
        收尾路径（``allow_disabled``）必须在空间 failed/stopped 或用户被禁用时
        仍可执行，否则已接纳任务无法进入终态、并发额度被永久占用。
        接纳新任务的入口必须另外调用 :meth:`_require_ready_workspace`。
        """
        user_id = _text(actor.user_id)
        workspace_id = _text(actor.workspace_id)
        user = self._connection.execute(
            "SELECT disabled_at FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            raise DomainError("unauthorized", 403)
        if user["disabled_at"] is not None and not allow_disabled:
            raise DomainError("user_disabled", 403)
        workspace = self._connection.execute(
            "SELECT owner_user_id, state FROM workspaces WHERE id = ?",
            (workspace_id,)).fetchone()
        if workspace is None or workspace["owner_user_id"] != user_id:
            raise DomainError("unauthorized", 403)
        return workspace_id

    def _require_ready_workspace(self, workspace_id: str) -> None:
        """只有 ready 空间接纳新任务；非 ready 不影响已接纳任务的收尾。"""
        row = self._connection.execute(
            "SELECT state FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if row is None:
            raise DomainError("unauthorized", 403)
        if row["state"] not in READY_WORKSPACE_STATES:
            raise DomainError("workspace_unavailable", 403)

    # ------------------------------------------------------------------
    # 事务
    # ------------------------------------------------------------------

    @contextmanager
    def _read_transaction(self) -> Iterator[None]:
        """显式只读事务，为多行读取提供一致快照。"""
        self._connection.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """显式写事务；任何异常整体回滚。"""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")


def _worker_lease(row: sqlite3.Row | None) -> WorkerLease | None:
    """把租约行映射为不可变类型；凭证字段只在内存中使用，不进入 repr。"""
    if row is None:
        return None
    return WorkerLease(
        workspace_id=row["workspace_id"],
        container_id=row["container_id"],
        config_version=int(row["config_version"]),
        state=row["state"],
        heartbeat_at=int(row["heartbeat_at"]),
        started_at=int(row["started_at"]),
        owner_id=row["owner_id"],
        generation=int(row["generation"]),
        lease_expires_at=int(row["lease_expires_at"]),
        updated_at=int(row["updated_at"]),
        last_activity_at=int(row["last_activity_at"]),
        active_requests=int(row["active_requests"]),
        upstream=row["upstream"],
        service_token=row["service_token"],
    )


def _is_lock_contention(error: sqlite3.OperationalError) -> bool:
    """区分可恢复的锁竞争与其他 OperationalError。

    Python 3.10 的 ``sqlite3.OperationalError`` 没有 ``sqlite_errorcode``，
    因此只能按 SQLite 的稳定错误文本判断；命中范围限定为 SQLITE_BUSY 与
    SQLITE_LOCKED 两类消息。
    """
    message = str(error).lower()
    return "locked" in message or "busy" in message


def _read_version_without_side_effects(path: Path) -> int:
    """用只读连接读取 ``user_version``，不触发 WAL checkpoint。

    ``mode=ro`` 会正常读取 WAL 中已提交的内容，因此"待恢复的 WAL 里写着未来
    版本"也能被正确识别；``immutable=1`` 会忽略 WAL，读到过期版本，不能用于
    这里的版本判定。只读访问可能更新共享内存的读者槽，这是读者记账，
    不属于业务写入，也不会合并或删除待恢复的 WAL。
    """
    connection = sqlite3.connect(_read_only_uri(path), uri=True)
    try:
        return connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()


def _read_only_uri(path: Path) -> str:
    """构造只读连接 URI；用绝对 Path 的标准 file URI 编码，保证与读写连接
    指向同一文件（相对路径先转为绝对路径，保留现有兼容性）。"""
    return f"{Path(os.path.abspath(path)).as_uri()}?mode=ro"


def _conversation_record(row: sqlite3.Row) -> ConversationRecord:
    return ConversationRecord(
        row["id"], row["workspace_id"], row["title"], bool(row["archived"]))


def _run_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(row["id"], row["workspace_id"], row["conversation_id"],
                     row["status"], row["config_version"])


def _restrict_directory(directory: Path) -> None:
    """控制库父目录仅服务账号可写；Windows 不使用 POSIX 权限位，跳过。"""
    if os.name != "posix":
        return
    try:
        directory.chmod(0o700)
    except OSError:  # 已由部署策略（systemd、容器挂载）约束时不必失败
        pass
