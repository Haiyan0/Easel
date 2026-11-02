# R1 正式命令模板（参数契约）

以下命令均在**新版源码根目录**执行，使用当前窗口执行策略
`-NoProfile -ExecutionPolicy Bypass`。`Distro` 与 `ProvisionerGid` 必须
来自现场实际登记/读取（`wsl -d <distro> -- id -g`），不得沿用占位值。
`<...>` 只表示需要在现场替换的值。

## 受控更新（旧 control + 旧 provisioner → 新版两端）

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File deploy/desktop/Update-WorkspaceEasel.ps1 -PublicOrigin <origin> -Image <new-control-image> -WorkerImage <worker-image> -GatewayImage <gateway-image> -ProjectName <project> -Port <port> -Distro <distro> -WslRepo <wsl-repo> -WorkspaceRoot <workspace-root> -DataRoot <data-root> -ProvisionerGid <gid>
```

## Stop（drain 后停止 control；不传 -WorkspaceRoot）

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File deploy/desktop/Stop-WorkspaceEasel.ps1 -ProjectName <project> -Distro <distro> -WslRepo <wsl-repo> -DataRoot <data-root>
```

## Backup（必须在 Stop 完成后）

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File deploy/desktop/Backup-WorkspaceEasel.ps1 -ProjectName <project> -Distro <distro> -WslRepo <wsl-repo> -DataRoot <data-root> -WorkspaceRoot <workspace-root> -Image <control-image>
```

## Verify Backup

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File deploy/desktop/Verify-WorkspaceEasel.ps1 -Backup <backup-dir> -Image <control-image> -Distro <distro>
```

## Restore（回退到新项目/新目录/新端口，不覆盖原实例）

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File deploy/desktop/Restore-WorkspaceEasel.ps1 -Backup <backup-dir> -ProjectName <rollback-project> -PublicOrigin <origin> -Image <control-image> -WorkerImage <worker-image> -GatewayImage <gateway-image> -Port <rollback-port> -Distro <distro> -WslRepo <wsl-repo> -DataRoot <rollback-data-root> -WorkspaceRoot <rollback-workspace-root> -ProvisionerGid <gid>
```

## 更新前材料与回退点

- 更新前**没有**一致备份：受支持的完整备份需要成功 Stop，而现场
  503 坏状态会阻断旧 control 的 drain。不要把这套流程描述成
  “先更新再备份”已经拥有升级前备份。
- 更新前的保护材料：旧 control 镜像 ID（deployment.json
  `control_image`）、旧 provisioner 源码 commit（966d650）、
  `provisioner-expected.json`、原 control/keys/egress 卷、worker/
  gateway 容器 ID 与 attempt 文件。Update 只读核对并在失败时保留
  屏障，不删卷、不改 Key、不动 worker/gateway 实例。
- 回退点：旧镜像仍在本地，旧源码 worktree/commit 可重建旧 control；
  回退到**新项目/新目录/新端口**使用 Restore，不覆盖原实例。
- 更新后 Stop→Backup→Verify 产生的备份是**升级后**备份，不是升级前
  回退点。

## 更新失败时

- 屏障保留；不要 `-Force`、不要 `admin resume`、不要删卷或手改 DB。
- 记录 `update_barrier_retained=<id>` 与准确错误码，交回主审。
- 修复后可重跑同一条 Update 命令；`desktop-update` 屏障会被安全沿用。
