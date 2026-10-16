# Easel 邀请制多用户工作区设计

日期：2026-10-16（Asia/Shanghai）

状态：已确认的聊天设计已整理成书面规格，待文档审阅；未授权开始产品实现。

配套计划：[实施计划](../plans/2026-10-16-multiuser-workspaces.md)。

## 1. 用户目标与已确认决策

将已经通过 ACP 接通本地 DeepSeek Harness 的 Easel，扩展为邀请制小团队服务。用户彼此可信，但每个人的文件、画像、会话、账号登录态和执行环境独立。管理员统一管理 Agent 底座及能力默认配置，普通用户通过现有 Web 模型配置体验设置自己的能力模型覆盖。

已确认的产品约束：

- 一个用户拥有一个私有 workspace；workspace 持久化在服务器文件系统。
- 一个 workspace 可以有多个 Harness 会话，原生会话管理继续由 Harness 承担。
- 管理员管理底座、ACP 启动配置、工具策略与默认模型服务；普通用户不能修改这些受管配置。
- 语音、配音、图像、视频、音乐等现有能力使用管理员默认配置；允许用户设置个人覆盖，清除覆盖后恢复继承。
- 登录身份、模型调用凭证、社交平台账号凭证分别管理。
- 统一登录入口；每用户一个独立 Easel 运行单元，单元内运行 Harness、Shell、技能和浏览器。
- 本轮只交付 spec 和 plan；实现需在审阅实施计划后另行开始。

## 2. 第一版边界与工程默认值

这是一个可整体验收的多人 MVP，按后述任务递增交付。普通用户现有内容工作流保留；不要求一次重写整个 Easel 后端。

工程默认值（可在部署前调整，不代表已经确认服务器环境）：

- 生产目标为单台 Linux 服务器，Docker Engine；优先 rootless，必须实际验证资源限制生效。Windows 保留现有本地模式，容器集成验收在 Linux 执行。
- 复用 Python >=3.10、FastAPI、HTTPX、cryptography、React/TypeScript；控制元数据使用 Python 标准库 sqlite3，启用外键、WAL、busy_timeout 和显式事务。
- 服务模式为 local / control / worker；默认 local，禁止 worker 因配置缺失回退为无认证 local。
- 多人模式第一条验证路径为管理员受管的 DeepSeek Harness ACP；其他现有底座保留 local 能力，不自动宣称经过多人隔离验收。
- 一个用户一个私有 workspace，无共享空间、开放注册、SSO、协作编辑、Kubernetes、多机调度、精确货币计费或自动跨用户迁移。
- 内置代码、依赖、规则与技能只读；多用户模式不开放用户安装底座插件、任意修改命令或导入未经审核的可执行配置。
- 默认每 workspace 最多 2 个活跃 Agent/技能任务，整机最多 8 个；同一会话仅 1 个 prompt；同一账号持久浏览器 profile 仅 1 个操作者。上限可由管理员调整。
- 单任务默认上限 1800 秒；worker 无活跃任务且无活跃请求后空闲 900 秒可回收；正在执行、等待权限确认或发布中的任务不计为空闲。
- 每 worker 初始预算 2 CPU、4 GiB 内存、256 个进程、1 GiB /tmp；持久区默认 10 GiB，采用独立受配额文件系统或经过验收的目录项目配额。配额无法生效时禁止启用多人模式。
- 登录会话闲置 24 小时失效、绝对有效期 7 天；邀请令牌首次兑换有效期 7 天、单次兑换。工程默认值均可由管理员收紧。

管理员默认凭证不可下发用户进程是正式验收条件；并非只在 Web 中隐藏字段。为此包括一个范围受限的 provider broker，见第 8 节。

## 3. 当前代码依据

以下为 2026-10-16 工作区观察，包含用户尚未提交的 ACP 修改；实施前须保留并复核这些改动，不得覆盖。

