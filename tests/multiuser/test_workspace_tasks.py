"""Task 5C1：control 技能/画像任务适配的授权、CSRF 与 Run 接纳。"""
from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.store import Store

ORIGIN = "https://easel.test"


class FakeWorkspace:
    def __init__(self):
        self.calls: list[tuple] = []
        self.skill_unconfigured = False
        self.skill_missing = False

    async def request(self, actor, method, path, *, query=None, payload=None,
                      body=None, content_type=None, headers=None):
        self.calls.append((actor, method, path, query, payload))
        if path == "/api/skills":
            return httpx.Response(200, json=[{
                "name": "demo", "description": "说明", "layer": "general",
                "needsApi": False, "apiConfigured": True,
            }])
        if path == "/api/skill/demo":
            return httpx.Response(200, json={
                "name": "demo", "layer": "general", "description": "说明",
                "body": "正文", "needsApi": False,
                "apiConfigured": True, "apiSpec": None,
            })
        if path == "/api/skill/prepare":
            if self.skill_missing:
                return httpx.Response(404, json={"detail": "missing"})
            return httpx.Response(200, json={
                "skill": "demo", "prompt": "SKILL-PROMPT",
                "needsApi": self.skill_unconfigured,
                "apiConfigured": not self.skill_unconfigured,
            })
        if path == "/api/profile/baseline":
            return httpx.Response(200, json={
                "created": True,
                "name": payload["name"],
                "instruction": "PROFILE-PROMPT",
            })
        return httpx.Response(404, json={"detail": "unknown"})


class FakeRelay:
    def __init__(self, store: Store, *, finish: bool = True):
        self.path = store.path
        self.finish = finish
        self.calls: list[tuple] = []
        self.counter = 0

    async def submit_task(self, actor, task_type, payload):
        self.calls.append((actor, task_type, dict(payload)))
        self.counter += 1
        turn = payload.get("turnId") or f"turn-{self.counter}"
        # TestClient 在事件循环线程调用；SQLite 连接不能跨线程共享。
        with Store.open(self.path) as store:
            run = store.reserve_run(
                actor, payload["sessionId"], turn,
                f"digest-{self.counter}", 0)
            store.mark_running(actor, run.id)
            if self.finish:
                store.finish_run(actor, run.id, "done")
        return {"runId": run.id, "state": "done", "duplicate": False}

    async def events(self, actor, run_id, *, after=0):
        return {"runId": run_id, "state": "done", "events": [],
                "nextAfter": after}

    async def messages(self, actor, session_id, *, limit=100, offset=0):
        with Store.open(self.path) as store:
            run = store.latest_run_for_conversation(actor, session_id)
        if run is None:
            return {"messages": []}
        return {"messages": [{
            "role": "assistant", "content": f"RESULT-{run.id}",
            "runId": run.id,
        }]}


def _app_with_tasks(tmp_path, workspace):
    store = Store.connect(tmp_path / "control.sqlite3")
    admin = store.create_user("Owner", "admin")
    member = store.create_user("Member")
    auth = AuthService(store)
    relay = FakeRelay(store)
    app = create_app(
        store=store, auth=auth,
        config=ControlConfig(public_origin=ORIGIN),
        workspace_api=workspace,
        relay_factory=lambda *args: relay)
    return store, auth, admin, member, relay, app


def _login(client, auth, admin, member):
    invite = auth.create_invite(admin, member.user_id)
    exchanged = client.post(
        "/api/auth/exchange", json={"token": invite},
        headers={"Origin": ORIGIN})
    assert exchanged.status_code == 200, exchanged.text
    body = exchanged.json()
    return body, {
        "Origin": ORIGIN,
        "X-CSRF-Token": body["csrfToken"],
    }


def test_skill_directory_and_detail_use_real_workspace_forwarding(tmp_path):
    workspace = FakeWorkspace()
    store, auth, admin, member, _relay, app = _app_with_tasks(
        tmp_path, workspace)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            body, headers = _login(client, auth, admin, member)
            listing = client.get("/api/skills")
            assert listing.status_code == 200
            assert listing.json()[0]["name"] == "demo"
            assert workspace.calls[-1][2] == "/api/skills"

            detail = client.get("/api/skill/demo")
            assert detail.status_code == 200
            assert detail.json()["body"] == "正文"
            assert workspace.calls[-1][2] == "/api/skill/demo"
            assert detail.json()["name"] == "demo"
    finally:
        store.close()


