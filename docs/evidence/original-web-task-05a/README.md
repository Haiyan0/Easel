# 原 Web 修复 Task 5A 证据：基础工作区状态、画像与内容/附件接线

日期：2026-10-30（Asia/Shanghai）。
父基线：`1b57e1927052d930e13779239ff2bc8907895ae5`（Task 4 验收）。
worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## 结论

本组把 status、画像列表/详情/文件读写/删除、内容树/文本/媒体/归档索引/删除、
上传限额与附件上传接通到本人 workspace。原 OutputsPage/ProfilePage/ChatPage/
MessageBubble/ArtifactGallery 继续使用原组件树，通过 WorkspaceData context
注入 local/control 适配器，没有设置模块全局 fetch/当前用户开关。

control 只从已认证 Identity 选择 workspace；每条请求经 GatewayClient 的
Ed25519 逐请求 method/path/query/body 签名到固定 gateway，再由 gateway 注入
workspace 服务凭证到固定 worker。未接入的技能库、工作台数据、账号/发布/
画像 AI 构建仍明确 503，不伪造空列表，不挂 local 宿主副作用。

## 接口对照表（实施前）

| 原组件调用 | method/path/query/body | 当前 worker handler | control policy | gateway 路由/签名 | workspace 路径 | 返回形状 | 本组验收 |
|---|---|---|---|---|---|---|---|
| `fetchStatus()`（Sidebar/App） | GET `/api/status` | `web/app.py:api_status` | `WORKSPACE_READ` | GET 精确路由；仅 ready 时查询 worker | `workspace_root`/`profiles`/`outputs` | `{gateway,transport,runtime,skills,personas,state,detail}` | 未启动/启动中/ready/不可用如实区分；status 不启动 worker、不建 Run |
| `fetchPersonas()` | GET `/api/personas` | `list_personas` | `WORKSPACE_READ` | GET 精确路由 | `workspace_root/profiles` | `[{name,description}]` | A/B 同名画像独立；按需启动 |
| `fetchPersonaDetail()` | GET `/api/persona/{name}` | `api_persona` | `WORKSPACE_READ`（persona） | GET + 单段规范编码 | `profiles/<name>` | `{name,content}` | 404/路径非法明确 |
| `fetchPersonaFiles()` | GET `/api/persona/{name}/files` | `api_persona_files` | `WORKSPACE_READ` | GET | `profiles/<name>/*.md` | `{name,files:[{filename,content}]}` | 六维文件读取 |
| `savePersonaFile()` | PUT `/api/persona/{name}/file` body `{filename,content}` | `api_persona_file_save` | `WORKSPACE_WRITE` + CSRF | PUT + body 签名 | `profiles/<name>/<file>.md` | `{ok,filename}` | 原子写；A/B 不串 |
| `deletePersona()` | DELETE `/api/persona/{name}` | `api_persona_delete` | `WORKSPACE_WRITE` + CSRF | DELETE | `profiles/<name>` | `{ok,deleted}` | 只删本人画像 |
| `fetchOutputs()` | GET `/api/outputs` | `get_output_tree` | `WORKSPACE_READ` | GET | `workspace_root/outputs` | `OutputNode[]` | 本人树；不伪造空树 |
| `fetchArtifactArchive()` | GET `/api/artifacts/archive?path=...` | `api_artifact_archive` | `WORKSPACE_READ` | GET + `path` query 签名 | `outputs/<path>.zip` | `{paths:string[]}` | 只索引本人 zip |
| `fetchOutputContent()` | GET `/api/output/{path}` | `api_output` | `WORKSPACE_READ`（output） | GET + 多段规范编码 | `outputs/<path>` | `{path,content,kind,isBinary}` | 文本/Markdown/HTML/图片按原合约 |
| `deleteOutput()` | DELETE `/api/output/{path}` | `api_output_delete` | `WORKSPACE_WRITE` + CSRF | DELETE | `outputs/<path>` | `{ok,deleted,kind}` | 受保护系统项拒绝 |
| `mediaUrl()`（img/video/audio/iframe/下载） | GET/HEAD `/api/media/{path}` + `Range`/`If-None-Match` | `api_media`（FileResponse） | `WORKSPACE_READ`（media） | GET/HEAD 流式路由；条件头透传 | `outputs/<path>` | 原 MIME/长度/206/304 | HEAD/Range/206；`private` 缓存；ETag 按 workspace 加前缀，A/B 不串 |
| `fetch('/api/upload/limits')` | GET `/api/upload/limits` | `api_upload_limits` | `WORKSPACE_READ` | GET | `outputs/_inbox` | `{ok,max_mb}` | 真实上限 |
| `uploadFiles()` | POST `/api/upload` multipart `files`,`sessionId` | `api_upload` | `WORKSPACE_WRITE` + CSRF | POST raw body 签名（≤60MiB） | `outputs/_inbox/<scope>/<batch>` | `{ok,files:[{id,name,path}]}` | 本人 session；超限/损坏无部分文件 |
| `adoptOversize()` | POST `/api/upload/local` | `api_upload_local` | `FORBIDDEN` | 无 | 任意宿主路径 | — | control 永不调用；UI 明确提示不可用 |
| `submitTurn()` attachments | POST `/api/chat` body `attachments` | proxy `_validate_task` → worker `RunManager` | `TASK` | 既有内部 Run 路由 | `outputs/_inbox/...` | Run 接纳 | 附件引用由同一 workspace 校验并读取；跨空间/内部路径在 prompt 前拒绝 |

