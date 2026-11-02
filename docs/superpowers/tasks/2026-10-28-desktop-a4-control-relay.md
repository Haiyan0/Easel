# Task A4：公开 control relay、gateway 签名与逐 workspace broker 装配

基线：`785f556bc81c464f59de5105a02c12305b2e6265`。

必读：AGENTS.md、Desktop 装配计划 Task A4、A2/A3/G1/G2 验收单、deploy/desktop/topology.json、WorkerRelay/RuntimeManager/Gateway 的实际接口。先报告 HEAD 与工作树，保护主审文档与既有资源。

## 目标

让正式 `serve-control` 的已认证会话/任务/事件/停止/权限回答/历史入口，实际经过 G1/G2 gateway 到固定 workspace worker；逐 Run 配置固定该 workspace sidecar broker 地址、channel 和最小 token。现有同源前端可消费已装配聊天接口。

首轮以真实 ACP 子进程协议夹具验收，不冒充真实 DeepSeek Harness。A5 才构建/验证真实 DSH，A6 做完整双用户浏览器和真实供应商闭环。产物/发布等未装配路由仍明确 503，不借机扩大本轮。

## 文件职责

- 新建 `easel/control/gateway_client.py`：复用 G1 control 签名工具，负责固定 gateway 目标、最终 URL/body 序列化与 HTTP 发送；不复制验签或另建 Run 状态机。
- 修改 `easel/control/proxy.py`：以明确 transport 模式使用签名 gateway client，保留现有 Run 接纳/幂等/派发/对账/问答/历史和请求租约。
- 修改 `easel/control/runtime.py` 及确有必要的受限 provisioner 协议：为既有 Run 提供“仅恢复同 worker 的 gateway”能力和明确暂不可用语义，不允许通用 ensure 启动新 worker 冒充旧实例。
- 修改 `easel/control/cli.py`、app.py、snapshot.py：正式 factory/受管配置加载装配；少量类型调整确有必要才做，不默认引入新迁移。
- 新建 Desktop 完整开发编排/启动入口（例如 `compose.workspace.yaml`、`Start-WorkspaceEasel.ps1`）；复用 A1 的登录配置/密钥保护与 G2 的 WSL socket，不改坏现有 login-only 使用方式。
- 新建 gateway client 与正式 relay 装配测试、真实 control→gateway→worker→sidecar→broker 集成测试；更新手册与本轮证据。测试夹具只能位于 tests，不成为生产运行时默认执行底座。

## A4.1 正式 gateway client

- [ ] 先写红测试：gateway-required 正式 factory 仍直连 worker 或发送 service token、缺私钥/句柄字段时退回旧路径必须失败。
- [ ] 明确区分直接 transport（既有本地/测试兼容）与 gateway-required。后者 URL 只取受信 handle.gateway_upstream，audience 固定 workspace、实际 gateway ID、boot generation、worker ID、config_version；缺字段/尚未 available 明确拒绝，不静默降级。
- [ ] JSON payload 仅序列化一次为严格 UTF-8 bytes；GET 无 payload 为真实空 body。URL/path/query 在一处规范化，最终 HTTP Request 的 URL 和实际 body 就是签名输入。client params/headers/cookies/auth 默认合并后不能改变签名语义；最后净化并注入单个断言头，控制面不把浏览器 Cookie、worker service token 等发给 gateway。
- [ ] 七项内部路由全接：submit、events、stop、questions/status、questions/{id}/answer、session messages、必要 lifecycle。query/权限 ID 百分号编码和 G1 契约一致；包含 nonce 的对账请求也必须签名正确。
- [ ] 不重试 POST、不跟随重定向。保留 JSON 事件语义；公开 SSE 仍由现有 control 层管理，逐请求 30 秒断言只限制接纳，不把正常长请求在 30 秒到点时伪结束。连接/读取有界、请求租约和响应清理保留。
- [ ] 无效签名/503/429/协议错误对用户返回脱敏稳定码，保留合理 Retry-After，不向前端泄漏 gateway/worker 地址、ID、断言或 token。

## A4.2 既有 Run 的 gateway 故障恢复

- [ ] 先补确定性交错：Run running/awaiting_permission 时 gateway 不可用、refresh starting、新 boot、隔离。公开 events/stop/answer/history/后台 reconciler 不得据此误判 worker 消失、调用停止或结算 interrupted，不撤销 broker token、不释放运行容量。
- [ ] 当前 RuntimeManager.bound_handle 的 None 同时可能表示 gateway 不可用；与 relay 统一成明确暂不可用（503）和已证实 worker 身份失效的不同结果。不要仅给前端换错误码，而后台仍将 Run 结算。
- [ ] 旧 Run 操作只能绑定原 worker ID/config；恢复只允许重配/更换对应 gateway，不创建/升级/替换 worker，不将旧 Run 转发到新 worker。必要时新增受限 gateway-only 恢复协议，所有目标由已核验实例派生，不能暴露任意 Docker 参数。
- [ ] 同 worker 新 gateway boot/实例恢复后，每次请求读取当前已 CAS 确认的 audience 并重新签名，GET/事件按 after 继续，POST 不因断言过期/断连自动重发。worker 确认终止才沿原有确认停止、容量和 token 撤销规则结算。
- [ ] gateway 生命周期/attempt/tombstone 修复均保留；模拟 control/provisioner 重启、迟到回应、Store 不同连接，确保不回退为旧故障路径。

