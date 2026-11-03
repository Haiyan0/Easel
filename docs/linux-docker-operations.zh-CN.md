# Easel Linux Docker 部署与运维手册

适用：Ubuntu 24.04 LTS、Bash、Docker Engine。源码分支：`feat/multiuser-task1-store`（2026-11-03 更新），使用 `git rev-parse HEAD` 记录实际版本。[Windows 多用户手册](windows-desktop-operations.zh-CN.md)。

## 1. 先确认部署目标

- **单人自用**：第 2–7 节给出从新机到镜像、启动、SSH 访问、备份、升级与回滚的命令。local 模式没有多人认证，只监听回环地址，不直接开放公网。
- **多人服务器**：第 8 节起给出同一 control/provisioner/worker/gateway 架构的原生 Linux 装配与维护顺序。现有完整自动化入口是 Windows PowerShell + WSL；Linux 原生启停、身份登记、升级联锁还没有等价的已验收脚本。下文将可直接执行的准备命令与需要目标机工程接线的步骤分开，不把模板称为可直接生产上线。

Windows Desktop 功能验收不覆盖目标 Linux 的 XFS 配额、防火墙、IPv6、Chromium 沙箱、systemd 及真实 DeepSeek。正式 production-release 仍 blocked。若现在要自己完整验收多人功能，先用 Windows 路线；服务器上线前须补齐第 8–11 节的环境验证与生命周期装配，不能只运行参考 compose.yaml。

## 2. 准备 Linux 主机和代码

### 2.1 约定

以下示例使用 Ubuntu 24.04 LTS、Bash、Linux Docker Engine。普通命令在 Linux SSH 终端执行；`sudo` 表示需要管理员权限。不要把 PowerShell 的反引号续行和 Bash 的反斜杠续行混用。

镜像是程序安装包，容器是正在运行的进程环境，挂载目录才是需要长期保存的数据。`compose up -d` 创建/更新运行实例，`stop` 只停进程；不要把删容器当成数据备份。HTTPS 代理是浏览器访问服务的入口，provisioner 则是有权限创建 worker 的宿主组件，两者不是同一个服务。

推荐目录：

```text
/srv/easel/
  src/              唯一 Git 源码目录，更新在原目录拉取
  ops/              本文的 Dockerfile、Compose 和镜像记录
  local/            单人模式 outputs/profiles/home/apps（apps 内含 .env）
  control/          多人 control.sqlite3
  keys/             master key，不能进 Git
  workspaces/       多人持久区；正式环境要求 XFS 项目配额
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

### 2.3 从自己的 GitHub 获取源码（只保留一个目录）

不再从 Windows 打包复制仓库，也不按每个版本创建源码目录。**在 Linux Bash** 中执行；下面 clone 只在首次运行，已有目录不能重复 clone：

```bash
sudo install -d -o "$USER" -g "$(id -gn)" /srv/easel/src /srv/easel/ops
git clone --branch feat/multiuser-task1-store --single-branch https://github.com/Haiyan0/Easel.git /srv/easel/src
cd /srv/easel/src
REV=$(git rev-parse HEAD)
SRC=/srv/easel/src
OPS=/srv/easel/ops
git status --short
```

以后仍在 `/srv/easel/src` 更新。**多人服务必须由已配置的生命周期管理入口完成 drain/stop，确认没有活跃 Run 或 provisioner 后再 pull**，不能热改运行中的 provisioner 源码：

```bash
cd /srv/easel/src
git status --short          # 有修改先保存处理，不能强制覆盖
git pull --ff-only origin feat/multiuser-task1-store
REV=$(git rev-parse HEAD)
```

更新完整顺序是：通知停机 → 正常停止 → 完整备份并 verify → 同目录 pull → 构建镜像 → 用原数据/Key 和匹配备份启动 → 检查登录/历史/新消息。原生 Linux 多人启停脚本尚未等价验收，不把上述拉取代码步骤当作完整生产部署命令；生命周期要求见第 9 节。单人 local 更新使用第 6 节。

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
if sudo test -e "$APP_ROOT" || sudo test -L "$APP_ROOT"; then
  echo '版本应用目录已存在，先核对，不覆盖'; exit 1
fi
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
easel_local stop easel || { echo '停止失败，先检查日志'; exit 1; }
easel_local start easel
sudo docker system df
df -h /srv/easel
sudo du -sh /srv/easel/local
```

