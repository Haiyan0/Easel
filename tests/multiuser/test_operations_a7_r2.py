"""A7-R2：prepare 输入验证与 verify 真只读回归。"""
from __future__ import annotations

import hashlib
import json
import runpy
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from easel.control.cli import (
    materialize_restore,
    prepare_restored_instance,
    restore_backup,
    verify_backup,
)
from easel.control.types import DomainError

ROOT = Path(__file__).resolve().parents[2]
READ_STOP = ROOT / "deploy" / "desktop" / "read_stop_state.py"
FIXTURE = runpy.run_path(str(Path(__file__).with_name(
    "test_operations_a7.py")))
_seed = FIXTURE["_seed"]
_stop = FIXTURE["_stop"]
_backup = FIXTURE["_backup"]
_prepared_staging = FIXTURE["_prepared_staging"]
_empty_targets = FIXTURE["_empty_targets"]


def _tree(root: Path) -> dict[str, tuple[int, str]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest())
        for path in root.rglob("*") if path.is_file()
    }


def _assert_error(error: pytest.ExceptionInfo, code: str, status: int) -> None:
    assert isinstance(error.value, DomainError)
    assert (error.value.code, error.value.status) == (code, status), error.value


def test_prepare_rejects_tampered_database_without_changing_staging(tmp_path):
    seeded = _seed(tmp_path)
    _stop(seeded)
    backup = tmp_path / "backup"
    _backup(seeded, backup)
    staging = tmp_path / "staging"
    restore_backup(backup, staging)
    with sqlite3.connect(staging / "control" / "control.sqlite3") as database:
        database.execute(
            "UPDATE users SET display_name = 'CORRUPTED-STAGING' WHERE id = ?",
            (seeded["member"].user_id,))
        database.commit()
    with pytest.raises(DomainError) as verify_error:
        verify_backup(staging)
    _assert_error(verify_error, "backup_integrity_failed", 409)

    before = _tree(staging)
    manifest_before = (staging / "manifest.json").read_bytes()
    with pytest.raises(DomainError) as prepare_error:
        prepare_restored_instance(staging, now=int(time.time()))
    _assert_error(prepare_error, "backup_integrity_failed", 409)
    assert _tree(staging) == before
    assert (staging / "manifest.json").read_bytes() == manifest_before


def test_prepare_rejects_tampered_workspace_without_changing_staging(tmp_path):
    seeded = _seed(tmp_path)
    _stop(seeded)
    backup = tmp_path / "backup"
    _backup(seeded, backup)
    staging = tmp_path / "staging"
    restore_backup(backup, staging)
    path = (staging / "workspaces" / seeded["member"].workspace_id /
            "workspace" / "sentinel.txt")
    original = path.read_bytes()
    path.write_bytes(original[:-1] + b"X")
    before = _tree(staging)
    with pytest.raises(DomainError) as prepare_error:
        prepare_restored_instance(staging, now=int(time.time()))
    _assert_error(prepare_error, "backup_integrity_failed", 409)
    assert _tree(staging) == before


def test_prepare_valid_staging_still_materializes(tmp_path):
    seeded = _seed(tmp_path)
    staging = _prepared_staging(seeded, tmp_path)
    assert verify_backup(staging)["version"] == 2
    control, keys, egress, workspace = _empty_targets(tmp_path)
    result = materialize_restore(
        staging, control_dir=control, keys_dir=keys,
        egress_dir=egress, workspace_root=workspace)
    assert result["workspaces"] == 2
    assert (control / "control.sqlite3").is_file()


def test_read_stop_state_helper_is_read_only_and_schema_compatible(tmp_path):
    control = tmp_path / "control"
    keys = tmp_path / "keys"
    control.mkdir()
    keys.mkdir()
    database = control / "control.sqlite3"
    barrier = {"id": "stop-old", "project": "p", "createdAt": 1}
    completion = {"barrierId": "stop-old", "project": "p",
                  "configSha256": "a" * 64, "completedAt": 2}
    with sqlite3.connect(database) as connection:
        # 模拟旧 schema 的 control_state：不依赖 Store 迁移/其他表。
        connection.execute(
            "CREATE TABLE control_state (key TEXT PRIMARY KEY,"
            " value TEXT NOT NULL, updated_at INTEGER NOT NULL)")
        connection.execute(
            "INSERT INTO control_state VALUES (?, ?, ?)",
            ("admission_barrier", json.dumps(barrier), 1))
        connection.execute(
            "INSERT INTO control_state VALUES (?, ?, ?)",
            ("stop_completion", json.dumps(completion), 2))
        connection.commit()
    for name, data in (
            ("master.key", b"k" * 32),
            ("snapshot.pem", b"private"),
            ("worker-public.key", b"worker"),
            ("gateway-public.pem", b"gateway")):
        (keys / name).write_bytes(data)
    before = _tree(tmp_path)
    result = subprocess.run(
        [sys.executable, str(READ_STOP), "--control-dir", str(control),
         "--keys-dir", str(keys), "--tmp-dir", str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    line = next(item for item in result.stdout.splitlines()
                if item.startswith("stop_state="))
    state = json.loads(line.split("=", 1)[1])
    assert state["admission_barrier"]["id"] == "stop-old"
    assert state["stop_completion"]["configSha256"] == "a" * 64
    for name, data in (
            ("master_key", b"k" * 32),
            ("signing_key", b"private"),
            ("worker_public", b"worker"),
            ("gateway_public", b"gateway")):
        assert f"{name}_sha256={hashlib.sha256(data).hexdigest()}" in result.stdout
    assert _tree(tmp_path) == before


def test_verify_prepared_backup_is_repeatedly_read_only(tmp_path):
    seeded = _seed(tmp_path)
    staging = _prepared_staging(seeded, tmp_path)
    before = _tree(staging)
    assert verify_backup(staging)["version"] == 2
    assert _tree(staging) == before
    assert verify_backup(staging)["version"] == 2
    assert _tree(staging) == before
    assert not list((staging / "control").glob("control.sqlite3-*"))

    control, keys, egress, workspace = _empty_targets(tmp_path)
    result = materialize_restore(
        staging, control_dir=control, keys_dir=keys,
        egress_dir=egress, workspace_root=workspace)
    assert result["workspaces"] == 2
