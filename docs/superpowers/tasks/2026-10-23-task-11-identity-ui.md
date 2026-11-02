# Task 11：邀请登录、身份隔离与服务端历史接入执行计划

> 执行方式沿用外部代码 Agent。使用 superpowers:executing-plans，先红测试再实现，完成后停止主审，不进入 Task 12。用户已委托主审下达下一批指令，无须再次选择执行方式或提供服务器。

**Goal：** 将现有前端接入 control 邀请登录、身份作用域缓存和 Task 10 admission/SSE/历史 API，切换账号后旧身份的数据与异步回调不可回填。

**Architecture：** 启动先确定服务模式；control 模式先验证 auth/me，再挂载身份绑定的应用。身份 epoch 管理请求、SSE、缓存和对象 URL 生命周期，服务端会话目录/最终快照为事实来源；local 保留原工作流。

**Tech Stack：** 现有 React/TypeScript/Vite、Node test runner 与仓库 TS transpileModule 测试装载器、FastAPI/HTTPX；浏览器验证使用现有可用 Chromium/Playwright。

**Spec：** [原规格](../specs/2026-10-16-multiuser-workspaces-design.md) §7/9、[原计划 Task 11](../plans/2026-10-16-multiuser-workspaces.md)、[Task 10 验收](../audits/2026-10-23-task-10-accepted.md)、[执行补充](../specs/2026-10-22-local-docker-development-gates.md)。

## 基线、范围与文件职责

- 基线：e4ffc83c685762b94bdc063d7a6200c35b861725。先核对 HEAD/分支/工作区，变化先说明关系，不 reset 或暂存主审 plan/spec、审计、任务单、Harness 基线。
- C:/Code/Easel，PowerShell；Python 固定 C:/Users/hyh/anaconda3/envs/Agent/python.exe。沿用本机已装 Node/npm 和依赖，报告实际版本，不为测试引入第二个 bundler 或无关升级。
- Task 2/9/10 代码依赖已验收；Task 8 真实部署门槛仍未通过。本批本地浏览器研发，不改安全边界，不要求目标服务器或真实供应商。
- 本批实现登录、身份生命周期、目录/历史与聊天协议接入。Task 12 的管理员配置/个人模型设置界面、Task 13 的平台适配及 Task 14 正式发布不纳入。

创建：

```text
web/frontend/src/lib/auth.ts
web/frontend/src/components/LoginPage.tsx
tests/frontend/multiuserAuth.test.mjs
tests/frontend/multiuserHistory.test.mjs
```

按需要修改：web/frontend/src 的 App.tsx、lib/api.ts、lib/store.ts、lib/whoami.ts、ChatPage.tsx、QuestionCards.tsx，以及实际使用用户缓存/裸 fetch 的页面。可抽出精简 ControlApp/身份管理模块，避免给庞大 App 全部逻辑堆条件；不无关重写 local 全站。

后端必要装配：easel/control/app.py、policy.py、sessions.py、store.py 及对应测试，仅用于公开静态前端启动信息与本人 Run 目录读取。不得为前端便利扩大 worker 权限或接通未验收业务。

## 核心接口与协议

保留原计划接口：

```typescript
type AuthContext = {
  userId: string; workspaceId: string; role: 'admin' | 'member';
  csrfToken: string; capabilities: string[];
};
loadIdentity(): Promise<AuthContext>;
logout(): Promise<void>;
identityCacheKey(ctx: Pick<AuthContext, 'userId' | 'workspaceId'>, name: string): string;
class IdentityEpoch {
  begin(ctx: Pick<AuthContext, 'userId' | 'workspaceId'>): number;
  invalidate(): void;
  isCurrent(epoch: number): boolean;
}
```

- control 身份：POST /api/auth/exchange，GET /api/auth/me，POST /api/auth/logout。HttpOnly Cookie 由服务端设置；csrfToken 仅当前身份内存持有，不复制登录 token 到 Web Storage。
- 目录：GET/POST /api/sessions，PATCH/DELETE /api/sessions/{id}；GET /api/sessions/{id}/messages 分页读取最终消息，服务端生成会话 ID，删除/归档沿用软删除语义。
- 任务：POST /api/chat 或 /api/chat/stream 返回 202 JSON admission，随后 GET /api/chat/jobs/{runId}/stream。旧 streamChat 的“POST 直接读 SSE”仅 local 保留，control 必须区分两阶段。
- client_request_id 仍通过 turnId 提交；admission 后持久恢复键保存服务器 runId，与浏览器生成的幂等键分开。错误/断线不能自动再次 POST prompt；同输入重试需保持原 turnId，仅由明确恢复策略或用户操作触发，不能新造请求键重复执行。
- SSE 事件实际使用 accepted/running/awaiting_permission/done/failed/interrupted/cancelled 的 state 记录及 token/thinking/activity/question/error。按真实协议处理终态，不能只等待旧 done 事件；传输 EOF 不等于任务成功，最终消息以服务端快照确认。
- 停止、问题回答/状态需要 runId；options.value（opaque）作为提交值，label 仅展示，保留 local 旧题卡兼容。不得提交别题的 value 或从展示文本重造授权值。

