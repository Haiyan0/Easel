# 验收实例 503 修复 R2：交付报告

日期：2026-11-02（Asia/Shanghai）

父基线：`43e4cd01bee0421226a193d54f6d4e09f045ff9e`
分支：`fix/acceptance-503-attempt-recovery`
实施 worktree：`C:\Code\Easel\.claude\worktrees\acceptance-503-repair`

本轮提交：

| 提交 | 内容 |
|---|---|
| `a2f8ed1` | R2-01 普通首屏 refresh 复用恢复接管屏障 |
| `528e41c` | R2-02 按精确 attempt 恢复丢失的 gateway 响应 |
| `5f5be50` | R2-03/04/05 更新活动工作、部分失败重试与项目快照 |

本轮未更新、未重启、未停止用户验收实例；未修改其 DB、attempt、Key、
Cookie 或容器；未执行真实模型/平台；未合并、未推送。

## 0. 五项关闭依据

| 项 | 处置 | 关闭依据 |
|---|---|---|
| R2-01 P1 普通首屏接管前提交旧 attempt | `ensure_ready` refresh 的过期 starting 与 restore 共用 `_takeover_for_restore`：先 owner/generation/attempt CAS，再按精确 attempt 收口 | `probe-r1-ensure-takeover.txt`；`test_ensure_ready_*` 四例；真实隔离更新链路 |
| R2-02 P1 替换响应丢失无法恢复 | provisioner 持久化 attempt 的 config/gateway 身份；`commit_attempt` 增加受限 resolve；runtime 在 `gateway_commit_superseded` 时取回真实候选、CAS adopt 后直接接管 | `probe-r1-lost-response.txt`（原探针）；`probe-r1-lost-response-once-variant.txt`；`test_lost_*` / `test_repeated_*` |
| R2-03 P1 Update 不保护活动 Run | 屏障建立后、Start 前用 `update-status` 事实有界等待 `activeRuns`/`activeRequests`；超时保留屏障、绝不替换 | `test_update_waits_for_active_run_and_does_not_start`；`test_admin_update_status_*` |
| R2-04 P1 部分更新失败不能重跑 | 先读控制库事实识别 `desktop-update` 未完成阶段；恢复阶段允许目标 control/停止的旧 provisioner，跳过旧 identity/PID；Start 后核验失败保留屏障，同命令重跑且沿用首次 managed 基线 | `test_update_resumes_*`；`test_update_failed_post_verification_can_rerun_same_command`（漂移持续仍拒绝）；真实隔离更新链路 |
| R2-05 P2 快照包含其他部署 | 快照只查询本项目控制库 workspaceIds（或本项目持久根目录），按 `easel.workspace-id` 过滤；重跑沿用首次基线，不重新打基线 | `test_update_ignores_other_project_instance_changes`；`test_update_snapshot_is_scoped_to_project_workspaces`；`test_update_failed_post_verification_can_rerun_same_command`；`git diff --check` |

## 1. R2-01 普通首屏接管屏障

### 实现

- `_refresh_gateway` 遇到 `starting` 租约不再先提交旧 attempt，而是调用
  `_takeover_for_restore`：
  1. 过期 starting 的 owner/generation/attempt + 过期时间 CAS 交给本
     manager，并把旧 attempt 持久写入 `pending_attempt_id`；
  2. 新 owner 按精确 attempt 收口旧 attempt，旧 owner 的
     `begin_worker_abort` 随即 CAS 失败；
  3. 之后才允许发起新的 ensure/staging。
- 非 starting 的 ready refresh 仍按原顺序收口租约自己的精确 attempt；
  `ready/draining` 上出现 pending 槽位失败关闭。
- 远程失败回退 `ready/unavailable`，取消保留可接管的 `starting`，不会
  留下 `stopping` 503。

### 证据

主审探针原样运行（`probe-r1-ensure-takeover.txt`）：

```text
{"before_owner": "owner-b", "cleanup_reported_success": false,
 "cleanup_error": "", "gateway_stop_count": 0,
 "database_state": "starting", "attempt_status": "committed"}
TEMP_CLEANUP=ok; LIVE_RESOURCES_TOUCHED=false
```

旧 owner 的清理 CAS 失败且没有误报物理停止；DB 停在 B 拥有的 starting，
随后由 B 完成 ready。正式回归覆盖接管赢、旧清理先赢、远程失败、取消后
再次接管四种窗口（`test_ensure_ready_*`）。

## 2. R2-02 替换成功但响应丢失

### 实现

