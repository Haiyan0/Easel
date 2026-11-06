<#
.SYNOPSIS
    受控更新已有 Desktop 拓扑到新版 control 镜像与新版 WSL provisioner。

.DESCRIPTION
    只用于同一已登记项目内的代码升级，不新建项目、不删卷、不改 Key/HOME/
    attempt、不按进程名泛杀。流程：

    1. 核对 deployment.json、运行中 control 镜像、项目/卷/网络/目录身份；
    2. 以旧 control 的 Store API 写入本项目的 update 接纳屏障；
    3. 调用 Start-WorkspaceEasel.ps1（持有同一生命周期锁），由它在
       屏障存在时按精确 socket 替换代码落后的 provisioner，并用新镜像
       重建 control；
    4. 核验运行中 control 镜像/源码摘要、provisioner 源码摘要/协议/动作、
       worker/gateway 实例集合、Key/policy 指纹与 attempt 文件均未变化；
    5. 只有全部核验通过后才显式 resume 屏障。

    任一步失败都保留屏障并退出 1；修复后可重跑本脚本（reason 为
    desktop-update 的屏障会被安全沿用）。不会自动回退、清屏障或改 DB。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PublicOrigin,
    [Parameter(Mandatory = $true)][string]$Image,
    [Parameter(Mandatory = $true)][string]$WorkerImage,
    [Parameter(Mandatory = $true)][string]$GatewayImage,
    [string]$ProjectName = 'easel-workspace-desktop',
    [ValidateRange(1, 65535)][int]$Port = 7861,
    [string]$Distro = 'Ubuntu-26.04',
    [string]$WslRepo = '/mnt/c/Code/Easel',
    [string]$WorkspaceRoot = 'C:\Easel\workspaces',
    [string]$DataRoot = 'C:\Easel\desktop',
    [string]$DatabaseQuerySecrets = '',
    [int]$ProvisionerGid = 1000,
    [ValidateRange(1, 600)][int]$TimeoutSeconds = 180,
    [ValidateRange(1, 600)][int]$LockTimeoutSeconds = 60
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'EaselDesktop.Common.ps1')

function Invoke-Wsl {
    param([Parameter(Mandatory)][string[]]$Arguments)
    $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if (-not $wsl) {
        throw 'wsl_unavailable'
    }
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

function Get-WslFileSha256 {
    param(
        [Parameter(Mandatory)][string]$Distro,
        [Parameter(Mandatory)][string]$Path
    )
    $result = Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'sha256sum', $Path)
    $digest = (($result.Output -join '') -split '\s+')[0]
    if ($result.ExitCode -ne 0 -or
        $digest -notmatch '^[0-9a-f]{64}$') {
        throw 'source_hash_unavailable'
    }
    return $digest
}

function Invoke-ProvisionerIdentity {
    param(
        [Parameter(Mandatory)][string]$Image,
        [Parameter(Mandatory)][string]$SocketPath,
        [Parameter(Mandatory)][string]$Distro,
        [Parameter(Mandatory)][int]$ProvisionerGid,
        [Parameter(Mandatory)][ValidateSet('show', 'check')][string]$Mode,
        [string]$ExpectedFile = ''
    )
    $arguments = @(
        '-d', $Distro, '--', 'docker', 'run', '--rm',
        '--network', 'none', '--read-only',
        '--user', '10001:10001', '--group-add', [string]$ProvisionerGid,
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
        '--entrypoint', 'python',
        '--mount', "type=bind,src=$SocketPath,dst=/run/provisioner.sock,readonly")
    if ($Mode -eq 'check') {
        $wslExpected = ConvertTo-EaselWslPath -Path $ExpectedFile
        $arguments += @(
            '--mount',
            "type=bind,src=$wslExpected,dst=/expected.json,readonly")
    }
    $arguments += @(
        $Image,
        '/opt/easel/deploy/desktop/provisioner_identity.py',
        $Mode, '--socket', '/run/provisioner.sock')
    if ($Mode -eq 'check') {
        $arguments += @('--expected', '/expected.json')
    }
    return Invoke-Wsl -Arguments $arguments
}

function Get-ProvisionerPid {
    param(
        [Parameter(Mandatory)][string]$Distro,
        [Parameter(Mandatory)][string]$SocketPath,
        [Parameter(Mandatory)][string]$ScriptDir
    )
    $script = @'
set -e
socket="$1"
pattern="deploy/multiuser/provisioner.py --socket $socket"
pids=$(pgrep -f -- "$pattern" || true)
count=$(printf '%s\n' "$pids" | sed '/^$/d' | wc -l)
if [ "$count" -ne 1 ]; then
    echo "provisioner_pid_count=$count"
    exit 4
fi
pid=$pids
tr '\0' '\n' < "/proc/$pid/cmdline" | grep -Fx -- "$socket" >/dev/null \
    || { echo "provisioner_cmdline_mismatch:$pid"; exit 5; }
echo "provisioner_pid=$pid"
'@
    # 通过脚本文件执行，避免 PowerShell 5.1 对内联双引号的转义差异。
    $null = New-Item -ItemType Directory -Path $ScriptDir -Force
    $scriptFile = Join-Path $ScriptDir 'provisioner-pid.sh'
    $normalized = ($script -replace "`r`n", "`n")
    [IO.File]::WriteAllText(
        $scriptFile, ($normalized + "`n"),
        (New-Object System.Text.UTF8Encoding($false)))
    $wslScript = ConvertTo-EaselWslPath -Path $scriptFile
    return Invoke-Wsl -Arguments @(
        '-d', $Distro, '--', 'bash', $wslScript, $SocketPath)
}

