# Task 6A 最终输入盘点与验收表

日期：2026-11-02。父基线：`4382180862e28d43f2974e32a618093c847ec262`。
实施 worktree：`.claude/worktrees/original-web-task1-diagnostics`（分支
`worktree-original-web-task1-diagnostics`，起始工作树干净）。

本文件只记录本轮可复核输入与验收边界；部署演练结果见
[verification.md](verification.md)，手册候选补丁见
[manual-patches/](manual-patches/)。真实供应商、真实平台账号/短信/发布、
用户实例切换均未执行，见“not-run”。

## 1. 最终源码与构建身份

| 项 | 值 | 来源/核对方式 |
|---|---|---|
| 最终提交 | `4382180862e28d43f2974e32a618093c847ec262` | `git rev-parse HEAD` |
| R2 功能实现 | `6b54e49e5dce99ec1fe4b2b3f4ed5703eb357da5` | 5C3 R2 验收记录；`40fca7c..HEAD` 仅前端/测试/文档 |
| control 镜像 | `sha256:c6677fd8d1fc2dd3d69e8df1f698568f7b280990c2659558866629ac43c62308` | `docker build -f deploy/desktop/Dockerfile.control`（含并发首屏 503 重试、gateway 状态修复与同 workspace ensure 串行化） |
| worker 镜像 | `sha256:6f62af472c8b272c0d67a1aec26284cff8f8c9adacfaad48e6ba0ce64fbf0612` | 在当前源码上增量叠加已验收 Harness runtime `easel-worker-harness:a6-r1`（其 `/opt/dsh` 固定在 `687f282…`） |
| 受控平台 worker | `sha256:caf564cc0e0954741fc5aab9ee54f354001794896e806baf93d4b0c4fa6dc7d3` | 以 worker 镜像为基础，仅替换 6 个原平台脚本（见下文“替身层”） |
| gateway 镜像 | `sha256:0d30939d375c1e3bef616678f5e8d7b6fad687143817f7d6183579ca8e881f0a` | `docker build -f deploy/desktop/Dockerfile.gateway` |
| 前端 index.html | `sha256:a91fc58445ec6698880e9438cf1da97db1a96027557d5c622a5cdcdec3ae49c1` | 本机 `npm run build` 与 control 镜像内文件一致 |
| 前端主 JS | `index-BreoCku2.js`，`sha256:37529fe5ffd1d78bb905f03afac02df112230ba97c8cb2344e999fa6ddf9ca2f` | 与 5C3 R2 证据一致 |
| 前端主 CSS | `index-Da4NYWkv.css`，`sha256:f5c7b6c1e9b131d94707f2b6e174bf60537ee9ea1c2dbec3afb33ee18c4d3b0e` | 同上 |

镜像内文件与工作树源码逐文件 SHA-256 比对（排除 `__pycache__`/`.pyc`）：

- control：`/opt/easel/easel`、`web/frontend/dist`、`skills/shared/scripts`、
  `deploy`、`managed` 共 84 个文件一致。
- gateway：`/opt/easel/easel` 等 72 个文件一致。
- worker：`/opt/easel` 下 `easel`、`web`、`skills`、`agents`、`deploy`、
  `managed` 共 1002 个文件与工作树一致（`managed` 在镜像内位于
  `/opt/easel/managed`，源码位于 `deploy/multiuser/managed`，已按映射核对）。
  `/opt/dsh/.git/HEAD` 为 `687f282e8625c6b8d11ff58211a90524be2e4317`。
- 受控平台 worker：仅 `skills/shared/scripts/{xhs_publish,douyin_publish,
  web_publisher,weixin_mp_stats,account_stats,bili_login}.py` 6 个文件为
  受控 runner，其余 241 个文件与工作树一致。

worker 采用“当前源码叠加到已验收 Harness runtime”的增量方式，避免无缓存
重建 Harness/Chromium；未使用只有生命周期的 `base`/`execution` 阶段冒充
runtime（首轮误用 execution 镜像时真实 Harness 启动失败，已立即更换并保留
失败记录）。

## 2. 受管 Harness 与配置来源

| 项 | 值 |
|---|---|
| Harness 仓库 | `https://github.com/deepseek-ai/deepseek-harness.git` |
| Harness 固定提交 | `687f282e8625c6b8d11ff58211a90524be2e4317` |
| 包版本 / Node / pnpm | `0.2.1-rc.2` / `24.22.1` / `11.28.5` |
| ACP profile | `deploy/multiuser/managed/acp-profile.json` |
| ACP patch | `deploy/multiuser/managed/acp.patch.yml`（provider `easel-broker`，默认 `deepseek-chat`） |
| 受管配置来源 | `Initialize-WorkspaceEasel.ps1 -ManagedAgentConfig` 复制的 `managed-agent.json`（不含任何 Key） |
| 聊天路由来源 | 管理员 `POST /api/admin/managed-config` 发布的版本化 chat 路由；broker 快照逐 Run 固定 |
| 出口策略 | `egress-policy.json`（默认空允许列表仍允许公网 HTTPS:443；本轮显式允许 `fake-provider.test`） |

