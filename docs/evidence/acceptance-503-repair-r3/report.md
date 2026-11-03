# 验收实例 503 修复 R3：交付报告

日期：2026-11-02（Asia/Shanghai）

父基线：`c9c0f0a`（R2 最终 tip）
分支：`fix/acceptance-503-r3`
实施 worktree：`C:\Code\Easel-acceptance`

本轮提交：

| 提交 | 内容 |
|---|---|
| `7e09f25` | R3-01 保留未知 ensure 结果的精确 attempt 归属 |
| `8d7a983` | R3-02 将更新屏障检查并入请求租约登记事务 |
| `1b7959c` | R3 独立复审修复：messages 请求接纳边界、未知结果立即恢复、连接失败分类 |

本轮未更新、未重启、未停止用户验收实例；未修改其 DB、attempt、Key、
Cookie 或容器；未执行真实模型/平台；未合并、未推送。未修改
`deploy/desktop/Update-WorkspaceEasel.ps1`（`c9c0f0a..HEAD` 该文件 diff 为
0 行），因此未复跑容器级更新流程，沿用 4382180 已通过的正常更新/故障
重试证据。

## 1. R3-01 普通网络错误的未知结果

### 根因

`_refresh_gateway` 在过期 `starting` 接管后发起远程 ensure；provisioner
已替换 G2 并持久 `pending`，但 control 等待回复超时。旧的异常分支把
`starting` 回退为 `ready/unavailable`，同时清空 fencing 槽位。下一次首屏
从 `ready` 走 refresh，只按当前 G1 收口；新 ensure 被 G2 的 pending 记录
挡住，连续返回 `gateway_cleanup_pending`，永远不进入精确 attempt 解析。

### 实现

- 新增 `_UNKNOWN_ENSURE_OUTCOME_CODES`：`provisioner_timeout`、
  `provisioner_connection_lost`、`provisioner_invalid_response`、
  `worker_start_timeout`。覆盖 Unix 客户端的读超时、连接建立后的
  写/读 OSError（含 EOF 前的连接重置）、EOF/不可信响应和 control 侧
  等待超时。
- `UnixProvisionerClient._call` 区分连接阶段与已发出后的失败：连接阶段
  OSError 仍是 `provisioner_unavailable`（确定失败，无请求发出）；
  连接建立后的 OSError 映射为 `provisioner_connection_lost`（未知结果）。
- `_refresh_gateway` 在 **starting** 租约、且 ensure 已实际发起时遇到这些
  错误，保留 `starting` 的 owner/generation/attempt 归属，并把租约期限
  缩短到当前时刻（`expire_worker_gateway_refresh`），使下一次首屏可立即
  接管恢复，而不是空等满刷新租约；随后直接上抛，不再回退 `ready`。
  下一次首屏按原有 `_takeover_for_restore` → `_resolve_fenced_attempt` →
  `_recover_lost_attempt` 路径按精确 attempt 解析 provisioner 持久候选，
  adopt/commit 后转 ready。
- 确定的失败（如 `worker_start_failed`、`worker_not_ready`、
  `gateway_cleanup_pending`、连接阶段 `provisioner_unavailable`）仍按原
  逻辑回退 `ready/unavailable`；`CancelledError` 原路径不变；不把未确认
  结果标 `completed`，也不绕过 attempt/worker/config 校验。

### 证据

主审探针修复前（`red-probe-r2-lost-ensure-error.txt`，退出 0）：

```text
after_failure={"state":"ready","gateway_is_old":true,"pending_attempt_id":""}
reopened_retry_errors=["gateway_cleanup_pending","gateway_cleanup_pending"]
resolve_calls=0
```

修复后原样复跑（`green-probe-r2-lost-ensure-error.txt`，退出 1，探针的
红态断言不再成立）：

```text
after_failure={"state":"starting","gateway_is_old":true,"pending_attempt_id":""}
reopened_retry_errors=[null,"provisioner_timeout"]
reopened_states=[{"state":"ready","gateway_is_old":false,"pending_attempt_id":""}, ...]
attempt_status=pending
attempt_gateway_is_replacement=true
physical_gateway_ensure_calls=2
resolve_calls=1
```

