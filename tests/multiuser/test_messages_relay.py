"""Task 10 Step 5：消息快照只经本人 worker 读取，不启动任务或宿主路径。

边界：

* 外空间/未知会话在记录任何 worker 调用前 404；
* 分页参数原样转发到本人内部快照路由；
* 没有 Run 的会话返回空历史，不启动 worker，也不启动 prompt；
* 读取只产生 GET，不产生任务 POST，也不创建新 Run。
"""
from __future__ import annotations

import asyncio

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.proxy import PinnedRunConfig, WorkerRelay
from easel.control.sessions import SessionService
from easel.control.store import Store
from easel.control.types import Identity, WorkerHandle

ORIGIN = "https://easel.test"
REVISION = 5


class StaticRuntime:
    """固定返回本人已就绪 worker，并记录是否被启动。"""

    def __init__(self, workspaces: dict[str, str]) -> None:
        self._workspaces = workspaces
        self.calls: list[tuple[str, int]] = []

    async def ensure_ready(self, workspace_id: str,
                           config_version: int) -> WorkerHandle:
        self.calls.append((workspace_id, config_version))
        upstream = self._workspaces[workspace_id]
        return WorkerHandle(workspace_id, "container-" + workspace_id,
                            upstream, "service-token-" + workspace_id,
                            config_version)

    async def bound_handle(self, workspace_id: str, container_id: str,
                           config_version: int) -> WorkerHandle | None:
        if container_id != "container-" + workspace_id:
            return None
        return WorkerHandle(
            workspace_id, container_id, self._workspaces[workspace_id],
            "service-token-" + workspace_id, config_version)


