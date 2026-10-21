"""Task 8：受限 provisioner 的请求校验、argv 构造与脱敏 inspect。

本文件不连接 Docker：所有注入、路径越界与配额能力判定都必须在执行任何
容器命令之前失败。真实容器行为由 Linux 集成测试覆盖。
"""
from __future__ import annotations

import asyncio
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
    stop = provisioner.parse_request({
        "version": 1, "action": "stop", "workspace_id": WORKSPACE,
        "instance_id": "f" * 64})
    assert stop.action == "stop" and stop.instance_id == "f" * 64
    inspect = provisioner.parse_request({
        "version": 1, "action": "inspect", "workspace_id": WORKSPACE})
    assert inspect.action == "inspect"
    assert inspect.config_version == 0


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
    identities = dict(zip(
        (paths.workspace, paths.home, paths.state), paths.directory_ids))

    class Foreign:
        st_uid = 1234
        st_mode = 0o040700

        def __init__(self, identity):
            self.st_dev, self.st_ino = identity

    monkeypatch.setattr(
        provisioner, "_stat_directory",
        lambda path: Foreign(identities[path]))
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


# ----------------------------------------------------------------------
# R1：实际 ensure 调用链、目录身份与旧实例严格验收
# ----------------------------------------------------------------------

def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


def _fail(stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 1, "", stderr)


def _not_found(target: str) -> subprocess.CompletedProcess:
    return _fail(f"Error: No such object: {target}\n")


def _valid_container(root: pathlib.Path, config_version: int = 3,
                     image: str = IMAGE) -> dict:
    paths = provisioner.prepare_workspace(root, WORKSPACE)
    return {
        "Id": "f" * 64,
        "Name": "/" + provisioner.container_name(WORKSPACE),
        "Config": {
            "User": "10001:10001",
            "Image": image,
            "Entrypoint": None,
            "Cmd": ["python", "-m", "easel.worker.lifecycle",
                    "serve", "--host", "0.0.0.0", "--port", "7860"],
            "Env": ["EASEL_SERVICE_TOKEN=super-secret"],
            "Labels": provisioner.workload_labels(WORKSPACE, config_version),
        },
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "HostConfig": {
            "ReadonlyRootfs": True,
            "Privileged": False,
            "CapDrop": ["ALL"],
            "CapAdd": [],
            "SecurityOpt": ["no-new-privileges:true"],
            "PidsLimit": 256,
            "Memory": 4 * 1024 ** 3,
            "MemorySwap": 4 * 1024 ** 3,
            "NanoCpus": 2_000_000_000,
            "NetworkMode": provisioner.network_name(WORKSPACE),
            "PortBindings": {},
            "PublishAllPorts": False,
            "Binds": [],
            "Devices": [],
            "PidMode": "",
            "IpcMode": "private",
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=1g"},
            "RestartPolicy": {"Name": "no"},
        },
        "Mounts": [
            {"Type": "bind", "Source": str(paths.workspace),
             "Destination": "/workspace", "RW": True},
            {"Type": "bind", "Source": str(paths.home),
             "Destination": "/home/easel", "RW": True},
        ],
        "NetworkSettings": {
            "Ports": {},
            "Networks": {
                provisioner.network_name(WORKSPACE): {
                    "IPAddress": "172.30.0.2"},
            },
        },
    }


def _valid_network() -> dict:
    return {
        "Name": provisioner.network_name(WORKSPACE),
        "Driver": "bridge",
        "Internal": True,
        "EnableIPv6": False,
        "Labels": {
            "easel.managed": "true",
            "easel.workspace-id": WORKSPACE,
        },
    }


