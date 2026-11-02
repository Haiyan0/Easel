# 原 Web Task 5C1：技能目录/执行与画像创建/增强

当前仅5C第一子组。父基线 `710d673bc1458c19e3f4d7301fc4d7680bd22fce`。
继续原worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。
必读 [总计划](../plans/2026-10-29-original-web-login-and-chat-repair.md)、[修复设计](../specs/2026-10-29-original-web-login-and-chat-repair.md)、[5B验收](../audits/2026-10-30-original-web-task-05b-accepted.md)、[运行保护清单](2026-10-29-original-web-running-baseline.json)。

5C按执行域分批：本组技能与画像；后续5C2账号/登录/分析、5C3发布/导出另行下发。目的仍是恢复原Web功能，不增加新页面或另造Agent平台。代码Agent实施、主会话审计；交付后停止。

## 1. 先盘点原调用与生命周期

原入口以实际代码为准：GET /api/skills、GET /api/skill/{name}、POST /api/skill；POST /api/profile/build、GET /api/profile/build/status/{name}。原组件SkillPage/SkillDrawer、ProfilePage/OnboardingWizard、WorkspaceApp与既有WorkspaceData context。

提交接口表：组件→请求字段/返回形状→control授权与任务分类→Run接纳/快照/令牌→gateway→worker业务→本人路径→状态/停止/恢复→测试。先按真实旧handler识别哪些是纯目录读、确定性画像基线写入、长Agent任务，不能把全部当普通HTTP代理。

现有control/tasks.py的skill/profile尚implemented=false；control/proxy.py只接受既有任务输入字段，worker已有task_type框架。要最小扩展必要适配并证明装配真实可达；不能仅翻implemented标志或直接转发旧run_agent_sync/daemon thread绕过容量账本。

## 2. 目录与画像基线

- 技能目录/详情来自固定只读code_root/skills，合法名称映射由服务端决定，客户端不能提交绝对路径、任意目录、命令或URL。防编码穿越、链接越界与原始宿主路径泄漏；目录查询不创建Run。
- apiConfigured/依赖状态只表达本workspace可用能力，不凭control宿主环境变量断言用户已配好，不返回真实Key、命令或本机.env。普通成员不能通过SkillDrawer的saveEnv分支写全局.env或安装底座；未支持的独立技能依赖如实不可用，不把缺配置伪装成功。
- 原画像表单确定性创建六维仍可用，文件/status在本人workspace；沿用5A真实目录/文件边界，含不存在目录首次创建、冲突、Unicode名字和链接目标，不覆盖已有画像或写code_root。
- 基线已创建与AI增强是否接纳/成功分别明确。容量满、模型未配、worker失败或增强失败时，不能虚报running/done，不能遗失已经合法创建的基线。并发同名创建不互相覆盖；状态查看/编辑/删除与进行中增强的交错有明确处理，不能删除后被迟到任务重新写回而不告知。

## 3. 长任务走既有受管Run

- skill/profile真实执行复用Store.reserve_run、受管配置revision、签名快照、broker和worker RunManager；任务类型与输入摘要绑定。新字段若有，control/worker摘要和验证同步，不能把正文任意拼装后脱离签名执行。
- 技能名映射固定目录；用户input与画像form是数据，不是shell参数。skill/profile任务只在本worker执行，control不启动Agent。复用同一有效模型/allowlist与凭证隔离，不拷贝用户.dsh/HOME或真实供应商Key。
- 任务稳定标识、权限/问答、SSE/轮询、停止、断线/刷新及终态归属明确；原组件可展示本任务结果/产物，不丢到无关联的隐式会话。允许复用已审对话/Run适配，不新建第二套执行账本。
- 重复请求/浏览器重试/进程重启不得重复派发prompt或增强；容量与lease按终态释放。接纳失败可明确重试，接纳结果不确定时先查原Run，不自动新建任务。
- 对同一画像的增强/编辑防止并发覆盖；需要的互斥只限实际资源，复用已有机制，别泛化重写调度。账号资源、平台登录、分析/发布仍保持未接通。
- 某技能缺少受管依赖或会触发尚未接通的账号/发布能力，原页面如实显示限制；不得用假目录就绪状态冒充所有技能真实验证。真实外部动作不在本轮测试授权中。

## 4. 原前端接线

SkillPage/SkillDrawer与OnboardingWizard/ProfilePage通过context/显式client适配，保留原主题/表单/展示；local保持既有行为。只启用实际支持入口，不重写页面。

目录/详情、表单、任务状态和产物引用均受identity epoch/Abort/归属保护；注销切换后旧响应/轮询/SSE不能提交到新用户树。失败保留可重试输入；重复点击不得双派发；刷新能找到原任务和明确结果。需权限确认时复用已审题卡边界，不因未知状态默许执行。5A/5B菜单能力和账号5C未接通说明保持真实。

## 5. 红绿、真实链路与交付

- 先新增行为红测：两用户目录可读但任务/画像/状态/同名产物各自隔离，401/CSRF/伪造目标拒绝；输入和服务端skill映射绑定；容量/模型未配/重复请求/停止/取消/重启恢复不重复prompt；错误与日志脱敏。
- 正常画像表单→基线可读→受控增强Run→状态/产物落本人根；失败/冲突/链接/并发、编辑删除与迟到完成路径实测。技能从原目录选择→受控执行→原结果/产物→停止/恢复；不能只测handler返回200或implemented标志。
- 原页面浏览器覆盖上述闭环、权限题卡/临时错误、跨身份迟到、深浅主题和移动布局；Node/build/lint、受影响后端任务/签名/worker/broker回归、5B五组、5A四组、Task3 33组、Task4八组保持。历史截图不覆盖。
- 隔离control/gateway/worker运行一次真实Harness+受控provider或假ACP的任务拓扑，分别标明真实层与fixture，证明任务账本/一次派发/本人文件/停止释放；不调用付费模型、真实平台账号/发布。只增量构建受影响镜像，不无缓存重建Harness/Chromium全栈。

证据放docs/evidence下original-web-task-05c1子目录，包含接口/任务表、红绿、签名与一次执行、原页面/隔离拓扑、not-run及清理。使用指定Agent Python，全新测试workspace/DB/HOME/假凭证，随机资源标签且清理前核归属。

用户运行实例/卷/socket/Key/Cookie/邀请保持不动，不重放用户prompt、不付费/真实发布、不全局prune或删历史网络。显式文件提交并列父/新SHA、文件职责；主审audit/task/plan/probe只读。完成停止等复审，不进入5C2/5C3/6、不自动上线；本组通过不代表所有技能或整个Task5已验收。
