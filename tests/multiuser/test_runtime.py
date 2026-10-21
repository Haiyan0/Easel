"""Task 8：worker 生命周期租约、就绪校验、drain 与空闲回收。"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from easel.control.runtime import RuntimeManager, RuntimeUnavailable
from easel.control.store import MIGRATIONS, Store
from easel.control.types import WorkerHandle


class Clock:
    """可推进的测试时钟；避免为每个场景等待真实秒数。"""

    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class FakeProvisioner:
    """明确、可观察的 fake；不模拟 Docker，只记录控制面调用。"""

    def __init__(self, *, delay: float = 0.0, fail: Exception | None = None,
                 block: asyncio.Event | None = None):
        self.delay = delay
        self.fail = fail
        self.block = block
        self.starts = 0
        self.stops = 0
        self.inspects = 0
        self.ensure_calls: list[tuple[str, int]] = []
        self.stop_calls: list[str] = []
        self.handles: dict[str, WorkerHandle] = {}
        self.entered = asyncio.Event()
        self.force_workspace: str | None = None
        self.force_config_version: int | None = None
        self.inspect_workspace: str | None = None
        self.inspect_config_version: int | None = None
        self.inspect_container_id: str | None = None
        self.inspect_ready = True
        self.inspect_labels_workspace: str | None = None
        self.inspect_error: Exception | None = None

    async def ensure(self, workspace_id: str, config_version: int) -> WorkerHandle:
        self.starts += 1
        self.ensure_calls.append((workspace_id, config_version))
        self.entered.set()
        if self.block is not None:
            await self.block.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        handle = WorkerHandle(
            self.force_workspace or workspace_id,
            f"container-{workspace_id[:8]}",
            f"http://10.0.0.{len(self.handles) + 2}:7860",
            f"service-token-{workspace_id[:8]}",
            (self.force_config_version
             if self.force_config_version is not None else config_version))
        self.handles[workspace_id] = handle
        return handle

    async def stop(self, workspace_id: str) -> None:
        self.stops += 1
        self.stop_calls.append(workspace_id)
        self.handles.pop(workspace_id, None)

    async def inspect(self, workspace_id: str) -> dict:
        self.inspects += 1
        if self.inspect_error is not None:
            raise self.inspect_error
        handle = self.handles.get(workspace_id)
        if handle is None:
            return {"workspace_id": workspace_id, "container_id": "",
                    "config_version": 0, "ready": False, "managed": False,
                    "labels": {}}
        label_workspace = self.inspect_labels_workspace or workspace_id
        return {
            "workspace_id": self.inspect_workspace or workspace_id,
            "container_id": self.inspect_container_id or handle.container_id,
            "config_version": (self.inspect_config_version
                               if self.inspect_config_version is not None
                               else handle.config_version),
            "ready": self.inspect_ready,
            "managed": True,
            "labels": {
                "easel.managed": "true",
                "easel.workspace-id": label_workspace,
                "easel.config-version": str(
                    self.inspect_config_version
                    if self.inspect_config_version is not None
                    else handle.config_version),
            },
            "upstream": handle.upstream,
        }


def run(coro):
    return asyncio.run(coro)


def _running(store: Store, user) -> object:
    conversation = store.create_conversation(user, "测试会话")
    run_record = store.reserve_run(
        user, conversation.id, "request-1", "digest-1", 1)
    store.mark_running(user, run_record.id)
    return run_record


def test_concurrent_cold_start_is_single_flight(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            provisioner = FakeProvisioner(delay=0.05)
            manager = RuntimeManager(store, provisioner, poll_interval=0.01)
            handles = await asyncio.gather(*[
                manager.ensure_ready(user.workspace_id, 1) for _ in range(4)])
            assert provisioner.starts == 1
            assert {handle.workspace_id for handle in handles} == {
                user.workspace_id}
            assert len({handle.service_token for handle in handles}) == 1
            assert {handle.container_id for handle in handles} == {
                handles[0].container_id}
    run(scenario())


def test_two_managers_and_connections_share_one_start(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as first_store, \
                Store.open(tmp_path / "db") as second_store:
            user = first_store.create_user("A")
            provisioner = FakeProvisioner(delay=0.05)
            first = RuntimeManager(first_store, provisioner, poll_interval=0.01)
            second = RuntimeManager(second_store, provisioner,
                                    poll_interval=0.01)
            handles = await asyncio.gather(
                first.ensure_ready(user.workspace_id, 3),
                second.ensure_ready(user.workspace_id, 3))
            assert provisioner.starts == 1
            assert handles[0].container_id == handles[1].container_id
    run(scenario())


def test_a_and_b_workspaces_get_separate_containers(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user_a = store.create_user("A")
            user_b = store.create_user("B")
            provisioner = FakeProvisioner()
            manager = RuntimeManager(store, provisioner)
            handle_a = await manager.ensure_ready(user_a.workspace_id, 1)
            handle_b = await manager.ensure_ready(user_b.workspace_id, 1)
            assert provisioner.starts == 2
            assert handle_a.container_id != handle_b.container_id
            assert handle_a.service_token != handle_b.service_token
            assert handle_a.upstream != handle_b.upstream
    run(scenario())


def test_handle_with_wrong_workspace_is_rejected_and_not_cached(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            provisioner = FakeProvisioner()
            provisioner.force_workspace = "other-workspace"
            manager = RuntimeManager(store, provisioner)
            with pytest.raises(RuntimeUnavailable) as error:
                await manager.ensure_ready(user.workspace_id, 1)
            assert error.value.status == 503
            lease = store.get_worker_lease(user.workspace_id)
            assert lease is not None and lease.state == "failed"
            assert lease.service_token == ""
            assert lease.upstream == ""
    run(scenario())


def test_handle_with_wrong_config_version_is_rejected(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            provisioner = FakeProvisioner()
            provisioner.force_config_version = 99
            manager = RuntimeManager(store, provisioner)
            with pytest.raises(RuntimeUnavailable) as error:
                await manager.ensure_ready(user.workspace_id, 7)
            assert error.value.status == 503
            assert store.get_worker_lease(user.workspace_id).state == "failed"
    run(scenario())


def test_start_failure_returns_503_retry_after_and_interrupts_runs(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            active = _running(store, user)
            provisioner = FakeProvisioner(fail=RuntimeError("boom"))
            manager = RuntimeManager(store, provisioner)
            with pytest.raises(RuntimeUnavailable) as error:
                await manager.ensure_ready(user.workspace_id, 1)
            assert error.value.code == "worker_start_failed"
            assert error.value.status == 503
            assert error.value.retry_after >= 1
            assert store.get_run(user, active.id).status == "interrupted"
            assert store.get_worker_lease(user.workspace_id).state == "failed"
    run(scenario())


def test_start_timeout_returns_503_and_marks_failed(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            active = _running(store, user)
            provisioner = FakeProvisioner(delay=1)
            manager = RuntimeManager(store, provisioner, ready_timeout=0.1,
                                     poll_interval=0.01)
            with pytest.raises(RuntimeUnavailable) as error:
                await manager.ensure_ready(user.workspace_id, 1)
            assert error.value.code == "worker_start_timeout"
            assert error.value.status == 503
            assert store.get_run(user, active.id).status == "interrupted"
            assert store.get_worker_lease(user.workspace_id).state == "failed"
    run(scenario())


def test_draining_rejects_new_ensure(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            provisioner = FakeProvisioner()
            manager = RuntimeManager(store, provisioner)
            await manager.ensure_ready(user.workspace_id, 1)
            assert store.mark_worker_draining(user.workspace_id, 1000)
            with pytest.raises(RuntimeUnavailable) as error:
                await manager.ensure_ready(user.workspace_id, 1)
            assert error.value.code == "worker_draining"
            assert error.value.status == 503
    run(scenario())


def test_active_run_prevents_idle_collection_until_finished(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            clock = Clock()
            provisioner = FakeProvisioner()
            manager = RuntimeManager(store, provisioner, clock=clock,
                                     idle_seconds=900, lease_seconds=120)
            await manager.ensure_ready(user.workspace_id, 1)
            active = _running(store, user)

            clock.value = 1901
            assert await manager.heartbeat(user.workspace_id,
                                           active_requests=0)
            assert await manager.collect_idle(clock.value) == []
            assert store.get_worker_lease(user.workspace_id).state == "ready"

            store.finish_run(user, active.id, "done")
            assert await manager.collect_idle(clock.value) == [
                user.workspace_id]
            assert store.get_worker_lease(user.workspace_id).state == "stopped"
            assert provisioner.stops == 1
    run(scenario())


def test_active_request_prevents_idle_collection(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            clock = Clock()
            provisioner = FakeProvisioner()
            manager = RuntimeManager(store, provisioner, clock=clock,
                                     idle_seconds=900, lease_seconds=120)
            await manager.ensure_ready(user.workspace_id, 1)

            clock.value = 1901
            assert await manager.heartbeat(user.workspace_id,
                                           active_requests=1)
            assert await manager.collect_idle(clock.value) == []

            clock.value = 2802
            assert await manager.heartbeat(user.workspace_id,
                                           active_requests=0)
            assert await manager.collect_idle(clock.value) == [
                user.workspace_id]
    run(scenario())


def test_idle_collection_resumes_ready_if_work_starts_during_claim(
        tmp_path, monkeypatch):
    """空闲认领与发布/权限等待并发时，不得把 workspace 永久卡在 draining。"""
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            clock = Clock()
            provisioner = FakeProvisioner()
            manager = RuntimeManager(store, provisioner, clock=clock,
                                     idle_seconds=900, lease_seconds=120)
            await manager.ensure_ready(user.workspace_id, 1)
            clock.value = 1901
            assert await manager.heartbeat(user.workspace_id,
                                           active_requests=0)

            original = store.mark_worker_draining

            def mark_then_start_work(workspace_id, now, **kwargs):
                changed = original(workspace_id, now, **kwargs)
                if changed:
                    _running(store, user)
                return changed

            monkeypatch.setattr(store, "mark_worker_draining",
                                mark_then_start_work)
            assert await manager.collect_idle(clock.value) == []
            lease = store.get_worker_lease(user.workspace_id)
            assert lease.state == "ready", "活跃工作出现后必须恢复 ready"
            assert provisioner.stops == 0
    run(scenario())


def test_stale_generation_cannot_mark_newer_lease_draining(tmp_path):
    with Store.open(tmp_path / "db") as store:
        user = store.create_user("A")
        outcome, lease = store.claim_worker_start(
            user.workspace_id, 1, "owner", 1000, 60)
        assert outcome == "started" and lease is not None
        assert store.complete_worker_start(
            user.workspace_id, "owner", lease.generation,
            WorkerHandle(user.workspace_id, "container-1",
                         "http://10.0.0.2:7860", "service-token", 1),
            1000, 60)

        assert not store.mark_worker_draining(
            user.workspace_id, 1001, generation=lease.generation + 1)
        assert store.get_worker_lease(user.workspace_id).state == "ready"
        assert store.mark_worker_draining(
            user.workspace_id, 1001, generation=lease.generation)
        assert store.get_worker_lease(user.workspace_id).state == "draining"


def test_idle_collection_never_deletes_persistent_data(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            clock = Clock()
            provisioner = FakeProvisioner()
            manager = RuntimeManager(store, provisioner, clock=clock,
                                     idle_seconds=900, lease_seconds=120)
            await manager.ensure_ready(user.workspace_id, 1)
            sentinel = tmp_path / "workspaces" / user.workspace_id / "outputs"
            sentinel.mkdir(parents=True)
            (sentinel / "keep.txt").write_text("keep", encoding="utf-8")

            clock.value = 1901
            await manager.heartbeat(user.workspace_id, active_requests=0)
            assert await manager.collect_idle(clock.value) == [
                user.workspace_id]
            assert (sentinel / "keep.txt").read_text(encoding="utf-8") == "keep"
    run(scenario())


def test_restart_adopts_matching_container_without_new_start(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as first_store:
            user = first_store.create_user("A")
            clock = Clock()
            provisioner = FakeProvisioner()
            first = RuntimeManager(first_store, provisioner, clock=clock,
                                   lease_seconds=60)
            handle = await first.ensure_ready(user.workspace_id, 4)
            assert provisioner.starts == 1

            clock.value = 1100  # 租约过期，进程已重启
            with Store.open(tmp_path / "db") as second_store:
                second = RuntimeManager(second_store, provisioner, clock=clock,
                                        lease_seconds=60)
                adopted = await second.ensure_ready(user.workspace_id, 4)
                assert adopted.container_id == handle.container_id
                assert adopted.service_token == handle.service_token
                assert provisioner.starts == 1
                assert provisioner.inspects == 1
                lease = second_store.get_worker_lease(user.workspace_id)
                assert lease.state == "ready"
                assert lease.owner_id == second.owner_id
    run(scenario())


def test_stale_owner_cannot_overwrite_new_generation(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as first_store, \
                Store.open(tmp_path / "db") as second_store:
            user = first_store.create_user("A")
            clock = Clock()
            gate = asyncio.Event()
            slow = FakeProvisioner(block=gate)
            fast = FakeProvisioner()
            first = RuntimeManager(first_store, slow, clock=clock,
                                   lease_seconds=1, ready_timeout=10,
                                   poll_interval=0.01)
            second = RuntimeManager(second_store, fast, clock=clock,
                                    lease_seconds=1, poll_interval=0.01)
            task = asyncio.create_task(first.ensure_ready(user.workspace_id, 1))
            await slow.entered.wait()
            clock.value = 1002  # 第一个启动租约已过期

            new_handle = await second.ensure_ready(user.workspace_id, 1)
            gate.set()
            with pytest.raises(RuntimeUnavailable) as error:
                await task
            assert error.value.code == "worker_start_superseded"
            lease = second_store.get_worker_lease(user.workspace_id)
            assert lease.container_id == new_handle.container_id
            assert lease.owner_id == second.owner_id
    run(scenario())


def test_false_ready_labels_are_rejected_and_not_cached(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as first_store:
            user = first_store.create_user("A")
            clock = Clock()
            provisioner = FakeProvisioner()
            first = RuntimeManager(first_store, provisioner, clock=clock,
                                   lease_seconds=60)
            await first.ensure_ready(user.workspace_id, 1)
            clock.value = 1100
            provisioner.inspect_labels_workspace = "another-workspace"

            with Store.open(tmp_path / "db") as second_store:
                second = RuntimeManager(second_store, provisioner, clock=clock,
                                        lease_seconds=60)
                with pytest.raises(RuntimeUnavailable) as error:
                    await second.ensure_ready(user.workspace_id, 1)
                assert error.value.code == "worker_identity_mismatch"
                lease = second_store.get_worker_lease(user.workspace_id)
                assert lease.state == "failed"
                assert lease.upstream == ""
                assert lease.service_token == ""
    run(scenario())


def test_config_upgrade_does_not_overwrite_active_run(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            provisioner = FakeProvisioner()
            manager = RuntimeManager(store, provisioner)
            await manager.ensure_ready(user.workspace_id, 1)
            active = _running(store, user)

            with pytest.raises(RuntimeUnavailable) as error:
                await manager.ensure_ready(user.workspace_id, 2)
            assert error.value.code == "worker_config_busy"
            lease = store.get_worker_lease(user.workspace_id)
            assert lease.state == "ready"
            assert lease.config_version == 1
            assert provisioner.stops == 0

            store.finish_run(user, active.id, "done")
            handle = await manager.ensure_ready(user.workspace_id, 2)
            assert handle.config_version == 2
            assert provisioner.stop_calls == [user.workspace_id]
            assert provisioner.ensure_calls[-1] == (user.workspace_id, 2)
            assert store.get_worker_lease(user.workspace_id).config_version == 2
    run(scenario())


def test_drain_waits_for_active_run_before_stopping(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            provisioner = FakeProvisioner()
            manager = RuntimeManager(store, provisioner, poll_interval=0.01,
                                     drain_timeout=2)
            await manager.ensure_ready(user.workspace_id, 1)
            active = _running(store, user)

            task = asyncio.create_task(manager.drain(user.workspace_id))
            await asyncio.sleep(0.05)
            assert provisioner.stops == 0
            assert store.get_worker_lease(user.workspace_id).state == "draining"

            store.finish_run(user, active.id, "done")
            await task
            assert provisioner.stops == 1
            assert store.get_worker_lease(user.workspace_id).state == "stopped"
    run(scenario())


def test_drain_timeout_leaves_draining_without_stopping(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            provisioner = FakeProvisioner()
            manager = RuntimeManager(store, provisioner, poll_interval=0.01,
                                     drain_timeout=0.05)
            await manager.ensure_ready(user.workspace_id, 1)
            _running(store, user)

            with pytest.raises(RuntimeUnavailable) as error:
                await manager.drain(user.workspace_id)
            assert error.value.code == "worker_drain_timeout"
            assert provisioner.stops == 0
            assert store.get_worker_lease(user.workspace_id).state == "draining"
    run(scenario())


def test_ensure_rejects_unknown_workspace_and_invalid_version(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            provisioner = FakeProvisioner()
            manager = RuntimeManager(store, provisioner)
            with pytest.raises(Exception):
                await manager.ensure_ready("no-such-workspace", 1)
            user = store.create_user("A")
            with pytest.raises(ValueError):
                await manager.ensure_ready(user.workspace_id, -1)
            with pytest.raises(ValueError):
                await manager.ensure_ready(user.workspace_id, True)
            assert provisioner.starts == 0
    run(scenario())


def test_worker_handle_still_hides_service_token_in_repr():
    handle = WorkerHandle("ws", "container", "http://10.0.0.2:7860",
                          "secret-service-token", 1)
    assert "secret-service-token" not in repr(handle)
    assert "secret-service-token" not in str(handle)


def test_control_app_binds_runtime_to_its_lifespan_store(tmp_path):
    """实际入口：runtime 必须绑定应用自己的数据库连接，而不是启动前的连接。"""
    from fastapi.testclient import TestClient

    from easel.control.app import create_app
    from easel.control.auth import AuthService

    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        app = create_app(store=store, auth=AuthService(store),
                         provisioner=FakeProvisioner())
        with TestClient(app, base_url="https://easel.test") as client:
            assert client.get("/api/health").status_code == 200
            runtime = app.state.runtime
            assert isinstance(runtime, RuntimeManager)
            assert runtime._store is app.state.control_store
            assert runtime._store is not store
    finally:
        store.close()


def test_unix_client_decodes_only_strict_protocol_responses(tmp_path):
    from easel.control.runtime import UnixProvisionerClient

    client = UnixProvisionerClient(tmp_path / "provisioner.sock")
    with pytest.raises(RuntimeUnavailable) as denied:
        client._decode_ensure({"ok": False, "error": "container_conflict"})
    assert denied.value.code == "container_conflict"
    with pytest.raises(RuntimeUnavailable):
        client._decode_ensure({"ok": True, "handle": {"workspace_id": "ws"}})
    with pytest.raises(RuntimeUnavailable):
        client._decode_ensure({"ok": True, "handle": {
            "workspace_id": "a" * 32, "container_id": "c",
            "upstream": "http://10.0.0.2:7860",
            "service_token": "t", "config_version": 1, "extra": "x"}})
    handle = client._decode_ensure({"ok": True, "handle": {
        "workspace_id": "a" * 32, "container_id": "c",
        "upstream": "http://10.0.0.2:7860",
        "service_token": "t", "config_version": 1}})
    assert handle.workspace_id == "a" * 32
    assert handle.service_token == "t"


def test_v4_database_upgrade_adds_lifecycle_columns_and_keeps_lease(tmp_path):
    """旧库按追加迁移升级：已有租约保留，新列按空所有者/0 代次回填。"""
    path = tmp_path / "v4-control.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        for statements in MIGRATIONS[:4]:
            for statement in statements:
                connection.execute(statement)
        connection.execute("PRAGMA user_version = 4")
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (4, 900)")
        connection.execute(
            "INSERT INTO users (id, display_name, role, created_at)"
            " VALUES ('user-old', '旧成员', 'member', 900)")
        connection.execute(
            "INSERT INTO workspaces (id, owner_user_id, state, config_version,"
            " created_at) VALUES ('ws-old', 'user-old', 'ready', 3, 900)")
        connection.execute(
            "INSERT INTO worker_leases (workspace_id, container_id,"
            " config_version, state, heartbeat_at, started_at)"
            " VALUES ('ws-old', 'old-container', 3, 'stopped', 900, 900)")
        connection.commit()
    finally:
        connection.close()

    with Store.open(path) as store:
        lease = store.get_worker_lease("ws-old")
        assert lease is not None
        assert lease.container_id == "old-container"
        assert lease.owner_id == ""
        assert lease.generation == 0
        assert lease.service_token == ""
        outcome, claimed = store.claim_worker_start(
            "ws-old", 3, "owner-new", 1000, 60)
        assert outcome == "started"
        assert claimed.generation == 1
        assert claimed.owner_id == "owner-new"
