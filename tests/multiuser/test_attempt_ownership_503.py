"""5C3/503：attempt 归属、pending 边界与旧坏记录受限恢复。

本文件不连接 Docker；只使用临时目录、假 ID 与内存 gateway fake。
"""
from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import pathlib
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.control.runtime import RuntimeManager, RuntimeUnavailable
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


class Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


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
            "boot_generation": "e" * 32,
            "gateway_upstream": "http://172.31.0.9:8897",
            "worker_origin": "http://172.32.0.2:7860",
            "active": True,
            "available": True,
            "configured": True,
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
        "gateway_generation": "d" * 32,
        "gateway_upstream": "http://172.31.0.2:8897",
        "worker_created": False,
        "gateway_created": False,
        "scope": "none",
        "status": "completed",
        "config_version": CONFIG_VERSION,
    }

    restarted = _service(tmp_path)
    assert restarted._load_attempt(WORKSPACE) == record


def test_committed_attempt_allows_new_created_attempt(tmp_path):
    service = _service(tmp_path)
    _record(service, "a" * 32, worker_created=False, gateway_created=True)
    asyncio.run(service.commit_attempt(
        WORKSPACE, WORKER, GATEWAY, attempt_id="a" * 32))
    assert service._load_attempt(WORKSPACE)["status"] == "committed"
    assert _service(
        tmp_path, service._gateway)._load_attempt(
            WORKSPACE)["status"] == "committed"

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
    assert service._load_attempt(WORKSPACE)["status"] == "committed"
    # 同 attempt 重试幂等，不改变 tombstone 或目标。
    asyncio.run(service.commit_attempt(
        WORKSPACE, WORKER, GATEWAY, attempt_id="a" * 32))
    assert service._load_attempt(WORKSPACE)["attempt_id"] == "a" * 32


def test_committed_attempt_cannot_be_aborted_as_cleaned(tmp_path):
    """成功 commit 与物理清理完成必须是不同状态，迟到 abort 不得假报成功。"""
    service = _service(tmp_path)
    _record(service, "a" * 32, worker_created=False, gateway_created=True)
    asyncio.run(service.commit_attempt(
        WORKSPACE, WORKER, GATEWAY, attempt_id="a" * 32))

    with pytest.raises(provisioner.ProvisionerError) as refused:
        asyncio.run(service.abort(
            WORKSPACE, WORKER, GATEWAY, attempt_id="a" * 32,
            scope="gateway", worker_created=False, gateway_created=True))

    assert refused.value.code == "gateway_abort_superseded"
    assert service._gateway.stops == []
    record = service._load_attempt(WORKSPACE)
    assert record is not None
    assert record["status"] == "committed"


def test_empty_attempt_commit_cannot_close_pending_cleanup(tmp_path):
    """control 不能用空 attempt 按目标猜归属，提前关闭其他 owner 的承诺。"""
    service = _service(tmp_path)
    _record(service, "a" * 32, worker_created=False, gateway_created=True)

    with pytest.raises(provisioner.ProvisionerError) as refused:
        asyncio.run(service.commit_attempt(
            WORKSPACE, WORKER, GATEWAY, attempt_id=""))

    assert refused.value.code == "gateway_commit_superseded"
    record = service._load_attempt(WORKSPACE)
    assert record is not None
    assert record["status"] == "pending"


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


def test_takeover_cas_persists_fenced_pending_attempt(tmp_path):
    """接管先完成 owner/generation/attempt 交接，旧 attempt 持久为待收口。"""
    with Store.open(tmp_path / "control.sqlite3") as store:
        workspace = store.create_user("A").workspace_id
        lease = store.claim_worker_start(
            workspace, 1, "owner-a", 1000, 60)[1]
        assert lease is not None
        handle = WorkerHandle(
            workspace, WORKER, "http://172.32.0.2:7860", "token", 1,
            gateway_instance_id=GATEWAY,
            gateway_generation="d" * 32,
            gateway_upstream="http://172.31.0.2:8897",
            gateway_state="staged")
        assert store.stage_worker_gateway_start(
            workspace, "owner-a", lease.generation, handle, "a" * 32,
            1001) is True

        taken = store.takeover_worker_gateway_refresh(
            workspace, lease.generation, "owner-b", "b" * 32, 1200, 60,
            expected_owner="owner-a", expected_attempt="a" * 32,
            expires_at_or_before=1200)

        assert taken is not None
        assert taken.state == "starting"
        assert taken.owner_id == "owner-b"
        assert taken.generation == lease.generation + 1
        assert taken.attempt_id == "b" * 32
        assert taken.pending_attempt_id == "a" * 32
        assert taken.container_id == WORKER
        assert taken.gateway_instance_id == GATEWAY
        # 同一过期代次不能被第三个 owner 再次接管。
        assert store.takeover_worker_gateway_refresh(
            workspace, lease.generation, "owner-c", "c" * 32, 1200, 60,
            expected_owner="owner-a", expected_attempt="a" * 32,
            expires_at_or_before=1200) is None
        # 未收口旧 attempt 前不能进入候选 staging。
        assert store.begin_worker_gateway_refresh(
            workspace, taken.generation, "owner-b", handle, "b" * 32,
            1201, 60) is None
        assert store.clear_pending_attempt(
            workspace, taken.generation, "owner-b", "b" * 32, 1202) is True
        assert store.begin_worker_gateway_refresh(
            workspace, taken.generation, "owner-b", handle, "b" * 32,
            1202, 60) is not None


