"""Task 6 返工：真实 HTTP 入口的持久化执行认领与快照时效门禁。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

import web.app as web
from easel.control.types import WorkspacePaths
from easel.worker.config import sign_worker_snapshot
from easel.worker.guard import WorkerIdentity

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "harness_rpc.py"
WORKSPACE = "workspace-gate"
RUN_ID = "run-gate"
TOKEN = "service-token-gate"
HEADERS = {"X-Easel-Workspace-Id": WORKSPACE,
           "X-Easel-Service-Token": TOKEN}


class FakeAdapter:
    """只在真正进入 Harness 时计数；可挂异步钩子模拟在途执行。"""

    def __init__(self) -> None:
        self.calls: list = []
        self.on_run = None

    async def run(self, request, emit, ask, bind):
        self.calls.append(request)
        bind(f"native-session-{len(self.calls)}")
        if self.on_run is not None:
            await self.on_run()
        emit("token", "ok")


def install_worker(tmp_path: Path, monkeypatch, *, expires_at: int) -> Path:
    """走真实启动路径加载签名快照并完成首次接纳。"""
    private = Ed25519PrivateKey.generate()
    envelope = sign_worker_snapshot(
        private, workspace_id=WORKSPACE, run_id=RUN_ID, revision=1,
        routes={"broker_token": "run-token"},
        agent={"runtime": "acp",
               "command": [sys.executable, str(FIXTURE), "acp"]},
        expires_at=expires_at, nonce="gate-nonce")
    snapshot_path = tmp_path / "snapshot.json"
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
    # 流式 supervisor 会注册到进程级任务集；隔离后不影响后续用例的空闲判断
    monkeypatch.setattr(web, "_BG_TASKS", set())
    monkeypatch.setenv("EASEL_WORKER_SNAPSHOT_FILE", str(snapshot_path))
    monkeypatch.setenv("EASEL_WORKER_PUBLIC_KEY_FILE", str(public_path))
    monkeypatch.setenv("EASEL_RUN_ID", RUN_ID)

    snapshot, admission = web._load_worker_runtime()
    assert admission["duplicate"] is False
    monkeypatch.setattr(web, "_WORKER_SNAPSHOT", snapshot)
    return paths.state_root / "snapshot_admissions.sqlite3"


def install_fake_adapter(monkeypatch) -> FakeAdapter:
    fake = FakeAdapter()
    monkeypatch.setattr(web.harnesses, "adapter", lambda runtime: fake)
    return fake


def chat(client: TestClient, session: str):
    return client.post("/api/chat", headers=HEADERS,
                       json={"message": "hello", "sessionId": session})


def stream_chat(client: TestClient, session: str, turn: str):
    return client.post("/api/chat/stream", headers=HEADERS,
                       json={"message": "hello", "sessionId": session,
                             "turnId": turn})


def worker_client():
    return TestClient(web.app, base_url="http://127.0.0.1:7860",
                      client=("203.0.113.10", 50000))


def test_real_http_duplicate_and_reload_do_not_rerun_adapter(
        tmp_path, monkeypatch):
    install_worker(tmp_path, monkeypatch, expires_at=int(time.time()) + 3600)
    fake = install_fake_adapter(monkeypatch)

    with worker_client() as client:
        first = chat(client, "session-1")
        assert first.status_code == 200, first.text
        assert first.json()["response"] == "ok"
        assert len(fake.calls) == 1

        duplicate = chat(client, "session-2")
        assert duplicate.status_code == 409, duplicate.text
        assert len(fake.calls) == 1, "重复 run/nonce 不得再次调用 Harness"

        # 重载 worker runtime：新快照对象、同一持久接纳库
        reloaded, admission = web._load_worker_runtime()
        assert admission["duplicate"] is True
        monkeypatch.setattr(web, "_WORKER_SNAPSHOT", reloaded)

        after_reload = chat(client, "session-3")
        assert after_reload.status_code == 409, after_reload.text
        assert len(fake.calls) == 1, "重载后重复 run/nonce 不得再次调用 Harness"


def test_concurrent_http_requests_claim_exactly_one_execution(
        tmp_path, monkeypatch):
    install_worker(tmp_path, monkeypatch, expires_at=int(time.time()) + 3600)
    fake = install_fake_adapter(monkeypatch)

    async def hold():
        await asyncio.sleep(0.2)

    fake.on_run = hold
    # 两个独立 TestClient（独立连接/事件循环）同时请求
    with worker_client() as first, worker_client() as second:
        clients = [first, second]
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(
                lambda index: chat(clients[index], f"concurrent-{index}"),
                range(2)))

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 409], [response.text for response in responses]
    assert len(fake.calls) == 1, "并发请求只能有一次真实执行"


def test_expired_snapshot_rejects_before_adapter_and_keeps_claim_unused(
        tmp_path, monkeypatch):
    database = install_worker(tmp_path, monkeypatch,
                              expires_at=int(time.time()) + 60)
    fake = install_fake_adapter(monkeypatch)
    real_time = time.time
    monkeypatch.setattr(web.time, "time", lambda: real_time() + 7200)

    with worker_client() as client:
        response = chat(client, "expired-session")

    assert response.status_code == 403, response.text
    assert "过期" in response.json()["detail"]
    assert fake.calls == [], "过期快照不得启动适配器"
    with sqlite3.connect(database) as connection:
        state = connection.execute(
            "SELECT state FROM snapshot_admissions").fetchone()[0]
    assert state == "accepted", "过期拒绝不应消耗执行认领"


def test_started_execution_completes_across_snapshot_expiry(
        tmp_path, monkeypatch):
    install_worker(tmp_path, monkeypatch, expires_at=int(time.time()) + 60)
    fake = install_fake_adapter(monkeypatch)
    clock = {"now": int(time.time())}
    monkeypatch.setattr(web.time, "time", lambda: clock["now"])

    async def cross_expiry():
        clock["now"] += 3600

    fake.on_run = cross_expiry
    with worker_client() as client:
        response = chat(client, "in-flight-session")

    assert response.status_code == 200, response.text
    assert response.json()["response"] == "ok"
    assert len(fake.calls) == 1, "合法启动的在途执行应正常完成"


def test_duplicate_stream_request_preserves_original_turn_log(
        tmp_path, monkeypatch):
    install_worker(tmp_path, monkeypatch, expires_at=int(time.time()) + 3600)
    fake = install_fake_adapter(monkeypatch)

    with worker_client() as client:
        first = stream_chat(client, "stream-session", "turn-stream-1")
        assert first.status_code == 200, first.text
        event_path = web._job_event_file("turn-stream-1")
        original = event_path.read_bytes()
        assert [event["event"] for event in
                web._read_job_events("turn-stream-1")] == [
                    "activity", "token", "done"]
        assert len(fake.calls) == 1

        duplicate = stream_chat(client, "stream-session", "turn-stream-1")
        assert event_path.read_bytes() == original, \
            "重复请求不得清空/改写原任务日志"
        assert duplicate.status_code == 409, duplicate.text
        assert len(fake.calls) == 1, "重复流式请求不得再次调用 Harness"

        replay = client.get("/api/chat/jobs/turn-stream-1/stream",
                            headers=HEADERS)
        assert replay.status_code == 200
        assert "event: token" in replay.text
        assert "ok" in replay.text, "原 token 仍应可回放"


def test_in_flight_duplicate_stream_does_not_corrupt_original_events(
        tmp_path, monkeypatch):
    install_worker(tmp_path, monkeypatch, expires_at=int(time.time()) + 3600)
    fake = install_fake_adapter(monkeypatch)
    started = threading.Event()
    release = threading.Event()

    async def hold():
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.02)

    fake.on_run = hold
    with worker_client() as first_client, worker_client() as second_client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(stream_chat, first_client,
                                 "in-flight-stream", "turn-inflight")
            try:
                assert started.wait(5), "首个流式执行未启动"
                duplicate = stream_chat(second_client,
                                        "in-flight-other", "turn-inflight")
            finally:
                release.set()
            first = future.result(timeout=15)

    assert first.status_code == 200, first.text
    assert len(fake.calls) == 1
    events = [event["event"] for event in
              web._read_job_events("turn-inflight")]
    assert events == ["activity", "token", "done"], events
    assert duplicate.status_code == 409, duplicate.text


def test_expired_first_stream_does_not_create_event_file(tmp_path, monkeypatch):
    database = install_worker(tmp_path, monkeypatch,
                              expires_at=int(time.time()) + 60)
    fake = install_fake_adapter(monkeypatch)
    real_time = time.time
    monkeypatch.setattr(web.time, "time", lambda: real_time() + 7200)

    with worker_client() as client:
        response = stream_chat(client, "expired-stream", "turn-expired")

    assert response.status_code == 403, response.text
    assert fake.calls == []
    assert not web._job_event_file("turn-expired").exists(), \
        "过期拒绝不得创建本轮事件文件"
    with sqlite3.connect(database) as connection:
        state = connection.execute(
            "SELECT state FROM snapshot_admissions").fetchone()[0]
    assert state == "accepted", "过期拒绝不应消耗执行认领"


@pytest.mark.parametrize("entry", ["stream", "chat"])
def test_legal_first_request_executes_once_without_double_claim(
        tmp_path, monkeypatch, entry):
    install_worker(tmp_path, monkeypatch, expires_at=int(time.time()) + 3600)
    fake = install_fake_adapter(monkeypatch)

    with worker_client() as client:
        if entry == "stream":
            response = stream_chat(client, "legal-stream", "turn-legal")
            assert response.status_code == 200, response.text
            assert [event["event"] for event in
                    web._read_job_events("turn-legal")] == [
                        "activity", "token", "done"]
        else:
            response = chat(client, "legal-chat")
            assert response.status_code == 200, response.text
            assert response.json()["response"] == "ok"

    assert len(fake.calls) == 1, "合法首次请求只能有一次执行"