worker 运行目标为最终 `runtime` 阶段（`python -m easel.worker.server serve`），
未使用只提供生命周期的 `base` 阶段冒充 runtime。worker/gateway 由
`deploy/multiuser/provisioner.py` 按 workspace 动态创建，不发布宿主端口。

## 3. 部署脚本与配置来源

| 入口 | 职责 |
|---|---|
| `deploy/desktop/Initialize-WorkspaceEasel.ps1` | 创建/校验 control、keys、egress 卷与控制网络；复制受管配置/策略；不生成真实 Key |
| `deploy/desktop/Start-WorkspaceEasel.ps1` | 校验固定镜像/卷/网络/key/前端/provisioner 身份，启动 provisioner、control、egress；登记 `deployment.json` |
| `deploy/desktop/Status-WorkspaceEasel.ps1` | 只读检查资源归属、健康、版本、发布策略、Stop 记录 |
| `deploy/desktop/Stop-WorkspaceEasel.ps1` | 屏障、drain、确认 Run/请求/实例收口后 down，写完成记录 |
| `deploy/desktop/Backup-WorkspaceEasel.ps1` | Stop 后完整 v2 备份（DB、key、配置、workspace、HOME） |
| `deploy/desktop/Verify-WorkspaceEasel.ps1` | 只读逐文件 SHA-256/SQLite/密文校验 |
| `deploy/desktop/Restore-WorkspaceEasel.ps1` | 新项目/卷/网络/目录恢复，撤销旧登录并签发一次性恢复邀请 |
| `deploy/desktop/compose.workspace.yaml` | control/egress 正式装配；`EASEL_PUBLISH_MODE` 必填 |
| `deploy/multiuser/provisioner.py` | 每 workspace 创建 worker/sidecar/gateway；固定镜像、配额、网络、只读代码根 |

配置来源：`DataRoot/config/managed-agent.json`（非秘密）、
`egress-policy.json`、`deployment.json`（镜像/前端/配置/源码/Harness 摘要）、
`start-args.xml`/`source-root.txt`（日常重启参数）。真实供应商 Key 只经
管理员认证后的秘密输入进入 control，不写入上述公开文件。

## 4. 验收表

层级说明：**真实**＝本轮实际运行的正式组件；**替身**＝测试专用、明确
替代外部系统；**not-run**＝本轮未执行。所有结论以
[verification.md](verification.md) 的原始输出为准。

本人存储/身份边界：每个页面的数据落在
`<WorkspaceRoot>\<workspaceId>\workspace`（`/workspace`）与
`<WorkspaceRoot>\<workspaceId>\home`（`/home/easel`）；Run 的接纳/状态/
停止沿用既有 RunManager、容量与权限机制；control 不共享用户路径，A/B
不共用 Cookie、会话、文件或浏览器 profile。

