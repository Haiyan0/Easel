# Task 6A 隔离整体验收记录

日期：2026-11-02。父基线/输入提交：
`4382180862e28d43f2974e32a618093c847ec262`。
实施提交：`51aacfb505d14e2e6b18498569ff8740bff6f51b`。证据提交：本目录所在提交（见交付说明）。
实施 worktree：`.claude/worktrees/original-web-task1-diagnostics`。

## 0. 本轮裁决（未整体通过）

本轮 6A **不能宣称整体通过**，原因与已完成部分如下：

- 已完成并通过：manual 发布策略（manual 403/0 Run、登记重启保留、迁移拒绝、
  Stop/清理）；主部署聊天/权限题卡/停止/刷新、工作台 API/页面入口、
  素材预览/下载/上传、人工审核 ZIP 导出、A/B 身份隔离、真实双 worker、
  worker 重启不重放、登出撤销与一次性邀请；frontend build/lint、Node 190、
  API/runtime 71、desktop operations 107。
- `stage:pages` 在主部署中因 Playwright 选择器命中侧栏隐藏
  `<option>测试画像</option>` 而记为 FAIL（测试侧误报，非产品失败）；
  交付脚本已改为等待 `.profile-summary` 可见卡片。该选择器修复后的
  定向页面复跑未在本轮完成。
- 正式备份/恢复/升级：A7 演练在恢复后首次 Start 触发
  `publish_mode_migration_required`。这是 5C3 策略迁移守卫的预期拒绝；
  演练脚本已为 Start 显式传 `-PublishMode automated`，但生命周期重跑
  未在本次会话完成，故 backup/restore/upgrade 记为 **not-run/阻塞**。
- 受控账号/发布整体演练未执行（not-run）；5C2/5C3 的分组容器证据仍有效，
  但不能替代 6A 的整体演练。
- 产品修复已按 TDD 红→绿：并发首屏 `worker_start_superseded` 有限重试、
  `/api/status` gateway 状态以 control 句柄为准、同 workspace ensure 串行化。
  这些修复解决了主部署首屏/工作台读取的 503 竞态；其余阻塞需主审裁决。

## 1. 环境与输入

- Windows 11 Pro，Docker Desktop Linux containers（Engine 29.2.1），WSL
  `Ubuntu-26.04`，Python `C:\Users\hyh\anaconda3\envs\Agent\python.exe`。
- 最终输入盘点见 [inventory.md](inventory.md)；镜像与源码逐文件 SHA-256
  核对结果见 `image-verification-*.json`。
- 本轮为便于定位，分阶段运行 `main`/`manual`/`controlled`/`lifecycle`；
  每次均使用同一最终镜像与脚本。可用
  `--phases lifecycle,manual,main,controlled` 一次复现；各阶段证据已合并到
  本目录。
- control：`sha256:c6677fd8d1fc2dd3d69e8df1f698568f7b280990c2659558866629ac43c62308`
- worker：`sha256:6f62af472c8b272c0d67a1aec26284cff8f8c9adacfaad48e6ba0ce64fbf0612`
- gateway：`sha256:0d30939d375c1e3bef616678f5e8d7b6fad687143817f7d6183579ca8e881f0a`
- 受控平台 worker：`sha256:caf564cc0e0954741fc5aab9ee54f354001794896e806baf93d4b0c4fa6dc7d3`

worker 使用“当前源码叠加到已验收 Harness runtime”的增量方式，`/opt/dsh`
固定为 `687f282e8625c6b8d11ff58211a90524be2e4317`；未使用只有生命周期的
`base`/`execution` 阶段冒充 runtime。

## 2. 阶段结果

原始检查由 `tests/multiuser/task6a_integration_drill.py` 写入
[checks.json](checks.json)；下表为同文件摘要。`PASS` 表示该断言在本轮
实际运行并通过；`FAIL` 记录在 checks.json（软阶段记录后继续，不中止）。

