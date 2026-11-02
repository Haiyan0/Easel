"""Task 6A 隔离整体验收演练（真实部署 + 受控外部层）。

真实层：正式 PowerShell 生命周期、Docker Desktop/WSL provisioner、固定
control/worker/gateway 镜像、原 React 构建、Auth/RunManager/WorkspaceApi、
真实 Harness ACP、两个真实 worker 与持久 HOME、Backup/Verify/Restore/升级。

替身层（显式列出，不冒充）：
- 上游模型：`tests/fixtures/fake_openai_tls.py` 受控 TLS provider；
- 账号/发布：派生 worker 镜像把原平台脚本替换为
  `tests/fixtures/task6a_controlled_platform.py`，不访问真实平台；
- 浏览器 TLS：`tests/fixtures/tls_terminator.py` 仅把 localhost HTTPS
  转发到正式 control，浏览器用本轮 SPKI pin。

真实供应商、真实平台账号/短信/发布、用户实例切换均 not-run。

用法：
    python tests/multiuser/task6a_integration_drill.py \
      --control-image easel-control:task6a-4382180 \
      --worker-image easel-worker:task6a-4382180 \
      --gateway-image easel-gateway:task6a-4382180 \
      --controlled-worker-image easel-worker:task6a-controlled-4382180
"""
from __future__ import annotations

import argparse
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
import tempfile
import time
import traceback
import uuid
import zipfile

import httpx

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "desktop"
FIXTURES = ROOT / "tests" / "fixtures"
FRONTEND = ROOT / "tests" / "frontend"
WSL_DISTRO = "Ubuntu-26.04"
WSL_REPO = "/mnt/c/Code/Easel/.claude/worktrees/original-web-task1-diagnostics"
EVIDENCE = ROOT / "docs" / "evidence" / "original-web-task-06a"
PROVIDER_MODEL = "deepseek-chat"
SENTINEL_KEY = "task6a-controlled-provider-key"
COOKIE = "__Host-easel_session"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8"
    b"\xff\xff?\x00\x05\xfe\x02\xfe\xa75\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")
VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"6a-controlled-video"
SCREENSHOTS: list[str] = []

sys.path.insert(0, str(FRONTEND))
sys.path.insert(0, str(ROOT))
import browser_desktop_chat as ui  # noqa: E402


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


a7 = _load_module(
    "task6a_a7",
    ROOT / "tests/integration/multiuser/test_desktop_operations_real.py")
a4 = a7.a4


# ---------------------------------------------------------------------------
# 基础命令
# ---------------------------------------------------------------------------
def run(argv: list[str], *, timeout: float = 180, check: bool = True,
        log: pathlib.Path | None = None, env: dict | None = None):
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        stream = log.open("ab")
    else:
        stream = subprocess.DEVNULL
    try:
        result = subprocess.run(
            argv, stdout=stream, stderr=subprocess.STDOUT,
            timeout=timeout, check=False, env=env)
    finally:
        if log is not None:
            stream.close()
    if check and result.returncode != 0:
        text = ""
        if log is not None and log.is_file():
            raw = log.read_bytes()
            text = raw.decode("utf-8", "replace")
            if "�" in text:
                text = raw.decode("gbk", "replace")
        raise RuntimeError(
            f"command failed rc={result.returncode}: {' '.join(argv[:6])}\n"
            f"{text[-4000:]}")
    return result


def docker(*args: str, check: bool = True, timeout: float = 180) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(args[:4])} failed:\n{result.stderr[-3000:]}")
    return result.stdout.strip()


def ps_script(name: str, *args: str, log: pathlib.Path,
              timeout: float = 900, check: bool = True):
    return run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(DEPLOY / name), *args],
        log=log, timeout=timeout, check=check)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_host(port: int, timeout: float = 120) -> None:
    import urllib.request
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=3).read()
            return
        except OSError as error:
            last = error
            time.sleep(0.5)
    raise RuntimeError(f"control 端口 {port} 健康检查失败：{last}")


def wsl_path(path: pathlib.Path) -> str:
    text = str(path)
    match = re.match(r"^([A-Za-z]):\\(.*)$", text)
    if match is None:
        raise RuntimeError(f"无法映射到 WSL 路径：{text}")
    return f"/mnt/{match.group(1).lower()}/{match.group(2).replace(chr(92), '/')}"


def image_id(reference: str) -> str:
    value = docker("image", "inspect", "--format", "{{.Id}}", reference)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise RuntimeError(f"镜像不是固定 ID：{reference} -> {value!r}")
    return value


def container_id(name: str) -> str:
    value = docker("container", "inspect", "--format", "{{.Id}}", name,
                   check=False)
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise RuntimeError(f"容器不存在或 ID 非法：{name} -> {value!r}")
    return value


def container_running(name: str) -> bool:
    value = docker("container", "inspect", "--format", "{{.State.Running}}",
                   name, check=False)
    return value == "true"


