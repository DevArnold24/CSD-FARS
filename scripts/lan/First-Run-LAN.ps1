[CmdletBinding()]
param([switch]$Graphical, [string]$ExpectedIPv4 = '', [int]$ExpectedPrefixLength = 0, [int]$ExpectedInterfaceIndex = 0,
      [switch]$Reconfigure, [switch]$RestoreLan)

. (Join-Path $PSScriptRoot "Lan-Common.ps1")
if ($Graphical) { . (Join-Path $PSScriptRoot '..\windows\Setup-Dialogs.ps1') }
Initialize-CsdSetupResult -RootPath $script:LanRootPath -Stage lan_setup

$bootstrapGateway = $null
$lifecycleMutex = $null
$lifecycleMutexAcquired = $false
$certificate = $null
$setupSucceeded = $false
$networkSelection = @{}
if ($Reconfigure) { $networkSelection['IgnoreSavedBinding'] = $true }
foreach ($name in @('ExpectedIPv4', 'ExpectedPrefixLength', 'ExpectedInterfaceIndex')) {
    if ($PSBoundParameters.ContainsKey($name)) { $networkSelection[$name] = $PSBoundParameters[$name] }
}

function Stop-LanBootstrapGateway {
    param($Gateway)
    if ($null -ne $Gateway) {
        try { $Gateway.Dispose() }
        catch { $script:CsdResultCode='CLEANUP_UNCONFIRMED'; throw }
    }
}

