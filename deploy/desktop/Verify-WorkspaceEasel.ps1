<#
.SYNOPSIS
    只读校验完整备份；不恢复、不生成 key、不改写备份。

.DESCRIPTION
    校验 manifest、文件集合/摘要、SQLite integrity/schema/密文、签名私钥
    与派生公钥、受管配置和出口策略。适合在恢复或升级前先跑一遍。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Backup,
    [Parameter(Mandatory = $true)][string]$Image,
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
    Assert-EaselDocker
    $Image = Resolve-EaselImage -Image $Image
    Test-EaselImagePin -Image $Image
    $Backup = [IO.Path]::GetFullPath($Backup)
    if (-not (Test-Path -LiteralPath $Backup -PathType Container)) {
        throw 'backup_missing'
    }
    $manifest = Assert-EaselBackupManifest -Backup $Backup -RequireComplete
    $wslBackup = ConvertTo-EaselWslPath -Path $Backup
    $result = Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'docker', 'run', '--rm',
        '--network', 'none', '--read-only', '--user', '10001:10001',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
        '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=32m',
        '--mount', "type=bind,src=$wslBackup,dst=/backup,readonly",
        $Image, 'python', '-m', 'easel', 'admin', 'verify-backup',
        '--source', '/backup')
    if ($result.ExitCode -ne 0) {
        [Console]::Error.WriteLine(
            "错误：backup_verify_failed`n" + ($result.Output -join "`n"))
        exit 1
    }
    foreach ($line in $result.Output) { Write-Output "$line" }
    $manifestPath = Join-Path $Backup 'manifest.json'
    $manifestSha = Get-EaselFileSha256 -Path $manifestPath
    Write-Output "backup_manifest_sha256=$manifestSha"
    Write-Output "verified_backup=$Backup"
    exit 0
}
catch {
    [Console]::Error.WriteLine("错误：$($_.Exception.Message)")
    exit 1
}
