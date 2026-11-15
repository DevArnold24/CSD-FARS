# Windows PowerShell 5.1; no machine configuration or persistent state changes.
function Assert-CsdNoPendingUpdate {
    param([Parameter(Mandatory = $true)][string]$Path)
    # Call only AFTER acquiring the installation lifecycle mutex. Otherwise a
    # stopped updater could leave a guard between the check and lock acquisition.
    $storage = Join-Path $Path '.updates'
    try { $attributes = [IO.File]::GetAttributes($storage) }
    catch [IO.FileNotFoundException] { return }
    catch [IO.DirectoryNotFoundException] { return }
    if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        ($attributes -band [IO.FileAttributes]::Directory) -eq 0) {
        throw 'Update storage is not a normal directory. Keep this installation unchanged for recovery.'
    }
    try { [void][IO.File]::GetAttributes((Join-Path $storage 'pending.json')) }
    catch [IO.FileNotFoundException] { return }
    throw 'An interrupted update needs recovery before CSD Attendance can start. Keep the installation and recovery files unchanged.'
}

function Get-RootScopedMutexName {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$Purpose = "Client-Lifecycle-v1"
    )
    $normalizedPath = [System.IO.Path]::GetFullPath($Path).TrimEnd("\").ToUpperInvariant()
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha256.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($normalizedPath))
    }
    finally { $sha256.Dispose() }
    $suffix = -join @($hashBytes | ForEach-Object { $_.ToString("x2") })
    # Global synchronizes the same installation in session 0 and interactive logons.
    return ("Global\CSD-Attendance-{0}-{1}" -f $Purpose, $suffix)
}

function New-CsdLifecycleMutex {
    param([Parameter(Mandatory = $true)][string]$Path)
    if ($PSVersionTable.PSEdition -ne "Desktop") {
        throw "Use the bundled CMD launchers or Windows PowerShell 5.1, not PowerShell 7, for the secured Windows lifecycle lock."
    }
    $ownerSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $security = [System.Security.AccessControl.MutexSecurity]::new()
    $security.SetOwner($ownerSid)
    $security.SetAccessRuleProtection($true, $false)
    $security.AddAccessRule([System.Security.AccessControl.MutexAccessRule]::new(
        $ownerSid, [System.Security.AccessControl.MutexRights]::FullControl,
        [System.Security.AccessControl.AccessControlType]::Allow
    ))
    $createdNew = $false
    $mutex = [System.Threading.Mutex]::new(
        $false, (Get-RootScopedMutexName -Path $Path), [ref]$createdNew, $security
    )
    try {
        # A pre-existing object must not silently substitute another user's lock.
        $actual = $mutex.GetAccessControl()
        $actualOwner = $actual.GetOwner([System.Security.Principal.SecurityIdentifier])
        $rules = @($actual.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
        if ($actualOwner.Value -ne $ownerSid.Value -or -not $actual.AreAccessRulesProtected -or
            $rules.Count -ne 1 -or $rules[0].IdentityReference.Value -ne $ownerSid.Value -or
            $rules[0].AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow -or
            $rules[0].MutexRights -ne [System.Security.AccessControl.MutexRights]::FullControl) {
            throw "The installation lifecycle lock has an unexpected owner or permissions. Run setup and startup as the same Windows user; do not change private-state permissions."
        }
        return $mutex
    }
    catch {
        $mutex.Dispose()
        throw
    }
}

function Invoke-CsdContainedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )
    if (-not ("Csd.Windows.ContainedProcess" -as [type])) {
        Add-Type -Path (Join-Path $PSScriptRoot "NativeLifetime.cs")
    }
    try {
        return [Csd.Windows.ContainedProcess]::Run($FilePath, $Arguments, $WorkingDirectory)
    }
    catch {
        # A native containment failure is not an ordinary server exit. Do not
        # permit automatic retry while the process tree's state is uncertain.
        $script:CsdResultCode='CLEANUP_UNCONFIRMED'
        throw
    }
}

function New-CsdContainedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [switch]$NoWindow
    )
    if (-not ("Csd.Windows.ContainedProcess" -as [type])) {
        Add-Type -Path (Join-Path $PSScriptRoot "NativeLifetime.cs")
    }
    return [Csd.Windows.ContainedProcess]::new($FilePath, $Arguments, $WorkingDirectory, [bool]$NoWindow)
}
