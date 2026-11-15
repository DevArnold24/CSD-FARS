# Loading this library never connects to Task Scheduler or changes the computer.
. (Join-Path $PSScriptRoot "Lan-Common.ps1")

function Get-CsdAutostartIdentity {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    return [pscustomobject]@{ Sid = $identity.User.Value; Name = $identity.Name }
}

function Get-CsdAutostartSpecification {
    param([string]$Root, [string]$OwnerSid, [string]$WindowsDirectory)
    $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd("\")
    if ($rootPath -match '["\r\n]' -or $OwnerSid -notmatch '^S-1-5-21-(\d+-){3}\d+$') {
        throw "A normal installation path and a named Windows user account are required."
    }
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha256.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($rootPath.ToUpperInvariant()))
        $suffix = -join @($hash[0..15] | ForEach-Object { $_.ToString("x2") })
    }
    finally { $sha256.Dispose() }
    return [pscustomobject]@{
        Root = $rootPath
        OwnerSid = $OwnerSid
        Name = "CSD Attendance LAN - $suffix"
        Executable = [System.IO.Path]::Combine($WindowsDirectory, "System32\WindowsPowerShell\v1.0\powershell.exe")
        Arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "{0}" -NoBrowser' -f (
            [System.IO.Path]::Combine($rootPath, "scripts\lan\Start-CSD-LAN.ps1")
        )
        Description = "CSD Attendance optional LAN autostart v1 | $rootPath | $OwnerSid"
        Sddl = "O:${OwnerSid}G:SYD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;${OwnerSid})"
    }
}

function New-CsdAutostartXml {
    param([Parameter(Mandatory = $true)]$Specification)
    $owner = [System.Security.SecurityElement]::Escape($Specification.OwnerSid)
    $description = [System.Security.SecurityElement]::Escape($Specification.Description)
    $executable = [System.Security.SecurityElement]::Escape($Specification.Executable)
    $arguments = [System.Security.SecurityElement]::Escape($Specification.Arguments)
    $root = [System.Security.SecurityElement]::Escape($Specification.Root)
    # No registration trigger: enabling never starts the application implicitly.
    # AC power is required to start; losing AC does not abruptly stop attendance.
    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>$description</Description></RegistrationInfo>
  <Triggers><BootTrigger><Enabled>true</Enabled><Delay>PT30S</Delay></BootTrigger></Triggers>
  <Principals><Principal id="Owner"><UserId>$owner</UserId><LogonType>Password</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>false</Enabled><Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>10</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Owner"><Exec><Command>$executable</Command><Arguments>$arguments</Arguments><WorkingDirectory>$root</WorkingDirectory></Exec></Actions>
</Task>
"@
}

