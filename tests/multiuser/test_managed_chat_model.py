"""Task 2：受管 chat 模型从有效路由到 ACP 选择的单链路验收。

边界：

* 管理员 chat 路由的 model/allowed_models 必须与 broker token 快照、签名
  Agent 模型来自同一份固定路由；
* 真实 Harness 的 modelId 是 ``JSON.stringify([provider, model])``，适配器
  必须按真实 session/new 目录匹配，不能臆造；
* 受管模型目录必须显式配置；空目录/未知模型在接纳前拒绝；
* 不读取真实 Key、不调用真实供应商、不触碰用户运行实例。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.control.auth import AuthService
from easel.control.broker import BrokerService
from easel.control.config import MASTER_KEY_BYTES, ConfigService
from easel.control.proxy import BrokerDestination, PinnedRunConfig, WorkerRelay
from easel.control.sessions import SessionService
from easel.control.snapshot import load_managed_agent_config
from easel.control.store import Store
from easel.control.types import DomainError, WorkerHandle
from easel.harnesses.acp import ACPAdapter
from easel.harnesses.base import HarnessError, RunRequest
from tests.multiuser.test_runs import (
    NeverExecutor,
    RecordingExecutor,
    make_manager,
    make_paths,
    request_for,
    sign,
    wait_until,
)

MASTER_KEY = b"k" * MASTER_KEY_BYTES
REVISION = 7
CATALOG = ("deepseek-chat", "deepseek-flash", "deepseek-v4-pro")
ADMIN_SECRET = "sk-task2-fake-admin-sentinel"


def chat_route(model: str, allowed: tuple[str, ...] = ()) -> dict:
    return {
        "provider": "deepseek",
        "model": model,
        "base_url": "https://chat.example/v1",
        "protocol": "openai",
        "secret": {"CHAT_API_KEY": ADMIN_SECRET},
        "options": {},
        "allowed_models": list(allowed or (model,)),
    }


def publish_chat(config: ConfigService, admin, model: str,
                 allowed: tuple[str, ...] = ()) -> int:
    return config.publish(
        admin, {"defaults": {"chat": chat_route(model, allowed)}},
        expected_version=0)


class StaticRuntime:
    async def ensure_ready(self, workspace_id: str,
                           config_version: int) -> WorkerHandle:
        return WorkerHandle(workspace_id, "container-" + workspace_id,
                            "http://worker.test",
                            "service-token-" + workspace_id, config_version)

    async def bound_handle(self, workspace_id, container_id,
                           config_version):
        return await self.ensure_ready(workspace_id, config_version)


class DispatchCapture(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.posts: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        if request.method == "POST" and "/internal/runs/" in request.url.path:
            self.posts.append({
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": json.loads(body),
            })
            return httpx.Response(
                202, json={"state": "accepted"}, request=request)
        return httpx.Response(200, json={}, request=request)


def make_agent(catalog=CATALOG) -> dict:
    return {
        "runtime": "acp",
        "command": ["managed-agent", "--acp"],
        "sandbox": "read-only",
        "timeout": 60,
        "models": list(catalog),
    }


def make_relay(store: Store, key: Ed25519PrivateKey, capture,
               broker: BrokerService | None = None) -> WorkerRelay:
    def provider(_actor):
        return PinnedRunConfig(
            revision=REVISION, routes={}, agent=make_agent(),
            broker=BrokerDestination("http://broker.internal", "chat"))

    return WorkerRelay(
        StaticRuntime(), SessionService(store), AuthService(store),
        store=store, signing_key=key, config_provider=provider,
        http_client=httpx.AsyncClient(transport=capture),
        broker=broker, clock=lambda: 1000.0,
        system_clock=lambda: 1000.0)


def test_signed_agent_uses_effective_chat_route_model(tmp_path):
    """签名 Agent 的 model/allowed_models 必须与 broker 路由快照一致。"""
    database = tmp_path / "control.sqlite3"
    with Store.open(database) as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        conversation = store.create_conversation(member, "模型链路")
        config = ConfigService(store, MASTER_KEY)
        publish_chat(config, admin, "deepseek-flash",
                     ("deepseek-flash", "deepseek-v4-pro"))
        broker = BrokerService(store, config, clock=lambda: 1000.0)
        capture = DispatchCapture()
        key = Ed25519PrivateKey.generate()

        async def scenario() -> dict:
            relay = make_relay(store, key, capture, broker)
            try:
                return await relay.submit(member, {
                    "sessionId": conversation.id,
                    "turnId": "turn-model-1",
                    "message": "使用管理员模型",
                })
            finally:
                await relay._http.aclose()

        result = asyncio.run(scenario())
        assert result["runId"]
        assert len(capture.posts) == 1
        envelope = capture.posts[0]["body"]["envelope"]
        agent = envelope["agent"]
        assert agent["model"] == "deepseek-flash"
        assert agent["allowed_models"] == [
            "deepseek-flash", "deepseek-v4-pro"]
        assert agent["models"] == list(CATALOG)
        assert ADMIN_SECRET not in json.dumps(envelope, ensure_ascii=False)

        route = broker.authorize(
            envelope["routes"]["broker_token"], member.workspace_id,
            result["runId"], "chat")
        assert route.model == "deepseek-flash"
        assert route.allowed_models == (
            "deepseek-flash", "deepseek-v4-pro")
        assert route.version == 1
        assert route.provider == "deepseek"


def test_chat_publish_rejects_model_outside_trusted_catalog(tmp_path):
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        config = ConfigService(store, MASTER_KEY, model_catalog=CATALOG)
        with pytest.raises(DomainError) as error:
            publish_chat(config, admin, "deepseek-unknown")
        assert error.value.code == "model_not_allowed"
        assert error.value.status == 409


def test_chat_publish_rejects_allowed_model_outside_trusted_catalog(tmp_path):
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        config = ConfigService(store, MASTER_KEY, model_catalog=CATALOG)
        with pytest.raises(DomainError) as error:
            publish_chat(config, admin, "deepseek-flash",
                         ("deepseek-flash", "deepseek-unknown"))
        assert error.value.code == "model_not_allowed"
        assert error.value.status == 409


def test_chat_publish_rejects_empty_trusted_catalog(tmp_path):
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        config = ConfigService(store, MASTER_KEY, model_catalog=())
        with pytest.raises(DomainError) as error:
            publish_chat(config, admin, "deepseek-flash")
        assert error.value.code == "model_catalog_unavailable"
        assert error.value.status == 503


def test_public_settings_filters_chat_models_to_trusted_catalog(tmp_path):
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        config = ConfigService(
            store, MASTER_KEY,
            model_catalog=("deepseek-flash", "deepseek-v4-pro"))
        publish_chat(config, admin, "deepseek-flash",
                     ("deepseek-flash", "deepseek-v4-pro"))
        narrowed = ConfigService(store, MASTER_KEY,
                                 model_catalog=("deepseek-flash",))
        public = narrowed.public_settings(member)["channels"]["chat"]
        assert public["allowed_models"] == ["deepseek-flash"]
        assert public["model_catalog"] == ["deepseek-flash"]


def test_broker_admission_rejects_route_outside_trusted_catalog(tmp_path):
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        config = ConfigService(store, MASTER_KEY, model_catalog=CATALOG)
        publish_chat(config, admin, "deepseek-flash",
                     ("deepseek-flash",))
        broker = BrokerService(
            store, config, clock=lambda: 1000.0,
            model_catalog=("deepseek-chat",))
        with pytest.raises(DomainError) as error:
            broker.ensure_issuable(member, "chat")
        assert error.value.code == "model_not_allowed"
        assert error.value.status == 409


def test_broker_admission_rejects_empty_trusted_catalog(tmp_path):
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        config = ConfigService(store, MASTER_KEY, model_catalog=CATALOG)
        publish_chat(config, admin, "deepseek-flash")
        broker = BrokerService(
            store, config, clock=lambda: 1000.0, model_catalog=())
        with pytest.raises(DomainError) as error:
            broker.ensure_issuable(member, "chat")
        assert error.value.code == "model_catalog_unavailable"
        assert error.value.status == 503


def test_broker_issue_rejects_route_outside_trusted_catalog(tmp_path):
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        conversation = store.create_conversation(member, "模型拒绝")
        config = ConfigService(store, MASTER_KEY, model_catalog=CATALOG)
        publish_chat(config, admin, "deepseek-flash")
        run = store.reserve_run(member, conversation.id, "req-model", "d", 1)
        broker = BrokerService(
            store, config, clock=lambda: 1000.0,
            model_catalog=("deepseek-chat",))
        with pytest.raises(DomainError) as error:
            broker.issue_with_route(member, run.id, "chat")
        assert error.value.code == "model_not_allowed"
        assert error.value.status == 409
        row = store._connection.execute(
            "SELECT COUNT(*) FROM broker_tokens").fetchone()
        assert row[0] == 0, "越界模型不能留下 broker token 快照"


def test_managed_agent_config_accepts_bounded_model_catalog(tmp_path):
    path = tmp_path / "managed.json"
    path.write_text(json.dumps({
        "revision": 2,
        "agent": {"runtime": "acp", "command": ["managed", "acp"],
                  "models": list(CATALOG)},
        "broker": {"mode": "workspace-sidecar", "channel": "chat"},
    }), encoding="utf-8")
    revision, agent, broker = load_managed_agent_config(path)
    assert revision == 2
    assert agent["models"] == list(CATALOG)
    assert broker == {"mode": "workspace-sidecar", "channel": "chat"}


@pytest.mark.parametrize("models", [
    [], ["deepseek-chat", "deepseek-chat"], ["deepseek-chat", 1],
    ["bad\nmodel"], "not-a-list",
])
def test_managed_agent_config_rejects_invalid_model_catalog(tmp_path, models):
    path = tmp_path / "managed.json"
    path.write_text(json.dumps({
        "revision": 2,
        "agent": {"runtime": "acp", "command": ["managed", "acp"],
                  "models": models},
        "broker": {"mode": "workspace-sidecar", "channel": "chat"},
    }), encoding="utf-8")
    with pytest.raises(ValueError):
        load_managed_agent_config(path)


class PausingConfigService(ConfigService):
    def __init__(self, store: Store, master_key: bytes, *,
                 model_catalog):
        super().__init__(store, master_key, model_catalog=model_catalog)
        self.entered = threading.Event()
        self.resume = threading.Event()

    def snapshot_from_rows(self, workspace_id, channel, managed_row,
                           override_row):
        route, bundle = super().snapshot_from_rows(
            workspace_id, channel, managed_row, override_row)
        self.entered.set()
        if not self.resume.wait(5):
            raise RuntimeError("test resume timeout")
        return route, bundle


def test_issue_route_and_agent_model_are_fixed_together(tmp_path):
    path = tmp_path / "control.sqlite3"
    with Store.open(path) as setup:
        admin = setup.create_user("Owner", "admin")
        member = setup.create_user("Member")
        conversation = setup.create_conversation(member, "交错发布")
        config = ConfigService(setup, MASTER_KEY, model_catalog=CATALOG)
        publish_chat(config, admin, "deepseek-flash",
                     ("deepseek-flash",))
        run = setup.reserve_run(member, conversation.id, "req-race", "d", 1)

    holder: dict = {}
    ready = threading.Event()
    issued: dict = {}

    def issuer() -> None:
        with Store.open(path) as store:
            pausing = PausingConfigService(
                store, MASTER_KEY, model_catalog=CATALOG)
            holder["config"] = pausing
            ready.set()
            issued["result"] = BrokerService(
                store, pausing, clock=lambda: 1000.0,
                model_catalog=CATALOG).issue_with_route(
                    member, run.id, "chat")

    def publisher() -> None:
        with Store.open(path) as store:
            config = ConfigService(store, MASTER_KEY, model_catalog=CATALOG)
            config.publish(
                admin, {"defaults": {"chat": chat_route(
                    "deepseek-v4-pro", ("deepseek-v4-pro",))}},
                expected_version=1)

    issuer_thread = threading.Thread(target=issuer)
    issuer_thread.start()
    assert ready.wait(2), "issuer 未就绪"
    pausing = holder["config"]
    assert pausing.entered.wait(2), "issue 未在写事务内读取路由"
    publisher_thread = threading.Thread(target=publisher)
    publisher_thread.start()
    pausing.resume.set()
    issuer_thread.join(10)
    publisher_thread.join(10)
    assert not issuer_thread.is_alive()
    assert not publisher_thread.is_alive()

    result = issued["result"]
    assert result.route.model == "deepseek-flash"
    assert result.route.version == 1
    with Store.open(path) as verify:
        row = verify._connection.execute(
            "SELECT route_json, config_version FROM broker_tokens"
            " WHERE run_id = ?", (run.id,)).fetchone()
    assert row is not None
    route = json.loads(row["route_json"])
    assert route["model"] == "deepseek-flash"
    assert route["version"] == 1
    assert row["config_version"] == 1


FIXTURE = (Path(__file__).resolve().parents[1]
           / "fixtures" / "harness_diagnostics.py")


def _adapter_request(tmp_path, mode: str, model: str, *,
                     native_id: str = "") -> RunRequest:
    command = [sys.executable, str(FIXTURE), "--mode", mode,
               "--audit", str(tmp_path / "adapter-audit.jsonl")]
    return RunRequest("hello", tmp_path, dict(os.environ), native_id,
                      model, "read-only", 20, tuple(command))


async def _reject(_questions):
    raise AssertionError("本测试不应请求权限")


def _audit(tmp_path) -> list[dict]:
    path = tmp_path / "adapter-audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines()]


def test_acp_selects_exact_harness_model_value(tmp_path):
    events: list[tuple[str, str]] = []
    request = _adapter_request(tmp_path, "model-catalog", "deepseek-flash")

    async def scenario():
        await ACPAdapter().run(request, lambda kind, text:
                               events.append((kind, text)), _reject,
                               lambda identifier: None)

    asyncio.run(scenario())
    assert ("token", "你好") in events
    sets = [line for line in _audit(tmp_path)
            if line.get("method") == "fixture/set-config"]
    assert sets == [{"method": "fixture/set-config",
                     "value": '["easel-broker","deepseek-flash"]'}]


def test_acp_rejects_model_missing_from_harness_catalog(tmp_path):
    request = _adapter_request(tmp_path, "model-catalog",
                               "deepseek-unknown")
    with pytest.raises(HarnessError) as caught:
        asyncio.run(ACPAdapter().run(
            request, lambda *args: None, _reject, lambda identifier: None))
    assert getattr(caught.value, "stage", None) == "model"
    assert getattr(caught.value, "category", "unknown") in (None, "unknown")
    methods = [line.get("method") for line in _audit(tmp_path)]
    assert "fixture/set-config" not in methods, (
        "不在真实目录中的模型不应先发给 Harness")
    assert "session/prompt" not in methods


def test_acp_resume_selects_exact_harness_model_value(tmp_path):
    bound: list[str] = []
    first = _adapter_request(tmp_path, "model-catalog", "deepseek-flash")

    async def first_run():
        await ACPAdapter().run(first, lambda *args: None, _reject,
                               bound.append)

    asyncio.run(first_run())
    assert bound == ["diag-session"]
    second = _adapter_request(tmp_path, "model-catalog", "deepseek-v4-pro",
                              native_id=bound[0])

    async def second_run():
        await ACPAdapter().run(second, lambda *args: None, _reject,
                               bound.append)

    asyncio.run(second_run())
    sets = [line for line in _audit(tmp_path)
            if line.get("method") == "fixture/set-config"]
    assert sets == [
        {"method": "fixture/set-config",
         "value": '["easel-broker","deepseek-flash"]'},
        {"method": "fixture/set-config",
         "value": '["easel-broker","deepseek-v4-pro"]'},
    ]


def _signed_agent(**overrides) -> dict:
    agent = {
        "runtime": "acp", "command": ["fixture-acp"],
        "sandbox": "read-only", "timeout": 60,
        "model": "deepseek-flash",
        "models": ["deepseek-chat", "deepseek-flash", "deepseek-v4-pro"],
        "allowed_models": ["deepseek-flash", "deepseek-v4-pro"],
    }
    agent.update(overrides)
    return agent


@pytest.mark.parametrize("agent", [
    _signed_agent(model="deepseek-unknown"),
    _signed_agent(models=["deepseek-chat"]),
    _signed_agent(allowed_models=["deepseek-unknown"]),
    _signed_agent(allowed_models=["deepseek-chat"]),
    _signed_agent(models=[]),
    _signed_agent(models="not-a-list"),
    _signed_agent(models=["deepseek-chat", "deepseek-chat"]),
    _signed_agent(allowed_models=["deepseek-flash", 1]),
])
def test_worker_rejects_invalid_signed_model_metadata(tmp_path, agent):
    paths = make_paths(tmp_path)
    key = Ed25519PrivateKey.generate()
    executor = NeverExecutor()
    manager = make_manager(paths, key, executor)
    envelope = sign(key, run_id="run-bad-model", session_id="conv-bad-model",
                    message="x", routes={}, agent=agent)

    async def scenario():
        try:
            with pytest.raises(DomainError) as error:
                await manager.submit(
                    "run-bad-model", envelope,
                    request_for("conv-bad-model", "x", envelope))
            assert error.value.status == 400
            assert error.value.code == "agent_config_invalid"
            assert executor.calls == 0, "非法模型元数据不得进入适配器"
        finally:
            await manager.aclose()

    asyncio.run(scenario())


def test_worker_consumes_signed_model_metadata_inside_catalog(tmp_path):
    paths = make_paths(tmp_path)
    key = Ed25519PrivateKey.generate()
    executor = RecordingExecutor()
    manager = make_manager(paths, key, executor)
    envelope = sign(key, run_id="run-good-model", session_id="conv-good-model",
                    message="x", routes={}, agent=_signed_agent())

    async def scenario():
        try:
            await manager.submit(
                "run-good-model", envelope,
                request_for("conv-good-model", "x", envelope))
            await wait_until(
                lambda: manager.state("run-good-model") == "done", timeout=10)
            assert executor.calls
            request, _context = executor.calls[0]
            assert request.model == "deepseek-flash"
        finally:
            await manager.aclose()

    asyncio.run(scenario())
