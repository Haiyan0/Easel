"""外部只读数据库查询的 SQL 只读策略（T2，规格 §5.1/§5.2/§7.1）。

**只读保证的第 1 层防线（AST 白名单）**：对完整输入做方言解析，**恰好一条语句**，
递归校验全部节点、CTE、子查询与函数，然后由校验通过的 AST **受控重建 SQL** ——
下游执行的是重建结果，而不是模型原文（规格 §5.2 逐字要求）。本模块不建立任何数据库
连接，也不执行任何 SQL；它是纯函数式的校验器。

白名单（默认拒绝，规格 §5.1「无法解析或未覆盖的结构默认拒绝」）：

1. **顶层**只允许一条 ``Select`` / ``Union``；写语句、DDL、管理语句、事务命令与
   ``REPLACE INTO``/``GRANT``/``CALL``/``PREPARE``/``EXECUTE``（sqlglot 把它降级为
   ``Command``）全部落在类型白名单之外。
2. **节点**：树中每个节点都必须命中 :data:`_ALLOWED_NODES`。这条规则同时承担两件事：
   拒绝未登记函数，以及拒绝一切未覆盖结构（``CASE``/``IF``、``DIV``、窗口函数、
   ``CURRENT_DATE``、语句修饰符 ``HIGH_PRIORITY`` 等）。首版按规格 §5.2 的**闭集**
   放行：普通 SELECT、只读非递归 CTE、UNION、受限 JOIN、聚合、排序与过滤所需的节点。
3. **函数**：只允许规格 §5.2 的内置白名单（COUNT/SUM/AVG/MIN/MAX/COALESCE/IFNULL/
   NULLIF/ABS/ROUND/LOWER/UPPER/LENGTH/CHAR_LENGTH/DATE/YEAR/MONTH/DAY/CAST）；
   未登记函数在 sqlglot 里是 ``Anonymous``，一律拒绝。
   ⚠️ ``IFNULL`` 与 ``COALESCE`` 合并为同一 ``Coalesce`` 节点，``LENGTH`` 与
   ``CHAR_LENGTH`` 合并为同一 ``Length`` 节点 —— 白名单按**节点类型**登记，同族纯函数
   因此共享同一格（无副作用，不构成风险）。
   ⚠️ **带 schema 的函数（``my_schema.my_func()``）的 schema 限定在解析期即被丢弃**，
   只剩 ``Anonymous(name='my_func')``；该信息**无法从节点还原**，故「带 schema 的函数
   拒绝」**依赖「未登记函数一律拒绝」这条规则覆盖，而不另做 schema 检查**。
4. **表引用**：每个真实表引用都必须命中 schema/表白名单。CTE 名按**作用域**排除
   （见 :func:`_visible_cte_names`），表别名不参与白名单；``other_db.t``（跨库）与
   ``cat.db.t``（三段名）一律拒绝。**首版不查询视图**：视图在 AST 上与表不可区分，
   白名单是唯一闸门（未登记即拒），describe 接口的报告由 T4/T5 负责。
5. **资源边界**（规格 §7.1）：SQL 文本 ≤ 32 KiB；``max_rows`` 默认 200、硬上限 1000，
   模型不能提高硬上限；最外层施加有效 LIMIT（已有更小值**保留**、更大值**收紧**）；
   拒绝非零 OFFSET（``LIMIT offset,count`` 与 ``LIMIT count OFFSET offset`` 归一后
   由同一条规则覆盖）与非字面量 LIMIT。
   ⚠️ 本函数收到的 ``max_rows`` 是**重建 SQL 的 LIMIT 值**，而 §7.1 的 1000 是「返回给
   模型的行数」上限、并**逐字允许少读一条作截断探针**（最多 ``max_rows + 1``），
   故这里的合法上限是 :data:`MAX_ROWS_LIMIT_WITH_PROBE`（1001）：
   ``1000 + 1`` 这一格是探针专用的，不是把返回行数上限放宽（``mysql._resolve_limits``
   仍按 1000 卡返回行数，``QueryPage.returned_rows`` 也仍以 1000 为界）。
6. **注释与 hint**：词法层面的注释（普通注释、块注释、MySQL 可执行版本注释
   ``/*!…*/``）经 ``expression.comments`` 检出后一律拒绝；优化器 hint 走
   ``exp.Hint`` 节点、**不在 comments 里**，必须单独检测。字符串字面量里的分号、
   关键字与注释符不受影响（按 AST 判定而非文本匹配）。

实测依据：sqlglot 30.18.0（本模块只按该版本已实测的行为编写，见任务简报 §6 九条）。

与下游（T3 ``execute_readonly``）的边界：本模块返回的 :class:`ValidatedQuery` 里
**保留模型原样的占位符节点**（``:name`` 回写仍是 ``:name``；不重建占位符节点，否则
会丢失参数名与绑定关系），参数以不可变映射随行。**把占位符翻译成驱动可绑定的形态
（asyncmy 的 ``%s`` / ``%(name)s``）属于 T3 的执行层职责**；届时须注意 MySQL 驱动在
带参数执行时会解释 SQL 文本里的 ``%``，需把其余 ``%`` 转义为 ``%%``。
"""