换设备与刷新恢复需要只读 Run 元数据：新增精确 GET /api/sessions/{id}/runs?limit=&offset=，先按 actor 校验会话，返回 {runs:[{runId, conversationId, state}], nextOffset}；limit 默认 50、上限 200，稳定分页。基于现有 Run 账本读取，不返回 token、nonce、内部地址、签名包或配置秘密，不创建任务。外空间/未知会话 404。用它发现本人正在执行的任务并只连接回放，不能靠缓存中的 pending 状态决定服务器是否仍执行。

## 全局约束

- control/local 模式由服务端可信启动信息或显式开发构建配置确定；未知值失败关闭。不得把 auth/me 的 401/403/503/网络异常解释成 local，再展示旧缓存。
- control 的 /、/onepage、assets/static 只提供固定前端构建目录的公开文件与必要模式信息，保持精确路由与路径边界；不暴露整个项目、输出、HOME 或配置。构建缺失明确失败，不读取任意请求给出的服务器路径。
- control 首次 auth/me 未完成前，不挂载私人页面、不读取用户内容缓存、不调用私人业务 API；无会话显示邀请登录，服务故障显示重试，不闪现历史。没有公开注册，不调用管理员 bootstrap 自动建账号。
- fetch/SSE/上传/题卡/裸 fetch 使用统一同源身份请求层。写请求添加内存 CSRF，FormData 不手填 multipart boundary；Cookie 采用同源 credentials，不使用 URL 中的 token。401 结束当前 epoch，清理并回登录；旧 epoch 迟到的 401 不得把刚登录的 B 踢下线。
- 每次身份校验成功/重新登录都建立新 epoch，不以同 user/workspace 就复用旧 epoch。异步请求在开始时捕获 ctx/epoch，写 React、localStorage、sessionStorage、whoami、对象 URL 前再次校验；仅 AbortController 不足以保证旧结果不提交。
- 退出立即停止当前身份的 UI 活动、fetch/SSE/轮询并清空内存/对象 URL，即使远端 logout 失败也不再展示私人数据。撤销请求使用失效前捕获的当前 CSRF，独立于被取消的业务请求，不能因清理 epoch 而把这次 logout 本身误中止。明确报告远端撤销失败，不谎称 Cookie 已清除或所有会话已退出。服务端任务不因浏览器退出默认取消。
- content BroadcastChannel 名称带 user/workspace。独立全局登录事件通道仅通知重查身份，不携带 token、csrf、消息内容或平台凭证；同源 Cookie 跨标签共享，账号切换后其他标签必须重新验证。
- 本地旧无作用域键只在 local 读取/迁移；control 不自动认领、导入或删除用户旧 local 历史。浏览器缓存仅加速，不能决定所有权或覆盖服务器 assistant 历史。
- Task 10 的 auth/me capabilities 当前是 workspace/admin 权限分类，不代表所有业务已实现。control 本批优先显示聊天/历史与退出；未装配 skill、平台登录、发布、analytics、头像画像等入口明确未开放，不自动触发 503 轮询，不把 member 带到 local env/harness 设置。具体设置界面留 Task 12；local 原有功能保留。

## Review Focus：五类真实验收风险

1. A 的 SSE/fetch/401 在 B 登录后返回：不得修改 B 状态、缓存、错误栏或登录状态。
2. 同浏览器多标签共享 Cookie：一标签退出/换身份，其他标签不得继续展示或回填原身份内容。
3. 两阶段 admission 与断线：202 必须解析为 admission，重连只 GET 原 runId，不能误发第二个 prompt。
4. 换设备/清缓存：目录和最终历史来自服务端，活跃 Run 从本人元数据发现；缓存损坏不能变成新会话或空历史覆盖。
5. local/control 切换和服务故障：control 不回退旧本地缓存，local 历史及原流式/删除逻辑不退化；私有响应 503 不被显示为“没有历史”。

## 实施步骤

### 1. 身份 epoch、请求层与缓存清单

- [ ] 先写红测试 old_identity_cannot_commit_after_switch：A begin→invalidate→B begin，A 的延迟 token/fetch/401/定时回调不能提交；同一 A 退出再登录也有新 epoch。
- [ ] 写请求层测试：写 API 带 CSRF、同源 Cookie；401 只使所属 epoch 失效；403/503 保留明确错误语义，不触发 local 降级。
- [ ] 实现 auth.ts 和统一请求生命周期，读取模式的边界明确；纯工具模块测试沿用现有 transpileModule 装载方式，不为测试导入私有 UI 状态。
- [ ] 盘点全部用户内容存储：sessions、active session、tab claim、pending Run/event cursor、publish draft、onboarding、whoami、analytics、model lists 与对象 URL。逐项使用可无歧义组合的 user/workspace key；内存 inflight/version 表也按身份隔离。主题等无用户内容的设备偏好可保持全局，并在交付清单说明。

