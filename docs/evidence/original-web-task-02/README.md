# 原 Web 修复 Task 2 实施证据：统一受管模型并验证 Harness 聊天

- 父基线：`bcf001a0ff5a9ba7242ba6f686a9ce7eb341081d`（Task 1 验收提交）
- 隔离 worktree：`C:\Code\Easel\.claude\worktrees\original-web-task1-diagnostics`
- 实施提交：完整 SHA 见本次交付消息
- 日期：2026-10-29
- 范围：仅 Task 2；未进入 Task 3，未切换/替换用户运行实例，未合入主工作区

## 1. 精确文件清单

生产代码：

- `easel/control/broker.py`
- `easel/control/config.py`
- `easel/control/cli.py`
- `easel/control/snapshot.py`
- `easel/control/proxy.py`
- `easel/control/app.py`
- `easel/harnesses/acp.py`
- `easel/worker/runs.py`
- `easel/cli.py`（R1：受管 profile / expected commit 参数）

受管 profile / 构建门槛：

- `deploy/multiuser/managed/acp-profile.json`
- `deploy/multiuser/managed/acp.patch.yml`
- `deploy/multiuser/verify_managed_profile.py`
- `deploy/multiuser/smoke_harness.py`
- `deploy/multiuser/smoke_worker.py`
- `deploy/desktop/Dockerfile.control`（R1：控制镜像内置受管 manifest）

测试：

- `tests/multiuser/test_managed_chat_model.py`（新增）
- `tests/multiuser/test_managed_profile.py`
- `tests/multiuser/test_relay_cli.py`
- `tests/fixtures/harness_diagnostics.py`
- `tests/integration/multiuser/test_managed_chat_model.py`（新增）
- `tests/integration/multiuser/test_harness_runtime_real.py`
- `tests/integration/multiuser/test_desktop_chat_full.py`
- `tests/integration/multiuser/test_desktop_chat_live.py`
- `tests/integration/multiuser/test_desktop_operations_real.py`

证据（本目录）：

- `README.md`
- `probe_harness_models.py` / `probe-harness-models.txt`
- `Dockerfile.worker-delta` / `Dockerfile.control-delta`
- `worker-delta-build.txt` / `control-delta-build.txt`
- `worker-image-smoke.txt`
- `image-source-hashes.txt`
- `red-task2-unit.txt`
- `green-task2-new-unit.txt`
- `green-task2-targeted.txt`
- `green-task1-regression.txt`
- `green-relay-related.txt`
- `green-tcp-related.txt`
- `real-flash-topology.txt`
- `real-flash-permission-stop.txt`
- `control-delta-build-r1.txt`
- `worker-delta-build-r1.txt`
- `worker-image-smoke-r1.txt`
- `image-source-hashes-r1.txt`
- `red-task2-r1-binding.txt`
- `green-task2-r1-targeted.txt`
- `green-task1-r1.txt`
- `real-flash-topology-r1.txt`
- `real-flash-permission-stop-r1.txt`

## 2. 单一来源与版本固定

生效链路：

1. **镜像信任目录**：`acp-profile.json` 的 `provider.models` 与
   `acp.patch.yml` 的 `llm-pi-ai.models` 由
   `verify_managed_profile.validate_manifest` 强制完全一致；目录为
   `deepseek-chat`、`deepseek-flash`、`deepseek-v4-pro`。默认模型仍为
   `deepseek-chat`，但每个 Run 的签名 Agent 会用有效路由模型覆盖它。
2. **镜像信任目录（R1 起为基线）**：控制镜像内
   `/opt/easel/managed/acp-profile.json` 与 worker 镜像使用同一份受管
   manifest；`load_managed_profile_models` 校验 schema、Harness commit、
   provider id/api、默认模型和 `provider.models`。`cmd_serve_control`
   在打开控制库前读取该目录；`--expected-harness-commit` /
   `EASEL_EXPECTED_HARNESS_COMMIT` 可绑定目标 worker 镜像 commit，不符即
   失败。
