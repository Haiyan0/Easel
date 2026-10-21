# 多人工作区部署手册（Task 8）

本目录实现单台 Linux 上每 workspace 一个隔离 worker 的运行时边界。控制面
只通过受限 Unix socket 请求容器操作；worker 不持有 Docker socket，也不获得
provisioner socket。Task 8 不包含完整 relay/任务分发（Task 10），不要据此
宣称多用户已正式发布。

## 组件与信任边界

| 组件 | 运行位置 | 能力 |
|---|---|---|
| control | 容器/宿主进程 | 数据库、HTTP、经 Unix socket 请求 provisioner；无 Docker socket |
| provisioner | **宿主机进程** | 唯一持有宿主 Docker socket 的进程；只创建/停止受管容器与内部网络 |
| worker | 每 workspace 一容器 | 只读 `/opt/easel`、持久 `/workspace`、`/home/easel`；无 Docker/provisioner socket |
| egress（可选） | control 网络 | Task 5 CONNECT 代理，只服务 control 侧 broker |

不要把 `/var/run/docker.sock` 挂进 control、egress 或 worker。不要把
provisioner socket 挂进 worker。

## 1. 固定镜像与 Harness

构建最终镜像前，管理员必须提供已验收的 Harness commit 和真实 ACP profile：

```bash
docker build -f deploy/multiuser/Dockerfile.worker \
  --build-arg DSH_COMMIT=<已验收 commit> \
  -t easel-worker:<tag> .
```

- `DSH_COMMIT` 为空、clone 后 `rev-parse` 不匹配、或缺少
  `deploy/multiuser/managed/acp-profile.json` 时构建失败。
- 依赖只在构建阶段安装；运行阶段不做在线 npm/pip 安装。
- Python/Node/Playwright/Chromium 版本在 Dockerfile 中固定；`/opt/easel`、
  `/opt/dsh`、`/opt/easel/managed` 构建后只读。
- `.dockerignore` 排除 `.env*`、outputs、profiles、浏览器状态、数据库、
  HOME、审计/任务文档与 tests；构建上下文不含开发者数据。
- 禁止 COPY 任何 master key、服务 token、模型 Key、Cookie。

构建后取摘要并部署，禁止用可变 tag 启动：

```bash
docker image inspect --format '{{.Id}}' easel-worker:<tag>
# 输出 sha256:<64hex>，作为 --image 参数
```

生命周期入口的轻量 base 目标（无浏览器/Harness）：

```bash
docker build -f deploy/multiuser/Dockerfile.worker --target base \
  -t easel-worker-test:base .
```

## 2. 持久区与配额

workspace 持久根是固定目录，例如 `/srv/easel/workspaces`。每个 workspace
得到 `<root>/<uuid>/workspace`（内含 `state/`）与 `<root>/<uuid>/home`；
容器只挂载这两个目录，`EASEL_STATE_ROOT=/workspace/state`。停止/重建容器
从不删除这些目录。

默认要求 XFS + `prjquota`/`pquota`：

```bash
# 文件系统必须实际启用项目配额
findmnt -no FSTYPE,OPTIONS /srv/easel/workspaces
# 期望 xfs ... prjquota 或 pquota
```

provisioner 用 `xfs_quota` 建立项目限额（默认 10 GiB）并读取 report 验证；
工具缺失、文件系统不支持、项目初始化/限额/验证任一失败都会拒绝启动 worker。
`docker --storage-opt` 对 bind mount 无效，不被视为配额。

仅测试可显式关闭配额：

```bash
--quota-mode none --allow-unquotaed
```

该参数会记录警告，真实多人部署不得使用。

## 3. 启动 provisioner（宿主机）

以 root 或可操作 Docker、可 chown 持久目录的专用账号运行：

```bash
python deploy/multiuser/provisioner.py \
  --socket /run/easel/provisioner.sock \
  --workspace-root /srv/easel/workspaces \
  --image sha256:<64hex> \
  --allowed-uid <control UID> \
  --quota-mode project --quota-bytes 10737418240
```

- socket 由 provisioner 创建，权限 `0660`；父目录 `0750`。
- 只接受建立连接时 `SO_PEERCRED` UID 在 `--allowed-uid` 中的调用；未配置时
  只允许 provisioner 自身 UID。
- 单行 JSON 协议，最大 8 KiB，10 秒请求超时：
  - `{"version":1,"action":"ensure","workspace_id":"<32hex>","config_version":N}`
  - `{"version":1,"action":"stop","workspace_id":"<32hex>"}`
  - `{"version":1,"action":"inspect","workspace_id":"<32hex>"}`
- 未知字段、非法 UUID、非法版本、未固定镜像在调用 Docker 前拒绝。
- 成功响应只含 `ok` 与 `handle`/`report`；错误只返回稳定错误码。

## 4. 启动 control