def test_restore_takeover_fences_old_cleanup_before_commit(tmp_path):
    """B 接管窗口内，A 的真实 cleanup 不得假报成功或关闭物理清理承诺。"""

    async def scenario() -> None:
        with Store.open(tmp_path / "control.sqlite3") as store:
            workspace = store.create_user("A").workspace_id
            lease = store.claim_worker_start(
                workspace, 1, "owner-a", 1000, 60)[1]
            assert lease is not None
            handle = WorkerHandle(
                workspace, WORKER, "http://172.32.0.2:7860", "token", 1,
                gateway_instance_id=GATEWAY,
                gateway_generation="d" * 32,
                gateway_upstream="http://172.31.0.2:8897",
                gateway_state="staged")
            original_attempt = "a" * 32
            assert store.stage_worker_gateway_start(
                workspace, "owner-a", lease.generation, handle,
                original_attempt, 1001) is True
            gateway = FakeGateway(config_version=1)
            service = _service(tmp_path, gateway)
            service._record_attempt(
                workspace, original_attempt, handle,
                worker_created=False, gateway_created=True)
            restore_entered = asyncio.Event()
            release_restore = asyncio.Event()

            class Adapter:
                async def commit_attempt(self, *args, **kwargs):
                    return await service.commit_attempt(*args, **kwargs)

                async def abort(self, *args, **kwargs):
                    return await service.abort(*args, **kwargs)

                async def restore_gateway(
                        self, workspace_id, worker_container_id,
                        config_version, *, attempt_id=""):
                    restore_entered.set()
                    await release_restore.wait()
                    restored = dataclasses.replace(
                        handle, attempt_id=attempt_id,
                        gateway_state="staged", gateway_created=False)
                    # 真实 provisioner 会在同一 worker 上复用/替换 gateway
                    # 并写入新 attempt 记录；这里用真实记录逻辑模拟。
                    service._record_attempt(
                        workspace_id, attempt_id, restored,
                        worker_created=False, gateway_created=False)
                    return restored

                async def activate(self, workspace_id, worker_container_id,
                                   gateway_instance_id, boot_generation,
                                   config_version):
                    return {"gateway": {
                        "managed": True,
                        "workspace_id": workspace_id,
                        "config_version": config_version,
                        "worker_container_id": worker_container_id,
                        "gateway_instance_id": gateway_instance_id,
                        "boot_generation": boot_generation,
                        "available": True,
                    }}

            first = RuntimeManager(
                store, Adapter(), owner_id="owner-a", gateway_required=True,
                clock=lambda: 1200)
            second = RuntimeManager(
                store, Adapter(), owner_id="owner-b", gateway_required=True,
                clock=lambda: 1200)
            pending = asyncio.create_task(second.restore_gateway(
                workspace, WORKER, 1))
            try:
                await asyncio.wait_for(restore_entered.wait(), 2)

                handed_off = store.get_worker_lease(workspace)
                assert handed_off is not None
                assert handed_off.owner_id == "owner-b"
                assert handed_off.attempt_id != original_attempt
                assert handed_off.state == "starting"

                cleaned = await first._cleanup_candidate(
                    workspace, handle, lease, original_attempt)
                assert cleaned is False
                assert gateway.stops == []

                record = service._load_attempt(workspace)
                assert record is not None
                assert record["attempt_id"] == original_attempt
                assert record["status"] == "committed"
                with pytest.raises(provisioner.ProvisionerError) as late:
                    await service.abort(
                        workspace, WORKER, GATEWAY,
                        attempt_id=original_attempt, scope="gateway",
                        worker_created=False, gateway_created=True)
                assert late.value.code == "gateway_abort_superseded"
                assert gateway.stops == []

                release_restore.set()
                final = await asyncio.wait_for(pending, 2)
                assert final.gateway_instance_id == GATEWAY
                ready = store.get_worker_lease(workspace)
                assert ready is not None
                assert ready.state == "ready"
                assert ready.owner_id == "owner-b"
                # B 复用同一物理 gateway，provisioner 写 no-op tombstone；
                # 旧 A 的 pending 已在交接后按精确 attempt 收口。
                final_record = service._load_attempt(workspace)
                assert final_record["attempt_id"] == ready.attempt_id
                assert final_record["scope"] == "none"
                assert final_record["status"] == "completed"
            finally:
                release_restore.set()
                if not pending.done():
                    pending.cancel()
                    try:
                        await pending
                    except asyncio.CancelledError:
                        pass

    asyncio.run(scenario())