3. **运行时目录只能收窄**：`managed-agent.json` 的 `agent.models` 可选；
   若提供，必须是镜像目录的子集。越界模型在服务启动前以
   `managed_config_invalid` 拒绝；manifest 缺失/schema/version 不匹配以
   `managed_profile_invalid` 拒绝；不打开控制库、不创建 Run/token。
   未提供时直接使用镜像目录。`ConfigService` 与 `BrokerService` 只接收
   校验后的有效目录。
4. **管理员 chat 路由**：`ConfigService.publish` 保存时拒绝默认模型或
   `allowed_models` 不在有效目录内的路由（409 `model_not_allowed`）；
   `public_settings` 只公开交集。
5. **接纳时固定**：`BrokerService.issue_with_route` 在写 broker token
   快照的同一事务中返回 `EffectiveRoute`；`WorkerRelay._sign` 对该 chat
   Run 写入 `agent.model = route.model` 与
   `agent.allowed_models = route.allowed_models`。broker token 的
   route_json 与签名 Agent 来自同一路由版本，不会出现 token 新版本而
   Agent 仍用旧版。
6. **worker 复核**：`RunManager._prepare` 校验签名 Agent 的
   `models`/`allowed_models`/`model` 关系，越界或格式错误在适配器启动前
   以 `agent_config_invalid` 拒绝。
7. **ACP 选择**：真实 Harness 的 `session/new` 模型选项值是
   `JSON.stringify([provider, model])`；`ACPAdapter` 从真实选项目录中
   按 model 匹配精确 value，再调用 `session/set_config_option`。目录存在
   但无匹配时本地拒绝，不会把臆造值发给 Harness；旧 Agent 未公布目录时
   保持 raw model 兼容。

旧 Run：签名快照与 broker token 已持久化；管理员发布新版本只影响新
Run。`test_issue_route_and_agent_model_are_fixed_together` 用双连接交错
证明发布与接纳只能得到整份旧版或新版。

### 部署前置（R1 更新）

R1 后不再要求部署方手工保持 `agent.models` 与镜像目录一致。控制镜像
已包含受管 manifest；`managed-agent.json` 的 `agent.models` 可选，仅用于
收窄：

- 不写：有效目录 = 镜像 manifest 的 `provider.models`
  （`deepseek-chat`、`deepseek-flash`、`deepseek-v4-pro`）。
- 写子集：有效目录 = 该子集；写镜像外模型 → 启动前
  `managed_config_invalid`，不打开控制库。
- manifest 缺失、schema/provider 不合法或 commit 与
  `--expected-harness-commit`/`EASEL_EXPECTED_HARNESS_COMMIT` 不符 →
  启动前 `managed_profile_invalid`。
- 控制镜像与 worker 镜像由同一源码构建；worker 构建期
  `verify_managed_profile` 校验 manifest/patch 一致；真实拓扑测试通过
  `--expected-harness-commit` 绑定目标 Harness commit。

用户当前实例未更新/未重启；新 control 镜像无需修改现有
`managed-agent.json` 即可从镜像 manifest 取得基线目录；若现有文件已写
`models`，必须收窄在镜像目录内。运维手册同步留到 Task 6。

## 3. 红绿结果

