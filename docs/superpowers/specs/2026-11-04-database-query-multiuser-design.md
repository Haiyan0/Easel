# 多人 Docker 下 database-query 凭据与连接通路设计

日期：2026-11-04（Asia/Shanghai）

状态：主体实现已入库；2026-11-06 整理补齐 Windows Desktop secrets 接线与部署身份校验。本文保留原始设计，Windows 当前仅支持 direct 拓扑；Linux 生产 SSH 连通性不属于本轮验收结论。

## 1. 背景

`skills/builtin/database-query` 从原平台迁移时保留了两类连接方式：

- 公网 MySQL 直连：DSN 指向公网 `host:3306`。
- SSH 隧道 + 公钥：维护者在能访问跳板机的机器上建立本地转发，DSN 指向本机 `127.0.0.1:<端口>`。

桌面部署按 `references/setup.md` 第 5 节手工建隧道即可。但多人 Docker 部署下这套做法不成立：

1. worker 在每 workspace 独立的 `--internal` 网络里，没有默认路由，到不了跳板机或公网。
2. worker 的 agent 子进程环境由 `easel/worker/config.py` 的 `build_run_env` 按 allowlist 重建，`SALES_READONLY_DATABASE_URL` 这类变量会被丢弃；`setup.md` 第 3 节“部署环境把连接串注入 worker 进程环境”与实际执行链不符。
3. worker 唯一的出口是 sidecar 的 CONNECT 代理，而 `deploy/multiuser/egress_proxy.py` 只允许 `443`；asyncmy 走 MySQL 原生 TCP，不经过 HTTP 代理。
4. SSH 私钥如果放进镜像或 workspace，agent 有 shell，可以直接读取；这与“管理员持有跳板机密钥”的边界冲突。

用户要求：多人 Docker 必须可用；同时支持公网直连和 SSH 公钥隧道两类库；所有 workspace/用户共用同一组数据库账号和同一份 SSH 私钥；不按用户逐份配置。

## 2. 目标、非目标与反目标

### 2.1 目标

- 多人 Docker 的 worker 能通过 database-query 查询公网直连库和经 SSH 跳板机访问的库。
- 连接串与 SSH 私钥只由部署管理员提供一次；所有 workspace 共用。
- 非秘密的连接拓扑随技能版本维护，不要求每个 workspace 单独写配置。
- 技能侧保持现有查询模型：每次调用开连接、查完关闭；不引入连接池或凭据管理服务。
- worker 仍然没有默认路由、没有宿主端口、不能直接访问数据库；agent 读不到 SSH 私钥。
- 不把任何秘密写进仓库、镜像、workspace 输出或日志。

### 2.2 非目标

- 每用户/每 workspace 独立数据库账号或独立 SSH 密钥。
- 网页上传私钥、密钥轮换、Vault/KMS、凭据代理服务。
- 按查询临时建立/销毁 SSH 隧道（隧道常驻，数据库连接即用即关）。
- 任意 TCP 出口或让 worker 直接访问公网。
- 密码认证、`StrictHostKeyChecking=no`、在跳板机上执行任意命令。
- Windows 多人生产部署；多人运行时仍以 Linux Docker Engine 为目标。

### 2.3 反目标（即使技术可用也算失败）

- 把 DSN、私钥、`known_hosts` 打进 worker/control 镜像或提交进仓库。
- 让 agent 能读取 SSH 私钥。
- 给 worker 增加默认路由、宿主端口或 Docker socket。
- 让 agent 能指定任意转发目标；转发目标只能来自随镜像发布的固定拓扑。
- 把技能配置里的 `allowed_tables` 当作安全边界；真正的边界仍是数据库端最小权限只读账号。

## 3. 已确认决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 秘密归属 | 所有 workspace 共用一份 DSN 与 SSH 私钥 | 用户确认 |
| 拓扑归属 | 非秘密拓扑放技能资产，随镜像发布 | 用户要求“在 skill 里配置好” |
| 秘密注入 | 宿主全局 secrets 目录，provisioner 只读挂载 | 镜像不可变，秘密不能烘焙 |
| 隧道位置 | 每 workspace 的 sidecar 内 | sidecar 是唯一同时接 internal 与受控出站网络的组件 |
| 隧道生命周期 | 常驻；数据库连接即用即关 | 避免每次查询握手与端口竞争 |
| 直连库 | 也经 sidecar 转发 | worker 无路由；统一入口 |
| 每库端口 | 固定 `13300–13399`，一库一口 | 配置简单，跨 workspace 可复用 |
| SSH 认证 | 专用无口令私钥 + 固定 known_hosts + BatchMode | 失败关闭，不交互 |
| 启用开关 | 只有一个：`--database-query-secrets`；未提供时节点行为与现状逐字节一致 | 不引入“半启用”状态 |
| 拓扑文件 | 仓库只提交 `tunnel.multiuser.example.yaml`；真实文件 gitignored，构建前由管理员填写 | 基础设施地址不进入公开仓库 |
| 配置迁移 | 启用或停用 secrets 前，先 drain + stop 旧 worker；sidecar 由 provisioner 自动重建 | 沿用现有 fail-closed 语义 |

