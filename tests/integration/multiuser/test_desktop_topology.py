"""Task A2：Docker Desktop 拓扑契约与真实网络反例。

默认只运行静态契约与纯逻辑清理测试；设置
``EASEL_RUN_CONTAINER_TESTS=1`` 才启动随机临时容器/网络。

所有 Docker 临时资源、包括 ``docker run`` 客户端，都有明确名称和
``easel.test.run-id`` 标签；复用 ``tests/multiuser/topology_support.py``
逐项 inspect/删除并汇总错误。单项超时或删除失败不会阻断后续清理，也不会
被测试假绿吞掉。

本文件验证的是 A2 的接入约束，不是 gateway 实现：控制/网关/worker 的真实
运行时代码仍等待设计批准。
"""
from __future__ import annotations

import contextlib
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

from tests.multiuser import topology_support as support

ROOT = pathlib.Path(__file__).resolve().parents[3]
TOPOLOGY = ROOT / "deploy" / "desktop" / "topology.json"
DOCKER = shutil.which("docker")
WSL = shutil.which("wsl")
PYTHON_IMAGE = os.environ.get("EASEL_TEST_PYTHON_IMAGE", "python:3.12-slim")
WSL_DISTRO = os.environ.get("EASEL_TEST_WSL_DISTRO", "Ubuntu-26.04")
RUN_CONTAINER = os.environ.get("EASEL_RUN_CONTAINER_TESTS") == "1"
RUN_LABEL = support.RUN_LABEL

EXPECTED_ROUTES = [
    {
        "method": "GET",
        "path": "/internal/lifecycle/health",
        "query": [],
    },
    {
        "method": "POST",
        "path": "/internal/runs/{run_id}",
        "query": [],
    },
    {
        "method": "GET",
        "path": "/internal/runs/{run_id}/events",
        "query": [
            {
                "name": "after",
                "type": "integer",
                "required": False,
                "default": 0,
                "min": 0,
                "max": "worker_max_event_id",
                "repeat": "reject",
            },
            {
                "name": "nonce",
                "type": "string",
                "required": False,
                "min_length": 1,
                "max_length": 128,
                "encoding": "rfc3986_percent_decode_once",
                "repeat": "reject",
            },
        ],
    },
    {
        "method": "POST",
        "path": "/internal/runs/{run_id}/stop",
        "query": [],
    },
    {
        "method": "POST",
        "path": "/internal/runs/{run_id}/questions/{question_id}/answer",
        "query": [],
    },
    {
        "method": "POST",
        "path": "/internal/runs/{run_id}/questions/status",
        "query": [],
    },
    {
        "method": "GET",
        "path": "/internal/sessions/{session_id}/messages",
        "query": [
            {
                "name": "limit",
                "type": "integer",
                "required": False,
                "default": 100,
                "min": 1,
                "max": 500,
                "repeat": "reject",
            },
            {
                "name": "offset",
                "type": "integer",
                "required": False,
                "default": 0,
                "min": 0,
                "max": None,
                "repeat": "reject",
            },
        ],
    },
]


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


def _cleanup_runner(argv, *, timeout: float = 20.0
                    ) -> subprocess.CompletedProcess:
    """供 topology_support 调用；支持被注入 CLI 超时后的按名清理。"""
    if DOCKER is None:
        raise RuntimeError("docker unavailable")
    if not argv or argv[0] != "docker":
        raise ValueError("cleanup command must start with docker")
    return subprocess.run(
        [DOCKER, *argv[1:]], capture_output=True, text=True,
        check=False, timeout=timeout)


def _attach_cleanup_errors(error: BaseException, errors: list[str]) -> None:
    if not errors:
        return
    detail = "cleanup: " + " | ".join(errors)
    add_note = getattr(error, "add_note", None)
    if callable(add_note):
        add_note(detail)
    else:
        # Python < 3.11 没有异常 note；保留可诊断属性而不覆盖原异常。
        error.cleanup_errors = tuple(errors)


def _cleanup_ordered(run_id: str,
                     resources: list[tuple[str, str]]) -> list[str]:
    # 先删容器，再删网络/卷；否则 active endpoint 会让网络删除失败。
    rank = {"container": 0, "network": 1, "volume": 2}
    ordered = sorted(
        resources, key=lambda item: rank.get(item[0], 3))
    return support.cleanup_resources(_cleanup_runner, ordered, run_id)


