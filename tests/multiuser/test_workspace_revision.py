"""T5A-R1-01：文件/status 与聊天使用同一受管配置 revision。"""
from __future__ import annotations

import asyncio
import json

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.control.auth import AuthService
from easel.control.cli import build_relay_factory, build_workspace_factory
from easel.control.runtime import RuntimeManager
from easel.control.sessions import SessionService
from easel.control.snapshot import load_managed_agent_config
from easel.control.store import Store
from easel.control.types import DomainError, Identity
from tests.multiuser.test_runtime_gateway import GatewayFake

REVISION = 7


def _managed_agent(root):
    path = root / "managed-agent.json"
    path.write_text(json.dumps({
        "revision": REVISION,
        "agent": {
            "runtime": "acp", "command": ["fixture-agent", "--acp"],
            "models": ["deepseek-flash"], "sandbox": "read-only",
        },
        "broker": {"mode": "workspace-sidecar", "channel": "chat"},
    }), encoding="utf-8")
    return load_managed_agent_config(path)


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/status":
        return httpx.Response(200, json={
            "gateway": True, "transport": "control", "runtime": "control",
            "skills": [], "personas": [],
        }, request=request)
    return httpx.Response(200, json=[], request=request)


def _factories(root, store):
    revision, agent, broker_config = _managed_agent(root)
    key = Ed25519PrivateKey.generate()
    relay_factory = build_relay_factory(
        signing_key=key, revision=revision, agent=agent,
        broker_config=broker_config, gateway_required=True)
    workspace_factory = build_workspace_factory(
        signing_key=key, revision=revision, gateway_required=True)
    return revision, relay_factory, workspace_factory


def test_file_request_uses_managed_revision_during_and_after_live_run(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "control.sqlite3") as store:
            revision, relay_factory, workspace_factory = _factories(
                tmp_path, store)
            actor = store.create_user("revision-member")
            provisioner = GatewayFake(store)
            runtime = RuntimeManager(
                store, provisioner, gateway_required=True)
            async with httpx.AsyncClient(
                    transport=httpx.MockTransport(_handler),
                    trust_env=False) as client:
                relay = relay_factory(
                    runtime, SessionService(store), AuthService(store),
                    store, client, None, None)
                pinned = relay._config_provider(actor)
                api = workspace_factory(runtime, store, client, None)
                assert revision == pinned.revision == 7
                await runtime.ensure_ready(actor.workspace_id, pinned.revision)
                conversation = store.create_conversation(actor)
                run = store.reserve_run(
                    actor, conversation.id, "revision-request",
                    "revision-digest", pinned.revision)
                store.mark_running(actor, run.id)

                response = await api.request(
                    actor, "GET", "/api/personas")
                assert response.json() == []
                status = await api.status(actor)
                assert status["state"] == "ready"
                lease = store.get_worker_lease(actor.workspace_id)
                assert lease.config_version == 7
                assert provisioner.stop_calls == []

                store.finish_run(actor, run.id, "done")
                after = await api.request(actor, "GET", "/api/personas")
                assert after.json() == []
                lease = store.get_worker_lease(actor.workspace_id)
                assert lease.config_version == 7
                assert provisioner.stop_calls == []

    asyncio.run(scenario())


def test_file_first_then_chat_never_switches_revision(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "control.sqlite3") as store:
            revision, relay_factory, workspace_factory = _factories(
                tmp_path, store)
            actor = store.create_user("file-first")
            provisioner = GatewayFake(store)
            runtime = RuntimeManager(
                store, provisioner, gateway_required=True)
            async with httpx.AsyncClient(
                    transport=httpx.MockTransport(_handler),
                    trust_env=False) as client:
                relay = relay_factory(
                    runtime, SessionService(store), AuthService(store),
                    store, client, None, None)
                pinned = relay._config_provider(actor)
                api = workspace_factory(runtime, store, client, None)
                await api.request(actor, "GET", "/api/outputs")
                lease = store.get_worker_lease(actor.workspace_id)
                assert lease.config_version == pinned.revision == 7

                handle = await runtime.ensure_ready(
                    actor.workspace_id, pinned.revision)
                assert handle.config_version == 7
                lease = store.get_worker_lease(actor.workspace_id)
                assert lease.config_version == 7
                assert provisioner.stop_calls == []

    asyncio.run(scenario())


def test_unknown_workspace_still_rejected_with_managed_revision(tmp_path):
    async def scenario():
        with Store.open(tmp_path / "control.sqlite3") as store:
            _revision, _relay, workspace_factory = _factories(tmp_path, store)
            provisioner = GatewayFake(store)
            runtime = RuntimeManager(
                store, provisioner, gateway_required=True)
            async with httpx.AsyncClient(
                    transport=httpx.MockTransport(_handler),
                    trust_env=False) as client:
                api = workspace_factory(runtime, store, client, None)
                actor = Identity("missing-user", "a" * 32, "member")
                try:
                    await api.request(actor, "GET", "/api/personas")
                except DomainError as error:
                    assert error.status == 404
                else:
                    raise AssertionError("unknown workspace must be rejected")

    asyncio.run(scenario())
