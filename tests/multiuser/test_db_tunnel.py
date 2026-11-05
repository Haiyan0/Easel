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


# ---------------------------------------------------------------------
# SSH 固定参数与失败码映射
# ---------------------------------------------------------------------


def _ssh_forward(tmp_path, port=13306):
    keys = tmp_path / "keys"
    keys.mkdir(exist_ok=True)
    key = keys / "database-query_ed25519"
    known = keys / "known_hosts"
    key.write_text("key", encoding="utf-8")
    known.write_text("", encoding="utf-8")
    return db_tunnel.Forward(
        source="tracking_readonly", mode="ssh", listen_port=port,
        database=db_tunnel.DatabaseTarget("10.0.0.10", 3306),
        ssh=db_tunnel.SshTarget(
            host="jump.internal", port=2222, user="easel-tunnel",
            private_key=key, known_hosts=known))


def test_ssh_argv_is_fixed_and_fail_closed(tmp_path):
    forward = _ssh_forward(tmp_path)
    argv = db_tunnel.build_ssh_argv(forward)
    assert argv[0] == "/usr/bin/ssh"
    for option in (
        "StrictHostKeyChecking=yes", "BatchMode=yes",
        "PreferredAuthentications=publickey", "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no", "IdentitiesOnly=yes",
        "IdentityAgent=none", "ForwardAgent=no", "ExitOnForwardFailure=yes",
        "ConnectTimeout=10", "ServerAliveInterval=30",
        "ServerAliveCountMax=3", "GlobalKnownHostsFile=/dev/null",
        "UpdateHostKeys=no", "ControlMaster=no", "ControlPath=none",
    ):
        assert ["-o", option] == argv[argv.index(option) - 1:
                                       argv.index(option) + 1]
    assert f"UserKnownHostsFile={forward.ssh.known_hosts}" in argv
    assert ["-i", str(forward.ssh.private_key)] == argv[argv.index("-i"):][:2]
    assert f"0.0.0.0:{forward.listen_port}:10.0.0.10:3306" == (
        argv[argv.index("-L") + 1])
    assert argv[-1] == "easel-tunnel@jump.internal"
    assert "-N" in argv and "-T" in argv and "-F" in argv
    assert "/dev/null" in argv
    assert "StrictHostKeyChecking=no" not in argv
    assert "accept-new" not in " ".join(argv)
    assert not any("/bin/sh" in part or "bash" in part for part in argv)


@pytest.mark.parametrize("text,reason", [
    ("Host key verification failed.", "host_key_mismatch"),
    ('Load key "/run/easel-secrets/keys/id": bad permissions',
     "key_unreadable"),
    ("Warning: Identity file /x not accessible", "key_unreadable"),
    ("Permission denied (publickey).", "auth_failed"),
    ("Could not request local forwarding.", "forward_failed"),
    ("cannot listen to port 13306", "forward_failed"),
    ("ssh: connect to host jump port 22: Connection refused",
     "connect_failed"),
    ("ssh: Could not resolve hostname jump", "connect_failed"),
    ("something unexpected", "connect_failed"),
])
def test_stderr_maps_to_fixed_reason(text, reason):
    assert db_tunnel.classify_ssh_stderr(text) == reason


# ---------------------------------------------------------------------
# SSH 常驻监督：退避、稳定重置、停止
# ---------------------------------------------------------------------


class _FakeStderr:
    def __init__(self, text=b""):
        self._text = text

    async def read(self, n=-1):
        return self._text


class FakeProcess:
    def __init__(self, stderr_text=b"", duration=0.0, ignore_term=False,
                 clock=None):
        self._stderr = _FakeStderr(stderr_text)
        self.returncode = None
        self._duration = duration
        self._ignore_term = ignore_term
        self._clock = clock if clock is not None else [0.0]
        self.terminated = False
        self.killed = False
        self._exited = asyncio.Event()

    @property
    def stderr(self):
        return self._stderr

    async def wait(self):
        if self._ignore_term:
            await self._exited.wait()
            return -9
        # 只推进注入时钟，不让测试真的等待 duration 秒。
        self._clock[0] += self._duration
        await asyncio.sleep(0)
        self.returncode = 1
        return 1

    def terminate(self):
        self.terminated = True
        if not self._ignore_term:
            self.returncode = -15
            self._exited.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._exited.set()

    def release(self):
        self._exited.set()


