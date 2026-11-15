[CmdletBinding()]
param(
    [switch]$NoBrowser
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "..\windows\Lifecycle.ps1")
. (Join-Path $PSScriptRoot "..\windows\Client-Storage.ps1")
. (Join-Path $PSScriptRoot '..\windows\Setup-Result.ps1')

$script:ExpectedPythonVersion = "3.11.16"
$script:ExpectedRuntimeLibraryVersion = "14.44.35211.0"
$script:ExpectedRuntimeLibraryHashes = @{
    "vcruntime140.dll" = "d5e4d9a3e835fa679450145d6a7d94e36573a509317111904d9b3712c30d9066"
    "vcruntime140_1.dll" = "1f2d41c4aa5db0bc33ebf7b66d72943a817d7ce6cbe880502a9403823633093f"
}
$script:RootPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$script:LifecycleMutex = $null
$script:LifecycleMutexAcquired = $false
$script:BrowserHelper = $null
Initialize-CsdSetupResult -RootPath $script:RootPath -Stage platform


function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )
    $result=Invoke-CsdBoundedSetupProcess -FilePath $FilePath -Arguments $Arguments -WorkingDirectory $script:RootPath -TimeoutSeconds 120
    if ($result.ExitCode -ne 0) {
        throw $FailureMessage
    }
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

