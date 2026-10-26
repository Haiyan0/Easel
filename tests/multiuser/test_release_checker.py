"""Task 14：正式发布门槛检查器的失败关闭行为。

检查器是本机可导入的纯脚本；这些测试只替换命令执行器，不连接 Docker、
不访问真实控制面、不读取开发者数据。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
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
        "EASEL_TEST_EGRESS_PROXY": "http://127.0.0.1:8899",
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


def _run_real_pytest(module, tmp_path: Path, body: str, capsys):
    test_file = tmp_path / "test_real_gate.py"
    test_file.write_text(body, encoding="utf-8")
    result = module.execute_gate(
        "real-gate",
        [sys.executable, "-m", "pytest", str(test_file), "-q", "-rsxX",
         "-p", "no:cacheprovider"],
        cwd=tmp_path, env=os.environ.copy(), run_command=subprocess.run)
    captured = capsys.readouterr()
    return result, captured


def test_execute_gate_captures_real_pytest_success_and_failure(
        tmp_path, capsys):
    module = _load_module()
    passed, passed_console = _run_real_pytest(
        module, tmp_path,
        "def test_ok():\n    assert True\n", capsys)
    assert passed.status == "pass"
    assert "1 passed" not in passed_console.out
    assert "1 passed" not in passed_console.err

    failed, failed_console = _run_real_pytest(
        module, tmp_path,
        "def test_bad():\n"
        "    assert False, 'RAW_OUTPUT_SENTINEL'\n", capsys)
    assert failed.status == "fail"
    assert failed.code == "gate_failed"
    assert "RAW_OUTPUT_SENTINEL" not in (
        failed_console.out + failed_console.err)

    skipped, skipped_console = _run_real_pytest(
        module, tmp_path,
        "import pytest\n"
        "def test_skip():\n"
        "    pytest.skip('SKIP_RAW_SENTINEL')\n", capsys)
    assert skipped.status == "fail"
    assert skipped.code == "gate_skipped"
    assert "SKIP_RAW_SENTINEL" not in (
        skipped_console.out + skipped_console.err)

    broken, broken_console = _run_real_pytest(
        module, tmp_path,
        "raise RuntimeError('EXC_RAW_SENTINEL')\n", capsys)
    assert broken.status == "fail"
    assert broken.code == "gate_failed"
    assert "EXC_RAW_SENTINEL" not in (
        broken_console.out + broken_console.err)


def test_run_release_gates_requests_captured_text_output(tmp_path):
    module = _load_module()
    env = _formal_env(tmp_path)
    calls: list[dict] = []

    def record(argv, **kwargs):
        calls.append(dict(kwargs))
        return Result(0, "1 passed in 0.10s")

    module.run_release_gates(env, run_command=record)
    assert calls
    assert all(kwargs.get("capture_output") is True for kwargs in calls)
    assert all(kwargs.get("text") is True for kwargs in calls)
    assert all(kwargs.get("encoding") == "utf-8" for kwargs in calls)
    assert all(kwargs.get("errors") == "replace" for kwargs in calls)


def test_all_mandatory_gates_reject_mixed_skip_xfail(tmp_path):
    module = _load_module()
    env = _formal_env(tmp_path)

    def mixed(argv, **kwargs):
        return Result(0, "1 passed, 1 skipped, 1 xfailed in 0.10s")

    results = module.run_release_gates(env, run_command=mixed)
    assert results
    assert all(result.status == "fail" for result in results)
    assert all(result.code in ("gate_skipped", "gate_xfailed", "gate_xpassed")
               for result in results)


def test_all_mandatory_gates_reject_empty_selection(tmp_path):
    module = _load_module()
    env = _formal_env(tmp_path)

    def empty(argv, **kwargs):
        return Result(0, "no tests ran in 0.01s")

    results = module.run_release_gates(env, run_command=empty)
    assert results
    assert all(result.status == "fail" for result in results)
    assert all(result.code == "gate_empty" for result in results)


def test_live_gate_rejects_deselected_marker_tests(tmp_path):
    module = _load_module()
    env = _formal_env(tmp_path)

    def deselected(argv, **kwargs):
        return Result(0, "1 passed, 2 deselected in 0.10s")

    results = module.run_release_gates(env, run_command=deselected)
    live = next(result for result in results
                if result.name == "live_two_users")
    assert live.status == "fail"
    assert live.code == "gate_unmarked"


def test_missing_egress_proxy_is_an_explicit_failure(tmp_path):
    module = _load_module()
    env = _formal_env(tmp_path)
    env.pop("EASEL_TEST_EGRESS_PROXY")
    checks = module.check_prerequisites(
        env, platform_name="linux", which=_which,
        run_command=_successful_command)
    codes = {check.code for check in checks if check.status == "fail"}
    assert "missing_env:EASEL_TEST_EGRESS_PROXY" in codes


def test_all_green_gates_still_blocked_by_unverified_evidence(
        tmp_path, capsys):
    module = _load_module()
    env = _formal_env(tmp_path)
    calls: list[tuple[list[str], dict]] = []

    def record(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return _successful_command(argv, **kwargs)

    code = module.main(
        ["--json"], env=env, platform_name="linux", which=_which,
        run_command=record)
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["release_status"] == "blocked"
    assert payload["worker_image"] == env["EASEL_TEST_WORKER_IMAGE"]
    evidence = {item["name"]: item for item in payload["evidence"]}
    assert evidence["egress_proxy"]["status"] == "verified"
    assert evidence["xfs_quota_enforcement"]["status"] == "blocked"
    assert evidence["host_firewall_ipv6_matrix"]["status"] == "blocked"
    assert (evidence["final_runtime_image_and_real_dsh"]["status"]
            == "blocked")
    assert "invite-secret-A" not in json.dumps(payload)
    assert "invite-secret-B" not in json.dumps(payload)

    gate_commands = [command for command, _kwargs in calls
                     if "pytest" in command]
    assert gate_commands
    assert any(any("test_egress.py" in part for part in command)
               for command in gate_commands)
    egress_calls = [kwargs for command, kwargs in calls
                    if any("test_egress.py" in part for part in command)]
    assert egress_calls
    assert all((kwargs.get("env") or {}).get("EASEL_EGRESS_LIVE") == "1"
               for kwargs in egress_calls)
    assert all((kwargs.get("env") or {}).get("EASEL_TEST_EGRESS_PROXY")
               == env["EASEL_TEST_EGRESS_PROXY"]
               for kwargs in egress_calls)
    # 必选门槛全部显式启用；固定镜像摘要也传给拓扑测试。
    assert all((kwargs.get("env") or {}).get(
        "EASEL_RUN_CONTAINER_TESTS") == "1"
        for _command, kwargs in calls if "pytest" in _command)
    assert all((kwargs.get("env") or {}).get("EASEL_WORKER_IMAGE")
               == env["EASEL_TEST_WORKER_IMAGE"]
               for _command, kwargs in calls if "pytest" in _command)


def test_human_report_does_not_claim_passed_when_evidence_blocked(
        tmp_path, capsys):
    module = _load_module()
    env = _formal_env(tmp_path)

    def green(argv, **kwargs):
        return _successful_command(argv, **kwargs)

    code = module.main(
        [], env=env, platform_name="linux", which=_which,
        run_command=green)
    output = capsys.readouterr().out
    assert code == 1
    assert "release_status=blocked" in output
    assert "release_status=passed" not in output
    assert "evidence_xfs_quota_unverified" in output
    assert "evidence_host_firewall_unverified" in output
    assert "evidence_final_runtime_image_unverified" in output
    assert env["EASEL_TEST_WORKER_IMAGE"] in output
