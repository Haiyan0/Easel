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


def _secrets(tmp_path):
    root = tmp_path / "secrets"
    (root / "keys").mkdir(parents=True)
    (root / "database-query.env").write_text(
        "SALES_READONLY_DATABASE_URL="
        "mysql+asyncmy://readonly:secret@db-tunnel:13300/sales\n",
        encoding="utf-8")
    (root / "keys" / "id_ed25519").write_text("key", encoding="utf-8")
    (root / "keys" / "known_hosts").write_text("", encoding="utf-8")
    return root


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
                      workspace: str = WORKSPACE,
                      mounts: list[dict] | None = None) -> dict:
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
        ] if mounts is None else list(mounts),
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
                       workspace: str = WORKSPACE,
                       mounts: list[dict] | None = None,
                       aliases: list[str] | None = None) -> dict:
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
            "RW": False}] if mounts is None else list(mounts),
        "NetworkSettings": {
            "Ports": {},
            "Networks": {
                provisioner.network_name(workspace): {
                    "IPAddress": "172.30.0.3",
                    **({"Aliases": list(aliases)} if aliases else {})}},
        },
    }


def _argv_mounts(args: list[str]) -> list[dict]:
    """把 docker run argv 的 --mount 三元组还原成 inspect 形状。"""
    mounts: list[dict] = []
    for index, value in enumerate(args):
        if value != "--mount":
            continue
        fields: dict[str, str] = {}
        options: set[str] = set()
        for part in args[index + 1].split(","):
            key, separator, field_value = part.partition("=")
            if separator:
                fields[key] = field_value
            else:
                options.add(part)
        mounts.append({
            "Type": fields.get("type", ""),
            "Source": fields.get("src", ""),
            "Destination": fields.get("dst", ""),
            "RW": "readonly" not in options,
        })
    return mounts


class FakeDocker:
    def __init__(self, root: pathlib.Path, policy: pathlib.Path, *,
                 workspace: str = WORKSPACE,
                 sidecar_failure: bool = False,
                 sidecar_stop_failure: bool = False,
                 invalid_sidecar: bool = False,
                 sidecar_network_internal: bool = False,
                 probe_failure: str | None = None,
                 worker_run_timeout_after_create: bool = False,
                 worker_rm_failure: bool = False):
        self.root = root
        self.policy = policy
        self.workspace = workspace
        self.sidecar_network_internal = sidecar_network_internal
        self.sidecar_failure = sidecar_failure
        self.sidecar_stop_failure = sidecar_stop_failure
        self.invalid_sidecar = invalid_sidecar
        self.probe_failure = probe_failure
        self.worker_run_timeout_after_create = worker_run_timeout_after_create
        self.worker_rm_failure = worker_rm_failure
        self.last_cidfile: pathlib.Path | None = None
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
            networks = sidecar["NetworkSettings"]["Networks"]
            entry = networks.get(network)
            if isinstance(entry, dict):
                # 只补运行时地址，保留 run 时写入的网络别名。
                entry["IPAddress"] = "172.20.0.4"
            else:
                networks[network] = {"IPAddress": "172.20.0.4"}
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
                aliases = None
                for index, value in enumerate(args):
                    if value == "--network-alias":
                        aliases = [args[index + 1]]
                raw = _sidecar_container(
                    self.policy, workspace=self.workspace,
                    mounts=_argv_mounts(args), aliases=aliases)
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
                self.root, env=env, workspace=self.workspace,
                mounts=_argv_mounts(args))
            self.containers[name] = raw
            _write_cidfile(args, raw["Id"])
            if "--cidfile" in args:
                self.last_cidfile = pathlib.Path(
                    args[args.index("--cidfile") + 1])
            if self.worker_run_timeout_after_create:
                raise subprocess.TimeoutExpired(argv, 60.0)
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
            worker = self.containers.get(
                provisioner.container_name(self.workspace))
            if (self.worker_rm_failure and worker is not None
                    and target == worker["Id"]):
                return subprocess.CompletedProcess(
                    argv, 1, "", "rm failed\n")
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


