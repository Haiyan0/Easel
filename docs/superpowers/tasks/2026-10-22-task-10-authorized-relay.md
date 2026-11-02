# Task 10：授权代理、任务生命周期与可恢复事件流执行计划

> 执行方式沿用用户指定的外部代码 Agent。使用 superpowers:executing-plans 按步骤执行，先红测试，再最小实现；完成本任务后停止等待主审，不自动进入 Task 11。

**Goal：** 将已认证的 control 请求接到本人 worker，完成一次任务的接纳、配置固定、唯一执行、权限确认、停止、持久事件及消息读取。

**Architecture：** control 保持唯一认证、资源归属和 Store.reserve_run 接纳边界；每用户 worker 接收已验签的逐 Run 快照并独立持久化执行和事件。WorkerRelay 只转发已分类且完成资源校验的窄路由，HTTP 流连接与任务执行分离。

**Tech Stack：** 现有 FastAPI、HTTPX、SQLite、Ed25519、ACPAdapter。使用现有依赖，不新增消息中间件。

**Spec：** [原设计](../specs/2026-10-16-multiuser-workspaces-design.md) §6–9、[原计划 Task 10](../plans/2026-10-16-multiuser-workspaces.md)、[执行补充](../specs/2026-10-22-local-docker-development-gates.md)、[Task 9 验收](../audits/2026-10-22-task-09-accepted.md)。

## 依赖、基线和修改边界

- 基线：9813d42511e9ca2db84de5ca4bd0463bf5a6786c；先检查 HEAD/分支/工作区，发现变化报告关系，不 reset 用户改动。
- C:/Code/Easel，PowerShell，Python 固定 C:/Users/hyh/anaconda3/envs/Agent/python.exe。
- 原计划 Task 10 依赖 2+3+4+5+8+9。现在使用这些任务已审查的代码接口开展研发；Task 8 的真实部署门槛尚未通过，不能称依赖的发布验收已满足。缺正式运行参数时保持 503，不新增 production bypass。
- 本批允许本地两 worker、真实 HTTP/ACP 夹具与 fake provider 验证；不要求用户新提供服务器、重新寻找本地 Harness 或重跑大型 Docker 镜像。
- 不实现 Task 11 登录壳/前端缓存，不重做 Task 8 防火墙、XFS、Chromium 或最终镜像。真实部署状态原样保留。
- 仅显式路径提交实现、测试与必要产品说明；中文提交信息，不夹带主审已有 plan/spec、审计、任务单及 Harness 基线文件。

## 文件职责与现有接口

创建：

```text
easel/control/proxy.py                # WorkerRelay：授权、转发与生命周期协调
easel/worker/runs.py                  # 逐 Run 接纳、执行上下文与持久事件
tests/multiuser/test_proxy.py
tests/multiuser/test_runs.py
```

按需要修改：easel/control 的 app.py、sessions.py、store.py、snapshot.py、cli.py；easel/worker 的 config.py、guard.py、lifecycle.py、sessions.py；easel/cli.py、web/app.py，以及必要的测试/部署说明。若需调整 provisioner 的固定公钥装配，只扩展受管配置接口，不能接收客户端任意挂载、env 或 argv。不为本任务重构整个 web/app.py。

直接复用：

- Store.reserve_run(actor, conversation_id, client_request_id, input_digest, config_version)、mark_running、finish_run、mark_interrupted；它是唯一控制面 Run 幂等与容量账本。
- SessionService.assert_run/get_conversation、WorkspaceSessionStore 的 binding/messages/validate_message。
- RuntimeManager.ensure_ready(workspace_id, config_version) → WorkerHandle；stop/heartbeat 对具体不可复用 container_id 和 generation 的约束不得退化。
- BrokerService.issue/revoke_run 及现有配置快照、出口限制；签名使用现有 worker.config 的 Ed25519 envelope，不自行发明弱认证。
- AuthService 当前登录数据库检查；policy 精确 method/path；worker.guard 的终态请求头清洗语义。

产出：

- WorkerRelay(runtime, sessions, auth, **显式依赖)；async forward(actor, request) → Response。签名保留原计划三个核心依赖，配置、broker、签名器、HTTP client、DB runner 作为明确依赖，不从浏览器输入猜测。
- worker 内部 POST /internal/runs/{id}、GET /internal/runs/{id}/events（after event_id），以及 GET /internal/sessions/{id}/messages。停止/权限回答的内部路由同样精确登记并检查 workspace/run/question 绑定。
- public /api/chat、/api/chat/stream、/api/chat/jobs/{run_id}/stream、/api/chat/stop、question/answer 与 POST question/status；保持现有准确方法契约，不扩大前缀放行。
- 返回 admission 中的服务器 run_id；后续事件/停止/问题回答使用它，客户端 turnId 只映射为 client_request_id。前端实现留 Task 11，API 字段与兼容映射在产品说明中写清。