def copy_evidence(source: pathlib.Path, target: pathlib.Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def write_json(path: pathlib.Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def read_json(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 部署
# ---------------------------------------------------------------------------
class Deployment:
    def __init__(self, label: str, *, control: str, worker: str, gateway: str,
                 publish_mode: str, root: pathlib.Path) -> None:
        self.label = label
        self.run_id = uuid.uuid4().hex[:8]
        self.project = f"easel-6a-{label}-{self.run_id}"
        self.control = control
        self.worker = worker
        self.gateway = gateway
        self.publish_mode = publish_mode
        self.root = root / label
        self.data = self.root / "data"
        self.workspaces = self.root / "workspaces"
        self.certs = self.root / "certs"
        self.config = self.root / "config"
        self.logs = self.root / "logs"
        self.port = free_port()
        self.origin = f"https://localhost:{free_port()}"
        self.network = f"{self.project}-control"
        self.users: list = []
        self.extra_containers: list[str] = []
        self.admin = None
        self.member_a = None
        self.member_b = None
        self.user_a = None
        self.user_b = None
        self.browser_pin = ""
        self._start_provider_done = False
        self._start_terminator_done = False

    # -- lifecycle -------------------------------------------------------
    def _common(self) -> list[str]:
        return [
            "-ProjectName", self.project,
            "-DataRoot", str(self.data),
            "-Distro", WSL_DISTRO,
        ]

    def _start_args(self, *, origin: str | None = None,
                    publish_mode: str | None = None,
                    worker: str | None = None,
                    gateway: str | None = None,
                    extra: list[str] | None = None) -> list[str]:
        args = [
            "-PublicOrigin", origin or self.origin,
            "-Image", self.control,
            "-WorkerImage", worker or self.worker,
            "-GatewayImage", gateway or self.gateway,
            "-ProjectName", self.project,
            "-Port", str(self.port),
            "-Distro", WSL_DISTRO,
            "-WslRepo", WSL_REPO,
            "-WorkspaceRoot", str(self.workspaces),
            "-DataRoot", str(self.data),
            "-ProvisionerGid", "1000",
        ]
        mode = self.publish_mode if publish_mode is None else publish_mode
        if mode:
            args += ["-PublishMode", mode]
        if self.certs.joinpath("ca.pem").is_file():
            args += ["-EgressCaFile", str(self.certs / "ca.pem")]
        if extra:
            args += extra
        return args

    def initialize(self) -> None:
        self.logs.mkdir(parents=True, exist_ok=True)
        managed, policy = self._write_configs()
        ps_script(
            "Initialize-WorkspaceEasel.ps1",
            "-Image", self.control,
            "-ManagedAgentConfig", str(managed),
            "-EgressPolicy", str(policy),
            "-ProjectName", self.project,
            "-DataRoot", str(self.data),
            "-WorkspaceRoot", str(self.workspaces),
            log=self.logs / "initialize.log")

    def start(self, *, publish_mode: str | None = None,
              worker: str | None = None, gateway: str | None = None,
              origin: str | None = None,
              extra: list[str] | None = None,
              check: bool = True,
              log_name: str = "start.log") -> subprocess.CompletedProcess:
        return ps_script(
            "Start-WorkspaceEasel.ps1",
            *self._start_args(
                origin=origin, publish_mode=publish_mode, worker=worker,
                gateway=gateway, extra=extra),
            log=self.logs / log_name, check=check)

    def status(self) -> str:
        result = ps_script(
            "Status-WorkspaceEasel.ps1",
            *self._common(), "-Port", str(self.port),
            "-ProvisionerGid", "1000",
            log=self.logs / "status.log", check=False)
        text = (self.logs / "status.log").read_bytes().decode(
            "utf-8", "replace")
        if result.returncode != 0 or "status=ok" not in text:
            raise RuntimeError(f"Status 未通过：{text[-4000:]}")
        return text

    def stop(self) -> None:
        ps_script(
            "Stop-WorkspaceEasel.ps1",
            *self._common(), "-WslRepo", WSL_REPO,
            log=self.logs / "stop.log")

    def backup(self) -> pathlib.Path:
        destination = self.data / "backups" / (
            "full-" + time.strftime("%Y%m%d-%H%M%S"))
        ps_script(
            "Backup-WorkspaceEasel.ps1",
            "-ProjectName", self.project,
            "-Image", self.control,
            "-DataRoot", str(self.data),
            "-WorkspaceRoot", str(self.workspaces),
            "-Destination", str(destination),
            "-Distro", WSL_DISTRO,
            "-WslRepo", WSL_REPO,
            log=self.logs / "backup.log")
        return destination

    def verify(self, backup: pathlib.Path) -> None:
        ps_script(
            "Verify-WorkspaceEasel.ps1",
            "-Backup", str(backup),
            "-Image", self.control,
            "-Distro", WSL_DISTRO,
            log=self.logs / "verify.log")

    def restore(self, backup: pathlib.Path, *, project: str,
                port: int, origin: str, data: pathlib.Path,
                workspaces: pathlib.Path, worker: str | None = None,
                gateway: str | None = None) -> None:
        self.logs.mkdir(parents=True, exist_ok=True)
        ps_script(
            "Restore-WorkspaceEasel.ps1",
            "-Backup", str(backup),
            "-ProjectName", project,
            "-PublicOrigin", origin,
            "-Image", self.control,
            "-WorkerImage", worker or self.worker,
            "-GatewayImage", gateway or self.gateway,
            "-Port", str(port),
            "-DataRoot", str(data),
            "-WorkspaceRoot", str(workspaces),
            "-Distro", WSL_DISTRO,
            "-WslRepo", WSL_REPO,
            "-ProvisionerGid", "1000",
            log=self.logs / "restore.log")

    def _write_configs(self) -> tuple[pathlib.Path, pathlib.Path]:
        for path in (self.data, self.workspaces, self.config, self.certs):
            path.mkdir(parents=True, exist_ok=True)
        profile = read_json(
            ROOT / "deploy/multiuser/managed/acp-profile.json")
        managed = self.config / "managed-agent.json"
        write_json(managed, {
            "revision": 1,
            "agent": {
                "runtime": "acp",
                "command": profile["command"],
                "models": profile["provider"]["models"],
                "sandbox": "workspace-write",
                "timeout": 300,
            },
            "broker": profile["broker"],
        })
        policy = self.config / "egress-policy.json"
        write_json(policy, {
            "approved_hosts": ["fake-provider.test"],
            "approved_cidrs": [],
        })
        a4._write_tls(self.certs)
        return managed, policy

    # -- controlled external layers -------------------------------------
    def start_provider(self) -> None:
        if self._start_provider_done:
            return
        audit = self.root / "provider-audit"
        audit.mkdir(exist_ok=True)
        (audit / "requests.jsonl").touch()
        name = f"{self.project}-provider"
        docker(
            "run", "-d", "--name", name,
            "--label", f"easel.6a.test={self.project}",
            "--user", "0:0",
            "--network", self.network,
            "--network-alias", "fake-provider.test",
            "-v", f"{FIXTURES}:/fixture:ro",
            "-v", f"{self.certs}:/certs:ro",
            "-v", f"{audit}:/audit",
            self.control, "python", "/fixture/fake_openai_tls.py",
            "--port", "443", "--cert", "/certs/server.pem",
            "--key", "/certs/server.key",
            "--audit", "/audit/requests.jsonl")
        self.extra_containers.append(name)
        code = (
            "import urllib.request;"
            "urllib.request.urlopen('https://fake-provider.test/v1/models',"
            "timeout=3).read()")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["docker", "exec", f"{self.project}-control-1",
                 "python", "-c", code],
                capture_output=True, check=False)
            if probe.returncode == 0:
                self._start_provider_done = True
                return
            time.sleep(1)
        raise RuntimeError("fake provider 不可达")

    def start_terminator(self) -> None:
        if self._start_terminator_done:
            return
        directory = self.root / "browser-certs"
        directory.mkdir(parents=True, exist_ok=True)
        cert, key, pin = write_browser_tls(directory)
        self.browser_pin = pin
        name = f"{self.project}-tls"
        docker(
            "run", "-d", "--name", name,
            "--label", f"easel.6a.test={self.project}",
            "--user", "10001:10001", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--network", self.network,
            "-p", f"127.0.0.1:{self._browser_port}:8443",
            "-v", f"{FIXTURES}:/fixture:ro",
            "-v", f"{directory}:/tls:ro",
            self.control, "python", "/fixture/tls_terminator.py",
            "--port", "8443", "--cert", "/tls/browser-cert.pem",
            "--key", "/tls/browser-key.pem", "--upstream", "control:7861")
        self.extra_containers.append(name)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(
                        ("127.0.0.1", self._browser_port), timeout=1):
                    self._start_terminator_done = True
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("TLS 终结器未就绪")

    # 浏览器访问的 localhost 端口；origin 必须与 control 的 PublicOrigin 一致。
    _browser_port = 0

    def prepare_browser_origin(self) -> None:
        self._browser_port = free_port()
        self.origin = f"https://localhost:{self._browser_port}"

    def bootstrap(self) -> None:
        bootstrap = docker(
            "exec", f"{self.project}-control-1", "python", "-m", "easel",
            "admin", "bootstrap", "--control-dir", "/srv/easel/control",
            "--name", "6A Owner")
        invite = next(
            (line.split("=", 1)[1] for line in bootstrap.splitlines()
             if line.startswith("invite=")), "")
        if not invite:
            raise RuntimeError("bootstrap 未返回管理员邀请")
        a4.ORIGIN = self.origin
        self.admin = a4._exchange_invite(self.port, invite)
        self.users.append(self.admin)
        self.member_a = a4._create_member(self.admin, "6A Alice")
        self.member_b = a4._create_member(self.admin, "6A Bob")
        invite_a = a4._create_invite(self.admin, self.member_a["userId"])
        invite_b = a4._create_invite(self.admin, self.member_b["userId"])
        self.user_a = a4._exchange_invite(self.port, invite_a)
        self.user_b = a4._exchange_invite(self.port, invite_b)
        self.users.extend([self.user_a, self.user_b])
        a4._publish_chat(self.admin)

    def new_invite(self, member: dict, user) -> str:
        del user
        return a4._create_invite(self.admin, member["userId"])

    def seed_workspace(self, workspace_id: str) -> None:
        base = self.workspaces / workspace_id / "workspace"
        outputs = base / "outputs"
        project = outputs / "项目"
        project.mkdir(parents=True, exist_ok=True)
        (project / "封面.png").write_bytes(PNG)
        (project / "clip.mp4").write_bytes(VIDEO)
        # 同名文件/画像用 workspace 标识区分，供 A/B 隔离断言。
        (base / "workspace-marker.txt").write_text(
            workspace_id, encoding="utf-8")
        persona = base / "profiles" / "测试画像"
        persona.mkdir(parents=True, exist_ok=True)
        (persona / "identity.md").write_text(
            f"# 测试画像\n\n{workspace_id}", encoding="utf-8")
        for name in ("style.md", "audience.md", "platforms.md",
                     "preferences.md", "memory.md"):
            (persona / name).write_text(
                f"{workspace_id}-{name}", encoding="utf-8")
        target = datetime.date.today() + datetime.timedelta(days=5)
        write_json(outputs / "_ideas.json", [{
            "id": "6a-idea-a", "title": "6A 初始选题", "note": "初始备注",
            "source": "manual", "status": "pending", "created": 1,
        }])
        write_json(outputs / "_schedule.json", [{
            "id": "6a-schedule-a", "title": "6A 初始排期",
            "date": target.isoformat(), "platform": "微博", "time": "10:00",
            "status": "scheduled", "note": "", "kind": "content",
            "url": "", "source": "manual", "event_type": "", "end_date": "",
        }])

    # -- cleanup ---------------------------------------------------------
    def cleanup(self, evidence_root: pathlib.Path) -> list[str]:
        errors: list[str] = []
        # 只删除带本轮 label 的临时容器。
        for name in self.extra_containers:
            labels = docker(
                "inspect", "--format",
                "{{index .Config.Labels \"easel.6a.test\"}}", name,
                check=False)
            if labels != self.project:
                errors.append(f"refuse foreign container {name}")
                continue
            docker("rm", "-f", name, check=False)
        # compose 容器必须带本项目 label。
        labeled = docker(
            "ps", "-aq", "--filter",
            f"label=com.docker.compose.project={self.project}", check=False)
        for value in labeled.splitlines():
            if value.strip():
                docker("rm", "-f", value.strip(), check=False)
        # 失败路径下 provisioner 可能仍留有 worker/sidecar/gateway；
        # 只按 easel.workspace-id 标签删除本轮两个身份的资源。
        workspace_ids = {
            user.workspace_id for user in (self.user_a, self.user_b)
            if user is not None}
        for workspace_id in workspace_ids:
            for name in (f"easel-worker-{workspace_id}",
                         f"easel-sidecar-{workspace_id}",
                         f"easel-gateway-{workspace_id}"):
                label = docker(
                    "container", "inspect", "--format",
                    "{{index .Config.Labels \"easel.workspace-id\"}}",
                    name, check=False)
                if label == workspace_id:
                    docker("rm", "-f", name, check=False)
                elif label:
                    errors.append(f"refuse foreign workspace container {name}")
            network = f"easel-ws-{workspace_id}"
            raw = docker(
                "network", "inspect", "--format", "{{json .Labels}}",
                network, check=False)
            try:
                labels = json.loads(raw) if raw else {}
            except ValueError:
                labels = {}
            if labels.get("easel.workspace-id") == workspace_id:
                docker("network", "rm", network, check=False)
            elif labels:
                errors.append(f"refuse foreign workspace network {network}")
        # 卷：名字前缀 + 本项目 label（Initialize 使用 easel.desktop.project，
        # compose 资源使用 com.docker.compose.project）双条件。
        volumes = docker("volume", "ls", "--format", "{{.Name}}", check=False)
        for name in volumes.splitlines():
            if not name.startswith(self.project + "-"):
                continue
            raw = docker(
                "volume", "inspect", "--format", "{{json .Labels}}", name,
                check=False)
            try:
                labels = json.loads(raw) if raw else {}
            except ValueError:
                labels = {}
            if (labels.get("easel.desktop.project") == self.project
                    or labels.get("com.docker.compose.project")
                    == self.project):
                docker("volume", "rm", "-f", name, check=False)
            else:
                errors.append(f"refuse foreign volume {name}")
        # 网络：精确名字 + 本项目 label。
        raw = docker(
            "network", "inspect", "--format", "{{json .Labels}}",
            self.network, check=False)
        try:
            labels = json.loads(raw) if raw else {}
        except ValueError:
            labels = {}
        if (labels.get("easel.desktop.project") == self.project
                or labels.get("com.docker.compose.project")
                == self.project):
            docker("network", "rm", self.network, check=False)
        elif labels:
            errors.append(f"refuse foreign network {self.network}")
        # 只终止本项目的 provisioner（按精确 socket 路径），再清理运行目录。
        subprocess.run(
            ["wsl.exe", "-d", WSL_DISTRO, "--", "bash", "-lc",
             "pkill -f 'provisioner.py --socket "
             f"/tmp/easel-runtime/{self.project}/provisioner.sock' || true"],
            capture_output=True, check=False)
        subprocess.run(
            ["wsl.exe", "-d", WSL_DISTRO, "--", "bash", "-lc",
             f"rm -rf '/tmp/easel-runtime/{self.project}'"],
            capture_output=True, check=False)
        for user in self.users:
            try:
                user.client.close()
            except Exception:
                pass
        shutil.rmtree(self.root, ignore_errors=True)
        return errors


def write_browser_tls(directory: pathlib.Path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    directory.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=8))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"),
            x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1")),
        ]), critical=False)
        .sign(key, hashes.SHA256()))
    cert_path = directory / "browser-cert.pem"
    key_path = directory / "browser-key.pem"
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    cert_path.write_bytes(certificate.public_bytes(
        serialization.Encoding.PEM))
    spki = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    pin = base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")
    return cert_path, key_path, pin


