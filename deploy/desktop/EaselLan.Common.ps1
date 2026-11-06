# 局域网地址与代理配置的纯函数；不修改部署状态。
function Read-EaselLanJson {
    param([Parameter(Mandatory)][string]$Path)
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Resolve-EaselLanAddress {
    param([AllowEmptyCollection()][string[]]$Addresses = @())
    $unique = @($Addresses | Select-Object -Unique)
    if ($unique.Count -ne 1) { throw 'lan_address_required' }
    $address = $unique[0]
    $parsed = $null
    if (-not [Net.IPAddress]::TryParse($address, [ref]$parsed) -or
        $parsed.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
        $parsed.ToString() -cne $address) {
        throw 'invalid_lan_address'
    }
    $bytes = $parsed.GetAddressBytes()
    if (-not ($bytes[0] -eq 10 -or
        ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or
        ($bytes[0] -eq 192 -and $bytes[1] -eq 168))) {
        throw 'invalid_lan_address'
    }
    return $address
}

function New-EaselLanCaddyfile {
    param(
        [string]$Address,
        [ValidateRange(1, 65535)][int]$HttpsPort,
        [ValidateRange(1, 65535)][int]$BackendPort,
        [ValidateRange(1, 65535)][int]$AdminPort,
        [string]$StorageRoot
    )
    $Address = Resolve-EaselLanAddress -Addresses @($Address)
    if (@($HttpsPort, $BackendPort, $AdminPort | Select-Object -Unique).Count -ne 3) {
        throw 'lan_port_conflict'
    }
    if ([string]::IsNullOrWhiteSpace($StorageRoot) -or $StorageRoot -match '["\r\n{}]') {
        throw 'invalid_storage_path'
    }
    $storage = $StorageRoot.Replace('\', '/')
    return @"
{
    admin 127.0.0.1:$AdminPort
    auto_https disable_redirects
    skip_install_trust
    storage file_system "$storage"
}
https://${Address}:$HttpsPort {
    bind $Address
    tls internal
    reverse_proxy 127.0.0.1:$BackendPort {
        flush_interval -1
    }
}
"@
}
