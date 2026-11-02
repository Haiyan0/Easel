# Task 10 首轮审计与 R1 返工单

日期：2026-10-23（Asia/Shanghai）。结论：暂不通过；下列九项同批返工，完成后停止复审，不进入 Task 11。

## 审查范围与证据边界

- Task 10 基线：9813d42511e9ca2db84de5ca4bd0463bf5a6786c。
- 审查 HEAD：7ad609a4df3403fbd74b1509ea7a01a2ee74983d，覆盖 ca13d49 至该 HEAD 的整批改动；不只审最后一步的 13 个文件。累计 22 文件，+6559/-62。
- 依据：[Task 10 执行计划](../tasks/2026-10-22-task-10-authorized-relay.md)。
- 主审未改产品代码或提交；运行现有测试、临时目录探针，另由两名独立审查者复核 worker 与 control 生命周期。没有运行 Docker、真实供应商、用户 Harness 或触碰真实 workspace。
- Task 8 的真实部署门槛保留，不作为本轮故障的借口，也不要求本轮补服务器。Task 11 前端尚未实现不构成这些后端故障的免责条件。

## T10-R1-01（P1）：控制面终态依赖浏览器轮询，断线及派发故障永久占用额度

定位：./easel/control/proxy.py 的 submit_task、_sync_worker_state、events/stop（约 284–425 行）；./easel/control/app.py 的 lifespan；Store 的 dispatch 状态。

实际只有 events/stop 会同步 worker 终态，未发现与客户端连接独立的持久对账。主审重跑独立审查者的临时探针，使用真实 Store、RunManager 与现有 WorkerTransport：两个任务不打开 SSE，worker 均 done，但 control 均 running，active=2，第三个请求 workspace_capacity，broker.revoked=[]。

派发故障同样不收敛：POST ConnectError 后首次 502，control 重建 relay 后同输入返回 202 duplicate/reserved；events、stop 均得 worker 404，但控制库仍 reserved/dispatching，占 1 个额度。禁止自动重发是正确的，永远悬挂不是持久恢复。

返工：增加独立于 SSE 和登录状态的任务监视/持久对账，应用重启后恢复；完成、失败、崩溃及派发结果未知均有明确收敛路径。收尾必须可在用户禁用/登录撤销后继续进行，终态只写一次、容量只释放一次并撤销 broker token。不能将 HTTP 断线当任务完成，不能以 TTL 删除记录代替确认执行已结束，也不能盲目重发发布任务。

派发不确定时依据原 Run、nonce、不可复用 worker 实例 ID 查询确认；不得查新实例后把旧执行当不存在。不可达时持续对账，任务截止/明确实例失效时按已确认停止/中断流程收尾，不能永久只返回 duplicate。保存足够持久信息，支持 control 重启后恢复同一判断。

验收：无任何浏览器 events/stop 请求的两轮任务自然结束后，控制库自动终态、release_count=1、token 撤销且第三轮可接纳；撤销登录/禁用用户后同样完成。派发前连接失败、已执行但响应丢失、control 重启分别覆盖；prompt/外部副作用均不重复。

## T10-R1-02（P1）：drain 拒绝了既有任务的停止和回放

定位：./easel/control/proxy.py 的 _ensure_ready、stop/events/answer；./easel/control/runtime.py 的 ensure_ready/drain。

已接受任务的 stop/events 仍调用 ensure_ready，而 RuntimeManager 对 draining 拒绝。主审实跑真实 RuntimeManager 租约状态迁移后，stop 和 events 均返回 worker_draining/503。drain 又等待活跃任务归零，因此既无法停止，也无法通过回放同步自然完成，形成收尾阻塞。

返工：区分新接纳就绪检查与既有任务操作。已接受的 Run 按持久实例绑定获取仍存活的 worker，draining 时允许结束、回答和回放；这些操作不能创建/升级/替换实例。若原实例确认失效，进入中断与恢复流程，不发到另一实例伪装为原 Run。保留新任务在 drain 时拒绝。

验收：真实 RuntimeManager + relay + worker：drain 拒绝新任务，但原 Run 可答权限/停止/自然完成/回放，控制库收尾后 drain 才结束；同时覆盖旧实例失效，不误路由到新代次。

## T10-R1-03（P1）：慢上游阻塞 SSE 撤销检查

定位：./easel/control/stream.py 的 run_event_stream，尤其末尾 await relay.events（约 82 行）。

4 秒复查常量只在串行循环里生效。主审实跑真实时间探针，使 relay.events 等待 8 秒，recheck 一旦运行即抛 unauthorized；5.1 秒后流仍未关闭，auth_checks=0。默认 HTTP 超时或容器就绪等待均可能超过撤销窗口。

返工：认证截止时间独立于事件请求、心跳及慢消费者推进；用受控并发/截止时间竞速，使慢上游无法遮蔽撤销。撤销后关闭对应流及在途读取资源并等待必要清理，后台任务保持持久执行；不取消整个 Run、不吞后台任务异常。

