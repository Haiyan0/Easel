"""Desktop 生命周期脚本与交付命令的参数契约。

防止报告/模板再次传入脚本不支持的参数（例如 Stop 的 -WorkspaceRoot），
并锁定受控更新脚本必须存在的安全开关。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "deploy" / "desktop"
RUNBOOK = (ROOT / "docs" / "evidence" / "acceptance-503-repair-r1"
           / "commands.md")

SCRIPTS = {
    "Start-WorkspaceEasel.ps1": DESKTOP / "Start-WorkspaceEasel.ps1",
    "Stop-WorkspaceEasel.ps1": DESKTOP / "Stop-WorkspaceEasel.ps1",
    "Update-WorkspaceEasel.ps1": DESKTOP / "Update-WorkspaceEasel.ps1",
    "Backup-WorkspaceEasel.ps1": DESKTOP / "Backup-WorkspaceEasel.ps1",
    "Verify-WorkspaceEasel.ps1": DESKTOP / "Verify-WorkspaceEasel.ps1",
    "Restore-WorkspaceEasel.ps1": DESKTOP / "Restore-WorkspaceEasel.ps1",
}

EXPECTED_PARAMETERS = {
    "Stop-WorkspaceEasel.ps1": {
        "ProjectName", "Distro", "WslRepo", "DataRoot",
        "LockTimeoutSeconds", "Force"},
    "Start-WorkspaceEasel.ps1": {
        "PublicOrigin", "Image", "WorkerImage", "GatewayImage",
        "ProjectName", "Port", "PublishMode", "Distro", "WslRepo",
        "WorkspaceRoot", "DataRoot", "DatabaseQuerySecrets",
        "EgressCaFile", "UpgradeFromBackup",
        "ProvisionerGid", "TimeoutSeconds", "LockTimeoutSeconds",
        "UpdateBarrierId", "RestartProvisionerIfStale",
        "LifecycleLockHeld"},
    "Update-WorkspaceEasel.ps1": {
        "PublicOrigin", "Image", "WorkerImage", "GatewayImage",
        "ProjectName", "Port", "Distro", "WslRepo", "WorkspaceRoot",
        "DataRoot", "DatabaseQuerySecrets", "ProvisionerGid",
        "TimeoutSeconds", "LockTimeoutSeconds"},
}


def _parameters(script: pathlib.Path) -> set[str]:
    text = script.read_text(encoding="utf-8-sig")
    match = re.search(r"(?ms)^param\((.*?)^\)", text)
    assert match is not None, script.name
    return set(re.findall(r"\$(\w+)\s*(?:=|,|$)", match.group(1)))


def test_script_parameter_contracts_are_stable():
    for name, expected in EXPECTED_PARAMETERS.items():
        assert _parameters(SCRIPTS[name]) == expected, name


def test_stop_has_no_workspace_root_parameter():
    """Stop 只按项目/卷/控制库停止；不接收 worker 持久根。"""
    assert "WorkspaceRoot" not in _parameters(SCRIPTS["Stop-WorkspaceEasel.ps1"])


def test_runbook_commands_only_use_supported_parameters():
    text = RUNBOOK.read_text(encoding="utf-8")
    pattern = re.compile(
        r"powershell\.exe\b[^\n]*?-File\s+"
        r"(?:\./)?deploy/desktop/([A-Za-z0-9_.-]+\.ps1)([^\n]*)")
    matches = pattern.findall(text)
    assert matches, "runbook 中必须包含正式 PowerShell 命令"
    for script_name, tail in matches:
        assert script_name in SCRIPTS, script_name
        used = set(re.findall(r"(?<!\w)-([A-Za-z]\w+)", tail))
        supported = _parameters(SCRIPTS[script_name])
        assert used <= supported, (script_name, sorted(used - supported))
