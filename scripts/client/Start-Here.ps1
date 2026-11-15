[CmdletBinding()]
param(
    [ValidateSet("Interactive", "Local", "Lan", "RecoverLan", "RestoreLan", "Autostart", "Update")]
    [string]$Mode = "Interactive",
    [ValidateSet("Status", "Enable", "Disable", "Start", "Stop")]
    [string]$AutostartAction = "Status",
    [switch]$AutostartResultPrompt,
    [switch]$NoBrowser,
    [switch]$Graphical
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$script:RootPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$script:WindowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
. (Join-Path $PSScriptRoot '..\windows\Setup-Result.ps1')
. (Join-Path $PSScriptRoot '..\windows\Lifecycle.ps1')
$script:EntryResultNonce=$env:CSD_SETUP_RESULT_NONCE
$script:EntryResultCorrelation=$env:CSD_SETUP_CORRELATION
Initialize-CsdSetupResult -RootPath $script:RootPath -NewInvocation
if ($Graphical) { . (Join-Path $PSScriptRoot '..\windows\Setup-Dialogs.ps1') }

function Test-JsonStatus {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedStatus
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    try {
        $state = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        return [string]$state.status -eq $ExpectedStatus
    }
    catch {
        return $false
    }
}

function Test-LocalReadyHint {
    foreach ($path in @(
        (Join-Path $script:RootPath ".env"),
        (Join-Path $script:RootPath "database\attendance.db"),
        (Join-Path $script:RootPath ".venv\Scripts\python.exe")
    )) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            return $false
        }
    }
    return Test-JsonStatus `
        -Path (Join-Path $script:RootPath ".client-state") `
        -ExpectedStatus "complete"
}

function Test-LanReadyHint {
    try {
        . (Join-Path $script:RootPath 'scripts\lan\Lan-Common.ps1')
        return [bool](Test-CsdLanReady)
    }
    catch {
        return $false
    }
}

function Invoke-CsdPowerShellScript {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string[]]$Arguments = @(),
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )

    $nonce=[Guid]::NewGuid().ToString('N')
    $previousNonce=$env:CSD_SETUP_RESULT_NONCE
    $previousCorrelation=$env:CSD_SETUP_CORRELATION
    $child=$null
    try {
        $env:CSD_SETUP_RESULT_NONCE=$nonce
        $env:CSD_SETUP_CORRELATION=$script:CsdResultCorrelation
        $child=New-CsdContainedProcess -FilePath $script:WindowsPowerShell -Arguments (@('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',$Path)+$Arguments) -WorkingDirectory $script:RootPath
        $clock=[Diagnostics.Stopwatch]::StartNew()
        while (-not $child.Wait(100)) {
            if ($clock.Elapsed.TotalSeconds -ge 1800) { $script:CsdResultCode='CHILD_TIMEOUT'; throw 'The setup time limit was reached.' }
        }
        $exitCode=$child.ExitCode
    } finally {
        $env:CSD_SETUP_RESULT_NONCE=$previousNonce
        $env:CSD_SETUP_CORRELATION=$previousCorrelation
        if ($null -ne $child) { try { $child.Dispose() } catch { $script:CsdResultCode='CLEANUP_UNCONFIRMED'; throw 'Setup cleanup not confirmed.' } }
    }
    if ($exitCode -ne 0) {
        $fixedResult=Get-CsdFailureFromExitCode -ExitCode $exitCode
        if ($null -ne $fixedResult) { $script:CsdResultStage=$fixedResult.stage; $script:CsdResultCode=$fixedResult.code }
        $receipt=Read-CsdSetupResult -RootPath $script:RootPath -Nonce $nonce
        if ($null -ne $receipt -and $receipt.exit_code -eq $exitCode -and $receipt.outcome -cne 'success') {
            $script:CsdResultStage=$receipt.stage
            $script:CsdResultCode=$receipt.code
        }
        throw $FailureMessage
    }
}

function Write-LanTroubleshootingGuide {
    param([Parameter(Mandatory = $true)][string]$Problem)

    Write-Host ""
    Write-Host "LAN SETUP HELP" -ForegroundColor Yellow
    Write-Host "=============="
    Write-Host ("Problem found: {0}" -f $Problem)
    Write-Host ""
    Write-Host "Before retrying:"
    Write-Host "  1. Connect this server laptop and every other laptop to the SAME trusted network."
    Write-Host "     Use a router, or a switch with unique private IP addresses and matching subnet settings."
    Write-Host "  2. Temporarily disconnect VPNs, Tailscale, mobile hotspots, and unused network adapters."
    Write-Host "  3. Make sure this is a trusted home, office, or school network - never public Wi-Fi."
    Write-Host "  4. Reopen START_HERE.cmd, choose option 2, and follow each message on screen."
    Write-Host ""
}

