"""新增四个 skill 的 Easel 适配验收：规范、工具映射、隐私与依赖。"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = PROJECT_ROOT / "skills" / "builtin"
ADDED_SKILLS = ["data-analysis", "database-query", "geo-content-sop", "project-docs"]
EXPECTED_LAYERS = {
    "data-analysis": "attribute",
    "database-query": "general",
    "geo-content-sop": "produce",
    "project-docs": "general",
}
# 原 smart_assistant 平台工具名，适配后不应再出现在给模型阅读的文档里。
FORBIDDEN_TOOL_TOKENS = (
    "workspace.prepare_python_input",
    "workspace.list_files",
    "workspace.read_file",
    "workspace.read_text",
    "workspace.write_file",
    "workspace.create_artifact",
    "sandbox.run_python",
    "skill.read_resource",
    "skill.activate",
    "database.query",
    "database.list_tables",
    "database.describe_table",
    "project.list",
    "project.resolve",
    "document.read",
    "web.search",
    "web.fetch",
    "prepare_python_input",
    "artifact_id",
    "next_offset",
)
# 来源与迁移记录允许保留旧平台名词，避免抹掉移植历史。
HISTORY_FILES = {"PROVENANCE.md", "EASEL-META.md"}


def _load_validator():
    path = PROJECT_ROOT / "scripts" / "validate_skills.py"
    spec = importlib.util.spec_from_file_location("easel_validate_skills", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ADDED_SKILLS)
def test_added_skill_conforms_to_easel_spec(name: str) -> None:
    validator = _load_validator()
    path = SKILLS_ROOT / name / "SKILL.md"
    assert path.is_file(), f"缺少 {name}/SKILL.md"
    assert validator.validate(path) == []
    fields, _ = validator.parse_frontmatter(path)
    assert fields["layer"] == EXPECTED_LAYERS[name]


@pytest.mark.parametrize("name", ADDED_SKILLS)
def test_added_skill_has_no_platform_tool_residue(name: str) -> None:
    root = SKILLS_ROOT / name
    hits: list[str] = []
    for path in root.rglob("*.md"):
        if path.name in HISTORY_FILES:
            continue
        if "tests" in path.relative_to(root).parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in FORBIDDEN_TOOL_TOKENS:
            # 稳定错误码 database.query_failed 等带下划线后缀的标识不算平台工具名。
            if re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text):
                hits.append(f"{path.relative_to(PROJECT_ROOT)}: {token}")
    assert hits == []


@pytest.mark.parametrize("name", ADDED_SKILLS)
def test_added_skill_excludes_platform_manifests_and_caches(name: str) -> None:
    root = SKILLS_ROOT / name
    assert not (root / "manifest.yaml").exists()
    assert not (root / "migration.json").exists()
    for pattern in ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".secrets"):
        assert not list(root.rglob(pattern)), f"{name} 不应包含 {pattern}"


def test_geo_skill_does_not_ship_the_tavily_secret() -> None:
    secret = SKILLS_ROOT / "geo-content-sop" / ".secrets" / "tavily_key.txt"
    assert not secret.exists()


def test_project_docs_private_business_docs_are_gitignored() -> None:
    private_doc = "skills/builtin/project-docs/references/元梦空间/元梦空间/业务说明.md"
    result = subprocess.run(
        ["git", "check-ignore", "-q", private_doc],
        cwd=PROJECT_ROOT,
        capture_output=True,
    )
    if result.returncode not in {0, 1}:
        pytest.skip("当前环境无法调用 git check-ignore")
    assert result.returncode == 0, "真实业务文档必须被 .gitignore 排除"
    public_catalogue = "skills/builtin/project-docs/references/projects.yaml"
    assert subprocess.run(
        ["git", "check-ignore", "-q", public_catalogue],
        cwd=PROJECT_ROOT,
        capture_output=True,
    ).returncode == 1, "项目目录结构应保持可入库"


def test_database_query_runtime_files_are_gitignored() -> None:
    for relative in (
        "skills/builtin/database-query/config.yaml",
        "skills/builtin/database-query/state/budget.sqlite3",
        "skills/builtin/database-query/outputs/result.json",
    ):
        assert subprocess.run(
            ["git", "check-ignore", "-q", relative],
            cwd=PROJECT_ROOT,
            capture_output=True,
        ).returncode == 0, f"{relative} 必须被 skill 内 .gitignore 排除"


def test_database_query_dependencies_are_declared_in_project() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "asyncmy" in pyproject
    assert "sqlglot" in pyproject
    assert "SQLAlchemy" in (PROJECT_ROOT / "deploy" / "multiuser" / "requirements-worker.txt").read_text(
        encoding="utf-8"
    )


def test_data_analysis_excel_dependencies_are_declared_in_project() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "openpyxl" in pyproject
    assert "xlrd" in pyproject
    worker = (PROJECT_ROOT / "deploy" / "multiuser" / "requirements-worker.txt").read_text(
        encoding="utf-8"
    )
    assert "openpyxl" in worker
    assert "xlrd" in worker


def test_geo_web_search_fallback_is_documented_and_present() -> None:
    skill = (SKILLS_ROOT / "geo-content-sop" / "SKILL.md").read_text(encoding="utf-8")
    script = SKILLS_ROOT / "geo-content-sop" / "scripts" / "web_search.py"
    assert "scripts/web_search.py" in skill
    assert script.is_file()


def test_database_query_multiuser_tunnel_is_gitignored_but_config_is_tracked():
    assert subprocess.run(
        ["git", "check-ignore", "-q",
         "skills/builtin/database-query/assets/tunnel.multiuser.yaml"],
        cwd=PROJECT_ROOT, capture_output=True).returncode == 0
    for tracked in (
        "skills/builtin/database-query/assets/config.multiuser.yaml",
        "skills/builtin/database-query/assets/tunnel.multiuser.example.yaml",
    ):
        assert subprocess.run(
            ["git", "check-ignore", "-q", tracked],
            cwd=PROJECT_ROOT, capture_output=True).returncode == 1


def test_build_context_excludes_skill_runtime_state():
    lines = {line.strip() for line in
             (PROJECT_ROOT / ".dockerignore").read_text(
                 encoding="utf-8").splitlines()}
    for pattern in (
        "skills/**/config.yaml", "skills/**/config/database-query.yaml",
        "skills/**/state/", "skills/**/outputs/", "skills/**/.secrets/",
        "skills/**/.env", "新增的skill/", "skill-test/",
    ):
        assert pattern in lines, f"构建上下文未排除：{pattern}"
    for allowed in (
        "assets/config.multiuser.yaml", "assets/tunnel.multiuser.yaml",
        "deploy/multiuser/db_tunnel.py",
    ):
        assert allowed not in lines
