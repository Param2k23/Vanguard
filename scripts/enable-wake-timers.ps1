# Only change the active plan's wake-timer setting; preserve its previous values.
$ErrorActionPreference = 'Stop'
$powercfg = "$env:WINDIR\System32\powercfg.exe"
$active = (& $powercfg /getactivescheme) -join "`n"
$scheme = [regex]::Match($active, '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}').Value
if (-not $scheme) { throw 'Cannot identify active power scheme; no changes made.' }
$policy = (& $powercfg /query $scheme SUB_SLEEP RTCWAKE) -join "`n"
$ac = [regex]::Match($policy, 'Current AC Power Setting Index:\s+0x([0-9a-fA-F]+)')
$dc = [regex]::Match($policy, 'Current DC Power Setting Index:\s+0x([0-9a-fA-F]+)')
if (-not $ac.Success -or -not $dc.Success) { throw 'Cannot read wake-timer values; no changes made.' }
$dir = Join-Path (Split-Path -Parent $PSScriptRoot) 'var\scheduler'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$backup = Join-Path $dir 'power-settings-before.json'
if (-not (Test-Path $backup)) {
    @{scheme=$scheme; ac=[Convert]::ToInt32($ac.Groups[1].Value,16); dc=[Convert]::ToInt32($dc.Groups[1].Value,16); saved_at=(Get-Date).ToString('o')} | ConvertTo-Json | Set-Content -Encoding UTF8 $backup
}
& $powercfg /setacvalueindex $scheme SUB_SLEEP RTCWAKE 1
if ($LASTEXITCODE -ne 0) { throw 'Failed to enable AC wake timers.' }
& $powercfg /setdcvalueindex $scheme SUB_SLEEP RTCWAKE 1
if ($LASTEXITCODE -ne 0) { throw 'Failed to enable battery wake timers.' }
& $powercfg /setactive $scheme
if ($LASTEXITCODE -ne 0) { throw 'Failed to apply wake-timer settings.' }
& $powercfg /query $scheme SUB_SLEEP RTCWAKE | Set-Content -Encoding UTF8 (Join-Path $dir 'power-settings-after.txt')