function Get-ManagedSnapshot {
    param(
        [Parameter(Mandatory)][string]$Root,
        [string[]]$WorkspaceIds = @()
    )
    # 只快照本项目登记/可信 workspace 的受管实例。其他部署正常增删
    # worker/gateway 不应阻断本项目更新；本项目实例被替换仍必须拒绝。
    $ids = @($WorkspaceIds | ForEach-Object { "$_".Trim().ToLowerInvariant() } |
        Where-Object { $_ -match '^[0-9a-f]{32}$' } | Sort-Object -Unique)
    if ($ids.Count -eq 0 -and
        (Test-Path -LiteralPath $Root -PathType Container)) {
        # 控制库不可读时的受限后备：仅枚举本项目持久根下的 workspace 目录。
        $ids = @(Get-ChildItem -LiteralPath $Root -Directory |
            Where-Object { $_.Name -match '^[0-9a-f]{32}$' } |
            ForEach-Object { $_.Name.ToLowerInvariant() } | Sort-Object -Unique)
    }
    $containerIds = @()
    foreach ($workspaceId in $ids) {
        $lookup = Invoke-EaselDocker -Arguments @(
            'ps', '-a', '-q',
            '--filter', 'label=easel.managed=true',
            '--filter', "label=easel.workspace-id=$workspaceId")
        if ($lookup.ExitCode -ne 0) {
            throw ("managed_snapshot_unavailable:ps:" +
                ($lookup.Output -join '|'))
        }
        $containerIds += @($lookup.Output | ForEach-Object { "$_".Trim() } |
            Where-Object { $_ })
    }
    $containerIds = @($containerIds | Sort-Object -Unique)
    if ($containerIds.Count -eq 0) {
        return @()
    }
    # 不在 --format 内使用双引号标签索引：PowerShell 传给原生 docker 时
    # 内层引号会被剥离，导致模板解析失败。只比较不可复用实例 ID 与运行态，
    # 已足以证明更新没有替换 worker/gateway。
    $inspectArguments = @('inspect', '--format', '{{.Id}}|{{.State.Running}}')
    $inspectArguments += $containerIds
    $inspect = Invoke-EaselDocker -Arguments $inspectArguments
    if ($inspect.ExitCode -ne 0) {
        throw ("managed_snapshot_unavailable:inspect:" +
            ($inspect.Output -join '|') + ':args:' +
            ($inspectArguments -join ',') + ':argcount:' +
            $inspectArguments.Count + ':arg0:' + $inspectArguments[0] +
            ':ids:' + ($containerIds -join ','))
    }
    return @($inspect.Output | Where-Object { "$_".Trim() } |
        ForEach-Object { "$_".Trim() } | Sort-Object)
}