def test_supervisor_backs_off_and_stops(tmp_path):
    async def scenario():
        sleeps = []
        logs = []
        argvs = []
        forward = _ssh_forward(tmp_path)

        async def spawn(argv):
            argvs.append(argv)
            return FakeProcess(b"Permission denied (publickey).", 0)

        supervisor = db_tunnel.SshForwardSupervisor(
            forward,
            spawn=spawn,
            sleep=lambda delay: sleeps.append(delay) or asyncio.sleep(0),
            ready_probe=lambda _port: asyncio.sleep(0, result=False),
            emit=lambda *args: logs.append(args))
        task = asyncio.create_task(supervisor.run())
        for _ in range(100):
            if len(sleeps) >= 4:
                break
            await asyncio.sleep(0.01)
        await supervisor.stop()
        await asyncio.wait_for(task, 2)
        assert argvs[0] == db_tunnel.build_ssh_argv(forward)
        assert sleeps[:4] == [1.0, 2.0, 4.0, 8.0]
        assert ("tracking_readonly", "ssh", "failed", "auth_failed") in logs
        assert ("tracking_readonly", "ssh", "stopped", "") in logs
        assert all("Permission denied" not in str(entry) for entry in logs)
    asyncio.run(scenario())


def test_supervisor_resets_backoff_after_stable_run(tmp_path):
    async def scenario():
        sleeps = []
        clock = [0.0]
        durations = [0.0, 200.0, 10.0] + [0.0] * 100

        async def spawn(_argv):
            return FakeProcess(b"", durations.pop(0), clock=clock)

        async def fake_sleep(delay):
            sleeps.append(delay)
            clock[0] += delay

        supervisor = db_tunnel.SshForwardSupervisor(
            _ssh_forward(tmp_path), spawn=spawn, sleep=fake_sleep,
            clock=lambda: clock[0],
            ready_probe=lambda _p: asyncio.sleep(0, result=False))
        task = asyncio.create_task(supervisor.run())
        for _ in range(100):
            if len(sleeps) >= 3:
                break
            await asyncio.sleep(0)
        await supervisor.stop()
        await asyncio.wait_for(task, 2)
        assert sleeps[:3] == [1.0, 1.0, 2.0]
    asyncio.run(scenario())


def test_supervisor_terminates_then_kills_on_stop(tmp_path):
    async def scenario():
        process = FakeProcess(ignore_term=True)
        logs = []

        async def spawn(_argv):
            return process

        supervisor = db_tunnel.SshForwardSupervisor(
            _ssh_forward(tmp_path), spawn=spawn,
            ready_probe=lambda _p: asyncio.sleep(0, result=True),
            stop_timeout=0.01, emit=lambda *args: logs.append(args))
        task = asyncio.create_task(supervisor.run())
        for _ in range(100):
            if ("tracking_readonly", "ssh", "ready", "") in logs:
                break
            await asyncio.sleep(0.01)
        await supervisor.stop()
        await asyncio.wait_for(task, 2)
        assert process.terminated and process.killed
        assert ("tracking_readonly", "ssh", "stopped", "") in logs
    asyncio.run(scenario())


class _FakeStopSupervisor:
    """记录 stop 是否并发触发，并可模拟单个监督器失败。"""

    def __init__(self, started, release, *, fail=False):
        self._started = started
        self._release = release
        self._fail = fail

    async def stop(self):
        self._started.set()
        await self._release.wait()
        if self._fail:
            raise RuntimeError("stop failed")


def test_runtime_stops_supervisors_concurrently_and_tolerates_failure():
    async def scenario():
        started_a, started_b = asyncio.Event(), asyncio.Event()
        release = asyncio.Event()
        runtime = db_tunnel.TunnelRuntime(
            tasks=[asyncio.create_task(asyncio.Event().wait()),
                   asyncio.create_task(asyncio.Event().wait())],
            servers=[],
            supervisors=[
                _FakeStopSupervisor(started_a, release, fail=True),
                _FakeStopSupervisor(started_b, release),
            ])
        stop_task = asyncio.create_task(runtime.stop())
        # 串行实现会阻塞在第一个 stop 的 release 上，第二个 started 不会置位。
        await asyncio.wait_for(
            asyncio.gather(started_a.wait(), started_b.wait()), 2)
        release.set()
        await asyncio.wait_for(stop_task, 2)
        assert all(task.cancelled() for task in runtime.tasks)
    asyncio.run(scenario())


def test_start_tunnel_rejects_keys_path_that_is_not_a_directory(tmp_path):
    async def scenario():
        keys_file = tmp_path / "keys"
        keys_file.write_text("not a directory", encoding="utf-8")
        logs = []
        runtime = await db_tunnel.start_tunnel(
            keys_dir=keys_file, config_path=tmp_path / "missing.yaml",
            emit=lambda *args: logs.append(args))
        assert runtime is not None
        assert runtime.tasks == [] and runtime.servers == []
        assert runtime.supervisors == []
        assert logs == [("", "", "failed", "config_invalid")]
    asyncio.run(scenario())


def test_start_tunnel_missing_keys_directory_is_silently_disabled(tmp_path):
    async def scenario():
        logs = []
        runtime = await db_tunnel.start_tunnel(
            keys_dir=tmp_path / "missing",
            config_path=tmp_path / "missing.yaml",
            emit=lambda *args: logs.append(args))
        assert runtime is None
        assert logs == []
    asyncio.run(scenario())
