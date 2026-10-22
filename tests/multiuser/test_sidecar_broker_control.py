"""Task 8 N-R1：真实 control broker 经实际 sidecar socket 的授权装配。

不是未授权的测试 HTTP 上游：这里启动真实 create_app/Auth/Config/Broker，
由独立 sidecar 进程转发，验证合法 token 到 broker、错误/跨 workspace/run
token 被真实 broker 拒绝，且管理路径不会进入 control。
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time

import httpx
import pytest
import uvicorn

from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.broker import BrokerService
from easel.control.config import MASTER_KEY_BYTES, ConfigService
from easel.control.egress import EgressGateway
from easel.control.store import Store

ROOT = pathlib.Path(__file__).resolve().parents[2]
SIDECAR = ROOT / "deploy" / "multiuser" / "worker_sidecar.py"
MASTER_KEY = b"k" * MASTER_KEY_BYTES
ADMIN_SECRET = "admin-secret-sentinel-sidecar-broker"
ORIGIN = "https://easel.test"


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = list(chunks)

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        return None


def _transport(record: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        record["method"] = request.method
        record["url"] = str(request.url)
        record["headers"] = dict(request.headers)
        record["body"] = request.content
        record["calls"] = record.get("calls", 0) + 1
        return httpx.Response(
            200, headers={"content-type": "audio/mpeg"},
            stream=ChunkStream([b"audio-through-real-broker"]))
    return httpx.MockTransport(handler)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_http(url: str, process: subprocess.Popen, deadline: float) -> None:
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            pytest.fail(f"sidecar 提前退出：{stderr[-2000:]}")
        try:
            with httpx.Client(trust_env=False, timeout=1.0) as client:
                response = client.get(url)
            if response.status_code == 200:
                return
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(0.05)
    stderr = ""
    if process.poll() is not None and process.stderr is not None:
        stderr = process.stderr.read()
    pytest.fail(f"sidecar 未在期限内就绪：{stderr[-2000:]}")


@contextlib.contextmanager
def real_broker_through_sidecar(tmp_path):
    """真实 control app/uvicorn + 独立 sidecar 进程 + fake provider 传输。"""
    policy = tmp_path / "egress-policy.json"
    policy.write_text("{}", encoding="utf-8")
    database = tmp_path / "control.sqlite3"
    record: dict = {}
    resources = contextlib.ExitStack()
    store = resources.enter_context(Store.open(database))
    server = None
    thread = None
    sidecar = None
    client = None
    try:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        other = store.create_user("Other")
        config = ConfigService(store, MASTER_KEY)
        config.publish(admin, {"defaults": {
            "speech": {
                "provider": "openai-compatible",
                "model": "tts-default",
                "base_url": "https://provider.example/v1",
                "protocol": "openai",
                "secret": ADMIN_SECRET,
                "options": {},
            },
        }}, expected_version=0)
        conversation = store.create_conversation(member)
        run = store.reserve_run(
            member, conversation.id, "req-sidecar-1", "digest", 1)
        broker = BrokerService(
            store, config, clock=lambda: 1000.0,
            egress=EgressGateway(transport=_transport(record)))
        token = broker.issue(member, run.id, "speech")
        app = create_app(
            store=store, auth=AuthService(store),
            config=ControlConfig(public_origin=ORIGIN),
            config_service=config, broker=broker)

        uv_config = uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="error",
            access_log=False)
        server = uvicorn.Server(uv_config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 15.0
        while not server.started and time.monotonic() < deadline:
            if not thread.is_alive():
                pytest.fail("control uvicorn 启动失败")
            time.sleep(0.02)
        if not server.started:
            pytest.fail("control uvicorn 未在期限内启动")
        control_port = int(
            server.servers[0].sockets[0].getsockname()[1])

        broker_port = _free_port()
        egress_port = _free_port()
        while egress_port == broker_port:
            egress_port = _free_port()
        sidecar = subprocess.Popen(
            [sys.executable, str(SIDECAR),
             "--listen", "127.0.0.1",
             "--broker-port", str(broker_port),
             "--egress-port", str(egress_port),
             "--broker-upstream", f"127.0.0.1:{control_port}",
             "--policy", str(policy)],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=os.environ.copy())
        _wait_http(
            f"http://127.0.0.1:{broker_port}/healthz", sidecar,
            time.monotonic() + 15.0)
        client = httpx.Client(trust_env=False, timeout=10.0)
        yield client, broker_port, token, member, other, run, record
    finally:
        if client is not None:
            client.close()
        if sidecar is not None:
            sidecar.terminate()
            try:
                sidecar.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sidecar.kill()
                sidecar.wait(timeout=5)
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=10)
        resources.close()


def _headers(token: str, workspace_id: str, run_id: str) -> dict[str, str]:
    return {
        "X-Easel-Broker-Token": token,
        "X-Easel-Workspace-Id": workspace_id,
        "X-Easel-Run-Id": run_id,
        "Content-Type": "application/json",
    }


def test_real_broker_authorization_through_actual_sidecar_socket(tmp_path):
    with real_broker_through_sidecar(tmp_path) as (
            client, port, token, member, other, run, record):
        url = (f"http://127.0.0.1:{port}/internal/broker/speech/"
               "audio/speech")
        body = json.dumps({"model": "tts-default", "input": "hello"})
        allowed = client.post(
            url, content=body, headers=_headers(
                token, member.workspace_id, run.id))
        assert allowed.status_code == 200, allowed.text
        assert allowed.content == b"audio-through-real-broker"
        assert record["calls"] == 1
        assert record["url"] == "https://provider.example/v1/audio/speech"
        assert record["method"] == "POST"
        assert json.loads(record["body"])["input"] == "hello"
        headers = {name.lower(): value
                   for name, value in record["headers"].items()}
        assert headers["authorization"] == f"Bearer {ADMIN_SECRET}"
        assert ADMIN_SECRET not in allowed.text
        assert token not in allowed.text

        invalid = client.post(
            url, content=body,
            headers=_headers("invalid-token", member.workspace_id, run.id))
        assert invalid.status_code == 401
        assert record["calls"] == 1

        crossed = client.post(
            url, content=body,
            headers=_headers(token, other.workspace_id, run.id))
        assert crossed.status_code == 404
        assert record["calls"] == 1

        wrong_run = client.post(
            url, content=body,
            headers=_headers(token, member.workspace_id, "other-run"))
        assert wrong_run.status_code == 404
        assert record["calls"] == 1

        # 管理路径在 sidecar 入口即 404，不会到达 control 的管理接口。
        management = client.post(
            f"http://127.0.0.1:{port}/api/admin/managed-config",
            content=json.dumps({"defaults": {}}),
            headers=_headers(token, member.workspace_id, run.id))
        assert management.status_code == 404
        assert management.text == "Not Found"
        assert record["calls"] == 1

        unknown_broker = client.post(
            f"http://127.0.0.1:{port}/internal/broker/speech/audio/unknown",
            content=body,
            headers=_headers(token, member.workspace_id, run.id))
        assert unknown_broker.status_code == 404
        assert record["calls"] == 1