验收：真实时间、阻塞 HTTP/事件页、静默及缓慢消费等场景，撤销/禁用后 ≤5 秒关闭连接，没有后续私人事件；在途 HTTP 任务/请求租约正确清理，恢复登录后只回放、不重发 prompt。

## T10-R1-04（P1）：权限问题事件绕过 token 脱敏

定位：./easel/worker/runs.py 的 _ask_for（约 987–1002 行），以及 ./easel/harnesses/acp.py 的 respond。

普通 emit 经过 _scrub，但 _ask_for 把 questions 原样写入问题记录和公开 question 事件。独立 probe 将该 Run 的 broker token 放入权限问题文本，manager.events 返回原值。实际 ACP 源码将 toolCall.title 拼入问句并调用 ask，故该数据来源真实可达；本轮实际 ACP 子进程权限探针未成功到达该点，不将其当作已实跑证据。

返工：对问题正文、选项、标题及其他嵌套文本采用同一内部凭证脱敏边界，持久公开事件和公开 question status 均不能泄漏。脱敏不能破坏实际授权选项对应关系；保留内部安全映射或明确拒绝无法安全展示的问题，不把浏览器展示文本误当原始授权值。不要仅在浏览器渲染时隐藏。

验收：通过实际 ACP 夹具产生包含该 Run token 的 toolCall.title/选项，经 control SSE/问题查询/日志与用户消息快照均无 sentinel；合法一次性回答仍对应原选项。普通 emit/错误脱敏回归通过。

## T10-R1-05（P1）：输出未保存就持久发布 done

定位：./easel/worker/runs.py 的 _supervise（约 1053–1065 行）、fail_after_output_error。

当前先 finish_if_active(done)，再 append assistant；失败后把终态 done 改成 failed。独立注入助手写失败，持久事件实际为 accepted→running→token→done(completed)→failed(output_storage_failed)，消息只有 user。消费 done 即结束的客户端会误报成功；done 提交后崩溃还可能永久缺少最终快照。即使同一进程事件循环在同步写中不能插入 GET，错误的 done 事件仍已持久存在。

返工：成功终态和对应最终消息必须有正确的持久顺序/恢复协议：必要输出保存成功后才提交唯一成功终态；失败只发布失败，不先发布成功再更改。处理停止与保存竞争、重复收尾和保存中崩溃，不能重复追加助手输出或在取消后伪造成功。

验收：助手磁盘失败时无任何 done 事件，终态 failed；正常完成消息存在后才可见 done；在保存/终态提交间故障重开，状态与快照可恢复且输出不重复；控制面和 SSE 都不能观察到假成功。

## T10-R1-06（P1）：正式 CLI 未接通逐 Run broker token 签发

定位：./easel/control/cli.py 的 pinned_config（约 113–123 行）；./easel/control/proxy.py 的签名/派发路径。

正式 CLI 永远返回 routes={}，WorkerRelay 只保存 provider 提供的 routes，没有调用 BrokerService.issue。报告如实披露了这一点，但它属于 Task 10 已要求的生产装配，不是 Task 11/未来受管路由任务。当前集成将预置 token 放入签名包只能证明传递，不能证明真实 broker 为当前 Run 签发并授权。现有 broker 参数仅在终态撤销时使用。

返工：从实际受管配置/能力路由为已接纳的 Run 签发最小 channel/workspace/run 范围的 token，并将必要 broker 目的地及 token 固定进该 Run 的签名包。原子固定配置/凭证，不从 latest 重读后混配；同请求重试及重启复用原签名包，不换 token/配置。缺必要配置时明确 capability_unavailable，不以 routes={} 接纳看似可运行的在线主模型任务。签名私钥/管理员 provider Key 仍不下发。

验收：使用正式 CLI factory/装配、真实 ConfigService/BrokerService/Store、受控 provider transport，Run 取得的实际 token 可调用该 Run 的批准 broker 路由；错 workspace/run/channel 拒绝，终态后拒绝；并发重试和管理员更新不会替换已固定快照。不得靠自定义测试 config_provider 返回静态 token 证明此项。

## T10-R1-07（P2）：配置更新破坏已有请求幂等

定位：./easel/control/proxy.py 的 submit_task（约 284–307 行）。

重试先读取最新配置并 ensure 最新 worker，再查旧 Run，随后 configuration_changed/409。主审独立 probe：首次 revision=7 的同请求完成后，将 provider/runtime 改为 revision=8，同 payload 重试得到 configuration_changed/409，拿不到原 admission。活跃旧任务时还可能先触发配置升级/drain 检查。

返工：授权和输入摘要确定后，先按 actor/client_request_id 查询原幂等记录；同输入返回原 Run/原快照，不要求新修订/新 worker 可用，不换配置也不重发。异输入仍 409，不能因发现已有 ID 而跳过当前 actor 归属校验。只有真正新请求才读新配置并启动/接纳。

验收：旧 Run 活跃、已完成、控制面重启、管理员配置更新/新 worker 不可用等情形，同输入均返回原 runId；实际派发 POST 数始终 1，签名包/token 字节不变，异输入 409。

## T10-R1-08（P2）：首个问题回答使其他并发权限问题过期

