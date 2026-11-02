# 原 Web Task 5C2：账号、平台登录与分析

> 实施者使用 superpowers:executing-plans；先盘点、红测，再最小实现，提交后停止等待主审。不得自动进入 5C3/6 或上线。

父基线：`654305a69c1d68d6b157b608dbd2c7d5f96d6298`。继续现有隔离 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`，开工先确认 HEAD 与干净状态。Python 固定 `C:\Users\hyh\anaconda3\envs\Agent\python.exe`，PowerShell。

目标：在原 AccountsPage 与 DashboardPage 恢复本人平台账号、登录和分析能力，local 行为保留；control 必须经过身份、精确签名、worker 与既有 Run/浏览器资源互斥，不能直通本地全局 handler。

必读：[总设计](../specs/2026-10-29-original-web-login-and-chat-repair.md)、[总计划](../plans/2026-10-29-original-web-login-and-chat-repair.md)、[5C1验收](../audits/2026-10-31-original-web-task-05c1-accepted.md)、[运行保护清单](2026-10-29-original-web-running-baseline.json)。root 主审任务/审计/计划/probe、README 和运维手册保持只读，不复制旧版本覆盖 root。

## 1. 接口与资源盘点

- [ ] 先提交接口表到本组证据目录。逐项列原组件/请求/返回、是否起浏览器或联网、身份与 CSRF、任务类型、容量接纳、固定脚本及本人路径、状态/停止/重试、凭证处理、测试。以实际 handler 为准，不能按 GET/POST 推断是否只读。
- [ ] 原调用从 `web/frontend/src/lib/api.ts`、AccountsPage、DashboardPage、`web/app.py:4933` 起的账号/分析区追踪到脚本；精确登记 `/api/accounts`、`/api/accounts/{platform}/whoami`、credentials GET/POST、mp-login POST/status GET、`/api/login/{platform}` POST/status GET/sms POST、`/api/logout/{platform}` POST、`/api/analytics/platforms` 与 `/api/analytics/{platform}` GET。查询参数含 force/restart 的语义必须明确，未知参数/平台不能透传任意脚本或 URL。
- [ ] 查明旧全局 LOGIN_PROCESSES、LOGIN_DIR、whoami/analytics cache、公众号配置、biliup cookies、浏览器 profile 与 HOME 的真实依赖。生产受管路径只能落本人 workspace；不能仅改模块全局变量后多用户共享。

## 2. 后端最小适配与红绿

涉及文件以盘点为准：`easel/control/tasks.py`、policy/HTTP装配、workspace_api/workspace_tasks 或职责明确的新适配模块；gateway 精确路由；worker HTTP/Run 执行与 `easel/worker/browser.py`；原脚本复用其业务能力。禁止为本组重写调度或另建进程内任务账本。

- [ ] 先用隔离 DB/workspace/假执行器写失败测试，证明当前 control 入口未接通、旧全局存储及浏览器启动不能直接复用。保留基线红测日志。
- [ ] 纯目录、快速状态、读取已有分析走签名短请求；whoami 强制校验、登录、mp-login、分析刷新及凭证校验等实际启动浏览器/子进程/联网的工作，经既有接纳/容量/lease/终态机制。注销、短信续传和停止绑定本人现有任务/平台，不冒充新 Agent prompt。不能只将 implemented 改 true。
- [ ] 分清 analytics 缓存读取与真正刷新，平台固定白名单。不同 workspace 同名平台不得共享任务或结果；同账号并发登录、校验、分析、注销按既有跨进程 profile 锁协调。GET 起任务同样必须绑定身份并考虑 CSRF/重复请求，必要时为 control 增加显式动作接口，由适配层转换原 UI 调用，local 兼容。
- [ ] 登录稳定任务标识与一次派发：重复点击/超时重试不新起第二个浏览器；显式 restart 先停止并等待旧进程树退出，再启动；短信只续传当前本人待短信任务，过期/旧任务拒绝。未知状态不能当 success。关闭弹窗与停止任务的语义分别明确；停止/取消/超时/进程退出/重启后状态和容量均收尾，不遗留浏览器或复活已注销登录态。
- [ ] profile、cookies、二维码、status、分析缓存与公众号配置均使用本人安全根；含首次创建、symlink/junction/reparse、损坏文件、注销删除边界。二维码只经已审本人媒体入口，不能公开任意文件。浏览器 profile 锁须与脚本同协议，删除 profile 不得删除锁对象而绕过互斥。
- [ ] 平台凭证不是全体默认配置。AppSecret/Cookie/SMS 等不得回传、写入普通 Run 事件/输入快照/错误日志、前端持久缓存或公开 outputs；状态只返回掩码。明确受管凭证存储位置、权限和保护方式，复用现有保护机制，不能把旧宿主 YAML/.env 搬进用户目录冒充接通。验证失败不覆盖有效旧配置；日志保留可诊断的阶段/类别，脱敏不可只靠测试夹具。
- [ ] 分析失败不能伪造零数据或更新时间；旧缓存可显示但明确 stale 与真实取数时间，未知数字保留 null。无登录/无依赖/平台不支持给真实状态，不能凭 control 宿主环境宣称已登录。

## 3. 原页面与身份边界

- [ ] AccountsPage、DashboardPage 经 WorkspaceData context 适配，保留原布局、扫码/短信/凭证表单和主题；control 不再直接调用 local client。仅在后端实际支持时启用 accountsEnabled，发布/导出入口仍保持未接通。
- [ ] whoami 的全局 localStorage 与 `easel_analytics` 缓存不得在 control 跨身份复用；可以不持久化，或严格按 identity/workspace 分区并在撤销时清理。A 请求迟到不能覆盖 B；401 撤销私人页面，403/503/断网保留错误语义。
- [ ] 登录与分析轮询串行、有界、可取消；暂时 503 后能恢复，不因一次错误永远停查或无限吞错。终态后停止轮询；切换平台/关闭弹窗/卸载/换用户不写旧状态、不重复启动。恢复查询只找原任务，不自动重放外部动作。页面加载不应无约束地为所有账号同时起浏览器。

## 4. 验收矩阵与证据

- [ ] 后端至少覆盖 A/B 同平台隔离、401/CSRF/伪造目标、参数边界、容量满、同平台互斥、重复接纳、restart 与旧迟到、SMS 归属、停止/超时/worker 重启、注销不复活；凭证失败保护与脱敏、缓存失败/过期、链接越界与坏存储明确失败。
- [ ] 新增 `tests/frontend/browser_original_web_task05c2.py`：原账号列表→假二维码→扫码/短信→成功昵称头像→刷新恢复；公众号两种登录状态分清；分析→旧缓存/失败→重试；停止、401、一次503恢复、A/B迟到与前端缓存隔离、浅/深色/移动布局。断言后台执行次数，不能只断言文案。受控站点/runner 实际写本人产物并由页面读取，不能固定返回 success。
- [ ] Node 全量、build/lint、受影响 control/gateway/worker/browser/profile/账号/分析测试与 5C1七组、5B五组、5A四组、Task3 33组、Task4八组回归。既有测试如 `test_login_start.py`、`test_login_transport.py`、`test_account_whoami.py`、`test_web_analytics.py` 按实际影响选跑，平台跳过和既有失败单列，不伪称全仓通过。
- [ ] 隔离 control/gateway/worker 跑一次受控浏览器任务闭环，证明 Run 接纳、一次派发、本人 profile/二维码/结果与停止释放；区分真实浏览器、假平台、假执行器。只增量构建受影响镜像，复用现有 Harness/Chromium 层，不无缓存重建全栈；按测试标签清理本轮容器/网络/卷。

证据目录（实施时新建）：docs/evidence/original-web-task-05c2/，包含接口表、红绿命令与退出码、任务/路径/互斥证据、浏览器截图、隔离拓扑、not-run、清理。列父/实施/证据 SHA 与精确文件职责；不要覆盖历史截图。

## 5. 执行边界

仅 5C2。不得操作用户当前 control/worker/gateway/sidecar、卷/socket、Key/Cookie/邀请，不复制本机 HOME/.dsh/.env/账号状态；不重放用户 prompt、不付费、不访问真实平台登录或提交真实短信/凭证、不发布、不全局 prune。受控夹具可测试扫码/短信/凭证完整流程，真实账号动作留待另行授权验收。不得因缺少真实账号阻塞受控实现，或用夹具通过宣称真实平台已通过。

实施提交后停止等待主审，主会话独立审查代码、复跑测试和反例，再裁决通过或下发定点返工。不进入 5C3/6，不自动合并、更新用户容器或上线。
