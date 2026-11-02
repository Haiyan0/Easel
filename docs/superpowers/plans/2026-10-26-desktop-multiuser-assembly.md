# Docker Desktop 多用户实例装配开发计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans 按任务实施。外部代码 Agent 执行，主审逐任务验收；不得自动跳到下一任务。

**Goal:** 在本机 Windows + Docker Desktop + ngrok 上，让两个邀请用户完成登录、独立会话、真实 DeepSeek Harness 聊天、重连续接，以及可恢复的日常运维。

**Architecture:** 复用已开发的 control/Auth/Session/WorkerRelay/RunManager/Broker/Provisioner，而不是另建一套多用户后端。ngrok 只负责公网 HTTPS；control 提供同源构建前端和 API。worker 按 workspace 隔离，应用与 Docker 管理权限分离。先交付登录管理面，再验证 Desktop 拓扑、装配执行 worker、真实 Harness 与端到端聊天。

**Tech Stack:** 现有 Python/FastAPI/SQLite、React 构建产物、Docker Desktop Linux containers、PowerShell、ngrok；需要 Linux Unix socket 的宿主组件优先评估 WSL2，不能假设 Windows Python 支持现有 Unix peer-credential 协议。

**Spec:** 继承 `../specs/2026-10-16-multiuser-workspaces-design.md` 与 `../specs/2026-10-22-local-docker-development-gates.md`。本计划只增加 Desktop 装配交付；不改变原隔离边界，不撤销正式发布阻断项。若拓扑必须改变既有信任边界，先提交明确设计差异，主审通过后实施。

## 当前节点

- 2026-10-29 A7 终审更新：`1a8811096c7c86bb9079a5c88b92cd225d1907b1` 的 Desktop 运维功能及 R2 三项验收通过；主审 153 passed/1 skipped，独立真实 Store/正式脚本反例关闭。见 `../audits/2026-10-29-desktop-a7-accepted.md`。作者真实容器演练保留为作者证据；真实供应商与生产 Linux 门槛仍 not-run/blocked。本计划 A1–A7 的 Desktop 功能范围收尾，不自动进入 A8、不授权上线。

- 2026-10-29 R1 复审更新：A7 `e8376753d63eb681560ae868aa2d1528b2756c85` 暂不通过；主审定向 147 passed/1 skipped，但真实反例确认 prepare 重登记损坏 DB、prepared verify 新增 WAL/SHM、旧 Stop 备份可授权新 Stop 升级。仅执行 `../tasks/2026-10-29-desktop-a7-rework-r2.md`，依据 `../audits/2026-10-29-desktop-a7-review-round-2.md`；R1-02/04 已关闭结果保留，不进入 A8、不自动上线。

- 2026-10-29 更新：A7 `04b5f23e36879e72fbafa345dc2ed06abd26a5ae` 暂不通过，主审定向 84 passed/1 skipped，但确认全量摘要缺漏、错误 workspace 根假完整备份、升级未先验完整备份、已有 DB 丢 key 仍初始化四项问题。仅执行 `../tasks/2026-10-29-desktop-a7-rework-r1.md`，详见 `../audits/2026-10-29-desktop-a7-review-round-1.md`；不自动上线、不改生产 blocked。

- 2026-10-29 更新：A6 功能与 R1 于 `d9c2f5ba960e892f198ebc4a111b7e2ad7e835af` 验收通过，主审 183 passed/7 skipped，独立慢取消/ACP/恢复四项通过。真实供应商仍 not-run，原未验证 UI/生产门槛保留。见 `../audits/2026-10-29-desktop-a6-functional-accepted.md`；下一项仅 `../tasks/2026-10-29-desktop-a7-operations.md`，两份运维手册在该任务中明确允许更新与提交，最终仍等主审终审。

- 2026-10-29 更新：A6 `acef78b3ae3b1b1f318a297bf2c5cb06200aa76b` 暂不通过；主审定向 75 passed，但确认 A6-R1-01：持久 cancelled 不代表取消清理结束，新增陈旧锁自愈可导致同会话重叠执行。见 `../audits/2026-10-29-desktop-a6-review-round-1.md`，仅执行 `../tasks/2026-10-29-desktop-a6-rework-r1.md`；原浏览器证据保留，真实供应商仍 not-run，不进入 A7。

