"""Task 8：受限 provisioner 的请求校验、argv 构造与脱敏 inspect。

本文件不连接 Docker：所有注入、路径越界与配额能力判定都必须在执行任何
容器命令之前失败。真实容器行为由 Linux 集成测试覆盖。
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "deploy" / "multiuser" / "provisioner.py"


def _load():
    spec = importlib.util.spec_from_file_location(
        "easel_provisioner_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    # dataclass 需要能从 sys.modules 解析所属模块
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provisioner = _load()
WORKSPACE = "a" * 32
IMAGE = "sha256:" + "b" * 64


def _ensure_payload(**overrides):
    payload = {
        "version": 1,
        "action": "ensure",
        "workspace_id": WORKSPACE,
        "config_version": 3,
    }
    payload.update(overrides)
    return payload


def test_parse_request_accepts_only_the_structured_actions():
    parsed = provisioner.parse_request(_ensure_payload())
    assert parsed.action == "ensure"
    assert parsed.workspace_id == WORKSPACE
    assert parsed.config_version == 3
    for action in ("stop", "inspect"):
        request = provisioner.parse_request({
            "version": 1, "action": action, "workspace_id": WORKSPACE})
        assert request.action == action
        assert request.config_version == 0


@pytest.mark.parametrize("field", [
    "image", "command", "cmd", "entrypoint", "volume", "volumes", "mount",
    "mounts", "network", "env", "environment", "path", "workspace_root",
    "docker_args", "argv", "privileged", "user", "cap_add",
])
def test_parse_request_rejects_any_injection_field_before_docker(field):
    with pytest.raises(provisioner.ProvisionerError) as rejected:
        provisioner.parse_request(_ensure_payload(**{field: "x"}))
    assert rejected.value.code == "unknown_field"


@pytest.mark.parametrize("workspace_id", [
    "", "..", "../etc", "A" * 32, "a" * 31, "a" * 33, "g" * 32,
    "a" * 31 + "\n", 123, None, "a" * 32 + "/../../x",
])
def test_parse_request_rejects_invalid_workspace_uuid(workspace_id):
    with pytest.raises(provisioner.ProvisionerError):
        provisioner.parse_request(_ensure_payload(workspace_id=workspace_id))


@pytest.mark.parametrize("value", [-1, True, "3", 3.0, None, 2 ** 31])
def test_parse_request_rejects_invalid_config_version(value):
    with pytest.raises(provisioner.ProvisionerError):
        provisioner.parse_request(_ensure_payload(config_version=value))


def test_parse_request_rejects_oversized_and_malformed_payloads():
    with pytest.raises(provisioner.ProvisionerError):
        provisioner.parse_request(b"{" + b" " * provisioner.MAX_REQUEST_BYTES)
    with pytest.raises(provisioner.ProvisionerError):
        provisioner.parse_request(b"not json")
    with pytest.raises(provisioner.ProvisionerError):
        provisioner.parse_request([])
    with pytest.raises(provisioner.ProvisionerError):
        provisioner.parse_request({"version": 2, "action": "inspect",
                                   "workspace_id": WORKSPACE})


def test_prepare_workspace_rejects_symlink_escape(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    try:
        (root / WORKSPACE).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不允许创建目录符号链接")

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        provisioner.prepare_workspace(root, WORKSPACE)
    assert rejected.value.code == "workspace_escape"
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "secret"


@pytest.mark.skipif(sys.platform != "win32", reason="仅验证 Windows junction")
def test_prepare_workspace_rejects_windows_junction_escape(tmp_path):
    """junction 不是 symlink，但 resolve 后仍会越界，必须同样拒绝。"""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(root / WORKSPACE), str(outside)],
        capture_output=True)
    if created.returncode != 0:
        pytest.skip("当前环境不允许创建 junction")

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        provisioner.prepare_workspace(root, WORKSPACE)
    assert rejected.value.code == "workspace_escape"
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "secret"
    assert not (outside / "workspace").exists()


def test_prepare_workspace_creates_private_directories_under_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    paths = provisioner.prepare_workspace(root, WORKSPACE)
    assert paths.workspace.is_dir()
    assert paths.home.is_dir()
    assert paths.state.is_dir()
    assert paths.workspace.parent == paths.root
    assert paths.workspace.name == "workspace"
    assert paths.home.name == "home"
    assert paths.state.name == "state"


def test_worker_state_root_lives_inside_the_workspace_mount(tmp_path):
    """state_root 必须落在唯一数据挂载内，否则容器内路径不存在。"""
    paths = provisioner.prepare_workspace(tmp_path / "root", WORKSPACE)
    assert paths.state == paths.workspace / "state"
    assert paths.state.is_dir()


def test_workspace_ownership_is_chowned_for_the_worker_uid(tmp_path,
                                                          monkeypatch):
    paths = provisioner.prepare_workspace(tmp_path / "root", WORKSPACE)
    calls = []
    monkeypatch.setattr(provisioner.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        provisioner.os, "chown",
        lambda path, uid, gid: calls.append((path, uid, gid)), raising=False)
    provisioner.ensure_worker_ownership(paths)
    assert {call[1] for call in calls} == {10001}
    assert {call[2] for call in calls} == {10001}
    assert {call[0] for call in calls} == {
        paths.workspace, paths.home, paths.state}


def test_workspace_ownership_fails_closed_when_not_chownable(tmp_path,
                                                             monkeypatch):
    paths = provisioner.prepare_workspace(tmp_path / "root", WORKSPACE)
    monkeypatch.setattr(provisioner.os, "geteuid", lambda: 1234, raising=False)

    class Foreign:
        st_uid = 1234
        st_mode = 0o700

    monkeypatch.setattr(provisioner.Path, "stat", lambda self: Foreign())
    with pytest.raises(provisioner.ProvisionerError) as rejected:
        provisioner.ensure_worker_ownership(paths)
    assert rejected.value.code == "workspace_owner_mismatch"


def test_build_run_argv_hardens_the_container_without_secrets_in_argv(
        tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    paths = provisioner.prepare_workspace(root, WORKSPACE)
    env_file = provisioner.credentials_dir(root) / f"{WORKSPACE}.env"
    argv = provisioner.build_run_argv(
        image=IMAGE, workspace_id=WORKSPACE, config_version=3, paths=paths,
        env_file=env_file)

    joined = "\n".join(argv)
    assert argv[:2] == ["run", "-d"]
    assert "--name" in argv
    assert provisioner.container_name(WORKSPACE) in argv
    assert "--user" in argv and argv[argv.index("--user") + 1] == "10001:10001"
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges:true" in argv
    assert "--cpus=2" in argv
    assert "--memory=4g" in argv
    assert "--memory-swap=4g" in argv
    assert "--pids-limit=256" in argv
    assert "/tmp:rw,noexec,nosuid,nodev,size=1g" in argv
    assert "--network" in argv
    assert argv[argv.index("--network") + 1] == provisioner.network_name(
        WORKSPACE)
    assert "--env-file" in argv
    assert argv[argv.index("--env-file") + 1] == str(env_file)
    assert "--privileged" not in joined
    assert "--publish" not in joined and "\n-p\n" not in joined
    assert "docker.sock" not in joined
    assert "EASEL_SERVICE_TOKEN" not in joined
    assert argv[-8:] == ["python", "-m", "easel.worker.lifecycle",
                         "serve", "--host", "0.0.0.0", "--port", "7860"]
    assert argv[argv.index(IMAGE) + 1:][:1] == ["python"]
    labels = provisioner.workload_labels(WORKSPACE, 3)
    assert labels == {
        "easel.managed": "true",
        "easel.workspace-id": WORKSPACE,
        "easel.config-version": "3",
    }
    assert "--label" in argv


def test_build_run_argv_rejects_unpinned_images(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    paths = provisioner.prepare_workspace(root, WORKSPACE)
    for image in ("easel-worker:latest", "easel-worker", "sha256:xyz",
                  "sha256:" + "a" * 63):
        with pytest.raises(provisioner.ProvisionerError):
            provisioner.build_run_argv(
                image=image, workspace_id=WORKSPACE, config_version=3,
                paths=paths, env_file=tmp_path / "env")


def test_peer_authorization_rejects_unknown_or_unavailable_credentials():
    assert provisioner.peer_allowed(1000, {1000})
    assert not provisioner.peer_allowed(1001, {1000})
    assert not provisioner.peer_allowed(None, {1000})
    assert not provisioner.peer_allowed(0, set())


def test_sanitize_inspect_drops_env_paths_and_unknown_labels():
    raw = {
        "Id": "f" * 64,
        "Name": "/" + provisioner.container_name(WORKSPACE),
        "Config": {
            "User": "10001:10001",
            "Env": ["EASEL_SERVICE_TOKEN=super-secret",
                    "EASEL_ADMIN_KEY=admin-sentinel"],
            "Labels": {
                "easel.managed": "true",
                "easel.workspace-id": WORKSPACE,
                "easel.config-version": "3",
                "com.example.leak": "host-detail",
            },
        },
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "HostConfig": {
            "ReadonlyRootfs": True,
            "Privileged": False,
            "CapDrop": ["ALL"],
            "PidsLimit": 256,
            "Memory": 4294967296,
            "NanoCpus": 2000000000,
            "SecurityOpt": ["no-new-privileges:true"],
        },
        "Mounts": [{"Source": "/host/secret/root", "Destination": "/workspace"}],
        "NetworkSettings": {
            "Ports": {},
            "Networks": {
                provisioner.network_name(WORKSPACE): {
                    "IPAddress": "172.30.0.2"},
            },
        },
    }
    report = provisioner.sanitize_inspect(raw)
    encoded = json.dumps(report)
    assert "super-secret" not in encoded
    assert "admin-sentinel" not in encoded
    assert "/host/secret/root" not in encoded
    assert "com.example.leak" not in encoded
    assert report["workspace_id"] == WORKSPACE
    assert report["container_id"] == provisioner.container_name(WORKSPACE)
    assert report["config_version"] == 3
    assert report["ready"] is True
    assert report["managed"] is True
    assert report["upstream"] == "http://172.30.0.2:7860"
    assert report["security"] == {
        "user": "10001:10001",
        "read_only": True,
        "privileged": False,
        "cap_drop": ["ALL"],
        "no_new_privileges": True,
        "pids_limit": 256,
        "memory_bytes": 4294967296,
        "nano_cpus": 2000000000,
    }


def test_sanitize_inspect_refuses_unmanaged_or_foreign_containers():
    foreign = {
        "Name": "/easel-worker-other",
        "Config": {"Labels": {"easel.workspace-id": "b" * 32}},
        "State": {"Running": True},
    }
    report = provisioner.sanitize_inspect(foreign)
    assert report["managed"] is False
    assert report["container_id"] == ""
    assert report["ready"] is False
    assert report["upstream"] == ""


def test_quota_capability_must_be_verified_not_assumed(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv):
        calls.append(argv)
        return 1  # 查询失败：不能当成限额已生效

    with pytest.raises(provisioner.QuotaUnavailable):
        provisioner.ensure_quota(
            tmp_path, WORKSPACE, mode="project", limit_bytes=10 * 1024 ** 3,
            allow_unquotaed=False, run=fake_run)
    assert calls, "必须实际尝试验证配额能力"


def test_quota_disabled_requires_explicit_opt_in(tmp_path):
    with pytest.raises(provisioner.QuotaUnavailable):
        provisioner.ensure_quota(
            tmp_path, WORKSPACE, mode="none",
            limit_bytes=10 * 1024 ** 3, allow_unquotaed=False,
            run=lambda argv: 0)
    provisioner.ensure_quota(
        tmp_path, WORKSPACE, mode="none", limit_bytes=10 * 1024 ** 3,
        allow_unquotaed=True, run=lambda argv: 0)
