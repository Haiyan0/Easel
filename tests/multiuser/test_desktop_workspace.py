"""A4.4：完整 Desktop 开发编排与初始化/启动/停止/诊断契约。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "desktop"
COMPOSE = DEPLOY / "compose.workspace.yaml"
INITIALIZE = DEPLOY / "Initialize-WorkspaceEasel.ps1"
START = DEPLOY / "Start-WorkspaceEasel.ps1"
STOP = DEPLOY / "Stop-WorkspaceEasel.ps1"
STATUS = DEPLOY / "Status-WorkspaceEasel.ps1"
RESTORE = DEPLOY / "Restore-WorkspaceEasel.ps1"
COMMON = DEPLOY / "EaselDesktop.Common.ps1"
KEYGEN = DEPLOY / "keygen.py"


def _assert_no_authtoken_read(text: str) -> None:
    for line in text.splitlines():
        if "authtoken" not in line.lower():
            continue
        lowered = line.lower()
        assert "get-content" not in lowered
        assert "select-string" not in lowered
        assert not lowered.strip().startswith("cat ")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_workspace_compose_binds_signing_keys_and_gateway_mode():
    compose = _compose()
    control = compose["services"]["control"]
    command = control["command"]
    assert "--gateway-required" in command
    assert "--snapshot-key-file" in command
    assert "/keys/snapshot.pem" in command
    assert "--managed-agent-config" in command
    assert "/keys/managed-agent.json" in command
    assert "--provisioner-socket" in command
    assert "/run/easel/provisioner.sock" in command
    assert "--egress-policy" in command
    assert "--egress-proxy" in command
    # 正式编排必须能表达受管发布策略；不能只留 serve-control 手动参数。
    assert "--publish-mode" in command
    mode_value = command[command.index("--publish-mode") + 1]
    assert "EASEL_PUBLISH_MODE" in mode_value
    assert mode_value.startswith("${EASEL_PUBLISH_MODE")
    assert control["user"] == "10001:10001"
    assert control["read_only"] is True
    assert control["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in control["security_opt"]
    assert control["ports"] == [
        "127.0.0.1:${EASEL_CONTROL_PORT:-7861}:7861"]
    volumes = control["volumes"]
    assert "control:/srv/easel/control" in volumes
    assert "keys:/keys:ro" in volumes
    assert any("provisioner.sock:ro" in str(item) for item in volumes)
    assert not any("docker.sock" in str(item) for item in volumes)
    assert not any("/workspace" in str(item) for item in volumes)


def test_workspace_compose_egress_uses_policy_volume_only():
    compose = _compose()
    egress = compose["services"]["egress"]
    assert "ports" not in egress
    assert egress["read_only"] is True
    assert egress["cap_drop"] == ["ALL"]
    assert egress["user"] == "10001:10001"
    assert egress["volumes"] == ["egress:/policy:ro"]
    assert not any("docker.sock" in str(item)
                   for item in egress["volumes"])
    assert "/policy/egress-policy.json" in egress["command"]
    assert "/keys" not in " ".join(str(part) for part in egress["command"])
    assert "command" in egress and any(
        "egress_proxy.py" in str(part) for part in egress["command"])
    assert compose["networks"]["control"]["external"] is True
    assert compose["volumes"]["keys"]["external"] is True
    assert compose["volumes"]["control"]["external"] is True
    assert compose["volumes"]["egress"]["external"] is True
    assert compose["volumes"]["egress"]["name"].startswith(
        "${EASEL_EGRESS_VOLUME")


def test_initialize_binds_key_creation_and_private_network():
    text = INITIALIZE.read_text(encoding="utf-8")
    assert "--signing-key" in text
    assert "--worker-public" in text
    assert "--gateway-public" in text
    assert "easel.desktop.role=control-network" in text
    assert "Assert-EaselNetworkContract" in text
    assert "workspace-sidecar" in text
    assert "managed-agent.json" in text
    assert "egress-policy.json" in text
    assert "egress-policy" in text
    assert "volumes.Egress" in text
    assert "/policy/egress-policy.json" in text
    _assert_no_authtoken_read(text)
    assert "chmod 777" not in text
    assert "chmod 666" not in text


def test_restore_reuses_registered_publish_mode():
    text = RESTORE.read_text(encoding="utf-8")
    assert "manifest.deployment.publish_mode" in text
    assert "-PublishMode" in text
    assert "backup_manifest_invalid" in text


def test_start_exposes_managed_publish_mode_parameter():
    text = START.read_text(encoding="utf-8")
    assert "$PublishMode" in text
    assert "EASEL_PUBLISH_MODE" in text
    assert "publish_mode" in text
    # 旧部署没有登记策略时必须显式迁移，不能静默按新默认放开自动发布。
    assert "publish_mode_migration_required" in text
    assert "deployment_project_mismatch" in text


def test_start_diagnoses_missing_dependencies_before_starting():
    text = (START.read_text(encoding="utf-8") +
            COMMON.read_text(encoding="utf-8"))
    for marker in (
            "worker-public.key", "public_file_missing",
            "managed_config_missing", "workspace_keys_invalid",
            "frontend_build_missing", "wsl_python_dependencies_missing",
            "provisioner_start_timeout", "compose_config_invalid",
            "control_health_timeout", "Assert-EaselNetworkContract",
            "Assert-EaselVolumeContract", "public_copy_mismatch",
            "docker_engine_mismatch", "provisioner_gid_mismatch"):
        assert marker.lower() in text.lower()
    for marker in (
            "provisioner-expected.json", "Invoke-ProvisionerIdentityCheck",
            "provisioner_identity_unavailable",
            "provisioner_config_mismatch",
            "provisioner_identity_mismatch", "admission_barrier_active",
            "public_copy_mismatch", "docker_engine_mismatch",
            "Get-EaselKeyFingerprints", "Get-EaselEngineIdentity",
            "admin", "reopen", "Enter-EaselLifecycleLock",
            "LockTimeoutSeconds"):
        assert marker in text
    _assert_no_authtoken_read(text)


def test_stop_drains_before_stopping_and_status_fails_closed():
    stop = STOP.read_text(encoding="utf-8")
    assert "admin drain" in stop
    assert "--barrier" in stop
    assert "admission_barrier" in stop
    assert "drain_verified" in stop
    assert "stop_completed" in stop
    assert "stop-complete" in stop
    assert "DataRoot" in stop
    assert "Enter-EaselLifecycleLock" in stop
    assert "LockTimeoutSeconds" in stop
    assert "worker_drain_failed" in stop
    assert "'down'" in stop
    assert "provisioner_socket_missing" in stop
    status = STATUS.read_text(encoding="utf-8")
    for marker in (
            "worker_leases", "api_health", "provisioner_socket",
            "Add-Failure", "status=failed", "exit 1",
            "control_mounts", "workspace_keys", "workspace_config",
            "egress_policy_mount", "provisioner_identity",
            "admission_barrier"):
        assert marker in status
    _assert_no_authtoken_read(status)


def test_keygen_fingerprints_are_derived_from_the_real_private_key(
        tmp_path):
    import hashlib

    def make(directory: Path):
        directory.mkdir()
        command = [
            sys.executable, str(KEYGEN), "--ensure",
            "--path", str(directory / "master.key"),
            "--signing-key", str(directory / "snapshot.pem"),
            "--worker-public", str(directory / "worker-public.key"),
            "--gateway-public", str(directory / "gateway-public.pem"),
        ]
        result = subprocess.run(command, capture_output=True, text=True,
                                check=False)
        assert result.returncode == 0, result.stderr
        check = subprocess.run(
            command[:2] + ["--check"] + command[3:] + ["--fingerprints"],
            capture_output=True, text=True, check=False)
        assert check.returncode == 0, check.stderr
        assert "PRIVATE KEY" not in check.stdout
        assert check.stdout.count("_sha256=") == 2
        values = {}
        for line in check.stdout.splitlines():
            name, separator, value = line.partition("=")
            if separator and name.endswith("_sha256"):
                values[name] = value
        workers = (directory / "worker-public.key").read_bytes()
        gateway = (directory / "gateway-public.pem").read_bytes()
        assert values["worker_public_sha256"] == hashlib.sha256(
            workers).hexdigest()
        assert values["gateway_public_sha256"] == hashlib.sha256(
            gateway).hexdigest()
        return values

    first = make(tmp_path / "first")
    second = make(tmp_path / "second")
    assert first["worker_public_sha256"] != second["worker_public_sha256"]
    assert first["gateway_public_sha256"] != second["gateway_public_sha256"]


def test_keygen_cli_creates_master_and_signing_keys(tmp_path):
    master = tmp_path / "master.key"
    private = tmp_path / "snapshot.pem"
    raw = tmp_path / "worker-public.key"
    pem = tmp_path / "gateway-public.pem"
    command = [
        sys.executable, str(KEYGEN), "--ensure",
        "--path", str(master),
        "--signing-key", str(private),
        "--worker-public", str(raw),
        "--gateway-public", str(pem),
    ]
    first = subprocess.run(command, capture_output=True, text=True,
                           check=False)
    assert first.returncode == 0, first.stderr
    assert "key=created" in first.stdout
    assert "signing_key=created" in first.stdout
    original_private = private.read_bytes()
    original_master = master.read_bytes()
    second = subprocess.run(command, capture_output=True, text=True,
                            check=False)
    assert second.returncode == 0, second.stderr
    assert "key=exists" in second.stdout
    assert "signing_key=exists" in second.stdout
    assert private.read_bytes() == original_private
    assert master.read_bytes() == original_master
    check = subprocess.run(
        [sys.executable, str(KEYGEN), "--check",
         "--path", str(master), "--signing-key", str(private),
         "--worker-public", str(raw), "--gateway-public", str(pem)],
        capture_output=True, text=True, check=False)
    assert check.returncode == 0, check.stderr
    assert "signing_key=exists" in check.stdout
