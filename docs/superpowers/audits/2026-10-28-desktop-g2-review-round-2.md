# Task G2 R1 复审：清理范围恢复定点收口

父提交：`3ce9fcf4a65823c12c19db64f7dddfdbfa612eb5`。
审查提交：`65cec868d933679fc850b5e7c84f38dc75bcc22a`。

## 裁决

G2-R1-01/02/03/04 原反例复验已关闭。迟到 abort CAS、refresh 崩溃恢复、worker/gateway 健康分离与同 boot 自愈、gateway create 追踪均保留。

仍需修以下停止恢复的清理范围遗漏，暂不关闭 G2，不进入 A4。仅修这一项，不重开整轮。

## G2-R2-01：gateway-only 启动清理恢复错误升级为完整 worker stop

位置：easel/control/runtime.py `_cleanup_candidate` 约 904–925、`_recover_stop/_stop_lease`；easel/control/store.py `begin_worker_abort` 约 980。

本轮可以复用既有 worker、仅创建新 gateway（worker_created=False / gateway_created=True）。激活失败时，abort 本应只删新 gateway。但清理失败/取消后 DB 只记录通用 stopping，没有持久化停止目的和资源范围。租约过期后 `_recover_stop` 调用完整 worker+gateway stop，结果删除本轮没有创建、仍可能执行 Run 的既有 worker。

主审与独立审查沿用现有 GatewayFake/真实临时 Store/RuntimeManager 实际复现：

```text
FIRST_ERROR gateway_cleanup_failed
BEFORE_RECOVERY stopping WORKER_CREATED False
RECOVERY_FULL_STOP [(workspace, existing_worker_id, gateway_id)]
RUN_STATE running
```

后续真实 provisioner stop 会物理停止该 worker，而记录仍为 running。涉及在途 Run 时为 P1；即便无 Run，也违反“abort 只清理本轮新建资源”的 G2 边界。

## 最小修复

1. 把停止目的（业务 drain/stop 或启动 abort）、attempt_id、实际目标 ID 和允许删除范围持久化；不能只留 stopping。范围来自已核验的本轮候选及 CAS，不接受任意请求布尔值扩大授权。
2. `recover_stop` 恢复同一种操作和同一范围：gateway-only abort 不得调用完整 stop，不停止 worker/sidecar；worker-only 或两者新建按各自范围确认。只有显式业务 drain/stop 才可执行原完整停止。
3. gateway-only 清理完成后，保留既有 worker 的物理身份、凭证、历史、Run/容量/token，回到可对账的 unavailable/恢复状态；不能写一个清空 worker 身份的假 stopped 再按冷启动处理。
4. 控制进程或 provisioner 重启后也能恢复这个精确尝试。provisioner 当前内存 `_attempts` 不是持久恢复凭据；明确可信恢复/认领方式，不能绕过 attempt fencing 或因内存记录丢失退回完整 stop。
5. 继续保持旧 owner/旧 attempt 不删新 owner 已采用实例的门禁。恢复过程中失败/超时/取消也不扩大范围，不清空唯一追踪材料。
6. 若需要新字段只追加迁移，不重写已交付 v12；正常业务停止与旧记录的默认语义明确，回归旧数据库升级。

## 红绿验收

- 两个真实 Store 连接：复用 worker + 新建 gateway，激活失败且 abort 非零/超时/取消；原控制进程退出、租约过期、新 owner 恢复。断言无 worker stop/rm、新 ID 不替换、sidecar 保留、Run 始终 running/awaiting_permission、容量释放计数和 broker token 不变。
- provisioner 实例重建导致内存 attempt 丢失后，可信恢复仍只处理原 gateway；调用真实受限 abort/stop 消费者检查实际 Docker runner 指令，不只依赖 fake.stop_calls。
- worker_created/gateway_created 的组合覆盖；显式业务 drain 仍完整停止并正确写终态。旧 owner 迟到 abort、旧 gateway/new tuple 保护继续通过。
- 至少一项真实临时 Docker 路径确认 gateway-only 失败恢复后既有 worker 容器仍 Running 且原 ID 不变；不要求真实供应商或重新构建无变化的 gateway/worker 应用镜像。
- 跑 runtime/store/gateway/provisioner/stop-recovery/文档受影响回归，diff check。

## 主审本轮验证与边界

- G2 四文件 + create_recovery + provisioner/runtime/store/stop_recovery + 文档：**288 passed, 1 skipped**（8.45 秒）。
- 原四项由独立审查重放关闭，相关 runtime/store 新测试 **19 passed**。
- 上述新反例由主审再次执行；只用临时 DB、现有测试边界和真实状态机，没有操作 Docker 或开发者数据。
- 本轮没有重跑 341 秒真实双 workspace 正常场景；实施方证据保留，不能覆盖新增的失败恢复范围。

## 给代码 Agent

仅修 G2-R2-01。先补清理范围丢失的红测试，再持久化精确停止目的/资源范围并接通恢复；保护主审文档。交付完整父/新 SHA、红绿、重启恢复时序、实际 runner/Docker 保留 worker 证据和迁移验证，停下等复审，不进入 A4。
