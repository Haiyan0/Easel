# 原 Web 修复 Task 3 首轮审计：原组件复用成立，三项交互需返工

日期：2026-10-29（Asia/Shanghai）。
父基线：`4bb993bb353f23f5eec06442d2f6ce1c6c2e3090`。
生产提交：`2c6bc6ca74443e38748312e9c935744157306318`。
证据 tip：`8f1c11253b828eacc8d53f60107356248956bb47`。
worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## 裁决

Task 3 暂不通过，仅修以下三项。原 Sidebar/ChatPage/MessageBubble/QuestionCards 与 WorkspaceLayout 的实际复用、ControlApp 删除、未接通能力守卫已核对，不重写该架构；Task 4 视觉和 Task 5 后端接线不在本轮返工范围。

本轮无后端修改。用户运行实例未切换，主审只在隔离源码、构建产物与本机随机端口假 API/独立 Chromium context 验证，不接用户 Cookie、Key、Docker 或供应商。

## T3-R1-01 / P1：当前身份 401 已失效 epoch，但私人树未卸载

位置：`web/frontend/src/hooks/useWorkspace.ts:807`–`:813` 的 submit catch；同文件 `:695`–`:698` 的会话初始化 catch、`:590`–`:607` 的 questionStatus 旁路。关联 `web/frontend/src/lib/auth.ts:293` 的当前身份失效逻辑。

identityFetchRaw 正确地先使当前 epoch 失效并给错误标记 identityInvalidated。submit catch 却先检查 !isCurrent 并返回，后面的 onExpired 不可达；会话初始化先检查 isSelected 也有相同顺序。questionStatus 的 catch 完全忽略异常，先查 owns，因 epoch 已失效而直接返回。

结果是 AuthBoundary 仍处于 ready，私人 UI 和发送中状态保留。后续 401 因 epoch 已失效而不再带当前身份失效标记，用户无法正常返回登录。这不仅是错误文案，破坏了本项核心身份生命周期。

主审真实构建 + 独立浏览器 fixture：登录 A、向空会话提交，发送接口返回 401：

```text
SUBMIT_401={sidebar_visible:true,login_visible:false,pending_visible:true}
```

独立复核另验证权限问题状态查询返回 401，随后新建会话再次 401：仍有 sidebar、没有登录页。第二名复核也以原 hook 调度器确认 onExpired 调用为 0。

修复：当前请求拥有的身份失效必须通知认证边界并清理私人树，不能被自身 epoch 失效或选择/连接 guard 吞掉。盘点所有 client 旁路；保留迟到 A 的 401 不能使 B 或同账号新 epoch 失效，不可全局遇 401 无条件 logout。

## T3-R1-02 / P2：临时权限回答失败被当作成功，题卡消失

位置：`web/frontend/src/hooks/useWorkspace.ts:886`–`:895`；`components/WorkspaceApp.tsx:97`–`:101`；关联 QuestionCards 的成功后 answered 标记。

answer 对 503/网络错误仍清空 question 并 resolve；WorkspaceApp 随后无条件返回 ok:true，组件标记已回答。服务端实际仍 awaiting_permission，流也可能保持连接，此时没有重连按钮，题卡却不能再操作。

主审真实浏览器：问题 pending，选择允许，回答 API 返回 503：

```text
ANSWER_503={question_cards:0,backend_answered:false,backend_waiting:true}
```

独立原 hook 探针得到相同结果，streamActive=true、reconnect=false。需要刷新/切会话才可能找回，不符合临时失败可重试。

修复：暂时网络/503 等失败保持题卡及选择，并将失败返回/抛给 QuestionCards；仅成功才 answered/移除，已过期/已回答的 409 单独收敛。401 按上一项回登录，403 保持权限提示。用户主动重试只重发答案，不重发原 prompt，不重复执行外部动作。

## T3-R1-03 / P2：思考等已消费事件未推进游标，重连重复追加

位置：`web/frontend/src/hooks/useWorkspace.ts:573`–`:615`，关联 `:865`–`:869` 重连。client 已提供 onFrame 和返回 cursor，但 hook 只对 state/token 更新内存游标，持久游标更只在 state 更新。

thinking 追加了内容却未推进 cursor，activity/question/error 也没有统一推进。断线后重连携带旧 after，重复消费已显示 thinking，导致内容重复。

主审真实构建浏览器：流返回 running id1、thinking id2 后结束，再点“重连（只回放）”：

```text
THINKING_REPLAY={before_count:1,after_count:2,after_values:[0,1]}
```

独立原 hook 调度器亦复现 THOUGHT → THOUGHTTHOUGHT。后台 POST 不增加，但内容和恢复游标错误。

修复：在有效 owner/epoch/connection 下统一、单调推进所有已消费事件游标并持久化；同页面重连从最后消费位置继续。保留刷新/重新挂载时需要重建未完成流内容的语义，不能只把所有恢复改成跳过已消费帧而丢失思考/正文。旧连接迟到回调不得回写新连接游标，不重发 POST。

## 主审复现与结果

可复跑的独立浏览器反例：`docs/evidence/original-web-task-03-review/probe_browser.py`，Agent Python 执行 exit 0，包含三条反例及隔离接纳顺序复跑；输出 FIXTURE_CLEANUP=ok。它读取本 worktree 已构建 dist，启动随机端口假 API 和独立 headless Chromium，不改产品文件。

本轮新运行：

- 执行单指定七文件 Node：59 passed；全部 frontend Node 文件：131 passed、0 skipped。二者重叠不相加。
- `npm --prefix web/frontend run build`：tsc + Vite 成功；已有大 chunk 提示不作为本项阻塞。
- `npm --prefix web/frontend run lint`：退出 0。
- `git diff 4bb993b 8f1c112 --check`：退出 0，worktree 干净。
- 主审复跑作者完整浏览器 fixture：18/19 场景通过，admission-ordering 在 .send-btn disabled 处超时；并非 19/19 全通过。
- 使用全新 fixture 单独复跑 admission-ordering：通过、failure_count=0。本轮不据此声称第四项确定产品缺陷；返工后需完整复跑，若仍波动应定位状态串扰/前置条件，不能仅延长超时或删除场景。

浏览器输出重定向到本 worktree 的临时 .pytest_cache 子目录，结束自动清理；没有覆盖作者已提交截图。已查看作者原布局截图，确为原组件/主题；设置桥接及其它未接通菜单按阶段边界保留，不要求本轮做 Task 4/5。

## 后续

仅执行 [Task 3 R1](../tasks/2026-10-29-original-web-task-03-rework-r1.md)，继续当前隔离 worktree。已通过基础复用保留，保护现有用户实例和主审文档。提交后复审，不进入 Task 4/5、不自动上线。
