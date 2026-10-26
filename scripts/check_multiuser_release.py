#!/usr/bin/env python
"""多人工作区正式发布门槛检查器。

本脚本只检查 **目标正式部署**。本机 Windows/Docker Desktop 或临时假
Harness 的双用户结果不能作为发布证据；缺少 Linux、Docker、XFS 项目配额、
正式控制面 URL/邀请、固定 worker 镜像或备份源时直接失败，不进入门槛。
跳过、预期失败（xfail）或未被 marker 选中的真实测试同样按失败处理。

输出只包含检查/门槛状态与稳定错误码，不打印邀请、token、master key、宿主
路径或测试原始输出。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE_MARKER = "multiuser_integration"
WORKER_IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")

REQUIRED_ENV = (
    "EASEL_TEST_CONTROL_URL",
    "EASEL_TEST_INVITE_A",
    "EASEL_TEST_INVITE_B",
    "EASEL_TEST_WORKSPACE_ROOT",
    "EASEL_MASTER_KEY_FILE",
    "EASEL_TEST_WORKER_IMAGE",
    "EASEL_TEST_BACKUP_SOURCE",
    "EASEL_TEST_RESTORE_DEST",
    "EASEL_TEST_PREVIEW_URL",
)
"""正式 live/restore 测试所需的最小环境；值本身不进入输出。"""

UNSAFE_OVERRIDES = (
    "EASEL_ALLOW_UNQUOTAED",
    "EASEL_DISABLE_SANDBOX",
    "EASEL_SKIP_ISOLATION",
    "EASEL_ALLOW_UNISOLATED",
)
"""任何绕过配额/沙箱/隔离的开关都使正式检查失败。"""


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    code: str = ""


@dataclass(frozen=True)
class GateResult:
    name: str
    status: str
    code: str = ""


def _passed(name: str) -> Check:
    return Check(name, "pass")


def _failed(name: str, code: str) -> Check:
    return Check(name, "fail", code)


def all_checks_passed(checks: Sequence[Check]) -> bool:
    return bool(checks) and all(check.status == "pass" for check in checks)


def _truthy(value: str | None) -> bool:
    return value is not None and value not in ("", "0", "false", "False")


def _run(run_command: Callable, argv: Sequence[str], **kwargs):
    try:
        return run_command(list(argv), **kwargs)
    except (OSError, subprocess.SubprocessError):
        return None


def check_prerequisites(
        env: Mapping[str, str] | None = None, *,
        platform_name: str | None = None,
        which: Callable[[str], str | None] = shutil.which,
        run_command: Callable = subprocess.run) -> list[Check]:
    """先验平台/安全边界/资源/测试环境；任何缺失都是失败而不是 skip。"""
    values = dict(os.environ if env is None else env)
    platform_name = sys.platform if platform_name is None else platform_name
    checks: list[Check] = []

    if not platform_name.startswith("linux"):
        # 本机 Windows 的 Docker Desktop 或假 Harness 不能冒充正式宿主。
        return [_failed("platform", "platform_not_linux")]
    checks.append(_passed("platform"))

    if any(_truthy(values.get(name)) for name in UNSAFE_OVERRIDES):
        checks.append(_failed("safety", "unsafe_override"))
    else:
        checks.append(_passed("safety"))

    for name in REQUIRED_ENV:
        if values.get(name):
            checks.append(_passed(f"env:{name}"))
        else:
            checks.append(_failed(f"env:{name}", f"missing_env:{name}"))

    control_url = values.get("EASEL_TEST_CONTROL_URL", "")
    if control_url and not control_url.startswith("https://"):
        checks.append(_failed("control_url", "control_url_not_https"))
    elif control_url:
        checks.append(_passed("control_url"))

    preview_url = values.get("EASEL_TEST_PREVIEW_URL", "")
    if preview_url and not preview_url.startswith("https://"):
        checks.append(_failed("preview_url", "preview_url_not_https"))
    elif preview_url:
        checks.append(_passed("preview_url"))

    image = values.get("EASEL_TEST_WORKER_IMAGE", "")
    if image and WORKER_IMAGE.fullmatch(image) is None:
        checks.append(_failed("worker_image", "worker_image_not_pinned"))
    elif image:
        checks.append(_passed("worker_image"))

    key_value = values.get("EASEL_MASTER_KEY_FILE", "")
    if not key_value:
        checks.append(_failed("master_key", "master_key_missing"))
    else:
        key_path = Path(key_value)
        try:
            data = key_path.read_bytes()
        except OSError:
            checks.append(_failed("master_key", "master_key_missing"))
        else:
            if len(data) != 32:
                checks.append(_failed("master_key", "master_key_invalid_length"))
            elif os.name == "posix" and key_path.stat().st_mode & 0o077:
                checks.append(_failed("master_key", "master_key_permissions"))
            else:
                checks.append(_passed("master_key"))

    workspace_value = values.get("EASEL_TEST_WORKSPACE_ROOT", "")
    if not workspace_value:
        checks.append(_failed("workspace_quota", "workspace_root_missing"))
    else:
        workspace = Path(workspace_value)
        if not workspace.is_dir():
            checks.append(_failed("workspace_quota", "workspace_root_missing"))
        else:
            findmnt = which("findmnt")
            xfs_quota = which("xfs_quota")
            if not findmnt:
                checks.append(_failed("workspace_quota", "findmnt_missing"))
            elif not xfs_quota:
                checks.append(_failed("workspace_quota", "xfs_quota_missing"))
            else:
                result = _run(
                    run_command,
                    [findmnt, "-no", "FSTYPE,OPTIONS", "--target",
                     str(workspace)],
                    capture_output=True, text=True, timeout=15)
                if result is None or result.returncode != 0:
                    checks.append(_failed("workspace_quota",
                                          "filesystem_unavailable"))
                else:
                    lines = (result.stdout or "").strip().splitlines()
                    fields = lines[0].split() if lines else []
                    fstype = fields[0] if fields else ""
                    options = fields[1] if len(fields) > 1 else ""
                    quota = any(option in ("prjquota", "pquota")
                                for option in options.split(","))
                    if fstype != "xfs":
                        checks.append(_failed("workspace_quota",
                                              "workspace_not_xfs"))
                    elif not quota:
                        checks.append(_failed("workspace_quota",
                                              "project_quota_missing"))
                    else:
                        checks.append(_passed("workspace_quota"))

    backup_value = values.get("EASEL_TEST_BACKUP_SOURCE", "")
    if not backup_value:
        checks.append(_failed("backup_source", "backup_source_missing"))
    else:
        backup = Path(backup_value)
        if not backup.is_dir() or not (backup / "manifest.json").is_file():
            checks.append(_failed("backup_source", "backup_source_missing"))
        else:
            checks.append(_passed("backup_source"))

    restore_value = values.get("EASEL_TEST_RESTORE_DEST", "")
    if not restore_value:
        checks.append(_failed("restore_target", "restore_dest_missing"))
    else:
        restore = Path(restore_value)
        if restore.exists() and (not restore.is_dir()
                                 or any(restore.iterdir())):
            checks.append(_failed("restore_target",
                                  "restore_dest_not_empty"))
        else:
            checks.append(_passed("restore_target"))

    docker = which("docker")
    if not docker:
        checks.append(_failed("docker", "docker_missing"))
    else:
        result = _run(
            run_command,
            [docker, "info", "--format", "{{json .SecurityOptions}}"],
            capture_output=True, text=True, timeout=30)
        if result is None or result.returncode != 0:
            checks.append(_failed("docker", "docker_unavailable"))
        else:
            try:
                options = json.loads(result.stdout or "[]")
            except ValueError:
                checks.append(_failed("docker", "docker_info_invalid"))
            else:
                if not isinstance(options, list):
                    checks.append(_failed("docker", "docker_info_invalid"))
                elif any("rootless" in str(item).lower() for item in options):
                    # rootless 的项目配额/UID 映射/SO_PEERCRED 尚无自动门槛；
                    # 不在检查器里用环境变量声明冒充已实测。
                    checks.append(_failed("docker", "rootless_unverified"))
                else:
                    checks.append(_passed("docker"))

    playwright = _run(
        run_command, [sys.executable, "-c", "import playwright.sync_api"],
        capture_output=True, text=True, timeout=30)
    if playwright is None or playwright.returncode != 0:
        checks.append(_failed("playwright", "playwright_missing"))
    else:
        checks.append(_passed("playwright"))
    return checks


def _count(output: str, label: str) -> int:
    match = re.search(rf"(\d+) {label}\b", output)
    return int(match.group(1)) if match else 0


def classify_gate_result(
        name: str, returncode: int, output: str, *,
        allow_skips: bool = False,
        require_marker_coverage: bool = False) -> GateResult:
    """把 pytest 结果映射为发布门槛；skip/xfail/xpass 默认失败。"""
    text = output or ""
    if returncode != 0:
        return GateResult(name, "fail", "gate_failed")
    if ("no tests ran" in text or "collected 0 items" in text
            or _count(text, "passed") == 0):
        return GateResult(name, "fail", "gate_empty")
    if not allow_skips:
        if _count(text, "skipped"):
            return GateResult(name, "fail", "gate_skipped")
        if _count(text, "xfailed"):
            return GateResult(name, "fail", "gate_xfailed")
        if _count(text, "xpassed"):
            return GateResult(name, "fail", "gate_xpassed")
    if require_marker_coverage and _count(text, "deselected"):
        return GateResult(name, "fail", "gate_unmarked")
    return GateResult(name, "pass")


def _pytest(python: str, *args: str) -> list[str]:
    return [python, "-m", "pytest", *args, "-q", "-rsxX"]


def run_release_gates(
        env: Mapping[str, str] | None = None, *,
        python: str | None = None,
        run_command: Callable = subprocess.run,
        root: Path = ROOT) -> list[GateResult]:
    """运行真实门槛；marker live 测试之外的容器/浏览器测试也必须实际执行。"""
    python = python or sys.executable
    environment = dict(os.environ)
    environment.update(env or {})
    # 显式启用真实 Docker 测试；测试文件自身的 skipif 不再作为缺省放行。
    environment["EASEL_RUN_CONTAINER_TESTS"] = "1"
    # 正式拓扑门槛必须使用检查器确认过的固定镜像摘要，而不是任意 tag。
    environment["EASEL_WORKER_IMAGE"] = environment.get(
        "EASEL_TEST_WORKER_IMAGE", "")

    live_files = (
        "tests/integration/multiuser/test_two_users_live.py",
        "tests/integration/multiuser/test_browser_live.py",
        "tests/integration/multiuser/test_restore_live.py",
    )
    gates = [
        # 只选显式 live 文件，避免把同目录的 Docker/浏览器本机测试误算成
        # 未打 marker；仍用 marker 和 deselected 检查防止新增 live 测试漏选。
        ("live_two_users",
         _pytest(python, *live_files, "--multiuser-live", "-m", LIVE_MARKER),
         root, False, True),
        ("container_isolation",
         _pytest(python,
                 "tests/integration/multiuser/test_container_isolation.py"),
         root, True, False),
        ("network_wiring",
         _pytest(python,
                 "tests/integration/multiuser/test_network_wiring.py"),
         root, True, False),
        ("control_broker_topology",
         _pytest(python,
                 "tests/integration/multiuser/test_control_broker_topology.py"),
         root, True, False),
        ("browser_profiles_real",
         _pytest(python,
                 "tests/integration/multiuser/test_browser_profiles_real.py"),
         root, True, False),
        ("sidecar_provisioner_real",
         _pytest(python,
                 "tests/integration/multiuser/test_sidecar_provisioner_real.py"),
         root, True, False),
        ("backend_regression",
         _pytest(python, "tests/multiuser"),
         root, True, False),
    ]
    results: list[GateResult] = []
    for name, argv, cwd, allow_skips, marker_coverage in gates:
        result = _run(run_command, argv, cwd=str(cwd), env=environment)
        if result is None:
            results.append(GateResult(name, "fail", "gate_unavailable"))
            continue
        output = (result.stdout or "") + (result.stderr or "")
        results.append(classify_gate_result(
            name, result.returncode, output, allow_skips=allow_skips,
            require_marker_coverage=marker_coverage))
    return results


def _print_report(checks: Sequence[Check], gates: Sequence[GateResult],
                  ok: bool, *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps({
            "release_status": "passed" if ok else "failed",
            "checks": [{"name": check.name, "status": check.status,
                        "code": check.code} for check in checks],
            "gates": [{"name": gate.name, "status": gate.status,
                       "code": gate.code} for gate in gates],
        }, ensure_ascii=False, separators=(",", ":")))
        return
    print(f"release_status={'passed' if ok else 'failed'}")
    for check in checks:
        suffix = f" {check.code}" if check.code else ""
        print(f"[{check.status}] {check.name}{suffix}")
    if not gates:
        print("gates=not_run")
    for gate in gates:
        suffix = f" {gate.code}" if gate.code else ""
        print(f"[{gate.status}] gate:{gate.name}{suffix}")


def main(argv: Sequence[str] | None = None, *,
         env: Mapping[str, str] | None = None,
         platform_name: str | None = None,
         which: Callable[[str], str | None] = shutil.which,
         run_command: Callable = subprocess.run,
         python: str | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="多人工作区正式发布门槛检查（缺前提即失败）")
    parser.add_argument("--json", action="store_true",
                        help="只输出机器可读状态")
    args = parser.parse_args(argv)
    checks = check_prerequisites(
        env, platform_name=platform_name, which=which,
        run_command=run_command)
    if not all_checks_passed(checks):
        _print_report(checks, [], False, json_mode=args.json)
        return 1
    gates = run_release_gates(
        env, python=python, run_command=run_command)
    ok = bool(gates) and all(gate.status == "pass" for gate in gates)
    _print_report(checks, gates, ok, json_mode=args.json)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
