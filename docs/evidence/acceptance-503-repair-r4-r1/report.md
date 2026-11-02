# 验收实例 503 修复 R4-R1：交付报告

日期：2026-11-03（Asia/Shanghai）

父基线：`34ede47`（R4 最终 tip）
分支：`fix/acceptance-503-r3`
实施 worktree：`C:\Code\Easel-acceptance`

本轮提交：

| 提交 | 内容 |
|---|---|
| `8b40525` | R4-R1-01 远程副作用前持久精确 attempt 归属 |

本轮只收口 R4-R1-01 的控制进程退出窗口。未重开 R3-01/R3-02/R4-01 的既有
已审行为；未修改前端、更新脚本、worker/gateway 源码或平台功能；未合并、
未推送、未改动用户验收实例。

## 1. R4-R1-01 根因与实现

### 根因

`_refresh_gateway` 正常 ready 刷新在 `_ensure_candidate`（远程 ensure）之后
才依赖 `except` guard 补写本轮的 `attempt_id`；`restore_gateway` 经
`_restore_candidate_guarded` 也一样。主审探针证明：远端已写 G2/pending
后控制进程直接 `os._exit`，异常/取消处理没有执行机会，DB 仍是
`ready/G1/旧 attempt`。重启后按旧 attempt 收口被远端新 pending 拒绝为
`gateway_cleanup_pending`，跨过租约期限仍永久 503。

### 实现（最小、复用已审归属）

- `Store.fence_unknown_gateway_refresh` 改为返回 CAS 后的 `WorkerLease`
  （失败返回 `None`），语义扩展为“远程调用前预占或结果未知时冻结”。
  CAS 仍严格要求 `workspace/generation/owner/attempt` 精确匹配且
  `pending_attempt_id = ''`；成功后原子写入本轮精确 attempt、代次 +1、
  转为立即到期的 `starting`。不覆盖已接管代次、不按目标猜候选、不结算 Run。
- `_refresh_gateway` 在调用 `_ensure_candidate` **之前**执行该预占；失败即
  抛 `worker_start_superseded`，绝不发出远程调用。后续 staging 使用预占
  返回的新代次/owner。
- `restore_gateway` 的 `_restore_candidate_guarded` 在调用
  `_restore_candidate` **之前**执行同一预占，两入口归属语义一致。
- 确定失败（不在已审未知结果码集合）走新增
  `Store.rollback_gateway_refresh_attempt`：按预占 generation/owner/attempt
  精确 CAS 撤销，恢复旧 attempt、`ready/unavailable` 与存活租约，保留在途
  Run 与既有 worker，正常 Stop 不受影响。
- 未知结果（超时/连接丢失/无效响应/等待超时）与协作取消**保留**预占，
  不再依赖 `except` 补写。恢复仍走既有
  `_takeover_for_restore → _resolve_fenced_attempt → _recover_lost_attempt`
  的精确 attempt 解析、adopt/commit 路径。
- “attempt 已持久但远程尚未执行”时，provisioner 无该 attempt 记录，
  `commit_attempt` 按无 pending 承诺成功，清空槽位后正常重试；不永久
  `starting`，不删除仍可信的 G1。

没有新增通用状态机；只复用现有 `starting/pending_attempt_id` 归属机制。
worker/gateway 源码、Run/token/收尾逻辑均未改动。

## 2. 红态证据（父基线 `34ede47`）

在同一父基线临时 worktree 原样运行主审探针
（`red-probe-ready-refresh.txt`，退出 0）：

```text
1202 worker_start_failed gateway_cleanup_pending ready <旧attempt> pending <新attempt>
2000 worker_start_failed gateway_cleanup_pending ready <旧attempt> pending <新attempt>
4000 worker_start_failed gateway_cleanup_pending ready <旧attempt> pending <新attempt>
CONFIRMED: 进程退出后重开DB，跨租约期限仍永久503
```

同一基线运行本轮新增子进程退出测试（`red-tests.txt`，退出 1）：

```text
5 failed
```

其中 3 项为真实子进程 `os._exit(73)` 的崩溃恢复用例，均停在“DB 仍为
ready、未持久预占 attempt”的断言；2 项为确定失败不得滞留预占的护栏，
停在“预占未产生代次失效/确定失败未恢复 unavailable”的断言。

## 3. 绿态证据

正式命令（固定 Agent Python，均记录在对应证据文件的 `COMMAND` 行）：

