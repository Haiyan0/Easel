"""真实 MySQL 8 探针测试的环境门控与探针库安全校验。"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

TABLE = "probe_orders"


@dataclass(frozen=True)
class Probe:
    url: str
    schema: str


@pytest.fixture(scope="session")
def probe_target() -> Probe:
    raw = os.environ.get("DATABASE_QUERY_TEST_URL", "").strip()
    if not raw:
        pytest.skip("DATABASE_QUERY_TEST_URL 未设置")
    if raw.startswith("mysql://"):
        raw = f"mysql+asyncmy://{raw[len('mysql://') :]}"
    schema = urlsplit(raw).path.lstrip("/")
    if not schema.endswith("_probe"):
        pytest.skip("拒绝在非探针库执行写入验证")
    return Probe(raw, schema)


@pytest.fixture
async def probe(probe_target: Probe) -> Probe:
    # 本机 Docker 端口代理偶发 TCP 连接挂起；夹具层做有限重试，产品默认预算不变
    engine = create_async_engine(
        probe_target.url, connect_args={"connect_timeout": 5, "read_timeout": 15}
    )
    try:
        for attempt in range(3):
            try:
                async with engine.begin() as connection:
                    await connection.exec_driver_sql(f"DROP TABLE IF EXISTS {TABLE}")
                    await connection.exec_driver_sql(
                        f"""
                        CREATE TABLE {TABLE} (
                          id BIGINT PRIMARY KEY,
                          amount DECIMAL(30,2) NOT NULL,
                          note VARCHAR(255),
                          created_at DATETIME NOT NULL
                        )
                        """
                    )
                    await connection.exec_driver_sql(
                        f"""
                        INSERT INTO {TABLE} (id, amount, note, created_at) VALUES
                          (1, 10.50, 'a', '2026-01-01 00:00:00'),
                          (2, 20.25, 'b', '2026-01-02 00:00:00'),
                          (9007199254740993, 30.75, 'big', '2026-01-03 00:00:00')
                        """
                    )
                break
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.5)
        yield probe_target
    finally:
        try:
            async with engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP TABLE IF EXISTS {TABLE}")
        finally:
            await engine.dispose()
