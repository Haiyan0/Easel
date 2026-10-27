"""G2 RuntimeManager 两阶段 gateway 激活、重启对账与临时不可用。"""
from __future__ import annotations

import asyncio
import dataclasses

import pytest

from easel.control.runtime import RuntimeManager, RuntimeUnavailable
from easel.control.store import Store
from easel.control.types import WorkerHandle

WORKER = "b" * 64
GATEWAY = "c" * 64
BOOT = "d" * 32
NEW_BOOT = "e" * 32
WORKER_ORIGIN = "http://172.32.0.2:7860"
GATEWAY_UPSTREAM = "http://172.31.0.5:8897"
TOKEN = "worker-service-token"


class Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class GatewayFake:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.boot = BOOT
        self.active = False
        self.available = True
        self.fail_ensure: Exception | None = None
        self.fail_activate: Exception | None = None
        self.ensure_calls = 0
        self.activate_calls: list[dict] = []
        self.inspect_calls = 0
        self.stop_calls: list[tuple[str, str, str]] = []
        self.abort_calls: list[dict] = []
        self.worker_created = False
        self.gateway_created = False
        self.worker_ready = True
        self.attempt_id = ""
        self.fail_abort: Exception | None = None
        self.restore_boot = NEW_BOOT
        self.restore_calls = 0
        self.fail_restore: Exception | None = None

    def handle(self, workspace_id: str, version: int) -> WorkerHandle:
        return WorkerHandle(
            workspace_id, WORKER, WORKER_ORIGIN, TOKEN, version,
            gateway_instance_id=GATEWAY,
            gateway_generation=self.boot,
            gateway_upstream=GATEWAY_UPSTREAM,
            gateway_state="available" if self.active else "staged",
            attempt_id=self.attempt_id,
            worker_created=self.worker_created,
            gateway_created=self.gateway_created)

    async def ensure(self, workspace_id: str, version: int, *,
                     attempt_id: str = "") -> WorkerHandle:
        self.ensure_calls += 1
        self.attempt_id = attempt_id
        if self.fail_ensure is not None:
            raise self.fail_ensure
        return self.handle(workspace_id, version)

    async def activate(self, workspace_id: str, worker_container_id: str,
                       gateway_instance_id: str, boot_generation: str,
                       config_version: int) -> dict:
        lease = self.store.get_worker_lease(workspace_id)
        self.activate_calls.append({
            "state": lease.state if lease else "",
            "gateway_state": lease.gateway_state if lease else "",
            "worker": worker_container_id,
            "gateway": gateway_instance_id,
            "boot": boot_generation,
        })
        if self.fail_activate is not None:
            raise self.fail_activate
        self.active = True
        return {"gateway": {
            "managed": True,
            "workspace_id": workspace_id,
            "config_version": config_version,
            "worker_container_id": worker_container_id,
            "gateway_instance_id": gateway_instance_id,
            "boot_generation": boot_generation,
            "available": True,
        }}

    async def restore_gateway(self, workspace_id: str,
                              worker_container_id: str,
                              config_version: int, *,
                              attempt_id: str = "") -> WorkerHandle:
        self.restore_calls += 1
        if self.fail_restore is not None:
            raise self.fail_restore
        if worker_container_id != WORKER:
            raise RuntimeUnavailable("worker_identity_mismatch", 503)
        self.active = False
        self.boot = self.restore_boot
        self.attempt_id = attempt_id
        return self.handle(workspace_id, config_version)

    async def inspect(self, workspace_id: str) -> dict:
        self.inspect_calls += 1
        lease = self.store.get_worker_lease(workspace_id)
        assert lease is not None
        # 物理事实独立于 Store；不得用 DB gateway_state 构造 available。
        available = bool(self.available and self.active)
        return {
            "workspace_id": workspace_id,
            "container_id": lease.container_id,
            "instance_id": lease.container_id,
            "config_version": lease.config_version,
            "ready": available and self.worker_ready,
            "worker_ready": self.worker_ready,
            "managed": True,
            "upstream": lease.upstream,
            "labels": {
                "easel.managed": "true",
                "easel.workspace-id": workspace_id,
                "easel.config-version": str(lease.config_version),
            },
            "gateway": {
                "managed": True,
                "workspace_id": workspace_id,
                "config_version": lease.config_version,
                "worker_container_id": lease.container_id,
                "gateway_instance_id": GATEWAY,
                "boot_generation": self.boot,
                "available": available,
                "active": self.active,
            },
            "gateway_generation": self.boot,
            "gateway_upstream": GATEWAY_UPSTREAM,
            "gateway_available": available,
        }

    async def stop(self, workspace_id: str, instance_id: str,
                   gateway_instance_id: str = "") -> None:
        self.stop_calls.append(
            (workspace_id, instance_id, gateway_instance_id))

    async def abort(self, workspace_id: str, worker_container_id: str,
                    gateway_instance_id: str, *, attempt_id: str, scope: str,
                    worker_created: bool, gateway_created: bool) -> None:
        self.abort_calls.append({
            "workspace": workspace_id,
            "worker": worker_container_id,
            "gateway": gateway_instance_id,
            "attempt_id": attempt_id,
            "scope": scope,
            "worker_created": worker_created,
            "gateway_created": gateway_created,
        })
        if self.fail_abort is not None:
            raise self.fail_abort


