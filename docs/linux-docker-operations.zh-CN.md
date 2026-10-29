# Easel Linux Docker 部署、使用、升级与恢复手册

适合读者：能运行 Python，但第一次维护 Linux/Docker 服务的开发者。核对基线：`04b5f23e36879e72fbafa345dc2ed06abd26a5ae`（A7；A7-R1 修订见交付说明），2026-10-29。Windows 操作见[Windows 手册](windows-desktop-operations.zh-CN.md)。

## 1. 本文提供哪两种部署

**先读结论：** 单人 local 工作台和完整 Desktop 拓扑都已在代码中装配；完整拓扑已在 Windows 11 + Docker Desktop + WSL2 的专用测试部署实际演练初始化/启停/聊天/权限/停止/备份/新目录恢复/升级/回滚。**本文不把该结果冒充 Linux 目标宿主验收**：Linux 上的 XFS 配额、iptables/nftables、IPv6、Chromium 沙箱和真实供应商仍未在目标机执行。

| 部署 | 可以做什么 | 本文定位 |
|---|---|---|
| 单人 Docker 工作台 | 自己使用 Web、配置 API；安装好 Linux Harness 后可用对应 Agent | 第 2–7 节，可先跑通 |
| 多人 control 管理面 | HTTPS 邀请登录、成员/邀请、默认/个人模型配置 | 第 8 节；只登录/配置的最小路线 |
| 完整 Desktop control/provisioner/worker/sidecar/gateway/broker | 每用户独立运行、受限出口、真实 Harness 聊天、停止收尾、完整备份/恢复/升级 | 第 9–13 节；代码已装配，Linux 目标门槛见 13 节 |

为什么要区分：

- `web` 是单人 local 模式，没有多用户登录，不应直接对公网开放。
- `serve-control` 是多人管理/授权边界，使用 Secure Cookie，公共地址必须是 HTTPS。
- 完整 Desktop 已由 A4–A7 装配：control 传入签名私钥/受管 Agent 配置，provisioner 启动真实 worker，gateway 逐请求签名，worker 只读公钥验签，broker 逐 Run 发 token。
- 代码装配完成不等于目标 Linux 主机通过：XFS/prjquota、宿主 firewall、IPv6、Chromium 沙箱等仍需在目标机验证；检查器的固定 blocked 证据项不能被本机 Docker Desktop 结果替代。
- 真实 DeepSeek 供应商请求仍为 **not-run**；受控 fake provider 成功不能写成真实供应商成功。

本文不会伪造缺失的生产入口，也不会把“命令可复制”写成“已在你的服务器执行”。

## 2. 准备 Linux 主机和代码

### 2.1 约定

以下示例使用 Ubuntu 24.04 LTS、Bash、Linux Docker Engine。普通命令在 Linux SSH 终端执行；`sudo` 表示需要管理员权限。不要把 PowerShell 的反引号续行和 Bash 的反斜杠续行混用。

镜像是程序安装包，容器是正在运行的进程环境，挂载目录才是需要长期保存的数据。`compose up -d` 创建/更新运行实例，`stop` 只停进程；不要把删容器当成数据备份。HTTPS 代理是浏览器访问服务的入口，provisioner 则是有权限创建 worker 的宿主组件，两者不是同一个服务。

推荐目录：

```text
/srv/easel/
  src/版本号/       代码版本，每个版本一个目录
  ops/              本文的 Dockerfile、Compose 和镜像记录
  local/            单人模式 outputs/profiles/home/apps（apps 内含 .env）
  control/          多人 control.sqlite3
  keys/             master key，不能进 Git
  workspaces/       将来多人持久区；正式环境要求 XFS 项目配额
  backups/          备份，不能只保留在同一故障磁盘
  restore/          恢复演练的新目录
```

单人模式不要求 XFS；多人正式部署才有该隔离门槛。下文不提供 `mkfs` 或自动格式化磁盘命令，不能拿已有数据盘试错。

### 2.2 安装或检查 Docker

已有 Docker 时跳过安装，只检查：

```bash
sudo docker version
sudo docker compose version
sudo docker info --format '{{.OSType}}'
```