def _seed_expired_ensure(tmp_path):
    """构造过期 starting：owner-a 已 stage 候选并写入 A 的清理承诺。"""
    store = Store.connect(tmp_path / "control.sqlite3")
    workspace = store.create_user("A").workspace_id
    lease = store.claim_worker_start(
        workspace, 1, "owner-a", 1000, 60)[1]
    assert lease is not None
    handle = WorkerHandle(
        workspace, WORKER, "http://172.32.0.2:7860", "token", 1,
        gateway_instance_id=GATEWAY,
        gateway_generation="d" * 32,
        gateway_upstream="http://172.31.0.2:8897",
        gateway_state="staged", gateway_created=True)
    original_attempt = "a" * 32
    assert store.stage_worker_gateway_start(
        workspace, "owner-a", lease.generation, handle,
        original_attempt, 1001) is True
    gateway = FakeGateway(config_version=1)
    service = _service(tmp_path, gateway)
    service._record_attempt(
        workspace, original_attempt, handle,
        worker_created=False, gateway_created=True)
    return store, service, gateway, workspace, lease, handle, original_attempt


def _ready_handle(handle: WorkerHandle) -> WorkerHandle:
    return dataclasses.replace(
        handle, gateway_state="available", attempt_id="")


def test_ensure_ready_takeover_fences_old_cleanup_before_commit(tmp_path):
    """普通首屏 refresh 必须先交接 owner/generation，再收口旧 attempt。"""

    async def scenario() -> None:
        store, service, gateway, workspace, lease, handle, original = \
            _seed_expired_ensure(tmp_path)
        entered = asyncio.Event()
        release = asyncio.Event()

        class Adapter:
            async def commit_attempt(self, *args, **kwargs):
                return await service.commit_attempt(*args, **kwargs)

            async def abort(self, *args, **kwargs):
                return await service.abort(*args, **kwargs)

            async def ensure(self, *args, **kwargs):
                # 模拟 B 已越过交接并进入远程 ensure，但尚未 staging。
                entered.set()
                await release.wait()
                return _ready_handle(handle)

        with store:
            adapter = Adapter()
            first = RuntimeManager(
                store, adapter, owner_id="owner-a", gateway_required=True,
                clock=lambda: 1200)
            second = RuntimeManager(
                store, adapter, owner_id="owner-b", gateway_required=True,
                clock=lambda: 1200)
            pending = asyncio.create_task(second.ensure_ready(workspace, 1))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                handed = store.get_worker_lease(workspace)
                assert handed is not None
                assert handed.owner_id == "owner-b"
                assert handed.state == "starting"
                assert handed.attempt_id != original
                record = service._load_attempt(workspace)
                assert record is not None
                assert record["status"] == "committed"

                cleaned = await first._cleanup_candidate(
                    workspace, handle, lease, original)
                assert cleaned is False
                assert gateway.stops == []
                after_cleanup = store.get_worker_lease(workspace)
                # 旧 cleanup 不能把新 owner 的代次改成 stopping 后滞留。
                assert after_cleanup is not None
                assert after_cleanup.state == "starting"
                assert after_cleanup.owner_id == "owner-b"

                release.set()
                final = await asyncio.wait_for(pending, 2)
                assert final.container_id == WORKER
                ready = store.get_worker_lease(workspace)
                assert ready is not None
                assert ready.state == "ready"
                assert ready.owner_id == "owner-b"
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(scenario())


