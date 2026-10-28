"""A4 R3：正式 Start/Stop 的跨进程生命周期互斥。

用真实命名互斥与正式 Stop 入口、无 Docker 副作用的 stub，验证第二个
Stop 在第一个持锁期间不会发送 drain/down/pkill；释放后才重新读取当前状态。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STOP = ROOT / "deploy" / "desktop" / "Stop-WorkspaceEasel.ps1"
COMMON = ROOT / "deploy" / "desktop" / "EaselDesktop.Common.ps1"
PROJECT = "easel-r3-lock-test"

pytestmark = pytest.mark.skipif(
    os.name != "nt" or shutil.which("powershell") is None,
    reason="需要 Windows PowerShell")


def _wait_file(path: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"等待文件超时：{path}")


def _holder_script(tmp_path: Path, held: Path, release: Path,
                   project: str = PROJECT) -> Path:
    script = tmp_path / "hold-lock.ps1"
    script.write_text(
        f"""$ErrorActionPreference = 'Stop'
. '{COMMON}'
$lock = Enter-EaselLifecycleLock -ProjectName '{project}' `
    -TimeoutSeconds 30
[IO.File]::WriteAllText('{held}', 'held')
$deadline = (Get-Date).AddSeconds(30)
while (-not (Test-Path -LiteralPath '{release}')) {{
    if ((Get-Date) -ge $deadline) {{ throw 'holder_timeout' }}
    Start-Sleep -Milliseconds 50
}}
Exit-EaselLifecycleLock -Lock $lock
""",
        encoding="utf-8")
    return script


def _stop_wrapper(tmp_path: Path, data_root: Path, log: Path) -> Path:
    wrapper = tmp_path / "run-stop.ps1"
    wrapper.write_text(
        f"""$ErrorActionPreference = 'Stop'
$env:STUB_STOP_LOG = '{log}'
function Add-StubCall([string]$text) {{
    Add-Content -LiteralPath $env:STUB_STOP_LOG -Value $text
}}
function global:docker {{
    param([Parameter(ValueFromRemainingArguments=$true)]$args)
    $items = @($args)
    $joined = $items -join ' '
    Add-StubCall ('docker ' + $joined)
    $global:LASTEXITCODE = 0
    if ($items.Count -ge 2 -and $items[0] -eq 'ps') {{
        Write-Output 'control-id'
        return
    }}
    if ($joined -match 'admin drain') {{
        Write-Output 'barrier=stop-test'
        Write-Output 'drained=0'
        Write-Output 'verified=settled'
        return
    }}
    if ($joined -match 'admin stop-complete') {{
        Write-Output 'stop_completed=stop-test'
        return
    }}
    if ($joined -match 'inspect --format') {{
        Write-Output ('sha256:' + ('a' * 64))
        return
    }}
    return
}}
function global:wsl.exe {{
    param([Parameter(ValueFromRemainingArguments=$true)]$args)
    $text = @($args) -join ' '
    Add-StubCall ('wsl ' + $text)
    $global:LASTEXITCODE = 0
    return
}}
& '{STOP}' -ProjectName '{PROJECT}' -DataRoot '{data_root}' `
    -LockTimeoutSeconds 30
exit $LASTEXITCODE
""",
        encoding="utf-8")
    return wrapper


def test_lifecycle_lock_blocks_second_powershell_process(tmp_path):
    held = tmp_path / "held"
    release = tmp_path / "release"
    holder = _holder_script(tmp_path, held, release)
    process = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(holder)], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)
    try:
        _wait_file(held)
        contender = tmp_path / "contend.ps1"
        marker = tmp_path / "entered"
        contender.write_text(
            f"""$ErrorActionPreference = 'Stop'
