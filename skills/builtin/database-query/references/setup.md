# 安装与配置

## 1. 依赖

需要 Python 3.10–3.12。Easel 已在 `pyproject.toml` 声明 `pydantic`、`PyYAML`、`SQLAlchemy`、`asyncmy`、`sqlglot`；优先使用项目 Python（本地默认 `python`，Docker 镜像内为镜像自带的 Python 环境）。单独安装时：

```powershell
python -m pip install -r skills/builtin/database-query/requirements.txt
```

已验证版本见 `requirements.txt` 注释与 `PROVENANCE.md`；不要在没有验证的情况下升级 sqlglot 或数据库驱动。

## 2. 配置文件位置

### 本机 / 桌面部署

把示例复制到技能目录，CLI 默认会读取该路径；`config.yaml` 已被 `.gitignore` 排除，不得提交：

```powershell
Copy-Item skills/builtin/database-query/assets/config.example.yaml skills/builtin/database-query/config.yaml
```

### 多人 Docker worker

多人部署的配置与凭据路径固定，不随 workspace 改变：

- 配置：`/opt/easel/skills/builtin/database-query/assets/config.multiuser.yaml`（随镜像只读分发，非秘密）
- 凭据：`/run/easel-secrets/database-query.env`（provisioner 从宿主 secrets 只读文件挂载；技能的 `--env-file` 是唯一凭据入口）

完整命令：

```bash
python /opt/easel/skills/builtin/database-query/scripts/db_query.py \
  --config /opt/easel/skills/builtin/database-query/assets/config.multiuser.yaml \
  --env-file /run/easel-secrets/database-query.env \
  doctor
```

`config.multiuser.yaml` 的 `project_docs_root` 指向镜像内只读的 `/opt/easel/skills/builtin/project-docs`，状态和结果写入 `/workspace` 下的持久输出目录。三个 source 必须与 `project-docs/references/projects.yaml` 登记的 `connection_id` 一致；少一个 `doctor` 的 `bindings` 检查就会失败。

隧道拓扑固定在镜像内的 `tunnel.multiuser.yaml`（真实文件，不入库）：

- 监听端口范围为 `13300–13399`，各 forward 不得重复，也不得占用 `7860`、`8898`、`8899`。
- 每条 forward 只能是 `direct`（sidecar 直接连数据库）或 `ssh`（sidecar 以固定参数常驻 ssh 转发）；具体哪个 source 走哪种模式由管理员按真实拓扑填写。
- worker 内的 DSN 一律指向 `db-tunnel:<listen_port>`。

### 配置格式

```yaml
version: 1
project_docs_root: ../project-docs
state_dir: ./state
output_dir: ./outputs
sources:
  - id: sales_readonly
    secret_env: SALES_READONLY_DATABASE_URL
    allowed_schema: sales
    allowed_tables: [orders, customers]
    readonly_account_verified: false
    enabled: false
```

- 相对路径一律相对于**配置文件所在目录**解析；也支持绝对路径。
- `project_docs_root` 必须指向一个已存在的 `project-docs` 技能目录（其下包含 `projects.yaml`）。
- `projects.yaml` 维护 `project_id → connection_id`；本配置维护 `connection_id → secret_env/schema/tables`。
- 示例中的源默认关闭且未验证；准备最小权限只读账号并完成部署检查后，才把 `enabled` 与 `readonly_account_verified` 置为 true。

## 3. 注入凭据

环境变量的值是 MySQL 连接串（scheme 为 `mysql` 或 `mysql+asyncmy`，包含账号、口令、主机、端口与库名）。桌面/本机可在进程环境中设置，或使用显式指定的 env 文件：

```powershell
$env:SALES_READONLY_DATABASE_URL = "…"
python skills/builtin/database-query/scripts/db_query.py --config skills/builtin/database-query/config.yaml --env-file .env doctor
```

- `--env-file` 只读取**显式指定**的文件，按 `KEY=VALUE` 字面解析：不展开 `$VAR`、不执行文件内容、拒绝重复键。Easel 项目根的 `.env` 已被仓库忽略，可作为本机凭据来源。
- 多人 Docker 的 worker 进程环境由 `build_run_env` 的 allowlist 重建，DSN 不会从宿主环境继承；凭据只通过只读文件 `/run/easel-secrets/database-query.env` 进入容器。不要在 provisioner 给 worker 的 Docker `--env-file` 或 `--env` 中放 DSN；技能自己用 `--env-file` 读取该文件。
- 不要自动搜索其他项目的 `.env`，也不要把真实连接串写进 `config.yaml`、SKILL.md 或任何会分发的文件。
- 连接串拒绝 URL 参数、片段、Unix socket 形态与库名错位；驱动固定为 asyncmy。

### 多人 Docker 的 env 文件与 secrets 布局

`database-query.env` 每行一个 `KEY=VALUE`，值与 `tunnel.multiuser.yaml` 的 forward 端口一一对应：

