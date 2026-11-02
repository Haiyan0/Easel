"""R1 旧→新正式引导更新验收（隔离项目，受控 TLS provider）。

流程：
1. 用 966d650 源码 + 旧 control 镜像启动全新隔离项目；
2. 受控 TLS fake provider，首屏 6/6 与短聊天通过；
3. 构造现场同构坏状态（DB stopping/gateway-only + 旧版无资源 attempt）；
4. 只用新版 Update-WorkspaceEasel.ps1 更新，验证 control/provisioner
   实际运行源码与能力、worker/gateway/Key/attempt 不被替换；
5. 更新后首屏 6/6、短聊天通过；
6. 正常 Stop → Backup → Verify 可走通。

本脚本不读取用户实例，不使用真实供应商/平台。随机项目/目录/卷，结束清理。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import shutil
import socket
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parents[3]
A4_PATH = ROOT / "tests" / "integration" / "multiuser" / "test_desktop_chat_full.py"
OLD_WORKTREE = pathlib.Path(
    "C:/Code/Easel/.claude/worktrees/acceptance-503-r1-old")
OLD_SOURCE_WSL = "/mnt/c/Code/Easel/.claude/worktrees/acceptance-503-r1-old"
NEW_SOURCE_WSL = "/mnt/c/Code/Easel/.claude/worktrees/acceptance-503-repair"
DISTRO = "Ubuntu-26.04"
ORIGIN = "https://easel.test"
FIRST_SCREEN = (
    "/api/status", "/api/ideas", "/api/schedule",
    "/api/outputs", "/api/accounts", "/api/personas")


def _load_a4():
    spec = importlib.util.spec_from_file_location("r1_a4_helpers", A4_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


a4 = _load_a4()


def _run(command: list[str], timeout: float = 600.0,
         check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(
            "command_failed:{}:{}:{}".format(
                command[0], result.returncode,
                (result.stderr or result.stdout)[-4000:]))
    return result


def _docker(*args: str, timeout: float = 600.0,
            check: bool = True) -> subprocess.CompletedProcess:
    return _run(["docker", *args], timeout=timeout, check=check)


def _wsl(*args: str, timeout: float = 600.0,
         check: bool = True) -> subprocess.CompletedProcess:
    return _run(["wsl.exe", "-d", DISTRO, "--", *args],
                timeout=timeout, check=check)


def _powershell(script: pathlib.Path, *args: str,
                timeout: float = 900.0,
                check: bool = True) -> subprocess.CompletedProcess:
    """用文件承接输出，避免 Start 拉起的 provisioner 继承管道导致挂起。"""
    import tempfile

    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(script), *args]
    temp = pathlib.Path(tempfile.mkdtemp(prefix="r1-ps-"))
    try:
        out_path = temp / "stdout.txt"
        err_path = temp / "stderr.txt"
        with out_path.open("w", encoding="utf-8") as out, \
                err_path.open("w", encoding="utf-8") as err:
            result = subprocess.run(
                command, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                timeout=timeout, check=False)
        stdout = out_path.read_text(encoding="utf-8", errors="replace")
        stderr = err_path.read_text(encoding="utf-8", errors="replace")
    finally:
        # Start 拉起的孙进程可能短暂持有输出文件；清理失败不影响证据。
        shutil.rmtree(temp, ignore_errors=True)
    completed = subprocess.CompletedProcess(
        command, result.returncode, stdout, stderr)
    if check and completed.returncode != 0:
        raise RuntimeError(
            "powershell_failed:{}:{}:{}".format(
                script.name, completed.returncode,
                (stderr or stdout)[-4000:]))
    return completed


def _image_id(tag: str) -> str:
    result = _docker("image", "inspect", "--format", "{{.Id}}", tag)
    image = result.stdout.strip()
    if not image.startswith("sha256:"):
        raise RuntimeError(f"image_missing:{tag}")
    return image


def _wait_until(predicate, timeout: float = 120.0,
                interval: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception:  # noqa: BLE001 - 轮询期间继续等待
            pass
        time.sleep(interval)
    raise RuntimeError("wait_timeout")


def _first_screen(user) -> dict[str, int]:
    def get(path: str):
        response = user.client.get(path, headers=user.headers())
        return path, response.status_code

    with ThreadPoolExecutor(max_workers=len(FIRST_SCREEN)) as pool:
        results = list(pool.map(get, FIRST_SCREEN))
    codes = dict(results)
    if any(code != 200 for code in codes.values()):
        raise RuntimeError(f"first_screen_failed:{json.dumps(codes)}")
    return codes


def _chat(user, title: str, message: str) -> dict:
    session = a4._create_session(user, title)
    submitted = time.time()
    run_id = a4._submit(user, session, message)
    state = a4._wait_run(
        user, session, run_id,
        {"done", "failed", "cancelled", "interrupted"}, timeout=240)
    messages = a4._messages(user, session)
    assistant = [
        str(item.get("content", "")) for item in messages
        if item.get("role") == "assistant"]
    return {
        "session": session,
        "run": run_id,
        "state": state,
        "assistant_has_marker": any("fake-provider-ok" in text
                                    for text in assistant),
        "elapsed": round(time.time() - submitted, 3),
    }


def _provider_requests(audit: pathlib.Path) -> int:
    path = audit / "requests.jsonl"
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines()
               if line.strip())


def _wsl_path(path: pathlib.Path) -> str:
    text = str(path)
    if len(text) < 3 or text[1] != ":":
        raise RuntimeError("path_not_on_windows_drive")
    return "/mnt/" + text[0].lower() + "/" + text[2:].replace("\\", "/").lstrip("/")


def _provisioner_process(project: str) -> dict:
    import tempfile

    socket = f"/tmp/easel-runtime/{project}/provisioner.sock"
    # 用脚本文件而不是 -lc 内联：Windows 命令行会把多行/带空格参数拆开。
    # socket 只通过 argv 传给脚本，辅助 shell 的 cmdline 不包含 provisioner
    # 模式，避免 pgrep -f 把辅助进程自己算进去。
    script = (
        "set -e\n"
        'socket="$1"\n'
        'pattern="deploy/multiuser/provisioner.py --socket $socket"\n'
        'pids=$(pgrep -f -- "$pattern" || true)\n'
        'count=$(printf \'%s\\n\' "$pids" | sed \'/^$/d\' | wc -l)\n'
        'pid=$(printf \'%s\\n\' "$pids" | sed \'/^$/d\' | head -1)\n'
        'cwd=""\n'
        'if [ -n "$pid" ]; then cwd=$(readlink "/proc/$pid/cwd"); fi\n'
        'printf \'pid=%s\\ncwd=%s\\ncount=%s\\n\' "$pid" "$cwd" "$count"\n')
    handle, name = tempfile.mkstemp(prefix="r1-proc-", suffix=".sh")
    path = pathlib.Path(name)
    try:
        with open(handle, "w", encoding="utf-8", newline="\n",
                  closefd=True) as stream:
            stream.write(script)
        output = _wsl("bash", _wsl_path(path), socket).stdout
    finally:
        path.unlink(missing_ok=True)
    values = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def _control_exec(control: str, code: str, *args: str) -> str:
    return _docker("exec", control, "python", "-c", code, *args).stdout


def _lease_state(control: str, workspace: str) -> dict:
    code = (
        "import json,sys\n"
        "from easel.control.store import Store\n"
        "with Store.open('/srv/easel/control/control.sqlite3') as s:\n"
        "    lease=s.get_worker_lease(sys.argv[1])\n"
        "    print(json.dumps({\n"
        "      'state': lease.state,\n"
        "      'owner': lease.owner_id,\n"
        "      'generation': lease.generation,\n"
        "      'worker': lease.container_id,\n"
        "      'gateway': lease.gateway_instance_id,\n"
        "      'gateway_state': lease.gateway_state,\n"
        "      'attempt': lease.attempt_id,\n"
        "    }, sort_keys=True))\n")
    return json.loads(_control_exec(control, code, workspace).strip())


def _construct_field_shape(control: str, workspace: str,
                           attempt_a: str) -> dict:
    """用真实 Store 构造 DB stopping/gateway-only；磁盘写旧版坏记录。"""
    code = (
        "import json,sys\n"
        "from easel.control.store import Store\n"
        "from easel.control.types import WorkerHandle\n"
        "with Store.open('/srv/easel/control/control.sqlite3') as s:\n"
        "    ws=sys.argv[1]; attempt=sys.argv[2]; past=int(sys.argv[3])\n"
        "    lease=s.get_worker_lease(ws)\n"
        "    handle=WorkerHandle(ws, lease.container_id, lease.upstream,\n"
        "        lease.service_token, lease.config_version,\n"
        "        gateway_instance_id=lease.gateway_instance_id,\n"
        "        gateway_generation=lease.gateway_generation,\n"
        "        gateway_upstream=lease.gateway_upstream,\n"
        "        gateway_state='staged')\n"
        "    assert s.begin_worker_gateway_refresh(\n"
        "        ws, lease.generation, 'r1-old-owner', handle, attempt, past, 1)\n"
        "    staged=s.get_worker_lease(ws)\n"
        "    assert s.begin_worker_abort(\n"
        "        ws, 'r1-old-owner', staged.generation, attempt,\n"
        "        'gateway', False, True, past)\n"
        "    final=s.get_worker_lease(ws)\n"
        "    print(json.dumps({\n"
        "      'state': final.state, 'scope': final.cleanup_scope,\n"
        "      'worker': final.container_id,\n"
        "      'gateway': final.gateway_instance_id,\n"
        "      'generation': final.generation,\n"
        "    }, sort_keys=True))\n")
    return json.loads(_control_exec(
        control, code, workspace, attempt_a,
        str(int(time.time()) - 3600)).strip())


def _write_legacy_attempt(workspace_root: pathlib.Path, workspace: str,
                          attempt_b: str, worker: str, gateway: str) -> pathlib.Path:
    path = workspace_root / ".gateway" / f"attempt-{workspace}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "attempt_id": attempt_b,
        "worker_container_id": worker,
        "gateway_instance_id": gateway,
        "worker_created": False,
        "gateway_created": False,
        "scope": "gateway",
        "status": "pending",
    }), encoding="utf-8")
    return path


def _write_managed_config(directory: pathlib.Path, manifest: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "managed-agent.json").write_text(json.dumps({
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
    (directory / "egress-policy.json").write_text(json.dumps(
        {"approved_hosts": ["fake-provider.test"], "approved_cidrs": []}),
        encoding="utf-8")


def _parse_output_line(output: str, prefix: str) -> str:
    for line in output.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return line.split("=", 1)[1] if "=" in line else ""
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true",
                        help="失败/结束后保留临时容器与目录")
    options = parser.parse_args()

    run_id = uuid.uuid4().hex[:8]
    project = f"easel-r1-{run_id}"
    data_root = pathlib.Path(f"C:/Easel/r1-{run_id}-data")
    workspace_root = pathlib.Path(f"C:/Easel/r1-{run_id}-workspaces")
    config_dir = data_root / "config"
    certs = data_root / "certs"
    audit = data_root / "provider-audit"
    backup_dir = data_root / "backups"
    evidence = {}
    provider_name = f"easel-r1-provider-{run_id}"
    control_network = f"{project}-control"
    users = []
    admin = None
    stale_attempt_path = None
    succeeded = False

    old_image = _image_id("easel-control:503-fix")
    new_image = _image_id("easel-control:503-r1")
    worker_image = _image_id("easel-worker:task6a-4382180")
    gateway_image = _image_id("easel-gateway:task6a-4382180")
    runtime_hash = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "python", new_image, "-c",
         "import hashlib;print(hashlib.sha256(open("
         "'/opt/easel/easel/control/runtime.py','rb').read()).hexdigest())"],
        capture_output=True, text=True, check=True).stdout.strip()
    source_runtime = subprocess.run(
        ["wsl.exe", "-d", DISTRO, "--", "sha256sum",
         f"{NEW_SOURCE_WSL}/easel/control/runtime.py"],
        capture_output=True, text=True, check=True).stdout.split()[0]
    if runtime_hash != source_runtime:
        raise RuntimeError("new_image_runtime_mismatch")
    provisioner_hash = subprocess.run(
        ["wsl.exe", "-d", DISTRO, "--", "sha256sum",
         f"{NEW_SOURCE_WSL}/deploy/multiuser/provisioner.py"],
        capture_output=True, text=True, check=True).stdout.split()[0]
    gid = int(_wsl("id", "-g").stdout.strip())
    port = a4._free_port()
    manifest = json.loads(
        (ROOT / "deploy" / "multiuser" / "managed" / "acp-profile.json")
        .read_text(encoding="utf-8"))
    for directory in (data_root, config_dir, certs, audit, backup_dir):
        directory.mkdir(parents=True, exist_ok=True)
    _write_managed_config(config_dir, manifest)
    ca_path, _cert_path, _key_path = a4._write_tls(certs)

    print("R1_IMAGES " + json.dumps({
        "old_control": old_image, "new_control": new_image,
        "worker": worker_image, "gateway": gateway_image,
        "new_runtime_sha256": runtime_hash,
        "new_provisioner_sha256": provisioner_hash,
    }, sort_keys=True))
    print("R1_PROJECT " + json.dumps({
        "project": project, "port": port, "gid": gid,
        "data_root": str(data_root),
        "workspace_root": str(workspace_root),
        "old_source": OLD_SOURCE_WSL, "new_source": NEW_SOURCE_WSL,
    }, sort_keys=True))

    try:
        # 1. 旧版本部署
        print("R1_STAGE init", flush=True)
        init = _powershell(
            OLD_WORKTREE / "deploy/desktop/Initialize-WorkspaceEasel.ps1",
            "-Image", old_image,
            "-ManagedAgentConfig", str(config_dir / "managed-agent.json"),
            "-EgressPolicy", str(config_dir / "egress-policy.json"),
            "-ProjectName", project,
            "-DataRoot", str(data_root),
            "-WorkspaceRoot", str(workspace_root))
        print("R1_INIT_OLD " + init.stdout.strip(), flush=True)
        print("R1_STAGE start_old", flush=True)
        old_start = _powershell(
            OLD_WORKTREE / "deploy/desktop/Start-WorkspaceEasel.ps1",
            "-PublicOrigin", ORIGIN, "-Image", old_image,
            "-WorkerImage", worker_image, "-GatewayImage", gateway_image,
            "-ProjectName", project, "-Port", str(port),
            "-PublishMode", "manual", "-Distro", DISTRO,
            "-WslRepo", OLD_SOURCE_WSL,
            "-WorkspaceRoot", str(workspace_root),
            "-DataRoot", str(data_root),
            "-ProvisionerGid", str(gid),
            "-EgressCaFile", str(certs / "ca.pem"), check=False)
        print("R1_START_OLD " + old_start.stdout.strip())
        if old_start.returncode != 0:
            raise RuntimeError(
                "old_start_failed:" + (old_start.stderr or old_start.stdout)[-3000:])
        evidence["old_start"] = old_start.stdout
        old_control = _docker(
            "ps", "-q", "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter", "label=com.docker.compose.service=control"
        ).stdout.strip()
        if not old_control:
            raise RuntimeError("old_control_missing")

        # 受控 TLS provider（只在本隔离网络内）
        print("R1_STAGE provider", flush=True)
        _docker(
            "run", "-d", "--name", provider_name, "--user", "0:0",
            "--network", control_network,
            "--network-alias", "fake-provider.test",
            "-v", f"{ROOT / 'tests' / 'fixtures'}:/fixture:ro",
            "-v", f"{certs}:/certs:ro", "-v", f"{audit}:/audit",
            new_image, "python", "/fixture/fake_openai_tls.py",
            "--port", "443", "--cert", "/certs/server.pem",
            "--key", "/certs/server.key",
            "--audit", "/audit/requests.jsonl")
        health = (
            "import urllib.request;"
            "urllib.request.urlopen('https://fake-provider.test/v1/models',"
            "timeout=3).read()")
        _wait_until(
            lambda: _docker("exec", old_control, "python", "-c", health,
                            check=False).returncode == 0,
            timeout=60)

        bootstrap = _docker(
            "exec", old_control, "python", "-m", "easel", "admin",
            "bootstrap", "--control-dir", "/srv/easel/control",
            "--name", "R1Admin")
        invite = _parse_output_line(bootstrap.stdout, "invite=")
        if not invite:
            raise RuntimeError("bootstrap_invite_missing")
        admin = a4._exchange_invite(port, invite)
        users.append(admin)
        member = a4._create_member(admin, "R1User")
        user = a4._exchange_invite(
            port, a4._create_invite(admin, member["userId"]))
        users.append(user)
        a4._publish_chat(admin)
        workspace = user.workspace_id
        workspace_dir = workspace_root / workspace / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        (workspace_dir / "workspace-marker.txt").write_text(
            workspace, encoding="ascii")

        print("R1_STAGE first_screen_before", flush=True)
        before_screen = _first_screen(user)
        print("R1_STAGE chat_before", flush=True)
        before_chat = _chat(user, "R1 before", "say fake-provider-ok")
        before_lease = _lease_state(old_control, workspace)
        before_requests = _provider_requests(audit)
        print("R1_BEFORE " + json.dumps({
            "first_screen": before_screen, "chat": before_chat,
            "lease": before_lease,
            "provider_requests": before_requests,
        }, sort_keys=True))
        if not before_chat["assistant_has_marker"]:
            raise RuntimeError("before_chat_marker_missing")
        evidence["before"] = {
            "first_screen": before_screen, "chat": before_chat,
            "lease": before_lease}

        # 1b. 复现旧版正式 Start 在身份配置一致时复用旧进程：
        # 只把 WslRepo 指向新源码，不重启旧 provisioner。
        process_before = _provisioner_process(project)
        reuse = _powershell(
            OLD_WORKTREE / "deploy/desktop/Start-WorkspaceEasel.ps1",
            "-PublicOrigin", ORIGIN, "-Image", old_image,
            "-WorkerImage", worker_image, "-GatewayImage", gateway_image,
            "-ProjectName", project, "-Port", str(port),
            "-PublishMode", "manual", "-Distro", DISTRO,
            "-WslRepo", NEW_SOURCE_WSL,
            "-WorkspaceRoot", str(workspace_root),
            "-DataRoot", str(data_root),
            "-ProvisionerGid", str(gid), check=False)
        process_after = _provisioner_process(project)
        print("R1_REUSE_REPRO " + json.dumps({
            "returncode": reuse.returncode,
            "pid_before": process_before.get("pid"),
            "pid_after": process_after.get("pid"),
            "cwd_after": process_after.get("cwd"),
            "stdout": reuse.stdout.strip(),
        }, sort_keys=True))
        if reuse.returncode != 0:
            raise RuntimeError(
                "reuse_repro_start_failed:" +
                (reuse.stderr or reuse.stdout)[-2000:])
        if process_after.get("pid") != process_before.get("pid"):
            raise RuntimeError("reuse_repro_pid_changed")
        # WSL 的 /proc/<pid>/cwd 可能省略 /mnt/c 前缀；两种等价写法都接受。
        cwd_after = process_after.get("cwd", "")
        old_wsl_suffix = OLD_SOURCE_WSL.removeprefix("/mnt/c")
        if cwd_after not in (OLD_SOURCE_WSL, old_wsl_suffix):
            raise RuntimeError(
                f"reuse_repro_old_source_not_running:{cwd_after}")
        evidence["reuse_repro"] = {
            "pid": process_after.get("pid"),
            "cwd": process_after.get("cwd"),
            "stdout": reuse.stdout}

        # 2. 构造现场同构坏状态（DB + 磁盘 attempt）
        print("R1_STAGE field_shape", flush=True)
        attempt_a = uuid.uuid4().hex
        attempt_b = uuid.uuid4().hex
        field_shape = _construct_field_shape(
            old_control, workspace, attempt_a)
        stale_attempt_path = _write_legacy_attempt(
            workspace_root, workspace, attempt_b,
            str(field_shape["worker"]), str(field_shape["gateway"]))
        print("R1_FIELD_SHAPE " + json.dumps({
            "db": field_shape, "attempt_file": str(stale_attempt_path),
            "attempt_a": attempt_a, "attempt_b": attempt_b,
        }, sort_keys=True))
        evidence["field_shape"] = field_shape

        # 3. 只运行新版正式更新流程
        print("R1_STAGE update", flush=True)
        update = _powershell(
            ROOT / "deploy/desktop/Update-WorkspaceEasel.ps1",
            "-PublicOrigin", ORIGIN, "-Image", new_image,
            "-WorkerImage", worker_image, "-GatewayImage", gateway_image,
            "-ProjectName", project, "-Port", str(port),
            "-Distro", DISTRO, "-WslRepo", NEW_SOURCE_WSL,
            "-WorkspaceRoot", str(workspace_root),
            "-DataRoot", str(data_root),
            "-ProvisionerGid", str(gid), check=False)
        print("R1_UPDATE_BEGIN")
        print(update.stdout.strip())
        print("R1_UPDATE_END code=" + str(update.returncode))
        evidence["update"] = update.stdout
        if update.returncode != 0:
            raise RuntimeError(
                "update_failed:" + (update.stderr or update.stdout)[-3000:])
        new_control = _docker(
            "ps", "-q", "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter", "label=com.docker.compose.service=control"
        ).stdout.strip()
        if not new_control:
            raise RuntimeError("new_control_missing")
        new_image_now = _docker(
            "inspect", "--format", "{{.Image}}", new_control).stdout.strip()
        if new_image_now != new_image:
            raise RuntimeError("new_control_image_mismatch")
        image_runtime = _docker(
            "exec", new_control, "python", "-c",
            "import hashlib;print(hashlib.sha256(open("
            "'/opt/easel/easel/control/runtime.py','rb').read()).hexdigest())"
        ).stdout.strip()
        if image_runtime != runtime_hash:
            raise RuntimeError("new_control_runtime_mismatch")
        barrier_status = _docker(
            "exec", new_control, "python", "-m", "easel", "admin",
            "barrier-status", "--control-dir", "/srv/easel/control")
        if barrier_status.stdout.strip() != "barrier=none":
            raise RuntimeError("barrier_not_resumed")
        identity_show = _wsl(
            "docker", "run", "--rm", "--network", "none", "--read-only",
            "--user", "10001:10001", "--group-add", str(gid),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--entrypoint", "python",
            "--mount",
            f"type=bind,src=/tmp/easel-runtime/{project}/provisioner.sock,"
            "dst=/run/provisioner.sock,readonly",
            new_image, "/opt/easel/deploy/desktop/provisioner_identity.py",
            "show", "--socket", "/run/provisioner.sock")
        identity = json.loads(identity_show.stdout.strip())
        if identity.get("sourceSha256") != provisioner_hash:
            raise RuntimeError("new_provisioner_source_mismatch")
        if identity.get("protocol") != 2:
            raise RuntimeError("new_provisioner_protocol_mismatch")
        for action in ("restore_gateway", "commit_attempt", "recover_abort"):
            if action not in identity.get("actions", []):
                raise RuntimeError(f"new_provisioner_action_missing:{action}")
        print("R1_POST_UPDATE " + json.dumps({
            "control_image": new_image_now,
            "control_runtime_sha256": image_runtime,
            "provisioner_source_sha256": identity["sourceSha256"],
            "provisioner_protocol": identity["protocol"],
            "provisioner_actions": identity["actions"],
            "barrier": barrier_status.stdout.strip(),
        }, sort_keys=True))
        evidence["post_update"] = {
            "control_image": new_image_now,
            "control_runtime_sha256": image_runtime,
            "provisioner_source_sha256": identity["sourceSha256"],
            "provisioner_protocol": identity["protocol"],
            "provisioner_actions": identity["actions"]}
        update_summary = data_root / "logs" / "update-last.json"
        if update_summary.exists():
            evidence["update_summary"] = json.loads(
                update_summary.read_text(encoding="utf-8"))

        # 4. 新恢复路径收口 + 首屏 + 短聊天
        print("R1_STAGE first_screen_after", flush=True)
        after_screen = _first_screen(user)
        print("R1_STAGE chat_after", flush=True)
        after_chat = _chat(user, "R1 after", "say fake-provider-ok again")
        after_lease = _lease_state(new_control, workspace)
        attempts_after = {
            "attempt_file_status": json.loads(
                stale_attempt_path.read_text(encoding="utf-8"))["status"],
            "attempt_file_id": json.loads(
                stale_attempt_path.read_text(encoding="utf-8"))["attempt_id"],
        }
        after_requests = _provider_requests(audit)
        print("R1_AFTER " + json.dumps({
            "first_screen": after_screen, "chat": after_chat,
            "lease": after_lease, "attempt": attempts_after,
            "provider_requests": after_requests,
        }, sort_keys=True))
        if not after_chat["assistant_has_marker"]:
            raise RuntimeError("after_chat_marker_missing")
        if after_requests <= before_requests:
            raise RuntimeError("provider_requests_did_not_increase")
        evidence["after"] = {
            "first_screen": after_screen, "chat": after_chat,
            "lease": after_lease, "attempt": attempts_after,
            "provider_requests": after_requests}

        # 5. 正常 Stop → Backup → Verify
        print("R1_STAGE stop", flush=True)
        _docker("rm", "-f", provider_name, check=False)
        stop = _powershell(
            ROOT / "deploy/desktop/Stop-WorkspaceEasel.ps1",
            "-ProjectName", project, "-Distro", DISTRO,
            "-WslRepo", NEW_SOURCE_WSL, "-DataRoot", str(data_root))
        print("R1_STOP " + stop.stdout.strip())
        evidence["stop"] = stop.stdout
        if stop.returncode != 0:
            raise RuntimeError("stop_failed:" + stop.stderr[-2000:])
        print("R1_STAGE backup", flush=True)
        backup = _powershell(
            ROOT / "deploy/desktop/Backup-WorkspaceEasel.ps1",
            "-ProjectName", project, "-Distro", DISTRO,
            "-WslRepo", NEW_SOURCE_WSL, "-DataRoot", str(data_root),
            "-WorkspaceRoot", str(workspace_root), "-Image", new_image)
        print("R1_BACKUP " + backup.stdout.strip())
        evidence["backup"] = backup.stdout
        if backup.returncode != 0:
            raise RuntimeError("backup_failed:" + backup.stderr[-2000:])
        backup_path = _parse_output_line(backup.stdout, "backup=")
        if not backup_path:
            raise RuntimeError("backup_path_missing")
        print("R1_STAGE verify", flush=True)
        verify = _powershell(
            ROOT / "deploy/desktop/Verify-WorkspaceEasel.ps1",
            "-Backup", backup_path, "-Image", new_image,
            "-Distro", DISTRO)
        print("R1_VERIFY " + verify.stdout.strip())
        evidence["verify"] = verify.stdout
        if verify.returncode != 0:
            raise RuntimeError("verify_failed:" + verify.stderr[-2000:])

        evidence["result"] = "R1_UPDATE_E2E_PASS"
        succeeded = True
        evidence_path = (ROOT / "docs" / "evidence"
                         / "acceptance-503-repair-r1" / "e2e-result.json")
        evidence_path.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8")
        print("R1_EVIDENCE=" + str(evidence_path))
        print("R1_UPDATE_E2E_PASS")
        return 0
    finally:
        for user in users:
            try:
                user.client.close()
            except Exception:  # noqa: BLE001
                pass
        if options.keep or not succeeded:
            print("R1_KEEP " + json.dumps({
                "project": project, "data_root": str(data_root),
                "workspace_root": str(workspace_root),
                "provider": provider_name,
                "succeeded": succeeded}, sort_keys=True))
        else:
            _docker("rm", "-f", provider_name, check=False)
            for volume in (f"{project}-control", f"{project}-keys",
                           f"{project}-egress"):
                _docker("volume", "rm", volume, check=False)
            _docker("network", "rm", control_network, check=False)
            shutil.rmtree(data_root, ignore_errors=True)
            shutil.rmtree(workspace_root, ignore_errors=True)
            print("R1_CLEANUP done")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # noqa: BLE001 - 证据脚本失败即退出 1
        import traceback
        traceback.print_exc()
        print("R1_FAILED " + repr(error), file=sys.stderr)
        sys.exit(1)
