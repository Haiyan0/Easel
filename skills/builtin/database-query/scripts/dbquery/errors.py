"""稳定领域错误与日志取值脱敏。

本模块只保存错误契约与日志工具，不依赖任何平台模块。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

logger = logging.getLogger(__name__)

# 日志上下文取值的截断上限：模型可控输入不得把日志灌爆
_LOG_VALUE_MAX = 64


class DomainError(Exception):
    """业务规则违反时抛出的领域错误。

    ``code`` 用于机器识别，``message`` 供人阅读；字符串表示同时携带稳定错误码，
    便于日志与测试按 code 匹配。
    """

    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class QueryError(DomainError):
    """执行层稳定错误：``reason`` 与 ``context`` 仅服务端可见。

    对外与普通 ``DomainError`` 完全同形；内部原因码用于排查与测试断言，
    绝不进任何 JSON 响应，也不含 SQL 正文、参数取值或连接串。
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


def _log_value(value: str) -> str:
    """日志上下文取值的安全形态：repr 转义控制字符 + 超长截断。"""
    if len(value) > _LOG_VALUE_MAX:
        return f"{value[:_LOG_VALUE_MAX]!r}(+{len(value) - _LOG_VALUE_MAX})"
    return repr(value)


def log_refusal(reason: str, context: Mapping[str, str]) -> None:
    """拒绝原因写服务端日志：稳定原因码 + 调用上下文（不含 URL/口令/配置取值）。"""
    rendered = " ".join(f"{key}={_log_value(value)}" for key, value in context.items())
    logger.warning("受控数据库工具拒绝访问：reason=%s %s", reason, rendered)