class FakeDocker:
    """模拟 Docker 外部边界；记录实际 argv 并返回可配置的 inspect。"""

    def __init__(self, root: pathlib.Path, *, existing: dict | None = None,
                 network: dict | None = None,
                 after_run: dict | None = None,
                 run_returncode: int = 0,
                 stop_failure: bool = False,
                 rm_failure: bool = False,
                 inspect_error: str | None = None,
                 instance_inspect_error: str | None = None):
        self.root = root
        self.container = existing
        self.network = network
        self.after_run = after_run
        self.run_returncode = run_returncode
        self.stop_failure = stop_failure
        self.rm_failure = rm_failure
        self.inspect_error = inspect_error
        self.instance_inspect_error = instance_inspect_error
        self.calls: list[list[str]] = []
        self.stopped: list[str] = []
        self.removed: list[str] = []

    def __call__(self, argv, *, timeout=120.0):
        self.calls.append(list(argv))
        assert argv[0] == "docker", argv
        args = argv[1:]
        if self.inspect_error is not None and args[:2] in (
                ["container", "inspect"], ["network", "inspect"]):
            return _fail(self.inspect_error)
        if args[:2] == ["network", "inspect"]:
            if self.network is None:
                name = provisioner.network_name(WORKSPACE)
                return _fail(
                    f"Error response from daemon: network {name} not found\n")
            return _ok(json.dumps([self.network]))
        if args[:2] == ["network", "create"]:
            self.network = _valid_network()
            return _ok(provisioner.network_name(WORKSPACE) + "\n")
        if args[:2] == ["container", "inspect"]:
            target = args[2]
            if (self.instance_inspect_error is not None
                    and len(target) == 64
                    and all(character in "0123456789abcdef"
                            for character in target)):
                return _fail(self.instance_inspect_error)
            if (self.container is None
                    or (target not in (
                        provisioner.container_name(WORKSPACE),
                        self.container.get("Id")))):
                return _not_found(target)
            return _ok(json.dumps([self.container]))
        if args[:1] == ["run"]:
            if self.run_returncode != 0:
                return subprocess.CompletedProcess(argv, self.run_returncode,
                                                   "", "")
            self.container = self.after_run or _valid_container(self.root)
            return _ok("fake-container-id\n")
        if args[:1] == ["stop"]:
            target = args[-1]
            self.stopped.append(target)
            if self.stop_failure:
                return _fail("Error response from daemon: stop failed\n")
            if self.container is None or self.container.get("Id") != target:
                return _not_found(target)
            self.container["State"] = {
                "Running": False, "Status": "exited"}
            return _ok()
        if args[:1] == ["rm"]:
            target = args[-1]
            self.removed.append(target)
            if self.rm_failure:
                return _fail("Error response from daemon: rm failed\n")
            if (self.container is None
                    or target not in (
                        provisioner.container_name(WORKSPACE),
                        self.container.get("Id"))):
                return _not_found(target)
            self.container = None
            return _ok()
        raise AssertionError(f"unexpected docker 调用：{argv}")

    def run_argv(self) -> list[str]:
        return next(call for call in self.calls if call[1:2] == ["run"])


def _settings(tmp_path: pathlib.Path) -> object:
    return provisioner.ProvisionerSettings(
        image=IMAGE, workspace_root=tmp_path / "root",
        socket_path=tmp_path / "provisioner.sock",
        quota_mode="none", allow_unquotaed=True)


def _fake_health(monkeypatch):
    def fake_get(host, port, path, headers, timeout):
        return {
            "workspace_id": WORKSPACE,
            "config_version": 3,
            "container_id": provisioner.container_name(WORKSPACE),
            "ready": True,
        }
    monkeypatch.setattr(provisioner, "_http_get_json", fake_get)


def test_ensure_uses_full_run_argv_and_returns_after_real_health_chain(
        tmp_path, monkeypatch):
    """实际 ensure→_docker→runner 链必须携带 docker run，而不是从 -d 开始。"""
    root = tmp_path / "root"
    root.mkdir()
    fake = FakeDocker(root)
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)
    _fake_health(monkeypatch)

    handle = asyncio.run(service.ensure(WORKSPACE, 3))

    run_argv = fake.run_argv()
    assert run_argv[:3] == ["docker", "run", "-d"], run_argv[:5]
    assert run_argv[-1] == "7860"
    assert handle.workspace_id == WORKSPACE
    assert handle.container_id == "f" * 64
    assert handle.upstream == "http://172.30.0.2:7860"
    assert handle.service_token


def test_ensure_rejects_container_with_insecure_actual_config(
        tmp_path, monkeypatch):
    """旧容器即使标签和健康响应合法，安全配置不符也不得接管。"""
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)

    def tamper(raw):
        raw["Config"]["User"] = "0:0"
        raw["HostConfig"]["Privileged"] = True
        raw["HostConfig"]["ReadonlyRootfs"] = False
        raw["Config"]["Image"] = "easel-worker:latest"
        raw["NetworkSettings"]["Networks"]["bridge"] = {
            "IPAddress": "172.17.0.2"}
        raw["HostConfig"]["PortBindings"] = {"7860/tcp": [
            {"HostIp": "0.0.0.0", "HostPort": "7860"}]}
        return raw

    existing = tamper(_valid_container(root))
    fake = FakeDocker(root, existing=existing)
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.ensure(WORKSPACE, 3))
    assert rejected.value.code == "container_conflict"
    assert all(call[1:2] != ["run"] for call in fake.calls), fake.calls


