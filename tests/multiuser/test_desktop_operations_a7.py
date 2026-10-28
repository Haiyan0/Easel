"""A7：Desktop 备份/校验/恢复/升级入口与手册路径契约。

只读脚本文本和 Compose 配置；真实 Docker 演练在
``tests/integration/multiuser/test_desktop_operations_real.py`` 单独计数。
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "desktop"
BACKUP = DEPLOY / "Backup-WorkspaceEasel.ps1"
VERIFY = DEPLOY / "Verify-WorkspaceEasel.ps1"
RESTORE = DEPLOY / "Restore-WorkspaceEasel.ps1"
LOGS = DEPLOY / "Logs-WorkspaceEasel.ps1"
START = DEPLOY / "Start-WorkspaceEasel.ps1"
STATUS = DEPLOY / "Status-WorkspaceEasel.ps1"
COMMON = DEPLOY / "EaselDesktop.Common.ps1"
COMPOSE = DEPLOY / "compose.workspace.yaml"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _assert_no_secret_reads(text: str) -> None:
    lowered = text.lower()
    assert "get-content" not in lowered or ".env" not in lowered
    for marker in ("--no-sandbox", "docker.sock", "chmod 777", "chmod 666"):
        assert marker not in lowered


def test_backup_requires_stopped_instance_and_full_materials():
    text = _text(BACKUP) + _text(COMMON)
    for marker in (
            "backup_requires_stopped", "--signing-key-file",
            "--managed-agent-config", "--egress-policy-file",
            "--egress-ca-file", "--deployment-json",
            "--require-clean-stop", "Set-EaselPrivateAcl",
            "Assert-EaselBackupManifest", "backup_workspace_root_missing"):
        assert marker in text
    _assert_no_secret_reads(text)
    assert "docker volume rm" not in text
    assert "system prune" not in text


def test_verify_is_read_only_and_checks_complete_bundle():
    text = _text(VERIFY)
    assert "--read-only" in text
    assert "verify-backup" in text
    assert "Verify-WorkspaceEasel" not in text  # 直接调用 CLI，不递归脚本
    assert "master.key" not in text  # 不主动读取/打印私钥
    assert "backup" in text.lower()


def test_restore_refuses_overwrite_and_rotates_recovery_credentials():
    text = _text(RESTORE)
    for marker in (
            "restore_target_exists", "restore_target_not_empty",
            "restore_target_same_project", "restore_target_overlaps_source",
            "restore_target_contains_backup", "prepare-restore",
            "materialize-restore", "recovery_invite",
            "admin-invite.txt", "Set-EaselPrivateAcl",
            "restore_permission_failed", "Assert-EaselBackupManifest"):
        assert marker in text
    _assert_no_secret_reads(text)
    assert "'volume', 'rm', '-f'" in text
    assert "system prune" not in text


def test_start_has_controlled_upgrade_and_writes_deployment_identity():
    text = _text(START) + _text(COMMON)
    for marker in (
            "UpgradeFromBackup", "upgrade-complete", "stop-status",
            "backup-manifest-sha256", "upgrade_stop_completion_mismatch",
            "deployment.json", "config_sha256", "frontend_sha256",
            "harness_revision", "egress-ca.pem"):
        assert marker in text or marker.lower() in text.lower()
    assert "admin resume" not in text
    assert "clear_admission_barrier" not in text


def test_compose_binds_egress_ca_without_publishing_ports():
    compose = yaml.safe_load(_text(COMPOSE))
    command = compose["services"]["control"]["command"]
    assert "--egress-ca-file" in command
    assert "/keys/egress-ca.pem" in command
    assert compose["services"]["control"]["environment"][
        "SSL_CERT_FILE"] == "/keys/egress-ca.pem"
    assert compose["services"]["egress"].get("ports") is None


def test_start_rejects_port_origin_image_volume_and_socket_failures():
    text = _text(START) + _text(COMMON)
    for marker in (
            "control_port_in_use", "invalid_origin", "control_image_unavailable",
            "control_image_mismatch", "worker_or_gateway_image_mismatch",
            "volume_not_owned", "network_not_owned", "volume_inspect_failed",
            "wsl_unavailable", "wsl_repo_missing",
            "wsl_python_dependencies_missing", "provisioner_start_timeout",
            "provisioner_identity_mismatch", "provisioner_config_mismatch",
            "admission_barrier_active"):
        assert marker in text, marker


def test_status_and_logs_expose_version_without_secret_material():
    status = _text(STATUS)
    for marker in (
            "version_control_image", "version_worker_image",
            "version_gateway_image", "version_frontend_sha256",
            "version_config_sha256", "version_schema", "version_source_commit",
            "version_harness_revision", "logs="):
        assert marker in status
    logs = _text(LOGS)
    assert "docker" in logs
    assert "tail" in logs
    assert "master.key" not in logs
    assert "snapshot.pem" not in logs
    assert "--master-key-file" not in status
    assert "read_bytes" not in status
