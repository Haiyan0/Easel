"""A7 R2 主审反例；仅使用临时假材料，不连接 Docker/WSL/供应商。"""
from __future__ import annotations

import contextlib
import hashlib
import json
import runpy
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from easel.control.cli import (materialize_restore, prepare_restored_instance,
                               restore_backup, verify_backup)
from easel.control.store import Store
from easel.control.types import DomainError


def main() -> None:
    fixture = runpy.run_path(str(ROOT / "tests/multiuser/test_operations_a7.py"))
    with tempfile.TemporaryDirectory(prefix="easel-a7-r2-prepare-") as directory:
        root = Path(directory)
        seeded = fixture["_seed"](root)
        fixture["_stop"](seeded)
        backup, staging = root / "backup", root / "staging"
        fixture["_backup"](seeded, backup)
        restore_backup(backup, staging)
        with contextlib.closing(sqlite3.connect(
                staging / "control/control.sqlite3")) as database:
            database.execute("UPDATE users SET display_name=? WHERE id=?",
                             ("CORRUPTED-STAGING", seeded["member"].user_id))
            database.commit()
        try:
            verify_backup(staging)
            print("TAMPER_VERIFY=accepted")
        except DomainError as error:
            print("TAMPER_VERIFY=" + error.code)
        prepare_restored_instance(staging, now=int(time.time()))
        control, keys, egress, workspaces = fixture["_empty_targets"](root)
        materialize_restore(staging, control_dir=control, keys_dir=keys,
                            egress_dir=egress, workspace_root=workspaces)
        with contextlib.closing(sqlite3.connect(
                control / "control.sqlite3")) as database:
            name = database.execute("SELECT display_name FROM users WHERE id=?",
                                    (seeded["member"].user_id,)).fetchone()[0]
        print("MATERIALIZED_NAME=" + name)

    with tempfile.TemporaryDirectory(prefix="easel-a7-r2-readonly-") as directory:
        root = Path(directory)
        seeded = fixture["_seed"](root)
        staging = fixture["_prepared_staging"](seeded, root)
        before = {path.relative_to(staging).as_posix()
                  for path in staging.rglob("*") if path.is_file()}
        verify_backup(staging)
        after = {path.relative_to(staging).as_posix()
                 for path in staging.rglob("*") if path.is_file()}
        print("FIRST_VERIFY_ADDED=" + json.dumps(sorted(after - before)))
        try:
            verify_backup(staging)
            print("SECOND_VERIFY=accepted")
        except DomainError as error:
            print("SECOND_VERIFY=" + error.code)

    with tempfile.TemporaryDirectory(prefix="easel-a7-r2-upgrade-") as directory:
        root = Path(directory)
        seeded = fixture["_seed"](root)
        deployment = fixture["_deployment"]()
        deployment.update(workspace_root=str(seeded["workspace_root"]),
                          data_root=str(root))
        fixture["_stop"](seeded, barrier_id="old-stop")
        backup = root / "old-backup"
        fixture["_backup"](seeded, backup, deployment=deployment,
                           require_clean_stop=True)
        verified = verify_backup(backup)
        print("OLD_BACKUP_REAL_VERIFY=" + str(verified["version"] == 2))
        now = int(time.time())
        with Store.open(seeded["control"] / "control.sqlite3") as store:
            store.reopen_after_completed_stop(
                "old-stop", fixture["PROJECT"], "a" * 64, now)
            store.create_conversation(seeded["member"], "AFTER-BACKUP")
        fixture["_stop"](seeded, barrier_id="new-stop")
        old_deployment = root / "deployment.json"
        old_deployment.write_text(json.dumps(deployment), encoding="utf-8")
        common = ROOT / "deploy/desktop/EaselDesktop.Common.ps1"
        # 只替换外部容器调用；输出来自上方真实 verify 的成功结果。
        script = root / "precheck.ps1"
        script.write_text(
            "param([string]$Common,[string]$Backup,[string]$Deployment)\n"
            "$ErrorActionPreference='Stop'\n. $Common\n"
            "function ConvertTo-EaselWslPath { param([string]$Path) return $Path }\n"
            "function Invoke-Wsl { param([string[]]$Arguments) "
            "return [pscustomobject]@{ExitCode=0;Output=@('version=2','clean_stop=true')} }\n"
            "$null=Assert-EaselUpgradeBackup -Backup $Backup "
            "-ProjectName '" + fixture["PROJECT"] + "' "
            "-OldDeploymentFile $Deployment -Distro 'stub' "
            "-Image '" + deployment["control_image"] + "'\n"
            "Write-Output 'OLD_BACKUP_PRECHECK_ACCEPTED=True'\n",
            encoding="utf-8-sig")
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-File", str(script), "-Common", str(common),
             "-Backup", str(backup), "-Deployment", str(old_deployment)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(result.stdout.strip())
        if result.returncode:
            raise RuntimeError(result.stderr)
        manifest_bytes = (backup / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        print("BACKUP_STOP=" + manifest["runtime_state"]["stop"]["barrierId"])
        with Store.open(seeded["control"] / "control.sqlite3") as store:
            print("CURRENT_STOP=" + store.stop_completion()["barrierId"])
            store.upgrade_stop_completion(
                "new-stop", fixture["PROJECT"], "a" * 64, "b" * 64,
                hashlib.sha256(manifest_bytes).hexdigest(), now)
        print("UPGRADE_WITH_OLD_BACKUP_ACCEPTED=True")
    print("TEMP_CLEANUP=ok")


if __name__ == "__main__":
    main()
