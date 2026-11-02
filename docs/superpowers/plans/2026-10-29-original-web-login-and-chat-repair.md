# 原 Easel Web 登录与聊天修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans 按任务实施。步骤按红测→最小实现→验收推进。主会话负责派发和审计；仅实施“当前执行节点”明确下发的一组，交付后等待复审，不自动创建新聊天或推进下一组。

**Goal:** 用户登录后使用原 Easel Web 的布局与功能，登录页与原主题一致，真实 Harness 聊天能够正常使用。

**Architecture:** 复用一棵原 Web 组件树，在入口增加认证边界，通过 local/control workspace client 适配身份、历史与流协议；control/worker/gateway 的隔离和受管凭证机制保留。先定位并修好聊天链路，再逐组接通原页面后端，最后移除独立 ControlApp 布局。

**Tech Stack:** 现有 React/TypeScript/CSS 变量、FastAPI/SQLite、Docker、Harness ACP、pytest/Node test runner/Playwright；不增加新的前端框架。

**Spec:** [修复设计](../specs/2026-10-29-original-web-login-and-chat-repair.md)。基线 `1a8811096c7c86bb9079a5c88b92cd225d1907b1`；实施前重新核对 HEAD/未提交改动。

## 当前执行节点

- 2026-11-02 Task6A 首轮未整体通过：生产 `51aacfb`、证据 `b582d26`。主审 API/runtime 71 passed、1 skipped；独立复审发现串行锁排队未纳入就绪超时，另须补页面复跑、正式生命周期、受控账号/发布整合与受跟踪原始证据。仅下发 [6A R1](../tasks/2026-11-02-original-web-task-06a-rework-r1.md)，见 [审计](../audits/2026-11-02-original-web-task-06a-review-round-1.md)。手册候选暂不应用，用户实例不切换；真实模型/平台与维护窗口由用户分别选择。

- 2026-11-02 Task5C3 R2验收通过：实现 `6b54e49`、tip `4382180862e28d43f2974e32a618093c847ec262` 关闭显式改素材被旧意图覆盖问题；主审Node190 passed、13组浏览器/build/lint、原改图与确认/刷新/丢ACK独立探针通过，见 [5C3验收](../audits/2026-11-02-original-web-task-05c3-accepted.md)。当前仅下发 [6A隔离整体验收与既有手册收口](../tasks/2026-11-02-original-web-task-06a-integration-operations.md)，用户实例/真实调用仍不授权自动执行。

- 2026-11-02 Task5C3 R1主体修复确认：tip `40fca7c4551db82aa782ef2db466080421337ed2` 的策略迁移/Stop/恢复、请求幂等与摘要已验证；主审665 passed/2 skipped、Node173 passed、5C3十一组及build/lint、隔离容器通过。仅完整payload恢复fallback把用户显式改选素材静默覆盖为旧图一项P2，按 [5C3 R2](../tasks/2026-11-02-original-web-task-05c3-rework-r2.md)定点修，见 [R1复审](../audits/2026-11-02-original-web-task-05c3-review-round-2.md)。不重开已关闭项，不进入Task6、不切换用户实例。

- 2026-11-01 Task5C3首轮复审暂不通过：tip `aef1a578185d15a90d8c46ab46ffb3adde1edfb3`，主审618 passed/2 skipped、Node166 passed、新浏览器五组与build/lint通过；正式manual策略传递/迁移、丢接纳响应同稿重试重复发布、同requestId异输入绕过摘要校验三项P2按 [5C3 R1](../tasks/2026-11-01-original-web-task-05c3-rework-r1.md)收口，见 [审计](../audits/2026-11-01-original-web-task-05c3-review-round-1.md)。用户实例不动，不真实发布、不进入Task6。

