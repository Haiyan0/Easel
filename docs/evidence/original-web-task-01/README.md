# 原 Web 修复 Task 1 实施证据：受管聊天失败诊断

- 父/基线提交：`1a8811096c7c86bb9079a5c88b92cd225d1907b1`
- 实施分支：`worktree-original-web-task1-diagnostics`（从基线创建的隔离 worktree）
- 实施提交：完整 SHA 见本次交付消息
- 日期：2026-10-29
- 范围：仅 Task 1；未修改模型/profile/受管路由/原 Web/登录页，未推进 Task 2

## 1. 精确文件清单

生产代码：

- `easel/harnesses/base.py`
- `easel/harnesses/rpc.py`
- `easel/harnesses/acp.py`
- `easel/worker/runs.py`

测试与 fixture：

- `tests/multiuser/test_run_diagnostics.py`（新增）
- `tests/fixtures/harness_diagnostics.py`（新增）

证据（本目录）：

- `README.md`
- `red-new-diagnostics.txt`
- `green-new-diagnostics.txt`
- `green-targeted.txt`
- `green-related-harness.txt`
- `green-related-tcp.txt`

## 2. 红绿命令与实际结果

### 红（基线提交，生产代码未改）

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser/test_run_diagnostics.py -q
```

实际结果：**15 failed, 2 passed**，退出码 1。完整输出见
`red-new-diagnostics.txt`。

- 15 个失败均因基线缺少阶段/分类/有界 stderr，或 worker 仍输出泛化的
  “Agent 执行失败，请检查本机 CLI、登录和模型配置”。
- 2 个通过项是兼容性护栏，不是新诊断行为：正常 ACP 流式文本/终态协议、
  `HarnessError(message)` 构造兼容。它们在基线即应通过。

### 绿（当前提交）

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser/test_run_diagnostics.py -q
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser/test_run_diagnostics.py tests/multiuser/test_runs.py tests/test_agent_harnesses.py tests/test_rpc_client_pressure.py -q
```

实际结果：

- 新增诊断测试：**17 passed**，见 `green-new-diagnostics.txt`。
- 执行单指定四文件：**147 passed, 4 skipped**，退出码 0，见
  `green-targeted.txt`。4 个 skip 为基线已有的平台条件跳过（POSIX 进程组/
  npm 启动器等），不是本任务新增的 skip/xfail。

```powershell
git diff --check
```

实际结果：无输出，退出码 0。

## 3. 阶段与安全字段定义

### 阶段（`HarnessError.stage`）

| stage | 含义 | 主要设置点 |
|---|---|---|
| `cli_start` | 启动 Agent 进程/解析受管命令 | `rpc.py` 子进程 OSError；`acp.py` 缺少受管命令 |
| `initialize` | ACP initialize 或协议版本校验 | `acp.py` initialize 调用 |
| `session` | session/new、session/load、session/resume | `acp.py` 会话建立/恢复 |
| `model` | session/set_config_option、session/set_model | `acp.py` 模型选择 |
| `prompt` | session/prompt、停止原因异常 | `acp.py` prompt 调用 |
| `rpc` | 通用 JSON-RPC 断连/无效帧/并发上限 | `rpc.py` 读取与等待 |
| `unknown` | worker 收到的异常没有可识别阶段 | `runs.py` 固定词表兜底 |

等待具体 RPC 时发生的断连，`RPC.call(stage=...)` 会把阶段归到该调用
（例如 prompt 期间子进程退出 → `prompt`）；没有具体调用的协议错误保持
`rpc`。子进程启动失败在创建进程前即标记 `cli_start`。

### 类别（`HarnessError.category`）

只有 ACP JSON-RPC `error.data` 中的结构化字段才产生类别：

- `data.status` / `data.statusCode` / `data.status_code` /
  `data.httpStatus` / `data.http_status`：401/403 → `auth`；
  429 → `rate_limited`；400/413/415/422 → `request_rejected`；
  5xx → `service_unavailable`。
- `data.code` / `data.error` 的已知字符串：`invalid_token`/`unauthorized`/
  `forbidden` → `auth`；`rate_limited`/`rate_limit_exceeded`/
  `too_many_requests` → `rate_limited`；`invalid_request`/`bad_request`/
  `validation_error` → `request_rejected`；`service_unavailable`/
  `upstream_unavailable`/`overloaded` → `service_unavailable`。

`message` 文本即使包含 “401”“unauthorized”“invalid api key” 也不参与
分类；没有上述结构化证据时类别为 `unknown`，终态 reason 为
`agent_failed`，不会猜成 “Key 错误”。

实现位置：`easel/harnesses/rpc.py:67`（`classify_error_evidence`）与
`easel/harnesses/rpc.py:318` 附近的错误回复处理。

### 持久分类（`run_admissions.reason`）

- 有结构化类别：`harness_auth`、`harness_rate_limited`、
  `harness_request_rejected`、`harness_service_unavailable`
- 无证据/旧异常：`agent_failed`
- 已有的 `timeout`、`stopped`、`output_storage_failed` 等语义不变

### 用户可见 error 事件（仍是字符串）

固定格式，不含异常 message、stderr、prompt、路径、命令或环境：

```text
Agent 执行失败（阶段：选择模型；类别：上游限速；状态码：429；任务：<runId>）
Agent 执行失败（阶段：执行本轮对话；类别：未知原因（未获得可分类的上游证据）；任务：<runId>）
Agent 执行超时，已终止本轮进程（任务：<runId>）
```

