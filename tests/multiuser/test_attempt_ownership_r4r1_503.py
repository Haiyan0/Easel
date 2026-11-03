"""R4-R1：控制进程在远程副作用后直接退出，重启按精确 attempt 恢复。

本文件使用独立控制子进程并调用 ``os._exit``，不经过 runtime 的异常或
取消处理；不连接 Docker，只使用临时目录、假 ID 与内存 gateway fake。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
import subprocess
import sys

import pytest

from tests.multiuser import test_attempt_ownership_503 as helpers

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKER = helpers.WORKER
GATEWAY = helpers.GATEWAY
FakeGateway = helpers.FakeGateway
RuntimeManager = helpers.RuntimeManager
Store = helpers.Store
WorkerHandle = helpers.WorkerHandle


_R4R1_CHILD = r'''
import asyncio
import dataclasses
import json
import os
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
directory = pathlib.Path(sys.argv[2]).resolve()
mode = sys.argv[3]
sys.path.insert(0, str(root))
from tests.multiuser import test_attempt_ownership_503 as h

REPLACEMENT = "f" * 64
CRASH_AT = 1201


def _candidate(handle, attempt_id):
    return dataclasses.replace(
        handle, gateway_instance_id=REPLACEMENT,
        gateway_generation="e" * 32,
        gateway_upstream="http://172.31.0.9:8897",
        gateway_state="available", attempt_id=attempt_id,
        worker_created=False, gateway_created=True)


async def _run():
    store, service, gateway, workspace, lease, handle, original = \
        h._seed_expired_ensure(directory)
    with store:
        ready = await h._make_normal_ready(store, service, workspace, handle)
        (directory / "meta.json").write_text(json.dumps({
            "workspace": workspace,
            "handle": dataclasses.asdict(handle),
            "old_attempt": ready.attempt_id,
            "old_generation": ready.generation,
            "crashed_at": CRASH_AT,
        }), encoding="utf-8")

        class Adapter:
            async def commit_attempt(self, *args, **kwargs):
                return await service.commit_attempt(*args, **kwargs)

            async def abort(self, *args, **kwargs):
                return await service.abort(*args, **kwargs)

            async def inspect(self, workspace_id):
                return await h._ready_inspect(workspace_id)

            async def ensure(self, workspace_id, config_version, *,
                             attempt_id="", orphan_takeover=False):
                if mode != "ensure-before-remote":
                    service._record_attempt(
                        workspace_id, attempt_id,
                        _candidate(handle, attempt_id),
                        worker_created=False, gateway_created=True)
                # 真实子进程直接退出：不经过 runtime 的异常/取消处理。
                os._exit(73)

            async def restore_gateway(self, workspace_id, worker_container_id,
                                      config_version, *, attempt_id=""):
                service._record_attempt(
                    workspace_id, attempt_id,
                    _candidate(handle, attempt_id),
                    worker_created=False, gateway_created=True)
                os._exit(73)

        manager = h.RuntimeManager(
            store, Adapter(), owner_id="owner-b", gateway_required=True,
            clock=lambda: CRASH_AT)
        if mode == "restore-crash":
            await manager.restore_gateway(workspace, h.WORKER, 1)
        else:
            await manager.ensure_ready(workspace, 1)
        raise AssertionError("子进程未退出")


asyncio.run(_run())
'''


def _run_child(tmp_path, mode):
    """在独立控制子进程中执行一次刷新并直接退出，返回崩溃现场。"""
    script = tmp_path / "r4r1_child.py"
    script.write_text(_R4R1_CHILD, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), str(ROOT), str(tmp_path), mode],
        capture_output=True, text=True, timeout=180)
    assert result.returncode == 73, (
        result.returncode, result.stdout, result.stderr)
    return json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))


def _stop_and_assert(service, gateway, workspace, placement):
    async def no_worker(workspace_id):
        return None

    async def no_sidecar(workspace_id):
        return None

    service._inspect_raw = no_worker
    service._stop_sidecar = no_sidecar
    asyncio.run(service.stop(
        workspace, WORKER, placement, protocol_version=2))
    assert gateway.stops == [(workspace, WORKER, placement)]


def test_ready_refresh_process_exit_after_remote_recovers(tmp_path):
    """正常 ready 刷新在远端写 G2/pending 后进程直接退出，重启接回 G2。"""
    meta = _run_child(tmp_path, "ensure-crash")
    workspace = meta["workspace"]
    replacement = "f" * 64
    with Store.open(tmp_path / "control.sqlite3") as store:
        crashed = store.get_worker_lease(workspace)
        assert crashed is not None
        # 远程调用前已持久本轮精确 attempt，而不是残留旧 ready/旧 attempt。
        assert crashed.state == "starting"
        assert crashed.attempt_id != meta["old_attempt"]
        assert crashed.generation > meta["old_generation"]
        assert crashed.lease_expires_at <= meta["crashed_at"]
        # 旧 owner 的迟到写回不得命中新代次。
        assert store.mark_worker_gateway_unavailable(
            workspace, meta["old_generation"],
            meta["crashed_at"] + 1) is False
        record = helpers._service(tmp_path)._load_attempt(workspace)
        assert record is not None
        assert record["attempt_id"] == crashed.attempt_id
        assert record["status"] == "pending"
        assert record["gateway_instance_id"] == replacement
        recovery_clock = crashed.lease_expires_at + 1

    gateway = FakeGateway(config_version=1)
    service = helpers._service(tmp_path, gateway)
    adapter = helpers._lost_response_adapter(
        service, WorkerHandle(**meta["handle"]), replacement, {"count": 0})
    counted = helpers._wrap_adapter_calls(adapter)
    with Store.open(tmp_path / "control.sqlite3") as store:
        manager = RuntimeManager(
            store, adapter, owner_id="owner-c", gateway_required=True,
            clock=lambda: recovery_clock)
        final = asyncio.run(manager.ensure_ready(workspace, 1))
        assert final.gateway_instance_id == replacement
        ready = store.get_worker_lease(workspace)
        assert ready is not None
        assert ready.state == "ready"
        assert ready.gateway_instance_id == replacement
        assert ready.attempt_id == final.attempt_id
        assert ready.pending_attempt_id == ""
        _stop_and_assert(service, gateway, workspace, replacement)
    # 恢复直接接管 provisioner 已持久化的 G2，不重复创建 worker/gateway。
    assert counted == {"ensure": 0, "restore": 0, "resolve": 1}


def test_ready_restore_process_exit_after_remote_recovers(tmp_path):
    """显式 restore 同一崩溃窗口：两入口按同一精确 attempt 归属恢复。"""
    meta = _run_child(tmp_path, "restore-crash")
    workspace = meta["workspace"]
    replacement = "f" * 64
    with Store.open(tmp_path / "control.sqlite3") as store:
        crashed = store.get_worker_lease(workspace)
        assert crashed is not None
        assert crashed.state == "starting"
        assert crashed.attempt_id != meta["old_attempt"]
        assert crashed.lease_expires_at <= meta["crashed_at"]
        record = helpers._service(tmp_path)._load_attempt(workspace)
        assert record is not None
        assert record["attempt_id"] == crashed.attempt_id
        assert record["status"] == "pending"
        assert record["gateway_instance_id"] == replacement
        recovery_clock = crashed.lease_expires_at + 1

    gateway = FakeGateway(config_version=1)
    service = helpers._service(tmp_path, gateway)
    adapter = helpers._lost_response_adapter(
        service, WorkerHandle(**meta["handle"]), replacement, {"count": 0})
    counted = helpers._wrap_adapter_calls(adapter)
    with Store.open(tmp_path / "control.sqlite3") as store:
        manager = RuntimeManager(
            store, adapter, owner_id="owner-c", gateway_required=True,
            clock=lambda: recovery_clock)
        final = asyncio.run(manager.restore_gateway(workspace, WORKER, 1))
        assert final is not None
        assert final.gateway_instance_id == replacement
        ready = store.get_worker_lease(workspace)
        assert ready is not None
        assert ready.state == "ready"
        assert ready.gateway_instance_id == replacement
        assert ready.pending_attempt_id == ""
        _stop_and_assert(service, gateway, workspace, replacement)
    assert counted == {"ensure": 0, "restore": 0, "resolve": 1}


def test_ready_refresh_process_exit_before_remote_recovers_g1(tmp_path):
    """本轮 attempt 已持久但远程尚未执行即退出：可恢复且不误删可信 G1。"""
    meta = _run_child(tmp_path, "ensure-before-remote")
    workspace = meta["workspace"]
    with Store.open(tmp_path / "control.sqlite3") as store:
        crashed = store.get_worker_lease(workspace)
        assert crashed is not None
        # 远程未执行，但预占已持久；不能永久停留在 starting。
        assert crashed.state == "starting"
        assert crashed.attempt_id != meta["old_attempt"]
        assert crashed.lease_expires_at <= meta["crashed_at"]
        # provisioner 没有任何本轮 attempt 记录（远端未执行）。
        record = helpers._service(tmp_path)._load_attempt(workspace)
        assert record is not None
        assert record["attempt_id"] != crashed.attempt_id
        recovery_clock = crashed.lease_expires_at + 1

    gateway = FakeGateway(config_version=1)
    service = helpers._service(tmp_path, gateway)
    adapter = helpers._lost_response_adapter(
        service, WorkerHandle(**meta["handle"]), GATEWAY, {"count": 0})
    counted = helpers._wrap_adapter_calls(adapter)
    with Store.open(tmp_path / "control.sqlite3") as store:
        manager = RuntimeManager(
            store, adapter, owner_id="owner-c", gateway_required=True,
            clock=lambda: recovery_clock)
        final = asyncio.run(manager.ensure_ready(workspace, 1))
        assert final.container_id == WORKER
        assert final.gateway_instance_id == GATEWAY
        ready = store.get_worker_lease(workspace)
        assert ready is not None
        assert ready.state == "ready"
        assert ready.pending_attempt_id == ""
        assert ready.gateway_instance_id == GATEWAY
        # 可信 G1 未被删除；恢复只重新执行一次原本未发出的远程 ensure。
        assert gateway.stops == []
    assert counted == {"ensure": 1, "restore": 0, "resolve": 0}


def test_ready_refresh_definite_failure_rolls_back_reservation(tmp_path):
    """确定失败撤销预占：恢复 ready/旧 attempt，不把刷新滞留在 starting。"""
    replacement = "f" * 64

    async def scenario():
        store, service, gateway, workspace, lease, handle, original = \
            helpers._seed_expired_ensure(tmp_path)
        with store:
            ready = await helpers._make_normal_ready(
                store, service, workspace, handle)
            old_attempt = ready.attempt_id
            old_generation = ready.generation
            adapter = helpers._lost_response_adapter(
                service, handle, replacement, {"count": 0})
            adapter.inspect = helpers._ready_inspect

            async def ensure(*args, **kwargs):
                raise helpers.RuntimeUnavailable(
                    "worker_start_failed", 503, retry_after=5)

            adapter.ensure = ensure
            manager = RuntimeManager(
                store, adapter, owner_id="owner-c", gateway_required=True,
                clock=lambda: 1201)
            with pytest.raises(helpers.RuntimeUnavailable):
                await manager.ensure_ready(workspace, 1)
            failed = store.get_worker_lease(workspace)
            assert failed is not None
            assert failed.state == "ready"
            assert failed.attempt_id == old_attempt
            assert failed.gateway_state == "unavailable"
            assert failed.pending_attempt_id == ""
            assert failed.generation > old_generation
            assert failed.lease_expires_at > 1201
            # 预占已撤销，旧代次迟到写回同样不得命中新代次。
            assert store.mark_worker_gateway_unavailable(
                workspace, old_generation, 1201) is False

    asyncio.run(scenario())


def test_ready_restore_definite_failure_rolls_back_reservation(tmp_path):
    """显式 restore 确定失败同样撤销预占，保留既有 worker 与 ready。"""
    replacement = "f" * 64

    async def scenario():
        store, service, gateway, workspace, lease, handle, original = \
            helpers._seed_expired_ensure(tmp_path)
        with store:
            ready = await helpers._make_normal_ready(
                store, service, workspace, handle)
            old_attempt = ready.attempt_id
            adapter = helpers._lost_response_adapter(
                service, handle, replacement, {"count": 0})

            async def restore_gateway(*args, **kwargs):
                raise helpers.RuntimeUnavailable(
                    "gateway_recovery_failed", 503, retry_after=5)

            adapter.restore_gateway = restore_gateway
            manager = RuntimeManager(
                store, adapter, owner_id="owner-c", gateway_required=True,
                clock=lambda: 1201)
            with pytest.raises(helpers.RuntimeUnavailable):
                await manager.restore_gateway(workspace, WORKER, 1)
            failed = store.get_worker_lease(workspace)
            assert failed is not None
            assert failed.state == "ready"
            assert failed.attempt_id == old_attempt
            assert failed.gateway_state == "unavailable"
            assert failed.pending_attempt_id == ""
            assert failed.container_id == WORKER
            assert gateway.stops == []

    asyncio.run(scenario())
