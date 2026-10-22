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
        # 生产装配未注入任何共享秘密路由；逐 Run token 由后续受管路由提供
        assert dict(config.routes) == {}
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
