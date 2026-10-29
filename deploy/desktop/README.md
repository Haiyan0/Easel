# Desktop 完整拓扑：契约、装配与运维入口

状态：**A7 运维验收完成代码与真实演练，A7-R1 四项与 A7-R2 三项准备/
只读校验/当前 Stop 绑定返工已实现；A5/A6 真实 Harness、双用户浏览器闭环
与 A6-R1 取消清理互斥已通过功能验收。真实 DeepSeek 供应商与 Linux 生产
隔离门槛仍为 not-run。**

基线：`e8376753d63eb681560ae868aa2d1528b2756c85`（A7-R1）。
A7 入口在本目录新增：

| 入口 | 作用 |
|---|---|
| `Initialize-WorkspaceEasel.ps1` | 只创建本项目卷/网络/key/配置；已有 DB/身份缺 key 时拒绝且不生成替代 key |
| `Start-WorkspaceEasel.ps1` | 固定镜像/卷/网络/key/前端/provisioner 检查后启动；升级备份在 compose up 前完成全量校验 |
| `Stop-WorkspaceEasel.ps1` | 屏障 → drain → down → 完成记录 |
| `Status-WorkspaceEasel.ps1` | 只读诊断 + 镜像/前端/配置/schema/日志版本摘要 |
| `Logs-WorkspaceEasel.ps1` | control/egress/provisioner 最近日志，不读 key |
| `Backup-WorkspaceEasel.ps1` | 成功 Stop 后的 v2 完整备份；源根必须匹配 deployment.json 登记值 |
| `Verify-WorkspaceEasel.ps1` | 只读校验清单全部文件大小/SHA、DB/派生公钥；私有 DB 副本不产生 WAL/SHM |
| `Restore-WorkspaceEasel.ps1` | 新项目/目录/端口恢复；撤销旧登录/token，签发恢复邀请 |

