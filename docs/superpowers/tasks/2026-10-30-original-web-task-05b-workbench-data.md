# 原 Web Task 5B：热点、选题、日历与概览数据

状态：5A 已验收，当前仅5B，由代码 Agent 实施、主会话审计。父基线 `7b9d76f73dd730f63b74a63f3cd2e81c9436dbef`。
继续原 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。
必读 [总计划](../plans/2026-10-29-original-web-login-and-chat-repair.md)、[修复设计](../specs/2026-10-29-original-web-login-and-chat-repair.md)、[5A验收](../audits/2026-10-30-original-web-task-05a-accepted.md)、[运行保护清单](2026-10-29-original-web-running-baseline.json)。

## 范围与接口表

复用原 TrendsPage、IdeasPage、CalendarPage、DashboardPage 和已建立的 WorkspaceData context；local保持原行为，control绑定本人 workspace。先登记实际组件→method/path/query/body→handler→policy→gateway签名→存储→返回形状→测试的对照表，再先红测、最小接线。以实际代码为准，已定位入口：

| 数据 | 原 API |
|---|---|
| 热点 | GET /api/trends?platforms=...&limit=... |
| 选题 | GET/POST /api/ideas；PUT/DELETE /api/ideas/{iid} |
| 日历 | GET/POST /api/schedule；PUT/DELETE /api/schedule/{sid}；GET /api/schedule/context?days=... |
| 概览 | 原 DashboardPage 汇总上述数据及5A outputs，不额外编造不存在的 overview API |

热点收藏为选题、选题加入日历及原页面“做内容”跳转接通；跳到聊天仍通过既有会话/Run接纳，不能自动提交或重复发送 prompt。日历这里只管理记录与摘要，不启动自动发布、scheduler或外部动作。

DashboardPage 当前还直接读取 accounts/analytics；它们属于5C，本组明确显示未接通，不能发旧 local 账号请求、伪造空账号/零指标冒充真实数据。技能执行、画像 AI 构建、账号登录/分析/发布保持禁用，不提前扩大本组。

## 后端隔离与真实数据

- 复用5A WorkspaceApi、正式受管 revision、RuntimeManager和 request lease；不另建 worker 调度器，不为数据CRUD伪造聊天Run。
- control只按已认证身份选workspace；写入保留CSRF/Origin。gateway逐条显式method/path/query/body签名与白名单，不开放通配代理或客户端上游URL。
- id、platforms、limit、days和JSON字段按实际合约校验、有界；限制请求大小与超时，错误保留明确语义。不允许用字段选数据根、外部命令、任意文件或URL。
- _ideas.json、_schedule.json及摘要必须来自本人workspace，不能沿镜像code_root/宿主路径。同名/同id的A/B记录隔离；编辑/删除/重启后持久化不串用。已有内部文件仍不能经5A产物接口公开。
- 检查存储路径链接、损坏JSON、读写失败与并发读改写；实际错误不能当空列表成功返回，不能覆盖坏文件而丢原数据。原子写与必要锁复用/最小补齐，不重构全仓存储。
- calendar context当前调用固定calendar_ops.py读取指定数据文件：检查worker内脚本及本人数据路径真实可达，固定命令/有界days，不能由浏览器输入任意参数；不调用付费模型或发布。
- 热点是固定来源的公开数据。盘点真实网络访问与现有出口约束，只允许原受限来源，不借此开放worker任意网络/URL或修改全局代理。测试用受控来源；网络不可达时原页面明确失败或标注旧数据，不能将fixture结果宣传为公网已通过。
- 公共热点缓存可以复用非私人数据；选题、日历、摘要、Dashboard私人状态必须按身份隔离。共享只读目录不得被用户CRUD改写。

## 前端与验收

- 原组件通过context/显式client适配，不引入模块全局当前用户或第二套页面。盘点所有直接fetch；身份切换Abort/epoch隔离、清空旧数据与编辑态，迟到响应不能写入新身份。
- 原布局/主题沿用；每页可区分加载、真实空数据、失败和未接通。提交失败保留可重试编辑内容，不假成功、自动重发或静默吞错。概览只启用本组与5A真实能力。
- 红绿覆盖真实临时Store+worker数据：A/B同id CRUD、未知id404、401/CSRF、伪造目标/内部头、恶意query/body、并发更新、重启持久化、损坏存储/链接边界、固定摘要路径、热点上游失败。
- 真实原页面浏览器：热点→收藏选题→编辑状态→加入日历→编辑/删除→刷新与概览一致；“做内容”只导航/预填，确认前无新prompt。A慢响应期间注销登录B不能显示A数据；成员不请求admin/accounts/analytics。浅/深色及移动布局沿用。
- 相关后端/gateway/存储回归、frontend Node/build/lint、5A四组、Task3 33组、Task4八组保持；新5B矩阵单列。浏览器证据输出本组目录，不改历史截图。
- 一次隔离control/gateway/worker数据链路，用假材料证明CRUD/摘要/重启持久化；按需要增量构建受影响镜像，不重建Harness/Chromium、不无缓存全栈。热点公网/真实模型均可明确not-run，不拿受控来源冒充。

## 交付与边界

使用指定 Agent Python。测试workspace/DB/HOME/凭证全新，容器/网络/卷随机标签，清理先核归属。用户运行实例、原卷/socket、Key/Cookie/邀请保持不动，不重放prompt、不付费、不真实发布、不全局prune、不删历史网络。

证据放docs/evidence下original-web-task-05b子目录，含接口表、红绿、A/B/持久化/浏览器和隔离容器结果、明确not-run及清理记录。显式文件提交，列父/新SHA与文件职责；主审audit/task/plan/probe只读。完成停止等复审，不进入5C/6、不自动上线。5B通过也不代表整个Task5完成。
