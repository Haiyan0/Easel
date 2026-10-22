"""Task 8：worker 容器→sidecar 容器→真实 control broker 的 Docker 拓扑。

供应商传输仍是 fake；授权、配置发布、token 签发和 HTTP 入口使用真实
create_app/Auth/Config/Broker。显式选择后才运行：

    EASEL_RUN_CONTAINER_TESTS=1 \
    python -m pytest tests/integration/multiuser/test_control_broker_topology.py -q
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import time
import uuid

import httpx
import pytest

from easel.control.broker import BrokerService
from easel.control.config import MASTER_KEY_BYTES, ConfigService
from easel.control.egress import EgressGateway
from easel.control.store import Store

ROOT = pathlib.Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "deploy" / "multiuser" / "Dockerfile.worker"
EXECUTION_TAG = "easel-worker-execution-test:task8-r2"
ORIGIN = "https://easel.test"
MASTER_KEY = b"k" * MASTER_KEY_BYTES
ADMIN_SECRET = "topology-provider-secret-sentinel"

pytestmark = pytest.mark.skipif(
    os.environ.get("EASEL_RUN_CONTAINER_TESTS") != "1",
    reason="设置 EASEL_RUN_CONTAINER_TESTS=1 才运行真实 Docker 拓扑测试")


def _docker(*args: str, timeout: float = 120.0,
            check: bool = True) -> subprocess.CompletedProcess:
    binary = shutil.which("docker")
    if binary is None:
        pytest.fail("缺少 docker 可执行文件")
    result = subprocess.run([binary, *args], capture_output=True, text=True,
                            check=False, timeout=timeout)
    if check and result.returncode != 0:
        pytest.fail(f"docker {' '.join(args[:3])} 失败："
                    f"{result.stderr.strip()[-2000:]}")
    return result


@pytest.fixture(scope="module")
def execution_image() -> str:
    supplied = os.environ.get("EASEL_WORKER_IMAGE")
    if supplied:
        result = _docker("image", "inspect", "--format", "{{.Id}}", supplied)
        image_id = result.stdout.strip()
    else:
        _docker("build", "-f", str(DOCKERFILE), "--target", "execution",
                "-t", EXECUTION_TAG, str(ROOT), timeout=1800)
        image_id = _docker(
            "image", "inspect", "--format", "{{.Id}}",
            EXECUTION_TAG).stdout.strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        pytest.fail(f"拓扑测试镜像未获得固定摘要：{image_id!r}")
    return image_id


_CONTROL_APP = r'''
import os
import pathlib

import httpx
import uvicorn

from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.broker import BrokerService
from easel.control.config import ConfigService
from easel.control.egress import EgressGateway
from easel.control.store import Store


class Stream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"CONTROL_BROKER_TOPOLOGY_OK"

    async def aclose(self):
        return None


def handler(request):
    expected = "Bearer " + os.environ["PROVIDER_SECRET"]
    if request.headers.get("authorization") != expected:
        return httpx.Response(500, content=b"provider-auth-missing")
    return httpx.Response(
        200, headers={"content-type": "text/plain"},
        stream=Stream())


db = pathlib.Path(os.environ["CONTROL_DB"])
key = pathlib.Path(os.environ["MASTER_KEY_FILE"]).read_bytes()
with Store.open(db) as store:
    config = ConfigService(store, key)
    broker = BrokerService(
        store, config, clock=lambda: 1000.0,
        egress=EgressGateway(transport=httpx.MockTransport(handler)))
    app = create_app(
        store=store, auth=AuthService(store),
        config=ControlConfig(public_origin=os.environ["PUBLIC_ORIGIN"]),
        config_service=config, broker=broker)
    uvicorn.run(app, host="0.0.0.0", port=7861, log_level="error",
                access_log=False)
'''

_CLIENT = r'''
import json
import os

import httpx


base = f"http://{os.environ['SIDECAR']}:8899"
token = os.environ["BROKER_TOKEN"]
workspace_id = os.environ["WORKSPACE_ID"]
other_workspace_id = os.environ["OTHER_WORKSPACE_ID"]
run_id = os.environ["RUN_ID"]


def headers(broker_token, ws, rid):
    return {
        "X-Easel-Broker-Token": broker_token,
        "X-Easel-Workspace-Id": ws,
        "X-Easel-Run-Id": rid,
        "Content-Type": "application/json",
    }


url = base + "/internal/broker/speech/audio/speech"
body = json.dumps({"model": "tts-default", "input": "topology"})
with httpx.Client(trust_env=False, timeout=10.0) as client:
    allowed = client.post(
        url, content=body, headers=headers(token, workspace_id, run_id))
    assert allowed.status_code == 200, (allowed.status_code, allowed.text)
    assert allowed.text == "CONTROL_BROKER_TOPOLOGY_OK", allowed.text

    missing = client.post(url, content=body)
    assert missing.status_code == 401, missing.status_code

    invalid = client.post(
        url, content=body,
        headers=headers("invalid-token", workspace_id, run_id))
    assert invalid.status_code == 401, invalid.status_code

    crossed = client.post(
        url, content=body,
        headers=headers(token, other_workspace_id, run_id))
    assert crossed.status_code == 404, crossed.status_code

    wrong_run = client.post(
        url, content=body,
        headers=headers(token, workspace_id, "other-run"))
    assert wrong_run.status_code == 404, wrong_run.status_code

    management = client.post(
        base + "/api/admin/managed-config", content=body,
        headers=headers(token, workspace_id, run_id))
    assert management.status_code == 404, management.status_code

print("CONTROL_BROKER_TOPOLOGY_OK")
'''


def _wait_healthy(name: str, deadline: float) -> None:
    while time.monotonic() < deadline:
        result = _docker(
            "inspect", "--format",
            "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
            name, check=False)
        if result.returncode == 0 and result.stdout.strip() == "running healthy":
            return
        if result.returncode == 0 and "exited" in result.stdout:
            logs = _docker("logs", "--tail", "100", name, check=False)
            pytest.fail(
                f"{name} 提前退出：{logs.stderr[-2000:]}{logs.stdout[-2000:]}")
        time.sleep(0.2)
    logs = _docker("logs", "--tail", "100", name, check=False)
    pytest.fail(f"{name} 未在期限内 healthy：{logs.stderr[-2000:]}")


def _seed_control_database(tmp_path):
    database = tmp_path / "control.sqlite3"
    with Store.open(database) as store:
        admin = store.create_user("Topology Owner", "admin")
        member = store.create_user("Topology Member")
        other = store.create_user("Topology Other")
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
            member, conversation.id, "topology-request-1", "digest", 1)
        broker = BrokerService(
            store, config, clock=lambda: 1000.0,
            egress=EgressGateway(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200))))
        token = broker.issue(member, run.id, "speech")
    return token, member, other, run


def test_worker_sidecar_control_broker_topology(execution_image, tmp_path):
    suffix = uuid.uuid4().hex[:12]
    workspace_network = f"easel-topo-ws-{suffix}"
    outbound_network = f"easel-topo-out-{suffix}"
    control = f"easel-topo-control-{suffix}"
    sidecar = f"easel-topo-sidecar-{suffix}"
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    token, member, other, run = _seed_control_database(tmp_path)
    key_path = tmp_path / "master.key"
    key_path.write_bytes(MASTER_KEY)
    control_script = tmp_path / "control_app.py"
    control_script.write_text(_CONTROL_APP, encoding="utf-8")
    client_script = tmp_path / "client.py"
    client_script.write_text(_CLIENT, encoding="utf-8")
    control_env = {
        "CONTROL_DB": "/srv/easel/control.sqlite3",
        "MASTER_KEY_FILE": "/srv/easel/master.key",
        "PUBLIC_ORIGIN": ORIGIN,
        "PROVIDER_SECRET": ADMIN_SECRET,
        "HOME": "/tmp",
        "PYTHONPATH": "/opt/easel",
    }

    try:
        _docker("network", "create", "--internal", workspace_network)
        _docker("network", "create", outbound_network)
        control_args = [
            "run", "-d", "--name", control,
            "--user", "10001:10001", "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--network", outbound_network,
            "-v", f"{tmp_path}:/srv/easel",
            "--health-cmd",
            ("python -c \"import urllib.request;urllib.request.urlopen("
             "'http://127.0.0.1:7861/api/health',timeout=2)\""),
            "--health-interval=1s", "--health-timeout=2s",
            "--health-retries=30", "--health-start-period=1s",
        ]
        for name, value in control_env.items():
            control_args.extend(["-e", f"{name}={value}"])
        control_args.extend([execution_image, "python",
                             "/srv/easel/control_app.py"])
        _docker(*control_args, timeout=60.0)
        _wait_healthy(control, time.monotonic() + 60.0)

        _docker(
            "run", "-d", "--name", sidecar,
            "--user", "10001:10001", "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--network", workspace_network,
            "-v", f"{policy}:/etc/easel/egress-policy.json:ro",
            "--health-cmd",
            ("python -c \"import urllib.request;urllib.request.urlopen("
             "'http://127.0.0.1:8899/healthz',timeout=2)\""),
            "--health-interval=1s", "--health-timeout=2s",
            "--health-retries=30", "--health-start-period=1s",
            execution_image, "python", "deploy/multiuser/worker_sidecar.py",
            "--listen", "0.0.0.0", "--broker-port", "8899",
            "--egress-port", "8898", "--broker-upstream",
            f"{control}:7861",
            "--policy", "/etc/easel/egress-policy.json",
            timeout=60.0)
        _docker("network", "connect", outbound_network, sidecar, timeout=30.0)
        _wait_healthy(sidecar, time.monotonic() + 60.0)

        worker_env = {
            "SIDECAR": sidecar,
            "BROKER_TOKEN": token,
            "WORKSPACE_ID": member.workspace_id,
            "OTHER_WORKSPACE_ID": other.workspace_id,
            "RUN_ID": run.id,
        }
        worker_args = [
            "run", "--rm", "--network", workspace_network,
            "--user", "10001:10001", "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "-v", f"{client_script}:/check/client.py:ro",
        ]
        for name, value in worker_env.items():
            worker_args.extend(["-e", f"{name}={value}"])
        worker_args.extend([execution_image, "python", "/check/client.py"])
        result = _docker(*worker_args, timeout=60.0, check=False)
        assert result.returncode == 0, result.stderr[-3000:]
        assert "CONTROL_BROKER_TOPOLOGY_OK" in result.stdout
    finally:
        _docker("rm", "-f", sidecar, check=False)
        _docker("rm", "-f", control, check=False)
        _docker("network", "rm", workspace_network, check=False)
        _docker("network", "rm", outbound_network, check=False)
