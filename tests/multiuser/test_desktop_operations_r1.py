"""A7-R1：正式 Desktop 脚本的源根、升级前置与 Initialize 反例。

使用临时 Docker/WSL 桩执行真实 PowerShell 控制流；不连接 Docker daemon，
不读取开发者卷/凭证。失败断言关注副作用（是否启动 compose、是否生成 key），
而不是只匹配脚本文本。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from easel.control.store import Store

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "desktop"
BACKUP = DEPLOY / "Backup-WorkspaceEasel.ps1"
START = DEPLOY / "Start-WorkspaceEasel.ps1"
INITIALIZE = DEPLOY / "Initialize-WorkspaceEasel.ps1"
KEYGEN = DEPLOY / "keygen.py"
INIT_STATE = DEPLOY / "initialize_state.py"

POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
CSC = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
IMAGE = "sha256:" + "a" * 64
WORKER = "sha256:" + "b" * 64
GATEWAY = "sha256:" + "c" * 64
PROJECT = "easel-a7-r1-test"
ORIGIN = "https://a7-r1.ngrok-free.app"
STOP = {
    "barrierId": "stop-test",
    "project": PROJECT,
    "configSha256": "e" * 64,
    "completedAt": 1,
}

pytestmark = pytest.mark.skipif(
    POWERSHELL is None or os.name != "nt",
    reason="Windows PowerShell 不可用")


_FAKE_DOCKER = r"""
from __future__ import annotations

import hashlib
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
image = os.environ["FAKE_IMAGE"]
project = os.environ["FAKE_PROJECT"]


def out(text: str = "") -> None:
    if text:
        print(text)


def fail(text: str, code: int = 1) -> None:
    print(text, file=sys.stderr)
    raise SystemExit(code)


def mapped(path: str) -> pathlib.Path:
    if path.startswith("/keys/"):
        return state / "volumes" / f"{project}-keys" / path[len("/keys/"):]
    if path.startswith("/policy/"):
        return (state / "volumes" / f"{project}-egress" /
                path[len("/policy/"):])
    if path == "/keys":
        return state / "volumes" / f"{project}-keys"
    if path == "/policy":
        return state / "volumes" / f"{project}-egress"
    return state / "data" / path.lstrip("/")


def volume_name(prefix: str) -> str:
    return f"{project}-{prefix}"


if not args:
    fail("fake docker: missing args", 2)

if args[0] == "volume":
    action = args[1] if len(args) > 1 else ""
    name = args[-1] if len(args) > 2 else ""
    marker = state / "volumes" / name
    if action == "inspect":
        if not marker.exists():
            fail("No such volume")
        labels_path = marker.with_name(marker.name + ".json")
        labels = (json.loads(labels_path.read_text(encoding="utf-8"))
                  if labels_path.exists() else {})
        if "--format" in args:
            out(json.dumps(labels))
        else:
            out(json.dumps({"Name": name, "Labels": labels}))
        raise SystemExit(0)
    if action == "create":
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.mkdir(exist_ok=True)
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

if args[0] == "network":
    action = args[1] if len(args) > 1 else ""
    name = args[-1] if len(args) > 2 else ""
    if action == "inspect":
        out(json.dumps({
            "Name": name,
            "Labels": {
                "easel.desktop.project": project,
                "easel.desktop.role": "control-network",
            },
            "Driver": "bridge",
            "Internal": False,
            "EnableIPv6": False,
        }))
        raise SystemExit(0)
    if action == "create":
        out(name)
        raise SystemExit(0)
    fail("unsupported network action")

if args[0] == "image" and args[1:2] == ["inspect"]:
    out(args[-1])
    raise SystemExit(0)

if args[0] == "info":
    out("fake-engine-id")
    raise SystemExit(0)

if args[0] == "ps":
    out("fake-control-id")
    raise SystemExit(0)

if args[0] == "inspect":
    if "--format" in args:
        out("healthy")
    else:
        out(json.dumps([{"Id": "fake-control-id"}]))
    raise SystemExit(0)

