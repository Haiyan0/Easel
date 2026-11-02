# 5C3 盘点：原发布工作台、人工导出与自动发布

父基线：`fcb351d532301d22afe3be0c91373b19f3505ca7`
盘点日期：2026-11-01
盘点范围：`PublishingPage.tsx`、`ManualPublishPage.tsx`、`PublishPage.tsx`、`web/frontend/src/lib/api.ts`、`web/app.py` 发布区、`easel/manual_publish.py`、平台脚本、已有测试。

## 1. 原页面 → 原接口表

| # | 页面/动作 | method + path | query/body | 原后端位置 | 副作用 | 归属/身份 |
|---|---|---|---|---|---|---|
| P1 | PublishingPage 读取发布方式 | `GET /api/publishing-policy` | 无 | `web/app.py:5768` | 只读 `ENV_FILE` 的 `EASEL_PUBLISH_MODE` | 原 Web 为单用户；control 需改为受管策略 |
| P2 | ManualPublishPage 导出稿件包 | `POST /api/manual-publish/{platform}/export` | `{title,body,tags,media[],reviewed}` | `web/app.py:5779` → `easel/manual_publish.py:build_package` | 在 `outputs/_publish/manual/` 新建 ZIP；不访问平台 | 必须绑定本人 outputs |
| P3 | PublishPage/ManualPublishPage 读取账号 | `GET /api/accounts` | 无 | `web/app.py` 账号区 | 只读账号状态 | 5C2 已按 workspace 隔离 |
| P4 | 两页读取媒体 | `GET /api/outputs` | 无 | `web/app.py:4801` | 只读内容库 | 本人 outputs |
| P5 | 媒体预览/下载地址 | `GET /api/media/{path}` | 无 | `web/app.py:4860` | 读文件；`_publish`/`_login` 等内部目录默认拒绝 | 本人 outputs；导出 ZIP 需窄例外 |
| P6 | AI 排版（流式） | `POST /api/chat/stream` | `{message,persona,sessionId,turnId}` + SSE | `web/app.py` 聊天流 | 启动 Agent Run | control 必须走受管 chat/skill Run |
| P7 | AI 检查（整段） | `POST /api/chat` | `{message,persona}` | `web/app.py` 聊天 | 启动 Agent；返回整段 markdown | 同上 |
| P8 | 人设一致性检查 | `POST /api/skill` | `{skill:'persona-check',input,persona}` | `web/app.py` SKILL 区 | 执行 SKILL | 5C1 已按 Run 接入 |
| P9 | 存草稿/排期/人工已发布 | `POST /api/schedule` | 日历记录 | `web/app.py` 日历区 | 写本人 `_schedule.json` | 5B 已按 workspace 隔离 |
| P10 | 自动发布 | `POST /api/publish/{platform}` | `{title,body,media[],tags,persona}` | `web/app.py:5906` | 先检查 `EASEL_PUBLISH_MODE=automated`；启动平台子进程/浏览器，真发布 | 必须经 Run/容量/签名；策略拒绝先于容量 |
| P11 | 异步发布状态 | `GET /api/publish/{platform}/status` | 无 | `web/app.py:5884` | 只读 `outputs/_publish/{platform}.json` | 旧状态无 Run 归属，control 不得据此结算新 Run |
| P12 | 发布短信验证码 | `POST /api/publish/{platform}/sms` | `{code}` | `web/app.py:5892` | 检查 automated；写 `outputs/_publish/{platform}.code` | 必须绑定当前本人平台 Run |
| P13 | 发布成功回流日历 | 内部 | — | `web/app.py:_record_successful_publish` | 成功后写 `_schedule.json` | 脚本侧 `AUTORECORD=0`，由 Web 记录一次 |

## 2. 发布策略来源与权限

- 原实现：`easel/manual_publish.py:publishing_mode(read_settings(ENV_FILE))`。`EASEL_PUBLISH_MODE` 缺失/空/`automated` → `automated`；`manual`/非法值 → `manual`。即**默认保留自动发布**。
- 原权限：单用户 Web，任何能访问页面的人都能提交；后台每次 POST 重新读取 .env，防止页面加载后策略被改动仍可发布。
- control 现状：`GET /api/publishing-policy`、`POST /api/publish/*`、`POST /api/manual-publish/*/export` 已登记在 policy/tasks 表，但 `publish` 任务 `implemented=False`，路由由 middleware 统一 503；没有 control 侧策略来源。
- 5C3 约束：control 不读取 worker/宿主 `.env` 当策略；成员不能通过请求改变策略。采用 control 部署侧的显式受管配置（`ControlConfig.publish_mode`，CLI `--publish-mode`，默认 `automated`），只读暴露给页面；人工/自动两条路径都保留。

## 3. 原平台发布链与规则

| 平台 | 脚本/命令 | 媒体规则 | 成功判定 | 异步/SMS |
|---|---|---|---|---|
| `xiaohongshu` | `xhs_publish.py publish[-video] --no-proxy --images/--video … --exec` | 需媒体；图文或视频二选一 | 脚本自行校验离开发布页/成功提示；未确认 exit 3 | 否 |
| `douyin` | `douyin_publish.py publish[-video] … --exec --status-file … --sms-code-file …` | 需媒体；只能视频（`VIDEO_ONLY`） | 发布后读回作品列表；verified=0，unverified=5，未确认=3 | 是；状态 `starting/sms_required/verifying/success/error` |
| `kuaishou` | `web_publisher.py publish --platform kuaishou … --exec` | 需媒体 | 发布成功校验 + 读回 | 否 |
| `weixin-channels` | `web_publisher.py publish --platform weixin-channels …` | 需媒体；只能视频 | 成功校验 | 否 |
| `zhihu` | `web_publisher.py publish --platform zhihu …` | 无媒体硬要求 | 跳转到文章页 | 否 |
| `bilibili` | `biliup -u cookies.json upload <video> --tid 36 …` | 必须视频 | 命令退出码 | 否 |
| `wechat-oa` | `html_converter.py` + `weixin_mp_stats.py publish --html --cover …` | 至少一张图片作封面；不能视频 | 命令退出码 | 否 |