| 现有位置 | 观察 | 设计影响 |
|---|---|---|
| web/app.py:81、212、428 | profiles、outputs、浏览器 HOME、.env 为实例范围 | worker 启动时固定本人的数据路径 |
| web/app.py:505 | Origin/Host 防护不等于用户认证 | 新增真正认证，保留防跨站写入 |
| web/app.py:2017 | 模型保存接口会修改 .env，部分路径同步 OpenClaw | 多人模式不得进入这些全局配置写入分支 |
| web/app.py:2928 | ACP 请求 cwd 使用项目根 | 分开代码根、工作区根与受管启动配置 |
| easel/harnesses/store.py:81 | binding 记录 runtime/native_id，不记录应用用户 | 增加应用会话归属，并绑定固定 workspace |
| easel/harnesses/rpc.py:43 | 私有 stdio 池按会话复用 | 保留实现，增加 workspace 命名空间 |
| web/frontend/src/lib/store.ts:71 | 固定 localStorage 键 | 登录前不读私有缓存，用户切换取消旧连接 |
| skills/shared/scripts/model_registry.py | 已有模型能力与供应商注册表 | 扩展原注册表的多人覆盖与代理能力，不另造第二份目录 |
| skills/builtin/skill-wechat-publisher/scripts/wechat_token.py （第 22 行） | token 缓存写脚本目录 | 迁入本 workspace 的账号状态区 |

## 4. 部署与信任边界

浏览器只访问一个 HTTPS 站点。TLS 反向代理将请求交给 control。control 负责登录、角色、配置、会话索引和授权路由。它不执行用户技能，不直接运行 Agent。

每个 workspace 的 worker 使用同一应用镜像，包含原有 Easel Web 后端和 Harness 适配器。worker 仅处理本 workspace；控制层根据登录记录选择 worker，不接受浏览器指定容器、目录或上游地址。worker 的就绪状态须返回预期 workspace_id，匹配后才能路由。

宿主机上的 provisioner 管理容器生命周期、挂载和资源预算；仅此进程具有容器管理权限。控制 API、provider broker 与用户 worker 均不挂载 Docker socket。provisioner 使用受限本地 Unix socket，仅接受 workspace_id 和受管镜像/配置版本等结构化请求，拒绝原始 docker 参数、任意挂载和任意命令。

每个 worker 使用独立网络，禁止访问其他 worker、宿主管理服务、控制面管理地址与云元数据地址。仅允许必要的入口代理、provider broker 和经限制的公网出口。不得把所有 worker 放在一个可互访的默认 bridge 网络后声称隔离完成。worker 不发布宿主端口；control 通过内部路由连接。

worker API 校验每实例独立的内部服务凭证和固定 workspace 绑定；该凭证只允许访问本人的 worker，不赋予 control 管理权限。所有外来身份/转发头由 control 移除后重建，不信任用户提交的角色头。内部凭证即使被用户工具读取，也不能访问其他 workspace。

受管底座与任务配置使用 control 的 Ed25519 私钥签署快照；worker 只读持有公钥，校验 workspace/run/version/有效期与 admission nonce。掌握本人 worker 的内部凭证不能伪造管理员命令或工具策略；重放相同 run 的快照只返回原任务状态。签名不替代 broker 对真实 Run 和路由范围的服务端校验。

rootfs 只读、非 root 用户、移除 Linux capabilities、no-new-privileges，保留正常浏览器进程沙箱，不使用 privileged 或默认关闭 Chromium 沙箱。容器共享宿主内核，不作为面向恶意公网租户的虚拟机级保证。

## 5. 身份、角色与登录

### 5.1 最小资源模型

```text
User(id, display_name, role[admin|member], disabled_at)
Workspace(id, owner_user_id UNIQUE, state, config_version)
Invite(id, user_id, token_digest UNIQUE, expires_at, redeemed_at, revoked_at)
Login(id, user_id, token_digest UNIQUE, csrf_digest, created_at, last_seen_at, expires_at, revoked_at)
ManagedRevision(version, public_json, encrypted_secrets, created_by, created_at)
WorkspaceOverride(workspace_id, channel, encrypted_json, version)
Conversation(workspace_id, id, title, persona, archived, created_at, updated_at)
Run(workspace_id, id, conversation_id, client_request_id, input_digest, status, config_version, heartbeat_at)
Audit(id, actor_id, workspace_id, action, target_id, outcome, created_at)
```

每表有真实主键、外键和事务约束。Conversation 的主键是 (workspace_id, id)，Run 对 (workspace_id, client_request_id) 唯一。所有查询显式限定 workspace_id；UUID 生成不能替代授权。worker 原生会话绑定保存在本 workspace，控制层无需复制原生 transcript。

