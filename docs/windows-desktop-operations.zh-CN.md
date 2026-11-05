# Easel Windows 多用户部署与运维手册

适用：Windows 11 + Docker Desktop Linux containers + WSL2；命令在 **PowerShell** 执行。2026-11-03 已合入并验证 acceptance 修复，用户已验证聊天。源码统一使用 `C:\Code\Easel`，远程为 `https://github.com/Haiyan0/Easel.git` 的 `feat/multiuser-task1-store` 分支。不要默认 main 包含这组功能。

**第一次使用按第 0～6 节；每天启停看第 7 节；以后更新只看第 10 节。** 不再创建 acceptance、next 或 worktree 代码副本。程序目录与数据目录分开：更新 Git 不会替你备份数据库。

原 Web 的登录、独立 workspace、聊天、内容库、画像、技能及账号/发布入口已分组验收。真实供应商及平台行为以本人受管配置和授权实测为准；Desktop 通过不等于 Linux 生产门槛通过。Linux 的 XFS、防火墙、IPv6、Chromium 沙箱等仍见 [Linux 手册](linux-docker-operations.zh-CN.md)，未通过项不冒充已通过。

多人 Docker 的 database-query 数据库隧道只支持 Linux 部署（由每 workspace 的 sidecar 常驻转发）；Windows 桌面多用户部署不运行该 sidecar，需要数据库查询时按 `skills/builtin/database-query/references/setup.md` 第 5 节手工建立 SSH 隧道。

<a id="9-完整-desktop-多人已装配的正式路线"></a>
<a id="9-windows-上的多人验证做到哪一步"></a>

源码可替换；control 数据卷、keys 卷和 workspace 是持久数据。备份含私钥和用户状态，须加密离机保存。不要删卷或全局 prune 作为重装手段。

## 0. 新电脑准备与固定源码

已有本机环境只做检查，安装步骤不重复执行。

管理员 PowerShell：

```powershell
wsl --install
```