第一次重开重试已恢复（错误为 `null`），`resolve_calls=1`，DB 指向替换后
G2。第二次重试的 `provisioner_timeout` 来自探针 Adapter 的 `ensure`
**无条件**抛超时；恢复已完成后的普通 refresh 再次调用该假 ensure 才失败，
不是旧的 `gateway_cleanup_pending` 循环。正式回归断言恢复过程
`ensure=0 / resolve=1`，证明恢复不会重复创建 worker/gateway。

正式回归（`tests/multiuser/test_attempt_ownership_503.py`）：

- `test_lost_ensure_transport_error_recovers_through_first_screen`：真实
  Store/Provisioner/attempt 文件，普通 `provisioner_timeout` 丢失回复；
  关闭重开 DB 与 manager 后恢复 G2，`resolve=1`、恢复不再 ensure；
  恢复后正常 Stop 按真实候选 `(workspace, WORKER, replacement)` 收口。
- `test_lost_ensure_unknown_result_recovers_before_lease_expiry`：未知结果
  后租约立即到期；新 manager 在原 120s 租约到期前（clock 1201）即可恢复
  G2，证明不再有约 120s 的首屏阻塞窗口。
- `test_interrupted_lost_ensure_recovery_keeps_fenced_attempt`：恢复解析
  再次 `gateway_commit_timeout` 时保留 `pending_attempt_id`，下一 owner
  仍能收口为 ready。
- `test_ensure_ready_takeover_connect_failure_fails_closed_to_ready`：连接
  阶段 `provisioner_unavailable` 没有发出请求，仍回退 ready/unavailable。
- 原有取消路径 `test_lost_ensure_response_recovers_through_first_screen_entry`
  保持通过。
- `tests/multiuser/test_runtime.py::test_unix_client_maps_transport_loss_to_unknown_outcome_codes`
  锁定真实 Unix 客户端映射：连接阶段 OSError → `provisioner_unavailable`
  （不属于未知结果集合）；读超时 → `provisioner_timeout`；EOF →
  `provisioner_invalid_response`；连接建立后重置 →
  `provisioner_connection_lost`；`ok=false` 明确错误 →
  `container_conflict`（确定失败）。

## 2. R3-02 更新屏障与请求接纳的原子边界

### 根因

请求在 `RuntimeManager` 的 ready claim 之后、`request_leases` 登记之前没有
屏障检查。Update 建立屏障并读到 `activeRequests=0` 后，已通过 claim 的
媒体流仍可登记租约并开始上游传输，随后被更新中断。

### 实现

- `Store.begin_worker_request_lease` 新增 `require_admission`；为 True 时
  在同一个 `BEGIN IMMEDIATE` 事务内先检查接纳屏障再插入租约。
  - 屏障前提交的登记：更新读取计数时必然可见，等待其释放；
  - 屏障后到达的登记：原子失败为 `admission_closed`，不会开始上游传输。
- `WorkspaceApi._request_with_handle`（同步请求、上传、媒体流、status）
  传入 `require_admission=True`；`WorkspaceApi._db` 支持关键字参数透传。
- `WorkerRelay._worker_request` 新增 `require_admission` 参数：
  - `messages`（会话历史读取，属于新 workspace 请求）传 True；
  - `stop`/`events`/`answer`、后台对账 `_reconcile_run`/`_confirm_stop`
    与已接纳 Run 的派发不传，屏障期间仍可完成收尾；
  - `question_status` 属于已接纳 Run 等待用户回答的收尾链路，保持可读，
    避免更新等待期间 UI 无法展示问题而间接拖住活动 Run。
- 新 Run 仍由 `Store.reserve_run` 的原子屏障检查拒绝。
- 未修改 Update 脚本的等待/替换逻辑；未要求 drain 静态坏 worker 租约。

### 证据

主审探针修复前（`red-probe-r2-update-late-request.txt`，退出 0）：

```text
before_update_start={"activeRequests":0,"activeRuns":0,...}
after_update_start={"activeRequests":1,"activeRuns":0,...}
upstream_started_after_barrier=true
```

