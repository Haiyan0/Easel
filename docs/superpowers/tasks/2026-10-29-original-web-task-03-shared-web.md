# 原 Web 修复 Task 3：原组件共用认证与会话数据层

状态：Task 2 已验收，当前仅 Task 3。代码 Agent 实施，主会话审计；交付后停止，不进入 Task 4、不替换用户实例。
父基线：`4bb993bb353f23f5eec06442d2f6ce1c6c2e3090`。
继续 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## 必读与目标

读 AGENTS.md、[修复设计](../specs/2026-10-29-original-web-login-and-chat-repair.md)、[总计划 Task 3](../plans/2026-10-29-original-web-login-and-chat-repair.md)、[Task 2 验收](../audits/2026-10-29-original-web-task-02-accepted.md)及[运行实例保护清单](2026-10-29-original-web-running-baseline.json)。

登录后的控制模式必须使用原 Easel Sidebar、ChatPage、MessageBubble、QuestionCards 和原布局 class/theme，不能继续独立 ControlApp，也不能复制原 JSX 再造第三套应用。本项修认证/会话数据层与对话视图，登录美化/设置视觉整合留 Task 4，其它原页面后端接线留 Task 5。

## 实施前盘点

- 报告 HEAD、状态、拟修改文件。保护 root README、两份手册和全部主审文档，不带入提交。
- 列出 App/LocalApp/ControlApp 当前各自拥有的状态、副作用、缓存、fetch 和原组件 props；说明哪些迁到共享 hook/client，哪些仅 local 执行。
- 特别盘点 ChatPage 的 upload/limits 直接 fetch、uploadFiles/adoptOversize，QuestionCards 的回答/状态请求和 MessageBubble 的附件/产物链接；不能只封装 lib/api.ts 的 request 就宣称全部身份化。

## 目标边界与实现原则

### 入口与一套原布局

App 确认 `/api/runtime`，control 先 loadIdentity，成功才挂载私人 WorkspaceApp。local 保留原单机功能；服务模式/身份异常显示错误，不能退回 local。身份校验 pending 时不读取旧 session/publish/model/画像缓存，不发私有 API、不闪现旧页面。

将 LocalApp 的原布局和数据行为分开，保留单一原布局组件。原本各页的 JSX/图标/样式不重写；为共享 Sidebar 添加身份/退出等必要 props 即可。不要把 ControlApp 上千行整块塞入 App 或只给 ControlApp 换标题。

control 的初始页本项可为原对话页；工作台/技能/内容/账号/画像等菜单保留原结构，但未接通的组不能触发旧 local 副作用或假装数据为空。可显示明确、无技术细节的暂不可用状态；这是阶段限制，不计 Task 5 完成。不要为消除 503 把未授权 API 全量透传。

### client/hook 协议

按总计划新增 `lib/workspaceClient.ts`、`hooks/useWorkspace.ts` 和 `components/AuthBoundary.tsx`，若更小的等价文件组织更合适，在实施说明写清职责，保持同一 UI 与模式适配，不增加新框架。

对话能力至少覆盖：分页会话目录、创建/重命名/归档/删除、分页历史、提交 Run、SSE 游标重连、停止、权限问题状态/回答。返回原 ChatSession/ChatMessage/StreamState 视图类型；control 的真相来自服务端，local 继续原 store/API 兼容格式。复用 lib/auth.ts 的 IdentityEpoch/identityFetch、lib/control.ts 的解析/恢复工具，不重写安全协议。

API 差异在 client/hook 内处理；组件不自行推断 native_id、生成控制面会话 ID、读全局 Cookie 或决定 workspace 路由。保留 turnId 幂等和旧 Run 恢复，切页/重连不自动重发 prompt。

### 身份生命周期

每轮异步工作绑定 userId/workspaceId/epoch；登出、401、账号切换、卸载时取消请求/SSE/待提交状态，迟到结果不能写入下一身份或 local 缓存。跨标签通知不得串账号。主题偏好可共享，私有业务缓存不共享。