### 红（父提交 `bcf001a0` 的生产代码 + 最终测试）

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser/test_managed_chat_model.py tests/multiuser/test_managed_profile.py tests/multiuser/test_relay_cli.py -q
```

实际结果：**25 failed, 33 passed**，退出码 1。见
`red-task2-unit.txt`。失败覆盖：

- 旧代码签名 Agent 没有有效路由 `model`（`agent["model"]` 缺失）；
- ACP 适配器把 raw `deepseek-flash` 发给真实目录形状的假 Harness，
  被 `unknown model option` 拒绝；
- `ConfigService`/`BrokerService` 不校验信任目录，未知模型可被发布/接纳；
- 受管配置不接受 `models` 字段；
- worker 不校验签名 Agent 的模型目录/允许列表；
- manifest/patch 没有共享模型目录，patch 只有 `deepseek-chat`。

### 绿（当前提交）

| 命令 | 结果 |
|---|---|
| 新模型链路单测 + profile + relay CLI | **58 passed**，见 `green-task2-new-unit.txt` |
| Task 2 指定七文件 | **119 passed**，见 `green-task2-targeted.txt` |
| Task 1 四文件回归 | **156 passed, 4 skipped**，见 `green-task1-regression.txt` |
| broker/config/relay 相关回归 | **158 passed**，见 `green-relay-related.txt` |
| 真实 TCP + 假 ACP | **2 passed, 2 warnings**，见 `green-tcp-related.txt` |
| `git diff --check` | 无输出，退出 0 |

4 个 skip 为既有平台条件，不是本任务新增。

## 4. 真实 Harness / 镜像 / 受控 provider

### 4.1 真实模型目录探针

`probe_harness_models.py` 在 A5 固定 Harness 镜像内、临时 HOME/workspace、
假 broker token 环境下运行；只做 initialize/session/new/set_config，不
发起模型请求。输出 `probe-harness-models.txt` 证明：

- `currentValue` 形状为 `["easel-broker","deepseek-chat"]`；
- 目录可列出 `deepseek-chat`、`deepseek-flash`、`deepseek-v4-pro`；
- 三个值都能通过 `session/set_config_option` 选中，返回的
  `currentValue` 与请求一致。

### 4.2 增量镜像（复用基础层）

未无缓存重建整个栈；使用 A5 已构建的固定 Harness/Chromium/依赖层做
增量镜像，只替换本轮受影响的 `easel` 代码、managed profile 和冒烟脚本：

- worker：`easel-worker-harness:task2-bcf001a0`
  - manifest list ID `sha256:b8a76e7044dd313cb6747f207e9f72a284e93274c7e070b7df76cd59dfe8c699`
- control：`easel-control-test:task2-bcf001a0`
  - manifest list ID `sha256:b3f2df4d51c24c0dd0b75505e3cf2c0bb5858f559e568d8b9f75a7202e2540a5`
- gateway 未改：`easel-gateway-test:g2`
  `sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a`

真实 Harness 镜像内门槛（`worker-image-smoke.txt`）：

```text
DSH_PROFILE_OK commit=687f282e8625c6b8d11ff58211a90524be2e4317 node=24.22.1 patch_sha256=96523f8ad2da5d2789e70814a8a62a12611d1e84642255dac80bf0828978175d model=easel-broker/deepseek-chat models=easel-broker/deepseek-chat,easel-broker/deepseek-flash,easel-broker/deepseek-v4-pro
WORKER_SMOKE_OK
```

`image-source-hashes.txt` 对 13 个 worker 文件与 8 个 control 文件逐项
比较源码与镜像内 SHA-256：**0 mismatches**。

### 4.3 真实拓扑（flash）

命令（显式开关与本轮镜像）：

```powershell
$env:EASEL_RUN_CONTAINER_TESTS='1'
$env:EASEL_TASK2_CONTROL_IMAGE='easel-control-test:task2-bcf001a0'
$env:EASEL_TASK2_WORKER_IMAGE='easel-worker-harness:task2-bcf001a0'
$env:EASEL_TASK2_MODEL='deepseek-flash'
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/integration/multiuser/test_managed_chat_model.py -q -s -k real_harness
```

实际结果：**1 passed, 1 deselected**（77.51s）。`real-flash-topology.txt` 记录：

```text
A5_REAL_PASS {... "provider_requests": 9, "token_rows": [revoked...], "worker_a": ..., "worker_b": ...}
TASK2_MODEL_AUDIT {"authorization_is_fake_sentinel": true, "chat_requests": 9, "provider_model": "deepseek-flash", "workspace_requests": 10}
```

覆盖：initialize/new/prompt、同会话追问/resume（进程重建后）、A/B
空间隔离、权限允许、provider 401/429/500/drop（每次恰好 1 个请求，
不自动重发）、Run 结束/令牌撤销。受控 provider 实际收到的 9 个 chat
请求 model 全部是 `deepseek-flash`，Authorization 是假 Key sentinel，
不是真实管理员 Key。

### 4.4 权限拒绝与停止后继续

同一组镜像/模型，复用 A6 真实浏览器闭环：

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/integration/multiuser/test_managed_chat_model.py -q -s -k permission_stop
```

