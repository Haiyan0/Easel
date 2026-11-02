# Task A6 首轮复审：同会话取消清理期间的互斥回归

基线：`1e70afab0a55bee4cf41f985b691b6ebbf82c604`。
审查提交：`acef78b3ae3b1b1f318a297bf2c5cb06200aa76b`。

## 裁决

暂不通过，仅修 A6-R1-01 后复审，不进入 A7。已有双用户/真实 Harness/ngrok 浏览器证据保留，不推倒重做 A6；真实 DeepSeek 请求仍为 not-run，不能算成已通过门槛。

本轮 control 实例恢复、同 workspace 当前实例历史读取及明确派发拒绝补偿未确认独立阻塞；独立 reviewer 已核对准备所有权/派发 CAS 与幂等路径，未将无法从正式路径触发的假设列为问题。

## A6-R1-01（P1）：持久终态不代表 executor 已退出，不能据此释放活会话锁

位置：`easel/worker/runs.py:1185`–`:1193`；关联 `RunManager.stop` 在 `:1543` 附近先写 cancelled、再取消并等待 supervisor 的流程。

新增自愈代码在 `holder_state in TERMINAL_STATES` 时直接移除 `_active_conversations`，即使对应 `_tasks[holder]` 仍未结束。正常 stop 会先写 cancelled，再 `task.cancel()` 并等待 executor 的取消清理。真实 ACP 仍可能等待 session/cancel、子进程或连接关闭；这个期间第二个同会话请求会把第一轮的活锁当陈旧锁清掉，开始新的执行。

主审与独立 reviewer 分别使用真实 RunManager、临时私有存储和受控 executor 确定性复现。第一轮已绑定固定 native session；收到取消后在清理 gate 等待，此时提交第二轮：

```text
FIRST_STATE=cancelled
FIRST_STOP_PENDING=True
SECOND_STATE=running
OVERLAPPING_EXECUTION=True
```

第二轮确实进入 executor，而第一轮 stop 尚未返回。正式 control 也可由并发 events/后台对账先看到 cancelled，收口旧 Run 后接纳下一轮，因此不能依赖 UI 等待 stop 的单一路径避免窗口。后果包括同 native session 的重叠访问、取消与新 prompt 相互干扰，或第二轮因 ACP 连接仍 busy 而无故失败。

已有 A6 正常停止测试在停止收尾后再提交，启动失败测试也有等待/新 turn 重试，没有覆盖“持久终态已写、物理清理尚未完成”的窗口。现有绿色用例不能排除此回归。

## 最小修复

- 本地 holder task 仍在执行/取消收尾时，继续保持同会话互斥；不能仅凭 registry cancelled/failed/done 解除活锁。
- 只在能够确认旧 holder 已结束时修复真正的陈旧锁，并保留 A6 的启动失败空 native 绑定恢复。不要直接撤回所有自愈，使原始启动失败再次永久 busy。
- 第二轮被拒绝时不得执行 adapter、追加用户/助手消息或改绑 native ID；保持原第一轮清理的所有权。关闭第一轮 gate 后，新 Run 可正常启动，原 native ID 与历史保持。
- 无需扩大到多用户 schema/新状态机，不改 G1/G2/A4 的已验收行为，不因为取消慢而强行释放会话或终止无关进程。

## 红绿与复审验收

1. 先写真实 RunManager + 取消清理 gate 红测，断言第一轮 stop pending、registry terminal、holder task 未完成；第二轮不得进入 executor，应明确 busy/不可接纳，且消息/绑定无副作用。实际断言执行计数与清理 gate，不能只加 sleep。
2. 放行 gate 后等待 stop 完成，再提交新 Run，正常完成/续接；原 native ID/历史不被覆盖。
3. 保留真正 task done/无活动执行的陈旧锁自愈、启动失败后空绑定的恢复、普通完成/停止/超时/权限等待等路径。
4. 至少用真实 ACP 子进程或现有 RPC fixture 的受控慢取消证明：新 prompt 不在旧进程/连接清理确认前启动；不靠取消超时绕过安全条件。
5. 跑 worker runs、ACP、session、control relay/停止/SSE 受影响回归；浏览器定向复跑 UI stop 后同会话续接及启动失败恢复。前端/ngrok 未改时不强制重做整套公网矩阵，原 A6 HTTPS 证据保留。

## 主审实际验证与边界

- proxy/runtime-gateway/messages-relay/runs/event-stream 五文件：**75 passed（17.83 秒）**。
- 主审真实 RunManager 临时目录故障探针如上，结束后放行并等待全部任务、关闭 manager，临时目录自动删除。
- 独立 worker 审查复现相同反例；独立 control 审查 **34 passed**，未确认其它阻塞项。
- `git diff 1e70afa HEAD --check` 退出 0。
- 本轮主审未重跑 A6 Docker/浏览器/ngrok 或真实供应商；作者 4 passed/1 skipped 的浏览器矩阵是作者实测，不能替代上述新增取消窗口验证。
- 原错公钥 UI 反例/运行期缺 broker UI 场景的未单独执行情况继续如实保留；没有把它们写成真实浏览器通过。真实供应商、A7、原生产 Chromium/XFS/Linux firewall/IPv6 门槛均未关闭。

## 给代码 Agent

以 `acef78b3ae3b1b1f318a297bf2c5cb06200aa76b` 为基线，仅修 A6-R1-01。按上述故障 gate 先红后绿，交付父/新完整 SHA、必要文件、执行计数与物理关闭证据、定向回归和资源清理。保留主审文档，不进入 A7。