- provisioner 的 attempt 记录新增 `config_version`、
  `gateway_generation`、`gateway_upstream`；旧记录缺字段时按网关 inspect
  回退，不把缺失字段当成匹配。
- `commit_attempt` 增加受限 `resolve=True` 只读模式：按精确 attempt 和
  worker 返回 provisioner 已持久候选，复核 workspace/worker/config/
  gateway 归属；不创建、不删除资源。
- runtime 在精确 commit 返回 `gateway_commit_superseded` 时：
  1. 按 pending attempt 取回真实候选；
  2. `adopt_fenced_attempt` 以 owner/generation/attempt/pending 全匹配
     CAS 写入 DB；
  3. 按精确 attempt 收口旧记录并清空 fencing 槽位；
  4. 直接以该候选 staging/激活，不再发起冗余 restore。
- 已完成/纯复用 tombstone 不被当作可接管候选；已清理记录不会把已消失的
  gateway 写回租约。

### 证据

原主审探针（`probe-r1-lost-response.txt`）在修复后第一次恢复已成功：
候选 F 被 adopt/commit、DB 转 ready。第二次恢复调用探针 Adapter 的
`restore_gateway`；该 Adapter 每次固定抛 `CancelledError`，而探针只捕获
`Exception`，因此 CancelledError 逃逸、探针在打印 JSON 前退出。这是探针
Adapter 的固有限制，不是恢复失败。原探针输出本身只有该 traceback；
最终终态证据来自下述变体与正式测试，不把原探针输出当作终态证明。

只丢失第一次响应的展示变体（文件头注明为变体，原探针未修改）得到：

```text
{"lost_response_target_mismatch": true, "recovery_errors": [],
 "database_state": "ready", "pending_attempt_present": false,
 "attempt_status": "completed"}
TEMP_CLEANUP=ok; LIVE_RESOURCES_TOUCHED=false
```

`lost_response_target_mismatch=true` 是恢复前观测到的原始 bug 状态；恢复后
无 `gateway_commit_superseded` 循环、DB ready、无 pending 槽位。

正式回归覆盖：restore 入口、ensure_ready 入口、连续恢复中断、正常 Stop、
未替换纯复用、resolve 对 worker/config/gateway 错配的拒绝；另验证
`active=true, available=false` 的 gateway 只能返回 staged，强制重新
activate 与完整复核（`test_resolve_attempt_requires_available_before_skipping_activation`）。

## 3. R2-03 Update 的活动工作边界

- 新增只读 `easel admin update-status`：输出屏障、`activeRuns`、
  `activeRequests`、`activeLeaseCount`、`workspaceIds`。
- Update 在屏障建立/恢复确认后、Start 前调用 `Wait-UpdateWorkload`，
  每 2 秒读取事实，直到 `activeRuns == 0 && activeRequests == 0`；
  超过 `-TimeoutSeconds` 报 `update_active_work_timeout:<runs>/<requests>`
  并保留屏障，绝不进入 Start/compose。
- 只等待活动 Run 与在途请求；`activeLeaseCount` 仅输出诊断，静态坏
  lease 交给 Start 的恢复路径，避免现场 drain 死锁。控制容器不在时仍用
  只读 SQLite 事实继续等待；offline 事实也不可读则失败关闭，绝不以
  “不可观察”为由在存在活动工作时替换服务。

证据（真实 PowerShell 控制流 + 临时 Docker/WSL 桩）：

```text
update_active_work=1/0
update_active_work_timeout:1/0
update_barrier_retained=update-...
```

断言未调用 `compose up`、未进入 Start（`test_update_waits_for_active_run_and_does_not_start`）。
控制容器不在时仍用 offline facts 等待；有活动工作时同样拒绝并保留屏障
（`test_update_rejects_active_work_when_control_container_is_absent`）。

## 4. R2-04 部分更新失败的安全重试

- Update 先通过运行中控制的 Store（旧镜像也可）读取事实；旧镜像没有
  `update-status` 时退回只读 SQLite（目标镜像执行），再决定阶段。
- 发现 `reason=desktop-update`、id 形如 `update-xxxxxxxxxxxx` 的屏障时
  进入恢复阶段：接受 control 尚未替换、已替换为目标镜像、或暂不可用；
  不再要求旧 control 镜像等于 deployment，也不再要求旧 provisioner
  identity/PID 可读。其他屏障/未知镜像/项目身份不符一律失败关闭。
- 正常阶段仍要求旧 control 运行且镜像匹配 deployment、旧 provisioner
  身份与 PID 可读，然后创建本轮屏障。
