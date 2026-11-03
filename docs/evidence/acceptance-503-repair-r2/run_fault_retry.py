"""R2-04 真实隔离故障重试：旧 provisioner 已停、新进程未起。

复用 run_e2e.py 的隔离初始化与受控 TLS provider；额外在更新前写入
desktop-update 屏障并只按精确 socket/PID 停止旧 provisioner，模拟首次
Update 已完成屏障建立和进程替换、但新进程未启动的失败阶段。随后运行
同一条 Update 命令，验证恢复阶段不再要求旧 identity/PID 并能完成更新。
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[3]
E2E_PATH = pathlib.Path(__file__).resolve().parent / "run_e2e.py"
spec = importlib.util.spec_from_file_location("r2_e2e", E2E_PATH)
e2e = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = e2e
spec.loader.exec_module(e2e)

DISTRO = e2e.DISTRO
ORIGIN = e2e.ORIGIN
NEW_SOURCE_WSL = e2e.NEW_SOURCE_WSL
OLD_WORKTREE = e2e.OLD_WORKTREE
OLD_SOURCE_WSL = e2e.OLD_SOURCE_WSL


def main() -> int:
    run_id = uuid.uuid4().hex[:8]
    project = f"easel-r2f-{run_id}"
    data_root = pathlib.Path(f"C:/Easel/r2f-{run_id}-data")
    workspace_root = pathlib.Path(f"C:/Easel/r2f-{run_id}-workspaces")
    config_dir = data_root / "config"
    certs = data_root / "certs"
    audit = data_root / "provider-audit"
    provider_name = f"easel-r2f-provider-{run_id}"
    control_network = f"{project}-control"
    barrier_id = "update-abcdef123456"
    users = []
    admin = None
    succeeded = False
    cleanup_workspaces: list[str] = []

    old_image = e2e._image_id("easel-control:4382180862e2")
    new_image = e2e._image_id("easel-control:503-r2-final")
    worker_image = e2e._image_id("easel-worker:4382180862e2")
    gateway_image = e2e._image_id("easel-gateway:4382180862e2")
    gid = int(e2e._wsl("id", "-g").stdout.strip())
    port = e2e.a4._free_port()
    manifest = json.loads(
        (ROOT / "deploy" / "multiuser" / "managed" / "acp-profile.json")
        .read_text(encoding="utf-8"))
    for directory in (data_root, config_dir, certs, audit):
        directory.mkdir(parents=True, exist_ok=True)
    e2e._write_managed_config(config_dir, manifest)
    ca_path, _cert_path, _key_path = e2e.a4._write_tls(certs)

    print("R2F_IMAGES " + json.dumps({
        "old_control": old_image, "new_control": new_image,
        "barrier": barrier_id}, sort_keys=True))
    print("R2F_PROJECT " + json.dumps({
        "project": project, "port": port,
        "data_root": str(data_root),
        "workspace_root": str(workspace_root)}, sort_keys=True))
    try:
        e2e._powershell(
            OLD_WORKTREE / "deploy/desktop/Initialize-WorkspaceEasel.ps1",
            "-Image", old_image,
            "-ManagedAgentConfig", str(config_dir / "managed-agent.json"),
            "-EgressPolicy", str(config_dir / "egress-policy.json"),
            "-ProjectName", project,
            "-DataRoot", str(data_root),
            "-WorkspaceRoot", str(workspace_root))
        old_start = e2e._powershell(
            OLD_WORKTREE / "deploy/desktop/Start-WorkspaceEasel.ps1",
            "-PublicOrigin", ORIGIN, "-Image", old_image,
            "-WorkerImage", worker_image, "-GatewayImage", gateway_image,
            "-ProjectName", project, "-Port", str(port),
            "-PublishMode", "manual", "-Distro", DISTRO,
            "-WslRepo", OLD_SOURCE_WSL,
            "-WorkspaceRoot", str(workspace_root),
            "-DataRoot", str(data_root),
            "-ProvisionerGid", str(gid),
            "-EgressCaFile", str(certs / "ca.pem"))
        print("R2F_START_OLD " + old_start.stdout.strip())
        old_control = e2e._docker(
            "ps", "-q", "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter", "label=com.docker.compose.service=control"
        ).stdout.strip()
        if not old_control:
            raise RuntimeError("old_control_missing")

        e2e._docker(
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
        e2e._wait_until(
            lambda: e2e._docker(
                "exec", old_control, "python", "-c", health,
                check=False).returncode == 0,
            timeout=60)

        bootstrap = e2e._docker(
            "exec", old_control, "python", "-m", "easel", "admin",
            "bootstrap", "--control-dir", "/srv/easel/control",
            "--name", "R2FAdmin")
        invite = e2e._parse_output_line(bootstrap.stdout, "invite=")
        admin = e2e.a4._exchange_invite(port, invite)
        users.append(admin)
        cleanup_workspaces.append(admin.workspace_id)
        member = e2e.a4._create_member(admin, "R2FUser")
        user = e2e.a4._exchange_invite(
            port, e2e.a4._create_invite(admin, member["userId"]))
        users.append(user)
        e2e.a4._publish_chat(admin)
        workspace = user.workspace_id
        cleanup_workspaces.append(workspace)
        e2e._first_screen(user)
        before_chat = e2e._chat(user, "R2F before", "say fake-provider-ok")

        # 模拟首次 Update 已建立屏障后的失败阶段。
        code = (
            "from easel.control.store import Store\n"
            "import sys\n"
            "with Store.open('/srv/easel/control/control.sqlite3') as s:\n"
            "    print(s.set_admission_barrier(sys.argv[1],"
            " 'desktop-update', int(sys.argv[2])))\n")
        barrier = e2e._control_exec(
            old_control, code, barrier_id, str(int(time.time())))
        print("R2F_BARRIER " + barrier.strip())
        # 真实首次 Update 会在屏障后写 managed 基线；故障夹具补同一文件，
        # 否则 Important 2 的“缺失首次基线失败关闭”会正确拒绝重跑。
        workspaces_code = (
            "import json\n"
            "from easel.control.store import Store\n"
            "with Store.open('/srv/easel/control/control.sqlite3') as s:\n"
            "    print(json.dumps(s.list_workspace_ids()))\n")
        workspaces = json.loads(e2e._control_exec(
            old_control, workspaces_code).strip())
        container_ids: list[str] = []
        for workspace in workspaces:
            found = e2e._docker(
                "ps", "-a", "-q",
                "--filter", "label=easel.managed=true",
                "--filter", f"label=easel.workspace-id={workspace}")
            container_ids.extend(
                item.strip() for item in found.stdout.split()
                if item.strip())
        baseline: list[str] = []
        if container_ids:
            inspected = e2e._docker(
                "inspect", "--format", "{{.Id}}|{{.State.Running}}",
                *sorted(set(container_ids)))
            baseline = sorted(
                line.strip() for line in inspected.stdout.splitlines()
                if line.strip())
        baseline_file = (data_root / "logs" /
                         f"update-baseline-{barrier_id}.json")
        baseline_file.write_text(
            json.dumps(baseline), encoding="utf-8")
        print("R2F_BASELINE " + json.dumps(
            {"count": len(baseline), "file": str(baseline_file)},
            sort_keys=True))
        process = e2e._provisioner_process(project)
        pid = process.get("pid", "")
        if not pid:
            raise RuntimeError("old_provisioner_pid_missing")
        socket = f"/tmp/easel-runtime/{project}/provisioner.sock"
        # 复杂 shell 必须走脚本文件；Windows 命令行会拆分多行/带空格参数。
        kill_script = (
            "set -e\n"
            f"kill -TERM {pid} 2>/dev/null || true\n"
            "for i in $(seq 1 30); do\n"
            f"  kill -0 {pid} 2>/dev/null || break\n"
            "  sleep 0.2\n"
            "done\n"
            f"if kill -0 {pid} 2>/dev/null; then exit 1; fi\n"
            f"rm -f {socket}\n")
        handle, name = tempfile.mkstemp(prefix="r2f-kill-", suffix=".sh")
        script_path = pathlib.Path(name)
        try:
            with open(handle, "w", encoding="utf-8", newline="\n",
                      closefd=True) as stream:
                stream.write(kill_script)
            e2e._wsl("bash", e2e._wsl_path(script_path))
        finally:
            script_path.unlink(missing_ok=True)
        gone = e2e._provisioner_process(project)
        print("R2F_PROVISIONER_STOPPED " + json.dumps({
            "pid_before": pid, "after": gone}, sort_keys=True))
        if gone.get("pid"):
            raise RuntimeError("old_provisioner_still_running")

        update = e2e._powershell(
            ROOT / "deploy/desktop/Update-WorkspaceEasel.ps1",
            "-PublicOrigin", ORIGIN, "-Image", new_image,
            "-WorkerImage", worker_image, "-GatewayImage", gateway_image,
            "-ProjectName", project, "-Port", str(port),
            "-Distro", DISTRO, "-WslRepo", NEW_SOURCE_WSL,
            "-WorkspaceRoot", str(workspace_root),
            "-DataRoot", str(data_root),
            "-ProvisionerGid", str(gid), check=False)
        print("R2F_UPDATE_BEGIN")
        print(update.stdout.strip())
        print("R2F_UPDATE_END code=" + str(update.returncode))
        if update.returncode != 0:
            raise RuntimeError(
                "update_failed:" + (update.stderr or update.stdout)[-3000:])
        if f"update_resumed_phase={barrier_id}" not in update.stdout:
            raise RuntimeError("resume_phase_not_entered")
        if "update_active_work=0/0" not in update.stdout:
            raise RuntimeError("unexpected_active_work")
        new_process = e2e._provisioner_process(project)
        if not new_process.get("pid") or new_process.get("pid") == pid:
            raise RuntimeError("new_provisioner_not_started")
        new_control = e2e._docker(
            "ps", "-q", "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter", "label=com.docker.compose.service=control"
        ).stdout.strip()
        after_screen = e2e._first_screen(user)
        after_chat = e2e._chat(user, "R2F after", "say fake-provider-ok again")
        print("R2F_AFTER " + json.dumps({
            "first_screen": after_screen, "chat": after_chat,
            "provisioner": new_process,
            "control": new_control,
            "before_chat_ok": before_chat["assistant_has_marker"],
        }, sort_keys=True))
        if not after_chat["assistant_has_marker"]:
            raise RuntimeError("after_chat_marker_missing")
        print("R2F_FAULT_RETRY_PASS")

        stop = e2e._powershell(
            ROOT / "deploy/desktop/Stop-WorkspaceEasel.ps1",
            "-ProjectName", project, "-Distro", DISTRO,
            "-WslRepo", NEW_SOURCE_WSL, "-DataRoot", str(data_root),
            check=False)
        print("R2F_STOP " + stop.stdout.strip())
        succeeded = True
        return 0
    finally:
        for user in users:
            try:
                user.client.close()
            except Exception:  # noqa: BLE001
                pass
        e2e._docker("rm", "-f", provider_name, check=False)
        if not succeeded:
            print("R2F_KEEP " + json.dumps({
                "project": project, "data_root": str(data_root),
                "workspace_root": str(workspace_root)}, sort_keys=True))
        else:
            # 只清理本轮随机项目：精确实例 ID + 本项目卷/网络/目录。
            for workspace in cleanup_workspaces:
                ids = e2e._docker(
                    "ps", "-a", "-q", "--filter",
                    f"label=easel.workspace-id={workspace}",
                    check=False).stdout.split()
                for item in ids:
                    e2e._docker("rm", "-f", item, check=False)
            for volume in (f"{project}-control", f"{project}-keys",
                           f"{project}-egress"):
                e2e._docker("volume", "rm", volume, check=False)
            e2e._docker("network", "rm", control_network, check=False)
            shutil.rmtree(data_root, ignore_errors=True)
            shutil.rmtree(workspace_root, ignore_errors=True)
            print("R2F_CLEANUP done")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("R2F_FAILED " + repr(error), file=sys.stderr)
        sys.exit(1)