. '{COMMON}'
$lock = Enter-EaselLifecycleLock -ProjectName '{PROJECT}' `
    -TimeoutSeconds 2
[IO.File]::WriteAllText('{marker}', 'entered')
Exit-EaselLifecycleLock -Lock $lock
""",
            encoding="utf-8")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(contender)], capture_output=True,
            check=False, timeout=20)
        stderr = result.stderr.decode("utf-8", errors="replace")
        assert result.returncode != 0
        assert "desktop_lifecycle_busy" in stderr
        assert not marker.exists()
        release.write_text("release", encoding="utf-8")
        assert process.wait(timeout=20) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_lifecycle_lock_is_per_project(tmp_path):
    held = tmp_path / "held"
    release = tmp_path / "release"
    holder = _holder_script(tmp_path, held, release)
    process = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(holder)], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)
    try:
        _wait_file(held)
        contender = tmp_path / "contend-other.ps1"
        marker = tmp_path / "entered-other"
        contender.write_text(
            f"""$ErrorActionPreference = 'Stop'
. '{COMMON}'
$lock = Enter-EaselLifecycleLock -ProjectName `
    'easel-r3-other-project' -TimeoutSeconds 2
[IO.File]::WriteAllText('{marker}', 'entered')
Exit-EaselLifecycleLock -Lock $lock
""",
            encoding="utf-8")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(contender)], capture_output=True, check=False,
            timeout=20)
        assert result.returncode == 0, result.stderr.decode(
            "utf-8", errors="replace")
        assert marker.exists()
        release.write_text("release", encoding="utf-8")
        assert process.wait(timeout=20) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_start_waits_for_lock_before_reading_or_starting(tmp_path):
    from tests.multiuser import test_workspace_start_identity as start_tests

    start_dir = tmp_path / "start"
    start_dir.mkdir()
    held = tmp_path / "start-held"
    release = tmp_path / "start-release"
    holder = _holder_script(
        tmp_path, held, release, project=start_tests.PROJECT)
    holder_process = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(holder)], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)
    try:
        _wait_file(held)
        wrapper = start_tests._wrapper(start_dir, 0)
        log = start_dir / "calls.log"
        start_process = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(wrapper)], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        time.sleep(1.5)
        before = log.read_text(encoding="utf-8") if log.exists() else ""
        assert "keygen.py" not in before
        assert "docker compose" not in before

        release.write_text("release", encoding="utf-8")
        stdout, stderr = start_process.communicate(timeout=40)
        calls = log.read_text(encoding="utf-8")
        assert start_process.returncode == 0, (
            stderr.decode("utf-8", errors="replace"))
        assert "keygen.py" in calls
        assert "docker compose" in calls
        assert "started project=" in stdout.decode("utf-8", errors="replace")
        assert holder_process.wait(timeout=20) == 0
    finally:
        if holder_process.poll() is None:
            holder_process.kill()
            holder_process.wait(timeout=10)
        if "start_process" in locals() and start_process.poll() is None:
            start_process.kill()
            start_process.wait(timeout=10)


def test_stop_does_not_touch_topology_while_lock_is_held(tmp_path):
    data_root = tmp_path / "data"
    logs = data_root / "logs"
    logs.mkdir(parents=True)
    (logs / "provisioner-expected.json").write_text(
        '{"engineId":"engine-test"}', encoding="utf-8")
    log = tmp_path / "stop-calls.log"
    held = tmp_path / "held"
    release = tmp_path / "release"
    holder = _holder_script(tmp_path, held, release)
    holder_process = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(holder)], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)
    try:
        _wait_file(held)
        wrapper = _stop_wrapper(tmp_path, data_root, log)
        stop_process = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(wrapper)], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        time.sleep(1.5)
        before = log.read_text(encoding="utf-8") if log.exists() else ""
        assert "admin drain" not in before
        assert "docker compose" not in before
        assert "pkill" not in before

        release.write_text("release", encoding="utf-8")
        stdout, stderr = stop_process.communicate(timeout=40)
        calls = log.read_text(encoding="utf-8")
        assert stop_process.returncode == 0, (
            stderr.decode("utf-8", errors="replace"))
        drain_at = calls.find("admin drain")
        down_at = calls.find("docker compose")
        pkill_at = calls.find("pkill")
        assert drain_at >= 0 and down_at > drain_at and pkill_at > down_at
        assert "stop_completed=stop-test" in stdout.decode(
            "utf-8", errors="replace")
        assert holder_process.wait(timeout=20) == 0
    finally:
        if holder_process.poll() is None:
            holder_process.kill()
            holder_process.wait(timeout=10)
        if "stop_process" in locals() and stop_process.poll() is None:
            stop_process.kill()
            stop_process.wait(timeout=10)
