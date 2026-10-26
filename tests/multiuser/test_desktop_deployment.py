"""Task A1：Windows Docker Desktop 登录管理面的部署契约。

这些测试不连接 Docker daemon，也不读取开发者数据。PowerShell 脚本通过
临时 PATH 中的 docker 桩执行；Compose 仅用 `docker compose config` 做静态
展开，不创建容器。真实的镜像、HTTPS 与双浏览器证据在交付说明中单独记录。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "desktop"
DOCKERFILE = DEPLOY / "Dockerfile.control"
COMPOSE = DEPLOY / "compose.login.yaml"
INITIALIZE = DEPLOY / "Initialize-Easel.ps1"
START = DEPLOY / "Start-Easel.ps1"
TEST_SCRIPT = DEPLOY / "Test-Easel.ps1"
KEYGEN = DEPLOY / "keygen.py"

IMAGE = "sha256:" + "a" * 64
ORIGIN = "https://a1-desktop.ngrok-free.app"
PROJECT = "easel-login-a1-test"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")

pytestmark = pytest.mark.skipif(
    POWERSHELL is None, reason="Windows PowerShell 不可用")


def _desktop_env(tmp_path: Path) -> dict[str, str]:
    return {
        "EASEL_CONTROL_IMAGE": IMAGE,
        "EASEL_PUBLIC_ORIGIN": ORIGIN,
        "EASEL_DESKTOP_PROJECT": PROJECT,
        "EASEL_CONTROL_VOLUME": f"{PROJECT}-control",
        "EASEL_KEY_VOLUME": f"{PROJECT}-keys",
        "EASEL_CONTROL_PORT": "7861",
    }


_FAKE_DOCKER = r"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

args = sys.argv[1:]
state = pathlib.Path(os.environ["FAKE_DOCKER_STATE"])
state.mkdir(parents=True, exist_ok=True)
with (state / "calls.log").open("a", encoding="utf-8") as log:
    log.write(json.dumps(args, ensure_ascii=False) + "\n")
scenario = os.environ.get("FAKE_DOCKER_SCENARIO", "ok")
volume_scenario = os.environ.get("FAKE_VOLUME_SCENARIO", "owned")
inspect_scenario = os.environ.get("FAKE_INSPECT_SCENARIO", "valid")
keygen = os.environ["EASEL_KEYGEN_PATH"]
project = os.environ["FAKE_PROJECT"]
origin = os.environ["FAKE_ORIGIN"]
image = os.environ["FAKE_IMAGE"]


def out(text: str = "") -> None:
    if text:
        print(text)


def fail(text: str, code: int = 1) -> None:
    print(text, file=sys.stderr)
    raise SystemExit(code)


def after(name: str) -> str:
    try:
        return args[args.index(name) + 1]
    except ValueError:
        return ""


def volume_record(name: str, labels):
    return {"Name": name, "Labels": labels}


def valid_container() -> dict:
    control_name = f"{project}-control"
    key_name = f"{project}-keys"
    actual_origin = ("https://wrong.example"
                     if inspect_scenario == "wrong_origin" else origin)
    command = [
        "python", "-m", "easel", "serve-control",
        "--control-dir", "/srv/easel/control",
        "--public-origin", actual_origin,
        "--master-key-file", "/keys/master.key",
        "--frontend-dir", "/opt/easel/web/frontend/dist",
        "--host", "0.0.0.0", "--port", "7861",
    ]
    if inspect_scenario == "wrong_command":
        command = ["python", "wrong_entry.py"]
    mounts = [
        {"Type": "volume", "Name": control_name,
         "Source": f"/var/lib/docker/volumes/{control_name}/_data",
         "Destination": "/srv/easel/control", "RW": True},
        {"Type": "volume", "Name": key_name,
         "Source": f"/var/lib/docker/volumes/{key_name}/_data",
         "Destination": "/keys", "RW": False},
    ]
    if inspect_scenario == "wrong_mount_source":
        mounts[0]["Name"] = f"{project}-foreign"
    if inspect_scenario == "bind_mount":
        mounts[1] = {"Type": "bind", "Name": "", "Source": "C:/host/keys",
                     "Destination": "/keys", "RW": False}
    if inspect_scenario == "key_writable":
        mounts[1]["RW"] = True
    ports = {"7861/tcp": [{"HostIp": "127.0.0.1", "HostPort": "7861"}]}
    if inspect_scenario == "extra_port":
        ports["9999/tcp"] = [{"HostIp": "127.0.0.1", "HostPort": "9999"}]
    labels = {
        "com.docker.compose.project": project,
        "com.docker.compose.service": "control",
    }
    if inspect_scenario == "wrong_compose_project":
        labels["com.docker.compose.project"] = "other-project"
    if inspect_scenario == "wrong_compose_service":
        labels["com.docker.compose.service"] = "other-service"
    container = {
        "Id": "fake-control-id",
        "Image": image,
        "Config": {
            "User": "10001:10001",
            "Entrypoint": None,
            "Cmd": command,
            "Labels": labels,
        },
        "HostConfig": {
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Privileged": False,
            "NetworkMode": "bridge",
        },
        "Mounts": mounts,
        "NetworkSettings": {"Ports": ports},
        "State": {"Health": {"Status": "healthy"}},
    }
    if inspect_scenario == "wrong_entrypoint":
        container["Config"]["Entrypoint"] = ["python", "wrong_entry.py"]
    if inspect_scenario == "missing_config":
        del container["Config"]
    if inspect_scenario == "missing_mounts":
        del container["Mounts"]
    return container


if not args:
    fail("fake docker: missing args", 2)

if args[0] == "image" and args[1:2] == ["inspect"]:
    if scenario == "missing_image":
        fail("image missing")
    out(image)
    raise SystemExit(0)

if args[0] == "volume":
    action = args[1] if len(args) > 1 else ""
    name = args[-1] if len(args) > 2 else ""
    marker = state / "volumes" / name
    if action == "inspect":
        if not marker.exists():
            fail("No such volume")
        labels_path = marker.with_name(marker.name + ".json")
        labels = (json.loads(labels_path.read_text(encoding="utf-8"))
                  if labels_path.exists() else None)
        if volume_scenario == "bad_json":
            out("not-json")
        elif volume_scenario == "empty_inspect":
            out("[]")
        elif volume_scenario == "multiple_inspect":
            out(json.dumps([volume_record(name, labels),
                            volume_record(name, labels)]))
        elif "--format" in args:
            out(json.dumps(labels))
        elif volume_scenario == "name_mismatch":
            out(json.dumps([volume_record(name + "-other", labels)]))
        else:
            out(json.dumps([volume_record(name, labels)]))
        raise SystemExit(0)
    if action == "create":
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1", encoding="utf-8")
        labels = {}
        for index, item in enumerate(args):
            if item == "--label" and index + 1 < len(args):
                key, _, value = args[index + 1].partition("=")
                labels[key] = value
        (marker.with_name(marker.name + ".json")).write_text(
            json.dumps(labels), encoding="utf-8")
        out(name)
        raise SystemExit(0)
    fail("unsupported volume action")

if args[0] == "run":
    joined = " ".join(args)
    if "deploy/desktop/keygen.py" in joined:
        mode = "--ensure" if "--ensure" in args else "--check"
        mapped = state / "key" / pathlib.Path(
            after("--path") or "/keys/master.key").name
        mapped.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [sys.executable, keygen, mode, "--path", str(mapped)],
            capture_output=True, text=True)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    if "frontend/dist/index.html" in joined:
        if scenario == "missing_frontend":
            fail("frontend_build_missing")
        raise SystemExit(0)
    raise SystemExit(0)

if args[0] == "compose":
    if "config" in args:
        if scenario == "bad_compose":
            fail("compose invalid")
        raise SystemExit(0)
    if "up" in args:
        (state / "compose-up").write_text("1", encoding="utf-8")
        raise SystemExit(0)
    if "ps" in args:
        out("fake-control-id")
        raise SystemExit(0)
    raise SystemExit(0)

if args[0] == "inspect":
    if "--format" in args:
        out("healthy")
        raise SystemExit(0)
    if inspect_scenario == "empty_inspect":
        out("[]")
    elif inspect_scenario == "multiple_inspect":
        out(json.dumps([valid_container(), valid_container()]))
    elif inspect_scenario == "invalid_inspect":
        out("not-json")
    else:
        out(json.dumps([valid_container()]))
    raise SystemExit(0)

fail("fake docker: unsupported " + " ".join(args))
"""


