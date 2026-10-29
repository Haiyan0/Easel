"""A7：真实 Desktop 备份、校验、新目录恢复、升级与回滚演练。

仅显式设置 ``EASEL_RUN_CONTAINER_TESTS=1`` 后运行。使用随机项目/卷/网络/
临时目录，复用正式 PowerShell 启停/备份/恢复入口；上游为受控 TLS fake
provider，worker 为真实 A6/A7 固定 Harness 镜像。真实供应商仍单独 not-run。
"""
from __future__ import annotations

import hashlib
import importlib.util
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

ROOT = pathlib.Path(__file__).resolve().parents[3]
DEPLOY = ROOT / "deploy" / "desktop"
WSL_DISTRO = "Ubuntu-26.04"
CONTROL_IMAGE = os.environ.get(
    "EASEL_A7_CONTROL_IMAGE",
    "sha256:9fc4e037f82ad16a6d0553ee208cf0b85731ed23d57532da132f628474422009")
WORKER_OLD = os.environ.get(
    "EASEL_A7_WORKER_OLD",
    "sha256:984bfac19412c0c6251e93568508fc9c078970727e4a9b515d5cb455a82128e9")
WORKER_NEW = os.environ.get(
    "EASEL_A7_WORKER_NEW",
    "sha256:e48eb63b6155b6fc14c37a2525d138e7419f7a8024f46a58d209c40d3a5bd993")
GATEWAY_IMAGE = os.environ.get(
    "EASEL_A7_GATEWAY_IMAGE",
    "sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a")

pytestmark = pytest.mark.skipif(
    os.environ.get("EASEL_RUN_CONTAINER_TESTS") != "1",
    reason="设置 EASEL_RUN_CONTAINER_TESTS=1 才运行 A7 真实运维演练")


def _load_a4():
    path = pathlib.Path(__file__).with_name("test_desktop_chat_full.py")
    spec = importlib.util.spec_from_file_location("a7_a4", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


a4 = _load_a4()


def _free_port(exclude: set[int] | None = None) -> int:
    # 避开 Windows/Hyper-V 排除段与 Docker 代理已占用端口；只选 7800–7899。
    exclude = exclude or set()
    for port in range(7800, 7900):
        if port in exclude:
            continue
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.fail("7800–7899 没有可用端口")


def _wait_host(port: int, timeout: float = 60) -> None:
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
    pytest.fail(f"宿主端口 {port} 健康检查失败：{last}")


def _run(command: list[str], log: pathlib.Path, *, timeout: float = 600) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as stream:
        result = subprocess.run(
            command, stdout=stream, stderr=subprocess.STDOUT,
            timeout=timeout, check=False)
    if result.returncode != 0:
        raw = log.read_bytes()
        text = raw.decode("utf-8", "replace")
        if "�" in text:
            text = raw.decode("gbk", "replace")
        pytest.fail(f"命令失败（{result.returncode}）：{' '.join(command)}\n{text[-4000:]}")


def _script(name: str, *args: str, log: pathlib.Path, timeout: float = 600) -> None:
    _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
          "-File", str(DEPLOY / name), *args], log, timeout=timeout)


def _docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False)
    if result.returncode != 0:
        pytest.fail(f"docker {' '.join(args)} 失败：{result.stderr[-2000:]}")
    return result.stdout.strip()


def _wait_provider(project: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    code = ("import urllib.request;"
            "urllib.request.urlopen('https://fake-provider.test/v1/models',"
            "timeout=3).read()")
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "exec", f"{project}-control-1", "python", "-c", code],
            capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return
        time.sleep(1)
    pytest.fail("fake provider 不可达，检查 egress CA 与网络")


def _start_provider(project: str, network: str, root: pathlib.Path) -> None:
    audit = root / "audit"
    audit.mkdir(exist_ok=True)
    _docker(
        "run", "-d", "--name", f"{project}-provider",
        "--label", f"easel.a7.test={project}", "--user", "0:0",
        "--network", network, "--network-alias", "fake-provider.test",
        "-v", f"{ROOT / 'tests/fixtures'}:/fixture:ro",
        "-v", f"{root / 'certs'}:/certs:ro",
        "-v", f"{audit}:/audit",
        CONTROL_IMAGE, "python", "/fixture/fake_openai_tls.py",
        "--port", "443", "--cert", "/certs/server.pem",
        "--key", "/certs/server.key", "--audit", "/audit/requests.jsonl")
    _wait_provider(project)


