# 原 Web 修复 Task 5A：基础工作区状态、画像与内容/附件接线

状态：Task 4 已验收，当前仅 Task 5 的基础组 5A。代码 Agent 实施，主会话审计；交付后停止，不进入 5B/5C 或 Task 6，不切换用户实例。
父基线：`1b57e1927052d930e13779239ff2bc8907895ae5`。
继续 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## 必读与交付边界

读 AGENTS.md、[修复设计](../specs/2026-10-29-original-web-login-and-chat-repair.md)、[总计划 Task 5](../plans/2026-10-29-original-web-login-and-chat-repair.md)、[Task 4 验收](../audits/2026-10-30-original-web-task-04-accepted.md)、[运行实例保护清单](2026-10-29-original-web-running-baseline.json)。

本组复用原画像/内容库/附件组件和既有 worker handler，让本人基础数据通过 control 授权访问；不只打开菜单、不伪造空列表，不新增第二套页面或全量任意反向代理。

范围为 status、画像列表/详情/文件读写/删除，内容树/文本/媒体/归档预览索引/删除，正常上传与上传限额、对话附件引用。画像 AI 构建、技能执行、平台账号/分析/发布属于 5C；热点/选题/日历/概览属于 5B，当前保持明确限制。

## 1. 先做实际接口表

提交前在本项证据中整理“原组件调用→method/path/query/body→当前 handler→control policy→gateway 签名/白名单→workspace 路径→返回形状→验收”对照表，先完成本组，再写接线代码。

已定位的原接口（以实际代码为准，不猜 payload）：

| 组 | 原入口 |
|---|---|
| 状态 | GET /api/status |
| 画像 | GET /api/personas；GET/DELETE /api/persona/{name}；GET /api/persona/{name}/files；PUT /api/persona/{name}/file |
| 内容 | GET /api/outputs；GET /api/artifacts/archive?path=...；GET/DELETE /api/output/{path} |
| 媒体 | GET/HEAD /api/media/{path}，原页面使用的 Range/条件请求 |
| 附件 | GET /api/upload/limits；POST /api/upload；原上传字段与 session 归属 |

`POST /api/upload/local` 在 control 仍拒绝，不能从浏览器参数采用任意服务器文件。超限上传在 UI 明确提示，不回退 local adoption。原 persona 构建/新建向导若依赖 5C，不假装已接通；本组只启用已有数据管理能力并明确剩余依赖。

## 2. control / gateway / worker 边界

- control 从已认证 Identity 选择 workspace，URL/body 不能选别人容器、根路径、上游或服务凭证。所有写请求保留 CSRF/精确 Origin，成员只能本人数据。
- 复用现有 RuntimeManager/provisioner/WorkerHandle、gateway 精确 audience/method/path/query/body 签名和 worker guard；只为本组新增必要的显式路由与受限参数，不用 `/api/*` 前缀放行，不接受任意命令/URL。
- 纯数据访问不得为伪造账本创建 chat Run/prompt；需要按需准备 worker 时检查身份/实例代次，活跃文件请求纳入原请求/idle 生命周期，不能下载中被空闲回收。
- status 如实区分未启动/按需启动/启动中/已就绪/不可用；不能 health 200 就声称模型、浏览器或全部技能可用。不得把宿主已安装/凭证状态泄露为用户配置。
- 保留现有配置/模型/Run 接纳、取消、权限和恢复机制，不建立另一套调度器。旧实例暂时不可达不自动删除活跃 worker，不为读文件重复 prompt。
- 普通路径与非聊天业务不绕过现有认证；未装配的其它组继续明确拒绝，不映射到 host local handler。

## 3. 文件/画像/附件要求

- worker 仅本人 /workspace 与 HOME，沿用原安全路径工具。验证名称、相对路径、编码、目录穿越、绝对路径、symlink/junction 与内部目录；凭证、数据库、原生内部事件、.credentials/.gateway、账号状态不作为公共产物。
- A/B 同名画像/文件各自独立；JSON、Markdown、HTML、图片、音视频、PDF、目录及缺文件保持原页面需要的返回形状，不把错误包装为空数据。
- GET/HEAD/Range、MIME、长度、下载 disposition 按原 handler 合约保留。媒体也是私有 API：请求先认证授权，不能改成公共 static、暴露 worker URL/服务 token，或通过缓存/304 把 A 文件显示给 B。
- 大文件流式转发、背压/断开清理与必要租约保持；限制大小/数量/超时，不为 body 签名无界读入内存。上传失败/取消不留下伪成功附件或部分可用文件。
- 上传 session 必须属于本人；使用服务器返回的附件引用及规范路径，不相信客户端任意 path/native_id。正常附件能进入本人 chat 并由同一 workspace 读取，跨空间/内部路径在 prompt 前拒绝。
- HTML/SVG 等主动内容沿用原沙箱隔离；预览 iframe 不加 allow-same-origin + allow-scripts 的组合。直接打开/下载的响应也不能让产物脚本获得主站已登录身份写配置；选用最小沙箱/CSP/下载策略并保持原可用预览。
- 删除只影响本人允许内容，不删除数据根/系统目录/账号密钥；失败明确报错。原用户数据不用于测试。

