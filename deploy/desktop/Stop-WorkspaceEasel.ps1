<#
.SYNOPSIS
    停止完整 Desktop 开发拓扑：先 drain 所有活动 workspace，再停止 control。

.DESCRIPTION
    默认要求 provisioner socket 与控制面可达，先执行 admin drain，再
    `docker compose down`（不删除外部卷/网络）。drain 失败时不静默停止
    control，避免留下无人管理的 worker/gateway。`-Force` 仅用于确认已无
    活动任务后的强制停止。
#>
[CmdletBinding()]
param(
    [string]$ProjectName = 'easel-workspace-desktop',
    [string]$Distro = 'Ubuntu-26.04',
    [string]$WslRepo = '/mnt/c/Code/Easel',
    [string]$DataRoot = 'C:\Easel\desktop',
    [ValidateRange(1, 600)][int]$LockTimeoutSeconds = 60,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'EaselDesktop.Common.ps1')

function Invoke-Wsl {
    param([Parameter(Mandatory)][string[]]$Arguments)
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

$lifecycleLock = $null
try {
    $ProjectName = Resolve-EaselProject -ProjectName $ProjectName
    Assert-EaselDocker
    $lifecycleLock = Enter-EaselLifecycleLock `
        -ProjectName $ProjectName -TimeoutSeconds $LockTimeoutSeconds
    $volumes = Get-EaselVolumeNames -ProjectName $ProjectName
    $network = Get-EaselWorkspaceNetworkName -ProjectName $ProjectName
    $socket = "/tmp/easel-runtime/$ProjectName/provisioner.sock"
    $controlId = ''
    $lookup = Invoke-EaselDocker -Arguments @(
        'ps', '-q',
        '--filter', "label=com.docker.compose.project=$ProjectName",
        '--filter', 'label=com.docker.compose.service=control')
    if ($lookup.ExitCode -eq 0) {
        $controlId = (($lookup.Output -join '').Trim())
    }

    $socketCheck = Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'bash', '-lc', "test -S '$socket'")
    $workerDrained = $false
    $barrierId = ''
    $controlImage = ''
    $configSha256 = ''
    # GID 来自实际登记的 provisioner 身份，不假定永远 1000。Force 且登记
    # 文件缺失时只用 0 作为 compose down 的插值占位；该值绝不用于启动。
    $expectedFile = Join-Path (Join-Path $DataRoot 'logs') `
        'provisioner-expected.json'
    $registeredGid = 0
    if (Test-Path -LiteralPath $expectedFile -PathType Leaf) {
        try {
            $expectedIdentity = Get-Content -LiteralPath $expectedFile -Raw |
                ConvertFrom-Json
            if ($expectedIdentity.PSObject.Properties.Name -notcontains
                'socketGid') {
                throw 'missing_socket_gid'
            }
            $registeredGid = [int]$expectedIdentity.socketGid
        }
        catch {
            throw 'stop_identity_config_invalid'
        }
        if ($registeredGid -lt 0 -or $registeredGid -gt 65535) {
            throw 'stop_identity_config_invalid'
        }
    }
    if (-not $Force -and $socketCheck.ExitCode -eq 0 -and $controlId) {
        $drain = Invoke-EaselDocker -Arguments @(
            'exec', $controlId, 'python', '-m', 'easel', 'admin', 'drain',
            '--control-dir', '/srv/easel/control',
            '--provisioner-socket', '/run/easel/provisioner.sock',
            '--gateway-required', '--barrier')
        if ($drain.ExitCode -ne 0) {
            throw 'worker_drain_failed'
        }
        # drain 已复核无活跃 Run/请求/未收口实例；屏障仍在，后续 submit 与
        # 新 workspace 都会被拒绝。失败时保留屏障，须显式 admin resume。
        $workerDrained = $true
        foreach ($line in $drain.Output) {
            if ("$line".StartsWith('barrier=')) {
                $barrierId = "$line".Substring(8)
                Write-Output ("admission_barrier=" + $barrierId)
            }
            if ("$line" -eq 'verified=settled') {
                Write-Output 'drain_verified=settled'
            }
        }
        if (-not $barrierId) {
            throw 'stop_barrier_missing'
        }
        if (-not (Test-Path -LiteralPath $expectedFile -PathType Leaf)) {
            throw 'stop_identity_config_missing'
        }
        $configSha256 = Get-EaselFileSha256 -Path $expectedFile
        $image = Invoke-EaselDocker -Arguments @(
            'inspect', '--format', '{{.Image}}', $controlId)
        $controlImage = (($image.Output -join '').Trim())
        if ($image.ExitCode -ne 0 -or
            $controlImage -notmatch '^sha256:[0-9a-f]{64}$') {
            throw 'control_image_unavailable'
        }
    }
    elseif (-not $Force -and $socketCheck.ExitCode -eq 0) {
        throw 'control_unavailable_for_drain'
    }
    elseif (-not $Force) {
        throw 'provisioner_socket_missing'
    }

    $composeWsl = "$WslRepo/deploy/desktop/compose.workspace.yaml"
    $envArgs = @(
        "EASEL_CONTROL_IMAGE=placeholder-not-used-for-down",
        "EASEL_PUBLIC_ORIGIN=https://easel.invalid",
        "EASEL_DESKTOP_PROJECT=$ProjectName",
        "EASEL_CONTROL_VOLUME=$($volumes.Control)",
        "EASEL_KEY_VOLUME=$($volumes.Keys)",
        "EASEL_EGRESS_VOLUME=$($volumes.Egress)",
        "EASEL_CONTROL_PORT=7861",
        "EASEL_CONTROL_NETWORK=$network",
        "EASEL_PROVISIONER_GID=$registeredGid",
        "EASEL_PROVISIONER_SOCKET=$socket",
        # down 不启动 control；该必填变量只为 compose 插值成功，使用
        # 非法的保守占位值，绝不用于启动。
        "EASEL_PUBLISH_MODE=placeholder-not-used-for-down"
    )
    $composeArgs = @('-d', $Distro, '--', 'env') + $envArgs + @(
        'docker', 'compose', '-p', $ProjectName, '-f', $composeWsl, 'down')
    $down = Invoke-Wsl -Arguments $composeArgs
    if ($down.ExitCode -ne 0) {
        throw 'control_stop_failed'
    }
    $kill = Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'pkill', '-f', $socket)
    if (-not $Force) {
        # control 已停止后才写完成记录；down 失败不会留下可重开承诺。
        $complete = Invoke-EaselDocker -Arguments @(
            'run', '--rm', '--network', 'none', '--user', '10001:10001',
            '--entrypoint', 'python',
            '--mount', "type=volume,src=$($volumes.Control),dst=/srv/easel/control",
            $controlImage, '-m', 'easel', 'admin', 'stop-complete',
            '--control-dir', '/srv/easel/control',
            '--barrier-id', $barrierId,
            '--project', $ProjectName,
            '--config-sha256', $configSha256)
        if ($complete.ExitCode -ne 0) {
            throw 'stop_completion_failed'
        }
        Write-Output "stop_completed=$barrierId"
    }
    Write-Output ("stopped project={0} drained={1} network={2}" -f `
        $ProjectName, $workerDrained, $network)
    exit 0
}
catch {
    [Console]::Error.WriteLine("错误：$($_.Exception.Message)")
    exit 1
}
finally {
    Exit-EaselLifecycleLock -Lock $lifecycleLock
}
