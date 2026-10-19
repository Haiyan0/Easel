"""Task 4 配置服务：默认继承、个人完整路由、加密与版本。"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

import pytest

from easel.control.config import CHANNELS, MASTER_KEY_BYTES, ConfigService
from easel.control.store import MIGRATIONS, SCHEMA_VERSION, Store
from easel.control.types import DomainError, EffectiveRoute, Identity

MASTER_KEY = b"k" * MASTER_KEY_BYTES
ADMIN_SECRET = "admin-secret-sentinel-9f3c"
PERSONAL_SECRET = "personal-secret-sentinel-7a1d"


def assert_error(error: pytest.ExceptionInfo, status: int, code: str = "") -> None:
    assert isinstance(error.value, DomainError)
    assert error.value.status == status, error.value
    if code:
        assert error.value.code == code, error.value


def speech_route(**overrides) -> dict:
    route = {
        "provider": "openai-compatible",
        "model": "tts-default",
        "base_url": "https://provider.example/v1",
        "protocol": "openai",
        "secret": ADMIN_SECRET,
        "options": {},
    }
    route.update(overrides)
    return route


def publish_speech(config: ConfigService, admin: Identity, version: int = 0,
                   route: dict | None = None, **overrides) -> int:
    return config.publish(
        admin, {"defaults": {"speech": route or speech_route(**overrides)}},
        expected_version=version)


@contextmanager
def control(tmp_path, key: bytes = MASTER_KEY):
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        yield store, ConfigService(store, key), admin, member


def read_override_blob(store: Store, workspace_id: str, channel: str) -> bytes:
    return store._connection.execute(
        "SELECT encrypted_secrets FROM workspace_overrides"
        " WHERE workspace_id = ? AND channel = ?",
        (workspace_id, channel)).fetchone()[0]


def database_bytes(path) -> bytes:
    data = path.read_bytes()
    wal = path.with_name(path.name + "-wal")
    if wal.exists():
        data += wal.read_bytes()
    return data


# --------------------------------------------------------------------------
# 默认继承与版本
# --------------------------------------------------------------------------

def test_new_personal_endpoint_never_inherits_admin_secret(tmp_path):
    with control(tmp_path) as (store, config, admin, member):
        assert publish_speech(config, admin) == 1
        inherited = config.resolve(member, "speech")
        assert inherited.source == "admin"
        assert inherited.version == 1
        assert ADMIN_SECRET not in repr(inherited)
        assert "credential_ref" in repr(inherited)

        with pytest.raises(DomainError) as invalid:
            config.save_override(member, "speech", speech_route(
                base_url="https://personal.example/v1", secret=""))
        assert_error(invalid, 400, "personal_credentials_required")

        public = config.public_settings(member)
        assert ADMIN_SECRET not in json.dumps(public, ensure_ascii=False)
        assert "credential_ref" not in json.dumps(public)


def test_publish_version_and_admin_authorization(tmp_path):
    with control(tmp_path) as (store, config, admin, member):
        assert publish_speech(config, admin) == 1
        assert publish_speech(config, admin, version=1,
                              model="tts-default-v2") == 2
        assert config.resolve(member, "speech").version == 2
        assert config.resolve(member, "speech").model == "tts-default-v2"

        with pytest.raises(DomainError) as stale:
            publish_speech(config, admin, version=1)
        assert_error(stale, 409, "version_conflict")

        with pytest.raises(DomainError) as denied:
            publish_speech(config, member)
        assert_error(denied, 403)

        # 授权先于请求体校验：member 不能借畸形 settings 探测校验细节
        with pytest.raises(DomainError) as denied_invalid:
            config.publish(member, {"not-defaults": {}}, 0)
        assert_error(denied_invalid, 403)

        versions = [row[0] for row in store._connection.execute(
            "SELECT version FROM managed_revisions ORDER BY version")]
        assert versions == [1, 2]


def test_concurrent_publish_with_same_expected_version_one_wins(tmp_path):
    path = tmp_path / "control.sqlite3"
    with Store.open(path) as setup:
        admin = setup.create_user("Owner", "admin")
    barrier = threading.Barrier(2, timeout=20)
    versions, errors = [], []

    def publish():
        with Store.open(path) as store:
            config = ConfigService(store, MASTER_KEY)
            barrier.wait()
            try:
                versions.append(publish_speech(config, admin))
            except DomainError as error:
                errors.append(error)

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()

    assert versions == [1]
    assert len(errors) == 1 and errors[0].status == 409


# --------------------------------------------------------------------------
# 个人覆盖与继承语义
# --------------------------------------------------------------------------

def test_complete_personal_route_stays_independent_of_admin_updates(tmp_path):
    with control(tmp_path) as (store, config, admin, member):
        publish_speech(config, admin)
        personal = speech_route(
            base_url="https://personal.example/v1", secret=PERSONAL_SECRET,
            model="tts-personal")
        config.save_override(member, "speech", personal)

        resolved = config.resolve(member, "speech")
        assert resolved.source == "user"
        assert resolved.base_url == "https://personal.example/v1"
        assert resolved.model == "tts-personal"
        assert "personal.example" not in resolved.credential_ref
        assert PERSONAL_SECRET not in repr(resolved)

        publish_speech(config, admin, version=1,
                       base_url="https://provider-v2.example/v1",
                       model="tts-v2")
        still_personal = config.resolve(member, "speech")
        assert still_personal.base_url == "https://personal.example/v1"
        assert still_personal.model == "tts-personal"
        assert still_personal.version == 1, "个人完整路由使用自己的修订号"

        other = store.create_user("Other")
        inherited = config.resolve(other, "speech")
        assert inherited.source == "admin"
        assert inherited.base_url == "https://provider-v2.example/v1"
        assert inherited.version == 2


def test_clear_override_restores_latest_default(tmp_path):
    with control(tmp_path) as (store, config, admin, member):
        publish_speech(config, admin)
        config.save_override(member, "speech", speech_route(
            base_url="https://personal.example/v1", secret=PERSONAL_SECRET))
        assert config.resolve(member, "speech").source == "user"
        config.clear_override(member, "speech")
        assert config.resolve(member, "speech").source == "admin"
        config.clear_override(member, "speech")  # 幂等：再次清除不报错
        assert config.resolve(member, "speech").source == "admin"


def test_a_b_override_isolation_and_forged_workspace_rejected(tmp_path):
    with control(tmp_path) as (store, config, admin, member):
        other = store.create_user("Other")
        publish_speech(config, admin)
        config.save_override(member, "speech", speech_route(
            base_url="https://a.example/v1", secret="a-secret"))
        config.save_override(other, "speech", speech_route(
            base_url="https://b.example/v1", secret="b-secret"))

        assert config.resolve(member, "speech").base_url == "https://a.example/v1"
        assert config.resolve(other, "speech").base_url == "https://b.example/v1"

        forged = Identity(member.user_id, other.workspace_id, "member")
        with pytest.raises(DomainError) as denied:
            config.save_override(forged, "speech", speech_route(
                base_url="https://evil.example/v1", secret="evil"))
        assert_error(denied, 403)


def test_empty_secret_reuses_only_same_credential_scope(tmp_path):
    with control(tmp_path) as (store, config, admin, member):
        publish_speech(config, admin)
        config.save_override(member, "speech", speech_route(
            secret=PERSONAL_SECRET))
        before = read_override_blob(store, member.workspace_id, "speech")

        config.save_override(member, "speech", speech_route(
            secret="", model="tts-personal-v2"))
        after = read_override_blob(store, member.workspace_id, "speech")
        assert after == before, "同一凭证作用域下空 Key 应复用原密文"
        assert config.resolve(member, "speech").model == "tts-personal-v2"

        for changed in (
            speech_route(base_url="https://new.example/v1", secret=""),
            speech_route(provider="gemini", secret=""),
            speech_route(protocol="anthropic", secret=""),
        ):
            with pytest.raises(DomainError) as invalid:
                config.save_override(member, "speech", changed)
            assert_error(invalid, 400, "personal_credentials_required")

        config.save_override(member, "speech", speech_route(
            base_url="https://new.example/v1", secret="new-personal-secret"))
        changed_blob = read_override_blob(store, member.workspace_id, "speech")
        assert changed_blob != before


# --------------------------------------------------------------------------
# managed-model
# --------------------------------------------------------------------------

def test_managed_model_follows_latest_admin_route(tmp_path):
    with control(tmp_path) as (store, config, admin, member):
        route = speech_route(allowed_models=["tts-default", "tts-alt"])
        publish_speech(config, admin, route=route)
        config.save_override(member, "speech", {
            "route_kind": "managed-model",
            "model": "tts-alt",
            "options": {"speed": 1.2},
        })

        resolved = config.resolve(member, "speech")
        assert resolved.source == "user"
        assert resolved.model == "tts-alt"
        assert resolved.base_url == "https://provider.example/v1"
        assert resolved.version == 1
        assert resolved.credential_ref.startswith("managed:")
        assert resolved.options == {"speed": 1.2}

        publish_speech(config, admin, version=1, route=speech_route(
            base_url="https://provider-v2.example/v1",
            model="tts-default-v2",
            allowed_models=["tts-default-v2", "tts-alt"]))
        followed = config.resolve(member, "speech")
        assert followed.base_url == "https://provider-v2.example/v1"
        assert followed.version == 2
        assert followed.model == "tts-alt"

        with pytest.raises(DomainError) as not_allowed:
            config.save_override(member, "speech", {
                "route_kind": "managed-model", "model": "tts-evil"})
        assert_error(not_allowed, 400, "model_not_allowed")


def test_managed_model_without_explicit_model_follows_latest_default(tmp_path):
    with control(tmp_path) as (store, config, admin, member):
        publish_speech(config, admin, route=speech_route(
            model="tts-v1", allowed_models=["tts-v1", "tts-v2"]))
        config.save_override(member, "speech", {"route_kind": "managed-model"})
        assert config.resolve(member, "speech").model == "tts-v1"

        publish_speech(config, admin, version=1, route=speech_route(
            model="tts-v2", allowed_models=["tts-v1", "tts-v2"]))
        followed = config.resolve(member, "speech")
        assert followed.model == "tts-v2"
        assert followed.version == 2


def test_managed_model_rejects_privileged_fields_and_missing_default(tmp_path):
    with control(tmp_path) as (store, config, admin, member):
        with pytest.raises(DomainError) as missing:
            config.save_override(member, "speech", {
                "route_kind": "managed-model", "model": "tts-default"})
        assert_error(missing, 404, "not_configured")

        publish_speech(config, admin,
                       route=speech_route(allowed_models=["tts-default"]))
        for bad in (
            {"route_kind": "managed-model", "model": "tts-default",
             "base_url": "https://evil.example/v1"},
            {"route_kind": "managed-model", "model": "tts-default",
             "secret": "evil"},
            {"route_kind": "managed-model", "model": "tts-default",
             "env": {"EVIL": "1"}},
            {"route_kind": "managed-model", "model": "tts-default",
             "options": {"api_key": "evil"}},
        ):
            with pytest.raises(DomainError) as invalid:
                config.save_override(member, "speech", bad)
            assert_error(invalid, 400, "invalid_route")


# --------------------------------------------------------------------------
# 校验与凭证 bundle
# --------------------------------------------------------------------------

def test_default_publish_rejects_unknown_and_privileged_fields(tmp_path):
    with control(tmp_path) as (store, config, admin, member):
        bad_routes = [
            speech_route(provider="unknown-provider"),
            speech_route(base_url="file:///etc/passwd"),
            speech_route(env={"EVIL": "1"}),
            speech_route(argv=["--evil"]),
            speech_route(runtime="evil"),
            speech_route(options={"api_key": "evil"}),
            {"route_kind": "managed-model", "model": "tts-default"},
        ]
        for bad in bad_routes:
            with pytest.raises(DomainError) as invalid:
                publish_speech(config, admin, route=bad)
            assert_error(invalid, 400)
        assert store._connection.execute(
            "SELECT COUNT(*) FROM managed_revisions").fetchone()[0] == 0


def test_publish_is_atomic_across_channels(tmp_path):
    with control(tmp_path) as (store, config, admin, member):
        bad = {
            "defaults": {
                "speech": speech_route(),
                "image": speech_route(provider="unknown-provider"),
            }
        }
        with pytest.raises(DomainError) as invalid:
            config.publish(admin, bad, expected_version=0)
        assert_error(invalid, 400, "unknown_provider")
        assert store._connection.execute(
            "SELECT COUNT(*) FROM managed_revisions").fetchone()[0] == 0


def test_default_publish_requires_broker_adapter(tmp_path):
    with control(tmp_path) as (store, config, admin, member):
        route = speech_route(provider="dashscope", secret={"DASHSCOPE_API_KEY": "d"})
        with pytest.raises(DomainError) as unavailable:
            publish_speech(config, admin, route=route)
        assert_error(unavailable, 400, "adapter_unavailable")


def test_secret_bundle_shapes_match_registry(tmp_path):
    with control(tmp_path) as (store, config, admin, member):
        publish_speech(config, admin)

        # 双 Key：Kling 两个 required secret 都必须提供
        kling = {
            "provider": "kling", "model": "kling-v1",
            "base_url": "https://kling.example/v1", "protocol": "kling",
            "secret": {"KLING_ACCESS_KEY": "ak", "KLING_SECRET_KEY": "sk"},
            "options": {},
        }
        config.save_override(member, "video", kling)
        for broken in (
            {**kling, "secret": {"KLING_ACCESS_KEY": "ak"}},
            {**kling, "secret": "single-string"},
            {**kling, "secret": {"KLING_ACCESS_KEY": "ak",
                                 "KLING_SECRET_KEY": "sk", "UNKNOWN": "x"}},
        ):
            with pytest.raises(DomainError) as invalid:
                config.save_override(member, "video", broken)
            assert_error(invalid, 400, "invalid_secret_bundle")

        # required 非 secret 字段（Minimax Group ID）必须提供
        minimax = {
            "provider": "minimax", "model": "speech-01",
            "base_url": "https://minimax.example/v1", "protocol": "minimax",
            "secret": "string-is-not-enough", "options": {},
        }
        with pytest.raises(DomainError) as missing_group:
            config.save_override(member, "speech", minimax)
        assert_error(missing_group, 400)

        minimax["secret"] = {"MINIMAX_API_KEY": "mk",
                             "MINIMAX_GROUP_ID": "group-1"}
        config.save_override(member, "speech", minimax)
        assert config.resolve(member, "speech").provider == "minimax"


def test_unknown_channel_and_expected_version_validation(tmp_path):
    with control(tmp_path) as (store, config, admin, member):
        with pytest.raises(DomainError) as channel:
            config.resolve(member, "chat")
        assert_error(channel, 400, "invalid_channel")

        for expected in (-1, True, "0", 1.5):
            with pytest.raises(DomainError) as invalid:
                config.publish(admin, {"defaults": {}}, expected)
            assert_error(invalid, 400, "invalid_version")


# --------------------------------------------------------------------------
# 加密、错误 key、篡改与跨作用域
# --------------------------------------------------------------------------

def test_database_and_public_output_contain_no_plaintext(tmp_path):
    path = tmp_path / "control.sqlite3"
    with Store.open(path) as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        config = ConfigService(store, MASTER_KEY)
        publish_speech(config, admin)
        config.save_override(member, "speech", speech_route(
            base_url="https://personal.example/v1", secret=PERSONAL_SECRET))
        public = json.dumps(config.public_settings(member), ensure_ascii=False)
        assert ADMIN_SECRET not in public
        assert PERSONAL_SECRET not in public
        blob = database_bytes(path)
        assert ADMIN_SECRET.encode() not in blob
        assert PERSONAL_SECRET.encode() not in blob


def test_wrong_or_short_master_key_fails_explicitly(tmp_path):
    with control(tmp_path) as (store, config, admin, member):
        publish_speech(config, admin)
        for bad_key in (b"short", b"x" * 31, b"x" * 33):
            with pytest.raises(ValueError):
                ConfigService(store, bad_key)
        with pytest.raises(ValueError) as mismatch:
            ConfigService(store, b"z" * MASTER_KEY_BYTES)
        assert "master_key" in str(mismatch.value)
        assert "z" * 32 not in str(mismatch.value)


def test_tampered_managed_and_override_ciphertext_fail_closed(tmp_path):
    with control(tmp_path) as (store, config, admin, member):
        publish_speech(config, admin)
        config.save_override(member, "speech", speech_route(
            base_url="https://personal.example/v1", secret=PERSONAL_SECRET))

        store._connection.execute(
            "UPDATE workspace_overrides SET encrypted_secrets = ?"
            " WHERE workspace_id = ? AND channel = 'speech'",
            (b"tampered-ciphertext", member.workspace_id))
        with pytest.raises(DomainError) as tampered_override:
            config.resolve(member, "speech")
        assert_error(tampered_override, 500, "secret_unavailable")

        store._connection.execute(
            "UPDATE managed_revisions SET encrypted_secrets = ? WHERE version = 1",
            (b"tampered-ciphertext",))
        with pytest.raises(DomainError) as tampered_managed:
            config.resolve(store.create_user("Third"), "speech")
        assert_error(tampered_managed, 500, "secret_unavailable")


def test_managed_ciphertext_cannot_be_swapped_across_versions(tmp_path):
    with control(tmp_path) as (store, config, admin, member):
        publish_speech(config, admin)
        publish_speech(config, admin, version=1,
                       base_url="https://provider-v2.example/v1")
        old_blob = store._connection.execute(
            "SELECT encrypted_secrets FROM managed_revisions WHERE version = 1"
        ).fetchone()[0]
        store._connection.execute(
            "UPDATE managed_revisions SET encrypted_secrets = ? WHERE version = 2",
            (old_blob,))
        with pytest.raises(DomainError) as swapped:
            config.resolve(member, "speech")
        assert_error(swapped, 500, "secret_unavailable")


def test_cross_workspace_and_channel_ciphertext_swap_fails(tmp_path):
    with control(tmp_path) as (store, config, admin, member):
        other = store.create_user("Other")
        publish_speech(config, admin)
        config.save_override(member, "speech", speech_route(
            base_url="https://a.example/v1", secret="a-secret"))
        config.save_override(other, "speech", speech_route(
            base_url="https://b.example/v1", secret="b-secret"))
        config.save_override(member, "image", {
            "provider": "openai", "model": "img-1",
            "base_url": "https://image.example/v1", "protocol": "openai",
            "secret": "image-secret", "options": {},
        })

        a_blob = read_override_blob(store, member.workspace_id, "speech")
        # A 的 speech 密文塞进 B 的 speech 行
        store._connection.execute(
            "UPDATE workspace_overrides SET encrypted_secrets = ?"
            " WHERE workspace_id = ? AND channel = 'speech'",
            (a_blob, other.workspace_id))
        with pytest.raises(DomainError) as cross_workspace:
            config.resolve(other, "speech")
        assert_error(cross_workspace, 500, "secret_unavailable")

        # A 的 speech 密文塞进 A 的 image 行
        store._connection.execute(
            "UPDATE workspace_overrides SET encrypted_secrets = ?"
            " WHERE workspace_id = ? AND channel = 'image'",
            (a_blob, member.workspace_id))
        with pytest.raises(DomainError) as cross_channel:
            config.resolve(member, "image")
        assert_error(cross_channel, 500, "secret_unavailable")


# --------------------------------------------------------------------------
# v2 → v3 迁移
# --------------------------------------------------------------------------

def make_v2_database(path) -> None:
    """用冻结的 migration 1/2 建 v2 真实旧库并写入历史数据。"""
    connection = sqlite3.connect(path)
    try:
        for statement in MIGRATIONS[0]:
            connection.execute(statement)
        for statement in MIGRATIONS[1]:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 2")
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (1, 900), (2, 901)")
        connection.execute(
            "INSERT INTO users (id, display_name, role, created_at) VALUES"
            " ('admin-1', '旧管理员', 'admin', 900),"
            " ('member-1', '旧成员', 'member', 901)")
        connection.execute(
            "INSERT INTO workspaces (id, owner_user_id, state, config_version, created_at)"
            " VALUES ('ws-admin', 'admin-1', 'ready', 1, 900),"
            " ('ws-member', 'member-1', 'ready', 2, 901)")
        connection.execute(
            "INSERT INTO conversations (workspace_id, id, title, archived, created_at, updated_at)"
            " VALUES ('ws-member', 'conv-1', '旧会话', 0, 902, 903)")
        connection.execute(
            "INSERT INTO runs (workspace_id, id, conversation_id, client_request_id,"
            " input_digest, status, config_version, created_at, finished_at,"
            " capacity_released_at, release_count)"
            " VALUES ('ws-member', 'run-1', 'conv-1', 'old-request', 'old-digest',"
            " 'done', 2, 904, 905, 905, 1)")
        connection.execute(
            "INSERT INTO invites (id, user_id, token_digest, expires_at, created_at, created_by)"
            " VALUES ('invite-1', 'member-1', 'invite-digest', 999999, 906, 'admin-1')")
        connection.execute(
            "INSERT INTO logins (id, user_id, token_digest, csrf_digest, created_at,"
            " last_seen_at, expires_at) VALUES"
            " ('login-1', 'member-1', 'login-digest', 'csrf-digest', 907, 907, 999999)")
        connection.execute(
            "INSERT INTO auth_failures (source, token_digest, attempted_at)"
            " VALUES ('203.0.113.1', 'failure-digest', 908)")
        connection.commit()
    finally:
        connection.close()


def test_v2_database_upgrades_in_place_and_preserves_data(tmp_path):
    path = tmp_path / "v2-control.sqlite3"
    make_v2_database(path)

    with Store.open(path) as store:
        assert SCHEMA_VERSION >= 3
        actor = Identity("member-1", "ws-member", "member")
        assert store.get_conversation(actor, "conv-1").title == "旧会话"
        replayed = store.reserve_run(actor, "conv-1", "old-request", "old-digest", 2)
        assert replayed.id == "run-1"
        assert store.get_run(actor, "run-1").status == "done"

        tables = {row[0] for row in store._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"managed_revisions", "workspace_overrides"} <= tables
        assert store._connection.execute(
            "SELECT COUNT(*) FROM invites WHERE id = 'invite-1'").fetchone()[0] == 1
        assert store._connection.execute(
            "SELECT COUNT(*) FROM logins WHERE id = 'login-1'").fetchone()[0] == 1
        assert store._connection.execute(
            "SELECT COUNT(*) FROM auth_failures").fetchone()[0] == 1

        admin = store.get_identity("admin-1")
        config = ConfigService(store, MASTER_KEY)
        assert publish_speech(config, admin) == 1
        assert config.resolve(actor, "speech").source == "admin"

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert [row[0] for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version")] == \
            list(range(1, SCHEMA_VERSION + 1))
    finally:
        connection.close()


# --------------------------------------------------------------------------
# 供应商元数据
# --------------------------------------------------------------------------

def test_registry_exposes_broker_and_override_metadata():
    from model_registry import MODEL_GROUPS, provider_ids, provider_metadata

    for group, spec in MODEL_GROUPS.items():
        assert provider_ids(group) == tuple(p["id"] for p in spec["providers"])
        for provider in spec["providers"]:
            assert "broker_adapter" in provider
            assert "override_allowed" in provider

    assert provider_metadata("voice", "openai-compatible")["broker_adapter"] == \
        "openai-compatible"
    assert provider_metadata("image", "openai")["broker_adapter"] == \
        "openai-compatible"
    assert provider_metadata("video", "dashscope")["broker_adapter"] is None
    assert provider_metadata("video", "dashscope")["override_allowed"] is True
