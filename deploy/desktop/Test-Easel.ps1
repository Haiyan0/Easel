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
    Test-EaselImagePin -Image $Image -Volumes $volumes
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
    $containerId = (($running.Output -join '').Trim())
    if ($running.ExitCode -ne 0 -or -not $containerId) {
        throw 'control_not_running'
    }

    $inspect = Invoke-EaselDocker -Arguments @('inspect', $containerId)
    if ($inspect.ExitCode -ne 0) {
        throw 'control_inspect_failed'
    }
    $parsed = (($inspect.Output -join "`n") | ConvertFrom-Json)
    $container = @($parsed)[0]

    $mounts = @($container.Mounts)
    $destinations = @($mounts | ForEach-Object {
        [string]$_.Destination
    })
    $capDrop = @($container.HostConfig.CapDrop)
    $securityOpt = (@($container.HostConfig.SecurityOpt) -join ',')
    $portProperty = $container.NetworkSettings.Ports.PSObject.Properties['7861/tcp']
    $portBindings = @()
    if ($null -ne $portProperty) {
        $portBindings = @($portProperty.Value)
    }
    $healthStatus = ''
    if ($null -ne $container.State.Health) {
        $healthStatus = [string]$container.State.Health.Status
    } else {
        $healthStatus = [string]$container.State.Status
    }

    $checks = [ordered]@{}
    $checks['image_matches'] = ([string]$container.Image -eq $Image)
    $checks['user_10001'] = ([string]$container.Config.User -eq '10001:10001')
    $checks['read_only_rootfs'] = [bool]$container.HostConfig.ReadonlyRootfs
    $checks['cap_drop_all'] = ($capDrop -contains 'ALL')
    $checks['no_new_privileges'] = ($securityOpt -match 'no-new-privileges')
    $checks['not_privileged'] = (-not [bool]$container.HostConfig.Privileged)
    $checks['no_host_network'] = (
        ([string]$container.HostConfig.NetworkMode -ne 'host') -and
        ([string]$container.HostConfig.NetworkMode -notlike 'container:*'))
    $checks['only_control_and_key_volumes'] = (
        $destinations.Count -eq 2 -and
        $destinations -contains '/srv/easel/control' -and
        $destinations -contains '/keys')
    $checks['no_docker_socket'] = (-not (
        $destinations -contains '/var/run/docker.sock'))
    $checks['loopback_port'] = (
        $portBindings.Count -eq 1 -and
        [string]$portBindings[0].HostIp -eq '127.0.0.1' -and
        [string]$portBindings[0].HostPort -eq [string]$Port)
    $checks['health_ok'] = ($healthStatus -eq 'healthy')

    $keyCheck = Invoke-EaselDocker -Arguments @(
        'run', '--rm', '--user', '10001:10001',
        '--entrypoint', 'python',
        '--mount', "type=volume,src=$($volumes.Keys),dst=/keys",
        $Image,
        '/opt/easel/deploy/desktop/keygen.py', '--check',
        '--path', '/keys/master.key')
    $checks['master_key_present'] = ($keyCheck.ExitCode -eq 0)

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
        [Console]::Error.WriteLine(
            "错误：diagnostics_failed:$($failed -join ',')")
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