function Select-GuidedLanNetwork {
    param([Parameter(Mandatory = $true)][object[]]$Networks)
    # Four connections per page leave room for Previous/Next in the shared
    # dialog. Values are inventory indexes, never text entered as an address.
    $page = 0
    while ($true) {
        if ($Graphical) {
            $choices = @()
            $first = $page * 4
            for ($index = $first; $index -lt [Math]::Min($first + 4, $Networks.Count); $index++) {
                $row = $Networks[$index]
                $label = [string]$row.InterfaceAlias
                if ($label.Length -gt 60) { $label = $label.Substring(0,57) + '...' }
                $choices += @{ Value=('network_' + $index); Label=($label + ' - ' + $row.ServerIPv4);
                    Detail=('Windows profile: {0}. Choose this connection only if it reaches the same trusted network as the joining computers.' -f $row.NetworkCategory) }
            }
            if ($page -gt 0) { $choices += @{Value='previous'; Label='Previous connections'; Detail='Show the previous page.'} }
            if ($first + 4 -lt $Networks.Count) { $choices += @{Value='next'; Label='More connections'; Detail='Show the next page.'} }
            $choice = Show-CsdSetupChoice -Title 'Choose the shared network' -Message 'This computer has more than one connected network. Choose the Ethernet or Wi-Fi connection used by the other computers. If both reach the same network, Ethernet is usually the more stable choice. A switch-only LAN needs unique private IP addresses and matching subnet settings. This does not change your IP address or routes.' -Choices $choices
            if ($choice -ceq 'next' -and $first + 4 -lt $Networks.Count) { $page++; continue }
            if ($choice -ceq 'previous' -and $page -gt 0) { $page--; continue }
            if ($choice -match '^network_(\d+)$') {
                $selected = [int]$Matches[1]
                if ($selected -ge $first -and $selected -lt [Math]::Min($first + 4, $Networks.Count)) { return $Networks[$selected] }
            }
        }
        else {
            Write-Host 'Choose the connection on the same trusted network as the joining computers:'
            for ($index = 0; $index -lt $Networks.Count; $index++) {
                $row = $Networks[$index]
                Write-Host ('  {0}. {1} - {2} ({3})' -f ($index + 1), $row.InterfaceAlias, $row.ServerIPv4, $row.NetworkCategory)
            }
            $selected = 0
            $choice = Read-Host 'Enter the connection number, or press Enter to cancel'
            if ([int]::TryParse($choice, [ref]$selected) -and $selected -ge 1 -and $selected -le $Networks.Count) { return $Networks[$selected - 1] }
        }
        throw 'Network selection was cancelled. Nothing was changed. Reopen START_HERE.cmd when ready.'
    }
}