### 2. 登录壳与 control 静态入口

- [ ] 写 auth/me 尚未完成、401、服务 503、兑换成功/失效/已用邀请等测试；登录前私人页面和私人 API 调用计数为 0。
- [ ] 实现简洁中文 LoginPage，沿用当前视觉样式，提供邀请兑换与明确错误。邀请明文只在表单短暂存在，成功后清空，不写日志或永久缓存。
- [ ] App 分离身份壳与私人工作区，identity epoch 变化时卸载旧工作区并清理副作用。补齐 control 固定构建目录静态装配及模式信息；local 构建/服务行为保持。
- [ ] 退出与跨标签事件先使本地身份失效，再执行受保护 logout/重查；测试旧回调不能恢复旧账号显示。

### 3. 服务端目录、历史与聊天状态

- [ ] 写多分页目录、创建、重命名、归档、删除与活跃 Run 冲突测试。control 不使用前端 generateId 作为服务器 session ID，失败不伪造本地成功。
- [ ] 实现本人 Run 元数据窄路由、真实 actor/CSRF/policy 回归和稳定分页；复用 Store 查询与专用 DB 线程。
- [ ] 接入目录/消息分页；换设备与清缓存重新加载能看到服务端相同数据；读失败明确展示，不将未知/未加载状态当作 [] 保存。
- [ ] 改造 control 聊天为 submit→admission→SSE，捕获 epoch、持久 runId/eventId；断线/重载只回放。终态后读取服务端最终快照，避免 token 乐观显示与最终消息双份追加。
- [ ] 停止和问答携带当前 Run 归属；题卡使用 opaque value，已解决题卡重放不再次可答。每个 Run 独立 pending 状态，同 workspace 两会话流并发不串。

### 4. 其余缓存与不可用功能

- [ ] 针对 whoami/analytics 等在途结果测试开始时捕获身份、完成前校验；不能以写入时的“全局当前身份”选择 B 的 key 来保存 A 的结果。
- [ ] 改造 sessionStorage、发布草稿、onboarding、跨标签 claim 等真实调用点，不只写无人使用的 identityCacheKey helper。clear/logout 不调用 localStorage.clear 破坏 local 历史与其它账号/应用数据。
- [ ] control 不挂载尚未支持的私有业务页面/自动探测；必要入口显示未开放或 503 原因。不要通过实现平台业务 relay、管理员配置 UI 来扩大本批。

### 5. 实际浏览器与回归

- [ ] Node 行为测试覆盖上述五类风险及原 sessionHistory/whoamiCache；不要仅以源码包含字符串测试代替执行。
- [ ] 本机真实浏览器、隔离临时 profile/数据库/worker：真实 HTTPS control 登录 A→创建会话→一次实际 ACP 夹具任务→刷新只回放→退出→登录 B；B 不见 A 内容，跨标签更新生效。测试 HTTPS 可用临时证书与隔离测试上下文，不能把 TLS 忽略、安全 Cookie 降级或凭证写入正式配置。
- [ ] 用受控延迟保留 A 的 fetch/SSE，再切 B，完成 A 响应后验证 B UI/存储不变。使用新浏览器上下文模拟换设备，服务端目录/消息一致；401/503 情况无旧内容闪现。
- [ ] 至少一次本地 local 回归冒烟，确认旧会话读取/继续/删除保留；不操作用户真实 local 历史或平台账号。

## 验证命令与交付

先运行新测试确认红；最终顺序执行：

```powershell
node --test tests/frontend/multiuserAuth.test.mjs tests/frontend/multiuserHistory.test.mjs tests/frontend/sessionHistory.test.mjs tests/frontend/whoamiCache.test.mjs
# 其余既有前端测试也全部运行，沿用项目已有装载器
$frontendTests = @(Get-ChildItem -LiteralPath tests/frontend -Filter '*.test.mjs' | Select-Object -ExpandProperty FullName)
node --test @frontendTests
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser -q
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/test_chat_question_replay.py tests/test_login_transport.py tests/test_web_security.py tests/test_agent_harnesses.py tests/test_harness_workspace.py tests/test_local_write_guard.py -q
Set-Location C:\Code\Easel\web\frontend
npm run build
Set-Location C:\Code\Easel
git diff --check
```

另实际运行浏览器用例，记录 browser 版本、精确入口、登录/重连计数、截图位置与临时资源清理。若浏览器或 HTTPS 测试环境暂时不可用，如实报告为未执行，不以 Node mock 或截图替代实际交互验收。

交付父/新 SHA、显式文件清单、逐步红绿、缓存/网络入口改造清单、真实 HTTPS 浏览器证据、Node/后端/构建结果、local 兼容与仍不可用功能。只显式路径提交必要实现、测试与产品说明，中文提交信息；不夹带审计/任务单/主审文档。

完成后停止等待审计，不进入 Task 12，不宣称 Task 8 或正式多人部署通过。
