Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "..\windows\Lifecycle.ps1")
. (Join-Path $PSScriptRoot "..\windows\Client-Storage.ps1")
. (Join-Path $PSScriptRoot "..\windows\Setup-Result.ps1")

$script:LanRootPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$script:LanSetupPath = Join-Path $PSScriptRoot "lan_setup.py"
$script:LanServerPath = Join-Path $PSScriptRoot "lan_server.py"
$script:LanStatePath = Join-Path $script:LanRootPath ".lan-server\state.json"
$script:LanCaddyfilePath = Join-Path $script:LanRootPath ".lan-server\Caddyfile"
$script:LanCaddyPath = Join-Path $script:LanRootPath "tools\caddy\caddy.exe"
$script:LanCertificatePath = Join-Path $script:LanRootPath "LAN_CLIENT_KIT\CSD_Attendance_LAN_CA.cer"
$script:LanVenvPythonPath = Join-Path $script:LanRootPath ".venv\Scripts\python.exe"

function Invoke-LanNativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$FailureMessage,
        [int]$TimeoutSeconds = 120,
        [switch]$Interactive
    )
    if ($Interactive) {
        $child = New-CsdContainedProcess -FilePath $FilePath -Arguments $Arguments -WorkingDirectory $script:LanRootPath
        try {
            $timer = [Diagnostics.Stopwatch]::StartNew()
            while (-not $child.Wait(100)) {
                if ($timer.Elapsed.TotalSeconds -ge $TimeoutSeconds) { throw 'The LAN setup stage timed out. Reopen setup to retry.' }
            }
            if ($child.ExitCode -ne 0) {
                $failure = Get-CsdFailureFromExitCode -ExitCode $child.ExitCode
                if ($null -ne $failure) { $script:CsdResultStage=$failure.stage; $script:CsdResultCode=$failure.code }
                throw ("{0} (exit code {1})." -f $FailureMessage, $child.ExitCode)
            }
        }
        finally {
            try { $child.Dispose() } catch { $script:CsdResultCode='CLEANUP_UNCONFIRMED'; throw }
        }
        return
    }
    $result = Invoke-CsdBoundedSetupProcess -FilePath $FilePath -Arguments $Arguments `
        -WorkingDirectory $script:LanRootPath -TimeoutSeconds $TimeoutSeconds -MaximumOutput 65536
    if ($result.ExitCode -ne 0) {
        $failure = Get-CsdFailureFromExitCode -ExitCode $result.ExitCode
        if ($null -ne $failure) { $script:CsdResultStage=$failure.stage; $script:CsdResultCode=$failure.code }
        throw ("{0} (exit code {1})." -f $FailureMessage, $result.ExitCode)
    }
}

# Match the exact IPv4 listeners used by Caddy and Waitress, not a machine-wide
# port number. A wildcard still conflicts. Treat an IPv6 wildcard conservatively
# because GetActiveTcpListeners does not report whether the socket is dual-stack.
function Get-LanTcpListeners {
    return [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
}

function Test-LanTcpEndpointConflict {
    param(
        [Parameter(Mandatory=$true)][System.Net.IPEndPoint]$Listener,
        [Parameter(Mandatory=$true)][System.Net.IPAddress]$Address,
        [Parameter(Mandatory=$true)][ValidateRange(1,65535)][int]$Port
    )
    if ($Address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
        $Address.Equals([System.Net.IPAddress]::Any)) {
        throw 'The LAN port preflight requires an exact IPv4 address.'
    }
    if ($Listener.Port -ne $Port) { return $false }
    $boundAddress = $Listener.Address
    if ($boundAddress.Equals([System.Net.IPAddress]::IPv6Any)) { return $true }
    if ($boundAddress.IsIPv4MappedToIPv6) { $boundAddress = $boundAddress.MapToIPv4() }
    return ($boundAddress.Equals([System.Net.IPAddress]::Any) -or $boundAddress.Equals($Address))
}

function Assert-LanPortsAvailable {
    param(
        [Parameter(Mandatory=$true)][System.Net.IPAddress]$ServerIPv4,
        [ValidateRange(1,65535)][int]$BackendPort
    )
    $ErrorActionPreference = 'Stop'
    if ($ServerIPv4.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
        $ServerIPv4.Equals([System.Net.IPAddress]::Any)) {
        $script:CsdResultCode = 'LAN_PORT_CHECK_FAILED'
        throw 'The LAN port preflight requires an exact IPv4 address.'
    }
    try {
        $listeners = @(Get-LanTcpListeners)
        # An unavailable or malformed snapshot must not be treated as free ports.
        foreach ($listener in $listeners) {
            if ($listener -isnot [System.Net.IPEndPoint]) { throw 'Invalid listener snapshot.' }
        }
    }
    catch {
        $script:CsdResultCode = 'LAN_PORT_CHECK_FAILED'
        throw 'Windows could not confirm local TCP listener availability.'
    }
    foreach ($listener in $listeners) {
        if (Test-LanTcpEndpointConflict -Listener $listener -Address $ServerIPv4 -Port 443) {
            $script:CsdResultCode = 'LAN_HTTPS_PORT_BUSY'
            throw 'The HTTPS port is in use on the selected server address.'
        }
    }
    if ($PSBoundParameters.ContainsKey('BackendPort')) {
        foreach ($listener in $listeners) {
            if (Test-LanTcpEndpointConflict -Listener $listener -Address ([System.Net.IPAddress]::Loopback) -Port $BackendPort) {
                $script:CsdResultCode = 'LAN_BACKEND_PORT_BUSY'
                throw 'The loopback application port is in use.'
            }
        }
    }
    # This is only a preflight snapshot. Caddy/Waitress still own the final bind;
    # a later bind failure must remain a startup failure, never a success receipt.
}

function Test-Rfc1918Address {
    param([Parameter(Mandatory = $true)][System.Net.IPAddress]$Address)
    $bytes = $Address.GetAddressBytes()
    if ($bytes.Count -ne 4) { return $false }
    return (
        $bytes[0] -eq 10 -or
        ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or
        ($bytes[0] -eq 192 -and $bytes[1] -eq 168)
    )
}

function Assert-LanRootLocation {
    if ($script:LanRootPath.StartsWith("\\", [System.StringComparison]::Ordinal) -or
        $script:LanRootPath -match '(?i)\\OneDrive(?:\s*-\s*[^\\]+)?(?:\\|$)') {
        throw "The LAN server must be kept in a normal folder on a fixed local drive, outside OneDrive."
    }
    $rootItem = Get-Item -LiteralPath $script:LanRootPath -Force
    while ($null -ne $rootItem) {
        if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "The LAN server cannot run through a symbolic link or junction."
        }
        $rootItem = $rootItem.Parent
    }
    $drive = [System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($script:LanRootPath))
    if (-not $drive.IsReady -or $drive.DriveType -ne [System.IO.DriveType]::Fixed -or
        -not $drive.DriveFormat.Equals("NTFS", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The LAN server must be kept on a fixed local NTFS drive."
    }
}

function Assert-LanNormalLocalTree {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Description
    )
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $rootItem = Get-Item -LiteralPath $Path -Force
    if (-not $rootItem.PSIsContainer -or
        ($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw ("{0} must be a normal local directory, not a file, symbolic link, or junction." -f $Description)
    }
    $reparsePoints = @(
        Get-ChildItem -LiteralPath $Path -Force -Recurse | Where-Object {
            ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
        }
    )
    if ($reparsePoints.Count -ne 0) {
        throw ("{0} cannot contain symbolic links or junctions: {1}" -f $Description, $reparsePoints[0].FullName)
    }
}

function Assert-LanPrivateStateIsLocal {
    Assert-LanNormalLocalTree `
        -Path (Join-Path $script:LanRootPath ".lan-server") `
        -Description "Private LAN state"
}

function Assert-LanClientKitIsLocal {
    Assert-LanNormalLocalTree `
        -Path (Join-Path $script:LanRootPath "LAN_CLIENT_KIT") `
        -Description "The LAN browser connection kit"
}

function New-LanRestrictedAcl {
    param([Parameter(Mandatory = $true)][bool]$IsDirectory)
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor `
        [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    if (-not $IsDirectory) {
        $inheritance = [System.Security.AccessControl.InheritanceFlags]::None
    }
    $propagation = [System.Security.AccessControl.PropagationFlags]::None
    $access = [System.Security.AccessControl.FileSystemRights]::FullControl
    $allow = [System.Security.AccessControl.AccessControlType]::Allow
    $acl = if ($IsDirectory) {
        [System.Security.AccessControl.DirectorySecurity]::new()
    }
    else {
        [System.Security.AccessControl.FileSecurity]::new()
    }
    $acl.SetAccessRuleProtection($true, $false)
    $ownerSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl.SetOwner($ownerSid)
    foreach ($sidValue in @(
        $ownerSid.Value,
        "S-1-5-18",
        "S-1-5-32-544"
    )) {
        $sid = [System.Security.Principal.SecurityIdentifier]::new($sidValue)
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid, $access, $inheritance, $propagation, $allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    return $acl
}

function Assert-LanPrivateStateAcl {
    $privatePath = Join-Path $script:LanRootPath ".lan-server"
    Assert-LanPrivateStateIsLocal
    if (-not (Test-Path -LiteralPath $privatePath -PathType Container)) {
        throw "The private LAN state directory was not created."
    }
    $allowedSids = @(
        [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value,
        "S-1-5-18",
        "S-1-5-32-544"
    )
    $items = @((Get-Item -LiteralPath $privatePath -Force)) + @(
        Get-ChildItem -LiteralPath $privatePath -Force -Recurse
    )
    foreach ($item in $items) {
        $acl = Get-Acl -LiteralPath $item.FullName
        try {
            $ownerSid = $acl.GetOwner(
                [System.Security.Principal.SecurityIdentifier]
            ).Value
        }
        catch {
            throw ("The private LAN state has an unresolvable owner: {0}" -f $item.FullName)
        }
        if ($ownerSid -notin $allowedSids) {
            throw ("The private LAN state has an unexpected owner: {0}" -f $item.FullName)
        }
        if ($item.FullName.Equals($privatePath, [System.StringComparison]::OrdinalIgnoreCase) -and
            -not $acl.AreAccessRulesProtected) {
            throw "The private LAN state root still inherits permissions from its parent folder."
        }
        $fullControlSids = @{}
        foreach ($rule in @($acl.Access)) {
            try {
                $sid = $rule.IdentityReference.Translate(
                    [System.Security.Principal.SecurityIdentifier]
                ).Value
            }
            catch {
                throw ("The private LAN state has an unresolvable access rule: {0}" -f $item.FullName)
            }
            if ($sid -notin $allowedSids -or
                $rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) {
                throw ("The private LAN state has an unexpected access rule: {0}" -f $item.FullName)
            }
            $fullControl = [System.Security.AccessControl.FileSystemRights]::FullControl
            if (($rule.FileSystemRights -band $fullControl) -eq $fullControl) {
                $fullControlSids[$sid] = $true
            }
        }
        foreach ($sid in $allowedSids) {
            if (-not $fullControlSids.ContainsKey($sid)) {
                throw ("The private LAN state lacks an expected full-control rule: {0}" -f $item.FullName)
            }
        }
    }
}

function Protect-LanPrivateState {
    $privatePath = Join-Path $script:LanRootPath ".lan-server"
    Assert-LanPrivateStateIsLocal
    if (-not (Test-Path -LiteralPath $privatePath -PathType Container)) {
        throw "The private LAN state directory was not created."
    }
    $rootAcl = New-LanRestrictedAcl -IsDirectory $true
    # Persist only the owner/access sections changed above. Set-Acl may request
    # audit (SACL) access on an already protected directory, which requires
    # SeSecurityPrivilege even though this operation does not change auditing.
    ([System.IO.DirectoryInfo]::new($privatePath)).SetAccessControl($rootAcl)
    foreach ($item in @(Get-ChildItem -LiteralPath $privatePath -Force -Recurse)) {
        $itemAcl = New-LanRestrictedAcl -IsDirectory ([bool]$item.PSIsContainer)
        $item.SetAccessControl($itemAcl)
    }
    Assert-LanPrivateStateAcl
}

function Get-LanRoutedPrivateNetworks {
    # Historical name retained for launcher callers. Same-subnet traffic does
    # not need a default gateway. The default cmdlet scope still excludes
    # virtual/disconnected interfaces; no address or route is changed here.
    if ($null -eq (Get-Command Get-NetIPConfiguration -ErrorAction SilentlyContinue) -or
        $null -eq (Get-Command Get-NetConnectionProfile -ErrorAction SilentlyContinue)) {
        throw "Windows networking commands are unavailable. Contact your technician."
    }
    $routedPrivateAddresses = @()
    foreach ($configuration in @(Get-NetIPConfiguration)) {
        if ($null -eq $configuration.NetAdapter -or $configuration.NetAdapter.Status -ne "Up") {
            continue
        }
        $profiles = @(Get-NetConnectionProfile -InterfaceIndex $configuration.InterfaceIndex -ErrorAction SilentlyContinue)
        $networkCategory = if ($profiles.Count -eq 1) {
            [string]$profiles[0].NetworkCategory
        }
        else {
            "Unknown"
        }
        foreach ($addressRecord in @($configuration.IPv4Address)) {
            $address = $null
            $prefix = 0
            if ($null -eq $addressRecord -or
                -not [System.Net.IPAddress]::TryParse([string]$addressRecord.IPAddress, [ref]$address) -or
                -not (Test-Rfc1918Address -Address $address) -or
                -not [int]::TryParse([string]$addressRecord.PrefixLength, [ref]$prefix) -or
                $prefix -lt 8 -or $prefix -gt 30) {
                continue
            }
            $routedPrivateAddresses += [pscustomobject]@{
                ServerIPv4 = $address.ToString()
                PrefixLength = $prefix
                InterfaceIndex = [int]$configuration.InterfaceIndex
                InterfaceAlias = [string]$configuration.InterfaceAlias
                NetworkCategory = $networkCategory
            }
        }
    }
    return @($routedPrivateAddresses | Sort-Object ServerIPv4, PrefixLength, InterfaceIndex -Unique)
}

function Get-LanSavedNetworkHint {
    if (-not (Test-Path -LiteralPath $script:LanStatePath)) { return $null }
    # A saved address selects only an existing adapter. The authoritative LAN,
    # certificate and firewall checks still run before the server can start.
    Assert-LanPrivateStateIsLocal
    Assert-LanPrivateStateAcl
    try {
        $state = Get-LanState
        $address = $null
        $prefix = 0
        if ($state.status -notin @('prepared', 'ready') -or
            -not [System.Net.IPAddress]::TryParse([string]$state.serverIPv4, [ref]$address) -or
            -not (Test-Rfc1918Address -Address $address) -or
            $address.ToString() -cne [string]$state.serverIPv4 -or
            -not [int]::TryParse([string]$state.prefixLength, [ref]$prefix) -or
            $prefix -lt 8 -or $prefix -gt 30) {
            throw 'Invalid saved network hint.'
        }
        return [pscustomobject]@{ ServerIPv4 = $address.ToString(); PrefixLength = $prefix }
    }
    catch {
        throw 'The saved LAN network could not be read. Keep this installation unchanged and ask the system owner to check its LAN setup.'
    }
}

function Get-LanSelectedNetworks {
    param([string]$ExpectedIPv4 = '', [int]$ExpectedPrefixLength = 0, [int]$ExpectedInterfaceIndex = 0, [switch]$IgnoreSavedBinding)
    $routedNetworks = @(Get-LanRoutedPrivateNetworks)
    # Only the explicit refresh workflow can choose a replacement binding.
    $savedNetwork = if ($IgnoreSavedBinding) { $null } else { Get-LanSavedNetworkHint }
    if ($null -ne $savedNetwork) {
        $routedNetworks = @($routedNetworks | Where-Object {
            $_.ServerIPv4 -ceq $savedNetwork.ServerIPv4 -and
            $_.PrefixLength -eq $savedNetwork.PrefixLength
        })
        if ($routedNetworks.Count -ne 1) {
            throw 'The saved server connection is unavailable, changed, or duplicated on more than one adapter. Reconnect to its trusted network and retry. LAN setup must be checked before using a different address or network.'
        }
    }
    if ($ExpectedIPv4 -ne '' -or $ExpectedPrefixLength -ne 0 -or $ExpectedInterfaceIndex -ne 0) {
        $address = $null
        if (-not [Net.IPAddress]::TryParse($ExpectedIPv4, [ref]$address) -or
            -not (Test-Rfc1918Address -Address $address) -or $address.ToString() -cne $ExpectedIPv4 -or
            $ExpectedPrefixLength -lt 8 -or $ExpectedPrefixLength -gt 30 -or $ExpectedInterfaceIndex -lt 1) {
            throw 'The selected network details are incomplete or invalid. Reopen START_HERE.cmd to choose the connection again.'
        }
        $sameAddress = @($routedNetworks | Where-Object { $_.ServerIPv4 -ceq $ExpectedIPv4 })
        if ($sameAddress.Count -gt 1) {
            throw 'The selected server address is duplicated on more than one connection. Disconnect the unused connection and reopen START_HERE.cmd.'
        }
        $routedNetworks = @($routedNetworks | Where-Object {
            $_.ServerIPv4 -ceq $ExpectedIPv4 -and $_.PrefixLength -eq $ExpectedPrefixLength -and
            $_.InterfaceIndex -eq $ExpectedInterfaceIndex
        })
        if ($routedNetworks.Count -ne 1) {
            throw 'The selected connection changed or does not match this installation. Reconnect to the chosen network and reopen START_HERE.cmd. No other network was selected.'
        }
    }
    return $routedNetworks
}

function Get-LanPublicNetworkCandidate {
    param([string]$ExpectedIPv4 = '', [int]$ExpectedPrefixLength = 0, [int]$ExpectedInterfaceIndex = 0, [switch]$IgnoreSavedBinding)
    $routedNetworks = @(Get-LanSelectedNetworks @PSBoundParameters)
    if ($routedNetworks.Count -eq 1 -and
        $routedNetworks[0].NetworkCategory -eq "Public") {
        return $routedNetworks[0]
    }
    return $null
}

function Get-LanNetworkCandidate {
    param([string]$ExpectedIPv4 = '', [int]$ExpectedPrefixLength = 0, [int]$ExpectedInterfaceIndex = 0, [switch]$IgnoreSavedBinding)
    $routedPrivateAddresses = @(Get-LanSelectedNetworks @PSBoundParameters)
    $candidates = @($routedPrivateAddresses | Where-Object {
        $_.NetworkCategory -in @("Private", "DomainAuthenticated")
    })
    if ($candidates.Count -eq 0) {
        $publicNetworks = @($routedPrivateAddresses | Where-Object {
            $_.NetworkCategory -eq "Public"
        } | Sort-Object InterfaceAlias -Unique)
        if ($publicNetworks.Count -gt 0) {
            $aliases = ($publicNetworks | ForEach-Object { $_.InterfaceAlias }) -join ", "
            throw ("Windows currently marks the selected private-address network as Public ({0}). START_HERE can guide you through changing this trusted network connection to Private." -f $aliases)
        }
        throw "No active private IPv4 network with a supported subnet was found. Connect this laptop to the same trusted router or switch as the other laptops. A switch-only LAN needs unique private IP addresses and matching subnet settings configured by your technician; no default gateway is required for same-subnet connections. Local-only mode still works without a LAN."
    }
    if ($candidates.Count -ne 1) {
        $summary = ($candidates | ForEach-Object {
            "{0}/{1} on {2}" -f $_.ServerIPv4, $_.PrefixLength, $_.InterfaceAlias
        }) -join "; "
        throw ("More than one eligible private network is active ({0}). Open START_HERE.cmd to choose the connection shared with the other computers." -f $summary)
    }
    return $candidates[0]
}

function Get-LanState {
    if (-not (Test-Path -LiteralPath $script:LanStatePath -PathType Leaf)) {
        throw "LAN server state is missing. Run LAN_SERVER_FIRST_RUN.cmd."
    }
    return Get-Content -LiteralPath $script:LanStatePath -Raw | ConvertFrom-Json
}

function Get-LanBackendPort {
    Assert-CsdClientPrivateStorage -Path $script:LanRootPath
    $envPath = Join-Path $script:LanRootPath ".env"
    $code = "from dotenv import dotenv_values; import sys; print(dotenv_values(sys.argv[1]).get('FLASK_PORT') or '5000')"
    $portText = (& $script:LanVenvPythonPath -c $code $envPath | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "The loopback application port could not be read from the private environment."
    }
    $port = 0
    if (-not [int]::TryParse($portText, [ref]$port) -or $port -lt 1024 -or $port -gt 65535 -or $port -eq 443) {
        throw "The private FLASK_PORT must be 1024-65535 and cannot be 443."
    }
    return $port
}

function Invoke-StandaloneClientCheck {
    $checkScript = Join-Path $script:LanRootPath "scripts\client\Check-CSD.ps1"
    $windowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    Invoke-LanNativeCommand -FilePath $windowsPowerShell -Arguments @(
        "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $checkScript
    ) -FailureMessage "The base client installation check failed"
}

function Complete-CsdLanUpdate {
    # The calling launcher must already own the secured lifecycle mutex. Keep
    # the child in its job so closing this window cannot outlive that lock.
    Assert-CsdNoPendingUpdate -Path $script:LanRootPath
    Assert-LanRootLocation
    Assert-LanPrivateStateIsLocal
    Assert-LanClientKitIsLocal
    if (Test-Path -LiteralPath (Join-Path $script:LanRootPath '.lan-server')) {
        Assert-LanPrivateStateAcl
    }
    $parent = [Diagnostics.Process]::GetCurrentProcess()
    $created = $parent.StartTime.ToUniversalTime().ToFileTimeUtc()
    $child = New-CsdContainedProcess -FilePath $script:LanVenvPythonPath -Arguments @(
        '-I', '-B', (Join-Path $script:LanRootPath 'scripts\client\complete_lan_update.py'),
        '--root', $script:LanRootPath, '--parent-pid', [string]$PID,
        '--parent-created', $created.ToString([Globalization.CultureInfo]::InvariantCulture)
    ) -WorkingDirectory $script:LanRootPath -NoWindow
    try {
        $timer = [Diagnostics.Stopwatch]::StartNew()
        while (-not $child.Wait(250)) {
            if ($timer.Elapsed.TotalSeconds -ge 120) { throw 'The LAN update check timed out. Keep the installation unchanged and reopen its launcher.' }
        }
        if ($child.ExitCode -ne 0) {
            throw 'The LAN update check could not finish. Keep the installation and recovery files unchanged, then reopen the usual launcher.'
        }
    }
    finally {
        try { $child.Dispose() } catch { $script:CsdResultCode='CLEANUP_UNCONFIRMED'; throw }
    }
}

function Set-CaddyPrivateEnvironment {
    $env:XDG_DATA_HOME = Join-Path $script:LanRootPath ".lan-server\caddy-data"
    $env:XDG_CONFIG_HOME = Join-Path $script:LanRootPath ".lan-server\caddy-config"
    foreach ($proxyVariable in @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")) {
        [Environment]::SetEnvironmentVariable($proxyVariable, $null, [EnvironmentVariableTarget]::Process)
    }
}

function Invoke-CaddyConfigurationCheck {
    if (-not (Test-Path -LiteralPath $script:LanCaddyPath -PathType Leaf)) {
        throw "The pinned HTTPS gateway is missing. Download the complete verified Windows Client Bundle."
    }
    Set-CaddyPrivateEnvironment
    Invoke-LanNativeCommand -FilePath $script:LanCaddyPath -Arguments @(
        "validate", "--config", $script:LanCaddyfilePath, "--adapter", "caddyfile"
    ) -FailureMessage "The pinned HTTPS gateway rejected its generated configuration"
}

function Get-CertificateSha256 {
    param([Parameter(Mandatory = $true)][System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        return (-join @($sha256.ComputeHash($Certificate.RawData) | ForEach-Object { $_.ToString("x2") }))
    }
    finally {
        $sha256.Dispose()
    }
}

function Assert-ServerCertificateTrusted {
    $state = Get-LanState
    $expectedThumbprint = ([string]$state.rootCertificateThumbprintSha1).ToUpperInvariant()
    $expectedSha256 = ([string]$state.rootCertificateSha256).ToLowerInvariant()
    if ($expectedThumbprint -notmatch '^[0-9A-F]{40}$' -or $expectedSha256 -notmatch '^[0-9a-f]{64}$') {
        throw "The LAN root-certificate identity is missing from server state."
    }
    $installed = @(Get-ChildItem -LiteralPath "Cert:\CurrentUser\Root" | Where-Object {
        $_.Thumbprint -eq $expectedThumbprint
    })
    if ($installed.Count -ne 1 -or
        (Get-CertificateSha256 -Certificate $installed[0]) -ne $expectedSha256) {
        throw "The exact LAN root certificate is not trusted for this Windows user. Run LAN_SERVER_FIRST_RUN.cmd."
    }
}

function Invoke-ElevatedFirewallAction {
    param([Parameter(Mandatory = $true)][ValidateSet("Ensure", "Remove", "Replace", "Restore")][string]$Action)
    $firewallScript = Join-Path $PSScriptRoot "Configure-LAN-Firewall.ps1"
    $windowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "{0}" -RootPath "{1}" -Action {2}' -f `
        $firewallScript, $script:LanRootPath, $Action
    $process = Start-Process -FilePath $windowsPowerShell -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -Wait -PassThru
    if ($null -eq $process -or $process.ExitCode -ne 0) {
        throw ("The administrator-only Windows Firewall action '{0}' did not complete." -f $Action)
    }
}

function Invoke-FirewallCheck {
    $firewallScript = Join-Path $PSScriptRoot "Configure-LAN-Firewall.ps1"
    $windowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    Invoke-LanNativeCommand -FilePath $windowsPowerShell -Arguments @(
        "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $firewallScript,
        "-RootPath", $script:LanRootPath, "-Action", "Check"
    ) -FailureMessage "The exact LAN Windows Firewall rule check failed"
}

function Invoke-LanAddressRecovery {
    param([Parameter(Mandatory = $true)][ValidateSet('stage','apply','verify','commit','rollback')][string]$Action,
          $Network = $null, [int]$BackendPort = 5000)
    $arguments = @('-I', '-B', (Join-Path $PSScriptRoot 'lan_reconfigure.py'), $Action, '--root', $script:LanRootPath)
    if ($null -ne $Network) {
        $arguments += @('--server-ip', [string]$Network.ServerIPv4, '--prefix-length', [string]$Network.PrefixLength,
                        '--backend-port', [string]$BackendPort)
    }
    Invoke-LanNativeCommand -FilePath $script:LanVenvPythonPath -Arguments $arguments `
        -FailureMessage 'LAN address recovery is incomplete; keep its recovery set and retry or restore the preceding configuration'
}

function Test-CsdLanReady {
    param([switch]$AllowReconfiguration)
    try {
        Assert-LanRootLocation
        Assert-LanPrivateStateIsLocal
        Assert-LanClientKitIsLocal
        Assert-LanPrivateStateAcl
        $network = Get-LanNetworkCandidate
        if ($AllowReconfiguration) {
            Invoke-LanAddressRecovery -Action verify -Network $network
        }
        else {
            Invoke-LanNativeCommand -FilePath $script:LanVenvPythonPath -Arguments @(
                '-I', '-B', $script:LanSetupPath, 'check', '--root', $script:LanRootPath,
                '--server-ip', [string]$network.ServerIPv4, '--prefix-length', [string]$network.PrefixLength
            ) -FailureMessage 'LAN configuration, certificate or connection kit needs attention'
        }
        Invoke-CaddyConfigurationCheck
        Assert-ServerCertificateTrusted
        Invoke-FirewallCheck
        return $true
    }
    catch {
        $cleanupCode = Get-Variable -Scope Script -Name CsdResultCode -ErrorAction SilentlyContinue
        if ($null -ne $cleanupCode -and $cleanupCode.Value -ceq 'CLEANUP_UNCONFIRMED') { throw }
        return $false
    }
}
