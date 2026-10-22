"""Task 9 R1：真实 worker HTTP 聊天快照与旧删除入口回归。"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

import web.app as web
from easel.control.types import DomainError, WorkspacePaths
from easel.harnesses.base import HarnessError
from easel.worker.config import sign_worker_snapshot
from easel.worker.guard import WorkerIdentity
from easel.worker.sessions import (
    MAX_ATTACHMENTS,
    MAX_MESSAGE_BYTES,
    WorkspaceSessionStore,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "harness_rpc.py"
WORKSPACE = "workspace-sessions-http"
TOKEN = "service-token-sessions-http"
HEADERS = {"X-Easel-Workspace-Id": WORKSPACE,
           "X-Easel-Service-Token": TOKEN}


def install_worker(tmp_path: Path, monkeypatch, *, run_id: str,
                   nonce: str, command: list[str] | None = None) -> WorkspacePaths:
    """安装一个新的受管 worker 快照；同一 state_root 可跨多轮 Run 复用。"""
    private = Ed25519PrivateKey.generate()
    agent = {"runtime": "acp",
             "command": command or [sys.executable, str(FIXTURE), "acp"],
             "sandbox": "read-only"}
    envelope = sign_worker_snapshot(
        private, workspace_id=WORKSPACE, run_id=run_id, revision=1,
        routes={"broker_token": "run-token"}, agent=agent,
        expires_at=int(time.time()) + 3600, nonce=nonce)
    snapshot_path = tmp_path / f"snapshot-{nonce}.json"
    snapshot_path.write_text(json.dumps(envelope), encoding="utf-8")
    public_path = tmp_path / "worker.pem"
    public_path.write_bytes(private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo))

    paths = WorkspacePaths(
        code_root=tmp_path / "code", workspace_root=tmp_path / "workspace",
        state_root=tmp_path / "state", home=tmp_path / "home")
    (paths.workspace_root / "outputs").mkdir(parents=True, exist_ok=True)
    (paths.workspace_root / "profiles").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(web, "_WORKER_IDENTITY",
                        WorkerIdentity(WORKSPACE, TOKEN), raising=False)
    monkeypatch.setattr(web, "_RUNTIME_PATHS", paths)
    monkeypatch.setattr(web, "SESSIONS_DIR", paths.state_root / "sessions")
    monkeypatch.setattr(web, "OUTPUTS_DIR", paths.workspace_root / "outputs")
    monkeypatch.setattr(web, "PROFILES_DIR", paths.workspace_root / "profiles")
    monkeypatch.setattr(web, "ENV_FILE", tmp_path / ".env")
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(web, "_session_locks", {})
    monkeypatch.setattr(web, "_RUNNING_CHAT", {})
    monkeypatch.setattr(web, "_HARNESS_TASKS", set())
    monkeypatch.setattr(web, "_STOPPED_CHAT", set())
    monkeypatch.setattr(web, "_BG_TASKS", set())
    monkeypatch.setenv("EASEL_WORKER_SNAPSHOT_FILE", str(snapshot_path))
    monkeypatch.setenv("EASEL_WORKER_PUBLIC_KEY_FILE", str(public_path))
    monkeypatch.setenv("EASEL_RUN_ID", run_id)

    snapshot, admission = web._load_worker_runtime()
    assert admission["duplicate"] is False
    monkeypatch.setattr(web, "_WORKER_SNAPSHOT", snapshot)
    return paths


def worker_client() -> TestClient:
    return TestClient(web.app, base_url="http://127.0.0.1:7860",
                      client=("203.0.113.10", 50000))


def upload_files(client: TestClient, session_id: str, count: int,
                 prefix: str = "attachment"):
    return client.post(
        "/api/upload", headers=HEADERS,
        data={"sessionId": session_id},
        files=[("files", (f"{prefix}-{index}.txt", b"x", "text/plain"))
               for index in range(count)])


def admission_state(state_root: Path, run_id: str) -> str:
    database = state_root / "snapshot_admissions.sqlite3"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT state FROM snapshot_admissions"
            " WHERE workspace_id = ? AND run_id = ?",
            (WORKSPACE, run_id)).fetchone()
    assert row is not None
    return row[0]


def install_adapter(monkeypatch, adapter) -> None:
    monkeypatch.setattr(web.harnesses, "adapter", lambda runtime: adapter)


class SnapshotAdapter:
    def __init__(self) -> None:
        self.calls: list = []

    async def run(self, request, emit, ask, bind):
        self.calls.append(request)
        bind(request.native_id or "native-session-shared")
        emit("token", f"answer-{len(self.calls)}")


class FailingAdapter:
    async def run(self, request, emit, ask, bind):
        bind(request.native_id or "native-failing")
        emit("token", "partial")
        raise HarnessError("原生会话续接失败")


def test_worker_http_chat_persists_two_turns_and_replay_does_not_append(
        tmp_path, monkeypatch):
    first = install_worker(tmp_path, monkeypatch, run_id="run-chat-1",
                           nonce="nonce-chat-1")
    adapter = SnapshotAdapter()
    install_adapter(monkeypatch, adapter)

    with worker_client() as client:
        one = client.post("/api/chat", headers=HEADERS, json={
            "message": "第一轮", "sessionId": "session-chat"})
        assert one.status_code == 200, one.text
        assert one.json()["response"] == "answer-1"

        # 生产上每轮 Run 使用新快照；同一 worker state_root 与目录继续复用。
        install_worker(tmp_path, monkeypatch, run_id="run-chat-2",
                       nonce="nonce-chat-2")
        two = client.post("/api/chat/stream", headers=HEADERS, json={
            "message": "第二轮", "sessionId": "session-chat",
            "turnId": "turn-chat-2"})
        assert two.status_code == 200, two.text
        assert "answer-2" in two.text

        replay = client.get("/api/chat/jobs/turn-chat-2/stream",
                            headers=HEADERS)
        assert replay.status_code == 200
        assert "answer-2" in replay.text

    store = WorkspaceSessionStore(first.state_root, WORKSPACE)
    messages = store.messages("session-chat")
    assert [(item["role"], item["content"]) for item in messages] == [
        ("user", "第一轮"),
        ("assistant", "answer-1"),
        ("user", "第二轮"),
        ("assistant", "answer-2"),
    ]
    assert len(adapter.calls) == 2
    assert adapter.calls[1].native_id == "native-session-shared"


def test_worker_http_failure_persists_input_without_overwriting_history(
        tmp_path, monkeypatch):
    install_worker(tmp_path, monkeypatch, run_id="run-success-before-failure",
                   nonce="nonce-success-before-failure")
    install_adapter(monkeypatch, SnapshotAdapter())
    with worker_client() as client:
        first = client.post("/api/chat", headers=HEADERS, json={
            "message": "成功的一轮", "sessionId": "session-failure"})
        assert first.status_code == 200, first.text

        install_worker(tmp_path, monkeypatch, run_id="run-failure",
                       nonce="nonce-failure")
        install_adapter(monkeypatch, FailingAdapter())
        failed = client.post("/api/chat", headers=HEADERS, json={
            "message": "会失败的一轮", "sessionId": "session-failure"})
    assert failed.status_code == 502, failed.text
    assert "续接失败" in failed.json()["detail"]

    store = WorkspaceSessionStore(tmp_path / "state", WORKSPACE)
    messages = store.messages("session-failure")
    assert [(item["role"], item["content"]) for item in messages] == [
        ("user", "成功的一轮"),
        ("assistant", "answer-1"),
        ("user", "会失败的一轮"),
    ]
    assert all(item["role"] != "assistant" for item in messages[2:])


def test_worker_http_chat_uses_real_acp_adapter_and_fixture(tmp_path, monkeypatch):
    launcher = tmp_path / "launch_fixture.py"
    launcher.write_text(
        "import os, runpy, sys\n"
        "os.environ['FAKE_HARNESS_MODE'] = 'normal'\n"
        "os.environ['FAKE_HARNESS_AUDIT'] = sys.argv[1]\n"
        "script = sys.argv[2]\n"
        "sys.argv = [script, *sys.argv[3:]]\n"
        "runpy.run_path(script, run_name='__main__')\n",
        encoding="utf-8")
    audit = tmp_path / "acp-audit.jsonl"
    command = [sys.executable, str(launcher), str(audit), str(FIXTURE), "acp"]
    paths = install_worker(tmp_path, monkeypatch, run_id="run-real-acp",
                           nonce="nonce-real-acp", command=command)

    with worker_client() as client:
        response = client.post("/api/chat", headers=HEADERS, json={
            "message": "真实 ACP", "sessionId": "session-real-acp"})
    assert response.status_code == 200, response.text
    assert response.json()["response"] == "你好"

    store = WorkspaceSessionStore(paths.state_root, WORKSPACE)
    messages = store.messages("session-real-acp")
    assert [(item["role"], item["content"]) for item in messages] == [
        ("user", "真实 ACP"), ("assistant", "你好")]
    methods = [json.loads(line).get("method") for line in
               audit.read_text(encoding="utf-8").splitlines()]
    assert "initialize" in methods
    assert "session/new" in methods
    assert "session/prompt" in methods


def test_worker_upload_and_chat_accept_21_attachments(tmp_path, monkeypatch):
    paths = install_worker(tmp_path, monkeypatch, run_id="run-21",
                           nonce="nonce-21")
    adapter = SnapshotAdapter()
    install_adapter(monkeypatch, adapter)

    with worker_client() as client:
        uploaded = upload_files(client, "session-21", 21)
        assert uploaded.status_code == 200, uploaded.text
        refs = uploaded.json()["files"]
        assert len(refs) == 21
        response = client.post("/api/chat", headers=HEADERS, json={
            "message": "多附件", "sessionId": "session-21",
            "attachments": refs})

    assert response.status_code == 200, response.text
    assert len(adapter.calls) == 1
    store = WorkspaceSessionStore(paths.state_root, WORKSPACE)
    messages = store.messages("session-21")
    assert messages[0]["content"] == "多附件"
    assert len(messages[0]["attachments"]) == 21
    assert messages[1]["content"] == "answer-1"
    assert admission_state(paths.state_root, "run-21") == "running"


def test_worker_upload_over_limit_rejects_before_creating_batch(
        tmp_path, monkeypatch):
    paths = install_worker(tmp_path, monkeypatch, run_id="run-upload-limit",
                           nonce="nonce-upload-limit")

    with worker_client() as client:
        response = upload_files(
            client, "session-upload-limit", MAX_ATTACHMENTS + 1)

    assert response.status_code == 400, response.text
    assert str(MAX_ATTACHMENTS) in response.json()["detail"]
    scope = web._attachment_scope("session-upload-limit")
    assert not (paths.workspace_root / "outputs" / "_inbox" / scope).exists()


def test_worker_chat_over_limit_rejects_before_claim_and_retry_succeeds(
        tmp_path, monkeypatch):
    paths = install_worker(tmp_path, monkeypatch, run_id="run-limit-a",
                           nonce="nonce-limit-a")
    adapter = SnapshotAdapter()
    install_adapter(monkeypatch, adapter)

    with worker_client() as client:
        boundary = upload_files(
            client, "session-limit", MAX_ATTACHMENTS, "boundary")
        assert boundary.status_code == 200, boundary.text
        refs = boundary.json()["files"]
        assert len(refs) == MAX_ATTACHMENTS
        at_limit = client.post("/api/chat", headers=HEADERS, json={
            "message": "边界附件", "sessionId": "session-limit",
            "attachments": refs})
        assert at_limit.status_code == 200, at_limit.text
        assert len(adapter.calls) == 1

        # 新 Run 用于验证超限输入不消耗认领，修正后仍可执行一次。
        install_worker(tmp_path, monkeypatch, run_id="run-limit-b",
                       nonce="nonce-limit-b")
        extra = upload_files(client, "session-limit", 1, "extra")
        assert extra.status_code == 200, extra.text
        over_limit = refs + extra.json()["files"]
        assert len(over_limit) == MAX_ATTACHMENTS + 1
        rejected = client.post("/api/chat", headers=HEADERS, json={
            "message": "超限附件", "sessionId": "session-limit",
            "attachments": over_limit})
        assert rejected.status_code == 400, rejected.text
        assert len(adapter.calls) == 1
        assert admission_state(paths.state_root, "run-limit-b") == "accepted"

        corrected = client.post("/api/chat", headers=HEADERS, json={
            "message": "修正附件", "sessionId": "session-limit",
            "attachments": extra.json()["files"]})
        assert corrected.status_code == 200, corrected.text

    assert len(adapter.calls) == 2
    assert admission_state(paths.state_root, "run-limit-b") == "running"


def test_worker_stream_rejects_oversized_message_before_claim(
        tmp_path, monkeypatch):
    paths = install_worker(tmp_path, monkeypatch, run_id="run-oversize",
                           nonce="nonce-oversize")
    adapter = SnapshotAdapter()
    install_adapter(monkeypatch, adapter)
    oversized = "x" * (MAX_MESSAGE_BYTES + 1)

    with worker_client() as client:
        rejected = client.post("/api/chat/stream", headers=HEADERS, json={
            "message": oversized, "sessionId": "session-oversize",
            "turnId": "turn-oversize"})
        assert rejected.status_code == 400, rejected.text
        assert not web._job_event_file("turn-oversize").exists()
        assert adapter.calls == []
        assert admission_state(paths.state_root, "run-oversize") == "accepted"

        corrected = client.post("/api/chat/stream", headers=HEADERS, json={
            "message": "修正消息", "sessionId": "session-oversize",
            "turnId": "turn-corrected"})
        assert corrected.status_code == 200, corrected.text

    assert len(adapter.calls) == 1
    assert admission_state(paths.state_root, "run-oversize") == "running"


def test_worker_old_delete_rejects_without_touching_binding_or_messages(
        tmp_path, monkeypatch):
    install_worker(tmp_path, monkeypatch, run_id="run-delete",
                   nonce="nonce-delete")
    install_adapter(monkeypatch, SnapshotAdapter())

    with worker_client() as client:
        response = client.post("/api/chat", headers=HEADERS, json={
            "message": "保留我", "sessionId": "session-delete"})
        assert response.status_code == 200, response.text
        store = WorkspaceSessionStore(tmp_path / "state", WORKSPACE)
        binding_before = store.binding_path("session-delete").read_bytes()
        messages_before = store.messages_path("session-delete").read_bytes()

        deleted = client.delete("/api/session/session-delete", headers=HEADERS)

        assert deleted.status_code == 409, deleted.text
        assert "worker" in deleted.json()["detail"]
        assert store.binding_path("session-delete").read_bytes() == binding_before
        assert store.messages_path("session-delete").read_bytes() == messages_before
        assert store.binding("session-delete")["native_id"] == "native-session-shared"
        with pytest.raises(DomainError) as conflict:
            store.bind("session-delete", "acp", "new-empty-native", 1)
        assert conflict.value.code == "session_binding_conflict"