应看到 Server 和 `linux`。新 Ubuntu 主机可以按 [Docker 官方 apt 仓库安装说明](https://docs.docker.com/engine/install/ubuntu/)执行。下面是对应的新机命令，不应覆盖已有企业包源配置：

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

不是 Ubuntu 的主机使用对应发行版官方步骤。本文一直用 `sudo docker`，不要求把初级操作员加入等同于宿主高权限的 docker 组。Docker 发布端口与 UFW 的关系见官方文档，不要以为打开 UFW 就自动隔离了容器端口。

### 2.3 把已验收的代码送到服务器

不要假设公开仓库 main 已含你的功能分支。可以在 **Windows PowerShell** 导出当前验收代码，不带未跟踪的密钥/个人数据：

```powershell
Set-Location 'C:\Code\Easel'
$Revision = '290d94a68b9a4c1e8e3f627b5c96fcbdb8f11f1b'
$Archive = "C:\Code\easel-$Revision.tar"
git archive --format=tar --output=$Archive $Revision
if ($LASTEXITCODE -ne 0) { throw '源码归档失败' }
Get-FileHash -Algorithm SHA256 $Archive
# 把下面的服务器账户与地址替换成你自己的
scp $Archive operator@YOUR_SERVER:/tmp/
```

`git archive` 是源码包，不包含未跟踪文件、构建后的 dist、模型 Key 或 Harness HOME；前端会在镜像里重新构建。两份新手手册若尚未提交，应另外复制留档。

在 **Linux Bash** 解包到新版本目录：

```bash
REV=290d94a68b9a4c1e8e3f627b5c96fcbdb8f11f1b
SRC=/srv/easel/src/$REV
OPS=/srv/easel/ops
sudo install -d -m 0755 "$SRC" "$OPS"
sha256sum "/tmp/easel-$REV.tar"
sudo tar -xf "/tmp/easel-$REV.tar" -C "$SRC"
test -f "$SRC/pyproject.toml" && test -f "$SRC/web/app.py"
```

核对 SHA-256 与发送端一致。解包目录必须是本次新建的代码目录；不要解到 `/` 或旧数据目录。后续新终端需要重新设置 `REV/SRC/OPS`。

## 3. 构建单人可用的 Docker 镜像

### 3.1 新建 Dockerfile.local

```bash
sudo tee "$OPS/Dockerfile.local" >/dev/null <<'EOF'
FROM node:24-bookworm-slim AS frontend
WORKDIR /build
COPY web/frontend/package*.json ./
RUN npm ci
COPY web/frontend/ ./
RUN npm run build

FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 EASEL_SERVICE_MODE=local HOME=/home/easel PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
WORKDIR /opt/easel
COPY --from=frontend /usr/local/bin/node /usr/local/bin/node
COPY . .
RUN apt-get update && apt-get install -y --no-install-recommends git ffmpeg ca-certificates libstdc++6 libatomic1 libgl1 libglib2.0-0 libgomp1 \
 && python -m pip install --no-cache-dir -e . \
 && python -m playwright install --with-deps chromium \
 && chmod -R a+rX /opt/playwright \
 && rm -rf /var/lib/apt/lists/*
COPY --from=frontend /build/dist ./web/frontend/dist
RUN groupadd -g 10001 easel && useradd -u 10001 -g 10001 -m -d /home/easel easel \
 && mkdir -p outputs profiles && touch .env \
 && chown -R 10001:10001 /opt/easel /home/easel
USER 10001:10001
EXPOSE 7860
CMD ["sh", "-c", "umask 077; exec python -m easel web --port 7860"]
EOF

sudo docker build -f "$OPS/Dockerfile.local" -t "easel-local:$REV" "$SRC" || { echo '构建失败，不继续'; exit 1; }
LOCAL_IMAGE=$(sudo docker image inspect "easel-local:$REV" --format '{{.Id}}') || exit 1
printf '%s\n' "$LOCAL_IMAGE"
```

构建失败就先解决最后的安装/网络错误，不继续启动。构建涉及 Python 媒体依赖和 Chromium，耗时可能较长；这里没有声称已在你的服务器构建成功。基础镜像 tag 会变化，升级时应保存实际镜像 ID、源码 SHA 和构建日志，不能只记录 `latest`。

浏览器固定在 /opt/playwright，不被持久 home/app 挂载遮蔽；umask 077 保护新凭证。这个镜像含本地工作台、Node、FFmpeg 和浏览器，默认没有安装 Harness 或其它 Agent CLI。先选“仅聊天模式”确认服务正常；不要把 local 镜像误当成经过多人安全验收的 worker 镜像。

### 3.2 持久目录和权限

```bash
DATA=/srv/easel/local
sudo install -d -o 10001 -g 10001 -m 0750 \
  "$DATA/outputs" "$DATA/profiles" "$DATA/home" "$DATA/apps"
APP_ROOT="$DATA/apps/$REV"
test ! -e "$APP_ROOT" || { echo '版本应用目录已存在，先核对，不覆盖'; exit 1; }
sudo install -d -o 10001 -g 10001 -m 0750 "$APP_ROOT"
SEED="easel-local-seed-$(date +%s)-$$"
sudo docker create --name "$SEED" "$LOCAL_IMAGE" >/dev/null || exit 1
sudo docker cp -a "$SEED:/opt/easel/." "$APP_ROOT" || { echo '应用复制失败，保留seed容器供排查'; exit 1; }
sudo docker rm "$SEED" >/dev/null
```

Linux 容器使用 UID/GID 10001。`docker cp -a` 保留镜像内应用文件属主；复制失败先停在这里检查，不删除其它容器。以上 seed 是本次未启动的专用容器，不会运行 Easel。

必须挂载完整应用目录，是因为网页用临时文件 + rename 原子保存 `/opt/easel/.env`；把单个 `.env` bind mount 到该位置会导致保存失败。`APP_ROOT/.env` 是应用凭证配置，下面 Compose 的 `local.env` 只记录镜像 ID 和目录。若报 Permission denied，检查 UID 10001，不使用 777。这是单人可信环境的可写工作目录，不是多人只读代码根。

### 3.3 Compose 文件

```bash
sudo tee "$OPS/compose.local.yaml" >/dev/null <<'EOF'
name: easel-local
services:
  easel:
    image: ${EASEL_LOCAL_IMAGE:?请设置镜像ID}
    pull_policy: never
    ports:
      - "127.0.0.1:7860:7860"
    volumes:
      - ${EASEL_APP_ROOT}:/opt/easel
      - ${EASEL_DATA_ROOT}/outputs:/opt/easel/outputs
      - ${EASEL_DATA_ROOT}/profiles:/opt/easel/profiles
      - ${EASEL_DATA_ROOT}/home:/home/easel
    restart: unless-stopped
EOF
printf 'EASEL_LOCAL_IMAGE=%s\nEASEL_DATA_ROOT=%s\nEASEL_APP_ROOT=%s\n' "$LOCAL_IMAGE" "$DATA" "$APP_ROOT" |
  sudo tee "$OPS/local.env" >/dev/null
```

定义一个只针对这套服务的短命令；新终端重做这两行即可：

```bash
OPS=/srv/easel/ops
easel_local() { sudo docker compose --env-file "$OPS/local.env" -f "$OPS/compose.local.yaml" "$@"; }
easel_local config --quiet
easel_local up -d
easel_local ps
easel_local logs --tail 80
curl -I http://127.0.0.1:7860/
```

`up -d` 在后台启动；`ps` 应为 running，HTTP 应返回页面响应。`restart: unless-stopped` 让 Docker 重启后恢复服务，但你手动 stop 的服务不会因此自动恢复。[Compose 官方行为](https://docs.docker.com/reference/cli/docker/compose/up/)说明更新镜像后 `up` 会重建容器并保留挂载数据；是否挂载正确仍由你的配置决定。

## 4. 第一次访问和使用

### 4.1 从自己的 Windows 电脑访问

单人 Web 没有多人认证，因此不开放 7860 公网端口。在 **Windows PowerShell** 建立 SSH 隧道：

```powershell
ssh -N -L 7860:127.0.0.1:7860 operator@YOUR_SERVER
```

保持终端开着，在电脑浏览器打开 <http://127.0.0.1:7860>。这是服务器的服务经 SSH 转发过来；你的电脑如果已占用 7860，可把最左边改成 `7870`，然后访问本机 7870。

### 4.2 先跑通一条最简单的请求

1. 设置 → 执行引擎选“仅聊天模式”，填写供应商地址、模型 ID 和 API Key；保存。
2. 新建会话，发送简单问题，确认收到实际回复。
3. 为图片/语音/视频/音乐分别配置所需通道。留空或未支持的供应商不代表已经连通。
4. 只有需要本地工具/技能时才继续接入 Agent。仅聊天模式不能执行文件、浏览器或技能。
5. 平台发布先用测试账号和草稿；无桌面容器不保证所有需要可见浏览器的登录流程可用。

### 4.3 接入 Linux 版 DeepSeek Harness/ACP

Windows 的 `C:\...` argv 不能直接用于 Linux。必须先获得**在 Linux 构建并验证过**的 Harness，包含 `apps/cli/lib/bin.js` 和它需要的 Linux 依赖；不要直接使用 Windows 的 node_modules。

可在 Compose 的单人服务增加一个只读挂载（示例宿主路径需要真实存在）：

```yaml
      - /srv/harness/deepseek-harness:/opt/harness:ro
```

然后在宿主 `DATA/outputs/_harness/acp.json` 中配置：

```json
{
  "command": ["/usr/local/bin/node", "/opt/harness/apps/cli/lib/bin.js", "--profile", "acp"]
}
```

文件归属应允许 UID 10001 读取。原生认证按 Harness 的方式配置在**本文专用持久 home**，不要挂整个宿主 `/home`。网页选择通用 ACP、保存后新建会话，验证连续追问与一个无外部副作用的文件任务。详细契约见[Agent 接入文档](agent-harnesses.md)。

本手册不把“文件存在”当成已完成 Harness 构建/认证。你在 Windows 已成功的 Harness 可以继续用 Windows 本机路线；Linux 构建和模型连通需要在实际容器另外验收。

## 5. 日常启停、日志和空间

```bash
easel_local ps
easel_local logs --tail 100
easel_local logs -f --tail 50
easel_local restart easel
easel_local stop easel
easel_local start easel
sudo docker system df
df -h /srv/easel
du -sh /srv/easel/local/*
```

看日志时按 Ctrl+C 只退出日志查看。日志分享前清理 Key、Cookie、邀请和消息正文。不要对整台 Docker 主机执行盲目 prune；不要把删用户 home/outputs 当成释放容器空间的方法。

## 6. 单人模式备份、恢复和升级

### 6.1 冷备份

等任务结束，停服务后再打包。备份包含 `.env`、浏览器/原生凭证，必须私密保存：

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
sudo install -d -m 0700 /srv/easel/backups
easel_local stop easel
sudo tar --numeric-owner -czpf "/srv/easel/backups/local-$STAMP.tar.gz" -C /srv/easel local || { echo '备份失败，先不要升级'; exit 1; }
sudo chmod 0600 "/srv/easel/backups/local-$STAMP.tar.gz"
sudo sha256sum "/srv/easel/backups/local-$STAMP.tar.gz"
easel_local start easel
```

如果 tar 失败，先排查，不删除源数据。副本还应加密复制到另一故障域；同盘备份防不了整盘损坏。不要只备份 outputs 忽略 home 和配置，也不要以此冒充多人一致性备份。

### 6.2 恢复到新目录演练

用真实备份时间替换变量，先查看清单确认它是你生成的可信归档：

```bash
STAMP=替换为备份时间
sudo tar -tzf "/srv/easel/backups/local-$STAMP.tar.gz" | head -30
sudo install -d -o 10001 -g 10001 -m 0750 "/srv/easel/restore/local-$STAMP"
sudo tar --numeric-owner -xzpf "/srv/easel/backups/local-$STAMP.tar.gz" -C "/srv/easel/restore/local-$STAMP"
```

复制 Compose 为演练文件，换项目名、宿主端口；`EASEL_DATA_ROOT` 指向恢复出的 local，`EASEL_APP_ROOT` 也要指向该副本内 apps/对应版本。不要在原服务还使用数据时覆盖它。确认模型配置、文件、原生历史可读后，再计划切换。浏览器 local 模式的展示记录不等于全在服务端，换浏览器可能需要单独处理站点存储。

### 6.3 版本升级

1. 记录旧源码 SHA、`local.env` 里的镜像 ID、Compose 和 Harness 版本，执行冷备份。
2. 用新源码归档解到新目录，按第 3.1 节构建新 tag；不要覆盖旧数据。
3. 保存旧镜像/应用目录记录，从新镜像提取**新的应用目录**；不能只换镜像却继续挂着旧代码。以下先把 NEW_IMAGE/NEW_REV 改为实际新版本，APP_ROOT 指向当前旧应用目录：

```bash
sudo cp "$OPS/local.env" "$OPS/local.env.before-upgrade"
APP_ROOT=$(sudo sed -n 's/^EASEL_APP_ROOT=//p' "$OPS/local.env")
case "$APP_ROOT" in /srv/easel/local/apps/*) ;; *) echo '旧应用目录配置不符，先核对'; exit 1;; esac
# 确认此变量来自新镜像的 docker image inspect
NEW_IMAGE=替换为新镜像sha256值
case "$NEW_IMAGE" in sha256:*) ;; *) echo '先填写新镜像ID'; exit 1;; esac
NEW_REV=替换为新代码完整SHA
case "$NEW_REV" in *替换*) echo '先填写新版本'; exit 1;; esac
NEW_APP="/srv/easel/local/apps/$NEW_REV"
test ! -e "$NEW_APP" || { echo '新应用目录已存在，先核对'; exit 1; }
sudo install -d -o 10001 -g 10001 -m 0750 "$NEW_APP"
SEED="easel-local-upgrade-$(date +%s)-$$"
sudo docker create --name "$SEED" "$NEW_IMAGE" >/dev/null || exit 1
sudo docker cp -a "$SEED:/opt/easel/." "$NEW_APP" || { echo '新应用复制失败，先排查'; exit 1; }
sudo docker rm "$SEED" >/dev/null
sudo cp -p "$APP_ROOT/.env" "$NEW_APP/.env" || exit 1
printf 'EASEL_LOCAL_IMAGE=%s\nEASEL_DATA_ROOT=/srv/easel/local\nEASEL_APP_ROOT=%s\n' "$NEW_IMAGE" "$NEW_APP" |
  sudo tee "$OPS/local.env" >/dev/null
easel_local up -d
easel_local logs --tail 100
```

4. 实测新会话、连续追问和文件输出。不要自动重发升级前状态不明的 prompt。
5. 回滚时 stop 服务、恢复旧 `local.env`（同时恢复旧镜像和旧 appRoot），再 `up -d`；有不兼容数据变更时使用升级前的数据副本。旧程序可能不能读取新 schema，不能只降镜像继续读已升级的数据。

应用目录里的旧格式平台配置也随 apps 归档，但不保证新版本自动迁移。逐项核对必要状态，可能需要重新登录；不要把整棵旧代码复制覆盖新代码。

## 7. 单人模式常见故障

| 现象 | 排查顺序 |
|---|---|
| 浏览器连不上 | `easel_local ps` → 宿主 curl → SSH 隧道 → 本机端口是否占用 |
| 容器启动后退出 | `logs --tail 100`，检查依赖、绑定文件是不是误建成目录 |
| .env/outputs 无法写入 | `ls -ld`、`ls -l` 核对 UID 10001；不放宽到 777 |
| 镜像构建很慢/失败 | 核对网络、磁盘和构建日志；首次媒体依赖大，失败不应继续启动 |
| ACP 报找不到 JS | 检查挂载、Linux 路径及文件权限，不填写 Windows 路径 |
| 页面能用但工具不能执行 | 仅聊天模式没有工具；确认底座已安装且完成原生认证 |
| 浏览器沙箱错误 | 多人环境不得用 --no-sandbox/privileged 绕过；local 成功也不算多人沙箱证据 |

## 8. 多人控制面：只部署已装配的部分

本节可用于受控测试环境里验证邀请登录、管理员和模型设置。**聊天/产物完整链路仍缺装配，不应给正式用户宣称可用。** 使用不同项目/端口和目录，不与单人数据混用。

### 8.1 建库目录和 master key

下面复用第 3 节的镜像作为携带代码/前端的 control 镜像，通过 command 覆盖启动入口；这不是拿 local 模式替代多人授权。

新开终端后先设置 OPS，再从已有记录读取镜像，不用重新 build：

```bash
OPS=/srv/easel/ops
LOCAL_IMAGE=$(sudo sed -n 's/^EASEL_LOCAL_IMAGE=//p' "$OPS/local.env")
case "$LOCAL_IMAGE" in sha256:*) ;; *) echo '先核对镜像记录'; exit 1;; esac
```

```bash
sudo install -d -o 10001 -g 10001 -m 0750 /srv/easel/control /srv/easel/keys /srv/easel/backups
sudo docker run --rm --user 10001:10001 \
  --mount type=bind,src=/srv/easel/keys,dst=/keys \
  "$LOCAL_IMAGE" python -c 'from pathlib import Path; import secrets,os; p=Path("/keys/master.key"); f=p.open("xb"); f.write(secrets.token_bytes(32)); f.close(); os.chmod(p,0o600)'
```

`xb` 表示文件已存在时拒绝覆盖。重启/升级不要重跑生成命令，不要把 master key 当成一次性登录 token；丢失它会导致已有模型凭证无法解密。已存在时只检查权限和长度，不打印内容。

### 8.2 control-only Compose

在 `$OPS/compose.control.yaml` 写入：

```yaml
name: easel-control-admin
services:
  control:
    image: ${EASEL_LOCAL_IMAGE:?请设置镜像ID}
    pull_policy: never
    user: "10001:10001"
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    tmpfs: ["/tmp:rw,noexec,nosuid,nodev,size=64m"]
    command:
      - python
      - -m
      - easel
      - serve-control
      - --control-dir
      - /srv/easel/control
      - --public-origin
      - ${EASEL_PUBLIC_ORIGIN:?请设置HTTPS域名}
      - --master-key-file
      - /run/secrets/master.key
      - --frontend-dir
      - /opt/easel/web/frontend/dist
      - --host
      - 0.0.0.0
      - --port
      - "7861"
    volumes:
      - /srv/easel/control:/srv/easel/control
      - /srv/easel/keys/master.key:/run/secrets/master.key:ro
      - /srv/easel/backups:/srv/easel/backups
    ports: ["127.0.0.1:7861:7861"]
    restart: unless-stopped
```

准备 `$OPS/control.env`，填写真实 HTTPS Origin（只含协议、域名、可选端口，不含业务路径）：

```bash
printf 'EASEL_LOCAL_IMAGE=%s\nEASEL_PUBLIC_ORIGIN=https://easel.example.com\n' "$LOCAL_IMAGE" |
  sudo tee "$OPS/control.env" >/dev/null
easel_control() { sudo docker compose --env-file "$OPS/control.env" -f "$OPS/compose.control.yaml" "$@"; }
easel_control config --quiet
easel_control up -d
curl -f http://127.0.0.1:7861/api/health
```

网页访问必须走下节的 HTTPS 域名；内部 curl 用 HTTP 只测健康，不代表登录 Cookie 可在 HTTP 页面正常使用。没有签名/受管配置和运行时，聊天等入口 503 是本示例预期结果。

### 8.3 TLS 反向代理示例

以下用**宿主机 Caddy**；先按 [Caddy 官方安装说明](https://caddyserver.com/docs/install)安装。DNS 指向这台主机，80/443 的访问策略和证书签发由你控制；受控测试仍应限制访问来源。不要把 7861 直接对公网发布。

将真实域名代入 `/etc/caddy/Caddyfile`，不要覆盖主机已有其它站点配置：

```caddyfile
easel.example.com {
    reverse_proxy 127.0.0.1:7861 {
        flush_interval -1
    }
}
```

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl -f https://easel.example.com/api/health
```

`flush_interval -1` 用于及时转发流式内容；不要在代理层缓存聊天事件。HTTPS 域名必须与 control.env 中完全一致。服务显式不信任转发来源头，因此代理后登录限速可能按代理地址共享额度，不要擅自打开对所有代理头的信任。

### 8.4 首次管理员和成员

```bash
easel_control exec -T control python -m easel admin bootstrap \
  --control-dir /srv/easel/control --name Owner
```

首次只引导一个管理员，记下输出中的 admin_user_id、workspace_id 和**一次性邀请**。打开 HTTPS 首页，把邀请粘进登录表单。不要写进 URL、截图或共享日志。

进入管理员设置：创建 member → 为该成员签发邀请 → 通过可信方式交给本人 → 用另一个浏览器 profile 登录验证。成员只能修改自己的能力覆盖，管理员配置受管底座和默认模型；没有个人覆盖时继承管理员默认，清除覆盖恢复继承。

已有成员需重新邀请时：

```bash
easel_control exec -T control python -m easel admin invite \
  --control-dir /srv/easel/control \
  --admin-user-id 替换为管理员ID --user-id 替换为成员ID \
  --ttl-seconds 604800
```

邀请已兑换就失效；丢失邀请需重新签发。禁用成员会撤销旧登录和未兑换邀请，重新启用不会复活旧令牌；管理员页面可撤销成员登录。master key 不支持直接替换热轮换，不能通过生成新 key 修复解密错误。

本节验收标准只是：HTTPS 正常、管理员/成员登录、权限区别和配置保存正确。**看到首页或创建成员成功，不等于多人聊天已部署。**

## 9. 完整多人 Docker 拓扑：已装配什么，Linux 目标还缺什么

### 9.1 当前代码装配与目标机门槛

| 部分 | 当前代码/Windows 测试事实 | Linux 目标仍需 |
|---|---|---|
| control 任务签名 | 正式 Compose 传入 `--snapshot-key-file` 与 `--managed-agent-config`；gateway 逐请求 Ed25519 断言 | 目标机镜像/挂载/权限实测 |
| worker 任务入口 | provisioner 启动真实 worker，暴露 `/internal/runs`、events、stop、questions | 目标机 XFS/权限/锁语义 |
| worker 验签公钥 | provisioner 只读挂载公钥，与 control 私钥配套 | 目标机只读挂载与身份核验 |
| Harness 与 broker | 固定 A5/A6 真实 ACP Harness + 每 Run sidecar broker；受控 fake provider 已跑通 | 真实供应商受管 channel（仍 not-run） |
| 产物等 control 路由 | 既有路由按正式授权边界运行 | 目标机反向代理/TLS/防火墙 |
| 正式隔离 | Windows 测试宿主已验证网络/权限/停止收尾；生产 Linux 门槛未通过 | XFS 配额、iptables/nftables、IPv6、Chromium 沙箱 |

代码装配已完成，不是靠运维人员补一个端口或改 JSON。不要绕过签名校验、关闭沙箱或改 local 模式冒充完整拓扑。

### 9.2 密钥、公共配置和最终镜像

签名私钥用 Ed25519；与 32 字节 master key 用途不同。公钥可以进入受管只读镜像，私钥不可以。可在受保护目录通过 `cryptography` 生成，私钥文件必须独占创建并设 0600；具体加载接口为 [snapshot.py](../easel/control/snapshot.py)。

受管 Agent JSON 的**形状示例**如下，它不是现成可运行的 DSH profile：

```json
{
  "revision": 1,
  "agent": {
    "runtime": "acp",
    "command": ["/usr/bin/node", "/opt/dsh/apps/cli/lib/bin.js", "--profile", "acp"],
    "sandbox": "workspace-write",
    "timeout": 900
  },
  "broker": {"url": "http://受管worker可达的broker入口:8899", "channel": "chat"}
}
```

命令文件、Node 版本、真实 profile 结构、逐 workspace broker 地址装配均需按最终镜像验证。受管 JSON 不放供应商 Key；供应商 Key 只在 control 管理并加密存储。Harness 不会因为存在 EASEL_BROKER_URL 就必然支持所有调用协议。

最终镜像的构建入口（前提未满足会失败，不能拿 execution 镜像替代）：

```bash
DSH_COMMIT=填已经验收的完整Harness提交
test -f "$SRC/deploy/multiuser/managed/acp-profile.json" || { echo '缺少已验收的公共ACP profile'; exit 1; }
sudo docker build -f "$SRC/deploy/multiuser/Dockerfile.worker" \
  --build-arg "DSH_COMMIT=$DSH_COMMIT" -t easel-worker:validated "$SRC"
WORKER_IMAGE=$(sudo docker image inspect easel-worker:validated --format '{{.Id}}')
```

源码 checkout 不等于 CLI 已构建。当前 Dockerfile 对 Node/pnpm/构建产物的实际兼容仍需核对，不应把构建期检查到某个文件当成真实 ACP 调用通过。已有本机 Harness commit 候选不等于管理员正式验收。

### 9.3 宿主 provisioner 与受限出口

下列为**补齐上述装配后**使用的原有命令参考。provisioner 必须在宿主运行，不能把 Docker socket 挂进 control/worker：

```bash
findmnt -no FSTYPE,OPTIONS --target /srv/easel/workspaces
command -v xfs_quota
sudo iptables -S DOCKER-USER
sudo ip6tables -S DOCKER-USER
```

应确认真实 XFS 和 prjquota/pquota；这些只读检查不能替代限额建立与耗尽测试。不要使用 `--quota-mode none --allow-unquotaed` 部署正式多人服务。

先为宿主维护命令准备独立 Python 环境（只有准备继续多人装配时才需要，单人 Docker 不需要）：

```bash
sudo apt-get install -y python3-venv
HOSTPY=/srv/easel/ops/host-venv/bin/python
sudo python3 -m venv /srv/easel/ops/host-venv
sudo "$HOSTPY" -m pip install -e "$SRC"
command -v setpriv
sudo install -d -o root -g 10001 -m 0750 /run/easel
```

这里的 Python 依赖安装到专用 venv，不污染系统 Python。UID/GID 10001 若已被主机其它业务使用，先调整部署设计，不覆盖别人的账户或目录权限。

参考启动命令（前台运行，应在专用 SSH 会话或经审核的服务管理器中运行）：

```bash
sudo setpriv --reuid=0 --regid=10001 --clear-groups "$HOSTPY" "$SRC/deploy/multiuser/provisioner.py" \
  --socket /run/easel/provisioner.sock \
  --workspace-root /srv/easel/workspaces \
  --image "$WORKER_IMAGE" --allowed-uid 10001 \
  --quota-mode project --quota-bytes 10737418240 \
  --sidecar-network easel-control \
  --sidecar-policy /etc/easel/egress-policy.json \
  --broker-upstream control:7861
```

此处的 Python 是**宿主专门安装该版本 Easel 的环境**，不是容器里的 Python。provisioner 以 root 进行 Docker/目录/配额操作，主组为 10001，使重建的 socket 组权限与 control 匹配；control 仍为 UID 10001，不能跟着改成 root。正式环境应把该固定命令交给专用受限服务管理器，不能把可任意执行命令的 root 入口开放给用户。

socket 还要有正确的文件系统权限：父目录 0750、socket 0660，并让其 group 与 control 的 10001 GID 一致。`--allowed-uid` 是连接后的身份授权，不会自动赋予文件读写权限；socket 每次重建后都要检查属组。不能用 chmod 777 解决。

出口策略例子：

```json
{"approved_hosts": [], "approved_cidrs": []}
```

空例外表仍允许代码默认校验通过的公网 HTTPS:443，并非“完全断网”或域名白名单。不要把整个私网/管理网段放入 approved_cidrs。详细策略及宿主防火墙矩阵见[网络边界说明](../deploy/multiuser/egress-policy.md)。

参考 Compose 会把 control 与 egress 接入 easel-control 网络，启动 egress 需要显式 `--profile egress`。必须先有网络和 provisioner socket，再创建 control；挂载单个 socket 文件时，provisioner 重建 socket 后通常还需要重建 control 的挂载。至少还要用 override 补上前端/签名/受管配置，不能原样一条 `up` 当成完整交付。

## 10. 多人控制库备份、恢复与升级

### 10.1 control-only 的备份

本节只针对第 8 节没有活跃 worker 的管理面。先停止接入和 control；单次 run 不发布服务端口：

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
easel_control stop control
easel_control run --rm --no-deps control python -m easel admin backup \
  --control-dir /srv/easel/control \
  --master-key-file /run/secrets/master.key \
  --destination "/srv/easel/backups/control-$STAMP"
easel_control up -d control
```

结果包含控制库、key、清单，不含未指定的 workspace 文件。不能把 control-only 备份说成完整多人备份。

### 10.2 已补齐完整多人环境后的维护顺序

先在代理阻止**新的任务创建**，保留停止、权限回答和 SSE/回放，让 control 继续对在途任务收尾。等 Run 终态后，通过备份命令 drain；确认 worker stopped，再停 control/provisioner 升级。不要一上来停 control 导致后台状态无人对账，再误认为数据库里已无活跃任务。

完整备份命令在能读取宿主控制/数据根且能访问 provisioner socket 的**专用管理员环境**执行；正常 control 不需要挂所有 workspace 数据根：

```bash
sudo setpriv --reuid=10001 --regid=10001 --clear-groups "$HOSTPY" -m easel admin backup \
  --control-dir /srv/easel/control \
  --workspace-root /srv/easel/workspaces \
  --master-key-file /srv/easel/keys/master.key \
  --destination "/srv/easel/backups/full-$STAMP" \
  --image-digest "$WORKER_IMAGE" \
  --harness-revision "$DSH_COMMIT" \
  --provisioner-socket /run/easel/provisioner.sock
```

备份不覆盖旧目录；drain 超时/中间态必须停止维护步骤。备份排除运行时 `.credentials`，但保留用户 Git 历史。普通链接/特殊文件会明确拒绝，需要按手册处理，不静默裁剪重要数据。

此 helper 以与 control 相同的 UID/GID 连接 socket，生成的备份也归 10001 所有；确认备份父目录和所有必要数据可被该 UID 访问。不要改成未授权 UID 后再为解决 unauthorized 放宽 socket 到所有用户。

### 10.3 恢复演练

以第 8 节镜像执行恢复 CLI，源只读，目标为新目录：

```bash
sudo install -d -o 10001 -g 10001 -m 0750 /srv/easel/restore
sudo docker run --rm --user 10001:10001 \
  --mount type=bind,src=/srv/easel/backups,dst=/input,readonly \
  --mount type=bind,src=/srv/easel/restore,dst=/restore \
  "$LOCAL_IMAGE" python -m easel admin restore \
  --source "/input/control-$STAMP" --destination "/restore/control-$STAMP" \
  --managed-root /restore
```

更换源名即可演练 full 备份。恢复会校验摘要和 key；缺 key/不匹配应失败，不生成新 key“修复”。新目录中包含 control/control.sqlite3 和 master.key；完整备份另有 workspaces。以新端口、新项目名和新数据根验证后再切换代理，保留旧目录。备份里有 master key 和可能的用户凭证，应加密离机保存。

### 10.4 以后迭代升级与回滚

1. 记录源码 SHA、control/worker/sidecar 镜像 ID、受管 revision、Harness commit/profile 及证书配置。
2. 维护挡新任务 → 等收尾 → drain/完整备份 → 校验备份可恢复。
3. 新代码/镜像先在新目录与测试数据验证；不要重建时丢掉 master key。
4. 停旧服务，更新固定镜像/配置，先启动 provisioner，再启动 control/egress；验证 socket 权限、健康、实际聊天和隔离。
5. 受管 revision 与模型配置版本不是同一个概念；更改底座后旧会话可能不兼容，不能改 native ID 假装续接。
6. 出错回滚时一起恢复旧镜像、旧配置和兼容的数据副本。SQLite 已升级后旧代码可能拒绝新 schema，禁止手工调低 user_version。

`easel admin migrate-local` 是显式迁移工具，不是日常升级命令。它要求合法目标 workspace、空目录和备份；迁移后的文件级历史也不等于 control 已登记会话目录或真实 Harness 可续接。具体 dry-run/导入命令见[原运维手册](multiuser-deployment.zh-CN.md#7-显式本机迁移)。

## 11. 怎样判断“真的能用”与“可以发布”

| 检查 | 只能说明什么 |
|---|---|
| compose ps 是 running | 容器进程还在 |
| /api/health 返回 200 | control 健康入口可达 |
| 管理员可以登录 | Cookie/邀请链路能用 |
| 新会话与连续追问成功 | 当前模型/底座调用能用 |
| A/B 相互读取被拒绝 | 被测试的授权边界成立 |
| 正式检查全部真实门槛通过 | 才能讨论发布；当前还达不到 |

在已安装测试依赖的宿主源码环境中，可复跑本地证据：

```bash
python -m pytest tests/multiuser/test_two_users.py -q
python -m pytest tests/multiuser/test_release_checker.py -q
python scripts/check_multiuser_release.py --json
```

正式检查所需 EASEL_TEST_* 是**专用测试环境**的 URL、一次性 A/B 邀请、空恢复目录等，不从开发者账号复制。显式 `--multiuser-live` 缺环境就报错；skip/xfail/xpass、漏选、统计不可信均不能计通过。检查器当前即使选中门槛全绿，也会因固定未验证证据退出 1、标为 blocked，这不是要求你绕过检查的安装故障。

## 12. 命令速查与排障

```bash
# 本机服务与磁盘
easel_local ps
easel_local logs --tail 100
easel_control ps
easel_control logs --tail 100
sudo ss -lntp | grep -E ':7860|:7861|:443'
df -h /srv/easel
# 只列受管 worker，避免误操作主机其它容器
sudo docker ps -a --filter label=easel.managed=true
```

| 错误 | 含义与处理 |
|---|---|
| invalid_origin | 必须是规范 HTTPS Origin，不能填业务路径 |
| master_key_unavailable / invalid_master_key | 检查文件、32 字节长度和 0600 权限；不要生成新 key 覆盖 |
| provisioner_unavailable / unauthorized | 核对先后顺序、socket 挂载、属组、真实连接 UID；不挂 Docker socket 兜底 |
| worker_wiring_mismatch | 旧实例接线不一致；先按维护流程 drain，不强删活跃容器 |
| capability_unavailable / 503 | 缺签名/模型/运行时装配或业务尚未实现，不是清缓存就能修好 |
| No usable sandbox! | 真实浏览器沙箱不可用；不得关闭沙箱换成功 |
| restore_target_not_empty | 用新的空目录，不删除正在使用的数据 |
| control_port_in_use | 无既有 control 时回环端口被其它进程占用；停占用或换端口，不杀未知进程 |
| upgrade_deployment_mismatch | 备份与旧 deployment.json/项目/镜像不一致；重新 Stop+Backup，不要强改完成记录 |
| release_status=blocked | 正式证据未验证，按证据项补验收，不改检查器强行放行 |

不要运行全机 `docker rm -f $(docker ps -aq)`、递归清空 `/srv/easel` 或覆盖 master key。容器、源码和持久数据是三件不同的东西，清理前先确认名字、标签、目录和备份。

外部依据已于 2026-10-26 核对：[Docker Ubuntu 安装](https://docs.docker.com/engine/install/ubuntu/)、[Compose up](https://docs.docker.com/reference/cli/docker/compose/up/)、[Caddy reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。Easel 的实际行为以本仓库源码和[本地代码验收记录](superpowers/audits/2026-10-26-task-14-accepted.md)为准；本文新增配置示例未在你的服务器执行，不宣称已通过完整镜像构建或生产部署。

## 13. A7 完整 Desktop：Linux 可核查命令与 not-run 清单

本节只给**能在目标 Linux 主机上核查/执行的命令模板**。A7 的真实恢复/升级演练在 Windows 11 + Docker Desktop + WSL2 测试部署完成；下面没有在目标 Linux 服务器执行，不能写成 Linux 已通过。Desktop 的 PowerShell 入口在 Linux 上不可直接运行，需要按同一 CLI/卷/权限语义移植；没有目标机时不要伪造执行结果。

### 13.1 固定镜像与前置核查

```bash
SRC=/srv/easel/src
CONTROL_IMAGE=$(sudo docker image inspect easel-control:a7 --format '{{.Id}}')
WORKER_IMAGE=sha256:替换为已验收-worker
GATEWAY_IMAGE=sha256:替换为已验收-gateway
PROJECT=easel-desktop-multi
DATA_ROOT=/srv/easel/desktop
WORKSPACE_ROOT=/srv/easel/workspaces

test "$CONTROL_IMAGE" = "sha256:$(printf '%s' "$CONTROL_IMAGE" | cut -d: -f2)" || exit 1
sudo docker image inspect "$WORKER_IMAGE" --format '{{.Id}}'
sudo docker image inspect "$GATEWAY_IMAGE" --format '{{.Id}}'
sudo docker info --format '{{.OSType}}'   # 必须是 linux
findmnt -no FSTYPE,OPTIONS --target "$WORKSPACE_ROOT"
sudo iptables -S DOCKER-USER
sudo ip6tables -S DOCKER-USER
```

XFS/prjquota、DOCKER-USER、IPv6 只是只读检查；它们通过不等于配额耗尽、跨租户隔离或 IPv6 拒绝测试通过。

### 13.2 数据位置（与 Windows 一致）

- `${PROJECT}-control` 卷：control.sqlite3、登录/邀请/Run/stop 记录。
- `${PROJECT}-keys` 卷：master.key、snapshot.pem、派生公钥、managed-agent.json、egress-policy.json、egress-ca.pem。
- `${PROJECT}-egress` 卷：出口策略副本。
- `${WORKSPACE_ROOT}/<workspace_id>/workspace`（容器 `/workspace`，含 `state`）与 `home`（容器 `/home/easel`，含 `.dsh`）。
- `${DATA_ROOT}/public`、`logs`、`backups`、`recovery`。
- `${WORKSPACE_ROOT}/.credentials`、`.gateway` 是运行期材料，不备份；恢复时重新生成。

### 13.3 只读 Status/日志/backup/verify（可直接核查）

Status/日志没有 PowerShell 时，用固定 `docker inspect`/`docker logs` 和 control DB 查询：

```bash
sudo docker ps --filter "label=com.docker.compose.project=$PROJECT" \
  --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
sudo docker logs --tail 200 "$(sudo docker ps -q \
  --filter "label=com.docker.compose.project=$PROJECT" \
  --filter label=com.docker.compose.service=control)"
sudo docker exec "$(sudo docker ps -q \
  --filter "label=com.docker.compose.project=$PROJECT" \
  --filter label=com.docker.compose.service=control)" \
  python -c "import sqlite3;print(sqlite3.connect('/srv/easel/control/control.sqlite3').execute('PRAGMA user_version').fetchone()[0])"
```

备份必须先 Stop（drain + barrier + 完成记录）。在 Linux 上可直接调用同一 CLI，核心挂载布局与 Windows 相同：

```bash
BACKUP_NAME=desktop-$(date +%Y%m%d-%H%M%S)
sudo mkdir -p "$DATA_ROOT/backups/$BACKUP_NAME"
sudo docker run --rm --network none --user 0:0 \
  --mount "type=volume,src=${PROJECT}-control,dst=/data/control" \
  --mount "type=bind,src=$WORKSPACE_ROOT,dst=/data/workspaces" \
  --mount "type=bind,src=$DATA_ROOT,dst=/data" \
  --mount "type=volume,src=${PROJECT}-keys,dst=/keys,readonly" \
  "$CONTROL_IMAGE" python -m easel admin backup \
    --control-dir /data/control \
    --destination "/data/backups/$BACKUP_NAME" \
    --master-key-file /keys/master.key \
    --workspace-root /data/workspaces \
    --signing-key-file /keys/snapshot.pem \
    --managed-agent-config /keys/managed-agent.json \
    --egress-policy-file /keys/egress-policy.json \
    --egress-ca-file /keys/egress-ca.pem \
    --deployment-json /data/logs/deployment.json \
    --image-digest "$WORKER_IMAGE" --harness-revision 替换为已验收提交 \
    --require-clean-stop
```

完整模式必须提供真实 `--workspace-root`；缺根不会产出 `complete=true`。已有 Run 历史的 workspace 缺目录会失败，尚未物化的新 workspace 才允许为空。Windows 脚本还会把该根绑定到 `deployment.json` 的登记值；Linux 手工命令必须使用同一已登记根，不能用另一个空目录替代。

只读校验：

```bash
sudo docker run --rm --network none --read-only --user 10001:10001 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m \
  --mount "type=bind,src=$DATA_ROOT/backups/$BACKUP_NAME,dst=/backup,readonly" \
  "$CONTROL_IMAGE" python -m easel admin verify-backup --source /backup
```

`verify-backup` 逐项核对清单全部文件的大小与 SHA-256（含 workspace/HOME 和有效 SQLite 内容），不只看结构/签名配置；缺文件、多文件或同大小内容篡改都失败。

### 13.4 恢复/升级（命令存在，但 Linux 端到端未执行）

恢复目标必须是全新项目/卷/网络/目录/端口；先 `admin restore`，再 `admin prepare-restore`（会撤销旧登录/邀请/token 并输出一次性管理员恢复邀请），最后 `admin materialize-restore` 落到新的 control/keys/egress/workspace 目标。核心命令：

```bash
sudo docker run --rm --network none --user 0:0 \
  --mount "type=bind,src=$DATA_ROOT/backups/$BACKUP_NAME,dst=/backup,readonly" \
  --mount "type=bind,src=$DATA_ROOT,dst=/data" \
  --mount "type=bind,src=$RESTORED_WORKSPACE_ROOT,dst=/data/workspaces" \
  --mount "type=volume,src=${RESTORED_PROJECT}-control,dst=/data/control" \
  --mount "type=volume,src=${RESTORED_PROJECT}-keys,dst=/keys" \
  --mount "type=volume,src=${RESTORED_PROJECT}-egress,dst=/policy" \
  "$CONTROL_IMAGE" python -m easel admin restore \
    --source /backup --destination /data/restore-staging --managed-root /data
```

```bash
# 同一挂载再执行两次：
python -m easel admin prepare-restore --destination /data/restore-staging
python -m easel admin materialize-restore --source /data/restore-staging \
  --control-dir /data/control --keys-dir /keys --egress-dir /policy \
  --workspace-root /data/workspaces
```

`prepare-restore` 会合法修改 staging 控制库并更新 staging 清单的来源摘要；原备份必须保持不变。`materialize-restore` 会重新核对输入及实际复制字节，失败只清理本次新建的目标，不提交部分恢复。

恢复到新部署后，初始化同样要先探测既有 control DB/key；已有 DB/身份缺 master/signing 时拒绝且不生成新 key，仅全新空部署创建初始材料。

恢复后必须用新的 provider/provisioner/worker/gateway 启动，验证 A/B 历史、sentinel、同会话新 Harness 消息；旧 Cookie 应 401，新 Origin 不匹配写请求应 403。升级必须先 Stop+Backup；Windows `Start -UpgradeFromBackup` 会在 compose up 前完成只读全量校验和旧部署身份匹配，Linux 手工命令必须保持同一顺序：`verify-backup` → 启动新镜像 → `admin stop-status` → `admin upgrade-complete` → `admin reopen`，不得先启动新 control 再查备份。**本轮没有 Linux 目标机，以上恢复/升级/回滚未在 Linux 执行，不得写成 passed。**

### 13.5 Linux not-run 清单（生产阻断保持）

| 项 | 状态 | 说明 |
|---|---|---|
| XFS/prjquota 配额建立与耗尽 | not-run | 没有目标 XFS 宿主 |
| iptables/nftables 宿主防火墙 | not-run | 未在目标机验证 DOCKER-USER/入站拒绝 |
| IPv6 | not-run | 未验证禁用/隔离 |
| Chromium 沙箱/XFS 持久化 | not-run | Docker Desktop 结果不替代 |
| 真实 DeepSeek 供应商 | not-run | 无批准受管 channel/凭证；未读开发者 `.env`/HOME |
| ngrok 公网复跑 | not-run | 沿用 A6 证据；A7 未重做公网浏览器矩阵 |
| Linux systemd/daemon 重启续接 | not-run | 只在 Windows Docker Desktop 演练 |

### 13.6 两个独立维度

- **Desktop 功能/运维（Windows 测试宿主）实际通过**：初始化、start/stop/status/logs、v2 备份/只读 verify、新目录恢复、A/B 隔离/历史/sentinel/新 Harness 消息、受控升级、坏镜像失败、旧镜像回滚。证据见 `docs/evidence/desktop-a7/`。
- **production-release（Linux 目标）仍未通过**：上表 not-run 项、原检查器固定 blocked 项、真实供应商。不得因为桌面功能通过就宣称生产可发布。
