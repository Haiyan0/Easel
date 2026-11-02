# 原 Web 修复 Task 3 验收：共用原组件、认证与会话数据层通过

日期：2026-10-30（Asia/Shanghai）。
父基线：`4bb993bb353f23f5eec06442d2f6ce1c6c2e3090`。
原生产：`2c6bc6ca74443e38748312e9c935744157306318`。
R3 生产：`4dc6ee5497963cd58d6fd957865702bf4002481f`。
最终证据 tip / 后续父基线：`55501036f31ff5880db87436bd35e3c5d24653d1`。
worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## 裁决与范围

Task 3 验收通过，T3-R1-01/02/03、T3-R2-01、T3-R3-01 均关闭。本轮聚焦复审未确认新增阻塞。下一项仅 [Task 4 登录/设置主题](../tasks/2026-10-30-original-web-task-04-login-theme.md)。

通过范围：控制模式复用原 WorkspaceLayout/Sidebar/ChatPage/MessageBubble/QuestionCards，一份前端构建；认证前不挂私人树、身份隔离/会话目录与历史/Run/SSE/停止/问题卡/安全设置桥接、local 原行为回归。

不代表全部原 Web 功能已恢复。未接通的工作台/技能/内容库/账号/画像等在 Task 5 逐组接线；登录与设置主题尚待 Task 4。真实供应商/用户实例仍未验证或切换，不能把假 API 浏览器结果称为用户真实聊天通过。

## R3 关闭依据

`web/frontend/src/hooks/useWorkspace.ts` 新增 TERMINAL_QUESTION_STATUSES，仅 answered/resolved/expired/not_found 为明确终态，unknown/checking 等未知值不清题卡、不加入 answered、不启用提交。

主审不修改原探针，实际新跑：

```text
QUESTION_STATUS null     → cards=1 pending=true
QUESTION_STATUS unknown  → cards=1 pending=true
QUESTION_STATUS checking → cards=1 pending=true
QUESTION_STATUS pending  → cards=1 pending=false

QUESTION_GATE={cards_before_reconnect:1,cards_after_reconnect:1,
 after_values:[0,3],question_status_calls:2,backend_pending:true,chat_posts:1}

SUBMIT_401={sidebar_visible:false,login_visible:true,pending_visible:false}
ANSWER_503={question_cards:1,backend_answered:false,backend_waiting:true}
THINKING_REPLAY={before_count:1,after_count:1,after_values:[0,2]}
ISOLATED_ADMISSION={results:{control_admission_ordering:true},failure_count:0}
FIXTURE_CLEANUP=ok
```

独立原 hook 门控矩阵 8 例通过：pending 保卡启用；四个明确终态清卡；unknown/checking/空字符串保留待确认。每例均保留初始 pending payload、重连正确游标，旧连接迟到相反状态不改变新状态。

## 主审新运行结果

- 全部 tests/frontend 的 Node 文件：131 passed、0 failed/0 skipped。
- `npm --prefix web/frontend run build`：tsc + Vite 成功，index-Bf2NQlq4.js；既有 chunk 提示不作为本项阻塞。
- `npm --prefix web/frontend run lint`：oxlint 退出 0。
- `browser_original_web_task03.py` 真实构建 + 隔离假 API/headless Chromium：33/33，FAILURES=[]，退出 0；包含全部 R1/R2/R3 场景与 local 回归。
- 原三份主审浏览器探针实际复跑，输出如上；没有改探针来变绿。
- `git diff 8b914403 55501036 --check` 退出 0，worktree 干净。

作者红态旧构建 31 个既有场景通过、unknown/checking 两条失败，以及连续三次 33/33，保留为作者证据；本次关闭另有主审新跑整套与独立 gate 矩阵支持。

## 资源与下一阶段

主审截图输出重定向到隔离 worktree 的临时 .pytest_cache 目录并清理，没有覆盖作者证据。假 API、独立 browser context/进程已关闭；未执行 Docker、未触及用户 Key/Cookie/卷或供应商。root 仍在原 1a881109，本轮仅更新主审文档，未 cherry-pick/发布。

Task 4 须收口原 LoginPage/AuthBoundary/设置桥接的主题和品牌资源；其中当前 dist/static 未含 Sidebar 所引用 Logo，测试 fixture 的占位 PNG 不能作为品牌资源发布证据。该资源打包属于下一阶段既定品牌范围，不重开 Task 3 会话与认证方案。Task 5 再接原业务 API，阶段验收不等于整体完成。
