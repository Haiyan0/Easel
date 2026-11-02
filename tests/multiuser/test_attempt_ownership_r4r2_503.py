"""R4-R2：活跃预占接管与迟到旧请求的精确执行权围栏。

主审复现的缺陷：A 预占后尚未抵达 provisioner 工作区锁，B 提前接管并
``commit_attempt(A)``；provisioner 因“当前没有 A 记录”返回成功，B 清空
DB 槽位后 A 迟到执行，写出无主 ``A/pending``，普通首屏永久 503。

本文件把 provisioner 的 workspace 锁作为唯一串行化点验证修复：
- A 已在锁内执行时，B 的 commit 等待并按精确 attempt 收口；
- A 尚未进入锁时，B 的 commit 持久围栏该 attempt，A 之后在目录/容器
  副作用之前被拒绝，不再产生无主 pending。
"""
from __future__ import annotations

import asyncio
import dataclasses

import pytest

from tests.multiuser import test_attempt_ownership_503 as h

WORKSPACE = h.WORKSPACE
WORKER = h.WORKER
GATEWAY = h.GATEWAY
provisioner = h.provisioner
RuntimeUnavailable = h.RuntimeUnavailable
RuntimeManager = h.RuntimeManager


# ----------------------------------------------------------------------
# provisioner 归属语义
# ----------------------------------------------------------------------

def _commit(service, attempt_id):
    asyncio.run(service.commit_attempt(
        WORKSPACE, WORKER, GATEWAY, attempt_id=attempt_id))


def test_commit_without_record_persists_execution_fence(tmp_path):
    """未执行过的 attempt 被 commit 时必须持久围栏，而不是无操作。"""
    service = h._service(tmp_path)
    attempt = "a" * 32

    _commit(service, attempt)

    record = service._load_attempt(WORKSPACE)
    assert record is not None
    assert record["attempt_id"] == attempt
    assert record["status"] == "completed"
    assert record["scope"] == "none"
    assert record["fenced_attempts"] == [attempt]

    with pytest.raises(provisioner.ProvisionerError) as refused:
        service._ensure_attempt_available(WORKSPACE, attempt)
    assert refused.value.code == "gateway_attempt_fenced"

    # 围栏只针对被收口的旧 attempt，新 attempt 仍可进入。
    service._ensure_attempt_available(WORKSPACE, "b" * 32)

    # 重启后围栏仍在（依赖落盘的 attempt 记录）。
    restarted = h._service(tmp_path)
    assert restarted._load_attempt(WORKSPACE)[
        "fenced_attempts"] == [attempt]
    with pytest.raises(provisioner.ProvisionerError) as refused:
        restarted._ensure_attempt_available(WORKSPACE, attempt)
    assert refused.value.code == "gateway_attempt_fenced"


def test_active_attempt_record_is_committed_not_fenced(tmp_path):
    """A 已在锁内持久真实记录：B 的 commit 按精确 attempt 收口，不误围栏。"""
    service = h._service(tmp_path)
    attempt = "a" * 32
    h._record(service, attempt, worker_created=False, gateway_created=True)

    _commit(service, attempt)

    record = service._load_attempt(WORKSPACE)
    assert record is not None
    assert record["status"] == "committed"
    assert "fenced_attempts" not in record
    # 已执行 attempt 的后续收口保持幂等，也不会被误围栏。
    _commit(service, attempt)
    record = service._load_attempt(WORKSPACE)
    assert record["status"] == "committed"
    assert "fenced_attempts" not in record


def test_fenced_attempt_cannot_create_gateway_side_effects(tmp_path):
    """迟到 attempt 必须在 gateway.ensure 之前被拒绝，不产生新资源。"""
    gateway = h.FakeGateway()
    service = h._service(tmp_path, gateway)
    attempt = "a" * 32
    _commit(service, attempt)

    with pytest.raises(provisioner.ProvisionerError) as refused:
        asyncio.run(service._with_gateway(
            h._handle(), 2, worker_created=False, attempt_id=attempt))

    assert refused.value.code == "gateway_attempt_fenced"
    assert gateway.ensure_calls == 0


