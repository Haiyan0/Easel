"""CLI 子命令、JSON 契约、退出码与脱敏。"""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dbquery.cli import main
from dbquery.errors import DomainError
from dbquery.results import SavedResult
from dbquery.service import AuthorizedTables, DatabaseQueryResult, ProjectTableDescription
from dbquery.types import QueryPage, TableDescription

_CANARY_URL = "mysql://u:canary-password-0001@host:3306/sales"

_PROJECTS = """
version: 1
projects:
  - {id: p1a, company: 企业, name: 项目一, aliases: [], document: references/p1a.md,
     connection_id: src_a}
  - {id: p2a, company: 企业, name: 未绑定, aliases: [], document: references/p2a.md,
     connection_id: null}
"""


class FakeService:
    def __init__(self, *, error: BaseException | None = None, saved: SavedResult | None = None):
        self.error = error
        self.saved = saved
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def list_tables(self, run_id: str, project_id: str) -> AuthorizedTables:
        self.calls.append(("list_tables", (run_id, project_id), {}))
        if self.error is not None:
            raise self.error
        return AuthorizedTables(project_id, "src_a", "摘要", ("orders", "customers"))

    async def describe(self, run_id: str, project_id: str, table: str):
        self.calls.append(("describe", (run_id, project_id, table), {}))
        if self.error is not None:
            raise self.error
        return ProjectTableDescription(
            project_id,
            "src_a",
            "摘要",
            TableDescription(table, (), ("id",), (), True, None, 0),
        )

    async def query(self, run_id: str, project_id: str, sql: str, **kwargs):
        self.calls.append(("query", (run_id, project_id, sql), kwargs))
        if self.error is not None:
            raise self.error
        return DatabaseQueryResult(
            project_id,
            "src_a",
            "摘要",
            QueryPage(("id",), ((1,),), 1, False, None, None, 1),
            self.saved,
            datetime.now(timezone.utc),
        )

    async def check(self, project_id: str) -> str:
        self.calls.append(("check", (project_id,), {}))
        if self.error is not None:
            raise self.error
        return "sales"


class LeakyService(FakeService):
    async def query(self, run_id: str, project_id: str, sql: str, **kwargs):
        logging.getLogger("dbquery.test").warning("connecting %s", _CANARY_URL)
        raise DomainError("database.source_unavailable", "数据源不存在或未授权", 404)