@contextlib.contextmanager
def _tracked(run_id: str):
    """资源登记和逐项清理；成功测试的清理失败必须变红。"""
    resources: list[tuple[str, str]] = []
    try:
        yield resources
    except BaseException as error:
        try:
            errors = _cleanup_ordered(run_id, resources)
        except Exception as cleanup_error:  # noqa: BLE001 - 保留原异常
            errors = [(
                f"cleanup helper {type(cleanup_error).__name__}: "
                f"{cleanup_error}")]
        _attach_cleanup_errors(error, errors)
        raise
    else:
        errors = _cleanup_ordered(run_id, resources)
        if errors:
            pytest.fail("A2 cleanup failed: " + " | ".join(errors))


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


def _label_args(run_id: str) -> list[str]:
    return support.label_args(run_id)


def _create_network(name: str, run_id: str, *, internal: bool) -> None:
    args = ["network", "create", *_label_args(run_id)]
    if internal:
        args.append("--internal")
    args.append(name)
    _docker(*args)


def _start_http(name: str, network: str, port: int, run_id: str) -> None:
    _docker(
        "run", "-d", "--name", name, "--network", network,
        *_label_args(run_id), PYTHON_IMAGE,
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


def _assert_wsl_blocked(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 3, (
        "WSL HTTP 探针基础设施失败，不能当成隔离通过："
        f"rc={result.returncode} stdout={result.stdout[-400:]!r} "
        f"stderr={result.stderr[-800:]!r}")
    assert "BLOCKED" in result.stdout, result.stdout[-400:]
    assert "CONNECTED" not in result.stdout


def _image_available_in_wsl(image: str) -> bool:
    return _wsl("docker", "image", "inspect", image,
                check=False).returncode == 0


def _assert_http_blocked(callable_probe, message: str) -> None:
    try:
        status = callable_probe()
    except (OSError, TimeoutError):
        return
    pytest.fail(f"{message}，实际 HTTP {status}")


def _wsl_cleanup_error(paths: list[str]) -> str | None:
    result = _wsl_python(
        "import os,shutil,sys\n"
        "for path in sys.argv[1:]:\n"
        "    try:\n"
        "        if os.path.isdir(path) and not os.path.islink(path):\n"
        "            shutil.rmtree(path)\n"
        "        else:\n"
        "            os.unlink(path)\n"
        "    except FileNotFoundError:\n"
        "        pass\n",
        *paths, check=False)
    if result.returncode == 0:
        return None
    return (f"WSL cleanup {paths}: rc={result.returncode} "
            f"stderr={result.stderr[-800:]}")


@contextlib.contextmanager
def _wsl_temp(paths: list[str]):
    try:
        yield
    except BaseException as error:
        detail = _wsl_cleanup_error(paths)
        if detail:
            _attach_cleanup_errors(error, [detail])
        raise
    else:
        detail = _wsl_cleanup_error(paths)
        if detail:
            pytest.fail(detail)


class _ScriptedDocker:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout=20.0):
        self.calls.append(list(argv))
        return self.handler(argv)


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


def _fail(stderr: str, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 1, stdout, stderr)


def _inspect(kind: str, name: str, run_id: str,
             labels: dict[str, str] | None = None
             ) -> subprocess.CompletedProcess:
    values = labels or {support.RUN_LABEL: run_id}
    if kind == "container":
        payload = [{"Name": name, "Config": {"Labels": values}}]
    else:
        payload = [{"Name": name, "Labels": values}]
    return _ok(json.dumps(payload))


def _absent(kind: str, name: str) -> subprocess.CompletedProcess:
    if kind == "container":
        return _fail(f"Error: No such object: {name}\n")
    if kind == "network":
        return _fail(f"Error response from daemon: network {name} not found\n")
    return _fail(f"Error response from daemon: get {name}: no such volume\n")


def _is_delete(argv: list[str]) -> bool:
    return argv[1:2] in (["rm"],) or argv[1:3] in (
        ["network", "rm"], ["volume", "rm"])


def test_cleanup_aggregates_failures_and_continues_to_later_items() -> None:
    resources = [
        ("container", "worker-a"),
        ("container", "worker-b"),
        ("network", "net-c"),
        ("container", "foreign-d"),
        ("container", "gone-e"),
        ("container", "client-f"),
    ]
    inspections: dict[str, int] = {}

    def handler(argv):
        kind = ("container" if argv[1:2] == ["container"]
                else "network" if argv[1:2] == ["network"]
                else "volume")
        name = argv[-1]
        if _is_delete(argv):
            if name == "worker-a":
                return _fail("Error response from daemon: rm failed\n")
            if name == "worker-b":
                raise subprocess.TimeoutExpired(argv, 1)
            if name == "gone-e":
                return _fail("Error response from daemon: rm failed\n")
            return _ok()
        inspections[name] = inspections.get(name, 0) + 1
        if name == "net-c":
            raise subprocess.TimeoutExpired(argv, 1)
        if name == "foreign-d":
            return _inspect(kind, name, "run-a",
                            {support.RUN_LABEL: "other-run"})
        if name == "gone-e" and inspections[name] > 1:
            return _absent(kind, name)
        return _inspect(kind, name, "run-a")

    run = _ScriptedDocker(handler)
    errors = support.cleanup_resources(run, resources, "run-a")
    deletes = [call[-1] for call in run.calls if _is_delete(call)]
    assert deletes == ["worker-a", "worker-b", "gone-e", "client-f"]
    assert any("worker-a" in error for error in errors)
    assert any("worker-b" in error for error in errors)
    assert any("net-c" in error for error in errors)
    assert any("foreign-d" in error and "ownership" in error
               for error in errors)
    assert not any("gone-e" in error for error in errors)
    assert not any("client-f" in error for error in errors)
    assert inspections["net-c"] == 1
    assert inspections["foreign-d"] == 1
    assert inspections["client-f"] == 1
    assert not any(call[-1] == "net-c" for call in run.calls
                   if _is_delete(call))


def test_named_client_after_cli_timeout_is_cleaned() -> None:
    resources = [("container", "easel-a2-run-client")]

    def handler(argv):
        if _is_delete(argv):
            return _ok()
        return _inspect("container", "easel-a2-run-client", "run-a")

    run = _ScriptedDocker(handler)
    try:
        raise subprocess.TimeoutExpired(["docker", "run"], 1)
    except subprocess.TimeoutExpired:
        errors = support.cleanup_resources(run, resources, "run-a")
    assert errors == []
    assert [call[-1] for call in run.calls if _is_delete(call)] == [
        "easel-a2-run-client"]


def test_tracked_cleanup_failure_makes_successful_test_red(monkeypatch):
    monkeypatch.setattr(
        support, "cleanup_resources",
        lambda *_args, **_kwargs: ["named client delete failed"])
    with pytest.raises(pytest.fail.Exception), _tracked("run-a"):
        pass


def test_tracked_cleanup_error_preserves_original_exception(monkeypatch):
    monkeypatch.setattr(
        support, "cleanup_resources",
        lambda *_args, **_kwargs: ["named client delete failed"])
    with pytest.raises(RuntimeError) as captured, _tracked("run-a"):
        raise RuntimeError("original failure")
    assert "original failure" in str(captured.value)
    notes = list(getattr(captured.value, "__notes__", []))
    cleanup_errors = list(
        getattr(captured.value, "cleanup_errors", ()))
    assert any("named client delete failed" in item
               for item in [*notes, *cleanup_errors])


def test_wsl_probe_requires_contract_exit_and_blocked_marker() -> None:
    _assert_wsl_blocked(
        subprocess.CompletedProcess([], 3, "BLOCKED TimeoutError\n", ""))
    for result in (
        subprocess.CompletedProcess([], 1, "CONNECTED\n", "infra failure"),
        subprocess.CompletedProcess([], 3, "CONNECTED\n", ""),
        subprocess.CompletedProcess([], 3, "", "probe crash"),
    ):
        with pytest.raises(AssertionError):
            _assert_wsl_blocked(result)


def test_desktop_topology_contract_is_explicit_and_secret_free() -> None:
    assert TOPOLOGY.is_file(), "A2 必须先提交 deploy/desktop/topology.json"
    data = json.loads(TOPOLOGY.read_text(encoding="utf-8"))

    assert data["schema"] == "easel.desktop.topology.v1"
    assert data["status"] == "assembled_a4"
    assert data["approval_required"] is False
    assert data["implementation"]["enabled"] is True
    assert data["implementation"]["control_relay"]["status"] == "assembled"

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
    runtime = management["worker_runtime"]
    assert runtime["implemented"] is True
    assert runtime["uid"] == 10001
    assert "easel.worker.server" in runtime["entry"]
    assert runtime["public_key_mount"] == "/keys/worker-public.key:ro"
    assert management["health_probe"]["mode"] == "docker_exec_required"
    assert management["health_probe"]["implemented"] is True
    assert "container_id" in management["health_probe"]["target"]
    assert management["health_probe"]["timeout_seconds"] == 10
    assert "任意退出 0" in management["health_probe"]["success"]
    assert "A3" in management["health_probe"]["execution_capability"]

    network = data["network"]
    assert network["worker_ports"] == []
    assert network["control"]["docker_socket"] is False
    assert network["worker"]["docker_socket"] is False
    assert network["worker"]["publish_all_ports"] is False
    assert network["control_to_worker"]["mode"] == "gateway_required"
    assert network["wsl_to_worker"]["http"] == "blocked"
    assert network["windows_to_worker"]["http"] == "blocked"
    gateway = network["gateway"]
    assert gateway["enabled"] is True
    assert gateway["public_relay_enabled"] is True
    assert gateway["docker_socket"] is False
    assert gateway["worker_ports"] == []
    assert gateway["workspace_mounts"] == []
    assert gateway["database_mounts"] == []
    assert not any("docker.sock" in mount for mount in gateway["mounts"])
    assert not any("/workspace" in mount for mount in gateway["mounts"])
    assert gateway["management_bind"] == "control_network_ip_only"
    assert gateway["management_port"] == 8897
    assert "docker inspect" in gateway["management_bind_procedure"]
    assert gateway["worker_to_management"] == (
        "blocked_by_bind_and_network")
    assert gateway["sse"] == "streaming_passthrough"

    assert gateway["routes"] == EXPECTED_ROUTES
    assert gateway["routes"][2]["query"][0]["repeat"] == "reject"
    assert gateway["routes"][2]["query"][1]["max_length"] == 128
    assert gateway["routes"][6]["query"][0]["max"] == 500
    assert any(route["method"] == "GET"
               and route["path"].endswith("/messages")
               for route in gateway["routes"])
    assert not any(route["path"] in (
        "/internal/runs/{run_id}/question",
        "/internal/runs/{run_id}/question/answer",
    ) for route in gateway["routes"])
    assert "未知" in gateway["url_rules"]["query"]
    assert "不跟随" in gateway["url_rules"]["redirect"]
    assert gateway["id_rules"]["run_id"] == "^[0-9a-f]{32}$"
    assert "解码" in gateway["url_rules"]["path"]
    assert "Cookie" in gateway["header_rules"]["strip_inbound"]
    assert "X-Easel-*" in gateway["header_rules"]["strip_inbound"]

    auth = gateway["auth"]
    assert auth["control_to_gateway"]["scheme"] == "ed25519_control_assertion"
    assert auth["control_to_gateway"]["expires_seconds"] == 30
    assert "worker_container_id" in auth["control_to_gateway"]["fields"]
    assert "body_sha256" in auth["control_to_gateway"]["fields"]
    assert auth["gateway_to_worker"]["scheme"] == "worker_service_token"
    assert "X-Easel-Service-Token" in auth["gateway_to_worker"]["headers"]
    assert "独立" in auth["independence"]

    binding = gateway["instance_binding"]
    assert "container_id" in binding["worker"]
    assert "gateway_instance_id" in binding["gateway"]
    assert "gateway_generation" in binding["gateway"]
    assert "gateway_instance_id" in binding["stop"]
    assert set(gateway["recovery"]) >= {
        "control_restart", "provisioner_restart", "gateway_restart",
        "gateway_unavailable", "gateway_replacement", "worker_replacement",
        "config_revision",
    }
    assert "503" in gateway["recovery"]["gateway_unavailable"]
    assert gateway["limits"]["max_header_bytes"] == 65536
    assert gateway["limits"]["max_body_bytes"] == 67108864
    assert gateway["limits"]["connect_timeout_seconds"] == 10
    assert gateway["limits"]["io_timeout_seconds"] == 120
    assert gateway["limits"]["sse_idle_timeout_seconds"] == 120
    assert gateway["limits"]["max_concurrent_requests"] == 8
    assert "drain" in gateway["limits"]["sse_backpressure"]

    assert data["paths"]["workspace_root_windows"].startswith("C:")
    assert data["paths"]["workspace_root_wsl"].startswith("/mnt/c/")
    assert data["paths"]["container_workspace"] == "/workspace"
    assert "UID 10001" in data["paths"]["persistence_probe"]
    assert "SQLite" in data["paths"]["persistence_probe"]
    assert "/tmp" in data["paths"]["persistence_probe"]
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
    with _tracked(run_id) as resources:
        resources.append(("network", ws))
        _create_network(ws, run_id, internal=True)
        resources.append(("container", worker))
        _start_http(worker, ws, 7860, run_id)
        time.sleep(1.0)
        address = _container_ip(worker, ws)
        assert _container_http(worker, "127.0.0.1", 7860) == 200
        _assert_http_blocked(
            lambda: _host_http(address, 7860),
            "Windows 宿主不应能用 HTTP 探测 worker")
        if WSL is not None:
            _assert_wsl_blocked(_wsl_http(address, 7860))


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
    with _tracked(run_id) as resources:
        resources.append(("network", ws))
        resources.append(("network", ctl))
        _create_network(ws, run_id, internal=True)
        _create_network(ctl, run_id, internal=False)
        resources.append(("container", worker))
        resources.append(("container", control))
        _start_http(worker, ws, 7860, run_id)
        _start_http(control, ctl, 7861, run_id)
        time.sleep(1.0)
        assert _container_http(worker, "127.0.0.1", 7860) == 200
        assert _container_http(control, "127.0.0.1", 7861) == 200
        worker_ip = _container_ip(worker, ws)
        control_ip = _container_ip(control, ctl)
        _assert_http_blocked(
            lambda: _container_http(control, worker_ip, 7860),
            "独立 control 容器不应到达 worker")
        _assert_http_blocked(
            lambda: _container_http(worker, control_ip, 7861),
            "worker 不应到达独立 control 容器")


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
    with _tracked(run_id) as resources:
        resources.append(("network", ws))
        resources.append(("network", ctl))
        _create_network(ws, run_id, internal=True)
        _create_network(ctl, run_id, internal=False)
        resources.append(("container", worker))
        resources.append(("container", control))
        _start_http(worker, ws, 7860, run_id)
        _start_http(control, ctl, 7861, run_id)
        _docker("network", "connect", ws, control)
        time.sleep(1.0)
        assert _container_http(worker, "127.0.0.1", 7860) == 200
        assert _container_http(control, "127.0.0.1", 7861) == 200
        worker_ip = _container_ip(worker, ws)
        control_ws_ip = _container_ip(control, ws)
        assert _container_http(control, worker_ip, 7860) == 200
        assert _container_http(worker, control_ws_ip, 7861) == 200


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
    win_client = f"easel-a2-{run_id}-win-client"
    wsl_client = f"easel-a2-{run_id}-wsl-client"
    with _tracked(run_id) as resources:
        resources.append(("container", win_client))
        resources.append(("container", wsl_client))
        with _wsl_temp([wsl_dir]):
            _wsl_python(
                "import os,sys;os.makedirs(sys.argv[1],exist_ok=True);"
                "open(sys.argv[2],'w',encoding='ascii').write('wsl-ext4')",
                wsl_dir, wsl_file)
            windows_mount = _wsl(
                "docker", "run", "--name", win_client,
                *_label_args(run_id),
                "-v", f"{_to_wsl_path(tmp_path)}:/check:ro", PYTHON_IMAGE,
                "sh", "-c", "cat /check/marker.txt")
            assert windows_mount.stdout.strip() == "windows-mounted"
            wsl_mount = _wsl(
                "docker", "run", "--name", wsl_client,
                *_label_args(run_id),
                "-v", f"{wsl_dir}:/check:ro", PYTHON_IMAGE,
                "sh", "-c", "cat /check/marker.txt")
            assert wsl_mount.stdout.strip() == "wsl-ext4"


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
    client_name = f"easel-a2-{run_id}-socket-client"
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
    with _tracked(run_id) as resources:
        resources.append(("container", client_name))
        with _wsl_temp([sock, ready]):
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
                    "docker", "run", "--name", client_name,
                    *_label_args(run_id),
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
