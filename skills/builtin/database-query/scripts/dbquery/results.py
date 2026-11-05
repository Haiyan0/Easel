"""有界查询结果的本地 JSON 落盘。

只保存本次已返回的有界结果；先写同目录临时文件并 fsync，再用 ``os.link`` 原子
创建目标（目标已存在即失败），因此不会覆盖已有文件、不会留下半写文件。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

from dbquery.errors import DomainError
from dbquery.types import QueryPage

logger = logging.getLogger(__name__)

_MAX_NAME_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True, slots=True)
class SavedResult:
    """成功落盘的结果文件。"""

    path: Path
    bytes: int


def build_payload(
    project_id: str,
    connection_id: str,
    queried_at: datetime,
    sql: str,
    parameters: dict[str, object] | None,
    page: QueryPage,
) -> dict[str, object]:
    """按稳定契约组装结果 JSON；不包含连接 URL 或口令。"""
    if queried_at.tzinfo is None:
        queried_at = queried_at.replace(tzinfo=timezone.utc)
    return dict(
        version=1,
        project_id=project_id,
        connection_id=connection_id,
        queried_at=queried_at.astimezone(timezone.utc).isoformat(),
        sql=sql,
        parameters=parameters or {},
        page=asdict(page),
    )


def preview_rows(page: QueryPage, limit: int = 20) -> tuple[tuple[object, ...], ...]:
    """返回有界预览；不改变 ``returned_rows/truncated``。"""
    return page.rows[:limit]


def save_payload(
    output_root: Path,
    payload: dict[str, object],
    *,
    filename: str | None = None,
) -> SavedResult:
    """把结果原子写入 ``output_root`` 内；拒绝逃逸、覆盖与不可序列化内容。"""
    try:
        content = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise DomainError("database.result_unavailable", "查询结果无法保存", status=503) from None

    root = Path(output_root).resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise DomainError("database.result_unavailable", "查询结果无法保存", status=503) from None

    name = filename or _default_name()
    _validate_name(name)
    try:
        target = (root / name).resolve()
    except OSError:
        raise _path_invalid() from None
    if not target.is_relative_to(root):
        raise _path_invalid()
    if target.exists():
        raise DomainError("database.result_unavailable", "结果文件已存在", status=503)

    temp = root / f".tmp-{uuid4().hex}.json"
    try:
        with open(temp, "xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.link(temp, target)
    except OSError:
        raise DomainError("database.result_unavailable", "查询结果无法保存", status=503) from None
    finally:
        try:
            temp.unlink()
        except OSError:
            pass
    return SavedResult(target, len(content))


def _default_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"query-{stamp}-{uuid4().hex}.json"


def _validate_name(name: str) -> None:
    """允许根目录内的简单相对子路径，拒绝绝对路径、穿越与非法字符。"""
    if not name or "\\" in name or ":" in name:
        raise _path_invalid()
    try:
        path = PurePosixPath(name)
    except ValueError:
        raise _path_invalid() from None
    if path.is_absolute() or path.suffix != ".json":
        raise _path_invalid()
    parts = name.split("/")
    if any(part in {"", ".", ".."} or _MAX_NAME_PART.fullmatch(part) is None for part in parts):
        raise _path_invalid()


def _path_invalid() -> DomainError:
    return DomainError("database.result_path_invalid", "结果文件路径无效", status=400)
