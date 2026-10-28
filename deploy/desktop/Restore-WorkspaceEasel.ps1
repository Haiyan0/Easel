<#
.SYNOPSIS
    把完整备份恢复到一个全新的 Desktop 项目/目录/端口。

.DESCRIPTION
    源部署必须已停止；本脚本只读备份，创建新的专用卷、控制网络、
    key/egress 卷和 workspace 根，不接管旧实例、不覆盖旧数据。恢复流程：
    只读校验 → 新目录恢复 → 清理运行期身份/撤销旧登录与 token → 落盘到
    新卷 → 派生公钥 → 写入一次性管理员恢复邀请文件。启动仍必须使用
    Start-WorkspaceEasel.ps1。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Backup,
    [Parameter(Mandatory = $true)][string]$ProjectName,
    [Parameter(Mandatory = $true)][string]$PublicOrigin,
    [Parameter(Mandatory = $true)][string]$Image,
    [Parameter(Mandatory = $true)][string]$WorkerImage,
    [Parameter(Mandatory = $true)][string]$GatewayImage,
    [ValidateRange(1, 65535)][int]$Port = 7862,
    [string]$Distro = 'Ubuntu-26.04',
    [string]$WslRepo = '/mnt/c/Code/Easel',
    [string]$DataRoot = 'C:\Easel\desktop-restored',
    [string]$WorkspaceRoot = 'C:\Easel\workspaces-restored',
    [int]$ProvisionerGid = 1000,
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

function Invoke-EaselVolumeInspect {
    param([Parameter(Mandatory)][string]$Name)
    return Invoke-EaselDocker -Arguments @(
        'volume', 'inspect', '--format', '{{json .Labels}}', $Name)
}