- Start 后任一步核验失败都保留屏障；同一 Update 命令再次运行沿用该屏障，
  修复后完成恢复并 resume。不提供手工 resume、SQL 改状态、删卷、泛杀或
  无条件接受任意镜像/PID 的路径。

证据：

- `test_update_resumes_replaced_control_without_old_identity`：目标 control
  已替换、deployment 尚旧、屏障存在 → 重跑完成。
- `test_update_resume_allows_stopped_old_provisioner`：旧 identity/PID
  不可读、屏障存在 → 重跑仍进入 Start。
- `test_update_failed_post_verification_can_rerun_same_command`：Start 成功后
  快照变化导致核验失败、屏障保留；第二次同命令恢复阶段完成。
- 真实隔离链路（见 §6）覆盖正常旧→新更新，以及旧 provisioner 已停、
  新进程未起的同命令重试（§6.3）。

未覆盖边界：control 容器完全不存在且只读 SQLite 也无法读取时，Update
报 `update_control_facts_unavailable` 失败关闭，需先修复 control；
不会盲目新建屏障。只读 SQLite 回退已在“WAL 仍有活动写者 + 只读挂载”
下手工验证（`offline-facts-check.txt`）；未在真实 control 崩溃现场注入。

## 5. R2-05 本项目实例快照

- `Get-ManagedSnapshot -Root <WorkspaceRoot> -WorkspaceIds <ids>` 只对
  本项目控制库登记的 workspace 逐个查询
  `label=easel.managed=true,label=easel.workspace-id=<id>`；控制库不可读时
  只枚举本项目持久根下的 32 位十六进制 workspace 目录。不再使用全机
  `label=easel.managed=true` 集合。
- 其他部署正常增删 worker/gateway 不进入本项目快照；本项目实例被替换
  仍会触发 `update_managed_instances_changed`。
- 重跑不重新打基线：首次 managed 快照在屏障建立后写入
  `update-baseline-<barrierId>.json`；恢复阶段沿用该文件，漂移持续时
  第二次仍拒绝，只有恢复原实例后才完成。缺失首次基线的恢复阶段失败
  关闭（`update_baseline_missing`）。
- attempt 文件快照仍扫描本项目 `$WorkspaceRoot\.gateway`；部署模型假定
  WorkspaceRoot 为本项目独占，共享根不在支持范围。
- `control-build.txt` 六处尾随空白已清理，内容与来源不变；
  `git diff --check` 在 `966d650..HEAD` 与工作树均通过。

证据：`test_update_ignores_other_project_instance_changes` 断言实际
`docker ps` 查询只带本项目 workspace ID；`test_update_snapshot_is_scoped_to_project_workspaces`
锁定脚本契约；`test_control_build_evidence_has_no_trailing_whitespace`。

## 6. 隔离真实资源验收

使用用户实际旧版本组合 `4382180`（源码 `C:\Code\Easel-acceptance`、
旧 control/worker/gateway 镜像）与最终新版 control 镜像
`easel-control:503-r2-final`，随机项目 `easel-r2-15aacce1`、随机端口、
随机数据/工作区目录；不访问用户 `easel` 实例。受控 TLS fake provider
只在本隔离网络内。本节数据来自复审修复后的最终代码/镜像重跑。

### 6.1 正常旧→新更新（`run_e2e.py`，退出 0）

- 旧部署首屏 6/6 200，短聊天 `state=done`、`assistant_has_marker=true`。
- 复用复现：仅把旧 Start 的 `WslRepo` 指向新源码，旧 provisioner
  PID `55704` 与 cwd `/Code/Easel-acceptance` 均未变化，证明“改磁盘源码”
  不会升级运行中的旧进程。
- 构造现场同构坏状态：真实旧 Store 把租约置为 `stopping/gateway`，
  新 generation 2；磁盘写入旧版无资源 attempt 记录。
- 正式 Update 退出 0：

```text
update_managed_baseline=fresh
update_active_work=0/0
update_barrier=update-091a52043dc9
update_start_exit=0
update_control_image=sha256:9272b556…->sha256:fabe6b75…
update_control_runtime_sha256=73702e19626fcd56159af65de4cdb0f95fa8eb30743afabbc3a26e6f1164b7ab
update_provisioner_source=->f229ec30e619030fe0082885d1006c36cc24b1853cc3b85a2dacb755b1042103
update_provisioner_pid=provisioner_pid=55704->provisioner_pid=57912
update_managed_instances=3
update_attempt_records=1
update_barrier_resumed=update-091a52043dc9
update_completed project=easel-r2-15aacce1
```

