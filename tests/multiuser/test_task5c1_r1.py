"""Task 5C1 R1：模型快照、权限问题、画像指令与准备期互斥的红灯测试。

只使用临时 Store/workspace、真实 control 适配和受控 relay/worker 替身；
不启动真实容器、不读取真实 Key、不调用真实供应商。
"""
from __future__ import annotations

import asyncio
import threading

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from easel import persona
from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.broker import BrokerService
from easel.control.cli import build_relay_factory
from easel.control.config import ConfigService
from easel.control.sessions import SessionService
from easel.control.store import Store
from easel.control.types import DomainError
from easel.control.workspace_tasks import WorkspaceTasks
from tests.multiuser.test_managed_chat_model import (
    CATALOG, MASTER_KEY, DispatchCapture, StaticRuntime, chat_route,
    make_agent, publish_chat,
)
from tests.multiuser.test_workspace_tasks import (
    FakeRelay, FakeWorkspace, ORIGIN, _app_with_tasks, _login,
)
from web import app as worker

REVISION = 7


class GatedWorkspace(FakeWorkspace):
    """基线请求在写完后暂停，允许测试观察准备期窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.ready = threading.Event()
        self.release = threading.Event()

    async def request(self, actor, method, path, *, query=None, payload=None,
                      body=None, content_type=None, headers=None):
        if path == "/api/profile/baseline":
            result = await worker.api_profile_baseline(
                worker.ProfileBaselineRequest(**payload))
            self.ready.set()
            if not await asyncio.to_thread(self.release.wait, 5):
                raise AssertionError("baseline gate timed out")
            return httpx.Response(200, json=result)
        if method == "DELETE" and path.startswith("/api/persona/"):
            return httpx.Response(200, json={"ok": True, "deleted": "profile"})
        if method == "PUT" and path.endswith("/file"):
            return httpx.Response(200, json={"ok": True})
        return await super().request(
            actor, method, path, query=query, payload=payload, body=body,
            content_type=content_type, headers=headers)


class QuestionRelay(FakeRelay):
    """把 Run 固定在 awaiting_permission，并返回一条持久 question 事件。"""

    async def events(self, actor, run_id, *, after=0):
        return {
            "runId": run_id,
            "state": "awaiting_permission",
            "events": [{
                "eventId": 1,
                "event": "question",
                "data": {
                    "id": "q-permission",
                    "questions": [{
                        "questionId": "permission",
                        "header": "授权",
                        "question": "允许执行？",
                        "options": [
                            {"label": "允许", "value": "allow"},
                            {"label": "拒绝", "value": "deny"},
                        ],
                    }],
                },
            }],
            "lastEventId": 1,
        }

    async def question_status(self, actor, payload):
        return {"ok": True, "questions": {
            payload["questionIds"][0]: {"status": "pending"}}}


class BrokenRelay(FakeRelay):
    async def submit_task(self, actor, task_type, payload):
        raise DomainError("capacity_exhausted", 429)


class UnconfirmedQuestionRelay(QuestionRelay):
    async def question_status(self, actor, payload):
        raise DomainError("gateway_unavailable", 503)


def _profile_prompt_from_relay(tmp_path, monkeypatch) -> str:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setattr(worker, "PROFILES_DIR", profiles)
    monkeypatch.setattr(persona, "PROFILES_DIR", profiles)

    class RealBaseline:
        async def request(self, actor, method, path, *, payload, **kwargs):
            result = await worker.api_profile_baseline(
                worker.ProfileBaselineRequest(**payload))
            return httpx.Response(200, json=result)

    class CaptureRelay:
        def __init__(self):
            self.payload = None

        async def submit_task(self, actor, task_type, payload):
            self.payload = payload
            return {"runId": "r1-run", "state": "accepted",
                    "duplicate": False}

    with Store.open(tmp_path / "prompt.sqlite3") as store:
        actor = store.create_user("PromptOwner")
        relay = CaptureRelay()
        tasks = WorkspaceTasks(
            store, SessionService(store), relay, RealBaseline())
        result = asyncio.run(tasks.build_profile(actor, {
            "name": "意图画像",
            "form": {"direction": "做数码内容"},
            "turnId": "prompt-turn",
        }))
        assert result["status"] == "running"
        return relay.payload["message"]


def test_skill_and_profile_signed_agent_use_issued_route_model(tmp_path):
    """新受管任务不能把静态 deepseek-chat 签进快照而 token 路由是 flash。"""
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        config = ConfigService(store, MASTER_KEY, model_catalog=CATALOG)
        publish_chat(config, admin, "deepseek-flash")
        broker = BrokerService(store, config, clock=lambda: 1000.0)
        capture = DispatchCapture()
        key = Ed25519PrivateKey.generate()

        async def scenario():
            async with httpx.AsyncClient(
                    transport=capture, trust_env=False) as client:
                factory = build_relay_factory(
                    signing_key=key, revision=REVISION,
                    agent={**make_agent(), "model": "deepseek-chat"},
                    broker_config={
                        "mode": "workspace-sidecar", "channel": "chat"},
                    gateway_required=False)
                relay = factory(
                    StaticRuntime(), SessionService(store), AuthService(store),
                    store, client, broker, None)
                results = []
                for task_type in ("skill", "profile"):
                    conversation = store.create_conversation(
                        member, f"{task_type} 模型")
                    admitted = await relay.submit_task(member, task_type, {
                        "sessionId": conversation.id,
                        "turnId": f"turn-{task_type}",
                        "message": f"fixture {task_type}",
                    })
                    results.append((task_type, admitted, conversation.id))
                # 配置发布后同一 turn 重试仍应命中原 Run/原快照，不重新签发。
                config.publish(
                    admin,
                    {"defaults": {"chat": chat_route(
                        "deepseek-v4-pro", ("deepseek-v4-pro",))}},
                    expected_version=1)
                duplicate = await relay.submit_task(member, "skill", {
                    "sessionId": results[0][2],
                    "turnId": "turn-skill",
                    "message": "fixture skill",
                })
                assert duplicate["duplicate"] is True
                assert duplicate["runId"] == results[0][1]["runId"]
                return results

        results = asyncio.run(scenario())
        assert len(capture.posts) == 2
        for task_type, admitted, _conversation_id in results:
            envelope = capture.posts[
                [item[0] for item in results].index(task_type)]["body"][
                    "envelope"]
            route = broker.authorize(
                envelope["routes"]["broker_token"], member.workspace_id,
                admitted["runId"], "chat")
            assert envelope["task_type"] == task_type
            assert envelope["agent"]["model"] == route.model == "deepseek-flash"
            assert envelope["agent"]["allowed_models"] == list(
                route.allowed_models)


def test_skill_and_profile_question_status_exposes_permission_card(tmp_path):
    """等待授权不能被折叠成 running；status 必须带可回答的题卡。"""
    workspace = FakeWorkspace()
    store = Store.connect(tmp_path / "control.sqlite3")
    admin = store.create_user("Owner", "admin")
    member = store.create_user("Member")
    auth = AuthService(store)
    relay = QuestionRelay(store, finish=False)
    app = create_app(
        store=store, auth=auth,
        config=ControlConfig(public_origin=ORIGIN),
        workspace_api=workspace,
        relay_factory=lambda *args: relay)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            _body, headers = _login(client, auth, admin, member)
            skill = client.post(
                "/api/skill",
                json={"skill": "demo", "input": "需要授权", "turnId": "q-skill"},
                headers=headers)
            assert skill.status_code == 202, skill.text
            profile = client.post(
                "/api/profile/build",
                json={"name": "问题画像", "form": {"direction": "方向"},
                      "turnId": "q-profile"},
                headers=headers)
            assert profile.status_code == 200, profile.text
            skill_status = client.get("/api/skill/status/demo")
            profile_status = client.get("/api/profile/build/status/问题画像")
            for status in (skill_status, profile_status):
                assert status.status_code == 200, status.text
                payload = status.json()
                assert payload["state"] == "awaiting_permission"
                assert payload["question"]["id"] == "q-permission"
                assert payload["question"]["questions"][0]["questionId"] == (
                    "permission")
                assert payload["questionPending"] is False
    finally:
        store.close()


def test_question_status_failure_keeps_card_but_disables_submit(tmp_path):
    """问答状态查询 503 时保留题卡，标记待确认，不能自动允许。"""
    workspace = FakeWorkspace()
    store = Store.connect(tmp_path / "control.sqlite3")
    admin = store.create_user("Owner", "admin")
    member = store.create_user("Member")
    auth = AuthService(store)
    relay = UnconfirmedQuestionRelay(store, finish=False)
    app = create_app(
        store=store, auth=auth,
        config=ControlConfig(public_origin=ORIGIN),
        workspace_api=workspace,
        relay_factory=lambda *args: relay)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            _body, headers = _login(client, auth, admin, member)
            submitted = client.post(
                "/api/skill",
                json={"skill": "demo", "input": "待确认", "turnId": "unknown-q"},
                headers=headers)
            assert submitted.status_code == 202, submitted.text
            status = client.get("/api/skill/status/demo")
            assert status.status_code == 200, status.text
            payload = status.json()
            assert payload["state"] == "awaiting_permission"
            assert payload["question"]["id"] == "q-permission"
            assert payload["questionPending"] is True
    finally:
        store.close()


def test_profile_prompt_contains_builder_directive_and_fixed_target(
        tmp_path, monkeypatch):
    prompt = _profile_prompt_from_relay(tmp_path, monkeypatch)
    assert "/skill-profile-builder" in prompt
    assert "profiles/意图画像/" in prompt
    assert "完善已存在" in prompt
    assert "润色/补全" in prompt
    assert "勿臆造" in prompt
    assert "完成度摘要" in prompt
    assert "做数码内容" in prompt


def test_profile_preparation_blocks_delete_and_releases_after_admission(
        tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setattr(worker, "PROFILES_DIR", profiles)
    monkeypatch.setattr(persona, "PROFILES_DIR", profiles)
    workspace = GatedWorkspace()
    store, auth, admin, member, relay, app = _app_with_tasks(
        tmp_path, workspace)
    holder: dict[str, object] = {}
    thread = None
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            _body, headers = _login(client, auth, admin, member)

            def submit():
                holder["response"] = client.post(
                    "/api/profile/build",
                    json={"name": "准备期画像", "form": {"direction": "方向"},
                          "turnId": "race-turn"},
                    headers=headers)

            def delete():
                holder["deleted"] = client.delete(
                    "/api/persona/准备期画像", headers=headers)

            thread = threading.Thread(target=submit)
            thread.start()
            assert workspace.ready.wait(5)
            deleter = threading.Thread(target=delete)
            deleter.start()
            # 基线已完成但 Run 尚未接纳；并发删除必须明确 busy，不能
            # 等到构建结束再自行删除。
            workspace.release.wait(0.2)
            deleted = holder.get("deleted")
            if deleted is None:
                workspace.release.set()
            deleter.join(10)
            assert not deleter.is_alive()
            deleted = holder["deleted"]
            assert deleted.status_code == 409, deleted.text
            assert deleted.json()["code"] == "profile_busy"
            workspace.release.set()
            thread.join(10)
            assert not thread.is_alive()
            response = holder["response"]
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "running"
            # Run 已完成，准备声明已释放，终态后编辑/删除可以继续。
            saved = client.put(
                "/api/persona/准备期画像/file",
                json={"filename": "identity.md", "content": "可保存"},
                headers=headers)
            assert saved.status_code == 200, saved.text
            allowed = client.delete("/api/persona/准备期画像", headers=headers)
            assert allowed.status_code == 200, allowed.text
            assert relay.calls and relay.calls[-1][1] == "profile"
    finally:
        workspace.release.set()
        if thread is not None and thread.is_alive():
            thread.join(10)
        store.close()


def test_same_name_concurrent_build_is_busy_while_preparing(
        tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setattr(worker, "PROFILES_DIR", profiles)
    monkeypatch.setattr(persona, "PROFILES_DIR", profiles)
    workspace = GatedWorkspace()
    store, auth, admin, member, relay, app = _app_with_tasks(
        tmp_path, workspace)
    holder: dict[str, object] = {}
    first_thread = None
    second_thread = None
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            _body, headers = _login(client, auth, admin, member)

            def first():
                holder["first"] = client.post(
                    "/api/profile/build",
                    json={"name": "并发同名画像",
                          "form": {"direction": "方向"},
                          "turnId": "same-name-1"},
                    headers=headers)

            def second():
                holder["second"] = client.post(
                    "/api/profile/build",
                    json={"name": "并发同名画像",
                          "form": {"direction": "方向"},
                          "turnId": "same-name-2"},
                    headers=headers)

            first_thread = threading.Thread(target=first)
            first_thread.start()
            assert workspace.ready.wait(5)
            second_thread = threading.Thread(target=second)
            second_thread.start()
            second_thread.join(2)
            if second_thread.is_alive():
                workspace.release.set()
                second_thread.join(10)
            workspace.release.set()
            first_thread.join(10)
            assert not first_thread.is_alive()
            second = holder.get("second")
            assert second is not None and second.status_code == 409, second
            assert second.json()["code"] == "profile_busy"
            assert holder["first"].status_code == 200
            assert len(relay.calls) == 1
    finally:
        workspace.release.set()
        for thread in (first_thread, second_thread):
            if thread is not None and thread.is_alive():
                thread.join(10)
        store.close()


class ReusingWorkspace(FakeWorkspace):
    def __init__(self) -> None:
        super().__init__()
        self.baseline_calls = 0

    async def request(self, actor, method, path, *, query=None, payload=None,
                      body=None, content_type=None, headers=None):
        if path == "/api/profile/baseline":
            self.baseline_calls += 1
            return httpx.Response(200, json={
                "created": self.baseline_calls == 1,
                "name": payload["name"],
                "instruction": "REUSE-PROMPT",
            })
        return await super().request(
            actor, method, path, query=query, payload=payload, body=body,
            content_type=content_type, headers=headers)


class IdempotentRelay(FakeRelay):
    def __init__(self, store: Store):
        super().__init__(store)
        self.by_turn: dict[tuple[str, str], str] = {}

    async def submit_task(self, actor, task_type, payload):
        key = (payload["sessionId"], payload["turnId"] or "")
        if key in self.by_turn:
            return {"runId": self.by_turn[key], "state": "done",
                    "duplicate": True}
        result = await super().submit_task(actor, task_type, payload)
        self.by_turn[key] = result["runId"]
        return result


def test_same_turn_profile_retry_does_not_dispatch_second_run(
        tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setattr(worker, "PROFILES_DIR", profiles)
    monkeypatch.setattr(persona, "PROFILES_DIR", profiles)
    with Store.open(tmp_path / "retry.sqlite3") as store:
        actor = store.create_user("RetryOwner")
        relay = IdempotentRelay(store)
        workspace = ReusingWorkspace()
        tasks = WorkspaceTasks(
            store, SessionService(store), relay, workspace)
        first = asyncio.run(tasks.build_profile(actor, {
            "name": "重试画像", "form": {"direction": "方向"},
            "turnId": "same-turn",
        }))
        second = asyncio.run(tasks.build_profile(actor, {
            "name": "重试画像", "form": {"direction": "方向"},
            "turnId": "same-turn",
        }))
        assert first["runId"] == second["runId"]
        assert len(relay.calls) == 1
        assert workspace.baseline_calls == 2


def test_profile_admission_failure_releases_claim_but_keeps_baseline(
        tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setattr(worker, "PROFILES_DIR", profiles)
    monkeypatch.setattr(persona, "PROFILES_DIR", profiles)

    class RealBaseline:
        async def request(self, actor, method, path, *, payload, **kwargs):
            result = await worker.api_profile_baseline(
                worker.ProfileBaselineRequest(**payload))
            return httpx.Response(200, json=result)

    with Store.open(tmp_path / "failure.sqlite3") as store:
        actor = store.create_user("FailureOwner")
        tasks = WorkspaceTasks(
            store, SessionService(store), BrokenRelay(store), RealBaseline())
        result = asyncio.run(tasks.build_profile(actor, {
            "name": "失败画像", "form": {"direction": "方向"},
            "turnId": "failed-turn",
        }))
        assert result["created"] is True
        assert result["status"] == "failed"
        assert (profiles / "失败画像" / "identity.md").is_file()
        # 接纳失败必须释放准备声明；同名新请求不能永久 busy。
        assert tasks.claim_profile(actor, "失败画像") is True
        tasks.release_profile(actor, "失败画像")
