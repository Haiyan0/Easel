# Task A1 共用校验。只处理无秘密的镜像/Origin/项目名与 Docker 调用封装。
# 脚本不读取 ngrok 配置文件、访问令牌、master key 内容或邀请明文。

function Assert-EaselDocker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'docker_unavailable'
    }
}

function Resolve-EaselImage {
    param([Parameter(Mandatory)][string]$Image)
    if ($Image -notmatch '^sha256:[0-9a-f]{64}$') {
        throw 'control_image_not_pinned'
    }
    return $Image
}

function Resolve-EaselProject {
    param([Parameter(Mandatory)][string]$ProjectName)
    if ($ProjectName -notmatch '^[a-z0-9][a-z0-9_-]{0,62}$') {
        throw 'invalid_project_name'
    }
    return $ProjectName
}

function Resolve-EaselPort {
    param([Parameter(Mandatory)][int]$Port)
    if ($Port -lt 1 -or $Port -gt 65535) {
        throw 'invalid_port'
    }
    return $Port
}

function Resolve-EaselOrigin {
    param([Parameter(Mandatory)][string]$Origin)
    if ([string]::IsNullOrWhiteSpace($Origin) -or $Origin -ne $Origin.Trim()) {
        throw 'invalid_origin'
    }
    # 只接受 https://host[:port] 或带一个根斜杠的写法；路径/查询/片段
    # 一律在启动前拒绝，不能交给 control 启动后再失败。
    if ($Origin -notmatch '^https://[^/?#]+/?$') {
        throw 'invalid_origin'
    }
    $uri = $null
    if (-not [Uri]::TryCreate($Origin, [UriKind]::Absolute, [ref]$uri)) {
        throw 'invalid_origin'
    }
    if ($uri.Scheme -ne 'https' -or $uri.UserInfo -ne '' -or
        $uri.AbsolutePath -ne '/' -or $uri.Query -ne '' -or
        $uri.Fragment -ne '') {
        throw 'invalid_origin'
    }
    $hostName = $uri.Host.ToLowerInvariant()
    if ([string]::IsNullOrEmpty($hostName) -or
        $hostName -notmatch '^[a-z0-9.-]+$' -or $hostName.Contains('..')) {
        throw 'invalid_origin'
    }
    foreach ($label in $hostName.Split('.')) {
        if ($label -notmatch '^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$') {
            throw 'invalid_origin'
        }
    }
    $normalized = 'https://' + $hostName
    if (-not $uri.IsDefaultPort) {
        $normalized = $normalized + ':' + [string]$uri.Port
    }
    return $normalized
}

function Get-EaselVolumeNames {
    param([Parameter(Mandatory)][string]$ProjectName)
    return [pscustomobject]@{
        Control = "$ProjectName-control"
        Keys = "$ProjectName-keys"
    }
}

function Assert-EaselVolumeOwnership {
    param(
        [AllowNull()]$Labels,
        [Parameter(Mandatory)][string]$ProjectName,
        [Parameter(Mandatory)][string]$Role
    )
    # 同名卷可能是开发者已有资源；没有本项目标签时拒绝 root chown。
    if ($null -eq $Labels) {
        throw 'volume_not_owned'
    }
    if ([string]$Labels.'easel.desktop.project' -ne $ProjectName) {
        throw 'volume_not_owned'
    }
    if ([string]$Labels.'easel.desktop.role' -ne $Role) {
        throw 'volume_not_owned'
    }
}

function Get-EaselComposePath {
    return (Join-Path $PSScriptRoot 'compose.login.yaml')
}

function Invoke-EaselDocker {
    param([Parameter(Mandatory)][string[]]$Arguments)
    # 原生命令的 stderr 在 2>&1 后可能变成 ErrorRecord；临时 Continue，
    # 由本函数把退出码与文本交给调用方判断，避免 `$ErrorActionPreference=Stop`
    # 把预期的 key_missing / No such volume 诊断变成未区分异常。
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = & docker @Arguments 2>&1
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

function Set-EaselComposeEnvironment {
    param(
        [Parameter(Mandatory)][string]$Image,
        [Parameter(Mandatory)][string]$PublicOrigin,
        [Parameter(Mandatory)][string]$ProjectName,
        [Parameter(Mandatory)][int]$Port
    )
    $volumes = Get-EaselVolumeNames -ProjectName $ProjectName
    $env:EASEL_CONTROL_IMAGE = $Image
    $env:EASEL_PUBLIC_ORIGIN = $PublicOrigin
    $env:EASEL_DESKTOP_PROJECT = $ProjectName
    $env:EASEL_CONTROL_VOLUME = $volumes.Control
    $env:EASEL_KEY_VOLUME = $volumes.Keys
    $env:EASEL_CONTROL_PORT = [string]$Port
}

function Test-EaselImagePin {
    param(
        [Parameter(Mandatory)][string]$Image,
        [Parameter(Mandatory)]$Volumes
    )
    $inspect = Invoke-EaselDocker -Arguments @(
        'image', 'inspect', '--format', '{{.Id}}', $Image)
    if ($inspect.ExitCode -ne 0) {
        throw 'control_image_unavailable'
    }
    $observed = (($inspect.Output -join '').Trim())
    if ($observed -ne $Image) {
        throw 'control_image_mismatch'
    }
    foreach ($volume in @($Volumes.Control, $Volumes.Keys)) {
        $result = Invoke-EaselDocker -Arguments @('volume', 'inspect', $volume)
        if ($result.ExitCode -ne 0) {
            throw 'volume_missing'
        }
    }
}
