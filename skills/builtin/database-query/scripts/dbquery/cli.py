"""本地 CLI：argparse 子命令、JSON I/O 与脱敏诊断。

stdout 只输出一个 JSON 对象；stderr 只输出 ``code: message``。退出码：
0 成功、1 领域错误、2 用法/请求格式错误。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import logging
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence, cast
from uuid import uuid4

from dbquery.budget import RUN_TTL_SECONDS, LocalBudget
from dbquery.catalog import ProjectDirectory
from dbquery.config import AppConfig, AppPaths, load_env_file, resolve_paths
from dbquery.errors import DomainError
from dbquery.redaction import install_redaction
from dbquery.results import build_payload
from dbquery.service import PortableDatabaseService
from dbquery.sources import LocalSourceResolver

logger = logging.getLogger(__name__)

_MAX_REQUEST_BYTES = 64 * 1024
_ALLOWED_REQUEST_KEYS = {"project_id", "sql", "parameters", "max_rows", "save_result"}
_DEPENDENCIES = ("pydantic", "PyYAML", "SQLAlchemy", "asyncmy", "sqlglot")
_SECRETS: set[str] = set()


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    """CLI 入口；测试可注入 ``argv`` 与 ``environ``。"""
    _configure_stdio()
    _SECRETS.clear()
    environment = dict(os.environ if environ is None else environ)
    install_redaction(lambda: tuple(_SECRETS))
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        return _dispatch(args, environment)
    except DomainError as exc:
        _print_json({"error": {"code": exc.code, "message": exc.message}})
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 2 if exc.code == "cli.invalid_request" else 1
    except Exception as exc:  # noqa: BLE001 —— 不把堆栈与取值暴露给调用方
        logger.error("内部错误：%s", type(exc).__name__)
        _print_json({"error": {"code": "internal.error", "message": "内部错误"}})
        print("internal.error: 内部错误", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="db_query.py", description="项目目录驱动的受控只读数据库查询"
    )
    parser.add_argument("--config", help="本地 YAML 配置路径")
    parser.add_argument("--env-file", help="显式 KEY=VALUE 环境文件；不做变量展开")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="检查配置、依赖、绑定、目录与连接")
    doctor.add_argument("--connect", action="store_true", help="执行真实连接检查")
    doctor.add_argument("--project", help="--connect 时的项目 ID")

    projects = commands.add_parser("projects", help="项目目录")
    project_commands = projects.add_subparsers(dest="projects_command", required=True)
    project_commands.add_parser("list", help="列出项目")
    resolve = project_commands.add_parser("resolve", help="解析精确项目 ID")
    resolve.add_argument("--project", required=True)

    tables = commands.add_parser("tables", help="列出授权表白名单")
    tables.add_argument("--run-id", required=True)
    tables.add_argument("--project", required=True)

    describe = commands.add_parser("describe", help="描述已授权表结构")
    describe.add_argument("--run-id", required=True)
    describe.add_argument("--project", required=True)
    describe.add_argument("--table", required=True)

    query = commands.add_parser("query", help="执行受控只读查询")
    query.add_argument("--run-id", required=True)
    query.add_argument("--request", help="JSON 请求文件；缺省从 stdin 读取")

    runs = commands.add_parser("runs", help="本地运行预算")
    run_commands = runs.add_subparsers(dest="runs_command", required=True)
    new_run = run_commands.add_parser("new", help="显式新建一个运行")
    new_run.add_argument("--run-id")
    new_run.add_argument("--ttl-seconds", type=int, default=RUN_TTL_SECONDS)
    show_run = run_commands.add_parser("show", help="查看运行计数")
    show_run.add_argument("--run-id", required=True)
    return parser


def _dispatch(args: argparse.Namespace, environ: dict[str, str]) -> int:
    config_path, environment = _load_environment(args, environ)
    config = AppConfig.load(config_path)
    paths = resolve_paths(config, config_path)
    _register_secrets(config, environment)
    directory = ProjectDirectory(paths.project_docs_root)

    if args.command == "doctor":
        return _doctor(args, config, paths, directory, environment)
    if args.command == "projects":
        return _projects(args, directory)
    if args.command == "runs":
        return _runs(args, paths)

    budget = LocalBudget(paths.state_dir / "budget.sqlite3")
    if args.command == "tables":
        service = _build_service(config, paths, directory, budget, environment)
        tables = service.list_tables(args.run_id, args.project)
        _print_json(
            {
                "version": 1,
                "project_id": tables.project_id,
                "connection_id": tables.connection_id,
                "config_summary": tables.config_summary,
                "tables": list(tables.tables),
            }
        )
        return 0
    if args.command == "describe":
        service = _build_service(config, paths, directory, budget, environment)
        described = asyncio.run(service.describe(args.run_id, args.project, args.table))
        _print_json(
            {
                "version": 1,
                "project_id": described.project_id,
                "connection_id": described.connection_id,
                "config_summary": described.config_summary,
                "description": asdict(described.description),
            }
        )
        return 0
    if args.command == "query":
        service = _build_service(config, paths, directory, budget, environment)
        request = _load_request(args.request)
        result = asyncio.run(
            service.query(
                args.run_id,
                request["project_id"],
                request["sql"],
                parameters=request["parameters"],
                max_rows=request["max_rows"],
                save_result=request["save_result"],
            )
        )
        response = build_payload(
            result.project_id,
            result.connection_id,
            result.queried_at,
            request["sql"],
            request["parameters"],
            result.page,
        )
        response["saved"] = (
            None
            if result.saved is None
            else {
                "path": str(result.saved.path),
                "bytes": result.saved.bytes,
                "preview_rows": len(result.page.rows),
            }
        )
        _print_json(response)
        return 0
    raise DomainError("cli.invalid_request", "未知子命令", status=400)


def _load_environment(
    args: argparse.Namespace, environ: dict[str, str]
) -> tuple[Path, dict[str, str]]:
    environment = dict(environ)
    if args.env_file:
        environment.update(load_env_file(Path(args.env_file)))
    raw = args.config or environment.get("DATABASE_QUERY_CONFIG")
    config_path = Path(raw) if raw else _default_config_path()
    if not config_path.is_file():
        raise DomainError("config.unavailable", "本地配置不可用（未找到配置文件）", status=503)
    return config_path, environment


def _default_config_path() -> Path:
    # cli.py → dbquery → scripts → 技能根目录
    return Path(__file__).resolve().parents[2] / "config.yaml"


def _register_secrets(config: AppConfig, environ: Mapping[str, str]) -> None:
    for source in config.sources:
        value = environ.get(source.secret_env)
        if value:
            _SECRETS.add(value)


def _build_service(
    config: AppConfig,
    paths: AppPaths,
    directory: ProjectDirectory,
    budget: LocalBudget,
    environ: Mapping[str, str],
) -> PortableDatabaseService:
    resolver = LocalSourceResolver(config.sources, environ)
    return PortableDatabaseService(paths, directory, resolver, budget)


def _projects(args: argparse.Namespace, directory: ProjectDirectory) -> int:
    if args.projects_command == "list":
        _print_json(
            {
                "version": 1,
                "projects": [
                    _project_payload(project, document_path=None) for project in directory.list()
                ],
            }
        )
        return 0
    project = directory.resolve(args.project)
    document = directory.document_path(project)
    _print_json({"version": 1, "project": _project_payload(project, document_path=str(document))})
    return 0


def _project_payload(project: Any, *, document_path: str | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": project.id,
        "company": project.company,
        "name": project.name,
        "aliases": list(project.aliases),
        "document": project.document,
        "connection_id": project.connection_id,
    }
    if document_path is not None:
        payload["document_path"] = document_path
    return payload


def _runs(args: argparse.Namespace, paths: AppPaths) -> int:
    budget = LocalBudget(paths.state_dir / "budget.sqlite3")
    if args.runs_command == "new":
        run_id = args.run_id or f"run-{uuid4().hex[:12]}"
        info = budget.create_run(run_id, ttl_seconds=args.ttl_seconds)
        _print_json(
            {
                "run_id": info.run_id,
                "deadline": info.deadline,
                "calls": info.calls,
                "rows": info.rows,
            }
        )
        return 0
    info = budget.run_info(args.run_id)
    _print_json(
        {
            "run_id": info.run_id,
            "deadline": info.deadline,
            "calls": info.calls,
            "rows": info.rows,
            "expired": info.deadline <= int(time.time()),
        }
    )
    return 0


def _doctor(
    args: argparse.Namespace,
    config: AppConfig,
    paths: AppPaths,
    directory: ProjectDirectory,
    environ: Mapping[str, str],
) -> int:
    checks: list[dict[str, object]] = []
    checks.append(
        _check("config", True, f"version={config.version}, sources={len(config.sources)}")
    )
    checks.append(_dependencies_check())
    projects: tuple[Any, ...] = ()
    try:
        projects = directory.list()
        checks.append(_check("project_docs", True, f"projects={len(projects)}"))
    except DomainError as exc:
        checks.append(_check("project_docs", False, exc.code))
    missing_bindings = [
        project.id
        for project in projects
        if project.connection_id is not None and config.get(project.connection_id) is None
    ]
    checks.append(
        _check(
            "bindings",
            not missing_bindings,
            "all bound" if not missing_bindings else ",".join(missing_bindings),
        )
    )
    states = [
        f"{source.id}:{'enabled' if source.enabled else 'disabled'}"
        + ("" if source.readonly_account_verified else ",unverified")
        for source in config.sources
    ]
    checks.append(_check("sources", True, ", ".join(states) or "none"))
    writable = _writable(paths.state_dir) and _writable(paths.output_dir)
    checks.append(_check("directories", writable, "state/output writable"))
    if args.connect:
        if not args.project:
            raise DomainError("cli.invalid_request", "参数无效（缺少 --project）", status=400)
        checks.append(_connection_check(args.project, config, directory, paths, environ))
    ok = all(bool(check["ok"]) for check in checks)
    _print_json({"ok": ok, "checks": checks})
    return 0 if ok else 1


def _dependencies_check() -> dict[str, object]:
    versions: list[str] = []
    try:
        for name in _DEPENDENCIES:
            versions.append(f"{name}={importlib.metadata.version(name)}")
    except importlib.metadata.PackageNotFoundError:
        return _check("dependencies", False, "missing dependency")
    return _check("dependencies", True, ", ".join(versions))


def _connection_check(
    project_id: str,
    config: AppConfig,
    directory: ProjectDirectory,
    paths: AppPaths,
    environ: Mapping[str, str],
) -> dict[str, object]:
    try:
        project = directory.resolve(project_id)
        expected = (
            None
            if project.connection_id is None
            else getattr(config.get(project.connection_id), "allowed_schema", None)
        )
        budget = LocalBudget(paths.state_dir / "budget.sqlite3")
        service = _build_service(config, paths, directory, budget, environ)
        actual = asyncio.run(service.check(project_id))
    except DomainError as exc:
        return _check("connection", False, exc.code)
    return _check("connection", actual == expected, f"schema={actual}")


def _writable(directory: Path) -> bool:
    probe = directory / f".probe-{uuid4().hex}"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _check(name: str, ok: bool, detail: str) -> dict[str, object]:
    return {"name": name, "ok": ok, "detail": detail}


def _load_request(request_path: str | None) -> dict[str, Any]:
    try:
        if request_path:
            raw_bytes = Path(request_path).read_bytes()
        else:
            raw_bytes = _read_stdin_bytes()
    except OSError:
        raise _invalid_request() from None
    if len(raw_bytes) > _MAX_REQUEST_BYTES:
        raise _invalid_request()
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _invalid_request() from None
    if not isinstance(data, dict) or set(data) - _ALLOWED_REQUEST_KEYS:
        raise _invalid_request()
    project_id = data.get("project_id")
    sql = data.get("sql")
    parameters = data.get("parameters", {})
    max_rows = data.get("max_rows", 200)
    save_result = data.get("save_result", False)
    if not isinstance(project_id, str) or not project_id or not isinstance(sql, str) or not sql:
        raise _invalid_request()
    if not isinstance(parameters, dict):
        raise _invalid_request()
    if type(max_rows) is not int or type(save_result) is not bool:
        raise _invalid_request()
    return {
        "project_id": project_id,
        "sql": sql,
        "parameters": parameters,
        "max_rows": max_rows,
        "save_result": save_result,
    }


def _read_stdin_bytes() -> bytes:
    """按原始字节读取 stdin，固定用 UTF-8 解码。

    不读 ``sys.stdin.read()`` 的文本结果：中文 Windows 的 stdin 默认按本地代码页
    （如 GBK）解码，UTF-8 请求会被静默改写或直接抛错。真实进程有 ``buffer``；
    pytest 的 StringIO 没有，退回文本读取后按 UTF-8 编码。
    """
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        return cast(bytes, buffer.read(_MAX_REQUEST_BYTES + 1))
    return sys.stdin.read(_MAX_REQUEST_BYTES + 1).encode("utf-8")


def _invalid_request() -> DomainError:
    return DomainError("cli.invalid_request", "请求格式无效", status=400)


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError):
            pass


def _print_json(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))
