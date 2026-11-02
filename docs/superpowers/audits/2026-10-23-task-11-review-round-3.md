# Task 11 R2 复审与 R3 定点返工

日期：2026-10-23。返工父基线：5e4908b73d12c74eb906b5c1477dcb8e8a194c59。复审提交：acb3793b29e93445f3d44617525be2acf90a6acd、249f79d72a276adcc5c3b6bbbf1cfc184321125c；当前 SHA 为后者。

结论：暂不通过，仅剩以下两项 P2。它们分别是 R2-01 和 R2-04 的另一种完成顺序，未增加业务范围。不进入 Task 12。

依据：[R2 返工单](2026-10-23-task-11-review-round-2.md)、[Task 11 交接单](../tasks/2026-10-23-task-11-identity-ui.md)。主审和独立审查者未改产品、未提交代码；独立执行现有回归，以及实际产品回调、真实 HTTPS/Chromium 受控交错探针。未访问开发者账号、真实供应商或生产数据。

## 独立验证与已关闭项

- 全部前端 Node：98 passed。
- tests/multiuser：908 passed、3 skipped、2 warnings，80.14 秒。
- 六文件兼容回归：210 passed、6 skipped，17.35 秒。
- npm run build：退出 0，仍有大于 500 kB 的非阻塞产物提示。
- browser_task11_r1.py：退出 0，15 项结果全 true；包括 2502 条历史无重叠、合法相同正文保留，以及 401 只清本人缓存。
- browser_task11_r2.py：退出 0，5 项结果全 true；包括同 Run 旧 controller、连续三次 503、第二页选择保持及已删除回落。退出打印 CONTROL_STOP_WARNING，见测试装配说明。
- 原 browser_task11.py：退出 0，4 次登录兑换、2 次 prompt、2 次 SSE；退出有 Windows Proactor 连接重置回调警告，脚本断言通过。
- git diff 5e4908b HEAD --check 与工作区 git diff --check：退出 0。

| R2 项 | 复审状态 |
|---|---|
| R2-01 旧终态/旧连接 | 新 admission 已到后的旧终态、同 Run 旧 controller 已修；admission 未交付时仍见 R3-01 |
| R2-02 连续 SSE 失败 | 关闭；streamActive 可渲染状态已接入，连续三次失败后均可重试并恢复 |
| R2-03 尾页重复 | 关闭；读取窗口精确、不按正文去重，Node 边界和真实浏览器通过 |
| R2-04 第二页目录刷新 | 静止选择保持与删除回落已修；刷新中用户再次选择仍见 R3-02 |
| R2-05 失效清缓存 | 关闭；expire 清本人命名空间，其他账号和 local 键保留，身份查询代次未退化 |

## T11-R3-01（P2）：新请求等待 202 时，旧终态仍可抹掉新输入

定位：./web/frontend/src/components/ControlApp.tsx 的 submitTurn（约 875–900 行）、finishRun（约 567–598 行）与 connectRun（约 604–629 行）。

当前 connectionSeq 只在连接建立/中止时变化。用户发送下一轮时，先追加新用户消息并 POST，但直到 admission 到达、调用 connectRun 才使旧终态的 owns 失效。该窗口内旧 finishRun 的历史结果仍可以 setMessages(latest.messages)。

已独立复现以下顺序：

1. r1 done，最终历史已从服务端取到，延迟交付页面。
2. 用户发送 new-prompt；服务端已实际返回 r2 的 202，但延迟交付页面。
3. 页面仍等待这份 202 时，先放行 r1 的旧历史。
4. 再放行 r2 的 202 并进入新 SSE。

真实 HTTPS/Chromium（复用交付脚本真实 control/worker/POST；SSE 使用交付脚本相同的受控事件，未调用真实供应商）结果：

```text
AUDIT_PENDING_ADMISSION_STATUS 202
AUDIT_NEW_PROMPT_BEFORE_OLD_HISTORY True
AUDIT_NEW_PROMPT_AFTER_OLD_HISTORY False
AUDIT_NEW_PROMPT_AFTER_202 False
```

独立产品回调探针同样得到 [OLD-PROMPT, NEW-PROMPT] 被替换为 [OLD-PROMPT, OLD-ANSWER]。交付的 R2-01a 只验证旧响应晚于新 admission，遗漏了此顺序。

