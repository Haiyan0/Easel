"""Task 8：provisioner 创建/复用/验收/清理每空间 sidecar。"""
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
        broker_upstream=UPSTREAM if sidecar else "")


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


def _not_found(target: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        [], 1, "", f"Error: No such object: {target}\n")


def _worker_container(root: pathlib.Path) -> dict:
    paths = provisioner.prepare_workspace(root, WORKSPACE)
    return {
        "Id": "1" * 64,
        "Name": "/" + provisioner.container_name(WORKSPACE),
        "Config": {
            "User": "10001:10001", "Image": IMAGE, "Entrypoint": None,
            "Cmd": ["python", "-m", "easel.worker.lifecycle", "serve",
                    "--host", "0.0.0.0", "--port", "7860"],
            "Labels": provisioner.workload_labels(WORKSPACE, 1),
        },
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "HostConfig": {
            "ReadonlyRootfs": True, "Privileged": False,
            "CapDrop": ["ALL"], "CapAdd": [],
            "SecurityOpt": ["no-new-privileges:true"],
            "PidsLimit": 256, "Memory": 4 * 1024 ** 3,
            "MemorySwap": 4 * 1024 ** 3, "NanoCpus": 2_000_000_000,
            "NetworkMode": provisioner.network_name(WORKSPACE),
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
        ],
        "NetworkSettings": {
            "Ports": {},
            "Networks": {
                provisioner.network_name(WORKSPACE): {
                    "IPAddress": "172.30.0.2"}},
        },
    }


def _sidecar_container(policy: pathlib.Path) -> dict:
    return {
        "Id": "2" * 64,
        "Name": "/" + provisioner.sidecar_container_name(WORKSPACE),
        "Config": {
            "User": "10001:10001", "Image": IMAGE, "Entrypoint": None,
            "Cmd": list(provisioner.sidecar_command(UPSTREAM)),
            "Labels": provisioner.sidecar_labels(WORKSPACE),
            "Healthcheck": {"Test": [
                "CMD-SHELL",
                ("python -c \"import urllib.request;"
                 "urllib.request.urlopen('http://127.0.0.1:8899/healthz',"
                 "timeout=2)\"")]},
        },
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "HostConfig": {
            "ReadonlyRootfs": True, "Privileged": False,
            "CapDrop": ["ALL"], "CapAdd": [],
            "SecurityOpt": ["no-new-privileges:true"],
            "PidsLimit": provisioner.SIDECAR_PIDS_LIMIT,
            "Memory": provisioner.SIDECAR_MEMORY_BYTES,
            "MemorySwap": provisioner.SIDECAR_MEMORY_BYTES,
            "NanoCpus": provisioner.SIDECAR_NANO_CPUS,
            "NetworkMode": provisioner.network_name(WORKSPACE),
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
                provisioner.network_name(WORKSPACE): {
                    "IPAddress": "172.30.0.3"}},
        },
    }


class FakeDocker:
    def __init__(self, root: pathlib.Path, policy: pathlib.Path, *,
                 sidecar_failure: bool = False,
                 sidecar_stop_failure: bool = False,
                 invalid_sidecar: bool = False):
        self.root = root
        self.policy = policy
        self.sidecar_failure = sidecar_failure
        self.sidecar_stop_failure = sidecar_stop_failure
        self.invalid_sidecar = invalid_sidecar
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
                    "Internal": False, "EnableIPv6": False}]))
            if name == provisioner.network_name(WORKSPACE) and (
                    self.workspace_network):
                return _ok(json.dumps([{
                    "Name": name, "Driver": "bridge", "Internal": True,
                    "EnableIPv6": False,
                    "Labels": {"easel.managed": "true",
                               "easel.workspace-id": WORKSPACE}}]))
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
                raw = _sidecar_container(self.policy)
                self.sidecar_runs += 1
                if self.invalid_sidecar and self.sidecar_runs == 1:
                    raw["HostConfig"]["Privileged"] = True
                self.containers[name] = raw
                return _ok(raw["Id"] + "\n")
            raw = _worker_container(self.root)
            self.containers[name] = raw
            return _ok(raw["Id"] + "\n")
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


def _fake_health(monkeypatch):
    def fake_get(_host, _port, _path, _headers, _timeout):
        return {
            "workspace_id": WORKSPACE, "config_version": 1,
            "container_id": provisioner.container_name(WORKSPACE),
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
