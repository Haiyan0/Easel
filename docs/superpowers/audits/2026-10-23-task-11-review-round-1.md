# Task 11 首轮审计与 R1 定点返工

日期：2026-10-23。审计基线：30a4e8688b2963e4423cef9f7e9baa3a6ce32eb4（Task 11 主提交 279d450，父任务验收基线 e4ffc83）。

结论：暂不通过。仅修复以下七项，不进入 Task 12；不扩大到 Task 8 部署、非聊天业务或新设置界面。

依据：[Task 11 交接单](../tasks/2026-10-23-task-11-identity-ui.md)。主审及独立审查者检查实际提交、执行组件行为探针、真实 HTTPS/Chromium 与后端 HTTP 探针，未修改产品实现、未提交代码，未访问开发者账号或调用真实供应商。

## 独立验证与边界

- 全部前端 Node 测试：97 passed。
- 后端 tests/multiuser：904 passed、3 skipped、2 warnings，80.69 秒。
- 交接单六文件兼容回归：210 passed、6 skipped，17.27 秒。
- 前端 npm run build：退出 0；有产物大于 500 kB 的构建提示，不作为本轮阻塞项。
- 原 tests/frontend/browser_task11.py：退出 0，Chromium 151.0.7922.34；4 次邀请兑换、2 次 prompt POST、2 次 SSE。退出时有 Windows Proactor 连接重置回调警告；脚本自身断言通过。
- 补充真实 HTTPS/Chromium：普通业务 401 后登录框数为 0、退出按钮数为 1，旧私人消息仍可见。
- 补充真实 Store + TestClient：归档前 Run 查询 200、归档 PATCH 200、归档后 Run 查询 409 conversation_archived。
- 组件行为探针实际转译并执行产品回调，控制请求完成顺序；不是仅搜索源码或执行复制的算法。它们用轻量 hook 宿主隔离闭包，尚不能替代返工后的真实 React/浏览器竞态回归。
- git diff e4ffc83 HEAD --check：退出 1，仅 ControlApp.tsx 第 1084 行新增文件尾空行。请顺带整理；不另设阻塞编号。

当前测试全绿仅覆盖已有场景，下列反例仍成立。

## T11-R1-01（P1）：业务 401 未卸载私人工作区

定位：./web/frontend/src/lib/auth.ts 的 identityFetch（约 256–260 行）；./web/frontend/src/components/ControlApp.tsx 的 call（约 317–330 行）。

identityFetch 先 invalidate 当前 epoch，再抛出 401；call 捕获后却要求同一 epoch 仍 current 才调用 onExpired。该条件因此不成立。私人内容继续显示，同时后续 apply 全被失效 epoch 拦截，页面冻结。

行为探针输出 expired=0、epochStillValid=false；真实 HTTPS 页面在已有用户/助手历史时拦截新建会话返回 401，得到 AUDIT_401_LOGIN_VISIBLE=0、AUDIT_401_PRIVATE_HISTORY_VISIBLE=True、AUDIT_401_LOGOUT_VISIBLE=1。

返工：统一“判断是否属于当前身份、使其失效、通知根壳退出”的责任和顺序。当前身份业务 401 必须卸载私有树并清理该身份资源；旧 epoch 的迟到 401 仍不得影响新身份。不能仅移除所有代次检查。

验收：真实浏览器已有消息后，让目录/新建/历史等普通 JSON 接口返回 401；登录页出现、私人内容消失，清理生效。另覆盖 A 旧 401 晚于 B 登录，B 不被退出。

## T11-R1-02（P1）：迟到的身份查询可以覆盖或退出新登录

定位：./web/frontend/src/components/ControlApp.tsx 的 checkIdentity、adopt、expire 与登录事件处理（约 144–211 行）。

