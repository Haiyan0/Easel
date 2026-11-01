"""Task 5C3：control 发布/导出受管适配的红灯测试。

只使用临时 Store、受控 relay/workspace 替身；不启动真实浏览器、不访问真实
平台、不读取真实凭证。
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
from easel.control.workspace_publishing import (
    EXPORT_PATH,
    WorkspacePublishing,
    canonical_export_path,
    canonical_media_path,
    canonical_platform,
    export_task_key,
    publish_task_key,
)
from easel.worker.config import run_input_digest

ORIGIN = "https://easel.test"
PLATFORM = "xiaohongshu"
DOUYIN = "douyin"


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
        self.message_log: list[dict] | None = None
        # 测试驱动：submit 时是否立即完成，以及完成内容。
        self.auto_state: str | None = None
        self.auto_result: object = None
        self.submit_started: asyncio.Event | None = None

    async def submit_task(self, actor, task_type, payload):
        if self.submit_error is not None:
            raise self.submit_error
        # 与真实 WorkerRelay 一致：摘要覆盖会话、任务类型、正文/附件/人设。
        digest = run_input_digest(
            conversation_id=payload["sessionId"], task_type=task_type,
            message=payload["message"], attachments=payload["attachments"],
            persona=payload.get("persona"))
        with Store.open(self.path) as store:
            existing = store.lookup_run(
                actor, payload["sessionId"], payload["turnId"], digest)
            if existing is not None:
                return {"runId": existing.id, "state": existing.status,
                        "duplicate": True}
            self.submissions.append((actor, task_type, dict(payload)))
            if self.submit_started is not None:
                self.submit_started.set()
            run = store.reserve_run(
                actor, payload["sessionId"], payload["turnId"], digest, 0)
            store.mark_running(actor, run.id)
        self.states[run.id] = "running"
        if self.auto_state is not None:
            with Store.open(self.path) as store:
                store.finish_run(actor, run.id, self.auto_state)
            self.states[run.id] = self.auto_state
            if self.auto_result is not None:
                self.results[run.id] = (
                    self.auto_result if isinstance(self.auto_result, str)
                    else json.dumps(self.auto_result, ensure_ascii=False))
        return {"runId": run.id, "state": self.states[run.id],
                "duplicate": False}

    async def events(self, actor, run_id, *, after=0):
        if self.events_error is not None:
            raise DomainError("worker_gateway_unavailable", self.events_error)
        state = self.states.get(run_id, "running")
        return {"runId": run_id, "state": state,
                "events": list(self.status_events.get(run_id, [])),
                "lastEventId": int(after)}

    def status_event(self, run_id: str, state: str, message: str = "") -> None:
        self.status_events.setdefault(run_id, []).append({
            "event": "activity",
            "data": json.dumps({
                "event": "browser_status", "state": state,
                "message": message,
            }, ensure_ascii=False),
        })

    async def messages(self, actor, conversation_id, *, limit=100, offset=0):
        if self.message_log is not None:
            page = self.message_log[offset:offset + limit]
            return {"messages": page, "total": len(self.message_log)}
        with Store.open(self.path) as store:
            runs = store.list_runs(actor, conversation_id)
        # 与真实 relay 一致：会话内多个 Run 的助手结果都可见，由调用方按
        # runId 选取本次 Run 的结果，不能只返回“最近一个 Run”。
        messages = [{
            "role": "assistant", "content": text, "runId": run.id}
            for run in runs for text in [self.results.get(run.id)]
            if text is not None]
        return {"messages": messages[offset:offset + limit],
                "total": len(messages)}

    async def stop(self, actor, payload):
        run_id = payload["runId"]
        self.stops.append(run_id)
        with Store.open(self.path) as store:
            store.finish_run(actor, run_id, "cancelled")
        self.states[run_id] = "cancelled"
        return {"runId": run_id, "state": "cancelled"}

    def finish(self, actor, run_id, state, result=None):
        with Store.open(self.path) as store:
            store.finish_run(actor, run_id, state)
        self.states[run_id] = state
        if result is not None:
            self.results[run_id] = (
                result if isinstance(result, str)
                else json.dumps(result, ensure_ascii=False))


class FakeWorkspace:
    """签名短请求替身；记录 method/path/payload。"""

    def __init__(self):
        self.calls: list[tuple] = []
        self.error: DomainError | None = None

    async def request(self, actor, method, path, *, query=None, payload=None,
                      body=None, content_type=None, headers=None):
        self.calls.append((actor, method, path, query, payload))
        if self.error is not None:
            raise self.error
        if path.endswith("/sms") and method == "POST":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": "unknown"})


def _fixture(tmp_path, *, publish_mode="automated"):
    store = Store.connect(tmp_path / "control.sqlite3")
    admin = store.create_user("Owner", "admin")
    member = store.create_user("Member")
    other = store.create_user("Other")
    auth = AuthService(store)
    relay = FakeRelay(store)
    workspace = FakeWorkspace()
    app = create_app(
        store=store, auth=auth,
        config=ControlConfig(public_origin=ORIGIN,
                             publish_mode=publish_mode),
        workspace_api=workspace,
        relay_factory=lambda *args: relay)
    return store, auth, admin, member, other, relay, workspace, app


def _login(client, auth, admin, user):
    invite = auth.create_invite(admin, user.user_id)
    exchanged = client.post(
        "/api/auth/exchange", json={"token": invite},
        headers={"Origin": ORIGIN})
    assert exchanged.status_code == 200, exchanged.text
    body = exchanged.json()
    return body, {"Origin": ORIGIN, "X-CSRF-Token": body["csrfToken"]}


def _service(store, relay, workspace, publish_mode="automated"):
    return WorkspacePublishing(
        store, relay, workspace, publish_mode=publish_mode,
        poll_interval=0.01, clock=time.time)


def _publish_payload(**overrides):
    payload = {"title": "测试标题", "body": "测试正文", "tags": "工具",
               "media": ["project/cover.png"]}
    payload.update(overrides)
    return payload


def _with_request(payload, request_id="publish-1"):
    return {**payload, "requestId": request_id}


def test_canonical_platform_and_paths_are_fixed_whitelists():
    assert canonical_platform("xiaohongshu") == "xiaohongshu"
    for bad in ("../x", "bad platform", "", "x" * 100, "xiaohongshu "):
        with pytest.raises(DomainError):
            canonical_platform(bad)
    assert publish_task_key(PLATFORM) != publish_task_key(DOUYIN)

    assert canonical_media_path("project/cover.png") == "project/cover.png"
    for bad in ("../outside.png", "/abs.png", "_login/a.png",
                "project/../a.png", "project/.hidden.png",
                "project/a\\b.png", "project/x.png\x00"):
        with pytest.raises(DomainError):
            canonical_media_path(bad)

    export = "_publish/manual/easel-xiaohongshu-0123456789ab.zip"
    assert canonical_export_path(export) == export
    for bad in ("_publish/manual/other.zip", "_publish/manual/easel-x.ts",
                "_publish/manual/easel-xiaohongshu-0123.zip",
                "_publish/manual/easel-unknown-0123456789ab.zip",
                "_publish/manual/sub/easel-xiaohongshu-0123456789ab.zip"):
        with pytest.raises(DomainError):
            canonical_export_path(bad)
    assert EXPORT_PATH


def test_policy_comes_from_control_not_worker_environment(tmp_path):
    store, auth, admin, member, _other, relay, workspace, app = _fixture(
        tmp_path, publish_mode="manual")
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            _body, _headers = _login(client, auth, admin, member)
            policy = client.get("/api/publishing-policy")
            assert policy.status_code == 200, policy.text
            assert policy.json() == {
                "mode": "manual", "automaticSubmission": False}
            assert relay.submissions == []
            assert workspace.calls == []
    finally:
        store.close()


def test_manual_policy_rejects_publish_before_capacity_or_dispatch(tmp_path):
    store, auth, admin, member, _other, relay, workspace, app = _fixture(
        tmp_path, publish_mode="manual")
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            _body, headers = _login(client, auth, admin, member)
            response = client.post(
                f"/api/publish/{PLATFORM}", json=_with_request(_publish_payload(media=["project/clip.mp4"])),
                headers=headers)
            assert response.status_code == 403, response.text
            assert response.json()["code"] in (
                "publishing_manual_only", "forbidden")
            sms = client.post(
                f"/api/publish/{PLATFORM}/sms",
                json={"code": "123456"}, headers=headers)
            assert sms.status_code == 403, sms.text
            assert relay.submissions == []
            assert workspace.calls == []
    finally:
        store.close()


def test_auto_publish_admits_one_owned_run_and_dedupes_same_request(tmp_path):
    store, auth, admin, member, _other, relay, _workspace, app = _fixture(
        tmp_path)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            _body, headers = _login(client, auth, admin, member)
            first = client.post(
                f"/api/publish/{DOUYIN}", json=_with_request(_publish_payload(media=["project/clip.mp4"])),
                headers=headers)
            assert first.status_code == 200, first.text
            body = first.json()
            assert body["async"] is True and body["pending"] is True
            assert body["runId"]
            second = client.post(
                f"/api/publish/{DOUYIN}", json=_with_request(_publish_payload(media=["project/clip.mp4"])),
                headers=headers)
            assert second.status_code == 200, second.text
            assert second.json()["runId"] == body["runId"]
            assert len(relay.submissions) == 1
            actor, task_type, payload = relay.submissions[0]
            assert actor.user_id == member.user_id
            assert task_type == "publish"
            descriptor = json.loads(payload["message"])
            assert descriptor["action"] == "publish"
            assert descriptor["platform"] == DOUYIN
            assert descriptor["title"] == "测试标题"
            assert descriptor["media"] == ["project/clip.mp4"]
            assert payload["turnId"] == "publish-1"

            # 同一平台不同请求在运行中不能开启第二次外部动作。
            busy = client.post(
                f"/api/publish/{DOUYIN}",
                json=_with_request(_publish_payload(media=["project/clip.mp4"]), "publish-2"),
                headers=headers)
            assert busy.status_code == 409, busy.text
            assert len(relay.submissions) == 1
    finally:
        store.close()


def test_same_request_id_changed_input_conflicts_while_active(tmp_path):
    store, _auth, _admin, member, _other, relay, workspace, _app = _fixture(
        tmp_path)
    try:
        async def scenario():
            service = _service(store, relay, workspace)
            first = await service.publish(
                member, DOUYIN,
                _publish_payload(media=["project/clip.mp4"]),
                request_id="same-request")
            with pytest.raises(DomainError) as conflict:
                await service.publish(
                    member, DOUYIN,
                    _publish_payload(
                        title="修改后的标题", body="修改后的正文",
                        media=["project/clip.mp4"]),
                    request_id="same-request")
            assert conflict.value.status == 409
            assert conflict.value.code == "request_conflict"
            # 异输入不能重复接纳，也不能开启第二次外部动作。
            assert len(relay.submissions) == 1
            same = await service.publish(
                member, DOUYIN,
                _publish_payload(media=["project/clip.mp4"]),
                request_id="same-request")
            assert same["runId"] == first["runId"]
            assert len(relay.submissions) == 1

        asyncio.run(scenario())
    finally:
        store.close()


def test_same_request_id_changed_input_conflicts_after_terminal(tmp_path):
    store, _auth, _admin, member, _other, relay, workspace, _app = _fixture(
        tmp_path)
    try:
        async def scenario():
            relay.auto_state = "done"
            relay.auto_result = {
                "state": "success", "ok": True, "message": "发布成功"}
            service = _service(store, relay, workspace)
            first = await service.publish(
                member, DOUYIN,
                _publish_payload(media=["project/clip.mp4"]),
                request_id="terminal-request")
            with pytest.raises(DomainError) as conflict:
                await service.publish(
                    member, DOUYIN,
                    _publish_payload(
                        title="终态改稿", body="终态改稿",
                        media=["project/clip.mp4"]),
                    request_id="terminal-request")
            assert conflict.value.status == 409
            assert conflict.value.code == "request_conflict"
            assert len(relay.submissions) == 1
            same = await service.publish(
                member, DOUYIN,
                _publish_payload(media=["project/clip.mp4"]),
                request_id="terminal-request")
            assert same["runId"] == first["runId"]
            assert len(relay.submissions) == 1

        asyncio.run(scenario())
    finally:
        store.close()


def test_export_same_request_id_changed_input_conflicts_while_active(
        tmp_path):
    store, _auth, _admin, member, _other, relay, workspace, _app = _fixture(
        tmp_path)
    try:
        async def scenario():
            service = _service(store, relay, workspace)
            path = "_publish/manual/easel-xiaohongshu-0123456789ab.zip"
            payload = {"title": "标题", "body": "正文", "tags": "",
                       "media": [], "reviewed": True}
            first = asyncio.create_task(service.manual_export(
                member, PLATFORM, payload, request_id="export-same"))
            while not relay.submissions:
                await asyncio.sleep(0.005)
            changed = asyncio.create_task(service.manual_export(
                member, PLATFORM,
                {**payload, "title": "改稿", "body": "改稿"},
                request_id="export-same"))
            with Store.open(store.path) as opened:
                conversation = opened.get_task_conversation(
                    member, export_task_key(PLATFORM))
                run = opened.latest_run_for_conversation(
                    member, conversation.id)
            relay.finish(member, run.id, "done", {
                "path": path, "filename": path.rsplit("/", 1)[-1],
                "published": False})
            result = await first
            assert result["path"] == path
            try:
                await changed
            except DomainError as error:
                assert error.status == 409
            else:
                pytest.fail("同 requestId 的改稿导出被当作原稿件包接纳")
            assert len(relay.submissions) == 1

        asyncio.run(scenario())
    finally:
        store.close()


def test_two_users_same_platform_have_isolated_runs(tmp_path):
    store, _auth, _admin, member, other, relay, workspace, _app = _fixture(
        tmp_path)
    try:
        async def scenario():
            service = _service(store, relay, workspace)
            a = await service.publish(
                member, DOUYIN, _publish_payload(media=["project/clip.mp4"]), request_id="same-request")
            b = await service.publish(
                other, DOUYIN, _publish_payload(media=["project/clip.mp4"]), request_id="same-request")
            assert a["runId"] != b["runId"]
            with Store.open(store.path) as opened:
                conv_a = opened.get_task_conversation(
                    member, publish_task_key(DOUYIN))
                conv_b = opened.get_task_conversation(
                    other, publish_task_key(DOUYIN))
            assert conv_a.id != conv_b.id
            own_a = await service.status(member, DOUYIN)
            own_b = await service.status(other, DOUYIN)
            assert own_a["runId"] == a["runId"]
            assert own_b["runId"] == b["runId"]

        asyncio.run(scenario())
    finally:
        store.close()


def test_status_by_request_id_returns_exact_owned_run(tmp_path):
    store, _auth, _admin, member, other, relay, workspace, app = _fixture(
        tmp_path)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            _body, headers = _login(client, _auth, _admin, member)
            first = client.post(
                f"/api/publish/{PLATFORM}",
                json=_with_request(_publish_payload(), "intent-a"),
                headers=headers)
            assert first.status_code == 200, first.text
            first_run = first.json()["runId"]
            relay.finish(member, first_run, "done", {
                "state": "success", "ok": True, "message": "发布成功"})
            # 明确完成后的再次发布是新意图；旧意图仍可按 requestId 精确查询。
            second = client.post(
                f"/api/publish/{PLATFORM}",
                json=_with_request(_publish_payload(), "intent-b"),
                headers=headers)
            assert second.status_code == 200, second.text
            second_run = second.json()["runId"]
            assert second_run != first_run

            old = client.get(
                f"/api/publish/{PLATFORM}/status?requestId=intent-a")
            assert old.status_code == 200, old.text
            assert old.json()["runId"] == first_run
            assert old.json()["state"] == "success"

            unknown = client.get(
                f"/api/publish/{PLATFORM}/status?requestId=never-sent")
            assert unknown.status_code == 200, unknown.text
            assert unknown.json()["runId"] == ""
            assert unknown.json()["state"] == "unknown"

            bad = client.get(
                f"/api/publish/{PLATFORM}/status?requestId=intent-a&other=1",
                headers=headers)
            assert bad.status_code == 400, bad.text
            invalid = client.get(
                f"/api/publish/{PLATFORM}/status?requestId=bad request",
                headers=headers)
            assert invalid.status_code == 400, invalid.text
            # 未带 requestId 的旧查询仍返回当前 Run。
            latest = client.get(f"/api/publish/{PLATFORM}/status")
            assert latest.status_code == 200
            assert latest.json()["runId"] == second_run

        async def isolated():
            service = _service(store, relay, workspace)
            foreign_status = await service.status(
                other, PLATFORM, request_id="intent-a")
            assert foreign_status["runId"] == ""
            assert foreign_status["state"] == "unknown"

        asyncio.run(isolated())
    finally:
        store.close()


def test_status_reads_only_current_run_events_and_never_legacy_state(tmp_path):
    store, _auth, _admin, member, _other, relay, _workspace, app = _fixture(
        tmp_path)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            _body, headers = _login(client, _auth, _admin, member)
            started = client.post(
                f"/api/publish/{DOUYIN}", json=_with_request(_publish_payload(media=["project/clip.mp4"])),
                headers=headers).json()
            run_id = started["runId"]
            wait = client.get(f"/api/publish/{DOUYIN}/status")
            assert wait.status_code == 200, wait.text
            assert wait.json()["state"] == "starting"
            assert wait.json()["runId"] == run_id

            # 脚本先写 success 但 Run 未终态：不能提前结算为成功。
            relay.status_event(run_id, "success", "发布成功")
            pending = client.get(f"/api/publish/{DOUYIN}/status")
            assert pending.status_code == 200, pending.text
            assert pending.json()["state"] == "starting"

            relay.status_event(run_id, "sms_required", "请输入验证码")
            sms = client.get(f"/api/publish/{DOUYIN}/status")
            assert sms.status_code == 200, sms.text
            assert sms.json()["state"] == "sms_required"

            # 旧共享状态即使 success 也不能推进当前 Run 或释放容量。
            relay.finish(member, run_id, "cancelled")
            cancelled = client.get(f"/api/publish/{DOUYIN}/status")
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["state"] == "unknown"
            assert cancelled.json()["state"] != "success"
            # 状态读取只走 Run 事件，不读取 worker 共享平台 status 文件。
            assert _workspace.calls == []
    finally:
        store.close()


def test_async_publish_status_maps_success_unknown_and_error(tmp_path):
    store, _auth, _admin, member, _other, relay, workspace, app = _fixture(
        tmp_path)
    try:
        async def scenario():
            service = _service(store, relay, workspace)
            relay.auto_state = "done"
            relay.auto_result = {
                "state": "success", "ok": True,
                "message": "发布成功", "calendarRecorded": True}
            success = await service.publish(
                member, "zhihu", _publish_payload(), request_id="sync-1")
            assert success["async"] is True
            assert success["state"] == "starting"
            status = await service.status(member, "zhihu")
            assert status["state"] == "success"
            assert status["calendarRecorded"] is True
            assert len(relay.submissions) == 1

            relay.auto_result = {
                "state": "unknown", "ok": False,
                "message": "发布未确认，请到平台核对"}
            await service.publish(
                member, "zhihu", _publish_payload(), request_id="sync-2")
            assert (await service.status(member, "zhihu"))["state"] == "unknown"
            assert len(relay.submissions) == 2

            relay.auto_result = {
                "state": "error", "ok": False, "message": "发布失败"}
            await service.publish(
                member, "zhihu", _publish_payload(), request_id="sync-3")
            assert (await service.status(member, "zhihu"))["state"] == "error"
            assert len(relay.submissions) == 3

        asyncio.run(scenario())
    finally:
        store.close()


def test_publish_sms_only_continues_own_current_pending_run(tmp_path):
    store, _auth, _admin, member, other, relay, workspace, app = _fixture(
        tmp_path)
    try:
        async def scenario():
            service = _service(store, relay, workspace)
            started = await service.publish(
                member, DOUYIN, _publish_payload(media=["project/clip.mp4"]), request_id="sms-1")
            run_id = started["runId"]
            # 尚未进入 sms_required：拒绝
            with pytest.raises(DomainError) as early:
                await service.submit_sms(member, DOUYIN, "123456")
            assert early.value.status == 409
            relay.status_event(run_id, "sms_required", "请输入验证码")
            accepted = await service.submit_sms(
                member, DOUYIN, "123456", run_id=run_id)
            assert accepted["ok"] is True
            handoff = [call for call in workspace.calls
                       if call[1] == "POST" and call[2].endswith("/sms")][-1]
            assert handoff[4] == {"code": "123456", "runId": run_id}

            with pytest.raises(DomainError) as foreign:
                await service.submit_sms(
                    other, DOUYIN, "123456", run_id=run_id)
            assert foreign.value.status == 404
            with pytest.raises(DomainError):
                await service.submit_sms(
                    member, DOUYIN, "123456", run_id="unknown-run")

        asyncio.run(scenario())
    finally:
        store.close()


def test_result_reads_latest_page_of_long_task_conversation(tmp_path):
    store, _auth, _admin, member, _other, relay, workspace, _app = _fixture(
        tmp_path)
    try:
        async def scenario():
            service = _service(store, relay, workspace)
            conversation = store.get_or_create_task_conversation(
                member, publish_task_key("zhihu"), "发布：zhihu")
            run = store.reserve_run(
                member, conversation.id, "long-run", "digest-long", 0)
            store.finish_run(member, run.id, "done")
            relay.message_log = [
                {"role": "user", "content": f"old-{index}"}
                for index in range(600)]
            relay.message_log.append({
                "role": "assistant", "runId": run.id,
                "content": json.dumps({
                    "state": "success", "ok": True, "message": "ok",
                }, ensure_ascii=False),
            })
            value = await service._result(member, run)
            assert value["state"] == "success"

        asyncio.run(scenario())
    finally:
        store.close()


def test_manual_export_uses_owned_run_and_returns_unpublished_package(tmp_path):
    store, _auth, _admin, member, _other, relay, workspace, app = _fixture(
        tmp_path, publish_mode="manual")
    try:
        async def scenario():
            service = _service(
                store, relay, workspace, publish_mode="manual")
            path = "_publish/manual/easel-xiaohongshu-0123456789ab.zip"
            relay.auto_state = "done"
            relay.auto_result = {
                "path": path, "filename": path.rsplit("/", 1)[-1],
                "published": False}
            result = await service.manual_export(
                member, PLATFORM,
                {"title": "标题", "body": "正文", "tags": "",
                 "media": [], "reviewed": True},
                request_id="export-1")
            assert result["published"] is False
            assert result["path"] == path
            assert result["runId"] in relay.states
            actor, task_type, payload = relay.submissions[0]
            assert task_type == "publish"
            descriptor = json.loads(payload["message"])
            assert descriptor["action"] == "manual-export"
            assert descriptor["platform"] == PLATFORM
            assert descriptor["reviewed"] is True

        asyncio.run(scenario())
    finally:
        store.close()


def test_concurrent_export_for_same_platform_is_controlled(tmp_path):
    store, _auth, _admin, member, _other, relay, workspace, _app = _fixture(
        tmp_path)
    try:
        async def scenario():
            service = _service(store, relay, workspace)
            path = "_publish/manual/easel-xiaohongshu-0123456789ab.zip"
            payload = {"title": "标题", "body": "正文", "tags": "",
                       "media": [], "reviewed": True}
            first = asyncio.create_task(service.manual_export(
                member, PLATFORM, payload, request_id="export-a"))
            while not relay.submissions:
                await asyncio.sleep(0.005)
            with pytest.raises(DomainError) as busy:
                await service.manual_export(
                    member, PLATFORM, payload, request_id="export-b")
            assert busy.value.status == 409
            assert busy.value.code == "export_busy"
            with Store.open(store.path) as opened:
                conversation = opened.get_task_conversation(
                    member, export_task_key(PLATFORM))
                run = opened.latest_run_for_conversation(
                    member, conversation.id)
            relay.finish(member, run.id, "done", {
                "path": path, "filename": path.rsplit("/", 1)[-1],
                "published": False})
            result = await first
            assert result["path"] == path
            assert len(relay.submissions) == 1

        asyncio.run(scenario())
    finally:
        store.close()


def test_manual_export_rejects_unreviewed_and_bad_fields(tmp_path):
    store, _auth, _admin, member, _other, relay, _workspace, app = _fixture(
        tmp_path, publish_mode="manual")
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            _body, headers = _login(client, _auth, _admin, member)
            base = {"title": "标题", "body": "正文", "tags": "",
                    "media": [], "reviewed": False}
            unreviewed = client.post(
                f"/api/manual-publish/{PLATFORM}/export", json=base,
                headers=headers)
            assert unreviewed.status_code == 400, unreviewed.text
            escaped = client.post(
                f"/api/manual-publish/{PLATFORM}/export",
                json={**base, "reviewed": True, "media": ["../outside.png"]},
                headers=headers)
            assert escaped.status_code == 400, escaped.text
            extra = client.post(
                f"/api/manual-publish/{PLATFORM}/export",
                json={**base, "reviewed": True, "command": "/bin/sh"},
                headers=headers)
            assert extra.status_code == 400, extra.text
            assert relay.submissions == []
    finally:
        store.close()


def test_publish_rejects_escaped_media_command_and_unknown_platform(tmp_path):
    store, _auth, _admin, member, _other, relay, _workspace, app = _fixture(
        tmp_path)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            _body, headers = _login(client, _auth, _admin, member)
            escaped = client.post(
                f"/api/publish/{PLATFORM}",
                json=_with_request(_publish_payload(media=["../outside.png"])),
                headers=headers)
            assert escaped.status_code == 400, escaped.text
            absolute = client.post(
                f"/api/publish/{PLATFORM}",
                json=_with_request(_publish_payload(media=["C:/secret.png"])),
                headers=headers)
            assert absolute.status_code == 400, absolute.text
            extra = client.post(
                f"/api/publish/{PLATFORM}",
                json=_with_request(_publish_payload(script="/bin/sh")),
                headers=headers)
            assert extra.status_code == 400, extra.text
            persona = client.post(
                f"/api/publish/{PLATFORM}",
                json=_with_request(_publish_payload(persona="../outside")),
                headers=headers)
            assert persona.status_code == 400, persona.text
            unknown = client.post(
                "/api/publish/not-a-platform", json=_with_request(_publish_payload(media=["project/clip.mp4"])),
                headers=headers)
            assert unknown.status_code in (400, 404), unknown.text
            no_csrf = client.post(
                f"/api/publish/{PLATFORM}", json=_with_request(_publish_payload()))
            assert no_csrf.status_code == 403, no_csrf.text
            assert relay.submissions == []
    finally:
        store.close()