# ---------------------------------------------------------------------------
# 证据
# ---------------------------------------------------------------------------
class Evidence:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.results: dict[str, object] = {}
        self.checks: list[dict] = []

    def record(self, name: str, payload: object) -> None:
        self.results[name] = payload
        write_json(self.root / f"{name}.json", payload)

    def check(self, name: str, ok: bool, detail: object = None) -> None:
        entry = {"name": name, "ok": bool(ok), "detail": detail}
        self.checks.append(entry)
        write_json(self.root / "checks.json", self.checks)
        if not ok:
            raise AssertionError(f"{name}: {detail}")

    def screenshot(self, page, name: str) -> None:
        path = self.root / "browser" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # 截图是证据，不是功能门槛；个别页面动画/字体等待不应中止演练。
            page.screenshot(path=str(path), full_page=False, timeout=10000)
            SCREENSHOTS.append(str(path))
        except Exception as error:  # noqa: BLE001
            self.checks.append({
                "name": f"screenshot:{name}", "ok": False,
                "detail": f"截图失败（不中止）：{error}"})
            write_json(self.root / "checks.json", self.checks)

    def soft_check(self, name: str, function) -> bool:
        """执行一个阶段；失败记录 FAIL 但继续后续阶段（不粉饰）。"""
        try:
            detail = function()
            self.checks.append({"name": name, "ok": True, "detail": detail})
            write_json(self.root / "checks.json", self.checks)
            return True
        except BaseException as error:  # noqa: BLE001
            self.checks.append({
                "name": name, "ok": False,
                "detail": f"{type(error).__name__}: {error}"})
            write_json(self.root / "checks.json", self.checks)
            return False

    def finalize(self) -> None:
        write_json(self.root / "checks.json", self.checks)
        write_json(self.root / "screenshots.json", SCREENSHOTS)


# ---------------------------------------------------------------------------
# API/浏览器辅助
# ---------------------------------------------------------------------------
def wait_until(predicate, timeout: float = 60, message: str = "条件",
               interval: float = 0.25) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as error:  # noqa: BLE001 - 等待期间保留最后错误
            last = error
        time.sleep(interval)
    raise RuntimeError(f"等待超时：{message}（{last!r}）")


def find_session(user, marker: str, timeout: float = 60) -> str:
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

    wait_until(check, timeout=timeout, message=f"会话 marker {marker}")
    return found["id"]


def latest_run(user, session: str, timeout: float = 60) -> str:
    latest: dict[str, str] = {}

    def check() -> bool:
        latest.update(a4._runs(user, session))
        return bool(latest)

    wait_until(check, timeout=timeout, message="Run 接纳")
    return list(latest)[-1]


def browser_fetch(page, path: str) -> list:
    return page.evaluate(
        "(p) => fetch(p).then(async (r) => [r.status, await r.text()])",
        path)


def browser_wait_api(page, path: str, timeout: float = 120) -> list:
    """用浏览器自身会话轮询工作区 API，跨过实例收尾窗口的可重试 503。"""
    deadline = time.monotonic() + timeout
    last: list = []
    while time.monotonic() < deadline:
        last = browser_fetch(page, path)
        if last and last[0] == 200:
            return last
        if not last or last[0] not in (429, 503):
            raise RuntimeError(f"浏览器 {path} 返回 {last!r}")
        page.wait_for_timeout(1000)
    raise RuntimeError(f"浏览器 {path} 预热超时：{last!r}")


def wait_ideas_ready(user, timeout: float = 180) -> list:
    """预热 workspace API：首次数据调用会按需启动 worker/gateway。

    control 在启动窗口内可能返回可重试 503；正式页面不应把这种瞬时状态
    当成最终失败。这里先把同一 workspace 的 worker/gateway 预热到可读，
    再进入浏览器验收。
    """
    last: dict = {}

    def check() -> bool:
        response = user.client.get(
            "/api/ideas", headers=user.headers())
        last["status"] = response.status_code
        last["body"] = response.text[:300]
        return response.status_code == 200

    wait_until(check, timeout=timeout, message="workspace ideas 预热")
    return last


def wait_done(user, session: str, run_id: str,
              timeout: float = 180) -> str:
    state = a4._wait_run(
        user, session, run_id,
        {"done", "failed", "interrupted", "cancelled"}, timeout=timeout)
    if state != "done":
        raise RuntimeError(f"Run {run_id} 未完成：{state}")
    return state


def provider_requests(root: pathlib.Path, marker: str) -> list[dict]:
    path = root / "provider-audit" / "requests.jsonl"
    if not path.is_file():
        return []
    found: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("path") != "/v1/chat/completions":
            continue
        if marker in json.dumps(item.get("body"), ensure_ascii=False):
            found.append(item)
    return found


def wait_provider(root: pathlib.Path, marker: str,
                  timeout: float = 90) -> list[dict]:
    wait_until(lambda: provider_requests(root, marker),
               timeout=timeout, message=f"provider 请求 {marker}")
    return provider_requests(root, marker)


def _empty_trends_route(route) -> None:
    """外部热点源的受控替身：不访问真实平台，返回明确空结果。

    工作台与热点页都会请求 /api/trends；受限出口下真实抓取会长时间占用
    workspace 请求窗口，阻塞选题/日历读取。本轮把外部热点源列为替身/not-run。
    """
    route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"trends": [], "stale": False, "updated": 0,
                         "failed": []}, ensure_ascii=False))


def browser_login(page, deployment: Deployment, invite: str) -> None:
    del deployment
    page.goto("/", wait_until="domcontentloaded")
    page.wait_for_selector("#invite", timeout=20000)
    page.fill("#invite", invite)
    page.get_by_role("button", name="进入工作区").click()
    page.locator(".new-chat-btn").wait_for(timeout=30000)


def browser_new_invite(deployment: Deployment, member: dict) -> str:
    return a4._create_invite(deployment.admin, member["userId"])


def open_dashboard(page) -> None:
    page.locator(".nav-item", has_text="工作台").click()
    page.locator(".dash-page").wait_for(timeout=20000)