def test_ensure_ready_old_cleanup_wins_then_next_claim_recovers(tmp_path):
    """旧 cleanup 先完成时，B 的下一次 ensure_ready 从 ready/unavailable 恢复。"""

    async def scenario() -> None:
        store, service, gateway, workspace, lease, handle, original = \
            _seed_expired_ensure(tmp_path)

        class Adapter:
            async def commit_attempt(self, *args, **kwargs):
                return await service.commit_attempt(*args, **kwargs)

            async def abort(self, *args, **kwargs):
                return await service.abort(*args, **kwargs)

            async def ensure(self, *args, **kwargs):
                return _ready_handle(handle)

        with store:
            adapter = Adapter()
            first = RuntimeManager(
                store, adapter, owner_id="owner-a", gateway_required=True,
                clock=lambda: 1200)
            second = RuntimeManager(
                store, adapter, owner_id="owner-b", gateway_required=True,
                clock=lambda: 1200)
            cleaned = await first._cleanup_candidate(
                workspace, handle, lease, original)
            assert cleaned is True
            after_cleanup = store.get_worker_lease(workspace)
            assert after_cleanup is not None
            assert after_cleanup.state == "ready"
            assert after_cleanup.gateway_state == "unavailable"
            assert after_cleanup.attempt_id == ""
            assert gateway.stops == [(workspace, WORKER, GATEWAY)]

            final = await second.ensure_ready(workspace, 1)
            assert final.gateway_instance_id == GATEWAY
            ready = store.get_worker_lease(workspace)
            assert ready is not None
            assert ready.state == "ready"
            assert ready.owner_id == "owner-b"

    asyncio.run(scenario())


def test_ensure_ready_takeover_remote_failure_fails_closed_to_ready(tmp_path):
    """交接后远程 ensure 失败：保留 worker/ready，绝不滞留 stopping。"""

    async def scenario() -> None:
        store, service, gateway, workspace, lease, handle, original = \
            _seed_expired_ensure(tmp_path)

        class Adapter:
            async def commit_attempt(self, *args, **kwargs):
                return await service.commit_attempt(*args, **kwargs)

            async def abort(self, *args, **kwargs):
                return await service.abort(*args, **kwargs)

            async def ensure(self, *args, **kwargs):
                raise RuntimeUnavailable(
                    "worker_start_failed", 503, retry_after=5)

        with store:
            adapter = Adapter()
            manager = RuntimeManager(
                store, adapter, owner_id="owner-b", gateway_required=True,
                clock=lambda: 1200)
            with pytest.raises(RuntimeUnavailable):
                await manager.ensure_ready(workspace, 1)
            failed = store.get_worker_lease(workspace)
            assert failed is not None
            assert failed.state == "ready"
            assert failed.gateway_state == "unavailable"
            assert failed.container_id == WORKER
            assert failed.owner_id == "owner-b"
            record = service._load_attempt(workspace)
            assert record is not None
            assert record["status"] == "committed"
            assert gateway.stops == []

    asyncio.run(scenario())


def test_ensure_ready_takeover_cancel_leaves_recoverable_starting(tmp_path):
    """B 在远程 ensure 中被取消：保留可接管的 starting，后续 owner 可恢复。"""

    async def scenario() -> None:
        store, service, gateway, workspace, lease, handle, original = \
            _seed_expired_ensure(tmp_path)
        entered = asyncio.Event()
        never = asyncio.Event()

        class Adapter:
            async def commit_attempt(self, *args, **kwargs):
                return await service.commit_attempt(*args, **kwargs)

            async def abort(self, *args, **kwargs):
                return await service.abort(*args, **kwargs)

            async def ensure(self, *args, **kwargs):
                entered.set()
                await never.wait()
                return _ready_handle(handle)

        with store:
            adapter = Adapter()
            second = RuntimeManager(
                store, adapter, owner_id="owner-b", gateway_required=True,
                clock=lambda: 1200)
            pending = asyncio.create_task(second.ensure_ready(workspace, 1))
            await asyncio.wait_for(entered.wait(), 2)
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            never.set()
            cancelled = store.get_worker_lease(workspace)
            assert cancelled is not None
            assert cancelled.state == "starting"
            assert cancelled.owner_id == "owner-b"
            assert cancelled.pending_attempt_id == ""
            record = service._load_attempt(workspace)
            assert record is not None
            assert record["status"] == "committed"
            assert gateway.stops == []

            recovered = RuntimeManager(
                store, adapter, owner_id="owner-c", gateway_required=True,
                clock=lambda: cancelled.lease_expires_at + 1)
            final = await recovered.ensure_ready(workspace, 1)
            assert final.container_id == WORKER
            ready = store.get_worker_lease(workspace)
            assert ready is not None
            assert ready.state == "ready"
            assert ready.owner_id == "owner-c"

    asyncio.run(scenario())


