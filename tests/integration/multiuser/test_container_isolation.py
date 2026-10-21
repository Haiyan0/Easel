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
def worker_image() -> str:
    """构建并固定本次测试镜像到实际 sha256 摘要。"""
    assert DOCKERFILE.is_file(), "缺少 deploy/multiuser/Dockerfile.worker"
    result = _docker(
        "build", "-f", str(DOCKERFILE), "--target", "base",
        "-t", IMAGE_TAG, str(ROOT), timeout=1800)
    image_id = _docker(
        "image", "inspect", "--format", "{{.Id}}", IMAGE_TAG).stdout.strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        pytest.fail(f"测试镜像未获得固定摘要：{image_id!r}（构建输出 {result.stdout[-500:]}）")
    return image_id


@dataclass
class Worker:
    workspace_id: str
    config_version: int
    paths: object
    env_file: pathlib.Path
    name: str
    network: str
    image: str = ""

    def token(self) -> str:
        for line in self.env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("EASEL_SERVICE_TOKEN="):
                return line.split("=", 1)[1]
        raise AssertionError("测试 env 文件缺少服务 token")


@dataclass
class Pair:
    workers: dict[str, Worker]


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
    _run_worker(worker)
    return worker


def _run_worker(worker: Worker) -> None:
    argv = provisioner.build_run_argv(
        image=worker.image, workspace_id=worker.workspace_id,
        config_version=worker.config_version, paths=worker.paths,
        env_file=worker.env_file)
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
    assert set(mount_map) == {"/workspace", "/home/easel"}
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
        "draining", "active_requests"}
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
    stopped = _docker("inspect", worker.name).stdout
    report = provisioner.sanitize_inspect(json.loads(stopped)[0])
    assert report["ready"] is False

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

sys.path.insert(0, "/opt/easel")
spec = importlib.util.spec_from_file_location("provisioner", "/check/provisioner.py")
module = importlib.util.module_from_spec(spec)
sys.modules["provisioner"] = module
spec.loader.exec_module(module)

WORKSPACE = "a" * 32
SOCKET = "/run/easel/provisioner.sock"
IMAGE = "sha256:" + "b" * 64


class FakeDocker:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, *, timeout=120.0):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")


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
        allow_unquotaed=True, allowed_uids=frozenset({0}))
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
