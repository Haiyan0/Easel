"""Task 10 R1 返工：后台对账、drain 收尾、SSE 撤销与幂等/默认头。

边界：

* 不依赖浏览器 events/stop：后台对账在任务自然结束后写控制库终态、
  释放一次容量并撤销 broker token；登录撤销/禁用用户后同样收敛。
* 派发结果未知（POST 502）不自动重发；按原实例查询，worker 未接纳则
  interrupted，重试只返回原 Run。
* drain 拒绝新任务，但既有 Run 的停止、回答与回放仍使用原实例句柄。
* 慢上游阻塞事件页时，撤销仍必须在 5 秒内关闭流并清理在途请求。
* 配置更新后同输入重试返回原 Run/原快照，不读新配置、不要求新 worker。
* 异步 relay 清洗最终 Request 的默认认证/头，并固定重建内部身份。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.control.auth import AuthService
from easel.control.proxy import PinnedRunConfig, WorkerRelay
from easel.control.reconcile import RunReconciler
from easel.control.runtime import RuntimeManager, RuntimeUnavailable
from easel.control.sessions import SessionService
from easel.control.store import Store
from easel.control.stream import run_event_stream
from easel.control.types import DomainError, Identity, WorkerHandle, WorkspacePaths
from easel.worker.runs import RunManager
from easel.worker.sessions import WorkspaceSessionStore

REVISION = 7
CLOCK = 1000
ORIGIN = "https://easel.test"


def make_paths(tmp_path: Path, name: str = "a") -> WorkspacePaths:
    paths = WorkspacePaths(
        code_root=tmp_path / name / "code",
        workspace_root=tmp_path / name / "workspace",
        state_root=tmp_path / name / "state",
        home=tmp_path / name / "home",
    )
    for root in (paths.code_root, paths.workspace_root,
                 paths.state_root, paths.home):
        root.mkdir(parents=True, exist_ok=True)
    return paths


def make_manager(paths: WorkspacePaths, workspace_id: str,
                 key: Ed25519PrivateKey, executor) -> RunManager:
    return RunManager(
        workspace_id=workspace_id, paths=paths,
        public_key=key.public_key(),
        sessions=WorkspaceSessionStore(paths.state_root, workspace_id),
        executor=executor,
        base_env={"PATH": "C:\\bin"},
        clock=lambda: CLOCK)


class ImmediateExecutor:
    def __init__(self, answer: str = "完成") -> None:
        self.answer = answer
        self.prompts = 0

    async def __call__(self, request, context, bind, emit, ask):
        self.prompts += 1
        emit("token", self.answer)
        return self.answer


class BlockingExecutor:
    def __init__(self) -> None:
        self.prompts = 0
        self.gate = asyncio.Event()

    async def __call__(self, request, context, bind, emit, ask):
        self.prompts += 1
        await self.gate.wait()
        return "结束"


class AskingExecutor:
    def __init__(self) -> None:
        self.prompts = 0

    async def __call__(self, request, context, bind, emit, ask):
        self.prompts += 1
        await ask([{"questionId": "allow", "question": "允许？",
                    "options": ["yes", "no"]}])
        return "已回答"


class AskOrBlockExecutor:
    """按输入区分：一问一答，或阻塞等待停止。"""

    def __init__(self) -> None:
        self.prompts = 0
        self.gate = asyncio.Event()

    async def __call__(self, request, context, bind, emit, ask):
        self.prompts += 1
        if request.prompt.startswith("回答"):
            await ask([{"questionId": "allow", "question": "允许？",
                        "options": ["yes", "no"]}])
            return "已回答"
        await self.gate.wait()
        return "结束"


class WorkerTransport(httpx.AsyncBaseTransport):
    """把 relay 内部 HTTP 调用接到真实 RunManager。"""

    def __init__(self, managers: dict[str, RunManager], *,
                 fail_posts: bool = False,
                 lose_post_response: bool = False) -> None:
        self.managers = managers
        self.fail_posts = fail_posts
        self.lose_post_response = lose_post_response
        self.calls: list[tuple[str, str]] = []

    async def handle_async_request(
            self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if self.fail_posts and request.method == "POST" and re.fullmatch(
                r"/internal/runs/[^/]+", request.url.path):
            raise httpx.ConnectError("worker unreachable", request=request)
        workspace = request.headers.get("x-easel-workspace-id", "")
        manager = self.managers.get(workspace)
        if manager is None:
            return httpx.Response(
                403, json={"detail": "worker_rejected"}, request=request)
        body = await request.aread()
        payload = json.loads(body) if body else {}
        try:
            status, result = await self._dispatch(manager, request, payload)
        except DomainError as error:
            return httpx.Response(
                error.status, json={"detail": error.code}, request=request)
        if (self.lose_post_response and request.method == "POST"
                and re.fullmatch(r"/internal/runs/[^/]+",
                                 request.url.path)):
            # worker 已接纳并开始执行，但响应在回程丢失
            raise httpx.ReadError("response lost", request=request)
        return httpx.Response(status, json=result, request=request)

    async def _dispatch(self, manager: RunManager, request: httpx.Request,
                        payload: dict) -> tuple[int, object]:
        path = request.url.path
        match = re.fullmatch(r"/internal/runs/([^/]+)", path)
        if request.method == "POST" and match:
            return 202, await manager.submit(
                match.group(1), payload.get("envelope"),
                payload.get("request"))
        match = re.fullmatch(r"/internal/runs/([^/]+)/events", path)
        if request.method == "GET" and match:
            after = int(request.url.params.get("after", "0"))
            return 200, manager.events(match.group(1), after)
        match = re.fullmatch(r"/internal/runs/([^/]+)/stop", path)
        if request.method == "POST" and match:
            return 200, await manager.stop(match.group(1))
        match = re.fullmatch(
            r"/internal/runs/([^/]+)/questions/([^/]+)/answer", path)
        if request.method == "POST" and match:
            return 200, await manager.answer(
                match.group(1), match.group(2), payload.get("answers"))
        match = re.fullmatch(r"/internal/runs/([^/]+)/questions/status", path)
        if request.method == "POST" and match:
            return 200, manager.question_status(
                match.group(1), payload.get("questionIds") or [])
        match = re.fullmatch(r"/internal/sessions/([^/]+)/messages", path)
        if request.method == "GET" and match:
            return 200, manager.messages(
                match.group(1),
                limit=int(request.url.params.get("limit", "100")),
                offset=int(request.url.params.get("offset", "0")))
        return 404, {"detail": "not_found"}

    def task_posts(self, run_id: str | None = None) -> list[tuple[str, str]]:
        return [
            call for call in self.calls if call[0] == "POST"
            and re.fullmatch(r"/internal/runs/[^/]+", call[1])
            and (run_id is None or call[1] == f"/internal/runs/{run_id}")]

    def stop_posts(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] == "POST"
                and call[1].endswith("/stop")]


class StaticRuntime:
    def __init__(self, revision: int = REVISION) -> None:
        self.revision = revision
        self.calls = 0

    async def ensure_ready(self, workspace_id: str,
                           config_version: int) -> WorkerHandle:
        self.calls += 1
        if config_version != self.revision:
            raise RuntimeUnavailable("worker_config_unavailable", 503)
        return WorkerHandle(workspace_id, "container-static",
                            "http://worker.test:7860",
                            "service-token", config_version)

    async def bound_handle(self, workspace_id: str, container_id: str,
                           config_version: int) -> WorkerHandle | None:
        if container_id != "container-static" or config_version != self.revision:
            return None
        return WorkerHandle(workspace_id, container_id,
                            "http://worker.test:7860", "service-token",
                            config_version)


class FakeProvisioner:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.fail_stops = False
        self.handles: dict[str, WorkerHandle] = {}

    async def ensure(self, workspace_id: str,
                     config_version: int) -> WorkerHandle:
        self.starts += 1
        handle = WorkerHandle(
            workspace_id, f"container-{self.starts}",
            "http://worker.test:7860", f"service-token-{self.starts}",
            config_version)
        self.handles[workspace_id] = handle
        return handle

    async def stop(self, workspace_id: str, instance_id: str) -> None:
        if self.fail_stops:
            raise OSError("stop failed")
        self.stops += 1

    async def inspect(self, workspace_id: str) -> dict:
        handle = self.handles[workspace_id]
        return {
            "managed": True, "ready": True,
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


class FakeBroker:
    def __init__(self) -> None:
        self.revoked: list[tuple[str, str]] = []

    def revoke_run(self, run_id: str, workspace_id: str) -> None:
        self.revoked.append((run_id, workspace_id))


class MutableProvider:
    def __init__(self, revision: int = REVISION) -> None:
        self.revision = revision
        self.calls = 0

    def __call__(self, _actor: Identity) -> PinnedRunConfig:
        self.calls += 1
        return PinnedRunConfig(
            revision=self.revision,
            routes={"broker_token": f"token-{self.revision}"},
            agent={"runtime": "acp", "command": ["fixture", "acp"],
                   "sandbox": "read-only"})


def make_relay(store: Store, client: httpx.AsyncClient, *,
               runtime=None, broker=None, provider=None,
               signing_key=None, dispatch_grace: int = 60,
               snapshot_ttl: int = 1800,
               db_runner=None) -> WorkerRelay:
    return WorkerRelay(
        runtime or StaticRuntime(), SessionService(store), AuthService(store),
        store=store,
        signing_key=signing_key or Ed25519PrivateKey.generate(),
        http_client=client, config_provider=provider or MutableProvider(),
        broker=broker, dispatch_grace=dispatch_grace,
        snapshot_ttl=snapshot_ttl, db_runner=db_runner)


def payload(conversation_id: str, message: str = "执行任务",
            turn_id: str | None = None) -> dict:
    body = {"sessionId": conversation_id, "message": message}
    if turn_id is not None:
        body["turnId"] = turn_id
    return body


async def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0.01)


def question_event_id(manager: RunManager, run_id: str) -> tuple[str, str]:
    for event in manager.events(run_id, after=0)["events"]:
        if event["event"] == "question":
            data = json.loads(event["data"])
            return data["id"], data["questions"][0]["questionId"]
    raise AssertionError("没有权限问题")


# ---------------------------------------------------------------------------
# R1-01：后台对账
# ---------------------------------------------------------------------------


def test_reconciler_finishes_runs_without_browser_requests(tmp_path):
    async def scenario() -> None:
        with Store.open(tmp_path / "control.sqlite3") as store:
            user = store.create_user("Owner")
            first = store.create_conversation(user, "一")
            second = store.create_conversation(user, "二")
            key = Ed25519PrivateKey.generate()
            executor = ImmediateExecutor()
            manager = make_manager(
                make_paths(tmp_path), user.workspace_id, key, executor)
            transport = WorkerTransport({user.workspace_id: manager})
            broker = FakeBroker()
            runtime = RuntimeManager(
                store, FakeProvisioner(), clock=time.time)
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(
                    store, client, broker=broker, signing_key=key,
                    runtime=runtime)
                reconciler = RunReconciler(relay, interval=0.05)
                await reconciler.start()
                try:
                    run_a = (await relay.submit(
                        user, payload(first.id)))["runId"]
                    run_b = (await relay.submit(
                        user, payload(second.id)))["runId"]
                finally:
                    await reconciler.stop()
                # 模拟 control 重启：新连接 + 新 relay/reconciler 从控制库恢复
                with Store.open(tmp_path / "control.sqlite3") as restarted:
                    async with httpx.AsyncClient(
                            transport=transport) as client2:
                        runtime2 = RuntimeManager(
                            restarted, FakeProvisioner(), clock=time.time)
                        relay2 = make_relay(
                            restarted, client2, broker=broker,
                            signing_key=key, runtime=runtime2)
                        reconciler2 = RunReconciler(relay2, interval=0.05)
                        await reconciler2.start()
                        try:
                            # 不使用 events/stop，也不依赖任何浏览器连接
                            await wait_until(
                                lambda: restarted.get_run(
                                    user, run_a).status == "done"
                                and restarted.get_run(
                                    user, run_b).status == "done",
                                timeout=5)
                            assert restarted.count_active_runs() == 0
                            assert sorted(broker.revoked) == sorted(
                                [(run_a, user.workspace_id),
                                 (run_b, user.workspace_id)])
                            rows = restarted._connection.execute(
                                "SELECT release_count FROM runs"
                                " WHERE workspace_id = ?",
                                (user.workspace_id,)).fetchall()
                            assert [row["release_count"]
                                    for row in rows] == [1, 1]

                            third = restarted.create_conversation(user, "三")
                            accepted = await relay2.submit(
                                user, payload(third.id, "第三"))
                            assert accepted["runId"]
                        finally:
                            await reconciler2.stop()
            await manager.aclose()

    asyncio.run(scenario())


def test_reconciler_finishes_after_user_disabled_and_session_revoked(
        tmp_path):
    async def scenario() -> None:
        with Store.open(tmp_path / "control.sqlite3") as store:
            user = store.create_user("Owner")
            conversation = store.create_conversation(user, "禁用")
            key = Ed25519PrivateKey.generate()
            manager = make_manager(
                make_paths(tmp_path), user.workspace_id, key,
                ImmediateExecutor())
            transport = WorkerTransport({user.workspace_id: manager})
            broker = FakeBroker()
            auth = AuthService(store)
            runtime = RuntimeManager(
                store, FakeProvisioner(), clock=time.time)
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(
                    store, client, broker=broker, signing_key=key,
                    runtime=runtime)
                reconciler = RunReconciler(relay, interval=0.05)
                await reconciler.start()
                try:
                    run_id = (await relay.submit(
                        user, payload(conversation.id)))["runId"]
                    store.disable_user(user.user_id)
                    auth.revoke_all(user.user_id)
                    await wait_until(
                        lambda: store.get_run(user, run_id).status == "done",
                        timeout=5)
                    assert store.count_active_runs() == 0
                    assert broker.revoked == [(run_id, user.workspace_id)]
                finally:
                    await reconciler.stop()
            await manager.aclose()

    asyncio.run(scenario())


def test_dispatch_unknown_converges_without_republish(tmp_path):
    async def scenario() -> None:
        with Store.open(tmp_path / "control.sqlite3") as store:
            user = store.create_user("Owner")
            conversation = store.create_conversation(user, "派发未知")
            key = Ed25519PrivateKey.generate()
            manager = make_manager(
                make_paths(tmp_path), user.workspace_id, key,
                ImmediateExecutor())
            transport = WorkerTransport(
                {user.workspace_id: manager}, fail_posts=True)
            broker = FakeBroker()
            provisioner = FakeProvisioner()
            runtime = RuntimeManager(store, provisioner, clock=lambda: CLOCK)
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(
                    store, client, runtime=runtime, broker=broker,
                    signing_key=key, dispatch_grace=0)
                with pytest.raises(DomainError) as unavailable:
                    await relay.submit(
                        user, payload(conversation.id, turn_id="turn-x"))
                assert unavailable.value.code == "upstream_unavailable"
                rows = store.list_runs(user, conversation.id)
                assert len(rows) == 1
                run_id = rows[0].id
                assert store.get_run(user, run_id).status == "reserved"

                reconciler = RunReconciler(relay, interval=0.05)
                await reconciler.start()
                try:
                    await wait_until(
                        lambda: store.get_run(user, run_id).status
                        == "interrupted", timeout=5)
                    assert broker.revoked == [(run_id, user.workspace_id)]
                    retried = await relay.submit(
                        user, payload(conversation.id, turn_id="turn-x"))
                    assert retried == {"runId": run_id,
                                       "state": "interrupted",
                                       "duplicate": True}
                    assert transport.task_posts(run_id) == [
                        ("POST", f"/internal/runs/{run_id}")]
                finally:
                    await reconciler.stop()
            await manager.aclose()

    asyncio.run(scenario())


def test_lost_dispatch_response_converges_from_worker_state(tmp_path):
    """POST 已被 worker 接纳但响应丢失：按原实例查询，不重复派发。"""
    async def scenario() -> None:
        with Store.open(tmp_path / "control.sqlite3") as store:
            user = store.create_user("Owner")
            conversation = store.create_conversation(user, "响应丢失")
            key = Ed25519PrivateKey.generate()
            manager = make_manager(
                make_paths(tmp_path), user.workspace_id, key,
                ImmediateExecutor())
            transport = WorkerTransport(
                {user.workspace_id: manager}, lose_post_response=True)
            broker = FakeBroker()
            runtime = RuntimeManager(
                store, FakeProvisioner(), clock=time.time)
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(
                    store, client, runtime=runtime, broker=broker,
                    signing_key=key, dispatch_grace=0)
                with pytest.raises(DomainError) as lost:
                    await relay.submit(
                        user, payload(conversation.id, turn_id="lost-turn"))
                assert lost.value.code == "upstream_unavailable"
                run_id = store.list_runs(user, conversation.id)[0].id
                await wait_until(lambda: manager.state(run_id) == "done")

                reconciler = RunReconciler(relay, interval=0.05)
                await reconciler.start()
                try:
                    await wait_until(
                        lambda: store.get_run(user, run_id).status == "done",
                        timeout=5)
                    assert broker.revoked == [(run_id, user.workspace_id)]
                    retried = await relay.submit(
                        user, payload(conversation.id, turn_id="lost-turn"))
                    assert retried == {"runId": run_id, "state": "done",
                                       "duplicate": True}
                    assert transport.task_posts(run_id) == [
                        ("POST", f"/internal/runs/{run_id}")]
                finally:
                    await reconciler.stop()
            await manager.aclose()

    asyncio.run(scenario())


class FlakyTransport(WorkerTransport):
    """可切换事件/停止故障与停止返回状态。"""

    def __init__(self, managers, **kwargs) -> None:
        super().__init__(managers, **kwargs)
        self.fail_events = False
        self.fail_stop = False
        self.stop_state: str | None = None
        self.target: str | None = None

    def _is_target(self, request) -> bool:
        return bool(self.target) and \
            f"/internal/runs/{self.target}" in request.url.path

    async def handle_async_request(self, request):
        path = request.url.path
        target = self._is_target(request)
        if (target and self.fail_events and request.method == "GET"
                and path.endswith("/events")):
            raise httpx.ConnectError("events unavailable", request=request)
        if (target and request.method == "POST" and path.endswith("/stop")
                and self.fail_stop):
            raise httpx.ConnectError("stop unavailable", request=request)
        if (target and request.method == "POST" and path.endswith("/stop")
                and self.stop_state is not None):
            return httpx.Response(
                200, json={"runId": path.split("/")[3],
                           "state": self.stop_state},
                request=request)
        return await super().handle_async_request(request)


def test_deadline_without_confirmed_stop_keeps_capacity(tmp_path):
    """GET/stop 失败或 stop 返回 running 时不得释放仍执行的任务。"""
    async def scenario() -> None:
        with Store.open(tmp_path / "control.sqlite3") as store:
            user = store.create_user("Owner")
            conversation = store.create_conversation(user, "到期")
            key = Ed25519PrivateKey.generate()
            executor = BlockingExecutor()
            manager = make_manager(
                make_paths(tmp_path), user.workspace_id, key, executor)
            transport = FlakyTransport({user.workspace_id: manager})
            broker = FakeBroker()
            provisioner = FakeProvisioner()
            runtime = RuntimeManager(store, provisioner, clock=time.time)
            now = {"value": time.time()}

            def clock() -> float:
                return now["value"]

            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(
                    store, client, runtime=runtime, broker=broker,
                    signing_key=key, snapshot_ttl=30)
                relay._system_clock = clock
                run_id = (await relay.submit(
                    user, payload(conversation.id)))["runId"]
                transport.target = run_id
                await wait_until(lambda: manager.state(run_id) == "running")
                original_token = json.loads(
                    store.get_run_snapshot(user, run_id))["routes"][
                        "broker_token"]
                now["value"] += 1000  # 超过快照有效期 + 收尾余量

                # GET 与 stop 都失败：执行状态未知，容量必须保持
                transport.fail_events = True
                transport.fail_stop = True
                await relay.reconcile_once()
                assert store.get_run(user, run_id).status == "running"
                assert store.count_active_runs() == 1
                # 占满第二个容量后，第三个任务必须仍被拒绝
                second = store.create_conversation(user, "第二个")
                second_run = store.reserve_run(
                    user, second.id, "capacity-2", "digest-2", REVISION)
                assert store.count_active_runs() == 2
                third = store.create_conversation(user, "第三个")
                with pytest.raises(DomainError) as full:
                    await relay.submit(user, payload(third.id))
                assert full.value.code == "workspace_capacity"
                # 释放占位 Run，后续只观察目标 Run 的容量保持
                store.finish_run(user, second_run.id, "cancelled")
                assert store.count_active_runs() == 1
                # 截止后先撤销凭证限制新模型调用，但不释放任务预算
                assert broker.revoked == [(run_id, user.workspace_id)]
                assert original_token

                # 模拟 control 重启：新 relay 从控制库恢复待确认状态
                relay2 = make_relay(
                    store, client, runtime=runtime, broker=broker,
                    signing_key=key, snapshot_ttl=30)
                relay2._system_clock = clock

                # 恢复 GET，但 stop 仍失败：继续对账、继续占用
                transport.fail_events = False
                await relay2.reconcile_once()
                assert store.get_run(user, run_id).status == "running"
                assert store.count_active_runs() == 1

                # stop 返回 running/finalizing：仍不得释放
                transport.fail_stop = False
                transport.stop_state = "running"
                await relay2.reconcile_once()
                assert store.get_run(user, run_id).status == "running"
                assert store.count_active_runs() == 1

                # 停止确认后才释放一次
                transport.stop_state = "cancelled"
                await relay2.reconcile_once()
                assert store.get_run(user, run_id).status == "cancelled"
                assert store.count_active_runs() == 0
                assert (run_id, user.workspace_id) in broker.revoked
                accepted = await relay.submit(user, payload(third.id))
                assert accepted["runId"]
                assert store.count_active_runs() == 1
                rows = store._connection.execute(
                    "SELECT release_count FROM runs WHERE id = ?",
                    (run_id,)).fetchone()
                assert rows["release_count"] == 1
            executor.gate.set()
            await manager.aclose()

    asyncio.run(scenario())


def test_instance_gone_requires_confirmed_provisioner_stop(tmp_path):
    """原实例句柄不可用本身不是执行结束证据；确认停止后才释放。"""
    async def scenario() -> None:
        with Store.open(tmp_path / "control.sqlite3") as store:
            user = store.create_user("Owner")
            first = store.create_conversation(user, "旧一")
            second = store.create_conversation(user, "旧二")
            key = Ed25519PrivateKey.generate()
            executor = BlockingExecutor()
            manager = make_manager(
                make_paths(tmp_path), user.workspace_id, key, executor)
            transport = WorkerTransport({user.workspace_id: manager})
            broker = FakeBroker()
            provisioner = FakeProvisioner()
            runtime = RuntimeManager(store, provisioner, clock=time.time)
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(
                    store, client, runtime=runtime, broker=broker,
                    signing_key=key)

                async def replace_instance() -> None:
                    store.stop_worker(user.workspace_id, int(time.time()))
                    handle = await runtime.ensure_ready(
                        user.workspace_id, REVISION)
                    assert handle.container_id

                run_a = (await relay.submit(
                    user, payload(first.id)))["runId"]
                await wait_until(lambda: manager.state(run_a) == "running")
                await replace_instance()
                provisioner.fail_stops = True
                await relay.reconcile_once()
                assert store.get_run(user, run_a).status == "running"
                assert store.count_active_runs() == 1
                provisioner.fail_stops = False
                await relay.reconcile_once()
                assert store.get_run(user, run_a).status == "interrupted"
                assert store.count_active_runs() == 0

                # 第二个 Run：provisioner 明确停止成功才释放
                run_b = (await relay.submit(
                    user, payload(second.id)))["runId"]
                await wait_until(lambda: manager.state(run_b) == "running")
                await replace_instance()
                provisioner.stops = 0
                await relay.reconcile_once()
                assert provisioner.stops >= 1
                assert store.get_run(user, run_b).status == "interrupted"
                assert store.count_active_runs() == 0
            executor.gate.set()
            await manager.aclose()

    asyncio.run(scenario())


def test_concurrent_prepare_has_single_owner_and_failure_cannot_interrupt(
        tmp_path):
    """并发同请求只有一个准备者；另一方的签发失败不得中断已派发 Run。"""
    from easel.control.broker import BrokerService
    from easel.control.cli import build_relay_factory
    from easel.control.config import ConfigService, MASTER_KEY_BYTES
    from easel.control.egress import EgressGateway
    from easel.control.proxy import BrokerDestination

    class StaticRuntime:
        async def ensure_ready(self, workspace_id, config_version):
            return WorkerHandle(workspace_id, "container-prepare",
                                "http://worker.test", "service-token",
                                config_version)

        async def bound_handle(self, workspace_id, container_id,
                               config_version):
            return WorkerHandle(workspace_id, container_id,
                                "http://worker.test", "service-token",
                                config_version)

    class SecondIssueFails:
        def __init__(self, real) -> None:
            self.real = real
            self.issues = 0

        def ensure_issuable(self, actor, channel):
            return self.real.ensure_issuable(actor, channel)

        def issue(self, actor, run_id, channel):
            self.issues += 1
            if self.issues > 1:
                raise DomainError("capability_unavailable", 503)
            return self.real.issue(actor, run_id, channel)

        def revoke_run(self, run_id, workspace_id):
            return self.real.revoke_run(run_id, workspace_id)

    class CaptureWorker(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.posts = 0

        async def handle_async_request(self, request):
            if request.method == "POST":
                self.posts += 1
            return httpx.Response(
                202, json={"state": "running"}, request=request)

    async def scenario() -> None:
        with Store.open(tmp_path / "control.sqlite3") as store:
            admin = store.create_user("Owner", "admin")
            member = store.create_user("Member")
            conversation = store.create_conversation(member, "并发准备")
            config = ConfigService(store, b"k" * MASTER_KEY_BYTES)
            config.publish(admin, {"defaults": {"chat": {
                "provider": "deepseek", "model": "deepseek-chat",
                "base_url": "https://chat.example/v1",
                "protocol": "openai",
                "secret": {"CHAT_API_KEY": "chat-secret"},
                "options": {},
            }}}, expected_version=0)
            broker = SecondIssueFails(BrokerService(
                store, config,
                egress=EgressGateway(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json={})))))
            key = Ed25519PrivateKey.generate()
            factory = build_relay_factory(
                signing_key=key, revision=REVISION,
                agent={"runtime": "acp", "command": ["managed-agent"],
                       "sandbox": "read-only", "timeout": 60},
                broker_destination=BrokerDestination(
                    "https://control.internal", "chat"))
            transport = CaptureWorker()
            lookups = {"n": 0}
            both_looked = asyncio.Event()
            reserves = {"n": 0}
            both_reserved = asyncio.Event()
            snapshots = {"n": 0}
            both_snapshots = asyncio.Event()

            def make_runner():
                async def runner(function, *args, **kwargs):
                    name = getattr(function, "__name__", "")
                    if name == "lookup_run":
                        result = function(*args, **kwargs)
                        lookups["n"] += 1
                        if lookups["n"] == 2:
                            both_looked.set()
                        await asyncio.wait_for(both_looked.wait(), timeout=0.5)
                        return result
                    if name == "reserve_run":
                        result = function(*args, **kwargs)
                        reserves["n"] += 1
                        if reserves["n"] == 2:
                            both_reserved.set()
                        await asyncio.wait_for(
                            both_reserved.wait(), timeout=0.5)
                        return result
                    if name == "get_run_snapshot":
                        result = function(*args, **kwargs)
                        snapshots["n"] += 1
                        if snapshots["n"] == 2:
                            both_snapshots.set()
                        try:
                            await asyncio.wait_for(
                                both_snapshots.wait(), timeout=0.05)
                        except asyncio.TimeoutError:
                            pass
                        return result
                    return function(*args, **kwargs)
                return runner

            clients = [httpx.AsyncClient(transport=transport)
                       for _ in range(2)]
            relays = [
                factory(StaticRuntime(), SessionService(store),
                        AuthService(store), store, client, broker, runner)
                for client, runner in zip(clients, [make_runner(),
                                                     make_runner()])
            ]
            body = {"sessionId": conversation.id,
                    "turnId": "same-turn", "message": "同一个请求"}
            try:
                results = await asyncio.gather(
                    relays[0].submit(member, dict(body)),
                    relays[1].submit(member, dict(body)),
                    return_exceptions=True)
            finally:
                for client in clients:
                    await client.aclose()

            assert all(isinstance(item, dict) for item in results), results
            assert {item["runId"] for item in results} == {
                results[0]["runId"]}
            assert sorted(item["duplicate"] for item in results) == [
                False, True]
            assert broker.issues == 1, "并发提交不得铸造第二个 token"
            assert transport.posts == 1
            run_id = results[0]["runId"]
            assert store.get_run(member, run_id).status == "running"
            assert store.count_active_runs() == 1

    asyncio.run(scenario())


def test_stale_pending_scan_cannot_interrupt_dispatched_run(tmp_path):
    """后台基于旧 pending 行的收尾必须 CAS；已准备/派发的 Run 不受改写。"""
    async def scenario() -> None:
        path = tmp_path / "control.sqlite3"
        scan_store = Store.connect(path)
        submit_store = Store.connect(path)
        try:
            user = scan_store.create_user("Owner")
            conversation = scan_store.create_conversation(user, "交错")
            key = Ed25519PrivateKey.generate()
            broker = FakeBroker()
            runtime = StaticRuntime()
            reserved = asyncio.Event()
            scanned = asyncio.Event()
            dispatched = asyncio.Event()

            class CaptureWorker(httpx.AsyncBaseTransport):
                def __init__(self) -> None:
                    self.posts = 0

                async def handle_async_request(self, request):
                    if request.method == "POST":
                        self.posts += 1
                    dispatched.set()
                    return httpx.Response(
                        202, json={"state": "running"}, request=request)

            transport = CaptureWorker()

            async def submit_runner(function, *args, **kwargs):
                if getattr(function, "__name__", "") == "reserve_run":
                    result = function(*args, **kwargs)
                    reserved.set()
                    await asyncio.wait_for(scanned.wait(), timeout=5)
                    return result
                return function(*args, **kwargs)

            async def scan_runner(function, *args, **kwargs):
                name = getattr(function, "__name__", "")
                if name == "list_active_run_bindings":
                    result = function(*args, **kwargs)
                    scanned.set()
                    return result
                if name == "interrupt_unstarted_run":
                    # 旧扫描已决定收尾；等待合法提交完成派发后再执行 CAS
                    await asyncio.wait_for(dispatched.wait(), timeout=5)
                    return function(*args, **kwargs)
                return function(*args, **kwargs)

            async with httpx.AsyncClient(
                    transport=transport) as submit_client, \
                    httpx.AsyncClient(
                        transport=httpx.MockTransport(
                            lambda request: httpx.Response(
                                200, json={"state": "running",
                                           "events": [],
                                           "lastEventId": 0}))) as scan_client:
                scan_relay = make_relay(
                    scan_store, scan_client, runtime=runtime,
                    broker=broker, signing_key=key, db_runner=scan_runner)
                scan_relay._system_clock = lambda: time.time() + 100
                submit_relay = make_relay(
                    submit_store, submit_client, runtime=runtime,
                    signing_key=key, db_runner=submit_runner)

                submit_task = asyncio.create_task(submit_relay.submit(
                    user, payload(conversation.id, turn_id="race-turn")))
                await asyncio.wait_for(reserved.wait(), timeout=5)
                reconcile_task = asyncio.create_task(
                    scan_relay.reconcile_once())
                submitted = await asyncio.wait_for(submit_task, timeout=5)
                result = await asyncio.wait_for(reconcile_task, timeout=5)
                run_id = submitted["runId"]
                assert result["finished"] == 0
                assert scan_store.get_run(user, run_id).status == "running"
                assert scan_store.count_active_runs() == 1
                assert scan_store.get_run_snapshot(user, run_id) is not None
                assert transport.posts == 1
                assert broker.revoked == [], "旧扫描不得撤销成功方 token"
                # 真正未准备/已过期的 Run 仍可结算一次
                stale = scan_store.reserve_run(
                    user, scan_store.create_conversation(user, "未准备").id,
                    "stale-turn", "stale-digest", REVISION)
                scan_store.claim_run_preparation(
                    user, stale.id, "expired-owner",
                    int(time.time()) - 100, 1)
                await scan_relay.reconcile_once()
                assert scan_store.get_run(user, stale.id).status == \
                    "interrupted"
        finally:
            scan_store.close()
            submit_store.close()

    asyncio.run(scenario())


def test_prepare_owner_crash_is_recovered_without_republish(tmp_path):
    """准备所有权持有者崩溃：后台中断、不接管重发，重试只读原 admission。"""
    from easel.worker.config import run_input_digest

    async def scenario() -> None:
        with Store.open(tmp_path / "control.sqlite3") as store:
            user = store.create_user("Owner")
            conversation = store.create_conversation(user, "准备崩溃")
            digest = run_input_digest(
                conversation_id=conversation.id, task_type="chat",
                message="执行任务", attachments=[], persona=None)
            run = store.reserve_run(
                user, conversation.id, "crash-turn", digest, REVISION)
            assert store.claim_run_preparation(
                user, run.id, "dead-preparer", int(time.time()), 5)
            broker = FakeBroker()
            transport = WorkerTransport({})
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(
                    store, client, broker=broker,
                    signing_key=Ed25519PrivateKey.generate(),
                    dispatch_grace=0)
                relay._system_clock = lambda: time.time() + 100
                await relay.reconcile_once()
                assert store.get_run(user, run.id).status == "interrupted"
                assert store.count_active_runs() == 0
                # token 撤销与容量释放在 Store CAS 同一事务内完成；该路径
                # 没有真实 broker 快照，FakeBroker 不是证据来源
                assert transport.calls == []

                retried = await relay.submit(
                    user, payload(conversation.id, turn_id="crash-turn"))
                assert retried == {"runId": run.id, "state": "interrupted",
                                   "duplicate": True}
                assert transport.calls == []

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# R1-02：drain 期间既有 Run 的收尾
# ---------------------------------------------------------------------------


def test_drain_allows_existing_run_stop_answer_and_replay(tmp_path):
    async def scenario() -> None:
        with Store.open(tmp_path / "control.sqlite3") as store:
            user = store.create_user("Owner")
            asking = store.create_conversation(user, "回答")
            blocking = store.create_conversation(user, "停止")
            key = Ed25519PrivateKey.generate()
            executor = AskOrBlockExecutor()
            manager = make_manager(
                make_paths(tmp_path), user.workspace_id, key, executor)
            transport = WorkerTransport({user.workspace_id: manager})
            broker = FakeBroker()
            provisioner = FakeProvisioner()
            runtime = RuntimeManager(store, provisioner, clock=lambda: CLOCK)
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(
                    store, client, runtime=runtime, broker=broker,
                    signing_key=key)
                ask_run = (await relay.submit(
                    user, payload(asking.id, "回答")))["runId"]
                stop_run = (await relay.submit(
                    user, payload(blocking.id, "停止")))["runId"]
                await wait_until(
                    lambda: manager.state(ask_run) == "awaiting_permission"
                    and manager.state(stop_run) == "running")

                drain = asyncio.create_task(
                    runtime.drain(user.workspace_id))
                await wait_until(
                    lambda: store.get_worker_lease(
                        user.workspace_id).state == "draining")
                try:
                    # 新任务在 drain 时拒绝
                    extra = store.create_conversation(user, "新任务")
                    with pytest.raises(DomainError) as rejected:
                        await relay.submit(user, payload(extra.id))
                    assert rejected.value.code == "worker_draining"

                    # 既有 Run 的事件、回答与停止仍用原实例句柄完成
                    replayed = await relay.events(user, ask_run, after=0)
                    assert replayed["state"] == "awaiting_permission"
                    question_id, _key = question_event_id(manager, ask_run)
                    answered = await relay.answer(
                        user, {"runId": ask_run, "questionId": question_id,
                               "answers": {"allow": ["yes"]}})
                    assert answered["ok"] is True
                    stopped = await relay.stop(
                        user, {"runId": stop_run})
                    assert stopped["state"] == "cancelled"
                    await wait_until(
                        lambda: manager.state(ask_run) == "done")
                    # 回放同步自然完成终态后，drain 才能结束
                    completed = await relay.events(user, ask_run, after=0)
                    assert completed["state"] == "done"
                    # 消息读取同样按原实例，不被 draining 拒绝
                    snapshot = await relay.messages(user, asking.id)
                    assert [item["role"] for item in snapshot["messages"]] \
                        == ["user", "assistant"]
                    await asyncio.wait_for(drain, timeout=5)
                finally:
                    if not drain.done():
                        drain.cancel()
                        await asyncio.gather(drain, return_exceptions=True)
                assert store.get_worker_lease(user.workspace_id).state == \
                    "stopped"
                assert store.count_active_runs() == 0
                # 停止后的回放仍可读取持久终态
                replay = await relay.events(user, stop_run, after=0)
                assert replay["state"] == "cancelled"
            executor.gate.set()
            await manager.aclose()

    asyncio.run(scenario())


def test_existing_run_never_routes_to_new_worker_instance(tmp_path):
    async def scenario() -> None:
        with Store.open(tmp_path / "control.sqlite3") as store:
            user = store.create_user("Owner")
            conversation = store.create_conversation(user, "旧实例")
            key = Ed25519PrivateKey.generate()
            executor = BlockingExecutor()
            manager = make_manager(
                make_paths(tmp_path), user.workspace_id, key, executor)
            transport = WorkerTransport({user.workspace_id: manager})
            broker = FakeBroker()
            provisioner = FakeProvisioner()
            runtime = RuntimeManager(store, provisioner, clock=lambda: CLOCK)
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(
                    store, client, runtime=runtime, broker=broker,
                    signing_key=key)
                run_id = (await relay.submit(
                    user, payload(conversation.id)))["runId"]
                await wait_until(lambda: manager.state(run_id) == "running")
                original = store.get_worker_lease(
                    user.workspace_id).container_id
                # 模拟原实例消失并启动了新代次
                store.stop_worker(user.workspace_id, CLOCK)
                new_handle = await runtime.ensure_ready(
                    user.workspace_id, REVISION)
                assert new_handle.container_id != original

                stopped = await relay.stop(user, {"runId": run_id})
                assert stopped["state"] == "interrupted"
                assert store.get_run(user, run_id).status == "interrupted"
                assert transport.stop_posts() == []
                assert broker.revoked == [(run_id, user.workspace_id)]
                replay = await relay.events(user, run_id, after=0)
                assert replay["state"] == "interrupted"
                assert replay["events"] == []
                # 新实例不能伪装成原 Run 被停止或查询
                assert transport.stop_posts() == []
            executor.gate.set()
            await manager.aclose()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# R1-03：慢上游不得遮蔽撤销
# ---------------------------------------------------------------------------


def test_slow_event_request_cannot_mask_revocation():
    class SlowRelay:
        def __init__(self) -> None:
            self.cancelled = False
            self.calls = 0

        async def events(self, actor, run_id, after=0):
            self.calls += 1
            try:
                await asyncio.sleep(8)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return {"runId": run_id, "state": "running",
                    "events": [], "lastEventId": 0}

    async def scenario() -> None:
        relay = SlowRelay()
        checks = 0

        async def recheck() -> None:
            nonlocal checks
            checks += 1
            raise DomainError("unauthorized", 401)

        page = {"runId": "run-a", "state": "running",
                "events": [], "lastEventId": 0}
        start = time.monotonic()
        frames = []
        async for chunk in run_event_stream(
                relay=relay, actor=Identity("u", "w", "member"),
                run_id="run-a", page=page, recheck=recheck,
                auth_check_seconds=4.0, poll_interval=0.01):
            frames.append(chunk)
        elapsed = time.monotonic() - start
        assert elapsed < 5.0, f"撤销后仍阻塞 {elapsed:.2f}s"
        assert checks >= 1
        assert frames == []
        assert relay.cancelled is True, "在途事件请求必须被取消释放"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# R1-07：配置更新不破坏幂等
# ---------------------------------------------------------------------------


def test_retry_returns_original_run_after_config_update(tmp_path):
    async def scenario() -> None:
        database = tmp_path / "control.sqlite3"
        store = Store.connect(database)
        try:
            user = store.create_user("Owner")
            conversation = store.create_conversation(user, "幂等")
            provider = MutableProvider(REVISION)
            runtime = StaticRuntime(REVISION)

            class CountingTransport(httpx.AsyncBaseTransport):
                def __init__(self) -> None:
                    self.posts = 0

                async def handle_async_request(self, request):
                    if request.method == "POST":
                        self.posts += 1
                    return httpx.Response(
                        202, json={"state": "accepted"}, request=request)

            transport = CountingTransport()
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(
                    store, client, runtime=runtime, provider=provider)
                first = await relay.submit(
                    user, payload(conversation.id, turn_id="turn-1"))
                run_id = first["runId"]
                assert provider.calls == 1

                # 管理员更新配置且新版本 worker 尚不可用
                provider = MutableProvider(REVISION + 1)
                runtime.revision = REVISION + 1
                unavailable_runtime = StaticRuntime(REVISION)
                relay._config_provider = provider
                relay._runtime = unavailable_runtime
                retried = await relay.submit(
                    user, payload(conversation.id, turn_id="turn-1"))
                assert retried["runId"] == run_id
                assert retried["duplicate"] is True
                assert provider.calls == 0, "同输入重试不得读取新配置"
                assert unavailable_runtime.calls == 0, "不得要求新 worker"
                assert transport.posts == 1

                with pytest.raises(DomainError) as conflict:
                    await relay.submit(
                        user, payload(conversation.id, message="不同输入",
                                      turn_id="turn-1"))
                assert conflict.value.code == "request_conflict"
                assert provider.calls == 0
                assert transport.posts == 1

                # control 重启后（新 Store/relay）仍返回原 Run
                with Store.open(database) as restarted:
                    async with httpx.AsyncClient(
                            transport=transport) as new_client:
                        new_relay = make_relay(
                            restarted, new_client,
                            runtime=StaticRuntime(REVISION),
                            provider=MutableProvider(REVISION + 2))
                        again = await new_relay.submit(
                            user, payload(conversation.id,
                                          turn_id="turn-1"))
                        assert again["runId"] == run_id
                        assert transport.posts == 1
        finally:
            store.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# R1-09：异步 relay 的最终请求头清洗
# ---------------------------------------------------------------------------


def test_async_send_strips_client_default_credentials(tmp_path):
    captured: list[httpx.Headers] = []

    class Capture(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            captured.append(httpx.Headers(request.headers))
            return httpx.Response(200, json={"ok": True}, request=request)

    async def scenario() -> None:
        client = httpx.AsyncClient(
            transport=Capture(),
            headers={
                "Authorization": "Bearer default-secret",
                "Cookie": "session=default-secret",
                "Forwarded": "for=198.51.100.7",
                "X-Easel-Role": "admin",
                "X-Forwarded-For": "198.51.100.7",
            },
            cookies={"jar": "default-secret"},
            auth=httpx.BasicAuth("user", "password"))
        try:
            store = Store.open(tmp_path / "control.sqlite3")
            with store as control:
                user = control.create_user("Owner")
                conversation = control.create_conversation(user, "头")
                control.reserve_run(user, conversation.id, "r", "d", REVISION)
                relay = make_relay(control, client)
                handle = WorkerHandle(
                    user.workspace_id, "container-x",
                    "http://worker.test:7860", "service-token", REVISION)
                for _ in range(2):
                    response = await relay._send(
                        handle, "GET", "/internal/runs/x/events?after=0",
                        None)
                    assert response.status_code == 200
            assert len(captured) == 2
            for headers in captured:
                lowered = {key.lower(): value for key, value in
                           headers.items()}
                for name in ("authorization", "cookie", "forwarded",
                             "x-easel-role", "x-forwarded-for",
                             "x-forwarded-proto"):
                    assert name not in lowered, (name, dict(headers))
                assert lowered["x-easel-workspace-id"] == user.workspace_id
                assert lowered["x-easel-service-token"] == "service-token"
            # client 默认状态不被临时清空或改写
            assert client.headers["authorization"] == "Bearer default-secret"
            assert client.cookies.get("jar") == "default-secret"
        finally:
            await client.aclose()

    asyncio.run(scenario())
