# Task 5B 工作台数据证据：热点、选题、日历与概览

日期：2026-10-30（Asia/Shanghai）。
父基线：`7b9d76f73dd730f63b74a63f3cd2e81c9436dbef`（Task 5A R1 验收 tip）。
实施 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。
范围：只实施 Task 5B；不进入 5C/6，不自动上线，不操作用户实例。

## 交付内容

- `GET /api/trends`：control 校验固定平台与 `limit`，gateway 精确签名，worker
  使用原固定来源与公开缓存。
- `GET/POST /api/ideas`、`PUT/DELETE /api/ideas/{id}`：本人
  `outputs/_ideas.json`，锁内读改写、随机临时文件原子替换。
- `GET/POST /api/schedule`、`PUT/DELETE /api/schedule/{id}`：
  本人 `outputs/_schedule.json`，同上。
- `GET /api/schedule/context?days=...`：固定 `calendar_ops.py` 与本人
  `--data` 路径；脚本失败/损坏存储明确报错，不返回假空摘要。
- 原 `DashboardPage` 汇总 5A outputs 与本组数据；账号/分析显示“5C 未接通”，
  control 不请求 `/api/accounts`、`/api/analytics`、`/api/admin`，不读取本机
  分析缓存，不伪造 0/0。
- 原 `TrendsPage`、`IdeasPage`、`CalendarPage` 通过 `WorkspaceData` context
  接线；local 保持原行为。
- “做内容”在 control 只导航并预填草稿，不自动提交 Run/prompt。
- 旧 `lib/api` 直接调用盘点与逐项对照见 `interface-table.md`。

## 红测与绿测

### 红测（实现前实际运行）

```text
C:\Users\hyh\anaconda3\envs\Agent\python.exe -m pytest \
  tests/multiuser/test_original_web_api.py -q -k "workbench or remaining_groups"
=> 6 failed, 2 passed, 1 skipped
失败：control 5B 路由仍返回 503 capability_unavailable；损坏 JSON 被当空列表；
      并发选题/排期会丢记录；context 使用旧模块常量路径而非本人临时根。
```

```text
node --test tests/frontend/workspaceData.test.mjs
=> 3 failed
失败：control 缺少 workbenchEnabled/accountsEnabled；fetchIdeas/fetchSchedule/
      fetchTrends/fetchScheduleContext 等尚未实现。
```

网关新增参数化用例与实现同批提交；绿测见
`tests/multiuser/test_gateway_workspace.py`（59 passed），本记录不把未单独
运行的网关红测宣称为已运行。

### 绿测（最终提交代码）

| 项目 | 结果 |
|---|---|
| 定点 control/worker 5B | **10 passed, 1 skipped**（skip 为 Windows 文件 symlink） |
| gateway workspace | **59 passed** |
| `tests/multiuser` 全量 | **1602 passed, 6 skipped** |
| 全部 frontend Node | **144 passed, 0 failed** |
| `npm --prefix web/frontend run build` | 成功，`dist/assets/index-eIaaBOlh.js` |
| `npm --prefix web/frontend run lint` | 退出 0 |
| control static + persona security | **22 passed** |
| `tests/test_trends.py` + manual publishing + bugfix 回归（排除无关用例） | **62 passed** |
| Task 5B 浏览器 | **4/4，5 次 `FAILURES=[]`** |
| Task 5A 浏览器回归 | **4/4，3 次 `FAILURES=[]`** |
| Task 3 浏览器回归 | **33/33，3 次 `FAILURES=[]`** |
| Task 4 浏览器回归 | **8/8，3 次 `FAILURES=[]`** |
| 5B 隔离容器链路 | **`TASK5B_CONTAINER_OK=True`，`CLEANUP_ERRORS=[]`** |

`tests/test_bugfix_regressions.py` 的
`test_video_pipeline_gates_empty_phase_fails_and_python_placeholder` 在
Windows 上因 `sys.executable` 反斜杠与脚本内正斜杠比较失败；该脚本/测试不在
本组 diff 中，属既有路径分隔符问题。已单独运行其余 62 项通过，不修改该项。

## 浏览器矩阵

`tests/frontend/browser_original_web_task05b.py` 使用真实 dist、control
Auth/Store、真实 Ed25519 gateway 和真实 `web.app` worker handler；A/B 使用
独立临时 workspace/数据根。热点在夹具内固定为受控来源，仅证明接线，不冒充
公网来源已通过。

覆盖：

1. 工作台加载热点/选题/排期/5A 产物；热点收藏→选题→编辑状态→加入日历→
   日历编辑/删除→刷新→概览一致。