def test_fence_survives_new_attempt_record(tmp_path):
    """新 attempt 的正常记录不得丢失旧围栏；被围栏的旧 attempt 仍拒绝。"""
    service = h._service(tmp_path)
    old, new = "a" * 32, "b" * 32
    _commit(service, old)
    h._record(service, new, worker_created=False, gateway_created=True)

    record = service._load_attempt(WORKSPACE)
    assert record is not None
    assert record["attempt_id"] == new
    assert record["status"] == "pending"
    assert record["fenced_attempts"] == [old]
    with pytest.raises(provisioner.ProvisionerError):
        service._ensure_attempt_available(WORKSPACE, old)
    with pytest.raises(provisioner.ProvisionerError) as direct:
        h._record(service, old, worker_created=False, gateway_created=True)
    assert direct.value.code == "gateway_attempt_fenced"

    # 新 attempt 自己的重试与收口不受影响，围栏继续保留。
    service._ensure_attempt_available(WORKSPACE, new)
    _commit(service, new)
    record = service._load_attempt(WORKSPACE)
    assert record["attempt_id"] == new
    assert record["status"] == "committed"
    assert record["fenced_attempts"] == [old]


def test_commit_of_unknown_attempt_keeps_committed_record(tmp_path):
    """已提交记录被未知 attempt 收口：保留原记录，只追加精确围栏。"""
    service = h._service(tmp_path)
    known, unknown = "a" * 32, "b" * 32
    h._record(service, known, worker_created=False, gateway_created=True)
    _commit(service, known)
    assert service._load_attempt(WORKSPACE)["status"] == "committed"

    _commit(service, unknown)

    record = service._load_attempt(WORKSPACE)
    assert record["attempt_id"] == known
    assert record["status"] == "committed"
    assert record["fenced_attempts"] == [unknown]
    with pytest.raises(provisioner.ProvisionerError) as refused:
        service._ensure_attempt_available(WORKSPACE, unknown)
    assert refused.value.code == "gateway_attempt_fenced"
    # 重复 commit 幂等，不重复追加或改写记录。
    _commit(service, unknown)
    assert service._load_attempt(WORKSPACE)["fenced_attempts"] == [unknown]


def test_legacy_record_commit_fences_different_attempt(tmp_path):
    """修复旧坏记录时同样围栏请求收口的另一个未知 attempt。"""
    service = h._service(tmp_path)
    legacy, requested = "e" * 32, "a" * 32
    h._write_attempt(tmp_path, {
        "attempt_id": legacy,
        "worker_container_id": WORKER,
        "gateway_instance_id": GATEWAY,
        "worker_created": False,
        "gateway_created": False,
        "scope": "gateway",
        "status": "pending",
    })

    _commit(service, requested)

    record = service._load_attempt(WORKSPACE)
    assert record["attempt_id"] == legacy
    assert record["status"] == "completed"
    assert record["fenced_attempts"] == [requested]
    with pytest.raises(provisioner.ProvisionerError) as refused:
        service._ensure_attempt_available(WORKSPACE, requested)
    assert refused.value.code == "gateway_attempt_fenced"


# ----------------------------------------------------------------------
# 运行时确定性交错：真实 Store/RuntimeManager + 真实 commit/admission
# ----------------------------------------------------------------------

def _candidate(handle, attempt_id):
    return dataclasses.replace(
        handle, attempt_id=attempt_id, gateway_state="available",
        worker_created=False, gateway_created=False)