## A4.3 每 workspace broker 与受管配置

- [ ] 管理员配置显式声明 broker 目标模式：旧固定 URL 模式可保留兼容；Desktop gateway-required 使用服务端 `workspace-sidecar` 模式。可采用 `broker: {mode: "workspace-sidecar", channel: "chat"}` 的明确标签结构，validator/factory/文档统一固定；不引入自由字符串模板、客户端 URL 或任意变量展开。
- [ ] 根据认证后重查的 workspace UUID，从已批准拓扑派生 `http://easel-sidecar-<workspace_id>:8899`，形成该 Run 固定的 BrokerDestination。绝不能用请求体传入 workspace/host/channel；解析发生在正式 pinned config provider，不在测试里另造一条生产不会用的签发链。
- [ ] `BrokerService.issue` 仍在接纳后按该 Run/workspace/channel 和原子配置快照签发最小 token；旧 Run 地址/路由/凭证快照固定，不随管理员默认更新漂移。worker 只收到 broker URL/短期 Run token，不收到 provider Key。
- [ ] 正式 serve-control 显式装配前端目录、master key、签名私钥、受管 Agent、gateway-required provisioner、受控出口策略/代理。私钥和 master key 只给 control，worker/gateway 仅只读公钥；argv/快照/公开设置不出现 provider Key。
- [ ] 运行本轮测试前可通过安全配置渠道使用测试 sentinel 默认模型；网络出口/无 broker channel/不完整签名配置继续失败关闭。测试 provider transport 可为 fake，但需如实标注，不触发真实计费。

## A4.4 可运行的 Desktop 开发编排

- [ ] 从 WSL CLI 启动需要 socket 的完整 control，或由 Windows 脚本明确调用指定 WSL；不使用未经验证的 Windows UNC socket 语义。发行版、GID 和项目路径可配置，校验实际值，不安装/改全局环境。
- [ ] 完整模式独立于 A1 login-only 项目，专用卷/目录有归属验证；重复启动/升级不再生成新 key。服务 secret 只读挂载，state 存放位置、权限和路径映射明确；镜像均固定 ID/digest，无 Docker socket 下放。
- [ ] provisioner 仍是受信宿主组件；若需要专用目录权限初始化，精确验证目标只在本项目根内，不 chmod 777/666、不修改开发者 HOME/旧 workspace。Desktop 无 XFS 的配置明确标记为开发，不改变生产门槛。
- [ ] ngrok 可沿用现有配置为 control:7861 提供 HTTPS，不重置 authtoken/已有隧道；公共 Origin 严格匹配。实际测试采用本次独立入口，敏感请求不写原始检查日志。
- [ ] 初始化、启动、status/停止、端口/密钥/前端/provisioner 缺失诊断命令写进手册；A7 的完整备份升级恢复不在本轮伪称完成。

## A4.5 真实装配验收与停止门

- [ ] 正式 create_app/relay_factory + 真实 SQLite/Auth/Session/RuntimeManager/UnixProvisionerClient，control 容器→gateway 容器→worker 容器；独立登录 A/B，不能用 fake runtime 或独立签名客户端绕过公开 control 路由。
- [ ] 经公开 API 创建会话、提交、轮询/公开 SSE、权限状态/回答、stop、消息历史。验证两个 workspace 身份/原生会话/消息不串，Cookie/Origin/CSRF/角色/禁用/撤销仍生效。
- [ ] ACP 协议夹具在真实子进程内读取受管 broker URL/token，发至少一次合法 `chat/chat/completions` 请求，经该 workspace sidecar 到真实 control broker，再到 fake provider transport；捕获实际 route/模型及服务端 sentinel Key 使用，worker 侧无真实 provider Key。禁止以另起客户端调用 broker 代替子进程接线证明。
- [ ] A token 到 B workspace、错误 run/channel、过期 token 拒绝；control/gateway 不可用/新 boot 后事件恢复，不重复启动适配器、不误结束 Run。浏览器用户不得直接访问内部端口。
- [ ] 真实 HTTPS 可用时复用 ngrok验证 A1 登录壳与一条 control SSE/历史交互；若因既有隧道限制无法开公网入口，可用范围受限的测试 TLS/SPKI 固定证书记录 API/TCP 证据，公开 ngrok 场景标明未运行，不借机修改认证或购买账户。
- [ ] 跑 gateway/relay/任务生命周期/事件/身份/会话/broker/runtime/provisioner/迁移与 A1 Desktop 的受影响回归；前端未改不要求无意义改版，若改则 build/lint/已有浏览器回归。记录所有 skip/not-run。
- [ ] 显式路径中文提交；报告父/新完整 SHA、配置格式与正式启动命令、固定镜像、真实调用链、各边界响应、故障恢复、无秘密证据和资源清理。提交后停止等待主审，不进入 A5/A6。

## 完成定义

A4 通过时可以宣称“公开多用户 control 已通过 gateway 执行受管 ACP 协议夹具并经 sidecar/broker 调模型协议”。仍不能宣称真实 DeepSeek Harness、真实供应商或生产上线已验收。A5 负责实际 DSH，A6 负责真实双用户浏览器/供应商闭环。
