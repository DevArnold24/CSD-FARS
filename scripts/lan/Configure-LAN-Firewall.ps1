[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RootPath,
    [Parameter(Mandatory = $true)][ValidateSet("Ensure", "Check", "Remove", "Replace", "Restore")][string]$Action
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

function Assert-Administrator {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "This exact Windows Firewall change requires administrator approval."
    }
}

function Assert-SingleValue {
    param(
        [Parameter(Mandatory = $true)]$Actual,
        [Parameter(Mandatory = $true)][string]$Expected,
        [Parameter(Mandatory = $true)][string]$Description
    )
    $values = @($Actual | ForEach-Object { [string]$_ })
    if ($values.Count -ne 1 -or
        -not $values[0].Equals($Expected, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw ("The CSD LAN firewall rule has an unexpected {0}." -f $Description)
    }
}

function ConvertTo-Ipv4Number {
    param([Parameter(Mandatory = $true)][System.Net.IPAddress]$Address)
    $bytes = $Address.GetAddressBytes()
    if ($bytes.Count -ne 4) {
        throw "The CSD LAN firewall state requires one canonical IPv4 address."
    }
    return [uint64](
        ([uint64]$bytes[0] * 16777216) +
        ([uint64]$bytes[1] * 65536) +
        ([uint64]$bytes[2] * 256) +
        [uint64]$bytes[3]
    )
}

function Assert-CanonicalPrivateNetwork {
    param([Parameter(Mandatory = $true)]$State)
    $serverText = [string]$State.serverIpv4
    $serverAddress = $null
    if (-not [System.Net.IPAddress]::TryParse($serverText, [ref]$serverAddress) -or
        $serverAddress.GetAddressBytes().Count -ne 4 -or
        -not $serverAddress.ToString().Equals($serverText, [System.StringComparison]::Ordinal)) {
        throw "The CSD LAN firewall state has a noncanonical server IPv4 address."
    }

    $networkText = [string]$State.networkCidr
    $networkMatch = [regex]::Match($networkText, '^([^/]+)/([0-9]{1,2})$')
    if (-not $networkMatch.Success) {
        throw "The CSD LAN firewall state has an invalid private-network CIDR."
    }
    $networkAddress = $null
    $prefixLength = 0
    if (-not [System.Net.IPAddress]::TryParse($networkMatch.Groups[1].Value, [ref]$networkAddress) -or
        $networkAddress.GetAddressBytes().Count -ne 4 -or
        -not [int]::TryParse($networkMatch.Groups[2].Value, [ref]$prefixLength) -or
        $prefixLength -lt 8 -or $prefixLength -gt 30 -or
        $prefixLength -ne [int]$State.prefixLength) {
        throw "The CSD LAN firewall state has an invalid private-network CIDR."
    }

    $serverValue = ConvertTo-Ipv4Number -Address $serverAddress
    $networkValue = ConvertTo-Ipv4Number -Address $networkAddress
    $blockSize = ([uint64]1) -shl (32 - $prefixLength)
    $expectedNetworkValue = $serverValue - ($serverValue % $blockSize)
    $broadcastValue = $expectedNetworkValue + $blockSize - 1
    if ($networkValue -ne $expectedNetworkValue -or
        -not $networkAddress.ToString().Equals($networkMatch.Groups[1].Value, [System.StringComparison]::Ordinal) -or
        $serverValue -eq $expectedNetworkValue -or $serverValue -eq $broadcastValue) {
        throw "The CSD LAN firewall state does not contain one canonical usable server subnet."
    }

    $privateRanges = @(
        @([uint64]167772160, [uint64]184549375),
        @([uint64]2886729728, [uint64]2887778303),
        @([uint64]3232235520, [uint64]3232301055)
    )
    $contained = $false
    foreach ($range in $privateRanges) {
        if ($expectedNetworkValue -ge $range[0] -and $broadcastValue -le $range[1]) {
            $contained = $true
            break
        }
    }
    if (-not $contained) {
        throw "The CSD LAN firewall subnet must remain entirely inside one RFC1918 private range."
    }
}

function Assert-ExactRemoteNetwork {
    param($Actual, [string]$Expected)
    # Windows may return /255.255.255.0 for a rule created with /24.
    # Accept that exact mask only; extra ranges and broader masks still fail.
    $values = @($Actual | ForEach-Object { [string]$_ })
    if ($values.Count -eq 1 -and $values[0] -eq $Expected) { return }
    $expectedParts = $Expected.Split('/')
    if ($values.Count -eq 1 -and $expectedParts.Count -eq 2) {
        $actualParts = $values[0].Split('/')
        $prefix = 0
        if ($actualParts.Count -eq 2 -and $actualParts[0] -ceq $expectedParts[0] -and
            [int]::TryParse($expectedParts[1], [ref]$prefix) -and $prefix -ge 8 -and $prefix -le 30) {
            $mask = $null
            if ([System.Net.IPAddress]::TryParse($actualParts[1], [ref]$mask) -and
                $mask.GetAddressBytes().Count -eq 4 -and $mask.ToString() -ceq $actualParts[1]) {
                $expectedMask = [uint64]4294967296 - (([uint64]1) -shl (32 - $prefix))
                if ((ConvertTo-Ipv4Number -Address $mask) -eq $expectedMask) { return }
            }
        }
    }
    throw 'The CSD LAN firewall rule has an unexpected remote network.'
}

function Assert-ExactRule {
    param($Rule, $State, [string]$CaddyPath)
    if ($null -eq $Rule -or [string]$Rule.Direction -ne "Inbound" -or
        [string]$Rule.Action -ne "Allow" -or [string]$Rule.Enabled -ne "True" -or
        [int]$Rule.Profile -ne 3 -or [string]$Rule.EdgeTraversalPolicy -ne "Block") {
        throw "The CSD LAN firewall rule is not the exact enabled Private/Domain inbound allow rule."
    }
    $ports = @($Rule | Get-NetFirewallPortFilter)
    $addresses = @($Rule | Get-NetFirewallAddressFilter)
    $applications = @($Rule | Get-NetFirewallApplicationFilter)
    if ($ports.Count -ne 1 -or $addresses.Count -ne 1 -or $applications.Count -ne 1) {
        throw 'The CSD LAN firewall rule has ambiguous filters.'
    }
    $portFilter = $ports[0]
    $addressFilter = $addresses[0]
    $applicationFilter = $applications[0]
    if ([string]$portFilter.Protocol -notin @("TCP", "6")) {
        throw "The CSD LAN firewall rule is not restricted to TCP."
    }
    Assert-SingleValue -Actual $portFilter.LocalPort -Expected "443" -Description "local port"
    Assert-SingleValue -Actual $portFilter.RemotePort -Expected "Any" -Description "remote port"
    Assert-SingleValue -Actual $addressFilter.LocalAddress -Expected ([string]$State.serverIpv4) -Description "local address"
    Assert-ExactRemoteNetwork -Actual $addressFilter.RemoteAddress -Expected ([string]$State.networkCidr)
    $actualProgram = [System.IO.Path]::GetFullPath([string]$applicationFilter.Program)
    if (-not $actualProgram.Equals($CaddyPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The CSD LAN firewall rule is not restricted to the pinned gateway executable."
    }
}

function Assert-ReplacementPrivateInterface {
    param($State)
    $addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | Where-Object {
        [string]$_.IPAddress -ceq [string]$State.serverIpv4
    })
    if ($addresses.Count -ne 1 -or [string]$addresses[0].AddressState -ne 'Preferred' -or
        [int]$addresses[0].PrefixLength -ne [int]$State.prefixLength) {
        throw 'The selected LAN address is not uniquely active with its confirmed prefix.'
    }
    $index = [int]$addresses[0].InterfaceIndex
    $profiles = @(Get-NetConnectionProfile -InterfaceIndex $index -ErrorAction Stop)
    $networks = @(Get-NetIPConfiguration -InterfaceIndex $index -ErrorAction Stop)
    if ($profiles.Count -ne 1 -or [string]$profiles[0].NetworkCategory -notin @('Private', 'DomainAuthenticated') -or
        $networks.Count -ne 1 -or [string]$networks[0].NetAdapter.Status -ne 'Up') {
        throw 'The selected LAN connection must remain active and Private or Domain.'
    }
}

function Get-LanReplacementPreviousState {
    param([string]$Root, $State, [string]$StatePath, [switch]$Restoring)
    $journalPath = Join-Path $Root '.lan-server\reconfiguration.json'
    $journal = Get-Content -LiteralPath $journalPath -Raw | ConvertFrom-Json
    $requiredPhase = if ($Restoring) { 'rolled_back' } else { 'published' }
    if ([int]$journal.schemaVersion -ne 1 -or [string]$journal.phase -cne $requiredPhase -or
        [string]$journal.id -cnotmatch '^[0-9a-f]{32}$') {
        throw 'A complete pending LAN recovery set is required before changing its firewall address.'
    }
    $beforePath = Join-Path $Root ('.lan-server\reconfiguration\{0}\before\.lan-server\state.json' -f $journal.id)
    $afterPath = Join-Path $Root ('.lan-server\reconfiguration\{0}\after\.lan-server\state.json' -f $journal.id)
    foreach ($path in @($journalPath, $beforePath, $afterPath)) {
        $component = Get-Item -LiteralPath $path -Force
        while ($null -ne $component -and $component.FullName.Length -ge $Root.TrimEnd('\').Length) {
            if (($component.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'LAN recovery files cannot traverse a link or junction.' }
            $component = if ($component.PSIsContainer) { $component.Parent } else { $component.Directory }
        }
    }
    $entry = $journal.files.PSObject.Properties['.lan-server/state.json'].Value
    if ([string]$entry.before -cnotmatch '^[0-9a-f]{64}$' -or [string]$entry.after -cnotmatch '^[0-9a-f]{64}$' -or
        (Get-FileHash -LiteralPath $beforePath -Algorithm SHA256).Hash.ToLowerInvariant() -cne [string]$entry.before -or
        (Get-FileHash -LiteralPath $afterPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne [string]$entry.after) {
        throw 'The LAN recovery state does not match its verified inventory.'
    }
    $expectedCurrentHash = if ($Restoring) { [string]$entry.before } else { [string]$entry.after }
    if ((Get-FileHash -LiteralPath $StatePath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $expectedCurrentHash) {
        throw 'The current LAN state changed outside recovery.'
    }
    $previous = Get-Content -LiteralPath $beforePath -Raw | ConvertFrom-Json
    $candidate = Get-Content -LiteralPath $afterPath -Raw | ConvertFrom-Json
    Assert-CanonicalPrivateNetwork -State $previous
    Assert-CanonicalPrivateNetwork -State $candidate
    foreach ($field in @('schemaVersion','mode','status','httpsPort','firewallRuleName','sourceCommit',
                         'bundleVersion','caddyExecutableSha256','rootCertificateSha256')) {
        if ([string]$previous.$field -cne [string]$candidate.$field -or
            [string]$previous.$field -cne [string]$State.$field) { throw 'The preceding LAN state belongs to a different installation or trust identity.' }
    }
    if ($Restoring) { return $candidate }
    return $previous
}

function Set-ConfirmedLanFirewallAddress {
    param($Rule, $State, $Previous, [string]$CaddyPath, [string]$StatePath, [switch]$Restoring)
    if ([string]$Rule.Name -cne [string]$State.firewallRuleName -or
        [string]$Rule.PolicyStoreSourceType -cne 'Local' -or [string]$Rule.Group -cne 'CSD Attendance LAN Server') {
        throw "Only this installation's exact locally managed firewall rule can be refreshed."
    }
    try { Assert-ExactRule -Rule $Rule -State $State -CaddyPath $CaddyPath; return } catch { }
    Assert-ExactRule -Rule $Rule -State $Previous -CaddyPath $CaddyPath
    if (-not $Restoring) { Assert-ReplacementPrivateInterface -State $State }
    $stateHash = (Get-FileHash -LiteralPath $StatePath -Algorithm SHA256).Hash
    $filter = @($Rule | Get-NetFirewallAddressFilter)[0]
    $oldLocal = @($filter.LocalAddress)
    $oldRemote = @($filter.RemoteAddress)
    try {
        $null = Set-NetFirewallAddressFilter -InputObject $filter -LocalAddress ([string]$State.serverIpv4) `
            -RemoteAddress ([string]$State.networkCidr) -ErrorAction Stop
        $verified = @(Get-NetFirewallRule -Name ([string]$State.firewallRuleName) -ErrorAction Stop)
        if ($verified.Count -ne 1) { throw 'The refreshed firewall rule is missing or ambiguous.' }
        Assert-ExactRule -Rule $verified[0] -State $State -CaddyPath $CaddyPath
        if (-not $Restoring) { Assert-ReplacementPrivateInterface -State $State }
        if ((Get-FileHash -LiteralPath $StatePath -Algorithm SHA256).Hash -cne $stateHash) { throw 'The LAN configuration changed during firewall refresh.' }
    }
    catch {
        try {
            $null = Set-NetFirewallAddressFilter -InputObject $filter -LocalAddress $oldLocal -RemoteAddress $oldRemote -ErrorAction Stop
            $restored = @(Get-NetFirewallRule -Name ([string]$Previous.firewallRuleName) -ErrorAction Stop)
            if ($restored.Count -ne 1) { throw 'The preceding rule could not be found.' }
            Assert-ExactRule -Rule $restored[0] -State $Previous -CaddyPath $CaddyPath
        }
        catch { throw 'Firewall refresh failed and restoration is unconfirmed. Keep LAN stopped and ask the administrator to check its exact rule.' }
        throw 'Firewall refresh failed. Its preceding addresses were restored; LAN recovery remains incomplete.'
    }
}

try {
    $resolvedRoot = [System.IO.Path]::GetFullPath($RootPath)
    if ($resolvedRoot.StartsWith("\\", [System.StringComparison]::Ordinal)) {
        throw "The LAN server cannot use a network-share path."
    }
    $statePath = Join-Path $resolvedRoot ".lan-server\state.json"
    $caddyPath = [System.IO.Path]::GetFullPath((Join-Path $resolvedRoot "tools\caddy\caddy.exe"))
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $caddyPath -PathType Leaf)) {
        throw "The LAN server state or pinned gateway executable is missing."
    }
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    if ([int]$state.schemaVersion -ne 1 -or [string]$state.mode -ne "lan-server" -or
        [string]$state.status -ne "ready" -or [int]$state.httpsPort -ne 443 -or
        [string]$state.firewallRuleName -notmatch '^CSD-Attendance-LAN-HTTPS-[0-9a-f]{16}$') {
        throw "The LAN server state cannot authorize a firewall change."
    }
    Assert-CanonicalPrivateNetwork -State $state
    $actualCaddyHash = (Get-FileHash -LiteralPath $caddyPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualCaddyHash -ne [string]$state.caddyExecutableSha256) {
        throw "The gateway executable does not match the LAN server state."
    }
    $ruleName = [string]$state.firewallRuleName
    $rules = @(Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue)
    if ($rules.Count -gt 1) {
        throw "More than one firewall rule has the exact CSD LAN identity."
    }

    if ($Action -in @('Replace','Restore')) {
        Assert-Administrator
        if ($rules.Count -ne 1) { throw 'The preceding exact CSD rule is missing. Preserve LAN recovery and ask the administrator to review it.' }
        $restoring = $Action -eq 'Restore'
        $previous = Get-LanReplacementPreviousState -Root $resolvedRoot -State $state -StatePath $statePath -Restoring:$restoring
        Set-ConfirmedLanFirewallAddress -Rule $rules[0] -State $state -Previous $previous -CaddyPath $caddyPath -StatePath $statePath -Restoring:$restoring
        exit 0
    }

    if ($Action -eq "Ensure") {
        Assert-Administrator
        if ($rules.Count -eq 0) {
            New-NetFirewallRule -Name $ruleName `
                -DisplayName "CSD Attendance LAN HTTPS (private network only)" `
                -Group "CSD Attendance LAN Server" `
                -Direction Inbound -Action Allow -Enabled True `
                -Profile Domain,Private -EdgeTraversalPolicy Block `
                -Program $caddyPath -Protocol TCP -LocalPort 443 -RemotePort Any `
                -LocalAddress ([string]$state.serverIpv4) `
                -RemoteAddress ([string]$state.networkCidr) | Out-Null
            $rules = @(Get-NetFirewallRule -Name $ruleName -ErrorAction Stop)
        }
        if ($rules.Count -ne 1) {
            throw "Windows did not create the exact CSD LAN firewall rule."
        }
        Assert-ExactRule -Rule $rules[0] -State $state -CaddyPath $caddyPath
        exit 0
    }
    if ($Action -eq "Check") {
        if ($rules.Count -ne 1) {
            throw "The exact CSD LAN firewall rule is missing. Run LAN_SERVER_FIRST_RUN.cmd."
        }
        Assert-ExactRule -Rule $rules[0] -State $state -CaddyPath $caddyPath
        exit 0
    }

    Assert-Administrator
    if ($rules.Count -eq 0) {
        exit 0
    }
    Assert-ExactRule -Rule $rules[0] -State $state -CaddyPath $caddyPath
    Remove-NetFirewallRule -Name $ruleName
    if (@(Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue).Count -ne 0) {
        throw "Windows did not remove the exact CSD LAN firewall rule."
    }
    exit 0
}
catch {
    Write-Host ("FIREWALL ACTION FAILED: {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}