## 全局约束

- 每 workspace 默认最多 2 个活跃任务、整机 8 个；同会话仅 1 个 prompt。任务默认 1800 秒；等待权限确认及发布不算空闲；worker 无活跃任务/请求 900 秒才可回收。
- SSE 每 5 秒重新检查当前登录撤销、禁用与资源归属；静默上游也要在最多 5 秒内关闭被撤销的连接。关闭连接不默认取消用户后台任务。
- 快照、临时 broker token 及 agent env 限定 workspace/run/配置版本，管理员模型 Key 不进入 worker/响应/日志；新 Run 的配置不能改变旧 Run 已固定内容。明确 Run.config_version、worker 配置版本、能力覆盖版本与原生绑定契约版本各自含义，不能将常量绑定版本当作模型配置修订；接纳后快照及 broker 路由以固定修订/原子快照生成，不能重读 latest 拼成另一份配置。
- 一用户 worker 可执行两个不同会话的 Run。不得通过赋值全局 _WORKER_SNAPSHOT、os.environ 或共享请求字段来轮换本轮 token/model/身份；实际适配器、消息采集、权限问题、进程池键和事件文件使用不可变逐 Run 上下文。
- 如果将启动时单 Run 模型演进为 workspace 常驻服务，必须明确固定 worker 身份/只读公钥与逐 Run 验签接纳的边界；缺快照、公钥或身份不得回退 local，也不能因删除旧启动检查而失去每次执行前的时效、签名及绑定校验。
- SQL 仍在专用数据库线程，网络/容器等待不占 SQLite 事务或该线程；当前同步 SessionService.message_reader 接口不能直接在 DB 线程等待远端消息，应拆成短授权检查与异步 relay。
- 入站所有 Cookie、Authorization、X-Easel-*、Forwarded、X-Forwarded-* 清除后由服务端固定 WorkerHandle 重建内部头，覆盖 client defaults/cookie jar/auth；不跟随重定向，不接受客户端目标 URL。
- 内部接口不是公开路由；固定服务凭证之外，执行请求还须通过签名和 workspace/run/内容关联检查。同一签名不得替换会话、输入、任务类型或附件后执行；必要时以版本化签名字段绑定服务端 input_digest，不能只签配置而信任旁边的任意 payload。
- local 模式、旧格式兼容与已通过 Task 9 的消息/绑定约束保留；worker 不能用旧执行路由绕过新 Run 接纳和容量限制。

## Review Focus：必须先有红测试的五类边界

1. 两个不同会话并发 Run：同 worker 不串 token/配置/native ID/事件/消息；第三个 429，同会话第二个 409。
2. 双 POST、响应头丢失、control 重启：同 request_id/input 只有一个服务器 Run 和一个 prompt；输入不同为 409，不因 502/超时自动再次发布。
3. SSE 上游静默时撤销登录：5 秒内连接关闭；后台执行仍有终态和消息，重连只回放。
4. A/B 混用 run_id/question_id/turnId：停止、回答、消息、事件均在转发前拒绝，不探测另一 worker。
5. 已接受后崩溃、输出存储失败、终态竞争与 drain：终态明确、容量只释放一次、不把传输断线当任务完成，不重复外部副作用。

## 实施步骤与验收

### 1. 授权与一次接纳

- [ ] 写 test_foreign_run_is_rejected_before_worker_call、test_concurrent_same_request_admits_once、test_request_digest_conflict。使用两个 control Store 连接和计数型 transport：外空间 404/调用 0；同输入只生成一个 Run；异输入 409。
- [ ] 跑上述定向测试确认红；实现 WorkerRelay 和必要 Store 原子状态，不复制第二套 Run ID/容量账本。
- [ ] 在客户端输入纯校验、会话归属之后接纳，固定配置并获得 worker；快照签名和提交内容关联。首次提交、重试、查询必须区分，不用“先查状态、再执行”的非原子组合。
- [ ] 用两个真实 worker HTTP 服务与受控 ACP 适配器验证合法 A/B，各执行一次；重试只得到原 Run，服务器生成 session/run/native 关联，浏览器不能指定 native ID 或内部 credential。

### 2. 常驻 worker 的逐 Run 上下文与持久执行