def open_workbench_tab(page, label: str) -> None:
    """从工作台子页的顶部 SubNav 导航；必要时先进入热点页。

    dashboard 概览在受限出口下会等待外部热点源，其加载状态可能遮住快捷
    按钮；SubNav 在子页可用且不受影响。
    """
    tab = page.locator(".subnav-tab", has_text=label)
    if tab.count() == 0:
        open_dashboard(page)
        page.get_by_role("button", name="看热点").click()
        page.locator(".trends-page").wait_for(timeout=20000)
        tab = page.locator(".subnav-tab", has_text=label)
    # 用 DOM click 绕开工作台加载遮罩/动画对 Playwright 可点击性判断的干扰。
    page.evaluate(
        "(label) => { const el = [...document.querySelectorAll('.subnav-tab')]"
        ".find((node) => node.textContent.includes(label));"
        "if (el) el.click(); }", label)
    page.wait_for_timeout(300)


def open_publish(page) -> None:
    open_workbench_tab(page, "发布中心")
    page.locator(".publishing-workspace").wait_for(timeout=20000)


def open_workbench_marker(page, tab_label: str, selector: str,
                          marker: str, *, attempts: int = 3) -> int:
    """打开工作台子页并等待本人数据；对实例收尾窗口的瞬时 503 做有限重试。"""
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            open_workbench_tab(page, tab_label)
            page.locator(selector).wait_for(timeout=20000)
            page.get_by_text(marker).first.wait_for(timeout=30000)
            return attempt
        except Exception as error:  # noqa: BLE001 - 汇总后重试
            last = error
            if attempt == attempts:
                raise
            page.reload(wait_until="domcontentloaded")
            page.locator(".sidebar").wait_for(timeout=30000)
            page.wait_for_timeout(1000)
    raise RuntimeError(f"页面 {tab_label} 未加载：{last!r}")


def ui_create_session(page) -> None:
    page.locator(".new-chat-btn").click()
    page.locator("textarea.chat-input").wait_for(timeout=20000)
    # 新会话创建后组件会再渲染一次；等输入区稳定，避免刚填入就被重建清空。
    page.wait_for_timeout(600)


def ui_send(page, text: str) -> None:
    last = ""
    for _ in range(12):
        box = page.locator("textarea.chat-input").first
        box.wait_for(timeout=20000)
        box.fill(text)
        page.wait_for_timeout(200)
        last = box.input_value()
        button = page.locator('.send-btn[title="发送"]').first
        if last == text and button.is_enabled():
            button.click()
            return
        page.wait_for_timeout(300)
    raise RuntimeError(f"发送按钮未启用，输入框值={last!r}")


def ui_stop(page) -> None:
    page.locator('.send-btn[title="停止生成"]').click()


def ui_assistant_texts(page) -> list[str]:
    return [text.strip() for text in
            page.locator(".message-bubble.assistant").all_inner_texts()
            if text.strip()]


def ui_wait_assistant(page, marker: str, timeout: float = 120) -> str:
    found: dict[str, str] = {}

    def check() -> bool:
        for text in ui_assistant_texts(page):
            if marker in text:
                found["text"] = text
                return True
        return False

    wait_until(check, timeout=timeout, message=f"助手消息 {marker}")
    return found["text"]


def ui_wait_assistant_count(page, marker: str, count: int,
                            timeout: float = 120) -> None:
    wait_until(
        lambda: sum(1 for text in ui_assistant_texts(page)
                    if marker in text) >= count,
        timeout=timeout, message=f"助手消息 {marker} x{count}")


def ui_wait_question(page, timeout: float = 90):
    card = page.locator(".question-card").first
    card.wait_for(timeout=int(timeout * 1000))
    labels = [text.strip() for text in
              card.locator(".question-card__option strong").all_inner_texts()
              if text.strip()]
    return card, card.inner_text(), labels


def ui_answer(page, label: str) -> None:
    card = page.locator(".question-card").first
    card.locator(".question-card__option").filter(
        has_text=re.compile(re.escape(label))).first.click()
    card.locator(".question-card__submit").click()
    wait_until(lambda: page.locator(".question-card").count() == 0,
               timeout=60, message="题卡提交")


def ui_logout(page) -> None:
    page.locator(".sidebar-logout").click()
    page.wait_for_selector("#invite", timeout=20000)


# ---------------------------------------------------------------------------
# 阶段
# ---------------------------------------------------------------------------
def phase_lifecycle(images, work, evidence: Evidence) -> None:
    """复用已审 A7 正式部署演练：Stop/备份/校验/恢复/升级/回滚。

    只把镜像替换为当前 HEAD 的固定摘要，并把生命周期脚本指向本 worktree
    的 WSL provisioner；断言与流程保持 A7 原样。
    """
    a7.CONTROL_IMAGE = images["control"]
    a7.WORKER_OLD = images["worker"]
    a7.WORKER_NEW = images["controlled_worker"]
    a7.GATEWAY_IMAGE = images["gateway"]
    original_script = a7._script

    def script(name, *args, log, timeout=600):
        extra: list[str] = []
        if name in ("Start-WorkspaceEasel.ps1", "Stop-WorkspaceEasel.ps1",
                    "Backup-WorkspaceEasel.ps1",
                    "Restore-WorkspaceEasel.ps1"):
            extra += ["-Distro", WSL_DISTRO, "-WslRepo", WSL_REPO]
        if name in ("Start-WorkspaceEasel.ps1",
                    "Restore-WorkspaceEasel.ps1"):
            extra += ["-ProvisionerGid", "1000"]
        if name == "Start-WorkspaceEasel.ps1":
            # 5C3 起的发布策略迁移守卫要求旧备份恢复后的首次 Start 显式
            # 声明策略；A7 恢复的是本轮 automated 备份。
            extra += ["-PublishMode", "automated"]
        return original_script(name, *args, *extra, log=log, timeout=timeout)

    a7._script = script
    lifecycle_root = work / "lifecycle"
    lifecycle_root.mkdir(parents=True, exist_ok=True)
    try:
        a7.test_real_backup_restore_upgrade_and_rollback(lifecycle_root)
        evidence.check("lifecycle_backup_restore_upgrade", True,
                       str(lifecycle_root))
    finally:
        a7._script = original_script
        # A7 的 _cleanup 不处理 provisioner 按 workspace 创建的
        # worker/gateway/sidecar；按本轮临时 workspace 根精确清理。
        for workspaces in (lifecycle_root / "source" / "workspaces",
                           lifecycle_root / "restore" / "workspaces"):
            if not workspaces.is_dir():
                continue
            for child in workspaces.iterdir():
                if not child.is_dir() or child.name.startswith("."):
                    continue
                wsid = child.name
                if len(wsid) != 32 or any(
                        character not in "0123456789abcdef"
                        for character in wsid):
                    continue
                for role in ("worker", "gateway", "sidecar"):
                    subprocess.run(
                        ["docker", "rm", "-f", f"easel-{role}-{wsid}"],
                        capture_output=True, check=False)
                subprocess.run(
                    ["docker", "network", "rm", f"easel-ws-{wsid}"],
                    capture_output=True, check=False)
        # A7 的 provisioner 由正式 Start 以后台进程启动；按本轮临时根结束。
        subprocess.run(
            ["wsl.exe", "-d", WSL_DISTRO, "--", "bash", "-lc",
             f"pkill -f '{wsl_path(lifecycle_root)}' || true"],
            capture_output=True, check=False)
        target = evidence.root / "lifecycle"
        target.mkdir(parents=True, exist_ok=True)
        for log_file in lifecycle_root.rglob("*.log"):
            relative = log_file.relative_to(lifecycle_root)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(log_file, destination)
        evidence.record("lifecycle", {
            "control": images["control"],
            "worker_old": images["worker"],
            "worker_new": images["controlled_worker"],
            "gateway": images["gateway"],
            "logs": str(target),
        })


