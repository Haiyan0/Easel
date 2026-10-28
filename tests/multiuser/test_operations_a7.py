"""A7：完整备份、只读校验、恢复准备与受控升级的契约测试。

所有材料都在 ``tmp_path`` 内生成；不读取开发者 HOME、真实密钥或运行中
的 Desktop 数据。真实容器演练在 ``tests/integration/multiuser`` 单独计数。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import easel.control.cli as control_cli
from easel.control.auth import AuthService
from easel.control.cli import (
    create_backup,
    materialize_restore,
    prepare_restored_instance,
    restore_backup,
    verify_backup,
)
from easel.control.store import SCHEMA_VERSION, Store
from easel.control.types import DomainError, WorkerHandle


PROJECT = "easel-a7-unit"
ADMIN_NAME = "A7 Owner"


def _assert_error(error: pytest.ExceptionInfo, code: str, status: int) -> None:
    assert isinstance(error.value, DomainError)
    assert (error.value.code, error.value.status) == (code, status), error.value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_private(path: Path, data: bytes, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if os.name == "posix":
        path.chmod(mode)
    return path


def _signing_key(path: Path) -> Ed25519PrivateKey:
    key = Ed25519PrivateKey.generate()
    _write_private(path, key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return key


def _managed_config(path: Path) -> Path:
    _write_private(path, json.dumps({
        "revision": 1,
        "agent": {
            "runtime": "acp",
            "command": ["python", "-c", "pass"],
            "sandbox": "workspace-write",
            "timeout": 60,
        },
        "broker": {"mode": "workspace-sidecar", "channel": "chat"},
    }).encode(), 0o600)
    return path


def _egress_policy(path: Path) -> Path:
    _write_private(path, json.dumps({
        "approved_hosts": ["provider.example"],
        "approved_cidrs": [],
    }).encode(), 0o600)
    return path


def _deployment(config_sha: str = "a" * 64) -> dict:
    return {
        "project": PROJECT,
        "origin": "https://a7.example",
        "port": 7861,
        "control_image": "sha256:" + "1" * 64,
        "worker_image": "sha256:" + "2" * 64,
        "gateway_image": "sha256:" + "3" * 64,
        "frontend_sha256": "b" * 64,
        "config_sha256": config_sha,
        "engine_id": "engine-test",
        "engine_context": "desktop-linux",
        "source_commit": "c" * 40,
        "harness_revision": "d" * 40,
    }


def _workspace_files(root: Path, workspace_id: str) -> None:
    workspace = root / workspace_id / "workspace"
    (workspace / "state").mkdir(parents=True, exist_ok=True)
    (workspace / "sentinel.txt").write_text("sentinel-a7", encoding="utf-8")
    (workspace / "state" / "state.json").write_text(
        "{\"state\": 1}", encoding="utf-8")
    home = root / workspace_id / "home"
    (home / ".dsh").mkdir(parents=True, exist_ok=True)
    (home / ".dsh" / "session.json").write_text(
        "{\"session\": 1}", encoding="utf-8")
    credentials = root / ".credentials" / f"{workspace_id}.env"
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text("EASEL_SERVICE_TOKEN=old-token", encoding="utf-8")
    attempt = root / ".gateway" / f"attempt-{workspace_id}.json"
    attempt.parent.mkdir(parents=True, exist_ok=True)
    attempt.write_text("{\"attempt_id\": \"old\"}", encoding="utf-8")


def _seed(tmp_path: Path):
    control = tmp_path / "control"
    workspace_root = tmp_path / "workspaces"
    master = _write_private(tmp_path / "master.key", b"k" * 32)
    signing = _signing_key(tmp_path / "snapshot.pem")
    managed = _managed_config(tmp_path / "managed-agent.json")
    policy = _egress_policy(tmp_path / "egress-policy.json")
    with Store.open(control / "control.sqlite3") as store:
        admin, invite = AuthService(store).bootstrap_admin(ADMIN_NAME)
        member = store.create_user("A7 Member")
        conversation = store.create_conversation(member)
        _workspace_files(workspace_root, member.workspace_id)
    return {
        "control": control,
        "workspace_root": workspace_root,
        "master": master,
        "signing": tmp_path / "snapshot.pem",
        "managed": managed,
        "policy": policy,
        "admin": admin,
        "member": member,
        "conversation": conversation,
        "invite": invite,
    }


def _backup(seeded, destination: Path, *, deployment=None,
            require_clean_stop: bool = False, **overrides):
    params = {
        "signing_key_file": seeded["signing"],
        "managed_config_file": seeded["managed"],
        "egress_policy_file": seeded["policy"],
        "deployment": deployment if deployment is not None else _deployment(),
        "require_clean_stop": require_clean_stop,
    }
    params.update(overrides)
    with Store.open(seeded["control"] / "control.sqlite3") as store:
        return create_backup(
            store, destination,
            control_dir=seeded["control"],
            master_key_file=seeded["master"],
            workspace_root=seeded["workspace_root"],
            image_digest=params["deployment"]["worker_image"],
            harness_revision=params["deployment"]["harness_revision"],
            **{key: value for key, value in params.items()
               if key not in ("deployment",)},
            **({"deployment": params["deployment"]}
               if params["deployment"] is not None else {}),
        )


def _stop(seeded, *, barrier_id: str = "barrier-a7",
          project: str = PROJECT, config_sha: str = "a" * 64):
    with Store.open(seeded["control"] / "control.sqlite3") as store:
        store.set_admission_barrier(barrier_id, "desktop-stop", int(time.time()))
        store.complete_stop(barrier_id, project, config_sha, int(time.time()))


def test_full_backup_requires_all_desktop_materials(tmp_path):
    seeded = _seed(tmp_path)
    # 缺签名私钥：即使 master key 存在也必须失败，不能生成缺项备份。
    with pytest.raises(DomainError) as missing:
        _backup(seeded, tmp_path / "backup-missing", signing_key_file=None)
    _assert_error(missing, "backup_signing_key_missing", 409)
    assert not (tmp_path / "backup-missing").exists()

    # 只有公钥没有私钥：不得初始化新 key“修复”，也不得写出备份。
    signing = seeded["signing"]
    signing.unlink()
    public = signing.with_name("gateway-public.pem")
    public.write_bytes(b"public-only")
    with pytest.raises(DomainError) as public_only:
        _backup(seeded, tmp_path / "backup-public-only")
    _assert_error(public_only, "backup_signing_key_missing", 409)
    assert not (tmp_path / "backup-public-only").exists()


def test_full_backup_manifest_records_identity_without_secret_material(tmp_path):
    seeded = _seed(tmp_path)
    destination = tmp_path / "backup"
    _backup(seeded, destination)

    manifest = json.loads((destination / "manifest.json").read_text(
        encoding="utf-8"))
    assert manifest["version"] == 2
    assert manifest["complete"] is True
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["deployment"]["config_sha256"] == "a" * 64
    assert manifest["deployment"]["control_image"] == "sha256:" + "1" * 64
    assert manifest["identity"]["signing_key"]["path"] == "snapshot.pem"
    assert manifest["identity"]["master_key"]["path"] == "master.key"
    expected = {
        "master.key": hashlib.sha256(b"k" * 32).hexdigest(),
    }
    for name, digest in expected.items():
        entry = next(item for item in manifest["files"]
                     if item["path"] == name)
        assert entry["sha256"] == digest

    # 备份里允许存在私钥文件本身，但清单文本不得包含密钥内容。
    manifest_text = (destination / "manifest.json").read_text(encoding="utf-8")
    assert "k" * 32 not in manifest_text
    assert "PRIVATE KEY" not in manifest_text
    assert manifest["runtime_state"]["excluded"]
    excluded = {item["path"] for item in manifest["runtime_state"]["excluded"]}
    assert ".credentials" in excluded
    assert ".gateway" in excluded
    assert not (destination / "workspaces" / seeded["member"].workspace_id
                / ".credentials").exists()

    # 派生公钥必须由私钥重建，且与 manifest 记录一致。
    raw = json.loads((destination / "manifest.json").read_text("utf-8"))
    worker = next(item for item in raw["files"]
                  if item["path"] == "worker-public.key")
    assert worker["sha256"] == _sha256(destination / "worker-public.key")


def test_verify_backup_is_read_only_and_detects_missing_signing_key(tmp_path):
    seeded = _seed(tmp_path)
    destination = tmp_path / "backup"
    _backup(seeded, destination)
    before = {path.relative_to(destination).as_posix(): _sha256(path)
              for path in destination.rglob("*") if path.is_file()}

    summary = verify_backup(destination)
    assert summary["version"] == 2
    assert summary["workspaces"] == 2
    assert summary["schema_version"] == SCHEMA_VERSION
    after = {path.relative_to(destination).as_posix(): _sha256(path)
             for path in destination.rglob("*") if path.is_file()}
    assert before == after

    (destination / "snapshot.pem").unlink()
    with pytest.raises(DomainError) as missing:
        verify_backup(destination)
    _assert_error(missing, "backup_integrity_failed", 409)


def test_restore_full_backup_to_new_root_verifies_every_material(tmp_path):
    seeded = _seed(tmp_path)
    destination = tmp_path / "backup"
    _backup(seeded, destination)
    restored = tmp_path / "restored"
    assert restore_backup(destination, restored) == restored
    assert (restored / "snapshot.pem").is_file()
    assert (restored / "worker-public.key").is_file()
    assert (restored / "gateway-public.pem").is_file()
    assert (restored / "managed-agent.json").is_file()
    assert (restored / "egress-policy.json").is_file()
    assert (restored / "workspaces" / seeded["member"].workspace_id
            / "workspace" / "state" / "state.json").is_file()
    assert (restored / "workspaces" / seeded["member"].workspace_id
            / "home" / ".dsh" / "session.json").is_file()


def test_restore_rejects_tampered_signing_key_without_partial_target(tmp_path):
    seeded = _seed(tmp_path)
    destination = tmp_path / "backup"
    _backup(seeded, destination)
    # 改私钥但不改摘要：必须按清单摘要失败，而不是用错误 key 上线。
    path = destination / "snapshot.pem"
    path.write_bytes(path.read_bytes() + b"tampered")
    target = tmp_path / "restored"
    with pytest.raises(DomainError) as tampered:
        restore_backup(destination, target)
    _assert_error(tampered, "backup_integrity_failed", 409)
    assert not target.exists()


def test_restore_rejects_future_schema_database(tmp_path):
    seeded = _seed(tmp_path)
    destination = tmp_path / "backup"
    _backup(seeded, destination)
    database = destination / "control" / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.commit()
    # 同一目录里改库后必须同步清单摘要，才能验证“未来 schema 被拒绝”，
    # 而不是先被摘要不匹配挡住。
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["files"]
                 if item["path"] == "control/control.sqlite3")
    entry["bytes"] = database.stat().st_size
    entry["sha256"] = _sha256(database)
    manifest["control"]["bytes"] = entry["bytes"]
    manifest["control"]["sha256"] = entry["sha256"]
    manifest_path.write_text(json.dumps(manifest, sort_keys=True),
                             encoding="utf-8")
    target = tmp_path / "restored-future"
    with pytest.raises(DomainError) as future:
        restore_backup(destination, target)
    _assert_error(future, "backup_incompatible", 409)
    assert not target.exists()


def test_prepare_restored_instance_revokes_sessions_tokens_and_barrier(tmp_path):
    seeded = _seed(tmp_path)
    with Store.open(seeded["control"] / "control.sqlite3") as store:
        admin = store.get_identity(seeded["admin"].user_id)
        AuthService(store).create_invite(admin, seeded["member"].user_id)
    _stop(seeded)
    with Store.open(seeded["control"] / "control.sqlite3") as store:
        session = AuthService(store).exchange(seeded["invite"])
        member_before = store.get_identity(seeded["member"].user_id)
        assert member_before.role == "member"

    destination = tmp_path / "backup"
    _backup(seeded, destination)
    restored = tmp_path / "restored"
    restore_backup(destination, restored)
    summary = prepare_restored_instance(restored, now=int(time.time()))
    assert summary["revoked_logins"] >= 1
    assert summary["revoked_invites"] >= 1
    assert summary["cleared_barrier"] == "barrier-a7"
    assert summary["recovery_invite"]
    with Store.open(restored / "control" / "control.sqlite3") as store:
        assert store.admission_barrier() is None
        assert store.stop_completion() is not None
        assert store.stop_completion()["reopenedAt"]
        with sqlite3.connect(restored / "control" / "control.sqlite3") as raw:
            revoked = raw.execute(
                "SELECT revoked_at FROM logins WHERE id = ?",
                (session.id,)).fetchone()
            assert revoked is not None and revoked[0] is not None
            assert raw.execute(
                "SELECT COUNT(*) FROM broker_tokens"
                " WHERE revoked_at IS NULL").fetchone()[0] == 0
        with pytest.raises(DomainError):
            store.authenticate_login(
                hashlib.sha256(session.token.encode()).hexdigest(),
                int(time.time()), 3600)
        # 新的管理员恢复邀请可以兑换一次。
        fresh = AuthService(store).exchange(summary["recovery_invite"])
        identity = store.authenticate_login(
            hashlib.sha256(fresh.token.encode()).hexdigest(),
            int(time.time()), 3600)
        assert identity.user_id == seeded["admin"].user_id


def test_prepare_restored_instance_refuses_active_run(tmp_path):
    seeded = _seed(tmp_path)
    _stop(seeded)
    destination = tmp_path / "backup"
    _backup(seeded, destination)
    restored = tmp_path / "restored"
    restore_backup(destination, restored)
    with sqlite3.connect(restored / "control" / "control.sqlite3") as connection:
        connection.execute(
            "INSERT INTO runs (workspace_id, id, conversation_id,"
            " client_request_id, input_digest, status, config_version,"
            " created_at) VALUES (?, ?, ?, ?, ?, 'running', 0, ?)",
            (seeded["member"].workspace_id, "f" * 32,
             seeded["conversation"].id, "active-request", "d" * 64,
             int(time.time())))
        connection.commit()
    with pytest.raises(DomainError) as active:
        prepare_restored_instance(restored, now=int(time.time()))
    _assert_error(active, "restore_source_active", 409)


def test_materialize_restore_populates_deployment_targets(tmp_path):
    seeded = _seed(tmp_path)
    _stop(seeded)
    destination = tmp_path / "backup"
    _backup(seeded, destination)
    restored = tmp_path / "restored"
    restore_backup(destination, restored)
    prepare_restored_instance(restored, now=int(time.time()))

    control = tmp_path / "target-control"
    keys = tmp_path / "target-keys"
    egress = tmp_path / "target-egress"
    workspace = tmp_path / "target-workspaces"
    for path in (control, keys, egress, workspace):
        path.mkdir()
    result = materialize_restore(
        restored, control_dir=control, keys_dir=keys,
        egress_dir=egress, workspace_root=workspace)
    assert result["workspaces"] == 2
    assert (control / "control.sqlite3").is_file()
    for name in ("master.key", "snapshot.pem", "managed-agent.json",
                 "egress-policy.json", "worker-public.key",
                 "gateway-public.pem"):
        assert (keys / name).is_file(), name
    assert (egress / "egress-policy.json").is_file()
    assert (workspace / seeded["member"].workspace_id / "workspace"
            / "sentinel.txt").is_file()
    if os.name == "posix":
        assert (keys / "master.key").stat().st_mode & 0o077 == 0
        assert (keys / "snapshot.pem").stat().st_mode & 0o077 == 0

    # 目标非空时拒绝覆盖，也不触碰源。
    with pytest.raises(DomainError) as occupied:
        materialize_restore(
            restored, control_dir=control, keys_dir=keys,
            egress_dir=egress, workspace_root=workspace)
    _assert_error(occupied, "restore_material_target_not_empty", 409)


def test_upgrade_stop_completion_requires_matching_backup_and_old_digest(tmp_path):
    seeded = _seed(tmp_path)
    _stop(seeded, config_sha="a" * 64)
    with Store.open(seeded["control"] / "control.sqlite3") as store:
        with pytest.raises(DomainError) as wrong:
            store.upgrade_stop_completion(
                "barrier-a7", PROJECT, "b" * 64, "c" * 64, "d" * 64,
                int(time.time()))
        _assert_error(wrong, "stop_completion_mismatch", 409)
        with pytest.raises(DomainError) as bad_backup:
            store.upgrade_stop_completion(
                "barrier-a7", PROJECT, "a" * 64, "c" * 64, "not-a-digest",
                int(time.time()))
        _assert_error(bad_backup, "invalid_argument", 400)
        updated = store.upgrade_stop_completion(
            "barrier-a7", PROJECT, "a" * 64, "c" * 64, "e" * 64,
            int(time.time()))
        assert updated["configSha256"] == "c" * 64
        assert updated["upgradedFrom"] == "a" * 64
        assert store.reopen_after_completed_stop(
            "barrier-a7", PROJECT, "c" * 64, int(time.time())) is True


def test_full_backup_requires_clean_stop_when_requested(tmp_path):
    seeded = _seed(tmp_path)
    with pytest.raises(DomainError) as not_stopped:
        _backup(seeded, tmp_path / "backup", require_clean_stop=True)
    _assert_error(not_stopped, "backup_requires_clean_stop", 409)
    assert not (tmp_path / "backup").exists()

    _stop(seeded)
    _backup(seeded, tmp_path / "backup-stopped", require_clean_stop=True)
    manifest = json.loads((tmp_path / "backup-stopped" / "manifest.json")
                          .read_text(encoding="utf-8"))
    assert manifest["runtime_state"]["clean_stop"] is True
    assert manifest["runtime_state"]["stop"]["barrierId"] == "barrier-a7"


def test_backup_copy_failure_and_cancel_leave_no_partial_or_old_loss(
        tmp_path, monkeypatch):
    seeded = _seed(tmp_path)
    old = tmp_path / "backup-old"
    _backup(seeded, old)
    old_manifest = (old / "manifest.json").read_bytes()
    source_before = {
        path.relative_to(seeded["workspace_root"]).as_posix(): _sha256(path)
        for path in seeded["workspace_root"].rglob("*") if path.is_file()}

    def disk_failure(source, destination):
        raise DomainError("backup_write_failed", 500)

    monkeypatch.setattr(control_cli, "_copy_file", disk_failure)
    with pytest.raises(DomainError) as failed:
        _backup(seeded, tmp_path / "backup-failed")
    _assert_error(failed, "backup_write_failed", 500)
    assert not (tmp_path / "backup-failed").exists()
    assert (old / "manifest.json").read_bytes() == old_manifest

    def cancelled(source, destination):
        raise KeyboardInterrupt()

    monkeypatch.setattr(control_cli, "_copy_file", cancelled)
    with pytest.raises(KeyboardInterrupt):
        _backup(seeded, tmp_path / "backup-cancelled")
    assert not (tmp_path / "backup-cancelled").exists()
    assert (old / "manifest.json").read_bytes() == old_manifest
    source_after = {
        path.relative_to(seeded["workspace_root"]).as_posix(): _sha256(path)
        for path in seeded["workspace_root"].rglob("*") if path.is_file()}
    assert source_after == source_before