try {
    # Both fresh setup and restoration finish shared cleanup before reporting.
    do {
    if ($Reconfigure -and $RestoreLan) { throw 'Choose either refresh or restore for LAN recovery.' }
    Assert-LanRootLocation
    Assert-CsdClientPrivateStorage -Path $script:LanRootPath
    Assert-LanPrivateStateIsLocal
    Assert-LanClientKitIsLocal
    Set-Location -LiteralPath $script:LanRootPath

    foreach ($requiredFile in @(
        $script:LanSetupPath,
        $script:LanCaddyPath,
        (Join-Path $script:LanRootPath "scripts\client\First-Run.ps1")
    )) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw ("Required LAN bundle file is missing: {0}. Download the complete verified Windows Client Bundle." -f $requiredFile)
        }
    }

    # Prove that LAN sharing is safe and possible before downloading a runtime,
    # creating a database, or asking for the first Dean account. Local-only
    # setup remains available through START_HERE.cmd on every network type.
    $initialNetwork = if (-not $RestoreLan) { Get-LanNetworkCandidate @networkSelection } else { $null }

    Write-Host "First, validating or creating this server laptop's private application runtime..."
    $windowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $baseFirstRun = Join-Path $script:LanRootPath "scripts\client\First-Run.ps1"
    $baseArguments = @(
        "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $baseFirstRun
    )
    if ($Graphical) { $baseArguments += '-Graphical' }
    if (-not $RestoreLan) {
        Invoke-LanNativeCommand -FilePath $windowsPowerShell -Arguments $baseArguments -TimeoutSeconds 1800 -Interactive -FailureMessage "The base client First Run did not complete"
    }

    Invoke-StandaloneClientCheck
    # The separate client checker must finish before this process owns the lock.
    # Hold it throughout LAN state/certificate changes so startup cannot race setup.
    $lifecycleMutex = New-CsdLifecycleMutex -Path $script:LanRootPath
    try {
        $lifecycleMutexAcquired = $lifecycleMutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $lifecycleMutexAcquired = $true
    }
    if (-not $lifecycleMutexAcquired) {
        throw "CSD Attendance or another setup/check is already running. Stop it before changing LAN setup."
    }
    Assert-CsdNoPendingUpdate -Path $script:LanRootPath
    Assert-CsdClientPrivateStorage -Path $script:LanRootPath
    Complete-CsdLanUpdate
    if ($RestoreLan) {
        Assert-LanPrivateStateAcl
        Invoke-LanAddressRecovery -Action rollback
        Protect-LanPrivateState
        $firewallRestored = $false
        try { Invoke-FirewallCheck; $firewallRestored = $true }
        catch { if ($script:CsdResultCode -eq 'CLEANUP_UNCONFIRMED') { throw } }
        if (-not $firewallRestored) {
            if ($Graphical) {
                $choice = Show-CsdSetupChoice -Title 'Restore the preceding LAN access rule' -Message (
                    'The preceding local configuration has been restored. Windows approval is needed to restore its exact private-network firewall addresses. This does not change an adapter or router.'
                ) -Choices @(@{Value='restore';Label='Continue to Windows approval';Detail='Restore only the recorded installation rule. Cancel keeps LAN readiness unconfirmed.'})
                if ($choice -cne 'restore') { $script:CsdResultCode='SETUP_CANCELLED'; throw 'Firewall restoration was cancelled. Keep LAN stopped and choose Restore LAN connection again.' }
            }
            Invoke-ElevatedFirewallAction -Action Restore
            Invoke-FirewallCheck
        }
        $setupSucceeded = $true
        break
    }
    $network = Get-LanNetworkCandidate @networkSelection
    if ($network.ServerIPv4 -ne $initialNetwork.ServerIPv4 -or
        $network.PrefixLength -ne $initialNetwork.PrefixLength -or
        $network.InterfaceIndex -ne $initialNetwork.InterfaceIndex) {
        throw "The private network address or adapter changed during setup. No new LAN state was created; retry after the connection is stable and the router reservation is confirmed."
    }
    $backendPort = Get-LanBackendPort

    Write-Host ""
    Write-Host "Detected private server network:" -ForegroundColor Cyan
    Write-Host ("  Address:   {0}/{1}" -f $network.ServerIPv4, $network.PrefixLength)
    Write-Host ("  Adapter:   {0}" -f $network.InterfaceAlias)
    Write-Host ("  Profile:   {0}" -f $network.NetworkCategory)
    Write-Host ""
    Write-Host "Press Enter to use the current address shown above. No router login is required."
    Write-Host "For reliable daily use, ask the router owner or technician to reserve this address for the server laptop."
    Write-Host "Never enable port forwarding, DMZ, or Internet exposure for CSD Attendance."
    if ($Graphical) {
        $choice = Show-CsdSetupChoice -Title 'Keep the server address stable' -Message (
            "This computer uses {0} on {1}. For daily use, reserve this address in your router so the other computers can keep finding it. Setup does not change the router." -f $network.ServerIPv4, $network.InterfaceAlias
        ) -Choices @(
            @{ Value='reserved'; Label='The address is already reserved'; Detail='Continue with this address for daily use.' },
            @{ Value='temporary'; Label='Use the current address'; Detail='This address is not reserved. If the router changes it, the connection will need to be set up again.' }
        )
        $reservation = switch ($choice) { 'reserved' { 'RESERVED' }; 'temporary' { '' }; default { $null } }
    }
    else {
        $reservation = Read-Host ("Type RESERVED if {0} is reserved, or press Enter to use the current address without a reservation" -f $network.ServerIPv4)
    }
    if ($null -eq $reservation) {
        $script:CsdResultCode='SETUP_CANCELLED'
        throw "No server address choice was received. Reopen START_HERE.cmd and choose option 2 again."
    }
    if (-not [string]::IsNullOrWhiteSpace($reservation) -and $reservation -cne "RESERVED") {
        throw "Type RESERVED exactly, or press Enter to use the current address without a reservation."
    }
    if ([string]::IsNullOrWhiteSpace($reservation)) {
        Write-Host "This address is not reserved. If the router later changes this laptop's address, ask your technician to refresh LAN setup." -ForegroundColor Yellow
    }

    # The operator may have changed connections while the question was open.
    $confirmedNetwork = Get-LanNetworkCandidate @networkSelection
    if ($confirmedNetwork.ServerIPv4 -ne $network.ServerIPv4 -or
        $confirmedNetwork.PrefixLength -ne $network.PrefixLength -or
        $confirmedNetwork.InterfaceIndex -ne $network.InterfaceIndex) {
        throw 'The network changed while setup was waiting. Reopen setup on the intended private network.'
    }
    Assert-LanPrivateStateIsLocal
    # Protect the directory before prepare writes its first saved binding. A
    # later interrupted setup can then read that hint without trusting inherited
    # permissions. An interruption before this point leaves no saved binding.
    $privateStateDirectory = Join-Path $script:LanRootPath '.lan-server'
    if (-not (Test-Path -LiteralPath $privateStateDirectory -PathType Container)) {
        $null = New-Item -ItemType Directory -Path $privateStateDirectory
    }
    Protect-LanPrivateState
    if ($Reconfigure) {
        Invoke-LanAddressRecovery -Action stage -Network $network -BackendPort $backendPort
        Invoke-LanAddressRecovery -Action apply
    }
    else {
        Invoke-LanNativeCommand -FilePath $script:LanVenvPythonPath -Arguments @(
            $script:LanSetupPath, "prepare", "--root", $script:LanRootPath,
            "--server-ip", $network.ServerIPv4, "--prefix-length", ([string]$network.PrefixLength),
            "--backend-port", ([string]$backendPort), "--threads", "32"
        ) -FailureMessage "The private LAN server configuration could not be prepared"
    }
    Protect-LanPrivateState
    Invoke-CaddyConfigurationCheck

    $state = Get-LanState
    $rootCertificate = Join-Path $script:LanRootPath ".lan-server\caddy-data\caddy\pki\authorities\local\root.crt"
    $rootKey = Join-Path $script:LanRootPath ".lan-server\caddy-data\caddy\pki\authorities\local\root.key"
    if (-not (Test-Path -LiteralPath $rootCertificate -PathType Leaf) -or
        -not (Test-Path -LiteralPath $rootKey -PathType Leaf)) {
        Assert-LanPortsAvailable -ServerIPv4 $network.ServerIPv4
        Set-CaddyPrivateEnvironment
        $bootstrapGateway = New-CsdContainedProcess -FilePath $script:LanCaddyPath -Arguments @(
            "run", "--config", $script:LanCaddyfilePath, "--adapter", "caddyfile"
        ) -WorkingDirectory $script:LanRootPath -NoWindow
        $bootstrapTimer = [Diagnostics.Stopwatch]::StartNew()
        while ($bootstrapTimer.Elapsed.TotalSeconds -lt 30) {
            if ($bootstrapGateway.Wait(0)) {
                throw ("The HTTPS gateway exited before generating its private CA (exit code {0})." -f $bootstrapGateway.ExitCode)
            }
            if ((Test-Path -LiteralPath $rootCertificate -PathType Leaf) -and
                (Test-Path -LiteralPath $rootKey -PathType Leaf)) {
                Start-Sleep -Milliseconds 750
                break
            }
            Start-Sleep -Milliseconds 250
        }
        if (-not (Test-Path -LiteralPath $rootCertificate -PathType Leaf) -or
            -not (Test-Path -LiteralPath $rootKey -PathType Leaf)) {
            throw "The HTTPS gateway did not generate its private LAN certificate authority."
        }
        Stop-LanBootstrapGateway -Gateway $bootstrapGateway
        $bootstrapGateway = $null
    }

    Assert-LanClientKitIsLocal
    if ($Reconfigure) {
        Invoke-LanAddressRecovery -Action verify -Network $network
    }
    else {
        Invoke-LanNativeCommand -FilePath $script:LanVenvPythonPath -Arguments @(
            $script:LanSetupPath, "finalize", "--root", $script:LanRootPath
        ) -FailureMessage "The LAN server state and public browser connection kit could not be finalized"
    }
    Protect-LanPrivateState
    $state = Get-LanState

    $certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($script:LanCertificatePath)
    $expectedSha256 = ([string]$state.rootCertificateSha256).ToLowerInvariant()
    if ((Get-CertificateSha256 -Certificate $certificate) -ne $expectedSha256 -or
        $certificate.Thumbprint -ne [string]$state.rootCertificateThumbprintSha1) {
        throw "The generated LAN root certificate does not match private server state."
    }
    $trusted = @(Get-ChildItem -LiteralPath "Cert:\CurrentUser\Root" | Where-Object {
        $_.Thumbprint -eq $certificate.Thumbprint
    })
    if ($trusted.Count -eq 0) {
        Write-Host ""
        Write-Host ("LAN CA SHA-256: {0}" -f $expectedSha256.ToUpperInvariant()) -ForegroundColor Yellow
        if ($Graphical) {
            $choice = Show-CsdSetupChoice -Title 'Allow the secure connection' -Message (
                "CSD Attendance created a private certificate authority for this installation. Trusting it lets this Windows account open the local HTTPS server. Only continue for your own installation.`r`n`r`nCertificate fingerprint (SHA-256):`r`n{0}" -f $expectedSha256.ToUpperInvariant()
            ) -Choices @(@{ Value='trust'; Label='Trust this installation'; Detail='Add this exact certificate to the current Windows account. Windows may ask you to confirm.' })
            $trustConfirmation = if ($choice -ceq 'trust') { 'TRUST' } else { $null }
        }
        else {
            $trustConfirmation = Read-Host "Type TRUST to trust this exact private CA for the server's current Windows user"
        }
        if ($trustConfirmation -cne "TRUST") {
            $script:CsdResultCode='SETUP_CANCELLED'
            throw "Server certificate trust was cancelled; Windows Firewall was not changed."
        }
        $confirmedCertificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($script:LanCertificatePath)
        try {
            if ((Get-CertificateSha256 -Certificate $confirmedCertificate) -ne $expectedSha256 -or
                $confirmedCertificate.Thumbprint -ne $certificate.Thumbprint) {
                throw 'The certificate changed while setup was waiting. Reopen setup to verify it again.'
            }
            $imported = Import-Certificate -FilePath $script:LanCertificatePath -CertStoreLocation "Cert:\CurrentUser\Root"
        }
        finally { $confirmedCertificate.Dispose() }
        if ($null -eq $imported -or $imported.Thumbprint -ne $certificate.Thumbprint) {
            throw "Windows did not import the expected LAN root certificate."
        }
    }
    Assert-ServerCertificateTrusted

    $firewallReady = $false
    try {
        Invoke-FirewallCheck
        $firewallReady = $true
    }
    catch {
        if ($script:CsdResultCode -eq 'CLEANUP_UNCONFIRMED') { throw }
        $firewallReady = $false
    }
    if (-not $firewallReady) {
        Write-Host "Windows will now request administrator approval for one narrowly scoped firewall rule."
        if ($Graphical) {
            $choice = Show-CsdSetupChoice -Title 'Allow computers on this network' -Message (
                'Windows administrator approval is needed to allow HTTPS connections to this server from its private network. This does not configure your router or enable Internet port forwarding.'
            ) -Choices @(@{ Value='allow'; Label='Continue to Windows approval'; Detail='Allow the CSD HTTPS gateway on the selected private network only.' })
            if ($choice -cne 'allow') { $script:CsdResultCode='SETUP_CANCELLED'; throw 'Network access setup was cancelled. Reopen setup to finish the firewall check.' }
        }
        $firewallAction = if ($Reconfigure) { 'Replace' } else { 'Ensure' }
        Invoke-ElevatedFirewallAction -Action $firewallAction
        Invoke-FirewallCheck
    }

    $finalNetwork = Get-LanNetworkCandidate @networkSelection
    if ($finalNetwork.ServerIPv4 -cne $network.ServerIPv4 -or
        $finalNetwork.PrefixLength -ne $network.PrefixLength -or
        $finalNetwork.InterfaceIndex -ne $network.InterfaceIndex) {
        throw 'The selected network changed before completion. LAN is not ready; reopen its recovery choices.'
    }
    if ($Reconfigure) { Invoke-LanAddressRecovery -Action commit -Network $finalNetwork }
    if (-not (Test-CsdLanReady)) { throw 'The final LAN configuration, trust or firewall check did not pass. LAN readiness is not confirmed.' }

    $setupSucceeded = $true
    } while ($false)
}
catch {
    $setupSucceeded = $false
}
finally {
    # Finish every owned cleanup attempt while the lifecycle mutex is held.
    # Publish the result only afterward: an uncertain Dispose must supersede
    # both a normal failure and an otherwise successful setup/restoration.
    if ($null -ne $certificate) {
        try { $certificate.Dispose() }
        catch { $setupSucceeded=$false; $script:CsdResultCode='CLEANUP_UNCONFIRMED' }
    }
    if ($null -ne $bootstrapGateway) {
        try { Stop-LanBootstrapGateway -Gateway $bootstrapGateway }
        catch { $setupSucceeded=$false }
    }
    if ($lifecycleMutexAcquired -and $null -ne $lifecycleMutex) {
        try { $lifecycleMutex.ReleaseMutex() }
        catch { $setupSucceeded=$false; $script:CsdResultCode='CLEANUP_UNCONFIRMED' }
    }
    if ($null -ne $lifecycleMutex) {
        try { $lifecycleMutex.Dispose() }
        catch { $setupSucceeded=$false; $script:CsdResultCode='CLEANUP_UNCONFIRMED' }
    }
}