## 4. 架构

```text
┌──────────────── workspace internal network ────────────────┐
│                                                            │
│  worker (agent + skill)                                    │
│    DSN: mysql+asyncmy://…@db-tunnel:13306/…                │
│        │                                                   │
│        ▼                                                   │
│  sidecar (db-tunnel 别名)                                  │
│    ├─ 13306 direct ──────► db.example.com:3306             │
│    └─ 13307 ssh -N -L ───► jump.example.com:22 ──► 10.0.0.10:3306
│                                                            │
└───────────────────────────┬────────────────────────────────┘
                            │ sidecar 另一条腿：受控出站网络
                            ▼
                    宿主 NAT / 管理员防火墙
```

关键点：

- worker 只认识 `db-tunnel:<端口>`；不需要知道直连还是 SSH，也不需要知道跳板机地址。
- 直连与 SSH 的转发目标都写在随镜像发布的 `tunnel.multiuser.yaml` 里，agent 不能修改。
- 私钥和 `known_hosts` 只出现在 sidecar 的只读挂载中；worker 只挂载 DSN env 文件。
- 数据库连接仍由技能脚本每次创建、查询后关闭；sidecar 只做传输层转发。

## 5. 文件与路径契约

### 5.1 技能侧（非秘密，随镜像发布）

`skills/builtin/database-query/assets/config.multiuser.yaml`：

```yaml
version: 1
project_docs_root: /opt/easel/skills/builtin/project-docs
state_dir: /workspace/outputs/_analytics/database-query/state
output_dir: /workspace/outputs/_analytics/database-query/results
sources:
  - id: yuanmeng_mall_tracking_readonly
    secret_env: YUANMENG_MALL_TRACKING_DATABASE_URL
    allowed_schema: ymkj2.0
    allowed_tables: [track_event]
    readonly_account_verified: true
    enabled: true
  - id: yuanmeng_space_readonly
    secret_env: YUANMENG_SPACE_DATABASE_URL
    allowed_schema: meta_new_sync
    allowed_tables:
      - tan_user
      - tan_vr_device_product
      - tan_common_order
      - tan_common_order_goods
      - tan_class_user
      - tan_tribe_order
      - tan_area_bid_order
      - tan_fixed_yuanmeng_store
      - tan_store_claim_log
      - tan_space_piece_log
      - tan_user_has_nfr
      - tan_core_group_part
      - tan_core_rent_order
      - tan_core_rent_room
    readonly_account_verified: true
    enabled: true
  - id: collectibles_readonly
    secret_env: COLLECTIBLES_DATABASE_URL
    allowed_schema: szcp_sync
    allowed_tables: [fa_user, fa_orders, fa_card_orders]
    readonly_account_verified: true
    enabled: true
```

- 三个 `id` 来自 `project-docs/references/projects.yaml` 实际登记的 `connection_id`；少一个都会让 `doctor` 的 `bindings` 检查失败（已用技能代码确认返回非 0）。
- `allowed_schema` 与表名以仓库业务文档为准：观测库为 `ymkj2.0` 且只授权 `track_event`；共享库是 `meta_new_sync`（不是 `yuanmeng_space`）；数字藏品商城库是 `szcp_sync`。`meta_new_sync` 的 `allowed_tables` 取 5 份业务文档“涉及数据表”的并集，排除跨库引用表（如 `tan_common_resource` 明确“不在本库”）。
- `enabled` 与 `readonly_account_verified` 是部署侧记录，不是权限边界。管理员必须先在目标库确认只读账号持有上述库表权限，才允许保留 `true`；未确认时改为 `false`，对应 source 会被技能统一拒绝（现有 `sources.py` 语义）。
- `collectibles_readonly` 已由用户确认纳入本次启用，与其余两个 source 一样需要 DSN、forward 与 `doctor --connect` 验收。
- 该文件遵循技能现有 `AppConfig` 严格 schema（`extra="forbid"`），不新增字段。
- 路径使用容器内绝对路径；`/opt/easel` 只读，`/workspace` 每 workspace 独立可写。
- 文件随 `COPY skills ./skills` 进入 worker 镜像；sidecar 使用同一镜像，也能读到。
- 该文件不得出现 DSN、口令、私钥内容。

`skills/builtin/database-query/assets/tunnel.multiuser.example.yaml`（提交的模板，全部为占位值）：