### 5.2 身份引导与会话

首次管理员通过仅能在服务器执行的 CLI 引导，不提供无认证的远程管理员初始化接口。CLI 创建用户和邀请令牌，令牌使用 secrets.token_urlsafe(32)；SHA-256 摘要入库，明文仅创建时输出一次。数据库中不得保存登录 token 明文。

POST /api/auth/exchange 接收令牌（请求体，不是 URL），事务内单次兑换。设置 __Host-easel_session Cookie，Secure、HttpOnly、SameSite=Lax、Path=/，不设置 Domain。登录会话 token 与邀请 token 不相同。失效或重复兑换返回统一 401。用户需要登录其他设备时由管理员签发新的邀请。

GET /api/auth/me 的 JSON 字段为 userId、workspaceId、role、capabilities、csrfToken；不返回登录访问凭证。POST /api/auth/logout 撤销当前登录。禁用用户或撤销该用户登录立即阻止新请求，关闭该登录已有的 SSE；正在执行的后台任务保持持久状态，用户禁用另由管理员选择取消任务。

所有浏览器写请求校验精确 Origin 和 X-CSRF-Token；登录兑换也检查 Origin。SameSite、TrustedHost 和本地 write guard 是补充，不能代替认证。登录失败按来源与令牌摘要限速，10 分钟最多 10 次，返回 429/Retry-After；日志不记录请求体或 token。

### 5.3 权限

member 可管理本人的内容、画像、平台账号、会话与允许覆盖的能力模型；不可修改底座、原生主聊天模型、ACP argv、sandbox、自动批准策略、工具安装、全局环境、默认配置或 provider broker 路由。