class _RealProvisionerAdapter:
    """按真实 provisioner 顺序：先 admission，再记录远程候选。

    A 的首次远程调用在 admission 之前暂停，模拟“预占已落盘、请求尚未
    抵达 provisioner 工作区锁”；B 的调用以此作为已接管的信号。
    """

    def __init__(self, service, handle, *, a_pause, release_a, a_done,
                 a_finish=None):
        self.service = service
        self.handle = handle
        self.a_pause = a_pause
        self.release_a = release_a
        self.a_done = a_done
        self.a_finish = a_finish

    async def commit_attempt(self, *args, **kwargs):
        return await self.service.commit_attempt(*args, **kwargs)

    async def abort(self, *args, **kwargs):
        return await self.service.abort(*args, **kwargs)

    async def inspect(self, workspace_id):
        return await h._ready_inspect(workspace_id)

    def _admit(self, workspace_id, attempt_id):
        try:
            self.service._ensure_attempt_available(workspace_id, attempt_id)
        except provisioner.ProvisionerError as error:
            raise RuntimeUnavailable(error.code, 503, retry_after=5)

    def _record(self, workspace_id, attempt_id):
        candidate = _candidate(self.handle, attempt_id)
        self.service._record_attempt(
            workspace_id, attempt_id, candidate,
            worker_created=False, gateway_created=False)
        return candidate

    async def _run(self, workspace_id, attempt_id):
        self._admit(workspace_id, attempt_id)
        return self._record(workspace_id, attempt_id)

    async def _finish_a(self, workspace_id, attempt_id):
        if self.a_finish is not None:
            return await self.a_finish(workspace_id, attempt_id)
        return await self._run(workspace_id, attempt_id)

    async def ensure(self, workspace_id, config_version, *,
                     attempt_id="", orphan_takeover=False):
        if not self.a_pause["attempt"]:
            # A：预占已落盘，请求在 provisioner 工作区锁之前暂停。
            self.a_pause["attempt"] = attempt_id
            self.a_pause["entered"].set()
            try:
                await self.release_a.wait()
            except asyncio.CancelledError:
                # 取消发生在远程调用之前：不产生任何远程副作用。
                self.a_done.set()
                raise
            lease = self.a_pause["store"].get_worker_lease(workspace_id)
            self.a_pause["owner"] = lease.owner_id
            self.a_pause["pending_slot"] = lease.pending_attempt_id
            try:
                return await self._finish_a(workspace_id, attempt_id)
            except RuntimeUnavailable as error:
                self.a_pause["error"] = error.code
                raise
            finally:
                self.a_done.set()
        if attempt_id == self.a_pause["attempt"]:
            # 同一旧 attempt 的僵尸请求：admission 之前不会产生副作用。
            try:
                return await self._run(workspace_id, attempt_id)
            finally:
                self.a_done.set()
        self.release_a.set()
        await asyncio.wait_for(self.a_done.wait(), 2)
        return await self._run(workspace_id, attempt_id)

    async def restore_gateway(self, workspace_id, worker_container_id,
                              config_version, *, attempt_id=""):
        return await self.ensure(
            workspace_id, config_version, attempt_id=attempt_id)


async def _seed_ready(tmp_path):
    store, service, gateway, workspace, lease, handle, original = \
        h._seed_expired_ensure(tmp_path)
    ready = await h._make_normal_ready(store, service, workspace, handle)
    return store, service, gateway, workspace, handle, ready


def _pause_state(store):
    return {"attempt": "", "entered": asyncio.Event(), "store": store,
            "owner": "", "pending_slot": None, "error": ""}


