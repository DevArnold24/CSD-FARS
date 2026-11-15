[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "..\windows\Lifecycle.ps1")
. (Join-Path $PSScriptRoot "..\windows\Client-Storage.ps1")

$script:ExpectedPythonVersion = "3.11.16"
$script:ExpectedUvSha256 = "69a60e42e824019a3e97577670e58f76652ed98f0b16f75ec46981012459929e"
$script:ExpectedRuntimeLibraryVersion = "14.44.35211.0"
$script:ExpectedRuntimeLibraryHashes = @{
    "vcruntime140.dll" = "d5e4d9a3e835fa679450145d6a7d94e36573a509317111904d9b3712c30d9066"
    "vcruntime140_1.dll" = "1f2d41c4aa5db0bc33ebf7b66d72943a817d7ce6cbe880502a9403823633093f"
}
$script:RootPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$script:Mutex = $null
$script:MutexAcquired = $false

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


function Acquire-CheckMutex {
    $script:Mutex = New-CsdLifecycleMutex -Path $script:RootPath
    try {
        $script:MutexAcquired = $script:Mutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $script:MutexAcquired = $true
    }
    if (-not $script:MutexAcquired) {
        throw "CSD Attendance, First Run, or another system check is already running. Close it or wait for it to finish."
    }
    Assert-CsdNoPendingUpdate -Path $script:RootPath
    Assert-CsdClientPrivateStorage -Path $script:RootPath
}