class SnapshotTransport(httpx.AsyncBaseTransport):
    """只实现内部快照读取；任何任务 POST 都会记录以便断言未发生。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        self.calls.append({"method": request.method,
                           "path": request.url.path,
                           "params": dict(request.url.params),
                           "headers": dict(request.headers),
                           "body": body})
        if request.method == "GET" and request.url.path.endswith("/messages"):
            session_id = request.url.path.split("/")[3]
            return httpx.Response(200, json={
                "sessionId": session_id,
                "messages": [
                    {"role": "user", "content": "第一问"},
                    {"role": "assistant", "content": "第一答"},
                ],
            }, request=request)
        return httpx.Response(202, json={"state": "accepted"},
                              request=request)


def provider(_actor: Identity) -> PinnedRunConfig:
    return PinnedRunConfig(
        revision=REVISION,
        routes={"broker_token": "run-scoped-token"},
        agent={"runtime": "acp", "command": ["fixture", "acp"],
               "sandbox": "read-only"})


def make_app(tmp_path):
    """构造真实 control app；worker 传输与运行时为受控假实现。"""
    setup = Store.connect(tmp_path / "control.sqlite3")
    try:
        admin = setup.create_user("Owner", "admin")
        member = setup.create_user("Member")
        other = setup.create_user("Other")
        owned = setup.create_conversation(member, "有任务")
        empty = setup.create_conversation(member, "无任务")
        foreign = setup.create_conversation(other, "别人的")
        setup.reserve_run(member, owned.id, "request-one", "digest-one",
                          REVISION)
        auth = AuthService(setup)
        invite = auth.create_invite(admin, member.user_id)
    finally:
        setup.close()

    transport = SnapshotTransport()
    runtime = StaticRuntime({
        member.workspace_id: "http://worker-member.test",
    })
    key = Ed25519PrivateKey.generate()
    clients: list[httpx.AsyncClient] = []

    def factory(runtime_arg, sessions, auth_arg, store, *deps):
        # 应用 lifespan 传入其 HTTP 客户端、broker 与数据库线程；测试用
        # MockTransport 客户端替换真实网络，但仍验证 db_runner 被传入。
        http_client = httpx.AsyncClient(transport=transport)
        clients.append(http_client)
        db_runner = deps[2] if len(deps) > 2 else None
        return WorkerRelay(
            runtime_arg, sessions, auth_arg, store=store,
            signing_key=key, http_client=http_client,
            config_provider=provider, db_runner=db_runner)

    app_store = Store.connect(tmp_path / "control.sqlite3")
    try:
        app = create_app(
            store=app_store, auth=AuthService(app_store),
            config=ControlConfig(public_origin=ORIGIN),
            runtime=runtime, relay_factory=factory)
    finally:
        # create_app 只读取控制库路径；lifespan 会在专用线程重建连接
        app_store.close()
    return (app, member, owned, empty, foreign, invite, transport, runtime,
            clients)


def close_clients(clients: list[httpx.AsyncClient]) -> None:
    for client in clients:
        asyncio.run(client.aclose())


def login(client: TestClient, invite: str) -> None:
    response = client.post("/api/auth/exchange", json={"token": invite},
                           headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text


def test_messages_route_reads_own_worker_snapshot_with_pagination(tmp_path):
    (app, member, owned, empty, _foreign, invite, transport,
     runtime, clients) = make_app(tmp_path)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            login(client, invite)
            response = client.get(
                f"/api/sessions/{owned.id}/messages?limit=2&offset=1")
            assert response.status_code == 200, response.text
            assert response.json() == {"messages": [
                {"role": "user", "content": "第一问"},
                {"role": "assistant", "content": "第一答"},
            ]}
            assert response.headers["cache-control"] == "no-store"

            # 无 Run 的会话不启动 worker，也不读取宿主任意路径
            empty_response = client.get(
                f"/api/sessions/{empty.id}/messages")
            assert empty_response.status_code == 200, empty_response.text
            assert empty_response.json() == {"messages": []}
    finally:
        close_clients(clients)

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == f"/internal/sessions/{owned.id}/messages"
    assert call["params"] == {"limit": "2", "offset": "1"}
    assert call["headers"]["x-easel-workspace-id"] == member.workspace_id
    assert call["headers"]["x-easel-service-token"] == (
        "service-token-" + member.workspace_id)
    # 只定位已有 Run 的本人 worker；空会话不触发 ensure_ready
    assert runtime.calls == [(member.workspace_id, REVISION)]


def test_foreign_or_unknown_session_is_404_before_worker(tmp_path):
    (app, _member, _owned, _empty, foreign, invite, transport,
     runtime, clients) = make_app(tmp_path)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            login(client, invite)
            before = client.get(f"/api/sessions/{foreign.id}/messages")
            assert before.status_code == 404
            assert before.json()["code"] == "not_found"
            unknown = client.get("/api/sessions/does-not-exist/messages")
            assert unknown.status_code == 404
            assert unknown.json()["code"] == "not_found"
    finally:
        close_clients(clients)

    assert transport.calls == []
    assert runtime.calls == []


def test_lifespan_owns_http_client_and_forwards_db_runner(tmp_path):
    """relay 的 HTTP 客户端由 lifespan 管理；数据库线程显式传入。"""
    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        captured: dict = {}

        def factory(*args):
            captured["args"] = args
            return object()

        app = create_app(
            store=store, auth=AuthService(store),
            config=ControlConfig(public_origin=ORIGIN),
            runtime=None, relay_factory=factory)
        with TestClient(app, base_url=ORIGIN):
            args = captured["args"]
            assert app.state.relay is not None
    finally:
        store.close()

    # (runtime, sessions, auth, store, http_client, broker, db_runner)
    assert len(args) == 7
    assert isinstance(args[4], httpx.AsyncClient)
    assert args[5] is None
    assert callable(args[6])
    assert args[4].is_closed, "lifespan 退出时必须关闭共享 HTTP 客户端"


def test_relay_messages_without_run_does_not_start_worker(tmp_path):
    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        member = store.create_user("Member")
        conversation = store.create_conversation(member, "空会话")
        transport = SnapshotTransport()
        runtime = StaticRuntime({
            member.workspace_id: "http://worker-member.test"})

        async def scenario() -> None:
            async with httpx.AsyncClient(transport=transport) as client:
                relay = WorkerRelay(
                    runtime, SessionService(store), AuthService(store),
                    store=store, signing_key=Ed25519PrivateKey.generate(),
                    http_client=client, config_provider=provider)
                result = await relay.messages(member, conversation.id,
                                              limit=10, offset=0)
                assert result == {"sessionId": conversation.id,
                                  "messages": []}

        asyncio.run(scenario())
        assert transport.calls == []
        assert runtime.calls == []
    finally:
        store.close()
