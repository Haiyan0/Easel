"""外部只读查询的执行层：连接生命周期、只读防线与有界读取（T3，规格 §4–§8）。

**只读保证的第 2、3 层防线 + 连接生命周期**：

- 第 2 层（数据库只读事务，§5.1）：连接建立后由 :func:`_apply_session_guards`
  执行**固定内部语句**建立只读事务并设置服务端 SELECT 超时，且**读回校验**实际生效；
  设置失败**直接退出，不降级普通事务**。内部语句是模块级常量，控制函数**不接受任何
  模型文本**（§5.1：内部命令不属于模型可执行 SQL）。
- 第 3 层（最小权限账户，§5.1）：部署侧口径，由集成测试用受限账号证明「账户本身
  拒绝写」——只读事务有例外，不能取代最小权限账户。
- 生命周期（§6）：**每次调用独立小池且返回前销毁**（``pool_size=1, max_overflow=0``），
  顺序为 建池/连接 → 只读与超时设置 → 必要元数据检查 → 有界读取 → 关闭结果 → 回滚 →
  关闭连接 → ``await engine.dispose()``。所有阶段失败都释放已创建资源，**清理失败不
  跳过后续清理**；取消路径用受保护且有上限（``CLEANUP_BUDGET_SECONDS``）的清理任务并
  **实际等待**，清理完成后才重新抛出取消异常。

**超时（§6）**：建连 5 秒（驱动 ``connect_timeout`` + 显式预算）、单次服务器 SELECT
10 秒（``SET SESSION max_execution_time`` 由服务端真正中断查询，客户端预算同时中止并
销毁驱动连接）、整个调用 15 秒、清理额外 3 秒。Python 3.10 兼容：只用 ``asyncio.wait_for``。

**驱动多语句能力（§3.1 逐字「必须明确关闭并测试」）**：asyncmy 在 ``connect()`` 里
**无条件** ``client_flag |= MULTI_STATEMENTS``，没有任何开关能关掉（实测 0.2.14：
``SELECT 1; SELECT 2`` 会被服务端执行两条）。因此本模块用子类
:class:`_NoMultiStatementsConnection` 在握手前把该能力位摘掉，并经
``connect_args["async_creator_fn"]`` 接进 SQLAlchemy —— 即使 AST 层被绕过，第二名语句
也不可能到达服务端（集成测试用对照连接证明这不是空转）。

**占位符与 ``%``（T2 交接）**：``ValidatedQuery.sql`` 原样保留 ``:name``。执行入口是
``AsyncConnection.stream(_bound_statement(...), params)``：由 dialect 的 text() 编译层完成
``:name`` → ``%s`` 的翻译，并把 SQL 文本中其余 ``%``（如 ``LIKE '50%'``）转义为
``%%``。手工翻译路线（``exec_driver_sql`` + 自拼 pyformat）**无法流式**（SQLAlchemy
直接抛 ``AsyncMethodRequired``，实测），而流式读取是 §7.1 的逐字要求，故模型 SQL 只走
``stream``；集成测试钉住「绑定参数含 %」「SQL 里字面 %」「字符串里的 :foo」三条。

⚠️ **``text()`` 是按正则找占位符的，它不认识 SQL 文法**：字面量里的 ``:name``（冒号前
是非单词字符时）也会被它当成绑定位，而 T2 的 sqlglot AST 认为那是字面量的一部分 ——
两侧判断不一致会让**字面量被静默改写**（``':floor'`` 变成绑定值 ``1``，比报错更糟：
模型会把改写后的值当真结果汇报）。故模型 SQL 进 ``text()`` 前一律先过
:func:`_bound_statement`：把「作为数据」的冒号按 ``text()`` 自己的约定转义，并与已验证
AST 的占位符**对账**，对不齐即拒（fail-closed）。

**并发额度（§6 顺序中的「获取/释放并发额度」）**：按账本 Ruling 2，本模块**不引入**
``budget.py``（T4 的 Redis 跨 Worker 租约）依赖，该步在编排层由 T4 插入；本模块不提供
任何绕过额度的入口。

秘密与错误（§8）：``echo=False``、``hide_parameters=True``；对外只给稳定错误码与固定
文案，底层异常、URL、口令、完整绑定参数与堆栈都不回传模型，内部原因码（
:class:`ExecutionRefusalReason`）只进服务端日志与异常对象属性。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from datetime import time as time_of_day
from decimal import Decimal
from typing import Any

import asyncmy  # asyncmy 自带 .pyi 类型标注（mypy strict 下无需 ignore）
import sqlglot
from asyncmy.constants.CLIENT import MULTI_STATEMENTS
from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncResult,
    create_async_engine,
)
from sqlglot import TokenType, exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import Tokenizer

from dbquery.errors import DomainError, _log_value
from dbquery.policy import (
    DATABASE_LIMIT_EXCEEDED,
    DATABASE_SQL_UNSUPPORTED,
    DATABASE_TABLE_NOT_ALLOWED,
    DIALECT,
    LIMIT_EXCEEDED_TEXT,
    MAX_ROWS_DEFAULT,
    MAX_ROWS_LIMIT,
    SQL_UNSUPPORTED_TEXT,
    TABLE_NOT_ALLOWED_TEXT,
    ValidatedQuery,
)
from dbquery.types import (
    ColumnDescription,
    IndexDescription,
    QueryPage,
    ResolvedSource,
    TableDescription,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 稳定错误码与对外固定文案（规格 §8）
# ---------------------------------------------------------------------------

DATABASE_TIMEOUT = "database.timeout"
DATABASE_CONNECTION_FAILED = "database.connection_failed"
DATABASE_QUERY_FAILED = "database.query_failed"

TIMEOUT_TEXT = "查询超时"
CONNECTION_FAILED_TEXT = "数据库连接失败"
QUERY_FAILED_TEXT = "查询执行失败"
# 视图与「未授权/不存在」共用同一个对外码；文案不同是为了让模型知道「别再试了」
# （§5.2：首版不查询视图，describe 接口明确报告不支持），两者都不含任何内部信息
VIEW_UNSUPPORTED_TEXT = "描述接口首版不支持视图，仅支持基表"

# ---------------------------------------------------------------------------
# 时限（规格 §6 逐字数值）与输出边界（规格 §7.1 逐字数值）
# ---------------------------------------------------------------------------

CONNECT_BUDGET_SECONDS = 5.0
SELECT_BUDGET_SECONDS = 10.0
TOTAL_BUDGET_SECONDS = 15.0
CLEANUP_BUDGET_SECONDS = 3.0
SELECT_BUDGET_MS = int(SELECT_BUDGET_SECONDS * 1000)

MAX_OUTPUT_BYTES = 256 * 1024
MAX_FIELD_BYTES = 16 * 1024
CHUNK_ROWS = 100
# JSON 安全整数上限（2**53 - 1）：超出即失去精度，按字符串回传以保留精度（§4）
JSON_SAFE_INT_MAX = 2**53 - 1

# 截断原因（QueryPage.truncation_reason 的稳定取值）。
# 同时命中多条边界时按「行上限 > 总字节 > 单字段 > 二进制」报告最外层原因：
# 行上限意味着整页不完整，是模型最需要知道的那条。
TRUNCATION_ROW_LIMIT = "row_limit"
TRUNCATION_OUTPUT_BYTES = "output_bytes"
TRUNCATION_FIELD_BYTES = "field_bytes"
TRUNCATION_BINARY_FIELD = "binary_field"

# ---------------------------------------------------------------------------
# 固定内部语句（§5.1：不属于模型可执行 SQL；控制函数不接受任何模型文本）
# ---------------------------------------------------------------------------

SQL_SET_READ_ONLY = "SET SESSION TRANSACTION READ ONLY"
SQL_READ_READ_ONLY = "SELECT @@session.transaction_read_only"
SQL_START_READ_ONLY_TRANSACTION = "START TRANSACTION READ ONLY"
SQL_SET_SELECT_TIMEOUT = f"SET SESSION max_execution_time = {SELECT_BUDGET_MS}"
SQL_READ_SELECT_TIMEOUT = "SELECT @@session.max_execution_time"
SQL_READ_SCHEMA = "SELECT DATABASE()"

# describe_table 的固定元数据查询：库名与表名一律**绑定参数**，不拼接文本
SQL_DESCRIBE_TABLE_TYPE = (
    "SELECT TABLE_TYPE FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA = %(schema)s AND TABLE_NAME = %(table)s"
)
SQL_DESCRIBE_COLUMNS = (
    "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_DEFAULT, EXTRA "
    "FROM information_schema.COLUMNS "
    "WHERE TABLE_SCHEMA = %(schema)s AND TABLE_NAME = %(table)s "
    "ORDER BY ORDINAL_POSITION"
)
SQL_DESCRIBE_INDEXES = (
    "SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME FROM information_schema.STATISTICS "
    "WHERE TABLE_SCHEMA = %(schema)s AND TABLE_NAME = %(table)s "
    "ORDER BY INDEX_NAME, SEQ_IN_INDEX"
)
TABLE_TYPE_BASE_TABLE = "BASE TABLE"
TABLE_TYPE_VIEW = "VIEW"

# 适合分页的整数主键类型（规格 §7.2 的扫描门槛）
_INTEGER_TYPES = frozenset({"tinyint", "smallint", "mediumint", "int", "integer", "bigint"})

# 服务端错误码：只用来区分「超时中断」与「其它执行失败」（对外都是稳定码）
_ER_QUERY_INTERRUPTED = 1317
_ER_MAX_EXECUTION_TIME_EXCEEDED = 3024
_TIMEOUT_ERRNOS = frozenset({_ER_QUERY_INTERRUPTED, _ER_MAX_EXECUTION_TIME_EXCEEDED})


@dataclass(frozen=True, slots=True)
class QueryLimits:
    """一次查询的资源边界；默认值即规格 §7.1 的逐字默认，调用方**不能**提高硬上限。

    放在本模块（而不是 ``types.py``）的理由：它只被执行层的 ``execute_readonly`` /
    ``describe_table`` 消费，且默认值来自 ``policy`` 的常量——放进 ``types.py`` 会让
    类型层反向依赖策略层（``policy`` 已经导入 ``types``，会形成导入环）。
    """

    max_rows: int = MAX_ROWS_DEFAULT
    max_bytes: int = MAX_OUTPUT_BYTES
    max_field_bytes: int = MAX_FIELD_BYTES
    chunk_rows: int = CHUNK_ROWS


class ExecutionRefusalReason:
    """内部拒绝原因码（对外只暴露 §8 的稳定错误码）。

    与 T1/T2 同款理由：同一对外码下有多条原因（超时是客户端预算用尽还是服务端中断？
    建连失败是拒绝连接还是握手失败？），只给统一错误会让排查无从下手。取值是固定枚举，
    不含 SQL 正文、参数取值与连接信息，可直接用于日志检索与告警规则。
    """

    CONNECT_FAILED = "connect_failed"
    CLIENT_DEADLINE = "client_deadline"
    SERVER_TIMEOUT = "server_timeout"
    STATEMENT_FAILED = "statement_failed"
    SERIALIZATION_FAILED = "serialization_failed"
    SESSION_GUARD_FAILED = "session_guard_failed"
    SCHEMA_MISMATCH = "schema_mismatch"
    LIMITS_INVALID = "limits_invalid"
    CLEANUP_INCOMPLETE = "cleanup_incomplete"
    CLEANUP_STEP_FAILED = "cleanup_step_failed"
    TABLE_NOT_ALLOWED = "table_not_allowed"
    VIEW_NOT_SUPPORTED = "view_not_supported"
    PLACEHOLDER_MISMATCH = "placeholder_mismatch"


class QueryError(DomainError):
    """执行层稳定错误（规格 §8）。

    对外与普通 ``DomainError`` 完全同形（同 code/message/status，错误处理器只读这三个
    属性）；``reason`` 与 ``context`` 是**仅服务端可见**的内部记录（同时进服务端日志），
    用于排查与测试断言，绝不进任何 API 响应，也不含 SQL 正文、参数取值或连接串。
    """

    def __init__(
        self,
        code: str,
        message: str,
        reason: str,
        *,
        context: Mapping[str, object] | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(code, message, status)
        self.reason = reason
        self.context: Mapping[str, object] = dict(context or {})


# 需要「销毁连接而不是归还池」的原因码：语句被中断，连接状态不确定（§6）
_ABNORMAL_REASONS = frozenset(
    {ExecutionRefusalReason.CLIENT_DEADLINE, ExecutionRefusalReason.SERVER_TIMEOUT}
)


def _refuse(code: str, message: str, reason: str, **context: object) -> QueryError:
    """构造稳定错误并写服务端日志：上下文取值经 ``_log_value`` 转义截断。"""
    error = QueryError(code, message, reason, context=context)
    rendered = " ".join(
        f"{key}={_log_value(str(value))}" for key, value in {"reason": reason, **context}.items()
    )
    logger.warning("外部只读查询失败：code=%s %s", code, rendered)
    return error


# ---------------------------------------------------------------------------
# 驱动多语句能力关闭（§3.1）
# ---------------------------------------------------------------------------


def _clear_multi_statements(connection: Any) -> None:
    """摘掉驱动多语句能力位（``_client_flag`` 是 asyncmy 的私有握手标志）。

    asyncmy 的公开类型标注里没有这个属性，但它在运行期确实存在且驱动正是用它拼握手包
    （0.2.14 实测：改它之后多语句被服务端以 1064 拒绝）；这里用 ``Any`` 承接，避免在
    调用点撒 ``type: ignore``。
    """
    connection._client_flag &= ~MULTI_STATEMENTS


class _NoMultiStatementsConnection(asyncmy.Connection):
    """握手前摘掉 ``CLIENT_MULTI_STATEMENTS`` 能力位的连接。

    asyncmy（0.2.14 实测）在 ``Connection.__init__`` 里无条件 ``client_flag |=
    MULTI_STATEMENTS``，公开参数里没有任何开关可关；而「驱动多语句能力必须明确关闭并
    测试」是 §3.1 的逐字安全要求。摘位发生在构造之后、握手之前，且 :meth:`connect`
    再摘一次，构造到握手之间没有窗口能让该位复活。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[no-untyped-call]  # Cython 实现无标注
        _clear_multi_statements(self)

    async def connect(self) -> Any:
        # 返回类型放宽为 Any：asyncmy 把 connect() 标注成上下文管理器，运行期是可 await 的
        _clear_multi_statements(self)
        return await super().connect()


