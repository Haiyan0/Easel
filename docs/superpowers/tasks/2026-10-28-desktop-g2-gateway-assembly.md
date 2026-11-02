# Task G2：gateway 容器、可信启动握手与租约代次装配

基线：`d1fc8b16d6cb98092fa1ff9d0772c15bd40e35bd`。先报告实际 HEAD、工作树与拟改路径，保护主审文档及用户资源。

必读：AGENTS.md、Desktop 装配计划、A2/A3/G1 验收单、deploy/desktop/topology.json 与真实 G1 接口。A2 验收单的两项附加约束优先于旧快照中不可执行/不安全的描述。

## 本轮交付

由受信 WSL provisioner 管理每 workspace gateway，将 G1 核心运行在真实 Docker 双网络容器中。固定管理监听地址、公钥和 worker 实例目标；可信启动握手取得实际 ID 与每次启动的新 boot generation，control 的租约/句柄通过 CAS 同步后才激活转发。恢复和停止动作必须绑定 worker/gateway 的不可复用实例与代次。

允许为此扩展 WorkerHandle、WorkerLease、Store 追加迁移、UnixProvisionerClient/受限协议与 RuntimeManager。**不装配公开 WorkerRelay/聊天/前端，不实施 A4 的 broker URL/受管 Agent 配置接线，不做真实 Harness 或供应商。** 集成测试可直接使用 G1 的正式签名工具验证 gateway，不伪称公开 control API 已打通。

## 文件职责与最小范围

- 新建 `easel/gateway/server.py`：进程启动、可信本地配置/状态/激活接口，复用 G1 app，不复制验签/转发代码。
- provisioner 内 gateway 创建/inspect/停止逻辑优先拆至 `deploy/multiuser/gateway_runtime.py`，从 provisioner 传入受限 Docker runner 与管理员设置；避免继续把所有逻辑堆进单文件。
- 修改 deploy/multiuser/provisioner.py、easel/control/runtime.py、types.py、store.py：受限协议、两阶段握手/CAS、gateway 元数据及恢复。只有确有必要才新增模块，保持权限主体清晰。
- gateway 专用最小 Dockerfile 可放 `deploy/desktop/Dockerfile.gateway`；固定非 root 10001、只读 rootfs、cap-drop ALL、no-new-privileges、无宿主发布端口、无 Docker socket、无用户 workspace/控制数据库挂载。公钥只读；如需启动配置/boot 状态，使用专属小型 tmpfs，不写用户卷；精确挂载/资源契约写入 inspect 验收。
- 新增 gateway server/runtime/租约迁移测试与 `tests/integration/multiuser/test_gateway_runtime_real.py`；更新 Desktop 契约、文档与证据，不提交主审审计/计划。

## Task G2.1：先固定启动与协议契约，再做红测试

- [ ] 同步 JSON/README：管理 bind、网络加入时点、可信配置通道、状态/activate 请求结构、boot_generation 来源。选择 A2 已批准的“create 前可信固定 IP”或“仅控制网络启动 + 受信握手 + inspect 对照后加入 workspace”之一。禁止 create 后试图修改 Env；禁止 0.0.0.0/首网卡猜测；网关不能在 worker 网卡监听管理端口。
- [ ] 可信配置由 provisioner 经固定命令/本地通道提供，包含实际 worker ID/IP/版本/凭证、公钥和 gateway 实例；普通 ensure/HTTP 请求不允许指定镜像、argv、挂载、IP、密钥路径或任意 Docker 参数。若用 exec+stdin，命令固定、参数结构白名单/大小有界，私密值不出现在 argv/日志。不能为方便把 Docker socket 挂进 gateway/control。
- [ ] 明确受限协议版本与旧版本策略：gateway 启用时缺少 gateway 元数据必须失败，不能静默回退直接 worker URL；未启用 gateway 的既有测试/部署路径显式保留，不把旧请求解码成部分新句柄。
- [ ] worker 的 container_id 不变；句柄/租约新增完整 gateway 容器 ID、boot_generation、gateway upstream 与所需配置版本（名字与 G1 GatewayAudience 一致，不另造两套 generation）。gateway boot_generation 与 RuntimeManager 的 owner/generation 是不同概念，不能互相覆盖。秘密不进 repr/公共响应。

## Task G2.2：可信启动、激活和 CAS

