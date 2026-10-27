# Task A2 Desktop 拓扑实测与接入契约证据

日期：2026-10-27（Asia/Shanghai）

基线：`f1c4e5ef98ea756513071367919fe57fac7dbf17`（A1 验收通过）。
2026-10-27 A2 R1：补齐精确 gateway 路由、查询白名单、两套凭证、实例代次与
恢复协议；清理/探针测试已改为可汇总、可失败的实现。仍只有契约与临时资源
实测，没有实现 gateway。
本报告只覆盖 A2 的只读盘点、临时资源实测和拓扑设计提交，不证明 A3/A4
执行 worker、真实 Harness、聊天闭环或正式 Linux 发布门槛。

## 结论

- **管理授权可行**：WSL2 宿主 provisioner + 挂载 WSL Unix socket 的 control
  容器可以继续使用现有 `ensure`/`stop`/`inspect` 协议；`SO_PEERCRED` 实测
  报告 UID 10001，非授权 UID 被拒绝。
- **宿主健康探测不可直接用 HTTP**：WSL/Windows 宿主到 worker 容器 IP 的 HTTP
  请求超时；真实 provisioner `ensure` 因 HTTP 探测失败返回
  `worker_not_ready`。计划改用固定 `docker exec` healthcheck，尚未实现。
- **control→worker 不可直接沿用当前独立 bridge**：独立 control 容器到
  worker 内部网络超时；把 control 加入 worker 网络会使 worker→control 也
  可达，已作为反例记录并拒绝。
- **拓扑提案待批准**：新增每 workspace 受信 gateway，management listener 只
  绑定 control 网络地址，固定路径/方法白名单、逐 workspace 认证、SSE 透传和
  失败清理。运行时代码未实现，A3/A4 未开始。

机器可读契约：[`deploy/desktop/topology.json`](../../deploy/desktop/topology.json)；
设计说明：[`deploy/desktop/README.md`](../../deploy/desktop/README.md)。

## 1. 只读盘点

| 项目 | 实际值 |
|---|---|
| 平台 | Windows 11 Pro 10.0.26200 |
| WSL | 2；默认 `Ubuntu-26.04`；另有 `Ubuntu`、`docker-desktop` |
| WSL 内核 | `6.6.87.2-microsoft-standard-WSL2` |
| WSL 用户 | `uid=1000 gid=1000`；在 `docker` 组 |
| Docker CLI（Windows） | 29.2.1；context `desktop-linux`；`npipe:////./pipe/dockerDesktopLinuxEngine` |
| Docker CLI（WSL） | `/usr/bin/docker`；context `default`；`/var/run/docker.sock` |
| Engine | Docker Desktop 4.63.0 (220185)，Engine 29.2.1，linux/amd64，overlayfs |
| 路径 | `C:\Code\Easel` ↔ `/mnt/c/Code/Easel` |
| 7860/7861/8898/8899/2375/2376 | 盘点时无监听者 |
| 现有资源 | 只读；未操作开发者容器、卷、网络或 WSL 发行版 |

未执行：安装/升级发行版、修改 `.wslconfig`、切换 Docker context、修改
Docker Desktop 全局设置。未启动任何既有开发者容器或读取其数据。

## 2. 管理授权实测

临时资源的 socket 父目录 `0750`、socket `0660`，owner/group 为 WSL
UID/GID 1000；容器从 WSL Docker CLI 启动。

| 场景 | 实际结果 |
|---|---|
| 容器 `10001:10001` + `--group-add 1000` 通过挂载 socket 连接 | 成功；服务端 `SO_PEERCRED (0, 10001, 10001)` |
| 容器 `10001:10001` 不加补充组 1000 | `PermissionError: [Errno 13] Permission denied` |
| 容器 UID 1000 连接真实 provisioner（allowed UID 10001） | `RESULT {"code": "unauthorized", "ok": false}` |
| 容器 UID 10001 + group 1000 连接真实 provisioner | `RESULT {"managed": false, "ok": true}`（无容器时的合法 inspect） |
| Windows Docker CLI 通过 `\\wsl.localhost\...` 挂载 socket 文件 | 文件可 stat 为 socket，但 0660 下实际连接 permission denied；不作为批准路径 |

结论：control 必须从 WSL Docker CLI 启动/重建，或由 Windows 启动脚本显式
调用 WSL；control 使用 UID 10001、补充组 1000，provisioner 只允许 10001。
Windows 原生 Python 不具备 Linux peer-credential 语义，未用于授权。

## 3. 路径映射实测

| 资源 | 实测 |
|---|---|
| `C:\Code\Easel` / `/mnt/c/Code/Easel` | 两视角存在；WSL 可读 |
| `/mnt/c` 目录作为 Docker bind 源（WSL CLI） | 文件在容器内可见 |
| WSL ext4 `/tmp` 目录作为 Docker bind 源（WSL CLI） | 文件在容器内可见 |
| WSL `/tmp` Unix socket 文件挂载进容器 | socket 可见且可连接 |