修复后原样复跑（`green-probe-r2-update-late-request.txt`，退出 1，探针的
红态断言不再成立）：

```text
before_update_start={"activeRequests":0,"activeRuns":0,...}
DomainError: admission_closed
```

请求在登记处被原子拒绝，未调用上游 `Gateway.request`，未产生 request
lease。

正式回归：

- `tests/multiuser/test_stop_barrier.py::test_update_barrier_rejects_request_that_passed_ready_claim`
  用真实 `Store`/`RuntimeManager`/`WorkspaceApi` 与 Event 控制在 ready
  claim 后的 inspect 暂停；建屏障并确认零计数后释放，断言
  `admission_closed`、上游未起流、`activeRequests` 保持 0。
- `test_update_barrier_counts_request_registered_before_barrier`：屏障前
  登记计入 `activeRequests=1`，释放后归零。
- `tests/multiuser/test_task_leases.py::test_update_barrier_rejects_late_messages_read`
  ：屏障后 `relay.messages` 在登记处被拒绝为 `admission_closed`，上游
  transport 未被调用，未留下 request lease。
- `tests/multiuser/test_task_leases.py::test_update_barrier_keeps_admitted_run_relay`
  ：屏障期间已接纳 Run 的 `events` 仍到达 worker，request lease 正常释放。

## 3. 回归与命令

受影响契约组（本机，Agent 环境；完整输出
`affected-contracts.txt`）：

```text
C:/Users/hyh/anaconda3/envs/Agent/python.exe -m pytest \
  tests/multiuser/test_attempt_ownership_503.py \
  tests/multiuser/test_runtime.py \
  tests/multiuser/test_runtime_gateway.py \
  tests/multiuser/test_original_web_api.py \
  tests/multiuser/test_artifact_access.py \
  tests/multiuser/test_gateway_client_errors.py \
  tests/multiuser/test_task_leases.py \
  tests/multiuser/test_stop_barrier.py \
  tests/multiuser/test_desktop_update_r2.py \
  tests/multiuser/test_workspace_start_identity.py \
  tests/multiuser/test_proxy.py \
  tests/multiuser/test_messages_relay.py -q
→ 248 passed, 4 skipped, EXIT=0
```

`pytest tests/multiuser` 独占全量运行（`full-multiuser.txt`）：
**1799 passed, 8 skipped, 5 failed, EXIT=1**。5 个失败均为
`tests/multiuser/test_admin_cli.py` 的 serve-control 用例，因本机
worktree 缺少前端构建产物报 `frontend_build_missing`；已在基线 `c9c0f0a`
的临时 worktree 原样复跑，同样 5 failed / 6 passed，确认与本轮改动无关。

另曾运行一次完整 `pytest tests/`：3131 passed、66 failed、100 skipped。
除上述 5 个 admin_cli 外，其余 61 个失败集中在 `test_setup_*`、
`test_direct_api.py`、`test_chat_question_replay.py`、`test_agent_harnesses.py`
等与本轮改动无关的既有环境/前端/平台用例；按 R3 执行单“不重复无关前端和
平台矩阵”的范围，本轮未逐个基线复跑这些非受影响套件，也不把它们作为
本轮回归门槛。

`git diff --check`：无输出。

## 4. 镜像与源码摘要

- 镜像：`easel-control:503-r3-final` = `sha256:0862f5475b856035082be24f4f7434f9d5f0ab0815411c1ab422daef1b9e8b45`
  （复审修复后重建）
- 构建命令（退出 0，日志 `control-build.txt`）：
  `docker build --progress=plain -f deploy/desktop/Dockerfile.control -t easel-control:503-r3-final --build-arg HTTP_PROXY=http://host.docker.internal:7897 --build-arg HTTPS_PROXY=http://host.docker.internal:7897 .`
- 镜像内与工作树源码 SHA-256 一致：