def _lost_response_adapter(service, handle, replacement, lost):
    """模拟 provisioner 已持久化候选、control 在接收/落盘前中断。"""

    class Adapter:
        async def commit_attempt(self, *args, **kwargs):
            return await service.commit_attempt(*args, **kwargs)

        async def abort(self, *args, **kwargs):
            return await service.abort(*args, **kwargs)

        def _candidate(self, attempt_id, *, created):
            return dataclasses.replace(
                handle, gateway_instance_id=replacement,
                gateway_generation="e" * 32,
                gateway_upstream="http://172.31.0.9:8897",
                gateway_state="available", attempt_id=attempt_id,
                worker_created=False, gateway_created=created)

        async def restore_gateway(self, workspace_id, worker_container_id,
                                  config_version, *, attempt_id=""):
            if lost["count"] > 0:
                lost["count"] -= 1
                candidate = self._candidate(attempt_id, created=True)
                service._record_attempt(
                    workspace_id, attempt_id, candidate,
                    worker_created=False, gateway_created=True)
                raise asyncio.CancelledError()
            candidate = self._candidate(attempt_id, created=False)
            service._record_attempt(
                workspace_id, attempt_id, candidate,
                worker_created=False, gateway_created=False)
            return candidate

        async def ensure(self, workspace_id, config_version, *,
                         attempt_id="", orphan_takeover=False):
            if lost["count"] > 0:
                lost["count"] -= 1
                candidate = self._candidate(attempt_id, created=True)
                service._record_attempt(
                    workspace_id, attempt_id, candidate,
                    worker_created=False, gateway_created=True)
                raise asyncio.CancelledError()
            candidate = self._candidate(attempt_id, created=False)
            service._record_attempt(
                workspace_id, attempt_id, candidate,
                worker_created=False, gateway_created=False)
            return candidate

    return Adapter()


def test_lost_restore_response_recovers_exact_attempt_candidate(tmp_path):
    """restore 响应丢失：重开 DB 后按精确 attempt 找到真实候选并继续接管。"""
    replacement = "f" * 64

    async def first_loss():
        store, service, gateway, workspace, lease, handle, original = \
            _seed_expired_ensure(tmp_path)
        lost = {"count": 1}
        with store:
            manager = RuntimeManager(
                store, _lost_response_adapter(
                    service, handle, replacement, lost),
                owner_id="owner-b", gateway_required=True,
                clock=lambda: 1200)
            with pytest.raises(asyncio.CancelledError):
                await manager.restore_gateway(workspace, WORKER, 1)
            before = store.get_worker_lease(workspace)
            assert before is not None
            assert before.gateway_instance_id == GATEWAY
            record = service._load_attempt(workspace)
            assert record is not None
            assert record["gateway_instance_id"] == replacement
            assert record["status"] == "pending"
            expires = before.lease_expires_at
        return workspace, expires, lost, handle

    workspace, expires, lost, handle = asyncio.run(first_loss())
    with Store.open(tmp_path / "control.sqlite3") as store:
        gateway = FakeGateway(config_version=1)
        service = _service(tmp_path, gateway)
        manager = RuntimeManager(
            store, _lost_response_adapter(service, handle, replacement, lost),
            owner_id="owner-c", gateway_required=True,
            clock=lambda: expires + 1)
        final = asyncio.run(manager.restore_gateway(workspace, WORKER, 1))
        assert final is not None
        assert final.gateway_instance_id == replacement
        ready = store.get_worker_lease(workspace)
        assert ready is not None
        assert ready.state == "ready"
        assert ready.gateway_instance_id == replacement
        assert ready.attempt_id == final.attempt_id
        record = service._load_attempt(workspace)
        assert record is not None
        assert record["status"] in ("committed", "completed")


def test_lost_ensure_response_recovers_through_first_screen_entry(tmp_path):
    """普通首屏 ensure_ready 的响应丢失同样必须恢复真实候选。"""
    replacement = "f" * 64

    async def first_loss():
        store, service, gateway, workspace, lease, handle, original = \
            _seed_expired_ensure(tmp_path)
        lost = {"count": 1}
        with store:
            manager = RuntimeManager(
                store, _lost_response_adapter(
                    service, handle, replacement, lost),
                owner_id="owner-b", gateway_required=True,
                clock=lambda: 1200)
            with pytest.raises(asyncio.CancelledError):
                await manager.ensure_ready(workspace, 1)
            before = store.get_worker_lease(workspace)
            assert before is not None
            assert before.gateway_instance_id == GATEWAY
            assert service._load_attempt(workspace)["status"] == "pending"
            expires = before.lease_expires_at
        return workspace, expires, lost, handle

    workspace, expires, lost, handle = asyncio.run(first_loss())
    with Store.open(tmp_path / "control.sqlite3") as store:
        service = _service(tmp_path, FakeGateway(config_version=1))
        manager = RuntimeManager(
            store, _lost_response_adapter(service, handle, replacement, lost),
            owner_id="owner-c", gateway_required=True,
            clock=lambda: expires + 1)
        final = asyncio.run(manager.ensure_ready(workspace, 1))
        assert final.gateway_instance_id == replacement
        ready = store.get_worker_lease(workspace)
        assert ready is not None
        assert ready.state == "ready"
        assert ready.gateway_instance_id == replacement