```yaml
version: 1
forwards:
  - source: yuanmeng_mall_tracking_readonly
    mode: ssh
    listen_port: 13306
    database:
      host: db-internal.example.com
      port: 3306
    ssh:
      host: jump.example.com
      port: 22
      user: easel-tunnel
      private_key: database-query_ed25519
      known_hosts: known_hosts
  - source: yuanmeng_space_readonly
    mode: direct
    listen_port: 13307
    database:
      host: db-public.example.com
      port: 3306
  - source: collectibles_readonly
    mode: direct
    listen_port: 13308
    database:
      host: db-public.example.com
      port: 3306
```

- 真实文件是同目录的 `tunnel.multiuser.yaml`，已在技能 `.gitignore` 与根 `.gitignore` 中排除；管理员在构建镜像前从 example 复制并填入真实值。真实文件必须进入构建上下文（`.dockerignore` 不得排除），否则 sidecar 启动时会记录 `config_invalid` 并不启动任何 forward。
- `mode` 只是这个 forward 的实现方式；哪个 source 走 `direct`、哪个走 `ssh` 由管理员按真实拓扑填写，设计不预设。跟踪库文档记录其原平台经独立 SSH 隧道访问，模板据此把它标成 `ssh`，仅作示例。
- 多个 source 即使指向同一数据库主机，也各占一个 forward 与端口；v1 不支持共享监听，避免引入 `sources: []` 结构。
- 模板中的 `.example.com` 占位域名在运行时被拒绝（见下），防止“忘了替换模板”被静默接受。

校验规则（sidecar 启动时强制执行）：

- `version` 必须为 `1`；未知字段拒绝。
- `source` 符合技能 `SourceId`：`^[a-z][a-z0-9_-]{2,63}$`，不得重复。
- `mode` 只能是 `direct` 或 `ssh`；`ssh` 必填 `ssh` 段，`direct` 出现 `ssh` 段即拒绝。
- `listen_port` 在 `13300–13399`，不得重复；不得使用 `7860`、`8898`、`8899`。
- `database.host` / `ssh.host`：`^[A-Za-z0-9._-]{1,253}$`（v1 不支持 IPv6 字面量）；`port` 在 `1–65535`。
- `database.host` / `ssh.host` 不得是保留示例域名（`example.com`、`.example`、`.invalid`、`.test`、`localhost`），否则 `config_invalid`。仓库一致性测试以 `allow_placeholders=True` 校验 example 模板，生产 sidecar 固定走严格模式。
- `ssh.user`：`^[A-Za-z0-9._-]{1,64}$`，不得含 `@`。
- `private_key` / `known_hosts` 必须是相对路径，解析后仍位于 `/run/easel-secrets/keys` 内；拒绝 `..`、绝对路径、symlink、缺失文件（`allow_placeholders` 模式跳过文件存在性检查，仅供模板测试）。
- 每个 `enabled: true` 的技能 source 必须有且仅有一个 forward；每个 forward 的 source 必须在技能配置中存在。该一致性由仓库测试保证（比对 `config.multiuser.yaml` 与 example 模板）；sidecar 运行时只校验自身文件。

### 5.2 宿主全局 secrets（秘密，只配置一次）

建议布局（路径由 provisioner 参数指定）：

```text
/srv/easel/secrets/database-query/
├── database-query.env          # DSN，只挂 worker
└── keys/
    ├── database-query_ed25519  # 无口令私钥，只挂 sidecar
    └── known_hosts             # 跳板机固定 host key
```

`database-query.env` 示例（每行一个 `KEY=VALUE`，不展开变量）：

```text
YUANMENG_MALL_TRACKING_DATABASE_URL=mysql+asyncmy://readonly:...@db-tunnel:13306/ymkj2.0
YUANMENG_SPACE_DATABASE_URL=mysql+asyncmy://readonly:...@db-tunnel:13307/meta_new_sync
COLLECTIBLES_DATABASE_URL=mysql+asyncmy://readonly:...@db-tunnel:13308/szcp_sync
```

- DSN 的密码部分必须按 URL 规则百分号编码（`@`、`:`、`/`、`?`、`#`、`%`）；技能拒绝 URL 参数与片段。
- 每一行必须与 `tunnel.multiuser.yaml` 的 `listen_port` 一一对应；端口写错只会在连接时失败，`doctor --connect` 是验收手段。

权限要求（Linux 宿主）：

```bash
install -d -o root   -g 10001 -m 0750 /srv/easel/secrets/database-query
install -d -o root   -g 10001 -m 0750 /srv/easel/secrets/database-query/keys
install -o 10001 -g 10001 -m 0400 <私钥>      /srv/easel/secrets/database-query/keys/database-query_ed25519
install -o root  -g 10001 -m 0440 <known_hosts> /srv/easel/secrets/database-query/keys/known_hosts
install -o 10001 -g 10001 -m 0400 <env 文件>  /srv/easel/secrets/database-query/database-query.env
```

