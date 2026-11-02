# 验收实例 503 定点修复交付报告

日期：2026-11-02（Asia/Shanghai）

实施分支/提交：`fix/acceptance-503-attempt-recovery` / `7d6599b845011e57b893baa5ffea0afbe84958ae`
基线：`665f7e8`（主仓库 `feat/multiuser-task1-store`）
实施 worktree：`C:\Code\Easel\.claude\worktrees\acceptance-503-repair`

本轮未更新、未重启、未停止用户正在运行的验收实例
`C:\Easel\acceptance-data` / `C:\Easel\acceptance-workspaces`；未修改其
DB、attempt 文件、Key、Cookie 或容器。所有真实容器验收使用随机
project/网络/卷和临时假材料，并按本轮标签清理。

## 1. 根因

现场 DB 的 worker lease 是
`state=stopping, cleanup_purpose=start_abort, cleanup_scope=gateway,
cleanup_worker_created=0, cleanup_gateway_created=1, attempt_id=A`；
磁盘 attempt 文件却是后来的纯复用 attempt B：

```json
{"attempt_id":"B","worker_created":false,"gateway_created":false,
 "scope":"gateway","status":"pending"}
```

`Provisioner._with_gateway` 在 `attempt_id` 非空时无条件写 attempt 记录。
纯复用调用没有新建资源，却仍写入了一个 `scope=gateway` 但两个 created
均为 false 的记录，既覆盖了 pending 清理承诺 A，又不符合
`_load_attempt` 的自洽校验。之后：

- `abort(A)` 因 attempt 不匹配返回 `gateway_abort_superseded`；
- provisioner 重启后该记录无法加载；
- control 的 `_recover_stop` 无法越过该校验，DB 永久 stopping，原 Web
  首屏和 POST 全部 503。

同时确认 6A R1 的“同 workspace 排队不计 ready_timeout”仍存在；受管模式
Dashboard 的 `control CLI` 提示也会误导用户。

## 2. 修复内容

### attempt 归属与持久化

- 只有真正创建了 worker/gateway 的 attempt 才写 pending 清理承诺。
- 纯复用写 `scope=none` 的 completed/no-op tombstone；它不删除任何资源，
  只作为迟到 abort 的代次围栏。
- 不同的 pending attempt 不能被覆盖：返回 `gateway_cleanup_pending`，
  创建路径在失败前清理本轮已建资源。
- 成功启动后 control 调用新的 `commit_attempt`，把对应记录收口为
  completed；commit 失败只记录 warning，由下一次 ready 读取按精确
  worker/gateway ID 重试。
- 只有 control 明确声明“新代次、DB 无 attempt/container”时，才允许
  `orphan_takeover` 接管控制面崩溃遗留的孤儿 pending 记录。
- 旧版无资源坏记录的恢复使用新的 `recover_abort`，限制为 gateway-only，
  且必须先通过：
  - 坏记录本身明确 `worker_created=false, gateway_created=false`；
  - 请求的 worker/gateway ID 与坏记录完全一致；
  - 真实 worker 的 workspace/instance/config_version 校验；
  - 真实 gateway 的 managed/workspace/worker/config_version 校验。
  任一不符返回 `gateway_abort_recovery_refused`，绝不按名称删除。
- gateway-only 恢复后 DB 没有 gateway ID；`stop` 现在允许空 gateway ID，
  但只在确认没有同名 gateway 时继续停止 worker/sidecar，否则
  `gateway_target_mismatch` 失败关闭。

### 并发启动预算

- `ensure_ready` 在进入时确定 deadline，等待 workspace 锁的时间计入
  `ready_timeout`；获锁后不再重置完整等待窗口。
- 排队取消不会污染后续启动；跨 workspace 仍然并发。
- POST 业务请求仍只在取得 handle 后转发一次，重试不会重放业务请求。

### 受管状态文案

- 新增 `web/frontend/src/lib/statusCopy.ts`，受管模式不再出现
  “请先安装/配置 control CLI”；gateway 状态以 control 的可信 lease/handle
  为准，stopping/unavailable 显示“受管工作区未就绪”。

## 3. 红证据

主审只读探针未修改，原样运行于 665f7e8 与 4382180：

```text
{"original_attempt_overwritten": true,
 "original_abort_error": "gateway_abort_superseded",
 "record_loadable_after_restart": false}
TEMP_CLEANUP=ok; LIVE_RESOURCES_TOUCHED=false
```

排队预算对照（同一受控场景）：

```text
base 665f7e8      outcome=queued_call_blocked_past_budget provisioner_calls=1
fixed worktree    outcome=worker_start_timeout            provisioner_calls=1
```

新测试在实现前先运行并观察到失败：

- `tests/multiuser/test_attempt_ownership_503.py`
- `tests/multiuser/test_runtime_gateway.py` 新增队列、commit、恢复、drain 用例
- `tests/frontend/controlStatusCopy.test.mjs`

## 4. 绿证据

### 单元/集成回归

