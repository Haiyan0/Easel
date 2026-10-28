"""A4 R1-04：真实 PowerShell 入口对既有 provisioner 的身份核验。

用无副作用 stub 代替 docker/wsl，只验证 Start 的控制流与调用顺序；真实
容器身份由 provisioner_identity 单元测试和隔离测试覆盖。
"""
from __future__ import annotations

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


def _wrapper(tmp_path: Path, identity_exit: int) -> Path:
    data = tmp_path / "data"
    workspace = tmp_path / "workspaces"
    public = data / "public"
    public.mkdir(parents=True)
    workspace.mkdir()
    for name, content in (
            ("worker-public.key", "worker"),
            ("gateway-public.pem", "gateway"),
            ("egress-policy.json", "{}")):
        (public / name).write_text(content, encoding="utf-8")
    log = tmp_path / "calls.log"
    wrapper = tmp_path / "run-start.ps1"
    wrapper.write_text(
        f"""$ErrorActionPreference = 'Stop'
$env:STUB_LOG = '{log}'
$env:STUB_PROJECT = '{PROJECT}'
$env:STUB_IDENTITY_EXIT = '{identity_exit}'
function Add-StubCall([string]$text) {{
    Add-Content -LiteralPath $env:STUB_LOG -Value $text
}}
function global:docker {{
    param([Parameter(ValueFromRemainingArguments=$true)]$args)
    $items = @($args)
    Add-StubCall ('docker ' + ($items -join ' '))
    $global:LASTEXITCODE = 0
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
        Write-Output 'barrier=none'
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


def _run(tmp_path: Path, identity_exit: int) -> tuple[subprocess.CompletedProcess, str]:
    wrapper = _wrapper(tmp_path, identity_exit)
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
