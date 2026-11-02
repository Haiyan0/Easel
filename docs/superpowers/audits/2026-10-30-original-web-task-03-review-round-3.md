# 原 Web 修复 Task 3 R2 复审：题卡重连已修复，未知状态判断需收窄

日期：2026-10-30（Asia/Shanghai）。
父基线：`1b461bac6199ed7d2766489c847ebeaf4f1b1d1e`。
R2 生产：`1dc7f0a6af2199f365b93b71b87d149eb57e0c04`。
证据 tip：`8b914403dfbf7dd3758a81ee992968f2dc796cb0`。
worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## 裁决

原 T3-R2-01 的题卡 payload/游标重连交错已修复，原 R1 三项继续关闭；仅 verifyQuestion 的未知状态终结判断仍不满足本轮交付和既有“明确已回答/已过期才关闭”要求。Task 3 暂不整体通过，仅一项 P2 做小范围修正，不重做恢复方案、不进入 Task 4/5。

## 已关闭结果

主审未修改原 probe_question_gate.py/probe_browser.py，使用新 dist 复跑：

```text
QUESTION_GATE={cards_before_reconnect:1,cards_after_reconnect:1,
  after_values:[0,3],question_status_calls:2,backend_pending:true,chat_posts:1}
SUBMIT_401={sidebar_visible:false,login_visible:true,pending_visible:false}
ANSWER_503={question_cards:1,backend_answered:false,backend_waiting:true}
THINKING_REPLAY={before_count:1,after_count:1,after_values:[0,2]}
ISOLATED_ADMISSION={results:{control_admission_ordering:true},failure_count:0}
FIXTURE_CLEANUP=ok
```

独立原 hook gate 矩阵确认新 pending 保卡并启用、resolved/expired 清卡；旧连接迟到 resolved 不清新 pending、旧 pending 不复活明确终态。同步提交 payload/questionPending 与 cursor 的改动成立，pending 提交按钮守卫也已接入原 QuestionCards。

## T3-R3-01 / P2：非空未知状态被错误当作明确终态

位置：`web/frontend/src/hooks/useWorkspace.ts:596`–`:597`，关联 `:610`–`:612`。

`explicit` 当前条件是任意非空字符串且不等于 pending。因此 unknown、checking 等没有定义为终态的值也会清卡并加入 answered，而不是按交付说明保持待确认。这会让服务端仍等待权限的问题从界面消失；不是自动批准，但不符合恢复与状态显示约定。

主审真实构建 + fresh 假 API/独立 Chromium 复现（后端 Run 始终 pending）：

```text
status=null      cards=1 pending=true
status=unknown   cards=0 pending=null
status=checking  cards=0 pending=null
status=pending   cards=1 pending=false
FIXTURE_CLEANUP=ok
```

主审脚本：`docs/evidence/original-web-task-03-review/probe_question_unknown.py`。Agent Python exit 0，只拦截本机假接口，无真实凭证或用户服务。独立复核在重连 gate 中得到同样 unknown/checking 清卡、空状态保卡的结果。

最小修复：只识别协议定义的明确终态（生产当前有 resolved/expired/not_found；answered 如作为既有兼容值需显式列出），未知字符串与缺字段一律保留待确认，不加入 answered、不启用提交。后续确认 pending 应能恢复可提交，旧连接回应的归属保护保留。

不用扩展状态服务、不新做 UI、不恢复全流回放。只收窄当前判断并补行为回归即可。

## 本轮主审新验证

- 全部 frontend Node：131 passed、0 skipped。
- tsc + Vite build 成功，生成 index-Dudtzw7s.js；既有 chunk 提示不作为本项阻塞。
- oxlint 退出 0。
- 完整浏览器矩阵：31/31，FAILURES=[]，退出 0。
- 原两份主审探针与新增未知状态探针均运行并清理。
- `git diff 1b461ba 8b914403 --check` 退出 0；worktree 干净。

完整矩阵截图写入 worktree 临时 .pytest_cache 子目录并自动清理，未覆盖实施证据。没有操作 Docker、用户容器/卷/登录态或供应商。root 仍为原基线，未 cherry-pick/发布。

## 后续

仅按 [Task 3 R3 单项执行单](../tasks/2026-10-30-original-web-task-03-rework-r3.md)修正未知状态分支。已通过的 31 场景、原布局/401/回答重试/游标和 gate 恢复保留，不进入 Task 4/5、不自动上线。
