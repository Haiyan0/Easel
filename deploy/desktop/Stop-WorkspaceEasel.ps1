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

try {
    $ProjectName = Resolve-EaselProject -ProjectName $ProjectName
    Assert-EaselDocker
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
    $drained = $false
    if (-not $Force -and $socketCheck.ExitCode -eq 0 -and $controlId) {
        $drain = Invoke-EaselDocker -Arguments @(
            'exec', $controlId, 'python', '-m', 'easel', 'admin', 'drain',
            '--control-dir', '/srv/easel/control',
            '--provisioner-socket', '/run/easel/provisioner.sock',
            '--gateway-required')
        if ($drain.ExitCode -ne 0) {
            throw 'worker_drain_failed'
        }
        $drained = $true
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
        "EASEL_CONTROL_PORT=7861",
        "EASEL_CONTROL_NETWORK=$network",
        "EASEL_PROVISIONER_GID=1000",
        "EASEL_PROVISIONER_SOCKET=$socket"
    )
    $composeArgs = @('-d', $Distro, '--', 'env') + $envArgs + @(
        'docker', 'compose', '-p', $ProjectName, '-f', $composeWsl, 'down')
    $down = Invoke-Wsl -Arguments $composeArgs
    if ($down.ExitCode -ne 0) {
        throw 'control_stop_failed'
    }
    $kill = Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'pkill', '-f', $socket)
    Write-Output ("stopped project={0} drained={1} network={2}" -f `
        $ProjectName, $drained, $network)
    exit 0
}
catch {
    [Console]::Error.WriteLine("错误：$($_.Exception.Message)")
    exit 1
}
