"""Task 10 Step 2：常驻 worker 的逐 Run 上下文与持久执行。

边界：
- 两个不同会话的 Run 同时停在适配器入口，各自的签名快照、broker token、
  模型、原生绑定、事件与消息互不串用。
- 篡改后的请求内容、快照或 Run 归属在接纳前被拒绝，不产生执行。
- worker 重载后同一 Run/nonce 只返回持久状态，不再触发 prompt。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.control.types import DomainError, WorkspacePaths
from easel.worker.config import run_input_digest, sign_run_snapshot
from easel.worker.runs import RunManager
from easel.worker.sessions import WorkspaceSessionStore

WORKSPACE = "workspace-a"
REVISION = 7
CLOCK = 1000


def make_paths(tmp_path: Path) -> WorkspacePaths:
    paths = WorkspacePaths(
        code_root=tmp_path / "code",
        workspace_root=tmp_path / "workspace",
        state_root=tmp_path / "state",
        home=tmp_path / "home",
    )
    for root in (paths.code_root, paths.workspace_root, paths.state_root,
                 paths.home):
        root.mkdir(parents=True, exist_ok=True)
    return paths


def sign(private_key: Ed25519PrivateKey, *, run_id: str, session_id: str,
         message: str, routes: dict, agent: dict,
         attachments=(), persona=None,
         workspace_id: str = WORKSPACE, revision: int = REVISION,
         nonce: str | None = None) -> dict:
    digest = run_input_digest(
        conversation_id=session_id, task_type="chat", message=message,
        attachments=attachments, persona=persona)
    return sign_run_snapshot(
        private_key,
        workspace_id=workspace_id,
        run_id=run_id,
        conversation_id=session_id,
        input_digest=digest,
        task_type="chat",
        revision=revision,
        routes=routes,
        agent=agent,
        expires_at=CLOCK + 60,
        nonce=nonce or ("nonce-" + run_id),
    )


def request_for(session_id: str, message: str, envelope: dict) -> dict:
    return {
        "sessionId": session_id,
        "turnId": "turn-" + session_id,
        "message": message,
        "attachments": [],
        "persona": None,
        "taskType": "chat",
        "inputDigest": envelope["input_digest"],
    }


class PausingExecutor:
    """真实适配器替身：在入口暂停，记录 RunRequest，按 Run 绑定原生 ID。"""

    def __init__(self, natives: dict[str, str], answers: dict[str, str],
                 chunks: dict[str, list[str]]) -> None:
        self.natives = natives
        self.answers = answers
        self.chunks = chunks
        self.calls: list[tuple] = []
        self.entered: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.finished: dict[str, asyncio.Event] = {}

    def event(self, run_id: str) -> asyncio.Event:
        return self.entered.setdefault(run_id, asyncio.Event())

    def gate(self, run_id: str) -> asyncio.Event:
        return self.release.setdefault(run_id, asyncio.Event())

    def done(self, run_id: str) -> asyncio.Event:
        return self.finished.setdefault(run_id, asyncio.Event())

    async def __call__(self, request, context, bind, emit, ask):
        self.calls.append((request, context))
        if not request.native_id:
            bind(self.natives[request.session_key])
        self.event(context.run_id).set()
        await self.gate(context.run_id).wait()
        for chunk in self.chunks.get(context.run_id, []):
            emit("token", chunk)
        self.done(context.run_id).set()
        return self.answers.get(context.run_id)


class NeverExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, request, context, bind, emit, ask):
        self.calls += 1
        return "不应执行"


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.entered = asyncio.Event()

    async def __call__(self, request, context, bind, emit, ask):
        self.calls.append((request, context))
        self.entered.set()
        return "ok"


async def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0.01)


def make_manager(paths: WorkspacePaths, key: Ed25519PrivateKey,
                 executor, *, public_key=None) -> RunManager:
    return RunManager(
        workspace_id=WORKSPACE,
        paths=paths,
        public_key=key.public_key() if public_key is None else public_key,
        sessions=WorkspaceSessionStore(paths.state_root, WORKSPACE),
        executor=executor,
        base_env={"PATH": "C:\\bin", "EASEL_ADMIN_KEY": "admin-secret"},
        clock=lambda: CLOCK,
    )


def token_events(manager: RunManager, run_id: str) -> list[str]:
    payload = manager.events(run_id, after=0)
    return [event["data"] for event in payload["events"]
            if event["event"] == "token"]


def test_two_runs_keep_independent_signed_contexts(tmp_path):
    paths = make_paths(tmp_path)
    key = Ed25519PrivateKey.generate()
    executor = PausingExecutor(
        natives={"conversation-a": "native-a",
                 "conversation-b": "native-b"},
        answers={"run-a": None, "run-b": None},
        chunks={"run-a": ["A1", "A2"], "run-b": ["B1", "B2"]},
    )
    manager = make_manager(paths, key, executor)
    session_store = WorkspaceSessionStore(paths.state_root, WORKSPACE)
    routes_a = {"broker_token": "broker-token-a",
                "broker_url": "http://broker-a.test"}
    routes_b = {"broker_token": "broker-token-b",
                "broker_url": "http://broker-b.test"}
    agent_a = {"runtime": "acp", "command": ["fixture-acp", "--a"],
               "sandbox": "read-only", "model": "model-a", "timeout": 60}
    agent_b = {"runtime": "acp", "command": ["fixture-acp", "--b"],
               "sandbox": "workspace-write", "model": "model-b", "timeout": 60}
    envelope_a = sign(key, run_id="run-a", session_id="conversation-a",
                      message="甲的消息", routes=routes_a, agent=agent_a)
    envelope_b = sign(key, run_id="run-b", session_id="conversation-b",
                      message="乙的消息", routes=routes_b, agent=agent_b)

    async def scenario() -> None:
        first = await manager.submit(
            "run-a", envelope_a,
            request_for("conversation-a", "甲的消息", envelope_a))
        second = await manager.submit(
            "run-b", envelope_b,
            request_for("conversation-b", "乙的消息", envelope_b))
        assert first == {"runId": "run-a", "state": "running",
                         "duplicate": False}
        assert second == {"runId": "run-b", "state": "running",
                          "duplicate": False}
        # submit 已返回，但两个适配器都还停在入口：HTTP 断开不取消 supervisor
        await asyncio.wait_for(
            asyncio.gather(executor.event("run-a").wait(),
                           executor.event("run-b").wait()), timeout=5)

        requests = {context.run_id: request
                    for request, context in executor.calls}
        assert set(requests) == {"run-a", "run-b"}
        request_a, request_b = requests["run-a"], requests["run-b"]
        assert request_a.env["EASEL_BROKER_TOKEN"] == "broker-token-a"
        assert request_b.env["EASEL_BROKER_TOKEN"] == "broker-token-b"
        assert request_a.env["EASEL_RUN_ID"] == "run-a"
        assert request_b.env["EASEL_RUN_ID"] == "run-b"
        assert request_a.env["EASEL_WORKSPACE_ID"] == WORKSPACE
        assert "EASEL_ADMIN_KEY" not in request_a.env
        assert request_a.model == "model-a"
        assert request_b.model == "model-b"
        assert request_a.command == ("fixture-acp", "--a")
        assert request_b.command == ("fixture-acp", "--b")
        assert request_a.sandbox == "read-only"
        assert request_b.sandbox == "workspace-write"
        assert request_a.session_key == "conversation-a"
        assert request_b.session_key == "conversation-b"

        # 交错释放：乙先输出，甲后输出
        executor.gate("run-b").set()
        await wait_until(lambda: manager.state("run-b") == "done")
        executor.gate("run-a").set()
        await wait_until(lambda: manager.state("run-a") == "done")

        assert token_events(manager, "run-a") == ["A1", "A2"]
        assert token_events(manager, "run-b") == ["B1", "B2"]
        encoded_a = json.dumps(manager.events("run-a", after=0),
                               ensure_ascii=False)
        assert "broker-token-b" not in encoded_a
        assert "native-b" not in encoded_a

        assert session_store.binding("conversation-a")["native_id"] == "native-a"
        assert session_store.binding("conversation-b")["native_id"] == "native-b"
        messages_a = session_store.messages("conversation-a")
        messages_b = session_store.messages("conversation-b")
        assert [item["role"] for item in messages_a] == ["user", "assistant"]
        assert messages_a[0]["content"] == "甲的消息"
        assert messages_a[1]["content"] == "A1A2"
        assert [item["role"] for item in messages_b] == ["user", "assistant"]
        assert messages_b[0]["content"] == "乙的消息"
        assert messages_b[1]["content"] == "B1B2"

        admission_a = manager.admission("run-a")
        admission_b = manager.admission("run-b")
        assert admission_a["snapshot"]["routes"]["broker_token"] == \
            "broker-token-a"
        assert admission_b["snapshot"]["routes"]["broker_token"] == \
            "broker-token-b"
        assert admission_a["snapshot"]["input_digest"] != \
            admission_b["snapshot"]["input_digest"]

        # 重开：状态、事件、原生绑定与消息保留；重复提交不再 prompt
        reopened_executor = NeverExecutor()
        reopened = make_manager(paths, key, reopened_executor)
        assert reopened.state("run-a") == "done"
        assert token_events(reopened, "run-a") == ["A1", "A2"]
        assert reopened.admission("run-b")["snapshot"]["agent"]["model"] == \
            "model-b"
        assert WorkspaceSessionStore(
            paths.state_root, WORKSPACE).binding(
                "conversation-a")["native_id"] == "native-a"
        duplicate = await reopened.submit(
            "run-a", envelope_a,
            request_for("conversation-a", "甲的消息", envelope_a))
        assert duplicate == {"runId": "run-a", "state": "done",
                             "duplicate": True}
        assert reopened_executor.calls == 0
        assert token_events(reopened, "run-a") == ["A1", "A2"]
        await reopened.aclose()

    asyncio.run(scenario())


def test_tampered_run_payload_is_rejected(tmp_path):
    paths = make_paths(tmp_path)
    key = Ed25519PrivateKey.generate()
    executor = NeverExecutor()
    manager = make_manager(paths, key, executor)
    routes = {"broker_token": "broker-token-a"}
    agent = {"runtime": "acp", "command": ["fixture-acp"],
             "sandbox": "read-only", "model": "model-a", "timeout": 60}

    async def scenario() -> None:
        envelope = sign(key, run_id="run-a", session_id="conversation-a",
                        message="原始输入", routes=routes, agent=agent)

        # 1) 替换输入内容并同步替换未签名的 inputDigest：签名绑定不匹配
        changed = request_for("conversation-a", "被替换的输入", envelope)
        changed["inputDigest"] = run_input_digest(
            conversation_id="conversation-a", task_type="chat",
            message="被替换的输入")
        with pytest.raises(DomainError) as mismatch:
            await manager.submit("run-a", envelope, changed)
        assert mismatch.value.code == "snapshot_binding_mismatch"
        assert mismatch.value.status == 403

        # 2) 替换会话：签名绑定的是 conversation-a
        other_session = request_for("conversation-b", "原始输入", envelope)
        other_session["inputDigest"] = run_input_digest(
            conversation_id="conversation-b", task_type="chat",
            message="原始输入")
        with pytest.raises(DomainError) as session_mismatch:
            await manager.submit("run-a", envelope, other_session)
        assert session_mismatch.value.status == 403

        # 3) 替换任务类型
        changed_type = request_for("conversation-a", "原始输入", envelope)
        changed_type["taskType"] = "publish"
        changed_type["inputDigest"] = run_input_digest(
            conversation_id="conversation-a", task_type="publish",
            message="原始输入")
        with pytest.raises(DomainError) as type_mismatch:
            await manager.submit("run-a", envelope, changed_type)
        assert type_mismatch.value.status == 403

        # 4) 签名后修改路由（例如替换 broker token）
        tampered_envelope = dict(envelope)
        tampered_envelope["routes"] = {"broker_token": "attacker-token"}
        with pytest.raises(DomainError) as bad_signature:
            await manager.submit(
                "run-a", tampered_envelope,
                request_for("conversation-a", "原始输入", envelope))
        assert bad_signature.value.code == "snapshot_invalid"
        assert bad_signature.value.status == 403

        # 5) 路径 Run 与签名 Run 不一致
        with pytest.raises(DomainError) as wrong_run:
            await manager.submit(
                "run-b", envelope,
                request_for("conversation-a", "原始输入", envelope))
        assert wrong_run.value.status == 403

        # 6) 其它 workspace 的签名
        foreign = sign(key, run_id="run-a", session_id="conversation-a",
                       message="原始输入", routes=routes, agent=agent,
                       workspace_id="workspace-b")
        with pytest.raises(DomainError) as foreign_workspace:
            await manager.submit(
                "run-a", foreign,
                request_for("conversation-a", "原始输入", envelope))
        assert foreign_workspace.value.status == 403

        # 全部拒绝发生在接纳与执行之前
        assert executor.calls == 0
        assert manager.state("run-a") is None
        assert WorkspaceSessionStore(
            paths.state_root, WORKSPACE).messages("conversation-a") == []

        # 合法请求仍可进入一次执行
        accepted = await manager.submit(
            "run-a", envelope,
            request_for("conversation-a", "原始输入", envelope))
        assert accepted["duplicate"] is False
        await wait_until(lambda: executor.calls == 1)
        await manager.aclose()

    asyncio.run(scenario())


def test_duplicate_submit_after_worker_reload_does_not_prompt(tmp_path):
    paths = make_paths(tmp_path)
    key = Ed25519PrivateKey.generate()
    executor = PausingExecutor(
        natives={"conversation-a": "native-a"}, answers={"run-a": None},
        chunks={"run-a": ["A1"]})
    manager = make_manager(paths, key, executor)
    routes = {"broker_token": "broker-token-a"}
    agent = {"runtime": "acp", "command": ["fixture-acp"],
             "sandbox": "read-only", "model": "model-a", "timeout": 60}
    envelope = sign(key, run_id="run-a", session_id="conversation-a",
                    message="原始输入", routes=routes, agent=agent)
    request = request_for("conversation-a", "原始输入", envelope)

    async def scenario() -> None:
        await manager.submit("run-a", envelope, request)
        await asyncio.wait_for(executor.event("run-a").wait(), timeout=5)
        assert len(executor.calls) == 1

        # 模拟进程重载：新管理器读同一持久接纳库，在途 Run 明确转 interrupted
        reloaded_executor = NeverExecutor()
        reloaded = make_manager(paths, key, reloaded_executor)
        assert reloaded.state("run-a") == "interrupted"
        interrupted = reloaded.events("run-a", after=0)
        assert interrupted["events"][-1]["event"] == "state"
        assert interrupted["events"][-1]["data"]["state"] == "interrupted"

        duplicate = await reloaded.submit("run-a", envelope, request)
        assert duplicate == {"runId": "run-a", "state": "interrupted",
                             "duplicate": True}
        assert reloaded_executor.calls == 0
        # 已接受并写入的用户消息保留；没有助手消息，也不重新 prompt
        assert [item["role"] for item in
                reloaded.messages("conversation-a")["messages"]] == ["user"]

        # 再次重载仍不重新 prompt
        again = make_manager(paths, key, NeverExecutor())
        assert (await again.submit("run-a", envelope, request))["duplicate"]
        assert again.state("run-a") == "interrupted"
        await again.aclose()
        await reloaded.aclose()

        # 旧 supervisor 释放后也不能覆盖已中断终态、追加事件或补写助手消息
        executor.gate("run-a").set()
        await asyncio.wait_for(executor.done("run-a").wait(), timeout=5)
        await asyncio.sleep(0.05)
        assert manager.state("run-a") == "interrupted"
        assert token_events(manager, "run-a") == []
        assert [item["role"] for item in WorkspaceSessionStore(
            paths.state_root, WORKSPACE).messages("conversation-a")] == ["user"]
        await manager.aclose()

    asyncio.run(scenario())


def test_missing_public_key_returns_503(tmp_path):
    paths = make_paths(tmp_path)
    key = Ed25519PrivateKey.generate()
    manager = RunManager(
        workspace_id=WORKSPACE, paths=paths, public_key=None,
        sessions=WorkspaceSessionStore(paths.state_root, WORKSPACE),
        executor=NeverExecutor(), base_env={}, clock=lambda: CLOCK)
    envelope = sign(key, run_id="run-a", session_id="conversation-a",
                    message="原始输入", routes={"broker_token": "token"},
                    agent={"runtime": "acp", "command": ["fixture-acp"]})

    async def scenario() -> None:
        with pytest.raises(DomainError) as unavailable:
            await manager.submit(
                "run-a", envelope,
                request_for("conversation-a", "原始输入", envelope))
        assert unavailable.value.code == "capability_unavailable"
        assert unavailable.value.status == 503

    asyncio.run(scenario())


def test_attachment_manifest_is_session_scoped(tmp_path):
    paths = make_paths(tmp_path)
    key = Ed25519PrivateKey.generate()
    executor = RecordingExecutor()
    manager = make_manager(paths, key, executor)
    scope = hashlib.sha256(b"conversation-a").hexdigest()[:20]
    relative = f"_inbox/{scope}/batch-1/0001-report.txt"
    attachment_id = hashlib.sha256(
        f"{scope}\0{relative}".encode("utf-8")).hexdigest()[:24]
    target = paths.workspace_root / "outputs" / relative
    target.parent.mkdir(parents=True)
    target.write_text("内容", encoding="utf-8")
    agent = {"runtime": "acp", "command": ["fixture-acp"],
             "sandbox": "read-only", "timeout": 60}
    attachments = [{"id": attachment_id, "name": "report.txt",
                    "path": relative}]
    envelope = sign(key, run_id="run-a", session_id="conversation-a",
                    message="看附件", routes={"broker_token": "token"},
                    agent=agent, attachments=attachments)
    request = request_for("conversation-a", "看附件", envelope)
    request["attachments"] = attachments

    async def scenario() -> None:
        await manager.submit("run-a", envelope, request)
        await asyncio.wait_for(executor.entered.wait(), timeout=5)
        prompt = executor.calls[0][0].prompt
        assert f"outputs/{relative}" in prompt
        await manager.aclose()

    asyncio.run(scenario())

    # 即使签名有效，指向其它会话 inbox 的附件仍被执行前拒绝
    bad_attachments = [{
        "id": attachment_id, "name": "report.txt",
        "path": f"_inbox/{'0' * 20}/batch-1/0001-report.txt"}]
    bad_envelope = sign(
        key, run_id="run-b", session_id="conversation-b",
        message="看附件", routes={"broker_token": "token"},
        agent=agent, attachments=bad_attachments)
    bad_request = request_for("conversation-b", "看附件", bad_envelope)
    bad_request["attachments"] = bad_attachments
    manager2 = make_manager(paths, key, NeverExecutor())

    async def rejected() -> None:
        with pytest.raises(DomainError) as error:
            await manager2.submit("run-b", bad_envelope, bad_request)
        assert error.value.status == 403
        assert error.value.code == "attachment_forbidden"
        await manager2.aclose()

    asyncio.run(rejected())
