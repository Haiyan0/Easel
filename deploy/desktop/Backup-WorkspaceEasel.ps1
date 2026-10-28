<#
.SYNOPSIS
    对已干净停止的完整 Desktop 部署做一致、可校验的完整备份。

.DESCRIPTION
    只接受已通过 Stop 收口的实例：控制容器必须已停止，控制库中必须存在
    匹配的停止屏障与完成记录。备份包含 control DB、master key、Ed25519
    私钥及派生公钥、受管 Agent 配置、出口策略和各 workspace 的
    workspace/state/HOME。.credentials 与 .gateway attempt 明确排除，
    由新部署重新生成。

.PARAMETER Destination
    可选；必须位于 DataRoot\backups 下的新目录。省略时自动生成时间戳目录。
#>
[CmdletBinding()]
param(
    [string]$Destination = '',
    [string]$ProjectName = 'easel-workspace-desktop',
    [string]$Distro = 'Ubuntu-26.04',
    [string]$WslRepo = '/mnt/c/Code/Easel',
    [string]$DataRoot = 'C:\Easel\desktop',
    [string]$WorkspaceRoot = 'C:\Easel\workspaces',
    [string]$Image = '',
    [ValidateRange(1, 600)][int]$LockTimeoutSeconds = 60
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
    Assert-EaselVolumeContract -ProjectName $ProjectName -Volumes $volumes `
        -IncludeEgress
    Assert-EaselNetworkContract -ProjectName $ProjectName `
        -NetworkName $network
    $DataRoot = [IO.Path]::GetFullPath($DataRoot)
    $WorkspaceRoot = [IO.Path]::GetFullPath($WorkspaceRoot)
    if (-not (Test-Path -LiteralPath $WorkspaceRoot -PathType Container)) {
        throw 'backup_workspace_root_missing'
    }
    $deploymentFile = Join-Path (Join-Path $DataRoot 'logs') 'deployment.json'
    if (-not (Test-Path -LiteralPath $deploymentFile -PathType Leaf)) {
        throw 'deployment_metadata_missing'
    }
    $deployment = Get-Content -LiteralPath $deploymentFile -Raw |
        ConvertFrom-Json
    if ([string]$deployment.project -ne $ProjectName) {
        throw 'deployment_project_mismatch'
    }
    if (-not $Image) {
        $Image = [string]$deployment.control_image
    }
    $Image = Resolve-EaselImage -Image $Image
    Test-EaselImagePin -Image $Image

    # 备份只允许在 control 已停止后进行；运行中复制会与写入者竞争。
    $running = Invoke-EaselDocker -Arguments @(
        'ps', '-q',
        '--filter', "label=com.docker.compose.project=$ProjectName",
        '--filter', 'label=com.docker.compose.service=control')
    if ($running.ExitCode -ne 0) {
        throw 'control_lookup_failed'
    }
    if ((($running.Output -join '').Trim())) {
        throw 'backup_requires_stopped'
    }

    $backupsRoot = Join-Path $DataRoot 'backups'
    $null = New-Item -ItemType Directory -Path $backupsRoot -Force
    Set-EaselPrivateAcl -Path $backupsRoot -Directory
    if (-not $Destination) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $Destination = Join-Path $backupsRoot ("desktop-$stamp")
    }
    $Destination = [IO.Path]::GetFullPath($Destination)
    $prefix = $backupsRoot.TrimEnd('\') + '\'
    if (-not $Destination.StartsWith($prefix,
            [StringComparison]::OrdinalIgnoreCase)) {
        throw 'backup_destination_outside_backups'
    }
    if (Test-Path -LiteralPath $Destination) {
        throw 'backup_exists'
    }
    $relative = $Destination.Substring($prefix.Length).Replace('\', '/')
    $wslData = ConvertTo-EaselWslPath -Path $DataRoot
    $wslWorkspace = ConvertTo-EaselWslPath -Path $WorkspaceRoot
    $containerDestination = '/data/backups/' + $relative

    $arguments = @(
        '-d', $Distro, '--', 'docker', 'run', '--rm',
        '--network', 'none', '--user', '0:0',
        '--mount', "type=volume,src=$($volumes.Control),dst=/data/control",
        '--mount', "type=bind,src=$wslWorkspace,dst=/data/workspaces",
        '--mount', "type=bind,src=$wslData,dst=/data",
        '--mount', "type=volume,src=$($volumes.Keys),dst=/keys,readonly",
        $Image,
        'python', '-m', 'easel', 'admin', 'backup',
        '--control-dir', '/data/control',
        '--destination', $containerDestination,
        '--master-key-file', '/keys/master.key',
        '--workspace-root', '/data/workspaces',
        '--signing-key-file', '/keys/snapshot.pem',
        '--managed-agent-config', '/keys/managed-agent.json',
        '--egress-policy-file', '/keys/egress-policy.json',
        '--egress-ca-file', '/keys/egress-ca.pem',
        '--deployment-json', '/data/logs/deployment.json',
        '--image-digest', [string]$deployment.worker_image,
        '--harness-revision', [string]$deployment.harness_revision,
        '--require-clean-stop'
    )
    $result = Invoke-Wsl -Arguments $arguments
    $text = ($result.Output -join "`n")
    if ($result.ExitCode -ne 0) {
        [Console]::Error.WriteLine("错误：backup_failed`n$text")
        exit 1
    }
    if (-not (Test-Path -LiteralPath $Destination -PathType Container)) {
        throw 'backup_destination_missing'
    }
    $manifest = Assert-EaselBackupManifest -Backup $Destination `
        -RequireComplete
    Set-EaselPrivateAcl -Path $Destination -Directory
    $manifestSha = Get-EaselFileSha256 -Path (
        Join-Path $Destination 'manifest.json')
    Write-Output "backup=$Destination"
    Write-Output "manifest_sha256=$manifestSha"
    Write-Output "version=$([int]$manifest.version)"
    Write-Output "schema_version=$([int]$manifest.schema_version)"
    Write-Output ("workspaces=" + @($manifest.workspaces.PSObject.Properties).Count)
    Write-Output 'clean_stop=true'
    Write-Output "control_image=$([string]$manifest.deployment.control_image)"
    Write-Output "worker_image=$([string]$manifest.deployment.worker_image)"
    Write-Output "gateway_image=$([string]$manifest.deployment.gateway_image)"
    exit 0
}
catch {
    [Console]::Error.WriteLine("错误：$($_.Exception.Message)")
    exit 1
}
finally {
    Exit-EaselLifecycleLock -Lock $lifecycleLock
}
