[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Root)
Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Private-Storage.ps1')
. (Join-Path $PSScriptRoot 'Client-Storage.ps1')
. (Join-Path $PSScriptRoot 'Lifecycle.ps1')
try {
    $null = Assert-CsdNormalLocalDirectory -Path $Root
    Assert-CsdClientPrivateStorage -Path $Root
    Assert-CsdPrivateTree -Path (Join-Path $Root '.updates')
    Assert-CsdPrivateTree -Path (Join-Path $Root '.update-trust')
    @{
        owner_sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        mutex = Get-RootScopedMutexName -Path $Root
    } | ConvertTo-Json -Compress
}
catch {
    # No paths, key material or exception details in the controller protocol.
    [Console]::Error.WriteLine('Update storage ownership or access could not be verified. No permissions were changed.')
    exit 1
}
