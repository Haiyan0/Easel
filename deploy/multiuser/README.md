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
  --broker-upstream control:7861 \
  --gateway-image sha256:<gateway-image-digest> \
  --gateway-control-network easel-control \
  --gateway-public-key /etc/easel/control-public.key
```

`--gateway-image`、`--gateway-control-network`、`--gateway-public-key`
必须同时提供；全部省略时保留旧版 v1 直连 worker 部署。任一缺失/非法时
provisioner 拒绝启动。gateway 启用后，v1 ensure/stop/inspect 失败关闭，
不会静默回退到直接 worker URL；v2 才返回 gateway 元数据。

`--sidecar-network` 必须是已存在、非 internal 的受控出站网络；三项 sidecar
参数必须同时提供，否则 provisioner 拒绝启动，避免把“未接线”误当允许路径。
`--broker-upstream` 指向 control 的 broker 监听，sidecar 只转发
`/internal/broker/<channel>/<relative>` 白名单路由，不暴露其他管理接口。

- socket 由 provisioner 创建，权限 `0660`；父目录 `0750`。
- 只接受建立连接时 `SO_PEERCRED` UID 在 `--allowed-uid` 中的调用；未配置时
  只允许 provisioner 自身 UID。
- 单行 JSON 协议，最大 8 KiB，10 秒请求超时：
  - v1 旧部署：`{"version":1,"action":"ensure","workspace_id":"<32hex>","config_version":N}`、`stop`、`inspect`。
  - v2 gateway：`ensure` 携带 attempt_id 并返回候选 gateway 字段但不激活；
    `activate` 精确携带 workspace/gateway_instance_id/worker_container_id/
    boot_generation/config_version；`stop` 同时携带 worker `instance_id` 与
    `gateway_instance_id`；`abort` 必须携带当前 attempt_id 以及本轮
    `worker_created`/`gateway_created`。gateway 启用时 v1 被拒绝。
  - `{"version":2,"action":"ensure","workspace_id":"<32hex>","config_version":N,"attempt_id":"<32hex>"}`
  - `{"version":2,"action":"activate","workspace_id":"<32hex>","config_version":N,"worker_container_id":"<64hex>","gateway_instance_id":"<64hex>","boot_generation":"<32hex>"}`
  - `{"version":2,"action":"stop","workspace_id":"<32hex>","instance_id":"<64hex>","gateway_instance_id":"<64hex>"}`
  - `{"version":2,"action":"inspect","workspace_id":"<32hex>"}`
  - `{"version":2,"action":"abort","workspace_id":"<32hex>","worker_container_id":"<64hex>","gateway_instance_id":"<64hex>","attempt_id":"<32hex>","worker_created":true,"gateway_created":true}`
- `stop` 的 `instance_id` 是 Docker 容器不可复用 ID；gateway 启用时先按
  `gateway_instance_id` 精确停止 gateway，失败不继续删除 worker。旧 stop
  不会作用于同名新代次。Docker stop/rm 非零或
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
  --provisioner-socket /run/easel/provisioner.sock   --gateway-required
```

`--provisioner-socket` 指向的 socket 不存在时 control 直接失败退出，不进入
无隔离的降级模式。启用 gateway 的部署必须加 `--gateway-required`，否则
control 使用 v1 旧协议并在 provisioner 侧失败；该 flag 只接通生命周期
CAS/激活，不启用公开聊天入口。compose 参考拓扑见 `compose.yaml`；实际部署前按
`egress-policy.md` 配置宿主防火墙，并验证控制面发布端口仅对本机/反向代理
可见。

## 5. 容器实际 argv（由 provisioner 构造）

```text
docker run -d --name easel-worker-<uuid> \
  --cidfile <root>/.credentials/.<uuid>.cid.<random> \
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
  --mount type=bind,src=<admin-key>,dst=/keys/worker-public.key,readonly \
  --env EASEL_WORKER_PUBLIC_KEY_FILE=/keys/worker-public.key \
  --env-file <root>/.credentials/<uuid>.env \
  --restart=no --stop-timeout=30 \
  --log-driver=local --log-opt max-size=10m --log-opt max-file=3 \
  <pinned-image> python -m easel.worker.server serve --host 0.0.0.0 --port 7860
```