function Get-AttemptSnapshot {
    param([Parameter(Mandatory)][string]$Root)
    $directory = Join-Path $Root '.gateway'
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        return @()
    }
    $files = Get-ChildItem -LiteralPath $directory -Filter 'attempt-*.json' `
        -File | Sort-Object Name
    $result = @()
    foreach ($file in $files) {
        $digest = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
        $result += ($file.Name.ToLowerInvariant() + ':' + $digest.ToLowerInvariant())
    }
    return $result
}

function Get-EaselControlContainerId {
    param([Parameter(Mandatory)][string]$ProjectName)
    $lookup = Invoke-EaselDocker -Arguments @(
        'ps', '-q',
        '--filter', "label=com.docker.compose.project=$ProjectName",
        '--filter', 'label=com.docker.compose.service=control')
    if ($lookup.ExitCode -ne 0) {
        return ''
    }
    return (($lookup.Output -join '').Trim())
}

function Get-BarrierRecord {
    param(
        [Parameter(Mandatory)][string]$ControlId,
        [Parameter(Mandatory)][string]$BarrierId
    )
    $code = @'
import json, sys
from easel.control.store import Store
with Store.open('/srv/easel/control/control.sqlite3') as store:
    existing = store.admission_barrier()
    if existing is not None:
        print(json.dumps({'existing': existing}, sort_keys=True))
    else:
        created = store.set_admission_barrier(
            sys.argv[1], 'desktop-update', int(sys.argv[2]))
        print(json.dumps({'created': created}, sort_keys=True))
'@
    $stamp = [string][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    return Invoke-EaselDocker -Arguments @(
        'exec', $ControlId, 'python', '-c', $code, $BarrierId, $stamp)
}

function Get-BarrierStatus {
    param([Parameter(Mandatory)][string]$ControlId)
    $result = Invoke-EaselDocker -Arguments @(
        'exec', $ControlId, 'python', '-m', 'easel', 'admin',
        'barrier-status', '--control-dir', '/srv/easel/control')
    return [pscustomobject]@{
        ExitCode = $result.ExitCode
        Text = (($result.Output -join '').Trim())
    }
}

function Get-OfflineControlFacts {
    param(
        [Parameter(Mandatory)][string]$Image,
        [Parameter(Mandatory)][string]$ControlVolume
    )
    # 只读数据库事实：不打开 Store（Store.open 可能迁移/写 WAL），也不
    # 依赖正在运行的控制进程。旧镜像/新镜像均可由目标镜像执行该脚本。
    $code = @'
import json, sqlite3, sys
path = '/srv/easel/control/control.sqlite3'
now = int(sys.argv[1])
try:
    conn = sqlite3.connect('file:' + path + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT value FROM control_state WHERE key='admission_barrier'"
    ).fetchone()
    barrier = json.loads(row['value']) if row is not None else None
    active_runs = conn.execute(
        "SELECT COUNT(*) FROM runs WHERE status NOT IN"
        " ('done','failed','interrupted','cancelled')").fetchone()[0]
    active_requests = conn.execute(
        "SELECT COUNT(*) FROM request_leases WHERE expires_at > ?",
        (now,)).fetchone()[0]
    leases = conn.execute(
        "SELECT COUNT(*) FROM worker_leases WHERE state IN"
        " ('starting','ready','draining','stopping')").fetchone()[0]
    workspaces = [r[0] for r in conn.execute(
        "SELECT id FROM workspaces ORDER BY id")]
    print(json.dumps({
        'barrier': barrier,
        'activeRuns': int(active_runs),
        'activeRequests': int(active_requests),
        'activeLeaseCount': int(leases),
        'workspaceIds': workspaces,
    }, sort_keys=True, separators=(',', ':')))
except Exception as error:
    print('update_control_facts_offline_failed:' + type(error).__name__,
          file=sys.stderr)
    sys.exit(1)
'@
    $stamp = [string][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    return Invoke-EaselDocker -Arguments @(
        'run', '--rm', '--network', 'none', '--read-only',
        '--entrypoint', 'python',
        '--mount',
        "type=volume,src=$ControlVolume,dst=/srv/easel/control,readonly",
        $Image, '-c', $code, $stamp)
}

function Get-ControlFacts {
    param(
        [string]$ControlId,
        [Parameter(Mandatory)][string]$Image,
        [Parameter(Mandatory)][string]$ControlVolume
    )
    # 优先在运行中的控制容器内用其自带 Store 读取事实；旧镜像没有
    # update-status 命令也能执行这段受限内联脚本。随后尝试新 CLI，
    # 控制不可用时退回只读 SQLite。三者输出同一事实集合。
    if ($ControlId) {
        $code = @'
import json, time
from easel.control.store import Store
with Store.open('/srv/easel/control/control.sqlite3') as store:
    status = store.drain_status(int(time.time()))
    print(json.dumps({
        'barrier': store.admission_barrier(),
        'activeRuns': status['activeRuns'],
        'activeRequests': status['activeRequests'],
        'activeLeaseCount': len(status['activeLeases']),
        'workspaceIds': store.list_workspace_ids(),
    }, sort_keys=True, separators=(',', ':')))
'@
        $inline = Invoke-EaselDocker -Arguments @(
            'exec', $ControlId, 'python', '-c', $code)
        $payload = $null
        if ($inline.ExitCode -eq 0) {
            foreach ($line in $inline.Output) {
                if ("$line".Trim().StartsWith('{')) {
                    try {
                        $payload = "$line".Trim() | ConvertFrom-Json
                    }
                    catch {
                        $payload = $null
                    }
                }
            }
            if ($null -ne $payload) {
                return [pscustomobject]@{
                    ExitCode = 0
                    Source = 'control-store'
                    Payload = $payload
                    Output = ''
                }
            }
        }
        $status = Invoke-EaselDocker -Arguments @(
            'exec', $ControlId, 'python', '-m', 'easel', 'admin',
            'update-status', '--control-dir', '/srv/easel/control')
        if ($status.ExitCode -eq 0) {
            $text = (($status.Output -join '').Trim())
            try {
                $payload = $text | ConvertFrom-Json
            }
            catch {
                $payload = $null
            }
            if ($null -ne $payload) {
                return [pscustomobject]@{
                    ExitCode = 0
                    Source = 'control-cli'
                    Payload = $payload
                    Output = ''
                }
            }
        }
    }
    $offline = Get-OfflineControlFacts -Image $Image `
        -ControlVolume $ControlVolume
    $payload = $null
    if ($offline.ExitCode -eq 0) {
        foreach ($line in $offline.Output) {
            if ("$line".Trim().StartsWith('{')) {
                try {
                    $payload = "$line".Trim() | ConvertFrom-Json
                }
                catch {
                    $payload = $null
                }
            }
        }
    }
    return [pscustomobject]@{
        ExitCode = $(if ($null -ne $payload) { 0 } else { 1 })
        Source = 'offline'
        Payload = $payload
        Output = ($offline.Output -join '|')
    }
}

