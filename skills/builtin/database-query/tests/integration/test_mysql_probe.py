"""真实 MySQL 8 探针：只读事务、schema、序列化、占位符与驱动多语句。"""

from __future__ import annotations

import asyncio
import functools
import time
from dataclasses import dataclass

import pytest
from pydantic import SecretStr

from dbquery import mysql
from dbquery.errors import DomainError
from dbquery.mysql import QueryLimits, describe_table, execute_readonly
from dbquery.policy import validate_query
from dbquery.types import ResolvedSource

TABLE = "probe_orders"


@dataclass(frozen=True)
class Probe:
    """与 conftest 的探针夹具同形（仅用于类型标注）。"""

    url: str
    schema: str


def _retryable(exc: BaseException) -> bool:
    return isinstance(exc, (TimeoutError, OSError)) or getattr(exc, "code", None) in {
        "database.connection_failed",
        "database.timeout",
    }


def retry_connect(attempts: int = 3):
    """本机 Docker 端口代理偶发 TCP 挂起时，仅对连接类失败重试。"""

    def decorate(function):
        @functools.wraps(function)
        async def wrapper(*args, **kwargs):
            for attempt in range(attempts):
                try:
                    return await function(*args, **kwargs)
                except BaseException as exc:
                    if attempt == attempts - 1 or not _retryable(exc):
                        raise
                    await asyncio.sleep(0.5)
            raise AssertionError("unreachable")

        return wrapper

    return decorate


def _source(probe: Probe, *, allowed_schema: str | None = None) -> ResolvedSource:
    return ResolvedSource(
        connection_id="probe",
        dialect="mysql",
        url=SecretStr(probe.url),
        allowed_schema=allowed_schema or probe.schema,
        allowed_tables=(TABLE,),
        config_summary="探针数据源",
    )


@retry_connect()
async def test_aggregate_query_returns_sum(probe: Probe) -> None:
    source = _source(probe)
    validated = validate_query(
        f"SELECT SUM(amount) AS total, COUNT(*) AS cnt FROM {TABLE}",
        {},
        source.allowed_schema,
        source.allowed_tables,
        201,
    )

    page = await execute_readonly(source, validated, QueryLimits())

    assert page.columns == ("total", "cnt")
    assert page.rows[0][0] == "61.50"
    assert page.rows[0][1] == 3
    assert page.truncated is False


@retry_connect()
async def test_describe_table_reports_columns_and_primary_key(probe: Probe) -> None:
    description = await describe_table(_source(probe), TABLE)

    assert description.primary_key == ("id",)
    assert [column.name for column in description.columns] == ["id", "amount", "note", "created_at"]
    assert description.pagination_suitable is True


@retry_connect()
async def test_decimal_bigint_and_duplicate_columns_roundtrip(probe: Probe) -> None:
    source = _source(probe)
    validated = validate_query(
        f"SELECT amount, id, amount FROM {TABLE} ORDER BY id",
        {},
        source.allowed_schema,
        source.allowed_tables,
        201,
    )

    page = await execute_readonly(source, validated, QueryLimits())

    assert page.columns == ("amount", "id", "amount")
    assert page.rows[0] == ("10.50", 1, "10.50")
    assert page.rows[2][1] == "9007199254740993"


@retry_connect()
async def test_literal_colon_and_percent_roundtrip(probe: Probe) -> None:
    source = _source(probe)
    validated = validate_query(
        f"SELECT ':floor' AS lit, '50%' AS pct, :v AS bound FROM {TABLE} LIMIT 1",
        {"v": 7},
        source.allowed_schema,
        source.allowed_tables,
        201,
    )

    page = await execute_readonly(source, validated, QueryLimits(max_rows=1))

    assert page.rows[0] == (":floor", "50%", 7)


@retry_connect()
async def test_readonly_transaction_blocks_write(probe: Probe) -> None:
    source = _source(probe)
    resources = mysql._CallResources()
    connection = await mysql._connect(resources, source, QueryLimits())
    try:
        await mysql._apply_session_guards(connection)
        with pytest.raises(Exception):
            await connection.exec_driver_sql(
                f"INSERT INTO {TABLE} (id, amount, note, created_at) VALUES (99, 1.00, 'x', NOW())"
            )
    finally:
        await mysql._release(resources)

    validated = validate_query(
        f"SELECT COUNT(*) AS cnt FROM {TABLE}",
        {},
        source.allowed_schema,
        source.allowed_tables,
        201,
    )
    page = await execute_readonly(source, validated, QueryLimits())
    assert page.rows[0][0] == 3


@retry_connect()
async def test_schema_mismatch_fails_closed(probe: Probe) -> None:
    source = _source(probe, allowed_schema="other_probe")
    validated = validate_query(
        f"SELECT id FROM {TABLE}", {}, "other_probe", source.allowed_tables, 201
    )

    with pytest.raises(DomainError) as exc:
        await execute_readonly(source, validated, QueryLimits())

    assert exc.value.code == "database.query_failed"


@retry_connect()
async def test_multi_statements_cannot_reach_server(probe: Probe) -> None:
    source = _source(probe)
    resources = mysql._CallResources()
    connection = await mysql._connect(resources, source, QueryLimits())
    try:
        await mysql._apply_session_guards(connection)
        with pytest.raises(Exception):
            await connection.exec_driver_sql("SELECT 1; SELECT 2")
    finally:
        await mysql._release(resources)


@retry_connect()
async def test_server_timeout_interrupts(probe: Probe) -> None:
    source = _source(probe)
    resources = mysql._CallResources()
    connection = await mysql._connect(resources, source, QueryLimits())
    started = time.monotonic()
    try:
        await mysql._apply_session_guards(connection)
        await connection.exec_driver_sql("SET SESSION max_execution_time = 100")
        try:
            await connection.exec_driver_sql("SELECT SLEEP(2)")
        except Exception:
            assert time.monotonic() - started < 1.9
        else:
            pytest.skip("当前 MySQL 版本未中断 SLEEP")
    finally:
        await mysql._release(resources)
