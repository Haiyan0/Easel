"""Task 9 R1：真实 worker HTTP 聊天快照与旧删除入口回归。"""
from __future__ import annotations

import json
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
from easel.worker.sessions import WorkspaceSessionStore

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
