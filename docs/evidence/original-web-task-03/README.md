# 原 Web 修复 Task 3 证据：共用认证与会话数据层

> Task 3 R1 三项返工证据见 [R1.md](R1.md)。

日期：2026-10-29（Asia/Shanghai）。
父基线：`4bb993bb353f23f5eec06442d2f6ce1c6c2e3090`（Task 2 验收）。
worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## 结论

登录后的 control 模式现在挂载**同一套原 Sidebar / ChatPage / MessageBubble /
QuestionCards / 原布局**，不再使用独立 `ControlApp` 自绘壳；local 继续使用
原单机 store/API。身份未确认、401、503 时不会挂载私人工作区，也不会读取
旧 session/publish/model/画像缓存。A 的迟到会话、历史、SSE、设置响应在
登出/换号后不会写入 B。会话 CRUD、分页、问题卡、停止后继续、终态历史
刷新、失败保留与刷新重连均已覆盖。

本项只完成认证/会话数据层与对话视图；登录页视觉、原设置整合、未接通
页面后端分别留给 Task 4/5。附件上传、媒体/产物预览在 control 显式禁用，
没有调用 local 宿主路径接口。真实供应商聊天未在本项验收。

## 提交与文件

- 生产提交：`2c6bc6ca74443e38748312e9c935744157306318`
- 证据提交：本文件所在提交
- 删除：`web/frontend/src/components/ControlApp.tsx`（独立布局壳）
- 新增：
  - `web/frontend/src/components/AuthBoundary.tsx`
  - `web/frontend/src/components/WorkspaceApp.tsx`
  - `web/frontend/src/components/WorkspaceLayout.tsx`
  - `web/frontend/src/hooks/useWorkspace.ts`
  - `web/frontend/src/lib/workspaceClient.ts`
  - `web/frontend/src/lib/questions.ts`
  - `tests/frontend/workspaceClient.test.mjs`
  - `tests/frontend/browser_original_web_task03.py`
- 最小适配：
  - `web/frontend/src/App.tsx`、`components/Sidebar.tsx`、`components/ChatPage.tsx`、
    `components/MessageBubble.tsx`、`components/QuestionCards.tsx`
  - `lib/api.ts`、`lib/auth.ts`、`lib/store.ts`、`styles/index.css`
  - `tests/frontend/multiuserAuth.test.mjs`、`multiuserHistory.test.mjs`、
    `questionCards.test.mjs`、`sessionHistory.test.mjs`、`personaSession.test.mjs`

## 实施前盘点与职责拆分

### App / LocalApp / ControlApp 原职责

- `App`：只调用 `/api/runtime` 判定 control/local；未知/失败显示错误，不回落。
- `LocalApp`：原布局 + 本机 sessions/personas/streams/设置/引导；直接使用
  `lib/store.ts`、`lib/api.ts` 与本地 `localStorage`。
- 旧 `ControlApp`：上层身份检查/登录/登出/跨标签；下层 `ControlWorkspace`
  持有服务端会话、消息窗口、Run、SSE、题卡、设置和管理员设置。

### 迁移到共享 client/hook 的状态

| 原 ControlApp 状态/副作用 | Task 3 位置 |
|---|---|
| 身份检查、登录、登出、跨标签 identity 广播 | `AuthBoundary` |
| 服务端会话目录/分页/选择代次 | `useWorkspace` + `workspaceClient.listSessions` |
| 创建/更名/归档/删除 | `workspaceClient` + hook |
| 消息最新窗口/加载更早 | `workspaceClient.loadMessages` + hook |
| 提交 Run、202 接纳、turnId 幂等 | `workspaceClient.submitTurn` + hook |
| SSE 帧/游标/重连/终态历史收敛 | `workspaceClient.streamRun` + hook |
| 停止、问题状态/回答 | `workspaceClient` + hook |
| 设置/管理员设置的 `call` | hook 暴露 `call`（identityFetch） |
| 恢复记录 `run:<session>` | identity-scoped `sessionStorage`，由 hook 读写 |

### 仅 local 保留

- `lib/store.ts` 的本地会话/草稿/标题生成/`localStorage`。
- 本地流式 `lib/api.ts::streamChat`、`stopChat`、`fetchLastTurn`、OpenClaw
  sessionKey 删除。