- 私钥必须由 10001 拥有且不可被组/其他用户读写，否则 OpenSSH 拒绝使用（失败关闭）。
- 私钥不得有口令；`BatchMode=yes` 下无法交互输入。
- 跳板机账号应专用，并在 `authorized_keys` 限制为端口转发（例如 `command="",no-pty,no-agent-forwarding,no-x11-forwarding,permitopen="<db_host>:3306"`）。
- DSN 由 worker 的 10001 用户读取；所有 workspace 共用，用户可在会话中读取该文件，这是“共用只读账号”决策的已知边界。

### 5.3 容器内固定路径

| 常量 | 值 | 挂载对象 |
|---|---|---|
| `DB_TUNNEL_CONFIG` | `/opt/easel/skills/builtin/database-query/assets/tunnel.multiuser.yaml` | 镜像内，只读 |
| `DB_QUERY_CONFIG` | `/opt/easel/skills/builtin/database-query/assets/config.multiuser.yaml` | 镜像内，只读；仅文档/命令使用 |
| `DB_QUERY_ENV_PATH` | `/run/easel-secrets/database-query.env` | worker，只读文件挂载 |
| `DB_TUNNEL_KEYS` | `/run/easel-secrets/keys` | sidecar，只读目录挂载 |
| `DB_TUNNEL_ALIAS` | `db-tunnel` | sidecar 在 workspace internal 网络上的别名 |

## 6. 运行行为

### 6.1 启动

1. provisioner 只在配置了 `--database-query-secrets` 时改变节点形状：
   - worker：在原有 3 个挂载之外，增加 `type=bind,src=<secrets>/database-query.env,dst=/run/easel-secrets/database-query.env,readonly`；挂载总数严格为 4。
   - sidecar：在策略文件之外，增加 `type=bind,src=<secrets>/keys,dst=/run/easel-secrets/keys,readonly`；挂载总数严格为 2。
   - sidecar 的 primary 网络仍是 workspace internal，并带 `--network-alias db-tunnel`；之后再 connect 受控出站网络。
   - 未配置 secrets 时，以上三项都不发生：挂载数仍为 3 / 1，没有别名，`write_env_file` 与 `verify_worker_wiring` 的期望值不变。
2. `worker_sidecar.py` 以 `DB_TUNNEL_KEYS` 目录是否存在作为唯一启用信号；任务在启动 broker/egress 监听之后、进入 `serve_forever` 之前启动：
   - 目录不存在：什么都不做（保持旧行为）。
   - 目录存在但 `DB_TUNNEL_CONFIG` 缺失/非法：记录 `state=failed reason=config_invalid`，不启动任何 forward；broker/egress 继续服务（不能因数据库配置错误拖垮聊天链路）。
   - 目录存在且配置合法：逐条启动 forward。
3. 停用 secrets 时的反向迁移与启用相同：旧 worker 仍带第 4 个挂载会被静态验收拒绝，必须先 drain + stop；sidecar 不匹配时由 provisioner 自动删除重建。

### 6.2 直连模式

- `asyncio.start_server` 监听 `0.0.0.0:<listen_port>`；每连接 `asyncio.open_connection(database.host, database.port)`，双向 pipe。
- 连接超时 10 秒；单 forward 并发上限 64，超出直接关闭（仅 direct 模式由本进程限制；SSH 模式的并发由 OpenSSH 与系统 fd 上限约束）。
- 监听 `0.0.0.0` 的原因：worker 是另一个容器，不能连 sidecar 的 `127.0.0.1`。sidecar 不发布宿主端口；除 workspace internal 网络外只接管理员控制网络，属于受信管理面。
- 直连目的地来自镜像内固定拓扑，不由 agent 提供。

### 6.3 SSH 模式

每个 forward 一个常驻 `ssh` 进程，参数固定：

```text
/usr/bin/ssh -F /dev/null -N -T
  -i <DB_TUNNEL_KEYS>/<private_key>
  -o IdentitiesOnly=yes
  -o BatchMode=yes
  -o PreferredAuthentications=publickey
  -o PasswordAuthentication=no
  -o KbdInteractiveAuthentication=no
  -o IdentityAgent=none
  -o ForwardAgent=no
  -o StrictHostKeyChecking=yes
  -o UserKnownHostsFile=<DB_TUNNEL_KEYS>/<known_hosts>
  -o GlobalKnownHostsFile=/dev/null
  -o UpdateHostKeys=no
  -o ExitOnForwardFailure=yes
  -o ConnectTimeout=10
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  -o ControlMaster=no
  -o ControlPath=none
  -p <ssh.port>
  -L 0.0.0.0:<listen_port>:<database.host>:<database.port>
  <ssh.user>@<ssh.host>
```

