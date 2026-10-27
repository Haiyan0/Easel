# Task A2：Desktop 完整拓扑契约与可行性报告

状态：**A2 拓扑契约保留；A3 执行 worker 已实现；G1 独立 gateway 核心已实现并通过单元/真实 TCP 测试；G2 生命周期接线与 A4 仍未实现。**

基线：`f1c4e5ef98ea756513071367919fe57fac7dbf17`（A1 验收通过）。
本目录只增加 A2 契约、反例和证据；没有修改认证核心、A1 Compose
拓扑或现有 provisioner/worker 行为。

## 1. 结论先行

| 能力 | 实测结论 | A2 处理 |
|---|---|---|
| WSL2 宿主 provisioner 调用 Docker | 可用；Ubuntu-26.04 内 Docker CLI 使用 `/var/run/docker.sock` 指向同一 Docker Desktop Engine | 保留 |
| control 容器使用现有 Unix socket 协议 | **可用**：WSL `/tmp` socket 挂载进容器，UID 10001 + 补充组 1000，`SO_PEERCRED` 正确报告 10001 | 固定契约 |
| WSL/Windows 宿主到 worker 容器 IP 的 HTTP | **不可用**：TCP 可能显示已连接，但 HTTP 请求超时；不能作为健康探测 | A3 已改为固定 `docker exec` healthcheck |
| 当前 control 网络到 worker 容器 | **不可达**：独立 bridge 到内部 worker 网络超时 | 不能直接沿用 |
| 直接把 control 加入 worker 网络 | control→worker 可达，但 worker→control 也可达 | 拒绝，作为反例记录 |
| 新增每 workspace 管理中继 | 推荐方案；避免 control 加入每个 worker 网络，并保持 control 无 Docker socket | G1 独立核心已实现；容器生命周期与 control 接线待 G2 |
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

## 4. 健康探测契约（A3 已实现）

WSL 宿主和 Windows 宿主到 worker 容器 IP 的 HTTP 健康请求均实测超时；TCP
连接成功不能证明 HTTP、身份或就绪。A3 已把 provisioner 的 `_probe` 改为
容器内固定 exec，不再使用宿主 HTTP。

已实现：

- 使用固定命令从 provisioner 侧执行：
  `docker exec <worker> python -m easel.worker.server healthcheck --port 7860`
- 目标必须是已核验 worker inspect 的不可复用 container_id，不能只用容器名；
  固定 argv、10 秒超时；退出 0 还必须由 healthcheck 返回的 workspace/
  config_version/container_id/ready 全部匹配，任意退出 0 不算通过。
- A3 合入的 healthcheck 已验证 execution_capable；真实 Run 路由/身份由
  真实协议测试覆盖。只通过 lifecycle-only check 不得标记执行能力 ready。
- 若主审不接受 `docker exec`，替代方案必须是宿主本地受控代理；不能把
  WSL/Windows 宿主直连容器 IP 写成已通过。

## 4.1 A3 执行 worker 状态（2026-10-27）

- A3-R1 最终 execution 测试镜像：`sha256:970e730af21c1f08647567854dceec61ca542c8d5b0ff30f8417a2690210d62b`。
- 固定入口：`python -m easel.worker.server serve --host 0.0.0.0 --port 7860`；
  UID/GID 10001；单一进程同时提供 lifecycle 和既有 `/internal/runs` 路由。
- 公钥：管理员固定文件只读挂载到 `/keys/worker-public.key`，环境
  `EASEL_WORKER_PUBLIC_KEY_FILE` 指向该固定路径；私钥只在测试签发端/未来
  control，不进入 worker。
- provisioner 固定 exec：`docker exec <不可复用 container_id> python -m
  easel.worker.server healthcheck --port 7860`，10 秒超时；输出最小 JSON，
  校验 workspace/config_version/container_id/ready/execution_capable，不含
  token。
- 既有实例接管只读宿主 token，并与容器实际 Env 比较；缺/错 token、重复
  Env 键或大小写不符失败关闭，不自动轮换、删除或重建活跃实例。探针
  失败、ready=false、paused/restarting/缺状态均保留实例与 sidecar；只有
  明确 exited/dead 才按不可复用 ID 重建。
- SIGTERM/SIGINT 接入 Uvicorn `Server.handle_exit`：首个信号先 drain，监听
  socket 保持到宽限期结束；默认 15 秒内短 Run 完成，超时取消为
  cancelled/interrupted，第二个信号强制取消。
- 持久路径预检（UID 10001）在 `/mnt/c` 与 WSL ext4 临时根均实测通过
  创建、SQLite/WAL、跨进程 flock、fsync、原子替换、重开数据；`/mnt/c`
  被选为 Desktop 持久根。
