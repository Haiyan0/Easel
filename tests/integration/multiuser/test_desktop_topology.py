"""Task A2：Docker Desktop 拓扑契约与真实网络反例。

默认只运行静态契约；设置 ``EASEL_RUN_CONTAINER_TESTS=1`` 才启动随机
临时容器/网络。所有临时资源都带本次 run 标签，清理前复核归属，不使用
全局 prune，也不接触开发者已有容器或工作区。

本文件验证的是 A2 的**接入约束**：管理授权、宿主健康探测、control→worker
路径和 WSL 路径映射。它不把“TCP 能连上”当成 HTTP 可用，也不把测试进程
直连当成目标拓扑。
"""
from __future__ import annotations

import http.client
import json
import os
import pathlib
import re
import shutil
import subprocess
import time
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
TOPOLOGY = ROOT / "deploy" / "desktop" / "topology.json"
DOCKER = shutil.which("docker")
WSL = shutil.which("wsl")
PYTHON_IMAGE = os.environ.get("EASEL_TEST_PYTHON_IMAGE", "python:3.12-slim")
WSL_DISTRO = os.environ.get("EASEL_TEST_WSL_DISTRO", "Ubuntu-26.04")
RUN_CONTAINER = os.environ.get("EASEL_RUN_CONTAINER_TESTS") == "1"
RUN_LABEL = "easel.a2.test"


def _skip_without_docker() -> None:
    if not RUN_CONTAINER:
        pytest.skip("设置 EASEL_RUN_CONTAINER_TESTS=1 才运行真实拓扑反例")
    if DOCKER is None:
        pytest.fail("显式选择真实拓扑测试，但找不到 docker")


def _docker(*args: str, check: bool = True,
            timeout: float = 120.0) -> subprocess.CompletedProcess:
    if DOCKER is None:
        pytest.fail("找不到 docker 可执行文件")
    result = subprocess.run(
        [DOCKER, *args], capture_output=True,
        text=True, check=False, timeout=timeout)
    if check and result.returncode != 0:
        pytest.fail(
            "docker {} 失败：{}".format(
                " ".join(args[:3]), result.stderr.strip()[-1600:]))
    return result


def _wsl(*args: str, check: bool = True,
         timeout: float = 120.0) -> subprocess.CompletedProcess:
    if WSL is None:
        pytest.skip("本机没有 wsl 可执行文件")
    result = subprocess.run(
        [WSL, "-d", WSL_DISTRO, "--", *args],
        capture_output=True, text=True,
        check=False, timeout=timeout)
    if check and result.returncode != 0:
        pytest.fail(
            "wsl {} 失败：{}".format(
                " ".join(args[:3]), result.stderr.strip()[-1600:]))
    return result


def _wsl_python(code: str, *args: str,
                check: bool = True) -> subprocess.CompletedProcess:
    return _wsl("python3", "-c", code, *args, check=check)


def _to_wsl_path(path: pathlib.Path) -> str:
    text = str(path)
    match = re.match(r"^([A-Za-z]):\\(.*)$", text)
    if match is None:
        pytest.skip("测试临时目录不在 Windows 盘符，无法映射到 WSL")
    drive = match.group(1).lower()
    rest = match.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


def _run_id() -> str:
    return uuid.uuid4().hex[:16]


def _label(run_id: str) -> str:
    return f"{RUN_LABEL}={run_id}"


def _create_network(name: str, run_id: str, *, internal: bool) -> None:
    args = ["network", "create", "--label", _label(run_id)]
    if internal:
        args.append("--internal")
    args.append(name)
    _docker(*args)


def _start_http(name: str, network: str, port: int, run_id: str) -> None:
    _docker(
        "run", "-d", "--name", name, "--network", network,
        "--label", _label(run_id), PYTHON_IMAGE,
        "python", "-u", "-m", "http.server", str(port),
        "--bind", "0.0.0.0")


def _container_ip(name: str, network: str) -> str:
    payload = json.loads(
        _docker("inspect", "--format",
                "{{json .NetworkSettings.Networks}}", name).stdout)
    entry = payload.get(network) or {}
    address = entry.get("IPAddress") or ""
    assert address, f"{name} 在 {network} 没有地址"
    return address


def _container_http(name: str, host: str, port: int) -> int:
    code = (
        "import http.client,sys;"
        "c=http.client.HTTPConnection(sys.argv[1],int(sys.argv[2]),timeout=2);"
        "c.request('GET','/');"
        "print(c.getresponse().status)"
    )
    result = _docker("exec", name, "python", "-c", code, host, str(port),
                     check=False)
    if result.returncode != 0:
        raise OSError(result.stderr.strip()[-800:])
    return int(result.stdout.strip())


