# Easel Windows 本机与 Docker Desktop 使用运维手册

适合读者：会运行 Python，但不熟悉 Docker、服务器和运维的开发者。核对基线：`290d94a68b9a4c1e8e3f627b5c96fcbdb8f11f1b`，2026-10-26。

本文把“Desktop”理解为 **Windows 上的 Docker Desktop**；Easel 当前的操作界面仍是浏览器，不是另一个独立 Windows 安装包。只读这份手册即可先在本机使用；服务器部署见[Linux Docker 手册](linux-docker-operations.zh-CN.md)。

## 1. 先选一条路线

| 路线 | 你会得到什么 | 适合谁 |
|---|---|---|
| A：Windows Python 本机运行 | 单人完整工作台，直接使用本机已配置的 DeepSeek Harness/ACP | **你当前优先用这条**，已有 Harness 无需搬迁 |
| B：Docker Desktop 单人运行 | 独立 Linux 容器、浏览器工作台；默认先配置“仅聊天模式” | 想学习容器、备份和升级的人 |
| C：Docker Desktop 多用户登录管理面 | ngrok HTTPS、邀请登录、成员与权限、模型设置 | 本机继续验证多用户，见第 9 节 |
| 完整多人 control/worker | 邀请、成员、配置、服务端历史等代码已验收，但完整生产装配和正式隔离未完成 | 暂不作为小白“一键上线”路线 |

重要区别：

- `easel web` 是 **local 单人模式**，没有多人邀请登录边界。不能把它直接开放给团队或公网。
- `easel serve-control` 是 **多人控制面**。仅验证邀请登录、成员管理和模型设置，需要 HTTPS 与 master key；执行聊天任务还需要 provisioner、实际 worker 和任务签名等配套。它不是给 `web` 换一个端口。
- 当前多人 `/api/outputs` 等工作区业务仍有 503；现有 Docker worker 默认启动 lifecycle 健康服务，不等于已经启动聊天 Web worker。不能靠本文把缺少的产品装配变成可用。
- Docker Desktop 的测试结果不等于 Linux 多人发布验收；正式检查器仍有固定 `blocked` 证据项。

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
| 看到 capability_unavailable/503 | 如果访问的是多人 control，这是未装配能力，不应靠关闭认证或改成 local 冒充修好 |

## 9. Windows 上的多人验证做到哪一步

**A1 结论（2026-10-26）**：已交付可独立使用的登录管理面，包含当前源码和当前前端的固定 control 镜像、专用 Compose、PowerShell 初始化/启动/诊断脚本。真实 Docker Desktop + ngrok HTTPS + Chromium 已验证管理员 bootstrap、邀请单次兑换、A/B 独立浏览器 context、权限与 403/404、会话目录隔离、注销/禁用，以及重开同一数据卷后的重新登录。

这不是完整多人聊天环境：未启动执行 worker，聊天/历史请求由 control 明确返回 `503 capability_unavailable`；原 Linux 防火墙/IPv6、XFS 配额、最终 runtime/真实 DSH 等正式发布门槛仍未通过。

### 9.1 边界与前提

- 访问链路：浏览器 → ngrok 公网 HTTPS → Windows `127.0.0.1:7861` → Docker Desktop 的 control 容器。
- control 容器只以 UID/GID `10001:10001` 运行，只读根文件系统、`cap_drop: ALL`、`no-new-privileges`，只挂 control 数据卷和 master key 卷；不挂 Docker socket、worker 根或开发者目录。
- 只发布宿主回环 `127.0.0.1:7861`；不暴露 worker、provisioner、broker 或 Docker socket。
- 需要 Docker Desktop 使用 Linux containers。下面命令在 PowerShell 执行，变量每次新终端重新设置。
- 不要复用旧 task8 测试镜像；每次都从当前源码构建 control 镜像。

```powershell
Set-Location C:\Code\Easel
$EaselPython = 'C:\Users\hyh\anaconda3\envs\Agent\python.exe'
$ProjectName = 'easel-login-desktop'
$ImageTag = 'easel-control:a1'
```

### 9.2 构建并固定 control 镜像

```powershell
docker build -f .\deploy\desktop\Dockerfile.control -t $ImageTag .
if ($LASTEXITCODE -ne 0) { throw 'control 镜像构建失败' }
$ControlImage = (docker image inspect $ImageTag --format '{{.Id}}').Trim()
$ControlImage
```

