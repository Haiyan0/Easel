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

## 4. 健康探测契约（待批准）

WSL 宿主和 Windows 宿主到 worker 容器 IP 的 HTTP 健康请求均实测超时；TCP
连接成功不能证明 HTTP、身份或就绪。当前 provisioner 的 `_probe` 使用
宿主 HTTP，因此真实 `ensure` 在临时资源上返回 `worker_not_ready`（容器创建后
按失败清理）。

提案（**未实现**）：

- 使用固定命令从 provisioner 侧执行：
  `docker exec <worker> python -m easel.worker.lifecycle healthcheck --port 7860`
- 只接受固定 argv，不接受调用方拼接命令；该命令继续核对 workspace/
  config_version/container_id/ready，不得只看退出码或任意 200。
- A3 执行能力接入后，healthcheck 还要验证真实 Run 路由/身份，而非只验证
  lifecycle-only 入口。
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
  `--internal`。
- 监听：management listener **只绑定 control 网络地址**，不监听 worker
  网络地址；worker 无路由进入 control 网络，不能访问 management listener。
- 不发布宿主端口、不挂 Docker socket、不挂 worker workspace/home、不读
  控制数据库；只读 rootfs、UID 10001、cap-drop ALL、no-new-privileges、
  固定资源上限。
- 请求方向：control → gateway（control 网络）→ worker（worker 网络）。
  不接受 worker 主动发起的管理请求。
- 认证：每 workspace 独立的 control→gateway 凭证；gateway 固定自身
  workspace，拒绝客户端指定目标。gateway 只在认证通过后，向该 workspace
  的固定 worker 转发内部身份头；凭证不写日志/磁盘。
- 路径/方法：固定白名单（见 topology.json），只转发 worker 的 lifecycle/
  run/事件/问题接口；不允许任意 URL、路径穿越、重定向或公开 `/api`。
- SSE：透传 `text/event-stream`，逐块刷新；control 断开时关闭上游；
  不重试、不伪造完成事件。
- 失败清理：gateway 与 worker 同代次创建/停止；worker 启动失败、gateway
  启动失败、worker 停止都清理 gateway；没有 worker 时不得留下可达的
  management 入口。
- `WorkerHandle.upstream` 改为 gateway 的 control 网络地址；不再把 worker
  容器 IP 暴露给 control。

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
3. 新增 gateway 及其生命周期、认证、路径白名单、SSE 和失败清理。
4. 修改 `WorkerHandle.upstream` 并补 A3/A4 的接口测试。

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
所有容器/网络使用随机 `easel-a2-*` 名称和 `easel.a2.test=<run>` 标签；清理前
复核标签，未执行全局 prune，未操作开发者已有资源。本次结束后不进入 A3。
