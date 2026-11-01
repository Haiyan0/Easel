# 5C2 接口与资源盘点（以实际 handler 为准）

日期：2026-11-01。父基线 `654305a69c1d68d6b157b608dbd2c7d5f96d6298`。
实施 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

本表按实际代码路径核对 `web/frontend/src/lib/api.ts`、`AccountsPage.tsx`、`DashboardPage.tsx`
与 `web/app.py`（账号/分析区自 4933 行起）后的 handler，不按 GET/POST 推断只读。
所有“起浏览器/子进程/联网”列均为代码事实；control 计划列描述本轮最小适配。

## 1. 请求 → handler → 资源矩阵

| 原组件/请求 | 实际 handler | 返回 | 起浏览器/子进程/联网 | 身份与 CSRF（control） | 任务类型/容量 | 固定脚本与本人路径 | 状态/停止/重试 | 凭证 | 现有测试 | 5C2 control 计划 |
|---|---|---|---|---|---|---|---|---|---|---|
| AccountsPage `fetchAccounts()` → `GET /api/accounts` | `web/app.py:4933 api_accounts` | `[{platform,name,backend,supported,loggedIn,note}]` | 否。只读 `LOGIN_RUNNERS` 与状态标记/`cookies.json` | 需登录；GET 无 CSRF；`WORKSPACE_READ` | 快速状态；不占 Run 容量 | 无脚本；读 `_login/<platform>.json`、worker `state_root/skills/platform-cookies/bilibili-default.json`、`wechat` 配置 | 无停止/重试；纯读取 | 不返回凭证 | `tests/test_login_start.py` 等间接覆盖 | 签名短请求 `WorkspaceApi.request`；platform 白名单在 worker |
| AccountsPage `accountWhoami(p,force)` → `GET /api/accounts/{platform}/whoami?force=bool` | `web/app.py:5186 api_account_whoami` → `_check_account_whoami` | `{loggedIn,name,avatar}` | **是**。`bili_login.py whoami`、`xhs_publish.py whoami`、`weixin_mp_stats.py whoami`、`douyin_publish.py whoami`、`web_publisher.py whoami`；headless 浏览器/联网，35–60s | 需登录；GET 无 CSRF；`WORKSPACE_READ` | **browser 长任务**；占 Run 容量 | `SHARED_SCRIPTS/<固定脚本>`；cwd=PROJECT_ROOT；worker profile=`HOME/.easel-browser-profiles/<platform>/default`，bili cookie/公众号配置在 `state_root` | 进程内 `_WHOAMI_TASKS`/`_WHOAMI_CACHE` 合并；超时 504；失败 502；`force=true` 绕过 TTL；无显式停止 | avatar 只是平台 URL；AppSecret 不回传 | `tests/test_account_whoami.py` | 受管 browser Run（`action=whoami`）；强制校验也走接纳/容量/终态；结果只返回昵称/头像 URL |
| AccountsPage `startLogin(p)` → `POST /api/login/{platform}?restart=true` | `web/app.py:4944 api_login_start` → `_start_login(platform,restart)` | `{mode:'qr'|'credentials'|'terminal', state,message,qr,qrTs,configured}` | **是**。按 backend 起 `xhs_publish.py login`、`bili_login.py login`、`douyin_publish.py login`、`web_publisher.py login-qr`；后台 `subprocess.Popen`，最长 240s | 需登录；POST 必须 Origin+CSRF；`TASK` | **browser 长任务**；占 Run 容量；同平台互斥 | 固定脚本+固定参数；QR/状态/日志写 `outputs/_login/<platform>.{png,json,log,code}`；profile 按平台/账号；公众号返回 `mode=credentials` 不启动 | `restart=true` 先 terminate 旧进程并等 8s；未知平台 404；重复点击若进程存活且 restart=false 返回当前状态；前端轮询 `status`；无独立“停止登录”路由（注销会停并删 profile） | 不返回 AppSecret；凭证式平台只回 `configured` | `tests/test_login_start.py`、`tests/test_login_transport.py` | 受管 browser Run（`action=login`）；`requestId` 稳定标识一次派发；`restart` 由适配层解释：先停旧 Run 等终态再新 Run；关闭弹窗≠停止任务 |
| AccountsPage `loginStatus(p)` → `GET /api/login/{platform}/status` | `web/app.py:5025 api_login_status` | `{mode:'qr',state,message,qr,qrTs}` | 否（只读状态文件与进程 poll）；`success` 时清 whoami 缓存 | 需登录；GET；`WORKSPACE_READ` | 快速状态 | 无；读 `_login/<platform>.json/.png` | 前端 1s 串行轮询；`success/expired/error` 终态停轮询；503/网络连续 5 次才报错 | 无凭证 | `tests/test_login_start.py` | 签名短请求；同时映射 control Run 终态（停/超时/worker 重启）到 UI 状态 |
| AccountsPage `submitLoginSms(p,code)` → `POST /api/login/{platform}/sms` | `web/app.py:5043 api_login_sms` | `{ok:true}` | 否（仅写验证码文件，runner 读走） | 需登录；POST CSRF；`TASK` | 绑定当前本人待短信任务，不新建 Run | 写 `outputs/_login/<platform>.code` | 4–8 位数字；旧/过期任务由 control 拒绝 `sms_not_pending` | 验证码不落日志/不回显 | `tests/test_login_transport.py` | control 先查本人该平台最新 active Run 且 worker 状态为 `sms_required`，再签名短请求转发；不冒充新 Agent prompt |
| AccountsPage `startMpLogin(p)` → `POST /api/accounts/{platform}/mp-login` | `web/app.py:5144 api_mp_login_start` | `{mode:'qr',state,message,qr,qrTs}` | **是**。`weixin_mp_stats.py login --proxy ... --timeout 240`；persistent Playwright，二维码 | 需登录；POST CSRF；`TASK` | **browser 长任务**；占 Run 容量；与 `wechat-oa` 扫码/分析同 profile 锁 | 仅 `wechat-oa`；QR/状态写 `outputs/_login/wechat-oa-mp.{png,json}`；profile=`WeixinMpProfile`(local) / `wechat-oa/default`(worker) | 已在跑则复用，不删二维码/不起第二个 Chromium；无独立停止路由 | 无 AppSecret | `tests/test_login_transport.py` | 独立 task key `browser:mp-login:wechat-oa`；Run executor `action=mp-login`；与凭证式登录状态分开 |
| AccountsPage `mpLoginStatus(p)` → `GET /api/accounts/{platform}/mp-login/status` | `web/app.py:5178 api_mp_login_status` | `{mode:'qr',state,message,qr,qrTs}` | 否（读文件 + 进程 poll） | 需登录；GET；`WORKSPACE_READ` | 快速状态 | 读 `_login/wechat-oa-mp.*` | 同登录轮询 | 无 | `tests/test_login_transport.py` | 签名短请求 + Run 终态映射 |
| AccountsPage `getCredentials(p)` → `GET /api/accounts/{platform}/credentials` | `web/app.py:5067 api_get_credentials` | `{configured,appIdMasked,name,author}` | 否（读 YAML） | 需登录；GET；`WORKSPACE_READ` | 快速状态 | worker `state_root/skills/wechat/default.yaml`；local 旧 `skills/builtin/skill-wechat-publisher/wechat-publisher.yaml` | `AppSecret` 永不回传；`appIdMasked` 只露前 6 后 4 | 只回掩码 | 无专门测试 | 签名短请求；只读本人 worker 路径 |
| AccountsPage `saveCredentials(p,{appId,appSecret,name,author})` → `POST /api/accounts/{platform}/credentials` | `web/app.py:5083 api_save_credentials` → `_wechat_save_credentials` + `_wechat_verify_token` | `{ok,message}` | **是**。子进程调用微信官方 token 接口，30s；当前实现先写后验证 | 需登录；POST CSRF；`WORKSPACE_WRITE` | **browser/联网任务**；占 Run 容量 | worker `state_root/skills/wechat/default.yaml`；token 缓存 `state_root/skills/wechat/tokens/web.json`；固定内联验证脚本 | 验证失败当前仍保留新凭证（本轮将改为先验证、失败不覆盖旧有效配置）；日志不含 secret | AppSecret 只在请求体→worker 进程内，不回传/不写事件 | 无专门测试 | 受管 browser Run（`action=credentials`）；先验证后原子保存；失败保留旧配置；返回掩码/原因 |
| AccountsPage `logoutAccount(p)` → `POST /api/logout/{platform}` | `web/app.py:5303 api_logout` | `{ok,deleted}` | **是**：终止登录进程、持 profile 锁删 profile；公众号清凭证与 token 缓存 | 需登录；POST CSRF；`TASK` | 绑定本人平台现有任务；先停 Run 等终态，不新建 Agent prompt | `LOGIN_PROCESSES`、`outputs/_login/*`、worker HOME profile、`state_root` cookie/wechat 配置 | 停进程并等退出后才删 profile，防止复活；profile 忙 409；删除锁外对象保留互斥 | 删 `accounts.web` 与 token 缓存；不回显 secret | `tests/test_login_transport.py`、`test_browser_profiles.py` | 先对本人该平台最新 active Run `stop` 并等终态，再签名短请求注销；注销后旧 Run 迟到成功不能写回标记 |
| DashboardPage `fetchAnalyticsPlatforms()` → `GET /api/analytics/platforms` | `web/app.py:5393 api_analytics_platforms` | `[{platform,name,loggedIn}]` | 否（标记/cookie/凭证判定） | 需登录；GET；`WORKSPACE_READ` | 快速状态 | 平台白名单 `ANALYTICS_PLATFORMS={xiaohongshu,douyin,kuaishou,zhihu,weixin-channels,bilibili,wechat-oa}` | 无重试 | 无 | `tests/test_web_analytics.py` | 签名短请求；固定白名单在 control 与 worker 双侧 |
| DashboardPage `fetchAccountAnalytics(p)` → `GET /api/analytics/{platform}` | `web/app.py:5403 api_analytics` → `_fetch_analytics` | 平台原始 JSON（followers/likes/following/posts/metrics/notes/growth/fetched_at 等） | **是**。`account_stats.py fetch --platform`、`bili_login.py stats --cookie`、`weixin_mp_stats.py stats`；Playwright/联网 90–180s | 需登录；GET；`TASK` | **analytics 长任务**；占 Run 容量；同平台 in-flight 共享一次 | 固定脚本/固定平台；worker profile/cookie/公众号会话均在本人根；无磁盘分析缓存（前端 localStorage `easel_analytics`） | 超时杀进程树 504；错误 502；平台白名单 404；失败不写零值/更新时间；旧缓存仅前端显示 | 不返回凭证；公众号走 mp 会话非 AppSecret | `tests/test_web_analytics.py` | 受管 analytics Run；同平台 active Run 合并（join），刷新新 Run；等待终态取结果；失败保留旧缓存并标 stale/真实 fetched_at；未知数字 null |
| AccountsPage 二维码 `<img src={mediaUrl(qr)}>` | `web/app.py` 媒体入口（`/api/media/...`） | PNG | 否 | 需登录；GET；`WORKSPACE_READ` | 快速读取 | `outputs/_login/<platform>.png`、`_login/wechat-oa-mp.png`（内部 `_` 目录，现有通用 media 拒绝） | `qrTs` 作缓存键 | 无 | 现有 media 边界测试 | 新增固定平台二维码入口 `/api/accounts/{platform}/qr`、`/api/accounts/{platform}/mp-login/qr`，只允许固定文件名，经本人 worker 流式返回 |