def _write_context(tmp_path: Path, *, enabled: bool = True) -> Path:
    root = tmp_path / "project-docs"
    references = root / "references"
    references.mkdir(parents=True)
    (references / "projects.yaml").write_text(_PROJECTS, encoding="utf-8")
    (references / "p1a.md").write_text("# p1a", encoding="utf-8")
    (references / "p2a.md").write_text("# p2a", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""
version: 1
project_docs_root: ./project-docs
state_dir: ./state
output_dir: ./outputs
sources:
  - id: src_a
    secret_env: SRC_A_DATABASE_URL
    allowed_schema: sales
    allowed_tables: [orders, customers]
    readonly_account_verified: {str(enabled).lower()}
    enabled: {str(enabled).lower()}
""".lstrip(),
        encoding="utf-8",
    )
    return config


def _run(config: Path, *args: str, environ: dict[str, str] | None = None) -> int:
    return main(["--config", str(config), *args], environ=environ or {})


def _payload(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    out, _ = capsys.readouterr()
    return json.loads(out)


def test_projects_list_and_resolve_json(tmp_path: Path, capsys) -> None:
    config = _write_context(tmp_path)

    assert _run(config, "projects", "list") == 0
    listed = _payload(capsys)
    assert [item["id"] for item in listed["projects"]] == ["p1a", "p2a"]

    assert _run(config, "projects", "resolve", "--project", "p1a") == 0
    resolved = _payload(capsys)
    document = Path(resolved["project"]["document_path"])
    assert document.is_file()
    assert document.parent == (tmp_path / "project-docs" / "references").resolve()


def test_projects_resolve_missing_document_fails(tmp_path: Path, capsys) -> None:
    config = _write_context(tmp_path)
    (tmp_path / "project-docs" / "references" / "p1a.md").unlink()

    assert _run(config, "projects", "resolve", "--project", "p1a") == 1
    error = _payload(capsys)["error"]
    assert error["code"] == "project.config_unavailable"


def test_tables_returns_whitelist(tmp_path: Path, capsys, monkeypatch) -> None:
    config = _write_context(tmp_path)
    service = FakeService()
    monkeypatch.setattr("dbquery.cli._build_service", lambda *args, **kwargs: service)

    assert _run(config, "tables", "--run-id", "run-1", "--project", "p1a") == 0
    payload = _payload(capsys)

    assert payload["tables"] == ["orders", "customers"]
    assert service.calls[0][0] == "list_tables"


def test_query_reads_request_from_stdin(tmp_path: Path, capsys, monkeypatch) -> None:
    config = _write_context(tmp_path)
    service = FakeService()
    monkeypatch.setattr("dbquery.cli._build_service", lambda *args, **kwargs: service)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"project_id": "p1a", "sql": "SELECT id FROM orders"})),
    )

    assert _run(config, "query", "--run-id", "run-1") == 0
    payload = _payload(capsys)

    assert payload["version"] == 1
    assert payload["project_id"] == "p1a"
    assert payload["page"]["rows"] == [[1]]
    assert service.calls[0][2]["max_rows"] == 200


def test_query_reads_request_file(tmp_path: Path, capsys, monkeypatch) -> None:
    config = _write_context(tmp_path)
    service = FakeService()
    monkeypatch.setattr("dbquery.cli._build_service", lambda *args, **kwargs: service)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "project_id": "p1a",
                "sql": "SELECT id FROM orders WHERE id = :id",
                "parameters": {"id": 1},
                "max_rows": 5,
                "save_result": False,
            }
        ),
        encoding="utf-8",
    )

    assert _run(config, "query", "--run-id", "run-1", "--request", str(request)) == 0
    _payload(capsys)

    assert service.calls[0][2]["parameters"] == {"id": 1}
    assert service.calls[0][2]["max_rows"] == 5


@pytest.mark.parametrize(
    "request_text",
    [
        "{",
        json.dumps({"project_id": "p1a", "sql": "SELECT 1", "url": _CANARY_URL}),
        json.dumps({"project_id": "p1a", "sql": "SELECT 1", "max_rows": "200"}),
        json.dumps({"project_id": "p1a", "sql": 1}),
    ],
)
def test_invalid_request_is_exit_2(tmp_path: Path, capsys, monkeypatch, request_text: str) -> None:
    config = _write_context(tmp_path)
    monkeypatch.setattr("dbquery.cli._build_service", lambda *args, **kwargs: FakeService())
    monkeypatch.setattr("sys.stdin", io.StringIO(request_text))

    assert _run(config, "query", "--run-id", "run-1") == 2
    out, err = capsys.readouterr()
    payload = json.loads(out)
    assert payload["error"]["code"] == "cli.invalid_request"
    assert _CANARY_URL not in out + err


def test_domain_error_is_exit_1_with_stable_code(tmp_path: Path, capsys, monkeypatch) -> None:
    config = _write_context(tmp_path)
    service = FakeService(error=DomainError("project.not_found", "项目不存在或未建档", 404))
    monkeypatch.setattr("dbquery.cli._build_service", lambda *args, **kwargs: service)

    assert _run(config, "tables", "--run-id", "run-1", "--project", "missing") == 1
    out, err = capsys.readouterr()
    payload = json.loads(out)
    assert payload["error"]["code"] == "project.not_found"
    assert "project.not_found" in err


def test_query_error_does_not_echo_secret(tmp_path: Path, capsys, monkeypatch) -> None:
    config = _write_context(tmp_path)
    monkeypatch.setattr(
        "dbquery.cli._build_service", lambda *args, **kwargs: LeakyService(error=None)
    )
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"project_id": "p1a", "sql": "SELECT id FROM orders"})),
    )
    # 用自有 root handler 捕获真实日志输出；pytest 默认 handler 不写 stderr，
    # 否则这条用例只验证了静态错误文案，验证不到脱敏接线。
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    old_handlers, old_filters, old_level = root.handlers[:], root.filters[:], root.level
    root.handlers = [handler]
    root.filters = []
    try:
        assert (
            _run(
                config,
                "query",
                "--run-id",
                "run-1",
                environ={"SRC_A_DATABASE_URL": _CANARY_URL},
            )
            == 1
        )
    finally:
        root.handlers = old_handlers
        root.filters = old_filters
        root.setLevel(old_level)
    out, err = capsys.readouterr()
    logged = stream.getvalue()

    assert "canary-password-0001" not in out + err + logged
    assert "mysql://u:***@host:3306/sales" in logged


def test_runs_new_and_show(tmp_path: Path, capsys) -> None:
    config = _write_context(tmp_path)

    assert _run(config, "runs", "new", "--run-id", "run-cli", "--ttl-seconds", "60") == 0
    created = _payload(capsys)
    assert created["run_id"] == "run-cli"

    assert _run(config, "runs", "show", "--run-id", "run-cli") == 0
    shown = _payload(capsys)
    assert shown["calls"] == 0
    assert shown["rows"] == 0

    assert _run(config, "runs", "show", "--run-id", "missing") == 1
    assert _payload(capsys)["error"]["code"] == "database.run_not_found"


def test_doctor_local_checks(tmp_path: Path, capsys) -> None:
    config = _write_context(tmp_path, enabled=False)

    assert _run(config, "doctor") == 0
    payload = _payload(capsys)

    checks = {check["name"]: check for check in payload["checks"]}
    assert payload["ok"] is True
    assert set(checks) == {
        "config",
        "dependencies",
        "project_docs",
        "bindings",
        "sources",
        "directories",
    }
    assert checks["sources"]["ok"] is True
    assert "disabled" in checks["sources"]["detail"]


def test_doctor_connect_requires_project(tmp_path: Path, capsys) -> None:
    config = _write_context(tmp_path)

    assert _run(config, "doctor", "--connect") == 2
    out, _ = capsys.readouterr()
    assert json.loads(out)["error"]["code"] == "cli.invalid_request"


def test_doctor_connect_uses_connector(tmp_path: Path, capsys, monkeypatch) -> None:
    config = _write_context(tmp_path)
    service = FakeService()
    monkeypatch.setattr("dbquery.cli._build_service", lambda *args, **kwargs: service)

    assert _run(config, "doctor", "--connect", "--project", "p1a") == 0
    payload = _payload(capsys)

    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["connection"]["ok"] is True
    assert service.calls[-1][0] == "check"


def test_saved_result_is_reported(tmp_path: Path, capsys, monkeypatch) -> None:
    config = _write_context(tmp_path)
    saved = SavedResult(tmp_path / "outputs" / "query-fixed.json", 123)
    service = FakeService(saved=saved)
    monkeypatch.setattr("dbquery.cli._build_service", lambda *args, **kwargs: service)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps({"project_id": "p1a", "sql": "SELECT id FROM orders", "save_result": True})
        ),
    )

    assert _run(config, "query", "--run-id", "run-1") == 0
    payload = _payload(capsys)

    assert payload["saved"]["path"].endswith("query-fixed.json")
    assert payload["saved"]["bytes"] == 123


def test_help_lists_all_subcommands(capsys) -> None:
    assert main(["--help"]) == 0
    out, _ = capsys.readouterr()

    for command in ("doctor", "projects", "tables", "describe", "query", "runs"):
        assert command in out


class _TextStdin:
    """模拟中文 Windows 的文本 stdin：文本流按本地代码页解码，buffer 保留原始字节。"""

    def __init__(self, payload: bytes, encoding: str = "gbk") -> None:
        self._wrapper = io.TextIOWrapper(io.BytesIO(payload), encoding=encoding)
        self.buffer = self._wrapper.buffer

    def read(self, size: int = -1) -> str:
        return self._wrapper.read(size)


def test_utf8_request_from_stdin_is_not_mangled_by_local_codepage(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config = _write_context(tmp_path)
    service = FakeService()
    monkeypatch.setattr("dbquery.cli._build_service", lambda *args, **kwargs: service)
    payload = json.dumps(
        {"project_id": "p1a", "sql": "SELECT '北京' AS city"}, ensure_ascii=False
    ).encode("utf-8")
    monkeypatch.setattr("sys.stdin", _TextStdin(payload, encoding="gbk"))

    assert _run(config, "query", "--run-id", "run-1") == 0
    _payload(capsys)

    assert service.calls[0][1][2] == "SELECT '北京' AS city"


def test_invalid_utf8_stdin_is_exit_2(tmp_path: Path, capsys, monkeypatch) -> None:
    config = _write_context(tmp_path)
    monkeypatch.setattr("dbquery.cli._build_service", lambda *args, **kwargs: FakeService())
    monkeypatch.setattr("sys.stdin", _TextStdin(b"\xff\xfe\x00\x00", encoding="gbk"))

    assert _run(config, "query", "--run-id", "run-1") == 2
    out, _ = capsys.readouterr()
    assert json.loads(out)["error"]["code"] == "cli.invalid_request"
