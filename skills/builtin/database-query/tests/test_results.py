"""本地结果文件的 JSON 保真、原子落盘与路径约束。"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dbquery.errors import DomainError
from dbquery.results import build_payload, preview_rows, save_payload
from dbquery.types import QueryPage

_QUERIED_AT = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def _page(rows: int = 1) -> QueryPage:
    values = tuple(("1.20", "2.30", "2026-10-08", "9007199254740993") for _ in range(rows))
    return QueryPage(
        columns=("value", "value", "date", "id"),
        rows=values,
        returned_rows=rows,
        truncated=True,
        truncation_reason="row_limit",
        next_cursor=None,
        elapsed_ms=10,
    )


def _payload(page: QueryPage | None = None) -> dict[str, object]:
    return build_payload(
        project_id="p1a",
        connection_id="src_a",
        queried_at=_QUERIED_AT,
        sql="SELECT value FROM orders WHERE id = :id",
        parameters={"id": 7},
        page=page or _page(),
    )


def _code(exc: pytest.ExceptionInfo[DomainError]) -> str:
    return exc.value.code


def _make_outside_junction(root: Path, outside: Path) -> bool:
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
        return True
    except OSError:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(root / "link"), str(outside)],
            capture_output=True,
        )
        return result.returncode == 0


def test_saved_json_preserves_column_positions_and_types(tmp_path: Path) -> None:
    payload = _payload()

    saved = save_payload(tmp_path / "outputs", payload, filename="query-fixed.json")
    data = json.loads(saved.path.read_text(encoding="utf-8"))

    assert data["version"] == 1
    assert data["project_id"] == "p1a"
    assert data["connection_id"] == "src_a"
    assert data["queried_at"] == "2026-10-28T12:00:00+00:00"
    assert data["sql"].startswith("SELECT value")
    assert data["parameters"] == {"id": 7}
    assert data["page"]["columns"] == ["value", "value", "date", "id"]
    assert data["page"]["rows"][0][0] == "1.20"
    assert data["page"]["rows"][0][3] == "9007199254740993"
    assert data["page"]["truncated"] is True
    assert data["page"]["truncation_reason"] == "row_limit"
    assert saved.bytes == saved.path.stat().st_size


def test_save_refuses_overwrite(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    root.mkdir()
    target = root / "query-fixed.json"
    target.write_text("old-content", encoding="utf-8")

    with pytest.raises(DomainError) as exc:
        save_payload(root, _payload(), filename="query-fixed.json")

    assert _code(exc) == "database.result_unavailable"
    assert target.read_text(encoding="utf-8") == "old-content"


def test_save_is_atomic_on_write_failure(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "outputs"
    root.mkdir()

    def _broken_link(source, target):  # noqa: ANN001, ANN202 - 注入失败
        raise OSError("disk full")

    monkeypatch.setattr("dbquery.results.os.link", _broken_link)

    with pytest.raises(DomainError) as exc:
        save_payload(root, _payload(), filename="query-fixed.json")

    assert _code(exc) == "database.result_unavailable"
    assert not (root / "query-fixed.json").exists()
    assert list(root.iterdir()) == []


def test_output_path_escape_rejected(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    root.mkdir()

    for filename in ("../../escape.json", "/absolute.json", "nested/../escape.json"):
        with pytest.raises(DomainError) as exc:
            save_payload(root, _payload(), filename=filename)
        assert _code(exc) == "database.result_path_invalid"


def test_output_symlink_escape_rejected(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if not _make_outside_junction(root, outside):
        pytest.skip("当前环境不允许创建符号链接或目录联接")

    with pytest.raises(DomainError) as exc:
        save_payload(root, _payload(), filename="link/escape.json")

    assert _code(exc) == "database.result_path_invalid"
    assert list(outside.iterdir()) == []


def test_saved_payload_does_not_contain_connection_url(tmp_path: Path) -> None:
    saved = save_payload(tmp_path / "outputs", _payload(), filename="query-fixed.json")

    content = saved.path.read_text(encoding="utf-8")
    assert "mysql+asyncmy://" not in content
    assert "canary-password" not in content


def test_output_root_is_created(tmp_path: Path) -> None:
    root = tmp_path / "deep" / "outputs"

    saved = save_payload(root, _payload(), filename="query-fixed.json")

    assert saved.path.parent == root.resolve()


def test_preview_rows_limits_without_changing_page() -> None:
    page = _page(rows=30)

    preview = preview_rows(page, limit=20)

    assert len(preview) == 20
    assert page.returned_rows == 30
    assert page.truncated is True