$lifecycleLock = $null
$network = ''
$staging = ''
$created = @{
    Volumes = New-Object System.Collections.Generic.List[string]
    Network = $false
    WorkspaceRoot = $false
    DataRoot = $false
}
$Invite = ''
try {
    $ProjectName = Resolve-EaselProject -ProjectName $ProjectName
    $PublicOrigin = Resolve-EaselOrigin -Origin $PublicOrigin
    $Image = Resolve-EaselImage -Image $Image
    $WorkerImage = Resolve-EaselImage -Image $WorkerImage
    $GatewayImage = Resolve-EaselImage -Image $GatewayImage
    $Port = Resolve-EaselPort -Port $Port
    Assert-EaselDocker
    $lifecycleLock = Enter-EaselLifecycleLock `
        -ProjectName $ProjectName -TimeoutSeconds $LockTimeoutSeconds

    $Backup = [IO.Path]::GetFullPath($Backup)
    $DataRoot = [IO.Path]::GetFullPath($DataRoot)
    $WorkspaceRoot = [IO.Path]::GetFullPath($WorkspaceRoot)
    if (-not (Test-Path -LiteralPath $Backup -PathType Container)) {
        throw 'backup_missing'
    }
    $manifest = Assert-EaselBackupManifest -Backup $Backup -RequireComplete
    if ([string]$manifest.deployment.project -eq $ProjectName) {
        throw 'restore_target_same_project'
    }
    foreach ($name in @('control_image', 'worker_image', 'gateway_image')) {
        $value = [string]$manifest.deployment.$name
        if ($value -notmatch '^sha256:[0-9a-f]{64}$') {
            throw 'backup_manifest_invalid'
        }
    }
    if ($manifest.deployment.workspace_root) {
        $sourceWorkspace = [IO.Path]::GetFullPath(
            [string]$manifest.deployment.workspace_root)
        $a = $sourceWorkspace.TrimEnd('\') + '\'
        $b = $WorkspaceRoot.TrimEnd('\') + '\'
        if ($a.StartsWith($b, [StringComparison]::OrdinalIgnoreCase) -or
            $b.StartsWith($a, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'restore_target_overlaps_source'
        }
    }
    if ($manifest.deployment.data_root) {
        $sourceData = [IO.Path]::GetFullPath(
            [string]$manifest.deployment.data_root)
        $a = $sourceData.TrimEnd('\') + '\'
        $b = $DataRoot.TrimEnd('\') + '\'
        if ($a.StartsWith($b, [StringComparison]::OrdinalIgnoreCase) -or
            $b.StartsWith($a, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'restore_target_overlaps_source'
        }
    }
    if ($Backup.StartsWith($WorkspaceRoot.TrimEnd('\') + '\',
            [StringComparison]::OrdinalIgnoreCase) -or
        $WorkspaceRoot.StartsWith($Backup.TrimEnd('\') + '\',
            [StringComparison]::OrdinalIgnoreCase) -or
        $Backup.StartsWith($DataRoot.TrimEnd('\') + '\',
            [StringComparison]::OrdinalIgnoreCase) -or
        $DataRoot.StartsWith($Backup.TrimEnd('\') + '\',
            [StringComparison]::OrdinalIgnoreCase)) {
        throw 'restore_target_contains_backup'
    }

    # 目标必须是全新的；同名卷/网络已有任何资源都拒绝接管。
    $volumes = Get-EaselVolumeNames -ProjectName $ProjectName
    $network = Get-EaselWorkspaceNetworkName -ProjectName $ProjectName
    foreach ($spec in @(
            [pscustomobject]@{ Name = $volumes.Control; Role = 'control' },
            [pscustomobject]@{ Name = $volumes.Keys; Role = 'keys' },
            [pscustomobject]@{ Name = $volumes.Egress; Role = 'egress-policy' })) {
        $inspect = Invoke-EaselDocker -Arguments @(
            'volume', 'inspect', $spec.Name)
        if ($inspect.ExitCode -eq 0) {
            throw "restore_target_exists:$($spec.Name)"
        }
    }
    $networkInspect = Invoke-EaselDocker -Arguments @(
        'network', 'inspect', $network)
    if ($networkInspect.ExitCode -eq 0) {
        throw "restore_target_exists:$network"
    }
    if (Test-Path -LiteralPath $WorkspaceRoot) {
        if (-not (Test-Path -LiteralPath $WorkspaceRoot -PathType Container)) {
            throw 'restore_target_not_empty'
        }
        if (@(Get-ChildItem -LiteralPath $WorkspaceRoot -Force).Count -gt 0) {
            throw 'restore_target_not_empty'
        }
    }
    else {
        $null = New-Item -ItemType Directory -Path $WorkspaceRoot
        $created.WorkspaceRoot = $true
    }
    if (Test-Path -LiteralPath $DataRoot) {
        if (-not (Test-Path -LiteralPath $DataRoot -PathType Container) -or
            @(Get-ChildItem -LiteralPath $DataRoot -Force).Count -gt 0) {
            throw 'restore_target_not_empty'
        }
    }
    else {
        $null = New-Item -ItemType Directory -Path $DataRoot
        $created.DataRoot = $true
    }
    foreach ($name in @('public', 'logs', 'recovery')) {
        $null = New-Item -ItemType Directory -Path (
            Join-Path $DataRoot $name) -Force
    }
    $null = New-Item -ItemType Directory -Path (
        Join-Path $DataRoot 'backups') -Force

    # 目标镜像必须真实存在且摘要匹配，不能等 Start 才发现“坏镜像”。
    foreach ($pin in @($Image, $WorkerImage, $GatewayImage)) {
        $inspect = Invoke-EaselDocker -Arguments @(
            'image', 'inspect', '--format', '{{.Id}}', $pin)
        if ($inspect.ExitCode -ne 0 -or
            (($inspect.Output -join '').Trim()) -ne $pin) {
            throw "restore_image_unavailable:$pin"
        }
    }

    foreach ($spec in @(
            [pscustomobject]@{ Name = $volumes.Control; Role = 'control' },
            [pscustomobject]@{ Name = $volumes.Keys; Role = 'keys' },
            [pscustomobject]@{ Name = $volumes.Egress; Role = 'egress-policy' })) {
        $create = Invoke-EaselDocker -Arguments @(
            'volume', 'create',
            '--label', "easel.desktop.project=$ProjectName",
            '--label', "easel.desktop.role=$($spec.Role)",
            $spec.Name)
        if ($create.ExitCode -ne 0) {
            throw "volume_create_failed:$($spec.Name)"
        }
        $created.Volumes.Add($spec.Name)
    }
    $createNetwork = Invoke-EaselDocker -Arguments @(
        'network', 'create',
        '--label', "easel.desktop.project=$ProjectName",
        '--label', 'easel.desktop.role=control-network',
        $network)
    if ($createNetwork.ExitCode -ne 0) {
        throw 'network_create_failed'
    }
    $created.Network = $true
    Assert-EaselVolumeContract -ProjectName $ProjectName -Volumes $volumes `
        -IncludeEgress
    Assert-EaselNetworkContract -ProjectName $ProjectName `
        -NetworkName $network

    $chown = Invoke-EaselDocker -Arguments @(
        'run', '--rm', '--user', '0:0', '--entrypoint', 'sh',
        '--mount', "type=volume,src=$($volumes.Control),dst=/data",
        '--mount', "type=volume,src=$($volumes.Keys),dst=/keys",
        '--mount', "type=volume,src=$($volumes.Egress),dst=/policy",
        $Image,
        '-c', 'chown 10001:10001 /data /keys /policy && chmod 750 /data /keys /policy')
    if ($chown.ExitCode -ne 0) {
        throw 'volume_permission_failed'
    }

    $stagingName = '.restore-staging-' + [guid]::NewGuid().ToString('N')
    $staging = Join-Path $DataRoot $stagingName
    $null = New-Item -ItemType Directory -Path $staging
    Set-EaselPrivateAcl -Path $staging -Directory
    $wslBackup = ConvertTo-EaselWslPath -Path $Backup
    $wslData = ConvertTo-EaselWslPath -Path $DataRoot
    $wslWorkspace = ConvertTo-EaselWslPath -Path $WorkspaceRoot
    $containerStaging = '/data/' + $stagingName
    $mounts = @(
        '--mount', "type=bind,src=$wslBackup,dst=/backup,readonly",
        '--mount', "type=bind,src=$wslData,dst=/data",
        '--mount', "type=bind,src=$wslWorkspace,dst=/data/workspaces",
        '--mount', "type=volume,src=$($volumes.Control),dst=/data/control",
        '--mount', "type=volume,src=$($volumes.Keys),dst=/keys",
        '--mount', "type=volume,src=$($volumes.Egress),dst=/policy"
    )

    $verify = Invoke-Wsl -Arguments (@(
        '-d', $Distro, '--', 'docker', 'run', '--rm', '--network', 'none',
        '--read-only', '--user', '10001:10001', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges:true',
        '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=32m',
        '--mount', "type=bind,src=$wslBackup,dst=/backup,readonly",
        $Image, 'python', '-m', 'easel', 'admin', 'verify-backup',
        '--source', '/backup'))
    if ($verify.ExitCode -ne 0) {
        throw ("backup_verify_failed:" + (($verify.Output -join ',')).Trim())
    }

    $restore = Invoke-Wsl -Arguments (@(
        '-d', $Distro, '--', 'docker', 'run', '--rm', '--network', 'none',
        '--user', '0:0') + $mounts + @(
        $Image, 'python', '-m', 'easel', 'admin', 'restore',
        '--source', '/backup', '--destination', $containerStaging,
        '--managed-root', '/data'))
    if ($restore.ExitCode -ne 0) {
        throw ("restore_failed:" + (($restore.Output -join ',')).Trim())
    }

    $prepare = Invoke-Wsl -Arguments (@(
        '-d', $Distro, '--', 'docker', 'run', '--rm', '--network', 'none',
        '--user', '0:0') + $mounts + @(
        $Image, 'python', '-m', 'easel', 'admin', 'prepare-restore',
        '--destination', $containerStaging))
    if ($prepare.ExitCode -ne 0) {
        throw ("restore_prepare_failed:" + (($prepare.Output -join ',')).Trim())
    }
    foreach ($line in $prepare.Output) {
        if ("$line".StartsWith('recovery_invite=')) {
            $Invite = "$line".Substring(16).Trim()
        }
    }
    if (-not $Invite) {
        throw 'restore_invite_missing'
    }

    $materialize = Invoke-Wsl -Arguments (@(
        '-d', $Distro, '--', 'docker', 'run', '--rm', '--network', 'none',
        '--user', '0:0') + $mounts + @(
        $Image, 'python', '-m', 'easel', 'admin', 'materialize-restore',
        '--source', $containerStaging,
        '--control-dir', '/data/control',
        '--keys-dir', '/keys',
        '--egress-dir', '/policy',
        '--workspace-root', '/data/workspaces'))
    if ($materialize.ExitCode -ne 0) {
        throw ("restore_materialize_failed:" +
            (($materialize.Output -join ',')).Trim())
    }

    $permissions = Invoke-Wsl -Arguments (@(
        '-d', $Distro, '--', 'docker', 'run', '--rm', '--network', 'none',
        '--user', '0:0') + $mounts + @(
        $Image, 'sh', '-c',
        'chown -R 10001:10001 /data/control /keys /policy /data/workspaces'))
    if ($permissions.ExitCode -ne 0) {
        throw 'restore_permission_failed'
    }
    $writable = Invoke-Wsl -Arguments (@(
        '-d', $Distro, '--', 'docker', 'run', '--rm', '--network', 'none',
        '--read-only', '--user', '10001:10001', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges:true',
        '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=32m') + $mounts + @(
        $Image, 'sh', '-c',
        'test -w /data/control && test -w /keys && test -w /policy && test -w /data/workspaces'))
    if ($writable.ExitCode -ne 0) {
        throw 'restore_permission_failed'
    }

    $export = Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'docker', 'run', '--rm', '--network', 'none',
        '--user', '0:0',
        '--mount', "type=bind,src=$wslData,dst=/data",
        '--mount', "type=volume,src=$($volumes.Keys),dst=/keys,readonly",
        $Image, 'sh', '-c',
        ('cp /keys/worker-public.key /data/public/worker-public.key && ' +
         'cp /keys/gateway-public.pem /data/public/gateway-public.pem && ' +
         'cp /keys/egress-policy.json /data/public/egress-policy.json && ' +
         'chmod 644 /data/public/worker-public.key ' +
         '/data/public/gateway-public.pem /data/public/egress-policy.json'))
    if ($export.ExitCode -ne 0) {
        throw ("restore_public_export_failed:" +
            (($export.Output -join ',')).Trim())
    }

    $inviteFile = Join-Path (Join-Path $DataRoot 'recovery') 'admin-invite.txt'
    [IO.File]::WriteAllText(
        $inviteFile, $Invite, (New-Object System.Text.UTF8Encoding($false)))
    Set-EaselPrivateAcl -Path $inviteFile
    $Invite = ''

    Remove-Item -LiteralPath $staging -Recurse -Force
    $created.WorkspaceRoot = $false
    $created.DataRoot = $false
    $workspaceCount = @($manifest.workspaces.PSObject.Properties).Count
    Write-Output "restored_project=$ProjectName"
    Write-Output "restored_schema=$([int]$manifest.schema_version)"
    Write-Output "restored_workspaces=$workspaceCount"
    Write-Output "recovery_invite_file=$inviteFile"
    Write-Output "next=Start-WorkspaceEasel.ps1 -ProjectName $ProjectName -PublicOrigin $PublicOrigin -Image $Image -WorkerImage $WorkerImage -GatewayImage $GatewayImage -Port $Port -DataRoot $DataRoot -WorkspaceRoot $WorkspaceRoot"
    exit 0
}
catch {
    if ($Invite) { $Invite = '' }
    if ($staging -and (Test-Path -LiteralPath $staging)) {
        Remove-Item -LiteralPath $staging -Recurse -Force `
            -ErrorAction SilentlyContinue
    }
    foreach ($name in $created.Volumes) {
        $labels = Invoke-EaselVolumeInspect -Name $name
        if ($labels.ExitCode -eq 0) {
            try {
                $parsed = (($labels.Output -join '') | ConvertFrom-Json)
                if ([string]$parsed.'easel.desktop.project' -eq $ProjectName) {
                    $null = Invoke-EaselDocker -Arguments @(
                        'volume', 'rm', '-f', $name)
                }
            }
            catch { }
        }
    }
    if ($created.Network) {
        $null = Invoke-EaselDocker -Arguments @(
            'network', 'rm', $network)
    }
    if ($created.WorkspaceRoot -and (Test-Path -LiteralPath $WorkspaceRoot)) {
        Remove-Item -LiteralPath $WorkspaceRoot -Recurse -Force `
            -ErrorAction SilentlyContinue
    }
    if ($created.DataRoot -and (Test-Path -LiteralPath $DataRoot)) {
        Remove-Item -LiteralPath $DataRoot -Recurse -Force `
            -ErrorAction SilentlyContinue
    }
    [Console]::Error.WriteLine("错误：$($_.Exception.Message)")
    exit 1
}
finally {
    Exit-EaselLifecycleLock -Lock $lifecycleLock
}
