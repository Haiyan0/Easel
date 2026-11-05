"""数据源配置解析、URL 规范与秘密读取边界的单元测试（不连接数据库）。"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping

import pytest
from pydantic import ValidationError

from dbquery.config import SourceConfig
from dbquery.errors import DomainError
from dbquery.sources import LocalSourceResolver

_SECRET_ENV = "SALES_READONLY_DATABASE_URL"
_CANARY_PASSWORD = "canary-password-0001"
_CANARY_USER = "canary-account"
_CANARY_HOST = "db.internal"
_CANARY_URL = f"mysql://{_CANARY_USER}:{_CANARY_PASSWORD}@{_CANARY_HOST}:3306/sales"


class SpyEnviron(Mapping[str, str]):
    """记录访问过的键，用于证明拒绝路径从未读取凭据。"""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)
        self.accessed: list[str] = []

    def __getitem__(self, key: str) -> str:
        self.accessed.append(key)
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def get(self, key: str, default: str | None = None) -> str | None:
        self.accessed.append(key)
        return self._values.get(key, default)


def _source(**overrides: object) -> SourceConfig:
    data: dict[str, object] = {
        "id": "sales_readonly",
        "secret_env": _SECRET_ENV,
        "allowed_schema": "sales",
        "allowed_tables": ("orders", "customers"),
        "readonly_account_verified": True,
        "enabled": True,
    }
    data.update(overrides)
    return SourceConfig.model_validate(data)


def _resolver(source: SourceConfig, environ: Mapping[str, str]) -> LocalSourceResolver:
    return LocalSourceResolver((source,), environ)


def _code(exc: pytest.ExceptionInfo[DomainError]) -> str:
    return exc.value.code


def test_url_is_normalized_to_asyncmy_with_default_port() -> None:
    environ = SpyEnviron({_SECRET_ENV: _CANARY_URL})

    resolved = _resolver(_source(), environ).resolve("sales_readonly")

    assert resolved.url.get_secret_value() == (
        f"mysql+asyncmy://{_CANARY_USER}:{_CANARY_PASSWORD}@{_CANARY_HOST}:3306/sales"
    )
    assert resolved.allowed_schema == "sales"
    assert resolved.allowed_tables == ("orders", "customers")
    assert _CANARY_PASSWORD not in resolved.config_summary
    assert _CANARY_HOST not in resolved.config_summary


def test_percent_encoded_password_is_preserved() -> None:
    environ = SpyEnviron({_SECRET_ENV: "mysql://u:p%40ss@host:3307/sales"})

    resolved = _resolver(_source(), environ).resolve("sales_readonly")

    assert "p%40ss" in resolved.url.get_secret_value()


@pytest.mark.parametrize(
    "url",
    [
        "mysql://u:p@host/sales?multiStatements=true",
        "mysql://u:p@host/sales#fragment",
        "mysql://u@/sales",
        "mysql://host/sales",
        "mysql://u:p@host/other",
        "postgres://u:p@host/sales",
        "mysql://u:p@host/",
    ],
)
def test_invalid_url_is_rejected(url: str) -> None:
    environ = SpyEnviron({_SECRET_ENV: url})

    with pytest.raises(DomainError) as exc:
        _resolver(_source(), environ).resolve("sales_readonly")

    assert exc.value.code in {"database.source_unavailable", "database.unsupported_dialect"}
    assert _CANARY_PASSWORD not in str(exc.value)


def test_missing_secret_env_rejected() -> None:
    environ = SpyEnviron({})

    with pytest.raises(DomainError) as exc:
        _resolver(_source(), environ).resolve("sales_readonly")

    assert _code(exc) == "database.source_unavailable"
    assert environ.accessed == [_SECRET_ENV]


def test_disabled_source_rejected_without_reading_secret() -> None:
    environ = SpyEnviron({_SECRET_ENV: _CANARY_URL})

    with pytest.raises(DomainError) as exc:
        _resolver(_source(enabled=False), environ).resolve("sales_readonly")

    assert _code(exc) == "database.source_unavailable"
    assert environ.accessed == []


def test_unverified_account_rejected_without_reading_secret() -> None:
    environ = SpyEnviron({_SECRET_ENV: _CANARY_URL})

    with pytest.raises(DomainError) as exc:
        _resolver(_source(readonly_account_verified=False), environ).resolve("sales_readonly")

    assert _code(exc) == "database.source_unavailable"
    assert environ.accessed == []


@pytest.mark.parametrize("connection_id", ["unknown", "SALES", "a", "../etc"])
def test_unknown_or_invalid_connection_id_rejected(connection_id: str) -> None:
    environ = SpyEnviron({_SECRET_ENV: _CANARY_URL})

    with pytest.raises(DomainError) as exc:
        _resolver(_source(), environ).resolve(connection_id)

    assert _code(exc) == "database.source_unavailable"
    assert environ.accessed == []


def test_rejection_does_not_log_or_echo_secret(caplog: pytest.LogCaptureFixture) -> None:
    environ = SpyEnviron({_SECRET_ENV: "mysql://u:p@host/other"})

    with caplog.at_level(logging.WARNING):
        with pytest.raises(DomainError) as exc:
            _resolver(_source(), environ).resolve("sales_readonly")

    assert _CANARY_PASSWORD not in str(exc.value)
    assert all("p@host" not in record.getMessage() for record in caplog.records)


def test_source_config_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SourceConfig.model_validate(
            {
                "id": "sales_readonly",
                "secret_env": _SECRET_ENV,
                "allowed_schema": "sales",
                "allowed_tables": ["orders"],
                "readonly_account_verified": True,
                "enabled": True,
                "extra": 1,
            }
        )
