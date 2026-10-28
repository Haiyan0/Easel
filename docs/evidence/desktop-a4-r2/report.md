# Task A4 R2 定向返工证据

日期：2026-10-28（Asia/Shanghai）

基线：`f5451df4a1f752605ed17fcb2f7434258ddb3011`（A4 R1 提交）。
返工提交：本次提交（`git rev-parse HEAD`）；仅修 A4-R2-01/02/03 及关联
测试/文档，未进入 A5/A6，未重开 G1/G2/A3 或已关闭的 R1 项。

## 1. 三项结果

### A4-R2-01：公钥以 key 卷私钥派生值为可信基准

- `keygen.py --check --fingerprints` 从 key 卷内真实 Ed25519 私钥派生
  worker raw 公钥和 gateway PEM 公钥，只输出两个 SHA-256；同时验证卷内
  公钥与私钥一致，不输出私钥或 key 内容。
- Start 以这两个摘要为唯一可信期望，比较宿主 `DataRoot/public` 副本；
  策略同时比较 key 卷/egress 卷/宿主副本三方摘要。任何不一致返回
  `public_copy_mismatch:<name>` / `policy_copy_mismatch`，不自动换 key。
- Status 复用同一契约；expectedIdentity 与 provisioner observed 都只能
  通过可信摘要比较。
- 真实两套 Ed25519 key：key 卷保持 A，宿主 worker 副本替换为 B；Start
  实际返回 `public_copy_mismatch:worker`。恢复 A 后 Start 成功。容器/脚本
  只输出摘要或布尔状态，没有私钥外流。

### A4-R2-02：Engine/context 与 socket 身份精确核验

- Start/Status 分别只读采集 Windows Docker CLI 与指定 WSL 的 Engine ID；
  两者不一致返回 `docker_engine_mismatch`，采集失败返回
  `docker_engine_unavailable` / `wsl_docker_engine_unavailable`。
- 从指定 WSL 采集 context、进程 UID、主 GID；GID 必须等于 control 使用的
  `ProvisionerGid`，socket 期望 mode 660、allowed UID 10001。
- expectedIdentity 增加 engineContext/engineId/socketUid；客户端
  `compare_identity` 精确比较 Engine ID/context、socket UID/GID/mode/
  allowed UID。actual 非空不再视为正确。
- 真实环境：Windows/WSL Engine ID 均为
  `4c5b9122-bd30-42bd-af1e-019c5053a914`，WSL context `default`，
  UID/GID 1000；Status 输出 `provisioner_engine=default/4c5b...` 且
  `provisioner_identity=ok`。
- PowerShell 入口用错误 Engine/UID 的期望值反例覆盖；不同真实 daemon
  需要第二台 Engine，本轮未修改全局 context，故未构造第二个真实 daemon。

### A4-R2-03：成功 Stop 的可重开完成记录

- Stop 成功 drain 并 `compose down` 后，用一次性 control 容器写入
  `stop_completion`：barrier ID、project、expected-identity SHA-256、
  完成时间；不提前无条件清屏障。
- 失败/取消没有完成记录，屏障保持失败关闭。
- Start 发现屏障时调用 `admin reopen`；仅当 barrier ID、project、配置摘要
  与完成记录完全匹配，且 Store `drain_status` 无活跃 Run/请求/未收口实例，
  才在同一事务内清除屏障。新 barrier、旧完成记录或配置漂移均拒绝。
- 真实临时项目连续两轮 Stop→Start 成功：

```text
Status: provisioner_engine=default/4c5b9122-... identity=ok status=ok
Stop 1: barrier=stop-0de83fdfa18e43d2ae1282108ff859c4
        stop_completed=stop-0de83fdfa18e43d2ae1282108ff859c4
Start 2: admission_barrier_reopened=stop-0de83fdfa18e43d2ae1282108ff859c4
Stop 2: barrier=stop-d07f50f4f84f43348db4030fad9a392b
        stop_completed=stop-d07f50f4f84f43348db4030fad9a392b
```

- 失败屏障无完成记录时 `admin reopen` 返回非零/`admission_barrier_not_reopenable`；
  旧 stop ID、错误项目、错误配置摘要、新 barrier 取代旧记录均有反例。
- 保留 R1-05 的并发 submit/新 workspace 屏障与 G2 精确实例停止。

## 2. 红证据

```text
R2-01  keygen.py: error: unrecognized arguments: --fingerprints
R2-02  compare_identity missing engineContext/engineId/socketUid mismatches
R2-03  AttributeError: 'Store' object has no attribute 'complete_stop'
       AttributeError: 'Store' object has no attribute
       'reopen_after_completed_stop'
```

