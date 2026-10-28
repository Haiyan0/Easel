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
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PublicOrigin,
    [Parameter(Mandatory = $true)][string]$Image,
    [Parameter(Mandatory = $true)][string]$WorkerImage,
    [Parameter(Mandatory = $true)][string]$GatewayImage,
    [string]$ProjectName = 'easel-workspace-desktop',
    [ValidateRange(1, 65535)][int]$Port = 7861,
    [string]$Distro = 'Ubuntu-26.04',
    [string]$WslRepo = '/mnt/c/Code/Easel',
    [string]$WorkspaceRoot = 'C:\Easel\workspaces',
    [string]$DataRoot = 'C:\Easel\desktop',
    [int]$ProvisionerGid = 1000,
    [ValidateRange(1, 600)][int]$TimeoutSeconds = 180
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

try {
    $PublicOrigin = Resolve-EaselOrigin -Origin $PublicOrigin
    $Image = Resolve-EaselImage -Image $Image
    $WorkerImage = Resolve-EaselImage -Image $WorkerImage
    $GatewayImage = Resolve-EaselImage -Image $GatewayImage
    $ProjectName = Resolve-EaselProject -ProjectName $ProjectName
    $Port = Resolve-EaselPort -Port $Port
    Assert-EaselDocker
    $volumes = Get-EaselVolumeNames -ProjectName $ProjectName
    $network = Get-EaselWorkspaceNetworkName -ProjectName $ProjectName
    $WorkspaceRoot = [IO.Path]::GetFullPath($WorkspaceRoot)
    $DataRoot = [IO.Path]::GetFullPath($DataRoot)
    $publicDir = Join-Path $DataRoot 'public'
    $compose = Get-EaselWorkspaceComposePath
    if (-not (Test-Path -LiteralPath $compose -PathType Leaf)) {
        throw 'compose_missing'
    }

    Assert-EaselVolumeContract -ProjectName $ProjectName -Volumes $volumes `
        -IncludeEgress
    Assert-EaselNetworkContract -ProjectName $ProjectName `
        -NetworkName $network
    Test-EaselImagePin -Image $Image
    foreach ($pin in @($WorkerImage, $GatewayImage)) {
        $inspect = Invoke-EaselDocker -Arguments @(
            'image', 'inspect', '--format', '{{.Id}}', $pin)
        if ($inspect.ExitCode -ne 0 -or
            (($inspect.Output -join '').Trim()) -ne $pin) {
            throw 'worker_or_gateway_image_mismatch'
        }
    }

    $fingerprints = Get-EaselKeyFingerprints -Image $Image `
        -KeyVolume $volumes.Keys
    $policyDigests = Get-EaselPolicyDigests -Image $Image `
        -KeyVolume $volumes.Keys -EgressVolume $volumes.Egress
    $trustedPublic = Assert-EaselPublicCopies -Fingerprints $fingerprints `
        -Policy $policyDigests -PublicDir $publicDir
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

    $socket = "/tmp/easel-runtime/$ProjectName/provisioner.sock"
    $wslWorkspace = ConvertTo-EaselWslPath -Path $WorkspaceRoot
    $wslPublic = ConvertTo-EaselWslPath -Path $publicDir
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
        engineContext = $engineIdentity.Context
        engineId = $engineIdentity.Id
        socketUid = $socketIdentity.Uid
        socketGid = $socketIdentity.Gid
        socketMode = '660'
        allowedUid = 10001
    }
    $logDir = Join-Path $DataRoot 'logs'
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
                # 配置漂移明确拒绝；不杀活跃实例，由操作员 drain/受控重启。
                throw ("provisioner_config_mismatch:" +
                    (($identity.Output -join ',').Trim()))
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
        $lines = @(
            '#!/usr/bin/env bash',
            'set -e',
            "cd $(Quote-Wsl $WslRepo)",
            "export PYTHONPATH=$(Quote-Wsl $WslRepo)",
            ('exec python3 deploy/multiuser/provisioner.py' +
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
        "EASEL_PROVISIONER_SOCKET=$socket"
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
    if ($barrierText -and $barrierText -ne 'barrier=none') {
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
            throw ("admission_barrier_active:" +
                (($reopen.Output -join ',').Trim()))
        }
        Write-Output "admission_barrier_reopened=$barrierId"
    }

    Write-Output ("started project={0} origin={1} port={2} control={3} worker={4} gateway={5} network={6} provisioner_socket={7}" -f `
        $ProjectName, $PublicOrigin, $Port, $Image, $WorkerImage,
        $GatewayImage, $network, $socket)
    exit 0
}
catch {
    [Console]::Error.WriteLine("错误：$($_.Exception.Message)")
    exit 1
}
