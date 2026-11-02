# Task 10 R1 复审与 R2 定点返工

日期：2026-10-23。审查基线：07a202e4f5841856a2bdb390540615f3908079d2，父提交 7ad609a4df3403fbd74b1509ea7a01a2ee74983d。

结论：暂不通过。已有九项修复的正常路径保留；仅修复下列五项，不进入 Task 11。

依据：[Task 10 首轮审计](2026-10-23-task-10-review-round-1.md)、[执行计划](../tasks/2026-10-22-task-10-authorized-relay.md)。本轮只读产品代码、运行测试与临时探针，未改产品/提交，未运行 Docker、真实供应商或用户 Harness。

## 前轮关闭情况

| 前轮项 | 本轮判断 |
|---|---|
| R1-01 后台收尾 | 普通完成、禁用/撤销后收尾与 control 重启已接入；停止失败却释放的问题见 R2-01 |
| R1-02 drain | 已验证按原实例停止/回答/回放，新任务拒绝；原实例变更不误路由，可关闭 |
| R1-03 SSE 撤销 | 慢上游读取竞速和租约清理已修；慢消费者仍阻塞，见 R2-02 |
| R1-04 问题脱敏 | 递归脱敏已修，选项映射冲突见 R2-04 |
| R1-05 输出终态 | 完整保存后再 done、普通异常失败及已写完重开去重已修；写到半行的真崩溃见 R2-03 |
| R1-06 正式 broker 签发 | 正式 factory 已真实签发；并发失败一方误收尾另一方，见 R2-05 |
| R1-07 配置升级后幂等 | 顺序重试先查原 Run，不依赖新配置/worker，可关闭；并发准备见 R2-05 |
| R1-08 并发问题状态 | pending 集合状态和逐问题回答/超时已修，可关闭 |
| R1-09 默认头清洗 | 终态 Request 清洗与默认 auth 覆盖已复用，可关闭 |

## T10-R2-01（P1）：到截止时间但未确认停止，就释放仍执行的任务

定位：./easel/control/proxy.py 的 _reconcile_run，约 796–799、814–820 行。

两条路径仍违反“先确认执行结束，再释放容量”：

1. 事件请求发生连接失败等 DomainError，now >= deadline 时直接 _finish_system，甚至不尝试停止。
2. 事件请求成功且仍 running，发送 stop 后不校验实际终态，stop 失败也被吞掉，随后一律 interrupted。

独立审查者使用真实 Store/RuntimeManager/RunManager 探针确认：

```text
到期 + GET 临时连接失败：
worker=running control=interrupted active=0 revoked=1 stop_posts=[]
到期 + GET running + stop 连接失败：
worker=running control=interrupted active=0
再次对账：checked=0
```

撤销 token 不能证明浏览器、发布工具或其他外部副作用已停止。任务仍执行却被控制库永久排除出对账，后续可接纳额外工作，造成账本失真。

返工：截止时间只能触发停止/确认流程，不能单独充当执行结束证明。停止请求失败、响应不明确或返回仍 running/finalizing 时保持待确认停止及容量占用，持续按原不可复用实例对账。只有真实终态、明确原实例已停止/不存在等可验证证据后释放一次；不能把“bound_handle 不可用”、探针失败或数据库状态不是 ready 直接当作执行已结束。可先撤销凭证限制新模型调用，但不得因此提前释放任务预算。不得自动重发原业务。

验收：GET 超时/不可达、stop 超时/失败、stop 返回非终态、原实例确实退出四类反例。前几类执行仍在时 capacity 保持、第三任务仍拒绝且下轮继续对账；恢复连接并确认停止后才释放一次，token 与账本状态一致。跨 control 重启仍恢复待确认状态。

## T10-R2-02（P1）：慢消费者使生成器停在 yield，仍超过撤销窗口

定位：./easel/control/stream.py 的逐帧 yield（约 73 行）以及 control 的 StreamingResponse 装配。

