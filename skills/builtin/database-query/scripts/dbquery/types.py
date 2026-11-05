"""可移植数据库查询的类型层：稳定错误码、结果形状与解析结果。

字段名与语义沿用原平台 ``smart_assistant.database.types``，但去掉了平台依赖。
本模块不含建连代码，也不依赖配置加载层。
"""

from dataclasses import dataclass
from typing import Annotated

from pydantic import SecretStr, StringConstraints

from dbquery.errors import DomainError, log_refusal

# 稳定错误码：不存在与无权访问数据源共用同一码，防探测式枚举
DATABASE_SOURCE_UNAVAILABLE = "database.source_unavailable"
DATABASE_UNSUPPORTED_DIALECT = "database.unsupported_dialect"

# 对外统一文案：不存在 / 无权 / 停用 / 未通过部署检查 / 缺凭据 不可区分
SOURCE_UNAVAILABLE_TEXT = "数据源不存在或未授权"
UNSUPPORTED_DIALECT_TEXT = "数据源类型不受支持"

# 数据源 ID 规范（即 connection_id：小写字母开头，允许数字/下划线/连字符）
SOURCE_ID_PATTERN = r"^[a-z][a-z0-9_-]{2,63}$"
# secret_env 只接受「环境变量名」：写死连接串属非法配置
ENV_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
# schema / 表名白名单元素只允许裸标识符
IDENTIFIER_PATTERN = r"^[A-Za-z_][A-Za-z0-9_$]{0,63}$"

SourceId = Annotated[str, StringConstraints(pattern=SOURCE_ID_PATTERN)]
EnvName = Annotated[str, StringConstraints(pattern=ENV_NAME_PATTERN)]
Identifier = Annotated[str, StringConstraints(pattern=IDENTIFIER_PATTERN)]
# 库名可含点号，作为一个完整名称匹配；SQL 限定库名时须整体用反引号引用
SchemaName = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z0-9_$]+)*$", max_length=64),
]


class RefusalReason:
    """内部拒绝原因码（对外一律 ``database.source_unavailable``）。

    取值是固定枚举，不含 URL、口令或任何配置取值，可直接用于日志检索。
    """

    # 配置面
    SOURCE_NOT_CONFIGURED = "source_not_configured"
    SOURCE_DISABLED = "source_disabled"
    ACCOUNT_UNVERIFIED = "account_unverified"
    # 配置文件面
    CONFIG_INVALID = "config_invalid"
    # 凭据与连接串面
    MISSING_SECRET = "missing_secret"
    INVALID_URL = "invalid_url"
    UNSUPPORTED_DIALECT = "unsupported_dialect"


class SourceUnavailableError(DomainError):
    """统一的「数据源不可用」拒绝错误（不存在与无权访问同码同文案）。"""

    def __init__(self, reason: str, **context: str) -> None:
        super().__init__(DATABASE_SOURCE_UNAVAILABLE, SOURCE_UNAVAILABLE_TEXT, status=404)
        self.reason = reason
        self.context = context


def source_unavailable(reason: str, **context: str) -> SourceUnavailableError:
    """构造统一拒绝错误并写入日志：**必须**给出内部原因码。

    上下文只允许放 ID 等服务端标识，绝不放连接串、口令或配置取值。
    """
    log_refusal(reason, context)
    return SourceUnavailableError(reason, **context)


def unsupported_dialect(**context: str) -> DomainError:
    """数据源方言不受支持（部署配置错误：改配置才能恢复，不进入重试路径）。"""
    log_refusal(RefusalReason.UNSUPPORTED_DIALECT, context)
    return DomainError(DATABASE_UNSUPPORTED_DIALECT, UNSUPPORTED_DIALECT_TEXT, status=503)


@dataclass(frozen=True, slots=True)
class QueryPage:
    """一次有界查询的统一结果形状。

    - ``columns``：列名，按位置与每行对应（重名列各占一格，不合并）；
    - ``rows``：按列顺序的数组行（不是字典，重名列不会互相覆盖）；
    - ``returned_rows``：返回给模型的行数（不含截断探针多读的那一行）；
    - ``truncated`` / ``truncation_reason``：结果是否被截断及原因；只有
      ``truncated=False`` 才代表本次结果完整；
    - ``next_cursor``：普通 query 恒为空；
    - ``elapsed_ms``：整个调用的墙钟耗时（含清理）。

    ★ 截断探针调用约定：调用方必须按 ``limits.max_rows + 1`` 调
    :func:`dbquery.policy.validate_query`，执行层最多读 ``max_rows + 1`` 行、
    返回 ``max_rows`` 行；不按此约定调用永远无法区分完整与截断。
    """

    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    returned_rows: int
    truncated: bool
    truncation_reason: str | None
    next_cursor: str | None
    elapsed_ms: int


@dataclass(frozen=True, slots=True)
class ColumnDescription:
    """``describe_table`` 的单列信息（服务端元数据原文，不做猜测性改写）。"""

    name: str
    data_type: str
    nullable: bool
    key: str
    default: str | None
    extra: str


@dataclass(frozen=True, slots=True)
class IndexDescription:
    """``describe_table`` 的单个索引（列序按索引内的顺序）。"""

    name: str
    columns: tuple[str, ...]
    unique: bool


@dataclass(frozen=True, slots=True)
class TableDescription:
    """已授权表的元数据描述（列、类型、主键、索引与是否适合分页）。"""

    table: str
    columns: tuple[ColumnDescription, ...]
    primary_key: tuple[str, ...]
    indexes: tuple[IndexDescription, ...]
    pagination_suitable: bool
    pagination_reason: str | None
    elapsed_ms: int


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    """一次成功解析的结果：固定驱动连接配置 + 白名单 + 不含秘密的配置摘要。

    - ``url``：已规范为固定 asyncmy 驱动、且不含任何 URL 参数的连接串
      （``SecretStr`` 不进 repr/日志）；
    - ``allowed_schema`` / ``allowed_tables``：AST 校验与元数据层的白名单来源；
    - ``config_summary``：可呈现给模型的说明，不含地址、账号与凭据。

    刻意不携带 ``readonly_account_verified``：它只是部署检查记录，不能代替
    数据库端只读账户。
    """

    connection_id: str
    dialect: str
    url: SecretStr
    allowed_schema: str
    allowed_tables: tuple[str, ...]
    config_summary: str