def _fake_docker(tmp_path: Path, scenario: str = "ok",
                 precreate_volumes: bool = True,
                 volume_scenario: str = "owned",
                 inspect_scenario: str = "valid"):
    state = tmp_path / "docker-state"
    state.mkdir()
    if precreate_volumes:
        volumes = state / "volumes"
        volumes.mkdir()
        for role, name in (("control", f"{PROJECT}-control"),
                           ("keys", f"{PROJECT}-keys")):
            marker = volumes / name
            marker.write_text("1", encoding="utf-8")
            project_label = PROJECT
            role_label = role
            if (scenario == "foreign_volume"
                    or volume_scenario == "foreign_volume"):
                project_label = "foreign-project"
            if volume_scenario == "foreign_control" and role == "control":
                project_label = "foreign-project"
            if volume_scenario == "foreign_keys" and role == "keys":
                project_label = "foreign-project"
            if volume_scenario == "wrong_role" and role == "control":
                role_label = "keys"
            if volume_scenario != "missing_labels":
                labels = {
                    "easel.desktop.project": project_label,
                    "easel.desktop.role": role_label,
                }
                (volumes / f"{name}.json").write_text(
                    json.dumps(labels), encoding="utf-8")
    if scenario in ("missing_frontend", "owned"):
        # 这些场景需要合法 key，确保失败/成功点不是 key。
        key = state / "key" / "master.key"
        key.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, str(KEYGEN), "--ensure", "--path", str(key)],
            check=True, capture_output=True)
    fake = tmp_path / "fake_docker.py"
    fake.write_text(_FAKE_DOCKER, encoding="utf-8")
    binary = tmp_path / "bin"
    binary.mkdir()
    wrapper = binary / "docker.cmd"
    wrapper.write_text(
        f'@echo off\r\n"{sys.executable}" "{fake}" %*\r\n',
        encoding="utf-8")
    env = os.environ.copy()
    env["PATH"] = str(binary) + os.pathsep + env.get("PATH", "")
    env["FAKE_DOCKER_STATE"] = str(state)
    env["FAKE_DOCKER_SCENARIO"] = scenario
    env["FAKE_VOLUME_SCENARIO"] = volume_scenario
    env["FAKE_INSPECT_SCENARIO"] = inspect_scenario
    env["FAKE_PROJECT"] = PROJECT
    env["FAKE_ORIGIN"] = ORIGIN
    env["FAKE_IMAGE"] = IMAGE
    env["EASEL_KEYGEN_PATH"] = str(KEYGEN)
    return state, env


