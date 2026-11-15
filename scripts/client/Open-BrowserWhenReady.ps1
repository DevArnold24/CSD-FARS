[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][ValidateRange(1, 2147483647)][int]$ParentProcessId,
    [ValidateRange(5, 300)][int]$TimeoutSeconds = 90
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

try {
    $target = [System.Uri]$Url
    if ($target.Scheme -ne "http" -or $target.Host -ne "127.0.0.1" -or $target.AbsolutePath -ne "/login" -or
        -not [string]::IsNullOrEmpty($target.UserInfo)) {
        exit 2
    }

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($null -eq (Get-Process -Id $ParentProcessId -ErrorAction SilentlyContinue)) {
            exit 3
        }

        try {
            $response = Invoke-WebRequest -Uri $target.AbsoluteUri -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) {
                Start-Process -FilePath $target.AbsoluteUri
                exit 0
            }
        }
        catch {
            # The foreground server may still be importing native dependencies.
        }

        Start-Sleep -Milliseconds 500
    }
    exit 4
}
catch {
    exit 5
}
