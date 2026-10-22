# 多人工作区部署手册（Task 8）

本目录实现单台 Linux 上每 workspace 一个隔离 worker 的运行时边界。控制面
只通过受限 Unix socket 请求容器操作；worker 不持有 Docker socket，也不获得
provisioner socket。**Task 8 仍未完成**：每空间 sidecar 的
worker→broker/受限出口允许路径已实现并有本机 Docker 接线证据，但目标 Linux
防火墙/IPv6、真实公网出口、浏览器沙箱、最终 Harness 与配额门槛仍未全部
通过。不得据此宣称多用户已发布或把剩余门槛顺延到 Task 10。

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
- 构建阶段职责：`base`（生命周期入口）→ `chromium`（固定浏览器）→
  `execution`（Web/ACP/技能代码与受管依赖）→ `runtime`（真实 Harness）。
- `execution` 复制 `web/`、`skills/`、公开 `agents/workspace/`，安装
  `deploy/multiuser/requirements-worker.txt` 中全部固定版本依赖，并实际
  执行 `smoke_worker.py --check-files`：检查文件/模块、worker 沙箱参数、
  worker Web 入口加载合法测试签名快照，并通过受管假 ACP 完成
  initialize/session/new/session/prompt 与共享规则/技能/路径/环境校验。
  该冒烟代替不了真实 DSH/供应商 Harness 验证。
- 依赖只在构建阶段安装；运行阶段不做在线 npm/pip 安装。
- Python/Node/Playwright/Chromium 版本在 Dockerfile 与 requirements 中固定；
  `/opt/easel`、`/opt/dsh`、`/opt/easel/managed` 构建后只读。
- `.dockerignore` 排除 `.env*`、outputs、profiles、浏览器状态、数据库、
  HOME、审计/任务文档、tests，以及补 COPY `skills/` 后可能带入的旧公众号
  YAML、token、Cookie、缓存；构建上下文不含开发者数据。
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

执行阶段（无真实 Harness）可单独构建并运行冒烟：

```bash
docker build -f deploy/multiuser/Dockerfile.worker --target execution \
  -t easel-worker-execution:test .
docker run --rm easel-worker-execution:test \
  python deploy/multiuser/smoke_worker.py --check-files
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
  --quota-mode project --quota-bytes 10737418240 \
  --sidecar-network easel-control \
  --sidecar-policy /etc/easel/egress-policy.json \
  --broker-upstream control:7861
```

`--sidecar-network` 必须是已存在、非 internal 的受控出站网络；三项 sidecar
参数必须同时提供，否则 provisioner 拒绝启动，避免把“未接线”误当允许路径。
`--broker-upstream` 指向 control 的 broker 监听，sidecar 只转发
`/internal/broker/<channel>/<relative>` 白名单路由，不暴露其他管理接口。

- socket 由 provisioner 创建，权限 `0660`；父目录 `0750`。
- 只接受建立连接时 `SO_PEERCRED` UID 在 `--allowed-uid` 中的调用；未配置时
  只允许 provisioner 自身 UID。
- 单行 JSON 协议，最大 8 KiB，10 秒请求超时：
  - `{"version":1,"action":"ensure","workspace_id":"<32hex>","config_version":N}`
  - `{"version":1,"action":"stop","workspace_id":"<32hex>","instance_id":"<64hex>"}`
  - `{"version":1,"action":"inspect","workspace_id":"<32hex>"}`
- `stop` 的 `instance_id` 是 Docker 容器不可复用 ID；provisioner 在 stop/rm
  前核对实际 ID，旧 stop 不会作用于同名新代次。Docker stop/rm 非零或
  inspect 失败返回 `container_stop_failed`、`container_remove_failed`、
  `docker_inspect_failed`，只有在确认原实例已消失后才回复 `ok=true`。
  只有明确指向本次容器/网络对象的 absent 响应按不存在处理；Docker context、
  endpoint、daemon、权限或 CLI 初始化错误一律失败关闭。inspect 退出 0
  但返回空数组、非数组或首项非对象时按 `inspect_invalid` /
  `network_invalid` 失败，不能当作对象已消失。仍被 Docker 标记 `Running`
  的 worker，其实际 `Config.Env` 与当前 sidecar broker/proxy 接线不符时
  返回 `worker_wiring_mismatch`，不会复用为已接线 ready，也不会因单次
  lifecycle 探针失败而删除该实例；只有明确 `exited`/`dead` 才重建。
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

### 每空间 sidecar

启用 sidecar 后，provisioner 为每个 workspace 创建
`easel-sidecar-<uuid>`，同样非 root、只读 rootfs、`cap-drop=ALL`、
`no-new-privileges`、1 CPU/512 MiB/128 PIDs，并只挂载管理员策略文件：

```text
docker run -d --name easel-sidecar-<uuid> \
  --network easel-ws-<uuid> \
  --mount type=bind,src=<policy>,dst=/etc/easel/egress-policy.json,readonly \
  <pinned-image> python deploy/multiuser/worker_sidecar.py \
    --broker-port 8899 --egress-port 8898 \
    --broker-upstream control:7861 --policy /etc/easel/egress-policy.json
docker network connect easel-control easel-sidecar-<uuid>
```

