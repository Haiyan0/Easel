# Easel Windows 本机与 Docker Desktop 使用运维手册

适合读者：会运行 Python，但不熟悉 Docker、服务器和运维的开发者。核对基线：`e8376753d63eb681560ae868aa2d1528b2756c85`（A7-R1；A7-R2 修订见交付说明），2026-10-29。

本文把“Desktop”理解为 **Windows 上的 Docker Desktop**；Easel 当前的操作界面仍是浏览器，不是另一个独立 Windows 安装包。只读这份手册即可先在本机使用；服务器部署见[Linux Docker 手册](linux-docker-operations.zh-CN.md)。

## 1. 先选一条路线

| 路线 | 你会得到什么 | 适合谁 |
|---|---|---|
| A：Windows Python 本机运行 | 单人完整工作台，直接使用本机已配置的 DeepSeek Harness/ACP | **个人本机最省事**，已有 Harness 无需搬迁 |
| B：Docker Desktop 单人运行 | 独立 Linux 容器、浏览器工作台；默认“仅聊天模式” | 想学习容器、备份和升级的人 |
| C：Docker Desktop 完整多人 | ngrok HTTPS、邀请登录、成员/权限、模型设置、真实 Harness 聊天、停止、备份/恢复/升级/回滚 | 本机团队验证；见第 9 节 |
| 正式 Linux 生产发布 | 本机 Desktop 功能已验收只覆盖 Windows 测试宿主；Linux/XFS/firewall/IPv6/Chromium 生产门槛仍未通过 | 暂不作为“生产可发布”依据 |

重要区别：

- `easel web` 是 **local 单人模式**，没有多人邀请登录边界。不能把它直接开放给团队或公网。
- `easel serve-control` 是 **多人控制面**。完整 Desktop 路线 C 已装配 control、gateway、真实 Harness worker、sidecar/broker、权限与停止收尾；不能把“仅聊天”或“仅登录”旧镜像当成完整聊天环境。
- 路线 C 的聊天必须通过受管 provider/channel；本手册不要求也不允许把真实供应商 Key 写进命令行、日志、截图或 Git。真实 DeepSeek 请求仍是独立 not-run 项。
- Docker Desktop 的功能/运维结果不等于 Linux 生产发布验收；原发布检查器的固定阻断项仍然存在。

## 2. 命令怎么读

本文命令在 **PowerShell** 执行，不在 Python 的 `>>>` 里执行。`#` 后面是说明。每次新开终端，需要重新设置本节变量。

| 词 | 你可以怎样理解 |
|---|---|
| 镜像 image | 打包好的程序和依赖，类似安装包；不等于用户数据 |
| 容器 container | 镜像启动后的一次运行实例，可以停止或重建 |
| 挂载 mount | 把宿主目录交给容器使用，重建容器后数据还在 |
| 端口 | 服务的访问号码；127.0.0.1:7860 只在本机访问 |
| Compose | 用一个 YAML 文件描述一套容器配置的工具 |
| Harness/ACP | 实际执行 Agent 的底座，以及 Easel 与它通信的协议 |

你当前电脑已有 `Agent` conda 环境，不要再安装一套系统 Python。先执行：

```powershell
$EaselRoot = 'C:\Code\Easel'
$EaselPython = 'C:\Users\hyh\anaconda3\envs\Agent\python.exe'
Set-Location $EaselRoot
& $EaselPython --version
git rev-parse --short HEAD
```

`&` 表示执行后面的程序路径；路径里有空格时也能正常工作。