2. “做内容”只跳转并预填；断言确认前没有新增 `/api/chat` 请求或用户消息。
3. A 慢 `/api/ideas` 响应期间注销并登录 B；B 页面不出现 A 数据。
4. 成员不请求 accounts/analytics/admin/skills/profile-build/publish/login；
   账号页明确“未接通”。
5. 浅色/深色/移动布局；无横向溢出。

截图：`browser/01-dashboard-light.png` … `09-dashboard-mobile.png`。
日志：`browser/browser-run1..3.txt`、`browser-final.txt`、`browser-final2.txt`
（均有 `FAILURES=[]`、`TASK5B_BROWSER_OK True`）。

## 隔离容器链路

脚本：`tests/multiuser/task5b_container_chain.py`。随机 run-id 标签与
独立 network/volume；清理前 inspect 归属校验。真实验证：

- 5A 既有 status/画像/内容/媒体 Range/HEAD/上传/删除；
- 选题 GET/POST/PUT/DELETE 与磁盘内容；
- 日历 GET/POST/PUT/DELETE、context 摘要；
- 未知 id 404；
- `docker restart worker` 后从本人卷读回选题并再次取得摘要；
- 删除后记录消失；匿名返回 401。

最终镜像：

- control `easel-control:task5b-20261030`
  `sha256:785135a95e702a5978464063a9fbc4cbd476ad4494192f168196d70c6df4633e`
- gateway `easel-gateway:task5b-20261030`
  `sha256:3a46f30375c808966c4188ac3a11bab7087dd9f8d398911cee63db25fc15bca7`
- worker `easel-worker-execution:task5b-20261030`
  `sha256:73901f9e86dbaeb3a3db7318f0e54a8b20f6cf3e0ec22d0254682ae12aabc310`

worker 仍基于既有 execution 镜像叠加当前 `easel/`、`web/`，未重建
Harness/Chromium。证据：`container/container-final.txt`、
`container/images-final.txt`，构建日志 `container/*-build-final.txt`。

## 未执行与清理

- 热点公网来源：**not-run**。浏览器/后端使用受控来源；未声称公网已通过。
- Windows 文件 symlink：环境不允许创建（既有 `WinError 1314` 限制），
  已用真实目录占位测试拒绝目录；真实 junction/symlink 边界仍由
  `_workbench_file` 逐文件解析检查，未用 mock/hardlink 冒充。
- 真实模型、真实账号登录/分析、发布/导出：属于 5C，not-run。
- 完整 Compose/生产 Linux/最终部署：未执行。
- 5A/Task 3/Task 4 的历史截图运行后已恢复；5B 截图输出独立目录。
- 用户 control/gateway/worker/sidecar、原卷/socket、Key/Cookie/邀请未操作；
  未重放 prompt、未调用付费模型、未发布、未全局 prune、未删除历史网络。
- 5B 隔离容器/网络/卷按标签归属清理，最终检查无残留。
- 5A 浏览器夹具仍报告 `close_clients` 未 await 的清理 RuntimeWarning（既有
  测试清理质量项，功能矩阵成功）；5B 夹具已改为 async stop，无该警告。

## 文件

生产：

- `easel/control/app.py`：5B control 路由、查询/JSON 校验、CSRF/身份转发。
- `easel/control/workspace_api.py`：工作台 id/字段/平台校验辅助。
- `easel/gateway/app.py`：5B 精确路由、id/平台/days 查询边界。
- `web/app.py`：本人工作台存储边界、锁、原子写、损坏/链接拒绝、固定日历摘要。
- `web/frontend/src/lib/workspaceData.ts`：control/local 工作台数据适配器与解析。
- `web/frontend/src/lib/workspaceDataContext.ts`：既有 context。
- `web/frontend/src/components/WorkspaceApp.tsx`：原页面接线与“做内容”预填。
- `web/frontend/src/components/ChatPage.tsx`：草稿预填/消费。
- `web/frontend/src/components/DashboardPage.tsx`：概览与 5C 未接通表达。
- `web/frontend/src/components/TrendsPage.tsx`：热点接线/加载/失败/收藏。
- `web/frontend/src/components/IdeasPage.tsx`：选题接线/失败保留编辑。
- `web/frontend/src/components/CalendarPage.tsx`：日历/摘要接线/失败保留编辑。

测试：

- 修改 `tests/multiuser/test_original_web_api.py`。
- 修改 `tests/multiuser/test_gateway_workspace.py`。
- 修改 `tests/frontend/workspaceData.test.mjs`。
- 新增 `tests/frontend/browser_original_web_task05b.py`。
- 新增 `tests/multiuser/task5b_container_chain.py`。

证据目录：本目录 `interface-table.md`、`browser/`、`container/`、
`regression/`。