- worker 只在 `easel-ws-<uuid>`（internal）内，经固定 sidecar 名访问：
  - `http://easel-sidecar-<uuid>:8899`：仅白名单 broker 路由；
  - `http://easel-sidecar-<uuid>:8898`：Task 5 CONNECT 出口。
- sidecar 额外接入已存在的受控出站网络；worker 自身没有默认路由，不能
  直连 control、broker、其他 workspace 或公网。
- worker env 注入 `EASEL_BROKER_URL`、`EASEL_EGRESS_PROXY` 及
  `HTTP(S)_PROXY`，`NO_PROXY` 排除 sidecar 自身。环境代理只是客户端指向，
  真正的网络边界仍是 internal 网络 + sidecar。Task 9 的签名快照若包含
  `broker_url`，必须指向同一个 sidecar broker 名，不能回指 control 管理地址。
- Docker 的 `--env-file` 只在创建容器时读取。复用/接管前除静态安全字段外，
  还必须核对 inspect 中实际 `Config.Env` 的 broker/proxy/no-proxy；只更新
  宿主 env 文件不构成新接线。旧实例与当前批准接线不符时，只要 Docker 仍
  报告 `Running`（无论 lifecycle 探针超时、网络失败或返回 not ready），
  都返回 `worker_wiring_mismatch` 并保留实例，交控制面先 drain/stop，单次
  探针失败不构成删除授权。只有明确 `exited`/`dead` 的实例才按新接线重建，
  持久目录不删除。零出口模式兼容无 sidecar 的实例，但不得继承仍指向
  `easel-sidecar-*` 的旧代理。
- sidecar 创建/复用/停止均要求严格 inspect：固定镜像、权限、资源、唯一
  策略挂载、恰好两条批准网络；停止失败明确报错，不写 stopped。
- 重启对账/租约过期的 `inspect` 同时验收 worker 实际接线、workspace 网络、
  sidecar 归属/固定镜像/权限/资源/策略挂载/两条网络/健康，以及受控出站
  网络属性。任一缺失、不健康或被篡改时 `ready=false`，不接管、不缓存；
  `inspect` 只读，不会自动补建 sidecar。合法完整依赖可直接接管，不重复
  创建 worker/sidecar。

## 6. 生命周期与回收

状态：`absent → starting → ready → draining → stopping → stopped`，另有
`failed`。`stopping` 是 drain 的停止认领状态，防止两个管理者同时调用
`docker stop`。

- 控制面认领写入数据库租约（owner_id + generation + Docker 实例 ID），跨
  连接/跨管理进程单飞；旧代次不能覆盖新代次，也不能按当前 generation 给
  旧持有者续心跳。Docker/网络调用不在 SQLite 事务或线程绑定连接上等待。
  真实 control 的短数据库步骤经 lifespan 专用线程执行，RuntimeManager
  不直接跨线程使用 SQLite 连接。
- drain 的 mark/stop/写回都携带取得的 generation；等待后复核同一实例，
  stop 认领失败即放弃，迟到回复不能改写或停止新代次。
- 停止认领过期后，新代次必须先按原实例 ID 完成停止并 CAS 写回 stopped，
  之后才允许创建/复用实例；不会直接复用仍可能有旧 stop 在途的同一物理
  容器。
- `docker stop`/`rm` 非零、Docker inspect 不可用或无法确认原实例消失时，
  provisioner 明确返回错误；RuntimeManager 不写 stopped，状态保留
  `stopping`，等待后续按同实例幂等恢复。已确认不存在的目标按成功处理。
- 就绪期限 60 秒；失败/超时返回 `503` 明确错误码与 `Retry-After`，不路由到
  其他 workspace。
- 配置版本升级只在没有活跃 Run/请求时进行；旧版本句柄不会被当作新版本。
- 空闲阈值 900 秒，且必须无活跃 Run、无活跃请求；权限等待、发布中均不回收。
- `drain`：拒绝新任务，等待现有工作结束后停止容器；超时返回
  `worker_drain_timeout` 并保持 draining，不 delete 持久目录。
- 停止只按 Docker 实例 ID 执行 `docker stop` + `rm`；同名新容器不会成为
  旧 stop 目标，`<root>/<uuid>/{workspace,home,state}` 保留。
- 静态安全配置仍合法但已退出的实例（运行时 IP 为空）可被回收重建；未知或
  不安全实例继续拒绝，不会误接管。
- control 重启后通过 inspect 的受管标签、配置版本、不可复用实例 ID 与健康
  身份响应接管原实例；不重复执行 Task 6 已认领的任务。

资源、网络与浏览器边界详见 `egress-policy.md`。

