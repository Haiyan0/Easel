"""项目→连接→策略→预算→执行→保存的本地编排。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from dbquery.budget import Lease, LocalBudget, Reservation
from dbquery.catalog import ProjectDirectory, ProjectEntry
from dbquery.config import AppPaths
from dbquery.errors import DomainError
from dbquery.mysql import (
    QueryLimits,
    check_connection,
    describe_table,
    execute_readonly,
)
from dbquery.policy import (
    DATABASE_LIMIT_EXCEEDED,
    DATABASE_TABLE_NOT_ALLOWED,
    LIMIT_EXCEEDED_TEXT,
    TABLE_NOT_ALLOWED_TEXT,
    ValidatedQuery,
    validate_query,
)
from dbquery.results import SavedResult, build_payload, preview_rows, save_payload
from dbquery.sources import LocalSourceResolver
from dbquery.types import QueryPage, ResolvedSource, TableDescription, source_unavailable

Executor = Callable[[ResolvedSource, ValidatedQuery, QueryLimits], Awaitable[QueryPage]]
Describer = Callable[[ResolvedSource, str], Awaitable[TableDescription]]
Connector = Callable[[ResolvedSource], Awaitable[str]]

logger = logging.getLogger(__name__)

PREVIEW_ROWS = 20


@dataclass(frozen=True)
class AuthorizedTables:
    project_id: str
    connection_id: str
    config_summary: str
    tables: tuple[str, ...]


@dataclass(frozen=True)
class ProjectTableDescription:
    project_id: str
    connection_id: str
    config_summary: str
    description: TableDescription


@dataclass(frozen=True)
class DatabaseQueryResult:
    project_id: str
    connection_id: str
    config_summary: str
    page: QueryPage
    saved: SavedResult | None
    queried_at: datetime


class PortableDatabaseService:
    """本地只读查询编排；不依赖平台会话、技能授权或 Redis。"""

    def __init__(
        self,
        paths: AppPaths,
        catalog: ProjectDirectory,
        resolver: LocalSourceResolver,
        budget: LocalBudget,
        *,
        executor: Executor = execute_readonly,
        describer: Describer = describe_table,
        connector: Connector = check_connection,
    ) -> None:
        self._paths = paths
        self._catalog = catalog
        self._resolver = resolver
        self._budget = budget
        self._executor = executor
        self._describer = describer
        self._connector = connector

    def _source(self, project_id: str) -> tuple[ProjectEntry, ResolvedSource]:
        """项目 → 文档 → 连接绑定 → 数据源；任何一步失败都不执行 SQL。"""
        project = self._catalog.resolve(project_id)
        self._catalog.document_path(project)
        if project.connection_id is None:
            raise source_unavailable("source_not_configured", project_id=project.id)
        source = self._resolver.resolve(project.connection_id)
        return project, source

    def list_tables(self, run_id: str, project_id: str) -> AuthorizedTables:
        project, source = self._source(project_id)
        reservation = self._budget.reserve(run_id, 0)
        self._cleanup(reservation, 0, None)
        return AuthorizedTables(
            project.id, source.connection_id, source.config_summary, source.allowed_tables
        )

    async def describe(self, run_id: str, project_id: str, table: str) -> ProjectTableDescription:
        project, source = self._source(project_id)
        if table not in source.allowed_tables:
            raise DomainError(DATABASE_TABLE_NOT_ALLOWED, TABLE_NOT_ALLOWED_TEXT, status=400)
        reservation = self._budget.reserve(run_id, 0)
        lease: Lease | None = None
        try:
            lease = self._budget.acquire(self._budget.target_key(source), run_id)
            description = await self._describer(source, table)
        except BaseException:
            self._cleanup(reservation, 0, lease)
            raise
        self._cleanup(reservation, 0, lease)
        return ProjectTableDescription(
            project.id, source.connection_id, source.config_summary, description
        )

    async def query(
        self,
        run_id: str,
        project_id: str,
        sql: str,
        parameters: Mapping[str, object] | None = None,
        max_rows: int = 200,
        save_result: bool = False,
    ) -> DatabaseQueryResult:
        if type(max_rows) is not int or not 1 <= max_rows <= 1000:
            raise DomainError(DATABASE_LIMIT_EXCEEDED, LIMIT_EXCEEDED_TEXT, status=400)
        if type(save_result) is not bool:
            raise DomainError("database.invalid_argument", "查询参数无效", status=400)
        if parameters is not None and not isinstance(parameters, Mapping):
            raise DomainError("database.invalid_argument", "查询参数无效", status=400)
        project, source = self._source(project_id)
        bound_parameters = dict(parameters or {})
        validated = validate_query(
            sql,
            bound_parameters,
            source.allowed_schema,
            source.allowed_tables,
            max_rows + 1,
        )
        queried_at = datetime.now(timezone.utc)
        reservation = self._budget.reserve(run_id, max_rows)
        lease: Lease | None = None
        page: QueryPage | None = None
        failure: BaseException | None = None
        rows = 0
        try:
            lease = self._budget.acquire(self._budget.target_key(source), run_id)
            page = await self._executor(source, validated, QueryLimits(max_rows=max_rows))
            rows = page.returned_rows
        except BaseException as exc:  # noqa: BLE001 —— 失败先结算/释放额度，再外泄原错误
            failure = exc
        finally:
            self._cleanup(reservation, rows, lease)
        if failure is not None:
            raise failure
        assert page is not None
        saved: SavedResult | None = None
        if save_result:
            payload = build_payload(
                project.id,
                source.connection_id,
                queried_at,
                sql,
                bound_parameters,
                page,
            )
            saved = save_payload(self._paths.output_dir, payload)
            page = replace(page, rows=preview_rows(page, PREVIEW_ROWS))
        return DatabaseQueryResult(
            project.id, source.connection_id, source.config_summary, page, saved, queried_at
        )

    async def check(self, project_id: str) -> str:
        """``doctor --connect`` 的连接检查；不消耗查询预算。"""
        _project, source = self._source(project_id)
        return await self._connector(source)

    def _cleanup(self, reservation: Reservation, rows: int, lease: Lease | None) -> None:
        """结算与释放失败只记日志：不掩盖主流程结果，额度按保守方向保留。"""
        try:
            self._budget.settle(reservation, rows)
        except DomainError:
            logger.warning("数据库预算结算失败")
        finally:
            if lease is not None:
                try:
                    self._budget.release(lease)
                except DomainError:
                    logger.warning("数据库租约释放失败")