- 2026-11-01 Task5C2 R1验收通过：修复 `b1e49cb`、tip `fcb351d532301d22afe3be0c91373b19f3505ca7` 关闭四项P2并补容器/日志。主审518 passed/1 skipped、Node157 passed、63组浏览器/build/lint、原反例正向复跑及隔离容器12项通过，清理无错误，见 [5C2验收](../audits/2026-11-01-original-web-task-05c2-accepted.md)。当前仅下发 [5C3发布/导出](../tasks/2026-11-01-original-web-task-05c3-publishing-export.md)，受控实现测试后等待复审；用户实例不切换，不真实发布、不进入Task6。

- 2026-11-01 Task5C2首轮复审暂不通过：实施 `0801343`、tip `a7c8ce253905918987f2a35fc269a1936a93dd6f`。主审361 passed/1 skipped、Node154 passed、61组浏览器及build/lint通过；独立反例确认旧登录状态提前结算、跨站GET启动分析、旧stop覆盖新弹窗、凭证取消后迟到保存四项P2，另需补原容器闭环和受忽略日志提交。仅按 [5C2 R1](../tasks/2026-11-01-original-web-task-05c2-rework-r1.md)收口，见 [审计](../audits/2026-11-01-original-web-task-05c2-review-round-1.md)。用户实例不动，5C3/6不进入。

- 2026-11-01 用户授权主会话继续派发和审计。当前仅下发 [5C2账号/登录/分析](../tasks/2026-11-01-original-web-task-05c2-accounts-analytics.md)，父基线 `654305a69c1d68d6b157b608dbd2c7d5f96d6298`，沿用原隔离worktree。先接口/资源盘点、红测、最小受管适配，夹具验证后提交等待独立复审；真实平台账号动作未授权，用户运行实例不动，5C3/6不进入。

- 2026-10-31 Task5C1 R2验收通过：修复 `977e9b6`、tip `654305a69c1d68d6b157b608dbd2c7d5f96d6298` 关闭技能状态503后轮询停止问题；独立探针查询1→3、题卡恢复可提交、executions=1。主审Node146 passed、浏览器5C1七组/5B五组/5A四组/Task3 33组/Task4八组及build/lint通过，见 [5C1验收](../audits/2026-10-31-original-web-task-05c1-accepted.md)。5C1已完成；下一阶段5C2账号/登录/分析待实施验收，5C3/6仍待处理，不自动推进或切换用户实例。

- 2026-10-30 Task5C1 R1复审：实施 `d6743f1`、tip `4d46c668021e0de884aee0699d6bbe88030cb167` 已关闭模型/画像指令/准备互斥，权限正常入口恢复；仅技能状态一次503后轮询停止、题卡不能恢复一项P2，按 [5C1 R2](../tasks/2026-10-30-original-web-task-05c1-rework-r2.md)定点修复。主审定向46 passed、相关193 passed、Node146 passed、现有浏览器矩阵/build/lint通过，见 [R1复审](../audits/2026-10-30-original-web-task-05c1-review-round-2.md)。不进入5C2/5C3/6，用户实例不切换。

- 2026-10-30 Task5C1首轮复审暂不通过：实施 `1195a17`、tip `c9df47738c344d382d45e91aec91f6019f0fbaa0`。主审定向96 passed、相关135 passed、Node146 passed、原浏览器矩阵和build/lint通过；新类型模型快照分叉、权限题卡缺失、画像增强指令丢失、画像准备期删除竞态四项P2，按 [5C1 R1执行单](../tasks/2026-10-30-original-web-task-05c1-rework-r1.md)收口，见 [审计](../audits/2026-10-30-original-web-task-05c1-review-round-1.md)。不进入5C2/5C3/6，用户实例不切换。