async def _connect_without_multi_statements(*args: Any, **kwargs: Any) -> Any:
    """SQLAlchemy asyncmy dialect 的 ``async_creator_fn``：建连并关闭驱动多语句能力。"""
    connection = _NoMultiStatementsConnection(*args, **kwargs)
    await connection.connect()
    return connection


def _create_engine(source: ResolvedSource) -> AsyncEngine:
    """按调用新建独立异步 engine（§6：``pool_size=1, max_overflow=0``，返回前销毁）。

    连接参数全部由受信配置与模块常量决定：模型既不能选驱动，也不能加 URL 参数
    （§3.1）。本函数是单元测试注入假 engine 的测试缝（``monkeypatch`` 替换）。
    """
    return create_async_engine(
        source.url.get_secret_value(),
        pool_size=1,
        max_overflow=0,
        echo=False,
        hide_parameters=True,
        connect_args={
            # 驱动多语句能力显式关闭（§3.1）：自定义建连函数，不依赖 URL 参数
            "async_creator_fn": _connect_without_multi_statements,
            # 建连上限 5 秒：驱动层 socket 超时 + 上层显式预算双保险
            "connect_timeout": CONNECT_BUDGET_SECONDS,
            # socket 读超时兜底 = 整个调用上限（真正的单次 SELECT 预算由服务端
            # max_execution_time 与客户端 wait_for 共同施加）
            "read_timeout": TOTAL_BUDGET_SECONDS,
            # 事务语义显式化：自动提交关闭，只读事务由固定内部语句建立
            "autocommit": False,
        },
    )


