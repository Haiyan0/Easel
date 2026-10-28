<#
.SYNOPSIS
    查看完整 Desktop 的日志路径与最近日志；不读取或打印密钥。

.DESCRIPTION
    默认打印 control/egress 的最近 200 行以及 provisioner 日志路径。
    `-PathOnly` 只输出路径，便于复制到文件或交给维护者。
#>
[CmdletBinding()]
param(
    [string]$ProjectName = 'easel-workspace-desktop',
    [string]$DataRoot = 'C:\Easel\desktop',
    [ValidateRange(1, 5000)][int]$Tail = 200,
    [switch]$PathOnly
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'EaselDesktop.Common.ps1')

try {
    $ProjectName = Resolve-EaselProject -ProjectName $ProjectName
    Assert-EaselDocker
    $DataRoot = [IO.Path]::GetFullPath($DataRoot)
    $logDir = Join-Path $DataRoot 'logs'
    $provisioner = Join-Path $logDir 'provisioner.log'
    $provisionerError = Join-Path $logDir 'provisioner.err.log'
    Write-Output "log_dir=$logDir"
    Write-Output "provisioner_log=$provisioner"
    Write-Output "provisioner_error_log=$provisionerError"
    if ($PathOnly) { exit 0 }
    foreach ($service in @('control', 'egress')) {
        Write-Output "--- service=$service tail=$Tail ---"
        $ids = Invoke-EaselDocker -Arguments @(
            'ps', '-q',
            '--filter', "label=com.docker.compose.project=$ProjectName",
            '--filter', "label=com.docker.compose.service=$service")
        $container = (($ids.Output -join '').Trim())
        if ($ids.ExitCode -ne 0 -or -not $container) {
            Write-Output "error=logs_unavailable:$service"
            continue
        }
        $result = Invoke-EaselDocker -Arguments @(
            'logs', '--tail', [string]$Tail, $container)
        if ($result.ExitCode -ne 0) {
            Write-Output "error=logs_unavailable:$service"
            continue
        }
        foreach ($line in $result.Output) { Write-Output "$line" }
    }
    foreach ($path in @($provisioner, $provisionerError)) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Write-Output "--- file=$path tail=$Tail ---"
            Get-Content -LiteralPath $path -Tail $Tail |
                ForEach-Object { Write-Output "$_" }
        }
    }
    exit 0
}
catch {
    [Console]::Error.WriteLine("错误：$($_.Exception.Message)")
    exit 1
}