- 2026-10-30 Task5B R1验收通过：主修复 `0ab4554`、补充 `8dace67`、tip `710d673bc1458c19e3f4d7301fc4d7680bd22fce` 关闭首次存储根与热点旧缓存两项。主审117 passed/1 skipped、Node145 passed、浏览器5B五组/5A四组/Task3 33组/Task4八组与build/lint通过，见 [5B验收](../audits/2026-10-30-original-web-task-05b-accepted.md)。5C按执行域分组，下一项仅 [5C1技能与画像构建](../tasks/2026-10-30-original-web-task-05c1-skills-profile.md)，账号/分析与发布/导出后续下发，用户实例不切换。

- 2026-10-30 Task 5B 首轮复审暂不通过：实施 `a9c89e5`、tip `38955b4c8707e07e51d6153abe6794dbb9a67ed1`；主审后端95 passed/1 skipped、Node144 passed、浏览器5B四组/5A四组/Task3 33组/Task4八组及build/lint通过。仅缺失数据文件的父目录链接首次写边界、热点旧缓存伪装刚更新两项P2，按 [5B R1执行单](../tasks/2026-10-30-original-web-task-05b-rework-r1.md)收口，见 [审计](../audits/2026-10-30-original-web-task-05b-review-round-1.md)；不进入5C/6，用户实例不切换。

- 2026-10-30 Task 5A R1 验收通过：修复 `16b634e`、tip `7b9d76f73dd730f63b74a63f3cd2e81c9436dbef` 关闭配置版本、画像链接和媒体错误流三项；主审定向93 passed、相关回归53 passed、Node143 passed、浏览器5A四组/Task3 33组/Task4八组与build/lint通过。见 [5A验收](../audits/2026-10-30-original-web-task-05a-accepted.md)。下一项仅 [5B工作台数据](../tasks/2026-10-30-original-web-task-05b-workbench-data.md)，5C/6仍待处理，用户实例不切换。

- 2026-10-30 Task 5A 首轮复审暂不通过：tip `ea3066471811daeb2f51211dea0162578cbfe483`；主审 API/gateway 61 passed、Node143 passed、浏览器5A四组/Task3 33组/Task4八组与build/lint通过。独立反例确认文件配置版本错配、画像链接根外读取、媒体错误流未消费即解析三项P2。仅按 [5A R1执行单](../tasks/2026-10-30-original-web-task-05a-rework-r1.md)收口，见 [审计记录](../audits/2026-10-30-original-web-task-05a-review-round-1.md)；不进入5B/5C/6，用户实例不切换。

- 2026-10-30 Task 4 验收通过：生产 `c0eadbeeed7c69fbfcafebe9bee4d21afa8dcfdc`、证据 tip `1b57e1927052d930e13779239ff2bc8907895ae5`；主审 Node 136 passed、新浏览器8/8、Task3回归33/33、static3 passed、build/lint通过。见 [Task4验收](../audits/2026-10-30-original-web-task-04-accepted.md)。Task5按原三组分批验收，当前仅 [5A基础工作区](../tasks/2026-10-30-original-web-task-05a-workspace-api.md)，用户实例不切换。

- 2026-10-30 Task 3 验收通过：R3 生产 `4dc6ee5497963cd58d6fd957865702bf4002481f`、证据 tip `55501036f31ff5880db87436bd35e3c5d24653d1` 关闭未知状态反例；主审 Node 131 passed、完整浏览器 33/33、build/lint及原探针通过。见 [Task 3 验收](../audits/2026-10-30-original-web-task-03-accepted.md)。下一项仅 [Task 4 原主题登录/设置](../tasks/2026-10-30-original-web-task-04-login-theme.md)，用户实例不切换。

- 2026-10-30 Task 3 R2 复审：生产 `1dc7f0a6af2199f365b93b71b87d149eb57e0c04`、tip `8b914403dfbf7dd3758a81ee992968f2dc796cb0` 关闭原 gate 丢卡交错，主审 Node 131 passed、浏览器 31/31、build/lint 通过；仅非空未知状态被当终态一项 P2 待收窄。按 [Task 3 R3](../tasks/2026-10-30-original-web-task-03-rework-r3.md)小范围修正，不重做恢复方案，不进入 Task 4/5。