R2-01 真实反例（主审）：key 卷第一套私钥，宿主 public 第二套公钥，
旧实现两道检查通过但签名失败；本轮实现后实际 Start 返回
`public_copy_mismatch:worker`。

## 3. 绿证据

```text
R2 定向（Desktop PowerShell/identity/Stop barrier/A1 兼容）
118 passed in 43.36s
```

```text
tests/multiuser + Desktop 拓扑静态
1361 passed, 10 skipped, 2 warnings in 180.74s
```

```text
G1 gateway auth/forward/stream/TCP
120 passed, 2 warnings in 2.20s
```

```text
真实 control→gateway→worker→sidecar→broker
1 passed in 88.44s
```

```text
真实同 UID 材料隔离
1 passed in 8.84s
```

```text
Desktop 脚本真实流程
Initialize/Start/Status/Stop 通过；Status status=ok；
两轮 Stop→Start 正常重开；临时资源清理
```

全量 `pytest tests` 快照：**2690 passed, 60 failed, 87 skipped**。失败仍集中
在未改动的 setup/bash 编码、Starlette TestClient/httpx2 环境测试；A4/R2
触及路径在定向、multiuser、G1 和真实链路中全绿。上一轮新增 PowerShell
身份测试无法加载 `Get-FileHash` 的问题已通过 .NET SHA-256 helper 移除
依赖，9 个 Start 身份测试（含 keygen 错副本、Engine 不匹配/采集失败、
关闭/重开屏障）全部通过。

## 4. 真实链路输出

```text
A4_IMAGES {"control": "sha256:1d9c559e97f172381da28dbbec4e1afeb008405cddbea972c0f2baecff7deb16", "gateway": "sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a", "worker": "sha256:39f7c847d931c91f486c06e867eab799f2db38ef72854a4f0564b8fcc61f4f24"}
A4_HANDLES [{"gateway": "f6d614f14ea38024bdd1076ba3b74d8b44e4c735ca16f8b532ff260e5b961c56", "worker": "8ef8a08a3a513662b5315bb7ab2d2a1cb87d48ec698f458efeaba3291e34ee4d", "workspace": "8f3d65578e6f4494b2e4c7a9f72ef60d"}, {"gateway": "a816b8f1f0f986e6d12115c7dfc148b79e2c4cc3e71b37c68c953fae260ab7c3", "worker": "ea339d130546dbc3aa23596937d9ae323681741f7c641e32dd55c21bdb9a5eb7", "workspace": "1bd0b34547c8486fb5d015d27c9ec015"}]
A4_GATEWAY_RECOVERY {"gateway": "f6d614f14ea38024bdd1076ba3b74d8b44e4c735ca16f8b532ff260e5b961c56", "same_worker": true, "tokens_after": 1, "tokens_before": 1, "worker": "8ef8a08a3a513662b5315bb7ab2d2a1cb87d48ec698f458efeaba3291e34ee4d", "workspace": "8f3d65578e6f4494b2e4c7a9f72ef60d"}
A4_GATEWAY_REPLACEMENT {"new_gateway": "fe2b1f0ccb7a97d63370c4893295aa7d04da7dac135366dc3e164eb5326790a7", "old_gateway": "f6d614f14ea38024bdd1076ba3b74d8b44e4c735ca16f8b532ff260e5b961c56", "same_worker": true, "tokens": 1, "worker": "8ef8a08a3a513662b5315bb7ab2d2a1cb87d48ec698f458efeaba3291e34ee4d", "workspace": "8f3d65578e6f4494b2e4c7a9f72ef60d"}
A4_PROVIDER {"cross_workspace": 404, "model": "deepseek-chat", "requests": 2, "sentinel_header_ok": true, "wrong_channel": 404}
A4_REAL_PASS
```

## 5. 清理与边界

- 未运行 ngrok 公网浏览器、真实 DeepSeek Harness、真实供应商计费或 A6
  双用户浏览器闭环。
- 真实第二 daemon 的 Engine 漂移未构造；不做全局 context 修改。Engine/
  context/socket UID 的差异用真实 Store/客户端反例与 PowerShell 入口覆盖。
- 未操作开发者容器/卷/网络/HOME/profile；只使用随机归属测试项目，未执行
  全局 prune。
- 真实链路、材料隔离、两轮启停和 PowerShell 临时资源已清理；固定镜像
  保留。
- 未新增 schema 迁移；继续使用已追加的 v14 `control_state` 保存完成记录。
- worker 测试镜像仍由已验收 execution 基础层覆盖本轮未变化的
  `fsutil.py`/`sessions.py`/`runs.py` 生成；生产镜像仍由完整
  Dockerfile.worker 构建。