admin 在独立 /api/admin/* 接口管理上述受管配置、用户和默认能力服务。管理员进入普通工作台时仍使用自己的 workspace。第一版不提供切换成其他用户或跨空间浏览内容的功能；服务器运维权限不等于产品提供的代理登录功能。

## 6. workspace 文件与运行状态

生产布局示例（新目录，不自动搬迁当前用户数据）：

```text
/srv/easel/control/control.sqlite3
/srv/easel/control/master.key
/srv/easel/managed/revisions/<version>/       # 管理员配置快照
/srv/easel/workspaces/<workspace_id>/
  profiles/
  outputs/                                  # 内容与兼容的会话事件文件
  state/                                    # worker 索引、平台配置、任务租约
  home/.dsh/                                # 原生会话、记忆
  home/.easel-browser-profiles/              # 按平台/账号保存
  home/.cache/
```

worker 内固定 /opt/easel 为代码根、/workspace 为本人数据根、/home/easel 为本人 HOME。EASEL_CODE_ROOT、EASEL_WORKSPACE_ROOT、EASEL_STATE_ROOT 分别明确这三类位置；现有 EASEL_ROOT 作为工作区兼容入口，在多人模式不指向可写源码。脚本和规则用代码根解析，产物/画像用工作区解析，临时环境与账号配置用状态根解析。

DSH_HOME 为本人的 Harness 状态目录，DSH_AGENTS_HOME 为显式挂载的规则/技能目录。DSH profiles、底座 patch 与共享 Agent 规则只读；会话数据写入对应可写子目录。显式配置优先级不能指回宿主 HOME。启动前测试原生 Harness 能在这种挂载下创建、续接会话。

平台凭证、broker 临时凭证、配置文件和内部事件不作为公共产物暴露。旧接口下载 outputs 内部下划线目录时必须按用途拒绝；上传/删除路径同时防范绝对路径、目录穿越、符号链接和 Windows junction。多人模式关闭从服务器任意路径采用文件的 upload/local 接口。

worker 状态：absent → starting → ready → draining → stopped；另有 failed。启动按 workspace 加互斥，重复请求共用一次启动；超时 60 秒返回 503 + Retry-After，不路由到别人的可用实例。回收只删除可再建容器，绝不删除持久挂载。进程异常退出后将残留 running 任务标为 interrupted，等待用户显式重试，不自动重复发布。

## 7. 会话、任务和重连

由服务端创建 Easel 会话 ID。浏览器可以带 client_request_id 实现重试幂等，不能指定 native_id。原生绑定至少记录 workspace_id、easel_session_id、runtime、native_id、创建时底座版本；本人的 native_id 可以与另一 worker 相同，不产生共享索引。

内部键使用 workspace_id + easel_session_id，覆盖进程池、锁、停止标记、问题卡、事件文件和连接回放。已有每会话私有 ACP stdio 复用保留；不改造为跨用户共享 Harness 进程。底座配置更换后旧会话不静默迁移或重新绑定；需要兼容的受管历史版本，否则给出明确不可续接提示。

run-scoped broker token 或配置版本变化会触发现有 RPC 签名校验重建子进程；此时必须 resume 同一 native_id，不能为追求进程复用扩大 token 作用范围。V1 接受这一启动开销，不承诺每轮都复用同一个 ACP 进程。

Conversation 在控制层保存列表元数据。服务端会话快照保留用户消息、最终回答和附件引用，不记录推测性未完成输出为已完成历史；实际 transcript 与原生会话仍各司其职。多设备列表和最终对话历史以服务端为准。localStorage 仅缓存，不能决定归属。

任务路径：授权会话 → 原子创建 Run/获取并发租约 → 解析配置快照 → 调用对应 worker → 记录绑定/事件 → 完成或中断 → 释放租约。相同 request_id 和相同输入返回原任务，相同 request_id 但不同输入返回 409。浏览器断开不重发 prompt；SSE 按 run_id/event_id 恢复。停止和权限回答检查 run 与问题归属，外空间资源统一 404。

同一 SSE 每 5 秒检查登录撤销、用户禁用和 run 归属；禁用或撤销后最多 5 秒关闭流。网关不缓冲 SSE、不自动重试任务 POST；HTTPX 上游响应在正常结束、异常和客户端断开时都关闭。

## 8. 管理员默认配置与个人覆盖

### 8.1 继承规则

唯一能力目录继续来自现有 model_registry。普通用户允许覆盖 speech（映射 registry voice）、image、video、music 中已注册供应商；不包含 chat、runtime、command、plugins、sandbox、自动批准和任意环境变量。

配置对象包含 channel、provider、model、base_url、protocol、secret_ref、options、source[admin|user]、version；协议专属双 Key 等凭证作为同一个 secret bundle 管理。禁止把不同来源的 URL 与 Key 逐字段拼接。

- 未配置覆盖：继承最新管理员默认路由；管理员原始 Key 不发送到 worker 或浏览器。
- 保存完整个人覆盖：校验供应商必填项后，以用户凭证和服务信息组成完整路由。
- 只覆盖模型/非敏感参数：可引用管理员同一受管路由，broker 仍限制模型集合，不暴露其密钥。
- 更换 URL/provider/protocol：必须提供完整个人凭证 bundle，不继承管理员或上一个服务地址的 Key。
- key 留空且个人路由完全不变：保留该个人路由已有凭证；留空不能表示复制管理员凭证。
- DELETE 覆盖：恢复继承；禁用能力是独立管理员策略，不用空字符串表达。
- 管理员默认更新只影响之后启动的任务；个人完整路由不变，引用默认路由的模型覆盖使用最新默认版本。每任务记录实际配置版本。

默认/个人秘密以 AES-GCM 加密保存在控制数据库，master.key 只对 control/broker 服务账号可读，不挂载 worker；AAD 包含配置作用域与记录 ID。用户完整 BYOK 路由也通过 broker 调用，避免同一套功能出现两条权限行为不同的路径。

### 8.2 Provider broker 的必要边界

broker 是按受管路由转发模型请求的窄接口，不是任意 URL 代理。worker 获得绑定 workspace/run/channel/配置版本、1800 秒过期且可撤销的 opaque 调用凭证。上游目标及认证方式由服务端查表确定，忽略客户端提供的上游 Authorization、Host、转发头和路由范围。

第一版实现 DeepSeek ACP 主模型调用与现有 image 的 openai 供应商（OpenAI-compatible 协议）、speech OpenAI-compatible TTS/ASR 的路由适配，包含 JSON、multipart、二进制响应、SSE 及必要模型列表调用。现有无密钥本地能力保持可用。video/music 等注册供应商保留个人覆盖和默认配置能力，但每个带管理员凭证的供应商在多人模式启用前必须通过同一 broker 适配契约验收；无适配器时返回 capability_unavailable，并在界面显示原因，不回退为明文 Key 注入。发布默认配置时拒绝将无适配器的在线供应商标记为可用默认。

该范围不删除已有 local 供应商；多人模式供应商支持表必须如实标明受支持的协议，扩展适配器是独立增量任务。

上游只允许管理员批准的公网 HTTPS 目的地址。个人地址保存、自检、列模型、实际调用均走相同出口策略：拒绝回环、私网、链路本地、元数据、其他 worker、control 和 provisioner，覆盖 IPv4/IPv6、DNS 重绑定和跳转目标。默认不跟随重定向；连接阶段通过受管出口代理实施目标校验，不能只在字符串保存时检查一次。确有内网供应商时由管理员登记精确例外，普通用户不能扩大例外。

记录请求次数、并发、提供商 usage（若有）与失败，不把估算当精确账单。并发额度原子检查，超额 429。worker 内可访问本人的短期 broker token，因此它限定用户预算而非防止本人消费；不得声称能按工作区外的真实服务 Key 约束用户自有外部消费。

## 9. API 和界面兼容

control 在同一站点提供登录壳与构建后的前端。公开路由仅登录壳、静态版本化资产、auth/exchange 和不含内部详情的 health。其他路由全部认证。动态数据统一 Cache-Control: no-store。

| 接口范围 | 多人模式处理 |
|---|---|
| /api/auth/* | control 处理身份 |
| /api/admin/users、invites、managed-config、workers | control 管理员接口 |
| GET/POST /api/sessions，PATCH/DELETE /api/sessions/{id} | control 的本人会话目录 |
| GET /api/sessions/{id}/messages | 本人服务端消息快照 |
| /api/settings/models、save、available、selftest | control 权限/覆盖服务，保留现有 UI 请求形状 |
| DELETE /api/settings/models/overrides/{channel} | 清除本人覆盖 |
| GET /api/settings/harness | 返回管理员受管配置的脱敏摘要和 locked=true |
| 写 harness、chat transport、env、local-agents、gateway restart、工具安装 | 普通用户 403，不能穿透原单实例接口 |
| chat/stream、jobs/stream、last、stop、question/answer/status、session 删除 | control 校验资源后路由固定 worker |
| persona、platform login/logout、publish、analytics、schedule、ideas、uploads、outputs | 全部路由本人的 worker，保留路径校验 |
| upload/local、未知 worker 路由、非白名单 HTTP 方法 | 多人模式明确拒绝 |

新增路由必须在显式 policy registry 分类，默认拒绝。读取模型设置只返回 configured/source/脱敏值，不允许导出管理员秘密。剪藏扩展现有特殊通道默认禁用，后续需独立限定 workspace 的扩展凭证后开放。

前端启动先获取 auth/me，再挂载私有页面和读取命名空间缓存。所有用户相关 localStorage、sessionStorage、BroadcastChannel 均包含 user_id/workspace_id；包括发布草稿、活动会话、画像、onboarding 与谁登录过平台的缓存。退出清空内存、对象 URL、AbortController 和 SSE；跨标签用独立登录事件通道通知重新取身份，不传秘密。旧无用户键不自动导入多人账户。

设置页显示“使用管理员默认配置”或“使用个人配置”，提供恢复默认；member 的底座展示只读，不出现安装/重启/切换操作。admin 管理页与个人配置页使用不同请求路径，避免管理员无意将个人模型保存成全局默认。

产物 HTML/SVG 不直接作为应用同源可执行文档。V1 以下载或严格 sandbox iframe 预览；预览响应使用 CSP sandbox 且不含 allow-same-origin/allow-scripts，阻止顶层跳转；需要脚本的产物仍可下载在用户自己的环境打开。防护以响应头为准，不能只包一层可被直链绕过的 iframe。

## 10. 错误、恢复与运维

- 401：登录无效；403：已登录但管理操作不允许；404：本空间不存在的资源（含其他空间）；409：任务幂等冲突、版本冲突或会话忙；413/507：上传/空间预算；429：并发/调用预算；503：worker/broker/配置不可用。
- 配置更新按 If-Match 版本比较，冲突返回 409，不丢失其他管理员的修改。配置版本在发布前校验，不允许半套凭证生效。
- SQLite 为控制元数据唯一写入口；单 control 进程、多异步请求，阻塞数据库操作在线程中执行。容器生命周期与租约由数据库约束/互斥管理，不依赖单个 Python dict。
- 发版先停止接纳新任务，再等待在途任务；超过任务期限标记 interrupted，显式通知用户。备份 control SQLite 一致性快照、master.key、配置版本和各 workspace 持久区；不能只备份 outputs。
- 恢复演练使用新目录与新 worker，验证会话续接、个人覆盖解密与平台 profile 文件完整。Cookie 平台侧过期时正常要求重新登录。
- 默认不自动导入当前开发者的 .env、HOME、浏览器目录或输出。迁移命令要求显式源、目标 workspace、dry-run 清单、目标为空和备份；保留原数据不动。原生会话可能绑定 canonical cwd，路径不兼容时只导入静态内容并报告不能续接的会话，不能假装完成历史迁移。
- 日志包含 request_id、workspace_id、run_id、配置版本和结果；不记录令牌、模型 Key、Cookie、完整请求体或用户内容。退出不删除持久任务；注销用户的数据清理由后续显式管理流程完成。

## 11. 验收标准与覆盖编号

| 编号 | 必须观察到的行为 |
|---|---|
| A01 | A/B 使用各自邀请登录，独立 workspace；无登录不能访问任何私有 API |
| A02 | 邀请单次兑换、会话到期/撤销、CSRF 和角色校验真实生效 |
| A03 | 伪造身份头、目录、worker 地址不能访问其他空间；worker 无直接外部端口 |
| A04 | 同名文件、相同 Easel/native session ID、并行任务仍相互独立 |
| A05 | 外空间的停止、权限回答、历史、SSE、下载均 404；撤销已有 SSE ≤5 秒关闭 |
| A06 | member 直接调用受管配置/安装/环境接口仍 403；通过文件上传不能改 ACP argv |
| A07 | 默认继承、个人覆盖、恢复默认、配置版本快照正确；跨用户和跨服务凭证不串 |
| A08 | 管理员 Key 不在 Web、worker env/文件/原生配置/日志中；broker 无效范围和过期 token 被拒绝 |
| A09 | 真实浏览器临时状态隔离，账号 profile 持久化且串行；共享技能目录可只读执行 |
| A10 | 同一浏览器切换账号/跨标签退出，旧缓存与旧流不回填新用户；跨设备会话可读取 |
| A11 | worker 并发冷启动只创建一次；回收后历史可续接；异常中断不自动重复发布 |
| A12 | 真实容器 CPU/内存/PID/空间限制生效；不能访问别的 worker、控制面或元数据地址 |
| A13 | multipart、二进制、SSE broker 转发正常；私网/IPv6/重定向/DNS 重绑定被出口阻止 |
| A14 | 同一请求重试不重复执行；任务/模型并发限制和租约恢复有效 |
| A15 | 产物预览不能执行应用同源脚本或泄漏内部配置；非法路径/符号链接/junction 被拒绝 |
| A16 | 现有 local 回归通过；双用户假 Harness 与 Linux 真实 DSH ACP/浏览器验收通过 |
| A17 | 备份在新目录恢复并演练成功；迁移不修改原用户目录 |

## 12. 计划分段与实现门槛

实施计划覆盖：基础数据与认证 → 配置继承/broker → worker 路径与运行隔离 → 会话路由与 UI → 运维及真实双用户验收。中间任务可独立测试，但不得在全部隔离门槛通过前把半成品作为多人服务开放。

计划审阅重点：Linux 容器部署假设、V1 供应商适配范围、邀请单次兑换的登录体验、默认配额、旧原生会话路径兼容。它们均已给出可执行默认行为，实施者不应擅自扩大范围或降低隔离要求。

## 13. 资料

- [DeepSeek ACP 多会话和续接](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/acp/acp/README.md)。
- [DSH_HOME 与 DSH_AGENTS_HOME](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/util/home-paths/README.md)。
- [Docker rootless](https://docs.docker.com/engine/security/rootless/)、[容器运行与资源约束](https://docs.docker.com/engine/containers/run/)。
- [HTTPX 异步流与响应关闭](https://www.python-httpx.org/async/)。
- 本仓库现状：[底座接入](../../agent-harnesses.md)。工作区另有未提交的 docs/skills-profile-multiuser-handbook.zh-CN.md，作为本地调查参考，不作为这两份文档提交的依赖。
