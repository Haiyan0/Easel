"""Task 10 Step 5：control CLI 的显式 relay 依赖装配。

边界：

* 只有同时显式给出受保护的签名私钥与受管 Agent 配置才构造 relay；
  两者都不给时保持任务入口 503，不伪造可执行能力。
* 只给其一、文件缺失、格式非法或夹带密钥字段时，在打开控制库和启动
  HTTP 之前失败关闭，错误输出不回显敏感值。
* 受管命令只来自该受保护文件，不从用户 HOME 或工作区 ACP 文件推断。
"""
from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from easel.cli import main
from easel.control.proxy import WorkerRelay
from easel.control.types import Identity

ORIGIN = "https://easel.test"


def write_master_key(tmp_path) -> object:
    path = tmp_path / "master.key"
    path.write_bytes(b"k" * 32)
    return path


def write_signing_key(tmp_path) -> tuple[object, Ed25519PrivateKey]:
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "snapshot-signing.pem"
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return path, key


def write_agent_config(tmp_path, payload: dict, name="managed-agent.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def capture_app(tmp_path, monkeypatch):
    from easel.control import cli as control_cli

    captured: dict = {}
    monkeypatch.setattr(
        control_cli.uvicorn, "run",
        lambda app, **kwargs: captured.setdefault("app", app))
    return captured


def test_serve_control_assembles_relay_only_from_explicit_files(
        tmp_path, monkeypatch):
    captured = capture_app(tmp_path, monkeypatch)
    master = write_master_key(tmp_path)
    signing, key = write_signing_key(tmp_path)
    agent_config = write_agent_config(tmp_path, {
        "revision": 4,
        "agent": {"runtime": "acp", "command": ["managed-agent", "--acp"],
                  "sandbox": "read-only", "timeout": 60},
        "broker": {"url": "https://control.internal", "channel": "chat"},
    })

    code = main(["serve-control",
                 "--control-dir", str(tmp_path / "control"),
                 "--public-origin", ORIGIN,
                 "--master-key-file", str(master),
                 "--snapshot-key-file", str(signing),
                 "--managed-agent-config", str(agent_config)])
    assert code == 0
    app = captured["app"]
    with TestClient(app, base_url=ORIGIN):
        relay = app.state.relay
        assert isinstance(relay, WorkerRelay)
        config = relay._config_provider(
            Identity("user-x", "workspace-x", "member"))
        assert config.revision == 4
        assert dict(config.agent) == {
            "runtime": "acp", "command": ["managed-agent", "--acp"],
            "sandbox": "read-only", "timeout": 60}
        # 生产装配不下发共享秘密；逐 Run token 在接纳后由 BrokerService 签发
        assert dict(config.routes) == {}
        assert config.broker.url == "https://control.internal"
        assert config.broker.channel == "chat"
        public = relay._signing_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw)
        assert public == key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def test_serve_control_without_relay_files_keeps_task_capability_503(
        tmp_path, monkeypatch):
    captured = capture_app(tmp_path, monkeypatch)
    master = write_master_key(tmp_path)

    code = main(["serve-control",
                 "--control-dir", str(tmp_path / "control"),
                 "--public-origin", ORIGIN,
                 "--master-key-file", str(master)])
    assert code == 0
    app = captured["app"]
    with TestClient(app, base_url=ORIGIN):
        assert app.state.relay is None


@pytest.mark.parametrize("missing", ["snapshot", "agent"])
def test_serve_control_rejects_half_configured_relay(
        tmp_path, monkeypatch, capsys, missing):
    captured = capture_app(tmp_path, monkeypatch)
    master = write_master_key(tmp_path)
    signing, _key = write_signing_key(tmp_path)
    agent_config = write_agent_config(tmp_path, {
        "revision": 1,
        "agent": {"runtime": "acp", "command": ["managed-agent"]},
        "broker": {"url": "https://control.internal", "channel": "chat"},
    })
    args = ["serve-control",
            "--control-dir", str(tmp_path / "control"),
            "--public-origin", ORIGIN,
            "--master-key-file", str(master)]
    if missing == "agent":
        args += ["--snapshot-key-file", str(signing)]
    else:
        args += ["--managed-agent-config", str(agent_config)]

    code = main(args)
    output = capsys.readouterr()
    assert code != 0
    assert "relay_config_incomplete" in output.err
    assert captured == {}
    assert not (tmp_path / "control").exists()


