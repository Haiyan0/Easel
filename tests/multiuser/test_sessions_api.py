"""Task 9 control 会话目录 HTTP 入口：认证、CSRF、归属与软删除。"""
from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.store import Store

ORIGIN = "https://easel.test"


@contextmanager
def control_api(tmp_path):
    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        other = store.create_user("Other")
        auth = AuthService(store)
        app = create_app(store=store, auth=auth,
                         config=ControlConfig(public_origin=ORIGIN))
        with TestClient(app, base_url=ORIGIN) as client:
            invite = auth.create_invite(admin, member.user_id)
            yield client, store, auth, admin, member, other, invite
    finally:
        store.close()


def login(client: TestClient, auth: AuthService, admin,
          user) -> str:
    invite = auth.create_invite(admin, user.user_id)
    response = client.post(
        "/api/auth/exchange", json={"token": invite},
        headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    return response.json()["csrfToken"]


def test_sessions_require_login_and_csrf(tmp_path):
    with control_api(tmp_path) as (client, store, _auth, _admin, member,
                                   _other, invite):
        response = client.post(
            "/api/auth/exchange", json={"token": invite},
            headers={"Origin": ORIGIN})
        assert response.status_code == 200
        csrf = response.json()["csrfToken"]

        rejected = client.post("/api/sessions", json={"title": "x"},
                               headers={"Origin": ORIGIN})
        assert rejected.status_code == 403
        assert rejected.json()["code"] == "csrf_invalid"
        assert store.list_conversations(member) == []

        no_origin = client.post(
            "/api/sessions", json={"title": "x"},
            headers={"X-CSRF-Token": csrf})
        assert no_origin.status_code == 403
        assert no_origin.json()["code"] == "origin_denied"
        assert store.list_conversations(member) == []

        client.cookies.clear()
        assert client.get("/api/sessions").status_code == 401


def test_create_list_update_archive_delete_directory(tmp_path):
    with control_api(tmp_path) as (client, _store, auth, admin, member,
                                   _other, _invite):
        csrf = login(client, auth, admin, member)
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}

        created = client.post(
            "/api/sessions", json={"title": "项目讨论"}, headers=headers)
        assert created.status_code == 201, created.text
        session = created.json()
        assert session["workspaceId"] == member.workspace_id
        assert session["title"] == "项目讨论"
        assert session["archived"] is False

        listed = client.get("/api/sessions")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["sessions"]] == [
            session["id"]]

        renamed = client.patch(
            f"/api/sessions/{session['id']}",
            json={"title": "新标题"}, headers=headers)
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "新标题"

        archived = client.patch(
            f"/api/sessions/{session['id']}",
            json={"archived": True}, headers=headers)
        assert archived.status_code == 200
        assert archived.json()["archived"] is True
        assert client.get("/api/sessions").json()["sessions"] == []
        assert len(client.get(
            "/api/sessions?include_archived=true").json()["sessions"]) == 1

        deleted = client.delete(
            f"/api/sessions/{session['id']}", headers=headers)
        assert deleted.status_code == 204
        assert client.get("/api/sessions?include_archived=true").json()[
            "sessions"] == []
        missing = client.patch(
            f"/api/sessions/{session['id']}",
            json={"title": "again"}, headers=headers)
        assert missing.status_code == 404


def test_foreign_sessions_are_hidden_and_messages_are_not_faked(tmp_path):
    with control_api(tmp_path) as (client, store, auth, admin, member,
                                   other, _invite):
        csrf = login(client, auth, admin, member)
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
        created = client.post(
            "/api/sessions", json={"title": "私有"}, headers=headers)
        session_id = created.json()["id"]

        foreign = store.create_conversation(other, "别人的")
        for response in (
                client.patch(f"/api/sessions/{foreign.id}",
                             json={"title": "改"}, headers=headers),
                client.delete(f"/api/sessions/{foreign.id}", headers=headers),
                client.get(f"/api/sessions/{foreign.id}/messages")):
            assert response.status_code == 404, response.text
            assert response.json()["code"] == "not_found"

        messages = client.get(f"/api/sessions/{session_id}/messages")
        assert messages.status_code == 503
        assert messages.json()["code"] == "capability_unavailable"
        assert session_id not in messages.text


def test_delete_is_blocked_by_active_run_and_pagination_is_bounded(tmp_path):
    with control_api(tmp_path) as (client, store, auth, admin, member,
                                   _other, _invite):
        csrf = login(client, auth, admin, member)
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
        created = client.post(
            "/api/sessions", json={"title": "任务中"}, headers=headers)
        session_id = created.json()["id"]
        run = store.reserve_run(
            member, session_id, "request-api", "digest-api", 1)

        blocked = client.delete(
            f"/api/sessions/{session_id}", headers=headers)
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "conversation_busy"
        blocked_archive = client.patch(
            f"/api/sessions/{session_id}", json={"archived": True},
            headers=headers)
        assert blocked_archive.status_code == 409
        assert blocked_archive.json()["code"] == "conversation_busy"
        assert store.get_run(member, run.id).id == run.id
        assert store.get_conversation(member, session_id).id == session_id

        for query in ("limit=0", "limit=201", "offset=-1",
                      "include_archived=maybe"):
            response = client.get(f"/api/sessions?{query}")
            assert response.status_code == 400, (query, response.text)
            assert response.json()["code"] == "invalid_argument"

        invalid = client.patch(
            f"/api/sessions/{session_id}", json={}, headers=headers)
        assert invalid.status_code == 400
        assert store.get_conversation(member, session_id).title == "任务中"