- 2026-10-29 Task 3 R1 复审：生产 `a1987ca5b337f73c7af064571f324b8fd8bba136`、tip `1b461bac6199ed7d2766489c847ebeaf4f1b1d1e` 关闭原三项，主审 Node 131 passed、浏览器 28/28、build/lint 通过；另确认状态查询未回时重连丢题卡一项 P2。仅按 [Task 3 R2](../tasks/2026-10-29-original-web-task-03-rework-r2.md)定点收口，保留原关闭结果，不进入 Task 4/5。

- 2026-10-29 Task 3 首轮复审：生产 `2c6bc6ca74443e38748312e9c935744157306318`、tip `8f1c11253b828eacc8d53f60107356248956bb47` 暂不通过，仅当前401未卸载私人树、回答临时失败丢卡、思考等事件游标未推进三项。原组件复用成立；主审 Node 131 passed、build/lint 通过，三条真实构建浏览器反例已复现。按 [Task 3 R1](../tasks/2026-10-29-original-web-task-03-rework-r1.md)定点修复，用户实例不切换，不进入 Task 4/5。

- 2026-10-29 Task 2 R1 复审通过：`4bb993bb353f23f5eec06442d2f6ce1c6c2e3090` 采用共享受管 manifest 的正式装配校验关闭 T2-R1-01；主审同口径 166 passed，CLI 拒绝/继承/收窄与旧 Run 独立探针通过。见 [验收记录](../audits/2026-10-29-original-web-task-02-accepted.md)。下一项仅 [Task 3 原组件共用认证/数据层](../tasks/2026-10-29-original-web-task-03-shared-web.md)，用户实例不切换。

- 2026-10-29 Task 2 首轮复审：生产 `f785d444874c6a804bc27702e57cb430aeca1216`、证据 tip `68789d3a36c96b3d087774e5aafc7cd33c18e2f5` 暂不整体通过，仅 T2-R1-01 运行目录与实际固定镜像目录未绑定一项 P2。已验证 flash 链路/同事务固定保留；主审相关 159 passed、Task 1 回归 156 passed/4 skipped。仅按 [Task 2 R1](../tasks/2026-10-29-original-web-task-02-rework-r1.md)收口，不进入 Task 3，用户实例保持不变。

- 2026-10-29 Task 1 复审通过：`bcf001a0ff5a9ba7242ba6f686a9ce7eb341081d` 关闭 T1-R1-01；主审 156 passed/4 skipped，原 Unicode/超长 status 与后续响应探针通过。见 [验收记录](../audits/2026-10-29-original-web-task-01-accepted.md)。下一项仅 [Task 2 模型/Harness 链路](../tasks/2026-10-29-original-web-task-02-managed-model.md)，继续原隔离 worktree，用户实例仍保留。

- 2026-10-29 首轮复审：Task 1 `9f95e5dfea96312550eecd3101ed61f7188ab68b` 暂不通过，仅 T1-R1-01 无效 status 解析终止 RPC reader 一项 P2；主审四文件 147 passed/4 skipped 仍不能覆盖该反例。按 [单项返工单](../tasks/2026-10-29-original-web-task-01-rework-r1.md)继续原隔离 worktree，不进入 Task 2；用户实例保持不变。

