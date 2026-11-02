# Task 11 R1 复审与 R2 定点返工

日期：2026-10-23。返工父基线：30a4e8688b2963e4423cef9f7e9baa3a6ce32eb4。复审提交：cab8443、47fb96c、5e4908b，最终 SHA：5e4908b73d12c74eb906b5c1477dcb8e8a194c59。

结论：暂不通过。两项 P1 已修复；下面五项 P2 是原七项要求的剩余边界，不扩大功能。仅执行本单，不进入 Task 12。

依据：[R1 返工单](2026-10-23-task-11-review-round-1.md)、[Task 11 交接单](../tasks/2026-10-23-task-11-identity-ui.md)。主审及两名独立审查者仅做代码审查、临时探针和验证，未修改产品、未提交代码、未运行真实供应商或开发者 Harness。

## 独立验证

- 全部前端 Node 测试：97 passed。
- tests/multiuser：908 passed、3 skipped、2 warnings，81.31 秒。
- 原交接单六文件兼容回归：210 passed、6 skipped，20.04 秒。
- npm run build：退出 0；仍有大于 500 kB 的非阻塞构建提示。
- tests/frontend/browser_task11_r1.py：退出 0，Chromium 151.0.7922.34，报告中的 13 项结果全部 true。
- 原 tests/frontend/browser_task11.py：退出 0，4 次登录兑换、2 次 prompt、2 次 SSE；退出有 Windows Proactor 连接重置回调警告，脚本断言通过。
- git diff 30a4e86 HEAD --check：退出 0，R1 文件尾空行已整理。
- 独立补充：实际转译组件回调、React 19.2.7 生命周期及真实 HTTPS/Chromium 受控竞态。各证据范围在对应问题中说明；不将合成 SSE 称为真实供应商执行。

## R1 关闭情况

| 原项 | 复审结果 |
|---|---|
| R1-01 业务 401 | 私人树卸载修复，真实浏览器通过；身份缓存清理仍见 R2-05 |
| R1-02 身份查询迟到 | 关闭；旧 200/401/503 无权覆盖或退出 B，独立 React 与交付浏览器路径通过 |
| R1-03 会话/身份边界 | 原历史晚到和 admission 切换路径已修；终态/同 Run 旧连接仍见 R2-01 |
| R1-04 SSE 获取失败清理 | 单次失败可重连，连续同种失败仍见 R2-02 |
| R1-05 游标与状态 | 原地保留完整状态、新组件从 0 重建的正常路径可关闭；旧连接抢写并入 R2-01 |
| R1-06 分页 | 更多入口、最新窗口、active=true 已接入；尾页重叠与目录刷新选择见 R2-03/04 |
| R1-07 归档只读 | 关闭；只读校验已拆开，后端和真实浏览器通过，新 Run 仍禁止归档准入 |

## T11-R2-01（P2）：旧终态与旧连接仍能提交到当前 Run

定位：./web/frontend/src/components/ControlApp.tsx 的 refreshLatestMessages（约 462–475 行）、finishRun（约 492–505 行）、connectRun 的 owns（约 526–528 行）、handleFrame 的异步问题分支（约 1200–1230 行）。

已确认两类未闭合的所有权：

1. **旧终态刷新与同会话新 Run。** state=done 先被提交，发送按钮开放；finishRun 仍在等旧历史。此时新 prompt/admission 不更新该历史请求所属代次，旧结果随后可以替换 messages。finishRun 等待后也只查会话 ID，并无条件清空 answers。主审真实 HTTPS/Chromium 探针捕获旧历史响应、延迟交付，再提交新 prompt 并以受控 SSE 保持新 Run 展示：NEW-PROMPT 在旧响应前可见、之后消失，而新 Run 的 INFLIGHT 仍可见。独立真实 React 探针另确认：旧 r1 收尾挂起 → 切 B → 回 A 恢复 r2/q2 并选答案 → 旧 r1 收尾返回，将 q2 的选中状态清空；即使 messageSeq 已拒绝旧正文，finishRun 后续仍污染回答。
2. **旧 controller 与重连后的同一个 Run。** owns 仅检查身份、sessionId、runId，不检查连接或选择代次。旧 q1 的 status 查询已得到 pending 但延迟返回；切走再回到同一 Run，新连接显示 q2 并选择答案；旧响应迟到仍通过 owns，将 q2 换回 q1、清空答案。实际调用产品 connectRun/handleFrame 的组件探针已复现。

```text
旧终态：messages=[old-prompt,old-answer], answers={}, run=new-run
旧问题返回前：question=q2, answers={permission:chosen-new}
旧问题返回后：question=q1, answers={}
```

返工：完整区分身份、会话选择、Run 和连接代次。终态启动的历史刷新须带预期 Run/选择所有权，结果及后续清理全部遵守；新 Run 开始后旧收尾不能写其 messages/answers，也不能清理新恢复记录。已取消 controller 的异步回调不得因重新选回同 session/run 而恢复权力。检查每个 await 之后的提交点；不能只依靠 runId 相同、取消 fetch 或扩大全局 messageSeq 来替代实际所有权。

验收：真实组件/浏览器覆盖旧终态响应晚于同会话新 admission、新问题选择，以及 A→B→A 恢复新 Run；再覆盖旧连接 q1/status 晚于同 Run 新连接 q2。新正文、题卡、回答选择和恢复缓存都不被旧回调改写。合法 Run 继续后台执行，不额外 POST、不自动 stop。

## T11-R2-02（P2）：连续同种 SSE 失败再次隐藏重连按钮

定位：./web/frontend/src/components/ControlApp.tsx 的 connectRun finally（约 612–618 行）及重连按钮条件（约 1055–1064 行）。

