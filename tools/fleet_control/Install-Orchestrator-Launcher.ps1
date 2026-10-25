<#
.SYNOPSIS
Installs the fixed standard-user Orchestrator launch task without starting it.
.DESCRIPTION
Run from the deployed Fleet directory, or supply its explicit -Root. The only
task action is the adjacent, fixed VBS launcher. No prompts, agent resumes,
daemon starts, credentials, elevation requests, or automatic triggers are used.

An existing task must already have the exact owned action, current user,
Limited/Interactive principal, IgnoreNew policy, and two-minute limit. Conflicts
are reported and left untouched; this installer never force-overwrites a task.
-WhatIf performs validation and read-only inspection only.
.EXAMPLE
.\Install-Orchestrator-Launcher.ps1 -WhatIf
.EXAMPLE
.\Install-Orchestrator-Launcher.ps1 -Root 'C:\Tools\AO-Fleet-Control'
#>
[CmdletBinding(SupportsShouldProcess=$true, ConfirmImpact='Medium')]
param(
    [Parameter()][string]$Root = $PSScriptRoot,
    [Parameter()][switch]$LoadFunctionsOnly
)

$ErrorActionPreference = 'Stop'
$script:FleetInstallerTaskName = 'MJobs Fleet - Orchestrator Standard'
$script:FleetInstallerTaskPath = '\'

function Resolve-FleetInstallerSid {
    param([Parameter(Mandatory)][string]$Identity)
    if ($Identity -match '^S-1-\d+(?:-\d+)+$') {
        return ([Security.Principal.SecurityIdentifier]::new($Identity)).Value
    }
    $account = [Security.Principal.NTAccount]::new($Identity)
    return $account.Translate([Security.Principal.SecurityIdentifier]).Value
}

function Get-FleetInstallerExistingTask {
    # Enumerating here avoids treating access errors as "task does not exist".
    # Multiple paths bearing this fixed name would also confuse the broker.
    $tasks = @(Get-ScheduledTask -ErrorAction Stop |
        Where-Object { $_.TaskName -eq $script:FleetInstallerTaskName })
    if ($tasks.Count -gt 1) { throw 'TASK_NAME_AMBIGUOUS: Multiple task paths use the fixed launcher name; none were changed.' }
    if ($tasks.Count -eq 0) { return $null }
    if ($tasks[0].TaskPath -cne $script:FleetInstallerTaskPath) {
        throw 'TASK_PATH_CONFLICT: The fixed launcher name exists outside the root task folder; it was not changed.'
    }
    return $tasks[0]
}

function Test-FleetInstallerExistingTask {
    param([Parameter(Mandatory)]$Task, [Parameter(Mandatory)]$Expected)
    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) { throw 'TASK_ACTION_CONFLICT: Existing task does not have exactly the owned launcher action.' }
    $execute = [string]$actions[0].Execute
    if ([string]::IsNullOrWhiteSpace($execute) -or
        -not [string]::Equals([IO.Path]::GetFullPath($execute), $Expected.HostPath,
            [StringComparison]::OrdinalIgnoreCase)) {
        throw 'TASK_HOST_CONFLICT: Existing task uses a different executable; it was not changed.'
    }
    if (-not [string]::Equals([string]$actions[0].Arguments, $Expected.Arguments,
        [StringComparison]::OrdinalIgnoreCase)) {
        throw 'TASK_ARGUMENT_CONFLICT: Existing task does not point exactly to this installation; it was not changed.'
    }
    $workingDirectory = [string]$actions[0].WorkingDirectory
    if (-not [string]::IsNullOrWhiteSpace($workingDirectory) -and
        -not [string]::Equals([IO.Path]::GetFullPath($workingDirectory), $Expected.Root,
            [StringComparison]::OrdinalIgnoreCase)) {
        throw 'TASK_DIRECTORY_CONFLICT: Existing task has a different working directory; it was not changed.'
    }
    if ([string]$Task.Principal.RunLevel -ne 'Limited' -or
        [string]$Task.Principal.LogonType -notin @('Interactive','InteractiveToken')) {
        throw 'TASK_PRINCIPAL_CONFLICT: Existing task is not Limited/Interactive; it was not changed.'
    }
    if ([string]::IsNullOrWhiteSpace([string]$Task.Principal.UserId) -or
        (Resolve-FleetInstallerSid ([string]$Task.Principal.UserId)) -ne $Expected.UserSid) {
        throw 'TASK_USER_CONFLICT: Existing task belongs to a different user; it was not changed.'
    }
    if (@($Task.Triggers | Where-Object { $null -ne $_ }).Count -ne 0) {
        throw 'TASK_TRIGGER_CONFLICT: Existing task has automatic triggers; they were not altered.'
    }
    if ([string]$Task.Settings.MultipleInstances -ne 'IgnoreNew') {
        throw 'TASK_INSTANCE_POLICY_CONFLICT: Existing task does not use IgnoreNew; it was not changed.'
    }
    try { $limit = [Xml.XmlConvert]::ToTimeSpan([string]$Task.Settings.ExecutionTimeLimit) }
    catch { throw 'TASK_TIME_LIMIT_UNVERIFIABLE: Existing task time limit could not be verified.' }
    if ($limit -ne [TimeSpan]::FromMinutes(2)) {
        throw 'TASK_TIME_LIMIT_CONFLICT: Existing task does not have the two-minute limit; it was not changed.'
    }
    if ($Task.Settings.Enabled -ne $true -or $Task.Settings.AllowDemandStart -ne $true) {
        throw 'TASK_DISABLED_PRESERVED: Existing task is disabled or demand-start is blocked; the user choice was not overridden.'
    }
    if ($Task.Settings.StartWhenAvailable -eq $true) {
        throw 'TASK_AUTOSTART_POLICY_CONFLICT: Existing task has StartWhenAvailable enabled; it was not changed.'
    }
    return $true
}