def phase_manual(images, work, evidence: Evidence) -> None:
    """manual 策略、迁移拒绝、登记重启保留、Stop 完成。"""
    evidence.root.mkdir(parents=True, exist_ok=True)
    deployment = Deployment(
        "manual", control=images["control"], worker=images["worker"],
        gateway=images["gateway"], publish_mode="manual", root=work)
    try:
        deployment.initialize()
        deployment.start()
        wait_host(deployment.port)
        deployment.bootstrap()
        a4.ORIGIN = deployment.origin

        policy = deployment.user_a.client.get(
            "/api/publishing-policy",
            headers=deployment.user_a.headers())
        evidence.check("manual_policy_mode",
                       policy.status_code == 200
                       and policy.json().get("mode") == "manual",
                       policy.text[:500])
        rejected = deployment.user_a.client.post(
            "/api/publish/xiaohongshu",
            headers=deployment.user_a.headers(write=True),
            json={"title": "6A manual", "body": "拒绝", "tags": "",
                  "media": ["项目/封面.png"], "persona": "",
                  "requestId": "6a-manual"})
        evidence.check("manual_publish_rejected",
                       rejected.status_code == 403
                       and rejected.json().get("code")
                       == "publishing_manual_only",
                       rejected.text[:500])
        count = docker(
            "exec", f"{deployment.project}-control-1", "python", "-c",
            "import sqlite3;"
            "c=sqlite3.connect('/srv/easel/control/control.sqlite3');"
            "print(c.execute('select count(*) from runs').fetchone()[0])")
        evidence.check("manual_no_run", count.strip() == "0", count)

        deployment.stop()
        # 登记重启：不传 -PublishMode，必须沿用 manual 且正常 Stop。
        deployment.start(publish_mode="")
        status_text = deployment.status()
        evidence.check("manual_registered_restart",
                       "version_publish_mode=manual" in status_text,
                       status_text[-2000:])
        deployment.stop()

        # 旧部署缺策略：删除登记中的 publish_mode 后 Start 必须拒绝。
        deployment_file = deployment.data / "logs" / "deployment.json"
        original = deployment_file.read_text(encoding="utf-8")
        payload = json.loads(original)
        payload.pop("publish_mode", None)
        deployment_file.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            result = deployment.start(
                publish_mode="", check=False,
                log_name="start-migration.log")
            text = (deployment.logs / "start-migration.log").read_bytes(
                ).decode("utf-8", "replace")
            evidence.check(
                "manual_migration_required",
                result.returncode != 0
                and "publish_mode_migration_required" in text,
                text[-2000:])
        finally:
            deployment_file.write_text(original, encoding="utf-8")
        evidence.record("manual", {
            "project": deployment.project,
            "policy": "manual",
            "stop_completed": True,
        })
        copy_evidence(deployment.logs, evidence.root / "manual" / "logs")
    finally:
        copy_evidence(deployment.logs, evidence.root / "manual" / "logs")
        errors = deployment.cleanup(work)
        evidence.check("manual_cleanup", not errors, errors)


def _chat_flow(page, deployment: Deployment, evidence: Evidence) -> None:
    user = deployment.user_a
    ui_create_session(page)
    ui_send(page, "harness-tool-allow")
    card, question_text, labels = ui_wait_question(page)
    evidence.check(
        "chat_permission_card",
        "执行授权" in question_text and len(labels) == 2,
        question_text)
    ui_answer(page, labels[0])
    ui_wait_assistant(page, "fake-provider-ok")
    evidence.screenshot(page, "01-chat-permission-allow.png")

    ui_create_session(page)
    ui_send(page, "harness-hang")
    session = find_session(user, "harness-hang")
    run = latest_run(user, session)
    a4._wait_run(user, session, run, {"running", "accepted"}, timeout=60)
    wait_provider(deployment.root, "harness-hang")
    ui_stop(page)
    state = a4._wait_run(
        user, session, run, {"cancelled", "interrupted"}, timeout=90)
    evidence.check("chat_stop_terminal", state in {"cancelled", "interrupted"},
                   state)
    ui_send(page, "harness-after-stop")
    ui_wait_assistant(page, "fake-provider-ok")
    evidence.screenshot(page, "02-chat-after-stop.png")

    page.reload(wait_until="domcontentloaded")
    page.locator(".new-chat-btn").wait_for(timeout=30000)
    ui_wait_assistant(page, "fake-provider-ok")
    ui_send(page, "harness-after-refresh")
    ui_wait_assistant_count(page, "fake-provider-ok", 2)
    evidence.check("chat_refresh_same_session", True, "same session resumed")
    evidence.screenshot(page, "03-chat-refresh.png")


def _pages_flow(page, deployment: Deployment, evidence: Evidence) -> None:
    open_dashboard(page)
    evidence.check("workbench_dashboard_entry",
                   page.locator(".dash-page").count() == 1,
                   "原工作台概览入口已渲染")
    evidence.screenshot(page, "04-dashboard.png")

    page.get_by_role("button", name="看热点").click()
    page.locator(".trends-page").wait_for(timeout=20000)
    evidence.check("workbench_trends_entry",
                   page.locator(".trends-page").count() == 1,
                   "热点页面已渲染；外部热点源按 not-run 记录")
    evidence.screenshot(page, "05-trends.png")

    open_workbench_tab(page, "选题库")
    page.locator(".ideas-page").wait_for(timeout=20000)
    ideas = browser_wait_api(page, "/api/ideas")
    evidence.check(
        "workbench_seeded_idea",
        ideas[0] == 200 and "6A 初始选题" in ideas[1],
        ideas[1][:500])
    evidence.screenshot(page, "06-ideas.png")

    open_workbench_tab(page, "内容日历")
    page.locator(".calendar-page").wait_for(timeout=20000)
    schedule = browser_wait_api(page, "/api/schedule")
    evidence.check(
        "workbench_seeded_schedule",
        schedule[0] == 200 and "6A 初始排期" in schedule[1],
        schedule[1][:500])
    evidence.screenshot(page, "07-calendar.png")

    page.locator(".nav-item", has_text="内容库").click()
    page.locator(".gcard", has_text="项目").first.wait_for(timeout=20000)
    outputs = browser_wait_api(page, "/api/outputs")
    evidence.check(
        "media_preview_download",
        outputs[0] == 200 and "封面.png" in outputs[1],
        outputs[1][:800])
    media_status = page.evaluate(
        "() => fetch('/api/media/' + encodeURIComponent('项目/封面.png'))"
        ".then(r => r.status)")
    evidence.check("media_bytes", media_status == 200, media_status)
    upload = page.evaluate(
        "async () => { const me = await (await fetch('/api/auth/me'))"
        ".json();"
        " const sessions = await (await fetch("
        "'/api/sessions?limit=1&offset=0')).json();"
        " const sid = (sessions.sessions || [])[0]?.id || '';"
        " const fd = new FormData();"
        " fd.append('files', new File(['6A upload'], '6a-upload.txt',"
        " {type: 'text/plain'}));"
        " fd.append('sessionId', sid);"
        " const r = await fetch('/api/upload', {method: 'POST',"
        " headers: {'X-CSRF-Token': me.csrfToken || ''}, body: fd});"
        " return [r.status, await r.text()]; }")
    evidence.check("material_upload", upload and upload[0] == 200,
                   upload)
    evidence.screenshot(page, "08-outputs.png")

    page.locator(".nav-item", has_text="画像").click()
    page.locator(".profile-page").wait_for(timeout=20000)
    # 画像下拉框里也有隐藏的 <option>测试画像</option>，必须等待可见的画像卡。
    page.locator(".profile-summary", has_text="测试画像").first.wait_for(
        timeout=20000)
    evidence.check("profile_entry", True, "测试画像")
    evidence.screenshot(page, "09-profile.png")

    page.locator(".nav-item", has_text="技能库").click()
    page.locator(".skills-page").wait_for(timeout=20000)
    card = page.locator(".skill-card-rawname").filter(
        has_text="copywriting").first
    card.wait_for(timeout=20000)
    card.click()
    page.locator(".drawer").wait_for(timeout=20000)
    page.locator(".drawer textarea").fill("6A 受控技能测试")
    page.get_by_role("button", name="执行").click()
    page.get_by_text("fake-provider-ok").first.wait_for(timeout=120000)
    evidence.check("skill_run_completed", True, "fake-provider-ok")
    evidence.check("skill_page_entry", True, "技能库")
    evidence.screenshot(page, "10-skill.png")
    body = page.text_content("body") or ""
    evidence.check(
        "no_unavailable_pages",
        "功能未接通" not in body and "暂未接通" not in body,
        "已访问入口无过时未接通占位")


def _manual_export_flow(page, deployment: Deployment,
                        evidence: Evidence) -> None:
    open_publish(page)
    page.get_by_role("button", name="人工审核发布").click()
    page.locator("#manual-title").wait_for(timeout=20000)
    page.get_by_role("button", name="抖音", exact=True).click()
    page.fill("#manual-title", "6A 人工稿件")
    page.fill("#manual-body", "6A 人工正文第一段\n\n第二段")
    page.fill("#manual-tags", "6A,集成")
    page.get_by_role("button", name="选择媒体").click()
    page.locator(".media-grid").wait_for(timeout=20000)
    cell = page.locator('.media-cell[title="项目/封面.png"]').first
    cell.wait_for(timeout=20000)
    cell.click()
    card = page.locator(".pv-card", has_text="小红书")
    card.get_by_label("小红书人工审核").check()
    card.get_by_text("已人工审核").wait_for(timeout=15000)
    export = card.get_by_role("button", name="导出稿件和素材 ZIP")
    with page.expect_download() as download_info:
        export.click()
    download = download_info.value
    package = download.path()
    target = evidence.root / "export" / "6a-manual-export.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(package, target)
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        evidence.check(
            "manual_export_zip",
            {"title.txt", "manifest.json", "body.txt"} <= names
            and manifest.get("status") == "ready_for_manual_submission"
            and manifest.get("platform") == "xiaohongshu",
            {"names": sorted(names), "manifest": manifest})
    evidence.screenshot(page, "11-manual-export.png")


