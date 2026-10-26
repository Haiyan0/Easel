<#
.SYNOPSIS
    校验并启动 Easel Windows Docker Desktop 登录管理面。

.DESCRIPTION
    启动前依次校验固定镜像、专用卷、master key、镜像内前端构建和 Compose
    配置；任一缺失都在 `docker compose up` 之前失败。默认只发布
    127.0.0.1:7861，供本机 ngrok 转发。聊天/worker 未装配时 control 会明确
    返回 503，脚本不会伪造可用。

.PARAMETER PublicOrigin
    ngrok 的 HTTPS Origin，例如 https://xxxx.ngrok-free.app。

.PARAMETER Image
    固定 control 镜像 ID（sha256:...）。

.PARAMETER ProjectName
    专用 Compose 项目名，默认 easel-login-desktop。

.PARAMETER Port
    宿主回环端口，默认 7861。

.PARAMETER TimeoutSeconds
    等待容器健康的最长秒数，默认 120。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PublicOrigin,
    [Parameter(Mandatory = $true)][string]$Image,
    [string]$ProjectName = 'easel-login-desktop',
    [ValidateRange(1, 65535)][int]$Port = 7861,
    [ValidateRange(1, 600)][int]$TimeoutSeconds = 120
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'EaselDesktop.Common.ps1')

try {
    # 参数校验必须先于任何 Docker 调用，避免无效 Origin 创建/启动资源。
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

    $keyCheck = Invoke-EaselDocker -Arguments @(
        'run', '--rm', '--user', '10001:10001',
        '--entrypoint', 'python',
        '--mount', "type=volume,src=$($volumes.Keys),dst=/keys",
        $Image,
        '/opt/easel/deploy/desktop/keygen.py', '--check',
        '--path', '/keys/master.key')
    if ($keyCheck.ExitCode -ne 0) {
        # keygen 用 3 表示缺失、4 表示已有 key 损坏；不依赖 stderr 文本。
        if ($keyCheck.ExitCode -eq 3) {
            throw 'master_key_missing'
        }
        throw 'master_key_invalid'
    }

    # 前端必须在镜像内，不允许退回到宿主旧 dist 或 task8 测试镜像。
    $frontendCheck = Invoke-EaselDocker -Arguments @(
        'run', '--rm', '--entrypoint', 'python', $Image, '-c',
        "import os,sys; sys.exit(0 if os.path.isfile('/opt/easel/web/frontend/dist/index.html') else 1)")
    if ($frontendCheck.ExitCode -ne 0) {
        throw 'frontend_build_missing'
    }

    Set-EaselComposeEnvironment -Image $Image `
        -PublicOrigin $PublicOrigin -ProjectName $ProjectName -Port $Port

    $configured = Invoke-EaselDocker -Arguments @(
        'compose', '-p', $ProjectName, '-f', $compose,
        'config', '--quiet')
    if ($configured.ExitCode -ne 0) {
        throw 'compose_config_invalid'
    }

    $up = Invoke-EaselDocker -Arguments @(
        'compose', '-p', $ProjectName, '-f', $compose, 'up', '-d')
    if ($up.ExitCode -ne 0) {
        throw 'control_start_failed'
    }

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $healthy = $false
    while ((Get-Date) -lt $deadline) {
        $container = Invoke-EaselDocker -Arguments @(
            'compose', '-p', $ProjectName, '-f', $compose,
            'ps', '-q', 'control')
        $containerId = (($container.Output -join '').Trim())
        if ($container.ExitCode -eq 0 -and $containerId) {
            $state = Invoke-EaselDocker -Arguments @(
                'inspect', '--format',
                '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}',
                $containerId)
            $status = (($state.Output -join '').Trim())
            if ($state.ExitCode -eq 0 -and $status -eq 'healthy') {
                $healthy = $true
                break
            }
            if ($status -eq 'unhealthy' -or $status -eq 'exited' -or
                $status -eq 'dead') {
                throw 'control_unhealthy'
            }
        }
        Start-Sleep -Seconds 1
    }
    if (-not $healthy) {
        throw 'control_health_timeout'
    }

    Write-Output ("started project={0} origin={1} port={2} image={3} health=healthy" -f `
        $ProjectName, $PublicOrigin, $Port, $Image)
    exit 0
}
catch {
    [Console]::Error.WriteLine("错误：$($_.Exception.Message)")
    exit 1
}
