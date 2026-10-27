<#
.SYNOPSIS
    初始化完整 Desktop 开发拓扑的专用卷、控制网络与两类 key。

.DESCRIPTION
    只创建/校验本项目专用资源：control 数据卷、key 卷、控制网络。master key
    与 Ed25519 签名私钥只创建一次，重复执行不轮换；worker/gateway 只得到
    派生公钥。受管 Agent 配置与出口策略复制进 key 卷（0600），不读取任何
    供应商秘密或 ngrok authtoken。

.PARAMETER Image
    固定 control 镜像 ID（sha256:...）。

.PARAMETER ManagedAgentConfig
    受管 Agent 配置 JSON（revision/agent/broker）；不得包含供应商 Key。

.PARAMETER EgressPolicy
    管理员出口策略 JSON（approved_hosts/approved_cidrs）。

.PARAMETER ProjectName
    专用 Compose 项目名，默认 easel-workspace-desktop。

.PARAMETER DataRoot
    本项目非秘密公共文件目录（worker/gateway 公钥、出口策略副本）。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Image,
    [Parameter(Mandatory = $true)][string]$ManagedAgentConfig,
    [Parameter(Mandatory = $true)][string]$EgressPolicy,
    [string]$ProjectName = 'easel-workspace-desktop',
    [string]$DataRoot = 'C:\Easel\desktop',
    [string]$WorkspaceRoot = 'C:\Easel\workspaces'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'EaselDesktop.Common.ps1')