- 2026-10-28 更新：A5 `1e70afab0a55bee4cf41f985b691b6ebbf82c604` 的 Linux 构建/真实 Harness/受控 provider 链路通过，主审真实容器集成 2 passed、单元 37 passed、兼容 223 passed/6 skipped。真实 DeepSeek 请求仍待批准受管凭证，未算通过。见 `../audits/2026-10-28-desktop-a5-accepted.md`；下一项仅 `../tasks/2026-10-28-desktop-a6-browser-closure.md`，A6 分别记录公网/真实供应商未执行项，不进入 A7。

- 2026-10-28 更新：A4 于 `04271949a8ce2b14d6d618063b2a0eb7fb355579` 验收通过；主审 R3 含文档 93 passed，独立进程门控 13 passed。范围为公开 relay/gateway/逐空间 broker 与协议夹具装配，见 `../audits/2026-10-28-desktop-a4-accepted.md`。下一项仅 A5 真实 Harness Linux runtime，执行单 `../tasks/2026-10-28-desktop-a5-harness-runtime.md`；A6/A7 及原生产门槛仍待验收。

- 2026-10-28 更新：A4 R2 `d5e80a9b87b19a14ddf6f6939396e84b646f321b` 的公钥/Engine 核验与顺序重开已修复，主审两组 79 passed、160 passed。仍需 A4-R3-01 保护并发 Stop/Start 的物理副作用，见 `../audits/2026-10-28-desktop-a4-review-round-3.md` 与 `../tasks/2026-10-28-desktop-a4-rework-r3.md`；不进入 A5/A6。

- 2026-10-28 更新：A4 R1 `f5451df4a1f752605ed17fcb2f7434258ddb3011` 已关闭原六项中的主要运行时问题，主审扩展回归 458 passed/2 skipped；可信公钥/Engine 接线与正常停止重开仍需三项定向修复。见 `../audits/2026-10-28-desktop-a4-review-round-2.md`，仅执行 `../tasks/2026-10-28-desktop-a4-rework-r2.md`，不进入 A5/A6。

- 2026-10-28 更新：A4 首轮 `81e9cc18f6ff698fdc16abff7a0418260e0217ee` 暂不通过；主审定向 410 passed/2 skipped，但确认六项挂载、恢复、启停与持久化问题。详见 `../audits/2026-10-28-desktop-a4-review-round-1.md`，仅执行 `../tasks/2026-10-28-desktop-a4-rework-r1.md`，不进入 A5/A6。

- 2026-10-28 更新：G2 于 `785f556bc81c464f59de5105a02c12305b2e6265` 验收通过，主审定向含文档 292 passed/1 skipped。裁决见 `../audits/2026-10-28-desktop-g2-accepted.md`；下一步仅 A4 公开 relay/gateway 签名及逐 workspace broker，执行单 `../tasks/2026-10-28-desktop-a4-control-relay.md`。真实 DSH/供应商留 A5/A6。

- 2026-10-28 更新：G1 于 `d1fc8b16d6cb98092fa1ff9d0772c15bd40e35bd` 验收通过，G1 含文档 130 passed，兼容 182 passed。裁决见 `../audits/2026-10-28-desktop-g1-accepted.md`；下一步仅 G2 容器/可信握手/租约代次装配，执行单 `../tasks/2026-10-28-desktop-g2-gateway-assembly.md`。A4 公开 relay/broker 之后单独实施。

- 2026-10-27 更新：A3 于 `8a5c16be2b80328f234ad4693b00b6795a23a12f` 验收通过，主审指定含文档 201 passed/1 skipped，真实 CLI 超时清理 1 passed。详细裁决见 `../audits/2026-10-27-desktop-a3-accepted.md`。下一步仅 G1 gateway 核心，执行单见 `../tasks/2026-10-27-desktop-g1-gateway-core.md`；G2 再接生命周期/control，之后继续 A4。

- 2026-10-27 更新：A2 于 `81589c0f24f3507b954e46b37ce85d181c619520` 验收通过，真实拓扑 11 passed。拓扑批准及附加约束见 `../audits/2026-10-27-desktop-a2-accepted.md`。下一步仅 A3，执行单见 `../tasks/2026-10-27-desktop-a3-worker-assembly.md`；gateway 运行时在 A3 验收后单独下发，再接 A4。