看日志时按 Ctrl+C 只退出日志查看。日志分享前清理 Key、Cookie、邀请和消息正文。不要对整台 Docker 主机执行盲目 prune；不要把删用户 home/outputs 当成释放容器空间的方法。

## 6. 单人模式备份、恢复和升级

### 6.1 冷备份

等任务结束，停服务后再打包。备份包含 `.env`、浏览器/原生凭证，必须私密保存：

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
sudo install -d -m 0700 /srv/easel/backups
easel_local stop easel || { echo '停止失败，不执行冷备份'; exit 1; }
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

将恢复副本用独立项目和端口启动，不覆盖源。先列出副本中的应用版本：

```bash
RESTORED_DATA="/srv/easel/restore/local-$STAMP/local"
sudo find "$RESTORED_DATA/apps" -mindepth 1 -maxdepth 1 -type d
RESTORED_APP="$RESTORED_DATA/apps/替换为备份中对应版本SHA"
sudo test -f "$RESTORED_APP/pyproject.toml" || exit 1
sudo sed 's/127.0.0.1:7860:7860/127.0.0.1:7862:7860/' \
  "$OPS/compose.local.yaml" | sudo tee "$OPS/compose.restore.yaml" >/dev/null
# 使用与该备份兼容的旧镜像；值来自升级前保留的 local.env
RESTORE_IMAGE=sha256:替换为旧镜像ID
printf 'EASEL_LOCAL_IMAGE=%s\nEASEL_DATA_ROOT=%s\nEASEL_APP_ROOT=%s\n' \
  "$RESTORE_IMAGE" "$RESTORED_DATA" "$RESTORED_APP" |
  sudo tee "$OPS/restore.env" >/dev/null
easel_restore() { sudo docker compose -p easel-local-restore \
  --env-file "$OPS/restore.env" -f "$OPS/compose.restore.yaml" "$@"; }
easel_restore config --quiet || exit 1
easel_restore up -d || exit 1
easel_restore logs --tail 80
```

用途：在 7862 验证副本；按第 4.1 节把 SSH 转发端口也改为 7862。先核对旧镜像确实存在，再启动；恢复卷不接入原服务。确认模型配置、文件、原生历史后再计划切换。local 浏览器展示记录不等于都在服务端，换浏览器可能需要处理站点存储。

### 6.3 版本升级

1. 记录旧源码 SHA、`local.env` 里的镜像 ID、Compose 和 Harness 版本，执行冷备份。
2. 保持服务停止，在唯一源码目录 `/srv/easel/src` 执行 `git pull --ff-only origin feat/multiuser-task1-store`，按第 3.1 节构建新 tag；不要覆盖旧数据。
3. 保存旧镜像/应用目录记录，从新镜像提取**新的应用目录**；不能只换镜像却继续挂着旧代码。以下先把 NEW_IMAGE/NEW_REV 改为实际新版本，APP_ROOT 指向当前旧应用目录：

