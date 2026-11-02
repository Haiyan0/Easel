# Task 12：受管配置展示、个人覆盖与管理员设置执行计划

> **For agentic workers:** 使用 superpowers:executing-plans，按本单逐步先红测试再实现。沿用外部代码 Agent 执行方式；完成后停止等待主审，不进入 Task 13。用户已授权主审下达下一批指令，无须再次选择执行方式或提供服务器。

**Goal：** 在已验收的 control 应用中提供个人能力模型设置，以及管理员默认模型、成员和邀请管理；保持聊天和身份生命周期完整。

**Architecture：** 设置沿用 Task 11 的身份请求边界，控制层继续负责权限、配置合并、加密与版本；浏览器只编辑公共配置和本次输入的凭证。control 页面与 local 设置隔离，设置切换不能重置聊天的待确认发送或制造新的 prompt。

**Tech Stack：** 现有 React/TypeScript/Vite、Node test runner、FastAPI、SQLite、cryptography、HTTPX 和现有 Chromium/Playwright 验收装配，不引入第二套状态管理、数据库或 bundler。

**Spec：** [原规格](../specs/2026-10-16-multiuser-workspaces-design.md) §5/8/9、[原计划 Task 12](../plans/2026-10-16-multiuser-workspaces.md)、[Task 11 验收](../audits/2026-10-25-task-11-accepted.md)、[本地开发与发布分离补充](../specs/2026-10-22-local-docker-development-gates.md)。本单按实际已实现接口具体化原计划，原计划中的示意字段/函数不能被误作现有 API。

## 基线、范围和文件职责

- 基线 a6fc17713c65cd0acced72fffdccf72586878d6c，先核对 HEAD、分支、工作区。不得 reset、暂存或提交主审既有 plan/spec、审计、任务单。
- C:/Code/Easel，Windows/PowerShell；Python 固定 C:/Users/hyh/anaconda3/envs/Agent/python.exe。依赖沿用当前环境。
- Task 11 已验收；Task 8 正式隔离未通过。本批使用临时 control/worker/浏览器测试，不要求远端 Linux 或真实供应商。
- 本批开放：受管底座脱敏摘要、个人 speech/image/video/music 覆盖、管理员 speech/image/video/music/chat 默认配置、成员创建/禁用与邀请/登录撤销。
- 主聊天 chat 只在管理员默认页可改，个人不能覆盖。ACP 可执行命令、安装、环境、沙箱、自动批准和底座发布仍由现有受管部署配置负责；本批不增加浏览器任意命令/环境编辑或执行入口。管理员可编辑默认主聊天模型，不等于可在个人页改底座。
- 不装配 skill、发布、平台登录、analytics、profile/build 等 Task 10 尚未开放业务；这些路由继续明确 503/拒绝。

建议文件拆分（Create 为计划新文件，不是已有文件）：

```text
Create: web/frontend/src/components/ControlSettingsPage.tsx 个人设置和受管摘要
Create: web/frontend/src/components/AdminSettingsPage.tsx   全局默认/成员/邀请
Create: web/frontend/src/lib/controlSettings.ts             类型、表单和协议转换
Create: easel/control/admin.py                              管理员操作服务与公共视图
Create: tests/frontend/multiuserSettings.test.mjs
Create: tests/multiuser/test_settings_api.py
Create: tests/multiuser/test_admin_api.py
Create: tests/frontend/browser_task12.py
Modify: web/frontend/src/components/ControlApp.tsx          只增加导航和装配
Modify: easel/control/{app,config,store,auth,policy,cli}.py   必要的窄接口
```

现有 local SettingsPanel/HarnessSettings 可复用纯展示/表单逻辑；不要挂载会读取 local 缓存、写 .env 或触发单实例安装/网关调用的容器组件。不重构已验收聊天状态机。

## 现有契约：先读实现，不重造配置服务

以下已经存在，保持兼容：

- GET /api/settings/models 返回 version、channels。通道含 configured/source/version/mode/provider/model/base_url/protocol/options/allowed_models，以及 override_allowed、broker_supported、broker_adapter。不得为了匹配原计划示意 camelCase 字段而移除或重命名这些字段。
- POST /api/settings/models/save 接收 `{channel, route}`，不是 rows 数组。route_kind 为 complete 或 managed-model；恢复继承使用 DELETE /api/settings/models/overrides/{channel}，返回 204。
- POST /api/admin/managed-config 接收 `{defaults: {channel: route}}` 并要求 If-Match，返回 `{version}`；未配置版本为 0。ConfigService.publish 保存完整版本，不是静默单通道 PATCH。
- ConfigService.public_settings/save_override/clear_override/publish 为现有服务；CHANNELS 只有 speech/image/video/music，ADMIN_CHANNELS 另含 chat。speech 对应 registry 的 voice 组，不添加不存在的 asr 通道来绕过校验。
- AuthService.create_invite(actor, user_id) 返回一次性明文；revoke_all(user_id) 是内部方法，不带 actor。Store.create_user/disable_user 也是内部能力，不能未经管理员事务内复查就直接暴露给 HTTP。
- 管理命令/签名配置来自 easel/control/snapshot.py 和 CLI 的装配；不要为展示摘要调用会签发 broker token 的任务配置提供器。