def _host_http(host: str, port: int) -> int:
    connection = http.client.HTTPConnection(host, port, timeout=2)
    try:
        connection.request("GET", "/")
        return connection.getresponse().status
    finally:
        connection.close()


def _wsl_http(host: str, port: int) -> subprocess.CompletedProcess:
    code = (
        "import http.client,sys\n"
        "try:\n"
        "    c=http.client.HTTPConnection(sys.argv[1],int(sys.argv[2]),"
        "timeout=2)\n"
        "    c.request('GET','/')\n"
        "    c.getresponse()\n"
        "    print('CONNECTED')\n"
        "except Exception as exc:\n"
        "    print('BLOCKED', type(exc).__name__)\n"
        "    raise SystemExit(3)\n"
    )
    return _wsl_python(code, host, str(port), check=False)


def _image_available_in_wsl(image: str) -> bool:
    return _wsl("docker", "image", "inspect", image,
                check=False).returncode == 0


def _cleanup(run_id: str, *, containers: list[str],
             networks: list[str]) -> None:
    """只删除带本次 run 标签的对象；标签不符则保留。"""
    for name in containers:
        result = _docker("inspect", name, check=False)
        if result.returncode != 0:
            continue
        try:
            labels = (json.loads(result.stdout)[0]
                      .get("Config", {}).get("Labels") or {})
        except (json.JSONDecodeError, KeyError, IndexError):
            continue
        if labels.get(RUN_LABEL) == run_id:
            _docker("rm", "-f", name, check=False)
    for name in networks:
        result = _docker("network", "inspect", name, check=False)
        if result.returncode != 0:
            continue
        try:
            labels = json.loads(result.stdout)[0].get("Labels") or {}
        except (json.JSONDecodeError, KeyError, IndexError):
            continue
        if labels.get(RUN_LABEL) == run_id:
            _docker("network", "rm", name, check=False)


def _assert_http_blocked(callable_probe, message: str) -> None:
    try:
        status = callable_probe()
    except (OSError, TimeoutError):
        return
    pytest.fail(f"{message}，实际 HTTP {status}")


