<#
.SYNOPSIS
    校验并启动完整 Desktop 开发拓扑（WSL provisioner + control + egress）。

.DESCRIPTION
    启动前校验固定镜像、专用卷/网络、master/signing key、受管配置、前端、
    WSL Python 依赖和 provisioner socket。worker/gateway 由 provisioner 按
    workspace 动态创建；脚本不发布它们，不读取 ngrok authtoken 或供应商
    Key。重复启动复用既有 key，不轮换。

.PARAMETER PublicOrigin
    ngrok HTTPS Origin，例如 https://xxxx.ngrok-free.app。

.PARAMETER Image
    固定 control 镜像 ID。

.PARAMETER WorkerImage
    固定 worker execution 镜像 ID。

.PARAMETER GatewayImage
    固定 gateway 镜像 ID。

.PARAMETER ProjectName
    专用 Compose 项目名。

.PARAMETER Port
    宿主回环端口，默认 7861。

.PARAMETER Distro
    WSL 发行版名称，默认 Ubuntu-26.04。

.PARAMETER WslRepo
    WSL 视角的仓库路径，默认 /mnt/c/Code/Easel。

.PARAMETER WorkspaceRoot
    worker 持久根（Windows 路径），默认 C:\Easel\workspaces。

.PARAMETER DataRoot
    非秘密公共文件根，默认 C:\Easel\desktop。

.PARAMETER DatabaseQuerySecrets
    可选的宿主 secrets 目录（Windows 路径），需包含 database-query.env
    与 keys/ 子目录；传入后 provisioner 才给 worker 挂只读 DSN、给
    sidecar 挂隧道密钥。省略即保持数据库查询禁用。

.PARAMETER UpdateBarrierId
    受控更新专用：确认调用方已在控制库写入该接纳屏障后，保留屏障完成
    启动，不尝试自动重开。更新脚本在核验新版 control/provisioner 后
    再显式 resume。

.PARAMETER RestartProvisionerIfStale
    受控更新专用：仅当现有 provisioner 配置身份一致、但源码摘要或动作
    能力落后时，按精确 socket/进程重启它。必须同时提供 UpdateBarrierId。
    配置漂移仍然失败关闭。

.PARAMETER LifecycleLockHeld
    受控更新专用：调用方已持有本项目生命周期锁，本脚本不再重复获取。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PublicOrigin,
    [Parameter(Mandatory = $true)][string]$Image,
    [Parameter(Mandatory = $true)][string]$WorkerImage,
    [Parameter(Mandatory = $true)][string]$GatewayImage,
    [string]$ProjectName = 'easel-workspace-desktop',
    [ValidateRange(1, 65535)][int]$Port = 7861,
    # 受管发布策略：全新部署可省略（保留 automated）；旧部署必须显式迁移。
    [ValidateSet('automated', 'manual')][string]$PublishMode = '',
    [string]$Distro = 'Ubuntu-26.04',
    [string]$WslRepo = '/mnt/c/Code/Easel',
    [string]$WorkspaceRoot = 'C:\Easel\workspaces',
    [string]$DataRoot = 'C:\Easel\desktop',
    [string]$DatabaseQuerySecrets = '',
    [string]$EgressCaFile = '',
    [string]$UpgradeFromBackup = '',
    [int]$ProvisionerGid = 1000,
    [ValidateRange(1, 600)][int]$TimeoutSeconds = 180,
    [ValidateRange(1, 600)][int]$LockTimeoutSeconds = 60,
    [string]$UpdateBarrierId = '',
    [switch]$RestartProvisionerIfStale,
    [switch]$LifecycleLockHeld
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'EaselDesktop.Common.ps1')

function Invoke-Wsl {
    param([Parameter(Mandatory)][string[]]$Arguments)
    $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if (-not $wsl) {
        throw 'wsl_unavailable'
    }
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = & wsl.exe @Arguments 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    return [pscustomobject]@{
        ExitCode = [int]$code
        Output = @($lines | ForEach-Object { "$_" })
    }
}

function Quote-Wsl {
    param([Parameter(Mandatory)][string]$Value)
    return "'" + $Value.Replace("'", "'\''") + "'"
}

function Test-ProvisionerSocket {
    param([Parameter(Mandatory)][string]$SocketPath)
    $code = @'
import socket, sys
try:
    probe = socket.socket(socket.AF_UNIX)
    probe.settimeout(2)
    probe.connect(sys.argv[1])
    probe.close()
except OSError:
    raise SystemExit(3)
'@
    return (Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'python3', '-c', $code, $SocketPath)).ExitCode -eq 0
}