- 不使用 shell，不执行远程命令；`-N` 只做转发。
- host key 必须与固定 `known_hosts` 匹配；不匹配或缺失即失败，不回退到 `no`/`accept-new`。
- 子进程环境：`HOME=/tmp`、`LC_ALL=C`，不继承 worker/agent 环境。
- 退出后按 1、2、4、8、16、30 秒退避重启；稳定运行 120 秒后重置退避。
- sidecar 收到停止信号时先 SIGTERM 子进程，5 秒后 SIGKILL。

### 6.4 日志

固定格式，不含 DSN、口令、私钥内容：

```text
db-tunnel source=<id> mode=<direct|ssh> state=<starting|ready|failed|stopped> reason=<code>
```

`reason` 只使用固定错误码（`config_invalid`、`key_unreadable`、`host_key_mismatch`、`auth_failed`、`connect_failed`、`forward_failed`、`ssh_unavailable`）。host/port 可以记录（非秘密），但不记录 key 路径与 DSN；ssh 的 stderr 只用于映射固定错误码，不原样写入日志。

### 6.5 技能侧

多人 Docker 的调用方式固定为：

```bash
python /opt/easel/skills/builtin/database-query/scripts/db_query.py \
  --config /opt/easel/skills/builtin/database-query/assets/config.multiuser.yaml \
  --env-file /run/easel-secrets/database-query.env \
  doctor
```

- 不使用进程环境注入 DSN；`--env-file` 由技能自己读取，绕开 `build_run_env` 的 allowlist。
- `doctor --connect --project <ID>` 走真实 `db-tunnel:<port>`，同时验证隧道与数据库认证。
- 每次查询仍然由 `query` 新建连接、结束关闭；不改动 `dbquery` 的连接生命周期。

## 7. provisioner 变更

### 7.1 新参数与依赖

```text
--database-query-secrets <宿主绝对目录>
```

- 未提供：`ProvisionerSettings.database_query_secrets = None`；argv、挂载、inspect 验收与当前完全一致（向后兼容）。
- 提供：启动时校验目录存在、非 symlink；`database-query.env` 是普通文件；`keys/` 是普通目录且非 symlink。非法时 provisioner 启动失败，错误码 `database_query_secrets_invalid`。
- 依赖关系：`--database-query-secrets` 只能在 sidecar 三件套（`--sidecar-network`、`--sidecar-policy`、`--broker-upstream`）齐全时使用；否则启动失败，错误码 `database_query_requires_sidecar`。复用现有 `validate_sidecar_settings` 的结果判断，不新增第二套 sidecar 校验。
- 反向不要求：sidecar 可以照常运行、不配 secrets；此时 sidecar 只做 broker 与 CONNECT 出口，不启动数据库转发。
- 校验通过后保存为 `DatabaseQuerySecrets(env_file: Path, keys_dir: Path)`，两个路径均为 `resolve(strict=True)` 后的绝对路径；后续 argv 与 inspect 只使用该对象。

### 7.2 argv 变更

- `build_run_argv(..., database_query_env_file: Path | None = None)`：
  - 非 `None` 时增加第四个挂载：`type=bind,src=<env_file>,dst=/run/easel-secrets/database-query.env,readonly`。
  - `None` 时不增加任何参数，argv 与当前逐项一致。
- `build_sidecar_run_argv(..., database_query_keys: Path | None = None)`：
  - 非 `None` 时增加第二个挂载：`type=bind,src=<keys_dir>,dst=/run/easel-secrets/keys,readonly`，并在 primary `--network` 后增加 `--network-alias db-tunnel`。
  - `None` 时两者都不增加，argv 与当前逐项一致。
- 所有挂载源必须是校验过的绝对路径；`docker.sock` 仍然拒绝。

### 7.3 inspect 验收（按期望状态严格验收，不用宽松的“3 或 4”）

- `_verify_worker_static(..., database_query_env_file: Path | None)`：期望挂载数严格为 `3 if None else 4`；第四项必须是固定 destination、固定 source、`RW=false`、`type=bind`，且不含 `docker.sock`。任何其他挂载仍失败。
- `verify_sidecar_inspect(..., database_query_keys: Path | None)`：期望挂载数严格为 `1 if None else 2`；第二项必须是 `keys` 目录、固定 destination、`RW=false`。network alias 与开关同向：有 keys 时 workspace 网络条目的 `Aliases` 必须包含 `db-tunnel`；无 keys 时不得包含。
- 不匹配时的现有语义：
  - worker：静态验收失败 → `ProvisionerError("container_conflict")`，失败关闭，不自动删除运行中的容器。启用或停用 secrets 前必须走部署 runbook：drain + stop 所有 workspace（`stop` 会删除容器与 sidecar），再用新参数启动并让 workspace 按需重建。已退出（exited/dead）的旧形状 worker 仍按现有流程删除重建。
  - sidecar：`_sidecar_verified` 返回 false → `_remove_sidecar_instance` 后重建，无需人工步骤；`_ensure_sidecar` 在 worker 创建/复用前调用，保证新 worker 不会配到旧形状 sidecar。
