"""Task 8：worker 生命周期租约、就绪校验、drain 与空闲回收。"""
from __future__ import annotations

import asyncio
import functools
import sqlite3
import time

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
                 block: asyncio.Event | None = None,
                 stop_gate: asyncio.Event | None = None,
                 stop_gate_calls: int | None = None,
                 stop_error: Exception | None = None):
        self.delay = delay
        self.fail = fail
        self.block = block
        self.stop_gate = stop_gate
        self.stop_gate_calls = stop_gate_calls
        self.stop_error = stop_error
        self._stop_seen = 0
        self.starts = 0
        self.stops = 0
        self.inspects = 0
        self.ensure_calls: list[tuple[str, int]] = []
        self.stop_calls: list[str] = []
        self.stop_targets: list[str] = []
        self.stop_requests: list[tuple[str, str | None]] = []
        self.stop_rejected = 0
        self.handles: dict[str, WorkerHandle] = {}
        self.entered = asyncio.Event()
        self.stop_entered = asyncio.Event()
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
            f"container-{workspace_id[:8]}-{self.starts}",
            f"http://10.0.0.{len(self.handles) + 2}:7860",
            f"service-token-{workspace_id[:8]}",
            (self.force_config_version
             if self.force_config_version is not None else config_version))
        self.handles[workspace_id] = handle
        return handle

    async def stop(self, workspace_id: str,
                   instance_id: str | None = None) -> None:
        # 生产 provisioner 按不可复用的实例身份停止。fake 同样在调用时
        # 解析当前实例；传入了与当前实例不符的身份时拒绝且不删除。
        target = self.handles.get(workspace_id)
        self.stops += 1
        self._stop_seen += 1
        self.stop_calls.append(workspace_id)
        self.stop_requests.append((workspace_id, instance_id))
        self.stop_targets.append(
            target.container_id if target is not None else "")
        self.stop_entered.set()
        if (self.stop_gate is not None
                and (self.stop_gate_calls is None
                     or self._stop_seen <= self.stop_gate_calls)):
            await self.stop_gate.wait()
        if self.stop_error is not None:
            raise self.stop_error
        if (instance_id is not None and target is not None
                and target.container_id != instance_id):
            self.stop_rejected += 1
            raise RuntimeUnavailable("container_conflict", 503,
                                     retry_after=5)
        if target is not None and self.handles.get(workspace_id) is target:
            self.handles.pop(workspace_id, None)

    async def inspect(self, workspace_id: str) -> dict:
        self.inspects += 1
        if self.inspect_error is not None:
            raise self.inspect_error
        handle = self.handles.get(workspace_id)
        if handle is None:
            return {"workspace_id": workspace_id, "container_id": "",
                    "instance_id": "", "config_version": 0, "ready": False,
                    "managed": False, "labels": {}}
        label_workspace = self.inspect_labels_workspace or workspace_id
        return {
            "workspace_id": self.inspect_workspace or workspace_id,
            "container_id": handle.container_id,
            "instance_id": handle.container_id,
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
            lease = store.get_worker_lease(user.workspace_id)
            active = _running(store, user)

            clock.value = 1901
            assert await manager.heartbeat(
                user.workspace_id, generation=lease.generation,
                instance_id=lease.container_id, active_requests=0)
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
            lease = store.get_worker_lease(user.workspace_id)

            clock.value = 1901
            assert await manager.heartbeat(
                user.workspace_id, generation=lease.generation,
                instance_id=lease.container_id, active_requests=1)
            assert await manager.collect_idle(clock.value) == []

            clock.value = 2802
            assert await manager.heartbeat(
                user.workspace_id, generation=lease.generation,
                instance_id=lease.container_id, active_requests=0)
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
            lease = store.get_worker_lease(user.workspace_id)
            clock.value = 1901
            assert await manager.heartbeat(
                user.workspace_id, generation=lease.generation,
                instance_id=lease.container_id, active_requests=0)

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


def test_stop_claim_is_generation_and_state_bound(tmp_path):
    with Store.open(tmp_path / "db") as store:
        user = store.create_user("A")
        outcome, lease = store.claim_worker_start(
            user.workspace_id, 1, "owner", 1000, 60)
        assert outcome == "started"
        assert store.complete_worker_start(
            user.workspace_id, "owner", lease.generation,
            WorkerHandle(user.workspace_id, "container-1",
                         "http://10.0.0.2:7860", "service-token", 1),
            1000, 60)
        assert store.mark_worker_draining(
            user.workspace_id, 1001, generation=lease.generation)

        assert not store.begin_worker_stop(
            user.workspace_id, 1001, generation=lease.generation + 1)
        assert store.get_worker_lease(user.workspace_id).state == "draining"
        assert store.begin_worker_stop(
            user.workspace_id, 1001, generation=lease.generation)
        assert store.get_worker_lease(user.workspace_id).state == "stopping"

        # 停止期间新 ensure 被拒绝；只有代次/状态都匹配的写回才成功。
        outcome, _ = store.claim_worker_start(
            user.workspace_id, 1, "other", 1001, 60)
        assert outcome == "draining"
        assert not store.stop_worker(
            user.workspace_id, 1002, generation=lease.generation,
            expected_state="draining")
        assert store.stop_worker(
            user.workspace_id, 1002, generation=lease.generation,
            expected_state="stopping")
        assert store.get_worker_lease(user.workspace_id).state == "stopped"


def test_idle_collection_never_deletes_persistent_data(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            clock = Clock()
            provisioner = FakeProvisioner()
            manager = RuntimeManager(store, provisioner, clock=clock,
                                     idle_seconds=900, lease_seconds=120)
            await manager.ensure_ready(user.workspace_id, 1)
            lease = store.get_worker_lease(user.workspace_id)
            sentinel = tmp_path / "workspaces" / user.workspace_id / "outputs"
            sentinel.mkdir(parents=True)
            (sentinel / "keep.txt").write_text("keep", encoding="utf-8")

            clock.value = 1901
            await manager.heartbeat(
                user.workspace_id, generation=lease.generation,
                instance_id=lease.container_id, active_requests=0)
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


def test_unix_client_maps_transport_loss_to_unknown_outcome_codes(
        monkeypatch, tmp_path):
    """真实 Unix 客户端的 EOF/超时必须映射为可恢复的未知结果码。"""
    from easel.control.runtime import UnixProvisionerClient

    client = UnixProvisionerClient(tmp_path / "provisioner.sock")

    async def unavailable(*args, **kwargs):
        raise OSError("socket reset")

    monkeypatch.setattr(
        asyncio, "open_unix_connection", unavailable, raising=False)
    with pytest.raises(RuntimeUnavailable) as error:
        asyncio.run(client._call({"action": "inspect"}, 0.1))
    assert error.value.code == "provisioner_unavailable"

    class Reader:
        async def readline(self):
            await asyncio.sleep(1)
            return b""

    class Writer:
        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def hanging(*args, **kwargs):
        return Reader(), Writer()

    monkeypatch.setattr(
        asyncio, "open_unix_connection", hanging, raising=False)
    with pytest.raises(RuntimeUnavailable) as error:
        asyncio.run(client._call({"action": "inspect"}, 0.01))
    assert error.value.code == "provisioner_timeout"


def test_late_drain_reply_cannot_overwrite_new_generation(tmp_path):
    """旧 drain 的 stop 已发生但回复暂停；新代次就绪后旧回复不得改写。"""
    async def scenario():
        with Store.open(tmp_path / "db") as first_store, \
                Store.open(tmp_path / "db") as second_store:
            user = first_store.create_user("A")
            clock = Clock()
            gate = asyncio.Event()
            provisioner = FakeProvisioner(stop_gate=gate,
                                          stop_gate_calls=1)
            first = RuntimeManager(first_store, provisioner, clock=clock,
                                   lease_seconds=1, poll_interval=0.01,
                                   drain_timeout=5)
            second = RuntimeManager(second_store, provisioner, clock=clock,
                                    lease_seconds=1, poll_interval=0.01)

            old = await first.ensure_ready(user.workspace_id, 1)
            drain = asyncio.create_task(first.drain(user.workspace_id))
            await asyncio.wait_for(provisioner.stop_entered.wait(), 1)

            # stop 的物理效果已经作用在旧实例上，仅回复被暂停；租约到期
            # 后另一管理者可接管并建立新代次。
            clock.value = 1002
            new = await second.ensure_ready(user.workspace_id, 1)
            assert new.container_id != old.container_id
            before_reply = second_store.get_worker_lease(user.workspace_id)
            assert before_reply.state == "ready"
            assert before_reply.generation > 1

            gate.set()
            await asyncio.wait_for(drain, 2)

            after_reply = second_store.get_worker_lease(user.workspace_id)
            assert after_reply.state == "ready"
            assert after_reply.container_id == new.container_id
            assert after_reply.generation == before_reply.generation
            assert provisioner.handles[user.workspace_id].container_id == (
                new.container_id)
            assert old.container_id in provisioner.stop_targets
            assert new.container_id not in provisioner.stop_targets
    run(scenario())


class GatedBeginStop:
    """在 begin_worker_stop 已提交、尚未返回 drain 调用方时暂停。"""

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


def test_stop_arriving_after_takeover_cannot_remove_new_instance(tmp_path):
    """停止认领已提交但 stop 尚未送达时，新代次实例不得被旧 stop 删除。"""
    async def scenario():
        with Store.open(tmp_path / "db") as first_store, \
                Store.open(tmp_path / "db") as second_store:
            user = first_store.create_user("A")
            clock = Clock()
            gate = asyncio.Event()
            entered = asyncio.Event()
            provisioner = FakeProvisioner()
            first = RuntimeManager(
                first_store, provisioner, clock=clock, lease_seconds=1,
                poll_interval=0.01, drain_timeout=5,
                db_runner=GatedBeginStop(entered, gate))
            second = RuntimeManager(second_store, provisioner, clock=clock,
                                    lease_seconds=1, poll_interval=0.01)

            old = await first.ensure_ready(user.workspace_id, 1)
            drain = asyncio.create_task(first.drain(user.workspace_id))
            await asyncio.wait_for(entered.wait(), 1)
            paused = first_store.get_worker_lease(user.workspace_id)
            assert paused.state == "stopping"
            assert provisioner.stops == 0, "旧 stop 尚未送达"

            clock.value = 1002
            new = await second.ensure_ready(user.workspace_id, 1)
            assert new.container_id != old.container_id

            gate.set()
            with pytest.raises(RuntimeUnavailable):
                await asyncio.wait_for(drain, 2)

            lease = second_store.get_worker_lease(user.workspace_id)
            assert lease.state == "ready"
            assert lease.container_id == new.container_id
            assert provisioner.handles[user.workspace_id].container_id == (
                new.container_id)
            assert provisioner.stop_requests[-1] == (
                user.workspace_id, old.container_id)
            assert provisioner.stop_rejected == 1
    run(scenario())


def test_idle_stop_after_takeover_cannot_remove_new_instance(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as first_store, \
                Store.open(tmp_path / "db") as second_store:
            user = first_store.create_user("A")
            clock = Clock()
            gate = asyncio.Event()
            entered = asyncio.Event()
            provisioner = FakeProvisioner()
            first = RuntimeManager(
                first_store, provisioner, clock=clock, lease_seconds=1,
                idle_seconds=0, poll_interval=0.01,
                db_runner=GatedBeginStop(entered, gate))
            second = RuntimeManager(second_store, provisioner, clock=clock,
                                    lease_seconds=1, poll_interval=0.01)

            old = await first.ensure_ready(user.workspace_id, 1)
            lease = first_store.get_worker_lease(user.workspace_id)
            assert await first.heartbeat(
                user.workspace_id, generation=lease.generation,
                instance_id=lease.container_id, active_requests=0)
            collecting = asyncio.create_task(first.collect_idle(1000))
            await asyncio.wait_for(entered.wait(), 1)
            assert provisioner.stops == 0

            clock.value = 1002
            new = await second.ensure_ready(user.workspace_id, 1)
            gate.set()
            assert await asyncio.wait_for(collecting, 2) == []

            assert provisioner.handles[user.workspace_id].container_id == (
                new.container_id)
            assert provisioner.stop_requests[-1] == (
                user.workspace_id, old.container_id)
            assert provisioner.stop_rejected == 1
            current = second_store.get_worker_lease(user.workspace_id)
            assert current.state == "ready"
            assert current.container_id == new.container_id
    run(scenario())


def test_upgrade_stop_after_takeover_cannot_remove_new_instance(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as first_store, \
                Store.open(tmp_path / "db") as second_store:
            user = first_store.create_user("A")
            clock = Clock()
            gate = asyncio.Event()
            entered = asyncio.Event()
            provisioner = FakeProvisioner()
            first = RuntimeManager(
                first_store, provisioner, clock=clock, lease_seconds=1,
                poll_interval=0.01, drain_timeout=5,
                db_runner=GatedBeginStop(entered, gate))
            second = RuntimeManager(second_store, provisioner, clock=clock,
                                    lease_seconds=1, poll_interval=0.01)

            old = await first.ensure_ready(user.workspace_id, 1)
            upgrading = asyncio.create_task(
                first.ensure_ready(user.workspace_id, 2))
            await asyncio.wait_for(entered.wait(), 1)
            assert provisioner.stops == 0

            clock.value = 1002
            new = await second.ensure_ready(user.workspace_id, 1)
            gate.set()
            with pytest.raises(RuntimeUnavailable):
                await asyncio.wait_for(upgrading, 2)

            assert provisioner.handles[user.workspace_id].container_id == (
                new.container_id)
            assert provisioner.stop_requests[-1] == (
                user.workspace_id, old.container_id)
            assert provisioner.stop_rejected == 1
            current = second_store.get_worker_lease(user.workspace_id)
            assert current.state == "ready"
            assert current.container_id == new.container_id
    run(scenario())


def test_stop_failure_does_not_write_stopped(tmp_path):
    """provisioner 停止失败时，drain/空闲/升级都保留可恢复状态。"""
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            clock = Clock()
            provisioner = FakeProvisioner(stop_error=RuntimeUnavailable(
                "container_stop_failed", 503, retry_after=5))
            manager = RuntimeManager(store, provisioner, clock=clock,
                                     lease_seconds=120, idle_seconds=900,
                                     poll_interval=0.01, drain_timeout=1)
            await manager.ensure_ready(user.workspace_id, 1)
            lease = store.get_worker_lease(user.workspace_id)

            with pytest.raises(RuntimeUnavailable) as error:
                await manager.drain(user.workspace_id)
            assert error.value.code == "container_stop_failed"
            failed = store.get_worker_lease(user.workspace_id)
            assert failed.state == "stopping"
            assert failed.container_id == lease.container_id

            # 从 stopping 恢复为可再尝试的 drained/ready 路径由新 ensure
            # 的 recover_stop 负责；这里确认不会伪写 stopped。
            assert failed.state != "stopped"
            assert provisioner.handles[user.workspace_id].container_id == (
                lease.container_id)
    run(scenario())


def test_idle_and_upgrade_stop_failure_preserve_state(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            clock = Clock()
            provisioner = FakeProvisioner(stop_error=RuntimeUnavailable(
                "container_stop_failed", 503, retry_after=5))
            manager = RuntimeManager(store, provisioner, clock=clock,
                                     lease_seconds=120, idle_seconds=0,
                                     poll_interval=0.01, drain_timeout=1)
            await manager.ensure_ready(user.workspace_id, 1)
            lease = store.get_worker_lease(user.workspace_id)
            assert await manager.heartbeat(
                user.workspace_id, generation=lease.generation,
                instance_id=lease.container_id, active_requests=0)
            clock.value = 1000
            assert await manager.collect_idle(clock.value) == []
            after_idle = store.get_worker_lease(user.workspace_id)
            assert after_idle.state == "stopping"
            assert provisioner.handles[user.workspace_id].container_id == (
                lease.container_id)

            # 直接构造 ready 的另一个 workspace 验证升级失败。
            other = store.create_user("B")
            await manager.ensure_ready(other.workspace_id, 1)
            with pytest.raises(RuntimeUnavailable):
                await manager.ensure_ready(other.workspace_id, 2)
            updated = store.get_worker_lease(other.workspace_id)
            assert updated.state == "stopping"
            assert updated.config_version == 1
    run(scenario())


def test_old_holder_heartbeat_cannot_touch_new_generation(tmp_path):
    """心跳必须携带调用者已持有的 generation/实例身份。"""
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            clock = Clock()
            provisioner = FakeProvisioner()
            manager = RuntimeManager(store, provisioner, clock=clock,
                                     lease_seconds=1)
            old = await manager.ensure_ready(user.workspace_id, 1)
            old_lease = store.get_worker_lease(user.workspace_id)
            assert await manager.heartbeat(
                user.workspace_id, generation=old_lease.generation,
                instance_id=old_lease.container_id, active_requests=7)

            assert store.mark_worker_draining(
                user.workspace_id, 1001,
                generation=old_lease.generation)
            assert store.begin_worker_stop(
                user.workspace_id, 1001,
                generation=old_lease.generation)
            assert store.stop_worker(
                user.workspace_id, 1001,
                generation=old_lease.generation,
                expected_state="stopping")
            outcome, claimed = store.claim_worker_start(
                user.workspace_id, 1, "new-owner", 1002, 60)
            assert outcome == "started"
            new_handle = WorkerHandle(
                user.workspace_id, "new-instance", "http://10.0.0.9:7860",
                "new-token", 1)
            assert store.complete_worker_start(
                user.workspace_id, "new-owner", claimed.generation,
                new_handle, 1002, 60)
            assert store.heartbeat_worker(
                user.workspace_id, claimed.generation, 1003, 60, 7,
                instance_id=new_handle.container_id)

            clock.value = 1003
            before = store.get_worker_lease(user.workspace_id)
            assert not await manager.heartbeat(
                user.workspace_id, generation=old_lease.generation,
                instance_id=old.container_id, active_requests=0)
            after = store.get_worker_lease(user.workspace_id)
            assert after.generation == before.generation
            assert after.owner_id == before.owner_id == "new-owner"
            assert after.active_requests == before.active_requests == 7
            assert after.heartbeat_at == before.heartbeat_at
            assert after.lease_expires_at == before.lease_expires_at

            assert await manager.heartbeat(
                user.workspace_id, generation=claimed.generation,
                instance_id=new_handle.container_id, active_requests=0)
            renewed = store.get_worker_lease(user.workspace_id)
            assert renewed.active_requests == 0
            assert renewed.heartbeat_at == 1003
    run(scenario())


def test_drain_cas_failure_before_stop_does_not_call_provisioner(
        tmp_path, monkeypatch):
    """认领后被新代次接管时，旧 drain 必须在调用 stop 前放弃。"""
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            clock = Clock()
            provisioner = FakeProvisioner()
            manager = RuntimeManager(store, provisioner, clock=clock,
                                     lease_seconds=1, poll_interval=0.01,
                                     drain_timeout=0.5)
            await manager.ensure_ready(user.workspace_id, 1)

            async def no_workload(workspace_id):
                # 模拟等待期间另一管理者接管：直接建立新代次。
                lease = store.get_worker_lease(workspace_id)
                store.mark_worker_draining(
                    workspace_id, 1000, generation=lease.generation)
                store.stop_worker(workspace_id, 1000,
                                  generation=lease.generation)
                outcome, new_lease = store.claim_worker_start(
                    workspace_id, 1, "other-owner", 1000, 60)
                assert outcome == "started"
                store.complete_worker_start(
                    workspace_id, "other-owner", new_lease.generation,
                    WorkerHandle(workspace_id, "other-container",
                                 "http://10.0.0.9:7860", "other-token", 1),
                    1000, 60)
                return False

            monkeypatch.setattr(manager, "_has_active_workload", no_workload)
            await manager.drain(user.workspace_id)

            assert provisioner.stops == 0, "已被接管的旧 drain 不得停止新实例"
            lease = store.get_worker_lease(user.workspace_id)
            assert lease.state == "ready"
            assert lease.container_id == "other-container"
    run(scenario())


def test_control_app_runtime_methods_use_the_app_database_thread(tmp_path):
    """真实 lifespan 装配：runtime 的短数据库步骤必须在连接所属线程执行。"""
    from fastapi.testclient import TestClient

    from easel.control.app import ControlConfig, create_app
    from easel.control.auth import AuthService

    origin = "https://easel.test"
    startup_store = Store.connect(tmp_path / "control.sqlite3")
    provisioner = FakeProvisioner(delay=0.05)
    app = create_app(
        store=startup_store, auth=AuthService(startup_store),
        config=ControlConfig(public_origin=origin),
        provisioner=provisioner)
    try:
        with TestClient(app, base_url=origin) as client:
            control_store = app.state.control_store
            runtime = app.state.runtime

            def call_db(function, *args):
                async def runner():
                    loop = asyncio.get_running_loop()
                    return await loop.run_in_executor(
                        app.state.db_executor,
                        functools.partial(function, *args))
                return client.portal.call(runner)

            admin = call_db(control_store.create_user, "Owner", "admin")
            member = call_db(control_store.create_user, "Member")
            invite = call_db(app.state.auth.create_invite,
                             admin, member.user_id)
            exchanged = client.post(
                "/api/auth/exchange", json={"token": invite},
                headers={"Origin": origin})
            assert exchanged.status_code == 200

            task = client.portal.start_task_soon(
                runtime.ensure_ready, member.workspace_id, 1)
            # 容器启动期间并发走真实认证中间件与数据库线程。
            for _ in range(3):
                assert client.get("/api/health").status_code == 200
                assert client.get("/api/auth/me").status_code == 200
            handle = task.result(timeout=10)
            assert handle.workspace_id == member.workspace_id
            handle_lease = call_db(control_store.get_worker_lease,
                                   member.workspace_id)

            assert client.portal.start_task_soon(
                functools.partial(
                    runtime.heartbeat, member.workspace_id,
                    generation=handle_lease.generation,
                    instance_id=handle.container_id,
                    active_requests=0)).result(timeout=10)
            assert client.portal.start_task_soon(
                runtime.collect_idle, time.time()).result(timeout=10) == []
            client.portal.start_task_soon(
                runtime.drain, member.workspace_id).result(timeout=10)

            lease = call_db(control_store.get_worker_lease,
                            member.workspace_id)
            assert lease.state == "stopped"
            assert client.get("/api/auth/me").status_code == 200
    finally:
        startup_store.close()


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