构建阶段会执行 `npm ci` 与 `npm run build`，最终镜像只复制当前 `easel` 源码、当前前端构建产物、共享模型目录和 keygen；不复制开发者环境文件。后续命令必须使用 `$ControlImage` 的 `sha256:...` ID，不能使用可移动 tag。

### 9.3 初始化专用卷与 master key

```powershell
.\deploy\desktop\Initialize-Easel.ps1 -Image $ControlImage -ProjectName $ProjectName
```

预期输出包含 `key=created`（首次）或 `key=exists`（重复运行）。脚本只管理：

- `$ProjectName-control`：control SQLite 数据卷
- `$ProjectName-keys`：master key 卷

重复执行不会覆盖 key、不会删除卷或数据库。卷创建时带 `easel.desktop.project` 与 `easel.desktop.role` 标签；如果同名卷没有本项目标签，初始化会以 `volume_not_owned` 拒绝，root 助手不会 chown 既有开发者卷。root 助手只修改这两个专用卷的目录权限；正式 control 仍以 10001 运行。不要把这两个卷换成开发者已有目录。

### 9.4 启动 ngrok 与 control

在**独立 PowerShell 窗口**启动已有 ngrok 配置；不要关闭其它项目的隧道，也不要读取或打印访问令牌：

```powershell
ngrok http http://127.0.0.1:7861 --inspect=false
```

复制输出的 HTTPS Forwarding 地址，回到部署终端：

```powershell
$PublicOrigin = 'https://替换为你的实际域名'
.\deploy\desktop\Start-Easel.ps1 `
  -PublicOrigin $PublicOrigin -Image $ControlImage -ProjectName $ProjectName
```

`Start-Easel.ps1` 会在调用 `docker compose up` 之前依次检查：Origin 形状、镜像是否为固定 `sha256`、专用卷是否存在、master key 是否可读、镜像内前端是否存在、Compose 配置是否可展开。任一检查失败都不会启动容器。

首次管理员邀请只在创建时输出一次；在 control 已启动后执行：

```powershell
$env:EASEL_CONTROL_IMAGE = $ControlImage
$env:EASEL_PUBLIC_ORIGIN = $PublicOrigin
$env:EASEL_DESKTOP_PROJECT = $ProjectName
$env:EASEL_CONTROL_VOLUME = "$ProjectName-control"
$env:EASEL_KEY_VOLUME = "$ProjectName-keys"
$env:EASEL_CONTROL_PORT = '7861'
docker compose -p $ProjectName -f .\deploy\desktop\compose.login.yaml `
  exec -T control python -m easel admin bootstrap `
  --control-dir /srv/easel/control --name Owner
```

已有管理员时不要重复 bootstrap。打开 ngrok HTTPS 地址，粘贴邀请登录；管理员在“全体默认 → 成员与邀请”创建用户并签发邀请。普通成员与管理员使用不同浏览器 profile/独立浏览器 context，不能只看同一浏览器的两个标签页。

### 9.5 只读诊断

```powershell
.\deploy\desktop\Test-Easel.ps1 `
  -PublicOrigin $PublicOrigin -Image $ControlImage -ProjectName $ProjectName
curl.exe -fsS -o NUL -w "http=%{http_code} ssl_verify=%{ssl_verify_result}`n" `
  "$PublicOrigin/api/health"
```

诊断检查实际容器：UID/GID、只读根、`cap_drop ALL`、`no-new-privileges`、非 privileged、非 host 网络、仅 control/key 两个挂载、无 Docker socket、回环端口、健康状态和 master key 存在性。输出只有检查名/状态码，不解析或回显 key、邀请、Cookie、访问令牌。

### 9.6 停止、重开与清理

停止本次服务：

```powershell
docker compose -p $ProjectName -f .\deploy\desktop\compose.login.yaml stop
# 在 ngrok 窗口按 Ctrl+C
```

重开（数据卷和 key 保留）：

```powershell
# 重新设置 9.1、9.4 的变量；重新启动 ngrok 并更新 $PublicOrigin
.\deploy\desktop\Initialize-Easel.ps1 -Image $ControlImage -ProjectName $ProjectName
.\deploy\desktop\Start-Easel.ps1 `
  -PublicOrigin $PublicOrigin -Image $ControlImage -ProjectName $ProjectName
```