- 现有不带 secrets 的测试必须保持通过；新增测试覆盖“带 secrets 但缺少挂载/别名”必须失败。

### 7.4 环境变量与凭据更新

- 不修改 `_ENV_ALLOWLIST`，不把 DSN 放进 worker 的 `--env-file`；DSN 只通过只读文件进入容器。
- 不修改 `NO_PROXY`，也不修改 `verify_worker_wiring`：asyncmy 是原生 MySQL TCP，不读取 HTTP 代理变量；为 `db-tunnel` 增加代理排除既无收益，又会改变既有实例的接线验收。
- 凭据文件只在容器创建时被 bind mount；轮换 DSN 或私钥后必须重建对应容器，不能假设运行中的容器会自动看到新内容。

## 8. 安全边界与已知风险

### 8.1 边界

- worker：internal 网络、无默认路由、无宿主端口；只能连 sidecar 的 broker、egress 与 db-tunnel 端口。
- sidecar：管理员镜像与固定 argv；worker 不能修改其配置、不能读取其文件系统。
- 私钥：只在 sidecar 的只读挂载中；worker 不挂载 `keys/`。
- 转发目标：来自镜像内固定拓扑；agent 不能新增或修改 forward。
- 数据库权限：由数据库端最小权限只读账号保证；技能配置的白名单不是权限边界。
- 出口代理：现有 CONNECT 策略仍然只允许 443；DB 流量不经过它，而是走独立的、管理员固定配置的 forwarding。这是本设计新增的受控例外，不是放宽 worker 的通用出口。

### 8.2 已知风险（接受并记录）

- 所有用户共用同一 DSN 与 SSH 私钥；任一用户通过 agent 读取 DSN 后可在自己的 workspace 内直接使用该只读账号。数据库账号必须是最小权限只读账号。
- 所有用户共用同一 SSH 私钥；必须使用专用跳板机账号并限制 `permitopen`，降低密钥泄露后的影响面。
- `tunnel.multiuser.yaml` 与 `config.multiuser.yaml` 随镜像发布，用户/agent 可读取数据库主机名与跳板机地址；这与“企业知识库随内部镜像分发”的既有边界一致，不包含秘密。
- sidecar 的隧道直连出站不经过 CONNECT 策略；其目的地由管理员在构建期固定。若后续要求“所有出站必须经同一条策略”，可扩展 `approved_services` 并让 sidecar 走本地 CONNECT，作为后续增强，不纳入本版。
- sidecar 进程能读取 SSH 私钥；其攻击面是 broker/egress/db_tunnel 的 Python 服务与 ssh 客户端。sidecar 不挂载 workspace、不暴露管理接口、只读 rootfs、`cap-drop=ALL`；私钥泄露的兜底仍是专用跳板机账号与 `permitopen` 限制。
- 每 workspace 各自建立 SSH 连接：N 个 workspace × M 个 SSH source 会产生 N×M 条常驻连接。跳板机必须允许相应并发；本版不引入共享隧道容器。若 workspace 数量明显增大，可另立设计做“全局隧道 sidecar + 每 workspace internal 网络接入”，不改变技能语义。
- 网络前提由部署方保证：sidecar 所在的 `easel-control` 网络必须有宿主 NAT/路由到数据库与跳板机，宿主防火墙必须放行出站 22/3306（按实际端口）。这些不在代码中校验，验收失败时先查这一层。
- `ymkj2.0` 含点号且与常见“库名.表名”写法相似，必须靠 `doctor --connect` 的真实库名核验确认；不得在文档中把它当成 `ymkj2` + `0`。
- `tunnel.multiuser.yaml` 与 `config.multiuser.yaml` 的一致性没有运行时交叉校验（sidecar 不导入技能配置）；由仓库测试校验 example 模板，管理员替换真实值时必须保持 source/端口/启用状态一致。漏配的 source 会在 `doctor --connect` 或首次查询时以连接失败暴露，不会被误报为可用。

## 9. 文档与仓库变更

- `skills/builtin/database-query/references/setup.md`
  - 重写“多人 Docker worker”小节：固定配置路径、`--env-file` 路径、`db-tunnel` 端口、两类库的拓扑示例。
  - 删除“部署环境把连接串注入 worker 进程环境”的错误描述。
  - 保留桌面手工 SSH 隧道小节，并明确“多人 Docker 不要在本机建隧道”。
  - 增加 secrets 布局、权限、跳板机 `authorized_keys` 限制。
