"""A4.1/A4.2：正式 WorkerRelay 的 gateway 传输与故障恢复红灯测试。"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.control.auth import AuthService
from easel.control.gateway_client import GatewayClient
from easel.control.proxy import PinnedRunConfig, WorkerRelay
from easel.control.runtime import RuntimeUnavailable
from easel.control.sessions import SessionService
from easel.control.store import Store
from easel.control.types import DomainError, Identity, WorkerHandle
from easel.gateway.auth import (
    GATEWAY_ASSERTION_HEADER,
    GatewayAudience,
    verify_gateway_assertion,
)

REVISION = 4
WORKSPACE = "a" * 32
WORKER = "b" * 64
GATEWAY = "c" * 64
BOOT = "d" * 32
NEW_BOOT = "e" * 32
UPSTREAM = "http://172.31.0.9:8897"
WORKER_UPSTREAM = "http://172.32.0.2:7860"
TOKEN = "worker-service-token"


def _handle(workspace_id: str = WORKSPACE, boot: str = BOOT) -> WorkerHandle:
    return WorkerHandle(
        workspace_id, WORKER, WORKER_UPSTREAM, TOKEN, REVISION,
        gateway_instance_id=GATEWAY, gateway_generation=boot,
        gateway_upstream=UPSTREAM, gateway_state="available")


class GatewayTransport(httpx.AsyncBaseTransport):
    def __init__(self, responses=None) -> None:
        self.requests: list[dict] = []
        self._responses = list(responses or [])

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        self.requests.append({
            "method": request.method, "url": str(request.url),
            "headers": dict(request.headers), "body": body})
        if self._responses:
            status, payload, headers = self._responses.pop(0)
            return httpx.Response(
                status, json=payload, headers=headers or {}, request=request)
        if "/events" in str(request.url):
            return httpx.Response(
                200, json={"runId": "f" * 32, "state": "running",
                           "events": [], "lastEventId": 0},
                request=request)
        if "/messages" in str(request.url):
            return httpx.Response(
                200, json={"sessionId": "e" * 32, "messages": [], "total": 0},
                request=request)
        if "/questions/" in str(request.url):
            return httpx.Response(
                200, json={"runId": "f" * 32, "state": "running"},
                request=request)
        return httpx.Response(
            202, json={"state": "accepted", "runId": "f" * 32},
            request=request)


class StaticRuntime:
    def __init__(self, handle: WorkerHandle | None = None,
                 error: Exception | None = None) -> None:
        self.handle = handle
        self.error = error
        self.ensure_calls = 0
        self.restore_calls: list[tuple[str, str, int]] = []
        self.restore_handle: WorkerHandle | None = None
        self.restore_error: Exception | None = None
        self.stop_calls: list[tuple[str, str]] = []

    async def ensure_ready(self, workspace_id: str, config_version: int):
        self.ensure_calls += 1
        if self.error is not None:
            raise self.error
        assert self.handle is not None
        return self.handle

    async def bound_handle(self, workspace_id: str, container_id: str,
                           config_version: int):
        if self.error is not None:
            raise self.error
        assert self.handle is not None
        if (workspace_id != self.handle.workspace_id
                or container_id != self.handle.container_id
                or config_version != self.handle.config_version):
            return None
        return self.handle

    async def restore_gateway(self, workspace_id: str, container_id: str,
                              config_version: int):
        self.restore_calls.append(
            (workspace_id, container_id, config_version))
        if self.restore_error is not None:
            raise self.restore_error
        return self.restore_handle

    async def confirm_instance_stopped(self, workspace_id: str,
                                       container_id: str) -> bool:
        self.stop_calls.append((workspace_id, container_id))
        return True


def _dispatched(store: Store, user: Identity, conversation, run,
                container: str = WORKER) -> None:
    store.save_run_snapshot(user, run.id, json.dumps({
        "workspace_id": user.workspace_id, "run_id": run.id,
        "conversation_id": conversation.id, "task_type": "chat",
        "revision": REVISION, "nonce": "nonce",
        "input_digest": "digest", "expires_at": 9999,
        "routes": {}, "agent": {}}))
    store.claim_run_dispatch(user, run.id, "nonce", container, now=900)
    store.mark_running(user, run.id)


def _provider(_actor: Identity) -> PinnedRunConfig:
    return PinnedRunConfig(
        revision=REVISION, routes={},
        agent={"runtime": "acp", "command": ["fixture", "acp"],
               "sandbox": "read-only"})


def _relay(store: Store, runtime, client: httpx.AsyncClient,
           key: Ed25519PrivateKey) -> WorkerRelay:
    return WorkerRelay(
        runtime, SessionService(store), AuthService(store), store=store,
        signing_key=key, config_provider=_provider, http_client=client,
        gateway_client=GatewayClient(key, client, clock=lambda: 1000),
        gateway_required=True, clock=lambda: 1000)


def _assert_gateway_signature(key: Ed25519PrivateKey, captured: dict,
                              handle: WorkerHandle) -> None:
    url = httpx.URL(captured["url"])
    raw_path = url.raw_path.decode("ascii").split("?", 1)[0]
    raw_query = url.query.decode("ascii")
    verify_gateway_assertion(
        key.public_key(),
        captured["headers"][GATEWAY_ASSERTION_HEADER.lower()],
        audience=GatewayAudience(
            handle.workspace_id, handle.gateway_instance_id,
            handle.gateway_generation, handle.container_id,
            handle.config_version),
        method=captured["method"], path=raw_path,
        canonical_query=raw_query,
        body_sha256=__import__("hashlib").sha256(
            captured["body"]).hexdigest(),
        now=1000)


def test_gateway_submit_never_uses_direct_worker_or_service_token(tmp_path):
    with Store.open(tmp_path / "db") as store:
        user = store.create_user("A")
        conversation = store.create_conversation(user, "gateway")
        handle = _handle(user.workspace_id)
        runtime = StaticRuntime(handle)
        transport = GatewayTransport()
        key = Ed25519PrivateKey.generate()

        async def scenario():
            async with httpx.AsyncClient(transport=transport) as client:
                relay = _relay(store, runtime, client, key)
                result = await relay.submit(user, {
                    "sessionId": conversation.id,
                    "turnId": "turn-gateway",
                    "message": "hello"})
                assert result["runId"]

        asyncio.run(scenario())
        assert len(transport.requests) == 1
        captured = transport.requests[0]
        assert captured["url"].startswith(UPSTREAM)
        assert WORKER_UPSTREAM not in captured["url"]
        lowered = {name.lower() for name in captured["headers"]}
        assert "x-easel-service-token" not in lowered
        assert captured["headers"][GATEWAY_ASSERTION_HEADER.lower()]
        _assert_gateway_signature(key, captured, handle)


def test_gateway_required_missing_handle_fields_fails_before_http(tmp_path):
    with Store.open(tmp_path / "db") as store:
        user = store.create_user("A")
        conversation = store.create_conversation(user, "missing")
        broken = WorkerHandle(
            user.workspace_id, WORKER, WORKER_UPSTREAM, TOKEN, REVISION,
            gateway_state="unavailable")
        runtime = StaticRuntime(broken)
        transport = GatewayTransport()
        key = Ed25519PrivateKey.generate()

        async def scenario():
            async with httpx.AsyncClient(transport=transport) as client:
                relay = _relay(store, runtime, client, key)
                with pytest.raises(DomainError) as rejected:
                    await relay.submit(user, {
                        "sessionId": conversation.id,
                        "turnId": "turn-missing", "message": "x"})
                assert rejected.value.code == "worker_gateway_unavailable"
                assert rejected.value.status == 503

        asyncio.run(scenario())
        assert transport.requests == []


def test_gateway_get_restores_same_worker_and_retries_after(tmp_path):
    with Store.open(tmp_path / "db") as store:
        user = store.create_user("A")
        conversation = store.create_conversation(user, "restore")
        run = store.reserve_run(user, conversation.id, "turn", "digest", REVISION)
        store.save_run_snapshot(user, run.id, json.dumps({
            "workspace_id": user.workspace_id, "run_id": run.id,
            "conversation_id": conversation.id, "task_type": "chat",
            "revision": REVISION, "nonce": "nonce",
            "input_digest": "digest", "expires_at": 9999,
            "routes": {}, "agent": {}}))
        store.claim_run_dispatch(user, run.id, "nonce", WORKER, now=900)
        store.mark_running(user, run.id)
        runtime = StaticRuntime(_handle(user.workspace_id))
        runtime.restore_handle = _handle(user.workspace_id, NEW_BOOT)
        transport = GatewayTransport([
            (503, {"error": "gateway_inactive"}, None),
        ])
        key = Ed25519PrivateKey.generate()

        async def scenario():
            async with httpx.AsyncClient(transport=transport) as client:
                relay = _relay(store, runtime, client, key)
                page = await relay.events(user, run.id, after=3)
                assert page["state"] == "running"

        asyncio.run(scenario())
        assert runtime.restore_calls == [
            (user.workspace_id, WORKER, REVISION)]
        assert len(transport.requests) == 2
        first, second = transport.requests
        assert "after=3" in first["url"]
        assert "after=3" in second["url"]
        _assert_gateway_signature(key, second, runtime.restore_handle)


def test_gateway_post_restores_but_does_not_retry(tmp_path):
    with Store.open(tmp_path / "db") as store:
        user = store.create_user("A")
        conversation = store.create_conversation(user, "stop")
        run = store.reserve_run(user, conversation.id, "turn", "digest", REVISION)
        store.save_run_snapshot(user, run.id, json.dumps({
            "workspace_id": user.workspace_id, "run_id": run.id,
            "conversation_id": conversation.id, "task_type": "chat",
            "revision": REVISION, "nonce": "nonce",
            "input_digest": "digest", "expires_at": 9999,
            "routes": {}, "agent": {}}))
        store.claim_run_dispatch(user, run.id, "nonce", WORKER, now=900)
        store.mark_running(user, run.id)
        runtime = StaticRuntime(_handle(user.workspace_id))
        runtime.restore_handle = _handle(user.workspace_id, NEW_BOOT)
        transport = GatewayTransport([
            (503, {"error": "gateway_inactive"}, {"Retry-After": "7"}),
        ])
        key = Ed25519PrivateKey.generate()

        async def scenario():
            async with httpx.AsyncClient(transport=transport) as client:
                relay = _relay(store, runtime, client, key)
                with pytest.raises(DomainError) as rejected:
                    await relay.stop(user, {"runId": run.id})
                assert rejected.value.code == "worker_gateway_unavailable"
                assert rejected.value.status == 503
                assert rejected.value.retry_after == 7

        asyncio.run(scenario())
        assert runtime.restore_calls
        assert len(transport.requests) == 1
        assert store.get_run(user, run.id).status == "running"


def test_gateway_unavailable_does_not_settle_or_stop_run(tmp_path):
    with Store.open(tmp_path / "db") as store:
        user = store.create_user("A")
        conversation = store.create_conversation(user, "keep")
        run = store.reserve_run(user, conversation.id, "turn", "digest", REVISION)
        _dispatched(store, user, conversation, run)
        runtime = StaticRuntime(error=RuntimeUnavailable(
            "gateway_unavailable", 503, retry_after=9))
        transport = GatewayTransport()
        key = Ed25519PrivateKey.generate()

        async def scenario():
            async with httpx.AsyncClient(transport=transport) as client:
                relay = _relay(store, runtime, client, key)
                for call in (
                        lambda: relay.events(user, run.id, after=0),
                        lambda: relay.stop(user, {"runId": run.id}),
                        lambda: relay.answer(user, {
                            "runId": run.id, "questionId": "q",
                            "answers": {"q": ["once"]}}),
                        lambda: relay.question_status(user, {
                            "runId": run.id, "questionIds": ["q"]}),
                        lambda: relay.messages(
                            user, conversation.id, limit=10, offset=0)):
                    with pytest.raises(DomainError) as rejected:
                        await call()
                    assert rejected.value.status == 503

        asyncio.run(scenario())
        assert transport.requests == []
        assert runtime.stop_calls == []
        assert store.get_run(user, run.id).status == "running"


def test_gateway_answer_encodes_question_id(tmp_path):
    with Store.open(tmp_path / "db") as store:
        user = store.create_user("A")
        conversation = store.create_conversation(user, "question")
        run = store.reserve_run(user, conversation.id, "turn", "digest", REVISION)
        _dispatched(store, user, conversation, run)
        runtime = StaticRuntime(_handle(user.workspace_id))
        transport = GatewayTransport()
        key = Ed25519PrivateKey.generate()

        async def scenario():
            async with httpx.AsyncClient(transport=transport) as client:
                relay = _relay(store, runtime, client, key)
                result = await relay.answer(user, {
                    "runId": run.id, "questionId": "ask me",
                    "answers": {"ask me": ["once"]}})
                assert result["state"] == "running"

        asyncio.run(scenario())
        captured = transport.requests[0]
        assert "/questions/ask%20me/answer" in captured["url"]
        _assert_gateway_signature(key, captured, _handle(user.workspace_id))


def test_gateway_client_none_with_gateway_required_is_unavailable(tmp_path):
    with Store.open(tmp_path / "db") as store:
        user = store.create_user("A")
        conversation = store.create_conversation(user, "cap")
        runtime = StaticRuntime(_handle(user.workspace_id))
        transport = GatewayTransport()
        key = Ed25519PrivateKey.generate()

        async def scenario():
            async with httpx.AsyncClient(transport=transport) as client:
                relay = WorkerRelay(
                    runtime, SessionService(store), AuthService(store),
                    store=store, signing_key=key, config_provider=_provider,
                    http_client=client, gateway_required=True,
                    clock=lambda: 1000)
                with pytest.raises(DomainError) as rejected:
                    await relay.submit(user, {
                        "sessionId": conversation.id,
                        "turnId": "turn-cap", "message": "x"})
                assert rejected.value.code == "capability_unavailable"

        asyncio.run(scenario())
        assert transport.requests == []


class _Broker:
    def __init__(self) -> None:
        self.revoked: list[tuple[str, str]] = []

    async def revoke_run(self, run_id: str, workspace_id: str) -> None:
        self.revoked.append((run_id, workspace_id))


def test_reconciler_gateway_unavailable_keeps_run_and_token(tmp_path):
    import time as _time
    with Store.open(tmp_path / "db") as store:
        user = store.create_user("A")
        conversation = store.create_conversation(user, "timeout")
        run = store.reserve_run(
            user, conversation.id, "turn", "digest", REVISION)
        now = int(_time.time())
        store.save_run_snapshot(user, run.id, json.dumps({
            "workspace_id": user.workspace_id, "run_id": run.id,
            "conversation_id": conversation.id, "task_type": "chat",
            "revision": REVISION, "nonce": "nonce",
            "input_digest": "digest", "expires_at": now - 900,
            "routes": {}, "agent": {"timeout": 1}}))
        store.claim_run_dispatch(user, run.id, "nonce", WORKER, now=now - 1000)
        store.mark_running(user, run.id)
        store._connection.execute(
            "UPDATE runs SET created_at = ? WHERE id = ?",
            (now - 1000, run.id))
        store._connection.commit()
        runtime = StaticRuntime(_handle(user.workspace_id))
        transport = GatewayTransport([
            (503, {"error": "gateway_inactive"}, None),
            (503, {"error": "gateway_inactive"}, None),
        ])
        key = Ed25519PrivateKey.generate()
        broker = _Broker()

        async def scenario():
            async with httpx.AsyncClient(transport=transport) as client:
                relay = _relay(store, runtime, client, key)
                relay._broker = broker
                result = await relay.reconcile_once()
                assert result == {"checked": 1, "finished": 0}
                assert store.get_run(user, run.id).status == "running"
                assert store.count_active_runs() == 1
                assert broker.revoked == []
                assert runtime.stop_calls == []

        asyncio.run(scenario())
