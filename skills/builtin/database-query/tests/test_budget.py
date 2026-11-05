"""本地 SQLite 运行预算与并发租约。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from dbquery.budget import (
    LEASE_SECONDS,
    MAX_CALLS_PER_RUN,
    MAX_GLOBAL,
    MAX_ROWS_PER_RUN,
    LocalBudget,
)
from dbquery.errors import DomainError
from dbquery.types import ResolvedSource

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _source(
    connection_id: str = "src_a",
    *,
    host: str = "host",
    schema: str = "schema",
    password: str = "canary",
) -> ResolvedSource:
    return ResolvedSource(
        connection_id=connection_id,
        dialect="mysql",
        url=SecretStr(f"mysql+asyncmy://u:{password}@{host}:3306/{schema}"),
        allowed_schema=schema,
        allowed_tables=("orders",),
        config_summary="安全摘要",
    )


def _code(exc: pytest.ExceptionInfo[DomainError]) -> str:
    return exc.value.code


def test_run_must_be_created_explicitly(tmp_path: Path) -> None:
    budget = LocalBudget(tmp_path / "budget.sqlite3")

    with pytest.raises(DomainError) as exc:
        budget.reserve("missing-run", 1)

    assert _code(exc) == "database.run_not_found"
    with pytest.raises(DomainError):
        budget.run_info("missing-run")


def test_create_run_rejects_duplicate_and_invalid_ids(tmp_path: Path) -> None:
    budget = LocalBudget(tmp_path / "budget.sqlite3")
    created = budget.create_run("run-1", ttl_seconds=60)

    assert created.run_id == "run-1"
    assert created.calls == 0
    with pytest.raises(DomainError) as exc:
        budget.create_run("run-1", ttl_seconds=60)
    assert _code(exc) == "database.run_exists"

    for run_id in ("", "a", "../x", "run id"):
        with pytest.raises(DomainError) as invalid:
            budget.create_run(run_id)
        assert _code(invalid) == "database.invalid_argument"


def test_call_cap_is_40(tmp_path: Path) -> None:
    budget = LocalBudget(tmp_path / "budget.sqlite3")
    budget.create_run("run-1")

    for _ in range(MAX_CALLS_PER_RUN):
        budget.reserve("run-1", 0)

    with pytest.raises(DomainError) as exc:
        budget.reserve("run-1", 0)
    assert _code(exc) == "database.budget_exceeded"


def test_row_cap_is_10000(tmp_path: Path) -> None:
    budget = LocalBudget(tmp_path / "budget.sqlite3")
    budget.create_run("run-1")

    for _ in range(MAX_ROWS_PER_RUN // 1000):
        budget.reserve("run-1", 1000)

    with pytest.raises(DomainError) as exc:
        budget.reserve("run-1", 1)
    assert _code(exc) == "database.budget_exceeded"


def test_settle_returns_unused_rows(tmp_path: Path) -> None:
    budget = LocalBudget(tmp_path / "budget.sqlite3")
    budget.create_run("run-1")

    reservation = budget.reserve("run-1", 200)
    budget.settle(reservation, 50)

    assert budget.run_info("run-1").rows == 50
    for _ in range(9):
        budget.reserve("run-1", 1000)
    budget.reserve("run-1", 950)
    with pytest.raises(DomainError) as exc:
        budget.reserve("run-1", 1)
    assert _code(exc) == "database.budget_exceeded"


def test_settle_validates_returned_rows_and_is_single_use(tmp_path: Path) -> None:
    budget = LocalBudget(tmp_path / "budget.sqlite3")
    budget.create_run("run-1")
    reservation = budget.reserve("run-1", 10)

    with pytest.raises(ValueError):
        budget.settle(reservation, -1)
    with pytest.raises(ValueError):
        budget.settle(reservation, 11)

    budget.settle(reservation, 10)
    with pytest.raises(DomainError) as exc:
        budget.settle(reservation, 10)
    assert _code(exc) == "database.budget_unavailable"


def test_expired_run_is_rejected(tmp_path: Path) -> None:
    now = [1000.0]
    budget = LocalBudget(tmp_path / "budget.sqlite3", clock=lambda: now[0])
    budget.create_run("run-1", ttl_seconds=10)
    now[0] += 11

    with pytest.raises(DomainError) as exc:
        budget.reserve("run-1", 1)
    assert _code(exc) == "database.run_expired"


def test_target_key_groups_aliases_and_ignores_secret(tmp_path: Path) -> None:
    budget = LocalBudget(tmp_path / "budget.sqlite3")
    original = _source()

    alias = _source("src_b", host="HOST", password="other-password")

    assert budget.target_key(original) == budget.target_key(alias)
    assert budget.target_key(original) != budget.target_key(_source(schema="other"))
    assert "host" not in budget.target_key(original)


def test_per_target_and_global_limits(tmp_path: Path) -> None:
    now = [1000.0]
    budget = LocalBudget(tmp_path / "budget.sqlite3", clock=lambda: now[0])
    budget.create_run("run-1")

    first = budget.acquire("target-a", "run-1")
    budget.acquire("target-a", "run-1")
    with pytest.raises(DomainError) as busy_target:
        budget.acquire("target-a", "run-1")
    assert _code(busy_target) == "database.busy"
    budget.release(first)
    budget.acquire("target-a", "run-1")

    for index in range(MAX_GLOBAL - 2):
        budget.acquire(f"target-{index}", "run-1")
    with pytest.raises(DomainError) as busy_global:
        budget.acquire("target-extra", "run-1")
    assert _code(busy_global) == "database.busy"

    now[0] += LEASE_SECONDS + 1
    budget.acquire("target-extra", "run-1")


def test_acquire_requires_existing_run(tmp_path: Path) -> None:
    budget = LocalBudget(tmp_path / "budget.sqlite3")

    with pytest.raises(DomainError) as exc:
        budget.acquire("target", "missing-run")

    assert _code(exc) == "database.run_not_found"


def test_budget_cap_across_processes(tmp_path: Path) -> None:
    db_path = tmp_path / "budget.sqlite3"
    LocalBudget(db_path).create_run("run-concurrent", ttl_seconds=3600)
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from dbquery.budget import LocalBudget\n"
        "from dbquery.errors import DomainError\n"
        "try:\n"
        "    LocalBudget(Path(sys.argv[2])).reserve('run-concurrent', 1)\n"
        "except DomainError as exc:\n"
        "    raise SystemExit(1 if exc.code == 'database.budget_exceeded' else 2)\n"
        "raise SystemExit(0)\n"
    )
    processes = [
        subprocess.Popen([sys.executable, "-c", code, str(SCRIPTS), str(db_path)])
        for _ in range(MAX_CALLS_PER_RUN + 5)
    ]
    results = [process.wait(timeout=60) for process in processes]

    assert results.count(0) == MAX_CALLS_PER_RUN
    assert results.count(1) == 5
    assert results.count(2) == 0
    assert LocalBudget(db_path).run_info("run-concurrent").calls == MAX_CALLS_PER_RUN