def _run_ps(script: Path, *args: str, env: dict[str, str]):
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, check=False, timeout=60)


def _calls(state: Path) -> list[list[str]]:
    path = state / "calls.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(
        encoding="utf-8").splitlines() if line.strip()]


def _has_compose_up(calls: list[list[str]]) -> bool:
    return any(call and call[0] == "compose" and "up" in call
               for call in calls)


@pytest.mark.parametrize("origin", [
    "http://a1-desktop.ngrok-free.app",
    "https://a1-desktop.ngrok-free.app/path",
    "https://user:pass@a1-desktop.ngrok-free.app",
    "https://a1-desktop.ngrok-free.app?x=1",
    " https://a1-desktop.ngrok-free.app",
])
def test_start_rejects_invalid_origin_before_any_docker_call(
        tmp_path, origin):
    state, env = _fake_docker(tmp_path)
    result = _run_ps(START, "-PublicOrigin", origin, "-Image", IMAGE,
                     "-ProjectName", PROJECT, env=env)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "invalid_origin" in output
    assert not (state / "calls.log").exists(), "非法 Origin 不应调用 Docker"


def test_start_rejects_unpinned_image_before_any_docker_call(tmp_path):
    state, env = _fake_docker(tmp_path)
    result = _run_ps(
        START, "-PublicOrigin", ORIGIN, "-Image", "easel-control:latest",
        "-ProjectName", PROJECT, env=env)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "control_image_not_pinned" in output
    assert not (state / "calls.log").exists(), "可变镜像不应启动"


