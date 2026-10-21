"""Task 8 R3：真实 provisioner 停止承诺与代次接管的一致性。"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

from easel.control.runtime import RuntimeManager
from easel.control.store import Store

ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "deploy" / "multiuser" / "provisioner.py"


def _load_provisioner():
    spec = importlib.util.spec_from_file_location(
        "easel_provisioner_r3", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provisioner = _load_provisioner()
IMAGE = "sha256:" + "b" * 64


class Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class GatedBeginStop:
    """在 begin_worker_stop 已提交但尚未返回旧管理者时暂停。"""

    def __init__(self, entered: asyncio.Event, gate: asyncio.Event):
        self.entered = entered
        self.gate = gate

    async def __call__(self, function, *args, **kwargs):
        result = function(*args, **kwargs)
        if (getattr(function, "__name__", "") == "begin_worker_stop"
                and result):
            self.entered.set()
            await self.gate.wait()
        return result


class FakeDocker:
    """真实 Provisioner 的 Docker 外部边界；按名称/IP 均可 inspect。"""

    def __init__(self, root: pathlib.Path, workspace_id: str):
        self.root = root
        self.workspace_id = workspace_id
        self.container = None
        self.network = None
        self.serial = 0
        self.commands: list[list[str]] = []
        self.stop_ids: list[str] = []
        self.rm_ids: list[str] = []

    @staticmethod
    def _ok(stdout: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], 0, stdout, "")

    @staticmethod
    def _not_found(target: str) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            [], 1, "", f"Error: No such object: {target}\n")

    def _container(self, workspace_id: str) -> dict:
        paths = provisioner.prepare_workspace(self.root, workspace_id)
        return {
            "Id": f"{self.serial:064x}",
            "Name": "/" + provisioner.container_name(workspace_id),
            "Config": {
                "User": "10001:10001",
                "Image": IMAGE,
                "Entrypoint": None,
                "Cmd": ["python", "-m", "easel.worker.lifecycle",
                        "serve", "--host", "0.0.0.0", "--port", "7860"],
                "Labels": provisioner.workload_labels(workspace_id, 1),
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
                "NetworkMode": provisioner.network_name(workspace_id),
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
                    provisioner.network_name(workspace_id): {
                        "IPAddress": "172.30.0.2"},
                },
            },
        }

    def __call__(self, argv, *, timeout=120.0):
        self.commands.append(list(argv))
        args = argv[1:]
        if args[:2] == ["network", "inspect"]:
            if self.network is None:
                return self._not_found("network")
            return self._ok(json.dumps([self.network]))
        if args[:2] == ["network", "create"]:
            name = provisioner.network_name(self.workspace_id)
            self.network = {
                "Name": name, "Driver": "bridge", "Internal": True,
                "EnableIPv6": False,
                "Labels": {"easel.managed": "true",
                           "easel.workspace-id": self.workspace_id},
            }
            return self._ok(name + "\n")
        if args[:2] == ["container", "inspect"]:
            target = args[2]
            if self.container is None:
                return self._not_found(target)
            if target not in (
                    self.container["Id"],
                    provisioner.container_name(self.workspace_id)):
                return self._not_found(target)
            return self._ok(json.dumps([self.container]))
        if args[:1] == ["run"]:
            self.serial += 1
            self.container = self._container(self.workspace_id)
            return self._ok(self.container["Id"] + "\n")
        if args[:1] == ["stop"]:
            target = args[-1]
            self.stop_ids.append(target)
            if self.container is None or self.container["Id"] != target:
                return self._not_found(target)
            self.container["State"] = {"Running": False,
                                       "Status": "exited"}
            return self._ok()
        if args[:1] == ["rm"]:
            target = args[-1]
            self.rm_ids.append(target)
            if self.container is None or self.container["Id"] != target:
                return self._not_found(target)
            self.container = None
            return self._ok()
        raise AssertionError(f"unexpected docker 调用：{argv}")


def _fake_health(monkeypatch, workspace_id: str):
    def fake_get(_host, _port, _path, _headers, _timeout):
        return {
            "workspace_id": workspace_id,
            "config_version": 1,
            "container_id": provisioner.container_name(workspace_id),
            "ready": True,
        }
    monkeypatch.setattr(provisioner, "_http_get_json", fake_get)


def _service(root: pathlib.Path, fake: FakeDocker) -> object:
    settings = provisioner.ProvisionerSettings(
        image=IMAGE, workspace_root=root,
        socket_path=root.parent / "provisioner.sock",
        quota_mode="none", allow_unquotaed=True)
    return provisioner.Provisioner(settings, run=fake)


def test_real_provisioner_reuses_healthy_instance_on_reconcile(
        tmp_path, monkeypatch):
    """对照路径：没有待停止承诺时，新代次确实会复用同一物理实例。"""
    async def scenario():
        root = tmp_path / "root"
        root.mkdir()
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            _fake_health(monkeypatch, user.workspace_id)
            fake = FakeDocker(root, user.workspace_id)
            service = _service(root, fake)
            clock = Clock()
            first = RuntimeManager(store, service, clock=clock,
                                   lease_seconds=1, poll_interval=0.01)
            second = RuntimeManager(store, service, clock=clock,
                                    lease_seconds=1, poll_interval=0.01)
            old = await first.ensure_ready(user.workspace_id, 1)
            clock.value = 1002
            reused = await second.ensure_ready(user.workspace_id, 1)
            assert reused.container_id == old.container_id
            assert fake.stop_ids == [] and fake.rm_ids == []
    asyncio.run(scenario())


def test_old_stop_cannot_delete_instance_after_stop_recovery(
        tmp_path, monkeypatch):
    """停止认领后暂停；新管理者必须先消解停止，旧 stop 不得删除新实例。"""
    async def scenario():
        root = tmp_path / "root"
        root.mkdir()
        with Store.open(tmp_path / "db") as first_store, \
                Store.open(tmp_path / "db") as second_store:
            user = first_store.create_user("A")
            _fake_health(monkeypatch, user.workspace_id)
            fake = FakeDocker(root, user.workspace_id)
            service = _service(root, fake)
            clock = Clock()
            gate = asyncio.Event()
            entered = asyncio.Event()
            first = RuntimeManager(
                first_store, service, clock=clock, lease_seconds=1,
                poll_interval=0.01, drain_timeout=5,
                db_runner=GatedBeginStop(entered, gate))
            second = RuntimeManager(second_store, service, clock=clock,
                                    lease_seconds=1, poll_interval=0.01)

            old = await first.ensure_ready(user.workspace_id, 1)
            assert fake.container["Id"] == old.container_id
            drain = asyncio.create_task(first.drain(user.workspace_id))
            await asyncio.wait_for(entered.wait(), 1)
            assert fake.stop_ids == [], "旧 stop 尚未送达"
            assert fake.container["Id"] == old.container_id

            clock.value = 1002
            new = await second.ensure_ready(user.workspace_id, 1)
            assert new.container_id != old.container_id
            # 新代次创建前，旧实例必须已被恢复停止流程删除。
            assert fake.container["Id"] == new.container_id
            assert fake.stop_ids == [old.container_id]
            assert fake.rm_ids == [old.container_id]

            gate.set()
            with pytest.raises(Exception):
                await asyncio.wait_for(drain, 2)
            assert fake.container["Id"] == new.container_id
            assert new.container_id not in fake.stop_ids
            assert new.container_id not in fake.rm_ids

            lease = second_store.get_worker_lease(user.workspace_id)
            assert lease.state == "ready"
            assert lease.container_id == new.container_id
    asyncio.run(scenario())
