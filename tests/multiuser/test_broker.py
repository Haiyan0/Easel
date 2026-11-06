"""Task 5 broker：Run 范围令牌、配置快照、生命周期与并发预算。"""
from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager

import httpx
import pytest

from easel.control.broker import BrokerService
from easel.control.config import ConfigService, MASTER_KEY_BYTES
from easel.control.egress import EgressGateway, EgressPolicy
from easel.control.store import Store
from easel.control.types import DomainError

MASTER_KEY = b"k" * MASTER_KEY_BYTES
ADMIN_SECRET = "admin-secret-sentinel-broker"
PERSONAL_SECRET = "personal-secret-sentinel-broker"


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


def publish_speech(config: ConfigService, admin, version: int = 0,
                   **overrides) -> int:
    return config.publish(
        admin, {"defaults": {"speech": speech_route(**overrides)}},
        expected_version=version)


@contextmanager
def control(tmp_path, clock=None):
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        config = ConfigService(store, MASTER_KEY)
        broker = BrokerService(store, config, clock=clock or (lambda: 1000.0))
        yield store, config, broker, admin, member


def reserve_run(store: Store, actor, request_id: str = "req-1"):
    conversation = store.create_conversation(actor)
    return store.reserve_run(actor, conversation.id, request_id, "digest", 1)


def database_bytes(path) -> bytes:
    data = path.read_bytes()
    wal = path.with_name(path.name + "-wal")
    if wal.exists():
        data += wal.read_bytes()
    return data


# --------------------------------------------------------------------------
# 令牌范围与生命周期
# --------------------------------------------------------------------------

def test_broker_token_cannot_change_workspace_or_channel(tmp_path):
    with control(tmp_path) as (store, config, broker, admin, member):
        publish_speech(config, admin)
        other = store.create_user("B")
        run = reserve_run(store, member)

        token = broker.issue(member, run.id, "speech")
        with pytest.raises(DomainError) as wrong_workspace:
            broker.authorize(token, other.workspace_id, run.id, "speech")
        assert_error(wrong_workspace, 404, "not_found")
        with pytest.raises(DomainError) as wrong_channel:
            broker.authorize(token, member.workspace_id, run.id, "image")
        assert_error(wrong_channel, 404, "not_found")
        with pytest.raises(DomainError) as wrong_run:
            broker.authorize(token, member.workspace_id, "other-run", "speech")
        assert_error(wrong_run, 404, "not_found")


def test_token_is_opaque_and_database_stores_only_digest(tmp_path):
    path = tmp_path / "control.sqlite3"
    with Store.open(path) as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        config = ConfigService(store, MASTER_KEY)
        publish_speech(config, admin)
        run = reserve_run(store, member)
        broker = BrokerService(store, config, clock=lambda: 1000.0)

        token = broker.issue(member, run.id, "speech")
        assert isinstance(token, str) and len(token) >= 32
        assert ADMIN_SECRET not in repr(broker)
        assert token.encode() not in database_bytes(path)

        row = store._connection.execute(
            "SELECT token_digest, snapshot_id, route_json, encrypted_secrets"
            " FROM broker_tokens").fetchone()
        assert row["token_digest"] and row["token_digest"] != token
        assert len(row["token_digest"]) == 64
        assert token not in json.dumps(dict(row), default=str)
        assert ADMIN_SECRET.encode() not in database_bytes(path)
        route = broker.authorize(token, member.workspace_id, run.id, "speech")
        assert ADMIN_SECRET not in repr(route)
        assert route.credential_ref.startswith("snapshot:")


def test_token_expiry_uses_injected_clock(tmp_path):
    now = {"value": 1000.0}
    with control(tmp_path, clock=lambda: now["value"]) as (
            store, config, broker, admin, member):
        publish_speech(config, admin)
        run = reserve_run(store, member)
        token = broker.issue(member, run.id, "speech")

        now["value"] = 1000.0 + 1799
        assert broker.authorize(token, member.workspace_id, run.id,
                                "speech").channel == "speech"
        now["value"] = 1000.0 + 1800
        with pytest.raises(DomainError) as expired:
            broker.authorize(token, member.workspace_id, run.id, "speech")
        assert_error(expired, 401, "invalid_token")