重复初始化应显示 `key=exists`；不要再次 bootstrap 已有管理员。若 ngrok 地址改变，必须用新地址重新执行 `Start-Easel.ps1`，并从新地址重新登录；旧域名的 `__Host-` Cookie 不会被浏览器带到新域名。免费 ngrok 账户可能只有一个固定静态域名，不能在本机无账户变更的情况下制造第二个公开地址。

确认只清理本项目测试资源时才执行：

```powershell
docker compose -p $ProjectName -f .\deploy\desktop\compose.login.yaml down --remove-orphans
docker volume rm "$ProjectName-control" "$ProjectName-keys"
```

不要执行 `down -v`、全局 `docker system prune` 或删除其它项目的卷/容器。删除卷会永久丢失本次登录数据；备份必须同时覆盖 control 数据卷与 master key 卷。

### 9.7 本次真实证据与边界

2026-10-26 在本机实际执行：

| 项目 | 实际结果 |
|---|---|
| Docker Desktop / Engine | 29.2.1，Linux containers |
| 浏览器证据 control 镜像 | `sha256:d06578a3b011f6b06e9b3781414aae1757bbe41ffba7e7f3479e3302d4d3b49f` |
| 最终脚本校验镜像（同源重建） | `sha256:c4a4c7f24462addff841f9b5b360e97654bd4cebaf89c48f113bcc57f8c4e6d1` |
| ngrok | 3.39.11，公网 HTTPS 可达 |
| Chromium | 151.0.7922.34，headless，独立 browser context |
| 管理员 HTTPS 登录 | 通过 |
| 邀请单次兑换 | 重复兑换 401 |
| A/B 身份与会话目录 | 独立 userId/workspaceId；互不可见 |
| 跨空间会话读取 | 404 |
| member 访问管理员 API | 403 |
| 未装配 worker 的聊天/历史 | 503 `capability_unavailable` |
| 注销 / 禁用后旧登录 | 401 |
| 停止后重开同一数据卷 | `key=exists`，管理员 userId 与 A 会话保留 |
| 旧域名 Cookie 到新 Origin | 401（host-only Cookie 未发送） |
| 旧 Origin 写请求到新 control | 403 |
| 新 Origin 重新登录 | 成功，userId 未变 |

截图见 `docs/evidence/desktop-a1/`，均不包含邀请令牌、Cookie、CSRF 或 master key。本次 ngrok 账户使用免费固定静态域名，无法在本机同时建立第二个公开 ngrok 主机；因此“ngrok 公开地址变化”本身标记为 **not-run**。Origin 变更子场景改用 `127.0.0.1:8443` 的真实 TLS 反向代理，并以 Chromium SPKI pin 校验该本地自签证书；它是范围受限的本地证书，不是公开 CA，也不冒充 ngrok 地址变化。核心双身份登录证据仍来自真实 ngrok 公网 HTTPS。

### 9.8 回归命令

```powershell
& $EaselPython -m pytest `
  tests/multiuser/test_desktop_deployment.py `
  tests/multiuser/test_auth_api.py `
  tests/multiuser/test_admin_api.py `
  tests/multiuser/test_config_api.py -q

Push-Location .\web\frontend
npm run lint
if ($LASTEXITCODE -ne 0) { throw '前端 lint 失败' }
npm run build
if ($LASTEXITCODE -ne 0) { throw '前端构建失败' }
Pop-Location
```

本次实际结果：上述后端 **82 passed**；前端 lint/build 通过；`docker compose config --quiet` 通过；完整 `tests/multiuser` 为 **1019 passed、4 skipped**。真实浏览器证据脚本只存在于本次临时工作区，不读取开发者 HOME、账号、profile 或浏览器数据。

仍有限制：没有执行 worker、Harness、真实供应商或聊天闭环；`/api/outputs` 等工作区业务继续 503；ngrok 公开地址变化未实测；本地 pinned TLS 只用于子场景；正式 Linux 发布检查器仍因固定证据 blockers 保持 blocked。本文不把本机登录管理面结果冒充正式上线。

本文外部依据已于 2026-10-26 核对：Docker 官方 [Windows 安装](https://docs.docker.com/desktop/setup/install/windows-install/)、[Compose up 行为](https://docs.docker.com/reference/cli/docker/compose/up/)。Easel 命令以本仓库 CLI 和源码为准。