| 证据文件 | 范围 | 结果 | 退出码 |
|---|---|---|---|
| `green-new-tests.txt` | R4-R1 新增 5 项 | 5 passed | 0 |
| `green-attempt-ownership.txt` | attempt 归属全文件 + 新增 | 48 passed | 0 |
| `green-affected-contracts.txt` | R3 受影响 12 文件 + 新增 | 260 passed, 4 skipped | 0 |
| `green-store-lifecycle.txt` | store/生命周期 3 文件 | 89 passed | 0 |
| `green-probe-ready-refresh.txt` | 主审探针修复后原样复跑 | 见下 | 1 |

受影响契约组覆盖 `test_attempt_ownership_503.py`、`test_runtime.py`、
`test_runtime_gateway.py`、`test_original_web_api.py`、
`test_artifact_access.py`、`test_gateway_client_errors.py`、
`test_task_leases.py`、`test_stop_barrier.py`、`test_desktop_update_r2.py`、
`test_workspace_start_identity.py`、`test_proxy.py`、
`test_messages_relay.py` 与本轮新增文件。R3 原子屏障、messages/既有 Run
收尾、超时/断连/取消、恢复再次中断、旧 owner 迟到写回护栏均保持通过。

主审探针修复后原样复跑退出 1，原因是探针自身编码的旧红态断言
`raise AssertionError('意外恢复成功')` 在恢复成功后触发；实际行为已从
“永久 503”变为“首屏恢复成功”。按 R4-R1 补单，以实际状态与正式正向
回归为准：

- `test_ready_refresh_process_exit_after_remote_recovers`：真实子进程在
  远端写 G2/pending 后 `os._exit(73)`；DB 预占为 `starting/精确 attempt/
  立即到期`；重启普通首屏恢复 G2，恢复阶段 `ensure=0、restore=0、
  resolve=1`，正常 Stop 按 G2 收口；旧代次迟到写回返回 False。
- `test_ready_restore_process_exit_after_remote_recovers`：显式 restore
  同一窗口；重启 `restore_gateway` 按同一精确归属恢复 G2，不重复创建
  worker/gateway。
- `test_ready_refresh_process_exit_before_remote_recovers_g1`：预占已落盘
  但远程尚未执行即 `os._exit(73)`；重启不永久 `starting`，不删除可信
  G1（`gateway.stops == []`），只重新执行一次原本未发出的 ensure。
- `test_ready_refresh_definite_failure_rolls_back_reservation` /
  `test_ready_restore_definite_failure_rolls_back_reservation`：确定失败
  撤销预占，恢复 `ready/旧 attempt/unavailable`，不把刷新滞留为
  `starting`。

## 4. 镜像与源码摘要

- 镜像：`easel-control:503-r4-r1-final`
  = `sha256:b30351b19ff72bd38b371d81c607db582c9e242581fb90776ef39564d9092ee3`
- 构建命令（退出 0，日志 `control-build.txt`）：
  `docker build --progress=plain -f deploy/desktop/Dockerfile.control -t easel-control:503-r4-r1-final --build-arg HTTP_PROXY=http://host.docker.internal:7897 --build-arg HTTPS_PROXY=http://host.docker.internal:7897 .`
- 无挂载、无网络、只读、`--rm` 临时摘要容器逐文件核对
  （`image-hashes.txt`）：

| 文件 | 宿主 SHA-256 | 镜像 SHA-256 |
|---|---|---|
| `easel/control/runtime.py` | `04a9f8be595160c3df99ef727349365024eb035e7ae63620bf3543d4ede35196` | 同左 |
| `easel/control/store.py` | `0f9a2fa00c327a38340af3928514a17be9d941f567f79066175668dd595d34ae` | 同左 |

本轮未改 worker/gateway 源码，未重建对应镜像。镜像仅本地构建，未推送、
未覆盖用户镜像。

## 5. 未执行项与清理

- 未复跑容器级 Update / Stop→Backup→Verify：更新脚本与正式更新流程未改，
  沿用 R4 报告中既有 `4382180` 正常更新/故障重试证据，明确非本轮重跑。
- 未运行全量 `pytest tests/`、前端或真实平台矩阵；按 R4-R1 补单只跑受影响
  attempt/runtime/store/Workspace API 与 R3 屏障/messages/Run 护栏。
- 未启动真实模型、真实平台或供应商请求；未读取 Key/Cookie；未操作
  bf8e 用户工作区、DB、Key、Cookie、attempt 或验收实例。
- 红态临时 worktree `C:\Users\hyh\AppData\Local\Temp\easel-r4-r1-red`
  已移除；探针临时目录自动删除；`docker run --rm` 摘要容器自动移除。
- `git diff --check` 无输出；代码提交后工作树仅余本证据目录。未合并、
  未推送，交付后停止等待主审。
