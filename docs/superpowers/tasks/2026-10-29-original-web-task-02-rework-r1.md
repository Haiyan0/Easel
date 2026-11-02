# 原 Web 修复 Task 2 R1：绑定实际模型目录与运行配置

父基线：`68789d3a36c96b3d087774e5aafc7cd33c18e2f5`（含证据修正）。
继续 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。
仅一项 T2-R1-01 / P2，依据 [首轮审计](../audits/2026-10-29-original-web-task-02-review-round-1.md)。

## 问题与目标

镜像 manifest/patch 目录相互校验，但 managed-agent.json 的 agent.models 仍独立手填。正式配置加载接受镜像没有的名称，ConfigService/BrokerService 只对这份自报目录校验，WorkerRelay 已创建并派发该 Run，到 ACP 选模型才发现失败。

要求实际可用目录来自受信、与目标 runtime/镜像绑定的现有公共元数据；部署配置可合法收窄，不得自报额外模型。绑定缺失/漂移/不能验证必须在正式装配或接纳前拒绝，不能只在 README 要求人工一致，也不能再增加一份没有绑定的手填列表。

复用现有 manifest、构建验证或可信部署契约，选择最小可证明实现；不要求新服务/注册表/调度状态机。若要扩展只读能力/身份元数据，在交付说明精确列出来源、校验时点和为何必要，不能引入任意 inspect/命令执行入口。

## 范围

允许修改本项所需的配置加载/CLI装配、目录验证、最小部署/构建公共资源及测试；保留 Task 2 已通过的 issue_with_route 同事务固定、ACP 映射和 Task 1 诊断。确需超出原 13 个生产/部署文件时在实施说明列出具体职责，不重构其它系统。

不改原 Web/UI，不切换用户实例，不读真实 Key/.env/.dsh/邀请，不重放 prompt，不发真实供应商请求；主审 spec/plan/task/audit/probe 保持只读。测试使用临时新资源和假凭证。

## 验收

- [ ] 先写行为红测，走正式配置加载/CLI装配→发布/接纳：runtime 声明镜像没有的名称；受管目录资源与目标版本不匹配；目录资源缺失/不可验证。
- [ ] 以上失败没有创建/派发新 Run、签发 token 或发送 prompt。若在服务启动前拒绝，证明未打开/改动正式控制库与密钥；不要先启动一轮才用 ACP 错误兜底。
- [ ] 合法同目录与合法收窄正常，新 Run 的模型、broker/token、签名快照仍同版本。
- [ ] 旧 Run 幂等/恢复保留旧固定快照，不因当前目录变化自动换模型/换 native_id/重发 prompt。
- [ ] 跑本项新增测试、原模型/profile/relay/config/broker相关回归，以及 Task 1 诊断必要回归；按实际影响选择，不默认全仓测试。
- [ ] 镜像内代码/资源改变才增量重建相应镜像并做目录/源码检查，补受影响真实门槛；原未变化的 flash/权限/停止演练证据保留，不无缓存重建全栈。
- [ ] 明确交付目录绑定依据、版本/兼容策略、红绿、合法路径与拒绝路径、真实/fixture/not-run区分及资源清理。

主审探针在 root 的 `docs/evidence/original-web-task-02-review/probe_catalog.py`，不修改它来变绿。新代码/测试/证据显式文件提交，交付父/新完整 SHA 后停止复审，不进入 Task 3、不自动上线。
