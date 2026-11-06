"""Task 12 管理员 API：成员、邀请、撤销与受管默认配置。

所有请求经真实 create_app、邀请兑换 Cookie、Origin/CSRF 与专用数据库
线程；管理员操作在写事务内按数据库复查 actor 与目标。
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from easel.control.admin import AdminService
from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.config import MASTER_KEY_BYTES, ConfigService
from easel.control.store import Store
from easel.control.types import DomainError

ORIGIN = "https://easel.test"
MASTER_KEY = b"k" * MASTER_KEY_BYTES
ADMIN_SECRET = "admin-secret-sentinel-task12-admin"
IMAGE_SECRET = "image-secret-sentinel-task12-admin"
PERSONAL_SECRET = "personal-secret-sentinel-task12-admin"


def speech_route(**overrides) -> dict:
    route = {
        "provider": "openai-compatible",
        "model": "tts-default",
        "base_url": "https://provider.example/v1",
        "protocol": "openai",
        "secret": ADMIN_SECRET,
        "options": {},
        "allowed_models": ["tts-default", "tts-alt"],
    }
    route.update(overrides)
    return route


def image_route(**overrides) -> dict:
    route = {
        "provider": "openai",
        "model": "image-default",
        "base_url": "https://image.example/v1",
        "protocol": "openai",
        "secret": IMAGE_SECRET,
        "options": {},
    }
    route.update(overrides)
    return route


def chat_route(**overrides) -> dict:
    route = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "protocol": "openai",
        "secret": "chat-secret-sentinel-task12-admin",
        "options": {},
    }
    route.update(overrides)
    return route


@contextmanager
def admin_api(tmp_path, *, with_service: bool = True):
    path = tmp_path / "control.sqlite3"
    store = Store.connect(path)
    try:
        admin = store.create_user("Owner", "admin")
        second_admin = store.create_user("Second", "admin")
        member = store.create_user("Member")
        other = store.create_user("Other")
        auth = AuthService(store, clock=lambda: 1000.0)
        service = ConfigService(store, MASTER_KEY) if with_service else None
        config = ControlConfig(public_origin=ORIGIN)
        first_app = create_app(
            store=store, auth=auth, config=config,
            config_service=service)
        second_app = create_app(
            store=store, auth=auth, config=config,
            config_service=service)
        with TestClient(first_app, base_url=ORIGIN) as admin_client, \
                TestClient(second_app, base_url=ORIGIN) as member_client:
            yield (admin_client, member_client, store, auth, service,
                   admin, second_admin, member, other)
    finally:
        store.close()


def login(client, auth, inviter, user) -> str:
    invite = auth.create_invite(inviter, user.user_id)
    response = client.post("/api/auth/exchange", json={"token": invite},
                           headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    return response.json()["csrfToken"]


def exchange(client, token: str):
    return client.post("/api/auth/exchange", json={"token": token},
                       headers={"Origin": ORIGIN})


def write_headers(csrf: str) -> dict:
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf}


def publish(client, csrf: str, defaults: dict, version: int):
    return client.post(
        "/api/admin/managed-config", json={"defaults": defaults},
        headers={**write_headers(csrf), "If-Match": str(version)})


# --------------------------------------------------------------------------
# 权限边界
# --------------------------------------------------------------------------

def test_member_cannot_reach_any_admin_route(tmp_path):
    with admin_api(tmp_path) as (admin_client, member_client, store, auth,
                                 service, admin, second_admin, member, other):
        csrf = login(member_client, auth, admin, member)
        headers = write_headers(csrf)
        invites_before = store._connection.execute(
            "SELECT COUNT(*) FROM invites").fetchone()[0]
        requests = [
            ("GET", "/api/admin/users", None),
            ("POST", "/api/admin/users", {"displayName": "Nope"}),
            ("PATCH", f"/api/admin/users/{member.user_id}",
             {"disabled": True}),
            ("POST", f"/api/admin/users/{member.user_id}/revoke-sessions",
             {}),
            ("GET", "/api/admin/invites", None),
            ("POST", "/api/admin/invites", {"userId": member.user_id}),
            ("DELETE", "/api/admin/invites/not-real", None),
            ("GET", "/api/admin/managed-config", None),
        ]
        for method, path, body in requests:
            response = member_client.request(
                method, path, headers=headers, json=body)
            assert response.status_code == 403, (method, path,
                                                 response.text)
            assert response.json()["code"] == "forbidden"
        assert store._connection.execute(
            "SELECT COUNT(*) FROM invites").fetchone()[0] == invites_before
        assert store._connection.execute(
            "SELECT COUNT(*) FROM users WHERE display_name = 'Nope'"
        ).fetchone()[0] == 0


# --------------------------------------------------------------------------
# 成员生命周期
# --------------------------------------------------------------------------

def test_admin_user_list_create_pagination_and_public_view(tmp_path):
    with admin_api(tmp_path) as (admin_client, member_client, store, auth,
                                 service, admin, second_admin, member, other):
        csrf = login(admin_client, auth, admin, admin)
        headers = write_headers(csrf)

        listing = admin_client.get("/api/admin/users?limit=50&offset=0")
        assert listing.status_code == 200, listing.text
        assert listing.headers["cache-control"] == "no-store"
        body = listing.json()
        assert set(body) == {"users", "nextOffset"}
        ids = {item["userId"] for item in body["users"]}
        assert ids == {admin.user_id, second_admin.user_id, member.user_id,
                       other.user_id}
        for item in body["users"]:
            assert set(item) == {"userId", "workspaceId", "displayName",
                                 "role", "disabled", "createdAt"}
            assert item["workspaceId"]
            assert item["disabled"] is False
            assert item["role"] in ("admin", "member")
        for forbidden in ("token", "csrf", "invite", "credential", "secret"):
            assert forbidden not in listing.text.lower()

        created = admin_client.post(
            "/api/admin/users", json={"displayName": "  New Member  "},
            headers=headers)
        assert created.status_code == 201, created.text
        user = created.json()
        assert user["displayName"] == "New Member"
        assert user["role"] == "member"
        assert user["disabled"] is False
        assert user["userId"] and user["workspaceId"]
        assert store.get_identity(user["userId"]).workspace_id == \
            user["workspaceId"]

        # 严格只接收 displayName；角色不能由请求体提升
        extra = admin_client.post(
            "/api/admin/users",
            json={"displayName": "Forged", "role": "admin"},
            headers=headers)
        assert extra.status_code == 400
        assert extra.json()["code"] == "invalid_argument"
        for bad in ("", "   ", "x" * 201, "bad\nname"):
            rejected = admin_client.post(
                "/api/admin/users", json={"displayName": bad},
                headers=headers)
            assert rejected.status_code == 400, bad

        # 有界分页、稳定无重复；最后 nextOffset 为 null
        seen: list[str] = []
        offset = 0
        for _ in range(20):
            page = admin_client.get(
                f"/api/admin/users?limit=1&offset={offset}").json()
            assert len(page["users"]) <= 1
            seen.extend(item["userId"] for item in page["users"])
            if page["nextOffset"] is None:
                break
            offset = page["nextOffset"]
        assert len(seen) == len(set(seen))
        assert set(seen) == ids | {user["userId"]}

        for query in ("limit=0", "limit=201", "offset=-1", "limit=x"):
            assert admin_client.get(
                f"/api/admin/users?{query}").status_code == 400, query

        # PATCH 只管理禁用状态；未知目标 404；管理员不可禁用
        role_change = admin_client.patch(
            f"/api/admin/users/{user['userId']}", json={"role": "admin"},
            headers=headers)
        assert role_change.status_code == 400
        mixed = admin_client.patch(
            f"/api/admin/users/{user['userId']}",
            json={"disabled": True, "role": "admin"}, headers=headers)
        assert mixed.status_code == 400
        unknown = admin_client.patch(
            "/api/admin/users/not-a-user", json={"disabled": True},
            headers=headers)
        assert unknown.status_code == 404
        protected = admin_client.patch(
            f"/api/admin/users/{admin.user_id}", json={"disabled": True},
            headers=headers)
        assert protected.status_code == 409
        assert protected.json()["code"] == "admin_protected"

        disabled = admin_client.patch(
            f"/api/admin/users/{user['userId']}", json={"disabled": True},
            headers=headers)
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["disabled"] is True
        repeated = admin_client.patch(
            f"/api/admin/users/{user['userId']}", json={"disabled": True},
            headers=headers)
        assert repeated.status_code == 200
        enabled = admin_client.patch(
            f"/api/admin/users/{user['userId']}", json={"disabled": False},
            headers=headers)
        assert enabled.status_code == 200
        assert enabled.json()["disabled"] is False


def test_disable_revokes_logins_and_invites_and_enable_does_not_revive(
        tmp_path):
    with admin_api(tmp_path) as (admin_client, member_client, store, auth,
                                 service, admin, second_admin, member, other):
        admin_csrf = login(admin_client, auth, admin, admin)
        headers = write_headers(admin_csrf)

        invite1 = auth.create_invite(admin, member.user_id)
        invite2 = auth.create_invite(admin, member.user_id)
        first = exchange(member_client, invite1)
        assert first.status_code == 200, first.text
        old_cookie = first.headers["set-cookie"].split(";", 1)[0]
        assert member_client.get("/api/auth/me").status_code == 200

        disabled = admin_client.patch(
            f"/api/admin/users/{member.user_id}", json={"disabled": True},
            headers=headers)
        assert disabled.status_code == 200, disabled.text

        assert member_client.get("/api/auth/me").status_code == 401
        member_client.cookies.clear()
        assert member_client.get(
            "/api/auth/me", headers={"Cookie": old_cookie}).status_code == 401
        assert exchange(member_client, invite2).status_code == 401

        while_disabled = admin_client.post(
            "/api/admin/invites", json={"userId": member.user_id},
            headers=headers)
        assert while_disabled.status_code == 403
        assert while_disabled.json()["code"] == "user_disabled"

        enabled = admin_client.patch(
            f"/api/admin/users/{member.user_id}", json={"disabled": False},
            headers=headers)
        assert enabled.status_code == 200

        # 重新启用不复活已撤销登录/邀请，必须新邀请
        member_client.cookies.clear()
        assert member_client.get(
            "/api/auth/me", headers={"Cookie": old_cookie}).status_code == 401
        assert exchange(member_client, invite1).status_code == 401
        assert exchange(member_client, invite2).status_code == 401
        fresh = admin_client.post(
            "/api/admin/invites", json={"userId": member.user_id},
            headers=headers)
        assert fresh.status_code == 201, fresh.text
        assert exchange(member_client, fresh.json()["token"]).status_code == 200


def test_revoke_sessions_keeps_workspace_conversations_and_runs(tmp_path):
    with admin_api(tmp_path) as (admin_client, member_client, store, auth,
                                 service, admin, second_admin, member, other):
        admin_csrf = login(admin_client, auth, admin, admin)
        headers = write_headers(admin_csrf)
        invite = auth.create_invite(admin, member.user_id)
        assert exchange(member_client, invite).status_code == 200
        created = member_client.post(
            "/api/sessions", json={"title": "keep-me"},
            headers={"Origin": ORIGIN,
                     "X-CSRF-Token": member_client.get(
                         "/api/auth/me").json()["csrfToken"]})
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]

        revoked = admin_client.post(
            f"/api/admin/users/{member.user_id}/revoke-sessions",
            headers=headers)
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["revoked"] >= 1
        assert member_client.get("/api/auth/me").status_code == 401

        # 只撤销登录，不删除 workspace/会话/历史
        exact = store.get_conversation(member, session_id)
        assert exact.workspace_id == member.workspace_id

        fresh = admin_client.post(
            "/api/admin/invites", json={"userId": member.user_id},
            headers=headers)
        assert exchange(member_client, fresh.json()["token"]).status_code == 200
        sessions = member_client.get("/api/sessions?limit=100").json()
        assert session_id in {item["id"] for item in sessions["sessions"]}

        unknown = admin_client.post(
            "/api/admin/users/not-a-user/revoke-sessions", headers=headers)
        assert unknown.status_code == 404
        protected = admin_client.post(
            f"/api/admin/users/{second_admin.user_id}/revoke-sessions",
            headers=headers)
        assert protected.status_code == 409
        assert protected.json()["code"] == "admin_protected"


# --------------------------------------------------------------------------
# 邀请
# --------------------------------------------------------------------------

def test_invite_issue_list_revoke_and_repeated_login(tmp_path):
    with admin_api(tmp_path) as (admin_client, member_client, store, auth,
                                 service, admin, second_admin, member, other):
        csrf = login(admin_client, auth, admin, admin)
        headers = write_headers(csrf)

        issued = admin_client.post(
            "/api/admin/invites", json={"userId": member.user_id},
            headers=headers)
        assert issued.status_code == 201, issued.text
        body = issued.json()
        assert set(body) == {"id", "token", "expiresAt"}
        assert body["token"]
        assert body["expiresAt"] == 0

        listing = admin_client.get("/api/admin/invites?limit=50&offset=0")
        assert listing.status_code == 200, listing.text
        assert listing.headers["cache-control"] == "no-store"
        listed = listing.json()
        assert set(listed) == {"invites", "nextOffset"}
        item = next(row for row in listed["invites"]
                    if row["id"] == body["id"])
        assert set(item) == {"id", "userId", "createdAt", "expiresAt",
                             "redeemedAt", "revokedAt"}
        assert item["userId"] == member.user_id
        assert item["redeemedAt"] is None and item["revokedAt"] is None
        assert body["token"] not in listing.text
        assert "token" not in listing.text.lower()

        # 永久可重用，已经登录过的码也可以撤销。
        assert exchange(member_client, body["token"]).status_code == 200
        member_client.cookies.clear()
        assert exchange(member_client, body["token"]).status_code == 200
        redeemed = admin_client.delete(
            f"/api/admin/invites/{body['id']}", headers=headers)
        assert redeemed.status_code == 204
        assert exchange(member_client, body["token"]).status_code == 401

        second = admin_client.post(
            "/api/admin/invites", json={"userId": member.user_id},
            headers=headers)
        assert second.status_code == 201
        revoked = admin_client.delete(
            f"/api/admin/invites/{second.json()['id']}", headers=headers)
        assert revoked.status_code == 204
        assert exchange(member_client, second.json()["token"]).status_code == 401
        again = admin_client.delete(
            f"/api/admin/invites/{second.json()['id']}", headers=headers)
        assert again.status_code == 204

        unknown = admin_client.delete(
            "/api/admin/invites/not-real", headers=headers)
        assert unknown.status_code == 404
        unknown_user = admin_client.post(
            "/api/admin/invites", json={"userId": "not-a-user"},
            headers=headers)
        assert unknown_user.status_code == 404
        extra = admin_client.post(
            "/api/admin/invites",
            json={"userId": member.user_id, "role": "admin"},
            headers=headers)
        assert extra.status_code == 400

        # 分页与稳定排序
        before = admin_client.get(
            "/api/admin/invites?limit=200").json()["invites"]
        for _ in range(3):
            admin_client.post("/api/admin/invites",
                              json={"userId": member.user_id},
                              headers=headers)
        seen: list[str] = []
        offset = 0
        for _ in range(20):
            page = admin_client.get(
                f"/api/admin/invites?limit=1&offset={offset}").json()
            seen.extend(row["id"] for row in page["invites"])
            if page["nextOffset"] is None:
                break
            offset = page["nextOffset"]
        assert len(seen) == len(set(seen)) == len(before) + 3
        for query in ("limit=0", "limit=201", "offset=-1"):
            assert admin_client.get(
                f"/api/admin/invites?{query}").status_code == 400

        # member 不能签发/查看
        member_csrf = login(member_client, auth, admin, member)
        denied = member_client.get("/api/admin/invites")
        assert denied.status_code == 403
        assert member_csrf


# --------------------------------------------------------------------------
# 事务内角色复查
# --------------------------------------------------------------------------

def test_admin_actor_and_target_are_rechecked_inside_transactions(tmp_path):
    path = tmp_path / "control.sqlite3"
    store = Store.connect(path)
    try:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        auth = AuthService(store, clock=lambda: 1000.0)
        service = AdminService(store, auth)
        other = Store.connect(path)
        try:
            other._connection.execute(
                "UPDATE users SET role = 'member' WHERE id = ?",
                (admin.user_id,))
        finally:
            other.close()

        # 路由层若已按旧身份放行，服务层仍必须在事务内拒绝
        with pytest.raises(DomainError) as denied:
            service.list_users(admin, limit=50, offset=0)
        assert denied.value.status == 403
        with pytest.raises(DomainError) as denied_create:
            service.create_member(admin, "Forged")
        assert denied_create.value.status == 403

        other = Store.connect(path)
        try:
            other._connection.execute(
                "UPDATE users SET role = 'admin', disabled_at = ?"
                " WHERE id = ?", (123, admin.user_id))
        finally:
            other.close()
        with pytest.raises(DomainError) as disabled:
            service.create_member(admin, "Forged")
        assert disabled.value.status == 403
        assert disabled.value.code == "user_disabled"

        other = Store.connect(path)
        try:
            other._connection.execute(
                "UPDATE users SET disabled_at = NULL WHERE id = ?",
                (admin.user_id,))
        finally:
            other.close()

        # 目标管理同样在事务内复查
        with pytest.raises(DomainError) as protected:
            service.set_member_disabled(admin, admin.user_id, True)
        assert protected.value.status == 409
        assert protected.value.code == "admin_protected"
        with pytest.raises(DomainError) as missing:
            service.set_member_disabled(admin, "not-a-user", True)
        assert missing.value.status == 404
        with pytest.raises(DomainError) as missing_invite:
            service.revoke_invite(admin, "not-an-invite")
        assert missing_invite.value.status == 404
        with pytest.raises(DomainError) as missing_user:
            service.create_invite(admin, "not-a-user")
        assert missing_user.value.status == 404
        assert store._connection.execute(
            "SELECT disabled_at FROM users WHERE id = ?",
            (member.user_id,)).fetchone()[0] is None
    finally:
        store.close()


# --------------------------------------------------------------------------
# 管理员默认配置：空凭证/多通道/并发
# --------------------------------------------------------------------------

def test_admin_default_empty_secret_reuses_same_scope_and_keeps_channels(
        tmp_path):
    with admin_api(tmp_path) as (admin_client, member_client, store, auth,
                                 service, admin, second_admin, member, other):
        csrf = login(admin_client, auth, admin, admin)
        first = publish(admin_client, csrf,
                        {"speech": speech_route(),
                         "image": image_route()}, 0)
        assert first.status_code == 200, first.text
        assert first.json() == {"version": 1}

        # 只编辑 speech，留空 Key；image 与 speech 原凭证都必须保留
        second = publish(admin_client, csrf,
                         {"speech": speech_route(
                             model="tts-v2", secret="",
                             allowed_models=["tts-v2"])},
                         1)
        assert second.status_code == 200, second.text
        assert second.json() == {"version": 2}
        view = admin_client.get("/api/admin/managed-config").json()
        assert view["version"] == 2
        assert view["defaults"]["speech"]["model"] == "tts-v2"
        assert view["defaults"]["image"]["model"] == "image-default"
        assert view["defaults"]["speech"]["credential_configured"] is True
        assert view["defaults"]["image"]["credential_configured"] is True
        assert service.credentials_for_ref("managed:2:speech") == {
            "VOICE_API_KEY": ADMIN_SECRET}
        assert service.credentials_for_ref("managed:2:image") == {
            "IMG_API_KEY": IMAGE_SECRET}
        # 旧不可变版本的凭证仍可用
        assert service.credentials_for_ref("managed:1:speech") == {
            "VOICE_API_KEY": ADMIN_SECRET}

        # 换地址但留空：拒绝，版本与旧配置不变
        changed_scope = publish(
            admin_client, csrf,
            {"speech": speech_route(
                model="tts-v3", base_url="https://other.example/v1",
                secret="", allowed_models=["tts-v3"])}, 2)
        assert changed_scope.status_code == 400
        assert changed_scope.json()["code"] == "invalid_secret_bundle"
        assert admin_client.get(
            "/api/admin/managed-config").json()["version"] == 2

        # 新通道不能留空
        new_channel = publish(admin_client, csrf,
                              {"chat": chat_route(secret="")}, 2)
        assert new_channel.status_code == 400
        assert new_channel.json()["code"] == "invalid_secret_bundle"
        assert admin_client.get(
            "/api/admin/managed-config").json()["version"] == 2

        third = publish(admin_client, csrf, {"chat": chat_route()}, 2)
        assert third.status_code == 200, third.text
        assert third.json() == {"version": 3}
        view = admin_client.get("/api/admin/managed-config").json()
        assert set(view["defaults"]) == {"speech", "image", "chat"}
        assert view["defaults"]["speech"]["model"] == "tts-v2"

        # 只改模型、不提交 secret 字段同样复用
        no_secret = speech_route(model="tts-v4",
                                 allowed_models=["tts-v4"])
        no_secret.pop("secret")
        fourth = publish(admin_client, csrf, {"speech": no_secret}, 3)
        assert fourth.status_code == 200, fourth.text
        assert service.credentials_for_ref("managed:4:speech") == {
            "VOICE_API_KEY": ADMIN_SECRET}
        assert admin_client.get(
            "/api/admin/managed-config").json()["defaults"]["speech"][
                "model"] == "tts-v4"

        # 管理员个人覆盖不改变全体默认视图
        service.save_override(
            admin, "speech",
            {"provider": "openai-compatible", "model": "admin-personal",
             "base_url": "https://admin-personal.example/v1",
             "protocol": "openai", "secret": PERSONAL_SECRET,
             "options": {}})
        assert admin_client.get(
            "/api/admin/managed-config").json()["defaults"]["speech"][
                "model"] == "tts-v4"

        # 未知/非法 options/非有限数值不推进版本
        for bad in (
            speech_route(options={"headers": {"x": "evil"}}),
            speech_route(options={"speed": float("inf")}),
        ):
            response = admin_client.post(
                "/api/admin/managed-config",
                content=json.dumps(
                    {"defaults": {"speech": bad}}).replace(
                        "Infinity", "1e309").encode("utf-8"),
                headers={**write_headers(csrf), "If-Match": "4",
                         "Content-Type": "application/json"})
            assert response.status_code == 400, response.text
        assert admin_client.get(
            "/api/admin/managed-config").json()["version"] == 4


def test_two_admins_conflict_and_do_not_overwrite_each_other(tmp_path):
    path = tmp_path / "control.sqlite3"
    store = Store.connect(path)
    try:
        admin = store.create_user("Owner", "admin")
        second_admin = store.create_user("Second", "admin")
        member = store.create_user("Member")
        first = ConfigService(store, MASTER_KEY)
        other_store = Store.connect(path)
        second = ConfigService(other_store, MASTER_KEY)
        try:
            assert first.publish(
                admin, {"defaults": {"speech": speech_route(),
                                     "image": image_route()}}, 0) == 1
            # 两个管理员都基于版本 1 编辑不同通道
            assert first.publish(
                admin, {"defaults": {
                    "speech": speech_route(model="tts-admin-one",
                                           secret="",
                                           allowed_models=["tts-admin-one"])}},
                1) == 2
            with pytest.raises(DomainError) as conflict:
                second.publish(
                    second_admin, {"defaults": {
                        "image": image_route(model="image-admin-two",
                                             secret="")}}, 1)
            assert conflict.value.status == 409
            assert conflict.value.code == "version_conflict"
            assert store.latest_managed_revision()["version"] == 2
            assert store._connection.execute(
                "SELECT COUNT(*) FROM managed_revisions").fetchone()[0] == 2

            # 后提交者重新读取版本后成功，且不丢失先提交者的通道
            assert second.publish(
                second_admin, {"defaults": {
                    "image": image_route(model="image-admin-two",
                                         secret="")}}, 2) == 3
            latest = first.public_managed_settings(admin)
            assert latest["defaults"]["speech"]["model"] == "tts-admin-one"
            assert latest["defaults"]["image"]["model"] == "image-admin-two"
            assert first.credentials_for_ref("managed:3:speech") == {
                "VOICE_API_KEY": ADMIN_SECRET}
            assert first.credentials_for_ref("managed:3:image") == {
                "IMG_API_KEY": IMAGE_SECRET}
        finally:
            other_store.close()
    finally:
        store.close()


def test_public_responses_and_logs_never_contain_secret_sentinels(
        tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    with admin_api(tmp_path) as (admin_client, member_client, store, auth,
                                 service, admin, second_admin, member, other):
        csrf = login(admin_client, auth, admin, admin)
        assert publish(admin_client, csrf,
                       {"speech": speech_route()}, 0).status_code == 200
        member_csrf = login(member_client, auth, admin, member)
        saved = member_client.post(
            "/api/settings/models/save",
            json={"channel": "speech",
                  "route": {"provider": "openai-compatible",
                            "model": "personal-model",
                            "base_url": "https://personal.example/v1",
                            "protocol": "openai",
                            "secret": PERSONAL_SECRET,
                            "options": {}}},
            headers=write_headers(member_csrf))
        assert saved.status_code == 200, saved.text

        admin_view = admin_client.get("/api/admin/managed-config")
        member_view = member_client.get("/api/settings/models")
        for response in (saved, admin_view, member_view):
            assert ADMIN_SECRET not in response.text
            assert PERSONAL_SECRET not in response.text
            assert "credential_ref" not in response.text
        rows = "".join(row[0] for row in store._connection.execute(
            "SELECT public_json FROM managed_revisions"))
        assert ADMIN_SECRET not in rows
        assert PERSONAL_SECRET not in rows
        assert ADMIN_SECRET not in caplog.text
        assert PERSONAL_SECRET not in caplog.text


def test_admin_users_and_invites_reach_second_page_over_http(tmp_path):
    with admin_api(tmp_path) as (admin_client, member_client, store, auth,
                                 service, admin, second_admin, member, other):
        csrf = login(admin_client, auth, admin, admin)
        headers = write_headers(csrf)
        members = []
        for index in range(55):
            response = admin_client.post(
                "/api/admin/users",
                json={"displayName": f"批量成员 {index:02d}"},
                headers=headers)
            assert response.status_code == 201, response.text
            members.append(response.json())
        invites = []
        for _ in range(55):
            response = admin_client.post(
                "/api/admin/invites",
                json={"userId": members[0]["userId"]},
                headers=headers)
            assert response.status_code == 201, response.text
            invites.append(response.json())

        first = admin_client.get(
            "/api/admin/users?limit=50&offset=0").json()
        assert len(first["users"]) == 50
        assert first["nextOffset"] == 50
        assert members[-1]["userId"] not in {
            item["userId"] for item in first["users"]}
        second = admin_client.get(
            "/api/admin/users?limit=50&offset=50").json()
        assert members[-1]["userId"] in {
            item["userId"] for item in second["users"]}
        assert second["nextOffset"] is None

        first_invites = admin_client.get(
            "/api/admin/invites?limit=50&offset=0").json()
        assert len(first_invites["invites"]) == 50
        assert first_invites["nextOffset"] == 50
        assert invites[0]["id"] not in {
            item["id"] for item in first_invites["invites"]}
        second_invites = admin_client.get(
            "/api/admin/invites?limit=50&offset=50").json()
        assert invites[0]["id"] in {
            item["id"] for item in second_invites["invites"]}
        assert second_invites["nextOffset"] is None
        assert invites[0]["token"] not in admin_client.get(
            "/api/admin/invites?limit=200").text

        # 后页成员仍可管理、可签发邀请；后页旧邀请可撤销
        assert admin_client.patch(
            f"/api/admin/users/{members[-1]['userId']}",
            json={"disabled": True}, headers=headers).status_code == 200
        assert admin_client.patch(
            f"/api/admin/users/{members[-1]['userId']}",
            json={"disabled": False}, headers=headers).status_code == 200
        assert admin_client.post(
            f"/api/admin/users/{members[-1]['userId']}/revoke-sessions",
            headers=headers).status_code == 200
        issued = admin_client.post(
            "/api/admin/invites", json={"userId": members[-1]["userId"]},
            headers=headers)
        assert issued.status_code == 201
        assert admin_client.delete(
            f"/api/admin/invites/{invites[0]['id']}",
            headers=headers).status_code == 204


def test_admin_routes_503_without_config_service_but_manage_users(tmp_path):
    with admin_api(tmp_path, with_service=False) as (
            admin_client, member_client, store, auth, service, admin,
            second_admin, member, other):
        csrf = login(admin_client, auth, admin, admin)
        headers = write_headers(csrf)
        assert admin_client.get(
            "/api/admin/managed-config").status_code == 503
        assert publish(admin_client, csrf,
                       {"speech": speech_route()}, 0).status_code == 503
        # 用户/邀请管理不依赖配置服务
        assert admin_client.get("/api/admin/users").status_code == 200
        assert admin_client.get("/api/admin/invites").status_code == 200