if (-not $setupSucceeded -or $script:CsdResultCode -eq 'CLEANUP_UNCONFIRMED') {
    $public=Get-CsdPublicSetupResult
    $cleanup=if ($public.code -eq 'CLEANUP_UNCONFIRMED') { 'unknown' } else { 'confirmed' }
    $outcome=if ($cleanup -eq 'unknown' -or $public.code -eq 'SETUP_RESULT_UNKNOWN') { 'unknown' } elseif ($public.code -eq 'SETUP_CANCELLED') { 'cancelled' } else { 'failed' }
    $failureExit = Get-CsdFailureExitCode
    Write-CsdSetupResult -Outcome $outcome -Cleanup $cleanup -ExitCode $failureExit
    if ($Graphical -and $env:CSD_SETUP_RESULT_NONCE -cnotmatch '^[0-9a-f]{32}$') { Show-CsdSetupNotice -Title 'Network setup needs attention' -Message $public.message }
    Write-Host ""
    Write-Host ("LAN FIRST RUN STOPPED [{0}]: {1}" -f $public.code,$public.message) -ForegroundColor Red
    if ($cleanup -eq 'unknown') {
        Write-Host 'Keep this installation closed until your technician confirms that its contained processes stopped. Do not start another setup or server.'
    }
    else {
        Write-Host "LAN readiness is not confirmed. START_HERE.cmd option 1 remains the safe local-only path. If address recovery was interrupted, use Refresh LAN connection to finish or restore the preceding configuration."
    }
    Write-Host "This setup never configures Internet port forwarding."
    exit $failureExit
}

