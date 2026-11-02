# Task G2 R2 复审：物理成功与数据库提交间的幂等收口

父提交：`65cec868d933679fc850b5e7c84f38dc75bcc22a`。
审查提交：`14bfff0636caa63cccf8ec6a36b7b462a9f3e0a1`。

## 裁决

原 G2-R2-01 的资源范围扩大问题已修复：gateway-only 清理持久化并按原 scope 恢复，不停止既有 worker。主审真实 Docker 验证也通过。

仍有同一恢复流程的最后一处幂等缺口，需要仅修 G2-R3-01 后关闭 G2；不进入 A4，不重做此前已关闭问题。

## G2-R3-01（P2）：abort 成功后先删记录，control 崩溃会永久卡 stopping

位置：deploy/multiuser/provisioner.py `abort` 成功后的 `_clear_attempt`（约 2303–2305）与缺记录统一返回 gateway_abort_superseded（约 2283–2291）；关联 RuntimeManager `_recover_stop` 的重试。

跨进程物理清理与 DB 提交不能原子完成。当前流程是：

1. control 已持久化 stopping/start_abort/scope/attempt。
2. provisioner 成功清理指定资源，立即删除持久 attempt 文件并返回。
3. control 在收到回复前断连，或回复后尚未执行 finish_gateway_abort/stop_worker 就崩溃。
4. 新 control 恢复原 stopping，再发同一 abort；provisioner 因记录已删除一律拒绝。重试不会自行恢复，DB 永久 stopping。

主审使用真实 Provisioner.abort、受控 gateway.stop 和临时目录复现：

```text
FIRST_ABORT_SUCCESS True ATTEMPT_RECORD_EXISTS False
RECOVERY_RETRY gateway_abort_superseded
RECOVERY_RETRY gateway_abort_superseded
PHYSICAL_GATEWAY_STOP_CALLS 1
```

独立复核用真实 Store + RuntimeManager + Provisioner 验证：物理成功后模拟 control 未写 DB，重启恢复连续两次 recover_stop 均拒绝，DB 仍 stopping、Run running。

## 最小修复

- abort 物理完成后保留持久的已完成记录（tombstone），包含准确 attempt、workspace、worker/gateway ID、scope 与完成状态；先可靠持久化，再返回成功，不立即删除唯一恢复依据。
- 同一 attempt/同一 ID/同一 scope 重试，能从完成记录得到幂等成功，不再次执行破坏操作。记录不匹配或被更晚 attempt 取代时，继续失败关闭；不能把所有缺记录情况直接当成功。
- 若物理已完成但写完成记录前崩溃，保留的 pending 记录应允许按同 ID/范围重做幂等终态核验，再提交 completed。不能扩大到完整 worker stop。
- completed 记录的回收必须不破坏 control 尚可能重试的窗口。可在受信的新 attempt 接管后按规则取代旧记录，或通过明确的确认协议回收；不要用固定极短 TTL 把正常恢复变成人工修复。
- 不改变既有 CAS/fencing 与恢复范围，不重写已交付迁移；通常可用现有 attempt JSON 增加版本/状态完成，无需改业务数据库。

## 红绿验收

1. 两个真实 Store 连接 + 真实 Provisioner 消费者：物理 abort 成功，故意不提交控制库/丢弃回复，重建 control/provisioner 后重复恢复。最终 DB ready/unavailable（gateway-only）或合法停止终态（本轮新建 worker），不再卡住。
2. 同一完成记录并发/多次重试不重复删除，scope/ID/attempt 任一不符仍拒绝。
3. 物理完成后、completed 记录持久化前故障，重试能有限完成；写记录失败不能假报已收口。
4. 新 attempt/新代次出现后，旧 completed 重试不能作用于新资源；旧 owner 不覆盖当前记录。
5. gateway-only 全程既有 worker ID/Running、Run/容量/token 不变；业务 drain 仍完整停止。

## 主审本轮验证

- runtime/store/gateway/provisioner/stop-recovery + 文档：**290 passed, 1 skipped**（8.37 秒）。
- 主审直接执行现有真实 Docker gateway-only 验收函数，传入报告中的两个已保留固定镜像摘要，绕过构建 fixture（未改测试文件），得到 **REVIEW_GATEWAY_ONLY_DOCKER_PASS**。真实 gateway 删除后 worker 原 ID 仍 Running，随后业务 stop 可删除。
- 实测 gateway ID：`e720e030c04f28b11d7a6b94d5dbc2461103b79263362ce34146aab578e8556e`；worker ID：`ecd844ad4fd1e9f75dcf273cf22a0aa19ce80c222ed76f672eab1cbfac2d5cb3`。均为本次临时测试资源。
- 测后 easel-gateway-*、easel-worker-*、easel-g2-*、easel-ws-* 查询无残留；未操作开发者资源/全局配置。
- `git diff 65cec86 14bfff0 --check`：退出 0。
- 真实正常清理通过不覆盖“回复丢失/DB 未提交”的新增崩溃窗口；上述反例由主审和独立审查分别确认。

## 给代码 Agent

仅修 G2-R3-01。先写物理成功但 DB 未提交的红测试，持久化完成记录并实现相同请求的幂等恢复；保留全部旧范围/代次保护。提交父/新 SHA、红绿与实际恢复时序，停下等复审，不进入 A4。无需重做整套双 workspace/镜像构建；若仅改管理记录与控制收口，针对新增故障路径验证即可。
