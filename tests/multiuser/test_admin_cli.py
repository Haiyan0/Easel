"""Task 2 本地 CLI：管理员引导、邀请与服务启动装配。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from easel.cli import main
from easel.control.auth import AuthService
from easel.control.store import Store


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

    code = main(["serve-control",
                 "--control-dir", str(tmp_path / "control"),
                 "--public-origin", "https://easel.test/",
                 "--host", "127.0.0.1", "--port", "9999"])
    assert code == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9999
    assert captured["proxy_headers"] is False, "不得默认信任转发头中的来源地址"

    app = captured["app"]
    with TestClient(app, base_url="https://easel.test") as client:
        assert client.get("/api/health").status_code == 200
        assert app.state.public_origin == "https://easel.test"


def test_serve_control_rejects_invalid_origin_before_opening_store(
        tmp_path, monkeypatch, capsys):
    from easel.control import cli as control_cli

    started = []
    monkeypatch.setattr(control_cli.uvicorn, "run",
                        lambda *args, **kwargs: started.append(True))

    control_dir = tmp_path / "control"
    code = main(["serve-control", "--control-dir", str(control_dir),
                 "--public-origin", "http://easel.test"])
    captured = capsys.readouterr()

    assert code != 0
    assert "invalid_origin" in captured.err
    assert started == [], "非法 Origin 不得启动 HTTP 服务"
    assert not control_dir.exists(), "非法配置不得创建或打开控制库"