def test_revoke_run_is_visible_across_connections(tmp_path):
    path = tmp_path / "control.sqlite3"
    with Store.open(path) as first:
        admin = first.create_user("Owner", "admin")
        member = first.create_user("Member")
        config = ConfigService(first, MASTER_KEY)
        publish_speech(config, admin)
        run = reserve_run(first, member)
        token = BrokerService(first, config, clock=lambda: 1000.0).issue(
            member, run.id, "speech")

        with Store.open(path) as second:
            other_config = ConfigService(second, MASTER_KEY)
            other_broker = BrokerService(second, other_config,
                                         clock=lambda: 1001.0)
            assert other_broker.authorize(
                token, member.workspace_id, run.id, "speech").channel == "speech"
            other_broker.revoke_run(run.id, member.workspace_id)
            with pytest.raises(DomainError) as revoked:
                other_broker.authorize(
                    token, member.workspace_id, run.id, "speech")
            assert_error(revoked, 401, "invalid_token")

        with pytest.raises(DomainError) as stale:
            BrokerService(first, config, clock=lambda: 1002.0).authorize(
                token, member.workspace_id, run.id, "speech")
        assert_error(stale, 401, "invalid_token")


def test_terminal_run_and_disabled_user_are_rejected(tmp_path):
    with control(tmp_path) as (store, config, broker, admin, member):
        publish_speech(config, admin)
        run = reserve_run(store, member)
        token = broker.issue(member, run.id, "speech")
        store.finish_run(member, run.id, "done")
        with pytest.raises(DomainError) as terminal:
            broker.authorize(token, member.workspace_id, run.id, "speech")
        assert_error(terminal, 409, "terminal_run")

        active = reserve_run(store, member, request_id="req-2")
        active_token = broker.issue(member, active.id, "speech")
        store.disable_user(member.user_id)
        with pytest.raises(DomainError) as disabled:
            broker.authorize(active_token, member.workspace_id, active.id,
                             "speech")
        assert_error(disabled, 403, "user_disabled")


def test_issue_requires_owned_active_run_and_never_creates_run(tmp_path):
    with control(tmp_path) as (store, config, broker, admin, member):
        publish_speech(config, admin)
        foreign = reserve_run(store, admin)
        before = store._connection.execute(
            "SELECT COUNT(*) FROM runs").fetchone()[0]
        with pytest.raises(DomainError):
            broker.issue(member, foreign.id, "speech")
        assert store._connection.execute(
            "SELECT COUNT(*) FROM runs").fetchone()[0] == before

        own = reserve_run(store, member)
        broker.issue(member, own.id, "speech")
        assert store._connection.execute(
            "SELECT COUNT(*) FROM runs").fetchone()[0] == before + 1

        store.finish_run(member, own.id, "done")
        with pytest.raises(DomainError) as terminal:
            broker.issue(member, own.id, "speech")
        assert_error(terminal, 409, "terminal_run")


# --------------------------------------------------------------------------
# 不可变配置快照
# --------------------------------------------------------------------------

def test_admin_update_does_not_change_existing_token_snapshot(tmp_path):
    with control(tmp_path) as (store, config, broker, admin, member):
        publish_speech(config, admin, model="tts-v1",
                       base_url="https://provider-v1.example/v1",
                       secret="admin-secret-v1")
        run = reserve_run(store, member)
        old_token = broker.issue(member, run.id, "speech")
        old = broker.authorize(old_token, member.workspace_id, run.id, "speech")
        assert old.model == "tts-v1"
        assert old.base_url == "https://provider-v1.example/v1"

        publish_speech(config, admin, version=1, model="tts-v2",
                       base_url="https://provider-v2.example/v1",
                       secret="admin-secret-v2")
        still_old = broker.authorize(old_token, member.workspace_id, run.id,
                                     "speech")
        assert still_old.model == "tts-v1"
        assert still_old.base_url == "https://provider-v1.example/v1"
        assert config.credentials_for_ref(still_old.credential_ref)[
            "VOICE_API_KEY"] == "admin-secret-v1"

        new_token = broker.issue(member, run.id, "speech")
        new = broker.authorize(new_token, member.workspace_id, run.id, "speech")
        assert new.model == "tts-v2"
        assert new.base_url == "https://provider-v2.example/v1"
        assert config.credentials_for_ref(new.credential_ref)[
            "VOICE_API_KEY"] == "admin-secret-v2"


