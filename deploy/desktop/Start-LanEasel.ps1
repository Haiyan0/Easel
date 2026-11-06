<#
.SYNOPSIS
    为已初始化的多人实例启动局域网 HTTPS 入口。
.DESCRIPTION
    自动选择带网关的实体网卡；IP 变化后重新执行。复用现有启动脚本，
    保留镜像、发布策略、数据和数据库查询接线。证书根存于 DataRoot/lan。
    默认仅准备配置；传入 -Apply 才启动代理并切换登录来源。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$DataRoot,
    [string]$LanAddress = '',
    [ValidateRange(1, 65535)][int]$HttpsPort = 7861,
    [ValidateRange(1, 65535)][int]$AdminPort = 17861,
    [string]$CaddyExe = 'C:\Easel\tools\caddy.exe',
    [string]$Distro = 'Ubuntu-26.04',
    [string]$WslRepo = '/mnt/c/Code/Easel',
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'EaselDesktop.Common.ps1')
. (Join-Path $PSScriptRoot 'EaselLan.Common.ps1')

$DataRoot = [IO.Path]::GetFullPath($DataRoot)
$deploymentFile = Join-Path $DataRoot 'logs\deployment.json'
$deployment = Read-EaselLanJson -Path $deploymentFile
if ([IO.Path]::GetFullPath([string]$deployment.data_root) -ne $DataRoot) {
    throw 'deployment_data_root_mismatch'
}
$physical = @(Get-NetAdapter -Physical | Where-Object Status -eq 'Up')
$interfaces = @(Get-NetIPConfiguration | Where-Object {
    $_.InterfaceIndex -in $physical.ifIndex -and $_.IPv4DefaultGateway
})
if (-not $LanAddress) {
    $LanAddress = Resolve-EaselLanAddress -Addresses @(
        $interfaces | ForEach-Object { $_.IPv4Address.IPAddress })
}
$LanAddress = Resolve-EaselLanAddress -Addresses @($LanAddress)
$localIp = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object IPAddress -eq $LanAddress)
if ($localIp.Count -ne 1) { throw 'lan_address_not_local' }
$project = Resolve-EaselProject -ProjectName $deployment.project
$backendPort = Resolve-EaselPort -Port $deployment.port
$lanRoot = Join-Path $DataRoot 'lan'
$storage = Join-Path $lanRoot 'storage'
$configText = New-EaselLanCaddyfile -Address $LanAddress -HttpsPort $HttpsPort `
    -BackendPort $backendPort -AdminPort $AdminPort -StorageRoot $storage
$origin = Resolve-EaselOrigin -Origin "https://${LanAddress}:$HttpsPort"
$null = New-Item -ItemType Directory -Path $lanRoot -Force
$candidate = Join-Path $lanRoot 'Caddyfile.next'
$config = Join-Path $lanRoot 'Caddyfile'
$utf8 = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText($candidate, $configText, $utf8)
$state = [ordered]@{
    project = $project; origin = $origin; backend_port = $backendPort
    address = $LanAddress; port = $HttpsPort; admin_port = $AdminPort
    interface_alias = [string]$localIp[0].InterfaceAlias
    prefix_length = [int]$localIp[0].PrefixLength
    caddy = $CaddyExe; config = $config
    certificate = (Join-Path $lanRoot 'Easel-LAN-Root.crt')
}
if (-not $Apply) {
    $state | ConvertTo-Json
    Write-Output 'prepared_only: 使用 -Apply 启动；需要已安装 Caddy。'
    exit 0
}
if (-not (Test-Path -LiteralPath $CaddyExe -PathType Leaf)) { throw 'caddy_missing' }
$CaddyExe = (Resolve-Path -LiteralPath $CaddyExe).Path
& $CaddyExe validate --config $candidate --adapter caddyfile
if ($LASTEXITCODE -ne 0) { throw 'lan_proxy_config_invalid' }

# 仅保存第一次切换前的来源，后续 IP 变化不覆盖回退入口。
$previousOrigin = Join-Path $lanRoot 'previous-origin.txt'
if (-not (Test-Path -LiteralPath $previousOrigin)) {
    [IO.File]::WriteAllText($previousOrigin, [string]$deployment.origin, $utf8)
}
$startArgs = @{
    PublicOrigin = $origin; Image = [string]$deployment.control_image
    WorkerImage = [string]$deployment.worker_image
    GatewayImage = [string]$deployment.gateway_image
    ProjectName = $project; Port = $backendPort
    WorkspaceRoot = [string]$deployment.workspace_root; DataRoot = $DataRoot
    PublishMode = [string]$deployment.publish_mode; Distro = $Distro; WslRepo = $WslRepo
}
# 数据库密钥路径不在 deployment.json；从已有 provisioner 身份恢复该接线。
$identityFile = Join-Path $DataRoot 'logs\provisioner-expected.json'
if (Test-Path -LiteralPath $identityFile) {
    $identity = Read-EaselLanJson -Path $identityFile
    $startArgs.ProvisionerGid = [int]$identity.socketGid
    if ($identity.databaseQueryEnvFile) {
        $envPath = [string]$identity.databaseQueryEnvFile
        if ($envPath -notmatch '^/mnt/([a-z])/(.+)/database-query\.env$') {
            throw 'database_query_path_not_windows'
        }
        $startArgs.DatabaseQuerySecrets = $Matches[1].ToUpperInvariant() + ':\' + $Matches[2].Replace('/', '\')
    }
}
$argsFile = Join-Path $lanRoot 'start-args.xml'
$startArgs | Export-Clixml -LiteralPath $argsFile
$pidFile = Join-Path $lanRoot 'caddy.pid'
$running = $null
if (Test-Path -LiteralPath $pidFile) {
    $proxyPid = 0
    if ([int]::TryParse((Get-Content -LiteralPath $pidFile -Raw -Encoding UTF8).Trim(), [ref]$proxyPid)) {
        $running = Get-Process -Id $proxyPid -ErrorAction SilentlyContinue
    }
    if ($running) {
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$proxyPid"
        if ($running.Path -ne $CaddyExe -or -not $processInfo.CommandLine.Contains($candidate)) {
            throw 'lan_proxy_pid_mismatch'
        }
    }
}
if ($running) {
    & $CaddyExe reload --address "127.0.0.1:$AdminPort" --config $candidate --adapter caddyfile
    if ($LASTEXITCODE -ne 0) { throw 'lan_proxy_reload_failed' }
}
else {
    # 端口被其他进程使用时拒绝接管；不通过管理接口修改未知 Caddy。
    foreach ($listenPort in @($HttpsPort, $AdminPort)) {
        if (Get-NetTCPConnection -State Listen -LocalPort $listenPort -ErrorAction SilentlyContinue) {
            throw "lan_port_in_use:$listenPort"
        }
    }
    $proxy = Start-Process -FilePath $CaddyExe -ArgumentList @(
        'run', '--config', ('"' + $candidate + '"'), '--adapter', 'caddyfile') `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $lanRoot 'caddy.stdout.log') `
        -RedirectStandardError (Join-Path $lanRoot 'caddy.stderr.log')
    [IO.File]::WriteAllText($pidFile, [string]$proxy.Id, $utf8)
}
$rootCert = Join-Path $storage 'pki\authorities\local\root.crt'
$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    if (Test-Path -LiteralPath $rootCert) {
        try {
            $null = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$backendPort/api/health" -TimeoutSec 3
            if (Get-NetTCPConnection -State Listen -LocalPort $HttpsPort -ErrorAction SilentlyContinue) {
                $ready = $true
                break
            }
        }
        catch { }
    }
    Start-Sleep -Seconds 1
}
if (-not $ready) { throw 'lan_proxy_start_timeout' }
Copy-Item -LiteralPath $rootCert -Destination $state.certificate -Force
if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) { throw 'curl_missing' }
# 本地 CA 不提供公共吊销服务，仍要求证书链、有效期与 IP 地址全部匹配。
& curl.exe --noproxy '*' --ssl-revoke-best-effort --cacert $rootCert `
    --fail --silent --show-error --max-time 10 "$origin/api/health"
