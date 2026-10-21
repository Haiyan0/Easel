"""Task 2 本地 CLI：管理员引导、邀请与服务启动装配。"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from easel.cli import main
from easel.control.auth import AuthService
from easel.control.config import ConfigService
from easel.control.store import Store
from easel.control.types import DomainError


def write_key(tmp_path, name="master.key", data=b"k" * 32):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def parse_output(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def test_bootstrap_creates_one_admin_and_prints_invite_once(tmp_path, capsys):
    control_dir = tmp_path / "control"
    code = main(["admin", "bootstrap", "--control-dir", str(control_dir),
                 "--name", "Owner"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    values = parse_output(captured.out)
    assert values["admin_user_id"]
    assert values["workspace_id"]
    invite = values["invite"]

    db_path = control_dir / "control.sqlite3"
    database_bytes = db_path.read_bytes()
    wal = db_path.with_name(db_path.name + "-wal")
    if wal.exists():
        database_bytes += wal.read_bytes()
    assert invite.encode() not in database_bytes, "数据库不得保存邀请明文"

    with Store.open(db_path) as store:
        assert store._connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0] == 1
        admin = store.get_identity(values["admin_user_id"])
        login = AuthService(store).exchange(invite)
        assert AuthService(store).authenticate(login.token) == admin

    repeated = main(["admin", "bootstrap", "--control-dir", str(control_dir),
                     "--name", "Second"])
    repeated_output = capsys.readouterr()
    assert repeated != 0
    assert "admin_exists" in repeated_output.err
    assert "invite=" not in repeated_output.out
    with Store.open(db_path) as store:
        assert store._connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0] == 1


def test_admin_invite_requires_real_admin_and_rejects_invalid_ttl(tmp_path, capsys):
    control_dir = tmp_path / "control"
    assert main(["admin", "bootstrap", "--control-dir", str(control_dir),
                 "--name", "Owner"]) == 0
    admin_id = parse_output(capsys.readouterr().out)["admin_user_id"]

    with Store.open(control_dir / "control.sqlite3") as store:
        member = store.create_user("Member")

    code = main(["admin", "invite", "--control-dir", str(control_dir),
                 "--admin-user-id", admin_id, "--user-id", member.user_id])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    invite = parse_output(captured.out)["invite"]

    with Store.open(control_dir / "control.sqlite3") as store:
        session = AuthService(store).exchange(invite)
        assert AuthService(store).authenticate(session.token) == member

    # 普通用户不能签发邀请：CLI 回数据库复查角色
    denied = main(["admin", "invite", "--control-dir", str(control_dir),
                   "--admin-user-id", member.user_id, "--user-id", member.user_id])
    denied_output = capsys.readouterr()
    assert denied != 0
    assert "forbidden" in denied_output.err
    assert "invite=" not in denied_output.out

    # 非法 TTL 明确拒绝
    invalid_ttl = main(["admin", "invite", "--control-dir", str(control_dir),
                        "--admin-user-id", admin_id, "--user-id", member.user_id,
                        "--ttl-seconds", "9999999"])
    invalid_output = capsys.readouterr()
    assert invalid_ttl != 0
    assert "invalid_ttl" in invalid_output.err
    assert "invite=" not in invalid_output.out


def test_serve_control_wires_app_and_public_origin(tmp_path, monkeypatch):
    from easel.control import cli as control_cli

    captured = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(control_cli.uvicorn, "run", fake_run)
    key_file = write_key(tmp_path)

    code = main(["serve-control",
                 "--control-dir", str(tmp_path / "control"),
                 "--public-origin", "https://easel.test/",
                 "--master-key-file", str(key_file),
                 "--host", "127.0.0.1", "--port", "9999"])
    assert code == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9999
    assert captured["proxy_headers"] is False, "不得默认信任转发头中的来源地址"

    app = captured["app"]
    with TestClient(app, base_url="https://easel.test") as client:
        assert client.get("/api/health").status_code == 200
        assert app.state.public_origin == "https://easel.test"
        assert app.state.runtime is None, "未配置 provisioner 时不得伪造运行时"


def test_serve_control_refuses_missing_provisioner_socket_before_serving(
        tmp_path, monkeypatch, capsys):
    """受限 provisioner 不可用时失败关闭，不回退到无隔离运行时。"""
    from easel.control import cli as control_cli

    started = []
    monkeypatch.setattr(control_cli.uvicorn, "run",
                        lambda *args, **kwargs: started.append(True))
    key_file = write_key(tmp_path)

    code = main(["serve-control",
                 "--control-dir", str(tmp_path / "control"),
                 "--public-origin", "https://easel.test",
                 "--master-key-file", str(key_file),
                 "--provisioner-socket", str(tmp_path / "missing.sock")])
    captured = capsys.readouterr()
    assert code != 0
    assert "provisioner_unavailable" in captured.err
    assert started == []
    assert not (tmp_path / "control").exists()


def test_serve_control_rejects_invalid_origin_before_opening_store(
        tmp_path, monkeypatch, capsys):
    from easel.control import cli as control_cli

    started = []
    monkeypatch.setattr(control_cli.uvicorn, "run",
                        lambda *args, **kwargs: started.append(True))
    key_file = write_key(tmp_path)

    control_dir = tmp_path / "control"
    code = main(["serve-control", "--control-dir", str(control_dir),
                 "--public-origin", "http://easel.test",
                 "--master-key-file", str(key_file)])
    captured = capsys.readouterr()

    assert code != 0
    assert "invalid_origin" in captured.err
    assert started == [], "非法 Origin 不得启动 HTTP 服务"
    assert not control_dir.exists(), "非法配置不得创建或打开控制库"


def test_serve_control_rejects_missing_or_malformed_master_key(
        tmp_path, monkeypatch, capsys):
    from easel.control import cli as control_cli

    started = []
    monkeypatch.setattr(control_cli.uvicorn, "run",
                        lambda *args, **kwargs: started.append(True))

    missing = main(["serve-control",
                    "--control-dir", str(tmp_path / "missing-control"),
                    "--public-origin", "https://easel.test",
                    "--master-key-file", str(tmp_path / "no.key")])
    missing_err = capsys.readouterr().err
    assert missing != 0
    assert "master_key_unavailable" in missing_err
    assert not (tmp_path / "missing-control").exists()

    short_key = write_key(tmp_path, "short.key", b"k" * 31)
    malformed = main(["serve-control",
                      "--control-dir", str(tmp_path / "short-control"),
                      "--public-origin", "https://easel.test",
                      "--master-key-file", str(short_key)])
    malformed_err = capsys.readouterr().err
    assert malformed != 0
    assert "invalid_master_key" in malformed_err
    assert not (tmp_path / "short-control").exists()
    assert started == []


def test_serve_control_rejects_key_mismatching_existing_ciphertext(
        tmp_path, monkeypatch, capsys):
    from easel.control import cli as control_cli

    started = []
    monkeypatch.setattr(control_cli.uvicorn, "run",
                        lambda *args, **kwargs: started.append(True))

    control_dir = tmp_path / "control"
    with Store.open(control_dir / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        ConfigService(store, b"a" * 32).publish(admin, {"defaults": {"speech": {
            "provider": "openai-compatible",
            "model": "tts-default",
            "base_url": "https://provider.example/v1",
            "protocol": "openai",
            "secret": "admin-secret-sentinel",
            "options": {},
        }}}, expected_version=0)

    wrong_key = write_key(tmp_path, "wrong.key", b"b" * 32)
    code = main(["serve-control", "--control-dir", str(control_dir),
                 "--public-origin", "https://easel.test",
                 "--master-key-file", str(wrong_key)])
    captured = capsys.readouterr()

    assert code != 0
    assert "invalid_master_key" in captured.err
    assert "admin-secret-sentinel" not in captured.err
    assert started == []


# --------------------------------------------------------------------------
# T5-R1-04：生产出口代理与策略必须显式装配
# --------------------------------------------------------------------------

def _serve_control_with_egress(tmp_path, monkeypatch, *extra_args):
    from easel.control import cli as control_cli

    captured: dict = {}
    monkeypatch.setattr(control_cli.uvicorn, "run",
                        lambda app, **kwargs: captured.setdefault("app", app))
    key_file = write_key(tmp_path)
    code = main(["serve-control",
                 "--control-dir", str(tmp_path / "control"),
                 "--public-origin", "https://easel.test",
                 "--master-key-file", str(key_file),
                 *extra_args])
    return code, captured.get("app")


def test_serve_control_proxy_without_policy_fails_closed(
        tmp_path, monkeypatch, capsys):
    code, app = _serve_control_with_egress(
        tmp_path, monkeypatch,
        "--egress-proxy", "http://127.0.0.1:8899")
    assert code == 0, capsys.readouterr().err
    with TestClient(app, base_url="https://easel.test"):
        egress = app.state.broker.egress
        assert egress.configured is False
        with pytest.raises(DomainError) as unavailable:
            egress.client()
        assert unavailable.value.status == 503
        assert unavailable.value.code == "egress_unavailable"


def test_serve_control_policy_without_proxy_fails_closed(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    code, app = _serve_control_with_egress(
        tmp_path, monkeypatch, "--egress-policy", str(policy))
    assert code == 0
    with TestClient(app, base_url="https://easel.test"):
        egress = app.state.broker.egress
        assert egress.configured is False
        with pytest.raises(DomainError) as unavailable:
            egress.client()
        assert unavailable.value.status == 503


def test_serve_control_valid_proxy_and_policy_creates_client(
        tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    code, app = _serve_control_with_egress(
        tmp_path, monkeypatch,
        "--egress-proxy", "http://127.0.0.1:8899",
        "--egress-policy", str(policy))
    assert code == 0
    with TestClient(app, base_url="https://easel.test"):
        egress = app.state.broker.egress
        assert egress.configured is True
        client = egress.client()
        assert client is not None
        asyncio.run(client.aclose())


def test_serve_control_invalid_egress_policy_fails_before_start(
        tmp_path, monkeypatch, capsys):
    from easel.control import cli as control_cli

    started = []
    monkeypatch.setattr(control_cli.uvicorn, "run",
                        lambda *args, **kwargs: started.append(True))
    key_file = write_key(tmp_path)
    missing = tmp_path / "missing-policy.json"
    code = main(["serve-control",
                 "--control-dir", str(tmp_path / "control"),
                 "--public-origin", "https://easel.test",
                 "--master-key-file", str(key_file),
                 "--egress-proxy", "http://127.0.0.1:8899",
                 "--egress-policy", str(missing)])
    captured = capsys.readouterr()
    assert code != 0
    assert "egress_policy_invalid" in captured.err
    assert started == []
    assert not (tmp_path / "control").exists()