返工：从开始提交新一轮输入起，就保护该输入所属的本地发送代次/消息窗口，旧收尾不得覆盖；不能等新 SSE 连接建立后才赋予保护。所有异步历史提交入口共用同等规则，包括终态、404 后刷新等，避免只修一个 setMessages。仍以服务端快照为最终历史，不永久丢掉 r1 的助手输出；若新请求失败或结果未知，保留可审查的待确认输入及同一 turnId 的重试语义。不把前端清理变成服务端 stop、重复 POST 或伪造成功。

验收：在真实页面控制服务端响应的交付顺序，分别覆盖旧历史早于/晚于新 202，新 202 失败/结果未知，以及用户切会话。新输入不能被抹去，随后快照能收敛到正确消息顺序；旧收尾不能改新题卡/答案/恢复记录。保持已通过的同 Run 旧 controller 拒绝和每 Run 一次执行。

## T11-R3-02（P2）：目录刷新补页后会撤销用户的新选择

定位：./web/frontend/src/components/ControlApp.tsx 的 loadSessionsPage（约 390–450 行）：第 401 行捕获 wanted，第 408/424 行等待补页或存在性查询，第 450 行仍无条件提交先前 wanted。

sessionsSeq 只区分目录请求，不区分用户在该请求期间的选择。为了保持第二页选择而补查后续页时，用户可以另选会话；旧请求完成仍把页面切回旧 wanted。

主审真实 HTTPS/Chromium 探针使用真实 101 会话目录与跨对象 BroadcastChannel：

1. 选第二页会话 s061（本次随机排序下的旧选择）。
2. 触发目录广播刷新，第一页缺 s061，捕获并暂停第二页响应。
3. 用户主动选 big，header 已显示 big。
4. 放行先前第二页响应，页面被切回 s061。

```text
AUDIT_SELECTION_BEFORE_OLD_PAGE big
AUDIT_SELECTION_AFTER_OLD_PAGE s061
AUDIT_OLD_CAPTURED_SELECTION s061
```

独立产品回调探针也确认 s0 被回切为旧 s100。这会触发活动会话 effect，取消用户刚选会话的展示连接、重建旧历史并改写活动键。交付浏览器只覆盖用户保持不动的目录刷新。

返工：目录请求代次与用户选择代次分别校验。目录结果可以更新目录，但若等待期间用户已作新选择，旧刷新不得改 activeId、活动键或连接；不能把用户选择简单当作目录加载失败。保留真正删除/失权时的明确回落，以及显式新建等操作的合理选中行为。补页、sessionStatus 慢响应、跨标签广播及重命名后的异步刷新都遵守同一规则。

验收：至少 101 个会话，刷新第二页挂起时用户切到第一页的活跃会话并收到题卡；旧页及存在性响应回来后，新 header、SSE、题卡、选项和活动键保持。再覆盖当前会话确实删除、目录请求彼此交错及用户再次切回原会话，不能以一次 ID 比较代替选择代次。

## 测试装配说明（不新增产品阻塞编号）

R2 浏览器脚本已如实报告 control 关停超时；主审复跑也看到 CONTROL_STOP_WARNING。因此本轮只确认断言通过和进程退出，不把它写成“所有测试线程均已正常关闭”的证据。后续补测宜在 finally 中关闭 capture 客户端、无条件停止新建 hang worker，并协调其在途请求/host 的关闭顺序；不要吞掉失败后笼统宣称无残留。此项无需修改产品安全边界，也不能用它替代两个真实产品问题。

## 执行与交付

1. 从 249f79d 开始，仅修 R3-01/02 及必要回归；已关闭三项不重写，不进入 Task 12。
2. 测试先控制旧响应和新响应的交付顺序；不要只延迟发送请求，不依赖固定 sleep 猜测前提，优先事件门禁并断言已发生的请求/状态。
3. 重新跑全部 Node、tests/multiuser、六文件兼容回归、生产构建及三份浏览器脚本，交付逐项红绿、父/新 SHA、显式文件范围和未执行门槛。
4. 主审 plan/spec、审计和任务单仍不暂存、不提交；停止等待复审。

Task 8 未关闭的真实隔离、最终镜像、真实 Harness/供应商与公网出口等发布门槛不变。
