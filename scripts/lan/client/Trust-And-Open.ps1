[CmdletBinding()]
param([switch]$Graphical)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

function Test-Rfc1918Address {
    param([Parameter(Mandatory = $true)][System.Net.IPAddress]$Address)
    $bytes = $Address.GetAddressBytes()
    if ($bytes.Count -ne 4) { return $false }
    return (
        $bytes[0] -eq 10 -or
        ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or
        ($bytes[0] -eq 192 -and $bytes[1] -eq 168)
    )
}

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
    $kitRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
    if ($kitRoot.StartsWith("\\", [System.StringComparison]::Ordinal)) {
        throw "Copy LAN_CLIENT_KIT to a normal local folder before running it."
    }
    . (Join-Path $kitRoot 'Certificate-Store.ps1')
    if ($Graphical) { . (Join-Path $kitRoot 'Setup-Dialogs.ps1') }
    $connectionPath = Join-Path $kitRoot "CONNECTION.json"
    if (-not (Test-Path -LiteralPath $connectionPath -PathType Leaf)) {
        throw "CONNECTION.json is missing. Copy the complete LAN_CLIENT_KIT again."
    }
    $connection = Get-Content -LiteralPath $connectionPath -Raw | ConvertFrom-Json
    if ([int]$connection.schemaVersion -ne 1 -or
        [string]$connection.certificateFile -ne "CSD_Attendance_LAN_CA.cer") {
        throw "The LAN connection document is not supported."
    }

    $serverAddress = $null
    if (-not [System.Net.IPAddress]::TryParse([string]$connection.serverIpv4, [ref]$serverAddress) -or
        -not (Test-Rfc1918Address -Address $serverAddress)) {
        throw "The connection kit does not name an approved RFC1918 private address."
    }
    $serverUri = $null
    if (-not [System.Uri]::TryCreate([string]$connection.serverUrl, [System.UriKind]::Absolute, [ref]$serverUri) -or
        $serverUri.Scheme -ne "https" -or $serverUri.Host -ne $serverAddress.ToString() -or
        $serverUri.Port -ne 443 -or $serverUri.AbsolutePath -ne "/" -or
        -not [string]::IsNullOrEmpty($serverUri.Query) -or
        -not [string]::IsNullOrEmpty($serverUri.Fragment) -or
        -not [string]::IsNullOrEmpty($serverUri.UserInfo)) {
        throw "The connection kit has an invalid HTTPS server URL."
    }

    $certificatePath = Join-Path $kitRoot "CSD_Attendance_LAN_CA.cer"
    if (-not (Test-Path -LiteralPath $certificatePath -PathType Leaf)) {
        throw "The public LAN CA certificate is missing."
    }
    $expectedSha256 = ([string]$connection.certificateSha256).ToLowerInvariant()
    if ($expectedSha256 -notmatch '^[0-9a-f]{64}$') {
        throw "The expected LAN CA SHA-256 is invalid."
    }
    $actualFileSha256 = (Get-FileHash -LiteralPath $certificatePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualFileSha256 -ne $expectedSha256) {
        throw "The public LAN CA certificate does not match CONNECTION.json."
    }

    $certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($certificatePath)
    if ($certificate.HasPrivateKey) {
        throw "The client certificate file unexpectedly contains a private key."
    }
    $expectedThumbprint = ([string]$connection.certificateThumbprintSha1).Replace(" ", "").ToUpperInvariant()
    if ($expectedThumbprint -notmatch '^[0-9A-F]{40}$' -or $certificate.Thumbprint -ne $expectedThumbprint -or
        $certificate.Subject -ne [string]$connection.certificateSubject -or
        (Get-CertificateSha256 -Certificate $certificate) -ne $expectedSha256) {
        throw "The public LAN CA certificate identity is inconsistent."
    }

    $installed = @(Get-CsdTrustedCertificate -Thumbprint $expectedThumbprint)
    if ($installed.Count -gt 1) {
        throw "More than one matching trusted-root certificate exists. Contact your technician."
    }
    if ($installed.Count -eq 1) {
        if ((Get-CertificateSha256 -Certificate $installed[0]) -ne $expectedSha256) {
            throw "A different trusted certificate has the expected thumbprint. Contact your technician."
        }
        Write-Host "This exact LAN certificate is already trusted for the current Windows user." -ForegroundColor Green
    }
    else {
        Write-Host ""
        Write-Host "CSD Attendance private LAN certificate" -ForegroundColor Yellow
        Write-Host ("Server:  {0}" -f $serverUri.AbsoluteUri)
        Write-Host ("SHA-256: {0}" -f $expectedSha256.ToUpperInvariant())
        Write-Host ""
        Write-Host "Compare this SHA-256 with the value shown on the server laptop."
        Write-Host "Trusting the wrong certificate could expose passwords and attendance data."
        if ($Graphical) {
            $choice = Show-CsdSetupChoice -Title 'Connect to your attendance server' -Message (
                "Server: {0}`r`n`r`nCompare the fingerprint below with the server owner's copy. Continue only when they match. This adds this server's certificate to the current Windows account's trusted certificates.`r`n`r`nCertificate fingerprint (SHA-256):`r`n{1}" -f $serverUri.AbsoluteUri,$expectedSha256.ToUpperInvariant()
            ) -Choices @(@{ Value='trust'; Label='The fingerprints match - connect'; Detail='Trust the displayed certificate for this Windows account and open the attendance system.' })
            $confirmed = $choice -ceq 'trust'
        }
        else { $confirmed = (Read-Host "If the values match exactly, type TRUST") -ceq 'TRUST' }
        if (-not $confirmed) {
            Write-Host 'Connection cancelled. No certificate was installed.'
            exit 2
        }
        # Import the exact in-memory certificate shown above; a file replacement
        # while the dialog was open must never change the approved certificate.
        Add-CsdTrustedCertificate -Certificate $certificate
        $imported = @(Get-CsdTrustedCertificate -Thumbprint $expectedThumbprint)
        if ($imported.Count -ne 1 -or (Get-CertificateSha256 -Certificate $imported[0]) -ne $expectedSha256) {
            throw "Windows did not import the expected LAN certificate."
        }
        Write-Host "The exact LAN certificate is now trusted for this Windows user." -ForegroundColor Green
    }

    while ($true) {
        try {
            Write-Host 'Checking the connection to your attendance server...'
            $loginUri = [System.Uri]::new($serverUri, "login")
            $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 -MaximumRedirection 0 -Uri $loginUri
            if ($response.StatusCode -ne 200 -or -not $response.Content.Contains('id="login-form"')) {
                throw "The server did not return the CSD Attendance login page."
            }
            break
        }
        catch {
            if (-not $Graphical) {
                throw ("The certificate is trusted, but the LAN server is not reachable at {0} Confirm that both laptops are on the same private network and START_CSD_LAN_SERVER.cmd is running." -f $serverUri.AbsoluteUri)
            }
            $choice = Show-CsdSetupChoice -Title 'The server is not ready yet' -Message (
                "This account trusts the approved certificate, but CSD Attendance could not be reached at {0}`r`n`r`nKeep the server on and connect both computers to the same router. If the server address changed, ask the owner for a new connection kit." -f $serverUri.AbsoluteUri
            ) -Choices @(@{ Value='retry'; Label='Check connection again'; Detail='Retry using the same approved server and certificate.' }) -CancelText 'Close'
            if ($choice -cne 'retry') {
                Write-Host 'Connection check closed. The approved certificate remains trusted. Reopen CONNECT_TO_CSD.cmd to retry.'
                exit 2
            }
        }
    }

    Start-Process -FilePath $serverUri.AbsoluteUri
    Write-Host ("Opened {0}" -f $serverUri.AbsoluteUri) -ForegroundColor Green
    exit 0
}
catch {
    if ($Graphical -and (Get-Command Show-CsdSetupNotice -ErrorAction SilentlyContinue)) {
        Show-CsdSetupNotice -Title 'Connection needs attention' -Message $_.Exception.Message
    }
    Write-Host ""
    Write-Host ("CONNECTION FAILED: {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}
