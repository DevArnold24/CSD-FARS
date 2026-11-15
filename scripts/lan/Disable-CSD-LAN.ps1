[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot "Lan-Common.ps1")

try {
    Assert-LanRootLocation
    Assert-LanPrivateStateIsLocal
    $state = Get-LanState
    Write-Host ("This will close Windows Firewall access to {0}" -f $state.publicUrl) -ForegroundColor Yellow
    $confirmation = Read-Host "Type DISABLE to remove only the exact CSD Attendance LAN firewall rule"
    if ($confirmation -cne "DISABLE") {
        throw "LAN access disablement was cancelled."
    }
    Invoke-ElevatedFirewallAction -Action "Remove"
    Write-Host "The exact CSD Attendance LAN firewall rule was removed." -ForegroundColor Green
    Write-Host "No database, application secret, certificate, private key, or recovery state was deleted."
    exit 0
}
catch {
    Write-Host ""
    Write-Host ("LAN DISABLE FAILED: {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}