实际结果：**1 passed, 1 deselected**（81.76s）。`real-flash-permission-stop.txt`：

```text
A6_2_PASS {"allow_requests": 2, "deny_requests": 2, "hang_state": "cancelled", "logout_close_seconds": 0.03, "retry_runs": 1, "slow_events": 4}
```

覆盖权限允许与拒绝（拒绝后未执行工具）、停止、停止后继续、SSE、
注销清理与令牌撤销。

### 4.5 broker 拒绝

- `BrokerService._validate_chat_route` 在 `ensure_issuable` 与
  `issue_with_route` 拒绝越界/空目录，不留下 broker token；
- 现有 `tests/multiuser/test_broker_forward.py` 的 JSON/multipart
  model 校验证明 broker 拒绝未授权 model；
- 新测 `test_broker_admission_rejects_*`、`test_broker_issue_rejects_*`
  覆盖接纳前拒绝。

## 5. 边界与未运行项

- 真实 DeepSeek 供应商通道、用户真实 Key、用户 prompt、用户运行实例：
  **未运行/未接触**。本轮受控 provider 是仓库 TLS fake provider，
  `TASK2_MODEL_AUDIT` 的 Authorization 为假 sentinel。
- 原 Web/登录/前端/UI：未修改；Task 3–6 未开始。
- 旧用户失败 Run 的直接触发点仍为 `history-unavailable`（Task 1 已记录）；
  本任务在隔离环境证明了“管理员 flash vs 受管 profile chat”的具体配置
  分叉，并消除该分叉，但不声称已精准归因旧 Run。
- 用户容器保持运行：本轮只做只读 `docker ps` 核对，未 Stop/down/restart/
  exec 写入/替换镜像；用户 control/worker/gateway/sidecar ID 与基线一致。
- 测试容器/网络/卷均带本轮随机 run-id 与标签；运行后核对：
  `easel.task2`、`easel.a5.harness`、`easel.a6.browser` 容器为空，
  `easel-a5*`/`easel-a6*` 网络与卷为空。未全局 prune，未删除历史
  `easel-ws-*` 网络。

## 6. 资源清理清单

- A5 真实拓扑测试：测试容器/网络/卷/临时 keys/workspace 由既有
  `cleanup()` 删除；provisioner 进程与 socket 目录清理。
- A6 浏览器测试：Topology.stop() 清理 control/gateway/worker/sidecar、
  网络、卷、临时 keys、浏览器与 TLS 终止器。
- 增量镜像与一次性 smoke 容器使用 `--rm`/本轮标签；用户镜像未替换。
- 证据中的路径已脱敏（如出现临时目录会替换为占位符）；无真实 Key/
  Cookie/邀请/prompt 写入证据。

## 7. R1 返工（T2-R1-01 / P2）：运行时目录绑定目标镜像

- R1 父 tip：`68789d3a36c96b3d087774e5aafc7cd33c18e2f5`
- 修改：`easel/control/snapshot.py`（`load_managed_profile_models`）、
  `easel/control/cli.py`（manifest 路径/有效目录装配）、`easel/cli.py`
  （`--managed-profile`、`--expected-harness-commit`）、
  `deploy/desktop/Dockerfile.control`（控制镜像内置受管 manifest）、
  测试与证据。Task 2 已通过的 token/签名同事务固定、ACP 映射和 Task 1
  诊断未改。

