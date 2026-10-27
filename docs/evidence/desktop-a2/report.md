# Task A2 Desktop 拓扑实测与接入契约证据

日期：2026-10-27（Asia/Shanghai）

基线：`f1c4e5ef98ea756513071367919fe57fac7dbf17`（A1 验收通过）。
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
待主审批准后实现；A3 还要验证执行能力，不只 lifecycle。

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

## 6. Broker/路径契约

- worker broker 仍为 `http://easel-sidecar-<workspace_id>:8899`。
- sidecar 仍加入 `easel-control` 与 `easel-ws-<workspace_id>`；worker 无默认
  路由、无宿主端口、无 Docker socket。
- control broker upstream 仍为 `control:7861`；gateway 的 management 端口
  不能复用为任意 broker/出口代理。
- gateway 固定路径/方法白名单、SSE 流式透传、逐 workspace 认证和失败清理；
  详见 `deploy/desktop/topology.json` 与 README。

## 7. 红绿

### 红

在创建 `deploy/desktop/topology.json` 前运行契约测试：

```text
C:\Users\hyh\anaconda3\envs\Agent\python.exe -m pytest tests/integration/multiuser/test_desktop_topology.py -q
Fsssss                                                                   [100%]
1 failed, 5 skipped in 0.27s
FAILED ... test_desktop_topology_contract_is_explicit_and_secret_free
AssertionError: A2 必须先提交 deploy/desktop/topology.json
```

### 绿

```text
C:\Users\hyh\anaconda3\envs\Agent\python.exe -m pytest tests/integration/multiuser/test_desktop_topology.py -q
.                                                                        [100%]
1 passed in 0.13s
```

真实临时资源（显式开关）：

```text
$env:EASEL_RUN_CONTAINER_TESTS = '1'
C:\Users\hyh\anaconda3\envs\Agent\python.exe -m pytest tests/integration/multiuser/test_desktop_topology.py -q
......                                                                   [100%]
6 passed in 21.37s
```

覆盖：Windows/WSL HTTP 被阻断、独立 bridge 隔离、control 加入 worker 网络的
双向反例、Windows/WSL bind 可见性、WSL socket 挂载与 UID 10001 peer
credential、静态契约无秘密。

### 回归与静态检查

| 命令 | 结果 |
|---|---|
| `pytest tests/multiuser/test_desktop_deployment.py tests/test_document_paths.py -q` | **55 passed**（Desktop 45 + 文档 10） |
| `pytest tests/multiuser/test_provisioner.py tests/multiuser/test_worker_lifecycle.py -q` | **107 passed, 1 skipped** |
| `ruff check tests/integration/multiuser/test_desktop_topology.py` | 通过 |
| `python -m json.tool deploy/desktop/topology.json` | 通过 |
| `git diff --check` | 退出 0 |

## 8. 清理与边界

- 全部临时容器/网络使用随机 `easel-a2-*` 名称与 `easel.a2.test=<run>` 标签；
  测试和手工探针按精确名称/标签清理。
- 复查 `docker ps -a --filter label=easel.a2.test` 与
  `docker network ls --filter label=easel.a2.test` 为空。
- 未执行全局 `prune`，未停止/删除任何开发者容器、卷、网络或 ngrok 隧道。
- `.scratch` 中的临时探针脚本/日志不进入提交。

## 9. 未执行与剩余限制

- gateway 仅设计，运行时代码未实现；不进入 A3/A4。
- 没有重新构建或启动 A1 control 镜像；A1 的登录/浏览器证据不变。
- 没有执行真实 Harness、供应商调用、正式 Linux/XFS/防火墙/Chromium
  沙箱门槛。
- 本机 Docker Desktop 的宿主到容器 IP HTTP 路径按本次实测判定为不可用；
  若 Docker Desktop 升级改变行为，需要重新跑 A2 实测并更新契约。
- 设计批准前，不把 WSL/Windows 直连、host-network 或 control 加入 worker
  网络作为已通过路径。
