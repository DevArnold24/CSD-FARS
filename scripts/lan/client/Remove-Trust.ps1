[CmdletBinding()]
param([switch]$Graphical)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

function Get-CertificateSha256 {
    param([Parameter(Mandatory = $true)][System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        return (-join @($sha256.ComputeHash($Certificate.RawData) | ForEach-Object { $_.ToString("x2") }))
    }
    finally {
        $sha256.Dispose()
    }
}

try {
    . (Join-Path $PSScriptRoot 'Certificate-Store.ps1')
    if ($Graphical) { . (Join-Path $PSScriptRoot 'Setup-Dialogs.ps1') }
    $connectionPath = Join-Path $PSScriptRoot "CONNECTION.json"
    if (-not (Test-Path -LiteralPath $connectionPath -PathType Leaf)) {
        throw "CONNECTION.json is missing. Refusing to remove any certificate."
    }
    $connection = Get-Content -LiteralPath $connectionPath -Raw | ConvertFrom-Json
    $expectedThumbprint = ([string]$connection.certificateThumbprintSha1).Replace(" ", "").ToUpperInvariant()
    $expectedSha256 = ([string]$connection.certificateSha256).ToLowerInvariant()
    if ($expectedThumbprint -notmatch '^[0-9A-F]{40}$' -or
        $expectedSha256 -notmatch '^[0-9a-f]{64}$') {
        throw "The connection kit does not contain an exact certificate identity."
    }

    $installed = @(Get-CsdTrustedCertificate -Thumbprint $expectedThumbprint)
    if ($installed.Count -eq 0) {
        Write-Host "The exact CSD Attendance LAN certificate is not trusted by this Windows user."
        if ($Graphical) { Show-CsdSetupNotice -Title 'Connection trust is already removed' -Message 'This Windows account does not trust the certificate named in this connection kit.' }
        exit 0
    }
    if ($installed.Count -ne 1 -or
        (Get-CertificateSha256 -Certificate $installed[0]) -ne $expectedSha256) {
        throw "The trusted certificate does not match this kit exactly. Refusing to remove it."
    }

    Write-Host ("Certificate SHA-256: {0}" -f $expectedSha256.ToUpperInvariant()) -ForegroundColor Yellow
    if ($Graphical) {
        $choice = Show-CsdSetupChoice -Title 'Remove this connection trust?' -Message (
            "Remove the displayed certificate from this Windows account's trusted certificates. Only continue when this account no longer needs this server connection.`r`n`r`nCertificate fingerprint (SHA-256):`r`n{0}" -f $expectedSha256.ToUpperInvariant()
        ) -Choices @(@{ Value='remove'; Label='Remove connection trust'; Detail='Remove only the exact certificate displayed above from this Windows account.' })
        $confirmed = $choice -ceq 'remove'
    }
    else { $confirmed = (Read-Host "Type REMOVE to remove only this certificate from the current user's trust store") -ceq 'REMOVE' }
    if (-not $confirmed) {
        Write-Host 'Certificate removal cancelled.'
        exit 2
    }
    Remove-CsdTrustedCertificate -Certificate $installed[0]
    if (@(Get-CsdTrustedCertificate -Thumbprint $expectedThumbprint).Count -ne 0) {
        throw "Windows did not remove the expected certificate."
    }
    Write-Host "The exact CSD Attendance LAN certificate was removed." -ForegroundColor Green
    if ($Graphical) { Show-CsdSetupNotice -Title 'Connection trust removed' -Message 'The approved certificate was removed from this Windows account.' }
    exit 0
}
catch {
    if ($Graphical -and (Get-Command Show-CsdSetupNotice -ErrorAction SilentlyContinue)) {
        Show-CsdSetupNotice -Title 'Trust removal needs attention' -Message $_.Exception.Message
    }
    Write-Host ""
    Write-Host ("TRUST REMOVAL FAILED: {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}
