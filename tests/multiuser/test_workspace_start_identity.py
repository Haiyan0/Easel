"""A4 R1-04：真实 PowerShell 入口对既有 provisioner 的身份核验。

用无副作用 stub 代替 docker/wsl，只验证 Start 的控制流与调用顺序；真实
容器身份由 provisioner_identity 单元测试和隔离测试覆盖。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
START = ROOT / "deploy" / "desktop" / "Start-WorkspaceEasel.ps1"
PROJECT = "easel-r1-test"
CONTROL = "sha256:" + "1" * 64
WORKER = "sha256:" + "2" * 64
GATEWAY = "sha256:" + "3" * 64

pytestmark = pytest.mark.skipif(
    os.name != "nt" or shutil.which("powershell") is None,
    reason="需要 Windows PowerShell")


def _wrapper(tmp_path: Path, identity_exit: int, *,
             keygen_mismatch: bool = False,
             engine_mismatch: bool = False,
             engine_failure: bool = False,
             barrier: str = "none",
             reopen_exit: int = 0) -> Path:
    data = tmp_path / "data"
    workspace = tmp_path / "workspaces"
    public = data / "public"
    public.mkdir(parents=True)
    workspace.mkdir()
    contents = {
        "worker-public.key": "worker",
        "gateway-public.pem": "gateway",
        "egress-policy.json": "{}",
    }
    for name, content in contents.items():
        (public / name).write_text(content, encoding="utf-8")
    worker_hash = hashlib.sha256(
        contents["worker-public.key"].encode()).hexdigest()
    gateway_hash = hashlib.sha256(
        contents["gateway-public.pem"].encode()).hexdigest()
    policy_hash = hashlib.sha256(
        contents["egress-policy.json"].encode()).hexdigest()
    trusted_worker = "0" * 64 if keygen_mismatch else worker_hash
    trusted_gateway = "0" * 64 if keygen_mismatch else gateway_hash
    trusted_policy = "0" * 64 if keygen_mismatch else policy_hash
    windows_engine = "bad" if engine_failure else "engine-test"
    wsl_engine = "other-engine" if engine_mismatch else "engine-test"
    log = tmp_path / "calls.log"
    wrapper = tmp_path / "run-start.ps1"
    wrapper.write_text(
        f"""$ErrorActionPreference = 'Stop'
