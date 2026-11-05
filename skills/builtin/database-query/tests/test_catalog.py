"""project-docs 目录解析与文档路径约束的单元测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dbquery.catalog import ProjectDirectory, ProjectEntry
from dbquery.errors import DomainError

_CATALOG = """
version: 1
projects:
  - id: p1a
    company: 企业
    name: 项目一
    aliases: [别名一]
    document: references/p1a.md
    connection_id: src_a
  - id: p2a
    company: 企业
    name: 项目二
    aliases: [别名一]
    document: references/p2a.md
    connection_id: null
"""


def _write_catalog(root: Path, text: str = _CATALOG, *, docs: bool = True) -> ProjectDirectory:
    (root / "references" / "projects.yaml").write_text(text, encoding="utf-8")
    if docs:
        (root / "references" / "p1a.md").write_text("# p1a", encoding="utf-8")
        (root / "references" / "p2a.md").write_text("# p2a", encoding="utf-8")
    return ProjectDirectory(root)


def _code(exc: pytest.ExceptionInfo[DomainError]) -> str:
    return exc.value.code


def test_exact_project_id_resolution(project_docs: Path) -> None:
    directory = _write_catalog(project_docs)

    project = directory.resolve("p1a")

    assert project.id == "p1a"
    assert project.connection_id == "src_a"
    assert [entry.id for entry in directory.list()] == ["p1a", "p2a"]


def test_alias_is_not_resolvable(project_docs: Path) -> None:
    directory = _write_catalog(project_docs)

    with pytest.raises(DomainError) as exc:
        directory.resolve("别名一")

    assert _code(exc) == "project.not_found"


def test_duplicate_project_names_and_aliases_are_not_resolvable(project_docs: Path) -> None:
    directory = _write_catalog(project_docs)

    for value in ("项目一", "项目二", "别名一"):
        with pytest.raises(DomainError) as exc:
            directory.resolve(value)
        assert _code(exc) == "project.not_found"


def test_duplicate_project_ids_rejected(project_docs: Path) -> None:
    text = """
    version: 1
    projects:
      - {id: p1a, company: c, name: n, aliases: [], document: references/p1a.md,
         connection_id: null}
      - {id: p1a, company: c, name: n2, aliases: [], document: references/p2a.md,
         connection_id: null}
    """
    directory = _write_catalog(project_docs, text, docs=False)

    with pytest.raises(DomainError) as exc:
        directory.list()

    assert _code(exc) == "project.config_unavailable"


def test_unknown_fields_rejected(project_docs: Path) -> None:
    text = """
    version: 1
    projects:
      - {id: p1a, company: c, name: n, aliases: [], document: references/p1a.md,
         connection_id: null, extra: 1}
    """
    directory = _write_catalog(project_docs, text)

    with pytest.raises(DomainError) as exc:
        directory.list()

    assert _code(exc) == "project.config_unavailable"


@pytest.mark.parametrize(
    "document",
    [
        "../outside.md",
        "/absolute.md",
        "references/../outside.md",
        "references\\p1a.md",
        "references/p1a.txt",
        "p1a.md",
        "references/p1a.md/..",
    ],
)
def test_document_path_must_be_relative_markdown(project_docs: Path, document: str) -> None:
    text = f"""
    version: 1
    projects:
      - {{id: p1a, company: c, name: n, aliases: [], document: '{document}',
         connection_id: null}}
    """
    directory = _write_catalog(project_docs, text)

    with pytest.raises(DomainError) as exc:
        directory.list()

    assert _code(exc) == "project.config_unavailable"


def test_missing_document_stops_resolution(project_docs: Path) -> None:
    directory = _write_catalog(project_docs, docs=False)
    project = directory.resolve("p1a")

    with pytest.raises(DomainError) as exc:
        directory.document_path(project)

    assert _code(exc) == "project.config_unavailable"


def test_document_path_stays_inside_root(project_docs: Path) -> None:
    directory = _write_catalog(project_docs)
    project = directory.resolve("p1a")

    path = directory.document_path(project)

    assert path == (project_docs / "references" / "p1a.md").resolve()


def test_catalog_is_reloaded_per_call(project_docs: Path) -> None:
    directory = _write_catalog(project_docs)
    assert [entry.id for entry in directory.list()] == ["p1a", "p2a"]

    updated = _CATALOG.split("  - id: p2a")[0]
    (project_docs / "references" / "projects.yaml").write_text(updated, encoding="utf-8")

    assert [entry.id for entry in directory.list()] == ["p1a"]


def test_missing_catalog_is_config_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "empty-docs"
    root.mkdir()

    with pytest.raises(DomainError) as exc:
        ProjectDirectory(root).list()

    assert _code(exc) == "project.config_unavailable"


def test_document_symlink_escape_rejected(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    (root / "references").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "p1a.md").write_text("# secret", encoding="utf-8")
    link = root / "references" / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        # Windows 无符号链接特权时退化为目录联接（junction），同样能验证真实路径逃逸
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
        )
        if result.returncode != 0:
            pytest.skip("当前环境不允许创建符号链接或目录联接")
    (root / "references" / "projects.yaml").write_text(
        """
        version: 1
        projects:
          - id: p1a
            company: c
            name: n
            aliases: []
            document: references/escape/p1a.md
            connection_id: null
        """,
        encoding="utf-8",
    )
    directory = ProjectDirectory(root)
    project = ProjectEntry(
        id="p1a",
        company="c",
        name="n",
        aliases=(),
        document="references/escape/p1a.md",
        connection_id=None,
    )

    with pytest.raises(DomainError) as exc:
        directory.document_path(project)

    assert _code(exc) == "project.config_unavailable"