```bash
sudo cp "$OPS/local.env" "$OPS/local.env.before-upgrade"
easel_local stop easel || { echo '停止失败，暂停升级'; exit 1; }
APP_ROOT=$(sudo sed -n 's/^EASEL_APP_ROOT=//p' "$OPS/local.env")
case "$APP_ROOT" in /srv/easel/local/apps/*) ;; *) echo '旧应用目录配置不符，先核对'; exit 1;; esac
# 确认此变量来自新镜像的 docker image inspect
NEW_IMAGE=替换为新镜像sha256值
case "$NEW_IMAGE" in sha256:*) ;; *) echo '先填写新镜像ID'; exit 1;; esac
NEW_REV=替换为新代码完整SHA
case "$NEW_REV" in *替换*) echo '先填写新版本'; exit 1;; esac
NEW_APP="/srv/easel/local/apps/$NEW_REV"
if sudo test -e "$NEW_APP" || sudo test -L "$NEW_APP"; then
  echo '新应用目录已存在，先核对'; exit 1
fi
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

## 8. 多人服务器：准备镜像和目标主机

<a id="13-a7-完整-desktopl-unix-可核查命令与-not-run-清单"></a>

**状态：以下是原生 Linux 装配命令模板，未在你的服务器端到端执行。当前缺少与 Windows 等价、已验收的 Linux 生命周期包装脚本，不应让初学者将它直接用于生产。** 第 9 节列出必须补齐的启动/停止契约；第 10 节是这些契约满足后才能执行的维护 CLI。不能用单人 local 或仅登录 control 替代完整多人服务。

### 8.1 固定并构建版本

使用第 2 节的源码目录，而不是临时改变运行中 provisioner 使用的目录：

```bash
REV=$(git -C /srv/easel/src rev-parse HEAD)
SRC=/srv/easel/src
OPS=/srv/easel/ops
DSH_COMMIT=687f282e8625c6b8d11ff58211a90524be2e4317
cd "$SRC"
sudo docker build -f deploy/desktop/Dockerfile.control -t "easel-control:$REV" . || exit 1
sudo docker build -f deploy/desktop/Dockerfile.gateway -t "easel-gateway:$REV" . || exit 1
sudo docker build -f deploy/multiuser/Dockerfile.worker \
  --build-arg "DSH_COMMIT=$DSH_COMMIT" -t "easel-worker:$REV" . || exit 1
CONTROL_IMAGE=$(sudo docker image inspect "easel-control:$REV" --format '{{.Id}}') || exit 1
WORKER_IMAGE=$(sudo docker image inspect "easel-worker:$REV" --format '{{.Id}}') || exit 1
GATEWAY_IMAGE=$(sudo docker image inspect "easel-gateway:$REV" --format '{{.Id}}') || exit 1
printf '%s\n' "$CONTROL_IMAGE" "$WORKER_IMAGE" "$GATEWAY_IMAGE"
```

用途：构建真实 Harness runtime 及对应 control/gateway。成功后三个值都是 sha256 镜像 ID；worker 首次构建较慢，不改用 base 镜像、不无缓存重建。镜像内部路径固定 `/opt/easel`、`/opt/dsh`、`/workspace`，不使用 Windows 路径。

### 8.2 检查正式隔离前提

```bash
sudo install -d -m 0750 /srv/easel/workspaces
findmnt -no TARGET,FSTYPE,OPTIONS --target /srv/easel/workspaces
sudo iptables -S DOCKER-USER
sudo ip6tables -S DOCKER-USER
sudo docker info --format '{{.OSType}}'
```

用途：确认 workspace 所在文件系统、项目配额和 Docker 防火墙入口。正式模式要求已配置的 XFS/prjquota 或经过验收的等效配额；普通 ext4 目录不满足该默认方案。这里不格式化磁盘、不自动修改既有防火墙。实际配额建立/耗尽、跨 workspace/宿主管理面/IPv6 拒绝、浏览器沙箱仍须目标机实测；读到链名不等于隔离验证通过。

没有这些前提时停在试部署准备阶段，不能通过 `--allow-unquotaed`、privileged 或关闭 Chromium 沙箱获得生产通过。[网络与出口约束](../deploy/multiuser/egress-policy.md)

## 9. 多人首次启动：必须按这个顺序装配

本节供部署工程实施，**不是已经存在的“一键启动命令”**。原生 Linux 需要将这些动作接入服务管理器，保留失败关闭和状态核对；直接启动参考 Compose 不能完成这一工作。

| 顺序 | 操作与成功判断 | 现有入口 |
|---|---|---|
| 1 | 创建专用 control/keys/egress 卷及控制网络；已有资源先核对归属标签，不能接管外来资源 | 对照 `deploy/desktop/Initialize-WorkspaceEasel.ps1` 的契约 |
| 2 | 只读检查 DB/key；已有身份缺私钥必须拒绝；仅全空部署创建 key | `deploy/desktop/initialize_state.py`、`keygen.py` |
| 3 | 写无秘密 managed-agent.json/egress-policy.json；私钥仅 control 可读，egress 只拿策略 | Windows 手册第 3 节配置结构 |
| 4 | 导出派生公钥到宿主 public 目录，准备 CA、workspace 根与配额 | 不复制管理员 HOME/供应商 Key |
| 5 | 先启动受限 provisioner，核对 socket 身份/权限与实际镜像/根/公钥 | 下方原生命令 |
| 6 | 启动 control + egress，control 加载签名/受管配置并启用 gateway-required | `deploy/desktop/compose.workspace.yaml` |
| 7 | TLS 固定域名反代只指向回环 control；bootstrap 一次后创建成员并配置 chat | 下方命令 |
| 8 | 保存真实 deployment.json/版本身份，跑双用户、重启和恢复验证 | Windows Start 的登记契约须移植，不手写假证据 |

### 9.1 provisioner 宿主命令

下面变量来自已经完成上述初始化的**本项目专用目录/网络**；不存在时先停下，不能把路径换成别人的资源：

```bash
PROJECT=easel-multi
DATA_ROOT=/srv/easel/desktop
WORKSPACE_ROOT=/srv/easel/workspaces
NETWORK="$PROJECT-control"
SOCKET=/run/easel/provisioner.sock
sudo apt-get install -y python3-venv
sudo python3 -m venv "$OPS/host-venv"
sudo "$OPS/host-venv/bin/python" -m pip install cryptography httpx
sudo install -d -o root -g 10001 -m 0750 /run/easel
test -s "$DATA_ROOT/public/worker-public.key" || exit 1
test -s "$DATA_ROOT/public/gateway-public.pem" || exit 1
test -s "$DATA_ROOT/public/egress-policy.json" || exit 1