def test_live_takeover_fences_late_ensure_before_side_effects(tmp_path):
    """A 预占后在锁前暂停；B 接管/commit 后 A 迟到，必须被围栏拒绝。"""
    async def scenario():
        store, service, gateway, workspace, handle, ready = \
            await _seed_ready(tmp_path)
        with store:
            release_a = asyncio.Event()
            a_done = asyncio.Event()
            pause = _pause_state(store)
            adapter = _RealProvisionerAdapter(
                service, handle, a_pause=pause, release_a=release_a,
                a_done=a_done)
            a = RuntimeManager(
                store, adapter, owner_id="live-a", gateway_required=True,
                clock=lambda: 1201)
            b = RuntimeManager(
                store, adapter, owner_id="live-b", gateway_required=True,
                clock=lambda: 1201)
            task = asyncio.create_task(a.ensure_ready(workspace, 1))
            await asyncio.wait_for(pause["entered"].wait(), 2)
            reserved = store.get_worker_lease(workspace)
            assert reserved is not None
            assert reserved.state == "starting"
            assert reserved.owner_id == "live-a"
            assert reserved.attempt_id == pause["attempt"]
            assert reserved.attempt_id != ready.attempt_id
            assert reserved.pending_attempt_id == ""

            final = await asyncio.wait_for(b.ensure_ready(workspace, 1), 2)
            assert final.gateway_instance_id == GATEWAY
            await asyncio.gather(task, return_exceptions=True)

            # A 恢复时 B 已接管并清空槽位；A 在副作用前被拒绝。
            assert pause["owner"] == "live-b"
            assert pause["pending_slot"] == ""
            assert pause["error"] == "gateway_attempt_fenced"
            ready_now = store.get_worker_lease(workspace)
            assert ready_now is not None
            assert ready_now.state == "ready"
            assert ready_now.owner_id == "live-b"
            assert ready_now.attempt_id == final.attempt_id
            assert ready_now.pending_attempt_id == ""

            record = service._load_attempt(workspace)
            assert record is not None
            assert record["attempt_id"] == final.attempt_id
            assert record["attempt_id"] != pause["attempt"]
            assert record["status"] in ("committed", "completed")
            assert pause["attempt"] in record["fenced_attempts"]
            # A 从未写出无主 pending；围栏在后续首屏中持续生效。
            with pytest.raises(provisioner.ProvisionerError) as refused:
                service._ensure_attempt_available(
                    workspace, pause["attempt"])
            assert refused.value.code == "gateway_attempt_fenced"

            # 普通首屏连续刷新不再 gateway_cleanup_pending。
            for _ in range(3):
                again = await asyncio.wait_for(
                    b.ensure_ready(workspace, 1), 2)
                assert again.gateway_instance_id == GATEWAY
            with pytest.raises(RuntimeUnavailable) as zombie:
                await adapter.ensure(
                    workspace, 1, attempt_id=pause["attempt"])
            assert zombie.value.code == "gateway_attempt_fenced"
            assert gateway.stops == []

    asyncio.run(scenario())


def test_live_takeover_fences_late_restore_before_side_effects(tmp_path):
    """显式 restore 入口与普通首屏共用同一围栏语义。"""
    async def scenario():
        store, service, gateway, workspace, handle, ready = \
            await _seed_ready(tmp_path)
        with store:
            release_a = asyncio.Event()
            a_done = asyncio.Event()
            pause = _pause_state(store)
            adapter = _RealProvisionerAdapter(
                service, handle, a_pause=pause, release_a=release_a,
                a_done=a_done)
            a = RuntimeManager(
                store, adapter, owner_id="live-a", gateway_required=True,
                clock=lambda: 1201)
            b = RuntimeManager(
                store, adapter, owner_id="live-b", gateway_required=True,
                clock=lambda: 1201)
            task = asyncio.create_task(
                a.restore_gateway(workspace, WORKER, 1))
            await asyncio.wait_for(pause["entered"].wait(), 2)
            reserved = store.get_worker_lease(workspace)
            assert reserved is not None
            assert reserved.state == "starting"
            assert reserved.attempt_id == pause["attempt"]

            final = await asyncio.wait_for(
                b.restore_gateway(workspace, WORKER, 1), 2)
            assert final is not None
            assert final.gateway_instance_id == GATEWAY
            await asyncio.gather(task, return_exceptions=True)

            assert pause["owner"] == "live-b"
            assert pause["pending_slot"] == ""
            assert pause["error"] == "gateway_attempt_fenced"
            ready_now = store.get_worker_lease(workspace)
            assert ready_now is not None
            assert ready_now.state == "ready"
            assert ready_now.attempt_id == final.attempt_id
            assert ready_now.pending_attempt_id == ""
            record = service._load_attempt(workspace)
            assert record["attempt_id"] == final.attempt_id
            assert pause["attempt"] in record["fenced_attempts"]
            with pytest.raises(provisioner.ProvisionerError) as refused:
                service._ensure_attempt_available(
                    workspace, pause["attempt"])
            assert refused.value.code == "gateway_attempt_fenced"

            # 显式恢复入口的后续普通首屏可用，且不再出现旧 A pending。
            again = await asyncio.wait_for(b.ensure_ready(workspace, 1), 2)
            assert again.gateway_instance_id == GATEWAY

    asyncio.run(scenario())


