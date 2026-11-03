# 验收实例503修复R2独立复审

基线 `43e4cd0`；最终 tip `c9c0f0a`。结论：暂不通过，仅下发 [R3 两项收口](../tasks/2026-11-02-acceptance-503-rework-r3.md)，不合并、不更新用户实例。

## 已关闭及独立验证

- R2-01 普通首屏过期 starting 已共用接管 CAS：原 ensure 探针显示 owner-b，旧 cleanup=false，DB starting，attempt committed。
- R2-04 部分更新失败恢复、R2-05 本项目实例范围与持久 managed 基线，本轮未发现新的阻塞。作者4382180→最终镜像的正常更新与精确停止旧 provisioner 后重试证据已核对；不是主审重跑容器。
- 原 pre-takeover、commit budget、rollout parameters 探针独立复跑与报告一致；更新屏障事实探针显示 activeRuns=1，屏障本身不会结算 Run，这符合设计。
- 原 lost-response 探针确实在第二次固定取消时退出；这是测试 Adapter 限制，不独立判作产品缺陷。作者一次性丢失变体由主审重新运行，结果 ready、pending=false、无 recovery error；但仅覆盖 CancelledError，不覆盖下面的普通超时。
- 主审 attempt/runtime/workspace API/desktop update/参数契约/stop barrier 七文件：163 passed，1 skipped。
- 主审 provisioner通配组/store/worker execution gate/gateway runtime：235 passed，1 skipped。两组文件不重叠，合计398 passed、2 skipped。
- `git diff 43e4cd0..HEAD --check` 无输出；实施工作树干净。前端无改动，未重复运行。作者1796项全量结果仅作为作者证据。
- 两名独立审查者分别审协议恢复及更新流程；以下两条纯临时探针均由主审再次执行确认。不使用 Docker、网络或用户凭证。

## R3-01 / P1：普通超时丢失回复后，恢复路径仍被绕过

位置：`easel/control/runtime.py:570` 的 `_refresh_gateway` 异常分支。takeover 已把新 attempt 写入租约；provisioner 完成替换 G2、持久 pending 后，控制端等待响应超时。此类 `RuntimeUnavailable` 落入普通 Exception 分支，把 starting 回退为 ready/unavailable，DB 仍保存 G1、pending_attempt_id 为空。

后续首屏从 ready/reconcile 进入 refresh，不调用只用于 starting 接管的 `_recover_lost_attempt`。提交 G1 失败被普通 commit 记录日志；新 ensure 又被 G2 pending 阻断。UnixProvisionerClient 会将 timeout/OSError 转为 RuntimeUnavailable（`:1751` 附近），故这是真实回复丢失方式，不是额外业务需求。

主审 [probe_r2_lost_ensure_error.py](../../evidence/acceptance-503-review/probe_r2_lost_ensure_error.py) 使用真实 Store/RuntimeManager/Provisioner `_with_gateway` 和 attempt 文件，仅远程 gateway 为假材料；关闭重开 DB、manager、provisioner 缓存后重试两次：

```text
first_error=provisioner_timeout
after_failure={state:ready,gateway_is_old:true,pending_attempt_id:""}
reopened_retry_errors=[gateway_cleanup_pending,gateway_cleanup_pending]
attempt_status=pending
attempt_gateway_is_replacement=true
physical_gateway_ensure_calls=1
resolve_calls=0
temporary_resources_removed=true
```

现有测试用 CancelledError 保留 starting，因此掩盖此分支。需让普通网络错误/超时的未知结果也保留可恢复归属，不能以取消测试替代。

## R3-02 / P1：零活动计数与晚登记请求之间缺少原子边界

位置：`deploy/desktop/Update-WorkspaceEasel.ps1:414` 看到 activeRuns/activeRequests=0 即允许进入 Start；请求转发到 `easel/control/workspace_api.py:296` 才登记请求租约；`store.py:1749` 登记不检查接纳屏障。

具体真实入口是 GET/HEAD `/api/media/{path:path}` → `_workspace_media_response` → `WorkspaceApi.open_stream`。请求在屏障前已通过 RuntimeManager 的 claim，在 gateway inspect 等待期间无 request lease。Update 建屏障并读取零计数后，该请求仍可登记并开始流式传输。此时重建 control/egress 可能切断请求，违反本轮更新的在途工作保护要求。

主审 [probe_r2_update_late_request.py](../../evidence/acceptance-503-review/probe_r2_update_late_request.py) 使用真实 Store/RuntimeManager/WorkspaceApi、gateway 模式、ready实例，以 Event 控制远程 inspect：

```text
before_update_start: activeRequests=0, activeRuns=0, lease=ready
after_update_start: activeRequests=1, activeRuns=0, lease=ready
upstream_started_after_barrier=true
temporary_resources_cleaned=true
```

需要请求接纳/计数与更新屏障建立形成原子边界。不能用等待所有 ready/starting 租约消失来解决，否则会重开现场坏租约的 drain 死锁。也不能误阻止已接纳 Run 的 stop/events/answer/broker 正常收尾。

## 边界与裁决

仅 R2-02 普通错误恢复和 R2-03 请求计数空窗继续返工。旧屏障不绑定新目标摘要、候选物理消失时失败关闭、独占WorkspaceRoot、单次Docker读取缺独立超时等已声明边界不泛化为新阻塞。

本轮主审未启动或修改任何用户容器/实例，未读取Key/Cookie，未修改用户DB/attempt，未调用真实模型/平台，未合并/推送。6A整体验收不因此宣称完成。
