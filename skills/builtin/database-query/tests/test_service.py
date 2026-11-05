"""查询编排：执行前拒绝顺序、预算结算、结果保存与多数据源。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbquery.budget import LocalBudget
from dbquery.catalog import ProjectDirectory
from dbquery.config import AppConfig, SourceConfig, resolve_paths
from dbquery.errors import DomainError
from dbquery.service import PortableDatabaseService
from dbquery.sources import LocalSourceResolver
from dbquery.types import QueryPage, TableDescription

_ENV_A = "SRC_A_DATABASE_URL"
_ENV_B = "SRC_B_DATABASE_URL"
_CANARY = "canary-password-0001"

_PROJECTS = """
version: 1
projects:
  - {id: p1a, company: 企业, name: 项目一, aliases: [], document: references/p1a.md,
     connection_id: src_a}
  - {id: p2a, company: 企业, name: 未绑定, aliases: [], document: references/p2a.md,
     connection_id: null}
  - {id: p3a, company: 企业, name: 同连接, aliases: [], document: references/p3a.md,
     connection_id: src_a}
  - {id: p4a, company: 企业, name: 另一库, aliases: [], document: references/p4a.md,
     connection_id: src_b}
"""


class SpyEnviron(dict):
    def __init__(self, values: dict[str, str]) -> None:
        super().__init__(values)
        self.accessed: list[str] = []

    def get(self, key, default=None):  # noqa: ANN001, ANN201 - 测试替身
        self.accessed.append(key)
        return super().get(key, default)


class FakeExecutor:
    def __init__(self, page: QueryPage | None = None, error: BaseException | None = None) -> None:
        self.calls: list[tuple[object, object, object]] = []
        self._page = page
        self._error = error

    async def __call__(self, source, validated, limits):  # noqa: ANN001, ANN201
        self.calls.append((source, validated, limits))
        if self._error is not None:
            raise self._error
        return self._page or _page(3)


class RoutingExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, source, validated, limits):  # noqa: ANN001, ANN201
        self.calls.append(source.connection_id)
        if source.connection_id == "src_a":
            return QueryPage(("id", "name"), ((1, "a"), (2, "b")), 2, False, None, None, 1)
        return QueryPage(("id", "amount"), ((1, 10), (2, 20)), 2, False, None, None, 1)


class FakeDescriber:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []

    async def __call__(self, source, table):  # noqa: ANN001, ANN201
        self.calls.append((source, table))
        return TableDescription(
            table=table,
            columns=(),
            primary_key=("id",),
            indexes=(),
            pagination_suitable=True,
            pagination_reason=None,
            elapsed_ms=0,
        )


def _page(rows: int) -> QueryPage:
    values = tuple((index + 1,) for index in range(rows))
    return QueryPage(("id",), values, rows, False, None, None, 1)


def _code(exc: pytest.ExceptionInfo[DomainError]) -> str:
    return exc.value.code


def _source_config(
    source_id: str,
    env: str,
    *,
    enabled: bool = True,
    verified: bool = True,
) -> SourceConfig:
    return SourceConfig(
        id=source_id,
        secret_env=env,
        allowed_schema="sales",
        allowed_tables=("orders", "customers"),
        readonly_account_verified=verified,
        enabled=enabled,
    )


def _build(
    tmp_path: Path,
    *,
    executor=None,  # noqa: ANN001
    describer=None,  # noqa: ANN001
    sources: tuple[SourceConfig, ...] | None = None,
    environ=None,  # noqa: ANN001
) -> SimpleNamespace:
    root = tmp_path / "project-docs"
    references = root / "references"
    references.mkdir(parents=True)
    (references / "projects.yaml").write_text(_PROJECTS, encoding="utf-8")
    for name in ("p1a", "p2a", "p3a", "p4a"):
        (references / f"{name}.md").write_text(f"# {name}", encoding="utf-8")

    source_configs = sources or (
        _source_config("src_a", _ENV_A),
        _source_config("src_b", _ENV_B),
    )
    config = AppConfig(
        version=1,
        project_docs_root=str(root),
        state_dir=str(tmp_path / "state"),
        output_dir=str(tmp_path / "outputs"),
        sources=source_configs,
    )
    paths = resolve_paths(config, tmp_path / "config.yaml")
    catalog = ProjectDirectory(paths.project_docs_root)
    environment = environ or {
        _ENV_A: f"mysql://u:{_CANARY}@host:3306/sales",
        _ENV_B: f"mysql://u:{_CANARY}@host2:3306/sales",
    }
    resolver = LocalSourceResolver(config.sources, environment)
    budget = LocalBudget(paths.state_dir / "budget.sqlite3")
    budget.create_run("run-1")
    service = PortableDatabaseService(
        paths,
        catalog,
        resolver,
        budget,
        executor=executor or FakeExecutor(),
        describer=describer or FakeDescriber(),
    )
    return SimpleNamespace(
        service=service,
        budget=budget,
        paths=paths,
        catalog=catalog,
        executor=executor,
        describer=describer,
        budget_path=paths.state_dir / "budget.sqlite3",
    )


def _lease_count(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0])


async def test_unknown_project_rejected_before_sql(tmp_path: Path) -> None:
    context = _build(tmp_path)

    with pytest.raises(DomainError) as exc:
        await context.service.query("run-1", "missing-project", "SELECT id FROM orders")

    assert _code(exc) == "project.not_found"
    assert context.service._executor.calls == []
    assert context.budget.run_info("run-1").calls == 0


async def test_unbound_project_rejected(tmp_path: Path) -> None:
    context = _build(tmp_path)

    with pytest.raises(DomainError) as exc:
        await context.service.query("run-1", "p2a", "SELECT id FROM orders")

    assert _code(exc) == "database.source_unavailable"
    assert context.service._executor.calls == []


async def test_missing_document_rejected(tmp_path: Path) -> None:
    context = _build(tmp_path)
    (context.paths.project_docs_root / "references" / "p1a.md").unlink()

    with pytest.raises(DomainError) as exc:
        await context.service.query("run-1", "p1a", "SELECT id FROM orders")

    assert _code(exc) == "project.config_unavailable"
    assert context.service._executor.calls == []


@pytest.mark.parametrize(
    ("enabled", "verified"),
    [(False, True), (True, False)],
)
async def test_disabled_or_unverified_source_rejected(
    tmp_path: Path, enabled: bool, verified: bool
) -> None:
    environ = SpyEnviron({_ENV_A: f"mysql://u:{_CANARY}@host:3306/sales"})
    context = _build(
        tmp_path,
        sources=(_source_config("src_a", _ENV_A, enabled=enabled, verified=verified),),
        environ=environ,
    )

    with pytest.raises(DomainError) as exc:
        await context.service.query("run-1", "p1a", "SELECT id FROM orders")

    assert _code(exc) == "database.source_unavailable"
    assert context.service._executor.calls == []
    assert environ.accessed == []


async def test_policy_rejection_happens_before_budget(tmp_path: Path) -> None:
    context = _build(tmp_path)

    with pytest.raises(DomainError) as exc:
        await context.service.query("run-1", "p1a", "DELETE FROM orders")

    assert _code(exc) == "database.sql_not_readonly"
    assert context.service._executor.calls == []
    assert context.budget.run_info("run-1").calls == 0


async def test_query_success_settles_actual_rows_and_releases_lease(tmp_path: Path) -> None:
    executor = FakeExecutor(_page(3))
    context = _build(tmp_path, executor=executor)

    result = await context.service.query("run-1", "p1a", "SELECT id FROM orders")

    assert result.page.returned_rows == 3
    assert context.budget.run_info("run-1").calls == 1
    assert context.budget.run_info("run-1").rows == 3
    assert _lease_count(context.budget_path) == 0
    validated = executor.calls[0][1]
    assert "LIMIT 201" in validated.sql


async def test_query_failure_settles_zero_and_releases_lease(tmp_path: Path) -> None:
    executor = FakeExecutor(error=DomainError("database.query_failed", "查询执行失败", 503))
    context = _build(tmp_path, executor=executor)

    with pytest.raises(DomainError) as exc:
        await context.service.query("run-1", "p1a", "SELECT id FROM orders")

    assert _code(exc) == "database.query_failed"
    assert context.budget.run_info("run-1").calls == 1
    assert context.budget.run_info("run-1").rows == 0
    assert _lease_count(context.budget_path) == 0


async def test_save_true_returns_preview_and_full_file(tmp_path: Path) -> None:
    context = _build(tmp_path, executor=FakeExecutor(_page(30)))

    result = await context.service.query("run-1", "p1a", "SELECT id FROM orders", save_result=True)

    assert result.saved is not None
    assert len(result.page.rows) == 20
    assert result.page.returned_rows == 30
    payload = json.loads(result.saved.path.read_text(encoding="utf-8"))
    assert len(payload["page"]["rows"]) == 30
    assert payload["page"]["returned_rows"] == 30


async def test_save_false_returns_full_page(tmp_path: Path) -> None:
    context = _build(tmp_path, executor=FakeExecutor(_page(30)))

    result = await context.service.query("run-1", "p1a", "SELECT id FROM orders")

    assert result.saved is None
    assert len(result.page.rows) == 30


async def test_save_failure_does_not_reexecute(tmp_path: Path, monkeypatch) -> None:
    executor = FakeExecutor(_page(3))
    context = _build(tmp_path, executor=executor)

    def _broken(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202 - 注入失败
        raise DomainError("database.result_unavailable", "查询结果无法保存", 503)

    monkeypatch.setattr("dbquery.service.save_payload", _broken)

    with pytest.raises(DomainError) as exc:
        await context.service.query("run-1", "p1a", "SELECT id FROM orders", save_result=True)

    assert _code(exc) == "database.result_unavailable"
    assert len(executor.calls) == 1


@pytest.mark.parametrize("max_rows", [0, 1001, True, "200"])
async def test_max_rows_bounds_strict(tmp_path: Path, max_rows: object) -> None:
    context = _build(tmp_path)

    with pytest.raises(DomainError) as exc:
        await context.service.query(
            "run-1",
            "p1a",
            "SELECT id FROM orders",
            max_rows=max_rows,  # type: ignore[arg-type]
        )

    assert _code(exc) == "database.limit_exceeded"
    assert context.service._executor.calls == []


async def test_save_result_must_be_bool(tmp_path: Path) -> None:
    context = _build(tmp_path)

    with pytest.raises(DomainError) as exc:
        await context.service.query(
            "run-1",
            "p1a",
            "SELECT id FROM orders",
            save_result="yes",  # type: ignore[arg-type]
        )

    assert _code(exc) == "database.invalid_argument"


async def test_two_projects_share_one_connection(tmp_path: Path) -> None:
    executor = FakeExecutor(_page(1))
    context = _build(tmp_path, executor=executor)

    first = await context.service.query("run-1", "p1a", "SELECT id FROM orders")
    second = await context.service.query("run-1", "p3a", "SELECT id FROM orders")

    assert first.connection_id == second.connection_id == "src_a"
    assert [call[0].connection_id for call in executor.calls] == ["src_a", "src_a"]


async def test_two_sources_return_rows_for_offline_id_join(tmp_path: Path) -> None:
    executor = RoutingExecutor()
    context = _build(tmp_path, executor=executor)

    first = await context.service.query("run-1", "p1a", "SELECT id, name FROM orders")
    second = await context.service.query("run-1", "p4a", "SELECT id, amount FROM orders")

    assert first.connection_id == "src_a"
    assert second.connection_id == "src_b"
    joined = {
        row[0]: (row[1], second.page.rows[index][1]) for index, row in enumerate(first.page.rows)
    }
    assert joined == {1: ("a", 10), 2: ("b", 20)}


async def test_list_tables_returns_whitelist_and_counts_call(tmp_path: Path) -> None:
    context = _build(tmp_path)

    tables = context.service.list_tables("run-1", "p1a")

    assert tables.tables == ("orders", "customers")
    assert context.budget.run_info("run-1").calls == 1
    assert context.budget.run_info("run-1").rows == 0


async def test_describe_table_rejects_unauthorized_table(tmp_path: Path) -> None:
    describer = FakeDescriber()
    context = _build(tmp_path, describer=describer)

    with pytest.raises(DomainError) as exc:
        await context.service.describe("run-1", "p1a", "secret_table")

    assert _code(exc) == "database.table_not_allowed"
    assert describer.calls == []
    assert context.budget.run_info("run-1").calls == 0


async def test_describe_table_success_uses_lease(tmp_path: Path) -> None:
    describer = FakeDescriber()
    context = _build(tmp_path, describer=describer)

    described = await context.service.describe("run-1", "p1a", "orders")

    assert described.description.table == "orders"
    assert context.budget.run_info("run-1").calls == 1
    assert _lease_count(context.budget_path) == 0


async def test_saved_payload_has_utc_queried_at(tmp_path: Path) -> None:
    context = _build(tmp_path, executor=FakeExecutor(_page(1)))

    result = await context.service.query("run-1", "p1a", "SELECT id FROM orders", save_result=True)

    assert result.queried_at.tzinfo is not None
    assert result.queried_at.astimezone(timezone.utc).year >= 2026
    assert isinstance(result.queried_at, datetime)
