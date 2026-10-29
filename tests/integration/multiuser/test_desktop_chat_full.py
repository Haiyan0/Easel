"""A4.5：真实公开 control→gateway→worker→sidecar→broker 装配验收。

显式 ``EASEL_RUN_CONTAINER_TESTS=1`` 后运行。control 使用正式
``serve-control``/``create_app``/relay factory 与真实 SQLite/Auth/RuntimeManager/
UnixProvisionerClient；worker 的 ACP 夹具在真实子进程内读取受管 broker
URL/token，经 sidecar 到 control broker，再到测试 TLS fake provider。

本测试不是真实 DeepSeek Harness，也不触发真实供应商计费。
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = pathlib.Path(__file__).resolve().parents[3]
CONTROL_TAG = "easel-control-test:a4"
GATEWAY_TAG = "easel-gateway-test:g2"
WORKER_TAG = "easel-worker-execution:a4"
FIXTURE = ROOT / "tests" / "fixtures" / "broker_acp.py"
FAKE_PROVIDER = ROOT / "tests" / "fixtures" / "fake_openai_tls.py"
WSL_DISTRO = "Ubuntu-26.04"
ORIGIN = "https://easel.test"
RUN_LABEL = "easel.a4.test"
SENTINEL_KEY = "sentinel-provider-key-a4"
PROVIDER_MODEL = "deepseek-chat"

pytestmark = pytest.mark.skipif(
    os.environ.get("EASEL_RUN_CONTAINER_TESTS") != "1",
    reason="设置 EASEL_RUN_CONTAINER_TESTS=1 才运行真实 A4 装配测试")


def _docker(*args: str, timeout: float = 180.0, check: bool = True,
            input_text: str | None = None) -> subprocess.CompletedProcess:
    binary = shutil.which("docker")
    if binary is None:
        pytest.fail("找不到 docker")
    result = subprocess.run(
        [binary, *args], input=input_text, capture_output=True, text=True,
        check=False, timeout=timeout)
    if check and result.returncode != 0:
        pytest.fail(
            f"docker {' '.join(args[:3])} 失败：{result.stderr[-2000:]}")
    return result


def _wsl(*args: str, check: bool = True,
         timeout: float = 180.0) -> subprocess.CompletedProcess:
    wsl = shutil.which("wsl")
    if wsl is None:
        pytest.skip("本机没有 wsl 可执行文件")
    result = subprocess.run(
        [wsl, "-d", WSL_DISTRO, "--", *args],
        capture_output=True, text=True, check=False, timeout=timeout)
    if check and result.returncode != 0:
        pytest.fail(
            "wsl {} 失败：{}".format(
                " ".join(args[:3]), result.stderr.strip()[-2000:]))
    return result


def _wsl_docker(*args: str, check: bool = True,
                timeout: float = 180.0) -> subprocess.CompletedProcess:
    return _wsl("docker", *args, check=check, timeout=timeout)


def _wsl_path(path: pathlib.Path) -> str:
    text = str(path)
    match = re.match(r"^([A-Za-z]):\\(.*)$", text)
    if match is None:
        pytest.skip("测试临时目录不在 Windows 盘符，无法映射到 WSL")
    rest = match.group(2).replace("\\", "/")
    return f"/mnt/{match.group(1).lower()}/{rest}"


def _image_id(tag: str) -> str:
    result = _docker("image", "inspect", "--format", "{{.Id}}", tag,
                     check=False)
    image = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(
            r"sha256:[0-9a-f]{64}", image) is None:
        pytest.fail(f"缺少固定镜像 {tag}")
    return image


def _label_args(run_id: str) -> list[str]:
    return ["--label", f"{RUN_LABEL}={run_id}"]


def _run_id() -> str:
    return uuid.uuid4().hex[:12]


def _wait_until(predicate, timeout: float = 60.0,
                interval: float = 0.5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception:  # noqa: BLE001,S110
            pass
        time.sleep(interval)
    pytest.fail("等待条件超时")


def _container_running(name: str) -> bool:
    result = _docker("container", "inspect", "--format", "{{.State.Running}}",
                     name, check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def _container_id(name: str) -> str:
    result = _docker("container", "inspect", "--format", "{{.Id}}", name,
                     check=False)
    value = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{64}", value):
        pytest.fail(f"容器不存在或 ID 非法：{name}")
    return value


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _write_ed25519_keys(directory: pathlib.Path) -> None:
    key = Ed25519PrivateKey.generate()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "snapshot.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    (directory / "worker-public.key").write_bytes(
        key.public_key().public_bytes_raw())
    (directory / "gateway-public.pem").write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo))
    (directory / "master.key").write_bytes(os.urandom(32))


def _name(prefix: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, prefix)])


def _write_tls(directory: pathlib.Path) -> tuple[pathlib.Path,
                                                 pathlib.Path,
                                                 pathlib.Path]:
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("easel-a4-test-ca"))
        .issuer_name(_name("easel-a4-test-ca"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                       critical=True)
        .sign(ca_key, hashes.SHA256()))
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("fake-provider.test"))
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName(
            [x509.DNSName("fake-provider.test")]), critical=False)
        .add_extension(x509.ExtendedKeyUsage(
            [ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256()))
    ca_path = directory / "ca.pem"
    cert_path = directory / "server.pem"
    key_path = directory / "server.key"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server_cert.public_bytes(
        serialization.Encoding.PEM))
    key_path.write_bytes(server_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return ca_path, cert_path, key_path


@dataclass
class User:
    user_id: str
    workspace_id: str
    cookie: str
    csrf: str
    client: httpx.Client

    def headers(self, *, write: bool = False) -> dict[str, str]:
        values = {"Cookie": self.cookie, "Origin": ORIGIN}
        if write:
            values["X-CSRF-Token"] = self.csrf
        return values


def _login(client: httpx.Client, invite: str) -> User:
    response = client.post(
        "/api/auth/exchange", headers={"Origin": ORIGIN},
        json={"token": invite})
    if response.status_code != 200:
        pytest.fail(f"邀请兑换失败 HTTP {response.status_code}", pytrace=False)
    cookie = response.headers.get("set-cookie", "").split(";", 1)[0]
    body = response.json()
    cookie_value = cookie.split("=", 1)[1] if "=" in cookie else ""
    if not cookie_value or not body.get("csrfToken"):
        pytest.fail("登录响应缺少 Cookie/CSRF", pytrace=False)
    return User(
        user_id=str(body["userId"]), workspace_id=str(body["workspaceId"]),
        cookie=cookie, csrf=str(body["csrfToken"]), client=client)


def _client(port: int) -> httpx.Client:
    return httpx.Client(
        base_url=f"http://127.0.0.1:{port}", trust_env=False,
        timeout=httpx.Timeout(120.0, connect=10.0, read=120.0),
        follow_redirects=False)


def _exchange_invite(port: int, invite: str) -> User:
    client = _client(port)
    try:
        return _login(client, invite)
    except BaseException:
        client.close()
        raise


def _create_member(admin: User, display_name: str) -> dict:
    response = admin.client.post(
        "/api/admin/users", headers=admin.headers(write=True),
        json={"displayName": display_name})
    assert response.status_code == 201, response.status_code
    return response.json()


def _create_invite(admin: User, user_id: str) -> str:
    response = admin.client.post(
        "/api/admin/invites", headers=admin.headers(write=True),
        json={"userId": user_id})
    assert response.status_code == 201, response.status_code
    token = response.json().get("token")
    assert isinstance(token, str) and token
    return token


def _publish_chat(admin: User) -> None:
    response = admin.client.post(
        "/api/admin/managed-config",
        headers={**admin.headers(write=True), "If-Match": "0"},
        json={"defaults": {"chat": {
            "route_kind": "complete",
            "provider": "deepseek",
            "model": PROVIDER_MODEL,
            "base_url": "https://fake-provider.test/v1",
            "protocol": "openai",
            "secret": {"CHAT_API_KEY": SENTINEL_KEY},
            "options": {},
            "allowed_models": [PROVIDER_MODEL],
        }}})
    assert response.status_code == 200, response.status_code


def _create_session(user: User, title: str) -> str:
    response = user.client.post(
        "/api/sessions", headers=user.headers(write=True),
        json={"title": title})
    assert response.status_code == 201, response.status_code
    return str(response.json()["id"])


def _submit(user: User, session_id: str, message: str) -> str:
    response = user.client.post(
        "/api/chat", headers=user.headers(write=True),
        json={"sessionId": session_id,
              "turnId": "a4-" + uuid.uuid4().hex[:16],
              "message": message})
    assert response.status_code == 202, (
        response.status_code, response.text[:500])
    run_id = response.json().get("runId")
    assert isinstance(run_id, str) and run_id
    return run_id


def _runs(user: User, session_id: str) -> dict[str, str]:
    response = user.client.get(
        f"/api/sessions/{session_id}/runs", headers=user.headers())
    assert response.status_code == 200, response.status_code
    return {item["runId"]: item["state"]
            for item in response.json().get("runs", [])}


def _wait_run(user: User, session_id: str, run_id: str,
              states: set[str], timeout: float = 60.0) -> str:
    result: dict[str, str] = {}

    def check() -> bool:
        result.update(_runs(user, session_id))
        return result.get(run_id) in states

    _wait_until(check, timeout=timeout)
    return result[run_id]


def _messages(user: User, session_id: str) -> list[dict]:
    response = user.client.get(
        f"/api/sessions/{session_id}/messages", headers=user.headers())
    assert response.status_code == 200, response.status_code
    messages = response.json().get("messages")
    assert isinstance(messages, list)
    return messages


def _iter_sse(response):
    frame: dict[str, str] = {}
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if frame or data_lines:
                yield {"id": frame.get("id"), "event": frame.get("event"),
                       "data": "\n".join(data_lines)}
            frame = {}
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value.lstrip(" ")
        if name == "data":
            data_lines.append(value)
        else:
            frame[name] = value
    if frame or data_lines:
        yield {"id": frame.get("id"), "event": frame.get("event"),
               "data": "\n".join(data_lines)}


def _collect_until_terminal(user: User, run_id: str,
                            timeout: float = 90.0) -> list[dict]:
    frames: list[dict] = []
    with user.client.stream(
            "GET", f"/api/chat/jobs/{run_id}/stream?after=0",
            headers=user.headers()) as response:
        if response.status_code != 200:
            response.read()
            pytest.fail(f"SSE HTTP {response.status_code}", pytrace=False)
        for frame in _iter_sse(response):
            frames.append(frame)
            if frame.get("event") == "state":
                try:
                    state = json.loads(frame.get("data") or "{}").get("state")
                except ValueError:
                    state = None
                if state in ("done", "failed", "interrupted", "cancelled"):
                    break
    return frames


def _read_question(user: User, run_id: str) -> tuple[str, dict]:
    with user.client.stream(
            "GET", f"/api/chat/jobs/{run_id}/stream?after=0",
            headers=user.headers()) as response:
        if response.status_code != 200:
            response.read()
            pytest.fail(f"SSE HTTP {response.status_code}", pytrace=False)
        for frame in _iter_sse(response):
            if frame.get("event") != "question":
                continue
            try:
                data = json.loads(frame.get("data") or "{}")
            except ValueError:
                continue
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    continue
            if not isinstance(data, dict):
                continue
            questions = data.get("questions")
            record_id = data.get("id")
            if (isinstance(record_id, str) and record_id
                    and isinstance(questions, list) and questions):
                return record_id, questions[0]
    pytest.fail("未收到权限问题事件", pytrace=False)


def _audit(path: pathlib.Path) -> list[dict]:
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _workspace_file(root: pathlib.Path, workspace_id: str,
                    name: str) -> pathlib.Path:
    return root / workspace_id / "workspace" / name


def _prepare_workspace(root: pathlib.Path, workspace_id: str,
                       peer_workspace: str) -> pathlib.Path:
    directory = root / workspace_id / "workspace"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIXTURE, directory / "broker_acp.py")
    (directory / "peer-workspace.txt").write_text(
        peer_workspace, encoding="ascii")
    return directory / "broker-audit.jsonl"


def _write_managed_config(directory: pathlib.Path) -> None:
    (directory / "managed-agent.json").write_text(json.dumps({
        "revision": 1,
        "agent": {
            "runtime": "acp",
            "command": ["python", "/workspace/broker_acp.py",
                        "--audit", "/workspace/broker-audit.jsonl", "acp"],
            "models": ["deepseek-chat", "deepseek-flash",
                       "deepseek-v4-pro"],
            "sandbox": "workspace-write",
            "timeout": 120,
        },
        "broker": {"mode": "workspace-sidecar", "channel": "chat"},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "egress-policy.json").write_text(json.dumps({
        "approved_hosts": ["fake-provider.test"],
        "approved_cidrs": [],
    }), encoding="utf-8")


def _init_volume(image: str, volume: str, source: pathlib.Path,
                 run_id: str) -> None:
    _docker("volume", "create", *_label_args(run_id), volume)
    script = (
        "cp -f /src/* /keys/ && "
        "chown 10001:10001 /keys/* && "
        "chmod 600 /keys/master.key /keys/snapshot.pem "
        "/keys/managed-agent.json /keys/egress-policy.json && "
        "chmod 644 /keys/worker-public.key /keys/gateway-public.pem "
        "/keys/ca.pem")
    _docker("run", "--rm", "--user", "0:0", *_label_args(run_id),
            "-v", f"{volume}:/keys",
            "-v", f"{source}:/src:ro",
            "--entrypoint", "sh", image, "-c", script)


def _control_volume(image: str, volume: str, run_id: str) -> None:
    _docker("volume", "create", *_label_args(run_id), volume)
    _docker("run", "--rm", "--user", "0:0", *_label_args(run_id),
            "-v", f"{volume}:/data", "--entrypoint", "chown",
            image, "10001:10001", "/data")


def _policy_volume(image: str, volume: str, policy: pathlib.Path,
                   run_id: str) -> None:
    _docker("volume", "create", *_label_args(run_id), volume)
    _docker("run", "--rm", "--user", "0:0", *_label_args(run_id),
            "-v", f"{volume}:/policy",
            "-v", f"{policy}:/src/egress-policy.json:ro",
            "--entrypoint", "sh", image, "-c",
            "cp /src/egress-policy.json /policy/egress-policy.json && "
            "chown 10001:10001 /policy/egress-policy.json && "
            "chmod 600 /policy/egress-policy.json")


def _sqlite_active_tokens(control: str, run_id: str) -> int:
    code = (
        "import sqlite3,sys,time;"
        "c=sqlite3.connect('/srv/easel/control/control.sqlite3');"
        "print(c.execute('select count(*) from broker_tokens where run_id=?"
        " and revoked_at is null and expires_at>?', (sys.argv[1], int("
        "time.time()))).fetchone()[0])")
    result = _docker("exec", control, "python", "-c", code, run_id)
    return int(result.stdout.strip())


def _assert_status(response: httpx.Response, status: int) -> None:
    assert response.status_code == status, (
        response.status_code, response.text[:500])


def test_public_control_gateway_sidecar_broker_chain(
        tmp_path: pathlib.Path) -> None:
    control_image = _image_id(CONTROL_TAG)
    gateway_image = _image_id(GATEWAY_TAG)
    worker_image = _image_id(WORKER_TAG)
    print("A4_IMAGES", json.dumps({
        "control": control_image, "gateway": gateway_image,
        "worker": worker_image}, sort_keys=True))
    run_id = _run_id()
    control_network = f"easel-a4-net-{run_id}"
    control_name = f"easel-a4-control-{run_id}"
    egress_name = f"easel-a4-egress-{run_id}"
    fake_name = f"easel-a4-provider-{run_id}"
    control_volume = f"easel-a4-control-data-{run_id}"
    keys_volume = f"easel-a4-keys-{run_id}"
    egress_volume = f"easel-a4-egress-policy-{run_id}"
    root = tmp_path / "workspaces"
    root.mkdir()
    keys = tmp_path / "keys"
    keys.mkdir()
    certs = tmp_path / "certs"
    audit_dir = tmp_path / "provider-audit"
    audit_dir.mkdir()
    provisioner_log = tmp_path / "provisioner.log"
    users: list[User] = []
    provisioner: subprocess.Popen | None = None
    provisioner_socket = f"/tmp/easel-a4-{run_id}/provisioner.sock"

    def cleanup() -> None:
        if os.environ.get("EASEL_A4_KEEP") == "1":
            print("A4_KEEP", control_name, egress_name, fake_name,
                  control_network)
            return
        for user in users:
            workspace = user.workspace_id
            for name in (f"easel-gateway-{workspace}",
                         f"easel-worker-{workspace}",
                         f"easel-sidecar-{workspace}"):
                _docker("container", "rm", "-f", name, check=False)
            _docker("network", "rm", f"easel-ws-{workspace}", check=False)
        for name in (control_name, egress_name, fake_name):
            _docker("container", "rm", "-f", name, check=False)
        labeled = _docker(
            "ps", "-aq", "--filter", f"label={RUN_LABEL}={run_id}",
            check=False).stdout.split()
        for container in labeled:
            _docker("container", "rm", "-f", container, check=False)
        _docker("network", "rm", control_network, check=False)
        for volume in (control_volume, keys_volume, egress_volume):
            _docker("volume", "rm", volume, check=False)
        for user in users:
            user.client.close()
        if provisioner is not None and provisioner.poll() is None:
            provisioner.terminate()
            try:
                provisioner.wait(timeout=10)
            except subprocess.TimeoutExpired:
                provisioner.kill()
                provisioner.wait(timeout=10)
        _wsl("sh", "-c", f"rm -rf {provisioner_socket.rsplit('/', 1)[0]}",
             check=False)

    try:
        _write_ed25519_keys(keys)
        ca_path, _cert_path, _server_key = _write_tls(certs)
        shutil.copy2(ca_path, keys / "ca.pem")
        _write_managed_config(keys)
        (audit_dir / "requests.jsonl").touch()
        _docker("network", "create", *_label_args(run_id), control_network)
        _control_volume(control_image, control_volume, run_id)
        _init_volume(control_image, keys_volume, keys, run_id)
        _policy_volume(control_image, egress_volume,
                       keys / "egress-policy.json", run_id)

        provisioner_command = " ".join([
            ("cd /mnt/c/Code/Easel && "
             "export PYTHONPATH=/mnt/c/Code/Easel && exec python3"),
            "deploy/multiuser/provisioner.py",
            "--socket", provisioner_socket,
            "--workspace-root", _wsl_path(root),
            "--image", worker_image,
            "--allowed-uid", "10001",
            "--quota-mode", "none",
            "--allow-unquotaed",
            "--worker-public-key", _wsl_path(keys / "worker-public.key"),
            "--sidecar-network", control_network,
            "--sidecar-policy", _wsl_path(keys / "egress-policy.json"),
            "--broker-upstream", "control:7861",
            "--gateway-image", gateway_image,
            "--gateway-control-network", control_network,
            "--gateway-public-key", _wsl_path(
                keys / "gateway-public.pem"),
        ])
        with provisioner_log.open("w", encoding="utf-8") as log:
            provisioner = subprocess.Popen(
                [shutil.which("wsl"), "-d", WSL_DISTRO, "--", "bash",
                 "-lc", provisioner_command],
                stdout=log, stderr=subprocess.STDOUT)
        _wait_until(
            lambda: _wsl("test", "-S", provisioner_socket,
                         check=False).returncode == 0,
            timeout=60)

        port = _free_port()
        _wsl_docker(
            "run", "-d", "--name", control_name, *_label_args(run_id),
            "--user", "10001:10001", "--group-add", "1000",
            "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--network", control_network,
            "--network-alias", "control",
            "-p", f"127.0.0.1:{port}:7861",
            "-v", f"{control_volume}:/srv/easel/control",
            "-v", f"{keys_volume}:/keys:ro",
            "-v", f"{provisioner_socket}:/run/easel/provisioner.sock:ro",
            "-e", "SSL_CERT_FILE=/keys/ca.pem",
            control_image,
            "python", "-m", "easel", "serve-control",
            "--control-dir", "/srv/easel/control",
            "--public-origin", ORIGIN,
            "--master-key-file", "/keys/master.key",
            "--snapshot-key-file", "/keys/snapshot.pem",
            "--managed-agent-config", "/keys/managed-agent.json",
            "--frontend-dir", "/opt/easel/web/frontend/dist",
            "--host", "0.0.0.0", "--port", "7861",
            "--provisioner-socket", "/run/easel/provisioner.sock",
            "--gateway-required",
            "--egress-proxy", "http://egress:8899",
            "--egress-policy", "/keys/egress-policy.json",
            "--egress-ca-file", "/keys/ca.pem")

        health_code = (
            "import urllib.request;"
            "urllib.request.urlopen('http://127.0.0.1:7861/api/health',"
            "timeout=3).read()")
        _wait_until(
            lambda: _docker("exec", control_name, "python", "-c",
                            health_code, check=False).returncode == 0,
            timeout=90)

        _docker(
            "run", "-d", "--name", egress_name, *_label_args(run_id),
            "--user", "10001:10001", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--network", control_network, "--network-alias", "egress",
            "-v", f"{egress_volume}:/policy:ro",
            control_image,
            "python", "deploy/multiuser/egress_proxy.py", "--listen",
            "0.0.0.0", "--port", "8899", "--policy",
            "/policy/egress-policy.json")

        _docker(
            "run", "-d", "--name", fake_name, *_label_args(run_id),
            "--user", "0:0",
            "--network", control_network,
            "--network-alias", "fake-provider.test",
            "-v", f"{FAKE_PROVIDER.parent}:/fixture:ro",
            "-v", f"{certs}:/certs:ro",
            "-v", f"{audit_dir}:/audit",
            control_image,
            "python", "/fixture/fake_openai_tls.py", "--port", "443",
            "--cert", "/certs/server.pem", "--key", "/certs/server.key",
            "--audit", "/audit/requests.jsonl")

        provider_health = (
            "import urllib.request;"
            "urllib.request.urlopen('https://fake-provider.test/v1/models',"
            "timeout=3).read()")
        _wait_until(
            lambda: _docker("exec", control_name, "python", "-c",
                            provider_health, check=False).returncode == 0,
            timeout=60)

        bootstrap = _docker(
            "exec", control_name, "python", "-m", "easel", "admin",
            "bootstrap", "--control-dir", "/srv/easel/control",
            "--name", "Admin")
        invite = next(
            (line.split("=", 1)[1] for line in bootstrap.stdout.splitlines()
             if line.startswith("invite=")), "")
        assert invite, "管理员引导未返回邀请"
        admin = _exchange_invite(port, invite)
        users.append(admin)
        alice = _create_member(admin, "Alice")
        bob = _create_member(admin, "Bob")
        alice_invite = _create_invite(admin, alice["userId"])
        bob_invite = _create_invite(admin, bob["userId"])
        user_a = _exchange_invite(port, alice_invite)
        user_b = _exchange_invite(port, bob_invite)
        users.extend([user_a, user_b])
        assert user_a.workspace_id != user_b.workspace_id
        _publish_chat(admin)

        audit_a = _prepare_workspace(root, user_a.workspace_id,
                                     user_b.workspace_id)
        audit_b = _prepare_workspace(root, user_b.workspace_id,
                                     user_a.workspace_id)

        session_a = _create_session(user_a, "A 私有会话")
        session_b = _create_session(user_b, "B 私有会话")

        run_a = _submit(user_a, session_a, "hello from A")
        frames_a = _collect_until_terminal(user_a, run_a)
        assert frames_a and json.loads(
            frames_a[-1]["data"]).get("state") == "done"
        messages_a = _messages(user_a, session_a)
        assert any(message.get("role") == "assistant"
                   and "fake-provider-ok" in str(message.get("content"))
                   for message in messages_a), messages_a

        run_b = _submit(user_b, session_b, "hello from B")
        _collect_until_terminal(user_b, run_b)
        messages_b = _messages(user_b, session_b)
        assert any("fake-provider-ok" in str(message.get("content"))
                   for message in messages_b)
        assert not any("hello from A" in str(message.get("content"))
                       for message in messages_b)
        print("A4_HANDLES", json.dumps([
            {"workspace": workspace,
             "worker": _container_id(f"easel-worker-{workspace}"),
             "gateway": _container_id(f"easel-gateway-{workspace}")}
            for workspace in (user_a.workspace_id, user_b.workspace_id)
        ], sort_keys=True))

        # A 不能读取 B 的会话/消息/Run/SSE。
        _assert_status(user_a.client.get(
            f"/api/sessions/{session_b}/messages",
            headers=user_a.headers()), 404)
        _assert_status(user_a.client.get(
            f"/api/sessions/{session_b}/runs",
            headers=user_a.headers()), 404)
        _assert_status(user_a.client.get(
            f"/api/chat/jobs/{run_b}/stream",
            headers=user_a.headers()), 404)

        audit_lines_a = _audit(audit_a)
        audit_lines_b = _audit(audit_b)
        broker_a = [line for line in audit_lines_a
                    if line.get("event") == "broker"]
        broker_b = [line for line in audit_lines_b
                    if line.get("event") == "broker"]
        assert broker_a and broker_a[-1]["status"] == 200
        assert broker_b and broker_b[-1]["status"] == 200
        assert broker_a[-1]["base"] == (
            f"http://easel-sidecar-{user_a.workspace_id}:8899")
        assert broker_b[-1]["base"] == (
            f"http://easel-sidecar-{user_b.workspace_id}:8899")
        assert broker_a[-1]["base"] != broker_b[-1]["base"]
        cross = [line for line in audit_lines_a
                 if line.get("event") == "broker_cross_workspace"]
        wrong = [line for line in audit_lines_a
                 if line.get("event") == "broker_wrong_channel"]
        assert cross and cross[-1]["status"] == 404
        assert wrong and wrong[-1]["status"] == 404

        provider_requests = _audit(audit_dir / "requests.jsonl")
        provider_chat = [item for item in provider_requests
                         if item.get("path") == "/v1/chat/completions"]
        assert provider_chat
        assert all(
            item.get("authorization") == f"Bearer {SENTINEL_KEY}"
            for item in provider_chat)
        assert all((item.get("body") or {}).get("model") == PROVIDER_MODEL
                   for item in provider_chat)
        assert not any(SENTINEL_KEY in response.text
                       for response in (user_a.client.get(
                           "/api/settings/models",
                           headers=user_a.headers()),))

        # 权限状态/回答经同一 gateway 链路。
        permission_session = _create_session(user_a, "权限会话")
        permission_run = _submit(user_a, permission_session,
                                 "please permission now")
        question_record, question = _read_question(user_a, permission_run)
        options = question.get("options") or []
        assert options
        label = options[0]["label"]
        answer_key = question.get("questionId", "permission")
        status = user_a.client.post(
            "/api/chat/question/status",
            headers=user_a.headers(write=True),
            json={"runId": permission_run,
                  "questionIds": [question_record]})
        assert status.status_code == 200, status.status_code
        assert status.json()["questions"][question_record]["status"] == \
            "pending"
        answered = user_a.client.post(
            "/api/chat/question/answer",
            headers=user_a.headers(write=True),
            json={"runId": permission_run, "questionId": question_record,
                  "answers": {answer_key: [label]}})
        assert answered.status_code == 200, answered.status_code
        _wait_run(user_a, permission_session, permission_run, {"done"},
                  timeout=60)

        # 停止：hang 运行保持执行，公开 stop 经 gateway 后进入 cancelled。
        stop_session = _create_session(user_a, "停止会话")
        stop_run = _submit(user_a, stop_session, "please hang now")
        _wait_run(user_a, stop_session, stop_run, {"running"},
                  timeout=60)
        worker_a = f"easel-worker-{user_a.workspace_id}"
        worker_a_id = _container_id(worker_a)
        stopped = user_a.client.post(
            "/api/chat/stop", headers=user_a.headers(write=True),
            json={"runId": stop_run})
        assert stopped.status_code == 200, stopped.status_code
        _wait_run(user_a, stop_session, stop_run,
                  {"cancelled", "interrupted"}, timeout=60)
        assert _container_running(worker_a)
        assert _container_id(worker_a) == worker_a_id

        # gateway 同容器重启：新 boot，旧断言失效；公开事件/历史不得
        # 结算 Run、撤销 token 或启动新的 ACP 适配器。下一请求触发受限
        # gateway-only 恢复，worker ID 与在途 Run 保持不变。
        restart_session = _create_session(user_a, "gateway 恢复")
        restart_run = _submit(user_a, restart_session,
                              "please hang for gateway")
        _wait_run(user_a, restart_session, restart_run, {"running"},
                  timeout=60)
        _wait_until(
            lambda: any(line.get("event") == "prompt"
                        and line.get("run") == restart_run
                        for line in _audit(audit_a)),
            timeout=30)
        gateway_a = f"easel-gateway-{user_a.workspace_id}"
        gateway_a_id = _container_id(gateway_a)
        initialize_before = len([line for line in _audit(audit_a)
                                 if line.get("event") == "initialize"])
        prompt_before = len([line for line in _audit(audit_a)
                             if line.get("event") == "prompt"])
        tokens_before = _sqlite_active_tokens(control_name, restart_run)
        assert tokens_before == 1
        _docker("restart", gateway_a, timeout=60)
        _wait_until(lambda: _container_running(gateway_a), timeout=60)
        assert _container_id(gateway_a) == gateway_a_id
        history = user_a.client.get(
            f"/api/sessions/{restart_session}/messages",
            headers=user_a.headers())
        assert history.status_code == 200, (
            history.status_code, history.text[:500])
        assert _container_id(worker_a) == worker_a_id
        assert _container_running(worker_a)
        assert _runs(user_a, restart_session).get(restart_run) == "running"
        tokens_after = _sqlite_active_tokens(control_name, restart_run)
        assert tokens_after == tokens_before
        initialize = [line for line in _audit(audit_a)
                      if line.get("event") == "initialize"]
        prompt = [line for line in _audit(audit_a)
                  if line.get("event") == "prompt"]
        assert len(initialize) == initialize_before
        assert len(prompt) == prompt_before
        print("A4_GATEWAY_RECOVERY", json.dumps({
            "workspace": user_a.workspace_id, "worker": worker_a_id,
            "gateway": gateway_a_id, "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "same_worker": _container_id(worker_a) == worker_a_id,
        }, sort_keys=True))
        stopped_restart = user_a.client.post(
            "/api/chat/stop", headers=user_a.headers(write=True),
            json={"runId": restart_run})
        assert stopped_restart.status_code == 200, stopped_restart.status_code
        _wait_run(user_a, restart_session, restart_run,
                  {"cancelled", "interrupted"}, timeout=60)
        assert _container_running(worker_a)
        assert _container_id(worker_a) == worker_a_id

        # gateway 容器被停止：受限恢复在同一 worker 上替换全新 gateway，
        # 不创建 worker，不结算 Run，不撤销 token。
        replace_session = _create_session(user_a, "gateway 替换")
        replace_run = _submit(user_a, replace_session,
                              "please hang for replacement")
        _wait_run(user_a, replace_session, replace_run, {"running"},
                  timeout=60)
        _wait_until(
            lambda: any(line.get("event") == "prompt"
                        and line.get("run") == replace_run
                        for line in _audit(audit_a)),
            timeout=30)
        initialize_before = len([line for line in _audit(audit_a)
                                 if line.get("event") == "initialize"])
        prompt_before = len([line for line in _audit(audit_a)
                             if line.get("event") == "prompt"])
        tokens_before = _sqlite_active_tokens(control_name, replace_run)
        old_gateway_id = _container_id(gateway_a)
        _docker("stop", gateway_a, timeout=60)
        history_replace = user_a.client.get(
            f"/api/sessions/{replace_session}/messages",
            headers=user_a.headers())
        assert history_replace.status_code == 200, (
            history_replace.status_code, history_replace.text[:500])
        new_gateway_id = _container_id(gateway_a)
        assert new_gateway_id != old_gateway_id
        assert _container_id(worker_a) == worker_a_id
        assert _container_running(worker_a)
        assert _runs(user_a, replace_session).get(replace_run) == "running"
        tokens_after = _sqlite_active_tokens(control_name, replace_run)
        assert tokens_after == tokens_before == 1
        assert len([line for line in _audit(audit_a)
                    if line.get("event") == "initialize"]) == initialize_before
        assert len([line for line in _audit(audit_a)
                    if line.get("event") == "prompt"]) == prompt_before
        print("A4_GATEWAY_REPLACEMENT", json.dumps({
            "workspace": user_a.workspace_id, "worker": worker_a_id,
            "old_gateway": old_gateway_id, "new_gateway": new_gateway_id,
            "tokens": tokens_after, "same_worker": True,
        }, sort_keys=True))
        stopped_replace = user_a.client.post(
            "/api/chat/stop", headers=user_a.headers(write=True),
            json={"runId": replace_run})
        assert stopped_replace.status_code == 200,             stopped_replace.status_code
        _wait_run(user_a, replace_session, replace_run,
                  {"cancelled", "interrupted"}, timeout=60)
        assert _container_id(worker_a) == worker_a_id

        # gateway/worker 未发布宿主端口，worker 不能直接访问内部端口。
        ports = json.loads(_docker(
            "container", "inspect", "--format",
            "{{json .NetworkSettings.Ports}}", gateway_a).stdout)
        assert not any(bool(bindings) for bindings in ports.values())
        assert not any(bool(bindings) for bindings in json.loads(_docker(
            "container", "inspect", "--format",
            "{{json .NetworkSettings.Ports}}", worker_a).stdout).values())
        print("A4_PROVIDER", json.dumps({
            "requests": len(provider_chat), "model": PROVIDER_MODEL,
            "sentinel_header_ok": True,
            "cross_workspace": cross[-1]["status"],
            "wrong_channel": wrong[-1]["status"],
        }, sort_keys=True))
        print("A4_REAL_PASS")
    finally:
        cleanup()