- 2026-10-27 更新：A1 于 `f1c4e5ef98ea756513071367919fe57fac7dbf17` 验收通过，主审定向回归含文档 122 passed；下一项仅 A2。详细裁决见 `../audits/2026-10-27-desktop-a1-accepted.md`。以下“待开发”描述以此逐项状态为准。

- 已完成：Tasks 1–14 的本地代码验收；认证/权限/会话/代理/SSE 等核心模块。
- 已完成：2026-10-26 重跑认证/管理员 HTTP 58 passed、真实 Docker broker 拓扑 1 passed。
- 待开发：以下装配任务；本计划不是完成报告。
- 未通过：目标 Linux 防火墙/IPv6、XFS 配额、最终 runtime/真实 Harness、硬化 Chromium 等原门槛。

## Global Constraints

- 当前审查基线 `290d94a68b9a4c1e8e3f627b5c96fcbdb8f11f1b`；实施前报告实际 HEAD 与差异。保护主审未提交的 plan/spec/手册。
- Windows 验证 Python 固定 `C:\Users\hyh\anaconda3\envs\Agent\python.exe`；WSL 与容器使用各自专用 Python 环境，不复用 Windows venv/node_modules。
- 默认只暴露 control 到 `127.0.0.1:7861`；ngrok 指向该端口，不暴露 worker、Docker socket、provisioner 或内部 broker。
- 严格 HTTPS Origin、Secure Cookie、CSRF、内部服务凭证和数据库身份复核保持启用。ngrok authtoken 与供应商秘密不进入仓库/报告/命令行参数。
- master key 与签名私钥分离；私钥只给 control；worker 只读公钥。供应商真实 Key 不注入 worker HOME/env/profile。
- worker 不增加特权、host network、Docker socket、控制数据库挂载；不关闭 Chromium sandbox。宿主 provisioner 的高权限不能转移给 control/worker/sidecar。
- 不操作既有开发者容器或持久目录；测试资源使用随机 run-id 标签、归属核对、逐项清理；不使用全局 prune。
- Desktop 如不支持 XFS 项目配额，必须显式标注开发限制，保留生产强制配额；不静默降级，不声称磁盘隔离已通过。
- 首期交付范围是登录、成员/设置、会话目录、聊天、停止、权限问答、SSE 与历史；产物/发布等仍未装配的业务继续明确 503。
- 每任务先写失败测试、记录红证据、实现最小修复、运行绿测试并显式路径提交，中文提交信息；提交后停下交付主审。

## Review Focus

1. ngrok 地址变化/旧 Cookie/写请求 Origin 不一致必须明确拒绝，更新 Origin 后新登录可恢复（A1）。
2. Desktop/WSL 路径、Unix socket、控制面到 worker 的网络可达性不能用测试进程直连替代真实拓扑（A2）。
3. 公钥/命令/网络接线变更遇到活跃 worker，不能删容器迁移；必须 drain/stop 后重建（A3/A4）。
4. 真实 Harness 启动成功不代表走 broker；需要证明用户模型请求经指定 broker，worker 无供应商真实 Key（A5/A6）。
5. 重启/超时/重复提交/升级失败不得串历史、重复执行或丢密钥；恢复必须在新目录演练（A6/A7）。

## 文件职责与接口原则

- 新建 `deploy/desktop/`：只放 Desktop 编排、初始化、诊断和运维入口，不复制 control/worker 业务实现。
- 新建 `deploy/desktop/compose.login.yaml`、`deploy/desktop/Dockerfile.control`：可独立运行的登录管理面。
- 新建 `deploy/desktop/Initialize-Easel.ps1`、`deploy/desktop/Start-Easel.ps1`、`deploy/desktop/Test-Easel.ps1`：创建专用资源、校验并启动、只读诊断；不得解析并回显秘密。
- 完整拓扑文件与 WSL 启动脚本由 A2 决策后确定，先验证再固定文件结构。
- 复用 `Provisioner.ensure(workspace_id, config_version) -> WorkerHandle`、`stop(workspace_id, instance_id)` 和 `inspect(workspace_id)`；不得绕开 RuntimeManager 的实例代次、CAS 和停止恢复机制。
- 复用 `web.app` 的常驻 worker 模式及 `/internal/runs/{id}` 系列契约；不要另造第二套 Run 状态机或浏览器直连 worker API。

## Task A1：可长期使用的登录管理面

