"""Task 14：正式发布门槛检查器的失败关闭行为。

检查器是本机可导入的纯脚本；这些测试只替换命令执行器，不连接 Docker、
不访问真实控制面、不读取开发者数据。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_multiuser_release.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_multiuser_release", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Result(SimpleNamespace):
    def __init__(self, returncode: int = 0, stdout: str = "",
                 stderr: str = "") -> None:
        super().__init__(returncode=returncode, stdout=stdout, stderr=stderr)


def _which(name: str) -> str | None:
    return f"/usr/bin/{name}"


def _formal_env(tmp_path: Path) -> dict[str, str]:
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    key = tmp_path / "master.key"
    key.write_bytes(b"k" * 32)
    try:
        os.chmod(key, 0o600)
    except OSError:
        pass
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "manifest.json").write_text("{}", encoding="utf-8")
    return {
        "EASEL_TEST_CONTROL_URL": "https://control.example.test",
        "EASEL_TEST_INVITE_A": "invite-secret-A",
        "EASEL_TEST_INVITE_B": "invite-secret-B",
        "EASEL_TEST_WORKSPACE_ROOT": str(workspace),
        "EASEL_MASTER_KEY_FILE": str(key),
        "EASEL_TEST_WORKER_IMAGE": "sha256:" + "a" * 64,
        "EASEL_TEST_BACKUP_SOURCE": str(backup),
        "EASEL_TEST_RESTORE_DEST": str(tmp_path / "restore"),
        "EASEL_TEST_PREVIEW_URL": "https://control.example.test/preview.html",
    }


def _successful_command(argv, **kwargs):
    binary = Path(str(argv[0])).name if argv else ""
    joined = " ".join(str(part) for part in argv)
    if binary == "docker" and "SecurityOptions" in joined:
        return Result(0, '["name=seccomp,profile=builtin"]')
    if binary == "findmnt":
        return Result(0, "xfs rw,relatime,prjquota")
    if binary == "docker":
        return Result(0, "29.2.1")
    return Result(0, "3 passed in 1.00s")


def test_windows_host_fails_and_never_runs_release_gates(tmp_path, capsys):
    module = _load_module()
    commands: list[list[str]] = []

    def record(argv, **kwargs):
        commands.append(list(argv))
        return _successful_command(argv, **kwargs)

    code = module.main(
        [], env=_formal_env(tmp_path), platform_name="win32",
        which=_which, run_command=record)
    output = capsys.readouterr().out
    assert code == 1
    assert "platform_not_linux" in output
    assert not any("pytest" in command for command in commands)


def test_missing_live_environment_is_an_explicit_failure(tmp_path):
    module = _load_module()
    env = _formal_env(tmp_path)
    env.pop("EASEL_TEST_CONTROL_URL")
    env.pop("EASEL_TEST_INVITE_A")
    checks = module.check_prerequisites(
        env, platform_name="linux", which=_which,
        run_command=_successful_command)
    codes = {check.code for check in checks if check.status == "fail"}
    assert "missing_env:EASEL_TEST_CONTROL_URL" in codes
    assert "missing_env:EASEL_TEST_INVITE_A" in codes
    assert not module.all_checks_passed(checks)


def test_quota_bypass_override_is_rejected(tmp_path):
    module = _load_module()
    env = _formal_env(tmp_path)
    env["EASEL_ALLOW_UNQUOTAED"] = "1"
    checks = module.check_prerequisites(
        env, platform_name="linux", which=_which,
        run_command=_successful_command)
    assert "unsafe_override" in {
        check.code for check in checks if check.status == "fail"}


def test_rootless_docker_is_not_silently_accepted(tmp_path):
    module = _load_module()
    env = _formal_env(tmp_path)

    def rootless(argv, **kwargs):
        binary = Path(str(argv[0])).name if argv else ""
        if binary == "docker" and "SecurityOptions" in " ".join(argv):
            return Result(0, '["name=rootless"]')
        return _successful_command(argv, **kwargs)

    checks = module.check_prerequisites(
        env, platform_name="linux", which=_which,
        run_command=rootless)
    assert "rootless_unverified" in {
        check.code for check in checks if check.status == "fail"}


def test_non_xfs_or_missing_project_quota_is_failure(tmp_path):
    module = _load_module()
    env = _formal_env(tmp_path)

    def ext4(argv, **kwargs):
        binary = Path(str(argv[0])).name if argv else ""
        if binary == "findmnt":
            return Result(0, "ext4 rw,relatime")
        return _successful_command(argv, **kwargs)

    checks = module.check_prerequisites(
        env, platform_name="linux", which=_which, run_command=ext4)
    assert "workspace_not_xfs" in {
        check.code for check in checks if check.status == "fail"}


@pytest.mark.parametrize("summary,code", [
    ("2 passed, 1 skipped in 2.00s", "gate_skipped"),
    ("2 passed, 1 xfailed in 2.00s", "gate_xfailed"),
    ("2 passed, 1 xpassed in 2.00s", "gate_xpassed"),
])
def test_skip_or_xfail_is_a_failed_release_gate(summary, code):
    module = _load_module()
    result = module.classify_gate_result("live", 0, summary)
    assert result.status == "fail"
    assert result.code == code


def test_empty_gate_is_a_failure():
    module = _load_module()
    result = module.classify_gate_result(
        "live", 0, "no tests ran in 0.01s")
    assert result.status == "fail"
    assert result.code == "gate_empty"


def test_unmarked_tests_are_a_failure():
    module = _load_module()
    result = module.classify_gate_result(
        "live", 0, "1 passed, 2 deselected in 0.20s",
        require_marker_coverage=True)
    assert result.status == "fail"
    assert result.code == "gate_unmarked"


def test_formal_checks_pass_then_gates_run_without_leaking_secrets(
        tmp_path, capsys):
    module = _load_module()
    env = _formal_env(tmp_path)
    calls: list[tuple[list[str], dict]] = []

    def record(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return _successful_command(argv, **kwargs)

    code = module.main(
        [], env=env, platform_name="linux", which=_which,
        run_command=record)
    output = capsys.readouterr().out
    assert code == 0
    assert "invite-secret-A" not in output
    assert "invite-secret-B" not in output
    assert "release_status=passed" in output
    gate_commands = [command for command, _kwargs in calls
                     if "pytest" in command]
    assert gate_commands
    # 真实容器门槛必须显式启用，不能依赖测试自身 skip；固定镜像摘要
    # 也必须传入拓扑/容器测试，而不是让测试自行使用可变 tag。
    assert all((kwargs.get("env") or {}).get(
        "EASEL_RUN_CONTAINER_TESTS") == "1"
        for _command, kwargs in calls if "pytest" in _command)
    assert all((kwargs.get("env") or {}).get("EASEL_WORKER_IMAGE")
               == env["EASEL_TEST_WORKER_IMAGE"]
               for _command, kwargs in calls if "pytest" in _command)
