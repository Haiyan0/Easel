# Task 5C2 账号、平台登录与分析 —— 实施与验收证据

日期：2026-11-01（Asia/Shanghai）。

- 父基线：`654305a69c1d68d6b157b608dbd2c7d5f96d6298`
- 实施提交：`0801343ddcdc4111af111108a5f15897ae1f1b43`
- 证据提交：`f05bd37`（本文件首次提交）
- 最终 tip：证据 SHA 记录提交（`docs: 记录 5C2 证据 SHA`）
- 实施 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`

## 1. 结论摘要

AccountsPage / DashboardPage 已恢复本人平台账号、二维码/短信/凭证登录、whoami 与分析展示，
local 行为保留（localStorage 缓存、原登录/分析 API、原组件布局）。control 不再直接调用
local handler：账号/分析经 WorkspaceData 适配，浏览器任务经既有按 Run 接纳、Store 容量、
签名短请求、worker RunManager、gateway 精确路由与终态/停止机制执行。

本组完成受控浏览器链路与隔离 ASGI control/gateway/worker 链路；未执行真实平台登录、
真实短信、真实 AppSecret、真实发布/导出或 Docker 容器闭环（见第 6 节 not-run）。
用户运行容器/卷/socket/Key/Cookie/邀请未操作，未切换用户实例。

## 2. 接口与资源盘点

见 [interface-inventory.md](interface-inventory.md)。要点：

- 账号列表、登录/MP 状态、凭证读取、分析平台列表走签名短请求；
- 登录、MP 登录、强制 whoami、分析刷新、凭证校验走既有 Run 接纳/容量/签名/终态；
- 短信、停止、注销绑定本人当前任务/平台，不新建 Agent prompt；
- 二维码只经固定平台/固定文件名的已审媒体入口；
- AppSecret 不进入 Run 输入快照/事件/日志，经一次短请求交接；验证失败不覆盖旧配置；
- 前端 control 的 whoami/分析缓存按适配器实例内存隔离，local 保留 localStorage。

## 3. 红测（父基线）与绿测

父基线红测：把本轮新增测试复制到父基线 worktree 运行，确保失败不是“事后编造”：

| 命令 | 结果 | 日志 |
|---|---|---|
| `pytest tests/multiuser/test_workspace_accounts.py tests/multiuser/test_worker_browser_tasks.py ...` | 退出 2：`No module named easel.control.workspace_accounts`、`browser_tasks` 不存在 | [baseline_red_backend.log](baseline_red_backend.log) |
| `pytest tests/multiuser/test_task5c2_security.py tests/multiuser/test_task5c2_chain.py`（父基线） | 5 failed / 2 errors：无二维码入口、无 `_wechat_verify_credentials`、无 Run 链路 | [baseline_red_security_chain.log](baseline_red_security_chain.log) |

绿测：

| 命令 | 结果 | 日志 |
|---|---|---|
| `pytest tests/multiuser/test_workspace_accounts.py` | 16 passed | [control_accounts_result.log](control_accounts_result.log) |
| `pytest tests/multiuser/test_worker_browser_tasks.py` | 9 passed | [worker_browser_tasks_result.log](worker_browser_tasks_result.log) |
| `pytest tests/multiuser/test_task5c2_security.py` | 5 passed | [security_result.log](security_result.log) |
| `pytest tests/multiuser/test_task5c2_chain.py` | 2 passed（真实 control/gateway/worker ASGI 链路） | [chain_test.log](chain_test.log) |
| `pytest tests/multiuser/test_workspace_accounts.py tests/multiuser/test_worker_browser_tasks.py tests/multiuser/test_task5c2_security.py tests/multiuser/test_task5c2_chain.py tests/multiuser/test_task_leases.py tests/multiuser/test_policy.py tests/multiuser/test_workspace_tasks.py tests/multiuser/test_original_web_api.py tests/multiuser/test_gateway_forward.py tests/multiuser/test_gateway_workspace.py` | 282 passed, 1 skipped | [regression_backend_final.log](regression_backend_final.log) |
| `pytest tests/test_login_start.py tests/test_login_transport.py tests/test_account_whoami.py tests/test_web_analytics.py tests/multiuser/test_browser_profiles.py` | 79 passed | [regression_original_accounts_final.log](regression_original_accounts_final.log) |
| `node --test tests/frontend/*.test.mjs` | 154 passed | [node_tests_final.log](node_tests_final.log) |
| `npm --prefix web/frontend run build` | 退出 0；仅既有 >500KB chunk 警告 | [frontend_build_final.log](frontend_build_final.log) |
| `npm --prefix web/frontend run lint` | 退出 0 | [frontend_lint_final.log](frontend_lint_final.log) |

## 4. 5C2 浏览器矩阵

`tests/frontend/browser_original_web_task05c2.py`：真实 React 构建、HTTPS control、
gateway、WorkspaceApi、WorkerRelay、worker RunManager；平台脚本入口为受控替身
（写本人临时 QR/状态/分析产物，页面从产物读取），真实 Chromium 驱动 UI。

| 场景 | 结果 | 断言要点 |
|---|---|---|
| login_flow | true | 账号列表→假二维码→短信（错误码保持待验证、正确码成功）→昵称/头像→刷新恢复；`login_starts` 只增一次；无 `easel_whoami_v4`/`easel_analytics` localStorage；浅色/深色/移动端截图 |
| wechat_states | true | 公众号“后台扫码未登录/AppID 凭证未配置”分清；凭证保存后只显示已配置；AppSecret 不回显；MP 扫码成功后后台扫码已登录 |
| analytics | true | 成功显示数值；刷新失败显示旧数据 + 真实 `fetched_at` + stale；重试成功清除 stale；执行次数 3 次（成功/失败/重试各一次） |
| stop_503_401 | true | 停止登录终止当前 Run，不新增浏览器；control 层一次 503 后轮询恢复；撤销会话 401 卸载私人页回登录 |

结果文件：[browser_results.json](browser/browser_results.json)；日志
[browser_run_final.log](browser_run_final.log)。截图 01–09 在
[browser/](browser/)；`stop_modal_failure.png` 为修复前调试截图，已从交付删除。

## 5. 既有浏览器矩阵回归

| 套件 | 结果 | 日志 |
|---|---|---|
| 5C1 技能/画像 7 组 | 7/7 true，`TASK5C1_BROWSER_OK True` | [regression_browser_original_web_task05c1.log](regression_browser_original_web_task05c1.log) |
| 5B 工作台 5 组（更新账号已接通语义） | 5/5 true | [regression_5b_updated.log](regression_5b_updated.log) |
| 5A 基础工作区 4 组 | 4/4 true | [regression_browser_original_web_workspace.log](regression_browser_original_web_workspace.log) |
| Task3 共用认证/会话 33 组（更新账号已接通语义） | 33/33 true | [regression_task3_updated.log](regression_task3_updated.log) |
| Task4 登录/主题 8 组 | 8/8 true | [regression_browser_original_web_login.log](regression_browser_original_web_login.log) |

回归脚本运行时使用 `EASEL_BROWSER_EVIDENCE` 指向本组 regression 子目录；5B/Task3 新增
截图在 `regression/5b`、`regression/task3`，未覆盖既有历史截图（历史目录已从 git 恢复）。

已知既有失败（非本组引入）：父基线上把
`tests/multiuser/test_worker_server.py` 与 `tests/multiuser/test_worker_execution_gate.py`
放在同一进程顺序运行时，3 个流式门禁用例因 Starlette/anyio 测试客户端顺序问题失败；
单独运行通过。父基线复现日志：[baseline_existing_order_failure.log](baseline_existing_order_failure.log)。
本组未改动这两个模块的执行逻辑，不据此宣称全仓通过。

## 6. 任务/路径/互斥/凭证证据

- A/B 同平台隔离：`test_workspace_accounts.py::test_a_b_same_platform_task_and_result_are_isolated`
  （不同 workspace 的 task conversation/Run/结果互不共享）；前端适配器实例内存隔离见
  `accountWorkspaceData.test.mjs::control whoami and analytics caches are per adapter identity`。
- 同平台互斥/重复接纳：`test_same_platform_second_request_reuses_active_run_without_dispatch`、
  `test_login_start_admits_one_browser_run_and_dedupes_same_request`；真实链路
  `test_chain_capacity_restart_and_stop` 覆盖容量满 429、restart 先停旧 Run、停止释放容量。
- SMS 归属：`test_sms_only_continues_own_current_pending_task`（本人 active + `sms_required`；
  外空间 404、旧/非当前 Run 409）。
- 停止/超时/worker 重启：`test_worker_restart_marks_run_interrupted_and_releases_capacity`、
  `test_explicit_restart_stops_old_run_before_dispatching_new_one`；worker
  `RunManager.stop` 取消 executor，executor 终止子进程树（`browser_tasks` + `web.app`
  `cancel_browser_task`/`_kill_process_tree`）。
- 注销不复活：`test_logout_stops_active_task_then_calls_worker_and_does_not_revive`；真实链路
  `test_real_chain_login_sms_whoami_analytics_credentials_logout` 注销后账号列表恢复未登录。
- 凭证：`test_credentials_verify_before_save_preserves_old_config`（失败不写 YAML）、
  `test_credentials_handoff_requires_active_run_and_masks_secret`（无 active Run 拒绝；
  只返回掩码；secret 不进入 Run 描述/事件）；文件写后 `chmod 0600`。
- 二维码/坏存储：`test_worker_qr_serves_only_fixed_platform_file`、
  `test_worker_qr_rejects_link_escape`（symlink/junction resolve 越界 404）、
  `test_gateway_allows_only_exact_account_routes`。
- 分析失败/旧缓存：`test_analytics_failure_is_not_fabricated_as_zero`（失败 502，不填零）、
  浏览器 analytics 场景 stale 标记 + 真实取数时间；Dashboard 不再为全部平台自动起浏览器，
  control 页面加载只对最多 2 个已登录平台自愈。
- 401：AccountsPage/Dashboard 对 `identityInvalidated` 401 调用适配器 `expireIdentity`，
  浏览器 stop_503_401 场景验证回登录页。

## 7. 隔离拓扑与清理

- 真实层：React/Vite 构建、HTTPS control、FastAPI gateway、Ed25519 签名、WorkspaceApi、
  WorkerRelay、worker RunManager、真实 Chromium（UI）。
- 替身层：平台登录/MP 登录/短信轮询/whoami/分析/凭证验证入口为受控函数，只写临时
  workspace 的 `_login`/`_analytics` 产物；不启动真实平台浏览器、不联网。
- 隔离层：临时 control SQLite、临时 workspace/state/home、随机端口与自签 TLS；
  运行结束删除 `.task5c2-browser`，未操作现有用户容器/卷/socket/Key/Cookie/邀请。
- 父基线对照 worktree 仅用于红测与既有顺序失败复现，现已删除，未复制其状态到交付。

## 8. not-run / 边界

- 未执行 Docker 容器控制面/worker/gateway 闭环；本轮用隔离 ASGI control/gateway/worker
  + 真实 Chromium 的受控链路验证，未构建或替换任何镜像，未复用用户卷。
- 未执行真实平台扫码、真实短信、真实 AppSecret 验证、真实发布/导出；受控夹具不等于真实平台通过。
- 未进入 5C3（发布/导出）、Task 6（整体部署/真实模型验收）、未合并、未更新用户容器、未上线。
- 未声称全仓测试通过；平台跳过与既有顺序失败已在上文单列。
