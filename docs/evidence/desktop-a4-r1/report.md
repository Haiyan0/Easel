# Task A4 R1 返工证据

日期：2026-10-28（Asia/Shanghai）

基线：`81e9cc18f6ff698fdc16abff7a0418260e0217ee`（A4 首轮提交）。
返工提交：本次提交（`git rev-parse HEAD`）；只实施 A4-R1-01 至 A4-R1-06，
未进入 A5/A6，未重开 G1/G2/A3。

## 1. 六项结果

### A4-R1-01：egress 只获得出口策略

- 新增独立 `<project>-egress` 外置卷，只放 `egress-policy.json`（0600）。
- `compose.workspace.yaml` 的 egress 只挂 `egress:/policy:ro`，命令读取
  `/policy/egress-policy.json`；不再挂 `/keys`。
- Initialize 创建第三个卷并复制策略；Start/Stop/Status/Common 契约同步；
  A1 登录编排通过 `-IncludeEgress` 保持旧两卷行为。
- 真实同 UID 容器测试：control 可读 master key、签名私钥和控制库；
  egress 挂载边界、gateway 镜像、worker 镜像均不可读。只输出布尔结果，
  未打印任何 key 内容。

### A4-R1-02：gateway 503 对账不再停止/结算

- `_reconcile_run` 在 gateway 模式下，events GET 未成功读取原实例状态时
  直接返回，不发送 stop、不撤销 broker token、不释放容量。
- 红测已派发且过 deadline，失败输出实际为 `['GET', 'POST']`。
- 保留 direct 模式原有超时停止语义。

### A4-R1-03：过期 refresh / gateway-only stopping 的旧 Run 恢复

- `restore_gateway` 对已过期的 `starting` 使用
  `owner/generation/attempt/lease_expires_at` CAS 接管；未过期仍明确忙。
- gateway-only `stopping` 按持久 attempt/scope 调用 `abort`，随后
  `finish_gateway_abort`，再恢复 gateway；绝不调用 ensure/冷启动 worker。
- 双 Store 连接、旧 owner 迟到完成 CAS 反例均覆盖；旧 Run 的读取/停止入口
  沿用 `bound_handle` → `restore_gateway`，不等待新 submit。
- 同时修复并发 refresh 轮询把暂态 `RuntimeUnavailable` 当成实例消失、
  过早返回 503 的问题；真实 gateway 重启链路因此恢复单次请求成功。

### A4-R1-04：Start 复用/新建 provisioner 身份核验

- provisioner v2 新增 `identity` 只读动作：实际 Docker context/Engine ID、
  worker/gateway 镜像、workspace 根、sidecar/gateway 控制网络、broker
  upstream、worker/gateway 公钥 SHA-256、egress policy SHA-256、socket
  UID/GID/mode/allowed UID。
- 新增 `deploy/desktop/provisioner_identity.py`：同 UID 客户端 `show`/`check`，
  不读取 key 内容；漂移只输出字段名。
- Start 在复用前和新建后都执行 check；配置漂移返回
  `provisioner_config_mismatch`，错误服务/读取失败返回
  `provisioner_identity_unavailable`；不盲杀活跃进程。
- Status 检查实际端口/挂载/密钥/配置卷/egress 隔离/身份/屏障；所有必需项
  成功才 `status=ok`，否则输出 `status=failed failures=...` 并非零退出。

### A4-R1-05：跨进程停止接纳屏障与终态复核

- schema v14 追加 `control_state`，`admission_barrier` 对同库所有 control
  进程可见。
- `claim_worker_start`、`reserve_run`、`create_user`、`admin_create_member`
  在写事务内复查屏障；已接纳 Run 的 stop/events/answer/历史仍走既有句柄。
- `admin drain --barrier` 先写屏障，再 drain；`drain_all` 复核无活跃 Run、
  未过期请求租约、starting/ready/draining/stopping 实例后才返回。
- `admin resume` 显式恢复；Stop 失败/取消保留屏障失败关闭，Start 检测到
  屏障返回 `admission_barrier_active`。
- 双 Store 并发 submit/新 workspace、既有 stopping 不假报完成、CLI
  drain/resume 均有测试。

### A4-R1-06：文件系统持久化错误不再被吞掉

- 新增 `easel/worker/fsutil.py`：只对 `/proc/self/mountinfo` 确认的
  9p/drvfs/virtiofs 开发挂载、且 errno 属已知不支持集合时容忍
  `fchmod`/目录 fsync；原生 Linux 的 EIO/ENOSPC/EPERM 一律传播。
- sessions 原子写、消息追加、run 输出暂存统一使用该边界；文件 fsync 与
  目录 fsync 失败都返回明确错误。
- WSL 原生目录 fd 注入 EIO：返回 `session_write_failed`，旧消息仍可读且
  无临时文件残留；9p `/mnt/c` 的 EINVAL 可容忍，EIO 仍失败。

## 2. 红证据

关键实际红输出：

```text
R1-02: assert ['GET', 'POST'] == ['GET']
R1-03: RuntimeUnavailable: worker_gateway_refresh_in_progress
       RuntimeUnavailable: worker_stopping
R1-04: AttributeError: 'Provisioner' object has no attribute 'identity'
       FileNotFoundError: deploy/desktop/provisioner_identity.py
R1-05: AttributeError: 'Store' object has no attribute 'set_admission_barrier'
R1-06: ImportError: cannot import name 'fsutil' from 'easel.worker'
```

