# Run automatically every day

The recommended starting schedule is **06:00 and 12:00 America/Phoenix, Monday
through Sunday**, with the existing limit of 15 new jobs per run. That allows up to
30 new jobs per day; the resume stage also handles pending jobs from earlier runs.
Actual availability, filters, extraction failures and model limits can reduce output.

This is an operational recommendation, not a proven best hour for getting hired.
[Indeed's application guidance](https://www.indeed.com/career-advice/finding-a-job/what-is-best-day-to-apply-for-job)
advises applying promptly when a posting becomes available. Morning preparation and
a midday refresh give you two chances to apply to fresh roles without hourly model
batches. The schedule prepares files; you still submit the applications yourself.

## Windows + WSL installation

Use Windows Task Scheduler so WSL does not need to be open at trigger time. The
installer requires Windows administrator permission for its background logon setup.
From an **administrator PowerShell** window, substitute your own paths/user/distro:

```powershell
& '\\wsl.localhost\Ubuntu\home\YOUR_USER\Vanguard\scripts\activate-windows-schedule.ps1' `
    -LinuxRoot '/home/YOUR_USER/Vanguard' -Distribution Ubuntu -LinuxUser YOUR_USER
```

Keep your repository `.venv` and local pipeline configuration in place. For an
installation using nvm, the scheduled launcher finds installed Node/Codex binaries
without sourcing interactive `.bashrc`. To pin the installed tool directory, create
an ignored `config/schedule.local.json`:

```json
{"tool_bin": "/home/YOUR_USER/.nvm/versions/node/YOUR_VERSION/bin"}
```

The launcher uses the local Python environment. Changing its location or removing
Node/Codex requires updating configuration and rerunning the scheduled health check.

The activation script first runs a temporary **read-only health check through Task
Scheduler**: local prerequisites/export access, Codex login status, headless Chromium
launch and public HTTPS endpoint reachability. It does not generate resumes, scrape
jobs, send messages or submit applications. Only a successful check enables the real
task, `Vanguard-Daily-Pipeline`. The temporary check is removed afterward.

The task uses the current Windows account with S4U logon rather than storing a Windows
password. The default requests the highest available Windows privileges to launch WSL;
Linux commands still run as the specified Linux user. This machine's limited Windows
background identity could not start WSL, while the elevated task passed the check.
`-RunLevel Limited` is available on installations where that health check succeeds.
S4U restricts Windows network/encrypted-file access; whether a particular WSL
installation works under it must be tested, which is why activation includes the real
scheduled check. The check does not prove behavior after a future Windows sign-out,
reboot, security policy change or account/session expiration.

The installer requires Windows timezone **US Mountain Standard Time** (Arizona). It
refuses a different timezone rather than silently changing your system clock. Windows
triggers follow the Windows timezone, so check the task if you later change that setting.
The launcher's duplicate/checkpoint slots remain in America/Phoenix, including DST dates.

## Reliability settings

- Daily triggers at 06:00 and 12:00, including weekends.
- Request wake from sleep/hibernate and start after a missed scheduled time.
- Allow battery operation; do not stop a run merely because AC disconnects.
- Ignore an overlapping Windows task instance, plus OS file locks in the launcher/pipeline.
- Retry failures twice at 30-minute intervals. Already completed slots do not run again.
- Persist the active pipeline stage before running it. Failed generation/export resumes
  from that stage before collecting another batch, including across the next slot.
- No forced Windows execution time limit, avoiding a timeout killing WSL while it is
  writing resumes/state. A hung external browser/model process can require intervention.

If an old slot's unfinished generation/export occupies the next slot, finishing that
work consumes the slot. Fresh scraping resumes at the following scheduled time. This
prioritizes finishing files over growing a backlog on repeated retries. A failed combined
scrape/fetch stage retries that component as a whole; its deduplication skips known jobs.

Windows [documents wake requests](https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-waketorun)
and [missed-run catch-up](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-startwhenavailable-settingstype-element).
Wake requires hardware/firmware and the active power plan to permit wake timers. A fully
powered-off computer cannot execute a local task. Missed runs can start when Windows is
available again. Keep internet and the D: export drive available. Expired Jobright/Codex
sessions, verification challenges, model allowance limits and unsupported page changes
still require attention. An always-on machine would be needed to avoid dependence on
this PC's power state; this setup does not provision cloud infrastructure.

The separate `scripts/enable-wake-timers.ps1` can be run in administrator PowerShell
if the active plan disables wake timers. It enables the wake-timer setting for AC and
battery, saving the original scheme/value information to
`var/scheduler/power-settings-before.json`. This setting applies to other wake timers
on the same power plan too. To restore a recorded value, use
`powercfg /setdcvalueindex SCHEME_GUID SUB_SLEEP RTCWAKE ORIGINAL_DC_VALUE` (or
`/setacvalueindex` for AC), followed by `powercfg /setactive SCHEME_GUID`.

## Check results and manage the task

From the Vanguard directory in WSL:

```bash
./scripts/run-scheduled status
./scripts/run-scheduled --dry-run
ls -lt var/scheduler/
```

`var/scheduler/` contains a timestamped log per invocation, persistent checkpoint state,
and the Windows setup result. `var/runs/` contains pipeline stage reports. These files
can contain private job/applicant information and stay out of Git. Look for the last
successful timestamp and any failure in the current entry. The `status` command shows
the next nominal slot; it does not query whether the Windows task is enabled.
Failures starting WSL itself are captured separately in Windows at
`%LOCALAPPDATA%\Vanguard\scheduler-bootstrap.log`.

In Windows PowerShell:

```powershell
Get-ScheduledTask -TaskName Vanguard-Daily-Pipeline
Get-ScheduledTaskInfo -TaskName Vanguard-Daily-Pipeline
# Pause / resume:
Disable-ScheduledTask -TaskName Vanguard-Daily-Pipeline
Enable-ScheduledTask -TaskName Vanguard-Daily-Pipeline
# Run now (performs a real pipeline run unless this slot is already completed):
Start-ScheduledTask -TaskName Vanguard-Daily-Pipeline
# Remove the schedule:
Unregister-ScheduledTask -TaskName Vanguard-Daily-Pipeline -Confirm:$false
```

For a fresh scheduled health check, use `install-windows-schedule.ps1 -Mode SmokeTest`
with the same path/user/distro arguments in administrator PowerShell, inspect its result,
then remove the temporary `Vanguard-Schedule-Check` task. Never leave a failed check
scheduled for automatic retries. Changing times requires changing both installer
triggers and the launcher's slot definitions, then reinstalling the task.
