"""Task 5C2：control 账号/登录/分析适配的红灯测试。

只用临时 Store、受控 relay/workspace 替身；不启动真实浏览器、不访问真实平台、
不读取真实 Key/Cookie。
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.store import Store
from easel.control.types import DomainError
from easel.control.workspace_accounts import (
    WorkspaceAccounts,
    canonical_platform,
    login_task_key,
)

ORIGIN = "https://easel.test"
PLATFORM = "xiaohongshu"


class FakeRelay:
    """用真实 Store 账本模拟 WorkerRelay 的接纳/事件/停止/结果。"""

    def __init__(self, store: Store):
        self.path = store.path
        self.submissions: list[tuple] = []
        self.states: dict[str, str] = {}
        self.results: dict[str, str] = {}
        self.stops: list[str] = []
        self.events_error: int | None = None
        self.submit_error: DomainError | None = None
        self.status_events: dict[str, list[dict]] = {}

    async def submit_task(self, actor, task_type, payload):
        if self.submit_error is not None:
            raise self.submit_error
        self.submissions.append((actor, task_type, dict(payload)))
        with Store.open(self.path) as store:
            existing = store.lookup_run(
                actor, payload["sessionId"], payload["turnId"],
                f"digest-{task_type}-{payload['turnId']}")
            if existing is not None:
                return {"runId": existing.id, "state": existing.status,
                        "duplicate": True}
            run = store.reserve_run(
                actor, payload["sessionId"], payload["turnId"],
                f"digest-{task_type}-{payload['turnId']}", 0)
            store.mark_running(actor, run.id)
        self.states[run.id] = "running"
        return {"runId": run.id, "state": "running", "duplicate": False}

    async def events(self, actor, run_id, *, after=0):
        if self.events_error is not None:
            raise DomainError("worker_gateway_unavailable", self.events_error)
        state = self.states.get(run_id, "running")
        return {"runId": run_id, "state": state,
                "events": list(self.status_events.get(run_id, [])),
                "lastEventId": int(after)}

    def status_event(self, run_id: str, state: str, message: str = "",
                     qr: str = "", qrTs: int = 0) -> None:
        """追加当前 Run 自己写下的 browser_status 事件。"""
        self.status_events.setdefault(run_id, []).append({
            "event": "activity",
            "data": json.dumps({
                "event": "browser_status", "state": state,
                "message": message, "qr": qr, "qrTs": qrTs,
            }, ensure_ascii=False),
        })

    async def messages(self, actor, conversation_id, *, limit=100, offset=0):
        with Store.open(self.path) as store:
            run = store.latest_run_for_conversation(actor, conversation_id)
        if run is None:
            return {"messages": []}
        text = self.results.get(run.id)
        if text is None:
            return {"messages": []}
        return {"messages": [{
            "role": "assistant", "content": text, "runId": run.id}]}

    async def stop(self, actor, payload):
        run_id = payload["runId"]
        self.stops.append(run_id)
        with Store.open(self.path) as store:
            store.finish_run(actor, run_id, "cancelled")
        self.states[run_id] = "cancelled"
        return {"runId": run_id, "state": "cancelled"}

    # 测试驱动终态
    def finish(self, actor, run_id, state, result=None):
        with Store.open(self.path) as store:
            store.finish_run(actor, run_id, state)
        self.states[run_id] = state
        if result is not None:
            self.results[run_id] = (
                result if isinstance(result, str)
                else json.dumps(result, ensure_ascii=False))


class FakeWorkspace:
    """签名短请求替身；记录 method/path/query/payload，按路径返回受控数据。"""

    def __init__(self):
        self.calls: list[tuple] = []
        self.status: dict[str, dict] = {}
        self.accounts = [
            {"platform": PLATFORM, "name": "小红书", "backend": "xhs",
             "supported": True, "loggedIn": False, "note": ""},
            {"platform": "wechat-oa", "name": "微信公众号",
             "backend": "wechat-oa", "supported": True,
             "loggedIn": False, "note": ""},
        ]
        self.analytics_platforms = [
            {"platform": PLATFORM, "name": "小红书", "loggedIn": False}]
        self.credentials = {
            "configured": False, "appIdMasked": "", "name": "", "author": ""}
        self.logout_result = {"ok": True, "deleted": ["profile"]}
        self.error: DomainError | None = None

    async def request(self, actor, method, path, *, query=None, payload=None,
                      body=None, content_type=None, headers=None):
        self.calls.append((actor, method, path, query, payload))
        if self.error is not None:
            raise self.error
        if path == "/api/accounts" and method == "GET":
            return httpx.Response(200, json=self.accounts)
        if path == "/api/analytics/platforms" and method == "GET":
            return httpx.Response(200, json=self.analytics_platforms)
        if path.endswith("/status") and method == "GET":
            platform = path.split("/")[3]
            return httpx.Response(200, json=self.status.get(platform, {
                "mode": "qr", "state": "unknown", "message": "",
                "qr": "", "qrTs": 0}))
        if path.endswith("/credentials") and method == "GET":
            return httpx.Response(200, json=self.credentials)
        if path.startswith("/api/logout/") and method == "POST":
            return httpx.Response(200, json=self.logout_result)
        if path.endswith("/credentials") and method == "POST":
            return httpx.Response(200, json={
                "ok": False, "message": "验证未通过"})
        if path.endswith("/sms") and method == "POST":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": "unknown"})


def _fixture(tmp_path):
    store = Store.connect(tmp_path / "control.sqlite3")
    admin = store.create_user("Owner", "admin")
    member = store.create_user("Member")
    other = store.create_user("Other")
    auth = AuthService(store)
    relay = FakeRelay(store)
    workspace = FakeWorkspace()
    accounts = WorkspaceAccounts(
        store, relay, workspace, poll_interval=0.01, clock=time.time)
    app = create_app(
        store=store, auth=auth,
        config=ControlConfig(public_origin=ORIGIN),
        workspace_api=workspace,
        relay_factory=lambda *args: relay)
    return store, auth, admin, member, other, relay, workspace, accounts, app


def _login(client, auth, admin, user):
    invite = auth.create_invite(admin, user.user_id)
    exchanged = client.post(
        "/api/auth/exchange", json={"token": invite},
        headers={"Origin": ORIGIN})
    assert exchanged.status_code == 200, exchanged.text
    body = exchanged.json()
    return body, {"Origin": ORIGIN, "X-CSRF-Token": body["csrfToken"]}


def test_canonical_platform_is_a_fixed_whitelist():
    assert canonical_platform("xiaohongshu") == "xiaohongshu"
    for bad in ("../x", "bad platform", "", "x" * 100, "xiaohongshu "):
        with pytest.raises(DomainError) as error:
            canonical_platform(bad)
        assert error.value.status in (400, 404)
    assert login_task_key(PLATFORM) != login_task_key("zhihu")


def test_account_and_analytics_lists_are_signed_short_reads(tmp_path):
    store, auth, admin, member, _other, relay, workspace, accounts, app = (
        _fixture(tmp_path))
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            _body, headers = _login(client, auth, admin, member)
            listed = client.get("/api/accounts")
            assert listed.status_code == 200, listed.text
            assert listed.json()[0]["platform"] == PLATFORM
            platforms = client.get("/api/analytics/platforms")
            assert platforms.status_code == 200, platforms.text
            assert platforms.json()[0]["platform"] == PLATFORM
            assert relay.submissions == []
            paths = [call[2] for call in workspace.calls]
            assert paths == ["/api/accounts", "/api/analytics/platforms"]
            assert headers["X-CSRF-Token"]
    finally:
        store.close()


def test_login_start_admits_one_browser_run_and_dedupes_same_request(
        tmp_path):
    store, _auth, _admin, member, _other, relay, workspace, accounts, app = (
        _fixture(tmp_path))
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            _body, headers = _login(client, _auth, _admin, member)
            first = client.post(
                f"/api/login/{PLATFORM}",
                json={"requestId": "login-request-1"},
                headers=headers)
            assert first.status_code == 200, first.text
            second = client.post(
                f"/api/login/{PLATFORM}",
                json={"requestId": "login-request-1"},
                headers=headers)
            assert second.status_code == 200, second.text
            assert first.json()["runId"] == second.json()["runId"]
            browser = [item for item in relay.submissions
                       if item[1] == "browser"]
            assert len(browser) == 1
            message = json.loads(browser[0][2]["message"])
            assert message == {"action": "login", "platform": PLATFORM}
            assert browser[0][2]["turnId"] == "login-request-1"
    finally:
        store.close()


def test_same_platform_second_request_reuses_active_run_without_dispatch(
        tmp_path):
    store, _auth, _admin, member, _other, relay, _workspace, _accounts, _app = (
        _fixture(tmp_path))
    try:
        async def scenario():
            first = await _accounts.login_start(
                member, PLATFORM, request_id="request-a")
            second = await _accounts.login_start(
                member, PLATFORM, request_id="request-b")
            assert first["runId"] == second["runId"]
            assert len(relay.submissions) == 1

        asyncio.run(scenario())
    finally:
        store.close()


def test_explicit_restart_stops_old_run_before_dispatching_new_one(tmp_path):
    store, _auth, _admin, member, _other, relay, _workspace, _accounts, _app = (
        _fixture(tmp_path))
    try:
        async def scenario():
            first = await _accounts.login_start(
                member, PLATFORM, request_id="request-old")
            restarted = await _accounts.login_start(
                member, PLATFORM, request_id="request-new", restart=True)
            assert first["runId"] != restarted["runId"]
            assert relay.stops == [first["runId"]]
            assert len(relay.submissions) == 2
            # 旧 Run 已终态，新 Run 才能占用同一平台会话
            assert relay.states[first["runId"]] == "cancelled"

        asyncio.run(scenario())
    finally:
        store.close()


def test_login_status_uses_run_terminal_state_and_releases_capacity(tmp_path):
    store, _auth, _admin, member, _other, relay, _workspace, accounts, _app = (
        _fixture(tmp_path))
    try:
        async def scenario():
            started = await accounts.login_start(
                member, PLATFORM, request_id="request-terminal")
            # 只有 worker Run 自己的终态才能推进 control Run 与容量。
            relay.finish(member, started["runId"], "done")
            status = await accounts.login_status(member, PLATFORM)
            assert status["state"] == "success"
            assert status["runId"] == started["runId"]
            with Store.open(store.path) as opened:
                run = opened.get_run(member, started["runId"])
            assert run.status == "done"

        asyncio.run(scenario())
    finally:
        store.close()


def test_shared_platform_success_does_not_finish_active_run(tmp_path):
    """共享 status 没有 Run 归属，不能推进新 Run 或释放容量。"""
    store, _auth, _admin, member, _other, relay, workspace, accounts, _app = (
        _fixture(tmp_path))
    try:
        async def scenario():
            started = await accounts.login_start(
                member, PLATFORM, request_id="request-shared-success")
            workspace.status[PLATFORM] = {
                "mode": "qr", "state": "success", "message": "旧成功",
                "qr": "", "qrTs": 0}
            status = await accounts.login_status(member, PLATFORM)
            assert status["state"] == "starting"
            with Store.open(store.path) as opened:
                run = opened.get_run(member, started["runId"])
                active = opened.count_active_runs_for_workspace(
                    member.workspace_id)
            assert run.status == "running"
            assert active == 1
            assert len(relay.submissions) == 1

        asyncio.run(scenario())
    finally:
        store.close()


def test_sms_only_continues_own_current_pending_task(tmp_path):
    store, _auth, _admin, member, other, relay, workspace, accounts, _app = (
        _fixture(tmp_path))
    try:
        async def scenario():
            started = await accounts.login_start(
                member, PLATFORM, request_id="request-sms")
            # 尚未进入 sms_required：拒绝
            with pytest.raises(DomainError) as early:
                await accounts.login_sms(member, PLATFORM, "1234")
            assert early.value.status == 409
            relay.status_event(
                started["runId"], "sms_required", "需短信")
            accepted = await accounts.login_sms(
                member, PLATFORM, "123456", run_id=started["runId"])
            assert accepted["ok"] is True
            # 另一身份不能续传该 Run
            with pytest.raises(DomainError) as foreign:
                await accounts.login_sms(
                    other, PLATFORM, "123456", run_id=started["runId"])
            assert foreign.value.status == 404
            # 指定旧 Run 也不能冒充当前任务
            with pytest.raises(DomainError):
                await accounts.login_sms(
                    member, PLATFORM, "123456", run_id="unknown-run")

        asyncio.run(scenario())
    finally:
        store.close()


def test_logout_stops_active_task_then_calls_worker_and_does_not_revive(
        tmp_path):
    store, _auth, _admin, member, _other, relay, workspace, accounts, _app = (
        _fixture(tmp_path))
    try:
        async def scenario():
            started = await accounts.login_start(
                member, PLATFORM, request_id="request-logout")
            workspace.status[PLATFORM] = {
                "mode": "qr", "state": "success", "message": "登录成功",
                "qr": "", "qrTs": 0}
            result = await accounts.logout(member, PLATFORM)
            assert result["ok"] is True
            assert relay.stops == [started["runId"]]
            # 注销后 worker 状态即使仍被伪造为 success，也不能把 Run 复活
            with Store.open(store.path) as opened:
                run = opened.get_run(member, started["runId"])
            assert run.status == "cancelled"
            status = await accounts.login_status(member, PLATFORM)
            assert status["state"] != "success"

        asyncio.run(scenario())
    finally:
        store.close()


def test_worker_restart_marks_run_interrupted_and_releases_capacity(tmp_path):
    store, _auth, _admin, member, _other, relay, _workspace, accounts, _app = (
        _fixture(tmp_path))
    try:
        async def scenario():
            started = await accounts.login_start(
                member, PLATFORM, request_id="request-restart")
            relay.states[started["runId"]] = "interrupted"
            with Store.open(store.path) as opened:
                opened.finish_run(member, started["runId"], "interrupted")
            status = await accounts.login_status(member, PLATFORM)
            assert status["state"] == "error"
            with Store.open(store.path) as opened:
                run = opened.get_run(member, started["runId"])
            assert run.status == "interrupted"

        asyncio.run(scenario())
    finally:
        store.close()


def test_whoami_uses_browser_run_and_returns_only_identity(tmp_path):
    store, _auth, _admin, member, _other, relay, _workspace, accounts, _app = (
        _fixture(tmp_path))
    try:
        async def scenario():
            task = asyncio.create_task(accounts.whoami(
                member, PLATFORM, force=True, request_id="whoami-1"))
            while not relay.submissions:
                await asyncio.sleep(0.005)
            actor, task_type, payload = relay.submissions[0]
            assert task_type == "browser"
            assert json.loads(payload["message"]) == {
                "action": "whoami", "platform": PLATFORM, "force": True}
            run_id = payload["turnId"]
            # 找到 Run id 后完成
            with Store.open(store.path) as opened:
                conversation = opened.get_task_conversation(
                    actor, "browser:whoami:" + PLATFORM)
                run = opened.latest_run_for_conversation(
                    actor, conversation.id)
            relay.finish(member, run.id, "done", {
                "loggedIn": True, "name": "昵称",
                "avatar": "https://cdn.test/a.png"})
            result = await task
            assert result == {
                "loggedIn": True, "name": "昵称",
                "avatar": "https://cdn.test/a.png"}

        asyncio.run(scenario())
    finally:
        store.close()


def test_analytics_joins_active_refresh_and_returns_result(tmp_path):
    store, _auth, _admin, member, _other, relay, _workspace, accounts, _app = (
        _fixture(tmp_path))
    try:
        async def scenario():
            first = asyncio.create_task(accounts.analytics(
                member, PLATFORM, request_id="analytics-1"))
            second = asyncio.create_task(accounts.analytics(
                member, PLATFORM, request_id="analytics-2"))
            while len(relay.submissions) < 1:
                await asyncio.sleep(0.005)
            await asyncio.sleep(0.02)
            assert len(relay.submissions) == 1
            actor, task_type, payload = relay.submissions[0]
            assert task_type == "analytics"
            assert json.loads(payload["message"]) == {
                "action": "analytics", "platform": PLATFORM}
            with Store.open(store.path) as opened:
                conversation = opened.get_task_conversation(
                    actor, "browser:analytics:" + PLATFORM)
                run = opened.latest_run_for_conversation(
                    actor, conversation.id)
            payload_result = {
                "platform": PLATFORM, "loggedIn": True,
                "followers": None, "likes": 12, "following": None,
                "posts": None, "metrics": [], "notes": [],
                "growth": {}, "fetched_at": 1234,
            }
            relay.finish(member, run.id, "done", payload_result)
            one, two = await asyncio.gather(first, second)
            assert one == payload_result == two

        asyncio.run(scenario())
    finally:
        store.close()


def test_analytics_failure_is_not_fabricated_as_zero(tmp_path):
    store, _auth, _admin, member, _other, relay, _workspace, accounts, _app = (
        _fixture(tmp_path))
    try:
        async def scenario():
            task = asyncio.create_task(accounts.analytics(
                member, PLATFORM, request_id="analytics-fail"))
            while not relay.submissions:
                await asyncio.sleep(0.005)
            actor = relay.submissions[0][0]
            with Store.open(store.path) as opened:
                conversation = opened.get_task_conversation(
                    actor, "browser:analytics:" + PLATFORM)
                run = opened.latest_run_for_conversation(
                    actor, conversation.id)
            relay.finish(member, run.id, "failed")
            with pytest.raises(DomainError) as error:
                await task
            assert error.value.status in (502, 504)

        asyncio.run(scenario())
    finally:
        store.close()


def test_capacity_full_rejects_new_browser_task(tmp_path):
    store, _auth, _admin, member, _other, relay, _workspace, accounts, _app = (
        _fixture(tmp_path))
    try:
        async def scenario():
            await accounts.login_start(
                member, PLATFORM, request_id="cap-1")
            await accounts.login_start(
                member, "zhihu", request_id="cap-2")
            with pytest.raises(DomainError) as error:
                await accounts.login_start(
                    member, "douyin", request_id="cap-3")
            assert error.value.status == 429
            assert error.value.code == "workspace_capacity"

        asyncio.run(scenario())
    finally:
        store.close()


def test_a_b_same_platform_task_and_result_are_isolated(tmp_path):
    store, _auth, _admin, member, other, relay, _workspace, accounts, _app = (
        _fixture(tmp_path))
    try:
        async def scenario():
            a = await accounts.login_start(
                member, PLATFORM, request_id="same-request")
            b = await accounts.login_start(
                other, PLATFORM, request_id="same-request")
            assert a["runId"] != b["runId"]
            with Store.open(store.path) as opened:
                conv_a = opened.get_task_conversation(
                    member, login_task_key(PLATFORM))
                conv_b = opened.get_task_conversation(
                    other, login_task_key(PLATFORM))
            assert conv_a.id != conv_b.id
            status_b = await accounts.login_status(other, PLATFORM)
            assert status_b["runId"] == b["runId"]
            status_a = await accounts.login_status(member, PLATFORM)
            assert status_a["runId"] == a["runId"]

        asyncio.run(scenario())
    finally:
        store.close()


def _analytics_payload():
    return {
        "platform": PLATFORM, "loggedIn": True,
        "followers": 12, "likes": None, "following": None, "posts": None,
        "metrics": [], "notes": [], "growth": {}, "fetched_at": 1234,
    }


def test_analytics_get_is_read_only_and_cross_site_navigation_cannot_start(
        tmp_path):
    """顶层跨站 GET 只能读缓存；没有明确动作时 executions 必须为 0。"""
    store, auth, admin, member, _other, relay, _workspace, _accounts, app = (
        _fixture(tmp_path))
    try:
        async def scenario():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        base_url=ORIGIN) as client:
                    invite = auth.create_invite(admin, member.user_id)
                    exchanged = await client.post(
                        "/api/auth/exchange", json={"token": invite},
                        headers={"Origin": ORIGIN})
                    assert exchanged.status_code == 200, exchanged.text
                    task = asyncio.create_task(client.get(
                        f"/api/analytics/{PLATFORM}",
                        headers={
                            "Sec-Fetch-Site": "cross-site",
                            "Sec-Fetch-Mode": "navigate",
                            "Sec-Fetch-Dest": "document",
                        }))
                    await asyncio.sleep(0.2)
                    if relay.submissions:
                        # 旧实现会在这里启动任务；先收尾，让断言可读地失败。
                        run_id = next(iter(relay.states))
                        relay.finish(member, run_id, "done",
                                     _analytics_payload())
                    response = await asyncio.wait_for(task, 3)
                    assert relay.submissions == [], (
                        "cross-site GET must not start an analytics Run")
                    assert response.status_code == 404, response.text
                    assert response.json()["code"] == "analytics_cache_missing"

        asyncio.run(scenario())
    finally:
        store.close()


def test_analytics_refresh_requires_explicit_csrf_post(tmp_path):
    """启动刷新只允许带正确 Origin+CSRF 的显式 POST。"""
    store, auth, admin, member, _other, relay, _workspace, _accounts, app = (
        _fixture(tmp_path))
    try:
        async def scenario():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        base_url=ORIGIN) as client:
                    invite = auth.create_invite(admin, member.user_id)
                    exchanged = await client.post(
                        "/api/auth/exchange", json={"token": invite},
                        headers={"Origin": ORIGIN})
                    csrf = exchanged.json()["csrfToken"]
                    path = f"/api/analytics/{PLATFORM}"
                    no_csrf = await client.post(path)
                    assert no_csrf.status_code == 403, no_csrf.text
                    wrong_origin = await client.post(path, headers={
                        "Origin": "https://evil.test",
                        "X-CSRF-Token": csrf})
                    assert wrong_origin.status_code == 403, wrong_origin.text
                    assert relay.submissions == []

                    first = asyncio.create_task(client.post(path, headers={
                        "Origin": ORIGIN, "X-CSRF-Token": csrf}))
                    second = asyncio.create_task(client.post(path, headers={
                        "Origin": ORIGIN, "X-CSRF-Token": csrf}))
                    deadline = asyncio.get_running_loop().time() + 3
                    while not relay.submissions:
                        if asyncio.get_running_loop().time() > deadline:
                            raise AssertionError("refresh did not start")
                        await asyncio.sleep(0.01)
                    await asyncio.sleep(0.05)
                    assert len(relay.submissions) == 1
                    run_id = next(iter(relay.states))
                    relay.finish(member, run_id, "done", _analytics_payload())
                    one, two = await asyncio.gather(first, second)
                    assert one.status_code == 200, one.text
                    assert two.status_code == 200, two.text
                    assert one.json() == two.json() == _analytics_payload()

        asyncio.run(scenario())
    finally:
        store.close()


def test_analytics_get_reads_last_completed_cache_without_dispatch(tmp_path):
    store, auth, admin, member, _other, relay, _workspace, _accounts, app = (
        _fixture(tmp_path))
    try:
        async def scenario():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        base_url=ORIGIN) as client:
                    invite = auth.create_invite(admin, member.user_id)
                    exchanged = await client.post(
                        "/api/auth/exchange", json={"token": invite},
                        headers={"Origin": ORIGIN})
                    headers = {
                        "Origin": ORIGIN,
                        "X-CSRF-Token": exchanged.json()["csrfToken"],
                    }
                    refresh = asyncio.create_task(client.post(
                        f"/api/analytics/{PLATFORM}", headers=headers))
                    deadline = asyncio.get_running_loop().time() + 3
                    while not relay.submissions:
                        if asyncio.get_running_loop().time() > deadline:
                            raise AssertionError("refresh did not start")
                        await asyncio.sleep(0.01)
                    run_id = next(iter(relay.states))
                    relay.finish(member, run_id, "done", _analytics_payload())
                    assert (await refresh).status_code == 200
                    submissions = len(relay.submissions)
                    cached = await client.get(
                        f"/api/analytics/{PLATFORM}")
                    assert cached.status_code == 200, cached.text
                    assert cached.json() == _analytics_payload()
                    assert len(relay.submissions) == submissions

        asyncio.run(scenario())
    finally:
        store.close()


def test_unknown_platform_and_extra_body_keys_are_rejected(tmp_path):
    store, _auth, _admin, member, _other, _relay, _workspace, _accounts, app = (
        _fixture(tmp_path))
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            _body, headers = _login(client, _auth, _admin, member)
            unknown = client.post(
                "/api/login/not-a-platform",
                json={"requestId": "x"}, headers=headers)
            assert unknown.status_code in (400, 404)
            extra = client.post(
                f"/api/login/{PLATFORM}",
                json={"requestId": "x", "script": "/bin/sh"},
                headers=headers)
            assert extra.status_code == 400
            no_csrf = client.post(
                f"/api/login/{PLATFORM}", json={"requestId": "x"},
                headers={"Origin": ORIGIN})
            assert no_csrf.status_code == 403
            client.cookies.clear()
            unauth = client.get("/api/accounts")
            assert unauth.status_code == 401
    finally:
        store.close()


def test_credentials_post_runs_verification_and_returns_masked_result(
        tmp_path):
    store, _auth, _admin, member, _other, relay, workspace, accounts, _app = (
        _fixture(tmp_path))
    try:
        async def scenario():
            task = asyncio.create_task(accounts.save_credentials(
                member, "wechat-oa",
                {"appId": "wx1234567890", "appSecret": "secret-value",
                 "name": "公众号", "author": ""},
                request_id="cred-1"))
            while not relay.submissions:
                await asyncio.sleep(0.005)
            actor, task_type, payload = relay.submissions[0]
            assert task_type == "browser"
            descriptor = json.loads(payload["message"])
            assert descriptor["action"] == "credentials"
            assert "secret-value" not in payload["message"]
            while not [call for call in workspace.calls
                       if call[1] == "POST"
                       and call[2].endswith("/credentials")]:
                await asyncio.sleep(0.005)
            handoff = [call for call in workspace.calls
                       if call[1] == "POST"
                       and call[2].endswith("/credentials")][-1]
            assert handoff[4]["appSecret"] == "secret-value"
            assert isinstance(handoff[4]["runId"], str)
            assert handoff[4]["runId"]
            with Store.open(store.path) as opened:
                conversation = opened.get_task_conversation(
                    actor, "browser:credentials:wechat-oa")
                run = opened.latest_run_for_conversation(
                    actor, conversation.id)
            relay.finish(member, run.id, "done", {
                "ok": False, "message": "验证未通过"})
            result = await task
            assert result == {"ok": False, "message": "验证未通过"}

        asyncio.run(scenario())
    finally:
        store.close()
