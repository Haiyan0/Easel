# Task 5C3 验收证据：原发布工作台与稿件导出

父基线：`fcb351d532301d22afe3be0c91373b19f3505ca7`
实施 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`
实施提交：`<待提交后回填>`
日期：2026-11-01

## 1. 盘点与接口表

见 [inventory.md](inventory.md)。覆盖：

- 原三页面（PublishingPage / ManualPublishPage / PublishPage）实际调用；
- publishing-policy、manual-publish export、publish/status/sms、outputs/media、AI 适配/检查、日历回流；
- 发布策略真实来源（worker `.env`）与 control 受管替代；
- `_publish` 状态/验证码、`_schedule.json`、浏览器 profile 锁、草稿 localStorage 的归属；
- 平台脚本、媒体规则、成功判定与读回语义；
- 已发现的旧缺陷：导出 ZIP 位于 `_publish` 下，原 `/api/media` 实际无法下载。

## 2. 实现摘要

### control

- 新增 `easel/control/workspace_publishing.py`：
  - 策略来自 `ControlConfig.publish_mode`（CLI `--publish-mode`，默认 `automated`），不从 worker/宿主 `.env` 读取；成员无写入口。
  - 发布/导出统一走 `Store.reserve_run`、WorkerRelay、签名快照、RunManager、容量与终态。
  - 同平台在途 Run 只允许同 `requestId` 加入；不同请求 409，不开启第二次外部动作。
  - 所有平台统一“先接纳、后轮询状态”：HTTP 断连/超时/刷新仍能定位当前 Run，避免重复外部动作。
  - 状态只读取当前 Run 自己的 `browser_status` 事件；done 才读结果，取消/中断/失败/未知明确返回 `unknown`，不伪造 success。
  - 短信验证码只交给当前本人平台 Run 且该 Run 处于 `sms_required`。
  - 导出结果必须是 `published=false` 的精确包路径。
- `app.py`/`tasks.py`/`proxy.py`/`cli.py`：装配路由与任务类型；发布属 BROWSER 任务，不走模型 broker。
- `workspace_api.py`：媒体下载只对 `_publish/manual/easel-<platform>-<12hex>.zip` 开窄例外。

### worker

- `easel/worker/browser_tasks.py`：严格解析 `publish` / `manual-export`，字段/长度/平台/媒体扩展名白名单。
- `web/app.py`：
  - 媒体在 Run 开始时复制到 `_publish/runs/<runId>/media/`，签名后原文件变化不影响本次快照；
  - 固定平台命令重建，复用 `_run_tracked_subprocess`/`_kill_process_tree`；
  - `_execute_managed_publish` 轮询本次 Run 自己的状态文件，取消/超时按进程树终止；
  - 成功后才记录一次日历；日历失败与平台成功分别返回；
  - 日志只写 platform/run/state，不写标题/正文/路径/凭证/上游原文；
  - 抖音验证码文件按 Run 注册，共享 `_publish/<platform>.code` 不再承载受管新任务；发布状态/短信只读当前 Run。
- `easel/manual_publish.py`：导出目录必须 resolve 在 outputs 内；ZIP 内媒体名净化；失败只清理本次包。

### 前端

- 三页面改走 `WorkspaceData`；local 保持原 `lib/api` 行为。
- 草稿按 `userId:workspaceId` 分组；control 不回退旧全局 `easel_publish_draft`。
- AI 适配复用 `content-repurposing` SKILL Run；AI 检查复用 `quality-gate`/`persona-check`；停止走同一 Run stop。
- 自动发布轮询按 `runId` 丢弃迟到回应；未知结果显式显示“待核对/勿自动重发”；停止先标未知再发停止请求；刷新按当前 Run 恢复卡片/短信/轮询。
- 人工“已发布”记录继续注明“人工确认，未自动验证平台状态”。

## 3. 红测与绿测

红灯（实施前，命令退出非 0）：

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser/test_workspace_publishing.py -q
# ModuleNotFoundError: No module named 'easel.control.workspace_publishing'

& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser/test_publish_worker.py -q
# AttributeError: module 'web.app' has no attribute 'run_managed_publish'

node --test tests/frontend/publishWorkspaceData.test.mjs tests/frontend/publishingPage.test.mjs
# 发布适配方法与页面数据上下文缺失，测试失败
```