- 画像选择、设置面板的模型/环境安装、发布/内容库/账号等页面副作用。
- 引导向导、发布草稿、主题偏好（共享主题不是私有业务数据）。

### 直接 fetch / 流 / 上传盘点

| 入口 | control 处理 |
|---|---|
| `ChatPage` `/api/upload/limits` | `attachmentsEnabled=false` 时不请求 |
| `uploadFiles` `/api/upload`、`adoptOversize` `/api/upload/local` | control 禁用素材按钮/拖拽/粘贴，`doUpload` 守卫；不调用宿主路径 |
| `QuestionCards` 直接 `answerQuestion` | 新增 `onAnswer` 注入；control 走 `/api/chat/question/answer`（runId + opaque value），local 保留原调用 |
| `questionStatus` | control 在 hook 内用 runId 查询，只显示 pending；local 原逻辑不变 |
| `MessageBubble` `linkifyOutputs` / `externalizeMediaLinks` / `ArtifactGallery` | control `mediaEnabled=false`，只渲染文本 Markdown，不生成 `/api/media` 或内容库链接 |
| `lib/api.ts::request` | control 不使用；由 `identityFetch`/`workspaceClient` 替代 |
| `streamChat` | control 不使用；由 `workspaceClient.streamRun` 消费 jobs SSE |
| `stopChat` / `fetchLastTurn` / `deleteSession` | control 分别用 `stopRun` / 服务端消息快照 / `deleteSession` |
| `ControlSettingsPage`/`AdminSettingsPage` | 通过 hook `call`（CSRF/401/AbortSignal）；不进入 local SettingsPanel 的环境安装/写 .env 分支 |

### 原组件 props/最小接口

- `Sidebar`：新增 `mode`、`identityLabel`、`onLogout`、`personasEnabled`、
  `showEmptySessions`、`statusText/statusOnline`、`hasMoreSessions`/
  `onLoadMoreSessions`；原导航/图标/样式不变。
- `ChatPage`：新增 `mode`、`attachmentsEnabled`、`identityKey`、`notice`、
  `retryAvailable/onRetrySend`、`reconnectAvailable/onReconnect`、
  `hasOlder/loadingOlder/onLoadOlder`、`onAnswerQuestion`；原消息/输入区/
  建议卡/布局不变。
- `QuestionCards`：新增 `onAnswer` 注入、opaque `value`、`data-value`；
  local 行为保持。
- `MessageBubble`：新增 `mediaEnabled` 与 pending/failed 提示；渲染结构不变。
- `WorkspaceLayout`：从 `LocalApp` 抽出的原 `app-layout`/Sidebar/SubNav/
  `page-host`，LocalApp 与 control 共用。

## 身份生命周期与反例

- `AuthBoundary` 在 identity 成功前只显示确认中/错误/登录页；不挂载
  `WorkspaceApp`。503 显示服务错误并可重试，不回落 local。
- 每次 `adopt` 都 `IdentityEpoch.begin`；logout/401/跨标签切换/卸载
  `invalidate`，并只清理该 `userId/workspaceId` 前缀的缓存键。
- `useWorkspace` 的每个异步提交复核 epoch、当前会话、selection/message/
  send 代次；卸载 abort 普通请求和 SSE。
- 迟到结果：A 的 history/settings/SSE 在 B 登录后返回时，旧 hook 已卸载/
  epoch 失效，不能 `setState` 到 B；旧 401 只失效其捕获 epoch。
- `ChatPage` 以 `epoch:sessionId` 为 key 重挂，且 `identityKey` effect 清理
  input/attachments/uploading/drag；问题卡随 `ChatPage` 卸载，不跨身份。

## 红绿

### 红（实现前）

Node（5 failed / 43 passed）：

```text
node --test tests/frontend/multiuserAuth.test.mjs tests/frontend/multiuserHistory.test.mjs tests/frontend/multiuserSettings.test.mjs tests/frontend/workspaceClient.test.mjs tests/frontend/questionCards.test.mjs tests/frontend/sessionHistory.test.mjs tests/frontend/personaSession.test.mjs
```