```text
YUANMENG_MALL_TRACKING_DATABASE_URL=mysql+asyncmy://readonly:<百分号编码口令>@db-tunnel:13306/ymkj2.0
YUANMENG_SPACE_DATABASE_URL=mysql+asyncmy://readonly:<百分号编码口令>@db-tunnel:13307/meta_new_sync
COLLECTIBLES_DATABASE_URL=mysql+asyncmy://readonly:<百分号编码口令>@db-tunnel:13308/szcp_sync
```

- 口令中的 `@`、`:`、`/`、`?`、`#`、`%` 必须按 URL 规则百分号编码；DSN 不接受 URL 参数与片段。
- `ymkj2.0` 是完整库名（不是 `ymkj2` + `0`）；`meta_new_sync`、`szcp_sync` 同样以 `project-docs` 业务文档为准。
- 端口写错不会在默认 `doctor` 中暴露，只有 `doctor --connect` 或首次查询才会失败。

宿主 secrets 布局：

```text
/srv/easel/secrets/database-query/
├── database-query.env          # DSN，只挂 worker
└── keys/
    ├── database-query_ed25519  # 无口令私钥，只挂 sidecar
    └── known_hosts             # 跳板机固定 host key
```

权限（Linux 宿主）：

```bash
sudo install -d -o root   -g 10001 -m 0750 /srv/easel/secrets/database-query
sudo install -d -o root   -g 10001 -m 0750 /srv/easel/secrets/database-query/keys
sudo install -o 10001 -g 10001 -m 0400 <私钥>        /srv/easel/secrets/database-query/keys/database-query_ed25519
sudo install -o root  -g 10001 -m 0440 <known_hosts> /srv/easel/secrets/database-query/keys/known_hosts
sudo install -o 10001 -g 10001 -m 0400 <env 文件>    /srv/easel/secrets/database-query/database-query.env
```

私钥必须无口令、由 UID 10001 拥有且组/其他用户不可读写，否则 OpenSSH 在 `BatchMode=yes` 下拒绝使用。跳板机账号应专用，并在 `authorized_keys` 中限制为端口转发：

```text
command="",no-pty,no-agent-forwarding,no-x11-forwarding,permitopen="<db_host>:3306" ssh-ed25519 AAAA...
```

## 4. 自检

在项目根执行：

```powershell
python skills/builtin/database-query/scripts/db_query.py --config skills/builtin/database-query/config.yaml doctor
python skills/builtin/database-query/scripts/db_query.py --config skills/builtin/database-query/config.yaml doctor --connect --project <精确项目 ID>
```

默认 `doctor` 只检查本地配置、依赖、项目目录、绑定、数据源状态与目录可写性；`sources` 检查只展示启用/验证标记，`ok: true` **不代表**凭据已配置或可用于查询，实际连通性必须用 `--connect`。`--connect` 才会建立真实连接，并核验实际库名与 `allowed_schema` 一致。多人 Docker 使用第 2 节的固定命令（`config.multiuser.yaml` + `/run/easel-secrets/database-query.env`），逐 source 验收时再加 `--connect --project <精确项目 ID>`。

## 5. SSH 隧道

多人 Docker 不使用桌面手工隧道：provisioner 为每个 workspace 启动 sidecar，按镜像内固定的 `tunnel.multiuser.yaml` 在 `db-tunnel` 别名下常驻转发。不要在 worker 内建隧道，也不要把私钥放进 workspace；私钥只在 sidecar 的只读挂载中。

桌面/本机部署仍由维护者手工建立隧道。技能不会自动启动、监督隧道或管理私钥。维护者在本机建立隧道后，把 `secret_env` 指向本机转发端点（例如 `127.0.0.1` 的某个端口）。建议的本机 OpenSSH 参数：

```text
Host db-tunnel
    HostName <跳板机地址>
    User <跳板机账号>
    IdentityFile <仅本人可读的私钥>
    LocalForward 127.0.0.1:<本地端口> <数据库主机>:3306
    ExitOnForwardFailure yes
    ServerAliveInterval 30
    ServerAliveCountMax 3
    StrictHostKeyChecking yes
    UserKnownHostsFile <固定 known_hosts 文件>
    RequestTTY no
    RemoteCommand none
```

只监听 `127.0.0.1`，不要绑定 `0.0.0.0`。原平台的 Compose 服务名（例如 `mall-tracking-tunnel`）只在容器网络内有效，宿主机必须使用实际本地转发端点，不能照抄。

## 6. 私有文件

以下内容不得提交或打包进分发副本：`config.yaml`、`config/database-query.yaml`、`.env`、`state/`、`outputs/`、SSH 私钥、真实业务数据、结果文件与 `.secrets/`。技能目录的 `.gitignore` 已覆盖常见路径。

多人 Docker 另有：

```text
assets/tunnel.multiuser.yaml          # 真实拓扑，不入库（技能目录内）
/srv/easel/secrets/database-query/    # DSN、私钥与 known_hosts
```

`tunnel.multiuser.yaml` 必须进入构建上下文（否则 sidecar 记录 `config_invalid` 且不启动任何 forward），但不得提交；宿主 secrets 目录不进入镜像、不进交接文件、不写日志。
