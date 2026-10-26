"""Task 13：控制库备份、受保护恢复与显式本机迁移。

所有用例只使用 ``tmp_path`` 下的临时数据库、密钥和工作区目录；不读取开发者
真实 HOME、outputs、浏览器目录或任何平台凭证。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path, PurePosixPath

import pytest

from easel.cli import main
from easel.control.auth import AuthService
from easel.control.cli import (
    MigrationReport,
    backup_control,
    create_backup,
    migrate_local,
    restore_backup,
)
from easel.control.config import ConfigService
from easel.control.runtime import RuntimeManager
from easel.control.store import SCHEMA_VERSION, Store
from easel.control.types import DomainError, WorkerHandle, WorkspacePaths
from easel.worker.sessions import WorkspaceSessionStore


def _write_key(path: Path, data: bytes = b"k" * 32) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if os.name == "posix":
        path.chmod(0o600)
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_error(error: pytest.ExceptionInfo, code: str, status: int) -> None:
    assert isinstance(error.value, DomainError)
    assert (error.value.code, error.value.status) == (code, status)


def _admin_and_member(store: Store):
    admin, invite = AuthService(store).bootstrap_admin("Owner")
    member = store.create_user("Member")
    member_invite = AuthService(store).create_invite(admin, member.user_id)
    return admin, member, invite, member_invite


def _publish(store: Store, admin, key: bytes) -> None:
    ConfigService(store, key).publish(admin, {"defaults": {"speech": {
        "provider": "openai-compatible",
        "model": "tts-default",
        "base_url": "https://provider.example/v1",
        "protocol": "openai",
        "secret": "admin-secret-sentinel",
        "options": {},
    }}}, expected_version=0)


def _make_junction(link: Path, target: Path) -> None:
    """测试专用 junction/symlink；Windows 无符号链接特权时用 junction。"""
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, check=False,
        )
        if result.returncode != 0:
            pytest.skip("当前 Windows 账户无法创建 junction")
    else:
        link.symlink_to(target, target_is_directory=True)


def _is_link(path: Path) -> bool:
    return bool(path.is_symlink()
                or getattr(path.lstat(), "st_reparse_tag", 0))


def _workspace_files(root: Path, workspace_id: str) -> None:
    data = root / workspace_id / "workspace" / "projects" / "demo.txt"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_text("project-data", encoding="utf-8")
    home = root / workspace_id / "home" / "profile.txt"
    home.parent.mkdir(parents=True, exist_ok=True)
    home.write_text("profile-data", encoding="utf-8")
    credentials = root / workspace_id / ".credentials" / f"{workspace_id}.env"
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text(
        "EASEL_SERVICE_TOKEN=service-token-sentinel\n", encoding="utf-8")


def _seed_bundle(tmp_path: Path):
    control = tmp_path / "control"
    workspace_root = tmp_path / "workspaces"
    key_file = _write_key(tmp_path / "master.key")
    with Store.open(control / "control.sqlite3") as store:
        admin, member, invite, member_invite = _admin_and_member(store)
        conversation = store.create_conversation(member)
        _publish(store, admin, key_file.read_bytes())
        _workspace_files(workspace_root, member.workspace_id)
        bundle = tmp_path / "backup"
        create_backup(
            store, bundle,
            control_dir=control,
            master_key_file=key_file,
            workspace_root=workspace_root,
            image_digest="sha256:" + "a" * 64,
            harness_revision="harness-r1",
        )
    return {
        "bundle": bundle, "control": control, "workspace_root": workspace_root,
        "key": key_file.read_bytes(), "admin": admin, "member": member,
        "conversation": conversation, "invite": invite,
        "member_invite": member_invite,
    }


def test_backup_restores_workspace_ownership_in_new_directory(tmp_path):
    with Store.open(tmp_path / "source" / "db") as original:
        user = original.create_user("A")
        conversation = original.create_conversation(user)
        backup = backup_control(original, tmp_path / "restored" / "db")
    with Store.open(backup) as restored:
        assert restored.get_conversation(
            user, conversation.id).workspace_id == user.workspace_id


def test_control_snapshot_is_consistent_and_does_not_overwrite(tmp_path):
    with Store.open(tmp_path / "control" / "control.sqlite3") as original:
        user = original.create_user("A")
        original.create_conversation(user)
        backup = backup_control(original, tmp_path / "snapshot" / "db")
        original.create_conversation(user)

        with pytest.raises(DomainError) as exists:
            backup_control(original, backup)
        _assert_error(exists, "backup_exists", 409)

    with Store.open(backup) as restored:
        assert len(restored.list_conversations(user)) == 1


def test_control_snapshot_drops_plaintext_worker_service_token(tmp_path):
    with Store.open(tmp_path / "control" / "control.sqlite3") as original:
        user = original.create_user("A")
        outcome, lease = original.claim_worker_start(
            user.workspace_id, 0, "owner-1", int(time.time()), 120)
        assert outcome == "started" and lease is not None
        handle = WorkerHandle(
            user.workspace_id, "d" * 64, "http://worker",
            "worker-token-sentinel", 0)
        assert original.complete_worker_start(
            user.workspace_id, "owner-1", lease.generation, handle,
            int(time.time()), 120)
        original._connection.execute(
            "UPDATE worker_leases SET state = 'stopped' WHERE workspace_id = ?",
            (user.workspace_id,))
        backup = backup_control(original, tmp_path / "snapshot" / "db")

    database_bytes = backup.read_bytes()
    wal = backup.with_name(backup.name + "-wal")
    if wal.exists():
        database_bytes += wal.read_bytes()
    assert b"worker-token-sentinel" not in database_bytes
    with Store.open(backup) as restored:
        assert restored.get_worker_lease(user.workspace_id).service_token == ""


def test_backup_bundle_copies_control_key_and_workspace_without_secrets(tmp_path):
    seeded = _seed_bundle(tmp_path)
    bundle = seeded["bundle"]
    member = seeded["member"]

    assert (bundle / "control" / "control.sqlite3").is_file()
    assert (bundle / "master.key").is_file()
    assert (bundle / "master.key").read_bytes() == seeded["key"]
    restored_workspace = bundle / "workspaces" / member.workspace_id
    assert (restored_workspace / "workspace" / "projects" / "demo.txt").read_text(
        encoding="utf-8") == "project-data"
    assert (restored_workspace / "home" / "profile.txt").read_text(
        encoding="utf-8") == "profile-data"
    assert not (restored_workspace / ".credentials").exists()

    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["format"] == "easel.control.backup"
    assert manifest["version"] == 1
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["config_version"] == 1
    assert manifest["workspace_config_versions"][member.workspace_id] == 0
    assert manifest["image"] == "sha256:" + "a" * 64
    assert manifest["harness_revision"] == "harness-r1"
    assert manifest["control"]["sha256"] == _sha256(
        bundle / "control" / "control.sqlite3")
    assert manifest["master_key"]["sha256"] == hashlib.sha256(
        seeded["key"]).hexdigest()

    manifest_bytes = manifest_path.read_bytes()
    for secret in (
            seeded["invite"], seeded["member_invite"],
            "admin-secret-sentinel", "service-token-sentinel"):
        assert secret.encode() not in manifest_bytes


def test_backup_without_workspace_root_copies_control_and_key_only(tmp_path):
    control = tmp_path / "control"
    key_file = _write_key(tmp_path / "master.key")
    destination = tmp_path / "backup-control-only"
    with Store.open(control / "control.sqlite3") as store:
        store.create_user("A")
        create_backup(
            store, destination, control_dir=control,
            master_key_file=key_file)
    manifest = json.loads(
        (destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["workspaces"]
    assert all(not entry for entry in manifest["workspaces"].values())
    assert (destination / "control" / "control.sqlite3").is_file()


def test_backup_refuses_active_worker_until_drained(tmp_path):
    control = tmp_path / "control"
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    key_file = _write_key(tmp_path / "master.key")
    destination = tmp_path / "backup"
    with Store.open(control / "control.sqlite3") as store:
        user = store.create_user("A")
        outcome, lease = store.claim_worker_start(
            user.workspace_id, 0, "owner-1", int(time.time()), 120)
        assert outcome == "started" and lease is not None

        with pytest.raises(DomainError) as active:
            create_backup(
                store, destination, control_dir=control,
                master_key_file=key_file, workspace_root=workspace_root)
        _assert_error(active, "worker_drain_required", 409)
        assert not destination.exists()

        drained = []

        def drain():
            drained.append(lease.workspace_id)
            store.stop_worker(
                user.workspace_id, int(time.time()), generation=lease.generation)

        create_backup(
            store, destination, control_dir=control,
            master_key_file=key_file, workspace_root=workspace_root,
            drain=drain)
    assert drained == [user.workspace_id]
    assert (destination / "manifest.json").is_file()


def test_backup_rejects_master_key_mismatch_before_writing(tmp_path):
    control = tmp_path / "control"
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    good = _write_key(tmp_path / "master.key", b"a" * 32)
    wrong = _write_key(tmp_path / "wrong.key", b"b" * 32)
    destination = tmp_path / "backup"
    with Store.open(control / "control.sqlite3") as store:
        admin, _member, _invite, _member_invite = _admin_and_member(store)
        _publish(store, admin, good.read_bytes())
        with pytest.raises(DomainError) as mismatch:
            create_backup(
                store, destination, control_dir=control,
                master_key_file=wrong, workspace_root=workspace_root)
        _assert_error(mismatch, "master_key_mismatch", 409)
    assert not destination.exists()


def test_backup_refuses_destination_outside_managed_root(tmp_path):
    control = tmp_path / "control"
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    key_file = _write_key(tmp_path / "master.key")
    outside = tmp_path.parent / f"{tmp_path.name}-outside-backup"
    with Store.open(control / "control.sqlite3") as store:
        with pytest.raises(DomainError) as escape:
            create_backup(
                store, outside, control_dir=control,
                master_key_file=key_file, workspace_root=workspace_root)
        _assert_error(escape, "backup_destination_escape", 400)
    assert not outside.exists()


def _rewrite_manifest(bundle: Path, mutate) -> dict:
    path = bundle / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest)
    path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8")
    return manifest


def _update_file_digest(manifest: dict, bundle: Path, path: str) -> None:
    target = bundle / path
    for entry in manifest["files"]:
        if entry["path"] == path:
            entry["sha256"] = _sha256(target)
            entry["bytes"] = target.stat().st_size
            if path == "master.key":
                manifest["master_key"]["sha256"] = entry["sha256"]
                manifest["master_key"]["bytes"] = entry["bytes"]
            return
    raise AssertionError(f"manifest entry missing: {path}")


def test_restore_bundle_to_new_root_preserves_ownership_key_and_files(tmp_path):
    seeded = _seed_bundle(tmp_path)
    source_db = seeded["bundle"] / "control" / "control.sqlite3"
    source_digest = _sha256(source_db)
    destination = tmp_path / "restore"

    restored = restore_backup(seeded["bundle"], destination)

    assert restored == destination
    assert (destination / "control" / "control.sqlite3").is_file()
    assert (destination / "manifest.json").is_file()
    assert (destination / "master.key").read_bytes() == seeded["key"]
    assert _sha256(source_db) == source_digest, "恢复不得改动备份源"
    assert (destination / "workspaces" / seeded["member"].workspace_id
            / "workspace" / "projects" / "demo.txt").read_text(
                encoding="utf-8") == "project-data"
    assert (destination / "workspaces" / seeded["member"].workspace_id
            / "home" / "profile.txt").read_text(
                encoding="utf-8") == "profile-data"

    with Store.open(destination / "control" / "control.sqlite3") as store:
        conversation = store.get_conversation(
            seeded["member"], seeded["conversation"].id)
        assert conversation.workspace_id == seeded["member"].workspace_id
        ConfigService(store, (destination / "master.key").read_bytes())


def test_restore_refuses_non_empty_running_control_directory(tmp_path):
    seeded = _seed_bundle(tmp_path)
    destination = tmp_path / "restore"
    destination.mkdir()
    running = destination / "control.sqlite3"
    running.write_bytes(b"running-control")

    with pytest.raises(DomainError) as occupied:
        restore_backup(seeded["bundle"], destination)
    _assert_error(occupied, "restore_target_not_empty", 409)
    assert running.read_bytes() == b"running-control"


def test_restore_rejects_missing_master_key_without_partial_destination(tmp_path):
    seeded = _seed_bundle(tmp_path)
    (seeded["bundle"] / "master.key").unlink()
    destination = tmp_path / "restore"

    with pytest.raises(DomainError) as missing:
        restore_backup(seeded["bundle"], destination)
    _assert_error(missing, "backup_master_key_missing", 409)
    assert not destination.exists()


def test_restore_rejects_wrong_master_key_without_resetting_secrets(tmp_path):
    seeded = _seed_bundle(tmp_path)
    bundle = seeded["bundle"]
    (bundle / "master.key").write_bytes(b"z" * 32)
    _rewrite_manifest(
        bundle, lambda manifest: _update_file_digest(
            manifest, bundle, "master.key"))
    destination = tmp_path / "restore"

    with pytest.raises(DomainError) as mismatch:
        restore_backup(bundle, destination)
    _assert_error(mismatch, "master_key_mismatch", 409)
    assert not destination.exists()


def test_restore_rejects_tampered_control_and_workspace_files(tmp_path):
    seeded = _seed_bundle(tmp_path)
    control = seeded["bundle"] / "control" / "control.sqlite3"
    control.write_bytes(control.read_bytes() + b"tampered")
    with pytest.raises(DomainError) as database:
        restore_backup(seeded["bundle"], tmp_path / "restore-db")
    _assert_error(database, "backup_integrity_failed", 409)
    assert not (tmp_path / "restore-db").exists()

    other = _seed_bundle(tmp_path / "second")
    workspace_file = (other["bundle"] / "workspaces"
                      / other["member"].workspace_id
                      / "workspace" / "projects" / "demo.txt")
    workspace_file.write_text("tampered", encoding="utf-8")
    with pytest.raises(DomainError) as data:
        restore_backup(other["bundle"], tmp_path / "restore-data")
    _assert_error(data, "backup_integrity_failed", 409)
    assert not (tmp_path / "restore-data").exists()


def test_restore_rejects_manifest_path_escape_and_extra_files(tmp_path):
    seeded = _seed_bundle(tmp_path)
    _rewrite_manifest(
        seeded["bundle"],
        lambda manifest: manifest["files"][0].__setitem__("path", "../escape.txt"))
    with pytest.raises(DomainError) as escape:
        restore_backup(seeded["bundle"], tmp_path / "restore")
    _assert_error(escape, "backup_invalid", 400)
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "restore").exists()

    other = _seed_bundle(tmp_path / "second")
    (other["bundle"] / "unlisted.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(DomainError) as extra:
        restore_backup(other["bundle"], tmp_path / "restore-extra")
    _assert_error(extra, "backup_integrity_failed", 409)
    assert not (tmp_path / "restore-extra").exists()


def test_restore_rejects_manifest_pointer_mismatch(tmp_path):
    seeded = _seed_bundle(tmp_path)
    _rewrite_manifest(
        seeded["bundle"],
        lambda manifest: manifest["control"].__setitem__("path", "master.key"))
    with pytest.raises(DomainError) as mismatch:
        restore_backup(seeded["bundle"], tmp_path / "restore")
    _assert_error(mismatch, "backup_invalid", 400)
    assert not (tmp_path / "restore").exists()


def test_restore_rejects_future_manifest_and_destination_outside_managed_root(tmp_path):
    seeded = _seed_bundle(tmp_path)
    _rewrite_manifest(
        seeded["bundle"], lambda manifest: manifest.__setitem__("version", 999))
    with pytest.raises(DomainError) as future:
        restore_backup(seeded["bundle"], tmp_path / "restore")
    _assert_error(future, "backup_incompatible", 409)
    assert not (tmp_path / "restore").exists()

    other = _seed_bundle(tmp_path / "second")
    managed = tmp_path / "second" / "managed"
    managed.mkdir()
    outside = tmp_path / "outside-restore"
    with pytest.raises(DomainError) as escape:
        restore_backup(other["bundle"], outside, managed_root=managed)
    _assert_error(escape, "restore_target_escape", 400)
    assert not outside.exists()


def test_backup_rejects_junction_workspace_persistent_root(tmp_path):
    control = tmp_path / "control"
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    key_file = _write_key(tmp_path / "master.key")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside-data", encoding="utf-8")
    destination = tmp_path / "backup"

    with Store.open(control / "control.sqlite3") as store:
        user = store.create_user("A")
        owner = workspace_root / user.workspace_id
        owner.mkdir()
        _make_junction(owner / "workspace", outside)
        with pytest.raises(DomainError) as rejected:
            create_backup(
                store, destination, control_dir=control,
                master_key_file=key_file, workspace_root=workspace_root)
        _assert_error(rejected, "backup_symlink_rejected", 409)
    assert not destination.exists()


def test_runtime_drain_all_stops_each_ready_worker_before_backup(tmp_path):
    with Store.open(tmp_path / "control" / "control.sqlite3") as store:
        user = store.create_user("A")
        outcome, lease = store.claim_worker_start(
            user.workspace_id, 0, "owner-1", int(time.time()), 120)
        assert outcome == "started" and lease is not None
        handle = WorkerHandle(
            user.workspace_id, "c" * 64, "http://worker", "service-token", 0)
        assert store.complete_worker_start(
            user.workspace_id, "owner-1", lease.generation, handle,
            int(time.time()), 120)
        stopped: list[tuple[str, str]] = []

        class Provisioner:
            async def stop(self, workspace_id, instance_id):
                stopped.append((workspace_id, instance_id))

        manager = RuntimeManager(store, Provisioner())
        drained = asyncio.run(manager.drain_all())

        assert drained == [user.workspace_id]
        assert stopped == [(user.workspace_id, "c" * 64)]
        assert store.get_worker_lease(user.workspace_id).state == "stopped"


def test_backup_rejects_junction_inside_workspace(tmp_path):
    control = tmp_path / "control"
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    key_file = _write_key(tmp_path / "master.key")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside-data", encoding="utf-8")
    destination = tmp_path / "backup"
    with Store.open(control / "control.sqlite3") as store:
        user = store.create_user("A")
        workspace = workspace_root / user.workspace_id / "workspace"
        workspace.mkdir(parents=True)
        _make_junction(workspace / "link", outside)
        with pytest.raises(DomainError) as rejected:
            create_backup(
                store, destination, control_dir=control,
                master_key_file=key_file, workspace_root=workspace_root)
        _assert_error(rejected, "backup_symlink_rejected", 409)
    assert not destination.exists()


def _tree_fingerprint(root: Path) -> tuple:
    records: list[tuple] = []

    def visit(directory: Path, relative: PurePosixPath) -> None:
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            path = Path(entry.path)
            shown = (relative / entry.name).as_posix()
            info = entry.stat(follow_symlinks=False)
            if path.is_symlink() or getattr(info, "st_reparse_tag", 0):
                records.append(("link", shown, os.readlink(path)
                                if path.is_symlink() else "junction"))
                continue
            if entry.is_dir(follow_symlinks=False):
                records.append(("dir", shown))
                visit(path, relative / entry.name)
            elif entry.is_file(follow_symlinks=False):
                records.append(("file", shown, info.st_size, _sha256(path)))
            else:
                records.append(("other", shown))
    visit(root, PurePosixPath())
    return tuple(records)


def _legacy_session(content_root: Path, key: str, messages: list[dict] | None,
                    binding: dict | None = None) -> tuple[str, str]:
    digest = hashlib.sha256(key.encode()).hexdigest()
    session_id = f"local-{digest}"
    sessions = content_root / "_sessions"
    if messages is not None:
        path = sessions / "api" / f"{digest}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
    if binding is not None:
        path = sessions / "harness" / f"{digest}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(binding, ensure_ascii=False), encoding="utf-8")
    return session_id, digest


def _seed_migration_source(tmp_path: Path, *, compatible: bool = False):
    install = tmp_path / "local-install"
    outputs = install / "outputs"
    project = outputs / "projects" / "demo.txt"
    project.parent.mkdir(parents=True)
    project.write_text("project-data", encoding="utf-8")
    (outputs / "_harness").mkdir(parents=True)
    (outputs / "_harness" / "config.json").write_text(
        '{"runtime":"api"}', encoding="utf-8")
    (outputs / "_browser").mkdir()
    (outputs / "_browser" / "cookie.txt").write_text(
        "browser-cookie-sentinel", encoding="utf-8")
    (outputs / ".env").write_text(
        "EASEL_SECRET=local-env-sentinel\n", encoding="utf-8")

    api_id, api_digest = _legacy_session(
        outputs, "api-session",
        [{"role": "user", "content": "api-question"},
         {"role": "assistant", "content": "api-answer"}])
    binding = {"runtime": "acp", "native_id": "native-abc"}
    if compatible:
        binding["cwd"] = "/workspace"
    native_id, native_digest = _legacy_session(
        outputs, "native-session",
        [{"role": "user", "content": "native-question"}],
        binding)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    return {
        "install": install, "outputs": outputs, "workspace_root": workspace_root,
        "workspace_id": "a" * 32, "api_id": api_id, "api_digest": api_digest,
        "native_id": native_id, "native_digest": native_digest,
        "binding": binding,
    }


def test_migration_dry_run_lists_without_writing_or_backup(tmp_path):
    seeded = _seed_migration_source(tmp_path)
    report = migrate_local(
        seeded["install"], workspace_root=seeded["workspace_root"],
        workspace=seeded["workspace_id"], dry_run=True)

    assert isinstance(report, MigrationReport)
    assert "projects/demo.txt" in report.imported
    assert seeded["api_id"] in report.imported
    assert seeded["native_id"] in report.imported
    assert report.non_resumable_sessions == [seeded["native_id"]]
    assert report.rejected == []
    target = (seeded["workspace_root"] / seeded["workspace_id"] / "workspace")
    assert not target.exists()
    assert not (seeded["workspace_root"] / ".migration-backups").exists()


def test_migration_imports_static_content_and_sessions_without_touching_source(
        tmp_path):
    seeded = _seed_migration_source(tmp_path)
    before = _tree_fingerprint(seeded["outputs"])
    backup = seeded["workspace_root"] / "migration-backup"
    report = migrate_local(
        seeded["install"], workspace_root=seeded["workspace_root"],
        workspace=seeded["workspace_id"], backup_destination=backup)

    target = seeded["workspace_root"] / seeded["workspace_id"] / "workspace"
    outputs = target / "outputs"
    state = target / "state"
    assert (outputs / "projects" / "demo.txt").read_text(
        encoding="utf-8") == "project-data"
    assert not (target / "projects").exists()
    assert not (outputs / "_harness").exists()
    assert not (outputs / "_browser").exists()
    assert not (outputs / ".env").exists()
    assert _tree_fingerprint(seeded["outputs"]) == before

    sessions = WorkspaceSessionStore(state, seeded["workspace_id"])
    assert [message["content"] for message in sessions.messages(
        seeded["api_id"])] == ["api-question", "api-answer"]
    assert sessions.binding(seeded["api_id"])["runtime"] == "api"
    assert seeded["native_id"] in report.non_resumable_sessions
    assert seeded["native_id"] in report.imported
    native_static = state / "migrated-sessions" / seeded["native_id"]
    assert "native-abc" in (native_static / "session.json").read_text(
        encoding="utf-8")
    assert "native-question" in (native_static / "messages.jsonl").read_text(
        encoding="utf-8")
    assert not (state / "sessions"
                / hashlib.sha256(seeded["native_id"].encode()).hexdigest()
                / "binding.json").exists()
    assert not (target / "sessions").exists()

    assert (backup / "projects" / "demo.txt").is_file()
    assert (backup / "_sessions" / "api"
            / f"{seeded['api_digest']}.json").is_file()
    assert "local-env-sentinel" not in (backup / "projects" / "demo.txt").read_text(
        encoding="utf-8")


def test_migration_isolates_source_state_and_sessions_from_worker_state(
        tmp_path):
    seeded = _seed_migration_source(tmp_path)
    (seeded["outputs"] / "state").mkdir()
    (seeded["outputs"] / "state" / "static.txt").write_text(
        "static-state", encoding="utf-8")
    (seeded["outputs"] / "sessions").mkdir()
    (seeded["outputs"] / "sessions" / "static.txt").write_text(
        "static-sessions", encoding="utf-8")

    migrate_local(
        seeded["install"], workspace_root=seeded["workspace_root"],
        workspace=seeded["workspace_id"])

    target = seeded["workspace_root"] / seeded["workspace_id"] / "workspace"
    assert (target / "outputs" / "state" / "static.txt").read_text(
        encoding="utf-8") == "static-state"
    assert (target / "outputs" / "sessions" / "static.txt").read_text(
        encoding="utf-8") == "static-sessions"
    state = target / "state"
    assert not (state / "static.txt").exists()
    sessions = WorkspaceSessionStore(state, seeded["workspace_id"])
    assert [item["content"] for item in sessions.messages(
        seeded["api_id"])] == ["api-question", "api-answer"]


def test_migration_imports_compatible_native_binding_without_replacing_id(
        tmp_path):
    seeded = _seed_migration_source(tmp_path, compatible=True)
    report = migrate_local(
        seeded["install"], workspace_root=seeded["workspace_root"],
        workspace=seeded["workspace_id"])

    assert report.non_resumable_sessions == []
    target = seeded["workspace_root"] / seeded["workspace_id"] / "workspace"
    sessions = WorkspaceSessionStore(target / "state", seeded["workspace_id"])
    binding = sessions.binding(seeded["native_id"])
    assert binding["native_id"] == "native-abc"
    assert binding["cwd"] == "/workspace"
    assert [message["content"] for message in sessions.messages(
        seeded["native_id"])] == ["native-question"]


def test_migration_refuses_non_empty_target_and_invalid_workspace(tmp_path):
    seeded = _seed_migration_source(tmp_path)
    target = seeded["workspace_root"] / seeded["workspace_id"] / "workspace"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(DomainError) as occupied:
        migrate_local(
            seeded["install"], workspace_root=seeded["workspace_root"],
            workspace=seeded["workspace_id"])
    _assert_error(occupied, "migration_target_not_empty", 409)
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"

    with pytest.raises(DomainError) as invalid:
        migrate_local(
            seeded["install"], workspace_root=seeded["workspace_root"],
            workspace="../escape", dry_run=True)
    _assert_error(invalid, "migration_workspace_invalid", 400)


def test_migration_refuses_outputs_root_through_junction(tmp_path):
    source = tmp_path / "local-install"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside-data", encoding="utf-8")
    _make_junction(source / "outputs", outside)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()

    with pytest.raises(DomainError) as escape:
        migrate_local(
            source, workspace_root=workspace_root,
            workspace="b" * 32, dry_run=True)
    _assert_error(escape, "migration_source_escape", 400)


def test_migration_refuses_target_through_junction_outside_root(tmp_path):
    seeded = _seed_migration_source(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _make_junction(seeded["workspace_root"] / seeded["workspace_id"], outside)

    with pytest.raises(DomainError) as escape:
        migrate_local(
            seeded["install"], workspace_root=seeded["workspace_root"],
            workspace=seeded["workspace_id"], dry_run=True)
    _assert_error(escape, "migration_target_escape", 400)
    assert not (outside / "workspace").exists()


def test_migration_reports_and_skips_source_junction_escape(tmp_path):
    seeded = _seed_migration_source(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside-data", encoding="utf-8")
    _make_junction(seeded["outputs"] / "projects" / "leak", outside)

    report = migrate_local(
        seeded["install"], workspace_root=seeded["workspace_root"],
        workspace=seeded["workspace_id"])

    assert any(item.startswith("projects/leak: symlink_or_junction")
               for item in report.rejected)
    target = seeded["workspace_root"] / seeded["workspace_id"] / "workspace"
    outputs = target / "outputs"
    assert not (outputs / "projects" / "leak").exists()
    assert not (outputs / "projects" / "leak" / "secret.txt").exists()
    assert (outputs / "projects" / "demo.txt").is_file()


def test_migration_interruption_leaves_no_partial_target_and_can_retry(
        tmp_path, monkeypatch):
    from easel.control import cli as control_cli

    seeded = _seed_migration_source(tmp_path)
    target = seeded["workspace_root"] / seeded["workspace_id"] / "workspace"
    backup = seeded["workspace_root"] / "migration-backup"
    real_copy = control_cli._copy_file

    def interrupted(source, destination):
        if any(part.startswith(".migration-staging-")
               for part in destination.parts):
            raise OSError("simulated interruption")
        return real_copy(source, destination)

    monkeypatch.setattr(control_cli, "_copy_file", interrupted)
    with pytest.raises(DomainError) as failure:
        migrate_local(
            seeded["install"], workspace_root=seeded["workspace_root"],
            workspace=seeded["workspace_id"], backup_destination=backup)
    _assert_error(failure, "migration_write_failed", 500)
    assert not target.exists()
    assert not list(seeded["workspace_root"].glob(".migration-staging-*"))

    monkeypatch.setattr(control_cli, "_copy_file", real_copy)
    report = migrate_local(
        seeded["install"], workspace_root=seeded["workspace_root"],
        workspace=seeded["workspace_id"],
        backup_destination=seeded["workspace_root"] / "migration-backup-2")
    assert "projects/demo.txt" in report.imported
    assert (target / "outputs" / "projects" / "demo.txt").is_file()


def test_migration_detects_source_changed_during_copy(
        tmp_path, monkeypatch):
    from easel.control import cli as control_cli

    seeded = _seed_migration_source(tmp_path)
    real_copy = control_cli._copy_file

    def mutating(source, destination):
        result = real_copy(source, destination)
        if any(part.startswith(".migration-staging-")
               for part in destination.parts):
            Path(source).write_text("changed-by-test", encoding="utf-8")
        return result

    monkeypatch.setattr(control_cli, "_copy_file", mutating)
    with pytest.raises(DomainError) as changed:
        migrate_local(
            seeded["install"], workspace_root=seeded["workspace_root"],
            workspace=seeded["workspace_id"])
    _assert_error(changed, "migration_source_changed", 409)
    target = seeded["workspace_root"] / seeded["workspace_id"] / "workspace"
    assert not target.exists()
    assert not list(seeded["workspace_root"].glob(".migration-staging-*"))


def test_migration_creates_automatic_backup_and_rejects_outside_backup(
        tmp_path):
    seeded = _seed_migration_source(tmp_path)
    report = migrate_local(
        seeded["install"], workspace_root=seeded["workspace_root"],
        workspace=seeded["workspace_id"])
    assert "projects/demo.txt" in report.imported
    backups = list((seeded["workspace_root"] / ".migration-backups").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "projects" / "demo.txt").is_file()
    assert (backups[0] / "migration.json").is_file()
    assert not (backups[0] / ".env").exists()
    assert not (backups[0] / "_harness").exists()
    assert not (backups[0] / "_browser").exists()

    other = _seed_migration_source(tmp_path / "second")
    with pytest.raises(DomainError) as escape:
        migrate_local(
            other["install"], workspace_root=other["workspace_root"],
            workspace=other["workspace_id"],
            backup_destination=tmp_path / "outside-backup")
    _assert_error(escape, "migration_backup_escape", 400)
    target = other["workspace_root"] / other["workspace_id"] / "workspace"
    assert not target.exists()


def _cli_values(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def test_admin_backup_and_restore_cli_round_trip(tmp_path, capsys):
    seeded = _seed_bundle(tmp_path)
    destination = tmp_path / "cli-backup"
    restored = tmp_path / "cli-restore"
    code = main([
        "admin", "backup",
        "--control-dir", str(seeded["control"]),
        "--destination", str(destination),
        "--master-key-file", str(tmp_path / "master.key"),
        "--workspace-root", str(seeded["workspace_root"]),
        "--image-digest", "sha256:" + "b" * 64,
        "--harness-revision", "cli-harness",
    ])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert _cli_values(captured.out)["backup"] == str(destination)
    assert destination.is_dir()

    code = main([
        "admin", "restore",
        "--source", str(destination),
        "--destination", str(restored),
    ])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert _cli_values(captured.out)["restored"] == str(restored)
    with Store.open(restored / "control" / "control.sqlite3") as store:
        assert store.get_conversation(
            seeded["member"], seeded["conversation"].id).workspace_id == (
                seeded["member"].workspace_id)


def test_admin_migrate_local_cli_dry_run_then_actual(tmp_path, capsys):
    seeded = _seed_migration_source(tmp_path)
    target = seeded["workspace_root"] / seeded["workspace_id"] / "workspace"
    base = [
        "admin", "migrate-local",
        "--source", str(seeded["install"]),
        "--workspace", seeded["workspace_id"],
        "--workspace-root", str(seeded["workspace_root"]),
    ]
    code = main([*base, "--dry-run"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    values = _cli_values(captured.out)
    assert values["imported"] == "3"
    assert values["non_resumable"] == "1"
    assert not target.exists()

    code = main(base)
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert _cli_values(captured.out)["imported"] == "3"
    assert (target / "outputs" / "projects" / "demo.txt").is_file()
    backups = list((seeded["workspace_root"] / ".migration-backups").iterdir())
    assert len(backups) == 1


def _git(repo: Path, *args: str, config: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    })
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=True, env=env)


def _git_setup(tmp_path: Path) -> tuple[Path, dict]:
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    owner = workspace_root / ("c" * 32)
    repo = owner / "workspace" / "project"
    repo.mkdir(parents=True)
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n",
                      encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main", config=config)
    (repo / "script.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "script.txt", config=config)
    _git(repo, "commit", "-q", "-m", "v1", config=config)
    _git(repo, "checkout", "-q", "-b", "extra", config=config)
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(repo, "add", "feature.txt", config=config)
    _git(repo, "commit", "-q", "-m", "extra", config=config)
    _git(repo, "checkout", "-q", "main", config=config)
    (repo / "script.txt").write_text("v2-stashed\n", encoding="utf-8")
    _git(repo, "stash", "push", "-q", "-m", "saved-change", "script.txt",
         config=config)
    (repo / "script.txt").write_text("v3-uncommitted\n", encoding="utf-8")

    control = tmp_path / "control"
    key_file = _write_key(tmp_path / "master.key")
    with Store.open(control / "control.sqlite3") as store:
        member = store.create_user("A")
        # 让 owner ID 与真实数据库 workspace 保持一致
        owner.rename(workspace_root / member.workspace_id)
        repo = workspace_root / member.workspace_id / "workspace" / "project"
        bundle = tmp_path / "git-backup"
        create_backup(
            store, bundle, control_dir=control,
            master_key_file=key_file, workspace_root=workspace_root)
    return repo, {"bundle": bundle, "workspace_id": member.workspace_id,
                  "config": config}


def test_migration_uses_worker_outputs_and_state_roots(tmp_path):
    seeded = _seed_migration_source(tmp_path)
    migrate_local(
        seeded["install"], workspace_root=seeded["workspace_root"],
        workspace=seeded["workspace_id"])

    workspace_root = seeded["workspace_root"] / seeded["workspace_id"] / "workspace"
    paths = WorkspacePaths(
        code_root=tmp_path / "code",
        workspace_root=workspace_root,
        state_root=workspace_root / "state",
        home=workspace_root.parent / "home",
    )
    assert (paths.workspace_root / "outputs" / "projects" / "demo.txt").is_file()
    assert not (paths.workspace_root / "projects" / "demo.txt").exists()
    sessions = WorkspaceSessionStore(paths.state_root, seeded["workspace_id"])
    assert [item["content"] for item in sessions.messages(
        seeded["api_id"])] == ["api-question", "api-answer"]
    assert not (paths.workspace_root / "sessions").exists()
    assert (paths.state_root / "migrated-sessions" / seeded["native_id"]
            / "messages.jsonl").is_file()


def test_backup_rejects_workspace_owner_junction_to_outside(tmp_path):
    control = tmp_path / "control"
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    key_file = _write_key(tmp_path / "master.key")
    outside = tmp_path / "outside"
    (outside / "workspace").mkdir(parents=True)
    sentinel = outside / "workspace" / "sentinel.txt"
    sentinel.write_text("outside-sentinel", encoding="utf-8")
    destination = tmp_path / "backup"
    with Store.open(control / "control.sqlite3") as store:
        user = store.create_user("A")
        _make_junction(workspace_root / user.workspace_id, outside)
        with pytest.raises(DomainError) as rejected:
            create_backup(
                store, destination, control_dir=control,
                master_key_file=key_file, workspace_root=workspace_root)
        _assert_error(rejected, "backup_workspace_path_escape", 409)
    assert not destination.exists()
    assert sentinel.read_text(encoding="utf-8") == "outside-sentinel"


def test_backup_rejects_workspace_owner_junction_to_other_workspace(tmp_path):
    control = tmp_path / "control"
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    key_file = _write_key(tmp_path / "master.key")
    destination = tmp_path / "backup"
    with Store.open(control / "control.sqlite3") as store:
        first = store.create_user("A")
        second = store.create_user("B")
        other = workspace_root / second.workspace_id / "workspace"
        other.mkdir(parents=True)
        (other / "other.txt").write_text("other-workspace", encoding="utf-8")
        _make_junction(workspace_root / first.workspace_id,
                       workspace_root / second.workspace_id)
        with pytest.raises(DomainError) as rejected:
            create_backup(
                store, destination, control_dir=control,
                master_key_file=key_file, workspace_root=workspace_root)
        _assert_error(rejected, "backup_workspace_path_escape", 409)
    assert not destination.exists()


def test_backup_restore_preserves_real_git_history_and_worktree(tmp_path):
    repo, seeded = _git_setup(tmp_path)
    manifest = json.loads(
        (seeded["bundle"] / "manifest.json").read_text(encoding="utf-8"))
    assert any("/project/.git/" in entry["path"]
               for entry in manifest["files"])
    restored = tmp_path / "restore"
    restore_backup(seeded["bundle"], restored)
    restored_repo = (restored / "workspaces" / seeded["workspace_id"]
                     / "workspace" / "project")
    config = seeded["config"]

    assert (restored_repo / "script.txt").read_text(encoding="utf-8") == (
        "v3-uncommitted\n")
    assert "v1" in _git(restored_repo, "show", "HEAD:script.txt",
                        config=config).stdout
    branches = _git(restored_repo, "branch", "--list", "extra",
                    config=config).stdout
    assert "extra" in branches
    stashes = _git(restored_repo, "stash", "list", config=config).stdout
    assert "saved-change" in stashes
    assert "v2-stashed" in _git(restored_repo, "stash", "show", "-p",
                                "stash@{0}", config=config).stdout
    assert "script.txt" in _git(restored_repo, "status", "--porcelain",
                                config=config).stdout
    assert _git(repo, "status", "--porcelain", config=config).stdout


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX 执行位在 Windows 文件系统上不可验证")
def test_backup_restore_preserves_executable_mode(tmp_path):
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    control = tmp_path / "control"
    key_file = _write_key(tmp_path / "master.key")
    with Store.open(control / "control.sqlite3") as store:
        user = store.create_user("A")
        script = (workspace_root / user.workspace_id / "workspace"
                  / "run.sh")
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
        bundle = tmp_path / "backup"
        create_backup(
            store, bundle, control_dir=control,
            master_key_file=key_file, workspace_root=workspace_root)
    restored = tmp_path / "restore"
    restore_backup(bundle, restored)
    restored_script = (restored / "workspaces" / user.workspace_id
                       / "workspace" / "run.sh")
    assert restored_script.stat().st_mode & 0o777 == 0o755


def test_migration_dry_run_and_actual_accept_same_empty_subdirectory(tmp_path):
    seeded = _seed_migration_source(tmp_path)
    target = (seeded["workspace_root"] / seeded["workspace_id"]
              / "workspace")
    (target / "empty").mkdir(parents=True)
    base = {"workspace_root": seeded["workspace_root"],
            "workspace": seeded["workspace_id"]}
    dry = migrate_local(seeded["install"], dry_run=True, **base)
    assert "projects/demo.txt" in dry.imported

    report = migrate_local(seeded["install"], **base)
    assert "projects/demo.txt" in report.imported
    assert (target / "outputs" / "projects" / "demo.txt").is_file()


def test_migration_accepts_pure_empty_target_and_rejects_link_content(
        tmp_path):
    seeded = _seed_migration_source(tmp_path)
    target = (seeded["workspace_root"] / seeded["workspace_id"]
              / "workspace")
    target.mkdir(parents=True)
    migrate_local(
        seeded["install"], workspace_root=seeded["workspace_root"],
        workspace=seeded["workspace_id"])
    assert (target / "outputs" / "projects" / "demo.txt").is_file()

    other = _seed_migration_source(tmp_path / "second")
    other_target = (other["workspace_root"] / other["workspace_id"]
                    / "workspace")
    other_target.mkdir(parents=True)
    outside = tmp_path / "second" / "outside"
    outside.mkdir()
    _make_junction(other_target / "link", outside)
    with pytest.raises(DomainError) as linked:
        migrate_local(
            other["install"], workspace_root=other["workspace_root"],
            workspace=other["workspace_id"], dry_run=True)
    _assert_error(linked, "migration_target_not_empty", 409)
    assert (other_target / "link").exists()


def _swap_owner_after_precheck(tmp_path, monkeypatch, seeded, replacement):
    from easel.control import cli as control_cli

    owner = seeded["workspace_root"] / seeded["workspace_id"]
    real_fingerprint = control_cli._source_fingerprint
    calls = 0

    def swapping_fingerprint(root):
        nonlocal calls
        result = real_fingerprint(root)
        calls += 1
        if calls == 2:
            _make_junction(owner, replacement)
        return result

    monkeypatch.setattr(
        control_cli, "_source_fingerprint", swapping_fingerprint)
    return owner


def test_migration_rejects_owner_swapped_to_outside_junction_before_commit(
        tmp_path, monkeypatch):
    seeded = _seed_migration_source(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = tmp_path / "outside-sentinel.txt"
    sentinel.write_text("outside-sentinel", encoding="utf-8")
    source_before = _tree_fingerprint(seeded["outputs"])
    owner = _swap_owner_after_precheck(
        tmp_path, monkeypatch, seeded, outside)

    with pytest.raises(DomainError) as escape:
        migrate_local(
            seeded["install"], workspace_root=seeded["workspace_root"],
            workspace=seeded["workspace_id"])
    _assert_error(escape, "migration_target_escape", 400)
    assert not (outside / "workspace").exists()
    assert not (outside / "workspace" / "outputs").exists()
    assert not (outside / "workspace" / "state").exists()
    assert sentinel.read_text(encoding="utf-8") == "outside-sentinel"
    assert owner.exists() and _is_link(owner)
    assert _tree_fingerprint(seeded["outputs"]) == source_before
    assert not list(seeded["workspace_root"].glob(".migration-staging-*"))


def test_migration_rejects_owner_swapped_to_other_workspace_before_commit(
        tmp_path, monkeypatch):
    seeded = _seed_migration_source(tmp_path)
    other_id = "b" * 32
    other_owner = seeded["workspace_root"] / other_id
    other_owner.mkdir()
    owner = _swap_owner_after_precheck(
        tmp_path, monkeypatch, seeded, other_owner)

    with pytest.raises(DomainError) as escape:
        migrate_local(
            seeded["install"], workspace_root=seeded["workspace_root"],
            workspace=seeded["workspace_id"])
    _assert_error(escape, "migration_target_escape", 400)
    assert not (other_owner / "workspace").exists()
    assert owner.exists() and _is_link(owner)
    assert not list(seeded["workspace_root"].glob(".migration-staging-*"))


def test_migration_preserves_data_created_after_precheck(
        tmp_path, monkeypatch):
    from easel.control import cli as control_cli

    seeded = _seed_migration_source(tmp_path)
    target = (seeded["workspace_root"] / seeded["workspace_id"]
              / "workspace")
    real_import = control_cli._import_sessions

    def import_with_late_file(*args, **kwargs):
        real_import(*args, **kwargs)
        target.mkdir(parents=True, exist_ok=True)
        (target / "late.txt").write_text("user-data", encoding="utf-8")

    monkeypatch.setattr(control_cli, "_import_sessions", import_with_late_file)
    with pytest.raises(DomainError) as occupied:
        migrate_local(
            seeded["install"], workspace_root=seeded["workspace_root"],
            workspace=seeded["workspace_id"])
    _assert_error(occupied, "migration_target_not_empty", 409)
    assert (target / "late.txt").read_text(encoding="utf-8") == "user-data"
    assert not list(seeded["workspace_root"].glob(".migration-staging-*"))


def test_admin_backup_cli_rejects_relative_destination_without_secrets(
        tmp_path, capsys):
    seeded = _seed_bundle(tmp_path)
    code = main([
        "admin", "backup",
        "--control-dir", str(seeded["control"]),
        "--destination", "relative-backup",
        "--master-key-file", str(tmp_path / "master.key"),
        "--workspace-root", str(seeded["workspace_root"]),
    ])
    captured = capsys.readouterr()
    assert code != 0
    assert "backup_destination_invalid" in captured.err
    assert seeded["invite"] not in captured.err
    assert not (Path.cwd() / "relative-backup").exists()