说明：`/api/status` 由 control 直接回答（只有已就绪时查询 worker）；本组其余
路由经 control 身份/CSRF → gateway 逐请求签名 → worker 固定 workspace。
未接入的 `/api/skills`、`/api/skill/*`、工作台、账号、发布、画像 AI 构建仍按
5B/5C 明确限制，不挂 local 副作用。

## 实现要点

### control

- 新增 `easel/control/workspace_api.py`：按 Identity 重查 workspace/config
  revision，`RuntimeManager.ensure_ready` 取得固定实例；GatewayClient 以
  `status_mode="workspace"` 转发。短请求登记 `request_leases`，流式响应
  持有 1 小时租约并在关闭/断开时释放，避免下载中被空闲回收。
- `create_app` 新增 `workspace_api`/`workspace_factory` 装配；CLI 在
  gateway 模式下构造 `WorkspaceApi`，未启用 gateway 时明确 503。
- status 只借用 worker 的 gateway 可用性与 skills/personas 目录；返回的
  transport/runtime 固定为 control，不把宿主安装的 Harness 名称当作
  用户配置。未启动时不查询/启动 worker。
- control 路由只做显式映射：status、personas、persona 详情/文件/保存/删除、
  outputs、archive、output 读写删除、media GET/HEAD、upload/limits/upload。
  写请求继续走精确 Origin + CSRF；`/api/upload/local` 仍 `FORBIDDEN`。
- 媒体响应：
  - `Range`/`If-Range`/`If-None-Match` 仅在本 workspace 作用域内透传；
  - worker ETag 加 `easel-<workspace hash>-` 前缀，跨身份的条件请求不会
    命中旧 304/缓存；响应 `Cache-Control: private, no-cache`（HTML 为
    `private, no-store`）、`Vary: Cookie`、`nosniff`；
  - HEAD 保留 content-length；206/content-range 原样保留；304 不作为重定向。
- 上传：control 上限 60MiB（低于 gateway 64MiB），读取有界；worker
  `/api/upload` 写盘前校验整批扩展名，任一步失败/取消删除整批，避免部分
  成功留下孤儿附件。

### gateway

- 新增精确工作区路由表，不使用 `/api/*` 前缀放行；`_Route` 支持多方法与
  HEAD/流式响应。
- 路径段严格解码 UTF-8、拒绝编码 slash/反斜杠/NUL、点段、编码 unreserved
  别名、隐藏首段与 `analytics`/`wechat` 内部目录；query 增加受限 `path`。
- 304 作为合法条件响应处理（其它 3xx 仍失败关闭）；HEAD 不发送 body 但
  保留长度；media 路由流式转发并保留 Content-Range/ETag/CSP 等头。
- 上传 raw body 原样签名；条件请求头仅允许 Range/If-Range/If-None-Match/
  If-Modified-Since，不在 control client 接受任意头。

### worker

- `web/app.py` 的共享画像助手跟随 `workspace_root/profiles`，修复 worker
  模式下画像文件仍读镜像 `code_root/profiles` 的问题；local 行为不变。
- 上传批次改为 all-or-nothing；文件/画像/媒体仍复用原 `_safe_output_path`、
  `_safe_output_target`、`_persona_file_path`、FileResponse 合约。
- `/api/status` 未新增虚假“模型/浏览器/全部技能可用”声明；ready 只代表
  本 workspace 实例可读，实际能力仍由原状态字段表达。

### 前端

- 新增 `lib/workspaceData.ts` + `lib/workspaceDataContext.ts` +
  `components/WorkspaceDataProvider.tsx`：local 默认适配器保持原 API；control
  适配器全部经 `useWorkspace.call`/`fetchText`（身份 epoch、CSRF、401 失效）。
