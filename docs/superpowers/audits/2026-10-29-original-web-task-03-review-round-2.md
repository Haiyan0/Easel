# 原 Web 修复 Task 3 R1 复审：三项原反例关闭，待确认题卡重连需单项返工

日期：2026-10-29（Asia/Shanghai）。
父基线：`8f1c11253b828eacc8d53f60107356248956bb47`。
R1 生产：`a1987ca5b337f73c7af064571f324b8fd8bba136`。
审查 tip：`1b461bac6199ed7d2766489c847ebeaf4f1b1d1e`。
worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## 裁决

原 T3-R1-01/02/03 的既有确定反例均关闭，主审完整浏览器 28/28 通过。Task 3 仍需完成本次确认的 T3-R2-01 一项 P2：question 已推进游标，但异步状态尚未落地时重连会丢卡。

只修这处提交时序，不重开原布局、认证或其它已关闭路径，不进入 Task 4/5。用户实例保持原样，主审未合入 root 或发布。

## 原三项关闭证据

主审保持原 probe_browser.py 不变，以本次真实构建 + fresh 假 API/独立 Chromium 运行：

```text
SUBMIT_401={sidebar_visible:false,login_visible:true,pending_visible:false}
ANSWER_503={question_cards:1,backend_answered:false,backend_waiting:true}
THINKING_REPLAY={before_count:1,after_count:1,after_values:[0,2]}
ISOLATED_ADMISSION={results:{control_admission_ordering:true},failure_count:0}
FIXTURE_CLEANUP=ok
```

独立认证复核用真实 dist/fresh fixture 验证提交 401、questionStatus 401、初始历史 401、迟到 A 401 保持 B、503 同卡同选择重试五项全部通过。

answer 的 typed 结果已透传，503/断网/403 保卡、409 明确过期单独处理；当前身份 401 的处理已在相关 guard 之前。thinking/activity/error 等游标同步 runRef、内存状态和持久游标，旧连接归属检查仍保留。刷新仍从 0 重建未完成内容。

## T3-R2-01 / P2：题卡状态未回时重连，已消费的问题无法恢复

位置：`web/frontend/src/hooks/useWorkspace.ts:610`–`:627`。

onQuestion 在 `:611` 先 `commitFrame(frame, () => ({}))`，持久化游标但没有保存 question payload。随后异步 questionStatus；到 `:623`–`:627` 的 then 中才写题卡，且此前 runRef 的 question 仍为空。

如果状态请求慢于流断开，用户可以点击重连。新连接继承空 question、after 已经跳过问题事件；旧状态请求被 abort 或因 owns() 拒绝，新连接也没有该问题需要查询的记录。后台继续 awaiting_permission，页面无题卡可回答。

### 主审真实浏览器反例

使用现有真实构建和独立假 API：登录 A → 第二会话 → 发送“断线问题”；对 questionStatus 使用 asyncio gate 暂停回应；首次流为 state1/awaiting2/question3 后断开。在题卡未显示时点击重连，确认新流已 after=3，再放行旧 pending 回应：

```text
QUESTION_GATE={
  cards_before_reconnect:0,
  cards_after_reconnect:0,
  after_values:[0,3],
  question_status_calls:1,
  backend_pending:true,
  chat_posts:1
}
FIXTURE_CLEANUP=ok
```

主审脚本：`docs/evidence/original-web-task-03-review/probe_question_gate.py`，Agent Python 执行 exit 0。只用受控 gate，没有用固定长 sleep 推进交错；不改源码、不用 Docker 或真实身份。独立复核以原 hook 转译代码/受控 gate 得到等价结果。

现有 scenario_r1_question_cursor 先等待“允许本次”可见，再重连，只覆盖状态已落地后的情况，故 28 项绿色不能覆盖本窗口。

### 最小修复要求

- question 事件被算作已消费时，同步保留其待确认 payload 与恢复所需状态到本轮 runRef/React 状态，或以等价方式确保该问题可恢复；不能只先确认游标再等待异步状态。
- 重连继承待确认问题并按新连接归属继续查询/核对状态；明确已回答/已过期才关闭。旧连接迟到回应不得覆盖新题卡，重复重连不得重复批准或重发原 prompt。
- 不将修复改成全流从 0 重放，不能重新引入 thinking/token 重复；保留 401 闭环、503 保卡和已有 409 语义。
- 补“状态尚未返回 → 断流 → 重连 → 旧 pending 回应迟到”的真实浏览器 gate 回归，并覆盖已回答/过期及旧回应不覆盖新连接。

## 本轮新验证结果

- 全部 tests/frontend 下 Node 文件：131 passed、0 skipped。
- tsc + Vite build 成功；已有 chunk 大小提示不作为本项阻塞。
- oxlint 退出 0。
- 主审完整浏览器矩阵：28/28，FAILURES=[]，退出 0。上轮 admission-ordering 超时本轮未出现。
- 原三条主审 probe 与新增 question gate probe 均已运行；后者证明上述单项。
- `git diff 8f1c112 1b461ba --check` 退出 0；worktree 干净。

浏览器截图输出使用 worktree 内临时 .pytest_cache 目录，结束自动清理，不覆盖作者证据。额外 gate 探针不写截图。假 API、浏览器 context/进程和临时目录均已清理，用户容器/凭证未接触。

## 后续

仅执行 [Task 3 R2](../tasks/2026-10-29-original-web-task-03-rework-r2.md)，基于 tip 继续原隔离 worktree。既有三个问题的关闭结果保留，不重做无变化系统，不进入 Task 4/5，不自动上线。