function Get-EaselEngineIdentity {
    # Windows CLI 与指定 WSL 必须指向同一个 daemon；context 别名可不同，
    # Engine ID 必须完全一致，采集失败一律失败关闭。
    $windows = Invoke-EaselDocker -Arguments @(
        'info', '--format', '{{.ID}}')
    $windowsId = (($windows.Output -join '').Trim())
    if ($windows.ExitCode -ne 0 -or
        $windowsId -notmatch '^[0-9A-Za-z:._-]{8,128}$') {
        throw 'docker_engine_unavailable'
    }
    $wsl = Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'docker', 'info', '--format', '{{.ID}}')
    $wslId = (($wsl.Output -join '').Trim())
    if ($wsl.ExitCode -ne 0 -or
        $wslId -notmatch '^[0-9A-Za-z:._-]{8,128}$') {
        throw 'wsl_docker_engine_unavailable'
    }
    if ($windowsId -ne $wslId) {
        throw "docker_engine_mismatch:$windowsId/$wslId"
    }
    $context = Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'docker', 'context', 'show')
    $contextName = (($context.Output -join '').Trim())
    if ($context.ExitCode -ne 0 -or -not $contextName) {
        throw 'docker_context_unavailable'
    }
    return [pscustomobject]@{ Context = $contextName; Id = $wslId }
}

function Get-EaselSocketIdentity {
    $uid = Invoke-Wsl -Arguments @('-d', $Distro, '--', 'id', '-u')
    $gid = Invoke-Wsl -Arguments @('-d', $Distro, '--', 'id', '-g')
    $uidText = (($uid.Output -join '').Trim())
    $gidText = (($gid.Output -join '').Trim())
    $uidValue = 0
    $gidValue = 0
    if ($uid.ExitCode -ne 0 -or
        -not [int]::TryParse($uidText, [ref]$uidValue) -or $uidValue -lt 0) {
        throw 'provisioner_socket_owner_unknown'
    }
    if ($gid.ExitCode -ne 0 -or
        -not [int]::TryParse($gidText, [ref]$gidValue) -or $gidValue -lt 0) {
        throw 'provisioner_socket_group_unknown'
    }
    if ($gidValue -ne $ProvisionerGid) {
        throw "provisioner_gid_mismatch:$gidValue/$ProvisionerGid"
    }
    return [pscustomobject]@{ Uid = $uidValue; Gid = $gidValue }
}

function Invoke-ProvisionerIdentityCheck {
    param(
        [Parameter(Mandatory)][string]$SocketPath,
        [Parameter(Mandatory)][string]$ExpectedFile
    )
    # 以 control 的 UID/GID 运行固定镜像内的只读客户端；只传 socket 与
    # 非秘密期望 JSON，不把 key 内容或 Docker socket 暴露给容器。
    $wslExpected = ConvertTo-EaselWslPath -Path $ExpectedFile
    return Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'docker', 'run', '--rm',
        '--network', 'none', '--read-only',
        '--user', '10001:10001', '--group-add', [string]$ProvisionerGid,
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
        '--entrypoint', 'python',
        '--mount', "type=bind,src=$SocketPath,dst=/run/provisioner.sock,readonly",
        '--mount', "type=bind,src=$wslExpected,dst=/expected.json,readonly",
        $Image,
        '/opt/easel/deploy/desktop/provisioner_identity.py',
        'check', '--socket', '/run/provisioner.sock',
        '--expected', '/expected.json')
}

function Get-WslFileSha256 {
    param(
        [Parameter(Mandatory)][string]$Distro,
        [Parameter(Mandatory)][string]$Path
    )
    $result = Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'sha256sum', $Path)
    $digest = (($result.Output -join '') -split '\s+')[0]
    if ($result.ExitCode -ne 0 -or
        $digest -notmatch '^[0-9a-f]{64}$') {
        throw 'provisioner_source_unavailable'
    }
    return $digest
}

function Get-EaselControlContainerId {
    param([Parameter(Mandatory)][string]$ProjectName)
    $lookup = Invoke-EaselDocker -Arguments @(
        'ps', '-q',
        '--filter', "label=com.docker.compose.project=$ProjectName",
        '--filter', 'label=com.docker.compose.service=control')
    if ($lookup.ExitCode -ne 0) {
        return ''
    }
    return (($lookup.Output -join '').Trim())
}

function Assert-UpdateBarrier {
    param(
        [Parameter(Mandatory)][string]$ProjectName,
        [Parameter(Mandatory)][string]$BarrierId
    )
    $controlId = Get-EaselControlContainerId -ProjectName $ProjectName
    if (-not $controlId) {
        throw 'update_barrier_control_unavailable'
    }
    $status = Invoke-EaselDocker -Arguments @(
        'exec', $controlId, 'python', '-m', 'easel', 'admin',
        'barrier-status', '--control-dir', '/srv/easel/control')
    $text = (($status.Output -join '').Trim())
    if ($status.ExitCode -ne 0 -or $text -ne "barrier=$BarrierId") {
        throw "update_barrier_mismatch:$text"
    }
}