`/tmp` 只用于 socket，不用于持久 workspace；持久 workspace 继续选择
Windows 可挂载的耐久路径（契约提议 `C:\Easel\workspaces`）。测试没有创建或
改写开发者 HOME/工作区。

## 4. 宿主健康探测

临时 worker 使用 `python:3.12-slim` 或 `easel-worker-test:task8`（仅本机临时
测试镜像），内部网络不发布端口。

| 探测 | 结果 |
|---|---|
| 容器内 `GET http://127.0.0.1:7860/internal/lifecycle/health` | 200，身份字段正确 |
| Windows 宿主 HTTP → worker 容器 IP:7860 | HTTP 超时 |
| WSL 宿主 HTTP → worker 容器 IP:7860 | HTTP 超时 |
| WSL 宿主 TCP connect → worker 容器 IP:7860 | 可能成功；不能证明 HTTP/身份/就绪 |
| 真实 provisioner `ensure`（宿主 HTTP `_probe`） | `worker_not_ready`，容器按失败清理 |

因此当前 A1/A2 形态不能把宿主 HTTP 探测作为健康门槛。提案使用固定
`docker exec <worker> python -m easel.worker.lifecycle healthcheck --port 7860`，
目标为核验过的不可复用 worker container_id（不能只按名称），固定 argv、10
秒超时；退出 0 还要匹配 healthcheck 身份/workspace/config_version/
container_id/ready，任意退出 0 不算通过。A3 还要验证真实 Run 路由/身份，
不只 lifecycle。待主审批准后实现。

## 5. control→worker 网络

使用随机内部/外部 Docker 网络和两个临时 HTTP 容器；没有发布 worker 端口。

| 路径 | 实测 |
|---|---|
| 独立 bridge 的 control → worker 内部网络 | HTTP 超时 |
| worker → 独立 bridge 的 control | `Network is unreachable` |
| control 加入 worker 网络后 control → worker | 200 |
| control 加入 worker 网络后 worker → control | 200（双向暴露，拒绝） |
| control 加入 worker 网络、只绑定 control 网络地址 | control→worker 200；worker→control ws 地址 `Connection refused`；worker→control ctl 地址 `Network is unreachable` |
| 上述绑定到 control 地址的临时服务，Windows/WSL → `127.0.0.1:17864` 发布端口 | 200（仅证明发布路径，不证明最终 control 服务） |

结论：直接让 control 加入 worker 网络不是可接受契约。必须新增控制面到
worker 的受信 gateway，或由主审批准另一种等价的单向网络设计；本次不实现。

## 6. A2 R1 精确 gateway 契约

A2 R1 只修订契约与测试；没有实现 gateway。精确表以
[`deploy/desktop/topology.json`](../../deploy/desktop/topology.json) 的
`network.gateway.routes` 为准：

| Method | Path | Query |
|---|---|---|
| GET | `/internal/lifecycle/health` | 无 |
| POST | `/internal/runs/{run_id}` | 无 |
| GET | `/internal/runs/{run_id}/events` | `after`（整数，可选，默认 0，>=0，最大 event_id）；`nonce`（字符串，可选，1..128，RFC3986 解码一次）；均拒绝重复/未知项 |
| POST | `/internal/runs/{run_id}/stop` | 无 |
| POST | `/internal/runs/{run_id}/questions/{question_id}/answer` | 无 |
| POST | `/internal/runs/{run_id}/questions/status` | 无 |
| GET | `/internal/sessions/{session_id}/messages` | `limit`（整数，可选，默认 100，1..500）；`offset`（整数，可选，默认 0，>=0）；均拒绝重复/未知项 |

- 已移除不存在的 `/question`、`/question/answer`；历史读取使用
  `/internal/sessions/{session_id}/messages`。