- 无 `--privileged`、无 `--publish`、无 Docker/provisioner socket 挂载。
- 服务 token 只在 0600 env 文件、容器实际 Env 与数据库中，不进入 argv、
  inspect 白名单输出或日志。provisioner 只为本轮新建实例生成 token；接管
  既有实例时只读宿主 token，并与容器实际 `Config.Env` 做常量时间比较。
  缺凭据、错误旧 token、大小写不符或重复 Env 键均失败关闭，不自动轮换、
  删除或重建活跃实例。
- `--cidfile` 只用于取得本轮 `docker run` 的不可复用容器 ID；健康循环、
  失败清理和 exited 重建都只使用该 ID，不按同名容器反推归属。CLI 超时/
  取消不等于 daemon 未创建：provisioner 会等待 in-thread CLI 真正结束，
  再读取 cidfile 并按 ID 清理。worker 删除未确认、inspect/daemon 错误或
  sidecar 停止失败时保留 cidfile（不含 token）并返回
  `worker_start_cleanup_incomplete`；不先把仍可能被 worker 使用的 sidecar
  拆掉。取消路径的清理任务独立运行，外层等待者不能把它取消。
- `docker inspect` 经 provisioner 输出前只保留白名单字段；Env、宿主路径和
  未知标签被丢弃。`Running + Status=running + 健康` 才可作为 ready；paused、
  restarting、缺状态、ready=false 或 exec 探针失败都只返回未就绪，不删除
  仍有停止授权争议的实例。

### 每 workspace gateway（G2）

启用后，provisioner 先为控制网络分配固定 IPv4，再构造：

```text
docker create --name easel-gateway-<uuid> \
  --label easel.role=gateway --label easel.workspace-id=<uuid> \
  --label easel.config-version=<N> \
  --label easel.worker-container-id=<worker-64hex> \
  --user 10001:10001 --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges:true --cpus=1 --memory=256m \
  --memory-swap=256m --pids-limit=128 \
  --tmpfs /run/easel:rw,noexec,nosuid,nodev,size=1m,mode=0700,uid=10001,gid=10001 \
  --network easel-control --ip <fixed-control-ip> \
  --mount type=bind,src=<control-public-key>,dst=/keys/control-public.key,readonly \
  --restart=no --stop-timeout=15 \
  <pinned-gateway-image> \
  python -m easel.gateway.server serve --bind <fixed-control-ip> \
    --port 8897 --public-key /keys/control-public.key \
    --control-socket /run/easel/gateway-control.sock
```

- 无宿主端口、无 Docker socket、无 worker workspace/控制数据库挂载。
- start 后 inspect 必须恰好一个 control IPv4 等于预分配地址；随后经固定
  `docker exec -i ... configure`（有界 stdin JSON）写入实际 worker 目标，
  再 connect 唯一 `easel-ws-<uuid>`；最终 inspect 必须恰好两张网。
- gateway 进程每次启动生成新 boot generation；configure 不激活，activate
  必须匹配 workspace/实例/boot/worker/config_version。RuntimeManager 在
  owner/generation CAS 中先保存候选，再 activate，成功后才写 ready。
- status 从实际 inspect + 容器内 worker_ready/worker_running、
  configured/active/available 计算；关闭失败隔离时 available=false，不通过
  active 布尔伪装可用。
- 创建使用专属 cidfile：`docker create` 超时/取消先等 CLI 终结，再按
  cidfile 中不可复用 ID 清理；start/connect/configure/activate 取消也等待
  底层 CLI。清理失败保留 cidfile 并返回 gateway_create_cleanup_incomplete。
