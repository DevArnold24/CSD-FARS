[CmdletBinding()]
param(
    [ValidateSet('status','review','apply','recover')][string]$Action = 'status',
    [string]$PackagePath = '',
    [string]$ExpectedSha256 = '',
    [switch]$NoUI
)
Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\windows\Lifecycle.ps1')
. (Join-Path $PSScriptRoot '..\windows\Private-Storage.ps1')
. (Join-Path $PSScriptRoot '..\windows\Client-Storage.ps1')
. (Join-Path $PSScriptRoot '..\windows\Setup-Dialogs.ps1')
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$mutex = $null
$acquired = $false
$script:child = $null
$exitCode = 1

function Invoke-CsdUpdateAction {
    param([string]$SelectedAction, [string]$SelectedPackage = '', [string]$ReviewedHash = '')
    Assert-CsdClientPrivateStorage -Path $root
    $requestFolder = Join-Path $root ('.updates\request-' + [guid]::NewGuid().ToString('N'))
    [void][IO.Directory]::CreateDirectory($requestFolder)
    $requestFile = Join-Path $requestFolder 'request.json'
    $document = @{ action=$SelectedAction; package=$SelectedPackage; sha256=$ReviewedHash } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($requestFile, $document, [Text.UTF8Encoding]::new($false))
    $parent = [Diagnostics.Process]::GetCurrentProcess()
    # Keep integer FILETIME precision across .NET and Python.
    $created = $parent.StartTime.ToUniversalTime().ToFileTimeUtc()
    $arguments = @('-I','-B',(Join-Path $root 'scripts\client\update_bootstrap.py'),
        '--root',$root,'--parent-pid',[string]$PID,
        '--parent-created',$created.ToString([Globalization.CultureInfo]::InvariantCulture),
        '--request',$requestFile)
    $script:child = New-CsdContainedProcess -FilePath (Join-Path $root '.venv\Scripts\python.exe') `
        -Arguments $arguments -WorkingDirectory $root -NoWindow
    try {
        if ($NoUI) { while (-not $script:child.Wait(250)) {} }
        else {
            Add-Type -AssemblyName System.Windows.Forms
            Add-Type -AssemblyName System.Drawing
            $form = [Windows.Forms.Form]::new()
            $form.Text = 'CSD Attendance - Checking update'
            $form.Font = [Drawing.Font]::new('Segoe UI',10)
            $form.AutoScaleMode = 'Dpi'
            $form.AutoScaleDimensions = [Drawing.SizeF]::new(96,96)
            $form.ClientSize = [Drawing.Size]::new(540,180)
            $form.StartPosition = 'CenterScreen'
            $form.ControlBox = $false
            $label = [Windows.Forms.Label]::new()
            $label.Dock = 'Fill'
            $label.Padding = [Windows.Forms.Padding]::new(24)
            $label.Text = 'Please wait while CSD checks the package and recovery files. Keep this window open. Larger databases can take several minutes.'
            $label.AccessibleName = $label.Text
            $form.Controls.Add($label)
            $progress = [Windows.Forms.ProgressBar]::new()
            $progress.Dock = 'Bottom'
            $progress.Style = 'Marquee'
            $form.Controls.Add($progress)
            $timer = [Windows.Forms.Timer]::new()
            $timer.Interval = 250
            $timer.Add_Tick({ if ($script:child.Wait(0)) { $timer.Stop(); $form.Close() } })
            try { $timer.Start(); [void]$form.ShowDialog() }
            finally { $timer.Dispose(); $form.Dispose() }
        }
        $resultFile = Join-Path $requestFolder 'result.json'
        if (-not [IO.File]::Exists($resultFile) -or ([IO.FileInfo]::new($resultFile)).Length -gt 16384) {
            throw 'The update controller could not finish. Keep the installation and recovery files unchanged.'
        }
        $result = [IO.File]::ReadAllText($resultFile) | ConvertFrom-Json
        if (-not $result.ok -or $script:child.ExitCode -ne 0) {
            if ($result.PSObject.Properties['message']) { throw ([string]$result.message) }
            throw 'The update could not finish. Keep its recovery files unchanged.'
        }
        return $result.data
    }
    finally { $script:child.Dispose(); $script:child = $null }
}

function Select-CsdUpdatePackage {
    Add-Type -AssemblyName System.Windows.Forms
    $picker = [Windows.Forms.OpenFileDialog]::new()
    $picker.Title = 'Choose the update ZIP supplied for this installation'
    $picker.Filter = 'CSD update package (*.zip)|*.zip'
    $picker.CheckFileExists = $true
    $picker.Multiselect = $false
    try {
        if ($picker.ShowDialog() -eq [Windows.Forms.DialogResult]::OK) { return $picker.FileName }
        return $null
    }
    finally { $picker.Dispose() }
}

function Invoke-CsdGuidedUpdate {
    # Check the protected controller first. Unprovisioned publishers, unfinished
    # setup and interrupted updates must never reach ordinary package selection.
    $status = Invoke-CsdUpdateAction -SelectedAction 'status'
    if ($status.status -eq 'recovery_required') {
        $choice = Show-CsdSetupChoice -Title 'Finish interrupted update' `
            -Message 'An earlier update did not finish. CSD must check its saved recovery files before the server can start. Keep this installation in its current folder.' `
            -Choices @(@{Value='recover';Label='Recover this installation';Detail='Check the saved update and matching recovery copy, then verify the application.'})
        if ($choice -ne 'recover') { throw [OperationCanceledException]::new('Recovery cancelled.') }
        $result = Invoke-CsdUpdateAction -SelectedAction 'recover'
    }
    elseif ($status.status -eq 'ready') {
        $choice = Show-CsdSetupChoice -Title 'Update this installation' `
            -Message 'Choose the update ZIP supplied by your system owner. Keep the ZIP as a single file; a fresh-install bundle is a different package. CSD will check the publisher and installed version before asking you to apply it. Keep this computer awake and connected to power.' `
            -Choices @(@{Value='choose';Label='Choose update package';Detail='Your accounts, enrollments, attendance and settings stay with this installation.'})
        if ($choice -ne 'choose') { throw [OperationCanceledException]::new('Update cancelled.') }
        while ($true) {
            $selected = Select-CsdUpdatePackage
            if ([string]::IsNullOrWhiteSpace($selected)) { throw [OperationCanceledException]::new('Update cancelled.') }
            try {
                $review = Invoke-CsdUpdateAction -SelectedAction 'review' -SelectedPackage $selected
                if ($review.status -cne 'reviewed' -or [string]$review.sha256 -cnotmatch '\A[0-9a-f]{64}\z') {
                    throw 'CSD could not confirm the selected update package. No update was applied.'
                }
                break
            }
            catch {
                $choice = Show-CsdSetupChoice -Title 'This package could not be used' `
                    -Message ($_.Exception.Message + "`r`n`r`nNo update was applied. Choose another supplied update ZIP, or close this window and ask the system owner for the correct package.") `
                    -Choices @(@{Value='retry';Label='Choose another package';Detail='The next package will go through the same publisher and version checks.'})
                if ($choice -ne 'retry') { throw [OperationCanceledException]::new('Update cancelled.') }
            }
        }
        $message = "Update to $($review.version).`r`n`r`nCSD will save a recovery copy, apply this checked package, and check the application. Keep the computer powered on. Accounts, enrollments, attendance and settings are preserved."
        $choice = Show-CsdSetupChoice -Title 'Ready to update' -Message $message `
            -Choices @(@{Value='apply';Label='Back up and apply update';Detail='The package publisher and installed version have been verified. This step applies the update.'})
        if ($choice -ne 'apply') { throw [OperationCanceledException]::new('Update cancelled.') }
        $result = Invoke-CsdUpdateAction -SelectedAction 'apply' -SelectedPackage $selected -ReviewedHash $review.sha256
    }
    else { throw 'CSD could not confirm the installation update state. Keep its files unchanged and ask the system owner for help.' }

    if ($result.status -eq 'restored') {
        $message = 'The previous version was restored and passed its application check. Open START_HERE.cmd and choose your usual mode, or use Manage automatic startup to start the configured server.'
    }
    elseif ($result.status -in @('applied','completed')) {
        $message = 'The update passed its application check. Open START_HERE.cmd and choose your usual mode, or use Manage automatic startup to start the configured server. Its recovery files have been kept.'
    }
    else { throw 'CSD could not confirm update completion. Keep the installation and recovery files unchanged.' }
    $null = Show-CsdSetupChoice -Title 'Update check finished' -Message $message -CancelText 'Close'
    return $result
}

