"""Task 10 Step 1：授权代理与一次接纳的红灯测试。

边界：
- 外空间 Run 的停止/回放在任何 worker HTTP 调用前返回 404。
- 两个 control Store 连接并发提交同一 request_id/input 只生成一个 Run，
  且只向 worker 发送一次任务 POST。
- 同一 request_id 携带不同输入返回 409，不覆盖原 Run。
"""
from __future__ import annotations

import asyncio
import threading

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.control.auth import AuthService
from easel.control.proxy import PinnedRunConfig, WorkerRelay
from easel.control.sessions import SessionService
from easel.control.store import Store
from easel.control.types import DomainError, Identity, WorkerHandle

REVISION = 7
UPSTREAM = "http://worker.test"


class CountingTransport(httpx.AsyncBaseTransport):
    """记录所有上游调用；共享于两个事件循环时用锁保护。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        with self._lock:
            self.calls.append({
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": body,
            })
        return httpx.Response(
            202, json={"state": "accepted", "runId": _run_from(request.url)},
            request=request)

    def posts(self) -> list[dict]:
        with self._lock:
            return [call for call in self.calls if call["method"] == "POST"]


def _run_from(url: httpx.URL) -> str:
    return str(url).rstrip("/").rsplit("/", 1)[-1]


class StaticRuntime:
    """固定返回已就绪 worker；记录调用次数。"""

    def __init__(self, handle: WorkerHandle) -> None:
        self.handle = handle
        self.calls: list[tuple[str, int]] = []

    async def ensure_ready(self, workspace_id: str,
                           config_version: int) -> WorkerHandle:
        self.calls.append((workspace_id, config_version))
        assert workspace_id == self.handle.workspace_id
        assert config_version == self.handle.config_version
        return self.handle

    async def bound_handle(self, workspace_id: str, container_id: str,
                           config_version: int) -> WorkerHandle | None:
        if (workspace_id != self.handle.workspace_id
                or container_id != self.handle.container_id
                or config_version != self.handle.config_version):
            return None
        return self.handle


def provider(_actor: Identity) -> PinnedRunConfig:
    return PinnedRunConfig(
        revision=REVISION,
        routes={"broker_token": "run-scoped-token"},
        agent={"runtime": "acp", "command": ["fixture", "acp"],
               "sandbox": "read-only"})


def make_relay(store: Store, handle: WorkerHandle, key: Ed25519PrivateKey,
               client: httpx.AsyncClient, runtime=None) -> WorkerRelay:
    return WorkerRelay(
        runtime or StaticRuntime(handle), SessionService(store),
        AuthService(store), store=store, signing_key=key,
        http_client=client, config_provider=provider,
        clock=lambda: 1000)


def handle_for(identity: Identity) -> WorkerHandle:
    return WorkerHandle(identity.workspace_id, "container-" + identity.user_id,
                        UPSTREAM, "service-token-" + identity.user_id,
                        REVISION)


def test_foreign_run_is_rejected_before_worker_call(tmp_path):
    database = tmp_path / "control.sqlite3"
    writer = Store.connect(database)
    reader = Store.connect(database)
    try:
        owner = writer.create_user("Owner")
        stranger = reader.create_user("Stranger")
        conversation = writer.create_conversation(owner, "私有会话")
        run = writer.reserve_run(
            owner, conversation.id, "request-owner", "digest-owner", REVISION)

        transport = CountingTransport()
        key = Ed25519PrivateKey.generate()

        async def scenario() -> None:
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(reader, handle_for(stranger), key, client)
                with pytest.raises(DomainError) as stopped:
                    await relay.stop(stranger, {"runId": run.id})
                assert stopped.value.status == 404
                with pytest.raises(DomainError) as replayed:
                    await relay.events(stranger, run.id, after=0)
                assert replayed.value.status == 404

        asyncio.run(scenario())
        assert transport.calls == []
    finally:
        writer.close()
        reader.close()


def test_concurrent_same_request_admits_once(tmp_path):
    database = tmp_path / "control.sqlite3"
    setup = Store.connect(database)
    try:
        owner = setup.create_user("Owner")
        conversation = setup.create_conversation(owner, "并发")
    finally:
        setup.close()

    transport = CountingTransport()
    barrier = threading.Barrier(2)
    results: list[object] = []
    lock = threading.Lock()

    def submit_once() -> None:
        store = Store.connect(database)
        try:
            async def scenario():
                async with httpx.AsyncClient(transport=transport) as client:
                    relay = make_relay(store, handle_for(owner),
                                       Ed25519PrivateKey.generate(), client)
                    barrier.wait(timeout=10)
                    return await relay.submit(owner, {
                        "sessionId": conversation.id,
                        "turnId": "client-turn-1",
                        "message": "同一个请求",
                    })
            outcome = asyncio.run(scenario())
        except BaseException as error:  # noqa: BLE001 - 原样传给主线程断言
            outcome = error
        finally:
            store.close()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=submit_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert [type(item).__name__ for item in results] == ["dict", "dict"], results
    assert len({item["runId"] for item in results}) == 1
    assert len(transport.posts()) == 1

    verify = Store.connect(database)
    try:
        runs = verify.list_runs(owner, conversation.id)
        assert len(runs) == 1
        assert runs[0].id == results[0]["runId"]
    finally:
        verify.close()


def test_request_digest_conflict(tmp_path):
    database = tmp_path / "control.sqlite3"
    store = Store.connect(database)
    try:
        owner = store.create_user("Owner")
        conversation = store.create_conversation(owner, "冲突")
        transport = CountingTransport()
        key = Ed25519PrivateKey.generate()

        async def scenario() -> None:
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(store, handle_for(owner), key, client)
                first = await relay.submit(owner, {
                    "sessionId": conversation.id,
                    "turnId": "client-turn-conflict",
                    "message": "原始输入",
                })
                assert first["runId"]
                with pytest.raises(DomainError) as conflict:
                    await relay.submit(owner, {
                        "sessionId": conversation.id,
                        "turnId": "client-turn-conflict",
                        "message": "被替换的输入",
                    })
                assert conflict.value.code == "request_conflict"
                assert conflict.value.status == 409

        asyncio.run(scenario())
        assert len(transport.posts()) == 1
        assert len(store.list_runs(owner, conversation.id)) == 1
    finally:
        store.close()
