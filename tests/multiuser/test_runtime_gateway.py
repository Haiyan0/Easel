"""G2 RuntimeManager 两阶段 gateway 激活、重启对账与临时不可用。"""
from __future__ import annotations

import asyncio

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

    def handle(self, workspace_id: str, version: int) -> WorkerHandle:
        return WorkerHandle(
            workspace_id, WORKER, WORKER_ORIGIN, TOKEN, version,
            gateway_instance_id=GATEWAY,
            gateway_generation=self.boot,
            gateway_upstream=GATEWAY_UPSTREAM,
            gateway_state="available" if self.active else "staged",
            worker_created=self.worker_created,
            gateway_created=self.gateway_created)

    async def ensure(self, workspace_id: str, version: int) -> WorkerHandle:
        self.ensure_calls += 1
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

    async def inspect(self, workspace_id: str) -> dict:
        self.inspect_calls += 1
        lease = self.store.get_worker_lease(workspace_id)
        assert lease is not None
        available = bool(
            self.available and self.active
            and lease.gateway_generation == self.boot
            and lease.gateway_state == "available")
        return {
            "workspace_id": workspace_id,
            "container_id": lease.container_id,
            "instance_id": lease.container_id,
            "config_version": lease.config_version,
            "ready": available,
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
                    gateway_instance_id: str, *, worker_created: bool,
                    gateway_created: bool) -> None:
        self.abort_calls.append({
            "workspace": workspace_id,
            "worker": worker_container_id,
            "gateway": gateway_instance_id,
            "worker_created": worker_created,
            "gateway_created": gateway_created,
        })


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
            manager = RuntimeManager(
                store, fake, gateway_required=True, clock=Clock())
            await manager.ensure_ready(user.workspace_id, 1)

            fake.boot = NEW_BOOT
            fake.active = False
            fake.worker_created = True
            fake.gateway_created = True
            fake.fail_activate = RuntimeUnavailable("gateway_activation_failed")
            with pytest.raises(RuntimeUnavailable):
                await manager.ensure_ready(user.workspace_id, 1)
            assert fake.abort_calls == [{
                "workspace": user.workspace_id,
                "worker": WORKER,
                "gateway": GATEWAY,
                "worker_created": True,
                "gateway_created": True,
            }]
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
            assert await manager.bound_handle(
                user.workspace_id, WORKER, 1) is None
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