import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from types import MappingProxyType

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError

# 与 T1 共用同一套日志取值脱敏（repr 转义 + 截断）：模型可控取值不得把日志灌爆或伪造日志行
from dbquery.errors import DomainError, _log_value

logger = logging.getLogger(__name__)

# 方言固定为 MySQL 8.0（首版唯一支持的数据源类型，规格 §2）
DIALECT = "mysql"

# max_rows 默认值与硬上限（规格 §7.1：默认 200，硬上限 1000，模型不能提高硬上限）
MAX_ROWS_DEFAULT = 200
MAX_ROWS_LIMIT = 1000
# **本模块**的 max_rows 合法上限：多出的那一格是给**截断探针**的。
# §7.1 的 1000 是「返回给模型的行数」上限，不是 LIMIT 子句的上限；而 §7.1 逐字允许
# 「少读一条作截断探针，最多 max_rows + 1」，故调用方按约定传 max_rows + 1 时
# （默认 200 ⇒ 传 201；上限 1000 ⇒ 传 1001）这里不能拒。返回给模型的行数仍以 1000 为界，
# 那一侧在执行层的 ``mysql._resolve_limits``（两处不是同一个量，改一处要看另一处）。
MAX_ROWS_LIMIT_WITH_PROBE = MAX_ROWS_LIMIT + 1
# SQL 文本上限 32 KiB（规格 §7.1）
MAX_SQL_BYTES = 32 * 1024

# 稳定错误码（规格 §8）
DATABASE_SQL_NOT_READONLY = "database.sql_not_readonly"
DATABASE_SQL_UNSUPPORTED = "database.sql_unsupported"
DATABASE_TABLE_NOT_ALLOWED = "database.table_not_allowed"
DATABASE_LIMIT_EXCEEDED = "database.limit_exceeded"

# 对外固定文案：不含 SQL 正文、参数、底层异常或任何配置取值
SQL_NOT_READONLY_TEXT = "仅允许只读查询"
SQL_UNSUPPORTED_TEXT = "SQL 结构不受支持"
TABLE_NOT_ALLOWED_TEXT = "查询引用了未授权的表"
LIMIT_EXCEEDED_TEXT = "查询超出资源上限"


class SqlRefusalReason:
    """内部拒绝原因码（对外只暴露上面四个稳定错误码）。

    与 T1 的 ``RefusalReason`` 同款理由：同一对外错误码下往往有多条原因，
    只给统一错误会让排查无从下手。取值是固定枚举，不含 SQL 正文与参数，
    可直接用于日志检索与告警规则；SQL 正文与参数一律不进日志（规格 §8）。
    """

    # 结构与解析面
    SQL_TOO_LONG = "sql_too_long"
    MAX_ROWS_OUT_OF_RANGE = "max_rows_out_of_range"
    PARSE_ERROR = "parse_error"
    EMPTY_STATEMENT = "empty_statement"
    MULTI_STATEMENT = "multi_statement"
    STATEMENT_NOT_READONLY = "statement_not_readonly"
    UNKNOWN_NODE = "unknown_node"
    UNKNOWN_FUNCTION = "unknown_function"
    # 副作用面
    COMMENT_PRESENT = "comment_present"
    HINT_PRESENT = "hint_present"
    LOCK_PRESENT = "lock_present"
    INTO_CLAUSE = "into_clause"
    USER_VARIABLE = "user_variable"
    # CTE 与表授权面
    RECURSIVE_CTE = "recursive_cte"
    CARTESIAN_JOIN = "cartesian_join"
    CROSS_DATABASE = "cross_database"
    TABLE_NOT_ALLOWED = "table_not_allowed"
    # 参数与 LIMIT 面
    PARAMETER_MISMATCH = "parameter_mismatch"
    OFFSET_PRESENT = "offset_present"
    LIMIT_NOT_LITERAL = "limit_not_literal"


