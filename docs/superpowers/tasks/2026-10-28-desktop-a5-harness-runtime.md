# Task A5：真实 DeepSeek Harness Linux runtime 与 broker 适配

> **For agentic workers:** 使用 superpowers:executing-plans 或按明确职责使用 subagent-driven-development；先读设计和本任务，再按项红绿实施。提交后等待主审，不进入 A6/A7。

**Goal:** 将 A4 的 ACP 协议夹具替换为固定源码构建的真实 DeepSeek Harness，在受限 worker 中完成 initialize/new/prompt/resume，并证明模型请求实际经过本空间 sidecar/control broker。

**Architecture:** 保留 A4 已验收 control→gateway→worker 与 worker→sidecar→broker 信任边界。Harness 在构建期安装和编译，运行时使用公开受管 profile、私有 HOME 和逐 Run broker URL/token；供应商 Key 只在 control。先查实际 provider 协议，再选择最小受管适配，不能把 broker OpenAI 路由硬套到不同协议上。

**Tech Stack:** 现有 Python ACPAdapter/RunManager/Broker、Docker Desktop Linux containers、固定 Node/pnpm、真实 Harness 源码；PowerShell 主机测试、WSL provisioner。

**Spec:** Desktop 装配计划 Task A5，继承原多用户 spec 和本地开发门槛。A4 验收见 `../audits/2026-10-28-desktop-a4-accepted.md`。

## 输入与约束

- Easel 基线：`04271949a8ce2b14d6d618063b2a0eb7fb355579`。报告 HEAD/工作树，保护主审 README/plan/spec/审计/任务单。
- 已知 Harness：`C:/Users/hyh/Desktop/deepseek-harness`，候选 commit `687f282e8625c6b8d11ff58211a90524be2e4317`。主审本轮再次验证该 SHA、工作树干净。可作为本轮构建输入；这是候选验证授权，不是预先宣称 Linux/runtime 已验收。
- 实际 package.json：Node `^22.19.0 || >=24.0.0`、`pnpm@11.28.5`、build=`tsx scripts/build.ts`；CLI 候选 `apps/cli/lib/bin.js --profile acp`。旧记录见 `2026-10-22-harness-local-baseline.json`，最终命令须实际验证。
- 主机 Python：`C:/Users/hyh/anaconda3/envs/Agent/python.exe`；Linux 构建使用 Linux 环境，不复制 Windows node_modules/原生插件/lib 产物。
- 不复制开发者 HOME、私有 profile、.env、账号或浏览器目录。只读源码公开 manifests/tests；不打印现有凭证。Harness 原仓库不直接改；如需补丁，以可复现的隔离 checkout/显式补丁文件及 SHA 管理，不隐瞒 upstream 差异。
- 不变：UID 10001、readonly rootfs、cap-drop ALL、NNP、无 Docker socket/宿主发布端口、workspace 独立持久根、gateway 签名/boot/attempt/scope 保护、restricted egress。不得用 --no-sandbox、真实 provider Key 入 worker、开放任意代理路径换取成功。

## Review Focus

1. 源码存在不等于可执行：CLI、profile、所有 Linux 原生/动态加载依赖必须实际加载。
2. 协议必须真实一致：Harness 当前 llm-deepseek 的代码是 Messages transport，而现有 broker chat 仅 `chat/completions`/`models`；不能只改 baseURL 就声称适配完成。
3. 两个 Run/原生续接更换 broker token 后不能继续用旧 token，也不能丢失 native session 或复用他人空间材料。
4. 无配置/上游出错/流中断必须明确失败，保留可恢复材料，不写假助手成功消息、不泄露 token/Key。
5. 构建期可联网，启动期不得 npm/pnpm/pip 下载，不把 fake ACP/fake model 响应带进正式 runtime 默认路径。

## Task A5.1：核对真实构建与协议，固定最小设计

**Files:** 读取 Harness package.json/lockfile、scripts/build.ts、公开 ACP bundle/profile 与 packages/llm 实际实现；读取 Easel Dockerfile.worker、smoke_worker.py、providers/openai.py、broker/worker env 与 A4 集成测试。