def _write_configs(root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    (root / "config").mkdir(parents=True)
    (root / "certs").mkdir(parents=True)
    (root / "workspaces").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    profile = json.loads(
        (ROOT / "deploy/multiuser/managed/acp-profile.json").read_text(
            encoding="utf-8"))
    managed = root / "config/managed-agent.json"
    managed.write_text(json.dumps({
        "revision": 1,
        "agent": {
            "runtime": "acp",
            "command": profile["command"],
            "sandbox": "workspace-write",
            "timeout": 300,
        },
        "broker": profile["broker"],
    }), encoding="utf-8")
    policy = root / "config/egress-policy.json"
    policy.write_text(json.dumps({
        "approved_hosts": ["fake-provider.test"],
        "approved_cidrs": [],
    }), encoding="utf-8")
    a4._write_tls(root / "certs")
    return managed, policy


def _bootstrap(project: str, port: int, origin: str, prefix: str):
    boot = _docker(
        "exec", f"{project}-control-1", "python", "-m", "easel", "admin",
        "bootstrap", "--control-dir", "/srv/easel/control",
        "--name", f"{prefix} Admin")
    invite = next(line.split("=", 1)[1] for line in boot.splitlines()
                  if line.startswith("invite="))
    a4.ORIGIN = origin
    admin = a4._exchange_invite(port, invite)
    member_a = a4._create_member(admin, f"{prefix} Alice")
    member_b = a4._create_member(admin, f"{prefix} Bob")
    invite_a = a4._create_invite(admin, member_a["userId"])
    invite_b = a4._create_invite(admin, member_b["userId"])
    user_a = a4._exchange_invite(port, invite_a)
    user_b = a4._exchange_invite(port, invite_b)
    a4._publish_chat(admin)
    return admin, member_a, member_b, user_a, user_b


def _chat(user, session: str, marker: str) -> dict:
    run = a4._submit(user, session, marker)
    a4._wait_run(user, session, run,
                 {"done", "failed", "interrupted", "cancelled"}, timeout=240)
    messages = a4._messages(user, session)
    return {"run": run, "state": a4._runs(user, session).get(run),
            "messages": messages,
            "text": json.dumps(messages, ensure_ascii=False)}


def _cleanup(project: str, root: pathlib.Path) -> None:
    subprocess.run(["docker", "rm", "-f", f"{project}-provider"],
                   capture_output=True, check=False)
    containers = _docker(
        "ps", "-aq", "--filter",
        f"label=com.docker.compose.project={project}")
    for container in containers.splitlines():
        if container.strip():
            subprocess.run(["docker", "rm", "-f", container.strip()],
                           capture_output=True, check=False)
    for volume in _docker("volume", "ls", "--format", "{{.Name}}").splitlines():
        if volume.startswith(f"{project}-"):
            subprocess.run(["docker", "volume", "rm", "-f", volume],
                           capture_output=True, check=False)
    for network in _docker("network", "ls", "--format", "{{.Name}}").splitlines():
        if network.startswith(f"{project}-"):
            subprocess.run(["docker", "network", "rm", network],
                           capture_output=True, check=False)
    subprocess.run(
        ["wsl.exe", "-d", WSL_DISTRO, "--", "bash", "-lc",
         f"rm -rf '/tmp/easel-runtime/{project}'"],
        capture_output=True, check=False)
    shutil.rmtree(root, ignore_errors=True)


def test_real_backup_restore_upgrade_and_rollback(tmp_path):
    run_id = uuid.uuid4().hex[:8]
    source = f"easel-a7-live-{run_id}"
    restored = f"easel-a7-restore-{run_id}"
    source_root = tmp_path / "source"
    restore_root = tmp_path / "restore"
    logs = tmp_path / "logs"
    source_port = _free_port()
    restore_port = _free_port({source_port})
    source_origin = f"https://a7-live-{run_id}.example"
    restore_origin = f"https://a7-restore-{run_id}.example"
    managed, policy = _write_configs(source_root)
    (restore_root / "data").mkdir(parents=True)
    (restore_root / "workspaces").mkdir(parents=True)
    try:
        _script("Initialize-WorkspaceEasel.ps1",
                "-Image", CONTROL_IMAGE,
                "-ManagedAgentConfig", str(managed),
                "-EgressPolicy", str(policy),
                "-ProjectName", source,
                "-DataRoot", str(source_root / "data"),
                "-WorkspaceRoot", str(source_root / "workspaces"),
                log=logs / "initialize.log")
        _script("Start-WorkspaceEasel.ps1",
                "-PublicOrigin", source_origin,
                "-Image", CONTROL_IMAGE,
                "-WorkerImage", WORKER_OLD,
                "-GatewayImage", GATEWAY_IMAGE,
                "-ProjectName", source,
                "-Port", str(source_port),
                "-DataRoot", str(source_root / "data"),
                "-WorkspaceRoot", str(source_root / "workspaces"),
                "-EgressCaFile", str(source_root / "certs/ca.pem"),
                log=logs / "start-source.log")
        _wait_host(source_port)
        _start_provider(source, f"{source}-control", source_root)
        admin, member_a, member_b, user_a, user_b = _bootstrap(
            source, source_port, source_origin, "Live")
        session_a = a4._create_session(user_a, "A7 live A")
        session_b = a4._create_session(user_b, "A7 live B")
        chat_a = _chat(user_a, session_a, "A7-LIVE-A")
        chat_b = _chat(user_b, session_b, "A7-LIVE-B")
        assert chat_a["state"] == "done" and chat_b["state"] == "done"
        sentinel = (source_root / "workspaces" / user_a.workspace_id
                    / "workspace" / "sentinel-a7.txt")
        sentinel.write_text("a7-live-sentinel", encoding="utf-8")

        _script("Stop-WorkspaceEasel.ps1",
                "-ProjectName", source,
                "-DataRoot", str(source_root / "data"),
                log=logs / "stop-source.log")
        _docker("rm", "-f", f"{source}-provider")
        _script("Backup-WorkspaceEasel.ps1",
                "-ProjectName", source,
                "-DataRoot", str(source_root / "data"),
                "-WorkspaceRoot", str(source_root / "workspaces"),
                "-Image", CONTROL_IMAGE,
                log=logs / "backup-source.log")
        backup = sorted((source_root / "data/backups").glob("desktop-*"))[-1]
        manifest_sha = hashlib.sha256(
            (backup / "manifest.json").read_bytes()).hexdigest()
        _script("Verify-WorkspaceEasel.ps1",
                "-Backup", str(backup), "-Image", CONTROL_IMAGE,
                log=logs / "verify-source.log")

        _script("Restore-WorkspaceEasel.ps1",
                "-Backup", str(backup),
                "-ProjectName", restored,
                "-PublicOrigin", restore_origin,
                "-Image", CONTROL_IMAGE,
                "-WorkerImage", WORKER_OLD,
                "-GatewayImage", GATEWAY_IMAGE,
                "-Port", str(restore_port),
                "-DataRoot", str(restore_root / "data"),
                "-WorkspaceRoot", str(restore_root / "workspaces"),
                log=logs / "restore.log")
        _script("Start-WorkspaceEasel.ps1",
                "-PublicOrigin", restore_origin,
                "-Image", CONTROL_IMAGE,
                "-WorkerImage", WORKER_OLD,
                "-GatewayImage", GATEWAY_IMAGE,
                "-ProjectName", restored,
                "-Port", str(restore_port),
                "-DataRoot", str(restore_root / "data"),
                "-WorkspaceRoot", str(restore_root / "workspaces"),
                log=logs / "start-restored.log")
        _wait_host(restore_port)
        _start_provider(restored, f"{restored}-control", source_root)

        a4.ORIGIN = restore_origin
        old = a4._client(restore_port)
        old_cookie = old.get(
            f"/api/sessions/{session_a}/messages",
            headers={"Cookie": user_a.cookie, "Origin": restore_origin})
        assert old_cookie.status_code == 401
        recovery_invite = (
            restore_root / "data/recovery/admin-invite.txt").read_text(
                encoding="utf-8").strip()
        restored_admin = a4._exchange_invite(restore_port, recovery_invite)
        invite_a = a4._create_invite(restored_admin, user_a.user_id)
        invite_b = a4._create_invite(restored_admin, user_b.user_id)
        restored_a = a4._exchange_invite(restore_port, invite_a)
        restored_b = a4._exchange_invite(restore_port, invite_b)
        evil = restored_a.client.post(
            "/api/chat",
            headers={"Cookie": restored_a.cookie,
                     "Origin": "https://evil.example",
                     "X-CSRF-Token": restored_a.csrf},
            json={"sessionId": session_a, "turnId": "evil", "message": "x"})
        assert evil.status_code == 403
        after_a = _chat(restored_a, session_a, "A7-RESTORE-A")
        after_b = _chat(restored_b, session_b, "A7-RESTORE-B")
        assert "A7-LIVE-A" in after_a["text"]
        assert "A7-LIVE-B" in after_b["text"]
        assert "A7-LIVE-B" not in after_a["text"]
        assert "fake-provider-ok" in after_a["text"]
        assert (restore_root / "workspaces" / user_a.workspace_id
                / "workspace" / "sentinel-a7.txt").read_text(
                    encoding="utf-8") == "a7-live-sentinel"
        assert hashlib.sha256(
            (backup / "manifest.json").read_bytes()).hexdigest() == manifest_sha
        _script("Stop-WorkspaceEasel.ps1",
                "-ProjectName", restored,
                "-DataRoot", str(restore_root / "data"),
                log=logs / "stop-restored.log")
        _docker("rm", "-f", f"{restored}-provider")

        # 受控升级：坏镜像失败 -> 匹配备份 -> 新 worker -> 历史/新消息。
        bad = "sha256:" + "f" * 64
        with pytest.raises(BaseException):
            _script("Start-WorkspaceEasel.ps1",
                    "-PublicOrigin", source_origin,
                    "-Image", CONTROL_IMAGE,
                    "-WorkerImage", bad,
                    "-GatewayImage", GATEWAY_IMAGE,
                    "-ProjectName", source,
                    "-Port", str(source_port),
                    "-DataRoot", str(source_root / "data"),
                    "-WorkspaceRoot", str(source_root / "workspaces"),
                    "-UpgradeFromBackup", str(backup),
                    log=logs / "upgrade-bad.log")
        bad_log = (logs / "upgrade-bad.log").read_bytes().decode(
            "utf-8", "replace")
        assert "worker_or_gateway_image_mismatch" in bad_log
        _script("Start-WorkspaceEasel.ps1",
                "-PublicOrigin", source_origin,
                "-Image", CONTROL_IMAGE,
                "-WorkerImage", WORKER_NEW,
                "-GatewayImage", GATEWAY_IMAGE,
                "-ProjectName", source,
                "-Port", str(source_port),
                "-DataRoot", str(source_root / "data"),
                "-WorkspaceRoot", str(source_root / "workspaces"),
                "-UpgradeFromBackup", str(backup),
                log=logs / "upgrade-good.log")
        _wait_host(source_port)
        _start_provider(source, f"{source}-control", source_root)
        a4.ORIGIN = source_origin
        invite = _docker(
            "exec", f"{source}-control-1", "python", "-m", "easel", "admin",
            "invite", "--control-dir", "/srv/easel/control",
            "--admin-user-id", admin.user_id, "--user-id", user_a.user_id)
        invite_token = next(line.split("=", 1)[1] for line in invite.splitlines()
                            if line.startswith("invite="))
        upgraded_a = a4._exchange_invite(source_port, invite_token)
        upgraded = _chat(upgraded_a, session_a, "A7-UPGRADED-A")
        assert "A7-LIVE-A" in upgraded["text"]
        assert "fake-provider-ok" in upgraded["text"]
        assert upgraded["state"] == "done"
        _script("Stop-WorkspaceEasel.ps1",
                "-ProjectName", source,
                "-DataRoot", str(source_root / "data"),
                log=logs / "stop-source-final.log")
    finally:
        _cleanup(source, source_root)
        _cleanup(restored, restore_root)