# ---------------------------------------------------------------------------
# 资源栈与清理（§6：「清理失败不能跳过后续资源清理」）
# ---------------------------------------------------------------------------


class _CallResources:
    """一次调用的资源栈：按创建顺序登记，逆序清理。

    ⚠️ **``result`` 的登记时机**：它只在 ``await connection.stream(...)`` **返回之后**才被登记，
    而真实驱动下该 await 要等服务端发出**首个结果包**才返回（审查探针实测：重查询在首包前
    一直停在 ``stream`` 内，直到客户端预算触发）。因此「查询在首包前被超时/取消中断」这一形态下
    ``result`` **从未登记** ⇒ :meth:`close_result` 在这条路径上是**不可达**的（不是「刻意跳过」：
    关闭结果本来的意图就是 drain，只是这里没有可关闭的对象）。该形态的物理关闭由
    :meth:`release_connection` 的 ``invalidate()`` 与随后的 ``engine.dispose()`` 保证。
    """

    def __init__(self) -> None:
        self.engine: AsyncEngine | None = None
        self.connection: AsyncConnection | None = None
        self.result: AsyncResult[Any] | None = None
        # 超时/取消：连接停在被中断的语句上，不可复用 ⇒ 清理时 invalidate 而不是归还池
        self.abnormal = False

    async def close_result(self) -> None:
        # 首包前被中断时 self.result 为 None：本步不可达（见类 docstring），不是空转设计
        if self.result is not None:
            await self.result.close()
            self.result = None

    async def rollback(self) -> None:
        if self.connection is not None:
            await self.connection.rollback()

    async def release_connection(self) -> None:
        if self.connection is None:
            return
        if self.abnormal:
            # 池化 close() 只是归还连接，不代表物理断开（§6）；异常路径直接销毁
            await self.connection.invalidate()
        else:
            await self.connection.close()
        self.connection = None

    async def dispose_engine(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None


async def _release(resources: _CallResources) -> bool:
    """按 §6 顺序释放资源，返回是否在清理预算内完成。

    清理放在**受保护且有上限**的任务里并实际等待（§6：不能只创建后台任务就宣称已
    关闭）。预算耗尽时不再等待，但**如实记录** ``cleanup_incomplete`` —— 不谎称已关闭。
    """
    task = asyncio.ensure_future(_release_steps(resources))
    try:
        await asyncio.wait_for(asyncio.shield(task), CLEANUP_BUDGET_SECONDS)
        return True
    except asyncio.TimeoutError:
        _refuse(
            DATABASE_QUERY_FAILED,
            QUERY_FAILED_TEXT,
            ExecutionRefusalReason.CLEANUP_INCOMPLETE,
            stage="cleanup_budget",
        )
        return False
    except asyncio.CancelledError:
        # 清理期间再次被取消：shield 保护下的清理任务继续跑，这里不再等（无法安全等待）
        _refuse(
            DATABASE_QUERY_FAILED,
            QUERY_FAILED_TEXT,
            ExecutionRefusalReason.CLEANUP_INCOMPLETE,
            stage="cleanup_cancelled",
        )
        raise


async def _release_steps(resources: _CallResources) -> None:
    """逐步释放：每步独立兜底，任何一步失败（含被取消）都不跳过后续步骤。"""
    for step_name, step in (
        ("close_result", resources.close_result),
        ("rollback", resources.rollback),
        ("release_connection", resources.release_connection),
        ("dispose_engine", resources.dispose_engine),
    ):
        try:
            await step()
        except BaseException as exc:  # noqa: BLE001 —— 清理步骤的失败一律不得中断清理
            logger.warning(
                "外部只读查询清理步骤失败：step=%s error=%s",
                step_name,
                type(exc).__name__,
            )


# ---------------------------------------------------------------------------
# 对外入口：execute_readonly
# ---------------------------------------------------------------------------


async def execute_readonly(
    source: ResolvedSource,
    validated_query: ValidatedQuery,
    limits: QueryLimits | None = None,
) -> QueryPage:
    """执行一条**已经过策略层校验**的只读查询，返回统一结果形状（规格 §4/§6）。

    只接受 :class:`ValidatedQuery`：调用方不可能在这里塞入未校验的原文。调用期间新建
    独立小池、建立只读事务、设置服务端超时、分块有界读取，返回前把连接与池全部销毁
    （见模块 docstring 的顺序与时限）。

    ★ 截断探针：调用方须按 ``limits.max_rows + 1`` 调 ``validate_query``（约定详见
    :class:`~smart_assistant.database.types.QueryPage` 的 docstring）。
    """
    effective_limits = _resolve_limits(limits)
    started = time.monotonic()
    resources = _CallResources()
    failure: BaseException | None = None
    page: QueryPage | None = None
    try:
        page = await asyncio.wait_for(
            _run_query(resources, source, validated_query, effective_limits),
            TOTAL_BUDGET_SECONDS,
        )
    except BaseException as exc:  # noqa: BLE001 —— 失败一律先清理再决定如何外泄
        failure = exc
    _mark_abnormal(resources, failure)
    await _release(resources)
    if failure is not None:
        raise _as_domain_error(failure) from None
    assert page is not None  # 无异常必有结果
    return replace(page, elapsed_ms=_elapsed_ms(started))


async def _run_query(
    resources: _CallResources,
    source: ResolvedSource,
    validated_query: ValidatedQuery,
    limits: QueryLimits,
) -> QueryPage:
    connection = await _connect(resources, source, limits)
    try:
        await _apply_session_guards(connection)
    except QueryError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise _refuse(
            DATABASE_QUERY_FAILED,
            QUERY_FAILED_TEXT,
            ExecutionRefusalReason.SESSION_GUARD_FAILED,
            stage="session_guard",
            error=type(exc).__name__,
        ) from None
    await _require_current_schema(connection, source)
    try:
        return await asyncio.wait_for(
            _read_page(resources, connection, validated_query, limits),
            SELECT_BUDGET_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except QueryError:
        raise
    except asyncio.TimeoutError:
        raise _refuse(
            DATABASE_TIMEOUT,
            TIMEOUT_TEXT,
            ExecutionRefusalReason.CLIENT_DEADLINE,
            stage="select_budget",
        ) from None
    except Exception as exc:
        raise _translate_statement_failure(exc) from None


async def _connect(
    resources: _CallResources, source: ResolvedSource, limits: QueryLimits
) -> AsyncConnection:
    """建池并借出连接：建连上限 5 秒，失败统一 ``database.connection_failed``。"""
    resources.engine = _create_engine(source)
    try:
        connection = await asyncio.wait_for(resources.engine.connect(), CONNECT_BUDGET_SECONDS)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        raise _refuse(
            DATABASE_CONNECTION_FAILED,
            CONNECTION_FAILED_TEXT,
            ExecutionRefusalReason.CLIENT_DEADLINE,
            stage="connect",
        ) from None
    except Exception as exc:
        raise _refuse(
            DATABASE_CONNECTION_FAILED,
            CONNECTION_FAILED_TEXT,
            ExecutionRefusalReason.CONNECT_FAILED,
            stage="connect",
            error=type(exc).__name__,
        ) from None
    resources.connection = connection
    return connection


async def _apply_session_guards(connection: AsyncConnection) -> None:
    """只读事务与服务端 SELECT 超时（§5.1 第 2 层 / §6）。

    全部是**固定内部语句**，本函数不接受任何模型文本；设置后**读回校验**，任何一项
    没有真正生效都直接退出（不降级普通事务、不带着不确定的超时继续读）。
    """
    await connection.exec_driver_sql(SQL_SET_READ_ONLY)
    read_only = (await connection.exec_driver_sql(SQL_READ_READ_ONLY)).scalar()
    if read_only != 1:
        raise _refuse(
            DATABASE_QUERY_FAILED,
            QUERY_FAILED_TEXT,
            ExecutionRefusalReason.SESSION_GUARD_FAILED,
            guard="read_only",
            value=read_only,
        )
    # 当前事务也显式只读：会话缺省只保证「后续」事务，显式形式让本次查询所在事务
    # 一定只读（不依赖驱动在连接期是否已经开过事务）
    await connection.exec_driver_sql(SQL_START_READ_ONLY_TRANSACTION)
    await connection.exec_driver_sql(SQL_SET_SELECT_TIMEOUT)
    budget = (await connection.exec_driver_sql(SQL_READ_SELECT_TIMEOUT)).scalar()
    if budget != SELECT_BUDGET_MS:
        raise _refuse(
            DATABASE_QUERY_FAILED,
            QUERY_FAILED_TEXT,
            ExecutionRefusalReason.SESSION_GUARD_FAILED,
            guard="select_timeout",
            value=budget,
        )


async def _require_current_schema(connection: AsyncConnection, source: ResolvedSource) -> None:
    """必要元数据检查：连接目标库必须与白名单 schema 一致（错位即 fail-closed）。

    URL 的库名由 T1 规定必须与 ``allowed_schema`` 相同；这里再对**真实连接**核一次，
    防止配置漂移让「表级白名单」落在另一个库上。
    """
    try:
        current = (await connection.exec_driver_sql(SQL_READ_SCHEMA)).scalar()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise _refuse(
            DATABASE_QUERY_FAILED,
            QUERY_FAILED_TEXT,
            ExecutionRefusalReason.STATEMENT_FAILED,
            stage="schema_check",
            error=type(exc).__name__,
        ) from None
    if current != source.allowed_schema:
        raise _refuse(
            DATABASE_QUERY_FAILED,
            QUERY_FAILED_TEXT,
            ExecutionRefusalReason.SCHEMA_MISMATCH,
            stage="schema_check",
            connection_id=source.connection_id,
        )


# ---------------------------------------------------------------------------
# 占位符判据：SQLAlchemy 的文本正则 vs T2 的 AST（I-1）
# ---------------------------------------------------------------------------

# SQLAlchemy ``text()`` 用来找占位符的正则（``sqlalchemy.sql.compiler.BIND_PARAMS``，
# 2.0.49 实测）。它是**驱动层**对「哪里是占位符」的判据：纯文本正则，不认识 SQL 文法 ⇒
# 字面量里「冒号 + 单词字符」且冒号前是引号/空格/逗号等**非**单词字符时同样命中
# （后视断言只排除冒号、单词字符、``$`` 与反斜杠；前视断言只排除冒号、单词字符与 ``$``）。
# T2 的判据是 sqlglot 的 AST
# （字面量根本不是 ``Placeholder`` 节点），两者**必须**对齐，否则字面量会被静默改写。
# 写法定为与上游源码**逐字相同**（连 ``\$`` 这种等价写法也不改写）：这样
# ``tests/unit/database/test_binding.py`` 的对照用例可以直接比 pattern 字面量，上游一改就红。
_BIND_PARAMS = re.compile(r"(?<![:\w\$\x5c]):([\w\$]+)(?![:\w\$])")


def _tokenizer() -> Tokenizer:
    """按 T2 的方言（``policy.DIALECT``）即时建一个分词器。

    不缓存实例：sqlglot 的 ``Tokenizer`` 在 ``tokenize()`` 期间把中间结果存在**实例属性**
    （``self.sql`` / ``self.tokens``）上，跨调用共享不安全；SQL 文本 ≤ 32 KiB，重建成本可忽略
    （T2 解析时同样是一次性使用）。
    """
    return sqlglot.Dialect.get_or_raise(DIALECT).tokenizer()


def _escape_literal_colons(sql: str) -> str:
    """把 SQL 文本里**作为数据**的冒号按 ``text()`` 的约定转义成 ``\\:``。

    为什么需要转义：``text()`` 按 :data:`_BIND_PARAMS` 找占位符，于是
    ``SELECT ':floor' AS lit, :floor AS bound`` 里**字面量那个** ``:floor`` 也被当成绑定位，
    编译成 ``SELECT '%s' AS lit, %s AS bound`` —— 两个 ``%s`` 只有一个参数，字面量被静默
    改写成绑定值。转义是 ``text()`` **自己的**约定：``\\:name`` 不参与占位符匹配，编译期
    再由 ``BIND_PARAMS_ESC`` 把反斜杠剥掉（实测 2.0.49 的 ``visit_textclause``：先
    ``BIND_PARAMS.sub`` 再 ``BIND_PARAMS_ESC.sub``）⇒ **数据库收到的文本与转义前逐字相同**。
    反斜杠因此不进数据库，不依赖数据库端的反斜杠转义语义（也不受 ``NO_BACKSLASH_ESCAPES``
    影响）。

    「哪些冒号是数据」由**词法**判定，且用的是与 T2 同一个库、同一个方言的分词器：
    **未加引号的冒号一定会被切成独立的 ``COLON`` token**（实测 ``:c`` → ``COLON`` + ``VAR``；
    带前导的字符串字面量如 ``N'x'`` 是 ``NATIONAL_STRING``），故「命中位置不是某个 ``COLON``
    token 的起点」⇔「该冒号在带引号的文本（字符串字面量/引号标识符）里」。刻意**不**写成
    「只转义 STRING/IDENTIFIER 两种 token 内部的冒号」：那要求穷举所有带引号的 token 类型，
    漏一种就重新引入静默改写；反向的「多转义」（把真实占位符也转义了）则会在
    :func:`_bound_statement` 的对账里被拦下（fail-closed），代价只是拒一条异常输入。
    """
    syntactic = {
        token.start for token in _tokenizer().tokenize(sql) if token.token_type is TokenType.COLON
    }
    pieces: list[str] = []
    last = 0
    for match in _BIND_PARAMS.finditer(sql):
        if match.start() in syntactic:
            continue
        pieces.append(sql[last : match.start()])
        pieces.append("\\")
        last = match.start()
    pieces.append(sql[last:])
    return "".join(pieces)


def _bound_statement(validated_query: ValidatedQuery) -> TextClause:
    """把 T2 的受控重建文本翻成 ``stream()`` 可执行的语句，并**对账两侧的占位符判断**。

    这是「sqlglot 的 AST 判断」与「SQLAlchemy 的文本正则」之间唯一的对齐点（I-1）：

    1. 先转义**数据冒号**（见 :func:`_escape_literal_colons`），让正则只看见真实占位符；
    2. 再核对「转义后正则认出的占位符名」与「已验证 AST 里的 ``Placeholder`` 名」
       **多重集相等**。不等即按 ``database.sql_unsupported`` 拒绝：宁可给稳定错误，也不让
       两侧理解不一致的文本到达数据库（静默改写比报错更糟）。

    用多重集而不是集合：同一名字出现多次时（``a = :x OR b = :x``）文本与 AST 必须同样多，
    否则「文本里少绑一个」会被集合比较掩盖。该对账同时是转义逻辑的安全网 —— 任何未预料到的
    形态（新 token 类型、sqlglot 改渲染方式）都会落到这里被拒，而不是被悄悄改写。
    """
    try:
        escaped = _escape_literal_colons(validated_query.sql)
    except SqlglotError as exc:
        # 分词失败 ⇒ 无法判定哪些冒号是数据，fail-closed（T2 校验过的文本上不可达）
        raise _refuse(
            DATABASE_SQL_UNSUPPORTED,
            SQL_UNSUPPORTED_TEXT,
            ExecutionRefusalReason.PLACEHOLDER_MISMATCH,
            stage="tokenize",
            error=type(exc).__name__,
        ) from None
    expected = sorted(
        str(placeholder.this)
        for placeholder in validated_query.expression.find_all(exp.Placeholder)
    )
    found = sorted(match.group(1) for match in _BIND_PARAMS.finditer(escaped))
    if expected != found:
        raise _refuse(
            DATABASE_SQL_UNSUPPORTED,
            SQL_UNSUPPORTED_TEXT,
            ExecutionRefusalReason.PLACEHOLDER_MISMATCH,
            stage="bind_agreement",
            ast=",".join(expected),
            driver=",".join(found),
        )
    return text(escaped)


async def _read_page(
    resources: _CallResources,
    connection: AsyncConnection,
    validated_query: ValidatedQuery,
    limits: QueryLimits,
) -> QueryPage:
    """分块有界读取（§7.1）：禁止 ``fetchall()`` 后切片，最多读 ``max_rows + 1`` 行。

    模型 SQL 只在这里进入驱动：``stream`` 用 asyncmy 的 ``SSCursor``（不预缓冲）逐块
    取行，累计字节越界即停；语句先经 :func:`_bound_statement`（数据冒号转义 + 占位符对账），
    ``:name`` → 驱动参数形态的翻译与 ``%`` 转义由 dialect 的 text() 编译层完成
    （见模块 docstring）。

    ⚠️ 结果的登记形状（与清理顺序的证据形态有关，见 :class:`_CallResources`）：
    ``stream()`` 在**首包到达前不返回** ⇒ 若查询在首包前被超时/取消中断，本函数的
    ``resources.result = result`` 那一行根本执行不到，「关闭结果」在清理里**不可达**，
    物理关闭靠 ``invalidate()`` + ``dispose()``。若要让该形态也能 drain，需要绕开
    ``AsyncConnection.stream`` 改用更低层的取游标入口（本版未采用：会失去 dialect 的
    参数翻译与列名口径，收益不足）。
    """
    result = await connection.stream(
        _bound_statement(validated_query), dict(validated_query.parameters)
    )
    resources.result = result
    columns = tuple(str(name) for name in result.keys())

    probe_target = limits.max_rows + 1
    rows: list[tuple[object, ...]] = []
    total_bytes = 0
    byte_stopped = False
    field_reason: str | None = None
    while len(rows) < probe_target:
        chunk = await result.fetchmany(limits.chunk_rows)
        if not chunk:
            break
        for raw_row in chunk:
            if len(rows) >= probe_target:
                break
            try:
                values, cell_reason = _encode_row(raw_row, limits)
            except Exception as exc:  # noqa: BLE001 —— 编码失败整体失败，不吐半页
                raise _refuse(
                    DATABASE_QUERY_FAILED,
                    QUERY_FAILED_TEXT,
                    ExecutionRefusalReason.SERIALIZATION_FAILED,
                    stage="serialize",
                    error=type(exc).__name__,
                ) from None
            field_reason = _worse_reason(field_reason, cell_reason)
            size = _row_bytes(values)
            if total_bytes + size > limits.max_bytes:
                byte_stopped = True
                break
            rows.append(values)
            total_bytes += size
        if byte_stopped:
            break

    truncated = False
    reason: str | None = None
    if len(rows) > limits.max_rows:
        # 多读的那一行只是探针：返回前丢掉，也不计入 returned_rows
        rows = rows[: limits.max_rows]
        truncated, reason = True, TRUNCATION_ROW_LIMIT
    elif byte_stopped:
        truncated, reason = True, TRUNCATION_OUTPUT_BYTES
    elif field_reason is not None:
        truncated, reason = True, field_reason
    return QueryPage(
        columns=columns,
        rows=tuple(rows),
        returned_rows=len(rows),
        truncated=truncated,
        truncation_reason=reason,
        next_cursor=None,  # §4：普通 query 无自动续页
        elapsed_ms=0,  # 由入口按整个调用耗时补齐
    )


# ---------------------------------------------------------------------------
# 对外入口：describe_table
# ---------------------------------------------------------------------------


async def describe_table(
    source: ResolvedSource, table: str, limits: QueryLimits | None = None
) -> TableDescription:
    """描述一张**已授权**表的列、主键与索引（规格 §4 工具契约、§5.2）。

    只走固定内部只读元数据查询（``information_schema``），表名与库名一律绑定参数：
    模型**无法**用它查任意系统表——表名必须先命中 ``source.allowed_tables`` 白名单。
    首版**不查询视图**：命中视图即明确报告不支持。生命周期与 ``execute_readonly``
    同一套设施（独立小池、只读事务、服务端超时、返回前销毁）。
    """
    _require_allowed_table(source, table)
    effective_limits = _resolve_limits(limits)
    started = time.monotonic()
    resources = _CallResources()
    failure: BaseException | None = None
    description: TableDescription | None = None
    try:
        description = await asyncio.wait_for(
            _run_describe(resources, source, table, effective_limits), TOTAL_BUDGET_SECONDS
        )
    except BaseException as exc:  # noqa: BLE001
        failure = exc
    _mark_abnormal(resources, failure)
    await _release(resources)
    if failure is not None:
        raise _as_domain_error(failure) from None
    assert description is not None
    return replace(description, elapsed_ms=_elapsed_ms(started))


async def _run_describe(
    resources: _CallResources,
    source: ResolvedSource,
    table: str,
    limits: QueryLimits,
) -> TableDescription:
    connection = await _connect(resources, source, limits)
    try:
        await _apply_session_guards(connection)
    except QueryError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise _refuse(
            DATABASE_QUERY_FAILED,
            QUERY_FAILED_TEXT,
            ExecutionRefusalReason.SESSION_GUARD_FAILED,
            stage="session_guard",
            error=type(exc).__name__,
        ) from None
    await _require_current_schema(connection, source)

    parameters = {"schema": source.allowed_schema, "table": table}
    table_type = await _read_scalar(connection, SQL_DESCRIBE_TABLE_TYPE, parameters)
    if table_type is None:
        # 白名单里的表在库里不存在：与「未授权」同码，不给出可枚举的差异
        raise _refuse(
            DATABASE_TABLE_NOT_ALLOWED,
            TABLE_NOT_ALLOWED_TEXT,
            ExecutionRefusalReason.TABLE_NOT_ALLOWED,
            connection_id=source.connection_id,
        )
    if table_type == TABLE_TYPE_VIEW:
        # §5.2：首版不查询视图（视图可能有隐含对象与函数访问），明确报告不支持
        raise _refuse(
            DATABASE_TABLE_NOT_ALLOWED,
            VIEW_UNSUPPORTED_TEXT,
            ExecutionRefusalReason.VIEW_NOT_SUPPORTED,
            connection_id=source.connection_id,
        )
    if table_type != TABLE_TYPE_BASE_TABLE:
        raise _refuse(
            DATABASE_TABLE_NOT_ALLOWED,
            TABLE_NOT_ALLOWED_TEXT,
            ExecutionRefusalReason.TABLE_NOT_ALLOWED,
            connection_id=source.connection_id,
        )

    columns = _build_columns(await _read_rows(connection, SQL_DESCRIBE_COLUMNS, parameters))
    indexes = _build_indexes(await _read_rows(connection, SQL_DESCRIBE_INDEXES, parameters))
    primary_key = tuple(column.name for column in columns if column.key == "PRI")
    suitable, reason = _pagination_verdict(primary_key, columns, indexes)
    return TableDescription(
        table=table,
        columns=columns,
        primary_key=primary_key,
        indexes=indexes,
        pagination_suitable=suitable,
        pagination_reason=reason,
        elapsed_ms=0,
    )


async def _read_scalar(
    connection: AsyncConnection, statement: str, parameters: Mapping[str, object]
) -> object:
    try:
        return (await connection.exec_driver_sql(statement, dict(parameters))).scalar()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise _translate_statement_failure(exc) from None


async def _read_rows(
    connection: AsyncConnection, statement: str, parameters: Mapping[str, object]
) -> list[tuple[object, ...]]:
    """读元数据行：仍按 chunk 分块取，不用 ``fetchall``（结果本身被 MySQL 的列/索引
    数量硬上限约束，这里保持一致的分块读取习惯）。"""
    try:
        result = await connection.exec_driver_sql(statement, dict(parameters))
        rows: list[tuple[object, ...]] = []
        while True:
            chunk = result.fetchmany(CHUNK_ROWS)
            if not chunk:
                break
            rows.extend(tuple(row) for row in chunk)
        return rows
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise _translate_statement_failure(exc) from None


def _build_columns(rows: Iterable[Sequence[object]]) -> tuple[ColumnDescription, ...]:
    return tuple(
        ColumnDescription(
            name=str(row[0]),
            data_type=str(row[1]),
            nullable=str(row[2]).upper() == "YES",
            key=str(row[3] or ""),
            default=None if row[4] is None else str(row[4]),
            extra=str(row[5] or ""),
        )
        for row in rows
    )


def _build_indexes(rows: Iterable[Sequence[object]]) -> tuple[IndexDescription, ...]:
    """把 ``information_schema.STATISTICS`` 的逐列行按索引名聚合成索引描述。"""
    order: list[str] = []
    columns: dict[str, list[str]] = {}
    unique: dict[str, bool] = {}
    for row in rows:
        name = str(row[0])
        if name not in columns:
            order.append(name)
            columns[name] = []
            # information_schema.STATISTICS.NON_UNIQUE：0 = 唯一索引
            unique[name] = str(row[1]).strip() == "0"
        columns[name].append(str(row[2]))
    return tuple(
        IndexDescription(name=name, columns=tuple(columns[name]), unique=unique[name])
        for name in order
    )


def _pagination_verdict(
    primary_key: tuple[str, ...],
    columns: tuple[ColumnDescription, ...],
    indexes: tuple[IndexDescription, ...],
) -> tuple[bool, str | None]:
    """规格 §7.2 的扫描门槛：单列、非空、唯一且有索引的整数主键。"""
    if len(primary_key) != 1:
        return False, "首版仅支持单列主键"
    name = primary_key[0]
    column = next((item for item in columns if item.name == name), None)
    if column is None:
        return False, "主键列不在列信息中"
    if column.nullable:
        return False, "主键列可为空"
    if _base_type(column.data_type) not in _INTEGER_TYPES:
        return False, "主键不是整数类型"
    if not any(index.unique and index.columns == (name,) for index in indexes):
        return False, "主键列未发现唯一索引"
    return True, None


def _base_type(data_type: str) -> str:
    """``bigint(20) unsigned`` → ``bigint``（只取类型名部分做判定）。"""
    return data_type.split("(", 1)[0].split(" ", 1)[0].strip().lower()


# ---------------------------------------------------------------------------
# 限额、序列化与错误翻译
# ---------------------------------------------------------------------------


def _resolve_limits(limits: QueryLimits | None) -> QueryLimits:
    """解析并校验资源边界：模型不能提高 §7.1 的硬上限，越界一律 fail-closed。

    ⚠️ 这里的 ``MAX_ROWS_LIMIT``（1000）限的是**返回给模型的行数**（§7.1 逐字），
    不是 LIMIT 子句的上限：调用方按截断探针约定传给 ``validate_query`` 的 ``max_rows + 1``
    （最多 1001）由 ``policy.MAX_ROWS_LIMIT_WITH_PROBE`` 把关。**不要**为了「对齐」把
    这一侧也放开到 1001（那会让 ``returned_rows`` 突破规格上限），也不要反过来收紧
    policy 那一侧（探针会取不到，截断判定失效）。
    """
    effective = QueryLimits() if limits is None else limits
    valid = (
        1 <= effective.max_rows <= MAX_ROWS_LIMIT
        and 1 <= effective.chunk_rows <= MAX_ROWS_LIMIT
        and 0 < effective.max_bytes <= MAX_OUTPUT_BYTES
        and 0 < effective.max_field_bytes <= MAX_FIELD_BYTES
    )
    if not valid:
        raise _refuse(
            DATABASE_LIMIT_EXCEEDED,
            LIMIT_EXCEEDED_TEXT,
            ExecutionRefusalReason.LIMITS_INVALID,
            stage="limits",
        )
    return effective


def _require_allowed_table(source: ResolvedSource, table: str) -> None:
    """表名必须精确命中白名单：模型不能借 describe 摸任意（系统）表。"""
    if table not in source.allowed_tables:
        raise _refuse(
            DATABASE_TABLE_NOT_ALLOWED,
            TABLE_NOT_ALLOWED_TEXT,
            ExecutionRefusalReason.TABLE_NOT_ALLOWED,
            connection_id=source.connection_id,
            table=table,
        )


def _encode_row(
    row: Sequence[object], limits: QueryLimits
) -> tuple[tuple[object, ...], str | None]:
    reason: str | None = None
    values: list[object] = []
    for value in row:
        encoded, cell_reason = _encode_cell(value, limits)
        values.append(encoded)
        reason = _worse_reason(reason, cell_reason)
    return tuple(values), reason


def _encode_cell(value: object, limits: QueryLimits) -> tuple[object, str | None]:
    """把驱动返回值转成可回传形态（规格 §4 序列化规则）。

    - ``Decimal`` → 字符串（保留精确值）；日期时间 → ISO（**不擅自赋予时区**）；
    - 超出 JSON 安全整数范围的整数 → 字符串（保留精度）；
    - 二进制 → 只报告类型与长度，不回传内容，并标记截断；
    - 字符串超单字段上限 → 按 UTF-8 字节截断（不切碎多字节字符）并标记截断；
    - 其余类型（驱动新增类型）兜底 ``str()``，同样受单字段上限约束。
    """
    if value is None:
        return None, None
    if isinstance(value, bool):  # 必须先于 int：bool 是 int 的子类
        return value, None
    if isinstance(value, int):
        if -JSON_SAFE_INT_MAX <= value <= JSON_SAFE_INT_MAX:
            return value, None
        return str(value), None
    if isinstance(value, float):
        return value, None
    if isinstance(value, Decimal):
        return str(value), None
    if isinstance(value, (datetime, date, time_of_day)):
        return value.isoformat(), None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary {len(bytes(value))} bytes>", TRUNCATION_BINARY_FIELD
    rendered = value if isinstance(value, str) else str(value)
    if len(rendered.encode("utf-8")) > limits.max_field_bytes:
        return _truncate_utf8(rendered, limits.max_field_bytes), TRUNCATION_FIELD_BYTES
    return rendered, None


def _truncate_utf8(value: str, limit: int) -> str:
    """按 UTF-8 字节截断：忽略结尾被切碎的多字节字符，不产出替换符。"""
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _row_bytes(values: Sequence[object]) -> int:
    """一行结果的序列化字节数（按 JSON 形态计），用于总输出预算。"""
    return len(json.dumps(values, ensure_ascii=False, default=str).encode("utf-8"))


def _worse_reason(current: str | None, new: str | None) -> str | None:
    """截断原因取「更外层」的那条：字段截断优先于二进制替换。"""
    if current is None:
        return new
    if new is None:
        return current
    rank = {TRUNCATION_BINARY_FIELD: 0, TRUNCATION_FIELD_BYTES: 1}
    return new if rank.get(new, 0) > rank.get(current, 0) else current


def _dbapi_errno(exc: BaseException) -> int | None:
    """取 DBAPI 错误码（asyncmy 把 errno 放在 ``orig.args[0]``）。"""
    args = getattr(getattr(exc, "orig", None), "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


def _translate_statement_failure(exc: BaseException) -> QueryError:
    """把驱动异常翻成稳定错误：服务端超时中断 → ``database.timeout``，其余 → 查询失败。"""
    errno = _dbapi_errno(exc)
    if errno in _TIMEOUT_ERRNOS:
        return _refuse(
            DATABASE_TIMEOUT,
            TIMEOUT_TEXT,
            ExecutionRefusalReason.SERVER_TIMEOUT,
            stage="statement",
            errno=errno,
        )
    return _refuse(
        DATABASE_QUERY_FAILED,
        QUERY_FAILED_TEXT,
        ExecutionRefusalReason.STATEMENT_FAILED,
        stage="statement",
        error=type(exc).__name__,
        errno=errno,
    )


def _mark_abnormal(resources: _CallResources, failure: BaseException | None) -> None:
    """超时/取消后连接停在被中断的语句上：标记为不可复用（清理时销毁而不是归还池）。"""
    if isinstance(failure, (asyncio.TimeoutError, asyncio.CancelledError)):
        resources.abnormal = True
        return
    # 预算类失败在 _run_query 里已被翻成稳定错误，故还要按原因码判断一次：
    # 客户端预算用尽 / 服务端中断查询，都意味着驱动连接的前一条语句没有正常收尾
    if isinstance(failure, QueryError) and failure.reason in _ABNORMAL_REASONS:
        resources.abnormal = True


def _as_domain_error(failure: BaseException) -> BaseException:
    """把内部失败翻成对外错误；取消原样重抛（调用方的取消语义不得被改写）。"""
    if isinstance(failure, asyncio.CancelledError):
        return failure
    if isinstance(failure, QueryError):
        return failure
    if isinstance(failure, asyncio.TimeoutError):
        return _refuse(
            DATABASE_TIMEOUT,
            TIMEOUT_TEXT,
            ExecutionRefusalReason.CLIENT_DEADLINE,
            stage="total_budget",
        )
    return _refuse(
        DATABASE_QUERY_FAILED,
        QUERY_FAILED_TEXT,
        ExecutionRefusalReason.STATEMENT_FAILED,
        stage="unknown",
        error=type(failure).__name__,
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def check_connection(source: ResolvedSource, limits: QueryLimits | None = None) -> str:
    """建立只读会话、读回 schema 后销毁资源（``doctor --connect`` 专用）。

    这是唯一不经过模型 SQL 的连接入口：固定内部语句 + 只读事务 + 超时读回，
    不接受任何模型文本。返回连接实际库名，供调用方与 ``allowed_schema`` 对账。
    """
    effective_limits = _resolve_limits(limits)
    resources = _CallResources()
    failure: BaseException | None = None
    schema: str | None = None
    try:
        schema = await asyncio.wait_for(
            _run_connection_check(resources, source, effective_limits),
            TOTAL_BUDGET_SECONDS,
        )
    except BaseException as exc:  # noqa: BLE001 —— 失败一律先清理再决定如何外泄
        failure = exc
    _mark_abnormal(resources, failure)
    await _release(resources)
    if failure is not None:
        raise _as_domain_error(failure) from None
    assert schema is not None
    return schema


async def _run_connection_check(
    resources: _CallResources, source: ResolvedSource, limits: QueryLimits
) -> str:
    connection = await _connect(resources, source, limits)
    try:
        await _apply_session_guards(connection)
    except QueryError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise _refuse(
            DATABASE_QUERY_FAILED,
            QUERY_FAILED_TEXT,
            ExecutionRefusalReason.SESSION_GUARD_FAILED,
            stage="session_guard",
            error=type(exc).__name__,
        ) from None
    await _require_current_schema(connection, source)
    return source.allowed_schema