# 顶层**只读**语句类型：写语句/DDL/管理语句/事务命令一律落在这里之外，
# 对外报「仅允许只读查询」。列表里不放行任何写操作。
# ⚠️ sqlglot 把 REPLACE INTO / GRANT / CALL / PREPARE / EXECUTE 降级为 Command，
# 把 DO / LOAD DATA / INTO OUTFILE 直接判为 ParseError —— 两条路径都自然落在白名单外。
# Except/Intersect 同为只读集合运算，故列在这里（否则会被误报成「非只读」），
# 但首版只支持 UNION：它们随后仍会被节点白名单按「结构不受支持」拒绝。
_ALLOWED_TOP_LEVEL: tuple[type[exp.Expr], ...] = (
    exp.Select,
    exp.Union,
    exp.Except,
    exp.Intersect,
)

# 允许出现在树中的节点类型（规格 §5.2 首版的闭集）。
# 按节点类型而非函数名登记：**未登记的节点类型即拒**，因此这里每放宽一格都应先问
# 「它有没有副作用、会不会引入未确定引用对象」。按组登记，新增须同时补正反测试。
_ALLOWED_NODES: frozenset[type[exp.Expr]] = frozenset(
    {
        # 查询结构与子句
        exp.Select,
        exp.Union,
        exp.From,
        exp.Where,
        exp.Group,
        exp.Having,
        exp.Order,
        exp.Ordered,
        exp.Join,
        exp.Limit,
        exp.Offset,
        exp.With,
        exp.CTE,
        exp.Subquery,
        exp.Distinct,
        # 标识符与别名
        exp.Table,
        exp.TableAlias,
        exp.Alias,
        exp.Column,
        exp.Star,
        exp.Identifier,
        # 字面量与占位符
        exp.Literal,
        exp.Null,
        exp.Boolean,
        exp.Placeholder,
        # 比较与布尔运算
        exp.EQ,
        exp.NEQ,
        exp.GT,
        exp.GTE,
        exp.LT,
        exp.LTE,
        exp.And,
        exp.Or,
        exp.Not,
        exp.Is,
        exp.In,
        exp.Between,
        exp.Like,
        exp.Exists,
        exp.Paren,
        # 算术（纯函数，无副作用）
        exp.Add,
        exp.Sub,
        exp.Mul,
        exp.Div,
        exp.Mod,
        exp.Neg,
        # 规格 §5.2 内置函数白名单（IFNULL→Coalesce、CHAR_LENGTH→Length、DATE→TsOrDsToDate）
        exp.Count,
        exp.Sum,
        exp.Avg,
        exp.Min,
        exp.Max,
        exp.Coalesce,
        exp.Nullif,
        exp.Abs,
        exp.Round,
        exp.Lower,
        exp.Upper,
        exp.Length,
        exp.TsOrDsToDate,
        exp.Year,
        exp.Month,
        exp.Day,
        exp.Cast,
        exp.DataType,
    }
)

# 用户变量/赋值相关节点（规格 §5.2：变量赋值与 @var 引用拒绝）。
# ⚠️ exp.Assignment 在 30.18 不存在：用户变量引用是 Parameter（内含 Var），赋值是 PropertyEQ。
# 刻意**不**把 exp.Var 整类算作变量：它同时被语句修饰符（HIGH_PRIORITY/SQL_NO_CACHE）与
# INTERVAL 单位复用，那些属于「未覆盖结构」，交给节点白名单按 sql_unsupported 拒绝。
_USER_VARIABLE_NODES: tuple[type[exp.Expr], ...] = (
    exp.Parameter,
    exp.PropertyEQ,
)


@dataclass(frozen=True, slots=True)
class ValidatedQuery:
    """校验通过后的内部查询对象：受控重建的 SQL + 绑定参数 + 已验证 AST。

    **仅服务端可见**：不得直接回传模型（模型只应看到连接摘要与查询结果）。下游
    ``execute_readonly`` 只接受本类型，从而不可能执行「未经校验的原文」。

    - ``sql``：由已验证 AST 重建的 SQL（含施加后的有效 LIMIT），执行的就是这一份；
      占位符保持模型原样（``:name``），不做重建；
    - ``parameters``：不可变映射，与 SQL 中的具名占位符严格一一对应，**由驱动绑定**，
      任何情况下都不得拼进 SQL 文本；
    - ``expression``：已验证的 AST（有效 LIMIT 在其 ``args["limit"]`` 上）。sqlglot 节点
      本身可变，它是「本对象由哪些结构校验通过」的证据，**不得据此改写 SQL 文本后再执行**
      —— 执行的永远是 ``sql``。

    刻意不携带 ``max_rows`` 或「有效行数」字段：下游若确需（如截断探针）可按
    消费驱动原则再追加，避免现在凭空多一个无人读的字段。
    """

    sql: str
    parameters: Mapping[str, object]
    expression: exp.Expr


