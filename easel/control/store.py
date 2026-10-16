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

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal

from .types import ConversationRecord, DomainError, Identity, RunRecord

SCHEMA_VERSION = 1
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
    # 会话
    # ------------------------------------------------------------------

    def create_conversation(self, actor: Identity, title: str = "新对话") -> ConversationRecord:
        workspace_id = self._require_actor(actor)
        self._require_ready_workspace(workspace_id)
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
        return ConversationRecord(conversation_id, workspace_id, title)

    def get_conversation(self, actor: Identity, conversation_id: str) -> ConversationRecord:
        workspace_id = self._require_actor(actor)
        row = self._connection.execute(
            "SELECT id, workspace_id, title FROM conversations WHERE workspace_id = ? AND id = ?",
            (workspace_id, _text(conversation_id))).fetchone()
        if row is None:
            raise DomainError("not_found", 404)
        return ConversationRecord(row["id"], row["workspace_id"], row["title"])

    # ------------------------------------------------------------------
    # 任务接纳
    # ------------------------------------------------------------------

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

    def get_run(self, actor: Identity, run_id: str) -> RunRecord:
        """读取本空间单个任务；任务查询属于收尾路径，不禁用用户也能读取。"""
        workspace_id = self._require_actor(actor, allow_disabled=True)
        return self._fetch_run(workspace_id, run_id)

    def list_runs(self, actor: Identity, conversation_id: str) -> list[RunRecord]:
        """按创建顺序列出本空间某个会话的任务。"""
        workspace_id = self._require_actor(actor)
        rows = self._connection.execute(
            "SELECT workspace_id, id, conversation_id, client_request_id, input_digest,"
            " status, config_version FROM runs"
            " WHERE workspace_id = ? AND conversation_id = ? ORDER BY created_at, id",
            (workspace_id, _text(conversation_id))).fetchall()
        return [_run_record(row) for row in rows]

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
            self._connection.execute(
                "UPDATE runs SET status = ?, finished_at = ?, capacity_released_at = ?,"
                " release_count = release_count + 1"
                " WHERE workspace_id = ? AND id = ? AND capacity_released_at IS NULL"
                " AND status NOT IN"
                f" ({_placeholders(TERMINAL_RUN_STATUSES)})",
                (status, stamp, stamp, workspace_id, run_id, *TERMINAL_RUN_STATUSES))

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
            "SELECT id FROM conversations WHERE workspace_id = ? AND id = ?",
            (workspace_id, conversation_id)).fetchone()
        if row is None:
            raise DomainError("not_found", 404)

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
    """构造只读连接 URI；Windows 路径使用正斜杠并转义 URI 保留字符。"""
    raw = path.as_posix().replace("?", "%3f").replace("#", "%23")
    return f"file:{raw}?mode=ro"


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
