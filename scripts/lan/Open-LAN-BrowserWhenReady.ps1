[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][int]$ParentProcessId
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

try {
    $uri = $null
    $address = $null
    if (-not [System.Uri]::TryCreate($Url, [System.UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -ne "https" -or $uri.Port -ne 443 -or $uri.AbsolutePath -ne "/" -or
        -not [System.Net.IPAddress]::TryParse($uri.Host, [ref]$address)) {
        exit 1
    }
    $bytes = $address.GetAddressBytes()
    $isPrivate = $bytes.Count -eq 4 -and (
        $bytes[0] -eq 10 -or
        ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or
        ($bytes[0] -eq 192 -and $bytes[1] -eq 168)
    )
    if (-not $isPrivate) { exit 1 }

    $loginUri = [System.Uri]::new($uri, "login")
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($null -eq (Get-Process -Id $ParentProcessId -ErrorAction SilentlyContinue)) {
            exit 0
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri $loginUri
            if ($response.StatusCode -eq 200 -and $response.Content.Contains('id="login-form"')) {
                Start-Process -FilePath $uri.AbsoluteUri
                exit 0
            }
        }
        catch { }
        Start-Sleep -Milliseconds 500
    }
    exit 1
}
catch {
    exit 1
}
