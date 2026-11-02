"""验收 503 R2：更新脚本的活动工作边界、部分完成恢复与实例快照范围。

用临时 Docker/WSL 桩执行真实 Update 控制流，不连接 Docker daemon，
不读取开发者卷/凭证。断言关注副作用（是否进入 Start/compose）与阶段
错误码，而不是只匹配脚本文本。
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

from tests.multiuser import test_desktop_operations_r1 as r1

ROOT = pathlib.Path(__file__).resolve().parents[2]
UPDATE = ROOT / "deploy" / "desktop" / "Update-WorkspaceEasel.ps1"
CONTROL_BUILD = (ROOT / "docs" / "evidence" /
                 "acceptance-503-repair-r1" / "control-build.txt")
OLD_IMAGE = "sha256:" + "f" * 64
BARRIER_ID = "update-abcdef123456"

pytestmark = pytest.mark.skipif(
    r1.POWERSHELL is None or os.name != "nt",
    reason="Windows PowerShell 不可用")


def _custom_exec_handler() -> str:
    return r'''
if args[0] == "exec":
    joined = " ".join(args)
    barrier_path = state / "barrier.txt"
    barrier_id = (barrier_path.read_text().strip()
                  if barrier_path.exists() else "")
    if "set_admission_barrier" in joined:
        barrier_id = "update-abcdef123456"
        barrier_path.write_text(barrier_id)
        out(json.dumps({"created": {"id": barrier_id,
                                     "reason": "desktop-update",
                                     "setAt": 1}}))
        raise SystemExit(0)
    if "drain_status" in joined or "update-status" in joined:
        out(json.dumps({
            "barrier": ({"id": barrier_id, "reason": "desktop-update",
                         "setAt": 1} if barrier_id else None),
            "activeRuns": int(os.environ.get("FAKE_ACTIVE_RUNS", "0")),
            "activeRequests": int(os.environ.get("FAKE_ACTIVE_REQUESTS", "0")),
            "activeLeaseCount": int(os.environ.get("FAKE_ACTIVE_LEASES", "0")),
            "workspaceIds": [os.environ.get("FAKE_WORKSPACE", "a" * 32)],
        }))
        raise SystemExit(0)
    if "barrier-status" in joined:
        out("barrier=" + barrier_id if barrier_id else "barrier=none")
        raise SystemExit(0)
    if "admin resume" in joined:
        barrier_path.unlink(missing_ok=True)
        raise SystemExit(0)
    if "hashlib.sha256" in joined:
        out("a" * 64)
        raise SystemExit(0)
'''


def _docker_source() -> str:
    source = r1._FAKE_DOCKER
    marker = 'if args[0] == "exec":'
    assert marker in source
    source = source.replace(
        marker, _custom_exec_handler() + "\n" + marker, 1)
    old_ps = (
        'if args[0] == "ps":\n'
        '    out("fake-control-id")\n'
        '    raise SystemExit(0)')
    new_ps = (
        'if args[0] == "ps":\n'
        '    workspace_filter = [a for a in args\n'
        '                        if a.startswith("label=easel.workspace-id=")]\n'
        '    if workspace_filter:\n'
        '        workspace_id = workspace_filter[0].split("=", 1)[1]\n'
        '        counter = state / "snapshot-counter.txt"\n'
        '        number = (int(counter.read_text()) if counter.exists()\n'
        '                  else 0) + 1\n'
        '        counter.write_text(str(number))\n'
        '        other = os.environ.get("FAKE_OTHER_WORKSPACE", "")\n'
        '        if (workspace_id == other\n'
        '                or os.environ.get("FAKE_MANAGED_DRIFT") == "1"):\n'
        '            out(format(number, "064x"))\n'
        '        else:\n'
        '            out("b" * 64)\n'
        '    else:\n'
        '        out("fake-control-id")\n'
        '    raise SystemExit(0)')
    assert old_ps in source
    source = source.replace(old_ps, new_ps, 1)
    old_inspect = (
        'if args[0] == "inspect":\n'
        '    if "--format" in args:\n'
        '        out("healthy")\n'
        '    else:\n'
        '        out(json.dumps([{"Id": "fake-control-id"}]))\n'
        '    raise SystemExit(0)')
    new_inspect = (
        'if args[0] == "inspect":\n'
        '    if "--format" in args:\n'
        '        template = args[args.index("--format") + 1]\n'
        '        if template == "{{.Image}}":\n'
        '            out(os.environ.get("FAKE_CONTROL_IMAGE", image))\n'
        '        elif template == "{{.State.Health.Status}}":\n'
        '            out("healthy")\n'
        '        elif "{{.Id}}|{{.State.Running}}" in template:\n'
        '            for item in args[args.index("--format") + 2:]:\n'
        '                out(item + "|true")\n'
        '        else:\n'
        '            out("healthy")\n'
        '    else:\n'
        '        out(json.dumps([{"Id": "fake-control-id"}]))\n'
        '    raise SystemExit(0)')
    assert old_inspect in source
    return source.replace(old_inspect, new_inspect, 1)


def _wsl_source() -> str:
    source = r1._FAKE_WSL
    old = (
        '        if (joined.Contains("provisioner_identity.py")) {\n'
        '            return 0;\n'
        '        }')
    new = (
        '        if (joined.Contains("provisioner_identity.py")) {\n'
        '            if (Environment.GetEnvironmentVariable('
        '"FAKE_WSL_IDENTITY_FAIL") == "1") return 1;\n'
        '            Console.WriteLine("{\\"sourceSha256\\":\\"" +'
        ' new string(\'a\', 64) +'
        ' "\\",\\"protocol\\":2,\\"actions\\":[\\"ensure\\",\\"stop\\",'
        '\\"inspect\\",\\"activate\\",\\"abort\\",\\"restore_gateway\\",'
        '\\"commit_attempt\\",\\"recover_abort\\",\\"identity\\"]}");\n'
        '            return 0;\n'
        '        }\n'
        '        if (joined.Contains("provisioner-pid.sh")) {\n'
        '            if (Environment.GetEnvironmentVariable('
        '"FAKE_WSL_PID_FAIL") == "1") return 1;\n'
        '            return 0;\n'
        '        }')
    assert old in source
    return source.replace(old, new, 1)


_FAKE_DOCKER_LAUNCHER = r"""
using System;
using System.Diagnostics;
using System.Text;

