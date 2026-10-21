"""Task 8 真实 Linux 网络接线：worker 仅经 sidecar 访问 broker/出口。

显式选择后才运行：

    EASEL_RUN_CONTAINER_TESTS=1 \
    python -m pytest tests/integration/multiuser/test_network_wiring.py -q
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "deploy" / "multiuser" / "Dockerfile.worker"
DEPLOY = ROOT / "deploy" / "multiuser"
IMAGE_TAG = "easel-worker-network-test:task8"

pytestmark = pytest.mark.skipif(
    os.environ.get("EASEL_RUN_CONTAINER_TESTS") != "1",
    reason="设置 EASEL_RUN_CONTAINER_TESTS=1 才运行真实网络接线测试")


def _docker(*args: str, timeout: float = 120.0,
            check: bool = True) -> subprocess.CompletedProcess:
    binary = shutil.which("docker")
    if binary is None:
        pytest.fail("缺少 docker 可执行文件")
    result = subprocess.run([binary, *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, check=False,
                            timeout=timeout)
    if check and result.returncode != 0:
        pytest.fail(f"docker {' '.join(args[:3])} 失败："
                    f"{result.stderr.strip()[-2000:]}")
    return result


@pytest.fixture(scope="module")
def network_image() -> str:
    _docker("build", "-f", str(DOCKERFILE), "--target", "base",
            "-t", IMAGE_TAG, str(ROOT), timeout=1800)
    return IMAGE_TAG


_UPSTREAM = r'''
import http.server
import json
import socketserver
import socket
import threading

class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        if self.path != "/internal/broker/chat/chat/completions":
            self.send_error(404)
            return
        payload = json.dumps({
            "tokenSeen": bool(self.headers.get("x-easel-broker-token")),
            "body": body.decode("utf-8", "replace"),
        }).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def log_message(self, *_args):
        pass

class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

def echo():
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", 443))
    listener.listen()
    while True:
        conn, _ = listener.accept()
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                conn.sendall(b"ECHO:" + data)
        finally:
            conn.close()

threading.Thread(target=echo, daemon=True).start()
Server(("0.0.0.0", 8000), Handler).serve_forever()
'''

_CLIENT = r'''
import http.client
import json
import socket
import sys

sidecar = sys.argv[1]
approved_ip = sys.argv[2]

def broker(path, body=b"{}"):
    connection = http.client.HTTPConnection(sidecar, 8899, timeout=10)
    connection.request("POST", path, body=body, headers={
        "x-easel-broker-token": "test-token",
        "x-easel-workspace-id": "a" * 32,
        "x-easel-run-id": "run-1",
    })
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, data

def connect(target):
    connection = socket.create_connection((sidecar, 8898), timeout=10)
    connection.sendall(
        ("CONNECT " + target + " HTTP/1.1\r\n\r\n").encode())
    data = connection.recv(4096)
    return connection, data

status, body = broker("/internal/broker/chat/chat/completions")
assert status == 200, (status, body)
payload = json.loads(body)
assert payload["tokenSeen"] is True and payload["body"] == "{}", payload

status, _ = broker("/api/admin/config")
assert status == 404, status

try:
    direct = socket.create_connection(("broker", 8000), timeout=3)
except OSError:
    pass
else:
    direct.close()
    raise AssertionError("worker 不得直连 broker 网络")

try:
    direct = socket.create_connection((approved_ip, 443), timeout=3)
except OSError:
    pass
else:
    direct.close()
    raise AssertionError("worker 不得直连受控出网目标")

connection, response = connect(approved_ip + ":443")
assert response.startswith(b"HTTP/1.1 200"), response
connection.sendall(b"ping")
echoed = connection.recv(4096)
connection.close()
assert echoed == b"ECHO:ping", echoed

connection, response = connect("127.0.0.1:443")
connection.close()
assert response.startswith(b"HTTP/1.1 403"), response

try:
    direct = socket.create_connection(("1.1.1.1", 443), timeout=3)
except OSError:
    pass
else:
    direct.close()
    raise AssertionError("worker 不得直接访问公网")

print("NETWORK_WIRING_OK")
'''


def _remove(container: str) -> None:
    _docker("rm", "-f", container, check=False)


def _remove_network(network: str) -> None:
    _docker("network", "rm", network, check=False)


def test_worker_reaches_broker_and_egress_only_through_sidecar(
        network_image, tmp_path):
    suffix = uuid.uuid4().hex[:12]
    workspace_network = f"easel-net-ws-{suffix}"
    outbound_network = f"easel-net-out-{suffix}"
    broker = f"easel-net-broker-{suffix}"
    sidecar = f"easel-net-sidecar-{suffix}"
    upstream_script = tmp_path / "upstream.py"
    client_script = tmp_path / "client.py"
    policy = tmp_path / "policy.json"
    upstream_script.write_text(_UPSTREAM, encoding="utf-8")
    client_script.write_text(_CLIENT, encoding="utf-8")
    try:
        _docker("network", "create", "--internal", workspace_network)
        _docker("network", "create", outbound_network)
        subnet = _docker(
            "network", "inspect", "--format",
            "{{range .IPAM.Config}}{{.Subnet}}{{end}}",
            outbound_network).stdout.strip()
        assert subnet, "测试出站网络必须分配 IPv4 子网"
        policy.write_text(json.dumps({"approved_cidrs": [subnet]}),
                          encoding="utf-8")

        _docker("run", "-d", "--name", broker,
                "--network", outbound_network,
                "-v", f"{upstream_script}:/check/upstream.py:ro",
                network_image, "python", "/check/upstream.py")
        broker_ip = _docker(
            "inspect", "--format",
            '{{(index .NetworkSettings.Networks "' + outbound_network
            + '").IPAddress}}', broker).stdout.strip()
        assert broker_ip

        _docker(
            "run", "-d", "--name", sidecar,
            "--user", "10001:10001",
            "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--network", workspace_network,
            "-v", f"{DEPLOY}:/opt/easel/deploy/multiuser:ro",
            "-v", (f"{ROOT / 'skills' / 'shared' / 'scripts'}"
                   ":/opt/easel/skills/shared/scripts:ro"),
            "-v", f"{policy}:/etc/easel/egress-policy.json:ro",
            network_image, "python", "deploy/multiuser/worker_sidecar.py",
            "--listen", "0.0.0.0", "--broker-port", "8899",
            "--egress-port", "8898", "--broker-upstream",
            f"{broker}:8000", "--policy", "/etc/easel/egress-policy.json")
        _docker("network", "connect", outbound_network, sidecar)

        # 等待 sidecar 两个监听器可用后再运行 worker 侧客户端。
        deadline = time.monotonic() + 30
        while True:
            check = _docker(
                "run", "--rm", "--network", workspace_network,
                network_image, "python", "-c",
                ("import socket;s=socket.socket();s.settimeout(1);"
                 f"s.connect(('{sidecar}',8899));s.close()"),
                check=False)
            if check.returncode == 0:
                break
            if time.monotonic() >= deadline:
                logs = _docker("logs", sidecar, check=False).stdout
                pytest.fail(f"sidecar 未就绪：{check.stderr}\n{logs[-2000:]}")
            time.sleep(0.3)

        result = _docker(
            "run", "--rm", "--network", workspace_network,
            "-v", f"{client_script}:/check/client.py:ro",
            network_image, "python", "/check/client.py", sidecar, broker_ip,
            timeout=60, check=False)
        assert result.returncode == 0, result.stderr[-3000:]
        assert "NETWORK_WIRING_OK" in result.stdout
    finally:
        _remove(sidecar)
        _remove(broker)
        _remove_network(workspace_network)
        _remove_network(outbound_network)