## 新增窄接口

1. GET /api/settings/harness：本人可读脱敏摘要，至少返回 locked=true、configured、runtime、revision 和缺失时的 unavailableReason。只投影管理员已装配的公开元数据，不返回 argv/env、宿主路径、profile 文件、签名材料或凭证。create_app/CLI 必要时显式传入只读摘要。缺装配明确标为未配置；写入/安装/重启边界保持原策略。
2. GET /api/admin/managed-config：只从最新受管版本返回 `{version, defaults}` 的公共配置和凭证是否已配置的布尔状态，不能混入管理员本人的个人覆盖；仅 admin 可读。新增 ConfigService.public_managed_settings(actor) 做白名单投影，不返回数据库行或内部 EffectiveRoute。
3. GET/POST /api/admin/users：有界分页列表及创建 member，POST 只接收 displayName。GET 返回公共用户/workspace 标识、显示名、角色、禁用状态；不返回登录/邀请摘要、worker token、内部地址或文件。
4. PATCH /api/admin/users/{id}：只管理成员禁用状态；角色不可由请求体修改，不提供代理登录。禁用后已有登录不可再认证、未兑换邀请不可继续使用；重新启用不复活已撤销凭证，需新邀请。HTTP 本批不能禁用管理员，防止锁死管理入口。
5. POST /api/admin/users/{id}/revoke-sessions：持久撤销目标成员的全部登录，不删除 workspace、会话或 Run。该新路由须精确登记 policy；无宽泛 admin 前缀放行。既有 SSE 通过现有认证窗口结束，不改为取消整个 worker。
6. GET/POST /api/admin/invites、DELETE /api/admin/invites/{id}：列表只给 id/userId/到期/兑换/撤销等公共元数据；POST `{userId}` 返回一次性 token、id 和 expiresAt；DELETE 按邀请 ID 撤销。不能要求 UI 再提交已不应保存的明文来撤销，也不能在列表重建/返回 token。

用户/邀请列表默认 limit=50、最大 200，offset 非负，稳定排序与 nextOffset；沿用现有查询校验模式。未知成员/邀请明确 404，不伪造成功。每次管理员操作在实际写事务内复查 actor 的角色/禁用状态与目标；不得只信 Cookie 之外传入的 role 或旧 Identity.role。保留 Store 原内部调用兼容，新增 actor-aware 包装或 Store 方法，不把内部无 actor 函数直接当授权边界。

新增 AdminService(store, auth) 作为上述 HTTP 的服务边界，方法为 list_users(actor, *, limit, offset)、create_member(actor, display_name)、set_member_disabled(actor, user_id, disabled)、revoke_member_sessions(actor, user_id)、list_invites(actor, *, limit, offset)、create_invite(actor, user_id)、revoke_invite(actor, invite_id)。只返回对应接口公共视图；分页返回 items/nextOffset，签发返回 id/token/expiresAt。保留 AuthService 的内部方法签名。服务和 Store 沿用 create_app lifespan 的专用数据库线程，HTTP 通过现有 _run_db 调用，不跨线程共享旧连接。

## Global Constraints

- 全部私有请求使用同源 Cookie、现有身份 epoch 和写请求 Origin/CSRF；管理员接口必须服务端拒绝 member，隐藏按钮不足以授权。
- 密钥只允许本次输入暂存内存并经同源请求提交；不写 localStorage/sessionStorage/URL/BroadcastChannel/日志/截图。响应不含 secret、credential_ref、密文、原始行或可导出 bundle。
- 个人 complete 的空凭证只复用本人既有同 provider/base_url/protocol 的密文；换地址/协议/供应商必须重新输入，绝不复制管理员 Key。managed-model 只选 allowlist 模型和非敏感参数；删除覆盖后继承最新管理员默认。
- 管理员默认编辑的空凭证语义：仅在已有该通道、同 provider/base_url/protocol 时保留原受管凭证；新增通道或更换凭证作用域必须显式输入。若需扩展 publish，在受管版本一致性与 If-Match 保护下处理并按新版本 AAD 重加密。不能把空字符串存成假凭证，也不能经公共响应把旧 Key 送给浏览器再回传。
- 管理员默认发布必须先读完整公共版本，编辑只改变预期通道；If-Match 缺失或过期拒绝，不自动覆盖他人新版本。旧修订不可改写，个人覆盖不随默认更新被删除。
- options 保持现有白名单、标量和有限数值约束；出口策略、broker 适配支持检查不因 UI 供应商选择放宽。
- 页面用中文标明“个人设置”“全体默认”“继承管理员默认”和配置来源/版本。broker_supported 只说明适配支持，不能把尚无真实验证的凭证显示为“连接已测试通过”。未装配 available/selftest 时明确不可用，不自动轮询或调用收费接口。
- 切换聊天/设置/管理页不得重建身份 epoch、丢 LocalSend/turnId、暗中 stop 或重复 POST。设置请求/保存/错误也要归属当前身份和对应表单代次，A 的迟到响应不能回填 B；退出清表单和邀请明文。
- 邀请明文只显示本次签发结果，复制按钮显式由管理员点击；不放分享 URL、不持久化。关闭结果或离开管理页清除明文，重开列表看不到原 token。
- local 模式继续使用原有工作流。不得修改开发者真实配置、账号或持久目录；不自动迁移旧 local 缓存到多人账号。

