"""Task 4 control 配置 API：If-Match 发布、脱敏读取、个人覆盖。"""
from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.config import MASTER_KEY_BYTES, ConfigService
from easel.control.store import Store

ORIGIN = "https://easel.test"
MASTER_KEY = b"k" * MASTER_KEY_BYTES
ADMIN_SECRET = "admin-secret-sentinel-api"
PERSONAL_SECRET = "personal-secret-sentinel-api"


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


@contextmanager
def config_api(tmp_path, *, with_service: bool = True):
    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        auth = AuthService(store, clock=lambda: 1000.0)
        service = ConfigService(store, MASTER_KEY) if with_service else None
        app = create_app(
            store=store, auth=auth,
            config=ControlConfig(public_origin=ORIGIN),
            config_service=service)
        with TestClient(app, base_url=ORIGIN) as client:
            yield client, store, auth, service, admin, member
    finally:
        store.close()


def login(client, auth, admin, user) -> str:
    invite = auth.create_invite(admin, user.user_id)
    response = client.post("/api/auth/exchange", json={"token": invite},
                           headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    return response.json()["csrfToken"]


def write_headers(csrf: str, origin: str = ORIGIN) -> dict:
    return {"Origin": origin, "X-CSRF-Token": csrf}


def test_admin_publish_requires_if_match_and_uses_optimistic_lock(tmp_path):
    with config_api(tmp_path) as (client, store, auth, service, admin, member):
        csrf = login(client, auth, admin, admin)
        payload = {"defaults": {"speech": speech_route()}}

        missing = client.post("/api/admin/managed-config", json=payload,
                              headers=write_headers(csrf))
        assert missing.status_code == 400
        assert missing.json()["code"] == "invalid_if_match"

        invalid = client.post("/api/admin/managed-config", json=payload,
                              headers={**write_headers(csrf),
                                       "If-Match": "not-a-version"})
        assert invalid.status_code == 400

        published = client.post("/api/admin/managed-config", json=payload,
                                headers={**write_headers(csrf),
                                         "If-Match": '"0"'})
        assert published.status_code == 200, published.text
        assert published.json() == {"version": 1}
        assert published.headers["cache-control"] == "no-store"
        assert ADMIN_SECRET not in published.text

        stale = client.post("/api/admin/managed-config", json=payload,
                            headers={**write_headers(csrf), "If-Match": '"0"'})
        assert stale.status_code == 409
        assert stale.json()["code"] == "version_conflict"

        second = client.post("/api/admin/managed-config", json=payload,
                             headers={**write_headers(csrf), "If-Match": "1"})
        assert second.status_code == 200
        assert second.json() == {"version": 2}

        # 写保护先于发布：缺 CSRF 不能推进版本
        no_csrf = client.post("/api/admin/managed-config", json=payload,
                              headers={"Origin": ORIGIN, "If-Match": "2"})
        assert no_csrf.status_code == 403
        assert service.public_settings(admin)["version"] == 2


def test_get_settings_is_sanitized_for_member_and_admin(tmp_path):
    with config_api(tmp_path) as (client, store, auth, service, admin, member):
        admin_csrf = login(client, auth, admin, admin)
        publish = client.post(
            "/api/admin/managed-config",
            json={"defaults": {"speech": speech_route()}},
            headers={**write_headers(admin_csrf), "If-Match": "0"})
        assert publish.status_code == 200

        member_csrf = login(client, auth, admin, member)
        response = client.get("/api/settings/models")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["version"] == 1
        speech = body["channels"]["speech"]
        assert speech["configured"] is True
        assert speech["source"] == "admin"
        assert speech["version"] == 1
        assert speech["provider"] == "openai-compatible"
        assert speech["model"] == "tts-default"
        assert speech["base_url"] == "https://provider.example/v1"
        assert ADMIN_SECRET not in response.text
        assert "credential_ref" not in response.text
        assert "encrypted" not in response.text
        assert member_csrf


def test_personal_save_and_clear_override_over_http(tmp_path):
    with config_api(tmp_path) as (client, store, auth, service, admin, member):
        admin_csrf = login(client, auth, admin, admin)
        assert client.post(
            "/api/admin/managed-config",
            json={"defaults": {"speech": speech_route()}},
            headers={**write_headers(admin_csrf), "If-Match": "0"}
        ).status_code == 200

        csrf = login(client, auth, admin, member)
        route = speech_route(base_url="https://personal.example/v1",
                             model="tts-personal", secret=PERSONAL_SECRET)
        route.pop("allowed_models")
        saved = client.post(
            "/api/settings/models/save",
            json={"channel": "speech", "route": route},
            headers=write_headers(csrf))
        assert saved.status_code == 200, saved.text
        assert saved.headers["cache-control"] == "no-store"
        assert PERSONAL_SECRET not in saved.text

        settings = client.get("/api/settings/models").json()
        assert settings["channels"]["speech"]["source"] == "user"
        assert settings["channels"]["speech"]["base_url"] == \
            "https://personal.example/v1"

        missing_csrf = client.post(
            "/api/settings/models/save",
            json={"channel": "speech", "route": route},
            headers={"Origin": ORIGIN})
        assert missing_csrf.status_code == 403
        wrong_origin = client.post(
            "/api/settings/models/save",
            json={"channel": "speech", "route": route},
            headers={"Origin": "https://evil.test", "X-CSRF-Token": csrf})
        assert wrong_origin.status_code == 403

        cleared = client.delete("/api/settings/models/overrides/speech",
                                headers=write_headers(csrf))
        assert cleared.status_code == 204
        assert cleared.headers["cache-control"] == "no-store"
        settings = client.get("/api/settings/models").json()
        assert settings["channels"]["speech"]["source"] == "admin"
        assert settings["channels"]["speech"]["base_url"] == \
            "https://provider.example/v1"


def test_member_cannot_publish_even_with_forged_admin_fields(tmp_path):
    with config_api(tmp_path) as (client, store, auth, service, admin, member):
        csrf = login(client, auth, admin, member)
        response = client.post(
            "/api/admin/managed-config",
            json={"defaults": {"speech": speech_route()},
                  "role": "admin", "userId": admin.user_id},
            headers={**write_headers(csrf), "If-Match": "0",
                     "X-Easel-Role": "admin", "X-User-Id": admin.user_id})
        assert response.status_code == 403
        assert response.json()["code"] == "forbidden"
        assert store._connection.execute(
            "SELECT COUNT(*) FROM managed_revisions").fetchone()[0] == 0


def test_available_and_selftest_stay_unavailable(tmp_path):
    with config_api(tmp_path) as (client, store, auth, service, admin, member):
        csrf = login(client, auth, admin, member)
        for path in ("/api/settings/models/available",
                     "/api/settings/models/selftest"):
            response = client.post(path, json={}, headers=write_headers(csrf))
            assert response.status_code == 503, path
            assert response.json()["code"] == "capability_unavailable"


def test_config_routes_are_503_without_config_service(tmp_path):
    with config_api(tmp_path, with_service=False) as (
            client, store, auth, service, admin, member):
        csrf = login(client, auth, admin, member)
        assert client.get("/api/settings/models").status_code == 503
        assert client.post(
            "/api/settings/models/save",
            json={"channel": "speech", "route": speech_route()},
            headers=write_headers(csrf)).status_code == 503
        assert client.delete(
            "/api/settings/models/overrides/speech",
            headers=write_headers(csrf)).status_code == 503


def test_http_options_cannot_smuggle_credentials_or_poison_settings(tmp_path):
    option_sentinel = "option-secret-sentinel-http"
    with config_api(tmp_path) as (client, store, auth, service, admin, member):
        csrf = login(client, auth, admin, admin)
        headers = {**write_headers(csrf), "If-Match": "0"}

        for options in (
            {"headers": {"api-key": option_sentinel}},
            {"Headers": {"API-Key": option_sentinel}},
            {"auth": {"token": option_sentinel}},
            {"credentials": {"api-key": option_sentinel}},
        ):
            response = client.post(
                "/api/admin/managed-config",
                json={"defaults": {"speech": speech_route(options=options)}},
                headers=headers)
            assert response.status_code == 400, response.text
            assert response.json()["code"] == "invalid_route"
            assert option_sentinel not in response.text
        assert service.public_settings(admin)["version"] == 0

        published = client.post(
            "/api/admin/managed-config",
            json={"defaults": {"speech": speech_route(
                options={"speed": 1.0})}},
            headers=headers)
        assert published.status_code == 200
        assert published.json() == {"version": 1}

        base = json.dumps({"defaults": {"speech": speech_route(options={})}})
        poison_bodies = [
            base.replace('"options": {}', '"options": {"speed": 1e309}'),
            base.replace('"options": {}', '"options": {"speed": NaN}'),
            base.replace('"options": {}', '"options": {"speed": Infinity}'),
            base.replace('"options": {}', '"options": {"speed": -Infinity}'),
        ]
        for body in poison_bodies:
            response = client.post(
                "/api/admin/managed-config",
                content=body.encode("utf-8"),
                headers={**write_headers(csrf), "If-Match": "1",
                         "Content-Type": "application/json"})
            assert response.status_code == 400, (body, response.text)
            assert response.json()["code"] == "invalid_route"
        assert service.public_settings(admin)["version"] == 1

        login(client, auth, admin, member)
        settings = client.get("/api/settings/models")
        assert settings.status_code == 200, settings.text
        assert "Infinity" not in settings.text
        assert "NaN" not in settings.text
        assert option_sentinel not in settings.text
        rows = "".join(row[0] for row in store._connection.execute(
            "SELECT public_json FROM managed_revisions"))
        assert "Infinity" not in rows and "NaN" not in rows


def test_publish_errors_do_not_echo_secrets(tmp_path):
    with config_api(tmp_path) as (client, store, auth, service, admin, member):
        csrf = login(client, auth, admin, admin)
        bad = speech_route(base_url="file:///etc/passwd")
        response = client.post(
            "/api/admin/managed-config",
            json={"defaults": {"speech": bad}},
            headers={**write_headers(csrf), "If-Match": "0"})
        assert response.status_code == 400
        assert ADMIN_SECRET not in response.text
        assert "file://" not in response.text


def test_invalid_channels_and_bodies_are_rejected(tmp_path):
    with config_api(tmp_path) as (client, store, auth, service, admin, member):
        csrf = login(client, auth, admin, admin)
        headers = {**write_headers(csrf), "If-Match": "0"}

        unknown_channel = client.post(
            "/api/admin/managed-config",
            json={"defaults": {"chat": speech_route()}}, headers=headers)
        assert unknown_channel.status_code == 400
        assert unknown_channel.json()["code"] == "invalid_channel"

        malformed = client.post("/api/admin/managed-config", content=b"{",
                                headers=headers)
        assert malformed.status_code == 400
        assert malformed.json()["code"] == "invalid_body"

        not_object = client.post("/api/admin/managed-config", json=[],
                                 headers=headers)
        assert not_object.status_code == 400

        bad_save = client.post(
            "/api/settings/models/save", json={"channel": "speech"},
            headers=write_headers(csrf))
        assert bad_save.status_code == 400
