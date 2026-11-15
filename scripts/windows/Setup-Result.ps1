# Fixed public result contract. Never persist raw output, exception text or paths.
. (Join-Path $PSScriptRoot 'Private-Storage.ps1')

function Initialize-CsdSetupResult {
    param([Parameter(Mandatory=$true)][string]$RootPath, [string]$Stage='launcher', [switch]$NewInvocation)
    $script:CsdResultRoot = $RootPath
    $script:CsdSuppressPrivacyReceipt = $false
    $script:CsdResultStage = $Stage
    $script:CsdResultCode = $null
    $script:CsdResultNonce = if (-not $NewInvocation -and $env:CSD_SETUP_RESULT_NONCE -cmatch '^[0-9a-f]{32}$') { $env:CSD_SETUP_RESULT_NONCE } else { [Guid]::NewGuid().ToString('N') }
    $script:CsdResultCorrelation = if (-not $NewInvocation -and $env:CSD_SETUP_CORRELATION -cmatch '^[0-9a-f]{32}$') { $env:CSD_SETUP_CORRELATION } else { [Guid]::NewGuid().ToString('N') }
    $script:CsdResultRevision = 'unknown'
    # Only the fixed public revision, never arbitrary manifest values.
    try {
        $null=Assert-CsdNormalLocalDirectory -Path $RootPath
        $manifest = Join-Path $RootPath 'CLIENT_BUNDLE_MANIFEST.json'
        $item=Get-Item -LiteralPath $manifest -Force -ErrorAction Stop
        if (-not $item.PSIsContainer -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) {
            $stream=[IO.File]::Open($manifest,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
            try {
                if ($stream.Length -gt 65536) { throw 'Invalid public manifest size.' }
                $reader=[IO.StreamReader]::new($stream)
                try { $metadata=$reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
            } finally { $stream.Dispose() }
            if ($metadata.sourceCommit -is [string] -and $metadata.sourceCommit -cmatch '^[0-9a-f]{40}$') { $script:CsdResultRevision = $metadata.sourceCommit }
        }
    } catch { }
}

function Get-CsdPublicSetupResult {
    param([string]$Stage=$script:CsdResultStage, [string]$Code=$script:CsdResultCode)
    $catalog = @{
        platform = @('WINDOWS_ARCHITECTURE', 'Use supported Windows x64 and Windows PowerShell x64. Ask your technician for the correct system.', 'technician', $false)
        media = @('WINDOWS_MEDIA', 'Required Windows media components are missing. Ask IT to install the official Microsoft Media Feature Pack; keep device policy enabled.', 'technician', $false)
        location = @('INSTALL_LOCATION', 'Setup cannot write safely here. Use a complete bundle in an owner-writable, normal local NTFS folder with at least 6 GB free, outside synchronized or removable storage.', 'new_destination', $true)
        lock = @('INSTALLATION_BUSY', 'This installation is already running or has unfinished recovery. Close its running windows and finish pending recovery before retrying.', 'close_or_recover', $true)
        bundle = @('BUNDLE_INTEGRITY', 'A required bundle file or checksum is invalid. Obtain a complete verified Windows Client Bundle from the system owner.', 'verified_bundle', $true)
        runtime_identity = @('PRIVATE_PYTHON_INVALID', 'The private Python runtime is invalid. Keep initialized data unchanged and ask your technician to recover the runtime or use a separate fresh destination.', 'technician', $false)
        python_download = @('PYTHON_PREPARATION', 'The pinned private Python runtime could not be prepared within its time limit. First setup requires the documented internet download. Retry with the complete verified bundle when available.', 'retry_setup', $true)
        dependencies = @('OFFLINE_DEPENDENCIES', 'Verified offline dependency installation or its single repair did not complete. Keep saved progress and obtain the complete verified bundle before retrying.', 'verified_bundle', $true)
        runtime_imports = @('RUNTIME_IMPORTS', 'The client runtime did not confirm that all required modules loaded. Setup stopped before creating accounts or data. Retry with the complete verified bundle or ask your technician.', 'verified_bundle', $true)
        administrator = @('ADMINISTRATOR_SETUP', 'Administrator setup did not complete. Reopen setup to check saved progress and finish the account. Existing data was not replaced.', 'retry_setup', $true)
        state_check = @('INSTALLATION_STATE', 'Saved installation state did not pass validation. Keep the folder unchanged and ask your technician; do not reset its database or keys.', 'technician', $false)
        startup = @('LOCAL_STARTUP', 'The local server could not start or stopped unexpectedly. Close other windows for this installation, then retry. Keep the folder unchanged if the error persists.', 'retry_start', $true)
        lan_setup = @('LAN_SETUP', 'Private LAN setup did not complete. Check the selected trusted private network and use the explicit network recovery option if its address changed. Reopen START_HERE.cmd and choose option 1 to use this laptop privately instead.', 'review_network', $true)
        lan_startup = @('LAN_STARTUP', 'The private LAN server could not start. Check this installation with the network setup/recovery option before retrying.', 'review_network', $true)
        autostart = @('AUTOSTART_ACTION', 'Automatic startup did not complete. Choose option 2 first to finish LAN setup. If Enable was selected, Windows administrator approval must complete in its separate window.', 'review_network', $true)
        launcher = @('LAUNCHER_UNKNOWN', 'Setup or startup could not be confirmed. Keep the installation unchanged, reopen the launcher, and ask your technician if it repeats.', 'technician', $false)
    }
    if (-not $catalog.ContainsKey($Stage)) { $Stage = 'launcher' }
    $entry = $catalog[$Stage]
    if ($Code -eq 'CHILD_TIMEOUT') { $entry = @('CHILD_TIMEOUT', 'This setup stage exceeded its time limit and was stopped. Reopen setup to check saved progress before retrying.', 'retry_setup', $true) }
    if ($Code -eq 'CHILD_OUTPUT_LIMIT') { $entry = @('CHILD_OUTPUT_LIMIT', 'This setup stage produced an invalid result and was stopped. Keep saved progress and ask your technician.', 'technician', $false) }
    if ($Code -eq 'CLEANUP_UNCONFIRMED') { $entry = @('CLEANUP_UNCONFIRMED', 'Windows did not confirm that this setup child stopped. Keep this installation closed until your technician confirms cleanup.', 'technician', $false) }
    if ($Code -eq 'SETUP_CANCELLED') { $entry = @('SETUP_CANCELLED', 'Setup was closed. Reopen setup to check or finish saved progress.', 'retry_setup', $true) }
    if ($Code -eq 'SETUP_RESULT_UNKNOWN') { $entry = @('SETUP_RESULT_UNKNOWN', 'The setup result could not be confirmed. Keep saved progress and reopen setup to check it before repeating account creation.', 'retry_setup', $true) }
    if ($Code -eq 'SELECTION_REQUIRED') { $entry = @('SELECTION_REQUIRED', 'No option was selected or the choice was invalid. Choose option 1, 2, 3, 4, 5, or 6. Nothing was changed.', 'retry_setup', $true) }
    if ($Code -eq 'LAN_SELECTION_CANCELLED') { $entry = @('LAN_SELECTION_CANCELLED', 'Network selection or Windows approval was cancelled. Nothing was changed by this selection. Reopen START_HERE.cmd and choose option 1 to use this laptop privately instead.', 'retry_setup', $true) }
    if ($Code -eq 'AUTOSTART_APPROVAL') { $entry = @('AUTOSTART_APPROVAL', 'Windows administrator approval was cancelled or unavailable. Automatic startup was not enabled by this launcher.', 'retry_setup', $true) }
    if ($Code -eq 'AUTOSTART_ENABLE_FAILED') { $entry = @('AUTOSTART_ENABLE_FAILED', 'Automatic startup could not be enabled. Read the result in its separate Windows window and keep the installation unchanged.', 'technician', $false) }
    if ($Code -eq 'LAN_HTTPS_PORT_BUSY') { $entry = @('LAN_HTTPS_PORT_BUSY', 'The secure web port (TCP 443) is already in use on the selected server address. Close another running CSD Attendance server, or ask your technician to resolve the conflicting service, then retry. Do not disable the firewall.', 'resolve_port_conflict', $true) }
    if ($Code -eq 'LAN_BACKEND_PORT_BUSY') { $entry = @('LAN_BACKEND_PORT_BUSY', 'The private loopback application port is already in use. Close another running CSD Attendance server, or ask your technician to resolve the conflicting service, then retry. Keep saved setup unchanged.', 'resolve_port_conflict', $true) }
    if ($Code -eq 'LAN_PORT_CHECK_FAILED') { $entry = @('LAN_PORT_CHECK_FAILED', 'Windows could not confirm which local TCP ports are available. Keep saved setup unchanged and ask your technician before retrying. No network settings were changed by this check.', 'technician', $false) }
    if ($Code -eq 'CLIENT_STORAGE_ACCESS') { $entry = @('CLIENT_STORAGE_ACCESS', 'Private installation permissions could not be verified. Keep existing files unchanged and ask your technician for an owner-private local destination. No permissions were changed.', 'technician', $false) }
    if ($Code -eq 'PRIVATE_STATE_ACCESS') { $entry = @('PRIVATE_STATE_ACCESS', 'The private LAN state permissions are unsafe or unexpected. Keep the installation unchanged and ask your technician; do not loosen its permissions.', 'technician', $false) }
    return [pscustomobject]@{ stage=$Stage; code=$entry[0]; message=$entry[1]; next_action=$entry[2]; retryable=[bool]$entry[3] }
}

function Write-CsdSetupResult {
    param([ValidateSet('success','failed','cancelled','unknown')][string]$Outcome,
          [ValidateSet('confirmed','pending','unknown')][string]$Cleanup='confirmed', [int]$ExitCode=1)
    if ($script:CsdSuppressPrivacyReceipt -or $script:CsdResultCode -eq 'CLIENT_STORAGE_ACCESS') { return }
    $public = Get-CsdPublicSetupResult
    $result = [ordered]@{ version=1; nonce=$script:CsdResultNonce; correlation=$script:CsdResultCorrelation;
        source_revision=$script:CsdResultRevision; stage=$public.stage; code=$public.code;
        outcome=$Outcome; retryable=$public.retryable; next_action=$public.next_action;
        exit_code=$ExitCode; cleanup=$Cleanup }
    if ($Outcome -eq 'success') { $result.code='COMPLETE'; $result.next_action='none'; $result.retryable=$false }
    # Diagnostic storage is optional. Failure must never change the true outcome
    # or fall back to another location. Latest only; no log export or upload.
    $temporary = $null
    try {
        $directory = Join-Path $script:CsdResultRoot '.support'
        New-CsdPrivateDirectory -Path $directory
        Assert-CsdPrivateTree -Path $directory -MaximumEntries 16
        $destination = Join-Path $directory 'latest-result.json'
        $temporary = Join-Path $directory ([Guid]::NewGuid().ToString('N') + '.tmp')
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($result | ConvertTo-Json -Compress))
        if ($bytes.Length -gt 16384) { return }
        $stream = [IO.File]::Open($temporary,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try { $stream.Write($bytes,0,$bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
        if ([IO.File]::Exists($destination)) { [IO.File]::Replace($temporary,$destination,[NullString]::Value) }
        else { [IO.File]::Move($temporary,$destination) }
    } catch { }
    finally { if ($temporary -and [IO.File]::Exists($temporary)) { try { [IO.File]::Delete($temporary) } catch { } } }
}

# Child exit status carries only fixed stage/event identifiers. This keeps the
# specific result available even when optional receipt storage fails or another
# invocation legitimately replaces the one latest receipt. No success is ever
# inferred from this failure channel, and older/unrecognized codes stay unknown.
function Get-CsdFailureExitCode {
    param([string]$Stage=$script:CsdResultStage,[string]$Code=$script:CsdResultCode)
    $stages=@('platform','media','location','lock','bundle','runtime_identity','python_download','dependencies','runtime_imports','administrator','state_check','startup','lan_setup','lan_startup','autostart','launcher')
    $events=@('default','CHILD_TIMEOUT','CHILD_OUTPUT_LIMIT','CLEANUP_UNCONFIRMED','SETUP_CANCELLED','SETUP_RESULT_UNKNOWN','SELECTION_REQUIRED','LAN_SELECTION_CANCELLED','AUTOSTART_APPROVAL','AUTOSTART_ENABLE_FAILED','PRIVATE_STATE_ACCESS','CLIENT_STORAGE_ACCESS','LAN_HTTPS_PORT_BUSY','LAN_BACKEND_PORT_BUSY','LAN_PORT_CHECK_FAILED')
    $stageIndex=[Array]::IndexOf($stages,$Stage)
    if ($stageIndex -lt 0) { $stageIndex=15 }
    $eventIndex=[Array]::IndexOf($events,$Code)
    if ($eventIndex -lt 0) { $eventIndex=0 }
    return 64 + $stageIndex*16 + $eventIndex
}

function Get-CsdFailureFromExitCode {
    param([int]$ExitCode)
    $stages=@('platform','media','location','lock','bundle','runtime_identity','python_download','dependencies','runtime_imports','administrator','state_check','startup','lan_setup','lan_startup','autostart','launcher')
    $events=@('default','CHILD_TIMEOUT','CHILD_OUTPUT_LIMIT','CLEANUP_UNCONFIRMED','SETUP_CANCELLED','SETUP_RESULT_UNKNOWN','SELECTION_REQUIRED','LAN_SELECTION_CANCELLED','AUTOSTART_APPROVAL','AUTOSTART_ENABLE_FAILED','PRIVATE_STATE_ACCESS','CLIENT_STORAGE_ACCESS','LAN_HTTPS_PORT_BUSY','LAN_BACKEND_PORT_BUSY','LAN_PORT_CHECK_FAILED')
    $value=$ExitCode-64
    $stageIndex=[int][Math]::Floor($value/16)
    $eventIndex=$value%16
    if ($stageIndex -lt 0 -or $stageIndex -ge $stages.Count -or $eventIndex -lt 0 -or $eventIndex -ge $events.Count) { return $null }
    return Get-CsdPublicSetupResult -Stage $stages[$stageIndex] -Code $events[$eventIndex]
}

function Read-CsdSetupResult {
    param([Parameter(Mandatory=$true)][string]$RootPath, [Parameter(Mandatory=$true)][string]$Nonce)
    try {
        $directory=Join-Path $RootPath '.support'
        Assert-CsdPrivateTree -Path $directory -MaximumEntries 16
        $path=Join-Path $directory 'latest-result.json'
        $stream=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
        try {
            if ($stream.Length -eq 0 -or $stream.Length -gt 16384) { return $null }
            $reader=[IO.StreamReader]::new($stream,[Text.UTF8Encoding]::new($false,$true))
            try { $document=$reader.ReadToEnd(); $result=$document | ConvertFrom-Json } finally { $reader.Dispose() }
        } finally { $stream.Dispose() }
        # Reject duplicate fields and JSON coercions normalized by the parser.
        if (($result | ConvertTo-Json -Compress) -cne $document) { return $null }
        $fields=@('version','nonce','correlation','source_revision','stage','code','outcome','retryable','next_action','exit_code','cleanup')
        foreach ($field in @('nonce','correlation','source_revision','stage','code','outcome','next_action','cleanup')) {
            if ($result.$field -isnot [string]) { return $null }
        }
        if (@($result.PSObject.Properties).Count -ne $fields.Count -or
            @($result.PSObject.Properties.Name | Where-Object { $_ -cnotin $fields }).Count -ne 0 -or
            $result.version -isnot [int] -or $result.version -cne 1 -or $result.nonce -cne $Nonce -or $result.nonce -cnotmatch '^[0-9a-f]{32}$' -or
            $result.correlation -cnotmatch '^[0-9a-f]{32}$' -or $result.source_revision -cnotmatch '^(unknown|[0-9a-f]{40})$' -or
            $result.outcome -cnotin @('success','failed','cancelled','unknown') -or
            $result.cleanup -cnotin @('confirmed','pending','unknown') -or $result.retryable -isnot [bool] -or
            $result.exit_code -isnot [int]) { return $null }
        $public=Get-CsdPublicSetupResult -Stage $result.stage -Code $result.code
        if ($public.stage -cne $result.stage) { return $null }
        if ($result.outcome -ceq 'success') {
            if ($result.code -cne 'COMPLETE' -or $result.next_action -cne 'none' -or $result.retryable -or $result.exit_code -ne 0 -or $result.cleanup -cne 'confirmed') { return $null }
        } elseif ($public.code -cne $result.code -or $public.next_action -cne $result.next_action -or $public.retryable -ne $result.retryable) { return $null }
        return $result
    } catch { return $null }
}

function New-CsdCapturedSetupProcess {
    param([string]$FilePath, [AllowEmptyString()][string[]]$Arguments, [string]$WorkingDirectory,
          [AllowEmptyString()][string]$InputText='', [ValidateRange(1024,65536)][int]$MaximumOutput=16384)
    if (-not ('Csd.Windows.ContainedProcess' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'NativeLifetime.cs') }
    return [Csd.Windows.ContainedProcess]::new($FilePath,$Arguments,$WorkingDirectory,$InputText,$MaximumOutput)
}

function Invoke-CsdBoundedSetupProcess {
    param([Parameter(Mandatory=$true)][string]$FilePath, [AllowEmptyString()][string[]]$Arguments,
          [Parameter(Mandatory=$true)][string]$WorkingDirectory,
          [ValidateRange(1,1800)][int]$TimeoutSeconds=120, [AllowEmptyString()][string]$InputText='',
          [ValidateRange(1024,65536)][int]$MaximumOutput=16384)
    $child=$null
    $clock=[Diagnostics.Stopwatch]::StartNew()
    try {
        $child=New-CsdCapturedSetupProcess -FilePath $FilePath -Arguments $Arguments -WorkingDirectory $WorkingDirectory -InputText $InputText -MaximumOutput $MaximumOutput
        while (-not ($child.Wait(50) -and $child.OutputComplete)) {
            if ($child.OutputExceeded) { $script:CsdResultCode='CHILD_OUTPUT_LIMIT'; throw 'Setup child output limit exceeded.' }
            if ($clock.Elapsed.TotalSeconds -ge $TimeoutSeconds) { $script:CsdResultCode='CHILD_TIMEOUT'; throw 'Setup child deadline exceeded.' }
        }
        if ($child.OutputExceeded) { $script:CsdResultCode='CHILD_OUTPUT_LIMIT'; throw 'Setup child output limit exceeded.' }
        return [pscustomobject]@{ ExitCode=$child.ExitCode; Output=$child.Output; ErrorOutput=$child.ErrorOutput }
    } finally {
        if ($null -ne $child) {
            try { $child.Dispose() } catch { $script:CsdResultCode='CLEANUP_UNCONFIRMED'; throw 'Windows did not confirm setup child cleanup.' }
        }
    }
}