def _identity_flow(page, context, deployment: Deployment, evidence: Evidence,
                   invite_b: str, invite_a_relogin: str) -> str:
    user = deployment.user_a
    ui_create_session(page)
    ui_send(page, "6A-A-private")
    ui_wait_assistant(page, "fake-provider-ok")
    session_a = find_session(user, "6A-A-private")
    cookie_a = {item["name"]: item["value"]
                for item in context.cookies()}[COOKIE]

    ui_logout(page)
    browser_login(page, deployment, invite_b)
    body_b = page.text_content("body") or ""
    evidence.check("identity_b_no_a_cache", "6A-A-private" not in body_b,
                   body_b[-1000:])
    ui_create_session(page)
    ui_send(page, "6A-B-private")
    ui_wait_assistant(page, "fake-provider-ok")
    body_b = page.text_content("body") or ""
    evidence.check("identity_b_own_session", "6A-B-private" in body_b,
                   body_b[-1000:])
    session_b = find_session(deployment.user_b, "6A-B-private")
    messages_a = a4._messages(user, session_a)
    messages_b = a4._messages(deployment.user_b, session_b)
    evidence.check(
        "identity_server_isolation",
        all("6A-B-private" not in str(item.get("content"))
            for item in messages_a)
        and all("6A-A-private" not in str(item.get("content"))
                for item in messages_b),
        {"a": len(messages_a), "b": len(messages_b)})
    worker_a = container_id(
        f"easel-worker-{deployment.user_a.workspace_id}")
    worker_b = container_id(
        f"easel-worker-{deployment.user_b.workspace_id}")
    evidence.check(
        "identity_two_workers",
        worker_a != worker_b
        and deployment.user_a.workspace_id
        != deployment.user_b.workspace_id,
        {"worker_a": worker_a, "worker_b": worker_b})
    home_a = deployment.workspaces / deployment.user_a.workspace_id / "home"
    home_b = deployment.workspaces / deployment.user_b.workspace_id / "home"
    evidence.check(
        "identity_home",
        home_a.is_dir() and home_b.is_dir() and home_a != home_b,
        [str(home_a), str(home_b)])
    root_a = deployment.workspaces / deployment.user_a.workspace_id / "workspace"
    root_b = deployment.workspaces / deployment.user_b.workspace_id / "workspace"
    marker_a = (root_a / "workspace-marker.txt").read_text(encoding="utf-8")
    marker_b = (root_b / "workspace-marker.txt").read_text(encoding="utf-8")
    identity_a = (root_a / "profiles/测试画像/identity.md").read_text(
        encoding="utf-8")
    identity_b = (root_b / "profiles/测试画像/identity.md").read_text(
        encoding="utf-8")
    evidence.check(
        "identity_same_name_storage",
        marker_a == deployment.user_a.workspace_id
        and marker_b == deployment.user_b.workspace_id
        and marker_a != marker_b
        and deployment.user_a.workspace_id in identity_a
        and deployment.user_b.workspace_id in identity_b
        and identity_a != identity_b,
        {"a": marker_a, "b": marker_b})

    ui_logout(page)
    browser_login(page, deployment, invite_a_relogin)
    body = page.text_content("body") or ""
    evidence.check("identity_a_relogin", "6A-B-private" not in body,
                   body[-1000:])
    return cookie_a


def _worker_restart_flow(page, deployment: Deployment,
                         evidence: Evidence) -> None:
    user = deployment.user_a
    ui_create_session(page)
    ui_send(page, "harness-one-stop")
    ui_wait_assistant(page, "fake-provider-ok")
    session = find_session(user, "harness-one-stop")
    sentinel = (deployment.workspaces / user.workspace_id / "workspace"
                / "workspace-6a-sentinel.txt")
    sentinel.write_text("6a-sentinel", encoding="utf-8")
    previous = set(a4._runs(user, session))
    ui_send(page, "harness-hang-worker")
    wait_until(lambda: set(a4._runs(user, session)) - previous,
               timeout=60, message="新 hang Run")
    hang_run = (set(a4._runs(user, session)) - previous).pop()
    a4._wait_run(user, session, hang_run, {"running", "accepted"}, timeout=60)
    wait_provider(deployment.root, "harness-hang-worker")
    worker_name = f"easel-worker-{user.workspace_id}"
    old_worker = container_id(worker_name)
    docker("rm", "-f", old_worker, timeout=30)

    rebuild_session = a4._create_session(user, "6A rebuild")
    rebuild_run = a4._submit(user, rebuild_session, "harness-rebuild-probe")
    wait_done(user, rebuild_session, rebuild_run)
    new_worker = container_id(worker_name)
    hang_state = a4._wait_run(
        user, session, hang_run, {"interrupted", "failed", "cancelled"},
        timeout=90)
    evidence.check(
        "worker_restart_no_replay",
        old_worker != new_worker
        and hang_state in {"interrupted", "failed", "cancelled"},
        {"old": old_worker, "new": new_worker, "hang": hang_state})

    follow_run = a4._submit(user, session, "harness-two-worker")
    wait_done(user, session, follow_run)
    resumed = provider_requests(deployment.root, "harness-two-worker")
    resumed_text = json.dumps(
        (resumed[-1].get("body") if resumed else {}), ensure_ascii=False)
    evidence.check(
        "worker_restart_session_resume",
        "harness-one-stop" in resumed_text
        and "fake-provider-ok" in resumed_text,
        resumed_text[-1000:])
    evidence.check(
        "worker_restart_files",
        sentinel.is_file()
        and sentinel.read_text(encoding="utf-8") == "6a-sentinel",
        str(sentinel))
    evidence.screenshot(page, "12-worker-restart.png")


def _logout_invite_flow(page, context, deployment: Deployment,
                        evidence: Evidence, used_invite: str,
                        fresh_invite: str) -> None:
    cookie_value = {item["name"]: item["value"]
                    for item in context.cookies()}[COOKIE]
    ui_logout(page)
    stale = httpx.Client(
        base_url=f"http://127.0.0.1:{deployment.port}", trust_env=False,
        timeout=15.0, follow_redirects=False)
    try:
        wait_until(
            lambda: stale.get(
                "/api/auth/me",
                headers={"Cookie": f"{COOKIE}={cookie_value}",
                         "Origin": deployment.origin}).status_code == 401,
            timeout=15, message="旧 Cookie 撤销")
    finally:
        stale.close()
    exchange = httpx.Client(
        base_url=f"http://127.0.0.1:{deployment.port}", trust_env=False,
        timeout=15.0, follow_redirects=False)
    try:
        reused = exchange.post(
            "/api/auth/exchange", headers={"Origin": deployment.origin},
            json={"token": used_invite})
        evidence.check("invite_one_time", reused.status_code != 200,
                       reused.status_code)
    finally:
        exchange.close()
    browser_login(page, deployment, fresh_invite)
    evidence.check("logout_relogin", True, "fresh invite accepted")
    evidence.screenshot(page, "13-relogin.png")


def phase_main(images, work, evidence: Evidence) -> None:
    deployment = Deployment(
        "main", control=images["control"], worker=images["worker"],
        gateway=images["gateway"], publish_mode="automated", root=work)
    deployment.prepare_browser_origin()
    try:
        deployment.initialize()
        deployment.start()
        wait_host(deployment.port)
        # 先做一次只读 Status：此时 provisioner socket 刚建立，避免 worker
        # 动态启动期间主机侧 socket 检查的瞬时竞态。
        status_text = deployment.status()
        evidence.check("main_status_ok", "status=ok" in status_text,
                       status_text[-2000:])
        evidence.check("main_publish_mode_automated",
                       "version_publish_mode=automated" in status_text,
                       status_text[-2000:])
        deployment.start_provider()
        deployment.start_terminator()
        deployment.bootstrap()
        deployment.seed_workspace(deployment.user_a.workspace_id)
        deployment.seed_workspace(deployment.user_b.workspace_id)

        invite_a_browser = browser_new_invite(deployment, deployment.member_a)
        invite_b_browser = browser_new_invite(deployment, deployment.member_b)
        invite_a_relogin = browser_new_invite(deployment, deployment.member_a)
        invite_a_final = browser_new_invite(deployment, deployment.member_a)

        # HTTP 基线：A/B 各一条真实 Harness 消息，确认 provider 与隔离。
        session_a0 = a4._create_session(deployment.user_a, "6A A base")
        run_a0 = a4._submit(deployment.user_a, session_a0, "6A-base-A")
        wait_done(deployment.user_a, session_a0, run_a0)
        session_b0 = a4._create_session(deployment.user_b, "6A B base")
        run_b0 = a4._submit(deployment.user_b, session_b0, "6A-base-B")
        wait_done(deployment.user_b, session_b0, run_b0)
        evidence.check(
            "http_chat_two_users",
            bool(provider_requests(deployment.root, "6A-base-A"))
            and bool(provider_requests(deployment.root, "6A-base-B")),
            {"a": len(provider_requests(deployment.root, "6A-base-A")),
             "b": len(provider_requests(deployment.root, "6A-base-B"))})
        evidence.record("workspace_api_warmup",
                        wait_ideas_ready(deployment.user_a))
        wait_ideas_ready(deployment.user_b)

        with sync_playwright() as playwright:
            browser = ui.launch(playwright, pin=deployment.browser_pin)
            try:
                context = ui.new_context(browser, deployment.origin)
                page = context.new_page()
                page.set_default_timeout(20000)
                page.route("**/api/trends*", _empty_trends_route)
                try:
                    browser_login(page, deployment, invite_a_browser)

                    def warmup_after_chat():
                        # 聊天/停止/刷新后再次确认 workspace API 可读，
                        # 避免把实例收尾窗口的瞬时 503 当成页面失败。
                        evidence.record(
                            "workspace_api_after_chat",
                            wait_ideas_ready(deployment.user_a))
                        evidence.record(
                            "browser_api_after_chat",
                            browser_wait_api(page, "/api/ideas"))

                    for name, action in [
                        ("stage:chat", lambda: _chat_flow(
                            page, deployment, evidence)),
                        ("stage:workspace_api_after_chat", warmup_after_chat),
                        ("stage:pages", lambda: _pages_flow(
                            page, deployment, evidence)),
                        ("stage:manual_export", lambda: _manual_export_flow(
                            page, deployment, evidence)),
                        ("stage:identity", lambda: _identity_flow(
                            page, context, deployment, evidence,
                            invite_b_browser, invite_a_relogin)),
                        ("stage:worker_restart", lambda: _worker_restart_flow(
                            page, deployment, evidence)),
                        ("stage:logout_invite", lambda: _logout_invite_flow(
                            page, context, deployment, evidence,
                            invite_a_browser, invite_a_final)),
                    ]:
                        evidence.soft_check(name, action)
                except BaseException:
                    try:
                        evidence.screenshot(page, "99-main-failure.png")
                    except Exception:
                        pass
                    raise
            finally:
                browser.close()

        evidence.record("main", {
            "project": deployment.project,
            "origin": deployment.origin,
            "workspace_a": deployment.user_a.workspace_id,
            "workspace_b": deployment.user_b.workspace_id,
            "worker_a": container_id(
                f"easel-worker-{deployment.user_a.workspace_id}"),
            "worker_b": container_id(
                f"easel-worker-{deployment.user_b.workspace_id}"),
        })
        copy_evidence(deployment.logs, evidence.root / "main" / "logs")
        copy_evidence(deployment.root / "provider-audit" / "requests.jsonl",
                      evidence.root / "main" / "provider-audit.jsonl")
    finally:
        # 失败路径也保留日志与 provider 审计，便于复审定位；不含真实秘密。
        copy_evidence(deployment.logs, evidence.root / "main" / "logs")
        copy_evidence(deployment.root / "provider-audit" / "requests.jsonl",
                      evidence.root / "main" / "provider-audit.jsonl")
        errors = deployment.cleanup(work)
        evidence.check("main_cleanup", not errors, errors)


