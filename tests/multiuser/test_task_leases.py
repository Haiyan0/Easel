"""Task 10 Step 3：统一任务类型表、容量账本与短请求租约。

红灯边界：

* 所有会启动后台/浏览器任务的入口都必须登记到同一张任务类型表，并共用
  ``Store.reserve_run`` 的 workspace 2 / global 8 / 同会话互斥账本。
* 未装配业务适配器的类型在接纳与 worker 调用之前明确 503，不能先占容量或
  直通旧本地 handler。
* ``awaiting_permission`` 属于活跃任务，占预算且不被空闲回收。
* 短请求租约在异常/断线时释放，过期租约不阻塞回收。
* drain 拒绝新任务且不留下残留接纳，已接受任务仍可结束与回放。
"""
from __future__ import annotations

import asyncio
import re

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from easel.control import policy as policy_module
from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.proxy import PinnedRunConfig, WorkerRelay
from easel.control.runtime import RuntimeManager
from easel.control.sessions import SessionService
from easel.control.store import Store
from easel.control.tasks import TASK_ENTRIES, entry_for_route
from easel.control.types import DomainError, Identity, WorkerHandle
from easel.worker.config import RUN_TASK_TYPES

REVISION = 7
UPSTREAM = "http://worker.test"
ALL_TASK_TYPES = tuple(RUN_TASK_TYPES)
ORIGIN = "https://easel.test"
COOKIE = "__Host-easel_session"


class Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class CountingTransport(httpx.AsyncBaseTransport):
    """记录上游调用；可注入连接失败验证租约释放。"""

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    async def handle_async_request(
            self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        self.calls.append({
            "method": request.method,
            "url": str(request.url),
            "body": body,
        })
        if self.fail is not None:
            raise self.fail
        match = re.search(r"/internal/runs/([^/]+)", request.url.path)
        run_id = match.group(1) if match else ""
        return httpx.Response(
            202, json={"state": "accepted", "runId": run_id}, request=request)

    def posts(self) -> list[dict]:
        return [call for call in self.calls if call["method"] == "POST"]


class WorkspaceRuntime:
    """按 workspace 返回固定已就绪句柄；不启动真实容器。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def ensure_ready(self, workspace_id: str,
                           config_version: int) -> WorkerHandle:
        self.calls.append((workspace_id, config_version))
        return WorkerHandle(
            workspace_id, "container-" + workspace_id[:8], UPSTREAM,
            "service-token-" + workspace_id[:8], config_version)

    async def bound_handle(self, workspace_id: str, container_id: str,
                           config_version: int) -> WorkerHandle | None:
        if container_id != "container-" + workspace_id[:8]:
            return None
        return WorkerHandle(
            workspace_id, container_id, UPSTREAM,
            "service-token-" + workspace_id[:8], config_version)


class FakeProvisioner:
    """最小 provisioner：只记录生命周期动作，供 drain/回收测试观察。"""

    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.handles: dict[str, WorkerHandle] = {}

    async def ensure(self, workspace_id: str,
                     config_version: int) -> WorkerHandle:
        self.starts += 1
        handle = WorkerHandle(
            workspace_id, f"container-{workspace_id[:8]}-{self.starts}",
            f"http://10.0.0.{self.starts + 1}:7860",
            f"service-token-{workspace_id[:8]}", config_version)
        self.handles[workspace_id] = handle
        return handle

    async def stop(self, workspace_id: str, instance_id: str) -> None:
        assert self.handles[workspace_id].container_id == instance_id
        self.stops += 1

    async def inspect(self, workspace_id: str) -> dict:
        handle = self.handles[workspace_id]
        return {
            "managed": True,
            "ready": True,
            "workspace_id": workspace_id,
            "config_version": handle.config_version,
            "instance_id": handle.container_id,
            "upstream": handle.upstream,
            "labels": {
                "easel.managed": "true",
                "easel.workspace-id": workspace_id,
                "easel.config-version": str(handle.config_version),
            },
        }


def provider(_actor: Identity) -> PinnedRunConfig:
    return PinnedRunConfig(
        revision=REVISION,
        routes={"broker_token": "run-scoped-token"},
        agent={"runtime": "acp", "command": ["fixture", "acp"],
               "sandbox": "read-only"})


def make_relay(store: Store, client: httpx.AsyncClient, *,
               runtime=None, accepted: tuple[str, ...] | None = None,
               clock: Clock | None = None) -> WorkerRelay:
    return WorkerRelay(
        runtime or WorkspaceRuntime(), SessionService(store),
        AuthService(store), store=store,
        signing_key=Ed25519PrivateKey.generate(), http_client=client,
        config_provider=provider, clock=clock or Clock(),
        accepted_task_types=accepted)


def payload(conversation_id: str, message: str = "执行任务",
            turn_id: str | None = None) -> dict:
    body = {"sessionId": conversation_id, "message": message}
    if turn_id is not None:
        body["turnId"] = turn_id
    return body


def run_async(coroutine):
    return asyncio.run(coroutine)


# --------------------------------------------------------------------------
# 任务类型表
# --------------------------------------------------------------------------

def test_task_table_covers_every_policy_task_route():
    """policy 中每个 TASK 分类路由都必须有且只有一个类型归属。"""
    table: dict[tuple[str, str], int] = {}
    for entry in TASK_ENTRIES:
        for route in entry.routes:
            key = (route.method, route.pattern)
            assert key not in table, f"重复登记：{key}"
            table[key] = table.get(key, 0) + 1

    seen: set[tuple[str, str]] = set()
    for rule in policy_module._RULES:  # noqa: SLF001 - 用注册表反查覆盖
        if rule.kind != policy_module.TASK:
            continue
        sample = re.sub(r"\(\[\^/\]\+\)", "sample", rule.pattern.pattern)
        path = sample.strip("^$")
        for method in rule.methods:
            entry = entry_for_route(method, path)
            assert entry is not None, f"未登记的任务入口：{method} {path}"
            seen.add((method, rule.pattern.pattern))

    assert seen == set(table), "任务表与 policy 的 TASK 路由必须一一对应"


def test_task_table_reports_missing_adapters():
    implemented = {entry.task_type for entry in TASK_ENTRIES
                   if entry.implemented}
    assert implemented == {"chat"}, "本轮只装配 chat；其余必须明确 503"
    assert {entry.task_type for entry in TASK_ENTRIES} == set(RUN_TASK_TYPES)


# --------------------------------------------------------------------------
# 容量账本
# --------------------------------------------------------------------------

@pytest.mark.parametrize("task_type", ALL_TASK_TYPES)
def test_workspace_capacity_is_two_for_every_task_type(tmp_path, task_type):
    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        user = store.create_user("Owner")
        conversations = [
            store.create_conversation(user, f"会话 {index}")
            for index in range(3)]
        transport = CountingTransport()

        async def scenario() -> None:
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(store, client, accepted=ALL_TASK_TYPES)
                await relay.submit_task(
                    user, task_type, payload(conversations[0].id, "第一条"))
                await relay.submit_task(
                    user, task_type, payload(conversations[1].id, "第二条"))
                with pytest.raises(DomainError) as error:
                    await relay.submit_task(
                        user, task_type,
                        payload(conversations[2].id, "第三条"))
                assert error.value.code == "workspace_capacity"
                assert error.value.status == 429

        run_async(scenario())
        assert len(transport.posts()) == 2
        assert store.list_runs(user, conversations[2].id) == []
    finally:
        store.close()


def test_global_capacity_is_eight_across_task_types(tmp_path):
    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        users = [store.create_user(f"用户 {index}") for index in range(9)]
        conversations = [
            store.create_conversation(user, "唯一会话") for user in users]
        transport = CountingTransport()

        async def scenario() -> None:
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(store, client, accepted=ALL_TASK_TYPES)
                for index in range(8):
                    task_type = ALL_TASK_TYPES[index % len(ALL_TASK_TYPES)]
                    await relay.submit_task(
                        users[index], task_type,
                        payload(conversations[index].id, f"任务 {index}"))
                with pytest.raises(DomainError) as error:
                    await relay.submit_task(
                        users[8], ALL_TASK_TYPES[0],
                        payload(conversations[8].id, "第九个"))
                assert error.value.code == "global_capacity"
                assert error.value.status == 429

        run_async(scenario())
        assert len(transport.posts()) == 8
        assert store.list_runs(users[8], conversations[8].id) == []
    finally:
        store.close()


@pytest.mark.parametrize("task_type", ALL_TASK_TYPES)
def test_same_conversation_has_one_active_task_for_every_type(
        tmp_path, task_type):
    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        user = store.create_user("Owner")
        conversation = store.create_conversation(user, "互斥")
        transport = CountingTransport()

        async def scenario() -> None:
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(store, client, accepted=ALL_TASK_TYPES)
                await relay.submit_task(
                    user, task_type, payload(conversation.id, "第一条"))
                with pytest.raises(DomainError) as error:
                    await relay.submit_task(
                        user, task_type, payload(conversation.id, "第二条"))
                assert error.value.code == "conversation_busy"
                assert error.value.status == 409

        run_async(scenario())
        assert len(transport.posts()) == 1
        assert len(store.list_runs(user, conversation.id)) == 1
    finally:
        store.close()


def test_awaiting_permission_occupies_capacity_until_terminal(tmp_path):
    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        user = store.create_user("Owner")
        conversations = [
            store.create_conversation(user, f"会话 {index}")
            for index in range(3)]
        transport = CountingTransport()

        async def scenario() -> None:
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(store, client, accepted=ALL_TASK_TYPES)
                first = await relay.submit_task(
                    user, "chat", payload(conversations[0].id, "会等待权限"))
                await relay.submit_task(
                    user, "skill", payload(conversations[1].id, "占满预算"))
                store.mark_awaiting_permission(user, first["runId"])
                with pytest.raises(DomainError) as error:
                    await relay.submit_task(
                        user, "profile",
                        payload(conversations[2].id, "第三个"))
                assert error.value.code == "workspace_capacity"

                store.finish_run(user, first["runId"], "cancelled")
                await relay.submit_task(
                    user, "profile",
                    payload(conversations[2].id, "额度释放后接纳"))

        run_async(scenario())
        assert len(transport.posts()) == 3
    finally:
        store.close()


# --------------------------------------------------------------------------
# 未装配适配器
# --------------------------------------------------------------------------

@pytest.mark.parametrize("task_type", [
    entry.task_type for entry in TASK_ENTRIES if not entry.implemented])
def test_missing_adapter_returns_503_before_admission(tmp_path, task_type):
    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        user = store.create_user("Owner")
        conversation = store.create_conversation(user, "未装配")
        transport = CountingTransport()

        async def scenario() -> None:
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(store, client)
                with pytest.raises(DomainError) as error:
                    await relay.submit_task(
                        user, task_type, payload(conversation.id))
                assert error.value.code == "capability_unavailable"
                assert error.value.status == 503

        run_async(scenario())
        assert transport.calls == []
        assert store.list_runs(user, conversation.id) == []
    finally:
        store.close()


# --------------------------------------------------------------------------
# 短请求租约与回收
# --------------------------------------------------------------------------

def test_request_lease_defers_idle_collection_until_released(tmp_path):
    async def scenario() -> None:
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            clock = Clock()
            provisioner = FakeProvisioner()
            manager = RuntimeManager(store, provisioner, clock=clock,
                                     idle_seconds=900, lease_seconds=120)
            await manager.ensure_ready(user.workspace_id, 1)
            lease = store.get_worker_lease(user.workspace_id)
            clock.value = 1901
            assert await manager.heartbeat(
                user.workspace_id, generation=lease.generation,
                instance_id=lease.container_id, active_requests=0)

            request_lease = store.begin_worker_request_lease(
                user.workspace_id, 60, now=int(clock.value))
            assert await manager.collect_idle(clock.value) == []
            assert provisioner.stops == 0

            assert store.end_worker_request_lease(request_lease) is True
            assert await manager.collect_idle(clock.value) == [
                user.workspace_id]
            assert provisioner.stops == 1

    run_async(scenario())


def test_expired_request_lease_does_not_block_idle_collection(tmp_path):
    async def scenario() -> None:
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            clock = Clock()
            provisioner = FakeProvisioner()
            manager = RuntimeManager(store, provisioner, clock=clock,
                                     idle_seconds=900, lease_seconds=120)
            await manager.ensure_ready(user.workspace_id, 1)
            lease = store.get_worker_lease(user.workspace_id)
            clock.value = 1901
            assert await manager.heartbeat(
                user.workspace_id, generation=lease.generation,
                instance_id=lease.container_id, active_requests=0)
            store.begin_worker_request_lease(
                user.workspace_id, 60, now=int(clock.value))

            # 控制进程崩溃后没有 end 调用；过期租约必须自行失效
            clock.value = 1962
            assert await manager.collect_idle(clock.value) == [
                user.workspace_id]

    run_async(scenario())


def test_awaiting_permission_is_not_idle_collected(tmp_path):
    async def scenario() -> None:
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            clock = Clock()
            provisioner = FakeProvisioner()
            manager = RuntimeManager(store, provisioner, clock=clock,
                                     idle_seconds=900, lease_seconds=120)
            await manager.ensure_ready(user.workspace_id, 1)
            lease = store.get_worker_lease(user.workspace_id)
            conversation = store.create_conversation(user, "权限等待")
            run = store.reserve_run(
                user, conversation.id, "request-permission",
                "digest-permission", 1)
            store.mark_awaiting_permission(user, run.id)

            clock.value = 1901
            assert await manager.heartbeat(
                user.workspace_id, generation=lease.generation,
                instance_id=lease.container_id, active_requests=0)
            assert await manager.collect_idle(clock.value) == []
            assert provisioner.stops == 0

            store.finish_run(user, run.id, "done")
            assert await manager.collect_idle(clock.value) == [
                user.workspace_id]

    run_async(scenario())


def test_relay_releases_request_lease_when_upstream_fails(tmp_path):
    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        user = store.create_user("Owner")
        conversation = store.create_conversation(user, "断线")
        run = store.reserve_run(
            user, conversation.id, "request-failure", "digest-failure",
            REVISION)
        # 已绑定的 Run 才会走实例句柄；本测试验证上游失败时租约释放
        assert store.claim_run_dispatch(
            user, run.id, "nonce-failure",
            "container-" + user.workspace_id[:8])
        transport = CountingTransport(httpx.ConnectError("upstream down"))

        async def scenario() -> None:
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(store, client, clock=Clock())
                with pytest.raises(DomainError) as error:
                    await relay.events(user, run.id, after=0)
                assert error.value.code == "upstream_unavailable"
                assert error.value.status == 502
                assert store.count_worker_request_leases(
                    user.workspace_id, now=1000) == 0

        run_async(scenario())
    finally:
        store.close()


# --------------------------------------------------------------------------
# drain：拒绝新任务，保留已接受任务的结束与回放
# --------------------------------------------------------------------------

def test_drain_rejects_new_task_and_preserves_accepted_run(tmp_path):
    async def scenario() -> None:
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            first = store.create_conversation(user, "已接受")
            second = store.create_conversation(user, "drain 后")
            clock = Clock()
            provisioner = FakeProvisioner()
            runtime = RuntimeManager(store, provisioner, clock=clock,
                                     idle_seconds=900, lease_seconds=120)
            transport = CountingTransport()

            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(store, client, runtime=runtime,
                                   accepted=("chat",), clock=clock)
                accepted = await relay.submit(
                    user, payload(first.id, "先接受", "turn-1"))

                drain = asyncio.create_task(runtime.drain(user.workspace_id))
                for _ in range(200):
                    lease = store.get_worker_lease(user.workspace_id)
                    if lease is not None and lease.state == "draining":
                        break
                    await asyncio.sleep(0.005)
                assert store.get_worker_lease(
                    user.workspace_id).state == "draining"

                with pytest.raises(DomainError) as error:
                    await relay.submit(
                        user, payload(second.id, "应被拒绝", "turn-2"))
                assert error.value.code == "worker_draining"
                assert error.value.status == 503
                # drain 拒绝不得留下已占容量的空接纳
                assert store.list_runs(user, second.id) == []
                assert not drain.done()

                store.finish_run(user, accepted["runId"], "done")
                await asyncio.wait_for(drain, timeout=5)
                assert store.get_worker_lease(
                    user.workspace_id).state == "stopped"

                replay = await relay.events(user, accepted["runId"], after=0)
                assert replay["runId"] == accepted["runId"]
                assert store.get_run(user, accepted["runId"]).status == "done"

    run_async(scenario())


# --------------------------------------------------------------------------
# control 公开入口只走 relay
# --------------------------------------------------------------------------

class FakeRelay:
    def __init__(self) -> None:
        self.submissions: list[tuple[Identity, dict]] = []

    async def submit(self, actor: Identity, body: object) -> dict:
        assert isinstance(body, dict)
        self.submissions.append((actor, body))
        return {"runId": "run-fake", "state": "accepted", "duplicate": False}


def test_public_chat_route_admits_through_relay(tmp_path):
    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        conversation = store.create_conversation(member, "入口")
        auth = AuthService(store, clock=Clock())
        relay = FakeRelay()
        app = create_app(
            store=store, auth=auth, config=ControlConfig(public_origin=ORIGIN),
            relay_factory=lambda *args: relay)
        with TestClient(app, base_url=ORIGIN) as client:
            invite = auth.create_invite(admin, member.user_id)
            exchanged = client.post(
                "/api/auth/exchange", json={"token": invite},
                headers={"Origin": ORIGIN})
            assert exchanged.status_code == 200
            csrf = exchanged.json()["csrfToken"]
            response = client.post(
                "/api/chat",
                json={"sessionId": conversation.id, "message": "你好"},
                headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
            assert response.status_code == 202
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["runId"] == "run-fake"
            assert relay.submissions[0][1]["message"] == "你好"
    finally:
        store.close()


def test_unwired_task_routes_remain_503(tmp_path):
    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        auth = AuthService(store, clock=Clock())
        app = create_app(
            store=store, auth=auth, config=ControlConfig(public_origin=ORIGIN))
        with TestClient(app, base_url=ORIGIN) as client:
            invite = auth.create_invite(admin, member.user_id)
            exchanged = client.post(
                "/api/auth/exchange", json={"token": invite},
                headers={"Origin": ORIGIN})
            csrf = exchanged.json()["csrfToken"]
            headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}

            for method, path, body in (
                    ("post", "/api/skill",
                     {"skill": "x", "input": "y"}),
                    ("post", "/api/profile/build", {"name": "p"}),
                    ("post", "/api/publish/xiaohongshu", {"title": "t"}),
                    ("post", "/api/login/xiaohongshu", {}),
                    ("get", "/api/analytics/xiaohongshu", None)):
                if method == "get":
                    response = client.get(path, headers=headers)
                else:
                    response = client.post(path, json=body, headers=headers)
                assert response.status_code == 503, path
                assert response.json()["code"] == "capability_unavailable"
    finally:
        store.close()