function Get-GuidedLanNetworkCandidate {
    param([Parameter(Mandatory = $true)][bool]$Interactive,[switch]$IgnoreSavedBinding)

    $selection = @{}
    if ($Interactive -and ($IgnoreSavedBinding -or $null -eq (Get-LanSavedNetworkHint))) {
        $networks = @(Get-LanRoutedPrivateNetworks | Where-Object {
            $_.NetworkCategory -in @('Private', 'DomainAuthenticated', 'Public') -and
            $_.PrefixLength -ge 8 -and $_.PrefixLength -le 30
        })
        if ($networks.Count -gt 1) {
            $selected = Select-GuidedLanNetwork -Networks $networks
            $selection = @{ExpectedIPv4=$selected.ServerIPv4; ExpectedPrefixLength=$selected.PrefixLength; ExpectedInterfaceIndex=$selected.InterfaceIndex}
        }
    }
    if ($IgnoreSavedBinding) { $selection['IgnoreSavedBinding']=$true }
    try {
        return (Get-LanNetworkCandidate @selection)
    }
    catch {
        $problem = $_.Exception.Message
        if (-not $Interactive) {
            throw $problem
        }

        Write-LanTroubleshootingGuide -Problem $problem
        $publicNetwork = Get-LanPublicNetworkCandidate @selection
        if ($null -eq $publicNetwork) {
            throw "Follow the LAN setup help above, then retry option 2. Nothing was changed."
        }

        Write-Host "ONE-CLICK WINDOWS NETWORK FIX" -ForegroundColor Cyan
        Write-Host "Windows calls this network connection Public, which blocks LAN sharing."
        Write-Host ("Connection: {0}  Address: {1}" -f $publicNetwork.InterfaceAlias, $publicNetwork.ServerIPv4)
        Write-Host "CSD Attendance can ask Windows to mark only this connection Private."
        Write-Host "Windows will show an administrator confirmation window."
        Write-Host ""
        if ($Graphical) {
            $choice = Show-CsdSetupChoice -Title 'Use your trusted private network' -Message (
                "Windows marks {0} ({1}) as Public. If this is your trusted school, office or home network, setup can ask Windows to mark only this connection Private. Windows administrator approval is required." -f $publicNetwork.InterfaceAlias, $publicNetwork.ServerIPv4
            ) -Choices @(@{ Value='private'; Label='Make this connection Private'; Detail='Continue only on a network you trust. Do not use this option for public Wi-Fi.' })
            $confirmation = if ($choice -ceq 'private') { 'FIX' } else { $null }
        }
        else {
            $confirmation = Read-Host "If this is your trusted network, type FIX (or press Enter to stop)"
        }
        if ($confirmation -cne "FIX") {
            $script:CsdResultCode='LAN_SELECTION_CANCELLED'
            throw "Nothing was changed. You can use option 1 now, or retry option 2 when the LAN is ready."
        }

        $repairScript = Join-Path $script:RootPath "scripts\lan\Set-Trusted-LAN-Private.ps1"
        if (-not (Test-Path -LiteralPath $repairScript -PathType Leaf)) {
            throw "The LAN network-fix helper is missing. Download the complete verified client ZIP again."
        }
        $repairArguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "{0}" -InterfaceIndex {1} -ExpectedIPv4 "{2}"' -f `
            $repairScript, $publicNetwork.InterfaceIndex, $publicNetwork.ServerIPv4
        try {
            $repairProcess = Start-Process -FilePath $script:WindowsPowerShell `
                -Verb RunAs -WindowStyle Hidden -Wait -PassThru `
                -ArgumentList $repairArguments
        }
        catch {
            Write-Host "Manual alternative:" -ForegroundColor Yellow
            Write-Host "  Settings > Network & internet > Ethernet or Wi-Fi > Network profile type > Private"
            throw "Windows administrator approval was cancelled or unavailable. Nothing else was changed."
        }
        $repairExitCode = $repairProcess.ExitCode
        $repairProcess.Dispose()
        if ($repairExitCode -ne 0) {
            Write-Host "Manual alternative:" -ForegroundColor Yellow
            Write-Host "  Settings > Network & internet > Ethernet or Wi-Fi > Network profile type > Private"
            throw "Windows could not apply the one-click network fix. Nothing else was changed."
        }

        $network = Get-LanNetworkCandidate -ExpectedIPv4 $publicNetwork.ServerIPv4 -ExpectedPrefixLength $publicNetwork.PrefixLength -ExpectedInterfaceIndex $publicNetwork.InterfaceIndex -IgnoreSavedBinding:$IgnoreSavedBinding
        if ($network.InterfaceIndex -ne $publicNetwork.InterfaceIndex -or
            $network.ServerIPv4 -ne $publicNetwork.ServerIPv4) {
            throw "The active network connection changed during the fix. Reopen START_HERE.cmd and retry option 2."
        }
        Write-Host ""
        Write-Host "Windows network check fixed. Continuing LAN setup automatically..." -ForegroundColor Green
        return $network
    }
}