- 首次 managed 基线（3 个实例）写入
  `update-baseline-update-091a52043dc9.json`；post-check 与基线一致。
- 更新后：control 镜像/源码摘要匹配新镜像；provisioner protocol 2、
  源码摘要 `f229ec30…`、actions 完整；屏障 `barrier=none`。
- 恢复与业务：worker ID `04acc485…` 与更新前相同；gateway 由
  `dbfa1ef2…` 替换为 `29c7b8c0…`；attempt 记录 `committed`；首屏
  6/6 200；短聊天 `state=done`、marker 存在；provider 请求 2→3。
- 正常 Stop（`drain_verified=settled`）→ Backup →
  Verify：备份 `schema_version=15`、`clean_stop=true`、28 文件、
  2 workspaces；manifest `1a02561c…`。完整输出见 `e2e-run.txt`、
  机器摘要见 `e2e-result.json`。
- 清理：随机项目容器/卷/网络已删除；遗留目录已手工删除。
  未操作用户实例、Key、Cookie、DB、attempt。

### 6.2 故障重试边界

- 旧 provisioner 已停、新进程未起的真实注入：见 §6.3（`run_fault_retry.py`）。
- 新 control 已替换但健康失败、deployment 尚旧：由真实 PowerShell 控制流
  桩测试覆盖（resume 阶段接受目标镜像、跳过旧 identity/PID），本轮未对
  真实容器注入健康故障。
- 成功 Start 后核验失败：由真实 PowerShell 控制流桩测试覆盖（快照变化 →
  屏障保留 → 同命令重跑完成），本轮未对真实容器制造快照漂移。
- 活动 Run 期间不替换：由真实 PowerShell 控制流桩测试覆盖；真实隔离
  链路只验证了无活动工作时正常更新。未在真实长流上注入，避免占用用户
  机器与真实模型。

### 6.3 真实故障重试（`run_fault_retry.py`）

随机项目 `easel-r2f-beb9954d`：先用 4382180 旧版本启动并完成首屏/聊天；
用旧 control 的真实 Store 写入 `update-abcdef123456`（reason
`desktop-update`，模拟首次 Update 已建立屏障），按真实更新格式写入
3 个实例的 managed 基线，再只按精确 socket/PID 停止旧 provisioner
（PID 59918，确认 `pid=""`）。随后运行同一条 Update：

> 说明：屏障与基线写入属于故障注入夹具，不是恢复手段；恢复本身只执行
> 正式 Update 命令，没有手工 resume、SQL 改 ready、删卷或泛杀。

```text
update_resumed_phase=update-abcdef123456
update_resume_control_image=sha256:9272b556…（旧镜像，允许）
update_managed_baseline=existing
update_active_work=0/0
update_start_exit=0
update_provisioner_pid=unavailable->provisioner_pid=61274
update_control_image=sha256:9272b556…->sha256:fabe6b75…
update_control_runtime_sha256=73702e19…
update_provisioner_source=->f229ec30…
update_barrier_resumed=update-abcdef123456
update_completed project=easel-r2f-beb9954d
R2F_AFTER first_screen 6/6 200
 chat={state:"done", assistant_has_marker:true}
 provisioner={pid:"61274", cwd:"/mnt/c/Code/Easel/.claude/worktrees/acceptance-503-repair"}
R2F_FAULT_RETRY_PASS
R2F_STOP drain_verified=settled
```

恢复阶段没有要求旧 identity/PID（输出 `unavailable`），沿用首次 managed
基线（3 个实例，post-check 一致），Start 启动了新 provisioner
（PID 61274、cwd 新源码），首屏/聊天仍通过，最后正常 Stop。
项目容器/卷/网络/目录已清理。原始输出见 `fault-retry-run.txt`。

未对真实容器注入“新 control 健康失败”和“Start 后核验失败”两种状态；
其脚本控制流由 §7 的 PowerShell 桩测试覆盖。


## 7. 回归与探针

- `pytest tests/multiuser`：1796 passed, 7 skipped。
- desktop/update 专项：`test_desktop_update_r2.py` 10 passed；连
  `test_desktop_command_contract.py` 共 13 passed。
- 本轮新增红绿：
  - `tests/multiuser/test_attempt_ownership_503.py`（32 passed）；
  - `tests/multiuser/test_stop_barrier.py`（12 passed）；
  - `tests/multiuser/test_desktop_update_r2.py`（10 passed）。