@pytest.mark.parametrize("scenario,code", [
    ("missing_key", "master_key_missing"),
    ("missing_frontend", "frontend_build_missing"),
])
def test_start_fails_before_compose_up_when_image_contract_is_missing(
        tmp_path, scenario, code):
    state, env = _fake_docker(tmp_path, scenario=scenario)
    result = _run_ps(
        START, "-PublicOrigin", ORIGIN, "-Image", IMAGE,
        "-ProjectName", PROJECT, env=env)
    assert result.returncode != 0
    assert code in result.stdout + result.stderr
    calls = _calls(state)
    assert calls, "应先执行只读检查"
    assert not _has_compose_up(calls), "缺 key/前端时不得启动 Compose"
    assert not (state / "compose-up").exists()


def test_initialize_is_idempotent_and_never_overwrites_key(tmp_path):
    state, env = _fake_docker(tmp_path, precreate_volumes=False)
    first = _run_ps(INITIALIZE, "-Image", IMAGE,
                    "-ProjectName", PROJECT, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    key = state / "key" / "master.key"
    assert key.is_file()
    original = key.read_bytes()
    assert len(original) == 32
    assert "key=created" in first.stdout

    second = _run_ps(INITIALIZE, "-Image", IMAGE,
                     "-ProjectName", PROJECT, env=env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert key.read_bytes() == original, "重复初始化不得覆盖 master key"
    assert "key=exists" in second.stdout

    calls = _calls(state)
    assert not any("rm" in call or "down" in call for call in calls)
    assert not any("compose" in call and "up" in call for call in calls)


def test_initialize_refuses_foreign_volume_before_chown(tmp_path):
    state, env = _fake_docker(tmp_path, scenario="foreign_volume")
    result = _run_ps(INITIALIZE, "-Image", IMAGE,
                     "-ProjectName", PROJECT, env=env)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "volume_not_owned" in output
    calls = _calls(state)
    assert not any(call and call[0] == "run" for call in calls), (
        "未归属卷不得进入 root chown 助手")
    assert not (state / "key" / "master.key").exists()


@pytest.mark.parametrize("volume_scenario,code", [
    ("foreign_control", "volume_not_owned"),
    ("foreign_keys", "volume_not_owned"),
    ("missing_labels", "volume_not_owned"),
    ("name_mismatch", "volume_not_owned"),
    ("wrong_role", "volume_not_owned"),
    ("bad_json", "volume_inspect_invalid"),
    ("empty_inspect", "volume_inspect_invalid"),
    ("multiple_inspect", "volume_inspect_invalid"),
])
def test_start_rejects_volume_contract_violations(
        tmp_path, volume_scenario, code):
    state, env = _fake_docker(
        tmp_path, scenario="owned", volume_scenario=volume_scenario)
    key = state / "key" / "master.key"
    before = key.read_bytes()
    result = _run_ps(
        START, "-PublicOrigin", ORIGIN, "-Image", IMAGE,
        "-ProjectName", PROJECT, env=env)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert code in output
    calls = _calls(state)
    assert not any(call and call[0] == "run" for call in calls), (
        "卷归属失败后不得调用 helper run")
    assert not _has_compose_up(calls), "卷归属失败后不得 Compose up"
    assert key.read_bytes() == before, "卷归属失败后不得生成/替换 key"


def test_start_rejects_missing_volume_before_helper(tmp_path):
    state, env = _fake_docker(
        tmp_path, scenario="owned", precreate_volumes=False)
    result = _run_ps(
        START, "-PublicOrigin", ORIGIN, "-Image", IMAGE,
        "-ProjectName", PROJECT, env=env)
    assert result.returncode != 0
    assert "volume_inspect_failed" in result.stdout + result.stderr
    calls = _calls(state)
    assert not any(call and call[0] == "run" for call in calls)
    assert not _has_compose_up(calls)


def test_start_accepts_owned_volumes_and_can_repeat(tmp_path):
    state, env = _fake_docker(tmp_path, scenario="owned")
    key = state / "key" / "master.key"
    before = key.read_bytes()
    for _ in range(2):
        result = _run_ps(
            START, "-PublicOrigin", ORIGIN, "-Image", IMAGE,
            "-ProjectName", PROJECT, env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "started project=" in result.stdout
    assert key.read_bytes() == before
    calls = _calls(state)
    assert any(call and call[0] == "compose" and "up" in call
               for call in calls)
    assert not any(call and call[0] == "volume" and "create" in call
                   for call in calls)


@pytest.mark.parametrize("volume_scenario,code", [
    ("foreign_control", "volume_not_owned"),
    ("missing_labels", "volume_not_owned"),
    ("name_mismatch", "volume_not_owned"),
    ("bad_json", "volume_inspect_invalid"),
])
def test_test_easel_rejects_volume_contract_before_container_inspect(
        tmp_path, volume_scenario, code):
    state, env = _fake_docker(
        tmp_path, scenario="owned", volume_scenario=volume_scenario)
    result = _run_ps(
        TEST_SCRIPT, "-PublicOrigin", ORIGIN, "-Image", IMAGE,
        "-ProjectName", PROJECT, env=env)
    assert result.returncode != 0
    assert code in result.stdout + result.stderr
    calls = _calls(state)
    assert not any(call and call[0] == "compose" for call in calls), (
        "卷归属失败后不应读取/启动 Compose")
    assert not any(call and call[0] == "inspect" for call in calls), (
        "卷归属失败后不应检查运行容器")


def test_diagnose_accepts_expected_container(tmp_path):
    _state, env = _fake_docker(tmp_path, scenario="owned")
    result = _run_ps(
        TEST_SCRIPT, "-PublicOrigin", ORIGIN, "-Image", IMAGE,
        "-ProjectName", PROJECT, env=env)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    for name in (
        "image_matches", "compose_project", "compose_service",
        "command_matches", "control_volume_source", "key_volume_source",
        "control_volume_writable", "key_volume_readonly",
        "only_published_port", "loopback_port", "master_key_present",
    ):
        assert f"[ok] {name}" in output
    assert "diagnostics=ok" in output


@pytest.mark.parametrize("inspect_scenario,expected", [
    ("wrong_mount_source", "[fail] control_volume_source"),
    ("bind_mount", "[fail] key_volume_source"),
    ("key_writable", "[fail] key_volume_readonly"),
    ("wrong_origin", "[fail] command_matches"),
    ("wrong_command", "[fail] command_matches"),
    ("extra_port", "[fail] only_published_port"),
    ("wrong_compose_project", "[fail] compose_project"),
    ("wrong_compose_service", "[fail] compose_service"),
    ("wrong_entrypoint", "[fail] command_matches"),
    ("empty_inspect", "container_inspect_invalid"),
    ("multiple_inspect", "container_inspect_invalid"),
    ("invalid_inspect", "container_inspect_invalid"),
    ("missing_config", "container_inspect_invalid"),
    ("missing_mounts", "container_inspect_invalid"),
])
def test_diagnose_rejects_actual_wiring_mismatch(
        tmp_path, inspect_scenario, expected):
    _state, env = _fake_docker(
        tmp_path, scenario="owned", inspect_scenario=inspect_scenario)
    result = _run_ps(
        TEST_SCRIPT, "-PublicOrigin", ORIGIN, "-Image", IMAGE,
        "-ProjectName", PROJECT, env=env)
    assert result.returncode != 0
    assert expected in result.stdout + result.stderr


def _load_keygen():
    spec = importlib.util.spec_from_file_location("desktop_keygen", KEYGEN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_keygen_creates_once_and_rejects_malformed_existing_key(tmp_path):
    module = _load_keygen()

    path = tmp_path / "master.key"
    assert module.ensure_master_key(path) == "created"
    original = path.read_bytes()
    assert len(original) == 32
    before = path.stat().st_mtime_ns
    assert module.ensure_master_key(path) == "exists"
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == before

    malformed = tmp_path / "short.key"
    malformed.write_bytes(b"x" * 31)
    with pytest.raises(ValueError, match="invalid_master_key"):
        module.ensure_master_key(malformed)
    assert malformed.read_bytes() == b"x" * 31
    with pytest.raises(ValueError, match="key_missing"):
        module.check_master_key(tmp_path / "absent.key")


def test_keygen_writes_exact_32_bytes_even_when_newline_is_drawn(
        tmp_path, monkeypatch):
    """Windows 文本模式会把 \n 写成 \r\n；key 仍必须严格 32 字节。"""
    module = _load_keygen()
    monkeypatch.setattr(module.secrets, "token_bytes",
                        lambda size: b"\n" * size)
    path = tmp_path / "master.key"
    assert module.ensure_master_key(path) == "created"
    assert path.read_bytes() == b"\n" * 32


def _compose_config(tmp_path: Path) -> dict:
    if shutil.which("docker") is None:
        pytest.skip("docker compose 不可用")
    env = os.environ.copy()
    env.update(_desktop_env(tmp_path))
    result = subprocess.run(
        ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE),
         "config", "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, check=False, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_control_service_is_loopback_only_and_has_no_worker_control_mount(
        tmp_path):
    config = _compose_config(tmp_path)
    service = config["services"]["control"]
    assert service["image"] == IMAGE
    assert service["user"] == "10001:10001"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert not service.get("privileged", False)
    assert service.get("network_mode") != "host"

    ports = service["ports"]
    assert len(ports) == 1
    assert ports[0]["host_ip"] == "127.0.0.1"
    assert int(ports[0]["published"]) == 7861
    assert int(ports[0]["target"]) == 7861
    assert ports[0]["protocol"] == "tcp"

    volumes = service["volumes"]
    sources = {item["source"] for item in volumes}
    targets = {item["target"] for item in volumes}
    assert sources == {"control", "keys"}
    assert targets == {"/srv/easel/control", "/keys"}
    declared = config["volumes"]
    assert declared["control"]["external"] is True
    assert declared["control"]["name"] == f"{PROJECT}-control"
    assert declared["keys"]["external"] is True
    assert declared["keys"]["name"] == f"{PROJECT}-keys"
    assert not any("docker.sock" in item["source"] for item in volumes)
    assert not any("workspace" in item["source"].lower() for item in volumes)

    command = service["command"]
    assert "serve-control" in command
    assert command[command.index("--control-dir") + 1] == "/srv/easel/control"
    assert command[command.index("--master-key-file") + 1] == "/keys/master.key"
    assert (command[command.index("--frontend-dir") + 1]
            == "/opt/easel/web/frontend/dist")
    assert command[command.index("--public-origin") + 1] == ORIGIN


def test_control_image_builds_current_source_and_frontend():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "task8" not in text.lower()
    assert "FROM node:24" in text
    assert "npm ci" in text
    assert "npm run build" in text
    assert "COPY easel ./easel" in text
    assert "COPY skills/shared/scripts/model_registry.py" in text
    assert "COPY --from=frontend" in text
    assert "/opt/easel/web/frontend/dist" in text
    assert "USER 10001:10001" in text
    assert "/var/run/docker.sock" not in text
    assert ".env" not in text


def test_operator_scripts_never_read_ngrok_or_master_key_contents():
    for script in (INITIALIZE, START, TEST_SCRIPT):
        text = script.read_text(encoding="utf-8").lower()
        assert "authtoken" not in text
        assert "ngrok.yml" not in text
        assert "get-content" not in text
        assert "invoke-restmethod" not in text