function Wait-UpdateWorkload {
    param(
        [string]$ControlId,
        [Parameter(Mandatory)][string]$Image,
        [Parameter(Mandatory)][string]$ControlVolume,
        [Parameter(Mandatory)][int]$TimeoutSeconds
    )
    # 只等待已接纳的 Run 与在途请求；静态坏 worker lease 由 Start 的
    # 恢复流程处理，不能在这里 drain，否则现场会死锁。控制容器不在时
    # 仍用只读 SQLite 事实等待；offline 事实也不可读则失败关闭，绝不
    # 以“不可观察”为由在存在活动工作时替换服务。
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ($true) {
        $facts = Get-ControlFacts -ControlId $ControlId -Image $Image `
            -ControlVolume $ControlVolume
        if ($facts.ExitCode -ne 0 -or $null -eq $facts.Payload) {
            throw ("update_work_status_unavailable:" + $facts.Output)
        }
        $runs = [int]$facts.Payload.activeRuns
        $requests = [int]$facts.Payload.activeRequests
        Write-Output ("update_active_work={0}/{1}" -f $runs, $requests)
        if ($runs -eq 0 -and $requests -eq 0) {
            return
        }
        if ((Get-Date) -ge $deadline) {
            throw ("update_active_work_timeout:{0}/{1}" -f $runs, $requests)
        }
        Start-Sleep -Seconds 2
    }
}

$lifecycleLock = $null
$barrierId = ''
try {
    $PublicOrigin = Resolve-EaselOrigin -Origin $PublicOrigin
    $Image = Resolve-EaselImage -Image $Image
    $WorkerImage = Resolve-EaselImage -Image $WorkerImage
    $GatewayImage = Resolve-EaselImage -Image $GatewayImage
    $ProjectName = Resolve-EaselProject -ProjectName $ProjectName
    $Port = Resolve-EaselPort -Port $Port
    $WorkspaceRoot = [IO.Path]::GetFullPath($WorkspaceRoot)
    $DataRoot = [IO.Path]::GetFullPath($DataRoot)
    $logDir = Join-Path $DataRoot 'logs'
    $deploymentFile = Join-Path $logDir 'deployment.json'
    $expectedFile = Join-Path $logDir 'provisioner-expected.json'
    if (-not (Test-Path -LiteralPath $deploymentFile -PathType Leaf)) {
        throw 'update_deployment_missing'
    }
    if (-not (Test-Path -LiteralPath $expectedFile -PathType Leaf)) {
        throw 'update_identity_config_missing'
    }
    Assert-EaselDocker
    $lifecycleLock = Enter-EaselLifecycleLock `
        -ProjectName $ProjectName -TimeoutSeconds $LockTimeoutSeconds
    $volumes = Get-EaselVolumeNames -ProjectName $ProjectName
    $network = Get-EaselWorkspaceNetworkName -ProjectName $ProjectName
    Assert-EaselVolumeContract -ProjectName $ProjectName -Volumes $volumes `
        -IncludeEgress
    Assert-EaselNetworkContract -ProjectName $ProjectName `
        -NetworkName $network
    Test-EaselImagePin -Image $Image

    try {
        $deployment = Get-Content -LiteralPath $deploymentFile -Raw |
            ConvertFrom-Json
    }
    catch {
        throw 'update_deployment_invalid'
    }
    if ([string]$deployment.project -ne $ProjectName) {
        throw 'update_project_mismatch'
    }
    if ([IO.Path]::GetFullPath([string]$deployment.workspace_root) -ne
        $WorkspaceRoot) {
        throw 'update_workspace_root_mismatch'
    }
    if ([IO.Path]::GetFullPath([string]$deployment.data_root) -ne $DataRoot) {
        throw 'update_data_root_mismatch'
    }
    if ((Resolve-EaselOrigin -Origin ([string]$deployment.origin)) -ne
        $PublicOrigin) {
        throw 'update_origin_mismatch'
    }
    if ([int]$deployment.port -ne $Port) {
        throw 'update_port_mismatch'
    }
    if ([string]$deployment.worker_image -ne $WorkerImage -or
        [string]$deployment.gateway_image -ne $GatewayImage) {
        throw 'update_worker_gateway_image_changed'
    }
    $oldControlImage = [string]$deployment.control_image
    if ($oldControlImage -notmatch '^sha256:[0-9a-f]{64}$') {
        throw 'update_deployment_invalid'
    }

    $controlId = Get-EaselControlContainerId -ProjectName $ProjectName
    $controlImageNow = ''
    if ($controlId) {
        $imageResult = Invoke-EaselDocker -Arguments @(
            'inspect', '--format', '{{.Image}}', $controlId)
        if ($imageResult.ExitCode -eq 0) {
            $controlImageNow = (($imageResult.Output -join '').Trim())
        }
    }

    # 先读控制库事实再决定阶段：desktop-update 屏障存在说明这是同一
    # 更新命令的未完成阶段，允许 control 已换新镜像/暂不可用或旧
    # provisioner 已停止；不带该屏障则必须是完整的旧版本现场。
    $facts = Get-ControlFacts -ControlId $controlId -Image $Image `
        -ControlVolume $volumes.Control
    if ($facts.ExitCode -ne 0 -or $null -eq $facts.Payload) {
        throw ("update_control_facts_unavailable:" + $facts.Output)
    }
    $factsPayload = $facts.Payload
    $barrierRecord = $factsPayload.barrier
    $resuming = $false
    $barrierId = ''
    if ($null -ne $barrierRecord) {
        if ([string]$barrierRecord.reason -ne 'desktop-update') {
            throw ("update_barrier_preexisting:" +
                [string]$barrierRecord.id + ':' +
                [string]$barrierRecord.reason)
        }
        $existingBarrierId = [string]$barrierRecord.id
        if ($existingBarrierId -notmatch '^update-[0-9a-f]{12}$') {
            # 只沿用本脚本格式的更新屏障；其他/损坏 id 必须人工处理。
            throw ("update_barrier_preexisting_invalid:" +
                $existingBarrierId)
        }
        $resuming = $true
        $barrierId = $existingBarrierId
        Write-Output ("update_resumed_phase={0}" -f $barrierId)
    }

    $oldProvisionerSha = ''
    $oldProvisionerPid = $null
    if ($resuming) {
        # 未完成阶段只接受本项目本次屏障保护下的两种控制身份：旧镜像
        # 尚未替换，或目标镜像已替换但部署记录尚未更新。其他镜像拒绝。
        if ($controlImageNow -and
            $controlImageNow -notin @($oldControlImage, $Image)) {
            throw ("update_control_image_unexpected:" +
                $controlImageNow)
        }
        Write-Output ("update_resume_control_image={0}" -f $(
            if ($controlImageNow) { $controlImageNow } else { 'unavailable' }))
    }
    else {
        if (-not $controlId -or -not $controlImageNow) {
            throw 'update_control_unavailable'
        }
        if ($controlImageNow -ne $oldControlImage) {
            throw ("update_control_image_mismatch:" +
                "$controlImageNow/$oldControlImage")
        }
        $oldIdentity = Invoke-ProvisionerIdentity -Image $Image `
            -SocketPath "/tmp/easel-runtime/$ProjectName/provisioner.sock" `
            -Distro $Distro -ProvisionerGid $ProvisionerGid -Mode show
        if ($oldIdentity.ExitCode -eq 0) {
            try {
                $oldIdentityPayload = (($oldIdentity.Output -join '') |
                    ConvertFrom-Json)
                $oldProvisionerSha = [string]$oldIdentityPayload.sourceSha256
            }
            catch {
                throw 'update_old_identity_invalid'
            }
        }
        else {
            throw 'update_old_identity_unavailable'
        }
        $oldProvisionerPid = Get-ProvisionerPid -Distro $Distro `
            -SocketPath "/tmp/easel-runtime/$ProjectName/provisioner.sock" `
            -ScriptDir $logDir
        if ($oldProvisionerPid.ExitCode -ne 0) {
            throw 'update_old_provisioner_pid_unavailable'
        }
    }
    $newRuntimeSha = Get-WslFileSha256 -Distro $Distro `
        -Path "$WslRepo/easel/control/runtime.py"

    # 只快照本项目登记 workspace；另一部署的正常增删不影响本轮。
    $workspaceIds = @($factsPayload.workspaceIds |
        ForEach-Object { "$_".Trim().ToLowerInvariant() } |
        Where-Object { $_ -match '^[0-9a-f]{32}$' } | Sort-Object -Unique)
    if ($workspaceIds.Count -eq 0 -and
        (Test-Path -LiteralPath $WorkspaceRoot -PathType Container)) {
        $workspaceIds = @(Get-ChildItem -LiteralPath $WorkspaceRoot `
            -Directory | Where-Object { $_.Name -match '^[0-9a-f]{32}$' } |
            ForEach-Object { $_.Name.ToLowerInvariant() } |
            Sort-Object -Unique)
    }

    if (-not $resuming) {
        # 屏障必须在本轮写入；已有 desktop-stop 等屏障时失败关闭，绝不
        # 借用或清除其他生命周期的承诺。desktop-update 屏障可被重试沿用。
        $barrierSuffix = [guid]::NewGuid().ToString('N').Substring(0, 12)
        $barrierId = "update-$barrierSuffix"
        $barrierResult = Get-BarrierRecord -ControlId $controlId `
            -BarrierId $barrierId
        if ($barrierResult.ExitCode -ne 0) {
            throw ("update_barrier_create_failed:" +
                ($barrierResult.Output -join '|'))
        }
        $barrierPayload = $null
        foreach ($line in $barrierResult.Output) {
            if ("$line".Trim().StartsWith('{')) {
                $barrierPayload = "$line".Trim() | ConvertFrom-Json
            }
        }
        if ($null -eq $barrierPayload) {
            throw 'update_barrier_response_invalid'
        }
        if ($null -ne $barrierPayload.existing) {
            if ([string]$barrierPayload.existing.reason -ne
                'desktop-update') {
                throw ("update_barrier_preexisting:" +
                    [string]$barrierPayload.existing.id + ':' +
                    [string]$barrierPayload.existing.reason)
            }
            $existingBarrierId = [string]$barrierPayload.existing.id
            if ($existingBarrierId -notmatch '^update-[0-9a-f]{12}$') {
                throw ("update_barrier_preexisting_invalid:" +
                    $existingBarrierId)
            }
            $barrierId = $existingBarrierId
        }
        else {
            $barrierId = [string]$barrierPayload.created.id
        }
        if (-not $barrierId) {
            throw 'update_barrier_response_invalid'
        }
        $barrierCheck = Get-BarrierStatus -ControlId $controlId
        if ($barrierCheck.ExitCode -ne 0 -or
            $barrierCheck.Text -ne "barrier=$barrierId") {
            throw "update_barrier_not_established:$($barrierCheck.Text)"
        }
    }
    elseif ($controlId) {
        # 恢复阶段再核对一次屏障没有在两次读取之间被清除。
        $barrierCheck = Get-BarrierStatus -ControlId $controlId
        if ($barrierCheck.ExitCode -eq 0 -and
            $barrierCheck.Text -ne "barrier=$barrierId") {
            throw "update_barrier_lost:$($barrierCheck.Text)"
        }
    }

    # 首次基线必须在屏障建立后、替换前落盘；重跑沿用同一基线，不能
    # 从已经漂移的实例重新打基线来掩盖本应阻断的替换。
    $baselineFile = Join-Path $logDir "update-baseline-$barrierId.json"
    $baselineSource = 'fresh'
    if (Test-Path -LiteralPath $baselineFile -PathType Leaf) {
        try {
            $baselineRaw = Get-Content -LiteralPath $baselineFile -Raw |
                ConvertFrom-Json
        }
        catch {
            throw "update_baseline_invalid:$barrierId"
        }
        $managedBefore = @($baselineRaw | ForEach-Object { "$_" } |
            Where-Object { $_ })
        $baselineSource = 'existing'
    }
    elseif ($resuming) {
        throw "update_baseline_missing:$barrierId"
    }
    else {
        $managedBefore = @(Get-ManagedSnapshot -Root $WorkspaceRoot `
            -WorkspaceIds $workspaceIds)
        $baselineJson = ConvertTo-Json -InputObject @($managedBefore) -Compress
        [IO.File]::WriteAllText(
            $baselineFile, $baselineJson,
            (New-Object System.Text.UTF8Encoding($false)))
    }
    $attemptBefore = @(Get-AttemptSnapshot -Root $WorkspaceRoot)
    $keyBefore = (Get-EaselKeyFingerprints -Image $Image `
        -KeyVolume $volumes.Keys | ConvertTo-Json -Compress)
    $policyBefore = (Get-EaselPolicyDigests -Image $Image `
        -KeyVolume $volumes.Keys -EgressVolume $volumes.Egress |
        ConvertTo-Json -Compress)
    Write-Output ("update_managed_baseline={0}" -f $baselineSource)

    # 活动工作边界：只等待已接纳 Run 与在途请求；静态坏 lease 交给
    # Start 的恢复路径，避免在现场坏状态上 drain 死锁。
    Wait-UpdateWorkload -ControlId $controlId -Image $Image `
        -ControlVolume $volumes.Control -TimeoutSeconds $TimeoutSeconds
    Write-Output ("update_barrier={0}" -f $barrierId)

    $startScript = Join-Path $PSScriptRoot 'Start-WorkspaceEasel.ps1'
    $startArguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $startScript,
        '-PublicOrigin', $PublicOrigin,
        '-Image', $Image,
        '-WorkerImage', $WorkerImage,
        '-GatewayImage', $GatewayImage,
        '-ProjectName', $ProjectName,
        '-Port', [string]$Port,
        '-Distro', $Distro,
        '-WslRepo', $WslRepo,
        '-WorkspaceRoot', $WorkspaceRoot,
        '-DataRoot', $DataRoot,
        '-ProvisionerGid', [string]$ProvisionerGid,
        '-TimeoutSeconds', [string]$TimeoutSeconds,
        '-UpdateBarrierId', $barrierId,
        '-RestartProvisionerIfStale',
        '-LifecycleLockHeld')
    if ($DatabaseQuerySecrets) {
        # 更新沿用同一 secrets 目录；省略即与旧部署一样保持禁用。
        $startArguments += @('-DatabaseQuerySecrets', $DatabaseQuerySecrets)
    }
    $registeredMode = [string]$deployment.publish_mode
    if ($registeredMode) {
        $startArguments += @('-PublishMode', $registeredMode)
    }
    # 用文件承接 Start 的输出：Start 拉起的 provisioner 是长驻孙进程，
    # PowerShell 管道会被它继承并让父进程一直等 EOF。
    $startStdout = Join-Path $logDir 'update-start.stdout.log'
    $startStderr = Join-Path $logDir 'update-start.stderr.log'
    $quotedArguments = @(
        $startArguments | ForEach-Object {
            if ("$_" -match '[\s"]') {
                '"' + ("$_" -replace '"', '\"') + '"'
            }
            else { "$_" }
        }) -join ' '
    $startProcess = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList $quotedArguments -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $startStdout `
        -RedirectStandardError $startStderr
    if ($null -eq $startProcess) {
        throw 'update_start_spawn_failed'
    }
    # 不用 Start-Process -Wait：Start 拉起的 provisioner 是长驻子进程，
    # -Wait 会把它算进等待树。只等待 Start 自身退出。
    $startTimeoutMs = ([int]$TimeoutSeconds + 300) * 1000
    if (-not $startProcess.WaitForExit($startTimeoutMs)) {
        throw 'update_start_timeout'
    }
    $startCode = [int]$startProcess.ExitCode
    # 不读取 Start 的 stdout/stderr 文件：长驻 provisioner 孙进程会继承
    # 这些句柄，Get-Content 可能一直等锁。日志保留在 logs 目录供核验。
    Write-Output ("update_start_exit={0}" -f $startCode)
    if ($startCode -ne 0) {
        throw ("update_start_failed:{0}:see:{1}" -f $startCode, $startStdout)
    }

    $controlId = Get-EaselControlContainerId -ProjectName $ProjectName
    if (-not $controlId) {
        throw 'update_control_unavailable_after_start'
    }
    $imageAfter = Invoke-EaselDocker -Arguments @(
        'inspect', '--format', '{{.Image}}', $controlId)
    $imageAfter = (($imageAfter.Output -join '').Trim())
    if ($imageAfter -ne $Image) {
        throw "update_control_image_not_applied:$imageAfter/$Image"
    }
    $runtimeResult = Invoke-EaselDocker -Arguments @(
        'exec', $controlId, 'python', '-c',
        ("import hashlib;print(hashlib.sha256(open(" +
         "'/opt/easel/easel/control/runtime.py','rb').read()).hexdigest())"))
    $runtimeInImage = (($runtimeResult.Output -join '').Trim())
    if ($runtimeResult.ExitCode -ne 0 -or
        $runtimeInImage -notmatch '^[0-9a-f]{64}$' -or
        $runtimeInImage -ne $newRuntimeSha) {
        throw "update_control_runtime_mismatch:$runtimeInImage/$newRuntimeSha"
    }

    $identityAfter = Invoke-ProvisionerIdentity -Image $Image `
        -SocketPath "/tmp/easel-runtime/$ProjectName/provisioner.sock" `
        -Distro $Distro -ProvisionerGid $ProvisionerGid -Mode show
    if ($identityAfter.ExitCode -ne 0) {
        throw 'update_provisioner_identity_unavailable'
    }
    try {
        $identityPayload = (($identityAfter.Output -join '') |
            ConvertFrom-Json)
    }
    catch {
        throw 'update_provisioner_identity_invalid'
    }
    $newProvisionerSha = [string]$identityPayload.sourceSha256
    $requiredActions = @(
        'ensure', 'stop', 'inspect', 'activate', 'abort', 'restore_gateway',
        'commit_attempt', 'recover_abort', 'identity')
    $observedActions = @($identityPayload.actions | ForEach-Object { "$_" })
    foreach ($action in $requiredActions) {
        if ($action -notin $observedActions) {
            throw "update_provisioner_action_missing:$action"
        }
    }
    if ([int]$identityPayload.protocol -ne 2 -or
        $newProvisionerSha -ne (Get-WslFileSha256 -Distro $Distro `
            -Path "$WslRepo/deploy/multiuser/provisioner.py")) {
        throw 'update_provisioner_source_mismatch'
    }
    $identityCheck = Invoke-ProvisionerIdentity -Image $Image `
        -SocketPath "/tmp/easel-runtime/$ProjectName/provisioner.sock" `
        -Distro $Distro -ProvisionerGid $ProvisionerGid -Mode check `
        -ExpectedFile $expectedFile
    if ($identityCheck.ExitCode -ne 0) {
        throw 'update_provisioner_identity_check_failed'
    }
    $newProvisionerPid = Get-ProvisionerPid -Distro $Distro `
        -SocketPath "/tmp/easel-runtime/$ProjectName/provisioner.sock" `
        -ScriptDir $logDir
    if ($newProvisionerPid.ExitCode -ne 0) {
        throw 'update_new_provisioner_pid_unavailable'
    }

    $managedAfter = @(Get-ManagedSnapshot -Root $WorkspaceRoot `
        -WorkspaceIds $workspaceIds)
    if (($managedBefore -join "`n") -ne ($managedAfter -join "`n")) {
        throw 'update_managed_instances_changed'
    }
    $attemptAfter = @(Get-AttemptSnapshot -Root $WorkspaceRoot)
    if (($attemptBefore -join "`n") -ne ($attemptAfter -join "`n")) {
        throw 'update_attempt_records_changed'
    }
    $keyAfter = (Get-EaselKeyFingerprints -Image $Image `
        -KeyVolume $volumes.Keys | ConvertTo-Json -Compress)
    $policyAfter = (Get-EaselPolicyDigests -Image $Image `
        -KeyVolume $volumes.Keys -EgressVolume $volumes.Egress |
        ConvertTo-Json -Compress)
    if ($keyBefore -ne $keyAfter -or $policyBefore -ne $policyAfter) {
        throw 'update_key_material_changed'
    }
    $barrierCheck = Get-BarrierStatus -ControlId $controlId
    if ($barrierCheck.ExitCode -ne 0 -or
        $barrierCheck.Text -ne "barrier=$barrierId") {
        throw "update_barrier_lost:$($barrierCheck.Text)"
    }

    $resume = Invoke-EaselDocker -Arguments @(
        'exec', $controlId, 'python', '-m', 'easel', 'admin', 'resume',
        '--control-dir', '/srv/easel/control',
        '--barrier-id', $barrierId)
    if ($resume.ExitCode -ne 0) {
        throw 'update_barrier_resume_failed'
    }
    $barrierCheck = Get-BarrierStatus -ControlId $controlId
    if ($barrierCheck.ExitCode -ne 0 -or
        $barrierCheck.Text -ne 'barrier=none') {
        throw "update_barrier_still_active:$($barrierCheck.Text)"
    }

    $oldPidText = 'unavailable'
    if ($null -ne $oldProvisionerPid) {
        $oldPidText = "$($oldProvisionerPid.Output -join '')"
    }
    $summary = [ordered]@{
        project = $ProjectName
        barrier_id = $barrierId
        control_image_before = $oldControlImage
        control_image_after = $Image
        control_runtime_sha256 = $newRuntimeSha
        provisioner_source_before = $oldProvisionerSha
        provisioner_source_after = $newProvisionerSha
        provisioner_pid_before = $oldPidText
        provisioner_pid_after = "$($newProvisionerPid.Output -join '')"
        managed_instances = $managedAfter
        managed_baseline = $managedBefore
        managed_baseline_source = $baselineSource
        managed_baseline_file = $baselineFile
        attempt_records = $attemptAfter
        volumes = @($volumes.Control, $volumes.Keys, $volumes.Egress)
        network = $network
        resumed = $true
        completed_at = [DateTimeOffset]::UtcNow.ToString('o')
    }
    $evidenceFile = Join-Path $logDir 'update-last.json'
    [IO.File]::WriteAllText(
        $evidenceFile,
        ($summary | ConvertTo-Json -Depth 5 -Compress),
        (New-Object System.Text.UTF8Encoding($false)))

    Write-Output ("update_control_image={0}->{1}" -f `
        $oldControlImage, $Image)
    Write-Output ("update_control_runtime_sha256={0}" -f $newRuntimeSha)
    Write-Output ("update_provisioner_source={0}->{1}" -f `
        $oldProvisionerSha, $newProvisionerSha)
    Write-Output ("update_provisioner_pid={0}->{1}" -f `
        $oldPidText, `
        ($newProvisionerPid.Output -join ''))
    Write-Output ("update_managed_instances={0}" -f $managedAfter.Count)
    Write-Output ("update_attempt_records={0}" -f $attemptAfter.Count)
    Write-Output ("update_barrier_resumed={0}" -f $barrierId)
    Write-Output ("update_evidence={0}" -f $evidenceFile)
    Write-Output ("update_completed project={0}" -f $ProjectName)
    exit 0
}
catch {
    [Console]::Error.WriteLine("错误：$($_.Exception.Message)")
    if ($barrierId) {
        [Console]::Error.WriteLine(
            "update_barrier_retained=$barrierId")
    }
    exit 1
}
finally {
    Exit-EaselLifecycleLock -Lock $lifecycleLock
}
