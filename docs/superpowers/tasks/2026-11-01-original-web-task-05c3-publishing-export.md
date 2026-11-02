# 原 Web Task 5C3：原发布工作台与稿件导出

> 实施者使用 superpowers:executing-plans，先盘点与红测，最小接通后提交等待主审。不自动进入 Task 6 或上线。

父基线：`fcb351d532301d22afe3be0c91373b19f3505ca7`。继续原隔离 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`，Python 固定 `C:\Users\hyh\anaconda3\envs\Agent\python.exe`，PowerShell。开工先核对 HEAD/干净状态，不接管 root 未提交改动。

目标：原 PublishingPage、ManualPublishPage、PublishPage 在 control 使用本人稿件/素材/账号与受管任务，恢复已有人工导出及策略允许的自动发布能力；local 保持原行为。不另造发布页面，不把“导出成功”宣称为“平台已发布”。

必读：[总设计](../specs/2026-10-29-original-web-login-and-chat-repair.md)、[总计划](../plans/2026-10-29-original-web-login-and-chat-repair.md)、[5C2验收](../audits/2026-11-01-original-web-task-05c2-accepted.md)、[运行保护清单](2026-10-29-original-web-running-baseline.json)。主审audit/task/plan/probe、README与运维手册只读。

## 1. 先盘点原接口与副作用

- [ ] 从原三页面、lib/api.ts、web/app.py 发布区、easel/manual_publish.py及平台脚本提交接口表：publishing-policy GET；manual-publish/{platform}/export POST；publish/{platform} POST、status GET、sms POST；现有内容适配/AI检查/素材选择/复制/草稿/日历回流分别调用什么、是否起Agent/浏览器、结果与路径是什么。
- [ ] 盘点发布策略真实来源、管理员与成员权限、原自动发布允许条件；control不能读取宿主.env代替受管策略，也不能让成员请求自行启用自动发布。保留原策略默认值与人工/自动双路径：政策不允许时明确禁用，允许时才可使用受管执行。不要未经说明删掉自动功能来缩小验收范围。
- [ ] 明确所有旧全局发布进程/状态/短信文件、profile锁、账号配置、日历记录、前端稿件持久缓存；control均归本人workspace/identity。不能通过重设模块全局变量复用多个用户数据。

## 2. 红测与后端适配

涉及 control/tasks/policy/HTTP装配、必要发布适配模块、gateway精确method/path/query/body签名、worker固定业务/Run执行、原发布与导出逻辑。沿用既有Run/lease和5C2浏览器锁，不新建第二套任务系统。

- [ ] 先用隔离两用户/临时存储/固定受控runner写行为红测：入口未接通、跨用户产物/账号/状态拒绝、策略拒绝先于容量/执行、重复请求不双提交。保留父基线红测与命令退出码。
- [ ] 发布/导出均按原任务分类处理容量和执行归属；实际起浏览器/子进程的发布必须经Store.reserve_run、签名快照、RunManager与终态。目录/状态读取不得起外部任务；写入/启动动作要求身份、Origin/CSRF和严格字段上限。publish类型只在真实业务装配后启用，不能单改implemented标志或直接放行旧后台线程。
- [ ] 输入绑定平台、正文、标签、本人画像和素材；固定平台/脚本白名单，客户端不能传命令/任意URL/绝对路径。原媒体数量/类型/空稿件规则保留。签名与执行之间素材变化必须有明确策略，拒绝根外链接、私有账号/密钥/内部状态文件充当媒体；不能从同源下载URL任意反解宿主路径。
- [ ] 导出固定本人outputs内的稿件/素材ZIP，通过已审本人媒体下载；解包检查只有目标稿件与指定素材，没有路径穿越/宿主绝对路径/凭证。缺文件/坏存储/首次目录链接/并发导出受控失败，不覆盖他人包，失败只清理本次临时产物。published始终false，复制/导出不启动浏览器或外部发布。
- [ ] 自动发布有稳定requestId/Run归属与一次派发；双击、超时、断连、刷新、worker重启后查原任务，不能自动重发不确定外部动作。接受请求不等于平台成功；无法确认外部结果时明确未知/待核查，不伪造success、也不擅自补发。Run停机只代表停止本地执行，不承诺撤销已发生的外部发布。
- [ ] 状态/二维码或短信只绑定当前本人平台Run，拒绝旧任务续传；复用5C2停止/取消/超时的子进程树清理与profile锁，和校验/分析/登录/注销互斥。不要根据共享平台旧status结算新Run。实际成功后再记录日历，重复回调不重复记录；日历失败与外部成功分别如实表示，不因日历失败重发平台内容。

## 3. 原前端接入

- [ ] 三页面通过WorkspaceData适配，保留主题/布局/人工审核、预览、复制、导出、策略选择及原自动确认步骤；control不直接落local client。内容适配/AI检查复用已审chat/skill任务与模型/权限/停止，不额外绕开Run。
- [ ] 草稿、审核记录、媒体选择、发布结果与轮询按identity/workspace隔离；control不能读取旧全局publish draft缓存。内容/素材/平台变更后当前审核失效，不能把A已审核稿件带给B或把旧审核用于新版本。
- [ ] 身份401卸载；403政策/权限和503暂不可用如实显示。旧POST/stop/SMS/轮询回应在切平台/关弹窗/新任务/注销后不得写新状态；暂时查询失败可恢复而不创建新任务，终态停止轮询。人工“已发布”记录注明用户确认，不能当作系统已验证平台结果。

## 4. 验收与证据

- [ ] 后端红绿覆盖身份/CSRF/签名精确路由、平台/媒体/正文限制、两用户同平台隔离、同账号锁、策略禁用、一次派发、短信归属、取消/超时/重启/未知结果、导出边界、日历去重与失败；秘密和上游原文不进入事件/日志/包。
- [ ] 新增 tests/frontend/browser_original_web_task05c3.py：原发布工作台→素材/草稿→人工审核→导出并实际解包/下载；编辑后重新审核、人工日历草稿/确认；策略禁用与允许的两条页面路径；受控自动发布→短信/停止/查询失败恢复/刷新找原任务；A/B迟到、重复提交次数、浅/深/移动。不要仅测试固定成功响应。
- [ ] Node/build/lint、受影响后端账本/签名/worker/发布/导出/日历测试（原test_manual_publishing.py、test_web_publisher_assets.py及前端publishingPage/publishPageLifecycle按影响选跑）；5C2六组、5C1七组、5B五组、5A四组、Task3 33组、Task4八组保持。既有失败或平台跳过单列。
- [ ] 隔离容器增量构建受影响镜像，受控本地平台/固定runner实际启动镜像内浏览器/子进程；证明一次提交、本人账号/profile/导出文件、停止/超时释放与重启不重放。原平台入口哪些实际执行、哪些替身单列；检查镜像源码哈希、清理归属，不无缓存重建全栈。

证据目录（实施时创建）：docs/evidence/original-web-task-05c3/。接口表、红绿命令、浏览器、解包检查、容器镜像/实际层、not-run、清理均纳入受跟踪文件；检查git ls-tree与文档引用，不能只在本地留被忽略的.log。历史截图不覆盖。交付父/实现/证据SHA、精确文件职责后停止等待复审。

## 5. 外部动作边界

本组只授权实现与隔离受控测试，绝不调用真实平台发布/短信/真实凭证、真实付费模型；不操作用户运行容器/卷/socket/Key/Cookie/邀请，不复制本机HOME/.dsh/.env/账号状态，不全局prune。受控平台成功不等于真实平台通过；真实操作须后续明确授权。不得因无真实账号而阻塞受控实现。5C3通过后仍由主会话下发Task6，不自动合并、更新用户容器或上线。
