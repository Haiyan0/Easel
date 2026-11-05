"""Task 9 真实 Docker 集成：数据库隧道 direct 与 SSH 两种模式。

只在 Linux 且显式设置 ``EASEL_RUN_CONTAINER_TESTS=1`` 时运行；其余平台
直接 skip，绝不伪装通过。测试使用真实 MySQL、真实 sshd 与真实 sidecar
进程，验证 worker 侧经 ``db-tunnel`` 能完成握手与查询、直连数据库被
internal 网络隔离阻断，并且容器日志不出现配置错误或私钥材料。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time
import uuid

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[3]
BASE_IMAGE = "python:3.11.12-slim-bookworm"
MYSQL_IMAGE = "mysql:8.0.40"
SSHD_BASE_IMAGE = "debian:12.9-slim"
KEYS_MOUNT = "/run/easel-secrets/keys"
CONFIG_MOUNT = ("/opt/easel/skills/builtin/database-query/assets/"
                "tunnel.multiuser.yaml")
POLICY_MOUNT = "/etc/easel/egress-policy.json"
SIDECAR_UID = 10001
SIDECAR_GID = 10001
DIRECT_SOURCE = "sales_readonly"
SSH_SOURCE = "tracking_readonly"

pytestmark = pytest.mark.skipif(
    sys.platform != "linux"
    or os.environ.get("EASEL_RUN_CONTAINER_TESTS") != "1",
    reason="需要 Linux Docker 与 EASEL_RUN_CONTAINER_TESTS=1")

# 临时 sidecar 镜像：brief 依赖之外必须安装 openssh-client，否则 SSH
# 模式只会记录 state=failed reason=ssh_unavailable。
SIDECAR_IMAGE_DOCKERFILE = f"""\
FROM {BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \\
 && apt-get install -y --no-install-recommends openssh-client \\
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \\
      PyYAML==6.0.3 pydantic==2.10.4 httpx==0.28.1 \\
      PyMySQL==1.1.1 cryptography
COPY easel /opt/easel/easel
COPY skills /opt/easel/skills
COPY deploy/multiuser /opt/easel/deploy/multiuser
WORKDIR /opt/easel
"""

# 临时 sshd 镜像：构建上下文是 pytest 临时目录，host key 与
# authorized_keys 由测试在宿主生成后 COPY 进镜像，避免运行期挂载
# 带来的属主/权限不确定。
SSHD_DOCKERFILE = f"""\
FROM {SSHD_BASE_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \\
 && apt-get install -y --no-install-recommends openssh-server \\
 && rm -rf /var/lib/apt/lists/* \\
 && mkdir -p /run/sshd \\
 && rm -f /etc/ssh/ssh_host_* \\
 && useradd --create-home --shell /bin/bash tunnel \\
 && mkdir -p /home/tunnel/.ssh
COPY authorized_keys /home/tunnel/.ssh/authorized_keys
COPY host_key /etc/ssh/ssh_host_ed25519_key
COPY host_key.pub /etc/ssh/ssh_host_ed25519_key.pub
RUN chown -R tunnel:tunnel /home/tunnel/.ssh \\
 && chmod 700 /home/tunnel/.ssh \\
 && chmod 600 /home/tunnel/.ssh/authorized_keys \\
 && chown root:root /etc/ssh/ssh_host_ed25519_key \\
 && chmod 600 /etc/ssh/ssh_host_ed25519_key
CMD ["/usr/sbin/sshd","-D","-e","-p","2222","-o","UsePAM=no"]
"""


def _docker(*args: str, timeout: float = 600.0,
            check: bool = True) -> subprocess.CompletedProcess:
    """调用 docker；失败时给出截断后的诊断输出。"""
    binary = shutil.which("docker")
    if binary is None:
        pytest.fail("缺少 docker 可执行文件")
    try:
        result = subprocess.run(
            [binary, *args], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, check=False,
            timeout=timeout)
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"docker {' '.join(args[:3])} 超时（{timeout:.0f}s）")
    if check and result.returncode != 0:
        pytest.fail(
            f"docker {' '.join(args[:3])} 失败："
            f"{(result.stderr or result.stdout).strip()[-2000:]}")
    return result


def _best_effort(*args: str) -> None:
    """清理路径专用：docker 失败或超时都不应掩盖原始断言失败。"""
    try:
        _docker(*args, timeout=120.0, check=False)
    except (subprocess.SubprocessError, OSError):
        pass


def _logs(name: str) -> str:
    """合并容器 stdout/stderr；隧道日志写在 stderr。"""
    result = _docker("logs", name, check=False)
    return (result.stdout or "") + "\n" + (result.stderr or "")


def _inspect(name: str) -> dict:
    result = _docker("inspect", name)
    try:
        return json.loads(result.stdout)[0]
    except (json.JSONDecodeError, IndexError, TypeError):
        pytest.fail(
            f"docker inspect {name} 输出无法解析：{result.stdout[:500]}")


def _container_status(name: str) -> str:
    result = _docker("inspect", "-f", "{{.State.Status}}", name,
                     check=False, timeout=30.0)
    if result.returncode != 0:
        return "missing"
    return result.stdout.strip()


def _network_ip(name: str, network: str) -> str:
    networks = _inspect(name)["NetworkSettings"]["Networks"]
    entry = networks.get(network) or {}
    address = entry.get("IPAddress") or ""
    assert address, f"{name} 在 {network} 上没有 IP"
    return address


def _free_port() -> int:
    """在固定 13300-13399 段内探测一个未被宿主占用的端口。"""
    for port in range(13300, 13400):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.fail("13300-13399 内没有空闲端口")


def _wait_until(probe, *, timeout: float, interval: float = 0.5):
    """带截止时间的轮询；返回第一个真值，超时返回 None。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(interval)
    return None


def _wait_mysql_ready(name: str, timeout: float = 300.0) -> None:
    """mysqladmin ping 成功后还要确认 TCP 3306 可连。

    官方镜像初始化阶段会先起一个仅 socket 的临时实例，单看
    ``mysqladmin ping`` 可能在 TCP 就绪前返回成功。
    """
    def ready() -> bool:
        ping = _docker("exec", name, "mysqladmin", "ping", "-pprobe",
                       check=False, timeout=30.0)
        if ping.returncode != 0:
            return False
        tcp = _docker(
            "exec", name, "mysqladmin", "-h", "127.0.0.1",
            "--protocol=tcp", "-P", "3306", "ping", "-pprobe",
            check=False, timeout=30.0)
        return tcp.returncode == 0

    if _wait_until(ready, timeout=timeout, interval=1.0):
        return
    pytest.fail(
        f"MySQL 未在 {timeout:.0f}s 内就绪：\n{_logs(name)[-4000:]}")


def _wait_tunnel_ready(container: str, expected: str,
                       timeout: float = 180.0) -> str:
    """轮询 sidecar 日志直到出现固定的 ready 行；返回完整日志。"""
    deadline = time.monotonic() + timeout
    logs = ""
    while time.monotonic() < deadline:
        logs = _logs(container)
        if expected in logs:
            return logs
        if "config_invalid" in logs:
            break
        if _container_status(container) not in ("created", "running"):
            break
        time.sleep(0.5)
    logs = _logs(container)
    pytest.fail(
        f"{container} 未出现 {expected!r}"
        f"（状态 {_container_status(container)}）：\n{logs[-4000:]}")


def _write_readable(path: pathlib.Path, text: str) -> pathlib.Path:
    """写入挂载给 uid 10001 的文件；显式放宽到可读，避免宿主 umask。"""
    path.write_text(text, encoding="utf-8")
    path.chmod(0o644)
    return path


def _chown_sidecar(path: pathlib.Path) -> None:
    try:
        os.chown(path, SIDECAR_UID, SIDECAR_GID)
    except PermissionError:
        pytest.fail(
            "集成测试需要 root/CAP_CHOWN 才能把 keys 属主改为 10001:10001")


def _prepare_sidecar_keys(
        keys: pathlib.Path,
        files: tuple[tuple[pathlib.Path, int], ...] = ()) -> None:
    """keys 目录至少 0750；私钥 0400、known_hosts 0444。"""
    os.chmod(keys, 0o750)
    _chown_sidecar(keys)
    for path, mode in files:
        os.chmod(path, mode)
        _chown_sidecar(path)


def _start_mysql(*, name: str, network: str,
                 pull_timeout: float = 900.0) -> None:
    _docker(
        "run", "-d", "--name", name, "--network", network,
        "-e", "MYSQL_ROOT_PASSWORD=probe",
        "-e", "MYSQL_ROOT_HOST=%",
        "-e", "MYSQL_DATABASE=probe",
        MYSQL_IMAGE,
        "--default-authentication-plugin=mysql_native_password",
        timeout=pull_timeout)


def _start_probe(*, name: str, image: str, workspace: str) -> None:
    _docker("run", "-d", "--name", name, "--network", workspace,
            image, "sleep", "600", timeout=300.0)


def _start_sidecar(*, name: str, image: str, workspace: str,
                   control: str, keys: pathlib.Path,
                   config: pathlib.Path, policy: pathlib.Path) -> None:
    """primary 网络是 workspace internal，别名 db-tunnel；再接入受控网络。"""
    _docker(
        "run", "-d", "--name", name,
        "--network", workspace, "--network-alias", "db-tunnel",
        "--user", f"{SIDECAR_UID}:{SIDECAR_GID}",
        "-v", f"{keys}:{KEYS_MOUNT}:ro",
        "-v", f"{config}:{CONFIG_MOUNT}:ro",
        "-v", f"{policy}:{POLICY_MOUNT}:ro",
        "-w", "/opt/easel",
        image,
        "python", "deploy/multiuser/worker_sidecar.py",
        "--broker-upstream", "127.0.0.1:1",
        "--policy", POLICY_MOUNT,
        timeout=120.0)
    _docker("network", "connect", control, name, timeout=120.0)


def _probe_select_one(probe: str, port: int) -> None:
    """在 probe 容器内用 PyMySQL 经 db-tunnel 执行 SELECT 1。"""
    code = (
        "import pymysql\n"
        f"conn = pymysql.connect(host='db-tunnel', port={port},"
        " user='root', password='probe', database='probe',"
        " connect_timeout=10)\n"
        "try:\n"
        "    with conn.cursor() as cursor:\n"
        "        cursor.execute('SELECT 1')\n"
        "        print(cursor.fetchone()[0])\n"
        "finally:\n"
        "    conn.close()\n")
    result = _docker("exec", probe, "python", "-c", code,
                     check=False, timeout=60.0)
    assert result.returncode == 0, (
        "probe 经 db-tunnel 查询失败："
        + (result.stderr or result.stdout or "")[-2000:])
    assert result.stdout.strip() == "1", (
        "probe 查询结果不是 1：" + result.stdout[-500:])


def _assert_direct_blocked(probe: str, hostname: str,
                           address: str) -> None:
    """probe 直连数据库名/IP 必须抛 OSError；连上则脚本以非 0 退出。"""
    targets = [(hostname, 3306)]
    if address:
        targets.append((address, 3306))
    code = (
        "import socket\n"
        "import sys\n"
        f"targets = {targets!r}\n"
        "for host, port in targets:\n"
        "    try:\n"
        "        socket.create_connection((host, port), 2).close()\n"
        "    except OSError:\n"
        "        continue\n"
        "    print('unexpected reachable:', host, port)\n"
        "    sys.exit(1)\n"
        "sys.exit(0)\n")
    result = _docker("exec", probe, "python", "-c", code,
                     check=False, timeout=30.0)
    assert result.returncode == 0, (
        "probe 竟然能直连 MySQL：" + (result.stdout or "")[-1000:]
        + (result.stderr or "")[-1000:])


def _assert_sidecar_isolation(sidecar: str, probe: str,
                              workspace: str) -> None:
    """keys 只挂到 sidecar；db-tunnel 别名只在 sidecar 上。"""
    sidecar_data = _inspect(sidecar)
    destinations = [mount.get("Destination")
                    for mount in sidecar_data.get("Mounts") or []]
    assert KEYS_MOUNT in destinations, (
        f"sidecar 缺少 keys 挂载，实际：{destinations}")
    networks = sidecar_data["NetworkSettings"]["Networks"]
    assert workspace in networks, (
        f"sidecar 未接入 workspace 网络：{sorted(networks)}")
    aliases = networks[workspace].get("Aliases") or []
    assert "db-tunnel" in aliases, (
        f"workspace 网络别名缺少 db-tunnel：{aliases}")

    probe_data = _inspect(probe)
    probe_destinations = [mount.get("Destination")
                          for mount in probe_data.get("Mounts") or []]
    assert KEYS_MOUNT not in probe_destinations, (
        f"probe 不应挂载 keys 目录：{probe_destinations}")
    for entry in probe_data["NetworkSettings"]["Networks"].values():
        assert "db-tunnel" not in (entry.get("Aliases") or [])


def _ssh_keygen(path: pathlib.Path) -> None:
    binary = shutil.which("ssh-keygen")
    if binary is None:
        pytest.skip("缺少宿主 ssh-keygen，无法生成 SSH 密钥")
    result = subprocess.run(
        [binary, "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        check=False, timeout=60.0)
    if result.returncode != 0:
        pytest.fail(
            f"ssh-keygen 生成 {path.name} 失败："
            f"{result.stderr.strip()[-1000:]}")


class _Cleanup:
    """按名字记录资源；无论断言是否失败都按逆序尽力清理。"""

    def __init__(self) -> None:
        self._containers: list[str] = []
        self._networks: list[str] = []
        self._images: list[str] = []

    def container(self, name: str) -> str:
        self._containers.append(name)
        return name

    def network(self, name: str, *create_args: str) -> str:
        _docker("network", "create", *create_args, name)
        self._networks.append(name)
        return name

    def image(self, tag: str) -> str:
        self._images.append(tag)
        return tag

    def cleanup(self) -> None:
        for name in reversed(self._containers):
            _best_effort("rm", "-f", name)
        for tag in reversed(self._images):
            _best_effort("rmi", "-f", tag)
        for name in reversed(self._networks):
            _best_effort("network", "rm", name)


@pytest.fixture(scope="module")
def tunnel_image(tmp_path_factory) -> str:
    build_dir = tmp_path_factory.mktemp("dbq-tunnel-image")
    dockerfile = build_dir / "Dockerfile"
    dockerfile.write_text(SIDECAR_IMAGE_DOCKERFILE, encoding="utf-8")
    tag = f"dbq-tunnel-test:{uuid.uuid4().hex}"
    _docker("build", "-f", str(dockerfile), "-t", tag, str(ROOT),
            timeout=1800.0)
    yield tag
    _best_effort("rmi", "-f", tag)


def test_direct_forward_reaches_mysql_from_probe(tunnel_image, tmp_path):
    """direct 模式：probe 经 db-tunnel 查询成功，直连被隔离阻断。"""
    cleanup = _Cleanup()
    try:
        token = uuid.uuid4().hex[:10]
        workspace = cleanup.network(
            f"dbq-direct-workspace-{token}", "--internal")
        control = cleanup.network(f"dbq-direct-control-{token}")
        mysql = cleanup.container(f"dbq-direct-mysql-{token}")
        probe = cleanup.container(f"dbq-direct-probe-{token}")
        sidecar = cleanup.container(f"dbq-direct-sidecar-{token}")

        _start_mysql(name=mysql, network=control)
        _start_probe(name=probe, image=tunnel_image,
                     workspace=workspace)
        _wait_mysql_ready(mysql)
        mysql_address = _network_ip(mysql, control)

        keys = tmp_path / "keys"
        keys.mkdir()
        _prepare_sidecar_keys(keys)
        policy = _write_readable(tmp_path / "policy.json", "{}")
        port = _free_port()
        config = _write_readable(
            tmp_path / "tunnel.yaml",
            yaml.safe_dump({
                "version": 1,
                "forwards": [{
                    "source": DIRECT_SOURCE,
                    "mode": "direct",
                    "listen_port": port,
                    "database": {"host": mysql, "port": 3306},
                }],
            }))
        _start_sidecar(name=sidecar, image=tunnel_image,
                       workspace=workspace, control=control,
                       keys=keys, config=config, policy=policy)
        logs = _wait_tunnel_ready(
            sidecar,
            f"source={DIRECT_SOURCE} mode=direct state=ready")
        assert "config_invalid" not in logs

        _probe_select_one(probe, port)
        _assert_direct_blocked(probe, mysql, mysql_address)
        _assert_sidecar_isolation(sidecar, probe, workspace)
    finally:
        cleanup.cleanup()


def test_ssh_forward_reaches_mysql_from_probe(tunnel_image, tmp_path):
    """SSH 模式：sshd 转发 dbq-mysql；probe 仍不能直连数据库。"""
    if shutil.which("ssh-keygen") is None:
        pytest.skip("缺少宿主 ssh-keygen，无法生成 SSH 主机/客户端密钥")
    cleanup = _Cleanup()
    try:
        token = uuid.uuid4().hex[:10]
        workspace = cleanup.network(
            f"dbq-ssh-workspace-{token}", "--internal")
        control = cleanup.network(f"dbq-ssh-control-{token}")
        backend = cleanup.network(f"dbq-ssh-backend-{token}", "--internal")
        # permitopen 与隧道配置都写死 dbq-mysql，因此容器名不能带随机后缀。
        mysql = cleanup.container("dbq-mysql")
        probe = cleanup.container(f"dbq-ssh-probe-{token}")
        sidecar = cleanup.container(f"dbq-ssh-sidecar-{token}")
        sshd = cleanup.container(f"dbq-ssh-sshd-{token}")

        _best_effort("rm", "-f", "dbq-mysql")
        _start_mysql(name=mysql, network=backend)
        _start_probe(name=probe, image=tunnel_image,
                     workspace=workspace)

        material = tmp_path / "key-material"
        material.mkdir()
        _ssh_keygen(material / "host_key")
        _ssh_keygen(material / "client_key")
        host_pub = (material / "host_key.pub").read_text(
            encoding="utf-8").split()
        client_pub = (material / "client_key.pub").read_text(
            encoding="utf-8").strip()

        build_dir = tmp_path / "sshd-build"
        build_dir.mkdir()
        (build_dir / "authorized_keys").write_text(
            'command="",no-pty,no-agent-forwarding,no-x11-forwarding,'
            'permitopen="dbq-mysql:3306" ' + client_pub + "\n",
            encoding="utf-8")
        shutil.copyfile(material / "host_key", build_dir / "host_key")
        shutil.copyfile(material / "host_key.pub",
                        build_dir / "host_key.pub")
        (build_dir / "Dockerfile").write_text(
            SSHD_DOCKERFILE, encoding="utf-8")
        sshd_tag = cleanup.image(f"dbq-sshd-test:{uuid.uuid4().hex}")
        _docker("build", "-t", sshd_tag, str(build_dir), timeout=1800.0)
        _docker("run", "-d", "--name", sshd, "--network", backend,
                sshd_tag, timeout=120.0)
        _docker("network", "connect", control, sshd, timeout=120.0)
        _wait_mysql_ready(mysql)
        sshd_address = _network_ip(sshd, control)

        keys = tmp_path / "sidecar-keys"
        keys.mkdir()
        shutil.copyfile(material / "client_key", keys / "client_key")
        (keys / "known_hosts").write_text(
            f"[{sshd_address}]:2222 {host_pub[0]} {host_pub[1]}\n",
            encoding="utf-8")
        _prepare_sidecar_keys(keys, files=(
            (keys / "client_key", 0o400),
            (keys / "known_hosts", 0o444),
        ))
        policy = _write_readable(tmp_path / "policy.json", "{}")
        port = _free_port()
        config = _write_readable(
            tmp_path / "tunnel.yaml",
            yaml.safe_dump({
                "version": 1,
                "forwards": [{
                    "source": SSH_SOURCE,
                    "mode": "ssh",
                    "listen_port": port,
                    "database": {"host": "dbq-mysql", "port": 3306},
                    "ssh": {
                        "host": sshd_address,
                        "port": 2222,
                        "user": "tunnel",
                        "private_key": "client_key",
                        "known_hosts": "known_hosts",
                    },
                }],
            }))
        _start_sidecar(name=sidecar, image=tunnel_image,
                       workspace=workspace, control=control,
                       keys=keys, config=config, policy=policy)
        logs = _wait_tunnel_ready(
            sidecar, f"source={SSH_SOURCE} mode=ssh state=ready")
        assert "config_invalid" not in logs
        assert "Permission denied" not in logs
        assert "BEGIN OPENSSH" not in logs
        assert f"{KEYS_MOUNT}/client_key" not in logs

        _probe_select_one(probe, port)
        _assert_direct_blocked(probe, "dbq-mysql",
                               _network_ip(mysql, backend))
        _assert_sidecar_isolation(sidecar, probe, workspace)
    finally:
        cleanup.cleanup()