if args[0] == "exec":
    joined = " ".join(args)
    if "barrier-status" in joined:
        out("barrier=none")
    elif "stop-status" in joined:
        out("stop_completion=none")
    else:
        out("")
    raise SystemExit(0)

if args[0] == "run":
    joined = " ".join(args)
    if "initialize_state.py" in joined:
        # 直接执行真实只读探测脚本，测试与容器行为一致。
        index = args.index("/opt/easel/deploy/desktop/initialize_state.py")
        mapped_args = []
        for item in args[index + 1:]:
            if item == "/data":
                mapped_args.append(str(state / "volumes" /
                                       f"{project}-control"))
            elif item == "/keys":
                mapped_args.append(str(state / "volumes" / f"{project}-keys"))
            else:
                mapped_args.append(item)
        result = subprocess.run(
            [sys.executable, os.environ["EASEL_INIT_STATE_PATH"]] + mapped_args,
            capture_output=True, text=True)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    if "keygen.py" in joined:
        index = args.index("/opt/easel/deploy/desktop/keygen.py")
        mapped_args = []
        for item in args[index + 1:]:
            if item.startswith("/keys/"):
                mapped_args.append(str(mapped(item)))
            else:
                mapped_args.append(item)
        result = subprocess.run(
            [sys.executable, os.environ["EASEL_KEYGEN_PATH"]] + mapped_args,
            capture_output=True, text=True)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    if any(item == "sha256sum" for item in args):
        for item in args:
            if item.startswith("/"):
                path = mapped(item)
                if path.is_file():
                    out(hashlib.sha256(path.read_bytes()).hexdigest()
                        + "  " + item)
        raise SystemExit(0)
    if "admin backup" in joined:
        fail("backup should not run before source-root binding")
    if "frontend/dist/index.html" in joined:
        raise SystemExit(0)
    raise SystemExit(0)

fail("fake docker: unsupported " + " ".join(args))
"""


_FAKE_WSL = r"""
using System;
using System.IO;
using System.Text;

