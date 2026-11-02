# 原 Web Task5C1 R2：技能题卡临时状态失败后自动恢复

父基线：`4d46c668021e0de884aee0699d6bbe88030cb167`。
继续原worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。
仅T5C1-R2-01 / P2，依据 [R1复审](../audits/2026-10-30-original-web-task-05c1-review-round-2.md)。模型、画像指令、准备互斥和正常题卡入口保留关闭，不重开其它任务。

## 最小修复与红绿

- SkillDrawer状态轮询临时503/断网时保留题卡与Run标识，设置待确认/禁用提交，但不能停止后续恢复查询；在当前组件/任务仍有效时继续有界轮询或退避。
- 后续200明确pending恢复可提交；未知状态继续待确认；明确已答/过期/终态正常清卡收尾。401仍走身份卸载，卸载/切换/取消时清定时器/请求，不重复轮询或写旧状态。
- 不自动回答、不清卡冒充恢复、不重新POST技能任务/创建Run。停止与输入保留不变，不新增后台服务或重写题卡体系。
- 先红测真实build+原隔离fixture：技能ask题卡已确认后，仅一次GET skill/status返回503，随后恢复200，等待至少原轮询两轮；必须再次请求、恢复提交、成功回答到终态，executions仍1。可附一次断网等价分支及卸载不再查询断言。
- 主审只读探针为root docs/evidence下original-web-task-05c1-review/probe_task_status_503.py；复跑需传实施worktree位置参数，不修改探针。

## 回归与交付

预计仅SkillDrawer必要轮询收尾/测试与证据。相关前端Node/build/lint、5C1六组加新恢复用例、5B五组/5A四组/Task3 33组/Task4八组保持；无后端生产改动时无需重跑无关全仓或重建Docker/Harness/Chromium。

证据放本组R2子目录，列单项红绿、原探针结果、定时器/请求清理、原矩阵及not-run。交付父/新SHA及精确文件职责；原始日志可去尾随空白，不改写失败含义或历史截图。

用户运行实例/卷/socket/Key/Cookie/邀请不动，不重放用户prompt、不付费/真实发布、不全局prune。主审audit/task/plan/probe只读。提交后停止复审，不进入5C2/5C3/6、不自动上线。
