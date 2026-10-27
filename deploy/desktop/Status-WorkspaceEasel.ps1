<#
.SYNOPSIS
    只读诊断完整 Desktop 开发拓扑（端口/卷/网络/key/前端/provisioner/worker）。
#>
[CmdletBinding()]
param(
    [string]$ProjectName = 'easel-workspace-desktop',
    [string]$Distro = 'Ubuntu-26.04'
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

    $networkState = 'missing'
    $inspect = Invoke-EaselDocker -Arguments @('network', 'inspect', $network)
    if ($inspect.ExitCode -eq 0) {
        $networkState = 'present'
    }
    Write-Output "network=$network state=$networkState"

    foreach ($spec in @(
            [pscustomobject]@{ Name = $volumes.Control; Role = 'control' },
            [pscustomobject]@{ Name = $volumes.Keys; Role = 'keys' })) {
        $volume = Invoke-EaselDocker -Arguments @(
            'volume', 'inspect', '--format', '{{json .Labels}}', $spec.Name)
        if ($volume.ExitCode -eq 0) {
            Write-Output "volume=$($spec.Name) state=present"
        }
        else {
            Write-Output "volume=$($spec.Name) state=missing"
        }
    }

    $control = Invoke-EaselDocker -Arguments @(
        'ps', '-q',
        '--filter', "label=com.docker.compose.project=$ProjectName",
        '--filter', 'label=com.docker.compose.service=control')
    $controlId = (($control.Output -join '').Trim())
    if ($control.ExitCode -eq 0 -and $controlId) {
        $state = Invoke-EaselDocker -Arguments @(
            'inspect', '--format',
            '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}',
            $controlId)
        Write-Output "control=$((($state.Output -join '').Trim()))"
    }
    else {
        Write-Output 'control=missing'
    }

    $socketCheck = Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'bash', '-lc', "test -S '$socket'")
    Write-Output ("provisioner_socket={0}" -f `
        $(if ($socketCheck.ExitCode -eq 0) { 'present' } else { 'missing' }))

    if ($controlId) {
        $leases = Invoke-EaselDocker -Arguments @(
            'exec', $controlId, 'python', '-c',
            ("import sqlite3;c=sqlite3.connect('/srv/easel/control/control.sqlite3');" +
             "print(','.join(f'{row[0]}:{row[1]}' for row in " +
             "c.execute('select state,count(*) from worker_leases group by state')))"))
        if ($leases.ExitCode -eq 0) {
            Write-Output "worker_leases=$((($leases.Output -join '').Trim()))"
        }
        $health = Invoke-EaselDocker -Arguments @(
            'exec', $controlId, 'python', '-c',
            "import urllib.request;urllib.request.urlopen('http://127.0.0.1:7861/api/health',timeout=3).read()")
        Write-Output ("api_health={0}" -f `
            $(if ($health.ExitCode -eq 0) { 'ok' } else { 'failed' }))
    }
    exit 0
}
catch {
    [Console]::Error.WriteLine("错误：$($_.Exception.Message)")
    exit 1
}
