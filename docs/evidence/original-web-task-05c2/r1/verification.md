# Task 5C2 R1：四项定点修复、隔离容器闭环与证据

日期：2026-11-01（Asia/Shanghai）。

- 父基线：`a7c8ce253905918987f2a35fc269a1936a93dd6f`
- 修复提交：`b1e49cb9bd69b3ba9cdec8713cc113cdcfea4681`
- 证据提交：`eaa0c6a7436d46487efa8f9ac36cfceeaff84033`
- 实施 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`

## 1. 结论

R1 仅按根主审只读返工指令
`docs/superpowers/tasks/2026-11-01-original-web-task-05c2-rework-r1.md`
收口四项 P2，并补齐原任务要求的隔离容器闭环：

| 项 | 修复 | 结果 |
|---|---|---|
| T5C2-R1-01 登录状态归属 | control 只读当前 Run 自己的事件/终态；共享平台 status 不再推进新 Run，也不再冒充二维码/短信状态 | 新 Run 未开始前保持 `starting`/`running`，容量不释放；正常成功/失败仍由 worker Run 终态驱动 |
| T5C2-R1-02 显式分析动作 | `GET /api/analytics/{platform}` 只读最近成功缓存；启动刷新改为带 Origin/CSRF 的 `POST`；policy/tasks/前端同步 | 跨站顶层 GET 0 次执行；无/错 CSRF 403 且 0 次执行；合法 POST 成功且并发合并 |
| T5C2-R1-03 迟到停止回包 | 停止请求捕获弹窗序列/platform/mp/run 归属；成功、失败、finally 更新前均校验 | 旧停止回包不能改写随后打开的新平台/新 Run 弹窗 |
| T5C2-R1-04 凭证提交栅栏 | 验证后回到事件循环重新确认交接槽与 Run，`committing` 原子栅栏；stop 等待提交边界 | 停止先成功时不写 YAML/标记；提交先完成时 stop 返回真实 `done` |
| T5C2-R1-05 容器与证据 | 增量构建三镜像，在全新网络/卷/容器中跑真实受管链路；`.log` 证据转为受跟踪 `.txt` | 12 项容器检查全 true；源码哈希与镜像内文件一致 |

未修改用户运行实例/卷/socket/秘密材料，未真实扫码/短信/凭证/发布，未进入 5C3/6，未合并或上线。

## 2. 红测（父基线 `a7c8ce2`）

在父基线临时 worktree 中仅复制本轮新增/更新测试文件运行；生产代码不修改。
复现日志：

| 测试 | 父基线结果 | 日志 |
|---|---|---|
| `pytest tests/multiuser/test_task5c2_r1.py` | 10 failed | [red_task5c2_r1.txt](red_task5c2_r1.txt) |
| `pytest tests/multiuser/test_workspace_accounts.py -k 'analytics... or shared_platform... or login_status...'` | 4 failed / 1 passed | [red_workspace_accounts_analytics.txt](red_workspace_accounts_analytics.txt) |
| `node --test tests/frontend/accountLogin.test.mjs` | 3 failed（迟到停止成功/失败/finally） | [red_account_login_stop.txt](red_account_login_stop.txt) |

父基线复现包含：旧 success 把新 Run 直接标 success；旧 expired/error 直接 failed；
MP 旧状态混入；共享 `sms_required` 被接受；restart 新 Run 被旧 success 结算；
暂停验证→stop 后仍写 YAML；取消后新校验被旧迟到结果覆盖；注销交错；提交先完成时 stop
仍报 cancelled。新增行为测试全部先红后绿。

## 3. 绿测与回归

| 命令 | 结果 | 日志 |
|---|---|---|
| `pytest tests/multiuser/test_task5c2_r1.py` | 10 passed | 含在 [backend_regression.txt](backend_regression.txt) |
| 受影响后端 + 账本/策略/gateway/workspace/原账号/浏览器 profile | 417 passed, 1 skipped | [backend_regression.txt](backend_regression.txt) |
| `pytest tests/test_login_start.py tests/test_login_transport.py tests/test_account_whoami.py tests/test_web_analytics.py` | 59 passed | [local_original_accounts.txt](local_original_accounts.txt) |
| `node --test tests/frontend/*.test.mjs` | 157 passed | [node_tests.txt](node_tests.txt) |
| `npm --prefix web/frontend run build` | 退出 0；`index-zM18pZTN.js`，仅既有 >500KB chunk 警告 | [frontend_build.txt](frontend_build.txt) |
| `npm --prefix web/frontend run lint` | 退出 0 | [frontend_lint.txt](frontend_lint.txt) |

后端命令覆盖：

```
pytest tests/multiuser/test_workspace_accounts.py tests/multiuser/test_worker_browser_tasks.py \
  tests/multiuser/test_task5c2_security.py tests/multiuser/test_task5c2_chain.py \
  tests/multiuser/test_task5c2_r1.py tests/multiuser/test_store.py \
  tests/multiuser/test_gateway_client.py tests/multiuser/test_gateway_relay.py \
  tests/multiuser/test_gateway_forward.py tests/multiuser/test_gateway_workspace.py \
  tests/multiuser/test_policy.py tests/multiuser/test_task_leases.py \
  tests/multiuser/test_workspace_tasks.py tests/multiuser/test_original_web_api.py \
  tests/multiuser/test_browser_profiles.py
```

另外单独运行 `test_runs.py/test_stop_barrier.py/test_stop_recovery.py`：42 passed
（提交栅栏未破坏既有停止/收尾协议）。

## 4. 5C2 浏览器矩阵

[源码](../../../../tests/frontend/browser_original_web_task05c2.py) 真实 React 构建、HTTPS
control、gateway、WorkspaceApi、WorkerRelay、worker RunManager；平台入口为受控替身，
真实 Chromium 驱动 UI。本轮新增/保留 6 组，全部 true：

| 场景 | 断言要点 |
|---|---|
| login_flow | 二维码→短信→成功→昵称/头像→刷新恢复；无 local 缓存 |
| wechat_states | 后台扫码/AppID 凭证两态分清；秘密不回显 |
| cross_site_analytics | 真实 Chromium 从 `data:` 外站文档点击顶层链接到 `GET /api/analytics/{platform}`；SameSite=Lax Cookie 仍发送，但返回 404 `analytics_cache_missing`，平台执行次数不增 |
| late_stop_modal | 挂住旧停止响应→关闭 A→打开 B→放行；B 题头/二维码/状态/后台 Run 均保持，不被旧回包改写 |
| analytics | 成功/失败保留旧值/重试；执行次数 3 |
| stop_503_401 | 显式停止释放；一次 503 后轮询恢复；401 回登录页 |

结果文件：[browser_results.json](browser/browser_results.json)，运行日志
[browser_run.txt](browser_run.txt)，截图在 [browser/](browser/)。历史首轮截图未覆盖。

## 5. 既有浏览器矩阵回归

| 套件 | 结果 | 日志 |
|---|---|---|
| 5C1 技能/画像 7 组 | 7/7 true，`TASK5C1_BROWSER_OK True` | [regression_5c1.txt](regression_5c1.txt) |
| 5B 工作台 5 组 | 5/5 true，`TASK5B_BROWSER_OK True` | [regression_5b.txt](regression_5b.txt) |
| 5A 基础工作区 4 组 | 4/4 true，`TASK5A_BROWSER_OK True` | [regression_5a.txt](regression_5a.txt) |
| Task3 共用认证/会话 33 组 | 33/33 true，`FAILURES []` | [regression_task3.txt](regression_task3.txt) |
| Task4 登录/主题 8 组 | 8/8 true，`TASK4_BROWSER_OK True` | [regression_task4.txt](regression_task4.txt) |

回归脚本使用 `EASEL_BROWSER_EVIDENCE` 指向本 R1 子目录；5C1/5A/Task4 也补齐了该
环境变量入口，历史截图未覆盖。总浏览器组：5C2 6 + 既有 57 = 63 组全部通过。

## 6. 隔离容器闭环（T5C2-R1-05）

### 6.1 构建与镜像

增量 Dockerfile（均为当前提交源码，基线镜像层复用，不重建 Harness/Chromium 全栈）：

- [Dockerfile.control-task5c2-r1](../../../../tests/multiuser/Dockerfile.control-task5c2-r1)：
  基于已验收 `easel-control:task5c1-r1-20261030`，覆盖 `easel/` 与当前 `web/frontend/dist`。
- [Dockerfile.gateway-task5c2-r1](../../../../tests/multiuser/Dockerfile.gateway-task5c2-r1)：
  基于已验收 `easel-gateway:task5c1-20261030`，覆盖 `easel/`。
- [Dockerfile.worker-task5c2-r1](../../../../tests/multiuser/Dockerfile.worker-task5c2-r1)：
  基于已验收 `easel-worker-execution:a4`（含 Playwright/Chromium），覆盖 `easel/` 与
  `web/app.py`；补装 `PyYAML==6.0.3`（已加入
  `deploy/multiuser/requirements-worker.txt`，容器闭环发现原镜像缺此依赖）。

本轮最终镜像 ID（来自 [container_chain.txt](container_chain.txt)）：

- control：`sha256:1e3cf0b6684969cf1087a0ec7065380d9f51a0081c97efaf0a2eb044171532d4`
- gateway：`sha256:4316aa71f57465ccf77a75eb088b3b1d6e920f49dbc964fe4ec4517f765a997e`
- worker：`sha256:586915838d0570065709a348cd94c8f228817119118cc97f7f9c6d43d6304328`

镜像内源码 SHA 与宿主逐文件一致性检查通过（见日志 `SOURCE_HASH`）：

- `easel/control/workspace_accounts.py` `8d91f51a…`
- `easel/control/app.py` `b1162f42…`
- `easel/control/policy.py` `fb6ae060…`
- `easel/control/tasks.py` `137f3384…`
- `easel/gateway/app.py` `3a0868a1…`
- `easel/worker/runs.py` `e83fc06c…`
- `easel/worker/browser_tasks.py` `26c8673f…`
- `web/app.py` `75ed8dd2…`
- `web/frontend/dist/assets/index-zM18pZTN.js` `5f251ed0…`

### 6.2 实际执行层与夹具

真实执行：

- control FastAPI、认证/CSRF/Origin、Store Run 账本、WorkspaceApi、GatewayClient；
- gateway Ed25519 验签与精确路由；
- worker `web.app` 内部 Run 路由、RunManager 接纳/事件/停止/超时、子进程登记与进程树终止；
- 容器内 Playwright + Chromium（内置沙箱；容器以 `seccomp=unconfined` 允许非特权
  用户命名空间，未使用 `--no-sandbox`）；
- 本人 HOME 下 `~/.easel-browser-profiles/<platform>/default` 持久 profile；
- Chromium 截图像素 240×240、871 字节二维码 PNG；
- 凭证 YAML 原子落盘到 `/workspace/state/skills/wechat/default.yaml`，秘密只经 stdin
  进入验证子进程，未出现在 control/worker 日志。

夹具（不访问真实平台/账号）：

- 本地固定 [task5c2_controlled_platform.py](../../../../tests/multiuser/task5c2_controlled_platform.py)
  在同一镜像内以真实 Python 子进程运行，启动 Chromium 并读写二维码/状态/profile；
- 平台站点、二维码内容、短信码（固定 `123456`）、whoami 昵称、analytics 数值、微信
  凭证验证结果均为本地夹具；
- 原平台脚本（`xhs_publish.py`、`account_stats.py`、`weixin_mp_stats.py` 等）在容器
  验收中未执行；launcher 仅替换这些平台入口点，Run 账本/停止/取消仍由原 RunManager
  执行。容器验收把登录 Run 时限临时缩短为 15s 以触发真实超时取消；生产默认 600s 不变。

### 6.3 容器检查项

[task5c2_container_chain.py](../../../../tests/multiuser/task5c2_container_chain.py) 在全新
标签网络/卷/容器中验证：

- 一次受管登录派发：`starting`→`qr_ready`→`sms_required`→`success`，control Run
  `done`，容量释放；
- 本人 profile 目录存在，容器进程表可见受控 runner/Chromium；
- 二维码入口返回真实 PNG（240×240）；
- whoami 结果昵称来自容器内 Chromium；analytics 结果与 `_analytics` 产物落本人卷；
- 凭证保存成功、AppID 配置可见、secret 不在日志；
- 显式停止返回 cancelled/interrupted、进程树消失、容量释放后还能再派发；
- 不提交验证码时 Run 超时 failed、进程树消失、容量释放；
- 跨站 GET 只读缓存且 Run 总数不变。

`CONTAINER_CHECK` 12 项全 true，`TASK5C2_CONTAINER_OK True`，
`CLEANUP_ERRORS []`。清理前逐个 `docker inspect` 校验 `easel.test.run-id` 标签，只删除
本轮容器/网络/三卷；未复用用户卷/socket/容器。

## 7. 主审只读探针复跑

探针脚本位于根 `docs/evidence/original-web-task-05c2-review/`，本轮未修改。

| 探针 | 结果 | 日志 |
|---|---|---|
| `probe_worker_lifecycle.py --case stale-login` | 200/`starting`；新脚本启动 0；control/worker Run 均 running；active=1 | [probe-stale-login.txt](probe-stale-login.txt) |
| `probe_worker_lifecycle.py --case credentials-stop` | stop 200/cancelled；放行后凭证请求 409；配置未写、configured=false、标记 unknown | [probe-credentials-stop.txt](probe-credentials-stop.txt) |
| `probe_analytics_navigation.py` | 退出 1：在“等待 analytics 执行”的守卫处超时（`analytics execution absent`），即不再执行；正向由 `cross_site_analytics` 场景覆盖 | [probe-analytics-navigation.txt](probe-analytics-navigation.txt) |
| `probe_late_stop_modal.py` | 退出 1：等待“已停止登录”超时，即旧回包不再改写新弹窗；正向由 `late_stop_modal` 场景覆盖 | [probe-late-stop-modal.txt](probe-late-stop-modal.txt) |

说明：后两个探针本身是“复现缺陷”的只读脚本，其成功条件是观察到旧缺陷；修复后它们在
等待旧缺陷出现处超时退出，这是预期的不再复现结果。未修改主审脚本，正向断言由本组浏览器
场景给出。

## 8. 证据可移交

首轮 5C2 的 31 个 `.log` 已全部转为受跟踪的 `.txt`（内容未改写），
[verification.md](../verification.md) 的链接同步更新。R1 新证据全部位于本目录，包括
红/绿日志、浏览器截图、容器日志、探针输出。提交后可用：

```
git ls-tree -r --name-only HEAD docs/evidence/original-web-task-05c2 | grep -E '\.txt$'
```

确认干净 checkout 也能取得这些证据，而不只存在于本机。

日志中出现的 `old-secret` / `new-secret` / `container-secret` 等均为临时测试字面量，
不是真实凭证；证据未包含真实 AppSecret、用户 Cookie、邀请码或 Key。

## 9. not-run / 边界

- 未执行真实平台扫码、真实短信、真实 AppSecret 验证、真实发布/导出；受控夹具不等于
  真实平台通过。
- 容器内未执行原平台脚本；只证明镜像内 Chromium/Playwright、子进程、profile、二维码、
  状态文件、Run 账本、停止/超时与凭证落盘链路可用。
- 未执行真实 Harness/Agent 对话或付费模型；容器 Run 的 Agent 配置仅为固定签名快照。
- 未进入 5C3（发布/导出）、Task 6（整体部署/真实模型验收）、未合并、未更新用户容器、
  未上线。
- 未声称全仓测试通过；上述命令与浏览器组是本轮验收范围，既有已知顺序失败未在本轮扩大。
