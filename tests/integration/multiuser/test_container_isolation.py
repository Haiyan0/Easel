"""Task 8 真实 Linux 容器隔离集成测试。

显式选择后才运行：

    EASEL_RUN_CONTAINER_TESTS=1 \
    python -m pytest tests/integration/multiuser/test_container_isolation.py -q

本文件直接使用 ``deploy/multiuser/provisioner.py`` 的批准 argv 与脱敏
inspect 逻辑启动自己创建的临时 A/B 容器，不调用开发者已有容器或数据。
全部断言基于实际 Docker/容器内结果；环境缺失时测试失败而不是跳过，避免把
“未执行”伪装成“通过”。清理只删除本次随机 UUID 命名的容器与网络，且删除
前复核受管标签。
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = pathlib.Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "deploy" / "multiuser" / "provisioner.py"
DOCKERFILE = ROOT / "deploy" / "multiuser" / "Dockerfile.worker"
IMAGE_TAG = "easel-worker-test:task8"
READY_DEADLINE = 60.0

pytestmark = pytest.mark.skipif(
    os.environ.get("EASEL_RUN_CONTAINER_TESTS") != "1",
    reason="设置 EASEL_RUN_CONTAINER_TESTS=1 才运行真实容器隔离测试",
)


def _load_provisioner():
    spec = importlib.util.spec_from_file_location(
        "easel_provisioner_integration", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provisioner = _load_provisioner()


def _docker(*args: str, timeout: float = 120.0,
            check: bool = True) -> subprocess.CompletedProcess:
    binary = shutil.which("docker")
    if binary is None:
        pytest.fail("显式选择真实容器测试，但找不到 docker 可执行文件")
    result = subprocess.run([binary, *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, check=False,
                            timeout=timeout)
    if check and result.returncode != 0:
        pytest.fail(
            f"docker {' '.join(args[:3])} 失败：{result.stderr.strip()[-2000:]}")
    return result


def _exec(name: str, *args: str, timeout: float = 15.0,
          check: bool = True) -> subprocess.CompletedProcess:
    return _docker("exec", name, *args, timeout=timeout, check=check)


def _exec_python(name: str, code: str, *, check: bool = True,
                 timeout: float = 15.0) -> subprocess.CompletedProcess:
    return _exec(name, "python", "-c", code, check=check, timeout=timeout)


def _exec_python_value(name: str, code: str) -> str:
    return _exec_python(name, code).stdout.strip()


def _inspect(name: str) -> dict:
    result = _docker("inspect", name)
    payload = json.loads(result.stdout)
    assert isinstance(payload, list) and payload, "inspect 必须返回容器"
    return payload[0]


def _wait_ready(name: str, workspace_id: str, config_version: int,
                deadline_seconds: float = READY_DEADLINE) -> dict:
    """等待 lifecycle 身份就绪；只读健康端点，不执行 prompt。"""
    code = (
        "import os,urllib.request\n"
        "r=urllib.request.Request('http://127.0.0.1:7860"
        "/internal/lifecycle/health',headers={"
        "'X-Easel-Workspace-Id':os.environ['EASEL_WORKSPACE_ID'],"
        "'X-Easel-Service-Token':os.environ['EASEL_SERVICE_TOKEN']})\n"
        "print(urllib.request.urlopen(r,timeout=3).read().decode())\n")
    deadline = time.monotonic() + deadline_seconds
    last = ""
    while time.monotonic() < deadline:
        result = _exec_python(name, code, check=False, timeout=10)
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            assert payload["workspace_id"] == workspace_id
            assert payload["config_version"] == config_version
            assert payload["ready"] is True
            assert payload["draining"] is False
            return payload
        last = result.stderr[-800:]
        time.sleep(0.5)
    details = _docker("logs", "--tail", "50", name, check=False).stdout
    pytest.fail(f"容器在期限内部未就绪：{last}\n--- logs ---\n{details[-3000:]}")


def _network_exists(name: str) -> bool:
    return _docker("network", "inspect", name, check=False).returncode == 0


def _remove_owned_container(workspace_id: str) -> None:
    name = provisioner.container_name(workspace_id)
    result = _docker("inspect", name, check=False)
    if result.returncode != 0:
        return
    try:
        payload = json.loads(result.stdout)
        labels = payload[0]["Config"]["Labels"] or {}
    except (json.JSONDecodeError, KeyError, IndexError):
        return
    if (labels.get("easel.managed") == "true"
            and labels.get("easel.workspace-id") == workspace_id):
        _docker("rm", "-f", name, check=False)


def _remove_owned_network(workspace_id: str) -> None:
    name = provisioner.network_name(workspace_id)
    if not _network_exists(name):
        return
    result = _docker("network", "inspect", name, check=False)
    try:
        payload = json.loads(result.stdout)
        labels = payload[0].get("Labels") or {}
    except (json.JSONDecodeError, IndexError):
        return
    if (labels.get("easel.managed") == "true"
            and labels.get("easel.workspace-id") == workspace_id):
        _docker("network", "rm", name, check=False)


@pytest.fixture(scope="session")
def worker_image(execution_image) -> str:
    """真实执行入口使用 execution 镜像，而不是 lifecycle-only base。"""
    return execution_image


EXECUTION_TAG = "easel-worker-execution-test:task8-r2"


@pytest.fixture(scope="session")
def execution_image() -> str:
    """构建不含真实 Harness 的执行阶段；冒烟在构建期与运行期各执行一次。"""
    _docker(
        "build", "-f", str(DOCKERFILE), "--target", "execution",
        "-t", EXECUTION_TAG, str(ROOT), timeout=1800)
    image_id = _docker(
        "image", "inspect", "--format", "{{.Id}}", EXECUTION_TAG).stdout.strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        pytest.fail(f"执行测试镜像未获得固定摘要：{image_id!r}")
    return image_id


def test_execution_image_contains_web_skills_and_fake_harness(execution_image):
    result = _docker(
        "run", "--rm", execution_image,
        "python", "deploy/multiuser/smoke_worker.py", "--check-files",
        timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "WORKER_SMOKE_OK" in result.stdout


_CHROMIUM_SANDBOX_CODE = r'''
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        headless=True, chromium_sandbox=True,
        args=["--disable-dev-shm-usage"])
    page = browser.new_page()
    page.set_content("<h1>sandbox-ok</h1>")
    print("CHROMIUM_SANDBOX_OK", page.text_content("h1"))
    browser.close()
'''

_FINAL_DRIVER_ARGS_CODE = r'''
import os
import pathlib
import stat
from playwright.sync_api import sync_playwright

fake = pathlib.Path("/tmp/fake-browser")
fake.write_text("""#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_BROWSER_OUT"
exit 1
""", encoding="utf-8")
fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

def capture(name, enabled):
    out = pathlib.Path(f"/tmp/{name}.args")
    os.environ["FAKE_BROWSER_OUT"] = str(out)
    try:
        with sync_playwright() as playwright:
            playwright.chromium.launch(
                executable_path=str(fake), headless=True,
                chromium_sandbox=enabled,
                args=["--disable-dev-shm-usage"])
    except Exception:
        pass
    return out.read_text(encoding="utf-8").splitlines()

enabled_args = capture("sandbox-enabled", True)
disabled_args = capture("sandbox-disabled", False)
assert not any(arg.split("=", 1)[0] in (
    "--no-sandbox", "--disable-setuid-sandbox") for arg in enabled_args), (
        enabled_args)
assert any(arg.split("=", 1)[0] in (
    "--no-sandbox", "--disable-setuid-sandbox") for arg in disabled_args), (
        disabled_args)
print("FINAL_DRIVER_ARGS_OK")
'''


def test_final_playwright_driver_args_do_not_disable_sandbox(
        execution_image):
    """检查驱动最终命令行：chromium_sandbox=True 时不得出现禁用项。"""
    result = _docker(
        "run", "--rm",
        "--user", "10001:10001",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=256m",
        "-e", "HOME=/tmp",
        "-e", "MPLCONFIGDIR=/tmp/.mpl",
        "-e", "PLAYWRIGHT_BROWSERS_PATH=/opt/playwright",
        execution_image, "python", "-c", _FINAL_DRIVER_ARGS_CODE,
        timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "FINAL_DRIVER_ARGS_OK" in result.stdout


def test_chromium_keeps_builtin_sandbox_in_hardened_container(execution_image):
    """与 worker 相同的权限边界内启动 Chromium；失败即门槛未通过。"""
    result = _docker(
        "run", "--rm",
        "--user", "10001:10001",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=256",
        "--memory=4g",
        "--memory-swap=4g",
        "--cpus=2",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=1g",
        "-e", "HOME=/tmp",
        "-e", "MPLCONFIGDIR=/tmp/.mpl",
        "-e", "PLAYWRIGHT_BROWSERS_PATH=/opt/playwright",
        execution_image, "python", "-c", _CHROMIUM_SANDBOX_CODE,
        timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "CHROMIUM_SANDBOX_OK sandbox-ok" in result.stdout


@dataclass
class Worker:
    workspace_id: str
    config_version: int
    paths: object
    env_file: pathlib.Path
    name: str
    network: str
    image: str = ""
    public_key: pathlib.Path | None = None

    def token(self) -> str:
        for line in self.env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("EASEL_SERVICE_TOKEN="):
                return line.split("=", 1)[1]
        raise AssertionError("测试 env 文件缺少服务 token")


@dataclass
class Pair:
    workers: dict[str, Worker]


def _public_key(root: pathlib.Path) -> pathlib.Path:
    path = root.parent / "worker.pub"
    path.write_bytes(Ed25519PrivateKey.generate().public_key()
                     .public_bytes_raw())
    return path


def _prepare_worker(root: pathlib.Path, config_version: int,
                    image: str) -> Worker:
    workspace_id = uuid.uuid4().hex
    paths = provisioner.prepare_workspace(root, workspace_id)
    # 测试宿主上显式放开临时目录，等价于部署时 provisioner 的 chown 10001；
    # 不放宽容器本身的安全配置。
    for directory in (paths.workspace, paths.home, paths.state):
        os.chmod(directory, 0o777)
    token = provisioner._load_or_create_token(root, workspace_id)
    env_file = provisioner.write_env_file(
        root, workspace_id, config_version, token, paths)
    network = provisioner.network_name(workspace_id)
    _docker("network", "create", "--internal",
            "--label", "easel.managed=true",
            "--label", f"easel.workspace-id={workspace_id}", network)
    worker = Worker(workspace_id, config_version, paths, env_file,
                    provisioner.container_name(workspace_id), network, image)
    worker.public_key = _public_key(root)
    _run_worker(worker)
    return worker


def _run_worker(worker: Worker) -> None:
    argv = provisioner.build_run_argv(
        image=worker.image, workspace_id=worker.workspace_id,
        config_version=worker.config_version, paths=worker.paths,
        env_file=worker.env_file, public_key_file=worker.public_key)
    _docker(*argv, timeout=120)
    _wait_ready(worker.name, worker.workspace_id, worker.config_version)


@pytest.fixture(scope="module")
def workers(worker_image, tmp_path_factory):
    """创建 A/B 两个本测试独占 workspace；退出时只清理自建资源。"""
    root = tmp_path_factory.mktemp("easel-worker-isolation")
    made: list[Worker] = []
    try:
        a = _prepare_worker(root, 1, worker_image)
        made.append(a)
        b = _prepare_worker(root, 1, worker_image)
        made.append(b)
        yield Pair({a.workspace_id: a, b.workspace_id: b})
    finally:
        for worker in made:
            _remove_owned_container(worker.workspace_id)
        for worker in made:
            _remove_owned_network(worker.workspace_id)


def _first(pair: Pair) -> Worker:
    return next(iter(pair.workers.values()))


def test_container_security_and_resource_boundaries(workers, worker_image):
    worker = _first(workers)
    raw = _inspect(worker.name)
    config = raw["Config"]
    host = raw["HostConfig"]

    assert config["User"] == "10001:10001"
    assert host["ReadonlyRootfs"] is True
    assert host["Privileged"] is False
    assert "ALL" in host["CapDrop"]
    assert any("no-new-privileges" in str(option)
               for option in host.get("SecurityOpt") or [])
    assert host["PidsLimit"] == 256
    assert host["Memory"] == 4 * 1024 ** 3
    assert host["NanoCpus"] == 2_000_000_000
    bindings = host.get("PortBindings") or {}
    assert not any(bool(values) for values in bindings.values())
    networks = raw["NetworkSettings"]["Networks"]
    assert list(networks) == [worker.network]
    ports = raw["NetworkSettings"]["Ports"] or {}
    assert not any(bool(bindings) for bindings in ports.values()), "不得发布宿主端口"

    mount_map = {item["Destination"]: item["Source"]
                 for item in raw["Mounts"]}
    assert set(mount_map) == {
        "/workspace", "/home/easel", "/keys/worker-public.key"}
    assert not any("docker.sock" in source for source in mount_map.values())

    assert _exec(worker.name, "id", "-u").stdout.strip() == "10001"
    assert _exec(worker.name, "sh", "-c",
                 "test -d /workspace/state").returncode == 0
    assert _exec_python_value(
        worker.name, "import os;print(os.environ['EASEL_STATE_ROOT'])"
        ) == "/workspace/state"
    assert _exec(worker.name, "cat", "/sys/fs/cgroup/memory.max"
                 ).stdout.strip() == str(4 * 1024 ** 3)
    assert _exec(worker.name, "cat", "/sys/fs/cgroup/pids.max"
                 ).stdout.strip() == "256"
    assert _exec(worker.name, "cat", "/sys/fs/cgroup/cpu.max"
                 ).stdout.split() == ["200000", "100000"]
    tmp_size = int(_exec_python_value(
        worker.name,
        "import os;s=os.statvfs('/tmp');print(s.f_blocks*s.f_frsize)"))
    assert tmp_size == 1024 ** 3

    read_only = _exec(worker.name, "sh", "-c", "echo x > /opt/easel/pwn",
                      check=False)
    assert read_only.returncode != 0, "只读代码目录必须拒绝写入"
    writable = _exec_python(
        worker.name,
        "open('/workspace/probe.txt','w').write('ok');print('ok')")
    assert writable.stdout.strip() == "ok"
    assert _exec_python_value(
        worker.name, "print(open('/workspace/probe.txt').read())") == "ok"

    report = provisioner.sanitize_inspect(raw)
    assert report["managed"] is True
    assert report["workspace_id"] == worker.workspace_id
    assert report["config_version"] == worker.config_version
    assert report["ready"] is True
    assert report["upstream"].endswith(":7860")
    assert report["security"]["read_only"] is True
    assert report["security"]["no_new_privileges"] is True
    assert worker.token() not in json.dumps(report)

    # 严格验收必须与真实 Docker inspect 一致；不能只测手工 fixture。
    assert provisioner.verify_worker_inspect(
        raw, workspace_id=worker.workspace_id,
        config_version=worker.config_version, image=worker.image,
        paths=worker.paths) is True
    network_raw = json.loads(_docker("network", "inspect",
                                     worker.network).stdout)
    assert provisioner.verify_network_inspect(
        network_raw, worker.workspace_id) is True


def test_lifecycle_requires_workspace_credentials(workers):
    worker = _first(workers)
    unauthenticated = _exec_python(
        worker.name,
        "import urllib.request;urllib.request.urlopen("
        "'http://127.0.0.1:7860/internal/lifecycle/health',timeout=3)",
        check=False)
    assert unauthenticated.returncode != 0
    assert "401" in unauthenticated.stderr

    payload = _wait_ready(worker.name, worker.workspace_id,
                          worker.config_version)
    assert set(payload) == {
        "workspace_id", "config_version", "container_id", "ready",
        "draining", "active_requests", "active_runs",
        "execution_capable"}
    assert payload["execution_capable"] is True
    assert payload["container_id"] == worker.name
    assert payload["active_requests"] == 0


def test_cross_workspace_and_management_network_are_denied(workers):
    a, b = list(workers.workers.values())
    b_ip = _docker(
        "inspect", "--format",
        '{{(index .NetworkSettings.Networks "' + b.network + '").IPAddress}}',
        b.name).stdout.strip()
    assert b_ip, "B 必须获得内部网络地址"

    probe = (
        "import socket,sys;s=socket.socket();s.settimeout(3);"
        "s.connect((sys.argv[1],7860));print('CONNECTED')"
    )
    cross = _docker("exec", a.name, "python", "-c", probe, b_ip, check=False)
    assert cross.returncode != 0, f"A 不应访问 B：{cross.stdout}"
    assert "CONNECTED" not in cross.stdout

    # 无默认路由（IPv4），且没有 IPv6 地址/默认路由。
    route = _exec(a.name, "cat", "/proc/net/route").stdout
    defaults = [line for line in route.splitlines()[1:]
                if line.split()[1:2] == ["00000000"]]
    assert not defaults, f"{a.name} 不应有 IPv4 默认路由"
    ipv6_addresses = _exec(a.name, "cat", "/proc/net/if_inet6",
                           check=False)
    if ipv6_addresses.returncode == 0:
        # 只允许 ::1 回环，不得有 eth0 或其他外部 IPv6 地址。
        external_v6 = [
            line for line in ipv6_addresses.stdout.splitlines()
            if line.split() and line.split()[-1] != "lo"]
        assert not external_v6, f"不应有非回环 IPv6 地址：{external_v6}"
    ipv6_route = _exec(a.name, "cat", "/proc/net/ipv6_route", check=False)
    # /proc 里可能存在经 lo 的不可用默认项；以实际连接为准确认拒绝。
    assert ipv6_route.returncode == 0
    ipv6_probe = (
        "import socket;s=socket.socket(socket.AF_INET6,socket.SOCK_STREAM);"
        "s.settimeout(3);s.connect(('2606:4700:4700::1111',53));"
        "print('CONNECTED')")
    public_v6 = _docker("exec", a.name, "python", "-c", ipv6_probe,
                        check=False)
    assert public_v6.returncode != 0, (
        f"worker 不应有可用 IPv6 公网出口：{public_v6.stdout}")
    assert "CONNECTED" not in public_v6.stdout

    ipv4_probe = (
        "import socket;s=socket.socket();s.settimeout(3);"
        "s.connect(('1.1.1.1',53));print('CONNECTED')")
    public_v4 = _docker("exec", a.name, "python", "-c", ipv4_probe,
                        check=False)
    assert public_v4.returncode != 0, (
        f"worker 不应有可用 IPv4 公网出口：{public_v4.stdout}")
    assert "CONNECTED" not in public_v4.stdout

    gateway = _docker(
        "inspect", "--format",
        '{{(index .NetworkSettings.Networks "' + a.network + '").Gateway}}',
        a.name).stdout.strip()
    connect_code = (
        "import socket,sys;s=socket.socket();s.settimeout(3);"
        "s.connect((sys.argv[1],int(sys.argv[2])));print('CONNECTED')")
    for target, port in ((gateway, "7861"), (gateway, "2375"),
                         ("169.254.169.254", "80")):
        if not target:
            continue
        denied = _docker("exec", a.name, "python", "-c", connect_code,
                         target, port, check=False)
        assert denied.returncode != 0, (
            f"worker 不应访问 {target}:{port}：{denied.stdout}")
        assert "CONNECTED" not in denied.stdout


def test_abnormal_exit_and_rebuild_preserve_persistent_data(workers):
    worker = _first(workers)
    _exec_python(worker.name,
                 "open('/workspace/keep.txt','w').write('persisted')")
    _docker("kill", worker.name, check=False)
    raw_exited = json.loads(_docker("inspect", worker.name).stdout)[0]
    report = provisioner.sanitize_inspect(raw_exited)
    assert report["ready"] is False
    # 已退出实例没有运行时健康，但静态安全配置仍应通过验收。
    assert provisioner.verify_worker_inspect(
        raw_exited, workspace_id=worker.workspace_id,
        config_version=worker.config_version, image=worker.image,
        paths=worker.paths) is True

    _docker("rm", "-f", worker.name, check=False)
    _run_worker(worker)
    value = _exec_python_value(
        worker.name, "print(open('/workspace/keep.txt').read())")
    assert value == "persisted"
    assert _exec(worker.name, "id", "-u").stdout.strip() == "10001"


_SOCKET_CHECK = r'''
import asyncio
import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, "/opt/easel")
spec = importlib.util.spec_from_file_location("provisioner", "/check/provisioner.py")
module = importlib.util.module_from_spec(spec)
sys.modules["provisioner"] = module
spec.loader.exec_module(module)

WORKSPACE = "a" * 32
SOCKET = "/run/easel/provisioner.sock"
IMAGE = "sha256:" + "b" * 64
PUBLIC_KEY = pathlib.Path("/check/worker.pub")
PUBLIC_KEY.write_bytes(b"\x00" * 32)
CONTEXT_NOT_FOUND = (
    "Failed to initialize: unable to resolve docker endpoint: "
    'context "codex-review-absent-context-20261021": context not found:\n'
    "open /root/.docker/contexts/meta/meta.json: "
    "The system cannot find the path specified.")


class FakeDocker:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, *, timeout=120.0):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv, 1, stdout="",
            stderr=f"Error: No such object: {argv[-1]}\n")


async def call(payload):
    reader, writer = await asyncio.open_unix_connection(SOCKET)
    writer.write(json.dumps(payload).encode() + b"\n")
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), 5)
    writer.close()
    await writer.wait_closed()
    return json.loads(line)


async def main():
    fake = FakeDocker()
    settings = module.ProvisionerSettings(
        image=IMAGE, workspace_root=pathlib.Path("/work"),
        socket_path=pathlib.Path(SOCKET), quota_mode="none",
        allow_unquotaed=True, allowed_uids=frozenset({0}),
        worker_public_key=PUBLIC_KEY)
    service = module.Provisioner(settings, run=fake)
    task = asyncio.create_task(service.serve())
    for _ in range(100):
        if pathlib.Path(SOCKET).exists():
            break
        await asyncio.sleep(0.05)
    assert pathlib.Path(SOCKET).exists(), "socket 未创建"
    mode = stat.S_IMODE(os.stat(SOCKET).st_mode)
    assert mode == 0o660, f"socket 权限错误: {oct(mode)}"

    injected = await call({
        "version": 1, "action": "ensure", "workspace_id": WORKSPACE,
        "config_version": 1, "image": "evil", "command": "sh"})
    assert injected == {"ok": False, "error": "unknown_field"}, injected
    assert fake.calls == [], "注入请求不得触达 Docker"

    bad_id = await call({
        "version": 1, "action": "inspect", "workspace_id": "../../etc"})
    assert bad_id == {"ok": False, "error": "invalid_workspace"}, bad_id
    assert fake.calls == []

    report = await call({
        "version": 1, "action": "inspect", "workspace_id": WORKSPACE})
    assert report["ok"] is True, report
    assert report["report"]["managed"] is False
    assert fake.calls == [[
        "docker", "container", "inspect",
        "easel-worker-" + WORKSPACE]], fake.calls

    # 走真实 control 客户端，验证响应白名单解码而非只测裸 socket。
    from easel.control.runtime import UnixProvisionerClient
    decoded = await UnixProvisionerClient(SOCKET).inspect(WORKSPACE)
    assert decoded["managed"] is False
    assert len(fake.calls) == 2, fake.calls

    # 放行文件权限以单独验证 SO_PEERCRED 授权，而不是只靠目录 0750 拦截。
    os.chmod("/run/easel", 0o777)
    os.chmod(SOCKET, 0o666)
    child = await asyncio.create_subprocess_exec(
        sys.executable, "/check/unpriv.py", SOCKET, WORKSPACE,
        user=10001, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(child.communicate(), 15)
    assert child.returncode == 0, err.decode("utf-8", "replace")
    assert "unauthorized" in out.decode("utf-8", "replace")
    assert len(fake.calls) == 2, "未授权请求不得触达 Docker"

    # 实际受限服务入口的新建调用：ensure 必须经 _docker 携带完整
    # docker run argv，健康核验后返回受管句柄。
    class EnsureDocker:
        def __init__(self):
            self.calls = []
            self.container = None
            self.network = None
            self.workspace_id = WORKSPACE
            self.fail_stop = False
            self.fail_rm = False
            self.inspect_error = ""
            self.name_inspect_stdout = None
            self.id_inspect_stdout = None
            self.network_inspect_stdout = None

        def completed(self, argv, code, stdout="", stderr=""):
            return subprocess.CompletedProcess(
                argv, code, stdout, stderr)

        def not_found(self, argv, target):
            return self.completed(
                argv, 1, stderr=f"Error: No such object: {target}\n")

        def make_container(self, env=None):
            workspace_id = self.workspace_id
            paths = module.prepare_workspace(
                pathlib.Path("/work"), workspace_id)
            return {
                "Id": "f" * 64,
                "Name": "/" + module.container_name(workspace_id),
                "Config": {
                    "User": "10001:10001",
                    "Image": IMAGE,
                    "Entrypoint": None,
                    "Cmd": list(module.WORKER_COMMAND),
                    "Labels": module.workload_labels(workspace_id, 1),
                    "Env": [*(env or ["EASEL_SERVICE_TOKEN=old-token"]),
                            "EASEL_WORKER_PUBLIC_KEY_FILE=/keys/worker-public.key"],
                },
                "State": {"Running": True, "Status": "running",
                          "Health": {"Status": "healthy"}},
                "HostConfig": {
                    "ReadonlyRootfs": True,
                    "Privileged": False,
                    "CapDrop": ["ALL"],
                    "CapAdd": [],
                    "SecurityOpt": ["no-new-privileges:true"],
                    "PidsLimit": 256,
                    "Memory": 4 * 1024 ** 3,
                    "MemorySwap": 4 * 1024 ** 3,
                    "NanoCpus": 2000000000,
                    "NetworkMode": module.network_name(workspace_id),
                    "PortBindings": {},
                    "PublishAllPorts": False,
                    "Binds": [],
                    "Devices": [],
                    "PidMode": "",
                    "IpcMode": "private",
                    "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=1g"},
                    "RestartPolicy": {"Name": "no"},
                },
                "Mounts": [
                    {"Type": "bind", "Source": str(paths.workspace),
                     "Destination": "/workspace", "RW": True},
                    {"Type": "bind", "Source": str(paths.home),
                     "Destination": "/home/easel", "RW": True},
                    {"Type": "bind", "Source": str(PUBLIC_KEY),
                     "Destination": "/keys/worker-public.key", "RW": False},
                ],
                "NetworkSettings": {
                    "Ports": {},
                    "Networks": {
                        module.network_name(workspace_id): {
                            "IPAddress": "172.30.0.2"},
                    },
                },
            }

        def __call__(self, argv, *, timeout=120.0):
            self.calls.append(list(argv))
            assert argv[0] == "docker", argv
            args = argv[1:]
            if self.inspect_error and args[:2] in (
                    ["network", "inspect"], ["container", "inspect"]):
                return self.completed(
                    argv, 1, stderr=self.inspect_error)
            if args[:2] == ["network", "inspect"]:
                if self.network_inspect_stdout is not None:
                    return self.completed(
                        argv, 0, self.network_inspect_stdout)
                if self.network is None:
                    return self.completed(
                        argv, 1,
                        stderr=("Error response from daemon: network "
                                f"{args[2]} not found\n"))
                return self.completed(argv, 0, json.dumps([self.network]))
            if args[:2] == ["network", "create"]:
                self.network = {
                    "Name": module.network_name(self.workspace_id),
                    "Driver": "bridge",
                    "Internal": True,
                    "EnableIPv6": False,
                    "Labels": {
                        "easel.managed": "true",
                        "easel.workspace-id": self.workspace_id,
                    },
                }
                return self.completed(argv, 0, "created\n")
            if args[:2] == ["container", "inspect"]:
                target = args[2]
                is_instance = (len(target) == 64
                               and all(character in "0123456789abcdef"
                                       for character in target))
                if is_instance and self.id_inspect_stdout is not None:
                    return self.completed(
                        argv, 0, self.id_inspect_stdout)
                if (not is_instance
                        and target == module.container_name(
                            self.workspace_id)
                        and self.name_inspect_stdout is not None):
                    return self.completed(
                        argv, 0, self.name_inspect_stdout)
                if (self.container is None
                        or target not in (
                            self.container["Id"],
                            module.container_name(self.workspace_id))):
                    return self.not_found(argv, target)
                return self.completed(argv, 0, json.dumps([self.container]))
            if args[:1] == ["run"]:
                env_path = pathlib.Path(
                    args[args.index("--env-file") + 1])
                env = env_path.read_text(encoding="utf-8").splitlines()
                self.container = self.make_container(env)
                if "--cidfile" in args:
                    pathlib.Path(
                        args[args.index("--cidfile") + 1]).write_text(
                            self.container["Id"] + "\n", encoding="ascii")
                return self.completed(argv, 0, "fake-container\n")
            if args[:1] == ["stop"]:
                target = args[-1]
                if self.fail_stop:
                    return self.completed(
                        argv, 1,
                        stderr="Error response from daemon: stop failed\n")
                if (self.container is None
                        or self.container["Id"] != target):
                    return self.not_found(argv, target)
                self.container["State"] = {
                    "Running": False, "Status": "exited"}
                return self.completed(argv, 0)
            if args[:1] == ["rm"]:
                target = args[-1]
                if self.fail_rm:
                    return self.completed(
                        argv, 1,
                        stderr="Error response from daemon: rm failed\n")
                if (self.container is None
                        or target not in (
                            self.container["Id"],
                            module.container_name(self.workspace_id))):
                    return self.not_found(argv, target)
                self.container = None
                return self.completed(argv, 0)
            raise AssertionError(f"unexpected docker 调用: {argv}")

    ensure_docker = EnsureDocker()
    service._runner = ensure_docker

    async def always_probe(*args, **kwargs):
        return True

    service._probe = always_probe
    ensured = await call({
        "version": 1, "action": "ensure", "workspace_id": WORKSPACE,
        "config_version": 1})
    assert ensured.get("ok") is True, ensured
    assert ensured["handle"]["container_id"] == "f" * 64
    run_calls = [c for c in ensure_docker.calls if c[1:2] == ["run"]]
    assert run_calls and run_calls[0][:3] == ["docker", "run", "-d"], (
        run_calls[:1])

    # 停止必须携带实例身份；同名但身份不符时不得 stop/rm。
    wrong_stop = await call({
        "version": 1, "action": "stop", "workspace_id": WORKSPACE,
        "instance_id": "e" * 64})
    assert wrong_stop == {"ok": False, "error": "container_conflict"}, (
        wrong_stop)
    assert ensure_docker.container is not None
    right_stop = await call({
        "version": 1, "action": "stop", "workspace_id": WORKSPACE,
        "instance_id": "f" * 64})
    assert right_stop == {"ok": True}, right_stop
    assert ensure_docker.container is None

    # 重新创建一个实例，逐项验证 Docker 非零退出不会被报告成功。
    recreated = await call({
        "version": 1, "action": "ensure", "workspace_id": WORKSPACE,
        "config_version": 1})
    assert recreated.get("ok") is True, recreated
    instance_id = recreated["handle"]["container_id"]
    assert ensure_docker.container is not None

    ensure_docker.fail_stop = True
    failed_stop = await call({
        "version": 1, "action": "stop", "workspace_id": WORKSPACE,
        "instance_id": instance_id})
    assert failed_stop == {"ok": False, "error": "container_stop_failed"}, (
        failed_stop)
    assert ensure_docker.container is not None
    assert ensure_docker.container["State"]["Running"] is True

    ensure_docker.fail_stop = False
    ensure_docker.fail_rm = True
    failed_rm = await call({
        "version": 1, "action": "stop", "workspace_id": WORKSPACE,
        "instance_id": instance_id})
    assert failed_rm == {"ok": False, "error": "container_remove_failed"}, (
        failed_rm)
    assert ensure_docker.container is not None

    ensure_docker.fail_rm = False
    cleaned = await call({
        "version": 1, "action": "stop", "workspace_id": WORKSPACE,
        "instance_id": instance_id})
    assert cleaned == {"ok": True}, cleaned
    assert ensure_docker.container is None

    # 已确认不存在：幂等成功，不触发 stop/rm。
    idempotent = await call({
        "version": 1, "action": "stop", "workspace_id": WORKSPACE,
        "instance_id": instance_id})
    assert idempotent == {"ok": True}, idempotent

    # Docker inspect 失败不能当作“不存在”。
    ensure_docker.inspect_error = (
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock")
    inspect_failed = await call({
        "version": 1, "action": "stop", "workspace_id": WORKSPACE,
        "instance_id": instance_id})
    assert inspect_failed == {
        "ok": False, "error": "docker_inspect_failed"}, inspect_failed

    # Docker context 不存在不是“目标容器不存在”；不得执行 stop/rm。
    before_context = len(ensure_docker.calls)
    ensure_docker.inspect_error = CONTEXT_NOT_FOUND
    context_failed = await call({
        "version": 1, "action": "stop", "workspace_id": WORKSPACE,
        "instance_id": instance_id})
    assert context_failed == {
        "ok": False, "error": "docker_inspect_failed"}, context_failed
    context_calls = ensure_docker.calls[before_context:]
    assert not [c for c in context_calls if c[1:2] in (["stop"], ["rm"])], (
        context_calls)
    ensure_docker.inspect_error = ""

    # 退出 0 但结构非法（[] / {} / [null]）不是对象不存在。
    for payload in ("[]", "{}", "[null]"):
        recreated = await call({
            "version": 1, "action": "ensure", "workspace_id": WORKSPACE,
            "config_version": 1})
        assert recreated.get("ok") is True, recreated
        invalid_id = recreated["handle"]["container_id"]
        ensure_docker.name_inspect_stdout = payload
        before_invalid = len(ensure_docker.calls)
        invalid_reply = await call({
            "version": 1, "action": "stop", "workspace_id": WORKSPACE,
            "instance_id": invalid_id})
        assert invalid_reply == {
            "ok": False, "error": "inspect_invalid"}, invalid_reply
        assert ensure_docker.container is not None
        invalid_calls = ensure_docker.calls[before_invalid:]
        assert not [c for c in invalid_calls
                    if c[1:2] in (["stop"], ["rm"])], invalid_calls
        ensure_docker.name_inspect_stdout = None
        cleaned = await call({
            "version": 1, "action": "stop", "workspace_id": WORKSPACE,
            "instance_id": invalid_id})
        assert cleaned == {"ok": True}, cleaned

    # 最终按实例 ID 确认收到非法成功响应时，即使 stop/rm 已执行也不得
    # 报告终态已确认。
    recreated = await call({
        "version": 1, "action": "ensure", "workspace_id": WORKSPACE,
        "config_version": 1})
    assert recreated.get("ok") is True, recreated
    invalid_id = recreated["handle"]["container_id"]
    ensure_docker.id_inspect_stdout = "[]"
    final_invalid = await call({
        "version": 1, "action": "stop", "workspace_id": WORKSPACE,
        "instance_id": invalid_id})
    assert final_invalid == {
        "ok": False, "error": "inspect_invalid"}, final_invalid
    assert ensure_docker.container is None
    ensure_docker.id_inspect_stdout = None

    # 网络 inspect 成功但结构非法时，不得继续 network create/run。
    ensure_docker.workspace_id = WORKSPACE
    ensure_docker.container = None
    ensure_docker.network = None
    ensure_docker.network_inspect_stdout = "{}"
    before_network = len(ensure_docker.calls)
    network_invalid = await call({
        "version": 1, "action": "ensure", "workspace_id": WORKSPACE,
        "config_version": 1})
    assert network_invalid == {
        "ok": False, "error": "network_invalid"}, network_invalid
    network_calls = ensure_docker.calls[before_network:]
    assert not [c for c in network_calls
                if c[1:3] in (["network", "create"], ["run"])], (
        network_calls)
    ensure_docker.network_inspect_stdout = None

    # 真实 Unix 服务 + UnixProvisionerClient + RuntimeManager + Store：
    # stop 失败必须让 drain 报错，数据库不得写 stopped，容器仍在运行。
    from easel.control.runtime import (
        RuntimeManager, RuntimeUnavailable, UnixProvisionerClient)
    from easel.control.store import Store

    database = pathlib.Path(tempfile.mkdtemp()) / "control.sqlite3"
    store = Store.connect(database)
    try:
        user = store.create_user("Runtime")
        ensure_docker.workspace_id = user.workspace_id
        ensure_docker.network = None
        ensure_docker.container = None
        ensure_docker.fail_stop = False
        ensure_docker.fail_rm = False
        client = UnixProvisionerClient(SOCKET)
        handle = await client.ensure(user.workspace_id, 1)
        now = 1000
        outcome, lease = store.claim_worker_start(
            user.workspace_id, 1, "runtime-owner", now, 60)
        assert outcome == "started", outcome
        assert store.complete_worker_start(
            user.workspace_id, "runtime-owner", lease.generation, handle,
            now, 60)
        manager = RuntimeManager(
            store, client, clock=lambda: 1000, poll_interval=0.01,
            drain_timeout=1)
        ensure_docker.fail_stop = True
        try:
            await manager.drain(user.workspace_id)
        except RuntimeUnavailable as error:
            assert error.code == "container_stop_failed", error.code
        else:
            raise AssertionError("stop 失败时 drain 不得成功")
        failed_lease = store.get_worker_lease(user.workspace_id)
        assert failed_lease.state == "stopping", failed_lease.state
        assert ensure_docker.container is not None
        assert ensure_docker.container["State"]["Running"] is True

        # context not found 同样不能消解停止承诺或写 stopped。
        ensure_docker.inspect_error = CONTEXT_NOT_FOUND
        recovering = RuntimeManager(
            store, client, clock=lambda: 2000, poll_interval=0.01,
            drain_timeout=1)
        try:
            await recovering.ensure_ready(user.workspace_id, 1)
        except RuntimeUnavailable as error:
            assert error.code == "worker_stop_failed", error.code
        else:
            raise AssertionError("context not found 不得消解停止承诺")
        assert store.get_worker_lease(
            user.workspace_id).state == "stopping"
        assert ensure_docker.container is not None
        assert ensure_docker.container["State"]["Running"] is True
        ensure_docker.inspect_error = ""

        # 恢复后按同一实例 ID 幂等清理。
        ensure_docker.fail_stop = False
        await client.stop(user.workspace_id, handle.container_id)
        assert store.stop_worker(
            user.workspace_id, 1001, generation=lease.generation,
            expected_state="stopping")
        assert store.get_worker_lease(user.workspace_id).state == "stopped"

        # 初始名称查询成功但结构非法：不写 stopped，容器仍在。
        name_user = store.create_user("InvalidName")
        ensure_docker.workspace_id = name_user.workspace_id
        ensure_docker.network = None
        ensure_docker.container = None
        ensure_docker.name_inspect_stdout = None
        name_handle = await client.ensure(name_user.workspace_id, 1)
        outcome, name_lease = store.claim_worker_start(
            name_user.workspace_id, 1, "name-owner", 1000, 60)
        assert outcome == "started", outcome
        assert store.complete_worker_start(
            name_user.workspace_id, "name-owner", name_lease.generation,
            name_handle, 1000, 60)
        ensure_docker.name_inspect_stdout = "[]"
        try:
            await manager.drain(name_user.workspace_id)
        except RuntimeUnavailable as error:
            assert error.code == "inspect_invalid", error.code
        else:
            raise AssertionError("非法成功 inspect 不得完成 drain")
        name_state = store.get_worker_lease(name_user.workspace_id)
        assert name_state.state == "stopping"
        assert ensure_docker.container is not None
        assert ensure_docker.container["State"]["Running"] is True
        ensure_docker.name_inspect_stdout = None
        await client.stop(name_user.workspace_id, name_handle.container_id)
        assert store.stop_worker(
            name_user.workspace_id, 1001,
            generation=name_lease.generation, expected_state="stopping")

        # 最终实例 ID 确认结构非法：stop/rm 已执行，但不得写 stopped。
        final_user = store.create_user("InvalidFinal")
        ensure_docker.workspace_id = final_user.workspace_id
        ensure_docker.network = None
        ensure_docker.container = None
        final_handle = await client.ensure(final_user.workspace_id, 1)
        outcome, final_lease = store.claim_worker_start(
            final_user.workspace_id, 1, "final-owner", 1000, 60)
        assert outcome == "started", outcome
        assert store.complete_worker_start(
            final_user.workspace_id, "final-owner", final_lease.generation,
            final_handle, 1000, 60)
        ensure_docker.id_inspect_stdout = "[]"
        try:
            await manager.drain(final_user.workspace_id)
        except RuntimeUnavailable as error:
            assert error.code == "inspect_invalid", error.code
        else:
            raise AssertionError("非法终态确认不得完成 drain")
        final_state = store.get_worker_lease(final_user.workspace_id)
        assert final_state.state == "stopping"
        assert ensure_docker.container is None
        ensure_docker.id_inspect_stdout = None
    finally:
        store.close()

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    print("SOCKET_CHECK_OK")


asyncio.run(main())
'''

_UNPRIV_CLIENT = r'''
import json
import socket
import sys

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(5)
sock.connect(sys.argv[1])
sock.sendall(json.dumps({
    "version": 1, "action": "inspect", "workspace_id": sys.argv[2],
}).encode() + b"\n")
data = b""
while not data.endswith(b"\n"):
    chunk = sock.recv(4096)
    if not chunk:
        break
    data += chunk
print(data.decode("utf-8"))
'''


_OWNERSHIP_CHECK = r'''
import importlib.util
import os
import pathlib
import stat
import sys
import tempfile

sys.path.insert(0, "/opt/easel")
spec = importlib.util.spec_from_file_location("provisioner", "/check/provisioner.py")
module = importlib.util.module_from_spec(spec)
sys.modules["provisioner"] = module
spec.loader.exec_module(module)

root = pathlib.Path(tempfile.mkdtemp(prefix="easel-ownership-"))

# 合法目录：fd 绑定 fchown 真正生效。
valid = module.prepare_workspace(root, "b" * 32)
module.ensure_worker_ownership(valid)
for directory in (valid.workspace, valid.home, valid.state):
    info = os.stat(directory)
    assert info.st_uid == 10001, (directory, info.st_uid)
    assert info.st_gid == 10001, (directory, info.st_gid)

# 受控替换：worker 在 prepare 后把 state 换成指向外部的 symlink。
outside = pathlib.Path(tempfile.mkdtemp(prefix="easel-outside-"))
sentinel = outside / "sentinel.txt"
sentinel.write_text("unchanged", encoding="utf-8")
os.chmod(sentinel, 0o640)
before_dir = os.stat(outside)
before_file = os.stat(sentinel)
before_content = sentinel.read_text(encoding="utf-8")

paths = module.prepare_workspace(root, "a" * 32)
saved = paths.state.with_name("saved-state")
paths.state.rename(saved)
paths.state.symlink_to(outside, target_is_directory=True)
try:
    module._capture_directory_ids(paths.root)
except module.ProvisionerError as error:
    print("CAPTURE_REJECTED", error.code)
else:
    raise AssertionError("身份捕获不得跟随被替换的 state")
try:
    module.ensure_worker_ownership(paths)
except module.ProvisionerError as error:
    print("REJECTED", error.code)
else:
    raise AssertionError("被替换的 state 未被拒绝")

after_dir = os.stat(outside)
after_file = os.stat(sentinel)
assert stat.S_IMODE(after_dir.st_mode) == stat.S_IMODE(before_dir.st_mode)
assert stat.S_IMODE(after_file.st_mode) == stat.S_IMODE(before_file.st_mode)
assert (after_dir.st_uid, after_dir.st_gid) == (before_dir.st_uid, before_dir.st_gid)
assert (after_file.st_uid, after_file.st_gid) == (before_file.st_uid, before_file.st_gid)
assert sentinel.read_text(encoding="utf-8") == before_content
assert not (outside / "state").exists()
print("OWNERSHIP_CHECK_OK")
'''


def test_privileged_ownership_binds_to_verified_directory(worker_image,
                                                          tmp_path):
    """真实 Linux symlink：拒绝替换后的 state，外部 sentinel 不变。"""
    script = tmp_path / "ownership_check.py"
    script.write_text(_OWNERSHIP_CHECK, encoding="utf-8")
    result = _docker(
        "run", "--rm", "--user", "0",
        "-v", f"{MODULE_PATH}:/check/provisioner.py:ro",
        "-v", f"{script}:/check/ownership.py:ro",
        worker_image, "python", "/check/ownership.py",
        timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "CAPTURE_REJECTED workspace_owner_failed" in result.stdout
    assert "REJECTED workspace_owner_failed" in result.stdout
    assert "OWNERSHIP_CHECK_OK" in result.stdout


def test_unix_socket_protocol_and_peer_authorization(worker_image, tmp_path):
    """真实 Unix socket：注入与未授权调用在 Docker 之前拒绝。"""
    script = tmp_path / "socket_check.py"
    client = tmp_path / "unpriv.py"
    script.write_text(_SOCKET_CHECK, encoding="utf-8")
    client.write_text(_UNPRIV_CLIENT, encoding="utf-8")
    result = _docker(
        "run", "--rm", "--user", "0",
        "-v", f"{MODULE_PATH}:/check/provisioner.py:ro",
        "-v", f"{script}:/check/run.py:ro",
        "-v", f"{client}:/check/unpriv.py:ro",
        worker_image, "python", "/check/run.py",
        timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "SOCKET_CHECK_OK" in result.stdout