别人的新电脑没有这个 conda 环境时，按仓库[快速开始](../README.md#-快速开始)安装 Python/Git/Node 后，在项目目录运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1 -SkipOpenClaw
if ($LASTEXITCODE -ne 0) { throw '请先排查安装器输出' }
$EaselPython = Join-Path (Get-Location) '.venv\Scripts\python.exe'
```

Process 只影响当前终端。已有环境不必重跑安装器，安装器也不负责替你安装 DeepSeek Harness。

**代码版本注意：** 本次多人改动在你的功能分支中，不能假设公开仓库 `main` 已含这些改动。以实际验收提交/维护者提供的版本为准，不要盲目执行 `git pull origin main`。

## 3. 路线 A：本机启动，最快开始使用

### 3.1 检查依赖

```powershell
& $EaselPython -m easel --help
& $EaselPython -m easel doctor
node --version
npm --version
ffmpeg -version
Test-Path .\web\frontend\dist\index.html
```

预期：Python 命令能运行；最后一行是 `True`。`doctor` 可能提示 OpenClaw 未安装：如果你打算使用 ACP，这不代表 ACP 也不可用；以实际所选底座和聊天验证为准。

如果只缺前端生产包：

```powershell
Push-Location .\web\frontend
npm ci
if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败' }
npm run build
if ($LASTEXITCODE -ne 0) { throw '前端构建失败' }
Pop-Location
```

如果这个 Python 环境尚未安装 Easel 依赖，才执行：

```powershell
& $EaselPython -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw 'Python 依赖安装失败' }
& $EaselPython -m playwright install chromium
```

前一个命令可能安装媒体处理等较大依赖，需要网络；后一个命令下载浏览器。无需在每次启动时重复执行。

### 3.2 启动

推荐只监听本机回环地址：

```powershell
$env:EASEL_SERVICE_MODE = 'local'
& $EaselPython -m uvicorn web.app:app --host 127.0.0.1 --port 7860
```

打开 <http://127.0.0.1:7860>。终端持续占用是正常的，窗口关掉服务就停止；停止时在该终端按 `Ctrl+C`。

仓库也提供下面的标准入口：

```powershell
& $EaselPython -m easel web --port 7860
```

但它当前监听 `0.0.0.0`，意味着可能接受局域网连接；个人本机使用优先选上面的回环命令。不要配置公网端口映射，也不要把浏览器同源保护当成用户认证。

### 3.3 使用你现有的 DeepSeek Harness

你已经连通过 ACP，可以先保留已有配置，**不要重新生成或覆盖** `outputs/_harness/acp.json`。

只检查文件结构和程序是否存在，不打印密钥：

```powershell
Test-Path .\outputs\_harness\acp.json
$AcpConfig = Get-Content -Raw .\outputs\_harness\acp.json | ConvertFrom-Json
Test-Path $AcpConfig.command[0]
Test-Path $AcpConfig.command[1]
```

使用本机源码 Harness 时，配置的典型形状如下，路径需要改为实际位置：

```json
{
  "command": [
    "C:\\Program Files\\nodejs\\node.exe",
    "C:\\Users\\hyh\\Desktop\\deepseek-harness\\apps\\cli\\lib\\bin.js",
    "--profile", "acp"
  ]
}
```

它是 JSON 参数数组，不是可以粘贴进 PowerShell 的命令。Windows 优先使用 `node.exe + JS 文件`，不要把 `.cmd`/`.bat` 当成直接可执行底座。不要在这里写 API Key。

在网页设置中选择 **通用 ACP**，保存后新建会话。模型覆盖留空可沿用 Harness 原生默认值。无需先启动 Harness 的 Web 服务，Easel 会启动 ACP 子进程。

若本机 Harness 依赖 `DEEPSEEK_API_KEY`，应由原生凭证配置或安全环境提供；只检查是否存在即可：

```powershell
[bool]$env:DEEPSEEK_API_KEY
```

返回 `False` 时，可能是当前终端没继承你后来设置的环境变量。关闭旧服务，从配置已生效的新终端重新启动；不要把 Key 回显到报告，也不要认为 Harness checkout 的 `.env` 会因启动脚本位于那里而被自动加载。

### 3.4 第一次操作建议

1. 打开设置，先确认执行引擎和模型。只有 API 问答需求时，可以选“仅聊天模式”；该模式不能执行本机技能。
2. 新建会话，发送“你好，请介绍你当前可用的能力”。确认不是只显示加载动画。
3. 使用 ACP 时，再要求创建一个无外部副作用的小文件，例如“在 outputs/demo 中写一份三行的学习笔记”。核对实际文件。
4. 在同一会话继续追问，检查能否续接；不要为了重试而删除会话绑定。
5. 图片、语音等通道需要分别配置，主聊天能工作不等于所有媒体通道都已配置。
6. 初次学习用“生成草稿→预览→人工发布”；平台账号登录、收费调用和真实发布是后续独立操作。

## 4. 路线 B：Docker Desktop 单人部署

### 4.1 准备 Docker

按 [Docker 官方 Windows 安装说明](https://docs.docker.com/desktop/setup/install/windows-install/)安装 Docker Desktop，启用 WSL 2 后端并使用 **Linux containers**。这不是 Windows containers 模式。

```powershell
wsl --status
docker version
docker compose version
docker info --format '{{.OSType}}'
```

最后应显示 `linux`，`docker version` 应同时有 Client 和 Server。没有 Server 时先启动 Docker Desktop。

### 4.2 建立运维目录

代码与数据分开，避免换镜像时丢记录：

```powershell
$OpsRoot = 'C:\Code\easel-desktop-ops'
$DataRoot = 'C:\Code\easel-desktop-data'
New-Item -ItemType Directory -Force $OpsRoot | Out-Null
foreach ($Part in @('outputs', 'profiles', 'home', 'apps')) {
    New-Item -ItemType Directory -Force (Join-Path $DataRoot $Part) | Out-Null
}
```

在 `$OpsRoot` 新建 `Dockerfile.local`，复制下面内容。**这是本文的单人镜像示例，不是多人 worker 镜像。** 它默认没有安装 DeepSeek Harness/其它 Agent CLI；先用网页“仅聊天模式”验证。

```dockerfile
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
```

这里把 Chromium 安装在不会被 home/app 挂载遮蔽的 /opt/playwright；启动的 umask 077 保护新建凭证文件。运行时仍需下载/访问模型和所选服务。首次构建会比较慢。它是可信单人的 local 环境，不能被描述为已经满足多人容器沙箱要求。

构建并固定本机镜像 ID：

```powershell
Set-Location $EaselRoot
docker build -f (Join-Path $OpsRoot 'Dockerfile.local') -t easel-local:learning .
if ($LASTEXITCODE -ne 0) { throw '镜像构建失败，请先看最后的错误' }
$LocalImage = (docker image inspect easel-local:learning --format '{{.Id}}').Trim()
$AppRoot = Join-Path $DataRoot 'apps\290d94a'
if (Test-Path -LiteralPath $AppRoot) { throw '应用目录已存在，请使用新的版本目录，避免覆盖' }
New-Item -ItemType Directory -Path $AppRoot | Out-Null
$SeedName = 'easel-desktop-seed-' + [guid]::NewGuid().ToString('N')
docker create --name $SeedName $LocalImage | Out-Null
if ($LASTEXITCODE -ne 0) { throw '创建专用初始化容器失败' }
docker cp "${SeedName}:/opt/easel/." $AppRoot
if ($LASTEXITCODE -ne 0) { throw "复制应用失败；保留 $SeedName 便于排查" }
docker rm $SeedName | Out-Null
```

这一步只从**未启动的专用容器**取出应用和前端。完整应用目录也持久化，是因为网页保存 `.env` 时要在同目录创建临时文件并 rename；不能把单个 `.env` 文件 bind mount 到容器根路径，否则原子保存会失败。它是单人可信工作目录，不是多人只读代码根。

在 `$OpsRoot` 新建 `compose.local.yaml`：

```yaml
name: easel-desktop-local
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
```

```powershell
$env:EASEL_LOCAL_IMAGE = $LocalImage
$env:EASEL_DATA_ROOT = $DataRoot.Replace('\', '/')
$env:EASEL_APP_ROOT = $AppRoot.Replace('\', '/')
[pscustomobject]@{
    image = $env:EASEL_LOCAL_IMAGE
    dataRoot = $env:EASEL_DATA_ROOT
    appRoot = $env:EASEL_APP_ROOT
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $OpsRoot 'desktop-state.json') -Encoding UTF8
docker compose -f (Join-Path $OpsRoot 'compose.local.yaml') config --quiet
if ($LASTEXITCODE -ne 0) { throw 'Compose 配置校验失败' }
docker compose -f (Join-Path $OpsRoot 'compose.local.yaml') up -d
docker compose -f (Join-Path $OpsRoot 'compose.local.yaml') logs --tail 80
```

打开 <http://127.0.0.1:7860>。如果本机路线 A 还在运行，先按 `Ctrl+C` 停掉它，或把 Compose 左侧宿主端口改为 `7870`。

**不能直接搬 Windows ACP 配置：** 容器看不到 `C:\...\node.exe`，Windows 的 `node_modules` 也不等于 Linux 依赖。Docker 路线先使用仅聊天模式；确实需要 Agent 时，必须在镜像中安装/构建对应的 Linux 版 Harness，再把 argv 改为容器内路径。不要把本机 HOME 整体挂给容器。你现有 Windows Harness 的完整使用仍以路线 A 最直接。

有可见浏览器窗口要求的平台登录，在无桌面的容器里未必可用；Docker 不会自动把 Linux 浏览器窗口弹到 Windows 桌面。

## 5. 日常运维

本机路线：重新打开终端、设置第 2 节变量，执行第 3.2 节启动命令。Docker 路线每次新开终端先恢复记录，不必重新 build：

```powershell
$OpsRoot = 'C:\Code\easel-desktop-ops'
$State = Get-Content -Raw (Join-Path $OpsRoot 'desktop-state.json') | ConvertFrom-Json
$DataRoot = $State.dataRoot
$AppRoot = $State.appRoot
$LocalImage = $State.image
$env:EASEL_LOCAL_IMAGE = $LocalImage
$env:EASEL_DATA_ROOT = $DataRoot
$env:EASEL_APP_ROOT = $AppRoot
```

然后启停或查日志：

```powershell
docker compose -f (Join-Path $OpsRoot 'compose.local.yaml') ps
docker compose -f (Join-Path $OpsRoot 'compose.local.yaml') logs --tail 100
docker compose -f (Join-Path $OpsRoot 'compose.local.yaml') restart easel
docker compose -f (Join-Path $OpsRoot 'compose.local.yaml') stop easel
docker compose -f (Join-Path $OpsRoot 'compose.local.yaml') start easel
```

这些命令只操作本文这套 Compose。停止容器不会删除宿主数据；删除容器也不等于删除绑定目录。不要用全机 `docker system prune` 解决普通启动错误。

查看端口：

```powershell
Get-NetTCPConnection -LocalPort 7860 -State Listen -ErrorAction SilentlyContinue |
    Select-Object LocalAddress, LocalPort, OwningProcess
```

只定位进程，不批量结束 Python/Node。发给别人排障的日志需要先删掉 Key、Cookie、邀请和本机私密内容。

## 6. 备份与恢复

### 6.1 单人本机目录备份

先等任务完成并停止 Web。下面保留原目录，不做镜像式删除同步：

```powershell
$BackupRoot = Join-Path 'C:\Code\easel-backups' (Get-Date -Format 'yyyyMMdd-HHmmss')
New-Item -ItemType Directory -Force $BackupRoot | Out-Null
foreach ($Part in @('outputs', 'profiles')) {
    $Source = Join-Path $EaselRoot $Part
    if (Test-Path -LiteralPath $Source) {
        robocopy $Source (Join-Path $BackupRoot $Part) /E /XJ /COPY:DAT /R:1 /W:1
        if ($LASTEXITCODE -ge 8) { throw "备份 $Part 失败" }
    }
}
if (Test-Path -LiteralPath (Join-Path $EaselRoot '.env')) {
    Copy-Item -LiteralPath (Join-Path $EaselRoot '.env') -Destination $BackupRoot
}
```

`robocopy` 的 0–7 通常不是失败，8 及以上才按失败处理；`/XJ` 不跟随 junction，若你主动把数据放在 junction 后面，要单独登记并备份真实数据。备份可能包含 API Key 和平台凭证，只放本人受保护目录或加密备份盘。

Harness 的原生会话/凭证还可能在当前用户的 `.dsh` 或所选 Agent 原生目录，必须按所选 Harness 的备份方式另外保留；**不要认为 outputs 一份备份包含了所有原生历史**。local 前端聊天展示也可能在当前浏览器存储里，不要随便清理站点数据。

恢复时先复制备份到一个新目录核对，停止服务后再手工切换，保留原目录作为回滚副本；不要直接覆盖正在写入的 outputs。

### 6.2 Docker Desktop 数据备份

```powershell
docker compose -f (Join-Path $OpsRoot 'compose.local.yaml') stop easel
$DesktopBackup = Join-Path 'C:\Code\easel-backups' ('desktop-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
robocopy $DataRoot $DesktopBackup /E /XJ /COPY:DAT /R:1 /W:1
if ($LASTEXITCODE -ge 8) { throw 'Docker 数据备份失败，先不要升级' }
Copy-Item -LiteralPath (Join-Path $OpsRoot 'desktop-state.json') -Destination $DesktopBackup
docker compose -f (Join-Path $OpsRoot 'compose.local.yaml') start easel
```

恢复演练使用 `$env:EASEL_DATA_ROOT` 指向**备份的另一个副本**，同时把 `$env:EASEL_APP_ROOT` 指向该副本中对应的 apps/版本目录；不要直接用唯一备份运行服务。保留原镜像 ID 和 Compose 文件，改项目名与宿主端口，避免动到原服务。容器重启不会补回未挂载的文件，重要数据只能依赖明确登记的持久目录。

## 7. 以后怎样升级和回滚

### 本机代码升级

1. 等任务结束，停止 Web，执行第 6 节备份；记下当前 `git rev-parse HEAD`。
2. 执行 `git status --short`。有自己的修改时先单独保存，不使用 `git reset --hard` 或 `git clean -fd` 清场。
3. `git fetch origin` 只更新远端引用，不直接换代码。确认维护者给出的目标提交属于你的功能分支。
4. 工作树干净后，才切到确认过的版本：

```powershell
$TargetRevision = '填维护者给出的完整提交SHA'
if ($TargetRevision -eq '填维护者给出的完整提交SHA') { throw '先填写目标版本' }
git switch --detach $TargetRevision
if ($LASTEXITCODE -ne 0) { throw '切换版本失败' }
& $EaselPython -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw 'Python 依赖安装失败' }
Push-Location .\web\frontend
npm ci
if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败' }
npm run build
if ($LASTEXITCODE -ne 0) { throw '前端构建失败' }
Pop-Location
```

5. 再检查 ACP 路径、`doctor`、实际新会话和连续追问；不要在已有会话中静默换 Harness 或 native ID。

失败时停止新服务，切回记录的旧 SHA，重装旧依赖/重建前端。数据格式若已变化，应连同旧数据副本一起恢复；**回滚代码不等于回滚数据库或原生历史**。升级前保存 `pip freeze` 输出可帮助重建环境，不要把它当成已经审过的依赖锁文件。

### Docker 镜像升级

不能只换 image：`/opt/easel` 挂着宿主应用目录，里面也必须换成新代码。按顺序执行：

1. 停服务并执行第 6.2 节备份，复制 desktop-state.json 为单独的旧版本记录。
2. 用新代码 build 新 tag，取 `$NewImage`；按第 4.2 节的专用 seed 容器办法把**新镜像**应用复制到新的 `$NewAppRoot`，不覆盖旧目录。
3. 将旧 `$AppRoot/.env` 复制到新 `$NewAppRoot/.env`，保留 outputs/profiles/home 的原挂载。
4. 更新 EASEL_LOCAL_IMAGE、EASEL_APP_ROOT 并重写 desktop-state.json，`docker compose ... up -d`。
5. 验证新会话与续接。需要回滚时恢复旧 state 中的 image/appRoot；数据已不兼容时一起切换到升级前副本。

第 2–4 步可使用以下命令；先确认当前源码已切到新版本，并且第 6.2 节备份成功：

```powershell
Set-Location $EaselRoot
$NewVersion = (git rev-parse --short HEAD).Trim()
$NewAppRoot = Join-Path $DataRoot "apps\$NewVersion"
if (Test-Path -LiteralPath $NewAppRoot) { throw '该版本应用目录已存在，先核对，不覆盖' }
docker build -f (Join-Path $OpsRoot 'Dockerfile.local') -t "easel-local:$NewVersion" .
if ($LASTEXITCODE -ne 0) { throw '新镜像构建失败' }
$NewImage = (docker image inspect "easel-local:$NewVersion" --format '{{.Id}}').Trim()
docker compose -f (Join-Path $OpsRoot 'compose.local.yaml') stop easel
Copy-Item -LiteralPath (Join-Path $OpsRoot 'desktop-state.json') -Destination (Join-Path $OpsRoot 'desktop-state.before-upgrade.json')
New-Item -ItemType Directory -Path $NewAppRoot | Out-Null
$SeedName = 'easel-desktop-upgrade-' + [guid]::NewGuid().ToString('N')
docker create --name $SeedName $NewImage | Out-Null
if ($LASTEXITCODE -ne 0) { throw '初始化容器创建失败' }
docker cp "${SeedName}:/opt/easel/." $NewAppRoot
if ($LASTEXITCODE -ne 0) { throw '新应用复制失败，先不要启动' }
docker rm $SeedName | Out-Null
Copy-Item -LiteralPath (Join-Path $AppRoot '.env') -Destination (Join-Path $NewAppRoot '.env')
$env:EASEL_LOCAL_IMAGE = $NewImage
$env:EASEL_APP_ROOT = $NewAppRoot.Replace('\', '/')
[pscustomobject]@{ image=$NewImage; dataRoot=$env:EASEL_DATA_ROOT; appRoot=$env:EASEL_APP_ROOT } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $OpsRoot 'desktop-state.json') -Encoding UTF8
docker compose -f (Join-Path $OpsRoot 'compose.local.yaml') up -d
```

技能可能在应用目录中保留本地旧格式配置；整份 apps 已纳入备份，但不保证新版本自动迁移这些旧配置。逐项复核，必要时重新登录平台，不把整棵旧代码覆盖新应用。不要删除旧镜像和唯一数据备份后再测试升级是否成功。

## 8. 常见问题

| 现象 | 先做什么 |
|---|---|
| ModuleNotFoundError | 检查是否用了 `$EaselPython`，不是系统 Python；确认 `pip install -e .` 在该环境执行 |
| 端口占用 | 查 `Get-NetTCPConnection`；停止自己那套服务或换端口 |
| 页面空白/旧界面 | 检查 dist/index.html；重新构建后刷新浏览器，不先清聊天存储 |
| ACP 找不到程序 | 核对 argv 的程序和 JS 路径；在同一账户/终端启动 |
| Key 改了仍报缺少 | 新终端重启 Web，确认只返回布尔值的环境检查；不把 Key 写进 argv |
| 只有 API 回复但不能执行技能 | 你可能选了“仅聊天模式”，需要可用且已认证的 Agent 底座 |
| Docker 下载超时 | 检查 Docker Desktop 网络与代理；失败就停，不用不明镜像替换正式配置 |
| 看到 capability_unavailable/503 | 先确认用的是第 9 节完整 Desktop 镜像/启动命令；仅登录旧编排或未配置受管 provider 会 503。不要靠关闭认证或改 local 冒充修好 |

## 9. 完整 Desktop 多人：已装配的正式路线

本节对应 A2–A7 已装配并实际演练的完整 Desktop 路线：control + 签名 gateway + WSL provisioner + 每 workspace worker/sidecar/broker + 真实 ACP Harness。它是**功能与运维范围**的验收，不是 Linux 生产发布验收。

### 9.1 当前事实与边界

- 聊天、权限允许/拒绝、停止、SSE 回放、control/gateway/worker 重启续接、A/B 隔离、取消清理互斥已由真实 Harness + 受控 provider + 浏览器/API 验证。
- 上游 provider 在演练中使用受控 TLS fake provider；真实 DeepSeek 请求仍为 **not-run**。不要把本手册的受控 provider 成功写成真实供应商成功。
- ngrok 公网 HTTPS 证据沿用 A6；本轮 A7 未重做整套公网浏览器矩阵。
- Linux 目标宿主的 XFS 配额、iptables/nftables、IPv6、Chromium 沙箱等生产门槛仍未验收；Docker Desktop 成功不能替代它们。
- 所有命令在 PowerShell 执行；每次新终端重新设置本节变量。不要复制开发者 `.env`、HOME、浏览器 profile 或任何真实 Key 到本流程。

### 9.2 变量与前置条件

```powershell
Set-Location C:\Code\Easel
$EaselPython = 'C:\Users\hyh\anaconda3\envs\Agent\python.exe'
$ProjectName = 'easel-desktop-multi'
$DataRoot = 'C:\Easel\desktop'
$WorkspaceRoot = 'C:\Easel\workspaces'
$Port = 7861
$PublicOrigin = 'https://替换为你的-ngrok-域名'
$ControlImage = 'sha256:替换为当前源码构建的-control-镜像'
$WorkerImage = 'sha256:e48eb63b6155b6fc14c37a2525d138e7419f7a8024f46a58d209c40d3a5bd993'
$GatewayImage = 'sha256:c0a35aaa9f304c47f9f5c3e73add7d9d5920041baef156e2ea674f3000c5580a'
```

前置：

```powershell
docker version
docker compose version
docker info --format '{{.OSType}}'          # 必须是 linux
wsl --status
wsl -d Ubuntu-26.04 -- python3 -c "import cryptography"  # provisioner 依赖
Test-Path .\web\frontend\dist\index.html    # 构建 control 镜像前可为 False，镜像内前端由 Dockerfile 构建
```

Docker Desktop 使用 Linux containers；WSL 发行版默认 `Ubuntu-26.04`。若不同，后续每个脚本都要显式加 `-Distro`、`-WslRepo`。

### 9.3 构建并固定 control 镜像

```powershell
$ImageTag = 'easel-control:a7'
docker build -f .\deploy\desktop\Dockerfile.control -t $ImageTag .
if ($LASTEXITCODE -ne 0) { throw 'control 镜像构建失败' }
$ControlImage = (docker image inspect $ImageTag --format '{{.Id}}').Trim()
$ControlImage
```

构建期如果 npm/pip 下载慢，可在 Docker Desktop 设置代理，或按本机代理方式传入：

```powershell
docker build --build-arg HTTP_PROXY=http://host.docker.internal:7897 `
  --build-arg HTTPS_PROXY=http://host.docker.internal:7897 `
  -f .\deploy\desktop\Dockerfile.control -t $ImageTag .
```

worker/gateway 必须是已验收的固定摘要，不能只写浮动的 tag：

```powershell
docker image inspect $WorkerImage --format '{{.Id}}'
docker image inspect $GatewayImage --format '{{.Id}}'
```

两行输出必须分别等于变量值。worker 镜像缺失时，先按仓库 A5/A6 证据中的构建说明重建，不要用任意新 tag 冒充。

### 9.4 受管 Agent 配置、出口策略与 CA

新建两个**不含任何供应商 Key** 的 JSON 文件。`revision` 每次有意变更时递增；`command` 来自锁定的 ACP profile：

```powershell
$ConfigDir = Join-Path $DataRoot 'config'
New-Item -ItemType Directory -Force $ConfigDir | Out-Null
$ManagedConfig = Join-Path $ConfigDir 'managed-agent.json'
@'
{
  "revision": 1,
  "agent": {
    "runtime": "acp",
    "command": ["node", "/opt/dsh/apps/cli/lib/bin.js", "--profile", "acp", "--patch", "/opt/easel/managed/acp.patch.yml"],
    "sandbox": "workspace-write",
    "timeout": 300
  },
  "broker": {"mode": "workspace-sidecar", "channel": "chat"}
}
'@ | Set-Content -LiteralPath $ManagedConfig -Encoding UTF8

$EgressPolicy = Join-Path $ConfigDir 'egress-policy.json'
@'
{"approved_hosts": ["你的受控-provider-域名"], "approved_cidrs": []}
'@ | Set-Content -LiteralPath $EgressPolicy -Encoding UTF8
```

- `managed-agent.json` 只允许 `revision/agent/broker`；含 `secret/api_key/token/password` 等字段会被拒绝。
- `egress-policy.json` 只允许 `approved_hosts/approved_cidrs`。
- 使用企业/私有 CA 时，把 CA PEM 作为 `-EgressCaFile` 传给 Start；省略时使用 control 镜像内的系统 CA bundle。CA 会复制到 key 卷并随 v2 备份保存，不包含私钥。

### 9.5 初始化专用卷、网络与 key

```powershell
.\deploy\desktop\Initialize-WorkspaceEasel.ps1 `
  -Image $ControlImage `
  -ManagedAgentConfig $ManagedConfig `
  -EgressPolicy $EgressPolicy `
  -ProjectName $ProjectName `
  -DataRoot $DataRoot `
  -WorkspaceRoot $WorkspaceRoot
```

首次预期包含 `key=created,signing_key=created`；重复执行应为 `key=exists,signing_key=exists`。创建的资源：

- `$ProjectName-control`：control SQLite 卷
- `$ProjectName-keys`：master.key、Ed25519 私钥、派生公钥、受管配置、出口策略、CA
- `$ProjectName-egress`：出口策略副本
- `$ProjectName-control` 网络
- `$DataRoot\public`：worker/gateway 公钥和策略副本
- `$WorkspaceRoot`：workspace 持久根

重复初始化不得换 key；卷/网络已有外来标签时以 `volume_not_owned`/`network_not_owned` 拒绝，不会 chown 接管。

### 9.6 启动并接 ngrok HTTPS

先启动 ngrok（独立窗口，不要读取/打印 authtoken）：

```powershell
ngrok http http://127.0.0.1:7861 --inspect=false
```

把输出的 HTTPS Forwarding 地址填入 `$PublicOrigin`，然后：

```powershell
.\deploy\desktop\Start-WorkspaceEasel.ps1 `
  -PublicOrigin $PublicOrigin `
  -Image $ControlImage `
  -WorkerImage $WorkerImage `
  -GatewayImage $GatewayImage `
  -ProjectName $ProjectName `
  -Port $Port `
  -DataRoot $DataRoot `
  -WorkspaceRoot $WorkspaceRoot
```

预期输出包含：

```text
started project=... origin=... port=... control=sha256:... worker=sha256:... gateway=sha256:...
deployment_file=...\logs\deployment.json
config_sha256=<64位>
frontend_sha256=<64位>
```

Start 会先核验固定镜像、卷/网络归属、key 指纹、公钥副本、策略副本、前端、WSL 依赖和 provisioner 身份；失败时不会启动替代实例。`config_sha256` 是 stop/reopen 与升级协议的配置身份。

### 9.7 管理员、邀请、登录、聊天、权限与停止

首次管理员（只执行一次）：

```powershell
$ControlContainer = (docker ps -q `
  --filter "label=com.docker.compose.project=$ProjectName" `
  --filter 'label=com.docker.compose.service=control').Trim()
docker exec $ControlContainer python -m easel admin bootstrap `
  --control-dir /srv/easel/control --name Owner
```

输出中的 `invite=` 只出现一次，复制后立即使用；不要写入报告或截图。打开 `$PublicOrigin`，粘贴邀请登录。管理员在“成员与邀请”创建用户并签发邀请；每个用户使用独立浏览器 profile/context，不能只用同一浏览器两个标签页。

验证聊天/权限/停止：

1. 管理员发布聊天模型配置（受管 provider/channel；不要填真实 Key 到公开文件）。
2. 用户新建会话发送消息；预期流式事件、助手回复、历史刷新后仍在。
3. 需要权限的任务选择允许/拒绝；拒绝后任务不应继续执行。
4. 点击停止；预期服务端 Run 进入 `cancelled` 或失败终态、临时 token 撤销；随后同会话新消息可继续，不出现“取消清理期间 409 误挡”或重叠执行。
5. 两个用户互相看不到会话；跨空间读取应为 404/403。

### 9.8 日常 start/stop/status/logs

```powershell
# 只读诊断：网络/卷/control/端口/挂载/key/lease/health/egress/provisioner/version
.\deploy\desktop\Status-WorkspaceEasel.ps1 `
  -ProjectName $ProjectName -DataRoot $DataRoot -Port $Port

# 查看最近日志（control/egress/provisioner；不读取 key）
.\deploy\desktop\Logs-WorkspaceEasel.ps1 `
  -ProjectName $ProjectName -DataRoot $DataRoot -Tail 200

# 停止：先建屏障、drain、确认 Run/请求/实例收口，再 down，最后写完成记录
.\deploy\desktop\Stop-WorkspaceEasel.ps1 `
  -ProjectName $ProjectName -DataRoot $DataRoot

# 重开：沿用同一 key/数据卷；只有匹配的成功 Stop 记录才自动重开
.\deploy\desktop\Start-WorkspaceEasel.ps1 `
  -PublicOrigin $PublicOrigin -Image $ControlImage `
  -WorkerImage $WorkerImage -GatewayImage $GatewayImage `
  -ProjectName $ProjectName -Port $Port `
  -DataRoot $DataRoot -WorkspaceRoot $WorkspaceRoot
```

`Status` 的 `status=ok` 才表示全部检查通过；`version_*` 是实际镜像/前端/配置/源码/Harness 摘要，`version_schema` 是数据库 schema。`Logs` 只输出日志路径和尾部，不打印 key/邀请/Cookie。

### 9.9 完整备份与只读校验

备份只能在成功 Stop 之后：

```powershell
.\deploy\desktop\Backup-WorkspaceEasel.ps1 `
  -ProjectName $ProjectName `
  -Image $ControlImage `
  -DataRoot $DataRoot `
  -WorkspaceRoot $WorkspaceRoot

$Backup = (Get-ChildItem (Join-Path $DataRoot 'backups') -Directory |
  Sort-Object LastWriteTime | Select-Object -Last 1).FullName
.\deploy\desktop\Verify-WorkspaceEasel.ps1 -Backup $Backup -Image $ControlImage
```

备份包含：control DB 一致快照、master.key、Ed25519 私钥及派生公钥、受管 Agent 配置、出口策略、CA、各 workspace 的 `workspace`（含 `state`）与 `HOME`。明确排除 `.credentials` 与 `.gateway`（每部署重新生成），不备份 socket/PID。

`Backup` 会先读取 `$DataRoot\logs\deployment.json`，要求传入的 `-DataRoot`/`-WorkspaceRoot` 与已启动部署登记值指向同一物理目录；错误空根、重解析点或路径别名在启动备份容器前失败。v2 完整备份必须提供真实 workspace 根；已有 Run 历史的 workspace 缺目录时失败，尚未物化的新 workspace 才允许为空。

预期 `version=2`、`clean_stop=true`、`workspaces=<数量>`、`verified_backup=...`。备份目录在 `$DataRoot\backups\<时间戳>`，Windows ACL 只授予当前用户/SYSTEM/Administrators；不要放进 Git 或公开目录。`Verify` 真正只读：逐项核对清单中全部文件的大小/SHA-256，并在私有数据库副本上检查 SQLite/密文，不创建 `-wal`/`-shm`、不生成 key、不改备份；连续执行两次结果一致。

### 9.10 恢复到新目录/新项目

源部署保持停止，目标必须是全新项目/卷/网络/目录/端口：

```powershell
$RestoredProject = 'easel-desktop-restored'
$RestoredPort = 7862
$RestoredOrigin = 'https://替换为恢复实例的-ngrok-域名'
$RestoredData = 'C:\Easel\desktop-restored'
$RestoredWorkspaces = 'C:\Easel\workspaces-restored'

.\deploy\desktop\Restore-WorkspaceEasel.ps1 `
  -Backup $Backup `
  -ProjectName $RestoredProject `
  -PublicOrigin $RestoredOrigin `
  -Image $ControlImage `
  -WorkerImage $WorkerImage `
  -GatewayImage $GatewayImage `
  -Port $RestoredPort `
  -DataRoot $RestoredData `
  -WorkspaceRoot $RestoredWorkspaces
```

预期 `restored_project=...`、`restored_schema=...`、`restored_workspaces=...`、`recovery_invite_file=...`。恢复会：

- 只读校验备份，拒绝错 key、缺文件、改摘要、未来 schema、非空目标、外来卷/路径逃逸；
- 创建新卷/网络，把恢复材料落到新目标，不覆盖源；
- `prepare-restore` 先按原清单校验整个 staging；数据库或 workspace 已被篡改时拒绝，DB/manifest/文件集合均不变。只有合法输入才修改 staging 控制库并更新 staging 清单的来源摘要，**原备份不变**；`materialize-restore` 再核对输入与实际复制字节，失败只清理本次目标；
- 清除旧屏障/旧 lease 身份、撤销旧登录/未兑换邀请/Run token，签发一次性管理员恢复邀请；
- 保留 workspace ID、消息和原生会话绑定（容器内 cwd 仍是 `/workspace`）。

读取恢复邀请文件后登录管理员，重新给成员签发邀请。旧 Cookie 应 401；新 Origin 不匹配的写请求应 403。恢复副本启动后先验证 A/B 历史、配置解密、sentinel 和同会话新 Harness 消息；确认后先 Stop 恢复副本，才允许源再次 Start。

### 9.11 固定版本升级与失败回滚

升级顺序（不能跳过备份）：

1. 记录旧版本：`Status-WorkspaceEasel.ps1` 的 `version_*`。
2. `Stop-WorkspaceEasel.ps1` 完成 drain/停止。
3. `Backup-WorkspaceEasel.ps1` + `Verify-WorkspaceEasel.ps1`。
4. 核验目标镜像存在且摘要匹配；可先用坏镜像演练失败（应 `worker_or_gateway_image_mismatch`，数据/Key 不变、屏障保持）。
5. 用新镜像启动，并显式提供匹配备份：

```powershell
.\deploy\desktop\Start-WorkspaceEasel.ps1 `
  -PublicOrigin $PublicOrigin `
  -Image $NewControlImage `
  -WorkerImage $NewWorkerImage `
  -GatewayImage $NewGatewayImage `
  -ProjectName $ProjectName -Port $Port `
  -DataRoot $DataRoot -WorkspaceRoot $WorkspaceRoot `
  -UpgradeFromBackup $Backup
```

预期出现 `upgraded_from_backup=...` 与 `admission_barrier_reopened=...`。升级协议只接受旧 Stop 完成记录、旧配置摘要与备份 manifest 完全匹配；不会无条件 `resume`。在 **compose up 新 control 之前**，`Start` 会通过只读副本读取当前 barrier/Stop 完成记录与 key 指纹，要求备份 `runtime_state.stop` 精确匹配**当前同一轮 Stop**（屏障/项目/旧配置/完成时间）且 key 来源一致；再用只读容器执行完整 `verify-backup`。旧备份、只有 manifest、缺 key/DB、损坏数据或错版本都会在新程序打开/迁移正式 DB 之前失败。随后才启动 control、读取 stop 完成记录并执行 `upgrade-complete`。启动后验证登录、历史、配置解密和同会话新 Harness 消息。

回滚：不要在原卷上强行降 schema。用升级前的匹配备份按 9.10 恢复到新项目/目录，使用旧镜像启动，验证历史和新消息，再停止。

没有新 schema 版本时，上述是**流程 + worker 代码版本切换演练**，不能写成 schema 升级成功。旧程序拒绝未来 schema、整套备份恢复边界由 `tests/multiuser/test_operations_a7.py` 的隔离用例覆盖。

### 9.12 数据位置与哪些命令会改数据

| 位置 | 内容 | 是否可丢 |
|---|---|---|
| `$ProjectName-control` 卷 | control.sqlite3、登录/邀请/Run 元数据、stop 记录 | 不可丢；靠备份 |
| `$ProjectName-keys` 卷 | master.key、snapshot.pem、派生公钥、managed-agent.json、egress-policy.json、egress-ca.pem | 不可丢；缺私钥不能“重新生成修复” |
| 初始化状态 | `Initialize` 先只读探测 control DB 与 key 卷；已有 DB/身份缺 master/signing 时拒绝且不生成新 key，仅全新空部署创建初始材料 | 不要删除 key 后重跑 Initialize 试图“修复” |
| `$ProjectName-egress` 卷 | 出口策略副本 | 可重建，但备份保留 |
| `$WorkspaceRoot\<id>\workspace` | 用户工作区（容器 `/workspace`），含 `state` | 不可丢 |
| `$WorkspaceRoot\<id>\home` | 用户 HOME（容器 `/home/easel`），含 `.dsh` 原生历史 | 不可丢 |
| `$WorkspaceRoot\.credentials`、`.gateway` | worker service token、启动 attempt | 运行期材料，不备份；新部署重建 |
| `$DataRoot\public` | worker/gateway 公钥、策略副本 | 可从 key 卷重建 |
| `$DataRoot\logs` | provisioner/control 日志、deployment.json | 可丢；deployment.json 用于备份/升级身份 |
| `$DataRoot\backups` | 完整 v2 备份 | 受 ACL 保护，按保留策略另存 |
| `$DataRoot\recovery` | 一次性管理员恢复邀请 | 用后删除，不入 Git/证据 |

会修改数据：`Initialize`（创建卷/key/配置）、`Start`（运行期 lease/日志/deployment.json）、`Stop`（屏障/完成记录、停止容器）、`Backup`（只读源、写新备份）、`Restore`（写新目标、撤销恢复副本登录/token）、`Upgrade Start`（核对备份后改写完成记录）。`Verify`、`Status`、`Logs` 只读。

### 9.13 常见错误与处理

| 错误码 | 含义 | 处理 |
|---|---|---|
| `volume_not_owned` / `network_not_owned` | 同名资源无本项目标签 | 换项目名或人工确认归属；不要删开发者资源 |
| `egress_ca_unavailable` | control 镜像内找不到系统 CA 且未提供 CA | 传 `-EgressCaFile` 或换含 CA bundle 的镜像 |
| `backup_requires_stopped` | 备份时 control 仍在运行 | 先 Stop；不要热备 |
| `backup_requires_clean_stop` | 无匹配停止完成记录 | 走正式 Stop，不要手工清屏障 |
| `backup_integrity_failed` | 文件/摘要/派生公钥/DB 校验失败 | 不要恢复；重新取备份或排查篡改 |
| `restore_target_exists` / `restore_target_not_empty` | 目标卷/目录已有内容 | 换全新项目/目录；不要覆盖 |
| `restore_target_same_project` | 目标项目等于源项目 | 换新项目名 |
| `admission_barrier_active` | 旧 Stop 记录与当前配置不匹配 | 用匹配备份走 `-UpgradeFromBackup`；不要无条件 resume |
| `upgrade_stop_completion_mismatch` | 备份/旧屏障/旧摘要不一致 | 重新 Stop+Backup，或换正确备份 |
| `upgrade_deployment_identity_missing` / `upgrade_deployment_mismatch` | 缺旧 deployment.json 或备份与旧实例/项目/镜像不一致 | 用当前实例生成的备份与 deployment.json 重试 |
| `worker_or_gateway_image_mismatch` | worker/gateway 摘要缺失或不匹配 | 核验固定镜像；修复后重试 |
| `control_port_in_use` | 无既有 control 时回环端口已被其它进程占用 | 停掉占用进程或换 `-Port`；不要杀未知进程 |
| `control_health_timeout` / `control_unhealthy` | control 未健康 | 看 `Logs` 和 `docker inspect`；不要自动换替代实例 |

### 9.14 两个独立验收维度

**Desktop 功能/运维实际通过（本机 Windows + Docker Desktop）**

- 初始化、两轮 start/stop、Status/Logs/版本摘要、A/B 登录与隔离、真实 Harness 聊天、权限允许/拒绝、停止与续接。
- v2 完整备份、只读 verify、新目录/新项目恢复、旧 Cookie/邀请/token 撤销、sentinel/历史/配置解密/同会话新消息。
- 受控版本切换、坏镜像失败、匹配备份升级、旧镜像回滚恢复、源/备份字节不变。
- 具体镜像、摘要、命令输出和计数见 `docs/evidence/desktop-a7/`。

**production-release 仍未通过（不因本机 Desktop 变绿）**

- 真实 DeepSeek 供应商请求：not-run（无批准受管 channel/凭证；未读取开发者 `.env`/HOME）。
- Linux 目标宿主/XFS 配额/iptables/nftables/IPv6/Chromium 沙箱：not-run。
- 错误公钥 UI、运行期缺 broker UI 反例：沿用 A6 未单独执行边界。
- 原发布检查器固定阻断项保持 blocked；不修改检查器绕过。