公共规则（`web/app.py:api_publish`）：
- 空标题且空正文 → 400；标题为空时取正文前 20 字。
- 图片与视频不能同时出现；`MEDIA_REQUIRED` 平台无媒体 → 400；`VIDEO_ONLY` 平台无视频 → 400。
- 画像可选；存在时必须通过原画像名校验且画像存在。
- 媒体路径经 `_safe_output_path` 解析到本人 outputs，内部目录/根外/符号链接逃逸拒绝。
- 子进程 env 由 `_publish_env()` 构造，强制 `EASEL_CALENDAR_AUTORECORD=0`，成功后由 Web 记录一次日历。
- 抖音状态/验证码文件位于 `outputs/_publish/`；旧文件无 Run 归属。

## 4. 人工导出（`build_package`）现状

- 平台固定白名单 7 个；`reviewed` 必须是严格 `True`；标题/正文不能同时为空。
- 媒体最多 32 个；每个路径 resolve 后必须仍在 outputs 内；拒绝顶层 `_`/`.` 目录；扩展名白名单；总大小 ≤ 2GB。
- 输出目录固定 `outputs/_publish/manual/`，文件名 `easel-<platform>-<12hex>.zip`。
- ZIP 内容：`title.txt`、`body.txt`、`tags.txt`、`manifest.json`（`status=ready_for_manual_submission`、`published=false` 语义）、`使用说明.txt`、`media/NN_<name>`；公众号额外 `article.html`（转义 HTML、去链接/远程图）。
- 失败只删除本次 ZIP；不写日历、不标记 published。
- **已发现的旧缺陷**：`_publish` 属于内部目录，`/api/media/_publish/...` 被 `_internal_output_path` 拒绝，原页面导出后 `mediaUrl(result.path)` 实际无法下载。5C3 需要只对 `_publish/manual/easel-<platform>-<12hex>.zip` 开窄例外。

## 5. 旧全局状态与 control 归属映射

| 旧状态 | 原位置 | control/worker 归属 |
|---|---|---|
| 发布状态/验证码 | `outputs/_publish/{platform}.json/.code` | 每个 worker 一份本人 outputs；新 Run 使用 `_publish/runs/<runId>/`，不读共享旧状态 |
| 发布日志 | `outputs/_publish.log` | 本人 outputs；不得写秘密/上游原文 |
| 日历记录 | `outputs/_schedule.json` | 5B 已按 workspace |
| 平台 profile | `~/.easel-browser-profiles/<platform>/default` | worker HOME；`browser_state.ProfileLock` 跨进程互斥 |
| 浏览器进程 | `web.app._BROWSER_PROCESSES` | worker 进程内登记；取消/超时按进程树终止 |
| 发布草稿 | 浏览器 `localStorage['easel_publish_draft']` | 原为全局键；control 必须按 user/workspace 分组，不能读旧全局草稿 |
| 发布策略 | worker `ENV_FILE` | control 受管配置；成员只读 |

## 6. 现有任务/路由装配

- `easel/control/tasks.py`：`publish` 类型已存在，`implemented=False`；路由含 `POST /api/publish/{p}`、`POST /api/publish/{p}/sms`、`POST /api/manual-publish/{p}/export`。
- `easel/control/policy.py`：发布/导出/状态/短信均已按 TASK 或 WORKSPACE_READ 精确登记。
- `easel/control/proxy.py`：`_BROWSER_TASK_TYPES = {browser, analytics}`；publish 未纳入，`_browser_action_timeout` 无 publish/manual-export。
- `easel/worker/browser_tasks.py`：`BROWSER_TASK_TYPES = {browser, analytics}`；动作表无 publish/manual-export。
- `easel/gateway/app.py`：workspace 路由白名单无发布短信/导出下载例外。
- 前端：`WorkspaceApp` 的 `publish` 页仍是 `UnavailablePage`；三页面直接 import `lib/api`，未走 `WorkspaceData`。

## 7. 现有测试与基线

- `tests/test_manual_publishing.py`：策略默认/手动、导出 reviewed、不标 published、路径穿越、公众号 HTML 安全、自动发布子进程命令。
- `tests/test_web_publisher_assets.py`：频道资源拦截与登录/发布不被错误过滤。
- `tests/frontend/publishingPage.test.mjs`：策略默认、手动禁用自动、两路径切换、异常重试、卸载后不更新。
- `tests/frontend/publishPageLifecycle.test.mjs`：卸载停止轮询/中断流。
- 5C2 既有链/浏览器/容器证据需保持；5C3 新增测试不得破坏父基线。

## 8. 5C3 实施结论（接口表摘要）

1. control 新增受管发布适配层：策略只读、发布/导出按 `publish` 任务进入既有 `Store.reserve_run`/签名/Relay/RunManager/终态；短信短请求绑定当前 Run。
2. worker 新增 publish 执行器：严格解析签名描述、校验并快照本人媒体、固定平台命令白名单、复用进程树取消与 profile 锁、成功后才记录日历。
3. 导出仍生成本人 outputs 内 ZIP，`published=false`；媒体下载仅放行导出包精确形状。
4. 前端三页面改走 `WorkspaceData`；草稿按 identity/workspace 分组；轮询/停止/短信/迟到响应按 Run 归属校验。
5. local 保持原 `lib/api` 行为；control 不直接落 local client。
