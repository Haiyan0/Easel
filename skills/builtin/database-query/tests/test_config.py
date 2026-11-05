"""本地配置、路径解析与显式 env 文件的单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dbquery.config import AppConfig, load_env_file, resolve_paths
from dbquery.errors import DomainError

_VALID = """
version: 1
project_docs_root: ../project-docs
state_dir: ./state
output_dir: ./outputs
sources:
  - id: sales_readonly
    secret_env: SALES_READONLY_DATABASE_URL
    allowed_schema: sales
    allowed_tables: [orders, customers]
    readonly_account_verified: false
    enabled: false
"""


def _code(exc: pytest.ExceptionInfo[DomainError]) -> str:
    return exc.value.code


def test_relative_paths_resolve_against_config_dir(tmp_path: Path, write_config) -> None:
    (tmp_path / "project-docs").mkdir()
    config_path = write_config(_VALID, "proj/config.yaml")
    config = AppConfig.load(config_path)

    paths = resolve_paths(config, config_path)

    assert paths.project_docs_root == (tmp_path / "project-docs").resolve()
    assert paths.state_dir == (tmp_path / "proj/state").resolve()
    assert paths.output_dir == (tmp_path / "proj/outputs").resolve()


def test_absolute_paths_are_preserved(tmp_path: Path, write_config) -> None:
    root = tmp_path / "docs-abs"
    root.mkdir()
    config_path = write_config(
        f"""
        version: 1
        project_docs_root: {root.as_posix()}
        """,
        "config.yaml",
    )
    config = AppConfig.load(config_path)

    assert resolve_paths(config, config_path).project_docs_root == root.resolve()


def test_default_state_and_output_dirs(tmp_path: Path, write_config) -> None:
    (tmp_path / "project-docs").mkdir()
    config_path = write_config("version: 1\nproject_docs_root: ./project-docs\n", "config.yaml")
    config = AppConfig.load(config_path)

    paths = resolve_paths(config, config_path)

    assert paths.state_dir == (tmp_path / "state").resolve()
    assert paths.output_dir == (tmp_path / "outputs").resolve()


@pytest.mark.parametrize(
    "extra",
    [
        "unknown: 1\n",
        "version: 2\n",
        "version: true\n",
        "sources: [{id: Sales, secret_env: A, allowed_schema: s, allowed_tables: [t], "
        "readonly_account_verified: false, enabled: false}]\n",
        "sources: [{id: src, secret_env: 'mysql://x', allowed_schema: s, "
        "allowed_tables: [t], readonly_account_verified: false, enabled: false}]\n",
        "sources: [{id: src, secret_env: A, allowed_schema: 'bad-schema', "
        "allowed_tables: [t], readonly_account_verified: false, enabled: false}]\n",
        "sources: [{id: src, secret_env: A, allowed_schema: s, allowed_tables: [], "
        "readonly_account_verified: false, enabled: false}]\n",
    ],
)
def test_invalid_config_is_unavailable(tmp_path: Path, write_config, extra: str) -> None:
    header = "project_docs_root: ./project-docs\n"
    config_path = write_config(f"version: 1\n{header}{extra}", "config.yaml")

    with pytest.raises(DomainError) as exc:
        AppConfig.load(config_path)

    assert _code(exc) == "config.unavailable"
    assert "mysql://x" not in str(exc.value)


def test_duplicate_source_ids_rejected(tmp_path: Path, write_config) -> None:
    config_path = write_config(
        """
        version: 1
        project_docs_root: ./project-docs
        sources:
          - {id: src_a, secret_env: A, allowed_schema: s, allowed_tables: [t],
             readonly_account_verified: false, enabled: false}
          - {id: src_a, secret_env: B, allowed_schema: s, allowed_tables: [t],
             readonly_account_verified: false, enabled: false}
        """,
        "config.yaml",
    )

    with pytest.raises(DomainError) as exc:
        AppConfig.load(config_path)

    assert _code(exc) == "config.unavailable"


def test_duplicate_yaml_keys_rejected(tmp_path: Path, write_config) -> None:
    config_path = write_config(
        "version: 1\nproject_docs_root: ./a\nproject_docs_root: ./b\n", "config.yaml"
    )

    with pytest.raises(DomainError) as exc:
        AppConfig.load(config_path)

    assert _code(exc) == "config.unavailable"


def test_unreadable_and_unparsable_config(tmp_path: Path, write_config) -> None:
    with pytest.raises(DomainError) as missing:
        AppConfig.load(tmp_path / "missing.yaml")
    assert _code(missing) == "config.unavailable"

    broken = write_config("version: [\n", "broken.yaml")
    with pytest.raises(DomainError) as invalid:
        AppConfig.load(broken)
    assert _code(invalid) == "config.unavailable"


def test_missing_project_docs_root_stops_resolution(tmp_path: Path, write_config) -> None:
    config_path = write_config("version: 1\nproject_docs_root: ./missing\n", "config.yaml")
    config = AppConfig.load(config_path)

    with pytest.raises(DomainError) as exc:
        resolve_paths(config, config_path)

    assert _code(exc) == "project.config_unavailable"


def test_env_file_reads_literal_values_only(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\n\nA=$HOME\nB=\"x y\"\nC='z'\nTOKEN=a=b\n", encoding="utf-8")

    values = load_env_file(env_file)

    assert values == {"A": "$HOME", "B": "x y", "C": "z", "TOKEN": "a=b"}


def test_env_file_duplicate_key_rejected(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\nA=2\n", encoding="utf-8")

    with pytest.raises(DomainError) as exc:
        load_env_file(env_file)

    assert _code(exc) == "config.unavailable"


def test_env_file_invalid_key_rejected(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("1BAD=x\n", encoding="utf-8")

    with pytest.raises(DomainError) as exc:
        load_env_file(env_file)

    assert _code(exc) == "config.unavailable"


def test_env_file_missing_is_config_unavailable(tmp_path: Path) -> None:
    with pytest.raises(DomainError) as exc:
        load_env_file(tmp_path / "missing.env")

    assert _code(exc) == "config.unavailable"