启动查询与跨标签事件触发的查询可以并发，checkIdentity 对任何完成结果都直接 adopt/expire，没有查询代次校验。实际执行两个查询，后发 B 200 先返回后，先发 A 200 迟到把身份变回 A；将迟到结果改为 401，则已登录 B 被置 anonymous。

这种情况下浏览器共享 Cookie 已属于 B，界面却使用 A 的 user/workspace 命名空间，破坏身份隔离前提。

返工：身份确认自身也要有代次/所有权；新的登录、退出、广播重新确认和卸载使旧查询无权提交成功或失败。Abort 仅用于回收资源，结果提交仍须检查有效代次。不得让旧查询结果重新激活一个 epoch。

验收：真实 React/浏览器受控交错，分别延迟旧 auth/me 的 200、401 和失败响应；新 B 已确认后旧结果全部无效。同一用户重新登录也必须启用新代次。

## T11-R1-03（P2）：异步结果缺少会话/身份提交边界

定位：./web/frontend/src/components/ControlApp.tsx 的 refreshMessages（约 421–436 行）、活动会话 effect（约 558–588 行）、submitTurn（约 663–685 行）、connectRun。

已复现三种交错：

1. A 会话历史延迟，切到 B 并加载 B 后，A 返回仍 setMessages，当前 B 页面显示 A 历史。effect 的 cancelled 检查位于 refreshMessages 已经提交之后。
2. A 的 POST admission 延迟，切到 B 并连接 B Run 后，A 返回仍 connectRun(A)，中止 B stream、覆盖全局 run。activeId 仍 B，停止入口消失，发送状态也不一致。
3. 账号 A POST 后退出并切 B，A 的 202 迟到仍重新写入已清除的 A 恢复缓存，并用当前共享 Cookie 发起 A Run 的 GET stream。后端继续保护归属，但前端清理与生命周期承诺已失效。

返工：区分身份 epoch、会话选择代次和 Run 连接所有权。提交显示前核对当前选择；失效身份不得写恢复缓存或启动新网络。合法后台 A Run 继续执行，切换会话不应 stop 或再次 POST；可为 A 保留本人作用域的恢复信息，但不得抢占 B 的连接与界面。终态刷新、回答、清理回调同样检查所属 Run，避免修复只覆盖首次加载。

验收：真实 React/浏览器分别控制历史和 admission 的晚到；A/B 两会话同时活跃，B 的正文/题卡/停止入口不被 A 改写；退出切账号后旧 admission 不重建缓存、不发新 SSE。保留服务端 Run 幂等与容量限制。

## T11-R1-04（P2）：首次 SSE 连接失败后无法使用所提示的重连

定位：./web/frontend/src/components/ControlApp.tsx 的 connectRun（约 469–508 行）与重连按钮（约 896–902 行）。

streamRef 在 fetch 前赋值；网络失败或非成功 HTTP 分支在 reader 的 finally 之前 return，引用未清空。重连按钮要求该引用为空，因此界面提示“可点击重连”，实际没有按钮。探针确认 streamRefStillNonNull=true、reconnectCondition=false。

返工：连接获取和读取共用覆盖所有退出路径的清理，按 controller 所有权清理，旧失败不得清掉新连接。当前身份、可恢复失败后给出可用的 GET 回放入口，不自动重复 POST。

验收：首次 GET 网络拒绝、503、缺响应体、主动取消；可恢复失败后可重连，后续成功连接正常读取；旧连接失败不得清掉新连接，401/404 仍按各自语义处理。

## T11-R1-05（P2）：携带游标重连却丢弃游标之前的运行状态

定位：./web/frontend/src/components/ControlApp.tsx 的 connectRun initial（约 472–477 行）和重连调用（约 900–901 行）。

手动重连使用 current.cursor 请求 after，却重置 content/thinking/question/answered/state。已收到待回答问题后游标越过该事件，重连不会再重放问题；Run 正等待回答而没有后续事件，题卡和正文丢失。探针在 awaiting_permission、cursor=5、已有题卡/正文时重连，实际 GET after=5，question=null、content 为空、state=accepted。