def test_personal_override_clear_and_rebuild_keeps_old_token_snapshot(tmp_path):
    with control(tmp_path) as (store, config, broker, admin, member):
        publish_speech(config, admin)
        config.save_override(member, "speech", speech_route(
            model="personal-v1", base_url="https://personal-v1.example/v1",
            secret="personal-secret-v1"))
        run = reserve_run(store, member)
        old_token = broker.issue(member, run.id, "speech")
        old = broker.authorize(old_token, member.workspace_id, run.id, "speech")
        assert old.model == "personal-v1"
        assert config.credentials_for_ref(old.credential_ref)[
            "VOICE_API_KEY"] == "personal-secret-v1"

        # 更新个人覆盖后 revision 递增
        config.save_override(member, "speech", speech_route(
            model="personal-v2", base_url="https://personal-v2.example/v1",
            secret="personal-secret-v2"))
        # 清除后 revision 从 1 重新开始；旧 token 不能静默指向新密文
        config.clear_override(member, "speech")
        config.save_override(member, "speech", speech_route(
            model="personal-v3", base_url="https://personal-v3.example/v1",
            secret="personal-secret-v3"))

        still_old = broker.authorize(old_token, member.workspace_id, run.id,
                                     "speech")
        assert still_old.model == "personal-v1"
        assert still_old.base_url == "https://personal-v1.example/v1"
        assert config.credentials_for_ref(still_old.credential_ref)[
            "VOICE_API_KEY"] == "personal-secret-v1"

        new_token = broker.issue(member, run.id, "speech")
        new = broker.authorize(new_token, member.workspace_id, run.id, "speech")
        assert new.model == "personal-v3"
        assert config.credentials_for_ref(new.credential_ref)[
            "VOICE_API_KEY"] == "personal-secret-v3"


def test_snapshot_and_errors_never_expose_plaintext_keys(tmp_path):
    path = tmp_path / "control.sqlite3"
    with Store.open(path) as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        config = ConfigService(store, MASTER_KEY)
        publish_speech(config, admin, secret="admin-secret-sentinel-snapshot")
        config.save_override(member, "speech", speech_route(
            base_url="https://personal.example/v1",
            secret="personal-secret-sentinel-snapshot"))
        run = reserve_run(store, member)
        broker = BrokerService(store, config, clock=lambda: 1000.0)
        token = broker.issue(member, run.id, "speech")
        broker.authorize(token, member.workspace_id, run.id, "speech")

        blob = database_bytes(path)
        for secret in ("admin-secret-sentinel-snapshot",
                       "personal-secret-sentinel-snapshot"):
            assert secret.encode() not in blob
            assert secret not in repr(broker)
            assert secret not in token

        with pytest.raises(DomainError) as failure:
            broker.authorize("not-a-token", member.workspace_id, run.id,
                             "speech")
        assert "admin-secret-sentinel-snapshot" not in str(failure.value)
        assert "not-a-token" not in str(failure.value)


# --------------------------------------------------------------------------
# 并发预算
# --------------------------------------------------------------------------

def test_broker_leases_enforce_workspace_and_global_limits(tmp_path):
    with control(tmp_path) as (store, config, broker, admin, member):
        publish_speech(config, admin)
        run = reserve_run(store, member)
        token = broker.issue(member, run.id, "speech")

        first = broker.acquire_lease(token, member.workspace_id, run.id,
                                     "speech")
        second = broker.acquire_lease(token, member.workspace_id, run.id,
                                      "speech")
        with pytest.raises(DomainError) as over:
            broker.acquire_lease(token, member.workspace_id, run.id, "speech")
        assert_error(over, 429, "workspace_capacity")
        broker.release_lease(first)
        broker.release_lease(second)
        broker.release_lease(first)  # 重复释放必须安全

        # 全局 8：四个 workspace 各占 2 个额度，第五个 workspace 的第 1 个被拒
        tokens = []
        for index in range(4):
            user = store.create_user(f"U{index}")
            user_run = reserve_run(store, user, request_id=f"global-{index}")
            tokens.append((user, user_run, broker.issue(
                user, user_run.id, "speech")))
        for user, user_run, user_token in tokens:
            broker.acquire_lease(user_token, user.workspace_id, user_run.id,
                                 "speech")
            broker.acquire_lease(user_token, user.workspace_id, user_run.id,
                                 "speech")

        fifth = store.create_user("U5")
        fifth_run = reserve_run(store, fifth, request_id="global-5")
        fifth_token = broker.issue(fifth, fifth_run.id, "speech")
        with pytest.raises(DomainError) as global_over:
            broker.acquire_lease(fifth_token, fifth.workspace_id,
                                 fifth_run.id, "speech")
        assert_error(global_over, 429, "global_capacity")


