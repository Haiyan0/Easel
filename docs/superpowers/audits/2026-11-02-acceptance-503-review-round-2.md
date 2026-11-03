# 验收实例 503 修复 R1 独立复审

基线 `966d650`；生产 `23146a2`；证据 tip `43e4cd0`。结论：暂不通过，不合并、不更新用户实例。仅下发 [R2 执行单](../tasks/2026-11-02-acceptance-503-rework-r2.md)。

## 已验证的改进

原三条主审探针未修改、独立复跑：restore 接管时旧清理返回 false、DB 保留 starting、attempt 为 committed；0.02 秒预算探针立即返回 success；Stop 不支持 WorkspaceRoot，模板已删除该参数。schema v15、精确 attempt 提交及 committed/completed 区分已有实质修复。

主审实际运行：

- attempt/runtime/workspace API/命令与身份契约六文件：134 passed，1 skipped。
- provisioner 通配组、worker execution gate、gateway runtime、store、desktop operations R1、lifecycle lock、workspace start identity：265 passed，1 skipped。与前组有 identity contract 文件重叠，不相加。
- 新增三条纯临时探针，见下；均清理完成，无 Docker/真实资源访问。
- 本轮前端无改动，未重跑前端；作者 1774 项全量与隔离更新结果已阅读，未冒充主审复跑。

第二组第一次命令误写不存在的 test_provisioner_gateway.py，未收集测试；随后改用实际 test_provisioner*.py 文件列表运行通过。实施 worktree 干净。`git diff 966d650..43e4cd0 --check` 发现 control-build.txt 六处尾随空白，交付所称整个补丁 diff check 通过不成立；生产代码不受此文档格式问题影响。

## 阻塞项

### R2-01 / P1：ensure_ready 仍在接管前提交旧 attempt

`easel/control/runtime.py:225` 的 refresh 分支调用 `_refresh_gateway`；`runtime.py:521` 在 owner/generation 交接前直接提交 lease.attempt_id。Store 对过期且已有 gateway 的 starting 返回 refresh（`store.py:1194`）。新增 `_takeover_for_restore` 只用于 restore_gateway，普通首屏入口没有使用它。

主审独立运行 [probe_r1_ensure_takeover.py](../../evidence/acceptance-503-review/probe_r1_ensure_takeover.py)：真实 Store、RuntimeManager.ensure_ready、清理函数及 Provisioner commit/abort；仅远程 ensure/gateway 用假材料。结果：

```text
before_owner=owner-a
cleanup_error=gateway_abort_superseded
gateway_stop_count=0
database_state=stopping
attempt_status=committed
```

旧 owner 仍能取得清理 CAS，但其承诺已被提交，导致停止失败并再次留下 503 状态。必须同时修普通首屏与既有 Run 恢复两种入口。

### R2-02 / P1：替换 gateway 的响应丢失后，持久槽位不能恢复

`runtime.py:630` 恢复远程候选，随后才将返回候选 staging 到 DB；新 takeover 已提前把本轮 attempt 写到租约。若 provisioner 已写入新 gateway G2 的 pending，而 control 在接收响应/DB staging 前退出，DB 仍保存 G1。后续 `_resolve_fenced_attempt`（`runtime.py:726`）用 G1 提交该 attempt，精确校验拒绝与 G2 对应的 pending。仅持久化 attempt ID 不足以恢复其真实资源归属。

主审独立运行 [probe_r1_lost_restore_response.py](../../evidence/acceptance-503-review/probe_r1_lost_restore_response.py)，关闭重开 SQLite、换两个 manager 在两个过期时间恢复：

```text
lost_response_target_mismatch=true
recovery_errors=[gateway_commit_superseded,gateway_commit_superseded]
database_state=starting
pending_attempt_present=true
attempt_status=pending
```

不能通过忽略 ID 或把所有 pending 当作 committed 修复；须让远程结果未知时的恢复仍绑定精确 attempt 和真实资源。

### R2-03 / P1：Update 屏障不保护已有活动 Run

`deploy/desktop/Update-WorkspaceEasel.ps1:380` 只确认接纳屏障存在，随后调用 Start；`Start-WorkspaceEasel.ps1:656` compose up 重建 control 和 egress。未检查活动 Run 或在途请求。屏障拒绝新任务，不会完成现有任务；模型请求仍依赖旧 control/egress，有限退出宽限不能保证长流结束，核验容器/Key/attempt 仍可能报告更新成功。

主审 [probe_r1_update_barrier.py](../../evidence/acceptance-503-review/probe_r1_update_barrier.py) 用真实临时 Store 验证设置屏障后仍为 `activeRuns=1, settled=false, barrier_present=true`。更新须在替换前等待/拒绝活动工作，且不把现场坏租约本身当作必须先正常 drain 的死循环条件。

### R2-04 / P1：部分更新失败后不能按模板重跑

Update `:296` 要求 control 正在运行且镜像与旧 deployment 一致，`:310` 要求旧 provisioner identity/PID 可读，这些检查早于沿用 desktop-update 屏障。Start 已停止旧 provisioner，但新进程启动失败时，重跑会报 update_old_identity_unavailable；compose 重建新 control 后健康/前端校验失败时，deployment 尚未在 Start `:823` 更新，重跑报 update_control_image_mismatch 或 update_control_unavailable。

这与 commands.md 承诺的“修复后可重跑同一条 Update”矛盾。要求有受屏障与原身份约束的部分完成恢复，不允许靠手工 resume、删状态或放宽所有镜像/PID 校验绕过。此项以源码执行顺序确认，未对真实实例注入故障。

### R2-05 / P2：更新实例快照包含其他部署

Update `:144` 只按 `label=easel.managed=true` 抓整个 Docker engine。另一部署正常创建/停止 worker 也会导致当前更新报 update_managed_instances_changed，留下接纳屏障。交付 e2e-result.json 的隔离项目快照甚至包含用户 bf8e 工作区的原 worker/gateway ID（仅只读包含，不代表被修改）。应限定本项目登记的 workspace/精确实例，不能依赖全机无其他变化。

## 边界与裁决

- R1-03 参数修正和 R1-04 附加 commit 预算反例已关闭；不要求重开前端、账号、发布或真实模型验证。
- R1-01/R1-02 尚不能整体关闭。新隔离更新链路成功是有效证据，但不能覆盖以上交错和失败窗口。
- 隔离旧版本为 966d650；真实用户实例仍是 4382180。已静态确认其有 identity/barrier API 与 CLI，未发现单纯缺接口的必现故障；最终上线指令仍应核验该实际版本组合，不能声称已演练。
- 主审未启动/停止/更新用户容器，未读取 Key/Cookie，未修改用户 DB/attempt，未运行真实供应商/平台，未合并或推送。
