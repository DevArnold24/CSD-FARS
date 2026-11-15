[CmdletBinding()]
param(
    [ValidateSet("Enable", "Status", "Disable", "Start", "Stop")]
    [string]$Action = "Status"
)

. (Join-Path $PSScriptRoot "Autostart-Common.ps1")

try {
    Invoke-CsdAutostart -Action $Action
    exit 0
}
catch {
    Write-Host ("AUTOSTART ACTION FAILED: {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}