- 主审原三条探针原样运行：`probe_pre_takeover_commit.py`、`probe_commit_budget.py`、
  `probe_rollout_parameters.py`，输出见同目录同名 `-*.txt`。
- 本轮三条：ensure/update barrier 原样通过；lost-response 原探针结果与
  变体结果见 §2。
- `probe_attempt_overwrite.py` 在 R1 基线 `43e4cd0` 已因同一失败关闭逻辑
  报 `gateway_cleanup_pending`，不属于本轮三条，未作为回归门槛。
- 前端本轮未改；沿用 R1 首轮 193 Node tests、tsc/vite build、oxlint
  通过的结论，不重复已关闭页面矩阵。

## 8. 独立整分支复审修复

独立整分支复审（fresh reviewer，范围 `43e4cd0..7c57777`）确认并已修复
三项：

- **Critical：控制容器不在时跳过 offline 活动工作事实。** 修复后控制
  容器不在时仍用只读 SQLite 事实做有界等待；offline 也不可读则
  `update_work_status_unavailable` 失败关闭。新增
  `test_update_rejects_active_work_when_control_container_is_absent`
  （RED→GREEN）。
- **Important 1：resolve 用 `active` 而非 `available` 判定可接管。**
  修复后只有 `available is True` 才返回 available，否则返回 staged 强制
  重新 activate。新增
  `test_resolve_attempt_requires_available_before_skipping_activation`
  （RED→GREEN）。
- **Important 2：重跑重新基线化 managed 快照，持续漂移可被掩盖。**
  首次 managed 快照在屏障建立后写入
  `update-baseline-<barrierId>.json`；恢复阶段沿用该基线，缺失时
  `update_baseline_missing` 失败关闭。新增三阶段测试
  `test_update_failed_post_verification_can_rerun_same_command`
  （首次失败 → 漂移持续仍失败 → 恢复原实例后完成）。

复审其余 Minor 的处置：

- 报告与证据口径不一致：已修正并在本轮提交。
- `Provisioner` 协议缺少 `resolve/config_version`：已同步类型声明。
- offline facts 回退：新增的 control-absent 测试覆盖 offline 分支；
  手工证据仍用 bind mount + root，交付代码的 volume + 10001 未单独注入。
- 候选消失后的永久 fencing：失败关闭行为保留，人工核验边界已写入 §9。
- 原 lost-response 探针输出归属：§2 已明确终态证据来自变体与正式测试。
- `Get-AttemptSnapshot` 项目范围：按 WorkspaceRoot 独占前提记录。
- 单次 Docker 事实读取超时：记录为未注入边界（§9）。

复审 “Declined to judge” 各行按任务边界保留：前端未改、不调用真实平台、
不声称旧镜像回退、不手工 resume/SQL/删卷/泛杀、Start 进程逻辑未改、
WorkspaceRoot 按项目独占、旧屏障不绑定 target digest、未登记 workspace
不在快照范围、原探针 Adapter 限制如实记录、更远版本组合不验证。

## 9. 安全边界、未执行与回退

- 未操作用户实例 `C:\Easel\acceptance-data` /
  `C:\Easel\acceptance-workspaces`；未动用户 bf8e… 工作区容器。
- 未执行真实供应商/平台请求；隔离验收使用受控 TLS fake provider。
- 未执行真实用户 prompt、未读取/写入用户 Key/Cookie。
- 未合并、未推送、未自动更新验收实例。
- 前向迁移：新 control 可能把控制库 schema 迁移到 v15；旧镜像不保证能
  打开已迁移库。本轮不声称“旧镜像回退已验证”，也不把旧镜像存在当作
  回退能力。需要回退时只能从旧源码/旧镜像恢复到**新项目/新目录/新端口**，
  不覆盖原实例。
- 真实 Linux 生产部署、其他旧版本组合未在本轮验证；Update 对缺少
  barrier/identity 能力的旧版本失败关闭。
- 候选消失的边界：若丢失响应窗口内记录中的 gateway 已被删除或
  inspect 不符，resolve 失败关闭（`gateway_recovery_target_mismatch` /
  503），不会猜测清理；需要人工核验精确实例与 attempt，未提供自动
  “标记 completed”的清理路径。
- 单次 Docker 事实读取没有独立进程级超时；`Wait-UpdateWorkload` 在
  轮询之间检查 deadline。控制容器挂起且 docker 调用本身不返回时，
  有界等待可能被单次调用拖长；该场景未在本轮注入。