- refresh 的 starting 过期保留 worker/候选/attempt，按实际实例重新握手；
  临时故障恢复 ready/unavailable，不按冷启动中断在途 Run。abort 先取得
  Store 的 starting owner/generation/attempt 停止承诺，旧 owner 的迟到
  abort 不得删除新 owner 已采用的实例。
- Store v13 追加 `attempt_id` 与 `cleanup_purpose/cleanup_scope/
  cleanup_worker_created/cleanup_gateway_created`；refresh 以已有 worker
  `container_id` 区分，不把进程崩溃当执行终止。worker_ready/worker_running
  与 gateway available 分开报告。
- abort 的 purpose/scope/attempt/target 持久化；gateway-only 启动 abort
  只删除新 gateway，失败恢复后保留既有 worker/凭证/历史/Run 回到
  ready/unavailable，不写假 stopped。worker/worker_gateway 按各自范围；
  只有业务 drain/旧记录默认完整停止。
- provisioner 在 workspace 根 `.gateway/attempt-<workspace>.json` 持久化
  当前 attempt 的不可复用目标、created 标记、scope 与 status
  （pending/completed）；物理 abort 成功后保留 completed tombstone，同一
  attempt/ID/scope 的 control 未提交重试幂等成功；新 attempt 写新记录取代
  旧记录。进程重启后 abort 必须匹配该记录，内存丢失不得退回完整 stop。
- completed 记录写入失败时保持 pending 并返回 `gateway_abort_record_failed`；
  重试按同 ID/范围做幂等终态核验后再提交 completed，不假报已收口。

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
- worker 进程的 SIGTERM/SIGINT 由 Uvicorn `Server.handle_exit` 子类接入：
  首个信号立即进入 draining（health ready=false、新 Run 返回 503
  `worker_draining`），并在监听 socket 仍可服务时给在途 Run 有限宽限
  （`EASEL_WORKER_DRAIN_TIMEOUT`，默认 15 秒）；宽限内完成的历史保留，
  超时取消并写 cancelled/interrupted，绝不伪标 done。第二个信号跳过剩余
  宽限强制取消；进程和 ACP 子进程清理均有界。
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

测试只创建随机 UUID 命名的临时容器/网络/卷，全部带本次 run 标签；清理前
核对归属，不删除开发者容器、workspace、outputs、profile 或浏览器状态。
Docker 拓扑测试使用三个独立、标签化、UID 10001 所有的受限卷：control 只
挂控制脚本/key/数据库，sidecar 只挂策略，worker 只挂客户端脚本；sidecar/
worker 不挂载控制卷。一次性初始化助手只处理这三个卷，目录 0750、脚本/
策略/key 0400、SQLite 0600，不修改宿主临时目录或使用 0777/0666。

## 8. 当前未关闭门槛

以下项目不得表述为全部通过；本机已执行证据与目标环境门槛分开记录：

- **XFS 项目配额**：Docker Desktop 使用 overlayfs，无 XFS prjquota；仅有
  `xfs_quota` 不可用时的失败关闭与注入测试，未验证真实限额生效。本机 WSL
  盘点有 XFS 内核支持但无 `mkfs.xfs`/`xfs_quota`，且不是已授权目标宿主。
- **Windows 宿主上的 control→socket 端到端**：Windows Python 无
  `socket.AF_UNIX`。本轮的 Linux 容器测试已覆盖 socket 权限、注入拒绝、
  `SO_PEERCRED` 未授权拒绝与 `UnixProvisionerClient` 解码；尚未在 Windows
  宿主启动完整 control 服务进程走同一链路。
- **宿主 provisioner → 容器 IP 健康探测**：A3 已改为对不可复用 container_id
  执行固定 `docker exec ... easel.worker.server healthcheck`，不再使用宿主
  HTTP；真实 Docker 验收通过。宿主到容器 IP 的 HTTP 路径仍不可用。
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
  矩阵、XFS 真实限额、宿主 provisioner→worker 健康链及原生 Linux 上
  UID 10001 测试卷权限仍需目标环境实测。

## 9. 会话目录与 worker 快照

control 已装配本人会话目录：