function Assert-CsdAutostartDefinition {
    param([string]$Xml, [Parameter(Mandatory = $true)]$Specification)
    # Task metadata is untrusted: prohibit DTDs/external entities and cap its size.
    if ($Xml.Length -gt 65536) { throw "The existing task definition is unexpectedly large." }
    $readerSettings = [System.Xml.XmlReaderSettings]::new()
    $readerSettings.DtdProcessing = [System.Xml.DtdProcessing]::Prohibit
    $readerSettings.XmlResolver = $null
    $stringReader = [System.IO.StringReader]::new($Xml)
    $reader = [System.Xml.XmlReader]::Create($stringReader, $readerSettings)
    $document = [System.Xml.XmlDocument]::new()
    $document.XmlResolver = $null
    try { $document.Load($reader) }
    finally { $reader.Dispose(); $stringReader.Dispose() }
    $ns = [System.Xml.XmlNamespaceManager]::new($document.NameTable)
    $ns.AddNamespace("t", "http://schemas.microsoft.com/windows/2004/02/mit/task")
    $expected = @{
        "RegistrationInfo/Description" = $Specification.Description
        "Principals/Principal/UserId" = $Specification.OwnerSid
        "Principals/Principal/LogonType" = "Password"
        "Principals/Principal/RunLevel" = "LeastPrivilege"
        "Triggers/BootTrigger/Enabled" = "true"
        "Triggers/BootTrigger/Delay" = "PT30S"
        "Settings/MultipleInstancesPolicy" = "IgnoreNew"
        "Settings/ExecutionTimeLimit" = "PT0S"
        "Settings/RestartOnFailure/Interval" = "PT1M"
        "Settings/RestartOnFailure/Count" = "10"
        "Settings/DisallowStartIfOnBatteries" = "true"
        "Settings/StopIfGoingOnBatteries" = "false"
        "Settings/AllowHardTerminate" = "true"
        "Settings/StartWhenAvailable" = "true"
        "Settings/RunOnlyIfNetworkAvailable" = "false"
        "Settings/AllowStartOnDemand" = "true"
        "Settings/RunOnlyIfIdle" = "false"
        "Settings/WakeToRun" = "false"
        "Actions/Exec/Command" = $Specification.Executable
        "Actions/Exec/Arguments" = $Specification.Arguments
        "Actions/Exec/WorkingDirectory" = $Specification.Root
    }
    foreach ($path in $expected.Keys) {
        $nodes = @($document.SelectNodes(("/t:Task/t:" + $path.Replace("/", "/t:")), $ns))
        if ($nodes.Count -ne 1 -or $nodes[0].InnerText -cne $expected[$path]) {
            throw ("An existing task does not match this installation's managed field '{0}'. It was not changed." -f $path)
        }
    }
    foreach ($path in @("Actions", "Triggers", "Principals")) {
        $containers = @($document.SelectNodes("/t:Task/t:$path", $ns))
        $nodes = @($document.SelectNodes("/t:Task/t:$path/*", $ns))
        if ($containers.Count -ne 1 -or $nodes.Count -ne 1) {
            throw "The existing task contains unexpected actions, triggers, or principals. It was not changed."
        }
    }
    $boot = $document.SelectSingleNode("/t:Task/t:Triggers/t:BootTrigger", $ns)
    if (@($boot.SelectNodes("*")).Count -ne 2 -or
        $document.SelectSingleNode("/t:Task/t:Actions", $ns).GetAttribute("Context") -cne "Owner" -or
        $document.SelectSingleNode("/t:Task/t:Principals/t:Principal", $ns).GetAttribute("id") -cne "Owner") {
        throw "The existing task contains unexpected execution context or boot conditions. It was not changed."
    }
}

function Assert-CsdAutostartTaskSecurity {
    param([string]$Sddl, [string]$OwnerSid)
    $descriptor = [System.Security.AccessControl.RawSecurityDescriptor]::new($Sddl)
    if ($null -eq $descriptor.Owner -or $descriptor.Owner.Value -ne $OwnerSid -or
        $null -eq $descriptor.DiscretionaryAcl -or
        ($descriptor.ControlFlags -band [System.Security.AccessControl.ControlFlags]::DiscretionaryAclProtected) -eq 0) {
        throw "The existing task owner or permissions do not match this Windows user. It was not changed."
    }
    $allowed = @($OwnerSid, "S-1-5-18", "S-1-5-32-544")
    $seen = @{}
    foreach ($ace in $descriptor.DiscretionaryAcl) {
        if ($ace -isnot [System.Security.AccessControl.CommonAce] -or
            $ace.AceType -ne [System.Security.AccessControl.AceType]::AccessAllowed -or
            $ace.SecurityIdentifier.Value -notin $allowed -or $ace.IsCallback -or
            $ace.AceFlags -ne [System.Security.AccessControl.AceFlags]::None) {
            throw "The existing task has unexpected permissions. It was not changed."
        }
        if (($ace.AccessMask -band 0x10000000) -eq 0x10000000 -or
            ($ace.AccessMask -band 0x1f01ff) -eq 0x1f01ff) {
            $seen[$ace.SecurityIdentifier.Value] = $true
        }
    }
    foreach ($sid in $allowed) {
        if (-not $seen.ContainsKey($sid)) { throw "The existing task is missing required owner/system permissions." }
    }
}

