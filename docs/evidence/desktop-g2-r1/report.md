# Task G2 R1 复审返工证据

日期：2026-10-28（Asia/Shanghai）

上一轮 G2 提交：`3ce9fcf4a65823c12c19db64f7dddfdbfa612eb5`
父基线：`d1fc8b16d6cb98092fa1ff9d0772c15bd40e35bd`

仅修复 G2-R1-01/02/03/04；未重做 G1/A3，未接公开 relay/聊天，未进入 A4。

## G2-R1-01：abort 清理所有权 fencing

- `WorkerLease`/`WorkerHandle` 新增 `attempt_id`，Store v12 只追加该列。
- v2 ensure 携带 attempt_id；provisioner 记录每 workspace 当前 attempt，
  abort 请求的 attempt 不匹配直接返回 `gateway_abort_superseded`。
- RuntimeManager 激活失败不直接按 created bool 删除：先在 Store 原子认领
  `starting owner/generation/attempt → stopping`。CAS 失败说明实例已被新
  owner 采用，绝不发送远程 abort；认领成功后新 owner 只能等待/走
  recover_stop，不能采用停止承诺未消解的实例。
- `abort` 成功后 `stopping → stopped`；失败保留 stopping 和 cidfile/材料，
  由精确 ID 的后续 stop/recover 处理。
- v2 stop 在旧 gateway ID 不存在但同名新 gateway 存在时返回
  `gateway_target_mismatch`，不按旧 tuple 删除新 gateway 正在使用的 worker。

## G2-R1-02：refresh 崩溃恢复

- refresh 的 `begin_worker_gateway_refresh` 保留 worker container ID、
  service token、在途 Run 和候选 gateway；`claim_worker_start` 对过期
  starting 且已有 worker ID 的租约返回 `refresh`，不再清空身份走冷启动。
- 新 owner 从 `starting` 执行 refresh CAS，重新 ensure/configure/activate；
  gateway 临时故障只恢复 `ready/unavailable`，不调用 `fail_worker_start`
  或中断 Run。
- 只有实际 ensure 返回不同 worker 时不采用新实例并保留旧租约；不把时钟
  推进或临时 gateway 故障当作执行终止。
- 测试真实双 Store 连接：旧 refresh staged 后进程退出、租约过期、下一次
  ensure 临时失败，Run 保持 running、stop 调用为空；随后恢复同 worker/
  新 boot。另覆盖 starting refresh 成功恢复。

## G2-R1-03：worker/gateway 健康分离与自愈

- provisioner v2 inspect 新增 `worker_running`、`worker_ready`、
  `sidecar_ready`；`worker_ready` 由 worker 网络/接线、token、exec 探针
  得出，`available` 独立由 gateway 容器内状态得出。
- RuntimeManager 快速路径先要求 worker 身份匹配且 `worker_ready=true`；
  未就绪只返回 503 `worker_not_ready`，不返回可派发句柄、不删 worker。
- DB `gateway_state=unavailable` 但物理 gateway 同 boot 恢复 available 时，
  通过 `mark_worker_gateway_available` CAS 恢复 DB，再返回句柄；不会永久
  identity mismatch。
- GatewayFake 改为独立维护物理 worker/gateway 状态，不再由 DB
  gateway_state 构造 available。

## G2-R1-04：gateway create 追踪清理

- `docker create` 使用专属 attempt 目录中的 cidfile；超时/取消时等待底层
  Docker CLI 终结后读取 cidfile。
- create、start、connect、configure 失败/取消都按 cidfile 的不可复用 ID
  清理；不按同名容器兜底强删，不影响已有 worker/其他 gateway。
- 清理使用唯一 cleanup task，外层反复取消仍等待其终结；删除失败抛出
  `gateway_create_cleanup_incomplete` 并保留 cidfile 与 trace 材料。
- 单元覆盖 create 超时、取消后才返回、stop 失败、同名后继；真实 Docker
  CLI 测试使用延迟 wrapper，在 daemon 已创建容器后触发 CLI 超时并确认
  定向删除。

## 红证据

主审 R1 复现的失败终态保留为红基线：

```text
CURRENT_LEASE ready owner-b
OLD_ABORT_SENT True
ABORT_RESOURCE_IS_CURRENT True True CREATED_FLAGS True True

RUN_AFTER_REFRESH_CRASH interrupted STOP_CALLS []

UNREADY_WORKER_RETURNED True
RECOVERY worker_identity_mismatch unavailable ENSURE_CALLS 1
RECOVERY worker_identity_mismatch unavailable ENSURE_CALLS 1

CREATE_TIMEOUT_NO_RECOVERY True
```

本轮新增确定性测试后，先观察到旧 code/接口的
`worker_start_failed`（临时 gateway 失败被包装）等问题；修正后上述路径
全部转绿。真实集成首轮还暴露 attempt_id 未进入 ensure 白名单的
`unknown_field`，已修复并复跑。

## 绿证据

```text
G2/R1 新增单元（server/runtime/runtime-gateway/store/create-recovery）
38 passed

指定 provisioner/runtime/store/stop/gateway/拓扑回归
284 passed, 6 skipped

tests/multiuser + Desktop 拓扑
1257 passed, 9 skipped, 2 warnings

真实双 workspace + 创建超时
2 passed, 341.40s
```

固定测试镜像（最终真实运行）：

```text
GATEWAY_IMAGE sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a
WORKER_IMAGE  sha256:0d8ea2c3e6f21d3ba9cb12125f80dd851eef73c013d0fb24d7a69676dfab7ef4
```

最终双 workspace handles：

```text
workspace 95e5610b0df14ce5ad724c822c1f35eb
  worker  0f157a65c914231b4420c5a96e0ada5ae6e381823c2b94e3b1c96c288859e6f0
  gateway 997bb5159fce489ea642a3c892cc385b3b0b4ba9a048e6787e9f56889a36f5ff
  boot    0f278c82088802788cc741a3483326ea
  upstream http://172.31.201.3:8897
workspace 86dbc4f4048b47b39264467a9ac66af5
  worker  ece57874ed31e7a76959efb1477e2dc81de3854869687c1ad56f717c50e11798
  gateway 425d1899a3f94993c01b944500a02b4ecb766b82fe7b0e4feb4eba4129bd7fc8
  boot    ebf7b9025767104b072551a15d636747
  upstream http://172.31.201.2:8897
```

真实覆跑保留正常双 workspace 的签名 health/submit/events/questions/stop/
messages、跨 workspace 拒绝、worker 反向不可达、同容器 gateway 重启新 boot、
旧断言 503→403、worker ID/在途 Run 保持、精确 ID drain；新增 create CLI
超时后的 cidfile 定向清理。

## 清理与边界

- 真实测试后按随机名称/精确 ID 删除两个 worker、两个 gateway、两个 worker
  网络和临时 control 网络；检查 `easel-gateway-*`、`easel-worker-*`、
  `easel-sidecar-*` 容器与 `easel-g2*`/`easel-ws-*` 网络为空。
- 未执行全局 prune，未操作开发者容器/卷/网络、HOME/profile、ngrok 或供应商。
- 未运行公开 relay/聊天、A4 broker/受管 Agent、真实 DSH、Chromium；Linux
  生产/XFS/防火墙门槛不变。
