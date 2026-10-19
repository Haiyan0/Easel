"""Task 6：Ed25519 签名快照、归属校验与 run/nonce 接纳。"""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.control.snapshot import load_signing_key
from easel.control.types import DomainError
from easel.worker.config import (
    SnapshotAdmissionStore,
    WorkerConfigSnapshot,
    load_public_key,
    load_worker_snapshot,
    sign_worker_snapshot,
    verify_worker_snapshot,
)


def write_public_key(path: Path, private_key: Ed25519PrivateKey) -> Path:
    path.write_bytes(private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo))
    return path


def write_private_key(path: Path, private_key: Ed25519PrivateKey) -> Path:
    path.write_bytes(private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()))
    return path


def signed(private_key, **overrides):
    values = {
        "workspace_id": "workspace-a",
        "run_id": "run-a",
        "revision": 4,
        "routes": {"broker_url": "http://broker.internal",
                   "broker_token": "run-token"},
        "agent": {"runtime": "acp", "command": ["node", "agent.js"],
                  "model": "deepseek-chat", "sandbox": "read-only"},
        "expires_at": 2000,
        "nonce": "a1b2c3d4",
    }
    values.update(overrides)
    return sign_worker_snapshot(private_key, **values)


def verify(envelope, public_key, **overrides):
    values = {
        "expected_workspace_id": "workspace-a",
        "expected_run_id": "run-a",
        "expected_revision": 4,
        "now": 1000,
    }
    values.update(overrides)
    return verify_worker_snapshot(envelope, public_key, **values)


def test_signed_snapshot_roundtrip_binds_all_fields(tmp_path):
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    envelope = signed(private)
    assert envelope["envelope_version"] == 1
    assert set(envelope) == {
        "envelope_version", "workspace_id", "run_id", "revision",
        "routes", "agent", "expires_at", "nonce", "signature",
    }
    snapshot = verify(envelope, public)
    assert isinstance(snapshot, WorkerConfigSnapshot)
    assert snapshot.workspace_id == "workspace-a"
    assert snapshot.run_id == "run-a"
    assert snapshot.revision == 4
    assert snapshot.routes["broker_token"] == "run-token"
    assert snapshot.agent["model"] == "deepseek-chat"
    assert snapshot.nonce == "a1b2c3d4"
    assert snapshot.expires_at == 2000
    assert signed(private)["signature"] == envelope["signature"]


@pytest.mark.parametrize(("field", "value"), [
    ("workspace_id", "workspace-b"),
    ("run_id", "run-b"),
    ("revision", 5),
    ("routes", {"broker_token": "tampered"}),
    ("agent", {"runtime": "acp", "command": ["node", "evil.js"]}),
    ("expires_at", 2001),
    ("nonce", "deadbeef"),
    ("signature", "AAAA"),
    ("envelope_version", 2),
])
def test_tampered_or_rebound_snapshot_is_rejected(field, value):
    private = Ed25519PrivateKey.generate()
    envelope = signed(private)
    envelope[field] = value
    with pytest.raises(DomainError):
        verify(envelope, private.public_key())


def test_extra_or_missing_envelope_fields_are_rejected():
    private = Ed25519PrivateKey.generate()
    envelope = signed(private)
    envelope["extra"] = "not-signed"
    with pytest.raises(DomainError):
        verify(envelope, private.public_key())
    missing = signed(private)
    missing.pop("nonce")
    with pytest.raises(DomainError):
        verify(missing, private.public_key())


def test_expired_wrong_key_and_wrong_scope_are_rejected():
    private = Ed25519PrivateKey.generate()
    envelope = signed(private, expires_at=1000)
    with pytest.raises(DomainError):
        verify(envelope, private.public_key())
    other = Ed25519PrivateKey.generate()
    with pytest.raises(DomainError):
        verify(signed(private), other.public_key())
    for override in (
            {"expected_workspace_id": "workspace-b"},
            {"expected_run_id": "run-b"},
            {"expected_revision": 3}):
        with pytest.raises(DomainError):
            verify(signed(private), private.public_key(), **override)


def test_signing_rejects_admin_secret_material():
    private = Ed25519PrivateKey.generate()
    with pytest.raises(DomainError):
        signed(private, agent={"runtime": "acp", "OPENAI_API_KEY": "secret"})
    with pytest.raises(DomainError):
        signed(private, routes={"broker_token": "ok", "admin_api_key": "secret"})
    with pytest.raises(DomainError):
        signed(private, agent={"runtime": "acp", "password": "secret"})