sudo env PYTHONPATH="$SRC" setpriv --reuid=0 --regid=10001 --clear-groups \
  "$OPS/host-venv/bin/python" "$SRC/deploy/multiuser/provisioner.py" \
  --socket "$SOCKET" --workspace-root "$WORKSPACE_ROOT" \
  --image "$WORKER_IMAGE" --allowed-uid 10001 \
  --quota-mode project --quota-bytes 10737418240 \
  --worker-public-key "$DATA_ROOT/public/worker-public.key" \
  --sidecar-network "$NETWORK" --sidecar-policy "$DATA_ROOT/public/egress-policy.json" \
  --broker-upstream control:7861 \
  --gateway-image "$GATEWAY_IMAGE" --gateway-control-network "$NETWORK" \
  --gateway-public-key "$DATA_ROOT/public/gateway-public.pem"
```

用途：以独立宿主进程创建/检查受限 worker/sidecar/gateway。固定 GID 10001 使 control 可连接 socket，allowed UID 仍为 10001。先确认主机该 GID 未被其它业务占用。命令前台持续运行是正常的；正式托管须用受控 systemd unit，不能临时 nohup 后声称已完成启动/重启恢复验收。

公钥/网络/镜像必须来自当前受信部署。socket 父目录 0750、socket 0660；不得用 777 或把 Docker socket 挂给 control。provisioner 重建 socket 后需要重建 control 的单文件 socket 挂载。

### 9.2 control/egress 和 HTTPS

完整 Compose 需要以下环境变量，全部由 Linux 初始化/启动包装层从实际资源填入：

| 变量 | 值的来源 |
|---|---|
| EASEL_CONTROL_IMAGE | 本次固定 CONTROL_IMAGE |
| EASEL_PUBLIC_ORIGIN | 固定 `https://你的域名`，无路径 |
| EASEL_DESKTOP_PROJECT | PROJECT |
| EASEL_CONTROL_VOLUME / EASEL_KEY_VOLUME / EASEL_EGRESS_VOLUME | 已创建且归属核验的三个卷 |
| EASEL_CONTROL_NETWORK | NETWORK |
| EASEL_PROVISIONER_SOCKET | SOCKET |
| EASEL_PROVISIONER_GID | 10001，与宿主组一致 |
| EASEL_CONTROL_PORT | 7861，Compose 仅发布 127.0.0.1 |