```text
tests/multiuser（全量）         1758 passed, 7 skipped
前端 Node 测试                    193 passed
前端 tsc + vite build             通过（仅既有 chunk 大小提示）
前端 oxlint                       通过
ruff（变更文件）                  仅剩基线已有告警
```

（最终数字以 `docs/evidence/acceptance-503-repair/full-multiuser.txt`、
`frontend-tests.txt` 为准。）

### 真实容器隔离验收（A4）

使用最终 control 镜像、6A gateway 镜像和 A4 execution worker，随机
project/网络/卷、假 Key、受控 TLS provider；未访问用户实例：

```text
A4_IMAGES {"control":"sha256:2582886195031ac4d71746bbddf670270e47f21f09a2d0bf55d26d1d00011ac2",
           "gateway":"sha256:0d30939d375c1e3bef616678f5e8d7b6fad687143817f7d6183579ca8e881f0a",
           "worker":"sha256:d46f0dd1f17273e91f3d3cca7e68854ca1dc55231a64b446ef058ad7b5066ec8"}
A4_FIRST_SCREEN 全部 200: /api/status /api/ideas /api/schedule /api/outputs /api/accounts /api/personas
A4_GATEWAY_RECOVERY same_worker=true tokens_before=1 tokens_after=1
A4_FIRST_SCREEN_AFTER_RECOVERY 全部 200
A4_GATEWAY_REPLACEMENT same_worker=true old/new gateway 不同
A4_PROVIDER requests=2 model=deepseek-chat sentinel_header_ok=true
A4_REAL_PASS
1 passed
```

### 真实 Harness 受控 provider

使用 `easel-worker:task6a-4382180`（含 `/opt/dsh`）与受控 TLS
fake provider；结果见 `a5-harness-final.txt`：

```text
A5_IMAGES {"control":"sha256:258288…","gateway":"sha256:0d3093…",
           "worker":"sha256:6f62af…"}
A5_REAL_PASS provider_requests=9
             token_rows=[revoked=true, revoked=true]
             worker_a/worker_b 隔离
2 passed
```

真实供应商、真实平台账号/短信/发布未执行。

### 同构现场恢复

`test_field_shape_legacy_record_recovers_through_runtime_manager` 使用真实
Store + 真实 Provisioner + 临时目录，复刻“DB stopping/gateway A、磁盘
无资源 B”的现场；恢复后 DB 回到 ready/unavailable，worker ID/token
保留，attempt 记录 A 变为 completed，gateway 只停止一次。

## 5. 镜像与源码身份

| 项 | 值 |
|---|---|
| 实施 commit | `7d6599b845011e57b893baa5ffea0afbe84958ae` |
| control 镜像 | `sha256:2582886195031ac4d71746bbddf670270e47f21f09a2d0bf55d26d1d00011ac2` |
| control 内 `runtime.py` SHA-256 | `dfee655f19570f37edbfb85af099b4a0243a41c10f3552d057efa67017661cfe`（与工作树一致） |
| provisioner 源码 SHA-256 | `d4e157c1482edb78b67fcea05ee27edd38ce611914b9e6113db37ce7903599ba` |
| gateway 镜像（未改） | `sha256:0d30939d375c1e3bef616678f5e8d7b6fad687143817f7d6183579ca8e881f0a` |
| 用户当前 worker/gateway | `sha256:02ff3af…` / `sha256:5ea06bb…`（未被本轮改动） |
| 前端 dist | `index.html` `ca8a6917…`；`index-D5UTpITU.js` `7a64e481…`；`index-Da4NYWkv.css` `f5c7b6c1…` |

control 镜像构建命令：

```powershell
docker build -f deploy/desktop/Dockerfile.control `
  --build-arg HTTP_PROXY=http://host.docker.internal:7897 `
  --build-arg HTTPS_PROXY=http://host.docker.internal:7897 `
  -t easel-control:503-fix <NEW_SOURCE>