try {
    $Image = Resolve-EaselImage -Image $Image
    $ProjectName = Resolve-EaselProject -ProjectName $ProjectName
    Assert-EaselDocker
    $volumes = Get-EaselVolumeNames -ProjectName $ProjectName
    $network = Get-EaselWorkspaceNetworkName -ProjectName $ProjectName
    $compose = Get-EaselWorkspaceComposePath
    if (-not (Test-Path -LiteralPath $compose -PathType Leaf)) {
        throw 'compose_missing'
    }

    $imageInspect = Invoke-EaselDocker -Arguments @(
        'image', 'inspect', '--format', '{{.Id}}', $Image)
    if ($imageInspect.ExitCode -ne 0) {
        throw 'control_image_unavailable'
    }
    if ((($imageInspect.Output -join '').Trim()) -ne $Image) {
        throw 'control_image_mismatch'
    }

    foreach ($file in @($ManagedAgentConfig, $EgressPolicy)) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
            throw 'config_file_missing'
        }
    }
    $ManagedAgentConfig = (Resolve-Path -LiteralPath $ManagedAgentConfig).Path
    $EgressPolicy = (Resolve-Path -LiteralPath $EgressPolicy).Path
    $DataRoot = [IO.Path]::GetFullPath($DataRoot)
    $WorkspaceRoot = [IO.Path]::GetFullPath($WorkspaceRoot)
    try {
        $managed = Get-Content -LiteralPath $ManagedAgentConfig -Raw |
            ConvertFrom-Json
        if ($null -eq $managed.broker -or
            [string]$managed.broker.mode -ne 'workspace-sidecar' -or
            [string]$managed.broker.channel -ne 'chat') {
            throw 'managed_config_invalid'
        }
        if ($null -eq $managed.agent -or
            [string]$managed.agent.runtime -ne 'acp') {
            throw 'managed_config_invalid'
        }
        $null = Get-Content -LiteralPath $EgressPolicy -Raw |
            ConvertFrom-Json
    }
    catch {
        throw 'config_invalid'
    }

    $volumeSpecs = @(
        [pscustomobject]@{ Name = $volumes.Control; Role = 'control' },
        [pscustomobject]@{ Name = $volumes.Keys; Role = 'keys' }
    )
    foreach ($spec in $volumeSpecs) {
        $inspect = Invoke-EaselDocker -Arguments @(
            'volume', 'inspect', '--format', '{{json .Labels}}', $spec.Name)
        if ($inspect.ExitCode -ne 0) {
            $created = Invoke-EaselDocker -Arguments @(
                'volume', 'create',
                '--label', "easel.desktop.project=$ProjectName",
                '--label', "easel.desktop.role=$($spec.Role)",
                $spec.Name)
            if ($created.ExitCode -ne 0) {
                throw "volume_create_failed:$($spec.Name)"
            }
            $inspect = Invoke-EaselDocker -Arguments @(
                'volume', 'inspect', '--format', '{{json .Labels}}',
                $spec.Name)
        }
        if ($inspect.ExitCode -ne 0) {
            throw "volume_inspect_failed:$($spec.Name)"
        }
        $labelsText = (($inspect.Output -join '').Trim())
        $labels = $null
        if ($labelsText) {
            try { $labels = $labelsText | ConvertFrom-Json }
            catch { throw 'volume_labels_invalid' }
        }
        Assert-EaselVolumeOwnership -Labels $labels `
            -ProjectName $ProjectName -Role $spec.Role
    }

    $networkInspect = Invoke-EaselDocker -Arguments @(
        'network', 'inspect', $network)
    if ($networkInspect.ExitCode -ne 0) {
        $created = Invoke-EaselDocker -Arguments @(
            'network', 'create',
            '--label', "easel.desktop.project=$ProjectName",
            '--label', 'easel.desktop.role=control-network',
            $network)
        if ($created.ExitCode -ne 0) {
            throw 'network_create_failed'
        }
    }
    Assert-EaselNetworkContract -ProjectName $ProjectName `
        -NetworkName $network

    $chown = Invoke-EaselDocker -Arguments @(
        'run', '--rm', '--user', '0:0', '--entrypoint', 'sh',
        '--mount', "type=volume,src=$($volumes.Control),dst=/data",
        '--mount', "type=volume,src=$($volumes.Keys),dst=/keys",
        $Image,
        '-c', 'chown 10001:10001 /data /keys && chmod 750 /data /keys')
    if ($chown.ExitCode -ne 0) {
        throw 'volume_permission_failed'
    }

    $keyResult = Invoke-EaselDocker -Arguments @(
        'run', '--rm', '--user', '10001:10001',
        '--entrypoint', 'python',
        '--mount', "type=volume,src=$($volumes.Keys),dst=/keys",
        $Image,
        '/opt/easel/deploy/desktop/keygen.py', '--ensure',
        '--path', '/keys/master.key',
        '--signing-key', '/keys/snapshot.pem',
        '--worker-public', '/keys/worker-public.key',
        '--gateway-public', '/keys/gateway-public.pem')
    if ($keyResult.ExitCode -ne 0) {
        throw 'key_initialization_failed'
    }

    $copyConfig = Invoke-EaselDocker -Arguments @(
        'run', '--rm', '--user', '0:0', '--entrypoint', 'sh',
        '--mount', "type=volume,src=$($volumes.Keys),dst=/keys",
        '--mount', "type=bind,src=$ManagedAgentConfig,dst=/src/managed.json,readonly",
        '--mount', "type=bind,src=$EgressPolicy,dst=/src/policy.json,readonly",
        $Image,
        '-c',
        ('cp /src/managed.json /keys/managed-agent.json && ' +
         'cp /src/policy.json /keys/egress-policy.json && ' +
         'chown 10001:10001 /keys/managed-agent.json /keys/egress-policy.json && ' +
         'chmod 600 /keys/managed-agent.json /keys/egress-policy.json'))
    if ($copyConfig.ExitCode -ne 0) {
        throw 'config_copy_failed'
    }

    $publicDir = Join-Path $DataRoot 'public'
    $null = New-Item -ItemType Directory -Path $publicDir -Force
    $exportPublic = Invoke-EaselDocker -Arguments @(
        'run', '--rm', '--user', '0:0', '--entrypoint', 'sh',
        '--mount', "type=volume,src=$($volumes.Keys),dst=/keys,readonly",
        '--mount', "type=bind,src=$publicDir,dst=/out",
        $Image,
        '-c',
        ('cp /keys/worker-public.key /out/worker-public.key && ' +
         'cp /keys/gateway-public.pem /out/gateway-public.pem && ' +
         'cp /keys/egress-policy.json /out/egress-policy.json && ' +
         'chmod 644 /out/worker-public.key /out/gateway-public.pem /out/egress-policy.json'))
    if ($exportPublic.ExitCode -ne 0) {
        throw 'public_key_export_failed'
    }
    $null = New-Item -ItemType Directory -Path $WorkspaceRoot -Force

    $keyStatus = @(
        $keyResult.Output |
        Where-Object { $_ -match '^(key|signing_key)=' } |
        ForEach-Object { "$_" }) -join ','
    if (-not $keyStatus) {
        throw 'key_status_missing'
    }
    Write-Output ("initialized project={0} control_volume={1} key_volume={2} network={3} {4}" -f `
        $ProjectName, $volumes.Control, $volumes.Keys, $network, $keyStatus)
    exit 0
}
catch {
    [Console]::Error.WriteLine("错误：$($_.Exception.Message)")
    exit 1
}