备份/恢复/升级的完整新手命令与错误码见
[docs/windows-desktop-operations.zh-CN.md](../../docs/windows-desktop-operations.zh-CN.md#9-完整-desktop-多人已装配的正式路线)，
Linux 目标门槛与 not-run 清单见
[docs/linux-docker-operations.zh-CN.md](../../docs/linux-docker-operations.zh-CN.md#13-a7-完整-desktopl-unix-可核查命令与-not-run-清单)。
A7 证据见 [docs/evidence/desktop-a7/](../../docs/evidence/desktop-a7/report.md)；
A7-R1 四项返工证据见
[docs/evidence/desktop-a7-r1/](../../docs/evidence/desktop-a7-r1/report.md)；
A7-R2 三项返工证据见
[docs/evidence/desktop-a7-r2/](../../docs/evidence/desktop-a7-r2/report.md)。

> 注意：以下 A2/A4 契约保留为历史设计与验收记录；其中“A5/A6 未做”的
> 状态描述以本段和 A5/A6/A7 证据为准。不要把历史段落当成当前缺口。

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
  容器内 0600 Unix socket，分别报告 worker_ready/worker_running 与 gateway
  configured/active/available，不输出 service token。
- 配置与激活分离：configure 只绑定目标且保持未激活；RuntimeManager 在
  owner/generation/attempt CAS 中保存候选后，才用固定 `activate` 命令提交
  精确 workspace/gateway_instance_id/boot_generation/worker_container_id/
  config_version。任何字段不匹配都拒绝；只有 worker 物理就绪且 gateway
  available 后才写 ready。attempt 使迟到的 abort 在实例被新 owner 采用后
  CAS 失败，不能删除新代次资源。
- 启动 abort 的停止目的/范围/attempt/目标会持久化到 Store v13；provisioner
  另有持久 attempt 文件，重启后仍能验证精确 scope。gateway-only abort 只
  删除本轮新 gateway，恢复后保留既有 worker/凭证/历史/Run，回到
  ready/unavailable；不写成清空 worker 的假 stopped。物理 abort 完成后
  保留 completed tombstone，同一 attempt/ID/scope 的 DB 未提交重试幂等
  成功；新 attempt 才能取代旧记录。
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
- 受限协议 v2：v2 ensure 携带 attempt_id 并只返回未激活候选；RuntimeManager
  在受保护 owner/generation/attempt CAS 中保存候选后调用 v2 activate；v2
  stop 同时携带
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
- gateway 重启/refresh 崩溃：**每次进程启动都生成新的 boot_generation**，
  同一容器/ID 重启也不得沿用旧代次；provisioner 从容器内 status 读取新
  boot，RuntimeManager 在 CAS 中保存新候选后 activate。refresh 的 starting
  过期保留 worker ID/gateway 候选/attempt，按 refresh 对账，不按冷启动
  清空身份或中断活跃 Run；临时 ensure/activate 失败恢复 ready/unavailable。
  同步前所有管理请求保持未激活 503，旧断言即使未过期也因 audience 不符
  被拒。若容器被重建，同时产生新 gateway ID/generation；已开始的 Run 不重发、
  不伪结算。
- gateway 暂不可用：provisioner status 从实际 inspect 与容器内
  worker_ready/worker_running、configured/active/available 计算，关闭失败
  隔离也报告 unavailable，不能只看 active 或 worker health；control 返回
  503；不删除 active worker；Run 不标记 done/failed/interrupted；SSE 断开
  可带 `after` 恢复；POST 不自动重发。物理 worker 未就绪只拒绝派发；物理
  gateway 同 boot 恢复时通过 CAS 恢复 DB available，不永久 identity mismatch。
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
   gateway 字段与 Store v12、三态关闭失败隔离上报、两阶段 CAS 激活、
   gateway 重启新 boot 对账与精确 ID 停止；真实双 workspace 拓扑已运行。
   G2 R1 进一步收口 attempt 清理认领 fencing、refresh 崩溃恢复、
   worker_ready/available 分离同步，以及 cidfile 创建超时/取消清理。
   G2 R2 追加 Store v13 停止目的/范围持久化与 provisioner 持久 attempt
   文件，确保 gateway-only abort 在控制/provisioner 重启后仍不删除既有
   worker。G2 R3 保留 completed attempt tombstone，使物理清理成功但
   control 未提交 DB 的恢复重试幂等成功。
5. A4 已实现公开 WorkerRelay/聊天入口、gateway 逐请求签名与逐 workspace
   broker URL/受管 Agent 接线；真实 Harness/供应商与完整浏览器闭环仍留
   A5/A6。A4 前不得宣称多用户真实聊天已验收。

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

G2 R3 静态结果：新增单元 **39 passed**；指定 provisioner/runtime/store/
stop/gateway/拓扑回归 **288 passed, 6 skipped**；multiuser+拓扑全量
**1261 passed, 9 skipped**（2 条既有 websockets/uvicorn deprecation warning）。
真实测试 **3 passed**（78.08 秒），固定镜像为 gateway
`sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a`、
worker execution
`sha256:0d8ea2c3e6f21d3ba9cb12125f80dd851eef73c013d0fb24d7a69676dfab7ef4`；
覆盖两个 workspace、签名 health/submit/events/question status/answer/stop/
messages、跨 workspace 拒绝、worker 反向不可达、同容器 gateway 重启新 boot、
旧断言 503→403、worker ID/在途 Run 保持、精确 ID drain，以及真实
docker create 已创建后 CLI 超时的 cidfile 定向清理，以及 gateway-only abort
清理失败恢复后既有真实 worker 容器仍 Running/ID 不变。详细证据见
[`docs/evidence/desktop-g2-r2/report.md`](../../docs/evidence/desktop-g2-r2/report.md)，
R1 证据保留在
[`docs/evidence/desktop-g2-r1/report.md`](../../docs/evidence/desktop-g2-r1/report.md)，
上一轮证据保留在
[`docs/evidence/desktop-g2/report.md`](../../docs/evidence/desktop-g2/report.md)。

---

# A4：公开 control relay、签名 gateway 与逐 workspace broker

状态：**A4 装配已完成。** 公开已认证聊天入口现在经 G1/G2 gateway 到固定
workspace worker；每 Run 的 broker 地址来自认证后重查的 workspace UUID，
worker 只收到短期 Run token。真实 DeepSeek Harness/供应商与完整双用户
浏览器闭环仍属于 A5/A6，不在本轮宣称通过。

## 1. 正式路径

```text
浏览器 → control:7861（登录/CSRF/Origin/Run 账本）
       → gateway（固定 control IPv4，Ed25519 逐请求断言）
       → worker（固定 container_id/config_version，service token）
       → sidecar（http://easel-sidecar-<workspace>:8899）
       → control broker（最小 Run token + 不可变配置快照）
       → 受控出口代理 → 模型协议
```

- control 只使用 `handle.gateway_upstream`，不直接访问 worker URL，也不把
  浏览器 Cookie、Authorization、worker service token 发给 gateway。
- 每个请求重新读取已 CAS 确认的 gateway 实例/boot/config audience 后签名；
  JSON body 只序列化一次。GET 可按游标恢复；POST 不自动重发。
- gateway 暂不可用返回稳定 503（可带 `Retry-After`），不停止 worker、不
  撤销 broker token、不把 Run 结算为 interrupted。既有 Run 的恢复只允许
  provisioner 在同一 worker ID/config 上重配/更换 gateway。
- 公开 SSE 仍由 control 轮询持久事件；已建立连接遇到临时 503 保持连接并
  继续按 after 拉取。

## 2. 受管 Agent 配置

`broker` 只接受两种非秘密标签：

```json
{
  "revision": 1,
  "agent": {
    "runtime": "acp",
    "command": ["node", "/opt/dsh/apps/cli/lib/bin.js",
                "--profile", "acp",
                "--patch", "/opt/easel/managed/acp.patch.yml"],
    "sandbox": "workspace-write",
    "timeout": 300
  },
  "broker": {"mode": "workspace-sidecar", "channel": "chat"}
}
```

- `workspace-sidecar` 只允许 `{mode, channel}`；地址固定派生为
  `http://easel-sidecar-<workspace_id>:8899`，不接受 URL、模板变量或客户端
  地址。
- 旧固定 URL 兼容模式仍可写 `{"url": "https://broker.internal", "channel":
  "chat"}`；两种模式不能混用。
- 受管配置不得包含 provider Key；逐 Run token 由 `BrokerService.issue` 在
  接纳后签发。
- A5 起正式 ACP 命令来自
  `deploy/multiuser/managed/acp-profile.json`（固定 Harness commit 与
  公开 `acp` profile），必须使用对应 `runtime` worker 镜像；Harness 的
  OpenAI-compatible provider 只访问本空间 sidecar broker，供应商 Key
  仍只在 control。

## 3. 初始化与启动（Windows PowerShell）

```powershell
# 1) 一次性初始化专用卷、控制网络、master key 与 Ed25519 签名 key
powershell -NoProfile -ExecutionPolicy Bypass `
  -File deploy/desktop/Initialize-WorkspaceEasel.ps1 `
  -Image sha256:<control-image-id> `
  -ManagedAgentConfig C:\Easel\desktop\managed-agent.json `
  -EgressPolicy C:\Easel\desktop\egress-policy.json `
  -ProjectName easel-workspace-desktop

# 2) 启动 WSL provisioner + control + egress
powershell -NoProfile -ExecutionPolicy Bypass `
  -File deploy/desktop/Start-WorkspaceEasel.ps1 `
  -PublicOrigin https://<your-ngrok-domain> `
  -Image sha256:<control-image-id> `
  -WorkerImage sha256:<worker-image-id> `
  -GatewayImage sha256:<gateway-image-id> `
  -ProjectName easel-workspace-desktop

# 3) 只读诊断
powershell -NoProfile -ExecutionPolicy Bypass `
  -File deploy/desktop/Status-WorkspaceEasel.ps1 `

# 4) 先 drain 所有 workspace，再停止 control（保留卷/网络）
powershell -NoProfile -ExecutionPolicy Bypass `
  -File deploy/desktop/Stop-WorkspaceEasel.ps1 `
  -ProjectName easel-workspace-desktop `
  -DataRoot C:\Easel\desktop
```

初始化重复执行不会轮换 master key 或签名私钥；已有公钥缺失/不匹配时从现有
私钥重建，不会覆盖私钥。worker/gateway 只得到 `C:\Easel\desktop\public`
下的只读公钥副本；master key、Ed25519 私钥和 control 数据库只在 control
可见的卷中。egress 使用独立的 `<project>-egress` 策略卷，只挂载
`/policy/egress-policy.json`，不挂 key 卷。

Start/Status 的公钥基准不是宿主副本，而是 key 卷内私钥派生出的 worker raw
公钥与 gateway PEM 公钥 SHA-256；`keygen --check --fingerprints` 同时验证
卷内公钥与私钥一致。宿主 `DataRoot/public` 副本、key 卷策略和 egress 卷
策略必须与这些可信摘要完全一致，否则返回 `public_copy_mismatch` /
`policy_copy_mismatch`，不自动换 key。

Start 只读采集 Windows Docker CLI 与指定 WSL 的 Engine ID，两者必须一致；
WSL context、socket 属主 UID、主 GID（必须等于 control 的 ProvisionerGid）、
mode 660 与 allowed UID 10001 写入 expectedIdentity。采集失败或 Engine 不同
即失败关闭。随后在固定镜像内以 UID 10001 执行只读
`provisioner_identity.py check`，精确核对 Engine ID/context、镜像、workspace
根、网络、broker upstream、key/policy 指纹和 socket 身份。配置漂移返回
`provisioner_config_mismatch`；错误服务或读取失败返回
`provisioner_identity_unavailable`，脚本不会盲删活跃 socket 或停止实例。
新建 provisioner 后同样核验。Status 复用同一契约，并检查实际端口、挂载、
密钥、配置卷、egress 隔离和停止屏障；任一必需项失败返回非零并输出
`status=failed failures=...`。

## 4. 端口、网络与资源

- control 只发布 `127.0.0.1:7861`；gateway/worker/sidecar/egress 不发布宿主
  端口，不挂 Docker socket。
- 专用网络 `<project>-control`（bridge、非 internal、IPv4-only）；gateway
  管理地址为 control 网络上的固定 IPv4。worker 仍使用每 workspace 的
  `easel-ws-<workspace>` internal 网络。
- worker/gateway 由 WSL2 provisioner 按需创建；sidecar 同时接入 workspace
  网络和专用控制网络，broker upstream 固定为 `control:7861`。
- Desktop 无条件使用 `--quota-mode none --allow-unquotaed` 时属于**开发
  限制**；生产 XFS 配额门槛不变，不因 Desktop 通过而通过。

## 5. 诊断码

启动脚本在创建/启动任何资源前校验并输出稳定错误码，例如：

```text
control_image_unavailable / control_image_mismatch
worker_or_gateway_image_mismatch
volume_not_owned / network_not_owned / network_invalid
workspace_keys_invalid / managed_config_missing
public_file_missing:<name> / frontend_build_missing
public_copy_mismatch:<name> / policy_copy_mismatch
workspace_key_fingerprints_missing / policy_copy_missing
docker_engine_unavailable / wsl_docker_engine_unavailable
docker_engine_mismatch / docker_context_unavailable
provisioner_gid_mismatch / provisioner_socket_owner_unknown
desktop_lifecycle_busy / desktop_lifecycle_lock_unavailable
wsl_repo_missing / wsl_python_dependencies_missing
provisioner_start_timeout / compose_config_invalid
provisioner_config_mismatch / provisioner_identity_unavailable
provisioner_identity_mismatch / admission_barrier_active
control_health_timeout / control_unhealthy
```

停止脚本默认要求 provisioner socket 与控制面可达；先写入跨进程停止接纳
屏障，再 drain 并复核没有活跃 Run、请求租约或 starting/ready/draining/
stopping 实例。无法 drain 或复核失败时返回
`worker_drain_failed`/`worker_drain_incomplete`/`provisioner_socket_missing`，
不会静默停止 control。成功 down 后 Stop 用一次性 control 容器写入
`stop_completion`（barrier ID、项目、expected-identity SHA-256）；失败或
取消没有该记录。屏障不会在 control 停止后自然失效。

Start/Stop 从读取资源到 drain、down、provisioner 进程操作、完成记录和
重开全程持有跨进程命名互斥；锁名只由真实 Compose 项目派生，不含 DataRoot，
同一项目不同 PowerShell 进程和 `-Force` 都不能绕过。等锁超时返回
`desktop_lifecycle_busy`，不会执行物理动作；异常/取消由 finally 或 OS 释放。

Start 看到屏障时只调用 `admin reopen`：仅当当前 barrier ID、项目、配置
摘要与完成记录完全匹配，且持久状态无活跃 Run/请求/未收口实例，才原子清除
并继续；因此正常 Stop→Start 可直接重开，连续多轮成立。迟到的第二个 Stop
必须等锁并重新读取当前状态，不能沿用旧 drain 快照；失败/取消/旧记录/
新 barrier 保持失败关闭，需显式恢复：

```powershell
docker exec <control-container> python -m easel admin resume `
  --control-dir /srv/easel/control
```

`-Force` 只用于已确认无活动任务后的强制停止。

## 6. 本轮验收命令

```powershell
# gateway client / relay / runtime 恢复 / workspace broker
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest `
  tests/multiuser/test_gateway_client.py `
  tests/multiuser/test_gateway_relay.py `
  tests/multiuser/test_runtime_gateway.py `
  tests/multiuser/test_workspace_broker.py `
  tests/multiuser/test_desktop_workspace.py -q

# 真实 control→gateway→worker→sidecar→broker（协议夹具 + TLS fake provider）
$env:EASEL_RUN_CONTAINER_TESTS = '1'
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest `
  tests/integration/multiuser/test_desktop_chat_full.py -q -s
```

A4 R1 定向回归：

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest `
  tests/multiuser/test_gateway_relay.py `
  tests/multiuser/test_runtime_gateway.py `
  tests/multiuser/test_stop_barrier.py `
  tests/multiuser/test_provisioner_identity.py `
  tests/multiuser/test_worker_fs_durability.py `
  tests/multiuser/test_workspace_start_identity.py `
  tests/multiuser/test_desktop_workspace.py -q
```

本轮真实证据、红绿与固定镜像见
[docs/evidence/desktop-a4-r2/report.md](../../docs/evidence/desktop-a4-r2/report.md)。
R2/R1 的证据保留在
[docs/evidence/desktop-a4-r2/report.md](../../docs/evidence/desktop-a4-r2/report.md)
与
[docs/evidence/desktop-a4-r1/report.md](../../docs/evidence/desktop-a4-r1/report.md)。
R3 并发启停锁证据见
[docs/evidence/desktop-a4-r3/report.md](../../docs/evidence/desktop-a4-r3/report.md)。
未运行 ngrok 公网浏览器、真实 DeepSeek Harness、真实供应商计费或 A6 双用户
浏览器闭环；这些分别留待 A5/A6。