失败点：`identityFetchRaw`/`clearIdentityCache` 不存在；`workspaceClient.ts`
不存在（workspaceClient/personaSession 加载失败）；题卡不携带 opaque value。
日志：`red-node-task3.txt`。

浏览器最终 fixture 对**基线 `4bb993b` 构建**（`red-baseline-build.txt`，产物
hash 与基线一致）：

```text
13 个 control 场景失败于登录后 `.sidebar` 不存在；2 个通过：
  control_identity_unavailable（旧 ControlApp 本身已不挂私人树）
  local_original_chat（local 原行为）
```

日志：`red-browser-final.txt`。除 `control_identity_unavailable` 外，
最终 fixture 的 13 个涉及原布局/会话数据的 control 场景在旧实现上均未通过；
该 503 场景在旧 ControlApp 已具备身份壳，故作为保留回归通过。

### 绿（当前提交）

```text
node --test tests/frontend/*.test.mjs
131 passed, 0 failed

npm --prefix web/frontend run build
tsc -b + vite build 成功

npm --prefix web/frontend run lint
无输出/无 warning
```

日志：`green-node-final.txt`、`green-build-final.txt`、`lint-final.txt`。

### 真实构建浏览器验收

```text
python tests/frontend/browser_original_web_task03.py
19/19 场景通过，0 failures
```

日志：`browser-final.txt`；截图：`browser/01..18-*.png`。

覆盖：

- control 未登录不出现 Sidebar/ChatPage、不请求 `/api/sessions`；登录后原
  Sidebar 导航 + 原 ChatPage + A 会话历史；未接通菜单显示“暂未接通”且
  不请求 `/api/skills`；设置桥接显示原侧栏 + control 设置，不请求
  `/api/env/tools`/`/api/settings/bootstrap`；无 `/api/upload/limits`。
- 文本/思考/活动/问题卡（opaque `qopt_allow`）/停止/停止后继续。
- 刷新中断后按 Run 回放，不重发 POST `/api/chat`（POST 计数不变）。
- A 草稿/SSE/延迟历史在 B 登录后不进入 B；跨标签 logout→B login 两页都
  只显示 B；A 设置响应延迟返回不污染 B。
- 目录刷新与用户新选择交错时保持用户选择；202 接纳响应延迟到切换会话
  之后，不抢占当前会话，切回后按 Run 恢复。
- 会话创建/改名/归档/删除；101 条会话分页加载；501 条消息“加载更早”；
  503 发送失败保留已完成回答，同一 turnId 重试成功。
- identity 503 不挂私人树、不读预置私有 `easel_sessions` 缓存、不请求
  `/api/sessions`。
- local 无登录 Cookie，原 Sidebar/ChatPage 读取本地历史、发送本地流式消息，
  并请求 `/api/upload/limits`（local 附件能力仍启用）。

## 暂未接通/边界

- 控制模式附件上传、媒体/产物预览/下载：显式禁用/不生成宿主链接；Task 5
  再启用。
- 工作台/热点/选题/日历/发布/技能库/内容库/账号/画像：保留原菜单，显示
  明确“暂未接通”，不挂载原页面、不请求 local API、不伪造空数据。
- 设置：复用 control 个人设置/管理员设置组件；完整主题和设置整合在 Task 4。
- 登录页仍为现有 `LoginPage`（含 EASEL CONTROL 文案），视觉/品牌收口在
  Task 4；本项只保证身份边界与入口。
- 真实 DeepSeek/用户真实聊天未验证；本项浏览器使用假 control/local API
  与假邀请/CSRF，不冒称真实供应商。
- 未改后端对话协议；未构建 Harness/control 镜像；未接入用户容器、真实
  Key、Cookie、邀请或卷。

## 资源与清理

- 浏览器 fixture 仅监听 `127.0.0.1` 随机端口，使用内存假邀请/CSRF/A/B
  会话与 SSE；截图未包含真实凭证。
- 基线红证使用临时 detached worktree `task3-baseline-red`，通过 junction
  复用当前 `node_modules`；构建后已删除 junction 与临时 worktree，当前
  `node_modules` 未受影响。
- 未启动/停止/重启/替换任何用户容器，未执行 Docker，未触碰用户卷、Key、
  Cookie 或邀请。
