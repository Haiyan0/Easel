from __future__ import annotations

import asyncio
import copy
import importlib.util
import pathlib
import socket
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "deploy" / "multiuser" / "db_tunnel.py"
EXAMPLE = (ROOT / "skills/builtin/database-query/assets"
           / "tunnel.multiuser.example.yaml")


def _load():
    spec = importlib.util.spec_from_file_location(
        "easel_db_tunnel", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


db_tunnel = _load()
BASE = {
    "version": 1,
    "forwards": [{
        "source": "sales_readonly",
        "mode": "direct",
        "listen_port": 13300,
        "database": {"host": "db.internal", "port": 3306},
    }],
}
SSH = {
    "host": "jump.internal", "port": 22, "user": "easel-tunnel",
    "private_key": "id_ed25519", "known_hosts": "known_hosts",
}


def _write(tmp_path, payload) -> pathlib.Path:
    path = tmp_path / "tunnel.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def test_example_is_valid_with_placeholders():
    forwards = db_tunnel.load_forwards(EXAMPLE, allow_placeholders=True)
    assert [(f.source, f.mode, f.listen_port) for f in forwards] == [
        ("yuanmeng_mall_tracking_readonly", "ssh", 13306),
        ("yuanmeng_space_readonly", "direct", 13307),
        ("collectibles_readonly", "direct", 13308),
    ]
    assert forwards[0].ssh is not None
    assert forwards[0].ssh.private_key.name == "database-query_ed25519"
    assert forwards[1].ssh is None


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(version=2),
    lambda p: p.update(version=True),
    lambda p: p.update(extra=True),
    lambda p: p.update(forwards=[]),
    lambda p: p["forwards"].append(copy.deepcopy(p["forwards"][0])),
    lambda p: p["forwards"][0].update(mode="tunnel"),
    lambda p: p["forwards"][0].update(listen_port=13299),
    lambda p: p["forwards"][0].update(listen_port=13400),
    lambda p: p["forwards"][0].update(listen_port=13300, password="x"),
    lambda p: p["forwards"][0]["database"].update(port=0),
    lambda p: p["forwards"][0].update(
        ssh={"host": "jump.internal", "port": 22, "user": "u",
             "private_key": "k", "known_hosts": "known_hosts"}),
])
def test_invalid_config_is_rejected(tmp_path, mutate):
    payload = copy.deepcopy(BASE)
    mutate(payload)
    with pytest.raises(db_tunnel.TunnelConfigError):
        db_tunnel.load_forwards(
            _write(tmp_path, payload), keys_dir=tmp_path / "keys",
            allow_placeholders=True)


def test_ssh_requires_ssh_section_and_rejects_at_in_user(tmp_path):
    payload = copy.deepcopy(BASE)
    payload["forwards"][0]["mode"] = "ssh"
    with pytest.raises(db_tunnel.TunnelConfigError):
        db_tunnel.load_forwards(_write(tmp_path, payload))
    payload["forwards"][0]["ssh"] = {**SSH, "user": "a@b"}
    with pytest.raises(db_tunnel.TunnelConfigError):
        db_tunnel.load_forwards(_write(tmp_path, payload))


@pytest.mark.parametrize("host", [
    "db.example.com", "foo.example", "x.invalid", "x.test", "localhost",
    "LOCALHOST",
])
def test_placeholder_hosts_are_rejected_in_strict_mode(tmp_path, host):
    payload = copy.deepcopy(BASE)
    payload["forwards"][0]["database"]["host"] = host
    keys = tmp_path / "keys"
    keys.mkdir()
    with pytest.raises(db_tunnel.TunnelConfigError):
        db_tunnel.load_forwards(
            _write(tmp_path, payload), keys_dir=keys)


@pytest.mark.parametrize("name", [
    "/etc/private_key", "../private_key", "sub/../key", "", "sub/key",
])
def test_ssh_key_paths_must_stay_directly_in_keys_dir(tmp_path, name):
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "id_ed25519").write_text("key", encoding="utf-8")
    (keys / "known_hosts").write_text("", encoding="utf-8")
    payload = copy.deepcopy(BASE)
    payload["forwards"][0]["mode"] = "ssh"
    payload["forwards"][0]["ssh"] = {**SSH, "private_key": name}
    with pytest.raises(db_tunnel.TunnelConfigError):
        db_tunnel.load_forwards(_write(tmp_path, payload), keys_dir=keys)


