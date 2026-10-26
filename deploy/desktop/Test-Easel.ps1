<#
.SYNOPSIS
    只读诊断 Easel Windows Docker Desktop 登录管理面。

.DESCRIPTION
    校验固定镜像、专用卷、master key、Compose 配置和运行中容器的实际隔离
    属性；不启动、不停止、不删除任何资源。诊断只输出检查名与状态码，不解析
    或回显 master key、邀请、Cookie、ngrok 访问令牌。

.PARAMETER PublicOrigin
    当前 control 使用的 ngrok HTTPS Origin。

.PARAMETER Image
    固定 control 镜像 ID（sha256:...）。

.PARAMETER ProjectName
    专用 Compose 项目名，默认 easel-login-desktop。

.PARAMETER Port
    宿主回环端口，默认 7861。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PublicOrigin,
    [Parameter(Mandatory = $true)][string]$Image,
    [string]$ProjectName = 'easel-login-desktop',
    [ValidateRange(1, 65535)][int]$Port = 7861
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'EaselDesktop.Common.ps1')

try {
    $PublicOrigin = Resolve-EaselOrigin -Origin $PublicOrigin
    $Image = Resolve-EaselImage -Image $Image
    $ProjectName = Resolve-EaselProject -ProjectName $ProjectName
    $Port = Resolve-EaselPort -Port $Port
    Assert-EaselDocker

    $compose = Get-EaselComposePath
    if (-not (Test-Path -LiteralPath $compose -PathType Leaf)) {
        throw 'compose_missing'
    }
    $volumes = Get-EaselVolumeNames -ProjectName $ProjectName
    # 诊断前先复核卷的真实名称/标签/结构，不能只信预期名称。
    Assert-EaselVolumeContract -ProjectName $ProjectName -Volumes $volumes
    Test-EaselImagePin -Image $Image
    Set-EaselComposeEnvironment -Image $Image `
        -PublicOrigin $PublicOrigin -ProjectName $ProjectName -Port $Port

    $configured = Invoke-EaselDocker -Arguments @(
        'compose', '-p', $ProjectName, '-f', $compose,
        'config', '--quiet')
    if ($configured.ExitCode -ne 0) {
        throw 'compose_config_invalid'
    }

    $running = Invoke-EaselDocker -Arguments @(
        'compose', '-p', $ProjectName, '-f', $compose,
        'ps', '-q', 'control')
    $containerLines = @($running.Output | Where-Object {
        $_ -and $_.Trim()
    })
    if ($running.ExitCode -ne 0 -or $containerLines.Count -ne 1) {
        throw 'control_container_ambiguous'
    }
    $containerId = $containerLines[0].Trim()

    $inspect = Invoke-EaselDocker -Arguments @('inspect', $containerId)
    if ($inspect.ExitCode -ne 0) {
        throw 'control_inspect_failed'
    }
    $inspectText = (($inspect.Output -join "`n").Trim())
    if (-not $inspectText) {
        throw 'container_inspect_invalid'
    }
    try {
        $parsed = $inspectText | ConvertFrom-Json
    }
    catch {
        throw 'container_inspect_invalid'
    }
    $items = @($parsed)
    if ($items.Count -ne 1 -or $null -eq $items[0]) {
        throw 'container_inspect_invalid'
    }
    $container = $items[0]
    if ($null -eq $container.Config -or $null -eq $container.HostConfig -or
        $null -eq $container.Mounts -or
        $null -eq $container.NetworkSettings) {
        throw 'container_inspect_invalid'
    }

    $mounts = @($container.Mounts)
    $controlMounts = @($mounts | Where-Object {
        [string]$_.Destination -eq '/srv/easel/control'
    })
    $keyMounts = @($mounts | Where-Object {
        [string]$_.Destination -eq '/keys'
    })
    $controlVolumeName = ''
    if ($controlMounts.Count -eq 1) {
        $controlVolumeName = [string]$controlMounts[0].Name
    }
    $keyVolumeName = ''
    if ($keyMounts.Count -eq 1) {
        $keyVolumeName = [string]$keyMounts[0].Name
    }

    $capDrop = @($container.HostConfig.CapDrop)
    $securityOpt = (@($container.HostConfig.SecurityOpt) -join ',')
    $labels = $container.Config.Labels
    $composeProject = ''
    $composeService = ''
    if ($null -ne $labels) {
        $composeProject = [string]$labels.'com.docker.compose.project'
        $composeService = [string]$labels.'com.docker.compose.service'
    }

    $actualCommand = @()
    if ($null -ne $container.Config.Entrypoint) {
        $actualCommand += @($container.Config.Entrypoint)
    }
    if ($null -ne $container.Config.Cmd) {
        $actualCommand += @($container.Config.Cmd)
    }
    $expectedCommand = @(
        'python', '-m', 'easel', 'serve-control',
        '--control-dir', '/srv/easel/control',
        '--public-origin', $PublicOrigin,
        '--master-key-file', '/keys/master.key',
        '--frontend-dir', '/opt/easel/web/frontend/dist',
        '--host', '0.0.0.0', '--port', '7861'
    )
    $checksCommand = ($actualCommand -join "`n") -eq (
        $expectedCommand -join "`n")

    $portProperties = @()
    if ($null -ne $container.NetworkSettings.Ports) {
        $portProperties = @(
            $container.NetworkSettings.Ports.PSObject.Properties)
    }
    $portBindings = @()
    if ($portProperties.Count -eq 1) {
        $portBindings = @($portProperties[0].Value)
    }

    $healthStatus = ''
    if ($null -ne $container.State.Health) {
        $healthStatus = [string]$container.State.Health.Status
    } else {
        $healthStatus = [string]$container.State.Status
    }

    $checks = [ordered]@{}
    $checks['image_matches'] = ([string]$container.Image -eq $Image)
    $checks['compose_project'] = ($composeProject -eq $ProjectName)
    $checks['compose_service'] = ($composeService -eq 'control')
    $checks['user_10001'] = ([string]$container.Config.User -eq '10001:10001')
    $checks['read_only_rootfs'] = [bool]$container.HostConfig.ReadonlyRootfs
    $checks['cap_drop_all'] = ($capDrop -contains 'ALL')
    $checks['no_new_privileges'] = ($securityOpt -match 'no-new-privileges')
    $checks['not_privileged'] = (-not [bool]$container.HostConfig.Privileged)
    $checks['no_host_network'] = (
        ([string]$container.HostConfig.NetworkMode -ne 'host') -and
        ([string]$container.HostConfig.NetworkMode -notlike 'container:*'))
    $checks['command_matches'] = $checksCommand
    $checks['only_expected_mounts'] = ($mounts.Count -eq 2)
    $checks['control_volume_source'] = (
        $controlMounts.Count -eq 1 -and
        [string]$controlMounts[0].Type -eq 'volume' -and
        $controlVolumeName -eq $volumes.Control)
    $checks['key_volume_source'] = (
        $keyMounts.Count -eq 1 -and
        [string]$keyMounts[0].Type -eq 'volume' -and
        $keyVolumeName -eq $volumes.Keys)
    $checks['control_volume_writable'] = (
        $controlMounts.Count -eq 1 -and [bool]$controlMounts[0].RW)
    $checks['key_volume_readonly'] = (
        $keyMounts.Count -eq 1 -and -not [bool]$keyMounts[0].RW)
    $checks['no_extra_mounts'] = ($mounts.Count -eq 2)
    $checks['only_published_port'] = (
        $portProperties.Count -eq 1 -and
        $portProperties[0].Name -eq '7861/tcp')
    $checks['loopback_port'] = (
        $portBindings.Count -eq 1 -and
        [string]$portBindings[0].HostIp -eq '127.0.0.1' -and
        [string]$portBindings[0].HostPort -eq [string]$Port)
    $checks['health_ok'] = ($healthStatus -eq 'healthy')

    $failed = @()
    foreach ($name in $checks.Keys) {
        if ($checks[$name]) {
            Write-Output ("[ok] {0}" -f $name)
        } else {
            Write-Output ("[fail] {0}" -f $name)
            $failed += $name
        }
    }
    if ($failed.Count -gt 0) {
        # 静态身份/命令/挂载/端口契约未通过时，绝不运行 key helper。
        [Console]::Error.WriteLine(
            "错误：diagnostics_failed:$($failed -join ',')")
        exit 1
    }

    # 只有静态契约全部通过才读取 key；目标固定为已验证的预期 key 卷，
    # 不使用运行容器的实际 Name 现场生成操作，并显式声明只读挂载。
    $checks['master_key_present'] = $false
    $keyCheck = Invoke-EaselDocker -Arguments @(
        'run', '--rm', '--user', '10001:10001',
        '--entrypoint', 'python',
        '--mount', "type=volume,src=$($volumes.Keys),dst=/keys,readonly",
        $Image,
        '/opt/easel/deploy/desktop/keygen.py', '--check',
        '--path', '/keys/master.key')
    $checks['master_key_present'] = ($keyCheck.ExitCode -eq 0)
    if ($checks['master_key_present']) {
        Write-Output '[ok] master_key_present'
    } else {
        Write-Output '[fail] master_key_present'
        [Console]::Error.WriteLine('错误：diagnostics_failed:master_key_present')
        exit 1
    }

    Write-Output ("diagnostics=ok project={0} checks={1}" -f `
        $ProjectName, $checks.Count)
    exit 0
}
catch {
    [Console]::Error.WriteLine("错误：$($_.Exception.Message)")
    exit 1
}
