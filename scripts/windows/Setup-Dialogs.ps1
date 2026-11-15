# Windows PowerShell 5.1 UI only. These helpers never change system settings.
# Callers must validate their action again after the operator makes a choice.
function Show-CsdSetupChoice {
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][string]$Message,
        [AllowEmptyCollection()][object[]]$Choices = @(),
        [string]$CancelText = 'Cancel'
    )
    if ($Choices.Count -gt 6 -or $Title.Length -gt 100 -or $Message.Length -gt 8000) {
        throw 'The setup question could not be displayed.'
    }
    $values = @{}
    foreach ($choice in $Choices) {
        if ([string]$choice.Value -notmatch '\A[a-zA-Z0-9_-]{1,32}\z' -or
            $values.ContainsKey([string]$choice.Value) -or
            [string]::IsNullOrWhiteSpace([string]$choice.Label) -or
            ([string]$choice.Label).Length -gt 100 -or ([string]$choice.Detail).Length -gt 2000) {
            throw 'The setup choices could not be displayed.'
        }
        $values[[string]$choice.Value] = $true
    }
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [Windows.Forms.Application]::EnableVisualStyles()
    $state = @{ Choice=$null }
    $form = [Windows.Forms.Form]::new()
    $form.Text = 'CSD Attendance - ' + $Title
    $form.Font = [Drawing.Font]::new('Segoe UI', 10)
    $form.AutoScaleMode = [Windows.Forms.AutoScaleMode]::Dpi
    $form.AutoScaleDimensions = [Drawing.SizeF]::new(96,96)
    $form.ClientSize = [Drawing.Size]::new(580, 520)
    $form.MinimumSize = [Drawing.Size]::new(360, 300)
    $form.StartPosition = 'CenterScreen'
    $form.BackColor = [Drawing.Color]::White
    $form.MaximizeBox = $false
    $scroll = [Windows.Forms.Panel]::new()
    $scroll.Dock = 'Fill'
    $scroll.AutoScroll = $true
    $scroll.Padding = [Windows.Forms.Padding]::new(24)
    $layout = [Windows.Forms.TableLayoutPanel]::new()
    $layout.Dock = 'Top'
    $layout.AutoSize = $true
    $layout.ColumnCount = 1
    [void]$layout.ColumnStyles.Add([Windows.Forms.ColumnStyle]::new('Percent',100))
    [void]$scroll.Controls.Add($layout)
    [void]$form.Controls.Add($scroll)
    $wrapping = [Collections.Generic.List[Windows.Forms.Control]]::new()
    $heading = [Windows.Forms.Label]::new()
    $heading.Text = $Title
    $heading.UseMnemonic = $false
    $heading.Font = [Drawing.Font]::new('Segoe UI',15,[Drawing.FontStyle]::Bold)
    $heading.AutoSize = $true
    $heading.Margin = [Windows.Forms.Padding]::new(0,0,0,12)
    [void]$layout.Controls.Add($heading)
    $wrapping.Add($heading)
    $description = [Windows.Forms.Label]::new()
    $description.Text = $Message
    $description.UseMnemonic = $false
    $description.AutoSize = $true
    $description.Margin = [Windows.Forms.Padding]::new(0,0,0,18)
    [void]$layout.Controls.Add($description)
    $wrapping.Add($description)
    foreach ($choice in $Choices) {
        $button = [Windows.Forms.Button]::new()
        $button.Text = [string]$choice.Label
        $button.AccessibleName = [string]$choice.Label
        $button.AccessibleDescription = [string]$choice.Detail
        $button.Tag = [string]$choice.Value
        $button.AutoSize = $true
        $button.Dock = 'Top'
        $button.Padding = [Windows.Forms.Padding]::new(10,8,10,8)
        $button.Margin = [Windows.Forms.Padding]::new(0,8,0,4)
        $button.Add_Click({ param($sender, $eventArgs); $state.Choice=[string]$sender.Tag; $form.Close() })
        [void]$layout.Controls.Add($button)
        $detail = [Windows.Forms.Label]::new()
        $detail.Text = [string]$choice.Detail
        $detail.UseMnemonic = $false
        $detail.AutoSize = $true
        $detail.Margin = [Windows.Forms.Padding]::new(4,0,0,8)
        [void]$layout.Controls.Add($detail)
        $wrapping.Add($detail)
    }
    $footer = [Windows.Forms.FlowLayoutPanel]::new()
    $footer.Dock = 'Bottom'
    $footer.FlowDirection = 'RightToLeft'
    $footer.AutoSize = $true
    $footer.Padding = [Windows.Forms.Padding]::new(16,10,16,14)
    $cancel = [Windows.Forms.Button]::new()
    $cancel.Text = $CancelText
    $cancel.AutoSize = $true
    $cancel.Padding = [Windows.Forms.Padding]::new(12,6,12,6)
    $cancel.Add_Click({ $form.Close() })
    [void]$footer.Controls.Add($cancel)
    [void]$form.Controls.Add($footer)
    $footer.BringToFront()
    # Enter and Escape initially cancel. A security-affecting choice always
    # requires selecting its named button; opening a dialog is not consent.
    $form.AcceptButton = $cancel
    $form.CancelButton = $cancel
    $resize = {
        $width = [Math]::Max(220, $scroll.ClientSize.Width - $scroll.Padding.Horizontal - 24)
        foreach ($control in $wrapping) { $control.MaximumSize = [Drawing.Size]::new($width,0) }
    }
    $scroll.Add_SizeChanged($resize)
    $form.Add_Shown({
        $area = [Windows.Forms.Screen]::FromControl($form).WorkingArea
        if ($form.Height -gt $area.Height-40) { $form.Height=$area.Height-40 }
        if ($form.Width -gt $area.Width-40) { $form.Width=$area.Width-40 }
        & $resize
        [void]$cancel.Focus()
    })
    try { [void]$form.ShowDialog(); return $state.Choice }
    finally { $form.Dispose() }
}

function Show-CsdSetupNotice {
    param([Parameter(Mandatory = $true)][string]$Title,
          [Parameter(Mandatory = $true)][string]$Message)
    [void](Show-CsdSetupChoice -Title $Title -Message $Message -Choices @() -CancelText 'Close')
}
