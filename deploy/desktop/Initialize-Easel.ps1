<#
.SYNOPSIS
    初始化 Easel Windows Docker Desktop 登录管理面的专用卷与 master key。

.DESCRIPTION
    只创建/校验本项目的两个外部卷，并调用镜像内的 keygen 一次性创建 32 字节
    master key。重复执行安全：不会覆盖 key、不会删除卷或数据库。root 助手只
    负责卷目录权限，正式 control 仍以 UID/GID 10001 运行。

.PARAMETER Image
    固定 control 镜像 ID（sha256:...）。不要传可变 tag。

.PARAMETER ProjectName
    专用 Compose 项目名，默认 easel-login-desktop。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Image,
    [string]$ProjectName = 'easel-login-desktop'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'EaselDesktop.Common.ps1')

try {
    $Image = Resolve-EaselImage -Image $Image
    $ProjectName = Resolve-EaselProject -ProjectName $ProjectName
    Assert-EaselDocker
    $volumes = Get-EaselVolumeNames -ProjectName $ProjectName

    $imageInspect = Invoke-EaselDocker -Arguments @(
        'image', 'inspect', '--format', '{{.Id}}', $Image)
    if ($imageInspect.ExitCode -ne 0) {
        throw 'control_image_unavailable'
    }
    $observed = (($imageInspect.Output -join '').Trim())
    if ($observed -ne $Image) {
        throw 'control_image_mismatch'
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
            if ($inspect.ExitCode -ne 0) {
                throw "volume_inspect_failed:$($spec.Name)"
            }
        }
        $labelsText = (($inspect.Output -join '').Trim())
        $labels = $null
        if ($labelsText) {
            try {
                $labels = $labelsText | ConvertFrom-Json
            }
            catch {
                throw 'volume_labels_invalid'
            }
        }
        Assert-EaselVolumeOwnership -Labels $labels `
            -ProjectName $ProjectName -Role $spec.Role
    }

    # root 助手仅管理本项目两个专用卷；不挂 Docker socket，不接触宿主目录。
    $chown = Invoke-EaselDocker -Arguments @(
        'run', '--rm', '--user', '0:0', '--entrypoint', 'sh',
        '--mount', "type=volume,src=$($volumes.Control),dst=/data",
        '--mount', "type=volume,src=$($volumes.Keys),dst=/keys",
        $Image,
        '-c', 'chown 10001:10001 /data /keys && chmod 750 /data /keys')
    if ($chown.ExitCode -ne 0) {
        throw 'volume_permission_failed'
    }

    # keygen 使用 O_EXCL；已有 key 时只校验，不覆盖。
    $keyResult = Invoke-EaselDocker -Arguments @(
        'run', '--rm', '--user', '10001:10001',
        '--entrypoint', 'python',
        '--mount', "type=volume,src=$($volumes.Keys),dst=/keys",
        $Image,
        '/opt/easel/deploy/desktop/keygen.py', '--ensure',
        '--path', '/keys/master.key')
    if ($keyResult.ExitCode -ne 0) {
        throw 'master_key_initialization_failed'
    }
    $statusLine = @(
        $keyResult.Output |
        Where-Object { $_ -match '^key=(created|exists)$' } |
        Select-Object -Last 1)
    if ($statusLine.Count -eq 0) {
        throw 'master_key_status_missing'
    }

    Write-Output ("initialized project={0} control_volume={1} key_volume={2} {3}" -f `
        $ProjectName, $volumes.Control, $volumes.Keys, $statusLine[0])
    exit 0
}
catch {
    [Console]::Error.WriteLine("错误：$($_.Exception.Message)")
    exit 1
}