def validate_query(
    sql: str,
    parameters: Mapping[str, object],
    allowed_schema: str,
    allowed_tables: Collection[str],
    max_rows: int = MAX_ROWS_DEFAULT,
) -> ValidatedQuery:
    """校验一条只读查询并返回受控重建的结果；任何不满足策略的输入一律拒绝。

    ``allowed_schema`` / ``allowed_tables`` 来自 T1 的 ``ResolvedSource``（配置白名单，
    即权威来源）；``parameters`` 是模型给出的具名参数值，只做键集合校验后原样绑定。
    所有拒绝都以 :class:`DomainError` 抛出，带稳定错误码与固定文案，不回传底层异常、
    SQL 正文或参数取值。
    """
    _require_resource_bounds(sql, max_rows)
    statement = _parse_single_readonly_statement(sql)
    _reject_unsafe_clauses(statement)
    _require_whitelisted_nodes(statement)
    _require_whitelisted_tables(statement, allowed_schema, allowed_tables)
    bound = _bind_parameters(statement, parameters)
    _apply_row_limit(statement, max_rows)
    return ValidatedQuery(
        sql=statement.sql(dialect=DIALECT),
        parameters=bound,
        expression=statement,
    )


def _require_resource_bounds(sql: str, max_rows: object) -> None:
    """解析前先卡资源边界（规格 §7.1）：超长文本不进入解析器，max_rows 不得越界。

    ``max_rows`` 按 ``object`` 收：它最终来自模型给出的工具参数，JSON 里的浮点或
    字符串在这里就要按「超出上限」拒绝，而不是留给下游拼出非法的 ``LIMIT 200.0``。
    文本长度按 UTF-8 字节数判定（「最多 32 KiB」是字节边界）；先比字符数可让超长
    输入在编码分配之前就被拒。

    ``max_rows`` 在这里是**重建 SQL 的最外层 LIMIT 值**，上限因此是
    :data:`MAX_ROWS_LIMIT_WITH_PROBE`（1001）而不是 :data:`MAX_ROWS_LIMIT`（1000）：
    调用方按 §7.1 的截断探针约定传 ``limits.max_rows + 1``，探针那一格本就该留
    （1000 管的是「返回给模型的行数」，由执行层的 ``mysql._resolve_limits`` 把关）。
    """
    if len(sql) > MAX_SQL_BYTES or len(sql.encode("utf-8")) > MAX_SQL_BYTES:
        raise _refuse(SqlRefusalReason.SQL_TOO_LONG, DATABASE_LIMIT_EXCEEDED, LIMIT_EXCEEDED_TEXT)
    # bool 是 int 的子类，但把 True 当 1 行上限没有意义，一并算越界
    if isinstance(max_rows, bool) or not isinstance(max_rows, int):
        raise _refuse(
            SqlRefusalReason.MAX_ROWS_OUT_OF_RANGE,
            DATABASE_LIMIT_EXCEEDED,
            LIMIT_EXCEEDED_TEXT,
        )
    if not 1 <= max_rows <= MAX_ROWS_LIMIT_WITH_PROBE:
        raise _refuse(
            SqlRefusalReason.MAX_ROWS_OUT_OF_RANGE,
            DATABASE_LIMIT_EXCEEDED,
            LIMIT_EXCEEDED_TEXT,
            max_rows=str(max_rows),
        )


