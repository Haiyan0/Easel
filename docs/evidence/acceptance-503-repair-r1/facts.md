# R1 事实与命令（交付报告引用）

## 基线

- 父基线：`966d6500964b0729008cc215e205a29116f32324`（分支
  `fix/acceptance-503-attempt-recovery`）
- 实施 worktree：`C:\Code\Easel\.claude\worktrees\acceptance-503-repair`
- 旧版本对照 worktree：`C:\Code\Easel\.claude\worktrees\acceptance-503-r1-old`
  （`git worktree add --detach <path> 966d650`）

## 镜像与源码摘要

| 项 | 值 |
|---|---|
| 旧 control 镜像 | `sha256:2582886195031ac4d71746bbddf670270e47f21f09a2d0bf55d26d1d00011ac2` |
| 新 control 镜像 | `sha256:b8883040b8b23f7097a399f6aa5a69e41e482e3653a14ef962e24a6f6ccedba3` |
| 新镜像内 `runtime.py` SHA-256 | `94305aea40b0e70c3d0aef5de4d132d7d1b06beeb0b433ff9000b26856be778b` |
| 新源码 `runtime.py` SHA-256 | `94305aea40b0e70c3d0aef5de4d132d7d1b06beeb0b433ff9000b26856be778b` |
| 新源码 `provisioner.py` SHA-256 | `049fd92256efdfc0a249d21885cb2ef23978f74ff26f35b0bd6cb8d388060ee9` |
| worker 镜像 | `sha256:6f62af472c8b272c0d67a1aec26284cff8f8c9adacfaad48e6ba0ce64fbf0612` |
| gateway 镜像 | `sha256:0d30939d375c1e3bef616678f5e8d7b6fad687143817f7d6183579ca8e881f0a` |

## 正式命令

见同目录 `commands.md`。所有命令使用
`powershell.exe -NoProfile -ExecutionPolicy Bypass -File`；Stop 不传
`-WorkspaceRoot`；Distro/GID 由现场读取（本次隔离环境
`wsl -d Ubuntu-26.04 -- id -g` = 1000，但命令不假定该值）。

## 主审探针（原样运行）

- `probe_pre_takeover_commit.py`：`cleanup_reported_success=false`、
  `gateway_stop_count=0`、`database_state=starting`、
  `attempt_status=committed`。旧实现在同一场景返回
  `cleanup_reported_success=true` 且不停止 gateway；现在旧 owner 的
  abort 在交接后必然 CAS 失败，不再假报完成。
- `probe_commit_budget.py`：`ready_budget_seconds=0.02`，
  `elapsed_seconds=0.0`、`outcome=success`。原因：剩余预算低于
  20ms 时非必要 commit 延后到下一次 ready 重试，不再额外占满 15 秒；
  `test_ready_commit_uses_remaining_budget_and_retries_later` 覆盖了
  预算足够时实际等待受 deadline 约束、以及后续重试成功的路径。
- `probe_rollout_parameters.py`：Stop 参数为
  `ProjectName,Distro,WslRepo,DataRoot,LockTimeoutSeconds,Force`，
  `report_stop_workspace_root_supported=false`。

## 隔离旧→新更新结果

- 项目：`easel-r1-3796fafc`；随机端口/目录/卷，结束已清理。
- 旧 control：`sha256:2582886195031ac4d71746bbddf670270e47f21f09a2d0bf55d26d1d00011ac2`
- 新 control：`sha256:b8883040b8b23f7097a399f6aa5a69e41e482e3653a14ef962e24a6f6ccedba3`
- 旧 provisioner PID `39664`（cwd 旧源码）→ 新 PID `41883`（源码摘要
  `049fd922…`）；旧 Start + 新 WslRepo 复用时 PID/cwd 不变。
- 首屏 6/6 200、短聊天 done（marker 命中）；更新后再次 6/6 + done；
  provider_requests 2→3。
- worker `f7b7bd…` 前后相同；gateway `abbd58…`→`970344…` 替换；
  managed 实例数 6、attempt 记录数 1 前后不变；屏障
  `update-d9c9d715d1c6` 仅在核验后 resume。
- Stop：`drain_verified=settled`、`stop_completed=stop-c6ba3702…`；
  Backup：version 2、schema 15、clean_stop=true；
  Verify：files=28、clean_stop=true；`R1_UPDATE_E2E_PASS`。

## 本轮新增回归

- `tests/multiuser/test_attempt_ownership_503.py`：commit/abort 状态拆分、
  空 attempt 禁止收口、接管 CAS 持久 `pending_attempt_id`、接管窗口内旧
  cleanup 不得假报成功、接管 commit 失败保持 fencing 后可恢复。
- `tests/multiuser/test_runtime_gateway.py`：空 attempt 不发送 commit、
  commit 预算与重试、commit 缓存按 attempt 而非物理目标。
- `tests/multiuser/test_provisioner_identity_contract.py`：旧进程配置
  相同但源码/动作能力落后时身份核验必须报漂移。
- `tests/multiuser/test_desktop_command_contract.py`：Start/Stop/Update/
  Backup/Verify/Restore 参数表与 `commands.md` 正式命令契约一致。

## Not run / 安全边界

- 未更新、未重启、未停止用户验收实例
  `C:\Easel\acceptance-data` / `C:\Easel\acceptance-workspaces`；
  未修改其 DB、attempt、Key、Cookie 或容器。
- 未执行真实模型/平台、未重放用户 prompt。
- 未合并、未推送、未自动更新验收实例。
- 所有真实容器验收使用随机项目/目录/卷和受控 TLS fake provider；
  结束后清理临时资源（用户 bf8e… 工作区未触碰）。
