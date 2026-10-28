"""A4 R1-05：跨进程停止接纳屏障与 drain 终态复核。"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from easel.control import cli
from easel.control.runtime import RuntimeManager, RuntimeUnavailable
from easel.control.store import Store
from easel.control.types import DomainError, WorkerHandle

PROJECT = "easel-workspace-desktop"


class StopProvisioner:
    def __init__(self) -> None:
        self.starts = 0
        self.stops: list[str] = []
        self.handles: dict[str, WorkerHandle] = {}

    async def ensure(self, workspace_id: str, config_version: int) -> WorkerHandle:
        self.starts += 1
        handle = WorkerHandle(
            workspace_id, "a" * 64, "http://10.0.0.2:7860",
            "service-token", config_version)
        self.handles[workspace_id] = handle
        return handle

    async def stop(self, workspace_id: str, instance_id: str) -> None:
        self.stops.append(workspace_id)
        self.handles.pop(workspace_id, None)


def _run(coro):
    return asyncio.run(coro)


def test_barrier_blocks_reservation_worker_and_new_workspace_across_stores(
        tmp_path):
    path = tmp_path / "db"
    with Store.open(path) as first, Store.open(path) as second:
        admin = first.create_user("admin", "admin")
        user = first.create_user("A")
        conversation = first.create_conversation(user, "stop")
        barrier = first.set_admission_barrier("stop-1", "desktop-stop", 1000)
        assert barrier["id"] == "stop-1"
        assert second.admission_barrier()["id"] == "stop-1"

        with pytest.raises(DomainError) as rejected:
            second.reserve_run(
                user, conversation.id, "turn", "digest", 0)
        assert (rejected.value.code, rejected.value.status) == (
            "admission_closed", 503)
        assert first.count_active_runs() == 0

        with pytest.raises(DomainError) as rejected:
            second.claim_worker_start(
                user.workspace_id, 0, "owner-b", 1000, 60)
        assert rejected.value.code == "admission_closed"
        assert second.get_worker_lease(user.workspace_id) is None

        with pytest.raises(DomainError) as rejected:
            second.create_user("B")
        assert rejected.value.code == "admission_closed"
        with pytest.raises(DomainError) as rejected:
            second.admin_create_member(admin, "C")
        assert rejected.value.code == "admission_closed"

        assert first.clear_admission_barrier("stop-1") is True
        assert second.admission_barrier() is None
        run = second.reserve_run(user, conversation.id, "turn", "digest", 0)
        assert run.status == "reserved"
        outcome, lease = second.claim_worker_start(
            user.workspace_id, 0, "owner-b", 1001, 60)
        assert outcome == "started"
        assert lease is not None


def test_drain_all_stops_worker_and_barrier_blocks_reopen_from_second_store(
        tmp_path):
    path = tmp_path / "db"

    async def first_scenario():
        with Store.open(path) as store:
            user = store.create_user("A")
            provisioner = StopProvisioner()
            manager = RuntimeManager(
                store, provisioner, poll_interval=0.01, drain_timeout=1)
            await manager.ensure_ready(user.workspace_id, 0)
            store.set_admission_barrier("stop-1", "desktop-stop", 1000)
            drained = await manager.drain_all()
            assert drained == [user.workspace_id]
            assert provisioner.stops == [user.workspace_id]
            lease = store.get_worker_lease(user.workspace_id)
            assert lease is not None and lease.state == "stopped"
            return user.workspace_id

    workspace_id = _run(first_scenario())

    async def second_scenario():
        with Store.open(path) as store:
            provisioner = StopProvisioner()
            manager = RuntimeManager(store, provisioner)
            with pytest.raises(DomainError) as rejected:
                await manager.ensure_ready(workspace_id, 0)
            assert rejected.value.code == "admission_closed"
            assert provisioner.starts == 0
            lease = store.get_worker_lease(workspace_id)
            assert lease is not None and lease.state == "stopped"

    _run(second_scenario())


def test_drain_all_does_not_report_success_while_stopping(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            provisioner = StopProvisioner()
            manager = RuntimeManager(
                store, provisioner, poll_interval=0.01, drain_timeout=0.05)
            await manager.ensure_ready(user.workspace_id, 0)
            lease = store.get_worker_lease(user.workspace_id)
            assert lease is not None
            assert store.mark_worker_draining(
                user.workspace_id, int(lease.heartbeat_at),
                generation=lease.generation) is True
            assert store.begin_worker_stop(
                user.workspace_id, int(lease.heartbeat_at),
                generation=lease.generation) is True
            store.set_admission_barrier("stop-1", "desktop-stop", 1000)
            with pytest.raises(RuntimeUnavailable) as rejected:
                await manager.drain_all()
            assert rejected.value.code in (
                "worker_drain_incomplete", "worker_drain_timeout")
            assert provisioner.stops == []
            current = store.get_worker_lease(user.workspace_id)
            assert current is not None and current.state == "stopping"

    _run(scenario())


def test_completed_stop_reopens_twice_with_exact_fence(tmp_path):
    with Store.open(tmp_path / "db") as store:
        first = store.set_admission_barrier("stop-1", "desktop-stop", 1000)
        assert first["id"] == "stop-1"
        assert store.complete_stop(
            "stop-1", PROJECT, "a" * 64, 1001)["barrierId"] == "stop-1"
        assert store.reopen_after_completed_stop(
            "stop-1", PROJECT, "a" * 64, 1002) is True
        assert store.admission_barrier() is None

        store.set_admission_barrier("stop-2", "desktop-stop", 2000)
        store.complete_stop("stop-2", PROJECT, "a" * 64, 2001)
        assert store.reopen_after_completed_stop(
            "stop-2", PROJECT, "a" * 64, 2002) is True
        assert store.admission_barrier() is None


def test_failed_stop_barrier_and_stale_completion_cannot_reopen(tmp_path):
    with Store.open(tmp_path / "db") as store:
        store.set_admission_barrier("stop-1", "desktop-stop", 1000)
        assert store.reopen_after_completed_stop(
            "stop-1", PROJECT, "a" * 64, 1001) is False
        assert store.admission_barrier()["id"] == "stop-1"
        store.complete_stop("stop-1", PROJECT, "a" * 64, 1002)
        assert store.reopen_after_completed_stop(
            "stop-1", PROJECT, "b" * 64, 1003) is False
        assert store.reopen_after_completed_stop(
            "stop-1", "other-project", "a" * 64, 1003) is False
        assert store.reopen_after_completed_stop(
            "stop-0", PROJECT, "a" * 64, 1003) is False
        assert store.admission_barrier()["id"] == "stop-1"

        # 新 Stop 建立新屏障后，旧完成记录不能清除新屏障。
        assert store.clear_admission_barrier("stop-1") is True
        store.set_admission_barrier("stop-2", "desktop-stop", 2000)
        assert store.reopen_after_completed_stop(
            "stop-1", PROJECT, "a" * 64, 2001) is False
        assert store.admission_barrier()["id"] == "stop-2"


def test_complete_stop_refuses_when_active_work_remains(tmp_path):
    with Store.open(tmp_path / "db") as store:
        user = store.create_user("A")
        conversation = store.create_conversation(user, "stop")
        run = store.reserve_run(
            user, conversation.id, "turn", "digest", 0)
        store.set_admission_barrier("stop-1", "desktop-stop", 1000)
        with pytest.raises(DomainError) as rejected:
            store.complete_stop("stop-1", PROJECT, "a" * 64, 1001)
        assert rejected.value.code == "worker_drain_incomplete"
        assert store.admission_barrier()["id"] == "stop-1"
        assert store.get_run(user, run.id).status == "reserved"


def test_admin_stop_complete_and_reopen_cli_fence(tmp_path, capsys):
    control = tmp_path / "control"
    control.mkdir()
    with Store.open(control / "control.sqlite3") as store:
        store.set_admission_barrier("stop-1", "desktop-stop", 1000)
    complete = SimpleNamespace(
        control_dir=str(control), barrier_id="stop-1", project=PROJECT,
        config_sha256="a" * 64)
    assert cli.cmd_admin_stop_complete(complete) == 0
    assert "stop_completed=stop-1" in capsys.readouterr().out
    with Store.open(control / "control.sqlite3") as store:
        assert store.admission_barrier()["id"] == "stop-1"

    reopen = SimpleNamespace(
        control_dir=str(control), barrier_id="stop-1", project=PROJECT,
        config_sha256="a" * 64)
    assert cli.cmd_admin_reopen(reopen) == 0
    assert "reopened=stop-1" in capsys.readouterr().out

    failed = SimpleNamespace(
        control_dir=str(control), barrier_id="stop-1", project=PROJECT,
        config_sha256="a" * 64)
    assert cli.cmd_admin_reopen(failed) == 0
    assert "barrier=none" in capsys.readouterr().out


def test_admin_reopen_refuses_failure_barrier(tmp_path, capsys):
    control = tmp_path / "control"
    control.mkdir()
    with Store.open(control / "control.sqlite3") as store:
        store.set_admission_barrier("stop-1", "desktop-stop", 1000)
    reopen = SimpleNamespace(
        control_dir=str(control), barrier_id="stop-1", project=PROJECT,
        config_sha256="a" * 64)
    assert cli.cmd_admin_reopen(reopen) == 1
    assert "admission_barrier_not_reopenable" in capsys.readouterr().err
    with Store.open(control / "control.sqlite3") as store:
        assert store.admission_barrier()["id"] == "stop-1"


def test_corrupt_barrier_fails_closed(tmp_path):
    with Store.open(tmp_path / "db") as store:
        store._connection.execute(
            "INSERT INTO control_state (key, value, updated_at)"
            " VALUES ('admission_barrier', 'null', 1)")
        store._connection.commit()
        with pytest.raises(DomainError) as rejected:
            store.admission_barrier()
        assert rejected.value.code == "admission_barrier_corrupt"


def test_barrier_clear_requires_matching_id(tmp_path):
    with Store.open(tmp_path / "db") as store:
        store.set_admission_barrier("stop-1", "stop", 1000)
        assert store.clear_admission_barrier("other") is False
        assert store.admission_barrier()["id"] == "stop-1"
        assert store.clear_admission_barrier("stop-1") is True
        assert store.admission_barrier() is None


class _DrainProvisioner:
    def __init__(self, socket, **kwargs) -> None:
        self.socket = socket
        self.stops: list[str] = []

    async def stop(self, workspace_id: str, instance_id: str) -> None:
        self.stops.append(workspace_id)


def test_admin_drain_sets_barrier_and_resume_clears_it(tmp_path, monkeypatch,
                                                       capsys):
    control = tmp_path / "control"
    control.mkdir()
    socket_path = tmp_path / "provisioner.sock"
    socket_path.write_text("", encoding="utf-8")
    with Store.open(control / "control.sqlite3") as store:
        user = store.create_user("A")
        now = int(time.time())
        outcome, lease = store.claim_worker_start(
            user.workspace_id, 0, "owner-1", now, 120)
        assert outcome == "started" and lease is not None
        assert store.complete_worker_start(
            user.workspace_id, "owner-1", lease.generation,
            WorkerHandle(user.workspace_id, "a" * 64,
                         "http://worker", "token", 0), now, 120)

    monkeypatch.setattr(cli, "UnixProvisionerClient", _DrainProvisioner)
    args = SimpleNamespace(
        control_dir=str(control), provisioner_socket=str(socket_path),
        gateway_required=False, barrier=True)
    assert cli.cmd_admin_drain(args) == 0
    output = capsys.readouterr().out
    assert "barrier=" in output and "verified=settled" in output
    with Store.open(control / "control.sqlite3") as store:
        barrier = store.admission_barrier()
        assert barrier is not None and barrier["reason"] == "desktop-stop"
        assert store.get_worker_lease(user.workspace_id).state == "stopped"
    resume = SimpleNamespace(control_dir=str(control), barrier_id=None)
    assert cli.cmd_admin_resume(resume) == 0
    with Store.open(control / "control.sqlite3") as store:
        assert store.admission_barrier() is None