function Assert-ClientLocation {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ($Path.StartsWith("\\", [System.StringComparison]::Ordinal) -or
        $Path -match '(?i)\\OneDrive(?:\s*-\s*[^\\]+)?(?:\\|$)') {
        throw "CSD Attendance must be kept in a normal folder on a local drive, outside OneDrive."
    }
    foreach ($variableName in @("OneDrive", "OneDriveConsumer", "OneDriveCommercial")) {
        $oneDrivePath = [Environment]::GetEnvironmentVariable($variableName)
        if ([string]::IsNullOrWhiteSpace($oneDrivePath)) { continue }
        $oneDriveFullPath = [System.IO.Path]::GetFullPath($oneDrivePath).TrimEnd("\")
        if ($Path.Equals($oneDriveFullPath, [System.StringComparison]::OrdinalIgnoreCase) -or
            $Path.StartsWith($oneDriveFullPath + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "CSD Attendance must be kept outside OneDrive."
        }
    }
    $pathItem = Get-Item -LiteralPath $Path -Force
    while ($null -ne $pathItem) {
        if (($pathItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "CSD Attendance cannot run through a symbolic link or junction."
        }
        $pathItem = $pathItem.Parent
    }
    $drive = [System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($Path))
    if (-not $drive.IsReady -or $drive.DriveType -ne [System.IO.DriveType]::Fixed -or
        -not $drive.DriveFormat.Equals("NTFS", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "CSD Attendance must be kept on a fixed local NTFS drive."
    }
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
        if ($trimmed.Length -eq 0 -or $trimmed.StartsWith("#")) { continue }
        if ($trimmed -notmatch '^([0-9A-Fa-f]{64})\s+\*?(.+)$') {
            throw ("Invalid checksum line in {0}." -f $ManifestPath)
        }
        $expectedHash = $matches[1].ToUpperInvariant()
        $relativePath = $matches[2].Trim().Replace("/", "\")
        if ([string]::IsNullOrWhiteSpace($relativePath) -or [System.IO.Path]::IsPathRooted($relativePath) -or
            (($relativePath -split '[\\/]') -contains "..")) {
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
            throw ("{0} checksum failed for {1}." -f $Description, [System.IO.Path]::GetFileName($fullPath))
        }
    }
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw ("{0} (exit code {1})." -f $FailureMessage, $LASTEXITCODE)
    }
}

try {
    Assert-SupportedWindowsArchitecture
    Assert-WindowsMediaComponents
    Assert-ClientLocation -Path $script:RootPath
    Assert-CsdClientPrivateStorage -Path $script:RootPath
    Acquire-CheckMutex
    Set-Location -LiteralPath $script:RootPath

    $uvPath = Join-Path $script:RootPath "tools\uv\uv.exe"
    $uvDirectory = Split-Path -Parent $uvPath
    $uvManifestCandidates = @(
        (Join-Path $uvDirectory "uv.exe.sha256"),
        (Join-Path $uvDirectory "SHA256SUMS")
    )
    $uvManifestPath = $uvManifestCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    $wheelhousePath = Join-Path $script:RootPath "wheelhouse"
    $wheelManifestPath = Join-Path $wheelhousePath "SHA256SUMS"
    $lockPath = Join-Path $script:RootPath "requirements-client.lock"
    $clientSetupPath = Join-Path $script:RootPath "scripts\client\client_setup.py"
    $runtimePath = Join-Path $script:RootPath ".runtime"
    $runtimePythonRoot = Join-Path $runtimePath "python"
    $venvPath = Join-Path $script:RootPath ".venv"
    $venvPythonPath = Join-Path $script:RootPath ".venv\Scripts\python.exe"
    $envPath = Join-Path $script:RootPath ".env"
    $databasePath = Join-Path $script:RootPath "database\attendance.db"

    Assert-PrivateDirectoryIsLocal -Path $runtimePath -AllowReviewedManagedPythonAlias
    Assert-PrivateDirectoryIsLocal -Path $venvPath

    Write-Step "Checking required client files"
    foreach ($requiredFile in @($uvPath, $lockPath, $clientSetupPath, $venvPythonPath, $envPath, $databasePath)) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw ("Required client file is missing: {0}" -f $requiredFile)
        }
    }
    if ($null -eq $uvManifestPath) {
        throw "The bundled Python runtime checksum is missing."
    }

    Write-Step "Verifying bundled runtime and dependency packages"
    $uvEntries = Get-ManifestEntries -ManifestPath $uvManifestPath -BasePath $uvDirectory
    $uvFullPath = [System.IO.Path]::GetFullPath($uvPath)
    if ($uvEntries.Count -ne 1 -or -not $uvEntries.ContainsKey($uvFullPath)) {
        throw "The Python runtime checksum manifest must contain exactly uv.exe."
    }
    Assert-ManifestFiles -Entries $uvEntries -Description "Python runtime"
    $actualUvHash = (Get-FileHash -LiteralPath $uvPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualUvHash -ne $script:ExpectedUvSha256) {
        throw "The bundled uv 0.12.8 tool does not match the checksum-verified official Windows x64 release."
    }

    $wheelEntries = Get-ManifestEntries -ManifestPath $wheelManifestPath -BasePath $wheelhousePath
    Assert-ManifestFiles -Entries $wheelEntries -Description "Dependency package"
    $wheelManifestFullPath = [System.IO.Path]::GetFullPath($wheelManifestPath)
    $wheelFiles = @(Get-ChildItem -LiteralPath $wheelhousePath -File -Recurse | Where-Object {
        -not $_.FullName.Equals($wheelManifestFullPath, [System.StringComparison]::OrdinalIgnoreCase)
    })
    if ($wheelFiles.Count -eq 0) {
        throw "The offline wheelhouse contains no dependency packages."
    }
    foreach ($file in $wheelFiles) {
        if (-not $file.Extension.Equals(".whl", [System.StringComparison]::OrdinalIgnoreCase) -or
            -not $wheelEntries.ContainsKey($file.FullName)) {
            throw ("Unverified file in the offline wheelhouse: {0}" -f $file.Name)
        }
    }

    Write-Step "Checking private Python runtime"
    foreach ($pythonEnvironmentName in @("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")) {
        [Environment]::SetEnvironmentVariable($pythonEnvironmentName, $null, [EnvironmentVariableTarget]::Process)
    }
    $runtimeIdentity = & $venvPythonPath -c "import platform, struct; print(platform.python_version() + '|' + str(struct.calcsize('P') * 8) + '|' + platform.python_implementation())"
    if ($LASTEXITCODE -ne 0 -or ($runtimeIdentity | Out-String).Trim() -ne "$($script:ExpectedPythonVersion)|64|CPython") {
        throw ("The private runtime is not 64-bit CPython {0}. Run FIRST_RUN.cmd or contact your technician." -f $script:ExpectedPythonVersion)
    }

    $basePrefix = (& $venvPythonPath -c "import os, sys; print(os.path.realpath(sys.base_prefix))" | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($basePrefix)) {
        throw "The managed Python runtime location could not be verified."
    }
    $expectedRuntimeRoot = [System.IO.Path]::GetFullPath($runtimePythonRoot).TrimEnd("\")
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

    $env:UV_PYTHON_INSTALL_DIR = Join-Path $runtimePath "python"
    $env:UV_CACHE_DIR = Join-Path $runtimePath "uv-cache"
    $env:UV_NO_PROGRESS = "1"
    $env:UV_PYTHON_INSTALL_BIN = "0"
    $env:UV_PYTHON_INSTALL_REGISTRY = "0"
    $env:UV_PYTHON_NO_REGISTRY = "1"
    $env:UV_NO_CONFIG = "1"
    Invoke-NativeCommand -FilePath $uvPath -Arguments @(
        "pip", "check", "--python", $venvPythonPath, "--offline"
    ) -FailureMessage "Installed dependency consistency check failed"

    $importCheck = "import struct, sys; assert sys.version_info[:3] == (3, 11, 16); assert struct.calcsize('P') * 8 == 64; import flask, werkzeug, dotenv, cv2, face_recognition, dlib, simpleaudio, numpy, psutil, cryptography, flask_wtf, openpyxl, gunicorn, setuptools, waitress; print('All client runtime imports passed.')"
    Invoke-NativeCommand -FilePath $venvPythonPath -Arguments @("-c", $importCheck) -FailureMessage "A required client runtime module could not be imported"

    Write-Step "Checking local configuration and database state"
    Invoke-NativeCommand -FilePath $venvPythonPath -Arguments @($clientSetupPath, "check", "--root", $script:RootPath) -FailureMessage "The client configuration or database state check failed"

    Write-Host ""
    Write-Host "All CSD Attendance client checks passed." -ForegroundColor Green
    exit 0
}
catch {
    Write-Host ""
    Write-Host ("SYSTEM CHECK FAILED: {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}
finally {
    if ($script:MutexAcquired -and $null -ne $script:Mutex) {
        try { $script:Mutex.ReleaseMutex() } catch { }
    }
    if ($null -ne $script:Mutex) {
        $script:Mutex.Dispose()
    }
}