**Files:** 新建上述 Dockerfile.control、compose.login.yaml、Initialize-Easel.ps1、Start-Easel.ps1、Test-Easel.ps1；新增 `tests/multiuser/test_desktop_deployment.py`；更新 Windows 手册第 9 节。

**Interfaces:** 输入固定 control 镜像、部署项目名、ngrok HTTPS Origin、专用数据目录/卷；输出同源前端 + control:7861。默认不启用执行 worker，不承诺聊天可用。

- [ ] 写失败测试：非法 Origin/缺前端/缺 key 在启动前失败；key 已存在不覆盖；重复初始化保持同一数据库和 key；控制服务无 Docker socket/worker 根挂载，端口仅回环。
- [ ] 跑新增测试确认失败，保存命令与断言。
- [ ] 把手册示例落为实际受版本管理的配置/脚本；控制镜像构建包含当前源码和当前前端，不能默认使用旧 task8 测试镜像。初始化 root helper 只管理本项目专用卷，正式服务 UID/GID 10001。
- [ ] ngrok 沿用用户已有安全配置，前台单独启动，不关闭其他项目隧道；脚本不读取 authtoken。不自动导入/改动本机已有账号。
- [ ] 真实浏览器 HTTPS 验证管理员 bootstrap、邀请单次兑换、A/B 独立浏览器 context、403 权限、会话目录互不可见、注销/禁用、Origin 变更与重新登录。记录准确的浏览器/容器边界；真实证书校验，不全局忽略证书错误。
- [ ] 回归 `test_auth_api.py`、`test_admin_api.py`、`test_config_api.py`、新增 Desktop 测试；Compose config、前端 build/lint。交付命令、清理方式、截图（无令牌）和 SHA 后停下。

**验收结果:** 用户能照手册独立启动登录管理面并创建两个用户；聊天 503 被清楚说明。A1 可以在 A2 尚未完成时独立通过。

## Task A2：Desktop 完整拓扑可行性与固定接入契约

**Files:** 新建 `deploy/desktop/README.md`、`deploy/desktop/topology.json`（仅无秘密配置契约）、`tests/integration/multiuser/test_desktop_topology.py`；必要的诊断脚本位于 deploy/desktop。

**Interfaces:** 继续现有 Provisioner 协议与 WorkerHandle；输出明确的管理进程运行位置、Unix socket 路径/UID/GID、宿主与 Docker bind 路径映射、控制面 worker 访问路径、每 workspace broker URL 解析规则。

- [ ] 只读盘点本机 WSL 发行版/版本、Docker context、Docker Desktop integration、现有端口与资源；不要自动安装发行版或改全局设置。
- [ ] 写真实拓扑反例：control 容器请求现有句柄地址、宿主探测容器 IP、跨 WSL bind 文件及 Unix socket peer UID；不假设这些天然可达。
- [ ] 优先验证 WSL2 宿主 provisioner + 现有 Unix socket 授权方案。Windows 原生 Python 不冒充 Linux SO_PEERCRED。
- [ ] 若 control→worker 或 WSL→worker 不可达，先提交一份确切的修订设计：方向、监听地址、逐 workspace 路由、服务认证、路径白名单、SSE、网络反向访问与失败清理。不得直接接共享管理网、发布所有 worker 端口、放宽 CONNECT 私网限制或给 control 挂 Docker socket。
- [ ] 若方案需要增加受信管理容器/中继，明确它相对原“宿主 provisioner”设计的权限差异，主审批准后再写运行时代码；不以“测试专用”绕开边界。
- [ ] 运行临时资源实测，输出完整调用链与约束，清理后检查无本次残留。所有架构选择固定到 topology.json/README，供 A3/A4 使用。

**验收结果:** 真实 Docker Desktop 拓扑内的管理授权与网络双向调用可证实；若确实不可行，精确说明缺什么本机组件，不要求用户租服务器。A2 未通过，不并行猜测 A3/A4 接线。

## Task A3：实际执行 worker 与生命周期统一

**Files:** 修改 `deploy/multiuser/provisioner.py`、`easel/worker/lifecycle.py`、`web/app.py`；必要时新建 `easel/worker/server.py` 抽取启动组合；更新 `tests/multiuser/test_provisioner.py`、`test_worker_runtime.py`、`test_runtime.py`，新增真实容器执行入口测试。

**Interfaces:** 单一 worker 进程同时提供已认证 lifecycle health 与现有 `/internal/runs/{id}` 提交/停止/事件/问题接口；保留常驻 RunManager。provisioner 健康探测必须验证此执行能力及身份，不能只看一个任意 200。