`runId` 只允许字母/数字/`-_.:`，最长 128 字符；不合法时显示 `unknown`。
当前 `ControlApp` 直接消费字符串，无需改动。

运维日志（`easel.worker.runs`）只记录：

```text
run failure run_id=<id> stage=<stage> category=<category> status=<status|None> error_type=<异常类名>
```

不记录 message、stderr、prompt、用户路径、完整命令或 env。

## 4. stderr 上限、排空与清理证据

- 固定上限：`MAX_STDERR_BYTES = 4096`
  （`easel/harnesses/rpc.py:22`）。
- `_drain_stderr` 持续按 8 KiB 块读取直到 EOF；内存中只保留前 4096 字节，
  另外只累计整数字节数；不按行读取、不无界采集。
- 子进程 stderr/stdout 任务在 `RPC.__aexit__` 取消并 `gather`；进程树按原
  逻辑终止。
- 覆盖测试（均为真实假 ACP 子进程，无 sleep 推进）：
  - `test_stderr_is_bounded_and_drained`：写入 65,536 字节且无换行；
    断言 `len(stderr_buffer) == MAX_STDERR_BYTES`、`stderr_total == 65536`、
    `stderr.done()`、进程已退出。
  - `test_stderr_burst_on_failure_leaves_no_tasks`：写完 stderr 立即退出；
    失败路径仍排空到上限、无遗留任务。
  - `test_cancel_during_stderr_burst_cleans_tasks`：子进程写满 stderr 后
    阻塞在 stdin；测试取消待处理调用并关闭连接，断言 reader/stderr 任务
    均完成、进程退出、缓冲不超上限。
- stderr 原文不进入 UI、持久事件或运维日志；本任务没有把 stderr 内容用于
  分类，避免根据模糊文本猜测。

## 5. 兼容性回归

- `HarnessError(message)` 仍可用；`str(error)` 仍返回原消息；新增
  `stage/category/status` 默认均为 `None`。
- 正常 Run/流式文本/终态：`test_worker_normal_acp_stream_and_terminal_protocol`
  通过真实假 ACP 子进程断言 thinking/token、`done` 状态与消息落盘。
- 问题卡、停止、取消清理、会话互斥、输出收尾等既有语义由
  `tests/multiuser/test_runs.py` 覆盖，本次四文件回归全部通过。
- 额外相关回归（不影响执行单结论，仅证明改动未扩散）：
  - `test_worker_runtime.py`、`test_sessions_chat.py`、
    `test_worker_execution_gate.py`、`test_harness_workspace.py`：
    **35 passed, 2 skipped**，见 `green-related-harness.txt`。
  - `test_tcp_integration.py`（真实 TCP：control ↔ 两个 worker + 假 ACP）：
    **2 passed, 2 warnings**，见 `green-related-tcp.txt`。警告为既有
    websockets 弃用提示。

## 6. 真实 / fixture / 未运行

- 本轮全部为 **fixture/单元/进程内**：假 ACP 子进程、假 Key、假 broker
  token；未调用真实 DeepSeek 或其他收费供应商。
- 未运行真实 Harness + 受控 provider、真实容器/Docker 集成、浏览器/
  Playwright、前端构建、备份恢复/升级联锁；这些留给后续任务或另行安排。
- 未读取或复制真实 Key/.env/.dsh/Cookie/邀请；未重发用户 prompt。
- 未接触用户运行中的 control/worker/gateway/sidecar 容器：没有执行
  Stop/down/restart/exec 写入或替换镜像。

## 7. 旧失败 Run 的归因结论

- 旧 Run 的 stderr 在基线实现中被直接排空；持久错误只有泛化字符串，
  没有阶段/类型/状态码。因此**无法事后追溯该次失败的直接触发点**。
- 结论明确记为：`unknown` / `history-unavailable`。本任务没有读取用户
  容器内的旧 Run 记录，也没有重放 prompt。
- 不声称“真实聊天已修好”；本任务只交付诊断能力与受控复现证据。

## 8. Task 2 输入（不冒充根因）

已知模型配置分叉单独记录为 Task 2 输入：

- 当前网页 chat v1：`provider=deepseek`、`model=deepseek-flash`、
  `protocol=openai`。
- 受管 `deploy/multiuser/managed/acp.patch.yml` 与模型目录仍固定
  `deepseek-chat`。

本轮没有修改模型、profile、受管路由或 broker，也没有把该分叉认定为
旧 Run 的唯一根因。

## 9. 测试资源清理清单

- 所有假 ACP 子进程由 `RPC.__aexit__` 终止；相关测试断言
  `process.returncode is not None`、reader/stderr 任务完成。
- 池化 ACP 连接由 `connection` 的异常路径或 `manager.aclose()` 关闭；
  `test_worker_reports_safe_stage_category_and_run_id` 断言当前事件循环的
  连接池为空。
- 测试临时目录由 pytest `tmp_path` 管理；没有创建容器、镜像、网络或持久
  测试卷。
- 未触碰基线中记录的用户容器、卷、邀请、Cookie 或 provisioner socket。

## 10. 代码质量

- 新增 `tests/multiuser/test_run_diagnostics.py` 与
  `tests/fixtures/harness_diagnostics.py`：`ruff check` 全部通过。
- 四个生产文件 `ruff check` 仍只报告基线已有规则（`BLE001`、`SIM102`、
  `I001`、`UP035` 等），本任务新增代码未引入新的 ruff 告警。
- `git diff --check` 无输出。
