# 原 Web Task 5C1 验收：技能与画像构建及 R2 通过

日期：2026-10-31（Asia/Shanghai）。R2 父基线 `4d46c668021e0de884aee0699d6bbe88030cb167`；修复提交 `977e9b6a314ce427752c5d1dd3a4e040c96ba52f`；证据 tip `654305a69c1d68d6b157b608dbd2c7d5f96d6298`。

实施 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## 裁决

Task 5C1 通过。[R1 复审](2026-10-30-original-web-task-05c1-review-round-2.md)遗留的 T5C1-R2-01 已关闭，未发现本轮新增阻塞。生产补丁仅涉及 SkillDrawer 的临时状态失败恢复与 401 收尾，未修改后端。

R1 已确认的模型快照与 broker 一致、画像完整增强指令、准备期互斥，以及正常题卡显示/回答/停止结论保留。下一阶段为 5C2 账号/登录/分析，仍待独立实施验收；5C3 发布/导出与 Task 6 整体验收未完成。本裁决不自动推进实施、切换实例或上线。

## T5C1-R2-01 关闭证据

原失败分支在设置待确认状态后不再安排查询；现遇到 503/网络错误，保留题卡和 Run 标识，并在 1500ms 后继续查询。成功确认 pending 后恢复提交；未知状态和明确终态沿用已审规则。401 不安排下一轮；effect 卸载通过 alive、AbortController 与 clearTimeout 清理，查询按串行完成后安排，未新增重复轮询链。

主审独立运行原[只读探针](../../evidence/original-web-task-05c1-review/probe_task_status_503.py)，使用当前真实前端构建与隔离假 ACP/RunManager：题卡已确认后只注入一次 skill/status 503，随后放行。失败时查询次数 1，4.2 秒后 3；后端 200、awaiting_permission、questionPending=false；前端题卡仍为 1，ui_still_pending=false、submit_still_disabled=false，executions=1。临时 Run 停止、目录清理完成，退出 0。主审输出见 [R2 探针结果](../../evidence/original-web-task-05c1-review/probe_task_status_503_r2_output.json)。独立复审者复跑得到相同结果，无阻塞意见。

新增浏览器场景进一步验证：同一题卡恢复可提交，回答后到 SKILL-ANSWERED 终态，执行次数只增加一次；不是重新提交技能任务伪装恢复。作者父基线红态证据与上轮主审独立失败结果一致。本轮未重新构建父基线。

## 主审新运行

- 全 frontend Node：146 passed、0 failed。
- tsc/Vite build 成功，产物 index-Czb1NDU1.js；oxlint 退出 0。
- 5C1 浏览器 7/7、5B 5/5、5A 4/4、Task 3 33/33、Task 4 8/8，共 57 组，全部 FAILURES=[]，各脚本退出 0。
- 本轮源码/测试补丁 diff --check 通过；实施树干净。完整 R2 diff 仍提示 lint-final.txt 末尾多余空行，属日志格式项，不构成生产阻塞，不能称完整 diff 检查通过。
- 浏览器证据改写到独立临时目录并清理，未覆盖交付或历史截图。探针 Windows Playwright 关闭连接时出现既有 Win10054 回调警告；5A fixture 的 coroutine 未等待警告和构建 chunk 提示保留，未影响本轮断言，不扩为本组生产阻塞。

本轮没有后端生产变更，未重复全后端、Docker/Harness 镜像构建；R1 主审 46+193 项后端测试是上轮结果，不冒充本轮新运行。网络错误与卸载/401 收尾经过代码复核，本轮独立故障注入针对 503，未另称已做所有网络故障实测。

## 边界

真实供应商与用户真实聊天、所有技能外部依赖、账号登录/分析、发布/导出、完整 Compose/生产 Linux、最终上线均未由本组验收。受控 provider/假 ACP 不等于真实供应商通过。

用户运行容器、卷/socket、Key/Cookie/邀请未操作，未重放用户 prompt、未付费、未真实发布、未合并或部署。主审仅在 root 保存审计及计划状态，未提交实施分支。