function Stop-EaselProvisionerProcess {
    param(
        [Parameter(Mandatory)][string]$Distro,
        [Parameter(Mandatory)][string]$SocketPath,
        [Parameter(Mandatory)][string]$ScriptDir
    )
    $script = @'
set -e
socket="$1"
pattern="deploy/multiuser/provisioner.py --socket $socket"
pids=$(pgrep -f -- "$pattern" || true)
if [ -z "$pids" ]; then
    echo provisioner_process_missing
    exit 3
fi
count=$(printf '%s\n' "$pids" | sed '/^$/d' | wc -l)
if [ "$count" -ne 1 ]; then
    echo "provisioner_pid_ambiguous:$count"
    exit 4
fi
pid=$pids
tr '\0' '\n' < "/proc/$pid/cmdline" | grep -Fx -- "$socket" >/dev/null \
    || { echo "provisioner_cmdline_mismatch:$pid"; exit 5; }
tr '\0' '\n' < "/proc/$pid/cmdline" \
    | grep -Fx -- "deploy/multiuser/provisioner.py" >/dev/null \
    || { echo "provisioner_cmdline_mismatch:$pid"; exit 5; }
kill -TERM "$pid"
for _ in $(seq 1 50); do
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "stopped_pid=$pid"
        exit 0
    fi
    sleep 0.2
done
echo "provisioner_stop_timeout:$pid"
exit 6
'@
    # 通过脚本文件执行：内联多行参数含双引号时，PowerShell 5.1 传给
    # 原生 wsl.exe 的引号转义不可靠。
    $null = New-Item -ItemType Directory -Path $ScriptDir -Force
    $scriptFile = Join-Path $ScriptDir 'provisioner-stop.sh'
    $normalized = ($script -replace "`r`n", "`n")
    [IO.File]::WriteAllText(
        $scriptFile, ($normalized + "`n"),
        (New-Object System.Text.UTF8Encoding($false)))
    $wslScript = ConvertTo-EaselWslPath -Path $scriptFile
    return Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'bash', $wslScript, $SocketPath)
}

