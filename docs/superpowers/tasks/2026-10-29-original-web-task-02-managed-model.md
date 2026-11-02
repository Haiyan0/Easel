# 原 Web 修复 Task 2：统一受管模型并验证 Harness 聊天

状态：Task 1 已验收，当前仅授权 Task 2。主会话负责审计，代码 Agent 实施；完成后停止，不自动进入 Task 3 或替换用户实例。
父基线：`bcf001a0ff5a9ba7242ba6f686a9ce7eb341081d`。
继续原隔离 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`；无需为改名再建第二个 worktree。

## 必读

- AGENTS.md。
- [修复设计](../specs/2026-10-29-original-web-login-and-chat-repair.md)与[总计划 Task 2](../plans/2026-10-29-original-web-login-and-chat-repair.md)。
- [Task 1 验收记录](../audits/2026-10-29-original-web-task-01-accepted.md)。
- [用户实例保护清单](2026-10-29-original-web-running-baseline.json)。

## 必须交付的结果

受管管理员 chat 路由、签名任务中的 ACP 模型选择、Harness 实际模型目录和 broker 白名单来自同一份被固定的配置。管理员选择 deepseek-flash 时，真实 Harness 送到受控 provider 的请求必须是该模型，不能仍隐式请求 deepseek-chat。

保留 Harness/ACP 的 Agent 能力，不改成 API 直连问答；不能将本机 .dsh、.env、Key、浏览器或账号状态复制进 worker。不使用用户运行实例、真实供应商或现有用户 prompt 作为调试探针。

## 先定位，不仅替换一个字符串

1. 先报告 HEAD、状态和拟改文件。核对锁定容器 Harness 基线，而非自动改用本机当前 checkout 或 latest。
2. 复用既有真实 Harness/受控 provider 测试设施与 Task 1 诊断，在隔离 HOME/workspace 复现“管理端 flash、受管 profile chat”的行为。记录 ACP 到哪一阶段、broker/受控 provider 实际看到什么；不能在没有请求证据时将模型分叉称原失败唯一根因。
3. 核对 initialize/new/resume 真实响应中的模型选择能力和 modelId 形状。现有 ACPAdapter 支持 set_config_option/set_model；按真实目录选择，不能臆造 provider/model 拼接格式。
4. 已结束用户 Run 的历史直接原因仍可记 unavailable；隔离复现中若另有直接阻塞，先给出确定证据和最小修复，不能顺手重做认证、前端或运行时。

## 实现约束

- 受管 provider 仍固定 easel-broker；公共模型目录只包含管理员验证过的允许项，不开放任意 endpoint/命令/patch。
- 不把静态 deepseek-chat 简单改成另一个静态 deepseek-flash：后续允许模型改变也要由可信配置驱动。无有效模型/目录时在接纳前明确拒绝，不能启动一轮才发现不可选。
- 逐 Run 固定 chat 的版本、模型、允许目录和 broker 路由；broker token 不能固定新版本而 Agent 模型仍用旧版。配置发布与接纳交错时只能得到整份旧版或新版。
- 复用现有签名快照/接纳记录，不建立第二套 Run 状态机或另一个模型注册表。需要扩展 snapshot 的公开模型元数据时，校验字段类型/大小/白名单并保留旧兼容测试。
- 可信模型元数据进入子进程时，必须通过已验签快照到干净 env/受管配置的明确映射，不从浏览器或用户 HOME 覆盖。只能暴露逐 Run broker token，不能发送真实管理员 Key。
- 模型变更只影响按新配置创建/接纳的工作；旧 Run 重放/恢复仍用旧固定快照。旧 native 会话不能用新 sessionId 伪装续接；版本/模型不兼容要明确拒绝或给出可执行的新建提示。
- Task 1 的无效 status、分类与安全日志回归保持；不为看上游错误打开原始 Key/完整请求日志。

## 修改范围

按实际需要修改 `easel/control/cli.py`、`config.py`、`snapshot.py`、`proxy.py`、`broker.py`，`easel/worker/config.py`、`runs.py`，`deploy/multiuser/managed/acp.patch.yml`、`acp-profile.json`、`smoke_worker.py`、必要的 worker 镜像构建校验。

复用 `tests/multiuser/test_managed_profile.py`、`test_provider_adapters.py`、`test_config.py`、`test_proxy.py`、`test_runs.py`、`test_worker_runtime.py` 及已有真实 Harness 测试；新增 `tests/integration/multiuser/test_managed_chat_model.py` 验证模型链路。若必须新增小型可信配置生成模块，在交付中说明来源/边界，不重构整套架构。确需修改 ACP 适配器时仅限真实协议响应证明的兼容点。

不得改原 Web/UI/登录，不编辑主审 spec/plan/task/audit；本项证据放在现有 `docs/evidence/` 下新建 `original-web-task-02` 子目录。两份运维手册如有本项必要更新可在说明中列出，但不覆盖 root 既有未提交修订；默认留到总体验收阶段统一更新。

## 红绿和真实协议验收

- [ ] 红测：固定旧 profile 导致请求模型与管理员配置不一致；受控服务记录实际 model，不能只检查 patch 文本。
- [ ] 纯逻辑/集成反例：非法模型、空配置、不支持协议、配置发布交错、新旧 Run 固定版本、篡改公开目录/签名、A/B 同时配置不能串用、重试/停止不重复 prompt。
- [ ] 最小实现，定向绿；现有默认/覆盖/密文、快照与 broker 范围保持。
- [ ] 真实锁定 Harness + 假 Key + 受控 provider：initialize/new/prompt、同会话追问/resume、允许/拒绝权限、停止后继续、provider 401/429/5xx、broker 拒绝；记录发送的 model、Run/空间归属和请求次数。上游故障不自动重发。
- [ ] 至少核对一次实际镜像中 profile/代码与交付源码相符。只重建本项受影响镜像，复用未变的基础层/前端/gateway；不得无缓存重建整个栈。

基础定向命令（新增测试实际落地后执行）：

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser/test_managed_profile.py tests/multiuser/test_provider_adapters.py tests/multiuser/test_config.py tests/multiuser/test_proxy.py tests/multiuser/test_worker_runtime.py tests/multiuser/test_runs.py tests/multiuser/test_run_diagnostics.py -q
git diff --check
```

真实 Harness 测试使用仓库已有显式环境开关/fixture，命令、镜像、源码 SHA 必须交付；默认 skip 不算真实执行通过。真实 DeepSeek 通道和用户正在运行实例不在本单自动操作范围，保留为后续受控切换/真实请求验收子项，不能以受控 provider 结果宣称已完成用户真实聊天。

## 资源与交付

使用指定 Agent Python。测试目录/HOME/数据库/Key 一律新建；容器/网络用随机 run-id 与归属标签，清理只针对本轮确认资源。不得 Stop/down/restart 用户实例，不动现有卷/socket，不删除历史 easel-ws-* 网络，不全局 prune。

显式文件提交后给出：父/新完整 SHA、精确文件清单、原失败可证明/不可证明边界、配置到模型的单一来源、版本固定与兼容策略、红绿命令/结果、实际 Harness/镜像/受控 provider 证据、异常/权限/停止结果、秘密不泄露和清理清单。交付后停止等主审复审，不进入 Task 3，不自动上线。
