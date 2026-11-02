"""R4-R3：Stop 清理不得撤销已持久化的旧请求执行围栏。

主审反例：A 被 provisioner 精确围栏后，B 的正常记录/commit 继承围栏；
正式 Stop(B) 的 ``_clear_attempt`` 删除整个 attempt 文件，重启后 A 又越过
准入并写出 ``A/pending``。物理资源已清理只证明 B 不再需要清理承诺，
不能证明 A 不会迟到。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from tests.multiuser import test_attempt_ownership_503 as h

WORKSPACE = h.WORKSPACE
WORKER = h.WORKER
GATEWAY = h.GATEWAY
CONFIG_VERSION = h.CONFIG_VERSION
provisioner = h.provisioner


def _commit(service, attempt_id):
    asyncio.run(service.commit_attempt(
        WORKSPACE, WORKER, GATEWAY, attempt_id=attempt_id))


def _stop(service, *, worker=WORKER, gateway=GATEWAY):
    async def missing(*args):
        return None

    async def no_sidecar(*args):
        pass

    service._inspect_raw = missing
    service._stop_sidecar = no_sidecar
    asyncio.run(service.stop(
        WORKSPACE, worker, gateway, protocol_version=2))


def _fence_then_finish_new_attempt(service):
    """A 被围栏，B 正常记录并 commit，记录继承 A 围栏。"""
    old, new = "a" * 32, "b" * 32
    _commit(service, old)
    h._record(service, new, worker_created=False, gateway_created=True)
    _commit(service, new)
    record = service._load_attempt(WORKSPACE)
    assert record["fenced_attempts"] == [old]
    assert record["attempt_id"] == new
    assert record["status"] == "committed"
    return old, new


def test_stop_preserves_fence_and_blocks_late_ensure_after_restart(tmp_path):
    gateway = h.FakeGateway()
    service = h._service(tmp_path, gateway)
    old, new = _fence_then_finish_new_attempt(service)

    _stop(service)

    # 物理清理仍执行；记录改写为无资源语义，但保留执行权事实。
    assert gateway.stops == [(WORKSPACE, WORKER, GATEWAY)]
    kept = service._load_attempt(WORKSPACE)
    assert kept is not None
    assert kept["status"] == "completed"
    assert kept["scope"] == "none"
    assert kept["worker_created"] is False
    assert kept["gateway_created"] is False
    # 旧 A 保留原围栏；已停止实例的 attempt B 也围栏，防止迟到复活。
    assert kept["fenced_attempts"] == [old, new]
    for stale in (old, new):
        with pytest.raises(provisioner.ProvisionerError) as refused:
            service._ensure_attempt_available(WORKSPACE, stale)
        assert refused.value.code == "gateway_attempt_fenced"

    # 重启后正式外部入口必须在 prepare_workspace 之前拒绝 A。
    restarted = h._service(tmp_path, gateway)

    class CrossedSideEffectBoundary(Exception):
        pass

    def side_effect(*args, **kwargs):
        raise CrossedSideEffectBoundary()

    with patch.object(provisioner, "prepare_workspace", side_effect):
        with pytest.raises(provisioner.ProvisionerError) as ensure_refused:
            asyncio.run(restarted.ensure(
                WORKSPACE, CONFIG_VERSION, protocol_version=2,
                attempt_id=old))
        assert ensure_refused.value.code == "gateway_attempt_fenced"
    with pytest.raises(provisioner.ProvisionerError) as restore_refused:
        asyncio.run(restarted.restore_gateway(
            WORKSPACE, WORKER, CONFIG_VERSION, attempt_id=old))
    assert restore_refused.value.code == "gateway_attempt_fenced"

    probe_gateway = h.FakeGateway()
    probe_gateway.ensure_calls = 0
    restarted._gateway = probe_gateway
    with pytest.raises(provisioner.ProvisionerError) as gateway_refused:
        asyncio.run(restarted._with_gateway(
            h._handle(), 2, worker_created=False, attempt_id=old))
    assert gateway_refused.value.code == "gateway_attempt_fenced"
    assert probe_gateway.ensure_calls == 0

    record = restarted._load_attempt(WORKSPACE)
    assert record["attempt_id"] != old
    assert record["status"] != "pending"
    assert old in record["fenced_attempts"]


def test_stop_fence_does_not_block_new_attempt_after_restart(tmp_path):
    gateway = h.FakeGateway()
    service = h._service(tmp_path, gateway)
    old, new = _fence_then_finish_new_attempt(service)
    _stop(service)

    restarted = h._service(tmp_path, gateway)
    fresh = "c" * 32
    restarted._ensure_attempt_available(WORKSPACE, fresh)
    handle = asyncio.run(restarted._with_gateway(
        h._handle(), 2, worker_created=False, attempt_id=fresh))
    assert handle.gateway_instance_id == GATEWAY
    assert gateway.ensure_calls == 1
    record = restarted._load_attempt(WORKSPACE)
    assert record["attempt_id"] == fresh
    assert record["status"] == "completed"
    assert record["scope"] == "none"
    # 新 attempt 的正常记录继承旧围栏，旧 A/已停止 B 仍被拒绝。
    assert record["fenced_attempts"] == [old, new]
    for stale in (old, new):
        with pytest.raises(provisioner.ProvisionerError) as refused:
            restarted._ensure_attempt_available(WORKSPACE, stale)
        assert refused.value.code == "gateway_attempt_fenced"


def test_stop_without_fence_still_deletes_record(tmp_path):
    """无围栏的普通清理保持原行为：物理清理后删除记录且幂等。"""
    service = h._service(tmp_path)
    h._record(service, "b" * 32, worker_created=False, gateway_created=True)
    _stop(service)
    assert service._load_attempt(WORKSPACE) is None
    _stop(service)
    assert service._load_attempt(WORKSPACE) is None


def test_stop_fence_record_is_idempotent(tmp_path):
    gateway = h.FakeGateway()
    service = h._service(tmp_path, gateway)
    old, new = _fence_then_finish_new_attempt(service)
    _stop(service)
    first = service._load_attempt(WORKSPACE)
    _stop(service)
    second = service._load_attempt(WORKSPACE)
    assert first == second
    assert second["fenced_attempts"] == [old, new]


def test_stop_instance_mismatch_keeps_fence_record(tmp_path):
    """精确实例错配时 Stop 不得改动携带围栏的记录。"""
    service = h._service(tmp_path)
    old, new = _fence_then_finish_new_attempt(service)
    _stop(service, worker="d" * 64)
    record = service._load_attempt(WORKSPACE)
    assert record is not None
    assert record["fenced_attempts"] == [old]
    assert record["worker_container_id"] == WORKER


def test_abort_completion_preserves_fence(tmp_path):
    """物理 abort 收口为 completed 时必须携带围栏。"""
    gateway = h.FakeGateway()
    service = h._service(tmp_path, gateway)
    old, new = "a" * 32, "b" * 32
    _commit(service, old)
    h._record(service, new, worker_created=False, gateway_created=True)

    asyncio.run(service.abort(
        WORKSPACE, WORKER, GATEWAY, attempt_id=new, scope="gateway",
        worker_created=False, gateway_created=True))

    assert gateway.stops == [(WORKSPACE, WORKER, GATEWAY)]
    record = service._load_attempt(WORKSPACE)
    assert record["status"] == "completed"
    assert record["fenced_attempts"] == [old]
    with pytest.raises(provisioner.ProvisionerError) as refused:
        service._ensure_attempt_available(WORKSPACE, old)
    assert refused.value.code == "gateway_attempt_fenced"


def test_recover_abort_preserves_fence(tmp_path):
    """旧坏记录恢复收口为 completed 时不得丢失围栏。"""
    gateway = h.FakeGateway()
    service = h._service(tmp_path, gateway)
    old, legacy, requested = "a" * 32, "e" * 32, "b" * 32
    h._write_attempt(tmp_path, {
        "attempt_id": legacy,
        "worker_container_id": WORKER,
        "gateway_instance_id": GATEWAY,
        "worker_created": False,
        "gateway_created": False,
        "scope": "gateway",
        "status": "pending",
        "fenced_attempts": [old],
    })
    async def worker_inspect(instance_id):
        return h._worker_inspect() if instance_id == WORKER else None

    service._inspect_instance = worker_inspect

    asyncio.run(service.recover_abort(
        WORKSPACE, WORKER, GATEWAY, CONFIG_VERSION,
        attempt_id=requested, scope="gateway",
        worker_created=False, gateway_created=True))

    assert gateway.stops == [(WORKSPACE, WORKER, GATEWAY)]
    record = service._load_attempt(WORKSPACE)
    assert record["status"] == "completed"
    assert record["fenced_attempts"] == [old]
    with pytest.raises(provisioner.ProvisionerError) as refused:
        service._ensure_attempt_available(WORKSPACE, old)
    assert refused.value.code == "gateway_attempt_fenced"
