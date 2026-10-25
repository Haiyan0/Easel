"""Task 12 设置 API：受管摘要、管理员公共视图与个人凭证保留。

测试使用真实 create_app / 真实 exchange Cookie / Origin/CSRF；
不替换角色守卫，也不把内部 ConfigService 结构直接暴露给浏览器。
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.config import MASTER_KEY_BYTES, ConfigService
from easel.control.store import Store

ORIGIN = "https://easel.test"
MASTER_KEY = b"k" * MASTER_KEY_BYTES
ADMIN_SECRET = "admin-secret-sentinel-task12-settings"
PERSONAL_SECRET = "personal-secret-sentinel-task12-settings"

HARNESS_SUMMARY = {
    "runtime": "acp",
    "revision": 7,
    "model": "managed-chat-model",
    "sandbox": "workspace-write",
    "timeout": 60,
    "codexAutoApprove": False,
}


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


def personal_speech(**overrides) -> dict:
    route = {
        "provider": "openai-compatible",
        "model": "tts-personal",
        "base_url": "https://personal.example/v1",
        "protocol": "openai",
        "secret": PERSONAL_SECRET,
        "options": {},
    }
    route.update(overrides)
    return route


def personal_minimax(**overrides) -> dict:
    route = {
        "provider": "minimax",
        "model": "speech-01",
        "base_url": "https://minimax.example/v1",
        "protocol": "minimax",
        "secret": {"MINIMAX_API_KEY": "minimax-key-sentinel-personal",
                   "MINIMAX_GROUP_ID": "minimax-group-sentinel-personal"},
        "options": {},
    }
    route.update(overrides)
    return route


@contextmanager
def settings_api(tmp_path, *, with_service: bool = True,
                 harness_summary: dict | None = HARNESS_SUMMARY):
    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        auth = AuthService(store, clock=lambda: 1000.0)
        service = ConfigService(store, MASTER_KEY) if with_service else None
        app = create_app(
            store=store, auth=auth,
            config=ControlConfig(public_origin=ORIGIN),
            config_service=service,
            harness_summary=harness_summary)
        with TestClient(app, base_url=ORIGIN) as client:
            yield client, store, auth, service, admin, member
    finally:
        store.close()


def login(client, auth, inviter, user) -> str:
    invite = auth.create_invite(inviter, user.user_id)
    response = client.post("/api/auth/exchange", json={"token": invite},
                           headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    return response.json()["csrfToken"]


def write_headers(csrf: str) -> dict:
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf}


# --------------------------------------------------------------------------
# 受管底座摘要
# --------------------------------------------------------------------------

def test_harness_summary_is_readable_by_member_and_sanitized(tmp_path):
    with settings_api(tmp_path) as (client, store, auth, service, admin,
                                    member):
        login(client, auth, admin, member)
        response = client.get("/api/settings/harness")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["locked"] is True
        assert body["configured"] is True
        assert body["runtime"] == "acp"
        assert body["revision"] == 7
        assert body["model"] == "managed-chat-model"
        assert body["sandbox"] == "workspace-write"
        assert body["timeout"] == 60
        assert body["codexAutoApprove"] is False
        assert body["unavailableReason"] is None
        for forbidden in ("command", "argv", "env", "broker", "url",
                          "credential", "secret", "profile"):
            assert forbidden not in response.text.lower()
        assert "managed-chat-model" in response.text

        # 底座写入仍然只有管理员且当前未装配
        member_csrf = client.get("/api/auth/me").json()["csrfToken"]
        denied = client.post("/api/settings/harness", json={},
                             headers=write_headers(member_csrf))
        assert denied.status_code == 403

        admin_csrf = login(client, auth, admin, admin)
        unavailable = client.post("/api/settings/harness", json={},
                                  headers=write_headers(admin_csrf))
        assert unavailable.status_code == 503
        assert unavailable.json()["code"] == "capability_unavailable"


def test_missing_harness_assembly_is_explicitly_unconfigured(tmp_path):
    with settings_api(tmp_path, harness_summary=None) as (
            client, store, auth, service, admin, member):
        login(client, auth, admin, member)
        response = client.get("/api/settings/harness")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["locked"] is True
        assert body["configured"] is False
        assert body["runtime"] is None
        assert body["revision"] is None
        assert isinstance(body["unavailableReason"], str)
        assert body["unavailableReason"]


# --------------------------------------------------------------------------
# 管理员公共视图
# --------------------------------------------------------------------------

def test_admin_managed_config_view_is_admin_only_and_never_mixes_overrides(
        tmp_path):
    with settings_api(tmp_path) as (client, store, auth, service, admin,
                                    member):
        member_csrf = login(client, auth, admin, member)
        forbidden = client.get("/api/admin/managed-config")
        assert forbidden.status_code == 403
        assert member_csrf

        admin_csrf = login(client, auth, admin, admin)
        empty = client.get("/api/admin/managed-config")
        assert empty.status_code == 200, empty.text
        assert empty.headers["cache-control"] == "no-store"
        assert empty.json() == {"version": 0, "defaults": {}}

        published = client.post(
            "/api/admin/managed-config",
            json={"defaults": {"speech": speech_route()}},
            headers={**write_headers(admin_csrf), "If-Match": "0"})
        assert published.status_code == 200, published.text

        view = client.get("/api/admin/managed-config")
        assert view.status_code == 200
        body = view.json()
        assert body["version"] == 1
        speech = body["defaults"]["speech"]
        assert speech["provider"] == "openai-compatible"
        assert speech["model"] == "tts-default"
        assert speech["base_url"] == "https://provider.example/v1"
        assert speech["protocol"] == "openai"
        assert speech["allowed_models"] == ["tts-default", "tts-alt"]
        assert speech["credential_configured"] is True
        assert ADMIN_SECRET not in view.text
        assert "credential_ref" not in view.text
        assert "encrypted" not in view.text

        # 管理员自己的个人覆盖不得混入“全体默认”公共视图
        service.save_override(admin, "speech", personal_speech(
            base_url="https://admin-personal.example/v1"))
        after = client.get("/api/admin/managed-config").json()
        assert after["defaults"]["speech"]["base_url"] == \
            "https://provider.example/v1"
        assert after["defaults"]["speech"]["model"] == "tts-default"


# --------------------------------------------------------------------------
# 个人覆盖与凭证保留
# --------------------------------------------------------------------------

def test_personal_empty_secret_reuses_only_the_same_personal_scope(tmp_path):
    with settings_api(tmp_path) as (client, store, auth, service, admin,
                                    member):
        admin_csrf = login(client, auth, admin, admin)
        assert client.post(
            "/api/admin/managed-config",
            json={"defaults": {"speech": speech_route()}},
            headers={**write_headers(admin_csrf), "If-Match": "0"}
        ).status_code == 200

        csrf = login(client, auth, admin, member)
        saved = client.post(
            "/api/settings/models/save",
            json={"channel": "speech",
                  "route": personal_speech(options={"speed": 1.0})},
            headers=write_headers(csrf))
        assert saved.status_code == 200, saved.text
        assert PERSONAL_SECRET not in saved.text

        before = store.get_workspace_override(member.workspace_id, "speech")
        assert before is not None
        blob = before["encrypted_secrets"]

        # 同一作用域、留空 Key：复用个人密文，只改模型/参数
        reused = client.post(
            "/api/settings/models/save",
            json={"channel": "speech",
                  "route": personal_speech(model="tts-personal-v2",
                                           secret="", options={"speed": 1.2})},
            headers=write_headers(csrf))
        assert reused.status_code == 200, reused.text
        after = store.get_workspace_override(member.workspace_id, "speech")
        assert after["encrypted_secrets"] == blob

        settings = client.get("/api/settings/models").json()
        speech = settings["channels"]["speech"]
        assert speech["source"] == "user"
        assert speech["mode"] == "complete"
        assert speech["model"] == "tts-personal-v2"
        assert PERSONAL_SECRET not in client.get(
            "/api/settings/models").text

        # 换地址但留空：必须重新输入，且失败不改变原配置/密文
        changed = client.post(
            "/api/settings/models/save",
            json={"channel": "speech",
                  "route": personal_speech(base_url="https://other.example/v1",
                                           secret="")},
            headers=write_headers(csrf))
        assert changed.status_code == 400, changed.text
        assert changed.json()["code"] == "personal_credentials_required"
        unchanged = store.get_workspace_override(member.workspace_id, "speech")
        assert unchanged["encrypted_secrets"] == blob

        # 留空永远不能复制管理员 Key：无个人覆盖时直接拒绝
        client.delete("/api/settings/models/overrides/speech",
                      headers=write_headers(csrf))
        no_personal = client.post(
            "/api/settings/models/save",
            json={"channel": "speech",
                  "route": personal_speech(secret="")},
            headers=write_headers(csrf))
        assert no_personal.status_code == 400
        assert no_personal.json()["code"] == "personal_credentials_required"


def test_personal_dual_key_bundle_reused_only_in_same_scope(tmp_path):
    with settings_api(tmp_path) as (client, store, auth, service, admin,
                                    member):
        csrf = login(client, auth, admin, member)
        saved = client.post(
            "/api/settings/models/save",
            json={"channel": "speech", "route": personal_minimax()},
            headers=write_headers(csrf))
        assert saved.status_code == 200, saved.text
        before = store.get_workspace_override(member.workspace_id, "speech")
        blob = before["encrypted_secrets"]

        reused = client.post(
            "/api/settings/models/save",
            json={"channel": "speech",
                  "route": personal_minimax(model="speech-02", secret="")},
            headers=write_headers(csrf))
        assert reused.status_code == 200, reused.text
        after = store.get_workspace_override(member.workspace_id, "speech")
        assert after["encrypted_secrets"] == blob
        assert "minimax-key-sentinel-personal" not in reused.text
        assert "minimax-group-sentinel-personal" not in reused.text
        assert "minimax-key-sentinel-personal" not in after["public_json"]

        changed = client.post(
            "/api/settings/models/save",
            json={"channel": "speech",
                  "route": personal_minimax(
                      base_url="https://minimax-2.example/v1", secret="")},
            headers=write_headers(csrf))
        assert changed.status_code == 400
        assert changed.json()["code"] == "personal_credentials_required"
        assert store.get_workspace_override(
            member.workspace_id, "speech")["encrypted_secrets"] == blob


def test_personal_managed_model_and_restore_inheritance(tmp_path):
    with settings_api(tmp_path) as (client, store, auth, service, admin,
                                    member):
        admin_csrf = login(client, auth, admin, admin)
        assert client.post(
            "/api/admin/managed-config",
            json={"defaults": {"speech": speech_route()}},
            headers={**write_headers(admin_csrf), "If-Match": "0"}
        ).status_code == 200

        csrf = login(client, auth, admin, member)
        saved = client.post(
            "/api/settings/models/save",
            json={"channel": "speech",
                  "route": {"route_kind": "managed-model",
                            "model": "tts-alt",
                            "options": {"speed": 1.1}}},
            headers=write_headers(csrf))
        assert saved.status_code == 200, saved.text

        settings = client.get("/api/settings/models").json()
        speech = settings["channels"]["speech"]
        assert speech["source"] == "user"
        assert speech["mode"] == "managed-model"
        assert speech["model"] == "tts-alt"
        assert speech["allowed_models"] == ["tts-default", "tts-alt"]
        assert ADMIN_SECRET not in client.get(
            "/api/settings/models").text

        cleared = client.delete("/api/settings/models/overrides/speech",
                                headers=write_headers(csrf))
        assert cleared.status_code == 204
        restored = client.get("/api/settings/models").json()
        assert restored["channels"]["speech"]["source"] == "admin"
        assert restored["channels"]["speech"]["mode"] == "inherited"
        assert restored["channels"]["speech"]["model"] == "tts-default"

        chat = client.post(
            "/api/settings/models/save",
            json={"channel": "chat", "route": personal_speech()},
            headers=write_headers(csrf))
        assert chat.status_code == 403
        assert chat.json()["code"] == "override_not_allowed"


def test_settings_routes_stay_503_without_config_service(tmp_path):
    with settings_api(tmp_path, with_service=False) as (
            client, store, auth, service, admin, member):
        csrf = login(client, auth, admin, admin)
        forbidden_member = client.get("/api/settings/models")
        assert forbidden_member.status_code == 503
        assert client.get("/api/admin/managed-config").status_code == 503
        assert client.post(
            "/api/admin/managed-config", json={"defaults": {}},
            headers={**write_headers(csrf), "If-Match": "0"}
        ).status_code == 503
        # 脱敏底座摘要不依赖配置服务，缺装配时明确未配置
        assert client.get("/api/settings/harness").status_code == 200


def test_harness_summary_input_rejects_private_fields(tmp_path):
    store = Store.connect(tmp_path / "control.sqlite3")
    auth_store = Store.connect(tmp_path / "other.sqlite3")
    try:
        with pytest.raises(ValueError):
            create_app(
                store=store,
                auth=AuthService(auth_store),
                config=ControlConfig(public_origin=ORIGIN),
                harness_summary={"runtime": "acp", "revision": 1,
                                 "command": ["/bin/evil"]})
    finally:
        store.close()
        auth_store.close()