def test_ssh_keys_must_exist_be_regular_and_not_symlinks(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "id_ed25519").write_text("key", encoding="utf-8")
    (keys / "known_hosts").write_text("", encoding="utf-8")
    payload = copy.deepcopy(BASE)
    payload["forwards"][0]["mode"] = "ssh"
    payload["forwards"][0]["ssh"] = SSH
    path = _write(tmp_path, payload)
    forwards = db_tunnel.load_forwards(path, keys_dir=keys)
    assert forwards[0].ssh.private_key == (keys / "id_ed25519").resolve()

    (keys / "id_ed25519").unlink()
    with pytest.raises(db_tunnel.TunnelConfigError):
        db_tunnel.load_forwards(path, keys_dir=keys)

    target = tmp_path / "outside_key"
    target.write_text("key", encoding="utf-8")
    try:
        (keys / "id_ed25519").symlink_to(target)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")
    with pytest.raises(db_tunnel.TunnelConfigError):
        db_tunnel.load_forwards(path, keys_dir=keys)


async def _echo(reader, writer):
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    finally:
        writer.close()


def _direct(listen_port, upstream_port):
    return db_tunnel.Forward(
        source="sales_readonly", mode="direct", listen_port=listen_port,
        database=db_tunnel.DatabaseTarget("127.0.0.1", upstream_port),
        ssh=None)


def test_direct_forward_pipes_bytes_both_ways():
    async def scenario():
        echo = await asyncio.start_server(_echo, "127.0.0.1", 0)
        upstream_port = echo.sockets[0].getsockname()[1]
        server = await db_tunnel.serve_direct(
            _direct(0, upstream_port), host="127.0.0.1")
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            payload = b"ping" * 4096
            writer.write(payload)
            await writer.drain()
            assert await reader.readexactly(len(payload)) == payload
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()
            echo.close()
            await echo.wait_closed()
    asyncio.run(scenario())


def test_direct_forward_caps_concurrent_connections():
    async def scenario():
        echo = await asyncio.start_server(_echo, "127.0.0.1", 0)
        upstream_port = echo.sockets[0].getsockname()[1]
        server = await db_tunnel.serve_direct(
            _direct(0, upstream_port), host="127.0.0.1", max_connections=1)
        port = server.sockets[0].getsockname()[1]
        try:
            first_reader, first_writer = await asyncio.open_connection(
                "127.0.0.1", port)
            second_reader, second_writer = await asyncio.open_connection(
                "127.0.0.1", port)
            assert await asyncio.wait_for(second_reader.read(), 2) == b""
            first_writer.close()
            await first_writer.wait_closed()
            second_writer.close()
            await second_writer.wait_closed()
            await first_reader.read()
        finally:
            server.close()
            await server.wait_closed()
            echo.close()
            await echo.wait_closed()
    asyncio.run(scenario())


def test_direct_forward_closes_client_when_upstream_refuses():
    async def scenario():
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        refused_port = probe.getsockname()[1]
        probe.close()
        server = await db_tunnel.serve_direct(
            _direct(0, refused_port), host="127.0.0.1")
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            # 本机环回连接拒绝约 2 秒才可见，5 秒仍低于默认 10 秒连接超时。
            assert await asyncio.wait_for(reader.read(), 5) == b""
            writer.close()
            await writer.wait_closed()
            # 转发器仍存活，可接受下一次连接。
            reader2, writer2 = await asyncio.open_connection("127.0.0.1", port)
            assert await asyncio.wait_for(reader2.read(), 5) == b""
            writer2.close()
            await writer2.wait_closed()
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())


def test_direct_forward_logs_starting_and_ready():
    events = []

    async def scenario():
        echo = await asyncio.start_server(_echo, "127.0.0.1", 0)
        upstream_port = echo.sockets[0].getsockname()[1]
        server = await db_tunnel.serve_direct(
            _direct(0, upstream_port), host="127.0.0.1",
            emit=lambda *args: events.append(args))
        server.close()
        await server.wait_closed()
        echo.close()
        await echo.wait_closed()
    asyncio.run(scenario())
    assert events == [
        ("sales_readonly", "direct", "starting", ""),
        ("sales_readonly", "direct", "ready", ""),
    ]