| 检查 | 结果 | 细节（截断） |
|---|---|---|
| main_status_ok | PASS | network=easel-6a-main-6ce50b97-control state=present  volume=easel-6a-main-6ce50b97-control state=present  volume=easel-6a-main-6ce50b97-keys state=present  volume=easel-6a-main-6ce50b97-egress state=present  control=ru… |
| main_publish_mode_automated | PASS | network=easel-6a-main-6ce50b97-control state=present  volume=easel-6a-main-6ce50b97-control state=present  volume=easel-6a-main-6ce50b97-keys state=present  volume=easel-6a-main-6ce50b97-egress state=present  control=ru… |
| http_chat_two_users | PASS | {"a": 1, "b": 1} |
| chat_permission_card | PASS | 执行授权 Agent 请求工具权限（仅本次）： 1. Allow once 2. Reject 自行输入… 提交 |
| chat_stop_terminal | PASS | cancelled |
| chat_refresh_same_session | PASS | same session resumed |
| stage:chat | PASS | None |
| stage:workspace_api_after_chat | PASS | None |
| workbench_dashboard_entry | PASS | 原工作台概览入口已渲染 |
| workbench_trends_entry | PASS | 热点页面已渲染；外部热点源按 not-run 记录 |
| workbench_seeded_idea | PASS | [{"id":"6a-idea-a","title":"6A 初始选题","note":"初始备注","source":"manual","status":"pending","created":1}] |
| workbench_seeded_schedule | PASS | [{"id":"6a-schedule-a","title":"6A 初始排期","date":"2026-11-07","platform":"微博","time":"10:00","status":"scheduled","note":"","kind":"content","url":"","source":"manual","event_type":"","end_date":""}] |
| media_preview_download | PASS | [{"name":"项目","type":"dir","path":"项目","mtime":1793557970,"children":[{"name":"clip.mp4","path":"项目/clip.mp4","kind":"video","mtime":1793557970,"size":31,"type":"file"},{"name":"封面.png","path":"项目/封面.png","kind":"image"… |
| media_bytes | PASS | 200 |
| material_upload | PASS | [200, "{\"ok\":true,\"files\":[{\"id\":\"90671b7f9b7601d5d8d57ddf\",\"name\":\"6a-upload.txt\",\"path\":\"_inbox/55391c3b62f69e1c3bb7/20261101-9a19f0/6a-upload.txt\"}]}"] |
| stage:pages | FAIL | TimeoutError: Locator.wait_for: Timeout 20000ms exceeded. Call log:   - waiting for get_by_text("测试画像").first to be visible     43 × locator resolved to hidden <option value="测试画像">测试画像</option>  |
| manual_export_zip | PASS | {"names": ["body.txt", "manifest.json", "media/01_封面.png", "tags.txt", "title.txt", "使用说明.txt"], "manifest": {"platform": "xiaohongshu", "title": "6A 人工稿件", "tags": "6A,集成", "reviewed": true, "exportedAt": "2026-11-01T1… |
| stage:manual_export | PASS | None |
| identity_b_no_a_cache | PASS |      Easel通用模式+ 新建画像…成员账户退出工作台对话技能库内容库账号画像对话 新对话6A B base工作区连接中… 设置Easel夜深了，想创作点什么？从选题到发布，一站式帮你把想法做成能发的内容。看看能做什么 素材Enter 发送 · Shift+Enter 换行🔥蹭个热点看看现在微博和抖音有什么热搜，挑几个适合我做二创的选题✍️写小红书文案帮我写一条小红书种草文案，主题先问我🎴做金句卡片把一句走心的话做成一张适合发朋… |
| identity_b_own_session | PASS |      Easel通用模式测试画像+ 新建画像…成员账户退出工作台对话技能库内容库账号画像对话 新对话新对话6A B base已登录 · 服务端会话 设置6A-B-private复制fake-provider-ok 复制看看能做什么 素材Enter 发送 · Shift+Enter 换行       |
| identity_server_isolation | PASS | {"a": 2, "b": 2} |
| identity_two_workers | PASS | {"worker_a": "3b63a4eb858d541ed9f3ca20cdeee9458e1bd7338d935858c50f31db9d71687f", "worker_b": "ed1967501a0424d7c16403408f38cccf9236f8c6ce71978e8eba8e8fef3d232d"} |
| identity_home | PASS | ["C:\\Code\\Easel\\.claude\\worktrees\\original-web-task1-diagnostics\\.test-tmp\\task6a-0cbe360c\\main\\workspaces\\118b2539f80f4e3a999cfeb5647ece81\\home", "C:\\Code\\Easel\\.claude\\worktrees\\original-web-task1-diag… |
| identity_same_name_storage | PASS | {"a": "118b2539f80f4e3a999cfeb5647ece81", "b": "46c257e5d2e54fd1942542382c79bd4e"} |
| identity_a_relogin | PASS |      Easel通用模式+ 新建画像…成员账户退出工作台对话技能库内容库账号画像对话 新对话新对话导出稿件：xiaohongshu新对话新对话6A A base工作区连接中… 设置Easel夜深了，想创作点什么？从选题到发布，一站式帮你把想法做成能发的内容。看看能做什么 素材Enter 发送 · Shift+Enter 换行🔥蹭个热点看看现在微博和抖音有什么热搜，挑几个适合我做二创的选题✍️写小红书文案帮我写一条小红书种草文案，主… |
| stage:identity | PASS | 6R1vr1iDHL-xe5YypP1hD8VvzhT7j6dA0siF9oa_qiw |
| worker_restart_no_replay | PASS | {"old": "3b63a4eb858d541ed9f3ca20cdeee9458e1bd7338d935858c50f31db9d71687f", "new": "a36d6a7a68cf294d074261f91a6e7c49edfd7d83cdacd9c89f99998d98a55941", "hang": "interrupted"} |
| worker_restart_session_resume | PASS | n": "Existing directory to enter. Omit to read the current directory."}}}}}, {"type": "function", "function": {"name": "write", "description": "Create or fully replace a UTF-8 text file.", "parameters": {"type": "object… |
| worker_restart_files | PASS | C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics\.test-tmp\task6a-0cbe360c\main\workspaces\118b2539f80f4e3a999cfeb5647ece81\workspace\workspace-6a-sentinel.txt |
| stage:worker_restart | PASS | None |
| invite_one_time | PASS | 401 |
| logout_relogin | PASS | fresh invite accepted |
| stage:logout_invite | PASS | None |
| main_cleanup | PASS | [] |
| manual_policy_mode | PASS | {"mode":"manual","automaticSubmission":false} |
| manual_publish_rejected | PASS | {"code":"publishing_manual_only","detail":"publishing_manual_only","request_id":"3883aa391a8041ed835992675f7b730c"} |
| manual_no_run | PASS | 0 |
| manual_registered_restart | PASS | network=easel-6a-manual-95c7cf02-control state=present  volume=easel-6a-manual-95c7cf02-control state=present  volume=easel-6a-manual-95c7cf02-keys state=present  volume=easel-6a-manual-95c7cf02-egress state=present  co… |
| manual_migration_required | PASS | ����publish_mode_migration_required   |
| manual_cleanup | PASS | [] |

各阶段原始记录：

- 手册策略：[manual/](manual/) 与 `manual.json`
- 主部署/原 Web：[main/](main/)（logs、provider audit、浏览器截图）与
  `main.json`
- 受控平台、正式备份/恢复/升级：本轮 **not-run/阻塞**，见 §0；没有
  可引用的通过证据目录。
- 导出 ZIP：[export/](export/)
- 浏览器截图：[browser/](browser/)

## 3. 实际故障与最小修正

1. **execution 阶段镜像冒充 runtime**：首轮 main 演练使用
   `easel-worker-execution`（无 `/opt/dsh`），真实 Harness 启动失败
   `HarnessError`。改为在当前源码上叠加已验收 Harness runtime 后，A/B
   真实聊天与受控 provider 审计通过。失败记录保留，未计为通过。
2. **浏览器发送按钮被新会话重渲染清空**：首轮 `fill` 后按钮保持 disabled。
   测试脚本改为 `textarea.chat-input` 重试直到发送按钮启用；产品 UI 未改。
3. **工作台概览等待外部热点源**：受限出口下 `/api/trends` 未返回，概览
   数据保持加载。演练改为验证工作台/热点入口，并在选题页/日历页分别等待
   本人种子数据；外部热点源列为 not-run，不伪造热点通过。
4. **正式 Status 与 worker 动态启动的 socket 检查竞态**：Status 改为
   Start 完成后、worker 动态启动前执行一次；worker 启动与运行证据由
   worker lease、Run、聊天和重启检查另行覆盖。
5. **工作区 API 首次调用与 worker/gateway 激活竞态**：独立探针显示
   `/api/status` 先返回 `not_started`，同 workspace 的 `/api/ideas`
   在按需启动完成后返回 200；浏览器若在激活窗口内打开选题/账号页，
   可能短暂显示“工作区服务暂不可用（503）”。已按 TDD 在
   `WorkspaceApi.request/open_stream` 对带 `retry_after` 的 503 做有限
   重试（`test_request_retries_transient_worker_start_superseded` 红→绿）；
   真实持续不可用仍会在约 2–4s 后返回 503。
6. **`/api/status` 的 gateway 字段错误**：control 曾把 worker payload 的
   `gateway=false` 透传给前端，而 control 租约句柄的
   `gateway_state=available`；原页面因此把工作区数据误判为不可用。
   已按 TDD 改为以 control 句柄状态为准
   （`test_status_reports_control_gateway_available` 红→绿）。

## 4. 真实层、替身层与 not-run

- 真实：正式 Initialize/Start/Status/Stop/Backup/Verify/Restore/升级联锁、
  Docker/WSL provisioner、control/gateway/worker、真实 Harness ACP、两个
  真实 worker 与持久 HOME、原 React 构建、SQLite/Auth/RunManager、
  浏览器 Chromium、手动导出 ZIP。
- 替身：上游模型为受控 TLS fake provider；账号/发布由
  `task6a_controlled_platform.py` 替换 6 个原平台脚本，不访问真实平台；
  浏览器 TLS 仅由本轮终结器转发到正式 control。
- not-run：真实 DeepSeek/第三方供应商请求与计费；真实平台登录/短信/发布/
  账号状态；用户现有容器、卷、Key、Cookie、邀请、socket；Linux 原生生产
  生命周期（XFS/防火墙/IPv6/systemd/Chromium 默认 seccomp）；真实 ngrok/
  公网 TLS。

## 5. 清理与剩余门槛

- 每个临时 project/network/volume/DataRoot 使用随机标签；清理前按
  `easel.desktop.project` / `easel.6a.test` / `easel.workspace-id` 标签
  与名称前缀双重核对，只删除本轮资源，无全局 prune。结果见
  `checks.json` 的 `*_cleanup`。
- 6B 待用户决策：真实模型最小请求验收是否授权、用户实例何时维护切换、
  实际平台是否另行验收。6A 只准备可复审的版本/配置/备份与操作步骤，
  不执行外部或用户实例动作。