新竞速解决了 relay.events 慢请求，但认证仍由同一生成器主动推进。独立审查者使用真实 Starlette StreamingResponse.stream_response，在 ASGI send 处阻塞模拟慢消费者；认证检查一旦执行就拒绝。真实时间 5.1 秒后：

```text
auth_checks=0 closed=False response_done=False
```

逐帧检查在 yield 返回之后才能继续；它不能在发送背压期间自行运行。

返工：让认证监视与响应发送/生成器推进独立，有权限在截止后取消该连接的发送和在途读取，等待清理；不能只给 relay.events 或下一次迭代加超时。保留现有慢上游清理逻辑，不取消 Run supervisor，不改变重连只回放语义。

验收：真实 ASGI 响应层阻塞 send，撤销/禁用后 ≤5 秒响应任务结束、没有后续私人帧，发送/读取子任务与请求租约均结束；同时验证后台任务继续、重新登录只回放。保留正常流、大页回放与慢上游测试。

## T10-R2-03（P1）：保存中真崩溃后整个会话不可读，恢复输出被删除

定位：./easel/worker/sessions.py 的 _append_locked/append_message_once（约 360–410 行）；./easel/worker/runs.py 的 _recover_finalizing（约 1365–1375 行）。

JSONL 仍直接追加，只有 OSError 会回滚；进程在追加中退出会留下半行。恢复遇到半行后直接 failed 并删除完整暂存输出，不能恢复既有完整历史。

独立审查者实际子进程在真实 os.write 写出前 40 字节后 os._exit(42)，父进程重开真实 RunManager：

```text
CHILD_REAL_EXIT 42
RECOVERED_STATE failed
RECOVERED_MESSAGES session_corrupt 500
RECOVERY_STAGED []
```

这是前轮明确要求的“保存中崩溃”，不是已完整追加后、提交 done 前的去重场景。当前后者通过，不能代表前者通过。

返工：为最终消息写入提供可恢复的原子提交协议，例如原子替换或持久记录已提交边界的日志方案。恢复必须保留先前完整消息，并能安全重试已暂存的本轮输出，只有提交一致后清除恢复材料；不重发 prompt、不重复助手消息、不提前 done。不能把任意损坏/错误身份 JSONL 直接截断或认领，恢复只可依据本系统可信、持久的提交记录。

验收：真实子进程分别在半行、完整行未完成提交、最终状态提交前后退出；重开后旧多轮完整历史仍可读，新输出按 runId 最多一次，有明确一致终态。坏文件/错误 workspace/version 仍拒绝，正常磁盘失败不假成功。

## T10-R2-04（P2）：选项映射没有按问题分区，脱敏 label 碰撞导致错答

定位：./easel/worker/runs.py 的 _public_questions（约 245–290 行）、RunManager.answer（约 1423–1429 行）。

当前 selections 为整批问题共用的扁平 dict；label 去重仅按 question_index，但保存 label→原文映射时不分问题。独立真实 manager 探针：同批第一问题选项为 allow secret-AAA（secret-AAA 是本 Run token），第二问题为 allow [redacted]；公开 label 相同，按公开 label 回答第一问题时被翻译成第二问题原文。

Codex requestUserInput 支持同批多问题，现有 QuestionCards.tsx 回传 label，故不是只能通过不规范客户端触发。opaque value 的正常路径通过，不代表兼容 label 路径正确。

返工：映射限定实际 questionId 与所属选项，区分 opaque 值和 label 名称空间；不能跨题覆盖、跨题选择，也不能让任意 label 假冒别题 opaque value。若同题 label 不能唯一对应，明确拒绝歧义回答或生成唯一展示标识，不猜测授权。保留脱敏与原始授权值的内部安全映射。