```text
GET    /api/sessions?limit=50&offset=0&include_archived=false
POST   /api/sessions                 {"title":"新对话"}
PATCH  /api/sessions/{id}            {"title":...} 或 {"archived":true|false}
DELETE /api/sessions/{id}
GET    /api/sessions/{id}/messages?limit=100&offset=0
```

- 所有调用使用现有 Cookie/Origin/CSRF 中间件；目录和 Run 按当前身份回到
  control 数据库重查归属，外空间/未知/已删除会话统一 404。
- 标题更新、归档和软删除均带 actor；活跃 Run 阻止归档/删除，已存在 Run
  的外键与原生历史不被删除。
- `GET /api/sessions/{id}/messages` 在 Task 10 relay/transport 装配前明确
  返回 `capability_unavailable`，不会读取服务器任意路径或伪造空历史。
- worker 的 `WorkspaceSessionStore` 位于本人 `state_root/sessions/`，按
  workspace/session 哈希分目录；绑定记录 workspace/runtime/native_id/
  agent_version，消息以加锁 JSONL 原子追加。旧无归属绑定/快照拒绝自动
  认领；local 模式继续使用原有 `_sessions` 格式。

## 10. 备份、恢复与显式迁移

完整前置、维护窗口、恢复演练和供应商矩阵见
[docs/multiuser-deployment.zh-CN.md](../../docs/multiuser-deployment.zh-CN.md)。

```bash
# 控制库一致性快照 + master key + workspace 持久区；活动 worker 先 drain
python -m easel admin backup \
  --control-dir /srv/easel/control \
  --destination /srv/easel/backups/2026-10-26 \
  --master-key-file /run/secrets/easel-master.key \
  --workspace-root /srv/easel/workspaces \
  --image-digest sha256:<64hex> \
  --harness-revision <revision> \
  --provisioner-socket /srv/easel/run/provisioner.sock

# 恢复到全新目录；非空目标、缺 key 或 key 不匹配都失败关闭
python -m easel admin restore \
  --source /srv/easel/backups/2026-10-26 \
  --destination /srv/easel-restore/2026-10-26

# 显式本机迁移：先 dry-run，再实际执行；源保持只读
python -m easel admin migrate-local \
  --source /home/operator/easel/outputs \
  --workspace <32hex> \
  --workspace-root /srv/easel/workspaces \
  --dry-run
```

备份清单记录 schema、镜像/Harness/配置版本和每个文件的 SHA-256；不含邀请、
登录或模型 Key 明文，排除 `.credentials/`，并清除快照中的 worker 服务
token。恢复不会覆盖运行中的 control 目录，也不会在 key 缺失/不匹配时重置
秘密。本机迁移只写入显式目标 workspace；
`cwd` 不兼容或缺失的原生绑定只作为静态历史报告为不可续接，不替换
`native_id`。上述能力不改变 Task 8 的真实防火墙/IPv6/配额/最终 Harness/
浏览器沙箱门槛。

## 11. 双用户验收与正式发布门槛

本机开发证据与正式部署证据必须分开记录；本机测试不能作为发布证明。

### 11.1 本机双用户证据

```powershell
python -m pytest tests/multiuser/test_two_users.py -q
python -m pytest tests/multiuser/test_release_checker.py -q
```

- `test_two_users.py` 启动真实 control app、两个独立 worker 子进程和临时
  假 ACP Harness；验证 A/B 一次性邀请登录、同名会话、HTTP/SSE、连续追问
  续接、worker outputs 的 token/文件隔离，以及控制库 + 持久区在新目录的
  备份恢复。它不连接真实 Docker、目标 Linux、真实 DSH 或供应商。
- `test_release_checker.py` 只验证检查器在缺前提、skip/xfail、空门槛、
  未标记测试、真实子进程输出捕获和未验证证据阻断时失败，不访问真实环境。
  `test_live_assertions.py` 用 MockTransport 证明 live 测试拒绝
  failed/interrupted/cancelled 及部分输出。
