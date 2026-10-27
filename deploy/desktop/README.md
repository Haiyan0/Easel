# Task A2：Desktop 完整拓扑契约与可行性报告

状态：**实测完成，拓扑提案待主审批准；未实现运行时代码，不进入 A3/A4。**

基线：`f1c4e5ef98ea756513071367919fe57fac7dbf17`（A1 验收通过）。
本目录只增加 A2 契约、反例和证据；没有修改认证核心、A1 Compose
拓扑或现有 provisioner/worker 行为。

## 1. 结论先行

| 能力 | 实测结论 | A2 处理 |
|---|---|---|
| WSL2 宿主 provisioner 调用 Docker | 可用；Ubuntu-26.04 内 Docker CLI 使用 `/var/run/docker.sock` 指向同一 Docker Desktop Engine | 保留 |
| control 容器使用现有 Unix socket 协议 | **可用**：WSL `/tmp` socket 挂载进容器，UID 10001 + 补充组 1000，`SO_PEERCRED` 正确报告 10001 | 固定契约 |
| WSL/Windows 宿主到 worker 容器 IP 的 HTTP | **不可用**：TCP 可能显示已连接，但 HTTP 请求超时；不能作为健康探测 | 改为固定 `docker exec` healthcheck，待批准 |
| 当前 control 网络到 worker 容器 | **不可达**：独立 bridge 到内部 worker 网络超时 | 不能直接沿用 |
| 直接把 control 加入 worker 网络 | control→worker 可达，但 worker→control 也可达 | 拒绝，作为反例记录 |
| 新增每 workspace 管理中继 | 推荐方案；避免 control 加入每个 worker 网络，并保持 control 无 Docker socket | **仅提交设计，不实现** |
| WSL `/mnt/c` 作为 worker bind 源 | 可用 | 固定 |
| WSL ext4 路径作为 bind 源 | WSL Docker CLI 可挂载；`/tmp` 不持久，只用于 socket | 耐久 workspace 仍使用 Windows 路径 |
| 每 workspace broker URL | 继续使用 `http://easel-sidecar-<workspace_id>:8899` | 不变 |

因此 A2 **不能**以“control 直接 HTTP 访问 worker 容器 IP”为验收基础。当前
拓扑在管理授权上可行，在 control→worker 和宿主健康探测上缺一个受信管理中继/
固定健康探测路径；这两项改变网络或管理边界，必须先由主审批准。

机器可读契约见 [topology.json](topology.json)；完整证据见
[docs/evidence/desktop-a2/report.md](../../docs/evidence/desktop-a2/report.md)。

## 2. 只读盘点（2026-10-27）

| 项目 | 实际值 |
|---|---|
| Windows | Windows 11 Pro 10.0.26200 |
| WSL | 2；默认为 `Ubuntu-26.04`（另有 `Ubuntu`、`docker-desktop`） |
| WSL 内核 | `6.6.87.2-microsoft-standard-WSL2` |
| WSL 用户 | UID 1000 / GID 1000；在 `docker` 组 |
| WSL Docker CLI | `/usr/bin/docker`；context `default`；socket `/var/run/docker.sock` |
| Windows Docker CLI | context `desktop-linux`；endpoint `npipe:////./pipe/dockerDesktopLinuxEngine` |
| Engine | Docker Desktop 4.63.0 (220185)，Engine 29.2.1，linux/amd64，overlayfs |
| 路径 | `C:\Code\Easel` ↔ `/mnt/c/Code/Easel` |
| 现有端口 | 未监听 7860/7861/8898/8899/2375/2376 |
| 现有资源 | 只读盘点；未操作任何开发者容器、卷、网络或 WSL 发行版 |

未执行：安装/升级 WSL 发行版、修改 `.wslconfig`、切换 Docker context、
修改 Docker Desktop 全局设置、改动任何现有容器/卷/网络。

## 3. 管理授权契约（已实测）

### 3.1 Unix socket

- provisioner 位置：WSL2 宿主进程（推荐 Ubuntu-26.04）。
- socket：`/tmp/easel-runtime/provisioner.sock`；父目录 `0750`，socket
  `0660`，owner/group 为 WSL UID/GID 1000。
- control 容器：UID 10001，补充组 1000；挂载为
  `/run/easel/provisioner.sock`。
- 协议：继续使用现有 provisioner v1（`ensure`/`stop`/`inspect`），只接受
  workspace UUID + 配置版本；不增加镜像/命令/挂载/网络参数。
- 授权：provisioner `--allowed-uid 10001`；`SO_PEERCRED` 是唯一授权依据。
  Windows 原生 Python 没有 Linux peer-credential 语义，**禁止**冒充。