- OutputsPage/ProfilePage/ChatPage/ArtifactGallery 改用 context；没有模块
  全局“当前用户/fetch”开关。身份切换时 WorkspaceApp 以 epoch key 重挂，
  组件清理草稿/附件/预览状态；项目没有 object URL 缓存。
- control 启用画像选择、内容库、附件上传/媒体；成员不获得管理员入口。
  ProfilePage 的新建画像、公众号数据抓取明确禁用/隐藏并说明属于后续版本；
  超限文件在 control 明确提示“不支持本地复制通道”，不调用 upload/local；
  附件必须随文字提交（服务端仍要求 message 非空）。
- ChatPage 发送时把结构化附件和当前画像随 `/api/chat` 提交；重试沿用同一
  附件/画像；control 消息解析保留附件引用。
- 原 Task 3 fixture 的“未请求 upload/limits”断言按本组真实附件能力更新为
  “已请求 upload/limits”；Task 3/4 历史截图在回归后已恢复。

## 红绿

### 红（实现前，父基线 `1b57e19`）

- Node：`tests/frontend/workspaceData.test.mjs` 因 `workspaceData.ts` 不存在
  在加载阶段失败（0 pass / 1 fail）。
- Control API：`test_original_web_api.py` 收集失败
  `ModuleNotFoundError: easel.control.workspace_api`。
- Gateway：`test_gateway_workspace.py` **30 failed / 8 passed**；工作区路由、
  HEAD/Range/304、上传原始 body、traversal 与方法负例均未实现。
- 详见 `runs/red-node.txt`、`runs/red-api.txt`、`runs/red-gateway.txt`。
- 新浏览器矩阵在路由实现后加入，作为真实 dist + 真实 control/gateway/worker
  的端到端验收；其早期失败是夹具本身（一次性邀请复用、选择器）问题，不冒充
  实现前红测。

### 绿

| 项目 | 结果 |
|---|---|
| 全部 frontend Node | **143 passed, 0 failed** |
| `tsc -b && vite build` | 成功，`index-Bw-bcRB_.js` |
| `oxlint` | 退出 0 |
| `tests/multiuser` 全量 | **1558 passed, 5 skipped** |
| `tests/test_core.py` + `tests/test_agent_harnesses.py` | **155 passed, 4 skipped** |
| control static | **3 passed** |
| Task 5A browser matrix | **4/4 组场景，3 次全部通过，FAILURES=[]** |
| Task 3 完整回归 | **33/33，3 次全部通过，FAILURES=[]** |
| Task 4 登录/设置回归 | **8/8，3 次全部通过，FAILURES=[]** |
| 隔离容器文件链路 | **TASK5A_CONTAINER_OK=True，CLEANUP_ERRORS=[]** |

日志：`runs/green-node.txt`、`green-build.txt`、`green-lint.txt`、
`green-multiuser.txt`、`green-static.txt`、`browser-run1..3.txt`、
`task3-run1..3.txt`、`task4-run1..3.txt`、`green-container.txt`。

## 浏览器场景

`tests/frontend/browser_original_web_workspace.py` 使用真实 dist、真实
control app、真实 gateway app、真实 `web.app` worker（进程内 ASGI，临时
workspace，无 Docker 用户实例），覆盖：

- 浅色桌面：内容树、文本预览、真实 800×800 PNG（398752 字节）自然尺寸、
  HEAD content-length、Range 206/content-range、作用域 ETag、HTML iframe
  `sandbox="allow-scripts"`（无 allow-same-origin）与 CSP/nosniff、删除文件。
- 画像：列表/详情、编辑保存落盘、新建画像禁用且说明后续版本、公众号抓取
  不出现、无管理员设置请求。
- 附件：新会话上传真实文件到 `_inbox`，UI 显示 chip；空文字发送被明确提示，
  不产生聊天消息。
- 深色桌面与 390×844 移动端内容库：计算主题 token、无横向溢出。
- 注销后同一媒体 URL 返回 401。

截图：`browser/01-outputs-light.png`、`02-media-light.png`、
`03-html-preview-light.png`、`04-profile-edit-light.png`、
`05-attachment-upload-light.png`、`06-outputs-dark.png`、
`07-outputs-mobile.png`。

## 隔离容器链路

镜像（本地增量构建，未触碰用户容器/卷/网络）：