- 路径只接受 origin-form，解码一次；拒绝绝对 URI、编码的 `/` 和 `\`、
  `%00`、尾斜杠、重复斜杠、片段及越界 ID；`run_id`/`session_id` 为
  32 位小写 hex，`question_id` 为单个 1..256 字节路径段。
- 请求上限沿用/对齐现有 relay：header 64 KiB、body 64 MiB、连接 10 秒、
  I/O 与 SSE idle 120 秒；每 gateway 最多 8 个并发请求/SSE。SSE 逐块
  `write`+`drain`，客户端慢时暂停上游读取，不缓存完整流；客户端断开取消
  上游；不自动重发 POST。
- control→gateway 使用 control Ed25519 签名断言（30 秒、nonce、method/
  path/query/body hash、workspace/gateway instance/generation/worker
  container_id/config_version 绑定）；gateway 只读公钥。gateway→worker
  使用 provisioner `.credentials` 提供的每 worker service 凭证。两者独立；
  浏览器 Cookie/Authorization/外来 `X-Easel-*` 不进入下游，由可信侧重建。
- `WorkerHandle.container_id` 仍是 worker 不可复用 Docker ID；
  `gateway_instance_id`/`gateway_generation` 单独绑定。旧 stop 按旧 gateway
  ID，不会删除新 gateway；旧断言 audience 不匹配新 gateway，旧 worker
  service 凭证不能访问新 worker。
- 恢复协议：control 重启只重载既有签名私钥；provisioner 重启只重建 socket，
  已有 gateway/worker 不删；gateway 重建必须产生新 ID/generation，旧断言
  失效；gateway 不可用时 control 503，不删 active worker，Run 不标记完成，
  GET/SSE 可按 `after` 恢复，POST 不自动重发；gateway 更换在核验过的同一
  worker 实例/config 上进行并保留 worker；config_version 变化必须
  drain/stop 后重建。不存在 per-gateway 共享 token，因此不会出现“旧
  gateway 配新 token”的不可用组合。

## 7. 清理、探针反例与红绿

### 清理和探针修订

- A2 测试改用 `tests/multiuser/topology_support.py` 的逐项 inspect/归属/
  删除/汇总逻辑；容器先于网络清理，避免 active endpoint 让网络删除失败。
- 所有 `docker run` 客户端都有显式随机名称和 `easel.test.run-id` 标签；
  Docker CLI 超时后仍按名称/标签清理。
- 单项删除非零、删除超时、inspect 超时、归属不符、删除后已不存在、
  named client CLI 超时均有纯逻辑测试；后续资源继续处理，最终错误不吞掉。
- `_assert_wsl_blocked` 只接受约定 `returncode=3` 且 stdout 含 `BLOCKED`；
  WSL 启动、解释器、探针脚本失败会作为基础设施错误变红。隔离测试先验证
  两端容器回环 HTTP 200，再做跨网络探测。
- socket 测试与真实 provisioner unauthorized 的区别保留：socket 测试证明
  挂载与 peer UID；真实 provisioner 的 authorized/unauthorized 结果见第 2 节
  手工实测，不冒充由 pytest 覆盖。

### 红

修复清理排序前，真实测试在清理阶段出现（测试业务探测本身已通过）：

```text
FAILED test_worker_http_is_blocked_from_windows_and_wsl
FAILED test_bridge_control_and_worker_networks_are_isolated
FAILED test_joining_control_to_worker_network_is_bidirectional
3 failed, 6 passed
A2 cleanup failed: network ... has active endpoints (...)
```

这正是“测试成功但清理失败不得假绿”的红；修复为容器先于网络清理后消失。

### 绿

```text
C:\Users\hyh\anaconda3\envs\Agent\python.exe -m pytest tests/integration/multiuser/test_desktop_topology.py -q
......sssss                                                              [100%]
6 passed, 5 skipped in 0.09s

$env:EASEL_RUN_CONTAINER_TESTS = '1'
C:\Users\hyh\anaconda3\envs\Agent\python.exe -m pytest tests/integration/multiuser/test_desktop_topology.py -q
...........                                                              [100%]
11 passed in 22.60s
```

真实测试覆盖：Windows/WSL HTTP 阻断（严格 BLOCKED 契约）、独立 bridge
隔离、control 加入 worker 网络的双向反例、Windows/WSL bind 可见性、WSL
socket 挂载与 UID 10001 peer credential，以及所有契约/清理/探针反例。

### 回归与静态检查

| 命令 | 结果 |
|---|---|
| `pytest tests/multiuser/test_desktop_deployment.py tests/multiuser/test_topology_support.py tests/test_document_paths.py -q` | **64 passed** |
| `pytest tests/multiuser/test_provisioner.py tests/multiuser/test_worker_lifecycle.py -q` | **107 passed, 1 skipped** |
| `ruff check tests/integration/multiuser/test_desktop_topology.py` | 通过 |
| `python -m json.tool deploy/desktop/topology.json` | 通过 |
| `git diff --check` | 退出 0 |

## 8. 清理与边界

- 全部临时容器/网络使用随机 `easel-a2-*` 名称与
  `easel.test.run-id=<run>` 标签；包括临时客户端；测试和手工探针按精确
  名称/标签清理。
- 最终复查 `docker ps -a --filter label=easel.test.run-id`、
  `docker network ls --filter label=easel.test.run-id` 均无 A2 资源；
  WSL `/tmp/easel-a2-*` 为空。
- 未执行全局 `prune`；未停止/删除开发者容器、卷、网络或 ngrok 隧道。
- `.scratch` 中的临时探针脚本/日志不进入提交。

## 9. 未执行与剩余限制

- gateway 仅设计，运行时代码未实现；不进入 A3/A4。
- 没有重新构建或启动 A1 control 镜像；A1 的登录/浏览器证据不变。
- 没有执行真实 Harness、供应商调用、正式 Linux/XFS/防火墙/Chromium
  沙箱门槛。
- 持久路径只证明 bind 可见性；A3 前还需 UID 10001 的真实创建/SQLite/
  锁/fsync/原子替换/重开探针；`/tmp` 不得作为持久根。
- 本机 Docker Desktop 的宿主到容器 IP HTTP 路径按本次实测判定为不可用；
  若 Docker Desktop 升级改变行为，需要重新跑 A2 实测并更新契约。
- 设计批准前，不把 WSL/Windows 直连、host-network 或 control 加入 worker
  网络作为已通过路径。