function Get-CsdAutostartFolder {
    $service = New-Object -ComObject "Schedule.Service"
    $service.Connect()
    return $service.GetFolder("\")
}

function Get-CsdAutostartTask {
    param($Folder, [string]$Name)
    try { return $Folder.GetTask($Name) }
    catch {
        $errorException = $_.Exception
        while ($null -ne $errorException) {
            # Only ERROR_FILE_NOT_FOUND means absent, not access denied or service failure.
            if ($errorException.HResult -eq -2147024894) { return $null }
            $errorException = $errorException.InnerException
        }
        throw "The exact scheduled task could not be inspected. No task was created or changed."
    }
}

function Assert-CsdAutostartOwnedTask {
    param($Task, $Specification)
    if ([string]$Task.Path -cne ("\" + $Specification.Name)) {
        throw "The scheduler returned a different task. It was not changed."
    }
    Assert-CsdAutostartDefinition -Xml $Task.Xml -Specification $Specification
    Assert-CsdAutostartTaskSecurity -Sddl $Task.GetSecurityDescriptor(7) -OwnerSid $Specification.OwnerSid
}

function Assert-CsdAutostartAdministrator {
    $principal = [System.Security.Principal.WindowsPrincipal]::new(
        [System.Security.Principal.WindowsIdentity]::GetCurrent()
    )
    if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Creating before-sign-in startup requires an administrator PowerShell opened as the SAME Windows user who configured LAN. Run this command there with -Action Enable. Do not supply another administrator account."
    }
}

function Assert-CsdAutostartReady {
    param($Specification)
    $privatePath = Join-Path $Specification.Root ".lan-server"
    if (-not (Test-Path -LiteralPath $privatePath -PathType Container)) {
        throw "LAN is not initialized. Complete LAN_SERVER_FIRST_RUN.cmd in this installation first."
    }
    Assert-LanRootLocation
    Assert-LanPrivateStateIsLocal
    $owner = (Get-Acl -LiteralPath $privatePath).GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    if ($owner -ne $Specification.OwnerSid) {
        throw "Run this manager as the exact Windows user who configured this installation's LAN state."
    }
    # Reuse all existing installation/key binding, ACL, pinned Caddy/CA, network
    # identity and firewall checks. No setup, migration, repair or live probe.
    Invoke-LanNativeCommand -FilePath $Specification.Executable -Arguments @(
        "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        (Join-Path $Specification.Root "scripts\lan\Check-CSD-LAN.ps1")
    ) -FailureMessage "LAN readiness failed; stop the running server before this offline check, or repair LAN setup explicitly"
}

function Register-CsdAutostartTask {
    param($Folder, $Specification, [string]$AccountName)
    Write-Host "Windows will store this account's task-logon credentials. Enter its Windows PASSWORD, not a Windows Hello PIN."
    Write-Host "No password is accepted as an argument or saved in this installation. Cancel with Ctrl+C."
    $securePassword = Read-Host -Prompt ("Windows password for {0}" -f $AccountName) -AsSecureString
    $plainPassword = $null
    $credential = $null
    try {
        if ($null -eq $securePassword -or $securePassword.Length -eq 0) {
            throw "A nonblank Windows account password is required for before-sign-in startup."
        }
        $credential = [System.Management.Automation.PSCredential]::new($AccountName, $securePassword)
        # Task Scheduler's COM API requires a transient string. Never pass it to
        # another process, write it, include it in XML, or echo registration errors.
        $plainPassword = $credential.GetNetworkCredential().Password
        try {
            # TASK_CREATE only: even a registration race cannot replace a task.
            [void]$Folder.RegisterTask(
                $Specification.Name, (New-CsdAutostartXml -Specification $Specification),
                2, $Specification.OwnerSid, $plainPassword, 1, $Specification.Sddl
            )
        }
        catch {
            throw "Windows could not register the task. Check account password, batch-logon policy, elevation, and whether this task already exists. No existing task was overwritten."
        }
    }
    finally {
        # CLR strings cannot be guaranteed zeroed; keep the API conversion's
        # lifetime narrow and release the secure input in all failure paths.
        $plainPassword = $null
        $credential = $null
        if ($null -ne $securePassword) { $securePassword.Dispose() }
    }
}

function Invoke-CsdAutostart {
    param([ValidateSet("Enable", "Status", "Disable", "Start", "Stop")][string]$Action = "Status")
    $identity = Get-CsdAutostartIdentity
    $specification = Get-CsdAutostartSpecification -Root $script:LanRootPath `
        -OwnerSid $identity.Sid -WindowsDirectory $env:SystemRoot
    $folder = Get-CsdAutostartFolder
    $task = Get-CsdAutostartTask -Folder $folder -Name $specification.Name
    if ($null -ne $task) {
        Assert-CsdAutostartOwnedTask -Task $task -Specification $specification
    }
    if ($Action -eq "Status") {
        if ($null -eq $task) { Write-Host "Optional before-sign-in startup is NOT configured for this installation."; return }
        Write-Host ("Task: {0}" -f $specification.Name)
        Write-Host ("Enabled: {0}; Scheduler state: {1}; Last result: {2}" -f $task.Enabled, $task.State, $task.LastTaskResult)
        Write-Host "Scheduler state: 1=disabled, 2=queued, 3=ready, 4=running. This is not HTTPS readiness."
        Write-Host 'From this installation, run: powershell.exe -NoProfile -File ".\scripts\lan\Check-CSD-LAN.ps1" -Live'
        Write-Host "Then test from a browser laptop. After changing the Windows account password, update this exact task's stored credentials locally in Windows Task Scheduler and retest. Enable does not refresh credentials."
        return
    }
    if ($Action -eq "Enable") {
        if ($null -eq $task) { Assert-CsdAutostartAdministrator }
        Assert-CsdAutostartReady -Specification $specification
        if ($null -eq $task) {
            Register-CsdAutostartTask -Folder $folder -Specification $specification -AccountName $identity.Name
            $task = Get-CsdAutostartTask -Folder $folder -Name $specification.Name
            if ($null -eq $task) { throw "Windows did not return the newly registered task. Verify Task Scheduler before retrying." }
            Assert-CsdAutostartOwnedTask -Task $task -Specification $specification
        }
        $task.Enabled = $true
        Write-Host "Before-sign-in startup is enabled. It has NOT been started or boot-tested. Use -Action Start for an explicit test."
        Write-Host "Keep the server on AC power and awake. Startup waits 30 seconds and retries failures 10 times at one-minute intervals."
        Write-Host "After a Windows account password change, update this exact task's credentials locally in Task Scheduler and retest. Enable does not refresh the saved password."
        return
    }
    if ($null -eq $task) { throw "No managed startup task exists for this installation. Nothing was changed." }
    switch ($Action) {
        "Disable" {
            $task.Enabled = $false
            Write-Host "Automatic startup disabled. A running server was NOT stopped; use -Action Stop if required."
        }
        "Stop" {
            $task.Stop(0)
            Write-Host "Stop requested for this installation's task only. The automatic-startup setting was not changed; use -Action Disable to prevent future boot starts."
        }
        "Start" {
            if (-not $task.Enabled) { throw "The managed task is disabled. Enable it explicitly before starting it." }
            if ($task.State -in @(2, 4)) { Write-Host "The managed task is already queued or running; no duplicate was started."; return }
            Assert-CsdAutostartReady -Specification $specification
            [void]$task.Run($null)
            Write-Host "Start requested. This is not a health verdict; verify HTTPS and perform the before-sign-in acceptance test."
        }
    }
}