def test_unknown_result_before_takeover_fences_late_attempt(tmp_path):
    """A 结果未知（回复丢失）后 B 接管：旧 attempt 围栏，僵尸请求被拒绝。"""
    async def scenario():
        store, service, gateway, workspace, handle, ready = \
            await _seed_ready(tmp_path)
        with store:
            release_a = asyncio.Event()
            a_done = asyncio.Event()
            pause = _pause_state(store)

            async def unknown_outcome(workspace_id, attempt_id):
                # 请求在发出/返回前丢失，control 只看到超时；A 未产生记录。
                raise RuntimeUnavailable(
                    "provisioner_timeout", 503, retry_after=5)

            adapter = _RealProvisionerAdapter(
                service, handle, a_pause=pause, release_a=release_a,
                a_done=a_done, a_finish=unknown_outcome)
            a = RuntimeManager(
                store, adapter, owner_id="live-a", gateway_required=True,
                clock=lambda: 1201)
            b = RuntimeManager(
                store, adapter, owner_id="live-b", gateway_required=True,
                clock=lambda: 1201)
            task = asyncio.create_task(a.ensure_ready(workspace, 1))
            await asyncio.wait_for(pause["entered"].wait(), 2)
            reserved = store.get_worker_lease(workspace)
            assert reserved is not None
            assert reserved.state == "starting"
            assert reserved.attempt_id == pause["attempt"]
            assert reserved.pending_attempt_id == ""

            final = await asyncio.wait_for(b.ensure_ready(workspace, 1), 2)
            assert final.gateway_instance_id == GATEWAY
            await asyncio.gather(task, return_exceptions=True)
            assert pause["error"] == "provisioner_timeout"

            ready_now = store.get_worker_lease(workspace)
            assert ready_now is not None
            assert ready_now.state == "ready"
            assert ready_now.pending_attempt_id == ""
            record = service._load_attempt(workspace)
            assert record["attempt_id"] == final.attempt_id
            assert pause["attempt"] in record["fenced_attempts"]
            # A 迟到的僵尸请求仍被拒绝，不会写出无主 pending。
            with pytest.raises(RuntimeUnavailable) as zombie:
                await adapter.ensure(
                    workspace, 1, attempt_id=pause["attempt"])
            assert zombie.value.code == "gateway_attempt_fenced"

    asyncio.run(scenario())


