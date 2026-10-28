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
        Egress = "$ProjectName-egress"
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

function Assert-EaselVolumeContract {
    param(
        [Parameter(Mandatory)][string]$ProjectName,
        [Parameter(Mandatory)]$Volumes,
        [switch]$IncludeEgress
    )
    # Start/Test 不得跳过 Initialize 的归属复核。原始 volume inspect 必须
    # 恰好返回一个对象，且实际 Name/项目/角色标签都匹配；不接受自动接管。
    # A1 登录编排没有 egress 卷；只有 Workspace 编排显式要求第三卷。
    $specs = @(
        [pscustomobject]@{ Name = $Volumes.Control; Role = 'control' },
        [pscustomobject]@{ Name = $Volumes.Keys; Role = 'keys' }
    )
    if ($IncludeEgress) {
        $specs += [pscustomobject]@{
            Name = $Volumes.Egress; Role = 'egress-policy' }
    }
    foreach ($spec in $specs) {
        $inspect = Invoke-EaselDocker -Arguments @(
            'volume', 'inspect', $spec.Name)
        if ($inspect.ExitCode -ne 0) {
            throw 'volume_inspect_failed'
        }
        $text = (($inspect.Output -join "`n").Trim())
        if (-not $text) {
            throw 'volume_inspect_invalid'
        }
        try {
            $parsed = $text | ConvertFrom-Json
        }
        catch {
            throw 'volume_inspect_invalid'
        }
        $items = @($parsed)
        if ($items.Count -ne 1 -or $null -eq $items[0]) {
            throw 'volume_inspect_invalid'
        }
        $item = $items[0]
        if (-not ($item.PSObject.Properties.Name -contains 'Name') -or
            -not ($item.PSObject.Properties.Name -contains 'Labels')) {
            throw 'volume_inspect_invalid'
        }
        if ([string]$item.Name -ne $spec.Name) {
            throw 'volume_not_owned'
        }
        Assert-EaselVolumeOwnership -Labels $item.Labels `
            -ProjectName $ProjectName -Role $spec.Role
    }
}

function Test-EaselImagePin {
    param(
        [Parameter(Mandatory)][string]$Image
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
}

function Get-EaselWorkspaceNetworkName {
    param([Parameter(Mandatory)][string]$ProjectName)
    return "$ProjectName-control"
}

function Assert-EaselNetworkOwnership {
    param(
        [AllowNull()]$Labels,
        [Parameter(Mandatory)][string]$ProjectName
    )
    if ($null -eq $Labels) {
        throw 'network_not_owned'
    }
    if ([string]$Labels.'easel.desktop.project' -ne $ProjectName) {
        throw 'network_not_owned'
    }
    if ([string]$Labels.'easel.desktop.role' -ne 'control-network') {
        throw 'network_not_owned'
    }
}

function Assert-EaselNetworkContract {
    param(
        [Parameter(Mandatory)][string]$ProjectName,
        [Parameter(Mandatory)][string]$NetworkName
    )
    $inspect = Invoke-EaselDocker -Arguments @(
        'network', 'inspect', $NetworkName)
    if ($inspect.ExitCode -ne 0) {
        throw 'network_inspect_failed'
    }
    $text = (($inspect.Output -join "`n").Trim())
    if (-not $text) {
        throw 'network_inspect_invalid'
    }
    try {
        $parsed = $text | ConvertFrom-Json
    }
    catch {
        throw 'network_inspect_invalid'
    }
    $items = @($parsed)
    if ($items.Count -ne 1 -or $null -eq $items[0]) {
        throw 'network_inspect_invalid'
    }
    $item = $items[0]
    if ([string]$item.Name -ne $NetworkName) {
        throw 'network_not_owned'
    }
    Assert-EaselNetworkOwnership -Labels $item.Labels `
        -ProjectName $ProjectName
    if ([string]$item.Driver -ne 'bridge' -or
        [bool]$item.Internal -ne $false -or
        [bool]$item.EnableIPv6 -ne $false) {
        throw 'network_invalid'
    }
}

function ConvertTo-EaselWslPath {
    param([Parameter(Mandatory)][string]$Path)
    if ($Path.Length -lt 3 -or $Path[1] -ne ':' -or
        [int][char]$Path[2] -ne 92 -or
        $Path.Substring(0, 1) -notmatch '^[A-Za-z]$') {
        throw 'path_not_windows_drive'
    }
    $drive = $Path.Substring(0, 1).ToLowerInvariant()
    $rest = $Path.Substring(3).Replace('\', '/')
    return "/mnt/$drive/$rest"
}

function Get-EaselFileSha256 {
    param([Parameter(Mandatory)][string]$Path)
    # 不依赖 Get-FileHash 的模块自动加载；受限/子 PowerShell 也能稳定使用。
    $stream = [IO.File]::Open(
        $Path, [IO.FileMode]::Open, [IO.FileAccess]::Read,
        [IO.FileShare]::ReadWrite)
    try {
        $algorithm = [Security.Cryptography.SHA256]::Create()
        try {
            $hash = $algorithm.ComputeHash($stream)
        }
        finally {
            $algorithm.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
    return ([BitConverter]::ToString($hash) -replace '-', '').ToLowerInvariant()
}

function Get-EaselKeyFingerprints {
    param(
        [Parameter(Mandatory)][string]$Image,
        [Parameter(Mandatory)][string]$KeyVolume
    )
    # keygen 从 key 卷内真实私钥派生公钥；输出的摘要才是可信基准。
    $result = Invoke-EaselDocker -Arguments @(
        'run', '--rm', '--user', '10001:10001', '--entrypoint', 'python',
        '--mount', "type=volume,src=$KeyVolume,dst=/keys,readonly",
        $Image, '/opt/easel/deploy/desktop/keygen.py', '--check',
        '--path', '/keys/master.key',
        '--signing-key', '/keys/snapshot.pem',
        '--worker-public', '/keys/worker-public.key',
        '--gateway-public', '/keys/gateway-public.pem',
        '--fingerprints')
    if ($result.ExitCode -ne 0) {
        throw 'workspace_keys_invalid'
    }
    $values = @{}
    foreach ($line in $result.Output) {
        if ("$line" -match '^(worker_public_sha256|gateway_public_sha256)=([0-9a-f]{64})$') {
            $values[$Matches[1]] = $Matches[2]
        }
    }
    if (-not $values.ContainsKey('worker_public_sha256') -or
        -not $values.ContainsKey('gateway_public_sha256')) {
        throw 'workspace_key_fingerprints_missing'
    }
    return [pscustomobject]@{
        Worker = $values['worker_public_sha256']
        Gateway = $values['gateway_public_sha256']
    }
}

function Get-EaselPolicyDigests {
    param(
        [Parameter(Mandatory)][string]$Image,
        [Parameter(Mandatory)][string]$KeyVolume,
        [Parameter(Mandatory)][string]$EgressVolume
    )
    $result = Invoke-EaselDocker -Arguments @(
        'run', '--rm', '--user', '10001:10001', '--entrypoint', 'sha256sum',
        '--mount', "type=volume,src=$KeyVolume,dst=/keys,readonly",
        '--mount', "type=volume,src=$EgressVolume,dst=/policy,readonly",
        $Image, '/keys/egress-policy.json', '/policy/egress-policy.json')
    if ($result.ExitCode -ne 0) {
        throw 'policy_copy_missing'
    }
    $digests = @{}
    foreach ($line in $result.Output) {
        if ("$line" -match '^([0-9a-f]{64})\s+(/keys/egress-policy\.json|/policy/egress-policy\.json)$') {
            $digests[$Matches[2]] = $Matches[1]
        }
    }
    if (-not $digests.ContainsKey('/keys/egress-policy.json') -or
        -not $digests.ContainsKey('/policy/egress-policy.json')) {
        throw 'policy_copy_missing'
    }
    return [pscustomobject]@{
        Keys = $digests['/keys/egress-policy.json']
        Egress = $digests['/policy/egress-policy.json']
    }
}

function Assert-EaselPublicCopies {
    param(
        [Parameter(Mandatory)]$Fingerprints,
        [Parameter(Mandatory)]$Policy,
        [Parameter(Mandatory)][string]$PublicDir
    )
    $worker = Get-EaselFileSha256 -Path (
        Join-Path $PublicDir 'worker-public.key')
    $gateway = Get-EaselFileSha256 -Path (
        Join-Path $PublicDir 'gateway-public.pem')
    $policyHash = Get-EaselFileSha256 -Path (
        Join-Path $PublicDir 'egress-policy.json')
    if ($worker -ne $Fingerprints.Worker) {
        throw 'public_copy_mismatch:worker'
    }
    if ($gateway -ne $Fingerprints.Gateway) {
        throw 'public_copy_mismatch:gateway'
    }
    if ($Policy.Keys -ne $Policy.Egress -or $policyHash -ne $Policy.Keys) {
        throw 'policy_copy_mismatch'
    }
    return [pscustomobject]@{
        Worker = $Fingerprints.Worker
        Gateway = $Fingerprints.Gateway
        Policy = $Policy.Keys
    }
}

function Get-EaselLifecycleLockName {
    param([Parameter(Mandatory)][string]$ProjectName)
    # 名称只由真实 Compose 项目派生，不包含 DataRoot/相对路径；同一项目
    # 的不同 PowerShell 进程/不同参数共用同一把跨进程锁。
    return "Global\Easel.Desktop.Lifecycle.$ProjectName"
}

function Enter-EaselLifecycleLock {
    param(
        [Parameter(Mandatory)][string]$ProjectName,
        [ValidateRange(1, 600)][int]$TimeoutSeconds = 60
    )
    $name = Get-EaselLifecycleLockName -ProjectName $ProjectName
    try {
        $mutex = [Threading.Mutex]::new($false, $name)
    }
    catch {
        throw 'desktop_lifecycle_lock_unavailable'
    }
    $acquired = $false
    try {
        try {
            $acquired = $mutex.WaitOne(
                [TimeSpan]::FromSeconds($TimeoutSeconds), $false)
        }
        catch [Threading.AbandonedMutexException] {
            # 上一个持锁进程异常退出；所有权已转给本次等待者。
            $acquired = $true
        }
    }
    catch {
        $mutex.Dispose()
        throw
    }
    if (-not $acquired) {
        $mutex.Dispose()
        throw 'desktop_lifecycle_busy'
    }
    return [pscustomobject]@{
        Name = $name
        TimeoutSeconds = $TimeoutSeconds
        Mutex = $mutex
    }
}

function Exit-EaselLifecycleLock {
    param([AllowNull()]$Lock)
    if ($null -eq $Lock -or $null -eq $Lock.Mutex) {
        return
    }
    try {
        $Lock.Mutex.ReleaseMutex()
    }
    catch {
        # 进程异常退出时 OS 会释放；这里只清理句柄，不掩盖原错误。
    }
    finally {
        try { $Lock.Mutex.Dispose() } catch { }
    }
}

function Get-EaselWorkspaceComposePath {
    return (Join-Path $PSScriptRoot 'compose.workspace.yaml')
}