- [ ] 先写红测试：未配置/未激活、错误管理 IP/公钥/worker 目标/实例 ID、Docker inspect 空/异常、gateway 自检失败都不 ready；错误 UID 的 socket 请求仍拒绝。
- [ ] provisioner 创建 worker 后固定该不可复用实例，再创建/配置 gateway；获取实际 ID 与本次进程生成的新 boot_generation，验证控制网 IPv4/监听地址、唯一 worker 网络、公钥、资源、命令、固定目标和 worker service token 一致性。不发 prompt 做 health。
- [ ] “已配置但未激活”与“整体 ready”明确区分：受限 ensure 可返回供控制面绑定的候选句柄；RuntimeManager 在同一受保护 owner/generation CAS 内保存候选 gateway 身份，再通过只接受精确实例/boot 的受限 activate 操作激活；验证后才提交/返回整体 ready。DB 事务内不得 await Docker/网络。
- [ ] 具体中间态可复用原 starting 状态与附加字段，不要求任意扩展状态机；必须证明并发控制进程不会读取候选就派发。CAS 失败的迟到 activate/stop 不可影响新代次，不清理活跃 worker。
- [ ] Store 只追加迁移（当前基线 schema 10，实施时先核对）；旧 worker/Run/登录/token 数据保留。使用独立冻结旧 DDL fixture 验证升级，不能用导入当前迁移自动生成的 fixture 冒充旧版本。旧无 gateway 字段记录不能被当作已完成 gateway 装配。

## Task G2.3：恢复、隔离与停止

- [ ] control/provisioner 重启按实际 inspect 和固定 ID 对账，不重复创建健康 worker/gateway；socket 重建后的挂载恢复写入手册。管理组件故障与 worker 确认退出必须是不同结论。
- [ ] gateway 同容器重启也生成新 boot_generation、清空激活状态；经受信握手及 CAS 同步前所有管理转发拒绝。旧 30 秒断言即使未过期也拒绝；不能直接沿用 Docker 标签旧 generation。
- [ ] gateway 关闭失败隔离状态真实上报为 unavailable；不能只读取 G1 的 active 标记。短暂故障/隔离不删 active worker、不将 Run 改 done/failed/interrupted；GET/事件可在新句柄恢复，POST 不自动重发。公开 relay 的适配留 A4，当前须固定可表达的临时不可用状态与接口。
- [ ] gateway 重建只更换 gateway，worker/历史/配置不变；旧 gateway 停止按其 ID，旧 stop 不删除新 gateway。控制面业务 drain/stop 则必须绑定当前 lease 的 worker/gateway 元组，完整终态确认后才能写 stopped。不能仅删除 worker 后按 workspace 名误删新 gateway。
- [ ] 新建/连接/配置/激活任一步失败，按本轮资源 ID、run-attempt 归属和唯一清理任务收尾；超时/取消后等 CLI 终结再处理 cidfile，不重现 A3 失败路径。已有活跃 worker/gateway 不因一次 probe 失败被删除。清理失败保留可恢复材料，无秘密泄露。

## Task G2.4：真实拓扑与交付

- [ ] 两个 workspace、真实 gateway/worker 测试容器；control 网络中独立签名客户端只通过 gateway 请求。合法 health、Run/事件、问题状态/回答、stop、历史协议保持 G1 路由；使用 A3 协议夹具，不称真实 DSH。
- [ ] worker→gateway 的 worker 网卡管理端口不可达；worker 直连 control/其它 workspace 不可达；控制网络上的 A audience 访问 B 返回拒绝。各 gateway 共享控制网络本身可达如实记录，不伪称网络完全隔绝。
- [ ] 真实同容器 gateway 重启、新 generation 可信对账、旧断言拒绝与新签名恢复；期间 worker ID 不变、已开始的 Run 不因中继故障重发或伪结算。
- [ ] 真实 Store/两个 RuntimeManager 确定性交错验证两阶段 CAS、旧 stop/迟到回复不改新代次、清理/停止失败不写假终态。单元覆盖故障组合，真实容器至少完成合法创建/重启/停止与上述反向访问反例。
- [ ] 跑 G1、worker/provisioner/runtime/store/stop-recovery/Unix 协议/A1 Desktop 的受影响回归和文档路径；Compose/JSON/ruff/diff check。真实资源随机名与 run 标签、逐项归属核对和清理，无全局 prune。
- [ ] 提交完整父/新 SHA、接口/迁移/中间态说明、红绿、固定镜像 ID、实际网络/监听/权限/挂载、两阶段时序、重启 boot 证明、清理结果和 pass/fail/not-run。完成后停止，等待主审，不进入 A4。

## 明确不是本轮目标

不修改用户真实账号或 HOME/profile，不接 ngrok/真实模型请求，不修改全局防火墙，不开启无沙箱 Chromium，不把 gateway 双网络接线验收当成 Linux 生产验收。G2 通过后，主审再给 A4 的公开 control relay 签名和逐 workspace broker 接线任务。
