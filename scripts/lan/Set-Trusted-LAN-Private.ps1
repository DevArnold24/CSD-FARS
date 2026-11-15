#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 2147483647)]
    [int]$InterfaceIndex,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(?:\d{1,3}\.){3}\d{1,3}$')]
    [string]$ExpectedIPv4
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Lan-Common.ps1")

try {
    $configuration = Get-NetIPConfiguration -InterfaceIndex $InterfaceIndex -ErrorAction Stop
    if ($null -eq $configuration.NetAdapter -or
        $configuration.NetAdapter.Status -ne "Up") {
        throw "The selected network is no longer connected."
    }

    $expectedAddress = $null
    if (-not [System.Net.IPAddress]::TryParse($ExpectedIPv4, [ref]$expectedAddress) -or
        -not (Test-Rfc1918Address -Address $expectedAddress)) {
        throw "The expected LAN address is not a private IPv4 address."
    }
    $matchingAddresses = @($configuration.IPv4Address | Where-Object {
        [string]$_.IPAddress -eq $ExpectedIPv4
    })
    if ($matchingAddresses.Count -ne 1) {
        throw "The selected network address changed. Return to START_HERE and retry LAN sharing."
    }

    $profiles = @(Get-NetConnectionProfile -InterfaceIndex $InterfaceIndex -ErrorAction Stop)
    if ($profiles.Count -ne 1) {
        throw "Windows could not identify one network profile for this connection."
    }
    $category = [string]$profiles[0].NetworkCategory
    if ($category -in @("Private", "DomainAuthenticated")) {
        Write-Host "This network connection is already ready for LAN sharing." -ForegroundColor Green
        exit 0
    }
    if ($category -ne "Public") {
        throw ("Windows reports an unsupported network profile: {0}." -f $category)
    }

    Set-NetConnectionProfile -InterfaceIndex $InterfaceIndex -NetworkCategory Private -ErrorAction Stop
    $updated = @(Get-NetConnectionProfile -InterfaceIndex $InterfaceIndex -ErrorAction Stop)
    if ($updated.Count -ne 1 -or [string]$updated[0].NetworkCategory -ne "Private") {
        throw "Windows did not keep the network profile change."
    }

    Write-Host "Windows now marks this trusted network connection as Private." -ForegroundColor Green
    exit 0
}
catch {
    Write-Host ("LAN NETWORK FIX FAILED: {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}