def _parse_single_readonly_statement(sql: str) -> exp.Expr:
    """解析为**恰好一条只读语句**，否则拒绝（规格 §5.1：不能只用 startswith 判定）。"""
    try:
        # error_level=RAISE：sqlglot 默认会「告警并降级」，显式钉死为抛错，行为不随
        # 版本默认值漂移（不可解析的结构一律进入拒绝路径，而不是被悄悄简化）
        parsed = sqlglot.parse(sql, read=DIALECT, error_level=ErrorLevel.RAISE)
    except (SqlglotError, RecursionError):
        # ⚠️ 解析器异常正文含**原始 SQL 片段**：只记异常类型，绝不回传、绝不进错误文案
        # （RecursionError 来自超深嵌套输入，按不受支持拒绝而不是抛 500）
        raise _refuse(
            SqlRefusalReason.PARSE_ERROR, DATABASE_SQL_UNSUPPORTED, SQL_UNSUPPORTED_TEXT
        ) from None
    statements: list[exp.Expr] = []
    for statement in parsed:
        if statement is None:  # 空串、纯空白、纯注释
            raise _refuse(
                SqlRefusalReason.EMPTY_STATEMENT,
                DATABASE_SQL_UNSUPPORTED,
                SQL_UNSUPPORTED_TEXT,
            )
        statements.append(statement)
    if not statements:
        raise _refuse(
            SqlRefusalReason.EMPTY_STATEMENT, DATABASE_SQL_UNSUPPORTED, SQL_UNSUPPORTED_TEXT
        )
    non_readonly = [s for s in statements if not isinstance(s, _ALLOWED_TOP_LEVEL)]
    if non_readonly:
        # 写语句/DDL/管理语句/事务命令/Command 回退，以及含写语句的多语句都落到这里
        raise _refuse(
            SqlRefusalReason.STATEMENT_NOT_READONLY,
            DATABASE_SQL_NOT_READONLY,
            SQL_NOT_READONLY_TEXT,
            node=type(non_readonly[0]).__name__,
        )
    if len(statements) != 1:
        # 多条只读语句：不是「非只读」，而是首版不支持多语句（规格 §5.2）
        raise _refuse(
            SqlRefusalReason.MULTI_STATEMENT,
            DATABASE_SQL_UNSUPPORTED,
            SQL_UNSUPPORTED_TEXT,
            count=str(len(statements)),
        )
    return statements[0]


def _reject_unsafe_clauses(statement: exp.Expr) -> None:
    """拒绝注释/hint/锁/INTO/用户变量/递归 CTE/笛卡尔连接（规格 §5.2）。"""
    for node in statement.walk():
        # ⚠️ 注释只能从 expression.comments 检出，不能只看 AST 结构：普通注释、块注释与
        # MySQL 可执行版本注释 /*!…*/ 都落在各节点的 comments 里（tokenizer 层不可见）
        if node.comments:
            raise _refuse(
                SqlRefusalReason.COMMENT_PRESENT, DATABASE_SQL_UNSUPPORTED, SQL_UNSUPPORTED_TEXT
            )
        # ⚠️ 优化器 hint 不在 comments 里（走 exp.Hint 节点 / tokenizer 的 HINT token），
        # 必须单独检测：超时 hint 只能由服务端生成
        if isinstance(node, exp.Hint) or node.args.get("hint") is not None:
            raise _refuse(
                SqlRefusalReason.HINT_PRESENT, DATABASE_SQL_UNSUPPORTED, SQL_UNSUPPORTED_TEXT
            )
        # FOR UPDATE / FOR SHARE / LOCK IN SHARE MODE 统一归到 locks 参数
        if node.args.get("locks"):
            raise _refuse(
                SqlRefusalReason.LOCK_PRESENT, DATABASE_SQL_NOT_READONLY, SQL_NOT_READONLY_TEXT
            )
        # INTO OUTFILE/DUMPFILE 解析期即失败，但 SELECT … INTO @var 会解析成 Into 节点；
        # 其回写形式是 CREATE TABLE … AS SELECT（DDL），必须在重建前拦下
        if node.args.get("into") is not None:
            raise _refuse(
                SqlRefusalReason.INTO_CLAUSE, DATABASE_SQL_NOT_READONLY, SQL_NOT_READONLY_TEXT
            )
        if isinstance(node, _USER_VARIABLE_NODES):
            raise _refuse(
                SqlRefusalReason.USER_VARIABLE, DATABASE_SQL_NOT_READONLY, SQL_NOT_READONLY_TEXT
            )
    # ⚠️ WITH RECURSIVE 的标记在 With 节点的 recursive 键上；exp.CTE.args 里恒为 None，
    # 查它必然漏判。且外层 With 非递归时内层仍可能递归 —— 必须遍历**全部** With 节点。
    for with_clause in statement.find_all(exp.With):
        if with_clause.args.get("recursive"):
            raise _refuse(
                SqlRefusalReason.RECURSIVE_CTE, DATABASE_SQL_UNSUPPORTED, SQL_UNSUPPORTED_TEXT
            )
    # 与关键字无关的兜底：CTE 定义体内自引用即递归（即使没有 RECURSIVE 关键字）
    for cte in statement.find_all(exp.CTE):
        if any(table.name == cte.alias and not table.db for table in cte.this.find_all(exp.Table)):
            raise _refuse(
                SqlRefusalReason.RECURSIVE_CTE, DATABASE_SQL_UNSUPPORTED, SQL_UNSUPPORTED_TEXT
            )
    for join in statement.find_all(exp.Join):
        # 笛卡尔连接：CROSS JOIN，以及逗号隐式连接/无 ON 无 USING 的 JOIN
        # （`FROM a, b WHERE a.x = b.y` 也按不受支持拒绝：连接条件不在 Join 上，
        # 无法在不重写语义的前提下确认引用对象，首版要求改写成显式 JOIN … ON）
        if join.args.get("kind") == "CROSS" or not (join.args.get("on") or join.args.get("using")):
            raise _refuse(
                SqlRefusalReason.CARTESIAN_JOIN, DATABASE_SQL_UNSUPPORTED, SQL_UNSUPPORTED_TEXT
            )