finally 只清空 ref，不产生渲染。第一次 503 更新 notice，因此按钮出现；点击重连后 setRun 新对象先触发渲染，按钮隐藏。第二次延迟 503 的 notice 文案与上次相同，React 不必再渲染，finally 清 ref 后界面仍没有按钮。

主审在真实 HTTPS/Chromium 上把两次 stream GET 均设为延迟 503，复用交付脚本 S4 的真实页面/任务入口：

```text
AUDIT_REPEATED_SSE_CALLS 2
AUDIT_RECONNECT_AFTER_SECOND_503 0
AUDIT_RECONNECT_NOTICE True
```

返工：让当前连接是否可重试由可渲染状态表达，所有失败/结束/取消路径按 controller 所有权更新。不能依赖恰好不同的提示文案或其它状态引起渲染；旧连接不得清掉新连接状态。继续坚持重连只 GET。

验收：连续至少三次相同网络失败或 503，每次都能再次点击重连，随后恢复成功；第二次响应延迟到连接开始后的渲染完成。保留 401/404/取消及旧连接失败不影响新连接的回归。

## T11-R2-03（P2）：非整页消息在最后一页被重复拼接

定位：./web/frontend/src/components/ControlApp.tsx 的 fetchMessagesPage 和 loadOlderMessages（约 441–447、674–687 行）。

每次固定 limit=500，但最后一次 messageStart 可能小于 500。offset 变成 0 后仍取 500 条并完整 prepend，覆盖已加载区间。只检查最早消息存在，不能证明分页没有重复。

交付浏览器 S1 实际种入 2500 条，再执行一轮产生 2 条，因此该数据本身就能触发缺陷。主审沿用该真实 worker/control/HTTPS 页面，在 S1 加载完后统计：

```text
AUDIT_HISTORY_TOTAL 3000
AUDIT_HISTORY_UNIQUE 2502
AUDIT_M2_OCCURRENCES 2
```

另一实际组件探针对纯 2501 条历史得到 3000 条展示、499 条重复。

返工：每个前向补页精确覆盖尚未加载区间，不读入或拼接重叠部分；保持每页有界、顺序稳定，消息内容可以相同，不能用正文去重。现有终态刷新与加载更早的代次隔离要保留。

验收：501、999、1001、2501、2502 以及 500 的整倍数；完整加载后记录数量、顺序和每条出现次数与源一致，无遗漏/重复。另覆盖真实重复正文，不能误删合法消息。

## T11-R2-04（P2）：目录刷新会把第二页当前会话切回第一页

定位：./web/frontend/src/components/ControlApp.tsx 的 loadSessionsPage（约 358–380 行）、reloadSessions（约 391–392 行）和目录广播处理（约 414–417 行）。

加载更多后能选择 s100，但刷新只保存第一页、在第一页找不到 wanted 即回落到 s0。产品 reloadSessions 回调探针已复现 activeId=s100 → reloadSessions(s100) → activeId=s0。其他标签新建/重命名等目录广播可触发该路径，导致当前第二页会话无端切走、其展示连接被取消；刷新后的活动会话缓存也跟着改变。

返工：目录分页刷新时保留仍然存在的当前选择；不能以“第一页没有”推断“已删除或无权访问”。必要时使用已有本人会话读取接口验证归属/状态，并用有界分页更新列表。会话真正删除/失权时才按明确策略回落；不要无限拉完所有历史，也不要保留已删除条目的可写状态。

验收：至少 101 个会话，选第二页并连接活跃 Run；另一标签触发目录变化，当前会话、SSE、题卡、活动键保持。刷新、重命名和更多分页交错也稳定；当前会话真正删除后正常回落。

## T11-R2-05（P2）：业务 401 退出仍保留该身份的缓存

定位：./web/frontend/src/components/ControlApp.tsx 的 expire（约 160–167 行）、handleExpired，以及 clearIdentityStorage。

显式 logout 和广播处理有清理，但业务 401 经 expire 回到登录页时没有清理本身份存储。独立真实 React 与主审真实 HTTPS/Chromium 均确认：登录页出现后，本身份的 active_session 和 run 恢复记录仍在 localStorage/sessionStorage。原 R1-01 要求的私人树卸载已完成，资源清理尚未完成。

返工：失效退出统一清理失效前捕获的身份资源，包括该身份存储和工作区清理；旧身份迟到事件仍不能清理 B 的缓存或卸载 B。只清理本人命名空间，不触碰 local 模式旧键及其他账号内容。无需为了本项发送额外 logout 或更改 Cookie 语义。

验收：当前身份普通业务/SSE/auth 确认 401 后，私人树消失且本人作用域键清空；预置 B 及 local 键保持。同用户重新登录使用新代次；旧 A 401 晚于 B 时 B 不受影响。

## 执行与交付

1. 从 5e4908b 开始，仅修本单五项；保持已关闭的身份查询、admission、归档读取与游标恢复行为。
2. 先写能在当前基线上失败的实际组件/浏览器回归，再改产品。连续失败、非整页及旧回调要检查实际结果，不能仅断言按钮曾经存在、首尾消息曾出现。
3. 账号切换 admission 的浏览器回归宜在服务端已返回 202 后延迟交付响应，避免把“请求尚未到达就被 abort”误作迟到 202 的证据。
4. 完成后运行全部 Node、tests/multiuser、原六文件兼容回归、生产构建、原浏览器脚本和补强 R1/R2 脚本；报告逐项红绿、父/新 SHA、文件清单和未执行项。
5. 主审 plan/spec、审计与任务单保持不暂存、不提交；不实施新的业务或部署绕过，不进入 Task 12，等待复审。

Task 8 未关闭的真实隔离、最终镜像、真实 Harness/供应商及出口等发布门槛保持原状态。
