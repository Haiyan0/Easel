"""T3 执行层的占位符翻译（I-1）：T2 与 SQLAlchemy ``text()`` 必须对「哪里是占位符」一致。

**不连任何数据库**：把执行层真正交给 ``stream()`` 的语句用**真实 MySQL 方言**编译，
断言数据库最终收到的文本与 SQLAlchemy 认出的绑定名。这样做的理由：本缺陷的物理形态
恰好是「编译产物被改写」（字面量变 ``%s``、凭空多出一个绑定），而编译层在离库环境里
既可观测、也可伪 —— 旧实现下 :func:`test_literal_colon_stays_data_in_driver_statement`
的编译产物是 ``SELECT '%s' AS lit, %s AS bound``（字面量被当成绑定位），
:func:`test_literal_only_sql_does_not_create_binds` 会凭空多出 ``missing`` 绑定。

真库侧的端到端证据（取回的字面量确实是原值）见
``tests/integration/database/test_mysql_readonly.py`` 的占位符一节。
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
import sqlglot
from sqlalchemy.dialects import mysql as sa_mysql

from dbquery import mysql
from dbquery.errors import DomainError
from dbquery.mysql import QueryLimits
from dbquery.policy import (
    DATABASE_SQL_UNSUPPORTED,
    ValidatedQuery,
    validate_query,
)

SCHEMA = "sales"
TABLES = ("orders", "customers")


class _CompilingResult:
    """假流式结果：只提供 ``_read_page`` 真正用到的那两个入口。"""

    def __init__(self, columns: tuple[str, ...], rows: tuple[tuple[object, ...], ...]) -> None:
        self._columns = columns
        self._rows = list(rows)

    def keys(self) -> tuple[str, ...]:
        return self._columns

    async def fetchmany(self, size: int) -> list[tuple[object, ...]]:
        chunk, self._rows = self._rows[:size], self._rows[size:]
        return chunk


class _CompilingConnection:
    """假连接：``stream()`` 用**真实 MySQL 方言**编译收到的语句并记账。

    记账两件事：``compiled``（数据库最终收到的文本，可判「字面量有没有被改写」）与
    ``binds``（SQLAlchemy 认出的绑定名，可判「有没有凭空多出/漏掉绑定」）。
    """

    def __init__(
        self,
        columns: tuple[str, ...] = ("lit",),
        rows: tuple[tuple[object, ...], ...] = (),
    ) -> None:
        self.compiled: str | None = None
        self.binds: list[str] = []
        self._result = _CompilingResult(columns, rows)

    async def stream(self, statement: object, parameters: object = None) -> _CompilingResult:
        compiled = statement.compile(dialect=sa_mysql.dialect())  # type: ignore[attr-defined]
        self.compiled = str(compiled)
        self.binds = sorted(compiled.params)
        return self._result


async def _read_through_execution_layer(
    sql: str, parameters: Mapping[str, object] | None = None
) -> _CompilingConnection:
    """走**真实** T2 校验与真实 T3 ``_read_page``，返回记账后的假连接。"""
    validated = validate_query(sql, dict(parameters or {}), SCHEMA, TABLES, 200)
    connection = _CompilingConnection()
    await mysql._read_page(mysql._CallResources(), connection, validated, QueryLimits(max_rows=200))
    return connection


@pytest.mark.parametrize(
    ("sql", "parameters", "expected_compiled", "expected_binds"),
    [
        pytest.param(
            "SELECT ':floor' AS lit, :floor AS bound",
            {"floor": 1},
            "SELECT ':floor' AS lit, %s AS bound LIMIT 200",
            ["floor"],
            id="冒号紧跟引号且与真实占位符同名",
        ),
        pytest.param(
            "SELECT id FROM orders WHERE note = ' :q'",
            {},
            "SELECT id FROM orders WHERE note = ' :q' LIMIT 200",
            [],
            id="冒号紧跟空格（无参数）",
        ),
        pytest.param(
            "SELECT id FROM orders WHERE note = ',:r' OR id = :r",
            {"r": 2},
            "SELECT id FROM orders WHERE note = ',:r' OR id = %s LIMIT 200",
            ["r"],
            id="冒号紧跟逗号且与真实占位符同名",
        ),
        pytest.param(
            "SELECT id FROM orders WHERE code LIKE '%:x%'",
            {},
            "SELECT id FROM orders WHERE code LIKE '%%:x%%' LIMIT 200",
            [],
            id="字面量里的 % 与 :name 并存",
        ),
        pytest.param(
            "SELECT 'it''s :z' AS s, :w AS w FROM orders",
            {"w": 4},
            "SELECT 'it''s :z' AS s, %s AS w FROM orders LIMIT 200",
            ["w"],
            id="转义引号之后仍是字面量",
        ),
        pytest.param(
            "SELECT 'a::b' AS x, :c AS y FROM orders",
            {"c": 3},
            "SELECT 'a::b' AS x, %s AS y FROM orders LIMIT 200",
            ["c"],
            id="双冒号是数据，不得被改写",
        ),
        pytest.param(
            "SELECT `:a` AS x, :b AS y FROM orders",
            {"b": 5},
            "SELECT `:a` AS x, %s AS y FROM orders LIMIT 200",
            ["b"],
            id="引号标识符里的冒号同样是数据",
        ),
    ],
)
async def test_literal_colon_stays_data_in_driver_statement(
    sql: str,
    parameters: Mapping[str, object],
    expected_compiled: str,
    expected_binds: list[str],
) -> None:
    """字面量/引号标识符里的 ``:name`` 必须原样到达数据库，且**不得**产生绑定。"""
    connection = await _read_through_execution_layer(sql, parameters)

    assert connection.compiled == expected_compiled
    assert connection.binds == expected_binds


async def test_literal_only_sql_does_not_create_binds() -> None:
    """无参数查询：字面量里的 ``:missing`` 不得凭空造出一个绑定（否则执行即失败）。"""
    connection = await _read_through_execution_layer(
        "SELECT id FROM orders WHERE note = ':missing'"
    )

    assert connection.compiled == "SELECT id FROM orders WHERE note = ':missing' LIMIT 200"
    assert connection.binds == []


async def test_repeated_placeholder_name_binds_every_occurrence() -> None:
    """反向对照：真实占位符**重名多次**时必须各自绑定（防「为了修 I-1 把绑定收紧」）。"""
    connection = await _read_through_execution_layer(
        "SELECT id FROM orders WHERE a = :x OR b = :x", {"x": 1}
    )

    assert connection.compiled == "SELECT id FROM orders WHERE a = %s OR b = %s LIMIT 200"
    assert connection.binds == ["x"]


def test_bind_params_regex_matches_sqlalchemy() -> None:
    """本地复刻的正则必须与 SQLAlchemy 真正在用的那条**逐字**一致（上游一改即红）。

    执行层靠它判定「哪些冒号会被 ``text()`` 当成绑定位」，上游改了正则而这里没跟，
    转义就会漏面（漏面由 :func:`mysql._bound_statement` 的对账兜成拒绝，而不是静默改写；
    本用例让漂移更早、更直接地暴露）。

    私有路径刻意写在用例内部而不是模块顶部：上游若改名/搬家，红的应当是**这一条**，
    而不是让整个文件的用例都收集失败。
    """
    from sqlalchemy.sql.compiler import BIND_PARAMS

    assert mysql._BIND_PARAMS.pattern == BIND_PARAMS.pattern


@pytest.mark.parametrize(
    ("text", "expression"),
    [
        pytest.param(
            "SELECT id FROM orders WHERE id = :id LIMIT 200",
            "SELECT id FROM orders LIMIT 200",
            id="文本里的占位符多于已验证 AST",
        ),
        pytest.param(
            "SELECT id FROM orders LIMIT 200",
            "SELECT id FROM orders WHERE id = :id LIMIT 200",
            id="已验证 AST 里的占位符多于文本",
        ),
    ],
)
async def test_refuses_when_text_placeholders_disagree_with_validated_ast(
    text: str, expression: str
) -> None:
    """对不齐时 fail-closed：宁可报「结构不受支持」，也不让字面量被静默改写。

    本用例代表「T2 的 AST 判断与驱动层正则无法靠转义对齐」的形态（当前实现不可达，
    是防未来漂移的安全网，例如 sqlglot 改了重建文本的渲染方式）。
    用手工构造的 ``ValidatedQuery`` 直达执行层：生产入口只接受校验结果，这里要伪的是
    「校验结果自身前后不一致」——只有绕过校验才构造得出来。
    """
    forged = ValidatedQuery(
        sql=text,
        parameters={"id": 1} if ":id" in text else {},
        expression=sqlglot.parse_one(expression, read="mysql"),
    )

    with pytest.raises(DomainError) as caught:
        await mysql._read_page(
            mysql._CallResources(), _CompilingConnection(), forged, QueryLimits(max_rows=200)
        )

    assert caught.value.code == DATABASE_SQL_UNSUPPORTED
    # 拒绝文案不得回显 SQL 正文（规格 §8）
    assert text not in caught.value.message
