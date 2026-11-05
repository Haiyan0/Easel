"""离开本仓库的外部项目端到端：复制技能、中文空格路径、其他 cwd。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
_CANARY = "canary-e2e-password-0001"
_FAKE_URL = f"mysql://probe:{_CANARY}@127.0.0.1:59999/sales"

_PROJECTS = """
version: 1
projects:
  - {id: p1a, company: 企业, name: 项目一, aliases: [], document: references/p1a.md,
     connection_id: sales_readonly}
  - {id: p2a, company: 企业, name: 未绑定, aliases: [], document: references/p2a.md,
     connection_id: null}
"""

_CONFIG = """
version: 1
project_docs_root: ./project-docs
state_dir: ./state
output_dir: ./outputs
sources:
  - id: sales_readonly
    secret_env: SALES_DATABASE_URL
    allowed_schema: sales
    allowed_tables: [orders, customers]
    readonly_account_verified: true
    enabled: true
"""


def _copy_and_configure(tmp_path: Path) -> Path:
    target = tmp_path / "外部 项目" / "database-query"
    shutil.copytree(
        SKILL_ROOT,
        target,
        ignore=shutil.ignore_patterns(
            "state", "outputs", "__pycache__", ".pytest_cache", "config.yaml"
        ),
    )
    references = target / "project-docs" / "references"
    references.mkdir(parents=True)
    (references / "projects.yaml").write_text(_PROJECTS, encoding="utf-8")
    (references / "p1a.md").write_text("# p1a", encoding="utf-8")
    (references / "p2a.md").write_text("# p2a", encoding="utf-8")
    (target / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    return target


def _run(
    target: Path,
    tmp_path: Path,
    *args: str,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": "", "SALES_DATABASE_URL": _FAKE_URL}
    return subprocess.run(
        [
            sys.executable,
            str(target / "scripts" / "db_query.py"),
            "--config",
            str(target / "config.yaml"),
            *args,
        ],
        cwd=tmp_path,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_copied_skill_runs_without_repository(tmp_path: Path) -> None:
    target = _copy_and_configure(tmp_path)

    doctor = _run(target, tmp_path, "doctor")
    assert doctor.returncode == 0, doctor.stderr
    assert json.loads(doctor.stdout)["ok"] is True

    listed = _run(target, tmp_path, "projects", "list")
    assert listed.returncode == 0, listed.stderr
    assert [item["id"] for item in json.loads(listed.stdout)["projects"]] == ["p1a", "p2a"]

    resolved = _run(target, tmp_path, "projects", "resolve", "--project", "p1a")
    assert resolved.returncode == 0, resolved.stderr
    document = Path(json.loads(resolved.stdout)["project"]["document_path"])
    assert document.is_file()
    assert document.is_relative_to((target / "project-docs").resolve())

    created = _run(target, tmp_path, "runs", "new", "--run-id", "run-e2e")
    assert created.returncode == 0, created.stderr

    tables = _run(target, tmp_path, "tables", "--run-id", "run-e2e", "--project", "p1a")
    assert tables.returncode == 0, tables.stderr
    assert json.loads(tables.stdout)["tables"] == ["orders", "customers"]


def test_unbound_project_query_stops_before_connect(tmp_path: Path) -> None:
    target = _copy_and_configure(tmp_path)
    assert _run(target, tmp_path, "runs", "new", "--run-id", "run-e2e").returncode == 0

    result = _run(
        target,
        tmp_path,
        "query",
        "--run-id",
        "run-e2e",
        stdin=json.dumps({"project_id": "p2a", "sql": "SELECT id FROM orders"}),
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["code"] == "database.source_unavailable"
    assert _CANARY not in result.stdout + result.stderr
