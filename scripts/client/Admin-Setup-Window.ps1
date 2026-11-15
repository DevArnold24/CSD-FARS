# Loaded by First-Run while it owns the installation lifecycle mutex.
# Credentials travel only through the child process's anonymous stdin pipe.
. (Join-Path $PSScriptRoot '..\windows\Setup-Result.ps1')
function Test-CsdPasswordCodePoint {
    param($Ranges, [int]$CodePoint)
    $low = 0; $high = $Ranges.Count - 1
    while ($low -le $high) {
        $middle = [int][Math]::Floor(($low + $high) / 2)
        if ($CodePoint -lt $Ranges[$middle][0]) { $high = $middle - 1 }
        elseif ($CodePoint -gt $Ranges[$middle][1]) { $low = $middle + 1 }
        else { return $true }
    }
    return $false
}

function Get-CsdPasswordFeedback {
    param([string]$Password, [string]$Confirmation, $Policy)
    $result = [ordered]@{length=$false; upper=$false; lower=$false; digit=$false; special=$false}
    $points = 0
    for ($index = 0; $index -lt $Password.Length; $index++) {
        if ([char]::IsHighSurrogate($Password[$index]) -and $index + 1 -lt $Password.Length -and [char]::IsLowSurrogate($Password[$index + 1])) {
            $code = [char]::ConvertToUtf32($Password, $index); $index++
        } else { $code = [int][char]$Password[$index] }
        $points++
        foreach ($name in @('upper','lower','digit')) {
            if (Test-CsdPasswordCodePoint $Policy.$name $code) { $result[$name] = $true }
        }
        if (-not (Test-CsdPasswordCodePoint $Policy.alnum $code)) { $result.special = $true }
    }
    $result.length = $points -ge $Policy.minimum
    return @{ Rules=$result; Valid=($result.Values -notcontains $false);
        Matches=($Confirmation.Length -gt 0 -and [string]::Equals($Password, $Confirmation, [StringComparison]::Ordinal)) }
}

function Get-CsdSetupPasswordPolicy {
    param([string]$PythonPath, [string]$SetupPath, [string]$RootPath)
    $savedCode=Get-Variable -Name CsdResultCode -Scope Script -ErrorAction SilentlyContinue
    $previousCode=if ($null -eq $savedCode) { $null } else { $savedCode.Value }
    try {
        $response = Invoke-CsdBoundedSetupProcess -FilePath $PythonPath -Arguments @('-B',$SetupPath,'password-policy') -WorkingDirectory $RootPath -TimeoutSeconds 8 -MaximumOutput 65536
        if ($response.ExitCode -ne 0) { return $null }
        $policy = $response.Output | ConvertFrom-Json
        if ($policy.minimum -ne 8) { return $null }
        foreach ($name in @('upper','lower','digit','alnum')) {
            if (-not $policy.$name -or $policy.$name.Count -lt 1) { return $null }
        }
        return $policy
    } catch {
        if ($script:CsdResultCode -eq 'CLEANUP_UNCONFIRMED') { throw }
        return $null # Server validation remains authoritative without guidance.
    }
    finally { if ($script:CsdResultCode -ne 'CLEANUP_UNCONFIRMED') { $script:CsdResultCode=$previousCode } }
}