_TAMPER_NAMES = [
    "root_user", "privileged", "writable_rootfs", "wrong_image",
    "extra_network", "published_port", "cap_add", "missing_nnp",
    "nnp_false", "nnp_substring", "nnp_conflict",
    "wrong_memory", "wrong_pids", "wrong_cpu", "extra_mount",
    "writable_workspace_mount", "foreign_command", "docker_socket_mount",
]


def _tampered(name: str, root: pathlib.Path) -> dict:
    raw = _valid_container(root)
    if name == "root_user":
        raw["Config"]["User"] = "0:0"
    elif name == "privileged":
        raw["HostConfig"]["Privileged"] = True
    elif name == "writable_rootfs":
        raw["HostConfig"]["ReadonlyRootfs"] = False
    elif name == "wrong_image":
        raw["Config"]["Image"] = "sha256:" + "e" * 64
    elif name == "extra_network":
        raw["NetworkSettings"]["Networks"]["bridge"] = {
            "IPAddress": "172.17.0.2"}
    elif name == "published_port":
        raw["HostConfig"]["PortBindings"] = {"7860/tcp": [
            {"HostIp": "0.0.0.0", "HostPort": "7860"}]}
    elif name == "cap_add":
        raw["HostConfig"]["CapAdd"] = ["SYS_ADMIN"]
    elif name == "missing_nnp":
        raw["HostConfig"]["SecurityOpt"] = []
    elif name == "nnp_false":
        raw["HostConfig"]["SecurityOpt"] = ["no-new-privileges:false"]
    elif name == "nnp_substring":
        raw["HostConfig"]["SecurityOpt"] = ["not-no-new-privileges:true"]
    elif name == "nnp_conflict":
        raw["HostConfig"]["SecurityOpt"] = [
            "no-new-privileges:true", "no-new-privileges:false"]
    elif name == "wrong_memory":
        raw["HostConfig"]["Memory"] = 8 * 1024 ** 3
    elif name == "wrong_pids":
        raw["HostConfig"]["PidsLimit"] = 0
    elif name == "wrong_cpu":
        raw["HostConfig"]["NanoCpus"] = 0
    elif name == "extra_mount":
        raw["Mounts"].append({
            "Type": "bind", "Source": "/etc", "Destination": "/host-etc",
            "RW": True})
    elif name == "writable_workspace_mount":
        raw["Mounts"][0]["RW"] = False
    elif name == "foreign_command":
        raw["Config"]["Cmd"] = ["sh", "-c", "id"]
    elif name == "docker_socket_mount":
        raw["Mounts"].append({
            "Type": "bind", "Source": "/var/run/docker.sock",
            "Destination": "/var/run/docker.sock", "RW": True})
    return raw


@pytest.mark.parametrize("name", _TAMPER_NAMES)
def test_ensure_rejects_each_tampered_inspect_fixture(tmp_path, monkeypatch,
                                                      name):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    fake = FakeDocker(root, existing=_tampered(name, root))
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.ensure(WORKSPACE, 3))
    assert rejected.value.code == "container_conflict"
    assert all(call[1:2] != ["run"] for call in fake.calls), name