class FakeDocker {
    static int Main(string[] args) {
        var script = Environment.GetEnvironmentVariable("FAKE_DOCKER_SCRIPT");
        var python = Environment.GetEnvironmentVariable("FAKE_DOCKER_PYTHON");
        var psi = new ProcessStartInfo(python);
        psi.UseShellExecute = false;
        var builder = new StringBuilder();
        builder.Append("\"").Append(script).Append("\"");
        foreach (var arg in args) {
            builder.Append(" \"")
                   .Append(arg.Replace("\\", "\\\\").Replace("\"", "\\\""))
                   .Append("\"");
        }
        psi.Arguments = builder.ToString();
        var child = Process.Start(psi);
        child.WaitForExit();
        return child.ExitCode;
    }
}
"""


def _fake_docker(tmp_path: pathlib.Path, env: dict[str, str]) -> None:
    r1._fake_docker(tmp_path, env)
    (tmp_path / "fake_docker.py").write_text(
        _docker_source(), encoding="utf-8")
    # .cmd 会把 --format 中的 | 当管道；用 C# 可执行文件原样转发参数。
    if not r1.CSC.is_file():
        pytest.skip("csc.exe 不可用")
    source = tmp_path / "FakeDocker.cs"
    source.write_text(_FAKE_DOCKER_LAUNCHER, encoding="utf-8")
    exe = tmp_path / "bin" / "docker.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    compiled = subprocess.run(
        [str(r1.CSC), "/nologo", "/target:exe", f"/out:{exe}",
         str(source)], capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    env["FAKE_DOCKER_SCRIPT"] = str(tmp_path / "fake_docker.py")
    env["FAKE_DOCKER_PYTHON"] = sys.executable
    env["PATH"] = str(exe.parent) + os.pathsep + env.get("PATH", "")


def _fake_wsl(tmp_path: pathlib.Path, env: dict[str, str]) -> None:
    if not r1.CSC.is_file():
        pytest.skip("csc.exe 不可用")
    source = tmp_path / "FakeWslR2.cs"
    source.write_text(_wsl_source(), encoding="utf-8")
    exe = tmp_path / "bin" / "wsl.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    compiled = subprocess.run(
        [str(r1.CSC), "/nologo", "/target:exe", f"/out:{exe}",
         str(source)], capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    env["PATH"] = str(exe.parent) + os.pathsep + env.get("PATH", "")


def _prepare(tmp_path: pathlib.Path, *, control_image: str,
             deployment_image: str, barrier: bool) -> tuple[pathlib.Path,
                                                            pathlib.Path,
                                                            dict[str, str]]:
    env = r1._base_env(tmp_path)
    env["FAKE_CONTROL_IMAGE"] = control_image
    data_root, workspace_root = r1._seed_deployment(tmp_path, env)
    deployment_file = data_root / "logs" / "deployment.json"
    record = json.loads(deployment_file.read_text(encoding="utf-8"))
    record["control_image"] = deployment_image
    deployment_file.write_text(json.dumps(record), encoding="utf-8")
    (data_root / "logs" / "provisioner-expected.json").write_text(
        json.dumps({"test": True}), encoding="utf-8")
    if barrier:
        (pathlib.Path(env["FAKE_DOCKER_STATE"]) / "barrier.txt").write_text(
            BARRIER_ID, encoding="utf-8")
    _fake_docker(tmp_path, env)
    _fake_wsl(tmp_path, env)
    return data_root, workspace_root, env


def _args(data_root: pathlib.Path, workspace_root: pathlib.Path,
          timeout: int = 1) -> tuple[str, ...]:
    return (
        "-PublicOrigin", r1.ORIGIN, "-Image", r1.IMAGE,
        "-WorkerImage", r1.WORKER, "-GatewayImage", r1.GATEWAY,
        "-ProjectName", r1.PROJECT, "-Port", "7861",
        "-Distro", "Ubuntu-26.04", "-WslRepo", "/mnt/c/Code/Easel",
        "-WorkspaceRoot", str(workspace_root), "-DataRoot", str(data_root),
        "-ProvisionerGid", "1000", "-TimeoutSeconds", str(timeout),
        "-LockTimeoutSeconds", "10")


def _wsl_calls(tmp_path: pathlib.Path) -> str:
    path = tmp_path / "wsl-state" / "calls.log"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def test_update_waits_for_active_run_and_does_not_start(tmp_path):
    """屏障建立后仍有活动 Run：有界等待失败，绝不进入 Start/compose。"""
    data_root, workspace_root, env = _prepare(
        tmp_path, control_image=r1.IMAGE, deployment_image=r1.IMAGE,
        barrier=False)
    env["FAKE_ACTIVE_RUNS"] = "1"
    env["FAKE_ACTIVE_REQUESTS"] = "0"
    result = r1._run_ps(
        UPDATE, *_args(data_root, workspace_root, timeout=1), env=env)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "update_active_work_timeout:1/0" in output
    assert "update_active_work=1/0" in output
    assert "update_barrier_retained=update-" in output
    assert not (tmp_path / "wsl-state" / "compose-up").exists()
    calls = _wsl_calls(tmp_path)
    assert not any("compose" in line and " up" in line
                   for line in calls.splitlines())
    assert "update_start_exit" not in output


def test_update_resumes_replaced_control_without_old_identity(tmp_path):
    """目标 control 已替换、deployment 尚旧：同一屏障下允许重跑。"""
    data_root, workspace_root, env = _prepare(
        tmp_path, control_image=r1.IMAGE, deployment_image=OLD_IMAGE,
        barrier=True)
    env["FAKE_ACTIVE_RUNS"] = "0"
    env["FAKE_ACTIVE_REQUESTS"] = "0"
    result = r1._run_ps(
        UPDATE, *_args(data_root, workspace_root, timeout=1), env=env)
    output = result.stdout + result.stderr
    assert f"update_resumed_phase={BARRIER_ID}" in output
    assert "update_active_work=0/0" in output
    assert "update_control_image_mismatch" not in output
    assert "update_old_identity_unavailable" not in output
    assert "update_control_unavailable" not in output
    assert "update_active_work_timeout" not in output
    assert "update_start_exit=0" in output
    assert result.returncode == 0, output
    assert "update_completed" in output


def test_update_failed_post_verification_can_rerun_same_command(tmp_path):
    """Start 后核验失败保留屏障；修复后同一条 Update 命令可完成。"""
    data_root, workspace_root, env = _prepare(
        tmp_path, control_image=r1.IMAGE, deployment_image=r1.IMAGE,
        barrier=False)
    env["FAKE_ACTIVE_RUNS"] = "0"
    env["FAKE_ACTIVE_REQUESTS"] = "0"
    env["FAKE_MANAGED_DRIFT"] = "1"
    first = r1._run_ps(
        UPDATE, *_args(data_root, workspace_root, timeout=2), env=env)
    first_output = first.stdout + first.stderr
    assert first.returncode != 0
    assert "update_managed_instances_changed" in first_output
    assert "update_barrier_retained=update-" in first_output
    assert "update_start_exit=0" in first_output

    env["FAKE_MANAGED_DRIFT"] = "0"
    second = r1._run_ps(
        UPDATE, *_args(data_root, workspace_root, timeout=2), env=env)
    second_output = second.stdout + second.stderr
    assert "update_resumed_phase=update-" in second_output
    assert second.returncode == 0, second_output
    assert "update_completed" in second_output


def test_update_resume_allows_stopped_old_provisioner(tmp_path):
    """旧 provisioner 已停、新进程未起：同一更新屏障下重跑不得卡旧身份。"""
    data_root, workspace_root, env = _prepare(
        tmp_path, control_image=r1.IMAGE, deployment_image=OLD_IMAGE,
        barrier=True)
    env["FAKE_WSL_IDENTITY_FAIL"] = "1"
    env["FAKE_WSL_PID_FAIL"] = "1"
    env["FAKE_ACTIVE_RUNS"] = "0"
    env["FAKE_ACTIVE_REQUESTS"] = "0"
    result = r1._run_ps(
        UPDATE, *_args(data_root, workspace_root, timeout=1), env=env)
    output = result.stdout + result.stderr
    assert f"update_resumed_phase={BARRIER_ID}" in output
    assert "update_old_identity_unavailable" not in output
    assert "update_old_provisioner_pid_unavailable" not in output
    assert "update_active_work_timeout" not in output
    assert "update_start_exit" in output


def test_update_ignores_other_project_instance_changes(tmp_path):
    """另一个部署增删实例不影响本项目快照；只查询本项目 workspace。"""
    project_workspace = "c" * 32
    other_workspace = "d" * 32
    data_root, workspace_root, env = _prepare(
        tmp_path, control_image=r1.IMAGE, deployment_image=r1.IMAGE,
        barrier=False)
    env["FAKE_ACTIVE_RUNS"] = "0"
    env["FAKE_ACTIVE_REQUESTS"] = "0"
    env["FAKE_WORKSPACE"] = project_workspace
    env["FAKE_OTHER_WORKSPACE"] = other_workspace
    result = r1._run_ps(
        UPDATE, *_args(data_root, workspace_root, timeout=2), env=env)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    calls = (pathlib.Path(env["FAKE_DOCKER_STATE"]) /
             "calls.log").read_text(encoding="utf-8").splitlines()
    snapshot_calls = []
    for line in calls:
        args = json.loads(line)
        if (args[:1] == ["ps"] and
                any("label=easel.workspace-id=" in item for item in args)):
            snapshot_calls.append(" ".join(args))
    assert snapshot_calls
    for call in snapshot_calls:
        assert f"label=easel.workspace-id={project_workspace}" in call
        assert other_workspace not in call


def test_update_snapshot_is_scoped_to_project_workspaces():
    text = UPDATE.read_text(encoding="utf-8-sig")
    assert "-WorkspaceIds" in text
    assert 'label=easel.workspace-id=$workspaceId' in text
    assert "Get-ManagedSnapshot -Root $WorkspaceRoot" in text
    assert 'label=easel.managed=true' in text
    # 不允许再单独用全局 managed 过滤，导致其他部署的增删影响本项目。
    assert " '--filter', 'label=easel.managed=true')" not in text


def test_update_uses_bounded_work_wait_before_start():
    text = UPDATE.read_text(encoding="utf-8-sig")
    wait = text.index("Wait-UpdateWorkload -ControlId")
    start = text.index("-FilePath 'powershell.exe'")
    assert wait < start
    assert "update_active_work_timeout" in text
    assert "$runs = [int]$facts.Payload.activeRuns" in text
    assert "$requests = [int]$facts.Payload.activeRequests" in text
    assert "$runs -eq 0 -and $requests -eq 0" in text
    # 静态坏 lease 只报告，不参与等待条件。
    wait_block = text[text.index("function Wait-UpdateWorkload"):
                      text.index("$lifecycleLock = $null")]
    assert "activeLeaseCount" not in wait_block


def test_update_accepts_only_barrier_protected_partial_states():
    text = UPDATE.read_text(encoding="utf-8-sig")
    assert "Get-ControlFacts" in text
    assert "update_resume_control_image" in text
    assert "$controlImageNow -notin @($oldControlImage, $Image)" in text
    assert "update_control_image_unexpected" in text
    # 旧身份/PID 只在非恢复阶段要求。
    resume_index = text.index("if ($resuming) {")
    identity_index = text.index("$oldIdentity = Invoke-ProvisionerIdentity")
    assert resume_index < identity_index


def test_control_build_evidence_has_no_trailing_whitespace():
    text = CONTROL_BUILD.read_text(encoding="utf-8")
    offenders = [number for number, line in enumerate(text.splitlines(), 1)
                 if line != line.rstrip()]
    assert offenders == []
