[CmdletBinding()]
param(
    [switch]$Live
)

. (Join-Path $PSScriptRoot "Lan-Common.ps1")

$lifecycleMutex = $null
$lifecycleMutexAcquired = $false

try {
    Assert-LanRootLocation
    Assert-LanPrivateStateIsLocal
    Assert-LanClientKitIsLocal
    Assert-LanPrivateStateAcl
    Set-Location -LiteralPath $script:LanRootPath
    # The offline client checker takes the same lifecycle mutex as the running
    # server. A live probe must not require stopping the server to pass it.
    if (-not $Live) {
        Invoke-StandaloneClientCheck
        $lifecycleMutex = New-CsdLifecycleMutex -Path $script:LanRootPath
        try {
            $lifecycleMutexAcquired = $lifecycleMutex.WaitOne(0)
        }
        catch [System.Threading.AbandonedMutexException] {
            $lifecycleMutexAcquired = $true
        }
        if (-not $lifecycleMutexAcquired) {
            throw "CSD Attendance or another setup/check is already running. Stop it before this offline LAN check."
        }
        Assert-CsdNoPendingUpdate -Path $script:LanRootPath
        # Complete only the metadata of a verified prior update. This never
        # configures a network, certificate trust, or firewall rule.
        Complete-CsdLanUpdate
    }
    $network = Get-LanNetworkCandidate
    Invoke-LanNativeCommand -FilePath $script:LanVenvPythonPath -Arguments @(
        $script:LanSetupPath, "check", "--root", $script:LanRootPath,
        "--server-ip", $network.ServerIPv4, "--prefix-length", ([string]$network.PrefixLength)
    ) -FailureMessage "The LAN server state check failed"
    if (-not $Live) {
        Invoke-CaddyConfigurationCheck
    }
    Assert-ServerCertificateTrusted
    Invoke-FirewallCheck
    $state = Get-LanState
    Write-Host ""
    Write-Host "LAN CONFIGURATION CHECK PASSED" -ForegroundColor Green
    Write-Host ("Server URL: {0}" -f $state.publicUrl)
    Write-Host ("Allowed network: {0}" -f $state.networkCidr)
    Write-Host ("LAN CA SHA-256: {0}" -f ([string]$state.rootCertificateSha256).ToUpperInvariant())
    Write-Host ("Browser client kit: {0}" -f (Join-Path $script:LanRootPath "LAN_CLIENT_KIT"))
    if ($Live) {
        $probePath = Join-Path $PSScriptRoot "lan_probe.py"
        & $script:LanVenvPythonPath $probePath --root $script:LanRootPath `
            --server-ip $network.ServerIPv4 --prefix-length ([string]$network.PrefixLength)
        exit $LASTEXITCODE
    }
    Write-Host "The live HTTPS service was not probed. Run this check with -Live after starting the server."
    exit 0
}
catch {
    Write-Host ""
    Write-Host ("LAN CHECK FAILED: {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}
finally {
    if ($lifecycleMutexAcquired -and $null -ne $lifecycleMutex) {
        try { $lifecycleMutex.ReleaseMutex() } catch { }
    }
    if ($null -ne $lifecycleMutex) { $lifecycleMutex.Dispose() }
}
