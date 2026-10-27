"""G2 Store v11：冻结 v10 升级与 gateway 候选/激活 CAS。"""
from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

from easel.control.store import CURRENT_SCHEMA_VERSION, Store
from easel.control.types import WorkerHandle

ROOT = Path(__file__).resolve().parents[2]
V10_FIXTURE = ROOT / "tests" / "fixtures" / "control-schema-v10.sql"
WORKSPACE = "a" * 32
WORKER = "b" * 64
GATEWAY = "c" * 64
BOOT = "d" * 32
NEW_BOOT = "e" * 32


def _v10_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(V10_FIXTURE.read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO users (id, display_name, role, created_at)"
            " VALUES ('user-old', '旧成员', 'member', 900)")
        connection.execute(
            "INSERT INTO workspaces"
            " (id, owner_user_id, state, config_version, created_at)"
            " VALUES ('ws-old', 'user-old', 'ready', 3, 900)")
        connection.execute(
            "INSERT INTO worker_leases"
            " (workspace_id, container_id, config_version, state,"
            "  heartbeat_at, started_at, owner_id, generation,"
            "  lease_expires_at, updated_at, last_activity_at,"
            "  active_requests, upstream, service_token)"
            " VALUES (?, ?, 3, 'ready', 900, 900, 'old-owner', 2, 1200,"
            "         900, 900, 0, 'http://10.0.0.2:7860', 'old-token')",
            ("ws-old", WORKER))
        connection.commit()
    finally:
        connection.close()


def _handle(*, boot=BOOT, state="available") -> WorkerHandle:
    return WorkerHandle(
        "ws-old", WORKER, "http://10.0.0.2:7860", "worker-token", 3,
        gateway_instance_id=GATEWAY, gateway_generation=boot,
        gateway_upstream="http://172.31.0.5:8897", gateway_state=state)


def test_frozen_v10_database_upgrades_without_gateway_assumption(tmp_path):
    path = tmp_path / "v10.sqlite3"
    _v10_database(path)

    with Store.open(path) as store:
        lease = store.get_worker_lease("ws-old")
        assert lease is not None
        assert lease.container_id == WORKER
        assert lease.service_token == "old-token"
        assert lease.upstream == "http://10.0.0.2:7860"
        assert lease.gateway_instance_id == ""
        assert lease.gateway_generation == ""
        assert lease.gateway_upstream == ""
        assert lease.gateway_state == "disabled"
        assert lease.cleanup_purpose == ""
        assert lease.cleanup_scope == ""
        assert lease.cleanup_worker_created is False
        assert lease.cleanup_gateway_created is False

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == \
            CURRENT_SCHEMA_VERSION
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 11"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_initial_gateway_candidate_is_staged_then_activated(tmp_path):
    path = tmp_path / "db"
    with Store.open(path) as store:
        user = store.create_user("A")
        workspace = user.workspace_id
        lease = store.claim_worker_start(workspace, 3, "owner", 1000, 60)[1]
        assert lease is not None

        handle = dataclasses.replace(
            _handle(state="staged"), workspace_id=workspace)
        assert store.stage_worker_gateway_start(
            workspace, "owner", lease.generation, handle,
            "a" * 32, 1001) is True
        staged = store.get_worker_lease(workspace)
        assert staged.state == "starting"
        assert staged.gateway_instance_id == GATEWAY
        assert staged.gateway_state == "staged"

        assert store.complete_worker_start(
            workspace, "owner", lease.generation,
            dataclasses.replace(handle, gateway_state="available"),
            1002, 60) is True
        ready = store.get_worker_lease(workspace)
        assert ready.state == "ready"
        assert ready.gateway_generation == BOOT
        assert ready.gateway_state == "available"


def test_gateway_refresh_cas_keeps_runs_and_stale_activation_cannot_win(
        tmp_path):
    path = tmp_path / "db"
    with Store.open(path) as store:
        user = store.create_user("A")
        workspace = user.workspace_id
        conversation = store.create_conversation(user)
        run = store.reserve_run(user, conversation.id, "request", "digest", 3)
        store.mark_running(user, run.id)
        lease = store.claim_worker_start(workspace, 3, "owner", 1000, 60)[1]
        assert lease is not None
        assert store.complete_worker_start(
            workspace, "owner", lease.generation,
            dataclasses.replace(_handle(), workspace_id=workspace), 1001, 60)

        candidate = dataclasses.replace(
            _handle(boot=NEW_BOOT, state="staged"), workspace_id=workspace)
        refreshed = store.begin_worker_gateway_refresh(
            workspace, lease.generation, "new-owner", candidate,
            "b" * 32, 1002, 60)
        assert refreshed is not None
        assert refreshed.state == "starting"
        assert refreshed.generation == lease.generation + 1
        assert refreshed.gateway_generation == NEW_BOOT
        assert refreshed.gateway_state == "staged"
        assert store.get_run(user, run.id).status == "running"

        # 旧 generation 的迟到完成/失败都不能碰新候选。
        assert store.complete_worker_gateway_refresh(
            workspace, "owner", lease.generation,
            dataclasses.replace(candidate, gateway_state="available"),
            1003, 60) is False
        assert store.fail_worker_gateway_refresh(
            workspace, "owner", lease.generation, 1003) is False

        assert store.fail_worker_gateway_refresh(
            workspace, "new-owner", refreshed.generation, 1004) is True
        unavailable = store.get_worker_lease(workspace)
        assert unavailable.state == "ready"
        assert unavailable.gateway_state == "unavailable"
        assert unavailable.container_id == WORKER
        assert unavailable.service_token == "worker-token"
        assert store.get_run(user, run.id).status == "running"

        second = store.begin_worker_gateway_refresh(
            workspace, unavailable.generation, "third-owner", candidate,
            "c" * 32, 1005, 60)
        assert second is not None
        assert store.complete_worker_gateway_refresh(
            workspace, "third-owner", second.generation,
            dataclasses.replace(candidate, gateway_state="available"),
            1006, 60) is True
        assert store.get_worker_lease(workspace).gateway_state == "available"


def test_stop_clears_gateway_identity_only_after_terminal_write(tmp_path):
    path = tmp_path / "db"
    with Store.open(path) as store:
        user = store.create_user("A")
        workspace = user.workspace_id
        lease = store.claim_worker_start(workspace, 3, "owner", 1000, 60)[1]
        assert lease is not None
        assert store.complete_worker_start(
            workspace, "owner", lease.generation,
            dataclasses.replace(_handle(), workspace_id=workspace), 1001, 60)
        assert store.mark_worker_draining(
            workspace, 1002, generation=lease.generation) is True
        assert store.begin_worker_stop(
            workspace, 1003, generation=lease.generation) is True
        assert store.stop_worker(
            workspace, 1004, generation=lease.generation,
            expected_state="stopping") is True
        stopped = store.get_worker_lease(workspace)
        assert stopped.state == "stopped"
        assert stopped.gateway_instance_id == ""
        assert stopped.gateway_generation == ""
        assert stopped.gateway_upstream == ""
        assert stopped.gateway_state == "disabled"