- 已完成：计划审阅及用户开始授权、运行实例只读盘点。
- 已完成：Task 1 聊天失败诊断及 R1 复审。
- 已完成：Task 2 模型/Harness 链路及 R1 复审。
- 已完成：Task 3 原组件/认证/会话适配及 R1–R3 复审。
- 已完成：Task 4 原主题登录/设置/品牌资源验收。
- 已完成：Task 5A 基础工作区与三项R1复审。
- 已完成：Task5B工作台数据及两项R1复审。
- 已完成：Task5C1技能与画像构建及R1/R2复审。
- 已完成：Task5C2账号/登录/分析及R1复审。
- 已完成：Task5C3发布工作台/导出及R1/R2复审，Task1–5功能组分组验收完成。
- 进行中：Task6A隔离正式部署整合、真实双worker与受控Harness链路、既有Windows/Linux运维手册候选补丁；交付后独立复审。
- 待处理：Task6B真实模型最小验收与用户实例维护切换；需用户明确授权，不自动执行或上线。
- 运行策略：沿用当前用户容器作为故障现场/对照，不向其注入源码或替换镜像；测试不得复用其卷、workspace、Key、邀请、Cookie、socket。盘点见 [运行基线](../tasks/2026-10-29-original-web-running-baseline.json)。

## Global Constraints

- Windows Python：`C:\Users\hyh\anaconda3\envs\Agent\python.exe`；中文计划/注释/提交。
- 不复制或重写原 Web；一套原页面、一份前端构建，local/control 只改变数据/权限。
- control 不运行用户 Agent，不把真实 Key 或本机 .dsh/HOME 发给 worker；保留 CSRF、签名、token 范围、路径和网络隔离。
- 不直接把 control 指向 LocalApp；身份确认前禁止旧私有缓存、API、副作用和权限闪现。
- 不删除数据/密钥/旧会话，不自动重发失败 prompt，不改 native_id 伪装恢复。
- 真实供应商、外部发布、平台登录的实测方式另由用户确认；受控测试先行。
- 阶段验收不等于全部完成；原 Web 的功能表全部有结论后才整体交付。

## Review Focus

1. A 的慢响应/SSE/文件预览在注销并登录 B 后返回，不能提交到 B 的原组件树。
2. 模型配置从旧 chat 改为 flash，不能出现 UI、ACP、broker 三份不同模型；旧 Run 固定旧版本。
3. 原 settings 的环境安装/底座命令分支不能让成员改受管命令或写全局 .env。
4. 原页面的 URL、media、上传、SSE 不全经过同一 request，不能遗漏认证/CSRF/撤销检查。
5. 停止和异常恢复不得重复 prompt/发布，退出/切页后既有任务及容量收尾语义保持。

## 文件职责

| 文件 | 计划处理 |
|---|---|
| App.tsx、Sidebar.tsx、ChatPage.tsx、MessageBubble.tsx、QuestionCards.tsx | 保留原布局/渲染，改成消费统一 workspace 数据 |
| LoginPage.tsx、ThemeToggle.tsx、styles/index.css、styles/theme.css | 登录样式使用既有品牌/主题；主题偏好沿用 |
| 新 `components/AuthBoundary.tsx` | 模式/身份确定前阻止私有挂载，401/登出处理 |
| 新 `lib/workspaceClient.ts` | local/control client 类型和适配，不另写 UI |
| 新 `hooks/useWorkspace.ts` | 身份生命周期、会话和运行状态；复用 control/auth 纯工具 |
| lib/api.ts、lib/auth.ts、lib/control.ts、lib/store.ts | 统一 request、CSRF、取消、身份缓存和流协议适配 |
| SettingsPanel.tsx、ControlSettingsPage.tsx、AdminSettingsPage.tsx | 合并到原设置入口，权限与数据逻辑复用，删除独立页面外壳 |
| easel/worker/runs.py、easel/harnesses/rpc.py | 可脱敏错误分类/诊断，不公开原始 stderr |
| easel/control/cli.py、config.py、snapshot.py、broker.py | 受管有效模型进入版本化快照和 broker 白名单 |
| deploy/multiuser/managed/acp.patch.yml、acp-profile.json、smoke_worker.py | 去除过时模型分叉，核对锁定 Harness 的实际模型选择 |
| easel/control/app.py、policy.py；easel/gateway/app.py；web/app.py | 原 API 的授权接线、受控转发与 worker 路径 |