## 2. 旧全局依赖与隔离结论

| 依赖 | 实际位置/作用域 | 风险 | 5C2 结论 |
|---|---|---|---|
| `LOGIN_PROCESSES: dict[str, Popen]` | `web/app.py:473`，worker 进程内全局 | 若 control 宿主直接 import/执行 handler，会跨用户共享；worker 重启丢进程句柄 | control 不执行旧 handler；每个 workspace 独立 worker 进程；浏览器任务由该 worker 的 RunManager 执行，状态持久化到 control Run/worker status 文件 |
| `LOGIN_DIR = OUTPUTS_DIR/_login` | worker `workspace_root/outputs/_login` | 相对路径/符号链接越界；内部目录默认不可公开 | 仅固定文件名；二维码走专用入口；注销/状态读写仍由 worker 路径工具约束 |
| `_WHOAMI_CACHE` / `_WHOAMI_TASKS` / `_WHOAMI_LOCK` | worker 进程内（TTL 600s） | 不能跨用户复用；control 进程若缓存会串身份 | 只在 worker 进程内；control 侧 whoami 结果不落 localStorage，按身份内存隔离并在 401 卸载时丢弃 |
| `_analytics_inflight` | worker 进程内 | 同平台共享任务；跨用户会串结果 | 只在 worker；control 以本人 task conversation + Run 合并，不同 workspace 无共享 |
| 公众号配置 `WECHAT_CONFIG_YAML` | worker `state_root/skills/wechat/default.yaml`；local 旧技能目录 | 旧宿主 YAML/.env 不能冒充本人配置 | 只读写 worker 本人状态根；不复制宿主 YAML/.env |
| B 站 cookies `_bili_cookie_path` | worker `state_root/skills/platform-cookies/bilibili-default.json`；local `PROJECT_ROOT/cookies.json` | 旧 cookie 不能跨用户 | 只用 worker 本人路径 |
| 浏览器 profile | worker `HOME/.easel-browser-profiles/<platform>/default`；local 旧 `XiaohongshuProfile` 等 | 同名平台不同 workspace 共享 profile 会串号 | workspace 隔离；锁在 profile 父目录 `.locks/`，删 profile 不删锁；脚本与 `easel.worker.browser` 同协议 |
| 前端 whoami 缓存 | `localStorage['easel_whoami_v4']` | control 跨身份复用 | control 改为适配器实例内存；local 保留 localStorage |
| 前端分析缓存 | `localStorage['easel_analytics']` | control 跨身份复用 | control 改为适配器实例内存；local 保留 localStorage；撤销/换身份丢弃 |
| `qr`/状态/二维码 mtime | `_login/<p>.json/.png` | 公开任意 `_` 文件会越界 | 仅允许白名单平台固定 QR 文件，经已审媒体入口 |

