"""A4.3：受管 broker 目的地标签与逐 workspace sidecar 派生。"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from easel.control.auth import AuthService
from easel.control.cli import build_relay_factory
from easel.control.proxy import BrokerDestination
from easel.control.sessions import SessionService
from easel.control.snapshot import load_managed_agent_config
from easel.control.store import Store
from easel.control.types import WorkerHandle


def _config(tmp_path, broker: dict) -> object:
    path = tmp_path / "managed.json"
    path.write_text(json.dumps({
        "revision": 2,
        "agent": {"runtime": "acp", "command": ["managed", "acp"],
                  "sandbox": "read-only"},
        "broker": broker,
    }), encoding="utf-8")
    return path


def test_load_workspace_sidecar_broker_mode(tmp_path):
    revision, agent, broker = load_managed_agent_config(
        _config(tmp_path, {"mode": "workspace-sidecar", "channel": "chat"}))
    assert revision == 2
    assert agent["runtime"] == "acp"
    assert broker == {"mode": "workspace-sidecar", "channel": "chat"}


def test_load_legacy_fixed_url_remains_compatible(tmp_path):
    _revision, _agent, broker = load_managed_agent_config(
        _config(tmp_path, {"url": "https://broker.internal/",
                           "channel": "chat"}))
    assert broker == {"url": "https://broker.internal", "channel": "chat"}


@pytest.mark.parametrize("broker", [
    {"mode": "workspace-sidecar", "channel": "chat",
     "url": "https://evil.test"},
    {"mode": "workspace-sidecar", "channel": "Chat"},
    {"mode": "template", "channel": "chat"},
    {"mode": "workspace-sidecar", "channel": "chat", "host": "evil"},
    {"url": "https://broker.internal", "channel": "chat", "mode": "fixed"},
    {},
])
def test_load_rejects_ambiguous_broker_modes(tmp_path, broker):
    with pytest.raises(ValueError):
        load_managed_agent_config(_config(tmp_path, broker))


class _Runtime:
    async def ensure_ready(self, workspace_id, config_version):
        return WorkerHandle(
            workspace_id, "b" * 64, "http://worker.test", "token",
            config_version)


def test_factory_derives_distinct_sidecar_url_per_workspace(tmp_path):
    key = Ed25519PrivateKey.generate()
    factory = build_relay_factory(
        signing_key=key, revision=2,
        agent={"runtime": "acp", "command": ["managed", "acp"]},
        broker_config={"mode": "workspace-sidecar", "channel": "chat"})

    with Store.open(tmp_path / "db") as store:
        first = store.create_user("A")
        second = store.create_user("B")

        async def scenario() -> None:
            async with httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(200))) as client:
                relay = factory(
                    _Runtime(), SessionService(store), AuthService(store),
                    store, client, None, None)
                one = relay._config_provider(first)
                two = relay._config_provider(second)
                assert one.broker.url == (
                    f"http://easel-sidecar-{first.workspace_id}:8899")
                assert two.broker.url == (
                    f"http://easel-sidecar-{second.workspace_id}:8899")
                assert one.broker.url != two.broker.url
                assert one.broker.channel == "chat"

        asyncio.run(scenario())


def test_factory_rejects_ambiguous_broker_inputs(tmp_path):
    key = Ed25519PrivateKey.generate()
    with pytest.raises(ValueError):
        build_relay_factory(
            signing_key=key, revision=1,
            agent={"runtime": "acp", "command": ["managed"]},
            broker_config={"url": "https://broker.internal",
                           "channel": "chat"},
            broker_destination=BrokerDestination(
                "https://broker.internal", "chat"))


def test_factory_gateway_mode_builds_signed_transport(tmp_path):
    key = Ed25519PrivateKey.generate()
    factory = build_relay_factory(
        signing_key=key, revision=1,
        agent={"runtime": "acp", "command": ["managed"]},
        broker_config={"mode": "workspace-sidecar", "channel": "chat"},
        gateway_required=True)
    with Store.open(tmp_path / "db") as store:
        store.create_user("A")

        async def scenario() -> None:
            async with httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(200))) as client:
                relay = factory(
                    _Runtime(), SessionService(store), AuthService(store),
                    store, client, None, None)
                assert relay._gateway_required is True
                assert relay._gateway_client is not None
                assert relay._gateway_client._key is key

        asyncio.run(scenario())


def test_factory_gateway_mode_rejects_non_ed25519_key():
    with pytest.raises(TypeError):
        build_relay_factory(
            signing_key="not-a-key", revision=1,
            agent={"runtime": "acp", "command": ["managed"]},
            broker_config={"mode": "workspace-sidecar", "channel": "chat"},
            gateway_required=True)