$lifecycleLock = $null
$upgradeBackup = $null
try {
    $PublicOrigin = Resolve-EaselOrigin -Origin $PublicOrigin
    $Image = Resolve-EaselImage -Image $Image
    $WorkerImage = Resolve-EaselImage -Image $WorkerImage
    $GatewayImage = Resolve-EaselImage -Image $GatewayImage
    $ProjectName = Resolve-EaselProject -ProjectName $ProjectName
    $Port = Resolve-EaselPort -Port $Port
    Assert-EaselDocker
    if (-not $LifecycleLockHeld) {
        $lifecycleLock = Enter-EaselLifecycleLock `
            -ProjectName $ProjectName -TimeoutSeconds $LockTimeoutSeconds
    }
    $volumes = Get-EaselVolumeNames -ProjectName $ProjectName
    $network = Get-EaselWorkspaceNetworkName -ProjectName $ProjectName
    $WorkspaceRoot = [IO.Path]::GetFullPath($WorkspaceRoot)
    $DataRoot = [IO.Path]::GetFullPath($DataRoot)
    $logDir = Join-Path $DataRoot 'logs'
    $publicDir = Join-Path $DataRoot 'public'
    $compose = Get-EaselWorkspaceComposePath
    if (-not (Test-Path -LiteralPath $compose -PathType Leaf)) {
        throw 'compose_missing'
    }

    # 发布策略只读正式部署登记（deployment.json）或显式 -PublishMode；
    # 不读取 worker/宿主 .env。旧部署没有登记时必须显式迁移，不能因为
    # 新 CLI 默认值把 manual 静默升级成 automated。
    $deploymentFile = Join-Path $logDir 'deployment.json'
    $registeredPublishMode = ''
    $probeMissingDeployment = $false
    if (Test-Path -LiteralPath $deploymentFile -PathType Leaf) {
        try {
            $existingDeployment = Get-Content -LiteralPath $deploymentFile -Raw |
                ConvertFrom-Json
        }
        catch {
            throw 'deployment_metadata_invalid'
        }
        if ([string]$existingDeployment.project -ne $ProjectName) {
            throw 'deployment_project_mismatch'
        }
        $registeredPublishMode = (
            [string]$existingDeployment.publish_mode).ToLowerInvariant()
        if ($registeredPublishMode -and
            $registeredPublishMode -notin @('automated', 'manual')) {
            throw 'deployment_publish_mode_invalid'
        }
        if (-not $registeredPublishMode -and -not $PublishMode) {
            throw 'publish_mode_migration_required'
        }
    }
    else {
        # 登记丢失时延后到镜像 pin 校验之后探测控制卷，避免为无效镜像
        # 触发拉取；已有控制库说明不是全新部署。
        $probeMissingDeployment = $true
    }

    Assert-EaselVolumeContract -ProjectName $ProjectName -Volumes $volumes `
        -IncludeEgress
    Assert-EaselNetworkContract -ProjectName $ProjectName `
        -NetworkName $network
    Test-EaselImagePin -Image $Image
    if ($probeMissingDeployment) {
        $dbProbe = Invoke-EaselDocker -Arguments @(
            'run', '--rm', '--network', 'none', '--read-only',
            '--user', '10001:10001', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges:true',
            '--entrypoint', 'sh',
            '--mount', "type=volume,src=$($volumes.Control),dst=/data,readonly",
            $Image, '-c', 'test -e /data/control.sqlite3')
        if ($dbProbe.ExitCode -eq 0 -and -not $PublishMode) {
            throw 'publish_mode_migration_required'
        }
        if ($dbProbe.ExitCode -notin @(0, 1)) {
            throw 'publish_mode_probe_failed'
        }
    }
    if ($PublishMode) {
        $resolvedPublishMode = $PublishMode.ToLowerInvariant()
    }
    elseif ($registeredPublishMode) {
        $resolvedPublishMode = $registeredPublishMode
    }
    else {
        # 全新部署保留原默认自动发布；旧部署已在上方要求显式迁移。
        $resolvedPublishMode = 'automated'
    }
    foreach ($pin in @($WorkerImage, $GatewayImage)) {
        $inspect = Invoke-EaselDocker -Arguments @(
            'image', 'inspect', '--format', '{{.Id}}', $pin)
        if ($inspect.ExitCode -ne 0 -or
            (($inspect.Output -join '').Trim()) -ne $pin) {
            throw 'worker_or_gateway_image_mismatch'
        }
    }

    if ($UpgradeFromBackup) {
        # 全量校验与旧部署身份绑定必须在任何 compose up 之前完成；
        # 只有 manifest 的空备份、缺 key/DB 或错项目在这里就失败。
        $upgradeBackup = Assert-EaselUpgradeBackup `
            -Backup $UpgradeFromBackup -ProjectName $ProjectName `
            -OldDeploymentFile (Join-Path $logDir 'deployment.json') `
            -Distro $Distro -Image $Image `
            -ControlVolume $volumes.Control -KeyVolume $volumes.Keys
    }

    $fingerprints = Get-EaselKeyFingerprints -Image $Image `
        -KeyVolume $volumes.Keys
    $policyDigests = Get-EaselPolicyDigests -Image $Image `
        -KeyVolume $volumes.Keys -EgressVolume $volumes.Egress
    $trustedPublic = Assert-EaselPublicCopies -Fingerprints $fingerprints `
        -Policy $policyDigests -PublicDir $publicDir
    if ($EgressCaFile) {
        if (-not (Test-Path -LiteralPath $EgressCaFile -PathType Leaf)) {
            throw 'egress_ca_missing'
        }
        $EgressCaFile = (Resolve-Path -LiteralPath $EgressCaFile).Path
        $caMount = @(
            '--mount', "type=bind,src=$EgressCaFile,dst=/src/ca.pem,readonly")
        $caCommand = ('cp /src/ca.pem /keys/egress-ca.pem && ' +
            'chown 10001:10001 /keys/egress-ca.pem && ' +
            'chmod 600 /keys/egress-ca.pem')
    }
    else {
        $caMount = @()
        $caCommand = ('test -s /keys/egress-ca.pem || ' +
            'cp /etc/ssl/certs/ca-certificates.crt /keys/egress-ca.pem; ' +
            'chown 10001:10001 /keys/egress-ca.pem && ' +
            'chmod 600 /keys/egress-ca.pem')
    }
    $ensureCa = Invoke-EaselDocker -Arguments (@(
        'run', '--rm', '--user', '0:0', '--entrypoint', 'sh',
        '--mount', "type=volume,src=$($volumes.Keys),dst=/keys") +
        $caMount + @($Image, '-c', $caCommand))
    if ($ensureCa.ExitCode -ne 0) {
        throw 'egress_ca_unavailable'
    }
    $egressCaSha256 = Invoke-EaselDocker -Arguments @(
        'run', '--rm', '--user', '10001:10001', '--entrypoint', 'sha256sum',
        '--mount', "type=volume,src=$($volumes.Keys),dst=/keys,readonly",
        $Image, '/keys/egress-ca.pem')
    $caDigest = (($egressCaSha256.Output -join '') -split '\s+')[0]
    if ($egressCaSha256.ExitCode -ne 0 -or
        $caDigest -notmatch '^[0-9a-f]{64}$') {
        throw 'egress_ca_unavailable'
    }
    $engineIdentity = Get-EaselEngineIdentity
    $socketIdentity = Get-EaselSocketIdentity
    $configCheck = Invoke-EaselDocker -Arguments @(
        'run', '--rm', '--user', '10001:10001', '--entrypoint', 'sh',
        '--mount', "type=volume,src=$($volumes.Keys),dst=/keys,readonly",
        $Image,
        '-c', 'test -s /keys/managed-agent.json && test -s /keys/egress-policy.json')
    if ($configCheck.ExitCode -ne 0) {
        throw 'managed_config_missing'
    }
    foreach ($name in @('worker-public.key', 'gateway-public.pem',
                        'egress-policy.json')) {
        if (-not (Test-Path -LiteralPath (Join-Path $publicDir $name) `
                -PathType Leaf)) {
            throw "public_file_missing:$name"
        }
    }
    $frontendCheck = Invoke-EaselDocker -Arguments @(
        'run', '--rm', '--entrypoint', 'python', $Image, '-c',
        "import os,sys; sys.exit(0 if os.path.isfile('/opt/easel/web/frontend/dist/index.html') else 1)")
    if ($frontendCheck.ExitCode -ne 0) {
        throw 'frontend_build_missing'
    }
    $null = New-Item -ItemType Directory -Path $WorkspaceRoot -Force

    $repoCheck = Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'bash', '-lc', "test -d $(Quote-Wsl $WslRepo)")
    if ($repoCheck.ExitCode -ne 0) {
        throw 'wsl_repo_missing'
    }
    $pythonCheck = Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'python3', '-c', 'import cryptography')
    if ($pythonCheck.ExitCode -ne 0) {
        throw 'wsl_python_dependencies_missing'
    }

    if ($RestartProvisionerIfStale -and -not $UpdateBarrierId) {
        # 没有接纳屏障时不得替换运行中的 provisioner。
        throw 'provisioner_restart_requires_barrier'
    }
    $socket = "/tmp/easel-runtime/$ProjectName/provisioner.sock"
    $wslWorkspace = ConvertTo-EaselWslPath -Path $WorkspaceRoot
    $wslPublic = ConvertTo-EaselWslPath -Path $publicDir
    $wslSecrets = ''
    $wslDatabaseQueryEnv = ''
    if ($DatabaseQuerySecrets) {
        # 宿主目录是唯一开关；env 与 keys 缺一即失败关闭，不启动半接线部署。
        $secretsRoot = [IO.Path]::GetFullPath(
            $DatabaseQuerySecrets).TrimEnd('\')
        if (-not (Test-Path -LiteralPath $secretsRoot -PathType Container) -or
            -not (Test-Path -LiteralPath `
                (Join-Path $secretsRoot 'database-query.env') `
                -PathType Leaf) -or
            -not (Test-Path -LiteralPath `
                (Join-Path $secretsRoot 'keys') -PathType Container)) {
            throw 'database_query_secrets_missing'
        }
        $wslSecrets = ConvertTo-EaselWslPath -Path $secretsRoot
        $wslDatabaseQueryEnv = "$wslSecrets/database-query.env"
    }
    $provisionerSourceSha256 = Get-WslFileSha256 -Distro $Distro `
        -Path "$WslRepo/deploy/multiuser/provisioner.py"
    $requiredProvisionerActions = @(
        'ensure', 'stop', 'inspect', 'activate', 'abort', 'restore_gateway',
        'commit_attempt', 'recover_abort', 'identity')
    $expectedIdentity = [ordered]@{
        workerImage = $WorkerImage
        gatewayImage = $GatewayImage
        workspaceRoot = $wslWorkspace
        sidecarNetwork = $network
        gatewayControlNetwork = $network
        brokerUpstream = 'control:7861'
        workerPublicSha256 = $trustedPublic.Worker
        gatewayPublicSha256 = $trustedPublic.Gateway
        policySha256 = $trustedPublic.Policy
        databaseQueryEnvFile = $wslDatabaseQueryEnv
        engineContext = $engineIdentity.Context
        engineId = $engineIdentity.Id
        socketUid = $socketIdentity.Uid
        socketGid = $socketIdentity.Gid
        socketMode = '660'
        allowedUid = 10001
        provisionerSourceSha256 = $provisionerSourceSha256
        provisionerActions = $requiredProvisionerActions
    }
    $null = New-Item -ItemType Directory -Path $logDir -Force
    $identityExpectedPath = Join-Path $logDir 'provisioner-expected.json'
    [IO.File]::WriteAllText(
        $identityExpectedPath,
        ($expectedIdentity | ConvertTo-Json -Compress),
        (New-Object System.Text.UTF8Encoding($false)))
    $identityConfigSha256 = Get-EaselFileSha256 -Path $identityExpectedPath

    $alreadyRunning = $false
    $socketExists = Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'bash', '-lc', "test -S '$socket'")
    if ($socketExists.ExitCode -eq 0) {
        if (Test-ProvisionerSocket -SocketPath $socket) {
            $identity = Invoke-ProvisionerIdentityCheck -SocketPath $socket `
                -ExpectedFile $identityExpectedPath
            if ($identity.ExitCode -eq 0) {
                $alreadyRunning = $true
            }
            elseif ($identity.ExitCode -eq 3) {
                $mismatchText = (($identity.Output -join ',')).Trim()
                $mismatchFields = @(
                    ($mismatchText -replace '^identity_mismatch=', '') -split ',')
                $staleOnly = @(
                    $mismatchFields | Where-Object {
                        $_ -notin @('provisionerSourceSha256',
                                    'provisionerActions')
                    }).Count -eq 0
                if ($RestartProvisionerIfStale -and $staleOnly) {
                    # 只替换“配置相同、代码落后”的本项目 provisioner；
                    # 必须先确认控制库已建立本次更新的接纳屏障。
                    Assert-UpdateBarrier -ProjectName $ProjectName `
                        -BarrierId $UpdateBarrierId
                    $stop = Stop-EaselProvisionerProcess `
                        -Distro $Distro -SocketPath $socket `
                        -ScriptDir $logDir
                    if ($stop.ExitCode -ne 0) {
                        throw ("provisioner_restart_failed:" +
                            (($stop.Output -join ',').Trim()))
                    }
                    $removed = Invoke-Wsl -Arguments @(
                        '-d', $Distro, '--', 'rm', '-f', $socket)
                    if ($removed.ExitCode -ne 0) {
                        throw 'provisioner_socket_stale'
                    }
                    $provisionerRestarted = (($stop.Output -join ',').Trim())
                    Write-Output ("provisioner_restart=" +
                        $provisionerRestarted)
                }
                else {
                    # 配置漂移明确拒绝；不杀活跃实例，由操作员显式更新。
                    throw ("provisioner_config_mismatch:" + $mismatchText)
                }
            }
            else {
                throw 'provisioner_identity_unavailable'
            }
        }
        else {
            # 无监听者的陈旧 socket 才可删除；能连接但不是本协议的
            # 服务已在上面失败关闭，绝不盲删。
            $stale = Invoke-Wsl -Arguments @(
                '-d', $Distro, '--', 'rm', '-f', $socket)
            if ($stale.ExitCode -ne 0) {
                throw 'provisioner_socket_stale'
            }
        }
    }
    if (-not $alreadyRunning) {
        $provisionerCommand = (
            'exec python3 deploy/multiuser/provisioner.py' +
            " --socket $(Quote-Wsl $socket)" +
            " --workspace-root $(Quote-Wsl $wslWorkspace)" +
            " --image $(Quote-Wsl $WorkerImage)" +
            ' --allowed-uid 10001 --quota-mode none --allow-unquotaed' +
            " --worker-public-key $(Quote-Wsl ($wslPublic + '/worker-public.key'))" +
            " --sidecar-network $(Quote-Wsl $network)" +
            " --sidecar-policy $(Quote-Wsl ($wslPublic + '/egress-policy.json'))" +
            ' --broker-upstream control:7861' +
            " --gateway-image $(Quote-Wsl $GatewayImage)" +
            " --gateway-control-network $(Quote-Wsl $network)" +
            " --gateway-public-key $(Quote-Wsl ($wslPublic + '/gateway-public.pem'))")
        if ($wslSecrets) {
            # 只在显式提供 secrets 目录时追加；省略即保持零凭据部署。
            $provisionerCommand += (
                " --database-query-secrets $(Quote-Wsl $wslSecrets)")
        }
        $lines = @(
            '#!/usr/bin/env bash',
            'set -e',
            "cd $(Quote-Wsl $WslRepo)",
            "export PYTHONPATH=$(Quote-Wsl $WslRepo)",
            $provisionerCommand
        ) -join "`n"
        $logDir = Join-Path $DataRoot 'logs'
        $null = New-Item -ItemType Directory -Path $logDir -Force
        $stdout = Join-Path $logDir 'provisioner.log'
        $stderr = Join-Path $logDir 'provisioner.err.log'
        $startScript = Join-Path $logDir 'provisioner-start.sh'
        [IO.File]::WriteAllText(
            $startScript,
            ($lines + "`n"),
            (New-Object System.Text.UTF8Encoding($false)))
        $wslStart = ConvertTo-EaselWslPath -Path $startScript
        $process = Start-Process -FilePath 'wsl.exe' -PassThru `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdout `
            -RedirectStandardError $stderr `
            -ArgumentList @('-d', $Distro, '--', 'bash', $wslStart)
        if ($null -eq $process) {
            throw 'provisioner_start_failed'
        }
        $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
        $started = $false
        $identityFailed = $false
        while ((Get-Date) -lt $deadline) {
            if (Test-ProvisionerSocket -SocketPath $socket) {
                $identity = Invoke-ProvisionerIdentityCheck `
                    -SocketPath $socket -ExpectedFile $identityExpectedPath
                if ($identity.ExitCode -eq 0) {
                    $started = $true
                    break
                }
                if ($identity.ExitCode -eq 3) {
                    throw ("provisioner_identity_mismatch:" +
                        (($identity.Output -join ',').Trim()))
                }
                $identityFailed = $true
            }
            Start-Sleep -Seconds 1
        }
        if (-not $started) {
            if ($identityFailed) {
                throw 'provisioner_identity_unavailable'
            }
            throw 'provisioner_start_timeout'
        }
    }

    $composeWsl = "$WslRepo/deploy/desktop/compose.workspace.yaml"
    $envArgs = @(
        "EASEL_CONTROL_IMAGE=$Image",
        "EASEL_PUBLIC_ORIGIN=$PublicOrigin",
        "EASEL_DESKTOP_PROJECT=$ProjectName",
        "EASEL_CONTROL_VOLUME=$($volumes.Control)",
        "EASEL_KEY_VOLUME=$($volumes.Keys)",
        "EASEL_EGRESS_VOLUME=$($volumes.Egress)",
        "EASEL_CONTROL_PORT=$Port",
        "EASEL_CONTROL_NETWORK=$network",
        "EASEL_PROVISIONER_GID=$ProvisionerGid",
        "EASEL_PROVISIONER_SOCKET=$socket",
        "EASEL_PUBLISH_MODE=$resolvedPublishMode"
    )
    $composeArgs = @(
        '-d', $Distro, '--', 'env') + $envArgs + @(
        'docker', 'compose', '-p', $ProjectName, '-f', $composeWsl)
    $configured = Invoke-Wsl -Arguments ($composeArgs + @('config', '--quiet'))
    if ($configured.ExitCode -ne 0) {
        throw 'compose_config_invalid'
    }
    $up = Invoke-Wsl -Arguments ($composeArgs + @('up', '-d'))
    if ($up.ExitCode -ne 0) {
        $upText = ($up.Output -join "`n")
        if ($upText -match 'port is already allocated|address already in use|ports are not available|bind') {
            throw 'control_port_in_use'
        }
        throw 'control_start_failed'
    }

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $healthy = $false
    while ((Get-Date) -lt $deadline) {
        $ids = Invoke-EaselDocker -Arguments @(
            'ps', '-q',
            '--filter', "label=com.docker.compose.project=$ProjectName",
            '--filter', 'label=com.docker.compose.service=control')
        $containerId = (($ids.Output -join '').Trim())
        if ($ids.ExitCode -eq 0 -and $containerId) {
            $state = Invoke-EaselDocker -Arguments @(
                'inspect', '--format', '{{.State.Health.Status}}', $containerId)
            $status = (($state.Output -join '').Trim())
            if ($state.ExitCode -eq 0 -and $status -eq 'healthy') {
                $healthy = $true
                break
            }
            if ($status -in @('unhealthy', 'exited', 'dead')) {
                throw 'control_unhealthy'
            }
        }
        Start-Sleep -Seconds 1
    }
    if (-not $healthy) {
        throw 'control_health_timeout'
    }

    $barrier = Invoke-EaselDocker -Arguments @(
        'exec', $containerId, 'python', '-m', 'easel', 'admin',
        'barrier-status', '--control-dir', '/srv/easel/control')
    $barrierText = (($barrier.Output -join '').Trim())
    if ($barrier.ExitCode -ne 0) {
        throw 'admission_barrier_status_failed'
    }
    if ($UpdateBarrierId) {
        if ($barrierText -eq 'barrier=none') {
            throw 'update_barrier_missing'
        }
        if (-not $barrierText.StartsWith('barrier=')) {
            throw "admission_barrier_status_invalid:$barrierText"
        }
        $barrierId = $barrierText.Substring(8)
        if ($barrierId -ne $UpdateBarrierId) {
            throw ("admission_barrier_mismatch:" +
                "$barrierId/$UpdateBarrierId")
        }
        # 受控更新：更新脚本负责在核验新版两端后显式 resume。
        Write-Output "admission_barrier_retained=$barrierId"
    }
    elseif ($barrierText -and $barrierText -ne 'barrier=none') {
        if (-not $barrierText.StartsWith('barrier=')) {
            throw "admission_barrier_status_invalid:$barrierText"
        }
        $barrierId = $barrierText.Substring(8)
        # 只有成功 Stop 写入且 barrier/项目/配置摘要完全匹配的完成记录
        # 才能自动重开；失败/取消/旧记录保持失败关闭。
        $reopen = Invoke-EaselDocker -Arguments @(
            'exec', $containerId, 'python', '-m', 'easel', 'admin', 'reopen',
            '--control-dir', '/srv/easel/control',
            '--barrier-id', $barrierId,
            '--project', $ProjectName,
            '--config-sha256', $identityConfigSha256)
        if ($reopen.ExitCode -ne 0) {
            if (-not $UpgradeFromBackup) {
                throw ("admission_barrier_active:" +
                    (($reopen.Output -join ',').Trim()))
            }
            # 受控升级：备份已在 compose up 之前完成只读全量校验和旧部署
            # 身份绑定；这里只核对正式 DB 中的 Stop 完成记录。
            if ($null -eq $upgradeBackup) {
                throw 'upgrade_backup_missing'
            }
            $backupPath = $upgradeBackup.Path
            $backupSha = $upgradeBackup.Sha256
            $oldConfigSha = $upgradeBackup.OldConfigSha256
            $status = Invoke-EaselDocker -Arguments @(
                'exec', $containerId, 'python', '-m', 'easel', 'admin',
                'stop-status', '--control-dir', '/srv/easel/control')
            $statusText = (($status.Output -join '').Trim())
            if ($status.ExitCode -ne 0 -or
                -not $statusText.StartsWith('stop_completion=')) {
                throw 'upgrade_stop_status_unavailable'
            }
            try {
                $stopStatus = $statusText.Substring(16) | ConvertFrom-Json
            }
            catch {
                throw 'upgrade_stop_status_invalid'
            }
            if ($null -eq $stopStatus.completion -or
                [string]$stopStatus.completion.barrierId -ne $barrierId -or
                [string]$stopStatus.completion.project -ne $ProjectName -or
                [string]$stopStatus.completion.configSha256 -ne $oldConfigSha) {
                throw 'upgrade_stop_completion_mismatch'
            }
            $upgrade = Invoke-EaselDocker -Arguments @(
                'exec', $containerId, 'python', '-m', 'easel', 'admin',
                'upgrade-complete',
                '--control-dir', '/srv/easel/control',
                '--barrier-id', $barrierId,
                '--project', $ProjectName,
                '--from-config-sha256', $oldConfigSha,
                '--to-config-sha256', $identityConfigSha256,
                '--backup-manifest-sha256', $backupSha)
            if ($upgrade.ExitCode -ne 0) {
                throw ("upgrade_completion_failed:" +
                    (($upgrade.Output -join ',').Trim()))
            }
            $reopen = Invoke-EaselDocker -Arguments @(
                'exec', $containerId, 'python', '-m', 'easel', 'admin',
                'reopen',
                '--control-dir', '/srv/easel/control',
                '--barrier-id', $barrierId,
                '--project', $ProjectName,
                '--config-sha256', $identityConfigSha256)
            if ($reopen.ExitCode -ne 0) {
                throw ("admission_barrier_active:" +
                    (($reopen.Output -join ',').Trim()))
            }
            Write-Output "upgraded_from_backup=$backupPath"
        }
        Write-Output "admission_barrier_reopened=$barrierId"
    }

    $frontendHash = Invoke-EaselDocker -Arguments @(
        'run', '--rm', '--network', 'none', '--read-only',
        '--entrypoint', 'python', $Image, '-c',
        ("import hashlib;print(hashlib.sha256(open(" +
         "'/opt/easel/web/frontend/dist/index.html','rb').read()).hexdigest())"))
    $frontendSha = (($frontendHash.Output -join '').Trim())
    if ($frontendHash.ExitCode -ne 0 -or
        $frontendSha -notmatch '^[0-9a-f]{64}$') {
        throw 'frontend_hash_unavailable'
    }
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
    $sourceCommit = Get-EaselSourceCommit -RepoRoot $repoRoot
    if (-not $sourceCommit) { $sourceCommit = 'unknown' }
    $harnessRevision = Get-EaselHarnessRevision -RepoRoot $repoRoot
    if (-not $harnessRevision) { $harnessRevision = 'unknown' }
    $deployment = [ordered]@{
        project = $ProjectName
        origin = $PublicOrigin
        port = $Port
        control_image = $Image
        worker_image = $WorkerImage
        gateway_image = $GatewayImage
        frontend_sha256 = $frontendSha
        config_sha256 = $identityConfigSha256
        egress_ca_sha256 = $caDigest
        engine_id = $engineIdentity.Id
        engine_context = $engineIdentity.Context
        source_commit = $sourceCommit
        harness_revision = $harnessRevision
        publish_mode = $resolvedPublishMode
        workspace_root = $WorkspaceRoot
        data_root = $DataRoot
    }
    $deploymentFile = Join-Path $logDir 'deployment.json'
    [IO.File]::WriteAllText(
        $deploymentFile,
        ($deployment | ConvertTo-Json -Compress),
        (New-Object System.Text.UTF8Encoding($false)))

    Write-Output ("started project={0} origin={1} port={2} control={3} worker={4} gateway={5} network={6} provisioner_socket={7} publish_mode={8}" -f `
        $ProjectName, $PublicOrigin, $Port, $Image, $WorkerImage,
        $GatewayImage, $network, $socket, $resolvedPublishMode)
    Write-Output "deployment_file=$deploymentFile"
    Write-Output "config_sha256=$identityConfigSha256"
    Write-Output "frontend_sha256=$frontendSha"
    exit 0
}
catch {
    [Console]::Error.WriteLine("错误：$($_.Exception.Message)")
    exit 1
}
finally {
    if (-not $LifecycleLockHeld) {
        Exit-EaselLifecycleLock -Lock $lifecycleLock
    }
}