## 3. `force` / `restart` / 未知参数语义

- `whoami?force=true`：仅此布尔查询；`force` 绕过 worker 进程内 whoami TTL 重新真校验。
  未知 query 键不转发；control 逐键校验后只转发 `force`。
- `login/{platform}?restart=true`：原 `api_login_start` 对运行中进程执行 terminate+wait(8s)，再启动。
  本轮 control 不把浏览器传来的 `restart=true` 直接透传：适配层用 `requestId` 固定一次派发；
  `restart=true` 解释为“显式重启”——先对本人该平台 active Run 发 stop 并等终态，再新建 Run；
  同 `requestId` 重试返回原 Run，不新起第二个浏览器。未知 query/body 键一律 400。
- 平台只允许 `LOGIN_RUNNERS` / `ANALYTICS_PLATFORMS` 固定集合；不把平台字符串拼进任意脚本或 URL。
- 状态未知值（`unknown`/缺失）不是 success；前端只在 `success` 显示已登录。

## 4. 本轮任务类型与容量映射

| control 入口 | task_type | 是否占 Run 容量 | 一次派发键 | 终态映射 |
|---|---|---|---|---|
| `POST /api/login/{platform}` | browser | 是 | `browser:login:{platform}` + requestId | worker Run done→登录状态文件 success；failed/cancelled/interrupted→error/cancelled |
| `POST /api/accounts/{platform}/mp-login` | browser | 是 | `browser:mp-login:{platform}` + requestId | 同上 |
| `GET /api/accounts/{platform}/whoami?force=` | browser | 是 | `browser:whoami:{platform}` + 调用方 requestId/现有 active | Run done→JSON；failed→502；超时→504 |
| `POST /api/accounts/{platform}/credentials` | browser | 是 | `browser:credentials:{platform}` + requestId | done→`{ok,message}`；failed→保留旧配置 |
| `GET /api/analytics/{platform}` | analytics | 是 | `browser:analytics:{platform}` + 每次刷新唯一 requestId；active 合并 | Run done→结果 JSON；failed→平台错误；timeout→504 |
| `GET /api/accounts`、`/api/analytics/platforms`、`.../status`、`.../credentials` GET、二维码 | 签名短请求 | 否（request lease） | 不适用 | 只读本人 worker |
| `POST /api/login/{platform}/sms` | 绑定现有 Run | 否（不新建） | 本人该平台最新 active Run | 只写验证码文件；过期/旧任务 409 |
| `POST /api/logout/{platform}` | 绑定现有 Run | 否（先停旧 Run） | 本人该平台 active Run | 停进程→删 profile；晚到 success 不回写 |

## 5. 现有测试入口

- `tests/test_login_start.py`、`tests/test_login_transport.py`：原登录启动/传输与状态。
- `tests/test_account_whoami.py`：whoami 真校验、TTL、并发与错误语义。
- `tests/test_web_analytics.py`：分析平台白名单、超时、错误与数据。
- `tests/multiuser/test_browser_profiles.py`、`tests/integration/multiuser/test_browser_profiles_real.py`：profile 锁与隔离。
- `tests/multiuser/test_task_leases.py`：任务表/容量/request lease。
- `tests/multiuser/test_original_web_api.py`、`tests/multiuser/test_workspace_tasks.py`、`tests/multiuser/test_task5c1_r1.py`：control 转发/任务适配模式。
- 前端 `tests/frontend/accountLogin.test.mjs`、`analytics.test.mjs`、`whoamiCache.test.mjs`、`workspaceData.test.mjs`。

## 6. 未决/本轮不做

- 真实平台扫码、真实短信、真实 AppSecret 验证、真实发布：未授权，只用受控夹具。
- 发布/导出（5C3）不进入；`/api/publish*` 保持未接通。
- 不把旧宿主 YAML/.env/cookies/profile 复制进用户目录；不操作用户运行容器/卷/Key/Cookie/邀请。