def test_desktop_topology_contract_is_explicit_and_secret_free() -> None:
    assert TOPOLOGY.is_file(), "A2 必须先提交 deploy/desktop/topology.json"
    data = json.loads(TOPOLOGY.read_text(encoding="utf-8"))

    assert data["schema"] == "easel.desktop.topology.v1"
    assert data["status"] in {"proposed", "measured_unapproved"}
    assert data["approval_required"] is True
    assert data["implementation"]["enabled"] is False

    forbidden = {
        "secret", "password", "authtoken", "private_key", "master_key",
        "service_token", "access_token",
    }

    def walk_keys(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                assert key.lower() not in forbidden, key
                walk_keys(child)
        elif isinstance(value, list):
            for child in value:
                walk_keys(child)

    walk_keys(data)

    management = data["management"]
    assert management["provisioner"]["location"] == "wsl2"
    assert management["provisioner"]["distro"] == "Ubuntu-26.04"
    assert management["provisioner"]["uid"] == 1000
    assert management["provisioner"]["gid"] == 1000
    assert management["control"]["uid"] == 10001
    assert management["control"]["supplemental_gid"] == 1000
    assert management["socket"]["peer_cred"]["required"] is True
    assert management["socket"]["peer_cred"]["allowed_uids"] == [10001]
    assert management["socket"]["mode"] == "0660"
    assert management["socket"]["windows_native_peer_cred"] is False
    assert management["health_probe"]["mode"] == "docker_exec_required"
    assert management["health_probe"]["implemented"] is False

    network = data["network"]
    assert network["worker_ports"] == []
    assert network["control"]["docker_socket"] is False
    assert network["worker"]["docker_socket"] is False
    assert network["worker"]["publish_all_ports"] is False
    assert network["control_to_worker"]["mode"] == "gateway_required"
    assert network["wsl_to_worker"]["http"] == "blocked"
    assert network["windows_to_worker"]["http"] == "blocked"
    assert network["gateway"]["enabled"] is False
    assert network["gateway"]["docker_socket"] is False
    assert network["gateway"]["worker_ports"] == []
    assert network["gateway"]["management_bind"] == "control_network_ip_only"
    assert network["gateway"]["management_port"] == 8897
    assert network["gateway"]["routing"]["workspace_fixed"] is True
    assert network["gateway"]["routing"]["client_target_allowed"] is False
    assert network["gateway"]["worker_to_management"] == (
        "blocked_by_bind_and_network")
    assert "/internal/lifecycle/health" in (
        network["gateway"]["path_allowlist"])
    assert network["gateway"]["sse"] == "streaming_passthrough"

    assert data["paths"]["workspace_root_windows"].startswith("C:")
    assert data["paths"]["workspace_root_wsl"].startswith("/mnt/c/")
    assert data["paths"]["container_workspace"] == "/workspace"
    assert data["broker"]["worker_url_template"] == (
        "http://easel-sidecar-{workspace_id}:8899")
    assert data["approval"]["items"]


@pytest.mark.skipif(not RUN_CONTAINER, reason="真实拓扑测试需要显式开关")
def test_worker_http_is_blocked_from_windows_and_wsl() -> None:
    _skip_without_docker()
    assert _docker("image", "inspect", PYTHON_IMAGE,
                   check=False).returncode == 0, (
        f"缺少测试镜像 {PYTHON_IMAGE}")
    run_id = _run_id()
    ws = f"easel-a2-{run_id}-ws"
    worker = f"easel-a2-{run_id}-worker"
    try:
        _create_network(ws, run_id, internal=True)
        _start_http(worker, ws, 7860, run_id)
        time.sleep(1.0)
        address = _container_ip(worker, ws)
        assert _container_http(worker, "127.0.0.1", 7860) == 200
        _assert_http_blocked(
            lambda: _host_http(address, 7860),
            "Windows 宿主不应能用 HTTP 探测 worker")
        if WSL is not None:
            result = _wsl_http(address, 7860)
            assert result.returncode != 0, result.stdout
            assert "CONNECTED" not in result.stdout
    finally:
        _cleanup(run_id, containers=[worker], networks=[ws])


@pytest.mark.skipif(not RUN_CONTAINER, reason="真实拓扑测试需要显式开关")
def test_bridge_control_and_worker_networks_are_isolated() -> None:
    _skip_without_docker()
    assert _docker("image", "inspect", PYTHON_IMAGE,
                   check=False).returncode == 0, (
        f"缺少测试镜像 {PYTHON_IMAGE}")
    run_id = _run_id()
    ws = f"easel-a2-{run_id}-ws"
    ctl = f"easel-a2-{run_id}-ctl"
    worker = f"easel-a2-{run_id}-worker"
    control = f"easel-a2-{run_id}-control"
    try:
        _create_network(ws, run_id, internal=True)
        _create_network(ctl, run_id, internal=False)
        _start_http(worker, ws, 7860, run_id)
        _start_http(control, ctl, 7861, run_id)
        time.sleep(1.0)
        worker_ip = _container_ip(worker, ws)
        control_ip = _container_ip(control, ctl)
        _assert_http_blocked(
            lambda: _container_http(control, worker_ip, 7860),
            "独立 control 容器不应到达 worker")
        _assert_http_blocked(
            lambda: _container_http(worker, control_ip, 7861),
            "worker 不应到达独立 control 容器")
    finally:
        _cleanup(run_id, containers=[worker, control], networks=[ws, ctl])


@pytest.mark.skipif(not RUN_CONTAINER, reason="真实拓扑测试需要显式开关")
def test_joining_control_to_worker_network_is_bidirectional() -> None:
    """记录已拒绝的直接接线：control 加入 worker 网络后双向可达。"""
    _skip_without_docker()
    assert _docker("image", "inspect", PYTHON_IMAGE,
                   check=False).returncode == 0, (
        f"缺少测试镜像 {PYTHON_IMAGE}")
    run_id = _run_id()
    ws = f"easel-a2-{run_id}-ws"
    ctl = f"easel-a2-{run_id}-ctl"
    worker = f"easel-a2-{run_id}-worker"
    control = f"easel-a2-{run_id}-control"
    try:
        _create_network(ws, run_id, internal=True)
        _create_network(ctl, run_id, internal=False)
        _start_http(worker, ws, 7860, run_id)
        _start_http(control, ctl, 7861, run_id)
        _docker("network", "connect", ws, control)
        time.sleep(1.0)
        worker_ip = _container_ip(worker, ws)
        control_ws_ip = _container_ip(control, ws)
        assert _container_http(control, worker_ip, 7860) == 200
        assert _container_http(worker, control_ws_ip, 7861) == 200
    finally:
        _cleanup(run_id, containers=[worker, control], networks=[ws, ctl])


@pytest.mark.skipif(not RUN_CONTAINER, reason="真实拓扑测试需要显式开关")
def test_windows_and_wsl_bind_sources_are_visible(
        tmp_path: pathlib.Path) -> None:
    _skip_without_docker()
    if WSL is None:
        pytest.skip("本机没有 WSL")
    assert _image_available_in_wsl(PYTHON_IMAGE), (
        f"WSL 内缺少测试镜像 {PYTHON_IMAGE}")
    run_id = _run_id()
    marker = tmp_path / "marker.txt"
    marker.write_text("windows-mounted", encoding="utf-8")
    wsl_dir = f"/tmp/easel-a2-{run_id}-bind"
    wsl_file = f"{wsl_dir}/marker.txt"
    try:
        _wsl_python(
            "import os,sys;os.makedirs(sys.argv[1],exist_ok=True);"
            "open(sys.argv[2],'w',encoding='ascii').write('wsl-ext4')",
            wsl_dir, wsl_file)
        windows_mount = _wsl(
            "docker", "run", "--rm", "--label", _label(run_id),
            "-v", f"{_to_wsl_path(tmp_path)}:/check:ro", PYTHON_IMAGE,
            "sh", "-c", "cat /check/marker.txt")
        assert windows_mount.stdout.strip() == "windows-mounted"
        wsl_mount = _wsl(
            "docker", "run", "--rm", "--label", _label(run_id),
            "-v", f"{wsl_dir}:/check:ro", PYTHON_IMAGE,
            "sh", "-c", "cat /check/marker.txt")
        assert wsl_mount.stdout.strip() == "wsl-ext4"
    finally:
        _wsl_python(
            "import shutil,sys;shutil.rmtree(sys.argv[1],ignore_errors=True)",
            wsl_dir, check=False)


@pytest.mark.skipif(not RUN_CONTAINER, reason="真实拓扑测试需要显式开关")
def test_wsl_unix_socket_mount_preserves_peer_uid(
        tmp_path: pathlib.Path) -> None:
    _skip_without_docker()
    if WSL is None:
        pytest.skip("本机没有 WSL")
    assert _image_available_in_wsl(PYTHON_IMAGE), (
        f"WSL 内缺少测试镜像 {PYTHON_IMAGE}")
    run_id = _run_id()
    sock = f"/tmp/easel-a2-{run_id}.sock"
    ready = f"{sock}.ready"
    server = tmp_path / "socket_server.py"
    client = tmp_path / "socket_client.py"
    server.write_text(
        "import os,socket,sys\n"
        "p,r=sys.argv[1],sys.argv[2]\n"
        "try: os.unlink(p)\n"
        "except FileNotFoundError: pass\n"
        "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
        "s.bind(p); os.chmod(p,0o660); s.listen(1); s.settimeout(20)\n"
        "open(r,'w',encoding='ascii').write('ready')\n"
        "c,_=s.accept()\n"
        "try:\n"
        " import struct\n"
        " raw=c.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,"
        "struct.calcsize('3i'))\n"
        " print('PEER',struct.unpack('3i',raw),flush=True)\n"
        " c.sendall(b'hello\\n')\n"
        "finally: c.close(); s.close()\n",
        encoding="ascii")
    client.write_text(
        "import socket\n"
        "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
        "s.settimeout(5); s.connect('/sock')\n"
        "print(s.recv(32).decode().strip())\n",
        encoding="ascii")
    process = None
    try:
        process = subprocess.Popen(
            [WSL, "-d", WSL_DISTRO, "--", "python3",
             _to_wsl_path(server), sock, ready],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if _wsl("test", "-f", ready, check=False).returncode == 0:
                break
            time.sleep(0.05)
        else:
            pytest.fail("WSL socket server 未在期限内就绪")
        result = _wsl(
            "docker", "run", "--rm", "--label", _label(run_id),
            "--user", "10001:10001", "--group-add", "1000",
            "-v", f"{sock}:/sock:ro",
            "-v", f"{_to_wsl_path(client)}:/client.py:ro",
            PYTHON_IMAGE, "python", "/client.py")
        assert result.stdout.strip() == "hello"
        out, err = process.communicate(timeout=10)
        assert "PEER (0, 10001, 10001)" in out, (out, err)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
        _wsl_python(
            "import os,sys\n"
            "for p in sys.argv[1:]:\n"
            " try: os.unlink(p)\n"
            " except FileNotFoundError: pass\n",
            sock, ready, check=False)
