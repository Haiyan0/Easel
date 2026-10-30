# Task 5C1 证据：技能目录/执行与画像创建/增强

日期：2026-10-30（Asia/Shanghai）。
父基线：`710d673bc1458c19e3f4d7301fc4d7680bd22fce`。
实施 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`。
本组只实现 5C1；账号/分析/发布/导出仍属 5C2/5C3，未进入。

## 范围与接口

| 能力 | 调用路径 | 处理 |
|---|---|---|
| 技能目录 | `GET /api/skills` | control 经 gateway/worker 转发固定只读 `code_root/skills/builtin` 目录 |
| 技能详情 | `GET /api/skill/{name}` | 固定目录 + SKILL.md；不创建 Run |
| 技能执行 | `POST /api/skill` | control 先调用 worker `/api/skill/prepare` 校验并生成 prompt；再用既有 `WorkerRelay.submit_task(task_type=skill)` 接纳 |
| 技能状态/刷新 | `GET /api/skill/status/{name}` | 按本人任务会话定位最新 Run；终态才读取 worker 消息结果 |
| 画像基线 | `POST /api/profile/build` | worker `/api/profile/baseline` 确定性写六维基线；随后以 `task_type=profile` 接纳增强 Run |
| 画像增强状态 | `GET /api/profile/build/status/{name}` | 读取本人 profile 任务会话最新 Run，必要时用 events 同步终态 |

任务会话 id 由 `workspace_id + task key` 确定性派生，任务不再另建调度器；
skill/profile 共用既有 `Store.reserve_run`、受管 revision、签名快照、gateway 和
worker RunManager。`tasks.py` 只把 skill/profile 标为已装配，普通输入仍经
`_validate_task` 白名单。

## 实现与安全边界

- 技能名/画像名由服务端固定目录解析；控制面拒绝路径分隔符、控制字符和
  非法空名；浏览器不能提交绝对路径、命令或上游 URL。
- `/api/skills` 的 `apiConfigured` 只来自 worker 本人 workspace `.env`，不读取
  control 宿主环境；control 的 SkillDrawer 不提供全局 `.env` 保存入口。
- 画像基线在本人 `profiles/<name>` 创建；目录链接/越界/冲突拒绝，Unicode
  名字可用，六维文件写入完成后才返回可用基线。
- skill/profile 的 prompt 由 worker 准备接口生成，再由 control 签名 Run；
  不接受浏览器拼接的 worker 路径/命令。
- 同一画像增强运行期间，control 对画像文件保存和删除返回
  `profile_busy` 409，避免编辑/删除与迟到增强互相覆盖；基线已成功但增强
  接纳失败时，响应明确区分 `created=true` 与 `status=failed`。
- 前端 SkillPage/SkillDrawer 和 OnboardingWizard 使用 WorkspaceData context；
  control 任务使用 identity epoch/CSRF/Abort；SkillDrawer 支持停止，刷新后
  通过 status 找回原 Run/结果，不重新派发 prompt。
- 5B 的工作台、5A 的基础工作区和账号 5C2 未接通说明保持真实；本组不启用
  账号/分析/发布。

## 红绿与回归

行为测试在实现链路建立后运行；此前 control 的 skill/profile 任务表为
`implemented=False`，对应入口明确 503。5C1 接入后：

| 项目 | 结果 |
|---|---|
| control task/worker skill/profile 定向 | **7 passed** |
| gateway/policy/task registry + 5B workbench | **178 passed** |
| `tests/multiuser` 全量 | **1613 passed, 6 skipped** |
| 全部 frontend Node | **146 passed, 0 failed** |
| build | 成功，`dist/assets/index-t86hh5YP.js` |
| lint | 退出 0 |
| Task 5C1 浏览器 | **4/4，3 次 `FAILURES=[]`** |
| Task 5B 浏览器回归 | **5/5，3 次 + 最终 1 次** |
| Task 5A 浏览器回归 | **4/4，3 次 + 最终 1 次** |
| Task 3 浏览器回归 | **33/33，3 次 + 最终 1 次** |
| Task 4 浏览器回归 | **8/8，3 次 + 最终 1 次** |
| 5C1 隔离容器链路 | **`TASK5C1_CONTAINER_OK=True`，`CLEANUP_ERRORS=[]`** |

已有 `tests/test_bugfix_regressions.py` 的 Windows `sys.executable` 路径分隔符
反例仍与该无关用例一致；本组未修改该项。

## 浏览器

`tests/frontend/browser_original_web_task05c1.py` 使用真实 dist、真实
control/gateway/worker HTTP、真实 RunManager 与受控 ACP 执行器（夹具层，
不调用真实供应商）：

1. 技能库目录/详情；执行 copywriting；结果可见。
2. 刷新后重新打开同一技能，status 找回上次结果；执行次数不增加。
3. 画像向导创建基线六维，增强 Run 接纳；文件落本人 profiles。
4. 技能运行中停止；终态为 cancelled，原页面显示停止错误且保留输入。
5. 深色/移动布局。

截图：`browser/01-skill-run-light.png` 至
`browser/06-skill-stop.png`。

## 隔离容器

`tests/multiuser/task5c1_container_chain.py` 使用真实 control/gateway/worker
容器、真实 Ed25519 gateway、真实 HTTP、真实 Store/Run 账本；worker 执行器
是夹具内的假 ACP 替身，明确不声称真实 Harness/供应商通过。覆盖：

- 技能目录、详情、prepare、受控 Run、结果消息；
- 画像基线六维、增强 Run、状态、本人卷文件；
- 未知技能 404；
- worker 重启后 skill/profile 状态与结果仍可定位；
- 5B 的基础 CRUD/媒体/上传/摘要回归；
- 匿名 401；随机标签资源清理。

最终镜像：

- control `easel-control:task5c1-20261030`
  `sha256:c0e6d9c3e91f5a29857f31e0d234824a903a5693dbc180eae6692d37736da783`
- gateway `easel-gateway:task5c1-20261030`
  `sha256:30126e515dd2b256276030ba495e6652aded7e8adf4fe802be2b0edff93bdb07`
- worker `easel-worker-execution:task5c1-20261030`
  `sha256:156582dab61b20dc8e35180028b3a2beae321e6e53a7cc9ba3081ee6a048384e`

## 未执行与清理

- 真实 Harness/真实 provider、真实模型、账号登录/分析、发布/导出：
  **not-run**（属 5C2/5C3 或后续真实通道）。
- 公网热点仍 not-run；本组不重新验证公网来源。
- 完整 Compose、生产 Linux、最终部署未执行；不做无缓存全栈重建。
- 5A/Task 3/Task 4/Task 5B 历史截图运行后已恢复；5C1 截图输出独立目录。
- 用户 control/gateway/worker/sidecar、原卷/socket、Key/Cookie/邀请未操作；
  未重放 prompt、未调用付费模型、未发布、未全局 prune。
- 隔离测试容器/网络/卷按标签清理，最终无残留。

## 文件

生产：

- `easel/control/workspace_tasks.py`（新增）：技能准备/基线/任务会话/Run 适配与状态。
- `easel/control/app.py`：5C1 路由、技能/画像任务控制面、画像忙碌保护。
- `easel/control/store.py`：确定性任务会话。
- `easel/control/tasks.py`、`policy.py`：skill/profile 任务装配与精确路由。
- `easel/gateway/app.py`：skills/detail/prepare/baseline 精确转发。
- `web/app.py`：技能 prepare、画像基线安全创建。
- `web/frontend/src/lib/api.ts`、`workspaceData.ts`、`workspaceDataContext.ts`：
  技能/画像类型与 control/local 适配。
- `web/frontend/src/hooks/useWorkspace.ts`：任务 SSE 等待与停止/结果。
- `web/frontend/src/components/WorkspaceApp.tsx`、`SkillPage.tsx`、
  `SkillDrawer.tsx`、`OnboardingWizard.tsx`：原页面接线。

测试：

- 新增 `tests/multiuser/test_workspace_tasks.py`
- 新增 `tests/multiuser/test_worker_skills_profile.py`
- 新增 `tests/frontend/browser_original_web_task05c1.py`
- 新增 `tests/multiuser/task5c1_container_chain.py`
- 修改 `tests/multiuser/test_original_web_api.py`、
  `test_gateway_workspace.py`、`test_task_leases.py`、
  `tests/frontend/workspaceData.test.mjs`
- 更新 Task 3/5A 浏览器中对“技能暂未接通/画像新建禁用”的过期断言