- control 启动方式：必须从 WSL 侧 Docker CLI 启动/重建（或由 Windows
  启动脚本调用 `wsl.exe ... docker compose`）。Windows 可通过 UNC 看到并把
  socket 挂进容器，但本次没有把 UNC 下的 0660 权限语义作为批准依据。

实测（临时资源，非开发者资源）：

- 660 socket + 容器 `10001:10001` + `--group-add 1000`：连接成功，
  `SO_PEERCRED (pid, uid, gid) = (0, 10001, 10001)`。
- 同容器不加补充组 1000：`Permission denied`。
- 容器 UID 1000：可连接，但 provisioner 明确返回 `unauthorized`。
- 真实 provisioner + 真实 Unix socket：授权 UID 10001 得到 `managed=false`
  （无容器时的合法 inspect）；非授权 UID 1000 得到 `unauthorized`。

### 3.2 路径

- worker 持久根使用耐久路径，推荐
  `C:\Easel\workspaces`（WSL 视角 `/mnt/c/Easel/workspaces`）。
- 容器路径固定：`/workspace`、`/home/easel`、`/workspace/state`。
- `/mnt/c` 和 WSL ext4 路径均可被 WSL Docker CLI 作为 bind 源；本次用
  `/tmp` 目录和 `/mnt/c` 目录分别实测。`/tmp` 不具备跨重启持久性，只用于
  Unix socket；耐久 workspace 推荐使用 Windows 路径。
- WSL `/tmp` socket 文件可以被 Docker 挂载，但其生命周期与 provisioner
  一致，provisioner 重启后必须重建 control 挂载。
- A3 前必须由 UID 10001 实际验证持久根的创建、SQLite 读写、文件锁、fsync、
  原子替换和重开。如果 Windows 挂载不满足权限/锁语义，改用耐久 WSL ext4
  根并记录 Windows 备份方式；`/tmp` 永远不作为持久 workspace 根。本轮只
  证明 bind 可见性，不把 marker 可读当成持久性验收。

## 4. 健康探测契约（待批准）

WSL 宿主和 Windows 宿主到 worker 容器 IP 的 HTTP 健康请求均实测超时；TCP
连接成功不能证明 HTTP、身份或就绪。当前 provisioner 的 `_probe` 使用
宿主 HTTP，因此真实 `ensure` 在临时资源上返回 `worker_not_ready`（容器创建后
按失败清理）。

提案（**未实现**）：

- 使用固定命令从 provisioner 侧执行：
  `docker exec <worker> python -m easel.worker.lifecycle healthcheck --port 7860`
- 目标必须是已核验 worker inspect 的不可复用 container_id，不能只用容器名；
  固定 argv、10 秒超时；退出 0 还必须由 healthcheck 返回的 workspace/
  config_version/container_id/ready 全部匹配，任意退出 0 不算通过。
- A3 执行能力接入后，healthcheck 还要验证真实 Run 路由/身份，而非只验证
  lifecycle-only 入口；只通过 lifecycle check 不得标记执行能力 ready。
- 若主审不接受 `docker exec`，替代方案必须是宿主本地受控代理；不能把
  WSL/Windows 宿主直连容器 IP 写成已通过。

## 5. control→worker 网络契约（设计，待批准）

### 5.1 已拒绝的直接路径

| 路径 | 实测 | 拒绝原因 |
|---|---|---|
| 独立 control bridge → worker 内部网络 | 超时 | 不可达 |
| WSL 宿主 HTTP → worker IP | HTTP 超时 | 不能用于健康/转发 |
| Windows 宿主 HTTP → worker IP | HTTP 超时 | 不能用于健康/转发 |
| control 容器 `--network host` → worker | 可达 | Windows/ngrok 侧访问不到 host-network 监听，且 host network 是边界扩张 |
| control 容器加入每个 worker 网络 | 双向可达 | worker 能访问 control；违反“worker 不访问控制面” |

control 加入 worker 网络且监听 worker 地址时，control→worker 与
worker→control 均可达；把监听只绑定到 control 网络地址的变体本次实测
worker→control 的 ws 地址被拒，但 control 仍是每个 workspace 网络的成员，
配置回归就可能暴露控制面。两种变体都不作为 A2 契约；后者若主审采用，必须
单独批准并持续验证单向性。

### 5.2 推荐方案：每 workspace 受信 gateway

新增 `easel-gateway-<workspace_id>`（运行时代码待批准后实现）：