def _require_whitelisted_nodes(statement: exp.Expr) -> None:
    """递归校验全部节点：未登记函数与未覆盖结构默认拒绝（规格 §5.1）。"""
    for node in statement.walk():
        if isinstance(node, exp.Anonymous):
            # 未登记函数（含 SLEEP/BENCHMARK/GET_LOCK/LOAD_FILE 等资源滥用函数，
            # 以及 schema 限定被解析期丢弃的存储函数/UDF）
            raise _refuse(
                SqlRefusalReason.UNKNOWN_FUNCTION,
                DATABASE_SQL_UNSUPPORTED,
                SQL_UNSUPPORTED_TEXT,
                function=node.name,
            )
        if type(node) not in _ALLOWED_NODES:
            raise _refuse(
                SqlRefusalReason.UNKNOWN_NODE,
                DATABASE_SQL_UNSUPPORTED,
                SQL_UNSUPPORTED_TEXT,
                node=type(node).__name__,
            )


def _require_whitelisted_tables(
    statement: exp.Expr, allowed_schema: str, allowed_tables: Collection[str]
) -> None:
    """每个真实表引用都必须命中 schema/表白名单，禁止跨库逃逸（规格 §5.2）。"""
    allowed = frozenset(allowed_tables)
    for table in statement.find_all(exp.Table):
        # ⚠️ 标识符位的**非标识符节点**会让下面的跨库判断静默失效：`table.db` 走
        # sqlglot 的 text()，对 Placeholder 一类节点返回**空串**，于是 `if table.db:`
        # 判假、被当作「未限定 schema」直接落到表白名单分支 —— `SELECT * FROM :d.orders`
        # 因此能整条绕过 schema 检查（table.name 仍取到 'orders' 而放行）。
        # 故表名与 schema/catalog 位一律要求纯 Identifier，否则按「引用了未授权的表」拒绝。
        if not isinstance(table.this, exp.Identifier) or any(
            part is not None and not isinstance(part, exp.Identifier)
            for part in (table.args.get("db"), table.args.get("catalog"))
        ):
            raise _refuse(
                SqlRefusalReason.CROSS_DATABASE,
                DATABASE_TABLE_NOT_ALLOWED,
                TABLE_NOT_ALLOWED_TEXT,
                table=table.name,
            )
        # 三段名（cat.db.t）一律拒绝：首版只允许单库
        if table.catalog:
            raise _refuse(
                SqlRefusalReason.CROSS_DATABASE,
                DATABASE_TABLE_NOT_ALLOWED,
                TABLE_NOT_ALLOWED_TEXT,
                table=table.name,
                catalog=table.catalog,
            )
        if table.db:
            if table.db != allowed_schema:
                raise _refuse(
                    SqlRefusalReason.CROSS_DATABASE,
                    DATABASE_TABLE_NOT_ALLOWED,
                    TABLE_NOT_ALLOWED_TEXT,
                    table=table.name,
                    db=table.db,
                )
        elif table.name in _visible_cte_names(table):
            # ⚠️ CTE 名与真实表混在同一个 find_all(exp.Table) 结果里，先按作用域排除，
            # 否则 CTE 名会被当真实表查白名单（同名误放、异名误拒）
            continue
        # 未限定 schema 的表就是当前连接库（T1 已强制连接串库名 == allowed_schema）；
        # 白名单与 schema 均按**精确匹配**（大小写敏感），与 T1 的 URL schema 校验同款：
        # 大小写变体在大小写敏感的服务器上可能指向另一个对象，不做宽松匹配
        if table.name not in allowed:
            raise _refuse(
                SqlRefusalReason.TABLE_NOT_ALLOWED,
                DATABASE_TABLE_NOT_ALLOWED,
                TABLE_NOT_ALLOWED_TEXT,
                table=table.name,
            )