| # | 验收项 | 真实层 | 替身层 | 入口/路径 | 断言 | 证据 | not-run |
|---|---|---|---|---|---|---|---|
| A1 | 邀请登录、管理员/成员权限 | control 前端构建、AuthService、SQLite、Cookie/CSRF | 无 | 原 Web 登录页；`/api/auth/*`、`/api/admin/*` | 邀请一次性；管理员可建成员/发邀请；成员访问管理员接口 403；注销后旧 Cookie 401 | `main/browser-*.png`、`main/http-checks.json` | 真实短信/外部 IdP |
| A2 | 受管 Harness 聊天流、权限题卡、停止、刷新续聊 | 真实 worker 镜像内 Harness ACP、RunManager、gateway、broker、SSE | 受控 OpenAI-compatible TLS provider（`fake-provider.test`） | 原对话页；`/api/chat`、`/api/chat/jobs/*/stream`、`/api/chat/stop` | 流式助手消息；允许/拒绝题卡；停止进入 cancelled/interrupted 且 token 撤销；刷新后同会话历史与续聊；同一 turn 不重复 | `main/chat-*.png`、`main/provider-audit.jsonl`、`main/run-checks.json` | 真实 DeepSeek Key/供应商 |
| A3 | 素材上传、预览、下载 | control/worker workspace、`/api/outputs`、`/api/upload`、`/api/media/*` | 无 | 原内容库/对话附件 | 上传成功；`/api/outputs` 列出本人 `项目/封面.png`；预览/下载 200；A/B 同名文件互不可见；注销后媒体 401 | `main/` 下 `media_*` 检查与浏览器截图 | 大文件/宿主外部路径 |
| A4 | 画像、技能 | worker Run、workspace `profiles`、技能目录 | 受控 provider（技能 Run 的模型响应） | 原画像/技能页 | 画像编辑持久化；技能 Run 走既有 Run/容量/停止；结果可见且不写全局 | `main/profile-checks.json`、`main/browser-profile.png` | 真实模型质量/真实平台技能 |
| A5 | 热点/选题/日历/概览入口与本人数据 | control/worker workspace JSON、RunManager | 受控 provider（概览生成） | 原工作台/热点/选题/日历 | 工作台/热点入口渲染；选题页显示本人初始选题、日历页显示本人初始排期；A/B 同名存储可区分；刷新后仍在 | `main/` 下 `workbench_*` 检查与浏览器截图 | 真实外部热点源（受限出口下不伪造热点内容） |
| A6 | 受控账号登录、分析 | worker 内 Playwright Chromium、持久 profile、受控平台子进程 | `task6a_controlled_platform.py` 替代原平台脚本 | 原账号页；`/api/login/*`、`/api/accounts/*/analytics` | 二维码→短信→成功；分析返回数值；profile 落本人 HOME；原平台脚本未执行 | `controlled/account-*.json`、`controlled/browser-accounts.png` | 真实平台账号/短信 |
| A7 | 人工审核 ZIP 导出并实际解包 | control/worker、`build_package`、原发布页下载 | 无 | 原发布工作台→人工审核发布 | 审核前置校验；ZIP 下载后可解包；`manifest.status=ready_for_manual_submission`；媒体在包内 | `main/export-*.zip`、`main/export-check.json`、`main/browser-manual-export.png` | 真实平台提交 |
| A8 | 受控自动发布、短信、停止、结果未知、一次提交 | worker 发布执行器、进程树取消、Run/容量 | `task6a_controlled_platform.py` 替代抖音平台脚本 | 原发布工作台→自动发布；`/api/publish/*` | 自动策略可提交；短信 `123456` 成功、错误码提示；停止后结果未知且不补发；同 requestId 只执行一次 | `controlled/publish-*.json`、`controlled/browser-publish-*.png` | 真实发布/真实短信/计费 |
| B1 | 正式 Initialize/Start/Status/Stop | PowerShell + Docker Desktop + WSL provisioner | 无 | `deploy/desktop/*.ps1` | Initialize 幂等；Start 输出固定镜像/前端/config 摘要；Status `status=ok`；Stop 完成记录 | `main/lifecycle/*.log`、`main/status.txt` | 真实 ngrok/公网 Origin |
| B2 | 发布策略 manual/automated、迁移、重启保留 | control 部署配置、Stop/Start 登记 | 无 | `-PublishMode`、`deployment.json` | manual 下 publish 403 且 0 Run；automated 可提交；旧登记缺策略 Start 拒绝 `publish_mode_migration_required`；manual 登记重启保留；Stop 可完成 | `manual/*.log`、`manual/policy-checks.json` | 无 |
| B3 | 双身份、双 worker、持久 HOME 隔离 | 两个真实 worker 容器、独立 HOME/workspace | 无 | 原 Web 两个浏览器 context | workspace/worker/HOME 不同；同名会话/文件/画像/草稿/发布意图可区分；慢响应/未确认发布期间切 B 不显示/重放 A | `main/identity-checks.json`、`controlled/browser-identity-switch.png` | 同浏览器多标签页（刻意不采用） |
| B4 | worker 重启、会话/文件恢复、在途不重放 | 真实 worker/provisioner | 受控 provider | 原对话页 + `docker rm -f worker` | 原会话/文件恢复；在途 Run 明确 interrupted/failed/cancelled，不重放 prompt；同会话新消息成功 | `main/restart-checks.json`、`main/browser-worker-restart.png` | 宿主重启/断电 |
| B5 | Stop→备份→逐文件校验→新目标恢复 | 正式 Backup/Verify/Restore/Start | 受控 provider | `deploy/desktop/*.ps1` | 备份 `version=2`、`clean_stop=true`；逐文件校验；恢复后旧 Cookie 401、新邀请可用、A/B 历史隔离、同会话新 Harness 消息通过 | `main/backup/*.txt`、`main/restore/*.txt` | 公网 TLS/健康检查外部监控 |
| B6 | 升级/回滚（仅临时项目） | 正式 Start `-UpgradeFromBackup`、固定镜像 | 坏镜像/派生镜像 | 临时项目 | 坏镜像 `worker_or_gateway_image_mismatch` 且数据/Key 不变；匹配备份升级后历史+新消息；回滚用备份恢复旧镜像 | `main/upgrade/*.log` | 真实生产版本升级 |
| C1 | 清理归属与痕迹 | Docker labels/资源清单 | 无 | 本轮随机 project/network/volume/DataRoot | 仅删除本轮已登记资源；无全局 prune；保存镜像/commit/配置哈希/清理结果 | `cleanup.json` | 历史主审目录 |

## 5. 明确 not-run

- 真实 DeepSeek/第三方供应商请求与计费；本轮 provider 为受控 TLS fake。
- 真实小红书/抖音/视频号/公众号等平台登录、短信、发布、账号状态变化；
  原平台脚本在本轮被受控 runner 替代，未执行。
- 用户现有容器/卷/workspace/Key/Cookie/邀请/socket：未读取、未注入、未切换。
- Linux 原生多人生产生命周期（systemd、XFS 项目配额、防火墙、IPv6、
  Chromium 默认 seccomp 的生产结论）：沿用既有手册的真实未验证状态。
- 真实 ngrok/公网 TLS、外部监控与生产回滚保证。
