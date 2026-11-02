# 原 Web 修复 Task 1：开工基线与运行实例裁决

日期：2026-10-29（Asia/Shanghai）。
代码基线：`1a8811096c7c86bb9079a5c88b92cd225d1907b1`。

## 分工与当前节点

用户明确开始原 Web/聊天修复任务，并指定本会话负责审计。延续代码 Agent 实施、主会话复核的工作方式；本轮建立 Task 1 执行单、保护对象清单和验收基线，未启动另一聊天、未直接修改产品代码。当前待代码 Agent 按执行单实施并提交，不表示诊断改动已完成。

执行单：[Task 1](../tasks/2026-10-29-original-web-task-01-diagnostics.md)。
总计划：[实施计划](../plans/2026-10-29-original-web-login-and-chat-repair.md)。

## 容器裁决

保留用户当前 `easel-desktop-multi` control 和其 worker/gateway/sidecar，作为故障现场和后续对照；不是本轮测试资源。control/sidecar 当前健康，四个容器均 running。只读 inspect 的完整 ID/镜像见 [运行基线](../tasks/2026-10-29-original-web-running-baseline.json)。

诊断代码验证使用临时目录、假凭证、受控 ACP/provider，必要时独立测试容器。不得替换现有镜像、重启服务、热拷代码、借用用户卷/socket、复制秘密或重发用户 prompt。后续确需用户实例验证，先准备已审版本和可回滚切换步骤，再由主会话安排受控切换。

## 主审新运行基线

在 `C:\Code\Easel`、Agent Python 执行：

```text
C:\Users\hyh\anaconda3\envs\Agent\python.exe -m pytest tests/multiuser/test_runs.py tests/test_agent_harnesses.py tests/test_rpc_client_pressure.py -q
130 passed, 4 skipped in 22.73s
```

这是既有代码回归基线，不是新诊断测试结果，不证明真实供应商聊天可用。当前已知模型配置分叉和通用错误仍未修复。四项 skipped 不能算真实环境验收通过。

主审本轮没有写运行数据库、生成邀请、读取真实 Key、调用供应商或操作 Docker 资源生命周期。

## 审计门槛

先审 Task 1 的确定性红绿、阶段/分类依据、有界输出、秘密不泄露、原 RPC/停止/会话兼容和真实 fixture；有证据才关闭问题。旧 Run 的 stderr 已丢失时允许如实记历史不可追溯，不能要求实施方凭空还原或伪造根因。

Task 1 通过后单独下发 Task 2 的模型/Harness 链路修复。未通过只定点返工，不扩大到前端、配置迁移或生产部署。整体最终目标仍是原 Easel Web 加登录，不以阶段验收替代整体验收。