def _visible_cte_names(table: exp.Table) -> set[str]:
    """收集在 ``table`` 处**可见**的 CTE 名（作用域感知）。

    向上遍历父链，每个 ``With`` 子句贡献它的 CTE 名。若当前子节点正是该子句中的某个
    CTE，则只有**排在它之前**的 CTE 可见：CTE 不允许前向引用，否则
    ``WITH a AS (SELECT * FROM t), t AS (SELECT 1) …`` 里的 ``t``（真实表）会被误判成
    CTE 而跳过白名单校验 —— 那等于给白名单开了后门。
    """
    # 先取出该表到根的整条祖先链（含自身）：``with_`` 挂在 Select/Union 上，
    # 而 CTE 定义体是挂在 With 节点下的，故必须整链判定「表落在哪个 CTE 里」
    chain: list[exp.Expr] = []
    node: exp.Expr | None = table
    while node is not None:
        chain.append(node)
        node = node.parent
    names: set[str] = set()
    for ancestor in chain:
        with_clause = ancestor.args.get("with_")
        if not isinstance(with_clause, exp.With):
            continue
        ctes = with_clause.expressions
        # 链上至多落在一个 CTE 里（链是线性的，用身份比较避免结构相等的误判）
        index = next((i for i, cte in enumerate(ctes) if any(cte is item for item in chain)), None)
        # 落在第 i 个 CTE 里 ⇒ 只有它之前定义的 CTE 可见；不在任何 CTE 里 ⇒ 全部可见
        names.update(cte.alias for cte in (ctes if index is None else ctes[:index]))
    return names


def _bind_parameters(statement: exp.Expr, parameters: object) -> Mapping[str, object]:
    """校验占位符与参数的对应关系，返回不可变绑定映射（禁止拼值，规格 §5.2）。

    首版只支持**具名占位符** ``:name``：sqlglot 回写时会原样保留名字，参数按键取值。
    匿名占位符（``?``）与驱动格式（``%s``，sqlglot 直接判为 ParseError）都不支持：
    「未使用/缺少」的参数一律拒绝，避免模型以为生效却静默忽略。
    """
    if not isinstance(parameters, Mapping):
        raise _refuse(
            SqlRefusalReason.PARAMETER_MISMATCH, DATABASE_SQL_UNSUPPORTED, SQL_UNSUPPORTED_TEXT
        )
    names: set[str] = set()
    for placeholder in statement.find_all(exp.Placeholder):
        # ⚠️ 具名占位符的名字在 ``this`` 上（是字符串，不是节点）；匿名占位符 this 为
        # None 且 name 会给出 '?'，必须按 this 判定。回写保留 :name，故不重建占位符节点
        name = placeholder.this
        if not isinstance(name, str) or not name:
            raise _refuse(
                SqlRefusalReason.PARAMETER_MISMATCH,
                DATABASE_SQL_UNSUPPORTED,
                SQL_UNSUPPORTED_TEXT,
            )
        names.add(name)
    supplied = set(parameters)
    if names != supplied:
        raise _refuse(
            SqlRefusalReason.PARAMETER_MISMATCH,
            DATABASE_SQL_UNSUPPORTED,
            SQL_UNSUPPORTED_TEXT,
            missing=",".join(sorted(names - supplied)),
            unused=",".join(sorted(supplied - names)),
        )
    # 复制成不可变映射：调用方后续改动入参不得影响已校验结果
    return MappingProxyType(dict(parameters))