def _chip_active(page, label: str) -> bool:
    return page.get_by_role("button", name=label, exact=True).evaluate(
        "el => el.classList.contains('active')")


def _fill_auto_draft(page, title: str, body: str,
                     media_name: str) -> None:
    page.locator(".publish-editor input.field").first.fill(title)
    page.locator(".publish-editor textarea.field").first.fill(body)
    if _chip_active(page, "小红书"):
        page.get_by_role("button", name="小红书", exact=True).click()
    if not _chip_active(page, "抖音"):
        page.get_by_role("button", name="抖音", exact=True).click()
    if page.locator(".media-grid").count() == 0:
        page.get_by_role("button", name="选择媒体").click()
    page.locator(".media-grid").wait_for(timeout=20000)
    cell = page.locator(".media-cell").filter(has_text=media_name).first
    cell.wait_for(timeout=20000)
    if "sel" not in ((cell.get_attribute("class")) or ""):
        cell.click()


_DIALOG_PAGES: set[int] = set()


def _submit_publish(page) -> None:
    if id(page) not in _DIALOG_PAGES:
        page.on("dialog", lambda dialog: dialog.accept())
        _DIALOG_PAGES.add(id(page))
    page.get_by_role("button", name="一键发布").click()


def _wait_sms(page) -> str:
    page.locator(".modal").wait_for(timeout=30000)
    text = page.locator(".modal").inner_text()
    return text


def _submit_code(page, code: str) -> None:
    page.get_by_placeholder("请输入手机收到的验证码").fill(code)
    page.get_by_role("button", name="提交验证码").click()


def _close_modal(page) -> None:
    button = page.get_by_role("button", name="关闭")
    if button.count():
        button.first.click()


def _counter_path(deployment: Deployment, workspace_id: str) -> pathlib.Path:
    return (deployment.workspaces / workspace_id / "workspace" / "outputs"
            / "_publish" / "controlled-count.txt")


def _counter(deployment: Deployment, workspace_id: str) -> int:
    path = _counter_path(deployment, workspace_id)
    if not path.is_file():
        return 0
    return sum(1 for line in path.read_text(
        encoding="utf-8").splitlines() if line.strip())


def _login_account(page, label: str, *, wrong_first: bool = False) -> None:
    card = page.locator(".account-card", has_text=label)
    card.wait_for(timeout=20000)
    card.get_by_role("button", name="登录").click()
    page.locator(".modal").wait_for(timeout=30000)
    # 受控 runner 先写 qr_ready 再进入 sms_required；轮询可能直接看到后者，
    # 因此只要求二者之一出现，再等短信输入。
    wait_until(
        lambda: page.locator(".modal .qr-img").count() > 0
        or page.locator(
            ".modal input[inputmode='numeric']").count() > 0,
        timeout=30, message=f"{label} 登录二维码或短信输入")
    page.locator(".modal input[inputmode='numeric']").wait_for(
        timeout=30000)
    if wrong_first:
        page.locator(".modal input[inputmode='numeric']").fill("000000")
        page.get_by_role("button", name="提交验证码").click()
        page.locator(".modal").get_by_text("验证码不正确").wait_for(
            timeout=20000)
    page.locator(".modal input[inputmode='numeric']").fill("123456")
    page.get_by_role("button", name="提交验证码").click()
    page.get_by_role("button", name="完成").wait_for(timeout=30000)
    page.get_by_role("button", name="完成").click()
    card.locator(".account-nick").get_by_text("受控账号").wait_for(
        timeout=30000)


def _accounts_flow(page, deployment: Deployment,
                   evidence: Evidence) -> None:
    page.locator(".nav-item", has_text="账号").click()
    page.locator(".accounts-page").wait_for(timeout=20000)
    _login_account(page, "小红书", wrong_first=True)
    _login_account(page, "抖音")
    evidence.check("account_login_sms", True, "小红书/抖音受控登录")
    evidence.screenshot(page, "20-accounts-login.png")

    open_dashboard(page)
    page.locator(".ana-plats .chip", has_text="小红书").click()
    wait_until(
        lambda: page.locator(".ana-stat-val").first.inner_text()
        not in ("—", ""), timeout=60, message="账号分析数值")
    evidence.check("account_analytics", True,
                   page.locator(".ana-stat-val").first.inner_text())
    evidence.screenshot(page, "21-accounts-analytics.png")


def _auto_publish_success(page, deployment: Deployment,
                          evidence: Evidence) -> None:
    open_publish(page)
    page.get_by_role("button", name="自动发布").click()
    page.locator(".publish-editor").wait_for(timeout=20000)
    before = _counter(deployment, deployment.user_a.workspace_id)
    _fill_auto_draft(page, "6A 自动稿件", "6A 自动发布正文", "clip.mp4")
    _submit_publish(page)
    _wait_sms(page)
    _submit_code(page, "000000")
    page.locator(".modal").get_by_text("验证码不正确").wait_for(
        timeout=20000)
    _submit_code(page, "123456")
    page.locator(".pv-pubstate.ok").wait_for(timeout=60000)
    after = _counter(deployment, deployment.user_a.workspace_id)
    evidence.check("auto_publish_sms", after == before + 1,
                   {"before": before, "after": after})
    evidence.screenshot(page, "22-auto-publish-success.png")
    _close_modal(page)


def _auto_publish_stop_unknown(page, deployment: Deployment,
                               evidence: Evidence) -> None:
    before = _counter(deployment, deployment.user_a.workspace_id)
    _fill_auto_draft(page, "TASK6A-HANG", "停止后平台结果未知", "clip.mp4")
    _submit_publish(page)
    _wait_sms(page)
    _close_modal(page)
    page.get_by_role("button", name="停止本地执行").click()
    page.locator(".pv-pubstate.unknown").wait_for(timeout=60000)
    text = page.locator(".pv-pubstate.unknown").inner_text()
    after = _counter(deployment, deployment.user_a.workspace_id)
    evidence.check(
        "auto_publish_stop_unknown",
        "平台结果未知" in text and after == before + 1,
        {"text": text, "before": before, "after": after})
    evidence.screenshot(page, "23-auto-publish-unknown.png")


def _auto_publish_once(page, deployment: Deployment,
                       evidence: Evidence) -> None:
    before = _counter(deployment, deployment.user_a.workspace_id)
    _fill_auto_draft(page, "6A 一次提交", "双击只执行一次", "clip.mp4")
    if id(page) not in _DIALOG_PAGES:
        page.on("dialog", lambda dialog: dialog.accept())
        _DIALOG_PAGES.add(id(page))
    page.get_by_role("button", name="一键发布").dblclick()
    _wait_sms(page)
    _submit_code(page, "123456")
    page.locator(".pv-pubstate.ok").wait_for(timeout=60000)
    after = _counter(deployment, deployment.user_a.workspace_id)
    evidence.check("auto_publish_once", after == before + 1,
                   {"before": before, "after": after})
    _close_modal(page)


