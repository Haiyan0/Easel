"""A6：真实 Harness + 正式前端 + 真实 Chromium 双用户浏览器闭环。

显式 ``EASEL_RUN_CONTAINER_TESTS=1`` 后运行。control/gateway/provisioner/
sidecar/broker 沿用 A4 正式拓扑，worker 使用 A5 固定真实 Harness 镜像；
浏览器为真实 Chromium，两个隔离 context 通过本轮 localhost TLS（SPKI pin）
访问正式构建前端。上游是受控 TLS fake provider；真实 DeepSeek 请求另行
标记，不用 sentinel 成功冒充。
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests" / "frontend"))
import browser_desktop_chat as ui

MANIFEST = ROOT / "deploy" / "multiuser" / "managed" / "acp-profile.json"
FAKE_PROVIDER = ROOT / "tests" / "fixtures" / "fake_openai_tls.py"
TLS_TERMINATOR = ROOT / "tests" / "fixtures" / "tls_terminator.py"
WSL_DISTRO = "Ubuntu-26.04"
RUN_LABEL = "easel.a6.browser"
PROVIDER_MODEL = "deepseek-chat"
CONTROL_TAG = "easel-control-test:a6"
GATEWAY_TAG = "easel-gateway-test:g2"
HARNESS_IMAGE_ID = (
    "sha256:e48eb63b6155b6fc14c37a2525d138e7419f7a8024f46a58d209c40d3a5bd993")
SCREENSHOTS = pathlib.Path(os.environ.get(
    "EASEL_A6_SCREENSHOTS",
    ROOT / "docs" / "evidence" / "desktop-a6" / "screenshots"))
CONTROL_COOKIE = "__Host-easel_session"

pytestmark = pytest.mark.skipif(
    os.environ.get("EASEL_RUN_CONTAINER_TESTS") != "1",
    reason="设置 EASEL_RUN_CONTAINER_TESTS=1 才运行 A6 浏览器闭环测试")


def _load_a4():
    path = pathlib.Path(__file__).with_name("test_desktop_chat_full.py")
    spec = importlib.util.spec_from_file_location("a4_desktop_chat", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


a4 = _load_a4()


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _harness_image() -> str:
    supplied = os.environ.get("EASEL_HARNESS_WORKER_IMAGE")
    reference = supplied or HARNESS_IMAGE_ID
    result = a4._docker(
        "image", "inspect", "--format", "{{.Id}}", reference, check=False)
    image = result.stdout.strip()
    if result.returncode != 0 or image != HARNESS_IMAGE_ID:
        pytest.fail(
            f"A5 固定 Harness 镜像不可用：{reference} -> {image!r}")
    return image


def _message_text(body: object) -> str:
    if not isinstance(body, dict):
        return ""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if (isinstance(block, dict)
                        and isinstance(block.get("text"), str)):
                    parts.append(block["text"])
    return "\n".join(parts)


def _provider_chat(audit: pathlib.Path) -> list[dict]:
    return [line for line in a4._audit(audit)
            if line.get("path") == "/v1/chat/completions"]


def _write_browser_tls(directory: pathlib.Path) -> tuple[pathlib.Path,
                                                         pathlib.Path, str]:
    directory.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=8))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"),
            x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1")),
        ]), critical=False)
        .add_extension(x509.ExtendedKeyUsage(
            [x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256()))
    cert_path = directory / "browser-cert.pem"
    key_path = directory / "browser-key.pem"
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    spki = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    pin = base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")
    return cert_path, key_path, pin


def _wait_tcp(port: int, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    pytest.fail(f"等待端口 {port} 超时")


class Topology:
    """A6 单测次正式装配（control/gateway/A5 worker/egress/fake provider）。"""

    def __init__(self, tmp_path: pathlib.Path, *,
                 public_origin: str | None = None) -> None:
        self.tmp_path = tmp_path
        self.run_id = uuid.uuid4().hex[:12]
        self.control_network = f"easel-a6-net-{self.run_id}"
        self.control_name = f"easel-a6-control-{self.run_id}"
        self.egress_name = f"easel-a6-egress-{self.run_id}"
        self.fake_name = f"easel-a6-provider-{self.run_id}"
        self.terminator_name = f"easel-a6-tls-{self.run_id}"
        self.control_volume = f"easel-a6-control-data-{self.run_id}"
        self.keys_volume = f"easel-a6-keys-{self.run_id}"
        self.egress_volume = f"easel-a6-egress-policy-{self.run_id}"
        self.root = tmp_path / "workspaces"
        self.keys = tmp_path / "keys"
        self.certs = tmp_path / "certs"
        self.browser_certs = tmp_path / "browser-certs"
        self.audit_dir = tmp_path / "provider-audit"
        self.provisioner_log = tmp_path / "provisioner.log"
        self.provisioner_socket = f"/tmp/easel-a6-{self.run_id}/provisioner.sock"
        self.users: list[a4.User] = []
        self.provisioner: subprocess.Popen | None = None
        self.control_image = ""
        self.gateway_image = ""
        self.worker_image = ""
        self._control_keys_volume = ""
        self.extra_volumes: list[str] = []
        self.port = a4._free_port()
        self.tls_port = a4._free_port()
        self.drop_port = a4._free_port()
        self.pin = ""
        self.origin = ""
        self.public_origin_override = public_origin
        self.admin = None
        self.user_a = None
        self.user_b = None
        self.invite_a_browser = ""
        self.invite_a_relogin = ""
        self.invite_a_api = ""
        self.invite_b_browser = ""
        self.invite_b_api = ""
        self.screenshots = SCREENSHOTS

    # -- setup -----------------------------------------------------------
    def start(self) -> Topology:
        self.control_image = a4._image_id(CONTROL_TAG)
        self.gateway_image = a4._image_id(GATEWAY_TAG)
        self.worker_image = _harness_image()
        print("A6_IMAGES", json.dumps({
            "control": self.control_image,
            "gateway": self.gateway_image,
            "worker": self.worker_image}, sort_keys=True))
        self.root.mkdir()
        self.keys.mkdir()
        self.certs.mkdir()
        self.audit_dir.mkdir()
        self.origin = (self.public_origin_override
                       or f"https://localhost:{self.tls_port}")
        a4.ORIGIN = self.origin
        a4._write_ed25519_keys(self.keys)
        ca_path, _cert, _key = a4._write_tls(self.certs)
        shutil.copy2(ca_path, self.keys / "ca.pem")
        self._write_managed_config()
        (self.audit_dir / "requests.jsonl").touch()
        a4._docker("network", "create", "--label",
                   f"{RUN_LABEL}={self.run_id}", self.control_network)
        a4._control_volume(self.control_image, self.control_volume, self.run_id)
        self._control_keys_volume = self.keys_volume
        a4._init_volume(self.control_image, self.keys_volume,
                        self.keys, self.run_id)
        a4._policy_volume(self.control_image, self.egress_volume,
                          self.keys / "egress-policy.json", self.run_id)
        self._start_provisioner()
        self._start_control()
        self._start_egress()
        self._start_fake_provider()
        self._bootstrap_users()
        # 本轮浏览器 TLS：真实 Chromium 用 SPKI pin 限定信任，不全局忽略。
        if self.public_origin_override is None:
            cert, key, self.pin = _write_browser_tls(self.browser_certs)
            self._start_terminator(cert, key)
        return self

    def _write_managed_config(self) -> None:
        manifest = _manifest()
        (self.keys / "managed-agent.json").write_text(json.dumps({
            "revision": 1,
            "agent": {
                "runtime": "acp",
                "command": manifest["command"],
                "models": manifest["provider"]["models"],
                "sandbox": "workspace-write",
                "timeout": 300,
            },
            "broker": manifest["broker"],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.keys / "egress-policy.json").write_text(json.dumps({
            "approved_hosts": ["fake-provider.test"],
            "approved_cidrs": [],
        }), encoding="utf-8")

    def _start_provisioner(self) -> None:
        command = " ".join([
            ("cd /mnt/c/Code/Easel && "
             "export PYTHONPATH=/mnt/c/Code/Easel && exec python3"),
            "deploy/multiuser/provisioner.py",
            "--socket", self.provisioner_socket,
            "--workspace-root", a4._wsl_path(self.root),
            "--image", self.worker_image,
            "--allowed-uid", "10001",
            "--quota-mode", "none",
            "--allow-unquotaed",
            "--worker-public-key", a4._wsl_path(
                self.keys / "worker-public.key"),
            "--sidecar-network", self.control_network,
            "--sidecar-policy", a4._wsl_path(self.keys / "egress-policy.json"),
            "--broker-upstream", "control:7861",
            "--gateway-image", self.gateway_image,
            "--gateway-control-network", self.control_network,
            "--gateway-public-key", a4._wsl_path(
                self.keys / "gateway-public.pem"),
        ])
        with self.provisioner_log.open("w", encoding="utf-8") as log:
            self.provisioner = subprocess.Popen(
                [shutil.which("wsl"), "-d", WSL_DISTRO, "--", "bash",
                 "-lc", command],
                stdout=log, stderr=subprocess.STDOUT)
        a4._wait_until(
            lambda: a4._wsl("test", "-S", self.provisioner_socket,
                            check=False).returncode == 0,
            timeout=60)

    def _start_control(self, *, wait: bool = True) -> None:
        a4._wsl_docker(
            "run", "-d", "--name", self.control_name,
            "--label", f"{RUN_LABEL}={self.run_id}",
            "--user", "10001:10001", "--group-add", "1000",
            "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--network", self.control_network,
            "--network-alias", "control",
            "-p", f"127.0.0.1:{self.port}:7861",
            "-v", f"{self.control_volume}:/srv/easel/control",
            "-v", f"{self._control_keys_volume}:/keys:ro",
            "-v", f"{self.provisioner_socket}:/run/easel/provisioner.sock:ro",
            "-e", "SSL_CERT_FILE=/keys/ca.pem",
            self.control_image,
            "python", "-m", "easel", "serve-control",
            "--control-dir", "/srv/easel/control",
            "--public-origin", self.origin,
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
        if wait:
            self._wait_control_health()

    def _wait_control_health(self, timeout: float = 90) -> None:
        code = (
            "import urllib.request;"
            "urllib.request.urlopen('http://127.0.0.1:7861/api/health',"
            "timeout=3).read()")
        a4._wait_until(
            lambda: a4._docker("exec", self.control_name, "python", "-c",
                               code, check=False).returncode == 0,
            timeout=timeout)

    def _start_egress(self) -> None:
        a4._docker(
            "run", "-d", "--name", self.egress_name,
            "--label", f"{RUN_LABEL}={self.run_id}",
            "--user", "10001:10001", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--network", self.control_network, "--network-alias", "egress",
            "-v", f"{self.egress_volume}:/policy:ro",
            self.control_image,
            "python", "deploy/multiuser/egress_proxy.py", "--listen",
            "0.0.0.0", "--port", "8899", "--policy",
            "/policy/egress-policy.json")

    def _start_fake_provider(self) -> None:
        a4._docker(
            "run", "-d", "--name", self.fake_name,
            "--label", f"{RUN_LABEL}={self.run_id}",
            "--user", "0:0",
            "--network", self.control_network,
            "--network-alias", "fake-provider.test",
            "-v", f"{FAKE_PROVIDER.parent}:/fixture:ro",
            "-v", f"{self.certs}:/certs:ro",
            "-v", f"{self.audit_dir}:/audit",
            self.control_image,
            "python", "/fixture/fake_openai_tls.py", "--port", "443",
            "--cert", "/certs/server.pem", "--key", "/certs/server.key",
            "--audit", "/audit/requests.jsonl")
        code = (
            "import urllib.request;"
            "urllib.request.urlopen('https://fake-provider.test/v1/models',"
            "timeout=3).read()")
        a4._wait_until(
            lambda: a4._docker("exec", self.control_name, "python", "-c",
                               code, check=False).returncode == 0,
            timeout=60)

    def _start_terminator(self, cert: pathlib.Path,
                          key: pathlib.Path) -> None:
        a4._docker(
            "run", "-d", "--name", self.terminator_name,
            "--label", f"{RUN_LABEL}={self.run_id}",
            "--user", "10001:10001", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--network", self.control_network,
            "-p", f"127.0.0.1:{self.tls_port}:8443",
            "-p", f"127.0.0.1:{self.drop_port}:8445",
            "-v", f"{TLS_TERMINATOR.parent}:/fixture:ro",
            "-v", f"{self.browser_certs}:/tls:ro",
            self.control_image,
            "python", "/fixture/tls_terminator.py", "--port", "8443",
            "--cert", "/tls/browser-cert.pem", "--key", "/tls/browser-key.pem",
            "--upstream", "control:7861", "--control-port", "8445")
        _wait_tcp(self.tls_port)

    def drop_browser_transport(self) -> None:
        """关闭本轮所有浏览器 TLS 连接；control/worker 与后台 Run 不受影响。"""
        with socket.create_connection(
                ("127.0.0.1", self.drop_port), timeout=10) as connection:
            connection.sendall(b"drop\n")
            connection.settimeout(10)
            reply = connection.recv(64)
        if b"ok" not in reply:
            pytest.fail(f"终结器 drop 命令失败：{reply!r}")

    def restore_browser_transport(self) -> None:
        _wait_tcp(self.tls_port)

    def _bootstrap_users(self) -> None:
        bootstrap = a4._docker(
            "exec", self.control_name, "python", "-m", "easel", "admin",
            "bootstrap", "--control-dir", "/srv/easel/control",
            "--name", "Admin")
        invite = next(
            (line.split("=", 1)[1] for line in bootstrap.stdout.splitlines()
             if line.startswith("invite=")), "")
        assert invite, "管理员引导未返回邀请"
        self.admin = a4._exchange_invite(self.port, invite)
        self.users.append(self.admin)
        alice = a4._create_member(self.admin, "Alice")
        bob = a4._create_member(self.admin, "Bob")
        self.invite_a_browser = a4._create_invite(
            self.admin, alice["userId"])
        self.invite_a_relogin = a4._create_invite(
            self.admin, alice["userId"])
        self.invite_a_api = a4._create_invite(self.admin, alice["userId"])
        self.invite_b_browser = a4._create_invite(self.admin, bob["userId"])
        self.invite_b_api = a4._create_invite(self.admin, bob["userId"])
        self.user_a = a4._exchange_invite(self.port, self.invite_a_api)
        self.user_b = a4._exchange_invite(self.port, self.invite_b_api)
        self.users.extend([self.user_a, self.user_b])
        assert self.user_a.workspace_id != self.user_b.workspace_id
        a4._publish_chat(self.admin)
        self._prepare_workspace(self.user_a.workspace_id)
        self._prepare_workspace(self.user_b.workspace_id)

    def _prepare_workspace(self, workspace_id: str) -> None:
        directory = self.root / workspace_id / "workspace"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "workspace-marker.txt").write_text(
            workspace_id, encoding="ascii")

    # -- runtime operations ---------------------------------------------
    def token_rows(self, run_ids: list[str]) -> list[dict]:
        code = (
            "import json, sqlite3, sys;"
            "c=sqlite3.connect('/srv/easel/control/control.sqlite3');"
            "rows=c.execute('select run_id, revoked_at from broker_tokens"
            " where run_id in ({}) order by run_id'.format("
            "','.join('?' for _ in sys.argv[1:])), sys.argv[1:]).fetchall();"
            "print(json.dumps([{'run': r[0], 'revoked': r[1] is not None}"
            " for r in rows]))")
        result = a4._docker("exec", self.control_name, "python", "-c",
                            code, *run_ids)
        return json.loads(result.stdout.strip())

    def run_states(self, user, session_id: str) -> dict[str, str]:
        return a4._runs(user, session_id)

    def latest_run(self, user, session_id: str, *,
                   timeout: float = 60.0) -> str:
        found: dict[str, str] = {}

        def check() -> bool:
            found.update(self.run_states(user, session_id))
            return bool(found)

        a4._wait_until(check, timeout=timeout)
        return list(found)[-1]

    def wait_run_state(self, user, session_id: str, run_id: str,
                       states: set[str], timeout: float = 90.0) -> str:
        return a4._wait_run(user, session_id, run_id, states,
                            timeout=timeout)

    def restart_control(self, origin: str | None = None) -> None:
        if origin is not None and origin != self.origin:
            self.origin = origin
            a4.ORIGIN = origin
            a4._docker("container", "rm", "-f", self.control_name, check=False)
            self._start_control()
            return
        a4._docker("restart", self.control_name, timeout=90)
        code = (
            "import urllib.request;"
            "urllib.request.urlopen('http://127.0.0.1:7861/api/health',"
            "timeout=3).read()")
        a4._wait_until(
            lambda: a4._docker("exec", self.control_name, "python", "-c",
                               code, check=False).returncode == 0,
            timeout=90)

    def _alt_keys_volume(self, managed: dict) -> str:
        directory = self.tmp_path / f"alt-keys-{uuid.uuid4().hex[:8]}"
        shutil.copytree(self.keys, directory)
        (directory / "managed-agent.json").write_text(
            json.dumps(managed, ensure_ascii=False, indent=2),
            encoding="utf-8")
        volume = f"easel-a6-keys-alt-{self.run_id}-{len(self.extra_volumes)}"
        a4._init_volume(self.control_image, volume, directory, self.run_id)
        self.extra_volumes.append(volume)
        return volume

    def restart_control_with_managed(self, managed: dict) -> None:
        """用本轮临时受管配置重启 control；DB/密钥/Cookie 保持同一卷。"""
        volume = self._alt_keys_volume(managed)
        a4._docker("container", "rm", "-f", self.control_name, check=False)
        self._control_keys_volume = volume
        self._start_control()

    def probe_control_with_managed(self, managed: dict) -> dict:
        """启动带临时配置的 control，返回是否健康或退出原因（不自动恢复）。"""
        volume = self._alt_keys_volume(managed)
        a4._docker("container", "rm", "-f", self.control_name, check=False)
        self._control_keys_volume = volume
        self._start_control(wait=False)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state = a4._docker(
                "container", "inspect", "--format",
                "{{.State.Running}} {{.State.ExitCode}}",
                self.control_name, check=False).stdout.split()
            if state and state[0] == "false":
                logs = a4._docker("logs", self.control_name, check=False)
                return {
                    "ok": False,
                    "exit": int(state[1]) if len(state) > 1 else None,
                    "log": ((logs.stdout or "") + (logs.stderr or "")),
                }
            if state and state[0] == "true":
                code = (
                    "import urllib.request;"
                    "urllib.request.urlopen("
                    "'http://127.0.0.1:7861/api/health',timeout=3).read()")
                if a4._docker("exec", self.control_name, "python", "-c",
                              code, check=False).returncode == 0:
                    return {"ok": True, "exit": 0, "log": ""}
            time.sleep(0.5)
        return {"ok": False, "exit": None, "log": ""}

    def restore_control_config(self) -> None:
        a4._docker("container", "rm", "-f", self.control_name, check=False)
        self._control_keys_volume = self.keys_volume
        self._start_control()

    def stop(self) -> None:
        for user in self.users:
            workspace = user.workspace_id
            for name in (f"easel-gateway-{workspace}",
                         f"easel-worker-{workspace}",
                         f"easel-sidecar-{workspace}"):
                a4._docker("container", "rm", "-f", name, check=False)
            a4._docker("network", "rm", f"easel-ws-{workspace}", check=False)
        for name in (self.control_name, self.egress_name, self.fake_name,
                     self.terminator_name):
            a4._docker("container", "rm", "-f", name, check=False)
        labeled = a4._docker(
            "ps", "-aq", "--filter", f"label={RUN_LABEL}={self.run_id}",
            check=False).stdout.split()
        for container in labeled:
            a4._docker("container", "rm", "-f", container, check=False)
        a4._docker("network", "rm", self.control_network, check=False)
        for volume in (self.control_volume, self.keys_volume,
                       self.egress_volume, *self.extra_volumes):
            a4._docker("volume", "rm", volume, check=False)
        for user in self.users:
            user.client.close()
        if self.provisioner is not None and self.provisioner.poll() is None:
            self.provisioner.terminate()
            try:
                self.provisioner.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.provisioner.kill()
                self.provisioner.wait(timeout=10)
        a4._wsl("sh", "-c",
                f"rm -rf {self.provisioner_socket.rsplit('/', 1)[0]}",
                check=False)


def _session_dir(topo: Topology, workspace_id: str) -> pathlib.Path:
    return topo.root / workspace_id / "home" / ".dsh" / "sessions"


def _current_prompt_text(body: object) -> str:
    """当前轮 user 文本：跳过 Harness 末尾的 ``Current runtime context`` 消息。"""
    if not isinstance(body, dict):
        return ""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if (isinstance(block, dict)
                        and isinstance(block.get("text"), str)):
                    parts.append(block["text"])
            text = "\n".join(parts)
        if text.lstrip().startswith("Current runtime context"):
            continue
        return text
    return ""


def _chat_requests(topo: Topology, marker: str) -> list[dict]:
    return [item for item in _provider_chat(
                topo.audit_dir / "requests.jsonl")
            if marker in _current_prompt_text(item.get("body"))]


def _wait_new_run(topo: Topology, user, session_id: str,
                  previous: set[str], timeout: float = 90.0) -> str:
    """等待发送后新接纳的 Run，避免把会话里的旧 Run 当成本轮。"""
    latest: dict[str, str] = {}

    def check() -> bool:
        latest.update(topo.run_states(user, session_id))
        return bool(set(latest) - previous)

    a4._wait_until(check, timeout=timeout)
    created = [run_id for run_id in latest if run_id not in previous]
    return created[-1]


def _wait_open_sse(page, tracker, run_id: str,
                   timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tracker.open_for_run(run_id) is not None:
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"未观察在途 SSE 连接：{run_id}")


def _find_session(topo: Topology, user, marker: str,
                  timeout: float = 60.0) -> str:
    """按消息 marker 找到 UI 创建的会话 id（服务端观测，不依赖 DOM）。"""
    found: dict[str, str] = {}

    def check() -> bool:
        payload = user.client.get(
            "/api/sessions", headers=user.headers()).json()
        for item in payload.get("sessions", []):
            messages = a4._messages(user, item["id"])
            if any(marker in str(message.get("content"))
                   for message in messages):
                found["id"] = item["id"]
                return True
        return False

    a4._wait_until(check, timeout=timeout)
    return found["id"]


def _terminal_events(user, run_id: str) -> tuple[list[int], list[str]]:
    frames = a4._collect_until_terminal(user, run_id, timeout=60)
    ids = [int(frame["id"]) for frame in frames
           if isinstance(frame.get("id"), str) and frame["id"].isdigit()]
    states: list[str] = []
    for frame in frames:
        if frame.get("event") != "state":
            continue
        try:
            state = json.loads(frame.get("data") or "{}").get("state")
        except ValueError:
            continue
        if isinstance(state, str):
            states.append(state)
    return ids, states


def test_browser_permission_stop_sse_and_revocation(
        tmp_path: pathlib.Path) -> None:
    topo = Topology(tmp_path)
    try:
        topo.start()
        SCREENSHOTS.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            browser = ui.launch(playwright, pin=topo.pin)
            try:
                context = ui.new_context(browser, topo.origin)
                page = context.new_page()
                tracker = ui.SseTracker()
                tracker.attach(page)
                ui.goto_login(page)
                ui.login(page, topo.invite_a_browser)

                # 1) 工具授权允许：UI 题卡 -> 允许一次 -> 真实执行 echo。
                ui.create_session(page)
                ui.send(page, "harness-tool-allow")
                question = ui.wait_question(page)
                assert "执行授权" in question.text, question.text
                assert len(question.labels) == 2, question.labels
                ui.screenshot(page, SCREENSHOTS / "04-permission.png")
                ui.answer(page, question.labels[0])
                ui.wait_assistant(page, "fake-provider-ok")
                allow_requests = _chat_requests(topo, "harness-tool-allow")
                assert len(allow_requests) == 2, len(allow_requests)
                assert "harness-tool-ok" in _message_text(
                    allow_requests[1].get("body"))
                assert any(message.get("role") == "tool"
                           for message in allow_requests[1]["body"].get(
                               "messages", []))

                # 2) 工具授权拒绝：没有 echo 结果，也没有绕过权限。
                ui.create_session(page)
                ui.send(page, "harness-tool-deny")
                deny_question = ui.wait_question(page)
                assert "执行授权" in deny_question.text
                ui.answer(page, deny_question.labels[1])
                deny_session = _find_session(
                    topo, topo.user_a, "harness-tool-deny")
                a4._wait_run(
                    topo.user_a, deny_session,
                    topo.latest_run(topo.user_a, deny_session),
                    {"done", "failed", "interrupted", "cancelled"},
                    timeout=120)
                deny_requests = _chat_requests(topo, "harness-tool-deny")
                assert deny_requests
                assert not any(
                    "harness-tool-ok" in _message_text(item.get("body"))
                    for item in deny_requests), "拒绝后仍执行了工具"
                assert "harness-tool-ok" not in ui.body_text(page)

                # 3) 同 turn 重试：首次 POST 被浏览器中断，重试只产生一个 Run。
                aborted = {"done": False}

                def abort_first(route):
                    request = route.request
                    if (not aborted["done"] and request.method == "POST"
                            and request.url.endswith("/api/chat")):
                        aborted["done"] = True
                        route.abort()
                    else:
                        route.continue_()

                context.route("**/api/chat", abort_first)
                ui.create_session(page)
                ui.send(page, "harness-retry")
                ui.wait_notice(page, "发送结果未知", timeout=30)
                page.get_by_role(
                    "button", name=re.compile("重试同一请求")).click()
                ui.wait_assistant(page, "fake-provider-ok")
                context.unroute("**/api/chat", abort_first)
                retry_session = _find_session(
                    topo, topo.user_a, "harness-retry")
                retry_runs = topo.run_states(topo.user_a, retry_session)
                assert len(retry_runs) == 1, retry_runs
                retry_run = next(iter(retry_runs))
                a4._wait_run(topo.user_a, retry_session, retry_run,
                             {"done"}, timeout=90)
                assert len(_chat_requests(topo, "harness-retry")) == 1
                retry_ids, retry_states = _terminal_events(
                    topo.user_a, retry_run)
                assert retry_ids == sorted(set(retry_ids)), retry_ids
                assert retry_states.count("done") == 1, retry_states

                # 4) 双击发送：服务端仍只有一个 Run、一次 prompt。
                ui.create_session(page)
                page.get_by_placeholder("输入消息…").fill("harness-double")
                page.get_by_role("button", name=ui.SEND_BUTTON,
                                 exact=True).dblclick()
                ui.wait_assistant(page, "fake-provider-ok")
                double_session = _find_session(
                    topo, topo.user_a, "harness-double")
                assert len(topo.run_states(topo.user_a, double_session)) == 1
                assert len(_chat_requests(topo, "harness-double")) == 1

                # 5) 浏览器侧传输中断后按 after 续接，不重复助手文本。
                ui.create_session(page)
                ui.send(page, "harness-slow")
                slow_session = _find_session(
                    topo, topo.user_a, "harness-slow")
                slow_run = topo.latest_run(topo.user_a, slow_session)
                deadline = time.monotonic() + 60
                while (time.monotonic() < deadline
                       and tracker.open_for_run(slow_run) is None):
                    page.wait_for_timeout(100)
                assert tracker.open_for_run(slow_run) is not None, \
                    "未观察在途 SSE 连接"
                topo.drop_browser_transport()
                ui.wait_notice_any(
                    page, ("连接中断", "连接已断开"), timeout=30)
                topo.restore_browser_transport()
                page.wait_for_timeout(300)
                assert ui.click_reconnect(page, timeout=30), \
                    "传输中断后未提供只回放重连"
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    urls = [record.url for record in tracker.for_run(slow_run)]
                    cursors = [
                        int(match.group(1))
                        for url in urls
                        if (match := re.search(r"[?&]after=(\d+)", url))]
                    if any(value > 0 for value in cursors):
                        break
                    page.wait_for_timeout(100)
                else:
                    raise AssertionError(
                        f"重连未按 after 游标续接：{urls}")
                ui.wait_assistant(page, "slow-end-ok", timeout=120)
                full = [text for text in ui.assistant_articles(page)
                        if "slow-end-ok" in text]
                assert len(full) == 1, full
                a4._wait_run(topo.user_a, slow_session, slow_run,
                             {"done"}, timeout=90)
                assert len(_chat_requests(topo, "harness-slow")) == 1
                slow_ids, slow_states = _terminal_events(
                    topo.user_a, slow_run)
                assert slow_ids == sorted(set(slow_ids))
                assert slow_states.count("done") == 1
                page.reload(wait_until="domcontentloaded")
                page.get_by_role(
                    "button", name=ui.NEW_SESSION).wait_for(timeout=20000)
                ui.wait_assistant(page, "slow-end-ok", timeout=30)
                assert ui.body_text(page).count("slow-end-ok") == 1

                # 6) UI 停止：服务端终态、token 撤销、容量释放。
                ui.create_session(page)
                ui.send(page, "harness-hang")
                hang_session = _find_session(
                    topo, topo.user_a, "harness-hang")
                hang_run = topo.latest_run(topo.user_a, hang_session)
                deadline = time.monotonic() + 60
                while (time.monotonic() < deadline
                       and tracker.open_for_run(hang_run) is None):
                    page.wait_for_timeout(100)
                assert tracker.open_for_run(hang_run) is not None, \
                    "未观察在途 SSE 连接"
                a4._wait_run(topo.user_a, hang_session, hang_run,
                             {"running", "accepted"}, timeout=60)
                deadline = time.monotonic() + 90
                while (time.monotonic() < deadline
                       and not _chat_requests(topo, "harness-hang")):
                    page.wait_for_timeout(200)
                assert _chat_requests(topo, "harness-hang"), \
                    "真实 Harness 尚未发出 provider 请求"
                ui.stop(page)
                hang_state = topo.wait_run_state(
                    topo.user_a, hang_session, hang_run,
                    {"cancelled", "interrupted"}, timeout=90)
                assert hang_state in {"cancelled", "interrupted"}
                page.get_by_role("button", name=ui.STOP_BUTTON,
                                 exact=True).wait_for(
                                     state="hidden", timeout=30000)
                assert len(_chat_requests(topo, "harness-hang")) == 1
                rows = topo.token_rows([hang_run])
                assert rows and rows[0]["revoked"] is True, rows
                worker = f"easel-worker-{topo.user_a.workspace_id}"
                assert a4._container_running(worker)
                ui.send(page, "harness-after-stop")
                ui.wait_assistant(page, "fake-provider-ok")
                after_session = _find_session(
                    topo, topo.user_a, "harness-after-stop")
                after_run = topo.latest_run(topo.user_a, after_session)
                a4._wait_run(topo.user_a, after_session, after_run,
                             {"done"}, timeout=90)

                # 7) 注销在途长流：5 秒内关闭读取，旧 Cookie 后续被拒，
                #    后台 Run 真实完成而不是伪造成 worker done。
                ui.create_session(page)
                ui.send(page, "harness-slow-logout")
                logout_session = _find_session(
                    topo, topo.user_a, "harness-slow-logout")
                logout_run = topo.latest_run(topo.user_a, logout_session)
                deadline = time.monotonic() + 60
                while (time.monotonic() < deadline
                       and tracker.open_for_run(logout_run) is None):
                    page.wait_for_timeout(100)
                active_record = tracker.open_for_run(logout_run)
                assert active_record is not None, "未观察到在途 SSE 连接"
                cookie = {item["name"]: item["value"]
                          for item in context.cookies()}[CONTROL_COOKIE]
                logout_statuses: list[int] = []
                page.on("response", lambda response: (
                    logout_statuses.append(response.status)
                    if response.url.endswith("/api/auth/logout") else None))
                started = time.monotonic()
                ui.logout(page)
                closed: list[float] = []
                while time.monotonic() - started < 8.0:
                    if active_record.ended is not None:
                        closed.append(active_record.ended - started)
                    if active_record.failed is not None:
                        closed.append(active_record.failed - started)
                    if closed:
                        break
                    page.wait_for_timeout(100)
                assert closed and min(closed) <= 5.0, closed
                stale = httpx.Client(
                    base_url=f"http://127.0.0.1:{topo.port}",
                    trust_env=False, timeout=10.0)
                try:
                    deadline = time.monotonic() + 10.0
                    status = 0
                    while time.monotonic() < deadline:
                        status = stale.get(
                            "/api/auth/me",
                            headers={
                                "Cookie": f"{CONTROL_COOKIE}={cookie}",
                                "Origin": topo.origin}).status_code
                        if status == 401:
                            break
                        page.wait_for_timeout(200)
                finally:
                    stale.close()
                assert status == 401, status
                assert logout_statuses and logout_statuses[-1] in (
                    200, 204, 401), logout_statuses

                # B 在同一浏览器 context 登录：看不到 A 的任何内容。
                ui.login(page, topo.invite_b_browser)
                text_b = ui.body_text(page)
                assert "harness-slow-logout" not in text_b
                assert "slow-end-ok" not in text_b

                # 后台 Run 按策略继续，真实到 done 且历史可读。
                a4._wait_run(topo.user_a, logout_session, logout_run,
                             {"done"}, timeout=120)
                messages = a4._messages(topo.user_a, logout_session)
                assert any(message.get("role") == "assistant"
                           and "slow-end-ok" in str(message.get("content"))
                           for message in messages), messages
                assert len(_chat_requests(
                    topo, "harness-slow-logout")) == 1

                print("A6_2_PASS", json.dumps({
                    "allow_requests": len(allow_requests),
                    "deny_requests": len(deny_requests),
                    "retry_runs": len(retry_runs),
                    "slow_events": len(slow_ids),
                    "hang_state": hang_state,
                    "logout_close_seconds": round(min(closed), 2),
                    "screenshots": ["04-permission.png"],
                }, sort_keys=True))
            finally:
                browser.close()
    finally:
        topo.stop()


def test_browser_two_users_chat_refresh_and_isolation(
        tmp_path: pathlib.Path) -> None:
    topo = Topology(tmp_path)
    try:
        topo.start()
        SCREENSHOTS.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            browser = ui.launch(playwright, pin=topo.pin)
            try:
                context_a = ui.new_context(browser, topo.origin)
                page_a = context_a.new_page()
                ui.goto_login(page_a)
                ui.login(page_a, topo.invite_a_browser)
                ui.create_session(page_a)
                ui.send(page_a, "harness-one")
                ui.wait_assistant(page_a, "fake-provider-ok")
                ui.send(page_a, "harness-two")
                ui.wait_assistant_count(page_a, "fake-provider-ok", 2)
                ui.screenshot(page_a, SCREENSHOTS / "01-a-two-rounds.png")

                # 刷新：同一 Cookie/服务端历史恢复，native binding 不变。
                page_a.reload(wait_until="domcontentloaded")
                page_a.get_by_role(
                    "button", name=ui.NEW_SESSION).wait_for(timeout=20000)
                ui.wait_assistant_count(page_a, "fake-provider-ok", 2)
                ui.send(page_a, "harness-after-relogin")
                ui.wait_assistant_count(page_a, "fake-provider-ok", 3)

                # 退出重登：历史仍可读。
                ui.logout(page_a)
                ui.login(page_a, topo.invite_a_relogin)
                page_a.get_by_role("button", name="新对话").first.click()
                ui.wait_assistant_count(page_a, "fake-provider-ok", 3)
                ui.screenshot(page_a, SCREENSHOTS / "02-a-after-relogin.png")

                # B 使用独立浏览器 context 与独立 Cookie。
                context_b = ui.new_context(browser, topo.origin)
                page_b = context_b.new_page()
                ui.goto_login(page_b)
                ui.login(page_b, topo.invite_b_browser)
                ui.create_session(page_b)
                ui.send(page_b, "harness-b1")
                ui.wait_assistant(page_b, "fake-provider-ok")
                ui.send(page_b, "harness-b2")
                ui.wait_assistant_count(page_b, "fake-provider-ok", 2)
                ui.screenshot(page_b, SCREENSHOTS / "03-b-two-rounds.png")

                text_a = ui.body_text(page_a)
                text_b = ui.body_text(page_b)
                for marker in ("harness-b1", "harness-b2"):
                    assert marker not in text_a, marker
                for marker in ("harness-one", "harness-two",
                               "harness-after-relogin"):
                    assert marker not in text_b, marker
                cookies_a = {item["name"]: item["value"]
                             for item in context_a.cookies()}
                cookies_b = {item["name"]: item["value"]
                             for item in context_b.cookies()}
                assert cookies_a[CONTROL_COOKIE] != cookies_b[CONTROL_COOKIE]
            finally:
                browser.close()

        workspace_a = topo.user_a.workspace_id
        workspace_b = topo.user_b.workspace_id
        worker_a = a4._container_id(f"easel-worker-{workspace_a}")
        worker_b = a4._container_id(f"easel-worker-{workspace_b}")
        assert worker_a != worker_b
        sessions_a = _session_dir(topo, workspace_a)
        sessions_b = _session_dir(topo, workspace_b)
        assert sessions_a.is_dir() and any(sessions_a.rglob("*"))
        assert sessions_b.is_dir() and any(sessions_b.rglob("*"))
        assert sessions_a != sessions_b

        # 服务端隔离：A 读 B 的会话/消息/Run/SSE 一律 404。
        session_b = a4._create_session(topo.user_b, "B private")
        run_b = a4._submit(topo.user_b, session_b, "harness-b3")
        a4._collect_until_terminal(topo.user_b, run_b)
        a4._assert_status(topo.user_a.client.get(
            f"/api/sessions/{session_b}/messages",
            headers=topo.user_a.headers()), 404)
        a4._assert_status(topo.user_a.client.get(
            f"/api/sessions/{session_b}/runs",
            headers=topo.user_a.headers()), 404)
        a4._assert_status(topo.user_a.client.get(
            f"/api/chat/jobs/{run_b}/stream",
            headers=topo.user_a.headers()), 404)
        sessions_view_a = topo.user_a.client.get(
            "/api/sessions", headers=topo.user_a.headers()).json()
        sessions_view_b = topo.user_b.client.get(
            "/api/sessions", headers=topo.user_b.headers()).json()
        assert session_b not in [item["id"] for item in
                                 sessions_view_a.get("sessions", [])]
        assert session_b in [item["id"] for item in
                             sessions_view_b.get("sessions", [])]

        requests = _provider_chat(topo.audit_dir / "requests.jsonl")
        for marker in ("harness-one", "harness-two",
                       "harness-after-relogin", "harness-b1", "harness-b2"):
            assert len(_chat_requests(topo, marker)) >= 1, marker
        after = _chat_requests(topo, "harness-after-relogin")[-1]
        resumed = _message_text(after.get("body"))
        assert "harness-one" in resumed and "harness-two" in resumed
        assert "fake-provider-ok" in resumed
        b2 = _chat_requests(topo, "harness-b2")[-1]
        b2_text = _message_text(b2.get("body"))
        assert "harness-b1" in b2_text
        assert "harness-one" not in b2_text
        assert all(item.get("authorization") == f"Bearer {a4.SENTINEL_KEY}"
                   for item in requests)
        print("A6_1_PASS", json.dumps({
            "workspace_a": workspace_a, "workspace_b": workspace_b,
            "worker_a": worker_a, "worker_b": worker_b,
            "provider_chat": len(requests),
            "screenshots": [
                "01-a-two-rounds.png", "02-a-after-relogin.png",
                "03-b-two-rounds.png"],
        }, sort_keys=True))
    finally:
        topo.stop()


def _reconnect_until(page, marker: str, timeout: float = 120.0) -> None:
    """断线后按 UI 的“重连（只回放）”恢复，直到出现目标文本。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(marker in text for text in ui.assistant_articles(page)):
            return
        button = page.get_by_role("button", name=ui.RECONNECT_BUTTON)
        if button.count() > 0 and button.first.is_visible():
            button.first.click()
        page.wait_for_timeout(500)
    raise AssertionError(f"重连后仍未看到：{marker}")