- `skills/builtin/database-query/SKILL.md`
  - 增加多人 Docker 的固定命令；桌面临时隧道仍按 setup.md。
- `skills/builtin/database-query/references/security-and-limits.md`
  - 记录共享账号/共享私钥、agent 可读 DSN、白名单非权限边界。
- `deploy/multiuser/README.md`、`docs/linux-docker-operations.zh-CN.md`
  - provisioner 新参数、secrets 目录、验收命令。
- `docs/windows-desktop-operations.zh-CN.md`
  - 明确多人隧道只在 Linux 部署；桌面继续手工隧道。
- `.dockerignore`
  - 增加 `skills/**/config.yaml`、`skills/**/config/database-query.yaml`、`skills/**/state/`、`skills/**/outputs/`、`skills/**/.secrets/`、`skills/**/.env`；不得排除 `assets/config.multiuser.yaml` 与 `assets/tunnel.multiuser.yaml`。
  - 增加 `新增的skill/`、`.tmp-dsh-iso/`、`skill-test/`，避免原始技能副本、构建日志与嵌套仓库进入构建上下文。
- `.gitignore` 与技能 `.gitignore`
  - 根 `.gitignore` 增加 `新增的skill/`、`.tmp-dsh-iso/`。
  - `skills/builtin/database-query/.gitignore` 增加 `assets/tunnel.multiuser.yaml`，保留 `assets/tunnel.multiuser.example.yaml` 可提交；`config.multiuser.yaml` 仍提交（非秘密）。
- 新增 `deploy/multiuser/db_tunnel.py`；`worker_sidecar.py` 集成。
- `deploy/multiuser/Dockerfile.worker`
  - 最终 `runtime` 阶段（`USER root` 后）安装 `openssh-client`：sidecar 使用同一最终镜像，且这样不使 `chromium` 浏览器层缓存失效。
  - `execution` 阶段 `COPY deploy/multiuser/db_tunnel.py ./deploy/multiuser/db_tunnel.py`。
- `deploy/multiuser/README.md` 增加部署 runbook：
  1. 复制 example 为真实 `tunnel.multiuser.yaml`，填真实主机/端口/账号与密钥文件名；
  2. 创建 `/srv/easel/secrets/database-query/` 与权限；
  3. 重建镜像（真实拓扑文件必须位于构建上下文）；
  4. drain + stop 所有 workspace（启用/停用 secrets 的必经步骤），重启 provisioner 并加 `--database-query-secrets`；
  5. 抽查 sidecar `docker logs` 是否出现每个 source 的 `state=ready`；
  6. 在 worker 内对每个 source 执行 `doctor --connect`；
  7. 记录时间与输出，未通过前不得声明多人数据库可用。

## 10. 测试与验收

### 10.1 单元测试

- `tests/multiuser/test_db_tunnel.py`
  - 拓扑 schema：重复 source/port、非法 mode、越界端口、路径穿越、symlink、未知字段、含 `password` 字段、保留示例域名全部拒绝。
  - 直连转发：起一个本地 echo TCP 上游，经 forward 往返字节一致；并发上限行为。
  - SSH argv：用假 `ssh` 可执行文件记录 argv，逐项断言固定参数；断言无 shell、无 `StrictHostKeyChecking=no`。
  - 监督：假 ssh 立即退出，断言退避重启序列；SIGTERM 后子进程退出。
- `tests/multiuser/test_database_query_multiuser_config.py`
  - `config.multiuser.yaml` 通过技能 `AppConfig` 校验；`tunnel.multiuser.example.yaml` 以 `allow_placeholders=True` 通过 `db_tunnel` 校验；两边 source 集合、启用状态、端口一致（enabled 的 source 必须有且仅有一个 forward，forward 必须在配置中存在）。
  - 三个 connection_id 与 `project-docs/references/projects.yaml` 的绑定逐一对应，保证 `doctor` 的 `bindings` 检查通过。
  - 两个文件都不含 `password`/`secret`/`private_key` 内容字段。
- `tests/multiuser/test_worker_sidecar.py`
  - 无 keys 目录时行为与现在一致；有 keys 目录且配置合法时启动转发任务；配置非法时 broker/egress 仍可用；缺配置时记录 `config_invalid`。
- `tests/multiuser/test_sidecar_provisioner.py`
  - 带/不带 `--database-query-secrets` 的 argv 与 inspect 验收：挂载数严格为 4/2 或 3/1，不能用“3 或 4”“1 或 2”放行旧形状。
  - `--database-query-secrets` 缺少 sidecar 三件套时启动失败（`database_query_requires_sidecar`）；sidecar 无 secrets 仍可正常启动。
  - 缺失/非法 secrets 目录启动失败；network alias 在启用时必须存在、未启用时必须不存在。
  - 旧形状 worker 在启用 secrets 后必须被 `container_conflict` 拒绝；旧 sidecar 必须被自动删除重建。

