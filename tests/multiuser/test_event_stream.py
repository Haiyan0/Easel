"""Task 10 Step 4：持久事件、SSE、停止与权限确认的红灯测试。

边界：

* 重连只按游标回放持久事件，不重新 prompt；无效游标在开始流式响应前拒绝。
* 上游静默时，登录撤销仍必须在 5 秒内关闭事件流；后台任务继续到终态。
* 停止、回答、事件都先核对 actor/run/question 归属，外空间 Run 不触发
  任何 worker 调用；一次回答的重试不能再次授权。
* 停止与自然完成竞争只推进一次终态、只释放一次容量，broker token 随真正
  终态撤销。
* worker 实例失效把控制库残留任务标 interrupted，且不自动重发任务 POST。
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
COOKIE = "__Host-easel_session"


class Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


# ---------------------------------------------------------------------------
# worker 侧真实 RunManager + 内部 HTTP 传输
# ---------------------------------------------------------------------------


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
        workspace_id=workspace_id,
        paths=paths,
        public_key=key.public_key(),
        sessions=WorkspaceSessionStore(paths.state_root, workspace_id),
        executor=executor,
        base_env={"PATH": "C:\\bin", "EASEL_ADMIN_KEY": "admin-secret"},
        clock=lambda: CLOCK,
    )


class WorkerTransport(httpx.AsyncBaseTransport):
    """把 relay 的内部 HTTP 调用接到真实 RunManager，并按 workspace 路由。"""

    def __init__(self, managers: dict[str, RunManager]) -> None:
        self.managers = managers
        self.calls: list[tuple[str, str]] = []

    async def handle_async_request(
            self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
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
            run_id = match.group(1)
            admission = manager.admission(run_id)
            if admission is None:
                raise DomainError("run_not_found", 404)
            session_id = payload.get("sessionId")
            if (session_id is not None
                    and session_id != admission["conversationId"]):
                raise DomainError("run_binding_mismatch", 403)
            return 200, await manager.stop(run_id)
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
        return [
            call for call in self.calls if call[0] == "POST"
            and call[1].endswith("/stop")]


class WorkspaceRuntime:
    def __init__(self, revision: int = REVISION) -> None:
        self.revision = revision

    async def ensure_ready(self, workspace_id: str,
                           config_version: int) -> WorkerHandle:
        return WorkerHandle(
            workspace_id, "container-" + workspace_id[:8],
            "http://worker.test:7860", "service-token-" + workspace_id[:8],
            config_version)

    async def bound_handle(self, workspace_id: str, container_id: str,
                           config_version: int) -> WorkerHandle | None:
        if container_id != "container-" + workspace_id[:8]:
            return None
        return WorkerHandle(
            workspace_id, container_id, "http://worker.test:7860",
            "service-token-" + workspace_id[:8], config_version)


class FakeProvisioner:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.handles: dict[str, WorkerHandle] = {}
        self.fail_inspect = False

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
        self.stops += 1

    async def inspect(self, workspace_id: str) -> dict:
        if self.fail_inspect:
            raise OSError("worker inspect failed")
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


class FakeBroker:
    """记录真正终态时的撤销；不签发任何令牌。"""

    def __init__(self) -> None:
        self.revoked: list[tuple[str, str]] = []

    def revoke_run(self, run_id: str, workspace_id: str) -> None:
        self.revoked.append((run_id, workspace_id))


def provider(_actor: Identity) -> PinnedRunConfig:
    return PinnedRunConfig(
        revision=REVISION,
        routes={"broker_token": "run-scoped-token"},
        agent={"runtime": "acp", "command": ["fixture", "acp"],
               "sandbox": "read-only"})


def make_relay(store: Store, client: httpx.AsyncClient, *, runtime=None,
               broker=None, clock: Clock | None = None,
               signing_key=None) -> WorkerRelay:
    return WorkerRelay(
        runtime or WorkspaceRuntime(), SessionService(store), AuthService(store),
        store=store,
        signing_key=signing_key or Ed25519PrivateKey.generate(),
        http_client=client, config_provider=provider, broker=broker,
        clock=clock or Clock())


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


def open_question_id(manager: RunManager, run_id: str) -> str | None:
    for event in manager.events(run_id, after=0)["events"]:
        if event["event"] == "question":
            return json.loads(event["data"])["id"]
    return None


# ---------------------------------------------------------------------------
# 执行器替身
# ---------------------------------------------------------------------------


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
        self.answers: dict | None = None

    async def __call__(self, request, context, bind, emit, ask):
        self.prompts += 1
        self.answers = await ask(
            [{"questionId": "allow", "question": "允许？",
              "options": ["yes", "no"]}])
        return "已回答"


# ---------------------------------------------------------------------------
# 重连回放
# ---------------------------------------------------------------------------


def test_reconnect_replays_without_prompt(tmp_path):
    async def scenario() -> None:
        with Store.open(tmp_path / "control.sqlite3") as store:
            user = store.create_user("Owner")
            conversation = store.create_conversation(user, "回放")
            key = Ed25519PrivateKey.generate()
            manager = make_manager(
                make_paths(tmp_path), user.workspace_id, key,
                ImmediateExecutor())
            transport = WorkerTransport({user.workspace_id: manager})
            broker = FakeBroker()
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(
                    store, client, broker=broker, signing_key=key)
                admitted = await relay.submit(user, payload(conversation.id))
                run_id = admitted["runId"]
                await wait_until(lambda: manager.state(run_id) == "done")

                first = await relay.events(user, run_id, after=0)
                event_ids = [event["eventId"] for event in first["events"]]
                assert event_ids == sorted(event_ids)
                assert event_ids == list(range(1, len(event_ids) + 1))
                assert first["state"] == "done"
                assert store.get_run(user, run_id).status == "done"
                assert broker.revoked == [(run_id, user.workspace_id)]

                replay = await relay.events(
                    user, run_id, after=first["lastEventId"])
                assert replay["events"] == []
                assert replay["lastEventId"] == first["lastEventId"]
                # 重连只读持久事件：prompt 与任务 POST 都只有一次
                assert manager.admission(run_id)["state"] == "done"
                assert transport.task_posts(run_id) == [
                    ("POST", f"/internal/runs/{run_id}")]

                third = await relay.events(
                    user, run_id, after=first["lastEventId"])
                assert third["events"] == [], "重复回放不得产生新事件"
                with pytest.raises(DomainError) as invalid:
                    await relay.events(
                        user, run_id, after=first["lastEventId"] + 10)
                assert invalid.value.status == 400
        await manager.aclose()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# SSE 撤销
# ---------------------------------------------------------------------------


def test_silent_sse_revocation_within_five_seconds(tmp_path):
    async def scenario() -> None:
        with Store.open(tmp_path / "control.sqlite3") as store:
            user = store.create_user("Owner")
            conversation = store.create_conversation(user, "静默撤销")
            key = Ed25519PrivateKey.generate()
            blocking = BlockingExecutor()
            manager = make_manager(
                make_paths(tmp_path), user.workspace_id, key, blocking)
            transport = WorkerTransport({user.workspace_id: manager})
            clock = Clock(0.0)
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(
                    store, client, clock=clock, signing_key=key)
                admitted = await relay.submit(user, payload(conversation.id))
                run_id = admitted["runId"]
                await wait_until(lambda: manager.state(run_id) == "running")

                first = await relay.events(user, run_id, after=0)
                state = {"revoked": False, "checks": 0}

                async def recheck() -> None:
                    state["checks"] += 1
                    if state["revoked"]:
                        raise DomainError("unauthorized", 401)

                frames: list[bytes] = []

                async def consume() -> None:
                    async for chunk in run_event_stream(
                            relay=relay, actor=user, run_id=run_id,
                            page=first, recheck=recheck, clock=clock,
                            poll_interval=0.01):
                        frames.append(chunk)

                task = asyncio.create_task(consume())
                await wait_until(lambda: any(b"event: state" in f
                                             for f in frames))
                # 上游静默且时钟未满 5 秒时不复查、不结束
                await asyncio.sleep(0.05)
                assert state["checks"] == 0
                assert not task.done()

                clock.value = 5.0
                state["revoked"] = True
                await asyncio.wait_for(task, timeout=5)
                assert state["checks"] >= 1
                # 关闭的是连接，不是后台任务：worker 仍在执行
                assert manager.state(run_id) == "running"

                # 后台执行仍能到达终态并写入消息
                blocking.gate.set()
                await wait_until(lambda: manager.state(run_id) == "done")
                messages = manager.messages(conversation.id)["messages"]
                assert messages[-1]["role"] == "assistant"

                replay = await relay.events(user, run_id, after=0)
                assert replay["state"] == "done"
                assert [event["event"] for event in replay["events"]][-1] \
                    == "state"
        await manager.aclose()

    asyncio.run(scenario())


def test_slow_consumer_send_is_closed_by_auth_watchdog():
    """ASGI send 被背压阻塞时，认证监视仍须在窗口内结束响应。"""
    from easel.control.stream import AuthGuardedStreamingResponse

    class BlockingBody:
        def __init__(self) -> None:
            self.closed = False
            self.produced = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.produced += 1
            if self.produced == 1:
                return b'event: token\ndata: "private"\n\n'
            await asyncio.sleep(60)
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed = True

    async def scenario() -> None:
        body = BlockingBody()
        block = asyncio.Event()
        sent: list[bytes] = []
        state = {"checks": 0}

        async def send(message) -> None:
            if message["type"] == "http.response.start":
                return
            if message.get("body"):
                sent.append(message["body"])
                await block.wait()

        async def recheck() -> None:
            state["checks"] += 1
            raise DomainError("unauthorized", 401)

        response = AuthGuardedStreamingResponse(
            body, recheck=recheck, auth_check_seconds=0.2,
            media_type="text/event-stream")
        task = asyncio.create_task(response.stream_response(send))
        await wait_until(lambda: len(sent) == 1)
        await asyncio.wait_for(task, timeout=2)
        assert task.exception() is None
        assert state["checks"] >= 1
        assert sent == [b'event: token\ndata: "private"\n\n']
        assert body.closed is True, "撤销后必须关闭响应体迭代器"

    asyncio.run(scenario())


def test_auth_watchdog_uses_fixed_cadence_from_check_start():
    """默认参数下检查耗时计入窗口：撤销到关闭仍 ≤5 秒。"""
    from easel.control.stream import AuthGuardedStreamingResponse

    class BlockingBody:
        def __init__(self) -> None:
            self.closed = False
            self.produced = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.produced += 1
            if self.produced == 1:
                return b"event: token\ndata: \"private\"\n\n"
            await asyncio.sleep(60)
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed = True

    async def scenario() -> None:
        body = BlockingBody()
        block = asyncio.Event()
        sent: list[bytes] = []
        state = {"calls": 0}
        revoked_at: dict[str, float] = {}

        async def send(message) -> None:
            if message["type"] == "http.response.start":
                return
            if message.get("body"):
                sent.append(message["body"])
                await block.wait()

        async def recheck() -> None:
            call = state["calls"]
            state["calls"] += 1
            if call == 0:
                # 检查开始即读到登录有效，随后（本次检查期间）撤销
                revoked_at["t"] = time.monotonic()
                await asyncio.sleep(0.7)
                return
            await asyncio.sleep(0.7)
            raise DomainError("unauthorized", 401)

        response = AuthGuardedStreamingResponse(
            body, recheck=recheck, auth_check_seconds=4.0,
            media_type="text/event-stream")
        task = asyncio.create_task(response.stream_response(send))
        await wait_until(lambda: len(sent) == 1)
        await asyncio.wait_for(task, timeout=12)
        closed_at = time.monotonic()
        assert task.exception() is None
        assert state["calls"] >= 2
        elapsed = closed_at - revoked_at["t"]
        assert elapsed <= 5.0, f"撤销到关闭 {elapsed:.3f}s"
        assert sent == [b'event: token\ndata: "private"\n\n']
        assert body.closed is True

    asyncio.run(scenario())


def test_auth_watchdog_check_timeout_fails_closed():
    from easel.control.stream import AuthGuardedStreamingResponse

    async def scenario() -> None:
        closed = {"value": False}

        class Body:
            def __aiter__(self):
                return self

            async def __anext__(self):
                return b"chunk"

            async def aclose(self):
                closed["value"] = True

        block = asyncio.Event()
        sent: list[bytes] = []

        async def send(message) -> None:
            if message["type"] != "http.response.start" and message.get("body"):
                sent.append(message["body"])
                await block.wait()

        async def recheck() -> None:
            await asyncio.sleep(30)

        response = AuthGuardedStreamingResponse(
            Body(), recheck=recheck, auth_check_seconds=0.2,
            media_type="text/event-stream")
        task = asyncio.create_task(response.stream_response(send))
        await wait_until(lambda: len(sent) == 1)
        await asyncio.wait_for(task, timeout=2)
        assert task.exception() is None
        assert closed["value"] is True
        # 监视/发送任务均已结束，没有残留后台任务
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(scenario())


def test_watchdog_cleans_up_on_downstream_disconnect():
    from easel.control.stream import AuthGuardedStreamingResponse

    async def scenario() -> None:
        body = {"closed": False}

        class Body:
            def __aiter__(self):
                return self

            async def __anext__(self):
                return b"chunk"

            async def aclose(self):
                body["closed"] = True

        async def send(message) -> None:
            if message["type"] != "http.response.start" and message.get("body"):
                raise OSError("client disconnected")

        async def recheck() -> None:
            return None

        response = AuthGuardedStreamingResponse(
            Body(), recheck=recheck, auth_check_seconds=10.0,
            media_type="text/event-stream")
        with pytest.raises(OSError):
            await response.stream_response(send)
        assert body["closed"] is True
        current = asyncio.current_task()
        leftovers = [task for task in asyncio.all_tasks()
                     if task is not current and not task.done()]
        assert leftovers == []

    asyncio.run(scenario())


def test_watchdog_does_not_close_normal_stream():
    from easel.control.stream import AuthGuardedStreamingResponse

    async def scenario() -> None:
        sent: list[bytes] = []
        checks = {"n": 0}

        async def body():
            yield b"one"
            yield b"two"

        async def send(message) -> None:
            if message["type"] != "http.response.start" and message.get("body"):
                sent.append(message["body"])

        async def recheck() -> None:
            checks["n"] += 1

        response = AuthGuardedStreamingResponse(
            body(), recheck=recheck, auth_check_seconds=10.0,
            media_type="text/event-stream")
        await asyncio.wait_for(response.stream_response(send), timeout=2)
        assert sent == [b"one", b"two"]
        assert checks["n"] == 0

    asyncio.run(scenario())


def test_default_auth_check_window_bounds_revocation_within_five_seconds():
    """默认轮询/复查间隔之和必须落在 5 秒撤销窗口内。

    真实静默撤销测试使用可控时钟，只能证明“到点会复查”，不能证明默认
    参数下最坏情况仍在 5 秒内关闭；这里直接约束默认常量关系。
    """
    from easel.control.stream import DEFAULT_AUTH_CHECK_SECONDS, DEFAULT_POLL_SECONDS

    assert DEFAULT_AUTH_CHECK_SECONDS + DEFAULT_POLL_SECONDS <= 5.0


# ---------------------------------------------------------------------------
# 问题归属
# ---------------------------------------------------------------------------


def test_question_answer_is_run_scoped(tmp_path):
    async def scenario() -> None:
        with Store.open(tmp_path / "control.sqlite3") as store:
            owner = store.create_user("Owner")
            other = store.create_user("Other")
            owner_conversation = store.create_conversation(owner, "本人")
            other_conversation = store.create_conversation(other, "外空间")
            key = Ed25519PrivateKey.generate()
            owner_executor = AskingExecutor()
            other_executor = AskingExecutor()
            owner_manager = make_manager(
                make_paths(tmp_path, "owner"), owner.workspace_id, key,
                owner_executor)
            other_manager = make_manager(
                make_paths(tmp_path, "other"), other.workspace_id, key,
                other_executor)
            transport = WorkerTransport({
                owner.workspace_id: owner_manager,
                other.workspace_id: other_manager,
            })
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(store, client, signing_key=key)
                owner_run = (await relay.submit(
                    owner, payload(owner_conversation.id)))["runId"]
                other_run = (await relay.submit(
                    other, payload(other_conversation.id)))["runId"]
                await wait_until(
                    lambda: owner_manager.state(owner_run)
                    == "awaiting_permission"
                    and other_manager.state(other_run) == "awaiting_permission")
                owner_question = open_question_id(owner_manager, owner_run)
                other_question = open_question_id(other_manager, other_run)
                assert owner_question and other_question
                calls_before = len(transport.calls)

                # 外空间 Run 在转发前 404，不探测任何 worker
                with pytest.raises(DomainError) as foreign:
                    await relay.answer(
                        owner, {"runId": other_run,
                                "questionId": other_question,
                                "answers": {"allow": ["yes"]}})
                assert foreign.value.status == 404
                assert foreign.value.code == "not_found"
                assert len(transport.calls) == calls_before

                # 本人的 Run 混用外空间问题 ID：只到本人 worker，且明确拒绝
                with pytest.raises(DomainError) as mismatch:
                    await relay.answer(
                        owner, {"runId": owner_run,
                                "questionId": other_question,
                                "answers": {"allow": ["yes"]}})
                assert mismatch.value.status == 409
                assert transport.calls[-1] == (
                    "POST",
                    f"/internal/runs/{owner_run}/questions/"
                    f"{other_question}/answer")
                assert other_manager.question_status(
                    other_run, [other_question])["questions"][
                        other_question]["status"] == "pending"

                # 正确回答只授权一次；重试不能再次授权
                answered = await relay.answer(
                    owner, {"runId": owner_run,
                            "questionId": owner_question,
                            "answers": {"allow": ["yes"]}})
                assert answered["ok"] is True
                with pytest.raises(DomainError) as retried:
                    await relay.answer(
                        owner, {"runId": owner_run,
                                "questionId": owner_question,
                                "answers": {"allow": ["yes"]}})
                assert retried.value.status in (404, 409)
                await wait_until(
                    lambda: owner_manager.state(owner_run) == "done")
                assert owner_manager.question_status(
                    owner_run, [owner_question])["questions"][
                        owner_question]["status"] == "resolved"
                # 外空间问题从未被回答
                assert other_manager.question_status(
                    other_run, [other_question])["questions"][
                        other_question]["status"] == "pending"
        await owner_manager.aclose()
        await other_manager.aclose()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 停止与终态竞争
# ---------------------------------------------------------------------------


def test_stop_completion_releases_once(tmp_path):
    async def scenario() -> None:
        with Store.open(tmp_path / "control.sqlite3") as store:
            user = store.create_user("Owner")
            first = store.create_conversation(user, "停止")
            second = store.create_conversation(user, "自然完成")
            key = Ed25519PrivateKey.generate()
            blocking = BlockingExecutor()
            blocking_manager = make_manager(
                make_paths(tmp_path, "stop"), user.workspace_id, key,
                blocking)
            transport = WorkerTransport(
                {user.workspace_id: blocking_manager})
            broker = FakeBroker()
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(
                    store, client, broker=broker, signing_key=key)
                stopping = (await relay.submit(
                    user, payload(first.id)))["runId"]
                await wait_until(
                    lambda: blocking_manager.state(stopping) == "running")

                stopped = await relay.stop(user, {"runId": stopping})
                assert stopped["state"] == "cancelled"
                assert store.get_run(user, stopping).status == "cancelled"
                assert broker.revoked == [(stopping, user.workspace_id)]

                # 重复停止不再调用 worker、不重复释放
                again = await relay.stop(user, {"runId": stopping})
                assert again["state"] == "cancelled"
                assert len(transport.stop_posts()) == 1
                assert broker.revoked == [(stopping, user.workspace_id)]
                assert store.count_active_runs() == 0
            await blocking_manager.aclose()

        # 自然完成与停止竞争：events 同步终态后，停止直接返回持久状态
        with Store.open(tmp_path / "control2.sqlite3") as store:
            user = store.create_user("Owner")
            conversation = store.create_conversation(user, "自然完成")
            key = Ed25519PrivateKey.generate()
            manager = make_manager(
                make_paths(tmp_path, "done"), user.workspace_id, key,
                ImmediateExecutor())
            transport = WorkerTransport({user.workspace_id: manager})
            broker = FakeBroker()
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(
                    store, client, broker=broker, signing_key=key)
                run_id = (await relay.submit(
                    user, payload(conversation.id)))["runId"]
                await wait_until(
                    lambda: manager.state(run_id) == "done")
                await relay.events(user, run_id, after=0)
                assert store.get_run(user, run_id).status == "done"
                result = await relay.stop(user, {"runId": run_id})
                assert result["state"] == "done"
                assert transport.stop_posts() == []
                assert broker.revoked == [(run_id, user.workspace_id)]
                assert store.count_active_runs() == 0

                # 容量只释放一次：仍可接纳两个新任务，第三个 429
                third = store.create_conversation(user, "新一")
                fourth = store.create_conversation(user, "新二")
                fifth = store.create_conversation(user, "新三")
                await relay.submit(user, payload(third.id, "一"))
                await relay.submit(user, payload(fourth.id, "二"))
                with pytest.raises(DomainError) as full:
                    await relay.submit(user, payload(fifth.id, "三"))
                assert full.value.code == "workspace_capacity"
        await manager.aclose()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# worker 失效
# ---------------------------------------------------------------------------


def test_crash_is_interrupted_without_retry(tmp_path):
    async def scenario() -> None:
        with Store.open(tmp_path / "control.sqlite3") as store:
            user = store.create_user("Owner")
            conversation = store.create_conversation(user, "崩溃")
            key = Ed25519PrivateKey.generate()
            blocking = BlockingExecutor()
            manager = make_manager(
                make_paths(tmp_path), user.workspace_id, key, blocking)
            transport = WorkerTransport({user.workspace_id: manager})
            clock = Clock()
            provisioner = FakeProvisioner()
            broker = FakeBroker()
            runtime = RuntimeManager(
                store, provisioner, clock=clock, idle_seconds=900,
                lease_seconds=120, broker=broker)
            async with httpx.AsyncClient(transport=transport) as client:
                relay = make_relay(
                    store, client, runtime=runtime, broker=broker,
                    clock=clock, signing_key=key)
                run_id = (await relay.submit(
                    user, payload(conversation.id)))["runId"]
                await wait_until(lambda: manager.state(run_id) == "running")
                assert transport.task_posts(run_id) == [
                    ("POST", f"/internal/runs/{run_id}")]

                # 租约过期后对账发现实例不可用：残留 Run 必须明确 interrupted
                clock.value = 1121
                provisioner.fail_inspect = True
                with pytest.raises(RuntimeUnavailable) as unavailable:
                    await runtime.ensure_ready(user.workspace_id, REVISION)
                assert unavailable.value.code == "worker_inspect_failed"
                assert store.get_run(user, run_id).status == "interrupted"
                assert store.count_active_runs() == 0
                assert broker.revoked == [(run_id, user.workspace_id)]
                # 不自动重发任务 POST
                assert transport.task_posts(run_id) == [
                    ("POST", f"/internal/runs/{run_id}")]

                # 再次失效不重复释放；重连只回放事件
                await runtime.ensure_ready(user.workspace_id, REVISION)
                assert store.get_run(user, run_id).status == "interrupted"
                provisioner.fail_inspect = False
                replay = await relay.events(user, run_id, after=0)
                assert replay["runId"] == run_id
                assert store.get_run(user, run_id).status == "interrupted"
                assert transport.task_posts(run_id) == [
                    ("POST", f"/internal/runs/{run_id}")]
        blocking.gate.set()
        await manager.aclose()

    asyncio.run(scenario())


def test_sse_survives_transient_gateway_unavailable():
    """已建立的 SSE 遇 503 不结束，按 after 继续；撤销仍由 recheck 处理。"""
    import time as _time

    class Relay:
        def __init__(self) -> None:
            self.calls: list[int] = []
            self.failed = False

        async def events(self, actor, run_id, *, after=0):
            self.calls.append(after)
            if not self.failed:
                self.failed = True
                raise DomainError(
                    "worker_gateway_unavailable", 503, retry_after=1)
            return {
                "runId": run_id, "state": "done",
                "events": [
                    {"eventId": 1, "event": "token", "data": "ok"},
                    {"eventId": 2, "event": "state",
                     "data": {"state": "done"}},
                ],
                "lastEventId": 2,
            }

    async def scenario() -> None:
        relay = Relay()
        frames: list[bytes] = []
        start = _time.monotonic()

        async def recheck() -> None:
            return None

        async for chunk in run_event_stream(
                relay=relay, actor=None, run_id="f" * 32,
                page={"runId": "f" * 32, "state": "running",
                      "events": [], "lastEventId": 0},
                recheck=recheck, clock=_time.monotonic,
                poll_interval=0.01, heartbeat_seconds=60,
                auth_check_seconds=60):
            frames.append(chunk)
            assert _time.monotonic() - start < 5
        assert frames
        assert any(b"event: state" in frame for frame in frames)
        assert relay.calls == [0, 0]

    asyncio.run(scenario())
