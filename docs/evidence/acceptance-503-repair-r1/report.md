# 验收实例 503 修复 R1：交付报告

日期：2026-11-02（Asia/Shanghai）

父基线：`966d6500964b0729008cc215e205a29116f32324`
分支：`fix/acceptance-503-attempt-recovery`
实施 worktree：`C:\Code\Easel\.claude\worktrees\acceptance-503-repair`

本轮未更新、未重启、未停止用户验收实例；未修改其 DB、attempt、Key、
Cookie 或容器；未执行真实模型/平台；未合并、未推送。

## 0. 四项关闭依据

| 项 | 处置 | 关闭依据 |
|---|---|---|
| R1-01 P1 接管前关闭清理承诺 | 交接 CAS 前禁止任何 attempt 收口；新增 `pending_attempt_id` 持久 fencing；`committed`/`completed` 状态拆分；空 attempt 一律拒绝 | `probe-pre-takeover.txt`；`test_attempt_ownership_503.py`；隔离旧→新恢复 |
| R1-02 P1 新 provisioner 不生效 | Start 身份加入实际源码摘要与动作能力；新增受控 `Update-WorkspaceEasel.ps1`：屏障→精确 PID 重启→新 control→核验→resume | `e2e-run.txt` 的复用复现、进程身份、首屏/聊天/Stop→Backup→Verify |
| R1-03 P2 Stop 模板参数 | `commands.md` 删除 `-WorkspaceRoot`；Stop GID 从登记身份读取；全部正式命令使用实际 Distro/GID 与显式执行策略；参数契约测试 | `probe-rollout-parameters.txt`；`test_desktop_command_contract.py` |
| R1-04 P2 commit 超预算 | commit 接收本次 deadline；剩余预算不足时延后到下一次 ready；接管恢复的 commit 失败关闭；缓存按 attempt | `probe-commit-budget.txt`；`test_runtime_gateway.py` 预算/重试用例 |

## 1. R1-01 接管与清理屏障

### 实现

- `worker_leases` schema v15 新增 `pending_attempt_id`。接管顺序改为：
  1. 先把已持久 fencing 的旧 attempt 按精确 ID 收口；
  2. `takeover_worker_gateway_refresh` 以旧 owner/generation/attempt +
     过期时间 CAS 完成交接，并把旧 attempt 持久写入 `pending_attempt_id`；
  3. 再按精确 ID 收口旧 attempt、清空槽位，之后才允许 staging 新候选。
- 交接 CAS 成功前，旧 owner 的 `begin_worker_abort` 仍可能成功；CAS 成功
  后旧 owner 必然失败。因此旧 cleanup 要么真实完成，要么被交接 fencing，
  绝不会被空 attempt 提前标成 completed。
- provisioner 状态拆分为 `pending`（清理承诺未完成）、`committed`
  （成功启动已收口）、`completed`（物理清理已完成）。`abort` 遇到
  `committed` 返回 `gateway_abort_superseded`，不再静默成功。
- 所有 lazy/ready/refresh/restore commit 调用点只传租约自己的精确
  attempt；`_commit_attempt` 对空 attempt 拒绝发送。

### 证据

主审探针原样运行（`probe-pre-takeover.txt`）：

```text
cleanup_reported_success=false
gateway_stop_count=0
database_state=starting
attempt_status=committed
TEMP_CLEANUP=ok; LIVE_RESOURCES_TOUCHED=false
```

旧实现同场景返回 `cleanup_reported_success=true` 且 gateway 未停止。
现在旧 owner 的 cleanup 在 B 接管后被 CAS fencing，返回 False；旧 attempt
已由 B 在交接后按精确 ID 收口为 committed，DB 停在 B 拥有的 starting。
正式回归 `test_restore_takeover_fences_old_cleanup_before_commit` 继续让 B
完成恢复，断言最终 ready、worker/gateway 未丢失、旧 attempt 不再可 abort。

## 2. R1-02 旧实例到新实例引导更新

### 复现

隔离环境先用 966d650 源码/旧 control 镜像启动，运行首屏与短聊天。随后
只把旧版 `Start-WorkspaceEasel.ps1` 的 `-WslRepo` 指向新源码再运行一次：