def _run(coro):
    return asyncio.run(coro)


def _running(store: Store, user) -> object:
    conversation = store.create_conversation(user)
    record = store.reserve_run(
        user, conversation.id, "request-1", "digest-1", 1)
    store.mark_running(user, record.id)
    return record


def test_two_phase_start_stages_before_activate_and_persists_ready(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            fake = GatewayFake(store)
            manager = RuntimeManager(
                store, fake, gateway_required=True, clock=Clock())
            handle = await manager.ensure_ready(user.workspace_id, 1)
            assert handle.gateway_instance_id == GATEWAY
            assert handle.gateway_generation == BOOT
            assert handle.gateway_upstream == GATEWAY_UPSTREAM
            assert handle.gateway_state == "available"
            assert fake.ensure_calls == 1
            assert fake.activate_calls == [{
                "state": "starting",
                "gateway_state": "staged",
                "worker": WORKER,
                "gateway": GATEWAY,
                "boot": BOOT,
            }]
            lease = store.get_worker_lease(user.workspace_id)
            assert lease.state == "ready"
            assert lease.gateway_instance_id == GATEWAY
            assert lease.gateway_generation == BOOT
            assert lease.gateway_state == "available"
            # 第二次 ready 路径只读核对，不重复启动/激活
            again = await manager.ensure_ready(user.workspace_id, 1)
            assert again.gateway_generation == BOOT
            assert fake.ensure_calls == 1
            assert len(fake.activate_calls) == 1
    _run(scenario())


def test_concurrent_managers_share_one_staged_activation(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            fake = GatewayFake(store)
            first = RuntimeManager(
                store, fake, gateway_required=True, clock=Clock(),
                poll_interval=0.01)
            second = RuntimeManager(
                store, fake, gateway_required=True, clock=Clock(),
                poll_interval=0.01)
            handles = await asyncio.gather(
                first.ensure_ready(user.workspace_id, 1),
                second.ensure_ready(user.workspace_id, 1))
            assert fake.ensure_calls == 1
            assert len(fake.activate_calls) == 1
            assert handles[0].gateway_instance_id == \
                handles[1].gateway_instance_id
            assert store.get_worker_lease(
                user.workspace_id).gateway_state == "available"
    _run(scenario())


def test_gateway_restart_new_boot_reuses_worker_and_preserves_run(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            run = _running(store, user)
            fake = GatewayFake(store)
            manager = RuntimeManager(
                store, fake, gateway_required=True, clock=Clock())
            first = await manager.ensure_ready(user.workspace_id, 1)
            assert first.gateway_generation == BOOT

            fake.boot = NEW_BOOT
            fake.active = False
            fake.available = True
            refreshed = await manager.ensure_ready(user.workspace_id, 1)
            assert refreshed.container_id == WORKER
            assert refreshed.gateway_instance_id == GATEWAY
            assert refreshed.gateway_generation == NEW_BOOT
            assert fake.ensure_calls == 2
            assert len(fake.activate_calls) == 2
            assert fake.activate_calls[-1]["state"] == "starting"
            lease = store.get_worker_lease(user.workspace_id)
            assert lease.container_id == WORKER
            assert lease.gateway_generation == NEW_BOOT
            assert lease.gateway_state == "available"
            assert store.get_run(user, run.id).status == "running"
    _run(scenario())


def test_gateway_activation_failure_marks_unavailable_without_interrupt(
        tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            run = _running(store, user)
            fake = GatewayFake(store)
            manager = RuntimeManager(
                store, fake, gateway_required=True, clock=Clock())
            await manager.ensure_ready(user.workspace_id, 1)

            fake.boot = NEW_BOOT
            fake.active = False
            fake.fail_activate = RuntimeUnavailable(
                "gateway_activation_failed", 503)
            with pytest.raises(RuntimeUnavailable) as rejected:
                await manager.ensure_ready(user.workspace_id, 1)
            assert rejected.value.code == "gateway_activation_failed"
            lease = store.get_worker_lease(user.workspace_id)
            assert lease.state == "ready"
            assert lease.container_id == WORKER
            assert lease.gateway_state == "unavailable"
            assert store.get_run(user, run.id).status == "running"
    _run(scenario())


def test_activation_failure_cleans_only_created_candidate(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            run = _running(store, user)
            fake = GatewayFake(store)
            fake.worker_created = True
            fake.gateway_created = True
            fake.fail_activate = RuntimeUnavailable("gateway_activation_failed")
            manager = RuntimeManager(
                store, fake, gateway_required=True, clock=Clock())
            with pytest.raises(RuntimeUnavailable):
                await manager.ensure_ready(user.workspace_id, 1)
            assert len(fake.abort_calls) == 1
            abort = fake.abort_calls[0]
            assert abort["workspace"] == user.workspace_id
            assert abort["worker"] == WORKER
            assert abort["gateway"] == GATEWAY
            assert abort["attempt_id"] == fake.attempt_id
            assert abort["scope"] == "worker_gateway"
            assert abort["worker_created"] is True
            assert abort["gateway_created"] is True
            assert store.get_worker_lease(
                user.workspace_id).state == "stopped"
            assert store.get_run(user, run.id).status == "running"
    _run(scenario())


def test_gateway_inspect_failure_does_not_interrupt_active_run(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            run = _running(store, user)
            fake = GatewayFake(store)
            manager = RuntimeManager(
                store, fake, gateway_required=True, clock=Clock())
            await manager.ensure_ready(user.workspace_id, 1)

            async def fail(_workspace):
                raise RuntimeUnavailable("gateway_inspect_failed", 503)
            fake.inspect = fail  # type: ignore[method-assign]
            with pytest.raises(RuntimeUnavailable) as rejected:
                await manager.ensure_ready(user.workspace_id, 1)
            assert rejected.value.code == "gateway_inspect_failed"
            assert store.get_run(user, run.id).status == "running"
            assert store.get_worker_lease(
                user.workspace_id).container_id == WORKER
    _run(scenario())


def test_drain_stops_exact_worker_gateway_tuple(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            fake = GatewayFake(store)
            manager = RuntimeManager(
                store, fake, gateway_required=True, clock=Clock())
            await manager.ensure_ready(user.workspace_id, 1)
            await manager.drain(user.workspace_id)
            assert fake.stop_calls == [
                (user.workspace_id, WORKER, GATEWAY)]
            assert store.get_worker_lease(
                user.workspace_id).state == "stopped"
    _run(scenario())


def test_bound_handle_requires_available_gateway_fields(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            fake = GatewayFake(store)
            manager = RuntimeManager(
                store, fake, gateway_required=True, clock=Clock())
            await manager.ensure_ready(user.workspace_id, 1)
            handle = await manager.bound_handle(
                user.workspace_id, WORKER, 1)
            assert handle is not None
            assert handle.gateway_instance_id == GATEWAY
            store.mark_worker_gateway_unavailable(
                user.workspace_id, 1, 1001)
            with pytest.raises(RuntimeUnavailable) as unavailable:
                await manager.bound_handle(user.workspace_id, WORKER, 1)
            assert unavailable.value.code == "gateway_unavailable"
            assert unavailable.value.status == 503
    _run(scenario())


def test_worker_unready_is_rejected_without_failing_or_deleting(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            run = _running(store, user)
            fake = GatewayFake(store)
            manager = RuntimeManager(
                store, fake, gateway_required=True, clock=Clock())
            await manager.ensure_ready(user.workspace_id, 1)
            fake.worker_ready = False
            with pytest.raises(RuntimeUnavailable) as rejected:
                await manager.ensure_ready(user.workspace_id, 1)
            assert rejected.value.code == "worker_not_ready"
            lease = store.get_worker_lease(user.workspace_id)
            assert lease.state == "ready"
            assert lease.container_id == WORKER
            assert fake.stop_calls == []
            assert store.get_run(user, run.id).status == "running"
    _run(scenario())


def test_db_unavailable_physical_recovery_same_boot_marks_available(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            fake = GatewayFake(store)
            manager = RuntimeManager(
                store, fake, gateway_required=True, clock=Clock())
            handle = await manager.ensure_ready(user.workspace_id, 1)
            store.mark_worker_gateway_unavailable(
                user.workspace_id, 1, 1001)
            assert store.get_worker_lease(
                user.workspace_id).gateway_state == "unavailable"
            recovered = await manager.ensure_ready(user.workspace_id, 1)
            assert recovered.gateway_instance_id == handle.gateway_instance_id
            assert recovered.gateway_generation == handle.gateway_generation
            assert store.get_worker_lease(
                user.workspace_id).gateway_state == "available"
            assert fake.ensure_calls == 1
    _run(scenario())


def test_expired_refresh_recovery_does_not_interrupt_running_work(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as first, Store.open(
                tmp_path / "db") as second:
            user = first.create_user("A")
            run = _running(first, user)
            clock = Clock()
            fake = GatewayFake(first)
            manager_a = RuntimeManager(
                first, fake, gateway_required=True, clock=clock)
            await manager_a.ensure_ready(user.workspace_id, 1)
            lease = first.get_worker_lease(user.workspace_id)
            assert lease is not None
            candidate = dataclasses.replace(
                fake.handle(user.workspace_id, 1), gateway_generation=NEW_BOOT,
                gateway_state="staged")
            assert first.begin_worker_gateway_refresh(
                user.workspace_id, lease.generation, "dead-owner",
                candidate, "a" * 32, 1001, 2) is not None
            clock.value = 1200
            fake.boot = NEW_BOOT
            fake.active = False
            class Broker:
                def __init__(self):
                    self.revoked = []

                async def revoke_run(self, run_id, workspace_id):
                    self.revoked.append((run_id, workspace_id))

            broker = Broker()
            fake.fail_ensure = RuntimeUnavailable("gateway_unavailable")
            manager_b = RuntimeManager(
                second, fake, gateway_required=True, clock=clock,
                broker=broker, poll_interval=0.01)
            with pytest.raises(RuntimeUnavailable) as rejected:
                await manager_b.ensure_ready(user.workspace_id, 1)
            assert rejected.value.code == "gateway_unavailable"
            after = second.get_worker_lease(user.workspace_id)
            assert after.container_id == WORKER
            assert after.state == "ready"
            assert after.gateway_state == "unavailable"
            assert fake.stop_calls == []
            assert broker.revoked == []
            assert first.get_run(user, run.id).status == "running"
            release_count = first._connection.execute(
                "SELECT release_count FROM runs WHERE id = ?",
                (run.id,)).fetchone()[0]
            assert release_count == 0

            fake.fail_ensure = None
            recovered = await manager_b.ensure_ready(user.workspace_id, 1)
            assert recovered.container_id == WORKER
            assert recovered.gateway_generation == NEW_BOOT
            assert first.get_run(user, run.id).status == "running"
    _run(scenario())


def test_expired_staged_refresh_recovers_same_worker_new_boot(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as first, Store.open(
                tmp_path / "db") as second:
            user = first.create_user("A")
            run = _running(first, user)
            clock = Clock()
            fake = GatewayFake(first)
            manager_a = RuntimeManager(
                first, fake, gateway_required=True, clock=clock)
            await manager_a.ensure_ready(user.workspace_id, 1)
            lease = first.get_worker_lease(user.workspace_id)
            assert lease is not None
            candidate = dataclasses.replace(
                fake.handle(user.workspace_id, 1), gateway_generation=NEW_BOOT,
                gateway_state="staged")
            assert first.begin_worker_gateway_refresh(
                user.workspace_id, lease.generation, "dead-owner",
                candidate, "a" * 32, 1001, 2) is not None
            clock.value = 1200
            fake.boot = NEW_BOOT
            fake.active = False
            manager_b = RuntimeManager(
                second, fake, gateway_required=True, clock=clock,
                poll_interval=0.01)
            recovered = await manager_b.ensure_ready(user.workspace_id, 1)
            assert recovered.container_id == WORKER
            assert recovered.gateway_generation == NEW_BOOT
            assert fake.stop_calls == []
            assert first.get_run(user, run.id).status == "running"
            assert second.get_worker_lease(
                user.workspace_id).state == "ready"
    _run(scenario())


def test_gateway_only_abort_recovery_keeps_existing_worker(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as first, Store.open(
                tmp_path / "db") as second:
            user = first.create_user("A")
            run = _running(first, user)
            clock = Clock(1000)
            fake = GatewayFake(first)
            manager = RuntimeManager(
                first, fake, gateway_required=True, clock=clock)
            await manager.ensure_ready(user.workspace_id, 1)
            lease = first.get_worker_lease(user.workspace_id)
            assert lease is not None
            candidate = dataclasses.replace(
                fake.handle(user.workspace_id, 1),
                gateway_instance_id="f" * 64,
                gateway_generation=NEW_BOOT,
                gateway_state="staged",
                worker_created=False,
                gateway_created=True,
                attempt_id="a" * 32)
            staged = first.begin_worker_gateway_refresh(
                user.workspace_id, lease.generation, manager.owner_id,
                candidate, "a" * 32, 1001, 60)
            assert staged is not None
            fake.fail_abort = RuntimeUnavailable("gateway_cleanup_failed")
            with pytest.raises(RuntimeUnavailable):
                await manager._cleanup_candidate(
                    user.workspace_id, candidate, staged, "a" * 32)
            stopped = first.get_worker_lease(user.workspace_id)
            assert stopped.state == "stopping"
            assert stopped.cleanup_purpose == "start_abort"
            assert stopped.cleanup_scope == "gateway"
            assert stopped.cleanup_worker_created is False
            assert stopped.cleanup_gateway_created is True
            assert stopped.container_id == WORKER
            assert first.get_run(user, run.id).status == "running"

            clock.value = 1200
            fake.fail_abort = None
            recovered_manager = RuntimeManager(
                second, fake, gateway_required=True, clock=clock,
                poll_interval=0.01)
            outcome, recovery = second.claim_worker_start(
                user.workspace_id, 1, "owner-b", 1200, 60)
            assert outcome == "recover_stop"
            assert recovery is not None
            await recovered_manager._recover_stop(
                user.workspace_id, recovery, 1300)
            after = second.get_worker_lease(user.workspace_id)
            assert after.state == "ready"
            assert after.gateway_state == "unavailable"
            assert after.container_id == WORKER
            assert after.service_token == "worker-service-token"
            assert after.cleanup_scope == ""
            assert fake.stop_calls == []
            assert [call["scope"] for call in fake.abort_calls[-2:]] == [
                "gateway", "gateway"]
            assert first.get_run(user, run.id).status == "running"
            release_count = first._connection.execute(
                "SELECT release_count FROM runs WHERE id = ?",
                (run.id,)).fetchone()[0]
            assert release_count == 0
    _run(scenario())


def test_late_abort_loses_cas_after_new_owner_adopts(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as first, Store.open(
                tmp_path / "db") as second:
            user = first.create_user("A")
            fake = GatewayFake(first)
            manager = RuntimeManager(
                first, fake, gateway_required=True, clock=Clock())
            old = first.claim_worker_start(
                user.workspace_id, 1, "owner-a", 1000, 60)[1]
            assert old is not None
            candidate = dataclasses.replace(
                fake.handle(user.workspace_id, 1),
                worker_created=True, gateway_created=True,
                attempt_id="a" * 32)
            # A 的旧启动尝试在 B 接管前仍留在 DB；B 在过期后采用同一
            # 物理身份并写 ready，A 的迟到 abort 必须 CAS 失败。
            assert first.stage_worker_gateway_start(
                user.workspace_id, old.owner_id, old.generation,
                candidate, "a" * 32, 1001) is True
            claimed = second.claim_worker_start(
                user.workspace_id, 1, "owner-b", 2000, 60)
            assert claimed[0] == "refresh"
            b_lease = claimed[1]
            assert b_lease is not None
            b_staged = second.begin_worker_gateway_refresh(
                user.workspace_id, b_lease.generation, "owner-b",
                candidate, "b" * 32, 2001, 60)
            assert b_staged is not None
            assert second.complete_worker_gateway_refresh(
                user.workspace_id, "owner-b", b_staged.generation,
                dataclasses.replace(
                    candidate, workspace_id=user.workspace_id,
                    gateway_state="available"), 2002, 60) is True
            cleaned = await manager._cleanup_candidate(
                user.workspace_id, candidate, old, "a" * 32)
            assert cleaned is False
            assert fake.abort_calls == []
            current = second.get_worker_lease(user.workspace_id)
            assert current.state == "ready"
            assert current.owner_id == "owner-b"
    _run(scenario())


def test_abort_claim_fences_new_owner_until_stop_terminal(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as first, Store.open(
                tmp_path / "db") as second:
            user = first.create_user("A")
            lease = first.claim_worker_start(
                user.workspace_id, 1, "owner-a", 1000, 60)[1]
            assert lease is not None
            handle = dataclasses.replace(
                GatewayFake(first).handle(user.workspace_id, 1),
                worker_created=True, gateway_created=True)
            assert first.stage_worker_gateway_start(
                user.workspace_id, "owner-a", lease.generation,
                handle, "a" * 32, 1001) is True
            assert first.begin_worker_abort(
                user.workspace_id, "owner-a", lease.generation,
                "a" * 32, "worker_gateway", True, True, 1002) is True
            outcome, current = second.claim_worker_start(
                user.workspace_id, 1, "owner-b", 1003, 60)
            assert outcome == "draining"
            assert current is not None and current.state == "stopping"
            assert second.stop_worker(
                user.workspace_id, 1004, generation=lease.generation,
                expected_state="stopping") is True
            outcome, current = second.claim_worker_start(
                user.workspace_id, 1, "owner-b", 1005, 60)
            assert outcome == "started"
            assert current is not None
    _run(scenario())


def test_create_app_binds_gateway_required_runtime(tmp_path):
    from fastapi.testclient import TestClient

    from easel.control.app import create_app
    from easel.control.auth import AuthService

    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        app = create_app(
            store=store, auth=AuthService(store),
            provisioner=GatewayFake(store), gateway_required=True)
        with TestClient(app, base_url="https://easel.test") as client:
            assert client.get("/api/health").status_code == 200
            assert app.state.runtime._gateway_required is True
    finally:
        store.close()


def test_restore_gateway_keeps_same_worker_and_new_boot(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            fake = GatewayFake(store)
            manager = RuntimeManager(
                store, fake, gateway_required=True, clock=Clock())
            first = await manager.ensure_ready(user.workspace_id, 1)
            store.mark_worker_gateway_unavailable(
                user.workspace_id, 1, 1001)
            restored = await manager.restore_gateway(
                user.workspace_id, first.container_id, 1)
            assert restored is not None
            assert restored.container_id == first.container_id
            assert restored.gateway_instance_id == first.gateway_instance_id
            assert restored.gateway_generation == NEW_BOOT
            assert restored.gateway_state == "available"
            assert fake.restore_calls == 1
            assert fake.ensure_calls == 1
            assert fake.stop_calls == []
            lease = store.get_worker_lease(user.workspace_id)
            assert lease.state == "ready"
            assert lease.gateway_state == "available"
            assert lease.gateway_generation == NEW_BOOT
    _run(scenario())


def test_restore_gateway_refuses_other_worker_and_never_creates(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            fake = GatewayFake(store)
            manager = RuntimeManager(
                store, fake, gateway_required=True, clock=Clock())
            await manager.ensure_ready(user.workspace_id, 1)
            assert await manager.restore_gateway(
                user.workspace_id, "f" * 64, 1) is None
            assert fake.restore_calls == 0
            assert fake.ensure_calls == 1
            assert fake.stop_calls == []
    _run(scenario())


def test_restore_gateway_propagates_worker_identity_failure(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            fake = GatewayFake(store)
            manager = RuntimeManager(
                store, fake, gateway_required=True, clock=Clock())
            await manager.ensure_ready(user.workspace_id, 1)
            fake.fail_restore = RuntimeUnavailable(
                "worker_identity_mismatch", 503)
            with pytest.raises(RuntimeUnavailable) as rejected:
                await manager.restore_gateway(user.workspace_id, WORKER, 1)
            assert rejected.value.code == "worker_identity_mismatch"
            assert fake.ensure_calls == 1
            assert fake.stop_calls == []
            assert store.get_worker_lease(
                user.workspace_id).container_id == WORKER
    _run(scenario())


def test_bound_handle_refresh_in_progress_is_explicit_503(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            fake = GatewayFake(store)
            manager = RuntimeManager(
                store, fake, gateway_required=True, clock=Clock())
            handle = await manager.ensure_ready(user.workspace_id, 1)
            lease = store.get_worker_lease(user.workspace_id)
            staged = store.begin_worker_gateway_refresh(
                user.workspace_id, lease.generation, manager.owner_id,
                handle, "a" * 32, 1001, 120)
            assert staged is not None
            with pytest.raises(RuntimeUnavailable) as rejected:
                await manager.bound_handle(user.workspace_id, WORKER, 1)
            assert rejected.value.code == "worker_gateway_refresh_in_progress"
            assert rejected.value.status == 503
            with pytest.raises(RuntimeUnavailable) as restore:
                await manager.restore_gateway(user.workspace_id, WORKER, 1)
            assert restore.value.code == "worker_gateway_refresh_in_progress"
            assert fake.stop_calls == []
    _run(scenario())


def test_bound_handle_gateway_only_abort_is_not_worker_gone(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            fake = GatewayFake(store)
            manager = RuntimeManager(
                store, fake, gateway_required=True, clock=Clock())
            handle = await manager.ensure_ready(user.workspace_id, 1)
            lease = store.get_worker_lease(user.workspace_id)
            attempt_id = "b" * 32
            staged = store.begin_worker_gateway_refresh(
                user.workspace_id, lease.generation, manager.owner_id,
                handle, attempt_id, 1001, 120)
            assert staged is not None
            assert store.begin_worker_abort(
                user.workspace_id, manager.owner_id, staged.generation,
                attempt_id, "gateway", False, True, 1001) is True
            with pytest.raises(RuntimeUnavailable) as rejected:
                await manager.bound_handle(user.workspace_id, WORKER, 1)
            assert rejected.value.code == "gateway_cleanup_in_progress"
            assert fake.stop_calls == []
            assert handle.container_id == WORKER
    _run(scenario())
