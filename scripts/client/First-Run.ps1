[CmdletBinding()]
param([switch]$PrepareOnly, [switch]$ConsoleSetup, [switch]$Graphical)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "..\windows\Lifecycle.ps1")
. (Join-Path $PSScriptRoot "..\windows\Client-Storage.ps1")
. (Join-Path $PSScriptRoot '..\windows\Setup-Result.ps1')
if ($Graphical) { . (Join-Path $PSScriptRoot '..\windows\Setup-Dialogs.ps1') }

$script:ExpectedPythonVersion = "3.11.16"
$script:ExpectedUvSha256 = "69a60e42e824019a3e97577670e58f76652ed98f0b16f75ec46981012459929e"
$script:ExpectedManagedPythonUrl = "https://releases.astral.sh/github/python-build-standalone/releases/download/20260825/cpython-3.11.16%2B20260825-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
$script:ExpectedRuntimeLibraryVersion = "14.44.35211.0"
$script:ExpectedRuntimeLibraryHashes = @{
    "vcruntime140.dll" = "d5e4d9a3e835fa679450145d6a7d94e36573a509317111904d9b3712c30d9066"
    "vcruntime140_1.dll" = "1f2d41c4aa5db0bc33ebf7b66d72943a817d7ce6cbe880502a9403823633093f"
}
$script:MinimumFreeBytes = 6GB
$script:RootPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$script:Mutex = $null
$script:MutexAcquired = $false
Initialize-CsdSetupResult -RootPath $script:RootPath -Stage platform
$script:SetupClock = [Diagnostics.Stopwatch]::StartNew()
$script:SetupTotalSeconds = 1800

function Invoke-SetupChild {
    param([string]$FilePath,[string[]]$Arguments,[int]$TimeoutSeconds=120)
    $remaining = [Math]::Floor($script:SetupTotalSeconds-$script:SetupClock.Elapsed.TotalSeconds)
    if ($remaining -lt 1) { $script:CsdResultCode='CHILD_TIMEOUT'; throw 'Setup total deadline exceeded.' }
    return Invoke-CsdBoundedSetupProcess -FilePath $FilePath -Arguments $Arguments -WorkingDirectory $script:RootPath -TimeoutSeconds ([Math]::Min($TimeoutSeconds,$remaining)) -MaximumOutput 16384
}

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ""
    Write-Host ("==> {0}" -f $Message) -ForegroundColor Cyan
}