def test_ensure_reuses_fully_valid_existing_container(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    fake = FakeDocker(root, existing=_valid_container(root))
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    handle = asyncio.run(service.ensure(WORKSPACE, 3))
    assert handle.container_id == "f" * 64
    assert all(call[1:2] != ["run"] for call in fake.calls)


def test_ensure_accepts_docker_bare_no_new_privileges_form(tmp_path,
                                                          monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    existing = _valid_container(root)
    existing["HostConfig"]["SecurityOpt"] = ["no-new-privileges"]
    fake = FakeDocker(root, existing=existing)
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    handle = asyncio.run(service.ensure(WORKSPACE, 3))
    assert handle.container_id == existing["Id"]


def test_ensure_replaces_safely_exited_container_without_runtime_ip(
        tmp_path, monkeypatch):
    """安全配置仍合法的已退出实例应被回收重建，而不是拒绝。"""
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    exited = _valid_container(root)
    exited["State"] = {"Running": False, "Status": "exited"}
    exited["NetworkSettings"]["Networks"][
        provisioner.network_name(WORKSPACE)]["IPAddress"] = ""
    fake = FakeDocker(root, existing=exited)
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    handle = asyncio.run(service.ensure(WORKSPACE, 3))
    assert handle.container_id == "f" * 64
    assert any(call[1:2] == ["run"] for call in fake.calls)


def test_inspect_accepts_safely_exited_container_as_not_ready(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    exited = _valid_container(root)
    exited["State"] = {"Running": False, "Status": "exited"}
    exited["NetworkSettings"]["Networks"][
        provisioner.network_name(WORKSPACE)]["IPAddress"] = ""
    fake = FakeDocker(root, existing=exited, network=_valid_network())
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    report = asyncio.run(service.inspect(WORKSPACE))
    assert report["managed"] is True
    assert report["ready"] is False
    assert report["upstream"] == ""


def test_ensure_still_rejects_exited_container_with_insecure_config(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    exited = _tampered("privileged", root)
    exited["State"] = {"Running": False, "Status": "exited"}
    fake = FakeDocker(root, existing=exited)
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.ensure(WORKSPACE, 3))
    assert rejected.value.code == "container_conflict"
    assert all(call[1:2] != ["run"] for call in fake.calls)


@pytest.mark.parametrize("updates", [
    {"Internal": False},
    {"EnableIPv6": True},
    {"Driver": "host"},
    {"Name": "other-network"},
])
def test_ensure_rejects_network_missing_isolation_properties(
        tmp_path, monkeypatch, updates):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    network = _valid_network()
    network.update(updates)
    fake = FakeDocker(root, network=network)
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    with pytest.raises(provisioner.ProvisionerError):
        asyncio.run(service.ensure(WORKSPACE, 3))
    assert all(call[1:2] != ["run"] for call in fake.calls)


def test_ensure_validates_newly_created_network_before_starting(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    fake = FakeDocker(root)
    original = fake.__call__

    def create_insecure(argv, *, timeout=120.0):
        result = original(argv, timeout=timeout)
        if argv[1:3] == ["network", "create"]:
            fake.network = _valid_network()
            fake.network["Internal"] = False
        return result

    service = provisioner.Provisioner(_settings(tmp_path),
                                      run=create_insecure)
    with pytest.raises(provisioner.ProvisionerError):
        asyncio.run(service.ensure(WORKSPACE, 3))
    assert all(call[1:2] != ["run"] for call in fake.calls)


def test_ensure_rejects_insecure_container_created_during_health_loop(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    bad = _valid_container(root)
    bad["HostConfig"]["Privileged"] = True
    fake = FakeDocker(root, after_run=bad)
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    with pytest.raises(provisioner.ProvisionerError):
        asyncio.run(service.ensure(WORKSPACE, 3))
    assert fake.stopped == []


def test_stop_requires_the_exact_instance_identity(tmp_path):
    """stop 只能作用于调用时取得的容器实例，不能按当前同名容器停止。"""
    root = tmp_path / "root"
    root.mkdir()
    existing = _valid_container(root)
    fake = FakeDocker(root, existing=existing)
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    with pytest.raises(provisioner.ProvisionerError):
        asyncio.run(service.stop(WORKSPACE, "e" * 64))
    assert fake.stopped == []
    assert fake.container is existing

    asyncio.run(service.stop(WORKSPACE, existing["Id"]))
    assert fake.stopped == [existing["Id"]]
    assert fake.container is None


@pytest.mark.parametrize("stop_failure,rm_failure,code", [
    (True, False, "container_stop_failed"),
    (False, True, "container_remove_failed"),
    (True, True, "container_stop_failed"),
])
def test_stop_command_failures_are_not_reported_success(
        tmp_path, stop_failure, rm_failure, code):
    root = tmp_path / "root"
    root.mkdir()
    existing = _valid_container(root)
    fake = FakeDocker(root, existing=existing,
                      stop_failure=stop_failure, rm_failure=rm_failure)
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.stop(WORKSPACE, existing["Id"]))
    assert rejected.value.code == code
    assert fake.container is existing, "失败时不得假定实例已消失"
    if stop_failure:
        assert fake.removed == []


_CONTEXT_NOT_FOUND = (
    'Failed to initialize: unable to resolve docker endpoint: '
    'context "codex-review-absent-context-20261021": context not found:\n'
    'open C:\\Users\\review\\.docker\\contexts\\meta\\meta.json: '
    "The system cannot find the path specified.")


def test_context_not_found_is_not_treated_as_container_absence(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    existing = _valid_container(root)
    fake = FakeDocker(root, existing=existing,
                      inspect_error=_CONTEXT_NOT_FOUND)
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.stop(WORKSPACE, existing["Id"]))
    assert rejected.value.code == "docker_inspect_failed"
    assert fake.container is existing
    assert fake.stopped == [] and fake.removed == []


def test_context_not_found_is_not_treated_as_network_absence(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    fake = FakeDocker(root, inspect_error=_CONTEXT_NOT_FOUND)
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.ensure(WORKSPACE, 3))
    assert rejected.value.code == "network_inspect_failed"
    assert all(call[1:2] != ["run"] for call in fake.calls)


def test_final_instance_confirmation_rejects_context_error(tmp_path):
    """stop/rm 退出 0 但最终 inspect 不可用时不得报告已消失。"""
    root = tmp_path / "root"
    root.mkdir()
    existing = _valid_container(root)
    fake = FakeDocker(root, existing=existing,
                      instance_inspect_error=_CONTEXT_NOT_FOUND)
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.stop(WORKSPACE, existing["Id"]))
    assert rejected.value.code == "docker_inspect_failed"
    assert fake.removed == [existing["Id"]]


def test_stop_reports_docker_inspect_failure_instead_of_absent(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    fake = FakeDocker(
        root, inspect_error="Cannot connect to the Docker daemon at unix:///var/run/docker.sock")
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.stop(WORKSPACE, "f" * 64))
    assert rejected.value.code == "docker_inspect_failed"
    assert fake.stopped == [] and fake.removed == []


def test_stop_of_already_absent_instance_is_idempotent(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    fake = FakeDocker(root)
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    asyncio.run(service.stop(WORKSPACE, "f" * 64))
    assert fake.stopped == [] and fake.removed == []


def test_inspect_action_reports_docker_daemon_errors(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    fake = FakeDocker(
        root, inspect_error="permission denied while trying to connect to Docker")
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.inspect(WORKSPACE))
    assert rejected.value.code == "docker_inspect_failed"


def test_stop_request_requires_a_pinned_instance_identity():
    request = provisioner.parse_request({
        "version": 1, "action": "stop", "workspace_id": WORKSPACE,
        "instance_id": "f" * 64})
    assert request.instance_id == "f" * 64
    with pytest.raises(provisioner.ProvisionerError) as rejected:
        provisioner.parse_request({
            "version": 1, "action": "stop", "workspace_id": WORKSPACE})
    assert rejected.value.code == "invalid_instance"
    with pytest.raises(provisioner.ProvisionerError):
        provisioner.parse_request({
            "version": 1, "action": "stop", "workspace_id": WORKSPACE,
            "instance_id": "not-an-id"})


def test_inspect_rejects_tampered_actual_config(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    fake = FakeDocker(root, existing=_tampered("privileged", root),
                      network=_valid_network())
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.inspect(WORKSPACE))
    assert rejected.value.code == "container_conflict"


def _replace_state_with_outside_link(paths, outside: pathlib.Path) -> None:
    saved = paths.state.with_name("saved-state")
    paths.state.rename(saved)
    try:
        paths.state.symlink_to(outside, target_is_directory=True)
    except OSError:
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(paths.state), str(outside)],
            capture_output=True)
        if created.returncode != 0:
            pytest.skip("当前环境不允许创建目录链接/junction")


def test_ownership_rejects_state_replaced_after_prepare(tmp_path, monkeypatch):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    paths = provisioner.prepare_workspace(root, WORKSPACE)
    _replace_state_with_outside_link(paths, outside)

    monkeypatch.setattr(provisioner.os, "geteuid", lambda: 0, raising=False)
    recorded = []
    monkeypatch.setattr(
        provisioner.os, "fchown",
        lambda fd, uid, gid: recorded.append((fd, uid, gid)), raising=False)
    monkeypatch.setattr(
        provisioner.os, "chown",
        lambda path, uid, gid: recorded.append((path, uid, gid)),
        raising=False)

    with pytest.raises(provisioner.ProvisionerError):
        provisioner.ensure_worker_ownership(paths)
    assert recorded == [], "不得对已替换路径执行特权 chown"
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert outside.is_dir() and not (outside / "state").exists()
