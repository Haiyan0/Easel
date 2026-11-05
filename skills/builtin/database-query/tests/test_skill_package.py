"""技能包结构：frontmatter、引用文件、示例配置、来源与无秘密检查。"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from dbquery.config import AppConfig

SKILL_ROOT = Path(__file__).resolve().parents[1]
_CREDENTIAL_URL = re.compile(r"mysql(?:\+asyncmy)?://[^:\s/]+:[^@\s]+@")
_SCANNED_SUFFIXES = {".md", ".yaml", ".yml", ".txt", ".py", ".toml", ".ini"}
_SKIP_DIRS = {"tests", "__pycache__", ".pytest_cache", "state", "outputs", ".git"}


def _frontmatter() -> dict[str, object]:
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    block = text.split("---\n", 2)[1]
    return yaml.safe_load(block)


def _package_files() -> list[Path]:
    files: list[Path] = []
    for path in SKILL_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in _SCANNED_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(SKILL_ROOT).parts):
            continue
        files.append(path)
    return files


def test_skill_frontmatter_has_name_and_description() -> None:
    data = _frontmatter()

    assert data["name"] == "database-query"
    assert isinstance(data["description"], str)
    assert "MySQL" in data["description"]


def test_referenced_files_exist() -> None:
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    referenced = set(re.findall(r"\]\(([^)]+)\)", text))
    referenced.update(re.findall(r"`((?:references|assets)/[^`]+)`", text))
    assert referenced, "SKILL.md 未引用任何参考文件"
    for relative in referenced:
        assert (SKILL_ROOT / relative).exists(), relative


def test_example_config_is_disabled_and_unverified() -> None:
    config = AppConfig.load(SKILL_ROOT / "assets" / "config.example.yaml")

    assert config.sources
    assert all(source.enabled is False for source in config.sources)
    assert all(source.readonly_account_verified is False for source in config.sources)


def test_package_contains_no_secret_material() -> None:
    hits: list[str] = []
    for path in _package_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if _CREDENTIAL_URL.search(text):
            hits.append(str(path.relative_to(SKILL_ROOT)))
    assert hits == []


def test_requirements_record_validated_versions() -> None:
    text = (SKILL_ROOT / "requirements.txt").read_text(encoding="utf-8")

    for name in ("pydantic", "PyYAML", "SQLAlchemy", "asyncmy", "sqlglot"):
        assert name in text
    for version in ("30.18.0", "0.2.14", "2.0.49", "2.13.4", "6.0.3"):
        assert version in text


def test_provenance_records_upstream_revision() -> None:
    text = (SKILL_ROOT / "PROVENANCE.md").read_text(encoding="utf-8")

    assert "bc6787d0b7e9a3a098307500d734f4cd2a6ec5b6" in text
    assert "policy.py" in text
    assert "mysql.py" in text


def test_gitignore_excludes_private_runtime_files() -> None:
    text = (SKILL_ROOT / ".gitignore").read_text(encoding="utf-8")

    for entry in ("config.yaml", ".env", "state/", "outputs/", "__pycache__/", ".pytest_cache/"):
        assert entry in text


def test_easel_tool_mapping_is_documented() -> None:
    path = SKILL_ROOT / "references" / "easel-mapping.md"
    assert path.is_file(), "缺少 Easel 本地工具等价说明"
    text = path.read_text(encoding="utf-8")
    for token in (
        "projects list",
        "projects resolve",
        "tables --run-id",
        "describe --run-id",
        "query --run-id",
        "saved.path",
        "data-analysis",
        "project-docs",
    ):
        assert token in text
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "references/easel-mapping.md" in skill