function Assert-SupportedWindowsArchitecture {
    try {
        $osArchitecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
        $processArchitecture = [System.Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture.ToString()
    }
    catch {
        throw "This client requires 64-bit AMD64/x64 Windows and a 64-bit PowerShell process. Contact your technician; ARM64 and 32-bit Windows are not supported."
    }
    if ($osArchitecture -ne "X64" -or $processArchitecture -ne "X64") {
        throw "This client bundle supports only 64-bit AMD64/x64 Windows. Contact your technician; do not continue on ARM64 or 32-bit Windows."
    }
}

function Assert-WindowsMediaComponents {
    $missing = @(
        foreach ($libraryName in @("mf.dll", "mfplat.dll", "mfreadwrite.dll")) {
            if (-not (Test-Path -LiteralPath (Join-Path ([Environment]::SystemDirectory) $libraryName) -PathType Leaf)) {
                $libraryName
            }
        }
    )
    if ($missing.Count -ne 0) {
        throw "Required Windows media/camera components are unavailable. This is common on Windows N/KN or restricted enterprise devices. Do not download individual DLLs or bypass device policy; contact your technician or IT administrator to enable Microsoft's official Media Feature Pack."
    }
}

function Assert-PrivateDirectoryIsLocal {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$AllowReviewedManagedPythonAlias
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer -or
        ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw ("Private runtime path must be a normal local directory, not a file, symbolic link, or junction: {0}" -f $Path)
    }

    $expectedAliasPath = [System.IO.Path]::GetFullPath(
        (Join-Path $Path "python\cpython-3.11-windows-x86_64-none")
    ).TrimEnd("\")
    $expectedTargetPath = [System.IO.Path]::GetFullPath(
        (Join-Path $Path "python\cpython-3.11.16-windows-x86_64-none")
    ).TrimEnd("\")
    $nestedReparsePoints = @(
        Get-ChildItem -LiteralPath $Path -Force -Recurse | Where-Object {
            ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
        }
    )
    foreach ($nestedReparsePoint in $nestedReparsePoints) {
        $nestedPath = [System.IO.Path]::GetFullPath($nestedReparsePoint.FullName).TrimEnd("\")
        if ($AllowReviewedManagedPythonAlias -and
            $nestedPath.Equals($expectedAliasPath, [System.StringComparison]::OrdinalIgnoreCase) -and
            $nestedReparsePoint.PSIsContainer -and
            $nestedReparsePoint.LinkType -eq "Junction") {
            $targets = @($nestedReparsePoint.Target)
            if ($targets.Count -eq 1 -and
                -not [string]::IsNullOrWhiteSpace([string]$targets[0]) -and
                [System.IO.Path]::IsPathRooted([string]$targets[0])) {
                $actualTargetPath = [System.IO.Path]::GetFullPath([string]$targets[0]).TrimEnd("\")
                if ($actualTargetPath.Equals($expectedTargetPath, [System.StringComparison]::OrdinalIgnoreCase) -and
                    (Test-Path -LiteralPath $expectedTargetPath -PathType Container)) {
                    $targetItem = Get-Item -LiteralPath $expectedTargetPath -Force
                    if (($targetItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0) {
                        continue
                    }
                }
            }
            throw "The managed-Python runtime alias does not point to the exact reviewed in-folder Python 3.11.16 directory."
        }
        throw ("Private runtime directories cannot contain unreviewed symbolic links or junctions: {0}" -f $nestedReparsePoint.FullName)
    }
}


function Assert-ClientLocation {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ($Path.StartsWith("\\", [System.StringComparison]::Ordinal)) {
        throw "This folder is on a network/UNC path. Move the complete CSD Attendance folder to a local drive first."
    }

    if ($Path -match '(?i)\\OneDrive(?:\s*-\s*[^\\]+)?(?:\\|$)') {
        throw "This folder is inside OneDrive. Move it to a normal local folder so database files cannot be cloud-synchronized while open."
    }

    foreach ($variableName in @("OneDrive", "OneDriveConsumer", "OneDriveCommercial")) {
        $oneDrivePath = [Environment]::GetEnvironmentVariable($variableName)
        if ([string]::IsNullOrWhiteSpace($oneDrivePath)) {
            continue
        }

        $oneDriveFullPath = [System.IO.Path]::GetFullPath($oneDrivePath).TrimEnd("\")
        if ($Path.Equals($oneDriveFullPath, [System.StringComparison]::OrdinalIgnoreCase) -or
            $Path.StartsWith($oneDriveFullPath + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "This folder is inside OneDrive. Move it to a normal local folder so database files cannot be cloud-synchronized while open."
        }
    }

    $pathItem = Get-Item -LiteralPath $Path -Force
    while ($null -ne $pathItem) {
        if (($pathItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "CSD Attendance cannot run through a symbolic link or junction. Move the complete folder to a normal local path."
        }
        $pathItem = $pathItem.Parent
    }

    $driveRoot = [System.IO.Path]::GetPathRoot($Path)
    if ([string]::IsNullOrWhiteSpace($driveRoot)) {
        throw "The local drive for this folder could not be determined."
    }

    $drive = [System.IO.DriveInfo]::new($driveRoot)
    if (-not $drive.IsReady) {
        throw "The drive containing this folder is not ready."
    }
    if ($drive.DriveType -ne [System.IO.DriveType]::Fixed) {
        throw "CSD Attendance must be placed on a fixed local drive, not removable or network storage."
    }
    if (-not $drive.DriveFormat.Equals("NTFS", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw ("CSD Attendance requires an NTFS drive. This drive reports {0}." -f $drive.DriveFormat)
    }
    if ($drive.AvailableFreeSpace -lt $script:MinimumFreeBytes) {
        $freeGB = [Math]::Round($drive.AvailableFreeSpace / 1GB, 1)
        throw ("At least 6 GB of free space is required for First Run. This drive has {0} GB free." -f $freeGB)
    }
}

function Acquire-SetupMutex {
    $script:Mutex = New-CsdLifecycleMutex -Path $script:RootPath
    try {
        $script:MutexAcquired = $script:Mutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $script:MutexAcquired = $true
    }

    if (-not $script:MutexAcquired) {
        throw "CSD Attendance, First Run, or a system check is already running. Close it before trying again."
    }
    Assert-CsdNoPendingUpdate -Path $script:RootPath
    Assert-CsdClientPrivateStorage -Path $script:RootPath
}

function Assert-ClientWritable {
    $probe=Join-Path $script:RootPath ('.csd-write-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $created=$false
    try {
        $stream=[IO.File]::Open($probe,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        $created=$true
        try { $stream.WriteByte(0); $stream.Flush($true) } finally { $stream.Dispose() }
    } finally { if ($created) { [IO.File]::Delete($probe) } }
}

function Move-IncompleteVirtualEnvironmentAside {
    param([Parameter(Mandatory = $true)][string]$VenvPath)

    $statePaths = @(
        (Join-Path $script:RootPath ".env"),
        (Join-Path $script:RootPath ".client-state"),
        (Join-Path $script:RootPath "database\attendance.db")
    )
    if (@($statePaths | Where-Object { Test-Path -LiteralPath $_ }).Count -gt 0) {
        throw "The private Python environment is incomplete but client data already exists. Leave the folder unchanged and contact your technician."
    }

    $venvItem = Get-Item -LiteralPath $VenvPath -Force
    if (($venvItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "The incomplete .venv is a symbolic link or junction. Leave the folder unchanged and contact your technician."
    }

    $suffix = "{0}-{1}" -f (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmss"), ([Guid]::NewGuid().ToString("N").Substring(0, 8))
    $preservedPath = Join-Path $script:RootPath (".venv.incomplete-{0}" -f $suffix)
    Move-Item -LiteralPath $VenvPath -Destination $preservedPath
    Write-Host ("Preserved an interrupted private environment as {0}." -f [System.IO.Path]::GetFileName($preservedPath)) -ForegroundColor Yellow
}

function Get-ManifestEntries {
    param(
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][string]$BasePath
    )

    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw ("Required checksum manifest is missing: {0}" -f $ManifestPath)
    }

    $baseFullPath = [System.IO.Path]::GetFullPath($BasePath).TrimEnd("\")
    $basePrefix = $baseFullPath + "\"
    $entries = @{}

    foreach ($line in Get-Content -LiteralPath $ManifestPath) {
        $trimmed = $line.Trim()
        if ($trimmed.Length -eq 0 -or $trimmed.StartsWith("#")) {
            continue
        }
        if ($trimmed -notmatch '^([0-9A-Fa-f]{64})\s+\*?(.+)$') {
            throw ("Invalid checksum line in {0}." -f $ManifestPath)
        }

        $expectedHash = $matches[1].ToUpperInvariant()
        $relativePath = $matches[2].Trim().Replace("/", "\")
        if ([string]::IsNullOrWhiteSpace($relativePath) -or [System.IO.Path]::IsPathRooted($relativePath)) {
            throw ("Unsafe checksum path in {0}." -f $ManifestPath)
        }
        if (($relativePath -split '[\\/]') -contains "..") {
            throw ("Unsafe checksum path in {0}." -f $ManifestPath)
        }

        $fullPath = [System.IO.Path]::GetFullPath((Join-Path $baseFullPath $relativePath))
        if (-not $fullPath.StartsWith($basePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw ("Unsafe checksum path in {0}." -f $ManifestPath)
        }
        if ($entries.ContainsKey($fullPath)) {
            throw ("Duplicate checksum entry for {0}." -f $relativePath)
        }
        $entries[$fullPath] = $expectedHash
    }

    if ($entries.Count -eq 0) {
        throw ("Checksum manifest is empty: {0}" -f $ManifestPath)
    }
    return $entries
}

function Assert-ManifestFiles {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Entries,
        [Parameter(Mandatory = $true)][string]$Description
    )

    foreach ($fullPath in $Entries.Keys) {
        if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
            throw ("{0} file is missing: {1}" -f $Description, [System.IO.Path]::GetFileName($fullPath))
        }
        $actualHash = (Get-FileHash -LiteralPath $fullPath -Algorithm SHA256).Hash.ToUpperInvariant()
        if ($actualHash -ne $Entries[$fullPath]) {
            throw ("{0} checksum failed for {1}. Download a fresh official client bundle." -f $Description, [System.IO.Path]::GetFileName($fullPath))
        }
    }
}

function Assert-UvBundle {
    param([Parameter(Mandatory = $true)][string]$UvPath)

    if (-not (Test-Path -LiteralPath $UvPath -PathType Leaf)) {
        throw "The bundled Python runtime tool is missing. Download the complete CSD Attendance Client Bundle, not the GitHub source ZIP."
    }

    $uvDirectory = Split-Path -Parent $UvPath
    $manifestCandidates = @(
        (Join-Path $uvDirectory "uv.exe.sha256"),
        (Join-Path $uvDirectory "SHA256SUMS")
    )
    $manifestPath = $manifestCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if ($null -eq $manifestPath) {
        throw "The bundled Python runtime checksum is missing. Download a fresh official client bundle."
    }

    $entries = Get-ManifestEntries -ManifestPath $manifestPath -BasePath $uvDirectory
    $uvFullPath = [System.IO.Path]::GetFullPath($UvPath)
    if (-not $entries.ContainsKey($uvFullPath)) {
        throw "The Python runtime checksum manifest does not contain uv.exe."
    }
    if ($entries.Count -ne 1) {
        throw "The Python runtime checksum manifest must contain exactly the bundled uv.exe entry."
    }
    Assert-ManifestFiles -Entries $entries -Description "Python runtime"
    $actualUvHash = (Get-FileHash -LiteralPath $UvPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualUvHash -ne $script:ExpectedUvSha256) {
        throw "The bundled uv 0.12.8 tool does not match the checksum-verified official Windows x64 release. Download a fresh official client bundle."
    }
}

function Assert-Wheelhouse {
    param([Parameter(Mandatory = $true)][string]$WheelhousePath)

    if (-not (Test-Path -LiteralPath $WheelhousePath -PathType Container)) {
        throw "The offline wheelhouse is missing. Download the complete CSD Attendance Client Bundle, not the GitHub source ZIP."
    }

    $manifestPath = Join-Path $WheelhousePath "SHA256SUMS"
    $entries = Get-ManifestEntries -ManifestPath $manifestPath -BasePath $WheelhousePath
    Assert-ManifestFiles -Entries $entries -Description "Dependency package"

    $manifestFullPath = [System.IO.Path]::GetFullPath($manifestPath)
    $files = @(Get-ChildItem -LiteralPath $WheelhousePath -File -Recurse | Where-Object {
        -not $_.FullName.Equals($manifestFullPath, [System.StringComparison]::OrdinalIgnoreCase)
    })
    if ($files.Count -eq 0) {
        throw "The offline wheelhouse contains no dependency packages."
    }

    foreach ($file in $files) {
        if (-not $file.Extension.Equals(".whl", [System.StringComparison]::OrdinalIgnoreCase)) {
            throw ("Unexpected file in the offline wheelhouse: {0}" -f $file.Name)
        }
        if (-not $entries.ContainsKey($file.FullName)) {
            throw ("Dependency package is not covered by SHA256SUMS: {0}" -f $file.Name)
        }
    }
    foreach ($fullPath in $entries.Keys) {
        if (-not $fullPath.EndsWith(".whl", [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "SHA256SUMS may contain only .whl dependency packages."
        }
    }

    Write-Host ("Verified {0} offline dependency package(s)." -f $files.Count) -ForegroundColor Green
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$FailureMessage,
        [switch]$Interactive,
        [switch]$ClientSetup
    )

    if ($Interactive) {
        $child=$null
        $clock=[Diagnostics.Stopwatch]::StartNew()
        try {
            $child=New-CsdContainedProcess -FilePath $FilePath -Arguments $Arguments -WorkingDirectory $script:RootPath
            while (-not $child.Wait(50)) {
                if ($clock.Elapsed.TotalSeconds -ge 600 -or $script:SetupClock.Elapsed.TotalSeconds -ge $script:SetupTotalSeconds) { $script:CsdResultCode='CHILD_TIMEOUT'; throw 'Setup child deadline exceeded.' }
            }
            $result=[pscustomobject]@{ExitCode=$child.ExitCode}
        } finally {
            if ($null -ne $child) { try { $child.Dispose() } catch { $script:CsdResultCode='CLEANUP_UNCONFIRMED'; throw 'Setup cleanup not confirmed.' } }
        }
    }
    else { $result = Invoke-SetupChild -FilePath $FilePath -Arguments $Arguments -TimeoutSeconds 600 }
    if ($ClientSetup) { Assert-CsdClientSetupExit -ExitCode $result.ExitCode }
    if ($result.ExitCode -ne 0) {
        throw $FailureMessage
    }
}

function Assert-ClientRuntimeImports {
    param([Parameter(Mandatory = $true)][string]$PythonPath)
    # Some native runtime helpers exit zero on import failure. Require a fresh
    # completion receipt from after every import, before initializing any data.
    $nonce = [Guid]::NewGuid().ToString('N')
    $completion = 'CSD_RUNTIME_IMPORTS_COMPLETE:' + $nonce
    $importCheck = @'
import struct, sys
# These are safety gates, not optional assertions: PYTHONOPTIMIZE must not
# disable them or let an invalid runtime emit the completion receipt.
if sys.version_info[:3] != tuple(map(int, sys.argv[1].split('.'))):
    raise RuntimeError('The private Python version does not match the required runtime.')
if struct.calcsize('P') * 8 != 64:
    raise RuntimeError('The private Python runtime is not 64-bit.')
import flask, werkzeug, dotenv, cv2, face_recognition, dlib, simpleaudio, numpy, psutil, cryptography, flask_wtf, openpyxl, gunicorn, setuptools, waitress
# Top-level imports and distribution metadata can survive missing package files.
# Require the APIs used by private-state encryption, account setup and serving
# before emitting the fresh receipt or allowing account/database initialization.
from cryptography.fernet import Fernet
from flask import Flask
from werkzeug.security import check_password_hash, generate_password_hash
from waitress import serve
if not all(callable(api) for api in (Fernet, Flask, check_password_hash, generate_password_hash, serve)):
    raise RuntimeError('A required runtime API is unavailable.')
print('CSD_RUNTIME_IMPORTS_COMPLETE:' + sys.argv[2], flush=True)
'@
    $result = Invoke-SetupChild -FilePath $PythonPath -Arguments @('-B','-c',$importCheck,$script:ExpectedPythonVersion,$nonce)
    $output = @($result.Output.TrimEnd([char[]]"`r`n") -split '\r?\n')
    $exitCode = $result.ExitCode
    if ($exitCode -ne 0 -or $output.Count -eq 0 -or $output.Count -gt 1024 -or
        @($output | Where-Object { $_ -isnot [string] -or $_.Length -gt 4096 }).Count -gt 0 -or
        $output[-1] -cne $completion -or @($output | Where-Object { $_ -ceq $completion }).Count -ne 1) {
        throw 'The client runtime did not confirm that all required modules loaded. Setup stopped before creating accounts or data.'
    }
    Write-Host 'All client runtime imports passed.'
}

function Assert-PythonRuntime {
    param([Parameter(Mandatory = $true)][string]$PythonPath)

    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw "The client virtual environment is incomplete: python.exe is missing."
    }

    $identityResult = Invoke-SetupChild -FilePath $PythonPath -Arguments @('-c',"import platform, struct; print(platform.python_version() + '|' + str(struct.calcsize('P') * 8) + '|' + platform.python_implementation())") -TimeoutSeconds 30
    $runtimeIdentity = $identityResult.Output
    if ($identityResult.ExitCode -ne 0) {
        throw "The client Python runtime could not be started."
    }
    if (($runtimeIdentity | Out-String).Trim() -ne "$($script:ExpectedPythonVersion)|64|CPython") {
        throw ("The client requires 64-bit CPython {0}. The existing .venv does not match." -f $script:ExpectedPythonVersion)
    }

    $prefixResult = Invoke-SetupChild -FilePath $PythonPath -Arguments @('-c','import os, sys; print(os.path.realpath(sys.base_prefix))') -TimeoutSeconds 30
    $basePrefix = $prefixResult.Output.Trim()
    if ($prefixResult.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($basePrefix)) {
        throw "The managed Python runtime location could not be verified."
    }
    $expectedRuntimeRoot = [System.IO.Path]::GetFullPath((Join-Path $script:RootPath ".runtime\python")).TrimEnd("\")
    $basePrefix = [System.IO.Path]::GetFullPath($basePrefix).TrimEnd("\")
    if (-not $basePrefix.StartsWith($expectedRuntimeRoot + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The private environment is not bound to this client's reviewed managed Python runtime."
    }
    foreach ($libraryName in $script:ExpectedRuntimeLibraryHashes.Keys) {
        $libraryPath = Join-Path $basePrefix $libraryName
        if (-not (Test-Path -LiteralPath $libraryPath -PathType Leaf)) {
            throw ("The managed Python runtime is missing {0}." -f $libraryName)
        }
        $actualHash = (Get-FileHash -LiteralPath $libraryPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $actualVersion = (Get-Item -LiteralPath $libraryPath).VersionInfo.FileVersion
        if ($actualHash -ne $script:ExpectedRuntimeLibraryHashes[$libraryName] -or
            $actualVersion -ne $script:ExpectedRuntimeLibraryVersion) {
            throw ("The managed Python runtime library {0} does not match the reviewed client artifact." -f $libraryName)
        }
    }
}

function Install-CsdVerifiedDependencies {
    param([string]$UvPath,[string]$PythonPath,[string]$WheelhousePath,[string[]]$InstallArguments)
    $script:CsdResultStage='dependencies'
    Invoke-NativeCommand -FilePath $UvPath -Arguments $InstallArguments -FailureMessage 'The verified offline dependencies could not be installed'
    try {
        Invoke-NativeCommand -FilePath $UvPath -Arguments @('pip','check','--python',$PythonPath,'--offline') -FailureMessage 'The installed dependency consistency check failed'
        $script:CsdResultStage='runtime_imports'
        Assert-ClientRuntimeImports -PythonPath $PythonPath
    }
    catch {
        # Metadata can survive missing files. At most one verified offline
        # reinstall; a timeout or uncertain cleanup never triggers another child.
        if ($script:CsdResultCode -in @('CHILD_TIMEOUT','CHILD_OUTPUT_LIMIT','CLEANUP_UNCONFIRMED')) { throw }
        $script:CsdResultStage='dependencies'
        Assert-Wheelhouse -WheelhousePath $WheelhousePath
        Invoke-NativeCommand -FilePath $UvPath -Arguments ($InstallArguments + @('--reinstall')) -FailureMessage 'The single verified offline dependency repair failed'
        Invoke-NativeCommand -FilePath $UvPath -Arguments @('pip','check','--python',$PythonPath,'--offline') -FailureMessage 'The repaired dependency consistency check failed'
        $script:CsdResultStage='runtime_imports'
        Assert-ClientRuntimeImports -PythonPath $PythonPath
    }
}

try {
    $script:CsdResultStage='platform'
    Assert-SupportedWindowsArchitecture
    $script:CsdResultStage='media'
    Assert-WindowsMediaComponents
    if ($PrepareOnly) {
        Write-Host "Preparing the private runtime only. Administrator and database setup will remain unfinished."
    }
    else {
        Write-Host "This guided setup creates a private runtime and a new local database on this laptop."
    }
    Write-Host "It does not install Python system-wide and does not need Administrator access."

    Write-Step "Checking the client folder"
    $script:CsdResultStage='location'
    Assert-ClientLocation -Path $script:RootPath
    Assert-CsdClientPrivateStorage -Path $script:RootPath
    Assert-ClientWritable
    $script:CsdResultStage='lock'
    Acquire-SetupMutex
    Set-Location -LiteralPath $script:RootPath

    $uvPath = Join-Path $script:RootPath "tools\uv\uv.exe"
    $wheelhousePath = Join-Path $script:RootPath "wheelhouse"
    $lockPath = Join-Path $script:RootPath "requirements-client.lock"
    $clientSetupPath = Join-Path $script:RootPath "scripts\client\client_setup.py"
    $runtimePath = Join-Path $script:RootPath ".runtime"
    $runtimePythonPath = Join-Path $runtimePath "python"
    $uvCachePath = Join-Path $runtimePath "uv-cache"
    $venvPath = Join-Path $script:RootPath ".venv"
    $venvPythonPath = Join-Path $venvPath "Scripts\python.exe"

    $script:CsdResultStage='location'
    Assert-PrivateDirectoryIsLocal -Path $runtimePath -AllowReviewedManagedPythonAlias
    Assert-PrivateDirectoryIsLocal -Path $venvPath

    $script:CsdResultStage='bundle'
    foreach ($requiredFile in @($lockPath, $clientSetupPath, (Join-Path $PSScriptRoot 'Admin-Setup-Window.ps1'))) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw ("Required client bundle file is missing: {0}" -f [System.IO.Path]::GetFileName($requiredFile))
        }
    }

    Write-Step "Verifying the client bundle integrity"
    Assert-UvBundle -UvPath $uvPath
    Assert-Wheelhouse -WheelhousePath $wheelhousePath

    if (-not (Test-Path -LiteralPath $runtimePath -PathType Container)) {
        New-Item -ItemType Directory -Path $runtimePath | Out-Null
    }
    if (-not (Test-Path -LiteralPath $uvCachePath -PathType Container)) {
        New-Item -ItemType Directory -Path $uvCachePath | Out-Null
    }

    $env:UV_PYTHON_INSTALL_DIR = $runtimePythonPath
    $env:UV_CACHE_DIR = $uvCachePath
    $env:UV_NO_PROGRESS = "1"
    $env:UV_PYTHON_INSTALL_BIN = "0"
    $env:UV_PYTHON_INSTALL_REGISTRY = "0"
    $env:UV_PYTHON_NO_REGISTRY = "1"
    $env:UV_NO_CONFIG = "1"
    foreach ($pythonEnvironmentName in @("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")) {
        [Environment]::SetEnvironmentVariable($pythonEnvironmentName, $null, [EnvironmentVariableTarget]::Process)
    }

    $venvReady = $false
    if (Test-Path -LiteralPath $venvPath) {
        $script:CsdResultStage='runtime_identity'
        if (-not (Test-Path -LiteralPath $venvPath -PathType Container)) {
            throw "The .venv path is not a folder. Leave it unchanged and contact your technician."
        }
        Write-Step "Validating the existing private Python runtime"
        try {
            Assert-PythonRuntime -PythonPath $venvPythonPath
            $venvReady = $true
            Write-Host "The existing private environment is compatible." -ForegroundColor Green
        }
        catch {
            if ($script:CsdResultCode -in @('CHILD_TIMEOUT','CHILD_OUTPUT_LIMIT','CLEANUP_UNCONFIRMED')) { throw }
            Move-IncompleteVirtualEnvironmentAside -VenvPath $venvPath
        }
    }

    if (-not $venvReady) {
        $script:CsdResultStage='python_download'
        Write-Step ("Preparing private 64-bit Python {0}" -f $script:ExpectedPythonVersion)
        $downloadResult = Invoke-SetupChild -FilePath $uvPath -Arguments @('python','list',$script:ExpectedPythonVersion,'--only-downloads','--show-urls') -TimeoutSeconds 30
        $managedPythonDownloads = @($downloadResult.Output -split '\r?\n' | Where-Object {
            $_ -match '^cpython-3\.11\.16-windows-x86_64-none\s+'
        })
        if ($downloadResult.ExitCode -ne 0 -or $managedPythonDownloads.Count -ne 1) {
            throw "The reviewed Windows x64 managed Python download could not be resolved. Contact your technician."
        }
        $managedPythonUrl = ($managedPythonDownloads[0] -split '\s+', 2)[1]
        if (-not $managedPythonUrl.Equals($script:ExpectedManagedPythonUrl, [System.StringComparison]::Ordinal)) {
            throw "The managed Python download no longer matches the reviewed immutable artifact. Contact your technician."
        }
        Invoke-NativeCommand -FilePath $uvPath -Arguments @(
            "python", "install", $script:ExpectedPythonVersion,
            "--install-dir", $runtimePythonPath,
            "--managed-python", "--no-bin", "--no-registry"
        ) -FailureMessage "The private Python runtime could not be prepared"
        Invoke-NativeCommand -FilePath $uvPath -Arguments @(
            "venv", $venvPath,
            "--python", $script:ExpectedPythonVersion,
            "--managed-python", "--no-python-downloads", "--no-project"
        ) -FailureMessage "The client virtual environment could not be created"
        $script:CsdResultStage='runtime_identity'
        Assert-PythonRuntime -PythonPath $venvPythonPath
    }

    $script:CsdResultStage='dependencies'
    Write-Step "Installing verified dependencies without using the internet"
    $offlineInstallArguments = @(
        "pip", "install",
        "--python", $venvPythonPath,
        "--offline", "--no-index", "--no-cache", "--no-build",
        "--find-links", $wheelhousePath,
        "--require-hashes",
        "--requirement", $lockPath
    )
    Install-CsdVerifiedDependencies -UvPath $uvPath -PythonPath $venvPythonPath -WheelhousePath $wheelhousePath -InstallArguments $offlineInstallArguments

    if ($PrepareOnly) {
        Write-Host "Runtime prepared only; no administrator, database, keys, or client state were created." -ForegroundColor Green
        Write-Host "Run START_HERE.cmd to create your administrator and finish setup."
        Write-CsdSetupResult -Outcome success -ExitCode 0
        exit 0
    }

    Write-Step "Creating or validating this laptop's private CSD Attendance data"
    $script:CsdResultStage='administrator'
    $env:SESSION_WORKER_MODE = "process"
    $env:FLASK_HOST = "127.0.0.1"
    $env:FLASK_DEBUG = "false"
    if (-not $ConsoleSetup) {
        # Ask the state machine whether input is needed. A file-existence hint
        # cannot distinguish a completed account from resumable staged setup.
        $setupChild = Invoke-SetupChild -FilePath $venvPythonPath -Arguments @($clientSetupPath,'initialize','--root',$script:RootPath,'--no-prompt','--json')
        $setupExit = $setupChild.ExitCode
        Assert-CsdClientSetupExit -ExitCode $setupExit
        $setupText = $setupChild.Output
        if ($setupText.Length -gt 16384) { throw 'The saved setup progress could not be checked.' }
        $setupResult = $setupText | ConvertFrom-Json
        if ($setupResult.ok -isnot [bool] -or $setupResult.message -isnot [string] -or $setupResult.message.Length -gt 2048) {
            throw 'The saved setup progress could not be checked.'
        }
        if ($setupExit -eq 2 -and $setupResult.status -ceq 'input_required' -and -not $setupResult.ok) {
            . (Join-Path $PSScriptRoot 'Admin-Setup-Window.ps1')
            if (-not (Show-CsdAdministratorSetup -RootPath $script:RootPath -PythonPath $venvPythonPath)) {
                if ($null -eq $script:CsdResultCode) { $script:CsdResultCode='SETUP_CANCELLED' }
                throw 'Administrator setup was closed. Reopen setup to check or finish this installation.'
            }
        }
        elseif ($setupExit -ne 0 -or -not $setupResult.ok -or $setupResult.status -cnotin @('initialized','resumed','ready')) {
            throw $setupResult.message
        }
    }
    else {
        Invoke-NativeCommand -FilePath $venvPythonPath -Arguments @($clientSetupPath, "initialize", "--root", $script:RootPath) -FailureMessage "The local CSD Attendance initialization did not complete" -Interactive -ClientSetup
    }
    $script:CsdResultStage='state_check'
    $checkChild=Invoke-SetupChild -FilePath $venvPythonPath -Arguments @($clientSetupPath,'check','--root',$script:RootPath,'--json')
    Assert-CsdClientSetupExit -ExitCode $checkChild.ExitCode
    $checkResult=$checkChild.Output | ConvertFrom-Json
    if ($checkChild.ExitCode -ne 0 -or $null -eq $checkResult -or $checkResult.ok -isnot [bool] -or -not $checkResult.ok -or $checkResult.status -cne 'ready') {
        throw 'The initialized client state did not confirm readiness.'
    }

    Write-Host ""
    Write-Host "CSD Attendance is ready on this laptop." -ForegroundColor Green
    Write-Host "START_HERE.cmd or START_CSD.cmd will run it. Keep the server window open while the system is in use."
    Write-CsdSetupResult -Outcome success -ExitCode 0
    exit 0
}
catch {
    $public=Get-CsdPublicSetupResult
    $outcome=if ($public.code -eq 'SETUP_CANCELLED') { 'cancelled' } elseif ($public.code -in @('SETUP_RESULT_UNKNOWN','CLEANUP_UNCONFIRMED')) { 'unknown' } else { 'failed' }
    $cleanup=if ($public.code -eq 'CLEANUP_UNCONFIRMED') { 'unknown' } else { 'confirmed' }
    $failureExit=Get-CsdFailureExitCode
    Write-CsdSetupResult -Outcome $outcome -Cleanup $cleanup -ExitCode $failureExit
    if ($Graphical -and $env:CSD_SETUP_RESULT_NONCE -cnotmatch '^[0-9a-f]{32}$') { Show-CsdSetupNotice -Title 'Setup needs attention' -Message $public.message }
    Write-Host ""
    Write-Host ("FIRST RUN STOPPED [{0}]: {1}" -f $public.code,$public.message) -ForegroundColor Red
    Write-Host "No existing database was reset or replaced."
    exit $failureExit
}
finally {
    if ($script:MutexAcquired -and $null -ne $script:Mutex) {
        try { $script:Mutex.ReleaseMutex() } catch { }
    }
    if ($null -ne $script:Mutex) {
        $script:Mutex.Dispose()
    }
}