function Get-SavedDeploymentPreference {
    # This is a non-secret menu hint, never permission to start a server. Do not
    # read the database or .env, invoke Python, or change Windows settings here.
    $fallback = [pscustomobject]@{ Mode = "local"; Saved = $false; Warning = $null }
    $stream = $null
    try {
        $root = [System.IO.Path]::GetFullPath($script:RootPath)
        if ($root.StartsWith("\\")) { throw "A local installation is required." }
        $ancestor = $root
        while (-not [string]::IsNullOrEmpty($ancestor)) {
            $attributes = [System.IO.File]::GetAttributes($ancestor)
            if (($attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
                ($attributes -band [System.IO.FileAttributes]::Directory) -eq 0) {
                throw "The installation path is not a normal directory."
            }
            $parent = [System.IO.Directory]::GetParent($ancestor)
            $ancestor = if ($null -eq $parent) { $null } else { $parent.FullName }
        }
        $path = Join-Path $root ".deployment-preference.json"
        try { $attributes = [System.IO.File]::GetAttributes($path) }
        catch [System.IO.FileNotFoundException] { return $fallback }
        if (($attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
            ($attributes -band [System.IO.FileAttributes]::Directory) -ne 0) {
            throw "The saved preference is not a normal file."
        }
        $item = Get-Item -LiteralPath $path -Force
        if ($null -eq $item.PSObject.Properties["LinkType"] -or
            -not [string]::IsNullOrEmpty([string]$item.LinkType)) {
            throw "The saved preference cannot use a linked file."
        }
        $stream = [System.IO.File]::Open($path, [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
        # Read at most one byte beyond the limit, even if the file grows. Opening
        # without write/delete sharing also prevents replacement while reading.
        $bytes = New-Object byte[] 1025
        $count = 0
        while ($count -lt $bytes.Length) {
            $read = $stream.Read($bytes, $count, $bytes.Length - $count)
            if ($read -eq 0) { break }
            $count += $read
        }
        if ($count -eq 0 -or $count -gt 1024) { throw "The preference size is invalid." }
        $encoding = New-Object System.Text.UTF8Encoding($false, $true)
        $text = $encoding.GetString($bytes, 0, $count)
        # The tiny exact schema deliberately accepts no duplicate/extra fields,
        # JSON coercions, escaped property names, or executable input. Field
        # order and JSON whitespace are immaterial; only these two values exist.
        $pattern = '\A[ \t\r\n]*\{[ \t\r\n]*(?:"version"[ \t\r\n]*:[ \t\r\n]*1[ \t\r\n]*,[ \t\r\n]*"mode"[ \t\r\n]*:[ \t\r\n]*"(?<mode>local|lan)"|"mode"[ \t\r\n]*:[ \t\r\n]*"(?<mode>local|lan)"[ \t\r\n]*,[ \t\r\n]*"version"[ \t\r\n]*:[ \t\r\n]*1)[ \t\r\n]*\}[ \t\r\n]*\z'
        $match = [System.Text.RegularExpressions.Regex]::Match($text, $pattern)
        if (-not $match.Success) { throw "The preference schema is invalid." }
        return [pscustomobject]@{ Mode = $match.Groups["mode"].Value; Saved = $true; Warning = $null }
    }
    catch {
        $fallback.Warning = "The saved next-launch preference is unavailable or invalid. Local-only is the safe default; choose a mode below. No settings were changed."
        return $fallback
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

function Get-SelectedMode {
    if ($Mode -ne "Interactive") {
        return $Mode
    }

    $preference = Get-SavedDeploymentPreference
    $defaultChoice = if ($preference.Mode -ceq "lan") { "2" } else { "1" }
    $defaultMode = if ($preference.Mode -ceq "lan") { "Lan" } else { "Local" }
    if ($Graphical) {
        $message = "This folder runs one CSD Attendance installation. Both modes use its existing accounts and attendance.`r`n`r`nTo connect to a server that is already installed on another computer, use that server's LAN_CLIENT_KIT instead."
        if ($preference.Warning) { $message += "`r`n`r`n" + $preference.Warning }
        elseif ($preference.Saved) { $message += "`r`n`r`nSaved launch preference: " + $defaultMode + '. Choose a mode to continue.' }
        $selection = Show-CsdSetupChoice -Title 'How will you use CSD Attendance?' -Message $message -Choices @(
            @{ Value='Local'; Label='Use on this computer'; Detail='Set up or open the local system. Other computers cannot connect in this mode.' },
            @{ Value='Lan'; Label='Share with computers on this network'; Detail='Set up or open the server for your private network. Install the server on one computer only.' },
            @{ Value='RecoverLan'; Label='Review a changed server network'; Detail='Explicitly refresh this installation after its trusted adapter or address changed. Stop attendance and the server first.' },
            @{ Value='RestoreLan'; Label='Restore preceding LAN configuration'; Detail='Restore preceding LAN files and kit. If needed, Windows asks approval to restore this installation''s exact prior firewall rule. The server stays stopped; adapters and the router stay unchanged.' },
            @{ Value='Autostart'; Label='Manage automatic startup'; Detail='After network setup, check or change Windows automatic startup. This opens the local startup manager.' },
            @{ Value='Update'; Label='Update or recover'; Detail='Use a supplied update package on this server, or finish an interrupted update. Finish attendance and stop the server first.' }
        )
        if ($null -eq $selection) { return 'Cancelled' }
        return $selection
    }
    Write-Host "CSD Attendance - Start Here" -ForegroundColor Cyan
    Write-Host "==========================="
    Write-Host ""
    Write-Host "This folder runs an independent CSD Attendance installation."
    Write-Host "To join another computer's existing system, use its LAN_CLIENT_KIT instead."
    Write-Host ""
    Write-Host "1. Use CSD Attendance on this laptop only (recommended)"
    Write-Host "   Works whether or not this laptop is connected to a LAN."
    Write-Host ""
    Write-Host "2. Share CSD Attendance with other laptops on this private LAN"
    Write-Host "   Checks the network and guides you through anything that needs fixing."
    Write-Host ""
    Write-Host "3. Manage automatic LAN startup (optional, after LAN setup)"
    Write-Host "   Check status, enable, disable, start, or stop the scheduled server."
    Write-Host "   Automatic startup is never enabled by options 1 or 2."
    Write-Host ""
    Write-Host "4. Update or recover this installation"
    Write-Host "   Use a supplied update package, or finish an interrupted update."
    Write-Host "   Finish attendance and stop the server first."
    Write-Host "5. Review and refresh a changed LAN address for this server"
    Write-Host "   Stop attendance and the server first; review the selected private network."
    Write-Host "6. Restore the preceding coherent LAN configuration and client kit"
    Write-Host "   May ask Windows approval to restore this installation's exact prior firewall rule."
    Write-Host "   The server stays stopped. Adapters and the router stay unchanged."
    Write-Host ""
    Write-Host "Fresh computer: use a clean Windows Client Bundle, not a copied installed folder."
    Write-Host "First setup needs internet for its private Python runtime; see PORTABLE_QUICK_START.md."
    if ($preference.Warning) {
        Write-Host $preference.Warning -ForegroundColor Yellow
    }
    elseif ($preference.Saved) {
        Write-Host ("Saved next-launch preference: option {0} ({1}). Confirm below; nothing starts automatically." -f $defaultChoice, $defaultMode) -ForegroundColor Cyan
    }
    else {
        Write-Host "No saved next-launch preference. Enter confirms local-only mode."
    }
    Write-Host "Both modes use this installation's SAME accounts and attendance database."
    Write-Host "Stop the running server before switching modes."
    Write-Host "Choosing local-only does NOT disable an existing automatic LAN startup task."
    Write-Host "Use option 3 locally to Stop and Disable that task before switching to local-only."
    Write-Host ""
    $rawSelection = Read-Host ("Press Enter to confirm option {0}, or choose 1 local-only, 2 LAN, 3 automatic startup, 4 update/recovery, 5 changed LAN address, or 6 restore preceding LAN configuration" -f $defaultChoice)
    if ($null -eq $rawSelection) {
        $script:CsdResultCode='SELECTION_REQUIRED'
        throw "No option was selected. Reopen START_HERE.cmd and choose option 1, 2, 3, or 4."
    }
    $selection = $rawSelection.Trim()
    if ([string]::IsNullOrWhiteSpace($selection)) {
        return $defaultMode
    }
    if ($selection -eq "1") {
        return "Local"
    }
    if ($selection -eq "2") {
        return "Lan"
    }
    if ($selection -eq "3") {
        return "Autostart"
    }
    if ($selection -eq "4") {
        return "Update"
    }
    if ($selection -eq '5') { return 'RecoverLan' }
    if ($selection -eq '6') { return 'RestoreLan' }
    $script:CsdResultCode='SELECTION_REQUIRED'
    throw "Choose option 1, 2, 3, 4, 5, or 6. No setup or server changes were made."
}

function Invoke-GuidedUpdate {
    # The updater alone verifies setup, publisher trust, pending recovery and
    # the lifecycle lock. This branch never enters First Run or LAN setup.
    $updater = Join-Path $PSScriptRoot 'Update-CSD.ps1'
    if (-not (Test-Path -LiteralPath $updater -PathType Leaf)) {
        throw 'The update window is missing. Keep this installation unchanged and ask the system owner for help.'
    }
    & $script:WindowsPowerShell -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File $updater | Out-Host
    $updateExitCode = $LASTEXITCODE
    if ($updateExitCode -eq 2) {
        Write-Host 'Update window closed. Reopen Update or recover when you are ready.'
        return 0
    }
    if ($updateExitCode -ne 0) {
        # Its own window already explains the refusal or recovery result.
        Write-Host 'The update did not complete. Read the updater result above.'
        return 1
    }
    return 0
}

function Invoke-GuidedAutostart {
    param([Parameter(Mandatory = $true)][bool]$Interactive)
    $script:CsdResultStage='autostart'

    # This path never invokes First Run, network repair, trust, or firewall setup.
    # The manager performs its authoritative checks before each requested action.
    if (-not (Test-LocalReadyHint)) {
        throw "Automatic startup needs completed LAN setup. Choose option 2 first. No setup or scheduled-task changes were made."
    }
    try { $null = Assert-LanPrivateStateAcl } catch { $script:CsdResultCode='PRIVATE_STATE_ACCESS'; throw }
    if (-not (Test-JsonStatus `
        -Path (Join-Path $script:RootPath ".lan-server\state.json") `
        -ExpectedStatus "ready")) {
        throw "Automatic startup needs completed LAN setup. Choose option 2 first. No setup or scheduled-task changes were made."
    }

    $managerPath = Join-Path $script:RootPath "scripts\lan\Manage-CSD-Autostart.ps1"
    if (-not (Test-Path -LiteralPath $managerPath -PathType Leaf)) {
        throw "The automatic-startup manager is missing. Ask the system owner for the complete verified Windows Client Bundle."
    }
    $action = $AutostartAction
    if ($Interactive) {
        Write-Host ""
        Write-Host "Optional automatic LAN startup" -ForegroundColor Cyan
        Write-Host "=============================="
        Write-Host "1. Show status (recommended; changes nothing)"
        Write-Host "2. Enable automatic startup"
        Write-Host "3. Disable automatic startup (does not stop a running server)"
        Write-Host "4. Start the configured server task now"
        Write-Host "5. Stop the server task now (does not disable future startup)"
        Write-Host "0. Exit without changes"
        Write-Host ""
        Write-Host "Enable requires Windows administrator approval and the installation owner's Windows password."
        Write-Host "Enter that password only in the local Windows prompt, never in chat."
        Write-Host "It is not your CSD Attendance password or Windows Hello PIN."
        Write-Host "Stop the foreground LAN server before Enable. Enable schedules the next boot; it does not start the server now."
        if ($Graphical) {
            $choice = Show-CsdSetupChoice -Title 'Automatic startup' -Message (
                'Automatic startup lets this server run after Windows boots. Stop the foreground server before enabling it. Enable asks for Windows administrator approval and the installation owner''s Windows password in a local window.'
            ) -Choices @(
                @{ Value='status'; Label='Show status'; Detail='Inspect the existing startup setting without changing it.' },
                @{ Value='enable'; Label='Enable automatic startup'; Detail='Schedule startup for the next boot. This does not start the server now.' },
                @{ Value='disable'; Label='Disable automatic startup'; Detail='Prevent future automatic starts. A running server stays running.' },
                @{ Value='start'; Label='Start the configured server'; Detail='Ask Windows to start this installation now.' },
                @{ Value='stop'; Label='Stop the configured server'; Detail='Finish active attendance first. This stops this installation''s scheduled server, and keeps the automatic startup setting.' }
            )
            $rawAction = switch ($choice) { 'status' { '1' }; 'enable' { '2' }; 'disable' { '3' }; 'start' { '4' }; 'stop' { '5' }; default { '0' } }
        }
        else {
            $rawAction = Read-Host "Press Enter for status, or choose 2-5 (0 to exit)"
        }
        if ($null -eq $rawAction) {
            throw "No automatic-startup action was selected. No scheduled-task changes were made."
        }
        switch ($rawAction.Trim()) {
            "" { $action = "Status" }
            "1" { $action = "Status" }
            "2" { $action = "Enable" }
            "3" { $action = "Disable" }
            "4" { $action = "Start" }
            "5" { $action = "Stop" }
            "0" { return }
            default { throw "Choose status, one of actions 2-5, or 0 to exit. No scheduled-task changes were made." }
        }
    }
    if ($action -eq "Enable" -and -not $AutostartResultPrompt) {
        # Elevation follows only an explicit Enable choice. Re-enter this
        # dispatcher so the elevated console can retain the manager's result.
        # The manager itself still verifies the same installation owner.
        $relayPath = Join-Path $PSScriptRoot "Start-Here.ps1"
        $relayArguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "{0}" -Mode Autostart -AutostartAction Enable -AutostartResultPrompt' -f $relayPath
        if ($Graphical) { $relayArguments += ' -Graphical' }
        try {
            $enableProcess = Start-Process -FilePath $script:WindowsPowerShell `
                -Verb RunAs -WindowStyle Normal -Wait -PassThru -ArgumentList $relayArguments
        }
        catch {
            $script:CsdResultCode='AUTOSTART_APPROVAL'
            throw "Windows administrator approval was cancelled or unavailable. Automatic startup was not enabled by this launcher."
        }
        try {
            if ($enableProcess.ExitCode -ne 0) {
                $script:CsdResultCode='AUTOSTART_ENABLE_FAILED'
                throw ("Automatic startup could not be enabled (exit code {0}). Use the error shown in the separate Windows window; keep the installation unchanged." -f $enableProcess.ExitCode)
            }
        }
        finally {
            $enableProcess.Dispose()
        }
        return
    }
    Invoke-CsdPowerShellScript -Path $managerPath -Arguments @("-Action", $action) `
        -FailureMessage "The requested automatic-startup action did not complete"
    if ($AutostartResultPrompt) {
        Write-Host "Enable schedules automatic startup for the next Windows boot; use Start in the manager to test it now." -ForegroundColor Green
        if ($Graphical) {
            Show-CsdSetupNotice -Title 'Automatic startup enabled' -Message 'Startup is scheduled for the next Windows boot. Use Start in the startup manager to test it now. Keep the server on power and awake.'
        }
        else { [void](Read-Host "Press Enter to close this result window") }
    }
}

try {
    if (-not (Test-Path -LiteralPath $script:WindowsPowerShell -PathType Leaf)) {
        throw "Windows PowerShell is unavailable. Contact your technician or IT administrator."
    }
    Set-Location -LiteralPath $script:RootPath
    $interactiveSelection = $Mode -eq "Interactive"
    $selectedMode = Get-SelectedMode
    if ($selectedMode -eq 'Cancelled') {
        $script:CsdResultCode='SETUP_CANCELLED'
        Write-CsdSetupResult -Outcome cancelled -ExitCode 0
        exit 0
    }
    if ($selectedMode -eq 'Update') {
        $updateResult = Invoke-GuidedUpdate
        exit $updateResult
    }
    $setupArguments = @()
    if ($Graphical) { $setupArguments += '-Graphical' }
    if ($selectedMode -eq 'RestoreLan') {
        $script:CsdResultStage='lan_setup'
        Invoke-CsdPowerShellScript -Path (Join-Path $script:RootPath 'scripts\lan\First-Run-LAN.ps1') -Arguments ($setupArguments + @('-RestoreLan')) -FailureMessage 'LAN configuration restoration did not complete'
        Write-Host 'The preceding LAN files were restored. Reopen START_HERE.cmd to check readiness before starting.'
        exit 0
    }

    if ($selectedMode -eq "Local") {
        $script:CsdResultStage='administrator'
        if (-not (Test-LocalReadyHint)) {
            Write-Host ""
            Write-Host "First use detected. Preparing this laptop automatically..." -ForegroundColor Cyan
            Invoke-CsdPowerShellScript `
                -Path (Join-Path $PSScriptRoot "First-Run.ps1") `
                -Arguments $setupArguments `
                -FailureMessage "This laptop's first-use setup did not complete"
        }

        Write-Host ""
        Write-Host "Starting private local mode. Other laptops cannot connect." -ForegroundColor Green
        $script:CsdResultStage='startup'
        $startParameters = @{}
        if ($NoBrowser) { $startParameters["NoBrowser"] = $true }
        # Run the long-lived server in this PowerShell host so Ctrl+C and
        # closing the console retain the original launcher's stop behavior.
        $env:CSD_SETUP_RESULT_NONCE=$script:CsdResultNonce
        & (Join-Path $PSScriptRoot "Start-CSD.ps1") @startParameters
        $serverExitCode = $LASTEXITCODE
        if ($serverExitCode -ne 0) {
            $fixedResult=Get-CsdFailureFromExitCode -ExitCode $serverExitCode
            if ($null -ne $fixedResult) { $script:CsdResultStage=$fixedResult.stage; $script:CsdResultCode=$fixedResult.code }
            $receipt=Read-CsdSetupResult -RootPath $script:RootPath -Nonce $script:CsdResultNonce
            if ($null -ne $receipt -and $receipt.exit_code -eq $serverExitCode -and $receipt.outcome -cne 'success') { $script:CsdResultStage=$receipt.stage; $script:CsdResultCode=$receipt.code }
            throw ("Local CSD Attendance stopped because of an error (exit code {0})." -f $serverExitCode)
        }
        exit 0
    }

    . (Join-Path $script:RootPath "scripts\lan\Lan-Common.ps1")
    if ($selectedMode -eq "Autostart") {
        Invoke-GuidedAutostart -Interactive $interactiveSelection
        exit 0
    }
    $script:CsdResultStage='lan_setup'
    try {
        $network = Get-GuidedLanNetworkCandidate -Interactive ($interactiveSelection -or [bool]$Graphical) -IgnoreSavedBinding:($selectedMode -eq 'RecoverLan')
    }
    catch {
        throw ("LAN sharing is not available yet. {0} Reopen START_HERE.cmd and choose option 1 to use this laptop privately instead." -f $_.Exception.Message)
    }

    Write-Host ""
    Write-Host ("Private LAN detected on {0}." -f $network.InterfaceAlias) -ForegroundColor Cyan
    if ($selectedMode -eq 'RecoverLan' -or -not (Test-LocalReadyHint) -or -not (Test-LanReadyHint)) {
        if ($script:CsdResultCode -eq 'CLEANUP_UNCONFIRMED') { throw 'Previous readiness child cleanup is unconfirmed.' }
        $script:CsdResultCode=$null
        Write-Host "Preparing private LAN sharing..."
        $lanSetupArguments = $setupArguments + @(
            '-ExpectedIPv4', $network.ServerIPv4, '-ExpectedPrefixLength', ([string]$network.PrefixLength),
            '-ExpectedInterfaceIndex', ([string]$network.InterfaceIndex)
        )
        if ($selectedMode -eq 'RecoverLan') { $lanSetupArguments += '-Reconfigure' }
        Invoke-CsdPowerShellScript `
            -Path (Join-Path $script:RootPath "scripts\lan\First-Run-LAN.ps1") `
            -Arguments $lanSetupArguments `
            -FailureMessage "Private LAN setup did not complete"
    }

    $lanParameters = @{}
    if ($script:CsdResultCode -eq 'CLEANUP_UNCONFIRMED') { throw 'Previous readiness child cleanup is unconfirmed.' }
    $script:CsdResultStage='lan_startup'
    if ($NoBrowser) { $lanParameters["NoBrowser"] = $true }
    # Keep the Caddy/application supervisor attached directly to this console
    # for the same predictable Ctrl+C and window-close behavior.
    & (Join-Path $script:RootPath "scripts\lan\Start-CSD-LAN.ps1") @lanParameters
    $serverExitCode = $LASTEXITCODE
    if ($serverExitCode -ne 0) {
        $failure=Get-CsdFailureFromExitCode -ExitCode $serverExitCode
        if ($null -ne $failure) { $script:CsdResultStage=$failure.stage; $script:CsdResultCode=$failure.code }
        throw ("The private LAN server stopped because of an error (exit code {0})." -f $serverExitCode)
    }
    exit 0
}
catch {
    $public=Get-CsdPublicSetupResult
    $outcome=if ($public.code -in @('SETUP_CANCELLED','LAN_SELECTION_CANCELLED')) { 'cancelled' } elseif ($public.code -in @('SETUP_RESULT_UNKNOWN','CLEANUP_UNCONFIRMED')) { 'unknown' } else { 'failed' }
    $cleanup=if ($public.code -eq 'CLEANUP_UNCONFIRMED') { 'unknown' } else { 'confirmed' }
    Write-CsdSetupResult -Outcome $outcome -Cleanup $cleanup
    if ($Graphical) { Show-CsdSetupNotice -Title 'Setup or startup needs attention' -Message $public.message }
    Write-Host ""
    Write-Host ("START HERE STOPPED [{0}]: {1}" -f $public.code,$public.message) -ForegroundColor Red
    if ($public.code -eq 'CLIENT_STORAGE_ACCESS' -and $Mode -eq 'Interactive') {
        . (Join-Path $PSScriptRoot '..\windows\Client-Storage.ps1')
        . (Join-Path $PSScriptRoot 'Prepare-Install-Destination.ps1')
        Invoke-CsdInstallDestinationPrompt
    }
    if ($AutostartResultPrompt -and -not $Graphical) {
        [void](Read-Host "Read the error above, then press Enter to close this result window")
    }
    exit 1
}
finally {
    $env:CSD_SETUP_RESULT_NONCE=$script:EntryResultNonce
    $env:CSD_SETUP_CORRELATION=$script:EntryResultCorrelation
}