这些值保存为 `$OPS/multiuser.env`（不含 Key），完成初始化及身份检查后才执行：

```bash
test -s "$OPS/multiuser.env" || { echo '缺少实际部署环境文件，停止'; exit 1; }
easel_multi() { sudo docker compose --env-file "$OPS/multiuser.env" \
  -p "$PROJECT" -f "$SRC/deploy/desktop/compose.workspace.yaml" "$@"; }
easel_multi config --quiet || exit 1
easel_multi up -d || exit 1
easel_multi ps
easel_multi logs --tail 100
curl --fail http://127.0.0.1:7861/api/health
```

用途：复用带签名、gateway-required、CA 与受控出口的完整 control 装配，而不是旧 control-only 配置。前提不满足时不要为了跑通新建空 key 或任意同名卷。

宿主已有 TLS 代理时，将固定域名反代到 `127.0.0.1:7861`。例如 Caddyfile 的最小站点配置：

```caddyfile
easel.example.com {
    reverse_proxy 127.0.0.1:7861
}
```

替换域名，DNS 指向本机，按 [Caddy 官方说明](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)在宿主配置/验证/重载；代理若在容器内，127.0.0.1 指的是代理容器自身，不能直接照用。外部只开放代理所需端口，不公开 worker/gateway/broker/provisioner。

```bash
CONTROL_ID=$(easel_multi ps -q control)
test -n "$CONTROL_ID" || exit 1
sudo docker exec "$CONTROL_ID" python -m easel admin bootstrap \
  --control-dir /srv/easel/control --name Owner
```

用途：首次生成管理员 invite，打开 PublicOrigin 登录，再在界面创建成员、设置受管 chat 供应商/模型/协议/凭证。Key 不进入公开配置、命令行或 worker。浏览器 A/B 独立上下文测试隔离、权限、停止、重启历史；health 200 仅表示入口存活。

## 10. 多人日常停止、完整备份与恢复

### 10.1 成功 Stop 的顺序

不要直接 systemctl stop/compose down。先在运行中的 control 建屏障并收尾：

```bash
sudo docker exec "$CONTROL_ID" python -m easel admin drain \
  --control-dir /srv/easel/control \
  --provisioner-socket /run/easel/provisioner.sock --gateway-required --barrier
```

用途：阻止新任务并停止/对账已有实例。必须 exit 0、`verified=settled`，保存输出 `barrier=`。随后包装层才能 down control/egress、停止本项目 provisioner，最后用同版本 CLI 写 stop-complete。完整调用参数可查：

```bash
sudo docker run --rm --network none "$CONTROL_IMAGE" \
  python -m easel admin stop-complete --help
sudo docker run --rm --network none "$CONTROL_IMAGE" \
  python -m easel admin reopen --help
```

`barrier-id/project/config-sha256` 必须来自实际登记身份。**Linux 端尚无经过验收的包装层来原子完成这些动作**，所以不在手册用任意摘要/手改 DB 代替；该缺口补齐前不得把手工 down 当成干净停止，也不能执行下方正式维护。

### 10.2 完整备份与只读校验

以下要求已成功 Stop，且 `$DATA_ROOT/logs/deployment.json` 是由上述包装层核对并登记的真实身份；缺文件直接停止。workspace 根须匹配登记值，不能换空目录。