- 网络：同时加入 `easel-control` 与 `easel-ws-<workspace_id>`；后者是
  `--internal`。gateway 加入共享 `easel-control` 后，**不能**声称网络上
  其他 gateway/sidecar 无法访问它；跨 workspace 保护依靠独立 control
  断言、固定 workspace/worker 目标和协议白名单，并在实施验收补 A 凭证访问
  B 的反例。
- 监听：management listener **只绑定 control 网络地址**。provisioner 先
  `docker create` gateway，inspect 实际控制网络 endpoint，要求恰好一个
  IPv4，把精确地址作为 `EASEL_GATEWAY_BIND` 在 start 前注入；地址缺失、
  歧义或启动前后变化均失败关闭。禁止 `0.0.0.0`、首张网卡或名称解析推断。
- 不发布宿主端口、不挂 Docker socket、不挂 worker workspace/home、不读
  控制数据库；仅只读挂载 control 公钥；只读 rootfs、UID 10001、
  cap-drop ALL、no-new-privileges、固定资源上限。worker service 凭证由
  provisioner 的 `.credentials/<workspace_id>.env` 提供给 gateway，不来自
  control/浏览器，且不写入日志。
- 请求方向：control → gateway（control 网络）→ 固定 worker（worker 网络）。
  不接受 worker 主动发起的管理请求。
- 认证：
  - **control→gateway**：control 现有 Ed25519 私钥签名的短时断言，独立
    domain `easel.desktop.gateway.v1`。gateway 只读持有 control 公钥；断言
    绑定 workspace、gateway instance/generation、worker container_id、
    config_version、method、path、canonical query、body SHA-256、issued/
    expires、nonce、request_id；有效期 30 秒，nonce 单次使用（有界内存
    缓存），每次请求重新签名。
  - **gateway→worker**：每 worker 独立的 service 凭证，由 provisioner 的
    `.credentials` 环境提供；gateway 不接受 control 或客户端替换它，且固定
    `container_id` + `config_version`，不按 workspace 名自动落到新实例。
  - Cookie、Authorization、Forwarded、`X-Forwarded-*`、外来 `X-Easel-*`
    永不传给下游；gateway 先剥离同名/所有 `X-Easel-*`，再从可信配置重建
    worker 身份头。浏览器凭证不参与 gateway 认证。

### 5.3 精确路由与查询白名单