- control：`easel-control:task5a-20261030`，ID
  `sha256:44ac745d6906f3efd82cb676405225632126f88e26832fd00fa721f8776783dd`；
  由 `deploy/desktop/Dockerfile.control` 构建（前端阶段实际 npm build）。
- gateway：`easel-gateway:task5a-20261030`，ID
  `sha256:afaa29d093d93735ff08aaa9ee379978ff8b25ccf0b040c16162f910ef5d276f`；
  由 `deploy/desktop/Dockerfile.gateway` 构建。
- worker：`easel-worker-execution:task5a-20261030`，ID
  `sha256:2189817cd38a5135d33d6af1fbb8bab240188a8f107e22f2200b0403b67f793d`；
  由 `tests/multiuser/Dockerfile.worker-task5a` 在已存在的
  `easel-worker-execution:a4`（ID `sha256:39f7c847d931...`）上仅覆盖当前
  `easel/`、`web/` 源码构建，**没有无缓存重建 Harness/Chromium 或全栈**。

`tests/multiuser/task5a_container_chain.py` 用随机 run-id 标签创建独立
network/volumes/containers，临时 Ed25519 密钥与假服务凭证；验证：

- control `/api/status` → gateway → worker，state ready；
- 画像列表、内容树、文本、媒体 HEAD/Range 206、上传 `_inbox`、画像保存、
  内容删除、未登录媒体 401；
- 清理前 `docker inspect --type` 校验归属，输出 `CLEANUP_ERRORS=[]`。

真实模型/Harness/Chromium 执行、真实账号与发布未在本组执行；worker 只用
文件链路需要的执行镜像派生层，不声称完整执行链已验收。

## 文件

生产：

- 新增 `easel/control/workspace_api.py`
- 修改 `easel/control/app.py`
- 修改 `easel/control/cli.py`
- 修改 `easel/control/gateway_client.py`
- 修改 `easel/gateway/app.py`
- 修改 `web/app.py`
- 新增 `web/frontend/src/lib/workspaceData.ts`
- 新增 `web/frontend/src/lib/workspaceDataContext.ts`
- 新增 `web/frontend/src/components/WorkspaceDataProvider.tsx`
- 修改 `web/frontend/src/lib/auth.ts`
- 修改 `web/frontend/src/lib/workspaceClient.ts`
- 修改 `web/frontend/src/hooks/useWorkspace.ts`
- 修改 `web/frontend/src/components/WorkspaceApp.tsx`
- 修改 `web/frontend/src/components/WorkspaceLayout.tsx`
- 修改 `web/frontend/src/components/Sidebar.tsx`
- 修改 `web/frontend/src/components/OutputsPage.tsx`
- 修改 `web/frontend/src/components/ProfilePage.tsx`
- 修改 `web/frontend/src/components/ChatPage.tsx`
- 修改 `web/frontend/src/components/ArtifactGallery.tsx`
- 修改 `web/frontend/src/styles/index.css`

测试：

- 新增 `tests/multiuser/test_original_web_api.py`
- 新增 `tests/multiuser/test_gateway_workspace.py`
- 新增 `tests/multiuser/task5a_container_chain.py`
- 新增 `tests/multiuser/Dockerfile.worker-task5a`
- 新增 `tests/frontend/browser_original_web_workspace.py`
- 新增 `tests/frontend/workspaceData.test.mjs`
- 修改 `tests/frontend/workspaceClient.test.mjs`
- 修改 `tests/frontend/personaSession.test.mjs`
- 修改 `tests/frontend/artifacts.test.mjs`
- 修改 `tests/frontend/browser_original_web_task03.py`

未接入：skills/技能执行、工作台热点/选题/日历/概览、账号/平台登录/分析/
发布、画像新建与 AI 构建（5B/5C）。未修改 Task 1/2 模型与 Task 3/4 登录/
题卡/主题协议；未改 Harness、worker 执行状态机或 control Dockerfile。

## 边界与清理

- 未修改主审 audit/task/spec/plan/probe；未进入 5B/5C/Task 6；未自动上线。
- 未读取或复制真实 Key、`.env`、`.dsh`、Cookie、邀请；未重放用户 prompt，
  未调用真实付费模型，未发布。
- 用户当前 control/worker/gateway/sidecar、原卷/socket 未动；未执行全局
  prune，未删除历史 `easel-ws-*` 网络。
- 容器证据只创建带 `easel.test.run-id` 标签的临时资源并在归属校验后清理；
  用户容器列表与开始时一致（另有本次任务前已存在的用户实例）。
- Task 3/4 历史截图已恢复；Task 5A 截图与日志只写入
  `docs/evidence/original-web-task-05a/`。