写 API 使用已有 CSRF；所有流式/直接 fetch 必须纳入撤销/取消机制。401 才回登录；403 保留权限提示，503 保留服务错误，不冒充“邀请过期”。退出不能自动重复兑换旧邀请。

ChatPage 的本地 input/attachments/question 状态要随身份切换清理，不能只清父会话数组；在 control 暂未接通的附件功能应显式禁用，不调用 upload/local 等宿主路径接口。Task 5 后再启用实际上传/预览下载。

### 设置兼容

保留当前管理员 chat 配置、成员邀请与个人覆盖的可达性，不能为了复用原侧栏丢掉已经可用能力。Task 3 可以在原设置容器内暂时复用既有 control 设置组件/数据逻辑；完整主题和设置整合由 Task 4 收口。control 不得打开原 SettingsPanel 中写全局 .env、安装 CLI 或改受管命令的 local 分支。

## 修改范围

前端 `web/frontend/src/App.tsx`、原 Sidebar/ChatPage/QuestionCards/MessageBubble 的最小数据接口，lib/api.ts、auth.ts、control.ts、store.ts 及新增 client/hook/认证边界。允许必要的设置入口/组件桥接，不在本项重做视觉。

对话协议稳定，不默认修改后端。确实遇到现有 control 对话接口缺陷时先给确定证据和最小范围，不借本项接通 Task 5 全业务。ControlApp 的协议状态逻辑迁移后可以删除独立壳或暂留无 UI 的兼容导出，不保留两套活跃对话布局。

测试扩充 tests/frontend/multiuserAuth.test.mjs、multiuserHistory.test.mjs、questionCards.test.mjs、sessionHistory.test.mjs、personaSession.test.mjs；新增 workspaceClient.test.mjs 与专用浏览器 fixture，证据放现有 `docs/evidence/` 下 `original-web-task-03` 子目录。不要只靠源文本 marker 测试 UI/身份行为。

## 红绿与浏览器验收

- [ ] 红测：control 登录后使用原 Sidebar/ChatPage；未登录、身份 pending/503 时不挂私人组件、不读取旧私有缓存/API。
- [ ] 红测：A 的延迟会话/历史/模型设置/SSE 在 B 登录后返回，旧结果、草稿、附件、问题卡不提交；跨标签切换与注销也验证。
- [ ] 红测：会话创建/更名/归档/删除、分页与用户新选择交错、202 前后响应顺序、终态历史刷新、失败保留已完成回答、停止后继续，保留已有回归。
- [ ] client/hook 最小实现；local 原历史/画像会话选择、重连/重试仍兼容。
- [ ] 运行 Node 回归和前端 build：

```powershell
node --test tests/frontend/multiuserAuth.test.mjs tests/frontend/multiuserHistory.test.mjs tests/frontend/multiuserSettings.test.mjs tests/frontend/workspaceClient.test.mjs tests/frontend/questionCards.test.mjs tests/frontend/sessionHistory.test.mjs tests/frontend/personaSession.test.mjs
npm --prefix web/frontend run build
git diff --check
```

- [ ] 用真实构建产物和两个浏览器 context 在隔离 fixture 验证原侧栏/对话组件、文本/思考/问题卡/停止、刷新与重连、注销/换号、原设置的安全桥接；提交截图与行为结果。测试 API/受控 ACP 可用 fixture，不冒称真实供应商。
- [ ] 至少验证一次 local 模式原页面与会话行为，不能只测 control。未接通菜单显示正确状态，不能泄露本机数据或出现假空列表。
- [ ] 本项不构建大 Harness 镜像，不接用户容器/真实 Key/登录态。前端依赖已存在则沿用；worktree 缺依赖才用锁文件 npm ci，不改依赖版本。

## 交付

父/新 SHA、文件清单、原组件复用/状态拆分图述、API/缓存/直接 fetch 盘点、身份取消和迟到响应反例、红绿/Node/build、真实浏览器截图与 A/B/local 结果、暂未接通的组、秘密及资源清理。显式文件提交后停止复审，不进入 Task 4/5、不自动上线。