- [ ] 红测试：当前 lifecycle-only 镜像 health 成功但真实 Run 路由缺失时，不得 ready；缺公钥/错误公钥/未知模式启动失败。
- [ ] 实现固定受管启动命令，不从用户 workspace 的 acp.json/环境推断。公钥装配使用 A4 的固定契约；测试可先用隔离测试公钥，不复制真实私钥。
- [ ] lifecycle drain 计数基于真实 RunManager 活跃 Run，不仅按 HTTP 请求数量；SSE 断开不得被误判为任务结束。
- [ ] 同步实际 inspect 验收的固定命令/env/挂载，防止重启对账继续接管旧 lifecycle-only 实例。活跃旧实例拒绝迁移，沿用 drain/stop。
- [ ] 真实容器 + 签名测试快照 + 真实 ACP 夹具子进程完成提交/事件/停止/重启历史；夹具结果标记为协议验收，不称真实 DSH。
- [ ] 跑生命周期/停止恢复/worker 执行回归，交付 SHA 后停下。

## Task A4：签名、受管配置与 broker 接线

**Files:** 修改 `easel/control/cli.py`、`easel/control/proxy.py`、`deploy/multiuser/provisioner.py`；按需修改 snapshot/config 模块；新增 Desktop 完整 Compose 和签名/接线回归。

**Interfaces:** control 加载独立 Ed25519 私钥与受管 Agent 配置；worker 只读对应公钥。逐 Run 的 broker 地址来自 A2 固定的可信 workspace 拓扑，禁止接受客户端提供地址；不得对管理员文本做任意字符串模板执行。

- [ ] 红测试：参考 Compose 缺签名装配、worker 缺公钥、固定 broker URL 不能到对应 sidecar、A token 被导向 B sidecar，全部明确失败且不伪成功。
- [ ] 接通已有 `--snapshot-key-file`、`--managed-agent-config`、前端目录参数；初始化脚本独占创建两类 key，重复运行不轮换。
- [ ] 实现并文档化可信 workspace→broker 地址选择；复用现有 BrokerDestination/快照签名约束，保留 token 的 workspace/run/channel 限定。
- [ ] 公钥变化视为受管配置变更，不对活跃 worker 偷换挂载或重建；新代次启动和旧实例 drain 按既有规则处理。
- [ ] 实测 A/B 容器 Key/数据库不可见、错误签名与跨空间/Run token 被拒、client headers 被净化；无代理/策略继续 503。
- [ ] 跑签名/代理/broker/sidecar/并发回归，交付 SHA 后停下。

## Task A5：真实 DeepSeek Harness Linux 镜像

**Files:** 修改 `deploy/multiuser/Dockerfile.worker`、`smoke_worker.py`，新增无秘密受管 profile 样例与 Desktop 构建命令；实际审查 Harness 的 provider/profile 适配后决定是否在 Harness 自身仓库改动，不能在 Easel 中臆造其接口。

**Input:** 已知本地候选 `C:/Users/hyh/Desktop/deepseek-harness`，commit `687f282e8625c6b8d11ff58211a90524be2e4317`，记录见 `../tasks/2026-10-22-harness-local-baseline.json`。这是候选而非已批准最终镜像；先复核工作树与 commit，不再要求用户手工寻找。

- [ ] 红测试：runtime 有源码却没有 CLI 构建产物、Node 版本不满足 engines、profile 引用不存在、运行时在线安装，均构建/冒烟失败。
- [ ] 固定 Node 与仓库 packageManager/lockfile；构建阶段装完整构建依赖并执行真实 build，运行阶段只带必要产物和依赖。不得保留当前“只 prod install、不 build 即视为可用”的假设。
- [ ] 检查真实 ACP profile、provider 配置、HOME/DSH_HOME 读取和 broker 协议兼容性；只迁移公开 profile 结构，不复制用户 HOME/私有 credentials。
- [ ] 若 Harness 不能直接使用既有 broker 协议，明确并开发最小受管适配，记录请求与响应/流式/错误映射；不能把真实管理员 Key 注入 worker 作为捷径。
- [ ] 先以真实 Harness + 本地受控 provider 验证 ACP initialize/new/prompt/resume；再通过现有安全配置通道做一次真实 DeepSeek 请求。没有可用凭证时只标记最后一项待执行，不捏造结果。
- [ ] 记录源码 SHA、Node/packageManager、profile 摘要、镜像 ID/digest、实际请求链路。交付后停下；不以镜像构建成功替代真实调用证据。

