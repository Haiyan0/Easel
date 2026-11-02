# Task G2 主审 R1

基线：`d1fc8b16d6cb98092fa1ff9d0772c15bd40e35bd`。
审查范围：`ab5d7affa939a247e064cf4f16db68029898f658` 至 `3ce9fcf4a65823c12c19db64f7dddfdbfa612eb5`。

结论：G2 本轮不通过；以下四组定向修复后复审，不进入 A4。保留 gateway server、网络绑定、迁移及正常双 workspace 路径的成果；不回退已通过的 G1/A3。

## G2-R1-01（P1）：迟到的失败 abort 会删除新 owner 已采用的实例

位置：easel/control/runtime.py `_start` 524–527、`_cleanup_candidate`；deploy/multiuser/provisioner.py `abort` 2130–2145。

旧候选 worker_created/gateway_created 是历史事实，不是永久删除授权。当前激活失败直接据此 abort，没有复核/认领当前 owner/generation/创建尝试。provisioner 也只相信请求中的两个布尔标记，即删除指定实例。

主审与独立审查确定性交错：A 冷启取得候选、activate 挂起；120 秒租约过期；B 复用同一 worker/gateway，完成激活并写 ready；A 的 activate 迟到报错。结果：

```text
CURRENT_LEASE ready owner-b
OLD_ABORT_SENT True
ABORT_RESOURCE_IS_CURRENT True True CREATED_FLAGS True True
```

该 abort 的实际消费者会删除 B 当前使用的实例，即使 DB 仍 ready/B。

要求：清理必须具有可验证的当前创建尝试/所有权 fencing，而非仅靠 created bool。旧 owner 在实例已被另一代采用后不得 abort。不能只“先查一次 DB 再远程删除”，那样仍有检查与删除间的接管竞态；需要清理认领/停止承诺与新 owner 接管协议共同保证。将候选完整物理身份和必要的尝试标识在两阶段中记录清楚。保留精确 ID 删除及 worker/gateway 元组限制。

测试：两个真实 Store 连接、两个 RuntimeManager + 真实受限 abort 消费逻辑，覆盖上述迟到失败、取消，以及旧清理先取得认领后新 owner 尝试接管；已接管资源不得被删，新 owner 不能采用停止承诺尚未消解的实例。额外覆盖旧 gateway 已消失而同 worker 配了新 gateway 的 stop：旧元组不得据“旧 gateway 不存在”继续停止新元组正在使用的 worker。

## G2-R1-02（P1）：refresh 崩溃恢复被误当冷启动，结算仍在运行的 Run

位置：easel/control/store.py `begin_worker_gateway_refresh` 903–907 与 `claim_worker_start` 的 starting 过期分支；easel/control/runtime.py `_start/_fail_start`。

refresh 将有实际 worker、活跃 Run 的 ready 租约转为 starting。控制进程崩溃后，starting 过期会调用 `_claim_new_generation` 清空实例身份；新进程随后一次临时 gateway ensure 故障走冷启动 `_fail_start`，把仍运行的 Run 改 interrupted。

主审真实临时 Store 探针：先 ready + running Run，再 staged refresh 模拟进程退出，推进时钟过租约，令下一次 gateway ensure 暂不可用：

```text
RUN_AFTER_REFRESH_CRASH interrupted STOP_CALLS []
```

没有任何物理停止证据，不能收回 Run 容量/撤销 token 并当作执行结束。

要求：持久区分冷启动与已有 worker 的 gateway refresh。过期 refresh 应保留 worker ID、活跃 Run、原生绑定/历史和必要 gateway 元数据，按实际实例对账/重新握手；临时故障仅 503/unavailable，不转冷启动失败。只有可信 worker 终态或已确认停止，才可按既有规则中断结算。不要用时钟推进本身作为执行终止证据。

测试：真实旧进程 staged 后退出、两个连接/新 owner 恢复，覆盖 inspect/ensure/activate 临时失败、恢复成功、同容器新 boot、确认 worker 真退出。未停止时 Run 始终 running/awaiting_permission，容量释放计数和 broker token 不变；恢复后 GET/事件可继续，POST 不自动重发。

## G2-R1-03（P2）：ready 快速路径健康判断与状态恢复不完整