### 绑定规则

1. 可信基线目录来自控制镜像内 `/opt/easel/managed/acp-profile.json`
   （与 worker 镜像同一源码/受管 manifest）；校验 schema、Harness
   commit、provider id/api、默认模型与 `provider.models`。
2. 可选 `--expected-harness-commit` 或 `EASEL_EXPECTED_HARNESS_COMMIT`
   绑定目标 worker 镜像 commit；不符即失败。
3. `managed-agent.json` 的 `agent.models` 可选，只能收窄：
   - 镜像外模型 → 服务启动前 `managed_config_invalid`；
   - manifest 缺失/schema/版本不匹配 → `managed_profile_invalid`；
   - 未写 models → 使用镜像目录；写子集 → 有效目录为该子集。
4. 校验发生在 `Store.connect`/`uvicorn.run` 之前：失败不打开/改动控制库，
   不创建 Run/token/prompt。

### 红绿

- 红（父代码 + 最终测试）：
  `pytest tests/multiuser/test_managed_chat_model.py -q` →
  **6 failed, 28 passed**，见 `red-task2-r1-binding.txt`。
  失败覆盖：镜像外模型仍可启动、未写 models 得到空目录、缺失 manifest
  仍启动、版本不匹配仍启动、loader 不存在。
- 绿：
  - 新测 + profile/relay/config/proxy/broker/provider/worker/runs/
    diagnostics：**179 passed**，见 `green-task2-r1-targeted.txt`。
  - Task 1 四文件：**156 passed, 4 skipped**，见 `green-task1-r1.txt`。
  - `git diff --check`：无输出，exit 0。

### R1 镜像与真实链路

增量镜像（复用 A5 固定 Harness/依赖层；worker 也重建以保持镜像内
`easel` 源码一致）：

- control：`easel-control-test:task2-r1-68789d3`
  manifest list `sha256:2acb2485b07dbc65732c0c9996d987922a9d98dbe4fc9a14312217011b46e745`
- worker：`easel-worker-harness:task2-r1-68789d3`
  manifest list `sha256:c44c786aa4ef70734ca878fdd9e5ca90c390acf907fc4598b42b042a62c80195`
- gateway 未改：`sha256:c0a35aaa...`

证据：

- `control-delta-build-r1.txt` / `worker-delta-build-r1.txt`
- `worker-image-smoke-r1.txt`：`DSH_PROFILE_OK ... models=...` +
  `WORKER_SMOKE_OK`
- `image-source-hashes-r1.txt`：14 个 worker 文件 + 10 个 control 文件
  （含控制镜像内 manifest）逐项 SHA-256，**0 mismatches**
- `real-flash-topology-r1.txt`：真实拓扑 1 passed；受控 provider 9 个
  chat 请求全部 `deepseek-flash`，假 Authorization，resume/A-B/权限/
  401/429/500/drop 通过
- `real-flash-permission-stop-r1.txt`：A6 `A6_2_PASS`，权限允许/拒绝、
  停止后继续、注销与令牌撤销通过

真实测试的控制命令已显式传 `--expected-harness-commit`（取工作区
manifest commit），控制镜像启动时验证镜像内 manifest 与目标版本一致。

### 边界与清理

- 用户运行实例未更新、未重启、未替换镜像；用户容器 ID 与基线一致。
- 测试容器/网络/卷已按本轮标签清理；未全局 prune，未删除历史
  `easel-ws-*` 网络。
- 旧 Run 幂等/恢复仍使用已持久化快照；R1 只改变新控制进程的目录来源
  校验，未改 Run 状态机、token 事务或 ACP 映射。
- 真实 DeepSeek/用户真实聊天/原 Web/UI/Task 3+ 仍未运行或验收。