只接受下表 method/path；未知方法/路径默认拒绝，不按前缀放行。路径 ID
`run_id`/`session_id` 为 32 位小写 hex；`question_id` 解码后必须是单个
路径段（1..256 字节，不得为 `.`/`..`，不得含 `/`、`\`、`?`、`#`、NUL、
CR、LF）。路径只接受 origin-form，解码一次，拒绝绝对 URI、编码的 `/`/
`\`、`%00`、尾斜杠、重复斜杠、片段和重定向。

| Method | Path | Query |
|---|---|---|
| GET | `/internal/lifecycle/health` | 无 |
| POST | `/internal/runs/{run_id}` | 无 |
| GET | `/internal/runs/{run_id}/events` | 可选 `after`、`nonce`，规则见下 |
| POST | `/internal/runs/{run_id}/stop` | 无 |
| POST | `/internal/runs/{run_id}/questions/{question_id}/answer` | 无 |
| POST | `/internal/runs/{run_id}/questions/status` | 无 |
| GET | `/internal/sessions/{session_id}/messages` | 可选 `limit`、`offset`，规则见下 |

- `events`：`after` 为整数，默认 0，最小 0，最大为 worker 当前最大
  event_id；`nonce` 为可选字符串，1..128，按 RFC3986 解码一次；两项均
  最多出现一次，重复参数拒绝；未知参数拒绝；查询顺序无关；不做二次解码。
- `messages`：`limit` 为整数，默认 100，范围 1..500；`offset` 为整数，
  默认 0，最小 0；两项均最多出现一次，重复参数拒绝；未知参数拒绝。
- 响应：拒绝 worker 的 3xx，不跟随重定向；剥离 `Set-Cookie`、`Location`
  与 hop-by-hop 头。
- 请求上限参考现有 relay：header 64 KiB、body 64 MiB、连接 10 秒、
  I/O/SSE idle 120 秒；每 gateway 最多 8 个并发的普通请求/SSE（与现有
  全局活跃 Run 上限 8 对齐），超出拒绝。SSE 逐块 `write`+`drain`，
  客户端慢时暂停上游读取，不缓存完整流；客户端断开立即取消上游读取；
  不自动重发任何 POST；GET/SSE 可按 `after` 游标恢复。

### 5.4 实例代次与恢复

- `WorkerHandle.container_id` 始终保留 worker 不可复用 Docker ID；新增
  `gateway_instance_id` 与 `gateway_generation`，`WorkerLease`/control
  绑定持久化二者。`WorkerHandle.upstream` 指向 gateway；不能用 gateway
  身份替代 worker ID。gateway 标签绑定 workspace、config_version、
  worker container_id，目标来自 provisioner 核验的实际 worker 地址。
- `stop` 同时携带 worker container_id 与 gateway_instance_id；旧 stop
  不能删除新 gateway。断言 audience 含 gateway ID/generation，旧断言不能
  访问新 gateway；旧 worker service 凭证不能访问新 worker。
- control 重启：重新加载现有受保护签名私钥；gateway/worker 不变，重新
  签发新断言，不重发旧凭证。
- provisioner 重启：重建 Unix socket；已有 gateway/worker 不删除；control
  重连后按不可复用 ID 复核并继续。
- gateway 重启：同一容器/ID/generation 可继续；若被重建，gateway 产生新
  ID/generation，control 必须使用新句柄，旧断言因 audience 不符被拒。
- gateway 暂不可用：control 返回 503；不删除 active worker；Run 不标记 done；
  SSE 断开可带 `after` 恢复；POST 不自动重发。
- gateway 更换：按旧 gateway_instance_id 停止旧 gateway，在核验过的同一
  worker 实例/config 上创建新 gateway；不删除 worker；在途请求失败，GET/
  SSE 按游标恢复，POST 不自动重试。
- control 签名私钥不可用：gateway 无法验证断言，管理面 503；不得删除
  worker，直到显式 drain/stop。worker 更换或 config_version 变化时，旧
  gateway/worker 必须 drain/stop 后重建新代次，禁止热换挂载或凭证。
- 不存在 per-gateway 共享 token；因此 control/gateway/provisioner 独立重启
  不需要恢复一把“只存在于某一进程”的 gateway token，也不会出现旧 gateway
  配新 token 的不可用组合。

相对原“宿主 provisioner”设计的权限差异：

- provisioner 仍独占 Docker 管理权限；gateway 没有 Docker socket，不能
  创建/停止容器、不能改挂载、不能访问宿主。
- gateway 是受信管理中继，能看到其固定 workspace 的 worker 请求；它不能
  访问其他 workspace，也不能获得宿主文件系统权限。
- control 仍不挂 Docker socket，只获得受限 HTTP 路由。

这属于新增管理中继和网络边界变化，按 A2 要求**只提交设计，不实现**。

## 6. Broker 契约（不变）

- worker 只经每 workspace sidecar 访问 broker：
  `http://easel-sidecar-<workspace_id>:8899`。
- sidecar 仍加入 `easel-control` 与 `easel-ws-<workspace_id>`；worker 无
  默认路由，不发布端口。
- `--broker-upstream control:7861` 不变；sidecar 只转发精确 broker 路由。
- 不允许把 gateway 的 management 端口复用为任意 broker/出口代理，也不
  放宽 CONNECT 私网限制。

## 7. 批准门（当前为 blocked）

在以下项目获主审批准前，不写 gateway/协议/Compose 运行时代码：

1. WSL socket 0660 + control 补充组 1000 + `--allowed-uid 10001` 的权限模型。
2. 用固定 `docker exec` healthcheck 替代宿主 HTTP 探测。
3. 新增 gateway 的精确路由/查询表、control 签名断言、worker service 凭证、
   实例代次/恢复协议、请求上限、SSE 背压和失败清理，并补 A 凭证访问 B
   gateway 的反例及跨 workspace 拒绝测试。
4. 修改 `WorkerHandle`（保留 worker container_id，新增 gateway instance/
   generation）与 `WorkerLease`/stop 绑定，并补 A3/A4 的接口测试。

主审若选择“control 加入 worker 网络 + 控制网络地址绑定”的替代方案，需要
单独批准并补 worker→control 不可达的持续验证；本次契约不采用该方案。

## 8. 验证与清理

静态契约：

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest `
  tests/integration/multiuser/test_desktop_topology.py -q
```

真实反例（显式开关）：

```powershell
$env:EASEL_RUN_CONTAINER_TESTS = '1'
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest `
  tests/integration/multiuser/test_desktop_topology.py -q
```

实测结果、命令与临时资源清理见
[`docs/evidence/desktop-a2/report.md`](../../docs/evidence/desktop-a2/report.md)。
默认静态/纯逻辑为 **6 passed, 5 skipped**；显式真实测试为 **11 passed**。
所有容器/网络（包括临时客户端）使用随机 `easel-a2-*` 名称和
`easel.test.run-id=<run>` 标签；清理前复核标签，单项失败继续并汇总，未执行
全局 prune，未操作开发者已有资源。本次结束后不进入 A3。