R1-01 基线 compose 的 egress 实际为 `keys:/keys:ro`，可读 master key 与
签名私钥；新增的 compose/同 UID 隔离测试在修复后通过。

真实链路复跑还暴露并修复了：

- 并发 gateway refresh 轮询把 `worker_gateway_refresh_in_progress` 当成
  实例消失，导致 gateway 重启后首次历史请求 503；改为继续等待 CAS 完成。
- Windows PowerShell 5.1 对无 BOM UTF-8 中文注释的换行吞并，导致 Stop 中
  `$workerDrained = $true` 被当成注释；为本次四个 Workspace 脚本补 UTF-8
  BOM，并用 PowerShell AST 确认赋值语句存在。真实 Stop 输出恢复
  `drained=True`。

## 3. 绿证据

```text
A4 R1 定向（gateway client/relay/runtime/stop barrier/provisioner identity/
worker fs durability/Desktop PowerShell）
120 passed in 10.66s
```

```text
tests/multiuser + Desktop 拓扑静态
1350 passed, 10 skipped, 2 warnings in 156.73s
```

```text
G1 gateway auth/forward/stream/TCP
120 passed, 2 warnings in 2.10s
```

```text
真实 control→gateway→worker→sidecar→broker
1 passed in 76.84s
```

```text
真实同 UID 材料隔离
1 passed in 4.89s
```

```text
Desktop Initialize/Start/Status/Stop 临时项目
初始化/启动通过；Status status=ok；admin drain --barrier → verified=settled；
barrier-status/resume 正常；Stop drained=True；资源全部清理
```

全量 `pytest tests` 快照：**2677 passed, 62 failed, 87 skipped**。失败仍集中在
未改动的 setup/bash 编码与 Starlette TestClient/httpx2 环境兼容测试；A4/R1
触及路径在定向、multiuser、G1 和真实链路中全绿。

## 4. 真实链路输出

```text
A4_IMAGES {"control": "sha256:588b18691b13966c33875b48597f63787151a6b87d4ed9ae8f390835376be5ee", "gateway": "sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a", "worker": "sha256:39f7c847d931c91f486c06e867eab799f2db38ef72854a4f0564b8fcc61f4f24"}
A4_HANDLES [{"gateway": "3863663de5d779f0c6649863cd2a1290ee3611fb7751afdb2ac35fb75dc8c95f", "worker": "4a22686f678db86d3aab27c41ca3dcea580b09dd58495d9d6d849588640d6e3b", "workspace": "a6df82efd476440d908590a0330b5e5b"}, {"gateway": "4d3c0ee1343bd1d94c52b85f6890c3474bdf75adce9bd2336f2f335c6873e991", "worker": "592177e51bd039f8c2f851124250925a7c45bd017958e4e930681730eadc258d", "workspace": "11ba89aecb8b4b9695fa0b8a451f6fd2"}]
A4_GATEWAY_RECOVERY {"gateway": "3863663de5d779f0c6649863cd2a1290ee3611fb7751afdb2ac35fb75dc8c95f", "same_worker": true, "tokens_after": 1, "tokens_before": 1, "worker": "4a22686f678db86d3aab27c41ca3dcea580b09dd58495d9d6d849588640d6e3b", "workspace": "a6df82efd476440d908590a0330b5e5b"}
A4_GATEWAY_REPLACEMENT {"new_gateway": "8dd0aa6a21428854413a9bca6f2b9c6c780cba54d0201621c50be5f51e3254da", "old_gateway": "3863663de5d779f0c6649863cd2a1290ee3611fb7751afdb2ac35fb75dc8c95f", "same_worker": true, "tokens": 1, "worker": "4a22686f678db86d3aab27c41ca3dcea580b09dd58495d9d6d849588640d6e3b", "workspace": "a6df82efd476440d908590a0330b5e5b"}
A4_PROVIDER {"cross_workspace": 404, "model": "deepseek-chat", "requests": 2, "sentinel_header_ok": true, "wrong_channel": 404}
A4_REAL_PASS
```

真实材料隔离只输出 control/egress/gateway/worker 对三类材料路径的布尔可读性，
未输出 key 内容或真实数据库内容。

## 5. 边界与清理

- 未运行 ngrok 公网浏览器、真实 DeepSeek Harness、真实供应商计费或 A6
  双用户浏览器闭环。
- 未操作开发者容器/卷/网络/HOME/profile，未读取 ngrok authtoken 或供应商
  秘密；未执行全局 prune。
- 真实链路、材料隔离和脚本 smoke 的随机容器、网络、卷、WSL socket 已逐项
  清理；固定测试镜像保留。
- `control` schema 追加 v14；历史迁移未改写，schema 指纹测试同步更新。
- worker 测试镜像仍由已验收 execution 基础镜像覆盖本轮改动的
  `fsutil.py`/`sessions.py`/`runs.py` 生成，未重建未变化的 Chromium/Playwright
  层；生产镜像仍由完整 Dockerfile.worker 生成。