绿灯（本会话实际运行）：

```powershell
# tests/multiuser/test_workspace_publishing.py
# tests/multiuser/test_publish_worker.py
# tests/multiuser/test_task5c3_chain.py
# 以及 5A/5B/5C1/5C2 受影响后端回归
443 passed, 1 skipped
333 passed, 1 skipped
```

原始输出：[backend-tests.txt](backend-tests.txt)。两批文件不重叠，共 **776 passed、2 skipped**。

```powershell
node --test tests/frontend/*.test.mjs
# 166 passed, 0 failed
```

原始输出：[node-tests.txt](node-tests.txt)。

```powershell
npm --prefix web/frontend run build   # 退出 0，index-DXvNQgIN.js
npm --prefix web/frontend run lint    # 退出 0
```

原始输出：[build-lint.txt](build-lint.txt)。仅有既有 chunk >500kB 警告。

## 4. 浏览器矩阵

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' tests/frontend/browser_original_web_task05c3.py
# TASK5C3_BROWSER_OK True，5 场景全部通过
```

场景：

1. `manual-export-review-calendar`：原发布工作台→素材/草稿→AI 排版→人工审核→真实下载 ZIP 并解包→编辑后审核失效→重新审核→日历草稿→人工确认发布记录；浅色/移动/深色截图。
2. `policy-automated`：默认自动路径可用，自动页保留。
3. `auto-publish-sms-stop-refresh`：受控自动发布→短信错误码不完成→正确码成功→查询状态暂时失败一次后恢复且不补发→停止本地执行标未知→刷新找回原 Run 并继续短信。
4. `duplicate-submit-once`：连续双击只产生一个受控发布子进程。
5. `policy-manual`：管理员手动策略禁自动页，只保留人工路径。

原始输出：[browser-matrix.txt](browser-matrix.txt)。截图：[browser/](browser/)。

浏览器替身说明：发布子进程与技能执行器为受控替身，不访问真实平台/模型；页面、control、gateway、WorkerRelay、RunManager、ZIP 导出与下载为真实层。

## 5. 隔离容器闭环

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' tests/multiuser/task5c3_container_chain.py --build
# TASK5C3_CONTAINER_OK True；CLEANUP_ERRORS []
```

原始输出：[container-chain.txt](container-chain.txt)。

- 增量构建三个镜像（基于 5C2 已验收镜像，不无缓存重建全栈）：
  - control `sha256:bd5cbcce5b4dbbedd87d1e11efa25dd57ac79422a22f8c2ebf9faa2be9d2368d`
  - gateway `sha256:1f0dcc60feecb816d492dd685b9b85bd353add28d16d30293ab89737da7ded06`
  - worker `sha256:55c9482fbb21107fefb2b0829053f7dc8c133535779114a48dc3c7b8faadbd2b`
- 镜像内源码/前端哈希与当前源码一致（见 container-chain.txt 的 `SOURCE_HASH`）。
- 真实层：固定身份/签名、control/gateway/worker、Gateway、WorkspaceApi、WorkerRelay、RunManager、受管发布执行器、镜像内受控发布子进程 + Playwright Chromium + `browser_state.ProfileLock`、真实 ZIP 导出/下载、进程树取消、worker 重启。
- 替身层：原平台脚本未执行；`xhs_publish.py`、`douyin_publish.py`、`web_publisher.py`、`weixin_mp_stats.py`、`biliup` 单项列为 substituted，不声称真实平台通过。
- 检查项全部 true：导出下载/解包、一次提交、日历一次、抖音短信归属、停止释放、超时释放、查询失败恢复、重启不重放、受控 runner 实际执行。
- 清理按标签归属删除容器/网络/卷，`CLEANUP_ERRORS []`；未操作运行中用户实例。

## 6. 父基线浏览器回归

以下为本次实际运行结果（不覆盖历史证据；运行后已用 `git checkout -- docs/evidence` 恢复被脚本重写的旧截图）：