- 真实容器协议（签名测试快照 + ACP 夹具）通过提交/重复/事件/问题回答/
  停止/重建历史；夹具不是真实 DSH。gateway 仍未实现，A4 未开始。

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

新增 `easel-gateway-<workspace_id>`（G1 已实现独立 ASGI 核心
`create_gateway_app`；容器创建、地址注入、公钥挂载与代次持久化待 G2）：

- 网络加入时点：provisioner 先 inspect `easel-control` 的 IPAM/已用地址，
  在 **create 前**分配固定 IPv4，并执行
  `docker create --network easel-control --ip <fixed>`；禁止 create 后修改
  Env。gateway 仅在控制网络 start 并 inspect 绑定，可信 configure 后再
  `docker network connect easel-ws-<workspace_id>`，最终 inspect 要求恰好
  control+worker 两张网且控制 IP 未变。地址缺失、冲突、歧义或前后变化均
  失败关闭；禁止 `0.0.0.0`、首张网卡或名称解析推断。worker 网络是
  `--internal`；共享控制网络上的其他 gateway/sidecar 本身可达，不承诺
  物理隔绝，跨 workspace 由独立断言、固定目标和白名单拒绝。
- 监听：management listener **只绑定上述固定 control IPv4**。gateway
  容器启动时不接收 worker 目标；provisioner 经固定
  `docker exec -i ... configure` + 有界 stdin JSON 绑定实际
  workspace/worker ID/version/IP/service token，进程生成新的 boot
  generation；配置只保存在内存，不写用户卷。状态经固定 `status` 命令读取
  容器内 0600 Unix socket，不输出 service token。
- 配置与激活分离：configure 只绑定目标且保持未激活；RuntimeManager 在
  owner/generation CAS 中保存候选后，才用固定 `activate` 命令提交精确
  workspace/gateway_instance_id/boot_generation/worker_container_id/
  config_version。任何字段不匹配都拒绝；只有验证 available 后才写 ready。
- 不发布宿主端口、不挂 Docker socket、不挂 worker workspace/home、不读
  控制数据库；仅只读挂载 control 公钥；只读 rootfs、UID 10001、
  cap-drop ALL、no-new-privileges、固定资源上限。worker service 凭证由
  provisioner 从 `.credentials/<workspace_id>.env` 读出后经 configure
  stdin 提供，不来自 control/浏览器，且不进入 argv/日志/状态。
- 请求方向：control → gateway（control 网络）→ 固定 worker（worker 网络）。
  不接受 worker 主动发起的管理请求。
- 认证：
  - **control→gateway**：control 现有 Ed25519 私钥签名的短时断言，独立
    domain `easel.desktop.gateway.v1`，放在唯一内部头
    `X-Easel-Gateway-Assertion`。值为
    `v1.<base64url(canonical_json)>.<base64url(signature)>`，canonical JSON
    使用 `sort_keys=True,separators=(',',':'),ensure_ascii=True`。断言
    绑定 workspace、gateway instance、进程启动时新生成的 boot_generation、
    worker container_id、config_version、method、path、canonical query、
    body SHA-256、issued/expires、nonce、request_id；有效期最多 30 秒，
    未来 iat 容忍 5 秒，nonce 由线程锁原子单次接纳（容量 4096，满时失败
    关闭，不提前驱逐仍有效 nonce），每次请求重新签名。实例默认未激活；
    `activate()` 之前所有管理请求返回 503 `gateway_inactive`，同一容器
    进程重启也必须更换 boot_generation，旧断言即使未过期也拒绝。
  - **验签顺序**：gateway 先验签并校验固定 method/path/query/audience 与
    签名声明的 body SHA-256，再读取 body、取得受保护槽位；读取有界 body
    后比较真实摘要，匹配后才接纳 nonce 并转发。缺失/畸形/错误签名/
    错误 audience/过期断言在读取 body 和占槽前快速拒绝；慢 body 不能延长
    断言有效期。
  - **gateway→worker**：每 worker 独立的 service 凭证，由 provisioner 从
    `.credentials/<workspace_id>.env` 读出后经固定 configure stdin 提供；
    gateway 不接受 control 或客户端替换它，且固定 `container_id` +
    `config_version`，不按 workspace 名自动落到新实例。
  - Cookie、Authorization、Forwarded、`X-Forwarded-*`、外来 `X-Easel-*`、
    Origin、Referer、Via、`X-Real-IP` 永不传给下游；gateway 先验签，再在
    最终 `httpx.Request`（含注入 client 的默认头/cookie/auth 合并之后）
    删除这些头，从可信配置重建 worker workspace/service token。浏览器
    凭证不参与 gateway 认证。

### 5.3 精确路由与查询白名单

