"""Task 14：正式备份在新目录恢复的数据级门槛。

只在显式 ``--multiuser-live`` 下运行。源备份由维护窗口实际产生；缺少
``EASEL_TEST_BACKUP_SOURCE``/``EASEL_TEST_RESTORE_DEST`` 时失败，不 skip。
本测试不删除源、不覆盖运行中的 control 目录，也不启动供应商调用。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from easel.control.cli import restore_backup
from easel.control.store import Store

pytestmark = pytest.mark.multiuser_integration


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_tree(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = _sha256(path)
    return result


def test_live_backup_restores_to_new_directory_without_touching_source(
        live_restore_env):
    source = Path(live_restore_env["source"])
    destination = Path(live_restore_env["destination"])
    managed = live_restore_env.get("managed_root")
    assert source.is_dir()
    assert (source / "manifest.json").is_file()
    if destination.exists():
        assert destination.is_dir()
        assert not any(destination.iterdir()), (
            "正式恢复目标必须由本次演练新建或为空；"
            "不能覆盖运行中的 control 目录")

    before = _digest_tree(source)
    restored = restore_backup(
        source, destination,
        managed_root=Path(managed) if managed else None)
    assert restored == destination
    assert _digest_tree(source) == before, "恢复不得改写备份源"
    assert (destination / "manifest.json").read_bytes() == (
        source / "manifest.json").read_bytes()

    manifest = json.loads((source / "manifest.json").read_text(
        encoding="utf-8"))
    assert manifest["format"] == "easel.control.backup"
    assert isinstance(manifest["files"], list) and manifest["files"]
    for entry in manifest["files"]:
        relative = entry["path"]
        path = destination.joinpath(*relative.split("/"))
        assert path.is_file(), relative
        assert path.stat().st_size == entry["bytes"], relative
        assert _sha256(path) == entry["sha256"], relative

    database = destination.joinpath(
        *manifest["control"]["path"].split("/"))
    with Store.open(database) as store:
        workspace_ids = set(store.list_workspace_ids())
    assert workspace_ids == set(manifest["workspaces"])

    # 归属关系必须随 SQLite 快照恢复；只读查询，不修改恢复副本。
    uri = f"{database.as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        owners = {
            row[0]: row[1] for row in connection.execute(
                "SELECT workspaces.id, workspaces.owner_user_id"
                " FROM workspaces JOIN users"
                " ON users.id = workspaces.owner_user_id")
        }
    assert set(owners) == set(manifest["workspaces"])
    assert all(value for value in owners.values())

    for workspace_id, parts in manifest["workspaces"].items():
        for part, present in parts.items():
            path = destination / "workspaces" / workspace_id / part
            if present:
                assert path.is_dir(), path