```bash
test -s "$DATA_ROOT/logs/deployment.json" || exit 1
BACKUP_NAME="full-$(date -u +%Y%m%dT%H%M%SZ)"
sudo install -d -m 0700 "$DATA_ROOT/backups"
BACKUP="$DATA_ROOT/backups/$BACKUP_NAME"
sudo docker run --rm --network none --user 0:0 \
  --mount "type=volume,src=${PROJECT}-control,dst=/data/control" \
  --mount "type=bind,src=$DATA_ROOT,dst=/data" \
  --mount "type=bind,src=$WORKSPACE_ROOT,dst=/data/workspaces" \
  --mount "type=volume,src=${PROJECT}-keys,dst=/keys,readonly" \
  "$CONTROL_IMAGE" python -m easel admin backup \
  --control-dir /data/control --destination "/data/backups/$BACKUP_NAME" \
  --master-key-file /keys/master.key --workspace-root /data/workspaces \
  --signing-key-file /keys/snapshot.pem --managed-agent-config /keys/managed-agent.json \
  --egress-policy-file /keys/egress-policy.json --egress-ca-file /keys/egress-ca.pem \
  --deployment-json /data/logs/deployment.json \
  --image-digest "$WORKER_IMAGE" --harness-revision "$DSH_COMMIT" --require-clean-stop
```

用途：备份控制库、key、配置与各 workspace/HOME，不含 .credentials/.gateway/socket。要求本项目卷名遵循第 9 节约定。备份完成后，给 UID 10001 只读访问所需权限（仅作用于本次新备份），再校验：

```bash
sudo chown -R 10001:10001 "$BACKUP"
sudo docker run --rm --network none --read-only --user 10001:10001 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  --mount "type=bind,src=$BACKUP,dst=/backup,readonly" \
  "$CONTROL_IMAGE" python -m easel admin verify-backup --source /backup
```

成功判断：version=2、clean_stop=true，完整文件摘要验证成功。Verify 在私有 DB 副本检查，不向备份创建 WAL/SHM；重复验证不改变源。备份有秘密，离机保存并保留旧镜像/配置。

### 10.3 新目录恢复及回滚

源保持停止，恢复目录必须新建、不包含源/备份；先建立空 staging 父目录并让 UID 10001 可写：

```bash
RESTORE_PARENT=/srv/easel/restore/a7-rehearsal
if sudo test -e "$RESTORE_PARENT" || sudo test -L "$RESTORE_PARENT"; then
  echo '目标已存在，请换新目录'; exit 1
fi
sudo install -d -o 10001 -g 10001 -m 0700 "$RESTORE_PARENT"
sudo docker run --rm --network none --user 10001:10001 \
  --mount "type=bind,src=$BACKUP,dst=/backup,readonly" \
  --mount "type=bind,src=$RESTORE_PARENT,dst=/restore" \
  "$CONTROL_IMAGE" python -m easel admin restore \
  --source /backup --destination /restore/staging --managed-root /restore
```

用途：在新目录验证/恢复，原备份不变。每条命令失败即停，不继续 prepare。下一步仍使用相同挂载：

```bash
sudo docker run --rm --network none --user 10001:10001 \
  --mount "type=bind,src=$RESTORE_PARENT,dst=/restore" \
  "$CONTROL_IMAGE" python -m easel admin prepare-restore --destination /restore/staging
```

用途：先验证 staging 再撤销恢复副本的旧登录/邀请/token、清理运行身份，输出一次性 recovery_invite；只在私密终端处理，不写公开日志。合法 prepare 更新 staging 清单，原备份不变。

最后用 `admin materialize-restore` 将 staging 落到**新项目**的空 control/keys/egress 卷与 workspace 根，再按第 9 节派生公钥并启动，不能 Initialize/bootstrap 覆盖恢复身份：

```bash
sudo docker run --rm --network none "$CONTROL_IMAGE" \
  python -m easel admin materialize-restore --help
```