## 7. 验证

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser -q
```

真实 control broker 经实际 sidecar socket 的授权装配（无需 Docker；供应商
传输为 fake）：

```bash
python -m pytest tests/multiuser/test_sidecar_broker_control.py -q
```

真实 Docker 拓扑：worker 容器→sidecar 容器→真实 control broker（供应商传输
仍为 fake；可选 `EASEL_WORKER_IMAGE` 指向已固定摘要的执行阶段镜像）：

```bash
EASEL_RUN_CONTAINER_TESTS=1 \
python -m pytest tests/integration/multiuser/test_control_broker_topology.py -q
```

该测试验证合法 token 经真实 broker 到达 fake provider，错误/跨 workspace/
run token 被拒绝，管理路径在 sidecar 入口 404；不证明真实供应商、真实公网
或目标宿主防火墙。

真实容器隔离（Linux Docker Engine；显式选择，失败就如实记录）：

```bash
EASEL_RUN_CONTAINER_TESTS=1 \
python -m pytest tests/integration/multiuser/test_container_isolation.py -q
```

集成覆盖实际 `Provisioner.ensure` 的 Unix socket 服务入口、Linux fd 绑定
chown 对 symlink 替换的拒绝、严格 inspect/网络验收、执行阶段镜像的文件/
签名快照/ACP 指令装配冒烟，以及 Playwright 最终驱动参数与硬化容器
Chromium 沙箱门槛。本机 Docker Desktop 上最终驱动参数正确，但硬化容器内
Chromium 返回 `No usable sandbox!`，因此**真实浏览器沙箱门槛未通过**。
镜像构建依赖 Debian/Playwright 下载，网络不可用时按未执行记录。

测试只创建随机 UUID 命名的临时容器/网络，清理前核对受管标签；不删除开发者
容器、workspace、outputs、profile 或浏览器状态。

## 8. 当前未关闭门槛

以下项目不得表述为全部通过；本机已执行证据与目标环境门槛分开记录：

- **XFS 项目配额**：Docker Desktop 使用 overlayfs，无 XFS prjquota；仅有
  `xfs_quota` 不可用时的失败关闭与注入测试，未验证真实限额生效。本机 WSL
  盘点有 XFS 内核支持但无 `mkfs.xfs`/`xfs_quota`，且不是已授权目标宿主。
- **Windows 宿主上的 control→socket 端到端**：Windows Python 无
  `socket.AF_UNIX`。本轮的 Linux 容器测试已覆盖 socket 权限、注入拒绝、
  `SO_PEERCRED` 未授权拒绝与 `UnixProvisionerClient` 解码；尚未在 Windows
  宿主启动完整 control 服务进程走同一链路。
- **宿主 provisioner → 容器 IP 健康探测**：集成测试经 `docker exec` 从容器
  内探测；Docker Desktop 的 VM 网络不提供宿主直达容器 IP 的等价路径。
- **最终 runtime 镜像**：缺少管理员已验收的 `DSH_COMMIT` 与
  `managed/acp-profile.json`，未构建 Harness/Chromium 最终镜像。`execution`
  阶段构建冒烟只证明文件/依赖/worker 参数/子进程启动，不证明真实 DSH
  可用。
- **worker 内真实 Chromium（未通过）**：代码已在 worker 模式同时
  `chromium_sandbox=True` 并移除自传禁用项，最终 Playwright 驱动参数已
  实测不含 `--no-sandbox`。但本机 Docker Desktop linux/amd64 的硬化容器
  （`10001:10001`、只读 rootfs、`cap-drop=ALL`、`no-new-privileges`、
  256 PIDs、4 GiB、`/tmp` noexec tmpfs）启动 Chromium 时明确失败：
  `No usable sandbox!`。目标内核需支持可用的用户命名空间/suid 沙箱；
  在此之前**浏览器隔离门槛不通过**，不得用 `--no-sandbox`、额外
  capabilities 或 privileged 绕过。
- **技能 Chromium 参数**：worker 模式已统一经
  `runtime_paths.chromium_launch_args()` 移除 `--no-sandbox` /
  `--disable-setuid-sandbox`，并显式传入 `chromium_sandbox=True`；local
  保留旧兼容参数。测试捕获真实 launch kwargs，并验证沙箱缺失时异常向上
  传播。
- **宿主防火墙/DOCKER-USER 规则**：仓库只定义矩阵，未自动安装或验证规则。
- **broker/受限公网出口到 worker 的允许路径**：代码与 sidecar 接线已完成，
  并用本机 Docker 真实网络验证 broker 白名单路由、批准出口与拒绝路径。
  另一组装配测试启动真实 `create_app`/Auth/Config/Broker 与独立 sidecar
  进程，经实际 sidecar socket 验证合法 token 到达 broker、错误/跨
  workspace/run token 被真实 broker 拒绝，管理路径在 sidecar 入口 404。
  本机 Docker 拓扑测试已实际启动 worker 容器、sidecar 容器和真实 control
  broker（供应商传输为 fake），验证同一授权矩阵；这不等于目标 Linux 宿主
  防火墙/IPv6、真实公网出口、真实供应商或 Task 5 live 出口已通过。
- **目标 Linux 宿主网络与配额**：未提供已授权目标连接信息；本机 WSL 仅用于
  只读环境盘点，不作为目标宿主证据。宿主防火墙/DOCKER-USER、IPv4/IPv6
  矩阵、XFS 真实限额与宿主 provisioner→worker 健康链仍需目标环境实测。
