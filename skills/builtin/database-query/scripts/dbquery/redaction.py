"""日志脱敏：隐藏连接串口令与显式秘密值，转义控制字符并限长。

过滤器安装在 root logger 上，因此解析器（如 sqlglot）自身发出的告警也会经过
同一套脱敏，而不是只包最外层异常。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable

_MAX_LEN = 4096

# 只匹配连接串的 userinfo 口令段；主机、端口与库名保留，便于排查
_URL_CREDENTIALS = re.compile(r"(?i)(mysql(?:\+asyncmy)?://[^:\s/]+:)([^@\s]+)(@)")
_REDACTED = "[REDACTED]"


def redact(text: object, secrets: Iterable[str]) -> str:
    """返回可安全写日志的文本：口令替换、控制字符转义、超长截断。"""
    try:
        rendered = text if isinstance(text, str) else str(text)
    except Exception:  # noqa: BLE001 - 不可渲染对象不得拖垮日志
        rendered = "<unrenderable>"
    rendered = _URL_CREDENTIALS.sub(r"\1***\3", rendered)
    for secret in secrets:
        if secret:
            rendered = rendered.replace(secret, _REDACTED)
    if any(ord(char) < 32 or ord(char) == 127 for char in rendered):
        rendered = repr(rendered)
    if len(rendered) > _MAX_LEN:
        rendered = f"{rendered[:_MAX_LEN]}...(truncated {len(rendered) - _MAX_LEN})"
    return rendered


class RedactingFilter(logging.Filter):
    """对所有日志记录套用 :func:`redact`；秘密提供者可动态返回当前环境值。"""

    def __init__(self, secrets: Callable[[], Iterable[str]]) -> None:
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            secrets = tuple(self._secrets())
        except Exception:  # noqa: BLE001 - 秘密来源故障时仍须脱敏 URL 形态
            secrets = ()
        record.msg = redact(record.getMessage(), secrets)
        record.args = ()
        return True


def install_redaction(secrets: Callable[[], Iterable[str]], level: int = logging.WARNING) -> None:
    """安装脱敏过滤器与 stderr handler（幂等）。

    子 logger 的记录会经 root 的 handler 输出，而**祖先 logger 的 filter 不参与
    传播**；因此过滤器同时挂到 root logger 与所有已存在的 handler 上。
    """
    root = logging.getLogger()
    redaction = RedactingFilter(secrets)
    if not any(isinstance(item, RedactingFilter) for item in root.filters):
        root.addFilter(redaction)
    if not root.handlers:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(stream_handler)
    for existing in root.handlers:
        if not any(isinstance(item, RedactingFilter) for item in existing.filters):
            existing.addFilter(redaction)
    if root.level > level:
        root.setLevel(level)