if ($RestoreLan) {
    Write-Host 'The preceding LAN configuration, public kit and exact firewall rule were restored. Reconnect to its preceding trusted network before starting LAN mode. Local-only mode remains available.'
}
else {
    Write-Host ""
    Write-Host "LAN SERVER SETUP COMPLETE" -ForegroundColor Green
    Write-Host ("Server URL: {0}" -f $state.publicUrl)
    Write-Host ("LAN CA SHA-256: {0}" -f ([string]$state.rootCertificateSha256).ToUpperInvariant())
    Write-Host ("Browser client kit: {0}" -f (Join-Path $script:LanRootPath "LAN_CLIENT_KIT"))
    Write-Host ""
    Write-Host "HOW TO CONNECT EACH OTHER LAPTOP" -ForegroundColor Cyan
    Write-Host "  1. Keep the CSD Attendance server window open on this laptop."
    Write-Host "  2. Copy the complete LAN_CLIENT_KIT folder to the other laptop."
    Write-Host "  3. On that laptop, open LAN_CLIENT_KIT and double-click CONNECT_TO_CSD.cmd."
    Write-Host "  4. Follow its TRUST instruction once. The browser will open automatically."
    Write-Host "Copy only LAN_CLIENT_KIT. Never copy .lan-server, .env, or database files."
}
Write-CsdSetupResult -Outcome success -ExitCode 0
exit 0