def test_lease_acquisition_is_atomic_across_connections(tmp_path):
    path = tmp_path / "control.sqlite3"
    with Store.open(path) as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        config = ConfigService(store, MASTER_KEY)
        publish_speech(config, admin)
        run = reserve_run(store, member)
        token = BrokerService(store, config, clock=lambda: 1000.0).issue(
            member, run.id, "speech")

    barrier = threading.Barrier(8)
    results: list[str] = []
    errors: list[str] = []

    def worker():
        with Store.open(path) as connection:
            broker = BrokerService(
                connection, ConfigService(connection, MASTER_KEY),
                clock=lambda: 1000.0)
            barrier.wait()
            try:
                results.append(broker.acquire_lease(
                    token, member.workspace_id, run.id, "speech"))
            except DomainError as error:
                errors.append(error.code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2, (results, errors)
    assert sorted(errors) == ["workspace_capacity"] * 6


def test_broker_requires_same_egress_policy_as_config(tmp_path):
    with Store.open(tmp_path / "control.sqlite3") as store:
        store.create_user("Owner", "admin")
        config = ConfigService(store, MASTER_KEY)
        mismatched = EgressGateway(
            policy=EgressPolicy(approved_hosts=frozenset({"provider.internal"})))
        with pytest.raises(ValueError):
            BrokerService(store, config, egress=mismatched)


# --------------------------------------------------------------------------
# T5-R1-01：路由与凭证必须在一致事务中取得
# --------------------------------------------------------------------------

class PausingConfigService(ConfigService):
    """在一致快照读取点暂停，供双连接交错测试使用。"""

    def __init__(self, store: Store, master_key: bytes):
        super().__init__(store, master_key)
        self.snapshot_entered = threading.Event()
        self.resume = threading.Event()

    def snapshot_from_rows(self, workspace_id, channel, managed_row,
                           override_row):
        route, bundle = super().snapshot_from_rows(
            workspace_id, channel, managed_row, override_row)
        self.snapshot_entered.set()
        if not self.resume.wait(5):
            raise RuntimeError("test resume timeout")
        return route, bundle


def test_issue_reads_route_and_credentials_in_one_transaction(tmp_path):
    path = tmp_path / "control.sqlite3"
    with Store.open(path) as setup:
        admin = setup.create_user("Owner", "admin")
        member = setup.create_user("Member")
        config = ConfigService(setup, MASTER_KEY)
        publish_speech(config, admin)
        config.save_override(member, "speech", speech_route(
            model="old-model", base_url="https://old.example/v1",
            secret="old-key-sentinel"))
        run = reserve_run(setup, member)

    holder: dict = {}
    ready = threading.Event()
    issued: dict = {}

    def issuer():
        with Store.open(path) as store:
            pausing = PausingConfigService(store, MASTER_KEY)
            holder["config"] = pausing
            ready.set()
            issued["token"] = BrokerService(
                store, pausing, clock=lambda: 1000.0).issue(
                    member, run.id, "speech")

    def mutator():
        with Store.open(path) as store:
            config = ConfigService(store, MASTER_KEY)
            config.clear_override(member, "speech")
            config.save_override(member, "speech", speech_route(
                model="new-model", base_url="https://new.example/v1",
                secret="new-key-sentinel"))

    issuer_thread = threading.Thread(target=issuer)
    issuer_thread.start()
    assert ready.wait(2), "issuer 未就绪"
    pausing = holder["config"]
    # 旧实现没有一致快照入口，issue 不会在这里暂停
    assert pausing.snapshot_entered.wait(2), (
        "issue 未在一致事务内读取路由与凭证")

    mutator_thread = threading.Thread(target=mutator)
    mutator_thread.start()
    pausing.resume.set()
    issuer_thread.join(10)
    mutator_thread.join(10)
    assert not issuer_thread.is_alive(), "issue 未结束"
    assert not mutator_thread.is_alive(), "双连接更新未结束"

    with Store.open(path) as store:
        config = ConfigService(store, MASTER_KEY)
        record: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            record["url"] = str(request.url)
            record["authorization"] = request.headers.get("authorization")

            class Stream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b"ok"

                async def aclose(self):
                    pass

            return httpx.Response(200, stream=Stream())

        broker = BrokerService(
            store, config, clock=lambda: 1000.0,
            egress=EgressGateway(transport=httpx.MockTransport(handler)))
        old_route = broker.authorize(
            issued["token"], member.workspace_id, run.id, "speech")
        assert old_route.base_url == "https://old.example/v1"
        assert old_route.model == "old-model"
        assert config.credentials_for_ref(old_route.credential_ref)[
            "VOICE_API_KEY"] == "old-key-sentinel"

        async def call(token: str, model: str):
            async def body():
                yield json.dumps({"model": model}).encode()
            response = await broker.forward(
                token=token, workspace_id=member.workspace_id, run_id=run.id,
                channel="speech", method="POST",
                relative_path="audio/speech",
                headers={"content-type": "application/json"}, body=body())
            return b"".join([chunk async for chunk in response.chunks()])

        assert asyncio.run(call(issued["token"], "old-model")) == b"ok"
        assert record["url"] == "https://old.example/v1/audio/speech"
        assert record["authorization"] == "Bearer old-key-sentinel"

        new_token = broker.issue(member, run.id, "speech")
        new_route = broker.authorize(
            new_token, member.workspace_id, run.id, "speech")
        assert new_route.base_url == "https://new.example/v1"
        assert new_route.model == "new-model"
        assert config.credentials_for_ref(new_route.credential_ref)[
            "VOICE_API_KEY"] == "new-key-sentinel"
        assert asyncio.run(call(new_token, "new-model")) == b"ok"
        assert record["url"] == "https://new.example/v1/audio/speech"
        assert record["authorization"] == "Bearer new-key-sentinel"


def _stream_response() -> httpx.Response:
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"ok"

        async def aclose(self):
            pass

    return httpx.Response(200, stream=Stream())


def _forward_speech(broker: BrokerService, token: str, member, run) -> bytes:
    async def call() -> bytes:
        async def body():
            yield json.dumps({"model": "tts-default"}).encode()

        response = await broker.forward(
            token=token, workspace_id=member.workspace_id, run_id=run.id,
            channel="speech", method="POST", relative_path="audio/speech",
            headers={"content-type": "application/json"}, body=body())
        return b"".join([chunk async for chunk in response.chunks()])

    return asyncio.run(call())


def test_forward_retries_transient_connect_errors_before_response(tmp_path):
    path = tmp_path / "control.sqlite3"
    with Store.open(path) as setup:
        admin = setup.create_user("Owner", "admin")
        member = setup.create_user("Member")
        config = ConfigService(setup, MASTER_KEY)
        publish_speech(config, admin)
        run = reserve_run(setup, member)

    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 3:
            raise httpx.ConnectError("transient", request=request)
        return _stream_response()

    with Store.open(path) as store:
        config = ConfigService(store, MASTER_KEY)
        broker = BrokerService(
            store, config, clock=lambda: 1000.0,
            egress=EgressGateway(transport=httpx.MockTransport(handler)),
            upstream_retry_delays=(0.0, 0.0))
        token = broker.issue(member, run.id, "speech")
        assert _forward_speech(broker, token, member, run) == b"ok"

    assert calls["count"] == 3


def test_forward_gives_up_after_bounded_upstream_retries(tmp_path):
    path = tmp_path / "control.sqlite3"
    with Store.open(path) as setup:
        admin = setup.create_user("Owner", "admin")
        member = setup.create_user("Member")
        config = ConfigService(setup, MASTER_KEY)
        publish_speech(config, admin)
        run = reserve_run(setup, member)

    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise httpx.ConnectError("down", request=request)

    with Store.open(path) as store:
        config = ConfigService(store, MASTER_KEY)
        broker = BrokerService(
            store, config, clock=lambda: 1000.0,
            egress=EgressGateway(transport=httpx.MockTransport(handler)),
            upstream_retry_delays=(0.0, 0.0))
        token = broker.issue(member, run.id, "speech")
        with pytest.raises(DomainError) as error:
            _forward_speech(broker, token, member, run)
        assert_error(error, 502, "upstream_unavailable")

    assert calls["count"] == 3


@pytest.mark.parametrize("error_type", [
    httpx.ReadTimeout, httpx.ReadError, httpx.WriteError,
    httpx.RemoteProtocolError,
])
def test_forward_never_replays_post_after_possible_upstream_execution(tmp_path, error_type):
    """上游可能已接收任务时，不因响应头缺失而重复执行或计费。"""
    path = tmp_path / "control.sqlite3"
    with Store.open(path) as setup:
        admin = setup.create_user("Owner", "admin")
        member = setup.create_user("Member")
        config = ConfigService(setup, MASTER_KEY)
        publish_speech(config, admin)
        run = reserve_run(setup, member)

    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise error_type("possibly already executed", request=request)

    with Store.open(path) as store:
        broker = BrokerService(
            store, ConfigService(store, MASTER_KEY), clock=lambda: 1000.0,
            egress=EgressGateway(transport=httpx.MockTransport(handler)),
            upstream_retry_delays=(0.0, 0.0))
        token = broker.issue(member, run.id, "speech")
        with pytest.raises(DomainError) as error:
            _forward_speech(broker, token, member, run)
        assert_error(error, 502, "upstream_unavailable")
    assert calls["count"] == 1