定位：./easel/worker/runs.py 的 RunRegistry.answer（约 608–634 行）、_ask_for。

独立 probe 在同 Run 同时 ask 两个问题；回答第一个后 registry 无条件置 running，第二个 answer 因非 awaiting_permission 被标 expired、返回 question_expired/409。底层 RPC 可并发处理反向请求，不能假定始终只有一个问题。

返工：Run 状态与 pending 问题集合一致，至少一个未决问题时保持 awaiting_permission；回答/过期/停止/重试原子更新，不作废其他问题，不重复授予权限。control 同步状态也应使用真实 worker 状态，不因 answer 成功一律强制 running。

验收：实际同 Run 两问题并发、逐个回答、重复回答、其中一个超时/停止；第二个问题仍可合法回答，只有最后一个未决问题结束后恢复 running；外空间/错 Run 拒绝不变。

## T10-R1-09（P2）：新异步 relay 未净化最终 Request 的默认凭证

定位：./easel/control/proxy.py 的 _send（约 519–560 行）；参考已通过的 ./easel/worker/guard.py 的终态清洗语义。

主审独立 MockTransport probe：给 AsyncClient 配默认 Basic auth、Cookie、Authorization、Forwarded、X-Easel-Role，调用真实 relay._send，实际下游包含 authorization/cookie/forwarded/x-easel-role。只自行构造 headers 不会清除 client.build_request/send 后合并的默认值。

当前正式 lifespan 创建的是无这些默认凭证的 client，因此此证据不是“普通浏览器现在即可伪造角色越权”；它是交接单明确要求的终态净化契约未实现，且重复了先前已修复的同步转发缺陷。

返工：清洗最终合并 Request，再按固定 WorkerHandle 重建内部身份，同时覆盖 send 时默认 auth；禁止重定向，不能临时清空再恢复共享 client 状态。复用/抽取已有清洗规则，避免同步异步两套规则漂移。

验收：实际异步下游捕获默认 headers/cookie jar/Basic auth 均被剥离或覆盖，内部固定头正确，client 原 defaults 不变，连接池可复用。保留 trust_env=False 的正式内部直连回归。

## 非阻塞范围说明

- skill/profile/analytics/publish/browser 的任务表 currently implemented=False，实际公开入口仍 503。任务单允许未装配业务明确 503，因此本轮不临时要求实现全部业务；但下一份交付与产品说明必须逐类列出，不把“登记任务表”称作这些能力均已支持共同账本执行。
- Task 10 接纳/回放架构、实际 TCP/ACP 基础链路、跨空间拒绝等已有工作保留，不推倒重写；只修本单具体问题及必要联动。
- Task 8 未通过的真实部署项仍保留。无须本轮购买服务器、真实收费调用或大镜像重建。

## 主审实际验证

- 定向 proxy/runs/relay_cli：15 passed，2.04 秒。
- tests/multiuser：863 passed、3 skipped、2 个既有依赖弃用 warning，61.21 秒。
- 指定六文件回归：209 passed、6 skipped，16.74 秒。
- git diff 9813d42 HEAD --check：退出 0。
- 主审实际重跑生命周期探针（退出 0）：

```text
NO_POLL ['done', 'done'] ['running', 'running'] 2 workspace_capacity broker_revoked= []
DISPATCH_FIRST upstream_unavailable
DISPATCH_RETRY state=reserved duplicate=True
DISPATCH events 404 not_found
DISPATCH stop 404 not_found
DISPATCH_FINAL reserved dispatching 1
DRAIN stop worker_draining 503
DRAIN events worker_draining 503
SSE_BLOCKED elapsed=5.1 closed=False auth_checks=0
```

另实际执行默认请求头与配置重试探针：四类禁用头均到达下游；更新修订后同请求得到 configuration_changed/409。worker 三项由独立审查者临时 probe 确认，实际源码可达性已交叉核对；不将未成功的 ACP 权限子进程探针称为通过。

## 给执行 Agent 的 R1 指令

以 7ad609a4df3403fbd74b1509ea7a01a2ee74983d 为返工基线，先核对分支/HEAD/工作区，不 reset 主审或用户改动。固定 Python C:/Users/hyh/anaconda3/envs/Agent/python.exe，PowerShell。

先为九项各补实际反例并确认红，再精确修改实现。生命周期、输出终态、token 装配等可以按关联分提交，交付最终 SHA 与每项红绿；不能用只断言 mock 函数被调用替代真实持久状态/下游请求证据。

运行新增定向测试、全部 tests/multiuser，以及 Task 10 原指定六文件回归；顺序执行，不并发污染全局夹具。运行 git diff --check。仅显式路径提交必要实现、测试及部署说明，不夹带本审计、任务单、主审 plan/spec、Harness 基线。

交付还需列出后台对账启动/重启/退出方式、原实例绑定与 drain 收尾、真实 broker 签发链、SSE 清理、终态及问题状态图，以及所有仍 503 的非聊天业务。完成后停止等待复审，不进入 Task 11，不自行宣称 Task 8 或正式发布通过。
