# Fresh-install destination preparation. No existing file or ACL is repaired.
function Assert-CsdInstallEntryAbsent {
    param([Parameter(Mandatory=$true)]$Parent, [Parameter(Mandatory=$true)][string]$Name)
    foreach ($entry in $Parent.EnumerateFileSystemInfos()) {
        if ($entry.Name -ieq $Name) {
            throw 'That folder or file already exists. Choose a different NEW folder.'
        }
    }
}

function Get-CsdNewInstallDestination {
    param([Parameter(Mandatory=$true)][string]$Path)
    if ($Path -notmatch '\A[A-Za-z]:\\' -or $Path.StartsWith('\')) {
        throw 'Choose an absolute folder path on a local NTFS drive.'
    }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $parent = [IO.Directory]::GetParent($full)
    if ($null -eq $parent -or -not [IO.Directory]::Exists($parent.FullName)) {
        throw 'Choose a new folder directly inside an existing local folder.'
    }
    $null = Assert-CsdNormalLocalDirectory -Path $parent.FullName
    # A broken junction also counts as existing. Enumerate the parent without
    # following entries; never treat an inaccessible target as an empty location.
    Assert-CsdInstallEntryAbsent -Parent $parent -Name ([IO.Path]::GetFileName($full))
    $owner = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $ancestor = $parent
    while ($null -ne $ancestor) {
        Assert-CsdClientAncestorDescriptor -Acl $ancestor.GetAccessControl() -OwnerSid $owner -VolumeRoot:($null -eq $ancestor.Parent)
        $ancestor = $ancestor.Parent
    }
    return $full
}

function New-CsdInstallDestination {
    param([Parameter(Mandatory=$true)][string]$Path, [switch]$Confirmed)
    if (-not $Confirmed) { throw 'Preparing a private installation folder requires your confirmation.' }
    $full = Get-CsdNewInstallDestination -Path $Path
    New-CsdPrivateDirectory -Path $full
    Assert-CsdPrivateDirectory -Path $full
    # No extraction, launch, data copying, network change or permission repair.
    return $full
}

function Invoke-CsdInstallDestinationPrompt {
    Write-Host ''
    Write-Host 'This extracted folder cannot safely hold private school data.' -ForegroundColor Yellow
    Write-Host 'For a NEW installation, prepare a private folder, then extract the original ZIP there.'
    Write-Host 'For an existing installation with accounts or records, stop here. Do not reinstall over it.'
    $choice = Read-Host 'Prepare a NEW installation folder? Type Y, or press Enter to leave everything unchanged'
    if ($choice -ine 'Y') { return }
    $default = Join-Path ([Environment]::GetFolderPath('UserProfile')) 'CSD-Attendance'
    $entered = Read-Host ("New folder path (Enter for {0})" -f $default)
    if ([string]::IsNullOrWhiteSpace($entered)) { $entered = $default }
    try {
        $full = Get-CsdNewInstallDestination -Path $entered
        Write-Host ("Create: {0}" -f $full)
        Write-Host 'Only your current Windows account, SYSTEM and Administrators will have full access to this NEW folder.'
        Write-Host 'Existing folders, their permissions, accounts and network settings will not be changed.'
        if ((Read-Host 'Type CREATE to confirm, or press Enter to cancel') -cne 'CREATE') { return }
        $created = New-CsdInstallDestination -Path $full -Confirmed
        Write-Host ''
        Write-Host ("Folder ready: {0}" -f $created) -ForegroundColor Green
        Write-Host 'Use Extract All on the original client ZIP and select this folder as the destination.'
        Write-Host 'Then open START_HERE.cmd inside the newly extracted folder. Do not copy an existing installation into it.'
        Write-Host 'No application has been installed or started yet.'
    }
    catch {
        Write-Host ("Folder preparation stopped: {0}" -f $_.Exception.Message) -ForegroundColor Yellow
        Write-Host 'Existing files and permissions were not changed. Choose a new folder under your Windows user folder.'
    }
}
