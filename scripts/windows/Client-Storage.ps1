# Read-only live-storage gate. This file never grants access or repairs an ACL.
[CmdletBinding()]
param([Alias('Root')][string]$CsdClientStorageRoot = '')

. (Join-Path $PSScriptRoot 'Private-Storage.ps1')

function Assert-CsdClientStorageDescriptor {
    param([Parameter(Mandatory=$true)]$Acl,
          [Parameter(Mandatory=$true)][string]$OwnerSid,
          [switch]$Directory)
    $allowed = @($OwnerSid, 'S-1-5-18', 'S-1-5-32-544')
    $actualOwner = $Acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($actualOwner -notin $allowed -or ($Directory -and $actualOwner -ne $OwnerSid)) {
        throw 'The installation owner does not match the current Windows account.'
    }
    $rules = @($Acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
    if ($rules.Count -ne 3) { throw 'Live storage requires owner, SYSTEM and Administrators access only.' }
    $inheritance = if ($Directory) {
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
    } else { [Security.AccessControl.InheritanceFlags]::None }
    $seen = @{}
    foreach ($rule in $rules) {
        $sid = $rule.IdentityReference.Value
        if ($sid -notin $allowed -or $seen.ContainsKey($sid) -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $rule.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl -or
            $rule.InheritanceFlags -ne $inheritance -or
            $rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None) {
            throw 'Live storage has unexpected access or inheritance rules.'
        }
        $seen[$sid] = $true
    }
    # Both explicit protected and inherited owner-private ACLs are supported.
    # Directory inheritance must protect every newly created staging/SQLite file.
}

function Assert-CsdClientAncestorDescriptor {
    param([Parameter(Mandatory=$true)]$Acl,
          [Parameter(Mandatory=$true)][string]$OwnerSid,
          [switch]$VolumeRoot)
    # TrustedInstaller may own/protect an OS ancestor, never a live-data directory.
    $allowed = @($OwnerSid, 'S-1-5-18', 'S-1-5-32-544', 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
    if ($Acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $allowed) {
        throw 'An installation ancestor is owned by another account.'
    }
    $raw = [Security.AccessControl.RawSecurityDescriptor]::new($Acl.GetSecurityDescriptorBinaryForm(),0)
    if ($null -eq $raw.DiscretionaryAcl) { throw 'An installation ancestor has an unrestricted DACL.' }
    # Another account must not replace an ancestor, delete a protected child,
    # or grant itself access. Read/traverse and creation of siblings are allowed.
    $unsafe = if ($VolumeRoot) { [int64]0x100C0040 } else { [int64]0x100D0040 }
    foreach ($rule in $Acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
            $rule.IdentityReference.Value -notin $allowed -and
            ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -eq 0 -and
            (([int64]$rule.FileSystemRights -band 0xFFFFFFFFL) -band $unsafe) -ne 0) {
            throw 'An installation ancestor can be replaced or reconfigured by another account.'
        }
    }
}

function Assert-CsdClientStorageEntry {
    param([Parameter(Mandatory=$true)][string]$Path,
          [Parameter(Mandatory=$true)][string]$OwnerSid,
          [switch]$Directory)
    # GetAttributes/GetAccessControl throw on denial. Never interpret denial as absence.
    $attributes = [IO.File]::GetAttributes($Path)
    $isDirectory = ($attributes -band [IO.FileAttributes]::Directory) -ne 0
    if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $isDirectory -ne [bool]$Directory) {
        throw 'Live storage must be a normal local file or directory, not a link or junction.'
    }
    $item = if ($Directory) { [IO.DirectoryInfo]::new($Path) } else { [IO.FileInfo]::new($Path) }
    $acl = $item.GetAccessControl()
    Assert-CsdClientStorageDescriptor -Acl $acl -OwnerSid $OwnerSid -Directory:$Directory
}

