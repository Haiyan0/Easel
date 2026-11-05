"""T3 执行层生命周期与结果形状单元测试（规格 §4/§5.1/§6/§7.1/§8/§11）。

全部用例**不建真实连接**：以可观测的假 engine/connection/result 驱动
``execute_readonly`` / ``describe_table``，断言

1. §6 的固定顺序：建池/连接 → 只读与超时设置（含读回校验）→ 元数据检查 →
   分块有界读取 → 关闭结果 → 回滚 → 关闭连接 → ``await engine.dispose()``；
2. 每条失败路径（建连、执行、读取、输出编码、超时、取消、清理异常）都释放已创建
   资源，且**清理失败不跳过后续清理**；清理异常不掩盖原始错误；
3. 清理**被真正等待**（dispose 的完成事件在 await 之后才记账），取消路径在清理完成
   之后才重新抛出取消异常 —— 这是 §11 阻断项「取消和超时路径没有真实关闭证据」的
   单元侧证据；真实驱动侧的物理关闭证据见
   ``tests/integration/database/test_mysql_readonly.py``（管理员观察连接）。
4. §4 序列化规则、§7.1 边界数值与 ``max_rows + 1`` 截断探针约定。

真实驱动的行为（只读事务拦写、驱动多语句被拒、服务端超时中断、会话退出）无法用假件
证明，一律由集成测试覆盖；本文件只证明**编排正确性**。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError, ProgrammingError

from dbquery import mysql
from dbquery.errors import DomainError
from dbquery.mysql import (
    CHUNK_ROWS,
    MAX_FIELD_BYTES,
    MAX_OUTPUT_BYTES,
    TRUNCATION_BINARY_FIELD,
    TRUNCATION_FIELD_BYTES,
    TRUNCATION_OUTPUT_BYTES,
    TRUNCATION_ROW_LIMIT,
    QueryLimits,
    check_connection,
    describe_table,
    execute_readonly,
)
from dbquery.policy import DATABASE_LIMIT_EXCEEDED, validate_query
from dbquery.types import (
    ColumnDescription,
    IndexDescription,
    QueryPage,
    ResolvedSource,
    TableDescription,
)

SCHEMA = "smart_assistant_test"
TABLE = "t3_orders"
# 假凭据（金丝雀）：断言错误不外泄连接串（规格 §8）
CANARY_PASSWORD = "canary-t3-password"
CANARY_HOST = "t3-secret-host.internal"

COLUMNS_MARKER = "information_schema.COLUMNS"
TABLES_MARKER = "information_schema.TABLES"
STATISTICS_MARKER = "information_schema.STATISTICS"

# 假件「首包前挂起」的**上限**：远大于用例里的客户端预算（0.05 秒）与取消延迟，
# 但有限 —— 实现若没能打断读取，1 秒后挂起自动放行，事件被记账、断言变红（而不是挂死）。
STREAM_GATE_SECONDS = 1.0


def _source(*, allowed_tables: tuple[str, ...] = (TABLE,), schema: str = SCHEMA) -> ResolvedSource:
    return ResolvedSource(
        connection_id="t3_orders",
        dialect="mysql",
        url=SecretStr(f"mysql+asyncmy://t3_ro:{CANARY_PASSWORD}@{CANARY_HOST}:3306/{schema}"),
        allowed_schema=schema,
        allowed_tables=allowed_tables,
        config_summary=f"MySQL 只读数据源 t3_orders（schema={schema}）",
    )


def _validated(
    sql: str = f"SELECT id FROM {TABLE} ORDER BY id",
    parameters: Mapping[str, object] | None = None,
    *,
    max_rows: int = 200,
    allowed_tables: tuple[str, ...] = (TABLE,),
) -> Any:
    """经 T2 真实策略层生成 ValidatedQuery（不手搓，保证与上游契约一致）。"""
    return validate_query(sql, dict(parameters or {}), SCHEMA, allowed_tables, max_rows)


def _dbapi_error(errno: int, message: str = "dbapi error") -> Exception:
    return Exception(errno, message)


# ---------------------------------------------------------------------------
# 可观测假件
# ---------------------------------------------------------------------------


class EventLog:
    """有序事件日志：假件把调用记进来，供断言顺序与「是否被等到」。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def record(self, name: str, detail: str = "") -> None:
        self.events.append((name, detail))

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def has(self, name: str) -> bool:
        return name in self.names

    def index(self, name: str) -> int:
        return self.names.index(name)

    def details(self, name: str) -> list[str]:
        return [detail for event, detail in self.events if event == name]

    def assert_order(self, *names: str) -> None:
        """断言给定事件按序出现（允许中间夹带其他事件）。

        **按序消费**而不是用 ``index()``：``index()`` 每次取的都是**首次出现**，于是
        「exec、exec、…」这类重复事件之间的次序断言**恒真**（空转断言）。这里从前一次的
        位置继续往后找，重复事件必须依次命中不同的出现。
        """
        position = 0
        for name in names:
            try:
                position = self.names.index(name, position) + 1
            except ValueError:
                raise AssertionError(
                    f"事件顺序不符：{name} 缺失或次序错；实际 {self.names}"
                ) from None

    def assert_absent(self, *names: str) -> None:
        for name in names:
            assert name not in self.names, f"不应出现事件 {name}：{self.names}"


class FakeExecResult:
    """``exec_driver_sql`` 的假结果：既有 scalar（会话变量）也有行（元数据查询）。

    与真实 ``CursorResult`` 一致：``fetchmany`` **推进游标**，取空后返回空列表。
    """

    def __init__(self, scalar_value: object = None, rows: Sequence[Sequence[object]] = ()) -> None:
        self._scalar = scalar_value
        self._rows = [tuple(row) for row in rows]
        self._position = 0

    def scalar(self) -> object:
        return self._scalar

    def fetchmany(self, size: int | None = None) -> list[tuple[object, ...]]:
        window = size or len(self._rows)
        chunk = self._rows[self._position : self._position + window]
        self._position += len(chunk)
        return chunk