$env:STUB_LOG = '{log}'
$env:STUB_PROJECT = '{PROJECT}'
$env:STUB_IDENTITY_EXIT = '{identity_exit}'
$env:STUB_TRUSTED_WORKER = '{trusted_worker}'
$env:STUB_TRUSTED_GATEWAY = '{trusted_gateway}'
$env:STUB_TRUSTED_POLICY = '{trusted_policy}'
$env:STUB_WINDOWS_ENGINE = '{windows_engine}'
$env:STUB_WSL_ENGINE = '{wsl_engine}'
$env:STUB_CONTEXT = 'desktop-linux'
$env:STUB_BARRIER = '{barrier}'
$env:STUB_REOPEN_EXIT = '{reopen_exit}'
function Add-StubCall([string]$text) {{
    Add-Content -LiteralPath $env:STUB_LOG -Value $text
}}
function global:docker {{
    param([Parameter(ValueFromRemainingArguments=$true)]$args)
    $items = @($args)
    Add-StubCall ('docker ' + ($items -join ' '))
    $global:LASTEXITCODE = 0
    $joined = $items -join ' '
    if ($joined -match 'test -e /data/control\\.sqlite3') {{
        $global:LASTEXITCODE = 1
        return
    }}
    if ($joined -match 'keygen\\.py' -and $joined -match 'fingerprints') {{
        Write-Output 'key=exists'
        Write-Output 'signing_key=exists'
        Write-Output ('worker_public_sha256=' + $env:STUB_TRUSTED_WORKER)
        Write-Output ('gateway_public_sha256=' + $env:STUB_TRUSTED_GATEWAY)
        return
    }}
    if ($joined -match 'hashlib\\.sha256' -or
        $joined -match 'frontend/dist/index.html') {{
        Write-Output ('f' * 64)
        return
    }}
    if ($joined -match 'sha256sum') {{
        Write-Output ($env:STUB_TRUSTED_POLICY + '  /keys/egress-policy.json')
        Write-Output ($env:STUB_TRUSTED_POLICY + '  /policy/egress-policy.json')
        return
    }}
    if ($items.Count -ge 2 -and $items[0] -eq 'info' -and
        $items[1] -eq '--format') {{
        Write-Output $env:STUB_WINDOWS_ENGINE
        return
    }}
    if ($items.Count -ge 1 -and $items[0] -eq 'context') {{
        Write-Output $env:STUB_CONTEXT
        return
    }}
    if ($items.Count -ge 2 -and $items[0] -eq 'image' -and
        $items[1] -eq 'inspect') {{
        Write-Output $items[$items.Count - 1]
        return
    }}
    if ($items.Count -ge 2 -and $items[0] -eq 'volume' -and
        $items[1] -eq 'inspect') {{
        $name = $items[$items.Count - 1]
        $role = 'control'
        if ($name.EndsWith('-keys')) {{ $role = 'keys' }}
        if ($name.EndsWith('-egress')) {{ $role = 'egress-policy' }}
        Write-Output ('[{{"Name":"' + $name + '","Labels":{{' +
            '"easel.desktop.project":"' + $env:STUB_PROJECT +
            '","easel.desktop.role":"' + $role + '"}}}}]')
        return
    }}
    if ($items.Count -ge 2 -and $items[0] -eq 'network' -and
        $items[1] -eq 'inspect') {{
        Write-Output ('[{{"Name":"' + $items[2] + '","Driver":"bridge",' +
            '"Internal":false,"EnableIPv6":false,"Labels":{{' +
            '"easel.desktop.project":"' + $env:STUB_PROJECT +
            '","easel.desktop.role":"control-network"}}}}]')
        return
    }}
    if ($items.Count -ge 2 -and $items[0] -eq 'ps') {{
        Write-Output 'control-id'
        return
    }}
    if ($items.Count -ge 2 -and $items[0] -eq 'inspect') {{
        $format = $items[2]
        if ($format -match 'Health.Status') {{ Write-Output 'healthy' }}
        else {{ Write-Output 'running healthy' }}
        return
    }}
    if ($items.Count -ge 3 -and $items[0] -eq 'exec' -and
        ($items -join ' ') -match 'barrier-status') {{
        Write-Output ('barrier=' + $env:STUB_BARRIER)
        return
    }}
    if ($items.Count -ge 3 -and $items[0] -eq 'exec' -and
        ($items -join ' ') -match ' admin reopen ') {{
        $global:LASTEXITCODE = [int]$env:STUB_REOPEN_EXIT
        if ($global:LASTEXITCODE -eq 0) {{
            Write-Output 'reopened=stop-test'
        }}
        return
    }}
    return
}}
function global:wsl.exe {{
    param([Parameter(ValueFromRemainingArguments=$true)]$args)
    $items = @($args)
    $text = $items -join ' '
    Add-StubCall ('wsl ' + $text)
    $global:LASTEXITCODE = 0
    if ($text -match 'provisioner_identity.py') {{
        $global:LASTEXITCODE = [int]$env:STUB_IDENTITY_EXIT
        if ($global:LASTEXITCODE -ne 0) {{
            Write-Output 'identity_mismatch=workspaceRoot'
        }}
        return
    }}
    if ($text -match 'docker info --format') {{
        Write-Output $env:STUB_WSL_ENGINE
        return
    }}
    if ($text -match 'docker context show') {{
        Write-Output $env:STUB_CONTEXT
        return
    }}
    if ($text -match ' id -u') {{
        Write-Output '1000'
        return
    }}
    if ($text -match ' id -g') {{
        Write-Output '1000'
        return
    }}
    return
}}
& '{START}' -PublicOrigin 'https://easel.test' -Image '{CONTROL}' `
    -WorkerImage '{WORKER}' -GatewayImage '{GATEWAY}' `
    -ProjectName '{PROJECT}' -Port 17862 -WorkspaceRoot '{workspace}' `
    -DataRoot '{data}' -TimeoutSeconds 2
exit $LASTEXITCODE
""",
        encoding="utf-8")
    return wrapper


def _run(tmp_path: Path, identity_exit: int, *,
         keygen_mismatch: bool = False,
         engine_mismatch: bool = False,
         engine_failure: bool = False,
         barrier: str = "none",
         reopen_exit: int = 0) -> tuple[subprocess.CompletedProcess, str]:
    wrapper = _wrapper(
        tmp_path, identity_exit, keygen_mismatch=keygen_mismatch,
        engine_mismatch=engine_mismatch, engine_failure=engine_failure,
        barrier=barrier, reopen_exit=reopen_exit)
    log = tmp_path / "calls.log"
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(wrapper)],
        capture_output=True, check=False, timeout=120)
    result.stdout = result.stdout.decode("utf-8", errors="replace")
    result.stderr = result.stderr.decode("utf-8", errors="replace")
    return result, log.read_text(encoding="utf-8") if log.exists() else ""


def test_start_reuses_only_matching_provisioner_identity(tmp_path):
    result, calls = _run(tmp_path, 0)
    assert result.returncode == 0, result.stderr
    assert "provisioner_identity.py" in calls
    assert "docker compose" in calls
    assert "started project=" in result.stdout


def test_start_rejects_parameter_drift_without_stopping_active_process(tmp_path):
    result, calls = _run(tmp_path, 3)
    assert result.returncode != 0
    assert "provisioner_config_mismatch" in result.stderr
    assert "provisioner_identity.py" in calls
    assert "docker compose" not in calls
    assert "rm -f" not in calls


def test_start_rejects_wrong_service_without_removing_socket(tmp_path):
    result, calls = _run(tmp_path, 4)
    assert result.returncode != 0
    assert "provisioner_identity_unavailable" in result.stderr
    assert "provisioner_identity.py" in calls
    assert "rm -f" not in calls


def test_start_rejects_host_public_copy_not_derived_from_control_key(tmp_path):
    result, calls = _run(tmp_path, 0, keygen_mismatch=True)
    assert result.returncode != 0
    assert "public_copy_mismatch:worker" in result.stderr
    assert "docker compose" not in calls


def test_start_rejects_windows_wsl_engine_drift(tmp_path):
    result, calls = _run(tmp_path, 0, engine_mismatch=True)
    assert result.returncode != 0
    assert "docker_engine_mismatch" in result.stderr
    assert "provisioner_identity.py" not in calls
    assert "docker compose" not in calls


def test_start_rejects_uncollectable_engine_identity(tmp_path):
    result, calls = _run(tmp_path, 0, engine_failure=True)
    assert result.returncode != 0
    assert "docker_engine_unavailable" in result.stderr
    assert "provisioner_identity.py" not in calls
    assert "docker compose" not in calls


def test_start_reopens_only_completed_stop_barrier(tmp_path):
    result, calls = _run(
        tmp_path, 0, barrier="stop-completed", reopen_exit=0)
    assert result.returncode == 0, result.stderr
    assert "admin reopen" in calls
    assert "admission_barrier_reopened=stop-completed" in result.stdout


def test_start_keeps_failure_barrier_closed(tmp_path):
    result, calls = _run(
        tmp_path, 0, barrier="stop-failed", reopen_exit=1)
    assert result.returncode != 0
    assert "admission_barrier_active" in result.stderr
    assert "admin reopen" in calls


def test_status_missing_resources_exits_nonzero(tmp_path):
    status = ROOT / "deploy" / "desktop" / "Status-WorkspaceEasel.ps1"
    wrapper = tmp_path / "run-status.ps1"
    wrapper.write_text(
        f"""$ErrorActionPreference = 'Stop'
function global:docker {{
    param([Parameter(ValueFromRemainingArguments=$true)]$args)
    $global:LASTEXITCODE = 1
    return
}}
function global:wsl.exe {{
    param([Parameter(ValueFromRemainingArguments=$true)]$args)
    $global:LASTEXITCODE = 1
    return
}}
& '{status}' -ProjectName '{PROJECT}' -Distro 'Ubuntu-26.04' `
    -DataRoot '{tmp_path / "data"}'
exit $LASTEXITCODE
""",
        encoding="utf-8")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(wrapper)],
        capture_output=True, check=False, timeout=120)
    stdout = result.stdout.decode("utf-8", errors="replace")
    assert result.returncode != 0
    assert "status=failed" in stdout
    assert "network_missing" in stdout
    assert "control_missing" in stdout
    assert "provisioner_socket_missing" in stdout