文件名未写完整路径的前端项均在 `web/frontend/src/`；后续功能实际命中旧 handler/存储模块时只改对照表列出的具体文件，不全仓重构。

### Task 1：定位失败并给聊天错误可用诊断

**Files:** worker/runs.py、harnesses/rpc.py；`tests/multiuser/test_runs.py`、`tests/test_agent_harnesses.py`、`tests/test_rpc_client_pressure.py`；新增 `tests/multiuser/test_run_diagnostics.py`。

**接口:** worker 的安全错误事件保持 error 语义，增加稳定分类及现有 runId 关联；运维诊断只记录错误阶段、类型、状态码和受限脱敏摘要，不保存完整环境/凭证/请求体。正常 Run/终态结构保持。

- [ ] 红测覆盖子进程缺失、ACP 初始化失败、模型选择失败、broker invalid_request、上游 401/429/5xx；错误不能全部变成“本机 CLI”。假 Key/Authorization/邀请不得进入 UI/日志。
- [ ] 读取现有失败 Run 的无秘密元数据，增加必要诊断；受控 provider/ACP fixture 确定性复现原失败阶段。不先假定是模型 Key 错误。
- [ ] 以命名错误映射与受限诊断替代吞异常；原始 stderr 仅保留有界缓冲并在输出前净化，不能无界采集。
- [ ] 运行 test_runs、实际 ACP 测试和新增诊断测试，记录旧版失败/新版通过。提交 `fix: 补齐受管聊天失败诊断`。

**完成:** 新失败能给出有证据的阶段/分类和可关联任务，受控反例能复现并证明诊断有效；不靠重新输入 Key 猜测。原已结束 Run 的 stderr 已丢弃，若无法追溯直接原因须明确记录，不能伪造根因、重发原 prompt 或为了补日志擅自重启用户实例；Task 2 用隔离的真实 Harness/受控 provider 继续定位。

### Task 2：统一受管模型，打通 Harness 聊天

**Files:** control/cli.py、config.py、snapshot.py、proxy.py、worker/config.py、runs.py、managed profile/patch、smoke_worker.py；`tests/multiuser/test_managed_profile.py`、test_provider_adapters.py、test_proxy.py；新增 `tests/integration/multiuser/test_managed_chat_model.py`。

**接口:** 同一版本的有效 chat 路由提供 provider/model/allowed_models/公开目录；broker 快照与签名 Agent 模型在同一接纳过程中固定。使用原 ACP model 选择能力，modelId 严格来自锁定 Harness 实际目录。不增加任意用户 overlay/命令入口。

- [ ] 红测：管理端 flash、ACP 默认 chat 的现状被复现；配置更新中途接纳只能得到整份旧/新快照，不能混合。未知模型/不支持协议在接纳前明确拒绝。
- [ ] 修复 Task 1 证实的直接触发点，并消除静态 profile 模型与受管 chat 分叉；生成受管模型目录/选择必须受信且不带 Key。
- [ ] 真实 Harness + 受控 OpenAI-compatible provider 验证新建、追问、resume、权限、停止；断言收到的 model 正确、没有真实 Key 进入 worker。
- [ ] 运行 profile/provider/proxy/worker 与实际容器协议测试；记录源码与镜像匹配。提交 `fix: 统一 ACP 与受管聊天模型`。

**完成:** 测试环境真实 Harness 可用；用户批准真实通道后，一条小真实请求和同会话追问成功才关闭真实模型子项，未执行不冒充通过。

### Task 3：统一原 Web 的认证与数据层

**Files:** App.tsx、lib/api.ts/auth.ts/control.ts/store.ts；新增 AuthBoundary.tsx、workspaceClient.ts、useWorkspace.ts；原 Sidebar/ChatPage/QuestionCards 的最小接口适配；tests/frontend/multiuserAuth.test.mjs、multiuserHistory.test.mjs、questionCards.test.mjs；新增 tests/frontend/workspaceClient.test.mjs。