### 10.2 worker 执行链测试（针对审计 P2-1）

新增测试：用 `build_run_env` 构造真实的 worker 子进程环境（确认 `SALES_READONLY_DATABASE_URL` 被丢弃），再以该环境运行技能 CLI：

```text
python db_query.py --config <temp config> --env-file <temp env> doctor
```

断言 `--env-file` 中的 DSN 被读取、`doctor` 通过、stdout 不含 DSN。该测试证明文档里的多人路径不依赖进程环境变量。

### 10.3 Docker 集成测试（Linux）

- 建立 workspace internal 网络与受控出站网络；在出站网络起 MySQL 容器；sidecar 带 `direct` forward 接入两网；worker 容器在 internal 网络。
- 断言：
  - worker 经 `db-tunnel:<port>` 能完成 MySQL 握手与只读查询。
  - worker 直连 MySQL 容器地址失败。
  - `docker inspect` 中 worker 没有 `keys/` 挂载，sidecar 有。
- SSH 模式：起一个带固定 host key 与 authorized_keys 的 sshd 容器，MySQL 只在该 sshd 容器网络内可达；sidecar 用测试私钥与 known_hosts 建立转发；断言 worker 经转发可查、直连不可达。
- 现有跳过真实 MySQL 的测试保持跳过条件；新增测试在没有 Docker/Linux 时明确 skip，不伪装通过。

### 10.4 真实环境验收（必须由部署管理员执行）

在目标 Linux 宿主上，用真实的跳板机、私钥和 MySQL 只读账号：

1. 配置 `/srv/easel/secrets/database-query/` 并按第 5.2 节设置权限。
2. 复制 example 为真实 `tunnel.multiuser.yaml` 并填入真实值；确认其中没有 `.example.com` 等占位域名。
3. 重建镜像；确认镜像内存在 `assets/tunnel.multiuser.yaml` 与 `assets/config.multiuser.yaml`。
4. drain + stop 所有 workspace；以 `--database-query-secrets` 启动 provisioner，重建至少一个 workspace。
5. 抽查 sidecar `docker logs`：每个启用的 source 都出现 `state=ready`；没有 `config_invalid`/`key_unreadable`/`host_key_mismatch`。
6. 在 worker 内对**每个启用的 source**执行 `doctor --connect --project <精确 ID>`，应全部通过。
7. 确认每个 source 的 `SELECT 1` 与一张授权表的限行查询成功，不输出业务记录。
8. 在 worker 内尝试直连数据库地址，应失败；确认 worker 没有私钥文件。
9. 保存命令输出与时间；真实结果之前，不得在部署材料中表述“多人数据库已可用”。

## 11. 实施分期

| 阶段 | 内容 | 验收 |
|---|---|---|
| 1 | `assets/config.multiuser.yaml`、`assets/tunnel.multiuser.example.yaml`、`.dockerignore`/`.gitignore`、setup/SKILL 文档、worker 执行链测试 | 配置校验与 `--env-file` 测试通过；文档路径/命令校验通过 |
| 2 | `db_tunnel.py` + sidecar 集成 + Dockerfile（runtime 阶段 openssh-client） | 单元测试通过；sidecar 无 secrets 时行为不变 |
| 3 | provisioner secrets 参数、依赖校验、挂载、alias、严格 inspect 验收 | provisioner/sidecar 现有与新增测试通过 |
| 4 | Docker 集成测试 + 真实 MySQL/跳板机验收 | Linux 集成测试通过；管理员对每个 source 的真实验收通过 |

阶段 1 可以先交付，解决审计 P2-1 与 `.dockerignore`；P2-2 在阶段 2–4 完成后才算端到端可用。

## 12. 留给实现者的细节

以下默认值可在实现时调整，但不得改变第 2 节的边界：

- 直连并发上限 64、连接超时 10 秒、SSH 退避序列与稳定重置时间。
- 端口段 `13300–13399`；如需更多库，扩大段并保持唯一。
- `db_tunnel.py` 的内部结构（dataclass schema、日志器、任务取消方式、SSH `state=ready` 的判定方式）。
- 是否提供 secrets 权限检查辅助脚本；v1 以文档中的 `install` 命令为准。
- 集成测试中 sshd/MySQL 容器的镜像与启动参数；必须固定版本，不使用 `latest`。
- 真实拓扑文件的填写方式（手工编辑或后续提供初始化脚本）；v1 以 example 复制 + 手工填写为准。
