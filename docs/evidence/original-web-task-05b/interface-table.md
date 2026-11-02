# Task 5B 接口对照表

日期：2026-10-30。父基线：`7b9d76f73dd730f63b74a63f3cd2e81c9436dbef`。
实施 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。

## 组件 → 请求 → handler → 边界 → 存储

| 原组件 | method/path/query/body | control 入口 | policy | gateway | worker handler / 存储 | 返回形状 | 测试 |
|---|---|---|---|---|---|---|---|
| `DashboardPage` | `GET /api/trends?platforms=weibo,douyin&limit=6` | `workspace_trends` | `WORKSPACE_READ` | `ws_trends`，白名单 `platforms`/`limit`，签名 query | `web.api_trends` → 固定 `TREND_SOURCES`，公开缓存 `_TREND_CACHE` | `{trends:[{platform,label,items,ok}],updated}` | 5B 控制面/网关；浏览器；`tests/test_trends.py` |
| `TrendsPage` | `GET /api/trends?platforms=...&limit=15` | 同上 | 同上 | 同上 | 同上 | 同上 | 同上 + 浏览器收藏 |
| `TrendsPage` | `POST /api/ideas`，JSON `{title,note,source,status}` | `workspace_idea_create` | `WORKSPACE_WRITE` | `ws_ideas`，POST；body 逐字节签名 | `web.api_ideas_create` → 本人 `outputs/_ideas.json` | `Idea` | 控制面过滤字段、CSRF；浏览器热点收藏 |
| `DashboardPage`/`IdeasPage` | `GET /api/ideas` | `workspace_ideas` | `WORKSPACE_READ` | `ws_ideas`，GET | `web.api_ideas_list` → 本人 `_ideas.json` | `Idea[]` | A/B 同 id；损坏/目录/并发；浏览器 |
| `IdeasPage` | `PUT /api/ideas/{12hex}`，JSON `{title,note,source,status}` | `workspace_idea_update` | `WORKSPACE_WRITE`，资源 `idea:1` | 动态 `ws_idea_item`，仅 PUT/DELETE；id 严格校验 | `web.api_ideas_update` → 原子写本人 `_ideas.json` | `Idea` | 控制面 404/400；A/B；浏览器编辑/失败重试 |
| `IdeasPage` | `DELETE /api/ideas/{12hex}` | `workspace_idea_delete` | 同上 | 同上 | `web.api_ideas_delete` | `{ok,deleted}` | A/B；未知 id 404 |
| `DashboardPage`/`CalendarPage` | `GET /api/schedule` | `workspace_schedule` | `WORKSPACE_READ` | `ws_schedule`，GET/POST | `web.api_schedule_list` → 本人 `_schedule.json` | `ScheduleItem[]` | A/B；损坏/目录；浏览器 |
| `IdeasPage`/`CalendarPage` | `POST /api/schedule`，JSON 字段见合约 | `workspace_schedule_create` | `WORKSPACE_WRITE` | `ws_schedule`，POST；body 签名 | `web.api_schedule_create` → 锁内原子写 `_schedule.json` | `ScheduleItem` | 控制面字段过滤；并发；浏览器加入日历 |
| `CalendarPage` | `PUT /api/schedule/{12hex}`，JSON 字段见合约 | `workspace_schedule_update` | `WORKSPACE_WRITE`，资源 `schedule:1` | 动态 `ws_schedule_item` | `web.api_schedule_update` → 锁内读改写 | `ScheduleItem` | A/B；浏览器编辑/删除 |
| `CalendarPage` | `DELETE /api/schedule/{12hex}` | `workspace_schedule_delete` | 同上 | 同上 | `web.api_schedule_delete` | `{ok,deleted}` | 未知 id 404；浏览器删除 |
| `DashboardPage`/`CalendarPage` | `GET /api/schedule/context?days=14` | `workspace_schedule_context` | `WORKSPACE_READ` | `ws_schedule_context`，白名单 `days` 1..90 | `web.api_schedule_context` → 固定 `skills/shared/scripts/calendar_ops.py`，固定本人 `--data`；失败明确 503 | 摘要 JSON | A/B；固定命令/路径；损坏存储；浏览器建议 |
| `DashboardPage` | `GET /api/outputs`（5A） | `workspace_outputs` | `WORKSPACE_READ` | `ws_outputs` | `web.api_outputs` | `OutputNode[]` | 5A 回归；5B 概览 |
| `DashboardPage` | accounts/analytics | 控制模式不调用 | — | — | — | 显示“5C 未接通”，不发 old local 请求、不读分析缓存、不伪造 0/0 | 浏览器请求白名单 |
| `DashboardPage` | 无独立 overview API | 由上述 5A outputs + 5B trends/schedule/ideas 汇总 | — | — | — | 原页面真实数据 | 浏览器刷新/概览一致 |

## 字段与边界

- id：控制面和 gateway 都只接受 `^[0-9a-f]{12}$`；路径段不允许编码别名。
- 热点：只接受 `weibo,douyin,zhihu,bilibili,baidu,toutiao`，去重，最多 6 个；`limit` 1..30；不允许客户端上游 URL。
- 日历摘要 `days`：1..90；worker 仍保留原 1..90 clamp 作为第二道边界。
- JSON body：控制面最多 64KiB；仅保留合约字段；字符串有长度上限；拒绝 NUL/控制字符（换行、回车、Tab 除外）；拒绝非有限 JSON 常量。gateway 对最终序列化 body 逐字节签名。
- 存储：只读写 `OUTPUTS_DIR/_ideas.json`、`OUTPUTS_DIR/_schedule.json`；拒绝 symlink/junction/reparse point、目录、解析后越出 outputs 的文件；损坏 JSON 返回明确错误，不覆盖原文件；临时文件随机名 + `os.replace` 原子写；ideas/schedule 各自锁保护读改写。
- 私人性：热点缓存是公开数据；选题、日历、摘要和 Dashboard 私人状态全部来自本人 workspace。A/B 同名/同 id 不共享。
- 前端：原组件通过 `WorkspaceData` context/显式适配器访问；control 用 `identityFetchRaw`（身份 epoch/CSRF），local 保持原 `lib/api` 行为。`CalendarPage`/`IdeasPage`/`TrendsPage`/`DashboardPage` 均有 AbortSignal/迟到响应守卫；身份切换由 `WorkspaceApp` keyed remount 清空旧数据/编辑态。
- 原页面“做内容”：control 只导航到聊天并预填草稿，`ChatPage` 需用户确认后才走既有会话/Run 接纳；浏览器断言没有新 `/api/chat` 请求。