def test_worker_loads_only_public_key_and_snapshot_file(tmp_path):
    private = Ed25519PrivateKey.generate()
    envelope = signed(private)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(envelope), encoding="utf-8")
    public_path = write_public_key(tmp_path / "worker.pem", private)
    key = load_public_key(public_path)
    snapshot = load_worker_snapshot(
        snapshot_path, key,
        expected_workspace_id="workspace-a", expected_run_id="run-a",
        expected_revision=4, now=1000)
    assert snapshot.revision == 4
    assert snapshot.agent["command"] == ["node", "agent.js"]
    with pytest.raises(DomainError):
        load_worker_snapshot(
            snapshot_path, key,
            expected_workspace_id="workspace-b", expected_run_id="run-a",
            now=1000)


def test_control_can_load_private_signing_key(tmp_path):
    private = Ed25519PrivateKey.generate()
    path = write_private_key(tmp_path / "control.pem", private)
    loaded = load_signing_key(path)
    envelope = sign_worker_snapshot(
        loaded, workspace_id="workspace-a", run_id="run-a", revision=1,
        routes={"broker_token": "t"}, agent={"runtime": "acp"},
        expires_at=2000, nonce="n1")
    verify(envelope, private.public_key(), expected_revision=1)


# ---------------------------------------------------------------------------
# admission：同一 run/nonce 只启动一次
# ---------------------------------------------------------------------------


def admitted_snapshot() -> WorkerConfigSnapshot:
    return WorkerConfigSnapshot(
        "workspace-a", "run-a", 4,
        {"broker_url": "http://broker.internal", "broker_token": "run-token"},
        {"runtime": "acp", "command": ["node", "agent.js"]},
        nonce="nonce-a", expires_at=2000)


def test_admission_is_persistent_and_returns_original_state(tmp_path):
    database = tmp_path / "state" / "admissions.sqlite3"
    first = SnapshotAdmissionStore(database).admit(admitted_snapshot())
    assert first["duplicate"] is False
    assert first["state"] == "accepted"
    second = SnapshotAdmissionStore(database).admit(admitted_snapshot())
    assert second["duplicate"] is True
    assert second["state"] == "accepted"
    assert second["run_id"] == "run-a"

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE snapshot_admissions SET state = 'running'"
            " WHERE workspace_id = ? AND run_id = ?",
            ("workspace-a", "run-a"))
    third = SnapshotAdmissionStore(database).admit(admitted_snapshot())
    assert third["duplicate"] is True
    assert third["state"] == "running"


def test_same_run_with_different_nonce_or_payload_conflicts(tmp_path):
    store = SnapshotAdmissionStore(tmp_path / "admissions.sqlite3")
    store.admit(admitted_snapshot())
    changed_nonce = WorkerConfigSnapshot(
        "workspace-a", "run-a", 4,
        {"broker_token": "run-token"}, {"runtime": "acp"},
        nonce="nonce-b", expires_at=2000)
    with pytest.raises(DomainError) as conflict:
        store.admit(changed_nonce)
    assert conflict.value.status == 409
    changed_payload = WorkerConfigSnapshot(
        "workspace-a", "run-a", 5,
        {"broker_token": "run-token"}, {"runtime": "acp"},
        nonce="nonce-a", expires_at=2000)
    with pytest.raises(DomainError):
        store.admit(changed_payload)


def test_concurrent_admission_starts_exactly_once(tmp_path):
    database = tmp_path / "admissions.sqlite3"
    SnapshotAdmissionStore(database)  # 建表完成后再并发
    snapshot = admitted_snapshot()
    executed: list[int] = []

    def admit_and_execute(index):
        result = SnapshotAdmissionStore(database).admit(snapshot)
        if not result["duplicate"]:
            executed.append(index)  # 假执行：只有接纳者才启动
        return result

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(admit_and_execute, range(8)))
    assert sum(1 for result in results if not result["duplicate"]) == 1
    assert sum(1 for result in results if result["duplicate"]) == 7
    assert len(executed) == 1
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT COUNT(*) FROM snapshot_admissions").fetchone()[0]
    assert rows == 1