def test_worker_timeout_cleanup_failure_preserves_sidecar_and_cidfile(
        tmp_path, monkeypatch):
    """worker 未确认删除时，不能先拆仍有使用者的 sidecar 或删追踪材料。"""
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = _settings(tmp_path)
    fake = FakeDocker(
        root, settings.sidecar_policy,
        worker_run_timeout_after_create=True, worker_rm_failure=True)
    service = provisioner.Provisioner(settings, run=fake)

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.ensure(WORKSPACE, 1))
    assert rejected.value.code == "worker_start_cleanup_incomplete"
    sidecar = provisioner.sidecar_container_name(WORKSPACE)
    assert sidecar in fake.containers
    assert provisioner.container_name(WORKSPACE) in fake.containers
    assert sidecar not in fake.stopped
    assert fake.last_cidfile is not None and fake.last_cidfile.exists()


def test_worker_timeout_cleanup_success_removes_only_this_round(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = _settings(tmp_path)
    fake = FakeDocker(
        root, settings.sidecar_policy, worker_run_timeout_after_create=True)
    service = provisioner.Provisioner(settings, run=fake)

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.ensure(WORKSPACE, 1))
    assert rejected.value.code == "worker_start_failed"
    assert provisioner.container_name(WORKSPACE) not in fake.containers
    assert provisioner.sidecar_container_name(WORKSPACE) not in fake.containers
    assert fake.last_cidfile is not None and not fake.last_cidfile.exists()


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


def test_database_query_secrets_require_sidecar(tmp_path):
    settings = _settings(tmp_path, sidecar=False)
    settings = dataclasses.replace(
        settings, database_query_secrets=_secrets(tmp_path))
    with pytest.raises(provisioner.ProvisionerError) as rejected:
        provisioner.Provisioner(settings)
    assert rejected.value.code == "database_query_requires_sidecar"


@pytest.mark.parametrize("damage", ["missing", "env_dir", "env_symlink",
                                    "keys_missing", "keys_file"])
def test_database_query_secrets_reject_invalid_layout(tmp_path, damage):
    root = _secrets(tmp_path)
    if damage == "missing":
        root = tmp_path / "absent"
    elif damage == "env_dir":
        (root / "database-query.env").unlink()
        (root / "database-query.env").mkdir()
    elif damage == "env_symlink":
        target = tmp_path / "real.env"
        target.write_text("X=Y\n", encoding="utf-8")
        (root / "database-query.env").unlink()
        try:
            (root / "database-query.env").symlink_to(target)
        except OSError:
            pytest.skip("当前环境不允许创建符号链接")
    elif damage == "keys_missing":
        (root / "keys" / "id_ed25519").unlink()
        (root / "keys" / "known_hosts").unlink()
        (root / "keys").rmdir()
    elif damage == "keys_file":
        (root / "keys" / "id_ed25519").unlink()
        (root / "keys" / "known_hosts").unlink()
        (root / "keys").rmdir()
        (root / "keys").write_text("not a dir", encoding="utf-8")
    settings = dataclasses.replace(
        _settings(tmp_path), database_query_secrets=root)
    with pytest.raises(provisioner.ProvisionerError) as rejected:
        provisioner.Provisioner(settings)
    assert rejected.value.code == "database_query_secrets_invalid"


