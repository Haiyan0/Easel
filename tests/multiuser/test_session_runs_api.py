"""Task 11：本人会话 Run 元数据窄路由与公开启动模式。"""
from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.store import Store

ORIGIN = "https://easel.test"
REVISION = 7


@contextmanager
def control_api(tmp_path, **kwargs):
    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        other = store.create_user("Other")
        auth = AuthService(store)
        app = create_app(store=store, auth=auth,
                         config=ControlConfig(public_origin=ORIGIN), **kwargs)
        with TestClient(app, base_url=ORIGIN) as client:
            yield client, store, auth, admin, member, other
    finally:
        store.close()


def login(client: TestClient, auth: AuthService, admin, user) -> str:
    invite = auth.create_invite(admin, user.user_id)
    response = client.post("/api/auth/exchange", json={"token": invite},
                           headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    return response.json()["csrfToken"]


def test_runtime_mode_is_public_and_explicit(tmp_path):
    with control_api(tmp_path) as (client, *_rest):
        response = client.get("/api/runtime")
        assert response.status_code == 200
        assert response.json() == {"mode": "control"}
        assert response.headers["cache-control"] == "no-store"
        # 静态入口未装配时不得回落 local；未登录也拿不到私有目录
        assert client.get("/api/sessions").status_code == 401


def test_session_runs_are_owned_paginated_and_secret_free(tmp_path):
    with control_api(tmp_path) as (client, store, auth, admin, member, other):
        csrf = login(client, auth, admin, member)
        conversation = store.create_conversation(member, "本人")
        foreign = store.create_conversation(other, "别人的")
        runs = []
        for index in range(3):
            run = store.reserve_run(
                member, conversation.id, f"req-{index}",
                f"digest-{index}", REVISION)
            store.finish_run(member, run.id, "done")
            runs.append(run)
        store.reserve_run(other, foreign.id, "req-foreign", "digest", REVISION)

        first = client.get(
            f"/api/sessions/{conversation.id}/runs?limit=2&offset=0")
        assert first.status_code == 200, first.text
        body = first.json()
        assert [item["runId"] for item in body["runs"]] == [
            runs[0].id, runs[1].id]
        assert body["runs"][0]["conversationId"] == conversation.id
        assert body["runs"][0]["state"] == "done"
        assert body["nextOffset"] == 2
        text = first.text
        for secret in ("digest-0", "req-0", "signature", "snapshot",
                       "service-token"):
            assert secret not in text

        second = client.get(
            f"/api/sessions/{conversation.id}/runs?limit=2&offset=2")
        assert second.status_code == 200
        assert [item["runId"] for item in second.json()["runs"]] == [
            runs[2].id]
        assert second.json()["nextOffset"] is None

        # 外空间/未知会话先 404，不泄露存在性
        assert client.get(
            f"/api/sessions/{foreign.id}/runs").status_code == 404
        assert client.get(
            "/api/sessions/does-not-exist/runs").status_code == 404
        assert client.get(
            f"/api/sessions/{conversation.id}/runs?limit=0").status_code == 400
        assert client.get(
            f"/api/sessions/{conversation.id}/runs?limit=201").status_code == 400
        assert client.get(
            f"/api/sessions/{conversation.id}/runs?offset=-1").status_code == 400


class FakeRelay:
    async def messages(self, actor, session_id, *, limit, offset):
        return {"sessionId": session_id,
                "messages": [{"role": "user", "content": "归档消息"}],
                "total": 2500}


def test_archived_conversation_allows_reads_but_blocks_new_runs(tmp_path):
    with control_api(tmp_path, relay_factory=lambda *args: FakeRelay()) as (
            client, store, auth, admin, member, other):
        csrf = login(client, auth, admin, member)
        conversation = store.create_conversation(member, "归档")
        run = store.reserve_run(
            member, conversation.id, "archived-run", "digest", REVISION)
        store.finish_run(member, run.id, "done")
        store.update_conversation(member, conversation.id, archived=True)

        listed = client.get(f"/api/sessions/{conversation.id}/runs")
        assert listed.status_code == 200, listed.text
        assert [item["runId"] for item in listed.json()["runs"]] == [run.id]
        active = client.get(
            f"/api/sessions/{conversation.id}/runs?active=true")
        assert active.status_code == 200
        assert active.json()["runs"] == []

        messages = client.get(
            f"/api/sessions/{conversation.id}/messages")
        assert messages.status_code == 200, messages.text
        assert messages.json()["total"] == 2500
        assert messages.json()["messages"][0]["content"] == "归档消息"

        # 归档仍禁止新 Run；不通过只读接口绕过
        import pytest
        from easel.control.types import DomainError
        with pytest.raises(DomainError) as archived:
            store.reserve_run(
                member, conversation.id, "new-run", "digest-2", REVISION)
        assert archived.value.code == "conversation_archived"
        assert archived.value.status == 409

        store.delete_conversation(member, conversation.id)
        assert client.get(
            f"/api/sessions/{conversation.id}/runs").status_code == 404
        assert client.get(
            f"/api/sessions/{conversation.id}/messages").status_code == 404


def test_active_run_filter_finds_run_beyond_first_pages(tmp_path):
    with control_api(tmp_path) as (client, store, auth, admin, member, other):
        conversation = store.create_conversation(member, "长历史")
        stamp = 900
        rows = []
        for index in range(2000):
            rows.append((
                member.workspace_id, f"old-{index:04d}", conversation.id,
                f"req-{index}", f"digest-{index}", "done", REVISION,
                stamp + index, stamp + index, stamp + index, 1))
        with store._transaction():
            store._connection.executemany(
                "INSERT INTO runs (workspace_id, id, conversation_id,"
                " client_request_id, input_digest, status, config_version,"
                " created_at, finished_at, capacity_released_at, release_count)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        active = store.reserve_run(
            member, conversation.id, "active-last", "digest-active", REVISION)
        login(client, auth, admin, member)

        active_page = client.get(
            f"/api/sessions/{conversation.id}/runs"
            "?active=true&limit=200&offset=0")
        assert active_page.status_code == 200, active_page.text
        assert [item["runId"] for item in active_page.json()["runs"]] == [
            active.id]
        first = client.get(
            f"/api/sessions/{conversation.id}/runs?limit=200&offset=0")
        assert first.status_code == 200
        assert active.id not in [item["runId"] for item in first.json()["runs"]]