function New-FleetInstallerReport {
    param([string]$Status, $Expected)
    return [pscustomobject]@{
        status = $Status
        taskName = $script:FleetInstallerTaskName
        taskPath = $script:FleetInstallerTaskPath
        root = $Expected.Root
        runLevel = 'Limited'
        logonType = 'Interactive'
        multipleInstances = 'IgnoreNew'
        executionTimeLimitSeconds = 120
        automaticTriggers = 0
        started = $false
    }
}

if ($LoadFunctionsOnly) { return }

if ($env:OS -ne 'Windows_NT') { throw 'WINDOWS_REQUIRED: Task Scheduler installation must run on Windows.' }
if ([string]::IsNullOrWhiteSpace($Root) -or $Root -match '["\r\n\x00]') {
    throw 'INVALID_ROOT: Supply the explicit Fleet installation directory.'
}
$directory = Get-Item -LiteralPath $Root -Force -ErrorAction Stop
if (-not $directory.PSIsContainer -or $directory.PSProvider.Name -ne 'FileSystem') {
    throw 'INVALID_ROOT: Fleet root must be an existing filesystem directory.'
}
$installRoot = [IO.Path]::GetFullPath($directory.FullName).TrimEnd([char]'\')
if ($installRoot -eq [IO.Path]::GetPathRoot($directory.FullName).TrimEnd([char]'\')) {
    throw 'INVALID_ROOT: Use the specific Fleet installation directory, not a filesystem root.'
}
# Fixed adjacent filenames only; the request file can never choose executable
# names, script paths, task arguments, or scheduler credentials.
foreach ($required in @('Start-Orchestrator-Requested.vbs','Start-Orchestrator-Requested.ps1','orchestrator-launcher.js')) {
    $item = Get-Item -LiteralPath (Join-Path $installRoot $required) -Force -ErrorAction Stop
    if ($item.PSIsContainer -or (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw "INVALID_WORKER_FILE: $required must be a regular file in the Fleet installation."
    }
}
$hostPath = Join-Path $env:WINDIR 'System32\wscript.exe'
$powerShellPath = 'C:\Program Files\PowerShell\7\pwsh.exe'
foreach ($executable in @($hostPath,$powerShellPath)) {
    if (-not [IO.File]::Exists($executable)) { throw "REQUIRED_EXECUTABLE_MISSING: $executable" }
}
foreach ($command in @('Get-ScheduledTask','New-ScheduledTaskAction','New-ScheduledTaskPrincipal','New-ScheduledTaskSettingsSet','Register-ScheduledTask')) {
    [void](Get-Command -Name $command -CommandType Cmdlet,Function -ErrorAction Stop)
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
try {
    if ($null -eq $identity.User -or $identity.User.Value -in @('S-1-5-18','S-1-5-19','S-1-5-20')) {
        throw 'INTERACTIVE_USER_REQUIRED: Run this installer as the Windows user who will launch Fleet, not a service account.'
    }
    $userSid = $identity.User.Value
} finally { $identity.Dispose() }
$vbsPath = Join-Path $installRoot 'Start-Orchestrator-Requested.vbs'
$expected = [pscustomobject]@{
    Root = $installRoot
    HostPath = [IO.Path]::GetFullPath($hostPath)
    Arguments = '//B //NoLogo "' + $vbsPath + '"'
    UserSid = $userSid
}
$existing = Get-FleetInstallerExistingTask
if ($null -ne $existing) {
    [void](Test-FleetInstallerExistingTask -Task $existing -Expected $expected)
    # Idempotent no-op: preserve description, nonessential settings, ACL, state,
    # and scheduler history. There is deliberately no -Force or task deletion.
    New-FleetInstallerReport -Status 'already_installed' -Expected $expected
    return
}
if (-not $PSCmdlet.ShouldProcess("$($script:FleetInstallerTaskPath)$($script:FleetInstallerTaskName)",
    'Register fixed Limited/Interactive launcher with no triggers; do not start it')) {
    New-FleetInstallerReport -Status 'not_changed' -Expected $expected
    return
}
# A task appearing during confirmation must never be overwritten. Register is
# also deliberately called without -Force to fail safely on a later race.
if ($null -ne (Get-FleetInstallerExistingTask)) {
    throw 'TASK_APPEARED_DURING_INSTALL: No task was overwritten. Inspect it and rerun installation.'
}
$action = New-ScheduledTaskAction -Execute $expected.HostPath -Argument $expected.Arguments -WorkingDirectory $installRoot
$principal = New-ScheduledTaskPrincipal -UserId $userSid -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes(2)) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable:$false
[void](Register-ScheduledTask -TaskName $script:FleetInstallerTaskName -TaskPath $script:FleetInstallerTaskPath `
    -Action $action -Principal $principal -Settings $settings `
    -Description 'MJobs Fleet fixed standard-user launcher; explicit nonce request only; no automatic triggers or agent resume.' `
    -ErrorAction Stop)
$registered = Get-FleetInstallerExistingTask
if ($null -eq $registered) { throw 'TASK_REGISTRATION_UNVERIFIED: Registration returned but the fixed task was not found.' }
[void](Test-FleetInstallerExistingTask -Task $registered -Expected $expected)
New-FleetInstallerReport -Status 'installed' -Expected $expected