## Review Focus

1. A 保存/读取设置时退出切 B，旧响应/401 不回填 B，也不泄漏 A 刚输入的 Key。
2. 个人空 Key 更换目的地址或参数嵌套注入凭证不能成功；失败不改原配置或密文。
3. 两管理员同版本编辑，后提交者 409，原有效凭证和未编辑通道保留；不以“用户再试一次”覆盖冲突。
4. 管理员在操作前被另一连接禁用/撤销权限，事务内拒绝；普通用户伪造 admin/目标 workspace 不能管理别人。
5. 等待题卡或 admission 期间进出设置页，聊天、待确认输入、问题选择和 SSE 回放保持，不重复执行。

## 实施步骤与验收

### 1. 后端契约与授权（TDD）

- [ ] 新建 test_settings_api.py/test_admin_api.py，用真实 bootstrap/exchange Cookie 与 Origin/CSRF 测成员越权、合法默认/个人保存、管理员公共视图和用户/邀请管理；先在基线实际跑红。
- [ ] 实现窄服务与 actor-aware 事务操作，再接 create_app 的专用数据库线程。新路由精确登记 policy，未知方法/路径仍拒绝；缺配置服务仍 503。
- [ ] 验证列出的接口状态、分页、禁用/重新启用不复活旧凭证、邀请一次兑换与按 ID 撤销；禁用/撤销不删除文件、历史或 Run。两真实连接验证事务内角色复查。

### 2. 配置编辑与凭证保留（TDD）

- [ ] 写管理员空凭证保留/换作用域拒绝、多通道不丢失、双 Key、非法 options/非有限数字及并发 If-Match 反例。
- [ ] 在 ConfigService 的既有验证/加密/版本路径中实现所需扩展，保持个人 complete 与 managed-model 的原契约；不要复制黑名单校验或建明文旁路。
- [ ] 证明 DB 公共 JSON、公共响应和日志无测试凭证 sentinel；失败前后版本/配置/密文不变。旧 broker token 固定快照、新 token 使用新默认的测试继续通过。

### 3. control 设置页与管理员页（TDD）

- [ ] 写 multiuserSettings.test.mjs，测试来源/模式转换、原样协议请求、空 Key 提交、冲突保留表单、member 不显示管理写入口和 A→B 迟到不提交；实际组件测试不能只有源码字符串断言。
- [ ] 新组件通过 Task 11 的身份请求边界装配；个人页只操作本人能力，管理员另有全局默认和成员/邀请页。固定当前表单/身份所有权，不把 secret 读回输入框或变成缓存。
- [ ] 保留聊天组件状态和待确认发送；不要为设置导航重写或重置已验收的消息状态机。补纯展示复用时保持 local 组件原行为。

### 4. 真实浏览器和交付

- [ ] 新建 browser_task12.py，复用隔离 HTTPS control、临时 master key/DB、真实登录 Cookie 和测试 provider；验证 A/B 个人覆盖/恢复继承、管理员默认更新/409、邀请单次展示/兑换、禁用/撤销后失效。
- [ ] 覆盖身份切换的旧 GET/保存响应及聊天中切设置页；用事件门禁，不靠固定 sleep 猜测。截图隐藏新输入凭证和邀请 token，只保留非敏感状态证据。
- [ ] 跑完整回归并显式路径提交，提交信息用中文。不得夹带主审文档；报告父/新 SHA、文件、红绿、API 形状与未执行门槛。完成后停止等待审计，不进入 Task 13。

验证命令（新测试文件须先由本任务创建）：

```powershell
$frontendTests = @(Get-ChildItem -LiteralPath tests/frontend -Filter '*.test.mjs' | Select-Object -ExpandProperty FullName)
node --test @frontendTests
& 'C:/Users/hyh/anaconda3/envs/Agent/python.exe' -m pytest tests/multiuser -q
& 'C:/Users/hyh/anaconda3/envs/Agent/python.exe' -m pytest tests/test_chat_question_replay.py tests/test_login_transport.py tests/test_web_security.py tests/test_agent_harnesses.py tests/test_harness_workspace.py tests/test_local_write_guard.py tests/test_web_model_config_parity.py tests/test_settings_stale_mirror.py -q
& 'C:/Users/hyh/anaconda3/envs/Agent/python.exe' tests/frontend/browser_task12.py
```

在 web/frontend 运行 npm run build 和 npm run lint；顺序复跑 Task 11 六份 browser_task11 系列脚本，保留既有 R2 关停警告的真实记录，测试资源由本次装配在 finally 清理。真实供应商/目标 Linux/正式发布门槛未执行者如实保留，不用 mock 结果替代。