位置：easel/control/runtime.py `_ready_gateway` 349–351，`_report_worker_matches`、`_handle_from_lease`。

两个已复现分支：

1. 实际 report 里 worker/sidecar ready=false，但身份与 gateway 元数据匹配，仍返回 ready handle。此处不能只比身份。
2. DB 标记 gateway unavailable 后，物理 gateway 原样恢复为 available、boot 未变，快速路径直接 `_handle_from_lease`；该函数拒绝 unavailable 租约，反复报 worker_identity_mismatch，不执行重新同步，永久无法自愈。

主审输出：

```text
UNREADY_WORKER_RETURNED True
RECOVERY worker_identity_mismatch unavailable ENSURE_CALLS 1
RECOVERY worker_identity_mismatch unavailable ENSURE_CALLS 1
```

测试 GatewayFake.inspect 把物理 available 强行依赖 DB gateway_state，掩盖了第二分支；真实 provisioner 无此依赖。

要求：明确 worker/sidecar 的物理健康与 gateway 的 available，再与 DB 状态分别判断。未就绪不得返回可派发句柄，也不得误删 worker；物理恢复需通过正确 CAS/对账恢复 DB，而非抛永久 identity mismatch。测试假边界应独立维护 Docker/进程状态，不能依据被测 Store 的结论构造“物理事实”。

测试：ready=false + gateway=true、DB unavailable + 物理已恢复同 boot、全好、gateway 隔离、新 boot，分别验证拒绝/自动恢复/不重建和在途 Run 保留。

## G2-R1-04（P2）：gateway create 超时/取消仍无本轮资源追踪

位置：easel/gateway/runtime.py `_create` 约 792–800（await create 在 try 之前），其余启动失败 cleanup。

当前只有 create 成功返回 stdout 后才得到实例 ID，且尚未进入清理 try。daemon 创建完成但 CLI 超时/取消会直接抛出，无 cidfile/创建尝试记录/唯一清理；start/connect 等 await 的取消也未像 A3 一样协调底层 CLI 结束。异常清理的删除失败还会被吞掉。

主审注入 create 异常边界确认，超时后没有任何 inspect/stop/rm/追踪处理：`CREATE_TIMEOUT_NO_RECOVERY True`。这再次落入 A3 已修的“CLI 未返回不等于 daemon 未创建”问题。

要求：gateway 全创建尝试纳入有归属追踪的唯一清理，等待底层命令终结，可靠取得本轮不可复用 ID（例如受保护 cidfile/attempt 记录）；不按当前同名容器兜底强删。失败清理错误明确返回并保留材料，不能吞掉后自称已清理；不得为清理新 gateway 而停止已有 active worker/其他代次 gateway。与 R1-01 的接管 fencing 配合。

测试：create 写 ID 后 CLI 超时、取消后才返回/写 ID、start/connect/配置/激活失败、重复取消、清理失败/daemon 不可用、同名后继实例。至少一项真实 Docker CLI 超时实测；不要求为修此项重新构建整个 Harness 镜像。

## 主审验证及证据边界

- G2 新增四文件：**28 passed**（0.97 秒）。
- provisioner/runtime/store/stop-recovery + G2 四文件 + 文档：**276 passed, 1 skipped**（7.84 秒）。
- `git diff d1fc8b1 3ce9fcf --check`：退出 0。
- 主审复跑四段恢复/交错探针；使用临时数据库、现有 Fake 边界及真实 RuntimeManager/Store，未操作 Docker/开发者资源。独立审查另复现状态机缺陷。
- 本轮未重跑 377 秒双 workspace 正常路径或构建镜像。其实施方证据保留，但不能证明上述故障交错已通过。

## 交付

仅修 G2-R1-01/02/03/04，先补确定性红测试。必要变更限 gateway runtime、provisioner、RuntimeManager/Store/受限协议及对应测试/契约/证据；只追加确有必要的新迁移，不重写已交付迁移。保护主审文档。

修复后复跑指定回归与真实双 workspace、同容器新 boot、停止/恢复场景，精确记录是否为最终提交代码/镜像。提交父/新完整 SHA、红绿、所有权/恢复时序和清理结果；停下等复审，不进入 A4。
