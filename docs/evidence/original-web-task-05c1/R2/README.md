# Task 5C1 R2 证据：技能状态临时 503 后恢复轮询

日期：2026-10-30（Asia/Shanghai）。
父基线：`4d46c668021e0de884aee0699d6bbe88030cb167`。
实施 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。
本轮仅修复 T5C1-R2-01；不进入 5C2/5C3/6，不自动上线。

## 最小修复

`web/frontend/src/components/SkillDrawer.tsx`：

- 技能状态查询临时 503/断网时保留当前题卡、`activeRunId` 和待确认状态，设置 `questionPending=true` 并在 1500ms 后继续 `check()`；不再因为一次外层 catch 退出而结束唯一轮询链。
- 后续成功状态若 question 仍为 pending，则重新启用提交；明确 answered/resolved/expired/not_found 才清卡；终态正常收尾。
- `AuthError 401` 直接返回，不安排新轮询；组件 effect 清理仍会 abort 请求并清除定时器，切换/卸载后不再写旧状态。
- 不自动回答、不清卡、不重新 POST 技能任务、不创建新 Run；停止和输入保留不变。

## 红绿

- 红测：主审只读探针 `probe_task_status_503.py` 对父基线原 build 复跑：
  `polls_at_failure=1`、`polls_after_4_seconds=1`、`ui_still_pending=true`、`submit_still_disabled=true`。
  证据：`logs/probe-before-503.json`。
- 绿测：同一探针在修复 build 复跑：
  `polls_at_failure=1`、`polls_after_4_seconds=3`、`ui_still_pending=false`、`submit_still_disabled=false`、`executions=1`。
  证据：`logs/probe-after-503.json`。

浏览器新增 `scenario_skill_status_recovery`：技能题卡确认后仅注入一次 `/api/skill/status` 503，随后恢复 200；断言至少再次请求、题卡恢复可提交、允许后继续到 `SKILL-ANSWERED`，executions 仍只增加一次。

## 回归

| 项目 | 结果 |
|---|---|
| R2 5C1 浏览器 | **7/7，`skill_status_recovery=True`，`FAILURES=[]`** |
| 5B 浏览器 | **5/5，`FAILURES=[]`** |
| 5A 浏览器 | **4/4，`FAILURES=[]`** |
| Task 3 浏览器 | **33/33，`FAILURES=[]`** |
| Task 4 浏览器 | **8/8，`FAILURES=[]`** |
| Node 全量 | **146 passed, 0 failed** |
| build | 成功，`dist/assets/index-Czb1NDU1.js` |
| lint | 退出 0 |

本轮没有后端生产改动，未重跑无关全仓、Docker/Harness/Chromium 全栈；R1 的模型、画像指令、准备期互斥证据保持有效。

## 文件与边界

- `web/frontend/src/components/SkillDrawer.tsx`：唯一生产修复。
- `tests/frontend/browser_original_web_task05c1.py`：R2 状态恢复场景。
- 本目录：R2 探针红绿、浏览器回归、构建/lint、截图。

截图：`browser/01`–`12`；其中 `12-skill-status-recovered.png` 为本次恢复场景。
历史 5A/5B/Task3/Task4/5C1 R1 截图已恢复，未覆盖。

未执行：真实 Harness/provider、账号/分析、发布/导出、完整 Compose/生产 Linux、最终上线。用户运行实例、卷/socket、Key/Cookie/邀请未操作；未重放 prompt、未付费、未发布、未全局 prune；临时浏览器/测试资源清理完成。