- [ ] 报告真实 revision/公开 profile 入口/Node 与包管理器/必要 Linux 原生依赖，不输出私有配置。
- [ ] 写下本轮实际请求协议、精确路径、鉴权、流式事件、模型/tool/错误语义的对应关系。先确认 Harness 是否已有可配置 OpenAI-compatible provider；若无，采用最小受管 provider 适配或精确 broker 协议扩展，说明选择与文件职责。不能臆造 schema 或用无损依据不足的字符串替换冒充转换。
- [ ] 必要扩展只允许明确 method/path/channel，继续模型白名单、快照、Run token/容量、限流、响应头净化与出口策略；不泛放行 /v1 或任意路径，不增加获取 provider Key 的能力。超出既有边界的方案先报告具体差异，不静默实施。
- [ ] 新增无秘密 `deploy/multiuser/managed/acp-profile.json`（若沿用 Easel manifest）及其实际引用的 Harness 原生公开配置；明确 JSON 是 Easel manifest 还是 Harness 原生 schema，不混用。所有模型/秘密仅来自受管解析与逐 Run env。

## Task A5.2：先红后绿，构建真实 Linux runtime

**Files:** 修改 `deploy/multiuser/Dockerfile.worker`、`smoke_worker.py`、managed/README；必要时新增职责明确的构建脚本/受管适配文件；测试放 tests/multiuser 与 tests/integration/multiuser。

- [ ] 红测：错误 Node、缺 CLI 构建产物、缺 profile/bundle、缺运行依赖、错误固定 SHA 均失败；--require-dsh 不能仍只检查 .git/HEAD 与文件只读。
- [ ] 固定兼容 Node 与 pnpm，锁文件 frozen 安装完整构建依赖，执行实际 build。使用多阶段保留必需运行产物与 Linux 原生组件，写入可核验的源码/补丁/profile/版本摘要；不得无锁 npm install 或只 prod install 不 build。
- [ ] 使用已固定构建输入，实际构建 runtime（不能只给旧 execution 叠源码就说最终 runtime 通过）。无变化基础层可缓存；网络错误如实报告具体阶段，不关闭安全检查。
- [ ] 以正式非 root/只读/私有可写根运行真实 CLI/profile，实际完成 ACP initialize/session new。此阶段不得凭全局安装或宿主 node_modules 成功。

## Task A5.3：真实 Harness→本空间 broker 的模型与续接验证

**Files:** 新增真实 Harness 集成测试（与 A4 fixture 测试分开）；如需新 provider/broker 适配，添加对应单元、负例和流式测试，不把测试 fake 变成默认实现。

- [ ] 用 A4 正式控制装配与容器拓扑、签名快照、实际 ACPAdapter/RunManager 启动真实 Harness 子进程；测试上游可为受控 fake provider，但 Harness 必须真实。
- [ ] 至少两个 prompt/同 session 原生 resume、两个 workspace 独立 HOME/profile/native binding；捕获实际 sidecar→control broker→provider 路径、请求模型与服务端 sentinel 鉴权。worker 不持 provider Key，不直连 provider。
- [ ] 验证 broker token 轮换/子进程重建后的续接：新请求使用新 Run token，旧 token 到期/撤销被拒，native ID/历史仍正确；不把 token 固化在镜像或持久公开 profile。
- [ ] 覆盖真实流式 token、最小工具调用/权限回答（按实际支持能力），上游 401/429/5xx、中途断流、错误 run/channel/workspace、缺 broker/env/profile。失败不写 done 或伪助手消息，不自动重复有副作用的 prompt POST。
- [ ] 在既有安全配置确有可用凭证与批准受管 channel 时，做一次有界的真实 DeepSeek 文本请求，低输出上限，无工具副作用/自动重试循环；凭证只经 control 配置解析，不写命令行/报告。没有可用凭证时把最后一项明确记为待执行，继续完成其余构建与 fake-provider 证据，不伪称真实调用通过。

## Task A5.4：回归、证据与停止门

- [ ] 跑受影响 image/smoke/provider/broker/egress/ACP/worker Run/env/session、gateway/relay/生命周期/Desktop 契约回归。原 A4 协议夹具正常路径保留，真实 Harness 测试单独计数；前端未改不要求无意义构建。
- [ ] 交付可复制的固定源码与 Docker build/启动/测试命令，Node/pnpm/Linux 平台、upstream SHA/补丁哈希、profile 摘要、镜像 ID/digest、真实请求链和原生续接结果。失败和 skip/not-run 单列。
- [ ] 只使用随机归属测试资源，逐项核对清理，不执行全局 prune、不触碰开发者容器/卷/网络或 ngrok 现有隧道。保留测试镜像可供复审，报告与运行资源分开。
- [ ] 显式路径中文提交；报告 Easel 父/新完整 SHA。若 Harness 有受管补丁，完整给出修改来源与复现方法。勿提交主审 plan/spec/审计/任务单。
- [ ] **提交后停止等待 A5 复审。** 不进入 A6 双用户公网浏览器或 A7 运维。不以文本聊天成功宣称 Chromium 沙箱/XFS/Linux 防火墙等生产门槛通过。