```bash
python -m easel serve-control \
  --control-dir /srv/easel/control \
  --public-origin https://easel.example.com \
  --master-key-file /run/secrets/easel-master.key \
  --egress-proxy http://127.0.0.1:8899 \
  --egress-policy /etc/easel/egress-policy.json \
  --provisioner-socket /run/easel/provisioner.sock
```

`--provisioner-socket` 指向的 socket 不存在时 control 直接失败退出，不进入
无隔离的降级模式。compose 参考拓扑见 `compose.yaml`；实际部署前按
`egress-policy.md` 配置宿主防火墙，并验证控制面发布端口仅对本机/反向代理
可见。

## 5. 容器实际 argv（由 provisioner 构造）

```text
docker run -d --name easel-worker-<uuid> \
  --label easel.managed=true \
  --label easel.workspace-id=<uuid> \
  --label easel.config-version=<N> \
  --user 10001:10001 --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges:true \
  --cpus=2 --memory=4g --memory-swap=4g --pids-limit=256 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=1g \
  --network easel-ws-<uuid> \
  --mount type=bind,src=<root>/<uuid>/workspace,dst=/workspace \
  --mount type=bind,src=<root>/<uuid>/home,dst=/home/easel \
  --env-file <root>/.credentials/<uuid>.env \
  --restart=no --stop-timeout=30 \
  --log-driver=local --log-opt max-size=10m --log-opt max-file=3 \
  <pinned-image> python -m easel.worker.lifecycle serve --host 0.0.0.0 --port 7860
```

- 无 `--privileged`、无 `--publish`、无 Docker/provisioner socket 挂载。
- 服务 token 只在 0600 env 文件与数据库中，不进入 argv。
- `docker inspect` 经 provisioner 输出前只保留白名单字段；Env、宿主路径和
  未知标签被丢弃。

## 6. 生命周期与回收

状态：`absent → starting → ready → draining → stopped`，另有 `failed`。

- 控制面认领写入数据库租约（owner_id + generation），跨连接/跨管理进程
  单飞；旧代次不能覆盖新代次。Docker/网络调用不在 SQLite 事务或线程绑定
  连接上等待。
- 就绪期限 60 秒；失败/超时返回 `503` 明确错误码与 `Retry-After`，不路由到
  其他 workspace。
- 配置版本升级只在没有活跃 Run/请求时进行；旧版本句柄不会被当作新版本。
- 空闲阈值 900 秒，且必须无活跃 Run、无活跃请求；权限等待、发布中均不回收。
- `drain`：拒绝新任务，等待现有工作结束后停止容器；超时返回
  `worker_drain_timeout` 并保持 draining，不 delete 持久目录。
- 停止只 `docker stop` + `rm`；`<root>/<uuid>/{workspace,home,state}` 保留。
- control 重启后通过 inspect 的受管标签、容器名、workspace/配置版本、健康
  身份响应接管原实例；不重复执行 Task 6 已认领的任务。

资源、网络与浏览器边界详见 `egress-policy.md`。

## 7. 验证

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser -q
```

真实容器隔离（Linux Docker Engine；显式选择，失败就如实记录）：

```bash
EASEL_RUN_CONTAINER_TESTS=1 \
python -m pytest tests/integration/multiuser/test_container_isolation.py -q
```

测试只创建随机 UUID 命名的临时容器/网络，清理前核对受管标签；不删除开发者
容器、workspace、outputs、profile 或浏览器状态。

## 8. 当前未执行门槛

以下项目在本轮 Windows + Docker Desktop 环境无法真实验证，不得表述为通过：

- **XFS 项目配额**：Docker Desktop 使用 overlayfs，无 XFS prjquota；仅有
  `xfs_quota` 不可用时的失败关闭与注入测试，未验证真实限额生效。
- **Windows 宿主上的 control→socket 端到端**：Windows Python 无
  `socket.AF_UNIX`。本轮的 Linux 容器测试已覆盖 socket 权限、注入拒绝、
  `SO_PEERCRED` 未授权拒绝与 `UnixProvisionerClient` 解码；尚未在 Windows
  宿主启动完整 control 服务进程走同一链路。
- **宿主 provisioner → 容器 IP 健康探测**：集成测试经 `docker exec` 从容器
  内探测；Docker Desktop 的 VM 网络不提供宿主直达容器 IP 的等价路径。
- **最终 runtime 镜像**：缺少管理员已验收的 `DSH_COMMIT` 与
  `managed/acp-profile.json`，未构建 Harness/Chromium 最终镜像。
- **worker 内真实 Chromium**：未在只读 rootfs + cap-drop + no-new-privileges
  的容器内启动浏览器；沙箱不可用即门槛不通过，不允许用 `--no-sandbox`
  或额外 capabilities 绕过。
- **宿主防火墙/DOCKER-USER 规则**：仓库只定义矩阵，未自动安装或验证规则。
- **broker/受限公网出口到 worker 的允许路径**：尚未接线（Task 10）；当前
  worker 全拒绝出站。