验收：同批多问题 label 相同/脱敏后碰撞、label 与 opaque 值同名、跨题 opaque 值、合法 label/value、重复回答。适配器收到准确原始选项且不泄漏 token，非法回答不消耗 pending 问题。R1-08 两个独立 ask 的状态回归保持通过。

## T10-R2-05（P1）：并发同请求准备阶段没有唯一所有权，失败一方中断成功一方

定位：./easel/control/proxy.py 的 submit_task，尤其 reserve_run 后 get_run_snapshot、_run_routes 及异常 _converge（约 423–455 行）。

两个提交都通过初次 lookup_run(None)，reserve_run 返回同一 Run，而两方都可能读到 snapshot=None，于是都签发 token。快照首写 CAS 在此之后才发生；其中一方签发失败会对整个共享 Run 调用 _converge(interrupted)，即使另一方已经固定快照并向 worker 成功 POST。

主审实际临时 probe 使用正式 build_relay_factory、真实 ConfigService/BrokerService/Store，DB runner 在两个 lookup/snapshot 读取点控制交错；第一方成功向受控 HTTP worker 派发，随后模拟管理员发布移除 chat 默认，第二方签发失败。输出为：

```text
RESULTS= [ {runId: 同一Run, state: running, duplicate: False},
           capability_unavailable ]
POSTS=1 ACTIVE=0 STATE=interrupted
```

该探针的 worker 为返回 running 的 MockTransport，不冒称真实 ACP 已执行；它证明控制面在已成功派发、没有停止确认时被另一重复请求提前收尾。角色/输入/幂等键均合法，配置变化可以在真实多连接环境发生。

返工：在签发 token/准备快照之前，为同一 Run 建立持久、原子的准备所有权，或等效地将准备与固定做成一致事务；其他并发提交只读原 admission，不重复铸造/替换 token。失败补偿只能作用于该调用仍持有且尚未移交执行的准备，不得中断另一方已派发的 Run、撤销它的有效 token。持有者崩溃由后台恢复，不盲目重发业务。不能仅再加一次 snapshot 查询缩小竞态窗口。

验收：两连接在 lookup/reserve/快照固定/派发点交错；签发期间管理员更新/移除配置、签发失败、准备进程崩溃分别覆盖。只有唯一准备者、一个固定 token/快照与一个 prompt；重复请求的失败不改变成功方 running 状态和容量，修订更新不替换原包。

## 主审独立验证及证据归属

- 定向 proxy/runs/relay_cli：25 passed，3.78 秒。
- tests/multiuser：882 passed、3 skipped、2 个既有依赖弃用 warning，67.69 秒。
- Task 10 指定六文件兼容回归：209 passed、6 skipped，17.29 秒。
- git diff 7ad609a HEAD --check：退出 0。
- R2-05 为主审实跑。R2-01/02/03/04 为独立审查者的临时探针，主审逐项交叉核对真实源码；R2-03 包含实际进程退出，不仅 mock 异常。上述探针均未操作真实用户资源。

## 执行 Agent 指令

以 07a202e4f5841856a2bdb390540615f3908079d2 为基线，只修 T10-R2-01～05 及必要联动。先核对 HEAD/工作区，保留主审 plan/spec 等改动。Python 固定 C:/Users/hyh/anaconda3/envs/Agent/python.exe，PowerShell。

五项逐一先补红测试，再实现并交付红绿；尤其不能以即时 transport、仅生成器单元测试或只在完整追加后崩溃代替本单故障场景。按依赖可拆显式提交，最终报告所有父/新 SHA、文件与准确调用链。

顺序运行新增测试、全部 tests/multiuser、Task 10 原指定六文件回归及 git diff --check；不并发污染共享夹具。只提交必要实现、测试、产品说明，不夹带审计/任务文档及主审改动。

已关闭项保持关闭，不重做 Task 8；非聊天业务仍 503 的范围如实列出。完成后停止等待复审，不进入 Task 11，不要求新服务器或真实付费模型调用。