try {
    $null = Assert-CsdNormalLocalDirectory -Path $root
    Assert-CsdClientPrivateStorage -Path $root
    $mutex = New-CsdLifecycleMutex -Path $root
    try { $acquired = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $acquired = $true }
    if (-not $acquired) {
        throw 'CSD Attendance is running or another setup window is open. Finish attendance and stop the server, then open this updater again.'
    }
    Assert-CsdClientPrivateStorage -Path $root
    New-CsdPrivateDirectory -Path (Join-Path $root '.updates')
    New-CsdPrivateDirectory -Path (Join-Path $root '.update-trust')
    if (-not [IO.File]::Exists((Join-Path $root '.venv\Scripts\python.exe')) -or
        -not [IO.File]::Exists((Join-Path $root '.client-state'))) {
        if ([IO.File]::Exists((Join-Path $root '.updates\pending.json'))) {
            throw 'The interrupted update is missing its original runtime or setup record. Keep this folder unchanged and ask the system owner for recovery help.'
        }
        throw 'Finish first setup before using updates. Open START_HERE.cmd and choose Use on this computer or Share with computers on this network.'
    }
    if ($NoUI) {
        $result = Invoke-CsdUpdateAction -SelectedAction $Action -SelectedPackage $PackagePath -ReviewedHash $ExpectedSha256
        $result | ConvertTo-Json -Compress
    }
    else {
        $result = Invoke-CsdGuidedUpdate
    }
    $exitCode = 0
}
catch [OperationCanceledException] { $exitCode = 2 }
catch {
    if ($NoUI) { [Console]::Error.WriteLine($_.Exception.Message) }
    else { $null = Show-CsdSetupChoice -Title 'Update needs attention' -Message $_.Exception.Message -CancelText 'Close' }
}
finally {
    if ($null -ne $script:child) { $script:child.Dispose() }
    if ($acquired) { $mutex.ReleaseMutex() }
    if ($null -ne $mutex) { $mutex.Dispose() }
    # All child jobs have ended before the server lock is released.
}
exit $exitCode