只接受下表 method/path；未知方法/路径默认拒绝，不按前缀放行。路径 ID
`run_id`/`session_id` 为 32 位小写 hex；`question_id` 解码后必须是单个
路径段（1..256 字节，不得为 `.`/`..`，不得含 `/`、`\`、`?`、`#`、NUL、
CR、LF）。路径只接受 origin-form，解码一次，拒绝绝对 URI、编码的 `/`/
`\`、`%00`、尾斜杠、重复斜杠、片段和重定向。G1 的路径只允许
`question_id` 单段做一次百分号解码/重编码，并拒绝编码 unreserved 的
别名；其他字面量/ID 段出现 `%` 直接拒绝。查询键按字典序规范化；值只接受 unreserved 字符
或对非 unreserved 字符的百分号编码，拒绝编码 unreserved、裸 `+`/`;`、
空值和重复键；整数参数必须是规范十进制。

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
  I/O/SSE idle 120 秒；每 gateway 最多 8 个并发请求，其中流请求最多 7 个，
  至少保留一个槽给 stop/权限回答等短控制请求（与现有全局活跃 Run 上限 8
  对齐）。饱和立即返回 429 + `Retry-After`，不无限排队。SSE/流式响应逐块
  `write`+`drain`，客户端慢时暂停上游读取，不缓存完整流；客户端断开或
  idle 超时立即取消/关闭上游读取；不自动重发任何 POST；GET/SSE 可按
  `after` 游标恢复。当前 worker `/events` 返回持久事件 JSON 时原样转发，
  不强制改为 SSE。
- 出站 URL：在注入 `AsyncClient` 的默认 params 合并后，最终 Request URL
  必须精确恢复为固定 origin + 已验签 path + canonical query；默认 params
  不得添加、覆盖或重排查询，也不修改共享 client 的 params/headers/cookies/auth。
- 上游清理：每个上游响应只有一个 cleanup task，直接消费原始
  `AsyncByteStream`，不让 `aiter_raw` 在自然 EOF 取得首次关闭所有权；EOF、
  异常、超时、主动关闭、断连和 background 共享该任务。取消等待者不取消
  cleanup，底层关闭完成后才释放槽位。关闭抛错或取消时保留失败终态和槽位，
  隔离整个实例（后续 503 `gateway_unavailable`）；重复关闭共享同一脱敏
  失败结果，缓冲响应返回明确 502，流式响应按传输失败结束。不关闭调用者
  注入的共享 client，也不重试业务请求。
- Content-Length 在 `int` 转换前按十进制位数与 body 上限做有界比较；超长值
  明确 413，不产生 500。重复/冲突 framing 规则不变。

### 5.4 实例代次与恢复

- `WorkerHandle.container_id` 始终保留 worker 不可复用 Docker ID，`upstream`
  保留 worker 内部地址；新增 `gateway_instance_id`（完整 gateway 容器 ID）、
  `gateway_generation`（每进程 boot generation）、`gateway_upstream` 与
  `gateway_state`，`WorkerLease`/control 持久化这些字段。控制面使用
  `gateway_upstream` 作为管理转发地址，不能用 gateway 身份替代 worker ID。
  gateway 标签绑定 workspace、config_version、worker container_id，目标来自
  provisioner 核验的实际 worker 地址；boot generation 不写入 Docker 标签。
- 受限协议 v2：v2 ensure 只返回未激活候选；RuntimeManager 在受保护
  owner/generation CAS 中保存候选后调用 v2 activate；v2 stop 同时携带
  worker container_id 与 gateway_instance_id，先按精确 gateway ID 停止
  gateway，失败时不继续删除 worker；v2 abort 仅按本轮 worker_created/
  gateway_created 清理。未启用 gateway 的旧部署保留 v1；gateway 启用时
  v1 ensure/stop/inspect 失败关闭，不能静默回退直接 worker URL。
- 断言 audience 含 gateway ID/boot_generation，旧断言不能访问新 gateway；
  旧 worker service 凭证不能访问新 worker。
- control 重启：重新加载现有受保护签名私钥；gateway/worker 不变，重新
  签发新断言，不重发旧凭证。
- provisioner 重启：重建 Unix socket；已有 gateway/worker 不删除；control
  重连后按不可复用 ID 复核并继续。
- gateway 重启：**每次进程启动都生成新的 boot_generation**，同一容器/ID
  重启也不得沿用旧代次；provisioner 从容器内 status 读取新 boot，
  RuntimeManager 在 CAS 中保存新候选后 activate。同步前所有管理请求保持
  未激活 503，旧断言即使未过期也因 audience 不符被拒。若容器被重建，同时
  产生新 gateway ID/generation；已开始的 Run 不重发、不伪结算。
- gateway 暂不可用：provisioner status 从实际 inspect 与容器内
  configured/active/available 计算，关闭失败隔离也报告 unavailable，不能
  只看 active 或 worker health；control 返回 503；不删除 active worker；
  Run 不标记 done/failed/interrupted；SSE 断开可带 `after` 恢复；POST 不
  自动重发。
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

这属于新增管理中继和网络边界变化。G1 只实现独立核心、签名工具和测试；
不修改 provisioner/WorkerHandle/schema/WorkerRelay，也不宣称容器运行时
或 control→worker 全链路已启用。

## 6. Broker 契约（不变）

- worker 只经每 workspace sidecar 访问 broker：
  `http://easel-sidecar-<workspace_id>:8899`。