**接口:** workspaceClient 明确提供 list/create/update/deleteSession、loadMessages、submitTurn、subscribeRun、stopRun、answerQuestion；返回原 ChatPage 消费的视图类型。control 用服务端目录和 Run，local 保持旧兼容。UI 导航与数据获取分开，不能切页重发 prompt。

- [ ] 红测：control 登录后仍渲染旧 Sidebar/ChatPage，登录前不读全局 session 缓存；A 慢响应在 B 登录后不可提交，跨标签也隔离。
- [ ] 提取认证边界与 client；将 LocalApp 原布局演进为共用 WorkspaceApp，复用 ControlApp 已有的恢复/游标/幂等纯逻辑。不要复制一份原 UI，也不直接载入旧 LocalApp 副作用。
- [ ] 所有写请求/流入口接入身份/CSRF/AbortSignal；401 清当前身份回登录，503 保持服务错误。保留问题卡、停止与历史顺序回归。
- [ ] Node 测试 + build；以测试浏览器验证原布局、身份切换及 local 原有使用。提交 `refactor: 原工作台共用身份与会话适配层`。

**完成:** 同一原组件树能在 local/control 展示并正确完成聊天；未接通的其它页面不得宣称已可用。

### Task 4：原主题登录页与设置入口

**Files:** LoginPage.tsx、ThemeToggle.tsx、Sidebar.tsx、SettingsPanel.tsx、ControlSettingsPage.tsx、AdminSettingsPage.tsx、styles/index.css/theme.css；新增 `tests/frontend/browser_original_web_login.py`，扩充 multiuserSettings.test.mjs。

**接口:** LoginPage 沿用 onAuthenticated/notice；样式改为共享 class/token。Sidebar 增加身份/注销及管理员设置入口，SettingsPanel 复用个人/管理员数据逻辑，普通成员不出现可执行管理员配置动作。

- [ ] 先用浏览器测试固定浅/深色、桌面/窄屏、回车/焦点、一次性邀请错误、429 和 auth 503；设置普通成员写管理员接口必须 403。
- [ ] 登录页复用原 logo/字体/青蓝渐变/圆角/ThemeToggle；移除 EASEL CONTROL 和技术性页脚，解释一次性邀请及重新获取。
- [ ] 将个人/全体默认/成员邀请融入原设置结构；多人模式不走原本写宿主 .env/安装 CLI 的分支，受管底座只读显示管理员策略。
- [ ] 浏览器截图与原页面逐项对比，检查主题连续/移动端/可访问性；Node settings 回归与 build。提交 `feat: 原主题登录与统一账号设置`。

**完成:** 登录前后像同一产品；不是把截图的精简 ControlApp 改几种颜色。

### Task 5：原 Web 功能逐组授权接线

**Files:** control/app.py、policy.py、gateway/app.py、web/app.py；必要时新增 `easel/control/workspace_api.py` 集中受控业务映射；原页面/API 模块仅改明确接口；新增 `tests/multiuser/test_original_web_api.py`。

**接口:** 每个原 API 保持组件需要的返回形状；control 绑定身份，gateway 精确 method/path/query/body 签名，worker 固定本空间路径。任务操作使用既有 RunManager/容量/权限机制，不另造无账本执行。

- [ ] 先生成实际组件 → API → 现有 handler → policy → gateway → workspace 存储的对照表，含所有直接 fetch/media/上传/下载，不只检查 lib/api.ts。每组在同一表登记可用/拒绝/未接通。
- [ ] 基础组红测：status/画像、产出列表/上传/预览/下载在原组件可用；A/B 同名文件、路径穿越、symlink/junction、注销后的媒体访问均拒绝越权。
- [ ] 工作台组红测：热点/选题/日历/概览使用本人数据；并发与重启持久化正确，管理员受管只读目录不被写入。
- [ ] 执行/账号组红测：技能、画像构建、平台登录/状态/分析、发布/导出进入受控任务，同账号 profile 互斥，停止/断连不重复外部动作；默认用 fixture，不擅自真实发布。
- [ ] 按上述三组最小接线，每组独立相关回归与提交。普通成员的全局插件/底座安装仍拒绝，页面按受管权限表达，不能绕到 local handler。
- [ ] 对照表无“默认 503 但称可用”残项后，删除 ControlApp 独立布局与旧导航，只保留仍被共用的数据工具；清理过时 UI 测试，保留协议回归。