class FakeStreamResult:
    """假流式结果：按 chunk 吐行，可在某次 fetch 上抛错或永久阻塞。"""

    def __init__(
        self,
        log: EventLog,
        columns: Sequence[str],
        rows: Sequence[Sequence[object]],
        *,
        fetch_error: BaseException | None = None,
        hang_on_fetch: int | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self._log = log
        self._columns = list(columns)
        self._rows = [tuple(row) for row in rows]
        self._fetch_error = fetch_error
        self._hang_on_fetch = hang_on_fetch
        self._close_error = close_error
        self._position = 0
        self._fetches = 0

    def keys(self) -> list[str]:
        return list(self._columns)

    async def fetchmany(self, size: int | None = None) -> list[tuple[object, ...]]:
        self._fetches += 1
        self._log.record("fetchmany", str(size))
        if self._hang_on_fetch is not None and self._fetches > self._hang_on_fetch:
            await asyncio.Event().wait()  # 永不返回：用于超时/取消路径
        if self._fetch_error is not None:
            raise self._fetch_error
        window = size or len(self._rows)
        chunk = self._rows[self._position : self._position + window]
        self._position += len(chunk)
        return chunk

    async def close(self) -> None:
        self._log.record("close_result")
        if self._close_error is not None:
            raise self._close_error


class FakeConnection:
    """假连接：记录每条内部语句；可按语句标记注入失败或提供元数据行。"""

    def __init__(
        self,
        log: EventLog,
        *,
        stream_result: FakeStreamResult | None = None,
        stream_error: BaseException | None = None,
        stream_hang: bool = False,
        marker_scalars: Mapping[str, object] | None = None,
        rows_by_marker: Mapping[str, Sequence[Sequence[object]]] | None = None,
        statement_errors: Mapping[str, BaseException] | None = None,
        read_only_value: object = 1,
        timeout_value: object = 10_000,
        schema_value: object = SCHEMA,
        rollback_error: BaseException | None = None,
        close_error: BaseException | None = None,
        invalidate_error: BaseException | None = None,
    ) -> None:
        self._log = log
        self._stream_result = stream_result
        self._stream_error = stream_error
        # 可释放的挂起点：默认不放行＝「首包到达前一直不返回」（真实驱动形态）；
        # release_stream() 放行后，调用会继续走到读取阶段 —— 这是让
        # assert_absent("fetchmany"/"close_result") 具备判别力的开关（见 release_stream）
        self._stream_gate = asyncio.Event() if stream_hang else None
        self._marker_scalars = dict(marker_scalars or {})
        self._rows_by_marker = dict(rows_by_marker or {})
        self._statement_errors = dict(statement_errors or {})
        self._read_only_value = read_only_value
        self._timeout_value = timeout_value
        self._schema_value = schema_value
        self._rollback_error = rollback_error
        self._close_error = close_error
        self._invalidate_error = invalidate_error

    def _scalar_for(self, statement: str) -> object:
        for marker, value in self._marker_scalars.items():
            if marker in statement:
                return value
        if statement.startswith("SET "):
            return None
        if "transaction_read_only" in statement:
            return self._read_only_value
        if "max_execution_time" in statement:
            return self._timeout_value
        if "DATABASE()" in statement:
            return self._schema_value
        return None

    async def exec_driver_sql(
        self, statement: str, parameters: Mapping[str, object] | None = None
    ) -> FakeExecResult:
        self._log.record("exec", statement)
        for marker, error in self._statement_errors.items():
            if marker in statement:
                raise error
        rows: Sequence[Sequence[object]] = ()
        for marker, marker_rows in self._rows_by_marker.items():
            if marker in statement:
                rows = marker_rows
                break
        return FakeExecResult(self._scalar_for(statement), rows)

    def release_stream(self) -> None:
        """立即放行 ``stream()`` 的挂起点（判别力自证用）。

        它让同一套假件在**只差这一个开关**的两种形态下可比 —— 放行后调用会走到读取阶段
        （``fetchmany``/``close_result`` 被记账），不放行则要等 :data:`STREAM_GATE_SECONDS`
        才放行。⇒ 「首包前中断」用例里的 ``assert_absent("fetchmany"/"close_result")`` 因此
        有判别力：同一配置下这两个事件**是会被记上的**，只是那条路径上不该发生。
        """
        if self._stream_gate is not None:
            self._stream_gate.set()

    async def stream(self, statement: object, parameters: object = None) -> FakeStreamResult:
        self._log.record("stream", str(statement))
        if self._stream_gate is not None:
            # 真实驱动形态：await 要等服务端**首个结果包**才返回（重查询在首包前被中断时，
            # 调用方根本拿不到 result 对象）。
            # 挂起**有上限**（STREAM_GATE_SECONDS）：若实现没能按预算/取消打断它，1 秒后按
            # 「首包到达」照常返回并进入读取阶段 ⇒ 事件被记账 ⇒ assert_absent 变红。没有这个
            # 上限，那种情形会表现为「测试挂死」而不是「断言失败」，判别力就落空了。
            # ⚠️ 但「1 秒后被记账 ⇒ assert_absent 变红」这条蕴含**只对「打断前先等到读取跑完」
            # 的实现成立**：若实现**既不打断也不等**（读取成孤儿任务），调用会在预算到点时就
            # 收尾，而读取任务还卡在这一秒的挂起里 —— 事件**来得太晚**（测试早已结束），
            # 那条路径上先红的是**存活任务断言**（两条「首包前中断」用例里的 _pending_tasks
            # 比对，机制见 _pending_tasks 的 docstring），不是 assert_absent。
            # 覆盖范围以那两条用例的 docstring 为准，**不要**从 assert_absent 反推。
            # 超时必须在这里吞掉（不能让它冒泡成 TimeoutError）：否则它会撞上被测代码里
            # 「SELECT 预算用尽 → database.timeout」那条分支，把「实现没打断」伪装成「实现
            # 正确超时」——正是本会话反复出现的假绿形态。
            try:
                await asyncio.wait_for(self._stream_gate.wait(), STREAM_GATE_SECONDS)
            except asyncio.TimeoutError:
                pass
        if self._stream_error is not None:
            raise self._stream_error
        assert self._stream_result is not None, "未配置假流式结果"
        return self._stream_result

    async def rollback(self) -> None:
        self._log.record("rollback")
        if self._rollback_error is not None:
            raise self._rollback_error

    async def close(self) -> None:
        self._log.record("close_connection")
        if self._close_error is not None:
            raise self._close_error

    async def invalidate(self) -> None:
        self._log.record("invalidate")
        if self._invalidate_error is not None:
            raise self._invalidate_error


class FakeEngine:
    """假 engine：connect/dispose 记账；dispose 的完成事件在 await 之后才记。"""

    def __init__(
        self,
        log: EventLog,
        *,
        connection: FakeConnection | None = None,
        connect_error: BaseException | None = None,
        connect_hang: bool = False,
        dispose_error: BaseException | None = None,
        dispose_delay: float = 0.0,
    ) -> None:
        self._log = log
        self._connection = connection
        self._connect_error = connect_error
        self._connect_hang = connect_hang
        self._dispose_error = dispose_error
        self._dispose_delay = dispose_delay

    async def connect(self) -> FakeConnection:
        self._log.record("connect_begin")
        if self._connect_hang:
            await asyncio.Event().wait()
        if self._connect_error is not None:
            raise self._connect_error
        self._log.record("connect")
        assert self._connection is not None
        return self._connection

    async def dispose(self) -> None:
        self._log.record("dispose_begin")
        if self._dispose_delay:
            await asyncio.sleep(self._dispose_delay)
        await asyncio.sleep(0)  # 真实 await 点：没被等待就不会记到完成事件
        self._log.record("dispose")
        if self._dispose_error is not None:
            raise self._dispose_error


class Harness:
    """把假件接到 ``mysql._create_engine`` 测试缝上，并记录被建池的数据源。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.log = EventLog()
        self.engine_calls: list[ResolvedSource] = []
        self.engine: FakeEngine | None = None
        monkeypatch.setattr(mysql, "_create_engine", self._create_engine)

    def _create_engine(self, source: ResolvedSource) -> FakeEngine:
        self.log.record("create_engine")
        self.engine_calls.append(source)
        assert self.engine is not None, "测试未配置假 engine"
        return self.engine

    def install(self, **kwargs: Any) -> FakeEngine:
        self.engine = FakeEngine(self.log, **kwargs)
        return self.engine

    async def run(
        self,
        *,
        validated: Any = None,
        source: ResolvedSource | None = None,
        limits: QueryLimits | None = None,
    ) -> QueryPage:
        return await execute_readonly(
            source or _source(),
            validated if validated is not None else _validated(),
            limits,
        )


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(monkeypatch)


def number_rows(count: int, columns: Sequence[str] = ("id",)) -> list[tuple[object, ...]]:
    return [(index + 1,) for index in range(count)]


def _pending_tasks() -> set[asyncio.Task[Any]]:
    """当前事件循环里除本测试任务外**仍未收尾**的任务。

    用途：抓「孤儿读取」这一族形态——实现**既不打断读取、也不等它**（例如
    ``wait_for(asyncio.shield(_read_page(...)), 预算)``）：调用会在预算到点时拒绝并返回，
    而读取任务继续在后台跑（等到假件的 1 秒上限放行后才去记账，那时测试早已结束）。
    这种形态下假件的事件断言抓不到它（事件来得太晚），但**调用结束后仍有存活任务**是立刻
    可观测的——而这正是 §11 关心的「只创建后台任务就宣称已关闭」。
    """
    current = asyncio.current_task()
    return {task for task in asyncio.all_tasks() if task is not current and not task.done()}


# ---------------------------------------------------------------------------
# 正常路径：顺序、dispose 被等待、每次调用独立小池
# ---------------------------------------------------------------------------


async def test_normal_path_follows_documented_order_and_awaits_dispose(harness: Harness) -> None:
    harness.install(
        connection=FakeConnection(
            harness.log, stream_result=FakeStreamResult(harness.log, ("id",), number_rows(3))
        )
    )

    page = await harness.run()

    assert isinstance(page, QueryPage)
    assert page.columns == ("id",)
    assert page.rows == ((1,), (2,), (3,))
    assert page.returned_rows == 3
    assert page.truncated is False
    assert page.truncation_reason is None
    assert page.next_cursor is None  # §4：普通 query 无自动续页
    assert page.elapsed_ms >= 0
    harness.log.assert_order(
        "create_engine",
        "connect",
        "exec",  # SET SESSION TRANSACTION READ ONLY
        "exec",  # 读回 @@session.transaction_read_only
        "exec",  # START TRANSACTION READ ONLY
        "exec",  # SET SESSION max_execution_time
        "exec",  # 读回 @@session.max_execution_time
        "exec",  # SELECT DATABASE()
        "stream",
        "fetchmany",
        "close_result",
        "rollback",
        "close_connection",
        "dispose_begin",
        "dispose",
    )


async def test_internal_statements_are_fixed_literals_without_model_text(harness: Harness) -> None:
    """§5.1：内部事务/超时控制命令是固定语句，模型文本只进流式读取入口。"""
    harness.install(
        connection=FakeConnection(
            harness.log, stream_result=FakeStreamResult(harness.log, ("id",), number_rows(1))
        )
    )
    validated = _validated(f"SELECT id FROM {TABLE} WHERE id > :floor", {"floor": 10})

    await harness.run(validated=validated)

    assert harness.log.details("exec") == [
        mysql.SQL_SET_READ_ONLY,
        mysql.SQL_READ_READ_ONLY,
        mysql.SQL_START_READ_ONLY_TRANSACTION,
        mysql.SQL_SET_SELECT_TIMEOUT,
        mysql.SQL_READ_SELECT_TIMEOUT,
        mysql.SQL_READ_SCHEMA,
    ]
    # 模型 SQL（含其 LIMIT 与占位符）只出现在 stream 入口，未被拼进任何内部语句
    assert harness.log.details("stream") == [validated.sql]
    for statement in harness.log.details("exec"):
        assert TABLE not in statement
        assert ":floor" not in statement


async def test_engine_is_fresh_pool_per_call_and_disposed_each_time(harness: Harness) -> None:
    """每次调用新建小池并在返回前销毁（§6）：两次调用 → 两次建池、两次 dispose。"""
    harness.install(
        connection=FakeConnection(
            harness.log, stream_result=FakeStreamResult(harness.log, ("id",), number_rows(1))
        )
    )

    await harness.run()
    await harness.run()

    assert harness.log.names.count("create_engine") == 2
    assert harness.log.names.count("dispose") == 2
    assert len(harness.engine_calls) == 2


def test_engine_creation_uses_minimal_pool_without_echo() -> None:
    """建池参数逐字：pool_size=1 / max_overflow=0 / echo=False / hide_parameters=True。"""
    captured: dict[str, object] = {}

    def _spy(url: str, **kwargs: object) -> object:
        captured["url"] = url
        captured.update(kwargs)
        return object()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mysql, "create_async_engine", _spy)
        mysql._create_engine(_source())

    assert str(captured["url"]).endswith(f"@{CANARY_HOST}:3306/{SCHEMA}")
    assert captured["pool_size"] == 1
    assert captured["max_overflow"] == 0
    assert captured["echo"] is False
    assert captured["hide_parameters"] is True
    connect_args = captured["connect_args"]
    assert isinstance(connect_args, dict)
    assert connect_args["connect_timeout"] == mysql.CONNECT_BUDGET_SECONDS
    assert connect_args["read_timeout"] == mysql.TOTAL_BUDGET_SECONDS
    # §3.1：驱动多语句能力必须显式关闭 → 经专用 creator 建连
    assert connect_args["async_creator_fn"] is mysql._connect_without_multi_statements


# ---------------------------------------------------------------------------
# 失败路径：建连 / 执行 / 读取 / 输出编码 / 服务端超时
# ---------------------------------------------------------------------------


async def test_connect_failure_maps_to_connection_failed_and_disposes(harness: Harness) -> None:
    harness.install(connect_error=OperationalError("boom", None, _dbapi_error(2003)))

    with pytest.raises(DomainError) as caught:
        await harness.run()

    assert caught.value.code == mysql.DATABASE_CONNECTION_FAILED
    assert caught.value.message == mysql.CONNECTION_FAILED_TEXT
    assert isinstance(caught.value, mysql.QueryError)
    assert caught.value.reason == mysql.ExecutionRefusalReason.CONNECT_FAILED
    # 池已建 ⇒ 必须销毁；连接未建立 ⇒ 没有可回滚/可关闭的连接
    harness.log.assert_order("create_engine", "connect_begin", "dispose")
    harness.log.assert_absent("stream", "rollback", "close_connection", "invalidate")


async def test_execute_failure_maps_to_query_failed_and_cleans_up(harness: Harness) -> None:
    error = ProgrammingError("bad sql", None, _dbapi_error(1064))
    harness.install(
        connection=FakeConnection(harness.log, statement_errors={"SELECT DATABASE()": error})
    )

    with pytest.raises(DomainError) as caught:
        await harness.run()

    assert caught.value.code == mysql.DATABASE_QUERY_FAILED
    assert caught.value.reason == mysql.ExecutionRefusalReason.STATEMENT_FAILED
    harness.log.assert_order("rollback", "close_connection", "dispose")


async def test_stream_entry_failure_maps_to_query_failed_and_cleans_up(harness: Harness) -> None:
    error = OperationalError("stream failed", None, _dbapi_error(2013))
    harness.install(connection=FakeConnection(harness.log, stream_error=error))

    with pytest.raises(DomainError) as caught:
        await harness.run()

    assert caught.value.code == mysql.DATABASE_QUERY_FAILED
    harness.log.assert_order("rollback", "close_connection", "dispose")


async def test_fetch_failure_closes_result_then_rolls_back(harness: Harness) -> None:
    error = OperationalError("lost connection", None, _dbapi_error(2013))
    result = FakeStreamResult(harness.log, ("id",), number_rows(500), fetch_error=error)
    harness.install(connection=FakeConnection(harness.log, stream_result=result))

    with pytest.raises(DomainError) as caught:
        await harness.run()

    assert caught.value.code == mysql.DATABASE_QUERY_FAILED
    harness.log.assert_order("close_result", "rollback", "close_connection", "dispose")


async def test_output_encoding_failure_maps_to_query_failed_and_cleans_up(
    harness: Harness,
) -> None:
    """输出编码失败：整体失败并完整清理，不吐出半页。"""

    class _Unencodable:
        def __str__(self) -> str:
            raise ValueError("cannot render")

    result = FakeStreamResult(harness.log, ("id",), [(1,), (_Unencodable(),)])
    harness.install(connection=FakeConnection(harness.log, stream_result=result))

    with pytest.raises(DomainError) as caught:
        await harness.run()

    assert caught.value.code == mysql.DATABASE_QUERY_FAILED
    assert caught.value.reason == mysql.ExecutionRefusalReason.SERIALIZATION_FAILED
    harness.log.assert_order("close_result", "rollback", "close_connection", "dispose")


async def test_server_side_timeout_error_maps_to_database_timeout(harness: Harness) -> None:
    """服务端 max_execution_time 中断（3024）对外是 database.timeout，内部原因码区分。"""
    error = OperationalError("interrupted", None, _dbapi_error(3024))
    result = FakeStreamResult(harness.log, ("id",), number_rows(500), fetch_error=error)
    harness.install(connection=FakeConnection(harness.log, stream_result=result))

    with pytest.raises(DomainError) as caught:
        await harness.run()

    assert caught.value.code == mysql.DATABASE_TIMEOUT
    assert caught.value.message == mysql.TIMEOUT_TEXT
    assert caught.value.reason == mysql.ExecutionRefusalReason.SERVER_TIMEOUT


# ---------------------------------------------------------------------------
# 超时与取消：受保护、有上限、被真正等待的清理
# ---------------------------------------------------------------------------


async def test_select_budget_timeout_cleans_up_and_reports_timeout(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """客户端 SELECT 预算用尽：抛 database.timeout，且清理在异常外泄前完成（含 dispose）。

    本用例覆盖**首包已到、读取中途**被中断的形态（假件挂在 ``fetchmany``）；真实重查询更常
    停在首包之前，那條路径见 :func:`test_timeout_before_first_packet_has_no_result_to_close`。
    """
    monkeypatch.setattr(mysql, "SELECT_BUDGET_SECONDS", 0.05)
    result = FakeStreamResult(harness.log, ("id",), number_rows(500), hang_on_fetch=0)
    harness.install(connection=FakeConnection(harness.log, stream_result=result))

    with pytest.raises(DomainError) as caught:
        await harness.run()

    assert caught.value.code == mysql.DATABASE_TIMEOUT
    assert caught.value.reason == mysql.ExecutionRefusalReason.CLIENT_DEADLINE
    # 超时路径不归还池：连接判定不可复用（invalidate），随后 dispose 物理断开
    harness.log.assert_order("close_result", "rollback", "invalidate", "dispose_begin", "dispose")
    harness.log.assert_absent("close_connection")


async def test_connect_budget_timeout_maps_to_connection_failed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mysql, "CONNECT_BUDGET_SECONDS", 0.05)
    harness.install(connect_hang=True)

    with pytest.raises(DomainError) as caught:
        await harness.run()

    assert caught.value.code == mysql.DATABASE_CONNECTION_FAILED
    assert caught.value.reason == mysql.ExecutionRefusalReason.CLIENT_DEADLINE
    assert harness.log.has("dispose")


async def test_total_budget_timeout_is_bounded_and_cleaned(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """整个调用上限：即便单条语句迟迟不返回，也必须在总预算内收口并清理。"""
    monkeypatch.setattr(mysql, "TOTAL_BUDGET_SECONDS", 0.05)
    original = FakeConnection.exec_driver_sql

    async def _slow_exec(
        connection: FakeConnection, statement: str, parameters: object = None
    ) -> object:
        harness.log.record("exec", statement)
        await asyncio.Event().wait()
        return await original(connection, statement, parameters)  # pragma: no cover

    harness.install(connection=FakeConnection(harness.log, stream_result=None))
    monkeypatch.setattr(FakeConnection, "exec_driver_sql", _slow_exec)

    with pytest.raises(DomainError) as caught:
        await harness.run()

    assert caught.value.code == mysql.DATABASE_TIMEOUT
    assert harness.log.has("dispose")


async def test_timeout_before_first_packet_has_no_result_to_close(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**真实驱动的超时形态**：中断发生在 ``stream()`` 内（首包到达前）。

    审查探针实测：重查询停在 ``await connection.stream(...)`` 里直到客户端预算触发，
    ⇒ ``_read_page`` 中登记 ``resources.result`` 的那一行从未执行，「关闭结果」在这条
    路径上**不可达**（``close_result`` 本意就是 drain，不是被刻意跳过）。
    故本用例断言：清理按 rollback → invalidate → dispose 走完，且**没有** ``close_result``；
    物理关闭由 ``invalidate()`` + ``dispose()`` 保证（集成侧另有观察连接的会话退出证据）。

    ``assert_absent`` 的判别力：假件**同时装了 stream_result**（事件可被记账），只是本例不放行
    挂起点；同一套假件放行后的事件形态由
    :func:`test_absent_assertions_have_discriminating_power_when_read_is_reached` 自证。

    红的**必要条件**（勿读宽）：实现必须**等到**读取跑完、或**读完后才**拒绝——只有那样事件
    才会在断言之前被记账。若实现**既不打断也不等**（读取成孤儿任务），事件来不及记账，本用例
    靠 :func:`_pending_tasks` 那条断言抓——实测：把读取 shield 成孤儿任务（预算到点后它继续
    在后台跑）会让本用例与取消用例**双双变红**，报错直指存活的 ``_read_page`` 任务。
    """
    monkeypatch.setattr(mysql, "SELECT_BUDGET_SECONDS", 0.05)
    harness.install(
        connection=FakeConnection(
            harness.log,
            stream_result=FakeStreamResult(harness.log, ("id",), number_rows(3)),
            stream_hang=True,
        )
    )
    tasks_before = _pending_tasks()

    with pytest.raises(DomainError) as caught:
        await harness.run()

    assert caught.value.code == mysql.DATABASE_TIMEOUT
    assert caught.value.reason == mysql.ExecutionRefusalReason.CLIENT_DEADLINE
    harness.log.assert_order("stream", "rollback", "invalidate", "dispose_begin", "dispose")
    harness.log.assert_absent("close_result")  # result 从未登记 ⇒ 关闭结果不可达
    harness.log.assert_absent("fetchmany")  # 首包都没到，自然也没读到行
    # 孤儿读取（既不打断也不等）立刻可观测：调用结束后不该有存活任务
    assert _pending_tasks() - tasks_before == set(), "调用结束后仍有未收尾的任务（孤儿读取？）"


async def test_cancellation_before_first_packet_cleans_up_and_reraises(harness: Harness) -> None:
    """**真实驱动的取消形态**：同样停在 ``stream()`` 内，清理完成后才重新抛出取消异常。"""
    harness.install(
        connection=FakeConnection(
            harness.log,
            stream_result=FakeStreamResult(harness.log, ("id",), number_rows(3)),
            stream_hang=True,
        )
    )
    tasks_before = _pending_tasks()

    task = asyncio.create_task(harness.run())
    while "stream" not in harness.log.names:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    harness.log.assert_order("stream", "rollback", "invalidate", "dispose_begin", "dispose")
    harness.log.assert_absent("close_result")
    harness.log.assert_absent("fetchmany")
    assert _pending_tasks() - tasks_before == set(), "取消后仍有未收尾的任务（孤儿读取？）"


async def test_absent_assertions_have_discriminating_power_when_read_is_reached(
    harness: Harness,
) -> None:
    """判别力自证：同一套假件**只把挂起点放行**，读取阶段的事件就真的会被记上。

    上面两条「首包前中断」用例断言 ``fetchmany``/``close_result`` **未发生**；若那些事件在
    该配置下根本不可能被记录，断言就是空转（本会话已吃过四次「断言在、判别力不在」的亏）。
    本用例用**完全相同的假件与开关**，仅调用 ``release_stream()`` 放行 ``stream()``：

    - 放行 ⇒ 调用继续走到读取阶段 ⇒ ``fetchmany``、``close_result`` **确实出现**（此处断言其在）；
    - 不放行 ⇒ 那两条用例断言其不在。

    ⇒ 两个方向都由可执行断言钉住，「未发生」是可伪的，不是「没断言到」。
    """
    connection = FakeConnection(
        harness.log,
        stream_result=FakeStreamResult(harness.log, ("id",), number_rows(3)),
        stream_hang=True,
    )
    harness.install(connection=connection)
    connection.release_stream()  # 唯一差异：放行挂起点

    page = await harness.run()

    assert page.returned_rows == 3
    harness.log.assert_order(
        "stream", "fetchmany", "close_result", "rollback", "close_connection", "dispose"
    )


async def test_cancellation_runs_cleanup_before_reraising(harness: Harness) -> None:
    """取消路径（**首包已到**、读取中途被取消）：清理被真正等待，完成后才重新抛出取消异常。

    首包之前的取消形态见 :func:`test_cancellation_before_first_packet_cleans_up_and_reraises`。
    """
    result = FakeStreamResult(harness.log, ("id",), number_rows(500), hang_on_fetch=0)
    harness.install(connection=FakeConnection(harness.log, stream_result=result))

    task = asyncio.create_task(harness.run())
    while "fetchmany" not in harness.log.names:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 取消异常外泄时清理已跑完：dispose 完成事件在 await 之后才记账
    harness.log.assert_order("close_result", "rollback", "invalidate", "dispose_begin", "dispose")


async def test_cancellation_during_connect_cleans_up(harness: Harness) -> None:
    harness.install(connect_hang=True)

    task = asyncio.create_task(harness.run())
    while "connect_begin" not in harness.log.names:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert harness.log.has("dispose")
    harness.log.assert_absent("rollback", "close_connection", "invalidate")


async def test_cleanup_budget_exceeded_does_not_block_forever_but_is_logged(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """清理自身超预算：不再无限等待（上限 3 秒），但**如实记录**未在预算内完成。"""
    monkeypatch.setattr(mysql, "CLEANUP_BUDGET_SECONDS", 0.05)
    error = OperationalError("lost", None, _dbapi_error(2013))
    result = FakeStreamResult(harness.log, ("id",), number_rows(1), fetch_error=error)
    harness.install(connection=FakeConnection(harness.log, stream_result=result), dispose_delay=0.2)

    # ⚠️ 必须先把这个 logger 显式打开，否则本用例**结构上不可能失败**（空转断言）：
    # 会话级迁移夹具跑 alembic 的 fileConfig(...)，其默认 disable_existing_loggers=True 会把
    # **导入期已存在**的全部 logger（含本模块，收集阶段就被导入）禁用；被禁 logger 的
    # isEnabledFor 恒为 False ⇒ 一条记录也不产生。而 caplog.at_level 只**设级别**、不会重新
    # 启用被禁 logger —— 只写 at_level 的话，caplog.records 永远是空列表，
    # `assert any(...)` 直接失败（带 conftest 跑时实测如此），或换成「断言某内容不在日志里」
    # 就会静默通过（更隐蔽的假绿）。用完还原 disabled 状态，不影响其他用例。
    module_logger = logging.getLogger(mysql.__name__)
    was_disabled = module_logger.disabled
    module_logger.disabled = False
    try:
        with caplog.at_level(logging.WARNING, logger=mysql.__name__):
            with pytest.raises(DomainError) as caught:
                await harness.run()
    finally:
        module_logger.disabled = was_disabled

    assert caught.value.code == mysql.DATABASE_QUERY_FAILED
    # 判别性：让实现不再记这条日志（把 _release 超预算分支里的 _refuse(...) 换成 pass），
    # 本断言即红（实测：带 conftest 下 records 为空 → AssertionError: []）
    assert any(
        mysql.ExecutionRefusalReason.CLEANUP_INCOMPLETE in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]
    await asyncio.sleep(0.3)  # 让被放弃等待的清理任务收尾，避免悬挂任务告警


# ---------------------------------------------------------------------------
# 清理异常：不跳过后续清理，也不掩盖原始错误
# ---------------------------------------------------------------------------


async def test_cleanup_failures_do_not_skip_remaining_steps(harness: Harness) -> None:
    result = FakeStreamResult(
        harness.log,
        ("id",),
        number_rows(1),
        close_error=OperationalError("x", None, _dbapi_error(2013)),
    )
    harness.install(
        connection=FakeConnection(
            harness.log,
            stream_result=result,
            rollback_error=OperationalError("y", None, _dbapi_error(2013)),
            close_error=OperationalError("z", None, _dbapi_error(2013)),
        ),
        dispose_error=OperationalError("w", None, _dbapi_error(2013)),
    )

    page = await harness.run()

    harness.log.assert_order("close_result", "rollback", "close_connection", "dispose")
    assert page.returned_rows == 1  # 清理异常不改变业务结果


async def test_cleanup_failure_does_not_mask_original_error(harness: Harness) -> None:
    error = OperationalError("interrupted", None, _dbapi_error(3024))
    result = FakeStreamResult(
        harness.log,
        ("id",),
        number_rows(500),
        fetch_error=error,
        close_error=ValueError("cleanup"),
    )
    harness.install(
        connection=FakeConnection(
            harness.log, stream_result=result, rollback_error=ValueError("cleanup2")
        ),
        dispose_error=ValueError("cleanup3"),
    )

    with pytest.raises(DomainError) as caught:
        await harness.run()

    assert caught.value.code == mysql.DATABASE_TIMEOUT  # 原始错误不被清理异常取代
    # 服务端中断（3024）属异常路径：连接销毁而非归还池；清理异常不跳过后续步骤
    harness.log.assert_order("close_result", "rollback", "invalidate", "dispose")


async def test_errors_never_leak_secret_or_parameters(harness: Harness) -> None:
    """§8：底层异常、URL、口令、完整绑定参数与堆栈都不回传模型。"""
    error = OperationalError(f"connect to {CANARY_HOST} failed", None, Exception(CANARY_PASSWORD))
    harness.install(connect_error=error)

    with pytest.raises(DomainError) as caught:
        await harness.run()

    rendered = f"{caught.value!r} {caught.value.args} {caught.value.context}"
    for leaked in (CANARY_PASSWORD, CANARY_HOST, "t3_ro"):
        assert leaked not in rendered


# ---------------------------------------------------------------------------
# 结果形状：序列化规则与资源边界（规格 §4/§7.1）
# ---------------------------------------------------------------------------


async def _page_of(
    harness: Harness, rows: Sequence[Sequence[object]], **run_kwargs: Any
) -> QueryPage:
    columns = ("c0",) if not rows else tuple(f"c{index}" for index in range(len(rows[0])))
    harness.install(
        connection=FakeConnection(
            harness.log, stream_result=FakeStreamResult(harness.log, columns, rows)
        )
    )
    return await harness.run(**run_kwargs)


async def test_decimal_is_serialized_as_string(harness: Harness) -> None:
    page = await _page_of(harness, [(Decimal("1234567890.123456"),)])
    assert page.rows == (("1234567890.123456",),)


async def test_datetime_is_isoformat_without_inventing_timezone(harness: Harness) -> None:
    page = await _page_of(
        harness,
        [(datetime(2024, 3, 4, 5, 6, 7, 890000), date(2024, 3, 4), time(5, 6, 7))],
    )
    assert page.rows == (("2024-03-04T05:06:07.890000", "2024-03-04", "05:06:07"),)


async def test_big_integer_keeps_precision(harness: Harness) -> None:
    """大整数保留精度：超出 JSON 安全整数范围的取值以字符串回传。"""
    page = await _page_of(harness, [(9007199254740993, 9007199254740991, 42, True, False)])
    assert page.rows == (("9007199254740993", 9007199254740991, 42, True, False),)


async def test_binary_field_is_reported_not_returned(harness: Harness) -> None:
    page = await _page_of(harness, [(b"\x00\x01\x02\x03",)])
    assert page.rows == (("<binary 4 bytes>",),)
    assert page.truncated is True
    assert page.truncation_reason == TRUNCATION_BINARY_FIELD


async def test_oversized_field_is_truncated_and_marked(harness: Harness) -> None:
    huge = "汉" * (MAX_FIELD_BYTES // 3 + 10)  # 多字节：按字节截断不得切碎字符
    page = await _page_of(harness, [(huge,), ("short",)])

    truncated_value = page.rows[0][0]
    assert isinstance(truncated_value, str)
    assert len(truncated_value.encode("utf-8")) <= MAX_FIELD_BYTES
    assert truncated_value.startswith("汉")
    assert "�" not in truncated_value
    assert page.rows[1] == ("short",)
    assert page.truncated is True
    assert page.truncation_reason == TRUNCATION_FIELD_BYTES


async def test_output_byte_budget_stops_reading_and_marks_reason(harness: Harness) -> None:
    limits = QueryLimits(max_rows=1000, max_bytes=4000)
    payload = "x" * 1000
    page = await _page_of(harness, [(payload,)] * 100, limits=limits)

    assert page.returned_rows < 100
    assert page.truncated is True
    assert page.truncation_reason == TRUNCATION_OUTPUT_BYTES
    total = sum(len(str(row[0]).encode("utf-8")) for row in page.rows)
    assert total <= limits.max_bytes


async def test_rows_use_positional_arrays_not_dicts(harness: Harness) -> None:
    """§4：行是「按列顺序的数组」，重名列不得互相覆盖。"""
    harness.install(
        connection=FakeConnection(
            harness.log,
            stream_result=FakeStreamResult(harness.log, ("amount", "amount"), [(1, 2), (3, 4)]),
        )
    )

    page = await harness.run()

    assert page.columns == ("amount", "amount")
    assert page.rows == ((1, 2), (3, 4))


async def test_empty_result_keeps_columns_and_is_not_truncated(harness: Harness) -> None:
    page = await _page_of(harness, [])

    assert page.columns == ("c0",)
    assert page.rows == ()
    assert page.returned_rows == 0
    assert page.truncated is False
    assert page.next_cursor is None


# ---------------------------------------------------------------------------
# 截断探针约定（§7.1 + 控制方裁定）：最多读 max_rows + 1，返回 max_rows
# ---------------------------------------------------------------------------


async def test_extra_row_probe_marks_truncation(harness: Harness) -> None:
    page = await _page_of(harness, number_rows(4), limits=QueryLimits(max_rows=3))

    assert page.returned_rows == 3
    assert page.rows == ((1,), (2,), (3,))
    assert page.truncated is True
    assert page.truncation_reason == TRUNCATION_ROW_LIMIT
    assert page.next_cursor is None  # 截断不自动续页：续页游标属 T4


async def test_exact_row_count_is_not_reported_as_truncated(harness: Harness) -> None:
    page = await _page_of(harness, number_rows(3), limits=QueryLimits(max_rows=3))

    assert page.returned_rows == 3
    assert page.truncated is False
    assert page.truncation_reason is None


async def test_reading_is_chunked_and_stops_at_probe_row(harness: Harness) -> None:
    """禁止 fetchall 后切片：按 chunk_rows 分块读，且不超过 max_rows + 1。"""
    page = await _page_of(harness, number_rows(1000), limits=QueryLimits(max_rows=250))

    sizes = harness.log.details("fetchmany")
    assert set(sizes) == {str(CHUNK_ROWS)}
    assert len(sizes) == 3  # 251 行 = 100 + 100 + 51，未把 1000 行读进来
    assert page.returned_rows == 250
    assert page.truncated is True
    assert page.truncation_reason == TRUNCATION_ROW_LIMIT


async def test_more_than_hard_cap_rows_is_refused_before_connecting(harness: Harness) -> None:
    harness.install(connection=FakeConnection(harness.log, stream_result=None))

    with pytest.raises(DomainError) as caught:
        await harness.run(limits=QueryLimits(max_rows=1001))

    assert caught.value.code == DATABASE_LIMIT_EXCEEDED
    assert caught.value.reason == mysql.ExecutionRefusalReason.LIMITS_INVALID
    harness.log.assert_absent("create_engine", "connect")


@pytest.mark.parametrize(
    "limits",
    [
        QueryLimits(max_rows=0),
        QueryLimits(max_rows=-1),
        QueryLimits(chunk_rows=0),
        QueryLimits(max_bytes=MAX_OUTPUT_BYTES + 1),
        QueryLimits(max_field_bytes=MAX_FIELD_BYTES + 1),
    ],
)
async def test_invalid_limits_are_refused(harness: Harness, limits: QueryLimits) -> None:
    harness.install(connection=FakeConnection(harness.log, stream_result=None))

    with pytest.raises(DomainError) as caught:
        await harness.run(limits=limits)

    assert caught.value.code == DATABASE_LIMIT_EXCEEDED
    harness.log.assert_absent("create_engine")


# ---------------------------------------------------------------------------
# 防线自检：设置失败即退出、schema 错位即退出（不降级、不继续读）
# ---------------------------------------------------------------------------


async def test_schema_mismatch_is_refused_before_reading(harness: Harness) -> None:
    harness.install(
        connection=FakeConnection(
            harness.log,
            stream_result=FakeStreamResult(harness.log, ("id",), number_rows(1)),
            schema_value="other_database",
        )
    )

    with pytest.raises(DomainError) as caught:
        await harness.run()

    assert caught.value.code == mysql.DATABASE_QUERY_FAILED
    assert caught.value.reason == mysql.ExecutionRefusalReason.SCHEMA_MISMATCH
    harness.log.assert_absent("stream")


async def test_read_only_guard_failure_refuses_to_continue(harness: Harness) -> None:
    """§5.1：只读事务设置失败直接退出，不降级普通事务。"""
    harness.install(
        connection=FakeConnection(
            harness.log,
            stream_result=FakeStreamResult(harness.log, ("id",), number_rows(1)),
            read_only_value=0,
        )
    )

    with pytest.raises(DomainError) as caught:
        await harness.run()

    assert caught.value.code == mysql.DATABASE_QUERY_FAILED
    assert caught.value.reason == mysql.ExecutionRefusalReason.SESSION_GUARD_FAILED
    harness.log.assert_absent("stream")


async def test_server_timeout_guard_failure_refuses_to_continue(harness: Harness) -> None:
    harness.install(
        connection=FakeConnection(
            harness.log,
            stream_result=FakeStreamResult(harness.log, ("id",), number_rows(1)),
            timeout_value=0,
        )
    )

    with pytest.raises(DomainError) as caught:
        await harness.run()

    assert caught.value.reason == mysql.ExecutionRefusalReason.SESSION_GUARD_FAILED
    harness.log.assert_absent("stream")


# ---------------------------------------------------------------------------
# describe_table：固定内部只读元数据查询、同生命周期、不查任意系统表
# ---------------------------------------------------------------------------


def _metadata_connection(
    log: EventLog,
    *,
    table_type: str | None = "BASE TABLE",
    column_rows: Sequence[Sequence[object]] | None = None,
    index_rows: Sequence[Sequence[object]] | None = None,
    statement_errors: Mapping[str, BaseException] | None = None,
) -> FakeConnection:
    columns = (
        column_rows
        if column_rows is not None
        else [
            ("id", "int", "NO", "PRI", None, "auto_increment"),
            ("amount", "decimal(10,2)", "YES", "", None, ""),
        ]
    )
    indexes = (
        index_rows
        if index_rows is not None
        else [
            ("PRIMARY", 0, "id"),
            ("idx_amount", 1, "amount"),
        ]
    )
    return FakeConnection(
        log,
        marker_scalars={TABLES_MARKER: table_type},
        rows_by_marker={COLUMNS_MARKER: columns, STATISTICS_MARKER: indexes},
        statement_errors=dict(statement_errors or {}),
    )


async def test_describe_table_reports_columns_primary_key_and_indexes(harness: Harness) -> None:
    harness.install(connection=_metadata_connection(harness.log))

    description = await describe_table(_source(), TABLE)

    assert isinstance(description, TableDescription)
    assert description.table == TABLE
    assert description.columns == (
        ColumnDescription(
            name="id",
            data_type="int",
            nullable=False,
            key="PRI",
            default=None,
            extra="auto_increment",
        ),
        ColumnDescription(
            name="amount",
            data_type="decimal(10,2)",
            nullable=True,
            key="",
            default=None,
            extra="",
        ),
    )
    assert description.primary_key == ("id",)
    assert description.indexes == (
        IndexDescription(name="PRIMARY", columns=("id",), unique=True),
        IndexDescription(name="idx_amount", columns=("amount",), unique=False),
    )
    assert description.pagination_suitable is True
    assert description.pagination_reason is None
    # 同一生命周期设施：连接 → 只读/超时设置 → 元数据读取 → 回滚 → 关连接 → dispose
    harness.log.assert_order(
        "create_engine", "connect", "exec", "rollback", "close_connection", "dispose"
    )
    # 表名不拼进 SQL（固定语句 + 绑定参数）
    for statement in harness.log.details("exec"):
        assert TABLE not in statement


async def test_describe_table_reports_why_pagination_is_unsuitable(harness: Harness) -> None:
    harness.install(
        connection=_metadata_connection(
            harness.log,
            column_rows=[("code", "varchar(32)", "NO", "PRI", None, "")],
            index_rows=[("PRIMARY", 0, "code")],
        )
    )

    description = await describe_table(_source(), TABLE)

    assert description.pagination_suitable is False
    assert description.pagination_reason  # 明确给出原因，不静默


async def test_describe_table_refuses_table_outside_whitelist(harness: Harness) -> None:
    harness.install(connection=_metadata_connection(harness.log))

    with pytest.raises(DomainError) as caught:
        await describe_table(_source(), "information_schema.tables")

    assert caught.value.code == mysql.DATABASE_TABLE_NOT_ALLOWED
    assert caught.value.reason == mysql.ExecutionRefusalReason.TABLE_NOT_ALLOWED
    harness.log.assert_absent("create_engine")


async def test_describe_table_reports_views_unsupported(harness: Harness) -> None:
    """§5.2：首版不查询视图，describe 接口必须明确报告不支持。"""
    harness.install(connection=_metadata_connection(harness.log, table_type="VIEW"))

    with pytest.raises(DomainError) as caught:
        await describe_table(_source(), TABLE)

    assert caught.value.code == mysql.DATABASE_TABLE_NOT_ALLOWED
    assert caught.value.reason == mysql.ExecutionRefusalReason.VIEW_NOT_SUPPORTED
    # 只查了表类型，未去读视图的列定义（首版不查询视图）
    assert not any(COLUMNS_MARKER in statement for statement in harness.log.details("exec"))


async def test_describe_table_refuses_missing_table_with_same_code(harness: Harness) -> None:
    harness.install(connection=_metadata_connection(harness.log, table_type=None))

    with pytest.raises(DomainError) as caught:
        await describe_table(_source(), TABLE)

    assert caught.value.code == mysql.DATABASE_TABLE_NOT_ALLOWED
    assert caught.value.reason == mysql.ExecutionRefusalReason.TABLE_NOT_ALLOWED


async def test_describe_table_cleans_up_on_failure(harness: Harness) -> None:
    harness.install(
        connection=_metadata_connection(
            harness.log, statement_errors={COLUMNS_MARKER: _metadata_error()}
        )
    )

    with pytest.raises(DomainError) as caught:
        await describe_table(_source(), TABLE)

    assert caught.value.code == mysql.DATABASE_QUERY_FAILED
    harness.log.assert_order("rollback", "close_connection", "dispose")


def _metadata_error() -> ProgrammingError:
    return ProgrammingError("metadata failed", None, _dbapi_error(1064))


# ---------------------------------------------------------------------------
# 跨任务契约：字段名/类型与稳定错误码
# ---------------------------------------------------------------------------


def test_event_log_order_assertion_is_not_vacuous() -> None:
    """自检：次序断言真按序消费。

    旧实现每项都取 ``index()``（首次出现），「exec、exec、…」这类重复事件之间的次序断言
    **恒真** —— 那正是本会话反复出现的「空转断言」族。这里用一个两事件的日志把新语义钉死：
    重复项必须依次命中不同出现，次序颠倒必须报错。
    """
    log = EventLog()
    log.record("a")
    log.record("b")

    log.assert_order("a", "b")
    with pytest.raises(AssertionError):
        log.assert_order("b", "a")  # 次序颠倒
    with pytest.raises(AssertionError):
        log.assert_order("a", "a")  # 只有一个 a，第二次出现不存在
    with pytest.raises(AssertionError):
        log.assert_order("a", "missing")


def test_query_page_field_names_and_types_are_frozen_for_t4() -> None:
    """§4 统一结果形状：字段名与类型由 T3 一次定准，T4 依赖它。"""
    assert [field.name for field in dataclasses.fields(QueryPage)] == [
        "columns",
        "rows",
        "returned_rows",
        "truncated",
        "truncation_reason",
        "next_cursor",
        "elapsed_ms",
    ]
    annotations = QueryPage.__annotations__
    assert annotations["columns"] == tuple[str, ...]
    assert annotations["rows"] == tuple[tuple[object, ...], ...]
    assert annotations["returned_rows"] is int
    assert annotations["truncated"] is bool
    assert annotations["truncation_reason"] == (str | None)
    assert annotations["next_cursor"] == (str | None)
    assert annotations["elapsed_ms"] is int


def test_stable_error_codes_are_reused() -> None:
    """表拒绝复用 T2 既有稳定码，新增码只限本任务三类（§8）。"""
    assert mysql.DATABASE_TABLE_NOT_ALLOWED == "database.table_not_allowed"
    assert mysql.DATABASE_TIMEOUT == "database.timeout"
    assert mysql.DATABASE_CONNECTION_FAILED == "database.connection_failed"
    assert mysql.DATABASE_QUERY_FAILED == "database.query_failed"


def test_streaming_is_wired_to_the_unbuffered_driver_cursor() -> None:
    """§7.1：分块读取必须落在**不预缓冲**的服务端游标上，否则不能记作流式通过。

    SQLAlchemy 的 asyncmy 方言在 ``stream_results`` 打开时用
    ``AsyncAdapt_asyncmy_ss_cursor``，其 ``_make_new_cursor`` 固定构造
    ``asyncmy.cursors.SSCursor`` —— asyncmy 的 Unbuffered Cursor（``conn.query(q,
    unbuffered=True)``，逐行从 socket 读，不把整个结果集先拷进内存）。本用例对着**当前
    安装版本**钉住这条接线：日后升级 SQLAlchemy/asyncmy 若换成缓冲游标，这里会红。
    """
    from types import SimpleNamespace

    import asyncmy
    from asyncmy.cursors import Cursor as _BufferedCursor
    from asyncmy.cursors import SSCursor
    from sqlalchemy.dialects.mysql import asyncmy as sqlalchemy_asyncmy

    created: list[object] = []
    stub_self = SimpleNamespace(
        _adapt_connection=SimpleNamespace(dbapi=SimpleNamespace(asyncmy=asyncmy))
    )
    driver_connection = SimpleNamespace(cursor=created.append)

    sqlalchemy_asyncmy.AsyncAdapt_asyncmy_ss_cursor._make_new_cursor(  # noqa: SLF001
        stub_self, driver_connection
    )

    assert created == [SSCursor]
    assert SSCursor is not _BufferedCursor
    assert issubclass(SSCursor, _BufferedCursor)
    assert sqlalchemy_asyncmy.AsyncAdapt_asyncmy_ss_cursor.server_side is True


async def test_check_connection_returns_schema_and_cleans_up(harness: Harness) -> None:
    """doctor --connect 的成功路径：读回 schema 后按同一生命周期销毁资源。"""
    harness.install(connection=FakeConnection(harness.log))

    schema = await check_connection(_source())

    assert schema == SCHEMA
    harness.log.assert_order(
        "create_engine",
        "connect",
        "exec",
        "exec",
        "exec",
        "exec",
        "exec",
        "exec",
        "rollback",
        "close_connection",
        "dispose",
    )


async def test_check_connection_schema_mismatch_fails_closed(harness: Harness) -> None:
    """连接实际库与白名单不一致时必须拒绝，且仍释放连接与池。"""
    harness.install(connection=FakeConnection(harness.log, schema_value="other_schema"))

    with pytest.raises(DomainError) as exc:
        await check_connection(_source())

    assert exc.value.code == "database.query_failed"
    assert exc.value.reason == mysql.ExecutionRefusalReason.SCHEMA_MISMATCH
    harness.log.assert_order(
        "create_engine",
        "connect",
        "exec",
        "exec",
        "exec",
        "exec",
        "exec",
        "exec",
        "rollback",
        "close_connection",
        "dispose",
    )