同一原则也适用于刷新/新组件：只保存 eventId 而没有对应完整状态，不能从该游标恢复一个空状态机。

返工：让游标与已应用的客户端状态一致。同 Run 原地重连保留状态；若没有可信且完整的前置状态，按服务端支持的回放起点重建，正确去重，不丢题卡/opaque value、不重复回答或 prompt。不要靠把游标置末尾跳过历史来规避。

验收：正文已收到后断线、待回答问题时断线、部分多问题回答、刷新与换设备恢复；题卡和已答状态一致、输出不重复、每次恢复均只有 GET，无新 prompt。

## T11-R1-06（P2）：目录分页未消费，旧历史被静默截断

定位：./web/frontend/src/components/ControlApp.tsx 的 reloadSessions 与初始化目录请求（约 334–383 行）；refreshMessages/loadRuns（约 421–450 行）。

目录只请求 limit=100，忽略服务端 nextOffset，也没有更多入口。实际探针提供 100 项和 nextOffset=100，仅请求一次；想选择第 101 项 s100 时回落 s0，后续会话无法从界面访问。

此外消息最多读取 5×500、Run 最多 10×200 后即当作完整数据提交，没有继续加载或截断说明。长历史会丢尾部显示；Run 按正序分页，未读尾页可能漏掉当前活跃任务。不能以固定页数替代完整性/可达性契约。

返工：提供有界、可继续的分页/加载方式或明确的最新窗口，并保证所有会话和消息可达、最新活跃 Run 可恢复；不要求一次拉完所有历史，也不得使用无限无界读取。未加载完不能伪装完整或据此判断没有活跃任务。

验收：至少 101 个会话、超过 2500 条消息，以及活跃 Run 位于旧 2000 项之外的目录；后续数据可到达、选择稳定、不会发送重复 prompt、不会漏掉活跃任务。保留服务端每页上限。

## T11-R1-07（P2）：归档历史被新 Run 元数据接口挡住

定位：./easel/control/store.py 的 list_run_page（约 1935–1945 行）与 _require_conversation（约 2182–2190 行）；./web/frontend/src/components/ControlApp.tsx 活动会话 effect。

list_run_page 复用禁止归档会话启动执行的校验，导致本人只读 Run 查询也返回 409。真实 HTTP 验证：归档前 GET runs=200；PATCH archived=true=200；归档后 GET runs=409 conversation_archived。前端先 await loadRuns 再读 messages，因此点击归档会话直接“历史读取失败”，并未读取消息。

返工：只读历史的归属/删除校验与新 Run 准入校验区分。归档会话允许本人读取 Run 元数据和消息，继续禁止新 Run；已删除/跨 workspace 仍不可读。不通过放开写准入解决。

验收：有已完成历史的会话归档后，真实 HTTP runs/messages 与浏览器历史仍可读；新任务仍拒绝，外空间和删除会话仍 404，local 行为不变。

## 执行与交付要求

1. 从当前 SHA 开始，只改必要产品与回归测试路径；主审已有 plan/spec、审计与任务单保持不暂存、不提交。
2. 先写反例并回放基线，再修实现。身份/会话竞态须覆盖实际组件和真实浏览器；仅 helper 断言不足以关闭这些项。
3. 重新运行全部 Node 前端测试、tests/multiuser、原交接单六文件兼容回归、生产构建；真实 HTTPS 脚本补入上述竞态与恢复用例。归档须有真实后端测试，分页须有边界数据。
4. 报告父/新 SHA、显式文件范围、逐项红绿、完整命令结果及未执行门槛；停止等待复审，不进入 Task 12。

Task 8 的最终镜像、Chromium 沙箱、目标宿主防火墙/IPv6、XFS、真实供应商/公网出口等门槛保持原状态。本轮前端验收不等于多人生产发布验收。