def _apply_row_limit(statement: exp.Expr, max_rows: int) -> None:
    """约束**每一层**行窗口：最外层施加有效 LIMIT，其余行窗口只允许受限形态（规格 §7.1）。

    ⚠️ 必须按**操作符节点**枚举，不能按「承载它的查询节点」枚举：``LIMIT``/``OFFSET``
    可以挂在 ``Select``/``Union`` 上，也可以挂在 ``Subquery`` 上——加一对括号就换承载者，
    实测 ``SELECT * FROM ((SELECT id FROM orders) LIMIT 10 OFFSET 900000) t`` 里行窗口挂在
    ``Subquery`` 上、``SELECT … WHERE EXISTS ((SELECT …) LIMIT 1000000)`` 里直接挂在
    ``Exists`` 下（``Subquery.args["limit"]`` 还是 None）。按承载者枚举必然漏面，
    故这里枚举 ``exp.Limit``/``exp.Offset`` 本身，用**身份比较**排除最外层那两个：
    树里其余行窗口一律按内层规则检查，与它挂在哪里无关。

    最外层的 ``LIMIT max_rows`` 只限制**单次返回行数**、不限制内层的偏移深度，而 §7.1
    拒绝非零 OFFSET 的目的从句是「避免给大表提供深分页通道」——它约束的是**能力**。
    （``LIMIT 900000, 10`` 会被 sqlglot 归一成 ``LIMIT 10 OFFSET 900000``，故同一条规则
    天然覆盖两种写法。）
    """
    outer_limit = statement.args.get("limit")
    outer_offset = statement.args.get("offset")
    for offset in statement.find_all(exp.Offset):
        if offset is not outer_offset:
            _require_zero_inner_offset(offset)
    for limit in statement.find_all(exp.Limit):
        if limit is not outer_limit:
            _require_bounded_inner_limit(limit, max_rows)
    _apply_outer_row_limit(statement, max_rows)


def _require_zero_inner_offset(offset: exp.Offset) -> None:
    """非最外层偏移必须是字面量 0：非零（含占位符等无法判定者）一律拒绝。"""
    if _literal_int(offset.expression) != 0:
        raise _refuse(
            SqlRefusalReason.OFFSET_PRESENT, DATABASE_SQL_UNSUPPORTED, SQL_UNSUPPORTED_TEXT
        )


def _require_bounded_inner_limit(limit: exp.Limit, max_rows: int) -> None:
    """非最外层 LIMIT 必须是字面量且 ≤ max_rows。

    内层**只拒不改**：改写内层 LIMIT 会改变查询语义（先截断再聚合/连接，结果不同），
    属于 §7.1「拒绝不能安全重写的结构」。全参数化的内层行窗口同样拒绝——SQL 文本可以
    固定不变，翻页深度全靠绑定值，与写死深偏移等价。
    """
    value = _literal_int(limit.expression)
    if value is None:
        raise _refuse(
            SqlRefusalReason.LIMIT_NOT_LITERAL,
            DATABASE_SQL_UNSUPPORTED,
            SQL_UNSUPPORTED_TEXT,
            node=type(limit.expression).__name__,
        )
    if value > max_rows:
        raise _refuse(
            SqlRefusalReason.LIMIT_NOT_LITERAL,
            DATABASE_SQL_UNSUPPORTED,
            SQL_UNSUPPORTED_TEXT,
            limit=str(value),
        )


def _apply_outer_row_limit(statement: exp.Expr, max_rows: int) -> None:
    """最外层：无 LIMIT 则补 max_rows，已有更小 LIMIT **保留**、更大值**收紧**。

    ``LIMIT 5,10`` 与 ``LIMIT 10 OFFSET 5`` 被 sqlglot 归一成同一结构
    （``limit=Limit(10)`` / ``offset=Offset(5)``），故一条规则覆盖两种写法。
    """
    offset = statement.args.get("offset")
    if offset is not None and _literal_int(offset.expression) != 0:
        # 非零偏移（含无法判定的非字面量偏移）一律拒绝：不给大表提供深分页通道
        raise _refuse(
            SqlRefusalReason.OFFSET_PRESENT, DATABASE_SQL_UNSUPPORTED, SQL_UNSUPPORTED_TEXT
        )
    limit = statement.args.get("limit")
    if limit is None:
        statement.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        return
    value = _literal_int(limit.expression)
    if value is None:
        raise _refuse(
            SqlRefusalReason.LIMIT_NOT_LITERAL, DATABASE_SQL_UNSUPPORTED, SQL_UNSUPPORTED_TEXT
        )
    if value > max_rows:
        limit.set("expression", exp.Literal.number(max_rows))


def _literal_int(node: exp.Expr | None) -> int | None:
    """取字面量整数值；非字面量（占位符、表达式）返回 None。"""
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return int(node.this)
        except (TypeError, ValueError):
            return None
    return None


def _refuse(reason: str, code: str, message: str, **context: str) -> DomainError:
    """构造拒绝错误并写服务端日志：固定文案对外，稳定原因码与上下文只进日志。

    上下文只允许放节点类型、表名、函数名等服务端标识（经 ``_log_value`` 转义截断）；
    **SQL 正文与参数取值一律不入日志**（规格 §8）。
    """
    rendered = " ".join(f"{key}={_log_value(value)}" for key, value in context.items())
    logger.warning("受控数据库查询拒绝执行：reason=%s %s", reason, rendered)
    return DomainError(code, message, status=400)