```json
{"pid_before":"21857","pid_after":"21857",
 "cwd_after":"/Code/Easel/.claude/worktrees/acceptance-503-r1-old",
 "returncode":0}
```

旧 process 的 PID 与 cwd 均未变化，证明“改了磁盘源码”不会让运行中的
provisioner 升级。旧 Start 随后被停止并只用于隔离验收，不作为交付路径。

### 实现

- Start 写入 `provisioner-expected.json` 时加入
  `provisionerSourceSha256`（WSL 源码文件 SHA-256）与
  `provisionerActions`；provisioner 在 import 时计算自身源码摘要并在
  `identity` 返回。配置相同但代码落后时身份核验报
  `provisionerSourceSha256/provisionerActions`。
- 新增 `-UpdateBarrierId`、`-RestartProvisionerIfStale`、
  `-LifecycleLockHeld`。重启只接受“配置一致、仅源码/动作能力落后”的
  情况，且必须已有匹配的 update 屏障；按精确 socket/`/proc` cmdline
  找到唯一 PID 后 SIGTERM，绝不按进程名泛杀。
- 新增 `Update-WorkspaceEasel.ps1`：读取 deployment.json 核对项目/镜像/
  目录；用旧 control 的 Store API 写 `desktop-update` 屏障；持有生命周期
  锁调用 Start（新版 control + 新版 provisioner）；核验运行中 control
  镜像、镜像内 runtime.py 摘要、provisioner 源码摘要/协议/动作、worker/
  gateway 实例集合、Key/policy 指纹、attempt 文件均未变化；最后显式
  `admin resume`。失败保留屏障，可重跑沿用 `desktop-update` 屏障。
- 保护活动 Run：屏障先于替换写入；worker/gateway/HOME/Key/attempt 不做
  删除或重建；不做 SQL 改 ready；不绕过 Stop。

### 证据（隔离项目 `easel-r1-3796fafc`）

旧版本首屏与聊天：

```text
R1_BEFORE first_screen={"/api/accounts":200,"/api/ideas":200,
  "/api/outputs":200,"/api/personas":200,"/api/schedule":200,
  "/api/status":200}
  chat={state:"done", assistant_has_marker:true, elapsed:4.175}
  provider_requests=2
```

复用复现（旧 Start + 新 WslRepo）：

```text
R1_REUSE_REPRO {"pid_before":"39664","pid_after":"39664",
 "cwd_after":"/Code/Easel/.claude/worktrees/acceptance-503-r1-old",
 "returncode":0}
```

构造现场同构坏状态（真实 Store，隔离项目）：

```text
R1_FIELD_SHAPE {"db":{"state":"stopping","scope":"gateway",
 "cleanup_gateway_created":1,"generation":2,
 "worker":"f7b7bd…","gateway":"abbd58…"},
 "attempt_file":"…/.gateway/attempt-c466956….json",
 "attempt_a":"6c72e9…","attempt_b":"7b7bbf…"}
```

正式 Update（退出码 0）：

```text
update_barrier=update-d9c9d715d1c6
update_start_exit=0
update_control_image=sha256:258288…->sha256:b8883040…
update_control_runtime_sha256=94305aea40b0e70c3d0aef5de4d132d7d1b06beeb0b433ff9000b26856be778b
update_provisioner_source=->049fd92256efdfc0a249d21885cb2ef23978f74ff26f35b0bd6cb8d388060ee9
update_provisioner_pid=provisioner_pid=39664->provisioner_pid=41883
update_managed_instances=6
update_attempt_records=1
update_barrier_resumed=update-d9c9d715d1c6
update_completed project=easel-r1-3796fafc
```

更新后恢复、首屏与聊天：

```text
R1_POST_UPDATE control_image=sha256:b8883040…,
 control_runtime_sha256=94305aea…,
 provisioner_source_sha256=049fd922…,
 provisioner_protocol=2,
 provisioner_actions=[ensure,stop,inspect,activate,abort,
   restore_gateway,commit_attempt,recover_abort,identity],
 barrier=barrier=none

R1_AFTER first_screen 6/6 200
 chat={state:"done", assistant_has_marker:true, elapsed:3.198}
 lease={state:"ready", generation:3,
   worker:"f7b7bd…" (与更新前相同),
   gateway:"970344…" (已替换),
   attempt:"ee566d…"}
 attempt_file={status:"committed"}
 provider_requests=3
```

