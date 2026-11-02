# R2 正式命令模板与重试边界

以下命令均在**新版源码根目录**执行，使用当前窗口执行策略
`-NoProfile -ExecutionPolicy Bypass`。`Distro` 与 `ProvisionerGid` 必须
来自现场实际登记/读取（`wsl -d <distro> -- id -g`），不得沿用占位值。
`<...>` 只表示需要在现场替换的值。

## 受控更新（旧 control + 旧 provisioner → 新版两端）

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File deploy/desktop/Update-WorkspaceEasel.ps1 -PublicOrigin <origin> -Image <new-control-image> -WorkerImage <worker-image> -GatewayImage <gateway-image> -ProjectName <project> -Port <port> -Distro <distro> -WslRepo <wsl-repo> -WorkspaceRoot <workspace-root> -DataRoot <data-root> -ProvisionerGid <gid>
```

- 正常阶段：要求旧 control 正在运行且镜像等于 deployment、旧
  provisioner identity/PID 可读；随后创建设置
  `reason=desktop-update` 的接纳屏障。
- 屏障建立后，Update 先等待已接纳 Run 与在途请求归零（有界
  `-TimeoutSeconds`）；超时报 `update_active_work_timeout`，保留屏障，
  不替换进程/服务。静态坏 worker lease 不参与等待。
- 恢复阶段：控制库已有 `reason=desktop-update` 的 `update-*` 屏障时，
  允许 control 尚未替换、已替换为目标镜像或暂不可用，也允许旧
  provisioner 已停止；不要求旧 identity/PID。只接受本项目 deployment
  与目标/旧镜像，其他镜像失败关闭。

## 失败后的重试

- 失败输出会包含 `update_barrier_retained=<id>`。**修复原因后直接重跑
  同一条 Update 命令**；脚本会沿用该 `desktop-update` 屏障进入恢复阶段。
- 禁止手工 `admin resume`、SQL 改状态、删卷、按进程名泛杀或放宽所有
  镜像/PID 校验来“修好”更新。若控制库完全不可读，Update 报
  `update_control_facts_unavailable`，应先恢复控制面而不是绕过。
- 若 Start 后核验失败（例如本项目 worker/gateway 集合变化），屏障保留；
  确认并恢复实例后重跑同一条命令。

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

## 回退边界（前向迁移）

- 新 control 可能把控制库迁移到 schema v15；旧镜像不保证能重新打开已
  迁移的库。本轮**不声称旧镜像回退已验证**，也不把旧镜像存在当作回退
  能力。
- 需要回退时，只能使用旧源码/旧镜像恢复到**新项目/新目录/新端口**，
  不覆盖原实例；恢复前先备份当前新实例。
- 更新前的保护材料：旧 control 镜像 ID、旧源码 commit（4382180）、
  `provisioner-expected.json`、control/keys/egress 卷、worker/gateway
  容器 ID 与 attempt 文件。Update 只读核对；失败保留屏障，不删卷、不改
  Key、不动 worker/gateway 实例。
- 更新后 Stop→Backup→Verify 产生的备份是**升级后**备份，不是升级前
  回退点。