def test_repeated_lost_responses_keep_advancing_without_commit_loop(tmp_path):
    """恢复在建 ready 前再次中断后仍能前进，不出现无限 commit 失败。"""
    replacement = "f" * 64

    async def first_loss():
        store, service, gateway, workspace, lease, handle, original = \
            _seed_expired_ensure(tmp_path)
        lost = {"count": 1}
        with store:
            manager = RuntimeManager(
                store, _lost_response_adapter(
                    service, handle, replacement, lost),
                owner_id="owner-b", gateway_required=True,
                clock=lambda: 1200)
            with pytest.raises(asyncio.CancelledError):
                await manager.restore_gateway(workspace, WORKER, 1)
            first_expiry = store.get_worker_lease(workspace).lease_expires_at
        return workspace, first_expiry, lost, handle

    workspace, expiry, lost, handle = asyncio.run(first_loss())
    with Store.open(tmp_path / "control.sqlite3") as store:
        service = _service(tmp_path, FakeGateway(config_version=1))
        manager = RuntimeManager(
            store, _lost_response_adapter(service, handle, replacement, lost),
            owner_id="owner-c", gateway_required=True,
            clock=lambda: expiry + 1)
        original_complete = store.complete_worker_gateway_refresh
        state = {"cancel": True}

        def cancel_once(*args, **kwargs):
            if state["cancel"]:
                state["cancel"] = False
                raise asyncio.CancelledError()
            return original_complete(*args, **kwargs)

        store.complete_worker_gateway_refresh = cancel_once
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(manager.restore_gateway(workspace, WORKER, 1))
        second = store.get_worker_lease(workspace)
        assert second is not None
        assert second.gateway_instance_id == replacement
        assert second.state == "starting"
        assert second.pending_attempt_id == ""
        expiry = second.lease_expires_at
        store.complete_worker_gateway_refresh = original_complete
    with Store.open(tmp_path / "control.sqlite3") as store:
        service = _service(tmp_path, FakeGateway(config_version=1))
        manager = RuntimeManager(
            store, _lost_response_adapter(service, handle, replacement, lost),
            owner_id="owner-d", gateway_required=True,
            clock=lambda: expiry + 1)
        final = asyncio.run(manager.restore_gateway(workspace, WORKER, 1))
        assert final is not None
        assert final.gateway_instance_id == replacement
        ready = store.get_worker_lease(workspace)
        assert ready is not None
        assert ready.state == "ready"
        assert ready.gateway_instance_id == replacement


def test_recovered_gateway_is_used_by_normal_stop(tmp_path):
    """恢复后的租约指向真实替换候选；正常 Stop 必须按该实例收口。"""
    replacement = "f" * 64

    async def recover():
        store, service, gateway, workspace, lease, handle, original = \
            _seed_expired_ensure(tmp_path)
        lost = {"count": 1}
        with store:
            manager = RuntimeManager(
                store, _lost_response_adapter(
                    service, handle, replacement, lost),
                owner_id="owner-b", gateway_required=True,
                clock=lambda: 1200)
            with pytest.raises(asyncio.CancelledError):
                await manager.restore_gateway(workspace, WORKER, 1)
            expires = store.get_worker_lease(workspace).lease_expires_at
        return workspace, expires, lost, handle

    workspace, expires, lost, handle = asyncio.run(recover())
    with Store.open(tmp_path / "control.sqlite3") as store:
        gateway = FakeGateway(config_version=1)
        service = _service(tmp_path, gateway)
        manager = RuntimeManager(
            store, _lost_response_adapter(service, handle, replacement, lost),
            owner_id="owner-c", gateway_required=True,
            clock=lambda: expires + 1)
        asyncio.run(manager.restore_gateway(workspace, WORKER, 1))

        async def no_worker(workspace_id: str):
            return None

        async def no_sidecar(workspace_id: str):
            return None

        service._inspect_raw = no_worker
        service._stop_sidecar = no_sidecar
        asyncio.run(service.stop(
            workspace, WORKER, replacement, protocol_version=2))
        assert gateway.stops == [(workspace, WORKER, replacement)]
        assert service._load_attempt(workspace) is None