function Assert-PortAvailable {
    param([Parameter(Mandatory = $true)][int]$Port)

    $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    if (@($listeners | Where-Object { $_.Port -eq $Port }).Count -gt 0) {
        throw ("Local port {0} is already in use. Close the other program or CSD Attendance window, then try again." -f $Port)
    }
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

try {
    $script:CsdResultStage='platform'
    Assert-SupportedWindowsArchitecture
    $script:CsdResultStage='media'
    Assert-WindowsMediaComponents
    $script:CsdResultStage='location'
    Assert-ClientLocation -Path $script:RootPath
    Assert-CsdClientPrivateStorage -Path $script:RootPath
    Set-Location -LiteralPath $script:RootPath

    $script:CsdResultStage='lock'
    $script:LifecycleMutex = New-CsdLifecycleMutex -Path $script:RootPath
    try {
        $script:LifecycleMutexAcquired = $script:LifecycleMutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $script:LifecycleMutexAcquired = $true
    }
    if (-not $script:LifecycleMutexAcquired) {
        throw "CSD Attendance, First Run, or a system check is already running on this laptop."
    }
    Assert-CsdNoPendingUpdate -Path $script:RootPath
    Assert-CsdClientPrivateStorage -Path $script:RootPath

    $venvPythonPath = Join-Path $script:RootPath ".venv\Scripts\python.exe"
    $runtimePath = Join-Path $script:RootPath ".runtime"
    $runtimePythonRoot = Join-Path $runtimePath "python"
    $venvPath = Join-Path $script:RootPath ".venv"
    $clientSetupPath = Join-Path $script:RootPath "scripts\client\client_setup.py"
    $envPath = Join-Path $script:RootPath ".env"
    $databasePath = Join-Path $script:RootPath "database\attendance.db"
    $browserHelperPath = Join-Path $PSScriptRoot "Open-BrowserWhenReady.ps1"

    $script:CsdResultStage='runtime_identity'
    Assert-PrivateDirectoryIsLocal -Path $runtimePath -AllowReviewedManagedPythonAlias
    Assert-PrivateDirectoryIsLocal -Path $venvPath

    foreach ($requiredFile in @($venvPythonPath, $clientSetupPath, $envPath, $databasePath, $browserHelperPath)) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw ("Required client file is missing: {0}. Run FIRST_RUN.cmd or contact your technician." -f $requiredFile)
        }
    }

    foreach ($pythonEnvironmentName in @("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")) {
        [Environment]::SetEnvironmentVariable($pythonEnvironmentName, $null, [EnvironmentVariableTarget]::Process)
    }
    $identityResult=Invoke-CsdBoundedSetupProcess -FilePath $venvPythonPath -Arguments @('-c',"import platform, struct; print(platform.python_version() + '|' + str(struct.calcsize('P') * 8) + '|' + platform.python_implementation())") -WorkingDirectory $script:RootPath -TimeoutSeconds 30
    $runtimeIdentity=$identityResult.Output
    if ($identityResult.ExitCode -ne 0 -or ($runtimeIdentity | Out-String).Trim() -ne "$($script:ExpectedPythonVersion)|64|CPython") {
        throw ("The private runtime is not 64-bit CPython {0}. Run FIRST_RUN.cmd or contact your technician." -f $script:ExpectedPythonVersion)
    }

    $prefixResult=Invoke-CsdBoundedSetupProcess -FilePath $venvPythonPath -Arguments @('-c','import os, sys; print(os.path.realpath(sys.base_prefix))') -WorkingDirectory $script:RootPath -TimeoutSeconds 30
    $basePrefix=$prefixResult.Output.Trim()
    if ($prefixResult.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($basePrefix)) {
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

    Write-Host "Validating this laptop's local CSD Attendance data..."
    $script:CsdResultStage='state_check'
    $checkChild=Invoke-CsdBoundedSetupProcess -FilePath $venvPythonPath -Arguments @($clientSetupPath,'check','--root',$script:RootPath,'--json') -WorkingDirectory $script:RootPath -TimeoutSeconds 120
    Assert-CsdClientSetupExit -ExitCode $checkChild.ExitCode
    $checkResult=$checkChild.Output | ConvertFrom-Json
    if ($checkChild.ExitCode -ne 0 -or $null -eq $checkResult -or $checkResult.ok -isnot [bool] -or -not $checkResult.ok -or $checkResult.status -cne 'ready') {
        throw 'The client configuration or database state did not confirm readiness.'
    }

    $applicationEnvironmentNames = @(
        "SECRET_KEY", "CAMERA_SOURCE_KEY", "BIOMETRIC_KEY",
        "BACKUP_ARCHIVE_KEY", "PURGE_ARCHIVE_KEY",
        "ENTRANCE_DISPLAY_TOKEN", "ENTRANCE_DISPLAY_SESSION_HOURS",
        "FLASK_DEBUG", "FLASK_HOST", "FLASK_PORT",
        "INSTITUTION_UTC_OFFSET_MINUTES", "SESSION_WORKER_MODE",
        "SESSION_RESUME_GRACE_MINUTES", "ENABLE_MAINTENANCE_PURGE",
        "REQUIRE_LIVENESS", "CCTV_HOST", "CCTV_PORT", "CCTV_USERNAME",
        "CCTV_PASSWORD", "CCTV_SNAPSHOT_PATH",
        "CCTV_SUBSTREAM_SNAPSHOT_PATH", "CCTV_VIEWER_PORT",
        "CCTV_VIEWER_BIND_HOST", "CCTV_VIEWER_USERNAME",
        "CCTV_VIEWER_PASSWORD", "CCTV_VIEWER_ALLOW_LAN",
        "CCTV_VIEWER_BEHIND_HTTPS_PROXY", "OPENCV_FFMPEG_CAPTURE_OPTIONS"
    )
    foreach ($name in $applicationEnvironmentNames) {
        [Environment]::SetEnvironmentVariable($name, $null, [EnvironmentVariableTarget]::Process)
    }

    $readPortCode = "from dotenv import dotenv_values; import sys; value = dotenv_values(sys.argv[1]).get('FLASK_PORT'); print('' if value is None else value)"
    $portResult=Invoke-CsdBoundedSetupProcess -FilePath $venvPythonPath -Arguments @('-c',$readPortCode,$envPath) -WorkingDirectory $script:RootPath -TimeoutSeconds 30
    $portText=$portResult.Output.Trim()
    if ($portResult.ExitCode -ne 0) {
        throw "FLASK_PORT could not be read safely from .env."
    }
    if ([string]::IsNullOrWhiteSpace($portText)) {
        $portText = "5000"
    }
    $port = 0
    if (-not [int]::TryParse($portText, [ref]$port) -or $port -lt 1024 -or $port -gt 65535) {
        throw "FLASK_PORT in .env must be a number from 1024 through 65535."
    }
    $script:CsdResultStage='startup'
    Assert-PortAvailable -Port $port

    $env:SESSION_WORKER_MODE = "process"
    $env:FLASK_HOST = "127.0.0.1"
    $env:FLASK_DEBUG = "false"
    $env:FLASK_PORT = $port.ToString([System.Globalization.CultureInfo]::InvariantCulture)
    $env:PYTHONUNBUFFERED = "1"

    $loginUrl = "http://127.0.0.1:$port/login"
    $windowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $helperArguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "{0}" -Url "{1}" -ParentProcessId {2}' -f `
        $browserHelperPath.Replace('"', '""'), $loginUrl, $PID
    if (-not $NoBrowser) {
        $script:BrowserHelper = Start-Process -FilePath $windowsPowerShell -ArgumentList $helperArguments -WindowStyle Hidden -PassThru
    }

    Write-Host ""
    Write-Host ("Starting CSD Attendance at {0}" -f $loginUrl) -ForegroundColor Green
    Write-Host "Keep this window open. Press Ctrl+C once to stop the system safely."
    Write-Host ""

    $serveCode = "import os, sys; from dotenv import load_dotenv; load_dotenv(sys.argv[1], override=True); os.environ['SESSION_WORKER_MODE']='process'; os.environ['FLASK_HOST']='127.0.0.1'; os.environ['FLASK_DEBUG']='false'; os.environ['FLASK_PORT']=sys.argv[2]; from waitress import serve; from app import app; from services.session_lifecycle import start_coordinator; start_coordinator(); app.config['CSD_SERVING_MODE']='local'; app.config['CSD_PUBLIC_URL']=None; serve(app, host='127.0.0.1', port=int(sys.argv[2]), threads=8)"
    try {
        $serverExitCode = Invoke-CsdContainedProcess -FilePath $venvPythonPath -Arguments @(
            "-c", $serveCode, $envPath, ([string]$port)
        ) -WorkingDirectory $script:RootPath
    } catch { $script:CsdResultCode='CLEANUP_UNCONFIRMED'; throw }
    if ($serverExitCode -eq -1073741510 -or $serverExitCode -eq 3221225786) {
        Write-Host "CSD Attendance was stopped by the operator."
        Write-CsdSetupResult -Outcome success -ExitCode 0
        exit 0
    }
    if ($serverExitCode -ne 0) {
        throw ("The CSD Attendance server exited with code {0}." -f $serverExitCode)
    }
    Write-CsdSetupResult -Outcome success -ExitCode 0
    exit 0
}
catch {
    $public=Get-CsdPublicSetupResult
    $cleanup=if ($public.code -eq 'CLEANUP_UNCONFIRMED') { 'unknown' } else { 'confirmed' }
    $failureExit=Get-CsdFailureExitCode
    $outcome=if ($cleanup -eq 'unknown') { 'unknown' } else { 'failed' }
    Write-CsdSetupResult -Outcome $outcome -Cleanup $cleanup -ExitCode $failureExit
    Write-Host ""
    Write-Host ("START FAILED [{0}]: {1}" -f $public.code,$public.message) -ForegroundColor Red
    exit $failureExit
}
finally {
    if ($null -ne $script:BrowserHelper) {
        try {
            if (-not $script:BrowserHelper.HasExited) {
                Stop-Process -Id $script:BrowserHelper.Id -Force -ErrorAction SilentlyContinue
            }
        }
        catch { }
    }
    if ($script:LifecycleMutexAcquired -and $null -ne $script:LifecycleMutex) {
        try { $script:LifecycleMutex.ReleaseMutex() } catch { }
    }
    if ($null -ne $script:LifecycleMutex) {
        $script:LifecycleMutex.Dispose()
    }
}
