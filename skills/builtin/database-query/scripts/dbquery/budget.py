"""本机 SQLite 运行预算与并发租约。

预算状态放在本地 SQLite（WAL + ``BEGIN IMMEDIATE``），因此不同 CLI 进程共享同一
份计数，而不是每次调用清零的内存计数。运行必须由 ``runs new`` 显式创建；未知
运行 ID 不会自动新建。预算状态不可用时一律 fail-closed。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy.engine import make_url

from dbquery.errors import DomainError
from dbquery.types import ResolvedSource

logger = logging.getLogger(__name__)

MAX_CALLS_PER_RUN = 40
MAX_ROWS_PER_RUN = 10000
MAX_PER_TARGET = 2
MAX_GLOBAL = 8
LEASE_SECONDS = 30
RUN_TTL_SECONDS = 86400
_MAX_TTL_SECONDS = 86400
_MAX_ROWS_PER_CALL = 1000

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS runs (
      run_id TEXT PRIMARY KEY,
      deadline INTEGER NOT NULL,
      calls INTEGER NOT NULL DEFAULT 0,
      rows INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reservations (
      token TEXT PRIMARY KEY,
      run_id TEXT NOT NULL,
      reserved_rows INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS leases (
      token TEXT PRIMARY KEY,
      target_key TEXT NOT NULL,
      expires_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS leases_target ON leases(target_key)",
)


@dataclass(frozen=True, slots=True)
class RunInfo:
    """一个运行的当前计数与到期时间。"""

    run_id: str
    deadline: int
    calls: int
    rows: int


@dataclass(frozen=True, slots=True)
class Reservation:
    """一次调用的行数预留；结算前计数按预留值计入。"""

    token: str
    run_id: str
    reserved_rows: int


@dataclass(frozen=True, slots=True)
class Lease:
    """一次查询的并发租约（按目标与全局计数，30 秒到期）。"""

    token: str
    target_key: str


class LocalBudget:
    """失败关闭的本地运行预算。"""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path)
        self._clock = clock
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise _unavailable("state_dir") from None
        self._initialize()

    def create_run(self, run_id: str, ttl_seconds: int = RUN_TTL_SECONDS) -> RunInfo:
        """显式创建一个运行；重复 ID 不重置计数。"""
        if _RUN_ID_RE.fullmatch(run_id) is None:
            raise _invalid_argument("run_id")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= _MAX_TTL_SECONDS:
            raise _invalid_argument("ttl_seconds")
        deadline = int(self._clock()) + ttl_seconds
        with self._tx() as connection:
            try:
                connection.execute(
                    "INSERT INTO runs (run_id, deadline) VALUES (?, ?)", (run_id, deadline)
                )
            except sqlite3.IntegrityError:
                raise DomainError("database.run_exists", "运行已存在", status=409) from None
        return RunInfo(run_id, deadline, 0, 0)

    def run_info(self, run_id: str) -> RunInfo:
        with self._tx() as connection:
            row = connection.execute(
                "SELECT deadline, calls, rows FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise _run_not_found()
        return RunInfo(run_id, int(row[0]), int(row[1]), int(row[2]))

    def reserve(self, run_id: str, requested_rows: int) -> Reservation:
        """预留一次调用与若干行；调用数与累计行数都在同一事务内判定。"""
        if type(requested_rows) is not int or not 0 <= requested_rows <= _MAX_ROWS_PER_CALL:
            raise _invalid_argument("requested_rows")
        now = self._clock()
        with self._tx() as connection:
            row = connection.execute(
                "SELECT deadline, calls, rows FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise _run_not_found()
            deadline, calls, rows = int(row[0]), int(row[1]), int(row[2])
            if deadline <= now:
                raise DomainError("database.run_expired", "运行已过期", status=410)
            if calls >= MAX_CALLS_PER_RUN or rows + requested_rows > MAX_ROWS_PER_RUN:
                raise DomainError("database.budget_exceeded", "数据库调用额度已用尽", status=429)
            token = uuid4().hex
            connection.execute(
                "UPDATE runs SET calls = calls + 1, rows = rows + ? WHERE run_id = ?",
                (requested_rows, run_id),
            )
            connection.execute(
                "INSERT INTO reservations (token, run_id, reserved_rows) VALUES (?, ?, ?)",
                (token, run_id, requested_rows),
            )
        return Reservation(token, run_id, requested_rows)

    def settle(self, reservation: Reservation, returned_rows: int) -> None:
        """结算预留：把未使用的行数退回；缺失预留按预算不可用处理。"""
        if type(returned_rows) is not int or returned_rows < 0:
            raise ValueError("返回行数无效")
        with self._tx() as connection:
            row = connection.execute(
                "SELECT reserved_rows FROM reservations WHERE token = ? AND run_id = ?",
                (reservation.token, reservation.run_id),
            ).fetchone()
            if row is None:
                raise _unavailable("reservation_missing")
            reserved = int(row[0])
            if returned_rows > reserved:
                raise ValueError("返回行数超出预留额度")
            connection.execute(
                "UPDATE runs SET rows = rows + ? WHERE run_id = ?",
                (returned_rows - reserved, reservation.run_id),
            )
            connection.execute("DELETE FROM reservations WHERE token = ?", (reservation.token,))

    def target_key(self, source: ResolvedSource) -> str:
        """按 host/port/schema 分组；连接别名与口令不改变目标键。"""
        url = make_url(source.url.get_secret_value())
        target = json.dumps(
            [(url.host or "").lower(), url.port or 3306, source.allowed_schema],
            separators=(",", ":"),
        )
        return hashlib.sha256(target.encode()).hexdigest()

    def acquire(self, target_key: str, run_id: str) -> Lease:
        """获取并发租约：单目标 2、全局 8；过期租约先回收。"""
        now = self._clock()
        with self._tx() as connection:
            row = connection.execute(
                "SELECT deadline FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise _run_not_found()
            if int(row[0]) <= now:
                raise DomainError("database.run_expired", "运行已过期", status=410)
            connection.execute("DELETE FROM leases WHERE expires_at <= ?", (now,))
            per_target = connection.execute(
                "SELECT COUNT(*) FROM leases WHERE target_key = ?", (target_key,)
            ).fetchone()
            total = connection.execute("SELECT COUNT(*) FROM leases").fetchone()
            if int(per_target[0]) >= MAX_PER_TARGET or int(total[0]) >= MAX_GLOBAL:
                raise DomainError("database.busy", "数据库查询繁忙", status=429)
            token = uuid4().hex
            connection.execute(
                "INSERT INTO leases (token, target_key, expires_at) VALUES (?, ?, ?)",
                (token, target_key, now + LEASE_SECONDS),
            )
        return Lease(token, target_key)

    def release(self, lease: Lease) -> None:
        with self._tx() as connection:
            connection.execute("DELETE FROM leases WHERE token = ?", (lease.token,))

    def _initialize(self) -> None:
        with self._tx() as connection:
            for statement in _SCHEMA:
                connection.execute(statement)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        except sqlite3.Error:
            logger.warning("本地预算状态不可用：%s", type(self._path).__name__)
            raise _unavailable("sqlite") from None
        finally:
            if connection is not None:
                connection.close()


def _run_not_found() -> DomainError:
    return DomainError("database.run_not_found", "运行不存在", status=404)


def _invalid_argument(field: str) -> DomainError:
    return DomainError("database.invalid_argument", f"参数无效（{field}）", status=400)


def _unavailable(detail: str) -> DomainError:
    return DomainError("database.budget_unavailable", f"本地预算状态不可用（{detail}）", status=503)