def test_pure_reuse_refresh_keeps_same_gateway_without_adoption(tmp_path):
    """未替换的纯复用不写 pending 资源承诺，也不改写 gateway 身份。"""
    async def scenario():
        store, service, gateway, workspace, lease, handle, original = \
            _seed_expired_ensure(tmp_path)
        # A 的 attempt 是纯复用：completed/no-op tombstone。
        _write_attempt(tmp_path, {
            "attempt_id": original,
            "worker_container_id": WORKER,
            "gateway_instance_id": GATEWAY,
            "worker_created": False,
            "gateway_created": False,
            "scope": "none",
            "status": "completed",
        }, workspace=workspace)
        pure = _service(tmp_path, gateway)
        lost = {"count": 0}
        with store:
            manager = RuntimeManager(
                store, _lost_response_adapter(pure, handle, GATEWAY, lost),
                owner_id="owner-b", gateway_required=True,
                clock=lambda: 1200)
            final = await manager.ensure_ready(workspace, 1)
            assert final.gateway_instance_id == GATEWAY
            ready = store.get_worker_lease(workspace)
            assert ready is not None
            assert ready.state == "ready"
            assert ready.gateway_instance_id == GATEWAY
            assert gateway.stops == []
            record = pure._load_attempt(workspace)
            assert record is not None
            assert record["scope"] == "none"
            assert record["status"] == "completed"

    asyncio.run(scenario())


def test_resolve_attempt_rejects_target_config_and_gateway_mismatch(tmp_path):
    """按 attempt 解析真实候选时必须复核 worker/config/gateway 归属。"""
    gateway = FakeGateway()
    service = _service(tmp_path, gateway)
    _record(service, "a" * 32, worker_created=False, gateway_created=True)

    resolved = asyncio.run(service.commit_attempt(
        WORKSPACE, WORKER, "", attempt_id="a" * 32, resolve=True,
        config_version=CONFIG_VERSION))
    assert resolved is not None
    assert resolved["found"] is True
    assert resolved["attempt_id"] == "a" * 32
    assert resolved["gateway_instance_id"] == GATEWAY
    assert resolved["status"] == "pending"

    with pytest.raises(provisioner.ProvisionerError) as wrong_worker:
        asyncio.run(service.commit_attempt(
            WORKSPACE, "d" * 64, "", attempt_id="a" * 32, resolve=True,
            config_version=CONFIG_VERSION))
    assert wrong_worker.value.code == "gateway_commit_superseded"

    with pytest.raises(provisioner.ProvisionerError) as wrong_config:
        asyncio.run(service.commit_attempt(
            WORKSPACE, WORKER, "", attempt_id="a" * 32, resolve=True,
            config_version=CONFIG_VERSION + 1))
    assert wrong_config.value.code == "gateway_recovery_config_mismatch"

    class UnmanagedGateway(FakeGateway):
        async def inspect(self, *, workspace_id: str,
                          gateway_instance_id: str = "") -> dict:
            return {
                "managed": False,
                "workspace_id": workspace_id,
                "gateway_instance_id": gateway_instance_id,
                "worker_container_id": WORKER,
                "config_version": CONFIG_VERSION,
            }

    (tmp_path / "unmanaged").mkdir()
    unmanaged = _service(tmp_path / "unmanaged", UnmanagedGateway())
    unmanaged._record_attempt(
        WORKSPACE, "b" * 32, _handle(), worker_created=False,
        gateway_created=True)
    with pytest.raises(provisioner.ProvisionerError) as bad_gateway:
        asyncio.run(unmanaged.commit_attempt(
            WORKSPACE, WORKER, "", attempt_id="b" * 32, resolve=True,
            config_version=CONFIG_VERSION))
    assert bad_gateway.value.code == "gateway_recovery_target_mismatch"


def test_resolve_attempt_requires_available_before_skipping_activation(
        tmp_path):
    """active 但不可用的 gateway 必须返回 staged，强制重新 activate/复核。"""
    class ActiveUnavailableGateway(FakeGateway):
        async def inspect(self, *, workspace_id: str,
                          gateway_instance_id: str = "") -> dict:
            report = await super().inspect(
                workspace_id=workspace_id,
                gateway_instance_id=gateway_instance_id)
            report["active"] = True
            report["available"] = False
            return report

    (tmp_path / "active-unavailable").mkdir()
    service = _service(tmp_path / "active-unavailable",
                       ActiveUnavailableGateway())
    _record(service, "a" * 32, worker_created=False, gateway_created=True)

    resolved = asyncio.run(service.commit_attempt(
        WORKSPACE, WORKER, "", attempt_id="a" * 32, resolve=True,
        config_version=CONFIG_VERSION))
    assert resolved is not None
    assert resolved["found"] is True
    assert resolved["gateway_state"] == "staged"


