# Task 5C3 R1 验收证据：发布策略迁移与请求幂等收口

父基线：`aef1a578185d15a90d8c46ab46ffb3adde1edfb3`
实施 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`
日期：2026-11-01
依据：[首轮审计](../../../superpowers/audits/2026-11-01-original-web-task-05c3-review-round-1.md)、[R1 执行单](../../../superpowers/tasks/2026-11-01-original-web-task-05c3-rework-r1.md)

本文件只记录 R1 三项 P2 的返工证据；原 5C3 验收结论与历史截图保持不变，
新截图写入本目录 `browser/`，不覆盖 `../browser/`。

## 1. 三项修复摘要

### T5C3-R1-01 正式部署策略传递与旧 manual 迁移

- `deploy/desktop/compose.workspace.yaml` 的 control 命令新增
  `--publish-mode ${EASEL_PUBLISH_MODE:?…}`；正式 Compose 在缺少策略时
  直接拒绝启动。
- `Start-WorkspaceEasel.ps1` 新增 `-PublishMode automated|manual`：
  - 只从显式参数或可信部署登记 `deployment.json` 的 `publish_mode` 取值，
    不读取 worker/宿主 `.env`；
  - 旧部署登记缺少该字段且未显式传参时，以
    `publish_mode_migration_required` 拒绝启动，不静默按新默认放开；
  - 部署登记丢失但控制卷已有 `control.sqlite3` 时（含恢复目标），同样
    要求显式 `-PublishMode`；
  - 全新部署仍保留 `automated` 默认；
  - 解析结果通过 `EASEL_PUBLISH_MODE` 传给 Compose，并写回
    `deployment.json`，后续重启/升级复用；大小写统一归一化为小写。
- `Stop-WorkspaceEasel.ps1` 的 `compose down` 同样会加载 command 并做
  必填插值，因此 envArgs 也提供保守占位 `EASEL_PUBLISH_MODE`；新增
  契约测试遍历 Compose 全部 `${VAR:?}`，要求 Start/Stop 的 envArgs
  都提供。Stop env 列表对正式 Compose 的 `config --quiet` 退出 0
  （见 [deploy-policy-check.txt](deploy-policy-check.txt) 的
  `WITH_STOP_ENV_EXIT:0`）。
- `Status-WorkspaceEasel.ps1` 只读输出 `version_publish_mode`。
- 备份元数据允许并校验 `publish_mode`；恢复后的下一条 Start 命令沿用该
  策略；升级时旧登记与备份清单都有策略且不一致则拒绝。

### T5C3-R1-02 接纳响应丢失后的意图恢复

- 前端在首次发送前把“平台 + 稿件版本指纹 + 完整 payload + requestId”
  写入按 `userId:workspaceId` 分组的本地意图存储；同稿重试/刷新复用同一
  requestId 与 payload，新稿才生成新请求号。
- 网络中断/408/5xx 时保留意图并显示“未知/核对中”，不自动补发；用户
  再次点击只重试同一请求。
- `GET /api/publish/{platform}/status?requestId=…` 只查询本会话该意图的
  原 Run；未确认返回 `runId=""` 的 unknown，刷新时据此恢复或保留意图。
- 明确成功/失败/未知终态后才清除意图；明确完成后的再次发布是新意图。
- 意图同时保存完整 payload（title/body/media/tags/persona）；刷新或切身份
  丢失 media/persona 选择时，只要标题/正文/标签未变，仍复用原 payload 与
  requestId 重试，不会因为前端状态丢失而生成第二次外部提交。
- 迟到旧回应只能清除同一 requestId 的意图；轮询发现当前平台意图已更换
  即停止旧轮询，不能覆盖新卡片或删除新意图。
- 意图与草稿都按身份/workspace 隔离；身份切换后 B 不读取 A 的草稿或
  意图，A 的未确认意图保留到 A 重新登录。

### T5C3-R1-03 同 requestId 必须绑定同一输入

- `RunRecord` 暴露持久 `input_digest`；`_join_or_submit` 在
  `active.client_request_id == requestId` 时不再直接返回旧 Run，而是用与
  WorkerRelay 完全相同的 `run_input_digest` 复核会话、任务类型、正文
  描述、附件与人设。
- 同 ID 同输入运行中/终态复用原 Run；同 ID 异输入运行中/终态均 409，
  不修改旧 Run、不启动新提交；普通发布与导出共用该 helper。
- 未新建第二套账本，继续使用 `runs.client_request_id`/`input_digest`。

## 2. 红绿证据

先写测试并实际观察失败，再实现最小修复；以下命令均在 worktree 内运行。

### T5C3-R1-03

红灯（修复前实际输出）：

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest `
  tests/multiuser/test_workspace_publishing.py `
  -k same_request_id_changed -q
# FAILED ... DID NOT RAISE DomainError（active 同 ID 改稿仍 200）

& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest `
  tests/multiuser/test_task5c3_chain.py -k same_request_id_changed -q