- [ ] 写 test_two_runs_keep_independent_signed_contexts、test_tampered_run_payload_is_rejected、test_duplicate_submit_after_worker_reload_does_not_prompt；记录实际 RunRequest.env/native_id 与持久快照。
- [ ] 实现 easel/worker/runs.py，逐 Run 持久接纳、唯一执行、状态与事件；已在后台执行时 HTTP 断开不取消 supervisor；同 Run/nonce 重复返回原接纳/状态而不再 prompt。
- [ ] 精确装配内部路由与签名器/公钥装配、control CLI 的显式依赖；测试用临时密钥，正式缺依赖为 503。受管命令来自服务器受保护配置，不读用户 HOME patch 或 workspace ACP 文件。
- [ ] 两 Run 同时暂停于适配器入口，交错 token 更新、输出和权限问题，证明真实执行环境/写入不会被共享全局替换；重开保留绑定/消息/事件，不能静默新建原生空会话。

### 3. 统一任务与短请求租约

- [ ] 盘点现有 chat、skill、profile/build、analytics、publish 及其他会启动后台/浏览器任务的精确入口，建立任务类型表；写每类入口进入共同容量账本的参数化测试。
- [ ] 长任务统一经过 Run 接纳；后台排队/等待权限也占预算。同步短读写用 request lease/活跃请求计数，在异常/断线时可靠回收，回收不得删除仍有请求或活跃任务的 worker。
- [ ] 资源读写只能映射到 actor 本人的 worker；目录/Run 归属回控制库，worker 内资源落私有根。不存在的业务适配器明确 503，不用直通旧本地 handler 绕过任务账本。任务类型表中仍 503 的条目需报告，不能声称已装配全部能力。
- [ ] 测试 workspace 2/global 8、同会话互斥、awaiting_permission 不被 idle 回收、drain 拒绝新任务但保留已接受任务的结束与回放。

### 4. 持久事件、SSE、停止与权限确认

- [ ] 写 test_reconnect_replays_without_prompt、test_silent_sse_revocation_within_five_seconds、test_question_answer_is_run_scoped、test_stop_completion_releases_once、test_crash_is_interrupted_without_retry。
- [ ] 事件按 run_id 单调递增，事件日志及最终状态持久化；after/Last-Event-ID 游标校验，无效游标明确错误，重复回放不产生新事件或输出。不能用旧 turnId 直接定位任意日志文件。
- [ ] control HTTPX client 由 lifespan 管理；上游响应在 EOF/异常/下游断开时 close 并等待完成，不修改共享默认凭证；SSE no-buffer、heartbeat 与反向代理说明齐全。
- [ ] 停止及问题回答验证 actor/run/question 映射；一次回答的重试不能再次授权，未知/已过期/外空间问题按契约拒绝。停止、自然完成、崩溃与超时竞争只推进一次终态并释放一次额度；broker token 随真正终态撤销。
- [ ] 不能把建立 relay 超时当成“下游未执行”的证据；响应未知时查持久 Run 对账，不自动重发任务 POST。worker 实例失效将其残留任务标 interrupted，并保留审查信息。

### 5. 消息读取与整条调用链

- [ ] 将 GET /api/sessions/{id}/messages 接到本人内部快照路由，外空间/未知先 404，合法分页读取持久消息；读取、回放、列表不得启动 prompt。不得直接读控制服务器传入的任意文件根。
- [ ] 加真实 TCP HTTP 集成：登录 → 新建会话 → submit → admission/SSE → 断线重连 → 权限回答/停止 → 读取快照；A/B 两个独立 worker/私有目录与真实 control app，provider 可受控 fake。至少一条链使用实际 ACPAdapter 与夹具子进程，不能所有层都 MockTransport。
- [ ] 验证内部凭证/私钥/管理员 Key 不出现在 public 事件、响应、日志/异常和用户可读取的消息快照；逐 Run 内部签名包允许包含必要的最小作用域 broker token，但不得返回浏览器。最后复核 policy 默认拒绝、已有 worker 旧入口没有绕过预算。

## 最终验证与交付

按顺序运行，不并发启动多组共享进程级夹具的 pytest：

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser/test_proxy.py tests/multiuser/test_runs.py -q
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser -q
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/test_chat_question_replay.py tests/test_login_transport.py tests/test_web_security.py tests/test_agent_harnesses.py tests/test_harness_workspace.py tests/test_local_write_guard.py -q
git diff --check
```

如需 schema 迁移只追加，使用旧版本真实数据升级测试保留 Run/幂等、认证、配置、worker 租约和会话引用，不改历史 migration。上述命令的成功标准是无新增失败；既有环境失败需在明确父基线独立复现，不能仅凭旧报告豁免。

交付父/新 SHA、文件清单、逐步红绿、公开/内部路由表、逐 Run 上下文与签名内容绑定、持久状态图、配置/启动装配方式、真实 HTTP/ACP 计数证据、并发/重连/撤销/故障清理，以及仍 503/未执行部署门槛。建议提交：feat(multiuser): 接入授权任务与可恢复事件流。

完成 Task 10 后停止等待审计，不进入 Task 11，不将研发验证称为正式发布通过。
