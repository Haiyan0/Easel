# 原 Easel Web 接入登录与聊天修复设计

日期：2026-10-29。基线：`1a8811096c7c86bb9079a5c88b92cd225d1907b1`。
状态：用户于 2026-10-29 明确开始任务，主会话负责审计；首项执行单已建立，用户运行实例先保留，不自动切换部署。

## 用户目标

登录后使用 Easel 原本的 Web，不交付另一套精简 Web。保留原来的品牌、主题、侧栏、工作台、对话、技能库、内容库、账号、画像及其工作台子页。登录页仅作为原应用的入口，视觉沿用原 Web。修复当前消息发送后失败的问题，并让管理员能查明真实原因。

授权更新：允许开始计划内的代码实施与审计；主会话负责审计和容器使用裁决。现有用户实例保留作现场/对照，代码与诊断验证使用隔离测试环境。真实供应商调用、外部发布或用户实例切换另行明确安排，不把“开始任务”解释为允许重放用户 prompt 或读取真实 Key。

## 当前证据与不能混淆的结论

1. `web/frontend/src/App.tsx:78` 明确分流：control → ControlApp，local → LocalApp。原 Sidebar/ChatPage/DashboardPage 等只在 LocalApp 使用。截图是独立 ControlApp 的实际 UI，不是部署参数错误。
2. ControlApp/LoginPage 大量内联深色背景和紫色按钮，未复用原主题变量。原样式位于 styles/index.css、styles/theme.css，已有浅色/深色、品牌字体、图标、圆角和渐变。
3. 当前部署已经保存 chat v1：provider=deepseek、model=deepseek-flash、protocol=openai，允许 deepseek-flash/deepseek-v4-pro。最近一轮任务状态 failed。与之前“未配置而 503”已不是同一状态。
4. 受管 `deploy/multiuser/managed/acp.patch.yml` 和模型目录仍固定 deepseek-chat；本机 ACP profile 覆盖为 deepseek-official/deepseek-flash，本机 Harness checkout 已不同于容器固定版本。已证实模型配置来源不一致；不能把此疑点直接写成截图异常唯一根因。
5. `easel/worker/runs.py:1372` 将所有普通异常转换成“请检查本机 CLI、登录和模型配置”；RPC 的 stderr 当前直接排空丢弃。因此本轮只读日志不足以确定失败发生于 ACP 启动、模型选择、broker 校验还是供应商响应。必须先补可脱敏诊断和确定性复现，再修真正触发点。
6. 旧页面直接使用 /api/status、personas、outputs、accounts、skills、schedule 等接口；control 只装配其中一部分，其余 503。直接把控制模式切到 LocalApp 会触发旧全局缓存、缺 CSRF、协议不匹配及未装配 API，不能作为修复。

## 目标实现

### 同一套页面，按模式替换数据访问

保留一个前端构建和一棵主界面组件树。App 先确认服务模式；local 保持原有单机行为，control 先完成身份校验，再挂载复用原布局的 WorkspaceApp。登录前不能初始化旧私有缓存或调用私有 API。

复用原 Sidebar、ChatPage、MessageBubble、QuestionCards、DashboardPage、OutputsPage、AccountsPage、ProfilePage、SkillPage、SettingsPanel、SubNav 等。将 LocalApp 的界面与数据副作用解耦，不复制整份 JSX 另造 MultiuserApp。ControlApp 只可临时保留其已验证协议逻辑，迁移完成后删除独立布局；复用 lib/auth.ts、lib/control.ts 等纯协议工具。

新增 typed workspace client 接口，分别实现 local/control 适配器。control 使用服务端会话目录、Run/SSE/问题卡/停止和身份命名空间；local 使用原有 API 与历史兼容格式。组件只消费共同的视图数据，不知道 Cookie、签名或原生会话 ID。不要将浏览器的 native_id 当授权依据。

所有旧 request、流式 fetch、上传、原生 media URL、文件预览/下载都须纳入身份与错误处理检查。登出/401/切换账号先取消请求与 SSE，再清理当前身份视图；迟到响应以 userId/workspaceId/epoch 拒绝。401 回登录，403 保持权限语义，503 显示服务原因，不能默默退回 local。

### 聊天的模型与凭证

