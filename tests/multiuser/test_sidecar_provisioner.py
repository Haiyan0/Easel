"""Task 8：provisioner 创建/复用/验收/清理每空间 sidecar。"""
from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.control.runtime import RuntimeManager, RuntimeUnavailable
from easel.control.store import Store

ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "deploy" / "multiuser" / "provisioner.py"


def _load():
    spec = importlib.util.spec_from_file_location(
        "easel_provisioner_sidecar", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provisioner = _load()
WORKSPACE = "a" * 32
IMAGE = "sha256:" + "b" * 64
SIDECAR_NETWORK = "easel-control"
UPSTREAM = "control:7861"


def _write_public_key(path):
    path.write_bytes(Ed25519PrivateKey.generate().public_key()
                     .public_bytes_raw())
    return path


def _settings(tmp_path, *, sidecar=True):
    policy = tmp_path / "policies" / "egress.json"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("{}", encoding="utf-8")
    return provisioner.ProvisionerSettings(
        image=IMAGE, workspace_root=tmp_path / "root",
        socket_path=tmp_path / "provisioner.sock",
        quota_mode="none", allow_unquotaed=True,
        sidecar_network=SIDECAR_NETWORK if sidecar else "",
        sidecar_policy=policy if sidecar else None,
        broker_upstream=UPSTREAM if sidecar else "",
        worker_public_key=_write_public_key(tmp_path / "worker.pub"))


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


def _not_found(target: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        [], 1, "", f"Error: No such object: {target}\n")


def _write_cidfile(args: list[str], instance_id: str) -> None:
    if "--cidfile" in args:
        pathlib.Path(args[args.index("--cidfile") + 1]).write_text(
            instance_id + "\n", encoding="ascii")


def _worker_container(root: pathlib.Path, env: list[str] | None = None,
                      workspace: str = WORKSPACE) -> dict:
    paths = provisioner.prepare_workspace(root, workspace)
    public_key = _write_public_key(root.parent / "worker.pub")
    env_list = list(env or ["EASEL_SERVICE_TOKEN=old-token"])
    if not any(line.startswith("EASEL_WORKER_PUBLIC_KEY_FILE=")
               for line in env_list):
        env_list.append("EASEL_WORKER_PUBLIC_KEY_FILE=/keys/worker-public.key")
    return {
        "Id": "1" * 64,
        "Name": "/" + provisioner.container_name(workspace),
        "Config": {
            "User": "10001:10001", "Image": IMAGE, "Entrypoint": None,
            "Cmd": list(provisioner.WORKER_COMMAND),
            "Labels": provisioner.workload_labels(workspace, 1),
            "Env": env_list,
        },
        "State": {"Running": True, "Status": "running",
                  "Health": {"Status": "healthy"}},
        "HostConfig": {
            "ReadonlyRootfs": True, "Privileged": False,
            "CapDrop": ["ALL"], "CapAdd": [],
            "SecurityOpt": ["no-new-privileges:true"],
            "PidsLimit": 256, "Memory": 4 * 1024 ** 3,
            "MemorySwap": 4 * 1024 ** 3, "NanoCpus": 2_000_000_000,
            "NetworkMode": provisioner.network_name(workspace),
            "PortBindings": {}, "PublishAllPorts": False, "Binds": [],
            "Devices": [], "PidMode": "", "IpcMode": "private",
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=1g"},
            "RestartPolicy": {"Name": "no"},
        },
        "Mounts": [
            {"Type": "bind", "Source": str(paths.workspace),
             "Destination": "/workspace", "RW": True},
            {"Type": "bind", "Source": str(paths.home),
             "Destination": "/home/easel", "RW": True},
            {"Type": "bind", "Source": str(public_key),
             "Destination": "/keys/worker-public.key", "RW": False},
        ],
        "NetworkSettings": {
            "Ports": {},
            "Networks": {
                provisioner.network_name(workspace): {
                    "IPAddress": "172.30.0.2"}},
        },
    }


def _with_public_key(env: list[str]) -> list[str]:
    return [*env, "EASEL_WORKER_PUBLIC_KEY_FILE=/keys/worker-public.key"]


def _sidecar_container(policy: pathlib.Path,
                       workspace: str = WORKSPACE) -> dict:
    return {
        "Id": "2" * 64,
        "Name": "/" + provisioner.sidecar_container_name(workspace),
        "Config": {
            "User": "10001:10001", "Image": IMAGE, "Entrypoint": None,
            "Cmd": list(provisioner.sidecar_command(UPSTREAM)),
            "Labels": provisioner.sidecar_labels(workspace),
            "Healthcheck": {"Test": [
                "CMD-SHELL",
                ("python -c \"import urllib.request;"
                 "urllib.request.urlopen('http://127.0.0.1:8899/healthz',"
                 "timeout=2)\"")]},
        },
        "State": {"Running": True, "Status": "running",
                  "Health": {"Status": "healthy"}},
        "HostConfig": {
            "ReadonlyRootfs": True, "Privileged": False,
            "CapDrop": ["ALL"], "CapAdd": [],
            "SecurityOpt": ["no-new-privileges:true"],
            "PidsLimit": provisioner.SIDECAR_PIDS_LIMIT,
            "Memory": provisioner.SIDECAR_MEMORY_BYTES,
            "MemorySwap": provisioner.SIDECAR_MEMORY_BYTES,
            "NanoCpus": provisioner.SIDECAR_NANO_CPUS,
            "NetworkMode": provisioner.network_name(workspace),
            "PortBindings": {}, "PublishAllPorts": False, "Binds": [],
            "Devices": [], "PidMode": "", "IpcMode": "private",
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=64m"},
            "RestartPolicy": {"Name": "no"},
        },
        "Mounts": [{
            "Type": "bind", "Source": str(policy),
            "Destination": provisioner.SIDECAR_POLICY_CONTAINER_PATH,
            "RW": False}],
        "NetworkSettings": {
            "Ports": {},
            "Networks": {
                provisioner.network_name(workspace): {
                    "IPAddress": "172.30.0.3"}},
        },
    }


class FakeDocker:
    def __init__(self, root: pathlib.Path, policy: pathlib.Path, *,
                 workspace: str = WORKSPACE,
                 sidecar_failure: bool = False,
                 sidecar_stop_failure: bool = False,
                 invalid_sidecar: bool = False,
                 sidecar_network_internal: bool = False,
                 probe_failure: str | None = None):
        self.root = root
        self.policy = policy
        self.workspace = workspace
        self.sidecar_network_internal = sidecar_network_internal
        self.sidecar_failure = sidecar_failure
        self.sidecar_stop_failure = sidecar_stop_failure
        self.invalid_sidecar = invalid_sidecar
        self.probe_failure = probe_failure
        self.sidecar_runs = 0
        self.containers: dict[str, dict] = {}
        self.workspace_network = False
        self.commands: list[list[str]] = []
        self.stopped: list[str] = []
        self.removed: list[str] = []

    def __call__(self, argv, *, timeout=120.0):
        self.commands.append(list(argv))
        args = argv[1:]
        if args[:2] == ["network", "inspect"]:
            name = args[2]
            if name == SIDECAR_NETWORK:
                return _ok(json.dumps([{
                    "Name": SIDECAR_NETWORK, "Driver": "bridge",
                    "Internal": self.sidecar_network_internal,
                    "EnableIPv6": False}]))
            if name == provisioner.network_name(self.workspace) and (
                    self.workspace_network):
                return _ok(json.dumps([{
                    "Name": name, "Driver": "bridge", "Internal": True,
                    "EnableIPv6": False,
                    "Labels": {"easel.managed": "true",
                               "easel.workspace-id": self.workspace}}]))
            return subprocess.CompletedProcess(
                [], 1, "", f"Error response from daemon: network {name} not found\n")
        if args[:2] == ["network", "create"]:
            self.workspace_network = True
            return _ok(args[-1] + "\n")
        if args[:2] == ["network", "connect"]:
            network, name = args[2], args[3]
            sidecar = self.containers.get(name)
            if sidecar is None:
                return _not_found(name)
            sidecar["NetworkSettings"]["Networks"][network] = {
                "IPAddress": "172.20.0.4"}
            return _ok()
        if args[:2] == ["container", "inspect"]:
            target = args[2]
            for name, raw in self.containers.items():
                if target in (name, raw["Id"]):
                    return _ok(json.dumps([raw]))
            return _not_found(target)
        if args[:1] == ["run"]:
            name = args[args.index("--name") + 1]
            if name.startswith("easel-sidecar-"):
                if self.sidecar_failure:
                    return subprocess.CompletedProcess(
                        argv, 1, "", "sidecar failed\n")
                raw = _sidecar_container(
                    self.policy, workspace=self.workspace)
                self.sidecar_runs += 1
                if self.invalid_sidecar and self.sidecar_runs == 1:
                    raw["HostConfig"]["Privileged"] = True
                self.containers[name] = raw
                _write_cidfile(args, raw["Id"])
                return _ok(raw["Id"] + "\n")
            # Docker 只在创建时读取 env-file；fake 也必须保存当时内容，
            # 否则“更新宿主文件”会被误当作容器实际配置已变化。
            env_path = pathlib.Path(
                args[args.index("--env-file") + 1])
            env = env_path.read_text(encoding="utf-8").splitlines()
            for index, value in enumerate(args):
                if value == "--env":
                    env.append(args[index + 1])
            raw = _worker_container(
                self.root, env=env, workspace=self.workspace)
            self.containers[name] = raw
            _write_cidfile(args, raw["Id"])
            return _ok(raw["Id"] + "\n")
        if args[:1] == ["exec"]:
            if self.probe_failure == "timeout":
                raise TimeoutError("probe timed out")
            if self.probe_failure == "network":
                raise OSError("probe network unreachable")
            raw = self.containers.get(
                provisioner.container_name(self.workspace))
            config_version = int(
                raw["Config"]["Labels"]["easel.config-version"])
            ready = self.probe_failure != "not_ready"
            return _ok(json.dumps({
                "workspace_id": self.workspace,
                "config_version": config_version,
                "container_id": provisioner.container_name(self.workspace),
                "ready": ready,
                "execution_capable": True,
            }) + "\n")
        if args[:1] == ["stop"]:
            target = args[-1]
            self.stopped.append(target)
            if (self.sidecar_stop_failure
                    and target.startswith("2" * 64)):
                return subprocess.CompletedProcess(
                    argv, 1, "", "stop failed\n")
            for raw in self.containers.values():
                if raw["Id"] == target:
                    raw["State"] = {"Running": False, "Status": "exited"}
                    return _ok()
            return _not_found(target)
        if args[:1] == ["rm"]:
            target = args[-1]
            self.removed.append(target)
            for name, raw in list(self.containers.items()):
                if target in (name, raw["Id"]):
                    del self.containers[name]
                    return _ok()
            return _not_found(target)
        raise AssertionError(f"unexpected docker 调用：{argv}")


def _is_sidecar_run(command: list[str]) -> bool:
    return (command[1:2] == ["run"]
            and any(str(part).startswith("easel-sidecar-")
                    for part in command))


def _is_worker_run(command: list[str]) -> bool:
    return command[1:2] == ["run"] and not _is_sidecar_run(command)


def _fake_health(monkeypatch, workspace: str = WORKSPACE):
    def fake_get(_host, _port, _path, _headers, _timeout):
        return {
            "workspace_id": workspace, "config_version": 1,
            "container_id": provisioner.container_name(workspace),
            "ready": True,
        }
    monkeypatch.setattr(provisioner, "_http_get_json", fake_get)


def test_sidecar_is_created_hardened_and_worker_gets_broker_proxy_env(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = _settings(tmp_path)
    fake = FakeDocker(root, settings.sidecar_policy)
    service = provisioner.Provisioner(settings, run=fake)

    handle = asyncio.run(service.ensure(WORKSPACE, 1))
    assert handle.container_id == "1" * 64
    sidecar = fake.containers[
        provisioner.sidecar_container_name(WORKSPACE)]
    assert provisioner.verify_sidecar_inspect(
        sidecar, workspace_id=WORKSPACE, image=IMAGE,
        policy_file=settings.sidecar_policy,
        sidecar_network=SIDECAR_NETWORK,
        broker_upstream=UPSTREAM)
    assert set(sidecar["NetworkSettings"]["Networks"]) == {
        provisioner.network_name(WORKSPACE), SIDECAR_NETWORK}
    run = next(call for call in fake.commands if _is_sidecar_run(call))
    assert run[:3] == ["docker", "run", "-d"]
    assert "--read-only" in run and "--cap-drop=ALL" in run
    assert "--privileged" not in run and "--publish" not in run
    assert provisioner.SIDECAR_POLICY_CONTAINER_PATH in "\n".join(run)

    env_text = (root / ".credentials" / f"{WORKSPACE}.env").read_text(
        encoding="utf-8")
    assert ("EASEL_BROKER_URL=http://"
            f"{provisioner.sidecar_container_name(WORKSPACE)}:8899") in env_text
    assert ("HTTP_PROXY=http://"
            f"{provisioner.sidecar_container_name(WORKSPACE)}:8898") in env_text
    assert f"{provisioner.sidecar_container_name(WORKSPACE)}" in env_text


def test_probe_failure_does_not_create_or_touch_sidecar(tmp_path,
                                                        monkeypatch):
    """既有 worker 健康失败时，不得先补 sidecar 再强删 worker。"""
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = _settings(tmp_path)
    fake = FakeDocker(root, settings.sidecar_policy)
    fake.workspace_network = True
    wiring = provisioner.worker_wiring_env(
        WORKSPACE, provisioner.sidecar_container_name(WORKSPACE))
    env = ["EASEL_SERVICE_TOKEN=old-token",
           "EASEL_WORKER_PUBLIC_KEY_FILE=/keys/worker-public.key"]
    env.extend(f"{key}={value}" for key, value in wiring.items())
    worker = _worker_container(root, env=env)
    fake.containers[provisioner.container_name(WORKSPACE)] = worker
    token_path = root / ".credentials" / f"{WORKSPACE}.token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("old-token", encoding="ascii")
    fake.probe_failure = "not_ready"
    service = provisioner.Provisioner(settings, run=fake)

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.ensure(WORKSPACE, 1))
    assert rejected.value.code == "worker_not_ready"
    assert fake.sidecar_runs == 0
    assert fake.containers[provisioner.container_name(WORKSPACE)] is worker
    assert provisioner.sidecar_container_name(WORKSPACE) not in fake.containers
    assert fake.stopped == [] and fake.removed == []


def test_valid_sidecar_is_reused(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = _settings(tmp_path)
    fake = FakeDocker(root, settings.sidecar_policy)
    service = provisioner.Provisioner(settings, run=fake)

    asyncio.run(service.ensure(WORKSPACE, 1))
    sidecar_runs = [c for c in fake.commands if _is_sidecar_run(c)]
    worker_runs = [c for c in fake.commands if _is_worker_run(c)]
    assert len(sidecar_runs) == 1 and len(worker_runs) == 1
    asyncio.run(service.ensure(WORKSPACE, 1))
    assert len([c for c in fake.commands if _is_sidecar_run(c)]) == 1
    assert len([c for c in fake.commands if _is_worker_run(c)]) == 1


def test_invalid_sidecar_is_recreated(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = _settings(tmp_path)
    fake = FakeDocker(root, settings.sidecar_policy)
    seeded = _sidecar_container(settings.sidecar_policy)
    seeded["HostConfig"]["Privileged"] = True
    fake.containers[provisioner.sidecar_container_name(WORKSPACE)] = seeded
    service = provisioner.Provisioner(settings, run=fake)

    asyncio.run(service.ensure(WORKSPACE, 1))
    sidecar_runs = [c for c in fake.commands if _is_sidecar_run(c)]
    assert len(sidecar_runs) == 1, "不安全的已有 sidecar 必须删除后重建"
    current = fake.containers[
        provisioner.sidecar_container_name(WORKSPACE)]
    assert current["HostConfig"]["Privileged"] is False


def test_sidecar_start_failure_fails_closed(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = _settings(tmp_path)
    fake = FakeDocker(root, settings.sidecar_policy, sidecar_failure=True)
    service = provisioner.Provisioner(settings, run=fake)

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.ensure(WORKSPACE, 1))
    assert rejected.value.code == "sidecar_start_failed"
    assert not [c for c in fake.commands if _is_worker_run(c)]


def test_stop_removes_worker_and_sidecar(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = _settings(tmp_path)
    fake = FakeDocker(root, settings.sidecar_policy)
    service = provisioner.Provisioner(settings, run=fake)

    handle = asyncio.run(service.ensure(WORKSPACE, 1))
    asyncio.run(service.stop(WORKSPACE, handle.container_id))
    assert fake.containers == {}


def test_sidecar_stop_failure_is_reported(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = _settings(tmp_path)
    fake = FakeDocker(root, settings.sidecar_policy,
                      sidecar_stop_failure=True)
    service = provisioner.Provisioner(settings, run=fake)

    handle = asyncio.run(service.ensure(WORKSPACE, 1))
    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.stop(WORKSPACE, handle.container_id))
    assert rejected.value.code == "sidecar_stop_failed"
    assert provisioner.sidecar_container_name(WORKSPACE) in fake.containers


def _setup_started(tmp_path, monkeypatch, *, sidecar=True):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = _settings(tmp_path, sidecar=sidecar)
    fake = FakeDocker(root, settings.sidecar_policy)
    service = provisioner.Provisioner(settings, run=fake)
    handle = asyncio.run(service.ensure(WORKSPACE, 1))
    return root, settings, fake, service, handle


def test_live_zero_egress_worker_is_not_reused_after_enabling_sidecar(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = _settings(tmp_path)
    fake = FakeDocker(root, settings.sidecar_policy)
    seeded = _worker_container(
        root, ["EASEL_SERVICE_TOKEN=old-token"])
    fake.containers[provisioner.container_name(WORKSPACE)] = seeded
    service = provisioner.Provisioner(settings, run=fake)

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.ensure(WORKSPACE, 1))
    assert rejected.value.code == "worker_wiring_mismatch"
    assert fake.containers[provisioner.container_name(WORKSPACE)] is seeded
    assert not [c for c in fake.commands if _is_worker_run(c)]
    assert not [c for c in fake.commands if _is_sidecar_run(c)], (
        "不能为无法安全迁移的活跃旧实例先创建 sidecar")


@pytest.mark.parametrize("probe_failure", ["timeout", "network", "not_ready"])
def test_live_wiring_mismatch_preserves_running_worker_on_probe_failure(
        tmp_path, monkeypatch, probe_failure):
    root = tmp_path / "root"
    root.mkdir()
    # 缩短健康等待：若误走删除重建，测试不应为空等 60 秒。
    settings = dataclasses.replace(_settings(tmp_path), health_timeout=0.2)
    fake = FakeDocker(root, settings.sidecar_policy)
    seeded = _worker_container(
        root, ["EASEL_SERVICE_TOKEN=old-token"])
    fake.containers[provisioner.container_name(WORKSPACE)] = seeded
    paths = provisioner.prepare_workspace(root, WORKSPACE)
    (paths.workspace / "keep.txt").write_text("persisted", encoding="utf-8")

    def failing_probe(_host, _port, _path, _headers, _timeout):
        if probe_failure == "timeout":
            raise TimeoutError("probe timed out")
        if probe_failure == "network":
            raise OSError("probe network unreachable")
        return {
            "workspace_id": WORKSPACE, "config_version": 1,
            "container_id": provisioner.container_name(WORKSPACE),
            "ready": False,
        }

    monkeypatch.setattr(provisioner, "_http_get_json", failing_probe)
    service = provisioner.Provisioner(settings, run=fake)

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.ensure(WORKSPACE, 1))
    assert rejected.value.code == "worker_wiring_mismatch"
    assert fake.containers[provisioner.container_name(WORKSPACE)] is seeded
    assert seeded["State"]["Running"] is True
    assert seeded["Config"]["Env"] == _with_public_key([
        "EASEL_SERVICE_TOKEN=old-token"])
    assert (paths.workspace / "keep.txt").read_text(
        encoding="utf-8") == "persisted"
    assert fake.removed == []
    assert fake.stopped == []
    assert not [c for c in fake.commands if _is_worker_run(c)]
    assert not [c for c in fake.commands if _is_sidecar_run(c)], (
        "运行中旧实例迁移前不得先变更 sidecar")


def test_stopped_zero_egress_worker_is_rebuilt_with_actual_new_env(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = _settings(tmp_path)
    fake = FakeDocker(root, settings.sidecar_policy)
    seeded = _worker_container(
        root, ["EASEL_SERVICE_TOKEN=old-token"])
    seeded["State"] = {"Running": False, "Status": "exited"}
    fake.containers[provisioner.container_name(WORKSPACE)] = seeded
    paths = provisioner.prepare_workspace(root, WORKSPACE)
    (paths.workspace / "keep.txt").write_text("persisted", encoding="utf-8")
    service = provisioner.Provisioner(settings, run=fake)

    handle = asyncio.run(service.ensure(WORKSPACE, 1))
    assert (paths.workspace / "keep.txt").read_text(
        encoding="utf-8") == "persisted"
    assert handle.container_id == "1" * 64
    worker = fake.containers[provisioner.container_name(WORKSPACE)]
    env = worker["Config"]["Env"]
    assert ("EASEL_BROKER_URL=http://"
            f"{provisioner.sidecar_container_name(WORKSPACE)}:8899") in env
    assert ("HTTP_PROXY=http://"
            f"{provisioner.sidecar_container_name(WORKSPACE)}:8898") in env
    assert len([c for c in fake.commands if _is_worker_run(c)]) == 1
    assert len([c for c in fake.commands if _is_sidecar_run(c)]) == 1


def test_running_worker_with_stale_proxy_env_is_not_reused(
        tmp_path, monkeypatch):
    _root, _settings_value, fake, service, _handle = _setup_started(
        tmp_path, monkeypatch)
    worker = fake.containers[provisioner.container_name(WORKSPACE)]
    worker["Config"]["Env"] = _with_public_key([
        "EASEL_SERVICE_TOKEN=old-token",
        "EASEL_BROKER_URL=http://127.0.0.1:8899",
        "HTTP_PROXY=http://127.0.0.1:8898",
        "HTTPS_PROXY=http://127.0.0.1:8898",
        "NO_PROXY=127.0.0.1,localhost",
    ])

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.ensure(WORKSPACE, 1))
    assert rejected.value.code == "worker_wiring_mismatch"
    assert fake.containers[provisioner.container_name(WORKSPACE)] is worker
    assert len([c for c in fake.commands if _is_worker_run(c)]) == 1
    assert len([c for c in fake.commands if _is_sidecar_run(c)]) == 1


def test_zero_egress_worker_with_matching_env_is_still_reused(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = _settings(tmp_path, sidecar=False)
    fake = FakeDocker(root, settings.sidecar_policy)
    seeded = _worker_container(
        root, ["EASEL_SERVICE_TOKEN=old-token"])
    fake.containers[provisioner.container_name(WORKSPACE)] = seeded
    token_path = root / ".credentials" / f"{WORKSPACE}.token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("old-token", encoding="ascii")
    service = provisioner.Provisioner(settings, run=fake)

    handle = asyncio.run(service.ensure(WORKSPACE, 1))
    assert handle.container_id == seeded["Id"]
    assert not [c for c in fake.commands if _is_worker_run(c)]
    assert not [c for c in fake.commands if _is_sidecar_run(c)]


def test_inspect_does_not_report_ready_when_sidecar_is_missing(
        tmp_path, monkeypatch):
    _root, _settings_value, fake, service, _handle = _setup_started(
        tmp_path, monkeypatch)
    del fake.containers[provisioner.sidecar_container_name(WORKSPACE)]

    report = asyncio.run(service.inspect(WORKSPACE))
    assert report["managed"] is True
    assert report["ready"] is False
    assert len([c for c in fake.commands if _is_sidecar_run(c)]) == 1


@pytest.mark.parametrize("tamper", ["unhealthy", "privileged", "network",
                                    "outbound"])
def test_inspect_does_not_report_ready_when_sidecar_is_tampered(
        tmp_path, monkeypatch, tamper):
    _root, _settings_value, fake, service, _handle = _setup_started(
        tmp_path, monkeypatch)
    sidecar = fake.containers[provisioner.sidecar_container_name(WORKSPACE)]
    if tamper == "unhealthy":
        sidecar["State"] = {
            "Running": True, "Health": {"Status": "unhealthy"}}
    elif tamper == "privileged":
        sidecar["HostConfig"]["Privileged"] = True
    elif tamper == "network":
        sidecar["NetworkSettings"]["Networks"].pop(SIDECAR_NETWORK)
    else:
        fake.sidecar_network_internal = True

    report = asyncio.run(service.inspect(WORKSPACE))
    assert report["ready"] is False


def test_inspect_does_not_report_ready_when_worker_wiring_is_stale(
        tmp_path, monkeypatch):
    _root, _settings_value, fake, service, _handle = _setup_started(
        tmp_path, monkeypatch)
    worker = fake.containers[provisioner.container_name(WORKSPACE)]
    worker["Config"]["Env"] = _with_public_key([
        "EASEL_SERVICE_TOKEN=old-token"])

    report = asyncio.run(service.inspect(WORKSPACE))
    assert report["ready"] is False


class _Clock:
    def __init__(self, value: float = 1000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


@pytest.mark.parametrize("tamper", ["missing", "unhealthy", "privileged",
                                    "network", "outbound"])
def test_runtime_restart_does_not_adopt_worker_with_broken_sidecar(
        tmp_path, monkeypatch, tamper):
    root = tmp_path / "root"
    root.mkdir()
    settings = _settings(tmp_path)
    database = tmp_path / "control.sqlite3"
    with Store.open(database) as bootstrap:
        workspace_id = bootstrap.create_user("A").workspace_id
    _fake_health(monkeypatch, workspace_id)
    fake = FakeDocker(root, settings.sidecar_policy, workspace=workspace_id)
    service = provisioner.Provisioner(settings, run=fake)
    clock = _Clock()

    async def scenario():
        with Store.open(database) as first_store:
            first = RuntimeManager(
                first_store, service, clock=clock, lease_seconds=60,
                poll_interval=0.01)
            handle = await first.ensure_ready(workspace_id, 1)
            assert handle.container_id == "1" * 64
            sidecar = fake.containers[
                provisioner.sidecar_container_name(workspace_id)]
            if tamper == "missing":
                del fake.containers[
                    provisioner.sidecar_container_name(workspace_id)]
            elif tamper == "unhealthy":
                sidecar["State"] = {
                    "Running": True, "Health": {"Status": "unhealthy"}}
            elif tamper == "privileged":
                sidecar["HostConfig"]["Privileged"] = True
            elif tamper == "network":
                sidecar["NetworkSettings"]["Networks"].pop(SIDECAR_NETWORK)
            else:
                fake.sidecar_network_internal = True

            clock.value += 100  # 租约过期，等价管理进程重启后的对账
            with Store.open(database) as second_store:
                second = RuntimeManager(
                    second_store, service, clock=clock, lease_seconds=60,
                    poll_interval=0.01)
                with pytest.raises(RuntimeUnavailable) as rejected:
                    await second.ensure_ready(workspace_id, 1)
                assert rejected.value.code == "worker_identity_mismatch"
                lease = second_store.get_worker_lease(workspace_id)
                assert lease.state == "failed"
                assert len([
                    c for c in fake.commands if _is_worker_run(c)]) == 1
                assert len([
                    c for c in fake.commands if _is_sidecar_run(c)]) == 1

    asyncio.run(scenario())


def test_runtime_restart_adopts_worker_with_complete_sidecar_dependency(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    settings = _settings(tmp_path)
    database = tmp_path / "control.sqlite3"
    with Store.open(database) as bootstrap:
        workspace_id = bootstrap.create_user("A").workspace_id
    _fake_health(monkeypatch, workspace_id)
    fake = FakeDocker(root, settings.sidecar_policy, workspace=workspace_id)
    service = provisioner.Provisioner(settings, run=fake)
    clock = _Clock()

    async def scenario():
        with Store.open(database) as first_store:
            first = RuntimeManager(
                first_store, service, clock=clock, lease_seconds=60,
                poll_interval=0.01)
            handle = await first.ensure_ready(workspace_id, 1)

            clock.value += 100
            with Store.open(database) as second_store:
                second = RuntimeManager(
                    second_store, service, clock=clock, lease_seconds=60,
                    poll_interval=0.01)
                adopted = await second.ensure_ready(workspace_id, 1)
                assert adopted.container_id == handle.container_id
                assert len([
                    c for c in fake.commands if _is_worker_run(c)]) == 1
                assert len([
                    c for c in fake.commands if _is_sidecar_run(c)]) == 1

    asyncio.run(scenario())


def test_incomplete_or_insecure_sidecar_config_is_rejected(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    base = dict(image=IMAGE, workspace_root=tmp_path / "root",
                socket_path=tmp_path / "sock", quota_mode="none",
                allow_unquotaed=True)
    with pytest.raises(provisioner.ProvisionerError) as rejected:
        provisioner.Provisioner(provisioner.ProvisionerSettings(
            **base, sidecar_network=SIDECAR_NETWORK))
    assert rejected.value.code == "sidecar_config_incomplete"
    with pytest.raises(provisioner.ProvisionerError):
        provisioner.Provisioner(provisioner.ProvisionerSettings(
            **base, sidecar_network=SIDECAR_NETWORK,
            sidecar_policy=policy, broker_upstream="http://evil/path"))