**完成:** 原 Web 页面及其功能有逐项证据；能力未完成时整个恢复原 Web 任务仍未完成。

### Task 6：真实部署验收与操作手册收口

**Files:** tests/integration/multiuser 下新增原 Web 验收，复用现有 browser_desktop_chat.py 的真实拓扑；更新两份现有运维手册与部署版本信息，不新建第三份用户手册。

- [ ] 在隔离临时项目上运行原 Web 双身份浏览器链路：登录→聊天→文件→画像→账号/技能 fixture→权限→停止→重启→同会话追问；浅/深色截图留证。
- [ ] 用户确认后在实际通道做最小真实模型验收；外部发布/收费/账号状态变化分别确认，不拿 fake provider 冒充。
- [ ] 运行相关前后端回归及 A7 v1/v2 备份/恢复/升级联锁；既有数据/密钥/会话无破坏，失败不自动重复 prompt。
- [ ] 更新手册的模型配置、重新登录、原设置入口、镜像构建与更新/回滚步骤。交付明确“已通过/未执行/仍受限”，独立审查后再安排用户实例切换。

**最终验收:** 用户进入原 Easel Web 而非另一套界面，真实聊天通过，原功能与隔离全部按表验收；生产 Linux 未验证项目保持原状态。

## 验证命令与证据要求

在 `C:\Code\Easel` 执行；下面是实施时的验收命令，本轮仅计划，没有声称新测试已经存在或通过。

```powershell
# Task 1：诊断与 ACP 兼容
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser/test_runs.py tests/multiuser/test_run_diagnostics.py tests/test_agent_harnesses.py tests/test_rpc_client_pressure.py -q
# Task 2：模型、profile、受管转发
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser/test_managed_profile.py tests/multiuser/test_provider_adapters.py tests/multiuser/test_proxy.py -q
# Task 3–4：前端身份、历史、设置及新适配器
node --test tests/frontend/multiuserAuth.test.mjs tests/frontend/multiuserHistory.test.mjs tests/frontend/multiuserSettings.test.mjs tests/frontend/workspaceClient.test.mjs tests/frontend/questionCards.test.mjs
npm --prefix web/frontend run build
# Task 5：原 API 的双用户授权接线
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser/test_original_web_api.py tests/multiuser/test_policy.py tests/multiuser/test_gateway_forward.py -q
# Task 6：运维兼容性（不重复与本次改动无关的全仓测试）
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser/test_operations.py tests/multiuser/test_operations_a7.py tests/multiuser/test_operations_a7_r2.py tests/multiuser/test_desktop_operations_r1.py -q
```

每一项红测需在修复前实际失败，绿测需当前提交 exit 0、无该项 skip/xfail。真实 Harness/Docker/浏览器/供应商分别记录，不把未启用真实环境的 skip 算通过。新浏览器脚本应由既有隔离 fixture 管理服务和随机测试资源，不能依赖开发者当前登录 Cookie。截图与功能表是 Task 4/6 的必交证据。

## 执行建议

先完成 Task 1–2 的可见聊天闭环，再完成 Task 3–4 的共用原 UI 与登录入口，再按 Task 5 接通其余功能。每阶段演示一个用户能用的结果，避免再出现“模块已验收、用户页面仍不能使用”。本计划不估算未经实测的工期，不承诺只改登录页即可恢复所有原功能。