- 5C2：6/6 场景通过（login_flow、wechat_states、cross_site_analytics、late_stop_modal、analytics、stop_503_401）。
- 5C1：7/7 通过（skill_run、profile_wizard、skill_question、skill_status_recovery、profile_question_stop、skill_stop、dark_mobile）。
- 5B：5/5 通过（workbench_flow、identity_isolation、member_accounts_connected、stale_trends、dark_mobile）。
- 5A：4/4 通过（outputs_media_delete_light、profile_and_attachment、dark_mobile、logout_media_401）。
- Task3：33/33 通过。
- Task4：8/8 通过。

## 7. 部署迁移注意

原策略来自 worker/宿主 `.env` 的 `EASEL_PUBLISH_MODE`；5C3 明确不读取它代替受管策略。control 新增 `--publish-mode automated|manual`（默认 `automated`，保留原默认）。**曾把 worker `EASEL_PUBLISH_MODE` 配成 `manual` 的现有部署，升级时必须显式给 control 传 `--publish-mode manual`**，否则 control 会按默认自动策略工作。成员没有修改策略的入口；策略变更由管理员重新部署/配置 control。

## 8. 未执行/边界

- 未执行真实平台发布、真实短信、真实账号扫码/凭证、真实付费模型；只使用受控本地平台/受控发布 runner。
- 受控平台成功不等于真实平台通过；真实发布/收费/账号状态变化需用户另行授权。
- 容器测试使用 `seccomp=unconfined` 运行镜像内 Chromium，未验证生产 Linux 默认 seccomp/网络拓扑；既有 Linux 生产门槛保留。
- 未运行完整顶层 pytest；既有平台/顺序失败未在本轮声称修复。
- 未操作/复制用户运行容器、卷、socket、Key、Cookie、邀请、HOME 或真实账号状态；未合并、未推送、未部署。
- Playwright Windows 关闭回调 `WinError 10054` 为既有运行噪声；前端 chunk >500kB 为既有构建警告。

## 9. 测试调整记录

`tests/multiuser/test_task5c2_chain.py` 在批量顺序下会出现“浏览器状态事件已 success，但 control 账本尚未落 done”的短窗口。测试改为有界等待终态（最多 5s），不改变产品逻辑；单独复跑原测试仍通过。此调整属于测试时序健壮性，不改变 5C2 验收结论。

## 10. 文件职责

| 文件 | 职责 |
|---|---|
| `easel/control/workspace_publishing.py` | 受管发布/导出/状态/短信与策略 |
| `easel/control/app.py` | 装配路由、策略、发布服务 |
| `easel/control/tasks.py` | publish 任务启用 |
| `easel/control/proxy.py` | publish 纳入 BROWSER 任务与时限 |
| `easel/control/workspace_api.py` | 导出包媒体路径窄例外 |
| `easel/cli.py`、`easel/control/cli.py` | `--publish-mode` 受管配置 |
| `easel/gateway/app.py` | 发布短信路由、导出包下载窄例外 |
| `easel/worker/browser_tasks.py` | 严格发布/导出动作解析与执行分发 |
| `web/app.py` | 媒体快照、受管发布子进程、Run 状态、导出、进程树清理、日历一次 |
| `easel/manual_publish.py` | 导出目录边界、ZIP 媒体名净化 |
| `web/frontend/src/lib/workspaceData.ts` | local/control 发布、导出、AI、状态适配 |
| `web/frontend/src/lib/store.ts` | 草稿按 identity/workspace 隔离 |
| `web/frontend/src/components/{PublishingPage,ManualPublishPage,PublishPage}.tsx` | 原页面接入、审核失效、Run 归属、未知结果、刷新恢复、停止 |
| `web/frontend/src/components/WorkspaceApp.tsx` | control 发布页装配 |
| `web/frontend/src/styles/index.css` | 未知发布状态样式 |
| `tests/multiuser/test_workspace_publishing.py` | control 策略/归属/一次派发/短信/导出 |
| `tests/multiuser/test_publish_worker.py` | 解析、媒体快照、进程树、导出边界/下载 |
| `tests/multiuser/test_task5c3_chain.py` | 真实 control/gateway/worker 链路 |
| `tests/frontend/publishWorkspaceData.test.mjs`、`publishDraftScope.test.mjs` | 前端适配/草稿隔离 |
| `tests/frontend/browser_original_web_task05c3.py` | 浏览器矩阵 |
| `tests/multiuser/task5c3_container_chain.py`、`task5c3_controlled_publisher.py`、Dockerfile.*5c3 | 容器闭环 |