def test_commit_of_unknown_attempt_is_noop_after_promise_resolved(tmp_path):
    """接管进程可能在写新记录前崩溃；已无 pending 时精确 commit 可无操作。"""
    service = _service(tmp_path)
    _record(service, "a" * 32, worker_created=False, gateway_created=True)
    asyncio.run(service.commit_attempt(
        WORKSPACE, WORKER, GATEWAY, attempt_id="a" * 32))

    asyncio.run(service.commit_attempt(
        WORKSPACE, WORKER, GATEWAY, attempt_id="b" * 32))

    record = service._load_attempt(WORKSPACE)
    assert record is not None
    assert record["attempt_id"] == "a" * 32
    assert record["status"] == "committed"


def test_takeover_commit_failure_keeps_fence_and_later_owner_recovers(
        tmp_path):
    """接管收口失败时保持 fencing；后续 owner 可按持久 attempt 恢复。"""

    async def scenario() -> None:
        with Store.open(tmp_path / "control.sqlite3") as store:
            workspace = store.create_user("A").workspace_id
            clock = Clock(1200)
            lease = store.claim_worker_start(
                workspace, 1, "owner-a", 1000, 60)[1]
            assert lease is not None
            handle = WorkerHandle(
                workspace, WORKER, "http://172.32.0.2:7860", "token", 1,
                gateway_instance_id=GATEWAY,
                gateway_generation="d" * 32,
                gateway_upstream="http://172.31.0.2:8897",
                gateway_state="staged")
            original_attempt = "a" * 32
            assert store.stage_worker_gateway_start(
                workspace, "owner-a", lease.generation, handle,
                original_attempt, 1001) is True
            gateway = FakeGateway(config_version=1)
            service = _service(tmp_path, gateway)
            service._record_attempt(
                workspace, original_attempt, handle,
                worker_created=False, gateway_created=True)
            fail = {"value": True}

            class Adapter:
                async def commit_attempt(self, *args, **kwargs):
                    if fail["value"]:
                        raise RuntimeUnavailable(
                            "gateway_commit_timeout", 503, retry_after=5)
                    return await service.commit_attempt(*args, **kwargs)

                async def abort(self, *args, **kwargs):
                    return await service.abort(*args, **kwargs)

                async def restore_gateway(
                        self, workspace_id, worker_container_id,
                        config_version, *, attempt_id=""):
                    restored = dataclasses.replace(
                        handle, attempt_id=attempt_id,
                        gateway_state="staged", gateway_created=False)
                    service._record_attempt(
                        workspace_id, attempt_id, restored,
                        worker_created=False, gateway_created=False)
                    return restored

                async def activate(self, workspace_id, worker_container_id,
                                   gateway_instance_id, boot_generation,
                                   config_version):
                    return {"gateway": {
                        "managed": True,
                        "workspace_id": workspace_id,
                        "config_version": config_version,
                        "worker_container_id": worker_container_id,
                        "gateway_instance_id": gateway_instance_id,
                        "boot_generation": boot_generation,
                        "available": True,
                    }}

            failing = RuntimeManager(
                store, Adapter(), owner_id="owner-b", gateway_required=True,
                clock=clock)
            with pytest.raises(RuntimeUnavailable) as refused:
                await failing.restore_gateway(workspace, WORKER, 1)
            assert refused.value.code == "gateway_commit_timeout"

            fenced = store.get_worker_lease(workspace)
            assert fenced is not None
            assert fenced.owner_id == "owner-b"
            assert fenced.state == "starting"
            assert fenced.pending_attempt_id == original_attempt
            assert service._load_attempt(workspace)["status"] == "pending"

            fail["value"] = False
            clock.value = fenced.lease_expires_at + 1
            recovered = RuntimeManager(
                store, Adapter(), owner_id="owner-c", gateway_required=True,
                clock=clock)
            final = await recovered.restore_gateway(workspace, WORKER, 1)
            assert final is not None
            ready = store.get_worker_lease(workspace)
            assert ready is not None
            assert ready.state == "ready"
            assert ready.owner_id == "owner-c"
            assert ready.pending_attempt_id == ""
            record = service._load_attempt(workspace)
            assert record is not None
            assert record["attempt_id"] == ready.attempt_id
            assert record["status"] == "completed"

    asyncio.run(scenario())


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
