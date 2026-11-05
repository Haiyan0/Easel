"""本地数据源解析：connection_id → 固定 asyncmy 连接串（不建立连接）。

拒绝顺序：ID 规范 → 已登记 → 启用 → 只读账户部署检查 → **最后**才读取秘密。
因此停用、未验证与未知数据源的拒绝路径不会读取凭据。
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from pydantic import SecretStr

from dbquery.config import SourceConfig
from dbquery.types import (
    SOURCE_ID_PATTERN,
    RefusalReason,
    ResolvedSource,
    source_unavailable,
    unsupported_dialect,
)

logger = logging.getLogger(__name__)

_SOURCE_ID_RE = re.compile(SOURCE_ID_PATTERN)

# 首版仅支持 MySQL 8.0：只接受这些 scheme，其余一律拒绝
_SUPPORTED_SCHEMES = frozenset({"mysql", "mysql+asyncmy"})
_FIXED_SCHEME = "mysql+asyncmy"
_FIXED_DIALECT = "mysql"
_DEFAULT_MYSQL_PORT = 3306


class LocalSourceResolver:
    """按 connection_id 解析本地配置的数据源；不读取任何平台授权状态。"""

    def __init__(
        self,
        sources: tuple[SourceConfig, ...],
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._sources = {source.id: source for source in sources}
        self._environ: Mapping[str, str] = os.environ if environ is None else environ

    def resolve(self, connection_id: str) -> ResolvedSource:
        if _SOURCE_ID_RE.fullmatch(connection_id) is None:
            raise source_unavailable(
                RefusalReason.SOURCE_NOT_CONFIGURED, connection_id=connection_id
            )
        source = self._sources.get(connection_id)
        if source is None:
            raise source_unavailable(
                RefusalReason.SOURCE_NOT_CONFIGURED, connection_id=connection_id
            )
        if not source.enabled:
            raise source_unavailable(RefusalReason.SOURCE_DISABLED, connection_id=connection_id)
        if not source.readonly_account_verified:
            raise source_unavailable(RefusalReason.ACCOUNT_UNVERIFIED, connection_id=connection_id)
        url = _materialize_url(source, self._environ)
        return ResolvedSource(
            connection_id=source.id,
            dialect=_FIXED_DIALECT,
            url=url,
            allowed_schema=source.allowed_schema,
            allowed_tables=source.allowed_tables,
            config_summary=(
                f"MySQL 只读数据源 {source.id}（schema={source.allowed_schema}，"
                f"授权表 {len(source.allowed_tables)} 张）"
            ),
        )


def _materialize_url(source: SourceConfig, environ: Mapping[str, str]) -> SecretStr:
    """读取 ``secret_env`` 并规范为固定驱动、无参数、库名匹配的连接串。"""
    raw = (environ.get(source.secret_env) or "").strip()
    if not raw:
        raise source_unavailable(
            RefusalReason.MISSING_SECRET,
            connection_id=source.id,
            secret_env=source.secret_env,
        )
    try:
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        hostname = parts.hostname
        username = parts.username
        port = parts.port
    except ValueError:
        raise source_unavailable(
            RefusalReason.INVALID_URL, connection_id=source.id, detail="unparsable"
        ) from None
    if scheme not in _SUPPORTED_SCHEMES:
        raise unsupported_dialect(connection_id=source.id)
    # 拒绝任意 URL 参数：含多语句执行开关与 Unix socket 参数；片段同样拒绝
    if "?" in raw or "#" in raw:
        raise source_unavailable(
            RefusalReason.INVALID_URL, connection_id=source.id, detail="query_or_fragment"
        )
    if not hostname or not username:
        raise source_unavailable(
            RefusalReason.INVALID_URL, connection_id=source.id, detail="missing_host_or_user"
        )
    if parts.path.lstrip("/") != source.allowed_schema:
        raise source_unavailable(
            RefusalReason.INVALID_URL, connection_id=source.id, detail="schema_mismatch"
        )
    userinfo = f"{username}:{parts.password}@" if parts.password is not None else f"{username}@"
    host = f"[{hostname}]" if ":" in hostname else hostname
    port_number = port if port is not None else _DEFAULT_MYSQL_PORT
    return SecretStr(f"{_FIXED_SCHEME}://{userinfo}{host}:{port_number}{parts.path}")