def _notice_texts(page) -> list[str]:
    notices = page.locator("[role=status]")
    return [notices.nth(index).inner_text().strip()
            for index in range(notices.count())]


def _managed_config(topo: Topology) -> dict:
    return json.loads(
        (topo.keys / "managed-agent.json").read_text(encoding="utf-8"))


def _newest_session(user) -> str:
    payload = user.client.get(
        "/api/sessions", headers=user.headers()).json()
    sessions = payload.get("sessions") or []
    assert sessions, "没有会话"
    return str(sessions[0]["id"])


def _open_session_with(page, marker: str, timeout: float = 60.0) -> None:
    """在会话列表里逐个打开，直到页面出现指定历史 marker。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        buttons = page.get_by_role("button", name="新对话")
        for index in range(buttons.count()):
            buttons.nth(index).click()
            page.wait_for_timeout(300)
            if marker in ui.body_text(page):
                return
        page.wait_for_timeout(300)
    raise AssertionError(f"未找到包含 {marker} 的会话")


def test_browser_restart_rebuild_and_failure_semantics(
        tmp_path: pathlib.Path) -> None:
    topo = Topology(tmp_path)
    try:
        topo.start()
        SCREENSHOTS.mkdir(parents=True, exist_ok=True)
        workspace = topo.user_a.workspace_id
        worker_name = f"easel-worker-{workspace}"
        gateway_name = f"easel-gateway-{workspace}"
        with sync_playwright() as playwright:
            browser = ui.launch(playwright, pin=topo.pin)
            try:
                context = ui.new_context(browser, topo.origin)
                page = context.new_page()
                tracker = ui.SseTracker()
                tracker.attach(page)
                ui.goto_login(page)
                ui.login(page, topo.invite_a_browser)

                # 预热：确认真实 Harness/worker 已可用。
                ui.create_session(page)
                ui.send(page, "harness-warm")
                ui.wait_assistant(page, "fake-provider-ok")
                warm_session = _find_session(
                    topo, topo.user_a, "harness-warm")

                # 1) control 重启：Cookie/会话按持久策略保留，历史可读，
                #    在途 Run 后台继续且不重复提交 prompt。
                previous = set(topo.run_states(topo.user_a, warm_session))
                ui.send(page, "harness-slow-control")
                control_run = _wait_new_run(
                    topo, topo.user_a, warm_session, previous)
                _wait_open_sse(page, tracker, control_run)
                a4._wait_run(topo.user_a, warm_session, control_run,
                             {"running", "accepted"}, timeout=60)
                deadline = time.monotonic() + 90
                while (time.monotonic() < deadline
                       and not _chat_requests(topo, "harness-slow-control")):
                    page.wait_for_timeout(200)
                assert _chat_requests(topo, "harness-slow-control")
                control_id = a4._container_id(topo.control_name)
                topo.restart_control()
                ui.wait_notice_any(
                    page, ("连接中断", "连接已断开"), timeout=40)
                page.reload(wait_until="domcontentloaded")
                page.get_by_role(
                    "button", name=ui.NEW_SESSION).wait_for(timeout=30000)
                # control 重启会打断在途 broker 请求：Run 必须如实收敛，
                # 不能伪造 done；账本不重复提交 prompt。
                control_state = a4._wait_run(
                    topo.user_a, warm_session, control_run,
                    {"done", "failed", "interrupted", "cancelled"},
                    timeout=120)
                assert control_state in {
                    "done", "failed", "interrupted", "cancelled"}
                assert len(_chat_requests(
                    topo, "harness-slow-control")) == 1
                if control_state == "done":
                    _reconnect_until(page, "slow-end-ok", timeout=90)
                    assert len([text for text in ui.assistant_articles(page)
                                if "slow-end-ok" in text]) == 1
                else:
                    page.wait_for_timeout(1500)
                    assert not any("slow-end-ok" in text
                                   for text in ui.assistant_articles(page))
                messages = a4._messages(topo.user_a, warm_session)
                assert any("harness-warm" in str(message.get("content"))
                           for message in messages)
                assert any("harness-slow-control" in str(message.get("content"))
                           for message in messages)
                assert a4._container_id(topo.control_name) == control_id
                assert a4._container_running(worker_name)
                # 重启后新 Run 正常接纳（对账与容量继续）。
                previous = set(topo.run_states(topo.user_a, warm_session))
                before_count = sum(
                    1 for text in ui.assistant_articles(page)
                    if "fake-provider-ok" in text)
                ui.send(page, "harness-after-control-restart")
                ui.wait_assistant_count(
                    page, "fake-provider-ok", before_count + 1, timeout=120)
                restart_run = _wait_new_run(
                    topo, topo.user_a, warm_session, previous)
                a4._wait_run(topo.user_a, warm_session, restart_run,
                             {"done"}, timeout=120)

                # 2) gateway 重启：worker ID 不变，原 Run/token 不误撤销，
                #    浏览器按 after 恢复，恢复 POST 不自动重发。
                ui.create_session(page)
                ui.send(page, "harness-slow-gateway")
                gateway_session = _find_session(
                    topo, topo.user_a, "harness-slow-gateway")
                gateway_run = topo.latest_run(
                    topo.user_a, gateway_session)
                _wait_open_sse(page, tracker, gateway_run)
                a4._wait_run(topo.user_a, gateway_session, gateway_run,
                             {"running", "accepted"}, timeout=60)
                deadline = time.monotonic() + 90
                while (time.monotonic() < deadline
                       and not _chat_requests(topo, "harness-slow-gateway")):
                    page.wait_for_timeout(200)
                assert _chat_requests(topo, "harness-slow-gateway")
                worker_before = a4._container_id(worker_name)
                gateway_before = a4._container_id(gateway_name)
                rows = topo.token_rows([gateway_run])
                assert rows and rows[0]["revoked"] is False, rows
                a4._docker("restart", gateway_name, timeout=60)
                a4._wait_until(
                    lambda: a4._container_running(gateway_name), timeout=60)
                # control 会在下一次上游轮询时自行恢复 gateway；SSE 可能
                # 保持或短暂断开，两种路径都必须只回放、不重发。
                _reconnect_until(page, "slow-end-ok", timeout=120)
                assert len([text for text in ui.assistant_articles(page)
                            if "slow-end-ok" in text]) == 1
                assert a4._container_id(worker_name) == worker_before
                assert a4._container_id(gateway_name) == gateway_before
                a4._wait_run(topo.user_a, gateway_session, gateway_run,
                             {"done"}, timeout=120)
                assert len(_chat_requests(
                    topo, "harness-slow-gateway")) == 1
                rows = topo.token_rows([gateway_run])
                assert rows and rows[0]["revoked"] is True, rows

                # 3) worker 消失/重建：旧在途 Run 明确非 done；同一接纳
                #    调用内重建 worker；原会话在新 worker 上续接 native 历史。
                ui.create_session(page)
                ui.send(page, "harness-one-stop")
                ui.wait_assistant(page, "fake-provider-ok")
                worker_session = _find_session(
                    topo, topo.user_a, "harness-one-stop")
                previous = set(topo.run_states(topo.user_a, worker_session))
                ui.send(page, "harness-hang-worker")
                hang_run = _wait_new_run(
                    topo, topo.user_a, worker_session, previous)
                _wait_open_sse(page, tracker, hang_run)
                a4._wait_run(topo.user_a, worker_session, hang_run,
                             {"running", "accepted"}, timeout=60)
                deadline = time.monotonic() + 90
                while (time.monotonic() < deadline
                       and not _chat_requests(topo, "harness-hang-worker")):
                    page.wait_for_timeout(200)
                assert _chat_requests(topo, "harness-hang-worker")
                old_worker = a4._container_id(worker_name)
                a4._docker("rm", "-f", old_worker, timeout=30)
                # 新会话首次发送必须识别实例身份消失：失败旧租约（中断
                # 旧 Run、撤销 token）并在同一调用内创建新 worker。
                ui.create_session(page)
                ui.send(page, "harness-rebuild-probe")
                ui.wait_assistant(page, "fake-provider-ok", timeout=180)
                rebuild_session = _find_session(
                    topo, topo.user_a, "harness-rebuild-probe")
                a4._wait_run(
                    topo.user_a, rebuild_session,
                    topo.latest_run(topo.user_a, rebuild_session),
                    {"done"}, timeout=120)
                new_worker = a4._container_id(worker_name)
                assert new_worker != old_worker
                hang_state = a4._wait_run(
                    topo.user_a, worker_session, hang_run,
                    {"interrupted", "failed", "cancelled"}, timeout=60)
                assert hang_state in {"interrupted", "failed", "cancelled"}
                rows = topo.token_rows([hang_run])
                assert rows and rows[0]["revoked"] is True, rows
                _open_session_with(page, "harness-one-stop")
                previous = set(topo.run_states(topo.user_a, worker_session))
                ui.send(page, "harness-two-worker")
                ui.wait_assistant_count(
                    page, "fake-provider-ok", 2, timeout=120)
                two_run = _wait_new_run(
                    topo, topo.user_a, worker_session, previous)
                a4._wait_run(topo.user_a, worker_session, two_run,
                             {"done"}, timeout=120)
                resumed = _chat_requests(topo, "harness-two-worker")[-1]
                resumed_text = _message_text(resumed.get("body"))
                assert "harness-one-stop" in resumed_text
                assert "fake-provider-ok" in resumed_text
                # 旧 hang Run 没有贡献 assistant；历史 + 新回答恰为 2 条。
                worker_messages = a4._messages(
                    topo.user_a, worker_session)
                assert len([message for message in worker_messages
                            if message.get("role") == "assistant"]) == 2

                # 4) 供应商 401/429/5xx/断流：UI 稳定错误、旧历史不覆盖、
                #    容量可收口；每个模式只有一次 provider 请求。
                ui.create_session(page)
                ui.send(page, "harness-baseline")
                ui.wait_assistant(page, "fake-provider-ok")
                failure_session = _find_session(
                    topo, topo.user_a, "harness-baseline")
                notice_seen: list[str] = []
                for marker in ("harness-401", "harness-429",
                               "harness-500", "harness-drop"):
                    previous = set(topo.run_states(
                        topo.user_a, failure_session))
                    ui.send(page, marker)
                    run = _wait_new_run(
                        topo, topo.user_a, failure_session, previous)
                    a4._wait_run(topo.user_a, failure_session, run,
                                 {"failed", "interrupted"}, timeout=120)
                    page.wait_for_timeout(300)
                    texts = _notice_texts(page)
                    assert texts, f"{marker} 未显示稳定错误"
                    notice_seen.append(texts[-1])
                    assert len(_chat_requests(topo, marker)) == 1
                    assert len([text for text in ui.assistant_articles(page)
                                if "fake-provider-ok" in text]) == 1
                    assert "fake-provider-ok" in ui.body_text(page)
                ui.screenshot(page, SCREENSHOTS / "05-failure.png")
                previous = set(topo.run_states(topo.user_a, failure_session))
                ui.send(page, "harness-after-failures")
                ui.wait_assistant_count(page, "fake-provider-ok", 2)
                after_run = _wait_new_run(
                    topo, topo.user_a, failure_session, previous)
                a4._wait_run(topo.user_a, failure_session, after_run,
                             {"done"}, timeout=120)

                # 5) 错误 broker channel：control 启动期即 fail-closed，
                #    不会带着不可签发的路由接客；恢复后正常。
                bad = _managed_config(topo)
                bad["broker"]["channel"] = "missing-channel"
                probe = topo.probe_control_with_managed(bad)
                assert probe["ok"] is False, probe
                assert probe["exit"] == 2, probe
                assert "managed_config_invalid" in probe["log"], probe
                topo.restore_control_config()
                page.reload(wait_until="domcontentloaded")
                page.get_by_role(
                    "button", name=ui.NEW_SESSION).wait_for(timeout=30000)
                ui.send(page, "harness-after-broker")
                ui.wait_assistant(page, "fake-provider-ok", timeout=120)
                broker_ok_session = _find_session(
                    topo, topo.user_a, "harness-after-broker")
                a4._wait_run(
                    topo.user_a, broker_ok_session,
                    topo.latest_run(topo.user_a, broker_ok_session),
                    {"done"}, timeout=120)

                # 6) 启动失败：受管命令无法启动 Harness；UI 稳定错误，
                #    无 provider 请求，恢复后同会话新 Run 正常。
                bad = _managed_config(topo)
                command = list(bad["agent"]["command"])
                command[-1] = "/opt/easel/managed/missing.patch.yml"
                bad["agent"]["command"] = command
                topo.restart_control_with_managed(bad)
                page.reload(wait_until="domcontentloaded")
                page.get_by_role(
                    "button", name=ui.NEW_SESSION).wait_for(timeout=30000)
                ui.create_session(page)
                startup_session = _newest_session(topo.user_a)
                previous = set(topo.run_states(
                    topo.user_a, startup_session))
                ui.send(page, "harness-startup-fail")
                startup_run = _wait_new_run(
                    topo, topo.user_a, startup_session, previous)
                a4._wait_run(topo.user_a, startup_session, startup_run,
                             {"failed", "interrupted"}, timeout=120)
                ui.wait_notice(page, "Agent 执行失败", timeout=60)
                assert _chat_requests(topo, "harness-startup-fail") == []
                # 等 worker supervisor 收尾释放会话锁后再恢复 control。
                page.wait_for_timeout(3000)
                topo.restore_control_config()
                page.reload(wait_until="domcontentloaded")
                page.get_by_role(
                    "button", name=ui.NEW_SESSION).wait_for(timeout=30000)
                # 若仍撞上 worker 收尾窗口（409），按用户重试语义再发一次；
                # 每轮都是新 turn，不会覆盖/复用失败 Run。
                for attempt in range(3):
                    ui.send(page, "harness-after-startup")
                    try:
                        ui.wait_assistant(
                            page, "fake-provider-ok", timeout=45)
                        break
                    except AssertionError:
                        print("A6_STARTUP_RETRY", json.dumps({
                            "attempt": attempt,
                            "states": topo.run_states(
                                topo.user_a, startup_session),
                            "notices": _notice_texts(page),
                            "body_tail": ui.body_text(page)[-400:],
                            "provider": len(_chat_requests(
                                topo, "harness-after-startup")),
                        }, ensure_ascii=False, sort_keys=True))
                        if attempt == 2:
                            raise
                        page.wait_for_timeout(3000)
                startup_ok_session = _find_session(
                    topo, topo.user_a, "harness-after-startup")
                a4._wait_run(
                    topo.user_a, startup_ok_session,
                    topo.latest_run(topo.user_a, startup_ok_session),
                    {"done"}, timeout=120)

                # 7) 秘密边界：浏览器/日志/worker env 无供应商 Key；
                #    worker/gateway 无发布端口。
                sentinel = a4.SENTINEL_KEY
                assert sentinel not in ui.body_text(page)
                public = [
                    topo.user_a.client.get(
                        "/api/settings/models",
                        headers=topo.user_a.headers()).text,
                    topo.user_a.client.get(
                        "/api/settings/harness",
                        headers=topo.user_a.headers()).text,
                    topo.user_a.client.get(
                        f"/api/sessions/{failure_session}/messages",
                        headers=topo.user_a.headers()).text,
                ]
                assert all(sentinel not in body for body in public)
                logs = a4._docker("logs", topo.control_name,
                                  check=False)
                combined = (logs.stdout or "") + (logs.stderr or "")
                assert sentinel not in combined
                worker_logs = a4._docker("logs", worker_name, check=False)
                worker_text = ((worker_logs.stdout or "")
                               + (worker_logs.stderr or ""))
                assert sentinel not in worker_text
                env = a4._docker("exec", worker_name, "env").stdout
                assert sentinel not in env
                assert "DEEPSEEK_API_KEY" not in env
                assert "CHAT_API_KEY" not in env
                for name in (worker_name, gateway_name):
                    ports = json.loads(a4._docker(
                        "container", "inspect", "--format",
                        "{{json .NetworkSettings.Ports}}", name).stdout)
                    assert not any(bool(bindings)
                                   for bindings in ports.values()), name
                chat = _provider_chat(topo.audit_dir / "requests.jsonl")
                assert chat and all(
                    item.get("authorization") == f"Bearer {sentinel}"
                    for item in chat)

                print("A6_3_PASS", json.dumps({
                    "control_id": control_id,
                    "worker_before": worker_before,
                    "worker_after": new_worker,
                    "gateway_restart_same_id": True,
                    "hang_state": hang_state,
                    "failure_notices": notice_seen,
                    "provider_chat": len(chat),
                    "screenshots": ["05-failure.png"],
                }, sort_keys=True))
            finally:
                browser.close()
    finally:
        topo.stop()


def _start_ngrok(target_port: int) -> tuple[subprocess.Popen, str, list[str]]:
    """启动本轮自有 ngrok 隧道并返回 URL；失败时 skip（不抢占既有隧道）。"""
    binary = shutil.which("ngrok")
    if binary is None:
        pytest.skip("本机没有 ngrok，公网 HTTPS 门槛 not-run")
    check = subprocess.run(
        [binary, "config", "check"], capture_output=True, text=True,
        check=False, timeout=30)
    if check.returncode != 0:
        pytest.skip("ngrok 配置不可用，公网 HTTPS 门槛 not-run")
    running = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "@(Get-Process ngrok -ErrorAction SilentlyContinue).Count"],
        capture_output=True, text=True, check=False, timeout=30)
    if running.stdout.strip() not in ("0", ""):
        pytest.skip("已有 ngrok 隧道在运行，不抢占；公网 HTTPS 门槛 not-run")
    process = subprocess.Popen(
        [binary, "http", f"127.0.0.1:{target_port}",
         "--log=stdout", "--log-format=json"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace")
    lines: list[str] = []

    def reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.append(line)  # noqa: PERF402 - 逐行收集 ngrok 日志

    threading.Thread(target=reader, daemon=True).start()
    deadline = time.monotonic() + 45
    try:
        while time.monotonic() < deadline:
            for line in list(lines):  # noqa: PERF101 - 读取线程并发追加
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                url = data.get("url") if isinstance(data, dict) else None
                if isinstance(url, str) and url.startswith("https://"):
                    return process, url.rstrip("/"), lines
            if process.poll() is not None:
                break
            time.sleep(0.2)
    except BaseException:
        process.terminate()
        raise
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
    pytest.skip("ngrok 隧道不可用（账户/网络限制），公网 HTTPS 门槛 not-run")


def _stop_ngrok(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def test_browser_ngrok_public_https(tmp_path: pathlib.Path) -> None:
    """公网 ngrok HTTPS：公开 CA、严格 Origin、Secure/HttpOnly、CSRF。"""
    topo = Topology(tmp_path, public_origin="https://placeholder.invalid")
    ngrok_process: subprocess.Popen | None = None
    try:
        topo.start()
        ngrok_process, public_url, _lines = _start_ngrok(topo.port)
        topo.restart_control(origin=public_url)
        SCREENSHOTS.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            # 不传 SPKI pin、不设置 ignore_https_errors：公开 CA 必须可信。
            browser = ui.launch(playwright)
            try:
                context = browser.new_context(
                    base_url=public_url, ignore_https_errors=False,
                    extra_http_headers={
                        "ngrok-skip-browser-warning": "true"})
                page = context.new_page()
                ui.goto_login(page)
                ui.login(page, topo.invite_a_browser)
                ui.create_session(page)
                ui.send(page, "harness-ngrok")
                ui.wait_assistant(page, "fake-provider-ok", timeout=120)
                assert page.url.startswith(public_url), page.url
                cookie = next(
                    item for item in context.cookies()
                    if item["name"] == CONTROL_COOKIE)
                assert cookie["secure"] is True
                assert cookie["httpOnly"] is True
                assert cookie["sameSite"] in ("Lax", "Strict")
                ui.screenshot(page, SCREENSHOTS / "06-ngrok-chat.png")
                assert len(_chat_requests(topo, "harness-ngrok")) == 1
                print("A6_4_NGROK_PASS", json.dumps({
                    "public_origin": public_url,
                    "cookie_secure": True,
                    "cookie_http_only": True,
                    "screenshots": ["06-ngrok-chat.png"],
                }, sort_keys=True))
            finally:
                browser.close()
    finally:
        if ngrok_process is not None:
            _stop_ngrok(ngrok_process)
        topo.stop()


def test_real_provider_request_not_run_without_approved_channel() -> None:
    pytest.skip(
        "未提供批准的受管 DeepSeek channel/凭证；真实供应商请求 not-run，"
        "sentinel fake provider 成功不代表真实供应商通过。")
