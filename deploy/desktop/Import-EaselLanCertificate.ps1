<#
.SYNOPSIS
    将 Easel 局域网根证书导入当前 Windows 用户，无需管理员权限。
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$CertificatePath,
    [string]$ExpectedThumbprint = ''
)
$ErrorActionPreference = 'Stop'
$certificate = New-Object Security.Cryptography.X509Certificates.X509Certificate2(
    (Resolve-Path -LiteralPath $CertificatePath).Path)
if ($certificate.HasPrivateKey -or $certificate.Subject -ne $certificate.Issuer) {
    throw 'lan_certificate_must_be_public_root'
}
$constraints = @($certificate.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.19' })
if ($constraints.Count -ne 1 -or -not $constraints[0].CertificateAuthority) {
    throw 'lan_certificate_not_ca'
}
if ($ExpectedThumbprint -and $certificate.Thumbprint -ne $ExpectedThumbprint.Replace(' ', '')) {
    throw 'lan_certificate_thumbprint_mismatch'
}
if ($certificate.NotAfter -le (Get-Date) -or $certificate.NotBefore -gt (Get-Date)) {
    throw 'lan_certificate_expired_or_not_valid_yet'
}
if ($PSCmdlet.ShouldProcess($certificate.Thumbprint, '信任 Easel 局域网根证书（当前用户）')) {
    $store = New-Object Security.Cryptography.X509Certificates.X509Store('Root', 'CurrentUser')
    try {
        $store.Open([Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
        $store.Add($certificate)
    }
    finally { $store.Close() }
}
Write-Output "certificate_thumbprint=$($certificate.Thumbprint)"