- sidecar 仍加入 `easel-control` 与 `easel-ws-<workspace_id>`；worker 无
  默认路由，不发布端口。
- `--broker-upstream control:7861` 不变；sidecar 只转发精确 broker 路由。
- 不允许把 gateway 的 management 端口复用为任意 broker/出口代理，也不
  放宽 CONNECT 私网限制。

## 7. 批准门（G2 已实现 provisioner 运行时；公开 relay 仍 blocked）

1. WSL socket 0660 + control 补充组 1000 + `--allowed-uid 10001` 的权限模型：
   已由 A1/A2 验收。
2. 用固定 `docker exec` healthcheck 替代宿主 HTTP 探测：已由 A3 验收。
3. gateway 精确路由/查询表、control 签名断言、worker service 凭证、
   请求上限、SSE 背压与失败清理：G1 已实现并通过 R1/R2 收口；跨 workspace
   断言、错误公钥和真实 TCP 转发已测试。
4. G2 已实现：固定 control IPv4 的 create/start、可信 configure/activate
   握手、只读公钥与固定资源/网络、v2 受限协议、WorkerHandle/Lease
   gateway 字段与 Store v11、三态关闭失败隔离上报、两阶段 CAS 激活、
   gateway 重启新 boot 对账与精确 ID 停止；真实双 workspace 拓扑已运行。
5. 公开 WorkerRelay/聊天入口与 A4 broker URL/受管 Agent 接线仍未实现；
   在 A4 验收前不宣称多用户聊天可用。

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
全局 prune，未操作开发者已有资源。

G1 gateway 核心：

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest `
  tests/multiuser/test_gateway_auth.py `
  tests/multiuser/test_gateway_forward.py `
  tests/multiuser/test_gateway_stream.py `
  tests/multiuser/test_gateway_tcp.py -q
```

G1 R2 结果为 **120 passed**；在 R1 的唯一上游关闭任务/慢关闭取消、最终
出站 URL、验签前快速拒绝慢 body/槽位、超长 Content-Length、真实 ASGI send
背压/断连，以及真实 TCP（临时 worker guard + gateway）的合法转发、
跨 workspace/错误断言拒绝、stop/history 和 SSE/断连清理之上，新增 streaming、
buffered、read/idle 收尾的关闭失败隔离，以及失败关闭并发等待/取消竞争。
测试后两个 uvicorn 线程均退出。详细红绿与边界见
[`docs/evidence/desktop-g1-r2/report.md`](../../docs/evidence/desktop-g1-r2/report.md)，
R1 证据保留在
[`docs/evidence/desktop-g1-r1/report.md`](../../docs/evidence/desktop-g1-r1/report.md)，
最早一轮证据保留在
[`docs/evidence/desktop-g1/report.md`](../../docs/evidence/desktop-g1/report.md)。

G2 gateway 运行时：

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest `
  tests/multiuser/test_gateway_server.py `
  tests/multiuser/test_gateway_runtime.py `
  tests/multiuser/test_runtime_gateway.py `
  tests/multiuser/test_store_gateway.py -q
$env:EASEL_RUN_CONTAINER_TESTS = '1'
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest `
  tests/integration/multiuser/test_gateway_runtime_real.py -q
```

G2 静态结果：新增单元 **28 passed**；provisioner/runtime/store/stop
**238 passed, 1 skipped**；multiuser+拓扑全量 **1245 passed, 9 skipped**
（2 条既有 websockets/uvicorn deprecation warning）。真实测试 **1 passed**
（约 377 秒），固定镜像为 gateway
`sha256:a25c259e9c09a36d902ddb9e68f28de803f30fea1636d8b794b4919ffceb5707`、
worker execution
`sha256:f599b224abdbf7ad71052febad55ebba0c87513dce4053d085f84fb3e17dc3cb`；
覆盖两个 workspace、签名 health/submit/events/question status/answer/stop/
messages、跨 workspace 拒绝、worker 反向不可达、同容器 gateway 重启新 boot、
旧断言 503→403、worker ID/在途 Run 保持、精确 ID drain。详细证据见
[`docs/evidence/desktop-g2/report.md`](../../docs/evidence/desktop-g2/report.md)。