def test_worker_argv_adds_readonly_env_mount_without_dsn(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    paths = provisioner.prepare_workspace(root, WORKSPACE)
    secrets = _secrets(tmp_path)
    env_file = (secrets / "database-query.env").resolve()
    argv = provisioner.build_run_argv(
        image=IMAGE, workspace_id=WORKSPACE, config_version=1, paths=paths,
        env_file=tmp_path / "worker.env",
        public_key_file=_write_public_key(tmp_path / "worker.pub"),
        database_query_env_file=env_file)
    mounts = [argv[index + 1] for index, value in enumerate(argv)
              if value == "--mount"]
    assert len(mounts) == 4
    assert (f"type=bind,src={env_file},dst={provisioner.DB_QUERY_ENV_PATH},"
            f"readonly") in mounts
    assert "readonly:secret@" not in " ".join(argv)
    assert "mysql+asyncmy://" not in " ".join(argv)


def test_worker_static_requires_exact_mount_count_when_enabled(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    paths = provisioner.prepare_workspace(root, WORKSPACE)
    secrets = _secrets(tmp_path)
    env_file = (secrets / "database-query.env").resolve()
    raw = _worker_container(root)
    public_key = pathlib.Path(raw["Mounts"][2]["Source"])
    kwargs = dict(workspace_id=WORKSPACE, config_version=1, image=IMAGE,
                  paths=paths, public_key_file=public_key)
    extra = {"Type": "bind", "Source": str(env_file),
             "Destination": provisioner.DB_QUERY_ENV_PATH, "RW": False}
    raw["Mounts"].append(extra)
    assert provisioner._verify_worker_static(
        raw, database_query_env_file=env_file, **kwargs)
    assert not provisioner._verify_worker_static(raw, **kwargs)
    raw["Mounts"][-1]["RW"] = True
    assert not provisioner._verify_worker_static(
        raw, database_query_env_file=env_file, **kwargs)


def test_sidecar_argv_adds_keys_mount_and_alias_only_when_enabled(tmp_path):
    keys = (_secrets(tmp_path) / "keys").resolve()
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    with_keys = provisioner.build_sidecar_run_argv(
        image=IMAGE, workspace_id=WORKSPACE, policy_file=policy,
        broker_upstream=UPSTREAM, database_query_keys=keys)
    mounts = [with_keys[index + 1] for index, value in enumerate(with_keys)
              if value == "--mount"]
    assert len(mounts) == 2
    assert f"type=bind,src={keys},dst={provisioner.DB_TUNNEL_KEYS_PATH},readonly" in mounts
    assert with_keys[with_keys.index("--network-alias") + 1] == "db-tunnel"

    without = provisioner.build_sidecar_run_argv(
        image=IMAGE, workspace_id=WORKSPACE, policy_file=policy,
        broker_upstream=UPSTREAM)
    assert len([value for value in without if value == "--mount"]) == 1
    assert "--network-alias" not in without


def test_sidecar_inspect_requires_keys_mount_and_alias(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    keys = (_secrets(tmp_path) / "keys").resolve()
    keys_mount = {"Type": "bind", "Source": str(keys),
                  "Destination": provisioner.DB_TUNNEL_KEYS_PATH,
                  "RW": False}
    raw = _sidecar_container(policy)
    raw["NetworkSettings"]["Networks"][SIDECAR_NETWORK] = {
        "IPAddress": "172.20.0.4"}
    kwargs = dict(workspace_id=WORKSPACE, image=IMAGE, policy_file=policy,
                  sidecar_network=SIDECAR_NETWORK, broker_upstream=UPSTREAM)
    assert provisioner.verify_sidecar_inspect(raw, **kwargs)
    assert not provisioner.verify_sidecar_inspect(
        raw, database_query_keys=keys, **kwargs)

    raw["Mounts"].append(keys_mount)
    assert not provisioner.verify_sidecar_inspect(
        raw, database_query_keys=keys, **kwargs)
    raw["NetworkSettings"]["Networks"][
        provisioner.network_name(WORKSPACE)]["Aliases"] = ["db-tunnel"]
    assert provisioner.verify_sidecar_inspect(
        raw, database_query_keys=keys, **kwargs)
    assert not provisioner.verify_sidecar_inspect(raw, **kwargs)


def test_old_sidecar_is_recreated_with_keys(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = dataclasses.replace(
        _settings(tmp_path), database_query_secrets=_secrets(tmp_path))
    fake = FakeDocker(root, settings.sidecar_policy)
    seeded = _sidecar_container(settings.sidecar_policy)
    fake.containers[provisioner.sidecar_container_name(WORKSPACE)] = seeded
    service = provisioner.Provisioner(settings, run=fake)

    asyncio.run(service.ensure(WORKSPACE, 1))
    sidecar = fake.containers[
        provisioner.sidecar_container_name(WORKSPACE)]
    assert len(sidecar["Mounts"]) == 2
    assert "db-tunnel" in sidecar["NetworkSettings"]["Networks"][
        provisioner.network_name(WORKSPACE)]["Aliases"]
    assert (seeded["Id"] in fake.removed
            or provisioner.sidecar_container_name(WORKSPACE) in fake.removed)


def test_sidecar_is_recreated_without_keys_when_disabled(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    policy = tmp_path / "policies" / "egress.json"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("{}", encoding="utf-8")
    keys = (_secrets(tmp_path) / "keys").resolve()
    seeded = _sidecar_container(
        policy, mounts=[
            {"Type": "bind", "Source": str(policy),
             "Destination": provisioner.SIDECAR_POLICY_CONTAINER_PATH,
             "RW": False},
            {"Type": "bind", "Source": str(keys),
             "Destination": provisioner.DB_TUNNEL_KEYS_PATH, "RW": False},
        ],
        aliases=["db-tunnel"])
    fake = FakeDocker(root, policy)
    fake.containers[provisioner.sidecar_container_name(WORKSPACE)] = seeded
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    asyncio.run(service.ensure(WORKSPACE, 1))
    sidecar = fake.containers[
        provisioner.sidecar_container_name(WORKSPACE)]
    assert len(sidecar["Mounts"]) == 1
    assert "db-tunnel" not in sidecar["NetworkSettings"]["Networks"][
        provisioner.network_name(WORKSPACE)].get("Aliases", [])


def test_ensure_with_secrets_creates_four_mount_worker(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = dataclasses.replace(
        _settings(tmp_path), database_query_secrets=_secrets(tmp_path))
    fake = FakeDocker(root, settings.sidecar_policy)
    service = provisioner.Provisioner(settings, run=fake)

    asyncio.run(service.ensure(WORKSPACE, 1))
    worker = fake.containers[provisioner.container_name(WORKSPACE)]
    sidecar = fake.containers[
        provisioner.sidecar_container_name(WORKSPACE)]
    assert len(worker["Mounts"]) == 4
    assert any(mount["Destination"] == provisioner.DB_QUERY_ENV_PATH
               for mount in worker["Mounts"])
    assert len(sidecar["Mounts"]) == 2

    asyncio.run(service.ensure(WORKSPACE, 1))
    assert len([c for c in fake.commands if _is_worker_run(c)]) == 1
    assert len([c for c in fake.commands if _is_sidecar_run(c)]) == 1


def test_old_three_mount_worker_is_conflict_when_secrets_enabled(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = dataclasses.replace(
        _settings(tmp_path), database_query_secrets=_secrets(tmp_path))
    fake = FakeDocker(root, settings.sidecar_policy)
    fake.workspace_network = True
    worker = _worker_container(root)
    worker["Mounts"][2]["Source"] = str(
        settings.worker_public_key.resolve())
    fake.containers[provisioner.container_name(WORKSPACE)] = worker
    service = provisioner.Provisioner(settings, run=fake)

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.ensure(WORKSPACE, 1))
    assert rejected.value.code == "container_conflict"
    assert fake.containers[provisioner.container_name(WORKSPACE)] is worker
    assert fake.stopped == [] and fake.removed == []


def test_exited_three_mount_worker_is_recreated_when_secrets_enabled(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = dataclasses.replace(
        _settings(tmp_path), database_query_secrets=_secrets(tmp_path))
    fake = FakeDocker(root, settings.sidecar_policy)
    fake.workspace_network = True
    worker = _worker_container(root)
    worker["State"] = {"Running": False, "Status": "exited",
                       "Health": {"Status": "unhealthy"}}
    worker["Mounts"][2]["Source"] = str(
        settings.worker_public_key.resolve())
    fake.containers[provisioner.container_name(WORKSPACE)] = worker
    service = provisioner.Provisioner(settings, run=fake)

    asyncio.run(service.ensure(WORKSPACE, 1))
    assert worker["Id"] in fake.removed
    rebuilt = fake.containers[provisioner.container_name(WORKSPACE)]
    assert len(rebuilt["Mounts"]) == 4


def test_exited_tampered_worker_is_conflict_when_secrets_enabled(
        tmp_path, monkeypatch):
    """旧形状保护不能放宽到篡改项：exited 也不是自动删除授权。"""
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = dataclasses.replace(
        _settings(tmp_path), database_query_secrets=_secrets(tmp_path))
    fake = FakeDocker(root, settings.sidecar_policy)
    fake.workspace_network = True
    worker = _worker_container(root)
    worker["Mounts"][2]["Source"] = str(
        settings.worker_public_key.resolve())
    worker["State"] = {"Running": False, "Status": "exited",
                       "Health": {"Status": "unhealthy"}}
    worker["HostConfig"]["Privileged"] = True
    fake.containers[provisioner.container_name(WORKSPACE)] = worker
    service = provisioner.Provisioner(settings, run=fake)

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.ensure(WORKSPACE, 1))
    assert rejected.value.code == "container_conflict"
    assert fake.containers[provisioner.container_name(WORKSPACE)] is worker
    assert fake.stopped == [] and fake.removed == []
    assert all(not _is_worker_run(c) for c in fake.commands)