def test_skill_submission_uses_prepared_prompt_and_admitted_run(tmp_path):
    workspace = FakeWorkspace()
    store, auth, admin, member, relay, app = _app_with_tasks(
        tmp_path, workspace)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            body, headers = _login(client, auth, admin, member)
            response = client.post(
                "/api/skill",
                json={"skill": "demo", "input": "写一条文案",
                      "persona": "画像", "turnId": "turn-skill"},
                headers=headers)
            assert response.status_code == 202, response.text
            payload = response.json()
            assert payload["skill"] == "demo"
            assert payload["sessionId"]
            assert payload["runId"]
            submitted = relay.calls[-1]
            assert submitted[1] == "skill"
            assert submitted[2]["message"] == "SKILL-PROMPT"
            assert submitted[2]["persona"] == "画像"
            assert submitted[2]["turnId"] == "turn-skill"
            assert submitted[2]["sessionId"] == payload["sessionId"]

            status = client.get("/api/skill/status/demo")
            assert status.status_code == 200
            assert status.json()["state"] == "done"
            assert status.json()["response"].startswith("RESULT-")

            bad = client.post(
                "/api/skill",
                json={"skill": "demo", "input": "x", "extra": 1},
                headers=headers)
            assert bad.status_code == 400
            missing_csrf = client.post(
                "/api/skill",
                json={"skill": "demo", "input": "x"},
                headers={"Origin": ORIGIN})
            assert missing_csrf.status_code == 403

            workspace.skill_unconfigured = True
            unconfigured = client.post(
                "/api/skill",
                json={"skill": "demo", "input": "x"},
                headers=headers)
            assert unconfigured.status_code == 409
            assert unconfigured.json()["code"] == "skill_not_configured"
    finally:
        store.close()


def test_profile_build_writes_baseline_and_submits_enhancement_run(tmp_path):
    workspace = FakeWorkspace()
    store, auth, admin, member, relay, app = _app_with_tasks(
        tmp_path, workspace)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            body, headers = _login(client, auth, admin, member)
            built = client.post(
                "/api/profile/build",
                json={"name": "新画像",
                      "form": {"direction": "做数码内容"},
                      "turnId": "turn-profile"},
                headers=headers)
            assert built.status_code == 200, built.text
            payload = built.json()
            assert payload["created"] is True
            assert payload["status"] == "running"
            assert payload["sessionId"]
            assert relay.calls[-1][1] == "profile"
            message = relay.calls[-1][2]["message"]
            assert "/skill-profile-builder" in message
            assert "profiles/新画像/" in message
            assert message.endswith("PROFILE-PROMPT")
            assert relay.calls[-1][2]["turnId"] == "turn-profile"
            assert workspace.calls[-1][2] == "/api/profile/baseline"

            status = client.get("/api/profile/build/status/新画像")
            assert status.status_code == 200
            assert status.json()["state"] == "done"
            assert status.json()["log"].startswith("RESULT-")

            invalid = client.post(
                "/api/profile/build",
                json={"name": "../x", "form": {}},
                headers=headers)
            assert invalid.status_code == 400
    finally:
        store.close()


def test_profile_edit_and_delete_are_blocked_while_enhancement_runs(tmp_path):
    workspace = FakeWorkspace()
    store = Store.connect(tmp_path / "control.sqlite3")
    admin = store.create_user("Owner", "admin")
    member = store.create_user("Member")
    auth = AuthService(store)
    relay = FakeRelay(store, finish=False)
    app = create_app(
        store=store, auth=auth,
        config=ControlConfig(public_origin=ORIGIN),
        workspace_api=workspace,
        relay_factory=lambda *args: relay)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            _body, headers = _login(client, auth, admin, member)
            built = client.post(
                "/api/profile/build",
                json={"name": "并发画像",
                      "form": {"direction": "方向"},
                      "turnId": "turn-running"},
                headers=headers)
            assert built.status_code == 200, built.text
            saved = client.put(
                "/api/persona/并发画像/file",
                json={"filename": "identity.md", "content": "不应写入"},
                headers=headers)
            assert saved.status_code == 409, saved.text
            assert saved.json()["code"] == "profile_busy"
            deleted = client.delete(
                "/api/persona/并发画像", headers=headers)
            assert deleted.status_code == 409, deleted.text
            assert deleted.json()["code"] == "profile_busy"
    finally:
        store.close()
