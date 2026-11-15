[CmdletBinding()]
param(
    [switch]$NoBrowser
)

. (Join-Path $PSScriptRoot "Lan-Common.ps1")
Initialize-CsdSetupResult -RootPath $script:LanRootPath -Stage lan_startup

$lifecycleMutex = $null
$lifecycleMutexAcquired = $false
$browserHelper = $null

try {
    Assert-LanRootLocation
    Assert-CsdClientPrivateStorage -Path $script:LanRootPath
    Assert-LanPrivateStateIsLocal
    Assert-LanClientKitIsLocal
    Assert-LanPrivateStateAcl
    Set-Location -LiteralPath $script:LanRootPath
    Invoke-StandaloneClientCheck

    $lifecycleMutex = New-CsdLifecycleMutex -Path $script:LanRootPath
    try {
        $lifecycleMutexAcquired = $lifecycleMutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $lifecycleMutexAcquired = $true
    }
    if (-not $lifecycleMutexAcquired) {
        throw "CSD Attendance, First Run, or a system check is already running on this laptop."
    }
    Assert-CsdNoPendingUpdate -Path $script:LanRootPath
    Assert-CsdClientPrivateStorage -Path $script:LanRootPath
    Complete-CsdLanUpdate

    $network = Get-LanNetworkCandidate
    Invoke-LanNativeCommand -FilePath $script:LanVenvPythonPath -Arguments @(
        $script:LanSetupPath, "check", "--root", $script:LanRootPath,
        "--server-ip", $network.ServerIPv4, "--prefix-length", ([string]$network.PrefixLength)
    ) -FailureMessage "The LAN server state check failed"
    Invoke-CaddyConfigurationCheck
    Assert-ServerCertificateTrusted
    Invoke-FirewallCheck

    $state = Get-LanState
    Assert-LanPortsAvailable -ServerIPv4 $network.ServerIPv4 -BackendPort ([int]$state.backendPort)

    foreach ($name in @(
        "SECRET_KEY", "CAMERA_SOURCE_KEY", "BIOMETRIC_KEY",
        "BACKUP_ARCHIVE_KEY", "PURGE_ARCHIVE_KEY", "ENTRANCE_DISPLAY_TOKEN",
        "FLASK_DEBUG", "FLASK_HOST", "FLASK_PORT", "SESSION_WORKER_MODE",
        "CCTV_HOST", "CCTV_PORT", "CCTV_USERNAME", "CCTV_PASSWORD",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"
    )) {
        [Environment]::SetEnvironmentVariable($name, $null, [EnvironmentVariableTarget]::Process)
    }
    $env:PYTHONUNBUFFERED = "1"

    if (-not $NoBrowser) {
        $helperPath = Join-Path $PSScriptRoot "Open-LAN-BrowserWhenReady.ps1"
        $windowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
        $helperArguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "{0}" -Url "{1}" -ParentProcessId {2}' -f `
            $helperPath, [string]$state.publicUrl, $PID
        $browserHelper = Start-Process -FilePath $windowsPowerShell -ArgumentList $helperArguments -WindowStyle Hidden -PassThru
    }

    Write-Host ""
    Write-Host ("Starting the CSD Attendance LAN server at {0}" -f $state.publicUrl) -ForegroundColor Green
    Write-Host ("Authorized browser laptops must be on {0}." -f $state.networkCidr)
    Write-Host "Keep this window open. Press Ctrl+C once to stop the system safely."
    Write-Host ""
    $serverExitCode = Invoke-CsdContainedProcess -FilePath $script:LanVenvPythonPath -Arguments @(
        $script:LanServerPath, "--root", $script:LanRootPath
    ) -WorkingDirectory $script:LanRootPath
    if ($serverExitCode -eq -1073741510 -or $serverExitCode -eq 3221225786) {
        Write-Host "CSD Attendance LAN server was stopped by the operator."
        Write-CsdSetupResult -Outcome success -ExitCode 0
        exit 0
    }
    if ($serverExitCode -ne 0) {
        throw ("The CSD Attendance LAN supervisor exited with code {0}." -f $serverExitCode)
    }
    Write-CsdSetupResult -Outcome success -ExitCode 0
    exit 0
}
catch {
    $public=Get-CsdPublicSetupResult
    $cleanup=if ($public.code -eq 'CLEANUP_UNCONFIRMED') { 'unknown' } else { 'confirmed' }
    $outcome=if ($cleanup -eq 'unknown' -or $public.code -eq 'SETUP_RESULT_UNKNOWN') { 'unknown' } else { 'failed' }
    $failureExit=Get-CsdFailureExitCode
    Write-CsdSetupResult -Outcome $outcome -Cleanup $cleanup -ExitCode $failureExit
    Write-Host ""
    Write-Host ("LAN START STOPPED [{0}]: {1}" -f $public.code,$public.message) -ForegroundColor Red
    exit $failureExit
}
finally {
    if ($null -ne $browserHelper) {
        try {
            if (-not $browserHelper.HasExited) {
                Stop-Process -Id $browserHelper.Id -Force -ErrorAction SilentlyContinue
            }
        }
        catch { }
    }
    if ($lifecycleMutexAcquired -and $null -ne $lifecycleMutex) {
        try { $lifecycleMutex.ReleaseMutex() } catch { }
    }
    if ($null -ne $lifecycleMutex) {
        $lifecycleMutex.Dispose()
    }
}