Harness/ACP 继续承担 Agent 执行，不改成普通 API 问答来让测试通过。管理员 chat 的版本化有效路由决定该 Run 的模型、允许模型和公开能力目录；同一份已验证配置必须进入 broker token 与签名任务快照，不能用户页面 flash、worker 默认 chat。

容器受管 profile 固定 provider easel-broker；模型目录与 ACP model 选择来自可信配置生成，不从用户可写 HOME/profile 继承管理员命令。优先使用现有 ACP 的 session/set_config_option 或 session/set_model 能力；确切 modelId 格式须以锁定 Harness 的实际可选目录验证，不能臆造。新会话使用新版本，既有 Run 仍固定旧快照；旧 native 会话不兼容时明确说明，不改绑为新会话掩盖失败。

管理员 API Key 仍只在 control 解密；worker 用逐 Run token。不会复制本机 .dsh、.env、账号令牌到用户容器。当前使用的 DeepSeek 模型先通过真实 Harness + 受控 provider 验证，再在用户批准的真实通道做一条小请求验收。

### 原功能的后端接线

为旧 Web 每个实际 API 建立 method/path/参数/归属/返回结构/任务类型对照表，不能仅展示菜单。复用 policy、worker guard、gateway 精确签名和原 worker handler；补显式 control 授权路由和所需 gateway 白名单，不增加任意 URL/路径转发，不让 control 执行技能或接管 Docker。

功能分组按依赖接入：

| 组 | 原页面能力 | 完成定义 |
|---|---|---|
| 基础工作区 | status、画像、会话、内容列表/预览/下载/上传 | 原组件可用，A/B 路径及缓存隔离 |
| 工作台数据 | 热点、选题、日历、概览 | 写入本人 workspace，重启后可恢复 |
| 执行与账号 | 技能、画像构建、平台登录/状态/分析、发布/导出 | 使用已有 Run/容量/权限/停止，浏览器账号按 workspace 隔离 |
| 设置 | 个人覆盖、受管聊天、管理员成员与邀请 | 融入原设置界面，成员不能改受管命令或读取管理员 Key |

中途未接通的能力可以显示有原因的不可用状态，但这不计整个“恢复原 Web”完成。原先确实只在 local 支持的第三方底座/插件安装，不因 UI 复用就向普通成员开放宿主权限；应显示管理员受管语义或明确原权限边界。

### 登录页视觉与交互

使用既有 Easel 图标与品牌字样，移除 EASEL CONTROL 产品名。沿用 Bodoni Moda 品牌字体及 Manrope/Noto Sans SC 正文栈，不加载新字体或另一套 UI 框架。

复用 `--bg/--surface/--border/--text/--accent-gradient/--radius-*`；浅色为纸白、雾蓝与淡桃色，深色沿用原 slate 与青蓝强调，不硬编码另一套紫黑配色。布局为居中窄卡片、简短引导、单一主要登录按钮；移动端自然收缩，原 ThemeToggle 可在登录前切换并延续到工作台。共享主题偏好不是私有业务数据。

沿用邀请登录，不默认新增账号密码/SSO。文案明确“邀请仅能使用一次”，已使用/过期时提示重新获取，不叫可复用验证码；管理员重新签发流程有可见帮助。说明清楚即可，不在产品 UI 展示 Cookie、broker、Docker 或文件路径等实现细节。

包含可见 label、键盘回车、焦点样式、提交中/失败/重试/429 状态；错误颜色与主题一致，contrast 和移动端布局实际验收。非授权异常不能以“邀请失效”误导用户。

## 验收与边界

- 登录后是原 Easel 布局和原页面组件，主题切换连续，不能是 ControlApp 换个标题。
- 同一原组件树支持 local/control；未登录无私有闪现，A/B 切换无旧缓存/SSE/附件串用。
- 真实 Harness 的新建、追问、权限允许/拒绝、停止、重启续接都通过；模型名与本次受管快照一致，错误有可追踪原因。
- 原页面功能按对照表逐项通过，默认 stub/503 不算通过；真实发布/付费/第三方登录不自动执行，独立确认其验收方式。
- 日常启停、备份、恢复、升级联锁回归通过。生产 Linux 门槛仍保留，不用本轮 UI 结果替代。
- 本轮仅设计/计划。现有卷、密钥、会话和未提交文档不动，不重建正在运行的实例。