def test_serve_control_rejects_missing_signing_key_before_store(
        tmp_path, monkeypatch, capsys):
    captured = capture_app(tmp_path, monkeypatch)
    master = write_master_key(tmp_path)
    agent_config = write_agent_config(tmp_path, {
        "revision": 1,
        "agent": {"runtime": "acp", "command": ["managed-agent"]},
        "broker": {"url": "https://control.internal", "channel": "chat"},
    })

    code = main(["serve-control",
                 "--control-dir", str(tmp_path / "control"),
                 "--public-origin", ORIGIN,
                 "--master-key-file", str(master),
                 "--snapshot-key-file", str(tmp_path / "missing.pem"),
                 "--managed-agent-config", str(agent_config)])
    output = capsys.readouterr()
    assert code != 0
    assert "snapshot_key_unavailable" in output.err
    assert captured == {}
    assert not (tmp_path / "control").exists()


def test_formal_assembly_issues_working_run_scoped_broker_token(tmp_path):
    """正式 CLI factory + ConfigService/BrokerService/Store 的真实签发链。"""
    import asyncio

    import httpx

    from easel.control.auth import AuthService
    from easel.control.broker import BrokerService
    from easel.control.cli import build_relay_factory
    from easel.control.config import ConfigService, MASTER_KEY_BYTES
    from easel.control.egress import EgressGateway
    from easel.control.proxy import BrokerDestination
    from easel.control.sessions import SessionService
    from easel.control.store import Store
    from easel.control.types import DomainError, WorkerHandle

    key = Ed25519PrivateKey.generate()
    master = b"k" * MASTER_KEY_BYTES
    record: dict = {}

    def provider_handler(request: httpx.Request) -> httpx.Response:
        record["method"] = request.method
        record["url"] = str(request.url)
        record["calls"] = record.get("calls", 0) + 1
        return httpx.Response(200, json={"ok": True},
                              headers={"content-type": "application/json"})

    def chat_route(model: str = "deepseek-chat") -> dict:
        return {"provider": "deepseek", "model": model,
                "base_url": "https://chat.example/v1",
                "protocol": "openai",
                "secret": {"CHAT_API_KEY": "chat-secret-sentinel"},
                "options": {}}

    class CaptureWorker(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.posts = 0

        async def handle_async_request(self, request):
            if request.method == "POST":
                self.posts += 1
            return httpx.Response(
                202, json={"state": "accepted"}, request=request)

    class StaticRuntime:
        async def ensure_ready(self, workspace_id, config_version):
            return WorkerHandle(workspace_id, "container-formal",
                                "http://worker.test", "service-token",
                                config_version)

    async def body():
        yield b'{"model":"deepseek-chat","messages":[]}'

    async def scenario() -> None:
        with Store.open(tmp_path / "control.sqlite3") as store:
            admin = store.create_user("Owner", "admin")
            member = store.create_user("Member")
            other = store.create_user("Other")
            conversation = store.create_conversation(member, "正式")
            config = ConfigService(store, master)
            broker = BrokerService(
                store, config,
                egress=EgressGateway(
                    transport=httpx.MockTransport(provider_handler)))
            factory = build_relay_factory(
                signing_key=key, revision=3,
                agent={"runtime": "acp", "command": ["managed-agent"],
                       "sandbox": "read-only", "timeout": 60},
                broker_destination=BrokerDestination(
                    "https://control.internal", "chat"))
            transport = CaptureWorker()
            async with httpx.AsyncClient(transport=transport) as client:
                relay = factory(StaticRuntime(), SessionService(store),
                                AuthService(store), store, client, broker, None)
                request = {"sessionId": conversation.id,
                           "turnId": "turn-formal", "message": "你好"}

                # 未配置 chat 能力：明确 503，不以空 routes 接纳
                with pytest.raises(DomainError) as unavailable:
                    await relay.submit(member, dict(request))
                assert unavailable.value.code == "capability_unavailable"
                assert unavailable.value.status == 503
                assert transport.posts == 0

                config.publish(admin, {"defaults": {"chat": chat_route()}},
                               expected_version=0)
                admitted = await relay.submit(member, dict(request))
                run_id = admitted["runId"]
                assert transport.posts == 1
                snapshot_text = store.get_run_snapshot(member, run_id)
                snapshot = json.loads(snapshot_text)
                token = snapshot["routes"]["broker_token"]
                assert token
                assert snapshot["routes"]["broker_url"] == \
                    "https://control.internal"
                assert token not in json.dumps(admitted)
                # 逐 Run 签名包只含最小路由与 token，不含管理员 provider Key
                assert "chat-secret-sentinel" not in snapshot_text
                assert set(snapshot["routes"]) == {
                    "broker_url", "broker_token"}

                # 错 workspace/run/channel 一律拒绝
                for workspace, run, channel in (
                        (other.workspace_id, run_id, "chat"),
                        (member.workspace_id, "other-run", "chat"),
                        (member.workspace_id, run_id, "image")):
                    with pytest.raises(DomainError):
                        broker.authorize(token, workspace, run, channel)

                # 实际 token 可调用该 Run 批准的 broker 路由
                response = await broker.forward(
                    token=token, workspace_id=member.workspace_id,
                    run_id=run_id, channel="chat", method="POST",
                    relative_path="chat/completions",
                    headers={"content-type": "application/json"},
                    body=body())
                chunks = [chunk async for chunk in response.chunks()]
                assert b"ok" in b"".join(chunks)
                await response.aclose()
                assert record["method"] == "POST"
                assert record["url"] == \
                    "https://chat.example/v1/chat/completions"

                # 管理员更新配置 + 同请求重试：复用原签名包与 token
                config.publish(admin, {"defaults": {
                    "chat": chat_route("other-model")}},
                    expected_version=1)
                retried = await relay.submit(member, dict(request))
                assert retried["runId"] == run_id
                assert retried["duplicate"] is True
                assert store.get_run_snapshot(member, run_id) == snapshot_text
                assert transport.posts == 1

                # 真正终态后 token 立即失效
                store.finish_run(member, run_id, "done")
                broker.revoke_run(run_id, member.workspace_id)
                with pytest.raises(DomainError) as revoked:
                    broker.authorize(token, member.workspace_id,
                                     run_id, "chat")
                assert revoked.value.status in (401, 409)

    asyncio.run(scenario())


def test_serve_control_serves_explicit_frontend_dir(tmp_path, monkeypatch):
    captured = capture_app(tmp_path, monkeypatch)
    master = write_master_key(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>control-ui</html>",
                                     encoding="utf-8")

    code = main(["serve-control",
                 "--control-dir", str(tmp_path / "control"),
                 "--public-origin", ORIGIN,
                 "--master-key-file", str(master),
                 "--frontend-dir", str(dist)])
    assert code == 0
    app = captured["app"]
    with TestClient(app, base_url=ORIGIN) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "control-ui" in index.text
        assert client.get("/api/runtime").json() == {"mode": "control"}


def test_serve_control_rejects_missing_frontend_build(
        tmp_path, monkeypatch, capsys):
    captured = capture_app(tmp_path, monkeypatch)
    master = write_master_key(tmp_path)
    code = main(["serve-control",
                 "--control-dir", str(tmp_path / "control"),
                 "--public-origin", ORIGIN,
                 "--master-key-file", str(master),
                 "--frontend-dir", str(tmp_path / "missing")])
    output = capsys.readouterr()
    assert code != 0
    assert "frontend_build_missing" in output.err
    assert captured == {}
    assert not (tmp_path / "control").exists()


def test_serve_control_rejects_agent_config_with_secret_field(
        tmp_path, monkeypatch, capsys):
    captured = capture_app(tmp_path, monkeypatch)
    master = write_master_key(tmp_path)
    signing, _key = write_signing_key(tmp_path)
    sentinel = "admin-secret-sentinel"
    agent_config = write_agent_config(tmp_path, {
        "revision": 1,
        "agent": {"runtime": "acp", "command": ["managed-agent"],
                  "api_key": sentinel},
    })

    code = main(["serve-control",
                 "--control-dir", str(tmp_path / "control"),
                 "--public-origin", ORIGIN,
                 "--master-key-file", str(master),
                 "--snapshot-key-file", str(signing),
                 "--managed-agent-config", str(agent_config)])
    output = capsys.readouterr()
    assert code != 0
    assert "managed_config_invalid" in output.err
    assert sentinel not in output.err
    assert sentinel not in output.out
    assert captured == {}
    assert not (tmp_path / "control").exists()