正常 Stop → Backup → Verify：

```text
R1_STOP admission_barrier=stop-c6ba3702…
 drain_verified=settled
 stop_completed=stop-c6ba3702…
 stopped project=easel-r1-3796fafc drained=True

R1_BACKUP backup=…\backups\desktop-20261102-162054
 manifest_sha256=0f10f383…
 version=2 schema_version=15 workspaces=2 clean_stop=true

R1_VERIFY verified=/backup version=2 schema_version=15
 files=28 workspaces=2 clean_stop=true
 backup_manifest_sha256=0f10f383…
 verified_backup=…\backups\desktop-20261102-162054

R1_UPDATE_E2E_PASS
```

完整原始输出见 `e2e-run.txt`，机器可读摘要见 `e2e-result.json`。
更新前后 worker ID 相同、gateway 已替换、managed 实例集合与 attempt 文件
哈希未变、Key/policy 指纹未变；屏障只在全部核验后 resume。

## 3. R1-03 操作模板参数

- `commands.md` 的 Stop 模板不再传 `-WorkspaceRoot`；参数来自脚本实际
  `param()` 块：`ProjectName,Distro,WslRepo,DataRoot,LockTimeoutSeconds,
  Force`。
- Stop 的 GID 从 `provisioner-expected.json.socketGid` 读取，不假定
  1000；Start/Update 的 `-ProvisionerGid` 由现场 `wsl id -g` 读取。
- 所有命令使用 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File`；
  Update 通过 `Start-Process` 显式子进程启动 Start，避免当前窗口执行策略
  与长驻 provisioner 句柄问题。
- 明确回退点：更新前没有一致备份（成功 Stop 被现场坏状态阻断）；保护
  材料为旧镜像 ID、旧源码 commit、登记身份、原卷、worker/gateway 实例与
  attempt；更新后的 Backup 是升级后备份，不是升级前回退点。

证据：`probe-rollout-parameters.txt` 输出 Stop 参数表且
`report_stop_workspace_root_supported=false`；
`test_desktop_command_contract.py` 解析 Start/Stop/Update/Backup/Verify/
Restore 参数表和 `commands.md` 全部正式命令，任何未支持参数都会失败。

## 4. R1-04 就绪预算

- `_commit_attempt` 接收本次 ready deadline。普通成功后收口最多使用
  剩余预算；剩余低于 20ms 时直接延后到下一次 ready 重试，不额外占用
  15 秒，也不把已提交 ready 改成失败。
- 接管恢复的 commit 使用 `required=True`：旧 owner 已被 CAS 剥夺 abort
  权，收口失败必须失败关闭，保持 `pending_attempt_id`，后续 owner 可按
  持久 attempt 恢复。
- commit 成功缓存只按精确 attempt，不按物理 worker/gateway 目标；避免同一
  gateway 被新 attempt 复用时跳过收口。

证据：`probe-commit-budget.txt`（0.02 秒预算，0.0 秒返回、success）；
`test_ready_commit_uses_remaining_budget_and_retries_later`；
`test_takeover_commit_failure_keeps_fence_and_later_owner_recovers`；
`test_commit_cache_is_per_attempt_not_physical_target`。

## 5. 回归

- `pytest tests/multiuser`：1774 passed, 7 skipped（见
  `full-multiuser.txt`）。
- 主审原分组 runtime 125 passed/1 skipped、provisioner 189 passed/
  1 skipped（本轮中途分组运行；最终以全量文件为准）。
- 前端本轮未改；沿用首轮 193 Node tests、tsc/vite build、oxlint 通过
  的结论，不重复运行已关闭页面矩阵。
- `git diff --check` 通过。

## 6. 安全边界与未执行

- 未操作用户实例 `C:\Easel\acceptance-data` /
  `C:\Easel\acceptance-workspaces`；未动用户 bf8e… 工作区容器。
- 未执行真实供应商/平台请求；隔离验收使用受控 TLS fake provider。
- 未执行真实用户 prompt、未读取/写入用户 Key/Cookie。
- 未合并、未推送、未自动更新验收实例。
- 真实 Linux 生产部署、旧于 966d650 的 control/provisioner 组合未在本轮
  验证；Update 对缺少 barrier CLI 的旧版本失败关闭，需人工评估。