class FakeWsl {
    static int Main(string[] args) {
        var state = Environment.GetEnvironmentVariable("FAKE_WSL_STATE");
        Directory.CreateDirectory(state);
        File.AppendAllText(Path.Combine(state, "calls.log"),
                           string.Join("\u001f", args) + "\n");
        string joined = string.Join(" ", args);
        if (joined.Contains("read_stop_state.py")) {
            var stopState = Environment.GetEnvironmentVariable("FAKE_STOP_STATE");
            if (!string.IsNullOrEmpty(stopState)) {
                Console.WriteLine("stop_state=" + stopState);
            }
            var hashes = Environment.GetEnvironmentVariable("FAKE_KEY_HASHES");
            if (!string.IsNullOrEmpty(hashes)) {
                Console.WriteLine(hashes.Replace("\\n", "\n"));
            }
            return 0;
        }
        if (joined.Contains("verify-backup")) {
            if (Environment.GetEnvironmentVariable("FAKE_VERIFY_EXIT") == "0") {
                Console.WriteLine("version=2");
                Console.WriteLine("clean_stop=true");
                return 0;
            }
            Console.Error.WriteLine("错误：backup_integrity_failed");
            return 1;
        }
        if (joined.Contains("docker info")) {
            Console.WriteLine("fake-engine-id");
            return 0;
        }
        if (joined.Contains("docker context show")) {
            Console.WriteLine("desktop-linux");
            return 0;
        }
        if (joined.Contains("id -u") || joined.Contains("id -g")) {
            Console.WriteLine("1000");
            return 0;
        }
        if (joined.Contains("compose") && joined.Contains("up")) {
            File.WriteAllText(Path.Combine(state, "compose-up"), "1");
            return 0;
        }
        if (joined.Contains("compose") && joined.Contains("config")) {
            return 0;
        }
        if (joined.Contains("test -S")) {
            return 0;
        }
        if (joined.Contains("AF_UNIX")) {
            return 0;
        }
        if (joined.Contains("provisioner_identity.py")) {
            return 0;
        }
        return 0;
    }
}
"""


def _run_ps(script: Path, *args: str, env: dict[str, str]):
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, check=False, timeout=120)


def _base_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["FAKE_DOCKER_STATE"] = str(tmp_path / "docker-state")
    env["FAKE_WSL_STATE"] = str(tmp_path / "wsl-state")
    env["FAKE_IMAGE"] = IMAGE
    env["FAKE_PROJECT"] = PROJECT
    env["EASEL_KEYGEN_PATH"] = str(KEYGEN)
    env["EASEL_INIT_STATE_PATH"] = str(INIT_STATE)
    env["FAKE_STOP_STATE"] = json.dumps({
        "admission_barrier": {"id": STOP["barrierId"], "project": PROJECT},
        "stop_completion": STOP,
    })
    env["OS"] = "Other"  # 跳过 Windows ACL 辅助，测试只关心控制流
    return env


def _fake_docker(tmp_path: Path, env: dict[str, str]) -> None:
    fake = tmp_path / "fake_docker.py"
    fake.write_text(_FAKE_DOCKER, encoding="utf-8")
    binary = tmp_path / "bin"
    binary.mkdir(exist_ok=True)
    wrapper = binary / "docker.cmd"
    wrapper.write_text(
        f'@echo off\r\n"{sys.executable}" "{fake}" %*\r\n',
        encoding="utf-8")
    env["PATH"] = str(binary) + os.pathsep + env.get("PATH", "")
    state = Path(env["FAKE_DOCKER_STATE"])
    volumes = state / "volumes"
    volumes.mkdir(parents=True, exist_ok=True)
    for role, suffix in (("control", "control"), ("keys", "keys"),
                         ("egress-policy", "egress")):
        name = f"{PROJECT}-{suffix}"
        (volumes / name).mkdir(exist_ok=True)
        (volumes / f"{name}.json").write_text(json.dumps({
            "easel.desktop.project": PROJECT,
            "easel.desktop.role": role,
        }), encoding="utf-8")


def _fake_wsl(tmp_path: Path, env: dict[str, str]) -> None:
    if not CSC.is_file():
        pytest.skip("csc.exe 不可用")
    source = tmp_path / "FakeWsl.cs"
    source.write_text(_FAKE_WSL, encoding="utf-8")
    exe = tmp_path / "bin" / "wsl.exe"
    exe.parent.mkdir(exist_ok=True)
    compiled = subprocess.run(
        [str(CSC), "/nologo", "/target:exe", f"/out:{exe}", str(source)],
        capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    env["PATH"] = str(exe.parent) + os.pathsep + env["PATH"]


def _seed_deployment(tmp_path: Path, env: dict[str, str], *,
                     public: bool = True) -> tuple[Path, Path]:
    data_root = tmp_path / "data"
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir(parents=True)
    (data_root / "logs").mkdir(parents=True)
    deployment = {
        "project": PROJECT,
        "origin": ORIGIN,
        "port": 7861,
        "control_image": IMAGE,
        "worker_image": WORKER,
        "gateway_image": GATEWAY,
        "frontend_sha256": "d" * 64,
        "config_sha256": "e" * 64,
        "engine_id": "fake-engine-id",
        "engine_context": "desktop-linux",
        "source_commit": "f" * 40,
        "harness_revision": "1" * 40,
        "workspace_root": str(workspace_root),
        "data_root": str(data_root),
    }
    (data_root / "logs" / "deployment.json").write_text(
        json.dumps(deployment), encoding="utf-8")
    if public:
        public_dir = data_root / "public"
        public_dir.mkdir()
        keys = (Path(env["FAKE_DOCKER_STATE"]) / "volumes" /
                f"{PROJECT}-keys")
        keys.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, str(KEYGEN), "--ensure",
             "--path", str(keys / "master.key"),
             "--signing-key", str(keys / "snapshot.pem"),
             "--worker-public", str(keys / "worker-public.key"),
             "--gateway-public", str(keys / "gateway-public.pem")],
            check=True, capture_output=True)
        policy = keys / "egress-policy.json"
        policy.write_text('{"approved_hosts":["a"],"approved_cidrs":[]}',
                          encoding="utf-8")
        egress = (Path(env["FAKE_DOCKER_STATE"]) / "volumes" /
                  f"{PROJECT}-egress")
        egress.mkdir(parents=True, exist_ok=True)
        (egress / "egress-policy.json").write_bytes(policy.read_bytes())
        (keys / "egress-ca.pem").write_text("ca", encoding="utf-8")
        for name in ("worker-public.key", "gateway-public.pem",
                     "egress-policy.json"):
            (public_dir / name).write_bytes((keys / name).read_bytes())
    return data_root, workspace_root


def _manifest() -> dict:
    return {
        "format": "easel.control.backup",
        "version": 2,
        "complete": True,
        "schema_version": 14,
        "created_at": 1,
        "config_version": 0,
        "workspace_config_versions": {},
        "workspaces": {},
        "image": WORKER,
        "harness_revision": "1" * 40,
        "deployment": {
            "project": PROJECT,
            "origin": ORIGIN,
            "port": 7861,
            "control_image": IMAGE,
            "worker_image": WORKER,
            "gateway_image": GATEWAY,
            "frontend_sha256": "d" * 64,
            "config_sha256": "e" * 64,
            "engine_id": "fake-engine-id",
            "engine_context": "desktop-linux",
            "source_commit": "f" * 40,
            "harness_revision": "1" * 40,
            "workspace_root": str(Path("__WORKSPACE__")),
            "data_root": str(Path("__DATA__")),
        },
        "runtime_state": {"clean_stop": True, "stop": dict(STOP),
                          "excluded": []},
        "files": [],
    }


def test_backup_rejects_unregistered_workspace_root_before_container(tmp_path):
    env = _base_env(tmp_path)
    _fake_docker(tmp_path, env)
    _fake_wsl(tmp_path, env)
    data_root, workspace_root = _seed_deployment(tmp_path, env, public=False)
    wrong = tmp_path / "wrong-workspaces"
    wrong.mkdir()
    result = _run_ps(
        BACKUP, "-ProjectName", PROJECT, "-DataRoot", str(data_root),
        "-WorkspaceRoot", str(wrong), "-Image", IMAGE, env=env)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "backup_workspace_root_mismatch" in output
    calls = (tmp_path / "docker-state" / "calls.log")
    assert not calls.exists() or not any(
        '"run"' in line for line in calls.read_text("utf-8").splitlines())
    wsl_calls = tmp_path / "wsl-state" / "calls.log"
    assert not wsl_calls.exists()


def test_start_rejects_incomplete_upgrade_backup_before_compose_up(tmp_path):
    env = _base_env(tmp_path)
    _fake_docker(tmp_path, env)
    _fake_wsl(tmp_path, env)
    data_root, workspace_root = _seed_deployment(tmp_path, env)
    backup = tmp_path / "upgrade-backup"
    backup.mkdir()
    manifest = _manifest()
    manifest["deployment"]["workspace_root"] = str(workspace_root)
    manifest["deployment"]["data_root"] = str(data_root)
    (backup / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    result = _run_ps(
        START, "-PublicOrigin", ORIGIN, "-Image", IMAGE,
        "-WorkerImage", WORKER, "-GatewayImage", GATEWAY,
        "-ProjectName", PROJECT, "-Port", "7861",
        "-DataRoot", str(data_root), "-WorkspaceRoot", str(workspace_root),
        "-UpgradeFromBackup", str(backup), env=env)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "upgrade_backup_verify_failed" in output
    assert not (tmp_path / "wsl-state" / "compose-up").exists()
    calls = (tmp_path / "wsl-state" / "calls.log").read_text("utf-8")
    assert "verify-backup" in calls
    assert not any("compose" in line and "up" in line
                   for line in calls.splitlines())


def _initialize_args(tmp_path: Path):
    data_root = tmp_path / "data"
    workspace_root = tmp_path / "workspaces"
    config = tmp_path / "managed.json"
    policy = tmp_path / "policy.json"
    config.write_text(json.dumps({
        "revision": 1,
        "agent": {"runtime": "acp", "command": ["python", "-c", "pass"],
                  "sandbox": "workspace-write", "timeout": 60},
        "broker": {"mode": "workspace-sidecar", "channel": "chat"},
    }), encoding="utf-8")
    policy.write_text('{"approved_hosts":["a"],"approved_cidrs":[]}',
                      encoding="utf-8")
    return (
        "-Image", IMAGE, "-ManagedAgentConfig", str(config),
        "-EgressPolicy", str(policy), "-ProjectName", PROJECT,
        "-DataRoot", str(data_root), "-WorkspaceRoot", str(workspace_root))


def _seed_control_database(env: dict[str, str]) -> tuple[Path, bytes, bytes]:
    control = Path(env["FAKE_DOCKER_STATE"]) / "volumes" / f"{PROJECT}-control"
    control.mkdir(parents=True, exist_ok=True)
    with Store.open(control / "control.sqlite3"):
        pass
    sentinel = control / "sentinel.txt"
    sentinel.write_text("existing-control", encoding="utf-8")
    return control, (control / "control.sqlite3").read_bytes(), sentinel.read_bytes()


def test_start_rejects_backup_from_previous_stop_before_compose_up(tmp_path):
    env = _base_env(tmp_path)
    _fake_docker(tmp_path, env)
    _fake_wsl(tmp_path, env)
    data_root, workspace_root = _seed_deployment(tmp_path, env)
    old_stop = dict(STOP)
    old_stop["barrierId"] = "old-stop"
    old_stop["completedAt"] = 0
    new_stop = dict(STOP)
    new_stop["barrierId"] = "new-stop"
    new_stop["completedAt"] = 2
    env["FAKE_STOP_STATE"] = json.dumps({
        "admission_barrier": {"id": "new-stop", "project": PROJECT},
        "stop_completion": new_stop,
    })
    backup = tmp_path / "old-upgrade-backup"
    backup.mkdir()
    manifest = _manifest()
    manifest["deployment"]["workspace_root"] = str(workspace_root)
    manifest["deployment"]["data_root"] = str(data_root)
    manifest["runtime_state"]["stop"] = old_stop
    (backup / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    result = _run_ps(
        START, "-PublicOrigin", ORIGIN, "-Image", IMAGE,
        "-WorkerImage", WORKER, "-GatewayImage", GATEWAY,
        "-ProjectName", PROJECT, "-Port", "7861",
        "-DataRoot", str(data_root), "-WorkspaceRoot", str(workspace_root),
        "-UpgradeFromBackup", str(backup), env=env)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "upgrade_stop_commitment_mismatch" in output
    assert not (tmp_path / "wsl-state" / "compose-up").exists()
    calls = (tmp_path / "wsl-state" / "calls.log").read_text("utf-8")
    assert "read_stop_state.py" in calls
    assert "verify-backup" not in calls


def test_initialize_rejects_existing_database_without_master_key(tmp_path):
    env = _base_env(tmp_path)
    _fake_docker(tmp_path, env)
    keys = (Path(env["FAKE_DOCKER_STATE"]) / "volumes" / f"{PROJECT}-keys")
    keys.mkdir(parents=True, exist_ok=True)
    # DB 已存在，但 master.key 丢失；签名私钥仍在。
    subprocess.run(
        [sys.executable, str(KEYGEN), "--ensure",
         "--signing-key", str(keys / "snapshot.pem"),
         "--worker-public", str(keys / "worker-public.key"),
         "--gateway-public", str(keys / "gateway-public.pem")],
        check=True, capture_output=True)
    control, db_before, sentinel_before = _seed_control_database(env)
    result = _run_ps(INITIALIZE, *_initialize_args(tmp_path), env=env)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "existing_identity_incomplete" in output
    assert not (keys / "master.key").exists()
    assert (control / "control.sqlite3").read_bytes() == db_before
    assert (control / "sentinel.txt").read_bytes() == sentinel_before
    calls = (Path(env["FAKE_DOCKER_STATE"]) / "calls.log").read_text("utf-8")
    assert "keygen.py" not in calls


def test_initialize_rejects_existing_database_without_signing_key(tmp_path):
    env = _base_env(tmp_path)
    _fake_docker(tmp_path, env)
    control, db_before, sentinel_before = _seed_control_database(env)
    keys = (Path(env["FAKE_DOCKER_STATE"]) / "volumes" / f"{PROJECT}-keys")
    keys.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, str(KEYGEN), "--ensure",
         "--path", str(keys / "master.key")],
        check=True, capture_output=True)
    master_before = (keys / "master.key").read_bytes()
    result = _run_ps(INITIALIZE, *_initialize_args(tmp_path), env=env)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "existing_identity_incomplete" in output
    assert not (keys / "snapshot.pem").exists()
    assert (keys / "master.key").read_bytes() == master_before
    assert (control / "control.sqlite3").read_bytes() == db_before
    assert (control / "sentinel.txt").read_bytes() == sentinel_before
    calls = (Path(env["FAKE_DOCKER_STATE"]) / "calls.log").read_text("utf-8")
    assert "keygen.py" not in calls


def test_initialize_rejects_existing_database_without_identity_material(tmp_path):
    env = _base_env(tmp_path)
    _fake_docker(tmp_path, env)
    control, db_before, _ = _seed_control_database(env)
    result = _run_ps(INITIALIZE, *_initialize_args(tmp_path), env=env)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "existing_identity_incomplete" in output
    keys = (Path(env["FAKE_DOCKER_STATE"]) / "volumes" / f"{PROJECT}-keys")
    assert not (keys / "master.key").exists()
    assert not (keys / "snapshot.pem").exists()
    assert (control / "control.sqlite3").read_bytes() == db_before
    calls = (Path(env["FAKE_DOCKER_STATE"]) / "calls.log").read_text("utf-8")
    assert "keygen.py" not in calls


def test_initialize_existing_complete_identity_is_idempotent(tmp_path):
    env = _base_env(tmp_path)
    _fake_docker(tmp_path, env)
    _seed_control_database(env)
    keys = (Path(env["FAKE_DOCKER_STATE"]) / "volumes" / f"{PROJECT}-keys")
    keys.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, str(KEYGEN), "--ensure",
         "--path", str(keys / "master.key"),
         "--signing-key", str(keys / "snapshot.pem"),
         "--worker-public", str(keys / "worker-public.key"),
         "--gateway-public", str(keys / "gateway-public.pem")],
        check=True, capture_output=True)
    before = {name: (keys / name).read_bytes() for name in (
        "master.key", "snapshot.pem", "worker-public.key",
        "gateway-public.pem")}
    result = _run_ps(INITIALIZE, *_initialize_args(tmp_path), env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "key=exists" in result.stdout
    assert all((keys / name).read_bytes() == data
               for name, data in before.items())


def test_initialize_fresh_deployment_still_creates_keys(tmp_path):
    env = _base_env(tmp_path)
    _fake_docker(tmp_path, env)
    result = _run_ps(INITIALIZE, *_initialize_args(tmp_path), env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    keys = (Path(env["FAKE_DOCKER_STATE"]) / "volumes" / f"{PROJECT}-keys")
    assert (keys / "master.key").is_file()
    assert (keys / "snapshot.pem").is_file()
    assert "key=created" in result.stdout