def _publish_identity_switch(page, context, deployment: Deployment,
                             evidence: Evidence, invite_b: str,
                             invite_a_relogin: str) -> None:
    user = deployment.user_a
    before = _counter(deployment, user.workspace_id)
    open_publish(page)
    page.get_by_role("button", name="自动发布").click()
    page.locator(".publish-editor").wait_for(timeout=20000)
    _fill_auto_draft(page, "TASK6A-HANG-身份A", "未确认意图", "clip.mp4")
    _submit_publish(page)
    _wait_sms(page)
    _close_modal(page)
    page.locator(".pv-pubstate.unknown").wait_for(timeout=30000)

    ui_logout(page)
    browser_login(page, deployment, invite_b)
    browser_wait_api(page, "/api/ideas")
    open_publish(page)
    page.get_by_role("button", name="自动发布").click()
    page.locator(".publish-editor").wait_for(timeout=20000)
    title_b = page.locator(".publish-editor input.field").first.input_value()
    b_storage = page.evaluate("() => JSON.stringify(localStorage)")
    evidence.check(
        "publish_identity_b_isolated",
        title_b == "" and "TASK6A-HANG-身份A" not in b_storage,
        {"title": title_b, "storage": b_storage[:500]})
    page.locator(".nav-item", has_text="账号").click()
    page.locator(".accounts-page").wait_for(timeout=20000)
    card_b = page.locator(".account-card", has_text="小红书")
    card_b.wait_for(timeout=20000)
    evidence.check(
        "publish_identity_account_isolated",
        card_b.get_by_role("button", name="登录").count() == 1,
        "B 未继承 A 的账号登录态")

    ui_logout(page)
    browser_login(page, deployment, invite_a_relogin)
    browser_wait_api(page, "/api/ideas")
    open_publish(page)
    page.get_by_role("button", name="自动发布").click()
    page.locator(".publish-editor").wait_for(timeout=20000)
    title_a = page.locator(".publish-editor input.field").first.input_value()
    evidence.check("publish_identity_a_restored",
                   title_a == "TASK6A-HANG-身份A", title_a)
    page.get_by_role("button", name="停止本地执行").click()
    page.locator(".pv-pubstate.unknown").wait_for(timeout=60000)
    after = _counter(deployment, user.workspace_id)
    evidence.check("publish_identity_no_replay", after == before + 1,
                   {"before": before, "after": after})
    evidence.screenshot(page, "24-publish-identity-switch.png")


def phase_controlled(images, work, evidence: Evidence) -> None:
    deployment = Deployment(
        "controlled", control=images["control"],
        worker=images["controlled_worker"], gateway=images["gateway"],
        publish_mode="automated", root=work)
    deployment.prepare_browser_origin()
    try:
        deployment.initialize()
        deployment.start()
        wait_host(deployment.port)
        deployment.start_provider()
        deployment.start_terminator()
        deployment.bootstrap()
        deployment.seed_workspace(deployment.user_a.workspace_id)
        deployment.seed_workspace(deployment.user_b.workspace_id)
        evidence.record("controlled_api_warmup",
                        wait_ideas_ready(deployment.user_a))

        invite_a = browser_new_invite(deployment, deployment.member_a)
        invite_b = browser_new_invite(deployment, deployment.member_b)
        invite_a_relogin = browser_new_invite(
            deployment, deployment.member_a)

        with sync_playwright() as playwright:
            browser = ui.launch(playwright, pin=deployment.browser_pin)
            try:
                context = ui.new_context(browser, deployment.origin)
                page = context.new_page()
                page.set_default_timeout(20000)
                page.route("**/api/trends*", _empty_trends_route)
                try:
                    browser_login(page, deployment, invite_a)
                    evidence.record(
                        "controlled_browser_warmup",
                        browser_wait_api(page, "/api/ideas"))
                    _accounts_flow(page, deployment, evidence)
                    _auto_publish_success(page, deployment, evidence)
                    _auto_publish_stop_unknown(page, deployment, evidence)
                    _auto_publish_once(page, deployment, evidence)
                    _publish_identity_switch(
                        page, context, deployment, evidence,
                        invite_b, invite_a_relogin)
                except BaseException:
                    try:
                        evidence.screenshot(
                            page, "99-controlled-failure.png")
                    except Exception:
                        pass
                    raise
            finally:
                browser.close()

        evidence.record("controlled", {
            "project": deployment.project,
            "worker_image": images["controlled_worker"],
            "workspace_a": deployment.user_a.workspace_id,
            "counter": _counter(deployment, deployment.user_a.workspace_id),
        })
        copy_evidence(deployment.logs, evidence.root / "controlled" / "logs")
        copy_evidence(deployment.root / "provider-audit" / "requests.jsonl",
                      evidence.root / "controlled" / "provider-audit.jsonl")
        copy_evidence(
            deployment.workspaces / deployment.user_a.workspace_id
            / "workspace" / "outputs" / "_publish",
            evidence.root / "controlled" / "publish-outputs")
    finally:
        copy_evidence(deployment.logs, evidence.root / "controlled" / "logs")
        copy_evidence(deployment.root / "provider-audit" / "requests.jsonl",
                      evidence.root / "controlled" / "provider-audit.jsonl")
        if deployment.user_a is not None:
            copy_evidence(
                deployment.workspaces / deployment.user_a.workspace_id
                / "workspace" / "outputs" / "_publish",
                evidence.root / "controlled" / "publish-outputs")
        errors = deployment.cleanup(work)
        evidence.check("controlled_cleanup", not errors, errors)


def build_images(images: dict) -> dict:
    """按当前 worktree HEAD 构建三个最终镜像与受控 runner 派生镜像。"""
    docker("build", "-f", str(ROOT / "deploy/desktop/Dockerfile.control"),
           "-t", "easel-control:task6a-local", str(ROOT), timeout=1800)
    docker("build", "-f", str(ROOT / "deploy/desktop/Dockerfile.gateway"),
           "-t", "easel-gateway:task6a-local", str(ROOT), timeout=1800)
    docker("build", "-f", str(ROOT / "deploy/multiuser/Dockerfile.worker"),
           "--build-arg",
           "DSH_COMMIT=687f282e8625c6b8d11ff58211a90524be2e4317",
           "-t", "easel-worker:task6a-local", str(ROOT), timeout=3600)
    result = {
        "control": image_id("easel-control:task6a-local"),
        "gateway": image_id("easel-gateway:task6a-local"),
        "worker": image_id("easel-worker:task6a-local"),
    }
    docker("build", "-f",
           str(ROOT / "tests/multiuser/Dockerfile.worker-task6a"),
           "--build-arg", "BASE_IMAGE=easel-worker:task6a-local",
           "-t", "easel-worker:task6a-controlled-local",
           str(ROOT / "tests/fixtures"), timeout=900)
    result["controlled_worker"] = image_id(
        "easel-worker:task6a-controlled-local")
    return result


def resolve_images(args) -> dict:
    return {
        "control": image_id(args.control_image),
        "worker": image_id(args.worker_image),
        "gateway": image_id(args.gateway_image),
        "controlled_worker": image_id(args.controlled_worker_image),
    }


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-image",
                        default="easel-control:task6a-4382180")
    parser.add_argument("--worker-image",
                        default="easel-worker:task6a-4382180")
    parser.add_argument("--gateway-image",
                        default="easel-gateway:task6a-4382180")
    parser.add_argument("--controlled-worker-image",
                        default="easel-worker:task6a-controlled-4382180")
    parser.add_argument(
        "--phases", default="lifecycle,manual,main,controlled")
    parser.add_argument("--evidence", default=str(EVIDENCE))
    parser.add_argument("--keep", action="store_true",
                        help="失败时保留临时部署供排查")
    parser.add_argument("--build", action="store_true",
                        help="先按当前 HEAD 构建镜像")
    args = parser.parse_args()

    images = build_images({}) if args.build else resolve_images(args)
    evidence = Evidence(pathlib.Path(args.evidence))
    evidence.record("images", images)
    work = ROOT / ".test-tmp" / f"task6a-{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)
    phases = [item.strip() for item in args.phases.split(",") if item.strip()]
    failed = None
    try:
        if "lifecycle" in phases:
            phase_lifecycle(images, work, evidence)
        if "manual" in phases:
            phase_manual(images, work, evidence)
        if "main" in phases:
            phase_main(images, work, evidence)
        if "controlled" in phases:
            phase_controlled(images, work, evidence)
    except BaseException as error:  # noqa: BLE001
        failed = error
        traceback.print_exc()
        (evidence.root / "failure.log").write_text(
            traceback.format_exc(), encoding="utf-8")
    finally:
        evidence.finalize()
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        print(json.dumps({
            "images": images,
            "checks": evidence.checks,
            "failed": str(failed) if failed else "",
        }, ensure_ascii=False, indent=2), flush=True)
    return 0 if failed is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
