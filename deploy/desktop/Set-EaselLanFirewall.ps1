<#
.SYNOPSIS
    以管理员身份为已启动的局域网入口放行当前网卡子网。
#>
[CmdletBinding(SupportsShouldProcess)]
param([Parameter(Mandatory)][string]$StateFile)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'EaselLan.Common.ps1')
. (Join-Path $PSScriptRoot 'EaselDesktop.Common.ps1')
$state = Read-EaselLanJson -Path $StateFile
$address = Resolve-EaselLanAddress -Addresses @([string]$state.address)
$project = Resolve-EaselProject -ProjectName $state.project
$port = Resolve-EaselPort -Port $state.port
$localIp = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object IPAddress -eq $address)
if ($localIp.Count -ne 1 -or $localIp[0].InterfaceAlias -ne $state.interface_alias -or
    $localIp[0].PrefixLength -ne $state.prefix_length) { throw 'lan_state_stale: 请重新运行 Start-LanEasel.ps1' }
$program = (Resolve-Path -LiteralPath $state.caddy).Path
$ruleName = "Easel-LAN-$project"
if ($PSCmdlet.ShouldProcess("${address}:$port", '仅放行当前网卡的局域网子网')) {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'firewall_requires_administrator'
    }
    $rule = @{
        Name = $ruleName; DisplayName = "Easel LAN ($project)"; Group = 'EaselLAN'
        Direction = 'Inbound'; Action = 'Allow'; Enabled = 'True'; Protocol = 'TCP'
        LocalPort = $port; LocalAddress = $address
        RemoteAddress = "$address/$($state.prefix_length)"
        InterfaceAlias = [string]$state.interface_alias; Program = $program
        Profile = 'Any'; EdgeTraversalPolicy = 'Block'
    }
    $existing = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    if ($existing) {
        if ($existing.Group -ne 'EaselLAN') { throw 'firewall_rule_not_owned' }
        $rule.Remove('Group')
        Set-NetFirewallRule @rule | Out-Null
    }
    else { New-NetFirewallRule @rule | Out-Null }
    Write-Output "firewall_ready=$ruleName"
}