# FAILED ... assert 200 == 409（真实链同 ID 改稿被当作原 Run）
```

绿灯：

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest `
  tests/multiuser/test_workspace_publishing.py `
  tests/multiuser/test_task5c3_chain.py -q
# 30 passed
```

覆盖：活动/终态发布、活动/终态导出、同输入复用、异输入 409、跨用户隔离；
真实 control→gateway→worker 链（`test_chain_*`）与带真实
`run_input_digest` 的持久 Store 替身共同验证，不仅比较 requestId 字符串。

### T5C3-R1-02

红灯：`GET /api/publish/{platform}/status?requestId=...` 在修复前被
`_no_query` 拒绝为 400；新增 `test_status_by_request_id_returns_exact_owned_run`
与真实链 `test_chain_status_by_request_id_recovers_original_run` 失败。前端第一次
丢 ack 浏览器运行同样观察不到“未知/核对中”状态（`pv-pubstate.unknown`
等待超时）。

绿灯：

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest `
  tests/multiuser/test_workspace_publishing.py `
  tests/multiuser/test_task5c3_chain.py -q
# 30 passed（含 requestId 状态查询）

node --test tests/frontend/*.test.mjs
# 173 passed（含意图 payload 持久化、身份隔离、迟到旧回应不删新意图、requestId 状态查询）

$env:EASEL_5C3_EVIDENCE = 'docs/evidence/original-web-task-05c3/r1/browser'
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' `
  tests/frontend/browser_original_web_task05c3.py
# TASK5C3_BROWSER_OK True；11/11 场景通过
```

### T5C3-R1-01

红灯：`test_workspace_compose_binds_signing_keys_and_gateway_mode` 找不到
`--publish-mode`；`test_start_exposes_managed_publish_mode_parameter` 找不到
`$PublishMode`；PowerShell 正式 Start 用例中旧部署缺策略仍继续执行，
显式 `-PublishMode manual` 因脚本没有该参数而失败。

绿灯：

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest `
  tests/multiuser/test_task5c3_chain.py -k formal_compose `
  tests/multiuser/test_desktop_workspace.py `
  tests/multiuser/test_desktop_operations_r1.py `
  -k "formal_compose or publish_mode or start_" -q
# 通过：正式 Compose argv→正式 CLI parser→真实 control/gateway/worker 链
# manual 在 Run 接纳前 403；旧部署缺策略拒绝；显式迁移/复用/非法值拒绝；
# 升级备份策略不一致拒绝
```

正式 Compose 缺变量时的 fail-closed 原始输出见
[deploy-policy-check.txt](deploy-policy-check.txt)（manual 成功、
无变量 `WITHOUT_MODE_EXIT:1`）。

### 后端与前端回归

```powershell
# Batch 1（5C3/发布/worker/正式部署/Store/Run/policy/leases）
# 386 passed, 1 skipped
# Batch 2（5C1/5C2/auth/admin/config/session/gateway/broker/worker/desktop）
# 947 passed, 4 skipped
# Batch 3（worker execution gate 独立进程）
# 9 passed
# Batch 4（备份/恢复/操作 CLI）
# 71 passed, 1 skipped
```

原始输出：[backend-tests.txt](backend-tests.txt)、
[backend-tests-operations.txt](backend-tests-operations.txt)；
完整命令清单见 [backend-commands.txt](backend-commands.txt)。

说明：把 Batch 1+2+3 的全部文件放在同一个 pytest 进程串跑时，3 个
`test_worker_execution_gate.py` 用例因跨文件 async loop/生命周期污染出现
`No response returned`/`different loop`；该文件单独进程 9/9 通过，属于
测试夹具全局状态问题，不伪报为通过，也不立为产品缺陷。本组采用独立进程
分批验收（与首轮主审对浏览器夹具的要求一致）。

```powershell
npm --prefix web/frontend run build   # 退出 0，index-CfTWRFga.js
npm --prefix web/frontend run lint    # 退出 0
node --test tests/frontend/*.test.mjs # 173 passed
```

原始输出：[build-lint.txt](build-lint.txt)、[node-tests.txt](node-tests.txt)。

### 首轮提交后补洞（独立审查发现并复现）

1. **Stop 也要求必填策略变量**：`docker compose down` 仍会加载 command
   并做 `${EASEL_PUBLISH_MODE:?}` 插值；首轮 Stop envArgs 未提供该变量，
   正常 Stop 会在 barrier 之后失败。已给 Stop envArgs 加保守占位值，并
   新增 `test_compose_required_variables_are_supplied_by_start_and_stop`
   遍历 Compose 全部 `${VAR:?}`，要求 Start/Stop 都提供。
2. **意图 payload 需要随意图持久化**：刷新/切身份会丢失未持久化的
   `selectedMedia`/`persona`，旧实现会因指纹变化生成新 requestId。现在
   意图保存完整 payload；标题/正文/标签不变时复用原 payload 与
   requestId。新增浏览器场景
   `lost-ack-refresh-retry-stored-payload`：丢 ack、后台 done、刷新且
   恢复查询失败一次、媒体选择丢失后重试，断言两次 POST 的 requestId
   相同、媒体仍为原选择、受控执行只 +1。
3. **策略大小写归一化**：Start/Restore 统一 `ToLowerInvariant()`，避免
   `-PublishMode Manual` 或登记文件混合大小写被 PowerShell 接受后被
   argparse 拒绝；新增 `test_start_normalizes_publish_mode_case`。

## 3. 正式部署迁移说明

### 全新部署

```powershell
deploy/desktop/Initialize-WorkspaceEasel.ps1 ...
deploy/desktop/Start-WorkspaceEasel.ps1 ... # 可省略 -PublishMode，默认 automated
```

### 从 5C3 之前/首轮 5C3 的旧部署升级

旧 `deployment.json` 没有 `publish_mode`（无论旧 worker `.env` 曾写
`manual` 还是 `automated`）时：

```powershell
# 旧部署曾明确 manual：
deploy/desktop/Start-WorkspaceEasel.ps1 ... -PublishMode manual
# 旧部署确认要自动发布：
deploy/desktop/Start-WorkspaceEasel.ps1 ... -PublishMode automated
```

不带 `-PublishMode` 会得到 `publish_mode_migration_required` 并以非 0 退出，
不会执行 compose up。迁移后策略写入 `deployment.json`，后续重启/升级无需
重复传参；如需变更，显式再传 `-PublishMode`。

### 直接使用 Compose

`EASEL_PUBLISH_MODE` 是必填变量（`automated|manual`），未设置时
`docker compose config/up` 直接失败，不再回退到 control 的 CLI 默认值。

### 边界

- 恢复（Restore）到新目标后，控制卷已有 DB 但没有新的
  `deployment.json`；Start 的 DB 探测会要求显式 `-PublishMode`。
  Restore 的 `next=Start-...` 提示在备份登记了策略时会自动带
  `-PublishMode <值>`，不会静默回到 automated。
- 成员没有策略写入口；策略只由管理员部署参数决定。
- control 不读取 worker/宿主 `.env` 作为实时权限源。
- 旧 worker `.env` 的 manual 不能自动推断；必须由操作员显式迁移。
- root 运维手册由主审后续统一更新；本文件只补本组迁移说明。

## 4. 浏览器矩阵新增场景

命令（独立 Python 进程运行，输出见 [browser-matrix.txt](browser-matrix.txt)）：

```powershell
$env:EASEL_5C3_EVIDENCE = 'docs/evidence/original-web-task-05c3/r1/browser'
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' tests/frontend/browser_original_web_task05c3.py
# TASK5C3_BROWSER_OK True；11 场景全部通过，failures=[]，退出 0
```

新增场景：

1. `lost-ack-running-retry-once`：首次 POST 已接纳但浏览器响应丢失，后台仍
   running；同稿重试复用同一 requestId，受控发布只执行 1 次。
2. `lost-ack-done-retry-once`：后台 Run 已 done 后同稿重试；仍复用原 Run，
   执行次数不增加。
3. `lost-ack-refresh-recovers`：丢响应后刷新，按 requestId 找回原 Run；
   不创建第二个任务，意图在明确成功后清除。
4. `explicit-new-intent`：明确成功后用户再次发布同稿；生成新 requestId，
   作为新意图执行（不永久去重同文）。
5. `lost-ack-refresh-retry-stored-payload`：丢响应、后台 done、刷新并让
   恢复查询失败一次；页面丢失媒体选择后重试，仍用落盘 payload 的媒体和
   原 requestId 恢复，受控执行次数不增加。
6. `identity-switch-isolates-intent`：A 丢响应后切 B；B 看不到 A 的草稿/
   意图、不会重发 A 的 requestId；切回 A 后原意图和草稿恢复，执行次数不变。

原五组（人工导出审核、自动策略、短信/停止/刷新、双击一次、manual 策略）
同时保持通过。截图见 [browser/](browser/)；历史截图仍保留在原
`../browser/` 未被覆盖。

### 父基线浏览器回归（每个脚本独立 Python 进程）

- 5C2：6/6 场景通过，`EXIT:0`（[regression-5c2.txt](regression-5c2.txt)）。
- 5C1：7/7 场景通过，`TASK5C1_BROWSER_OK True`，`EXIT:0`
  （[regression-5c1.txt](regression-5c1.txt)）。
- 5B：5/5 场景通过，`TASK5B_BROWSER_OK True`，`EXIT:0`
  （[regression-5b.txt](regression-5b.txt)）。
- 5A：4/4 场景通过，`TASK5A_BROWSER_OK True`，`EXIT:0`
  （[regression-5a.txt](regression-5a.txt)）。
- Task3：33/33 场景通过，`FAILURES []`，`EXIT:0`
  （[regression-task3.txt](regression-task3.txt)）。
- Task4：8/8 场景通过，`TASK4_BROWSER_OK True`，`EXIT:0`
  （[regression-task4.txt](regression-task4.txt)）。

5C1/5B/5A/Task3/Task4 的 `EASEL_BROWSER_EVIDENCE` 指向本组临时
`r1/regression-browser/`；5C2 脚本固定写历史目录，运行后已用
`git checkout -- docs/evidence/original-web-task-05c2/` 恢复历史截图。
回归截图只用于本轮人工核对，未重复提交到仓库；本组跟踪的是各脚本文本
结果与退出码。5C1 首次运行因证据路径用相对值触发脚本自身 `relative_to`
报错（场景结果全部 true、`FAILURES []`），改用绝对路径后 7/7 通过；
不把该路径问题算作产品通过或缺陷。

## 5. 隔离容器闭环

只增量重建受影响的 control 镜像（基于已验收的 5C2-R1 control 基座），
gateway/worker 沿用 5C3 已验收镜像；未无缓存重建全栈。原始输出见
[container-chain.txt](container-chain.txt)。

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' `
  tests/multiuser/task5c3_container_chain.py `
  --control-image easel-control:task5c3-r1-20261101 `
  --gateway-image easel-gateway:task5c3-20261101 `
  --worker-image easel-worker-execution:task5c3-20261101
# TASK5C3_CONTAINER_OK True；CLEANUP_ERRORS []
```

镜像：

- control `sha256:902de6c1330d8d773dcd6e532943d5ef485e55a40344a2be34d6250af293ddee`
- gateway `sha256:f778221c003cae140452aff976331790ca9f0e90f302bf461a92204ff164c872`
- worker `sha256:d46f0dd1f17273e91f3d3cca7e68854ca1dc55231a64b446ef058ad7b5066ec8`

镜像内源码/前端哈希与当前源码一致（见 `SOURCE_HASH` 行，control 新
`index-CfTWRFga.js`）。

真实层：固定身份/签名、control/gateway/worker、Gateway、WorkspaceApi、
WorkerRelay、RunManager、受管发布执行器、镜像内受控发布子进程、进程树
取消、worker 重启。替身层：原平台脚本未执行（`xhs_publish.py`、
`douyin_publish.py`、`web_publisher.py`、`weixin_mp_stats.py`、`biliup`
均为 substituted），不声称真实平台通过。

新增检查全部 true：

- `manual_policy_rejected`：独立 control 卷以 manual 启动，策略为 manual，
  发布 POST 403，Run 数 0，worker 受控执行 0。
- `status_by_intent`：`?requestId=` 返回原 Run；未接纳的 requestId 返回
  `runId=""` 的 unknown。
- `request_content_bound`：终态同 requestId 改稿 409，Run 数与受控执行
  次数不变。
- `active_content_bound`：运行中同 requestId 改稿 409，不新增 Run。
- 原检查（导出下载/解包、一次提交、日历一次、抖音短信归属、停止/超时
  释放、查询恢复、重启不重放、受控 runner 实际执行、profile 锁）保持 true。

清理按标签归属删除容器/网络/卷，`CLEANUP_ERRORS []`；未操作运行中用户
实例。

## 6. 未执行与边界

- 未修改三条主审只读探针。修复后：
  - `probe_request_content.py` 原样复跑输出
    `first_status=200, changed_content_status=409, same_run=false,
    cleanup_stop_status=200, temporary_directory_cleaned=true`；
  - `probe_publish_lost_ack.py` 等待旧的 `.pv-pubstate.fail`；修复后页面按
    R1 要求显示 unknown/核对中，因此旧探针预期超时，不作为通过证据，
    由本组新增浏览器场景替代；
  - `probe_manual_deployment.py` 假设正式 Compose argv 可原样解析；正式
    命令现在要求 `EASEL_PUBLISH_MODE`（缺失即拒绝），旧探针的解析前提
    不再成立；由 `test_formal_compose_argv_resolves_managed_publish_mode`
    与真实链 manual 拒绝用例替代。
- 未真实发布、未发送真实短信、未访问真实平台、未读取真实凭证。
- 未操作用户运行容器/卷/socket/Key/Cookie/邀请。
- 未合并、推送、部署，未进入 Task 6。
- 未清理主审被阻止删除的两个临时目录。

## 7. 文件职责

| 文件 | 职责 |
|---|---|
| `deploy/desktop/compose.workspace.yaml` | 正式 Compose 传递必填 `EASEL_PUBLISH_MODE` |
| `deploy/desktop/Start-WorkspaceEasel.ps1` | 解析/迁移/持久化发布策略并传 Compose |
| `deploy/desktop/Status-WorkspaceEasel.ps1` | 只读显示已登记发布策略 |
| `deploy/desktop/Restore-WorkspaceEasel.ps1` | 恢复后 Start 命令沿用备份的发布策略 |
| `deploy/desktop/EaselDesktop.Common.ps1` | 升级时旧登记与备份策略一致性校验 |
| `docs/manual-publishing.md` | 正式 Desktop 与本机 Web 的发布策略配置说明 |
| `easel/control/types.py` | `RunRecord.input_digest` |
| `easel/control/store.py` | 按 requestId 查询本人原 Run；Run 记录携带摘要 |
| `easel/control/workspace_publishing.py` | 同 requestId 输入摘要校验；按 requestId 查状态 |
| `easel/control/app.py` | status 的严格 `requestId` 查询参数 |
| `easel/control/cli.py` | 备份元数据允许并校验 `publish_mode` |
| `web/frontend/src/lib/store.ts` | 按身份/workspace 隔离的发布意图存储 |
| `web/frontend/src/lib/workspaceData.ts` | 带 requestId 的状态查询与不确定错误分类 |
| `web/frontend/src/components/PublishPage.tsx` | 稳定意图、未知/核对中、刷新恢复、新意图 |
| `tests/multiuser/test_workspace_publishing.py` | 输入绑定/意图状态单元与 API |
| `tests/multiuser/test_task5c3_chain.py` | 真实 control/gateway/worker 链输入绑定与正式 Compose 参数 |
| `tests/multiuser/test_desktop_operations_r1.py` | 正式 Start 策略迁移红绿 |
| `tests/multiuser/test_desktop_workspace.py` | Compose/Start 策略传递静态契约 |
| `tests/frontend/publishDraftScope.test.mjs` | 意图存储身份隔离与损坏数据 |
| `tests/frontend/publishWorkspaceData.test.mjs` | requestId 状态查询与不确定错误 |
| `tests/frontend/browser_original_web_task05c3.py` | 丢 ack/刷新/新意图/A-B 浏览器矩阵 |

## 8. 源码 SHA 与镜像一致性

容器脚本在镜像内读取以下文件并与宿主当前源码逐项比对，全部一致
（原始 `SOURCE_HASH` 行见 [container-chain.txt](container-chain.txt)）：

| 组件 | 文件 | SHA256 |
|---|---|---|
| control | `easel/control/workspace_publishing.py` | `4e7dcf5b6b51e9f7e37adffb69a204b78c6c7cf48bb8e3a4d168c08a026fb917` |
| control | `easel/control/app.py` | `16157a5b9f23c6f4319c5e0deea11d015c8854bc05146be8c5fe7155edf434c3` |
| control | `easel/control/tasks.py` | `ce65dfa19a30ad7a707acd66e9ee61bd68c0a4cb95b26f9a97971c662f5dacfd` |
| control | `easel/control/proxy.py` | `75badc104569868840835057277f0ac315af76f7be5e30a44395afcb6c10319c` |
| control | `easel/control/store.py` | `6ff3127961e9796e534a8698e0589c1178ebfc09b8b7ffb88da1af688cde2489` |
| control | `easel/control/types.py` | `0b796cde6b89834015a9cd4832fd9ee6f41b4d11873df6bae62d684f833eb780` |
| control | `easel/control/cli.py` | `82a0d91e817e6e6f5943c2cb64900745d006cf2ed49181b91d5f7d22dac47b31` |
| control | `easel/manual_publish.py` | `6c8648d7299e1dad33ec7a0fcee71a4dce0048aa8901c8874f44e7a980d6937e` |
| control | `web/frontend/dist/assets/index-CfTWRFga.js` | `6f7123fc6617e8ef40a74200bb3ef195bdfd7339aa0fc17b2f3a85ea1668a9f3` |
| gateway | `easel/gateway/app.py` | `96f71488fd8332441452fb4d16142a674004e360bf1eabef575232e8c1b9a464` |
| worker | `easel/worker/browser_tasks.py` | `007ff68315e4eacb98e531fb66998280dfa7a28b85d0c8a428b55403020e1109` |
| worker | `easel/manual_publish.py` | `6c8648d7299e1dad33ec7a0fcee71a4dce0048aa8901c8874f44e7a980d6937e` |
| worker | `web/app.py` | `d0a1e5c4b40818317b9eac3d1291dc5d831c00e06a521063834d5b4fd53ca82a` |

control 镜像是基于 5C2-R1 基座的增量层（仅复制当前 `easel/` 与
`web/frontend/dist`），gateway/worker 沿用 5C3 已验收镜像；未无缓存重建
全栈。镜像内受控发布不触达真实平台。
