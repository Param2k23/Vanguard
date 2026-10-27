# Register from Windows PowerShell (or invoke from WSL with powershell.exe -File).
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory=$true)][string]$LinuxRoot,
    [Parameter(Mandatory=$true)][string]$Distribution,
    [Parameter(Mandatory=$true)][string]$LinuxUser,
    [ValidateSet('Install','Status','SmokeTest','Remove')][string]$Mode = 'Install',
    [ValidateSet('Limited','Highest')][string]$RunLevel = 'Highest'
)
$ErrorActionPreference = 'Stop'
$taskName = 'Vanguard-Daily-Pipeline'
$testName = 'Vanguard-Schedule-Check'
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($LinuxRoot -notmatch '^/[A-Za-z0-9_./-]+$' -or $LinuxUser -notmatch '^[a-z_][a-z0-9_-]*$' -or $Distribution -match '["\r\n]') {
    throw 'Use a Linux repository path without whitespace/quotes and a valid Linux username.'
}
if ($Mode -eq 'Status') {
    $task = Get-ScheduledTask -TaskName $taskName
    $info = $task | Get-ScheduledTaskInfo
    [pscustomobject]@{
        Name=$task.TaskName; State=[string]$task.State; NextRun=$info.NextRunTime.ToString('o')
        LastRun=$info.LastRunTime.ToString('o'); LastResult=$info.LastTaskResult
        LogonType=[string]$task.Principal.LogonType; RunLevel=[string]$task.Principal.RunLevel; TimeZone=(Get-TimeZone).Id
        WakeToRun=$task.Settings.WakeToRun; CatchUp=$task.Settings.StartWhenAvailable
        Triggers=@($task.Triggers | Select-Object StartBoundary,DaysInterval,Enabled)
        WakeTimerPolicy=(& "$env:WINDIR\System32\powercfg.exe" /query SCHEME_CURRENT SUB_SLEEP RTCWAKE)
    } | ConvertTo-Json -Depth 6
    exit 0
}
if ($Mode -eq 'Remove') {
    if ($PSCmdlet.ShouldProcess($taskName, 'Unregister Vanguard schedule')) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    exit 0
}
# Arizona does not observe daylight saving time. Do not silently change system timezone.
if ((Get-TimeZone).Id -ne 'US Mountain Standard Time') {
    throw 'This schedule is 06:00 and 12:00 Arizona time. Set matching trigger times explicitly for your Windows timezone before installation.'
}
$modeArgument = if ($Mode -eq 'SmokeTest') { 'doctor' } else { 'run' }
$actionArgs = '--distribution "{0}" --user "{1}" --exec /bin/bash {2}/scripts/run-scheduled {3}' -f $Distribution, $LinuxUser, $LinuxRoot, $modeArgument
$bootstrapDir = Join-Path $env:LOCALAPPDATA 'Vanguard'
if (-not $WhatIfPreference) { New-Item -ItemType Directory -Force -Path $bootstrapDir | Out-Null }
$bootstrapLog = Join-Path $bootstrapDir 'scheduler-bootstrap.log'
# Keep bootstrap diagnostics on Windows: failures starting WSL happen before Linux logging.
$bootstrap = '& "{0}\System32\wsl.exe" {1} *> "{2}"; exit $LASTEXITCODE' -f $env:WINDIR, $actionArgs, $bootstrapLog
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($bootstrap))
$action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument "-NoProfile -NonInteractive -EncodedCommand $encoded"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 30) -ExecutionTimeLimit ([TimeSpan]::Zero)
# S4U avoids storing a Windows password and can launch while logged off. The smoke
# test checks that this account can actually open its WSL distribution and credentials.
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType S4U -RunLevel $RunLevel
if ($Mode -eq 'SmokeTest') {
    if (Get-ScheduledTask -TaskName $testName -ErrorAction SilentlyContinue) { throw "$testName already exists; inspect it before replacing." }
    if ($PSCmdlet.ShouldProcess($testName, 'Register and start read-only scheduled prerequisite check')) {
        Register-ScheduledTask -TaskName $testName -Action $action -Settings $settings -Principal $principal -Description 'Temporary Vanguard WSL prerequisite/authentication check; no scraping or generation' | Out-Null
        Start-ScheduledTask -TaskName $testName
        Write-Output "Started $testName. Inspect its result, then unregister this temporary check."
    }
    exit 0
}
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw "$taskName already exists; inspect or remove it explicitly before reinstalling."
}
$triggers = @((New-ScheduledTaskTrigger -Daily -At '06:00'), (New-ScheduledTaskTrigger -Daily -At '12:00'))
$task = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description 'Vanguard scrape, resume generation, verification and export. Every day at 06:00 and 12:00 Arizona time. No auto-applications.'
if ($PSCmdlet.ShouldProcess($taskName, 'Install twice-daily pipeline with wake, missed-run catch-up and two failure retries')) {
    Register-ScheduledTask -TaskName $taskName -InputObject $task | Out-Null
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    [pscustomobject]@{ Name=$taskName; NextRun=$info.NextRunTime.ToString('o'); TimeZone=(Get-TimeZone).Id; LogonType='S4U' } | ConvertTo-Json
}
