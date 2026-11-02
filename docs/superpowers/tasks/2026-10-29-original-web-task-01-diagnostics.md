# 原 Web 修复 Task 1：受管聊天失败诊断

状态：用户已授权开始；本单仅 Task 1，代码 Agent 实施，主会话审计。
父基线：`1a8811096c7c86bb9079a5c88b92cd225d1907b1`。

## 必读及任务边界

1. 仓库 AGENTS.md。
2. [修复设计](../specs/2026-10-29-original-web-login-and-chat-repair.md)。
3. [实施计划 Task 1](../plans/2026-10-29-original-web-login-and-chat-repair.md)。
4. [当前运行基线与保护对象](2026-10-29-original-web-running-baseline.json)。

仅改善诊断并复现失败阶段，不修改模型/profile/受管路由，不改原 Web/登录页，不推进 Task 2。不将“有诊断”说成“真实聊天已修好”。

用户容器由主审裁决先沿用作现场/对照，保持运行。不得 Stop/down/restart/exec 写入/替换镜像，不读或复制真实 Key/.env/.dsh/Cookie/邀请，不重新提交已失败或未完成 prompt，不运行真实收费供应商请求。

## 实施环境

- 先报告实际 HEAD、git status、拟修改文件；保护根 README、两份运维手册和主审 spec/plan/audit/tasks/evidence 的既有改动。
- 实施优先使用从本基线创建的隔离 worktree。若执行环境无法访问主审未跟踪文档，仅将上述四份说明只读复制供阅读，不能提交主审文件；不复制 outputs、用户 HOME、密钥或构建缓存。
- Python 固定 `C:\Users\hyh\anaconda3\envs\Agent\python.exe`。从仓库已存在的 pytest/fixture/RPC 子进程测试开始，不先构建大镜像。
- 需要真实 Harness 时使用独立临时 HOME/workspace、受控 provider、假 Key。不得复用用户 control 卷、workspace、现有 provisioner socket 或任何已登录浏览器。
- 若确需容器，按随机 run-id 创建、标签核验、逐项清理；不全局 prune、不按 easel-ws-* 通配删除历史网络。不触碰上轮两个被自动审批阻止清理的临时目录。

## 已知事实

- worker 的 `_supervise` 对普通异常只输出泛化字符串并登记 agent_failed。
- RPC 在 `_drain_stderr` 中排空丢弃 stderr；原异常阶段/类型不能从历史通用错误反推。
- 当前网页 chat 为 deepseek-flash，容器受管 patch 仍 deepseek-chat，是模型配置断层的已知证据；本单不把它直接认定为唯一根因或趁机修模型。

## 允许修改范围

- `easel/harnesses/base.py`：确有需要时给 HarnessError 增加可选诊断字段，保持 `HarnessError(message)` 和已有异常语义兼容。
- `easel/harnesses/rpc.py`、`easel/harnesses/acp.py`：阶段/结构化错误关联，以及必要的有界诊断。不改 ACP 权限、协议兼容、连接复用和停止逻辑。
- `easel/worker/runs.py`：保留安全 error 事件和终态语义，记录可追踪分类。
- `tests/multiuser/test_run_diagnostics.py`（新增）、`tests/multiuser/test_runs.py`、`tests/test_agent_harnesses.py`、`tests/test_rpc_client_pressure.py` 及专用假子进程 fixture。
- 实施证据放在现有 `docs/evidence/` 下新建的 `original-web-task-01` 子目录；不要改主审文件。若确需一个小的公共诊断模块，先在实施说明写明职责与必要性，不建立新诊断服务/数据库。

## 必须证明的行为

1. 子进程启动、ACP initialize、session/model 选择、prompt/RPC 断连可区分阶段，正常 Run/流式文本/问题卡/终态协议兼容。
2. 上游或 broker **实际提供结构化证据时**才能分类为鉴权/限速/请求拒绝/服务不可用；无依据时使用 unknown/agent_failed，不能根据模糊文本猜出“Key 错误”。
3. 用户可见错误至少有安全中文原因/类别及本轮关联方式；不再只有“请检查本机 CLI”。error 事件仍兼容当前 string 消费者，不强迫本轮改 ControlApp。
4. 若保留 stderr，必须有固定有界上限、持续排空、关闭/取消后无遗留任务；不把完整 stderr 或异常 repr/traceback 原样写日志或事件。限额在代码和测试中明确，防大输出/无换行输出堵塞或内存增长。
5. 假 Key、Bearer、broker/service token、邀请、Cookie、prompt、用户路径与完整命令/env 不进入 UI、持久事件或运维日志。无法可靠清洗的内容只保留安全分类，不输出原文。
6. 同一 workspace 不同 Run 的诊断不可串用；取消、超时、进程退出、旧 RPC 清理不会污染新一轮或改变已有租约/会话锁语义。

## 执行与验收

- [ ] 先写确定性红测，至少覆盖启动/initialize/model/RPC 错误及证据不足的 fallback；显示旧提交的真实失败，不以字符串 marker 断言替代行为测试。
- [ ] 最小实现并跑绿；保留有结构化状态与无状态两条路径。不可为了分类主动发送另一条模型 prompt。
- [ ] 至少一个真实假 ACP 子进程产生 stderr/协议错误，验证有界排空与清理；大 stderr 与取消反例使用受控 gate，不靠长 sleep。
- [ ] 在同一测试输出中核对事件/日志/持久记录的秘密 sentinel 均未泄露；旧 `HarnessError(message)` 兼容。
- [ ] 运行：

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser/test_run_diagnostics.py tests/multiuser/test_runs.py tests/test_agent_harnesses.py tests/test_rpc_client_pressure.py -q
git diff --check
```

- [ ] 只在改动影响另一个已知行为时补充相应回归，不默认重跑全部前后端/重建所有镜像。
- [ ] 记录能证明的失败阶段；若旧 Run 的根因因 stderr 已丢失而不可追溯，明确写 unknown/history-unavailable。已知模型配置分叉单独列作 Task 2 输入，不伪称这次异常已精准归因。
- [ ] 显式文件列表提交，中文提交信息；交付后停止，不自动进入模型修复或前端任务。

## 交付格式

父/新完整 SHA、精确文件清单、红绿命令/实际结果、错误阶段与安全字段定义、stderr 上限/脱敏与清理证据、兼容性回归、真实与 fixture/not-run 的区分、是否接触运行实例（预期否）、测试资源清理清单。主审会独立重跑定向测试并构造反例，再决定是否验收和下发 Task 2。