## Task A6：双用户真实浏览器闭环

**Files:** 新建 `tests/integration/multiuser/test_desktop_chat_live.py`、对应浏览器脚本；修改范围仅限此轮发现的实际装配缺陷，不扩展未装配业务。

- [ ] 用真实 control/worker/sidecar 容器、A5 Harness、ngrok HTTPS、两个独立浏览器 context，先写失败场景并明确依赖。
- [ ] A/B 分别邀请登录、创建会话、发送消息；验证各自 workspace/native binding、持久 HOME/profile/历史隔离及真实 broker 归属。
- [ ] 覆盖 SSE 断网重连、重复 submit 幂等、停止、权限问题回答、撤销登录后流关闭、worker/control 重启后续接；不能用 fake runtime 或协议夹具代替此门槛。
- [ ] 故障注入：worker 启动失败/公钥错误/broker 不可达/供应商失败，容量释放与历史不伪成功；失败材料可恢复、秘密不外泄。
- [ ] 实际验证 worker 不能直连公网/管理面/其它 workspace；浏览器技能沙箱不可用时明确禁用对应功能，不用 --no-sandbox，文本聊天与浏览器能力分别报告。
- [ ] 输出每项 pass/fail/not-run、测试资源清理结果；全套 frontend/backend 和指定兼容回归按实际改动运行。交付后停下。

## Task A7：日常启动、备份、升级与验收分层

**Files:** 完善 deploy/desktop 脚本/README 与两份中文手册，新增运维脚本契约及恢复集成测试。

- [ ] 红测试：密钥丢失/镜像不匹配/端口占用/Origin 改变/WSL 未启动/旧 socket，不得创建新 key 或落到 local；输出可操作且无秘密的诊断。
- [ ] 提供首次初始化、日常 start/stop/status/logs、版本与摘要记录；ngrok 生命周期独立管理，不停止用户其它隧道。
- [ ] 实测阻止新任务→在途收尾→drain→备份 DB/key/workspaces→新目录恢复→新项目端口验收；禁止共享卷在线裸拷贝冒充一致备份。
- [ ] 实测更新固定镜像与 profile、公钥版本的升级及失败回滚；schema 回退恢复匹配备份，不降 user_version。
- [ ] 最终标识两层：`desktop-functional`（本计划实际已验证项）、`production-release`（原 Linux 发布检查）。不能修改原检查器让 Desktop 获得生产通过。
- [ ] 验收清单逐项回填真实证据，说明仍 503 的业务与未通过的浏览器/磁盘/防火墙门槛。主审终审后再向用户宣称本机多用户聊天可用。

## 外部代码 Agent 首轮任务（可直接复制）

请在 C:\Code\Easel 实施本计划的 **Task A1，且仅 A1**。先读取本计划及引用 spec、AGENTS.md，报告实际 HEAD、工作树和拟修改路径；保护主审已有文档。使用指定 Agent Python，先写测试复现缺少可重复登录部署入口，再实现 control 镜像、专用 Compose、PowerShell 初始化/启动/诊断与手册。使用本机 Docker Desktop，不操作现有开发者容器/账号；ngrok 使用已有配置，只为本次服务创建独立隧道，不回显 authtoken。验收必须包含真实 HTTPS 浏览器双身份邀请登录/权限/会话目录隔离与注销；不以 fake 页面或仅 200 health 代替。没有签名/执行 worker 时聊天明确 503。交付父/新 SHA、精确文件清单、红绿命令与实际结果、启动/停止/重开步骤、剩余限制，提交后停止，等待主审，不进入 A2。

## 主审自检

- A1 能独立交付用户可操作的登录管理面。
- A2 在任何管理权限/网络架构变化前设实测与设计审查门，不以未知 Desktop IP 可达性做基础。
- A3/A4 复用已有协议与状态机；真实执行、生命周期、公钥、可信 broker 路由各有验收。
- A5 区分真实 Harness、真实模型与协议夹具，复用已知候选，无用户秘密搬运。
- A6/A7 覆盖五类 Review Focus；本计划不宣称既有未验收业务或 Linux 门槛完成。