function Show-CsdAdministratorSetup {
    param(
        [Parameter(Mandatory = $true)][string]$RootPath,
        [Parameter(Mandatory = $true)][string]$PythonPath
    )
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [Windows.Forms.Application]::EnableVisualStyles()
    $setupPath = Join-Path $RootPath 'scripts/client/client_setup.py'
    if (-not (Test-Path -LiteralPath $setupPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $PythonPath -PathType Leaf) -or
        $RootPath.Contains('"') -or $setupPath.Contains('"')) {
        throw 'The setup files are unavailable. Keep the complete installation folder together.'
    }

    $form = [Windows.Forms.Form]::new()
    $form.Text = 'CSD Attendance - Administrator setup'
    $form.Font = [Drawing.Font]::new('Segoe UI', 10)
    $form.AutoScaleMode = [Windows.Forms.AutoScaleMode]::Dpi
    $form.AutoScaleDimensions = [Drawing.SizeF]::new(96, 96)
    $form.ClientSize = [Drawing.Size]::new(560, 650)
    $form.MinimumSize = [Drawing.Size]::new(380, 340)
    $form.StartPosition = 'CenterScreen'
    $form.MaximizeBox = $false
    $form.BackColor = [Drawing.Color]::White

    $scroll = [Windows.Forms.Panel]::new()
    $scroll.Dock = 'Fill'
    $scroll.AutoScroll = $true
    $scroll.Padding = [Windows.Forms.Padding]::new(24)
    $layout = [Windows.Forms.TableLayoutPanel]::new()
    $layout.Dock = 'Top'
    $layout.AutoSize = $true
    $layout.ColumnCount = 1
    [void]$layout.ColumnStyles.Add([Windows.Forms.ColumnStyle]::new('Percent', 100))
    [void]$scroll.Controls.Add($layout)
    [void]$form.Controls.Add($scroll)

    $heading = [Windows.Forms.Label]::new()
    $heading.Text = 'Create your administrator account'
    $heading.Font = [Drawing.Font]::new('Segoe UI', 15, [Drawing.FontStyle]::Bold)
    $heading.AutoSize = $true
    $heading.Margin = [Windows.Forms.Padding]::new(0, 0, 0, 12)
    [void]$layout.Controls.Add($heading)
    $intro = [Windows.Forms.Label]::new()
    $intro.Text = 'This Dean account manages CSD Attendance on this computer. Use credentials you can keep securely.'
    $intro.AutoSize = $true
    $intro.Margin = [Windows.Forms.Padding]::new(0, 0, 0, 16)
    [void]$layout.Controls.Add($intro)

    $fields = @{}
    foreach ($definition in @(
        @{Key='Username'; Label='&Username'; Secret=$false},
        @{Key='Password'; Label='&Password'; Secret=$true},
        @{Key='Confirmation'; Label='&Confirm password'; Secret=$true}
    )) {
        $label = [Windows.Forms.Label]::new()
        $label.Text = $definition.Label
        $label.AutoSize = $true
        $label.Margin = [Windows.Forms.Padding]::new(0, 8, 0, 4)
        [void]$layout.Controls.Add($label)
        $field = [Windows.Forms.TextBox]::new()
        $field.Dock = 'Top'
        $field.AccessibleName = $definition.Label.Replace('&', '')
        $field.UseSystemPasswordChar = $definition.Secret
        $field.MaxLength = if ($definition.Secret) { 256 } else { 128 }
        $field.Margin = [Windows.Forms.Padding]::new(0, 0, 0, 4)
        $fields[$definition.Key] = $field
        [void]$layout.Controls.Add($field)
    }
    $show = [Windows.Forms.CheckBox]::new()
    $show.Text = '&Show passwords'
    $show.AutoSize = $true
    $show.Margin = [Windows.Forms.Padding]::new(0, 8, 0, 12)
    $show.Add_CheckedChanged({
        $fields.Password.UseSystemPasswordChar = -not $show.Checked
        $fields.Confirmation.UseSystemPasswordChar = -not $show.Checked
    })
    [void]$layout.Controls.Add($show)
    $rules = [Windows.Forms.Label]::new()
    $rules.AccessibleName = 'Password requirements'
    $rules.Text = 'Use at least 8 characters, including an uppercase letter, a lowercase letter, a number and a special character.'
    $rules.AutoSize = $true
    $rules.Margin = [Windows.Forms.Padding]::new(0, 0, 0, 12)
    [void]$layout.Controls.Add($rules)
    $guidanceSummary = [Windows.Forms.Label]::new()
    $guidanceSummary.AutoSize = $true
    $guidanceSummary.AccessibleName = 'Password requirements feedback'
    $guidanceSummary.Margin = [Windows.Forms.Padding]::new(0, 0, 0, 8)
    [void]$layout.Controls.Add($guidanceSummary)
    $matchHint = [Windows.Forms.Label]::new()
    $matchHint.AutoSize = $true
    $matchHint.AccessibleName = 'Password confirmation feedback'
    $matchHint.Margin = [Windows.Forms.Padding]::new(0, 0, 0, 12)
    [void]$layout.Controls.Add($matchHint)
    $policy = Get-CsdSetupPasswordPolicy $PythonPath $setupPath $RootPath
    $ruleLabels = [ordered]@{length='At least 8 characters'; upper='An uppercase letter'; lower='A lowercase letter'; digit='A number'; special='A special character'}
    $refreshGuidance = {
        $matchHint.Text = if (-not $fields.Confirmation.Text) { 'Enter the same new password again.' }
            elseif ([string]::Equals($fields.Password.Text, $fields.Confirmation.Text, [StringComparison]::Ordinal)) { 'Passwords match' }
            else { 'Passwords do not match yet.' }
        if ($null -eq $policy) {
            $guidanceSummary.Text = 'Live checks are unavailable. Requirements will be checked when you create the account.'
            return
        }
        $feedback = Get-CsdPasswordFeedback $fields.Password.Text $fields.Confirmation.Text $policy
        $lines = foreach ($name in $ruleLabels.Keys) {
            $prefix = if ($feedback.Rules[$name]) { '[OK] ' } else { '[ ] ' }
            $prefix + $ruleLabels[$name]
        }
        $rules.Text = "Password requirements`r`n" + ($lines -join "`r`n")
        $guidanceSummary.Text = if ($feedback.Valid) { 'Meets password requirements' }
            elseif (-not $fields.Password.Text) { 'Use a password that meets all five requirements.' }
            else { 'Complete the remaining password requirements above.' }
    }
    $fields.Password.Add_TextChanged($refreshGuidance)
    $fields.Confirmation.Add_TextChanged($refreshGuidance)
    & $refreshGuidance
    $status = [Windows.Forms.Label]::new()
    $status.AutoSize = $true
    $status.AccessibleName = 'Setup status'
    $status.ForeColor = [Drawing.Color]::FromArgb(160, 30, 30)
    $status.Margin = [Windows.Forms.Padding]::new(0, 0, 0, 12)
    [void]$layout.Controls.Add($status)

    $buttons = [Windows.Forms.FlowLayoutPanel]::new()
    $buttons.FlowDirection = 'RightToLeft'
    $buttons.Dock = 'Bottom'
    $buttons.AutoSize = $true
    $buttons.Padding = [Windows.Forms.Padding]::new(16, 10, 16, 14)
    $submit = [Windows.Forms.Button]::new()
    $submit.Text = '&Create account'
    $submit.AutoSize = $true
    $submit.Padding = [Windows.Forms.Padding]::new(12, 6, 12, 6)
    $cancel = [Windows.Forms.Button]::new()
    $cancel.Text = 'Cancel'
    $cancel.AutoSize = $true
    $cancel.Padding = [Windows.Forms.Padding]::new(12, 6, 12, 6)
    [void]$buttons.Controls.Add($submit)
    [void]$buttons.Controls.Add($cancel)
    [void]$form.Controls.Add($buttons)
    $buttons.BringToFront()
    $form.AcceptButton = $submit
    $form.CancelButton = $cancel

    $state = @{ Running=$false; Process=$null; Clock=$null; Success=$false; CleanupConfirmed=$true }
    $timer = [Windows.Forms.Timer]::new()
    $timer.Interval = 100
    $setBusy = {
        param([bool]$busy)
        $state.Running = $busy
        foreach ($field in $fields.Values) { $field.Enabled = -not $busy }
        $show.Enabled = -not $busy
        $submit.Enabled = -not $busy
        $cancel.Enabled = $true
        $form.UseWaitCursor = $busy
    }
    $disposeChild = {
        $timer.Stop()
        if ($null -ne $state.Process) {
            try { $state.Process.Dispose() }
            catch { $state.CleanupConfirmed=$false; $script:CsdResultCode='CLEANUP_UNCONFIRMED' }
            finally { $state.Process = $null }
        }
    }
    $timer.Add_Tick({
        try {
            if ($state.Clock.Elapsed.TotalSeconds -gt 120) {
                $script:CsdResultCode='CHILD_TIMEOUT'
                & $disposeChild
                & $setBusy $false
                $submit.Enabled = $false
                $status.Text = if ($state.CleanupConfirmed) { 'Setup took too long and was stopped. Close this window and reopen setup to check the saved progress. Existing data was not replaced.' }
                    else { 'Windows did not confirm that setup stopped. Keep this installation closed until your technician confirms cleanup.' }
                return
            }
            if ($state.Process.OutputExceeded) { $script:CsdResultCode='CHILD_OUTPUT_LIMIT'; throw 'Invalid setup output' }
            if (-not $state.Process.Wait(0) -or -not $state.Process.OutputComplete) { return }
            $exitCode = $state.Process.ExitCode
            $result = $state.Process.Output | ConvertFrom-Json
            if ($result.ok -isnot [bool] -or $result.message -isnot [string] -or $result.message.Length -gt 2048) {
                throw 'Invalid setup result'
            }
            & $disposeChild
            & $setBusy $false
            if (-not $state.CleanupConfirmed) {
                $submit.Enabled=$false
                $status.Text='Windows did not confirm that setup stopped. Keep this installation closed until your technician confirms cleanup.'
                return
            }
            if ($state.CleanupConfirmed -and $exitCode -eq 0 -and $result.ok -and $result.status -cin @('initialized','resumed','ready')) {
                $state.Success = $true
                $form.DialogResult = [Windows.Forms.DialogResult]::OK
                $form.Close()
            }
            else {
                if ($exitCode -eq 3) {
                    $script:CsdResultCode='CLIENT_STORAGE_ACCESS'
                    $script:CsdSuppressPrivacyReceipt=$true
                    $submit.Enabled=$false
                    $status.Text='Private installation permissions could not be verified. Keep existing files unchanged and ask your technician for an owner-private local destination. No permissions were changed.'
                    return
                }
                $script:CsdResultCode='ADMINISTRATOR_SETUP'
                $status.Text = $result.message
                $retryable = $null -ne $result.PSObject.Properties['retryable_input'] -and
                    $result.retryable_input -is [bool] -and $result.retryable_input
                if (-not $retryable -and (@('.env','.client-state','database\attendance.db') | Where-Object { Test-Path -LiteralPath (Join-Path $RootPath $_) })) {
                    $submit.Enabled = $false
                    $status.Text += ' Close this window and reopen setup to check the saved progress.'
                }
                $fields.Password.Focus() | Out-Null
            }
        }
        catch {
            if ($null -eq $script:CsdResultCode) { $script:CsdResultCode='SETUP_RESULT_UNKNOWN' }
            & $disposeChild
            & $setBusy $false
            $submit.Enabled = $false
            $status.Text = if ($state.CleanupConfirmed) { 'The setup result could not be confirmed. Close this window and reopen setup to check the saved progress. Existing data was not replaced.' }
                else { 'Windows did not confirm that setup stopped. Keep this installation closed until your technician confirms cleanup.' }
        }
    })
    $submit.Add_Click({
        if ($state.Running -or -not $state.CleanupConfirmed) { return }
        $status.Text = ''
        if ([string]::IsNullOrWhiteSpace($fields.Username.Text)) {
            $status.Text = 'Enter a username.'
            $fields.Username.Focus() | Out-Null
            return
        }
        if ([string]::IsNullOrEmpty($fields.Password.Text)) {
            $status.Text = 'Enter a password.'
            $fields.Password.Focus() | Out-Null
            return
        }
        if (-not [string]::Equals($fields.Password.Text, $fields.Confirmation.Text, [StringComparison]::Ordinal)) {
            $status.Text = 'The two passwords do not match. Check them and try again.'
            $fields.Confirmation.Focus() | Out-Null
            return
        }
        if ($null -ne $policy) {
            $feedback = Get-CsdPasswordFeedback $fields.Password.Text $fields.Confirmation.Text $policy
            if (-not $feedback.Valid) {
                $status.Text = 'Complete the password requirements above, then try again.'
                $fields.Password.Focus() | Out-Null
                return
            }
        }
        try {
            $script:CsdResultCode=$null
            & $setBusy $true
            $show.Checked = $false
            $status.Text = 'Creating and checking your account. Please keep this window open.'
            $payload = @{username=$fields.Username.Text.Trim(); password=$fields.Password.Text; confirmation=$fields.Confirmation.Text} | ConvertTo-Json -Compress
            try {
                $env:PYTHONIOENCODING='utf-8'
                $state.Process=New-CsdCapturedSetupProcess -FilePath $PythonPath -Arguments @($setupPath,'initialize','--root',$RootPath,'--credentials-stdin','--json') -WorkingDirectory $RootPath -InputText $payload
            }
            finally {
                $payload = $null
            }
            $state.Clock = [Diagnostics.Stopwatch]::StartNew()
            $timer.Start()
        }
        catch {
            if ($null -eq $script:CsdResultCode) { $script:CsdResultCode='SETUP_RESULT_UNKNOWN' }
            & $disposeChild
            & $setBusy $false
            $submit.Enabled = $false
            $status.Text = 'The account setup could not start or finish. Close this window and reopen setup to check the saved progress.'
        }
    })
    $cancel.Add_Click({ $form.Close() })
    $form.Add_FormClosing({
        if ($state.Running) {
            & $disposeChild
            $script:CsdResultCode=if ($state.CleanupConfirmed) { 'SETUP_CANCELLED' } else { 'CLEANUP_UNCONFIRMED' }
        }
        elseif (-not $state.Success -and $null -eq $script:CsdResultCode) { $script:CsdResultCode='SETUP_CANCELLED' }
    })
    $resizeText = {
        $width = [Math]::Max(220, $scroll.ClientSize.Width - $scroll.Padding.Horizontal - 24)
        foreach ($label in @($heading,$intro,$rules,$guidanceSummary,$matchHint,$status)) {
            $label.MaximumSize = [Drawing.Size]::new($width, 0)
        }
    }
    $scroll.Add_SizeChanged($resizeText)
    $form.Add_Shown({
        $area = [Windows.Forms.Screen]::FromControl($form).WorkingArea
        if ($form.Height -gt $area.Height - 40) { $form.Height = $area.Height - 40 }
        if ($form.Width -gt $area.Width - 40) { $form.Width = $area.Width - 40 }
        & $resizeText
        $fields.Username.Focus() | Out-Null
    })
    try {
        [void]$form.ShowDialog()
        return [bool]$state.Success
    }
    finally {
        & $disposeChild
        $timer.Dispose()
        foreach ($field in $fields.Values) { $field.Clear() }
        $form.Dispose()
    }
}
