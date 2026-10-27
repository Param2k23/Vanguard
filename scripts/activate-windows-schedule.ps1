# Run elevated by Windows UAC. Test the exact background identity before activation.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$LinuxRoot,
    [Parameter(Mandatory=$true)][string]$Distribution,
    [Parameter(Mandatory=$true)][string]$LinuxUser,
    [ValidateSet('Limited','Highest')][string]$RunLevel = 'Highest'
)
$ErrorActionPreference = 'Stop'
$repoWindows = Split-Path -Parent $PSScriptRoot
$resultDir = Join-Path $repoWindows 'var\scheduler'
New-Item -ItemType Directory -Force -Path $resultDir | Out-Null
$resultPath = Join-Path $resultDir 'windows-setup.json'
$result = @{ status='running'; started_at=(Get-Date).ToString('o') }
$result | ConvertTo-Json | Set-Content -Encoding UTF8 $resultPath
$installer = Join-Path $PSScriptRoot 'install-windows-schedule.ps1'
$testName = 'Vanguard-Schedule-Check'
$installedCheck = $false
try {
    & $installer -LinuxRoot $LinuxRoot -Distribution $Distribution -LinuxUser $LinuxUser -RunLevel $RunLevel -Mode SmokeTest
    $installedCheck = $true
    $deadline = (Get-Date).AddMinutes(3)
    do {
        Start-Sleep -Seconds 2
        $task = Get-ScheduledTask -TaskName $testName
        $info = Get-ScheduledTaskInfo -TaskName $testName
        if ($task.State -ne 'Running' -and $info.LastRunTime.Year -gt 2000) {
            if ($info.LastTaskResult -ne 0) {
                throw ('Scheduled prerequisite check failed: result {0} (0x{0:X}). See var/scheduler logs.' -f $info.LastTaskResult)
            }
            $result.check_result = 0
            break
        }
    } while ((Get-Date) -lt $deadline)
    if (-not $result.ContainsKey('check_result')) { throw 'Scheduled prerequisite check timed out. Inspect logs before enabling live runs.' }
    & $installer -LinuxRoot $LinuxRoot -Distribution $Distribution -LinuxUser $LinuxUser -RunLevel $RunLevel -Mode Install
    $info = Get-ScheduledTaskInfo -TaskName 'Vanguard-Daily-Pipeline'
    $result.status = 'installed'
    $result.task_name = 'Vanguard-Daily-Pipeline'
    $result.next_run = $info.NextRunTime.ToString('o')
    $result.timezone = (Get-TimeZone).Id
    $result.times = @('06:00','12:00')
} catch {
    $result.status = 'failed'
    $result.error = $_.Exception.Message
} finally {
    if ($installedCheck) {
        Stop-ScheduledTask -TaskName $testName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $testName -Confirm:$false -ErrorAction SilentlyContinue
    }
    $result.finished_at = (Get-Date).ToString('o')
    $result | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $resultPath
}
if ($result.status -ne 'installed') { Write-Error $result.error; exit 1 }