docker image inspect --format '{{.Id}}' easel-control:503-fix
```

## 6. 用户实例恢复与更新（待主审批准后执行）

实际实例参数（只读登记）：

```text
ProjectName   = easel
DataRoot      = C:\Easel\acceptance-data
WorkspaceRoot = C:\Easel\acceptance-workspaces
Port          = 7863
PublicOrigin  = https://acts-revise-unrented.ngrok-free.dev
PublishMode   = manual
WslRepo       = /mnt/c/Code/Easel-acceptance
```

不要照抄 `easel-acceptance`、`easel-desktop-multi` 或旧目录。

### 6.1 更新顺序

1. 主审批准后，把 `C:\Code\Easel-acceptance` 更新到提交
   `7d6599b845011e57b893baa5ffea0afbe84958ae`，并确认
   `git -C C:\Code\Easel-acceptance status` 干净/符合预期。
2. 从该源码构建 control 镜像，确认镜像 ID 为
   `sha256:2582886195031ac4d71746bbddf670270e47f21f09a2d0bf55d26d1d00011ac2`
   （或记录本次实际 ID）。worker/gateway 镜像不变。
3. 用现有 `Start-WorkspaceEasel.ps1` 参数启动，补上
   `-Image <新 control 镜像 ID> -ProjectName easel -Port 7863
   -WorkspaceRoot C:\Easel\acceptance-workspaces
   -DataRoot C:\Easel\acceptance-data -PublishMode manual
   -WslRepo /mnt/c/Code/Easel-acceptance
   -WorkerImage sha256:02ff3af… -GatewayImage sha256:5ea06bb…`。
   `Start` 会用新源码生成 provisioner-start.sh。
4. 更新后由用户在网页手动重试一条短消息。首屏请求会触发
   `ensure_ready -> recover_stop -> recover_abort`；若状态从后端 503
   变为正常对话，再区分后续模型上游错误。
5. 若要先做备份：新 control 的 `Stop-WorkspaceEasel.ps1` 会通过
   `drain_all` 对已过期的 stopping promise 执行同一受限恢复，再正常
   Stop；Stop 完成后再运行 `Backup-WorkspaceEasel.ps1`。

### 6.2 旧坏状态恢复语义

- 自动恢复只处理 `cleanup_purpose=start_abort`、`cleanup_scope=gateway`、
  `worker_created=0`、`gateway_created=1`，且坏记录两个 created 均为
  false、worker/gateway/config 身份精确匹配的情况。
- 恢复会先持久化重建的 attempt A，再停止精确 gateway；随后
  `finish_gateway_abort` 回到 ready/unavailable，保留 worker、service
  token、在途 Run、容量与历史。
- 若返回 `gateway_abort_recovery_refused`、`gateway_attempt_record_unreadable`
  或 `gateway_cleanup_pending`：不要 SQL 改 ready、不要清空 attempt
  文件、不要按名称删容器、不要 down/删卷；保留原始文件/日志并交回
  主审。拒绝本身就是安全边界。

### 6.3 Stop / Backup / 回退

```powershell
# Stop（新 control 的 drain 会先恢复过期 stopping）
powershell -File deploy/desktop/Stop-WorkspaceEasel.ps1 `
  -ProjectName easel -DataRoot C:\Easel\acceptance-data `
  -WorkspaceRoot C:\Easel\acceptance-workspaces `
  -WslRepo /mnt/c/Code/Easel-acceptance

# Backup（必须在 Stop 完成后）
powershell -File deploy/desktop/Backup-WorkspaceEasel.ps1 `
  -ProjectName easel -DataRoot C:\Easel\acceptance-data `
  -WorkspaceRoot C:\Easel\acceptance-workspaces `
  -Image sha256:2582886195031ac4d71746bbddf670270e47f21f09a2d0bf55d26d1d00011ac2 `
  -WslRepo /mnt/c/Code/Easel-acceptance
```

若 Stop 仍返回 `worker_drain_incomplete` 或 `gateway_cleanup_failed`：
记录准确错误码/日志，不要 `-Force`、不要绕过屏障直接 down、不要删卷
或手改 DB；交回主审。

回退使用备份恢复到**新项目/新目录/新端口**，不覆盖原实例：

```powershell
powershell -File deploy/desktop/Restore-WorkspaceEasel.ps1 `
  -Backup <备份目录> -ProjectName easel-rollback `
  -PublicOrigin https://acts-revise-unrented.ngrok-free.dev `
  -Image sha256:9272b5562d2d9bfbdeef1d60e3ccb910dc5f90ccc24e79802ad9178548d769d2 `
  -WorkerImage sha256:02ff3af7913a90c63677a6cfcc1602187465441fd7eb680537e74b73880c5b57 `
  -GatewayImage sha256:5ea06bbef5a6c349e14c815239dc64683295c8d505d5d26fa13aa74596059eac `
  -Port 7864 -DataRoot C:\Easel\acceptance-data-rollback `
  -WorkspaceRoot C:\Easel\acceptance-workspaces-rollback `
  -WslRepo /mnt/c/Code/Easel-acceptance
```

## 7. 明确未执行

- 未更新/重启用户验收实例；未读取或写入用户 Key、Cookie、邀请、prompt。
- 未调用真实 DeepSeek/第三方供应商，未执行真实平台登录/短信/发布。
- 未合并、未推送、未进入 Linux 生产部署。
- 真实 Harness 验收使用受控 TLS fake provider，不是真实计费请求。
- WSL/Linux 原生生产生命周期边界沿用既有手册的未验证状态。

## 8. 证据文件

同目录下 `*.txt` 为原始命令输出（已脱敏，不含真实 Key/Cookie/prompt）：

- `red-probe.txt`：主审探针原始输出
- `queue-budget.txt`：排队预算修复前后对照
- `a4-503-final.txt`：真实 A4 拓扑最终通过日志
- `a5-harness-final.txt`：真实 Harness 受控 provider 日志
- `full-multiuser.txt`：最终 multiuser 全量 pytest
- `frontend-tests.txt`：前端 Node 测试
- `frontend-build-lint.txt`：前端 build/lint
- `image-hashes.txt`：源码/镜像/前端产物哈希
