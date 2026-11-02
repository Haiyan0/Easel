"""5C3/503：attempt 归属、pending 边界与旧坏记录受限恢复。

本文件不连接 Docker；只使用临时目录、假 ID 与内存 gateway fake。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.control.runtime import RuntimeManager
from easel.control.store import Store
from easel.control.types import WorkerHandle

ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "deploy" / "multiuser" / "provisioner.py"


def _load():
    spec = importlib.util.spec_from_file_location(
        "easel_provisioner_attempt_503", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provisioner = _load()
WORKSPACE = "a" * 32
WORKER = "c" * 64
GATEWAY = "b" * 64
CONFIG_VERSION = 3
IMAGE = "sha256:" + "d" * 64


def _write_key(path: pathlib.Path) -> pathlib.Path:
    path.write_bytes(Ed25519PrivateKey.generate().public_key()
                     .public_bytes_raw())
    return path


def _settings(tmp_path: pathlib.Path):
    return provisioner.ProvisionerSettings(
        image=IMAGE, workspace_root=tmp_path / "root",
        socket_path=tmp_path / "provisioner.sock",
        quota_mode="none", allow_unquotaed=True,
        worker_public_key=_write_key(tmp_path / "worker.pub"),
        gateway_image=IMAGE,
        gateway_control_network="easel-control",
        gateway_public_key=_write_key(tmp_path / "gateway.pub"))


class FakeGateway:
    def __init__(self, *, worker: str = WORKER, gateway: str = GATEWAY,
                 config_version: int = CONFIG_VERSION) -> None:
        self.worker = worker
        self.gateway = gateway
        self.config_version = config_version
        self.stops: list[tuple[str, str, str]] = []
        self.inspect_calls: list[tuple[str, str]] = []
        self.ensure_calls = 0
        self.current_gateway = ""

    async def current_instance_id(self, workspace_id: str) -> str:
        return self.current_gateway

    async def stop(self, workspace_id: str, worker_id: str,
                   gateway_id: str) -> None:
        self.stops.append((workspace_id, worker_id, gateway_id))

    async def inspect(self, *, workspace_id: str,
                      gateway_instance_id: str = "") -> dict:
        self.inspect_calls.append((workspace_id, gateway_instance_id))
        return {
            "managed": True,
            "workspace_id": workspace_id,
            "gateway_instance_id": gateway_instance_id or self.gateway,
            "worker_container_id": self.worker,
            "config_version": self.config_version,
        }

    async def ensure(self, **kwargs):
        self.ensure_calls += 1

        class Candidate:
            gateway_instance_id = self.gateway
            boot_generation = "e" * 32
            gateway_upstream = "http://172.31.0.2:8897"
            active = True
            gateway_created = False
            worker_created = False

        return Candidate()


def _service(tmp_path: pathlib.Path, gateway: FakeGateway | None = None):
    service = provisioner.Provisioner(_settings(tmp_path))
    service._gateway = gateway or FakeGateway()
    return service


def _handle(attempt_handle_worker: str = WORKER,
            attempt_handle_gateway: str = GATEWAY) -> WorkerHandle:
    return WorkerHandle(
        WORKSPACE, attempt_handle_worker, "http://172.32.0.2:7860", "token",
        CONFIG_VERSION, gateway_instance_id=attempt_handle_gateway,
        gateway_generation="d" * 32,
        gateway_upstream="http://172.31.0.2:8897")


def _record(service, attempt_id: str, *, worker_created: bool,
            gateway_created: bool, worker: str = WORKER,
            gateway: str = GATEWAY) -> None:
    service._record_attempt(
        WORKSPACE, attempt_id, _handle(worker, gateway),
        worker_created=worker_created, gateway_created=gateway_created)


def _write_attempt(tmp_path: pathlib.Path, payload: dict,
                   workspace: str = WORKSPACE) -> pathlib.Path:
    path = tmp_path / "root" / ".gateway" / f"attempt-{workspace}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _worker_inspect(worker: str = WORKER,
                    config_version: int = CONFIG_VERSION,
                    workspace: str = WORKSPACE) -> dict:
    return {
        "Id": worker,
        "Name": "/" + provisioner.container_name(workspace),
        "Config": {"Labels": {
            "easel.managed": "true",
            "easel.workspace-id": workspace,
            "easel.config-version": str(config_version),
        }},
        "State": {"Running": True, "Status": "running",
                  "Health": {"Status": "healthy"}},
        "NetworkSettings": {"Networks": {
            provisioner.network_name(workspace): {
                "IPAddress": "172.32.0.2"}}},
        "HostConfig": {},
    }


def test_pure_reuse_cannot_overwrite_pending_cleanup(tmp_path):
    service = _service(tmp_path)
    _record(service, "a" * 32, worker_created=False, gateway_created=True)

    with pytest.raises(provisioner.ProvisionerError) as refused:
        _record(service, "e" * 32, worker_created=False, gateway_created=False)

    assert refused.value.code == "gateway_cleanup_pending"
    record = service._load_attempt(WORKSPACE)
    assert record is not None
    assert record["attempt_id"] == "a" * 32
    assert record["status"] == "pending"
    assert record["scope"] == "gateway"


def test_pure_reuse_same_attempt_keeps_pending_cleanup(tmp_path):
    service = _service(tmp_path)
    _record(service, "a" * 32, worker_created=False, gateway_created=True)

    _record(service, "a" * 32, worker_created=False, gateway_created=False)

    record = service._load_attempt(WORKSPACE)
    assert record is not None
    assert record["attempt_id"] == "a" * 32
    assert record["status"] == "pending"
    assert record["scope"] == "gateway"


def test_created_attempt_cannot_overwrite_pending_cleanup(tmp_path):
    service = _service(tmp_path)
    _record(service, "a" * 32, worker_created=False, gateway_created=True)

    with pytest.raises(provisioner.ProvisionerError) as refused:
        _record(service, "e" * 32, worker_created=True, gateway_created=True)

    assert refused.value.code == "gateway_cleanup_pending"
    record = service._load_attempt(WORKSPACE)
    assert record is not None
    assert record["attempt_id"] == "a" * 32


def test_pure_reuse_writes_self_consistent_completed_tombstone(tmp_path):
    service = _service(tmp_path)
    _record(service, "e" * 32, worker_created=False, gateway_created=False)

    record = service._load_attempt(WORKSPACE)
    assert record == {
        "attempt_id": "e" * 32,
        "worker_container_id": WORKER,
        "gateway_instance_id": GATEWAY,
        "worker_created": False,
        "gateway_created": False,
        "scope": "none",
        "status": "completed",
    }

    restarted = _service(tmp_path)
    assert restarted._load_attempt(WORKSPACE) == record


def test_completed_tombstone_allows_new_created_attempt(tmp_path):
    service = _service(tmp_path)
    _record(service, "a" * 32, worker_created=False, gateway_created=True)
    asyncio.run(service.commit_attempt(
        WORKSPACE, WORKER, GATEWAY, attempt_id="a" * 32))
    assert service._load_attempt(WORKSPACE)["status"] == "completed"

    _record(service, "e" * 32, worker_created=True, gateway_created=True)
    record = service._load_attempt(WORKSPACE)
    assert record is not None
    assert record["attempt_id"] == "e" * 32
    assert record["status"] == "pending"
    assert record["scope"] == "worker_gateway"


def test_commit_requires_exact_attempt_and_target_identity(tmp_path):
    service = _service(tmp_path)
    _record(service, "a" * 32, worker_created=False, gateway_created=True)

    with pytest.raises(provisioner.ProvisionerError) as wrong_attempt:
        asyncio.run(service.commit_attempt(
            WORKSPACE, WORKER, GATEWAY, attempt_id="e" * 32))
    assert wrong_attempt.value.code == "gateway_commit_superseded"

    with pytest.raises(provisioner.ProvisionerError) as wrong_gateway:
        asyncio.run(service.commit_attempt(
            WORKSPACE, WORKER, "f" * 64, attempt_id="a" * 32))
    assert wrong_gateway.value.code == "gateway_commit_superseded"

    asyncio.run(service.commit_attempt(
        WORKSPACE, WORKER, GATEWAY, attempt_id="a" * 32))
    assert service._load_attempt(WORKSPACE)["status"] == "completed"
    # 同 attempt 重试幂等，不改变 tombstone 或目标。
    asyncio.run(service.commit_attempt(
        WORKSPACE, WORKER, GATEWAY, attempt_id="a" * 32))
    assert service._load_attempt(WORKSPACE)["attempt_id"] == "a" * 32


def test_commit_without_record_is_a_noop(tmp_path):
    service = _service(tmp_path)
    asyncio.run(service.commit_attempt(
        WORKSPACE, WORKER, GATEWAY, attempt_id="a" * 32))
    assert service._load_attempt(WORKSPACE) is None


def test_legacy_no_resource_record_requires_verified_recovery(tmp_path):
    service = _service(tmp_path)
    _write_attempt(tmp_path, {
        "attempt_id": "e" * 32,
        "worker_container_id": WORKER,
        "gateway_instance_id": GATEWAY,
        "worker_created": False,
        "gateway_created": False,
        "scope": "gateway",
        "status": "pending",
    })

    with pytest.raises(provisioner.ProvisionerError) as normal_abort:
        asyncio.run(service.abort(
            WORKSPACE, WORKER, GATEWAY, attempt_id="a" * 32,
            scope="gateway", worker_created=False, gateway_created=True))
    assert normal_abort.value.code == "gateway_abort_superseded"
    assert service._gateway.stops == []

    async def worker_inspect(instance_id: str):
        return _worker_inspect() if instance_id == WORKER else None

    service._inspect_instance = worker_inspect
    with pytest.raises(provisioner.ProvisionerError) as wrong_target:
        asyncio.run(service.recover_abort(
            WORKSPACE, WORKER, "f" * 64, CONFIG_VERSION,
            attempt_id="a" * 32, scope="gateway",
            worker_created=False, gateway_created=True))
    assert wrong_target.value.code == "gateway_abort_recovery_refused"
    assert service._gateway.stops == []

    asyncio.run(service.recover_abort(
        WORKSPACE, WORKER, GATEWAY, CONFIG_VERSION,
        attempt_id="a" * 32, scope="gateway",
        worker_created=False, gateway_created=True))
    assert service._gateway.stops == [(WORKSPACE, WORKER, GATEWAY)]
    record = service._load_attempt(WORKSPACE)
    assert record is not None
    assert record["attempt_id"] == "a" * 32
    assert record["status"] == "completed"

    # 重启后同一恢复请求幂等，不再停止 gateway。
    restarted = _service(tmp_path, service._gateway)
    restarted._inspect_instance = worker_inspect
    asyncio.run(restarted.recover_abort(
        WORKSPACE, WORKER, GATEWAY, CONFIG_VERSION,
        attempt_id="a" * 32, scope="gateway",
        worker_created=False, gateway_created=True))
    assert service._gateway.stops == [(WORKSPACE, WORKER, GATEWAY)]


def test_recovery_refuses_live_pending_or_non_gateway_scope(tmp_path):
    service = _service(tmp_path)
    _record(service, "a" * 32, worker_created=False, gateway_created=True)

    async def worker_inspect(instance_id: str):
        return _worker_inspect() if instance_id == WORKER else None

    service._inspect_instance = worker_inspect
    with pytest.raises(provisioner.ProvisionerError) as live:
        asyncio.run(service.recover_abort(
            WORKSPACE, WORKER, GATEWAY, CONFIG_VERSION,
            attempt_id="e" * 32, scope="gateway",
            worker_created=False, gateway_created=True))
    assert live.value.code == "gateway_abort_superseded"
    assert service._gateway.stops == []

    _write_attempt(tmp_path, {
        "attempt_id": "e" * 32,
        "worker_container_id": WORKER,
        "gateway_instance_id": GATEWAY,
        "worker_created": False,
        "gateway_created": False,
        "scope": "gateway",
        "status": "pending",
    })
    restarted = _service(tmp_path, service._gateway)
    restarted._inspect_instance = worker_inspect
    with pytest.raises(provisioner.ProvisionerError) as worker_scope:
        asyncio.run(restarted.recover_abort(
            WORKSPACE, WORKER, GATEWAY, CONFIG_VERSION,
            attempt_id="a" * 32, scope="worker",
            worker_created=True, gateway_created=False))
    assert worker_scope.value.code == "gateway_abort_recovery_refused"
    assert service._gateway.stops == []


def test_recovery_refuses_corrupt_or_unknown_created_record(tmp_path):
    service = _service(tmp_path)
    service._inspect_instance = lambda instance_id: _worker_inspect()
    _write_attempt(tmp_path, {
        "attempt_id": "e" * 32,
        "worker_container_id": WORKER,
        "gateway_instance_id": GATEWAY,
        "worker_created": True,
        "gateway_created": False,
        "scope": "gateway",
        "status": "pending",
    })
    with pytest.raises(provisioner.ProvisionerError) as refused:
        asyncio.run(service.recover_abort(
            WORKSPACE, WORKER, GATEWAY, CONFIG_VERSION,
            attempt_id="a" * 32, scope="gateway",
            worker_created=False, gateway_created=True))
    assert refused.value.code == "gateway_abort_recovery_refused"
    assert service._gateway.stops == []


def test_orphan_takeover_replaces_pending_from_lost_attempt(tmp_path):
    service = _service(tmp_path)
    _record(service, "a" * 32, worker_created=False, gateway_created=True)

    with pytest.raises(provisioner.ProvisionerError) as refused:
        asyncio.run(service._with_gateway(
            _handle(), 2, worker_created=False, attempt_id="e" * 32))
    assert refused.value.code == "gateway_cleanup_pending"

    handle = asyncio.run(service._with_gateway(
        _handle(), 2, worker_created=False, attempt_id="e" * 32,
        orphan_takeover=True))
    assert handle.gateway_instance_id == GATEWAY
    record = service._load_attempt(WORKSPACE)
    assert record is not None
    assert record["attempt_id"] == "e" * 32
    assert record["scope"] == "none"
    assert record["status"] == "completed"


def test_field_shape_legacy_record_recovers_through_runtime_manager(tmp_path):
    """同构现场：DB stopping/gateway A，磁盘是旧版无资源 B，真实恢复收口。"""
    with Store.open(tmp_path / "control.sqlite3") as store:
        user = store.create_user("A")
        workspace = user.workspace_id
        lease = store.claim_worker_start(
            workspace, 1, "owner-a", 1000, 60)[1]
        assert lease is not None
        handle = WorkerHandle(
            workspace, WORKER, "http://172.32.0.2:7860", "token", 1,
            gateway_instance_id=GATEWAY,
            gateway_generation="d" * 32,
            gateway_upstream="http://172.31.0.2:8897")
        assert store.stage_worker_gateway_start(
            workspace, "owner-a", lease.generation, handle, "a" * 32,
            1001) is True
        assert store.begin_worker_abort(
            workspace, "owner-a", lease.generation, "a" * 32,
            "gateway", False, True, 1002) is True
        _write_attempt(tmp_path, {
            "attempt_id": "e" * 32,
            "worker_container_id": WORKER,
            "gateway_instance_id": GATEWAY,
            "worker_created": False,
            "gateway_created": False,
            "scope": "gateway",
            "status": "pending",
        }, workspace=workspace)
        gateway = FakeGateway(config_version=1)
        service = _service(tmp_path, gateway)

        async def worker_inspect(instance_id: str):
            return (_worker_inspect(config_version=1, workspace=workspace)
                    if instance_id == WORKER else None)

        service._inspect_instance = worker_inspect

        class Adapter:
            async def abort(self, *args, **kwargs):
                return await service.abort(*args, **kwargs)

            async def recover_abort(self, *args, **kwargs):
                return await service.recover_abort(*args, **kwargs)

        manager = RuntimeManager(
            store, Adapter(), gateway_required=True, clock=lambda: 1200)
        stopping = store.get_worker_lease(workspace)
        assert stopping is not None
        assert stopping.state == "stopping"

        asyncio.run(manager._recover_stop(workspace, stopping, 1300))

        after = store.get_worker_lease(workspace)
        assert after is not None
        assert after.state == "ready"
        assert after.gateway_state == "unavailable"
        assert after.container_id == WORKER
        assert after.service_token == "token"
        assert gateway.stops == [(workspace, WORKER, GATEWAY)]
        record_path = (tmp_path / "root" / ".gateway"
                       / f"attempt-{workspace}.json")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        assert record["attempt_id"] == "a" * 32
        assert record["status"] == "completed"


def test_stop_without_gateway_id_requires_no_current_gateway(tmp_path):
    gateway = FakeGateway()
    service = _service(tmp_path, gateway)

    async def no_worker(workspace_id: str):
        return None

    async def no_sidecar(workspace_id: str):
        return None

    service._inspect_raw = no_worker
    service._stop_sidecar = no_sidecar

    asyncio.run(service.stop(
        WORKSPACE, WORKER, "", protocol_version=2))

    gateway.current_gateway = "e" * 64
    with pytest.raises(provisioner.ProvisionerError) as refused:
        asyncio.run(service.stop(
            WORKSPACE, WORKER, "", protocol_version=2))
    assert refused.value.code == "gateway_target_mismatch"


def test_with_gateway_refuses_before_creating_when_cleanup_pending(tmp_path):
    gateway = FakeGateway()
    service = _service(tmp_path, gateway)
    _record(service, "a" * 32, worker_created=False, gateway_created=True)

    with pytest.raises(provisioner.ProvisionerError) as refused:
        asyncio.run(service._with_gateway(
            _handle(), 2, worker_created=False, attempt_id="e" * 32))

    assert refused.value.code == "gateway_cleanup_pending"
    assert gateway.ensure_calls == 0
    assert service._load_attempt(WORKSPACE)["attempt_id"] == "a" * 32


def test_with_gateway_pure_reuse_records_completed_noop(tmp_path):
    gateway = FakeGateway()
    service = _service(tmp_path, gateway)

    handle = asyncio.run(service._with_gateway(
        _handle(), 2, worker_created=False, attempt_id="e" * 32))

    assert handle.gateway_instance_id == GATEWAY
    assert gateway.ensure_calls == 1
    record = service._load_attempt(WORKSPACE)
    assert record is not None
    assert record["attempt_id"] == "e" * 32
    assert record["scope"] == "none"
    assert record["status"] == "completed"