def test_cancel_before_takeover_does_not_resurrect_late_attempt(tmp_path):
    """A 取消后 B 接管：旧 attempt 围栏，取消的请求不得复活写 pending。"""
    async def scenario():
        store, service, gateway, workspace, handle, ready = \
            await _seed_ready(tmp_path)
        with store:
            pause = _pause_state(store)
            adapter = _RealProvisionerAdapter(
                service, handle, a_pause=pause,
                release_a=asyncio.Event(), a_done=asyncio.Event())
            a = RuntimeManager(
                store, adapter, owner_id="live-a", gateway_required=True,
                clock=lambda: 1201)
            b = RuntimeManager(
                store, adapter, owner_id="live-b", gateway_required=True,
                clock=lambda: 1201)
            task = asyncio.create_task(a.ensure_ready(workspace, 1))
            await asyncio.wait_for(pause["entered"].wait(), 2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            cancelled = store.get_worker_lease(workspace)
            assert cancelled is not None
            assert cancelled.state == "starting"
            assert cancelled.attempt_id == pause["attempt"]
            assert cancelled.pending_attempt_id == ""

            adapter.release_a.set()
            final = await asyncio.wait_for(b.ensure_ready(workspace, 1), 2)
            assert final.gateway_instance_id == GATEWAY
            ready_now = store.get_worker_lease(workspace)
            assert ready_now.state == "ready"
            record = service._load_attempt(workspace)
            assert pause["attempt"] in record["fenced_attempts"]
            with pytest.raises(RuntimeUnavailable) as zombie:
                await adapter.ensure(
                    workspace, 1, attempt_id=pause["attempt"])
            assert zombie.value.code == "gateway_attempt_fenced"

    asyncio.run(scenario())


def test_interrupted_takeover_commit_fences_both_attempts(tmp_path):
    """B 收口中再次中断（响应丢失）：下一 owner 按精确 attempt 围栏并前进。"""
    async def scenario():
        store, service, gateway, workspace, handle, ready = \
            await _seed_ready(tmp_path)
        with store:
            release_a = asyncio.Event()
            a_done = asyncio.Event()
            pause = _pause_state(store)
            state = {"lose_commit_response": True}

            class Adapter(_RealProvisionerAdapter):
                async def commit_attempt(self, *args, **kwargs):
                    result = await self.service.commit_attempt(
                        *args, **kwargs)
                    if (state["lose_commit_response"]
                            and kwargs.get("attempt_id")
                            == pause["attempt"]):
                        # provisioner 已持久围栏，但控制进程未收到回复。
                        state["lose_commit_response"] = False
                        raise RuntimeUnavailable(
                            "provisioner_timeout", 503, retry_after=5)
                    return result

            adapter = Adapter(
                service, handle, a_pause=pause, release_a=release_a,
                a_done=a_done)
            a = RuntimeManager(
                store, adapter, owner_id="live-a", gateway_required=True,
                clock=lambda: 1201)
            b = RuntimeManager(
                store, adapter, owner_id="live-b", gateway_required=True,
                clock=lambda: 1201)
            task = asyncio.create_task(a.ensure_ready(workspace, 1))
            await asyncio.wait_for(pause["entered"].wait(), 2)
            with pytest.raises(RuntimeUnavailable) as interrupted:
                await b.ensure_ready(workspace, 1)
            assert interrupted.value.code == "provisioner_timeout"
            handed = store.get_worker_lease(workspace)
            assert handed is not None
            assert handed.owner_id == "live-b"
            assert handed.pending_attempt_id == pause["attempt"]
            interrupted_attempt = handed.attempt_id

            # 下一 owner 在租约到期后接管：先按精确 attempt 收口旧槽位。
            clock = handed.lease_expires_at + 1
            c = RuntimeManager(
                store, adapter, owner_id="live-c", gateway_required=True,
                clock=lambda: clock)
            release_a.set()
            final = await asyncio.wait_for(c.ensure_ready(workspace, 1), 2)
            assert final.gateway_instance_id == GATEWAY
            await asyncio.gather(task, return_exceptions=True)

            ready_now = store.get_worker_lease(workspace)
            assert ready_now is not None
            assert ready_now.state == "ready"
            assert ready_now.owner_id == "live-c"
            assert ready_now.pending_attempt_id == ""
            record = service._load_attempt(workspace)
            assert record["attempt_id"] == final.attempt_id
            # A 的预占 attempt 与 B 交接后的 attempt 都被精确围栏。
            assert pause["attempt"] in record["fenced_attempts"]
            assert interrupted_attempt in record["fenced_attempts"]
            for stale in (pause["attempt"], interrupted_attempt):
                with pytest.raises(provisioner.ProvisionerError) as refused:
                    service._ensure_attempt_available(workspace, stale)
                assert refused.value.code == "gateway_attempt_fenced"

    asyncio.run(scenario())