if ($LASTEXITCODE -ne 0) { throw 'lan_tls_health_failed' }

# 用子进程执行，隔离既有脚本的 exit 与环境变量。启动失败时不伪造成功。
$startScript = Join-Path $PSScriptRoot 'Start-WorkspaceEasel.ps1'
$escapedArgs = $argsFile.Replace("'", "''")
$escapedScript = $startScript.Replace("'", "''")
$command = "`$ProgressPreference='SilentlyContinue'; `$a = Import-Clixml -LiteralPath '$escapedArgs'; & '$escapedScript' @a"
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
& powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand $encoded
if ($LASTEXITCODE -ne 0) {
    throw 'lan_control_start_failed: 请查看现有启动日志；原来源保存在 lan/previous-origin.txt'
}
& curl.exe --noproxy '*' --ssl-revoke-best-effort --cacert $rootCert `
    --fail --silent --show-error --max-time 10 "$origin/api/health"
if ($LASTEXITCODE -ne 0) { throw 'lan_control_health_failed' }
Copy-Item -LiteralPath $candidate -Destination $config -Force
[IO.File]::WriteAllText((Join-Path $lanRoot 'state.json'), ($state | ConvertTo-Json), $utf8)
Write-Output "lan_origin=$origin"
Write-Output "root_certificate=$($state.certificate)"
Write-Output '请在客户端导入根证书；入口防火墙规则由 Set-EaselLanFirewall.ps1 配置。'