- 2026-10-26 本机（Windows，父基线 `ced2a64`）结果：
  `test_two_users.py` 4 passed、`test_release_checker.py` 24 passed、
  `test_live_assertions.py` 4 passed；完整 `tests/multiuser` 为
  `1004 passed, 4 skipped`；真实 Chromium 的
  `tests/integration/multiuser/test_browser_profiles_real.py` 为
  `12 passed`（含缓存值负向敏感性）；前端 Node 测试 `118 passed`，
  生产构建通过。另在本机
  Docker Desktop 用已有 Task 8 执行镜像运行
  `smoke_worker.py --check-files`，输出 `WORKER_SMOKE_OK`；这不是从头
  构建、容器隔离/网络/配额或目标 Linux 证据。Windows 上 POSIX 执行位等
  既有跳过不冒充通过，本机 Chromium 也不等于目标 Linux worker 沙箱通过。

### 11.2 正式门槛（缺前提即失败）

```bash
python scripts/check_multiuser_release.py
python -m pytest \
  tests/integration/multiuser/test_two_users_live.py \
  tests/integration/multiuser/test_browser_live.py \
  tests/integration/multiuser/test_restore_live.py \
  --multiuser-live -m multiuser_integration -q
```

正式检查要求：

- Linux 宿主；Windows/Docker Desktop 结果直接 `platform_not_linux` 失败；
- Docker Engine 可用；检测到 rootless 时当前检查器直接失败，除非后续
  单独完成项目配额、UID 映射、`SO_PEERCRED` 与 bind mount 实测并扩展
  自动门槛；
- `EASEL_TEST_WORKSPACE_ROOT` 所在文件系统为 XFS 且启用
  `prjquota`/`pquota`，并存在 `xfs_quota`；
- `EASEL_MASTER_KEY_FILE` 为 32 字节且权限不含 group/world；
- `EASEL_TEST_CONTROL_URL`/`EASEL_TEST_PREVIEW_URL` 为 HTTPS；
- `EASEL_TEST_WORKER_IMAGE` 为 `sha256:<64hex>` 固定摘要；
- `EASEL_TEST_INVITE_A`/`EASEL_TEST_INVITE_B` 为未兑换的一次性邀请；
- `EASEL_TEST_BACKUP_SOURCE` 为已有备份，`EASEL_TEST_RESTORE_DEST` 为新/
  空目录；并安装 Playwright/Chromium；
- `EASEL_TEST_EGRESS_PROXY` 为受控出口代理；检查器会设置
  `EASEL_EGRESS_LIVE=1` 并实际运行 `test_egress.py`（公网允许、
  IPv6 回环/元数据/DNS 重绑定拒绝）。

所有选中的安全/发布门槛都必须真实执行；任一 skip/xfail/xpass、空集合
或漏选均失败，没有文件级宽松开关。统计只取 pytest 最终汇总行，警告或
用例正文中的数字不参与；找不到可信最终统计时失败关闭。检查器还区分：

- **已验证证据**：真实出口测试通过；
- **阻断证据**：XFS 挂载/工具存在不等于 10 GiB 配额建立与耗尽已实测；
  宿主 DOCKER-USER/IPv4/IPv6 矩阵未自动验证；容器测试只构建 base/
  execution 假 Harness，不等于传入最终 runtime 镜像及真实 DSH/profile
  已验收。因此后三项保持 `blocked`，`release_status` 只能为 `blocked`
  （退出 1），不能出现 `passed`。

缺任一变量、门槛失败或证据未验证，检查器均退出 1。2026-10-26 在 Windows
本机实际运行检查器输出：

```text
release_status=failed
[fail] platform platform_not_linux
gates=not_run
evidence=not_run
```

这是本机证据，不是正式通过。正式 Linux/Docker/XFS/真实 DSH/浏览器沙箱/
供应商门槛未执行；control 的 workspace-content 路由（如 `/api/outputs`）
尚未装配，继续返回 503。live 测试把该 503 视为发布门槛失败，不能以本机
worker 产物测试替代。