用途：首次安装 WSL2 和默认 Ubuntu，按提示重启并创建 Linux 用户。已有 WSL 用 `wsl --list --verbose` 查看。[Microsoft 安装说明](https://learn.microsoft.com/en-us/windows/wsl/install)

安装并启动 [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)，使用 Linux containers，在 Settings → Resources → WSL Integration 中启用 Ubuntu。安装 Git 和 [ngrok](https://ngrok.com/docs/getting-started/)，按官方账号引导完成认证；authtoken 不写进文档、Git 或截图。

**这台电脑已经有 `C:\Code\Easel`，不要再次 clone，直接进入该目录。** 只有新电脑才执行：

```powershell
git clone --branch feat/multiuser-task1-store --single-branch https://github.com/Haiyan0/Easel.git C:\Code\Easel
if ($LASTEXITCODE -ne 0) { throw '下载失败，请检查 GitHub 网络或权限' }
```

已有目录只检查，不覆盖本地文件：

```powershell
Set-Location C:\Code\Easel
git branch --show-current   # 应为 feat/multiuser-task1-store
git status --short         # 更新前必须确认没有未保存的修改
git rev-parse --short HEAD # 当前代码版本，排错时保留这个值
```

更新代码按第 10 节做，**运行中不要直接 pull**：provisioner 从源码目录运行，必须先正常 Stop。

安装 WSL 宿主依赖并核对 Engine：

```powershell
wsl --list --verbose
$Distro = 'Ubuntu-26.04'   # 按上方实际名称填写
wsl -d $Distro -- sudo apt-get update
wsl -d $Distro -- sudo apt-get install -y python3 python3-cryptography
docker info --format '{{.ID}}'
wsl -d $Distro -- docker info --format '{{.ID}}'
```

两个 ID 必须相同。WSL 无 docker 时修复 WSL Integration，不另装独立 daemon。镜像包含 Python/Node/前端/Harness；Windows 不必再安装一套运行依赖。

## 1. 变量与前置条件

```powershell
Set-Location C:\Code\Easel
$EaselPython = 'C:\Users\hyh\anaconda3\envs\Agent\python.exe'
$ProjectName = 'easel-main'
$DataRoot = 'C:\Easel\main-data'
$WorkspaceRoot = 'C:\Easel\main-workspaces'
$Port = 7863
$PublicOrigin = 'https://acts-revise-unrented.ngrok-free.dev'
$PublishMode = 'manual'                  # 先做聊天/文件/人工导出；真实自动发布验收前再显式切换
$Distro = 'Ubuntu-26.04'                  # 实际发行版
$WslRepo = '/mnt/c/Code/Easel'  # 与 Windows 源码路径对应
$ProvisionerGid = [int]((wsl -d $Distro -- id -g).Trim())
$DshCommit = '687f282e8625c6b8d11ff58211a90524be2e4317'
$Release = (git rev-parse HEAD).Trim()
$ErrorActionPreference = 'Stop'
Set-ExecutionPolicy -Scope Process Bypass
```

前置：

```powershell
docker version
docker compose version
docker info --format '{{.OSType}}'          # 必须是 linux
wsl --status
wsl -d $Distro -- python3 -c "import cryptography"  # provisioner 依赖
Test-Path .\web\frontend\dist\index.html    # 构建 control 镜像前可为 False，镜像内前端由 Dockerfile 构建
```

Docker Desktop 使用 Linux containers；WSL 发行版默认 `Ubuntu-26.04`。若不同，后续支持这些参数的脚本都要显式加 `-Distro`、`-WslRepo`。

## 2. 从源码构建三个镜像

```powershell
$Tag = $Release.Substring(0, 12)
$ImageTag = "easel-control:$Tag"
docker build --progress=plain -f .\deploy\desktop\Dockerfile.control -t $ImageTag .
if ($LASTEXITCODE -ne 0) { throw 'control 镜像构建失败' }
$ControlImage = (docker image inspect $ImageTag --format '{{.Id}}').Trim()
$ControlImage
docker build --progress=plain -f .\deploy\desktop\Dockerfile.gateway -t "easel-gateway:$Tag" .
if ($LASTEXITCODE -ne 0) { throw 'gateway 镜像构建失败' }
docker build --progress=plain -f .\deploy\multiuser\Dockerfile.worker `
  --build-arg "DSH_COMMIT=$DshCommit" -t "easel-worker:$Tag" .
if ($LASTEXITCODE -ne 0) { throw 'worker 镜像构建失败' }
$WorkerImage = (docker image inspect "easel-worker:$Tag" --format '{{.Id}}').Trim()
$GatewayImage = (docker image inspect "easel-gateway:$Tag" --format '{{.Id}}').Trim()
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

第一条输出应等于 `$WorkerImage` 保存的 `sha256:...`，第二条应等于 `$GatewayImage`；不是与 `easel-worker:...` 这样的 tag 字符串比较。control 提供登录/配置/前端，worker 执行 Agent，gateway 校验内部请求。`--progress=plain` 避免反复刷新进度占满终端。worker 首次构建较慢，保留缓存，不加 `--no-cache`，不使用只有生命周期的 `--target base` 或 `execution` 替代 runtime。

## 3. 受管 Agent 配置、出口策略与 CA

新建两个**不含任何供应商 Key** 的 JSON 文件。`revision` 每次有意变更时递增；`command` 来自锁定的 ACP profile：

```powershell
$ConfigDir = Join-Path $DataRoot 'config'
New-Item -ItemType Directory -Force $ConfigDir | Out-Null
$ManagedConfig = Join-Path $ConfigDir 'managed-agent.json'
if (Test-Path $ManagedConfig) { throw '配置已存在，请检查现有内容并跳过首次创建' }
@'
{
  "revision": 1,
  "agent": {
    "runtime": "acp",
    "command": ["node", "/opt/dsh/apps/cli/lib/bin.js", "--profile", "acp", "--patch", "/opt/easel/managed/acp.patch.yml"],
    "models": ["deepseek-chat", "deepseek-flash", "deepseek-v4-pro"],
    "sandbox": "workspace-write",
    "timeout": 300
  },
  "broker": {"mode": "workspace-sidecar", "channel": "chat"}
}
'@ | Set-Content -LiteralPath $ManagedConfig -Encoding UTF8

$EgressPolicy = Join-Path $ConfigDir 'egress-policy.json'
if (Test-Path $EgressPolicy) { throw '出口配置已存在，请检查并跳过首次创建' }
@'
{"approved_hosts": [], "approved_cidrs": []}
'@ | Set-Content -LiteralPath $EgressPolicy -Encoding UTF8
```

- `managed-agent.json` 只允许 `revision/agent/broker`；含 `secret/api_key/token/password` 等字段会被拒绝。
- `egress-policy.json` 只允许 `approved_hosts/approved_cidrs`。
- 空列表仍允许默认策略认可的公网 HTTPS:443，不等于断网或域名白名单；私网供应商需审核精确例外。[策略说明](../deploy/multiuser/egress-policy.md)
- `agent.models` 可省略，省略时使用镜像受管 manifest 的完整目录；填写时只允许其中的非空子集，不能加入镜像目录外的模型。ACP patch 的默认模型是 `deepseek-chat`。该文件只声明运行时/broker；真实 Key/模型在管理员“全体默认 → 主聊天”中配置，不能从本机 `.dsh` 登录状态自动推断。
- 使用企业/私有 CA 时，把 CA PEM 作为 `-EgressCaFile` 传给 Start；省略时使用 control 镜像内的系统 CA bundle。CA 会复制到 key 卷并随 v2 备份保存，不包含私钥。

## 4. 初始化专用卷、网络与 key

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

## 5. 启动并接 ngrok HTTPS

先启动 ngrok（独立窗口，不要读取/打印 authtoken）：

```powershell
ngrok http http://127.0.0.1:7863 --inspect=false
```

用途：为 Secure Cookie 提供 HTTPS。自定义 Port 时同步修改 ngrok 目标。把 HTTPS Forwarding 地址填入 `$PublicOrigin`，不带路径和末尾 `/`。全新部署默认 `automated`；需要人工审核策略时在 Start 显式加 `-PublishMode manual`。已有部署若登记缺 `publish_mode`，必须显式传 `-PublishMode` 迁移，否则 Start 拒绝。然后：

```powershell
$StartArgs = @{
  PublicOrigin = $PublicOrigin; Image = $ControlImage
  WorkerImage = $WorkerImage; GatewayImage = $GatewayImage
  ProjectName = $ProjectName; Port = $Port
  Distro = $Distro; WslRepo = $WslRepo; ProvisionerGid = $ProvisionerGid
  DataRoot = $DataRoot; WorkspaceRoot = $WorkspaceRoot
  PublishMode = $PublishMode
}
.\deploy\desktop\Start-WorkspaceEasel.ps1 @StartArgs
if ($LASTEXITCODE -ne 0) { throw '启动失败，先处理错误' }
$StartArgs | Export-Clixml (Join-Path $DataRoot 'config\start-args.xml')
(Get-Location).Path | Set-Content (Join-Path $DataRoot 'config\source-root.txt') -Encoding UTF8
```

预期输出包含：

```text
started project=... origin=... port=... control=sha256:... worker=sha256:... gateway=sha256:...
deployment_file=...\logs\deployment.json
config_sha256=<64位>
frontend_sha256=<64位>
```

Start 会先核验固定镜像、卷/网络归属、key 指纹、公钥副本、策略副本、前端、WSL 依赖和 provisioner 身份；失败时不会启动替代实例。`config_sha256` 是 stop/reopen 与升级协议的配置身份。

## 6. 管理员、邀请、登录、聊天、权限与停止

首次管理员（只执行一次）：

```powershell
$ControlContainer = (docker ps -q `
  --filter "label=com.docker.compose.project=$ProjectName" `
  --filter 'label=com.docker.compose.service=control').Trim()
docker exec $ControlContainer python -m easel admin bootstrap `
  --control-dir /srv/easel/control --name Owner
```

输出中的 `invite=` 只出现一次，复制后立即使用；不要写入报告或截图。打开 `$PublicOrigin`，粘贴邀请登录。管理员在“成员与邀请”创建用户并签发邀请；每个用户使用独立浏览器 profile/context，不能只用同一浏览器两个标签页。

**bootstrap 创建的是管理员 Owner，仍从同一个登录页进入，没有单独的管理员登录网址。邀请是单次兑换令牌，不是可反复使用的密码；注销后原邀请失效属于当前登录设计。**

### 注销后怎样重新登录管理员

在原 PowerShell 里查出管理员 ID（只读，不输出密钥）：

```powershell
docker exec $ControlContainer python -c "import sqlite3; c=sqlite3.connect('file:/srv/easel/control/control.sqlite3?mode=ro',uri=True); print(c.execute('SELECT id, display_name FROM users WHERE role = ?', ('admin',)).fetchall()); c.close()"
```

把输出中对应管理员的 ID 填入下面变量，为该管理员自己签发新邀请：

```powershell
$AdminId = '替换为查到的管理员ID'
docker exec $ControlContainer python -m easel admin invite `
  --control-dir /srv/easel/control --admin-user-id $AdminId --user-id $AdminId
if ($LASTEXITCODE -ne 0) { throw '邀请签发失败，检查容器/管理员状态' }
```

用途：保留原账号/workspace/历史，仅创建新的单次邀请。复制新 `invite=` 到同一 HTTPS 登录页即可。若新开终端或重建过 control，先加载第 7 节参数，再执行本节开头查询 ControlContainer 的命令；不重复 bootstrap。

### 管理员在哪里配置聊天

登录后左侧显示“管理员”，点击 **全体默认 → 主聊天**，填写供应商、模型、API 地址、协议和 API Key，然后点 **保存全体默认**。这是受管 chat 通道，个人媒体设置或初始化的 managed-agent.json 都不能代替它。模型必须来自受管 manifest 的 `provider.models`（当前 `deepseek-chat`、`deepseek-flash`、`deepseek-v4-pro`）；保存后生效的是服务端版本化路由，不是浏览器本地配置。API Key 只填认证后的秘密输入框，不写入命令行/公开配置，不发给维护者。

首次部署还未保存全体默认时，聊天可能返回 503；先完成该配置再发送消息。若保存成功后仍 503，检查 Logs 中的具体错误，不能仅凭状态码认定仍是模型问题。

验证聊天/权限/停止：

1. 管理员发布聊天模型配置（受管 provider/channel；不要填真实 Key 到公开文件）。
2. 用户新建会话发送消息；预期流式事件、助手回复、历史刷新后仍在。
3. 需要权限的任务选择允许/拒绝；拒绝后任务不应继续执行。
4. 点击停止；预期服务端 Run 进入 `cancelled` 或失败终态、临时 token 撤销；随后同会话新消息可继续，不出现“取消清理期间 409 误挡”或重叠执行。
5. 两个用户互相看不到会话；跨空间读取应为 404/403。

## 7. 日常 start/stop/status/logs

新终端先设置仅当前窗口有效的执行策略，再恢复参数；第 1 节的 Process 策略在关闭窗口后失效，不会自动带到新终端。始终在同一源码目录运行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
Set-Location C:\Code\Easel
$DataRoot = 'C:\Easel\main-data'  # 必须是本次实例目录，不要加载旧实例参数
$StartArgs = Import-Clixml (Join-Path $DataRoot 'config\start-args.xml')
$Repo = (Get-Content (Join-Path $DataRoot 'config\source-root.txt') -Raw).Trim()
Set-Location $Repo
$ProjectName = $StartArgs.ProjectName
$DataRoot = $StartArgs.DataRoot
$WorkspaceRoot = $StartArgs.WorkspaceRoot
$Port = $StartArgs.Port
$ControlImage = $StartArgs.Image
$WorkerImage = $StartArgs.WorkerImage
$GatewayImage = $StartArgs.GatewayImage
$PublicOrigin = $StartArgs.PublicOrigin
$Distro = $StartArgs.Distro
$WslRepo = $StartArgs.WslRepo
$ProvisionerGid = $StartArgs.ProvisionerGid
$PublishMode = $StartArgs.PublishMode
if (-not $PublishMode) { throw '旧参数未登记策略；先明确选择 manual 或 automated 并更新 StartArgs，不要默认沿用' }
```

```powershell
# 只读诊断：网络/卷/control/端口/挂载/key/lease/health/egress/provisioner/version
.\deploy\desktop\Status-WorkspaceEasel.ps1 `
  -ProjectName $ProjectName -DataRoot $DataRoot -Port $Port `
  -Distro $Distro -ProvisionerGid $ProvisionerGid

# 查看最近日志（control/egress/provisioner；不读取 key）
.\deploy\desktop\Logs-WorkspaceEasel.ps1 `
  -ProjectName $ProjectName -DataRoot $DataRoot -Tail 200

# 停止：先建屏障、drain、确认 Run/请求/实例收口，再 down，最后写完成记录
.\deploy\desktop\Stop-WorkspaceEasel.ps1 `
  -ProjectName $ProjectName -DataRoot $DataRoot -Distro $Distro -WslRepo $WslRepo
if ($LASTEXITCODE -ne 0) { throw '未成功停止，暂停备份/升级' }

# 重开：沿用同一 key/数据卷；只有匹配的成功 Stop 记录才自动重开
.\deploy\desktop\Start-WorkspaceEasel.ps1 @StartArgs
if ($LASTEXITCODE -ne 0) { throw '启动失败' }
```

`Status` 的 `status=ok` 才表示全部检查通过；`version_*` 是实际镜像/前端/配置/源码/Harness/发布策略摘要（含 `version_publish_mode`），`version_schema` 是数据库 schema。`Logs` 只输出日志路径和尾部，不打印 key/邀请/Cookie。

Stop 和 Start 是分开的操作，不要求每天连着执行。不要用 Force、直接 compose down 或删除 worker 替代成功 Stop。ngrok 在自己的窗口 Ctrl+C。Initialize 会复制受管配置，不是重启命令，日常和恢复后不重跑。

## 8. 完整备份与只读校验

备份只能在成功 Stop 之后，完成前不要 Start：

```powershell
$Backup = Join-Path $DataRoot ('backups\full-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
.\deploy\desktop\Backup-WorkspaceEasel.ps1 `
  -ProjectName $ProjectName `
  -Image $ControlImage `
  -DataRoot $DataRoot `
  -WorkspaceRoot $WorkspaceRoot -Destination $Backup -Distro $Distro -WslRepo $WslRepo
if ($LASTEXITCODE -ne 0) { throw '备份失败，暂停升级' }
.\deploy\desktop\Verify-WorkspaceEasel.ps1 -Backup $Backup -Image $ControlImage -Distro $Distro
if ($LASTEXITCODE -ne 0) { throw '校验失败，暂停升级' }
$Backup
```

备份包含：control DB 一致快照、master.key、Ed25519 私钥及派生公钥、受管 Agent 配置、出口策略、CA、各 workspace 的 `workspace`（含 `state`）与 `HOME`。明确排除 `.credentials` 与 `.gateway`（每部署重新生成），不备份 socket/PID。

`Backup` 会先读取 `$DataRoot\logs\deployment.json`，要求传入的 `-DataRoot`/`-WorkspaceRoot` 与已启动部署登记值指向同一物理目录；错误空根、重解析点或路径别名在启动备份容器前失败。v2 完整备份必须提供真实 workspace 根；已有 Run 历史的 workspace 缺目录时失败，尚未物化的新 workspace 才允许为空。

预期 `version=2`、`clean_stop=true`、`workspaces=<数量>`、`verified_backup=...`。备份目录在 `$DataRoot\backups\<时间戳>`，Windows ACL 只授予当前用户/SYSTEM/Administrators；不要放进 Git 或公开目录。`Verify` 真正只读：逐项核对清单中全部文件的大小/SHA-256，并在私有数据库副本上检查 SQLite/密文，不创建 `-wal`/`-shm`、不生成 key、不改备份；连续执行两次结果一致。

## 9. 恢复到新目录/新项目

源部署保持停止，目标必须是全新项目/卷/网络/目录/端口：

在独立窗口为恢复端口提供 HTTPS，例如 `ngrok http http://127.0.0.1:7862 --inspect=false`。源隧道和恢复隧道的账号限制需自行确认。恢复时使用与备份兼容的镜像；例行演练可用当前参数，升级失败回滚须选择升级前保存的参数：

```powershell
$RestoredProject = 'easel-desktop-restored'
$RestoredPort = 7862
$RestoredOrigin = 'https://替换为恢复实例的-ngrok-域名'
$RestoredData = 'C:\Easel\desktop-restored'
$RestoredWorkspaces = 'C:\Easel\workspaces-restored'
$RestoreVersion = $StartArgs   # 例行演练
$RestoreRepo = (Get-Content (Join-Path $DataRoot 'config\source-root.txt') -Raw).Trim()
# 升级回滚时改为：
# $RestoreVersion = Import-Clixml (Join-Path $DataRoot 'config\previous-start-args.xml')
# $RestoreRepo = (Get-Content (Join-Path $DataRoot 'config\previous-source-root.txt') -Raw).Trim()
Set-Location $RestoreRepo

.\deploy\desktop\Restore-WorkspaceEasel.ps1 `
  -Backup $Backup `
  -ProjectName $RestoredProject `
  -PublicOrigin $RestoredOrigin `
  -Image $RestoreVersion.Image `
  -WorkerImage $RestoreVersion.WorkerImage `
  -GatewayImage $RestoreVersion.GatewayImage `
  -Port $RestoredPort `
  -DataRoot $RestoredData `
  -WorkspaceRoot $RestoredWorkspaces `
  -Distro $RestoreVersion.Distro -WslRepo $RestoreVersion.WslRepo -ProvisionerGid $RestoreVersion.ProvisionerGid
if ($LASTEXITCODE -ne 0) { throw '恢复失败，保留源数据与备份' }
.\deploy\desktop\Start-WorkspaceEasel.ps1 `
  -PublicOrigin $RestoredOrigin -Image $RestoreVersion.Image `
  -WorkerImage $RestoreVersion.WorkerImage -GatewayImage $RestoreVersion.GatewayImage `
  -ProjectName $RestoredProject -Port $RestoredPort `
  -DataRoot $RestoredData -WorkspaceRoot $RestoredWorkspaces `
  -PublishMode $PublishMode `
  -Distro $RestoreVersion.Distro -WslRepo $RestoreVersion.WslRepo -ProvisionerGid $RestoreVersion.ProvisionerGid
if ($LASTEXITCODE -ne 0) { throw '恢复实例启动失败' }
```

预期 `restored_project=...`、`restored_schema=...`、`restored_workspaces=...`、`recovery_invite_file=...`。恢复会：

- 只读校验备份，拒绝错 key、缺文件、改摘要、未来 schema、非空目标、外来卷/路径逃逸；
- 创建新卷/网络，把恢复材料落到新目标，不覆盖源；
- `prepare-restore` 先按原清单校验整个 staging；数据库或 workspace 已被篡改时拒绝，DB/manifest/文件集合均不变。只有合法输入才修改 staging 控制库并更新 staging 清单的来源摘要，**原备份不变**；`materialize-restore` 再核对输入与实际复制字节，失败只清理本次目标；
- 清除旧屏障/旧 lease 身份、撤销旧登录/未兑换邀请/Run token，签发一次性管理员恢复邀请；
- 保留 workspace ID、消息和原生会话绑定（容器内 cwd 仍是 `/workspace`）。

读取恢复邀请文件后登录管理员，重新给成员签发邀请。旧 Cookie 应 401；新 Origin 不匹配的写请求应 403。恢复副本启动后先验证 A/B 历史、配置解密、sentinel 和同会话新 Harness 消息；确认后先 Stop 恢复副本，才允许源再次 Start。

## 10. 以后怎样更新：停机 → 备份 → 拉取 → 构建 → 启动

这是**原目录更新**路线。第一次部署不用执行这一节。更新期间服务会暂停，先告知其他使用者退出并保存工作。全程在 PowerShell 的 `C:\Code\Easel` 执行。代码仍只保留一份；保留旧镜像和数据备份是为了恢复，不是再复制一个仓库。

### 10.1 读取自己的部署参数

只需填写你的 DataRoot（第 1 节设置的那个），其余读取上次保存值：

```powershell
Set-Location C:\Code\Easel
$ErrorActionPreference = 'Stop'
$DataRoot = 'C:\Easel\main-data'
$StartArgs = Import-Clixml (Join-Path $DataRoot 'config\start-args.xml')
$ProjectName = $StartArgs.ProjectName
$WorkspaceRoot = $StartArgs.WorkspaceRoot
$Distro = $StartArgs.Distro
$WslRepo = '/mnt/c/Code/Easel'
$OldControlImage = $StartArgs.Image
$OldRevision = (git rev-parse HEAD).Trim()
git status --short
```

有本地修改时先保存并处理，不能用 `reset --hard` 或 `clean -fd` 清掉。不要打印或上传 key、Cookie、邀请文件。

### 10.2 正常停止，再备份并验证

```powershell
.\deploy\desktop\Stop-WorkspaceEasel.ps1 -ProjectName $ProjectName `
  -DataRoot $DataRoot -Distro $Distro -WslRepo $WslRepo
if ($LASTEXITCODE -ne 0) { throw '停止未完成，不能继续拉代码' }

$Backup = Join-Path $DataRoot ('backups\before-update-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
.\deploy\desktop\Backup-WorkspaceEasel.ps1 -ProjectName $ProjectName `
  -DataRoot $DataRoot -WorkspaceRoot $WorkspaceRoot -Image $OldControlImage `
  -Destination $Backup -Distro $Distro -WslRepo $WslRepo
if ($LASTEXITCODE -ne 0) { throw '备份失败，停止更新' }
.\deploy\desktop\Verify-WorkspaceEasel.ps1 -Backup $Backup -Image $OldControlImage -Distro $Distro
if ($LASTEXITCODE -ne 0) { throw '备份校验失败，停止更新' }
$OldRevision | Set-Content (Join-Path $DataRoot 'config\previous-revision.txt')
$StartArgs | Export-Clixml (Join-Path $DataRoot 'config\previous-start-args.xml')
$Backup | Set-Content (Join-Path $DataRoot 'config\upgrade-backup-path.txt')
```

预期看到 `drained=True`、`clean_stop=true`、`verified_backup=...`。任一失败就停在当前步骤，不用 Force，也不手工清屏障。

### 10.3 在同一目录获取 GitHub 新版

```powershell
git switch feat/multiuser-task1-store
if ($LASTEXITCODE -ne 0) { throw '切换分支失败' }
git pull --ff-only origin feat/multiuser-task1-store
if ($LASTEXITCODE -ne 0) { throw '拉取失败或分支分叉，请保留现场排查' }
$Release = (git rev-parse HEAD).Trim()
$Tag = $Release.Substring(0, 12)
```

`--ff-only` 的意思是只接受能直接前进的更新，不会偷偷替你合并冲突。GitHub 网络失败时可重试这一步；不要删除数据库、卷或整个仓库。

### 10.4 构建新版安装包

```powershell
$DshCommit = '687f282e8625c6b8d11ff58211a90524be2e4317'
docker build -f deploy/desktop/Dockerfile.control -t "easel-control:$Tag" .
if ($LASTEXITCODE -ne 0) { throw 'control 构建失败' }
docker build -f deploy/desktop/Dockerfile.gateway -t "easel-gateway:$Tag" .
if ($LASTEXITCODE -ne 0) { throw 'gateway 构建失败' }
docker build -f deploy/multiuser/Dockerfile.worker --build-arg "DSH_COMMIT=$DshCommit" -t "easel-worker:$Tag" .
if ($LASTEXITCODE -ne 0) { throw 'worker 构建失败' }
$StartArgs.Image = (docker image inspect "easel-control:$Tag" --format '{{.Id}}').Trim()
$StartArgs.WorkerImage = (docker image inspect "easel-worker:$Tag" --format '{{.Id}}').Trim()
$StartArgs.GatewayImage = (docker image inspect "easel-gateway:$Tag" --format '{{.Id}}').Trim()
$StartArgs.WslRepo = $WslRepo
```

第一次较慢，以后 Docker 会复用缓存。不要随意改 DshCommit，不要加 `--no-cache`，也不要删旧镜像。构建失败时旧数据和备份还在，修正网络/构建问题后重试。

### 10.5 用备份证明允许升级，启动后检查

```powershell
.\deploy\desktop\Start-WorkspaceEasel.ps1 @StartArgs -UpgradeFromBackup $Backup
if ($LASTEXITCODE -ne 0) { throw '启动失败，保留屏障、数据和备份，查看日志' }
.\deploy\desktop\Status-WorkspaceEasel.ps1 -ProjectName $StartArgs.ProjectName `
  -DataRoot $DataRoot -Port $StartArgs.Port -Distro $Distro -ProvisionerGid $StartArgs.ProvisionerGid
if ($LASTEXITCODE -ne 0) { throw '状态异常，暂不开放使用' }
$StartArgs | Export-Clixml (Join-Path $DataRoot 'config\start-args.xml')
'C:\Code\Easel' | Set-Content (Join-Path $DataRoot 'config\source-root.txt')
```

然后打开原 HTTPS 地址，检查登录、旧历史、一条新聊天、停止按钮。正常更新不重跑 Initialize，不生成新 Key，不重新兑换旧邀请。重开 PowerShell 后使用第 7 节的参数文件。

**重试与回退：** 构建阶段失败可重试构建；启动已部分执行时先查 Status/Logs，不能强行清屏障或重新初始化。旧镜像不保证能打开新版 schema。需要恢复数据时按第 9 节，从升级前已验证备份恢复，原卷先保留；恢复副本与源不能同时写同一 workspace。Git 回到旧提交本身不等于数据库回滚。

`Update-WorkspaceEasel.ps1` 是已有在线项目的受控 control/provisioner 维护入口，带屏障和身份校验；它不代替完整备份，不适合顺便换 worker、gateway 或 schema。初学者统一使用本节的停机更新路线即可。

## 11. 数据位置与哪些命令会改数据

| 位置                                          | 内容                                                                                                                         | 是否可丢                                                |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| `$ProjectName-control` 卷                   | control.sqlite3、登录/邀请/Run 元数据、stop 记录                                                                             | 不可丢；靠备份                                          |
| `$ProjectName-keys` 卷                      | master.key、snapshot.pem、派生公钥、managed-agent.json、egress-policy.json、egress-ca.pem                                    | 不可丢；缺私钥不能“重新生成修复”                      |
| 初始化状态                                    | `Initialize` 先只读探测 control DB 与 key 卷；已有 DB/身份缺 master/signing 时拒绝且不生成新 key，仅全新空部署创建初始材料 | 不要删除 key 后重跑 Initialize 试图“修复”             |
| `$ProjectName-egress` 卷                    | 出口策略副本                                                                                                                 | 可重建，但备份保留                                      |
| `$WorkspaceRoot\<id>\workspace`             | 用户工作区（容器 `/workspace`），含 `state`                                                                              | 不可丢                                                  |
| `$WorkspaceRoot\<id>\home`                  | 用户 HOME（容器 `/home/easel`），含 `.dsh` 原生历史                                                                      | 不可丢                                                  |
| `$WorkspaceRoot\.credentials`、`.gateway` | worker service token、启动 attempt                                                                                           | 运行期材料，不备份；新部署重建                          |
| `$DataRoot\public`                          | worker/gateway 公钥、策略副本                                                                                                | 可从 key 卷重建                                         |
| `$DataRoot\logs`                            | provisioner/control 日志、deployment.json                                                                                    | 日志可轮转；deployment.json 用于备份/升级身份，必须保留 |
| `$DataRoot\backups`                         | 完整 v2 备份                                                                                                                 | 受 ACL 保护，按保留策略另存                             |
| `$DataRoot\recovery`                        | 一次性管理员恢复邀请                                                                                                         | 用后删除，不入 Git/证据                                 |

会修改数据：`Initialize`（创建卷/key/配置）、`Start`（运行期 lease/日志/deployment.json）、`Stop`（屏障/完成记录、停止容器）、`Backup`（只读源、写新备份）、`Restore`（写新目标、撤销恢复副本登录/token）、`Upgrade Start`（核对备份后改写完成记录）。`Verify`、`Status`、`Logs` 只读。

## 12. 常见错误与处理

| 错误码                                                                    | 含义                                                | 处理                                                                          |
| ------------------------------------------------------------------------- | --------------------------------------------------- | ----------------------------------------------------------------------------- |
| `volume_not_owned` / `network_not_owned`                              | 同名资源无本项目标签                                | 换项目名或人工确认归属；不要删开发者资源                                      |
| `egress_ca_unavailable`                                                 | control 镜像内找不到系统 CA 且未提供 CA             | 传 `-EgressCaFile` 或换含 CA bundle 的镜像                                  |
| `backup_requires_stopped`                                               | 备份时 control 仍在运行                             | 先 Stop；不要热备                                                             |
| `backup_requires_clean_stop`                                            | 无匹配停止完成记录                                  | 走正式 Stop，不要手工清屏障                                                   |
| `backup_integrity_failed`                                               | 文件/摘要/派生公钥/DB 校验失败                      | 不要恢复；重新取备份或排查篡改                                                |
| `restore_target_exists` / `restore_target_not_empty`                  | 目标卷/目录已有内容                                 | 换全新项目/目录；不要覆盖                                                     |
| `restore_target_same_project`                                           | 目标项目等于源项目                                  | 换新项目名                                                                    |
| `admission_barrier_active`                                              | 旧 Stop 记录与当前配置不匹配                        | 用匹配备份走 `-UpgradeFromBackup`；不要无条件 resume                        |
| `upgrade_stop_completion_mismatch`                                      | 备份/旧屏障/旧摘要不一致                            | 重新 Stop+Backup，或换正确备份                                                |
| `upgrade_deployment_identity_missing` / `upgrade_deployment_mismatch` | 缺旧 deployment.json 或备份与旧实例/项目/镜像不一致 | 用当前实例生成的备份与 deployment.json 重试                                   |
| `worker_or_gateway_image_mismatch`                                      | worker/gateway 摘要缺失或不匹配                     | 核验固定镜像；修复后重试                                                      |
| `publish_mode_migration_required`                                       | 旧部署登记缺发布策略且未显式传 `-PublishMode`     | 用 `-PublishMode manual` 或 `-PublishMode automated` 显式迁移；不要删登记 |
| `deployment_publish_mode_invalid`                                       | 登记值不是 manual/automated                         | 检查 deployment.json，不要写入未定义值                                        |
| `control_port_in_use`                                                   | 无既有 control 时回环端口已被其它进程占用           | 停掉占用进程或换 `-Port`；不要杀未知进程                                    |
| `control_health_timeout` / `control_unhealthy`                        | control 未健康                                      | 看 `Logs` 和 `docker inspect`；不要自动换替代实例                         |

## 13. 本机验收实例收尾与聊天出口

2026-11-03 验收修复已合入主工作目录。旧 `acceptance-data` / `acceptance-workspaces` 是持久数据，不是垃圾；如需旧聊天，可按第 9 节使用其完整备份恢复。新部署使用第 1 节的 `easel-main`、`main-data`、`main-workspaces`，不会覆盖旧数据；管理员和成员需要重新建立。旧验收服务停止后才使用同一端口启动新服务。

若聊天报未知原因且 broker 返回 502，先查出口。Clash Fake-IP 可能把 `api.deepseek.com` 解析为 `198.18.*`，Easel 会按非公网地址拒绝。先关闭 Fake-IP/TUN 或为该域名配置精确排除，再测试；不要把整个测试网段加入 Easel 白名单。登录与 ngrok HTTPS 正常不代表模型出口正常。
