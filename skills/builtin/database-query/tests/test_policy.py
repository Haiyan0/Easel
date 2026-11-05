"""SQL 只读策略（T2）的单元测试：**不建立任何数据库连接，也不执行任何 SQL**。

覆盖规格 §5.1（AST 白名单：完整输入方言解析、恰好一条语句、递归校验所有节点与函数、
无法解析或未覆盖的结构默认拒绝）、§5.2（首版允许与拒绝清单）、§7.1（max_rows 与
LIMIT/OFFSET 资源边界）、§8（稳定错误码与「不回传底层异常／SQL 正文／参数」）。

正向覆盖与拒绝覆盖并重：只读 CTE／UNION／聚合、参数绑定、字符串含 UPDATE 或分号、
较小 LIMIT 保留都必须**放行**——「一律拒绝」不算通过安全测试。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

import pytest

from dbquery.errors import DomainError
from dbquery.policy import (
    DATABASE_LIMIT_EXCEEDED,
    DATABASE_SQL_NOT_READONLY,
    DATABASE_SQL_UNSUPPORTED,
    DATABASE_TABLE_NOT_ALLOWED,
    LIMIT_EXCEEDED_TEXT,
    MAX_ROWS_DEFAULT,
    MAX_ROWS_LIMIT,
    MAX_ROWS_LIMIT_WITH_PROBE,
    SQL_NOT_READONLY_TEXT,
    SQL_UNSUPPORTED_TEXT,
    TABLE_NOT_ALLOWED_TEXT,
    ValidatedQuery,
    validate_query,
)

_SCHEMA = "sales"
_TABLES = ("orders", "customers")


def _validate(
    sql: str,
    parameters: Mapping[str, object] | None = None,
    *,
    schema: str = _SCHEMA,
    tables: tuple[str, ...] = _TABLES,
    max_rows: int = MAX_ROWS_DEFAULT,
) -> ValidatedQuery:
    """按简报第 2 节的签名调用（参数默认空映射）。"""
    return validate_query(sql, {} if parameters is None else parameters, schema, tables, max_rows)


def _refuse(
    sql: str,
    code: str,
    parameters: Mapping[str, object] | None = None,
    **kwargs: Any,
) -> DomainError:
    """断言被拒绝且错误码正确；顺带断言 SQL 正文未出现在对外文案里（§8）。"""
    with pytest.raises(DomainError) as caught:
        _validate(sql, parameters, **kwargs)
    error = caught.value
    assert error.code == code
    assert error.status == 400
    stripped = sql.strip()
    # 空串是任何字符串的子串，只在有实际正文时断言「SQL 正文未外泄」
    if stripped:
        assert stripped not in error.message
        assert stripped not in str(error)
    return error


def _effective_limit(validated: ValidatedQuery) -> int | None:
    """从受控重建的 SQL 里读出最外层生效的 LIMIT（下游据此判断截断）。"""
    marker = " LIMIT "
    assert marker in validated.sql, validated.sql
    return int(validated.sql.rsplit(marker, 1)[1].split(" ")[0])


# ---------------------------------------------------------------------------
# §5.2 拒绝：写语句、DDL、管理语句、事务、多语句（「恰好一条且只读」）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        pytest.param("DELETE FROM orders", DATABASE_SQL_NOT_READONLY, id="DELETE"),
        pytest.param("UPDATE orders SET a = 1", DATABASE_SQL_NOT_READONLY, id="UPDATE"),
        pytest.param("INSERT INTO orders VALUES (1)", DATABASE_SQL_NOT_READONLY, id="INSERT"),
        pytest.param("REPLACE INTO orders VALUES (1)", DATABASE_SQL_NOT_READONLY, id="REPLACE"),
        pytest.param("DROP TABLE orders", DATABASE_SQL_NOT_READONLY, id="DROP"),
        pytest.param("CREATE TABLE x (a INT)", DATABASE_SQL_NOT_READONLY, id="CREATE"),
        pytest.param("ALTER TABLE orders ADD COLUMN x INT", DATABASE_SQL_NOT_READONLY, id="ALTER"),
        pytest.param("TRUNCATE TABLE orders", DATABASE_SQL_NOT_READONLY, id="TRUNCATE"),
        pytest.param("RENAME TABLE orders TO o2", DATABASE_SQL_NOT_READONLY, id="RENAME"),
        pytest.param("GRANT ALL ON *.* TO 'a'@'%'", DATABASE_SQL_NOT_READONLY, id="GRANT"),
        pytest.param("REVOKE ALL ON *.* FROM 'a'@'%'", DATABASE_SQL_NOT_READONLY, id="REVOKE"),
        pytest.param("CALL some_proc()", DATABASE_SQL_NOT_READONLY, id="CALL"),
        pytest.param("SET @x = 1", DATABASE_SQL_NOT_READONLY, id="SET"),
        pytest.param("SET GLOBAL max_connections = 1", DATABASE_SQL_NOT_READONLY, id="SET-GLOBAL"),
        pytest.param("START TRANSACTION", DATABASE_SQL_NOT_READONLY, id="事务开始"),
        pytest.param("COMMIT", DATABASE_SQL_NOT_READONLY, id="COMMIT"),
        pytest.param("ROLLBACK", DATABASE_SQL_NOT_READONLY, id="ROLLBACK"),
        pytest.param("PREPARE s FROM 'SELECT 1'", DATABASE_SQL_NOT_READONLY, id="PREPARE"),
        pytest.param("EXECUTE s", DATABASE_SQL_NOT_READONLY, id="EXECUTE"),
        pytest.param("SHOW TABLES", DATABASE_SQL_NOT_READONLY, id="SHOW"),
        pytest.param("DESCRIBE orders", DATABASE_SQL_NOT_READONLY, id="DESCRIBE"),
        pytest.param("EXPLAIN SELECT * FROM orders", DATABASE_SQL_NOT_READONLY, id="EXPLAIN"),
        pytest.param("SELECT 1; DROP TABLE orders", DATABASE_SQL_NOT_READONLY, id="多语句含写"),
        pytest.param("SELECT 1; SELECT 2", DATABASE_SQL_UNSUPPORTED, id="多语句均只读"),
        pytest.param("", DATABASE_SQL_UNSUPPORTED, id="空串"),
        pytest.param("   ", DATABASE_SQL_UNSUPPORTED, id="纯空白"),
        pytest.param("-- 只有注释", DATABASE_SQL_UNSUPPORTED, id="只有注释"),
        pytest.param("SELECT FROM WHERE", DATABASE_SQL_UNSUPPORTED, id="语法错误"),
        pytest.param("SELECT ((((1", DATABASE_SQL_UNSUPPORTED, id="括号不闭合"),
        pytest.param("DO SLEEP(1)", DATABASE_SQL_UNSUPPORTED, id="DO"),
        pytest.param(
            "LOAD DATA INFILE '/tmp/x' INTO TABLE orders",
            DATABASE_SQL_UNSUPPORTED,
            id="LOAD DATA",
        ),
    ],
)
def test_rejects_non_readonly_statements(sql: str, code: str) -> None:
    _refuse(sql, code)


def test_rejects_deeply_nested_input_without_crashing() -> None:
    """超深嵌套会让 sqlglot 触发 RecursionError：必须按「不受支持」拒绝而非 500。"""
    _refuse("SELECT " + "(" * 4000 + "1" + ")" * 4000, DATABASE_SQL_UNSUPPORTED)


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("WITH c AS (DELETE FROM orders) SELECT * FROM c", id="CTE 体内 DELETE"),
        pytest.param("WITH c AS (UPDATE orders SET a = 1) SELECT * FROM c", id="CTE 体内 UPDATE"),
        pytest.param(
            "WITH c AS (INSERT INTO orders VALUES (1)) SELECT * FROM c", id="CTE 体内 INSERT"
        ),
    ],
)
def test_rejects_write_nested_in_cte_body(sql: str) -> None:
    """**嵌套写入**（§9.1 必覆盖项）：sqlglot 会把「只读外套 + CTE 体内的写语句」解析成
    顶层 ``Select`` 加一个内层 ``Delete``/``Update``/``Insert`` 节点——**只看顶层类型必然
    漏判**，拦住它的是节点闭集（拒绝日志为 ``reason=unknown_node node='Delete'``）。

    判别性：把 ``_ALLOWED_NODES`` 的节点检查关掉，本条即红（顶层是 Select、表名在白名单内，
    其余检查全过）。
    """
    _refuse(sql, DATABASE_SQL_UNSUPPORTED)


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT * FROM (DELETE FROM orders) t", id="派生表内 DELETE"),
        pytest.param("SELECT * FROM (INSERT INTO orders VALUES (1)) t", id="派生表内 INSERT"),
        pytest.param("SELECT * FROM (UPDATE orders SET a = 1) t", id="派生表内 UPDATE"),
        pytest.param("SELECT id FROM orders UNION DELETE FROM orders", id="UNION 分支内 DELETE"),
        pytest.param(
            "SELECT id FROM orders WHERE id IN (DELETE FROM orders)", id="IN 子查询内 DELETE"
        ),
        pytest.param("SELECT (DELETE FROM orders)", id="标量子查询内 DELETE"),
    ],
)
def test_rejects_write_nested_outside_cte(sql: str) -> None:
    """CTE 之外的嵌套写入：sqlglot 直接判 ``ParseError``，走「无法解析一律拒绝」这条路。

    与上一条**并行存在**是有意的：同一类输入在 sqlglot 里有两条完全不同的落点
    （解析成内层节点 / 解析期失败），只钉住一条会漏掉另一条路径的回归。
    """
    _refuse(sql, DATABASE_SQL_UNSUPPORTED)


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("WITH c AS (SELECT id FROM orders) SELECT * FROM c", id="只读 CTE"),
        pytest.param("SELECT * FROM (SELECT id FROM orders) t", id="只读派生表"),
        pytest.param(
            "SELECT id FROM orders WHERE id IN (SELECT id FROM customers)", id="只读 IN 子查询"
        ),
    ],
)
def test_readonly_nested_subqueries_are_not_misjudged(sql: str) -> None:
    """反面对照：同样「嵌套」但只读的写法必须放行——防「嵌套一律拒」的偷懒实现。"""
    assert _validate(sql).sql


# ---------------------------------------------------------------------------
# §5.2 拒绝：副作用与资源滥用语法
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        pytest.param(
            "SELECT * FROM orders INTO OUTFILE '/tmp/x.txt'",
            DATABASE_SQL_UNSUPPORTED,
            id="INTO OUTFILE",
        ),
        pytest.param(
            "SELECT * FROM orders INTO DUMPFILE '/tmp/x.bin'",
            DATABASE_SQL_UNSUPPORTED,
            id="INTO DUMPFILE",
        ),
        pytest.param("SELECT id INTO @x FROM orders", DATABASE_SQL_NOT_READONLY, id="INTO 变量"),
        pytest.param("SELECT * FROM orders FOR UPDATE", DATABASE_SQL_NOT_READONLY, id="FOR UPDATE"),
        pytest.param("SELECT * FROM orders FOR SHARE", DATABASE_SQL_NOT_READONLY, id="FOR SHARE"),
        pytest.param(
            "SELECT * FROM orders LOCK IN SHARE MODE",
            DATABASE_SQL_NOT_READONLY,
            id="LOCK IN SHARE MODE",
        ),
        pytest.param("SELECT GET_LOCK('a', 1)", DATABASE_SQL_UNSUPPORTED, id="GET_LOCK"),
        pytest.param("SELECT RELEASE_LOCK('a')", DATABASE_SQL_UNSUPPORTED, id="RELEASE_LOCK"),
        pytest.param("SELECT SLEEP(1)", DATABASE_SQL_UNSUPPORTED, id="SLEEP"),
        pytest.param(
            "SELECT BENCHMARK(1000000, MD5('x'))", DATABASE_SQL_UNSUPPORTED, id="BENCHMARK"
        ),
        pytest.param("SELECT LOAD_FILE('/etc/passwd')", DATABASE_SQL_UNSUPPORTED, id="LOAD_FILE"),
        pytest.param("SELECT @x := 1 FROM orders", DATABASE_SQL_NOT_READONLY, id="变量赋值"),
        pytest.param(
            "SELECT * FROM orders WHERE id = @x", DATABASE_SQL_NOT_READONLY, id="变量读取"
        ),
    ],
)
def test_rejects_side_effect_and_resource_abuse(sql: str, code: str) -> None:
    _refuse(sql, code)


# ---------------------------------------------------------------------------
# §5.2 拒绝：注释与优化器 hint（含 MySQL 可执行版本注释）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT * FROM orders -- 行注释", id="行注释"),
        pytest.param("SELECT * /* 块注释 */ FROM orders", id="块注释"),
        pytest.param("SELECT /*! STRAIGHT_JOIN */ * FROM orders", id="可执行注释"),
        pytest.param("SELECT /*!80000 STRAIGHT_JOIN */ * FROM orders", id="可执行注释带版本号"),
        pytest.param(
            "SELECT * FROM orders /*!50000 UNION SELECT password FROM users */",
            id="可执行注释藏 UNION",
        ),
        pytest.param("SELECT 1 /*! ; DROP TABLE orders */", id="可执行注释藏分号"),
        pytest.param("SELECT /*+ MAX_EXECUTION_TIME(1) */ * FROM orders", id="优化器 hint"),
        pytest.param("SELECT HIGH_PRIORITY * FROM orders", id="语句修饰符"),
        pytest.param("SELECT SQL_CALC_FOUND_ROWS * FROM orders", id="SQL_CALC_FOUND_ROWS"),
    ],
)
def test_rejects_comments_and_hints(sql: str) -> None:
    _refuse(sql, DATABASE_SQL_UNSUPPORTED)


# ---------------------------------------------------------------------------
# §5.2 拒绝：递归 CTE、笛卡尔连接、无法确定引用对象的结构
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(
            "WITH RECURSIVE c AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM c) SELECT * FROM c",
            id="顶层递归 CTE",
        ),
        pytest.param(
            "WITH c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 5) SELECT * FROM c",
            id="MySQL 列名写法",
        ),
        pytest.param(
            "WITH a AS (SELECT 1 AS n), b AS (WITH RECURSIVE d AS "
            "(SELECT 1 AS n UNION ALL SELECT n + 1 FROM d) SELECT * FROM d) SELECT * FROM a",
            id="嵌套递归 CTE（外层非递归）",
        ),
        pytest.param("WITH c AS (SELECT * FROM c) SELECT * FROM c", id="自引用无关键字"),
        pytest.param(
            "WITH RECURSIVE c AS (SELECT 1 AS n) SELECT n FROM c",
            id="带关键字但无自引用",
        ),
    ],
)
def test_rejects_recursive_cte(sql: str) -> None:
    """⚠️ 递归标记在 ``With`` 节点上（``exp.CTE`` 上恒为 None），且须遍历全部 With。"""
    _refuse(sql, DATABASE_SQL_UNSUPPORTED)


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT * FROM orders, customers", id="逗号隐式连接"),
        pytest.param("SELECT * FROM orders CROSS JOIN customers", id="CROSS JOIN"),
        pytest.param("SELECT * FROM orders JOIN customers", id="JOIN 无 ON"),
        pytest.param(
            "SELECT * FROM orders, customers WHERE orders.cid = customers.id",
            id="逗号连接带 WHERE 条件",
        ),
        pytest.param("SELECT * FROM orders NATURAL JOIN customers", id="NATURAL JOIN"),
    ],
)
def test_rejects_cartesian_and_implicit_joins(sql: str) -> None:
    _refuse(sql, DATABASE_SQL_UNSUPPORTED)


# ---------------------------------------------------------------------------
# §5.2 拒绝：函数白名单之外的函数（含带 schema 的函数与未登记内置）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT CONCAT(a, b) FROM orders", id="CONCAT"),
        pytest.param("SELECT NOW() FROM orders", id="NOW"),
        pytest.param("SELECT MD5('x') FROM orders", id="MD5"),
        pytest.param("SELECT IF(a, 1, 0) FROM orders", id="IF"),
        pytest.param("SELECT DATE_SUB(d, INTERVAL 1 DAY) FROM orders", id="DATE_SUB"),
        pytest.param("SELECT CURRENT_DATE FROM orders", id="CURRENT_DATE"),
        pytest.param("SELECT a DIV 2 FROM orders", id="DIV"),
        pytest.param("SELECT CASE WHEN a > 1 THEN 1 ELSE 0 END FROM orders", id="CASE"),
        pytest.param("SELECT my_schema.my_func(1) FROM orders", id="带 schema 的函数"),
        pytest.param("SELECT `my_func`(1) FROM orders", id="反引号函数"),
    ],
)
def test_rejects_functions_outside_whitelist(sql: str) -> None:
    """带 schema 的函数：schema 限定在解析期即丢失（只剩 Anonymous），故由白名单覆盖。"""
    _refuse(sql, DATABASE_SQL_UNSUPPORTED)


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(
            "SELECT id FROM orders INTERSECT SELECT id FROM customers",
            id="INTERSECT",
        ),
        pytest.param("SELECT id FROM orders EXCEPT SELECT id FROM customers", id="EXCEPT"),
        pytest.param("SELECT id, ROW_NUMBER() OVER (ORDER BY id) FROM orders", id="窗口函数"),
        pytest.param("SELECT id FROM orders GROUP BY id WITH ROLLUP", id="WITH ROLLUP"),
        pytest.param("SELECT JSON_EXTRACT(x, '$.a') FROM orders", id="JSON 函数"),
        pytest.param("SELECT id FROM orders WHERE name REGEXP 'a'", id="REGEXP"),
    ],
)
def test_rejects_structures_outside_first_version_scope(sql: str) -> None:
    """首版白名单是 §5.2 举证结构的**闭集**：其余只读结构同样默认拒绝并须补测试后才放开。"""
    _refuse(sql, DATABASE_SQL_UNSUPPORTED)


# ---------------------------------------------------------------------------
# §5.2 拒绝：CTE 名/别名不构成授权、跨库逃逸
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        pytest.param("SELECT id FROM payroll", DATABASE_TABLE_NOT_ALLOWED, id="未登记表"),
        pytest.param("SELECT o.id FROM payroll o", DATABASE_TABLE_NOT_ALLOWED, id="别名不改授权"),
        pytest.param(
            "WITH c AS (SELECT id FROM payroll) SELECT * FROM c",
            DATABASE_TABLE_NOT_ALLOWED,
            id="CTE 体内未登记表",
        ),
        pytest.param(
            "WITH a AS (SELECT * FROM payroll), payroll AS (SELECT 1 AS x) SELECT * FROM a",
            DATABASE_TABLE_NOT_ALLOWED,
            id="后置 CTE 不得为前置 CTE 体内同名表背书",
        ),
        pytest.param("SELECT id FROM other_db.orders", DATABASE_TABLE_NOT_ALLOWED, id="跨库"),
        pytest.param("SELECT id FROM cat.other_db.orders", DATABASE_TABLE_NOT_ALLOWED, id="三段名"),
        pytest.param("SELECT id FROM other_db.orders", DATABASE_TABLE_NOT_ALLOWED, id="跨库别名"),
    ],
)
def test_rejects_unauthorized_table_references(sql: str, code: str) -> None:
    _refuse(sql, code)


# ---------------------------------------------------------------------------
# §7.1 拒绝与收紧：OFFSET、非字面量 LIMIT、资源上限
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT id FROM orders LIMIT 5, 10", id="LIMIT offset,count"),
        pytest.param("SELECT id FROM orders LIMIT 10 OFFSET 5", id="LIMIT ... OFFSET"),
        pytest.param("SELECT id FROM orders LIMIT :n", id="LIMIT 占位符"),
        pytest.param("SELECT id FROM orders LIMIT (1 + 1)", id="LIMIT 表达式"),
        pytest.param("SELECT id FROM orders LIMIT 10 OFFSET :off", id="OFFSET 占位符"),
    ],
)
def test_rejects_nonzero_offset_and_non_literal_limit(sql: str) -> None:
    _refuse(sql, DATABASE_SQL_UNSUPPORTED)


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(
            "SELECT * FROM (SELECT id FROM orders LIMIT 10 OFFSET 900000) AS t",
            id="派生表深偏移",
        ),
        pytest.param(
            "SELECT * FROM (SELECT id FROM orders LIMIT 900000, 10) AS t",
            id="派生表 offset,count 归一形",
        ),
        pytest.param(
            "SELECT id FROM orders WHERE id IN (SELECT id FROM customers LIMIT 1000000)",
            id="IN 子查询超大 LIMIT",
        ),
        pytest.param(
            "SELECT id FROM orders WHERE id IN (SELECT id FROM customers LIMIT 10 OFFSET 5)",
            id="IN 子查询偏移",
        ),
        pytest.param(
            "WITH c AS (SELECT id FROM orders LIMIT 10 OFFSET 900000) SELECT * FROM c",
            id="CTE 定义体内深偏移",
        ),
    ],
)
def test_rejects_unbounded_inner_row_windows(sql: str) -> None:
    """内层行窗口同样受 §7.1 约束：外层 LIMIT 只限返回行数，不限内层偏移深度。

    反向对照（内层小 LIMIT 必须放行）见 :func:`test_accepts_bounded_inner_row_windows`。
    """
    _refuse(sql, DATABASE_SQL_UNSUPPORTED)


@pytest.mark.parametrize(
    ("sql", "expected_sql"),
    [
        pytest.param(
            "SELECT COUNT(*) FROM (SELECT id FROM orders LIMIT 5) x",
            "SELECT COUNT(*) FROM (SELECT id FROM orders LIMIT 5) AS x LIMIT 200",
            id="派生表小 LIMIT",
        ),
        pytest.param(
            "SELECT * FROM (SELECT id FROM orders LIMIT 5 OFFSET 0) AS t",
            "SELECT * FROM (SELECT id FROM orders LIMIT 5 OFFSET 0) AS t LIMIT 200",
            id="派生表零偏移",
        ),
        pytest.param(
            "WITH c AS (SELECT id FROM orders LIMIT 3) SELECT * FROM c",
            "WITH c AS (SELECT id FROM orders LIMIT 3) SELECT * FROM c LIMIT 200",
            id="CTE 内小 LIMIT",
        ),
        pytest.param(
            "(SELECT id FROM orders LIMIT 5) UNION (SELECT id FROM customers LIMIT 9)",
            "(SELECT id FROM orders LIMIT 5) UNION (SELECT id FROM customers LIMIT 9) LIMIT 200",
            id="UNION 分支各自小 LIMIT",
        ),
        pytest.param(
            "SELECT id FROM orders WHERE id IN (SELECT id FROM customers LIMIT 10)",
            "SELECT id FROM orders WHERE id IN (SELECT id FROM customers LIMIT 10) LIMIT 200",
            id="IN 子查询小 LIMIT",
        ),
    ],
)
def test_accepts_bounded_inner_row_windows(sql: str, expected_sql: str) -> None:
    """内层有限小 LIMIT 必须保留（不得因为内层检查而误伤「先截断再聚合」这类合法写法）。

    ★ 断言是**逐字比对**而不是「有 SQL 就通过」：内层行窗口的规格是**只拒不改**
    （改写内层 LIMIT 会改语义：先截断再聚合/连接的结果不同），而「只拒不改」这条性质
    只有逐字比对重建结果才钉得住 —— 若实现某天把内层 LIMIT 也一并改写（例如照搬最外层
    那套「收紧到 max_rows」），语义回归会**无声通过**「有 SQL」型断言。
    """
    validated = _validate(sql)

    assert isinstance(validated, ValidatedQuery)
    assert validated.sql == expected_sql


def test_rejects_parameterized_inner_row_window() -> None:
    """内层行窗口全用绑定参数：SQL 文本可以固定不变、翻页深度全靠参数值，与写死深偏移等价。"""
    _refuse(
        "SELECT * FROM (SELECT id FROM orders LIMIT :n OFFSET :off) AS t",
        DATABASE_SQL_UNSUPPORTED,
        {"n": 50, "off": 500},
    )


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(
            "SELECT * FROM ((SELECT id FROM orders) LIMIT 10 OFFSET 900000) t",
            id="Subquery 承载深偏移",
        ),
        pytest.param(
            "SELECT * FROM ((SELECT id FROM orders) LIMIT 900000, 10) t",
            id="Subquery 承载 offset,count 归一形",
        ),
        pytest.param(
            "SELECT id FROM orders WHERE id IN ((SELECT id FROM customers) LIMIT 1000000)",
            id="IN 内 Subquery 承载超大 LIMIT",
        ),
        pytest.param(
            "WITH c AS ((SELECT id FROM orders) LIMIT 10 OFFSET 900000) SELECT * FROM c",
            id="CTE 内 Subquery 承载深偏移",
        ),
        pytest.param(
            "SELECT id FROM orders WHERE id IN ((SELECT id FROM customers) LIMIT 10 OFFSET 5)",
            id="IN 内 Subquery 承载深偏移",
        ),
        pytest.param(
            "SELECT id FROM orders WHERE EXISTS ((SELECT id FROM customers) LIMIT 1000000)",
            id="EXISTS 直挂超大 LIMIT",
        ),
    ],
)
def test_rejects_row_window_on_non_query_carriers(sql: str) -> None:
    """行窗口的**承载者不止查询节点**：加一对括号就挂到 ``Subquery`` 上，
    ``EXISTS ((…) LIMIT …)`` 甚至直接挂在 ``Exists`` 下。

    实测承载者（探针 `probe_t2g.py`）：
    - `SELECT * FROM ((…) LIMIT …) t` / `IN ((…) LIMIT …)` / CTE 内同形 → Limit/Offset 的
      父节点是 **Subquery**（加一对括号就换承载者）；
    - `WHERE EXISTS ((…) LIMIT …)` → 父节点是 **Exists**，且 `Subquery.args["limit"]` 是 None；
    - 注：`EXISTS ((…) LIMIT … OFFSET …)` **本身不可解析**（sqlglot 判 ParseError），
      故 EXISTS 承载那一格只用 LIMIT 形钉住。

    ⇒ 按「承载节点类型」枚举必然漏面（这正是上一版修复的缺陷）；实现改为按
    ``exp.Limit``/``exp.Offset`` **操作符节点**枚举、用身份比较排除最外层那两个，
    因此与括号形态、承载者类型无关。判别性：把实现改回按查询节点枚举，本条即红。
    """
    _refuse(sql, DATABASE_SQL_UNSUPPORTED)


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(
            "SELECT * FROM ((SELECT id FROM orders) LIMIT 5) t", id="Subquery 承载小 LIMIT"
        ),
        pytest.param(
            "SELECT * FROM ((SELECT id FROM orders) LIMIT 5 OFFSET 0) t", id="Subquery 承载零偏移"
        ),
        pytest.param(
            "SELECT id FROM orders WHERE EXISTS ((SELECT id FROM customers) LIMIT 10)",
            id="EXISTS 直挂小 LIMIT",
        ),
    ],
)
def test_accepts_bounded_row_window_on_non_query_carriers(sql: str) -> None:
    """反面对照：同样挂法的**受限**行窗口必须放行——防「见到括号就拒」的偷懒实现。"""
    assert _validate(sql).sql


@pytest.mark.parametrize(
    ("sql", "parameters"),
    [
        pytest.param("SELECT * FROM :d.orders", {"d": "sales"}, id="schema 位是占位符"),
        pytest.param(
            "SELECT * FROM :d.:t", {"d": "sales", "t": "orders"}, id="schema 与表名都是占位符"
        ),
    ],
)
def test_rejects_non_identifier_in_schema_position(
    sql: str, parameters: Mapping[str, object]
) -> None:
    """schema/表名位必须能静态确定：非标识符节点会让跨库检查静默失效（§5.2 禁止跨库逃逸）。

    sqlglot 的 ``table.db`` 对 ``Placeholder`` 返回空串，于是 ``if table.db:`` 判假、
    该表被当成「未限定 schema」直接落到表白名单分支——``:d.orders`` 因此整条绕过检查。
    """
    _refuse(sql, DATABASE_TABLE_NOT_ALLOWED, parameters)


def test_rejects_oversized_sql_text_and_max_rows() -> None:
    """§7.1 资源边界：SQL 文本 32 KiB、max_rows 不得越过**探针那一格**（1001）。

    注意拒的位置是 ``MAX_ROWS_LIMIT + 2`` 而不是 ``+ 1``：``+ 1`` 是 §7.1 逐字允许的
    截断探针，必须放行（正向用例见 :func:`test_accepts_probe_slot_above_hard_cap`）。
    """
    huge = "SELECT id FROM orders WHERE id IN (" + ", ".join(str(i) for i in range(9000)) + ")"
    assert len(huge) > 32 * 1024

    _refuse(huge, DATABASE_LIMIT_EXCEEDED)
    _refuse("SELECT id FROM orders", DATABASE_LIMIT_EXCEEDED, max_rows=MAX_ROWS_LIMIT + 2)
    _refuse("SELECT id FROM orders", DATABASE_LIMIT_EXCEEDED, max_rows=0)
    _refuse("SELECT id FROM orders", DATABASE_LIMIT_EXCEEDED, max_rows=-1)


def test_accepts_max_rows_at_hard_cap() -> None:
    validated = _validate("SELECT id FROM orders", max_rows=MAX_ROWS_LIMIT)

    assert _effective_limit(validated) == MAX_ROWS_LIMIT


def test_accepts_probe_slot_above_hard_cap() -> None:
    """探针那一格（硬上限 + 1 = 1001）必须放行，且重建 SQL 的 LIMIT 就是 1001。

    §7.1 的 1000 是「**返回给模型的行数**」上限，不是 LIMIT 子句的上限；同一条 §7.1 又
    逐字允许「少读一条作截断探针，最多 ``max_rows + 1``」。调用方按该约定传
    ``limits.max_rows + 1`` 时若被拒，「完整 / 被截断」就再也分不出来（§11 阻断项
    「返回已截断结果却声称完整」）。这里**逐字比对**重建结果，而不是只断言「不再拒绝」：
    纯拒绝型断言在「把拒的位置挪一格」时同样会绿，钉不住这一格。
    """
    validated = _validate("SELECT id FROM orders", max_rows=MAX_ROWS_LIMIT_WITH_PROBE)

    assert _effective_limit(validated) == MAX_ROWS_LIMIT_WITH_PROBE
    assert validated.sql == "SELECT id FROM orders LIMIT 1001"


# ---------------------------------------------------------------------------
# 参数绑定：具名占位符，保持驱动绑定、禁止拼值
# ---------------------------------------------------------------------------


def test_binds_named_parameters_without_splicing_values() -> None:
    sql = "SELECT id FROM orders WHERE created_at >= :start_time AND status = :status LIMIT 20"

    validated = _validate(sql, {"start_time": "2026-01-01", "status": "paid"})

    assert validated.parameters == {"start_time": "2026-01-01", "status": "paid"}
    # 重建后的 SQL 保留原占位符节点（不还原、不拼值）
    assert ":start_time" in validated.sql
    assert ":status" in validated.sql
    assert "2026-01-01" not in validated.sql
    assert "paid" not in validated.sql


def test_repeated_placeholder_uses_single_parameter() -> None:
    validated = _validate("SELECT id FROM orders WHERE a = :x OR b = :x", {"x": 1})

    assert validated.parameters == {"x": 1}
    assert validated.sql.count(":x") == 2


def test_parameters_are_copied_and_immutable() -> None:
    supplied: dict[str, object] = {"x": 1}

    validated = _validate("SELECT id FROM orders WHERE a = :x", supplied)
    supplied["x"] = 999

    assert validated.parameters == {"x": 1}
    with pytest.raises(TypeError):
        validated.parameters["y"] = 2


@pytest.mark.parametrize(
    ("sql", "parameters"),
    [
        pytest.param("SELECT id FROM orders WHERE a = :x", {}, id="缺少参数"),
        pytest.param("SELECT id FROM orders WHERE a = :x", {"x": 1, "y": 2}, id="多余参数"),
        pytest.param("SELECT id FROM orders", {"x": 1}, id="无占位符却传参"),
        pytest.param("SELECT id FROM orders WHERE a = ?", {"x": 1}, id="匿名占位符"),
        pytest.param("SELECT id FROM orders WHERE a = :x AND b = ?", {"x": 1}, id="混合占位符"),
        pytest.param("SELECT id FROM orders WHERE a = %s", {}, id="驱动格式占位符"),
    ],
)
def test_rejects_parameter_mismatch(sql: str, parameters: Mapping[str, object]) -> None:
    _refuse(sql, DATABASE_SQL_UNSUPPORTED, parameters)


@pytest.mark.parametrize(
    "parameters",
    [
        pytest.param([1, 2], id="数组"),
        pytest.param("x", id="字符串"),
        pytest.param(1, id="标量"),
    ],
)
def test_rejects_non_mapping_parameters(parameters: object) -> None:
    """运行期防线：工具层可能把 JSON 数组/标量直接传进来（类型标注之外的输入）。

    具名绑定是规格要求（§4 逐字 ``parameters: dict`` 与「命名绑定如 :start_time」），
    非映射形态必须按「结构不受支持」拒绝，而不是在后面 as_dict 之类的地方抛 500。
    """
    with pytest.raises(DomainError) as caught:
        validate_query(
            "SELECT id FROM orders",
            parameters,  # type: ignore[arg-type]
            _SCHEMA,
            _TABLES,
            MAX_ROWS_DEFAULT,
        )

    assert caught.value.code == DATABASE_SQL_UNSUPPORTED


# ---------------------------------------------------------------------------
# 正向覆盖：只读结构必须放行（不得以「一律拒绝」假装通过）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT id, name FROM orders WHERE status = 'paid'", id="普通 SELECT"),
        pytest.param("select id from orders", id="小写关键字"),
        pytest.param("SELECT id FROM orders;", id="分号结尾单语句"),
        pytest.param("SELECT `id` FROM `orders`", id="反引号标识符"),
        pytest.param("SELECT id FROM sales.orders", id="限定 schema 的白名单表"),
        pytest.param("WITH c AS (SELECT id FROM orders) SELECT * FROM c", id="只读 CTE"),
        pytest.param(
            "WITH c AS (SELECT id FROM orders WHERE id > 10) SELECT COUNT(*) FROM c",
            id="CTE 引用真实表",
        ),
        pytest.param("WITH orders AS (SELECT 1 AS n) SELECT n FROM orders", id="CTE 遮蔽同名表"),
        pytest.param("SELECT id FROM orders UNION SELECT id FROM customers", id="UNION"),
        pytest.param(
            "SELECT id FROM orders UNION ALL SELECT id FROM customers LIMIT 3", id="UNION ALL"
        ),
        pytest.param("SELECT status, COUNT(*) AS n FROM orders GROUP BY status", id="聚合"),
        pytest.param(
            "SELECT status, COUNT(*) AS n FROM orders GROUP BY status HAVING COUNT(*) > 1",
            id="GROUP BY + HAVING",
        ),
        pytest.param("SELECT DISTINCT status FROM orders", id="DISTINCT"),
        pytest.param("SELECT id FROM orders ORDER BY created_at DESC LIMIT 10", id="排序"),
        pytest.param("SELECT o.id FROM orders o JOIN customers c ON o.cid = c.id", id="JOIN ON"),
        pytest.param(
            "SELECT o.id FROM orders o LEFT JOIN customers c ON o.cid = c.id", id="LEFT JOIN"
        ),
        pytest.param("SELECT o.id FROM orders o JOIN customers c USING (cid)", id="JOIN USING"),
        pytest.param(
            "SELECT id FROM orders WHERE cid IN (SELECT id FROM customers)", id="子查询 IN"
        ),
        pytest.param("SELECT x.n FROM (SELECT COUNT(*) AS n FROM orders) x", id="FROM 子查询"),
        pytest.param(
            "SELECT id FROM orders o WHERE EXISTS (SELECT 1 FROM customers c WHERE c.id = o.cid)",
            id="EXISTS",
        ),
        pytest.param("SELECT id FROM orders WHERE id BETWEEN 1 AND 9", id="BETWEEN"),
        pytest.param("SELECT id FROM orders WHERE name LIKE 'a%'", id="LIKE 通配符"),
        pytest.param("SELECT id FROM orders WHERE note IS NULL", id="IS NULL"),
        pytest.param("SELECT id FROM orders WHERE note IS NOT NULL", id="IS NOT NULL"),
        pytest.param("SELECT id FROM orders WHERE a = 1 AND b = 2 OR c = 3", id="布尔组合"),
        pytest.param("SELECT id FROM orders WHERE NOT a = 1", id="NOT"),
        pytest.param("SELECT id FROM orders WHERE status IN ('paid', 'new')", id="IN 列表"),
        pytest.param("SELECT (a + b) * 2 - c / 3 % 4 AS t FROM orders", id="算术运算"),
        pytest.param("SELECT -a AS t FROM orders", id="一元负号"),
        pytest.param("SELECT id FROM orders WHERE a = TRUE", id="布尔字面量"),
        pytest.param("SELECT id FROM orders WHERE a <> 1", id="不等于"),
        pytest.param("SELECT COUNT(DISTINCT status) FROM orders", id="COUNT DISTINCT"),
        pytest.param(
            "SELECT COUNT(*), SUM(x), AVG(x), MIN(x), MAX(x), COALESCE(a, b), IFNULL(a, 0), "
            "NULLIF(a, b), ABS(x), ROUND(x, 2), LOWER(s), UPPER(s), LENGTH(s), CHAR_LENGTH(s), "
            "DATE(d), YEAR(d), MONTH(d), DAY(d), CAST(x AS CHAR) FROM orders",
            id="函数白名单全覆盖",
        ),
        pytest.param("SELECT CAST(x AS SIGNED) FROM orders", id="CAST 数值类型"),
    ],
)
def test_accepts_readonly_queries(sql: str) -> None:
    validated = _validate(sql)

    assert isinstance(validated, ValidatedQuery)
    assert validated.sql


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT id FROM orders WHERE note = 'a;b'", id="字符串内分号"),
        pytest.param(
            "SELECT id FROM orders WHERE note = 'please UPDATE later'", id="字符串含 UPDATE"
        ),
        pytest.param("SELECT id FROM orders WHERE note = 'DROP TABLE orders'", id="字符串含 DROP"),
        pytest.param("SELECT id FROM orders WHERE note = '-- not a comment'", id="字符串含注释符"),
        pytest.param("SELECT id FROM orders WHERE note = '50%'", id="字符串含百分号"),
    ],
)
def test_string_literals_are_not_misjudged(sql: str) -> None:
    """词法层面的关键字/分号/注释符判定不得误伤字符串字面量（§5.2 逐字要求）。"""
    validated = _validate(sql)

    assert "note" in validated.sql


# ---------------------------------------------------------------------------
# 受控重建与 LIMIT 语义（校验后执行的是重建结果，不是模型原文）
# ---------------------------------------------------------------------------


def test_rebuild_normalizes_sql_and_applies_default_limit() -> None:
    validated = _validate("select id from orders")

    assert validated.sql == "SELECT id FROM orders LIMIT 200"
    assert _effective_limit(validated) == MAX_ROWS_DEFAULT


def test_keeps_smaller_limit_and_tightens_larger_limit() -> None:
    kept = _validate("SELECT id FROM orders LIMIT 10")
    tightened = _validate("SELECT id FROM orders LIMIT 5000", max_rows=200)

    assert kept.sql == "SELECT id FROM orders LIMIT 10"
    assert tightened.sql == "SELECT id FROM orders LIMIT 200"


def test_applies_limit_to_union_toplevel() -> None:
    """UNION 的 LIMIT 落在 Union 节点上，管住整个并集（不是各分支各限一行数）。"""
    validated = _validate("SELECT id FROM orders UNION SELECT id FROM customers LIMIT 5000")

    assert validated.sql == "SELECT id FROM orders UNION SELECT id FROM customers LIMIT 200"


def test_accepts_zero_offset() -> None:
    """只拒绝**非零** OFFSET：零偏移不构成深分页通道。"""
    validated = _validate("SELECT id FROM orders LIMIT 0, 10")

    assert _effective_limit(validated) == 10


def test_returned_expression_is_the_validated_ast() -> None:
    validated = _validate("SELECT id FROM orders LIMIT 5000")

    assert validated.expression.args["limit"].expression.this == "200"


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT id FROM orders WHERE note = '50%'", id="字符串含百分号"),
        pytest.param("SELECT id FROM orders LIMIT 0, 10", id="零偏移"),
        pytest.param("SELECT a FROM orders UNION SELECT b FROM customers", id="UNION"),
        pytest.param(
            "WITH c AS (SELECT id FROM orders) SELECT * FROM c JOIN customers USING (cid)",
            id="CTE + JOIN USING",
        ),
        pytest.param("SELECT id FROM orders WHERE a IS NOT NULL AND b LIKE 'x%'", id="IS NOT NULL"),
        pytest.param("SELECT id FROM orders WHERE id = :i", id="占位符"),
    ],
)
def test_rebuilt_sql_is_stable_under_revalidation(sql: str) -> None:
    """受控重建的结果必须能被自身再次校验且不再变化：执行的就是校验过的那一份。"""
    once = _validate(sql, {"i": 1} if ":i" in sql else {})

    twice = _validate(once.sql, {"i": 1} if ":i" in sql else {})

    assert twice.sql == once.sql


# ---------------------------------------------------------------------------
# 类型与文案（§8：固定文案、不回传底层异常）
# ---------------------------------------------------------------------------


def test_validated_query_is_frozen() -> None:
    validated = _validate("SELECT id FROM orders")

    with pytest.raises(dataclasses.FrozenInstanceError):
        validated.sql = "DROP TABLE orders"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("sql", "code", "text"),
    [
        pytest.param(
            "DELETE FROM orders", DATABASE_SQL_NOT_READONLY, SQL_NOT_READONLY_TEXT, id="非只读"
        ),
        pytest.param(
            "SELECT CONCAT(a, b) FROM orders",
            DATABASE_SQL_UNSUPPORTED,
            SQL_UNSUPPORTED_TEXT,
            id="不受支持",
        ),
        pytest.param(
            "SELECT id FROM payroll",
            DATABASE_TABLE_NOT_ALLOWED,
            TABLE_NOT_ALLOWED_TEXT,
            id="表未授权",
        ),
        pytest.param(
            "SELECT id FROM orders", DATABASE_LIMIT_EXCEEDED, LIMIT_EXCEEDED_TEXT, id="超上限"
        ),
    ],
)
def test_error_message_is_fixed_text(sql: str, code: str, text: str) -> None:
    error = _refuse(sql, code, max_rows=0 if code == DATABASE_LIMIT_EXCEEDED else MAX_ROWS_DEFAULT)

    assert error.message == text


def test_parse_error_body_does_not_leak_into_error() -> None:
    """解析器异常正文含原始 SQL 片段：既不得进错误文案，也不得进字符串表示。"""
    secret_sql = "SELECT %s FROM secret_table"

    error = _refuse(secret_sql, DATABASE_SQL_UNSUPPORTED)

    assert "secret_table" not in error.message
    assert "secret_table" not in str(error)
    assert "ParseError" not in error.message
    assert "Mod" not in error.message