## 4. 原前端的数据适配

通过现有 workspaceClient、props 或 React context 注入原 OutputsPage/ProfilePage/ChatPage/MessageBubble/ArtifactGallery 等所需接口，local 默认适配保持；不设置模块全局“当前用户/fetch”开关造成切换串用。

盘点直接 fetch、media URL、HTML/归档缓存和 object URL，不仅改 lib/api.request。control 走身份/CSRF、epoch/AbortSignal/迟到提交检查，身份切换清掉旧页面/附件/预览与本人的缓存，撤销 object URL；主题偏好继续共享。

只在本组路由真实可用后启用对应菜单/画像选择/附件与媒体；未知能力保持明确不可用。工作台/账号等未接组不为了消除错误挂载原 local 副作用。原页面/主题不重写，不变更 Task 4 登录/设置流程。

## 5. 修改范围与测试

后端按需要修改 control/app.py、policy.py、gateway/app.py/auth 的受限匹配、control gateway client/受控业务转发及 web/app.py 原 workspace handler；可在 `easel/control/workspace_api.py` 等专责模块抽取数据转发，不把全部实现继续堆入大文件。

前端只做本组所需原组件/client/hook 适配与能力启用。每项实际文件在实施说明列出职责，不能泛化重构底座、网络策略、认证或其它业务组。

新增 tests/multiuser/test_original_web_api.py 等真实 Store/临时文件/受控 transport 测试，补 gateway 明确路由/签名/流清理测试和 tests/frontend 原页面行为/浏览器 fixture。证据放现有 docs/evidence 下 original-web-task-05a 子目录，主审文档/probe 只读。

## 6. 红绿与验收清单

- [ ] 接口表中的本组每项先行为红测，再最小实现；未认证 401、成员跨空间拒绝、伪造内部头/目标拒绝。
- [ ] 两用户真实临时文件同名画像/内容，创建/读写/删除/刷新/预览各自独立；绝对路径、编码穿越、symlink/junction、内部目录、注销后新媒体请求均拒绝。
- [ ] 正常上传→附件引用→本人对话可读取；跨空间 session/附件、超限/取消/损坏输入无部分成功。control 不调用 upload/local。
- [ ] 真实原 OutputsPage/ProfilePage/ChatPage 与静态/媒体资源链路验证，媒体 HEAD/Range、iframe 沙箱及缓存策略实际断言；不是只测后端 200。
- [ ] 流下载中取消/断开无遗留响应/容量，worker 状态与其它已有 Run 不串；失败不自动重发任务。
- [ ] Task 1/2 已审模型与 Task 3/4 身份/题卡/主题保持。按受影响范围跑后端/gateway回归、frontend Node/build/lint，复跑33对话+8登录/设置矩阵；新增本组浏览器场景单列。
- [ ] 使用新建隔离 control/worker/gateway + 假凭证做一次真实容器文件链路（文件由测试直接生成，不调用收费模型）；核对镜像/源码。只增量构建受影响镜像，不无缓存重建 Harness 或全栈。

## 交付与资源

指定 Agent Python；临时 HOME、数据库、workspace 和 Key 全新，容器/网络/卷随机标签且归属核验。用户当前 control/worker/gateway/sidecar、原卷/socket、真实 Key/Cookie/邀请保持不动；不得重放用户 prompt、真实发布、全局 prune 或删除历史 easel-ws-* 网络。

显式文件提交，交付父/新 SHA、接口表、路径/签名/生命周期方案、红绿、A/B/媒体/上传/流清理证据、真实容器与 fixture/not-run 区分、保留限制与清理清单。5A 通过不等于 Task 5 或整体完成；提交后停止复审，不进入 5B/5C/6、不自动上线。