该命令必须同时挂载 staging、新 control、新 keys、新 egress、新 workspace，并分别传 `--source/--control-dir/--keys-dir/--egress-dir/--workspace-root`。Linux 包装层尚需实现新目标归属/权限/清理与启动登记，不能只执行上面 restore 就称服务恢复成功。[Windows 已验收的新项目恢复步骤](windows-desktop-operations.zh-CN.md#9-恢复到新目录新项目)

恢复实例采用新端口和域名验证：旧 Cookie 401、错误 Origin 写入 403、A/B 历史与文件隔离、同会话新消息。成功后才切 TLS 代理；源/副本不同时使用。回滚恢复到备份时刻，不自动合并升级后新增数据，禁止手调 user_version。

## 11. 多人代码更新与上线顺序

1. **获取版本**：按第 2 节导出新 SHA 到新源码目录；保留旧目录，不原地覆盖运行代码。
2. **提前构建**：以新 REV/SRC 重跑第 8.1 节，记录三个镜像 ID 和 DSH_COMMIT；不以浮动 tag 作为发布身份。先在隔离测试项目做登录、聊天、停止和恢复验证。
3. **维护窗口**：成功 Stop → 完整备份 → verify；此后不 reopen，否则要重新 Stop+Backup。
4. **升级前联锁**：在新 control 可能迁移正式 DB 前，读取当前 Stop/key 与备份匹配，再完整验证备份。下方 helper 只读复制 DB 后查询，不打开 Store、不迁移：

```bash
sudo docker run --rm --network none --read-only --user 10001:10001 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  --mount "type=volume,src=${PROJECT}-control,dst=/control,readonly" \
  --mount "type=volume,src=${PROJECT}-keys,dst=/keys,readonly" \
  "$CONTROL_IMAGE" python /opt/easel/deploy/desktop/read_stop_state.py \
  --control-dir /control --keys-dir /keys
```

用途：得到停止记录和四个 key 的指纹。包装层必须严格匹配备份 runtime_state.stop 的 barrierId/project/configSha256/completedAt，以及 identity 中 key 摘要；旧 Stop、错项目、错镜像/根、缺材料、损坏备份一律在启动前拒绝。不能让操作员只“看起来相同”就跳过程序核对。

5. **切换程序**：更新固定镜像、provisioner 与源码路径，先启动 provisioner 再重建 control/egress；原 key 与数据不变。启动后复核 stop-status，由 upgrade-complete 更新配置承诺，再按精确身份 reopen，不能无条件 resume。
6. **放量上线**：先验证受管模型真实回复、登录/历史/隔离/停止，再恢复用户访问，记录时间、版本、镜像、备份与结果。
7. **失败回滚**：保留故障卷/日志，按第 10.3 节在新目录/新项目恢复旧备份和旧镜像，验证后才切代理；不能直接降镜像继续读新 schema。

第 4–5 步的自动比较、停止 fencing、升级重开协议与 systemd 管理还需 Linux 原生包装层完成并实测。**当前仅改手册不能补出这些生产能力。** 不将 Linux 生产上线写成已完成；可以先完成镜像/基础设施准备，在包装层和隔离门槛验收后执行维护流程。

## 12. 生产验收与排障入口

```bash
sudo docker ps -a --filter label=easel.managed=true
sudo ss -lntp
df -h /srv/easel
```

用途：只读检查本项目受管实例、监听端口与磁盘。日志看 control/egress 与宿主 provisioner，排查不要输出 key/Cookie/邀请。

在安装测试依赖的目标源码环境执行：

```bash
sudo "$OPS/host-venv/bin/python" -m pip install -e "$SRC" pytest pytest-asyncio
cd "$SRC"
"$OPS/host-venv/bin/python" -m pytest "$SRC/tests/multiuser/test_two_users.py" -q
"$OPS/host-venv/bin/python" "$SRC/scripts/check_multiuser_release.py" --json
```

第一条给原本只有 provisioner 依赖的独立 venv 安装项目与测试依赖，不污染系统 Python；准备复跑代码检查时才需要。检查器当前固定证据 blocked，退出非零不能通过修改检查器绕过。单测通过不代表 XFS/防火墙/IPv6/浏览器/真实供应商已实测。[当前验收范围](superpowers/audits/2026-10-29-desktop-a7-accepted.md)