function Assert-CsdClientPrivateStorage {
    param([Parameter(Mandatory=$true)][string]$Path,
          [ValidateRange(1,100000)][int]$MaximumEntries = 20000)
    try {
        # Validates the lexical path and every ancestor before path resolution;
        # it does not follow or admit installation/data junctions.
        $full = Assert-CsdNormalLocalDirectory -Path $Path
        $owner = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        Assert-CsdClientStorageEntry -Path $full -OwnerSid $owner -Directory
        $ancestor = [IO.Directory]::GetParent($full)
        while ($null -ne $ancestor) {
            Assert-CsdClientAncestorDescriptor -Acl $ancestor.GetAccessControl() -OwnerSid $owner -VolumeRoot:($null -eq $ancestor.Parent)
            $ancestor = $ancestor.Parent
        }
        $database = Join-Path $full 'database'
        Assert-CsdClientStorageEntry -Path $database -OwnerSid $owner -Directory
        $pending = [Collections.Generic.Stack[IO.DirectoryInfo]]::new()
        $pending.Push([IO.DirectoryInfo]::new($database))
        $count = 0
        # Enumerate the root, rather than Test-Path, so inaccessible private files
        # cannot masquerade as absent. Existing runtime/private-service trees are
        # left to their separate guards, including the reviewed Python alias.
        foreach ($item in ([IO.DirectoryInfo]::new($full)).EnumerateFileSystemInfos()) {
            if ($item.Name -ieq 'backups') {
                Assert-CsdClientStorageEntry -Path $item.FullName -OwnerSid $owner -Directory
                $pending.Push([IO.DirectoryInfo]$item)
            } elseif ($item.Name -imatch '^(?:\.env(?:\..*)?|\.client-state(?:\..*)?|\.client-publish-.*|\.client-setup\.log(?:\..*)?|\.client-audit-fallback\.log(?:\..*)?|system\.log(?:\..*)?|audit_fallback\.log(?:\..*)?)$') {
                Assert-CsdClientStorageEntry -Path $item.FullName -OwnerSid $owner
                $count++
            }
        }
        # Covers final/staged databases, WAL/SHM/journal companions and backup
        # descendants without descending into the runtime or installed app tree.
        while ($pending.Count -gt 0) {
            $directory = $pending.Pop()
            foreach ($item in $directory.EnumerateFileSystemInfos()) {
                $count++
                if ($count -gt $MaximumEntries) { throw 'Live storage has too many entries to verify safely.' }
                $isDirectory = ($item.Attributes -band [IO.FileAttributes]::Directory) -ne 0
                Assert-CsdClientStorageEntry -Path $item.FullName -OwnerSid $owner -Directory:$isDirectory
                if ($isDirectory) { $pending.Push([IO.DirectoryInfo]$item) }
            }
        }
    } catch {
        # A privacy refusal must not create .support or change its permissions.
        $script:CsdSuppressPrivacyReceipt = $true
        $script:CsdResultCode = 'CLIENT_STORAGE_ACCESS'
        throw 'Private installation permissions could not be verified. Keep existing files unchanged and ask your technician for an owner-private local destination. No permissions were changed.'
    }
}

function Assert-CsdClientSetupExit {
    param([int]$ExitCode)
    if ($ExitCode -eq 3) {
        $script:CsdSuppressPrivacyReceipt = $true
        $script:CsdResultCode = 'CLIENT_STORAGE_ACCESS'
        throw 'Private installation permissions could not be verified. Keep existing files unchanged and ask your technician for an owner-private local destination. No permissions were changed.'
    }
}

if ($PSBoundParameters.ContainsKey('CsdClientStorageRoot')) {
    try {
        Assert-CsdClientPrivateStorage -Path $CsdClientStorageRoot
        [Console]::Out.WriteLine('CSD_PRIVATE_STORAGE_OK_V1')
    } catch {
        [Console]::Error.WriteLine('Private installation permissions could not be verified. No permissions were changed.')
        exit 1
    }
}