| 文件 | SHA-256 |
|---|---|
| `easel/control/runtime.py` | `b10761508d2b385370810d42ec1f3a7d5e24d5d7c676b2dc926098acd750083e` |
| `easel/control/store.py` | `80591b80ce078db20be134f6af4086caa21b0a4bae504e3c45663c391e59244d` |
| `easel/control/workspace_api.py` | `e40f6abc161197616365b1cd49f841cdb37d936e4ed45d67746ede2824b3dcb9` |
| `easel/control/proxy.py` | `ace317b3a46c2220c82a1a7d5767b65a72c14b410543b191eb4446bb66939150` |

未构建 worker/gateway 镜像（本轮未改其源码）。镜像仅本地构建，未推送、
未覆盖用户镜像。

## 5. 独立复审与修复轮

独立只读复审（fresh reviewer，范围 `c9c0f0a..8d7a983`）结论：无
Critical；1 项 Important、3 项 Minor。修复轮提交 `1b7959c`：

| 复审项 | 处置 |
|---|---|
| Important：`WorkerRelay._worker_request` 的 `messages` 等非收尾读取仍可绕过屏障 | 已修：`messages` 传 `require_admission=True`；stop/events/answer/对账保持不受接纳屏障限制；新增 `test_update_barrier_rejects_late_messages_read`（RED→GREEN）。 |
| Minor：未知结果保留完整 120s 租约，首屏有界阻塞 | **提升为 Important** 并修复：`expire_worker_gateway_refresh` 只缩短租约期限，不改写归属；新增 `test_lost_ensure_unknown_result_recovers_before_lease_expiry`（RED→GREEN）。按影响重估：单次网络超时可造成用户可见的约 120s 首屏不可用，修复成本仅一个 CAS。 |
| Minor：Unix 映射测试未覆盖 EOF/连接失败且未锁定码集合 | **提升为 Important** 并修复：区分连接阶段 `provisioner_unavailable` 与已发出后的 `provisioner_connection_lost`；测试覆盖 EOF、连接重置、明确 `ok=false`，并断言未知结果集合。执行单要求真实客户端映射覆盖，测试名原先宽于实际覆盖。 |
| Minor：`db_runner` kwargs 透传无固化回归测试 | 延后（deferred）：复审者已用生产一致的 runner 手工实测通过；本轮不扩测试面。 |

复审其余 “Declined to judge” 项按执行单边界保留：

- **ready→`_refresh_gateway` 的同类丢失回复**：ready 租约在 refresh ensure
  时丢失回复仍可能保留 G1 并遇 pending 阻断。执行单与复审记录明确限定
  “过期 starting 接管后”的场景，本轮不扩范围；列为后续 P1。
- `WorkerRelay.submit` 在 `reserve_run` 前的 worker 启动与屏障竞态：Update
  有意不等待静态 lease，属既有设计。
- 冷启动 `_start` 丢失回复：`_fail_start` 后下一次认领以
  `orphan_takeover` 接管，路径可恢复。
- broker 转发未接屏障：执行单明确要求保留已接纳 Run 的 broker 行为。
- 服务端 `ok=false` 但已产生资源：provisioner 自身失败关闭契约，非本 diff
  引入。
- `_ensure_handle_retry` 对 `admission_closed` 重试 3 次：既有行为，约 2s
  失败时延。
- 前端/Update 脚本/容器级更新流程未复跑：Update 脚本未改，引用 4382180
  证据；用户实例/Key/Cookie/DB 未操作；真实模型/平台未调用；Windows 本机
  未建立真实 Unix socket。

## 6. 未执行项与清理

- 未复跑容器级 Update / Stop→Backup→Verify：Update 脚本与部署流程未改，
  按 R3 执行单引用 4382180 已通过的正常更新与故障重试证据。
- 未启动真实模型、真实平台、真实供应商请求；未读取 Key/Cookie；未操作
  用户 bf8e… 工作区、DB、attempt 或验收实例。
- 未重复前端/平台矩阵；前端未改。
- 真实 Unix socket 未在 Windows 本机建立；错误映射测试通过替换
  `asyncio.open_unix_connection` 固定真实客户端的超时/OSError 分支。
- 探针均在临时目录运行并自动删除；`docker run --rm` 的摘要核对容器已
  自动移除；未遗留本轮容器、卷、网络或临时项目。
- 未合并、未推送；工作树交付后干净，停止等待复审。
