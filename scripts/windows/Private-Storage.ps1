# Windows PowerShell 5.1. Create owner-private storage; never repair another
# account's existing folder or recursively change installation permissions.
function Assert-CsdNormalLocalDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\') -or $full -notmatch '\A[A-Za-z]:\\') {
        throw 'Private update storage requires a local Windows drive.'
    }
    $drive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($full))
    if (-not $drive.IsReady -or $drive.DriveType -ne [IO.DriveType]::Fixed -or $drive.DriveFormat -ine 'NTFS') {
        throw 'Private update storage requires a fixed local NTFS drive.'
    }
    $ancestor = $full
    while ($ancestor) {
        $attributes = [IO.File]::GetAttributes($ancestor)
        if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            ($attributes -band [IO.FileAttributes]::Directory) -eq 0) {
            throw 'Private update storage cannot use links, junctions or non-directory ancestors.'
        }
        $parent = [IO.Directory]::GetParent($ancestor)
        $ancestor = if ($null -eq $parent) { $null } else { $parent.FullName }
    }
    return $full
}

function Assert-CsdPrivateDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = Assert-CsdNormalLocalDirectory -Path $Path
    $owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = ([IO.DirectoryInfo]::new($full)).GetAccessControl()
    $rules = @($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $owner.Value -or
        -not $acl.AreAccessRulesProtected -or $rules.Count -ne 3) {
        throw 'Private update storage has unexpected ownership or permissions. It was not changed.'
    }
    $seen = @{}
    foreach ($rule in $rules) {
        $sid = $rule.IdentityReference.Value
        if ($sid -notin @($owner.Value,'S-1-5-18','S-1-5-32-544') -or $seen.ContainsKey($sid) -or
            $rule.IsInherited -or $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $rule.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl -or
            $rule.InheritanceFlags -ne ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit) -or
            $rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None) {
            throw 'Private update storage has unexpected access rules. It was not changed.'
        }
        $seen[$sid] = $true
    }
}

function New-CsdPrivateDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    $parent = [IO.Directory]::GetParent($full)
    if ($null -eq $parent) { throw 'A dedicated private subfolder is required.' }
    $null = Assert-CsdNormalLocalDirectory -Path $parent.FullName
    if ([IO.Directory]::Exists($full) -or [IO.File]::Exists($full)) {
        Assert-CsdPrivateDirectory -Path $full
        return
    }
    $owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetOwner($owner)
    $acl.SetAccessRuleProtection($true,$false)
    foreach ($sid in @($owner.Value,'S-1-5-18','S-1-5-32-544')) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($sid),
            [Security.AccessControl.FileSystemRights]::FullControl,
            ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit),
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        ))
    }
    # Permissions apply at creation, before the first recovery/key byte exists.
    [void][IO.Directory]::CreateDirectory($full,$acl)
    Assert-CsdPrivateDirectory -Path $full
}

function Assert-CsdPrivateTree {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [ValidateRange(1,100000)][int]$MaximumEntries = 20000
    )
    # Verify inherited permissions on every recovery file, not only its root.
    # Enumeration is iterative and checks reparse points before descending.
    Assert-CsdPrivateDirectory -Path $Path
    $full = [IO.Path]::GetFullPath($Path)
    $owner = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $allowed = @($owner,'S-1-5-18','S-1-5-32-544')
    $pending = [Collections.Generic.Stack[IO.DirectoryInfo]]::new()
    $pending.Push([IO.DirectoryInfo]::new($full))
    $count = 0
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($item in $directory.EnumerateFileSystemInfos()) {
            $count++
            if ($count -gt $MaximumEntries) { throw 'Private recovery has too many entries to verify.' }
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Private recovery contains a link or junction. Its permissions were not changed.'
            }
            $isDirectory = ($item.Attributes -band [IO.FileAttributes]::Directory) -ne 0
            $acl = $item.GetAccessControl()
            $rules = @($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
            if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $allowed -or
                $acl.AreAccessRulesProtected -or $rules.Count -ne 3) {
                throw 'A private recovery entry has unexpected ownership or inheritance. It was not changed.'
            }
            $expectedInheritance = if ($isDirectory) {
                [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
            } else { [Security.AccessControl.InheritanceFlags]::None }
            $seen = @{}
            foreach ($rule in $rules) {
                $sid = $rule.IdentityReference.Value
                if ($sid -notin $allowed -or $seen.ContainsKey($sid) -or -not $rule.IsInherited -or
                    $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
                    $rule.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl -or
                    $rule.InheritanceFlags -ne $expectedInheritance -or
                    $rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None) {
                    throw 'A private recovery entry has unexpected access rules. They were not changed.'
                }
                $seen[$sid] = $true
            }
            if ($isDirectory) { $pending.Push([IO.DirectoryInfo]$item) }
        }
    }
}
